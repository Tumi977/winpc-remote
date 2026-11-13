#!/usr/bin/env python3
"""winpc_cp：在 Linux 与远端 Windows 之间传输文件（经 winpc-agent 的 MCP 接口，只用标准库）。

文件内容直接走隧道，不经过模型上下文，所以大文件、二进制文件都可以。全程 sha256 校验。

    winpc_cp.py put  本地文件  C:\\Users\\me\\Desktop\\          # 末尾是 \\ 或 / 表示目录，沿用原文件名
    winpc_cp.py get  C:\\Users\\me\\Desktop\\a.zip  ./downloads/
    winpc_cp.py stat C:\\Users\\me\\Desktop\\a.zip --sha256

环境变量：WINPC_TOKEN（必需）、WINPC_URL（默认 http://127.0.0.1:8765/mcp）。
令牌不接受命令行参数，以免出现在进程列表里。

Windows 端同样会弹确认框/拦截敏感路径：写入需要用户授权（或 approval_mode=all），私钥/凭据文件无法下载，
.exe/.bat/.ps1 等可执行扩展名默认无法上传（config.json 的 file_blocked_write_ext）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
MAX_CHUNK = 2 * 1024 * 1024


class TransferError(Exception):
    pass


class McpClient:
    """最小的 Streamable HTTP MCP 客户端：initialize → notifications/initialized → tools/call。"""

    def __init__(self, url: str, token: str, timeout: float = 180) -> None:
        self.url, self.timeout = url, timeout
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        # 不走系统代理：目标是本机回环（隧道出口）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._id = 0
        self._initialize()

    def _post(self, payload: dict, want_id: int | None):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self.headers,
                                     method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.headers["Mcp-Session-Id"] = sid
                raw = resp.read().decode("utf-8")
                ctype = resp.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise TransferError("HTTP 401：令牌不正确（检查 WINPC_TOKEN 是否与 Windows config.json 里的一致）。") from None
            raise TransferError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise TransferError(f"连不上 {self.url}：{e.reason}。隧道和 Agent 都在运行吗？") from None
        except (http.client.HTTPException, ConnectionError, socket.timeout, OSError) as e:
            # 例如 RemoteDisconnected / IncompleteRead：隧道还在（端口在监听），但 Windows 上的 Agent 已经没了或正在重启
            raise TransferError(f"与 Agent 的连接被中断（{type(e).__name__}）。隧道可能还在，但 Windows 上的 "
                                "Agent 没有运行或正在重启；请检查后重试（支持 --resume 续传）。") from None
        if want_id is None or not raw.strip():
            return None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == want_id:
                        return msg
            raise TransferError("服务器响应里没有找到对应的结果。")
        return json.loads(raw)

    def _initialize(self) -> None:
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "winpc_cp", "version": "1"}}}, self._id)
        if r is None or "error" in r:
            raise TransferError(f"initialize 失败: {r}")
        self.headers["MCP-Protocol-Version"] = r["result"].get("protocolVersion", PROTOCOL)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)

    def close(self) -> None:
        """结束 MCP 会话（HTTP DELETE）。不关的话，Agent 端会一直保留每次运行留下的会话。"""
        sid = self.headers.get("Mcp-Session-Id")
        if not sid:
            return
        req = urllib.request.Request(self.url, method="DELETE", headers={
            "Authorization": self.headers["Authorization"], "Mcp-Session-Id": sid,
            "MCP-Protocol-Version": self.headers.get("MCP-Protocol-Version", PROTOCOL)})
        try:
            self._opener.open(req, timeout=10).read()
        except Exception:      # noqa: BLE001  关闭失败不影响传输结果
            pass
        self.headers.pop("Mcp-Session-Id", None)

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, tool: str, **arguments) -> dict:
        """调用工具并解析其返回的 JSON 文本；工具报错时抛 TransferError（带 Agent 给出的原因）。"""
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}}, self._id)
        if "error" in r:
            raise TransferError(f"{tool}: {r['error'].get('message', r['error'])}")
        res = r["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise TransferError(text or f"{tool} 失败")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise TransferError(f"{tool} 返回了无法解析的内容: {text[:200]}") from None


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _progress(msg: str, quiet: bool) -> None:
    if not quiet:
        sys.stderr.write("\r" + msg.ljust(60))
        sys.stderr.flush()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _join_remote(remote: str, local_name: str) -> str:
    return remote + os.path.basename(local_name) if remote.endswith(("\\", "/")) else remote


def put(c: McpClient, local: str, remote: str, *, overwrite=False, create_dirs=False,
        chunk=1024 * 1024, resume=False, quiet=False) -> dict:
    if not os.path.isfile(local):
        raise TransferError(f"本地文件不存在: {local}")
    remote = _join_remote(remote, local)
    size, digest = os.path.getsize(local), _sha256_file(local)
    offset = 0
    if resume:  # 从 Agent 上未完成的临时文件处继续
        try:
            part = c.call("stat_file", path=remote).get("partial_upload_size")
        except TransferError:
            part = None  # 目标文件还不存在时 stat_file 会报错，等价于没有可续传的部分
        if part and part <= size:
            offset = part
    with open(local, "rb") as f:
        f.seek(offset)
        while True:
            data = f.read(chunk)
            final = f.tell() >= size
            r = c.call("put_file", path=remote, data_b64=base64.b64encode(data).decode("ascii"),
                       offset=offset, final=final, overwrite=overwrite, create_dirs=create_dirs,
                       expected_size=size if final else None, expected_sha256=digest if final else None)
            offset += len(data)
            _progress(f"上传 {_human(offset)} / {_human(size)}", quiet)
            if final:
                break
    if not quiet:
        sys.stderr.write("\n")
    if r.get("sha256") != digest or r.get("size") != size:
        raise TransferError(f"校验失败：本地 {digest}/{size}，远端 {r.get('sha256')}/{r.get('size')}")
    return r


def get(c: McpClient, remote: str, local: str, *, overwrite=False, chunk=1024 * 1024, quiet=False) -> dict:
    info = c.call("stat_file", path=remote, sha256=True)
    if local.endswith(("/", os.sep)) or os.path.isdir(local):
        local = os.path.join(local, os.path.basename(remote.replace("\\", "/")))
    if os.path.exists(local) and not overwrite:
        raise TransferError(f"本地文件已存在: {local}（覆盖请加 --overwrite）")
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    tmp = local + ".winpc-part"
    size, h, offset = info["size"], hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as out:
            while True:
                r = c.call("get_file", path=remote, offset=offset, length=chunk)
                data = base64.b64decode(r["data_b64"])
                out.write(data)
                h.update(data)
                offset += len(data)
                _progress(f"下载 {_human(offset)} / {_human(size)}", quiet)
                if r["eof"]:
                    break
                if not data:
                    raise TransferError("远端返回了空数据块但文件尚未读完。")
        if not quiet:
            sys.stderr.write("\n")
        if offset != size or h.hexdigest() != info["sha256"]:
            raise TransferError(f"校验失败：收到 {offset} 字节 sha256={h.hexdigest()}，远端 {size} 字节 sha256={info['sha256']}")
        os.replace(tmp, local)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"path": local, "size": size, "sha256": info["sha256"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="在 Linux 与远端 Windows 之间传输文件（经 winpc-agent）")
    ap.add_argument("--url", default=os.environ.get("WINPC_URL", DEFAULT_URL))
    ap.add_argument("-q", "--quiet", action="store_true", help="不显示进度")
    ap.add_argument("--chunk-kib", type=int, default=1024, help="每块 KiB（默认 1024，最大 2048）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put", help="上传：本地 → Windows")
    p.add_argument("local"); p.add_argument("remote")
    p.add_argument("--overwrite", action="store_true", help="目标已存在时覆盖（原文件会先在 Windows 端备份）")
    p.add_argument("--create-dirs", action="store_true")
    p.add_argument("--resume", action="store_true", help="从 Windows 上未完成的临时文件处续传")
    g = sub.add_parser("get", help="下载：Windows → 本地")
    g.add_argument("remote"); g.add_argument("local")
    g.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("stat", help="查看远端文件信息")
    s.add_argument("remote"); s.add_argument("--sha256", action="store_true")
    a = ap.parse_args(argv)

    token = os.environ.get("WINPC_TOKEN", "").strip()
    if not token:
        print("错误：未设置 WINPC_TOKEN 环境变量。", file=sys.stderr)
        return 2
    if not 1 <= a.chunk_kib * 1024 <= MAX_CHUNK:
        print(f"错误：--chunk-kib 必须在 1..{MAX_CHUNK // 1024}", file=sys.stderr)
        return 2
    chunk = a.chunk_kib * 1024
    try:
        with McpClient(a.url, token) as c:
            if a.cmd == "put":
                r = put(c, a.local, a.remote, overwrite=a.overwrite, create_dirs=a.create_dirs, chunk=chunk,
                        resume=a.resume, quiet=a.quiet)
            elif a.cmd == "get":
                r = get(c, a.remote, a.local, overwrite=a.overwrite, chunk=chunk, quiet=a.quiet)
            else:
                r = c.call("stat_file", path=a.remote, sha256=a.sha256)
    except TransferError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
