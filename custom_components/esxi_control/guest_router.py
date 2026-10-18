"""来宾操作的通道路由：同一组 op 名，按每台 VM 的通道分派到 VMware Tools 或 SSH。

为什么要有这一层
----------------
两条通道（``guest_ops`` 的 pyVmomi Guest Operations / ``ssh_ops`` 的 SSH+SFTP）
在概念上是同一批动作：列进程、结束进程、列目录、上传下载……但对上层来说应该只有
一套接口。所以：

    · 页面 API、服务调用、凭据校验 → 统一走本模块的 ``async_run``；
    · 本模块决定"这台 VM 走哪条通道"（``GuestCredentialManager.resolved_transport``）；
    · 两条通道的实现互不认识，加第三条通道也只需要在这里挂一个分支。

op 名与参数沿用原来页面 API 的那一套（services.yaml 里的服务名去掉 ``guest_`` 前缀
就是 op 名），参数名两条通道共用；少数只在一条通道存在的 op（注册表 / VIX 别名）
在另一条通道上会返回明确的中文错误，而不是 500。
"""
from __future__ import annotations

import functools
import logging

from .guest_ops import (
    GuestOpsError,
    guest_download_file,
    guest_list_aliases,
    guest_list_files,
    guest_list_processes,
    guest_list_registry,
    guest_make_directory,
    guest_remove_path,
    guest_start_program,
    guest_terminate_process,
    guest_upload_file,
    guest_validate,
)
from .guest_store import TRANSPORT_SSH, TRANSPORT_TOOLS
from . import ssh_ops
from .ssh_ops import SshOps, SshOpsError

_LOGGER = logging.getLogger(__name__)


class GuestTarget:
    """一次来宾操作的目标：走哪条通道、用哪套凭据。"""

    __slots__ = ("entry_id", "vm_key", "vm_data", "conn_details",
                 "tools", "ssh", "transport")

    def __init__(self, entry_id, vm_key, vm_data, conn_details,
                 tools, ssh, transport):
        self.entry_id = entry_id
        self.vm_key = vm_key
        self.vm_data = vm_data or {}
        self.conn_details = conn_details or {}
        self.tools = tools
        self.ssh = ssh or {}
        self.transport = transport

    @property
    def moid(self):
        return self.vm_data.get("moid")

    @property
    def vm_name(self):
        return self.vm_data.get("vm_name") or self.vm_key


def build_target(hass, manager, entry_id, vm_key, conn_details=None, vm_data=None):
    """解析出操作目标；配置不全时抛 ``GuestOpsError``（上层翻成 409 + 中文提示）。"""
    if manager is None:
        raise GuestOpsError("来宾配置管理器尚未初始化")
    if conn_details is None or vm_data is None:
        try:
            resolved_conn, resolved_vm = manager.resolve(entry_id, vm_key)
        except KeyError as error:
            raise GuestOpsError(str(error)) from error
        conn_details = conn_details if conn_details is not None else resolved_conn
        vm_data = vm_data if vm_data is not None else resolved_vm

    if not manager.get_cred(entry_id, vm_key):
        raise GuestOpsError(
            "这台虚拟机还没有配置系统连接：打开控制台 → 点顶栏「来宾」按钮 → "
            "点页面右上角那枚凭据徽标（就是「系统连接设置」入口）填写；"
            "VMware Tools 通道的账号也可以在「集成 → 配置 → 来宾凭据」里填"
        )

    transport = manager.resolved_transport(entry_id, vm_key, vm_data)
    tools = manager.get_tools_cred(entry_id, vm_key)
    ssh = manager.get_ssh_conf(entry_id, vm_key)

    if transport == TRANSPORT_SSH and not str(ssh.get("username") or "").strip():
        raise GuestOpsError("这台虚拟机选了 SSH 通道，但还没填 SSH 用户名")
    if transport == TRANSPORT_TOOLS and not tools:
        raise GuestOpsError(
            "这台虚拟机还没填 VMware Tools 通道的账号密码"
            "（可以在「连接设置」里改用 SSH 通道，或补上客户机账号）"
        )
    if transport == TRANSPORT_TOOLS and vm_data.get("moid") is None:
        raise GuestOpsError("虚拟机 moId 缺失，请先刷新数据")

    return GuestTarget(entry_id, vm_key, vm_data, conn_details,
                       tools, ssh, transport)


def _env_list(params):
    """env_vars 允许 dict（页面）或 ["K=V"]（服务），统一成 ["K=V"]。"""
    env = params.get("env_vars")
    if env is None:
        env = params.get("env_variables")
    if isinstance(env, dict):
        return [f"{key}={value}" for key, value in env.items()]
    if isinstance(env, (list, tuple)):
        return [str(item) for item in env if item]
    return None


def _hostkey_recorder(manager, entry_id, vm_key, vm_name=""):
    """给 SSH 通道的 TOFU 用：首次看到主机指纹就落库。"""

    async def remember(fingerprint):
        if manager is None or not fingerprint:
            return
        await manager.async_set_ssh_hostkey(entry_id, vm_key, fingerprint)
        _LOGGER.info("已记录虚拟机 %s 的 SSH 主机指纹（首次信任）", vm_name or vm_key)

    return remember


# --------------------------------------------------------------------------- #
# 通道 1：VMware Tools（阻塞的 pyVmomi，跑在 executor 里）
# --------------------------------------------------------------------------- #
async def _run_tools(hass, target, op, params):
    moid = str(target.moid)

    def call(func, **kwargs):
        return hass.async_add_executor_job(
            functools.partial(func, target.conn_details, moid, target.tools, **kwargs)
        )

    if op == "validate":
        result = await call(guest_validate)
        return {"ok": bool(result.get("ok")), "error": result.get("error"),
                "transport": TRANSPORT_TOOLS}
    if op == "list_processes":
        pids = params.get("pids")
        return await call(
            guest_list_processes,
            pids=[int(item) for item in pids] if pids else None,
        )
    if op == "start_program":
        return await call(
            guest_start_program,
            program_path=str(params.get("program_path") or ""),
            arguments=str(params.get("arguments") or ""),
            working_directory=str(params.get("working_directory") or ""),
            env_vars=_env_list(params),
        )
    if op == "terminate_process":
        return await call(guest_terminate_process, pid=params.get("pid"))
    if op == "list_files":
        return await call(
            guest_list_files,
            path=str(params.get("path") or ""),
            max_results=int(params.get("max_results") or 500),
        )
    if op == "make_directory":
        return await call(
            guest_make_directory,
            path=str(params.get("path") or ""),
            create_parents=bool(params.get("create_parents", True)),
        )
    if op == "remove_path":
        return await call(
            guest_remove_path,
            path=str(params.get("path") or ""),
            recursive=bool(params.get("recursive", True)),
        )
    if op == "upload_file":
        return await call(
            guest_upload_file,
            local_path=str(params.get("local_path") or ""),
            guest_path=str(params.get("guest_path") or ""),
            overwrite=bool(params.get("overwrite", True)),
        )
    if op == "download_file":
        return await call(
            guest_download_file,
            guest_path=str(params.get("guest_path") or ""),
            local_path=str(params.get("local_path") or ""),
        )
    if op == "exec":
        # vSphere 没有"命令输出"这个概念（StartProgramInGuest 拿不到 stdout），
        # 想要输出只能走 SSH —— 这里明确说清楚，别让用户以为是参数写错了
        raise GuestOpsError(
            "执行命令并取回输出需要 SSH 通道：VMware Tools 通道拿不到 stdout/stderr。"
            "请到「连接设置」里把通道改成 SSH"
        )
    if op == "list_aliases":
        return await call(
            guest_list_aliases,
            guest_username=params.get("guest_username") or None,
        )
    if op == "list_registry":
        return await call(
            guest_list_registry,
            key_path=str(params.get("key_path") or ""),
            recursive=bool(params.get("recursive", False)),
        )
    raise GuestOpsError(f"未知操作：{op}")


# --------------------------------------------------------------------------- #
# 通道 2：SSH / SFTP（全异步，不进 executor）
# --------------------------------------------------------------------------- #
async def _run_ssh(manager, target, op, params):
    ops = SshOps(
        target.ssh,
        target.vm_data,
        _hostkey_recorder(manager, target.entry_id, target.vm_key, target.vm_name),
    )

    if op == "validate":
        result = await ops.test()
        result["transport"] = TRANSPORT_SSH
        return result
    if op == "list_processes":
        return await ops.list_processes(params.get("pids"))
    if op == "start_program":
        return await ops.start_program(
            params.get("program_path"),
            params.get("arguments") or "",
            params.get("working_directory") or "",
            _env_list(params),
        )
    if op == "terminate_process":
        return await ops.terminate_process(params.get("pid"))
    if op == "list_files":
        return await ops.list_files(
            params.get("path") or "/", params.get("max_results") or 500
        )
    if op == "make_directory":
        return await ops.make_directory(
            params.get("path"), bool(params.get("create_parents", True))
        )
    if op == "remove_path":
        return await ops.remove_path(
            params.get("path"), bool(params.get("recursive", True))
        )
    if op == "upload_file":
        return await ops.upload(
            params.get("local_path"), params.get("guest_path"),
            bool(params.get("overwrite", True)),
        )
    if op == "download_file":
        return await ops.download(params.get("guest_path"), params.get("local_path"))
    if op == "exec":
        return await ops.run_command(
            params.get("command") or params.get("cmd"),
            params.get("timeout") or ssh_ops.COMMAND_TIMEOUT,
        )
    if op == "list_registry":
        raise SshOpsError(
            "注册表只在 Windows + VMware Tools 通道下可查（SSH 通道没有注册表）"
        )
    if op == "list_aliases":
        raise SshOpsError(
            "Guest 别名（VIX 账号映射）是 VMware Tools 专有能力，SSH 通道没有"
        )
    raise GuestOpsError(f"未知操作：{op}")


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
async def async_run(hass, manager, target, op, params=None):
    """按通道执行一个 op。异常都是 ``GuestOpsError`` 的子类，消息可直接给用户看。"""
    params = params or {}
    if target.transport == TRANSPORT_SSH:
        return await _run_ssh(manager, target, op, params)
    return await _run_tools(hass, target, op, params)


async def async_validate_target(hass, manager, entry_id, vm_key):
    """校验一台 VM 的来宾连接（页面 / 二进传感器 / 服务共用）。

    ⚠️ **永远返回一个结论，不往外抛异常**。校验失败是"结果"，不是"故障"：
    早先把它当普通 op 抛出去，页面上探测失败一次就会显示「凭据不可用」，
    而且那个徽标再也不会刷新 —— 哪怕进程 / 文件其实都能正常用（实测踩过）。

    返回 ``{"ok", "error", "transport", "unconfigured"?}``：
      · ok=True                      通道可用
      · unconfigured=True            还没配 / 通道没配全（不是错，是"待配置"）
      · 其余 ok=False                确实连不上，error 里是原因
    """
    try:
        target = build_target(hass, manager, entry_id, vm_key)
    except GuestOpsError as error:
        return {"ok": False, "error": str(error), "transport": None,
                "unconfigured": True}
    try:
        return await async_run(hass, manager, target, "validate", {})
    except GuestOpsError as error:
        return {"ok": False, "error": str(error), "transport": target.transport}
    except Exception as error:  # pylint: disable=broad-except
        _LOGGER.exception("来宾连接校验出现意外错误")
        return {"ok": False, "error": f"校验失败：{error}",
                "transport": target.transport}


async def async_upload(hass, manager, target, local_path, guest_path, overwrite=True):
    """上传（页面直传视图 / 服务共用）：local_path 在 HA 这一侧。"""
    return await async_run(hass, manager, target, "upload_file", {
        "local_path": local_path, "guest_path": guest_path, "overwrite": overwrite,
    })


async def async_download(hass, manager, target, guest_path, local_path):
    """下载（页面下载视图 / 服务共用）：local_path 在 HA 这一侧。"""
    return await async_run(hass, manager, target, "download_file", {
        "guest_path": guest_path, "local_path": local_path,
    })


async def async_test_conn(manager, entry_id, vm_key, vm_data, payload=None):
    """测试 SSH 连接：用**页面上还没保存**的参数试连（密码留空 = 用已存的）。

    不抛异常，返回 ``{ok, error, fingerprint, first_seen, host, port}`` ——
    「测试连接」要的就是把失败原因原样展示出来。
    """
    ssh_conf = dict(manager.get_ssh_conf(entry_id, vm_key) or {})
    incoming = dict(payload or {})
    for key in ("host", "port", "username", "verify"):
        if key in incoming:
            ssh_conf[key] = incoming[key]
    if incoming.get("password"):
        ssh_conf["password"] = incoming["password"]
    ops = SshOps(
        ssh_conf,
        vm_data,
        _hostkey_recorder(manager, entry_id, vm_key,
                          str((vm_data or {}).get("vm_name") or vm_key)),
    )
    return await ops.test()
