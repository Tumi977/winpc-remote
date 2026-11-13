"""File read/write: path policy and the implementation of read / list / write / edit.

Local file operations only; no dependency on Windows APIs, so it can also be tested on Linux. Authorization (dialogs, session memory, pause) is done in
winpc_agent.py; this module handles: path resolution and sensitive-path blocking, encoding and newline preservation, atomic writes, backup before overwrite.
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
    """An error message that can be shown directly to the model."""


MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_READ_CHARS = 20_000
MAX_READ_CHARS = 100_000
MAX_WRITE_CHARS = 1_000_000
MAX_LIST_ENTRIES = 1000
MAX_LIST_DEPTH = 4
# Binary transfer (get_file / put_file / stat_file)
MAX_CHUNK_BYTES = 2 * 1024 * 1024          # upper limit of raw bytes per call (about 2.7 MB after base64, below the 4 MiB HTTP request body limit)
DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_TRANSFER_MAX_BYTES = 512 * 1024 * 1024

# Chinese-locale Windows often redirects Desktop/Documents to OneDrive, so both are listed; a directory that does not exist has no side effects.
DEFAULT_ROOTS = ["~/Desktop", "~/Documents", "~/OneDrive/Desktop", "~/OneDrive/Documents"]
# Writing these extensions leaves auto-executable content behind, so it is refused by default (adjustable via file_blocked_write_ext in config.json)
DEFAULT_BLOCKED_WRITE_EXT = [".exe", ".dll", ".sys", ".scr", ".msi", ".com", ".bat", ".cmd", ".ps1", ".psm1",
                             ".vbs", ".vbe", ".wsf", ".hta", ".lnk", ".reg",
                             ".jse", ".wsh", ".pif", ".cpl", ".msc", ".url", ".scf", ".appref-ms", ".application"]

# Sensitive paths (refused for read and write, in every approval mode). Lowercase, forward slashes, matched as substrings.
_BLOCKED_SUBSTR = [
    "/.ssh/", "/.aws/", "/.gnupg/", "/.azure/", "/.kube/",
    "/appdata/roaming/microsoft/credentials", "/appdata/local/microsoft/credentials",
    "/appdata/roaming/microsoft/protect",
    "/appdata/local/google/chrome/user data", "/appdata/local/microsoft/edge/user data",
    "/appdata/roaming/mozilla/firefox",
    "/start menu/programs/startup",
    "/.docker/", "/.config/gh/", "/.config/gcloud/", "/.claude/",
    "/appdata/local/bravesoftware/", "/appdata/roaming/opera software/", "/appdata/local/vivaldi/",
    "/appdata/roaming/microsoft/windows/powershell/psreadline",   # PowerShell command history, often contains tokens
]
_BLOCKED_SUFFIX = (".kdbx", ".kdb", ".pfx", ".p12", ".ppk")
_BLOCKED_NAME_PREFIX = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_BLOCKED_NAMES = {"ntuser.dat", ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
                  "credentials.json", ".credentials.json"}
# Detect private keys by content: users often keep them in unusual places under names that do not look like id_rsa (e.g. C:/Users/me/ssh/myserver)
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
            raise FileError(f"File system error: {e.strerror or e} ({type(e).__name__})") from e
    return wrapper


# --------------------------------------------------------------------------- #
# Path policy
# --------------------------------------------------------------------------- #
@dataclass
class PathPolicy:
    roots: list[str]               # resolved real paths of file_roots (for display)
    blocked_dirs: list[str]        # normalized directory prefixes: the Agent's own directory, the log directory
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
        """Turn the path given by the model into a real absolute path (resolving symlinks/junctions); reject UNC, alternate data streams, etc."""
        s = raw or ""
        if not s.strip():
            raise FileError("path is empty")
        if s.startswith(("\\\\", "//")):
            raise FileError("Network paths (UNC) and device paths are not supported.")
        p = os.path.expanduser(s)
        if not os.path.isabs(p):
            raise FileError("path must be absolute (~ may be used for the user's home directory).")
        _drive, rest = os.path.splitdrive(p)
        if ":" in rest:
            raise FileError("A path must not contain ':' (NTFS alternate data streams).")
        for part in rest.replace("\\", "/").split("/"):
            if part not in ("", ".", "..") and part != part.rstrip(". "):
                raise FileError("Path components must not end with a space or a dot (Windows silently strips them).")
        return Path(os.path.realpath(p))

    def classify(self, p: Path, write: bool) -> bool:
        """Sensitive paths raise FileError directly; returns True if the path is inside file_roots."""
        n = _norm(p)
        for d in self.blocked_dirs:
            if _is_within(n, d):
                raise FileError("This path is inside the Agent's own directory (program, config, token, logs, backups); access is forbidden.")
        padded = n + "/"
        name = p.name.lower()
        if (any(sub in padded for sub in self.blocked_substr)
                or name.startswith(_BLOCKED_NAME_PREFIX) or name in _BLOCKED_NAMES
                or name.endswith(_BLOCKED_SUFFIX)):
            raise FileError("This path is a sensitive location (keys, credentials, password vaults, browser data, startup items, etc.); access is forbidden. Do not work around this; tell the user.")
        if write and p.suffix.lower() in self.blocked_write_ext:
            raise FileError(f"Writing files of type {p.suffix} is not allowed (auto-executable content). "
                            "If really needed, ask the user to adjust file_blocked_write_ext in config.json.")
        return any(_is_within(n, _norm(r)) for r in self.roots)


# --------------------------------------------------------------------------- #
# Encoding / newlines
# --------------------------------------------------------------------------- #
def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as e:
            raise FileError("Failed to decode the UTF-16 file.") from e
    if b"\x00" in data[:8192]:
        raise FileError("This looks like a binary file (contains NUL bytes); text read/write is not supported.")
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"  # common encoding of old text files on Chinese Windows (a superset of GBK)
    except UnicodeDecodeError as e:
        raise FileError("Cannot decode as UTF-8 / GB18030; it may be a binary file.") from e


def _load(p: Path) -> tuple[str, str, str]:
    """Return (text with newlines normalized to \n, encoding, original newline style 'CRLF'/'LF')."""
    if p.stat().st_size > MAX_FILE_BYTES:
        raise FileError(f"The file is larger than {MAX_FILE_BYTES // 1024 // 1024} MB; not supported.")
    text, enc = _decode(p.read_bytes())
    if _SECRET_CONTENT.search(text[:16384]):
        raise FileError("This file looks like a private key (BEGIN ... PRIVATE KEY); reading or modifying it is forbidden. Do not work around this; tell the user.")
    eol = "CRLF" if "\r\n" in text else "LF"
    return text.replace("\r\n", "\n"), enc, eol


def _atomic_write(p: Path, data: bytes) -> None:
    """Temp file in the same directory + os.replace; a failure halfway never leaves a half-written file."""
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
        raise FileError(f"File does not exist: {p}")
    if p.is_dir():
        raise FileError(f"{p} is a directory; use list_files.")


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
@_oserr
def read_text(p: Path, offset: int = 0, max_chars: int = DEFAULT_READ_CHARS) -> str:
    _need_file(p)
    if offset < 0:
        raise FileError("offset must not be negative")
    if not 1 <= max_chars <= MAX_READ_CHARS:
        raise FileError(f"max_chars must be in 1..{MAX_READ_CHARS}")
    text, enc, eol = _load(p)
    total = len(text)
    if total and offset >= total:
        raise FileError(f"offset={offset} is beyond the file length {total}")
    chunk = text[offset:offset + max_chars]
    end = offset + len(chunk)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    head = f"File {p} | encoding {enc} | newlines {eol} | {total} characters, {lines} lines | showing characters {offset}-{end}"
    if end < total:
        head += f" | not fully read; continue with offset={end}"
    return head + "\n---\n" + chunk


@_oserr
def list_dir(p: Path, policy: PathPolicy, pattern: str | None = None, recursive: bool = False,
             max_entries: int = 200) -> str:
    if not p.exists():
        raise FileError(f"Directory does not exist: {p}")
    if not p.is_dir():
        raise FileError(f"{p} is not a directory; use read_file.")
    if not 1 <= max_entries <= MAX_LIST_ENTRIES:
        raise FileError(f"max_entries must be in 1..{MAX_LIST_ENTRIES}")

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
            rows.append(f"[no permission] {os.path.relpath(d, p)}/")
            continue
        for e in entries:
            if not visible(e.path):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if recursive and is_dir and depth + 1 < MAX_LIST_DEPTH \
                    and _norm(os.path.realpath(e.path)) == _norm(os.path.abspath(e.path)):  # do not descend into links/junctions
                queue.append((e.path, depth + 1))
            if pattern and not fnmatch.fnmatch(e.name.lower(), pattern.lower()):
                continue
            if len(rows) >= max_entries:
                truncated = True
                break
            rel = os.path.relpath(e.path, p).replace("\\", "/")
            try:
                st = e.stat(follow_symlinks=False)
                meta = f"{st.st_size} bytes  " if not is_dir else ""
                meta += datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                meta = "?"
            rows.append(f"{'[dir]' if is_dir else '[file]'} {rel}{'/' if is_dir else ''}  {meta}")
    head = f"{len(rows)} entries shown under {p}" + (f" (max_entries={max_entries} reached, results truncated)" if truncated else "")
    if recursive:
        head += f", recursion depth limit {MAX_LIST_DEPTH}"
    return head + ("\n" + "\n".join(rows) if rows else " (empty)")


@_oserr
def write_text(p: Path, content: str, mode: str, backup_dir: Path, create_dirs: bool = False) -> str:
    if mode not in ("create", "overwrite", "append"):
        raise FileError("mode must be create / overwrite / append")
    if len(content) > MAX_WRITE_CHARS:
        raise FileError(f"content is too long (limit {MAX_WRITE_CHARS} characters); write it in several parts (create first, then append).")
    if p.is_dir():
        raise FileError(f"{p} is a directory")
    exists = p.exists()
    if mode == "create" and exists:
        raise FileError("The file already exists. Use mode='overwrite' to replace it or mode='append' to append; use edit_file to change part of it.")
    if not p.parent.is_dir():
        if not create_dirs:
            raise FileError(f"Directory does not exist: {p.parent} (set create_dirs=true if needed)")
        p.parent.mkdir(parents=True)

    enc, eol, old = "utf-8", "LF", ""
    if exists:
        old, enc, eol = _load(p)
    body = content.replace("\r\n", "\n")
    new_text = old + body if mode == "append" else body
    if exists and eol == "CRLF":
        new_text = new_text.replace("\n", "\r\n")
    elif not exists:
        new_text = content  # new file: write the given content as-is
    note = ""
    if exists:
        note = f", the original file was backed up to {_backup(p, backup_dir)}"
    _atomic_write(p, new_text.encode(enc))
    verb = {"create": "Created", "overwrite": "Overwrote" if exists else "Created", "append": "Appended to"}[mode]
    return f"{verb} {p} ({len(content)} characters written, encoding {enc}{', newlines CRLF' if eol == 'CRLF' and exists else ''}){note}."


@_oserr
def edit_text(p: Path, old: str, new: str, replace_all: bool, backup_dir: Path) -> str:
    _need_file(p)
    text, enc, eol = _load(p)
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    if not old:
        raise FileError("old_string must not be empty (use write_file to create a file or write it whole).")
    if old == new:
        raise FileError("old_string and new_string are identical; there is nothing to change.")
    count = text.count(old)
    if count == 0:
        raise FileError("old_string was not found. It must match verbatim (spaces, indentation, newlines); read_file first to confirm the current content.")
    if count > 1 and not replace_all:
        raise FileError(f"old_string occurs {count} times, so it is not unique. Include more context to make it unique, or set replace_all=true.")
    first = text.index(old)
    result = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    if len(result) > MAX_FILE_BYTES:
        raise FileError("The file would be too large after the edit.")
    backup = _backup(p, backup_dir)
    _atomic_write(p, (result.replace("\n", "\r\n") if eol == "CRLF" else result).encode(enc))

    line_no = text.count("\n", 0, first) + 1
    lines = result.split("\n")
    lo, hi = max(0, line_no - 1 - 2), min(len(lines), line_no - 1 + new.count("\n") + 1 + 2)
    snippet = "\n".join(f"{i + 1:>5}| {lines[i]}" for i in range(lo, min(hi, lo + 20)))
    return (f"Edited {p}: replaced {count if replace_all else 1} occurrence(s) (starting at line {line_no}), encoding {enc}, newlines {eol}. "
            f"The original file was backed up to {backup}.\nContext after the edit:\n{snippet[:2000]}")


# --------------------------------------------------------------------------- #
# Binary file transfer (chunked)
# --------------------------------------------------------------------------- #
def part_path(p: Path) -> Path:
    """Temp file used during an upload (in the same directory as the target; atomically replaces the target when finished)."""
    return p.with_name(f".{p.name}.winpc-part")


def _check_not_secret_bytes(p: Path) -> None:
    """Binary reads must also block private keys by content (otherwise get_file would bypass the read_file check)."""
    with p.open("rb") as f:
        head = f.read(16384)
    if _SECRET_CONTENT.search(head.decode("latin-1")):
        raise FileError("This file looks like a private key (BEGIN ... PRIVATE KEY); reading it is forbidden. Do not work around this; tell the user.")


def _sha256(p: Path, limit: int) -> str:
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            n += len(chunk)
            if n > limit:
                raise FileError(f"The file exceeds the transfer limit of {limit // 1024 // 1024} MB.")
            h.update(chunk)
    return h.hexdigest()


def _need_regular_file(p: Path) -> None:
    if not p.exists():
        raise FileError(f"File does not exist: {p}")
    if p.is_dir():
        raise FileError(f"{p} is a directory; use list_files.")


@_oserr
def stat_file(p: Path, want_sha256: bool, max_bytes: int) -> dict:
    if not p.exists() and part_path(p).exists():  # an interrupted new upload: the target does not exist yet, but it can be resumed
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
        info["partial_upload_size"] = part.stat().st_size  # unfinished upload; can be resumed from this offset
    return info


@_oserr
def read_chunk(p: Path, offset: int, length: int, max_bytes: int) -> tuple[dict, bytes]:
    _need_regular_file(p)
    if offset < 0:
        raise FileError("offset must not be negative")
    if not 1 <= length <= MAX_CHUNK_BYTES:
        raise FileError(f"length must be in 1..{MAX_CHUNK_BYTES}")
    _check_not_secret_bytes(p)
    size = p.stat().st_size
    if size > max_bytes:
        raise FileError(f"The file is {size} bytes, over the transfer limit of {max_bytes // 1024 // 1024} MB (transfer_max_bytes in config.json).")
    if offset > size:
        raise FileError(f"offset={offset} is beyond the file size {size}")
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    return {"path": str(p), "size": size, "offset": offset, "length": len(data),
            "eof": offset + len(data) >= size}, data


@_oserr
def write_chunk(p: Path, data: bytes, offset: int, final: bool, overwrite: bool, create_dirs: bool,
                expected_size: int | None, expected_sha256: str | None, backup_dir: Path,
                max_bytes: int) -> dict:
    """Chunked upload: write .<name>.winpc-part in the target directory first; on final, verify and atomically replace the target.

    offset=0 starts (or restarts) an upload; after that each chunk's offset must equal the current size of the temp file (query it with stat_file to resume).
    """
    if offset < 0:
        raise FileError("offset must not be negative")
    if len(data) > MAX_CHUNK_BYTES:
        raise FileError(f"A single chunk must not exceed {MAX_CHUNK_BYTES} bytes; split it into chunks.")
    if offset + len(data) > max_bytes:
        raise FileError(f"Over the transfer limit of {max_bytes // 1024 // 1024} MB (transfer_max_bytes in config.json).")
    if p.is_dir():
        raise FileError(f"{p} is a directory. When uploading, path must be the full path of the target file.")
    if p.exists() and not overwrite:
        raise FileError("The target file already exists. Set overwrite=true to replace it (the original is backed up first).")
    if not p.parent.is_dir():
        if not create_dirs:
            raise FileError(f"Directory does not exist: {p.parent} (set create_dirs=true if needed)")
        p.parent.mkdir(parents=True)
    part = part_path(p)
    if part.is_symlink():
        raise FileError("The temp file is a symlink; refused.")
    if offset == 0:
        mode = "wb"
    else:
        have = part.stat().st_size if part.exists() else None
        if have != offset:
            raise FileError(f"offset={offset} does not match the received size {have if have is not None else '(no temp file)'}; "
                            f"continue from offset={have or 0}, or restart with offset=0.")
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
        discard(f"Size mismatch: received {received} bytes, expected {expected_size}. The temp file was discarded.")
    digest = _sha256(part, max_bytes)
    if expected_sha256 and digest != expected_sha256.lower():
        discard(f"sha256 mismatch: received {digest}, expected {expected_sha256.lower()}. The temp file was discarded.")
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
