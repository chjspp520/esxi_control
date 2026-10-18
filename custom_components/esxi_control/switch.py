"""Switch platform for ESXi Control integration."""
import logging
from datetime import timedelta
from homeassistant.components.switch import SwitchEntity

from .const import (
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_NAME,
    CONF_SCREENSHOT_DIR,
    DEFAULT_SCREENSHOT_DIR,
    CONF_SCREENSHOT_INTERVAL,
    DEFAULT_SCREENSHOT_INTERVAL,
    MIN_SCREENSHOT_INTERVAL,
    MAX_SCREENSHOT_INTERVAL,
    MANAGED_SERVICES,
)
from .entity_sync import dedupe_entities, register_platform
from .entry_options import current_options, store_options
from .esxi import (
    vm_pwr,
    host_pwr,
    host_maintenance,
    host_lockdown,
    host_service,
    get_vm_screenshot,
    vm_nic_set,
    vm_autostart_set,
    host_autostart_set_enabled,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import entity_registry as er
from .optimistic import PendingState
from .screenshot_hub import get_hub, vm_screenshot_paths
from .console_web import (
    REMOTE_DESKTOP_UNIQUE_ID,
    async_set_remote_desktop,
    remote_desktop_enabled,
)
from .vm_bulk import (
    BatchEntityMixin,
    call_all,
    host_device_info,
    host_device_key,
    vm_device_key,
    refresh_data_now,
    run_batch,
    vm_entity_ids,
    vm_keys,
)

# 平台轮询只读 hass.data 快照（数据由 __init__ 里的中央定时器统一拉取），15 秒足够。
# ⚠️ 截图开关**不能**用平台轮询来驱动抓图：HA 的轮询间隔是平台级单一值，实体上的
#   scan_interval 属性不会被读取（详见 ESXiVMScreenshotSwitch 的类 docstring）——
#   它改为自己排定时器按「截图间隔」抓图，本平台轮询只负责刷新快照与发现间隔变更。
SCAN_INTERVAL = timedelta(seconds=15)

_LOGGER = logging.getLogger(__name__)

#: 全局「远程桌面」开关的固定 entity_id：集成级只有一个，写死便于卡片 / 自动化引用
REMOTE_DESKTOP_ENTITY_ID = "switch.esxi_remote_desktop"


def _remote_desktop_host_is(hass, entry_id):
    """本条目是不是全局「远程桌面」开关的宿主条目。

    HA 的实体必须挂在某个 config entry 上，而这个开关是**整集成一个**（管所有
    ESXi、所有虚拟机），只能由一个条目来建：注册表里它记在谁名下就是谁，其余条目
    返回 False 不重复创建（同一 unique_id 重复添加会被 HA 拒绝）。
    宿主条目被删除时注册表条目随之清掉，另一个条目会在下一轮同步里接手。
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("switch", DOMAIN, REMOTE_DESKTOP_UNIQUE_ID)
    if not entity_id:
        # 首次加载（注册表里还没有它）：多个条目几乎同时 setup，只看注册表会
        #   让每个条目都以为自己该建 → 同一 unique_id 被添加两次、后一个被 HA
        #   丢掉并刷「does not generate unique IDs」告警。这里固定给**第一个**
        #   条目，结果确定、不依赖加载顺序。
        entries = [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)]
        return not entries or entries[0] == entry_id
    reg_entry = registry.async_get(entity_id)
    return bool(reg_entry) and reg_entry.config_entry_id == entry_id


def _build_entities(hass, config_entry):
    """Return every switch entity this entry should currently have.

    同步 service（entity_sync）会反复调用它来算「此刻应该存在的实体」，
    所以必须是纯函数：只返回对象，不添加实体、不改 hass.data。
    """
    config = config_entry.data
    entry_id = config_entry.entry_id
    entry_data = hass.data[DOMAIN_DATA][entry_id]
    switches = []

    # Create VM switches
    if "vm" in entry_data["monitored_conditions"]:
        for vm_name, vm_info in entry_data["vm"].items():
            switches.append(ESXiVMSwitch(hass, config, vm_name, config_entry))
            switches.append(ESXiVMScreenshotSwitch(hass, config, vm_name, config_entry))
            # 随主机自启（一台 VM 一个，状态挂在主机 AutoStartManager 序列上）
            switches.append(ESXiVMAutostartSwitch(hass, config, vm_name, config_entry))
            # 每块网卡一个连接开关（热连接/断开；没有硬件 MAC 的网卡无法精确定位，跳过）
            for nic in vm_info.get("nics") or []:
                if nic.get("mac"):
                    switches.append(
                        ESXiVMNicSwitch(
                            hass, config, vm_name, nic.get("mac"), config_entry
                        )
                    )

    # Create host switches
    if "vmhost" in entry_data["monitored_conditions"]:
        for host_name, host_info in entry_data["vmhost"].items():
            switches.append(ESXiHostSwitch(hass, config, host_name, config_entry))
            # 维护模式 / 服务开关：长期实体，关机时自动置灰（见 available）
            switches.append(
                ESXiHostMaintenanceSwitch(hass, config, host_name, config_entry)
            )
            # 锁定模式仅在信息可用（adminDisabled 非 None）的主机上创建
            if host_info.get("lockdown") is not None:
                switches.append(
                    ESXiHostLockdownSwitch(hass, config, host_name, config_entry)
                )
            for service_key in MANAGED_SERVICES:
                switches.append(
                    ESXiHostServiceSwitch(
                        hass, config, host_name, service_key, config_entry
                    )
                )
            # 「随主机启动和停止虚拟机」总开关（AutoStartManager.defaults.enabled）
            switches.append(
                ESXiHostAutostartSwitch(hass, config, host_name, config_entry)
            )

    # 全局静态：整机（全部虚拟机）批量控制，挂在主机设备下
    if "vm" in entry_data["monitored_conditions"]:
        switches.append(ESXiAllVMsPowerSwitch(hass, config_entry))
        switches.append(ESXiAllVMsScreenshotSwitch(hass, config_entry))

    # 集成级：全局「远程桌面」总开关（不挂任何一台 ESXi，整集成只有一个）。
    #   它只管控制台 / 文件系统面板 / 虚拟机管理页面，和截图（image.*）无关。
    if _remote_desktop_host_is(hass, entry_id):
        switches.append(ESXiRemoteDesktopSwitch(hass, config_entry))

    return dedupe_entities(switches)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up switch platform."""
    entry_id = config_entry.entry_id
    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(hass, entry_id, "switch", _build_entities, async_add_entities)


class ESXiVMSwitch(SwitchEntity):
    """ESXi VM Power Switch."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the switch."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._state = None
        self._vm_data = {}
        # 乐观状态：提交开关机后先按用户期望显示，等真实状态追上来再收敛。
        #   ESXi 的开关机是异步任务（几秒~几十秒），而集成数据还有 45 秒节流，
        #   不这样处理的话开关会在点击后立刻弹回旧状态，过一会儿又自己变回来。
        self._pending = PendingState()

    def update(self):
        """Update the switch state（纯快照；数据由中央定时器统一拉取）。"""
        try:
            entry_data = self.hass.data[DOMAIN_DATA][self._entry_id]
            self._vm_data = entry_data["vm"][self._vm_name]

            real = self._vm_data.get("state") == "running"
            self._state, need_refresh, expired = self._pending.resolve(real)
            if expired:
                _LOGGER.warning("虚拟机 %s：%s", self._vm_name, expired)
            if need_refresh:
                self._request_refresh()

        except KeyError:
            _LOGGER.debug("VM %s 暂时不在数据里（可能在刷新，或已被删除）", self._vm_name)
            self._vm_data = {}
            self._state = None

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "电源"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_switch_{self._vm_name}"

    @property
    def is_on(self):
        """Return true if the VM is powered on."""
        return self._state

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
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
            # Only include power-related attributes that aren't available as individual sensors
            # State is relevant since it's the power state of the VM
            attrs_map = {
                "state": "state",  # Power state is relevant to the power switch
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._vm_data:
                    attrs[attr_name] = self._vm_data[data_key]
        return attrs

    async def async_turn_on(self, **kwargs):
        """Turn the VM on（幂等 + 乐观状态）。"""
        try:
            if self._pending.active and self._pending.target:
                _LOGGER.debug("虚拟机 %s 的开机命令已提交，忽略重复请求", self._vm_name)
                return

            vm_uuid = await self._ensure_uuid()
            if not vm_uuid:
                _LOGGER.error("Cannot power on VM %s: UUID not found", self._vm_name)
                return

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            # 先乐观置位再下发命令（顺序很重要：反过来，命令执行期间的任何一次
            #   轮询都会把开关打回旧状态）
            self._pending.start(True, "开机")
            self._state = True
            self.async_write_ha_state()

            ok = await self.hass.async_add_executor_job(
                vm_pwr,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                "on",
                conn_details,
                False,  # notify
            )
            if not ok:
                # 命令根本没发出去（例如连不上 ESXi）→ 不能继续骗用户
                _LOGGER.error("虚拟机 %s：开机命令下发失败", self._vm_name)
                self._pending.clear()
                self.async_schedule_update_ha_state(force_refresh=True)

        except Exception as e:
            self._pending.clear()
            _LOGGER.error("Failed to power on VM %s: %s", self._vm_name, e)

    async def async_turn_off(self, **kwargs):
        """Turn the VM off with smart shutdown logic（幂等 + 乐观状态）。"""
        try:
            if self._pending.active and not self._pending.target:
                _LOGGER.debug("虚拟机 %s 的关机命令已提交，忽略重复请求", self._vm_name)
                return

            vm_uuid = await self._ensure_uuid()
            if not vm_uuid:
                _LOGGER.error("Cannot power off VM %s: UUID not found", self._vm_name)
                return

            # Determine shutdown method based on VMware Tools status
            tools_status = self._vm_data.get("tools_status", "").lower()

            # Use graceful shutdown if VMware Tools are available and functional
            # toolsOk = current and running, toolsOld = old version but functional
            if tools_status in ["toolsok", "toolsold"]:
                power_command = "shutdown"
                shutdown_method = f"graceful shutdown (VMware Tools: {tools_status})"
            else:
                power_command = "off"
                shutdown_method = f"hard power off (VMware Tools: {tools_status})"

            _LOGGER.info("VM %s: Using %s", self._vm_name, shutdown_method)

            conn_details = {
                "host": self.config["host"],
                "user": self.config["username"],
                "pwd": self.config["password"],
                "port": self.config["port"],
                "ssl": self.config["verify_ssl"],
            }

            self._pending.start(False, "关机")
            self._state = False
            self.async_write_ha_state()

            ok = await self.hass.async_add_executor_job(
                vm_pwr,
                self.hass,
                self.config["host"],
                self._vm_name,
                [vm_uuid],
                power_command,
                conn_details,
                False,  # notify
            )
            if not ok:
                _LOGGER.error("虚拟机 %s：关机命令下发失败", self._vm_name)
                self._pending.clear()
                self.async_schedule_update_ha_state(force_refresh=True)

        except Exception as e:
            self._pending.clear()
            _LOGGER.error("Failed to power off VM %s: %s", self._vm_name, e)

    async def _ensure_uuid(self):
        """确保拿到虚拟机 UUID；数据还没到位时先催一次再取。"""
        vm_uuid = self._vm_data.get("uuid")
        if vm_uuid:
            return vm_uuid
        await refresh_data_now(self.hass, self._entry_id)
        entry_data = (self.hass.data.get(DOMAIN_DATA, {}) or {}).get(self._entry_id) or {}
        self._vm_data = (entry_data.get("vm", {}) or {}).get(self._vm_name) or {}
        return self._vm_data.get("uuid")

    def _request_refresh(self):
        """催一次数据刷新。

        ``update()`` 跑在 executor 线程里，不能直接 await，所以交给事件循环执行。
        """
        self.hass.add_job(self._refresh_soon)

    async def _refresh_soon(self):
        """用无节流内核刷新数据并重写状态，让等待中的操作尽快收敛。"""
        if await refresh_data_now(self.hass, self._entry_id):
            self.async_schedule_update_ha_state(force_refresh=True)

    @property
    def icon(self):
        """Return the icon for the switch（虚拟机，不是主机）."""
        if self.is_on:
            return "mdi:desktop-tower"
        return "mdi:power-off"


class ESXiHostSwitch(SwitchEntity):
    """ESXi Host Power Switch."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_name, config_entry):
        """Initialize the switch."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._state = None
        self._host_data = {}

    def update(self):
        """Update the switch state（纯快照；数据由中央定时器统一拉取）。"""
        try:
            self._host_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"][self._host_name]

            # Set state based on host power state
            host_state = self._host_data.get("state", "unknown")
            self._state = host_state == "poweredOn"

        except KeyError:
            _LOGGER.debug("Host %s 暂时不在数据里（可能在刷新，或已被删除）", self._host_name)
            self._state = None

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "电源"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_host_switch_{self._host_name}"

    @property
    def is_on(self):
        """Return true if the host is powered on."""
        return self._state

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        return self._host_data is not None and "state" in self._host_data

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
            # Only include power-related attributes that aren't available as individual sensors
            # State and shutdown capability are relevant to power control
            attrs_map = {
                "state": "state",  # Power state is relevant to the power switch
                "shutdown_supported": "shutdown_supported",  # Power capability info
            }
            for attr_name, data_key in attrs_map.items():
                if data_key in self._host_data:
                    attrs[attr_name] = self._host_data[data_key]
        return attrs

    async def async_turn_on(self, **kwargs):
        """Turn the host on."""
        # Note: ESXi hosts typically cannot be powered on remotely
        # This is mainly for wake-on-LAN scenarios or integrated management
        _LOGGER.warning(
            "Power on command not supported for ESXi hosts. "
            "Use physical power button or management interface."
        )

    async def async_turn_off(self, **kwargs):
        """Turn the host off (shutdown)."""
        try:
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

            # Use shutdown command without force - safer approach
            await self.hass.async_add_executor_job(
                host_pwr,
                self.hass,
                target_host,
                "shutdown",
                conn_details,
                False,  # force=False - user should manually set maintenance mode first
                True    # notify
            )

            # 立刻无节流拉一轮让状态收敛，再重读快照（update 本身不再触发网络）
            await refresh_data_now(self.hass, self._entry_id)
            self.update()

        except Exception as e:
            _LOGGER.error("Failed to shutdown host %s: %s", self._host_name, e)

    @property
    def icon(self):
        """Return the icon for the switch."""
        if self.is_on:
            return "mdi:server-network"
        return "mdi:server-network-off"


class _ESXiHostControlSwitchBase(SwitchEntity):
    """主机可写开关的公共部分：轮询 / 设备 / 连接信息 / 乐观收敛。"""

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._state = None
        self._host_data = {}
        # 维护模式进入 / 服务启停都要几秒，且数据有节流，同样用乐观状态防止弹回
        self._pending = PendingState()

    def _load_host_data(self):
        """读主机数据快照并用乐观状态覆盖显示值（网络拉取由中央定时器负责）。"""
        self._host_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"].get(
            self._host_name
        ) or {}

    def _resolve_real(self, real):
        """把真实值交给 PendingState；若已到期望状态则清乐观、必要时催刷新。"""
        self._state, need_refresh, expired = self._pending.resolve(real)
        if expired:
            _LOGGER.warning("%s：%s", self.name, expired)
        if need_refresh:
            self.hass.add_job(self._refresh_soon)

    async def _refresh_soon(self):
        """无节流强刷一轮，让等待中的开关尽快收敛。"""
        if await refresh_data_now(self.hass, self._entry_id):
            self.async_schedule_update_ha_state(force_refresh=True)

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """Online host only."""
        return self._host_data.get("state") == "poweredOn"

    @property
    def is_on(self):
        """Current optimistic/real state."""
        return self._state

    @property
    def device_info(self):
        """Attach to host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    def _conn_details(self):
        """Connection details dict for esxi.* backend functions."""
        return {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }

    def _target_host(self):
        """ESXi 实际主机名（清单里存的是处理过的键，查找要用原始名）。"""
        return self._host_data.get("original_name", self._host_name)


class ESXiHostMaintenanceSwitch(_ESXiHostControlSwitchBase):
    """维护模式开关：on = 进入维护模式（evacuate=False，不迁走 VM）。

    ⚠️ ESXi 单主机上仍有开机 VM 时进入维护模式会失败（vCenter 才会自动迁移），
    这是服务端语义，集成如实把失败暴露在日志里，不做额外拦截。
    """

    def update(self):
        """Read maintenance flag."""
        try:
            self._load_host_data()
            value = self._host_data.get("maintenance_mode")
            real = bool(value) if value != "N/A" else False
            self._resolve_real(real)
        except KeyError:
            self._state = None

    @property
    def name(self):
        """Short name."""
        return "维护模式开关"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_host_maintenance_{self._host_name}"

    @property
    def icon(self):
        """Icon."""
        return "mdi:wrench-check" if self.is_on else "mdi:wrench-outline"

    async def _set(self, enable):
        """Enter/exit maintenance with optimistic UI."""
        if self._pending.active:
            _LOGGER.debug("维护模式操作已在进行，忽略重复请求")
            return
        self._pending.start(enable, "进入维护模式" if enable else "退出维护模式")
        self._state = enable
        self.async_write_ha_state()
        try:
            ok = await self.hass.async_add_executor_job(
                host_maintenance, self._conn_details(), self._target_host(), enable
            )
            if not ok:
                self._pending.clear()
                self.async_schedule_update_ha_state(force_refresh=True)
                return
            await self._refresh_soon()
        except Exception as error:  # pylint: disable=broad-except
            self._pending.clear()
            _LOGGER.error("维护模式切换失败（%s）：%s", self._host_name, error)

    async def async_turn_on(self, **kwargs):
        """Enter maintenance mode."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Exit maintenance mode."""
        await self._set(False)


class ESXiHostLockdownSwitch(_ESXiHostControlSwitchBase):
    """锁定模式开关：on = 禁止 DCUI 以外的本地管理入口。"""

    def update(self):
        """Read lockdown flag."""
        try:
            self._load_host_data()
            if self._host_data.get("lockdown") is None:
                self._state = None
                return
            self._resolve_real(bool(self._host_data.get("lockdown")))
        except KeyError:
            self._state = None

    @property
    def available(self):
        """Only when lockdown info is available."""
        return super().available and self._host_data.get("lockdown") is not None

    @property
    def name(self):
        """Short name."""
        return "锁定模式开关"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_host_lockdown_{self._host_name}"

    @property
    def icon(self):
        """Icon."""
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant-outline"

    async def _set(self, enable):
        """Enable/disable lockdown with optimistic UI."""
        if self._pending.active:
            _LOGGER.debug("锁定模式操作已在进行，忽略重复请求")
            return
        self._pending.start(enable, "启用锁定模式" if enable else "退出锁定模式")
        self._state = enable
        self.async_write_ha_state()
        try:
            ok = await self.hass.async_add_executor_job(
                host_lockdown, self._conn_details(), self._target_host(), enable
            )
            if not ok:
                self._pending.clear()
                self.async_schedule_update_ha_state(force_refresh=True)
                return
            await self._refresh_soon()
        except Exception as error:  # pylint: disable=broad-except
            self._pending.clear()
            _LOGGER.error("锁定模式切换失败（%s）：%s", self._host_name, error)

    async def async_turn_on(self, **kwargs):
        """Enable lockdown."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Disable lockdown."""
        await self._set(False)


class ESXiHostServiceSwitch(_ESXiHostControlSwitchBase):
    """受管服务的启动 / 停止开关（SSH、ESXi Shell、NTP 守护进程）。"""

    def __init__(self, hass, config, host_name, service_key, config_entry):
        """Initialize for one managed service."""
        super().__init__(hass, config, host_name, config_entry)
        self._service_key = service_key

    def _service_entry(self):
        """Snapshot entry for this service."""
        for item in self._host_data.get("services") or []:
            if item.get("key") == self._service_key:
                return item
        return None

    def update(self):
        """Read running flag for the service."""
        self._load_host_data()
        entry = self._service_entry()
        if entry is None:
            self._state = None
            return
        self._resolve_real(bool(entry.get("running")))

    @property
    def available(self):
        """Host online and service present in the list."""
        return super().available and self._service_entry() is not None

    @property
    def name(self):
        """Short name = service label + 开关."""
        _, label, _ = MANAGED_SERVICES[self._service_key]
        return f"{label}开关"

    @property
    def unique_id(self):
        """Return a unique ID."""
        slug, _, _ = MANAGED_SERVICES[self._service_key]
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_service_{slug}_{self._host_name}"
        )

    @property
    def icon(self):
        """Service icon."""
        _, _, icon = MANAGED_SERVICES[self._service_key]
        return icon

    async def _set(self, start):
        """Start/stop service with optimistic UI."""
        if self._pending.active:
            _LOGGER.debug("服务 %s 操作已在进行，忽略重复请求", self._service_key)
            return
        action = "start" if start else "stop"
        self._pending.start(start, f"{'启动' if start else '停止'}{self._service_key}")
        self._state = start
        self.async_write_ha_state()
        try:
            ok = await self.hass.async_add_executor_job(
                host_service,
                self._conn_details(),
                self._target_host(),
                self._service_key,
                action,
            )
            if not ok:
                self._pending.clear()
                self.async_schedule_update_ha_state(force_refresh=True)
                return
            await self._refresh_soon()
        except Exception as error:  # pylint: disable=broad-except
            self._pending.clear()
            _LOGGER.error(
                "服务 %s %s 失败：%s", self._service_key, action, error
            )

    async def async_turn_on(self, **kwargs):
        """Start service."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Stop service."""
        await self._set(False)


class ESXiAllVMsPowerSwitch(BatchEntityMixin, SwitchEntity):
    """一键开 / 关所有虚拟机（挂在主机设备下）。

    状态语义与 HA 的开关组一致：只要还有任意一台在运行就是 on。
      · 打开 → 对全部虚拟机开机；
      · 关闭 → 对全部虚拟机关机。

    刻意走 ``switch.turn_on/off`` 服务而不是直接调 ESXi 接口，是为了复用每台虚拟机
    自己那套逻辑：装 Tools 的优雅关机、没装的才硬关闭，与单台操作完全同一条路径。
    """

    _attr_has_entity_name = True
    _attr_name = "全部虚拟机电源"

    def __init__(self, hass, config_entry):
        """Initialize the bulk power switch."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._on_count = 0
        self._total = 0
        # 乐观状态：批量操作提交后先按「全部开 / 全部关」显示
        #   _want_on 记的是期望方向（True 全开 / False 全关 / None 无等待），
        #   _pending 的目标恒为 True（"期望达成"），因为批量场景下"真实状态"
        #   表达的是「有没有达成目标」，而不是开/关本身。
        self._pending = PendingState()
        self._want_on = None

    def update(self):
        """统计运行中的虚拟机数量（纯快照；提交后先按期望显示，避免状态抖动）。"""
        entry_data = (self.hass.data.get(DOMAIN_DATA, {}) or {}).get(self._entry_id) or {}
        # 数据新鲜度由 __init__ 的中央定时器保证（即使本平台实体全被禁用也照常拉取）
        vms = entry_data.get("vm", {}) or {}
        self._total = len(vms)
        self._on_count = sum(
            1 for info in vms.values() if info.get("state") == "running"
        )

        if self._want_on is not None:
            want_on = self._want_on
            achieved = (
                self._on_count == self._total if want_on else self._on_count == 0
            )
            _, need_refresh, expired = self._pending.resolve(achieved)
            if expired:
                _LOGGER.warning("全部虚拟机电源：%s", expired)
                self._want_on = None
            elif not self._pending.active:
                self._want_on = None  # 已收敛
            else:
                if need_refresh:
                    self._request_refresh()
                # 还没收敛 → 按期望显示（全开 / 全关）
                self._on_count = self._total if want_on else 0

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_power"

    @property
    def is_on(self):
        """有任意一台虚拟机在运行即为 on。"""
        return self._on_count > 0

    @property
    def available(self):
        """没有虚拟机时不可用。"""
        return self._total > 0

    @property
    def icon(self):
        """Return the icon for the switch."""
        return "mdi:power" if self.is_on else "mdi:power-off"

    @property
    def extra_state_attributes(self):
        """Return the running / total counts."""
        return {"running": self._on_count, "total": self._total}

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_turn_on(self, **kwargs):
        """开启所有虚拟机（乐观；按「批量分组」参数分批下发）。"""
        if self._want_on is True:
            _LOGGER.debug("全部虚拟机的开机命令已提交，忽略重复请求")
            return
        if not self._start_batch(self._run_power(True)):
            return
        self._want_on = True
        self._pending.start(True, "全部开机")
        self._on_count = self._total
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """关闭所有虚拟机（乐观；按「批量分组」参数分批下发）。"""
        if self._want_on is False:
            _LOGGER.debug("全部虚拟机的关机命令已提交，忽略重复请求")
            return
        if not self._start_batch(self._run_power(False)):
            return
        self._want_on = False
        self._pending.start(True, "全部关机")
        self._on_count = 0
        self.async_write_ha_state()

    async def _run_power(self, turn_on):
        """实际下发：按用户配置分组、组间等间隔，全过程写入操作日志。"""
        entity_ids = vm_entity_ids(
            self.hass, self._config_entry, "switch", "vm_switch"
        )
        count = await run_batch(
            self.hass,
            self._config_entry,
            entity_ids,
            operation="全部开机" if turn_on else "全部关闭",
            operation_key="power_on" if turn_on else "power_off",
            domain="switch",
            service="turn_on" if turn_on else "turn_off",
        )
        if not count:
            self._pending.clear()
            self._want_on = None
            self.async_schedule_update_ha_state(force_refresh=True)

    def _request_refresh(self):
        """催一次数据刷新（update() 在 executor 线程里，必须回到事件循环）。"""
        self.hass.add_job(self._refresh_soon)

    async def _refresh_soon(self):
        """用无节流内核刷新数据并重写状态，让批量操作尽快收敛。"""
        if await refresh_data_now(self.hass, self._entry_id):
            self.async_schedule_update_ha_state(force_refresh=True)


class ESXiAllVMsScreenshotSwitch(SwitchEntity):
    """一键开 / 关所有虚拟机的截图开关（挂在主机设备下）。

    打开 → 每台虚拟机的截图开关都打开（各自立刻抓一帧）；
    关闭 → 全部关闭。
    状态同样取「有任意一台开着即为 on」。
    """

    _attr_has_entity_name = True
    _attr_name = "全部虚拟机截图"

    def __init__(self, hass, config_entry):
        """Initialize the bulk screenshot switch."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._on_count = 0
        self._total = 0
        # 乐观状态：批量开启/关闭是并发调各台自己的服务，options 写入有先后，
        #   这里先按期望显示，下次轮询读到新 options 自然收敛
        #   _want_on 记期望方向（True 全开 / False 全关 / None 无等待）
        self._pending = PendingState()
        self._want_on = None

    def update(self):
        """统计截图开关处于开启状态的虚拟机数量。

        状态与单台开关同源：config_entry.options["screenshot_switches"]，
        两边读同一份数据，避免出现两套互相打架的状态。
        """
        # 读最新的 options（实体里缓存的 config_entry 是旧对象，会读到过期数据）
        options = current_options(self.hass, self._config_entry)
        toggles = options.get("screenshot_switches", {}) or {}
        keys = vm_keys(self.hass, self._entry_id) or list(toggles)
        self._total = len(keys)
        self._on_count = sum(1 for key in keys if toggles.get(key))

        if self._want_on is not None:
            want_on = self._want_on
            achieved = (
                self._on_count == self._total if want_on else self._on_count == 0
            )
            # 截图状态不来自 ESXi 数据，所以不需要催数据刷新
            _, _, expired = self._pending.resolve(achieved)
            if expired:
                _LOGGER.warning("全部虚拟机截图：%s", expired)
                self._want_on = None
            elif not self._pending.active:
                self._want_on = None  # 已收敛
            else:
                self._on_count = self._total if want_on else 0

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_all_vms_screenshot"

    @property
    def is_on(self):
        """有任意一台开着截图即为 on。"""
        return self._on_count > 0

    @property
    def available(self):
        """没有虚拟机时不可用。"""
        return self._total > 0

    @property
    def icon(self):
        """Return the icon for the switch."""
        return "mdi:camera" if self.is_on else "mdi:camera-off"

    @property
    def extra_state_attributes(self):
        """Return the enabled / total counts."""
        return {"enabled": self._on_count, "total": self._total}

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    async def async_turn_on(self, **kwargs):
        """开启所有虚拟机的截图（乐观：先显示全开）。"""
        if self._want_on is True:
            _LOGGER.debug("全部虚拟机的截图开启命令已提交，忽略重复请求")
            return
        self._want_on = True
        self._pending.start(True, "开启全部截图")
        self._on_count = self._total
        self.async_write_ha_state()

        count = await call_all(
            self.hass,
            "switch",
            "turn_on",
            vm_entity_ids(self.hass, self._config_entry, "switch", "vm_screenshot"),
        )
        if not count:
            self._pending.clear()
            self._want_on = None
            self.async_schedule_update_ha_state(force_refresh=True)

    async def async_turn_off(self, **kwargs):
        """关闭所有虚拟机的截图（乐观：先显示全关）。"""
        if self._want_on is False:
            _LOGGER.debug("全部虚拟机的截图关闭命令已提交，忽略重复请求")
            return
        self._want_on = False
        self._pending.start(True, "关闭全部截图")
        self._on_count = 0
        self.async_write_ha_state()

        count = await call_all(
            self.hass,
            "switch",
            "turn_off",
            vm_entity_ids(self.hass, self._config_entry, "switch", "vm_screenshot"),
        )
        if not count:
            self._pending.clear()
            self._want_on = None
            self.async_schedule_update_ha_state(force_refresh=True)


class ESXiVMScreenshotSwitch(SwitchEntity):
    """ESXi VM截图开关 - 开启后自动获取虚拟机截图。

    ⚠️ 抓图节奏**不能靠平台轮询**：HA 的轮询间隔是平台级单一值
    （``entity_platform.py`` 里只有 ``self.scan_interval``，从不读实体上的
    ``scan_interval`` 属性），switch 平台是 15 秒一轮，所以以前"设 5 秒也没变化"
    —— 曾用过的实体级 ``scan_interval`` 属性是**死代码**（任何 HA 版本都不生效）。
    现在改为实体自管理定时器：``async_call_later`` 抓完一帧再排下一帧，间隔从
    「上一帧结束」起算（抓图比间隔慢时不会堆积），平台轮询只负责读快照。
    """

    _attr_has_entity_name = True

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize the screenshot switch."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._state = False  # 默认关闭
        self._vm_data = {}
        self._last_screenshot_time = None
        self._last_screenshot_path = None
        self._last_screenshot_url = None
        # 自管理的抓图定时器：取消函数 + 当前已排的间隔（用于发现选项被改过）
        self._unsub_capture = None
        self._scheduled_interval = None
        # 实体已被移除（重载集成 / VM 被删）：抓图 tick 的 finally 里不能再排下一次，
        #   否则会留下一个永远取消不掉的定时器
        self._removed = False

        # 从配置中读取截图开关状态
        screenshot_switches = config_entry.options.get("screenshot_switches", {})
        if vm_name in screenshot_switches:
            self._state = screenshot_switches[vm_name]

    def update(self):
        """Update the switch state."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"].get(self._vm_name, {})
        except KeyError:
            self._vm_data = {}

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "截图"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_vm_screenshot_{self._vm_name}"

    @property
    def is_on(self):
        """Return true if screenshot capture is enabled."""
        return self._state

    @property
    def should_poll(self):
        """Return True - 平台轮询负责刷新快照 / 发现间隔变更（抓图不看它）."""
        return True

    @property
    def available(self):
        """Return True if entity is available."""
        return self._vm_data is not None and len(self._vm_data) > 0

    @property
    def _capture_interval(self):
        """Return the configured screenshot interval in seconds."""
        raw = current_options(self.hass, self._config_entry).get(
            CONF_SCREENSHOT_INTERVAL, DEFAULT_SCREENSHOT_INTERVAL
        )
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = DEFAULT_SCREENSHOT_INTERVAL
        return max(MIN_SCREENSHOT_INTERVAL, min(MAX_SCREENSHOT_INTERVAL, interval))

    # ------------------------------------------------------------------ #
    # 抓图定时器（自管理：不依赖平台轮询粒度）
    # ------------------------------------------------------------------ #
    async def async_added_to_hass(self):
        """实体上线：开关是开着的就按配置间隔排上抓图。"""
        await super().async_added_to_hass()
        self._removed = False
        self._schedule_capture()

    async def async_will_remove_from_hass(self):
        """实体移除（重载集成 / VM 被删）：一定要停掉定时器，否则会漏排。"""
        self._removed = True
        self._cancel_capture()
        await super().async_will_remove_from_hass()

    def _cancel_capture(self):
        """取消已排的抓图。"""
        if self._unsub_capture is not None:
            self._unsub_capture()
            self._unsub_capture = None
        self._scheduled_interval = None

    def _schedule_capture(self):
        """按当前配置的间隔排下一次抓图；开关关着 / 实体已移除时不排。"""
        self._cancel_capture()
        if self._removed or not self._state or self.hass is None:
            return
        interval = self._capture_interval
        self._scheduled_interval = interval
        self._unsub_capture = async_call_later(
            self.hass, interval, self._async_capture_tick
        )

    async def _async_capture_tick(self, _now=None):
        """到点抓一帧，抓完再排下一帧。

        顺序执行（``finally`` 里才排下一次）⇒ 间隔从「上一帧结束」开始算：
        抓图比间隔还慢时（登录 ESXi + 取图通常 1~3 秒）不会堆积任务，
        只是实际节奏变成「抓图耗时 + 间隔」。
        """
        self._unsub_capture = None
        try:
            await self._capture_screenshot()
        finally:
            self._schedule_capture()

    def _sync_capture_schedule(self):
        """间隔被「截图间隔」number 实体改过就重排。

        由平台轮询（15 秒一次）调用 ⇒ 改动最迟 15 秒内生效，
        不用等旧间隔（最长 600 秒）走完。
        """
        if not self._state:
            self._cancel_capture()
            return
        if self._capture_interval != self._scheduled_interval:
            _LOGGER.debug(
                "虚拟机 %s 截图间隔 %s → %s 秒，重排抓图",
                self._vm_name,
                self._scheduled_interval,
                self._capture_interval,
            )
            self._schedule_capture()

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
        attrs = {
            "vm_name": self._vm_data.get("vm_name", self._vm_name),
            "enabled": self._state,
            "interval": self._capture_interval,
        }
        if self._last_screenshot_time:
            attrs["last_screenshot_time"] = self._last_screenshot_time
        if self._last_screenshot_path:
            attrs["last_screenshot_path"] = self._last_screenshot_path
            attrs["screenshot_url"] = self._last_screenshot_url or (
                f"/local/esxiimg/{self.config['host']}/"
                f"{self._vm_data.get('vm_name', self._vm_name)}.png"
            )
        return attrs

    @property
    def icon(self):
        """Return the icon for the switch."""
        if self.is_on:
            return "mdi:camera"
        return "mdi:camera-off"

    def _save_switch_state(self):
        """保存开关状态到配置选项。

        ⚠️ 必须基于「最新」的 options 做读-改-写：HA 更新 options 时会**换掉
        ConfigEntry 对象**，而实体缓存的那个引用是旧的 —— 直接照着旧值写回，
        会把后一次保存的内容覆盖掉（多台虚拟机、以及批量开关并发保存时都会中招），
        表现为 HA 重启后只有一台虚拟机记得自己的截图开关状态。
        """
        options = current_options(self.hass, self._config_entry)
        screenshot_switches = dict(options.get("screenshot_switches") or {})
        screenshot_switches[self._vm_name] = self._state
        options["screenshot_switches"] = screenshot_switches
        store_options(self.hass, self._config_entry, options)

    async def async_turn_on(self, **kwargs):
        """开启截图：立即抓一帧，然后按配置间隔排上定时器。"""
        self._state = True
        self._save_switch_state()
        await self._capture_screenshot()
        self._schedule_capture()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """关闭截图：停掉定时器（不排新帧，已抓的图保留）。"""
        self._state = False
        self._save_switch_state()
        self._cancel_capture()
        self.async_write_ha_state()

    async def _capture_screenshot(self):
        """获取虚拟机截图"""
        if not self._state:
            return

        vm_proper_name = self._vm_data.get("vm_name", self._vm_name)
        if not vm_proper_name:
            return

        # 虚拟机未开机则跳过
        if self._vm_data.get("state") != "running":
            _LOGGER.debug("虚拟机 %s 未开机，跳过截图", vm_proper_name)
            return

        # 截图保存路径（与 image 实体共用同一套规则，避免写/读路径不一致）
        screenshot_dir = current_options(self.hass, self._config_entry).get(
            CONF_SCREENSHOT_DIR,
            DEFAULT_SCREENSHOT_DIR,
        )
        save_path, image_url = vm_screenshot_paths(
            self.config.get("host", "unknown"), vm_proper_name, screenshot_dir
        )

        try:
            result = await self.hass.async_add_executor_job(
                get_vm_screenshot,
                self.config["host"],
                self.config["username"],
                self.config["password"],
                self.config["port"],
                self.config["verify_ssl"],
                vm_proper_name,  # 用原始名称搜索
                save_path,
            )
            if result:
                from datetime import datetime as dt
                self._last_screenshot_time = dt.now().strftime("%Y-%m-%d %H:%M:%S")
                self._last_screenshot_path = save_path
                self._last_screenshot_url = image_url
                _LOGGER.info("虚拟机 %s 截图成功", vm_proper_name)
                # 通知 image 实体立刻强刷一次，实现画面「近实时」更新
                hub = get_hub(self.hass, self._entry_id)
                if hub:
                    await hub.async_notify_frame(self._vm_name, save_path)
            else:
                _LOGGER.warning("虚拟机 %s 截图失败", vm_proper_name)
        except Exception as e:
            _LOGGER.error("虚拟机 %s 截图异常: %s", vm_proper_name, e)

    async def async_update(self):
        """平台轮询：只刷新快照 + 发现间隔变更，**不抓图**。

        抓图由 ``_async_capture_tick``（自管理定时器）按配置间隔驱动 ——
        以前放在这里导致「最快只能 15 秒一帧」（平台轮询粒度），
        配置成 5 秒时看着像没生效。
        """
        self.update()
        self._sync_capture_schedule()


class ESXiVMNicSwitch(SwitchEntity):
    """单块虚拟网卡的连接开关（热操作，开关机状态下都能切换）。

    对应 vSphere 里网卡右键的「连接 / 断开连接」：同时改 ``connected`` 与
    ``startConnected``，保证下次开机状态与开关一致。每块网卡各一个实体，
    用 MAC 地址做唯一标识（设备热加/删后顺序可能变，MAC 才稳定）。
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lan-connect"

    def __init__(self, hass, config, vm_name, mac, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._mac = mac
        self._mac_slug = str(mac).replace(":", "").replace("-", "").lower()
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}
        self._nic = {}

    def update(self):
        """Refresh VM + NIC snapshot."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )
        self._nic = {}
        for nic in self._vm_data.get("nics") or []:
            if str(nic.get("mac", "")).lower() == str(self._mac).lower():
                self._nic = nic
                break

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """VM 与该网卡都还在快照里才可用。"""
        return bool(self._vm_data) and bool(self._nic)

    @property
    def name(self):
        """短名：网卡 + 网络标签（如「网卡 VM Network」），无标签时退回适配器名。"""
        label = self._nic.get("network") or self._nic.get("label") or self._mac_slug[-4:]
        return f"网卡 {label}"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_nic_{self._mac_slug}_{self._vm_name}"
        )

    @property
    def is_on(self):
        """Connected flag."""
        return bool(self._nic.get("connected"))

    @property
    def extra_state_attributes(self):
        """MAC / 网络 / 适配器型号 / 开机自连，便于核对物理对应关系。"""
        if not self._nic:
            return {}
        return {
            "mac": self._nic.get("mac"),
            "network": self._nic.get("network"),
            "adapter": self._nic.get("adapter"),
            "start_connected": self._nic.get("start_connected"),
        }

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

    async def _set(self, connected):
        """Connect/disconnect the NIC."""
        vm_uuid = self._vm_data.get("uuid")
        if not vm_uuid:
            _LOGGER.error("虚拟机 %s：UUID 缺失，无法操作网卡", self._vm_name)
            return
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        try:
            await self.hass.async_add_executor_job(
                vm_nic_set, conn_details, vm_uuid, self._mac, connected
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(
                "虚拟机 %s 网卡 %s %s 失败：%s",
                self._vm_name,
                self._mac,
                "连接" if connected else "断开",
                error,
            )
            return
        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_turn_on(self, **kwargs):
        """Connect."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Disconnect."""
        await self._set(False)


class ESXiVMAutostartSwitch(SwitchEntity):
    """虚拟机随主机启动开关（AutoStartManager.powerInfo 中的一员）。

    打开 = 加入主机自启动序列（动作 PowerOn，顺序追加到队尾，延时沿用主机默认）；
    关闭 = 从序列移除。注意主机总开关关闭时，即使在序列里也不会被启动，
    主机侧「虚拟机自启动」开关与本开关是两层语义。
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:power-cycle"

    def __init__(self, hass, config, vm_name, config_entry):
        """Initialize."""
        self.hass = hass
        self.config = config
        self._vm_name = vm_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._vm_data = {}
        self._host_snapshot = {}

    def update(self):
        """Refresh VM snapshot and locate its host autostart config."""
        self._vm_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vm", {})
            .get(self._vm_name)
            or {}
        )
        self._host_snapshot = self._find_host_snapshot()

    def _find_host_snapshot(self):
        """独立主机通常只有一台：按 vm.runtime.host.name 精确匹配，兜底取第一台。"""
        hosts = (
            self.hass.data[DOMAIN_DATA][self._entry_id].get("vmhost", {}) or {}
        )
        runtime_host = self._vm_data.get("host_name")
        for key, info in hosts.items():
            if info.get("original_name") == runtime_host or key == runtime_host:
                return info
        return next(iter(hosts.values()), {}) if hosts else {}

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """VM 快照与 VM moid 就绪即可（自启配置关机状态下也能改）。"""
        return bool(self._vm_data.get("moid")) and bool(self._host_snapshot)

    @property
    def name(self):
        """Short name."""
        return "随主机自启"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_vm_autostart_{self._vm_name}"
        )

    @property
    def is_on(self):
        """VM 在自启序列中且启动动作非空即为 on。

        ESXi 6.5 里「移出自启」并不删除条目，而是把 startAction 清空，所以只看
        moid 是否存在会误判，必须同时要求启动动作为 PowerOn（非空）。
        """
        vm_moid = str(self._vm_data.get("moid") or "")
        for entry in (self._host_snapshot.get("autostart") or {}).get("entries") or []:
            if str(entry.get("moid")) == vm_moid and entry.get("start_action"):
                return True
        return False

    @property
    def extra_state_attributes(self):
        """带出主机总开关状态与本 VM 的启动顺序，便于解释为何开了也没启动。"""
        autostart = self._host_snapshot.get("autostart") or {}
        attrs = {"host_autostart_enabled": bool(autostart.get("enabled"))}
        vm_moid = str(self._vm_data.get("moid") or "")
        for entry in autostart.get("entries") or []:
            if str(entry.get("moid")) == vm_moid:
                attrs["start_order"] = entry.get("start_order")
                attrs["start_action"] = entry.get("start_action")
                attrs["stop_action"] = entry.get("stop_action")
                break
        return attrs

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

    async def _set(self, enabled):
        """Add/remove this VM in the host autostart sequence."""
        vm_moid = self._vm_data.get("moid")
        target_host = self._host_snapshot.get(
            "original_name"
        ) or self._vm_data.get("host_name")
        if not vm_moid or not target_host:
            _LOGGER.error(
                "虚拟机 %s：moid/主机名缺失，无法设置随主机自启", self._vm_name
            )
            return
        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        try:
            await self.hass.async_add_executor_job(
                vm_autostart_set, conn_details, target_host, str(vm_moid), enabled
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(
                "虚拟机 %s %s随主机自启失败：%s",
                self._vm_name,
                "加入" if enabled else "移出",
                error,
            )
            return
        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_turn_on(self, **kwargs):
        """Add to autostart sequence."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Remove from autostart sequence."""
        await self._set(False)


class ESXiHostAutostartSwitch(_ESXiHostControlSwitchBase):
    """主机「随主机启动和停止虚拟机」总开关（AutoStartManager.defaults.enabled）。

    只切换总开关，保留已有的 VM 启停顺序与延时配置不变。
    """

    def update(self):
        """Read autostart master flag."""
        self._load_host_data()
        autostart = self._host_data.get("autostart") or {}
        self._resolve_real(bool(autostart.get("enabled")))

    @property
    def name(self):
        """Short name."""
        return "虚拟机自启动"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_host_autostart_{self._host_name}"
        )

    @property
    def icon(self):
        """Icon."""
        return "mdi:power-cycle" if self.is_on else "mdi:power-cycle-off"

    @property
    def extra_state_attributes(self):
        """序列中 VM 数量。"""
        entries = (self._host_data.get("autostart") or {}).get("entries") or []
        return {"autostart_vms": len(entries)}

    async def _set(self, enable):
        """Toggle master flag with optimistic UI."""
        if self._pending.active:
            _LOGGER.debug("自启动总开关操作已在进行，忽略重复请求")
            return
        self._pending.start(enable, "启用自启动" if enable else "关闭自启动")
        self._state = enable
        self.async_write_ha_state()
        try:
            await self.hass.async_add_executor_job(
                host_autostart_set_enabled,
                self._conn_details(),
                self._target_host(),
                enable,
            )
        except Exception as error:  # pylint: disable=broad-except
            self._pending.clear()
            _LOGGER.error("虚拟机自启动总开关切换失败（%s）：%s", self._host_name, error)
            return
        await self._refresh_soon()

    async def async_turn_on(self, **kwargs):
        """Enable."""
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        """Disable."""
        await self._set(False)


class ESXiRemoteDesktopSwitch(SwitchEntity):
    """集成级「远程桌面」总开关（整集成只有一个，管所有主机 / 所有虚拟机）。

    它决定"要不要把钥匙发出去"：
      · 开 → 立刻重签所有虚拟机设备页的「访问」链接，控制台 / 文件系统面板 /
        虚拟机管理页面正常用；
      · 关 → 清空设备页链接、拒绝所有控制台 / 文件系统（进程 / 文件 / 注册表 /
        终端）/ 虚拟机管理请求，并**立即掐断**正在跑的控制台 WebSocket 会话。
    实现见 console_web.async_set_remote_desktop：落盘 + 立即生效，不用重载集成。

    ⚠️ 只管控制台、文件系统面板与虚拟机管理页面：截图功能（image.* 实体）走的是
       ESXi 的 ``/screen?id=`` 接口 → 本地文件 → image 实体，完全不经过控制台，
       所以本开关**不影响**截图，也用不着跟着它开关。
    ⚠️ 状态存在 HA 私有的 ``.storage/esxi_control.remote_desktop`` 里：
       · 不来自 ESXi、也不向 ESXi 写任何东西 —— HA 关机 / 断电都不会改变它，
         更不会把虚拟机侧的状态"改坏"（与批量关机类开关正好相反）；
       · HA 重启后按落盘值恢复，不会退回默认（首次使用默认开启）。
    """

    _attr_has_entity_name = True
    _attr_name = "远程桌面"
    _attr_icon = "mdi:remote-desktop"

    def __init__(self, hass, config_entry):
        """Initialize the global remote-desktop switch."""
        self.hass = hass
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        # 固定 entity_id：HA 按中文名生成的 slug 不稳定（不同版本还不一样），
        #   而这个开关是要在卡片 / 自动化里写死的，所以显式指定 —— 与「实体聚合」
        #   传感器同一套路（见 naming.AGGREGATE_ENTITY_ID 的注释）。
        # 万一这个 id 已被别的实体占用（用户自己建过同名 switch），就让 HA 自动生成：
        #   宁可 id 不理想，也不能让实体整个加不进去。
        registry = er.async_get(hass)
        holder = registry.async_get(REMOTE_DESKTOP_ENTITY_ID)
        if holder is None or holder.unique_id == REMOTE_DESKTOP_UNIQUE_ID:
            self.entity_id = REMOTE_DESKTOP_ENTITY_ID

    @property
    def unique_id(self):
        """全局唯一：**不含** entry_id（它不属于任何一台 ESXi，只属于整个集成）。"""
        return REMOTE_DESKTOP_UNIQUE_ID

    @property
    def device_info(self):
        """挂到 **ESXi 主机设备**下的「控制」区（与其它主机控制实体同一台设备）。

        开关本身仍是整集成一个（不新建设备、不重复建实体），只是"寄放"在宿主条目的
        主机设备上：所以用 host_device_info，并且**不设** entity_category —— 设备页
        里这类可操作实体正是归在「控制」那一段（设成 CONFIG 会被归到「配置」）。
        """
        return host_device_info(self.hass, self._config_entry)

    @property
    def available(self):
        """永远可用：纯 HA 侧策略开关，ESXi 连不上 / 虚拟机全关机也不影响它。"""
        return True

    @property
    def is_on(self):
        """现读现算：真正的状态在 console_web 的全局 store 里（不来自 ESXi）。"""
        return remote_desktop_enabled(self.hass)

    async def async_turn_on(self, **kwargs):
        """启用：立刻把设备页「访问」链接发回去。"""
        await async_set_remote_desktop(self.hass, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """停用：清空链接、拒绝请求，并掐断正在跑的控制台会话。"""
        await async_set_remote_desktop(self.hass, False)
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """把已注册的旧 entity_id 迁移到 ``switch.esxi_remote_desktop``（幂等）。

        ``__init__`` 里设置的 entity_id 只在「注册表里还没有这个 unique_id」时生效；
        已经注册过的（例如上一版是 switch.remote_desktop，或者短 id 总开关关着时
        HA 按中文名生成的 id）得在这里显式改名，否则会永远停在旧 id 上。
        目标 id 已被别的实体占用就保持原样并告警 —— 宁可 id 不统一，也不抢别人的 id。
        """
        await super().async_added_to_hass()
        current = self.entity_id
        if not current or current == REMOTE_DESKTOP_ENTITY_ID:
            return
        registry = er.async_get(self.hass)
        holder = registry.async_get(REMOTE_DESKTOP_ENTITY_ID)
        if holder is not None:
            _LOGGER.warning(
                "「远程桌面」开关的目标 entity_id %s 已被 %s 占用，保持 %s 不变",
                REMOTE_DESKTOP_ENTITY_ID,
                holder.entity_id,
                current,
            )
            return
        try:
            registry.async_update_entity(
                current, new_entity_id=REMOTE_DESKTOP_ENTITY_ID
            )
        except (ValueError, KeyError) as err:
            _LOGGER.warning(
                "「远程桌面」开关的 entity_id 迁移为 %s 失败：%s",
                REMOTE_DESKTOP_ENTITY_ID,
                err,
            )
            return
        # 注册表改名会同步到活实体，这里再显式回写一次，本生命周期内的引用立刻生效
        self.entity_id = REMOTE_DESKTOP_ENTITY_ID
        _LOGGER.info(
            "「远程桌面」开关的 entity_id 已固定为 %s（原 %s）；"
            "引用旧 id 的卡片 / 自动化需要同步修改",
            REMOTE_DESKTOP_ENTITY_ID,
            current,
        )
