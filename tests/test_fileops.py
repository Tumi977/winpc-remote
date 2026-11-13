"""文件工具测试：路径策略、编码/换行保持、备份、授权模式、暂停、审计脱敏。无需 Windows。"""
import json
import os

import pytest

from backends import FakeBackend
from fileops import FileError, PathPolicy
from winpc_agent import Config, WinPCAgent


def make(tmp_path, **cfg):
    roots = [str(tmp_path / "docs")]
    (tmp_path / "docs").mkdir(exist_ok=True)
    cfg.setdefault("file_roots", roots)
    backend = FakeBackend()
    agent = WinPCAgent(Config(log_dir=str(tmp_path / "logs"), settle_ms=0, **cfg), backend)
    return agent, backend, tmp_path / "docs"


def txt(r):
    return r.content[0].text


# ----------------------------- 路径策略 ----------------------------- #
@pytest.mark.parametrize("raw", ["", "   ", "relative/a.txt", r"\\server\share\a.txt", "//server/share/a.txt",
                                 "C:/x/a.txt:stream", "C:/x/a.txt.", "C:/x/a.txt "])
def test_resolve_rejects_bad_paths(raw):
    pol = PathPolicy.build([], [], [], [])
    with pytest.raises(FileError):
        pol.resolve(raw)


@pytest.mark.parametrize("sub", [".ssh/id_rsa", ".aws/credentials", "x/id_ed25519", "vault.kdbx",
                                 "AppData/Local/Google/Chrome/User Data/Default/Login Data",
                                 "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/a.txt"])
def test_sensitive_paths_blocked(tmp_path, sub):
    pol = PathPolicy.build([str(tmp_path)], [], [], [])
    with pytest.raises(FileError):
        pol.classify(pol.resolve(str(tmp_path / sub)), write=False)


def test_agent_dir_and_logs_blocked(tmp_path):
    agent, _, _ = make(tmp_path)
    here = os.path.dirname(os.path.abspath(__import__("winpc_agent").__file__))
    for p in (os.path.join(here, "config.json"), str(tmp_path / "logs" / "file_backups" / "x.txt")):
        with pytest.raises(FileError):
            agent.policy.classify(agent.policy.resolve(p), write=False)


def test_executable_extensions_blocked_for_write_only(tmp_path):
    pol = PathPolicy.build([str(tmp_path)], [], [".bat", "ps1"], [])
    p = pol.resolve(str(tmp_path / "run.bat"))
    pol.classify(p, write=False)
    with pytest.raises(FileError):
        pol.classify(p, write=True)
    with pytest.raises(FileError):
        pol.classify(pol.resolve(str(tmp_path / "x.PS1")), write=True)


@pytest.mark.skipif(os.name == "nt", reason="符号链接需要特权")
def test_symlink_escape_is_resolved(tmp_path):
    (tmp_path / "docs").mkdir()
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("x")
    os.symlink(secret, tmp_path / "docs" / "link.txt")
    pol = PathPolicy.build([str(tmp_path / "docs")], [], [], [])
    assert pol.classify(pol.resolve(str(tmp_path / "docs" / "link.txt")), write=False) is False  # 指向 roots 外


# ----------------------------- 读 ----------------------------- #
async def test_read_in_roots_needs_no_prompt(tmp_path):
    agent, fb, docs = make(tmp_path)
    f = docs / "a.txt"
    f.write_text("你好\nworld\n", encoding="utf-8")
    r = await agent.read_file(str(f))
    assert not r.is_error and "你好" in txt(r) and "utf-8" in txt(r) and "共 9 字符" in txt(r)
    assert fb.confirm_calls == []


async def test_read_outside_roots_prompts_once_per_dir(tmp_path):
    agent, fb, _ = make(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_text("a")
    (other / "b.txt").write_text("b")
    assert not (await agent.read_file(str(other / "a.txt"))).is_error
    assert not (await agent.read_file(str(other / "b.txt"))).is_error
    assert len(fb.confirm_calls) == 1


async def test_read_outside_roots_denied(tmp_path):
    agent, fb, _ = make(tmp_path)
    fb.confirm_result = False
    (tmp_path / "x.txt").write_text("secret")
    r = await agent.read_file(str(tmp_path / "x.txt"))
    assert r.is_error and "拒绝" in txt(r) and "secret" not in txt(r)


async def test_read_chunking_and_encodings(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "long.txt").write_text("0123456789" * 10)
    r = await agent.read_file(str(docs / "long.txt"), offset=0, max_chars=30)
    assert "offset=30" in txt(r)
    r = await agent.read_file(str(docs / "long.txt"), offset=90, max_chars=30)
    assert "未读完" not in txt(r)
    (docs / "gbk.txt").write_bytes("中文内容".encode("gbk"))
    r = await agent.read_file(str(docs / "gbk.txt"))
    assert "中文内容" in txt(r) and "gb18030" in txt(r)
    (docs / "bom.txt").write_bytes(b"\xef\xbb\xbfhi")
    assert "utf-8-sig" in txt(await agent.read_file(str(docs / "bom.txt")))
    (docs / "u16.txt").write_bytes("héllo".encode("utf-16"))
    assert "héllo" in txt(await agent.read_file(str(docs / "u16.txt")))


async def test_read_errors(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "bin.dat").write_bytes(b"\x00\x01\x02" * 10)
    assert "二进制" in txt(await agent.read_file(str(docs / "bin.dat")))
    assert "不存在" in txt(await agent.read_file(str(docs / "nope.txt")))
    assert "list_files" in txt(await agent.read_file(str(docs)))
    (docs / "a.txt").write_text("abc")
    assert (await agent.read_file(str(docs / "a.txt"), offset=99)).is_error
    assert (await agent.read_file(str(docs / "a.txt"), max_chars=0)).is_error


# ----------------------------- 列目录 ----------------------------- #
async def test_list_files_filters_and_hides_sensitive(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "a.txt").write_text("1")
    (docs / "b.md").write_text("2")
    (docs / "vault.kdbx").write_text("3")
    (docs / "sub").mkdir()
    (docs / "sub" / "c.txt").write_text("4")
    r = txt(await agent.list_files(str(docs)))
    assert "a.txt" in r and "sub/" in r and "vault.kdbx" not in r
    r = txt(await agent.list_files(str(docs), pattern="*.txt", recursive=True))
    assert "a.txt" in r and "sub/c.txt" in r and "b.md" not in r
    r = txt(await agent.list_files(str(docs), max_entries=1))
    assert "截断" in r


# ----------------------------- 写 ----------------------------- #
async def test_write_always_prompts_then_remembers_dir(tmp_path):
    agent, fb, docs = make(tmp_path)
    r = await agent.write_file(str(docs / "new.txt"), "hello")
    assert not r.is_error and (docs / "new.txt").read_text() == "hello"
    assert len(fb.confirm_calls) == 1  # roots 内的写入也要确认
    await agent.write_file(str(docs / "new2.txt"), "again")
    assert len(fb.confirm_calls) == 1  # 同目录本会话不再问


async def test_write_denied_changes_nothing(tmp_path):
    agent, fb, docs = make(tmp_path)
    fb.confirm_result = False
    r = await agent.write_file(str(docs / "new.txt"), "hello")
    assert r.is_error and not (docs / "new.txt").exists()


async def test_write_modes_and_backup(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "a.txt"
    await agent.write_file(str(f), "one\n")
    r = await agent.write_file(str(f), "two\n")  # 默认 create：不覆盖
    assert r.is_error and f.read_text() == "one\n"
    r = await agent.write_file(str(f), "two\n", mode="overwrite")
    assert not r.is_error and f.read_text() == "two\n" and "已备份" in txt(r)
    backups = list((tmp_path / "logs" / "file_backups").glob("*-a.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "one\n"
    await agent.write_file(str(f), "three\n", mode="append")
    assert f.read_text() == "two\nthree\n"
    assert (await agent.write_file(str(f), "x", mode="bogus")).is_error


async def test_write_preserves_crlf_and_encoding(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "w.txt"
    f.write_bytes("第一行\r\n第二行\r\n".encode("gbk"))
    await agent.write_file(str(f), "第三行\n", mode="append")
    assert f.read_bytes() == "第一行\r\n第二行\r\n第三行\r\n".encode("gbk")


async def test_write_dirs_and_executables(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    r = await agent.write_file(str(docs / "n" / "a.txt"), "x")
    assert r.is_error and "create_dirs" in txt(r)
    r = await agent.write_file(str(docs / "n" / "a.txt"), "x", create_dirs=True)
    assert not r.is_error and (docs / "n" / "a.txt").exists()
    r = await agent.write_file(str(docs / "evil.bat"), "calc")
    assert r.is_error and not (docs / "evil.bat").exists()
    assert not list(docs.glob(".winpc-*.tmp"))  # 没有遗留临时文件


# ----------------------------- 改 ----------------------------- #
async def test_edit_unique_replace_and_backup(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "e.txt"
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    r = await agent.edit_file(str(f), "beta", "BETA")
    assert not r.is_error and f.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"
    assert "第 2 行" in txt(r) and "CRLF" in txt(r)
    assert len(list((tmp_path / "logs" / "file_backups").glob("*-e.txt"))) == 1


async def test_edit_multiline_old_string_with_lf_matches_crlf_file(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "e.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    assert not (await agent.edit_file(str(f), "a\nb", "X\nY")).is_error
    assert f.read_bytes() == b"X\r\nY\r\nc\r\n"


async def test_edit_errors_leave_file_untouched(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    f = docs / "e.txt"
    f.write_text("x x x")
    for old, new, kw in [("zzz", "y", {}), ("x", "y", {}), ("", "y", {}), ("x", "x", {})]:
        r = await agent.edit_file(str(f), old, new, **kw)
        assert r.is_error and f.read_text() == "x x x"
    r = await agent.edit_file(str(f), "x", "y", replace_all=True)
    assert not r.is_error and f.read_text() == "y y y"
    assert (await agent.edit_file(str(docs / "nope.txt"), "a", "b")).is_error


# ----------------------------- 授权模式 / 暂停 / 审计 ----------------------------- #
async def test_allowlist_mode_rejects_without_prompt(tmp_path):
    agent, fb, docs = make(tmp_path, approval_mode="allowlist")
    (docs / "a.txt").write_text("ok")
    assert not (await agent.read_file(str(docs / "a.txt"))).is_error        # roots 内读取仍可
    r = await agent.write_file(str(docs / "b.txt"), "x")
    assert r.is_error and fb.confirm_calls == [] and not (docs / "b.txt").exists()
    (tmp_path / "o.txt").write_text("x")
    assert (await agent.read_file(str(tmp_path / "o.txt"))).is_error        # roots 外读取被拒


async def test_all_mode_still_blocks_sensitive_paths(tmp_path):
    agent, fb, _ = make(tmp_path, approval_mode="all", file_roots=[str(tmp_path)])
    k = tmp_path / ".ssh" / "id_rsa"
    k.parent.mkdir()
    k.write_text("PRIVATE")
    r = await agent.read_file(str(k))
    assert r.is_error and "PRIVATE" not in txt(r)
    assert (await agent.write_file(str(tmp_path / "a.bat"), "x")).is_error


async def test_pause_blocks_writes_but_not_reads(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    (docs / "a.txt").write_text("hi")
    agent.paused = True
    assert (await agent.write_file(str(docs / "b.txt"), "x")).is_error
    assert (await agent.edit_file(str(docs / "a.txt"), "hi", "yo")).is_error
    assert (docs / "a.txt").read_text() == "hi" and not (docs / "b.txt").exists()
    assert not (await agent.read_file(str(docs / "a.txt"))).is_error


async def test_file_tools_can_be_disabled(tmp_path):
    agent, _, docs = make(tmp_path, file_tools_enabled=False)
    (docs / "a.txt").write_text("hi")
    assert (await agent.read_file(str(docs / "a.txt"))).is_error


async def test_audit_redacts_content(tmp_path):
    agent, _, docs = make(tmp_path, approval_mode="all")
    secret = "TOP-SECRET-" + "x" * 50
    await agent.write_file(str(docs / "a.txt"), secret)
    await agent.edit_file(str(docs / "a.txt"), "TOP-SECRET-", "PUBLIC-")
    lines = [json.loads(x) for x in (tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
    raw = json.dumps(lines, ensure_ascii=False)
    assert secret not in raw
    w = next(x for x in lines if x["tool"] == "write_file")
    assert w["params"]["content_len"] == len(secret) and w["process"].startswith("file:")


# ----------------------------- 安全加固回归测试 ----------------------------- #
@pytest.mark.parametrize("mode", ["all", "prompt"])
async def test_private_key_blocked_by_content_not_name(tmp_path, mode):
    """私钥放在非常规目录、文件名随意（如 ...\\ssh\\myserver）也必须读不到，即使在 all 模式或用户点了“是”。"""
    agent, fb, docs = make(tmp_path, approval_mode=mode)
    fb.confirm_result = True
    for header in ("OPENSSH", "RSA", "EC", "ENCRYPTED"):
        key = tmp_path / "ssh" / f"myserver_{header}"
        key.parent.mkdir(exist_ok=True)
        key.write_text(f"-----BEGIN {header} PRIVATE KEY-----\nAAAA\n-----END {header} PRIVATE KEY-----\n")
        r = await agent.read_file(str(key))
        assert r.is_error and "AAAA" not in txt(r)
    putty = tmp_path / "ssh" / "k.txt"
    putty.write_text("PuTTY-User-Key-File-3: ssh-ed25519\nPublic-Lines: 2\n")
    assert (await agent.read_file(str(putty))).is_error
    # 不能通过 edit / 覆盖写入来读取或破坏它
    assert (await agent.edit_file(str(key), "AAAA", "BBBB")).is_error
    assert (await agent.write_file(str(key), "x", mode="overwrite")).is_error
    assert "AAAA" in key.read_text()


async def test_public_key_and_plain_text_still_readable(tmp_path):
    agent, _, docs = make(tmp_path)
    (docs / "a.pub").write_text("ssh-ed25519 AAAAC3Nza... me@host\n")
    (docs / "notes.md").write_text("PRIVATE KEY 的说明文字，没有 BEGIN 头。\n")
    assert not (await agent.read_file(str(docs / "a.pub"))).is_error
    assert not (await agent.read_file(str(docs / "notes.md"))).is_error


@pytest.mark.parametrize("name", [".env", ".netrc", ".npmrc", ".git-credentials", "credentials.json"])
async def test_credential_file_names_blocked_even_in_all_mode(tmp_path, name):
    agent, _, docs = make(tmp_path, approval_mode="all")
    (docs / name).write_text("TOKEN=abc")
    r = await agent.read_file(str(docs / name))
    assert r.is_error and "abc" not in txt(r)


@pytest.mark.parametrize("ext", [".jse", ".wsh", ".url", ".cpl", ".msc", ".scf", ".pif"])
async def test_more_autorun_extensions_blocked(tmp_path, ext):
    agent, _, docs = make(tmp_path, approval_mode="all")
    assert (await agent.write_file(str(docs / f"x{ext}"), "x")).is_error


def test_config_outside_agent_dir_is_blocked(tmp_path):
    """--config 指向 Agent 目录之外时，含令牌的 config.json 同样不能被文件工具读到。"""
    from winpc_agent import load_config
    cfg_path = tmp_path / "elsewhere" / "config.json"
    cfg_path.parent.mkdir()
    original = json.dumps({"token": "FAKE-TOKEN-FOR-TEST", "file_roots": [str(tmp_path / "elsewhere")]})
    cfg_path.write_text(original)
    cfg = load_config(cfg_path)
    agent = WinPCAgent(cfg, FakeBackend())
    with pytest.raises(FileError):
        agent.policy.classify(agent.policy.resolve(str(cfg_path)), write=False)
    assert cfg_path.read_text() == original  # 已有令牌：不改写文件


def test_generated_token_saved_without_runtime_block_rule(tmp_path, monkeypatch, capsys):
    from winpc_agent import load_config
    monkeypatch.delenv("WINPC_TOKEN", raising=False)
    p = tmp_path / "config.json"
    cfg = load_config(p)
    saved = json.loads(p.read_text())
    assert saved["token"] == cfg.token and len(cfg.token) >= 32
    assert saved["file_blocked_paths"] == []
    assert str(p.resolve()) in cfg.file_blocked_paths
