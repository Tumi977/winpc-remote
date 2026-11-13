"""文件读写：路径策略与读/列/写/改的实现。

只做本地文件操作，不依赖 Windows API，Linux 上也能测试。授权（确认框、会话记忆、暂停）在
winpc_agent.py 里做；这里负责：路径解析与敏感路径拦截、编码与换行保持、原子写入、覆盖前备份。
"""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class FileError(Exception):
    """可直接展示给模型的错误。"""


MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_READ_CHARS = 20_000
MAX_READ_CHARS = 100_000
MAX_WRITE_CHARS = 1_000_000
MAX_LIST_ENTRIES = 1000
MAX_LIST_DEPTH = 4
# 二进制传输（get_file / put_file / stat_file）
MAX_CHUNK_BYTES = 2 * 1024 * 1024          # 单次调用的原始字节上限（base64 后约 2.7MB，低于 HTTP 请求体 4MiB 上限）
DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_TRANSFER_MAX_BYTES = 512 * 1024 * 1024

# 中文 Windows 常把桌面/文档重定向到 OneDrive，所以两种都列上；不存在的目录无副作用。
DEFAULT_ROOTS = ["~/Desktop", "~/Documents", "~/OneDrive/Desktop", "~/OneDrive/Documents"]
# 写入这些扩展名会留下可自动执行的内容，默认拒绝（可在 config.json 的 file_blocked_write_ext 调整）
DEFAULT_BLOCKED_WRITE_EXT = [".exe", ".dll", ".sys", ".scr", ".msi", ".com", ".bat", ".cmd", ".ps1", ".psm1",
                             ".vbs", ".vbe", ".wsf", ".hta", ".lnk", ".reg",
                             ".jse", ".wsh", ".pif", ".cpl", ".msc", ".url", ".scf", ".appref-ms", ".application"]

# 敏感路径（读写都拒绝，任何授权模式都生效）。小写、正斜杠，按子串匹配。
_BLOCKED_SUBSTR = [
    "/.ssh/", "/.aws/", "/.gnupg/", "/.azure/", "/.kube/",
    "/appdata/roaming/microsoft/credentials", "/appdata/local/microsoft/credentials",
    "/appdata/roaming/microsoft/protect",
    "/appdata/local/google/chrome/user data", "/appdata/local/microsoft/edge/user data",
    "/appdata/roaming/mozilla/firefox",
    "/start menu/programs/startup",
    "/.docker/", "/.config/gh/", "/.config/gcloud/", "/.claude/",
    "/appdata/local/bravesoftware/", "/appdata/roaming/opera software/", "/appdata/local/vivaldi/",
    "/appdata/roaming/microsoft/windows/powershell/psreadline",   # PowerShell 命令历史，常含令牌
]
_BLOCKED_SUFFIX = (".kdbx", ".kdb", ".pfx", ".p12", ".ppk")
_BLOCKED_NAME_PREFIX = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_BLOCKED_NAMES = {"ntuser.dat", ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
                  "credentials.json", ".credentials.json"}
# 按内容识别私钥：用户的私钥常放在非常规位置、文件名也不像 id_rsa（例如 C:/Users/me/ssh/myserver）
_SECRET_CONTENT = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|PuTTY-User-Key-File-\d")


def _norm(p) -> str:
    return os.path.normcase(str(p)).replace("\\", "/").lower().rstrip("/") or "/"


def _is_within(n: str, root: str) -> bool:
    return n == root or n.startswith(root if root.endswith("/") else root + "/")


def _oserr(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except OSError as e:
            raise FileError(f"文件系统错误: {e.strerror or e}（{type(e).__name__}）") from e
    return wrapper


# --------------------------------------------------------------------------- #
# 路径策略
# --------------------------------------------------------------------------- #
@dataclass
class PathPolicy:
    roots: list[str]               # file_roots 解析后的真实路径（用于展示）
    blocked_dirs: list[str]        # 归一化后的目录前缀：Agent 自身目录、日志目录
    blocked_substr: list[str]
    blocked_write_ext: frozenset[str]

    @classmethod
    def build(cls, roots: list[str], extra_blocked: list[str], blocked_write_ext: list[str],
              blocked_dirs: list[str]) -> "PathPolicy":
        real = lambda s: os.path.realpath(os.path.expanduser(s))  # noqa: E731
        substr = list(_BLOCKED_SUBSTR)
        for s in extra_blocked:
            n = s.replace("\\", "/").lower().strip()
            if n:
                substr.append(n)
        return cls(
            roots=[real(r) for r in roots],
            blocked_dirs=[_norm(real(d)) for d in blocked_dirs],
            blocked_substr=substr,
            blocked_write_ext=frozenset(e.lower() if e.startswith(".") else "." + e.lower()
                                        for e in blocked_write_ext),
        )

    def resolve(self, raw: str) -> Path:
        """把模型给的路径变成真实绝对路径（解析符号链接/联接点），拒绝 UNC、备用数据流等。"""
        s = raw or ""
        if not s.strip():
            raise FileError("path 为空")
        if s.startswith(("\\\\", "//")):
            raise FileError("不支持网络路径（UNC）或设备路径。")
        p = os.path.expanduser(s)
        if not os.path.isabs(p):
            raise FileError("path 必须是绝对路径（可以用 ~ 表示用户主目录）。")
        _drive, rest = os.path.splitdrive(p)
        if ":" in rest:
            raise FileError("路径里不能包含 ':'（NTFS 备用数据流）。")
        for part in rest.replace("\\", "/").split("/"):
            if part not in ("", ".", "..") and part != part.rstrip(". "):
                raise FileError("路径各段不能以空格或点结尾（Windows 会悄悄去掉它们）。")
        return Path(os.path.realpath(p))

    def classify(self, p: Path, write: bool) -> bool:
        """敏感路径直接抛 FileError；返回 True 表示在 file_roots 内。"""
        n = _norm(p)
        for d in self.blocked_dirs:
            if _is_within(n, d):
                raise FileError("该路径在 Agent 自身目录（程序、配置、令牌、日志、备份）内，禁止访问。")
        padded = n + "/"
        name = p.name.lower()
        if (any(sub in padded for sub in self.blocked_substr)
                or name.startswith(_BLOCKED_NAME_PREFIX) or name in _BLOCKED_NAMES
                or name.endswith(_BLOCKED_SUFFIX)):
            raise FileError("该路径属于敏感位置（密钥、凭据、密码库、浏览器数据、启动项等），禁止访问。不要绕过，请告诉用户。")
        if write and p.suffix.lower() in self.blocked_write_ext:
            raise FileError(f"不允许写入 {p.suffix} 类型的文件（可自动执行的内容）。"
                            "如确需，让用户调整 config.json 的 file_blocked_write_ext。")
        return any(_is_within(n, _norm(r)) for r in self.roots)


# --------------------------------------------------------------------------- #
# 编码 / 换行
# --------------------------------------------------------------------------- #
def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as e:
            raise FileError("UTF-16 文件解码失败。") from e
    if b"\x00" in data[:8192]:
        raise FileError("看起来是二进制文件（含 NUL 字节），不支持文本读写。")
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"  # 中文 Windows 旧文本文件常见编码（GBK 的超集）
    except UnicodeDecodeError as e:
        raise FileError("无法按 UTF-8 / GB18030 解码，可能是二进制文件。") from e


def _load(p: Path) -> tuple[str, str, str]:
    """返回 (换行已归一为 \\n 的文本, 编码, 原换行风格 'CRLF'/'LF')。"""
    if p.stat().st_size > MAX_FILE_BYTES:
        raise FileError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MB，不支持。")
    text, enc = _decode(p.read_bytes())
    if _SECRET_CONTENT.search(text[:16384]):
        raise FileError("该文件内容看起来是私钥（BEGIN … PRIVATE KEY），禁止读取或修改。不要绕过，请告诉用户。")
    eol = "CRLF" if "\r\n" in text else "LF"
    return text.replace("\r\n", "\n"), enc, eol


def _atomic_write(p: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace，写到一半失败不会留下半截文件。"""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".winpc-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if p.exists():
            try:
                shutil.copymode(p, tmp)
            except OSError:
                pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup(p: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{p.name}"
    shutil.copy2(p, dest)
    return dest


def _need_file(p: Path) -> None:
    if not p.exists():
        raise FileError(f"文件不存在: {p}")
    if p.is_dir():
        raise FileError(f"{p} 是目录，请用 list_files。")


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
@_oserr
def read_text(p: Path, offset: int = 0, max_chars: int = DEFAULT_READ_CHARS) -> str:
    _need_file(p)
    if offset < 0:
        raise FileError("offset 不能为负数")
    if not 1 <= max_chars <= MAX_READ_CHARS:
        raise FileError(f"max_chars 必须在 1..{MAX_READ_CHARS}")
    text, enc, eol = _load(p)
    total = len(text)
    if total and offset >= total:
        raise FileError(f"offset={offset} 超出文件长度 {total}")
    chunk = text[offset:offset + max_chars]
    end = offset + len(chunk)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    head = f"文件 {p}｜编码 {enc}｜换行 {eol}｜共 {total} 字符 {lines} 行｜本次显示字符 {offset}–{end}"
    if end < total:
        head += f"｜未读完，继续请用 offset={end}"
    return head + "\n---\n" + chunk


@_oserr
def list_dir(p: Path, policy: PathPolicy, pattern: str | None = None, recursive: bool = False,
             max_entries: int = 200) -> str:
    if not p.exists():
        raise FileError(f"目录不存在: {p}")
    if not p.is_dir():
        raise FileError(f"{p} 不是目录，请用 read_file。")
    if not 1 <= max_entries <= MAX_LIST_ENTRIES:
        raise FileError(f"max_entries 必须在 1..{MAX_LIST_ENTRIES}")

    def visible(path: str) -> bool:
        try:
            policy.classify(Path(os.path.realpath(path)), write=False)
            return True
        except FileError:
            return False

    rows: list[str] = []
    truncated = False
    queue: list[tuple[str, int]] = [(str(p), 0)]
    while queue and not truncated:
        d, depth = queue.pop(0)
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except PermissionError:
            rows.append(f"[无权限] {os.path.relpath(d, p)}/")
            continue
        for e in entries:
            if not visible(e.path):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if recursive and is_dir and depth + 1 < MAX_LIST_DEPTH \
                    and _norm(os.path.realpath(e.path)) == _norm(os.path.abspath(e.path)):  # 不进入链接/联接点
                queue.append((e.path, depth + 1))
            if pattern and not fnmatch.fnmatch(e.name.lower(), pattern.lower()):
                continue
            if len(rows) >= max_entries:
                truncated = True
                break
            rel = os.path.relpath(e.path, p).replace("\\", "/")
            try:
                st = e.stat(follow_symlinks=False)
                meta = f"{st.st_size} 字节  " if not is_dir else ""
                meta += datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                meta = "?"
            rows.append(f"{'[目录]' if is_dir else '[文件]'} {rel}{'/' if is_dir else ''}  {meta}")
    head = f"{p} 下显示 {len(rows)} 项" + (f"（已达 max_entries={max_entries}，结果被截断）" if truncated else "")
    if recursive:
        head += f"，递归深度上限 {MAX_LIST_DEPTH}"
    return head + ("\n" + "\n".join(rows) if rows else "（空）")


@_oserr
def write_text(p: Path, content: str, mode: str, backup_dir: Path, create_dirs: bool = False) -> str:
    if mode not in ("create", "overwrite", "append"):
        raise FileError("mode 只能是 create / overwrite / append")
    if len(content) > MAX_WRITE_CHARS:
        raise FileError(f"content 过长（上限 {MAX_WRITE_CHARS} 字符），请分段写入（先 create，再 append）。")
    if p.is_dir():
        raise FileError(f"{p} 是目录")
    exists = p.exists()
    if mode == "create" and exists:
        raise FileError("文件已存在。覆盖请用 mode='overwrite'，追加用 mode='append'；改局部内容用 edit_file。")
    if not p.parent.is_dir():
        if not create_dirs:
            raise FileError(f"目录不存在: {p.parent}（需要的话设置 create_dirs=true）")
        p.parent.mkdir(parents=True)

    enc, eol, old = "utf-8", "LF", ""
    if exists:
        old, enc, eol = _load(p)
    body = content.replace("\r\n", "\n")
    new_text = old + body if mode == "append" else body
    if exists and eol == "CRLF":
        new_text = new_text.replace("\n", "\r\n")
    elif not exists:
        new_text = content  # 新文件：按给定内容原样写入
    note = ""
    if exists:
        note = f"，原文件已备份到 {_backup(p, backup_dir)}"
    _atomic_write(p, new_text.encode(enc))
    verb = {"create": "已创建", "overwrite": "已覆盖" if exists else "已创建", "append": "已追加到"}[mode]
    return f"{verb} {p}（写入 {len(content)} 字符，编码 {enc}{'，换行 CRLF' if eol == 'CRLF' and exists else ''}）{note}。"


@_oserr
def edit_text(p: Path, old: str, new: str, replace_all: bool, backup_dir: Path) -> str:
    _need_file(p)
    text, enc, eol = _load(p)
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old:
        raise FileError("old_string 不能为空（新建/整体写入请用 write_file）。")
    if old == new:
        raise FileError("old_string 与 new_string 相同，没有可修改的内容。")
    count = text.count(old)
    if count == 0:
        raise FileError("没有找到 old_string。必须逐字匹配（含空格、缩进、换行）；请先 read_file 确认当前内容。")
    if count > 1 and not replace_all:
        raise FileError(f"old_string 出现了 {count} 次，不唯一。请带上更多上下文使其唯一，或设置 replace_all=true。")
    first = text.index(old)
    result = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    if len(result) > MAX_FILE_BYTES:
        raise FileError("修改后文件过大。")
    backup = _backup(p, backup_dir)
    _atomic_write(p, (result.replace("\n", "\r\n") if eol == "CRLF" else result).encode(enc))

    line_no = text.count("\n", 0, first) + 1
    lines = result.split("\n")
    lo, hi = max(0, line_no - 1 - 2), min(len(lines), line_no - 1 + new.count("\n") + 1 + 2)
    snippet = "\n".join(f"{i + 1:>5}| {lines[i]}" for i in range(lo, min(hi, lo + 20)))
    return (f"已修改 {p}：替换 {count if replace_all else 1} 处（第 {line_no} 行起），编码 {enc}，换行 {eol}。"
            f"原文件已备份到 {backup}。\n修改后的上下文：\n{snippet[:2000]}")


# --------------------------------------------------------------------------- #
# 二进制文件传输（分块）
# --------------------------------------------------------------------------- #
def part_path(p: Path) -> Path:
    """上传过程中的临时文件（与目标同目录，完成后原子替换目标）。"""
    return p.with_name(f".{p.name}.winpc-part")


def _check_not_secret_bytes(p: Path) -> None:
    """二进制读取同样要按内容拦截私钥（否则 get_file 会绕过 read_file 的检查）。"""
    with p.open("rb") as f:
        head = f.read(16384)
    if _SECRET_CONTENT.search(head.decode("latin-1")):
        raise FileError("该文件内容看起来是私钥（BEGIN … PRIVATE KEY），禁止读取。不要绕过，请告诉用户。")


def _sha256(p: Path, limit: int) -> str:
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            n += len(chunk)
            if n > limit:
                raise FileError(f"文件超过传输上限 {limit // 1024 // 1024} MB。")
            h.update(chunk)
    return h.hexdigest()


def _need_regular_file(p: Path) -> None:
    if not p.exists():
        raise FileError(f"文件不存在: {p}")
    if p.is_dir():
        raise FileError(f"{p} 是目录，请用 list_files。")


@_oserr
def stat_file(p: Path, want_sha256: bool, max_bytes: int) -> dict:
    if not p.exists() and part_path(p).exists():  # 新上传中断：目标还不存在，但可以续传
        return {"path": str(p), "exists": False, "partial_upload_size": part_path(p).stat().st_size}
    _need_regular_file(p)
    _check_not_secret_bytes(p)
    st = p.stat()
    info = {"path": str(p), "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    if want_sha256:
        info["sha256"] = _sha256(p, max_bytes)
    part = part_path(p)
    if part.exists():
        info["partial_upload_size"] = part.stat().st_size  # 未完成的上传，可从这个偏移续传
    return info


@_oserr
def read_chunk(p: Path, offset: int, length: int, max_bytes: int) -> tuple[dict, bytes]:
    _need_regular_file(p)
    if offset < 0:
        raise FileError("offset 不能为负数")
    if not 1 <= length <= MAX_CHUNK_BYTES:
        raise FileError(f"length 必须在 1..{MAX_CHUNK_BYTES}")
    _check_not_secret_bytes(p)
    size = p.stat().st_size
    if size > max_bytes:
        raise FileError(f"文件 {size} 字节，超过传输上限 {max_bytes // 1024 // 1024} MB（config.json 的 transfer_max_bytes）。")
    if offset > size:
        raise FileError(f"offset={offset} 超出文件大小 {size}")
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    return {"path": str(p), "size": size, "offset": offset, "length": len(data),
            "eof": offset + len(data) >= size}, data


@_oserr
def write_chunk(p: Path, data: bytes, offset: int, final: bool, overwrite: bool, create_dirs: bool,
                expected_size: int | None, expected_sha256: str | None, backup_dir: Path,
                max_bytes: int) -> dict:
    """分块上传：先写同目录的 .<name>.winpc-part，final 时校验后原子替换目标。

    offset=0 开始（或重新开始）一次上传；之后每块的 offset 必须等于临时文件当前大小（可用 stat_file 查询以续传）。
    """
    if offset < 0:
        raise FileError("offset 不能为负数")
    if len(data) > MAX_CHUNK_BYTES:
        raise FileError(f"单块不能超过 {MAX_CHUNK_BYTES} 字节，请分块。")
    if offset + len(data) > max_bytes:
        raise FileError(f"超过传输上限 {max_bytes // 1024 // 1024} MB（config.json 的 transfer_max_bytes）。")
    if p.is_dir():
        raise FileError(f"{p} 是目录。上传时 path 必须是目标文件的完整路径。")
    if p.exists() and not overwrite:
        raise FileError("目标文件已存在。覆盖请设置 overwrite=true（原文件会先备份）。")
    if not p.parent.is_dir():
        if not create_dirs:
            raise FileError(f"目录不存在: {p.parent}（需要的话设置 create_dirs=true）")
        p.parent.mkdir(parents=True)
    part = part_path(p)
    if part.is_symlink():
        raise FileError("临时文件是符号链接，已拒绝。")
    if offset == 0:
        mode = "wb"
    else:
        have = part.stat().st_size if part.exists() else None
        if have != offset:
            raise FileError(f"offset={offset} 与已接收的大小 {have if have is not None else '（无临时文件）'} 不一致；"
                            f"从 offset={have or 0} 继续，或用 offset=0 重新开始。")
        mode = "ab"
    with part.open(mode) as f:
        f.write(data)
    received = part.stat().st_size
    if not final:
        return {"path": str(p), "received": received, "done": False}

    def discard(msg: str):
        try:
            part.unlink()
        except OSError:
            pass
        raise FileError(msg)

    if expected_size is not None and received != expected_size:
        discard(f"大小不一致：收到 {received} 字节，期望 {expected_size}。已丢弃临时文件。")
    digest = _sha256(part, max_bytes)
    if expected_sha256 and digest != expected_sha256.lower():
        discard(f"sha256 不一致：收到 {digest}，期望 {expected_sha256.lower()}。已丢弃临时文件。")
    note = None
    if p.exists():
        note = str(_backup(p, backup_dir))
        try:
            shutil.copymode(p, part)
        except OSError:
            pass
    os.replace(part, p)
    out = {"path": str(p), "size": received, "sha256": digest, "done": True}
    if note:
        out["backup"] = note
    return out
