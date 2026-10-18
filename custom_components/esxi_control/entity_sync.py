"""实体增量同步：把 HA 里的实体清单对齐到 ESXi 的真实状态。

背景
----
集成的所有实体都只在各平台的 ``async_setup_entry`` 里创建一次。于是在 ESXi 上
新增/删除/改名虚拟机之后，HA 侧既不会多出实体、也不会少掉实体：

* 新虚拟机永远看不见（连数据都拉回来了，只是没有任何实体承载它）；
* 删掉的虚拟机反而会一直挂着（更糟：残留的键让它继续读到删除前的旧值）。

这里提供**显式触发**的同步（service ``esxi_control.sync``）：

1. 强制刷新一次 ESXi 数据（绕过 ``update_data`` 的 45 秒节流）；
2. 让各平台重新算出「此刻应该存在的实体清单」；
3. 只把**缺失的**实体 add 进去（``unique_id`` 已存在的绝不重复创建）；
4. 数据刷新成功时，把注册表里已经不该存在的实体删掉，并顺手清空壳设备。

为什么是「手动」而不是「自动」
------------------------------
删除实体不可逆，而且「ESXi 一时连不上 / 权限变化导致列表为空」这类情况会让自动
逻辑误删整台主机的所有实体。让用户显式点一下，语义清晰，也天然带了一道人肉确认；
同时这里还额外守一道：**只有数据刷新确实成功**才执行删除。
"""
import asyncio
import logging
import time

from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_SHORT_ENTITY_IDS,
    DEFAULT_SHORT_ENTITY_IDS,
    DOMAIN,
    DOMAIN_DATA,
    EVENT_ENTRY_SYNCED,
)
from .naming import apply_short_entity_ids

_LOGGER = logging.getLogger(__name__)

#: 各平台登记「如何算出实体清单 + 如何添加实体」的键
HOOKS_KEY = "sync_hooks"


def register_platform(hass, entry_id, platform, build_entities, add_entities):
    """登记一个平台的同步钩子（由各平台在 setup 时调用）。

    ``build_entities(hass, config_entry)`` 必须是**纯函数**：只返回对象，
    不添加、不改状态、不改 hass.data —— 因为同步时它会被反复调用并与注册表比对。
    """
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if entry_data is None:
        return
    entry_data.setdefault(HOOKS_KEY, {})[platform] = {
        "build": build_entities,
        "add": add_entities,
    }


def dedupe_entities(entities):
    """按 ``unique_id`` 去掉同一批次里的重复实体（保持原顺序）。

    为什么需要：HA 的 ``async_add_entities`` 收到两个 unique_id 相同的实体时，
    后一个会被丢掉并刷一条
    ``Platform esxi_control does not generate unique IDs ... already exists`` ——
    实体本身不受影响（先到的那个照常工作），但日志会一直脏，而且说明构建逻辑
    算出了重复项。

    各平台的 ``_build_entities`` 在 ``unique_id`` 里带上了对象键，理论上一对一；
    真正算重只可能来自"同一个键被算了两次"（例如数据里出现指向同一对象的重复键、
    或者以后新增的实体忘了去重）。这里统一兜一层：**去重 + 告警里带上 unique_id**，
    既不影响功能，又能一眼看出是哪一类实体重了。
    """
    seen = set()
    result = []
    for entity in entities:
        unique_id = getattr(entity, "unique_id", None)
        if unique_id:
            if unique_id in seen:
                _LOGGER.warning(
                    "实体清单里出现重复的 unique_id（已忽略后一个）：%s", unique_id
                )
                continue
            seen.add(unique_id)
        result.append(entity)
    return result


def clear_platform_hooks(hass, entry_id):
    """卸载集成时清掉回调，避免它们继续指向已卸载的实体平台。"""
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    if entry_data is not None:
        entry_data.pop(HOOKS_KEY, None)


async def _wait_registered(hass, entry_id, unique_ids, timeout=8.0):
    """等新增的实体真正落进实体注册表（最多 ``timeout`` 秒）。

    平台的 ``async_add_entities`` 只是 ``create_task`` 排了个任务，真正写入实体注册表
    是在那之后。同步里刚 add 完就去遍历注册表，会一个都看不到 —— 短 entity_id 也就
    应用不上，新增实体只能留 HA 按中文名生成的拼音 id。
    """
    pending = {uid for uid in unique_ids if uid}
    if not pending:
        return
    registry = er.async_get(hass)
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for reg_entry in er.async_entries_for_config_entry(registry, entry_id):
            pending.discard(reg_entry.unique_id)
        if not pending:
            return
        await asyncio.sleep(0.2)
    if pending:
        _LOGGER.warning(
            "同步：等待 %s 个新实体注册超时，短 entity_id 可能没应用到它们身上",
            len(pending),
        )


async def async_sync_entry(hass, entry):
    """重新扫描 ESXi，增删实体使 HA 与真实状态一致。

    返回 ``{"added": n, "removed": m, "reason": str | None}``。
    """
    entry_id = entry.entry_id
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id)
    result = {"added": 0, "removed": 0, "reason": None}

    if entry_data is None:
        result["reason"] = "entry_not_loaded"
        return result

    hooks = entry_data.get(HOOKS_KEY) or {}
    client = entry_data.get("client")
    if not hooks or client is None:
        result["reason"] = "platforms_not_ready"
        return result

    # ---- 1) 强制刷新数据（顺带重建 vm / vmhost / datastore / license 子字典）----
    try:
        await hass.async_add_executor_job(client._update_data)  # noqa: SLF001
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("同步：刷新 ESXi 数据时出错")
        result["reason"] = "refresh_error"
        return result

    refreshed = bool(getattr(client, "last_update_ok", False))

    registry = er.async_get(hass)
    existing = {
        reg_entry.unique_id: reg_entry
        for reg_entry in er.async_entries_for_config_entry(registry, entry_id)
        if reg_entry.unique_id
    }

    # ---- 2) 各平台算出「此刻应存在的实体」，缺失的就地补上 ----
    wanted = set()
    failed_platforms = set()
    added_unique_ids = []

    for platform, hook in hooks.items():
        try:
            entities = hook["build"](hass, entry)
        except Exception:  # pylint: disable=broad-except
            # 算不出来就绝不动这个平台的实体（下面会把它们全部视为"要保留"）
            failed_platforms.add(platform)
            _LOGGER.exception("同步：%s 平台无法计算实体清单，本次跳过", platform)
            continue

        pending = []
        for entity in entities:
            unique_id = getattr(entity, "unique_id", None)
            if not unique_id:
                continue
            wanted.add(unique_id)
            if unique_id not in existing:
                pending.append(entity)

        if not pending:
            continue
        try:
            hook["add"](pending, True)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("同步：向 %s 平台添加实体失败", platform)
            continue
        added_unique_ids.extend(entity.unique_id for entity in pending)
        result["added"] += len(pending)
        _LOGGER.info("同步：%s 平台新增 %s 个实体", platform, len(pending))

    # 算不出清单的平台：它已有的实体一律视为要保留，避免误删
    for unique_id, reg_entry in existing.items():
        if reg_entry.entity_id.split(".", 1)[0] in failed_platforms:
            wanted.add(unique_id)

    # ---- 3) 删除已不该存在的实体（只在数据确实刷新成功时）----
    if not refreshed:
        result["reason"] = "refresh_failed_keep_all"
        _LOGGER.warning(
            "同步：ESXi 数据刷新未成功（主机可能不可达），本次只新增、不删除任何实体"
        )
        return result

    # 合理性守卫：刷新"成功"了，却一个主机、一台虚拟机都没看到，而注册表里还有实体 ——
    #   典型原因是权限被收回、或连到了别的（空的）vCenter/主机。这种情况绝不删。
    if existing and not (entry_data.get("vm") or {}) and not (entry_data.get("vmhost") or {}):
        result["reason"] = "suspicious_empty_keep_all"
        _LOGGER.warning(
            "同步：刷新成功但未发现任何主机/虚拟机（可能是权限或连接目标变化），"
            "为防止误删，本次不删除任何实体"
        )
        return result

    orphan_devices = set()
    for unique_id, reg_entry in existing.items():
        if unique_id in wanted:
            continue
        if reg_entry.device_id:
            orphan_devices.add(reg_entry.device_id)
        _LOGGER.info("同步：移除已不存在的实体 %s", reg_entry.entity_id)
        registry.async_remove(reg_entry.entity_id)
        result["removed"] += 1

    # ---- 4) 顺手清掉变成空壳的设备（整台虚拟机被删掉的情况）----
    if orphan_devices:
        dev_reg = dr.async_get(hass)
        for device_id in orphan_devices:
            if er.async_entries_for_device(
                registry, device_id, include_disabled_entities=True
            ):
                continue
            dev_reg.async_remove_device(device_id)
            _LOGGER.info("同步：移除已无实体的设备 %s", device_id)

    # ---- 5) 新增的实体再套一次短 entity_id 规则 ----
    # HA 自己生成的 entity_id 与集成的短名风格不一致（setup 时靠 apply_short_entity_ids
    #   统一），所以动态补进来的实体必须补这一步，否则会混着两套命名（中文名 → 拼音 id）。
    # ⚠️ 必须先在注册表里等到这些新实体：平台是 create_task 异步注册的，
    #   紧接着调用时注册表里还没有它们，短 id 会一个都应用不上（实测踩到）。
    # ⚠️ 也必须在刷新聚合快照之前做：快照条目里存的是 entity_id。
    if result["added"]:
        try:
            if entry.options.get(CONF_SHORT_ENTITY_IDS, DEFAULT_SHORT_ENTITY_IDS):
                await _wait_registered(hass, entry_id, added_unique_ids)
                apply_short_entity_ids(hass, entry)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("同步：应用短 entity_id 失败（实体本身不受影响）")

    # ---- 6) 让实体聚合快照立刻反映新清单（它有刷新间隔，不催要等一整个周期）----
    aggregate = entry_data.get("aggregate_entity")
    if aggregate is not None:
        try:
            aggregate.force_refresh()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("同步：刷新实体聚合快照失败")

    # ---- 7) 广播同步完成：外围功能（虚拟机控制台设备链接）据此跟进新增 VM ----
    hass.bus.async_fire(EVENT_ENTRY_SYNCED, {"entry_id": entry_id, "domain": DOMAIN})

    return result
