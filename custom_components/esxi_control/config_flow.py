"""Config flow for esxi_control integration."""
import logging
import voluptuous as vol
from yarl import URL

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_AGGREGATE_INTERVAL,
    CONF_CONSOLE_BASE_URL,
    CONF_DS_STATE,
    CONF_LIC_STATE,
    CONF_NOTIFY,
    CONF_SCREENSHOT_INTERVAL,
    CONF_SHORT_ENTITY_IDS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    DOMAIN_DATA,
    DEFAULT_PORT,
    DEFAULT_DS_STATE,
    DEFAULT_LIC_STATE,
    DEFAULT_AGGREGATE_INTERVAL,
    DEFAULT_SCREENSHOT_INTERVAL,
    DEFAULT_SHORT_ENTITY_IDS,
    DEFAULT_UPDATE_INTERVAL,
    MIN_AGGREGATE_INTERVAL,
    MAX_AGGREGATE_INTERVAL,
    MIN_SCREENSHOT_INTERVAL,
    MAX_SCREENSHOT_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    DATASTORE_STATES,
    LICENSE_STATES,
)
from .esxi import esx_connect, esx_disconnect
from .guest_store import get_manager


_LOGGER = logging.getLogger(__name__)


@config_entries.HANDLERS.register(DOMAIN)
class ESXiStatsFlowHandler(config_entries.ConfigFlow):
    """Config flow for ESXi Control."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        # 新版 HA：OptionsFlow 自己持有 config_entry（由框架注入），返回无参实例
        # 老版本：OptionsFlow 需要构造参数，只能把 entry 传进去
        if hasattr(config_entries.OptionsFlow, "config_entry"):
            return ESXiStatsOptionsFlow()
        return ESXiStatsOptionsFlow(config_entry)

    def __init__(self):
        """Initialize."""
        self._errors = {}

    async def async_step_user(
        self, user_input={}
    ):  # pylint: disable=dangerous-default-value
        """Handle a flow initialized by the user."""
        self._errors = {}
        if self.hass.data.get(DOMAIN):
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # Check if entered host is already in HomeAssistant
            existing = await self._check_existing(user_input["host"])
            if existing:
                return self.async_abort(reason="already_configured")

            # If it is not, continue with communication test
            valid = await self.hass.async_add_executor_job(
                self._test_communication,
                user_input["host"],
                user_input["port"],
                user_input["verify_ssl"],
                user_input["username"],
                user_input["password"],
            )
            if valid:
                return self.async_create_entry(
                    title=user_input["host"], data=user_input
                )
            else:
                self._errors["base"] = "communication"

            return await self._show_config_form(user_input)

        return await self._show_config_form(user_input)

    async def _show_config_form(self, user_input):
        """Show the configuration form to edit location data."""
        # Defaults
        host = ""
        port = DEFAULT_PORT
        username = ""
        password = ""
        verify_ssl = False
        vmhost = True
        datastore = True
        license = True
        vm = True

        if user_input is not None:
            if "host" in user_input:
                host = user_input["host"]
            if "port" in user_input:
                port = user_input["port"]
            if "username" in user_input:
                username = user_input["username"]
            if "password" in user_input:
                password = user_input["password"]
            if "verify_ssl" in user_input:
                verify_ssl = user_input["verify_ssl"]
            if "vmhost" in user_input:
                vmhost = user_input["vmhost"]
            if "datastore" in user_input:
                datastore = user_input["datastore"]
            if "license" in user_input:
                license = user_input["license"]
            if "vm" in user_input:
                vm = user_input["vm"]

        data_schema = {
            vol.Required("host", default=host): str,
            vol.Required("port", default=port): int,
            vol.Required("username", default=username): str,
            vol.Required("password", default=password): str,
            vol.Optional("verify_ssl", default=verify_ssl): bool,
            vol.Optional("vmhost", default=vmhost): bool,
            vol.Optional("datastore", default=datastore): bool,
            vol.Optional("license", default=license): bool,
            vol.Optional("vm", default=vm): bool,
        }
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(data_schema), errors=self._errors
        )

    async def async_step_import(self, user_input):
        """Import a config entry.

        Special type of import, we're not actually going to store any data.
        Instead, we're going to rely on the values that are in config file.
        """
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return self.async_create_entry(title="configuration.yaml", data={})

    async def _check_existing(self, host):
        for entry in self._async_current_entries():
            if host == entry.data.get("host"):
                return True

    def _test_communication(self, host, port, verify_ssl, username, password):
        """Return true if the communication is ok."""
        try:
            conn = esx_connect(host, username, password, port, verify_ssl)
            _LOGGER.debug(conn)

            esx_disconnect(conn)
            return True
        except Exception as exception:  # pylint: disable=broad-except
            _LOGGER.error(exception)
            return False


class ESXiStatsOptionsFlow(config_entries.OptionsFlow):
    """Handle ESXi Control options."""

    def __init__(self, config_entry=None):
        """Initialize ESXi Control options flow.

        新版 HA 的 OptionsFlow 自己持有 config_entry（框架注入的只读属性），
        但基类 __init__ 里初始化了 flow 的内部状态，所以这里必须调 super()；
        漏掉它会让框架读取 flow.config_entry 时抛异常，表现为「配置」按钮 500。
        老版本的 OptionsFlow.__init__ 需要 config_entry 参数，会抛 TypeError，忽略即可。
        """
        try:
            super().__init__()
        except TypeError:
            pass
        self._legacy_entry = config_entry
        #: 来宾凭据流程里当前选中的 VM key
        self._cred_vm = None

    @property
    def _entry(self):
        """Return the config entry regardless of HA version."""
        try:
            entry = self.config_entry
        except Exception:  # pylint: disable=broad-except
            # 老版本没有该属性，或框架尚未注入
            entry = None
        return entry or self._legacy_entry

    async def async_step_init(self, user_input=None):
        """选项入口：菜单（通用设置 / 来宾凭据）。"""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "guest_creds"],
        )

    async def async_step_general(self, user_input=None):
        """Manage ESXi Control Options."""
        options = dict(getattr(self._entry, "options", {}) or {})
        errors = {}
        if user_input is not None:
            console_base = (user_input.get(CONF_CONSOLE_BASE_URL) or "").strip()
            if console_base:
                try:
                    parsed = URL(console_base)
                except ValueError:
                    parsed = None
                if (
                    parsed is None
                    or parsed.scheme not in ("http", "https")
                    or not parsed.host
                ):
                    errors["base"] = "invalid_console_base"
            if not errors:
                options[CONF_DS_STATE] = user_input[CONF_DS_STATE]
                options[CONF_LIC_STATE] = user_input[CONF_LIC_STATE]
                options[CONF_NOTIFY] = user_input[CONF_NOTIFY]
                options[CONF_SCREENSHOT_INTERVAL] = user_input[CONF_SCREENSHOT_INTERVAL]
                options[CONF_SHORT_ENTITY_IDS] = user_input.get(
                    CONF_SHORT_ENTITY_IDS, DEFAULT_SHORT_ENTITY_IDS
                )
                options[CONF_AGGREGATE_INTERVAL] = user_input.get(
                    CONF_AGGREGATE_INTERVAL, DEFAULT_AGGREGATE_INTERVAL
                )
                options[CONF_UPDATE_INTERVAL] = user_input.get(
                    CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                )
                # 留空=自动（跟随页面实际访问来源 / HA 外部 URL）
                options[CONF_CONSOLE_BASE_URL] = console_base
                # options flow 里不要再传 title，标题由 entry 决定
                entry_result = self.async_create_entry(data=options)
                # 立即按新基地址重签设备页「访问」链接（首次迭代立即执行）
                try:
                    from .console_web import schedule_initial_link

                    schedule_initial_link(self.hass, self._entry.entry_id)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("控制台访问地址变更后安排重签失败")
                return entry_result

        return self.async_show_form(
            step_id="general",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CONSOLE_BASE_URL,
                        default=options.get(CONF_CONSOLE_BASE_URL, ""),
                    ): str,
                    vol.Optional(
                        CONF_DS_STATE,
                        default=options.get(CONF_DS_STATE, DEFAULT_DS_STATE),
                    ): vol.In(DATASTORE_STATES),
                    vol.Optional(
                        CONF_LIC_STATE,
                        default=options.get(CONF_LIC_STATE, DEFAULT_LIC_STATE),
                    ): vol.In(LICENSE_STATES),
                    vol.Optional(
                        CONF_NOTIFY,
                        default=options.get(CONF_NOTIFY, True),
                    ): bool,
                    vol.Optional(
                        CONF_SCREENSHOT_INTERVAL,
                        default=options.get(
                            CONF_SCREENSHOT_INTERVAL, DEFAULT_SCREENSHOT_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCREENSHOT_INTERVAL, max=MAX_SCREENSHOT_INTERVAL),
                    ),
                    vol.Optional(
                        CONF_SHORT_ENTITY_IDS,
                        default=options.get(
                            CONF_SHORT_ENTITY_IDS, DEFAULT_SHORT_ENTITY_IDS
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_AGGREGATE_INTERVAL,
                        default=options.get(
                            CONF_AGGREGATE_INTERVAL, DEFAULT_AGGREGATE_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_AGGREGATE_INTERVAL, max=MAX_AGGREGATE_INTERVAL),
                    ),
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=options.get(
                            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                    ),
                }
            ),
        )

    # ------------------------------------------------------------------ #
    # 来宾凭据（每台虚拟机一套客户机操作系统的账号/密码，供 Guest Operations）
    # ------------------------------------------------------------------ #
    _DONE = "__done__"

    def _guest_choices(self):
        """构造 VM 下拉选项：已配置的打 ✓，末尾加「完成」。"""
        entry_data = self.hass.data.get(DOMAIN_DATA, {}).get(self._entry.entry_id) or {}
        manager = get_manager(self.hass)
        configured = (
            manager.configured_vms(self._entry.entry_id) if manager else set()
        )
        choices = {}
        for vm_key, vm_data in (entry_data.get("vm") or {}).items():
            label = vm_data.get("vm_name", vm_key)
            if vm_key in configured:
                label = f"✓ {label}"
            choices[vm_key] = label
        # 已配置但当前监控快照里没有的 VM（比如暂时关机未同步），也要能进去删除
        for vm_key in sorted(configured - set(choices)):
            choices[vm_key] = f"✓ {vm_key}（当前不在监控数据中）"
        choices[self._DONE] = "— 完成 —"
        return choices

    async def async_step_guest_creds(self, user_input=None):
        """选择一台虚拟机来配置它的来宾凭据。"""
        if user_input is not None:
            chosen = user_input.get("vm")
            if chosen == self._DONE:
                # 凭据走独立存储，不碰 options；这里以原 options 收尾即可
                return self.async_create_entry(
                    data=dict(getattr(self._entry, "options", {}) or {})
                )
            self._cred_vm = chosen
            return await self.async_step_guest_edit()

        choices = self._guest_choices()
        if len(choices) == 1:  # 只有「完成」
            return self.async_show_form(
                step_id="guest_creds",
                data_schema=vol.Schema({vol.Required("vm"): vol.In(choices)}),
                description_placeholders={"hint": "当前监控数据中没有虚拟机。请先确认集成已启用「虚拟机」监控并完成一次数据同步。"},
            )
        return self.async_show_form(
            step_id="guest_creds",
            data_schema=vol.Schema(
                {vol.Required("vm", default=self._DONE): vol.In(choices)}
            ),
            description_placeholders={
                "hint": "带 ✓ 的是已配置凭据的虚拟机。选择一台后填写客户机操作系统（Windows/Linux）的登录账号；密码保存在集成的私有存储中，不会出现在任何实体或日志里。"
            },
        )

    async def async_step_guest_edit(self, user_input=None):
        """编辑选中虚拟机的用户名/密码/域，或删除其凭据。"""
        vm_key = self._cred_vm
        manager = get_manager(self.hass)
        if vm_key is None or manager is None:
            return await self.async_step_guest_creds()

        existing = manager.get_cred(self._entry.entry_id, vm_key) or {}

        if user_input is not None:
            if user_input.get("remove"):
                await manager.async_remove_cred(self._entry.entry_id, vm_key)
            else:
                try:
                    await manager.async_set_cred(
                        self._entry.entry_id,
                        vm_key,
                        user_input.get("username", "").strip(),
                        user_input.get("password", ""),
                        user_input.get("domain", "").strip(),
                    )
                except ValueError as error:
                    return self.async_show_form(
                        step_id="guest_edit",
                        data_schema=self._guest_edit_schema(existing),
                        errors={"base": "invalid_cred"},
                        description_placeholders={
                            "vm": vm_key,
                            "hint": str(error),
                        },
                    )
            self._cred_vm = None
            return await self.async_step_guest_creds()

        return self.async_show_form(
            step_id="guest_edit",
            data_schema=self._guest_edit_schema(existing),
            description_placeholders={
                "vm": vm_key,
                "hint": "这里填的是 VMware Tools 通道的客户机账号。密码框留空表示保持已保存的密码不变；"
                        "勾选「删除凭据」会清掉这台 VM 的全部配置（含 SSH）。"
                        "要走 SSH 通道请到「来宾」页面的「系统连接设置」里配。",
            },
        )

    def _guest_edit_schema(self, existing):
        """guest_edit 表单（existing 为整条记录；这里只编辑 VMware Tools 那套）。

        ⚠️ 存储结构是 ``{"transport", "tools": {...}, "ssh": {...}}``，
        这里只取 ``tools`` 子字典；SSH 通道的 IP/端口/账号在「来宾」页面的
        「系统连接设置」里配（那里能顺手点「测试连接」）。
        """
        tools = (existing or {}).get("tools") or {}
        return vol.Schema(
            {
                vol.Required(
                    "username", default=tools.get("username", "")
                ): str,
                vol.Optional("password", default=""): str,
                vol.Optional("domain", default=tools.get("domain", "")): str,
                vol.Optional("remove", default=False): bool,
            }
        )
