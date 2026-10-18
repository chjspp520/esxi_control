"""Sensor platform for esxi_control."""
import inspect
import json
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from string import capwords
from datetime import timedelta
from homeassistant.const import MATCH_ALL
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .entity_sync import dedupe_entities, register_platform
from .entry_options import current_options
from .naming import AGGREGATE_ENTITY_ID
from .op_log import STATE_IDLE, get_log
from .event_hub import CATEGORY_CN, get_event_hub
from .vm_bulk import (
    device_by_identifier,
    host_device_info,
    host_device_key,
    vm_device_key,
)
from .const import (
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    HOST_INTERNAL_KEYS,
    MAP_TO_MEASUREMENT,
    CONF_AGGREGATE_INTERVAL,
    DEFAULT_AGGREGATE_INTERVAL,
    MIN_AGGREGATE_INTERVAL,
    MAX_AGGREGATE_INTERVAL,
    VM_INTERNAL_KEYS,
    VM_PERF_KEYS,
)

SCAN_INTERVAL = timedelta(seconds=15)

# 聚合实体的刷新间隔由选项控制（CONF_AGGREGATE_INTERVAL，默认 60 秒）；
#   平台轮询是 15 秒一次，这里再节流 —— 遍历实体/设备注册表没必要跟着 15 秒走。
#   快照里每个实体带的 state 也是按这个间隔采的（不是实时值）。
# recorder 对单条状态的属性总大小有约 16KB（16384 字节）的硬上限，超了属性会被丢弃。
#   本实体的属性现在**显式声明为不记录**（ESXiEntitiesAggregateSensor 的
#   _unrecorded_attributes = MATCH_ALL），所以：属性不再受这个上限影响，也不会再刷
#   "State attributes ... exceed maximum size of 16384 bytes" 警告。
#   AGGREGATE_SAFE_BYTES 因此改为**体积体检线**（快照越大，每次重建与前端读取越重）。
#   想连状态行都不写数据库，只能用户侧配置（集成侧没有这个接口）：
#     recorder:
#       exclude:
#         entities: [sensor.esxi_all_entities]
#   其它减重办法（按性价比）：① 去掉 name（省约 4.8KB，按短名匹配角色的使用方会失效）；
#     ② 字段名改短键（entity_id→e / name→n / icon→i / state→s，省约 2.2KB，使用方要跟着改）。
AGGREGATE_SAFE_BYTES = 16000

# 每个属性的图标：HA 会把它写进 state attributes（attributes["icon"]），
#   实体聚合也会把它带出去。键与 VM_ATTR_CN / HOST_ATTR_CN 完全一致。
#   ⚠️ 只使用确定存在的 mdi 图标名，写错会显示成空白。
VM_ATTR_ICON = {
    "name": "mdi:rename-box",
    "cpu_count": "mdi:cpu-64-bit",
    "cpu_use_pct": "mdi:speedometer",
    "cpu_ready_pct": "mdi:timer-sand",
    "mem_balloon_mb": "mdi:balloon-outline",
    "net_dropped_ps": "mdi:alert-circle-outline",
    "cpuusage_ghz": "mdi:speedometer",
    "memory_allocated_mb": "mdi:memory",
    "memory_used_mb": "mdi:memory",
    "memory_active_mb": "mdi:memory",
    "used_space_gb": "mdi:harddisk",
    "uptime_hours": "mdi:clock-outline",
    "snapshots": "mdi:camera-burst",
    "tools_status": "mdi:tools",
    "guest_os": "mdi:laptop",
    "guest_ip": "mdi:ip-network",
    "status": "mdi:heart-pulse",
    "state": "mdi:power",
    "host_name": "mdi:server-network",
}

#: VM 电源状态英文值 → 中文展示（只用于传感器展示层，绝不动 hass.data 快照）
VM_POWER_STATE_CN = {
    "running": "运行中",
    "off": "已关机",
    "suspended": "挂起中",
}

HOST_ATTR_ICON = {
    "name": "mdi:server",
    # power_policy 的 sensor 实体已不再创建（改由 select 承担，见 LEGACY_HOST_ATTRS_SKIPPED），
    #   映射保留只为兼容历史快照 / 防御性兜底
    "power_policy": "mdi:power-settings",
    "available_power_policies": "mdi:format-list-bulleted",
    "build": "mdi:package-variant-closed",
    "version": "mdi:information-outline",
    "state": "mdi:power",
    # 下面 4 项目前 get_host_info 并不产出（HOST_ATTR_CN 里有、实体不存在），
    #   一并给出图标只是防御：将来若真的产出，不至于落到兜底图标上
    "cpu_usage": "mdi:speedometer",
    "memory_usage": "mdi:memory",
    "connection_state": "mdi:lan-connect",
    "overall_status": "mdi:heart-pulse",
    "cputotal_ghz": "mdi:cpu-64-bit",
    "cpuusage_ghz": "mdi:speedometer",
    "memtotal_gb": "mdi:memory",
    "memusage_gb": "mdi:memory",
    "vms": "mdi:monitor-multiple",
    "vms_on": "mdi:play-circle-outline",
    "uptime_hours": "mdi:clock-outline",
    "maintenance_mode": "mdi:wrench-outline",
    "shutdown_supported": "mdi:power-plug-off-outline",
    "hardware_vendor": "mdi:domain",
    "hardware_model": "mdi:desktop-tower",
    "bios_version": "mdi:chip",
    "bios_date": "mdi:calendar-month-outline",
    "cpu_sockets": "mdi:server-network",
    "cpu_cores": "mdi:cpu-64-bit",
    "cpu_threads": "mdi:tune",
    "ntp_servers": "mdi:clock-check-outline",
    "ntp_timezone": "mdi:earth",
}

# 中文短名：has_entity_name 打开后，friendly_name = 设备名 + 这里的短名
VM_ATTR_CN = {
    "name": "名称",
    "cpu_count": "CPU数量",
    "cpu_use_pct": "CPU使用率",
    "cpu_ready_pct": "CPU就绪率",
    "mem_balloon_mb": "内存气球",
    "net_dropped_ps": "网卡丢包速率",
    "cpuusage_ghz": "CPU使用量",
    "memory_allocated_mb": "分配内存",
    "memory_used_mb": "已用内存",
    "memory_active_mb": "活跃内存",
    "used_space_gb": "已用空间",
    "uptime_hours": "运行时长",
    "snapshots": "快照数量",
    "tools_status": "Tools状态",
    "guest_os": "客户机操作系统",
    "guest_ip": "IP地址",
    "status": "运行状态",
    "state": "电源状态",
    "host_name": "所属主机",
}

HOST_ATTR_CN = {
    "name": "名称",
    # 同上：电源策略只由 select 实体提供，这里保留纯粹是兜底
    "power_policy": "电源策略",
    "available_power_policies": "可选电源策略",
    "build": "构建版本",
    "version": "ESXi版本",
    "state": "电源状态",
    "cputotal_ghz": "CPU总量",
    "cpuusage_ghz": "CPU使用量",
    "cpu_usage": "CPU使用率",
    "memtotal_gb": "内存总量",
    "memusage_gb": "内存已用",
    "memory_usage": "内存使用率",
    "vms": "虚拟机总数",
    "vms_on": "已开机虚拟机",
    "uptime_hours": "运行时长",
    "maintenance_mode": "维护模式",
    "shutdown_supported": "支持关机",
    "connection_state": "连接状态",
    "overall_status": "整体状态",
    "hardware_vendor": "硬件厂商",
    "hardware_model": "硬件型号",
    "bios_version": "BIOS版本",
    "bios_date": "BIOS发布日期",
    "cpu_sockets": "CPU插槽",
    "cpu_cores": "CPU物理核心",
    "cpu_threads": "CPU逻辑线程",
    "ntp_servers": "NTP服务器",
    "ntp_timezone": "时区",
}

# 主机属性里不再单独建 sensor 实体的项：
#   power_policy —— 电源策略已经由 select.<host>_power_policy 承担（同一个值、可读可写），
#   再留一个只读 sensor 只会让实体聚合里出现两条「同名 + 同图标 + 同值」的条目，
#   使用方还要靠 domain 去分辨，纯属噪音。
LEGACY_HOST_ATTRS_SKIPPED = ("power_policy",)

_LOGGER = logging.getLogger(__name__)


def _remove_legacy_host_sensors(hass, entry_id):
    """Drop the deprecated read-only host sensors from the entity registry.

    不再创建的实体，HA 的实体注册表**不会**自动遗忘：残留条目会变成
    restored / unavailable 的幽灵实体，而且还照样出现在实体聚合快照里。
    所以升级时必须显式删除（唯一 ID 以 _attr_power_policy 结尾）。
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry_id):
        if reg_entry.domain != "sensor":
            continue
        unique_id = reg_entry.unique_id or ""
        if any(unique_id.endswith(f"_attr_{key}") for key in LEGACY_HOST_ATTRS_SKIPPED):
            _LOGGER.info(
                "移除已废弃的只读主机传感器：%s (%s)",
                reg_entry.entity_id,
                unique_id,
            )
            registry.async_remove(reg_entry.entity_id)


async def async_setup_platform(
    hass, config, async_add_entities, discovery_info=None
):  # pylint: disable=unused-argument
    """Set up sensor platform."""
    for cond in hass.data[DOMAIN_DATA]["monitored_conditions"]:
        for obj in hass.data[DOMAIN_DATA][cond]:
            async_add_entities([ESXiSensor(hass, discovery_info, cond, obj)], True)


def _build_entities(hass, config_entry):
    """Return every sensor entity this entry should currently have.

    同步 service（entity_sync）会反复调用它来算「此刻应该存在的实体」，
    所以必须是纯函数：只返回对象，不添加实体、不改 hass.data。
    """
    config = config_entry.data
    entry_id = config_entry.entry_id
    sensors = []

    for cond in hass.data[DOMAIN_DATA][entry_id]["monitored_conditions"]:
        for obj in hass.data[DOMAIN_DATA][entry_id][cond]:
            if cond == "vm":
                # Create individual sensors for each VM attribute
                vm_data = hass.data[DOMAIN_DATA][entry_id][cond][obj]
                for attr_key, attr_value in vm_data.items():
                    if attr_key in VM_INTERNAL_KEYS:  # Skip internal fields
                        continue
                    sensors.append(ESXiSensor(hass, config, cond, obj, config_entry, attr_key))
                # 控制台完整访问地址（与设备页「访问」按钮同源，方便复制 / 自动化取用）
                sensors.append(ESXiVMConsoleUrlSensor(hass, config, obj, config_entry))
            elif cond == "vmhost":
                # Create individual sensors for each host attribute
                host_data = hass.data[DOMAIN_DATA][entry_id][cond][obj]
                # 6 个诊断详情传感器（硬件健康 / 服务 / 网络 / 防火墙 / 会话 / 存储）：
                #   明细嵌套在内部字段里，单独聚合成一个实体而不是拆一堆
                for detail_kind in (
                    "health",
                    "services",
                    "network",
                    "firewall",
                    "sessions",
                    "storage",
                ):
                    sensors.append(
                        ESXiHostDetailSensor(
                            hass, config, obj, detail_kind, config_entry
                        )
                    )
                # 虚拟机管理页面的访问地址（受「远程桌面」总开关控制）
                sensors.append(
                    ESXiHostManagerUrlSensor(hass, config, obj, config_entry)
                )
                for attr_key, attr_value in host_data.items():
                    # 内部嵌套字段 / 改由其它平台承担的字段不建逐属性传感器
                    if attr_key in HOST_INTERNAL_KEYS:
                        continue
                    if attr_key in LEGACY_HOST_ATTRS_SKIPPED:
                        # 电源策略改由 select 实体承担（见 LEGACY_HOST_ATTRS_SKIPPED）
                        continue
                    sensors.append(ESXiSensor(hass, config, cond, obj, config_entry, attr_key))
            elif cond == "license":
                # License entities go to their respective host devices, except vCenter license
                if obj == "vcenter_license":
                    # vCenter license stays under ESXi Control device
                    sensors.append(ESXiSensor(hass, config, cond, obj, config_entry))
                else:
                    # Host licenses go to their respective host devices
                    sensors.append(ESXiSensor(hass, config, cond, obj, config_entry))
            else:
                # Datastore and other entities stay under ESXi Control device
                sensors.append(ESXiSensor(hass, config, cond, obj, config_entry))

    # 聚合所有实体的快照，挂到主机设备上（每个设备一个节点）
    host_name = next(iter(hass.data[DOMAIN_DATA][entry_id]["vmhost"]), None)
    sensors.append(
        ESXiEntitiesAggregateSensor(config, entry_id, host_name, config_entry)
    )

    # 批量操作日志（与「批量分组」一样，只在开了虚拟机监控时才有意义）
    if "vm" in hass.data[DOMAIN_DATA][entry_id]["monitored_conditions"]:
        sensors.append(ESXiOperationLogSensor(config_entry))

    # ESXi 事件流（EventManager）：与监控开关无关，只要连得上主机就有事件
    sensors.append(ESXiEventSensor(config_entry))

    return dedupe_entities(sensors)


async def async_setup_entry(hass, config_entry, async_add_devices):
    """Set up sensor platform."""
    entry_id = config_entry.entry_id

    # 升级清理：删掉不再创建的只读主机传感器（否则会残留成幽灵实体）
    _remove_legacy_host_sensors(hass, entry_id)

    async_add_devices(_build_entities(hass, config_entry), True)
    # 登记同步钩子：ESXi 上新增/删除虚拟机后，靠它增删实体
    register_platform(hass, entry_id, "sensor", _build_entities, async_add_devices)


class ESXiOperationLogSensor(Entity):
    """批量操作日志。

    状态 = 当前正在进行的批量操作（空闲时是「空闲」）；
    属性 = 本次会话的滚动记录（按时间正序）+ 进度。

    ⚠️ 只记录**批量**操作（全部开机 / 全部关闭 / 全部快照 / 全部重启）：
    单台操作在 HA 自己的日志里已经看得到。日志只存在内存里，重启即清空。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:clipboard-text-clock"
    # 状态完全由日志中枢推送，不需要轮询
    _attr_should_poll = False

    def __init__(self, config_entry):
        """Initialize the operation log sensor."""
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._log = None

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "操作日志"

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_operation_log"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    def _get_log(self):
        """取日志中枢并订阅（重载集成后中枢会换新对象，要重新订阅）。"""
        log = get_log(self.hass, self._entry_id)
        if log is not None and log is not self._log:
            if self._log is not None:
                self._log.unsubscribe(self._handle_change)
            self._log = log
            log.subscribe(self._handle_change)
        return log

    def _handle_change(self):
        """中枢有变化就立刻刷新实体。"""
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Subscribe to the log hub."""
        await super().async_added_to_hass()
        self._get_log()

    async def async_will_remove_from_hass(self):
        """Unsubscribe from the log hub."""
        if self._log is not None:
            self._log.unsubscribe(self._handle_change)
            self._log = None
        await super().async_will_remove_from_hass()

    @property
    def state(self):
        """Return the running batch operation, or 空闲."""
        log = self._get_log()
        return log.state if log is not None else STATE_IDLE

    @property
    def extra_state_attributes(self):
        """Return progress and the rolling log."""
        log = self._get_log()
        if log is None:
            return {"running": False, "log": []}
        return log.attributes()


class ESXiEventSensor(Entity):
    """ESXi 事件流传感器（数据来自 event_hub，推送式、不轮询）。

    状态 = 最新一条事件的一句话描述（无事件时为「暂无事件」）；
    属性 = 最近 20 条事件（时间 / 分类 / 类型 / 用户 / 对象 / 内容）。
    每条新事件同时进 HA 事件总线（``esxi_control_event``）与日志簿。
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, config_entry):
        """Initialize the event sensor."""
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._hub = None

    @property
    def name(self):
        """短名（friendly_name = 设备名 + 短名）。"""
        return "事件"

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_events"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    @property
    def icon(self):
        """按最新事件分类切换图标。"""
        hub = self._get_hub()
        events = hub.events if hub is not None else []
        if not events:
            return "mdi:history"
        return {
            "error": "mdi:alert-circle",
            "warning": "mdi:alert",
            "user": "mdi:account-check-outline",
        }.get(events[0]["category"], "mdi:information-outline")

    def _get_hub(self):
        """取事件中枢并订阅（重载集成后中枢会换新对象，要重新订阅）。"""
        hub = get_event_hub(self.hass, self._entry_id)
        if hub is not None and hub is not self._hub:
            if self._hub is not None:
                self._hub.unsubscribe(self._handle_change)
            self._hub = hub
            hub.subscribe(self._handle_change)
        return hub

    def _handle_change(self):
        """中枢有新事件就立刻刷新实体。"""
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        """订阅事件中枢。"""
        await super().async_added_to_hass()
        self._get_hub()

    async def async_will_remove_from_hass(self):
        """取消订阅。"""
        if self._hub is not None:
            self._hub.unsubscribe(self._handle_change)
            self._hub = None
        await super().async_will_remove_from_hass()

    @property
    def state(self):
        """最新一条事件的一句话描述。"""
        hub = self._get_hub()
        if hub is None or not hub.events:
            return "暂无事件"
        latest = hub.events[0]
        obj = f"{latest['object']} · " if latest.get("object") else ""
        return f"{obj}{latest['message']}"[:240]

    @property
    def extra_state_attributes(self):
        """最近事件列表与计数。"""
        hub = self._get_hub()
        if hub is None:
            return {"events": [], "new_events_since_startup": 0}
        events = []
        for event in hub.events:
            events.append(
                {
                    "time": event["time"],
                    "category": CATEGORY_CN.get(event["category"], event["category"]),
                    "type": event["type"],
                    "user": event["user"],
                    "object": event["object"],
                    "message": event["message"],
                }
            )
        latest_time = events[0]["time"] if events else None
        return {
            "events": events,
            "latest_event_time": latest_time,
            "new_events_since_startup": hub.new_count,
        }


class ESXiHostDetailSensor(Entity):
    """主机诊断详情传感器：硬件健康 / 服务状态 / 网络拓扑 / 防火墙。

    与逐属性传感器的分工：标量（NTP 服务器、CPU 插槽数等）直接由 ESXiSensor 承担；
    这四类是**明细列表**（89 个硬件传感器、十几个服务、vSwitch/端口组/网卡、
    30+ 防火墙规则集），拆成实体就是几百个噪音，所以聚合成 4 个诊断传感器：
    状态是一行人类可读摘要，完整明细放在属性里，点开实体即可查看。
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    DETAILS = {
        "health": ("硬件健康", "mdi:heart-pulse"),
        "services": ("服务状态", "mdi:application-cog-outline"),
        "network": ("网络拓扑", "mdi:lan"),
        "firewall": ("防火墙规则", "mdi:shield-check-outline"),
        "sessions": ("登录会话", "mdi:account-multiple"),
        "storage": ("存储拓扑", "mdi:nas"),
    }

    def __init__(self, hass, config, host_key, kind, config_entry):
        """Initialize the host detail sensor."""
        self.hass = hass
        self.config = config
        self._host_key = host_key
        self._kind = kind
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._detail = None
        self._host_data = {}

    def update(self):
        """取出本实体对应的主机明细块（纯快照；数据由中央定时器统一拉取）。"""
        self._host_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"].get(self._host_key)
            or {}
        )
        self._detail = self._host_data.get(self._kind)

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """主机在线且明细块存在时可用。"""
        return bool(self._detail) and self._host_data.get("state") == "poweredOn"

    @property
    def name(self):
        """Return the short name."""
        return self.DETAILS[self._kind][0]

    @property
    def icon(self):
        """Return the icon."""
        return self.DETAILS[self._kind][1]

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_detail_{self._kind}_{self._host_key}"
        )

    @property
    def device_info(self):
        """Attach to the host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_key))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware ESXi",
            "model": "ESXi 主机",
        }

    @property
    def state(self):
        """一行摘要。"""
        d = self._detail or {}
        if self._kind == "health":
            alerts = len(d.get("alerts", []))
            return "正常" if alerts == 0 and d.get("total", 0) > 0 else f"{alerts} 项告警"
        if self._kind == "services":
            # 明细块本身就是服务列表（见 extra_state_attributes）
            running = sum(1 for item in self._service_list() if item.get("running"))
            total = len(self._service_list())
            return f"{running}/{total} 运行中"
        if self._kind == "network":
            return (
                f"{len(d.get('vswitch', []))} 虚拟交换机 / "
                f"{len(d.get('portgroup', []))} 端口组 / "
                f"{len(d.get('pnic', []))} 物理网卡 / "
                f"{len(d.get('vmknic', []))} VMkernel"
            )
        if self._kind == "sessions":
            return f"{d.get('total', len(d.get('items', [])))} 个登录会话"
        if self._kind == "storage":
            return (
                f"{len(d.get('hba', []))} HBA / "
                f"{len(d.get('luns', []))} LUN / "
                f"{len(d.get('volumes', []))} 存储卷"
            )
        # firewall
        return f"{d.get('enabled', 0)}/{d.get('total', 0)} 已启用"

    def _service_list(self):
        """服务明细列表（键名为 services，见 _get_services）。"""
        if not self._detail:
            return []
        return self._detail if isinstance(self._detail, list) else []

    @property
    def extra_state_attributes(self):
        """完整明细（HA 会以 JSON 形式展示嵌套结构）。"""
        d = self._detail
        if not d:
            return {}
        if self._kind == "services":
            # 服务块本身就是列表，包一层让属性名更清晰
            return {"services": d, "total": len(d)}
        if self._kind == "health":
            return {
                "total": d.get("total"),
                "green": d.get("green"),
                "yellow": d.get("yellow"),
                "red": d.get("red"),
                "unknown": d.get("unknown"),
                "alerts": d.get("alerts", []),
            }
        return d


class ESXiVMConsoleUrlSensor(Entity):
    """虚拟机控制台完整访问地址（远程地址）。

    值直接取自设备注册表的 configuration_url —— 设备页「访问」按钮用的就是
    同一个字段：console_web 在签名到期 / 访问基地址变化时重写它，本传感器只
    跟随读取，绝不自己另签一套，保证两处永远一致。

    完整地址约 400 字符（含 authSig），超过 HA 状态值 255 字符硬上限，所以：
      * state 只放访问源地址（scheme://host:port，几十字符），稳定不变；
      * 完整可点击地址放在 ``console_url`` 属性里，随重签自动刷新。
    界面在实体属性上选中即可复制；自动化 / 脚本统一用
    ``{{ state_attr('sensor.<虚拟机>_remote', 'console_url') }}`` 读取，
    不受状态长度限制。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:link-variant"

    def __init__(self, hass, config, vm_name, config_entry):
        """Init."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._url = None
        self._base = None
        self._vm_data = {}

    @property
    def should_poll(self):
        """Polled（跟随设备注册表的重签节奏拿到最新地址）。"""
        return True

    @property
    def unique_id(self):
        """Return a unique ID to use for this sensor."""
        return (
            f"{self.config['host'].replace('.', '_')}_"
            f"{self._entry_id}_vm_console_url_{self._vm_name}"
        )

    @property
    def name(self):
        """Short name."""
        return "远程地址"

    @property
    def available(self):
        """设备链接尚未签出前（启动最初几秒）先显示不可用。"""
        return bool(self._url)

    @property
    def state(self):
        """短地址（scheme://host:port），保证不超过 255 字符状态上限。"""
        return self._base

    @property
    def extra_state_attributes(self):
        """完整签名地址放属性（state_attr 读取，无长度顾虑，随重签刷新）。"""
        return {"console_url": self._url} if self._url else {}

    @property
    def device_info(self):
        """Attach to VM device."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    async def async_update(self):
        """从设备注册表读「访问」按钮的同一个 configuration_url。"""
        self._vm_data = (
            self.hass.data.get(DOMAIN_DATA, {})
            .get(self._entry_id, {})
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )
        # HA 2024.x+ 起 async_get 返回已缓存的注册表实例（同步）；老版本是协程，
        #   两种都兼容：是协程就 await，否则直接用。
        dev_registry = dr.async_get(self.hass)
        if inspect.isawaitable(dev_registry):
            dev_registry = await dev_registry
        device = device_by_identifier(
            dev_registry, (DOMAIN, vm_device_key(self._entry_id, self._vm_name))
        )
        self._url = getattr(device, "configuration_url", None) if device else None
        # 从完整地址拆出 scheme://host:port 作为短状态值；
        #   拆不出来（极端兜底）就退化为固定占位，绝不让 >255 的 URL 进 state。
        self._base = None
        if self._url:
            try:
                parsed = urlsplit(self._url)
                if parsed.scheme and parsed.netloc:
                    self._base = f"{parsed.scheme}://{parsed.netloc}"
            except ValueError:
                self._base = None
            if not self._base:
                self._base = "已就绪"


class ESXiHostManagerUrlSensor(Entity):
    """虚拟机管理页面的访问地址（挂在 ESXi 主机设备上）。

    值取自主机设备的 ``configuration_url`` —— 设备页「访问」按钮用的就是同一个
    字段：console_web 在签名到期 / 访问地址变化时重写它，本实体只跟随读取，
    绝不自己另签一套，保证两处永远一致。

    与「远程桌面」总开关的关系（用户明确要求地址也跟着这个开关走）：
      · 开  → state 是**不带 authSig 的绝对地址**（登录 HA 的浏览器直接点开即可，
              长度也远小于 HA 状态值 255 字符的上限）；
              ``url`` 属性给带签名的完整地址（自动化 / 脚本 / 外部打开用）；
      · 关  → 设备链接已被清空、管理页请求一律 403，本实体显示「已停用」。
    之所以不把带 authSig 的完整地址放进 state：它约 400 字符，超过 HA 的状态值
    上限（255），会被截断 / 触发告警 —— 这与「远程地址」传感器的处理完全一致。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:view-dashboard-edit"
    _attr_name = "虚拟机管理"

    def __init__(self, hass, config, host_key, config_entry):
        """Init."""
        self.hass = hass
        self.config = config
        self._host_key = host_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._enabled = True
        self._url = None
        self._path = None
        self._state = None

    @property
    def should_poll(self):
        """Polled（跟随设备注册表的重签节奏拿到最新地址）。"""
        return True

    @property
    def unique_id(self):
        """Return a unique ID to use for this sensor."""
        return (
            f"{self.config['host'].replace('.', '_')}_"
            f"{self._entry_id}_vm_manager_url_{self._host_key}"
        )

    @property
    def available(self):
        """永远可用：被总开关停用时也要能看见「已停用」这个结论。"""
        return True

    @property
    def state(self):
        """总开关关闭 → 已停用；否则给出不带签名的绝对地址。"""
        if not self._enabled:
            return "已停用"
        return self._state

    @property
    def extra_state_attributes(self):
        """开关状态 + 带签名的完整地址 + 相对路径。"""
        return {
            "enabled": self._enabled,
            "url": self._url if self._enabled else None,
            "path": self._path if self._enabled else None,
        }

    @property
    def device_info(self):
        """Attach to host device."""
        return host_device_info(self.hass, self._config_entry)

    async def async_update(self):
        """从主机设备的 configuration_url 取管理页地址，并按总开关判断可用性。"""
        # 局部导入：sensor 平台比 console_web 的模块导入更早，避免循环 import
        from .console_web import remote_desktop_enabled

        self._enabled = remote_desktop_enabled(self.hass)
        dev_registry = dr.async_get(self.hass)
        if inspect.isawaitable(dev_registry):
            dev_registry = await dev_registry
        device = device_by_identifier(
            dev_registry, (DOMAIN, host_device_key(self._entry_id, self._host_key))
        )
        url = getattr(device, "configuration_url", None) if device else None
        self._url = url if self._enabled else None
        self._path = None
        self._state = None
        if not (self._enabled and url):
            return
        try:
            parsed = urlsplit(url)
            if not (parsed.scheme and parsed.netloc):
                return
            # 去掉 authSig：登录 HA 的浏览器访问不带签名的路径同样放行，
            #   而且短得多（不会踩 255 字符的状态上限）
            query = [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key != "authSig"
            ]
            self._path = parsed.path + (f"?{urlencode(query)}" if query else "")
            self._state = f"{parsed.scheme}://{parsed.netloc}{self._path}"
        except ValueError:
            self._state = None


class ESXiSensor(Entity):
    """ESXi Control Sensor class."""

    def __init__(self, hass, config, cond, obj, config_entry=None, attribute_key=None):
        """Init."""
        self.hass = hass
        self._attr = {}
        self._config_entry = config_entry
        self._state = None
        self.config = config
        self._attribute_key = attribute_key  # For individual attribute sensors

        # If configured via yaml, set options to defaults
        # This is likely a temporary fix because yaml config will likely be removed
        if config_entry is not None:
            self._entry_id = config_entry.entry_id
            self._options = config_entry.options
        else:
            self._entry_id = None
            self._options = DEFAULT_OPTIONS
        self._cond = cond
        self._obj = obj

    def update(self):
        """Update the sensor（纯快照；数据由中央定时器统一拉取）。"""
        data = self.hass.data[DOMAIN_DATA][self._entry_id][self._cond].get(self._obj)
        if data is None:
            # 对象（虚拟机/主机/数据存储）已经从 ESXi 上消失：同步 service 会把这个实体
            #   移除；在移除之前必须保持「不可用」，而不是直接 KeyError 冒泡。
            #   ⚠️ 这是本平台唯一直接对 [cond][obj] 下标取值的地方，必须走 .get()。
            self._data = {}
            self._state = None
            self._measurement = ""
            self._attr = {}
            self._attr_available = False
            return

        self._attr_available = True
        self._data = data

        if self._attribute_key:
            # For individual attribute sensors, state is the attribute value
            raw_value = self._data.get(self._attribute_key, "Unknown")
            self._measurement = measure_format(self._attribute_key)
            self._attr = {}
            if self._cond == "vm" and self._attribute_key == "state":
                # 电源状态展示成中文（运行中/已关机/挂起中）；
                # 底层快照里的英文值保留在 raw_state 属性里，供自动化/模板判断，
                # switch / button 等平台读的是 hass.data 快照，不受这里影响。
                self._state = VM_POWER_STATE_CN.get(raw_value, raw_value)
                self._attr = {"raw_state": raw_value}
            else:
                self._state = raw_value
        else:
            # For legacy sensors (datastore, vCenter license), use configured state
            if self._options[self._cond] not in self._data.keys():
                self._state = "Error"
                self._measurement = ""
                _LOGGER.error(
                    "State is set to incorrect key. Check Options in Integration UI"
                )
            else:
                self._state = self._data[self._options[self._cond]]
                self._measurement = measure_format(self._options[self._cond])

            # Set attributes for legacy sensors
            for key, value in self._data.items():
                if key != "uuid":
                    self._attr[key] = value

    @property
    def unique_id(self):
        """Return a unique ID to use for this sensor."""
        if self._attribute_key:
            return "{}_{}_{}_{}_{}_{}".format(
                self.config["host"].replace(".", "_"), self._entry_id, self._cond, self._obj, "attr", self._attribute_key
            )
        else:
            return "{}_{}_{}_{}".format(
                self.config["host"].replace(".", "_"), self._entry_id, self._cond, self._obj
            )

    @property
    def should_poll(self):
        """Return the name of the sensor."""
        return True

    # friendly_name 由「设备名 + 自身短名」拼出，避免实体名里重复设备/vm 名
    _attr_has_entity_name = True

    @property
    def name(self):
        """Return the short entity name.

        所有实体都设了 has_entity_name，最终 friendly_name = 设备名 + 这里返回的短名，
        所以这里绝不能再带上设备名/虚拟机名，否则会出现
        "虚拟机 HA HA CPU使用率" 这种重复前缀。
        """
        if self._attribute_key:
            if self._cond == "vm":
                return VM_ATTR_CN.get(self._attribute_key) or capwords(
                    self._attribute_key.replace("_", " ")
                )
            if self._cond == "vmhost":
                return HOST_ATTR_CN.get(self._attribute_key) or capwords(
                    self._attribute_key.replace("_", " ")
                )
            return (
                f"{capwords(self._cond)} "
                f"{capwords(self._attribute_key.replace('_', ' '))}"
            )

        if self._cond == "datastore":
            return f"数据存储 {self._obj.replace('_', ' ').title()}"
        if self._cond == "license":
            if self._obj == "vcenter_license":
                return "vCenter 许可证"
            return "许可证"
        return f"{capwords(self._cond)} {self._obj.replace('_', ' ').title()}"

    @property
    def icon(self):
        """Return a per-attribute icon（写错会空白，见 VM_ATTR_ICON 注释）."""
        if self._attribute_key:
            if self._cond == "vm":
                return VM_ATTR_ICON.get(self._attribute_key) or "mdi:information-outline"
            if self._cond == "vmhost":
                return HOST_ATTR_ICON.get(self._attribute_key) or "mdi:information-outline"
            return "mdi:information-outline"
        # 非「逐属性」实体：数据存储 / 许可证
        if self._cond == "datastore":
            return "mdi:database"
        if self._cond == "license":
            return "mdi:license"
        return "mdi:information-outline"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._measurement

    def _host_key(self):
        """Return the identifier key of this entry's ESXi host."""
        try:
            return next(iter(self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"]))
        except (KeyError, TypeError, StopIteration):
            return self.config.get("host", "unknown")

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._attr

    @property
    def device_info(self):
        """Return device info for this sensor."""
        if self._config_entry is None:
            identifier = {(DOMAIN, self.config["host"].replace(".", "_"))}
            device_name = "ESXi 统计"
            manufacturer = "VMware, Inc."
        else:
            if self._cond == "vm":
                # VM sensors go to VM device
                vm_data = self._data
                vm_name = vm_data.get("vm_name", self._obj)
                identifier = {(DOMAIN, vm_device_key(self._entry_id, self._obj))}
                device_name = f"虚拟机 {vm_name}"
                manufacturer = "VMware 虚拟机"
            elif self._cond == "vmhost":
                # 主机传感器挂到主机设备；设备名只用「ESXi 主机」，不再拼主机名，
                # 否则 friendly_name 会变成「ESXi 主机 Localhost.Localdomain CPU使用量」
                identifier = {(DOMAIN, host_device_key(self._entry_id, self._obj))}
                device_name = DEFAULT_NAME
                manufacturer = "VMware ESXi"
            elif self._cond == "license" and self._obj != "vcenter_license":
                # 许可证挂到对应主机设备
                identifier = {
                    (DOMAIN, host_device_key(self._entry_id, self._obj.replace('_license', '')))
                }
                device_name = DEFAULT_NAME
                manufacturer = "VMware ESXi"
            elif self._cond == "datastore":
                # 存储也并进主机设备，避免再多出一个「ESXi 统计」设备
                identifier = {
                    (DOMAIN, host_device_key(self._entry_id, self._host_key()))
                }
                device_name = DEFAULT_NAME
                manufacturer = "VMware ESXi"
            else:
                # Everything else stays under ESXi Control device
                identifier = {(DOMAIN, self._config_entry.entry_id)}
                device_name = "ESXi 统计"
                manufacturer = "VMware, Inc."

        return {
            "identifiers": identifier,
            "name": device_name,
            "manufacturer": manufacturer,
        }


def measure_format(input):
    """Return measurement in readable form."""
    if input in MAP_TO_MEASUREMENT.keys():
        return MAP_TO_MEASUREMENT[input]
    else:
        return capwords(input.replace("_", " "))


class ESXiEntitiesAggregateSensor(Entity):
    """把本集成的所有实体按设备聚合成一份快照。

    数据来源是实体注册表/设备注册表/状态机，不是 hass.data，
    所以看到的就是 HA 里真实生效的那些实体（含用户改名、禁用的处理）。

    设备节点的字段：
      · ``device_name`` —— 设备名（HA 里的显示名）；
      · ``kind``        —— ``host``（ESXi 主机）/ ``vm``（虚拟机）/ ``other``；
      · ``entities``    —— 该设备下的实体条目（见下）。

    每个条目的字段（刻意保持精简 —— recorder 对单条状态的属性有约 16384 字节硬上限）：
      · ``entity_id`` —— 实体 ID，实时值请**直接读这个实体**；
      · ``name``      —— **短名**（已去掉设备名前缀，设备名在同层 ``device_name`` 里）；
      · ``icon``      —— 图标（注册表手动改过的优先，其次是实体自己声明的）；
      · ``state``     —— **采集时刻**该实体的 state 字符串。

    ⚠️ ``state`` 是快照值，不是实时值：它跟着本实体的重建节拍走，也就是
    「实体聚合刷新间隔」（``CONF_AGGREGATE_INTERVAL``，默认 60 秒）——
    集成自身实体的值还另外受「数据刷新间隔」约束，两者叠加后最长滞后更久。
    所以它只适合「看一眼大概情况 / 存档 / 做概览统计」，要判断当前状态请读实体本身。

    ⚠️ 本实体的属性**不进历史库**（见 ``_unrecorded_attributes``）：快照体积本来就越过
    recorder 的 16384 字节上限，历史价值也低（它是一份"实体目录"）。
    实时读取、卡片、自动化读的是状态机里的属性，完全不受影响。

    ⚠️ 想让整个实体连**状态行**都不写数据库，集成侧没有这个能力（HA 未提供该接口），
    只能在用户侧配置：``recorder: exclude: entities: [sensor.esxi_all_entities]``。
    """

    #: 全部属性都不写进历史库。
    #:   · 用 ``MATCH_ALL`` 一次排除全部（官方文档明确支持，且必须是**类属性**，
    #:     写成实例属性会被忽略）；
    #:   · 以前是靠"属性超过 16384 字节 → 被 recorder 丢弃"，那样每条写入都会刷一条
    #:     "State attributes ... exceed maximum size of 16384 bytes" 警告；显式声明后
    #:     属性是"按设计不记录"，警告不再出现。
    _unrecorded_attributes = frozenset({MATCH_ALL})

    _attr_icon = "mdi:format-list-group"
    _attr_has_entity_name = True

    def __init__(self, config, entry_id, host_name, config_entry=None):
        """Initialize the aggregate sensor."""
        self.config = config
        self._entry_id = entry_id
        self._host_name = host_name or config.get("host", "unknown")
        self._config_entry = config_entry
        self._attr_unique_id = (
            f"{str(config.get('host', 'unknown')).replace('.', '_')}"
            f"_{entry_id}_entities_aggregate"
        )
        self._attr_name = "实体聚合"
        # 固定 entity_id（不受「短 entity_id」总开关影响，见 naming.AGGREGATE_ENTITY_ID）：
        #   · 首次创建时直接生效（注册表里还没有它）；
        #   · 升级前已经注册过的实体（旧 id 是 sensor.entities，关掉短 id 时是长拼音）
        #     注册表 id 优先，得靠 async_added_to_hass 里的 _async_migrate_entity_id 改名。
        self.entity_id = AGGREGATE_ENTITY_ID

        self._state = None
        self._devices = []
        self._generated_at = None
        self._size = 0
        self._perf_skipped = 0
        self._warned_size = False
        self._next_run = 0

    @property
    def state(self):
        """Return the number of aggregated entities."""
        if self._state is None:
            return None
        return self._state

    @property
    def device_info(self):
        """Attach to the same host device as the host switches."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    @property
    def extra_state_attributes(self):
        """Return the per-device snapshot."""
        attrs = {
            "total_entities": self._state or 0,
            "total_devices": len(self._devices),
            # 未进明细的 VM 实时性能传感器数量（为保护 16KB recorder 上限有意排除）
            "perf_sensors_excluded": self._perf_skipped,
            "attributes_bytes": self._size,
            "devices": self._devices,
        }
        if self._generated_at:
            attrs["generated_at"] = dt_util.as_local(self._generated_at).isoformat(
                timespec="seconds"
            )
        return attrs

    async def async_added_to_hass(self):
        """登记自己，供同步 service 立即刷新快照用。"""
        await super().async_added_to_hass()
        # 已注册过的旧 entity_id 迁移到固定 id（HA 的实体注册表优先于 __init__ 里设的值）
        self._async_migrate_entity_id()
        entry_data = self.hass.data.get(DOMAIN_DATA, {}).get(self._entry_id)
        if entry_data is not None:
            entry_data["aggregate_entity"] = self

    @callback
    def _async_migrate_entity_id(self):
        """把已注册的旧 entity_id 迁移到固定的 ``AGGREGATE_ENTITY_ID``（幂等）。

        ``__init__`` 里设置的 entity_id 只在「注册表里还没有这个 unique_id」时生效，
        升级前已经存在的实体（旧 id 为 sensor.entities / 长拼音）必须显式改名，
        否则会永远停在旧 id 上。目标 id 被别的实体占着就保持原样并告警
        （宁可 id 不统一，也不去抢别人的 id）。
        """
        current = self.entity_id
        if not current or current == AGGREGATE_ENTITY_ID:
            return
        registry = er.async_get(self.hass)
        holder = registry.async_get(AGGREGATE_ENTITY_ID)
        if holder is not None:
            _LOGGER.warning(
                "「实体聚合」的目标 entity_id %s 已被 %s 占用，保持 %s 不变",
                AGGREGATE_ENTITY_ID,
                holder.entity_id,
                current,
            )
            return
        try:
            registry.async_update_entity(current, new_entity_id=AGGREGATE_ENTITY_ID)
        except (ValueError, KeyError) as err:
            _LOGGER.warning(
                "「实体聚合」的 entity_id 迁移为 %s 失败：%s",
                AGGREGATE_ENTITY_ID,
                err,
            )
            return
        # 注册表改名会同步到活实体，这里再显式回写一次：同一生命周期内的后续代码
        #   （快照排除自身、force_refresh 写状态）要立刻拿到新 id
        self.entity_id = AGGREGATE_ENTITY_ID
        _LOGGER.info(
            "「实体聚合」的 entity_id 已固定为 %s（原 %s）；"
            "引用旧 id 的卡片 / 自动化需要同步修改",
            AGGREGATE_ENTITY_ID,
            current,
        )

    def force_refresh(self):
        """Rebuild the snapshot right now, ignoring the interval.

        同步 service 增删实体后必须立刻刷新快照，否则卡片最长要等一整个
        「实体聚合刷新间隔」才能看到变化。
        """
        self._next_run = 0
        self._collect()
        if self.entity_id:
            self.async_write_ha_state()

    def update(self):
        """Rebuild the snapshot, throttled by the configured interval."""
        now = time.monotonic()
        if self._devices and now < self._next_run:
            return
        self._next_run = now + self._aggregate_interval()
        self._collect()

    def _aggregate_interval(self):
        """Return the snapshot rebuild interval in seconds.

        取值：集成选项 CONF_AGGREGATE_INTERVAL（配置 → 实体聚合刷新间隔）。
        未配置 / 非法值回退默认 60 秒，并夹在 [MIN, MAX] 内 —— 调太小会把
        「遍历实体 + 设备注册表」变成高频开销，调太大则 devices 属性更新变慢。
        """
        # 读最新的 options：改了间隔要立刻生效，而不是等重启集成
        options = current_options(self.hass, self._config_entry)
        try:
            value = int(options.get(CONF_AGGREGATE_INTERVAL, DEFAULT_AGGREGATE_INTERVAL))
        except (TypeError, ValueError):
            value = DEFAULT_AGGREGATE_INTERVAL
        return max(MIN_AGGREGATE_INTERVAL, min(MAX_AGGREGATE_INTERVAL, value))

    def _collect(self):
        """Build the device -> entities snapshot."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        devices = {}
        perf_skipped = 0
        for reg_entry in er.async_entries_for_config_entry(ent_reg, self._entry_id):
            # 别把自己算进去
            if reg_entry.entity_id == self.entity_id:
                continue
            # 每台 VM 的 3 个实时性能传感器（129 VM ≈ 387 实体）若进明细，
            #   属性会冲破 recorder 的 16KB 上限，导致整个 devices 明细丢历史。
            #   它们是排障用的细分指标，不进聚合清单（单独仍可正常使用），
            #   只在顶层属性里报个数。
            if isinstance(reg_entry.unique_id, str) and any(
                reg_entry.unique_id.endswith(f"_attr_{perf_key}")
                for perf_key in VM_PERF_KEYS
            ):
                perf_skipped += 1
                continue
            state = self.hass.states.get(reg_entry.entity_id)
            if state is None:
                continue

            device_name = None
            device_kind = "other"
            if reg_entry.device_id:
                device = dev_reg.async_get(reg_entry.device_id)
                if device is not None:
                    device_name = device.name_by_user or device.name
                    device_kind = self._device_kind(device)

            key = reg_entry.device_id or "__no_device__"
            node = devices.setdefault(
                key,
                {
                    "device_name": device_name or "未归属设备",
                    # 设备分类：host（ESXi 主机）/ vm（虚拟机）/ other（如「ESXi 统计」）
                    #   由设备 identifier 前缀直接得出，使用方不必靠设备名或实体特征反推
                    "kind": device_kind,
                    "entities": [],
                },
            )
            node["entities"].append(
                {
                    "entity_id": state.entity_id,
                    # 短名：设备名已经在同一层（device_name），再拼一遍纯属重复占字节
                    "name": self._short_name(state, device_name),
                    # 图标：注册表里手动改过的优先，其次是实体自己声明的（attributes["icon"]）
                    "icon": reg_entry.icon or state.attributes.get("icon") or None,
                    # 采集时刻的 state（永远是字符串）。它随「实体聚合刷新间隔」更新 ——
                    #   实时值请直接读实体本身（见类 docstring 的说明）
                    "state": state.state,
                }
            )

        nodes = sorted(devices.values(), key=lambda item: str(item["device_name"]))
        for node in nodes:
            node["entities"].sort(key=lambda item: item["entity_id"])

        # 只告警，绝不再删 friendly_name
        size = self._byte_size(nodes)
        if size > AGGREGATE_SAFE_BYTES and not self._warned_size:
            self._warned_size = True
            _LOGGER.warning(
                "实体聚合快照已达 %s 字节（体检线 %s）：属性不进历史库、功能不受影响，"
                "但快照越大，重建与前端读取越重；可按 sensor.py 里 AGGREGATE_SAFE_BYTES 的"
                "注释精简，或把「实体聚合刷新间隔」调大。",
                size,
                AGGREGATE_SAFE_BYTES,
            )

        self._devices = nodes
        self._size = size
        self._perf_skipped = perf_skipped
        self._state = sum(len(node["entities"]) for node in nodes)
        self._generated_at = dt_util.utcnow()

    @staticmethod
    def _device_kind(device):
        """Return ``host`` / ``vm`` / ``other`` for a device registry entry.

        设备是本集成自己建的，identifier 就是权威分类：
        0.10 起为 ``<entry_id>__vm_<虚拟机键>`` / ``<entry_id>__host_<主机键>``
        （多 ESXi 条目同名对象隔离）；兼容 0.9 旧版 ``vm_<键>`` / ``host_<键>``。
        """
        for domain, identifier in device.identifiers or ():
            if domain != DOMAIN or not isinstance(identifier, str):
                continue
            # 顺序重要：「...vm_host_x」这种名字要先按 vm 判
            if "__vm_" in identifier or identifier.startswith("vm_"):
                return "vm"
            if "__host_" in identifier or identifier.startswith("host_"):
                return "host"
        return "other"

    @staticmethod
    def _short_name(state, device_name):
        """Return friendly_name without the device-name prefix.

        例：「虚拟机 HA CPU使用率」+ 设备名「虚拟机 HA」→「CPU使用率」。
        设备名丢不掉（同层 device_name 里已有），所以没必要在每个条目里重复一遍；
        这能省下每条约 15~25 字节，是本次把属性压回 recorder 上限内的主要来源。
        """
        full = state.attributes.get("friendly_name") or state.entity_id
        if device_name and full.startswith(device_name):
            short = full[len(device_name):].strip()
            if short:
                return short
        return full

    @staticmethod
    def _byte_size(nodes):
        """Rough serialized size of the snapshot."""
        try:
            return len(json.dumps(nodes).encode("utf-8"))
        except (TypeError, ValueError):
            return 0
