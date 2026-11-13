"""Regression tests for two defects found in live testing:
1) type_text pushed too many key events at once and the new Notepad dropped/garbled characters -> send character by character with a delay;
2) after the dialog stole focus, input was sent to a window that was never approved -> re-check the window after approval and abort if it changed."""
import pytest

import backends
from backends import FakeBackend, WindowsBackend
from winpc_agent import Config, WinPCAgent


def txt(r):
    return r.content[0].text


# ----------------------------- 1) grouping and pacing of key events ----------------------------- #
def flags(group):
    return [(e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in group]


def test_one_group_per_character_and_events_are_down_up_pairs():
    groups = WindowsBackend._char_events("ab\n\t\u4e2d")
    assert len(groups) == 5
    assert all(len(g) == 2 for g in groups)
    assert flags(groups[0]) == [(0, ord("a"), 0x4), (0, ord("a"), 0x4 | 0x2)]          # UNICODE down / up
    assert flags(groups[2])[0][0] == 0x0D and flags(groups[3])[0][0] == 0x09          # Enter / Tab go through virtual keys
    assert flags(groups[4])[0][1] == ord("\u4e2d")


def test_emoji_surrogate_pair_stays_in_one_group():
    """An emoji is two UTF-16 code units: they must be sent in the same group; sent separately they turn into garbage."""
    (g,) = WindowsBackend._char_events("🙂")
    assert len(g) == 4
    units = [e.ki.wScan for e in g if not e.ki.dwFlags & 0x2]
    assert units == [0xD83D, 0xDE42]


def test_crlf_and_cr_are_normalised_to_single_enter():
    assert len(WindowsBackend._char_events("a\r\nb\rc")) == 5      # a, Enter, b, Enter, c


@pytest.fixture
def typer(monkeypatch):
    """No Windows needed: bypass __init__ and record the number of characters of each SendInput and each sleep."""
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
    b.type_text("Line 3: type_text OK 🙂")           # exactly the kind of string that came out as ttttt... on the real machine
    assert len(sends) == 22 and all(n in (2, 4) for n in sends)      # no send carries more than one character (4 events for an emoji)
    assert max(sends) == 4 and sends.count(4) == 1
    assert sleeps == [0.005] * 22


def test_chunking_and_zero_delay_are_configurable(typer):
    b, sends, sleeps = typer(3, 0)
    b.type_text("abcdefg")
    assert sends == [6, 6, 2] and sleeps == []


def test_constructor_clamps_bad_values(monkeypatch):
    monkeypatch.setattr(backends.sys, "platform", "linux")
    with pytest.raises(RuntimeError):               # a non-Windows platform still refuses construction, but the parameters were normalized before that
        WindowsBackend(type_chunk_chars=0, type_delay_ms=-3)


def test_config_defaults_and_overrides():
    c = Config()
    assert (c.type_chunk_chars, c.type_delay_ms) == (1, 5)
    assert Config(type_chunk_chars=4, type_delay_ms=12).type_delay_ms == 12


# ----------------------------- 2) re-check the window after authorization ----------------------------- #
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
    """Reproduces the real incident: approving searchhost.exe made the dialog close the Start menu, and the input would have gone into code.exe."""
    agent, fb = make(tmp_path)
    fb.foreground_process, fb.foreground_title = "searchhost.exe", "Search"
    steal_focus_to(fb, "code.exe")
    r = await agent.type_text("\u8bb0\u4e8b\u672c", screenshot_after=False)
    assert r.is_error and "target window changed during authorization" in txt(r) and "nothing was sent" in txt(r)
    assert not [a for a in fb.actions if a["op"] == "type_text"], "input must never go to an unapproved window"
    assert "searchhost.exe" in agent.session_approved       # approved, so a later retry shows no dialog


async def test_retry_after_focus_is_restored_does_not_prompt_again(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    assert (await agent.type_text("x", screenshot_after=False)).is_error
    fb.during_confirm = None
    fb.foreground_process, fb.foreground_title = "searchhost.exe", "Search"      # the user/Agent reopened the Start menu
    n = len(fb.confirm_calls)
    r = await agent.type_text("abc", screenshot_after=False)
    assert not r.is_error and len(fb.confirm_calls) == n
    assert fb.actions[-1] == {"op": "type_text", "text": "abc"}


async def test_focus_stolen_during_confirm_aborts_press_key_and_click(tmp_path):
    agent, fb = make(tmp_path)
    fb.foreground_process = fb.point_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    assert "during authorization" in txt(await agent.press_key("enter", screenshot_after=False))
    assert not fb.actions
    agent2, fb2 = make(tmp_path / "b")
    await agent2.screenshot()
    fb2.point_process = "searchhost.exe"
    fb2.during_confirm = lambda: setattr(fb2, "point_process", "code.exe")      # the window under the mouse changed
    r = await agent2.click(10, 10, screenshot_after=False)
    assert r.is_error and "during authorization" in txt(r) and not [a for a in fb2.actions if a["op"] == "click"]


async def test_window_swapped_after_approval_is_still_caught(tmp_path):
    """An already approved application is checked too: between the authorization check and the actual send the window was swapped for another app."""
    agent, fb = make(tmp_path, allowed_apps=["notepad.exe", "code.exe"])
    calls = {"n": 0}
    real = fb.foreground_window

    def swapping():
        calls["n"] += 1
        if calls["n"] >= 2:                         # call 1 is for the authorization decision, call 2 is the re-check before sending
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
    assert "block-list" in txt(await agent.type_text("x", screenshot_after=False))
    fb.foreground_process, fb.confirm_result = "chrome.exe", False
    assert "denied" in txt(await agent.type_text("x", screenshot_after=False))
    assert not fb.actions


# ----------------------------- 3) paste mode / delay after Enter / approval dialog ----------------------------- #
async def test_method_paste_routes_to_paste_text_and_audit_records_method(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    r = await agent.type_text("\u591a\u884c\n\u6587\u672c 🙂", method="paste", screenshot_after=False)
    assert not r.is_error and "Pasted" in txt(r)
    assert fb.actions[-1] == {"op": "paste_text", "text": "\u591a\u884c\n\u6587\u672c 🙂"}
    assert not [a for a in fb.actions if a["op"] == "type_text"]
    import json
    entry = json.loads((tmp_path / "logs" / "actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["params"]["method"] == "paste" and entry["params"]["text_len"] == 7


async def test_default_method_is_keys_and_invalid_method_rejected(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    await agent.type_text("abc", screenshot_after=False)
    assert fb.actions[-1]["op"] == "type_text"
    assert "keys or paste" in txt(await agent.type_text("abc", method="clipboard", screenshot_after=False))


async def test_paste_limit_is_larger_than_keys_limit(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    assert (await agent.type_text("x" * 10_001, screenshot_after=False)).is_error
    assert not (await agent.type_text("x" * 10_001, method="paste", screenshot_after=False)).is_error
    assert (await agent.type_text("x" * 100_001, method="paste", screenshot_after=False)).is_error


async def test_paste_obeys_authorization_and_focus_recheck(tmp_path):
    agent, fb = make(tmp_path)                 # prompt mode
    fb.foreground_process = "searchhost.exe"
    steal_focus_to(fb, "code.exe")
    r = await agent.type_text("x", method="paste", screenshot_after=False)
    assert r.is_error and "during authorization" in txt(r) and not fb.actions
    agent.paused = True
    assert "Paused" in txt(await agent.type_text("x", method="paste", screenshot_after=False))


async def test_paste_note_is_returned_to_the_model(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.paste_text = lambda text: "(Note: the original clipboard held 2 non-text formats...)"
    assert "non-text format" in txt(await agent.type_text("x", method="paste", screenshot_after=False))


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


# ---- approval dialog: tk first, any exception falls back to the system message box ----
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
            raise RuntimeError("tkinter unavailable")
        b._confirm_tk = boom
    else:
        b._confirm_tk = lambda *a: None
    assert b.confirm("t", "m", 5) is True and b.calls == ["messagebox"]


# ---- verify the flow of paste_text with a simulated Win32 clipboard API ----
class FakeWin32:
    """A minimal user32/kernel32 clipboard simulation: records the call order; the clipboard content lives in a dict."""

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
        assert hwnd == 77, "SetClipboardData fails after OpenClipboard(NULL); the owner window must be passed"
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
            raise backends.BackendError("SendInput was rejected")
    b._send = send
    return b, api


def test_paste_text_sequence_and_restore(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({13: "\u6211\u7684\u539f\u526a\u8d34\u677f"})
    note = b.paste_text("\u884c1\n\u884c2")
    assert note == ""
    assert api.log[:5] == ["create_owner", "open", "empty", "set:'\u884c1\\r\\n\u884c2'", "close"]      # newlines are normalized to CRLF
    assert api.log[5] == "send:4"                                                               # four events for Ctrl+V
    assert api.log[6:-1] == ["open", "empty", "set:'\u6211\u7684\u539f\u526a\u8d34\u677f'", "close"]                    # restored only after the paste
    assert api.log[-1] == "destroy_owner"
    assert api.clip == {13: "\u6211\u7684\u539f\u526a\u8d34\u677f"}, "the clipboard must be restored to the original text"


def test_paste_restores_clipboard_even_if_send_fails(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({13: "\u539f\u6587"}, fail_send=True)
    with pytest.raises(backends.BackendError):
        b.paste_text("\u65b0\u5185\u5bb9")
    assert api.clip == {13: "\u539f\u6587"} and api.log[-1] == "destroy_owner"


def test_paste_with_empty_clipboard_leaves_it_empty_and_warns_about_other_formats(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda s: None)
    b, api = paste_backend({})
    assert b.paste_text("x") == "" and api.clip == {}
    b2, api2 = paste_backend({2: "bitmap-like", 49161: "html"})               # only non-text formats
    note = b2.paste_text("x")
    assert "2 non-text format" in note and api2.clip == {}


def test_paste_empty_text_does_nothing(monkeypatch):
    b, api = paste_backend({13: "keep"})
    assert b.paste_text("") == "" and api.log == [] and api.clip == {13: "keep"}


# ----------------------------- 4) expect_app: declare the app to operate; refuse if it is not in the foreground ----------------------------- #
async def test_expect_app_mismatch_refuses_type_text_and_press_key(tmp_path):
    """Reproduces the real incident (three times): the user switched to VS Code, but the keys went to code.exe."""
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process, fb.foreground_title = "code.exe", "VS Code"
    r = await agent.type_text("E:\\windows-agent\\bin\\win-remote-on.cmd", expect_app="windowsterminal",
                              screenshot_after=False)
    assert r.is_error and "not the expected windowsterminal.exe" in txt(r) and "nothing was sent" in txt(r)
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
    agent, fb = make(tmp_path)                    # prompt mode
    fb.foreground_process = "code.exe"
    r = await agent.type_text("x", expect_app="notepad", screenshot_after=False)
    assert r.is_error and not fb.confirm_calls, "the approval dialog must not be shown first when the window is wrong"
    fb.foreground_process = None                  # the foreground window is unknown
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
    """Several terminals are all windowsterminal.exe (the Agent window, the tunnel window, mine); the process name cannot tell them apart, so checking by handle must work."""
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "windowsterminal.exe"
    mine = fb.foreground_window().handle
    ok = await agent.type_text("x", expect_app="windowsterminal", expect_handle=mine, screenshot_after=False)
    assert not ok.is_error
    r = await agent.type_text("x", expect_app="windowsterminal", expect_handle=mine + 1, screenshot_after=False)
    assert r.is_error and f"handle={mine + 1}" in txt(r) and "nothing was sent" in txt(r)
    r = await agent.press_key("enter", expect_handle=mine + 1, screenshot_after=False)     # giving only the handle is fine too
    assert r.is_error
    assert len(fb.actions) == 1


async def test_expect_handle_zero_is_still_a_real_constraint_and_none_is_not(tmp_path):
    agent, fb = make(tmp_path, approval_mode="all")
    fb.foreground_process = "x.exe"
    assert (await agent.type_text("x", expect_handle=0, screenshot_after=False)).is_error     # 0 is not None
    assert not (await agent.type_text("x", expect_handle=None, screenshot_after=False)).is_error


# ---- approval dialog subprocess: on a real machine the Agent crashed from tkinter's Tcl_AsyncDelete in a thread, so process isolation is mandatory ----
import ast
import sys as _sys
import textwrap as _tw


def test_tkinter_is_never_imported_in_the_agent_process_code_paths():
    """tkinter may only appear in the subprocess entry run_confirm_dialog; importing tkinter anywhere in the Agent process could reproduce the crash."""
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
    assert not top, "tkinter must not be imported at module top level"
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
        assert a["title"] == "\u6807\u9898 ⚠" and a["message"] == "\u884c1\\n\u884c2" and a["timeout_s"] == 5
        print("noise before"); print("WINPC_CONFIRM:YES")
    """)
    assert yes._confirm_tk("\u6807\u9898 ⚠", "\u884c1\n\u884c2", 5) is True
    no = fake_dialog(monkeypatch, 'print("WINPC_CONFIRM:NO")')
    assert no._confirm_tk("t", "m", 5) is False


@pytest.mark.parametrize("code", ["import sys; sys.exit(3)",                        # crash, no output
                                  'print("garbage")',                                # garbled output
                                  "import os; os._exit(134)",                        # an abort like Tcl_AsyncDelete
                                  ""])                                               # empty
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
    b._confirm_messagebox = lambda t, m, s: False        # fall back to the system message box
    assert b.confirm("t", "m", 2) is False


import io as _io


class _FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = _io.BytesIO(data)

    def read(self):                                 # the old sys.stdin.read(): decoded with the system code page, deliberately producing garbled text
        return self.buffer.getvalue().decode("gbk", errors="replace")


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_dialog_entry_decodes_utf8_stdin_with_or_without_bom(monkeypatch, capsys, bom):
    """Found by the self-test on a real machine: the BOM from a PowerShell pipe makes json parsing fail, and sys.stdin decoding as GBK by default garbles non-ASCII text."""
    import json
    payload = json.dumps({"title": "WinPC Remote - \u5e94\u7528\u6388\u6743", "message": "\u8bf7\u6c42\u64cd\u4f5c\u5e94\u7528\uff1asearchhost.exe\n\u52a8\u4f5c\uff1a\u8f93\u5165 3 \u4e2a\u5b57\u7b26",
                          "timeout_s": 5}, ensure_ascii=False).encode("utf-8")
    seen = {}
    monkeypatch.setattr(backends, "run_confirm_dialog", lambda t, m, s: seen.update(t=t, m=m, s=s) or True)
    monkeypatch.setattr(backends.sys, "stdin", _FakeStdin(bom + payload))
    assert backends._dialog_main() == 0
    assert seen == {"t": "WinPC Remote - \u5e94\u7528\u6388\u6743", "m": "\u8bf7\u6c42\u64cd\u4f5c\u5e94\u7528\uff1asearchhost.exe\n\u52a8\u4f5c\uff1a\u8f93\u5165 3 \u4e2a\u5b57\u7b26", "s": 5.0}
    assert "WINPC_CONFIRM:YES" in capsys.readouterr().out
