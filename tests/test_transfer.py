"""二进制文件传输：分块读写原语、授权与拦截、审计脱敏，以及经真实 HTTP 的 winpc_cp.py 端到端。无需 Windows。"""
import base64
import hashlib
import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

import fileops
from backends import FakeBackend
from fileops import FileError
from winpc_agent import Config, WinPCAgent, create_app

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("winpc_cp", ROOT / "claude-plugin/winpc/scripts/winpc_cp.py")
winpc_cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(winpc_cp)

TOKEN = "transfer-test-token-" + "x" * 24
B64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731


def make(tmp_path, **cfg):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    cfg.setdefault("file_roots", [str(tmp_path / "docs")])
    fb = FakeBackend()
    agent = WinPCAgent(Config(log_dir=str(tmp_path / "logs"), settle_ms=0, **cfg), fb)
    return agent, fb, tmp_path / "docs"


def js(r):
    assert not r.is_error, r.content[0].text
    return json.loads(r.content[0].text)


def err(r):
    assert r.is_error
    return r.content[0].text


BLOB = bytes(range(256)) * 40 + b"\x00\x00\xff\xfe" + os.urandom(3000)   # 含 NUL、非 UTF-8 的二进制


# ----------------------------- 原语：分块写 ----------------------------- #
async def test_put_multi_chunk_roundtrip_with_hash(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    dst = str(docs / "bin.dat")
    parts = [BLOB[i:i + 4000] for i in range(0, len(BLOB), 4000)]
    off = 0
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        r = js(await agent.put_file(dst, B64(part), offset=off, final=last,
                                    expected_size=len(BLOB) if last else None,
                                    expected_sha256=sha(BLOB) if last else None))
        off += len(part)
        assert r["done"] is last
    assert (docs / "bin.dat").read_bytes() == BLOB
    assert r["sha256"] == sha(BLOB) and r["size"] == len(BLOB)
    assert not list(docs.glob(".*winpc-part")), "临时文件必须被原子替换掉"


async def test_put_empty_file(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    r = js(await agent.put_file(str(docs / "empty.bin"), ""))
    assert r["size"] == 0 and (docs / "empty.bin").read_bytes() == b""


async def test_put_requires_matching_offset_and_supports_resume(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    dst = str(docs / "r.bin")
    js(await agent.put_file(dst, B64(BLOB[:1000]), offset=0, final=False))
    assert "offset=1000" in err(await agent.put_file(dst, B64(BLOB[2000:]), offset=2000))      # 跳块被拒
    assert "不一致" in err(await agent.put_file(dst, B64(BLOB[500:]), offset=500))             # 回退也被拒
    st = js(await agent.stat_file(dst))                                                         # 目标尚不存在，但能查到续传位置
    assert st["exists"] is False and st["partial_upload_size"] == 1000
    js(await agent.put_file(dst, B64(BLOB[1000:]), offset=1000, final=True, expected_sha256=sha(BLOB)))
    assert (docs / "r.bin").read_bytes() == BLOB


async def test_offset_zero_restarts_upload(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    dst = str(docs / "s.bin")
    js(await agent.put_file(dst, B64(b"OLD-GARBAGE"), offset=0, final=False))
    js(await agent.put_file(dst, B64(b"new"), offset=0, final=True))
    assert (docs / "s.bin").read_bytes() == b"new"


@pytest.mark.parametrize("kw,expect", [({"expected_sha256": "0" * 64}, "sha256 不一致"), ({"expected_size": 1}, "大小不一致")])
async def test_integrity_mismatch_discards_and_never_creates_target(tmp_path, kw, expect):
    agent, _, docs = make(tmp_path, approval_mode="all")
    assert expect in err(await agent.put_file(str(docs / "x.bin"), B64(b"hello"), **kw))
    assert not (docs / "x.bin").exists() and not list(docs.glob(".*winpc-part"))


async def test_overwrite_protection_backup_and_original_kept_on_failure(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "keep.bin"
    f.write_bytes(b"ORIGINAL")
    assert "已存在" in err(await agent.put_file(str(f), B64(b"new")))
    assert f.read_bytes() == b"ORIGINAL"
    err(await agent.put_file(str(f), B64(b"new"), overwrite=True, expected_sha256="0" * 64))   # 校验失败不能破坏原文件
    assert f.read_bytes() == b"ORIGINAL"
    r = js(await agent.put_file(str(f), B64(b"new"), overwrite=True))
    assert f.read_bytes() == b"new" and Path(r["backup"]).read_bytes() == b"ORIGINAL"


async def test_put_input_validation(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all", transfer_max_bytes=10)
    assert "base64" in err(await agent.put_file(str(docs / "a"), "###not-b64###"))
    assert "上限" in err(await agent.put_file(str(docs / "a"), B64(b"x" * 11)))
    assert "是目录" in err(await agent.put_file(str(docs), B64(b"x")))
    assert "目录不存在" in err(await agent.put_file(str(docs / "no" / "a"), B64(b"x")))
    js(await agent.put_file(str(docs / "no" / "a"), B64(b"x"), create_dirs=True))
    assert "不能超过" in err(await agent.put_file(str(docs / "b"), B64(b"x" * (fileops.MAX_CHUNK_BYTES + 1))))
    assert "不能为负" in err(await agent.put_file(str(docs / "b"), B64(b"x"), offset=-1))


# ----------------------------- 原语：分块读 / stat ----------------------------- #
async def test_get_chunks_reassemble_binary_and_stat_hash(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "b.bin").write_bytes(BLOB)
    st = js(await agent.stat_file(str(docs / "b.bin"), sha256=True))
    assert st["size"] == len(BLOB) and st["sha256"] == sha(BLOB)
    got, off = b"", 0
    while True:
        r = js(await agent.get_file(str(docs / "b.bin"), offset=off, length=5000))
        got += base64.b64decode(r["data_b64"])
        off += r["length"]
        if r["eof"]:
            break
    assert got == BLOB


async def test_get_errors(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "a.bin").write_bytes(b"abc")
    assert "不存在" in err(await agent.get_file(str(docs / "none.bin")))
    assert "是目录" in err(await agent.get_file(str(docs)))
    assert "超出" in err(await agent.get_file(str(docs / "a.bin"), offset=99))
    assert "length" in err(await agent.get_file(str(docs / "a.bin"), length=0))
    agent2, _, docs2 = make(tmp_path / "t2", transfer_max_bytes=2)
    (docs2 / "big.bin").write_bytes(b"abc")
    assert "传输上限" in err(await agent2.get_file(str(docs2 / "big.bin")))


# ----------------------------- 授权与拦截（与文件工具同一套闸门）----------------------------- #
async def test_put_prompts_once_per_dir_and_denial_changes_nothing(tmp_path):
    agent, fb, docs = make(tmp_path)
    fb.confirm_result = False
    assert "拒绝" in err(await agent.put_file(str(docs / "a.bin"), B64(b"x")))
    assert not (docs / "a.bin").exists() and not list(docs.glob(".*part"))
    fb.confirm_result = True
    js(await agent.put_file(str(docs / "a.bin"), B64(b"x")))
    js(await agent.put_file(str(docs / "b.bin"), B64(b"y")))
    assert len([c for c in fb.confirm_calls if "文件授权" in c[0]]) == 2   # 拒绝一次 + 同意一次；同意后同目录不再问


async def test_allowlist_rejects_put_but_reads_in_roots_work(tmp_path):
    agent, fb, docs = make(tmp_path, approval_mode="allowlist")
    (docs / "r.bin").write_bytes(b"data")
    assert "allowlist" in err(await agent.put_file(str(docs / "w.bin"), B64(b"x")))
    js(await agent.get_file(str(docs / "r.bin")))
    assert not fb.confirm_calls


async def test_pause_blocks_put_but_not_get(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    (docs / "r.bin").write_bytes(b"data")
    agent.paused = True
    assert "暂停" in err(await agent.put_file(str(docs / "w.bin"), B64(b"x")))
    js(await agent.get_file(str(docs / "r.bin")))


@pytest.mark.parametrize("mode", ["all", "prompt"])
async def test_get_cannot_exfiltrate_private_keys_or_credentials(tmp_path, mode):
    """下载工具不能成为读取私钥/凭据的旁路，即使 approval_mode=all 或用户点了“是”。"""
    agent, fb, docs = make(tmp_path, approval_mode=mode)
    fb.confirm_result = True
    key = tmp_path / "ssh" / "myserver"
    key.parent.mkdir()
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETBYTES\n-----END OPENSSH PRIVATE KEY-----\n")
    (docs / ".env").write_text("TOKEN=abc")
    for p in (key, docs / ".env"):
        r = await agent.get_file(str(p))
        assert r.is_error and "SECRETBYTES" not in r.content[0].text and "abc" not in r.content[0].text
        assert (await agent.stat_file(str(p))).is_error
    # 二进制文件头部含私钥也拦
    blob = tmp_path / "docs" / "x.bin"
    blob.write_bytes(b"\x00\x01" + key.read_bytes())
    assert (await agent.get_file(str(blob))).is_error


async def test_put_blocked_paths_and_executables_even_in_all_mode(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    for name in ("setup.exe", "run.bat", "x.ps1", "a.lnk"):
        assert "不允许写入" in err(await agent.put_file(str(docs / name), B64(b"MZ")))
        assert not (docs / name).exists()
    assert err(await agent.put_file(str(tmp_path / ".ssh" / "authorized_keys"), B64(b"x"), create_dirs=True))
    here = os.path.dirname(os.path.abspath(__import__("winpc_agent").__file__))
    assert "Agent 自身目录" in err(await agent.put_file(os.path.join(here, "evil.py"), B64(b"x")))
    assert not os.path.exists(os.path.join(here, "evil.py"))


async def test_symlink_part_and_symlink_target_are_not_followed_out(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="prompt")
    agent  # noqa: B018
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("keep")
    os.symlink(victim, docs / "link.bin")           # 目标是指向 roots 之外的符号链接
    fb = agent.backend
    fb.confirm_result = False
    r = await agent.put_file(str(docs / "link.bin"), B64(b"PWN"), overwrite=True)
    assert victim.read_text() == "keep"             # 解析为真实路径后落在别的目录，需要用户授权；被拒
    assert r.is_error


async def test_audit_never_logs_binary_payload(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    payload = B64(b"TOP-SECRET-BYTES" * 50)
    js(await agent.put_file(str(docs / "a.bin"), payload))
    raw = (tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8")
    assert payload not in raw and "TOP-SECRET" not in raw
    entry = json.loads(raw.splitlines()[-1])
    assert entry["tool"] == "put_file" and entry["params"]["data_b64_len"] == len(payload)
    assert entry["process"].startswith("file:")


async def test_file_tools_disabled_blocks_transfer(tmp_path):
    agent, _, docs = make(tmp_path, file_tools_enabled=False)
    assert "关闭" in err(await agent.put_file(str(docs / "a"), B64(b"x")))
    assert "关闭" in err(await agent.get_file(str(docs / "a")))


# ----------------------------- 端到端：winpc_cp.py 经真实 HTTP ----------------------------- #
@pytest.fixture
def live(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "docs").mkdir()
    fb = FakeBackend()
    cfg = Config(port=port, token=TOKEN, log_dir=str(tmp_path / "logs"), settle_ms=0,
                 approval_mode="all", file_roots=[str(tmp_path / "docs")])
    app, agent = create_app(cfg, fb)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", tmp_path
    srv.should_exit = True
    t.join(timeout=10)


def test_cli_upload_download_roundtrip_multi_chunk(live):
    url, tmp = live
    big = os.urandom(3 * 1024 * 1024 + 123)          # 3 块 + 零头，且含大量非 UTF-8 字节
    src = tmp / "src.bin"
    src.write_bytes(big)
    c = winpc_cp.McpClient(url, TOKEN)
    r = winpc_cp.put(c, str(src), str(tmp / "docs") + os.sep, quiet=True)          # 目录写法：沿用文件名
    assert r["size"] == len(big) and r["sha256"] == sha(big)
    assert (tmp / "docs" / "src.bin").read_bytes() == big
    out = winpc_cp.get(c, str(tmp / "docs" / "src.bin"), str(tmp / "dl") + os.sep, quiet=True, chunk=700_000)
    assert Path(out["path"]).read_bytes() == big and out["sha256"] == sha(big)
    assert not list((tmp / "dl").glob("*.winpc-part"))


def test_cli_main_exit_codes_and_no_token_in_args(live, monkeypatch, capsys):
    url, tmp = live
    f = tmp / "a.txt"
    f.write_text("hi")
    monkeypatch.setenv("WINPC_TOKEN", TOKEN)
    assert winpc_cp.main(["--url", url, "-q", "put", str(f), str(tmp / "docs" / "a.txt")]) == 0
    assert json.loads(capsys.readouterr().out)["size"] == 2
    assert winpc_cp.main(["--url", url, "-q", "put", str(f), str(tmp / "docs" / "a.txt")]) == 1     # 已存在
    assert "已存在" in capsys.readouterr().err
    assert winpc_cp.main(["--url", url, "-q", "put", "--overwrite", str(f), str(tmp / "docs" / "a.txt")]) == 0
    assert winpc_cp.main(["--url", url, "-q", "stat", "--sha256", str(tmp / "docs" / "a.txt")]) == 0
    monkeypatch.setenv("WINPC_TOKEN", "wrong")
    assert winpc_cp.main(["--url", url, "-q", "stat", str(tmp / "docs" / "a.txt")]) == 1
    assert "401" in capsys.readouterr().err
    monkeypatch.delenv("WINPC_TOKEN")
    assert winpc_cp.main(["--url", url, "stat", "x"]) == 2


def test_cli_resume_after_interrupted_upload(live):
    url, tmp = live
    data = os.urandom(2_500_000)
    src = tmp / "big.bin"
    src.write_bytes(data)
    c = winpc_cp.McpClient(url, TOKEN)
    dst = str(tmp / "docs" / "big.bin")
    first = 1_000_000
    c.call("put_file", path=dst, data_b64=B64(data[:first]), offset=0, final=False)     # 模拟只传了一半就断了
    r = winpc_cp.put(c, str(src), dst, resume=True, quiet=True)
    assert r["sha256"] == sha(data) and (tmp / "docs" / "big.bin").read_bytes() == data


def test_cli_refuses_to_download_private_key(live):
    url, tmp = live
    key = tmp / "docs" / "id_whatever"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n")
    c = winpc_cp.McpClient(url, TOKEN)
    with pytest.raises(winpc_cp.TransferError, match="私钥|敏感"):
        winpc_cp.get(c, str(key), str(tmp / "out.key"), quiet=True)
    assert not (tmp / "out.key").exists()


def test_cli_closes_its_mcp_session_so_the_agent_does_not_accumulate_them(live):
    """实机发现：每次运行都留下一个未关闭的会话，Agent 控制台刷屏。现在退出时必须 DELETE 会话。"""
    import urllib.request
    url, tmp = live
    c = winpc_cp.McpClient(url, TOKEN)
    sid = c.headers["Mcp-Session-Id"]
    c.close()
    assert "Mcp-Session-Id" not in c.headers

    def post_with(session):
        req = urllib.request.Request(url, method="POST", data=json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", "Mcp-Session-Id": session,
                     "MCP-Protocol-Version": "2025-06-18"})
        try:
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post_with(sid) == 404, "会话关闭后，旧会话 ID 不应再可用"


def test_cli_main_uses_a_closing_session(live, monkeypatch, capsys):
    url, tmp = live
    closed = []
    real_close = winpc_cp.McpClient.close
    monkeypatch.setattr(winpc_cp.McpClient, "close", lambda self: (closed.append(1), real_close(self))[1])
    monkeypatch.setenv("WINPC_TOKEN", TOKEN)
    f = tmp / "a.txt"
    f.write_text("hi")
    assert winpc_cp.main(["--url", url, "-q", "put", str(f), str(tmp / "docs" / "a.txt")]) == 0
    assert winpc_cp.main(["--url", url, "-q", "stat", str(tmp / "docs" / "nope")]) == 1     # 出错路径也要关闭
    assert len(closed) == 2


def _serve_once(handler):
    """起一个只回应一次的假服务器，用来制造各种网络故障。"""
    import socketserver
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(65536)
            handler(self.request)
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


@pytest.mark.parametrize("fault", ["close", "truncated_chunked", "refused"])
def test_cli_network_faults_become_clean_errors_not_tracebacks(fault, monkeypatch, capsys):
    """实机场景：隧道端口在监听，但 Windows 上的 Agent 没了 → 连接被立刻关闭 / 响应被截断。"""
    monkeypatch.setenv("WINPC_TOKEN", TOKEN)
    if fault == "close":
        srv, url = _serve_once(lambda sock: None)                                   # 收到请求就关
    elif fault == "truncated_chunked":
        srv, url = _serve_once(lambda sock: sock.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\n\r\n"))
    else:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{s.getsockname()[1]}/mcp"                      # 没人监听
        srv = None
    rc = winpc_cp.main(["--url", url, "-q", "stat", "x"])
    err = capsys.readouterr().err
    assert rc == 1 and "错误：" in err and "Traceback" not in err
    assert ("Agent" in err) or ("连不上" in err)
    if srv:
        srv.server_close()
