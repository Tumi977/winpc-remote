"""实机测试中发现的两个缺陷的回归测试：
1) type_text 一次塞太多按键事件，新版记事本会丢字/错字 → 逐字符发送并留间隔；
2) 确认框抢走焦点后，输入被发给了没有被授权的窗口 → 授权后重新核对窗口，变了就中止。"""
import pytest

import backends
from backends import FakeBackend, WindowsBackend
from winpc_agent import Config, WinPCAgent


def txt(r):
    return r.content[0].text


# ----------------------------- 1) 按键事件的分组与节奏 ----------------------------- #
def flags(group):
    return [(e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in group]


def test_one_group_per_character_and_events_are_down_up_pairs():
    groups = WindowsBackend._char_events("ab\n\t中")
    assert len(groups) == 5
    assert all(len(g) == 2 for g in groups)
    assert flags(groups[0]) == [(0, ord("a"), 0x4), (0, ord("a"), 0x4 | 0x2)]          # UNICODE 按下 / 抬起
    assert flags(groups[2])[0][0] == 0x0D and flags(groups[3])[0][0] == 0x09          # Enter / Tab 走虚拟键
    assert flags(groups[4])[0][1] == ord("中")


def test_emoji_surrogate_pair_stays_in_one_group():
    """emoji 是两个 UTF-16 码元：必须在同一组里发送，拆开会变成乱码。"""
    (g,) = WindowsBackend._char_events("🙂")
    assert len(g) == 4
    units = [e.ki.wScan for e in g if not e.ki.dwFlags & 0x2]
    assert units == [0xD83D, 0xDE42]


def test_crlf_and_cr_are_normalised_to_single_enter():
    assert len(WindowsBackend._char_events("a\r\nb\rc")) == 5      # a, Enter, b, Enter, c


@pytest.fixture
def typer(monkeypatch):
    """不需要 Windows：绕过 __init__，记录每次 SendInput 的字符数和每次 sleep。"""
    def make(chunk, delay_ms):
        b = object.__new__(WindowsBackend)
        b.type_chunk_chars, b.type_delay_s = chunk, delay_ms / 1000
        sends, sleeps = [], []
        b._failsafe_check = lambda: None
        b._send = lambda evs: sends.append(len(evs))
        monkeypatch.setattr(backends.time, "sleep", lambda s: sleeps.append(s))
        return b, sends, sleeps
    return make


def test_default_is_char_by_char_with_a_delay_after_each(typer):
    b, sends, sleeps = typer(1, 5)
    b.type_text("Line 3: type_text OK 🙂")           # 这正是实机上被写成 ttttt… 的那类字符串
    assert len(sends) == 22 and all(n in (2, 4) for n in sends)      # 没有任何一次发送超过一个字符（emoji 4 个事件）
    assert max(sends) == 4 and sends.count(4) == 1
    assert sleeps == [0.005] * 22


def test_chunking_and_zero_delay_are_configurable(typer):
    b, sends, sleeps = typer(3, 0)
    b.type_text("abcdefg")
    assert sends == [6, 6, 2] and sleeps == []


def test_constructor_clamps_bad_values(monkeypatch):
    monkeypatch.setattr(backends.sys, "platform", "linux")
    with pytest.raises(RuntimeError):               # 非 Windows 仍然拒绝构造，但参数在此之前已被规整
        WindowsBackend(type_chunk_chars=0, type_delay_ms=-3)


def test_config_defaults_and_overrides():
    c = Config()
    assert (c.type_chunk_chars, c.type_delay_ms) == (1, 5)
    assert Config(type_chunk_chars=4, type_delay_ms=12).type_delay_ms == 12


# ----------------------------- 2) 授权之后重新核对窗口 ----------------------------- #
def make(tmp_path, **cfg):
    fb = FakeBackend()
    agent = WinPCAgent(Config(log_dir=str(tmp_path / "logs"), settle_ms=0, **cfg), fb)
    return agent, fb


def steal_focus_to(fb, process, title="VS Code"):
    def go():
        fb.foreground_process, fb.foreground_title = process, title
        fb.point_process = None
    fb.during_confirm = go


async def test_focus_stolen_during_confirm_aborts_type_text(tmp_path):
    """复现实机事故：批准 searchhost.exe 时弹框关掉了开始菜单，输入本会落进 code.exe。"""
    agent, fb = make(tmp_path)
    fb.foreground_process, fb.foreground_title = "searchhost.exe", "搜索"
    steal_focus_to(fb, "code.exe")
    r = await agent.type_text("记事本", screenshot_after=False)
    assert r.is_error and "授权期间目标窗口变了" in txt(r) and "没有发送任何输入" in txt(r)
    assert not [a for a in fb.actions if a["op"] == "type_text"], "输入绝不能发给未授权的窗口"
    assert "searchhost.exe" in agent.session_approved       # 已授权，稍后重试不会再弹框


async def test_retry_after_focus_is_restored_does_not_prompt_again(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    assert (await agent.type_text("x", screenshot_after=False)).is_error
    fb.during_confirm = None
    fb.foreground_process, fb.foreground_title = "searchhost.exe", "搜索"      # 用户/Agent 把开始菜单重新打开
    n = len(fb.confirm_calls)
    r = await agent.type_text("abc", screenshot_after=False)
    assert not r.is_error and len(fb.confirm_calls) == n
    assert fb.actions[-1] == {"op": "type_text", "text": "abc"}


async def test_focus_stolen_during_confirm_aborts_press_key_and_click(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = fb.point_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    assert "授权期间" in txt(await agent.press_key("enter", screenshot_after=False))
    assert not fb.actions
    agent2, fb2 = make(tmp_path / "b")
    await agent2.screenshot()
    fb2.point_process = "searchhost.exe"
    fb2.during_confirm = lambda: setattr(fb2, "point_process", "code.exe")      # 鼠标下的窗口变了
    r = await agent2.click(10, 10, screenshot_after=False)
    assert r.is_error and "授权期间" in txt(r) and not [a for a in fb2.actions if a["op"] == "click"]


async def test_window_swapped_after_approval_is_still_caught(tmp_path):
    """已授权的应用也要核对：授权检查与真正发送之间窗口被换成了别的应用。"""
    agent, fb = make(tmp_path, allowed_apps=["notepad.exe", "code.exe"])
    calls = {"n": 0}
    real = fb.foreground_window

    def swapping():
        calls["n"] += 1
        if calls["n"] >= 2:                         # 第 1 次用于授权判断，第 2 次是发送前的复核
            fb.foreground_process = "chrome.exe"
        return real()
    fb.foreground_window = swapping
    r = await agent.type_text("secret?", screenshot_after=False)
    assert r.is_error and not fb.actions


async def test_unchanged_window_still_works_and_prompts_once(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "chrome.exe"
    for _ in range(2):
        assert not (await agent.type_text("ok", screenshot_after=False)).is_error
    assert len(fb.confirm_calls) == 1
    assert [a["text"] for a in fb.actions] == ["ok", "ok"]


async def test_blocklist_and_deny_paths_unchanged(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "1password.exe"
    assert "黑名单" in txt(await agent.type_text("x", screenshot_after=False))
    fb.foreground_process, fb.confirm_result = "chrome.exe", False
    assert "拒绝" in txt(await agent.type_text("x", screenshot_after=False))
    assert not fb.actions


# ----------------------------- 3) 粘贴模式 / 回车后延迟 / 授权框 ----------------------------- #
async def test_method_paste_routes_to_paste_text_and_audit_records_method(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    r = await agent.type_text("多行\n文本 🙂", method="paste", screenshot_after=False)
    assert not r.is_error and "已粘贴" in txt(r)
    assert fb.actions[-1] == {"op": "paste_text", "text": "多行\n文本 🙂"}
    assert not [a for a in fb.actions if a["op"] == "type_text"]
    import json
    entry = json.loads((tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["params"]["method"] == "paste" and entry["params"]["text_len"] == 7


async def test_default_method_is_keys_and_invalid_method_rejected(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    await agent.type_text("abc", screenshot_after=False)
    assert fb.actions[-1]["op"] == "type_text"
    assert "keys 或 paste" in txt(await agent.type_text("abc", method="clipboard", screenshot_after=False))


async def test_paste_limit_is_larger_than_keys_limit(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    assert (await agent.type_text("x" * 10_001, screenshot_after=False)).is_error
    assert not (await agent.type_text("x" * 10_001, method="paste", screenshot_after=False)).is_error
    assert (await agent.type_text("x" * 100_001, method="paste", screenshot_after=False)).is_error


async def test_paste_obeys_authorization_and_focus_recheck(tmp_path):
    agent, fb = make(tmp_path)                 # prompt 模式
    fb.foreground_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    r = await agent.type_text("x", method="paste", screenshot_after=False)
    assert r.is_error and "授权期间" in txt(r) and not fb.actions
    agent.paused = True
    assert "暂停" in txt(await agent.type_text("x", method="paste", screenshot_after=False))


async def test_paste_note_is_returned_to_the_model(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.paste_text = lambda text: "（注意：原剪贴板里有 2 种非文本格式…）"
    assert "非文本格式" in txt(await agent.type_text("x", method="paste", screenshot_after=False))


def test_enter_gets_an_extra_pause_in_keys_mode(typer):
    b, sends, sleeps = typer(1, 5)
    b.type_newline_delay_s = 0.15
    b.type_text("ab\ncd")
    assert sleeps == [0.005, 0.005, 0.15, 0.005, 0.005]


def test_newline_delay_never_shortens_a_longer_base_delay(typer):
    b, sends, sleeps = typer(1, 400)
    b.type_newline_delay_s = 0.15
    b.type_text("a\n")
    assert sleeps == [0.4, 0.4]


# ---- 授权框：tk 优先，任何异常都回退到系统消息框 ----
def bare_backend():
    b = object.__new__(WindowsBackend)
    b.calls = []
    b._confirm_messagebox = lambda t, m, s: b.calls.append("messagebox") or True
    return b


def test_confirm_uses_tk_answer_when_available():
    b = bare_backend()
    b._confirm_tk = lambda t, m, s: False
    assert b.confirm("t", "m", 5) is False and b.calls == []


@pytest.mark.parametrize("tk_result", ["raises", "none"])
def test_confirm_falls_back_to_messagebox(tk_result):
    b = bare_backend()
    if tk_result == "raises":
        def boom(*a):
            raise RuntimeError("tkinter 不可用")
        b._confirm_tk = boom
    else:
        b._confirm_tk = lambda *a: None
    assert b.confirm("t", "m", 5) is True and b.calls == ["messagebox"]


# ---- 用模拟的 Win32 剪贴板 API 验证 paste_text 的流程 ----
class FakeWin32:
    """最小的 user32/kernel32 剪贴板模拟：记录调用顺序，剪贴板内容放在 dict 里。"""

    def __init__(self, clip: dict[int, str | bytes], fail_send=False):
        import ctypes
        self.ct, self.clip, self.log, self.fail_send = ctypes, dict(clip), [], fail_send
        self.mem, self.opened, self.owner = {}, False, None
        self.user32 = self.kernel32 = self

    # user32
    def CreateWindowExW(self, *a):
        self.log.append("create_owner"); return 77

    def DestroyWindow(self, h):
        self.log.append("destroy_owner")

    def OpenClipboard(self, hwnd):
        assert hwnd == 77, "OpenClipboard(NULL) 之后 SetClipboardData 会失败，必须传所有者窗口"
        self.opened = True; self.log.append("open"); return 1

    def CloseClipboard(self):
        self.opened = False; self.log.append("close"); return 1

    def EmptyClipboard(self):
        assert self.opened; self.clip.clear(); self.log.append("empty"); return 1

    def EnumClipboardFormats(self, f):
        keys = sorted(self.clip)
        if f == 0:
            return keys[0] if keys else 0
        i = keys.index(f) + 1
        return keys[i] if i < len(keys) else 0

    def GetClipboardData(self, fmt):
        v = self.clip.get(fmt)
        if v is None:
            return None
        data = (v + "\0").encode("utf-16-le")
        buf = self.ct.create_string_buffer(data, len(data))
        self.mem[self.ct.addressof(buf)] = buf
        return self.ct.addressof(buf)

    def SetClipboardData(self, fmt, handle):
        assert self.opened
        raw = bytes(self.mem[handle])
        self.clip[fmt] = raw.decode("utf-16-le").split("\0", 1)[0]
        self.log.append(f"set:{self.clip[fmt]!r}")
        return handle

    # kernel32
    def GlobalAlloc(self, flags, size):
        buf = self.ct.create_string_buffer(size); a = self.ct.addressof(buf); self.mem[a] = buf; return a

    def GlobalLock(self, h):
        return h

    def GlobalUnlock(self, h):
        return 1

    def GlobalSize(self, h):
        return len(self.mem[h])

    def GlobalFree(self, h):
        self.mem.pop(h, None)


def paste_backend(clip, fail_send=False, monkeypatch=None):
    api = FakeWin32(clip)
    b = object.__new__(WindowsBackend)
    b.user32 = b.kernel32 = api
    b.paste_restore_delay_s = 0
    b._failsafe_check = lambda: None

    def send(events):
        api.log.append(f"send:{len(events)}")
        if fail_send:
            raise backends.BackendError("SendInput 被拒绝")
    b._send = send
    return b, api


def test_paste_text_sequence_and_restore(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({13: "我的原剪贴板"})
    note = b.paste_text("行1\n行2")
    assert note == ""
    assert api.log[:5] == ["create_owner", "open", "empty", "set:'行1\\r\\n行2'", "close"]      # 换行规范成 CRLF
    assert api.log[5] == "send:4"                                                               # Ctrl+V 四个事件
    assert api.log[6:-1] == ["open", "empty", "set:'我的原剪贴板'", "close"]                    # 粘贴之后才恢复
    assert api.log[-1] == "destroy_owner"
    assert api.clip == {13: "我的原剪贴板"}, "剪贴板必须恢复成原来的文本"


def test_paste_restores_clipboard_even_if_send_fails(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({13: "原文"}, fail_send=True)
    with pytest.raises(backends.BackendError):
        b.paste_text("新内容")
    assert api.clip == {13: "原文"} and api.log[-1] == "destroy_owner"


def test_paste_with_empty_clipboard_leaves_it_empty_and_warns_about_other_formats(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({})
    assert b.paste_text("x") == "" and api.clip == {}
    b2, api2 = paste_backend({2: "bitmap-like", 49161: "html"})               # 只有非文本格式
    note = b2.paste_text("x")
    assert "2 种非文本格式" in note and api2.clip == {}


def test_paste_empty_text_does_nothing(monkeypatch):
    b, api = paste_backend({13: "keep"})
    assert b.paste_text("") == "" and api.log == [] and api.clip == {13: "keep"}


# ----------------------------- 4) expect_app：声明要操作的应用，前台不是它就拒绝 ----------------------------- #
async def test_expect_app_mismatch_refuses_type_text_and_press_key(tmp_path):
    """复现实机事故（三次）：用户切到了 VS Code，按键却发给了 code.exe。"""
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process, fb.foreground_title = "code.exe", "VS Code"
    r = await agent.type_text("E:\\windows-agent\\bin\\win-remote-on.cmd", expect_app="windowsterminal",
                              screenshot_after=False)
    assert r.is_error and "不是预期的 windowsterminal.exe" in txt(r) and "没有发送任何输入" in txt(r)
    r = await agent.press_key("ctrl+a", expect_app="notepad.exe", screenshot_after=False)
    assert r.is_error and "code.exe" in txt(r)
    assert not fb.actions


async def test_expect_app_match_is_case_and_extension_insensitive(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "windowsterminal.exe"
    for spec in ("windowsterminal", "WindowsTerminal.EXE", " windowsterminal "):
        assert not (await agent.type_text("x", expect_app=spec, screenshot_after=False)).is_error
    assert not (await agent.press_key("enter", expect_app="windowsterminal", screenshot_after=False)).is_error
    assert len(fb.actions) == 4


async def test_expect_app_is_checked_before_any_prompt_and_unknown_foreground_is_refused(tmp_path):
    agent, fb = make(tmp_path)                    # prompt 模式
    fb.foreground_process = "code.exe"
    r = await agent.type_text("x", expect_app="notepad", screenshot_after=False)
    assert r.is_error and not fb.confirm_calls, "窗口不对时不应该先弹授权框"
    fb.foreground_process = None                  # 前台窗口未知
    assert (await agent.type_text("x", expect_app="notepad", screenshot_after=False)).is_error
    assert not fb.actions


async def test_without_expect_app_behaviour_is_unchanged(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "anything.exe"
    assert not (await agent.type_text("x", screenshot_after=False)).is_error


async def test_expect_app_also_applies_to_paste_method(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "code.exe"
    assert (await agent.type_text("x", method="paste", expect_app="notepad", screenshot_after=False)).is_error
    assert not fb.actions


async def test_expect_handle_distinguishes_two_windows_of_the_same_process(tmp_path):
    """好几个终端都是 windowsterminal.exe（Agent 窗口、隧道窗口、我的终端），按进程名分不开，必须能按句柄校验。"""
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "windowsterminal.exe"
    mine = fb.foreground_window().handle
    ok = await agent.type_text("x", expect_app="windowsterminal", expect_handle=mine, screenshot_after=False)
    assert not ok.is_error
    r = await agent.type_text("x", expect_app="windowsterminal", expect_handle=mine + 1, screenshot_after=False)
    assert r.is_error and f"handle={mine + 1}" in txt(r) and "没有发送任何输入" in txt(r)
    r = await agent.press_key("enter", expect_handle=mine + 1, screenshot_after=False)     # 只给 handle 也行
    assert r.is_error
    assert len(fb.actions) == 1


async def test_expect_handle_zero_is_still_a_real_constraint_and_none_is_not(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "x.exe"
    assert (await agent.type_text("x", expect_handle=0, screenshot_after=False)).is_error     # 0 不是 None
    assert not (await agent.type_text("x", expect_handle=None, screenshot_after=False)).is_error


# ---- 授权框子进程：实机上 Agent 因线程内 tkinter 的 Tcl_AsyncDelete 崩溃过，必须进程隔离 ----
import ast
import sys as _sys
import textwrap as _tw


def test_tkinter_is_never_imported_in_the_agent_process_code_paths():
    """tkinter 只能出现在子进程入口 run_confirm_dialog 里；Agent 进程里任何地方 import tkinter 都可能重现崩溃。"""
    tree = ast.parse(open(backends.__file__, encoding="utf-8").read())
    owners = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                        (a.name or "").split(".")[0] == "tkinter" for a in getattr(n, "names", [])) or (
                        isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "tkinter"):
                    owners.add(fn.name)
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and "tkinter" in ast.dump(n)]
    assert not top, "tkinter 不能在模块顶层导入"
    assert owners == {"run_confirm_dialog"}, owners


def fake_dialog(monkeypatch, code: str, slack=0.5):
    b = object.__new__(WindowsBackend)
    b.dialog_slack_s = slack
    f = _tw.dedent(code)
    monkeypatch.setattr(WindowsBackend, "_dialog_command", staticmethod(lambda: [_sys.executable, "-c", f]))
    return b


def test_dialog_subprocess_yes_and_no_and_payload_roundtrip(monkeypatch):
    yes = fake_dialog(monkeypatch, """
        import json, sys
        a = json.loads(sys.stdin.read())
        assert a["title"] == "标题 ⚠" and a["message"] == "行1\\n行2" and a["timeout_s"] == 5
        print("noise before"); print("WINPC_CONFIRM:YES")
    """)
    assert yes._confirm_tk("标题 ⚠", "行1\n行2", 5) is True
    no = fake_dialog(monkeypatch, 'print("WINPC_CONFIRM:NO")')
    assert no._confirm_tk("t", "m", 5) is False


@pytest.mark.parametrize("code", ["import sys; sys.exit(3)",                        # 崩溃，无输出
                                  'print("garbage")',                                # 乱码输出
                                  "import os; os._exit(134)",                        # 类似 Tcl_AsyncDelete 的 abort
                                  ""])                                               # 空
def test_dialog_subprocess_failures_return_none_so_caller_falls_back(monkeypatch, code):
    b = fake_dialog(monkeypatch, code or "pass")
    assert b._confirm_tk("t", "m", 2) is None


def test_dialog_subprocess_hang_is_killed_and_counts_as_deny(monkeypatch):
    b = fake_dialog(monkeypatch, "import time; time.sleep(60)", slack=0.3)
    import time as _t
    t0 = _t.time()
    assert b._confirm_tk("t", "m", 0.3) is False
    assert _t.time() - t0 < 10


def test_dialog_cannot_start_returns_none(monkeypatch):
    b = object.__new__(WindowsBackend)
    b.dialog_slack_s = 1
    monkeypatch.setattr(WindowsBackend, "_dialog_command", staticmethod(lambda: ["/no/such/python"]))
    assert b._confirm_tk("t", "m", 1) is None


def test_confirm_never_crashes_the_agent_when_dialog_process_dies(monkeypatch):
    b = fake_dialog(monkeypatch, "import os; os._exit(134)")
    b._confirm_messagebox = lambda t, m, s: False        # 回退到系统消息框
    assert b.confirm("t", "m", 2) is False


import io as _io


class _FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = _io.BytesIO(data)

    def read(self):                                 # 老写法 sys.stdin.read()：按系统代码页解码，故意造成乱码
        return self.buffer.getvalue().decode("gbk", errors="replace")


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_dialog_entry_decodes_utf8_stdin_with_or_without_bom(monkeypatch, capsys, bom):
    """实机自检发现：PowerShell 管道带 BOM 会让 json 解析失败；且 sys.stdin 默认按 GBK 解码会让中文乱码。"""
    import json
    payload = json.dumps({"title": "WinPC Remote - 应用授权", "message": "请求操作应用：searchhost.exe\n动作：输入 3 个字符",
                          "timeout_s": 5}, ensure_ascii=False).encode("utf-8")
    seen = {}
    monkeypatch.setattr(backends, "run_confirm_dialog", lambda t, m, s: seen.update(t=t, m=m, s=s) or True)
    monkeypatch.setattr(backends.sys, "stdin", _FakeStdin(bom + payload))
    assert backends._dialog_main() == 0
    assert seen == {"t": "WinPC Remote - 应用授权", "m": "请求操作应用：searchhost.exe\n动作：输入 3 个字符", "s": 5.0}
    assert "WINPC_CONFIRM:YES" in capsys.readouterr().out
