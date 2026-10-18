"""ESXi Control select platform."""
import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_NAME, DOMAIN, DOMAIN_DATA, MANAGED_SERVICES
from .entity_sync import register_platform
from .esxi import esx_connect, esx_disconnect, host_service_policy
from .vm_bulk import host_device_key, refresh_data_now

_LOGGER = logging.getLogger(__name__)

#: 服务启动策略：内部值（ESXi ServicePolicy 枚举）↔ 下拉框中文标签
SERVICE_POLICY_OPTIONS = [
    ("on", "自动启动"),
    ("off", "保持关闭"),
    ("automatic", "按需自动"),
]
_POLICY_TO_LABEL = dict(SERVICE_POLICY_OPTIONS)
_LABEL_TO_POLICY = {label: value for value, label in SERVICE_POLICY_OPTIONS}


def _build_entities(hass, config_entry):
    """Return every select entity this entry should currently have.

    同步 service（entity_sync）会反复调用它来算「此刻应该存在的实体」，
    所以必须是纯函数：只返回对象，不添加实体、不改 hass.data。
    """
    config = config_entry.data
    entry_id = config_entry.entry_id
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if not entry_data:
        return []
    if "vmhost" not in entry_data.get("monitored_conditions", []):
        return []

    entities = [
        ESXiPowerPolicySelect(hass, config, host_name, config_entry)
        for host_name in entry_data["vmhost"]
    ]

    # 受管服务（SSH / ESXi Shell / NTP）的启动策略：
    #   每台主机都建（长期实体），关机时由 available 置灰
    for host_name in entry_data.get("vmhost", {}):
        for service_key in MANAGED_SERVICES:
            entities.append(
                ESXiServicePolicySelect(
                    hass, config, host_name, service_key, config_entry
                )
            )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ESXi Control select entities."""
    entry_id = config_entry.entry_id
    selects = _build_entities(hass, config_entry)
    if selects:
        _LOGGER.debug("Adding %d power policy select entities", len(selects))
        async_add_entities(selects, True)
    else:
        _LOGGER.debug("No power policy select entities to add")
    register_platform(hass, entry_id, "select", _build_entities, async_add_entities)


class ESXiPowerPolicySelect(SelectEntity):
    """ESXi Power Policy Select Entity."""

    _attr_has_entity_name = True

    def __init__(self, hass, config, host_name, config_entry):
        """Initialize the select entity."""
        self.hass = hass
        self.config = config
        self._host_name = host_name
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}
        self._friendly_name = host_name.replace("_", " ").title()

        # Get initial host data
        try:
            self._host_data = hass.data[DOMAIN_DATA][self._entry_id]["vmhost"][self._host_name]
        except KeyError:
            _LOGGER.debug("Host %s 初始化时还没有数据（正常，等首次刷新）", self._host_name)
            self._host_data = {}

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "电源策略"

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return f"{self.config['host'].replace('.', '_')}_{self._entry_id}_select_power_policy_{self._host_name}"

    @property
    def icon(self):
        """Return the icon for this entity."""
        return "mdi:power-settings"

    @property
    def entity_category(self):
        """Return the entity category."""
        # Using None instead of "config" to ensure visibility
        return None

    def update(self):
        """Update the select state."""
        try:
            # 纯快照：网络拉取由中央定时器统一负责
            self._host_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vmhost"][self._host_name]
        except KeyError:
            _LOGGER.debug("Host %s 暂时不在数据里（可能在刷新，或已被删除）", self._host_name)
            self._host_data = {}

    @property
    def device_info(self):
        """Return device information."""
        host_data = self._host_data or {}
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware ESXi",
            "model": "ESXi 主机",
            "sw_version": host_data.get("version", "Unknown"),
        }

    @property
    def current_option(self) -> str | None:
        """Return the current power policy."""
        if self._host_data:
            current_policy = self._host_data.get("power_policy")
            _LOGGER.debug("Host %s current power policy: %s", self._host_name, current_policy)
            # Return None if it's "n/a" or not available
            if current_policy and current_policy != "n/a":
                return current_policy
        return None

    @property
    def options(self) -> list[str]:
        """Return the list of available power policies."""
        if self._host_data:
            policies = self._host_data.get("available_power_policies", [])
            _LOGGER.debug("Host %s available power policies: %s", self._host_name, policies)
            return policies
        return []

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if self._host_data is None:
            _LOGGER.debug("Host %s: no host data available", self._host_name)
            return False

        host_state = self._host_data.get("state")
        available_policies = self._host_data.get("available_power_policies", [])

        _LOGGER.debug("Host %s availability check: state=%s, policies=%s",
                     self._host_name, host_state, available_policies)

        # Entity is available if host is powered on AND has power policies
        # But we show it even if temporarily unavailable for better UX
        is_available = (host_state == "poweredOn" and len(available_policies) > 0)

        if not is_available:
            _LOGGER.debug("Host %s is not available: state=%s, policy_count=%d",
                         self._host_name, host_state, len(available_policies))

        return is_available

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state."""
        return True

    async def async_select_option(self, option: str) -> None:
        """Change the power policy."""
        _LOGGER.info("Changing power policy for %s to %s", self._host_name, option)

        success = await self.hass.async_add_executor_job(
            self._set_power_policy, option
        )

        if success:
            # Trigger a data update to refresh the UI
            await self.hass.async_add_executor_job(self.update)

            # Show notification
            try:
                from homeassistant.components import persistent_notification
                persistent_notification.create(
                    self.hass,
                    f"电源策略已更改为 '{option}' 用于主机 {self._friendly_name}",
                    title="ESXi 电源策略已更改",
                    notification_id=f"esxi_power_policy_{self._host_name}"
                )
            except Exception as e:
                _LOGGER.debug("Could not create notification: %s", e)
        else:
            _LOGGER.error("Failed to change power policy for %s", self._host_name)

    def _set_power_policy(self, policy: str) -> bool:
        """Set the power policy on the ESXi host."""
        try:
            # Get connection details from config
            conn_details = {
                "host": self.config.get("host"),
                "user": self.config.get("username"),
                "pwd": self.config.get("password"),
                "port": self.config.get("port", 443),
                "ssl": self.config.get("verify_ssl", False),
            }

            conn = esx_connect(**conn_details)
            if not conn:
                _LOGGER.error("Failed to connect to ESXi host %s", self._host_name)
                return False

            try:
                from pyVmomi import vim, vmodl

                content = conn.RetrieveContent()
                obj_view = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.HostSystem], True
                )
                esxi_hosts = obj_view.view
                obj_view.Destroy()

                # Find our specific host using original name
                target_host = None
                original_host_name = self._host_data.get("original_name", self._host_name)
                for host in esxi_hosts:
                    if original_host_name and (
                        host.summary.config.name.lower() == original_host_name.lower() or
                        host.name.lower() == original_host_name.lower()):
                        target_host = host
                        break

                if not target_host:
                    _LOGGER.error("Host %s not found", self._host_name)
                    return False

                # Check if host supports power system capability
                if (not hasattr(target_host.config, 'powerSystemCapability') or
                    not target_host.config.powerSystemCapability):
                    _LOGGER.error("Host %s does not support power policy configuration", self._host_name)
                    return False

                # Find the policy key
                policy_key = None
                for available_policy in target_host.config.powerSystemCapability.availablePolicy:
                    if available_policy.shortName == policy:
                        policy_key = available_policy.key
                        break

                if not policy_key:
                    available_policies = [p.shortName for p in target_host.config.powerSystemCapability.availablePolicy]
                    _LOGGER.error(
                        "Power policy '%s' not found for host %s. Available policies: %s",
                        policy, self._host_name, available_policies
                    )
                    return False

                # Apply the power policy
                target_host.configManager.powerSystem.ConfigurePowerPolicy(policy_key)
                _LOGGER.info("Successfully changed power policy to '%s' for host %s", policy, self._host_name)
                return True

            except vmodl.MethodFault as error:
                _LOGGER.error("VMware method fault while setting power policy: %s", error.msg)
                return False
            except Exception as error:
                _LOGGER.error("Unexpected error while setting power policy: %s", error)
                return False
            finally:
                esx_disconnect(conn)

        except Exception as error:
            _LOGGER.error("Error setting power policy for %s: %s", self._host_name, error)
            return False


class ESXiServicePolicySelect(SelectEntity):
    """受管服务的启动策略选择器（on / off / automatic）。

    与二进传感器 / 开关的分工：
      · binary_sensor 只读报告「服务现在是否在跑」；
      · switch 负责「现在立刻启动 / 停止」；
      · 本选择器负责「主机开机 / 防火墙端口打开时要不要自动拉起它」。
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, config, host_name, service_key, config_entry):
        """Initialize the service-policy select."""
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
        """Snapshot entry for this service."""
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
        """Online host + service present."""
        return self._host_data.get("state") == "poweredOn" and self._service_entry() is not None

    @property
    def name(self):
        """Short name = 服务 + 启动策略."""
        _, label, _ = MANAGED_SERVICES[self._service_key]
        return f"{label}启动策略"

    @property
    def unique_id(self):
        """Return a unique ID."""
        slug, _, _ = MANAGED_SERVICES[self._service_key]
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_select_service_policy_{slug}_{self._host_name}"
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
            "manufacturer": "VMware ESXi",
            "model": "ESXi 主机",
        }

    @property
    def options(self) -> list[str]:
        """Chinese labels for the three ESXi policies."""
        return [label for _, label in SERVICE_POLICY_OPTIONS]

    @property
    def current_option(self) -> str | None:
        """Map raw policy to its Chinese label."""
        entry = self._service_entry()
        if not entry:
            return None
        return _POLICY_TO_LABEL.get(entry.get("policy"), entry.get("policy"))

    async def async_select_option(self, option: str) -> None:
        """Write the chosen policy back to ESXi."""
        policy = _LABEL_TO_POLICY.get(option, option)
        conn_details = {
            "host": self.config.get("host"),
            "user": self.config.get("username"),
            "pwd": self.config.get("password"),
            "port": self.config.get("port", 443),
            "ssl": self.config.get("verify_ssl", False),
        }
        target_host = self._host_data.get("original_name", self._host_name)
        ok = await self.hass.async_add_executor_job(
            host_service_policy, conn_details, target_host, self._service_key, policy
        )
        if ok:
            await refresh_data_now(self.hass, self._entry_id)
            self.async_schedule_update_ha_state(force_refresh=True)
        else:
            _LOGGER.error(
                "修改服务 %s 启动策略失败（主机 %s）", self._service_key, self._host_name
            )
