"""安全读写配置条目 options 的方式。

⚠️ HA 的 ``async_update_entry`` 会**创建一个新的 ConfigEntry 对象**并替换掉旧的，
而实体里缓存的 ``self._config_entry`` 永远指向最初那个旧对象。所以：

* 直接读 ``self._config_entry.options`` 会拿到**过期数据**；
* 基于它做「读-改-写」更糟 —— 每次写入都会把上一次刚存进去的内容覆盖掉。

实测踩到的坑：截图开关状态存在 ``options["screenshot_switches"]`` 里，多台虚拟机
（或批量开关并发）各自保存时互相覆盖，最后只剩一台的状态活着，HA 重启后其余
虚拟机全部回到默认的"关闭"。

因此这里统一按 ``entry_id`` 取「当前最新」的配置条目再操作。
"""
import logging

_LOGGER = logging.getLogger(__name__)


def current_options(hass, config_entry):
    """返回该配置条目**最新**的 options 副本（拿到的是副本，随便改）。"""
    if config_entry is None:  # YAML 模式（没有配置条目）
        return {}
    entry = hass.config_entries.async_get_entry(config_entry.entry_id)
    if entry is None:
        return dict(getattr(config_entry, "options", None) or {})
    return dict(entry.options or {})


def store_options(hass, config_entry, options):
    """把 options 写回该配置条目（内部自动定位最新的对象）。"""
    entry = hass.config_entries.async_get_entry(config_entry.entry_id)
    if entry is None:
        _LOGGER.warning(
            "配置条目 %s 已不存在，设置未保存", config_entry.entry_id
        )
        return False
    hass.config_entries.async_update_entry(entry, options=options)
    return True


def update_option(hass, config_entry, key, value):
    """只改一个键（基于最新 options），不会覆盖别人的改动。"""
    options = current_options(hass, config_entry)
    options[key] = value
    store_options(hass, config_entry, options)
    return options
