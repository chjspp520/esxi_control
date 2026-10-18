"""Number platform for ESXi Control."""
import logging
import time

from homeassistant.components.number import NumberEntity, NumberMode

from .entity_sync import register_platform
from .entry_options import current_options, update_option
from .vm_bulk import host_device_key
from .const import (
    CONF_AGGREGATE_INTERVAL,
    CONF_SCREENSHOT_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_AGGREGATE_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_SCREENSHOT_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DOMAIN_DATA,
    MAX_AGGREGATE_INTERVAL,
    MAX_SCREENSHOT_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_AGGREGATE_INTERVAL,
    MIN_SCREENSHOT_INTERVAL,
    MIN_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _build_entities(hass, config_entry):
    """Return every number entity this entry should currently have.

    这些都是全局静态实体（数量不随虚拟机增减变化），但同步 service 需要一份
    完整清单才能判断「哪些实体已经不该存在」，所以同样要暴露出来。
    """
    entry_id = config_entry.entry_id
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if not entry_data:
        return []

    host_name = next(iter(entry_data.get("vmhost", {})), None)
    # 「数据刷新间隔」总是创建：它控制的是向 ESXi 拉数据的频率，与监控哪些对象无关
    entities = [ESXiUpdateIntervalNumber(config_entry, host_name)]
    entities.append(ESXiAggregateIntervalNumber(config_entry, host_name))
    if "vm" in entry_data.get("monitored_conditions", []):
        entities.append(ESXiScreenshotIntervalNumber(config_entry, host_name))
    return entities


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the interval number entities.

    · 「实体聚合刷新间隔」**总是**创建 —— 聚合实体本身不依赖任何 monitored 条件；
    · 「截图间隔」只在开了虚拟机监控时才有意义。
    """
    entry_id = config_entry.entry_id
    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(hass, entry_id, "number", _build_entities, async_add_entities)


class ESXiUpdateIntervalNumber(NumberEntity):
    """向 ESXi 拉取数据的间隔（秒）。

    「数据刷新间隔」—— 实体轮询触发「真正连 ESXi 拉一次全量数据」的节流间隔。
    值写回 config entry options，``EsxiStats.update_data()`` 每次都实时读取，
    所以改动**即时生效、不需要重载集成**。

    ⚠️ 每次刷新都要连一次 ESXi 并遍历全部主机 / 虚拟机 / 数据存储 / 许可证，
       调小会让界面数值更跟手，代价是 ESXi 侧（hostd）压力线性上升。
       只想"现在看一眼实时值"的话，用卡片的「立即刷新」按钮更划算。
    """

    _attr_icon = "mdi:timer-refresh-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_UPDATE_INTERVAL
    _attr_native_max_value = MAX_UPDATE_INTERVAL
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "秒"
    # friendly_name 由「设备名 + 自身短名」拼出（如「ESXi 主机 数据刷新间隔」）
    _attr_has_entity_name = True

    def __init__(self, config_entry, host_name=None):
        """Initialize the number entity."""
        self._config_entry = config_entry
        self._host_name = host_name or config_entry.data.get("host", "unknown")
        self._attr_unique_id = (
            f"{str(config_entry.data.get('host', 'unknown')).replace('.', '_')}"
            f"_{config_entry.entry_id}_update_interval"
        )
        self._attr_name = "数据刷新间隔"

    @property
    def device_info(self):
        """Attach to the same host device as the host switches."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._config_entry.entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    @property
    def native_value(self):
        """Return the currently configured interval in seconds."""
        raw = current_options(self.hass, self._config_entry).get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_UPDATE_INTERVAL
        return max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, value))

    @property
    def extra_state_attributes(self):
        """用法、代价，以及**实际生效情况**（诊断用）。

        「设置值」与「实际节拍」不是一回事：数据刷新是由实体轮询触发的
        （sensor 平台 15 秒、switch 平台 2 秒一轮），所以设置值会被向上取整到
        轮询粒度 —— 设 10 秒、实际约 15 秒；设 20 秒、实际约 30 秒。
        """
        value = self.native_value
        step = 15  # sensor 平台的 SCAN_INTERVAL
        effective = max(step, ((value + step - 1) // step) * step)
        attrs = {
            "usage": "两次向 ESXi 拉取数据之间的最小间隔（秒）",
            "default": DEFAULT_UPDATE_INTERVAL,
            "range": f"{MIN_UPDATE_INTERVAL} ~ {MAX_UPDATE_INTERVAL}",
            "current": f"每 {value} 秒",
            "effective": f"约每 {effective} 秒（受实体轮询粒度限制：sensor 平台 15 秒）",
            "note": "调小更跟手，但每次刷新都会连一次 ESXi 并全量遍历虚拟机，"
                    "ESXi 负载随之上升；只想立刻看一次实时值请用卡片的「立即刷新」按钮",
        }
        # ── 实际刷新情况：设了间隔却看不到数据更新时，看这几个值最快 ──
        entry_data = (
            self.hass.data.get(DOMAIN_DATA, {}).get(self._config_entry.entry_id) or {}
        )
        client = entry_data.get("client")
        if client is not None:
            last_at = getattr(client, "last_refresh_at", 0.0) or 0.0
            attrs["last_refresh"] = (
                f"{int(time.monotonic() - last_at)} 秒前" if last_at else "尚未刷新"
            )
            attrs["last_refresh_ok"] = bool(getattr(client, "last_update_ok", False))
            seconds = getattr(client, "last_refresh_seconds", None)
            if seconds is not None:
                attrs["last_refresh_seconds"] = seconds
            failures = getattr(client, "_consecutive_failures", 0)
            if failures:
                attrs["consecutive_failures"] = failures
        return attrs

    async def async_set_native_value(self, value):
        """Persist the new interval into the config entry options."""
        # 基于最新 options 只改这一个键 —— 否则会把别人（例如截图开关状态）覆盖掉
        update_option(self.hass, self._config_entry, CONF_UPDATE_INTERVAL, int(value))
        self.async_write_ha_state()
        _LOGGER.debug("数据刷新间隔已更新为 %s 秒", value)


class ESXiAggregateIntervalNumber(NumberEntity):
    """实体聚合快照的刷新间隔（秒）。

    与「截图间隔」同构：值写回 config entry options，聚合实体每次重建前实时读取，
    所以改动**即时生效、不需要重载集成**（下一次 _collect 就用新间隔）。
    不设置 entity_category，这样它会显示在设备页的「控制」区域。
    """

    _attr_icon = "mdi:timer-sync-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_AGGREGATE_INTERVAL
    _attr_native_max_value = MAX_AGGREGATE_INTERVAL
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "秒"
    # friendly_name 由「设备名 + 自身短名」拼出（如「ESXi 主机 实体聚合刷新间隔」）
    _attr_has_entity_name = True

    def __init__(self, config_entry, host_name=None):
        """Initialize the number entity."""
        self._config_entry = config_entry
        self._host_name = host_name or config_entry.data.get("host", "unknown")
        self._attr_unique_id = (
            f"{str(config_entry.data.get('host', 'unknown')).replace('.', '_')}"
            f"_{config_entry.entry_id}_aggregate_interval"
        )
        self._attr_name = "实体聚合刷新间隔"

    @property
    def device_info(self):
        """Attach to the same host device as the host switches."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._config_entry.entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    @property
    def native_value(self):
        """Return the currently configured interval in seconds."""
        raw = current_options(self.hass, self._config_entry).get(
            CONF_AGGREGATE_INTERVAL, DEFAULT_AGGREGATE_INTERVAL
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_AGGREGATE_INTERVAL
        return max(MIN_AGGREGATE_INTERVAL, min(MAX_AGGREGATE_INTERVAL, value))

    async def async_set_native_value(self, value):
        """Persist the new interval into the config entry options."""
        # 基于最新 options 只改这一个键 —— 否则会把别人（例如截图开关状态）覆盖掉
        update_option(
            self.hass, self._config_entry, CONF_AGGREGATE_INTERVAL, int(value)
        )
        self.async_write_ha_state()
        _LOGGER.debug("实体聚合刷新间隔已更新为 %s 秒", value)


class ESXiScreenshotIntervalNumber(NumberEntity):
    """虚拟机截图抓取间隔。

    值写回 config entry options，截图开关每次比对时实时读取该值，
    所以改动即时生效，也不需要重载集成。
    不设置 entity_category，这样它会显示在设备页的「控制」区域。
    """

    _attr_icon = "mdi:timer-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_SCREENSHOT_INTERVAL
    _attr_native_max_value = MAX_SCREENSHOT_INTERVAL
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "秒"
    # friendly_name 由「设备名 + 自身短名」拼出
    _attr_has_entity_name = True

    def __init__(self, config_entry, host_name=None):
        """Initialize the number entity."""
        self._config_entry = config_entry
        self._host_name = host_name or config_entry.data.get("host", "unknown")
        self._attr_unique_id = (
            f"{str(config_entry.data.get('host', 'unknown')).replace('.', '_')}"
            f"_{config_entry.entry_id}_screenshot_interval"
        )
        self._attr_name = "截图间隔"

    @property
    def device_info(self):
        """Attach to the same host device as the host switches."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._config_entry.entry_id, self._host_name))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware",
            "model": "ESXi 主机",
        }

    @property
    def native_value(self):
        """Return the currently configured interval in seconds."""
        raw = current_options(self.hass, self._config_entry).get(
            CONF_SCREENSHOT_INTERVAL, DEFAULT_SCREENSHOT_INTERVAL
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_SCREENSHOT_INTERVAL
        return max(MIN_SCREENSHOT_INTERVAL, min(MAX_SCREENSHOT_INTERVAL, value))

    async def async_set_native_value(self, value):
        """Persist the new interval into the config entry options."""
        update_option(
            self.hass, self._config_entry, CONF_SCREENSHOT_INTERVAL, int(value)
        )
        self.async_write_ha_state()
        _LOGGER.debug("截图间隔已更新为 %s 秒", value)
