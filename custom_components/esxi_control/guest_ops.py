"""虚拟机「来宾操作」（Guest Operations）后端 —— MOB 的 guestOperationsManager 部分。

对应 MOB 中 content.guestOperationsManager 下的五个管理器：

  · authManager                    —— 校验来宾账号密码
  · processManager                 —— 列进程 / 启动程序 / 结束进程
  · fileManager                    —— 列目录 / 建目录 / 删除 / 上传 / 下载
  · aliasManager                   —— 列来宾别名（证书映射）
  · guestWindowsRegistryManager    —— 查询 Windows 注册表（仅 Windows 来宾）

与控制台票据的区别：控制台票据（webmks/vmrc）是「人看画面、敲键盘」；Guest
Operations 是「拿着客户机操作系统的账号密码，经 VMware Tools 直接做管理动作」，
所以每个虚拟机都需要在集成选项里单独配置一次来宾用户名/密码（见 guest_store）。

所有函数都是**阻塞**的（pyVmomi 同步 API），必须经 executor 调用，与 esxi.py 里
其它函数保持一致；返回值全部用原生 dict/list/str/int，便于直接走 HA 服务响应。
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests
from pyVmomi import vim  # pylint: disable=no-name-in-module

from .esxi import esx_connect, esx_disconnect

_LOGGER = logging.getLogger(__name__)

#: 列目录时单次最多取多少条（翻页拉完，上限防止百万级目录卡死）
_MAX_LIST_FILES = 1000

# requests 调文件传输接口时的 TLS 校验开关：
# ⚠️ requests 的 verify= 参数**不接受 ssl.SSLContext**（2.33 实测会在
# cert_verify() 里对它做 os.path.exists()，报
# "stat: path should be string ... not SSLContext"）——pyVmomi 用的
# ignore_ssl_verify() 上下文只能给 SmartConnect 用，requests 这里只能传 bool。
# ESXi 几乎都是自签名证书，verify=False 并抑制一次 InsecureRequestWarning。
try:
    import urllib3 as _urllib3

    _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
except Exception:  # pragma: no cover - 极老环境无 urllib3 时忽略
    pass


def _requests_verify(conn_details):
    """根据连接配置返回 requests 的 verify 参数（bool）。"""
    return bool(conn_details.get("ssl", False))


class GuestOpsError(Exception):
    """来宾操作失败：消息是中文，可直接抛给前端/服务调用方展示。"""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _auth(vim, cred):
    """构造 NamePasswordAuthentication（Linux/Windows 通用）。

    ⚠️ vSphere 的这个类型只有 ``username`` / ``password`` / ``interactiveSession``
    三个字段，**没有 domain**（见 pyVmomi 的 ``_typeinfo_vim.py``）：
    多传一个 ``domain=`` 会被它直接拒掉 —— ``DataObject.__setattr__`` 找不到该属性时
    抛 ``AttributeError('domain')``，str 出来只有 ``domain`` 两个字，非常难认。
    域账号按 Windows 惯例拼进用户名（``DOMAIN\\user``）；用户自己写成
    ``user@domain`` 形式时不再重复拼。
    """
    if not cred or not cred.get("username") or not cred.get("password"):
        raise GuestOpsError("该虚拟机尚未配置来宾用户名/密码，请先在集成选项中配置")
    username = str(cred["username"]).strip()
    domain = str(cred.get("domain") or "").strip()
    if domain and "\\" not in username and "@" not in username:
        username = f"{domain}\\{username}"
    try:
        return vim.vm.guest.NamePasswordAuthentication(
            username=username,
            password=str(cred["password"]),
            # 非交互式会话：我们只做 API 调用，不需要「已登录的交互桌面」
            interactiveSession=False,
        )
    except Exception as error:  # pylint: disable=broad-except
        raise GuestOpsError(f"构造来宾认证对象失败：{error}") from error


def _find_vm(content, moid):
    """按 moId 找虚拟机对象。"""
    obj_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for virtual_machine in obj_view.view:
            if getattr(virtual_machine, "_moId", None) == str(moid):
                return virtual_machine
    finally:
        obj_view.Destroy()
    return None


def _ready(virtual_machine):
    """来宾操作的前提：开机 + Tools 在跑。返回 (ok, 中文原因)。"""
    runtime = virtual_machine.runtime
    guest = getattr(virtual_machine, "guest", None)
    if str(getattr(runtime, "powerState", "")) != "poweredOn":
        return False, "虚拟机未开机"
    tools_status = str(getattr(guest, "toolsRunningStatus", "") or "")
    if tools_status != "guestToolsRunning":
        return False, f"VMware Tools 未运行（当前：{tools_status or '未知'}）"
    return True, ""


def _dt(value):
    """安全地把 pyVmomi 的 datetime 转成字符串。"""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return value


#: RegistryValueData 的具体子类（vmodl 名最后一段）→ 注册表类型名
_REG_TYPE_BY_CLASS = {
    "RegistryValueDword": "REG_DWORD",
    "RegistryValueQword": "REG_QWORD",
    "RegistryValueString": "REG_SZ",
    "RegistryValueExpandString": "REG_EXPAND_SZ",
    "RegistryValueMultiString": "REG_MULTI_SZ",
    "RegistryValueBinary": "REG_BINARY",
}


def _reg_type(data):
    """把注册表值的具体类型对象映射成 REG_* 名字。

    ⚠️ pyVmomi 的数据对象类名是**完整 vmodl 名**（如
    ``vim.vm.guest.WindowsRegistryManager.RegistryValueDword``），取最后一段再查表。
    基类 ``RegistryValueData`` 自己没有 value 字段，真正的值在具体子类上。
    """
    if data is None:
        return None
    short = type(data).__name__.rsplit(".", 1)[-1]
    return _REG_TYPE_BY_CLASS.get(short, short)


def _guest_ops(content):
    """取 guestOperationsManager（5.x 上可能整体缺失）。"""
    manager = getattr(content, "guestOperationsManager", None)
    if manager is None:
        raise GuestOpsError("该主机不提供 guestOperationsManager（版本过低或被禁用）")
    return manager


def _run(conn_details, moid, cred, worker):
    """统一的连接/找 VM/检查 Tools/执行/收尾包装。

    ``worker(content, gom, vm, auth, vim_mod)`` 返回的任何原生结构都会直接回传。
    """
    # ⚠️ `vim` 必须是**模块级**导入：同模块里的 _find_vm() 直接用它（`vim.VirtualMachine`），
    #   而 _find_vm 不在这个函数的作用域里 —— 以前这里写的是局部 `from pyVmomi import vim`，
    #   结果每次来宾操作都在「按 moId 找虚拟机」一步抛 NameError: name 'vim' is not defined，
    #   被下面的兜底 catch 包成 GuestOpsError 返回，表现为校验按钮报「来宾凭据校验失败」。
    conn = esx_connect(**conn_details)
    if not conn:
        raise GuestOpsError("无法连接到 ESXi 主机")
    try:
        content = conn.RetrieveContent()
        virtual_machine = _find_vm(content, moid)
        if virtual_machine is None:
            raise GuestOpsError(f"找不到 moId 为 {moid} 的虚拟机")
        ok, reason = _ready(virtual_machine)
        if not ok:
            raise GuestOpsError(reason)
        gom = _guest_ops(content)
        auth = _auth(vim, cred)
        return worker(content, gom, virtual_machine, auth, vim)
    except GuestOpsError:
        raise
    except Exception as error:  # pylint: disable=broad-except
        msg = getattr(error, "msg", None) or str(error) or error.__class__.__name__
        raise GuestOpsError(str(msg)) from error
    finally:
        esx_disconnect(conn)


def _rewrite_transfer_url(url, config_host):
    """ESXi 返回的传输 URL 主机名可能是 '*' 或内部名，统一换成连接用的主机。"""
    if not url:
        return url
    head, _, tail = url.partition("://")
    if not tail:
        return url
    _host_part, _, rest = tail.partition("/")
    # _host_part 形如 host:port；保留端口，只替换主机
    port = ""
    if ":" in _host_part:
        port = ":" + _host_part.rsplit(":", 1)[1]
    return f"{head}://{config_host}{port}/{rest}"


# --------------------------------------------------------------------------- #
# 1) 认证（authManager）
# --------------------------------------------------------------------------- #
def guest_validate(conn_details, moid, cred):
    """校验来宾账号密码是否有效。返回 {ok, error}，不抛业务异常。"""

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        try:
            gom.authManager.ValidateCredentialsInGuest(virtual_machine, auth)
            return {"ok": True, "error": None}
        except Exception as error:  # pylint: disable=broad-except
            return {"ok": False, "error": getattr(error, "msg", None) or str(error)}

    try:
        return _run(conn_details, moid, cred, worker)
    except GuestOpsError as error:
        return {"ok": False, "error": str(error)}


# --------------------------------------------------------------------------- #
# 2) 进程（processManager）
# --------------------------------------------------------------------------- #
def guest_list_processes(conn_details, moid, cred, pids=None):
    """列出来宾内进程（默认全部；pids 指定时只列这些 PID）。"""

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        proc_infos = gom.processManager.ListProcessesInGuest(
            virtual_machine, auth, list(pids) if pids else []
        )
        result = []
        for info in proc_infos or []:
            result.append(
                {
                    "pid": getattr(info, "pid", None),
                    "name": getattr(info, "name", ""),
                    "owner": getattr(info, "owner", ""),
                    "cmd_line": getattr(info, "cmdLine", ""),
                    "started_at": _dt(getattr(info, "startTime", None)),
                    "exit_code": getattr(info, "exitCode", None),
                }
            )
        return {"processes": result, "count": len(result)}

    return _run(conn_details, moid, cred, worker)


def guest_start_program(conn_details, moid, cred, program_path,
                        arguments="", working_directory="", env_vars=None):
    """在来宾内启动程序，返回进程 PID。"""
    if not program_path:
        raise GuestOpsError("program_path 不能为空")

    def worker(_content, gom, virtual_machine, auth, vim_mod):
        spec = vim_mod.vm.guest.ProcessManager.ProgramSpec(
            programPath=program_path,
            arguments=arguments or "",
            workingDirectory=working_directory or "",
        )
        if env_vars:
            # envVars 是字符串列表，元素形如 "KEY=VALUE"
            spec.envVariables = [str(item) for item in env_vars]
        pid = gom.processManager.StartProgramInGuest(virtual_machine, auth, spec)
        return {"pid": int(pid)}

    return _run(conn_details, moid, cred, worker)


def guest_terminate_process(conn_details, moid, cred, pid):
    """结束来宾内的指定进程。"""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError) as error:
        raise GuestOpsError(f"pid 必须是整数，收到：{pid!r}") from error

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        gom.processManager.TerminateProcessInGuest(virtual_machine, auth, pid_int)
        return {"pid": pid_int, "terminated": True}

    return _run(conn_details, moid, cred, worker)


# --------------------------------------------------------------------------- #
# 3) 文件（fileManager）
# --------------------------------------------------------------------------- #
def _file_info(info):
    """把 GuestFileInfo 拍平成 dict。

    ⚠️ 时间与属主都不在 FileInfo 自己身上，而在它的 ``attributes`` 里
    （``FileAttributes.modificationTime`` / ``accessTime``；``ownerId`` 只有
    Posix 属性才有）—— 直接读 ``info.modification`` / ``info.owner`` 恒为 None。
    """
    attrs = getattr(info, "attributes", None)
    return {
        "path": getattr(info, "path", ""),
        "type": getattr(info, "type", ""),
        "size": getattr(info, "size", None),
        "modified": _dt(getattr(attrs, "modificationTime", None)),
        "owner": getattr(attrs, "ownerId", None),
    }


def guest_list_files(conn_details, moid, cred, path, max_results=200):
    """列出来宾目录内容（自动翻页）。"""
    path = path or "/"
    max_results = min(int(max_results or 200), _MAX_LIST_FILES)

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        files = []
        index = 0
        while True:
            result = gom.fileManager.ListFilesInGuest(
                virtual_machine, auth, path, index=index, maxResults=max_results
            )
            for info in getattr(result, "files", None) or []:
                files.append(_file_info(info))
                if len(files) >= max_results:
                    return {"path": path, "files": files, "count": len(files),
                            "truncated": True}
            # ⚠️ 字段名是 remaining（vmodl：FileManager.ListFileInfo），不是
            #   remainingIndex —— 下一页的起始下标，-1 表示没有更多。
            remainder = getattr(result, "remaining", None)
            if remainder is None or int(remainder) < 0 or int(remainder) <= index:
                break
            index = int(remainder)
        return {"path": path, "files": files, "count": len(files), "truncated": False}

    return _run(conn_details, moid, cred, worker)


def guest_make_directory(conn_details, moid, cred, path, create_parents=True):
    """在来宾内创建目录。"""
    if not path:
        raise GuestOpsError("path 不能为空")

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        gom.fileManager.MakeDirectoryInGuest(
            virtual_machine, auth, path, bool(create_parents)
        )
        return {"path": path, "created": True}

    return _run(conn_details, moid, cred, worker)


def guest_remove_path(conn_details, moid, cred, path, recursive=True):
    """删除来宾文件或目录（先试文件，再试目录）。"""
    if not path:
        raise GuestOpsError("path 不能为空")

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        try:
            gom.fileManager.DeleteFileInGuest(virtual_machine, auth, path)
            return {"path": path, "type": "file", "removed": True}
        except Exception:  # pylint: disable=broad-except
            gom.fileManager.DeleteDirectoryInGuest(
                virtual_machine, auth, path, bool(recursive)
            )
            return {"path": path, "type": "directory", "removed": True}

    return _run(conn_details, moid, cred, worker)


def _file_attributes(vim_mod, virtual_machine):
    """按来宾类型给上传用的文件属性（Windows / Linux 各一套）。"""
    family = str(getattr(getattr(virtual_machine, "guest", None), "guestFamily", "") or "").lower()
    if family.startswith("win"):
        return vim_mod.vm.guest.FileManager.WindowsFileAttributes()
    attrs = vim_mod.vm.guest.FileManager.PosixFileAttributes()
    attrs.permissions = 0o755
    return attrs


def guest_upload_file(conn_details, moid, cred, local_path, guest_path, overwrite=True):
    """把 HA 本机文件上传到来宾路径（InitiateFileTransferToGuest + HTTPS PUT）。"""
    if not local_path or not guest_path:
        raise GuestOpsError("local_path 和 guest_path 都不能为空"
                            )

    def worker(_content, gom, virtual_machine, auth, vim_mod):
        import os

        if not os.path.isfile(local_path):
            raise GuestOpsError(f"HA 本机文件不存在：{local_path}")
        file_size = os.path.getsize(local_path)
        url = gom.fileManager.InitiateFileTransferToGuest(
            virtual_machine,
            auth,
            guest_path,
            _file_attributes(vim_mod, virtual_machine),
            file_size,
            bool(overwrite),
        )
        url = _rewrite_transfer_url(url, conn_details["host"])
        with open(local_path, "rb") as handle:
            put_resp = requests.put(
                url,
                data=handle,
                verify=_requests_verify(conn_details),
                headers={"Content-Type": "application/octet-stream"},
                timeout=120,
            )
        if put_resp.status_code >= 400:
            raise GuestOpsError(
                f"上传失败：HTTP {put_resp.status_code} {put_resp.text[:200]}"
            )
        return {"guest_path": guest_path, "size": file_size, "uploaded": True}

    return _run(conn_details, moid, cred, worker)


def guest_download_file(conn_details, moid, cred, guest_path, local_path):
    """把来宾文件下载到 HA 本机（InitiateFileTransferFromGuest + HTTPS GET）。"""
    if not guest_path or not local_path:
        raise GuestOpsError("guest_path 和 local_path 都不能为空")

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        import os

        info = gom.fileManager.InitiateFileTransferFromGuest(
            virtual_machine, auth, guest_path
        )
        url = _rewrite_transfer_url(getattr(info, "url", ""), conn_details["host"])
        os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
        with requests.get(url, verify=_requests_verify(conn_details),
                          stream=True, timeout=120) as resp:
            if resp.status_code >= 400:
                raise GuestOpsError(
                    f"下载失败：HTTP {resp.status_code} {resp.text[:200]}"
                )
            with open(local_path, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
        return {"guest_path": guest_path, "local_path": os.path.abspath(local_path),
                "size": getattr(info, "size", None), "downloaded": True}

    return _run(conn_details, moid, cred, worker)


# --------------------------------------------------------------------------- #
# 4) 别名（aliasManager）
# --------------------------------------------------------------------------- #
def guest_list_aliases(conn_details, moid, cred, guest_username=None):
    """列出来宾用户的 Guest 别名（证书/VIX 映射等，一般运维只需只读查看）。"""

    def worker(_content, gom, virtual_machine, auth, _vim_mod):
        username = guest_username or cred["username"]
        # ⚠️ 返回的是 GuestAliases[]（不是 GuestAlias[]）：
        #   每个元素 {base64Cert, aliases: [{subject, comment}]}，
        #   subject 是基类 GuestAuthSubject（具体为 GuestAuthNamedSubject.name
        #   或没有任何字段的 GuestAuthAnySubject）—— 没有 username/mappingCert 字段。
        groups = gom.aliasManager.ListGuestAliases(virtual_machine, auth, username) or []
        items = []
        cert_present = False
        for group in groups:
            cert = getattr(group, "base64Cert", None)
            if cert:
                cert_present = True
            for alias in getattr(group, "aliases", None) or []:
                subject = getattr(alias, "subject", None)
                items.append(
                    {
                        "username": username,
                        "subject": getattr(subject, "name", None) or "任意（AnySubject）",
                        "comment": getattr(alias, "comment", None),
                        "mapping_cert_present": bool(cert),
                    }
                )
        return {
            "guest_username": username,
            "aliases": items,
            "count": len(items),
            "mapping_cert_present": cert_present,
        }

    return _run(conn_details, moid, cred, worker)


# --------------------------------------------------------------------------- #
# 5) Windows 注册表（guestWindowsRegistryManager）
# --------------------------------------------------------------------------- #
def guest_list_registry(conn_details, moid, cred, key_path, recursive=False):
    """查询 Windows 来宾注册表某键下的子键与值（只读）。

    key_path 必须是完整注册表路径，例如：
      HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion
    """
    if not key_path:
        raise GuestOpsError("key_path 不能为空")

    def worker(_content, gom, virtual_machine, auth, vim_mod):
        reg_mgr = getattr(gom, "guestWindowsRegistryManager", None)
        if reg_mgr is None:
            raise GuestOpsError(
                "该主机/客户机不提供 Windows 注册表管理器（仅支持 Windows 来宾）"
            )
        # 方法名（复数）与参数都要按 vSphere 原文：
        #   ListRegistryKeysInGuest(vm, auth, keyName, recursive, matchPattern)
        #   ListRegistryValuesInGuest(vm, auth, keyName, expandStrings, matchPattern)
        #   第 4 个参数分别是「是否递归子键」与「是否展开 REG_EXPAND_SZ」，不是同一个东西。
        #
        # ⚠️ keyName 不是裸字符串，必须是 RegistryKeyName(registryPath, wowBitness)；
        #   wowBitness 必填，取值为枚举 GuestRegKeyWowSpec：WOWNative/WOW32/WOW64
        #   （实测传空串或旧文档里的 wowBitness32_64 都会被 ESXi 以
        #   InvalidArgument "wow bitness read" 拒绝）。两个 List 方法的第 3 参
        #   都是 RegistryKeyName（RegistryValueName 只出现在返回值里）。
        #
        # ⚠️ 返回值的形状（vmodl，实测 pyVmomi 8.0.3）：
        #   ListRegistryKeysInGuest  -> RegistryKeyRecord[]
        #       record.key（RegistryKey）→ keyName.registryPath / keyName.wowBitness /
        #       classType / lastWritten；record.fault 有值表示这个子键读不到。
        #   ListRegistryValuesInGuest -> RegistryValue[]
        #       record.name（RegistryValueName）→ keyName.registryPath / name；
        #       record.data 是 RegistryValue* 的具体子类，值在它的 value 字段上。
        def _key_name(path):
            return vim_mod.vm.guest.WindowsRegistryManager.RegistryKeyName(
                registryPath=path, wowBitness="WOWNative"
            )

        key_records = reg_mgr.ListRegistryKeysInGuest(
            virtual_machine, auth, _key_name(key_path), bool(recursive), ""
        ) or []
        keys = []
        value_paths = []
        skipped = 0
        for record in key_records:
            key_spec = getattr(record, "key", None)
            key_name = getattr(key_spec, "keyName", None)
            record_path = getattr(key_name, "registryPath", None) or key_path
            fault = getattr(record, "fault", None)
            keys.append(
                {
                    "path": record_path,
                    "wow_bitness": getattr(key_name, "wowBitness", None),
                    "class_type": getattr(key_spec, "classType", None),
                    "last_written": _dt(getattr(key_spec, "lastWritten", None)),
                    # 个别子键可能没权限/读不到：记下原因，但不要让整次查询失败
                    "error": getattr(fault, "msg", None) if fault is not None else None,
                }
            )
            if fault is not None:
                skipped += 1
                continue
            # 递归时子键的「值」要逐键再查一次（列表接口只给子键名）
            if recursive and record_path not in value_paths:
                value_paths.append(record_path)

        # 查询的那个键本身不会出现在子键列表里，但它的值必须查
        if key_path not in value_paths:
            value_paths.insert(0, key_path)

        values = []
        for one_path in value_paths:
            value_records = reg_mgr.ListRegistryValuesInGuest(
                virtual_machine, auth, _key_name(one_path), True, ""
            ) or []
            for record in value_records:
                name_spec = getattr(record, "name", None)
                data = getattr(record, "data", None)
                values.append(
                    {
                        "key_path": (
                            getattr(getattr(name_spec, "keyName", None), "registryPath", None)
                            or one_path
                        ),
                        "name": getattr(name_spec, "name", ""),
                        "type": _reg_type(data),
                        "value": getattr(data, "value", None),
                    }
                )
        return {"key_path": key_path, "keys": keys, "values": values,
                "key_count": len(keys), "value_count": len(values),
                "unreadable_keys": skipped}

    return _run(conn_details, moid, cred, worker)
