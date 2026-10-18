"""乐观状态 + 待确认窗口。

给「提交动作后状态不会立刻生效」的实体用（典型是虚拟机电源开关）：

* ESXi 的开关机是**异步任务**，从 poweredOff 到 poweredOn 要几秒到几十秒；
* 集成读的数据还有 **45 秒节流**，期间拿到的都是提交前的旧快照。

不处理的话，用户点「开机」会看到开关立刻弹回「关」，等数据刷新后又变回「开」——
就是状态抖动。这里让实体**先按用户期望显示**，等真实状态追上来再收敛；
超时仍追不上就回退到真实状态并告警，**不会一直骗人**。
"""
import time


class PendingState:
    """记录一次「已提交但还没生效」的状态变更。"""

    #: 超过这个时间还没收敛就放弃乐观状态（开机、优雅关机都可能很慢）
    TIMEOUT = 90.0
    #: 等待期间主动催数据的间隔（秒）
    REFRESH_INTERVAL = 2.5

    def __init__(self):
        """Initialize an idle pending state."""
        self._target = None
        self._deadline = 0.0
        self._label = ""
        self._last_refresh = 0.0

    @property
    def active(self):
        """是否还在等待确认。"""
        return self._target is not None

    @property
    def target(self):
        """用户期望的状态（无等待时为 None）。"""
        return self._target

    def start(self, target, label=""):
        """提交动作后调用：先按用户期望显示。"""
        self._target = bool(target)
        self._deadline = time.monotonic() + self.TIMEOUT
        self._label = label or "状态切换"
        self._last_refresh = 0.0

    def clear(self):
        """放弃乐观状态（例如命令根本没发出去）。"""
        self._target = None

    def resolve(self, real):
        """返回 ``(应显示的状态, 是否该催一次刷新, 超时说明)``。

        * 真实状态已追上目标 → 收敛，返回真实值（等待结束）；
        * 仍在窗口内 → 返回期望值（不让 UI 弹回），并按间隔要求催刷新；
        * 超时 → 放弃乐观态，返回真实值并给出说明，由调用方决定怎么告警。
        """
        if self._target is None:
            return real, False, None
        if real == self._target:
            self._target = None
            return real, False, None

        now = time.monotonic()
        if now >= self._deadline:
            label = self._label
            self._target = None
            return real, False, f"「{label}」在 {int(self.TIMEOUT)} 秒内没有生效"

        need_refresh = (now - self._last_refresh) >= self.REFRESH_INTERVAL
        if need_refresh:
            self._last_refresh = now
        return self._target, need_refresh, None
