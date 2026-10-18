"""Image platform for ESXi Control.

把每台虚拟机的最新截图暴露成一个 image 实体。

两条取图通路，自动择优：
  1) entity_picture 直连 /local/esxiimg/...?t=<mtime>（需要 www 下有软链，最快）
  2) /api/image_proxy/<entity> → ImageEntity.image() 直接读文件（无需软链，兜底）

关于「图片变了就更新」：
  HA 没有推送。真正的触发链是 —— 截图写入 → hub 通知 → 实体强制刷新 →
  image_last_updated(state) 与 entity_picture 同时变化 → 前端重新拉图，感知延迟秒级。

全程不解码、不缩放、不引入 ffmpeg/stream。
"""
import logging
import os
from datetime import timedelta

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SCREENSHOT_DIR,
    DEFAULT_NAME,
    DEFAULT_SCREENSHOT_DIR,
    DOMAIN,
    DOMAIN_DATA,
)
from .entity_sync import register_platform
from .entry_options import current_options
from .vm_bulk import vm_device_key
from .screenshot_hub import (
    WWW_SUBDIR,
    file_version,
    get_hub,
    vm_screenshot_paths,
)

_LOGGER = logging.getLogger(__name__)

# 兜底轮询：只在漏收到通知的极端情况下起作用，成本是一次 os.stat
SCAN_INTERVAL = timedelta(seconds=30)


def _build_entities(hass, config_entry):
    """Return every image entity this entry should currently have.

    同步 service（entity_sync）会反复调用它，所以必须是纯函数（同步、无副作用）；
    ``local_ok`` 在 setup 时探测过一次并缓存，这里直接读缓存。
    """
    entry_id = config_entry.entry_id
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if not entry_data:
        return []
    if "vm" not in entry_data.get("monitored_conditions", []):
        return []
    local_ok = entry_data.get("screenshot_local_ok", False)
    return [
        ESXiVMScreenshotImage(hass, config_entry, vm_name, local_ok)
        for vm_name in entry_data["vm"]
    ]


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up image platform."""
    entry_id = config_entry.entry_id
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if not entry_data:
        return

    if "vm" not in entry_data.get("monitored_conditions", []):
        return

    screenshot_dir = config_entry.options.get(
        CONF_SCREENSHOT_DIR, DEFAULT_SCREENSHOT_DIR
    )

    # /local/esxiimg 是否存在直接决定能不能走「直连」通路
    local_ok = await hass.async_add_executor_job(
        os.path.exists, hass.config.path("www", WWW_SUBDIR)
    )
    entry_data["screenshot_local_ok"] = local_ok
    if not local_ok:
        _LOGGER.warning(
            "未找到 %s，image 实体将改用 %s 代理通路读取文件（也可把截图目录 %s "
            "软链接到该路径以启用直连）",
            hass.config.path("www", WWW_SUBDIR),
            "/api/image_proxy",
            screenshot_dir,
        )

    get_hub(hass, entry_id)

    entities = _build_entities(hass, config_entry)
    if entities:
        async_add_entities(entities, True)
    register_platform(hass, entry_id, "image", _build_entities, async_add_entities)


class ESXiVMScreenshotImage(ImageEntity):
    """虚拟机屏幕画面."""

    _attr_content_type = "image/png"
    _attr_icon = "mdi:monitor-screenshot"
    # friendly_name 由「设备名 + 自身短名」拼出
    _attr_has_entity_name = True
    # ImageEntity 默认 should_poll=False；这里打开轮询作为「漏通知」兜底
    _attr_should_poll = True

    def __init__(self, hass, config_entry, vm_key, local_ok=False):
        """Initialize the image entity."""
        # 必须调用，基类在这里创建 access_tokens / httpx client（否则写状态就会崩）
        # 旧版本 ImageEntity.__init__ 不带 hass 参数，这里做兼容
        try:
            super().__init__(hass)
        except TypeError:  # pragma: no cover - 兼容旧签名
            super().__init__()
        if not hasattr(self, "access_tokens"):  # pragma: no cover - 兜底
            import collections
            self.access_tokens = collections.deque(maxlen=2)
            self.access_tokens.append("0")

        self.hass = hass
        self._config_entry = config_entry
        self._vm_key = vm_key
        self._local_ok = local_ok
        self._entry_id = config_entry.entry_id
        self._attr_unique_id = (
            f"{config_entry.data['host'].replace('.', '_')}"
            f"_{self._entry_id}_vm_screen_{vm_key}"
        )

        self._vm_data = (
            hass.data.get(DOMAIN_DATA, {})
            .get(self._entry_id, {})
            .get("vm", {})
            .get(vm_key, {})
        )
        self._save_path = None
        self._url_base = None
        self._version = None
        self._last_change = None

        # entity_picture / image_url 都是 propcache 缓存属性，
        # 更新 _attr_ 后必须手动失效，否则会一直拿到旧值
        self._attr_entity_picture = None
        self._attr_image_last_updated = None
        self._attr_available = False

        self._unsub_hub = None

    @property
    def _screenshot_dir(self):
        """Return the screenshot directory（读最新 options，改了立刻生效）."""
        return current_options(self.hass, self._config_entry).get(
            CONF_SCREENSHOT_DIR, DEFAULT_SCREENSHOT_DIR
        )

    @property
    def _display_name(self):
        """Return the VM display name (falls back to the internal key)."""
        return self._vm_data.get("vm_name", self._vm_key)

    @property
    def name(self):
        """Return the short name (friendly_name = 设备名 + 短名)."""
        return "屏幕画面"

    @property
    def available(self):
        """Return True if a screenshot file is readable."""
        return self._attr_available

    @property
    def device_info(self):
        """Return device information (same device as the switches)."""
        return {
            "identifiers": {(DOMAIN, vm_device_key(self._entry_id, self._vm_key))},
            "name": f"虚拟机 {self._display_name}",
            "manufacturer": "VMware",
            "model": "虚拟机",
        }

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {
            "vm_name": self._display_name,
            "vm_state": self._vm_data.get("state"),
            "file": self._save_path,
            "version": self._version,
            "source": "local" if (self._local_ok and self._attr_entity_picture) else "proxy",
        }
        if self._last_change:
            attrs["last_screenshot_time"] = dt_util.as_local(
                self._last_change
            ).isoformat(timespec="seconds")
        return attrs

    def image(self):
        """Return bytes of image (called in executor).

        当 /local 直连不可用时，/api/image_proxy 走这里直接读文件。
        """
        if not self._save_path:
            return None
        try:
            with open(self._save_path, "rb") as image_file:
                return image_file.read()
        except OSError:
            _LOGGER.debug("读取画面文件失败: %s", self._save_path)
            return None

    async def async_update(self):
        """Refresh from the screenshot file."""
        try:
            self._vm_data = self.hass.data[DOMAIN_DATA][self._entry_id]["vm"].get(
                self._vm_key, {}
            )
        except KeyError:
            self._vm_data = {}

        self._save_path, self._url_base = vm_screenshot_paths(
            self._config_entry.data.get("host", "unknown"),
            self._display_name,
            self._screenshot_dir,
        )

        version = await self.hass.async_add_executor_job(file_version, self._save_path)
        if version is None:
            self._attr_available = False
            self._invalidate_image_cache()
            return

        self._attr_available = True
        if version != self._version:
            self._version = version
            self._last_change = dt_util.utcnow()
            self._attr_image_last_updated = self._last_change
            # cache-buster：文件一换 URL 就变，浏览器必须重新下载
            self._attr_entity_picture = (
                f"{self._url_base}?t={version}" if self._local_ok else None
            )
            self._invalidate_image_cache()

    def _invalidate_image_cache(self):
        """让 image 组件的缓存属性重新计算."""
        # entity_picture 不是缓存属性（普通 property），不需要失效
        for key in ("image_last_updated", "content_type", "image_url"):
            self.__dict__.pop(key, None)
        # async_image() 会缓存第一次取到的图，不清的话代理永远返回旧帧
        self._cached_image = None

    @callback
    def _handle_frame_changed(self):
        """Hub 通知：立刻强制刷新一次（这才是「秒级更新」的来源）."""
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_added_to_hass(self):
        """Subscribe to frame updates."""
        hub = get_hub(self.hass, self._entry_id)
        if hub:
            self._unsub_hub = hub.subscribe(self._vm_key, self._handle_frame_changed)

    async def async_will_remove_from_hass(self):
        """Unsubscribe from frame updates."""
        if self._unsub_hub:
            self._unsub_hub()
            self._unsub_hub = None
