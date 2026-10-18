"""固定的短 entity_id 规则与迁移。

本集成原本让 HA 依据实体名自动生成 entity_id，中文名会被拼成一长串拼音，
例如 sensor.xu_ni_ji_windows10_xia_zai_windows10_xia_zai_cpushi_yong_lu。

这里按 unique_id 的结构反推出短且稳定的名字，例如：
    sensor.*_ha_attr_cpu_use_pct                     -> sensor.ha_cpu
    image.*_esxi_zhu_ji_hua_mian_ha                  -> image.ha_screen
    switch.*_esxi_zhu_ji_xu_ni_ji_ha                 -> switch.ha_power
    sensor.*_vmhost_localhost_localdomain_attr_memusage_gb -> sensor.localdomain_mem_used
    sensor.*_entities_aggregate                      -> sensor.esxi_all_entities
                                                        （固定 id，不受短 id 总开关影响）

主机名也压缩成最后一段：localhost.localdomain -> localdomain。

⚠️ 只改 **entity_id**，不动实体显示名：显示名保持中文（各平台 ``name`` 给出中文短名，
   再由 ``has_entity_name`` 拼成「设备名 + 中文短名」，如「虚拟机 HA 电源」）。

多台 ESXi 条目下存在同名虚拟机时（两台主机都叫 1001），短 id 会在**全局**范围内
检测碰撞，碰撞时给后规划的实体加上主机 IP 前缀消歧，例如
``switch.192_168_20_197_1001_power`` / ``switch.192_163_0_254_1001_power``；
只有一台主机、或两边虚拟机不同名时，仍然保持最简短的 ``switch.1001_power``。
"""
import logging
from collections import Counter

from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import DOMAIN, DOMAIN_DATA

_LOGGER = logging.getLogger(__name__)

# VM 属性的短后缀
VM_ATTR_SLUG = {
    "cpu_count": "cpu_count",
    "cpu_use_pct": "cpu",
    "cpuusage_ghz": "cpu_ghz",
    "memory_allocated_mb": "mem_alloc",
    "memory_used_mb": "mem_used",
    "memory_active_mb": "mem_active",
    "used_space_gb": "disk_used",
    "uptime_hours": "uptime",
    "snapshots": "snapshots",
    "tools_status": "tools",
    "guest_os": "os",
    "guest_ip": "ip",
    "status": "health",
    "state": "power",
    "host_name": "host",
    "name": "vm_name",
    # 实时性能（perf 批量查询）
    "cpu_ready_pct": "cpu_ready",
    "mem_balloon_mb": "mem_balloon",
    "net_dropped_ps": "net_dropped",
}

# 主机属性的短后缀
HOST_ATTR_SLUG = {
    "name": "name",
    "available_power_policies": "power_policy_options",
    "power_policy": "power_policy",
    "build": "build",
    "cputotal_ghz": "cpu_total",
    "cpuusage_ghz": "cpu_ghz",
    "maintenance_mode": "maintenance",
    "memtotal_gb": "mem_total",
    "memusage_gb": "mem_used",
    "shutdown_supported": "can_shutdown",
    "state": "power",
    "uptime_hours": "uptime",
    "version": "version",
    "vms": "vm_total",
    "vms_on": "vm_on",
    # 主机增强详情标量（明细列表走 host_detail_* 聚合诊断传感器）
    "connection_state": "connection",
    "overall_status": "overall",
    "hyperthreading_active": "hyperthreading",
    "hardware_vendor": "vendor",
    "hardware_model": "model",
    "bios_version": "bios",
    "bios_date": "bios_date",
    "cpu_sockets": "cpu_sockets",
    "cpu_cores": "cpu_cores",
    "cpu_threads": "cpu_threads",
    "ntp_servers": "ntp_servers",
    "ntp_timezone": "timezone",
}

# VM 相关的 unique_id 后缀 -> entity_id 后缀
# ⚠️ 顺序敏感：长前缀 / 易混前缀必须放前面（snapshot_remove_all 先于 remove_first）
VM_KINDS = (
    ("screen_", "screen"),
    ("reboot_", "restart"),
    # 强制重启（硬重置）：button.<vm>_force_restart
    ("force_restart_", "force_restart"),
    # 客户机优雅关机（ShutdownGuest，需 Tools）：button.<vm>_guest_shutdown
    ("shutdown_", "guest_shutdown"),
    # 硬断电（PowerOffVM）：button.<vm>_power_off
    ("power_off_", "power_off"),
    ("switch_", "power"),
    ("screenshot_", "screenshot"),
    # 客户机动作（Tools）
    ("standby_", "standby"),
    # ESXi 级挂起 / 唤醒
    ("suspend_", "suspend"),
    ("wake_", "wake"),
    # 光盘
    ("cdrom_eject_", "cdrom_eject"),
    # 合并磁盘
    ("consolidate_", "consolidate"),
    # 随主机自启动开关
    ("autostart_", "autostart"),
    # 控制台远程地址传感器：sensor.<vm>_remote
    ("console_url_", "remote"),
    ("snapshot_revert_", "snapshot_revert"),
    ("tools_upgrade_", "tools_upgrade"),
    ("snapshot_create_", "snapshot_create"),
    ("snapshot_remove_all_", "snapshot_remove_all"),
    ("snapshot_remove_first_", "snapshot_remove_first"),
    ("snapshot_remove_last_", "snapshot_remove_last"),
    # 来宾凭据：binary_sensor.<vm>_guest_cred / button.<vm>_guest_validate
    ("guest_validate_", "guest_validate"),
    ("bguest_", "guest_cred"),
    # VM 注意事项（卡问题/待合并）：binary_sensor.<vm>_issue_state
    ("bissue_", "issue_state"),
    # 光盘已连接：binary_sensor.<vm>_cdrom_state
    ("bcdrom_", "cdrom_state"),
)

#: 主机诊断聚合传感器：host_detail_{kind}_{host}
HOST_DETAIL_KINDS = ("health", "services", "network", "firewall", "sessions", "storage")

#: 「实体聚合」传感器的固定 entity_id。
#: ⚠️ 它**不受**「短 entity_id」总开关（CONF_SHORT_ENTITY_IDS）影响 ——
#:   开关关掉时其它实体仍用 HA 生成的长拼音 id，唯独这一个保持固定：
#:   卡片 / 自动化需要一个「永远能写死」的入口。
#:   由 sensor.ESXiEntitiesAggregateSensor 自己保证：__init__ 里设置该 id，
#:   async_added_to_hass 里把已注册的旧 id 迁移过来（注册表 id 优先）。
AGGREGATE_ENTITY_ID = "sensor.esxi_all_entities"


def _short_host_name(raw, host_slugs):
    """把完整主机名 / 主机键替换成短名；不在映射里就原样返回。"""
    if not raw:
        return raw
    if raw in (host_slugs or {}):
        return host_slugs[raw]
    # 复合片段（如 localhost_localdomain_license）时替换前缀部分
    for full, short_name in (host_slugs or {}).items():
        if raw == full or raw.startswith(f"{full}_"):
            return short_name + raw[len(full):]
    return raw


def _join(domain, name, suffix):
    """Build "${domain}.${name}_${suffix}"，名字拿不到 slug 时放弃。"""
    slug = slugify(name)
    if not slug:
        # 纯中文之类拿不到 ascii slug 时，与其造出非法的 id，不如保持原样
        return None
    return f"{domain}.{slug}_{suffix}" if suffix else f"{domain}.{slug}"


def host_slug_map(hass, entry_id):
    """Return {完整主机名 slug: 短主机名 slug}.

    localhost.localdomain -> localdomain，单标签主机名（如 esxi01）保持不变。
    """
    mapping = {}
    entry_data = hass.data.get(DOMAIN_DATA, {}).get(entry_id) or {}
    for raw_name in entry_data.get("vmhost", {}) or {}:
        raw = str(raw_name)
        # ESXi 上报的域名常是 FQDN；有些路径里点已被换成下划线，两种都要认
        dotted = raw if "." in raw else raw.replace("_", ".")
        short = slugify(dotted.split(".")[-1]) or slugify(raw)
        for key in {raw, slugify(raw), raw.replace("_", ".")}:
            mapping[key] = short
    return mapping


def _disambiguate(config_entry, target):
    """碰撞消歧：在目标 id 的对象名前插一段配置主机（IP）slug。"""
    domain, _, rest = target.partition(".")
    host_slug = slugify(str(config_entry.data.get("host", "")))
    if not host_slug:
        return None
    return f"{domain}.{host_slug}_{rest}"


def suggested_entity_id(domain, entry_id, unique_id, host_slugs=None):
    """Return the short fixed entity_id for an entity, or None to leave it alone."""
    if not unique_id or f"{entry_id}_" not in unique_id:
        return None
    # unique_id 形如 "{host_ip}_{entry_id}_{kind}_{name}"，这里先剥掉前半段
    rest = unique_id.split(f"{entry_id}_", 1)[1]

    def short_host(raw):
        """把完整主机名替换成短名。"""
        return _short_host_name(raw, host_slugs)

    def host_singleton():
        """主机级单例实体（「事件」等）用的主机短名。

        这类实体的 unique_id 形如 ``{config_host}_{entry_id}_events`` ——
        ESXi 主机名不在里面（只有配置里填的主机地址）。所以先从 ``entry_id``
        之前的前缀取；配置地址是 IP 拿不到短名时，单主机场景直接用那台主机的
        短名兜底，保证与同设备下的 ``sensor.<host>_health`` 等风格一致。
        """
        prefix = unique_id.split(f"{entry_id}_", 1)[0].strip("_")
        mapped = short_host(prefix)
        if mapped and mapped != prefix:
            return mapped
        if len(host_slugs or {}) == 1:
            return next(iter(host_slugs.values()))
        return slugify(prefix) or None

    if rest == "entities_aggregate" or rest.endswith("_entities_aggregate"):
        # 「实体聚合」：固定 id，且**不随短 id 总开关变化**
        #   （关掉短 id 这一趟不会跑，由该实体自己在 async_added_to_hass 里迁移）
        return AGGREGATE_ENTITY_ID
    if rest == "screenshot_interval" or rest.endswith("_screenshot_interval"):
        return f"{domain}.screenshot_interval"
    if rest == "aggregate_interval" or rest.endswith("_aggregate_interval"):
        # 实体聚合刷新间隔（number 实体）→ number.aggregate_interval
        return f"{domain}.aggregate_interval"
    if rest == "update_interval" or rest.endswith("_update_interval"):
        # 数据刷新间隔（number 实体）→ number.update_interval
        return f"{domain}.update_interval"

    if rest == "sync_button" or rest.endswith("_sync_button"):
        # 集成侧的「同步实体」按钮 → button.sync
        return f"{domain}.sync"

    if rest == "batch_grouping" or rest.endswith("_batch_grouping"):
        # 批量分组参数 → text.batch_grouping
        return f"{domain}.batch_grouping"

    if rest == "operation_log" or rest.endswith("_operation_log"):
        # 批量操作日志 → sensor.operation_log
        return f"{domain}.operation_log"

    if rest == "events" or rest.endswith("_events"):
        # 事件流传感器 → sensor.<host>_events
        #   unique_id 形如 {config_host}_{entry_id}_events：主机名不在 rest 里，
        #   由 host_singleton() 从前缀解析（与同设备的 sensor.<host>_health 对齐）
        return _join(domain, host_singleton(), "events")

    if rest.startswith("all_vms_"):
        # 整机批量实体：switch.all_vms_power / switch.all_vms_screenshot /
        #   button.all_vms_snapshot / button.all_vms_restart
        return f"{domain}.all_vms_{rest[len('all_vms_'):]}"

    # ⚠️ 必须放在 ``vm_`` 分支之前：``vm_manager_url_<host>`` 同样以 vm_ 开头，
    #    被下面的 VM 分支吞掉就永远拿不到短 id（只剩 HA 生成的长拼音）。
    if rest.startswith("vm_manager_url_"):
        # vm_manager_url_{host} → sensor.<host>_vm_manager
        return _join(
            domain, short_host(rest[len("vm_manager_url_"):]), "vm_manager"
        )

    if rest.startswith("vm_"):
        tail = rest[3:]
        if "_attr_" in tail:
            vm, attr = tail.split("_attr_", 1)
            suffix = VM_ATTR_SLUG.get(attr) or slugify(attr)
            return _join(domain, short_host(vm), suffix)
        # 网卡开关：vm_nic_<mac无分隔>_<vm名> → switch.<vm>_nic_<mac后6位>
        if tail.startswith("nic_"):
            payload = tail[len("nic_"):]
            mac, _, vm_name = payload.partition("_")
            if mac and vm_name and len(mac) >= 6:
                return _join(domain, short_host(vm_name), f"nic_{mac[-6:].lower()}")
            return None
        for prefix, suffix in VM_KINDS:
            if tail.startswith(prefix):
                return _join(domain, short_host(tail[len(prefix):]), suffix)
        return None

    if rest.startswith("vmhost_") and "_attr_" in rest:
        tail = rest[len("vmhost_"):]
        host, attr = tail.split("_attr_", 1)
        suffix = HOST_ATTR_SLUG.get(attr) or slugify(attr)
        return _join(domain, short_host(host), suffix)

    if rest.startswith("host_switch_"):
        return _join(domain, short_host(rest[len("host_switch_"):]), "power")
    if rest.startswith("host_reboot_"):
        return _join(domain, short_host(rest[len("host_reboot_"):]), "reboot")

    # ---- 主机随主机自启动总开关：host_autostart_<host> → switch.<host>_autostart ----
    if rest.startswith("host_autostart_"):
        return _join(domain, short_host(rest[len("host_autostart_"):]), "autostart")

    # ---- 主机维护 / 锁定（switch 可写、binary_sensor 只读状态分开命名）----
    if rest.startswith("host_maintenance_"):
        return _join(
            domain, short_host(rest[len("host_maintenance_"):]), "maintenance"
        )
    if rest.startswith("host_lockdown_"):
        return _join(domain, short_host(rest[len("host_lockdown_"):]), "lockdown")
    if rest.startswith("host_bmaintenance_"):
        return _join(
            domain, short_host(rest[len("host_bmaintenance_"):]), "maintenance_state"
        )
    if rest.startswith("host_blockdown_"):
        return _join(
            domain, short_host(rest[len("host_blockdown_"):]), "lockdown_state"
        )

    # ---- 受管服务（SSH / ESXi Shell / NTP）----
    if rest.startswith("host_service_restart_"):
        # host_service_restart_{slug}_{host} → button.<host>_<slug>_restart
        tail = rest[len("host_service_restart_"):]
        slug, _, host = tail.partition("_")
        return _join(domain, short_host(host), f"{slug}_restart")
    if rest.startswith("host_service_"):
        # host_service_{slug}_{host} → switch.<host>_<slug>_service
        tail = rest[len("host_service_"):]
        slug, _, host = tail.partition("_")
        return _join(domain, short_host(host), f"{slug}_service")
    if rest.startswith("host_bservice_"):
        # host_bservice_{slug}_{host} → binary_sensor.<host>_<slug>_running
        tail = rest[len("host_bservice_"):]
        slug, _, host = tail.partition("_")
        return _join(domain, short_host(host), f"{slug}_running")
    if rest.startswith("select_service_policy_"):
        # select_service_policy_{slug}_{host} → select.<host>_<slug>_policy
        tail = rest[len("select_service_policy_"):]
        slug, _, host = tail.partition("_")
        return _join(domain, short_host(host), f"{slug}_policy")

    # ---- 服务清单刷新 / NTP / 诊断详情 ----
    if rest.startswith("host_refresh_services_"):
        return _join(
            domain,
            short_host(rest[len("host_refresh_services_"):]),
            "refresh_services",
        )
    if rest.startswith("text_ntp_servers_"):
        return _join(
            domain, short_host(rest[len("text_ntp_servers_"):]), "ntp_servers"
        )
    if rest.startswith("host_detail_"):
        # host_detail_{kind}_{host} → sensor.<host>_<kind>
        tail = rest[len("host_detail_"):]
        for kind in HOST_DETAIL_KINDS:
            if tail.startswith(kind + "_"):
                return _join(domain, short_host(tail[len(kind) + 1:]), kind)

    if rest.startswith("select_power_policy_"):
        return _join(
            domain, short_host(rest[len("select_power_policy_"):]), "power_policy"
        )
    if rest.startswith("datastore_"):
        return _join(domain, f"ds_{rest[len('datastore_'):]}", None)
    if rest.startswith("license_"):
        host = short_host(rest[len("license_"):].replace("_license", ""))
        return _join(domain, f"license_{host}", None)

    return None


def _collect_plans(hass, ent_reg):
    """为所有已加载的 esxi 条目收集 (entry, registry_entry, 期望短id) 规划。"""
    plans = []
    loaded = hass.data.get(DOMAIN_DATA, {}) or {}
    config_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in config_entries:
        if entry.entry_id not in loaded:
            continue
        host_slugs = host_slug_map(hass, entry.entry_id)
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            target = suggested_entity_id(
                reg_entry.domain, entry.entry_id, reg_entry.unique_id, host_slugs
            )
            if target:
                plans.append((entry, reg_entry, target))
    return plans


def apply_short_entity_ids(hass, config_entry):
    """Rename already registered entities to their short fixed ids.

    幂等：id 已经一致或被占用就直接跳过，所以每次 setup 调用都没有副作用。

    多条目场景：规划在**所有已加载条目**范围内统一进行（见模块 docstring），
    同名对象的短 id 碰撞时加主机 IP 前缀消歧，保证两个 ESXi 下同名虚拟机不会
    抢同一个 entity_id。
    """
    ent_reg = er.async_get(hass)
    plans = _collect_plans(hass, ent_reg)
    if not plans:
        return 0

    base_counts = Counter(target for _, _, target in plans)
    planned_ids = {reg_entry.entity_id for _, reg_entry, _ in plans}

    renamed = 0
    for entry, reg_entry, target in plans:
        final_target = target
        if base_counts[target] > 1:
            # 与另一个条目的实体抢同一个短 id：加主机 IP 消歧
            final_target = _disambiguate(entry, target) or target
        else:
            # 目标 id 已被不属于本规划的实体占用（用户手工占用 / 历史遗留）：
            # 不抢别人的 id，自己消歧
            holder = ent_reg.async_get(target)
            if holder is not None and holder.entity_id not in planned_ids:
                final_target = _disambiguate(entry, target) or target

        if not final_target or final_target == reg_entry.entity_id:
            continue
        if ent_reg.async_get(final_target) is not None:
            continue
        try:
            ent_reg.async_update_entity(
                reg_entry.entity_id, new_entity_id=final_target
            )
            renamed += 1
        except ValueError as err:
            _LOGGER.debug("跳过重命名 %s: %s", reg_entry.entity_id, err)

    if renamed:
        _LOGGER.info(
            "已把 %s 个实体重命名为固定的短 entity_id；"
            "若你在自动化/卡片里写死了旧的 entity_id，需要同步修改",
            renamed,
        )
    return renamed
