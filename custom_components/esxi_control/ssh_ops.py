"""SSH / SFTP 通道的来宾操作（主要给 Linux 来宾用）。

为什么要有这条通道
------------------
VMware Tools 通道（guest_ops.py）不需要网络可达、凭据也不出 ESXi，但有两个硬限制：

  1. 客户机必须装 open-vm-tools **并且 vgauthd 在跑**，否则整个通道不可用；
  2. vSphere 不提供「命令输出」，只有 start/terminate + 文件读写。

SSH 通道正好补上这两块：不依赖 Tools，SFTP 能读文件、能跑命令拿 stdout。

v1 的设计取舍
-------------
* 用 **asyncssh**（纯 Python、asyncio 原生）：文件传输可能跑几分钟，绝不能占住 HA
  的 executor 线程；连接/超时/取消在 asyncio 里也更自然。整条链路全 await，
  **不进 executor**（Tools 通道那边是阻塞的 pyVmomi，才需要 executor）。
* **每次操作新建连接**：无状态、简单、HA 重启即干净。连接池留到以后再说。
* **host key 用 TOFU**（首次信任）：第一次连记住指纹，之后对不上就**直接拒绝**
  并给出明确提示。重装系统、克隆虚拟机、回滚快照都会换指纹 —— 这时候应该报警，
  而不是像"不校验"那样静默接受。用户也可以在连接设置里显式关掉校验。
* 只支持**密码**认证（v1）。私钥留到以后。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
import stat
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from .guest_ops import GuestOpsError

_LOGGER = logging.getLogger(__name__)

try:  # 库没装上时整条通道"软失败"，不能连累集成加载
    import asyncssh
except Exception:  # pylint: disable=broad-except
    asyncssh = None

#: 建立 TCP+SSH+认证的总超时
CONNECT_TIMEOUT = 8
#: 一次性命令（ps / kill / echo）的超时
COMMAND_TIMEOUT = 20
#: 列目录超时（大目录 + 慢盘）
LIST_TIMEOUT = 45

#: ps 解析时跳过的自身命令行（避免每次刷新都多出一条 ps）
_PS_SELF = ("ps -eo pid=", "ps aux")


class SshOpsError(GuestOpsError):
    """SSH 通道的业务错误。

    继承 ``GuestOpsError``：上层的 except 分支（HTTP 400、服务报错）不用改一行。
    """


if asyncssh is not None:

    class _TofuClient(asyncssh.SSHClient):
        """自己校验服务器主机指纹（TOFU）。

        返回 True/False 而不是 None —— 明确表示"我自己已经判过了"，不要落到
        known_hosts 的默认流程里去。第一次没有期望值就直接接受，连接成功后再
        由调用方把指纹记进存储。
        """

        def __init__(self, expected_fingerprint):
            super().__init__()
            self._expected = expected_fingerprint or ""
            self.seen_fingerprint = ""

        def validate_host_public_key(self, host, addr, port, key):  # noqa: D102
            """⚠️ 这是**同步**回调（asyncssh 在 KEX 阶段直接判返回值）。

            返回 True/False 明确表示"我自己判过了"，不要返回 None 落到 known_hosts
            的默认流程里去。
            """
            self.seen_fingerprint = _fingerprint(key)
            if not self._expected:
                return True
            return self.seen_fingerprint == self._expected

else:  # pragma: no cover - 依赖缺失时的占位
    _TofuClient = None


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _fingerprint(key):
    """主机公钥的 SHA256 指纹（形如 SHA256:xxxx）。"""
    try:
        value = key.get_fingerprint("sha256")
    except Exception:  # pylint: disable=broad-except
        return ""
    if isinstance(value, bytes):      # asyncssh 对 sha256 返回 bytes
        value = value.decode("ascii", "replace")
    return str(value)


def _friendly_error(error, host, port):
    """把 asyncssh / socket 的异常翻译成用户能看懂、能自己排查的中文。"""
    if asyncssh is not None:
        hostkey_error = getattr(asyncssh, "HostKeyNotVerifiable", None)
        if hostkey_error is not None and isinstance(error, hostkey_error):
            return ("主机指纹与上次记录的不一致（重装系统 / 回滚快照 / 克隆虚拟机都会换指纹）。"
                    "确认这台机器就是它之后，在「连接设置」里点「清除已记录的指纹」再连。")
        denied = getattr(asyncssh, "PermissionDenied", None)
        if denied is not None and isinstance(error, denied):
            return f"认证失败：用户名或密码不对（{host}:{port}）"
    if isinstance(error, socket.gaierror):
        return f"解析不了主机名「{host}」，请确认地址写对了（或直接填 IP）"
    if isinstance(error, ConnectionRefusedError):
        return f"{host}:{port} 拒绝连接：目标没开 sshd，或者被防火墙挡了"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return f"连接 {host}:{port} 超时：网络不通（HA 和这台虚拟机在同一网络里吗？）"
    text = str(error) or type(error).__name__
    if isinstance(error, OSError):
        return f"连接 {host}:{port} 失败：{text}"
    return f"SSH 连接失败（{host}:{port}）：{text}"


async def _run(conn, command, timeout=COMMAND_TIMEOUT):
    """执行一条命令；decode 一律用 errors=replace，别让一条乱码把整个响应炸掉。"""
    try:
        return await conn.run(
            command, check=False, timeout=timeout, errors="replace"
        )
    except TypeError:  # 老版本 asyncssh 不认 errors 参数
        return await conn.run(command, check=False, timeout=timeout)


def _stdout(result):
    return (getattr(result, "stdout", "") or "")


def _stderr(result):
    return (getattr(result, "stderr", "") or "").strip()


def _utc_text(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _drain(reader, limit):
    """边读边攒，超过 limit 的部分直接丢弃（**继续读**，免得对端被窗口卡住）。

    为什么要这么做：``conn.run()`` 会把输出整段缓冲在内存里，用户随手打一条
    ``yes`` / ``cat 大文件`` 就能把 HA 的内存吃光。这里改成流式读取 + 上限截断，
    返回 ``(文本, 原始总长度)``。
    """
    chunks = []
    kept = 0
    total = 0
    while True:
        data = await reader.read(65536)
        if not data:
            break
        total += len(data)
        if kept < limit:
            room = limit - kept
            chunks.append(data if len(data) <= room else data[:room])
            kept += min(len(data), room)
    return "".join(chunks), total


def _parse_etime(text):
    """"01:02:03" / "1-02:03:04" / "02:03" → timedelta。"""
    text = str(text or "").strip()
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    try:
        nums = [int(part) for part in text.split(":")]
    except ValueError:
        return None
    if len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _command_name(cmd_line):
    """从命令行取可执行文件名（对应 Tools 通道的 name 字段）。"""
    head = str(cmd_line or "").strip().split(" ")[0]
    if not head:
        return ""
    name = os.path.basename(head) or head
    # 形如 "nginx:" 的进程名去掉尾巴的冒号，看着顺眼一点
    return name[:-1] if name.endswith(":") else name


def _is_self(command):
    return any(command.startswith(mark) for mark in _PS_SELF) or "ps -eo pid=" in command


def _parse_ps(text):
    """解析 ``ps -eo pid=,user=,etime=,args=``（procps-ng）。"""
    rows = []
    now = datetime.now(timezone.utc)
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        pid_text, owner, etime_text = parts[0], parts[1], parts[2]
        cmd_line = parts[3] if len(parts) > 3 else ""
        if not pid_text.isdigit() or _is_self(cmd_line):
            continue
        elapsed = _parse_etime(etime_text)
        rows.append({
            "pid": int(pid_text),
            "name": _command_name(cmd_line),
            "owner": owner,
            "cmd_line": cmd_line,
            "started_at": _utc_text(now - elapsed) if elapsed else "",
            "exit_code": None,
        })
    return rows


def _parse_ps_aux(text):
    """解析 ``ps aux``（busybox / 精简系统的退路）。"""
    rows = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("USER") or line.startswith("PID"):
            continue
        parts = line.split(None, 10)
        if len(parts) < 11 or not parts[1].isdigit():
            continue
        cmd_line = parts[10]
        if _is_self(cmd_line):
            continue
        rows.append({
            "pid": int(parts[1]),
            "name": _command_name(cmd_line),
            "owner": parts[0],
            "cmd_line": cmd_line,
            "started_at": "",          # ps aux 的 START 列语义不统一，宁可不显示
            "exit_code": None,
        })
    return rows


# --------------------------------------------------------------------------- #
# 通道实现
# --------------------------------------------------------------------------- #
class SshOps:
    """一条 SSH 通道的会话入口。

    参数
    ----
    conf          : ``guest_store`` 里这台 VM 的 ``ssh`` 配置（host/port/username/
                    password/verify/hostkey）
    vm_data       : 虚拟机快照（用来在没填 host 时兜底取 ``guest_ip``）
    on_first_seen : ``async def (fingerprint)``，首次看到指纹时回调（TOFU 落库）
    """

    def __init__(self, conf=None, vm_data=None, on_first_seen=None):
        self.conf = dict(conf or {})
        self.vm_data = vm_data or {}
        self.on_first_seen = on_first_seen

    # ---------------- 连接参数 ---------------- #
    def host(self):
        """连哪台机器：优先用户填的，其次虚拟机上报的 IP（来自 VMware Tools）。"""
        host = str(self.conf.get("host") or "").strip()
        if host:
            return host
        ip = str(self.vm_data.get("guest_ip") or "").strip()
        if ip and ip.lower() not in ("n/a", "unknown"):
            return ip
        raise SshOpsError(
            "这台虚拟机没有可用的 IP：客户机没上报（未装 VMware Tools 时拿不到），"
            "请在「连接设置」里手动填写 IP 或主机名"
        )

    def _safe_host(self):
        """给"报错信息"用的：取不到就返回空串，别在错误处理里再抛一次。"""
        try:
            return self.host()
        except SshOpsError:
            return ""

    def port(self):
        try:
            return int(self.conf.get("port") or 22)
        except (TypeError, ValueError):
            return 22

    def verify(self):
        return bool(self.conf.get("verify", True))

    # ---------------- 连接 ---------------- #
    async def _connect(self):
        """建连并完成 host key 校验，返回 (conn, fingerprint)。"""
        if asyncssh is None:
            raise SshOpsError(
                "SSH 通道未就绪：asyncssh 没装上。更新集成后重启 Home Assistant 会自动安装"
            )
        host, port = self.host(), self.port()
        username = str(self.conf.get("username") or "").strip()
        password = str(self.conf.get("password") or "")
        if not username:
            raise SshOpsError("SSH 用户名还没填，请在「连接设置」里补齐")
        if not password:
            raise SshOpsError("SSH 密码还没填（当前只支持密码登录）")

        expected = str(self.conf.get("hostkey") or "") if self.verify() else ""
        client = _TofuClient(expected)
        try:
            conn = await asyncssh.connect(
                host,
                port=port,
                username=username,
                password=password,
                known_hosts=None,          # 校验交给 _TofuClient，不用它的 known_hosts
                client_factory=lambda: client,
                config=None,               # 别去读 HA 容器里 /root/.ssh/config
                preferred_auth=["password"],
                connect_timeout=CONNECT_TIMEOUT,
                login_timeout=CONNECT_TIMEOUT,
            )
        except Exception as error:  # pylint: disable=broad-except
            raise SshOpsError(_friendly_error(error, host, port)) from error

        # 兜底：万一上面的回调没被走到，这里再比一次 —— 宁可多一次，也不静默放行
        try:
            seen = _fingerprint(conn.get_server_host_key())
        except Exception:  # pylint: disable=broad-except
            seen = ""
        seen = seen or client.seen_fingerprint
        if expected and seen and seen != expected:
            # 正常路径上 _TofuClient 在 KEX 阶段就已经拒绝了；走到这里说明那个回调
            # 没生效 —— 宁可多一次比对，也不静默放行（连接立刻断开，不会被使用）
            conn.close()
            raise SshOpsError(
                "主机指纹与上次记录的不一致（重装系统 / 回滚快照 / 克隆虚拟机都会换指纹）。"
                "确认这台机器就是它之后，在「连接设置」里点「清除已记录的指纹」再连。"
            )
        if not expected and seen and self.on_first_seen is not None:
            try:
                await self.on_first_seen(seen)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.warning("记录 SSH 主机指纹失败（不影响本次操作）", exc_info=True)
        return conn, seen

    @asynccontextmanager
    async def _session(self):
        """一次操作一条连接，用完就关（不留状态，HA 重启也不会留下半死连接）。"""
        conn, fingerprint = await self._connect()
        try:
            yield conn, fingerprint
        finally:
            try:
                conn.close()
                await conn.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass

    async def _sftp(self, conn):
        starter = getattr(conn, "start_sftp_client", None) or getattr(conn, "sftp", None)
        if starter is None:
            raise SshOpsError("这台 asyncssh 版本没有 SFTP 客户端，请升级 asyncssh")
        sftp = await starter()
        if sftp is None:  # sshd 没开 sftp 子系统
            raise SshOpsError("目标 sshd 没有启用 SFTP 子系统（Subsystem sftp 未开）")
        return sftp

    # ---------------- 测试 / 校验 ---------------- #
    async def test(self):
        """连一次并跑个 echo：给「测试连接」和凭据校验用，**不抛异常**、只返回结果。"""
        first_seen = not (self.verify() and str(self.conf.get("hostkey") or "").strip())
        try:
            async with self._session() as (conn, fingerprint):
                result = await _run(conn, "echo esxi-stats-ok")
                ok = getattr(result, "exit_status", 1) == 0 and \
                    "esxi-stats-ok" in _stdout(result)
                return {
                    "ok": ok,
                    "error": None if ok else
                    ("连上了但命令没有正常返回，sshd 可能限制了 shell / 用了 sftp-only 的账号"),
                    "host": self._safe_host(),
                    "port": self.port(),
                    "fingerprint": fingerprint,
                    "first_seen": first_seen,
                }
        except SshOpsError as error:
            return {"ok": False, "error": str(error), "host": self._safe_host(),
                    "port": self.port(), "first_seen": first_seen}
        except Exception as error:  # pylint: disable=broad-except
            return {"ok": False, "error": f"SSH 测试失败：{error}", "first_seen": first_seen}

    # ---------------- 进程 ---------------- #
    async def list_processes(self, pids=None):
        """列进程：优先 procps-ng 的 ``ps -eo``，失败退回 ``ps aux``（busybox）。"""
        wanted = {int(x) for x in (pids or [])}
        async with self._session() as (conn, _fp):
            rows = []
            for command in ("ps -eo pid=,user=,etime=,args=", "ps axo pid=,user=,etime=,args="):
                result = await _run(conn, command)
                rows = _parse_ps(_stdout(result))
                if rows:
                    break
            if not rows:
                result = await _run(conn, "ps aux")
                rows = _parse_ps_aux(_stdout(result))
            if not rows:
                raise SshOpsError(
                    "取不到进程列表：这台系统的 ps 参数与预期不同（试试手动跑 ps aux 看输出）"
                )
        if wanted:
            rows = [row for row in rows if row["pid"] in wanted]
        return {"processes": rows, "count": len(rows)}

    async def terminate_process(self, pid):
        try:
            pid_int = int(pid)
        except (TypeError, ValueError) as error:
            raise SshOpsError(f"pid 必须是整数，收到：{pid!r}") from error
        async with self._session() as (conn, _fp):
            result = await _run(conn, f"kill -TERM {pid_int}")
            if getattr(result, "exit_status", 1) != 0:
                raise SshOpsError(
                    "结束进程失败：" + (_stderr(result) or "权限不足或进程已不存在")
                    + "（用普通账号登录时，只能结束自己的进程）"
                )
        return {"pid": pid_int, "terminated": True}

    async def start_program(self, program_path, arguments="", working_directory="",
                            env_vars=None):
        """后台起一个程序（nohup + setsid），返回进程号。"""
        if not program_path:
            raise SshOpsError("program_path 不能为空")
        command = shlex.quote(str(program_path))
        if arguments:
            command += " " + str(arguments)
        prefix = ""
        if working_directory:
            prefix = f"cd {shlex.quote(str(working_directory))} && "
        env = " ".join(shlex.quote(str(item)) for item in (env_vars or []) if item)
        body = f"nohup setsid {command} >/dev/null 2>&1 < /dev/null & echo $!"
        async with self._session() as (conn, _fp):
            result = await _run(conn, prefix + body)
            pid_text = _stdout(result).strip().splitlines()
            pid = pid_text[-1].strip() if pid_text else ""
            if not pid.isdigit():
                # 有些精简系统没有 setsid，退回普通 nohup
                result = await _run(conn, f"{prefix}nohup {command} >/dev/null 2>&1 < /dev/null & echo $!")
                pid_text = _stdout(result).strip().splitlines()
                pid = pid_text[-1].strip() if pid_text else ""
            if not pid.isdigit():
                raise SshOpsError(
                    "启动失败：" + (_stderr(result) or "拿不到新进程号（程序路径对吗？）")
                )
        return {"pid": int(pid), "started": True}

    # ---------------- 命令 ---------------- #
    async def run_command(self, command, timeout=COMMAND_TIMEOUT):
        """跑一条命令并取回输出（终端 tab / guest_exec 服务用）。

        ⚠️ v1 是"一次性、带超时"的执行：不接 stdin、不做 PTY、不做流式输出。
        想跑 `tail -f` 这种不会自己结束的命令，超时到了会被中断并返回提示。
        输出按 ``_MAX_OUTPUT`` 截断，免得一条 `cat` 大文件把响应撑爆。
        """
        text = str(command or "").strip()
        if not text:
            raise SshOpsError("命令不能为空")
        limit = COMMAND_TIMEOUT
        try:
            if timeout:
                limit = max(1, min(int(timeout), 600))
        except (TypeError, ValueError):
            pass
        async with self._session() as (conn, _fp):
            process = None
            try:
                process = await conn.create_process(
                    text, encoding="utf-8", errors="replace"
                )
                # 两个流必须**并发**读：只读 stdout 的话，一条往 stderr 猛写的命令会
                # 被对端的窗口流控卡死，最后表现成"超时"
                (out, out_total), (err, err_total) = await asyncio.wait_for(
                    asyncio.gather(
                        _drain(process.stdout, _MAX_OUTPUT),
                        _drain(process.stderr, _MAX_OUTPUT),
                    ),
                    timeout=limit,
                )
                # 等进程收尾；退出码直接读属性，不依赖 wait() 的返回类型
                await process.wait()
                exit_status = getattr(process, "exit_status", None)
            except (asyncio.TimeoutError, TimeoutError) as error:
                try:
                    if process is not None:
                        process.kill()
                except Exception:  # pylint: disable=broad-except
                    pass
                raise SshOpsError(
                    f"命令执行超过 {limit} 秒被中断（不会自己结束的命令请放后台，"
                    "例如 nohup 命令 &）"
                ) from error
        truncated = out_total > len(out) or err_total > len(err)
        if out_total > len(out):
            out += "\n…（输出过长，已截断）"
        if err_total > len(err):
            err += "\n…（输出过长，已截断）"
        return {
            "command": text,
            "stdout": out,
            "stderr": err,
            "exit_status": exit_status,
            "truncated": truncated,
        }

    # ---------------- 文件 ---------------- #
    async def list_files(self, path, max_results=500):
        target = str(path or "").strip() or "/"
        try:
            limit = max(1, int(max_results or 500))
        except (TypeError, ValueError):
            limit = 500
        async with self._session() as (conn, _fp):
            sftp = await self._sftp(conn)
            try:
                entries = await asyncio.wait_for(sftp.readdir(target), timeout=LIST_TIMEOUT)
            except asyncio.TimeoutError as error:
                raise SshOpsError(f"列目录超时：{target}") from error
            except Exception as error:  # pylint: disable=broad-except
                raise SshOpsError(f"读不了目录 {target}：{error}") from error

            files = []
            truncated = False
            for entry in entries:
                name = getattr(entry, "filename", "")
                if not name or name in (".", ".."):
                    continue
                if len(files) >= limit:
                    truncated = True
                    break
                attrs = getattr(entry, "attrs", None)
                is_dir = await self._entry_is_dir(attrs, sftp, target, name)
                modified = getattr(attrs, "mtime", None)
                files.append({
                    "path": name,
                    "type": "directory" if is_dir else "file",
                    "size": None if is_dir else getattr(attrs, "size", None),
                    "modified": _utc_text(datetime.fromtimestamp(modified, timezone.utc))
                    if modified else "",
                    "owner": None,
                })
        return {"path": target, "files": files, "count": len(files), "truncated": truncated}

    @staticmethod
    async def _entry_is_dir(attrs, sftp, parent, name):
        """判断目录：优先 attrs.type（SFTP v4+ 才有），其次权限位，最后逐个 stat。"""
        dir_type = getattr(asyncssh, "FILEXFER_TYPE_DIRECTORY", None)
        reg_type = getattr(asyncssh, "FILEXFER_TYPE_REGULAR", None)
        ftype = getattr(attrs, "type", None)
        if ftype is not None and dir_type is not None:
            if ftype == dir_type:
                return True
            if ftype == reg_type:
                return False
        permissions = getattr(attrs, "permissions", None)
        if permissions is not None:
            return stat.S_ISDIR(permissions)
        join = f"{str(parent).rstrip('/')}/{name}"
        try:
            info = await sftp.stat(join)
            return stat.S_ISDIR(getattr(info, "permissions", 0) or 0) or \
                getattr(info, "type", None) == dir_type
        except Exception:  # pylint: disable=broad-except
            return False

    async def upload(self, local_path, guest_path, overwrite=True):
        if not local_path or not guest_path:
            raise SshOpsError("local_path 和 guest_path 都不能为空")
        async with self._session() as (conn, _fp):
            sftp = await self._sftp(conn)
            if not overwrite:
                try:
                    await sftp.stat(guest_path)
                    raise SshOpsError(f"目标已存在：{guest_path}")
                except SshOpsError:
                    raise
                except Exception:  # pylint: disable=broad-except
                    pass
            try:
                await sftp.put(local_path, guest_path)
            except Exception as error:  # pylint: disable=broad-except
                raise SshOpsError(f"上传失败：{error}") from error
            size = None
            try:
                size = os.path.getsize(local_path)
            except OSError:
                pass
        return {"guest_path": guest_path, "size": size, "uploaded": True}

    async def download(self, guest_path, local_path):
        if not guest_path or not local_path:
            raise SshOpsError("guest_path 和 local_path 都不能为空")
        async with self._session() as (conn, _fp):
            sftp = await self._sftp(conn)
            try:
                await sftp.get(guest_path, local_path)
            except Exception as error:  # pylint: disable=broad-except
                raise SshOpsError(f"下载失败：{error}") from error
            try:
                size = os.path.getsize(local_path)
            except OSError:
                size = None
        return {"guest_path": guest_path, "size": size, "downloaded": True}

    async def make_directory(self, path, create_parents=True):
        if not path:
            raise SshOpsError("path 不能为空")
        async with self._session() as (conn, _fp):
            sftp = await self._sftp(conn)
            made = False
            makedirs = getattr(sftp, "makedirs", None)
            if create_parents and callable(makedirs):
                try:
                    await makedirs(path, exist_ok=True)
                    made = True
                except Exception as error:  # pylint: disable=broad-except
                    raise SshOpsError(f"创建目录失败：{error}") from error
            if not made:
                mkdir = getattr(sftp, "mkdir", None)
                if callable(mkdir) and not create_parents:
                    try:
                        await mkdir(path)
                        made = True
                    except Exception as error:  # pylint: disable=broad-except
                        raise SshOpsError(f"创建目录失败：{error}") from error
            if not made:  # SFTP 缺方法时退回 shell
                result = await _run(conn, f"mkdir -p -- {shlex.quote(str(path))}")
                if getattr(result, "exit_status", 1) != 0:
                    raise SshOpsError("创建目录失败：" + (_stderr(result) or "未知错误"))
        return {"path": path, "created": True}

    async def remove_path(self, path, recursive=True):
        if not path:
            raise SshOpsError("path 不能为空")
        async with self._session() as (conn, _fp):
            sftp = await self._sftp(conn)
            try:
                if recursive:
                    rmtree = getattr(sftp, "rmtree", None)
                    if callable(rmtree):
                        await rmtree(path)
                    else:
                        result = await _run(conn, f"rm -rf -- {shlex.quote(str(path))}")
                        if getattr(result, "exit_status", 1) != 0:
                            raise SshOpsError("删除失败：" + (_stderr(result) or "未知错误"))
                else:
                    remove = getattr(sftp, "remove", None) or getattr(sftp, "unlink", None)
                    try:
                        if callable(remove):
                            await remove(path)
                        else:
                            raise AttributeError("no remove")
                    except Exception:  # 文件删不掉就当作目录试一次
                        await sftp.rmdir(path)
            except SshOpsError:
                raise
            except Exception as error:  # pylint: disable=broad-except
                raise SshOpsError(f"删除失败：{error}") from error
        return {"path": path, "removed": True}
