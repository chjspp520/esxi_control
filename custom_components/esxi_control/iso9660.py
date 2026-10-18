"""把上传的文件/文件夹打成只读 ISO 镜像（ISO9660 + Joliet）。

为什么不继续手写扇区布局：早期版本手工拼 PVD/SVD/路径表/目录记录，生成的镜像
虽然 Linux 能容错挂载，但大小端路径表等细节不合规，Windows 挂载时报
「磁盘结构损坏且无法读取」。现改用成熟的纯 Python 库 **pycdlib** 生成，产出经
严格解析器校验，Windows / macOS / Linux 均可正常读取。

镜像同时带：

  * PVD（ISO9660 Level 1）——最老设备也能读，文件名是 F0000001.EXT 序号名；
  * SVD（Joliet，UCS-2BE）——Windows / macOS / 现代 Linux 优先读它，
    保留原始长文件名、中文与目录层级。

公开接口只有一个：``build_iso(files, out_path, volume_label)``，
``files`` 为 ``[(镜像内相对路径(用 / 分隔), 本地文件路径), ...]``。
pycdlib 在 ``write`` 时逐文件读取拷贝，不把整盘读进内存。
"""
import os
import datetime

import pycdlib

_SECTOR = 2048

#: PVD 序号名：目录 D0000001、文件 F0000001[.EXT]，天然全局唯一且严格 8.3
_NAME_SERIAL_WIDTH = 7


def _sanitize_iso_char(text):
    """卷标/扩展名用：A-Z 0-9 _，其余转下划线，小写转大写。"""
    out = []
    for ch in text.upper():
        if ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "_"


class _Node:
    """目录树节点（目录与文件共用）。"""

    __slots__ = (
        "name",
        "is_dir",
        "children",
        "src_path",
        "size",
        "mtime",
        "parent",
    )

    def __init__(self, name, is_dir, src_path=None, size=0, mtime=None):
        self.name = name
        self.is_dir = is_dir
        self.children = {} if is_dir else None
        self.src_path = src_path
        self.size = size
        self.mtime = mtime or datetime.datetime(2026, 1, 1)
        self.parent = None

    def walk_dirs_dfs(self):
        """深度优先遍历所有目录（父先于子，同层按名排序）。"""
        yield self
        for child in sorted(self.children.values(), key=lambda n: n.name):
            if child.is_dir:
                yield from child.walk_dirs_dfs()

    def walk_files(self):
        """深度优先列出所有文件（同目录内目录在前、文件在后，各自按名排序）。"""
        kids = sorted(
            self.children.values(),
            key=lambda n: (0 if n.is_dir else 1, n.name),
        )
        for child in kids:
            if child.is_dir:
                yield from child.walk_files()
            else:
                yield child


def _build_tree(files):
    """把 [(arcname, local_path), ...] 收成目录树并做基本校验。

    返回根节点。路径穿越（.. / 绝对路径 / 盘符）一律拒绝，重名自动加序号。
    """
    root = _Node("", True)
    seen = set()

    for arcname, src_path in files:
        # 规范化镜像内路径：反斜杠转正斜杠、去首尾空白与斜杠
        norm = arcname.replace("\\", "/").strip().lstrip("/")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise ValueError(f"非法的镜像内路径: {arcname!r}")
        if len(parts) > 8:
            raise ValueError(f"目录层级过深（>8 级）: {arcname!r}")

        # 防重名（同名文件上传两次时后一个自动加序号，不互相覆盖）
        key = "/".join(parts).lower()
        unique_key = key
        serial = 1
        while unique_key in seen:
            serial += 1
            stem = "/".join(parts[:-1])
            fname = parts[-1]
            if "." in fname:
                base, ext = fname.rsplit(".", 1)
                fname = f"{base}_{serial}.{ext}"
            else:
                fname = f"{fname}_{serial}"
            parts[-1] = fname
            unique_key = (stem + "/" if stem else "") + fname
        seen.add(unique_key)

        node = root
        for depth, part in enumerate(parts):
            is_leaf = depth == len(parts) - 1
            if is_leaf:
                st = os.stat(src_path)
                if st.st_size >= 0xFFFFFFFF * _SECTOR:
                    raise ValueError(f"文件超过 ISO9660 单文件上限: {arcname!r}")
                leaf = _Node(
                    part,
                    False,
                    src_path=src_path,
                    size=st.st_size,
                    mtime=datetime.datetime.fromtimestamp(st.st_mtime),
                )
                leaf.parent = node
                node.children[part] = leaf
            else:
                nxt = node.children.get(part)
                if nxt is None:
                    nxt = _Node(part, True)
                    nxt.parent = node
                    node.children[part] = nxt
                elif not nxt.is_dir:
                    raise ValueError(f"路径冲突（同名文件挡在目录位置）: {part!r}")
                node = nxt
    return root


def _joliet_path(node):
    """节点在 Joliet 树中的绝对路径（原始名，含中文/长名），如 /资料/a.txt。"""
    parts = []
    cur = node
    while cur is not None and cur.parent is not None:  # 根节点 name 为空且 parent=None
        parts.append(cur.name)
        cur = cur.parent
    return "/" + "/".join(reversed(parts))


def build_iso(files, out_path, volume_label="ESXI_CONTROL"):
    """把 files 打成 ISO9660(+Joliet) 镜像写到 out_path。

    :param files: [(arcname, local_path), ...]，arcname 用 ``/`` 分层
    :param out_path: 镜像输出路径（会被覆盖）
    :param volume_label: 卷标（非 ASCII 字符在 PVD/SVD 会被清洗为 ASCII）
    :returns: 镜像总字节数
    """
    files = list(files)
    if not files:
        raise ValueError("没有任何文件可打包")

    root = _build_tree(files)

    # 卷标仅用 ASCII（SVD 卷标与 PVD 同源，pycdlib 对非 ASCII 卷标支持不佳）
    label = _sanitize_iso_char(volume_label.replace(" ", "_"))[:32] or "HA_ISO"

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, joliet=3, vol_ident=label)
    try:
        # 节点 -> 其在 PVD 树中的父目录路径（根为空串）
        parent_iso_path = {id(root): ""}

        # 先建目录（walk_dirs_dfs 保证父先于子）
        dir_serial = 0
        for dnode in root.walk_dirs_dfs():
            if dnode is root:
                continue
            dir_serial += 1
            iso_dir = f"/D{dir_serial:0{_NAME_SERIAL_WIDTH}d}"
            iso.add_directory(
                iso_path=iso_dir,
                joliet_path=_joliet_path(dnode),
            )
            parent_iso_path[id(dnode)] = iso_dir

        # 再放文件
        file_serial = 0
        for fnode in root.walk_files():
            file_serial += 1
            ext = ""
            if "." in fnode.name:
                ext = _sanitize_iso_char(fnode.name.rsplit(".", 1)[1])[:3]
            iso_name = f"F{file_serial:0{_NAME_SERIAL_WIDTH}d}"
            iso_name = f"{iso_name}.{ext};1" if ext else f"{iso_name};1"
            parent_iso = parent_iso_path[id(fnode.parent)]
            iso.add_file(
                fnode.src_path,
                iso_path=f"{parent_iso}/{iso_name}",
                joliet_path=_joliet_path(fnode),
            )

        iso.write(out_path)
    finally:
        iso.close()

    return os.path.getsize(out_path)
