"""Button platform for ESXi Control integration."""
import logging
from datetime import datetime
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .const import (
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_NAME,
    MANAGED_SERVICES,
)
from .entity_sync import async_sync_entry, dedupe_entities, register_platform
from .guest_store import get_manager
from .esxi import (
    host_pwr,
    host_service,
    host_refresh_services,
    vm_pwr,
    vm_snap_take,
    vm_snap_remove,
    vm_guest_action,
    vm_consolidate,
)
from .cdrom_ops import eject_iso
from .vm_bulk import (
    BatchEntityMixin,
    call_all,
    host_device_info,
    host_device_key,
    vm_device_key,
    refresh_data_now,
    run_batch,
    vm_entity_ids,
)

_LOGGER = logging.getLogger(__name__)


def _build_entities(hass, config_entry):
    """Return every button entity this entry should currently have.

    同步 service（entity_sync）会反复调用它来算「此刻应该存在的实体」，
    所以必须是纯函数：只返回对象，不添加实体、不改 hass.data。
    """
    config = config_entry.data
    entry_id = config_entry.entry_id
    entry_data = hass.data[DOMAIN_DATA][entry_id]
    buttons = []

    # 全局静态：同步按钮（不随对象增减变化，但同步 service 需要它在清单里才不会被误删）
    buttons.append(ESXiSyncButton(hass, config_entry))

    # 全局静态：整机（全部虚拟机）批量动作，挂在主机设备下
    if "vm" in entry_data["monitored_conditions"]:
        buttons.append(ESXiAllVMsSnapshotButton(hass, config_entry))
        buttons.append(ESXiAllVMsRestartButton(hass, config_entry))
        buttons.append(ESXiAllVMsForceRestartButton(hass, config_entry))
        # 优雅关机（ShutdownGuest，只对装了 Tools 的运行中 VM 生效）
        buttons.append(ESXiAllVMsShutdownButton(hass, config_entry))
        # 硬关闭电源（PowerOffVM，断电语义，逃生通道）
        buttons.append(ESXiAllVMsPowerOffButton(hass, config_entry))

    # Create host buttons
    if "vmhost" in entry_data["monitored_conditions"]:
        for host_name in entry_data["vmhost"]:
            buttons.append(ESXiHostRebootButton(hass, config, host_name, config_entry))
            # 受管服务各自一个「重启」按钮 + 一个聚合的「刷新服务清单」按钮；
            #   长期实体，主机关机时由 available 自动置灰
            for service_key in MANAGED_SERVICES:
                buttons.append(
                    ESXiHostServiceRestartButton(
                        hass, config, host_name, service_key, config_entry
                    )
                )
            buttons.append(
                ESXiHostRefreshServicesButton(hass, config, host_name, config_entry)
            )

    # Create VM buttons
    if "vm" in entry_data["monitored_conditions"]:
        for vm_name in entry_data["vm"]:
            buttons.append(ESXiVMRebootButton(hass, config, vm_name, config_entry))
            # 强制重启（硬重置）：与「重启」互补，客户机卡死时的逃生通道
            buttons.append(ESXiVMForceRestartButton(hass, config, vm_name, config_entry))
            # 关机（客户机优雅关机 ShutdownGuest，需 Tools 在线）
            buttons.append(ESXiVMShutdownButton(hass, config, vm_name, config_entry))
            # 关闭电源（PowerOffVM，直接断电，Tools 失效时的兜底）
            buttons.append(ESXiVMPowerOffButton(hass, config, vm_name, config_entry))
            # 合并快照遗留磁盘（仅 consolidationNeeded 时可用）
            buttons.append(ESXiVMConsolidateButton(hass, config, vm_name, config_entry))
            # 弹出光驱中的 ISO（不断电热操作，只弹出不删文件）
            buttons.append(ESXiVMCdromEjectButton(hass, config, vm_name, config_entry))
            # 客户机待机（需 Tools + 运行中）
            buttons.append(ESXiVMStandbyButton(hass, config, vm_name, config_entry))
            # 挂起到内存（仅运行中可用）/ 从挂起唤醒（仅挂起中可用）
            buttons.append(ESXiVMSuspendButton(hass, config, vm_name, config_entry))
            buttons.append(ESXiVMWakeButton(hass, config, vm_name, config_entry))
            # 还原到当前快照（有快照才可用）
            buttons.append(ESXiVMSnapshotRevertButton(hass, config, vm_name, config_entry))
            # 升级 VMware Tools（诊断/配置类动作）
            buttons.append(ESXiVMToolsUpgradeButton(hass, config, vm_name, config_entry))
            # Add snapshot buttons for each VM
            buttons.append(ESXiVMSnapshotCreateButton(hass, config, vm_name, config_entry))
            buttons.append(ESXiVMSnapshotRemoveAllButton(hass, config, vm_name, config_entry))
            buttons.append(ESXiVMSnapshotRemoveFirstButton(hass, config, vm_name, config_entry))
            buttons.append(ESXiVMSnapshotRemoveLastButton(hass, config, vm_name, config_entry))

        # 「验证来宾凭据」：仅在该 VM 配置过来宾账号密码时出现
        manager = get_manager(hass)
        configured = manager.configured_vms(entry_id) if manager else set()
        for vm_name in entry_data["vm"]:
            if vm_name in configured:
                buttons.append(
                    ESXiVMGuestValidateButton(hass, config, vm_name, config_entry)
                )

    return dedupe_entities(buttons)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up button platform."""
    entry_id = config_entry.entry_id
    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(hass, entry_id, "button", _build_entities, async_add_entities)


class ESXiSyncButton(ButtonEntity):
    """触发一次「实体同步」：重新扫描 ESXi 并增删实体。

    对应 ``esxi_control.sync`` 服务。做成实体是为了在 HA 的实体列表 / 仪表盘上
    有一个可直接点击的入口（也方便写自动化）。按下即执行，无开关状态。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:sync"
    _attr_name = "同步实体"

    def __init__(self, hass, config_entry):
        """Initialize the sync button."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host = config_entry.data.get("host", "unknown")

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return f"{self._host.replace('.', '_')}_{self._entry_id}_sync_button"

    @property
    def device_info(self):
        """挂到主机设备上（与其它主机实体同设备，不额外造设备）。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_press(self):
        """执行同步（与 esxi_control.sync 服务同一个内核）。"""
        _LOGGER.info("用户点击了「同步实体」按钮，开始同步 ESXi 实体")
        result = await async_sync_entry(self.hass, self._config_entry)
        _LOGGER.info(
            "同步按钮执行完成：新增 %s 个实体、移除 %s 个实体%s",
            result.get("added", 0),
            result.get("removed", 0),
            f"（{result['reason']}）" if result.get("reason") else "",
        )


class ESXiAllVMsSnapshotButton(BatchEntityMixin, ButtonEntity):
    """给所有虚拟机各打一个快照（挂在主机设备下）。

    这是**一次性动作**，所以做成按钮而不是开关 —— 开关表达的是持续状态，
    表达不了「按一下执行一次」。逐台复用单台的「创建快照」按钮。
    """

    _attr_has_entity_name = True
    _attr_name = "全部虚拟机快照"
    _attr_icon = "mdi:camera-plus"

    def __init__(self, hass, config_entry):
        """Initialize the bulk snapshot button."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_snapshot"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_press(self):
        """对全部虚拟机创建快照（按「批量分组」参数分批下发）。"""
        if not self._start_batch(self._run_batch()):
            return

    async def _run_batch(self):
        """实际下发：分组 + 组间间隔，全过程写入操作日志。"""
        entity_ids = vm_entity_ids(
            self.hass, self._config_entry, "button", "vm_snapshot_create"
        )
        await run_batch(
            self.hass,
            self._config_entry,
            entity_ids,
            operation="全部快照",
            operation_key="snapshot",
            domain="button",
            service="press",
        )


class ESXiAllVMsRestartButton(BatchEntityMixin, ButtonEntity):
    """重启所有虚拟机（挂在主机设备下）。

    同样是**一次性动作**。逐台复用单台的「重启」按钮 —— 那个按钮自己会跳过
    未运行的虚拟机，所以这里不必预先过滤。
    """

    _attr_has_entity_name = True
    _attr_name = "全部虚拟机重启"
    _attr_icon = "mdi:restart"

    def __init__(self, hass, config_entry):
        """Initialize the bulk restart button."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_restart"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_press(self):
        """重启全部虚拟机（按「批量分组」参数分批下发）。"""
        if not self._start_batch(self._run_batch()):
            return

    async def _run_batch(self):
        """实际下发：分组 + 组间间隔，全过程写入操作日志。"""
        entity_ids = vm_entity_ids(
            self.hass, self._config_entry, "button", "vm_reboot"
        )
        await run_batch(
            self.hass,
            self._config_entry,
            entity_ids,
            operation="全部重启",
            operation_key="reboot",
            domain="button",
            service="press",
        )


class ESXiAllVMsForceRestartButton(BatchEntityMixin, ButtonEntity):
    """强制重启所有虚拟机（硬重置；按「批量分组」参数分批下发）。

    ⚠️ 这是本集成**最危险**的批量操作：对每台虚拟机都是**断电式重启**，
    不会走客户机 OS 的正常关机流程。只对正在运行的虚拟机生效
    （已关机的会被 vm_pwr 侧跳过并记日志）。
    """

    _attr_has_entity_name = True
    _attr_name = "全部强制重启"
    _attr_icon = "mdi:restart-alert"

    def __init__(self, hass, config_entry):
        """Initialize the bulk force-restart button."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_force_restart"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_press(self):
        """强制重启全部虚拟机（按「批量分组」参数分批下发）。"""
        if not self._start_batch(self._run_batch()):
            return

    async def _run_batch(self):
        """实际下发：分组 + 组间间隔，全过程写入操作日志。"""
        entity_ids = vm_entity_ids(
            self.hass, self._config_entry, "button", "vm_force_restart"
        )
        await run_batch(
            self.hass,
            self._config_entry,
            entity_ids,
            operation="全部强制重启",
            operation_key="force_restart",
            domain="button",
            service="press",
        )


class ESXiHostRebootButton(ButtonEntity):
    """ESXi Host Reboot Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._host_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"][self._host_name]
        except KeyError:
            _LOGGER.debug("Host %s 暂时不在数据里（可能在刷新，或已被删除）", self._host_name)
            self._host_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "重启"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_host_reboot_{self._host_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Only available if host is powered on
        if not self._host_data:
            return False
        host_state = self._host_data.get("state", "unknown")
        return host_state == "poweredOn"

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._host_data:
            # Only include reboot-related attributes that aren't available as individual sensors
            attrs_map = {
                "power_state": "state",  # Power state is relevant for reboot operations
                "maintenance_mode": "maintenance_mode",  # Important for reboot safety
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._host_data:
                    attrs[attr_name] = self._host_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            # Check if host is available and powered on
            if not self.available:
                _LOGGER.warning(
                    "Cannot reboot host %s: host is not powered on or unavailable",
                    self._host_name
                )
                return

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            # Use the original host name from stored data for exact matching
            target_host = self._host_data.get("original_name", self._host_name)
            if not target_host:
                _LOGGER.error("Cannot determine target host name for %s", self._host_name)
                return

            # Use reboot command without force - safer approach
            await self.hass.async_add_executor_job(
                host_pwr,
                self.hass,
                target_host,
                "reboot",
                conn_details,
                False,  # force=False - user should manually set maintenance mode first
                True    # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to reboot host %s: %s", self._host_name, e)

    @property
    def icon(self):
        """Return the icon for the button."""
        return "mdi:restart"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiVMRebootButton(ButtonEntity):
    """ESXi VM Reboot Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][self._vm_name]
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "重启"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_reboot_{self._vm_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Only available if VM is powered on
        if not self._vm_data:
            return False
        vm_state = self._vm_data.get("state", "unknown")
        return vm_state == "running"

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            # Only include reboot-related attributes that aren't available as individual sensors
            attrs_map = {
                "power_state": "state",  # Power state is relevant for reboot operations
                "tools_status": "tools_status",  # Important for reboot method selection
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            # Check if VM is available and powered on
            if not self.available:
                _LOGGER.warning(
                    "Cannot reboot VM %s: VM is not powered on or unavailable",
                    self._vm_name
                )
                return

            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error("Cannot reboot VM %s: UUID not found", self._vm_name)
                return

            # Determine reboot method based on VMware Tools status
            tools_status = self._vm_data.get("tools_status", "").lower()

            # Use graceful reboot if VMware Tools are available and functional
            # toolsOk = current and running, toolsOld = old version but functional
            if tools_status in ["toolsok", "toolsold"]:
                reboot_command = "reboot"
                reboot_method = f"graceful reboot (VMware Tools: {tools_status})"
            else:
                reboot_command = "reset"
                reboot_method = f"hard reset (VMware Tools: {tools_status})"

            _LOGGER.info("VM %s: Using %s", self._vm_name, reboot_method)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            await self.hass.async_add_executor_job(
                vm_pwr,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                reboot_command,
                conn_details,
                False  # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to reboot VM %s: %s", self._vm_name, e)

    @property
    def icon(self):
        """Return the icon for the button."""
        return "mdi:restart"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiVMForceRestartButton(ButtonEntity):
    """强制重启（硬重置）：相当于按机箱上的 reset 键。

    与「重启」的分工：

    * 「重启」在装了 VMware Tools 时走**客户机优雅重启**（RebootGuest）；
    * 本按钮**一律**走 ``ResetVM_Task`` —— hypervisor 直接重置虚拟机执行，
      不依赖 Tools，专门用于**客户机 OS 卡死 / Tools 无响应**的逃生场景。

    ⚠️ 硬重置等同断电重启：未刷盘的数据会丢失，文件系统可能需要自检，
    不应作为日常重启的替代品。
    """

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"].get(
                self._vm_name, {}
            )
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "强制重启"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}"
            f"_{self._entry_id}_vm_force_restart_{self._vm_name}"
        )

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """只在虚拟机运行时可用（关机的机器不需要重置）。"""
        return self._vm_data.get("state") == "running"

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            attrs_map = {
                "power_state": "state",
                "tools_status": "tools_status",  # 便于判断"热重启是否可行"
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    @property
    def icon(self):
        """Return the icon for the button（与「重启」明显区分）."""
        return "mdi:restart-alert"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button

    async def async_press(self, **kwargs):
        """执行强制重启（硬重置，不看 VMware Tools）。"""
        try:
            if not self.available:
                _LOGGER.warning(
                    "无法强制重启虚拟机 %s：它不在运行状态", self._vm_name
                )
                return

            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error(
                    "Cannot force restart VM %s: UUID not found", self._vm_name
                )
                return

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            _LOGGER.info(
                "虚拟机 %s：执行强制重启（硬重置，不走客户机 OS 流程）", self._vm_name
            )
            await self.hass.async_add_executor_job(
                vm_pwr,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                "reset",
                conn_details,
                False,  # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.error("Failed to force restart VM %s: %s", self._vm_name, e)


class ESXiVMSnapshotCreateButton(ButtonEntity):
    """ESXi VM Create Snapshot Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][self._vm_name]
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "创建快照"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_snapshot_create_{self._vm_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Available regardless of VM power state
        return self._vm_data is not None and "state" in self._vm_data

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            # Include snapshot-related attributes
            attrs_map = {
                "power_state": "state",
                "snapshots": "snapshots",
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error("Cannot create snapshot for VM %s: UUID not found", self._vm_name)
                return

            vm_proper_name = self._vm_data.get("vm_name", self._vm_name)

            # Generate snapshot name with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap_name = f"HA_Snapshot_{timestamp}"
            description = f"Snapshot created by Home Assistant on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            _LOGGER.info("Creating snapshot '%s' for VM %s", snap_name, vm_proper_name)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            await self.hass.async_add_executor_job(
                vm_snap_take,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                snap_name,
                description,
                False,  # memory - don't include memory in snapshot
                True,   # quiesce - quiesce file system if VMware Tools available
                conn_details,
                True    # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to create snapshot for VM %s: %s", self._vm_name, e)

    @property
    def icon(self):
        """Return the icon for the button（与「截图开关」的 mdi:camera 区分开）."""
        return "mdi:camera-plus"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiVMSnapshotRemoveAllButton(ButtonEntity):
    """ESXi VM Remove All Snapshots Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][self._vm_name]
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "删除所有快照"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_snapshot_remove_all_{self._vm_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Only available if VM has snapshots
        if not self._vm_data:
            return False
        snapshots = self._vm_data.get("snapshots", 0)
        return snapshots > 0

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            attrs_map = {
                "snapshots": "snapshots",
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            # Check if VM has snapshots
            if not self.available:
                _LOGGER.warning("Cannot remove snapshots for VM %s: no snapshots available", self._vm_name)
                return

            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error("Cannot remove snapshots for VM %s: UUID not found", self._vm_name)
                return

            vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
            _LOGGER.info("Removing all snapshots for VM %s", vm_proper_name)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            await self.hass.async_add_executor_job(
                vm_snap_remove,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                "all",
                conn_details,
                True    # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to remove all snapshots for VM %s: %s", self._vm_name, e)

    @property
    def icon(self):
        """Return the icon for the button."""
        return "mdi:delete-sweep"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiVMSnapshotRemoveFirstButton(ButtonEntity):
    """ESXi VM Remove First Snapshot Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][self._vm_name]
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "删除第一个快照"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_snapshot_remove_first_{self._vm_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Only available if VM has snapshots
        if not self._vm_data:
            return False
        snapshots = self._vm_data.get("snapshots", 0)
        return snapshots > 0

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            attrs_map = {
                "snapshots": "snapshots",
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            # Check if VM has snapshots
            if not self.available:
                _LOGGER.warning("Cannot remove first snapshot for VM %s: no snapshots available", self._vm_name)
                return

            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error("Cannot remove first snapshot for VM %s: UUID not found", self._vm_name)
                return

            vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
            _LOGGER.info("Removing first snapshot for VM %s", vm_proper_name)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            await self.hass.async_add_executor_job(
                vm_snap_remove,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                "first",
                conn_details,
                True    # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to remove first snapshot for VM %s: %s", self._vm_name, e)

    @property
    def icon(self):
        """Return the icon for the button."""
        return "mdi:delete-outline"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiVMSnapshotRemoveLastButton(ButtonEntity):
    """ESXi VM Remove Last Snapshot Button."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Update the button state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][self._vm_name]
        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}

    @property
    def name(self):
        """Return the name of the button."""
        return "删除最后一个快照"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_snapshot_remove_last_{self._vm_name}"

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        # Only available if VM has snapshots
        if not self._vm_data:
            return False
        snapshots = self._vm_data.get("snapshots", 0)
        return snapshots > 0

    @property
    def device_info(self):
        """Return device information."""
        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_proper_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {}
        if self._vm_data:
            attrs_map = {
                "snapshots": "snapshots",
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_press(self, **kwargs):
        """Handle the button press."""
        try:
            # Check if VM has snapshots
            if not self.available:
                _LOGGER.warning("Cannot remove last snapshot for VM %s: no snapshots available", self._vm_name)
                return

            vm_uuid = self._vm_data.get("uuid")
            if not vm_uuid:
                _LOGGER.error("Cannot remove last snapshot for VM %s: UUID not found", self._vm_name)
                return

            vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
            _LOGGER.info("Removing last snapshot for VM %s", vm_proper_name)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            await self.hass.async_add_executor_job(
                vm_snap_remove,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                "last",
                conn_details,
                True    # notify
            )

            # Request immediate update
            await self.hass.async_add_executor_job(self.update)

        except Exception as e:
            _LOGGER.error("Failed to remove last snapshot for VM %s: %s", self._vm_name, e)

    @property
    def icon(self):
        """Return the icon for the button."""
        return "mdi:delete"

    @property
    def entity_category(self):
        """Return the entity category."""
        return None  # This is a control button, not a config button


class ESXiHostServiceRestartButton(ButtonEntity):
    """重启单个受管服务（SSH / ESXi Shell / NTP）。

    服务改完配置 / 状态可疑时用得上。开关只能 start/stop，「重启」是独立语义
    （RestartService 原子完成），所以单独做按钮。
    """

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_name, service_key, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._service_key = service_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}

    def update(self):
        """Refresh host snapshot（纯快照；数据由中央定时器统一拉取）。"""
        self._host_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vmhost", {})
            .get(self._host_name)
            or {}
        )

    def _service_entry(self):
        """Find service snapshot."""
        for item in self._host_data.get("services") or []:
            if item.get("key") == self._service_key:
                return item
        return None

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """Host online + service exists."""
        return self._host_data.get("state") == "poweredOn" and self._service_entry() is not None

    @property
    def name(self):
        """Short name = 重启 + 服务名."""
        _, label, _ = MANAGED_SERVICES[self._service_key]
        return f"重启{label}"

    @property
    def unique_id(self):
        """Return a unique ID."""
        slug, _, _ = MANAGED_SERVICES[self._service_key]
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_service_restart_{slug}_{self._host_name}"
        )

    @property
    def icon(self):
        """Service icon."""
        _, _, icon = MANAGED_SERVICES[self._service_key]
        return icon

    @property
    def device_info(self):
        """Attach to host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    async def async_press(self):
        """Restart the service, then force a data refresh."""
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        target_host = self._host_data.get("original_name", self._host_name)
        ok = await self.hass.async_add_executor_job(
            host_service, conn_details, target_host, self._service_key, "restart"
        )
        if ok:
            await refresh_data_now(self.hass, self._entry_id)
            self.async_schedule_update_ha_state(force_refresh=True)
        else:
            _LOGGER.error("重启服务 %s 失败（主机 %s）", self._service_key, self._host_name)


class ESXiHostRefreshServicesButton(ButtonEntity):
    """刷新主机服务清单（serviceSystem.RefreshServices）。

    在 ESXi Shell 里手工改过服务、或刚安装了新的主机守护进程后，让主机重新读取
    service.xml，集成随后一轮轮询即可看到新状态。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh-circle-outline"

    def __init__(self, hass, config, host_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}

    def update(self):
        """Refresh host snapshot (availability only, 纯快照)."""
        self._host_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vmhost", {})
            .get(self._host_name)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """Host online only."""
        return self._host_data.get("state") == "poweredOn"

    @property
    def name(self):
        """Short name."""
        return "刷新服务清单"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_refresh_services_{self._host_name}"
        )

    @property
    def device_info(self):
        """Attach to host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    async def async_press(self):
        """Ask host to refresh service registry, then refresh our data."""
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        target_host = self._host_data.get("original_name", self._host_name)
        ok = await self.hass.async_add_executor_job(
            host_refresh_services, conn_details, target_host
        )
        if ok:
            await refresh_data_now(self.hass, self._entry_id)
            self.async_schedule_update_ha_state(force_refresh=True)
        else:
            _LOGGER.error("刷新服务清单失败（主机 %s）", self._host_name)


class _ESXiVMGuestActionButtonBase(ButtonEntity):
    """客户机内一次性动作按钮公共部分：轮询 / 设备 / 数据。"""

    _attr_has_entity_name = True

    #: 子类覆盖：传给 vm_guest_action 的动作名
    _guest_action = None

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Refresh VM snapshot（纯快照；数据由中央定时器统一拉取）。"""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

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

    async def _run_action(self):
        """Call the guest action backend and refresh data afterwards."""
        if not self.available:
            _LOGGER.warning("虚拟机 %s 当前不允许执行该动作", self._vm_name)
            return
        vm_uuid = self._vm_data.get("uuid")
        if not vm_uuid:
            _LOGGER.error("虚拟机 %s：UUID 缺失，无法执行动作", self._vm_name)
            return
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        await self.hass.async_add_executor_job(
            vm_guest_action,
            self.hass,
            self.config["host"],
            self._vm_name,
            vm_uuid,
            self._guest_action,
            conn_details,
            False,  # notify
        )


class ESXiVMStandbyButton(_ESXiVMGuestActionButtonBase):
    """客户机待机（StandbyGuest，需 VMware Tools 正常）。"""

    _guest_action = "standby"
    _attr_icon = "mdi:sleep"

    @property
    def name(self):
        """Short name."""
        return "待机"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_standby_{self._vm_name}"

    @property
    def available(self):
        """Running + functional Tools only."""
        return self._vm_data.get("state") == "running" and str(
            self._vm_data.get("tools_status", "")
        ).lower() in ("toolsok", "toolsold")

    async def async_press(self):
        """Send guest standby."""
        await self._run_action()


class _ESXiVMPowerStateButtonBase(ButtonEntity):
    """ESXi 电源状态类按钮公共部分：挂起 / 唤醒。

    与客户机待机（StandbyGuest，需 Tools）不同，这两个是 ESXi 级电源动作：
    suspend = SuspendVM_Task（挂起到内存/磁盘），唤醒 = 对挂起的 VM 执行
    PowerOnVM_Task（恢复运行，后端 vm_pwr 的 "on" 指令天然覆盖该路径）。
    """

    _attr_has_entity_name = True

    #: 子类覆盖：传给 vm_pwr 的指令（"suspend" / "on"）
    _power_cmnd = None
    #: 子类覆盖：仅在该 state 下按钮可用（"running" / "suspended"）
    _enabled_state = None
    _attr_icon = "mdi:pause"

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the button."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Refresh VM snapshot（纯快照；数据由中央定时器统一拉取）。"""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"][
                self._vm_name
            ]
        except KeyError:
            _LOGGER.debug(
                "VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name
            )
            self._vm_data = {}

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """仅在匹配的电源状态下可用。"""
        return bool(self._vm_data) and self._vm_data.get("state") == self._enabled_state

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

    @property
    def extra_state_attributes(self):
        """把当前电源状态带上，方便自动化判断按钮为何置灰。"""
        if self._vm_data and "state" in self._vm_data:
            return {"power_state": self._vm_data["state"]}
        return {}

    async def async_press(self, **kwargs):
        """下发挂起 / 唤醒指令。"""
        if not self.available:
            _LOGGER.warning(
                "虚拟机 %s 当前状态不允许执行 %s",
                self._vm_name,
                self._power_cmnd,
            )
            return
        vm_uuid = self._vm_data.get("uuid")
        if not vm_uuid:
            _LOGGER.error("虚拟机 %s：UUID 缺失，无法执行 %s", self._vm_name, self._power_cmnd)
            return

        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        await self.hass.async_add_executor_job(
            vm_pwr,
            self.hass,
            self.config["host"],
            self._vm_name,
            [vm_uuid],
            self._power_cmnd,
            conn_details,
            False,  # notify
        )
        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)


class ESXiVMSuspendButton(_ESXiVMPowerStateButtonBase):
    """挂起（SuspendVM_Task，仅运行中可用）。"""

    _power_cmnd = "suspend"
    _enabled_state = "running"
    _attr_icon = "mdi:pause"

    @property
    def name(self):
        """Short name."""
        return "挂起"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_suspend_{self._vm_name}"


class ESXiVMWakeButton(_ESXiVMPowerStateButtonBase):
    """唤醒（对挂起的 VM 执行 PowerOn，即恢复运行；仅挂起中可用）。"""

    _power_cmnd = "on"
    _enabled_state = "suspended"
    _attr_icon = "mdi:play-circle-outline"

    @property
    def name(self):
        """Short name."""
        return "唤醒"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_wake_{self._vm_name}"


class ESXiVMSnapshotRevertButton(_ESXiVMGuestActionButtonBase):
    """还原到当前快照（RevertToCurrentSnapshot_Task）。"""

    _guest_action = "snapshot_revert"
    _attr_icon = "mdi:restore"

    @property
    def name(self):
        """Short name."""
        return "还原当前快照"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_snapshot_revert_{self._vm_name}"

    @property
    def available(self):
        """Only when at least one snapshot exists."""
        try:
            return int(self._vm_data.get("snapshots", 0)) > 0
        except (TypeError, ValueError):
            return False

    async def async_press(self):
        """Revert to current snapshot."""
        await self._run_action()


class ESXiVMToolsUpgradeButton(_ESXiVMGuestActionButtonBase):
    """升级 VMware Tools（UpgradeTools_Task；挂载安装镜像并启动客户机内安装）。"""

    _guest_action = "tools_upgrade"
    _attr_icon = "mdi:tools"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def name(self):
        """Short name."""
        return "升级VMware Tools"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_tools_upgrade_{self._vm_name}"

    @property
    def available(self):
        """Tools install only makes sense on a running VM."""
        return self._vm_data.get("state") == "running"

    async def async_press(self):
        """Trigger Tools upgrade."""
        await self._run_action()


class ESXiVMGuestValidateButton(ButtonEntity):
    """立即强制校验一次该虚拟机保存的来宾凭据（结果反映到二进传感器）。"""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Snapshot VM state."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def name(self):
        """Short name."""
        return "验证来宾凭据"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_guest_validate_{self._vm_name}"
        )

    @property
    def device_info(self):
        """Attach to VM device."""
        vm_name = self._vm_data.get("vm_name", self._vm_name)
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_name))},
            "name": f"虚拟机 {vm_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def available(self):
        """Only for running VMs."""
        return self._vm_data.get("state") == "running"

    async def async_press(self):
        """Force validation; raise so HA UI shows the failure reason."""
        manager = get_manager(self.hass)
        if manager is None or not manager.get_cred(self._entry_id, self._vm_name):
            raise HomeAssistantError("该虚拟机尚未配置来宾凭据")
        result = await manager.async_validate(
            self._entry_id, self._vm_name, force=True
        )
        if not result.get("ok"):
            raise HomeAssistantError(
                f"来宾凭据校验失败：{result.get('error') or '未知原因'}"
            )


class ESXiVMShutdownButton(_ESXiVMPowerStateButtonBase):
    """关机（ShutdownGuest）：通知客户机操作系统走正常关机流程。

    与「关闭电源」的分工等同于物理机上的「开始菜单关机」与「长按电源键」：
    本按钮依赖 VMware Tools，Tools 不在线时按钮直接置灰 —— 那种场景请用
    「关闭电源」硬关。
    """

    _power_cmnd = "shutdown"
    _enabled_state = "running"
    _attr_icon = "mdi:power"

    @property
    def name(self):
        """Short name."""
        return "关机"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_shutdown_{self._vm_name}"

    @property
    def available(self):
        """运行中 + VMware Tools 可用（toolsOk/toolsOld 都能响应 Guest 操作）。"""
        if self._vm_data.get("state") != "running":
            return False
        return str(self._vm_data.get("tools_status", "")).lower() in (
            "toolsok",
            "toolsold",
        )


class ESXiVMPowerOffButton(_ESXiVMPowerStateButtonBase):
    """关闭电源（PowerOffVM_Task）：hypervisor 直接断电，不经过客户机 OS。

    ⚠️ 等同直接拔电源：未保存数据会丢失。日常关机请优先用「关机」；
    本按钮是 Tools 失效 / 客户机卡死时的兜底手段。运行中与挂起中都可关。
    """

    _power_cmnd = "off"
    _enabled_state = "running"
    _attr_icon = "mdi:power-off"

    @property
    def name(self):
        """Short name."""
        return "关闭电源"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_power_off_{self._vm_name}"

    @property
    def available(self):
        """运行中或挂起中都允许硬关（已关机的置灰）。"""
        return bool(self._vm_data) and self._vm_data.get("state") in (
            "running",
            "suspended",
        )


class ESXiVMConsolidateButton(ButtonEntity):
    """合并快照遗留磁盘（ConsolidateVMDisks_Task）。

    删除快照后若磁盘没能立刻合并（``runtime.consolidationNeeded = True``），
    残留的增量盘会一直增长。按钮仅在确实需要合并时可用，任务可能耗时较长。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:database-export-outline"

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Refresh VM snapshot."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def name(self):
        """Short name."""
        return "合并磁盘"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_consolidate_{self._vm_name}"

    @property
    def available(self):
        """Only when consolidation is flagged."""
        return bool(self._vm_data.get("consolidation_needed"))

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

    async def async_press(self):
        """Run ConsolidateVMDisks_Task; raise on failure so UI surfaces it."""
        vm_uuid = self._vm_data.get("uuid")
        if not vm_uuid:
            raise HomeAssistantError("虚拟机 UUID 缺失，无法合并磁盘")
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        try:
            await self.hass.async_add_executor_job(
                vm_consolidate, conn_details, vm_uuid
            )
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(f"磁盘合并失败：{err}") from err
        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)


class ESXiVMCdromEjectButton(ButtonEntity):
    """弹出光驱中的 ISO（IsoBacking → 客户端设备 + 断开连接）。

    只做「弹出」动作，不删除数据存储上的文件 —— 文件删除只发生在控制台
    上传打包 ISO 的完整链路里（那里有挂载记录，确认是本集成传的才删），
    避免误删用户手工挂载的安装镜像。
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:tray-arrow-up"

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}

    def update(self):
        """Refresh VM snapshot."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def name(self):
        """Short name."""
        return "弹出光盘"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_cdrom_eject_{self._vm_name}"

    @property
    def available(self):
        """光驱里挂着 ISO 镜像时才可用（物理光驱/客户端设备不算）。"""
        return any(
            cd.get("backing") == "iso"
            for cd in self._vm_data.get("cdroms") or []
        )

    @property
    def extra_state_attributes(self):
        """暴露当前挂载的 ISO 路径，便于确认弹的是哪张盘。"""
        isos = [
            cd.get("iso")
            for cd in self._vm_data.get("cdroms") or []
            if cd.get("backing") == "iso" and cd.get("iso")
        ]
        return {"iso": isos[0]} if isos else {}

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

    async def async_press(self):
        """Eject ISO backing; raise on failure so the UI shows why."""
        vm_uuid = self._vm_data.get("uuid")
        if not vm_uuid:
            raise HomeAssistantError("虚拟机 UUID 缺失，无法弹出光盘")
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        try:
            result = await self.hass.async_add_executor_job(
                eject_iso, conn_details, vm_uuid, None
            )
        except Exception as err:  # pylint: disable=broad-except
            raise HomeAssistantError(f"弹出光盘失败：{err}") from err
        if not result.get("ejected"):
            raise HomeAssistantError("光驱中没有已挂载的 ISO")
        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)


class _ESXiAllVMsPowerButtonBase(BatchEntityMixin, ButtonEntity):
    """批量关机 / 关闭电源的公共部分（结构与批量重启按钮一致）。"""

    _attr_has_entity_name = True

    #: 子类覆盖：单台按钮 unique_id 中段（vm_shutdown / vm_power_off）
    _vm_kind = None
    _attr_name = None
    _attr_icon = "mdi:power"

    def __init__(self, hass, config_entry):
        """Initialize."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_{self._vm_kind}"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_press(self):
        """批量下发（按「批量分组」参数分批）。"""
        if not self._start_batch(self._run_batch()):
            return

    async def _run_batch(self):
        """实际下发：分组 + 组间间隔，全过程写入操作日志。"""
        entity_ids = vm_entity_ids(
            self.hass, self._config_entry, "button", self._vm_kind
        )
        await run_batch(
            self.hass,
            self._config_entry,
            entity_ids,
            operation=self._attr_name,
            operation_key=self._vm_kind,
            domain="button",
            service="press",
        )


class ESXiAllVMsShutdownButton(_ESXiAllVMsPowerButtonBase):
    """全部虚拟机关机（优雅关机；逐台复用单台「关机」按钮）。"""

    _vm_kind = "vm_shutdown"
    _attr_name = "全部关机"
    _attr_icon = "mdi:power"


class ESXiAllVMsPowerOffButton(_ESXiAllVMsPowerButtonBase):
    """全部虚拟机关闭电源（硬断电；最危险的批量动作之一）。"""

    _vm_kind = "vm_power_off"
    _attr_name = "全部关闭电源"
    _attr_icon = "mdi:power-off"
