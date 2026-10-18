"""JVM 截图变更中枢.

把「截图写入」与「图片实体」解耦：

* ``vm_screenshot_paths`` 统一一处算路径，避免 switch 写到 A 处、image 读到 B 处。
* ``ScreenshotHub`` 在新帧落盘后扇出通知。这里的通知是「变化已发生」的信号，
  具体的最新版本由实体自己去 stat 文件拿（mtime 是真值来源），
  这样即使某个实体漏收通知，下一次自身轮询也会追回来。

为什么不直接用 watchdog/inotify：
  截图目录通常是容器挂载或网络盘，inotify 在这类场景（尤其 Docker on Windows/Mac）
  事件不一定下发；而写图的人本来就是我们自己，写入成功即知晓，成本最低。
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from .const import DOMAIN_DATA

_LOGGER = logging.getLogger(__name__)

HUB_KEY = "screenshot_hub"
WWW_SUBDIR = "esxiimg"


def vm_screenshot_paths(host: str, vm_name: str, screenshot_dir: str):
    """返回 (保存绝对路径, 对外访问的 /local URL).

    switch 与 image 必须共用这一个函数。
    """
    safe_name = str(vm_name).replace(" ", "_").replace("/", "_")
    save_path = os.path.join(screenshot_dir, str(host), f"{safe_name}.png")
    return save_path, f"/local/{WWW_SUBDIR}/{host}/{safe_name}.png"


def file_version(path: str):
    """返回文件版本号（纳秒级 mtime），文件不可读时返回 None."""
    try:
        return int(os.stat(path).st_mtime_ns)
    except OSError:
        return None


class ScreenshotHub:
    """新帧写入后通知订阅者."""

    def __init__(self, hass, entry_id):
        """Initialize the hub."""
        self._hass = hass
        self._entry_id = entry_id
        self._subscribers: dict[str, list[Callable[[], None]]] = {}

    @property
    def entry_id(self):
        """Return the config entry id."""
        return self._entry_id

    def subscribe(self, vm_key: str, callback: Callable[[], None]):
        """订阅某台 VM 的画面变化，返回取消订阅函数."""
        self._subscribers.setdefault(vm_key, []).append(callback)

        def _unsubscribe():
            self.unsubscribe(vm_key, callback)

        return _unsubscribe

    def unsubscribe(self, vm_key: str, callback: Callable[[], None]):
        """取消订阅."""
        callbacks = self._subscribers.get(vm_key)
        if not callbacks or callback not in callbacks:
            return
        callbacks.remove(callback)
        if not callbacks:
            self._subscribers.pop(vm_key, None)

    async def async_notify_frame(self, vm_key: str, save_path: str):
        """截图原子写成功后调用."""
        # 先在 executor 里过一次 stat：既确认文件真的落地，也留一条 debug 记录
        version = await self._hass.async_add_executor_job(file_version, save_path)
        if version is None:
            _LOGGER.debug("VM %s 截图文件不可读，跳过通知: %s", vm_key, save_path)
            return

        _LOGGER.debug("VM %s 画面已更新 (version=%s)", vm_key, version)
        for callback in list(self._subscribers.get(vm_key, ())):
            try:
                callback()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("通知 VM %s 画面变化时出错", vm_key)


def get_hub(hass, entry_id):
    """取（或惰性创建）某个 entry 的 hub."""
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if entry_data is None:
        return None
    hub = entry_data.get(HUB_KEY)
    if hub is None:
        hub = ScreenshotHub(hass, entry_id)
        entry_data[HUB_KEY] = hub
    return hub
