"""Constants for ESXi Control."""
DOMAIN = "esxi_control"
DOMAIN_DATA = f"{DOMAIN}_data"

#: 改名前的域名：只在**读取旧数据**（.storage 里的凭据 / 开关状态 / 学到的访问地址）
#: 时用一次，用于一次性迁移；新写入一律用 DOMAIN。
LEGACY_DOMAIN = "esxi_stats"

PLATFORMS = [
    "sensor",
    "binary_sensor",
    "switch",
    "button",
    "select",
    "image",
    "number",
    "text",
]
REQUIRED_FILES = [
    "const.py",
    "esxi.py",
    "manifest.json",
    "sensor.py",
    "binary_sensor.py",
    "switch.py",
    "button.py",
    "select.py",
    "image.py",
    "number.py",
    "entity_sync.py",
    "event_hub.py",
    "vm_bulk.py",
    "optimistic.py",
    "entry_options.py",
    "op_log.py",
    "text.py",
    "naming.py",
    "screenshot_hub.py",
    "console_web.py",
    "cdrom_ops.py",
    "iso9660.py",
    "vm_admin_ops.py",
    "guest_ops.py",
    "guest_store.py",
    "guest_router.py",
    "ssh_ops.py",
    "config_flow.py",
    "services.yaml",
    "translations/en.json",
    "www_console/index.html",
    "www_console/frontend/console-embed.js",
    "www_console/vendor/jquery.js",
    "www_console/vendor/jquery.ui.js",
    "www_console/vendor/wmks.js",
    "www_admin/index.html",
    "www_guest/index.html",
]
VERSION = "1.0.0"

# 实体同步完成后广播的事件：控制台设备链接等外围功能靠它在 VM 增删后跟进
EVENT_ENTRY_SYNCED = "esxi_control_synced"

# 每条新 ESXi 事件在 HA 事件总线上派发的事件名（可用于自动化触发/通知）
EVENT_ESXI_EVENT = "esxi_control_event"
# 事件流中枢存放在 entry_data 里的键名
EVENT_HUB_KEY = "event_hub"
# 事件轮询周期（秒）：比全量数据刷新轻得多，只建一个事件收集器读增量
EVENT_POLL_INTERVAL = 15
# 首次启动往回看多久来填充传感器（不触发总线/日志簿，避免重启刷历史屏）
EVENT_BACKLOG_SECONDS = 30 * 60
# 传感器里保留多少条最近事件（recorder 单状态属性 ~16KB 上限，不能太大）
EVENT_MAX_KEEP = 20
# 单轮最多处理多少条新事件（事件风暴时的洪峰保护）
EVENT_MAX_NEW_PER_POLL = 300

# 虚拟机控制台网页 / API / WebSocket 代理的路径前缀
CONSOLE_PAGE_PREFIX = "/esxi_control/console"
CONSOLE_STATIC_PREFIX = "/esxi_control/console/static"
CONSOLE_TICKET_PREFIX = "/esxi_control/api/console_ticket"
CONSOLE_ORIGIN_PREFIX = "/esxi_control/api/console_origin"
CONSOLE_CDROM_PREFIX = "/esxi_control/api/console_cdrom"
CONSOLE_WS_PREFIX = "/esxi_control/ws"

# 来宾操作（Guest Operations：进程 / 文件 / 注册表）管理页面与 API
CONSOLE_GUEST_PAGE_PREFIX = "/esxi_control/guest"
CONSOLE_GUEST_API_PREFIX = "/esxi_control/api/guest"
CONSOLE_GUEST_UPLOAD_PREFIX = "/esxi_control/api/guest_upload"
CONSOLE_GUEST_DOWNLOAD_PREFIX = "/esxi_control/api/guest_download"

# 主机级「虚拟机管理」悬浮窗（创建 / 克隆 / 改配 / 删除 / 模板磁盘）
ADMIN_PAGE_PREFIX = "/esxi_control/admin"
ADMIN_API_PREFIX = "/esxi_control/api/vmadmin"

# ISO 上传工作目录（数据存储根目录下的相对目录名）
ISO_UPLOAD_DIR = "ha_iso_uploads"
ISSUE_URL = "https://github.com/chjspp520/esxi_control/issues"

# 选项：设备页「访问」链接使用的访问基地址（端口映射 / 反向代理场景下手工指定，
# 例如 http://192.168.1.10:18123 ；留空则按「页面实际访问来源 → HA 外部 URL」
# 的顺序自动判断）
CONF_CONSOLE_BASE_URL = "console_base_url"

STARTUP = """
-------------------------------------------------------------------
{name}
Version: {version}
This is a custom component
If you have any issues with this you need to open an issue here:
{issueurl}
-------------------------------------------------------------------
"""

CONF_NAME = "name"
CONF_DS_STATE = "datastore"
CONF_LIC_STATE = "license"
CONF_NOTIFY = "notify"

DEFAULT_NAME = "ESXi 主机"
DEFAULT_PORT = 443
DEFAULT_DS_STATE = "free_space_gb"
DEFAULT_LIC_STATE = "status"
CONF_SCREENSHOT_DIR = "screenshot_dir"
DEFAULT_SCREENSHOT_DIR = "/media/esxiimg"
CONF_SCREENSHOT_INTERVAL = "screenshot_interval"
DEFAULT_SCREENSHOT_INTERVAL = 15
# 下限只看抓图本身的代价：每次都要登录 ESXi 取图 + 写盘（实测 1~3 秒），
#   设得太密会把 ESXi 的 hostd 打满。截图开关由实体自管理定时器按这个间隔抓图，
#   **不受**平台轮询粒度（switch 平台 15 秒）限制。
MIN_SCREENSHOT_INTERVAL = 2
MAX_SCREENSHOT_INTERVAL = 600
# 是否把实体重命名为短且固定的 entity_id（关闭后保留 HA 自动生成的长 id）
CONF_SHORT_ENTITY_IDS = "short_entity_ids"
DEFAULT_SHORT_ENTITY_IDS = True
# 实体聚合快照的刷新间隔（秒）：聚合实体的 devices 属性重建一次的实际周期。
#   平台轮询是 15 秒一次，这里再节流；调小只增加「遍历实体/设备注册表」的开销，
#   聚合实体本身只是「实体目录」，实时值请直接读各实体状态。
CONF_AGGREGATE_INTERVAL = "aggregate_interval"
DEFAULT_AGGREGATE_INTERVAL = 60
MIN_AGGREGATE_INTERVAL = 10
MAX_AGGREGATE_INTERVAL = 3600
# 数据刷新间隔（秒）：实体轮询触发「真正向 ESXi 拉一次全量数据」的最小间隔。
#   每次刷新都会连一次 ESXi 并遍历全部主机 / 虚拟机 / 数据存储 / 许可证，
#   调小会让界面上的实时值更跟手，代价是 ESXi 侧 hostd 压力线性上升 ——
#   所以下限刻意保守（再小多半也没收益：数据刷新由实体轮询触发，
#   sensor 平台 15 秒一轮、switch 平台 2 秒一轮）。
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 45
MIN_UPDATE_INTERVAL = 10
MAX_UPDATE_INTERVAL = 3600

DEFAULT_OPTIONS = {
    "datastore": "free_space_gb",
    "license": "status",
    "notify": "true",
}

DATASTORE_STATES = [
    "connected_hosts",
    "free_space_gb",
    "total_space_gb",
    "type",
    "virtual_machines",
]

LICENSE_STATES = ["expiration_days", "status"]

MAP_TO_MEASUREMENT = {
    "cpu_count": "个",
    "cpuusage_ghz": "GHz",
    "expiration_days": "天",
    "free_space_gb": "GB",
    "memusage_gb": "GB",
    "total_space_gb": "GB",
    "uptime_hours": "小时",
    "virtual_machines": "台",
    "vms": "\u53f0",
    "vms_on": "\u53f0",
    "name": None,  # Name text, no unit

    # VM attributes
    "cpu_use_pct": "%",
    "cpu_ready_pct": "%",
    "mem_balloon_mb": "MB",
    "net_dropped_ps": "包/秒",
    "memory_allocated_mb": "MB",
    "memory_used_mb": "MB",
    "memory_active_mb": "MB",
    "used_space_gb": "GB",
    "snapshots": None,  # Count, no unit
    "tools_status": None,  # Status text
    "guest_os": None,  # Text
    "guest_ip": None,  # IP address
    "status": None,  # Status text
    "state": None,  # State text
    "host_name": None,  # Text

    # Host attributes
    "cputotal_ghz": "GHz",
    "memtotal_gb": "GB",
    "version": None,  # Version text
    "build": None,  # Build text
    "maintenance_mode": None,  # Boolean
    "power_policy": None,  # Policy text
    "available_power_policies": None,  # List text
    "shutdown_supported": None,  # Boolean
    "cpu_sockets": "个",
    "cpu_cores": "个",
    "cpu_threads": "个",
    "hardware_vendor": None,
    "hardware_model": None,
    "bios_version": None,
    "bios_date": None,
    "ntp_servers": None,
    "ntp_timezone": None,

    # Datastore attributes
    "connected_hosts": None,  # Count, no unit
    "type": None,  # Type text
}

SUPPORTED_PRODUCTS = ["VMware ESX Server", "VMware VirtualCenter Server"]
AVAILABLE_CMND_VM_POWER = ["on", "off", "reboot", "reset", "shutdown", "suspend"]
AVAILABLE_CMND_VM_SNAP = ["all", "first", "last"]
AVAILABLE_CMND_HOST_POWER = ["shutdown", "reboot"]
HOST = "host"
TARGET_HOST = "target_host"
VM = "vm"
COMMAND = "command"
FORCE = "force"

# ===================== 主机可管理服务（开关 / 启动策略 / 重启按钮）=====================
# 只暴露这三个最常用、且 start/stop/restart/updatePolicy 在 5.5~8.0 上都稳定可用的
#   服务；其余服务（DCUI、snmpd、sfcbd 等）只在「服务状态」诊断传感器里只读展示，
#   避免误停 DCUI / CIM 把主机自己的管理面关掉。
# 结构：ESXi 服务 key -> (URL/unique_id 用的 ascii 短码, 中文显示名, mdi 图标)
MANAGED_SERVICES = {
    "TSM-SSH": ("ssh", "SSH", "mdi:console-network"),
    "TSM": ("shell", "ESXi Shell", "mdi:console"),
    "ntpd": ("ntp", "NTP 守护进程", "mdi:clock-check-outline"),
}

# 主机详情嵌套字段（sensor 平台逐属性建实体时必须跳过，
#   它们由专门的诊断传感器 / binary_sensor / switch 承载）
HOST_INTERNAL_KEYS = (
    "original_name",
    "moid",
    "lockdown",
    "services",
    "health",
    "network",
    "firewall",
    "sessions",
    "autostart",
    "storage",
)
# VM 内部字段（不逐属性建传感器；由专门的二进/开关/诊断实体承载）
VM_INTERNAL_KEYS = (
    "uuid",
    "vm_name",
    "moid",
    "nics",
    "cdroms",
    "question",
    "consolidation_needed",
    "video",
)
# 每台虚拟机都稳定存在的性能计数器键（coordinator 每轮注入，值可能为 None）
VM_PERF_KEYS = ("cpu_ready_pct", "mem_balloon_mb", "net_dropped_ps")
