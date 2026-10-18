"""ESXi Control Integration."""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from pyVmomi import vim  # pylint: disable=no-name-in-module
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

try:  # 服务响应（2023.x+ 为枚举，老版本只认布尔）
    from homeassistant.core import SupportsResponse as _SupportsResponse

    _SERVICE_RESPONSE = _SupportsResponse.ONLY
except ImportError:  # pragma: no cover - 老版本兼容
    _SERVICE_RESPONSE = True
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity_component import async_update_entity

from homeassistant.const import (
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_VERIFY_SSL,
    __version__ as HAVERSION,
)

from .esxi import (
    esx_connect,
    esx_disconnect,
    check_license,
    get_host_info,
    get_datastore_info,
    get_license_info,
    get_vm_info,
    get_vms_perf,
    host_pwr,
    host_pwr_policy,
    vm_pwr,
    vm_snap_take,
    vm_snap_remove,
    list_esxi_hosts,
    list_power_policies,
    vm_nic_set,
    vm_consolidate,
    vm_autostart_set,
    host_autostart_set_enabled,
)
from .cdrom_ops import upload_and_mount, eject_iso
from .vm_bulk import refresh_data_now

from .const import (
    AVAILABLE_CMND_VM_SNAP,
    AVAILABLE_CMND_VM_POWER,
    AVAILABLE_CMND_HOST_POWER,
    COMMAND,
    CONF_SHORT_ENTITY_IDS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_SHORT_ENTITY_IDS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    DOMAIN_DATA,
    EVENT_HUB_KEY,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    PLATFORMS,
    REQUIRED_FILES,
    HOST,
    TARGET_HOST,
    VM,
    FORCE,
    VM_PERF_KEYS,
    ISO_UPLOAD_DIR,
)

from .entity_sync import async_sync_entry, clear_platform_hooks
from .entry_options import current_options
from .naming import apply_short_entity_ids
from .screenshot_hub import HUB_KEY
from .guest_store import get_manager
from .guest_ops import GuestOpsError
from . import guest_router


_LOGGER = logging.getLogger(__name__)

# 数据刷新间隔不再用 @Throttle：它的间隔在导入时就固定死了，而这个间隔现在必须
# 能在界面上调（「数据刷新间隔」number 实体）。节流逻辑移进 EsxiStats.update_data()。

HOST_PWR_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Optional(TARGET_HOST): cv.string,
        vol.Required(COMMAND): cv.string,
        vol.Required(FORCE): cv.boolean,
    }
)
LIST_HOSTS_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
    }
)
LIST_POWER_POLICIES_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Optional(TARGET_HOST): cv.string,
    }
)
HOST_PWR_POLICY_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(COMMAND): cv.string,
        vol.Optional(TARGET_HOST): cv.string,
    }
)
VM_PWR_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Required(COMMAND): cv.string,
    }
)
# 网卡连接 / 断开（热操作）
VM_NIC_SET_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Required("mac"): cv.string,
        vol.Required("connected"): cv.boolean,
    }
)
# 合并快照磁盘
VM_CONSOLIDATE_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
    }
)
# 虚拟机加入 / 移出主机自启动序列
VM_AUTOSTART_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Required("enabled"): cv.boolean,
    }
)
# 主机自启动总开关
HOST_AUTOSTART_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required("enabled"): cv.boolean,
        vol.Optional(TARGET_HOST): cv.string,
    }
)
# 挂载本地 ISO（路径在 HA 主机/容器内可访问）
VM_MOUNT_ISO_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Required("local_path"): cv.string,
        vol.Optional("datastore_dir", default=ISO_UPLOAD_DIR): cv.string,
    }
)
# 弹出光盘；delete_file=true 时同时删除当前挂载 ISO 的数据存储文件
VM_EJECT_ISO_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Optional("delete_file", default=False): cv.boolean,
    }
)
SNAP_CREATE_SCHEMA = vol.Schema(
    {vol.Required(HOST): cv.string, vol.Required(VM): cv.string}, extra=vol.ALLOW_EXTRA
)
SNAP_REMOVE_SCHEMA = vol.Schema(
    {
        vol.Required(HOST): cv.string,
        vol.Required(VM): cv.string,
        vol.Required(COMMAND): cv.string,
    }
)
SYNC_SCHEMA = vol.Schema(
    {
        # 不传 = 同步所有 ESXi Control 配置条目（通常只有一个）
        vol.Optional("entry_id"): cv.string,
    }
)
REFRESH_SCHEMA = vol.Schema(
    {
        # 不传 = 刷新所有 ESXi Control 配置条目
        vol.Optional("entry_id"): cv.string,
    }
)

# --------------------------------------------------------------------------- #
# 来宾操作（Guest Operations）服务 schema
#   所有服务都以 host（ESXi 主机）+ vm（虚拟机名）定位目标，凭据来自集成选项里
#   为该 VM 单独保存的来宾账号密码，服务本身不收密码。
# --------------------------------------------------------------------------- #
_GUEST_BASE = {
    vol.Required(HOST): cv.string,
    vol.Required(VM): cv.string,
}
GUEST_VALIDATE_SCHEMA = vol.Schema(dict(_GUEST_BASE))
GUEST_LIST_PROCESSES_SCHEMA = vol.Schema(
    {**_GUEST_BASE, vol.Optional("pids"): [int]}
)
GUEST_START_PROGRAM_SCHEMA = vol.Schema(
    {
        **_GUEST_BASE,
        vol.Required("program_path"): cv.string,
        vol.Optional("arguments", default=""): cv.string,
        vol.Optional("working_directory", default=""): cv.string,
        vol.Optional("env_variables"): [cv.string],
    }
)
GUEST_TERMINATE_PROCESS_SCHEMA = vol.Schema(
    {**_GUEST_BASE, vol.Required("pid"): int}
)
GUEST_LIST_FILES_SCHEMA = vol.Schema(
    {
        **_GUEST_BASE,
        vol.Optional("path", default="/"): cv.string,
        vol.Optional("max_results", default=200): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
    }
)
GUEST_PATH_SCHEMA = vol.Schema({**_GUEST_BASE, vol.Required("path"): cv.string})
GUEST_MAKE_DIR_SCHEMA = vol.Schema(
    {**_GUEST_BASE, vol.Required("path"): cv.string, vol.Optional("create_parents", default=True): bool}
)
GUEST_REMOVE_SCHEMA = vol.Schema(
    {**_GUEST_BASE, vol.Required("path"): cv.string, vol.Optional("recursive", default=True): bool}
)
GUEST_UPLOAD_SCHEMA = vol.Schema(
    {
        **_GUEST_BASE,
        vol.Required("local_path"): cv.string,
        vol.Required("guest_path"): cv.string,
        vol.Optional("overwrite", default=True): bool,
    }
)
GUEST_DOWNLOAD_SCHEMA = vol.Schema(
    {
        **_GUEST_BASE,
        vol.Required("guest_path"): cv.string,
        vol.Required("local_path"): cv.string,
    }
)
GUEST_ALIASES_SCHEMA = vol.Schema(
    {**_GUEST_BASE, vol.Optional("guest_username"): cv.string}
)
GUEST_REGISTRY_SCHEMA = vol.Schema(
    {
        **_GUEST_BASE,
        vol.Required("key_path"): cv.string,
        vol.Optional("recursive", default=False): bool,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({}, extra=vol.ALLOW_EXTRA)}, extra=vol.ALLOW_EXTRA
)


async def _async_write_entity_states(hass, entry):
    """催该配置条目下的实体立刻重写一次状态。

    数据刷新只换了 ``hass.data`` 里的内容，实体的 state 要等下一次平台轮询
    （sensor 平台 15 秒、节流后实际 45 秒）才反映出来。手动刷新最要紧的就是
    "点完立刻看见"，所以这里主动推一把。

    个别实体可能不支持主动更新（例如 should_poll 为 False），失败只记 debug。
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.disabled_by:
            continue
        try:
            await async_update_entity(hass, reg_entry.entity_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("刷新：%s 状态重写失败", reg_entry.entity_id, exc_info=True)


@callback
def async_register_maintenance_services(hass):
    """注册本集成的两个维护服务：``esxi_control.sync`` / ``esxi_control.refresh``。

    刻意不放进 ``async_add_services``：那个只在 license 支持写 API 时才注册，
    而「同步实体」纯粹是本地的注册表增删、「立即刷新数据」只是多拉一次数据，
    两者都与 ESXi 写权限无关 —— 所以无条件注册。

    两者分工：
      · ``sync``    —— 重新扫描 ESXi 的**对象清单**，为新增对象建实体、移除已消失的；
      · ``refresh`` —— 只把**数据**拉新一次（越过 45 秒节流），不碰任何实体。
    """
    if not hass.services.has_service(DOMAIN, "sync"):

        async def async_sync(call):
            target = call.data.get("entry_id")
            entries = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if not target or entry.entry_id == target
            ]
            if not entries:
                _LOGGER.warning("同步：没有找到可同步的 ESXi Control 配置条目")
                return
            for entry in entries:
                result = await async_sync_entry(hass, entry)
                _LOGGER.info(
                    "同步完成（%s）：新增 %s 个实体、移除 %s 个实体%s",
                    entry.title or entry.entry_id,
                    result.get("added", 0),
                    result.get("removed", 0),
                    f"（{result['reason']}）" if result.get("reason") else "",
                )

        hass.services.async_register(DOMAIN, "sync", async_sync, schema=SYNC_SCHEMA)

    if not hass.services.has_service(DOMAIN, "refresh"):

        async def async_refresh(call):
            """立刻向 ESXi 拉一次最新数据（不等 45 秒节流），并让实体马上写状态。"""
            target = call.data.get("entry_id")
            entries = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if not target or entry.entry_id == target
            ]
            if not entries:
                _LOGGER.warning("刷新：没有找到可刷新的 ESXi Control 配置条目")
                return
            for entry in entries:
                entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry.entry_id) or {}
                client = entry_data.get("client")
                if client is None:
                    continue
                # 直接调用不带节流的内核：手动刷新的意义就是"现在就要最新值"。
                # 代价是随后一次常规轮询可能还会再拉一次（节流计时不被这次重置），
                # 属于可接受的重复。
                await hass.async_add_executor_job(client._update_data)  # pylint: disable=protected-access
                if not client.last_update_ok:
                    _LOGGER.warning(
                        "刷新（%s）：数据拉取失败，ESXi 可能不可达，本次不更新实体",
                        entry.title or entry.entry_id,
                    )
                    continue
                await _async_write_entity_states(hass, entry)

        hass.services.async_register(
            DOMAIN, "refresh", async_refresh, schema=REFRESH_SCHEMA
        )


async def async_setup_entry(hass, config_entry):
    """Set up this integration using UI."""
    conf = hass.data.get(DOMAIN_DATA)
    if config_entry.source == config_entries.SOURCE_IMPORT:
        if conf is None:
            hass.async_create_task(
                hass.config_entries.async_remove(config_entry.entry_id)
            )
        # This is using YAML for configuration
        return False

    # check all required files
    file_check = await hass.async_add_executor_job(check_files, hass)
    if not file_check:
        return False

    config = {DOMAIN: config_entry.data}
    entry = config_entry.entry_id

    # create data dictionary
    if DOMAIN_DATA not in hass.data:
        hass.data[DOMAIN_DATA] = {}
    hass.data[DOMAIN_DATA][entry] = {}
    hass.data[DOMAIN_DATA][entry]["configuration"] = "config_flow"
    hass.data[DOMAIN_DATA][entry]["vmhost"] = {}
    hass.data[DOMAIN_DATA][entry]["datastore"] = {}
    hass.data[DOMAIN_DATA][entry]["license"] = {}
    hass.data[DOMAIN_DATA][entry]["vm"] = {}
    hass.data[DOMAIN_DATA][entry]["monitored_conditions"] = []

    if config_entry.data["vmhost"]:
        hass.data[DOMAIN_DATA][entry]["monitored_conditions"].append("vmhost")
    if config_entry.data["datastore"]:
        hass.data[DOMAIN_DATA][entry]["monitored_conditions"].append("datastore")
    if config_entry.data["license"]:
        hass.data[DOMAIN_DATA][entry]["monitored_conditions"].append("license")

    if config_entry.data["vm"]:
        hass.data[DOMAIN_DATA][entry]["monitored_conditions"].append("vm")

    if not config_entry.options:
        async_update_options(hass, config_entry)

    # get global config
    _LOGGER.debug("Setting up host %s", config[DOMAIN].get(CONF_HOST))
    client = EsxiStats(hass, config, config_entry)
    hass.data[DOMAIN_DATA][entry]["client"] = client

    # 控制台链接重签必须尽早开始：HA 每次重启会换掉 authSig 的内存签名密钥，
    #   设备注册表里的旧链接在本进程重签完成前打开一律 401。connect() 实测可能
    #   耗时近一分钟，平台 setup 更晚，所以不能等到它们之后再签；设备记录是跨
    #   重启持久存在的，第一轮通常就能直接签上。任务内部幂等且会自行重试。
    from .console_web import async_load_learned_bases, schedule_initial_link

    # 先读回历史学到的浏览器访问地址（端口映射场景下至关重要），再启动重签
    try:
        await async_load_learned_bases(hass)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("加载控制台访问地址记忆失败")
    schedule_initial_link(hass, entry)

    # connect 失败会抛 ConfigEntryNotReady —— 那时不能先挂定时器，否则重试
    #   setup 会留下一个永远取消不掉的旧定时器（旧 client 一直连 ESXi）
    lic = await hass.async_add_executor_job(connect, hass, config, entry)

    # 唯一的数据拉取源：中央定时器在 executor 线程按「数据刷新间隔」（默认 45 秒，
    #   tick 10 秒 + client 内节流）拉 ESXi。各实体的 update() 只读 hass.data
    #   快照，绝不再自己触发网络请求 —— 否则几十个实体的 2~15 秒轮询会叠在同一条
    #   全量拉取上（129 台 VM 时单次拉取超过 10 秒），堵死实体更新线程池并刷告警。
    async def _central_refresh(_now=None):
        await hass.async_add_executor_job(client.update_data)

    unsub_refresh = async_track_time_interval(
        hass, _central_refresh, timedelta(seconds=MIN_UPDATE_INTERVAL)
    )
    hass.data[DOMAIN_DATA][entry]["unsub_refresh"] = unsub_refresh

    # 维护类服务（同步实体 / 立即刷新数据）与 license 写权限无关，无条件注册
    async_register_maintenance_services(hass)

    # 来宾凭据管理器（每台 VM 的客户机账号密码 + 校验缓存），平台构建实体要用，
    #   必须在 forward_entry_setups 之前初始化
    from .guest_store import async_setup_manager

    await async_setup_manager(hass)

    # 全局「远程桌面」总开关的状态（HA 私有 Store，默认开启）：必须在平台加载前
    #   读回来 —— switch 平台要用它建那个开关，否则首轮会先显示默认值再纠正。
    from .console_web import async_load_remote_state

    await async_load_remote_state(hass)

    # ESXi 事件流中枢（EventManager 轮询 → 事件传感器 / HA 总线 / 日志簿）。
    #   必须在 sensor 平台加载之前就位：事件传感器是推送式的，注册时就要拿到
    #   中枢并订阅，否则首轮回填事件没人通知它刷新。
    from .event_hub import async_setup_events

    await async_setup_events(hass, config_entry)

    # load platforms
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # 虚拟机控制台：注册托管网页 / 票据 API / 同源 WebSocket 代理，并给每台 VM
    #   设备写 configuration_url（幂等；view 全局只注册一次）
    from .console_web import async_setup_console

    await async_setup_console(hass, config_entry)

    # 平台加载完成后统一应用短且固定的 entity_id（幂等，已一致的会跳过）
    if config_entry.options.get(CONF_SHORT_ENTITY_IDS, DEFAULT_SHORT_ENTITY_IDS):
        apply_short_entity_ids(hass, config_entry)

    # 清理本条目下已无任何实体引用的设备（例如早期版本留下的「ESXi 统计」）
    hass.async_create_task(_async_remove_orphan_devices(hass, config_entry))

    # if lisense allows API write, register services
    if lic:
        async_add_services(hass, config_entry)
    else:
        _LOGGER.info(
            "Service calls are disabled - %s doesn't have a supported license",
            config[DOMAIN]["host"],
        )

    return True


async def _async_remove_orphan_devices(hass, config_entry):
    """移除本条目下已没有任何实体引用的设备。

    历史遗留（如「ESXi 统计」）会把数据存储传感器挂在一个单独设备上，
    这些传感器现在归属主机设备，旧设备就成了空壳，只能从注册表里清掉。
    """
    # 等各平台把实体注册完，否则可能误删实体尚未落地的设备
    await asyncio.sleep(20)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    removed = []
    for device in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id):
        if er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        ):
            continue
        removed.append(device.name or device.id)
        dev_reg.async_remove_device(device.id)

    if removed:
        _LOGGER.info("已移除无实体引用的设备: %s", ", ".join(removed))


def connect(hass, config, entry):
    """Connect."""
    conn = None
    try:
        conn_details = {
            "host": config[DOMAIN]["host"],
            "user": config[DOMAIN]["username"],
            "pwd": config[DOMAIN]["password"],
            "port": config[DOMAIN]["port"],
            "ssl": config[DOMAIN]["verify_ssl"],
        }

        conn = esx_connect(**conn_details)
        if conn:
            _LOGGER.debug("Product Line: %s", conn.content.about.productLineId)

            # get license type and objects
            lic = check_license(conn.RetrieveContent().licenseManager)
            hass.data[DOMAIN_DATA][entry]["client"].update_data()
        else:
            lic = "n/a"
    except Exception as exception:  # pylint: disable=broad-except
        _LOGGER.error(exception)
        raise ConfigEntryNotReady from exception
    finally:
        if conn:
            esx_disconnect(conn)

    return lic


class EsxiStats:
    """This class handles communication, services, and stores the data."""

    def __init__(self, hass, config, config_entry=None):
        """Initialize the class."""
        self.hass = hass
        self.config = config[DOMAIN]
        self.host = config[DOMAIN].get(CONF_HOST)
        self.user = config[DOMAIN].get(CONF_USERNAME)
        self.passwd = config[DOMAIN].get(CONF_PASSWORD)
        self.port = config[DOMAIN].get(CONF_PORT)
        self.ssl = config[DOMAIN].get(CONF_VERIFY_SSL)
        self.entry = config_entry.entry_id
        # 保留配置条目对象：读「数据刷新间隔」等 options 时要拿最新的那一份
        self.config_entry = config_entry
        # 上一次真正向 ESXi 拉数据的时间（单调时钟），节流用
        self._last_update = 0.0
        # 正在拉数据？（拉一次全量要好几秒 —— 间隔设得比它小的时候，实体轮询会叠着连 ESXi）
        self._updating = False
        # 上一次数据刷新是否成功：同步 / 立即刷新 service 靠它决定「能不能动实体」
        self.last_update_ok = False
        # 诊断用：上一次刷新的结束时刻与耗时（秒），以及连续失败次数
        self.last_refresh_at = 0.0
        self.last_refresh_seconds = None
        self._consecutive_failures = 0

    def update_interval(self):
        """当前生效的数据刷新间隔（秒），由「数据刷新间隔」实体决定。

        每次都读最新 options，所以实体一改就生效（不需要重载集成）；
        取值被夹在 MIN / MAX 之间，非法值回落到默认值。
        """
        options = current_options(self.hass, self.config_entry)
        raw = options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_UPDATE_INTERVAL
        return max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, value))

    def update_data(self):
        """带节流的入口：距上次真正刷新不足「数据刷新间隔」秒时直接返回。

        为什么不再用 ``@Throttle``：它的间隔在导入时就固定死了，而这里必须能在界面上
        调（见「数据刷新间隔」number 实体）。节流语义与 ``@Throttle`` 保持一致 ——
        被挡下时调用方继续读 ``hass.data`` 里的旧快照（各实体的 update 都是
        "先调 update_data()、再从 hass.data 取数"）。

        ⚠️ 另外要防重叠：拉一次全量要好几秒（连 ESXi + 遍历全部主机/虚拟机/数据存储），
        而实体轮询每隔几秒就来一次 —— 间隔一旦设得比拉取耗时还小，就会有多个任务
        叠着去连 ESXi。上一次没跑完时直接返回（此时读到的是上一次的结果，与节流同义）。
        """
        if self._updating:
            return
        interval = self.update_interval()
        now = time.monotonic()
        if self._last_update and (now - self._last_update) < interval:
            return
        self._last_update = now
        self._updating = True
        try:
            self._update_data()
        finally:
            self._updating = False

    def _update_data(self):
        """实际的数据刷新（不带节流）。

        刻意与 ``update_data`` 拆开：实体轮询走节流版；而同步 / 立即刷新 service
        需要「立刻拿到最新数据」，它们直接调用这个内核绕过节流。
        """
        started = time.monotonic()
        self.last_update_ok = False
        conn = None
        try:
            # connect and get data from host
            conn = esx_connect(self.host, self.user, self.passwd, self.port, self.ssl)
            content = conn.RetrieveContent()
        except Exception as error:  # pylint: disable=broad-except
            # ⚠️ 这里必须让人看得见：拉不到数据时实体值会**停在旧值**，界面上表现为
            #   "刷新没生效"，唯一的线索就是这条日志 —— 压成 debug 会让这类问题隐形。
            #   为不刷屏：只在"由正常转为失败"时 warning 一次，持续失败降到 debug，
            #   恢复时给一条 info。
            self._consecutive_failures += 1
            if self._consecutive_failures == 1:
                _LOGGER.warning(
                    "向 ESXi 拉取数据失败（实体值会停在旧值，看起来像没刷新）：%s",
                    error,
                )
            else:
                _LOGGER.debug(
                    "向 ESXi 拉取数据再次失败（连续第 %s 次）：%s",
                    self._consecutive_failures,
                    error,
                )
        else:
            # 每轮都重建子字典（否则在 ESXi 上删除的对象会留下僵尸键：实体继续读到
            #   删除前的旧值，看起来一切正常、也永远不会变成不可用）。
            #
            # ⚠️ 但要**先构建好再一次性整体替换**，绝不能原地 clear 之后慢慢填：
            #   实体轮询是并发的，而「清空 → 填满」之间要连 ESXi 拉全量数据（好几秒），
            #   这期间任何一次并发 poll 都会读到空字典，表现为满屏
            #   "VM xxx not found in data" 报错 + 实体瞬间不可用。整体替换只是一次
            #   引用赋值，对并发读者是原子的。
            entry_data = self.hass.data[DOMAIN_DATA][self.entry]
            vmhost_data = {}
            datastore_data = {}
            license_data = {}
            vm_data = {}

            # get host stats
            if self.config.get("vmhost") is True:
                # create/destroy view objects
                host_objview = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.HostSystem], True
                )
                esxi_hosts = host_objview.view
                host_objview.Destroy()

                # Look through object list and get data
                _LOGGER.debug("Found %s host(s)", len(esxi_hosts))
                for esxi_host in esxi_hosts:
                    host_name = esxi_host.summary.config.name.replace(" ", "_").lower()

                    _LOGGER.debug("Getting stats for vmhost: %s", host_name)
                    vmhost_data[host_name] = get_host_info(esxi_host, content)

            # get datastore stats
            if self.config.get("datastore") is True:
                # create/destroy view objects
                ds_objview = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.Datastore], True
                )
                ds_list = ds_objview.view
                ds_objview.Destroy()

                # Look through object list and get data
                _LOGGER.debug("Found %s datastore(s)", len(ds_list))
                for datastore in ds_list:
                    ds_name = datastore.summary.name.replace(" ", "_").lower()

                    _LOGGER.debug("Getting stats for datastore: %s", ds_name)
                    datastore_data[ds_name] = get_datastore_info(datastore)

            # get license stats
            if self.config.get("license") is True:
                lic_list = content.licenseManager

                # Get all hosts for better context
                host_objview = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.HostSystem], True
                )
                esxi_hosts = host_objview.view
                host_objview.Destroy()

                _LOGGER.debug("Found %s license(s) and %s host(s)", len(lic_list.licenses), len(esxi_hosts))

                # Collect host names for reference
                host_names = []
                for esxi_host in esxi_hosts:
                    host_names.append({
                        'name': esxi_host.summary.config.name.replace(" ", "_").lower(),
                        'original_name': esxi_host.summary.config.name
                    })

                # Process each license and assign meaningful names
                vcenter_license_count = 0
                esxi_license_count = 0
                other_license_count = 0
                processed_license_keys = set()  # Track processed license keys to avoid duplicates
                valid_licenses = []  # Collect valid licenses first (skip only clearly invalid products)

                # First pass: collect all valid licenses (skip only clearly invalid ones)
                for lic in lic_list.licenses:
                    product_name = None  # Start with None to detect missing ProductName
                    license_key = getattr(lic, 'licenseKey', None) or getattr(lic, 'name', None)
                    license_name = getattr(lic, 'name', '')

                    for key in lic.properties:
                        if key.key == "ProductName":
                            product_name = key.value
                            break

                    _LOGGER.debug("Checking license: name='%s', product='%s'", license_name, product_name)

                    # Skip licenses without a valid ProductName (will result in product='n/a' in entity)
                    if product_name is None or product_name == "n/a":
                        _LOGGER.warning("Filtering out invalid license: name='%s', product='%s'", license_name, product_name)
                        continue

                    valid_licenses.append(lic)

                # Second pass: process valid licenses
                for lic in valid_licenses:
                    # Determine product type for better naming
                    product_name = "unknown"
                    license_key = getattr(lic, 'licenseKey', None) or getattr(lic, 'name', None)
                    license_name = getattr(lic, 'name', '')

                    for key in lic.properties:
                        if key.key == "ProductName":
                            product_name = key.value
                            break

                    product_name_lower = product_name.lower()

                    # Skip if we've already processed this license key (same license used by multiple hosts)
                    if license_key and license_key in processed_license_keys:
                        continue

                    # Determine entity name based on product and environment
                    if "vcenter" in product_name_lower or "vpx" in product_name_lower or "virtualcenter" in product_name_lower:
                        # vCenter Server license - create one entity
                        entity_name = "vcenter_license"
                        associated_host = self.host  # vCenter server itself

                        # Mark this license key as processed
                        if license_key:
                            processed_license_keys.add(license_key)

                        _LOGGER.debug("Created vCenter license entity")
                        license_data[entity_name] = get_license_info(
                            lic, associated_host
                        )

                    elif ("esx" in product_name_lower or
                          "vmware_esx" in product_name_lower or
                          product_name_lower.startswith("vmware esx") or
                          "esxi" in product_name_lower):
                        # ESXi host license - create separate entities for each host, even with shared licenses
                        for host_info in host_names:
                            entity_name = f"{host_info['name']}_license"
                            associated_host = host_info['original_name']

                            license_data[entity_name] = get_license_info(
                                lic, associated_host
                            )

                        # Mark this license key as processed
                        if license_key:
                            processed_license_keys.add(license_key)
                        esxi_license_count += 1
                    else:
                        # Other/unknown license types
                        _LOGGER.warning("Unknown license product type: '%s' - please report this for better detection", product_name)
                        other_license_count += 1

                        # For unknown licenses, create entities for each host if we have hosts
                        if len(esxi_hosts) > 0:
                            _LOGGER.info("Treating unknown license as ESXi license for hosts: %s", ", ".join([host['original_name'] for host in host_names]))
                            for host_info in host_names:
                                entity_name = f"{host_info['name']}_unknown_license_{other_license_count}"
                                associated_host = host_info['original_name']

                                license_data[entity_name] = get_license_info(
                                    lic, associated_host
                                )
                        else:
                            # No hosts - create generic entity
                            clean_product = product_name_lower.replace(" ", "_").replace("-", "_")
                            if clean_product == "unknown":
                                entity_name = f"unknown_license_{other_license_count}"
                            else:
                                entity_name = f"{clean_product}_license"
                            associated_host = self.host

                            license_data[entity_name] = get_license_info(
                                lic, associated_host
                            )

                        # Mark this license key as processed
                        if license_key:
                            processed_license_keys.add(license_key)            # get vm stats
            if self.config.get("vm") is True:
                # create/destroy view objects
                vm_objview = content.viewManager.CreateContainerView(
                    content.rootFolder, [vim.VirtualMachine], True
                )
                vm_list = vm_objview.view
                vm_objview.Destroy()

                # Look through object list and get data
                _LOGGER.debug("Found %s VM(s)", len(vm_list))

                # 实时性能指标（cpu ready / 内存气球 / 网卡丢包）：一次批量
                #   QueryPerf 拿回全部运行中 VM，避免对 129 台逐个查询打爆 hostd。
                #   失败只影响这三个键（留 None），不影响常规数据。
                perf_by_moid = get_vms_perf(content, vm_list)

                for virtual_machine in vm_list:
                    vm_name = virtual_machine.summary.config.name.replace(
                        " ", "_"
                    ).lower()

                    _LOGGER.debug("Getting stats for vm: %s", vm_name)
                    one_vm = get_vm_info(virtual_machine)
                    # 三个 perf 键每轮都注入（哪怕值是 None）：实体清单据此构建，
                    #   这样某轮 perf 查询失败时 sync 不会把这些传感器误删
                    perf_values = perf_by_moid.get(getattr(virtual_machine, "_moId", ""))
                    for perf_key in VM_PERF_KEYS:
                        one_vm[perf_key] = (
                            perf_values.get(perf_key) if perf_values else None
                        )
                    vm_data[vm_name] = one_vm

            # 原子发布：四个子字典一次性换上（并发 poll 只会看到旧的完整快照或新的
            #   完整快照，不存在"空字典"的中间态）
            entry_data["vmhost"] = vmhost_data
            entry_data["datastore"] = datastore_data
            entry_data["license"] = license_data
            entry_data["vm"] = vm_data

            # 走到这里说明连接与数据拉取全都成功
            self.last_update_ok = True
            if self._consecutive_failures:
                _LOGGER.info(
                    "向 ESXi 拉取数据已恢复正常（此前连续失败 %s 次）",
                    self._consecutive_failures,
                )
                self._consecutive_failures = 0
        finally:
            if conn is not None:
                esx_disconnect(conn)
            # 诊断信息（「数据刷新间隔」实体的属性里会显示出来）
            self.last_refresh_at = time.monotonic()
            self.last_refresh_seconds = round(self.last_refresh_at - started, 2)


def check_files(hass):
    """Return bool that indicates if all files are present."""
    base = f"{hass.config.path()}/custom_components/{DOMAIN}/"
    missing = []
    for file in REQUIRED_FILES:
        fullpath = f"{base}{file}"
        if not os.path.exists(fullpath):
            missing.append(file)

    if missing:
        _LOGGER.critical("The following files are missing: %s", str(missing))
        returnvalue = False
    else:
        returnvalue = True

    return returnvalue


@callback
def async_add_services(hass, config_entry):
    """Add ESXi Control services."""

    # Set notify here - but there has to be a better way
    if (
        "notify" in config_entry.options.keys()
        and config_entry.options["notify"] is not None  # noqa: W503
    ):
        notify = config_entry.options["notify"]
    else:
        notify = True
        _LOGGER.debug("Notify key is missing. Setting notification to true")

    # Check that a host exists in HomeAssistant and get its credentials
    @callback
    def async_get_conn_details(host):
        for _entry in hass.config_entries.async_entries(DOMAIN):
            if host == _entry.data.get("host"):
                return {
                    "host": _entry.data.get("host"),
                    "user": _entry.data.get("username"),
                    "pwd": _entry.data.get("password"),
                    "port": _entry.data.get("port"),
                    "ssl": _entry.data.get("verify_ssl"),
                }

        raise ValueError("Host is not configured in HomeAssistant")

    # Host shutdown service
    async def host_power(call):
        host = call.data["host"]
        target_host = call.data.get("target_host")  # Optional for vCenter multi-host
        cmnd = call.data["command"]
        forc = call.data["force"]

        if cmnd in AVAILABLE_CMND_HOST_POWER:
            try:
                conn_details = async_get_conn_details(host)
                await hass.async_add_executor_job(
                    host_pwr, hass, target_host, cmnd, conn_details, forc, notify
                )
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.error(str(error))
        else:
            _LOGGER.error("host_power: '%s' is not a supported command", cmnd)

    # List hosts service (useful for vCenter environments)
    async def list_hosts(call):
        host = call.data["host"]

        try:
            conn_details = async_get_conn_details(host)
            await hass.async_add_executor_job(
                list_esxi_hosts, hass, conn_details
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(str(error))

    async def list_power_policies(call):
        host = call.data["host"]
        target_host = call.data.get("target_host")

        try:
            conn_details = async_get_conn_details(host)
            await hass.async_add_executor_job(
                list_power_policies, hass, target_host, conn_details
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(str(error))

    @callback
    def async_get_vm_details(vm_name):
        for _entry in hass.data[DOMAIN_DATA]:
            if vm_name in hass.data[DOMAIN_DATA][_entry]["vm"]:
                return hass.data[DOMAIN_DATA][_entry]["vm"][vm_name]["uuid"]

        raise ValueError("VM UUID not found")

    # Host Power Policy service
    async def host_power_policy(call):
        host = call.data["host"]
        cmnd = call.data["command"]
        target_host = call.data.get("target_host")

        try:
            conn_details = async_get_conn_details(host)
            await hass.async_add_executor_job(host_pwr_policy, target_host, cmnd, conn_details)
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(str(error))

    # VM power service
    async def vm_power(call):
        host = call.data["host"]
        vm_name = call.data["vm"]
        vm_uuid = async_get_vm_details(vm_name)
        cmnd = call.data["command"]

        if cmnd in AVAILABLE_CMND_VM_POWER:
            try:
                conn_details = async_get_conn_details(host)
                await hass.async_add_executor_job(
                    vm_pwr, hass, host, vm_name, vm_uuid, cmnd, conn_details, notify
                )
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.error(str(error))
        else:
            _LOGGER.error("vm_power: '%s' is not a supported command", cmnd)

    # Snapshot create service
    async def snap_create(call):
        host = call.data["host"]
        vm_name = call.data["vm"]
        vm_uuid = async_get_vm_details(vm_name)
        memory = False
        quiesce = False
        now = datetime.now()
        name = f"snapshot_{now.strftime('%Y%m%d_%H%M%S')}"  # Default name
        desc = "Taken from HASS (" + HAVERSION + ") on " + now.strftime("%x %X")

        if "name" in call.data:
            name = call.data["name"]
        if "description" in call.data:
            desc = call.data["description"]
        if "memory" in call.data:
            memory = call.data["memory"]
        if "quiesce" in call.data:
            quiesce = call.data["quiesce"]

        try:
            conn_details = async_get_conn_details(host)
            hass.async_add_executor_job(
                vm_snap_take,
                hass,
                host,
                vm_name,
                vm_uuid,
                name,
                desc,
                memory,
                quiesce,
                conn_details,
                notify,
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.error(str(error))

    # Snapshot remove service
    async def snap_remove(call):
        host = call.data["host"]
        vm_name = call.data["vm"]
        vm_uuid = async_get_vm_details(vm_name)
        cmnd = call.data["command"]

        if cmnd in AVAILABLE_CMND_VM_SNAP:
            try:
                conn_details = async_get_conn_details(host)
                hass.async_add_executor_job(
                    vm_snap_remove,
                    hass,
                    host,
                    vm_name,
                    vm_uuid,
                    cmnd,
                    conn_details,
                    notify,
                )
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.error(str(error))
        else:
            _LOGGER.error("snap_remove: '%s' is not a supported command", cmnd)

    # ------------------------------------------------------------------ #
    # 硬件 / 自启动 / 光盘服务（与按钮、开关同一套后端函数）
    # ------------------------------------------------------------------ #
    def _resolve_service_vm(host, vm_name):
        """按「配置的主机地址 + VM 名」定位条目与快照。

        多 ESXi 条目允许同名 VM：必须先用 host 锁定条目，再在该条目里取 VM，
        uuid/moid/host_name 都从快照里拿，服务调用方不需要填内部标识。
        返回 (entry_id, vm_data, conn_details)。
        """
        conn_details = async_get_conn_details(host)
        for _entry in hass.config_entries.async_entries(DOMAIN):
            if _entry.data.get("host") != host:
                continue
            entry_data = hass.data.get(DOMAIN_DATA, {}).get(_entry.entry_id) or {}
            vm_data = (entry_data.get("vm") or {}).get(vm_name)
            if vm_data is not None:
                return _entry.entry_id, vm_data, conn_details
        raise HomeAssistantError(f"主机 {host} 的条目里找不到虚拟机 {vm_name}")

    def _resolve_service_host(host, target_host=None):
        """定位主机条目，返回 (entry_id, ESXi 内部主机名, conn_details)。

        独立 ESXi 上内部主机名取该条目第一台主机快照的 original_name；
        target_host 显式给定时以它为准（vCenter 多主机场景）。
        """
        conn_details = async_get_conn_details(host)
        for _entry in hass.config_entries.async_entries(DOMAIN):
            if _entry.data.get("host") != host:
                continue
            entry_data = hass.data.get(DOMAIN_DATA, {}).get(_entry.entry_id) or {}
            hosts = entry_data.get("vmhost") or {}
            if not hosts:
                continue
            if target_host:
                internal = target_host
            else:
                first = next(iter(hosts.values()))
                internal = first.get("original_name") or next(iter(hosts))
            return _entry.entry_id, internal, conn_details
        raise HomeAssistantError(f"找不到主机 {host} 的已加载条目")

    async def vm_nic_service(call):
        entry_id, vm_data, conn_details = _resolve_service_vm(
            call.data[HOST], call.data[VM]
        )
        try:
            await hass.async_add_executor_job(
                vm_nic_set,
                conn_details,
                vm_data["uuid"],
                call.data["mac"],
                call.data["connected"],
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"网卡切换失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    async def vm_consolidate_service(call):
        entry_id, vm_data, conn_details = _resolve_service_vm(
            call.data[HOST], call.data[VM]
        )
        try:
            await hass.async_add_executor_job(
                vm_consolidate, conn_details, vm_data["uuid"]
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"合并磁盘失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    async def vm_autostart_service(call):
        entry_id, vm_data, conn_details = _resolve_service_vm(
            call.data[HOST], call.data[VM]
        )
        entry_data = hass.data[DOMAIN_DATA][entry_id]
        runtime_host = vm_data.get("host_name")
        internal_host = runtime_host
        for info in (entry_data.get("vmhost") or {}).values():
            if info.get("original_name") == runtime_host:
                internal_host = info.get("original_name") or runtime_host
                break
        else:
            first_host = next(iter((entry_data.get("vmhost") or {}).values()), None)
            if first_host is not None and not internal_host:
                internal_host = first_host.get("original_name")
        if not vm_data.get("moid") or not internal_host:
            raise HomeAssistantError("虚拟机 moid 或主机名缺失，请先刷新数据")
        try:
            await hass.async_add_executor_job(
                vm_autostart_set,
                conn_details,
                internal_host,
                str(vm_data["moid"]),
                call.data["enabled"],
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"设置随主机自启失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    async def host_autostart_service(call):
        entry_id, internal_host, conn_details = _resolve_service_host(
            call.data[HOST], call.data.get(TARGET_HOST)
        )
        try:
            await hass.async_add_executor_job(
                host_autostart_set_enabled,
                conn_details,
                internal_host,
                call.data["enabled"],
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"主机自启动总开关切换失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    def _parse_ds_path(iso_text):
        """把 '[datastore1] dir/file.iso' 解析成 ('datastore1', 'dir/file.iso')。"""
        text = str(iso_text or "").strip()
        if not text.startswith("[") or "]" not in text:
            return None
        ds_name, _, rest = text[1:].partition("]")
        ds_name = ds_name.strip()
        rel_path = rest.strip().lstrip("/")
        if not ds_name or not rel_path:
            return None
        return ds_name, rel_path

    async def vm_mount_iso_service(call):
        local_path = call.data["local_path"]
        if not os.path.isfile(local_path):
            raise HomeAssistantError(
                f"本地 ISO 不存在（注意路径在 Home Assistant 主机/容器内）：{local_path}"
            )
        entry_id, vm_data, conn_details = _resolve_service_vm(
            call.data[HOST], call.data[VM]
        )
        try:
            await hass.async_add_executor_job(
                upload_and_mount,
                conn_details,
                vm_data["uuid"],
                local_path,
                call.data["datastore_dir"],
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"上传并挂载 ISO 失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    async def vm_eject_iso_service(call):
        entry_id, vm_data, conn_details = _resolve_service_vm(
            call.data[HOST], call.data[VM]
        )
        delete_path = None
        if call.data["delete_file"]:
            iso_cdrom = next(
                (
                    cd
                    for cd in vm_data.get("cdroms") or []
                    if cd.get("backing") == "iso"
                ),
                None,
            )
            delete_path = _parse_ds_path((iso_cdrom or {}).get("iso"))
            if delete_path is None:
                raise HomeAssistantError("当前没有挂载数据存储上的 ISO 文件可删除")
        try:
            await hass.async_add_executor_job(
                eject_iso, conn_details, vm_data["uuid"], delete_path
            )
        except Exception as error:  # pylint: disable=broad-except
            raise HomeAssistantError(f"弹出光盘失败：{error}") from error
        await refresh_data_now(hass, entry_id)

    # ------------------------------------------------------------------ #
    # 来宾操作（Guest Operations）
    #   目标定位 + 取配置都走 GuestCredentialManager；具体走哪条通道（VMware
    #   Tools / SSH）由 guest_router 按每台 VM 的配置分派，服务这一层不用关心。
    # ------------------------------------------------------------------ #
    def _guest_target(call):
        """按 host+vm 解析出操作目标，失败抛中文异常。"""
        manager = get_manager(hass)
        if manager is None:
            raise HomeAssistantError("来宾配置管理器尚未初始化")
        try:
            entry_id, vm_key, conn_details, vm_data = manager.resolve_by_host_vm(
                call.data[HOST], call.data[VM]
            )
        except KeyError as error:
            raise HomeAssistantError(str(error)) from error
        try:
            return guest_router.build_target(
                hass, manager, entry_id, vm_key, conn_details, vm_data
            )
        except GuestOpsError as error:
            raise HomeAssistantError(str(error)) from error

    def _make_guest_handler(op_name, arg_names):
        """生成标准来宾服务处理器：解析目标 → 按通道执行 → 响应/抛错。"""

        async def handler(call):
            target = _guest_target(call)
            params = {name: call.data[name] for name in arg_names if name in call.data}
            try:
                return await guest_router.async_run(
                    hass, get_manager(hass), target, op_name, params
                )
            except GuestOpsError as error:
                raise HomeAssistantError(str(error)) from error

        return handler

    async def guest_validate_handler(call):
        """强制校验一次凭据（结果同步给二进传感器）。"""
        manager = get_manager(hass)
        try:
            entry_id, vm_key, _conn, _vm_data = manager.resolve_by_host_vm(
                call.data[HOST], call.data[VM]
            )
        except KeyError as error:
            raise HomeAssistantError(str(error)) from error
        if not manager.get_cred(entry_id, vm_key):
            raise HomeAssistantError(f"虚拟机 {call.data[VM]} 尚未配置来宾凭据")
        return await manager.async_validate(entry_id, vm_key, force=True)

    # (服务名, op 名, schema, 透传给后端的参数名)
    #   op 名与页面 API 完全一致（服务名去掉 guest_ 前缀），由 guest_router 按
    #   这台 VM 的通道（VMware Tools / SSH）分派；validate 走单独的处理函数。
    #   ⚠️ 页面「终端」用的 exec op 只在签名 API 里开放，刻意不注册成服务：
    #      服务是任何有服务权限的人都能调的，等于在客户机里开一个任意命令入口。
    guest_services = [
        ("guest_validate", None, GUEST_VALIDATE_SCHEMA, ()),
        ("guest_list_processes", "list_processes", GUEST_LIST_PROCESSES_SCHEMA, ("pids",)),
        ("guest_start_program", "start_program", GUEST_START_PROGRAM_SCHEMA,
         ("program_path", "arguments", "working_directory", "env_variables")),
        ("guest_terminate_process", "terminate_process", GUEST_TERMINATE_PROCESS_SCHEMA, ("pid",)),
        ("guest_list_files", "list_files", GUEST_LIST_FILES_SCHEMA, ("path", "max_results")),
        ("guest_make_directory", "make_directory", GUEST_MAKE_DIR_SCHEMA, ("path", "create_parents")),
        ("guest_remove_path", "remove_path", GUEST_REMOVE_SCHEMA, ("path", "recursive")),
        ("guest_upload_file", "upload_file", GUEST_UPLOAD_SCHEMA,
         ("local_path", "guest_path", "overwrite")),
        ("guest_download_file", "download_file", GUEST_DOWNLOAD_SCHEMA,
         ("guest_path", "local_path")),
        ("guest_list_aliases", "list_aliases", GUEST_ALIASES_SCHEMA, ("guest_username",)),
        ("guest_list_registry", "list_registry", GUEST_REGISTRY_SCHEMA,
         ("key_path", "recursive")),
    ]

    def _register_guest_service(name, handler, schema):
        """带响应注册；极老版本不认识 supports_response 时退化为普通注册。"""
        try:
            hass.services.async_register(
                DOMAIN, name, handler, schema=schema,
                supports_response=_SERVICE_RESPONSE,
            )
        except TypeError:
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

    hass.services.async_register(DOMAIN, "vm_power", vm_power, schema=VM_PWR_SCHEMA)
    hass.services.async_register(
        DOMAIN, "host_power", host_power, schema=HOST_PWR_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "list_hosts", list_hosts, schema=LIST_HOSTS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "list_power_policies", list_power_policies, schema=LIST_POWER_POLICIES_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "create_snapshot", snap_create, schema=SNAP_CREATE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "remove_snapshot", snap_remove, schema=SNAP_REMOVE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        "host_power_policy",
        host_power_policy,
        schema=HOST_PWR_POLICY_SCHEMA,
    )

    # 硬件 / 自启动 / 光盘服务（幂等：重载集成时不重复注册）
    extra_services = [
        ("vm_set_nic", vm_nic_service, VM_NIC_SET_SCHEMA),
        ("vm_consolidate", vm_consolidate_service, VM_CONSOLIDATE_SCHEMA),
        ("vm_set_autostart", vm_autostart_service, VM_AUTOSTART_SCHEMA),
        ("host_set_autostart", host_autostart_service, HOST_AUTOSTART_SCHEMA),
        ("vm_mount_iso", vm_mount_iso_service, VM_MOUNT_ISO_SCHEMA),
        ("vm_eject_iso", vm_eject_iso_service, VM_EJECT_ISO_SCHEMA),
    ]
    for service_name, service_func, service_schema in extra_services:
        if hass.services.has_service(DOMAIN, service_name):
            continue
        hass.services.async_register(
            DOMAIN, service_name, service_func, schema=service_schema
        )

    # 来宾操作服务（幂等：重载集成时不重复注册）
    for service_name, op_name, service_schema, arg_names in guest_services:
        if hass.services.has_service(DOMAIN, service_name):
            continue
        if op_name is None:
            service_handler = guest_validate_handler
        else:
            service_handler = _make_guest_handler(op_name, arg_names)
        _register_guest_service(service_name, service_handler, service_schema)


async def async_unload_entry(hass, config_entry):
    """Handle removal of an entry."""
    if hass.data.get(DOMAIN_DATA, {}).get("configuration") == "yaml":
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data={}
            )
        )
    else:
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, platform)
                for platform in PLATFORMS
            ]
        )
        # 释放画面变更中枢，避免残留订阅回调持有已卸载实体
        entry_data = hass.data.get(DOMAIN_DATA, {}).get(config_entry.entry_id)
        if entry_data is not None:
            unsub = entry_data.pop("unsub_refresh", None)
            if unsub is not None:
                unsub()
            # 停掉 ESXi 事件轮询定时器
            event_hub = entry_data.pop(EVENT_HUB_KEY, None)
            if event_hub is not None:
                event_hub.stop()
            for key in (HUB_KEY, "aggregate_entity", "screenshot_local_ok"):
                entry_data.pop(key, None)
        # 清掉各平台登记的同步钩子（重载后它们会指向已卸载的实体平台）
        clear_platform_hooks(hass, config_entry.entry_id)
        _LOGGER.info("Successfully removed the ESXi Control integration")

    return True


@callback
def async_update_options(hass, config_entry):
    """Update config entry options"""
    hass.config_entries.async_update_entry(config_entry, options=DEFAULT_OPTIONS)
