#!/usr/bin/env python3
"""winpc_cp: transfer files between Linux and the remote Windows PC (through the winpc-agent MCP interface; standard library only).

File contents travel straight through the tunnel and never pass through the model context, so large and binary files are fine. sha256 is verified end to end.

    winpc_cp.py put  LOCAL_FILE  C:\\Users\\me\\Desktop\\          # a trailing \\ or / means a directory; the original file name is kept
    winpc_cp.py get  C:\\Users\\me\\Desktop\\a.zip  ./downloads/
    winpc_cp.py stat C:\\Users\\me\\Desktop\\a.zip --sha256

Environment variables: WINPC_TOKEN (required), WINPC_URL (default http://127.0.0.1:8765/mcp).
The token is not accepted as a command-line argument, so that it does not show up in the process list.

The Windows side also shows confirmation dialogs / blocks sensitive paths: writes need the user's approval (or approval_mode=all), private-key/credential files cannot be downloaded,
and executable extensions such as .exe/.bat/.ps1 cannot be uploaded by default (file_blocked_write_ext in config.json).
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
    """A minimal Streamable HTTP MCP client: initialize -> notifications/initialized -> tools/call."""

    def __init__(self, url: str, token: str, timeout: float = 180) -> None:
        self.url, self.timeout = url, timeout
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        # do not use the system proxy: the target is the local loopback (the tunnel exit)
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
                raise TransferError("HTTP 401: wrong token (check that WINPC_TOKEN matches the one in the Windows config.json).") from None
            raise TransferError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise TransferError(f"Cannot reach {self.url}: {e.reason}. Are the tunnel and the Agent both running?") from None
        except (http.client.HTTPException, ConnectionError, socket.timeout, OSError) as e:
            # e.g. RemoteDisconnected / IncompleteRead: the tunnel is still up (the port is listening), but the Agent on Windows is gone or restarting
            raise TransferError(f"The connection to the Agent was interrupted ({type(e).__name__}). The tunnel may still be up, but the Agent on Windows "
                                "is not running or is restarting; check and retry (--resume continues an upload).") from None
        if want_id is None or not raw.strip():
            return None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == want_id:
                        return msg
            raise TransferError("The server response did not contain the matching result.")
        return json.loads(raw)

    def _initialize(self) -> None:
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "winpc_cp", "version": "1"}}}, self._id)
        if r is None or "error" in r:
            raise TransferError(f"initialize failed: {r}")
        self.headers["MCP-Protocol-Version"] = r["result"].get("protocolVersion", PROTOCOL)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)

    def close(self) -> None:
        """End the MCP session (HTTP DELETE). If it is not closed, the Agent keeps the session left behind by every run."""
        sid = self.headers.get("Mcp-Session-Id")
        if not sid:
            return
        req = urllib.request.Request(self.url, method="DELETE", headers={
            "Authorization": self.headers["Authorization"], "Mcp-Session-Id": sid,
            "MCP-Protocol-Version": self.headers.get("MCP-Protocol-Version", PROTOCOL)})
        try:
            self._opener.open(req, timeout=10).read()
        except Exception:      # noqa: BLE001  a failure to close does not affect the transfer result
            pass
        self.headers.pop("Mcp-Session-Id", None)

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, tool: str, **arguments) -> dict:
        """Call a tool and parse the JSON text it returns; when the tool reports an error, raise TransferError (with the reason given by the Agent)."""
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}}, self._id)
        if "error" in r:
            raise TransferError(f"{tool}: {r['error'].get('message', r['error'])}")
        res = r["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise TransferError(text or f"{tool} failed")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise TransferError(f"{tool} returned content that cannot be parsed: {text[:200]}") from None


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
        raise TransferError(f"Local file does not exist: {local}")
    remote = _join_remote(remote, local)
    size, digest = os.path.getsize(local), _sha256_file(local)
    offset = 0
    if resume:  # continue from the unfinished temp file on the Agent
        try:
            part = c.call("stat_file", path=remote).get("partial_upload_size")
        except TransferError:
            part = None  # when the target does not exist yet, stat_file reports an error, which is equivalent to nothing to resume
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
            _progress(f"Uploading {_human(offset)} / {_human(size)}", quiet)
            if final:
                break
    if not quiet:
        sys.stderr.write("\n")
    if r.get("sha256") != digest or r.get("size") != size:
        raise TransferError(f"Verification failed: local {digest}/{size}, remote {r.get('sha256')}/{r.get('size')}")
    return r


def get(c: McpClient, remote: str, local: str, *, overwrite=False, chunk=1024 * 1024, quiet=False) -> dict:
    info = c.call("stat_file", path=remote, sha256=True)
    if local.endswith(("/", os.sep)) or os.path.isdir(local):
        local = os.path.join(local, os.path.basename(remote.replace("\\", "/")))
    if os.path.exists(local) and not overwrite:
        raise TransferError(f"Local file already exists: {local} (add --overwrite to replace it)")
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
                _progress(f"Downloading {_human(offset)} / {_human(size)}", quiet)
                if r["eof"]:
                    break
                if not data:
                    raise TransferError("The remote returned an empty chunk but the file has not been fully read.")
        if not quiet:
            sys.stderr.write("\n")
        if offset != size or h.hexdigest() != info["sha256"]:
            raise TransferError(f"Verification failed: received {offset} bytes sha256={h.hexdigest()}, remote {size} bytes sha256={info['sha256']}")
        os.replace(tmp, local)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"path": local, "size": size, "sha256": info["sha256"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Transfer files between Linux and the remote Windows PC (through winpc-agent)")
    ap.add_argument("--url", default=os.environ.get("WINPC_URL", DEFAULT_URL))
    ap.add_argument("-q", "--quiet", action="store_true", help="do not show progress")
    ap.add_argument("--chunk-kib", type=int, default=1024, help="KiB per chunk (default 1024, maximum 2048)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put", help="upload: local -> Windows")
    p.add_argument("local"); p.add_argument("remote")
    p.add_argument("--overwrite", action="store_true", help="overwrite if the target exists (the original is backed up on Windows first)")
    p.add_argument("--create-dirs", action="store_true")
    p.add_argument("--resume", action="store_true", help="resume from the unfinished temp file on Windows")
    g = sub.add_parser("get", help="download: Windows -> local")
    g.add_argument("remote"); g.add_argument("local")
    g.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("stat", help="show information about a remote file")
    s.add_argument("remote"); s.add_argument("--sha256", action="store_true")
    a = ap.parse_args(argv)

    token = os.environ.get("WINPC_TOKEN", "").strip()
    if not token:
        print("Error: the WINPC_TOKEN environment variable is not set.", file=sys.stderr)
        return 2
    if not 1 <= a.chunk_kib * 1024 <= MAX_CHUNK:
        print(f"Error: --chunk-kib must be in 1..{MAX_CHUNK // 1024}", file=sys.stderr)
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
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
