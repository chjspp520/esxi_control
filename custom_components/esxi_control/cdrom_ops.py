"""光盘镜像（ISO）全链操作。

功能闭环：
  上传本地文件/文件夹 → iso9660 打包成 ISO → HTTP PUT 到 ESXi 数据存储
  → ReconfigVM_Task 热挂载（没有光驱就热添加一个 IDE CD/DVD）
  → 弹出：断开并把 backing 退回「客户端设备」→ 删除数据存储上的 ISO 文件

设计要点：
  * 上传走 ESXi 内置的 /folder/ access API（与 datastore browser 同源），
    用标准库 urllib，HA Container 里零额外依赖；
  * 文件管理（建目录/删除）优先走 pyVmomi FileManager，比 HTTP 更兼容 5.5；
  * 所有重操作返回结构化结果或抛 RuntimeError，由调用方（实体/网页/服务）提示；
  * 「挂载记录」由调用方（console_web 的 HA Store）维护，本模块只负责 ESXi 侧动作，
    因此不会误删用户手工挂载的其它 ISO。
"""
import base64
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from pyVmomi import vim

from .esxi import esx_connect, esx_disconnect, _find_vm  # noqa: SLF001

_LOGGER = logging.getLogger(__name__)

#: 上传分片读块大小
_UPLOAD_BLOCK = 1024 * 1024
#: 光驱在 IDE 总线上的两个槽位
_IDE_UNITS = (0, 1)


def _get_datacenter(content, datastore=None):
    """取（唯一的）数据中心；独立主机的 FileManager 也需要它。"""
    if datastore is not None:
        node = datastore.parent
        while node is not None and not isinstance(node, vim.Datacenter):
            node = node.parent
        if node is not None:
            return node
    for entity in content.rootFolder.childEntity or []:
        if isinstance(entity, vim.Datacenter):
            return entity
    return None


def choose_datastore(content, vm):
    """挑目标数据存储：优先 VM 配置文件所在存储，其次剩余空间最大的可写存储。"""
    datastores = [
        ds for ds in (vm.datastore or [])
        if getattr(ds.summary, "accessible", False)
    ]
    if not datastores:
        # 退一步：主机可见的全部存储
        host = vm.runtime.host
        datastores = [
            ds for ds in (host.datastore if host else []) or []
            if getattr(ds.summary, "accessible", False)
        ]
    if not datastores:
        raise RuntimeError("该主机没有可访问的数据存储")

    vm_path = getattr(vm.summary.config, "vmPathName", "") or ""
    for ds in datastores:
        if f"[{ds.summary.name}]" in vm_path:
            return ds.summary.name
    return max(datastores, key=lambda ds: ds.summary.freeSpace or 0).summary.name


def ensure_datastore_dir(content, ds_name, dir_name):
    """确保数据存储根目录下存在 dir_name 目录；已存在不算错误。"""
    datacenter = _get_datacenter(content)
    full_path = f"[{ds_name}] {dir_name}"
    try:
        content.fileManager.MakeDirectory(
            name=full_path, datacenter=datacenter
        )
        _LOGGER.info("数据存储目录已创建：%s", full_path)
    except vim.fault.FileAlreadyExists:
        pass
    except Exception as error:  # pylint: disable=broad-except
        # 5.5 偶发以通用 fault 报「已存在」，目录存在性以下载/上传成败为准
        _LOGGER.debug("创建目录 %s 时返回: %s", full_path, error)


def upload_to_datastore(conn_details, local_path, ds_name, ds_path):
    """用 ESXi access API（HTTP PUT）把本地文件传到数据存储。

    返回 True；HTTP 失败抛 RuntimeError。
    """
    host = conn_details["host"]
    port = conn_details.get("port", 443)
    quoted_path = "/".join(
        urllib.parse.quote(segment) for segment in ds_path.split("/")
    )
    query = urllib.parse.urlencode(
        {"dsName": ds_name, "dcPath": "ha-datacenter"}
    )
    url = f"https://{host}:{port}/folder/{quoted_path}?{query}"

    token = base64.b64encode(
        f"{conn_details['user']}:{conn_details['pwd']}".encode("utf-8")
    ).decode("ascii")

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    total_bytes = 0
    file_size = __import__("os").path.getsize(local_path)
    request = urllib.request.Request(url, method="PUT")
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/octet-stream")
    # 显式 Content-Length：否则 urllib 对文件体用 chunked 传输，老版 hostd 不接受
    request.add_header("Content-Length", str(file_size))
    with open(local_path, "rb") as handle:
        request.data = handle
        try:
            response = urllib.request.urlopen(  # noqa: S310
                request, context=context, timeout=300
            )
            total_bytes = handle.tell()
            code = response.getcode()
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"上传失败：ESXi 返回 HTTP {error.code} {error.reason}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"上传失败：无法连接 ESXi（{error.reason}）") from error
    if code not in (200, 201, 204):
        raise RuntimeError(f"上传失败：意外的 HTTP 状态码 {code}")
    _LOGGER.info("ISO 已上传到 [%s] %s（%s 字节）", ds_name, ds_path, total_bytes)
    return True


def delete_datastore_file(content, ds_name, ds_path):
    """删除数据存储上的文件（弹出后自动清理用）；文件不存在视为成功。"""
    datacenter = _get_datacenter(content)
    full_name = f"[{ds_name}] {ds_path}"
    task = content.fileManager.DeleteDatastoreFile_Task(
        name=full_name, datacenter=datacenter
    )
    while task.info.state not in ("success", "error"):
        time.sleep(0.2)
    if task.info.state == "error":
        msg = getattr(task.info.error, "msg", "unknown error")
        if "not found" not in msg.lower() and "不存在" not in msg:
            raise RuntimeError(f"删除 ISO 失败：{msg}")
        _LOGGER.debug("ISO 已不存在：%s", full_name)
    else:
        _LOGGER.info("ISO 已删除：%s", full_name)
    return True


def _cdrom_connectable():
    return vim.vm.device.VirtualDevice.ConnectInfo(
        connected=True,
        startConnected=True,
        allowGuestControl=True,
        status="ok",
    )


def _find_ide_slot(vm):
    """找一个空闲的 IDE (controllerKey, unitNumber)；没有空位时返回 (None, None)。"""
    controllers = [
        device for device in vm.config.hardware.device
        if isinstance(device, vim.vm.device.VirtualIDEController)
    ]
    if not controllers:
        return None, None
    used = {
        (device.controllerKey, device.unitNumber)
        for device in vm.config.hardware.device
        if getattr(device, "controllerKey", None) is not None
    }
    for controller in controllers:
        for unit in _IDE_UNITS:
            if (controller.key, unit) not in used:
                return controller.key, unit
    return None, None


def upload_and_mount(conn_details, vm_uuid, local_iso_path, ds_dir, dest_name=None):
    """一条龙：选存储 → 建目录 → HTTP PUT 上传 ISO → 热挂载到虚拟机。

    供控制台光盘 API 调用（打包工作 iso9660.build_iso 由调用方先完成）。
    ``ds_dir`` 是数据存储根下的相对目录（如 ``ha_iso_uploads``）；
    ``dest_name`` 为最终文件名（不含目录），缺省用本地文件名。
    返回 {ds, path, cdrom_label, iso(完整 [ds] path 串)}。
    """
    import os as _os

    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        content = conn.RetrieveContent()
        vm = _find_vm(content, vm_uuid)
        if vm is None:
            raise RuntimeError("未找到目标虚拟机")
        ds_name = choose_datastore(content, vm)
        ensure_datastore_dir(content, ds_name, ds_dir)
        file_name = dest_name or _os.path.basename(local_iso_path)
        ds_path = f"{ds_dir}/{file_name}"
        upload_to_datastore(conn_details, local_iso_path, ds_name, ds_path)
    finally:
        esx_disconnect(conn)

    result = mount_iso(conn_details, vm_uuid, ds_name, ds_path)
    return {
        "ds": ds_name,
        "path": ds_path,
        "cdrom_label": result.get("cdrom_label"),
        "iso": result.get("iso") or f"[{ds_name}] {ds_path}",
    }


def mount_iso(conn_details, vm_uuid, ds_name, ds_path, iso_label=None):
    """把数据存储上的 ISO 挂到虚拟机的第一个光驱；没有光驱就热添加一个。

    返回 {changed, cdrom_label}。
    """
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        content = conn.RetrieveContent()
        vm = _find_vm(content, vm_uuid)
        if vm is None:
            raise RuntimeError("未找到目标虚拟机")

        file_name = f"[{ds_name}] {ds_path}"
        backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=file_name)

        cdrom = next(
            (
                device for device in vm.config.hardware.device
                if isinstance(device, vim.vm.device.VirtualCdrom)
            ),
            None,
        )

        spec = vim.vm.ConfigSpec()
        if cdrom is None:
            controller_key, unit = _find_ide_slot(vm)
            if controller_key is None:
                raise RuntimeError("该虚拟机没有空闲的 IDE 插槽，无法热添加光驱")
            existing_keys = [
                device.key for device in vm.config.hardware.device
                if getattr(device, "key", None) is not None
            ]
            new_key = -1 * (max(abs(key) for key in existing_keys) + 100)
            cdrom = vim.vm.device.VirtualCdrom(
                key=new_key,
                controllerKey=controller_key,
                unitNumber=unit,
            )
            operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            label = "CD/DVD 驱动器 1"
        else:
            operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            label = getattr(cdrom.deviceInfo, "label", "CD/DVD 驱动器")

        cdrom.backing = backing
        cdrom.connectable = _cdrom_connectable()
        device_spec = vim.vm.device.VirtualDeviceSpec(
            operation=operation, device=cdrom
        )
        spec.deviceChange = [device_spec]

        task = vm.ReconfigVM_Task(spec)
        while task.info.state not in ("success", "error"):
            time.sleep(0.2)
        if task.info.state == "error":
            raise RuntimeError(
                f"挂载 ISO 失败：{getattr(task.info.error, 'msg', 'unknown error')}"
            )
        _LOGGER.info("虚拟机 %s 已挂载 %s", vm.name, file_name)
        return {"changed": True, "cdrom_label": label, "iso": file_name}
    finally:
        esx_disconnect(conn)


def eject_iso(conn_details, vm_uuid, delete_path=None):
    """弹出虚拟机当前的 ISO 镜像：断开连接并退回「客户端设备」backing。

    delete_path = (ds_name, ds_path) 时，弹出成功后一并删除数据存储上的文件。
    返回 {ejected: bool, deleted: bool}。
    """
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        content = conn.RetrieveContent()
        vm = _find_vm(content, vm_uuid)
        if vm is None:
            raise RuntimeError("未找到目标虚拟机")

        iso_cdroms = [
            device for device in vm.config.hardware.device
            if isinstance(device, vim.vm.device.VirtualCdrom)
            and isinstance(
                device.backing, vim.vm.device.VirtualCdrom.IsoBackingInfo
            )
        ]
        if not iso_cdroms:
            return {"ejected": False, "deleted": False}

        changes = []
        for cdrom in iso_cdroms:
            # vSphere Client 的「弹出」效果：backing 退回客户端设备 + 断开
            cdrom.backing = (
                vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo(deviceName="")
            )
            cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
                connected=False,
                startConnected=False,
                allowGuestControl=True,
                status="ok",
            )
            spec = vim.vm.device.VirtualDeviceSpec(
                operation=vim.vm.device.VirtualDeviceSpec.Operation.edit,
                device=cdrom,
            )
            changes.append(spec)

        config_spec = vim.vm.ConfigSpec(deviceChange=changes)
        task = vm.ReconfigVM_Task(config_spec)
        while task.info.state not in ("success", "error"):
            time.sleep(0.2)
        if task.info.state == "error":
            raise RuntimeError(
                f"弹出 ISO 失败：{getattr(task.info.error, 'msg', 'unknown error')}"
            )

        deleted = False
        if delete_path:
            ds_name, ds_path = delete_path
            delete_datastore_file(content, ds_name, ds_path)
            deleted = True
        _LOGGER.info("虚拟机 %s 已弹出 ISO", vm.name)
        return {"ejected": True, "deleted": deleted}
    finally:
        esx_disconnect(conn)
