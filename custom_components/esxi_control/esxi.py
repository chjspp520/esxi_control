# pylint: disable=import-outside-toplevel

"""ESXi commands for ESXi Control component."""
import logging
import re
import ssl
import socket
import atexit
from datetime import timezone
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl  # pylint: disable=no-name-in-module

try:
    # 集成为兼容自签证书会主动跳过校验（见 ignore_ssl_verify），urllib3 为此每次都刷
    # InsecureRequestWarning —— 一次开关机就能刷出几十条，把真正有用的日志淹掉。
    # 只关掉这一个类别，其它 urllib3 警告照常。
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # pylint: disable=broad-except
    pass

from .const import SUPPORTED_PRODUCTS

_LOGGER = logging.getLogger(__name__)


def ignore_ssl_verify():
    """Create SSL context that ignores certificate verification for ESXi 5.5 compatibility."""
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.options |= ssl.OP_NO_SSLv3
        context.set_ciphers('DEFAULT:@SECLEVEL=1')
        return context
    except Exception as e:
        _LOGGER.error("Failed to create SSL context: %s", str(e))
        return None


def esx_connect(host, user, pwd, port, ssl):
    """Establish connection with host/vcenter."""
    service_instance = None

    try:
        # connect depending on SSL_VERIFY setting
        if ssl is False:
            # Use custom SSL context that ignores certificate verification
            ssl_context = ignore_ssl_verify()
            if ssl_context:
                service_instance = SmartConnect(
                    host=host, user=user, pwd=pwd, port=port, sslContext=ssl_context
                )
            else:
                # Fallback to disableSslCertValidation if context creation fails
                service_instance = SmartConnect(
                    host=host, user=user, pwd=pwd, port=port, disableSslCertValidation=True
                )
        else:
            service_instance = SmartConnect(host=host, user=user, pwd=pwd, port=port)

        if service_instance:
            current_session = service_instance.content.sessionManager.currentSession.key
            _LOGGER.debug("Logged in - session %s", current_session)
            # Register disconnect to ensure clean connection cleanup
            atexit.register(Disconnect, service_instance)
        else:
            _LOGGER.error("Failed to create service instance for %s", host)
            return None

    except ConnectionRefusedError as error:
        _LOGGER.error("Connection refused to %s: %s", host, error)
        return None
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Failed to connect to %s: %s", host, error)
        return None

    return service_instance


def esx_disconnect(conn):
    """Kill connection from host/vcenter."""

    if conn:
        try:
            current_session = conn.content.sessionManager.currentSession.key
            Disconnect(conn)
            ## This is an old method to disconnect without leaving an active session on the ESXi host
            ## Keeping this commented out, but will remove in future release
            # conn._stub.pool[0][0].sock.shutdown(2)  # pylint: disable=protected-access
            _LOGGER.debug("Logged out - session %s", current_session)
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.debug(error)


def check_license(lic):
    """Retrieve license from connected system."""
    _LOGGER.debug("Checking license type")

    if not lic or not hasattr(lic, 'licenses'):
        _LOGGER.warning("No license information available")
        return False

    for license_obj in lic.licenses:
        if not hasattr(license_obj, 'properties'):
            continue

        product_name = None
        for key in license_obj.properties:
            if key.key == "ProductName":
                product_name = key.value
                break

        if not product_name or product_name not in SUPPORTED_PRODUCTS:
            continue

        _LOGGER.debug("Found %s license", product_name)

        # Check for vCenter Server (index 1)
        if product_name == SUPPORTED_PRODUCTS[1]:
            return True

        # Check for ESX Server (index 0) with vSphere API feature
        if product_name == SUPPORTED_PRODUCTS[0]:
            for feature in license_obj.properties:
                if (feature.key == "feature" and
                    hasattr(feature.value, 'key') and
                    feature.value.key == "vimapi"):
                    _LOGGER.debug("vSphere API feature enabled")
                    return True

    _LOGGER.warning("No supported license found")
    return False


def get_license_info(lic, host):
    """Get license information."""
    expiration = "n/a"
    product = "n/a"
    status = "n/a"

    for key in lic.properties:
        if key.key == "ProductName":
            product = key.value
        if key.key == "count_disabled":
            expiration = "never"
        if key.key == "expirationHours":
            expiration = round((key.value / 24))

    if isinstance(expiration, int):
        if expiration > 30:
            status = "Ok"
        if expiration <= 30:
            status = "Expiring Soon"
        if expiration < 1:
            status = "expired"
    else:
        status = "Ok"

    license_data = {
        "name": lic.name,
        "status": status,
        "product": product,
        "expiration_days": expiration,
        "host": host,
        "license_key": getattr(lic, 'licenseKey', 'n/a'),
    }

    _LOGGER.debug(license_data)

    return license_data


def get_host_info(host, content=None):
    """Get host information."""
    host_summary = host.summary
    host_state = host_summary.runtime.powerState
    host_name = host_summary.config.name.replace(" ", "_").lower()
    host_capability = host.capability

    _LOGGER.debug("vmhost: %s state is %s", host_name, host_state)

    if hasattr(host_summary.runtime, "inMaintenanceMode"):
        host_mm_mode = host_summary.runtime.inMaintenanceMode
    else:
        host_mm_mode = "N/A"

    if host_state == "poweredOn":
        host_version = host_summary.config.product.version
        host_build = host_summary.config.product.build
        host_uptime = round(host_summary.quickStats.uptime / 3600, 1)
        host_cpu_total = round(
            host_summary.hardware.cpuMhz * host_summary.hardware.numCpuCores / 1000, 1
        )
        host_mem_total = round(host_summary.hardware.memorySize / 1073741824, 2)
        host_cpu_usage = round(host_summary.quickStats.overallCpuUsage / 1000, 1)
        host_mem_usage = round(host_summary.quickStats.overallMemoryUsage / 1024, 2)

        # Get current power policy
        try:
            host_power_policy = host.config.powerSystemInfo.currentPolicy.shortName
        except Exception as e:
            _LOGGER.debug("Could not get current power policy for %s: %s", host_name, e)
            host_power_policy = "n/a"

        # Get available power policies
        available_power_policies = []
        try:
            if (hasattr(host.config, 'powerSystemCapability') and
                host.config.powerSystemCapability):
                for policy in host.config.powerSystemCapability.availablePolicy:
                    available_power_policies.append(policy.shortName)
                available_power_policies = sorted(available_power_policies)
        except Exception as e:
            _LOGGER.debug("Could not get available power policies for %s: %s", host_name, e)

        host_vms = len(host.vm)
        host_vms_on = sum(1 for vm in host.vm if vm.runtime.powerState == "poweredOn")

        # ---- 以下为增强详情（5.5 MOB 全量勘探后设计，逐项独立容错）----
        host_detail = _get_host_detail(host)
        # 登录会话清单在 ServiceContent 上（不属于 HostSystem）
        if content is not None:
            try:
                host_detail["sessions"] = _get_sessions(content)
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.debug("读取登录会话失败: %s", error)
    else:
        host_version = "n/a"
        host_build = "n/a"
        host_uptime = "n/a"
        host_cpu_total = "n/a"
        host_cpu_usage = "n/a"
        host_mem_total = "n/a"
        host_mem_usage = "n/a"
        host_power_policy = "n/a"
        available_power_policies = []
        host_vms = "n/a"
        host_vms_on = "n/a"
        host_detail = _empty_host_detail()

        _LOGGER.debug("Unable to return stats for %s", host_name)

    host_data = {
        "name": host_name,
        "original_name": host_summary.config.name,  # Store original for exact matching
        "state": host_state,
        "version": host_version,
        "build": host_build,
        "uptime_hours": host_uptime,
        "cputotal_ghz": host_cpu_total,
        "cpuusage_ghz": host_cpu_usage,
        "memtotal_gb": host_mem_total,
        "memusage_gb": host_mem_usage,
        "maintenance_mode": host_mm_mode,
        "shutdown_supported": host_capability.shutdownSupported,
        "power_policy": host_power_policy,
        "available_power_policies": available_power_policies,
        "vms": host_vms,
        "vms_on": host_vms_on,
        # 主机总体健康 / 连接状态（MOB root 下最直观的两个状态量）
        "overall_status": getattr(host, "overallStatus", "n/a") or "n/a",
        "connection_state": getattr(host.runtime, "connectionState", "n/a") or "n/a",
        "hyperthreading_active": _get_hyperthreading(host),
    }
    # 详情里既有标量（自动成为逐属性传感器），也有嵌套结构（在
    #   const.HOST_INTERNAL_KEYS 里登记，由专门的诊断传感器/binary_sensor/switch 承载）
    host_data.update(host_detail)

    _LOGGER.debug(host_data)

    return host_data


def _empty_host_detail():
    """主机关机 / 详情不可用时的安全空壳（保证实体能算出 available=False 而不崩）。"""
    return {
        "moid": None,
        "hardware_vendor": "n/a",
        "hardware_model": "n/a",
        "bios_version": "n/a",
        "bios_date": "n/a",
        "cpu_sockets": "n/a",
        "cpu_cores": "n/a",
        "cpu_threads": "n/a",
        "ntp_servers": "n/a",
        "ntp_timezone": "n/a",
        "lockdown": None,
        "services": [],
        "health": {
            "total": 0,
            "green": 0,
            "yellow": 0,
            "red": 0,
            "unknown": 0,
            "alerts": [],
        },
        "network": {"vswitch": [], "portgroup": [], "pnic": [], "vmknic": [], "dns": "", "gateway": ""},
        "firewall": {"total": 0, "enabled": 0, "rulesets": []},
        "sessions": {"total": 0, "items": []},
        "autostart": {"enabled": False, "entries": []},
        "storage": {"hba": [], "luns": [], "volumes": []},
    }


def _get_hyperthreading(host):
    """超线程是否激活（取不到时返回 n/a，保持传感器可见）。"""
    try:
        active = host.config.hyperThread.active
        return bool(active)
    except Exception:  # pylint: disable=broad-except
        return "n/a"


def _get_sessions(content):
    """当前登录会话清单（sessionManager.sessionList）。

    只保留审计需要的字段，时间转 ISO 字符串；最多 30 条，避免属性超长。
    """
    result = {"total": 0, "items": []}
    try:
        sessions = content.sessionManager.sessionList or []
    except Exception:  # pylint: disable=broad-except
        return result
    items = []
    for sess in sessions:
        login_time = getattr(sess, "loginTime", None)
        active_time = getattr(sess, "lastActiveTime", None)
        items.append(
            {
                "user": getattr(sess, "userName", "?"),
                "ip": getattr(sess, "ipAddress", None) or "local",
                "login_time": login_time.isoformat() if login_time else None,
                "last_active": active_time.isoformat() if active_time else None,
                "agent": getattr(sess, "userAgent", None),
                "calls": getattr(sess, "callCount", 0),
            }
        )
    items.sort(key=lambda item: item.get("last_active") or "", reverse=True)
    result["total"] = len(items)
    result["items"] = items[:30]
    return result


def _get_autostart(host):
    """主机自启动配置：总开关 + 每台 VM 的启停顺序/动作。"""
    result = {"enabled": False, "entries": []}
    try:
        config = host.configManager.autoStartManager.config
    except Exception:  # pylint: disable=broad-except
        return result
    try:
        result["enabled"] = bool(config.defaults.enabled)
    except Exception:  # pylint: disable=broad-except
        pass
    for info in getattr(config, "powerInfo", []) or []:
        result["entries"].append(
            {
                # key 是 VirtualMachine 托管对象引用，真正的 moId 在 _moId 上
                # （str(mor) 会是 "vim.VirtualMachine:1002"，不能直接和快照 moid 比）
                "moid": getattr(info.key, "_moId", None) or str(info.key),
                "start_order": getattr(info, "startOrder", None),
                "start_action": getattr(info, "startAction", None),
                "start_delay": getattr(info, "startDelay", None),
                "stop_action": getattr(info, "stopAction", None),
                "stop_delay": getattr(info, "stopDelay", None),
            }
        )
    return result


def _get_storage(host):
    """存储拓扑：HBA / LUN(含多路径活动数) / 已挂载 VMFS 卷（只读诊断）。"""
    result = {"hba": [], "luns": [], "volumes": []}
    try:
        storage_system = host.configManager.storageSystem
        device_info = storage_system.storageDeviceInfo
    except Exception:  # pylint: disable=broad-except
        return result

    # ---- HBA ----
    for hba in getattr(device_info, "hostBusAdapter", []) or []:
        result["hba"].append(
            {
                "device": getattr(hba, "device", None),
                "model": getattr(hba, "model", None),
                "driver": getattr(hba, "driver", None),
                "status": getattr(hba, "status", None),
            }
        )

    # ---- 多路径：canonicalName -> (active, total) ----
    path_counts = {}
    try:
        for lun_path in getattr(device_info, "multipathInfo", None).lun or []:
            paths = getattr(lun_path, "path", []) or []
            active = sum(
                1 for path in paths if getattr(getattr(path, "state", None), "key", "") == "active"
            )
            path_counts[lun_path.id] = (active, len(paths))
    except Exception:  # pylint: disable=broad-except
        pass

    # ---- LUN ----
    for lun in getattr(device_info, "scsiLun", []) or []:
        capacity_gb = None
        try:
            cap = lun.capacity
            capacity_gb = round(cap.blockSize * cap.block / 1073741824, 2)
        except Exception:  # pylint: disable=broad-except
            pass
        active_paths, total_paths = path_counts.get(lun.canonicalName, (0, 0))
        result["luns"].append(
            {
                "name": getattr(lun, "canonicalName", None),
                "vendor": getattr(lun, "vendor", None),
                "model": getattr(lun, "model", None),
                "type": getattr(lun, "lunType", None),
                "capacity_gb": capacity_gb,
                "paths_active": active_paths,
                "paths_total": total_paths,
            }
        )

    # ---- 已挂载文件系统卷（VMFS/NFS 等）----
    try:
        volume_info = storage_system.fileSystemVolumeInfo
        for mount in getattr(volume_info, "mountInfo", []) or []:
            volume = mount.volume
            result["volumes"].append(
                {
                    "name": getattr(volume, "name", None),
                    "type": getattr(volume, "type", None),
                    "capacity_gb": round(getattr(volume, "capacity", 0) / 1073741824, 2),
                    "path": getattr(mount.mountInfo, "path", None),
                    "mode": getattr(mount.mountInfo, "accessMode", None),
                }
            )
    except Exception:  # pylint: disable=broad-except
        pass

    return result


def _get_host_detail(host):
    """采集主机增强详情：硬件 / 服务 / 硬件健康 / 网络拓扑 / 防火墙 / NTP / 锁定模式 /
    自启动 / 存储拓扑。

    每个子系统独立 try：5.5 与 6.x/7.x 的属性差异（如 adminDisabled 在 5.5 上为 None、
    healthSystemRuntime 结构不同）不能让整轮数据刷新失败。
    """
    detail = _empty_host_detail()

    try:
        detail["moid"] = host._moId
    except Exception:  # pylint: disable=broad-except
        pass

    # ---- 硬件 / BIOS / CPU 拓扑（标量，直接成为传感器）----
    try:
        hw_sum = host.summary.hardware
        detail["hardware_vendor"] = getattr(hw_sum, "vendor", None) or "n/a"
        detail["hardware_model"] = getattr(hw_sum, "model", None) or "n/a"
        detail["cpu_sockets"] = getattr(
            hw_sum, "numCpuPkgs", None
        ) or _count_cpu_packages(host)
        detail["cpu_cores"] = getattr(hw_sum, "numCpuCores", "n/a")
        detail["cpu_threads"] = getattr(hw_sum, "numCpuThreads", "n/a")
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取硬件概要失败: %s", error)

    try:
        bios = host.hardware.biosInfo
        detail["bios_version"] = getattr(bios, "biosVersion", "n/a") or "n/a"
        # HA state 不接受 datetime 对象，统一转成 YYYY-MM-DD 字符串
        release_date = getattr(bios, "releaseDate", None)
        detail["bios_date"] = (
            release_date.strftime("%Y-%m-%d") if release_date else "n/a"
        )
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取 BIOS 信息失败: %s", error)

    # ---- NTP / 时区（标量）----
    try:
        dt_info = host.config.dateTimeInfo
        servers = list(getattr(dt_info.ntpConfig, "server", []) or [])
        detail["ntp_servers"] = ", ".join(servers) if servers else "未配置"
        detail["ntp_timezone"] = getattr(dt_info.timeZone, "name", None) or "n/a"
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取时间/NTP 配置失败: %s", error)

    # ---- 锁定模式（5.5 为非 Task 版 EnterLockdownMode；adminDisabled 在部分
    #      独立主机/老版本上为 None = 信息不可用，必须保留 None，不能 bool() 吞掉）----
    try:
        admin_disabled = host.config.adminDisabled
        detail["lockdown"] = None if admin_disabled is None else bool(admin_disabled)
    except Exception:  # pylint: disable=broad-except
        detail["lockdown"] = None

    # ---- 服务清单（running/policy；开关与策略实体只读这份快照）----
    try:
        detail["services"] = _get_services(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取服务清单失败: %s", error)

    # ---- 硬件健康传感器（numericSensorInfo；5.5 没有温度/风扇的 StatusInfo）----
    try:
        detail["health"] = _get_health(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取硬件健康失败: %s", error)

    # ---- 网络拓扑（vSwitch / 端口组 / 物理网卡 / VMkernel 网卡）----
    try:
        detail["network"] = _get_network(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取网络拓扑失败: %s", error)

    # ---- 防火墙规则集 ----
    try:
        detail["firewall"] = _get_firewall(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取防火墙规则失败: %s", error)

    # ---- 随主机自启动配置 ----
    try:
        detail["autostart"] = _get_autostart(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取自启动配置失败: %s", error)

    # ---- 存储拓扑（HBA / LUN / 卷 / 多路径）----
    try:
        detail["storage"] = _get_storage(host)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取存储拓扑失败: %s", error)

    return detail


def _count_cpu_packages(host):
    """numCpuPkgs 不可用时，按物理 CPU 封装数量兜底统计。"""
    try:
        return len(host.hardware.cpuPkg or []) or "n/a"
    except Exception:  # pylint: disable=broad-except
        return "n/a"


def _get_services(host):
    """服务系统清单：[{key, label, running, policy}]。"""
    services = []
    svc_info = host.configManager.serviceSystem.serviceInfo
    for svc in svc_info.service:
        services.append(
            {
                "key": svc.key,
                "label": getattr(svc, "label", svc.key) or svc.key,
                "running": bool(svc.running),
                "policy": svc.policy,  # on / off / automatic
            }
        )
    return services


def _get_health(host):
    """numericSensorInfo 汇总：按绿/黄/红/未知计数，非 green 的逐条列出。"""
    info = {
        "total": 0,
        "green": 0,
        "yellow": 0,
        "red": 0,
        "unknown": 0,
        "alerts": [],
    }
    try:
        runtime = host.runtime.healthSystemRuntime
        sensors = runtime.systemHealthInfo.numericSensorInfo
    except Exception:  # pylint: disable=broad-except
        return info

    for sensor in sensors or []:
        info["total"] += 1
        state = "unknown"
        try:
            state = str(sensor.healthState.key)
        except Exception:  # pylint: disable=broad-except
            pass
        if state in info:
            info[state] += 1
        else:
            info["unknown"] += 1
            state = "unknown"
        if state not in ("green",):
            info["alerts"].append(
                {
                    "name": getattr(sensor, "name", "?"),
                    "type": getattr(sensor, "sensorType", "?"),
                    "state": state,
                    "reading": getattr(sensor, "currentReading", None),
                }
            )
    return info


def _get_network(host):
    """网络拓扑（只取诊断所需的静态/半静态字段，避免实体爆炸）。"""
    net = host.config.network
    data = {"vswitch": [], "portgroup": [], "pnic": [], "vmknic": [], "dns": "", "gateway": ""}

    # DNS / 默认网关（MOB networkSystem 里排查网络问题最常用的两个标量）
    try:
        dns_config = getattr(net, "dnsConfig", None)
        servers = list(getattr(dns_config, "address", []) or [])
        data["dns"] = ", ".join(servers) if servers else "未配置"
        ip_route = getattr(net, "ipRouteConfig", None)
        data["gateway"] = getattr(getattr(ip_route, "defaultGateway", None), "ipAddress", None) or "n/a"
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取 DNS/网关失败: %s", error)

    try:
        for vsw in net.vswitch or []:
            data["vswitch"].append(
                {
                    "name": vsw.name,
                    "mtu": getattr(vsw.spec, "mtu", None),
                    "ports": getattr(vsw.spec, "numPorts", None),
                    "uplinks": list(getattr(vsw.spec.bridge, "nicDevice", []) or []),
                }
            )
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取 vSwitch 失败: %s", error)

    try:
        for pg in net.portgroup or []:
            data["portgroup"].append(
                {
                    "name": pg.spec.name,
                    "vswitch": pg.spec.vswitchName,
                    "vlan": getattr(pg.spec, "vlanId", None),
                }
            )
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取端口组失败: %s", error)

    try:
        for pnic in net.pnic or []:
            speed = None
            try:
                speed = pnic.linkSpeed.speedMb
            except Exception:  # pylint: disable=broad-except
                pass
            data["pnic"].append(
                {
                    "device": pnic.device,
                    "driver": getattr(pnic, "driver", None),
                    "mac": getattr(pnic, "mac", None),
                    "speed_mbps": speed,
                }
            )
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取物理网卡失败: %s", error)

    # ⚠️ 5.5 的 HostNetworkInfo 里字段名是 ``vnic``（高版本同样有 vnic，
    #   vmknic 是访问对象的类型名），写错属性名会让整个网络拓扑静默回退成空
    try:
        for vmk in getattr(net, "vnic", None) or getattr(net, "vmknic", None) or []:
            ip_info = getattr(vmk.spec, "ip", None)
            data["vmknic"].append(
                {
                    "device": vmk.device,
                    "mac": getattr(vmk.spec, "mac", None),
                    "ip": getattr(ip_info, "ipAddress", None),
                    "netmask": getattr(ip_info, "subnetMask", None),
                }
            )
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("读取 VMkernel 网卡失败: %s", error)

    return data


def _get_firewall(host):
    """防火墙规则集启用情况。"""
    result = {"total": 0, "enabled": 0, "rulesets": []}
    try:
        fw_info = host.configManager.firewallSystem.firewallInfo
    except Exception:  # pylint: disable=broad-except
        return result

    for ruleset in fw_info.ruleset or []:
        result["total"] += 1
        enabled = bool(ruleset.enabled)
        if enabled:
            result["enabled"] += 1
        ports = []
        for rule in getattr(ruleset, "rule", []) or []:
            ports.append(
                {
                    "port": getattr(rule, "port", None),
                    "protocol": getattr(rule, "protocol", None),
                    "direction": getattr(rule, "direction", None),
                }
            )
        result["rulesets"].append(
            {"key": ruleset.key, "enabled": enabled, "rules": ports[:4]}
        )
    return result


def get_datastore_info(datastore):
    """Get datastore information."""
    ds_summary = datastore.summary
    ds_name = ds_summary.name.replace(" ", "_").lower()
    ds_capacity = round(ds_summary.capacity / 1073741824, 2)
    ds_freespace = round(ds_summary.freeSpace / 1073741824, 2)
    ds_type = ds_summary.type.lower()

    ds_data = {
        "name": ds_name,
        "type": ds_type,
        "free_space_gb": ds_freespace,
        "total_space_gb": ds_capacity,
        "connected_hosts": len(datastore.host),
        "virtual_machines": len(datastore.vm),
    }

    _LOGGER.debug(ds_data)

    return ds_data


def get_vm_info(virtual_machine):
    """Get VM information."""
    vm_conf = virtual_machine.configStatus
    vm_sum = virtual_machine.summary
    vm_run = virtual_machine.runtime
    vm_snap = virtual_machine.snapshot

    vm_name = vm_sum.config.name.replace(" ", "_").lower()
    vm_proper_name = vm_sum.config.name

    # If a VM configuration is in INVALID state, return Inalid status
    if vm_conf == "red":
        vm_data = {
            "name": vm_name,
            "status": "Invalid",
            "moid": getattr(virtual_machine, "_moId", None),
        }
        _LOGGER.debug(vm_data)
        return vm_data

    vm_tools_status = vm_sum.guest.toolsStatus
    vm_used_space = round(vm_sum.storage.committed / 1073741824, 2)

    # if snapshots present, get number of snapshots
    if vm_snap is not None:
        vm_snapshots = len(list_snapshots(vm_snap.rootSnapshotList))
    else:
        vm_snapshots = 0

    # set vm_state based on power state
    if vm_sum.runtime.powerState == "poweredOn":
        vm_state = "running"
    elif vm_sum.runtime.powerState == "poweredOff":
        vm_state = "off"
    elif vm_sum.runtime.powerState == "suspended":
        vm_state = "suspended"
    else:
        vm_state = vm_sum.runtime.powerState

    # set runtime related attributes based on vm power state
    if vm_state == "running":
        # check if stats exist and set values, otherwise return "n/a"
        if vm_sum.quickStats.overallCpuUsage and vm_run.maxCpuUsage:
            vm_cpu_usage = round(
                ((vm_sum.quickStats.overallCpuUsage / vm_run.maxCpuUsage) * 100), 2
            )
        else:
            vm_cpu_usage = "n/a"
            _LOGGER.debug("Unable to return cpu usage for %s", vm_name)

        if vm_sum.quickStats.hostMemoryUsage:
            vm_mem_usage = round(vm_sum.quickStats.hostMemoryUsage, 2)
        else:
            vm_mem_usage = "n/a"
            _LOGGER.debug("Unable to return host memory usage for %s", vm_name)

        if vm_sum.quickStats.guestMemoryUsage:
            vm_mem_active = round(vm_sum.quickStats.guestMemoryUsage, 2)
        else:
            vm_mem_active = "n/a"
            _LOGGER.debug("Unable to return active memory usage")

        if vm_sum.quickStats.uptimeSeconds:
            vm_uptime = round(vm_sum.quickStats.uptimeSeconds / 3600, 1)
        else:
            vm_uptime = "n/a"
            _LOGGER.debug("Unable to return uptime for %s", vm_name)

        if vm_sum.guest.ipAddress:
            vm_ip = vm_sum.guest.ipAddress
        else:
            vm_ip = "n/a"
            _LOGGER.debug("Unable to return VM IP address for %s", vm_name)

        if vm_sum.guest.guestFullName:
            vm_guest_os = vm_sum.guest.guestFullName
        else:
            _LOGGER.debug(
                ("Unable to return Guest OS Name, using Configured Guest Name instead")
            )
            vm_guest_os = vm_sum.config.guestFullName
    else:
        vm_cpu_usage = "n/a"
        vm_mem_usage = "n/a"
        vm_mem_active = "n/a"
        vm_ip = "n/a"
        vm_uptime = "n/a"
        vm_guest_os = vm_sum.config.guestFullName

    vm_data = {
        "name": vm_name,
        "vm_name": vm_proper_name,
        "status": vm_sum.overallStatus,
        "state": vm_state,
        "uptime_hours": vm_uptime,
        "cpu_count": vm_sum.config.numCpu,
        "cpu_use_pct": vm_cpu_usage,
        "memory_allocated_mb": vm_sum.config.memorySizeMB,
        "memory_used_mb": vm_mem_usage,
        "memory_active_mb": vm_mem_active,
        "used_space_gb": vm_used_space,
        "tools_status": vm_tools_status,
        "guest_os": vm_guest_os,
        "guest_ip": vm_ip,
        "snapshots": vm_snapshots,
        "uuid": vm_sum.config.uuid,
        # moId 供虚拟机控制台 URL / 性能结果映射使用（内部字段，不建传感器）
        "moid": getattr(virtual_machine, "_moId", None),
        "host_name": vm_run.host.name,
    }
    # 网卡 / 光驱 / 待处理问题 / 待合并磁盘 / 显卡（内部字段，由专门实体承载）
    vm_data.update(_get_vm_devices(virtual_machine))

    _LOGGER.debug(vm_data)

    return vm_data


def _get_vm_devices(vm):
    """采集 VM 设备级状态：网卡、光驱、卡问题、待合并磁盘、显卡。

    全部字段放进 VM_INTERNAL_KEYS，不生成逐属性传感器，而是由专门的
    switch / binary_sensor / 诊断传感器承载（状态可写的才暴露控件）。
    """
    data = {
        "nics": [],
        "cdroms": [],
        "question": None,
        "consolidation_needed": False,
        "video": {},
    }
    # ---- 网卡 / 光驱 / 显卡 ----
    try:
        devices = vm.config.hardware.device or []
    except Exception:  # pylint: disable=broad-except
        devices = []

    for device in devices:
        # 网卡（VirtualE1000 / VirtualVmxnet 都继承自 VirtualEthernetCard）
        if isinstance(device, vim.vm.device.VirtualEthernetCard):
            connectable = getattr(device, "connectable", None)
            network_label = None
            try:
                network_label = device.deviceInfo.summary
            except Exception:  # pylint: disable=broad-except
                pass
            data["nics"].append(
                {
                    "label": getattr(device.deviceInfo, "label", None),
                    "network": network_label,
                    "mac": getattr(device, "macAddress", None),
                    "connected": bool(connectable and connectable.connected),
                    "start_connected": bool(
                        connectable and getattr(connectable, "startConnected", False)
                    ),
                    "adapter": type(device).__name__.replace("Virtual", ""),
                }
            )
        # 光驱
        elif isinstance(device, vim.vm.device.VirtualCdrom):
            backing = device.backing
            backing_kind = "none"
            iso_path = None
            device_name = None
            if isinstance(backing, vim.vm.device.VirtualCdrom.IsoBackingInfo):
                backing_kind = "iso"
                iso_path = getattr(backing, "fileName", None)
            elif isinstance(backing, vim.vm.device.VirtualCdrom.AtapiBackingInfo):
                backing_kind = "host"
                device_name = getattr(backing, "deviceName", None)
            elif isinstance(
                backing, vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo
            ):
                backing_kind = "client"
            connectable = getattr(device, "connectable", None)
            data["cdroms"].append(
                {
                    "key": getattr(device, "key", None),
                    "label": getattr(device.deviceInfo, "label", "CD/DVD 驱动器"),
                    "backing": backing_kind,
                    "iso": iso_path,
                    "device_name": device_name,
                    "connected": bool(connectable and connectable.connected),
                    "start_connected": bool(
                        connectable and getattr(connectable, "startConnected", False)
                    ),
                    "controller_key": getattr(device, "controllerKey", None),
                    "unit_number": getattr(device, "unitNumber", None),
                }
            )
        # 显卡
        elif isinstance(device, vim.vm.device.VirtualVideoCard):
            data["video"] = {
                "use_auto_detect": bool(getattr(device, "useAutoDetect", False)),
                "video_ram_kb": getattr(device, "videoRamSizeInKB", None),
                "num_displays": getattr(device, "numDisplays", None),
            }

    # ---- 卡问题（启动/关机时待用户回答的提问，会把 VM 卡住）----
    try:
        question = getattr(vm.runtime, "question", None)
        if question is not None:
            choices = []
            for choice in getattr(question.questionChoice, "choice", []) or []:
                choices.append(
                    {
                        "key": getattr(choice, "key", None),
                        "label": getattr(choice, "label", None),
                    }
                )
            data["question"] = {
                "id": getattr(question, "id", None),
                "text": getattr(question, "text", None),
                "choices": choices,
            }
    except Exception:  # pylint: disable=broad-except
        pass

    # ---- 删除快照后需要合并磁盘 ----
    try:
        data["consolidation_needed"] = bool(vm.runtime.consolidationNeeded)
    except Exception:  # pylint: disable=broad-except
        pass

    return data


def list_snapshots(snapshots, tree=False):
    """Get VM snapshot information.

    tree=True will return snapshot tree details required for snapshot removal
    """
    snapshot_data = []

    for snapshot in snapshots:
        if tree is True:
            snapshot_data.append(snapshot)
        else:
            snapshot_data.append(snapshot.id)
        snapshot_data = snapshot_data + list_snapshots(snapshot.childSnapshotList, tree)

    return snapshot_data


def host_pwr(hass, target_host_name, target_cmnd, conn_details, force, notify):
    """Host power commands - supports both ESXi and vCenter."""
    import time
    start_time = time.time()

    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get('host', 'host'))
        return False

    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    esxi_hosts = obj_view.view
    obj_view.Destroy()

    _LOGGER.info("Found %s host(s) in environment", len(esxi_hosts))

    # Determine target host(s)
    target_hosts = []

    if len(esxi_hosts) == 1:
        # Single ESXi host scenario
        if target_host_name:
            # Verify the target host name matches
            host = esxi_hosts[0]
            if (host.summary.config.name.lower() == target_host_name.lower() or
                host.name.lower() == target_host_name.lower()):
                target_hosts = [host]
            else:
                _LOGGER.error(
                    "Target host '%s' does not match available host '%s'",
                    target_host_name, host.summary.config.name
                )
                esx_disconnect(conn)
                return False
        else:
            # No target specified, use the single available host
            target_hosts = esxi_hosts
            _LOGGER.info("No target host specified, using single available host")

    elif len(esxi_hosts) > 1:
        # vCenter with multiple hosts scenario
        if not target_host_name:
            # List available hosts for user reference
            available_hosts = [host.summary.config.name for host in esxi_hosts]
            _LOGGER.error(
                "Multiple hosts found in vCenter. You must specify target_host. "
                "Available hosts: %s", ", ".join(available_hosts)
            )
            esx_disconnect(conn)
            return False
        else:
            # Find the specified target host
            for host in esxi_hosts:
                if (host.summary.config.name.lower() == target_host_name.lower() or
                    host.name.lower() == target_host_name.lower()):
                    target_hosts = [host]
                    break

            if not target_hosts:
                available_hosts = [host.summary.config.name for host in esxi_hosts]
                _LOGGER.error(
                    "Target host '%s' not found. Available hosts: %s",
                    target_host_name, ", ".join(available_hosts)
                )
                esx_disconnect(conn)
                return False

    else:
        # No hosts found
        _LOGGER.error("No ESXi hosts found")
        esx_disconnect(conn)
        return False

    # Execute power command on target host(s)
    try:
        for esxi_host in target_hosts:
            host_name = esxi_host.summary.config.name

            # Check if host is in maintenance mode
            if not force and esxi_host.runtime.inMaintenanceMode is False:
                _LOGGER.warning(
                    "Host '%s' is not in maintenance mode. Consider setting force=true "
                    "or putting the host in maintenance mode first", host_name
                )

            # Check for running VMs if not forced
            if not force:
                vm_count = len([vm for vm in esxi_host.vm if vm.runtime.powerState == "poweredOn"])
                if vm_count > 0:
                    _LOGGER.warning(
                        "Host '%s' has %d powered-on VMs. Consider migrating VMs or setting force=true",
                        host_name, vm_count
                    )

            # Execute the power command
            task = None
            if target_cmnd == "shutdown":
                _LOGGER.info(
                    "Sending shutdown command to host '%s' (forced: %s)",
                    host_name, force
                )
                task = esxi_host.ShutdownHost_Task(force)
            elif target_cmnd == "reboot":
                _LOGGER.info(
                    "Sending reboot command to host '%s' (forced: %s)",
                    host_name, force
                )
                task = esxi_host.RebootHost_Task(force)
            else:
                _LOGGER.error("Unsupported host power command: %s", target_cmnd)
                continue

            # Monitor task status
            if task:
                message = f"Host {target_cmnd} command sent to {host_name} (forced: {force})"
                task_status(hass, task, message, notify)
            else:
                _LOGGER.info("'%s' command does not provide task feedback", target_cmnd)

    except vmodl.MethodFault as error:
        _LOGGER.error("VMware method fault: %s", error.msg)
        return False
    except vmodl.HostConfigFault as error:
        _LOGGER.error("Host configuration fault: %s", str(error))
        return False
    except vmodl.RuntimeFault as error:
        _LOGGER.error("VMware runtime fault: %s", error.msg)
        return False
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Unexpected error during host power operation: %s", str(error))
        return False
    finally:
        esx_disconnect(conn)
        operation_time = time.time() - start_time
        _LOGGER.info("Host power operation '%s' completed in %.2f seconds", target_cmnd, operation_time)

    return True


def host_pwr_policy(target_host_name, host_cmnd, conn_details):
    """Host power policy command - supports both ESXi and vCenter."""
    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get('host', 'host'))
        return False

    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    esxi_hosts = obj_view.view
    obj_view.Destroy()

    _LOGGER.info("Found %s host(s) in environment", len(esxi_hosts))

    # Determine target host(s)
    target_hosts = []

    if len(esxi_hosts) == 1:
        # Single ESXi host scenario
        if target_host_name:
            # Verify the target host name matches
            host = esxi_hosts[0]
            if (host.summary.config.name.lower() == target_host_name.lower() or
                host.name.lower() == target_host_name.lower()):
                target_hosts = [host]
            else:
                _LOGGER.error(
                    "Target host '%s' does not match available host '%s'",
                    target_host_name, host.summary.config.name
                )
                esx_disconnect(conn)
                return False
        else:
            # No target specified, use the single available host
            target_hosts = esxi_hosts
            _LOGGER.info("No target host specified, using single available host")

    elif len(esxi_hosts) > 1:
        # vCenter with multiple hosts scenario
        if not target_host_name:
            # List available hosts for user reference
            available_hosts = [host.summary.config.name for host in esxi_hosts]
            _LOGGER.error(
                "Multiple hosts found in vCenter. You must specify target_host. "
                "Available hosts: %s", ", ".join(available_hosts)
            )
            esx_disconnect(conn)
            return False
        else:
            # Find the specified target host
            for host in esxi_hosts:
                if (host.summary.config.name.lower() == target_host_name.lower() or
                    host.name.lower() == target_host_name.lower()):
                    target_hosts = [host]
                    break

            if not target_hosts:
                available_hosts = [host.summary.config.name for host in esxi_hosts]
                _LOGGER.error(
                    "Target host '%s' not found. Available hosts: %s",
                    target_host_name, ", ".join(available_hosts)
                )
                esx_disconnect(conn)
                return False

    else:
        # No hosts found
        _LOGGER.error("No ESXi hosts found")
        esx_disconnect(conn)
        return False

    # Apply power policy to target host(s)
    try:
        for vm_host in target_hosts:
            host_name = vm_host.summary.config.name
            _LOGGER.info(
                "Sending power policy '%s' command to host '%s'", host_cmnd, host_name
            )

            policy_key = ""
            available_policy = []

            # Check if host supports power system capability
            if not hasattr(vm_host.config, 'powerSystemCapability') or not vm_host.config.powerSystemCapability:
                _LOGGER.warning("Host '%s' does not support power policy configuration", host_name)
                continue

            for policy in vm_host.config.powerSystemCapability.availablePolicy:
                available_policy.append(policy.shortName)
                if policy.shortName == host_cmnd:
                    policy_key = policy.key

            if host_cmnd in available_policy:
                vm_host.configManager.powerSystem.ConfigurePowerPolicy(policy_key)
                _LOGGER.info("Power policy '%s' applied to host '%s'", host_cmnd, host_name)
            else:
                _LOGGER.warning(
                    "Power policy '%s' not available on host '%s'. Available policies: %s",
                    host_cmnd, host_name, available_policy
                )

    except vmodl.MethodFault as error:
        _LOGGER.error("VMware method fault: %s", error.msg)
        return False
    except vmodl.HostConfigFault as error:
        _LOGGER.error("Host configuration fault: %s", str(error))
        return False
    except vmodl.RuntimeFault as error:
        _LOGGER.error("VMware runtime fault: %s", error.msg)
        return False
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Unexpected error during power policy configuration: %s", str(error))
        return False
    finally:
        esx_disconnect(conn)

    return True


def vm_pwr(
    hass, target_host, target_vm, target_vm_uuid, target_cmnd, conn_details, notify
):
    """VM power commands."""
    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get('host', 'host'))
        return False

    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    data = obj_view.view
    obj_view.Destroy()

    try:
        for vm in [vm for vm in data if vm.summary.config.uuid in target_vm_uuid]:
            _LOGGER.info("Sending '%s' command to vm '%s'", target_cmnd, vm.name)

            if vm.name == target_vm:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) matches name on target",
                    target_vm,
                    target_vm_uuid,
                )
            else:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) does notmatch name on target",
                    target_vm,
                    target_vm_uuid,
                )

            # 幂等 / 前置条件检查：目标已达成（或前置不满足）时不下发命令。
            #   ESXi 对「已经开机的虚拟机再执行开机」会直接报错
            #   （The attempted operation cannot be performed in the current state），
            #   而重复下发在实际使用中很常见：UI 因 45 秒数据节流显示滞后，用户以为
            #   没生效会再点一次；批量开机也会把已经开着的机器一起带上。
            #   在同一个连接里先看一眼真实电源状态即可，零额外开销。
            power_state = str(vm.runtime.powerState)
            skip_reason = None
            if target_cmnd == "on" and power_state == "poweredOn":
                skip_reason = "已经在运行"
            elif target_cmnd == "off" and power_state == "poweredOff":
                skip_reason = "已经关机"
            elif target_cmnd == "suspend" and power_state == "suspended":
                skip_reason = "已经挂起"
            elif (
                target_cmnd in ("reset", "reboot", "shutdown")
                and power_state != "poweredOn"
            ):
                skip_reason = f"当前状态为 {power_state}，无法执行"

            if skip_reason:
                _LOGGER.info(
                    "虚拟机 %s 跳过 '%s' 命令：%s", vm.name, target_cmnd, skip_reason
                )
                break

            # generate task based on requested command
            if target_cmnd == "on":
                task = vm.PowerOnVM_Task()
            elif target_cmnd == "off":
                task = vm.PowerOffVM_Task()
            elif target_cmnd == "suspend":
                task = vm.SuspendVM_Task()
            elif target_cmnd == "reset":
                task = vm.ResetVM_Task()
            elif target_cmnd == "reboot":
                task = vm.RebootGuest()
            elif target_cmnd == "shutdown":
                task = vm.ShutdownGuest()

            # while task is running, check status
            # some tasks are fire and forget, no status will be provided
            if task:
                message = "power " + target_cmnd + " on " + vm.name
                task_status(hass, task, message, notify)
            else:
                _LOGGER.info("'%s' task does not provide feedback", target_cmnd)

            break
        else:
            _LOGGER.info(
                "VM %s on host %s not found. Make sure the name is correct",
                target_vm,
                target_host,
            )
    except vmodl.MethodFault as error:
        _LOGGER.info(error.msg)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.info(str(error))
    finally:
        esx_disconnect(conn)

    return True


def _find_vm(content, vm_uuid):
    """按 config.uuid 找虚拟机；找不到返回 None。"""
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in obj_view.view:
            try:
                if vm.summary.config.uuid in (vm_uuid or []):
                    return vm
            except Exception:  # pylint: disable=broad-except
                continue
    finally:
        obj_view.Destroy()
    return None


def _find_vm_by_moid(content, vm_moid):
    """按 moId（如 "1002"）找虚拟机托管对象；找不到返回 None。"""
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm_obj in obj_view.view:
            if vm_obj._moId == str(vm_moid):
                return vm_obj
    finally:
        obj_view.Destroy()
    return None


def _find_host(content, host_name):
    """按主机名（原始名/小写/下划线形式均可）找主机。"""
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    try:
        for host in obj_view.view:
            candidates = {
                host.summary.config.name,
                host.summary.config.name.lower(),
                host.summary.config.name.replace(" ", "_").lower(),
            }
            if host_name in candidates:
                return host
        # 单主机环境直接兜底（名字偶尔因为大小写/空格对不上）
        hosts = list(obj_view.view)
        return hosts[0] if len(hosts) == 1 else None
    finally:
        obj_view.Destroy()


def vm_nic_set(conn_details, vm_uuid, mac, connected):
    """连接 / 断开虚拟机的某块网卡（热操作，开关机均可）。

    成功返回 True；ESXi 侧报错时抛出 RuntimeError，由调用方（实体/服务）接住并提示。
    """
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        vm = _find_vm(conn.RetrieveContent(), vm_uuid)
        if vm is None:
            raise RuntimeError("未找到目标虚拟机")
        nic = None
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                if (device.macAddress or "").lower() == str(mac).lower():
                    nic = device
                    break
        if nic is None:
            raise RuntimeError(f"未找到 MAC 为 {mac} 的网卡")

        nic.connectable.connected = bool(connected)
        nic.connectable.startConnected = bool(connected)
        spec = vim.vm.ConfigSpec()
        device_spec = vim.vm.device.VirtualDeviceSpec()
        device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
        device_spec.device = nic
        spec.deviceChange = [device_spec]
        task = vm.ReconfigVM_Task(spec)
        task_info = _wait_task(task)
        if task_info not in ("success", None):
            raise RuntimeError(f"网卡操作失败：{task_info}")
        return True
    finally:
        esx_disconnect(conn)


def _wait_task(task):
    """等任务结束，返回 success/error（无 task 对象时返回 None）。"""
    if task is None:
        return None
    while task.info.state not in ("success", "error"):
        continue
    if task.info.state == "error":
        return getattr(task.info.error, "msg", "unknown error")
    return "success"


def vm_consolidate(conn_details, vm_uuid):
    """合并快照遗留磁盘（ConsolidateVMDisks_Task）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        vm = _find_vm(conn.RetrieveContent(), vm_uuid)
        if vm is None:
            raise RuntimeError("未找到目标虚拟机")
        result = _wait_task(vm.ConsolidateVMDisks_Task())
        if result not in ("success", None):
            raise RuntimeError(f"磁盘合并失败：{result}")
        return True
    finally:
        esx_disconnect(conn)


def host_autostart_set_enabled(conn_details, host_name, enabled):
    """打开 / 关闭主机的「随主机启动虚拟机」总开关（保留既有启停顺序与延时）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        content = conn.RetrieveContent()
        host = _find_host(content, host_name)
        if host is None:
            raise RuntimeError("未找到目标主机")
        asm = host.configManager.autoStartManager
        old = asm.config
        defaults = vim.host.AutoStartManager.SystemDefaults(
            enabled=bool(enabled),
            startDelay=getattr(old.defaults, "startDelay", 120),
            stopAction=getattr(old.defaults, "stopAction", "PowerOff"),
            stopDelay=getattr(old.defaults, "stopDelay", 120),
            waitForHeartbeat=bool(getattr(old.defaults, "waitForHeartbeat", False)),
        )
        spec = vim.host.AutoStartManager.Config(
            defaults=defaults, powerInfo=list(old.powerInfo or [])
        )
        asm.Reconfigure(spec)
        return True
    finally:
        esx_disconnect(conn)


def vm_autostart_set(conn_details, host_name, vm_moid, enabled):
    """把某台虚拟机加入 / 移出主机自启动序列。

    ESXi 的 AutoStartManager 有两个坑（实测 6.5）：

    * ``AutoPowerInfo.key`` 必须是 **VirtualMachine 托管对象引用**，传 moId
      字符串会在客户端序列化时直接 ``TypeError: expected type
      vim.VirtualMachine, but got str`` —— 开关看着点了，实际从未下发；
    * 加入列表后还要主机总开关 ``defaults.enabled=True`` 才会真的随主机启动，
      所以打开某台 VM 的自启时若总开关关着，一并打开（与 vSphere Client
      勾选自启的实际效果一致）。
    """
    conn = esx_connect(**conn_details)
    if not conn:
        raise RuntimeError(f"无法连接到 ESXi 主机 {conn_details.get('host')}")
    try:
        content = conn.RetrieveContent()
        host = _find_host(content, host_name)
        if host is None:
            raise RuntimeError("未找到目标主机")
        vm_obj = _find_vm_by_moid(content, vm_moid)
        if vm_obj is None:
            raise RuntimeError(f"未找到 moId 为 {vm_moid} 的虚拟机")

        asm = host.configManager.autoStartManager
        old = asm.config
        defaults = old.defaults

        # 旧条目里 key 是 MOR，按 _moId 找到本机条目（动作/顺序/心跳沿用旧值），
        # 其余 VM 的条目原样保留
        new_entries = []
        old_info = None
        for info in old.powerInfo or []:
            if getattr(info.key, "_moId", str(info.key)) == str(vm_moid):
                old_info = info
                continue
            new_entries.append(info)

        # waitForHeartbeat 在 AutoPowerInfo 上是 WaitHeartbeatSetting 枚举
        # （yes/no/systemDefault），**不是布尔**且必填；系统默认 defaults 上的
        # 才是布尔，不能直接搬。沿用旧值，没有则 systemDefault（跟随系统默认）。
        wait_hb = (
            old_info.waitForHeartbeat
            if old_info is not None and getattr(old_info, "waitForHeartbeat", None)
            else vim.host.AutoStartManager.AutoPowerInfo.WaitHeartbeatSetting.systemDefault
        )
        start_delay = getattr(old_info, "startDelay", None)
        if start_delay is None:
            start_delay = getattr(defaults, "startDelay", 120)
        stop_delay = getattr(old_info, "stopDelay", None)
        if stop_delay is None:
            stop_delay = getattr(defaults, "stopDelay", 120)

        if enabled:
            max_order = (
                max((getattr(info, "startOrder", 0) or 0) for info in new_entries)
                if new_entries
                else 0
            )
            order = (
                old_info.startOrder
                if old_info is not None and getattr(old_info, "startOrder", None) is not None
                else max_order + 1
            )
            entry = vim.host.AutoStartManager.AutoPowerInfo(
                key=vm_obj,  # 必须传托管对象，不能传 moId 字符串
                startOrder=order,
                startAction="PowerOn",
                startDelay=start_delay,
                stopAction=getattr(defaults, "stopAction", None) or "PowerOff",
                stopDelay=stop_delay,
                waitForHeartbeat=wait_hb,
            )
        else:
            # ESXi 6.5 hostd 实测：Reconfigure 的 powerInfo 是**合并语义**，
            # 不传某 VM / 传空列表都不会删掉它的条目。真正的"移出自动启动"与
            # vSphere Client 里把启动/停止动作选成「无」一致：条目仍在，但
            # startAction/stopAction 留空（省略可选字段时 hostd 即清空）。
            order = (
                old_info.startOrder
                if old_info is not None and getattr(old_info, "startOrder", None) is not None
                else 1
            )
            entry = vim.host.AutoStartManager.AutoPowerInfo(
                key=vm_obj,
                startOrder=order,
                startDelay=start_delay,
                stopDelay=stop_delay,
                waitForHeartbeat=wait_hb,
            )
        new_entries.append(entry)

        # 加入自启时保证主机总开关打开；移出时保持现状（不替用户关掉全局开关）
        master_enabled = bool(getattr(defaults, "enabled", False))
        if enabled and not master_enabled:
            master_enabled = True
            _LOGGER.info("主机 %s 自启动总开关原已关闭，加入 VM 自启时一并打开", host_name)

        new_defaults = vim.host.AutoStartManager.SystemDefaults(
            enabled=master_enabled,
            startDelay=getattr(defaults, "startDelay", 120),
            stopAction=getattr(defaults, "stopAction", "PowerOff"),
            stopDelay=getattr(defaults, "stopDelay", 120),
            waitForHeartbeat=bool(getattr(defaults, "waitForHeartbeat", False)),
        )
        asm.Reconfigure(
            vim.host.AutoStartManager.Config(
                defaults=new_defaults, powerInfo=new_entries
            )
        )
        return True
    finally:
        esx_disconnect(conn)


def vm_snap_take(
    hass,
    target_host,
    target_vm,
    target_vm_uuid,
    snap_name,
    desc,
    memory,
    quiesce,
    conn_details,
    notify,
):
    """Take Snapshot commands."""
    conn = esx_connect(**conn_details)
    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    data = obj_view.view
    obj_view.Destroy()

    try:
        for vm in [vm for vm in data if vm.summary.config.uuid in target_vm_uuid]:
            _LOGGER.info("Sending create snapshot command to vm '%s'", vm.name)

            if vm.name == target_vm:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) matches name on target",
                    target_vm,
                    target_vm_uuid,
                )
            else:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) does notmatch name on target",
                    target_vm,
                    target_vm_uuid,
                )
            task = vm.CreateSnapshot_Task(snap_name, desc, memory, quiesce)

            # while task is running, check status
            if task:
                message = "create snapshot on " + vm.name
                task_status(hass, task, message, notify)
            else:
                _LOGGER.info("Task does not provide feedback")

            break
        else:
            _LOGGER.info(
                "VM %s (UUID %s) on host %s not found. Make sure the name is correct",
                target_vm,
                target_vm_uuid,
                target_host,
            )
    except vmodl.MethodFault as error:
        _LOGGER.error("VMware method fault during snapshot creation: %s", error.msg)
        return False
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Unexpected error during snapshot creation: %s", str(error))
        return False
    finally:
        esx_disconnect(conn)

    return True


def vm_snap_remove(
    hass, target_host, target_vm, target_vm_uuid, target_cmnd, conn_details, notify
):
    """Remove Snapshot commands."""
    conn = esx_connect(**conn_details)
    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    data = obj_view.view
    obj_view.Destroy()

    try:
        for vm in [vm for vm in data if vm.summary.config.uuid in target_vm_uuid]:
            if vm.name == target_vm:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) matches name on target",
                    target_vm,
                    target_vm_uuid,
                )
            else:
                _LOGGER.debug(
                    "Provided name %s (UUID %s) does notmatch name on target",
                    target_vm,
                    target_vm_uuid,
                )

            # if there are 0 snapshots, stop
            if vm.snapshot is None:
                _LOGGER.info("No snapshots to remove on %s", vm.name)
                break

            _LOGGER.info(
                "Sending remove '%s' snapshot command to vm '%s'", target_cmnd, vm.name
            )

            # get a list of all snapshots
            snapshots = list_snapshots(vm.snapshot.rootSnapshotList, True)

            # remove all snapshots
            if target_cmnd == "all":
                task = vm.RemoveAllSnapshots_Task()
            # remove first snapshot in a snapshot tree
            elif target_cmnd == "first":
                first_snap = snapshots[0].snapshot
                task = first_snap.RemoveSnapshot_Task(False)
            # remove last snapshot in a snapshot tree
            elif target_cmnd == "last":
                last_snap = snapshots[(len(snapshots) - 1)].snapshot
                task = last_snap.RemoveSnapshot_Task(False)

            # while task is running, check status
            if task:
                message = "remove " + target_cmnd + " snapshot(s) on " + vm.name
                task_status(hass, task, message, notify)
            else:
                _LOGGER.info("Task does not provide feedback")

            break
        else:
            _LOGGER.info(
                "VM %s on host %s not found. Make sure the name is correct",
                target_vm,
                target_host,
            )
    except vmodl.MethodFault as error:
        _LOGGER.info(error.msg)
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.info(str(error))
    finally:
        esx_disconnect(conn)

    return True


def task_status(hass, task, command, notify):
    """Check status of running task."""
    from time import sleep
    from homeassistant.components import persistent_notification

    # Wait while task is in progress
    state = vim.TaskInfo.State
    max_wait_time = 300  # 5 minutes timeout
    elapsed_time = 0

    while task.info.state not in [state.success, state.error] and elapsed_time < max_wait_time:
        if task.info.progress is not None:
            _LOGGER.debug(
                "Task %s progress %s%%", task.info.eventChainId, task.info.progress
            )

        sleep(2)
        elapsed_time += 2

    # Check for timeout
    if elapsed_time >= max_wait_time:
        _LOGGER.error("Task '%s' timed out after %d seconds", command, max_wait_time)
        message = f"Timeout - {command}\n\nTask did not complete within {max_wait_time} seconds"
        # 只在显式要求通知时才弹（实体调用一律 notify=False，失败有日志就够了；
        #   否则用户点一次开关失败就会在通知中心多一条持久通知）
        if notify:
            persistent_notification.create(hass, message, "ESXi Control")
        return False

    # Output task status once complete
    if task.info.state == "success":
        _LOGGER.info("Task '%s' on '%s' completed successfully", command, task.info.entityName)
        message = "Complete - " + command
        if notify:
            persistent_notification.create(hass, message, "ESXi Control")
        else:
            _LOGGER.debug("Not creating notification: notification flag is false")
        return True
    elif task.info.state == "error":
        _LOGGER.error("Task '%s' on '%s' failed: %s", command, task.info.entityName, task.info.error.msg)
        message = "Failed - " + command + "\n\n"
        message += task.info.error.msg
        # 同上：失败也只在显式要求通知时才弹
        if notify:
            persistent_notification.create(hass, message, "ESXi Control")
        return False

    return False


def list_esxi_hosts(hass, conn_details):
    """List all ESXi hosts available in the environment (useful for vCenter)."""
    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get('host', 'host'))
        return

    try:
        content = conn.RetrieveContent()
        obj_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        esxi_hosts = obj_view.view
        obj_view.Destroy()

        if not esxi_hosts:
            _LOGGER.info("No ESXi hosts found")
            return

        _LOGGER.info("Found %d ESXi host(s):", len(esxi_hosts))

        host_info = []
        for host in esxi_hosts:
            host_name = host.summary.config.name
            connection_state = host.runtime.connectionState
            power_state = host.runtime.powerState
            maintenance_mode = host.runtime.inMaintenanceMode
            vm_count = len([vm for vm in host.vm if vm.runtime.powerState == "poweredOn"])

            info = (
                f"  - Name: {host_name}, "
                f"Connection: {connection_state}, "
                f"Power: {power_state}, "
                f"Maintenance: {maintenance_mode}, "
                f"Running VMs: {vm_count}"
            )
            host_info.append(info)
            _LOGGER.info(info)

        # Create a persistent notification for easy viewing
        try:
            from homeassistant.components import persistent_notification
            notification_message = (
                f"ESXi Hosts in {conn_details['host']}:\n\n" +
                "\n".join([info.replace("  - ", "") for info in host_info]) +
                "\n\nUse the 'Name' field as the target_host parameter for host power commands."
            )
            persistent_notification.create(
                hass,
                notification_message,
                title="ESXi Hosts List",
                notification_id="esxi_control_hosts_list"
            )
        except Exception as e:
            _LOGGER.debug("Could not create persistent notification: %s", e)

    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Failed to list ESXi hosts: %s", error)
    finally:
        esx_disconnect(conn)


def list_power_policies(hass, target_host_name, conn_details):
    """List available power policies for a specific host."""
    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get('host', 'host'))
        return

    try:
        content = conn.RetrieveContent()
        obj_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        esxi_hosts = obj_view.view
        obj_view.Destroy()

        # Find the target host
        target_host = None
        if len(esxi_hosts) == 1:
            # Single ESXi host scenario
            if target_host_name:
                # Verify the target host name matches
                host = esxi_hosts[0]
                if (host.summary.config.name.lower() == target_host_name.lower() or
                    host.name.lower() == target_host_name.lower()):
                    target_host = host
                else:
                    _LOGGER.error(
                        "Target host '%s' does not match available host '%s'",
                        target_host_name, host.summary.config.name
                    )
                    return
            else:
                # No target specified, use the single available host
                target_host = esxi_hosts[0]
                _LOGGER.info("No target host specified, using single available host")

        elif len(esxi_hosts) > 1:
            # vCenter with multiple hosts scenario
            if not target_host_name:
                # List available hosts for user reference
                available_hosts = [host.summary.config.name for host in esxi_hosts]
                _LOGGER.error(
                    "Multiple hosts found in vCenter. You must specify target_host. "
                    "Available hosts: %s", ", ".join(available_hosts)
                )
                return
            else:
                # Find the specified target host
                for host in esxi_hosts:
                    if (host.summary.config.name.lower() == target_host_name.lower() or
                        host.name.lower() == target_host_name.lower()):
                        target_host = host
                        break

                if not target_host:
                    available_hosts = [host.summary.config.name for host in esxi_hosts]
                    _LOGGER.error(
                        "Target host '%s' not found. Available hosts: %s",
                        target_host_name, ", ".join(available_hosts)
                    )
                    return
        else:
            # No hosts found
            _LOGGER.error("No ESXi hosts found")
            return

        # Get power policies for the target host
        host_name = target_host.summary.config.name

        # Check if host supports power system capability
        if not hasattr(target_host.config, 'powerSystemCapability') or not target_host.config.powerSystemCapability:
            _LOGGER.warning("Host '%s' does not support power policy configuration", host_name)
            try:
                from homeassistant.components import persistent_notification
                notification_message = f"Host '{host_name}' does not support power policy configuration"
                persistent_notification.create(
                    hass,
                    notification_message,
                    title="Power Policies Not Supported",
                    notification_id="esxi_control_power_policies_not_supported"
                )
            except Exception as e:
                _LOGGER.debug("Could not create persistent notification: %s", e)
            return

        # Get available policies and current policy
        available_policies = []
        current_policy = None

        try:
            current_policy = target_host.config.powerSystemInfo.currentPolicy.shortName
        except Exception as e:
            _LOGGER.debug("Could not get current power policy: %s", e)
            current_policy = "Unknown"

        for policy in target_host.config.powerSystemCapability.availablePolicy:
            policy_info = {
                'short_name': policy.shortName,
                'full_name': policy.fullName,
                'description': getattr(policy, 'description', 'No description available'),
                'is_current': policy.shortName == current_policy
            }
            available_policies.append(policy_info)

        _LOGGER.info("Found %d power policies for host '%s':", len(available_policies), host_name)

        policy_details = []
        for policy in available_policies:
            current_marker = " (CURRENT)" if policy['is_current'] else ""
            detail = (
                f"  - {policy['short_name']}{current_marker}\n"
                f"    Full Name: {policy['full_name']}\n"
                f"    Description: {policy['description']}"
            )
            policy_details.append(detail)
            _LOGGER.info(detail.replace("    ", ""))

        # Create a persistent notification for easy viewing
        try:
            from homeassistant.components import persistent_notification
            notification_message = (
                f"Power Policies for {host_name}:\n\n" +
                "\n\n".join(policy_details) +
                f"\n\nCurrent Policy: {current_policy}\n\n" +
                "Use the 'short_name' values (like 'static', 'dynamic', 'low') as the 'command' parameter in the host_power_policy service."
            )
            persistent_notification.create(
                hass,
                notification_message,
                title="ESXi Power Policies",
                notification_id="esxi_control_power_policies"
            )
        except Exception as e:
            _LOGGER.debug("Could not create persistent notification: %s", e)

    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("Failed to list power policies: %s", error)
    finally:
        esx_disconnect(conn)


# ===================== 增强功能：性能/维护模式/锁定/服务/NTP/控制台 =====================

def _find_hosts(content, target_host_name=None):
    """返回环境中的 HostSystem 列表（单机直连 / vCenter 通用）。

    传 ``target_host_name`` 时按原始名过滤，找不到返回空列表。
    """
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    hosts = obj_view.view
    obj_view.Destroy()
    if not target_host_name:
        return list(hosts)
    target = target_host_name.lower()
    return [
        host
        for host in hosts
        if host.summary.config.name.lower() == target
        or (host.name or "").lower() == target
    ]


def _wait_task(task):
    """统一的任务等待（pyvmomi 自带，失败抛异常给调用方记日志）。"""
    from pyVim.task import WaitForTask

    WaitForTask(task)


def get_vms_perf(content, vm_list):
    """**一次** QueryPerf 拉回全部虚拟机的三个核心实时指标。

    返回 ``{moId: {cpu_ready_pct, mem_balloon_mb, net_dropped_ps}}``：
      · cpu.ready(summation, ms/20s)      → CPU 就绪时间占比 %（跨 vCPU 汇总）
      · mem.vmmemctl(average, KB)         → 内存气球 MB
      · net.droppedRx+Tx(summation, 包数)  → 每秒丢包数

    刻意只取这 3 个排障最常用、且聚合实例（instance=""）在 5.5~8.0 都稳定存在的
    计数器；逐网卡 / 逐磁盘的指标会让实体数与 hostd 压力爆炸，不做。
    任何失败都返回空字典，由调用方把值留成 None（传感器显示未知）。
    """
    result = {}
    try:
        perf = content.perfManager
        counter_map = {}
        for counter in perf.perfCounter:
            counter_map[
                (counter.groupInfo.key, counter.nameInfo.key, counter.rollupType)
            ] = counter.key

        ready_id = counter_map.get(("cpu", "ready", "summation"))
        balloon_id = counter_map.get(("mem", "vmmemctl", "average"))
        drop_rx_id = counter_map.get(("net", "droppedRx", "summation"))
        drop_tx_id = counter_map.get(("net", "droppedTx", "summation"))
        metric_ids = [
            vim.PerformanceManager.MetricId(counterId=cid, instance="")
            for cid in (ready_id, balloon_id, drop_rx_id, drop_tx_id)
            if cid
        ]
        if not metric_ids:
            return result

        powered_on = [
            vm for vm in vm_list if str(getattr(vm.runtime, "powerState", "")) == "poweredOn"
        ]
        if not powered_on:
            return result

        # 同一台主机（或同一 vCenter 集群）realtime 刷新周期一致（通常 20s），
        #   取第一台的即可，避免对 129 台 VM 各调一次 QueryPerfProviderSummary。
        interval = perf.QueryPerfProviderSummary(entity=powered_on[0]).refreshRate
        if not interval:
            return result

        specs = [
            vim.PerformanceManager.QuerySpec(
                entity=vm, metricId=metric_ids, intervalId=interval, maxSample=1
            )
            for vm in powered_on
        ]
        # 一次调用拿回全部（批量 QuerySpec 是 perfManager 的标准用法）
        samples = perf.QueryPerf(querySpec=specs)

        for sample in samples or []:
            moid = getattr(sample.entity, "_moId", None)
            values = {"cpu_ready_pct": None, "mem_balloon_mb": None, "net_dropped_ps": None}
            for series in sample.value or []:
                if not getattr(series, "value", None):
                    continue
                value = series.value[-1]
                cid = series.id.counterId
                if cid == ready_id:
                    values["cpu_ready_pct"] = round(value / (interval * 1000.0) * 100, 1)
                elif cid == balloon_id:
                    values["mem_balloon_mb"] = round(value / 1024.0, 1)
                elif cid in (drop_rx_id, drop_tx_id):
                    prev = values["net_dropped_ps"] or 0
                    values["net_dropped_ps"] = round(prev + value / float(interval), 1)
            result[moid] = values
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.debug("虚拟机性能查询失败（本轮 perf 值留空）: %s", error)
    return result


def host_maintenance(conn_details, target_host_name, enable):
    """进入 / 退出维护模式。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("维护模式操作：找不到主机 %s", target_host_name)
            return False
        for host in hosts:
            if enable:
                _LOGGER.info("主机 %s 进入维护模式", target_host_name)
                # timeout=0：不无限等待 DRS 迁移（单机/无 vMotion 时立刻报错而不是挂死）；
                # evacuatePoweredOff=False：不强制迁走已关机 VM
                _wait_task(host.EnterMaintenanceMode_Task(0, False))
            else:
                _LOGGER.info("主机 %s 退出维护模式", target_host_name)
                _wait_task(host.ExitMaintenanceMode_Task())
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("维护模式操作失败（%s）: %s", target_host_name, error)
        return False
    finally:
        esx_disconnect(conn)


def host_lockdown(conn_details, target_host_name, enable):
    """进入 / 退出锁定模式（5.5 是非 Task 版，6.x+ 优先用 Task 版）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("锁定模式操作：找不到主机 %s", target_host_name)
            return False
        for host in hosts:
            if enable:
                _LOGGER.info("主机 %s 进入锁定模式", target_host_name)
                if hasattr(host, "EnterLockdownMode_Task"):
                    _wait_task(host.EnterLockdownMode_Task())
                else:
                    host.EnterLockdownMode()
            else:
                _LOGGER.info("主机 %s 退出锁定模式", target_host_name)
                if hasattr(host, "ExitLockdownMode_Task"):
                    _wait_task(host.ExitLockdownMode_Task())
                else:
                    host.ExitLockdownMode()
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("锁定模式操作失败（%s）: %s", target_host_name, error)
        return False
    finally:
        esx_disconnect(conn)


def host_service(conn_details, target_host_name, service_key, action):
    """启动 / 停止 / 重启主机服务（action: start / stop / restart）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("服务操作：找不到主机 %s", target_host_name)
            return False
        methods = {
            "start": "StartService",
            "stop": "StopService",
            "restart": "RestartService",
        }
        method_name = methods.get(action)
        if not method_name:
            _LOGGER.error("不支持的服务操作: %s", action)
            return False
        for host in hosts:
            service_system = host.configManager.serviceSystem
            _LOGGER.info("主机 %s：%s 服务 %s", target_host_name, action, service_key)
            getattr(service_system, method_name)(service_key)
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error(
            "服务 %s %s 操作失败（%s）: %s", service_key, action, target_host_name, error
        )
        return False
    finally:
        esx_disconnect(conn)


def host_service_policy(conn_details, target_host_name, service_key, policy):
    """修改服务启动策略（on / off；部分服务支持 automatic）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("服务策略操作：找不到主机 %s", target_host_name)
            return False
        for host in hosts:
            _LOGGER.info(
                "主机 %s：服务 %s 启动策略设为 %s",
                target_host_name,
                service_key,
                policy,
            )
            host.configManager.serviceSystem.UpdatePolicy(service_key, policy)
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("修改服务 %s 启动策略失败（%s）: %s", service_key, target_host_name, error)
        return False
    finally:
        esx_disconnect(conn)


def host_refresh_services(conn_details, target_host_name):
    """刷新主机服务清单（serviceSystem.RefreshServices）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("刷新服务：找不到主机 %s", target_host_name)
            return False
        for host in hosts:
            host.configManager.serviceSystem.RefreshServices()
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("刷新服务清单失败（%s）: %s", target_host_name, error)
        return False
    finally:
        esx_disconnect(conn)


def set_ntp_servers(conn_details, target_host_name, servers):
    """写入 NTP 服务器列表（保留 configFile；写完尝试让 ntpd 重新加载）。"""
    conn = esx_connect(**conn_details)
    if not conn:
        return False
    try:
        hosts = _find_hosts(conn.RetrieveContent(), target_host_name)
        if not hosts:
            _LOGGER.error("NTP 配置：找不到主机 %s", target_host_name)
            return False
        for host in hosts:
            date_time = host.configManager.dateTimeSystem
            old_config = host.config.dateTimeInfo.ntpConfig
            new_config = vim.host.NtpConfig(
                server=list(servers),
                configFile=list(getattr(old_config, "configFile", []) or []),
            )
            date_time.UpdateConfig(new_config)
            _LOGGER.info("主机 %s：NTP 服务器已更新为 %s", target_host_name, servers)
            # 立即触发一次时间同步（不支持 / 未运行 ntpd 时忽略错误）
            try:
                date_time.UpdateDateTime()
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.debug("UpdateDateTime 不可用（忽略）: %s", error)
            try:
                host.configManager.serviceSystem.RestartService("ntpd")
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.debug("重启 ntpd 失败（忽略，可能未运行）: %s", error)
        return True
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("写入 NTP 配置失败（%s）: %s", target_host_name, error)
        return False
    finally:
        esx_disconnect(conn)


def vm_guest_action(
    hass, target_host, target_vm, target_vm_uuid, action, conn_details, notify
):
    """虚拟机的杂项一次性动作（都是实体按钮触发）。

    支持：
      · standby         —— 客户机待机（StandbyGuest，需 VMware Tools）
      · snapshot_revert —— 还原到当前快照（RevertToCurrentSnapshot_Task）
      · tools_upgrade   —— 升级 VMware Tools（UpgradeTools_Task）
    """
    conn = esx_connect(**conn_details)
    if not conn:
        _LOGGER.error("Failed to connect to %s", conn_details.get("host", "host"))
        return False

    content = conn.RetrieveContent()
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    data = obj_view.view
    obj_view.Destroy()

    try:
        for vm in [vm for vm in data if vm.summary.config.uuid in target_vm_uuid]:
            _LOGGER.info("VM %s: action '%s'", vm.name, action)
            task = None
            if action == "standby":
                vm.StandbyGuest()
            elif action == "snapshot_revert":
                task = vm.RevertToCurrentSnapshot_Task()
            elif action == "tools_upgrade":
                task = vm.UpgradeTools_Task()
            else:
                _LOGGER.error("vm_guest_action: 不支持的动作 %s", action)
                return False

            if task:
                task_status(
                    hass,
                    task,
                    f"{action} on {vm.name}",
                    notify,
                )
            break
        else:
            _LOGGER.info(
                "VM %s on host %s not found. Make sure the name is correct",
                target_vm,
                target_host,
            )
    except vmodl.MethodFault as error:
        _LOGGER.error("VM %s 动作 %s 失败: %s", target_vm, action, error.msg)
        return False
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.error("VM %s 动作 %s 异常: %s", target_vm, action, error)
        return False
    finally:
        esx_disconnect(conn)

    return True


def acquire_console_ticket(conn_details, moid):
    """为指定 moId 的虚拟机取控制台票据（服务端调用，凭据绝不下发到浏览器）。

    返回：
      · {"mode": "webmks", "ticket": ..., "host": ..., "port": 443,
         "ssl_thumbprint": ..., "name": 虚拟机名, "power_state": ...}
      · {"mode": "vmrc", "uri": "vmrc://clone:<ticket>@host:port/?moid=<moid>", ...}
      · 失败 {"mode": "error", "error": 中文原因, "power_state": ...}

    ESXi 5.5 的 AcquireTicket 不支持 webmks/vmrc 票据类型（实测抛
    InvalidArgument），此时降级为 SessionManager.AcquireCloneTicket()，
    由浏览器侧调起本机安装的 VMware Remote Console。
    """
    conn = esx_connect(**conn_details)
    if not conn:
        return {"mode": "error", "error": "无法连接到 ESXi 主机，请检查集成配置"}

    try:
        content = conn.RetrieveContent()
        obj_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vm = next((v for v in obj_view.view if getattr(v, "_moId", None) == str(moid)), None)
        obj_view.Destroy()

        if vm is None:
            return {"mode": "error", "error": f"找不到 moId 为 {moid} 的虚拟机"}

        power_state = str(vm.runtime.powerState)
        vm_name = vm.summary.config.name
        host_addr = conn_details["host"]
        host_port = int(conn_details.get("port") or 443)

        if power_state != "poweredOn":
            return {
                "mode": "error",
                "error": "虚拟机当前未开机，控制台仅在运行中可用；请先开机后重试",
                "power_state": power_state,
                "name": vm_name,
            }

        # 首选：HTML5 内置控制台（webmks 票据 + wss）
        try:
            ticket = vm.AcquireTicket("webmks")
            return {
                "mode": "webmks",
                "ticket": ticket.ticket,
                "host": host_addr,
                "port": host_port,
                "ssl_thumbprint": getattr(ticket, "sslThumbprint", None),
                "name": vm_name,
                "power_state": power_state,
            }
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.info(
                "VM %s 不支持 webmks 票据（%s），降级 VMRC",
                vm_name,
                getattr(error, "msg", error),
            )

        # 降级：VMRC 克隆票据（5.5 实测唯一可用的网页控制台路径）
        try:
            clone_ticket = content.sessionManager.AcquireCloneTicket()
            uri = (
                f"vmrc://clone:{clone_ticket}@{host_addr}:{host_port}/?moid={moid}"
            )
            return {
                "mode": "vmrc",
                "uri": uri,
                "name": vm_name,
                "power_state": power_state,
            }
        except Exception as error:  # pylint: disable=broad-except
            return {
                "mode": "error",
                "error": f"webmks 与 VMRC 票据均获取失败：{getattr(error, 'msg', error)}",
                "power_state": power_state,
                "name": vm_name,
            }
    finally:
        esx_disconnect(conn)


# ===================== 截图会话缓存（Cookie复用，避免频繁登录） =====================

import json
import os as _os
import time as _time
import threading as _threading

_screenshot_lock = _threading.Lock()
_screenshot_sessions = {}   # {esxi_ip: requests.Session}
_screenshot_cookies = {}   # {esxi_ip: cookies_dict}
_screenshot_vm_ids = {}   # {esxi_ip: {vm_name: vm_id}}
_screenshot_last_login = {}  # {esxi_ip: timestamp}
_COOKIE_EXPIRE = 3600  # Cookie有效期1小时


def _load_screenshot_cookies(host):
    """从文件加载截图Cookie缓存"""
    cookie_file = f"/config/esxi_screenshot_cookies_{host.replace('.', '_')}.json"
    try:
        if _os.path.exists(cookie_file):
            with open(cookie_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _screenshot_cookies[host] = data.get("cookies", {})
                _screenshot_last_login[host] = data.get("last_login", 0)
                _screenshot_vm_ids[host] = data.get("vm_ids", {})
                _LOGGER.debug("已加载 %s 的截图Cookie缓存", host)
    except Exception as e:
        _LOGGER.debug("加载截图Cookie缓存失败: %s", e)


def _save_screenshot_cookies(host):
    """保存截图Cookie缓存到文件"""
    cookie_file = f"/config/esxi_screenshot_cookies_{host.replace('.', '_')}.json"
    try:
        data = {
            "cookies": _screenshot_cookies.get(host, {}),
            "last_login": _screenshot_last_login.get(host, 0),
            "vm_ids": _screenshot_vm_ids.get(host, {}),
        }
        with open(cookie_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _LOGGER.debug("保存截图Cookie缓存失败: %s", e)


def _screenshot_login(host, user, pwd):
    """登录ESXi获取截图用的Cookie"""
    import requests as req_lib
    login_url = f"https://{host}/sdk"
    try:
        session = req_lib.Session()
        session.verify = False
        # ESXi登录请求
        login_body = '''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:urn="urn:vim25">
<soapenv:Body><urn:Login><urn:_this type="SessionManager">ha-sessionmgr</urn:_this>
<urn:userName>{user}</urn:userName><urn:password>{pwd}</urn:password></urn:Login></soapenv:Body></soapenv:Envelope>'''.format(user=user, pwd=pwd)

        resp = session.post(
            login_url,
            data=login_body,
            headers={'Content-Type': 'text/xml; charset=utf-8', 'User-Agent': 'VMware vim25'},
            verify=False,
            timeout=15
        )

        if resp.status_code == 200:
            # 保存Cookie
            _screenshot_cookies[host] = dict(session.cookies)
            _screenshot_last_login[host] = _time.time()
            _screenshot_sessions[host] = session
            _save_screenshot_cookies(host)
            _LOGGER.info("截图Cookie登录成功: %s", host)
            return session
        else:
            _LOGGER.warning("截图Cookie登录失败: %s (status=%d)", host, resp.status_code)
            session.close()
            return None
    except Exception as e:
        _LOGGER.debug("截图Cookie登录异常: %s", e)
        return None


def _get_screenshot_session(host, user, pwd):
    """获取截图用的会话（优先复用Cookie）"""
    import requests as req_lib

    now = _time.time()

    # 1. 检查现有session是否可用
    if host in _screenshot_sessions:
        last = _screenshot_last_login.get(host, 0)
        if now - last < _COOKIE_EXPIRE:
            return _screenshot_sessions[host]

    # 2. 尝试从缓存Cookie恢复session
    if host not in _screenshot_cookies:
        _load_screenshot_cookies(host)

    if host in _screenshot_cookies and _screenshot_cookies[host]:
        last = _screenshot_last_login.get(host, 0)
        if now - last < _COOKIE_EXPIRE:
            session = req_lib.Session()
            session.verify = False
            session.cookies.update(_screenshot_cookies[host])
            _screenshot_sessions[host] = session
            _LOGGER.debug("复用缓存Cookie: %s", host)
            return session

    # 3. Cookie无效或过期，重新登录
    return _screenshot_login(host, user, pwd)


def _get_vm_id_cached(host, user, pwd, port, ssl_verify, vm_name):
    """获取虚拟机moId（优先从缓存读取）"""
    # 1. 检查缓存
    if host in _screenshot_vm_ids:
        vm_ids = _screenshot_vm_ids[host]
        if vm_name in vm_ids:
            _LOGGER.debug("使用缓存的VM ID: %s -> %s", vm_name, vm_ids[vm_name])
            return vm_ids[vm_name]

    # 2. 缓存未命中，连接ESXi获取
    si = None
    try:
        ssl_context = ignore_ssl_verify() if not ssl_verify else None
        if ssl_context:
            si = SmartConnect(host=host, user=user, pwd=pwd, port=port, sslContext=ssl_context)
        else:
            si = SmartConnect(host=host, user=user, pwd=pwd, port=port)

        if not si:
            return None

        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)

        # 获取所有VM的ID并缓存
        vm_ids = {}
        for vm in container.view:
            vm_id = vm._moId if hasattr(vm, '_moId') else str(vm).split(':')[-1].replace("'", "")
            vm_ids[vm.name] = vm_id
        container.Destroy()

        # 更新缓存
        _screenshot_vm_ids[host] = vm_ids
        _save_screenshot_cookies(host)
        _LOGGER.info("已缓存 %s 的 %d 个VM ID", host, len(vm_ids))

        return vm_ids.get(vm_name)

    except Exception as e:
        _LOGGER.error("获取VM ID失败: %s", e)
        return None
    finally:
        if si:
            Disconnect(si)


def _invalidate_screenshot_session(host):
    """使截图会话失效（401/403时调用）"""
    if host in _screenshot_sessions:
        try:
            _screenshot_sessions[host].close()
        except Exception:
            pass
        del _screenshot_sessions[host]
    _screenshot_cookies.pop(host, None)
    _screenshot_last_login.pop(host, None)
    _LOGGER.debug("已失效截图会话: %s", host)


def get_vm_screenshot(host, user, pwd, port, ssl_verify, vm_name, save_path):
    """获取虚拟机截图并保存到指定路径（Cookie复用，避免频繁登录）"""
    import os
    import tempfile
    import subprocess
    import requests as req_lib

    with _screenshot_lock:
        # 1. 获取VM的moId（优先从缓存）
        vm_id = _get_vm_id_cached(host, user, pwd, port, ssl_verify, vm_name)
        if not vm_id:
            _LOGGER.error("未找到虚拟机 %s 的ID", vm_name)
            return False

    # 2. 通过HTTP截图API获取截图（Cookie复用）
    screenshot_data = None
    screenshot_url = f"https://{host}/screen?id={vm_id}"

    with _screenshot_lock:
        session = _get_screenshot_session(host, user, pwd)

    if session:
        try:
            resp = session.get(
                screenshot_url,
                headers={'User-Agent': 'Mozilla/4.0'},
                verify=False,
                allow_redirects=False,
                timeout=30
            )

            if resp.status_code == 200 and len(resp.content) > 1000:
                screenshot_data = resp.content
            elif resp.status_code in [401, 403]:
                # Cookie失效，重新登录
                _LOGGER.info("截图Cookie失效，重新登录: %s", host)
                _invalidate_screenshot_session(host)
                with _screenshot_lock:
                    session = _get_screenshot_session(host, user, pwd)
                if session:
                    resp = session.get(
                        screenshot_url,
                        headers={'User-Agent': 'Mozilla/4.0'},
                        verify=False,
                        allow_redirects=False,
                        timeout=30
                    )
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        screenshot_data = resp.content
        except Exception as e:
            _LOGGER.debug("Cookie方式获取截图失败: %s，尝试curl", e)

    # 3. curl兜底
    if not screenshot_data:
        try:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
                tmp_path = tmp_file.name

            cmd = [
                'curl', '-k', '-s', '-m', '30',
                '-u', f'{user}:{pwd}',
                '-o', tmp_path,
                '--tlsv1.0',
                '--ciphers', 'DEFAULT:@SECLEVEL=0',
                f'https://{host}/screen?id={vm_id}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            if result.returncode == 0 and os.path.exists(tmp_path):
                with open(tmp_path, 'rb') as f:
                    screenshot_data = f.read()
                os.unlink(tmp_path)
        except Exception as e:
            _LOGGER.debug("curl方式获取截图失败: %s", e)

    # 4. 保存截图
    if not screenshot_data or len(screenshot_data) < 1000:
        _LOGGER.warning("获取虚拟机 %s 截图失败或数据过小", vm_name)
        return False

    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        # 原子写：先写临时文件再替换，避免读取端（image 实体）读到半张图导致破图
        tmp_save_path = f"{save_path}.tmp"
        with open(tmp_save_path, 'wb') as f:
            f.write(screenshot_data)
        os.replace(tmp_save_path, save_path)
        _LOGGER.info("虚拟机 %s 截图已保存到 %s (%d bytes)", vm_name, save_path, len(screenshot_data))
        return True
    except Exception as e:
        _LOGGER.error("保存截图失败: %s", e)
        return False


# ============================ vSphere 事件（EventManager）============================ #
# vSphere 的「事件」就是 vSphere Client 里「监控 → 事件与任务」的结构化记录
#   （开关机 / 登录 / 配置变更 / 主机告警等）。EventManager 本身只暴露
#   description / latestEvent / maxCollector 三个属性，读历史必须先用
#   CreateCollectorForEvents + 时间过滤器建一个历史收集器，再 ReadNextEvents。
#   收集器是服务端对象，用完必须 DestroyCollector，否则会占用 maxCollector 配额。
def _format_event_message(ev):
    """从 vim.event.Event 提取一句人类可读的话。

    优先用服务端拼好的 ``fullFormattedMessage``（独立 ESXi 不一定填充）；
    否则拿 ``message.defaultMessage`` 自己替换 ``{0}/{name}`` 占位符；
    再不行就退回事件类名（例如 VmPoweredOnEvent）。
    """
    text = getattr(ev, "fullFormattedMessage", None)
    if text:
        return text.strip()
    msg = getattr(ev, "message", None)
    default = getattr(msg, "defaultMessage", None) if msg is not None else None
    if not default:
        return type(ev).__name__
    try:
        args = {str(a.key): str(a.value) for a in (msg.arg or [])}

        def repl(match):
            key = match.group(1)
            if key in args:
                return args[key]
            # 占位符有时是 {01} 而参数键是 {1}
            stripped = key.lstrip("0") or "0"
            return args.get(stripped, match.group(0))

        return re.sub(r"\{([^{}]+)\}", repl, default).strip()
    except Exception:  # pylint: disable=broad-except
        return default.strip()


def _event_category_map(content):
    """eventType(类名) → 分类（info / warning / error / user）映射表。

    注意 pyVmomi 里 ``EventDescription.eventInfo[].key`` 不是字符串而是
    **事件类型对象**（pyVmomi.VmomiSupport 动态生成的 vim.event.XxxEvent 类），
    它的 ``__name__`` 形如 ``vim.event.XxxEvent``，与 ``type(event).__name__``
    同源 —— 两个形式（全名 / 短名）都建一份索引，取值时互为兜底。
    """
    result = {}
    try:
        for detail in content.eventManager.description.eventInfo or []:
            name = getattr(detail.key, "__name__", None)
            if not name:
                continue
            result[name] = detail.category
            result[name.rsplit(".", 1)[-1]] = detail.category
    except Exception:  # pylint: disable=broad-except
        return {}
    return result


def _event_category(type_name, category_map):
    """按全名/短名两级查分类，查不到给最保守的 info。"""
    return category_map.get(type_name) or category_map.get(
        type_name.rsplit(".", 1)[-1], "info"
    )


def _event_object_name(ev):
    """取事件关联对象（VM / 主机 / 存储 / 网络）的名字与种类。"""
    for attr, kind in (
        ("vm", "vm"),
        ("host", "host"),
        ("ds", "datastore"),
        ("net", "network"),
    ):
        ref = getattr(ev, attr, None)
        if ref is not None:
            name = getattr(ref, "name", None)
            if name:
                return name, kind
    return "", ""


def normalize_event(ev, category_map):
    """把 pyVmomi 事件对象压成可进 HA 状态/事件总线的纯字典。"""
    full_type = type(ev).__name__  # 形如 vim.event.UserLoginSessionEvent
    short_type = full_type.rsplit(".", 1)[-1]
    obj_name, obj_kind = _event_object_name(ev)
    created = ev.createdTime
    return {
        "key": int(ev.key),
        # 对外一律用短名（vim.event.XxxEvent → XxxEvent）
        "type": short_type,
        "category": _event_category(full_type, category_map),
        # 两条时间：本地时区（展示）与 UTC ISO（下轮增量查询用）
        "time": created.astimezone().isoformat(),
        "time_utc": created.astimezone(timezone.utc).isoformat(),
        "user": getattr(ev, "userName", "") or "",
        "object": obj_name,
        "object_kind": obj_kind,
        # 仅用于过滤集成自身产生的登录噪音；不进传感器属性
        "user_agent": getattr(ev, "userAgent", "") or "",
        "message": _format_event_message(ev),
    }


def fetch_events(content, begin_time, max_events=300):
    """读取 begin_time（含）之后的事件，按时间升序返回归一化字典列表。

    单次轮询的事件量被 ``max_events`` 截断（事件风暴时保护 hostd 与 HA），
    最新的一批可能因此被跳过 —— 由调用方按 last_key/last_time 决定怎么补。
    """
    event_mgr = content.eventManager
    category_map = _event_category_map(content)
    spec = vim.event.EventFilterSpec(
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin_time)
    )
    collector = event_mgr.CreateCollectorForEvents(spec)
    out = []
    try:
        while len(out) < max_events:
            batch = collector.ReadNextEvents(100)
            if not batch:
                break
            for ev in batch:
                out.append(normalize_event(ev, category_map))
                if len(out) >= max_events:
                    break
    finally:
        try:
            collector.DestroyCollector()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("DestroyCollector 失败（可忽略）", exc_info=True)
    return out
