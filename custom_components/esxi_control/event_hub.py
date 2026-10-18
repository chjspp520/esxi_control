"""ESXi 事件流中枢：定时轮询 vSphere EventManager 并向 HA 投递新事件。

三条消费通道（同一份事件数据）
--------------------------------
1. 内存滚动列表 + 订阅回调：``sensor.esxi_事件`` 实体据此实时写状态
   （状态=最新一条，属性=最近 EVENT_MAX_KEEP 条）；
2. HA 事件总线：每条新事件派发 ``esxi_control_event``，可直接做自动化 / 通知；
3. HA 日志簿（Logbook）：每条新事件写一条记录，在「日志簿」页面像系统日志一样看。

轮询语义
--------
* 每 ``EVENT_POLL_INTERVAL`` 秒建一条短连接，用 ByTime 过滤器读增量；
  收集器随连接销毁，不长期占用服务端 collector 配额（maxCollector=1000）。
* 首次启动只**回填**最近 EVENT_BACKLOG_SECONDS 分钟的事件到传感器，
  不刷总线 / 日志簿（避免每次重启 HA 都把历史事件重放成通知）。
* 去重靠服务端全局递增的 event key（环形保留最近 2000 个），
  查询窗口再带 15 秒重叠，防止边界丢事件。
"""
import logging
from collections import deque
from datetime import timedelta, timezone

from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    DOMAIN_DATA,
    EVENT_BACKLOG_SECONDS,
    EVENT_ESXI_EVENT,
    EVENT_HUB_KEY,
    EVENT_MAX_KEEP,
    EVENT_MAX_NEW_PER_POLL,
    EVENT_POLL_INTERVAL,
)
from .esxi import esx_connect, esx_disconnect, fetch_events

_LOGGER = logging.getLogger(__name__)

# 日志簿 / 界面上显示的中文分类
CATEGORY_CN = {
    "info": "信息",
    "warning": "警告",
    "error": "错误",
    "user": "用户操作",
}

# 日志簿内部 API 在各 HA 版本签名稳定（内部就是 fire 一个 logbook 事件），
#   但仍做防御性导入：拿不到就只走总线 + 传感器，不影响主功能。
try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.components.logbook import async_log_entry
except Exception:  # pylint: disable=broad-except  # pragma: no cover
    async_log_entry = None

# 集成自己每 15 秒（事件轮询）/45 秒（全量刷新）都会登录一次 ESXi，
#   若不过滤，事件流和日志簿会被「pyvmomi 登录/登出」彻底刷屏。
#   只吞掉 userAgent 表明来自 pyvmomi 的会话事件 —— 用户从网页/SSH 的真实
#   登录事件（浏览器 UA）照常保留。
_SELF_NOISE_TYPES = {"UserLoginSessionEvent", "UserLogoutSessionEvent"}


def _is_self_noise(event):
    """是否是本集成（pyvmomi）自己产生的登录/登出事件。"""
    return (
        event.get("type") in _SELF_NOISE_TYPES
        and "pyvmomi" in (event.get("user_agent") or "").lower()
    )


class ESXiEventHub:
    """每个配置条目一个：维护增量游标、滚动缓冲与订阅者。"""

    def __init__(self, hass, config_entry):
        """Initialize the hub."""
        self.hass = hass
        self._entry = config_entry
        self._entry_id = config_entry.entry_id
        self._unsub_interval = None
        # 最新事件排在最前
        self._events = deque(maxlen=EVENT_MAX_KEEP)
        # 最近见过的 event key（环形），跨 hostd 重启导致 key 回绕也不会误判
        self._seen = deque(maxlen=2000)
        # 增量游标（aware UTC）与是否已完成首次回填
        self._last_time = None
        self._primed = False
        self._subscribers = set()
        # 启动以来收到的**新**事件计数（回填不计）
        self._new_count = 0
        # 连续轮询失败次数（日志降级用）
        self._failures = 0

    # ------------------------------ 生命周期 ------------------------------ #
    async def start(self):
        """启动定时轮询，并立刻在后台做第一次（回填）查询。"""
        self._unsub_interval = async_track_time_interval(
            self.hass, self._poll, timedelta(seconds=EVENT_POLL_INTERVAL)
        )
        self.hass.async_create_task(self._poll(None))

    def stop(self):
        """卸载集成时停掉定时器。"""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    # ------------------------------ 订阅接口 ------------------------------ #
    def subscribe(self, callback):
        """注册传感器刷新回调。"""
        self._subscribers.add(callback)

    def unsubscribe(self, callback):
        """注销传感器刷新回调。"""
        self._subscribers.discard(callback)

    @property
    def events(self):
        """最近事件（最新在前），返回浅拷贝列表。"""
        return list(self._events)

    @property
    def new_count(self):
        """启动以来的新事件数。"""
        return self._new_count

    @property
    def primed(self):
        """首轮回填是否完成。"""
        return self._primed

    def _notify(self):
        for callback in list(self._subscribers):
            try:
                callback()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("ESXi 事件订阅者回调出错")

    # ------------------------------ 数据查询 ------------------------------ #
    def _query(self, begin_time):
        """在线程里跑：建短连接读事件，返回列表；连接失败返回 None。"""
        data = self._entry.data
        conn = esx_connect(
            data["host"],
            data["username"],
            data["password"],
            data["port"],
            data["verify_ssl"],
        )
        if conn is None:
            return None
        try:
            return fetch_events(conn.RetrieveContent(), begin_time)
        finally:
            esx_disconnect(conn)

    @staticmethod
    def _parse_utc(value):
        """ISO 字符串 → aware UTC datetime；解析失败返回 None。"""
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _poll(self, _now):
        """一轮增量查询与分发（async_track_time_interval 回调）。"""
        now = dt_util.utcnow()
        if not self._primed:
            begin = now - timedelta(seconds=EVENT_BACKLOG_SECONDS)
        else:
            # 重叠 15 秒，配合 key 去重，吃掉时钟抖动 / 网络延迟造成的边界丢失
            begin = self._last_time - timedelta(seconds=15)

        try:
            events = await self.hass.async_add_executor_job(self._query, begin)
        except Exception:  # pylint: disable=broad-except
            self._failures += 1
            _LOGGER.debug("ESXi 事件轮询异常（连续第 %s 次）", self._failures, exc_info=True)
            return

        if events is None:
            self._failures += 1
            if self._failures == 1:
                _LOGGER.warning(
                    "ESXi 事件轮询连不上主机（事件实体会暂时停在旧值）：%s",
                    self._entry.data.get("host"),
                )
            return

        if self._failures:
            _LOGGER.info("ESXi 事件轮询已恢复（此前连续失败 %s 次）", self._failures)
            self._failures = 0

        # key 去重（回填轮和增量轮都要）；本集成自身的 pyvmomi 登录噪音直接吞掉
        #   （key 仍记入 seen，避免下轮窗口重叠时再被捞出来）
        fresh = [
            e
            for e in events
            if e["key"] not in self._seen and not _is_self_noise(e)
        ]
        for event in events:
            self._seen.append(event["key"])
        # 推进时间游标到这批里最新一条
        for event in events:
            parsed = self._parse_utc(event["time_utc"])
            if parsed is not None and (self._last_time is None or parsed > self._last_time):
                self._last_time = parsed

        if not fresh:
            self._primed = True
            return

        if not self._primed:
            # 首轮：只回填传感器，不打扰总线 / 日志簿。
            # fresh 按时间升序，逐条 appendleft 后队首即最新；
            #   deque maxlen 淘汰的也正是最旧的那些。
            for event in fresh:
                self._events.appendleft(event)
            self._primed = True
            self._notify()
            _LOGGER.debug("ESXi 事件回填 %s 条", len(fresh))
            return

        # 增量轮：洪峰保护，只处理最新的 EVENT_MAX_NEW_PER_POLL 条
        if len(fresh) > EVENT_MAX_NEW_PER_POLL:
            _LOGGER.warning(
                "单轮 ESXi 事件达 %s 条（超过 %s），较早的 %s 条只计数不分发",
                len(fresh),
                EVENT_MAX_NEW_PER_POLL,
                len(fresh) - EVENT_MAX_NEW_PER_POLL,
            )
            fresh = fresh[-EVENT_MAX_NEW_PER_POLL:]

        for event in fresh:
            self._events.appendleft(event)
            self._new_count += 1
            self._dispatch(event)

        self._notify()

    def _dispatch(self, event):
        """把一条新事件投递到 HA 总线与日志簿。"""
        host = self._entry.data.get("host", "")
        payload = {
            "entry_id": self._entry_id,
            "host": host,
            **event,
        }
        self.hass.bus.async_fire(EVENT_ESXI_EVENT, payload)

        if async_log_entry is None:
            return
        category = CATEGORY_CN.get(event["category"], event["category"])
        prefix = f"{event['object']} · " if event["object"] else ""
        message = f"[{category}] {prefix}{event['message']}"[:500]
        name = f"ESXi {self._entry.title or host}"
        try:
            async_log_entry(self.hass, name, message, domain=DOMAIN)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("写入日志簿失败", exc_info=True)


async def async_setup_events(hass, config_entry):
    """为一个配置条目创建并启动事件中枢。"""
    entry_data = hass.data[DOMAIN_DATA][config_entry.entry_id]
    hub = ESXiEventHub(hass, config_entry)
    entry_data[EVENT_HUB_KEY] = hub
    await hub.start()
    return hub


def get_event_hub(hass, entry_id):
    """取条目的事件中枢（未初始化时返回 None）。"""
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id) or {}
    return entry_data.get(EVENT_HUB_KEY)
