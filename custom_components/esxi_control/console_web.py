"""虚拟机控制台的 Web 托管层。

四类 HTTP 端点（全部挂在 HA 自己的 web server 下，同源、跟随 HA 的 HTTPS 端口）：

1. ``GET  /esxi_control/console?entry=&vm=``        控制台页面（WMKS / VMRC 自适应）
2. ``GET  /esxi_control/console/static/<file>``     页面静态资源（jQuery / WMKS）
3. ``GET  /esxi_control/api/console_ticket/...``    服务端代取控制台票据（凭据不出服务端）
4. ``GET  /esxi_control/ws/<entry>/<vm>``           同源 WebSocket 代理 → wss://ESXi/ticket/..

安全模型
--------
HA 的自定义 view 默认要登录态，而浏览器**顶层导航**到这个页面时不会带 HA 的
Authorization 头（HA 的令牌存在 localStorage，不是 Cookie）。因此采用 HA 官方
给摄像头 / TTS 代理用的同一套机制：``async_sign_path`` 给路径签发带时效的
``authSig``。设备页「访问」链接、页面里的票据接口和 WS 地址都是**按虚拟机粒度**
签好名的 URL：拿到链接只能访问那一台虚拟机的控制台，且 24 小时后自动失效；
ESXi 的账号密码永远不会出现在页面 / 前端代码里。

注意：新版 HA（2025 年起）的鉴权中间件只在 GET/HEAD 上接受 authSig，所以光盘
接口（POST/DELETE）把 ``requires_auth`` 关掉，由视图调用
``_verify_signed_request`` 自行验签，规则与官方完全一致但不限制请求方法。
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid as uuid_lib
from datetime import timedelta
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiohttp.http_websocket import WSMsgType
from yarl import URL as YarlURL

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CONSOLE_BASE_URL,
    CONSOLE_PAGE_PREFIX,
    CONSOLE_STATIC_PREFIX,
    CONSOLE_TICKET_PREFIX,
    CONSOLE_ORIGIN_PREFIX,
    CONSOLE_WS_PREFIX,
    CONSOLE_CDROM_PREFIX,
    CONSOLE_GUEST_PAGE_PREFIX,
    CONSOLE_GUEST_API_PREFIX,
    CONSOLE_GUEST_UPLOAD_PREFIX,
    CONSOLE_GUEST_DOWNLOAD_PREFIX,
    ADMIN_PAGE_PREFIX,
    ADMIN_API_PREFIX,
    ISO_UPLOAD_DIR,
    DOMAIN,
    DOMAIN_DATA,
    EVENT_ENTRY_SYNCED,
    LEGACY_DOMAIN,
)
from .esxi import acquire_console_ticket, ignore_ssl_verify
from .cdrom_ops import upload_and_mount, eject_iso
from . import vm_admin_ops
from .guest_ops import GuestOpsError
from . import guest_router
from .guest_store import get_manager
from .iso9660 import build_iso
from .vm_bulk import (
    device_by_identifier,
    host_device_key,
    vm_device_key,
    refresh_data_now,
)

# 新版 HA 才有签名路径；没有时退化成相对路径（在已登录的同浏览器标签里多数仍可用，
#   但不保证，所以这是降级路径而不是主路径）
try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.components.http.auth import async_sign_path
except ImportError:  # pragma: no cover
    async_sign_path = None

# authSig 的验签零件：HA 2025 年起中间件只接受 GET/HEAD 的签名请求，
#   而光盘接口必须 POST/DELETE，只能在视图里按同样规则自行验签
try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.auth import jwt_wrapper
    from homeassistant.components.http.auth import (
        DATA_SIGN_SECRET,
        SAFE_QUERY_PARAMS,
        SIGN_QUERY_PARAM,
    )
except ImportError:  # pragma: no cover
    jwt_wrapper = None
    DATA_SIGN_SECRET = "http.auth.sign_secret"
    SAFE_QUERY_PARAMS = frozenset(("height", "width"))
    SIGN_QUERY_PARAM = "authSig"

_LOGGER = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_WWW_DIR = os.path.join(_HERE, "www_console")
_INDEX = os.path.join(_WWW_DIR, "index.html")
_ADMIN_DIR = os.path.join(_HERE, "www_admin")
_ADMIN_INDEX = os.path.join(_ADMIN_DIR, "index.html")
_ADMIN_CACHE = {"mtime": None, "text": ""}

#: 签名有效期：设备链接 / 票据接口 / WS 代理统一 24 小时；
#: 到期前由「同步完成」事件和每 12 小时的定时任务自动重签
_SIGN_EXPIRY = timedelta(hours=24)
_RESIGN_INTERVAL = timedelta(hours=12)

#: 静态资源白名单：只暴露这几个文件，绝不按请求路径拼磁盘路径（防穿越）
#:   元组第二项 True = Cache-Control: no-store（需要随版本即时更新的前端模块）
_STATIC_FILES = {
    "vendor/jquery.js": ("application/javascript; charset=utf-8", False),
    "vendor/jquery.ui.js": ("application/javascript; charset=utf-8", False),
    "vendor/wmks.js": ("application/javascript; charset=utf-8", False),
    # 注入 HA 前端的「控制台悬浮窗」模块（add_extra_js_url 加载）
    "frontend/console-embed.js": ("application/javascript; charset=utf-8", True),
}

_DATA_KEY = f"{DOMAIN}_console_web"
_INDEX_CACHE = {"key": None, "text": ""}

#: 页面依赖的三个 vendor 脚本（白名单，同时也是内联进 HTML 的清单）
_VENDOR_FILES = ("vendor/jquery.js", "vendor/jquery.ui.js", "vendor/wmks.js")

#: 学到的访问基地址持久化（重启不丢，避免重启后又拼错 host/端口）
_BASE_STORE_VERSION = 1
_BASE_STORE_KEY = f"{DOMAIN}.console_base_urls"

#: 控制台上传 ISO 的挂载记录持久化：{entries: {entry_id: {vm_key: {ds,path,iso,...}}}}
#: 只记录**本集成上传**的 ISO，弹出时才连带删文件；用户手挂的镜像绝不动
_CDROM_STORE_VERSION = 1
_CDROM_STORE_KEY = f"{DOMAIN}.console_cdrom_mounts"

#: 全局「远程桌面」总开关（控制台 + 文件系统面板 + 虚拟机管理页面）的持久化。
#: ⚠️ 这是**集成级**状态，不是每台 ESXi 一份：所以用独立的 Store 而不是
#:   config_entry.options（options 是每条目一份，管不了「所有主机」）。
#: 它只决定"要不要把钥匙发出去"——关掉期间不向 ESXi 写任何东西，所以 HA 关机 /
#:   断电都不会改变它，反而可能比"每台 VM 一个开关"更安全；HA 重启后按落盘值
#:   恢复（默认开启，与升级前行为一致）。
_REMOTE_STORE_VERSION = 1
_REMOTE_STORE_KEY = f"{DOMAIN}.remote_desktop"

#: 全局「远程桌面」开关的 unique_id（switch 平台用它保证整集成只有这一个实体；
#:   实体挂在该条目第一台 ESXi 主机设备下，不单独建设备）
REMOTE_DESKTOP_UNIQUE_ID = f"{DOMAIN}_remote_desktop"
#: 被停用时给用户看的一句话（页面用文本、数据接口用 JSON）
_REMOTE_DISABLED_TEXT = "远程桌面已被管理员停用（打开 HA 里的「远程桌面」开关即可恢复）。"

#: 改名（esxi_stats → esxi_control）之前的旧 .storage 键。
#: 域名变化会让 Store 的文件名跟着变，这里在**读新键为空**时回退读旧键并复制过来，
#: 保证升级后「远程桌面」开关状态、学到的访问地址、ISO 挂载记录不丢。
_LEGACY_BASE_STORE_KEY = f"{LEGACY_DOMAIN}.console_base_urls"
_LEGACY_CDROM_STORE_KEY = f"{LEGACY_DOMAIN}.console_cdrom_mounts"
_LEGACY_REMOTE_STORE_KEY = f"{LEGACY_DOMAIN}.remote_desktop"


async def _async_load_with_legacy(store, version, new_key, legacy_keys):
    """读 Store；为空时依次尝试旧存储键，命中就复制到新键（一次性迁移）。"""
    try:
        data = await store.async_load() or {}
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("加载存储 %s 失败", new_key)
        data = {}
    if data:
        return data
    for legacy_key in legacy_keys:
        try:
            legacy = Store(store.hass, version, legacy_key)
            legacy_data = await legacy.async_load() or {}
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("加载旧存储 %s 失败（跳过迁移）", legacy_key)
            continue
        if not legacy_data:
            continue
        try:
            await store.async_save(legacy_data)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("迁移旧存储 %s → %s 失败（本次仍按旧数据运行）",
                              legacy_key, new_key)
        _LOGGER.info("已把旧存储 %s 的数据迁移到 %s", legacy_key, new_key)
        return legacy_data
    return {}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _sign(hass, path):
    """给路径签 authSig；老版本 HA 没这能力时原样返回。"""
    if async_sign_path is None:
        return path
    try:
        return async_sign_path(hass, path, _SIGN_EXPIRY)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("控制台路径签名失败: %s", path)
        return path


async def _verify_signed_request(hass, request):
    """按 HA 官方同样的规则校验 authSig，但**不限制请求方法**。

    HA 2025 年起 ``auth_middleware`` 里只有 GET/HEAD 接受签名请求，POST/DELETE
    即使带正确 authSig 也直接 401。控制台页面本身是靠签名打开的，前端手上没有
    bearer token，所以光盘接口（分片上传 POST / 弹出 DELETE）把 ``requires_auth``
    关掉，在这里自行验签。判定规则与
    ``homeassistant.components.http.auth.async_validate_signed_request`` 一致：
    JWT 用 http 签名密钥验签且未过期 → 路径一致 → query 参数一致 → 签发用的
    refresh token 仍有效。签名本身是一次性 HMAC 票据，跨站页面无法伪造，
    且上传带自定义头还会触发 CORS 预检，没有 CSRF 面。
    """
    if jwt_wrapper is None:
        return False

    secret = hass.data.get(DATA_SIGN_SECRET)
    signature = request.query.get(SIGN_QUERY_PARAM)
    if not secret or not signature:
        return False

    try:
        claims = jwt_wrapper.verify_and_decode(
            signature, secret, algorithms=["HS256"], options={"verify_iss": False}
        )
    except Exception:  # pylint: disable=broad-except  JWT 过期/篡改都归为未授权
        return False

    if claims.get("path") != request.path:
        return False

    params = [
        list(itm)  # claims 里元组被 JSON 存成列表
        for itm in request.query.items()
        if itm[0] not in SAFE_QUERY_PARAMS and itm[0] != SIGN_QUERY_PARAM
    ]
    if claims.get("params") != params:
        return False

    # iss 是签发时的 refresh token id；token 被吊销/登出后签名立即失效
    if hass.auth.async_get_refresh_token(claims.get("iss")) is None:
        return False

    return True


def _unauthorized():
    """统一的 401 响应（JSON 接口用）。"""
    return web.json_response(
        {"error": "未授权：控制台凭据缺失、已失效或已被吊销，请从设备页重新打开"},
        status=401,
    )


def _read_utf8(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _inline_js(code):
    """把 JS 源码安全地嵌进 <script> 标签里。

    vendor 源码（尤其 jQuery）里有 ``</script>`` 形式的字符串/正则字面量，
    HTML 解析器不管它是不是字符串，见到就会提前结束脚本块，必须转义。
    （三个 vendor 文件均不含 ``<!--``/``-->``，无需处理 script data escaped 状态。）
    """
    return code.replace("</script", "<\\/script")


def _page_template():
    """构建内联了全部 vendor JS 的控制台页面模板（按各文件 mtime 缓存）。

    为什么不再用 <script src> 静态加载：控制台页面是靠 authSig 临时签名打开的，
    多一个静态请求就多一个失败点（部署漏文件 → 404 → 页面里 ``$ is not defined``
    永远停在"正在准备控制台"）。内联后整个页面只有一个必须成功的请求。
    """
    all_files = (("index", _INDEX),) + tuple(
        (rel, os.path.join(_WWW_DIR, *rel.split("/"))) for rel in _VENDOR_FILES
    )
    mtimes = {key: os.path.getmtime(path) for key, path in all_files}
    cache_key = tuple(mtimes[key] for key, _ in all_files)
    if _INDEX_CACHE["key"] != cache_key:
        html = _read_utf8(_INDEX)
        for rel in _VENDOR_FILES:
            vendor_path = os.path.join(_WWW_DIR, *rel.split("/"))
            src_tag = f'<script src="__STATIC_PREFIX__/{rel}"></script>'
            if src_tag not in html:
                raise RuntimeError(f"控制台页面模板中找不到 {src_tag}，模板与代码版本不匹配")
            html = html.replace(
                src_tag, f"<script>\n{_inline_js(_read_utf8(vendor_path))}\n</script>"
            )
        _INDEX_CACHE["key"] = cache_key
        _INDEX_CACHE["text"] = html
    return _INDEX_CACHE["text"]


def _admin_page_template():
    """读取虚拟机管理页（单文件、无 vendor），按 mtime 缓存。"""
    mtime = os.path.getmtime(_ADMIN_INDEX)
    if _ADMIN_CACHE["mtime"] != mtime:
        _ADMIN_CACHE["mtime"] = mtime
        _ADMIN_CACHE["text"] = _read_utf8(_ADMIN_INDEX)
    return _ADMIN_CACHE["text"]


def _normalize_base(value):
    """把用户输入/探测到的地址规范成 ``scheme://host[:port]``；非法返回空串。"""
    if not value:
        return ""
    try:
        parsed = YarlURL(str(value).strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return ""
    return str(parsed.origin()).rstrip("/")


def _entry_data(hass, entry_id):
    """取集成条目的运行时数据；不存在 / 已卸载返回 None。"""
    return (hass.data.get(DOMAIN_DATA) or {}).get(entry_id)


def _vm_snapshot(entry_data, vm_key):
    """从条目数据里安全取一台虚拟机的快照。"""
    return (entry_data.get("vm") or {}).get(vm_key)


async def _upstream_ssl_context(hass):
    """忽略自签证书的 SSL context（5.5 还要降 SECLEVEL），全局复用一个。"""
    store = hass.data.setdefault(_DATA_KEY, {})
    if store.get("ssl_ctx") is None:
        store["ssl_ctx"] = await hass.async_add_executor_job(ignore_ssl_verify)
    return store["ssl_ctx"]


def _conn_details(hass, entry_id):
    """从配置条目构造 esxi/cdrom 后端共用的连接信息 dict。"""
    config = hass.config_entries.async_get_entry(entry_id).data
    return {
        "host": config["host"],
        "user": config["username"],
        "pwd": config["password"],
        "port": config["port"],
        "ssl": config["verify_ssl"],
    }


async def async_load_cdrom_mounts(hass):
    """（幂等）加载控制台 ISO 挂载记录。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    if "cdrom_mounts" in store_data:
        return
    store = Store(hass, _CDROM_STORE_VERSION, _CDROM_STORE_KEY)
    data = await _async_load_with_legacy(
        store, _CDROM_STORE_VERSION, _CDROM_STORE_KEY, (_LEGACY_CDROM_STORE_KEY,)
    )
    store_data["cdrom_store"] = store
    store_data["cdrom_mounts"] = dict(data.get("entries") or {})


async def _save_cdrom_mounts(hass):
    """落盘挂载记录。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    store = store_data.get("cdrom_store")
    if store is None:
        return
    await store.async_save({"entries": dict(store_data.get("cdrom_mounts") or {})})


def _cdrom_record(hass, entry_id, vm_key):
    """取某台 VM 的 HA 上传 ISO 记录（无则 None）。"""
    mounts = hass.data.get(_DATA_KEY, {}).get("cdrom_mounts") or {}
    return (mounts.get(entry_id) or {}).get(vm_key)


async def _put_cdrom_record(hass, entry_id, vm_key, record):
    """写入/更新一台 VM 的挂载记录并落盘。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    mounts = store_data.setdefault("cdrom_mounts", {})
    mounts.setdefault(entry_id, {})[vm_key] = record
    await _save_cdrom_mounts(hass)


async def _pop_cdrom_record(hass, entry_id, vm_key):
    """删除一台 VM 的挂载记录并落盘；返回被删记录（无则 None）。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    mounts = store_data.setdefault("cdrom_mounts", {})
    record = (mounts.get(entry_id) or {}).pop(vm_key, None)
    if record is not None:
        await _save_cdrom_mounts(hass)
    return record


def _b64_name(value):
    """把归档相对路径编码成可做 multipart 字段名的 base64url。"""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64_name(value):
    """_b64_name 的逆过程；非法输入返回 None。"""
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
        text = raw.decode("utf-8").replace("\\", "/").strip("/")
        return text or None
    except Exception:  # pylint: disable=broad-except
        return None


def _safe_filename(name):
    """把上传文件名清洗成临时目录里的安全单段文件名。"""
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "blob"
    return base[:120]


# --------------------------------------------------------------------------- #
# 设备 configuration_url（设备页的「访问」按钮）
# --------------------------------------------------------------------------- #
def _configured_base(hass, entry_id):
    """设备链接要用的绝对基地址（设备注册表不接受相对路径）。

    优先级：
    1. 集成选项里显式填写的「控制台访问地址」（端口映射 / 反代的最终解法）；
    2. 从浏览器**实际打开控制台页面的 host:端口**学到的来源（端口映射时自动跟随
       地址栏，持久化保存，重启不丢）；
    3. HA 的外部 URL → 内部 URL；
    4. 自动探测的局域网地址。
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None:
        explicit = _normalize_base(entry.options.get(CONF_CONSOLE_BASE_URL))
        if explicit:
            return explicit

    learned = _normalize_base(
        (hass.data.get(_DATA_KEY, {}).get("learned_bases") or {}).get(entry_id)
    )
    if learned:
        return learned

    configured = hass.config.external_url or hass.config.internal_url or ""
    normalized = _normalize_base(configured)
    if normalized:
        return normalized
    try:
        from homeassistant.helpers.network import get_url

        detected = get_url(hass, allow_ip=True, allow_cloud=False)
    except Exception:  # pylint: disable=broad-except
        detected = ""
    return _normalize_base(detected) or detected.rstrip("/")


async def _persist_learned_bases(hass):
    """把学到的基地址落盘（防抖：2 秒内的多次学习只写一次）。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    if store_data.get("base_persist_task") is not None:
        return

    async def _do_save():
        try:
            await asyncio.sleep(2)
            store = store_data.get("base_store")
            if store is not None:
                await store.async_save(
                    {"bases": dict(store_data.get("learned_bases") or {})}
                )
        finally:
            store_data["base_persist_task"] = None

    store_data["base_persist_task"] = hass.async_create_task(_do_save())


def _schedule_relink(hass, entry_id, delay=1.5):
    """基地址变化后防抖重签该条目的设备链接。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    tasks = store_data.setdefault("relink_tasks", {})
    old = tasks.get(entry_id)
    if old is not None and not old.done():
        return

    async def _do_relink():
        await asyncio.sleep(delay)
        try:
            count = await _link_one_entry(hass, entry_id)
            if count:
                _LOGGER.debug("控制台设备链接已按新访问地址刷新：%s 台虚拟机", count)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("访问地址变化后重签条目 %s 的链接失败", entry_id)

    tasks[entry_id] = hass.async_create_task(_do_relink())


async def _remember_origin(hass, entry_id, request):
    """记住"浏览器实际用哪个 host:端口 打开控制台"，供设备链接自动跟随。

    端口映射 / 反向代理环境下，HA 服务端探测到的往往是容器内地址（浏览器根本
    到不了）；而签名本身只覆盖路径不覆盖 host，同一个 authSig 在任意能到达本
    HA 的 host 上都有效 —— 所以直接记住页面请求的实际来源即可。
    用户在选项里显式填了地址时，学习结果不生效（显式优先）。
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return
    if _normalize_base(entry.options.get(CONF_CONSOLE_BASE_URL)):
        return
    origin = _normalize_base(str(request.url.origin()))
    if not origin:
        return
    store_data = hass.data.setdefault(_DATA_KEY, {})
    learned = store_data.setdefault("learned_bases", {})
    if learned.get(entry_id) == origin:
        return
    learned[entry_id] = origin
    await _persist_learned_bases(hass)
    _schedule_relink(hass, entry_id)


async def remember_origin_for_all_entries(hass, request):
    """前端心跳：用浏览器当前地址栏来源更新**所有**未显式配置的条目。

    悬浮窗模块（console-embed.js）随 HA 前端每个页面加载，浏览器一打开 HA
    就上报一次，不必等用户点开控制台 —— 这样换网络 / 换上级路由访问后，
    设备链接与「远程地址」传感器几秒内自动跟随当前地址栏，不用先开一次
    控制台才纠偏。返回是否有变化（变化才会触发落盘 + 防抖重签）。
    """
    origin = _normalize_base(str(request.url.origin()))
    if not origin:
        return False
    store_data = hass.data.setdefault(_DATA_KEY, {})
    learned = store_data.setdefault("learned_bases", {})
    changed_entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        # 用户在选项里显式写死了访问地址：绝不覆盖
        if _normalize_base(entry.options.get(CONF_CONSOLE_BASE_URL)):
            continue
        if learned.get(entry.entry_id) == origin:
            continue
        learned[entry.entry_id] = origin
        changed_entries.append(entry.entry_id)
    if not changed_entries:
        return False
    await _persist_learned_bases(hass)
    for entry_id in changed_entries:
        _schedule_relink(hass, entry_id)
    _LOGGER.info("控制台访问地址跟随浏览器地址栏更新为 %s（%s 个条目）",
                 origin, len(changed_entries))
    return True


# --------------------------------------------------------------------------- #
# 全局「远程桌面」总开关（控制台 + 文件系统面板 + 虚拟机管理页面）
#
# 它管的是"要不要把钥匙发出去"：关掉 = 设备页不再挂「访问」链接、所有控制台 /
#   文件系统 / 管理页请求一律 403、正在跑的控制台会话被立刻掐断。完全不碰截图
#   链路（image.* 走 ESXi 的 /screen 接口 → 本地文件），所以截图照常工作。
# 状态读写都在 HA 私有的 .storage 里；视图**每次请求**现读，所以开关即时生效、
#   不需要重载集成，HA 重启也只是把落盘的值读回来。
# --------------------------------------------------------------------------- #
async def async_load_remote_state(hass):
    """（幂等）加载全局「远程桌面」开关：没落过盘时按**开启**处理（保持升级前行为）。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    if "remote_enabled" in store_data:
        return store_data["remote_enabled"]
    store = Store(hass, _REMOTE_STORE_VERSION, _REMOTE_STORE_KEY)
    store_data["remote_store"] = store
    data = await _async_load_with_legacy(
        store, _REMOTE_STORE_VERSION, _REMOTE_STORE_KEY, (_LEGACY_REMOTE_STORE_KEY,)
    )
    store_data["remote_enabled"] = bool(data.get("enabled", True))
    return store_data["remote_enabled"]


def remote_desktop_enabled(hass):
    """「远程桌面」总开关当前是否开启（状态尚未加载时按开启，避免误拦）。"""
    store_data = hass.data.get(_DATA_KEY) or {}
    return bool(store_data.get("remote_enabled", True))


def _disabled_response(json_body=False):
    """总开关关闭时的统一拒绝响应（页面用文本、数据接口用 JSON）。"""
    if json_body:
        return web.json_response({"error": _REMOTE_DISABLED_TEXT}, status=403)
    return web.Response(
        text=_REMOTE_DISABLED_TEXT, status=403, content_type="text/plain", charset="utf-8"
    )


def _blocked_response(request, json_body=False):
    """总开关关闭 → 返回拒绝响应；开启 → 返回 None（调用方继续处理）。"""
    if remote_desktop_enabled(request.app["hass"]):
        return None
    return _disabled_response(json_body)


def console_sessions(hass):
    """当前活着的控制台 WebSocket 会话集合（停用总开关时要逐个掐断）。"""
    store_data = hass.data.setdefault(_DATA_KEY, {})
    return store_data.setdefault("console_sessions", set())


async def async_close_console_sessions(hass):
    """掐断所有正在跑的控制台 WebSocket 会话（关掉总开关时调用）。

    视图那边已经把 client_ws 登记进集合；这里逐个 close，pump 循环随即结束、
    上游 ESXi 连接在 finally 里被释放 —— 不需要额外的"轮询复查"。
    """
    sessions = console_sessions(hass)
    if not sessions:
        return
    _LOGGER.info("远程桌面已停用：断开 %s 个正在运行的控制台会话", len(sessions))
    for client_ws in list(sessions):
        if client_ws.closed:
            sessions.discard(client_ws)
            continue
        try:
            # 1001 = going away：正常关闭语义，前端按"被管理员停用"提示
            await client_ws.close(code=1001, message=b"remote desktop disabled")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("断开控制台会话失败", exc_info=True)
        finally:
            sessions.discard(client_ws)


async def _unlink_one_entry(hass, entry_id):
    """清空该条目下 VM 设备的控制台链接与主机设备的管理页链接。"""
    entry_data = _entry_data(hass, entry_id)
    if not entry_data:
        return 0
    dev_registry = dr.async_get(hass)
    cleared = 0
    for vm_key in entry_data.get("vm") or {}:
        device = device_by_identifier(
            dev_registry, (DOMAIN, vm_device_key(entry_id, vm_key))
        )
        if device is None:
            continue
        if device.configuration_url:
            # configuration_url=None 会把设备页的「访问」按钮收掉
            dev_registry.async_update_device(device.id, configuration_url=None)
        cleared += 1
    host_device = _host_device(hass, entry_id)
    if host_device is not None and host_device.configuration_url:
        dev_registry.async_update_device(host_device.id, configuration_url=None)
    return cleared


async def _unlink_all_entries(hass):
    """清空全部条目的访问链接（总开关关闭时调用）。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        try:
            await _unlink_one_entry(hass, entry.entry_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("清空条目 %s 的访问链接失败", entry.entry_id)


async def async_set_remote_desktop(hass, enabled):
    """设置全局「远程桌面」总开关：落盘 + 立即生效。

    开 → 立刻给所有条目重签设备页「访问」链接；
    关 → 清空设备页链接 + 掐断所有正在跑的控制台 WebSocket 会话。
    HTTP 视图只在请求时读 :func:`remote_desktop_enabled`，所以**不用重载集成**；
    落盘保证 HA 重启后仍是用户选的那个状态（不会退回默认值）。
    """
    enabled = bool(enabled)
    if "remote_enabled" not in (hass.data.get(_DATA_KEY) or {}):
        await async_load_remote_state(hass)
    store_data = hass.data.setdefault(_DATA_KEY, {})
    changed = store_data.get("remote_enabled") != enabled
    store_data["remote_enabled"] = enabled
    store = store_data.get("remote_store")
    if store is not None:
        try:
            await store.async_save({"enabled": enabled})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("保存「远程桌面」开关状态失败")
    if not changed:
        return
    _LOGGER.info("远程桌面总开关：%s", "已启用" if enabled else "已停用")
    if enabled:
        await _link_all_entries(hass)
    else:
        await _unlink_all_entries(hass)
        await async_close_console_sessions(hass)
    # 让「虚拟机管理」等读取开关状态的实体立刻刷新显示，不用等下一次轮询
    hass.bus.async_fire(f"{DOMAIN}_remote_desktop_changed", {"enabled": enabled})


def _host_device(hass, entry_id):
    """该条目第一台 ESXi 主机设备（主机管理页链接挂在它上面）。"""
    entry_data = _entry_data(hass, entry_id)
    if not entry_data:
        return None
    config = hass.config_entries.async_get_entry(entry_id)
    host_key = next(
        iter(entry_data.get("vmhost") or {}),
        str((config.data.get("host") if config else "") or "unknown").replace(".", "_"),
    )
    return device_by_identifier(
        dr.async_get(hass), (DOMAIN, host_device_key(entry_id, host_key))
    )


async def _link_one_entry(hass, entry_id):
    """给该条目下 VM 设备签控制台链接、主机设备签管理页链接。

    返回 ``{"vm": VM 链接数, "host": 主机管理链接是否已写入}``。
    主机设备实体可能比 VM 设备晚注册，``host=False`` 时调用方应继续重试。
    """
    entry_data = _entry_data(hass, entry_id)
    if not entry_data:
        return {"vm": 0, "host": False}

    # 总开关关着：不但不签新链接，还要把设备页上已有的「访问」链接清掉 ——
    #   否则那把 24 小时有效的旧钥匙还能用。周期重签（12h）与「同步完成」事件
    #   走的也是这个函数，所以关闭状态不会被后台任务悄悄改回来。
    if not remote_desktop_enabled(hass):
        await _unlink_one_entry(hass, entry_id)
        return {"vm": 0, "host": False}

    # 设备链接必须是绝对 URL（设备注册表会校验 scheme + host，拒绝相对路径）
    base_url = _configured_base(hass, entry_id)

    dev_registry = dr.async_get(hass)
    linked = 0
    for vm_key in entry_data.get("vm") or {}:
        device = device_by_identifier(
            dev_registry, (DOMAIN, vm_device_key(entry_id, vm_key))
        )
        if device is None:
            # 平台实体还没把设备建出来（首次加载时会晚几秒），等同步事件下一轮
            continue
        page_path = (
            f"{CONSOLE_PAGE_PREFIX}?entry={quote(entry_id)}&vm={quote(vm_key)}"
        )
        signed = _sign(hass, page_path)
        configuration_url = f"{base_url}{signed}" if base_url else signed
        if device.configuration_url != configuration_url:
            dev_registry.async_update_device(
                device.id, configuration_url=configuration_url
            )
        linked += 1

    # 主机设备 → 虚拟机管理悬浮窗（创建 / 克隆 / 改配 / 删除），与每台 VM 的
    #   控制台链接互不相同：主机页只能管理，VM 页只有那一台的控制台
    config = hass.config_entries.async_get_entry(entry_id)
    host_key = next(
        iter(entry_data.get("vmhost") or {}),
        str((config.data.get("host") if config else "") or "unknown")
        .replace(".", "_"),
    )
    host_linked = False
    host_device = device_by_identifier(
        dev_registry, (DOMAIN, host_device_key(entry_id, host_key))
    )
    if host_device is not None:
        admin_path = f"{ADMIN_PAGE_PREFIX}?entry={quote(entry_id)}" \
                     f"&host={quote(host_key)}"
        admin_signed = _sign(hass, admin_path)
        admin_url = f"{base_url}{admin_signed}" if base_url else admin_signed
        if host_device.configuration_url != admin_url:
            dev_registry.async_update_device(
                host_device.id, configuration_url=admin_url
            )
        host_linked = True
    return {"vm": linked, "host": host_linked}


async def _link_all_entries(hass):
    """重签全部已加载条目（定时任务 / 同步事件共用）。"""
    total = 0
    for entry in hass.config_entries.async_entries(DOMAIN):
        try:
            total += (await _link_one_entry(hass, entry.entry_id))["vm"]
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("刷新条目 %s 的控制台链接失败", entry.entry_id)
    if total:
        _LOGGER.debug("控制台设备链接已刷新：%s 台虚拟机", total)


#: 启动后重签的重试节奏：立即先签一次，之后每 10 秒一轮，最多约 4 分钟。
#: 必须覆盖「集成 setup 很慢（实测可达 57 秒）」的窗口，不能再像旧版那样
#: 只在 setup 完后傻等 25 秒试一次 —— HA 每次重启会换掉 authSig 的内存签名
#: 密钥，设备注册表里的旧链接在重签完成前打开一律 401。
_LINK_RETRY_INTERVAL = 10
_LINK_MAX_ATTEMPTS = 25


async def _initial_link_loop(hass, entry_id):
    """启动后反复重签该条目的控制台链接，直到每台 VM 设备都挂上本次进程的签名。

    设备记录在设备注册表里是**跨重启持久**的，所以哪怕集成数据/实体平台都还没
    加载完，第一轮通常就能直接签上；数据尚未拉到时（vm 为空）则等下一轮。
    """
    for _attempt in range(_LINK_MAX_ATTEMPTS):
        # 总开关关着：没有链接要签（反而要把旧链接清掉），直接收工，
        #   不然会白等 25 轮并刷一条"未能重签"的告警
        if not remote_desktop_enabled(hass):
            await _unlink_one_entry(hass, entry_id)
            return
        # 条目被卸载/删除：停止
        if not hass.config_entries.async_get_entry(entry_id):
            return
        entry_data = _entry_data(hass, entry_id)
        if entry_data is None:
            return
        try:
            result = await _link_one_entry(hass, entry_id)
            linked = result["vm"]
            host_linked = result["host"]
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("初始控制台链接写入失败（条目 %s）", entry_id)
            linked, host_linked = 0, False

        expected = len(entry_data.get("vm") or {})
        client = entry_data.get("client")
        first_pull_done = bool(client and getattr(client, "last_refresh_at", 0))
        # VM 链接全部签上 **且** 主机管理链接已写入（主机设备实体可能晚注册，
        #   没出现就继续重试到窗口结束，同步事件/12 小时定时还会兜底）
        if (linked >= expected and host_linked
                and (expected > 0 or first_pull_done)):
            if linked:
                _LOGGER.debug("启动控制台链接重签完成：%s 台虚拟机", linked)
            return
        await asyncio.sleep(_LINK_RETRY_INTERVAL)

    _LOGGER.warning(
        "条目 %s 的控制台链接在 %s 秒内未能全部重签完成，"
        "设备页「访问」可能出现 401；下一轮定时任务（12 小时）或同步服务会补签",
        entry_id,
        _LINK_RETRY_INTERVAL * _LINK_MAX_ATTEMPTS,
    )


def schedule_initial_link(hass, entry_id):
    """幂等地为一个条目启动重签轮询（重复调用不会叠任务）。"""
    store = hass.data.setdefault(_DATA_KEY, {})
    tasks = store.setdefault("link_tasks", {})
    old = tasks.get(entry_id)
    if old is not None and not old.done():
        return
    tasks[entry_id] = hass.async_create_task(_initial_link_loop(hass, entry_id))


async def async_load_learned_bases(hass):
    """（幂等）加载历史学到的控制台访问基地址。

    必须在首次重签设备链接前完成，否则重启后第一轮会用 HA 探测到的（端口映射下
    往往不可达的）地址写链接，要等下一轮重试才纠正。
    """
    store_data = hass.data.setdefault(_DATA_KEY, {})
    if "learned_bases" in store_data:
        return
    base_store = Store(hass, _BASE_STORE_VERSION, _BASE_STORE_KEY)
    data = await _async_load_with_legacy(
        base_store, _BASE_STORE_VERSION, _BASE_STORE_KEY, (_LEGACY_BASE_STORE_KEY,)
    )
    store_data["base_store"] = base_store
    learned = dict(data.get("bases") or {})

    # 过继：集成被删除后重新添加会得到全新的 entry_id，而学到的地址是按旧 id
    #   存的 —— 新条目查不到记忆就退回 HA 自探测地址（往往是容器/另一张网卡的
    #   内网 IP，外部网络根本不可达）。基地址是「访问 HA 的地址」，与具体条目
    #   无关，所以当现存条目缺失记忆、而存储里恰好只剩一个无主记忆时，直接过继。
    live_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
    missing = [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)
               if not _normalize_base(learned.get(e.entry_id))]
    orphan_values = {
        _normalize_base(v)
        for k, v in learned.items()
        if k not in live_ids and _normalize_base(v)
    }
    if missing and len(orphan_values) == 1:
        inherited = orphan_values.pop()
        for entry_id in missing:
            learned[entry_id] = inherited
        _LOGGER.info("控制台访问地址从历史记忆过继给新集成条目：%s", inherited)
        try:
            await base_store.async_save({"bases": dict(learned)})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("过继控制台访问地址后落盘失败")

    store_data["learned_bases"] = learned


# --------------------------------------------------------------------------- #
# View 1：控制台页面
# --------------------------------------------------------------------------- #
class ConsolePageView(HomeAssistantView):
    """控制台 HTML 页面（路径带 authSig 才能访问）。"""

    requires_auth = True
    url = CONSOLE_PAGE_PREFIX
    name = "esxi_control:console"

    async def get(self, request):
        """渲染页面：把按 VM 签好名的票据 / WS 路径注入模板。"""
        try:
            return await self._get(request)
        except Exception:  # pylint: disable=broad-except
            # 把真实堆栈直接回显到页面，避免前端只看到一句无信息的 500
            _LOGGER.exception("控制台页面渲染失败")
            import traceback

            return web.Response(
                text="控制台页面渲染失败：\n\n" + traceback.format_exc(),
                status=500,
                content_type="text/plain",
                charset="utf-8",
            )

    async def _get(self, request):
        """实际渲染逻辑（get 的可测/可兜底版本）。"""
        hass = request.app["hass"]
        # 总开关关着：连页面都不给（设备页的「访问」链接此时也已被清除）
        if not remote_desktop_enabled(hass):
            return _disabled_response()
        entry_id = request.query.get("entry", "")
        vm_key = request.query.get("vm", "")
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.Response(
                text="该 ESXi 集成条目不存在或尚未加载完成。",
                status=404,
                content_type="text/plain",
                charset="utf-8",
            )
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.Response(
                text="找不到指定的虚拟机（可能已被删除，请从设备页重新进入）。",
                status=404,
                content_type="text/plain",
                charset="utf-8",
            )

        # 记住浏览器实际打开页面用的 host:端口（端口映射下设备链接自动跟随地址栏）
        await _remember_origin(hass, entry_id, request)

        # 票据接口按「这一台 VM」单独签名。WS 地址不能在页面渲染时签：
        #   HA 的 authSig 把全部 query 参数都纳入校验，而 ticket 要等浏览器
        #   打开页面后才现取，所以 WS 路径改由票据接口在发票时一并签好返回。
        ticket_path = _sign(
            hass, f"{CONSOLE_TICKET_PREFIX}/{quote(entry_id)}/{quote(vm_key)}"
        )
        # 光盘状态/分片上传/弹出删除共用同一路径（GET/POST/DELETE），签一次即可
        cdrom_path = _sign(
            hass, f"{CONSOLE_CDROM_PREFIX}/{quote(entry_id)}/{quote(vm_key)}"
        )
        # 来宾管理（进程/文件/注册表）独立页面，同样按这一台 VM 签名
        guest_path = _sign(
            hass,
            f"{CONSOLE_GUEST_PAGE_PREFIX}?entry={quote(entry_id)}"
            f"&vm={quote(vm_key)}",
        )

        # 读盘 + 内联 vendor JS 全部放到 executor —— 新版 HA 会直接把事件循环里的
        #   阻塞 open 记为违规告警
        template = await hass.async_add_executor_job(_page_template)
        html = (
            template
            .replace('"__TICKET_PATH__"', json.dumps(ticket_path))
            .replace('"__CDROM_PATH__"', json.dumps(cdrom_path))
            .replace('"__GUEST_PATH__"', json.dumps(guest_path))
            .replace(
                '"__VM_NAME__"',
                json.dumps(vm_data.get("vm_name") or vm_key),
            )
        )
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


# --------------------------------------------------------------------------- #
# View 2：静态资源（不含敏感信息，允许缓存，白名单制）
# --------------------------------------------------------------------------- #
class ConsoleStaticView(HomeAssistantView):
    """Serve whitelisted static assets for the console page."""

    requires_auth = False
    url = CONSOLE_STATIC_PREFIX + r"/{file_path:.*}"
    name = "esxi_control:console_static"

    async def get(self, request, file_path):
        """Return a whitelisted file from www_console."""
        spec = _STATIC_FILES.get(file_path)
        if spec is None:
            raise web.HTTPNotFound()
        content_type, no_store = spec
        full_path = os.path.join(_WWW_DIR, *file_path.split("/"))
        if not os.path.isfile(full_path):
            raise web.HTTPNotFound()
        return web.FileResponse(
            full_path,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-store" if no_store else "public, max-age=86400",
            },
        )


# --------------------------------------------------------------------------- #
# View 3：票据接口（服务端换票，浏览器拿不到 ESXi 凭据）
# --------------------------------------------------------------------------- #
class ConsoleTicketView(HomeAssistantView):
    """Acquire a WebMKS/VMRC ticket on behalf of the browser."""

    requires_auth = True
    url = CONSOLE_TICKET_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:console_ticket"

    async def get(self, request, entry_id, vm_key):
        """Return JSON ticket descriptor (see esxi.acquire_console_ticket)."""
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.json_response(
                {"mode": "error", "error": "集成条目不存在或未加载"}, status=404
            )
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.json_response(
                {"mode": "error", "error": "虚拟机不存在"}, status=404
            )
        moid = vm_data.get("moid")
        if not moid:
            return web.json_response(
                {"mode": "error", "error": "虚拟机缺少 moId，请先执行一次实体同步"},
                status=409,
            )

        config = hass.config_entries.async_get_entry(entry_id).data
        conn_details = {
            "host": config["host"],
            "user": config["username"],
            "pwd": config["password"],
            "port": config["port"],
            "ssl": config["verify_ssl"],
        }
        result = await hass.async_add_executor_job(
            acquire_console_ticket, conn_details, str(moid)
        )

        # webmks：把 ticket 纳入签名后返回 WS 相对路径。浏览器必须原样使用
        #   （不能再加 / 改 query 参数，否则 authSig 校验不过）
        if result.get("mode") == "webmks" and result.get("ticket"):
            ws_unsigned = (
                f"{CONSOLE_WS_PREFIX}/{quote(entry_id)}/{quote(vm_key)}"
                f"?ticket={quote(str(result['ticket']), safe='')}"
            )
            result["ws_path"] = _sign(hass, ws_unsigned)

        status = 200 if result.get("mode") != "error" else 502
        return web.json_response(result, status=status)


# --------------------------------------------------------------------------- #
# View 3.5：前端心跳上报「当前地址栏来源」（同源 cookie 鉴权即可）
# --------------------------------------------------------------------------- #
class ConsoleOriginView(HomeAssistantView):
    """Report the browser's actual origin so device links follow the URL bar."""

    requires_auth = True
    url = CONSOLE_ORIGIN_PREFIX
    name = "esxi_control:console_origin"

    async def get(self, request):
        """Record request origin for every non-pinned entry; cheap no-op if unchanged."""
        hass = request.app["hass"]
        try:
            changed = await remember_origin_for_all_entries(hass, request)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("控制台来源地址心跳处理失败")
            return web.json_response({"ok": False}, status=500)
        return web.json_response({"ok": True, "changed": bool(changed)})


# --------------------------------------------------------------------------- #
# View 3.8：光盘 ISO 上传 / 状态 / 弹出删除（multipart，全链在服务端完成）
# --------------------------------------------------------------------------- #
class ConsoleIsoView(HomeAssistantView):
    """Upload files -> (pack ISO) -> datastore -> mount; status; eject+delete.

    ``requires_auth=False``：新版 HA 的中间件不接受 POST/DELETE 上的 authSig，
    而控制台页面手上只有签名没有 bearer token，因此三个方法统一在入口调用
    :func:`_verify_signed_request` 自行验签（GET 中间件本会放行，这里重复验一次
    也无害，保证三个方法鉴权语义一致）。
    """

    requires_auth = False
    url = CONSOLE_CDROM_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:console_cdrom"

    async def get(self, request, entry_id, vm_key):
        """返回该 VM 当前光盘状态 + 是否为本集成上传（决定弹出时是否删文件）。"""
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.json_response({"error": "集成条目不存在或未加载"}, status=404)
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.json_response({"error": "虚拟机不存在"}, status=404)
        iso_cdrom = next(
            (
                cd
                for cd in vm_data.get("cdroms") or []
                if cd.get("backing") == "iso"
            ),
            None,
        )
        record = _cdrom_record(hass, entry_id, vm_key)
        return web.json_response(
            {
                "mounted": bool(iso_cdrom),
                "iso": (iso_cdrom or {}).get("iso"),
                "connected": bool((iso_cdrom or {}).get("connected")),
                "ha_managed": bool(
                    record and iso_cdrom and record.get("iso") == iso_cdrom.get("iso")
                ),
                "record": record,
            }
        )

    #: 分片大小必须小于 HA http 的请求体上限（默认 16MB），4MB 留足余量
    CHUNK_SIZE = 4 * 1024 * 1024
    #: 未完成的上传会话保留多久（崩溃/关页后的临时目录懒清理）
    _STAGING_TTL = 7200

    async def post(self, request, entry_id, vm_key):
        """接收一个上传分片；最后一片（X-Finalize: 1）触发打包/上传/挂载。

        HA 的 HTTP 层把单请求体限制在 16MB，ISO 常常是几百 MB～几 GB，
        所以整条上传链路走**分片协议**（路径始终不变，authSig 持续有效）：

          * 每片 body 是裸字节（application/octet-stream）；
          * X-Upload-Id  本批上传的唯一 id；
          * X-Files       base64url(JSON) 清单 [{"n":名,"a":归档路径,"s":大小}]
                          （每片都带，服务端只在会话首片采用）；
          * X-File / X-Chunk / X-Chunks  文件序号 / 分片序号(0基) / 该文件分片数；
          * X-Finalize=1  全局最后一片，到齐后服务端拼装→(打包)→传 ESXi→挂载。
        """
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.json_response({"error": "集成条目不存在或未加载"}, status=404)
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.json_response({"error": "虚拟机不存在"}, status=404)
        vm_uuid = vm_data.get("uuid")
        if not vm_uuid:
            return web.json_response({"error": "虚拟机缺少 UUID"}, status=409)

        upload_id = re.sub(r"[^A-Za-z0-9_-]", "", request.headers.get("X-Upload-Id", ""))
        if not upload_id:
            return web.json_response({"error": "缺少 X-Upload-Id"}, status=400)
        try:
            file_index = int(request.headers.get("X-File", "x"))
            chunk_index = int(request.headers.get("X-Chunk", "x"))
            chunk_total = int(request.headers.get("X-Chunks", "x"))
        except (TypeError, ValueError):
            return web.json_response({"error": "分片序号不合法"}, status=400)
        finalize = request.headers.get("X-Finalize", "") == "1"

        body = await request.read()
        if not body:
            return web.json_response({"error": "空分片"}, status=400)

        staging = self._get_staging(hass, upload_id, request.headers.get("X-Files", ""))
        if staging is None:
            return web.json_response(
                {"error": "上传会话不存在，或文件清单缺失/不合法"}, status=400
            )
        if not (0 <= file_index < len(staging["manifest"])):
            return web.json_response({"error": "文件序号越界"}, status=400)
        if not (0 <= chunk_index < chunk_total):
            return web.json_response({"error": "分片序号越界"}, status=400)

        chunk_path = os.path.join(
            staging["dir"], f"f{file_index:04d}_c{chunk_index:06d}"
        )
        # 分片由前端串行发送；同名重写（前端失败重试同一片时天然幂等）
        with open(chunk_path, "wb") as handle:
            handle.write(body)
        staging["last"] = time.time()

        if not finalize:
            return web.json_response({"ok": True, "received": True})

        # ---- 最后一片：拼装全部文件 → 判定直传/打包 → 上传挂载 ----
        tmp_dir = staging["dir"]
        try:
            collected = self._assemble(staging)
        except Exception as error:  # pylint: disable=broad-except
            self._discard_staging(hass, upload_id)
            return web.json_response({"error": f"分片校验失败：{error}"}, status=400)

        conn_details = _conn_details(hass, entry_id)
        try:
            # 上次本集成传的盘若还挂着/还在存储里，先弹出并删除，避免堆积
            old_record = _cdrom_record(hass, entry_id, vm_key)
            if old_record and old_record.get("ds") and old_record.get("path"):
                try:
                    await hass.async_add_executor_job(
                        eject_iso,
                        conn_details,
                        vm_uuid,
                        (old_record["ds"], old_record["path"]),
                    )
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.debug("挂载新盘前清理旧盘失败（继续尝试）", exc_info=True)

            # 单个 .iso 直接传；多个文件 / 文件夹先在本地打成 ISO9660
            if len(collected) == 1 and collected[0][0].lower().endswith(".iso"):
                _arcname, local_iso = collected[0]
                packed = False
            else:
                packed_iso = os.path.join(tmp_dir, "ha_packed.iso")
                await hass.async_add_executor_job(
                    build_iso, collected, packed_iso, "HA_ISO"
                )
                local_iso = packed_iso
                packed = True

            ts = time.strftime("%Y%m%d_%H%M%S")
            rand = upload_id[:6] if len(upload_id) >= 6 else uuid_lib.uuid4().hex[:6]
            vm_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(vm_key))[:24] or "vm"
            dest_name = f"ha_{vm_slug}_{ts}_{rand}.iso"

            result = await hass.async_add_executor_job(
                upload_and_mount,
                conn_details,
                vm_uuid,
                local_iso,
                ISO_UPLOAD_DIR,
                dest_name,
            )
        except Exception as error:  # pylint: disable=broad-except
            self._discard_staging(hass, upload_id)
            _LOGGER.exception("光盘上传/挂载失败")
            return web.json_response({"error": str(error)}, status=502)

        self._discard_staging(hass, upload_id)

        record = {
            "ds": result["ds"],
            "path": result["path"],
            "iso": result.get("iso"),
            "packed": packed,
            "files": len(collected),
            "ts": ts,
        }
        await _put_cdrom_record(hass, entry_id, vm_key, record)
        await refresh_data_now(hass, entry_id)
        return web.json_response({"ok": True, **record})

    # ---- 分片会话管理（全部同步快速操作，不碰网络）----
    def _get_staging(self, hass, upload_id, manifest_header):
        """取或建上传会话；带清单头时校验首片。"""
        store_data = hass.data.setdefault(_DATA_KEY, {})
        registry = store_data.setdefault("iso_staging", {})

        # 懒清理过期会话（顺带扫，不开定时器）
        now = time.time()
        for stale_id in [
            sid
            for sid, item in registry.items()
            if now - item.get("last", 0) > self._STAGING_TTL
        ]:
            self._discard_staging(hass, stale_id)

        staging = registry.get(upload_id)
        if staging is None:
            manifest = self._parse_manifest(manifest_header)
            if not manifest:
                return None
            tmp_dir = tempfile.mkdtemp(prefix=f"esxi_iso_{upload_id}_")
            staging = {
                "dir": tmp_dir,
                "manifest": manifest,
                "last": now,
            }
            registry[upload_id] = staging
        return staging

    @staticmethod
    def _parse_manifest(header):
        """X-Files 头 → [{"n","a","s"}]；非法/空清单返回 None。"""
        text = _unb64_name(header or "")
        if not text:
            return None
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return None
        manifest = []
        if not isinstance(raw, list):
            return None
        for item in raw:
            if not isinstance(item, dict):
                return None
            name = _safe_filename(item.get("n") or item.get("a") or "")
            arcname = str(item.get("a") or name).replace("\\", "/").strip("/")
            if not name or not arcname:
                return None
            try:
                size = int(item.get("s") or 0)
            except (TypeError, ValueError):
                return None
            manifest.append({"n": name, "a": arcname, "s": max(size, 0)})
        return manifest or None

    @staticmethod
    def _assemble(staging):
        """校验分片齐全并按序拼装；返回 [(arcname, assembled_path)]。"""
        tmp_dir = staging["dir"]
        manifest = staging["manifest"]
        assembled = []
        for index, meta in enumerate(manifest):
            # 按声明大小计算应有片数（与前端同公式），到不齐直接报错
            total = max(
                1,
                (meta["s"] + ConsoleIsoView.CHUNK_SIZE - 1)
                // ConsoleIsoView.CHUNK_SIZE,
            ) if meta["s"] else 1
            # 临时拼装名只用序号：真实文件名走 arcname（Joliet 保留中文原名），
            #   本地名无需可读，还能避开中文清洗后互相碰撞
            out_path = os.path.join(tmp_dir, f"assembled_{index:04d}")
            actual = 0
            with open(out_path, "wb") as out:
                for chunk_index in range(total):
                    part = os.path.join(
                        tmp_dir, f"f{index:04d}_c{chunk_index:06d}"
                    )
                    if not os.path.isfile(part):
                        raise RuntimeError(
                            f"文件 {meta['a']} 缺少第 {chunk_index + 1}/{total} 片"
                        )
                    with open(part, "rb") as handle:
                        shutil.copyfileobj(handle, out, 1024 * 1024)
                    actual += os.path.getsize(part)
            if meta["s"] and actual != meta["s"]:
                raise RuntimeError(
                    f"文件 {meta['a']} 大小不符（收到 {actual}，预期 {meta['s']}）"
                )
            assembled.append((meta["a"], out_path))
        return assembled

    @staticmethod
    def _discard_staging(hass, upload_id):
        """删除上传会话的临时目录与登记。"""
        registry = hass.data.get(_DATA_KEY, {}).get("iso_staging") or {}
        staging = registry.pop(upload_id, None)
        if staging is not None:
            shutil.rmtree(staging.get("dir", ""), ignore_errors=True)

    async def delete(self, request, entry_id, vm_key):
        """弹出光盘；本集成上传的 ISO 一并删除数据存储文件并清记录。"""
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.json_response({"error": "集成条目不存在或未加载"}, status=404)
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.json_response({"error": "虚拟机不存在"}, status=404)
        vm_uuid = vm_data.get("uuid")
        if not vm_uuid:
            return web.json_response({"error": "虚拟机缺少 UUID"}, status=409)

        record = _cdrom_record(hass, entry_id, vm_key)
        delete_path = None
        if record and record.get("ds") and record.get("path"):
            delete_path = (record["ds"], record["path"])
        try:
            result = await hass.async_add_executor_job(
                eject_iso, _conn_details(hass, entry_id), vm_uuid, delete_path
            )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.exception("弹出光盘失败")
            return web.json_response({"error": str(error)}, status=502)

        if record is not None:
            await _pop_cdrom_record(hass, entry_id, vm_key)
        await refresh_data_now(hass, entry_id)
        return web.json_response({"ok": True, **result})

    @staticmethod
    async def _collect_parts(request, tmp_dir):
        """流式接收 multipart 文件到临时目录。

        返回 [(arcname, local_path, size)]。arcname 优先取表单字段名里的
        base64url 相对路径（文件夹上传），否则用 filename 的 basename。
        全程落盘不进内存，ISO 可能达数 GB。
        """
        reader = await request.multipart()
        collected = []
        index = 0
        while True:
            part = await reader.next()
            if part is None:
                break
            if not getattr(part, "filename", None):
                # 非文件的普通字段直接丢弃
                continue
            index += 1
            local_name = f"f_{index:04d}_{_safe_filename(part.filename)}"
            local_path = os.path.join(tmp_dir, local_name)
            size = 0
            with open(local_path, "wb") as handle:
                while True:
                    try:
                        chunk = await part.read_chunk(size=1024 * 1024)
                    except AttributeError:
                        # 老版本 aiohttp 不支持 size 参数
                        chunk = await part.read_chunk()
                    if not chunk:
                        break
                    handle.write(chunk)
                    size += len(chunk)
            if size == 0:
                continue
            arcname = _unb64_name(part.name)
            if not arcname:
                arcname = _safe_filename(part.filename)
            collected.append((arcname, local_path, size))
        return collected


# --------------------------------------------------------------------------- #
# View 4：同源 WebSocket 代理（浏览器 ↔ HA ↔ wss://ESXi/ticket/<ticket>）
# --------------------------------------------------------------------------- #
class ConsoleWebSocketView(HomeAssistantView):
    """Proxy browser WebSocket to ESXi WebMKS endpoint.

    流程：浏览器连本代理（带 authSig）→ 服务端先取 webmks 票据 → 用票据向上游
    ESXi 建 wss → 双向透明转发二进制/文本帧。VMRC 降级模式不走这里（浏览器直接
    调起本机 VMRC 客户端）。
    """

    requires_auth = True
    url = CONSOLE_WS_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:console_ws"

    async def get(self, request, entry_id, vm_key):
        """Handle one proxied console session."""
        hass = request.app["hass"]
        # 总开关关着：正在跑的老会话也已经被 async_set_remote_desktop 掐断，
        #   这里挡住的是"页面还开着、重连/新开"的请求
        if not remote_desktop_enabled(hass):
            raise web.HTTPForbidden(text=_REMOTE_DISABLED_TEXT)
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            raise web.HTTPNotFound()
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None or not vm_data.get("moid"):
            raise web.HTTPNotFound()

        # 票据由页面在升级前通过票据接口取得后带在 query 上 —— 一次控制台会话
        #   只换一次票（webmks 票据一次性，重复换票会让上一张失效）
        ticket_value = request.query.get("ticket", "")
        if not ticket_value:
            # 直接访问 WS 端点（没经过页面）：拒绝并关闭
            raise web.HTTPBadRequest(text="missing ticket")

        config = hass.config_entries.async_get_entry(entry_id).data

        # WebMKS（wmks.js）打开 WebSocket 时带 Sec-WebSocket-Protocol: binary，
        #   且在 onopen 里**强制校验**协商结果：不是 binary/vmware-vvc 就直接
        #   fail("no agreement on protocol")，画面永远连不上。aiohttp 默认不协商
        #   任何子协议（返回空），所以必须在这里显式声明支持，让它回带 binary。
        client_ws = web.WebSocketResponse(
            protocols=("binary", "vmware-vvc", "uint8utf8"),
            max_msg_size=0,  # 入站帧不限制（保持与上游一致，防御未来大帧）
        )
        # aiohttp 版本兼容（实测 HA 2026.4 自带 aiohttp 3.13.5）：
        #   prepare() 的返回值是 payload_writer，它的 .protocol 是 RequestHandler
        #   连接对象而不是子协议字符串，绝不能从返回值取协议！协商出的子协议在
        #   WebSocketResponse.ws_protocol（新版）/ .protocol（旧版，str）。
        #   必须 isinstance(str) 校验，防止对象混入上游 ws_connect 的 join。
        await client_ws.prepare(request)
        # 登记会话：关掉「远程桌面」总开关时要逐个掐断（含正在远程控制的画面）
        sessions = console_sessions(hass)
        sessions.add(client_ws)
        subprotocol = getattr(client_ws, "ws_protocol", None)
        if not isinstance(subprotocol, str):
            legacy = getattr(client_ws, "protocol", None)
            subprotocol = legacy if isinstance(legacy, str) and legacy else None
        if not subprotocol:
            # 浏览器（wmks.js）固定请求 binary，协商不到也按 binary 对上游声明
            subprotocol = "binary"

        ssl_ctx = await _upstream_ssl_context(hass)
        upstream_url = (
            f"wss://{config['host']}:{int(config.get('port') or 443)}"
            f"/ticket/{quote(ticket_value, safe='')}"
        )
        _LOGGER.info(
            "虚拟机控制台代理建立：%s（moId=%s，子协议=%s）",
            vm_data.get("vm_name") or vm_key,
            vm_data["moid"],
            subprotocol,
        )

        try:
            # total=None 至关重要：aiohttp 默认总超时 5 分钟，会把长连接的控制台
            #   在 5 分钟时整个掐掉；只保留建连阶段的超时。
            session_timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                async with session.ws_connect(
                    upstream_url,
                    ssl=ssl_ctx,
                    # ESXi hostd 的 /ticket 端点同样要求带子协议，回带 binary；
                    #   不开 heartbeat：MKS 端点是极简 WS 实现，不请自来的 PING
                    #   可能被当成异常帧，控制台保活由 WMKS/RFB 自身完成
                    protocols=(subprotocol,),
                    max_msg_size=0,  # 画面帧可能较大，不限制
                ) as upstream_ws:

                    async def pump_upstream():
                        """浏览器 → ESXi。"""
                        async for msg in client_ws:
                            if msg.type == WSMsgType.BINARY:
                                await upstream_ws.send_bytes(msg.data)
                            elif msg.type == WSMsgType.TEXT:
                                await upstream_ws.send_str(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break

                    async def pump_downstream():
                        """ESXi → 浏览器。"""
                        msg = None
                        async for msg in upstream_ws:
                            if msg.type == WSMsgType.BINARY:
                                await client_ws.send_bytes(msg.data)
                            elif msg.type == WSMsgType.TEXT:
                                await client_ws.send_str(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break
                        _LOGGER.info(
                            "控制台上游（ESXi）关闭：code=%s reason=%s",
                            upstream_ws.close_code,
                            getattr(msg, "data", ""),
                        )

                    task_up = asyncio.create_task(pump_upstream())
                    task_down = asyncio.create_task(pump_downstream())
                    _, pending = await asyncio.wait(
                        {task_up, task_down}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
        except aiohttp.ClientResponseError as error:
            # ESXi 拒绝 WebSocket 升级（票据失效 / 子协议不被接受时会带 HTTP 状态码）
            _LOGGER.warning(
                "控制台代理连不上 ESXi（%s）：HTTP %s %s",
                vm_data.get("vm_name"),
                error.status,
                error.message,
            )
            if not client_ws.closed:
                await client_ws.close(
                    code=1011, message=b"upstream handshake failed"
                )
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.warning("控制台代理异常（%s）：%s", vm_data.get("vm_name"), error)
            if not client_ws.closed:
                await client_ws.close()
        finally:
            sessions.discard(client_ws)
            _LOGGER.info("虚拟机控制台代理结束：%s", vm_data.get("vm_name"))

        return client_ws


# --------------------------------------------------------------------------- #
# Guest Operations：来宾管理页面 / JSON API / 文件上传下载
#
# 入口与鉴权完全复用控制台那一套：设备页「访问」打开控制台 → 顶栏「来宾」按钮
# 跳到本页，两个页面的 URL 都按「这一台 VM」用 authSig 签名（24h 有效）。
# 浏览器手上没有 bearer token，所以 POST/GET 的数据接口与光盘接口一样：
# requires_auth=False，入口统一走 _verify_signed_request 自行验签。
# ESXi 与来宾账号密码都不出服务端；签名只覆盖被访问的那一台 VM。
# --------------------------------------------------------------------------- #
_GUEST_WWW_DIR = os.path.join(_HERE, "www_guest")
_GUEST_INDEX = os.path.join(_GUEST_WWW_DIR, "index.html")

#: 上传走 HA 自带的 HTTP server，请求体受 HA 16MB 上限约束，留 256KB 余量
_GUEST_UPLOAD_LIMIT = 16 * 1024 * 1024 - 256 * 1024
#: 下载到 HA 的临时文件目录保留多久（视图每次被调用时懒清理）
_GUEST_DL_TTL = 6 * 3600


def _resolve_guest_target(hass, entry_id, vm_key):
    """来宾接口统一的目标解析。

    返回 ``(error_response, target)``：前置不满足时第一个元素非空，调用方直接
    ``return`` 它即可。``target`` 里已经决定了这台 VM 走哪条通道（Tools / SSH）。
    """
    entry_data = _entry_data(hass, entry_id)
    if not entry_data:
        return web.json_response(
            {"error": "集成条目不存在或尚未加载完成"}, status=404
        ), None
    vm_data = _vm_snapshot(entry_data, vm_key)
    if vm_data is None:
        return web.json_response(
            {"error": "虚拟机不存在（可能已被删除）"}, status=404
        ), None
    try:
        target = guest_router.build_target(
            hass, get_manager(hass), entry_id, vm_key,
            _conn_details(hass, entry_id), vm_data,
        )
    except GuestOpsError as error:
        # 凭据/通道没配好属于"前置条件不满足"，与参数错误（400）区分开
        return web.json_response({"error": str(error)}, status=409), None
    return None, target


#: 连接设置相关的 op：读写本机存储 / 试探 SSH / 删除凭据，不碰 ESXi
_CONN_OPS = frozenset((
    "get_conn", "set_conn", "test_conn", "forget_hostkey", "remove_conn",
))


def _conn_view(manager, entry_id, vm_key, vm_data):
    """连接设置的只读视图。**绝不回传密码**，只回"设过没有"。"""
    record = manager.get_cred(entry_id, vm_key) or {}
    tools = record.get("tools") or {}
    ssh = record.get("ssh") or {}
    auto_host = str((vm_data or {}).get("guest_ip") or "")
    if auto_host.lower() in ("n/a", "unknown"):
        auto_host = ""
    return {
        "transport": manager.transport_setting(entry_id, vm_key),
        "resolved": manager.resolved_transport(entry_id, vm_key, vm_data),
        "tools": {
            "username": tools.get("username", ""),
            "domain": tools.get("domain", ""),
            "has_password": bool(tools.get("password")),
        },
        "ssh": {
            "host": ssh.get("host", ""),
            "port": int(ssh.get("port") or 22),
            "username": ssh.get("username", ""),
            "has_password": bool(ssh.get("password")),
            "verify": bool(ssh.get("verify", True)),
            "fingerprint": ssh.get("hostkey", ""),
        },
        "auto_host": auto_host,
        "guest_os": str((vm_data or {}).get("guest_os") or ""),
        "tools_status": str((vm_data or {}).get("tools_status") or ""),
        "vm_name": str((vm_data or {}).get("vm_name") or vm_key),
    }


async def _handle_conn_op(hass, entry_id, vm_key, op, data):
    """连接设置的读 / 写 / 测试（页面「系统连接设置」弹层用）。"""
    manager = get_manager(hass)
    if manager is None:
        return web.json_response({"error": "系统连接管理器尚未初始化"}, status=503)
    entry_data = _entry_data(hass, entry_id)
    vm_data = _vm_snapshot(entry_data, vm_key) if entry_data else None

    try:
        if op == "get_conn":
            return web.json_response(_conn_view(manager, entry_id, vm_key, vm_data))

        if op == "set_conn":
            transport = str(data.get("transport") or "auto").strip()
            ssh_in = data.get("ssh") or {}
            tools_in = data.get("tools") or {}
            ssh_user = str(ssh_in.get("username") or "").strip()
            if transport == "ssh" and not ssh_user:
                return web.json_response(
                    {"error": "选择 SSH 通道时，SSH 用户名不能为空"}, status=400
                )
            await manager.async_set_ssh(
                entry_id, vm_key,
                host=str(ssh_in.get("host") or ""),
                port=ssh_in.get("port") or 22,
                username=ssh_user,
                password=str(ssh_in.get("password") or ""),   # 空 = 保留原密码
                verify=bool(ssh_in.get("verify", True)),
            )
            tools_user = str(tools_in.get("username") or "").strip()
            if tools_user:
                # 只在这里填过 Tools 账号才动 tools 那一份（空 = 保留原密码）
                await manager.async_set_cred(
                    entry_id, vm_key, tools_user,
                    str(tools_in.get("password") or ""),
                    str(tools_in.get("domain") or "").strip(),
                )
            await manager.async_set_transport(entry_id, vm_key, transport)
            # 保存后立刻校验一次：徽标和二进传感器马上反映新连接，不用等 5 分钟后台扫描
            try:
                await manager.async_validate(entry_id, vm_key, force=True)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("保存连接设置后的校验失败（不影响保存）", exc_info=True)
            return web.json_response(_conn_view(manager, entry_id, vm_key, vm_data))

        if op == "test_conn":
            result = await guest_router.async_test_conn(
                manager, entry_id, vm_key, vm_data, data.get("ssh")
            )
            return web.json_response(result)

        if op == "forget_hostkey":
            await manager.async_clear_ssh_hostkey(entry_id, vm_key)
            return web.json_response({"ok": True})

        if op == "remove_conn":
            # 两条通道一起清掉（只删一条会导致"剩下的那条谁也说不清是什么"）。
            await manager.async_remove_cred(entry_id, vm_key)
            return web.json_response({"ok": True})
    except (ValueError, GuestOpsError) as error:
        return web.json_response({"error": str(error)}, status=400)
    return web.json_response({"error": f"未知操作：{op}"}, status=400)


def _guest_signed_action(hass, prefix, entry_id, vm_key, query):
    """给上传/下载的完整路径（含 query）签 authSig。"""
    path = f"{prefix}/{quote(entry_id)}/{quote(vm_key)}?{query}"
    return _sign(hass, path)


def _guest_basename(guest_path):
    """从来宾路径取最后一段作为浏览器下载文件名。"""
    name = str(guest_path or "").replace("\\", "/").rstrip("/").split("/")[-1].strip()
    return _safe_filename(name) or "guest_file"


class GuestPageView(HomeAssistantView):
    """来宾管理 HTML 页面（路径带 authSig 才能访问）。"""

    requires_auth = True
    url = CONSOLE_GUEST_PAGE_PREFIX
    name = "esxi_control:guest_page"

    async def get(self, request):
        hass = request.app["hass"]
        # 总开关关着：文件系统面板（进程 / 文件 / 注册表 / 终端）整页不给
        if not remote_desktop_enabled(hass):
            return _disabled_response()
        entry_id = request.query.get("entry", "")
        vm_key = request.query.get("vm", "")
        entry_data = _entry_data(hass, entry_id)
        if not entry_data:
            return web.Response(text="该 ESXi 集成条目不存在或尚未加载完成。",
                                status=404, content_type="text/plain", charset="utf-8")
        vm_data = _vm_snapshot(entry_data, vm_key)
        if vm_data is None:
            return web.Response(text="找不到指定的虚拟机。",
                                status=404, content_type="text/plain", charset="utf-8")

        def _read_page():
            with open(_GUEST_INDEX, "r", encoding="utf-8") as handle:
                return handle.read()

        try:
            template = await hass.async_add_executor_job(_read_page)
        except FileNotFoundError:
            return web.Response(
                text="来宾管理页面文件缺失（www_guest/index.html 未部署）。",
                status=500, content_type="text/plain", charset="utf-8",
            )

        api_path = _sign(
            hass, f"{CONSOLE_GUEST_API_PREFIX}/{quote(entry_id)}/{quote(vm_key)}"
        )
        console_path = _sign(
            hass, f"{CONSOLE_PAGE_PREFIX}?entry={quote(entry_id)}&vm={quote(vm_key)}"
        )
        vm_name = str(vm_data.get("vm_name") or vm_key)
        # 页面里 <title>/<h1> 是不带引号的裸占位符，要做 HTML 转义后替换；
        #   JS 中的引号形式（"__VM_NAME__"）走 JSON 注入
        page_html = (
            template
            .replace('"__VM_NAME__"', json.dumps(vm_name))
            .replace("__VM_NAME__", html.escape(vm_name))
            .replace('"__GUEST_API__"', json.dumps(api_path))
            .replace('"__CONSOLE_PATH__"', json.dumps(console_path))
            .replace('"__UPLOAD_LIMIT__"', str(_GUEST_UPLOAD_LIMIT))
        )
        return web.Response(
            text=page_html, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


class GuestApiView(HomeAssistantView):
    """来宾操作统一 JSON 接口（自验签）：进程 / 文件 / 注册表 / 签名换发。"""

    requires_auth = False
    url = CONSOLE_GUEST_API_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:guest_api"

    async def post(self, request, entry_id, vm_key):
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        try:
            raw = await request.content.read(2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "请求不是合法的 JSON"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "请求体必须是 JSON 对象"}, status=400)

        op = str(data.get("op") or "")

        # 签名换发不连 ESXi、不查凭据：页面路径已按 VM 签好，这里只是在同一 VM
        #   范围内给带动态 query 的上传/下载 URL 补签
        if op in ("sign_upload", "sign_download"):
            guest_path = str(data.get("guest_path") or "").strip()
            if not guest_path:
                return web.json_response({"error": "guest_path 不能为空"}, status=400)
            query = f"guest_path={quote(guest_path, safe='')}"
            prefix = CONSOLE_GUEST_DOWNLOAD_PREFIX
            if op == "sign_upload":
                overwrite = "0" if data.get("overwrite") is False else "1"
                query += f"&overwrite={overwrite}"
                prefix = CONSOLE_GUEST_UPLOAD_PREFIX
            return web.json_response(
                {"url": _guest_signed_action(hass, prefix, entry_id, vm_key, query)}
            )

        # 连接设置（通道 / SSH 参数 / 主机指纹）：不碰 ESXi，也不需要凭据先配好
        if op in _CONN_OPS:
            return await _handle_conn_op(hass, entry_id, vm_key, op, data)

        # 校验：一次"探测"，成功失败都要拿到结论（含"还没配置"）。走专用路径，
        #   不经过 _resolve_guest_target —— 那样没配凭据会直接 409，页面只能猜。
        if op == "validate":
            return web.json_response(
                await guest_router.async_validate_target(
                    hass, get_manager(hass), entry_id, vm_key
                )
            )

        error, target = _resolve_guest_target(hass, entry_id, vm_key)
        if error is not None:
            return error

        # 具体走哪条通道（VMware Tools / SSH）由 guest_router 决定，这里只管分发
        try:
            result = await guest_router.async_run(
                hass, get_manager(hass), target, op, data
            )
        except GuestOpsError as error:
            return web.json_response({"error": str(error)}, status=400)
        except (TypeError, ValueError) as error:
            return web.json_response({"error": f"参数不合法：{error}"}, status=400)
        except Exception as error:  # pylint: disable=broad-except
            _LOGGER.exception("来宾操作失败 op=%s", op)
            return web.json_response({"error": str(error)}, status=502)
        return web.json_response(result)


class GuestUploadView(HomeAssistantView):
    """浏览器直传字节流 → HA 临时文件 → 来宾目标路径（自验签）。"""

    requires_auth = False
    url = CONSOLE_GUEST_UPLOAD_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:guest_upload"

    async def post(self, request, entry_id, vm_key):
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        error, target = _resolve_guest_target(hass, entry_id, vm_key)
        if error is not None:
            return error
        guest_path = request.query.get("guest_path", "")
        overwrite = request.query.get("overwrite", "1") != "0"
        if not guest_path:
            return web.json_response({"error": "guest_path 不能为空"}, status=400)

        body = await request.read()
        if not body:
            return web.json_response({"error": "空文件"}, status=400)
        if len(body) > _GUEST_UPLOAD_LIMIT:
            return web.json_response(
                {"error": f"文件超过 {_GUEST_UPLOAD_LIMIT // (1024 * 1024)}MB 上限，"
                          "请先压缩或在客户机内通过其他方式传输"},
                status=413,
            )

        fd, tmp_path = tempfile.mkstemp(prefix="esxi_guest_up_")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            try:
                await guest_router.async_upload(
                    hass, get_manager(hass), target, tmp_path, guest_path, overwrite
                )
            except GuestOpsError as error:
                return web.json_response({"error": str(error)}, status=400)
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.exception("来宾文件上传失败")
                return web.json_response({"error": str(error)}, status=502)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return web.json_response(
            {"ok": True, "guest_path": guest_path, "size": len(body)}
        )


def _guest_cleanup_old_downloads():
    """删掉超过 TTL 的来宾下载临时目录（崩溃/关页后的残留懒清理）。"""
    tmp_root = tempfile.gettempdir()
    now = time.time()
    for name in os.listdir(tmp_root):
        if not name.startswith("esxi_guest_dl_"):
            continue
        full = os.path.join(tmp_root, name)
        try:
            if os.path.isdir(full) and now - os.path.getmtime(full) > _GUEST_DL_TTL:
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass


class GuestDownloadView(HomeAssistantView):
    """来宾文件 → HA 临时文件 → 浏览器下载（GET 自验签，便于 <a>/新标签直下）。"""

    requires_auth = False
    url = CONSOLE_GUEST_DOWNLOAD_PREFIX + "/{entry_id}/{vm_key}"
    name = "esxi_control:guest_download"

    async def get(self, request, entry_id, vm_key):
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        error, target = _resolve_guest_target(hass, entry_id, vm_key)
        if error is not None:
            return error
        guest_path = request.query.get("guest_path", "")
        if not guest_path:
            return web.json_response({"error": "guest_path 不能为空"}, status=400)

        await hass.async_add_executor_job(_guest_cleanup_old_downloads)
        tmp_dir = await hass.async_add_executor_job(
            lambda: tempfile.mkdtemp(prefix="esxi_guest_dl_")
        )
        local_path = os.path.join(tmp_dir, _guest_basename(guest_path))
        try:
            info = await guest_router.async_download(
                hass, get_manager(hass), target, guest_path, local_path
            )
        except GuestOpsError as error:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return web.json_response({"error": str(error)}, status=400)
        except Exception as error:  # pylint: disable=broad-except
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _LOGGER.exception("来宾文件下载失败")
            return web.json_response({"error": str(error)}, status=502)

        download_name = _guest_basename(guest_path)
        response = web.FileResponse(
            local_path,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(download_name)
                ),
                "Cache-Control": "no-store",
            },
        )
        # FileResponse 由框架在 handler 任务内完成 sendfile；任务结束后再清理
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.add_done_callback(
                lambda _task: shutil.rmtree(tmp_dir, ignore_errors=True)
            )
        response.headers["X-Guest-File-Size"] = str(info.get("size", ""))
        return response


# --------------------------------------------------------------------------- #
# 主机级「虚拟机管理」悬浮窗
#   页面 GET（authSig）+ 清单 GET（authSig，查询参数在签名路径里固定）
#   其余全部 POST 到不带查询参数的签名路径，由本视图自行验签（HA 2026 的中间件
#   只接受 GET/HEAD 的 authSig）
# --------------------------------------------------------------------------- #
def _admin_resolve(hass, entry_id, host_key):
    """返回 (conn_details, host_key, 错误响应)。"""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None, host_key, web.json_response(
            {"error": "集成条目不存在或已卸载"}, status=404)
    entry_data = _entry_data(hass, entry_id)
    if not entry_data:
        return None, host_key, web.json_response(
            {"error": "集成条目尚未加载完成"}, status=409)
    # vCenter 多主机：host_key 必须是该条目快照里真实存在的主机
    hosts = entry_data.get("vmhost") or {}
    if host_key not in hosts:
        if len(hosts) == 1:
            host_key = next(iter(hosts))
        elif not host_key or host_key == "-":
            return None, host_key, web.json_response(
                {"error": "该条目下有多台主机，链接缺少主机标识，请重新进入"},
                status=400)
        else:
            return None, host_key, web.json_response(
                {"error": f"主机 {host_key} 不属于该集成条目"}, status=404)
    config = entry.data
    return {
        "host": config["host"],
        "user": config["username"],
        "pwd": config["password"],
        "port": config["port"],
        "ssl": config["verify_ssl"],
    }, host_key, None


def _admin_jobs_store(hass):
    """管理批量任务状态（内存）：{entry_id: {job_id: job}}。"""
    store = hass.data.setdefault(_DATA_KEY, {})
    jobs = store.setdefault("admin_jobs", {})
    return jobs


def _admin_get_job(hass, entry_id, job_id):
    return (_admin_jobs_store(hass).get(entry_id) or {}).get(job_id)


async def _admin_run_batch(hass, entry_id, job_id, conn_details, host_key,
                           payload):
    """串行执行一批创建任务；逐项 try/continue，中途可在项间取消。"""
    jobs = _admin_jobs_store(hass)
    job = jobs[entry_id][job_id]
    names = list(payload.get("names") or [])
    manual_vm = payload.get("vm")
    profile = payload.get("profile")
    for name in names:
        if job.get("cancel"):
            break
        try:
            if profile:
                params = dict(profile)
                params["name"] = name
                await hass.async_add_executor_job(
                    vm_admin_ops.create_from_profile,
                    conn_details, params, host_key)
            else:
                params = dict(manual_vm or {})
                params["name"] = name
                await hass.async_add_executor_job(
                    vm_admin_ops.create_vm, conn_details, params, host_key)
            job["items"].append({"name": name, "state": "ok"})
        except Exception as error:  # pylint: disable=broad-except
            message = str(error)
            job["items"].append({"name": name, "state": "failed",
                                 "error": message})
            _LOGGER.warning("批量创建 %s 失败：%s", name, message)
        job["done"] += 1
    job["state"] = "canceled" if job.get("cancel") else "done"
    await refresh_data_now(hass, entry_id)


class AdminPageView(HomeAssistantView):
    """虚拟机管理页面（主机设备「访问」入口，签名 GET）。"""

    requires_auth = True
    url = ADMIN_PAGE_PREFIX
    name = "esxi_control:admin_page"

    async def get(self, request):
        hass = request.app["hass"]
        # 「虚拟机管理」地址同样受「远程桌面」总开关管辖：关掉后既不给页面，
        #   设备页上的链接也已被清掉（见 async_set_remote_desktop）
        if not remote_desktop_enabled(hass):
            return _disabled_response()
        entry_id = request.query.get("entry", "")
        host_key = request.query.get("host", "-")
        if not _entry_data(hass, entry_id):
            return web.Response(
                text="该 ESXi 集成条目不存在或尚未加载完成。",
                status=404, content_type="text/plain", charset="utf-8")
        await _remember_origin(hass, entry_id, request)

        api_base = f"{ADMIN_API_PREFIX}/{quote(entry_id)}/{quote(host_key)}"
        # 清单接口路径固定（含 /inventory），可以随页面一起签好；
        # 所有 POST（含任务状态轮询）走不带查询参数的裸路径
        inventory_path = _sign(hass, api_base + "/inventory")
        api_path = _sign(hass, api_base)
        host_label = host_key if host_key != "-" else "ESXi 主机"

        try:
            template = await hass.async_add_executor_job(_admin_page_template)
        except FileNotFoundError:
            return web.Response(
                text="管理页面文件缺失：www_admin/index.html 未部署",
                status=500, content_type="text/plain", charset="utf-8")
        html_text = (
            template
            .replace('"__ADMIN_API_PATH__"', json.dumps(api_path))
            .replace('"__ADMIN_INVENTORY_PATH__"', json.dumps(inventory_path))
            .replace("__HOST_NAME__", html.escape(host_label))
        )
        return web.Response(
            text=html_text, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


class AdminInventoryView(HomeAssistantView):
    """一次性拉取主机/存储/端口组/VM 硬件清单（签名 GET，路径固定）。"""

    requires_auth = True
    url = ADMIN_API_PREFIX + r"/{entry_id:[^/]+}/{host_key:[^/]+}/inventory"
    name = "esxi_control:admin_inventory"

    async def get(self, request, entry_id, host_key):
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        conn_details, host_key, error = _admin_resolve(
            hass, entry_id, host_key)
        if error is not None:
            return error
        try:
            data = await hass.async_add_executor_job(
                vm_admin_ops.get_inventory, conn_details, host_key)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.exception("虚拟机清单获取失败")
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response(data, headers={"Cache-Control": "no-store"})


class AdminApiView(HomeAssistantView):
    """虚拟机管理操作：创建/批量创建、改配、模板开关、删除、任务轮询。"""

    requires_auth = False
    url = ADMIN_API_PREFIX + r"/{entry_id:[^/]+}/{host_key:[^/]+}"
    name = "esxi_control:admin_api"

    async def post(self, request, entry_id, host_key):
        hass = request.app["hass"]
        blocked = _blocked_response(request, json_body=True)
        if blocked is not None:
            return blocked
        if not await _verify_signed_request(hass, request):
            return _unauthorized()
        conn_details, host_key, error = _admin_resolve(
            hass, entry_id, host_key)
        if error is not None:
            return error
        try:
            payload = await request.json()
        except Exception:  # pylint: disable=broad-except
            return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        op = payload.get("op")

        if op == "job_status":
            job = _admin_get_job(hass, entry_id, payload.get("job_id"))
            if job is None:
                return web.json_response({"found": False})
            return web.json_response({"found": True, **job})

        if op == "cancel_job":
            job = _admin_get_job(hass, entry_id, payload.get("job_id"))
            if job is not None and job["state"] == "running":
                job["cancel"] = True
            return web.json_response({"ok": True})

        if op == "create_batch":
            return await self._create_batch(
                hass, request, entry_id, conn_details, host_key, payload)

        # 以下为单机即时操作
        moid = str(payload.get("moid") or "")
        if not moid:
            return web.json_response({"error": "缺少虚拟机标识 moid"}, status=400)
        try:
            if op == "reconfigure":
                result = await hass.async_add_executor_job(
                    vm_admin_ops.reconfigure_vm, conn_details, moid,
                    payload.get("changes") or {}, host_key)
            elif op == "set_template":
                result = await hass.async_add_executor_job(
                    vm_admin_ops.set_template, conn_details, moid,
                    bool(payload.get("enabled")), host_key)
            elif op == "set_disk_mode":
                result = await hass.async_add_executor_job(
                    vm_admin_ops.set_disk_mode, conn_details, moid,
                    payload.get("disk_key"), payload.get("mode"), host_key)
            elif op == "delete":
                result = await hass.async_add_executor_job(
                    vm_admin_ops.delete_vm, conn_details, moid, host_key)
            else:
                return web.json_response(
                    {"error": f"未知操作 {op}"}, status=400)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.warning("虚拟机管理操作 %s 失败：%s", op, exc)
            return web.json_response({"error": str(exc)}, status=400)
        await refresh_data_now(hass, entry_id)
        return web.json_response({"ok": True, "result": result})

    async def _create_batch(self, hass, request, entry_id, conn_details,
                            host_key, payload):
        names = [str(n).strip() for n in payload.get("names") or []]
        names = [n for n in names if n]
        if not names:
            return web.json_response({"error": "没有要创建的虚拟机名称"},
                                     status=400)
        if len(names) > 99:
            return web.json_response({"error": "单次批量创建不能超过 99 台"},
                                     status=400)
        if not payload.get("vm") and not payload.get("profile"):
            return web.json_response(
                {"error": "缺少虚拟机配置（手工参数或样板配置）"}, status=400)
        entry_jobs = _admin_jobs_store(hass).setdefault(entry_id, {})
        if any(job.get("state") == "running" for job in entry_jobs.values()):
            return web.json_response(
                {"error": "该条目已有一个批量创建任务在进行，请等待完成或取消"},
                status=409)
        # 重名预检（不占用任务名额；竞态下 ESXi 自身仍会拒绝）
        entry_data = _entry_data(hass, entry_id)
        existing = {
            (v.get("vm_name") or k)
            for k, v in (entry_data.get("vm") or {}).items()
        }
        duplicated = [n for n in names if n in existing]
        if duplicated:
            return web.json_response(
                {"error": "以下名称已存在：" + "、".join(duplicated)}, status=409)

        job_id = uuid_lib.uuid4().hex
        entry_jobs[job_id] = {
            "state": "running", "total": len(names), "done": 0,
            "items": [], "cancel": False,
        }
        hass.async_create_task(
            _admin_run_batch(
                hass, entry_id, job_id, conn_details, host_key,
                {"names": names, "vm": payload.get("vm"),
                 "profile": payload.get("profile")}))
        return web.json_response({"ok": True, "job_id": job_id})


# --------------------------------------------------------------------------- #
# 注册（view 全局只注册一次；每加载一个条目补一次设备链接）
# --------------------------------------------------------------------------- #
async def async_setup_console(hass, config_entry):
    """Register console views (once globally) and refresh this entry's links."""
    store = hass.data.setdefault(_DATA_KEY, {})

    if not store.get("views_registered"):
        hass.http.register_view(ConsolePageView)
        hass.http.register_view(ConsoleStaticView)
        hass.http.register_view(ConsoleTicketView)
        hass.http.register_view(ConsoleOriginView)
        hass.http.register_view(ConsoleIsoView)
        hass.http.register_view(ConsoleWebSocketView)
        hass.http.register_view(GuestPageView)
        hass.http.register_view(GuestApiView)
        hass.http.register_view(GuestUploadView)
        hass.http.register_view(GuestDownloadView)
        hass.http.register_view(AdminPageView)
        hass.http.register_view(AdminInventoryView)
        hass.http.register_view(AdminApiView)
        store["views_registered"] = True

        # 向 HA 前端注入「控制台悬浮窗」模块：设备页点「访问」时不再整页跳转，
        #   而是在 HA 界面内弹出可拖动/缩放的 iframe 悬浮窗。
        #   官方 API（homeassistant.components.frontend.add_extra_js_url），
        #   模块通过上面的 ConsoleStaticView 白名单提供；HA 停止时注销。
        embed_url = f"{CONSOLE_STATIC_PREFIX}/frontend/console-embed.js"
        try:
            from homeassistant.components import frontend as _frontend

            _frontend.add_extra_js_url(hass, embed_url)
            store["embed_url"] = embed_url
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("注册控制台悬浮窗前端模块失败（不影响独立页面访问）", exc_info=True)

        # 任意条目同步完成（新增 / 删除 VM）→ 重签链接
        async def _on_synced(event):
            await _link_one_entry(hass, event.data.get("entry_id", ""))

        hass.bus.async_listen(EVENT_ENTRY_SYNCED, _on_synced)

        # 兜底：每 12 小时全量重签一次，保证 24 小时有效期的链接不会断档
        async def _on_interval(_now):
            await _link_all_entries(hass)

        unsub = async_track_time_interval(hass, _on_interval, _RESIGN_INTERVAL)

        async def _on_stop(_event):
            unsub()
            if store.get("embed_url"):
                try:
                    from homeassistant.components import frontend as _frontend

                    _frontend.remove_extra_js_url(hass, store["embed_url"])
                except Exception:  # pylint: disable=broad-except
                    pass

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
        # 预热页面模板缓存（在 executor 读盘 + 内联 vendor JS，避免首个请求卡顿）。
        #   文件缺失（部署漏了 www_console）不能拖垮整个集成 setup：首个页面请求
        #   会再次尝试并把明确的错误回显到页面上
        try:
            await hass.async_add_executor_job(_page_template)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "控制台页面模板预热失败（请检查 www_console/ 及 vendor 文件是否完整部署）"
            )
        _LOGGER.debug("esxi_control 控制台视图已注册")

    # 确保历史学到的访问地址已加载（setup 早期 __init__ 里可能已加载过，幂等）
    await async_load_learned_bases(hass)
    # 光盘上传挂载记录（幂等：内部只在 store 未加载时读盘）
    await async_load_cdrom_mounts(hass)
    # 全局「远程桌面」总开关（幂等；__init__ 在平台加载前已经读过一次，
    #   这里兜底：视图注册与它同源，缺了会按"开启"处理而放行）
    await async_load_remote_state(hass)

    # 幂等启动重签轮询（setup 早期可能已经启动过一次，这里不会重复）
    schedule_initial_link(hass, config_entry.entry_id)
