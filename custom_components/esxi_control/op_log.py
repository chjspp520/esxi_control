"""批量操作日志中枢。

只记录**批量**操作（全部开机 / 全部关闭 / 全部快照 / 全部重启）：
单台操作在 HA 自己的日志里已经看得到，没必要重复记。

数据放在 ``hass.data[DOMAIN_DATA][entry_id]["operation_log"]``，**重启即丢失** ——
刻意不持久化，它记录的是"过程"而不是"状态"。

条数有上限：内存里留最近 ``MAX_ENTRIES`` 条，暴露给实体 attributes 的只取最近
``EXPOSE_ENTRIES`` 条 —— sensor 的 attributes 有约 16KB 硬上限，日志多了反而会被
recorder 整条丢掉。
"""
import logging
from collections import deque

from homeassistant.util import dt as dt_util

from .const import DOMAIN_DATA

_LOGGER = logging.getLogger(__name__)

LOG_KEY = "operation_log"
#: 内存里保留的日志条数
MAX_ENTRIES = 120
#: 暴露到实体 attributes 的条数（attributes 有大小上限）
EXPOSE_ENTRIES = 60
#: 空闲时实体显示的状态
STATE_IDLE = "空闲"


class OperationLog:
    """批量操作的滚动日志 + 当前进度。"""

    def __init__(self):
        """Initialize an empty log."""
        self._entries = deque(maxlen=MAX_ENTRIES)
        self._listeners = set()

        self.running = False
        self.operation = ""
        self.operation_key = ""
        self.total = 0
        self.done = 0
        self.started_at = None
        #: 最近一次操作的结论（完成 / 已下发 / 跳过 / 已取消）
        self.last_status = ""

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, callback):
        """订阅变化（日志传感器用它及时刷新状态）。"""
        self._listeners.add(callback)

    def unsubscribe(self, callback):
        """取消订阅。"""
        self._listeners.discard(callback)

    def _notify(self):
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("通知操作日志订阅者时出错")

    # ------------------------------------------------------------------ 写入

    def _append(self, action, target, status, message=""):
        entry = {
            "time": dt_util.now().strftime("%H:%M:%S"),
            "action": action,
            "target": target,
            "status": status,
        }
        if message:
            entry["message"] = message
        self._entries.append(entry)

    def begin(self, operation, operation_key, total):
        """开始一次批量操作。"""
        self.running = True
        self.operation = operation
        self.operation_key = operation_key
        self.total = total
        self.done = 0
        self.started_at = dt_util.now().strftime("%H:%M:%S")
        self._append(operation, "—", "开始", f"共 {total} 台")
        self._notify()

    def add(self, status, target, message=""):
        """追加一条进度记录（沿用当前操作名）。"""
        self._append(self.operation or "批量操作", target, status, message)
        self._notify()

    def advance(self, count=1):
        """推进进度计数。"""
        self.done = min(self.done + count, self.total) if self.total else self.done + count
        self._notify()

    def end(self, status="完成", message=""):
        """结束一次批量操作。"""
        self.running = False
        self.last_status = status
        self._append(self.operation or "批量操作", "—", status, message)
        self._notify()

    # ------------------------------------------------------------------ 读取

    @property
    def state(self):
        """实体状态：当前/最近一次批量操作名；从未执行过才是「空闲」。

        ⚠️ 为什么不是「只在运行中才显示操作名」：下发本身是瞬时的
        （无分组间隔时一批命令几毫秒就发完了），HA 的状态机根本捕捉不到那个中间态，
        结果用户永远只能看到「空闲」。所以这里改成只要执行过就显示操作名，
        是否还在跑由 ``attributes["running"]`` 精确表达。
        """
        return self.operation or STATE_IDLE

    def attributes(self):
        """实体属性：进度 + 滚动日志。"""
        attrs = {
            "running": self.running,
            "log": list(self._entries)[-EXPOSE_ENTRIES:],
            "total_entries": len(self._entries),
        }
        if self.total:
            # 进度无条件给出：跑完之后也能看到「上次完成了 6/6」
            attrs["progress"] = f"{self.done}/{self.total}"
        if self.operation:
            attrs["operation"] = self.operation
            attrs["operation_key"] = self.operation_key
        if self.running:
            attrs["started_at"] = self.started_at
        if self.last_status:
            attrs["last_status"] = self.last_status
        return attrs


def get_log(hass, entry_id):
    """取（或惰性创建）某个 entry 的操作日志中枢；entry 未加载时返回 None。"""
    entry_data = (hass.data.get(DOMAIN_DATA, {}) or {}).get(entry_id)
    if entry_data is None:
        return None
    log = entry_data.get(LOG_KEY)
    if log is None:
        log = OperationLog()
        entry_data[LOG_KEY] = log
    return log



