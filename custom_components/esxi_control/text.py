"""Text platform for ESXi Control.

目前只有一个实体：「批量分组」，用 ``"组大小,间隔秒"`` 控制批量操作
（全部开机 / 全部关闭 / 全部快照 / 全部重启）的执行节奏。

为什么用 text 而不是两个 number 实体：一个实体就能表达两个数，而且可以把用户的
输入（空格、中文逗号、只写一个数）**归一化后存回去**，实体上始终显示规范格式。
"""
import logging
import re

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .const import DEFAULT_NAME, DOMAIN, DOMAIN_DATA
from .entity_sync import register_platform
from .entry_options import current_options, update_option
from .esxi import set_ntp_servers
from .vm_bulk import (
    DEFAULT_GROUPING,
    GROUPING_OPTION,
    host_device_info,
    host_device_key,
    normalize_grouping,
    parse_grouping,
    refresh_data_now,
)

_LOGGER = logging.getLogger(__name__)

#: 主机名（RFC 1123 标签）或 IPv4 —— 足够覆盖 NTP 服务器的填写场景
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
#: 用户可能用逗号 / 中文逗号 / 分号 / 空白（含换行）分隔多个服务器
_SPLIT_RE = re.compile(r"[\s,，;；]+")


def _build_entities(hass, config_entry):
    """本平台的全部实体（纯函数，同步 service 需要一份完整清单）。"""
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(config_entry.entry_id)
    if not entry_data:
        return []
    entities = []
    if "vm" in entry_data.get("monitored_conditions", []):
        # 没开虚拟机监控时，批量分组没有意义
        entities.append(ESXiBatchGroupingText(config_entry))

    # NTP 服务器：每台主机一个（挂主机设备；关机时实体现有但置灰）
    if "vmhost" in entry_data.get("monitored_conditions", []):
        config = config_entry.data
        for host_key in entry_data.get("vmhost") or {}:
            entities.append(
                ESXiNtpServersText(hass, config, host_key, config_entry)
            )

    return entities


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up text platform."""
    entry_id = config_entry.entry_id
    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(hass, entry_id, "text", _build_entities, async_add_entities)


class ESXiBatchGroupingText(TextEntity):
    """批量操作的分组与间隔。

    ``"10,20"`` = 每 10 台一组、每组之间等 20 秒；
    ``"0,0"`` / ``"0"`` / 空 = 不启用（全并行，保持原有行为）。
    写入时会校验并归一化，非法输入直接拒绝、实体保留原值。
    """

    _attr_has_entity_name = True
    _attr_name = "批量分组"
    _attr_icon = "mdi:group"
    _attr_mode = TextMode.TEXT
    # ⚠️ text 平台的 min/max 限制的是**字符串长度**（不是数值上限）
    _attr_native_min = 0
    _attr_native_max = 16
    # 仅供前端表单做即时提示；真正校验在 async_set_native_value 里（服务调用不走 pattern）
    _attr_native_pattern = r"^\s*\d+\s*([,，]\s*\d*)?\s*$"

    def __init__(self, config_entry):
        """Initialize the grouping text entity."""
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id

    @property
    def unique_id(self):
        """Return a unique ID."""
        host = str(self._config_entry.data.get("host", "unknown")).replace(".", "_")
        return f"{host}_{self._entry_id}_batch_grouping"

    @property
    def device_info(self):
        """挂在主机设备下。"""
        return host_device_info(self.hass, self._config_entry)

    @property
    def native_value(self):
        """读最新的 options（重启后自然保持）。"""
        raw = current_options(self.hass, self._config_entry).get(
            GROUPING_OPTION, DEFAULT_GROUPING
        )
        return normalize_grouping(raw) or DEFAULT_GROUPING

    @property
    def extra_state_attributes(self):
        """把参数用法写在实体自己身上 —— 在 HA 里点开这个实体就能看到。

        （HA 的实体翻译只支持 name、没有 description 字段，所以说明放在这里最合适。）
        """
        size, interval = parse_grouping(self.native_value)
        if size > 0:
            current = f"每 {size} 台一批"
            current += f"，批间等 {interval} 秒" if interval > 0 else "，批间不停顿"
        else:
            current = "不启用：全部虚拟机一次性同时执行"

        return {
            "usage": "「组大小,间隔秒」；例如 3,10 = 每 3 台一组、每组之间等 10 秒",
            "disabled": "0 / 0,0 / 留空 = 不启用（默认值，全部一次性同时执行）",
            "current": current,
            "applies_to": "全部开机 / 全部关闭 / 全部快照 / 全部重启",
            "not_applies_to": "全部虚拟机截图（本地动作，与 ESXi 负载无关）",
            "note": "组大小需小于虚拟机总数才会真正分批（7 台却设 10，就只有一批）",
            "group_size": size,
            "interval": interval,
        }

    async def async_set_value(self, value):
        """校验 + 归一化后保存。

        ⚠️ text 平台的钩子是 ``async_set_value``（``async_set_native_value`` 是
        number 平台的），写错名字会直接落到 HA 的默认实现上抛 NotImplementedError。

        空值视为「不启用」（等价 ``0,0``）；其它非法输入抛 HomeAssistantError，
        HA 会把这条中文提示原文回给调用方，实体值保持不变。
        """
        text = "" if value is None else str(value).strip()
        normalized = DEFAULT_GROUPING if not text else normalize_grouping(text)
        if normalized is None:
            raise HomeAssistantError(
                "格式应为「组大小,间隔」且都填不小于 0 的整数（例如 10,10）；"
                f"收到的是 {value!r}"
            )

        update_option(self.hass, self._config_entry, GROUPING_OPTION, normalized)
        self.async_write_ha_state()
        _LOGGER.info(
            "批量分组已设为 %s（%s）",
            normalized,
            "未启用，全部并行" if normalized.startswith("0,") else "已启用分组与组间间隔",
        )


class ESXiNtpServersText(TextEntity):
    """主机 NTP 服务器列表（逗号分隔；留空 = 清空全部 NTP 服务器）。

    选择 text 平台而不是在选项流里做配置框的原因：NTP 是**主机运行状态**的一部分，
    与服务开关 / 策略选择器放在一起才完整；而且要能在仪表盘上直接查看和修改。
    """

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    # text 的 min/max 限的是字符串长度
    _attr_native_min = 0
    _attr_native_max = 255
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, config, host_key, config_entry):
        """Initialize the NTP text entity."""
        self.hass = hass
        self.config = config
        self._host_key = host_key
        self._config_entry = config_entry
        self._entry_id = config_entry.entry_id
        self._host_data = {}

    def update(self):
        """Refresh host snapshot（纯快照；数据由中央定时器统一拉取）。"""
        self._host_data = (
            self.hass.data[DOMAIN_DATA][self._entry_id]
            .get("vmhost", {})
            .get(self._host_key)
            or {}
        )

    @property
    def should_poll(self):
        """Polled."""
        return True

    @property
    def available(self):
        """Online host only."""
        return self._host_data.get("state") == "poweredOn"

    @property
    def name(self):
        """Short name."""
        return "NTP服务器"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return (
            f"{self.config['host'].replace('.', '_')}_{self._entry_id}"
            f"_text_ntp_servers_{self._host_key}"
        )

    @property
    def icon(self):
        """Icon."""
        return "mdi:clock-check-outline"

    @property
    def device_info(self):
        """Attach to host device."""
        return {
            "identifiers": {(DOMAIN, host_device_key(self._entry_id, self._host_key))},
            "name": DEFAULT_NAME,
            "manufacturer": "VMware ESXi",
            "model": "ESXi 主机",
        }

    @property
    def native_value(self):
        """Comma-joined server list (empty string when unconfigured)."""
        raw = self._host_data.get("ntp_servers")
        if raw in (None, "", "未配置"):
            return ""
        if isinstance(raw, str):
            return raw
        return ", ".join(str(item) for item in raw)

    @property
    def extra_state_attributes(self):
        """Usage hint + raw server count."""
        servers = [s for s in _SPLIT_RE.split(self.native_value) if s]
        return {
            "usage": "多个 NTP 服务器用逗号或换行分隔，例如 ntp.aliyun.com, pool.ntp.org",
            "allow_empty": "清空内容并提交 = 删除主机上全部 NTP 服务器",
            "server_count": len(servers),
            "timezone": self._host_data.get("ntp_timezone"),
        }

    async def async_set_value(self, value):
        """Validate then write the server list to ESXi."""
        text = "" if value is None else str(value).strip()
        servers = [item for item in _SPLIT_RE.split(text) if item]

        invalid = [
            item
            for item in servers
            if not (_IPV4_RE.match(item) or _HOSTNAME_RE.match(item))
        ]
        if invalid:
            raise HomeAssistantError(
                "NTP 服务器必须是 IP 地址或域名（逗号/换行分隔）；"
                f"以下内容无法识别：{', '.join(invalid)}"
            )
        if len(", ".join(servers)) > 255:
            raise HomeAssistantError("NTP 服务器列表过长（超过 255 字符）")

        conn_details = {
            "host": self.config["host"],
            "user": self.config["username"],
            "pwd": self.config["password"],
            "port": self.config["port"],
            "ssl": self.config["verify_ssl"],
        }
        target_host = self._host_data.get("original_name", self._host_key)
        ok = await self.hass.async_add_executor_job(
            set_ntp_servers, conn_details, target_host, servers
        )
        if not ok:
            raise HomeAssistantError("写入 ESXi NTP 配置失败，请查看日志")

        await refresh_data_now(self.hass, self._entry_id)
        self.async_schedule_update_ha_state(force_refresh=True)
        _LOGGER.info("主机 %s：NTP 服务器已设为 %s", self._host_key, servers)
