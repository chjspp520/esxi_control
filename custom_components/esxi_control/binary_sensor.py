"""Binary sensor platform for ESXi Control integration.

主机侧的「是 / 否」状态统一放这里，而不是塞进 sensor：
  · 维护模式（on = 主机处于维护模式）
  · 锁定模式（on = 已启用；adminDisabled 为 None 的老版本/独立主机不建实体）
  · 受管服务运行状态（SSH / ESXi Shell / NTP 守护进程，见 MANAGED_SERVICES）

与 switch 平台的分工：这里**只读**报告当前状态；真正去启停服务 / 切换维护模式
的可写控件在 switch 平台。
"""
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import DEFAULT_NAME, DOMAIN, DOMAIN_DATA, MANAGED_SERVICES
from .entity_sync import dedupe_entities, register_platform
from .guest_store import get_manager
from .vm_bulk import host_device_key, vm_device_key

SCAN_INTERVAL = timedelta(seconds=15)

_LOGGER = logging.getLogger(__name__)


def _build_entities(hass, config_entry):
    """Return every binary sensor this entry should currently have.

    纯函数：只 new 实体，不 add、不改 hass.data（entity_sync 会反复调用）。
    """
    config = config_entry.data
    entry_data = hass.data[DOMAIN_DATA][config_entry.entry_id]
    entities = []

    if "vmhost" in entry_data["monitored_conditions"]:
        for host_key in entry_data.get("vmhost") or {}:
            # 实体一旦构建就长期保留（主机关机时 available=False 置灰），
            #   避免「关机 → 同步 → 实体被删 → 开机后历史断掉」的抖动
            entities.append(ESXiMaintenanceBinarySensor(hass, config, host_key, config_entry))

            # 锁定模式：该主机数据里 adminDisabled 为 None（信息不可用，常见于
            #   5.5 独立主机）时不建，避免出现一个永远未知的无意义状态实体
            host_data = entry_data["vmhost"][host_key]
            if host_data.get("lockdown") is not None:
                entities.append(ESXiLockdownBinarySensor(hass, config, host_key, config_entry))

            # 受管服务：每类一个运行状态二进传感器（明细全量在「服务状态」诊断传感器里）
            for service_key in MANAGED_SERVICES:
                entities.append(
                    ESXiServiceBinarySensor(
                        hass, config, host_key, service_key, config_entry
                    )
                )

    # 已配置来宾凭据的虚拟机：每台一个「来宾凭据有效」二进传感器
    #   （没配置的 VM 不建，避免设备页出现一堆永远未知的实体）
    if "vm" in entry_data.get("monitored_conditions", []):
        manager = get_manager(hass)
        configured = manager.configured_vms(config_entry.entry_id) if manager else set()
        for vm_key in entry_data.get("vm") or {}:
            if vm_key in configured:
                entities.append(
                    ESXiVMGuestCredentialBinarySensor(
                        hass, config, vm_key, config_entry
                    )
                )
            # VM 问题状态（启动/关机卡问题、待合并磁盘）与光盘挂载：长期只读诊断
            entities.append(
                ESXiVMIssueBinarySensor(hass, config, vm_key, config_entry)
            )
            entities.append(
                ESXiVMCdromBinarySensor(hass, config, vm_key, config_entry)
            )

    return dedupe_entities(entities)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up binary_sensor platform."""
    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(
        hass, config_entry.entry_id, "binary_sensor", _build_entities, async_add_entities
    )


class _HostBinarySensorBase(BinarySensorEntity):
    """主机二进传感器公共部分：轮询、设备归属、可用性。"""

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_key, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._host_key = host_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}

    def update(self):
        """Keep this host's snapshot（纯快照；数据由中央定时器统一拉取）。"""
        self._host_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vmhost", {})
            .get(self._host_key)
            or {}
        )

    @property
    def should_poll(self):
        """Polled entity."""
        return True

    @property
    def available(self):
        """Available while the host is powered on and present in data."""
        return self._host_data.get("state") == "poweredOn"

    @property
    def device_info(self):
        """Attach to the ESXi host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_key))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware ESXi",
            "model": "ESXi 主机",
        }


class ESXiMaintenanceBinarySensor(_HostBinarySensorBase):
    """主机维护模式：on = 正在维护。"""

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_bmaintenance_{self._host_key}"
        )

    @property
    def name(self):
        """Short name."""
        return "维护模式"

    @property
    def is_on(self):
        """True when host is in maintenance mode."""
        value = self._host_data.get("maintenance_mode")
        return bool(value) if value != "N/A" else False

    @property
    def icon(self):
        """Icon reflects state."""
        return "mdi:wrench-check" if self.is_on else "mdi:wrench-outline"


class ESXiLockdownBinarySensor(_HostBinarySensorBase):
    """主机锁定模式：on = 已锁定（DCUI 之外的本地管理被禁止）。"""

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_blockdown_{self._host_key}"
        )

    @property
    def name(self):
        """Short name."""
        return "锁定模式"

    @property
    def is_on(self):
        """True when lockdown is enabled."""
        return bool(self._host_data.get("lockdown"))

    @property
    def icon(self):
        """Icon reflects state."""
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant-outline"


class ESXiServiceBinarySensor(_HostBinarySensorBase):
    """单个受管服务的运行状态（running 类）。"""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, config, host_key, service_key, config_entry):
        """Initialize with the managed-service key (TSM-SSH / TSM / ntpd)."""
        super().__init__(hass, config, host_key, config_entry)
        self._service_key = service_key

    def _service_entry(self):
        """Find this service's snapshot inside host_data['services']."""
        for item in self._host_data.get("services") or []:
            if item.get("key") == self._service_key:
                return item
        return None

    @property
    def unique_id(self):
        """Return a unique ID."""
        slug, _, _ = MANAGED_SERVICES[self._service_key]
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_bservice_{slug}_{self._host_key}"
        )

    @property
    def name(self):
        """Short name = service label."""
        _, label, _ = MANAGED_SERVICES[self._service_key]
        return label

    @property
    def is_on(self):
        """True when the service is running."""
        entry = self._service_entry()
        return bool(entry and entry.get("running"))

    @property
    def available(self):
        """Only available when the service exists in the service list."""
        return super().available and self._service_entry() is not None

    @property
    def extra_state_attributes(self):
        """Expose startup policy (on/off/automatic) for quick inspection."""
        entry = self._service_entry()
        if not entry:
            return {}
        policy_cn = {"on": "自动启动", "off": "手动/已禁用", "automatic": "按需自动"}
        return {
            "service_key": entry.get("key"),
            "policy": entry.get("policy"),
            "policy_cn": policy_cn.get(entry.get("policy"), entry.get("policy")),
        }

    @property
    def icon(self):
        """Service icon from MANAGED_SERVICES."""
        _, _, icon = MANAGED_SERVICES[self._service_key]
        return icon


class ESXiVMGuestCredentialBinarySensor(BinarySensorEntity):
    """虚拟机来宾凭据：on = 保存的客户机账号密码校验通过。

    校验是重操作（经 Tools 打客户机），不由本实体轮询触发：结果来自
    GuestCredentialManager 的缓存/后台扫描/「验证来宾凭据」按钮。
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, config, vm_key, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_key = vm_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}
        self._result = None
        self._subscribed = False

    def update(self):
        """只读快照 + 读缓存，绝不在这里发起远程校验。"""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_key)
            or {}
        )
        manager = get_manager(self.hass)
        if manager is not None:
            self._result = manager.cached_result(self._entry_id, self._vm_key)

    async def async_added_to_hass(self):
        """订阅结果推送；首次没有缓存时排队校验一次。"""
        manager = get_manager(self.hass)
        if manager is None:
            return
        manager.register_listener(self._entry_id, self._vm_key, self)
        self._subscribed = True
        if manager.cached_result(self._entry_id, self._vm_key) is None:
            self.hass.async_create_task(
                manager.async_validate(self._entry_id, self._vm_key)
            )

    async def async_will_remove_from_hass(self):
        """Unsubscribe."""
        if self._subscribed:
            manager = get_manager(self.hass)
            if manager is not None:
                manager.unregister_listener(self._entry_id, self._vm_key, self)
            self._subscribed = False

    @callback
    def async_set_guest_result(self, result):
        """由管理器在校验完成后回调（事件循环线程，同步执行）。"""
        self._result = result
        self.async_schedule_update_ha_state()

    @property
    def should_poll(self):
        """Polled for availability snapshot."""
        return True

    @property
    def available(self):
        """VM 运行中才谈得上校验凭据。"""
        return self._vm_data.get("state") == "running"

    @property
    def name(self):
        """Short name."""
        return "来宾凭据"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_bguest_{self._vm_key}"
        )

    @property
    def device_info(self):
        """Attach to the VM device."""
        vm_name = self._vm_data.get("vm_name", self._vm_key)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_key))},
            "name": f"虚拟机 {vm_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def is_on(self):
        """True only when last validation succeeded."""
        return bool(self._result and self._result.get("ok"))

    @property
    def icon(self):
        """Icon reflects validation state."""
        return "mdi:account-key" if self.is_on else "mdi:account-key-outline"

    @property
    def extra_state_attributes(self):
        """Show channel / username / last validation time / failure reason (never password)."""
        manager = get_manager(self.hass)
        record = manager.get_cred(self._entry_id, self._vm_key) if manager else None
        tools = (record or {}).get("tools") or {}
        ssh = (record or {}).get("ssh") or {}
        try:
            channel = manager.resolved_transport(self._entry_id, self._vm_key)
        except Exception:  # pylint: disable=broad-except
            channel = "tools"
        attrs = {"channel": channel}
        if channel == "ssh":
            attrs["username"] = ssh.get("username") or None
            attrs["host"] = ssh.get("host") or None
            attrs["port"] = ssh.get("port") or 22
        else:
            attrs["username"] = tools.get("username") or None
            attrs["domain"] = tools.get("domain") or None
        if self._result:
            import time as _time

            attrs["validated_at"] = _time.strftime(
                "%Y-%m-%d %H:%M:%S", _time.localtime(self._result.get("ts", 0))
            )
            if not self._result.get("ok"):
                attrs["last_error"] = self._result.get("error")
        return attrs


class _VMStateBinarySensorBase(BinarySensorEntity):
    """VM 二进传感器公共部分：轮询读快照 + 挂 VM 设备。"""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_key, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_key = vm_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Refresh VM snapshot."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_key)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """Snapshot present."""
        return bool(self._vm_data)

    @property
    def device_info(self):
        """Attach to VM device."""
        vm_name = self._vm_data.get("vm_name", self._vm_key)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_key))},
            "name": f"虚拟机 {vm_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }


class ESXiVMIssueBinarySensor(_VMStateBinarySensorBase):
    """虚拟机问题状态：on = 存在待回答提问（VM 被卡住）或磁盘待合并。

    两类问题合并成一个告警实体，属性里区分具体原因；处理动作分别是
    在 MOB/控制台回答提问，以及点「合并磁盘」按钮（consolidate_needed 时可用）。
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_bissue_{self._vm_key}"
        )

    @property
    def name(self):
        """Short name."""
        return "问题状态"

    @property
    def is_on(self):
        """Question pending or consolidation needed."""
        return bool(self._vm_data.get("question")) or bool(
            self._vm_data.get("consolidation_needed")
        )

    @property
    def icon(self):
        """Icon."""
        return "mdi:alert-octagon" if self.is_on else "mdi:check-circle-outline"

    @property
    def extra_state_attributes(self):
        """Question text/choices + consolidation flag（state ≤255，明细放属性）。"""
        attrs = {
            "consolidation_needed": bool(
                self._vm_data.get("consolidation_needed")
            ),
        }
        question = self._vm_data.get("question")
        if question:
            attrs["question_id"] = question.get("id")
            attrs["question_text"] = question.get("text")
            choices = question.get("choices") or []
            # 属性保持精简：只给「键=标签」平面映射，供自动化/排障读取
            attrs["question_choices"] = {
                c.get("key"): c.get("label") for c in choices if c.get("key")
            }
        return attrs


class ESXiVMCdromBinarySensor(_VMStateBinarySensorBase):
    """光盘挂载状态：on = 光驱挂着 ISO 镜像（IsoBacking）。

    物理光驱 / 客户端设备不算挂载。弹出动作见「弹出光盘」按钮；
    ISO 完整路径、连接状态放在属性里。
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_bcdrom_{self._vm_key}"
        )

    @property
    def name(self):
        """Short name."""
        return "光盘"

    def _iso_cdroms(self):
        """Return mounted ISO cdrom snapshot entries."""
        return [
            cd
            for cd in self._vm_data.get("cdroms") or []
            if cd.get("backing") == "iso"
        ]

    @property
    def is_on(self):
        """An ISO backing exists."""
        return bool(self._iso_cdroms())

    @property
    def icon(self):
        """Icon."""
        return "mdi:disc-alert" if self.is_on else "mdi:disc"

    @property
    def extra_state_attributes(self):
        """ISO path / connected / 驱动器总数。"""
        isos = self._iso_cdroms()
        if not isos:
            return {"cdrom_count": len(self._vm_data.get("cdroms") or [])}
        cd = isos[0]
        return {
            "iso": cd.get("iso"),
            "connected": bool(cd.get("connected")),
            "start_connected": bool(cd.get("start_connected")),
            "label": cd.get("label"),
            "cdrom_count": len(self._vm_data.get("cdroms") or []),
        }
