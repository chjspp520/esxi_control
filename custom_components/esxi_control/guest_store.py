"""每台虚拟机的「来宾连接配置」存储与校验状态管理。

为什么单独做一层存储
--------------------
Guest Operations（进客户机操作系统执行程序 / 文件 / 注册表）需要的是**客户机自己的
账号密码**，和集成连接 ESXi 用的 root 完全是两套凭据，而且每台 VM 都可能不同。

不做成 ``text`` 实体是因为：text 实体的值会进入状态机和 recorder 数据库/历史，
密码会长期明文落库且到处可见。这里改用 HA 的 ``Store``（``.storage`` 下的私有
JSON，``private=True`` 不会出现在任何实体/日志/备份接口里），保存方式与
config entry 里的 ESXi 密码同级。

存储结构（v2）
--------------
每台 VM 一条记录，两条通道各存一份配置，互不干扰：

    {
      "transport": "auto" | "tools" | "ssh",     # 用户选择；auto 见 resolved_transport
      "tools": {"username", "password", "domain"},
      "ssh":   {"host", "port", "username", "password",
                "verify", "hostkey", "hostkey_kind"},
      "updated_at": <unix 秒>
    }

v1 是扁平结构 ``{username, password, domain}``（只有 Tools 一套）。``_migrate``
在读取时自动把老记录搬进 ``tools``，用户无感。

管理器还负责：
  · 凭据校验结果的短时缓存（避免 129 台 VM 每 15 秒打一次客户机）；
  · 定时（5 分钟）对已配置凭据的运行中 VM 做一轮校验并推送给二进传感器；
  · 凭据增删后触发一次实体同步，让「来宾凭据」实体即时出现/消失。
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .const import DOMAIN, DOMAIN_DATA, LEGACY_DOMAIN

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_guest_creds"
#: 改名（esxi_stats → esxi_control）前的旧存储键：新键为空时读它一次并复制过来，
#: 避免用户已经填好的客户机账号密码在升级后凭空消失。
_LEGACY_STORAGE_KEY = f"{LEGACY_DOMAIN}_guest_creds"
_DATA_KEY = f"{DOMAIN}_guest_mgr"

#: 校验结果缓存时长：5 分钟内复用，不重复打客户机
_VALID_TTL_SECONDS = 300
#: 后台轮询间隔
_SCAN_INTERVAL = timedelta(minutes=5)

#: 通道取值（transport 字段）
TRANSPORT_AUTO = "auto"
TRANSPORT_TOOLS = "tools"
TRANSPORT_SSH = "ssh"
TRANSPORTS = (TRANSPORT_AUTO, TRANSPORT_TOOLS, TRANSPORT_SSH)

#: 判定「来宾是 Linux」用的关键字（guest_os 来自 VMware Tools 上报的 guestFullName）
_LINUX_HINTS = (
    "linux", "ubuntu", "debian", "centos", "red hat", "rhel", "fedora",
    "suse", "opensuse", "rocky", "alma", "arch", "alpine", "oracle",
)


def get_manager(hass):
    """返回全局唯一的凭据管理器（未初始化时返回 None）。"""
    return hass.data.get(_DATA_KEY)


async def async_setup_manager(hass):
    """创建并加载全局凭据管理器（集成 setup 时、平台加载前调用一次）。"""
    manager = hass.data.get(_DATA_KEY)
    if manager is not None:
        return manager
    manager = GuestCredentialManager(hass)
    hass.data[_DATA_KEY] = manager
    await manager.async_load()
    manager.async_start_scan()
    return manager


def _is_linux(vm_data):
    """来宾是不是 Linux（guest_os 里带发行版关键字）。"""
    text = str((vm_data or {}).get("guest_os") or "").lower()
    return any(hint in text for hint in _LINUX_HINTS)


def _migrate(record):
    """把 v1 的扁平记录就地升级成 v2 结构（已经是 v2 则原样返回）。"""
    if not isinstance(record, dict):
        return {"transport": TRANSPORT_AUTO, "tools": {}, "ssh": {}}
    if "tools" in record or "ssh" in record or "transport" in record:
        record.setdefault("tools", {})
        record.setdefault("ssh", {})
        record.setdefault("transport", TRANSPORT_AUTO)
        return record
    tools = {
        "username": str(record.get("username") or ""),
        "password": str(record.get("password") or ""),
        "domain": str(record.get("domain") or ""),
    }
    return {
        "transport": TRANSPORT_AUTO,
        "tools": tools,
        "ssh": {},
        "updated_at": record.get("updated_at") or int(time.time()),
    }


class GuestCredentialManager:
    """连接配置存储（Tools / SSH 两条通道）+ 校验缓存 + 实体推送。"""

    def __init__(self, hass):
        """Initialize."""
        self.hass = hass
        self._store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY, private=True)
        self._data: dict = {}
        self._lock = asyncio.Lock()
        # "{entry_id}:{vm_key}" -> {"ok","error","ts"}
        self._cache: dict = {}
        # "{entry_id}:{vm_key}" -> set(二进传感器实体)
        self._listeners: dict = {}
        self._unsub_scan = None

    # ------------------------------------------------------------------ #
    # 存储
    # ------------------------------------------------------------------ #
    async def async_load(self):
        """从 .storage 读取已保存的配置（首次为 {}），顺带做结构迁移。"""
        try:
            data = await self._store.async_load()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("加载来宾凭据存储失败，按空配置启动")
            data = None
        legacy_data = None
        if not data:
            # 改名迁移：旧 .storage/esxi_stats_guest_creds
            try:
                legacy = Store(
                    self.hass, _STORAGE_VERSION, _LEGACY_STORAGE_KEY, private=True
                )
                legacy_data = await legacy.async_load()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("读取旧来宾凭据存储失败（跳过迁移）")
                legacy_data = None
            if legacy_data:
                data = legacy_data
                _LOGGER.info(
                    "已把旧来宾凭据存储 %s 迁移到 %s", _LEGACY_STORAGE_KEY, _STORAGE_KEY
                )
        self._data = data or {}
        need_save = bool(legacy_data)
        migrated = False
        for entry_map in self._data.values():
            if not isinstance(entry_map, dict):
                continue
            for vm_key, record in list(entry_map.items()):
                if isinstance(record, dict) and not (
                    "tools" in record or "ssh" in record or "transport" in record
                ):
                    entry_map[vm_key] = _migrate(record)
                    migrated = True
        if migrated:
            _LOGGER.info("来宾凭据存储已升级到「通道」结构（老数据按 VMware Tools 通道处理）")
            need_save = True
        if need_save and self._data:
            # 结构升级 / 从旧存储键迁移过来：都要在新键下落一次盘
            try:
                await self._save()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.warning("来宾凭据存储写回失败", exc_info=True)

    async def _save(self):
        await self._store.async_save(self._data)

    def configured_vms(self, entry_id):
        """该条目下已配置（任一条通道）的 VM key 集合（同步、只读内存）。"""
        return set((self._data.get(entry_id) or {}).keys())

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get_cred(self, entry_id, vm_key):
        """取整条记录（v2 结构）的副本；没有则 None。"""
        raw = (self._data.get(entry_id) or {}).get(vm_key)
        return dict(raw) if raw else None

    def get_tools_cred(self, entry_id, vm_key):
        """取 VMware Tools 通道的账号（guest_ops 用），没配则 None。"""
        tools = (self.get_cred(entry_id, vm_key) or {}).get("tools") or {}
        if not tools.get("username"):
            return None
        return {
            "username": str(tools.get("username") or ""),
            "password": str(tools.get("password") or ""),
            "domain": str(tools.get("domain") or ""),
        }

    def get_ssh_conf(self, entry_id, vm_key):
        """取 SSH 通道配置的副本（可能不全，缺什么由连接时校验）。"""
        return dict((self.get_cred(entry_id, vm_key) or {}).get("ssh") or {})

    def has_ssh_cred(self, entry_id, vm_key):
        """SSH 通道是否已填了用户名（没用户名等于没配）。"""
        return bool(str((self.get_ssh_conf(entry_id, vm_key) or {}).get("username") or "").strip())

    def transport_setting(self, entry_id, vm_key):
        """用户选的通道（未配置过时按 auto）。"""
        want = str((self.get_cred(entry_id, vm_key) or {}).get("transport") or "").strip()
        return want if want in TRANSPORTS else TRANSPORT_AUTO

    def resolved_transport(self, entry_id, vm_key, vm_data=None):
        """实际走哪条通道：tools / ssh。

        auto 的判定：**配了 SSH 账号 + 来宾是 Linux** → ssh，否则 tools。
        （Windows 保持现行行为不变；显式选 ssh 但没填账号时回落 tools，
          避免"选了 SSH 却完全用不了"。）
        """
        want = self.transport_setting(entry_id, vm_key)
        has_ssh = self.has_ssh_cred(entry_id, vm_key)
        if want == TRANSPORT_SSH:
            return TRANSPORT_SSH if has_ssh else TRANSPORT_TOOLS
        if want == TRANSPORT_TOOLS:
            return TRANSPORT_TOOLS
        if has_ssh:
            if vm_data is None:
                try:
                    _, vm_data = self.resolve(entry_id, vm_key)
                except KeyError:
                    vm_data = None
            if _is_linux(vm_data) or not self.get_tools_cred(entry_id, vm_key):
                return TRANSPORT_SSH
        return TRANSPORT_TOOLS

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def async_set_cred(self, entry_id, vm_key, username, password, domain=""):
        """新增/更新 VMware Tools 通道的来宾凭据。

        password 传空字符串时表示「保留已存密码」（编辑时密码框不回显）。
        """
        async with self._lock:
            record = self._record(entry_id, vm_key)
            existing = record.get("tools") or {}
            if not password:
                password = existing.get("password", "")
            if not username or not password:
                raise ValueError("用户名和密码不能为空")
            record["tools"] = {
                "username": str(username),
                "password": str(password),
                "domain": str(domain or ""),
            }
            record["updated_at"] = int(time.time())
            await self._save()
        self._cache.pop(f"{entry_id}:{vm_key}", None)
        self._request_resync(entry_id)

    async def async_set_ssh(self, entry_id, vm_key, host=None, port=None,
                            username=None, password=None, verify=None):
        """新增/更新 SSH 通道配置。

        只覆盖**显式传进来**的字段（None = 不动）；password 传空字符串 =
        保留已存密码。这样前端可以只改端口而不用回传密码。
        """
        async with self._lock:
            record = self._record(entry_id, vm_key)
            ssh = dict(record.get("ssh") or {})
            if host is not None:
                ssh["host"] = str(host).strip()
            if port is not None:
                try:
                    ssh["port"] = int(port or 22)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"SSH 端口必须是数字，收到：{port!r}") from error
            if username is not None:
                ssh["username"] = str(username).strip()
            if password:
                ssh["password"] = str(password)
            if verify is not None:
                ssh["verify"] = bool(verify)
            ssh.setdefault("port", 22)
            ssh.setdefault("verify", True)
            record["ssh"] = ssh
            record["updated_at"] = int(time.time())
            await self._save()
        self._cache.pop(f"{entry_id}:{vm_key}", None)
        self._request_resync(entry_id)

    async def async_set_transport(self, entry_id, vm_key, transport):
        """设置这台 VM 使用哪条通道（auto / tools / ssh）。"""
        want = str(transport or "").strip()
        if want not in TRANSPORTS:
            raise ValueError(f"未知通道：{transport!r}（可选 auto / tools / ssh）")
        async with self._lock:
            record = self._record(entry_id, vm_key)
            record["transport"] = want
            record["updated_at"] = int(time.time())
            await self._save()
        self._cache.pop(f"{entry_id}:{vm_key}", None)

    async def async_set_ssh_hostkey(self, entry_id, vm_key, fingerprint, kind=""):
        """记下（TOFU）这台 VM 的 SSH 主机指纹。"""
        if not fingerprint:
            return
        async with self._lock:
            record = self._record(entry_id, vm_key)
            ssh = dict(record.get("ssh") or {})
            ssh["hostkey"] = str(fingerprint)
            if kind:
                ssh["hostkey_kind"] = str(kind)
            record["ssh"] = ssh
            await self._save()

    async def async_clear_ssh_hostkey(self, entry_id, vm_key):
        """清掉记录的主机指纹（换机器 / 重装系统之后用）。"""
        async with self._lock:
            record = self._record(entry_id, vm_key)
            ssh = dict(record.get("ssh") or {})
            ssh.pop("hostkey", None)
            ssh.pop("hostkey_kind", None)
            record["ssh"] = ssh
            await self._save()

    async def async_remove_cred(self, entry_id, vm_key):
        """删除一台 VM 的全部来宾配置（两条通道一起删）。"""
        async with self._lock:
            entry_map = self._data.get(entry_id) or {}
            if vm_key in entry_map:
                entry_map.pop(vm_key)
                await self._save()
        self._cache.pop(f"{entry_id}:{vm_key}", None)
        self._request_resync(entry_id)

    def _record(self, entry_id, vm_key):
        """取（或建）一台 VM 的记录，保证 v2 结构齐全。调用方需持有锁。"""
        entry_map = self._data.setdefault(entry_id, {})
        record = entry_map.get(vm_key)
        if not isinstance(record, dict):
            record = {"transport": TRANSPORT_AUTO, "tools": {}, "ssh": {}}
        record.setdefault("tools", {})
        record.setdefault("ssh", {})
        record.setdefault("transport", TRANSPORT_AUTO)
        entry_map[vm_key] = record
        return record

    # ------------------------------------------------------------------ #
    # 目标解析（供实体与服务共用）
    # ------------------------------------------------------------------ #
    def resolve(self, entry_id, vm_key):
        """返回 (conn_details, vm_data)；目标不存在时抛 KeyError。"""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise KeyError(f"集成条目不存在：{entry_id}")
        entry_data = self.hass.data[DOMAIN_DATA].get(entry_id) or {}
        vm_data = (entry_data.get("vm") or {}).get(vm_key)
        if vm_data is None:
            raise KeyError(f"虚拟机 {vm_key} 不在当前监控数据中（可先执行一次同步）")
        config = entry.data
        conn_details = {
            "host": config["host"],
            "user": config["username"],
            "pwd": config["password"],
            "port": config["port"],
            "ssl": config["verify_ssl"],
        }
        return conn_details, vm_data

    def resolve_by_host_vm(self, host, vm_name):
        """服务调用入口：按 ESXi 主机 + 虚拟机名解析 (entry_id, vm_key, conn, vm)。"""
        host = str(host or "").strip().lower()
        wanted = str(vm_name or "").strip().lower().replace(" ", "_")
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            config = entry.data
            if str(config.get("host", "")).lower() != host:
                continue
            entry_data = self.hass.data[DOMAIN_DATA].get(entry.entry_id) or {}
            for vm_key, vm_data in (entry_data.get("vm") or {}).items():
                if vm_key == wanted or str(vm_data.get("vm_name", "")).lower() == wanted:
                    conn_details = {
                        "host": config["host"],
                        "user": config["username"],
                        "pwd": config["password"],
                        "port": config["port"],
                        "ssl": config["verify_ssl"],
                    }
                    return entry.entry_id, vm_key, conn_details, vm_data
        raise KeyError(f"在主机 {host} 上找不到虚拟机 {vm_name}")

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def cached_result(self, entry_id, vm_key):
        """同步读缓存结果（供二进传感器的 update 用）。"""
        result = self._cache.get(f"{entry_id}:{vm_key}")
        if not result:
            return None
        if time.time() - result.get("ts", 0) > _VALID_TTL_SECONDS * 4:
            return None
        return result

    async def async_validate(self, entry_id, vm_key, force=False):
        """（后台）校验一台 VM 的来宾连接，结果入缓存并推送实体。

        实际走哪条通道由 guest_router 决定（Tools 或 SSH）。
        """
        cache_key = f"{entry_id}:{vm_key}"
        cached = self._cache.get(cache_key)
        if (
            not force
            and cached
            and time.time() - cached.get("ts", 0) < _VALID_TTL_SECONDS
        ):
            return cached

        if not self.get_cred(entry_id, vm_key):
            result = {"ok": False, "error": "未配置来宾凭据", "ts": int(time.time())}
        else:
            try:
                # 局部导入：guest_router 要用本模块的 get_manager，模块级互相 import 会成环
                from .guest_router import async_validate_target

                result = await async_validate_target(self.hass, self, entry_id, vm_key)
                result["ts"] = int(time.time())
            except Exception as error:  # pylint: disable=broad-except
                result = {"ok": False, "error": str(error), "ts": int(time.time())}

        self._cache[cache_key] = result
        for entity in list(self._listeners.get(cache_key, ())):
            entity.async_set_guest_result(result)
        return result

    # ------------------------------------------------------------------ #
    # 实体订阅
    # ------------------------------------------------------------------ #
    def register_listener(self, entry_id, vm_key, entity):
        self._listeners.setdefault(f"{entry_id}:{vm_key}", set()).add(entity)

    def unregister_listener(self, entry_id, vm_key, entity):
        group = self._listeners.get(f"{entry_id}:{vm_key}")
        if group:
            group.discard(entity)

    # ------------------------------------------------------------------ #
    # 后台扫描 / 同步
    # ------------------------------------------------------------------ #
    def async_start_scan(self):
        """定时对已配置凭据的 VM 做一轮校验。"""
        if self._unsub_scan is not None:
            return
        self._unsub_scan = async_track_time_interval(
            self.hass, self._async_scan, _SCAN_INTERVAL
        )

    async def _async_scan(self, _now=None):
        for entry_id, vm_keys in list(self._data.items()):
            for vm_key in list(vm_keys):
                try:
                    _, vm_data = self.resolve(entry_id, vm_key)
                except KeyError:
                    continue
                # 关机的一律跳过（强行校验只会得到一片无意义错误）
                if vm_data.get("state") != "running":
                    continue
                # Tools 通道必须有 Tools 在跑；SSH 通道跟 Tools 无关，别跟着被跳过
                if self.resolved_transport(entry_id, vm_key, vm_data) == TRANSPORT_TOOLS:
                    tools = str(vm_data.get("tools_status", "") or "").lower()
                    if tools not in ("toolsok", "toolsold"):
                        continue
                try:
                    await self.async_validate(entry_id, vm_key)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("来宾凭据后台校验异常 %s/%s", entry_id, vm_key)

    def _request_resync(self, entry_id):
        """凭据增删后让实体清单对齐（后台做，含一次强制数据刷新）。"""
        async def _do():
            try:
                from .entity_sync import async_sync_entry

                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is not None:
                    await async_sync_entry(self.hass, entry)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("来宾凭据变更后的实体同步失败")

        self.hass.async_create_task(_do())
