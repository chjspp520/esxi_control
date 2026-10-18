"""虚拟机管理：创建 / 克隆 / 改配 / 删除 / 磁盘模式切换的 ESXi 操作层。

只做 pyVmomi 原语，全部函数设计为在 executor 线程中调用（视图层负责
``hass.async_add_executor_job``）。

三条工程铁律（均由真机验证得到，见项目记忆 / 验证脚本）：

* **pyVmomi 同进程并发多连接会静默退出** —— 模块级 ``_LOCK`` 包住「连接 →
  操作 → 断开」整段，所有公开函数天然串行；
* **独立 ESXi（无 vCenter）不支持 ``CloneVM_Task``**（抛
  ``vmodl.fault.NotSupported``，与许可证无关）——「完整克隆」走
  ``CopyDatastoreFile_Task`` 复制 VMDK 后再 ``CreateVM_Task`` 挂载；
* 「引用模板母盘」= 以 ``independent_nonpersistent`` 模式 **add 现有 VMDK**
  （不带 fileOperation=create），多台 VM 可同时开机，redo 落在消费者各自
  VM 目录、关机丢弃，母盘只读不锁。

删除规则（用户硬性要求）在 :func:`delete_vm`：
含非持久盘（或磁盘在 VM 自己目录之外）→ ``UnregisterVM()`` + 删除该 VM
自己的目录；全持久盘 → ``Destroy_Task``。运行中 / 挂起一律拒绝。
"""
import contextlib
import logging
import re
import threading
import time

from pyVmomi import vim

from .cdrom_ops import ensure_datastore_dir
from .esxi import esx_connect, esx_disconnect

_LOGGER = logging.getLogger(__name__)

#: 全局串行锁：pyvmomi 在同进程里开第二个连接可能导致整个进程静默退出
_LOCK = threading.RLock()

#: 免费 Hypervisor 的每 VM vCPU 上限（6.5 免费版 8 vCPU；eval 模式不受限，
#: 这里取主机线程数与 8 的较小值只是保守的 UI 校验，ESXi 自身仍会再裁一次）
_FREE_VCPU_LIMIT = 8

#: 设备 key 规划：SCSI 控制器 1000+ ；IDE 200 ；磁盘 2000+ ；网卡 4000+
_SCSI_KEY_BASE = 1000
_DISK_KEY_BASE = 2000
_NIC_KEY_BASE = 4000
_IDE_KEY = 200
_CDROM_KEY = 3000

#: 常用客户机标识（guestId）。选「复制配置」时以前端实际拿到的源 VM
#: guestId 为准，不受此表限制；手工新建才用这个下拉
GUEST_OS_CHOICES = [
    ("other4xLinux64Guest", "其他 Linux 4.x 或更高内核（64 位）"),
    ("otherLinux64Guest", "其他 Linux（64 位）"),
    ("otherLinuxGuest", "其他 Linux（32 位）"),
    ("centos9_64Guest", "CentOS/Rocky 9（64 位）"),
    ("centos8_64Guest", "CentOS 8（64 位）"),
    ("centos7_64Guest", "CentOS 7（64 位）"),
    ("centos7Guest", "CentOS 7（32 位）"),
    ("rhel8_64Guest", "RHEL 8（64 位）"),
    ("rhel7_64Guest", "RHEL 7（64 位）"),
    ("ubuntu64Guest", "Ubuntu Linux（64 位）"),
    ("ubuntuGuest", "Ubuntu Linux（32 位）"),
    ("debian11_64Guest", "Debian 11（64 位）"),
    ("debian10_64Guest", "Debian 10（64 位）"),
    ("debian9_64Guest", "Debian 9（64 位）"),
    ("windows2019srv_64Guest", "Windows Server 2019（64 位）"),
    ("windows2016srv_64Guest", "Windows Server 2016（64 位）"),
    ("windows9Server64Guest", "Windows Server 2012 R2（64 位）"),
    ("windows9_64Guest", "Windows 10 / 11（64 位）"),
    ("windows9Guest", "Windows 10（32 位）"),
    ("windows7_64Guest", "Windows 7（64 位）"),
    ("windows7Guest", "Windows 7（32 位）"),
    ("freebsd12_64Guest", "FreeBSD 12（64 位）"),
    ("freebsd64Guest", "FreeBSD（64 位）"),
    ("otherGuest", "其他（32 位）"),
    ("otherGuest64", "其他（64 位）"),
]

#: 网卡型号选择
NIC_CHOICES = [
    ("vmxnet3", "VMXNET 3（推荐，需装 VMware Tools）"),
    ("e1000e", "E1000e（Intel 82574，兼容性好）"),
    ("e1000", "E1000（老系统）"),
]

#: SCSI 控制器选择
SCSI_CHOICES = [
    ("lsilogic", "LSI Logic Parallel"),
    ("lsisas", "LSI Logic SAS"),
    ("pvscsi", "VMware Paravirtual（高性能）"),
]

_NIC_CLASSES = {
    "e1000": vim.vm.device.VirtualE1000,
    "e1000e": vim.vm.device.VirtualE1000e,
    "vmxnet3": vim.vm.device.VirtualVmxnet3,
    "vmxnet2": vim.vm.device.VirtualVmxnet2,
    "vmxnet": vim.vm.device.VirtualVmxnet,
}
_NIC_NAMES = {
    "VirtualE1000": "e1000",
    "VirtualE1000e": "e1000e",
    "VirtualVmxnet3": "vmxnet3",
    "VirtualVmxnet2": "vmxnet2",
    "VirtualVmxnet": "vmxnet",
}
_SCSI_CLASSES = {
    "lsilogic": vim.vm.device.VirtualLsiLogicController,
    "lsisas": vim.vm.device.VirtualLsiLogicSASController,
    "pvscsi": vim.vm.device.ParaVirtualSCSIController,
}
_SCSI_NAMES = {
    "VirtualLsiLogicController": "lsilogic",
    "VirtualLsiLogicSASController": "lsisas",
    "ParaVirtualSCSIController": "pvscsi",
    "VirtualAHCIController": "ahci",
}

DISK_MODE_PERSISTENT = "persistent"
DISK_MODE_NONPERSISTENT = "independent_nonpersistent"
VALID_DISK_MODES = {DISK_MODE_PERSISTENT, DISK_MODE_NONPERSISTENT}


# --------------------------------------------------------------------------- #
# 基础工具
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _session(conn_details):
    """持全局锁建立一次短连接；退出时一定断开。"""
    with _LOCK:
        conn = esx_connect(**conn_details)
        if not conn:
            raise RuntimeError(
                f"无法连接到 ESXi 主机 {conn_details.get('host', '')}")
        try:
            yield conn, conn.RetrieveContent()
        finally:
            esx_disconnect(conn)


def _fault_text(error):
    """把 fault 链（含 faultCause / faultMessage）摊成可读中文字符串。"""
    parts = []
    node = error
    depth = 0
    while node is not None and depth < 4:
        msg = getattr(node, "msg", None) or str(node)
        label = type(node).__name__ if depth == 0 else "  原因"
        parts.append(f"{label}: {msg}")
        messages = getattr(node, "faultMessage", None) or []
        for item in messages:
            text = getattr(item, "message", None)
            if text and text not in parts:
                parts.append(f"  {text}")
        node = getattr(node, "faultCause", None)
        depth += 1
    return "\n".join(parts)


def _wait_task(task, label="任务", timeout=300):
    """轮询等待 TaskInfo；失败抛 RuntimeError。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = task.info
        if info.state == "success":
            return info.result
        if info.state == "error":
            raise RuntimeError(f"{label}失败：\n{_fault_text(info.error)}")
        time.sleep(0.3)
    raise RuntimeError(f"{label}超时（{timeout} 秒）")


def _container_view(content, vim_type):
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim_type], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _norm_name(value):
    """快照里的主机键是 ``name.replace(' ','_').lower()``，比对时同样归一化。"""
    return (value or "").replace(" ", "_").lower()


def _get_host(content, host_name=None):
    hosts = _container_view(content, vim.HostSystem)
    if not hosts:
        raise RuntimeError("找不到任何 ESXi 主机")
    if len(hosts) == 1:
        return hosts[0]
    if not host_name or host_name == "-":
        raise RuntimeError("vCenter 下存在多台主机，必须指定主机名")
    wanted = _norm_name(host_name)
    for host in hosts:
        if _norm_name(host.summary.config.name) == wanted or (
                _norm_name(host.name) == wanted):
            return host
    raise RuntimeError(f"找不到主机 {host_name}")


def _get_datacenter(content):
    for entity in content.rootFolder.childEntity or []:
        if isinstance(entity, vim.Datacenter):
            return entity
    raise RuntimeError("找不到数据中心")


def _find_vm(content, moid):
    for vm in _container_view(content, vim.VirtualMachine):
        if vm._moId == str(moid):
            return vm
    raise RuntimeError(f"找不到虚拟机（moId={moid}），可能已被删除")


def _find_datastore(host, ds_name):
    for ds in host.datastore or []:
        if ds.summary.name == ds_name and ds.summary.accessible:
            return ds
    raise RuntimeError(f"主机上找不到可访问的数据存储 {ds_name}")


def _find_network(host, network_name):
    for network in host.network or []:
        if network.name == network_name:
            return network
    return None


def _parse_ds_path(file_name):
    """``[datastore1] dir/sub/file.vmdk`` → ``("datastore1", "dir/sub/file.vmdk")``。"""
    match = re.match(r"^\s*\[([^\]]+)\]\s*(.+?)\s*$", file_name or "")
    if not match:
        return None, None
    return match.group(1), match.group(2).replace("\\", "/")


def _vmx_home(vm):
    """从 ``config.files.vmPathName`` 反解 VM 主目录（hostd 可能生成 _1 子目录，
    所以**绝不**按 VM 名字拼路径）。返回 ``(ds_name, "顶层目录", "完整目录")``。
    """
    raw = (getattr(vm.config.files, "vmPathName", "") or "").strip()
    ds_name, rel = _parse_ds_path(raw)
    if not ds_name:
        raise RuntimeError("无法解析虚拟机配置路径")
    rel = rel or ""
    if rel.lower().endswith(".vmx"):
        rel = rel.rsplit("/", 1)[0]
    full_dir = rel.rstrip("/")
    top_dir = full_dir.split("/", 1)[0] if full_dir else ""
    return ds_name, top_dir, full_dir


def _power_state(vm):
    return getattr(vm.runtime, "powerState", "unknown")


def _is_off(vm):
    return _power_state(vm) == "poweredOff"


def _disk_devices(vm):
    return [
        dev for dev in vm.config.hardware.device
        if isinstance(dev, vim.vm.device.VirtualDisk)
    ]


def _disk_mode(dev):
    return getattr(dev.backing, "diskMode", DISK_MODE_PERSISTENT)


def _devspec(device, operation, file_operation=None):
    spec = vim.vm.device.VirtualDeviceSpec(operation=operation, device=device)
    if file_operation is not None:
        spec.fileOperation = file_operation
    return spec


def _new_scsi(kind, bus_number, key):
    cls = _SCSI_CLASSES.get(kind, vim.vm.device.VirtualLsiLogicController)
    ctrl = cls(key=key, busNumber=bus_number, sharedBus="noSharing")
    return ctrl


def _new_ide_and_cdrom():
    """新建 VM 默认带一个客户端介质光驱（和 host client 新建向导一致）。"""
    ide = vim.vm.device.VirtualIDEController(key=_IDE_KEY, busNumber=0)
    cdrom = vim.vm.device.VirtualCdrom(key=_CDROM_KEY, controllerKey=_IDE_KEY,
                                      unitNumber=0)
    cdrom.backing = (
        vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo(deviceName=""))
    cdrom.connectable = (
        vim.vm.device.VirtualDevice.ConnectInfo(
            connected=False, startConnected=False, allowGuestControl=True))
    return [
        _devspec(ide, vim.vm.device.VirtualDeviceSpec.Operation.add),
        _devspec(cdrom, vim.vm.device.VirtualDeviceSpec.Operation.add),
    ]


# --------------------------------------------------------------------------- #
# 清单 / 资源
# ---------------------------------------------------------------------------
def get_inventory(conn_details, host_name=None):
    """供管理页一次拉全：主机资源、数据存储、端口组、全部 VM 的完整硬件清单。"""
    with _session(conn_details) as (_, content):
        host = _get_host(content, host_name)
        datastores = []
        for ds in host.datastore or []:
            summ = ds.summary
            datastores.append({
                "name": summ.name,
                "type": summ.type,
                "accessible": bool(summ.accessible),
                "free_gb": round((summ.freeSpace or 0) / 1073741824, 1),
                "capacity_gb": round((summ.capacity or 0) / 1073741824, 1),
            })
        portgroups = []
        seen = set()
        for pg in getattr(host.config.network, "portgroup", None) or []:
            spec = pg.spec
            if spec.name in seen:
                continue
            seen.add(spec.name)
            vlan = getattr(spec, "vlanId", None)
            portgroups.append({
                "name": spec.name,
                "vswitch": getattr(spec, "vswitchName", None),
                "vlan": getattr(vlan, "vlanId", vlan),
            })

        host_mem_total = host.hardware.memorySize
        host_mem_used = (getattr(host.summary.quickStats, "overallMemoryUsage", 0)
                         or 0) * 1048576
        vms = []
        for vm in _container_view(content, vim.VirtualMachine):
            try:
                vms.append(_vm_inventory(vm))
            except Exception as exc:  # noqa: BLE001  失效 VM 也要列出
                vms.append({
                    "moid": vm._moId, "name": vm.name,
                    "state": "invalid", "error": str(exc),
                })
        return {
            "host": {
                "name": host.summary.config.name,
                "version": host.summary.config.product.fullName,
                "cpu_threads": host.hardware.cpuInfo.numCpuThreads,
                "mem_total_gb": round(host_mem_total / 1073741824, 1),
                "mem_free_gb": round(
                    max(0, host_mem_total - host_mem_used) / 1073741824, 1),
            },
            "datastores": sorted(datastores, key=lambda x: x["name"]),
            "portgroups": sorted(portgroups, key=lambda x: x["name"]),
            "vms": sorted(vms, key=lambda x: x.get("name", "")),
            "guest_choices": [{"id": gid, "label": label}
                              for gid, label in GUEST_OS_CHOICES],
            "nic_choices": [{"id": gid, "label": label}
                            for gid, label in NIC_CHOICES],
            "scsi_choices": [{"id": gid, "label": label}
                             for gid, label in SCSI_CHOICES],
        }


def _vm_inventory(vm):
    config = vm.config
    runtime = vm.runtime
    disks, nics, controllers = [], [], []
    for dev in config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            backing = dev.backing
            ds_name, rel = _parse_ds_path(getattr(backing, "fileName", ""))
            disks.append({
                "key": dev.key,
                "label": getattr(dev.deviceInfo, "label", "硬盘"),
                "capacity_gb": round((dev.capacityInKB or 0) / 1048576, 2),
                "mode": getattr(backing, "diskMode", DISK_MODE_PERSISTENT),
                "thin": bool(getattr(backing, "thinProvisioned", False)),
                "file": getattr(backing, "fileName", None),
                "ds": ds_name,
                "path": rel,
                "controller_key": getattr(dev, "controllerKey", None),
                "unit": getattr(dev, "unitNumber", None),
            })
        elif isinstance(dev, vim.vm.device.VirtualEthernetCard):
            nics.append({
                "key": dev.key,
                "label": getattr(dev.deviceInfo, "label", "网卡"),
                "adapter": _NIC_NAMES.get(type(dev).__name__, "other"),
                "mac": getattr(dev, "macAddress", None),
                "network": getattr(dev.deviceInfo, "summary", None),
                "connected": bool(dev.connectable and dev.connectable.connected),
            })
        elif isinstance(dev, (vim.vm.device.VirtualSCSIController,
                              vim.vm.device.VirtualAHCIController)):
            controllers.append({
                "key": dev.key,
                "label": getattr(dev.deviceInfo, "label", "控制器"),
                "type": _SCSI_NAMES.get(type(dev).__name__, "other"),
                "bus_number": getattr(dev, "busNumber", None),
            })
    ds_name, top_dir, full_dir = _vmx_home(vm)
    return {
        "moid": vm._moId,
        "name": config.name,
        "state": _power_state(vm),
        "guest_id": config.guestId,
        "guest_os": config.guestFullName,
        "cpu": config.hardware.numCPU,
        "cores_per_socket": config.hardware.numCoresPerSocket,
        "mem_mb": config.hardware.memoryMB,
        "firmware": getattr(config, "firmware", "bios"),
        "cpu_hotadd": bool(getattr(config, "cpuHotAddEnabled", False)),
        "mem_hotadd": bool(getattr(config, "memoryHotAddEnabled", False)),
        "tools_status": str(getattr(vm.guest, "toolsStatus", "") or ""),
        "has_snapshots": vm.snapshot is not None,
        "vmx_ds": ds_name,
        "vmx_dir": top_dir,
        "vmx_dir_full": full_dir,
        "disks": disks,
        "nics": nics,
        "controllers": controllers,
        "is_template": any(
            d["mode"] == DISK_MODE_NONPERSISTENT for d in disks),
    }


# --------------------------------------------------------------------------- #
# 创建（手工空白盘）
# ---------------------------------------------------------------------------
def _build_fresh_disk(name, ds_name, size_gb, thin, key, ctrl_key, unit):
    disk = vim.vm.device.VirtualDisk(
        key=key, controllerKey=ctrl_key, unitNumber=unit,
        capacityInKB=int(round(float(size_gb) * 1048576)))
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    # 必须给完整 vmdk 路径；给目录会被 hostd 拼成双层嵌套
    backing.fileName = f"[{ds_name}] {name}/{name}.vmdk"
    backing.diskMode = DISK_MODE_PERSISTENT
    backing.thinProvisioned = bool(thin)
    backing.eagerlyScrub = False
    disk.backing = backing
    return _devspec(disk, vim.vm.device.VirtualDeviceSpec.Operation.add,
                    vim.vm.device.VirtualDeviceSpec.FileOperation.create)


def _build_nic(kind, network_name, network_obj, key, unit):
    cls = _NIC_CLASSES.get(kind, vim.vm.device.VirtualE1000e)
    nic = cls(key=key, unitNumber=unit)
    nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
        deviceName=network_name, network=network_obj)
    nic.addressType = "generated"
    nic.wakeOnLanEnabled = False
    return _devspec(nic, vim.vm.device.VirtualDeviceSpec.Operation.add)


def create_vm(conn_details, params, host_name=None):
    """手工参数创建一台空白盘虚拟机。

    params 字段：name / guest_id / cpu / memory_mb / firmware / datastore /
    disk_size_gb / provisioning(thin|thick) / network(可空) / nic_adapter /
    scsi
    """
    name = str(params["name"]).strip()
    ds_name = str(params["datastore"]).strip()
    size_gb = float(params.get("disk_size_gb") or 20)
    if size_gb <= 0:
        raise RuntimeError("磁盘容量必须大于 0")
    with _session(conn_details) as (_, content):
        host = _get_host(content, host_name)
        ds = _find_datastore(host, ds_name)
        if ds.summary.freeSpace < size_gb * 1073741824 * 0.95:
            raise RuntimeError(
                f"数据存储 {ds_name} 剩余空间不足（需要约 {size_gb:.0f}GB）")
        network_name = params.get("network") or None
        network_obj = _find_network(host, network_name) if network_name else None
        if network_name and network_obj is None:
            raise RuntimeError(f"找不到端口组 {network_name}")

        cpu = max(1, int(params.get("cpu") or 1))
        cpu = min(cpu, host.hardware.cpuInfo.numCpuThreads, _FREE_VCPU_LIMIT)
        mem_mb = max(4, int(params.get("memory_mb") or 512))

        changes = _new_ide_and_cdrom()
        scsi_kind = params.get("scsi") or "lsilogic"
        changes.append(_devspec(
            _new_scsi(scsi_kind, 0, _SCSI_KEY_BASE),
            vim.vm.device.VirtualDeviceSpec.Operation.add))
        changes.append(_build_fresh_disk(
            name, ds_name, size_gb,
            (params.get("provisioning") != "thick"),
            _DISK_KEY_BASE, _SCSI_KEY_BASE, 0))
        if network_name:
            changes.append(_build_nic(
                params.get("nic_adapter") or "e1000e",
                network_name, network_obj, _NIC_KEY_BASE, 0))

        spec = vim.vm.ConfigSpec(
            name=name,
            guestId=params.get("guest_id") or "otherLinux64Guest",
            numCPUs=cpu,
            memoryMB=mem_mb,
            numCoresPerSocket=cpu,
            files=vim.vm.FileInfo(vmPathName=f"[{ds_name}] {name}"),
            deviceChange=changes,
        )
        firmware = (params.get("firmware") or "").lower()
        if firmware in ("bios", "efi"):
            spec.firmware = firmware
        dc = _get_datacenter(content)
        _LOGGER.info("创建虚拟机 %s（%s, %s vCPU, %sMB, 磁盘 %sGB）",
                     name, ds_name, cpu, mem_mb, size_gb)
        task = dc.vmFolder.CreateVM_Task(
            config=spec, pool=host.parent.resourcePool, host=host)
        _wait_task(task, f"创建虚拟机 {name}", timeout=600)
        return {"name": name}


# --------------------------------------------------------------------------- #
# 以已有 VM 为样板创建（空白盘 / 完整克隆 / 引用非持久母盘）
# ---------------------------------------------------------------------------
def create_from_profile(conn_details, params, host_name=None):
    """以源 VM 的硬件配置为样板创建一台新 VM。

    params: name / src_moid / disk_source(new|clone|attach) /
            datastore(目标存储) / disk_size_gb(new 时可覆盖容量) /
            src_disk_key(attach 时选择母盘，缺省第一块)
    """
    name = str(params["name"]).strip()
    source = params.get("disk_source", "new")
    if source not in ("new", "clone", "attach"):
        raise RuntimeError(f"未知的磁盘来源 {source}")
    with _session(conn_details) as (_, content):
        host = _get_host(content, host_name)
        dc = _get_datacenter(content)
        src_vm = _find_vm(content, params["src_moid"])
        src_cfg = src_vm.config
        target_ds = str(params.get("datastore") or "").strip()
        if not target_ds:
            # 默认落到源 VM 所在存储
            target_ds = (src_vm.datastore[0].summary.name
                         if src_vm.datastore else None)
        if not target_ds:
            raise RuntimeError("未指定目标数据存储")
        ds_obj = _find_datastore(host, target_ds)

        src_disks = _disk_devices(src_vm)
        if not src_disks:
            raise RuntimeError("样板虚拟机没有硬盘")
        # 控制器：old_key → (kind, bus, new_key)
        ctrl_map = {}
        changes = _new_ide_and_cdrom()
        for dev in src_cfg.hardware.device:
            kind = _SCSI_NAMES.get(type(dev).__name__)
            if kind is None:
                continue  # 非磁盘控制器（IDE 等）跳过，光驱统一新建
            if kind == "ahci":
                # 样板盘在 SATA/AHCI 上：VMDK 可挂到任意 SCSI 控制器，
                # 统一落成 LSI SAS（对 Windows/Linux 兼容性都好）
                kind = "lsisas"
            bus = len(ctrl_map)
            new_key = _SCSI_KEY_BASE + len(ctrl_map)
            ctrl_map[dev.key] = (kind, bus, new_key)
            changes.append(_devspec(
                _new_scsi(kind, bus, new_key),
                vim.vm.device.VirtualDeviceSpec.Operation.add))
        if not ctrl_map:
            # 样板没有任何可用磁盘控制器（罕见）：补一个 LSI SAS
            ctrl_map["synthetic"] = ("lsisas", 0, _SCSI_KEY_BASE)
            changes.append(_devspec(
                _new_scsi("lsisas", 0, _SCSI_KEY_BASE),
                vim.vm.device.VirtualDeviceSpec.Operation.add))

        # ---- 磁盘三分支 ----
        if source == "attach":
            disk_key = params.get("src_disk_key")
            src_disk = next(
                (d for d in src_disks if disk_key is None
                 or d.key == int(disk_key)), src_disks[0])
            if not _is_off(src_vm):
                raise RuntimeError(
                    "引用母盘时样板（模板）虚拟机必须处于关机状态，"
                    "否则母盘被独占锁定，新虚拟机无法开机")
            attached = _attach_existing_disk(
                changes, src_disk, ctrl_map,
                mode=DISK_MODE_NONPERSISTENT, unit_index=0)
            if not attached:
                raise RuntimeError("样板硬盘挂在不受支持的控制器上，无法引用")
        elif source == "clone":
            if not _is_off(src_vm):
                raise RuntimeError("完整克隆要求样板虚拟机已关机")
            if vm_has_nonpersistent(src_vm):
                raise RuntimeError(
                    "样板虚拟机含有非持久硬盘，不能完整克隆；"
                    "请改用「引用非持久母盘」")
            _clone_all_disks(content, dc, src_vm, src_disks, ctrl_map,
                             changes, name, target_ds)
        else:  # new：空白盘，容量取表单覆盖或样板第一块盘容量
            size_gb = params.get("disk_size_gb")
            if not size_gb:
                size_gb = round(src_disks[0].capacityInKB / 1048576, 2)
            if ds_obj.summary.freeSpace < float(size_gb) * 1073741824:
                raise RuntimeError(
                    f"数据存储 {target_ds} 剩余空间不足（需要约 {size_gb:.0f}GB）")
            first_ctrl_key = next(iter(ctrl_map.values()))[2]
            changes.append(_build_fresh_disk(
                name, target_ds, size_gb, True,
                _DISK_KEY_BASE, first_ctrl_key, 0))

        # ---- 网卡：继承型号与端口组，MAC 重新生成 ----
        src_nics = [
            dev for dev in src_cfg.hardware.device
            if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        nic_unit = 0
        for dev in src_nics:
            pg_name = getattr(dev.deviceInfo, "summary", None)
            if not pg_name or pg_name in ("-", "假"):
                continue
            network_obj = _find_network(host, pg_name)
            if network_obj is None:
                # 样板端口组在本机不存在：跳过该网卡，创建结果里由调用方提示
                continue
            kind = _NIC_NAMES.get(type(dev).__name__, "e1000e")
            changes.append(_build_nic(
                kind, pg_name, network_obj,
                _NIC_KEY_BASE + nic_unit, nic_unit))
            nic_unit += 1

        spec = vim.vm.ConfigSpec(
            name=name,
            guestId=src_cfg.guestId,
            numCPUs=src_cfg.hardware.numCPU,
            memoryMB=src_cfg.hardware.memoryMB,
            numCoresPerSocket=src_cfg.hardware.numCoresPerSocket,
            files=vim.vm.FileInfo(vmPathName=f"[{target_ds}] {name}"),
            deviceChange=changes,
        )
        firmware = (getattr(src_cfg, "firmware", "") or "").lower()
        if firmware in ("bios", "efi"):
            spec.firmware = firmware
        if getattr(src_cfg, "cpuHotAddEnabled", False):
            spec.cpuHotAddEnabled = True
        if getattr(src_cfg, "memoryHotAddEnabled", False):
            spec.memoryHotAddEnabled = True

        _LOGGER.info("按样板 %s 创建虚拟机 %s（磁盘来源=%s）",
                     src_vm.name, name, source)
        task = dc.vmFolder.CreateVM_Task(
            config=spec, pool=host.parent.resourcePool, host=host)
        _wait_task(task, f"创建虚拟机 {name}", timeout=900)
        return {"name": name, "disk_source": source}


def vm_has_nonpersistent(vm):
    return any(_disk_mode(d) == DISK_MODE_NONPERSISTENT
               for d in _disk_devices(vm))


def _attach_existing_disk(changes, src_disk, ctrl_map, mode, unit_index):
    """把已有 VMDK 作为新 VM 的磁盘加入 deviceChange（不创建文件）。"""
    ctrl_info = ctrl_map.get(getattr(src_disk, "controllerKey", None))
    if ctrl_info is None:
        # 样板盘控制器未被复制（synthetic 兜底等）：挂到第一个新控制器
        ctrl_info = next(iter(ctrl_map.values()))
    _kind, _bus, ctrl_key = ctrl_info
    disk = vim.vm.device.VirtualDisk(
        key=_DISK_KEY_BASE + unit_index,
        controllerKey=ctrl_key,
        unitNumber=_next_free_unit([], unit_index),
        capacityInKB=src_disk.capacityInKB)
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    backing.fileName = src_disk.backing.fileName
    backing.diskMode = mode
    disk.backing = backing
    changes.append(_devspec(
        disk, vim.vm.device.VirtualDeviceSpec.Operation.add))
    return True


def _next_free_unit(used_units, preferred):
    units = set(used_units or [])
    unit = preferred if preferred != 7 else 8
    while unit in units or unit == 7:
        unit += 1
    return unit


def _clone_all_disks(content, dc, src_vm, src_disks, ctrl_map, changes,
                     new_name, target_ds):
    """逐块复制样板的持久磁盘到新目录并挂到新 VM（独立主机克隆退路）。"""
    ensure_datastore_dir(content, target_ds, new_name)
    fm = content.fileManager
    used_units = {}
    for index, src_disk in enumerate(src_disks):
        mode = _disk_mode(src_disk)
        if mode == DISK_MODE_NONPERSISTENT:
            raise RuntimeError("样板含非持久硬盘，不能完整克隆")
        src_path = src_disk.backing.fileName
        suffix = ".vmdk" if index == 0 else f"_{index + 1}.vmdk"
        dst_path = f"[{target_ds}] {new_name}/{new_name}{suffix}"
        task = fm.CopyDatastoreFile_Task(
            sourceName=src_path, sourceDatacenter=dc,
            destinationName=dst_path, destinationDatacenter=dc, force=True)
        _wait_task(task, f"复制样板硬盘 {index + 1}", timeout=1800)

        ctrl_info = ctrl_map.get(getattr(src_disk, "controllerKey", None))
        if ctrl_info is None:
            ctrl_info = next(iter(ctrl_map.values()))
        _kind, _bus, ctrl_key = ctrl_info
        used = used_units.setdefault(ctrl_key, set())
        unit = _next_free_unit(
            used, getattr(src_disk, "unitNumber", index) or index)
        used.add(unit)
        disk = vim.vm.device.VirtualDisk(
            key=_DISK_KEY_BASE + index, controllerKey=ctrl_key, unitNumber=unit,
            capacityInKB=src_disk.capacityInKB)
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        backing.fileName = dst_path
        backing.diskMode = DISK_MODE_PERSISTENT
        disk.backing = backing
        changes.append(_devspec(
            disk, vim.vm.device.VirtualDeviceSpec.Operation.add))


# --------------------------------------------------------------------------- #
# 改配
# ------------------------------------------------------------------
def reconfigure_vm(conn_details, moid, changes, host_name=None):
    """统一改配入口。changes 可含：

    * cpu / memory_mb：运行中仅在热加开关打开且为增大时允许；
    * resize_disks: [{key, size_gb}]（只能增大；在线要求无快照的持久盘）；
    * add_disks: [{size_gb, provisioning}]；
    * remove_disks: [{key, delete_file}]；
    * add_nics: [{network, adapter}]；
    * edit_nics: [{key, network}]；
    * remove_nics: [{key}]。
    """
    with _session(conn_details) as (_, content):
        host = _get_host(content, host_name)
        vm = _find_vm(content, moid)
        running = not _is_off(vm)
        cfg = vm.config
        spec = vim.vm.ConfigSpec()
        device_change = []
        messages = []

        # ---- CPU / 内存 ----
        new_cpu = changes.get("cpu")
        if new_cpu:
            new_cpu = int(new_cpu)
            if new_cpu != cfg.hardware.numCPU:
                if running and (
                        not cfg.cpuHotAddEnabled or new_cpu < cfg.hardware.numCPU):
                    raise RuntimeError(
                        "运行中只能在已开启 CPU 热添加时**增大** vCPU，"
                        "减小或关闭热添加请先关机")
                new_cpu = min(
                    new_cpu, host.hardware.cpuInfo.numCpuThreads, _FREE_VCPU_LIMIT)
                spec.numCPUs = new_cpu
                spec.numCoresPerSocket = new_cpu
        new_mem = changes.get("memory_mb")
        if new_mem:
            new_mem = int(new_mem)
            if new_mem != cfg.hardware.memoryMB:
                if running and (
                        not cfg.memoryHotAddEnabled
                        or new_mem < cfg.hardware.memoryMB):
                    raise RuntimeError(
                        "运行中只能在已开启内存热添加时**增大**内存，"
                        "减小请先关机")
                spec.memoryMB = new_mem

        devices = {dev.key: dev for dev in cfg.hardware.device}

        # ---- 磁盘扩容 ----
        for item in changes.get("resize_disks", []) or []:
            dev = devices.get(int(item["key"]))
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                raise RuntimeError(f"找不到要扩容的硬盘（key={item['key']}）")
            old_gb = dev.capacityInKB / 1048576
            new_gb = float(item["size_gb"])
            if new_gb < old_gb - 0.01:
                raise RuntimeError(
                    f"硬盘只能扩容不能缩小（当前 {old_gb:.1f}GB）")
            if abs(new_gb - old_gb) < 0.01:
                continue
            if running:
                if vm.snapshot is not None:
                    raise RuntimeError("虚拟机有快照时不能在线扩容，请先删除快照")
                if _disk_mode(dev) != DISK_MODE_PERSISTENT:
                    raise RuntimeError("非持久硬盘不能扩容，请先关机并转回持久模式")
            dev.capacityInKB = int(round(new_gb * 1048576))
            device_change.append(_devspec(
                dev, vim.vm.device.VirtualDeviceSpec.Operation.edit))
            messages.append(f"硬盘扩容 {old_gb:.1f}→{new_gb:.1f}GB")

        # ---- 加盘 ----
        existing_keys = [
            dev.key for dev in cfg.hardware.device
            if getattr(dev, "key", None) is not None]
        next_key = -100
        for item in changes.get("add_disks", []) or []:
            while next_key in existing_keys:
                next_key -= 1
            size_gb = float(item["size_gb"])
            ds_name = item.get("datastore")
            if not ds_name:
                ds_name = vm.datastore[0].summary.name if vm.datastore else None
            if not ds_name:
                raise RuntimeError("未指定新硬盘的数据存储")
            ds_obj = _find_datastore(host, ds_name)
            if ds_obj.summary.freeSpace < size_gb * 1073741824:
                raise RuntimeError(f"数据存储 {ds_name} 空间不足，无法加硬盘")
            # 找一个 SCSI 控制器，没有就先加一个
            scsi = next(
                (d for d in cfg.hardware.device
                 if isinstance(d, vim.vm.device.VirtualSCSIController)), None)
            if scsi is None:
                scsi = _new_scsi("lsilogic", 0, next_key)
                next_key -= 1
                scsi_key = scsi.key
                device_change.append(_devspec(
                    scsi, vim.vm.device.VirtualDeviceSpec.Operation.add))
                used_units = set()
            else:
                scsi_key = scsi.key
                used_units = {
                    d.unitNumber for d in cfg.hardware.device
                    if getattr(d, "controllerKey", None) == scsi_key
                    and getattr(d, "unitNumber", None) is not None}
            unit = _next_free_unit(used_units, len(used_units))
            disk = vim.vm.device.VirtualDisk(
                key=next_key, controllerKey=scsi_key, unitNumber=unit,
                capacityInKB=int(round(size_gb * 1048576)))
            next_key -= 1
            backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            existing_vdisks = [
                d for d in cfg.hardware.device
                if isinstance(d, vim.vm.device.VirtualDisk)]
            seq = len(existing_vdisks) + 1
            vmdk_name = f"{vm.name}_{seq}.vmdk" if seq > 1 else f"{vm.name}.vmdk"
            backing.fileName = f"[{ds_name}] {vm.name}/{vmdk_name}"
            backing.diskMode = DISK_MODE_PERSISTENT
            backing.thinProvisioned = (item.get("provisioning") != "thick")
            disk.backing = backing
            device_change.append(_devspec(
                disk, vim.vm.device.VirtualDeviceSpec.Operation.add,
                vim.vm.device.VirtualDeviceSpec.FileOperation.create))
            messages.append(f"新增硬盘 {size_gb:.0f}GB")

        # ---- 删盘 ----
        for item in changes.get("remove_disks", []) or []:
            dev = devices.get(int(item["key"]))
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                raise RuntimeError(f"找不到要删除的硬盘（key={item['key']}）")
            if running:
                raise RuntimeError("删除硬盘需要先关机")
            spec_item = _devspec(
                dev, vim.vm.device.VirtualDeviceSpec.Operation.remove)
            if item.get("delete_file"):
                spec_item.fileOperation = (
                    vim.vm.device.VirtualDeviceSpec.FileOperation.destroy)
            device_change.append(spec_item)
            messages.append(
                "删除硬盘" + ("（并删除磁盘文件）" if item.get("delete_file")
                          else "（保留磁盘文件）"))

        # ---- 网卡 ----
        nic_devices = [
            dev for dev in cfg.hardware.device
            if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        nic_unit = max(
            [getattr(d, "unitNumber", -1) for d in nic_devices] + [-1]) + 1
        for item in changes.get("add_nics", []) or []:
            network_name = item.get("network")
            if not network_name:
                raise RuntimeError("新网卡未选择端口组")
            network_obj = _find_network(host, network_name)
            if network_obj is None:
                raise RuntimeError(f"找不到端口组 {network_name}")
            nic = _NIC_CLASSES.get(
                item.get("adapter") or "e1000e",
                vim.vm.device.VirtualE1000e)(key=next_key, unitNumber=nic_unit)
            next_key -= 1
            nic_unit += 1
            nic.backing = (
                vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                    deviceName=network_name, network=network_obj))
            nic.addressType = "generated"
            nic.wakeOnLanEnabled = False
            device_change.append(_devspec(
                nic, vim.vm.device.VirtualDeviceSpec.Operation.add))
            messages.append(f"新增网卡→{network_name}")

        for item in changes.get("edit_nics", []) or []:
            dev = devices.get(int(item["key"]))
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                raise RuntimeError(f"找不到要修改的网卡（key={item['key']}）")
            network_name = item.get("network")
            network_obj = _find_network(host, network_name)
            if network_obj is None:
                raise RuntimeError(f"找不到端口组 {network_name}")
            dev.backing = (
                vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                    deviceName=network_name, network=network_obj))
            device_change.append(_devspec(
                dev, vim.vm.device.VirtualDeviceSpec.Operation.edit))
            messages.append(f"网卡改接→{network_name}")

        for item in changes.get("remove_nics", []) or []:
            dev = devices.get(int(item["key"]))
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                raise RuntimeError(f"找不到要删除的网卡（key={item['key']}）")
            device_change.append(_devspec(
                dev, vim.vm.device.VirtualDeviceSpec.Operation.remove))
            messages.append("删除网卡")

        if spec.numCPUs is None and spec.memoryMB is None and not device_change:
            raise RuntimeError("没有需要修改的内容")
        spec.deviceChange = device_change
        _wait_task(vm.ReconfigVM_Task(spec), "修改虚拟机硬件", timeout=600)
        _LOGGER.info("虚拟机 %s 硬件已修改：%s", vm.name, "、".join(messages))
        return {"name": vm.name, "changes": messages}


# --------------------------------------------------------------------------- #
# 磁盘模式（模板开关）
# --------------------------------------------------------------------------- #
def set_disk_mode(conn_details, moid, disk_key, mode, host_name=None):
    """切换某块硬盘 persistent ↔ independent_nonpersistent（模板开关）。"""
    if mode not in VALID_DISK_MODES:
        raise RuntimeError(f"不支持的磁盘模式 {mode}")
    with _session(conn_details) as (_, content):
        vm = _find_vm(content, moid)
        if not _is_off(vm):
            raise RuntimeError("切换磁盘模式（设置/取消模板）必须先关机")
        if vm.snapshot is not None:
            raise RuntimeError(
                "该虚拟机存在快照，独立磁盘与快照互斥，请先删除全部快照")
        disk = next(
            (d for d in _disk_devices(vm) if d.key == int(disk_key)), None)
        if disk is None:
            raise RuntimeError(f"找不到硬盘（key={disk_key}）")
        if _disk_mode(disk) == mode:
            return {"name": vm.name, "mode": mode, "changed": False}
        disk.backing.diskMode = mode
        spec = vim.vm.ConfigSpec(deviceChange=[_devspec(
            disk, vim.vm.device.VirtualDeviceSpec.Operation.edit)])
        _wait_task(vm.ReconfigVM_Task(spec), "切换磁盘模式", timeout=300)
        _LOGGER.info("虚拟机 %s 硬盘 %s 模式切换为 %s",
                     vm.name, disk_key, mode)
        return {"name": vm.name, "mode": mode, "changed": True}


def set_template(conn_details, moid, enabled, host_name=None):
    """整 VM 模板开关：所有硬盘一起切到 nonpersistent / 切回 persistent。

    一次 ReconfigVM 提交全部磁盘；要求关机且无快照。
    """
    target = (DISK_MODE_NONPERSISTENT if enabled
              else DISK_MODE_PERSISTENT)
    with _session(conn_details) as (_, content):
        vm = _find_vm(content, moid)
        if not _is_off(vm):
            raise RuntimeError("设置 / 取消模板必须先关机")
        if vm.snapshot is not None:
            raise RuntimeError(
                "该虚拟机存在快照，独立磁盘与快照互斥，请先删除全部快照")
        device_change = []
        changed_count = 0
        for disk in _disk_devices(vm):
            if _disk_mode(disk) == target:
                continue
            disk.backing.diskMode = target
            device_change.append(_devspec(
                disk, vim.vm.device.VirtualDeviceSpec.Operation.edit))
            changed_count += 1
        if not device_change:
            return {"name": vm.name, "enabled": enabled, "changed": 0}
        spec = vim.vm.ConfigSpec(deviceChange=device_change)
        _wait_task(vm.ReconfigVM_Task(spec), "切换模板状态", timeout=300)
        _LOGGER.info("虚拟机 %s 模板状态→%s（%s 块硬盘）",
                     vm.name, target, changed_count)
        return {"name": vm.name, "enabled": enabled, "changed": changed_count}


# --------------------------------------------------------------------------- #
# 删除（双路径）
# --------------------------------------------------------------------------- #
def delete_vm(conn_details, moid, host_name=None):
    """删除虚拟机。

    * 运行中 / 挂起：一律拒绝（调用方负责先关机）；
    * 含非持久盘，或有磁盘位于 VM 主目录之外（共享母盘）：
      ``UnregisterVM()`` 取消注册 + 删除该 VM 自己的目录，绝不动外部磁盘；
    * 全部为内部持久盘：``Destroy_Task`` 从磁盘彻底移除。
    """
    with _session(conn_details) as (_, content):
        dc = _get_datacenter(content)
        vm = _find_vm(content, moid)
        if not _is_off(vm):
            raise RuntimeError(
                "虚拟机正在运行或处于挂起状态，请先彻底关机再删除")

        ds_name, top_dir, full_dir = _vmx_home(vm)
        external_disks = []
        has_nonpersistent = False
        for disk in _disk_devices(vm):
            mode = _disk_mode(disk)
            if mode == DISK_MODE_NONPERSISTENT:
                has_nonpersistent = True
            disk_ds, disk_rel = _parse_ds_path(
                getattr(disk.backing, "fileName", ""))
            if disk_ds and disk_rel:
                disk_dir = disk_rel.rsplit("/", 1)[0]
                if disk_ds != ds_name or not (
                        disk_dir == full_dir or disk_dir.startswith(
                            full_dir + "/")):
                    external_disks.append(disk.backing.fileName)

        if not has_nonpersistent and not external_disks:
            _wait_task(vm.Destroy_Task(), "从磁盘移除虚拟机", timeout=600)
            _LOGGER.info("虚拟机 %s 已从磁盘彻底移除（Destroy）", vm.name)
            return {"name": vm.name, "method": "destroy"}

        # 安全路径：取消注册 + 只删自己的目录
        if external_disks:
            _LOGGER.info("虚拟机 %s 含外部/共享磁盘，取消注册时保留：%s",
                         vm.name, external_disks)
        vm.UnregisterVM()
        time.sleep(1)
        _delete_datastore_dir(content, dc, ds_name, top_dir)
        _LOGGER.info(
            "虚拟机 %s 已取消注册并删除其目录 [%s] %s（非持久=%s，外部盘=%s）",
            vm.name, ds_name, top_dir, has_nonpersistent,
            len(external_disks))
        return {
            "name": vm.name,
            "method": "unregister",
            "deleted_dir": f"[{ds_name}] {top_dir}",
            "external_disks_kept": external_disks,
        }


def _delete_datastore_dir(content, dc, ds_name, dir_name):
    """删除数据存储目录（DeleteDatastoreFile_Task 可递归删非空目录）。

    主路径失败时退路：浏览器枚举文件，从深到浅逐个删除。
    """
    full = f"[{ds_name}] {dir_name}"
    try:
        _wait_task(
            content.fileManager.DeleteDatastoreFile_Task(
                name=full, datacenter=dc),
            "删除虚拟机目录", timeout=600)
        return
    except vim.fault.FileNotFound:
        return
    except RuntimeError:
        _LOGGER.debug("直接删除目录 %s 失败，尝试逐文件删除", full,
                      exc_info=True)

    # 退路：SearchDatastoreSubFolders 枚举后逐文件、逐目录删除
    ds = next((d for d in _container_view(content, vim.Datastore)
               if d.summary.name == ds_name), None)
    if ds is None:
        raise RuntimeError(f"找不到数据存储 {ds_name}")
    task = ds.browser.SearchDatastoreSubFolders_Task(
        full, vim.host.DatastoreBrowser.SearchSpec())
    _wait_task(task, "枚举待删目录", timeout=120)
    entries = []
    for result in task.info.result or []:
        folder = result.folderPath.rstrip("/")
        for f in result.file or []:
            entries.append(f"{folder}/{f.path}")
    for path in sorted(set(entries), key=len, reverse=True):
        try:
            _wait_task(
                content.fileManager.DeleteDatastoreFile_Task(
                    name=path, datacenter=dc),
                "删除残留文件 " + path, timeout=120)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("退路删除 %s 失败", path, exc_info=True)
    # 文件清空后再删各级空目录（深→浅）
    parts = dir_name.split("/")
    for index in range(len(parts), 0, -1):
        try:
            content.fileManager.DeleteDatastoreFile_Task(
                name=f"[{ds_name}] {'/'.join(parts[:index])}", datacenter=dc)
        except Exception:  # noqa: BLE001
            break
