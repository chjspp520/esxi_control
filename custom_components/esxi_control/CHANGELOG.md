# ESXi Control 集成改动记录

---

# 2026-10-18 改名（集成版本 `0.12.0`）

> **集成版本**：`0.12.0`（`manifest.json` 与 `const.py` 的 `VERSION`）
> **范围**：`manifest.json`、`const.py`、全部 `.py` 的域名/展示名引用、
> `translations/*.json`、`www_console/frontend/console-embed.js`、目录名。

## 1. 改名内容

| 项目 | 原 | 现 |
|---|---|---|
| 域名（`domain`） | `esxi_stats` | `esxi_control` |
| 目录 | `custom_components/esxi_stats` | `custom_components/esxi_control` |
| 集成显示名 | ESXi Stats | **ESXi Control** |
| 维护者（`codeowners`） | `@wxt9861` | `@chjspp520` |
| 文档 / 问题地址 | `github.com/wxt9861/esxi_stats` | `github.com/chjspp520/esxi_control` |

## 2. 域名变化牵连到的东西（都会跟着变）

- **服务名**：`esxi_control.sync` / `esxi_control.refresh` / `esxi_control.guest_*` /
  `esxi_control.vm_*` …（自动化、脚本、前端卡片里写死的 `esxi_stats.xxx` 都要改）
- **事件名**：`esxi_control_synced`、`esxi_control_event`（监听旧事件名的自动化要改）
- **URL 前缀**：`/esxi_control/console`、`/esxi_control/guest`、`/esxi_control/admin`
  （`console-embed.js` 里的路径映射同步改过；旧书签失效）
- **「远程桌面」开关的 unique_id**：`esxi_control_remote_desktop`
  （实体 id 仍是 `switch.esxi_remote_desktop`，卡片引用不用改）
- **实体聚合**：`sensor.esxi_all_entities` **保持不变**（前端卡片 / 概览直接写死了这个 id，
  故意不改，见 `naming.AGGREGATE_ENTITY_ID` 的注释）
- 其它实体的 unique_id 里带的是「主机 IP + entry_id + 对象名」，不含域名，**不受影响**

## 3. `.storage` 一次性迁移（不会丢密码 / 状态）

Store 文件名由 domain 派生，域名一变文件名就变。以下四项在**新键为空**时会回退读旧键
（`esxi_stats...`）并复制到新键：

| 数据 | 旧键 | 新键 |
|---|---|---|
| 来宾账号密码（Tools + SSH） | `esxi_stats_guest_creds` | `esxi_control_guest_creds` |
| 远程桌面开关状态 | `esxi_stats.remote_desktop` | `esxi_control.remote_desktop` |
| 学到的控制台访问地址 | `esxi_stats.console_base_urls` | `esxi_control.console_base_urls` |
| 光盘挂载记录 | `esxi_stats.console_cdrom_mounts` | `esxi_control.console_cdrom_mounts` |

旧文件不会被删除（留作回退），新文件在首次读取后写入。

## 4. 升级步骤（必须按顺序）

1. **停止 Home Assistant**（旧目录里的 .py 还被 Python 打开着，Windows 下会锁住目录）；
2. 删除配置目录下的 `custom_components/esxi_stats` **整个文件夹**（本次只复制出了
   `esxi_control`，旧文件夹仍在，两个目录都声明 `domain: esxi_control` 会让 HA 报重复）；
3. 启动 HA；
4. 「设置 → 设备与服务」里旧的 `ESXi Stats` 条目会显示未加载/未知 —— **删除它**；
5. 重新添加集成（搜索 `ESXi Control`），重填 ESXi 地址 / 账号 / 密码与选项
   （配置条目本身无法跨域名继承，选项会用默认值）；
6. 前端卡片里把 `hass.callService('esxi_stats', ...)` 改成 `esxi_control` 并重新构建。

> 注意：**不要**在旧条目还没删掉的情况下重新添加 —— 旧条目名下的实体还占着
> 那些 entity_id，新实体会被迫加 `_2` 后缀。

---

# 2026-10-18 改动（集成版本 `0.11.0`）

> **集成版本**：`0.11.0`（`manifest.json` 与 `const.py` 的 `VERSION`）
> **范围**：`guest_store.py`（升 v2）、**新增** `guest_router.py` / `ssh_ops.py`、
> `console_web.py`、`__init__.py`、`binary_sensor.py`、`config_flow.py`、
> `const.py`、`manifest.json`、`www_guest/index.html`（整页替换）、
> `www_console/index.html`（文件系统抽屉）、`switch.py`（远程桌面总开关）、
> `sensor.py` + `naming.py`（虚拟机管理传感器）。
> **来源**：从 `esxi_stats-有来宾前端`（同基底分支）移植「系统连接设置 + 双通道」。

## 1. 来宾操作支持双通道（VMware Tools / SSH）

**背景**

原来只有 VMware Tools 一条通道，有三个绕不过去的限制：

- 客户机没装 open-vm-tools / 没跑 vgauthd 就完全用不了（Linux 精简安装很常见）；
- Tools 通道**拿不到 stdout**，所以「执行命令并看输出」根本做不了；
- 注册表只有 Windows 的 Tools 有，Linux 侧没有任何等价物。

**实现**

- **新增 `ssh_ops.py`**：基于 `asyncssh` 的第二条通道。密码认证 + SFTP，
  列进程（`ps -eo` → `ps axo` 自动降级）、列目录、建/删、上传/下载、
  一次性命令（带超时）。全异步，不进 executor。
- **新增 `guest_router.py`**：两条通道的统一入口。同一批 op 名
  （`list_processes` / `list_files` / `list_registry` / `exec` …），由
  `build_target()` 按**每台 VM 的配置**决定走哪条，页面 API、服务、凭据校验共用。
- **`guest_store.py` 升 v2**：记录从扁平 `{username,password,domain}` 变成
  `{transport, tools:{…}, ssh:{…, hostkey}}`。`_migrate()` 在 `async_load()`
  时把 v1 老记录就地搬进 `tools` 并回写 —— 用户无感，不用重填密码。
- 通道判定（`resolved_transport`）：`auto` = 配了 SSH 账号且来宾是 Linux（或没配
  Tools 账号）→ ssh，否则 tools；显式选 `ssh` 但没填用户名会回落 tools，
  避免「选了 SSH 却完全用不了」。

**取舍**

- **注册表仍只在 Windows + VMware Tools 下可用**；SSH 通道调用会返回明确的
  中文错误，而不是 500。
- **`exec` 只在 SSH 通道开放**：Tools 没有 stdout 这个概念。
- `asyncssh` 未安装时**软失败**（`ssh_ops` 里 `asyncssh = None`）：只有 SSH 通道
  报「asyncssh 没装上」，集成照常加载、Tools 通道照常用。

**验证**

离线冒烟（stub HA 的 Store，24 项断言全通过）：v1→v2 迁移、密码保留、
通道判定矩阵（仅 tools / auto+Linux / auto+Windows / 显式 ssh / 无 tools 账号）、
指纹写入与清除、删除配置、后台扫描「关机跳过 / SSH 通道不因 Tools 未运行被跳过」。

## 2. 页面「系统连接设置」弹层（5 个新 op）

**背景**

原来凭据只能在「集成 → 配置 → 来宾凭据」里填，且只能配 Tools 那一套；
配错了要退出去改，改完还得等后台 5 分钟扫描才知道能不能连上。

**实现**

- `console_web.py` 新增 `_CONN_OPS` / `_conn_view()` / `_handle_conn_op()`：
  `get_conn`（读，**永不回传密码**，只回 `has_password`）、`set_conn`（写通道 +
  两套参数，空密码 = 保留原值，保存后立刻强制校验一次）、`test_conn`（用
  **还没保存**的参数试连 SSH）、`forget_hostkey`（清 TOFU 指纹）、
  `remove_conn`（两条通道一起删）。
- 这 5 个 op 在 `_resolve_guest_target()` **之前**分派 —— 所以**没配凭据也能
  打开连接设置**（这正是旧页面「一进来就 409、没法自救」的死结）。
- `GuestApiView` 其余 op 改为 `guest_router.async_run()` 分派；上传/下载视图
  改走 `guest_router.async_upload/async_download`，两条通道共用同一段视图代码。

## 3. 来宾页面整页替换（`www_guest/index.html`）

旧页（29KB）只有进程/文件/注册表三个只读视图，没有入口也没有终端。新页（92KB）：

- 三个选项卡 + **终端**（仅 SSH 通道时显示）；
- 顶栏「凭据徽标」= **系统连接设置**入口（点开选通道 / 填 SSH IP·端口·账号·
  校验指纹 / 填 Tools 账号 / 测试连接 / 清指纹 / 删除配置）；
- 预设管理（常用程序、结束预设、收藏目录、常用注册表键）、上传下载、自动刷新；
- 兼容控制台 iframe 嵌入形态（`#embed` 收顶栏、`postMessage` 同步凭据状态）。

## 4. 控制台改「文件系统」右侧抽屉（控制台不再被卸载）

**背景**

上一版控制台点「来宾」是 `window.location.href` 整页跳走 —— 控制台这张页被浏览器
卸载，画面、票据、WebSocket 会话全没了；看完文件点「← 控制台」回来又是一次
重新申请票据 + 重新握手，中间画面一直黑着。这与「远程控制不中断」的诉求相悖。

**实现**（`www_console/index.html`）

- 工具栏按钮改名「文件系统」，点它改为右侧抽屉：`#guest-panel`（`width:
  min(880px, 96vw)`，比光盘抽屉宽，因为进程/文件/注册表都是多列表格）。
- **半透明**：抽屉底 `rgba(27, 34, 45, .8)`（与光盘抽屉同一层底，不做毛玻璃），
  iframe 背景 `transparent`；来宾页在 `body.embed` 下把自己的 body / 表格面板
  一起透掉，表头保留高不透明度避免文字叠在一起。禁用按钮在面板内改实心暗色
  （半透明底上再叠 opacity 会"透到后面去"）。
- **控制台不中断**：来宾页塞进 `iframe`，抽屉关闭只是 CSS 位移，iframe 不销毁 ——
  再点开还是原来的选项卡 / 筛选词；`src` 懒加载（首次打开才填，不点不连 ESXi）。
- 抽屉与「光盘」互斥（都从右边滑出）；伪全屏（`body.css-fs`）时抽屉 `top: 0`；
  手机上抽屉铺满整屏。
- 标题栏右端那枚**凭据徽标** = 连接设置入口：点它给 iframe 发
  `esxi-guest-open-conn`（iframe 未就绪时先排队，等它发出第一条状态消息再补发）。
  来宾页在嵌入形态收起自己的顶栏，凭据状态通过 `postMessage`（同源 + 校验
  `event.source`）同步到这枚徽标。
- 嵌入靠 url 尾巴的 `#embed`（hash 不进请求行，不影响 authSig 验签）。

**验证**：两个页面的内联脚本 `node --check` 通过；抽屉/半透明/嵌入形态**待实测**。

## 5. 服务层改走 router；`guest_exec` 刻意不注册

- `__init__.py` 的 `guest_services` 表改成 `(服务名, op 名, schema, 参数名)`，
  处理器统一走 `guest_router.async_run`，服务层不再认识具体通道。
- **`guest_exec` 不注册成服务**（只在页面签名 API 里开放）：服务是任何有服务
  权限的人都能调的，等于在客户机里开一个任意命令入口。需要时再加一行即可。

## 6. 全局「远程桌面」总开关（`switch.esxi_remote_desktop`）

**背景**

控制台 / 文件系统面板 / 虚拟机管理页面都是"把一张带签名的钥匙发到浏览器"。
以前没有总闸：只要有人拿到那串 URL（或设备页「访问」按钮还在），就能一直开控制台；
想要临时全部停掉，只能删集成。需要的是**一个开关，关掉立刻断**，并且重启后记住。

**实现**

- `console_web.py` 新增集成级状态（HA 私有 Store `.storage/esxi_stats.remote_desktop`，
  每请求现读，默认开启）：
  `async_load_remote_state` / `remote_desktop_enabled` / `async_set_remote_desktop` /
  `_blocked_response`（页面用文本 403、数据接口用 JSON 403）。
- **关掉时同时做三件事**：① 清空所有 VM 设备与主机设备的 `configuration_url`
  （设备页「访问」按钮消失，旧链接即便还留着也验不过）；
  ② 所有控制台 / 文件系统 / 管理页视图一律 403；
  ③ **`async_close_console_sessions` 逐个 close 正在跑的控制台 WebSocket**
  （code 1001），上游 ESXi 连接随 finally 释放 —— 正在远程控制的画面**立刻断**。
  控制台 WS 在 `prepare()` 后把自己登记进 `console_sessions`，结束时移除。
- 被拦的面：控制台页面 / 票据接口 / 光盘接口（GET/POST/DELETE）/ 控制台 WS /
  来宾页面 / 来宾 API / 来宾上传 / 来宾下载 / 虚拟机管理页面 / 管理清单 / 管理 API。
- `_link_one_entry` 在开关关闭时不再签链接，改为**主动清除**；`_initial_link_loop`
  在关闭状态直接收工（否则会白等 25 轮并刷一条"未能重签"告警）。
- `switch.py` 新增 `ESXiRemoteDesktopSwitch`：整集成**只有一个**（unique_id 不含
  entry_id，固定 `entity_id` 为 `switch.esxi_remote_desktop`，并在
  `async_added_to_hass` 里把旧 id 迁移过来）；挂在宿主条目的主机设备「控制」区。
- `__init__.py` 在平台加载**之前** `await async_load_remote_state(hass)`，
  这样开关首次显示就是落盘值，不会先显示默认再纠正。

**取舍**

- 与截图（`image.*`）**无关**：截图走 ESXi `/screen` → 本地文件 → image 实体，
  不经过控制台，所以截图不跟着开关走。
- 纯 HA 侧策略，不向 ESXi 写任何东西，HA 关机 / 断电不会改变它。

## 7. 主机「虚拟机管理」传感器（地址同样受总开关管辖）

- `sensor.py` 新增 `ESXiHostManagerUrlSensor`（`sensor.<主机>_vm_manager`，
  `naming.py` 新增 `vm_manager_url_{host} → <host>_vm_manager` 规则）：
  - **开**：state = **不带 authSig 的绝对地址**（登录 HA 的浏览器直接可点，且不会
    踩 255 字符状态上限）；`url` 属性给带签名的完整地址、`path` 属性给相对路径。
  - **关**：state = 「已停用」，`url` / `path` 为 `None`（`available` 保持 True，
    以便一眼看出是"被停用"而不是"坏了"）。
  - 数据源与设备页「访问」按钮**同一个字段**（主机设备 `configuration_url`），
    由 `console_web._link_one_entry` 在签名到期 / 访问地址变化时重写，本实体只跟随
    读取 —— 两处永远一致，不会各签一套。

## 8. 其它

- `binary_sensor.py`：「来宾凭据」二进传感器属性新增 `channel`，SSH 通道下改为
  给 `host`/`port`，不再假装有 `domain`。
- `config_flow.py`：选项里的「来宾凭据」表单明确只编辑 **VMware Tools 通道**
  （存储已是 v2，表单只取 `tools` 子字典），提示文案指向页面里的连接设置。
- `manifest.json`：`requirements` 加 `asyncssh>=2.14.0`；`const.REQUIRED_FILES`
  补上 `guest_ops.py` / `guest_store.py` / `guest_router.py` / `ssh_ops.py` /
  `www_guest/index.html`。

## 9. 升级注意

1. **先备份** `.storage/esxi_stats_guest_creds`：结构升到 v2 后，回退到 0.10.x
   会把它当扁平结构读（用户名读空）。
2. `asyncssh` 由 HA 按 manifest 自动安装；离线环境需手工 `pip install asyncssh`，
   否则 SSH 通道报错但集成仍可用。
3. 已验证项之外的**待实测**：重启后旧凭据是否正常迁移、Linux VM 配 SSH 后
   进程/文件/终端、Windows VM 的注册表查询、上传下载在两条通道下的表现。
4. 新增 `switch.esxi_remote_desktop`（默认**开启**，与升级前行为一致）与
   `sensor.<主机>_vm_manager`。关掉开关后设备页「访问」按钮会消失、
   主机页的虚拟机管理入口也一起停用；想恢复就把它打开。

---

# 2026-10-08 改动（集成版本 `0.10.3`）

> **集成版本**：`0.10.3`（`manifest.json` 与 `const.py` 的 `VERSION`）
> **范围**：`sensor.py`、`naming.py`、`const.py`、`manifest.json`、
> `www_console/index.html`（第 8～25 节，纯前端，不改集成版本）。
> **记录方式**：背景（为什么要改）→ 实现（怎么改）→ 取舍 / 验证（怎么确认）。

## 1. 「实体聚合」快照里每个实体带上 `state`

**背景**

聚合快照原先刻意**不带** state（当时的理由：快照值最长滞后一个刷新间隔，容易被拿去做实时判断；
而且属性体积已经贴着 recorder 的 16384 字节上限）。但使用方（卡片 / 概览 / 存档）希望
「一份快照就能看到每个实体的当前值」，并且这个值的更新节奏统一交给
「实体聚合刷新间隔」（`number.aggregate_interval`）控制。

**实现**

- `sensor.py`：`ESXiEntitiesAggregateSensor._collect()` 的条目从
  `{entity_id, name, icon}` 变成 `{entity_id, name, icon, state}`；
  `state` 直接取状态机里的 `hass.states.get(entity_id).state`（字符串，
  `unavailable` / `unknown` 原样保留，不做二次解释）。
- 刷新节拍**不变**：`update()` 仍按 `_aggregate_interval()`
  （`CONF_AGGREGATE_INTERVAL`，默认 60 秒，夹在 10 ~ 3600 之间）节流，
  所以这些 state 值是「每个刷新间隔采一次」；`esxi_stats.sync` 仍走
  `force_refresh()` 立即重建。
- 类 docstring 与 `AGGREGATE_SAFE_BYTES` 的注释同步改写：明确「state 是快照值、
  不是实时值」，并列出体积取舍的三条路（见下）。

**取舍（重要）**

每条约 +20 字节（`"state":"running"` 量级），161 个实体约 +3KB，
`attributes_bytes` 会从约 15900 涨到约 19000 —— **越过 recorder 单条状态
16384 字节的硬上限**：devices 明细不再进历史库，实时读取、卡片、自动化不受影响。
想拿回历史，按性价比排序：

1. 在 `recorder:` 里排除「实体聚合」实体（推荐：它本质是「实体目录」，历史价值低）；
2. 聚合里去掉 `name`（省约 4.8KB，但按短名匹配角色的使用方会失效）；
3. 字段名改短键（`entity_id`→`e` / `name`→`n` / `icon`→`i` / `state`→`s`，省约 2.2KB，
   使用方要跟着改）。

越线时集成会打一条 warning（每个 HA 进程只打一次），文案里直接给出要排除的实体 id。

**验证（待实测）**

未在真实 HA 上跑（需重启后观察）：`sensor.esxi_all_entities` 的
`attributes_bytes`、某个条目的 `state` 是否等于该实体当前值、把
「实体聚合刷新间隔」调小（例如 10 秒）后 state 值的最长滞后是否随之缩短。

## 2. 「实体聚合」的 `entity_id` 固定为 `sensor.esxi_all_entities`

**背景**

聚合实体是卡片 / 自动化的入口，需要一个**永远能写死**的 id。而旧 id 取决于
「短 entity_id」总开关：开着是 `sensor.entities`，关掉则退化成 HA 按中文名生成的
一长串拼音（如 `sensor.esxi_tong_ji_shi_ti_ju_he`），两套 id 都不适合写进配置。

**实现**

- `naming.py`：新增常量 `AGGREGATE_ENTITY_ID = "sensor.esxi_all_entities"`；
  `suggested_entity_id()` 对 `*_entities_aggregate` 直接返回它 ——
  这样「短 entity_id」那一趟规划的目标也是同一个 id，不会出现两套规则来回改名。
  该规则**不受**短 id 总开关影响（与其它规则不同，这里不判断开关）。
- `sensor.py`：`ESXiEntitiesAggregateSensor.__init__` 里 `self.entity_id = AGGREGATE_ENTITY_ID`
  （注册表里还没有这个 unique_id 时直接生效）；`async_added_to_hass` 里调用新增的
  `_async_migrate_entity_id()`。
- `_async_migrate_entity_id()`：HA 的**实体注册表优先于代码里设置的 entity_id**，
  升级前已注册的实体（旧 id 为 `sensor.entities` / 长拼音）必须显式
  `entity_registry.async_update_entity(..., new_entity_id=...)` 迁移一次，
  成功后再回写 `self.entity_id`（同一生命周期内的后续代码 —— 快照排除自身、
  `force_refresh()` 写状态 —— 要立刻拿到新 id）。目标 id 被别的实体占用时保持原样并 warning。
- 这套「`__init__` 设固定 id + `async_added_to_hass` 迁移一次」与
  `shaobo_pocket_carrier` 的 `ForcedEntityIdMixin` 同源（已被验证可用）。
- 兜底文案里的 id 也统一成该常量（recorder 体积告警）。

**升级注意**

- 引用旧 id `sensor.entities` 的卡片 / 自动化 / 模板需要同步改成
  `sensor.esxi_all_entities`（例如 room-elves-card 的 `esxi` 卡片默认实体）。
- 配了多个 ESXi 条目时，这个全局唯一的 id 只有第一个条目能拿到，
  其余条目保持各自原 id 并打 warning（宁可 id 不统一，也不去抢别人的 id）。

**验证（待实测）**

未在真实 HA 上跑（需重启后观察）：重启一次后实体详情页的 entity_id 是否为
`sensor.esxi_all_entities`；重载集成第二次不再改名（迁移幂等）；
关掉「短 entity_id」开关后这一个 id 是否仍保持不变。

## 3. 修复全部来宾操作失效（`name 'vim' is not defined`）

**背景**

点「来宾凭据校验」按钮报错：
`Error during service call to button.press: 来宾凭据校验失败：name 'vim' is not defined`。

**排查**

`guest_ops.py` 的内部工具 `_find_vm(content, moid)`（按 moId 找 VirtualMachine 对象）
用的是**模块级全局** `vim`（`vim.VirtualMachine`），而 `vim` 只在 `_run()` 函数内部做了
一次局部 `from pyVmomi import vim` —— 局部导入只属于 `_run` 作用域，`_find_vm` 里看不到它，
于是每次调用都在「按 moId 找虚拟机」这一步抛 `NameError`；该异常又被 `_run()` 的兜底
`except Exception` 捕获并包成 `GuestOpsError("name 'vim' is not defined")` 返回，
所以界面上看到的是一条「校验失败」而不是崩溃堆栈。

影响面**不止校验**：`_run()` 是所有来宾操作的统一入口（校验 / 进程 / 文件 / 注册表，
11 个服务 + 对应按钮与二进制传感器），因此这些功能此前全部不可用。

**实现**

- `from pyVmomi import vim` 提到模块顶层（与 `esxi.py` / `cdrom_ops.py` 一致，
  而且 `guest_ops` 本来就 `from .esxi import ...`，pyVmomi 早已加载，局部导入没有收益）；
- 删掉 `_run()` 里的局部导入，并留下注释说明为什么必须是模块级导入（防止再被「优化」回去）。

**验证**

`python -m py_compile guest_ops.py` 通过。另外写了一个基于 `symtable` 的
「引用了但任何作用域都没定义、也不是内置」扫描脚本，遍历整个集成目录 **0 命中**
（同一脚本对修复前的形态能准确报出 `_find_vm :: vim`）。运行时待实测：
重启 HA 后点「来宾凭据校验」，Tools 在线的虚拟机应返回真实校验结果（不再报 NameError）。

## 4. 来宾操作第二批修复（`构造来宾认证对象失败：domain` 及其它字段级错误）

**背景**

第 3 条修好后，点「来宾凭据校验」换成新报错：`来宾凭据校验失败：构造来宾认证对象失败：domain`。
（说明 NameError 只是第一层，`guest_ops.py` 这一段代码此前**从来没真正跑通过**。）

**排查**

- 从 pyVmomi 8.0.3.0.1 的类型定义（`_typeinfo_vim.py`）确认：
  `vim.vm.guest.NamePasswordAuthentication` 只有 `interactiveSession` / `username` /
  `password` 三个字段，**没有 `domain`**；多传的 kwarg 会在
  `DataObject.__setattr__ → GetPropertyInfo` 里抛 `AttributeError(name)`，
  而 `str(AttributeError('domain'))` 就是 `domain` —— 所以错误消息只有两个字。
- 顺手用同一份类型定义把该模块的其它 API 用法全核对了一遍，又发现 4 处字段/接口误用
  （都是「读不到值却不报错」的静默 bug）：

| 位置 | 原来（错的） | 实际（vmodl） |
|---|---|---|
| `guest_list_files` 翻页 | `result.remainingIndex` | `result.remaining`（下一页下标，-1 = 结束） |
| `_file_info` 时间/属主 | `info.modification` / `info.owner` | `info.attributes.modificationTime` / `.ownerId`（Posix 才有 ownerId） |
| `guest_list_aliases` | 把返回值当 `GuestAlias[]` 读 `username/subject/mappingCert` | 返回 `GuestAliases[]`：`{base64Cert, aliases:[{subject, comment}]}`，名字在 `subject.name`（NamedSubject） |
| `guest_list_registry` | `record.keyPath` / `record.keyNames` / `record.values` | 键：`record.key.keyName.registryPath`（`record.fault` 标记读不到）；值：`record.name.name` + `record.data`（具体子类才有 `value`） |

**实现**（`guest_ops.py`）

- `_auth()`：不再传 `domain`，按 Windows 惯例把它拼进用户名（`DOMAIN\user`；用户名已写成
  `user@domain` 时不再重复拼），并显式 `interactiveSession=False`；
- `_file_info()` / 翻页字段按上表改为正确来源；
- `guest_list_aliases()` 按 `GuestAliases[]` 结构解析，并多带出 `comment` / 整体 `mapping_cert_present`；
- `guest_list_registry()` 按 `RegistryKeyRecord[]` / `RegistryValue[]` 结构解析：
  子键带 `path` / `wow_bitness` / `class_type` / `last_written` / `error`（读不到的键保留在列表里
  并计数到 `unreadable_keys`，只是不再去查它的值），值的类型由 `RegistryValueData` 具体子类
  映射成 `REG_SZ` / `REG_DWORD` / `REG_QWORD` / `REG_EXPAND_SZ` / `REG_MULTI_SZ` / `REG_BINARY`。

**验证**

把 pyVmomi 8.0.3.0.1 装到临时目录，写了一个**离线单测**（临时脚本）：
伪装包把 `guest_ops.py` 当子模块加载（`.esxi` 换桩），把 `_run` 换成「直接调用 worker」，
再用**真实 pyVmomi 数据对象**喂假 `gom`，覆盖：
`domain=` 旧写法确实抛 `AttributeError('domain')`、域账号/无域账号/UPN 三种用户名拼接、
翻页请求下标 `[0, 1]`、`attributes.modificationTime`/`ownerId`、
别名（NamedSubject + base64Cert）、注册表（键含 fault 的跳过与计数、父键+子键各查一次值、
`REG_SZ`/`REG_DWORD` 识别与取值）—— **22 项断言全过**。
运行时待实测：重启 HA 后点校验应返回真实结果（通过 / 明确的中文失败原因）。

---

## 5. 前端心跳改成带登录态（修 `invalid authentication` 告警与封 IP 风险）

**背景**

日志出现：

```
Login attempt or request with invalid authentication from 192.168.1.34
Requested URL: '/esxi_stats/api/console_origin'
```

**排查**

「地址栏来源心跳」是注入 HA 前端的 `console-embed.js` 打的，用的是**裸 fetch**（无
`Authorization` 头）；而 `ConsoleOriginView` 是 `requires_auth = True` 的视图，
于是每次触发都被 HA 记成一次失败登录。HA 的 ban 模块对失败登录**累加计数**，
攒够阈值会把来源 IP 拉黑 —— 也就是用户可能被自己的 HA 封掉。

**实现**（`www_console/frontend/console-embed.js`）

- 新增 `authFetcher()`：优先 `hass.fetchWithAuth`（HA 前端自带 Bearer 且会刷新令牌），
  退路用 `hass.auth.data.access_token` 自己拼 `Authorization`（两种写法在本机其它
  自定义卡片里已在用）；
- 取不到登录态时**直接不发请求**，并做有限次重试（每 1.5 秒、最多 10 次）——
  本模块由 `add_extra_js_url` 注入，常先于 `hass` 就绪，但绝不无限重试、也绝不裸发；
- 加载标记 `window.__esxiConsoleEmbed` 随版本号更新（便于 F12 确认模块是否生效）。

---

## 6. 修设备注册表弃用接口告警（`async_get_device`）

**背景**

日志两条 frame 告警：`sensor.py:708` 与 `console_web.py:498` 调用
`device_registry.async_get_device`，官方说明 2027.8 起停用，替代品是
`async_get_device_by_identifier`。

**实现**

- `vm_bulk.py` 新增 `device_by_identifier(dev_registry, identifier)`：
  有 `async_get_device_by_identifier` 就用它（本集成的 identifier 已经把配置条目 id
  拼在里面，天然唯一，不需要再传 `config_entry_id`），老版本没有该方法则退回旧接口；
- 两个调用点（`sensor.py` 的远程地址传感器、`console_web.py` 的设备链接）改用它。
  这样既消掉告警，又不会在老版本 HA 上炸。

**验证**

`python -m py_compile` 四个文件全过；全目录 grep 确认已无其它
`async_get_device(identifiers=...)` 调用点（只剩兼容分支里的那一次）。运行时待实测：
重启后日志里不再出现这两条 frame 告警。

---

## 7. 「实体聚合」的属性不再进历史库（recorder）

**背景**

用户提出：这个实体的数据没必要存进 HA 数据库。

**排查（实测本机数据库）**

- `states` 里该实体共 4758 行、**近 24 小时 2256 行**（约每 38 秒一行，跟着快照重建节拍走）；
- 属性**并没有真的入库**：本机 HA 的属性存在 `state_attributes.shared_attrs`
  （`states.attributes` 已是遗留列、恒为 NULL），该实体对应的记录只有 `{}`（2 字节）/
  32 / 63 字节 —— 那 19KB 的 devices 明细一直是"超过 recorder 16384 字节上限被丢弃"；
  代价是每次写入都刷一条 `State attributes ... exceed maximum size of 16384 bytes` 警告；
- 结论：数据库里从来没有那 19KB 的明细，只有一堆"没有属性"的状态行。

**实现**

- `ESXiEntitiesAggregateSensor._unrecorded_attributes = frozenset({MATCH_ALL})`：
  按官方机制**显式**声明所有属性都不记录（官方实体文档：`_unrecorded_attributes:
  frozenset[str]`，**必须是类属性**，实例属性会被忽略；`MATCH_ALL` 可用于一次排除全部，
  且 `device_class` / `state_class` / `unit_of_measurement` / `friendly_name` 不受它影响）。
  效果：属性变成"按设计不记录"，那条溢出警告随之消失。
  实时读取、卡片、自动化读的是状态机里的属性，**不受影响**。
- `AGGREGATE_SAFE_BYTES` 的语义从"recorder 上限告警"改为"**体积体检线**"，
  注释与日志文案同步改写（快照越大 → 每次重建与前端读取越重）。

**没做的（能力边界）**

HA **没有**提供"集成侧声明整个实体不记录"的接口（只有用户侧 `recorder.exclude`）。
要连状态行都不写数据库，只能用户侧加：

```yaml
recorder:
  exclude:
    entities: [sensor.esxi_all_entities]
```

**验证**

`python -m py_compile sensor.py` 通过；类属性写法与 `MATCH_ALL` 语义按官方实体文档核对
（见上）。运行时待实测：重启后实体详情页属性仍在（实时），历史库里不再出现带属性的行；
若同时加了 `recorder.exclude`，该实体在 `states` 里不再新增任何行。

## 8. 控制台页面移动端适配（`www_console/index.html`，纯前端）

**背景**

控制台页面原本按桌面一行工具栏写的：`#toolbar` 固定 `height: 52px`、`#stage` 写死
`top: 52px`。手机上（窄屏）工具栏那一行放不下（标题 + 状态 + 8 个按钮），要么把内容
挤出屏幕、要么换行后被固定定位的 `#stage` 盖住；另外触摸、软键盘、iOS 的页面缩放
都会干扰操作。

**实现**

- **骨架改 flex 纵向**：`body { display: flex; flex-direction: column }`，`#toolbar`
  改 `min-height: 52px`（可换行）、`#stage` 改 `flex: 1 1 auto` + `position: relative`
  （**删掉写死的 `top: 52px`**），`#console` 仍是 `100%`。工具栏真实高度由脚本量出来
  写进 CSS 变量 `--tb-h`（`ResizeObserver` + `resize`/`orientationchange`），
  「光盘」抽屉的 `top` 改用它 —— 换行后不再盖住按钮。
- **移动端媒体查询**（`max-width: 700px` 或 `矮屏 + pointer: coarse`，桌面端不受影响）：
  工具栏可换行 + 按钮紧凑；隐藏 `#title`（名字在悬浮窗标题栏里）与 `.spacer`；
  浮层卡片收小内边距并允许自身滚动；光盘抽屉整屏宽；虚拟键盘按钮收小、
  隐掉"可拖动…"那行说明、`#kbd-text` 字号提到 16px（**iOS 聚焦小于 16px 的输入框会把
  整页放大**）。
- **触摸**：`#console` 加 `touch-action: none` + `user-select: none` +
  `-webkit-touch-callout: none`（触摸事件全部交给 WMKS，它自带 `isTouchDevice` 与
  touchstart/move/end→鼠标的映射）；`viewport` 加 `maximum-scale=1, user-scalable=no`；
  触摸设备上屏蔽控制台区的长按菜单（挂 `#stage` 上做事件委托 —— 重连时 `#console`
  会被整体替换，直接挂它上面会丢）。
- **「伪全屏」退路**：`Fullscreen API` 被拒时（iOS Safari / 部分 App 内嵌 WebView /
  iframe 限制）以前点「全屏」没有任何反应。现在先试原生，约 300ms 后确认没进全屏就
  给 `body` 加 `css-fs` 类：**只隐藏工具栏**（控制台区本来就靠 flex 吃剩余高度，一隐藏
  自动铺满），并显示右下角的浮动「退出全屏」按钮（进伪全屏后工具栏连同按钮一起消失，
  必须留一个退出口）。原生全屏真的起来时会自动撤掉 `css-fs`，两者不叠加。
- **软键盘**：跟随 `visualViewport` 抬高悬浮键盘面板并给它一个跟可视高度一致的高度上限
  （否则软键盘弹出后面板顶端连关闭按钮一起被顶出屏幕且滚不回来）；「发送」后主动
  `blur()`，让系统输入法收起、虚拟键盘重新露出来。用户手动拖过面板后不再自动贴底。
- 页面高度用 `100dvh`（带 `@supports` 回退到 `100%`），避免手机浏览器地址栏伸缩导致
  底部被切掉。

**验证**

`<style>` 花括号 / 圆括号配平、两个内联脚本 `new Function()` 语法检查、`div/button/span/a`
标签配平均通过；改动点用脚本逐条断言（21 项）。**真机待实测**：手机竖屏 / 横屏下工具栏
换行后控制台是否铺满、伪全屏进退、软键盘弹出时悬浮键盘是否仍可用、iOS 上聚焦文字输入框
是否还会放大页面。

## 9. 控制台页面：修「画面下方一排 F1…F12」+ 移动端工具栏字号减小 / 可收起（`www_console/index.html`，纯前端）

**背景**

1. **画面下方一直挂着一排 F1、F2…F12**（PC 与手机都有）。这不是页面自己的 DOM，是
   `vendor/wmks.js` 自己画出来的控件：
   - WMKS 在 `isTouchDevice()`（UA 含 iphone / ipod / ipad / android / bb）为真时，
     按默认选项 `allowMobileExtendedKeypad`、`allowMobileTrackpad`（都是 `true`）
     初始化两个自带面板：扩展键盘（`#ctrlPanePopup`，jQuery-UI 对话框）与触控板
     （`#trackpad`）；
   - 扩展键盘里那条功能键是 `getFunctionKeyHtml()` 生成的 `#fnKeyPad`
     （`class="fnKey-pane-wrapper hide"`），它被 **append 到 `<body>`** 下（不在
     对话框里），隐藏**只靠 `.hide` 这个类**；
   - 这两个面板的样式**全在 `wmks.css` 里**，而 `www_console/vendor/` 只有
     `jquery.js` / `jquery.ui.js` / `wmks.js` 三个 js，没有这个 css（`index.html`
     里也没有任何 `<link>`）。于是 `.hide` 这条规则不存在 → `#fnKeyPad` 现形 →
     页面底部就是那排 F1…F12。
2. 移动端工具栏要占两行（≈66px），手机上画面本来就小；字号还可以再收一档。

**实现**

- **从源头关掉 WMKS 自带的两个移动面板**（`connectWebmks` 里 `WMKS.createWMKS`
  的选项）：`allowMobileExtendedKeypad: false`、`allowMobileTrackpad: false`。
  本页已经有自己的手机键盘（`#kbd-panel`：功能键齐全、还能整串发送文本），WMKS 那套
  是重复的第二套键盘；关掉后这两个面板不再创建。`allowMobileKeyboardInput` 保持默认
  （软键盘输入代理 `#input-proxy` 自带完整内联样式：1px、透明、绝对定位，平时不会露
  出来；留着还能用系统输入法打字）。
- **CSS 再兜一层**：`#fnKeyPad, .fnKey-pane-wrapper, #ctrlPaneWidget, #ctrlPanePopup,
  .ctrl-pane-wrapper, #trackpad, .trackpad-container { display: none !important; }`
  —— 换 WMKS 版本、或那两个选项被改动时，也不会把没样式的控件露在页面上。
- **移动端工具栏字号收一档**：`button.tb` 由 `12.5px / padding 6px 10px` 改为
  `11.5px / padding 5px 9px`；`#status` 由 `12px` 改为 `11px`；工具栏的
  `gap` / `row-gap` 与左右内边距一并收小。
- **新增「收起 / 展开」（移动端专用）**：工具栏最后加一颗 `#btn-tb`（桌面端
  `display: none`，移动端显示）。收起时 `body.tb-collapsed` 让工具栏只剩一行
  （≈31px）：**状态点 + 键盘 + 全屏 + 「展开」**，省下的那一行高度全给控制台。
  - 收起态**必须留「键盘」**：虚拟键盘面板（`#kbd-panel`）的唯一入口就是工具栏这颗
    按钮，藏掉它 = 手机上没法往虚拟机里打字（第一版只留了状态点 + 「展开」，用户立刻
    反馈"本地键盘无法使用"）。「全屏」同理留着 —— 手机上就靠它把工具栏也让出去。
    两颗都是小按钮，同一行放得下，不会多占高度；其余的 光盘 / 适配分辨率 /
    Ctrl+Alt+Del / 重连 / 断开 点一下「展开」再点。
  - 默认：手机上**默认收起**；手动展开过一次就记进 `localStorage`
    （`esxi-console-toolbar-collapsed`），以后按用户的来。隐私模式下 `localStorage`
    读写会抛，忽略即可 —— 只影响"记住"，不影响这一次收起。
  - 只在移动端生效：按钮与收起规则都写在媒体查询里；"手机上收起过、又换成桌面宽窗口"
    时会自动展开，不会留下一个没有展开入口的工具栏。
  - 收起 / 展开会改变工具栏高度：立刻 `syncToolbarHeight()` 更新 `--tb-h`（光盘抽屉的
    `top` 用它），60ms 后补一发 `applyLiveScale()`；`#console` 上的 `ResizeObserver`
    也会照常重算缩放并请求客户机适配分辨率。
  - 媒体查询字符串在 JS（`matchMedia`）与 CSS 里各写一份，**改一处要改两处**。

**取舍**

- 只关掉 WMKS 的这两个移动面板，**不动**它的"触摸 → 鼠标"映射（在 `TouchHandler`
  里，与这两个面板无关），手机上点击 / 拖动操作不受影响。
- 3 指点击原本的作用是开关 WMKS 的软键盘输入代理（`toggleKeyboard`），不是这两个面板，
  关掉面板不影响它。

**验证**

`<style>` 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、
`div/button/span/a` 标签配平均通过；本次改动点用脚本逐条断言（30 项，含"tb-collapsed
规则确实落在媒体查询内"、"移动端字号已降到 11.5px"、"收起态保留了「键盘」按钮"、
"兜底 CSS 没有误伤 `#kbd-panel`"）。
**真机待实测**：手机竖屏打开时工具栏是否默认收起且只剩状态点 + 键盘 + 全屏 + 「展开」、
收起态点「键盘」能正常弹出虚拟键盘并往虚拟机里打字、点「展开」后 8 个按钮是否正常换行、
PC 与手机下方那排 F1…F12 是否都消失、收起 / 展开时画面是否自动重新贴合（无黑边）、
光盘抽屉顶部是否仍对齐工具栏下沿。

## 10. 控制台页面：补「物理键盘桥」（触摸设备 / Chrome 模拟移动端打字无反应）

**背景**

用户反馈：在 Chrome「模拟移动端」下用电脑键盘打字，虚拟机里没有任何反应。

**排查**（读 `vendor/wmks.js`）

- WMKS 的键盘事件绑在**它自己的元素**上：`connectEvents()` 里
  `this.element.bind("keydown.wmks", …)`（`this.element` 就是 `#console`），真正能被点中
  拿到焦点的是里面的 `#mainCanvas`（`_create()` 里 `.prop({ id: "mainCanvas", tabindex: 1 })`）。
  **桌面端**：点一下画面 → canvas 拿到焦点 → `keydown` 冒泡到 `#console` → WMKS 转发。
- **触摸设备**（`WMKS.BROWSER.isTouchDevice()` = UA 含 android / iphone / ipod / ipad；
  **Chrome 的"模拟移动端"把 UA 换成 Android，所以也命中**）走的是另一套输入模型：
  触摸映射成鼠标（`TouchHandler`），文字输入交给**软键盘输入代理** `#input-proxy`
  （`allowMobileKeyboardInput`）：手机上三指点一下唤起系统键盘，按键再由代理转发
  （`onKeyDownSoftKb` / `onKeyPressSoftKb` / `onInputTextSoftKb`）。点画面不会让 canvas
  拿到焦点，WMKS 自己那套 `keydown` 收不到东西 → "模拟移动端 + 电脑键盘"两边都不接，
  打字毫无反应（真机同理，只是真机本来也没有物理键盘，得先三指点出系统键盘）。
- 这与上一节关掉的两个面板**无关**：那两个是 `allowMobileExtendedKeypad` /
  `allowMobileTrackpad`，本次没有改动任何输入通道；本页自带的手机键盘走 WMKS 的 API
  （`sendInputString` / `sendKeyCodes`），不需要焦点，两种模式下都能用。

**实现**（`www_console/index.html`）

- 新增 `bridgePhysicalKeyboard()`：在 `document` 上接 `keydown`，把按键直接交给
  `wmks.sendKeyCodes([code])`（Esc / Tab / Enter / Backspace / Delete / Insert / 方向键 /
  Home / End / PgUp / PgDn / F1..F12）或 `wmks.sendInputString(e.key)`（普通字符，
  含 Shift 出的符号）。两条边界：
  - 事件目标在 `#console` **内部**时不转发 —— 那里 WMKS 自己的 `keydown` 已经在发，
    再发一遍就是一个键打两下（桌面端会立刻暴露）；
  - 带 **Ctrl / Alt / Win** 的组合键不转发，留给浏览器与页面（否则 Ctrl+R 刷新、
    Ctrl+T 新标签会被页面吃掉）；要发组合键用键盘面板的粘滞修饰键
    （点亮 Ctrl 再点 A = Ctrl+A）。输入框 / `contenteditable` 里的按键也不拦。
- 顺带暴露调试句柄 `window.wmks`（`connectWebmks` 里赋值、`destroyWmks` 里清空）：
  出问题时可在 DevTools 控制台直接验证发送通道 —— `wmks.sendInputString("abc")`，
  虚拟机里出现 abc 说明通道正常，问题在别处（没连上 / 焦点 / 面板）。
- 修正上一节注释里的一处口误：三指点一下唤起的是**软键盘输入代理**
  （`allowMobileKeyboardInput`），不是被关掉的扩展键盘。

**取舍**

- 桥是 `document` 级监听，但**桌面端不会双发**：WMKS 只在它的元素有焦点时才收到按键，
  而那种情况下事件目标就在 `#console` 里，被桥跳过。
- 桥**不**主动 focus `#input-proxy`（不会在手机上弹出系统键盘），三指点一下那条原生
  通道原样保留 —— 比"自动 focus 代理"的手机端体验更稳。

**验证**

`<style>` 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、
`div/button/span/a` 标签配平、本次改动点逐条断言（28 项）全通过。**实测待确认**：
模拟移动端里用电脑键盘打字能进虚拟机；桌面端（点一下画面后）打字**没有**双字符；
`Ctrl+R` / `Ctrl+T` 等浏览器快捷键仍然生效。

## 11. 控制台页面：移动端工具栏改「悬浮菜单」+ 画面内屏蔽浏览器右键菜单

**背景**

1. 用户反馈：移动端工具栏**还是占高度**（上一版"收起"后那条窄条也占 ≈31px）。
   想要的形态：屏幕上只浮一颗半透明小按钮，点开后菜单**也浮在画面上**，
   布局高度完全让给虚拟机画面。
2. 用户反馈：在画面里点右键，弹出的是**浏览器**右键菜单，盖住了虚拟机内部自己的
   右键菜单（客户机里的那个才是要用的）。原来只在"指针是粗的"（触摸设备）时屏蔽、
   桌面端故意留着浏览器菜单 —— 实测桌面端同样会盖住。

**实现**（`www_console/index.html`）

- **移动端工具栏 = 悬浮菜单**（全部写在移动端媒体查询里，桌面端一行不变）：
  - `#toolbar` 改 `position: fixed`（顶部 / 左右各 8px + 安全区、`z-index: 60`、圆角 +
    阴影 + `max-height: calc(100dvh - 70px)` 自身可滚）。**fixed 不参与 flex 布局**，
    所以 `#stage`（`flex: 1`）始终铺满整屏 —— 打开 / 关闭菜单都**不改变画面尺寸**，
    也就不需要重新贴合缩放（这是和上一版"收起工具栏"最大的区别）。
  - `body:not(.tb-open) #toolbar { display: none }`：菜单关着时整块不渲染。
  - 新增 `#btn-tb`：右上角 38px 半透明圆钮（背景 `rgba(27,34,45,.55)` + 细边框 +
    `backdrop-filter: blur(6px)`），☰ / ✕ 切换，`z-index: 61`。**它必须是 `#toolbar`
    的兄弟节点**：放里面会跟着 `display: none` 一起消失，那就再也打不开菜单了。
  - **小圆钮可以拖**（挡着画面就拖走，拖动位置记在 localStorage）：
    - 指针事件 + `setPointerCapture`（鼠标 / 触摸一套代码，指针拖出按钮范围也不丢），
      CSS 上 `touch-action: none`（否则触摸拖动会被浏览器当成页面滚动）+ `cursor: grab`；
    - **轻点 = 开 / 关菜单，拖动 = 只挪位置**：用 5px 位移阈值区分，超过阈值就标
      `menuBtnMoved`，那次 `click` 只当"把球放下"（不开关菜单）；阈值内不动就照旧走
      `click`，所以按钮仍然点得动；
    - 位置存 `localStorage`（键 `esxi-console-menu-btn-pos`，值 `"left,top"`，
      **视口坐标**）；旋屏 / 改窗口后按当前视口再夹一次（边距 6px），不会跑到屏幕外；
    - 按钮的 CSS 原本是 `right/top` 定位，一旦拖动就换成 `left/top` 并把 `right` 置空。
  - 新增 `#menu-scrim`：菜单开着时的淡遮罩（`rgba(0,0,0,.14)`、`z-index: 44`）。
    点它 = 收起菜单，且那一下被吞掉 —— 否则"关菜单"的点击会顺带落进虚拟机里
    （可能正好点到客户机系统里的按钮）。44 是刻意的：**高于画面**（挡住触摸漏进客户机），
    **低于**虚拟键盘面板(50) / 光盘抽屉(45) / 菜单(60) / 小钮(61) ——
    菜单开着时那几个浮层照样能用。
  - `#overlay`（加载 / 错误卡片）提到 `z-index: 58`：比遮罩高、比菜单低，
    卡片上的「重试」按钮不会被遮罩吞掉。
  - 移动端 `#cdrom-panel` 的 `top` 由 `var(--tb-h)` 改为 `0`（工具栏不再占位）；
    `#fit-hint` 下移到 52px 处，避开右上角小钮。
  - 伪全屏（`body.css-fs`）里连小钮、遮罩一起藏（`display: none !important`），
    并在进入伪全屏时 `setMenuOpen(false)` 复位，免得退出全屏时菜单"自己弹回来"。
- **脚本**（`setMenuOpen(open, remember)` + `applyMenuForLayout()`）：
  - 手机上默认只有小钮；点了菜单里任一按钮自动收起（悬浮菜单就是个"启动器"）；
  - 选择记 `localStorage`（键 `esxi-console-menu-open`，**不与旧的
    `esxi-console-toolbar-collapsed` 共用** —— 语义相反的历史值会被误读）；
    隐私模式读写抛异常只影响"记住"；
  - 桌面端强制关闭、小钮 `display: none`，切回桌面宽窗口不会留下"打不开的工具栏"；
  - 媒体查询字符串在 JS（`matchMedia`）与 CSS 各一份，**改一处要改两处**。
- **画面内屏蔽浏览器右键菜单**：`blockBrowserContextMenu()` 在 `#stage` 上做事件委托，
  目标是 `#console` 内部就一律 `preventDefault()`（去掉原来 `(pointer: coarse)` 的开关）。
  只拦画面内部：工具栏 / 悬浮菜单 / 面板等页面自己的地方照旧保留浏览器菜单。
  挂 `#stage` 而不是 `#console`：重连时 `#console` 会被整体替换，直接挂它会丢监听。
  WMKS 自己的右键转发不受影响 —— 它走的是 mousedown / mouseup（button = 2），
  与 `contextmenu` 无关，所以客户机里的右键菜单照常出现。

**取舍**

- 上一版"收起后保留状态点 + 键盘 + 全屏"整条思路**删掉**（那是"少占一行"的方案），
  换成完全不占高度的悬浮菜单；`body.tb-collapsed` 与
  `esxi-console-toolbar-collapsed` 不再使用（老值留在 localStorage 里也无副作用）。
- 开 / 关菜单**不**重新请求客户机分辨率：悬浮层不改变画面尺寸，请求了反而会让
  客户机白切一次分辨率。

**验证**

`<style>` 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、
`div/button/span/a` 标签配平、本次改动点逐条断言（40 项，含"5px 阈值区分点 / 拖"、
"拖动写 left/top 并置空 right"、"位置夹进可视区"、"旋屏后重新夹"）全通过。
**实测待确认**：手机竖屏时画面铺满整屏（工具栏不再占高度）、右上角半透明小钮点开后
菜单悬浮且可操作（键盘 / 光盘 / 全屏…）、点画面任意处收起且虚拟机里没有多余点击、
**小圆钮能拖动且不误触发开关菜单、刷新后还在拖到的位置**、画面内右键不再弹浏览器菜单
（桌面端同样）、桌面端布局与之前完全一致。

## 12. 控制台页面：移动端新增「上下」布局（上面画面、下面键盘）

**背景**

移动端默认那套虚拟键盘是**悬浮**在画面上的（可拖动），打长命令 / 看输出时总被挡一块；
用户希望能把键盘放到画面**下面**，两者各占一块、互不遮挡。

**实现**（`www_console/index.html`）

- 工具栏加一颗 `#btn-split`（「上下」，紧跟「键盘」，只在移动端显示）。
- 布局本体只有一件事：`body.kbd-split #kbd-panel { position: relative }` —— 面板从
  "fixed 悬浮"回到**文档流**；而 body 本来就是 `display: flex; flex-direction: column`，
  于是 `#stage`（`flex: 1 1 auto`）自动让出下面那块给面板，**两者永不重叠**。
  面板用 `flex: 0 0 auto; height: var(--kbd-h, 42dvh)`；`--kbd-h` 由脚本维护
  （切换布局时写入、拖动时实时改、退出布局时移除 → 回到 CSS 默认的 42dvh）。
- **拖标题栏 = 调分割比例**：`#kbd-head` 的拖动在「上下」布局下走另一条分支
  （`pointermove.kbdsplit`）—— 往上拖键盘变高，往下拖变矮；高度夹在
  `[140px, 屏高 × 0.75]`，拖动中 `requestAnimationFrame(applyLiveScale)` 实时贴合，
  松手才写 localStorage。标题栏在「上下」布局下光标改成 `row-resize`。
- **切换时的清理（关键）**：进入「上下」必须先清掉悬浮遗留的内联定位
  （`left / top / right / bottom / transform`）—— 面板改成 `position: relative` 后，
  内联 `left / top` 会继续把它顶偏；`bottom` 与 `maxHeight` 是"跟着系统输入法抬升"
  用的，也一并清掉、交回 `followSoftKeyboard` 按需重写。于是系统输入法弹出时面板
  仍会被抬进可视区（`position: relative` + 内联 `bottom` = 视觉上移、**不影响布局**，
  画面区尺寸不变）。
- 开关与分割高度都记 localStorage（`esxi-console-kbd-split` /
  `esxi-console-kbd-split-h`）。**只在移动端提供**：桌面端按钮 `display: none`，
  切回桌面宽窗口时 `applySplitForLayout()` 强制关掉，不会留下"分了栏却没有按钮"的状态。
- 「上下」= 画面 + 键盘这一套，所以点它会自动把键盘面板打开（`toggle(true)`）；
  之后单独点「键盘」仍可收起面板（布局偏好保留，下次打开还在）。

**取舍**

- 分割比例不做双指缩放之类的花哨交互，就一个标题栏拖拽；下限 140px（再矮按不动）、
  上限屏高 75%（给画面留四分之一）。
- 桌面端不提供该布局：桌面有物理键盘，悬浮小面板更合适。

**验证**

`<style>` 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、
`div/button/span/a` 标签配平、本次改动点逐条断言（48 项，含"面板回到文档流且
`flex: 0 0 auto`""切换时清内联定位""拖标题栏改分割比例""高度夹取""旋屏后重新夹"）
全通过。**实测待确认**：手机上点「上下」后画面与键盘上下分栏、互不遮挡；拖标题栏能调
比例且画面实时跟随；刷新后保持上次的布局与比例；系统输入法弹出时"发送"行仍可点；
点「键盘」收起面板后画面恢复整屏；桌面端不出现「上下」按钮。

## 13. 控制台页面：虚拟键盘的「功能键行」改成自动换行（多行网格）

**背景**

功能键行（Esc / F1..F12 / Ins / Del / Home / PgUp / PgDn / End，共 19 个）原来是
**一行 + 横向滚动**（`.kbd-row.func { overflow-x: auto }` + 固定最小宽度）。手机上十几个
键挤在一行里，得左右滚动找键，按着别扭。用户希望它按宽度自动折行。

**实现**（`www_console/index.html`，纯 CSS）

- `.kbd-row.func` 由 flex 行改成**网格**：
  `display: grid; grid-template-columns: repeat(auto-fill, minmax(44px, 1fr)); gap: 5px;`
  —— 一行能放几个放几个，**所有键等宽**；`auto-fill` 的列是整块算的，所以每行键宽一致，
  也不会出现"最后一行那几个键被拉得很宽"的参差（最后一行不满就空在右侧）。
- 去掉 `overflow-x: auto`（不再需要横向滚动）。
- 功能键内边距单独收窄（`8px 4px`；手机 `8px 2px`），保证 `F1` / `PgDn` 这类短标签在
  38~44px 的列里排得下。
- 手机媒体查询里补
  `.kbd-row.func { grid-template-columns: repeat(auto-fill, minmax(38px, 1fr)); gap: 4px; }`
  —— ⚠️ **gap 必须在这里重写**：基础样式里的 `.kbd-row.func` 是双类选择器，
  单类的 `.kbd-row { gap: 4px; }` 覆盖不了它。
- 其它键行（数字 / 字母 / 底部修饰键行）仍是 `display: flex`：它们的键数本来就是按
  排布设计好的，不该折行。

**取舍**

- 折行后键盘整体高 1~2 行（桌面约 2 行、手机约 3 行）：面板本来就有 `max-height` +
  `overflow-y: auto`，超高时在面板内滚动；「上下」布局下把分割条往上拖一点即可看全。
- 不做"两行固定、每行 10 个"这种硬编码：列数跟着宽度走，横竖屏 / 分屏都不用改。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、`div/button/span/a`
标签配平、本次改动与回归点逐条断言（28 项）全通过。**实测待确认**：手机上功能键折成
多行、每行按键数随宽度变化、键宽一致、按下去能正常发键；横屏与「上下」布局下同样正常。

## 14. 控制台页面：工具栏增加「复制地址」+ 轻量提示条

**背景**

控制台页面地址本身是集成签好名的 URL（带 `authSig`，24 小时有效），**可以顶层导航直接
打开**（同一套机制见 `console_web.py` docstring）。用户希望能在工具栏里一键把它复制走，
拿到手机 / 别的浏览器 / 发给别人用。页面上原来没有任何"复制"入口，也缺少"一次性反馈"
的 UI（只有会盖住画面的卡片与状态点）。

**实现**（`www_console/index.html`）

- 工具栏末尾加 `#btn-copy`（「复制地址」，桌面端与移动端都显示）。
- `copyConsoleUrl()`：复制 `window.location.href`（本页地址）。
  - **优先 `navigator.clipboard.writeText`**；**但它在非安全上下文（`http://` 访问 HA，
    局域网很常见）下根本不存在** —— 所以必须带回退：临时 `textarea` + `document
    .execCommand("copy")`（老而稳，http 下也能用）。Clipboard API **被拒**（Promise
    reject，例如 iframe 的权限策略）时同样回退，所以两条路都覆盖了。
  - iOS 上只读 `textarea` 光 `select()` 不足以选中，补 `setSelectionRange(0, len)`。
  - 复制完立刻删掉临时节点；拿不到地址 / 两条路都失败都会给提示。
- 新增轻量提示条 `#toast`（`role="status" aria-live="polite"`，`z-index: 70` 高于悬浮
  菜单与悬浮球、`pointer-events: none` 不挡操作）：底部居中、1.8s 自动淡出，重复调用
  会重置计时。`showToast(msg, isErr)` 备着后面别的一次性反馈用。
  - ⚠️ 为什么不用"改按钮文字"当反馈：移动端点完菜单里任何一个按钮，菜单就自动收起了
    （见第 11 节），按钮文字的改动会被一起藏掉；弹卡片又太重。

**取舍**

- 复制的是**本页地址**而不是后端另签一个新地址：本页地址就是集成签好的那个 URL
  （24h 有效、刷新即用），再签一份没有意义；ESXi 凭据始终留在服务端，地址里只有 `authSig`。
- 提示条是"轻反馈"，所以不复用 `#fit-hint` / 卡片，避免挡住画面。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、`div/button/span/a`
标签配平、本次改动与回归点逐条断言（32 项，含"Clipboard 不可用时走 execCommand 回退"
"Clipboard 被拒也会回退""临时节点用完即删""#toast 高于悬浮菜单且不挡点击"）全通过。
**实测待确认**：https 与 http 两种访问方式下点「复制地址」都能拿到可用地址、粘贴到新
标签页能直接打开这台虚拟机的控制台、移动端菜单收起后仍能看到"已复制"提示。

## 15. 控制台页面：虚拟键盘标题栏加「透明度」滑块

**背景**

移动端虚拟键盘悬浮在画面上（或「上下」布局下占下半屏），全不透明时把画面挡得比较严实；
用户希望能随手把它调透一点，看清后面的画面。

**实现**（`www_console/index.html`）

- `#kbd-head` 右侧加一组「透明 + range」（`#kbd-alpha`，`min=40 max=100 step=5`），
  外面包一层 `.kbd-nodrag`。
- **透的是"底色"，不是整块**：值写进 CSS 变量 `--kbd-alpha`（0.4 ~ 1.0），只作用在
  面板底 `rgba(27,34,45,…)`、键帽底 `rgba(32,42,56,…)`、输入框底 `rgba(17,21,28,…)`；
  文字 / 边框 / 标题栏保持不透明。理由：整块用 `opacity` 时，拉到 0.4 连"调回去用的
  那颗滑块"自己都看不清了，键帽文字也会糊在画面里更难认。
  - ⚠️ 键帽那条写的是 `#kbd-panel .k:not(.mod-on):not(:active)`：id 选择器优先级高于
    `.k:active` / `.k.mod-on`（各两个类），不排除的话"按下变蓝""点亮修饰键"这两个
    状态反馈会被半透明底色盖掉；同理补了一条
    `#kbd-panel button.tb:hover:not(:disabled)` 把面板里按钮的 hover 态补回来。
- **触摸拖动不用浏览器原生那套**：标题栏为了"按住挪面板"带 `touch-action: none`，
  而 range 的原生拖动正是浏览器默认手势 —— 手机上容易按不动（不同内核表现还不一致）。
  所以 `dragAlphaSlider()` 自己按指针坐标算数值：`pointerdown` 定位 + `pointermove`
  实时更新 + `setPointerCapture`（拖出滑块也不丢），松手才写 localStorage；原生
  `input` / `change` 仍留着，服务键盘方向键调值。
- 标题栏的拖动把手（挪面板 / 「上下」布局里调分割比例）会跳过 `.kbd-nodrag`：
  在滑块上按住是"调数值"，在别处按住才是"挪面板"。
- 标签用 `<span class="alpha-lab">` 而不是 `<label for>`：range 上点 label 会被浏览器
  当成"在轨道上点了一下"，可能把数值一下拽到底；顺带也不会被移动端那条
  `#kbd-head > span:not(.ttl) { display: none }` 命中（它不是直接子元素）。
- 值存 localStorage（`esxi-console-kbd-alpha`），启动时恢复。

**取舍**

- 只调"虚拟键盘面板"的透明度，不动光盘抽屉与悬浮菜单。
- 下限 40%：再透就基本看不清键了；上限 100%（默认不透明，行为同以前）。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本次改动与
回归点逐条断言（40 项，含"键帽规则排除了 `:active` / `.mod-on`""滑块自己按坐标算值"
"标题栏把手排除 `.kbd-nodrag`""`#toast` / 复制地址 / 上下布局 / 功能键网格仍在"）全通过。
**实测待确认**：手机上拖滑块能实时变透、按下的键仍然变蓝、点亮 Ctrl 仍高亮、
刷新后透明度保持不变、拖动面板与调数值互不干扰。

## 16. 控制台页面：新增「虚拟鼠标」悬浮面板（手机上的左/右键、滚轮、方向键）

**背景**

先讨论过"接管 WMKS 的触摸手势"来做双击 / 长按右键 / 立即拖动，但那条路要自己扛坐标
换算、还要防与 WMKS 内部状态打架。用户提出照**向日葵远程控制**的做法：给一个悬浮的
虚拟鼠标面板 —— 风险为零，且能覆盖手机缺的那几样能力。

现状（读 `vendor/wmks.js` 得到的事实）：

- WMKS 的 `TouchHandler` 只提供"轻点 = 单击""**两指点一下 = 右键**""按住 **300ms** 再拖
  = 拖动""两指滑 = 滚轮"；**双击只发一次单击**（`tap_twice` 分支只 down+up 一次，
  另外给一个写着 double 的视觉反馈）、**没有长按右键**。
- WMKS 公开 API 里**没有发鼠标事件的方法**（只有 `sendInputString` / `sendKeyCodes` /
  全屏 / 分辨率这些），只能用它的内部件。

**实现**（`www_console/index.html`，只在移动端提供）

- 工具栏加 `#btn-mouse`（「鼠标」，紧跟「上下」）；悬浮面板 `#mouse-panel`（可拖动、
  默认右下角、`z-index: 52` 压在虚拟键盘面板(50)之上；伪全屏里跟工具栏一起藏）。
- 面板内容就是向日葵那套：
  - **方向键**（3×3 网格，中间一个点表示指针处）：挪**客户机指针**（= 悬停，触摸屏
    做不到这件事），步长按客户机画面短边的 5%（夹在 6–80px），结果再夹进画面；
    **按住连发**（420ms 后每 90ms 一次）；
  - **滚轮 上 / 下**：`sendScrollMessage`，同样按住连发；
  - **左键 / 右键 / 双击**：都在**当前指针处**发"按下 + 抬起"；双击就是两次快速
    按下抬起 —— 客户机按"时间 + 位置"识别双击，比 WMKS 那个只发一次单击的
    "双击反馈"真实。
- 指针位置取自 WMKS 自己维护的 `wmksData._mousePosGuest`（任何一次点击 / 移动都会更新）
  —— 于是"先在画面上点一下大致位置，再用方向键微调"是通的；从没点过（值为 0,0）时
  先落到画面中央，免得一上来就点左上角。
- **安全守卫**：用到的内部件 `getEventPosition` / `sendMouseButtonMessage` /
  `sendMouseMoveMessage`（滚轮另加 `sendScrollMessage`）**缺任一个就整体停用**，
  点按钮只弹一句"控制台还没连上，虚拟鼠标暂时不能用"，绝不乱发鼠标事件。
- 桌面端不提供（按钮与面板都只出现在移动端布局里；窗口拉宽时自动收起）。

**取舍 / 待定**

- 面板给的是"离散点击 + 指针微调"，**拖动**（按住左键移动）没做成按钮 —— 因为触摸侧
  本来就有 WMKS 的"按住 300ms 再拖"。若需要，可以在面板里加一颗「拖」切换键
  （按下 = 左键按住，方向键移动即拖动，再点一次 = 抬起），代价是要防"忘记抬起"
  （面板关闭 / 页面隐藏时自动抬起）—— **待用户确认**。
- 不替换 WMKS 的触摸手势，两者并存：轻点、两指点右键、长按再拖照旧；
  虚拟鼠标只补"悬停 / 右键 / 双击 / 滚轮"这几样。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本次改动与
回归点逐条断言（56 项，含"9 个动作按钮齐全""三个内部件的守卫""双击 = 两次按下抬起"
"指针夹在画面内""只有方向 / 滚轮连发""未连接时只提示不发事件"）全通过。
**实测待确认**：面板挪指针是否跟随、左右键 / 双击落点是否在指针处、滚轮方向、
按住连发是否顺手、与触摸手势并存是否互相干扰。

## 17. 控制台页面：虚拟鼠标面板 UI 玻璃化 + 「中心即指针」（点 = 左键 / 拖 = 挪指针）

**背景**

用户反馈两点：① 面板太"实"了，想要**基本上透明**（它一直浮在画面上，挡得越少越好）；
② "这个本身就是鼠标指针" —— 中间那颗不该只是个装饰点。

**实现**（`www_console/index.html`，仍是纯前端，不改集成版本）

- **玻璃化 HUD**：面板底改成变量 `--mp-bg: rgba(14,19,26,.32)`（32% 黑）+
  `backdrop-filter: blur(9px) saturate(130%)`；面板里的按钮统一成"8% 白底 + 16% 白边 +
  圆角 10px + 内高光"，文字/箭头加 `text-shadow`，透出去也读得清；边框与阴影都换成
  半透明白，不再用实色 `--line`。
- **按下反馈 + "忙碌态"**：因为 `pointerdown` 被 `preventDefault()`（防滚动 / 长按选择），
  `:active` 不一定触发 —— 改成脚本挂 `.mp-press`（按钮变蓝）与 `.mp-busy`（面板底提到
  58%）。抬手 / 取消统一清掉（监听挂在 `document` 上，手指滑出面板也收得住）。
- **中心那颗就是指针**：`<span class="mp-dot">` 换成 `<button class="mp-btn mp-dot">` +
  内联 SVG（手画的经典指针箭头路径），圆形底、`fill: rgba(255,255,255,.92)` + 投影。
  行为分两种（**不走 `[data-mp]` 委托**，单独一套逻辑，见 `dotPointer()`）：
  - **轻点**（位移 < 6px）= 在指针处**左键单击**；
  - **按住拖动** = **挪指针**：相对位移 × `DOT_SENS 3.2` × （画布→客户机的比例），
    每一步都从 WMKS 记的当前指针位置累加、结果夹在画面内 —— 相当于一个"会自己动的
    迷你触控板"，比方向键一格一格挪顺手得多。
  - 用 `setPointerCapture`：手指拖出面板也不会丢。
- 尺寸 / 间距微调（面板 196px、方向键 38px、`gap` 4px），整体更紧凑。

**取舍**

- 指针移动倍数取 3.2：面板那格只有 38px，1:1 挪一格只能动 38 个客户机像素，太钝；
  3.2 倍下拖半屏就够横跨画面（结果本来就夹在画面内，不怕拖过头）。
- 中心仍然是"左键"语义，没做成右键 / 中键 —— 右键有专门那颗键，语义更清楚。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本次改动与
回归点逐条断言（43 项，含"面板底是低不透明变量""玻璃模糊 + 白边""mp-busy 提不透明"
"中心是指针 SVG 且不带 data-mp""点 = 左键 / 拖 = 挪指针 / 6px 阈值区分""结果夹在画面内"）
全通过。**实测待确认**：面板在亮 / 暗画面上是否都看得清、按下反馈是否跟手、
拖中心挪指针的手感（3.2 倍是否合适）、点中心是否稳定判为单击而不是拖动。

## 18. 控制台页面：虚拟鼠标改成「**能拖的指针**」（拖 = 移动鼠标本身，点 = 左键）

**背景**

第 16 / 17 节做的是"面板 + 方向键 + 按钮"，用户看过后明确：**要的不是面板** ——
要的是**显示一个鼠标指针**，用它可以精确点击；**拖它时就是在移动鼠标本身**；
而且面板（连同按钮）要**高透明**，否则看不见下面的内容。

**实现**（`www_console/index.html`）

- **形态换成"指针 + 一条很淡的按钮条"**：`#mp-cursor`（SVG 指针，26×37）下面跟
  `#mp-btns`（左键 / 右键 / 双击 / 滚▲ / 滚▼ / ×）。原来的 `#mp-head` 标题栏、
  `.mp-pad` 方向键盘、`.mp-dot` 中心点**全部删掉** —— 拖动指针就是移动它，不再需要方向键。
- **绝对映射**（"像真的在拖鼠标"的关键，不是相对位移那种越拖越偏）：
  - 屏幕 → 客户机：`guestFromScreen()` 用 canvas 的 `getBoundingClientRect()` 换算
    （把本页的 CSS 适配缩放也算进去），结果由 `clampGuest()` 夹在画面内；
  - 客户机 → 屏幕：`screenFromGuest()`，用来把小部件摆到"指针现在的位置"；
  - **`#mp-cursor` 里 SVG 的 `viewBox="-0.5 -0.5 14 20"` + `preserveAspectRatio="xMinYMin meet"`
    是特意这么写的**：让"笔尖"正好落在元素左上角，于是 `getBoundingClientRect().left/top`
    就是指针坐标，不必再补偏移。
- **交互**：按住指针拖动 → 小部件跟着手指走，并**每帧（rAF 节流）**把对应位置的
  `sendMouseMoveMessage` 发出去 —— 客户机指针与小部件 1:1 同步；位移 < 6px 当作"点"，
  抬手时在该处发一次左键单击（`oneClick("left")`）。拖动时按钮条自动淡出（`.mp-hide`），
  免得挡住要精确点的目标。
- **它一直显示指针在哪**：打开时先摆到 WMKS 记的当前指针位置
  （`wmksData._mousePosGuest`），并且**对齐一次**（小部件在哪 = 指针就在哪，避免"从没点过
  画面"时小部件在中间、一点却点到左上角）；之后每 400ms 同步一次位置 —— 用触摸手势在画面上
  点过之后，小部件也会跟过去。
- **高透明、且不做背景模糊**：按钮条 22% 黑、按钮 14% 白底 + 16% 白边、指针 72% 白 +
  深色细描边 + 一点投影；**去掉了 `backdrop-filter`** —— 模糊会把下面的内容糊掉，
  正好违背"要看得见下面"的诉求。
- 容器 `pointer-events: none` + 子元素 `auto`：只有指针与按钮条吃触摸，其余地方不挡。

**取舍**

- 不再提供"按住连发"的方向键（拖动取代了它）；滚轮仍可按住连发。
- 指针按下时自己变蓝（`#mp-cursor.mp-press svg`）而不是靠 `:active` —— `pointerdown` 被
  `preventDefault()` 掉之后 `:active` 不一定触发。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本次改动与
回归点逐条断言（41 项，含"指针笔尖落在 viewBox 原点""无 backdrop-filter""绝对映射两个
方向都在""rAF 节流发指针位置""点 / 拖 6px 阈值""打开时先摆位再对齐""旧面板元素已移除"）
全通过。**实测待确认**：拖指针时客户机指针是否 1:1 跟随、点指针是否精确落在笔尖处、
按钮条透明度是否够透又不至于看不清、按钮条淡出时机是否合适。

## 19. 控制台页面：虚拟鼠标「拖动 = 移动鼠标本身」做扎实（抓取区 / 全局监听 / 层级）

**背景**

用户反馈：虚拟鼠标"要能拖动，拖动时就是移动鼠标本身"。第 18 节已经有拖动逻辑，但在真机 /
模拟器上有几处容易变成"抓不住 / 拖一半丢"，这一轮把它做扎实。

**实现**（`www_console/index.html`）

- **抓取区放大**：`#mp-cursor` 元素由 26×37 改成 **34×46**（图形仍是 26×37，贴左上角画）——
  "手指有肉"，26px 宽的抓取区太难点到。图形贴左上角这一点不能动：**"笔尖 = 元素左上角"
  是 `guestFromScreen` 映射的前提**。
- **移动 / 抬起监听改挂 `document`**（`pointermove.mp-drag` /
  `pointerup.mp-drag pointercancel.mp-drag`）：之前挂在指针元素上，手指（或鼠标）拖出
  小部件范围就可能收不到。`setPointerCapture` 保留 —— 能加就加，加不上也不影响。
- **抓手不只是图形本身**：在 `#mouse-panel` 上做 pointerdown 委托，目标是按钮就跳过
  （`closest("button") && !closest("#mp-cursor")`），于是**指针图形 + 按钮条的空隙**都能拖。
- **层级提到 62**：比悬浮工具栏(60)、☰ 悬浮球(61) 都高 —— 指针拖到屏幕顶部 / 右上角时
  不会被它们盖住而"抓不回来"（自己永远在最上层，就永远拖得动）；toast(70) 仍在它上面。
- **首次打开给一句用法提示**：`showToast("拖动指针 = 移动鼠标本身；点它一下 = 左键单击")`
  —— 它长得像指针，"能拖"这件事不看提示不一定想得到（每次打开页面只说一次）。
- 拖动顺序也顺了一遍：先 `placeWidget()` 把面板放到手指处，**再**读
  `panel.getBoundingClientRect()` 当"笔尖坐标"发出去（用面板 rect，而不是图形按钮的
  rect —— 与抓取偏移的口径一致）。

**取舍**

- 抬起时位移 < 6px 仍判为"点"（左键单击），> 6px 才算拖动：两个手势共用一个抓手，
  用阈值区分。拖动中按钮条淡出（`.mp-hide`），不挡要瞄的目标。
- 抓取区放大意味着"指针图形右侧 / 下方那圈空白"也会吃掉触摸（不会点穿到虚拟机）。
  34×46 是在"好点"与"少挡"之间的折中。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本次改动与
回归点逐条断言（36 项，含"z-index 62""抓取区 34×46 且图形仍 26×37""移动 / 抬起挂
document""抓手跳过按钮""按面板 rect 算笔尖""rAF 节流""首次提示"）全通过。
**实测待确认**：手机上（或 Chrome 设备模拟 + 触摸）按住指针能拖动、客户机指针 1:1 跟随、
拖到屏幕四角都不会被工具栏盖住、点一下仍是精确左键。

## 20. 控制台页面：按钮条不再拽住指针（独立定位 + 自动翻边）

**问题**

用户反馈："鼠标指针会受到 `mp-btns` 的影响不能自由移动（按钮条靠边后就无法继续移动）"。

根因在布局：`#mouse-panel` 原本是 `display: flex` 竖排，`#mp-cursor` 与 `#mp-btns` 是它
的两个子项，于是**面板的盒子宽高 = 指针 + 按钮条**（宽 ≈ 240px，高 ≈ 37+5+32px）。而
`placeWidget()` 又是按面板的 `offsetWidth/offsetHeight` 把指针夹在视口内的：

    x ∈ [0, innerWidth - 240]        y ∈ [0, innerHeight - 74]

→ 指针最多只能挪到 `innerWidth - 240` 附近 —— 也就是"按钮条贴到右边缘之后，指针就再也
拖不过去了"；底部同理。

**实现**（`www_console/index.html`）

- **按钮条脱离指针盒子**：`#mp-btns` 改为 `position: fixed` 的独立层（仍是 `#mouse-panel`
  的 DOM 子节点，所以收起面板时它跟着消失），不再参与面板的尺寸计算。
- **指针的夹取只按指针自己**：`placeWidget()` 用 `#mp-cursor` 的 `34×46` 夹取，并
  **允许指针滑出屏幕、只留 `KEEP = 16px` 在屏内** —— 笔尖（= 指针左上角）因此能一直顶到
  屏幕边，画面四角（尤其右下角）的客户机内容都点得到。
- **新增 `placeStrip()`：按钮条自己找地方**（指针一动就重算）：
  - 竖直：优先在指针**下方**（`p.bottom + 8`）；下面放不下 → 翻到**上方**；两边都放不下 →
    夹进视口（屏幕很矮时可能压住指针，但总比被切成两半强）；
  - 水平：默认与指针**左对齐**（往右展开）；右边放不下 → 与指针**右对齐**（往左展开）；
  - 最后统一夹进 `[4, 视口边长 - 自身尺寸 - 4]`：**它自己永不出屏**。
- **旋屏 / 改窗口大小**重摆一次（`resize` → `syncToPointer()` + `placeStrip()`）。
- 抓手逻辑不变：按钮条的事件照样冒泡到面板上的 pointerdown 委托，按钮本身仍然跳过。

**取舍**

- "允许指针滑出屏幕 16px"是"够抓住"与"能点到屏幕最边"之间的折中：笔尖能到
  `innerWidth`，指针图形则可能有一小半在屏外（看不见但抓得到）。
- 按钮条与指针之间留 8px 缝、且盒子不与指针重叠：既不误触，也不遮住笔尖周围。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（36 项，含"面板不再 flex 竖排""按钮条 fixed""夹取只看指针尺寸""允许滑出屏幕""下面
放不下翻上面""右边放不下往左展开""旋屏重摆"）全通过。**实测待确认**：指针在屏幕四角都能
拖到、贴边时按钮条自动翻到上方 / 左侧且完整可见、点 / 拖手感与之前一致。

## 21. 控制台页面：虚拟鼠标键区重排（方向键 / 左右键 / 滚动）+ 拖键区慢速移动指针

**需求**

1. 键区按功能设计：**上下左右**、**左键 / 右键**、**滚动**（外加快捷双击与收起）。
2. **拖键区 = 慢速移动鼠标**（细调）。

**实现**（`www_console/index.html`）

- **键区重排**（`#mp-btns` 内分三块，竖排：慢速条在上、键盘区在下）：
  - ① `#mp-drag`：慢速微调条（见下）；
  - ② `.mp-pad`：**方向键** 3×2 网格 —— `↑` 在 `↓` 上方、`← ↓ →` 一行，右上角空位放
    「双」（双击）；键格 `30×26`；
  - ③ 两列 `.mp-col`：**左键 / 右键**、**滚▲ / 滚▼**；右端竖长的 `#mp-close`（收起）。
  - 收起键从原来的横排末尾挪到键区右端、双击键从横排挪进方向键区的空位 —— 键区宽度基本
    不变（≈220px），但功能分区一眼可辨。
- **方向键 = 按步移动指针 + 按住连发**：一次走 **屏幕 8px**（用 canvas 缩放比换算成客户机
  px，所以手感与客户机分辨率无关），按住 420ms 后每 90ms 连发一次（与滚轮共用连发机制）。
  比"拖着指针挪"更容易停在细小目标上（标题栏边、滚动条、菜单项）。
- **拖键区顶上那条 = 慢速微调**（`slowDrag`，`SLOW_GAIN = 0.35`）：手指在屏幕上走 100px，
  指针只走 35px。要点：
  - 换算是"先算指针该去的屏幕坐标，再由 `placeWidget()` + `movePointerTo(笔尖)` 一次落位"
    —— 与拖指针（绝对定位）**共用同一套口径**，不会出现两套换算对不上；
  - 每次落位后把累积量**回读**成面板实际位置（`vx = r.left`）：指针顶到屏幕边时不会继续
    累积，回程也就没有"背隙"（否则会有"拖回来半天指针才动"的怪手感）；
  - rAF 节流，松手补最后一帧；拖动期间让"跟随指针"的轮询让路（复用 `dragging` 标志）。
- **键区冻结**（`freezeStrip`）：手指正按在键区上（按键连发 / 慢速拖动）期间 **`placeStrip`
  不重摆键区** —— 否则按着连发时按钮会从指头底下溜走、拖动面也跟着手指跑而没准头；松手后
  再摆回指针旁边。
- **手势不再互相抢**：绝对拖动改成**直接绑在 `#mp-cursor` 上**（原来是在面板上做委托 +
  `closest("button")` 判断"按钮跳过"），键区那条拖动面有自己独立的监听 —— 两边各管各的，
  `moveDrag` 里再加一道 `slowActive` 兜底。
- 首次打开提示改为："拖指针 = 直接定位；拖键区顶上那条 = 慢速微调；点指针 = 左键"。

**取舍**

- 双击键位置从横排挪到方向键区右上角：横排只剩"左右键 + 滚动 + 收起"，宽度更紧；双击仍
  保留（客户机里开文件 / 进目录常用），只是不再独占一格。
- 慢速倍率 `0.35` 是"细调够用"与"别太慢"之间的折中；要更慢 / 更快改 `SLOW_GAIN` 一处即可。
- 慢速条做成固定文字提示（"拖动 = 慢速移动指针"）而不是只有图标：手指第一次摸上去不一定
  猜得到它是拖动面。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（48 项，含"方向键 3×2 网格位置指派""nudge 走 canvas 缩放比""方向键进连发表""慢速
倍率与累积""落位后回读锚点""拖动中键区冻结""绝对拖动绑在指针本体且不再抢键区手势"）
全通过。**实测待确认**：方向键单步 / 连发手感、拖慢速条是否能精细停在窗口边缘或滚动条上、
按着方向键不放时键区是否稳住不动、键区贴屏幕边时自动翻边。

## 22. 控制台页面：虚拟鼠标新增「拖动」（锁住左键，拖窗口 / 拖滚动条）

**需求**

"在 `mp-btns` 里增加一个按住拖动的功能：鼠标移到一个窗口时，点个按钮后可以随意拖动窗口"。

**背景 / 为什么这样做**

客户机里"拖窗口 / 拖滚动条 / 框选文件"的本质都是 **按住左键再移动**。手机上做不到一边
按住按钮一边拖指针（单指），所以做成**自锁**（点一下开、点一下关）：

- 点「拖动」→ 在指针当前位置**按下左键并保持**（按钮变橙、字变「放开」）；
- 之后拖指针本体（绝对定位）/ 点方向键（小步连发）/ 拖慢速微调条，都带着左键走 ——
  客户机看就是拖动；
- 点「放开」→ 松开左键。

**为什么"按下之后只发移动"客户机也认**：WMKS 解码器自己记着按键掩码（`wmks.js`：
`VNCDecoder.onMouseButton` 改 `_mouseButtonMask`、`_sendMouseEvent` 每次都带上它），
所以按下之后每次 `sendMouseMoveMessage()` 发出的指针事件都带着"左键按住"这一位。本页只
调 WMKS 的（半）公开方法，不自己拼协议。

**实现**（`www_console/index.html`）

- 键区（`.mp-pad`）左上角空位放「拖动」按钮（`data-mp="dragLatch"`）—— 原本只有
  拖动/双/↑/◀/▼/▶ 里的五个键，正好补上第 6 格，**宽度不变**；锁住时按钮**橙底深字**
  （`.mp-latch`），与"手指按下的蓝色反馈"区分开，一眼能看出"左键现在是按住的"。
- `setLatch(on)`：`sendMouseButtonMessage(cursor(), on, mask("left", 1))` —— **按下与放开
  都在指针当前位置**（客户机就是按"按键 + 位置"理解拖拽）。首次按下弹一句用法提示。
- 与其他手势的关系（都做了明确处理，不留含糊）：
  - **方向键 / 慢速微调条 / 拖指针**：照常工作，效果就是"拖着窗口走"（拖窗口时往往要在
    屏幕上蹭几像素，这几个正好用得上）；
  - **点「左键」/「双击」**：先自动放开锁住的左键，再重新点一次（否则客户机会把
    "按下 + 放开"理解成松手、结束拖动，界面与实际状态也会对不上）；
  - **轻点指针**：锁住时 = 就地放开（等于点「放开」），不再补一次点击。
- **不让客户机"以为左键一直按着"**（这类 bug 最难受：点哪都是拖）：
  - 收起虚拟鼠标（`setOpen(false)`）→ 自动放开；
  - 断开 / 重连（`destroyWmks`）→ **在 `wmks.destroy()` 之前**先放开（那会儿链路还在，
    mouseup 才发得出去），对外暴露 `MP.releaseLatch`；
  - **每 400ms 对账**（`syncLatchFromWidget`，挂在 `syncToPointer` 入口）：在画面（canvas）
    上轻点一下时 WMKS 自己会发"按下 + 放开"，锁住的左键会被它放掉 —— 界面跟着改回
    「拖动」并提示一句，不会出现"界面按住中、实际早松开"的假象。
- 慢速微调条的文案由"拖动 = 慢速移动指针"改成 **"慢速微调"**：避免与新按钮的「拖动」在
  字面上打架。

**取舍**

- 名字叫"按住"，实现是自锁：手机单指做不到"一边按住一边拖"，自锁是唯一可行的手感；代价是
  状态要自己管好（上面那几条放开路径）。
- 在画面上轻点会结束拖动：这是 WMKS 自己的触摸 → 鼠标映射决定的（它会发一次完整的
  按下 + 放开），不做拦截 —— 拦下来反而会让画面上的正常点击失效。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（39 项，含"按钮落在方向键区 (1,1) 格""锁定态橙色且与蓝色按下态区分""按下 / 放开都在
指针当前位置""与 WMKS 实际掩码对账""收起面板即放开""destroy 之前放开且顺序正确""锁住时点
左键先放开再点""轻点指针 = 就地放开"）全通过。**实测待确认**：点亮「拖动」后拖窗口 /
拖滚动条是否真的跟着走、方向键能否带着窗口微调、点「放开」后左键确实松开（客户机里点一下
不再拖）。

## 23. 控制台页面：虚拟鼠标键区排版统一化（网格 / 尺寸 / 标签 / 分组）

**需求**

"mp-btns 布局优化一下" —— 键区是前面几节一个个功能加上去的（方向键 → 慢速条 → 拖动…），
排版已经有点散。

**问题（改之前）**

- 按键尺寸三套：方向键格 30×26、两侧列 `min-width: 40px`、收起键被 `align-items: stretch`
  拉成一整列高的**竖长条**（像根柱子，✕ 悬在中间很怪）；
- 间距两套（方向键区内部 3px、列间与列内 4px）；
- 字号三套（方向键 12px、两侧列 11px、方向键区里的中文标签 10.5px）；
- 标签混用**同一组三角形**：方向键是 ▲◀▼▶，滚动键却是"滚▲ / 滚▼"，扫一眼容易当成
  同一类；
- 功能分组没有视觉线索（方向键、按键、滚动挤成一排）。

**实现**（`www_console/index.html`，纯 CSS + 标签，不动逻辑）

- **尺寸统一**：所有按键 **26px 高**、字号 **11px**；方向键格 30×26；两侧列同宽 **34px**
  （「左键 / 滚上」都是 2 字，等宽才像一组）；收起键改成 **26×26 居中小方钮**。
- **间距统一 4px**（键区行内、方向键区内部、列内一致）。
- **标签统一成 2 字**：`双 → 双击`、`滚▲ / 滚▼ → 滚上 / 滚下` —— 不再和方向键的三角形
  混；方向键仍是纯三角形（一格里一个符号，最清楚）。
- **分组有视觉线索**：方向键区加一层 `rgba(255,255,255,.06)` 淡底 + 圆角，一眼看出"这块
  是移动指针的"；`拖动` 从左上角挪到**右上角**（紧邻「左键 / 右键」列，都是"按键"类）。
- **DOM 顺序 = 视觉顺序**（双击 ▲ 拖动 ◀ ▼ ▶）：位置是 `grid-column / grid-row` 显式
  指派的，顺序本来就"看不见"，但 Tab / 读屏按 DOM 走，理顺了更好。
- 慢速微调条收成 **16px 高**（"标题条"排版：左端点阵 grip + 名字，整条都能按下去拖）。
- 键区整体 ≈ **212×84px**（原来 221×85）：宽度略收，观感从"几块拼起来"变成一个网格。

**取舍**

- 没有改功能键的**数量与种类**（方向键 / 慢速微调 / 左键 / 右键 / 双击 / 拖动 / 滚上 /
  滚下 / 收起），只动排版 —— 手感与既有用法完全不变。
- 收起键从"整列高"缩到 26×26：更好看，但触摸目标变小（仍在键区右端，不难按）。觉得难点
  就说一声，把它放回整列高。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（32 项，含"标签统一成 2 字""滚动不再用三角形""DOM 顺序 = 视觉顺序""间距统一 4px"
"按键统一 26px / 11px""收起键 26×26 居中""旧样式无残留""拖动锁定态 / 按下反馈 / 连发 /
慢速拖 / 翻边等逻辑未被碰坏"）全通过。**实测待确认**：整体观感是否更整齐、每个按键是否都
好按、贴边翻边时键区仍完整。

## 24. 控制台页面：移动端「长按 = 右键」补上（并让出被 WMKS 占用的长按）

**问题**

用户反馈："移动端不使用虚拟鼠标时，长按（长按就是右键的作用）不起作用"。

查 `vendor/wmks.js` 的触摸模型（`WMKS.TouchHandler`）：

- `touchstart`（单指）先假定是"轻点"，并挂一个 `CONST.TOUCH.leftDragDelayMs`（默认
  **300ms**）的定时器；到点后把 `opType` 改成 `drag`，调用 `_initDragEventAndSendFeedback()`
  —— 里面就是 **`sendMouseButtonMessage(pos, true, LEFT)`**（"按住左键"）；
- `touchend` 按 `opType` 分派：`tap` → `_sendTwoTouchEvent(..., CLICK.left)`（左键单击）、
  `drag` → 放开左键、`tap_twice` → 双击、两指 → `CLICK.right`（右键）。

也就是说：**WMKS 的长按 = "按住左键开始拖动"，不是右键**；手机上真正的右键手势只有
"两指点一下"（很隐蔽）。用户要的是长按出右键菜单 —— 这是**同一个手势**，不能两用，必须挑。

**实现**（`www_console/index.html`，两处）

1. **让出长按**：把 `WMKS.CONST.TOUCH.leftDragDelayMs` 改成 `2147483647`（`setTimeout`
   的极限 ≈ 24.8 天，等于关掉"长按 = 按住左键"）。带存在性判断 + `try/catch`，换 WMKS
   版本没有这个字段时直接跳过。要在客户机里拖东西，改用虚拟鼠标键区上的「拖动」锁按钮
   （第 22 节）—— 比"手指完全不能抖地原地按 300ms 再拖"可控得多。
2. **自己实现长按 = 右键**（`longPressRightClick`，放在虚拟鼠标模块里，因为它要用
   `widget()` / `mask()` / `guestFromScreen()` / `isOpen()`）：
   - 在 **`#stage` 的捕获阶段**听 `touchstart / touchmove / touchend / touchcancel` ——
     捕获先于 canvas 上的 WMKS 监听（`installTouchHandlers` 绑在 canvas 上），拦得住；
     `#stage` 又不会被重连替换（`#console` 会），监听不会丢；
   - 判定条件：**单指** + 落在 `#console` 内 + 按住 ≥ **500ms** + 期间位移 < **12px** +
     抬指时仍满足 → 长按：`preventDefault()` + **`stopPropagation()`**（拦掉 WMKS 那次
     左键单击），并在**触点**发一次完整右键（`sendMouseButtonMessage(p, true, right)`，
     它内部会先把指针移过去；再发 `false` 放开）；
   - **只拦 touchend**：`touchstart` 一直放行 —— WMKS 的状态机不会"卡住"；就算它真按下了
     左键，抬指时也一定会被它松开，不会把左键卡在按下状态；
   - 其余手势（轻点 / 双击 / 两指点一下 = 右键 / 滚动 / 指头移动过的拖动）**一律不碰**；
   - 触点 → 客户机坐标优先走 WMKS 自己的 `_applyZoomCorrectionToTouchXY()`（与它内部的
     缩放 / 居中修正一致），拿不到再退回本页那套 `guestFromScreen()`（= 拖指针用的同一个
     换算）；
   - 首次长按弹一句提示："长按 = 右键（客户机的右键菜单；两指点一下也是右键）"；
   - **虚拟鼠标打开时不接管**（`isOpen()` 直接返回）：那时指针位置由小部件决定，屏幕上戳
     一下生成右键会和"指针在哪"对不上 —— 那种情况用键区的「右键」按钮（在指针处点）。

**取舍**

- **同一个手势只能选一个语义**：长按给了右键，WMKS 原来那个"长按 = 按住左键再拖"就没有
  了。替代方案是虚拟鼠标键区的「拖动」（锁左键）—— 它不要求"手指一动不动"，也比 300ms
  的暗规则更好发现。
- 只拦被我们判成长按的那一次手势：轻点、双击、两指点一下（右键）、双指滚动全部原样交给
  WMKS，风险面最小。
- 桌面端不受影响（鼠标不产生 touch 事件）；Chrome 设备模拟 + 触摸时同样按真机行为走。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（32 项，含"leftDragDelayMs 推到极限且带存在性判断 / try-catch""四个触摸监听都在
捕获阶段""touchstart 不拦截""四个判定条件""只拦 touchend 且 preventDefault +
stopPropagation""在触点发完整右键""坐标优先用 WMKS 换算""虚拟鼠标打开时不接管"
"未连接时不拦"，以及虚拟鼠标各功能 / 键区布局 / 右键屏蔽 / 键盘桥等回归）全通过。
**真机待确认**：手机上（或 Chrome 设备模拟 + 触摸）在画面上按住约半秒抬指 → 客户机应在
触点弹出右键菜单；轻点仍是单击、两指点一下仍是右键、双指拖动仍滚动、双击仍是双击。

## 25. 控制台页面：移动端键盘默认收起「功能键行 + 发送行」，点标题栏展开

**需求**

"移动端时，键盘的 `kbd-row func` 和 `kbd-sendrow` 默认隐藏，`kbd-head` 点击可以打开"。

**背景**

手机竖屏上这两块很占地方：19 个功能键要 3 行（≈110px）+ 发送行（≈46px），键盘一半高度
都花在"不是每次打字都要用"的东西上。

**实现**（`www_console/index.html`）

- **CSS（只写在移动端媒体查询里）**：
  `body.kbd-rows-closed #kbd-panel .kbd-row.func, body.kbd-rows-closed #kbd-panel
  #kbd-sendrow { display: none; }` —— 写法是"**默认可见 + 类名收起**"（不是反过来）：
  脚本挂了也只会多显示两行，不会把功能藏起来；桌面端不加这个类，两条常显。
- **标题栏加箭头提示**：`#kbd-head .ttl::after` 画一个小箭头，展开时朝下、收起时朝右
  （`rotate(45deg)` / `rotate(-45deg)`），"这里点得动"一眼可见。
- **点标题栏 = 切换**：`#kbd-head` 原来只是拖动把手（悬浮布局挪面板 / 「上下」布局拖分割
  比例），现在两用，用**位移**区分（`HEAD_TAP_SLOP = 8px`，和虚拟鼠标那套同一个思路）：
  - 悬浮布局：`pointermove` 里累计位移，抬指时 `< 8px` 才算"点"；
  - 「上下」布局：同一个 `rsz()` 里也调一次 `watchMove()`，抬指时同样判"点"（拖动调比例
    与点击切换共存）；
  - 顺手修了个小毛病：`dragged`（"用户自己摆过位置"标志，软键盘跟随逻辑靠它）原来是
    **按下就置位**，现在改成**真移动之后**才置位 —— 轻点一下标题栏不该被当成拖过。
- **默认收起**：脚本载入时 `if (isMobileLayout()) { setKbdRows(false); }`（桌面端不加这个
  类）。用户手动展开后**不持久化** —— "默认隐藏"就按每次都是默认来。
- **「上下」布局配套**：`body.kbd-split.kbd-rows-closed #kbd-panel { height: auto; }` ——
  收起时面板跟着内容缩下去（画面变大），再展开回到用户拖出来的分割高度（`--kbd-h`）。
  悬浮布局不用管（本来就按内容自适应高度）。
- **首次提示**：手机上第一次打开键盘且功能键正收着时，弹一句"功能键与发送框已收起：
  点键盘标题栏展开 / 收起"。
- `setKbdRows()` 里补一发 `applyLiveScale()`：「上下」布局下面板高度变了 = 画面区高度变了，
  立刻重新贴合；悬浮布局等于空跑。

**取舍**

- 透明度滑块（`.kbd-nodrag`）与关闭按钮（`#kbd-close`）照旧被排除：在它们上面按不会切换
  收起。
- 状态不持久化：需求是"默认隐藏"，每次打开键盘都从收起开始；要常显点一下标题栏即可，
  本次会话一直有效。
- 桌面端完全不参与（CSS 规则只写在移动端媒体查询里，脚本也用 `isMobileLayout()` 判）。

**验证**

CSS 花括号 / 圆括号配平、两段内联脚本 `new Function()` 语法检查、标签配平、本轮与回归
断言（30 项，含"收起规则确实在移动端媒体查询内、别的分支没有""箭头两个方向""split 下
height: auto""默认收起""8px 位移阈值""split 与悬浮两条路径抬指都判点""滑块 / 关闭按钮仍被
排除""dragged 只在真移动后置位"，以及键盘拖动 / 分割比例 / 透明度 / 软键盘跟随 / 长按 = 右键
/ 虚拟鼠标 / 悬浮菜单等回归）全通过。**真机待确认**：手机上打开键盘 → 功能键与发送框应默认
不见、标题栏右边是小箭头；点标题栏 → 两块出现且箭头转向下；按住标题栏拖动仍是挪面板
（「上下」布局下仍是调分割比例）；桌面端两条照旧常显。

---

## 26. 修「截图间隔」不生效（截图开关改为自管理定时器）

**背景**

用户反馈：把「截图间隔」设成 5 秒后，截图节奏毫无变化。

**排查**

- 实测历史库：`number.…截图间隔` 在 `14:35:45` 由 15 改成 **5**（选项
  `screenshot_interval = 5`、实体属性里也显示 `interval: 5`），但
  `switch.<vm>_screenshot` 的状态写入仍是**严格每 15 秒一次**（14:38:17 / :32 / :47 …）
  —— 抓图节奏被平台轮询（switch 平台 `SCAN_INTERVAL = 15` 秒）卡住了；
- 读 HA 核心 `helpers/entity_platform.py` 确认：轮询间隔是**平台级单一值**
  （`self.scan_interval` / `self.scan_interval_seconds`，构造时注入），
  文件里**从不读 `entity.scan_interval`**，判定实体是否参与轮询只看 `entity.should_poll`。
  所以实体上那个 `scan_interval` property（注释还写着"老版本 HA 不识别此属性时安全回落"）
  是**死代码**：任何 HA 版本都不会用它 → 抓图只能发生在 `async_update()` 里，
  最快就是 15 秒一帧；设 ≥15 秒看着"正常"（那是节流在起作用），
  **一旦设到 15 秒以下就完全没变化**。

**实现**（`switch.py` 的 `ESXiVMScreenshotSwitch`）

- 删掉无效的 `scan_interval` property 与 `_next_capture_at` 节流，改为**实体自管理定时器**：
  `_schedule_capture()` 用 `async_call_later(hass, interval, self._async_capture_tick)` 排下一次；
  `_async_capture_tick()` 抓完一帧后在 `finally` 里再排下一次 ⇒ 间隔从「上一帧结束」起算，
  抓图比间隔慢时不会堆积任务；
- `async_update()`（平台 15 秒轮询）只做两件事：`self.update()` 刷快照 +
  `_sync_capture_schedule()` —— 后者发现「截图间隔」被 number 实体改过就重排，
  改动最迟 15 秒内生效，不用等旧间隔（最长 600 秒）走完；
- 生命周期：`async_added_to_hass` 排上、`async_will_remove_from_hass` 取消
  （并用 `_removed` 挡住"抓图还在飞时实体已被删、`finally` 又排一次"的漏排）；
  `async_turn_on` 立即抓一帧再排、`async_turn_off` 取消；
- `const.py` 的 `MIN_SCREENSHOT_INTERVAL` 注释同步改写（下限只取决于抓图本身的代价，
  不再受平台轮询粒度限制），`switch.py` 顶部注释一并纠正。

**验证**

`python -m py_compile switch.py const.py` 通过；全目录 grep 确认已无
`scan_interval` / `_next_capture_at` 残留（只剩注释里说明"HA 不读它"）。运行时待实测：
重启后把「截图间隔」设成 5 秒，看截图文件 mtime / `switch.<vm>_screenshot` 的
`last_screenshot_time` 是否变成约 5 秒一次。

---

# 2026-10-06 改动（集成版本 `0.8.0`）

> **记录日期**：2026-10-06
> **集成版本**：`0.8.0`（`manifest.json` 与 `const.py` 的 `VERSION`）
> **范围**：当日对 `Y:\custom_components\esxi_stats` 的全部修改。
> 每条按 **背景（为什么要改）→ 排查（查到了什么）→ 实现（怎么改）→ 验证（怎么确认）** 记录。

## 当日改动文件一览

| 文件 | 性质 | 说明 |
|---|---|---|
| `manifest.json` | 变更 | 版本号 → `0.8.0` |
| `const.py` | 变更 | 新增选项常量、`text` 平台、必需文件清单 |
| `config_flow.py` | 变更 | 新增两个选项；修 OptionsFlow 在新版 HA 下的兼容问题 |
| `translations/zh-Hans.json` / `en.json` | 变更 | 补齐选项界面的中文 / 英文文案 |
| `naming.py` | 变更 | 短 `entity_id` 规则扩充 |
| `__init__.py` | 变更 | 数据层重建、刷新内核拆分、`sync` 服务、卸载清理 |
| `sensor.py` | 变更 | 聚合快照加 `kind`、移除重复实体、操作日志实体、取值保护 |
| `switch.py` | 变更 | 两个批量开关、乐观状态、配置项安全读写 |
| `button.py` | 变更 | 同步按钮、三个批量按钮、每台虚拟机的强制重启 |
| `select.py` / `image.py` / `number.py` | 变更 | 抽出纯函数以支持增量同步 |
| `esxi.py` | 变更 | 电源命令幂等、日志降噪、通知受控 |
| `services.yaml` | 变更 | 登记 `sync` / `refresh` 服务 |
| `entity_sync.py` | **新增** | 实体增量同步内核 |
| `optimistic.py` | **新增** | 乐观状态 + 待确认窗口 |
| `entry_options.py` | **新增** | 配置项 `options` 的安全读写 |
| `vm_bulk.py` | **新增** | 批量操作工具（分组解析、调度、共用 Mixin） |
| `op_log.py` | **新增** | 批量操作日志中枢 |
| `text.py` | **新增** | `text` 平台（批量分组参数实体） |

---

# 第一部分：改动过程

## 1. 集成选项与配置流程

### 1.1 新增两个集成选项

**背景**
短 `entity_id` 机制（见第 2 节）需要一个总开关 —— 用户可能想保留 HA 原生的长 id；
「实体聚合」快照的刷新间隔原本写死在代码里，需要变成可调（调大可以省掉反复遍历注册表的开销）。

**实现**
- `const.py`：新增 `CONF_SHORT_ENTITY_IDS` / `DEFAULT_SHORT_ENTITY_IDS = True`、
  `CONF_AGGREGATE_INTERVAL` / `DEFAULT_AGGREGATE_INTERVAL = 60` /
  `MIN_AGGREGATE_INTERVAL = 10` / `MAX_AGGREGATE_INTERVAL = 3600`。
- `config_flow.py`：`async_step_init` 的表单与提交分支加入这两个字段，并做范围校验。
- `number.py`：`ESXiAggregateIntervalNumber` 直接读写该 option。

**验证**：选项界面能改、能保存；改聚合间隔后 `sensor.entities` 的重建周期随之变化。

### 1.2 修复「配置」按钮报 500（OptionsFlow 兼容性）

**背景**
点集成卡片上的「配置」进不去（后端 500）。

**排查**
新版 HA 的 `OptionsFlow` 由框架注入 `config_entry`（是**只读属性**），
构造函数签名与老版本不同：
- 老写法 `OptionsFlow(config_entry)` 在新版会因重复赋值只读属性而异常；
- 新版子类若漏调 `super().__init__()`，框架读 `flow.config_entry` 时会抛异常。

**实现**
- `__init__(self, config_entry=None)`：先 `super().__init__()`（老版本会抛 `TypeError`，忽略），
  再保存 `self._legacy_entry = config_entry`；
- 新增 `_entry` 属性：优先取框架注入的 `config_entry`，取不到时回退到 `_legacy_entry`；
- `async_get_options_flow` 按 `hasattr(OptionsFlow, "config_entry")` 判断新旧版本，分别返回
  `ESXiStatsOptionsFlow()` 或 `ESXiStatsOptionsFlow(config_entry)`。

**验证**：新旧两条分支都不再抛异常；选项表单能正常打开与提交。

### 1.3 补齐选项界面翻译

`translations/zh-Hans.json` / `en.json` 的 `options.step.init` 补上
`datastore` / `license` / `notify` / `screenshot_interval` / `short_entity_ids` / `aggregate_interval`
六项文案（例如「把实体重命名为简短固定的 entity_id（如 switch.ha_power）」），避免界面上显示英文键名。

---

## 2. 实体命名：短 `entity_id`

### 2.1 背景：中文名把 `entity_id` 变成一长串拼音

集成让 HA 依据实体名自动生成 `entity_id`，而实体名是中文，于是变成：

```
sensor.xu_ni_ji_windows10_xia_zai_windows10_xia_zai_cpushi_yong_lu
```

后果：`entity_id` 又长又不稳定（改一次虚拟机名就换一套），
外部（卡片、自动化、模板）**无法按 `entity_id` 后缀匹配**，只能按中文短名去认。

### 2.2 实现（`naming.py`）

按 `unique_id` 的结构反推短名。`unique_id` 形如 `{主机}_{entry_id}_{种类}_{名字}`，
剥掉前两段后用三张规则表映射：

| 规则表 | 作用 | 例子 |
|---|---|---|
| `VM_ATTR_SLUG` | 虚拟机属性 → 短后缀 | `cpu_use_pct` → `cpu`、`memory_used_mb` → `mem_used`、`used_space_gb` → `disk_used` |
| `HOST_ATTR_SLUG` | 主机属性 → 短后缀 | `memusage_gb` → `mem_used`、`vms` → `vm_total`、`shutdown_supported` → `can_shutdown` |
| `VM_KINDS` | 虚拟机实体种类 → 短后缀 | `switch_` → `power`、`reboot_` → `restart`、`snapshot_create_` → `snapshot_create` |

另外两处细节：
- **主机名压缩**：`localhost.localdomain` → `localdomain`（只留最后一段），
  单标签主机名（`esxi01`）不变；ESXi 上报常是 FQDN，而部分路径里点已换成下划线，两种都要认。
- **`_join` 拿不到 ascii slug 就放弃**：与其造出非法 id，不如保持原样（返回 `None`）。
- **只改 `entity_id`，不动显示名**：显示名仍是「设备名 + 中文短名」，如「虚拟机 HA 电源」。

`apply_short_entity_ids()` 负责实际重命名，**幂等**：目标 id 已一致或已被占用就跳过，
所以每次 setup 调用都无副作用。

### 2.3 现行规则表

| `unique_id` 后缀 | 生成 `entity_id` | 备注 |
|---|---|---|
| `sync_button` | `button.sync` | 当日新增 |
| `batch_grouping` | `text.batch_grouping` | 当日新增 |
| `operation_log` | `sensor.operation_log` | 当日新增 |
| `all_vms_power` / `_screenshot` / `_snapshot` / `_restart` / `_force_restart` | `<domain>.all_vms_*` | 当日新增 |
| `vm_force_restart_<虚拟机>` | `button.<虚拟机>_force_restart` | 当日新增 |
| `entities_aggregate` | `sensor.entities` | |
| `aggregate_interval` / `screenshot_interval` | `number.*` | |

### 2.4 踩坑：同步新增的实体拿到的是拼音 id

**背景**
实体同步（第 5 节）动态补进来的新实体，`entity_id` 仍是 HA 生成的拼音长名，
与 setup 时建立的其它实体风格不一致。

**排查**
`apply_short_entity_ids()` 是遍历**实体注册表**工作的，而
`async_add_entities()` 之后实体是**异步**注册进注册表的 —— 同步流程里紧接着调用它，
扫到的是"还没有新实体"的旧注册表，自然什么都没改到。

**实现**
同步流程中先等新增实体真正进入注册表，再调用 `apply_short_entity_ids()`；
并且顺序必须在**刷新聚合快照之前** —— 快照条目里存的是 `entity_id`，顺序错了卡片就查不到实体。
无新增实体时跳过重命名。

**验证**：用 stub 加载真实的 `entity_sync.py`，断言调用顺序为 `["rename", "refresh"]`；
无新增时为 `["refresh"]`。

---

## 3. 聚合实体 `sensor.entities`

### 3.1 背景：一次状态体检

对聚合实体做了一次完整核对（当时 `state: '161'`）：

| 检查项 | 结果 |
|---|---|
| 计数一致性 | `state = 161` = `total_entities` = 7 设备 × 23 实体，一致 |
| 条目字段 | 全部为 `{entity_id, name, icon}`；`name` 是短名（无设备名前缀重复） |
| 图标 | 161 条全部有值，分类正确（主机 `mdi:server`、VM `mdi:desktop-tower`、关机 `mdi:power-off`、截图开关随状态切换） |
| 排序 | 设备按名字典序、实体按 `entity_id` 升序 → 结构签名稳定，前端不会无谓重建 DOM |
| `attributes_bytes` | **17853**，超过 recorder 单条状态 16384 字节的硬上限（历史记录里该属性会被丢弃，实时读取不受影响） |

体检发现两件值得改的事：**主机下有一条重复的「电源策略」**、**快照里没有设备类型信息**。

### 3.2 移除重复的只读「电源策略」实体

**排查**
`ESXi 主机` 设备下同时存在两条几乎相同的记录：

```
select.localdomain_power_policy   电源策略   mdi:power-settings   同一个值（可读可写）
sensor.localdomain_power_policy   电源策略   mdi:power-settings   同一个值（只读）
```

名字、图标、值三者全同，只有域不同 → 列表里表现为重复项，容易点错或写错自动化。
真正的操作入口是 `select`（能读能写），只读的 `sensor` 没有额外价值。

**实现**
- `sensor.py` 新增 `LEGACY_HOST_ATTRS_SKIPPED = ("power_policy",)`，
  `vmhost` 分支据此不再创建该只读实体；
- **关键**：只停建不够 —— HA 的实体注册表**不会自动遗忘**不再创建的实体，
  残留条目会变成 restored / unavailable 的幽灵实体，**而且照样出现在聚合快照里**。
  因此新增 `_remove_legacy_host_sensors()`，在平台初始化时按 `unique_id` 后缀
  `_attr_power_policy` 从注册表删除（只删 `sensor` 域，`select`、其它属性、虚拟机的 `power` 一律不碰）；
- `HOST_ATTR_CN` / `HOST_ATTR_ICON` 里的 `power_policy` 保留并加注释（仅兜底，不再产实体）。

**验证**：stub 加载真实 `sensor.py`，断言清理函数只删目标条目、保留 `select` 与虚拟机的 `power`、可重复执行。

### 3.3 设备节点加 `kind`

**背景**
聚合快照里**没有任何类型信息**，使用方只能靠"设备名以『虚拟机』开头 / 有没有 image 实体 / 短名特征"去猜，
属于推断而非事实。

**实现**
- `sensor.py` 新增 `_device_kind(device)`：遍历设备 `identifiers`，
  按前缀判定 —— `vm_` → `vm`、`host_` → `host`、其它 → `other`
  （设备是本集成自己建的，identifier 是权威事实）；
  **顺序重要**：`vm_host_x` 这类虚拟机名要先按 `vm` 判。
- `_collect()` 的设备节点变为 `{device_name, kind, entities}`。
- 前端卡片（`modules/cards/esxi.js`）优先采信 `kind`，缺失时回退到原有推断链
  （兼容旧版后端），并把 `kind` 纳入结构签名。

**验证**：断言含 `vm_host_x` 顺序、`other` / 空 / 跨域 identifier 的分类结果；快照结构完整。

---

## 4. 数据层

### 4.1 排查：「在 ESXi 新建虚拟机，HA 会自动同步吗？」

逐层核实后结论明确 —— **数据会同步，实体不会**：

| 层 | 行为 | 证据 |
|---|---|---|
| 数据层 | **会**：`update_data()` 每 45 秒（`@Throttle`）重新遍历 ESXi 全部虚拟机，写进 `hass.data[...]["vm"]` | `__init__.py` |
| 实体层 | **不会**：所有实体只在 `async_setup_entry` 里创建一次，集成内没有任何动态补建实体的机制 | 各平台 setup |
| 聚合层 | 不会：`_collect()` 遍历实体注册表，只反映"HA 里真实存在的实体" | `sensor.py` |
| 卡片层 | 不会：卡片跟着聚合走 | `esxi.js` |

也就是说新虚拟机**数据其实已在内存里**，只是没有实体把它呈现出来 —— 必须重载集成或做增量同步。
同时排查出两个更隐蔽的缺陷：**删除虚拟机留下僵尸数据**、**重命名虚拟机新老实体并存**。

### 4.2 每轮重建子字典（修僵尸数据）

**排查**
`update_data()` **不清空** `vm` / `vmhost` / `datastore` / `license` 子字典，只是覆盖同名键：

```python
for virtual_machine in vm_list:
    self.hass.data[DOMAIN_DATA][self.entry]["vm"][vm_name] = get_vm_info(virtual_machine)
```

于是虚拟机在 ESXi 上被删除后，键还在 → 实体仍从 `hass.data` 取到**删除前的旧数据** →
卡片里那台机器状态永远停在最后一刻（比如一直显示"运行中"），**看起来像好的**。
主机、数据存储、许可证同一套写法，同样如此。

**实现**
每轮刷新时重建这四个子字典。**做法不是逐个 `clear()` 再写入**（见 4.4），
而是构建新字典后**一次性整体替换**。重建之后，「某个键消失了」才成为可靠信号 ——
同步服务正是靠它判定该删哪些实体。

**副作用（预期）**：对象消失后实体表现为**不可用**，而不再是"显示旧值"。

### 4.3 拆出无节流的刷新内核 + 刷新成功标志

**背景**
同步服务需要"立刻拿到最新数据"，但 `update_data()` 带 45 秒节流，直接调用拿到的可能是旧快照。

**实现**
- 把原 `update_data()` 拆成两层：`update_data()`（带 `@Throttle`，供实体轮询）
  与 `_update_data()`（实际刷新，无节流，供同步服务直接调用）；
- 新增 `self.last_update_ok`：刷新全流程走完才置 `True`（失败会在异常分支直接返回）。
  同步服务靠它决定**能不能删实体**。

**验证**：实体轮询仍走节流；同步调用能拿到新数据。

### 4.4 竞态：日志刷屏 `VM xxx not found in data`

**排查**
"先 `clear()` 再逐项写入"的窗口期内，若另一处触发读取（并发轮询），就会读到中间态 → 抛异常刷屏。

**实现**
改为**构建新字典 → 一次性赋值替换**（读取方要么看到旧字典、要么看到新字典，不存在空窗）；
同时把这类"对象已消失"的日志从 error 降级为 debug。

**验证**：并发场景不再刷错误日志。

---

## 5. 实体同步（`entity_sync.py` + `esxi_stats.sync`）

### 5.1 为什么同步必须在集成侧

前端**看不到"还没注册成实体的新虚拟机"** —— 集成只在 setup 时建一次实体，
之后在 ESXi 上增删改名，HA 侧实体清单都不动。
因此任何"前端刷新数据"的方案都救不了；唯一有效动作是让集成侧重新计算并增删实体
（比整体重载更优：不中断、已存在虚拟机的 `entity_id` / `unique_id` 不变）。

### 5.2 同步内核流程

新增 `entity_sync.py`，`async_sync_entry(hass, entry)` 依次执行：

1. 强制刷新一次 ESXi 数据（调用 `_update_data()`，绕过节流）；
2. 让全部平台重新计算**"此刻应该存在的实体清单"**；
3. 只把**缺失的**实体 `async_add_entities` 进去（`unique_id` 已存在的绝不重复创建）；
4. 数据刷新成功时，把注册表里**已经不该存在的**实体移除，并清理变成空壳的设备；
5. 给新增实体补套短 `entity_id`（见 2.4，必须在下一步之前）；
6. 强制刷新聚合快照（否则卡片最长要等一整个刷新间隔）。

### 5.3 三道防误删守卫（删除不可逆，重点）

| 守卫 | 触发场景 | 行为 |
|---|---|---|
| 数据刷新失败 | ESXi 不可达 | **只新增，绝不删除**（看 `last_update_ok`） |
| 某平台算不出清单 | `build` 抛异常 | 该平台已有实体**全部保留**，其它平台照常处理 |
| 刷新"成功"却没有任何主机 / 虚拟机 | 权限被收回 / 连错目标 | **一个都不删**，日志告警 |

第三道守卫的判断依据是：注册表里还有实体，但 `vm` 与 `vmhost` 都为空 —— 这几乎不可能是真实状态。

**验证**：stub 加载真实 `entity_sync.py`，覆盖"只补缺失 / 不重复创建 / 移除已消失 + 清理空壳设备 /
三道守卫各自生效 / 调用顺序 / 钩子缺失时安全返回"，断言全过。

### 5.4 平台改造：抽出纯函数 + 登记钩子

**背景**
同步需要反复询问各平台"你现在应该有哪些实体"，而原来的构造逻辑写在
`async_setup_entry` 里（直接 `async_add_entities`），无法复用。

**实现**
六个平台（`sensor` / `switch` / `button` / `image` / `number` / `select`）统一改造：

- 抽出纯函数 `_build_entities(hass, config_entry)` —— **只返回实体对象列表，
  不添加实体、不改 `hass.data`**；
- `async_setup_entry` 改为调用它 + `register_platform(hass, entry_id, platform, _build_entities, async_add_entities)`
  把 `add` 回调登记进 `hass.data`，供同步调用。

顺带处理三处连带问题：

- **`image.py` 的 `local_ok`**：原本在 setup 里 `await hass.async_add_executor_job(os.path.exists, ...)` 探测
  `/local/esxiimg` 是否存在。`_build_entities` 必须**同步无副作用**，
  因此改为 setup 时探测一次并缓存到 `entry_data["screenshot_local_ok"]`，build 时直接读缓存。
- **`sensor.py` 的取值保护**：`ESXiSensor.update` 是唯一一处直接对
  `hass.data[...][cond][obj]` 下标取值的地方，对象消失后会抛 `KeyError`。改用 `.get()`，
  缺失时置 `_attr_available = False`（保持不可用，等同步移除它）。
- **聚合实体可被强制刷新**：新增 `force_refresh()`（绕过自身节流重建快照并写状态）
  与 `async_added_to_hass` 登记（把自身存进 `entry_data["aggregate_entity"]`）。

### 5.5 服务 `esxi_stats.sync`

**实现**
- `__init__.py` 新增 `SYNC_SCHEMA`（可选 `entry_id`）与 `async_register_sync_service(hass)`；
- **刻意不放进原 `async_add_services`**：那个只在 license 支持写 API 时才注册，
  而实体同步纯粹是本地的注册表增删，与 ESXi 写权限无关 → 在 setup 时**无条件注册**；
- 支持 `entry_id` 精确指定，不填则同步全部 ESXi Stats 条目；
- 结果写日志：`同步完成（ESXi 主机）：新增 23 个实体、移除 0 个实体`；
- `async_unload_entry` 里清理 `HUB_KEY` / `aggregate_entity` / `screenshot_local_ok`
  以及各平台登记的同步钩子（否则重载后钩子指向已卸载的平台）。

`services.yaml` 登记该服务（含中英文说明与安全约束说明）。

### 5.6 同步按钮实体 `button.sync`

**背景**：需要一个能在 HA 里直接点击的入口（而不是只调服务）。

**实现**：`button.py` 新增 `ESXiSyncButton`（中文名「同步实体」，图标 `mdi:sync`），
挂在「ESXi 主机」设备下；`async_press` 直接转发到同一个 `async_sync_entry`。
它是全局静态实体，也在"应存在的实体清单"里，所以不会被同步自己误删。

---

## 6. 开关状态可靠性

### 6.1 乐观状态 + 待确认窗口（`optimistic.py`）

**背景**
点「开机」，开关**立刻弹回「关」**，几秒后又变成「开」——状态抖动。

**排查**
两个原因叠加：ESXi 开关机是**异步任务**（要几秒到几十秒）；
而集成读的数据还有 **45 秒节流**，期间拿到的都是提交前的旧快照。

**实现**
新增 `PendingState`：

- `start(target)`：提交动作后记录期望状态；
- `resolve(real)` 返回 `(应显示的状态, 是否该催刷新, 超时说明)`：
  真实状态追上 → 收敛返回真实值；仍在窗口内 → 返回期望值并按 `REFRESH_INTERVAL = 2.5` 秒催刷新；
  超过 `TIMEOUT = 90` 秒 → **放弃乐观态、回退真实值并告警**（不会一直骗人）。

虚拟机电源开关与两个批量开关都接入。两个批量开关的实现略有不同：

- `switch.all_vms_power`：`PendingState` 的目标恒为 `True` —— 批量场景下"真实状态"表达的是
  「有没有达成目标」（全部运行 / 全部关闭），而不是开或关本身，因此另用 `_want_on` 记录期望方向；
- `switch.all_vms_screenshot`：状态不来自 ESXi 数据（读的是本地 `options`），
  所以只按期望显示、**不需要催数据刷新**，下次轮询读到新 options 自然收敛。

### 6.2 电源命令幂等（`esxi.py`）

**排查**
日志出现 `Task 'power on' failed: ... (Powered on)` —— 对已经在运行的虚拟机重复下发开机命令。
场景很常见：UI 因 45 秒节流显示滞后，用户以为没生效又点一次；批量开机也会把已开着的机器一起带上。

**实现**
`vm_pwr()` 在**同一个连接里**先读一次真实电源状态（零额外开销），命中以下情况直接跳过并记 info 日志：

| 命令 | 跳过条件 |
|---|---|
| `on` | 已经 `poweredOn` |
| `off` | 已经 `poweredOff` |
| `suspend` | 已经 `suspended` |
| `reset` / `reboot` / `shutdown` | 不是 `poweredOn`（无法执行） |

### 6.3 配置项安全读写（`entry_options.py`）

**背景**
两个现象：**截图开关状态在 HA 重启后丢失**；**改截图间隔 / 聚合间隔会把截图状态清掉**。

**排查**
根因是 HA 的一个行为：`async_update_entry()` 会**创建一个新的 `ConfigEntry` 对象**并替换旧的，
而实体里缓存的 `self._config_entry` 永远指向最初那个旧对象。于是：

- 直接读 `self._config_entry.options` 拿到的是**过期数据**；
- 基于它做「读-改-写」更糟 —— 每次写入都会把上一次刚存进去的内容覆盖掉。

实测表现：截图开关状态存在 `options["screenshot_switches"]` 里，多台虚拟机各自保存时互相覆盖，
最后只剩一台的状态活着，HA 重启后其余虚拟机全部回到默认的"关闭"。

**实现**
新增 `entry_options.py` 统一入口：

- `current_options(hass, config_entry)`：取**最新**的 options（优先读 HA 里的当前 entry）；
- `store_options(...)` / `update_option(...)`：以最新 options 为基线做增量更新。

`switch.py`（截图开关）与 `number.py`（两个间隔）改用它。

**验证**：stub 断言"多台虚拟机连续开关截图"与"改间隔后截图状态仍在"。

---

## 7. 批量操作

**需求**：在**主机设备**下集中放几个"整机"控制 —— 一键开/关所有虚拟机、一键开关所有截图、
以及一键对所有虚拟机做快照 / 重启。

**设计取舍**：有状态、可开可关的做成 `switch`；一次性动作（快照、重启）做成 `button`
（开关表达不了"按一下执行一次"）。全部挂到同一个主机设备下，不额外造设备。

### 7.1 两个批量开关（`switch.py`）

| 实体 | 短 id | 打开 / 关闭 | 状态语义 |
|---|---|---|---|
| 全部虚拟机电源 | `switch.all_vms_power` | 对全部虚拟机开机 / 关机 | 有任意一台在运行即 `on` |
| 全部虚拟机截图 | `switch.all_vms_screenshot` | 全部开 / 关截图（各自立刻抓一帧） | 有任意一台开着即 `on` |

要点：

- **不直接调 ESXi 接口**，而是逐台调用各虚拟机自己的 `switch.turn_on/off` 服务 ——
  这样完全复用单台逻辑：装了 VMware Tools 的走优雅关机（`ShutdownGuest`），
  没装的才硬关机，与单台操作**行为一致**（这是刻意选择，避免两套语义）。
- 截图开关的状态与单台开关**同源**（读同一份 `options["screenshot_switches"]`），不存在两套状态打架。
- 各自带 `running` / `total`（或 `enabled` / `total`）属性；没有虚拟机时不可用。

### 7.2 三个批量按钮（`button.py`）

| 实体 | 短 id | 图标 | 行为 |
|---|---|---|---|
| 全部虚拟机快照 | `button.all_vms_snapshot` | `mdi:camera-plus` | 对全部虚拟机创建快照 |
| 全部虚拟机重启 | `button.all_vms_restart` | `mdi:restart` | 智能重启（见 7.5 的分工） |
| 全部强制重启 | `button.all_vms_force_restart` | `mdi:restart-alert` | 一律硬重置 |

### 7.3 批量分组参数（`text.batch_grouping`）

**背景**
一次性对 7 台虚拟机同时开关机，会把 ESXi 的 `hostd` 打满；需要可调的执行节奏。

**实现**（`vm_bulk.py` + `text.py`）
新增 `text` 平台（`PLATFORMS` 从 6 个变 7 个），承载「批量分组」实体，格式 **`"组大小,间隔秒"`**：

| 输入 | 行为 |
|---|---|
| `"3,10"` | 每 3 台一批，批间等 10 秒 |
| `"3"` | 等价 `"3,0"` |
| `"0"` / `"0,0"` / 留空 | **不启用**（默认值 `DEFAULT_GROUPING = "0,0"`）：全部一次性执行 |
| `"0,20"` | 视为不启用（组大小为 0 时间隔无意义） |
| `"abc"`、`"10,20,30"`、`"-1,5"`、`"10 20"` | **拒绝写入并报错**，实体保留原值 |

- `normalize_grouping()` 宽容处理空格、中文逗号，写入后归一化保存（`" 10 ，10 "` → `"10,10"`）；
- 间隔上限 `MAX_INTERVAL = 3600` 秒（避免误填一个把批量操作挂死的值）；
- **生效范围**：全部开机 / 全部关闭 / 全部快照 / 全部重启 / 全部强制重启；
- **不生效**：全部虚拟机截图（本地动作，与 ESXi 负载无关）；
- ⚠️ **组大小需小于虚拟机总数才会真正分批** —— 7 台设 `10` 只有一批，间隔永远用不上；
- ⚠️ 组间间隔是**固定节奏**，不是"等上一批关机/重启完成再发下一批"（开关机命令本身异步下发、立即返回）；
- `BatchEntityMixin` 提供**并发保护**：同一时间只允许一个批量任务跑
  （否则两批任务交叉，"分组 + 间隔"的语义会彻底乱掉），并在卸载时取消任务；
- 实体属性带 `usage` / `disabled` / `current` / `applies_to` / `not_applies_to` / `note`
  以及机器可读的 `group_size`、`interval`，在 HA 里点开实体即可看到用法；
- 实体图标 `mdi:group`；输入长度上限 16 字符（`_attr_native_max`），
  并带一份仅供前端表单即时提示的正则 pattern（服务调用不走 pattern，真正校验在 `async_set_value` 里）。

**踩坑**：初次实现时报 `NotImplementedError` / `unknown error` ——
`text` 平台的写入钩子是 `async_set_value`，误写成了 number 平台的 `async_set_native_value`；
修正后并改抛 `HomeAssistantError` 以便界面显示中文提示。

### 7.4 操作日志（`op_log.py` + `sensor.operation_log`）

**背景**
批量操作是"一次点下去、结果分散在 ESXi 侧"，HA 里看不到过程。

**实现**
- `op_log.py` 新增 `OperationLog`（内存滚动日志 + 当前进度）：
  `MAX_ENTRIES = 120` 条保留在内存，`EXPOSE_ENTRIES = 60` 条暴露到实体属性（单条状态属性有约 16KB 上限）；
- `sensor.py` 新增 `ESXiOperationLogSensor`（短 id `sensor.operation_log`）：
  **state** = 当前 / 最近一次操作名（`全部开机` / `全部关闭` / `全部快照` / `全部重启` / `全部强制重启`），
  从未执行过才是"空闲"；**属性** = `running` / `progress`（如 `6/6`）/ `operation` / `operation_key` /
  `last_status` / `total_entries` / `log[]`（运行中还会带 `started_at`）；
- 日志按时间正序，每条含 `time` / `action` / `target` / `status` / `message`；
- **只记录批量操作**（单台操作在 HA 自带日志里已可见，不重复记）；
- 结束状态用 **`已下发`** 而不是"完成" —— 命令只是提交给了 ESXi，虚拟机仍在执行。

**踩坑**：起初 state 恒为"空闲" —— 批量下发是瞬时的，`running: true` 的中间态根本捕捉不到；
改为"显示当前 / 最近一次操作"后可用，`running` 由属性精确表达。

**实现补充**：`OperationLog` 的对外读取接口是 `state`（property）与 `attributes()`，
其余状态（`running` / `operation` / `done` / `total` 等）为实例属性；
并提供 `subscribe` / `unsubscribe` 订阅机制 —— 日志传感器据此**及时**刷新，不必等下一个轮询周期。

### 7.5 每台虚拟机的强制重启（`button.<vm>_force_restart`）

**背景**：客户机卡死、VMware Tools 无响应时，"重启"走不通（要 Tools 配合），需要机箱 reset 键那条路。

**实现**：`button.py` 新增 `ESXiVMForceRestartButton`（中文名「强制重启」，`mdi:restart-alert`），
对每台虚拟机各建一个，调用 `vm_pwr(..., "reset")` → `ResetVM_Task()`（hypervisor 层面直接重置，不走客户机流程）。

**与「重启」的分工**：

| | 重启 | 强制重启 |
|---|---|---|
| 调用 | 装了 Tools → `RebootGuest()`；否则硬重置 | **一律 `ResetVM_Task()`** |
| 需要 Tools | 热重启路径需要 | 不需要 |
| 数据风险 | 低 | **高**（未刷盘数据会丢，文件系统可能自检） |
| 用途 | 日常重启 | **客户机卡死、Tools 无响应时的逃生通道** |

虚拟机未运行时按钮不可用；`vm_pwr` 侧也会再拦一道（非 `poweredOn` 一律跳过）。
`naming.py` 的 `VM_KINDS` 中加入 `force_restart_` → `force_restart`
（**顺序在前**，否则会被 `reboot_` 规则抢先匹配成 `restart`）。

---

## 8. 日志与通知降噪（`esxi.py`）

**背景**：开关机一次会在日志里刷出几十条 `InsecureRequestWarning`；
点开关失败还会在通知中心留下一条持久通知。

**实现**
- 集成为兼容自签证书主动跳过校验（`ignore_ssl_verify`），urllib3 为此每次都告警 ——
  只 `disable_warnings(InsecureRequestWarning)` 关掉**这一个类别**，其它 urllib3 警告照常；
- `task_status()` 的持久通知改为受 `notify` 参数控制（成功与失败分支都是），
  实体调用一律传 `False`：失败有日志就够，不必往通知中心塞东西。

---

## 9. 立即刷新数据（`esxi_stats.refresh` 服务）

**背景**
卡片自身每 3 秒读一次值，但**数据本身** 45 秒才换一次 —— 于是屏幕上的 CPU 占用率、内存
等实时值大约 45 秒才动一下。需求是"想立刻看实时值就点一下"，而不是被动等节流。

**排查（为什么用现成的手段催不动）**
`update_data()` 上的 `@Throttle(MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=45))` 是共享的：
HA 的 `homeassistant.update_entity` 服务同样会走到它，所以在节流窗口内"催一下"是无效的 ——
必须有一条**绕过节流**的入口。

**实现**
- 新增服务 `esxi_stats.refresh`（可选 `entry_id`，不填 = 全部条目）：
  1. `await hass.async_add_executor_job(client._update_data)` —— 直接调用不带节流的刷新内核
     （同步服务已经在用同一个内核）；
  2. 依据 `client.last_update_ok` 判断本次是否真的拉到数据，**失败就不动实体**；
  3. 新增 `_async_write_entity_states()`：遍历该条目的实体注册表，逐个
     `async_update_entity()` 催它们立刻重写状态（跳过已禁用的）—— 否则数据虽然换新了，
     界面还要等下一次平台轮询（最多 15 秒）才反映出来。
- **只读**：不创建、不删除、不修改任何实体 —— 与 `sync` 的分工正在这里。
- 服务注册函数由 `async_register_sync_service` 改名为 `async_register_maintenance_services`，
  一次注册 `sync` + `refresh`；两者都与 license 写权限无关（无条件注册）。
- `services.yaml` 登记该服务；卡片头部加了对应的刷新按钮（前端部分，见卡片侧改动）。

**代价（如实说明）**：这次强制拉取**不会重置** `@Throttle` 的计时，所以随后一次常规轮询
可能还会再拉一次，属于可接受的重复（手动刷新本来就是低频动作）。

**验证**
- 用 stub 加载**真实的 `__init__.py`**，13 项断言全过：两个服务都注册、刷新走无节流内核、
  成功后按注册表逐个催状态、跳过禁用实体、拉取失败时不催状态、无 client / 无配置条目时
  安全返回、指定 `entry_id` 只刷该条目、重复注册幂等。
- 卡片按钮（前端）jsdom 冒烟 19 项断言全过：渲染与 `show_refresh: false` 隐藏、点击调
  `esxi_stats.refresh`、旋转态、进行中重复点击无效、成功/失败收尾与提示、停表时清理标记。

---

## 10. 数据刷新间隔可配置（`number.update_interval`）

**背景**
数据刷新由硬编码的 `@Throttle(MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=45))` 控制，
用户既看不见、也改不了。需求是能在界面上调节"多久向 ESXi 拉一次数据"。

**实现**
- 新增 `number.update_interval` 实体（中文名「数据刷新间隔」，`mdi:timer-refresh-outline`），
  范围 `MIN_UPDATE_INTERVAL = 10` ~ `MAX_UPDATE_INTERVAL = 3600` 秒，默认 45；
  值写回 config entry options（走 `entry_options.update_option`，只改这一个键）；
  `extra_state_attributes` 里给出用法、默认值、范围与代价说明（与「批量分组」同样的自解释风格）。
- `EsxiStats.update_data()` **不再用 `@Throttle`**（它的间隔在导入时就固定死了），改为自己做节流：
  每次读 `update_interval()`（从 options 取，带夹紧与非法值回落），距上次真正刷新不足该秒数
  就直接返回 —— 语义与 `@Throttle` 保持一致（被挡下时调用方继续读 `hass.data` 里的旧快照）。
  顺带把计时从"类级共享"变成"每个配置条目独立"。
- 新增 `self._last_update`（单调时钟）与 `self.config_entry`；删掉不再需要的
  `MIN_TIME_BETWEEN_UPDATES` 常量与 `homeassistant.util.Throttle` 导入。
- 实体清单（`number._build_entities`）**总是**包含它（与监控哪些对象无关）—— 否则同步 service
  会把它当成多余实体删掉。
- 短 entity_id 规则：`_update_interval` → `number.update_interval`。
- options flow 表单与中英文翻译同步补上该字段。

**取值口径（为什么下限是 10 秒）**：数据刷新由**实体轮询**触发（sensor 平台 15 秒一轮、
switch 平台 2 秒一轮），所以间隔调到比轮询粒度还小也不会更频繁 —— 下限取 10 秒既保守又能
覆盖两种平台。

**验证**：stub 加载真实模块，**26 项断言全过** —— 默认值来自 options、间隔内被挡 / 超时放行、
改 options 后立刻生效（无需重载）、非法值回落默认、上下限夹紧、无节流内核（同步 / 刷新服务
用的那个）不受影响、实体清单（含关掉 vm 监控时仍在）、unique_id / 名称 / 范围、写入只改一个键、
`extra_state_attributes` 齐全、短 entity_id 规则未被既有规则抢占。

---

## 11. 刷新诊断可见化 + 防重叠

**背景**
把「数据刷新间隔」调小后，实测记录间隔"有时 15 秒、有时 30 秒"，却**没有任何线索**
判断这是"节流生效"还是"那次根本没拉到数据" —— 因为失败分支只打了一行 debug 日志
（`ESXi host is not reachable - skipping update`），在默认日志级别下完全隐形。

**实现**
- `_update_data()` 失败分支改为：**只在"由正常转为失败"时打一次 warning**（持续失败降到
  debug，恢复时给一条 info）。拉不到数据时实体值会停在旧值，在界面上就是"刷新没生效"，
  这是唯一线索，不该被压掉。
- 新增诊断字段：`last_refresh_at`（本次结束时刻）、`last_refresh_seconds`（耗时）、
  `_consecutive_failures`（连续失败次数）。
- `update_data()` 增加**防重叠**：拉一次全量要好几秒（连 ESXi + 遍历全部对象），
  间隔设得比它小时，实体轮询会叠着连 ESXi；上一次没跑完时直接返回（语义与节流一致）。
- `number.update_interval` 的属性新增：`effective`（实际节拍 —— 按轮询粒度向上取整，
  设 10 秒是"约 15 秒"、设 20 秒是"约 30 秒"）、以及 `last_refresh` / `last_refresh_ok` /
  `last_refresh_seconds` / `consecutive_failures`，点开实体就能判断到底有没有在拉数据。

**验证**：stub 加载真实模块，19 项断言全过 —— 失败计数与 warning 时机、恢复后清零、
失败也记录时刻/耗时、防重叠（`_updating` 期间不叠加、结束后复位）、
`effective` 取整表（10→15 / 15→15 / 20→30 / 45→45 / 100→105）、诊断属性按需出现。

---

# 第二部分：附录

## A. 当日新增实体（共 10 类）

| 实体名 | 短 entity_id | 平台 | 归属设备 |
|---|---|---|---|
| 数据刷新间隔 | `number.update_interval` | number | ESXi 主机 |
| 同步实体 | `button.sync` | button | ESXi 主机 |
| 全部虚拟机电源 | `switch.all_vms_power` | switch | ESXi 主机 |
| 全部虚拟机截图 | `switch.all_vms_screenshot` | switch | ESXi 主机 |
| 全部虚拟机快照 | `button.all_vms_snapshot` | button | ESXi 主机 |
| 全部虚拟机重启 | `button.all_vms_restart` | button | ESXi 主机 |
| 全部强制重启 | `button.all_vms_force_restart` | button | ESXi 主机 |
| 强制重启（每台虚拟机一个） | `button.<虚拟机>_force_restart` | button | 各虚拟机设备 |
| 批量分组 | `text.batch_grouping` | text | ESXi 主机 |
| 操作日志 | `sensor.operation_log` | sensor | ESXi 主机 |

**操作日志示例**：

```
state: 全部快照
attributes:
  running: false
  operation: 全部快照
  operation_key: snapshot
  last_status: 已下发
  progress: 6/6
  log:
    - {time: '17:54:57', action: 全部快照, target: —, status: 开始, message: 共 6 台}
    - {time: '17:54:57', action: 全部快照, target: 6 台, status: 下发}
    - {time: '17:54:57', action: 全部快照, target: —, status: 已下发, message: 共 6 台，分 1 批}
```

## B. 新增 / 变更服务

```yaml
# 同步实体清单：为新增对象建实体、把已消失对象的实体移除（可选 entry_id）
service: esxi_stats.sync
data:
  entry_id: 3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d   # 可选，不填 = 全部 ESXi Stats 条目

# 立即刷新数据：越过 45 秒节流拉一次最新值，不增删任何实体（可选 entry_id）
service: esxi_stats.refresh
data:
  entry_id: 3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d   # 可选，不填 = 全部 ESXi Stats 条目
```

## C. 升级注意事项

1. **必须重启 HA 或重载一次集成**，本次所有改动才会生效。
2. **`sensor.<主机>_power_policy` 会被自动删除**（只保留 `select.<主机>_power_policy`，同一个值、可读可写）。
   引用过它的自动化 / 模板 / 仪表盘需改指 `select`。
3. **实体短 id**：新增实体会被重命名为固定短 id（见附录 A）。
   若你改过 `entity_id` 或不想用短 id，可在集成选项里关闭 `short_entity_ids`。
   短 id 只在目标 id 未被占用时生效，否则保留 HA 自动生成的 id。
4. **批量分组默认 `0,0`**（不启用）—— 行为与改动前一致，可以先不动它。
5. **"全部虚拟机"包含运行 HA 自身的那台虚拟机**（若 HA 跑在 ESXi 上，
   例如 `虚拟机 Home Assistant`，以及当路由用的 IstoreOS）。
   点「全部虚拟机电源 → 关闭」会把这些一起关掉，请谨慎。
6. **「强制重启 / 全部强制重启」是硬重置**，等同断电重启：未保存的数据会丢失、
   文件系统可能需要自检。建议只作为系统卡死时的逃生手段。
7. **操作日志不持久化**：HA 重启即清空（刻意如此，它记录的是过程而非状态）。
8. `sensor.entities` 的 `attributes_bytes` 仍在 recorder 的 16384 字节上限之上
   （属性在历史记录里会被丢弃，实时读取、卡片、自动化不受影响）。
9. **同步时重命名虚拟机**：删除旧实体、创建新实体，新的 `entity_id` 与旧的**不同**，
   有引用它的自动化需要跟着改。

## D. 已知限制

1. 同步是**手动触发**的（服务 / 同步按钮），集成不会自动增删实体。
2. 删除实体是从**实体注册表永久移除**（三道守卫兜底，但请知悉）。
3. 批量分组只在"虚拟机数量 > 组大小"时才真正分批。
4. 组间间隔是固定节奏，不代表"上一批已完成"。
5. 操作日志受单条状态属性大小上限约束，条数已做限制。
