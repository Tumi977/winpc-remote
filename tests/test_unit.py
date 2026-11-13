"""Unit tests: coordinate conversion, key parsing, authorization policy, pause, audit. All use FakeBackend; no Windows needed."""
import json

import pytest

from backends import FakeBackend, Monitor
from winpc_agent import (AgentError, Config, ScreenState, WinPCAgent, compute_scale, parse_keys,
                         parse_modifiers)


def make_agent(tmp_path, backend=None, **cfg):
    backend = backend or FakeBackend()
    config = Config(log_dir=str(tmp_path / "logs"), settle_ms=0, **cfg)
    return WinPCAgent(config, backend), backend


def text_of(result):
    return result.content[0].text


# ----------------------------- coordinate conversion ----------------------------- #
def test_scale_2560x1440():
    s = compute_scale(2560, 1440, 1280)
    assert s == 0.5
    st = ScreenState(1, Monitor(0, 0, 2560, 1440), s, 1280, 720)
    assert st.to_physical(640, 360) == (1280, 720)


def test_scale_never_upscales():
    assert compute_scale(1024, 768, 1280) == 1


def test_multi_monitor_offset():
    mon = Monitor(-1920, 0, 1920, 1080)
    s = compute_scale(1920, 1080, 1280)
    st = ScreenState(2, mon, s, 1280, 720)
    assert st.to_physical(0, 0) == (-1920, 0)
    assert st.to_physical(640, 360) == (-1920 + 960, 540)


@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (1280, 0), (0, 720), (5000, 5000)])
def test_out_of_bounds_raises(x, y):
    st = ScreenState(1, Monitor(0, 0, 2560, 1440), 0.5, 1280, 720)
    with pytest.raises(AgentError):
        st.to_physical(x, y)


async def test_action_before_screenshot_errors(tmp_path):
    agent, fb = make_agent(tmp_path, allowed_apps=["notepad.exe"])
    r = await agent.click(10, 10)
    assert r.is_error and "screenshot" in text_of(r)
    assert fb.actions == []


async def test_click_uses_last_screenshot_monitor(tmp_path):
    fb = FakeBackend([Monitor(0, 0, 2560, 1440), Monitor(-1920, 0, 1920, 1080)])
    agent, _ = make_agent(tmp_path, fb)
    await agent.screenshot(2)
    r = await agent.click(640, 360, screenshot_after=False)
    assert not r.is_error, text_of(r)
    assert (fb.actions[-1]["x"], fb.actions[-1]["y"]) == (-1920 + 960, 540)
    # later screenshots without monitor reuse the last monitor
    r = await agent.screenshot()
    assert "monitor 2" in text_of(r)


async def test_invalid_monitor(tmp_path):
    agent, _ = make_agent(tmp_path)
    r = await agent.screenshot(3)
    assert r.is_error


# ----------------------------- key parsing ----------------------------- #
def test_parse_keys():
    assert parse_keys("Ctrl+Shift+Esc") == ["ctrl", "shift", "escape"]
    assert parse_keys("return") == ["enter"]
    assert parse_keys("cmd+c") == ["win", "c"]
    assert parse_keys("Page_Down") == ["pagedown"]
    assert parse_keys("BackSpace") == ["backspace"]
    assert parse_keys("Control_L+a") == ["ctrl", "a"]
    assert parse_keys("super") == ["win"]
    assert parse_keys("F5") == ["f5"]
    assert parse_keys("ctrl++") == ["ctrl", "+"]


def test_parse_keys_invalid():
    for bad in ["", "ctrl+nonsense", "+"[:0]]:
        with pytest.raises(AgentError):
            parse_keys(bad)


def test_parse_modifiers():
    assert parse_modifiers("ctrl+shift") == ["ctrl", "shift"]
    assert parse_modifiers(None) == []
    with pytest.raises(AgentError):
        parse_modifiers("ctrl+a")


# ----------------------------- authorization policy ----------------------------- #
async def test_blocked_app_denied_even_if_allowlisted(tmp_path):
    agent, fb = make_agent(tmp_path, allowed_apps=["keepass.exe"], approval_mode="all")
    fb.foreground_process = "KeePass.exe"
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert r.is_error and "block-list" in text_of(r)
    assert fb.actions == [] and fb.confirm_calls == []


async def test_allowlisted_app_passes_without_prompt(tmp_path):
    agent, fb = make_agent(tmp_path)  # default allowlist: notepad.exe
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert not r.is_error and fb.confirm_calls == []


async def test_prompt_approved_then_remembered(tmp_path):
    agent, fb = make_agent(tmp_path)
    fb.foreground_process = "chrome.exe"
    fb.confirm_result = True
    await agent.screenshot()
    assert not (await agent.click(10, 10, screenshot_after=False)).is_error
    assert len(fb.confirm_calls) == 1 and "chrome.exe" in fb.confirm_calls[0][1]
    assert not (await agent.press_key("a", screenshot_after=False)).is_error
    assert len(fb.confirm_calls) == 1  # not asked a second time
    assert "chrome.exe" in agent.session_approved


async def test_prompt_denied(tmp_path):
    agent, fb = make_agent(tmp_path)
    fb.foreground_process = "chrome.exe"
    fb.confirm_result = False
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert r.is_error and "denied" in text_of(r)
    assert fb.actions == [] and agent.session_approved == set()
    r = await agent.type_text("hi", screenshot_after=False)  # asks again
    assert r.is_error and len(fb.confirm_calls) == 2


async def test_allowlist_mode_rejects_without_prompt(tmp_path):
    agent, fb = make_agent(tmp_path, approval_mode="allowlist")
    fb.foreground_process = "chrome.exe"
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert r.is_error and "allowlist" in text_of(r)
    assert fb.confirm_calls == [] and fb.actions == []


async def test_all_mode_allows_everything_but_blocked(tmp_path):
    agent, fb = make_agent(tmp_path, approval_mode="all")
    fb.foreground_process = "whatever.exe"
    await agent.screenshot()
    assert not (await agent.click(10, 10, screenshot_after=False)).is_error
    assert fb.confirm_calls == []


async def test_pointer_actions_check_window_under_pointer(tmp_path):
    agent, fb = make_agent(tmp_path, approval_mode="allowlist")
    fb.foreground_process = "notepad.exe"   # the foreground is Notepad
    fb.point_process = "1password.exe"      # but the app under the coordinates is on the block-list
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert r.is_error and "block-list" in text_of(r)


async def test_elevated_window_gives_uipi_error(tmp_path):
    agent, fb = make_agent(tmp_path, approval_mode="all")
    fb.elevated_processes = {"notepad.exe"}
    await agent.screenshot()
    r = await agent.click(10, 10, screenshot_after=False)
    assert r.is_error and "administrator" in text_of(r)


async def test_focus_window_is_authorized(tmp_path):
    agent, fb = make_agent(tmp_path)  # prompt mode, chrome is not in the allowlist
    fb.confirm_result = False
    r = await agent.focus_window(1002)
    assert r.is_error and fb.confirm_calls
    fb.confirm_result = True
    r = await agent.focus_window(1002, screenshot_after=False)
    assert not r.is_error and fb.foreground_process == "chrome.exe"
    assert (await agent.focus_window(9999)).is_error


# ----------------------------- pause ----------------------------- #
async def test_failsafe_pauses_input_tools_but_not_screenshot(tmp_path):
    agent, fb = make_agent(tmp_path)
    await agent.screenshot()
    fb.failsafe_armed = True
    r = await agent.click(10, 10)
    assert r.is_error and "Paused" in text_of(r) and agent.paused
    n = len(fb.actions)
    for call in (agent.click(1, 1), agent.type_text("x"), agent.press_key("a"),
                 agent.scroll(1, 1), agent.drag(1, 1, 2, 2), agent.move_mouse(1, 1)):
        r = await call
        assert r.is_error and "Paused" in text_of(r)
    assert len(fb.actions) == n
    assert not (await agent.screenshot()).is_error
    assert not (await agent.get_status()).is_error
    assert "Paused" in text_of(await agent.get_status())


# ----------------------------- audit ----------------------------- #
async def test_audit_does_not_log_full_text(tmp_path):
    agent, fb = make_agent(tmp_path)
    secret = "\u8fd9\u662f\u4e00\u6bb5\u5f88\u957f\u5f88\u957f\u7684\u6587\u672c\uff0c" + "X" * 100 + "SECRET_TAIL"
    await agent.type_text(secret, screenshot_after=False)
    lines = [json.loads(l) for l in (tmp_path / "logs" / "actions.jsonl").read_text("utf-8").splitlines()]
    entry = lines[-1]
    assert entry["tool"] == "type_text" and entry["process"] == "notepad.exe"
    assert entry["params"]["text_len"] == len(secret)
    assert entry["params"]["text_head"] == secret[:20]
    assert "text" not in entry["params"]
    assert "SECRET_TAIL" not in json.dumps(entry, ensure_ascii=False)
    assert fb.actions[-1]["text"] == secret  # but the real input is complete


async def test_audit_saves_screenshots_when_enabled(tmp_path):
    agent, _ = make_agent(tmp_path, save_screenshots=True)
    await agent.screenshot()
    assert list((tmp_path / "logs" / "screenshots").glob("*.jpg"))


# ----------------------------- other ----------------------------- #
async def test_black_screen_hint(tmp_path):
    agent, fb = make_agent(tmp_path)
    fb.black = True
    assert "locked" in text_of(await agent.screenshot())
    assert "locked" in text_of(await agent.get_status())


async def test_zoom_keeps_coordinate_system(tmp_path):
    agent, _ = make_agent(tmp_path)
    await agent.screenshot()
    before = agent.state
    r = await agent.zoom(100, 100, 300, 200)
    assert not r.is_error and agent.state is before
    assert (await agent.zoom(300, 100, 100, 200)).is_error
    assert (await agent.zoom(0, 0, 5000, 200)).is_error


async def test_wait_bounds(tmp_path):
    agent, _ = make_agent(tmp_path)
    assert (await agent.wait(31)).is_error
    assert (await agent.wait(0)).is_error
    assert not (await agent.wait(0.01)).is_error


def test_config_normalizes_apps_and_rejects_bad_mode():
    c = Config(allowed_apps=["Notepad"], blocked_apps=["KeePass.EXE"])
    assert c.allowed_apps == ["notepad.exe"] and c.blocked_apps == ["keepass.exe"]
    with pytest.raises(ValueError):
        Config(approval_mode="yolo")


def test_load_config_generates_token_once(tmp_path, monkeypatch, capsys):
    from winpc_agent import load_config
    monkeypatch.delenv("WINPC_TOKEN", raising=False)
    p = tmp_path / "config.json"
    c1 = load_config(p)
    assert len(c1.token) >= 32 and c1.token in capsys.readouterr().out
    assert json.loads(p.read_text("utf-8"))["token"] == c1.token
    c2 = load_config(p)
    assert c2.token == c1.token and c1.token not in capsys.readouterr().out
    monkeypatch.setenv("WINPC_TOKEN", "from-env")
    assert load_config(p).token == "from-env"
