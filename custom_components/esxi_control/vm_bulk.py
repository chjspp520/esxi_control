"""批量控制所有虚拟机时的共用工具。

「全部虚拟机电源 / 截图 / 快照 / 重启」这几个实体都要做同一件事：
把这台主机上的**所有虚拟机**对应的实体找出来，然后对它们统一调用一次服务。

这里刻意按 ``unique_id`` 反查 ``entity_id``，而不是自己拼 entity_id：
集成的短 entity_id 机制会改写 entity_id（例如 ``switch.ha_power``），
但 ``unique_id`` 永远不会变，所以反查是唯一可靠的方式。
"""
import asyncio
import logging

from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_NAME, DOMAIN, DOMAIN_DATA
from .entry_options import current_options
from .op_log import get_log

_LOGGER = logging.getLogger(__name__)

#: 分组参数在配置条目 options 里的键
GROUPING_OPTION = "batch_grouping"
#: 默认值：0,0 = 不启用（全并行，保持原有行为）
DEFAULT_GROUPING = "0,0"
#: 组间间隔上限（秒）—— 避免误填一个把批量操作挂死的值
MAX_INTERVAL = 3600


def normalize_grouping(raw):
    """把用户输入归一化成 ``"组大小,间隔"``；无法解析时返回 ``None``。

    宽容：允许空格、中文逗号、只写一个数（等价间隔 0）。
    严格拒绝：负数、非数字、超过两段、空段。
    """
    if raw is None:
        return None
    text = str(raw).strip().replace("，", ",")
    if not text:
        return None
    # ⚠️ 只按逗号分段、再逐段 strip：不能先把所有空格删掉，否则 "10 20"
    #   会被悄悄拼成 "1020" 而蒙混过关（要拒绝的是"用空格当分隔符"这种写法）
    parts = [part.strip() for part in text.split(",")]
    if len(parts) > 2:
        return None
    values = []
    for part in parts:
        if not part.isdigit():  # 负数 / 字母 / 空段 / 内含空格都在这里被拒
            return None
        values.append(int(part))
    while len(values) < 2:
        values.append(0)
    size, interval = values
    return f"{size},{min(interval, MAX_INTERVAL)}"


def parse_grouping(raw):
    """返回 ``(组大小, 间隔秒)``；未启用或解析失败时返回 ``(0, 0)``。

    组大小为 0 视为「不启用」—— 此时组间间隔没有意义，一并忽略。
    """
    normalized = normalize_grouping(raw)
    if not normalized:
        return (0, 0)
    size_text, interval_text = normalized.split(",")
    size = int(size_text)
    if size <= 0:
        return (0, 0)
    return (size, int(interval_text))


def current_grouping(hass, config_entry):
    """读取当前生效的分组设置。"""
    options = current_options(hass, config_entry)
    return parse_grouping(options.get(GROUPING_OPTION, DEFAULT_GROUPING))


def _display_name(hass, entity_id):
    """取实体的显示名（日志里用），拿不到就退回 entity_id。"""
    states = getattr(hass, "states", None)
    state = states.get(entity_id) if states is not None else None
    if state is not None:
        name = state.attributes.get("friendly_name")
        if name:
            return name
    return entity_id


async def run_batch(
    hass, config_entry, entity_ids, *, operation, operation_key, domain, service
):
    """按「分组 + 组间间隔」下发一次批量操作。

    * **组内并行**：一次服务调用带上该组全部实体；
    * **组间等间隔**：这是固定节奏，不是「等上一批完成再发下一批」
      （开关机/重启都是异步下发，命令本身立即返回）；
    * 全过程写入操作日志中枢，实体 attributes 上能看到进度。
    """
    entry_id = config_entry.entry_id
    log = get_log(hass, entry_id)
    size, interval = current_grouping(hass, config_entry)

    if not entity_ids:
        if log is not None:
            log.begin(operation, operation_key, 0)
            log.end("跳过", "没有需要操作的虚拟机")
        return 0

    if log is not None:
        log.begin(operation, operation_key, len(entity_ids))

    groups = (
        [entity_ids[index:index + size] for index in range(0, len(entity_ids), size)]
        if size > 0
        else [entity_ids]
    )

    try:
        for index, group in enumerate(groups):
            if log is not None:
                names = [_display_name(hass, entity_id) for entity_id in group]
                label = "、".join(names) if len(names) <= 3 else f"{len(names)} 台"
                log.add("下发", label)
            await call_all(hass, domain, service, group)
            if log is not None:
                log.advance(len(group))
            if index < len(groups) - 1 and interval > 0:
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        if log is not None:
            log.end("已取消", "批量操作被中断")
        raise

    if log is not None:
        # 用「已下发」而不是「完成」：命令已经提交给 ESXi，但虚拟机还在继续执行
        #   （关机/重启/快照在 ESXi 侧都是异步的，这里刻意不阻塞等待）
        log.end("已下发", f"共 {len(entity_ids)} 台，分 {len(groups)} 批")
    return len(entity_ids)


def vm_keys(hass, entry_id):
    """当前主机上的虚拟机内部键（与实体 unique_id 里的 vm_name 一致）。"""
    entry_data = (hass.data.get(DOMAIN_DATA, {}) or {}).get(entry_id) or {}
    return list(entry_data.get("vm", {}) or {})


# ===================== 设备标识（多 ESXi 条目同名对象隔离）=====================
# 0.10 起 identifier 必须带配置条目 id：不同条目下同名虚拟机/主机以前会被设备
# 注册表合并成同一个设备，导致虚拟机 1001 的实体串到另一台 ESXi 的设备页里。
def host_device_key(entry_id, host_key=None):
    """主机设备 identifier 的值（不含 DOMAIN 元组外壳）。"""
    return f"{entry_id}__host_{host_key or 'default'}"


def vm_device_key(entry_id, vm_key):
    """虚拟机设备 identifier 的值。"""
    return f"{entry_id}__vm_{vm_key}"


def device_by_identifier(dev_registry, identifier):
    """按 (DOMAIN, identifier) 取设备，兼容新旧 HA 的设备注册表接口。

    HA 2025.x 起 ``device_registry.async_get_device(identifiers=...)`` 已弃用
    （identifier 不再跨配置条目唯一，官方说明 2027.8 起停用，日志里会打一条
    frame 警告），替代品是 ``async_get_device_by_identifier(identifier)``。
    老版本没有这个方法，所以两种都试。

    本集成自己的 identifier 已经把配置条目 id 拼在里面（见 host_device_key /
    vm_device_key），天然唯一，不需要再传 config_entry_id。
    """
    getter = getattr(dev_registry, "async_get_device_by_identifier", None)
    if getter is not None:
        try:
            return getter(identifier)
        except TypeError:
            # 签名在不同版本间有差异：退回老接口，别为一次查设备把功能打断
            _LOGGER.debug("async_get_device_by_identifier 调用失败，退回旧接口", exc_info=True)
    return dev_registry.async_get_device(identifiers={identifier})


def host_device_info(hass, config_entry, host_key=None):
    """主机设备信息。

    所有「整机」实体（同步按钮、全部虚拟机开关等）都挂到同一个主机设备下，
    不额外制造新设备 —— 设备名与其它主机实体保持一致（DEFAULT_NAME）。
    identifier 带配置条目 id，多台 ESXi 同名主机互不串显。
    """
    entry_data = (hass.data.get(DOMAIN_DATA, {}) or {}).get(config_entry.entry_id) or {}
    if host_key is None:
        host_key = next(
            iter(entry_data.get("vmhost", {}) or {}),
            str(config_entry.data.get("host", "unknown")).replace(".", "_"),
        )
    return {
        "identifiers": {(DOMAIN, host_device_key(config_entry.entry_id, host_key))},
        "name": DEFAULT_NAME,
        "manufacturer": "VMware ESXi",
        "model": "ESXi 主机",
    }


def vm_device_info(hass, config_entry, vm_key):
    """虚拟机设备信息（同样带配置条目 id 隔离）。

    设备显示名优先用快照里的真实 vm_name（vm_key 可能是加了后缀的内部键），
    快照暂缺（刷新中 / 实体刚构建）时退回内部键。
    """
    entry_data = (hass.data.get(DOMAIN_DATA, {}) or {}).get(config_entry.entry_id) or {}
    vm_data = (entry_data.get("vm") or {}).get(vm_key) or {}
    proper_name = vm_data.get("vm_name") or vm_key
    return {
        "identifiers": {(DOMAIN, vm_device_key(config_entry.entry_id, vm_key))},
        "name": f"虚拟机 {proper_name}",
        "manufacturer": "VMware",
        "model": "虚拟机",
    }


def vm_entity_ids(hass, config_entry, platform, kind):
    """反查各虚拟机实体的 entity_id；查不到的直接跳过。

    ``kind`` 是 unique_id 里的中段标识：
    ``vm_switch`` / ``vm_screenshot`` / ``vm_reboot`` / ``vm_snapshot_create``。
    """
    registry = er.async_get(hass)
    host = str(config_entry.data.get("host", "unknown")).replace(".", "_")
    entity_ids = []
    for vm_name in vm_keys(hass, config_entry.entry_id):
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{host}_{config_entry.entry_id}_{kind}_{vm_name}"
        )
        if entity_id:
            entity_ids.append(entity_id)
    return entity_ids


async def refresh_data_now(hass, entry_id):
    """用**无节流**内核立刻刷一次 ESXi 数据。

    实体轮询走的是带节流的 ``update_data()``（间隔可配，默认 45 秒），提交开关机之后
    靠它要等很久才能看到真实状态；等待确认的操作就用这个内核主动催，让它尽快收敛。
    返回是否刷新成功。
    """
    entry_data = (hass.data.get(DOMAIN_DATA, {}) or {}).get(entry_id) or {}
    client = entry_data.get("client")
    if client is None:
        return False
    try:
        await hass.async_add_executor_job(client._update_data)  # noqa: SLF001
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("刷新 ESXi 数据失败", exc_info=True)
        return False
    return True


class BatchEntityMixin:
    """批量实体的共用部分：并发保护 + 卸载时取消。

    · 同一时间只允许一个批量任务跑（否则两批任务交叉，"分组 + 间隔"的语义会彻底乱掉）；
    · 实体卸载（重载集成 / 删除）时取消正在 sleep 的任务，不留孤儿协程继续下发命令。
    """

    _batch_task = None

    async def async_added_to_hass(self):
        """Register the cleanup callback."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_batch)

    def _batch_busy(self):
        """是否有批量任务正在跑。"""
        return self._batch_task is not None and not self._batch_task.done()

    def _cancel_batch(self):
        """取消正在进行的批量任务。"""
        if self._batch_task is not None and not self._batch_task.done():
            _LOGGER.info("批量操作被中断（实体已卸载或集成重载）")
            self._batch_task.cancel()
        self._batch_task = None

    def _start_batch(self, coro):
        """启动批量任务；已有任务在跑时拒绝并返回 False。"""
        if self._batch_busy():
            _LOGGER.warning("已有批量操作正在进行中，忽略本次请求")
            return False
        self._batch_task = self.hass.async_create_task(coro)
        return True


async def call_all(hass, domain, service, entity_ids):
    """对一批实体调用同一个服务（不阻塞，调用后立刻返回）。

    用服务调用而不是直接操作 ESXi，是为了复用每台虚拟机自己那套逻辑：
    例如关机带 VM Tools 的走优雅关机、没装的走硬关闭，重启会先检查运行状态。
    """
    if not entity_ids:
        _LOGGER.warning("批量操作：没有找到任何可操作的虚拟机实体")
        return 0
    await hass.services.async_call(
        domain, service, {"entity_id": entity_ids}, blocking=False
    )
    return len(entity_ids)
