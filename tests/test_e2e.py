"""End to end: start a real HTTP MCP server on a random port (FakeBackend) and call all tools with an MCP client."""
import base64
import io
import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from backends import FakeBackend
from winpc_agent import Config, create_app

TOKEN = "test-token-" + "x" * 32
EXPECTED_TOOLS = {"screenshot", "zoom", "click", "move_mouse", "drag", "scroll", "type_text", "press_key",
                  "wait", "list_windows", "focus_window", "get_status",
                  "read_file", "list_files", "write_file", "edit_file", "stat_file", "get_file", "put_file"}


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config(port=port, token=TOKEN, log_dir=str(tmp_path / "logs"), settle_ms=0)
    backend = FakeBackend()
    app, agent = create_app(cfg, backend)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server start-up timed out"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", backend, agent
    srv.should_exit = True
    t.join(timeout=10)


def mcp_client(url, token=TOKEN):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return Client(streamable_http_client(url, http_client=http))


INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"}}}
HDRS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


# ----------------------------- auth / Host ----------------------------- #
def test_no_token_401(server):
    url, *_ = server
    assert httpx.post(url, json=INIT_BODY, headers=HDRS).status_code == 401


@pytest.mark.parametrize("auth", ["Bearer wrong", "Bearer", "Basic " + TOKEN, TOKEN])
def test_bad_token_401(server, auth):
    url, *_ = server
    r = httpx.post(url, json=INIT_BODY, headers={**HDRS, "Authorization": auth})
    assert r.status_code == 401


def test_wrong_host_421(server):
    url, *_ = server
    r = httpx.post(url, json=INIT_BODY, headers={**HDRS, "Authorization": f"Bearer {TOKEN}", "Host": "evil.com"})
    assert r.status_code == 421


def test_good_token_and_host_accepted(server):
    url, *_ = server
    r = httpx.post(url, json=INIT_BODY, headers={**HDRS, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


# ----------------------------- tools ----------------------------- #
async def test_list_tools(server):
    url, *_ = server
    async with mcp_client(url) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert EXPECTED_TOOLS <= names


async def test_screenshot_content(server):
    url, *_ = server
    async with mcp_client(url) as c:
        r = await c.call_tool("screenshot", {})
    assert not r.is_error
    assert [x.type for x in r.content] == ["text", "image"]
    img = r.content[1]
    assert img.mime_type == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(img.data)))
    assert decoded.format == "JPEG" and decoded.size == (1280, 720)
    assert r.structured_content is None  # must never carry structuredContent
    assert "1280x720" in r.content[0].text and "2560x1440" in r.content[0].text


async def test_raw_response_has_no_structured_content(server):
    """Look at the JSON on the wire and confirm there is no structuredContent field (Claude Code would drop images because of it)."""
    url, *_ = server
    h = {**HDRS, "Authorization": f"Bearer {TOKEN}"}
    with httpx.Client() as http:
        r = http.post(url, json=INIT_BODY, headers=h)
        sid = r.headers.get("mcp-session-id")
        h2 = {**h, **({"mcp-session-id": sid} if sid else {}), "mcp-protocol-version": "2025-03-26"}
        http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
        r = http.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "screenshot", "arguments": {}}}, headers=h2)
    assert "structuredContent" not in r.text and "image/jpeg" in r.text


async def test_click_type_press(server):
    url, fb, agent = server
    async with mcp_client(url) as c:
        await c.call_tool("screenshot", {})
        r = await c.call_tool("click", {"x": 640, "y": 360})
        assert not r.is_error and [x.type for x in r.content] == ["text", "image"]  # a screenshot is attached after the action
        assert fb.actions[-1] == {"op": "click", "x": 1280, "y": 720, "button": "left", "clicks": 1,
                                  "modifiers": []}

        r = await c.call_tool("click", {"x": 100, "y": 100, "button": "right", "clicks": 2,
                                        "modifiers": "ctrl+shift", "screenshot_after": False})
        assert [x.type for x in r.content] == ["text"]
        assert fb.actions[-1] == {"op": "click", "x": 200, "y": 200, "button": "right", "clicks": 2,
                                  "modifiers": ["ctrl", "shift"]}

        await c.call_tool("type_text", {"text": "\u4f60\u597d🙂", "press_enter": True, "screenshot_after": False})
        assert fb.actions[-2]["op"] == "type_text" and fb.actions[-2]["text"] == "\u4f60\u597d🙂"
        assert fb.actions[-1] == {"op": "press_keys", "keys": ["enter"], "repeat": 1}

        await c.call_tool("press_key", {"keys": "ctrl+s", "screenshot_after": False})
        assert fb.actions[-1] == {"op": "press_keys", "keys": ["ctrl", "s"], "repeat": 1}


async def test_remaining_tools(server):
    url, fb, agent = server
    async with mcp_client(url) as c:
        await c.call_tool("screenshot", {})
        await c.call_tool("move_mouse", {"x": 10, "y": 20, "screenshot_after": False})
        assert fb.actions[-1] == {"op": "move_mouse", "x": 20, "y": 40}
        await c.call_tool("drag", {"x1": 10, "y1": 10, "x2": 110, "y2": 60, "screenshot_after": False})
        assert fb.actions[-1] == {"op": "drag", "x1": 20, "y1": 20, "x2": 220, "y2": 120, "button": "left"}
        await c.call_tool("scroll", {"x": 640, "y": 360, "direction": "down", "amount": 5,
                                     "screenshot_after": False})
        assert fb.actions[-1] == {"op": "scroll", "x": 1280, "y": 720, "direction": "down", "amount": 5}

        z = await c.call_tool("zoom", {"x1": 0, "y1": 0, "x2": 100, "y2": 50})
        assert z.content[1].mime_type == "image/jpeg"
        assert Image.open(io.BytesIO(base64.b64decode(z.content[1].data))).size == (200, 100)  # native resolution

        w = await c.call_tool("wait", {"seconds": 0.05})
        assert [x.type for x in w.content] == ["text", "image"]

        lw = await c.call_tool("list_windows", {})
        assert "notepad.exe" in lw.content[0].text and "chrome.exe" in lw.content[0].text

        st = await c.call_tool("get_status", {})
        assert '"paused": false' in st.content[0].text

        fb.confirm_result = True
        f = await c.call_tool("focus_window", {"handle": 1002, "screenshot_after": False})
        assert not f.is_error and fb.actions[-1] == {"op": "focus_window", "handle": 1002}


async def test_errors_and_pause_over_http(server):
    url, fb, agent = server
    async with mcp_client(url) as c:
        r = await c.call_tool("click", {"x": 1, "y": 1})
        assert r.is_error and "screenshot" in r.content[0].text
        await c.call_tool("screenshot", {})
        r = await c.call_tool("click", {"x": 99999, "y": 1})
        assert r.is_error and "outside" in r.content[0].text
        fb.failsafe_armed = True
        r = await c.call_tool("click", {"x": 1, "y": 1})
        assert r.is_error and "Paused" in r.content[0].text
        r = await c.call_tool("type_text", {"text": "x"})
        assert r.is_error and "Paused" in r.content[0].text
        r = await c.call_tool("screenshot", {})
        assert not r.is_error


async def test_file_tools_over_http(server, tmp_path):
    """File tools over real HTTP: writes need confirmation, reads inside roots do not, an edit can be read back."""
    url, fb, agent = server
    root = tmp_path / "docs"
    root.mkdir()
    agent.policy.roots.append(str(root.resolve()))
    f = str(root / "note.txt")
    async with mcp_client(url) as c:
        r = await c.call_tool("write_file", {"path": f, "content": "\u4f60\u597d🙂\n\u7b2c\u4e8c\u884c\n"})
        assert not r.is_error and [x.type for x in r.content] == ["text"]  # file tools attach no screenshot
        assert len(fb.confirm_calls) == 1
        r = await c.call_tool("read_file", {"path": f})
        assert "\u4f60\u597d🙂" in r.content[0].text and r.structured_content is None
        r = await c.call_tool("edit_file", {"path": f, "old_string": "\u7b2c\u4e8c\u884c", "new_string": "\u7b2c\u0020\u0032\u0020\u884c"})
        assert not r.is_error
        assert "\u7b2c\u0020\u0032\u0020\u884c" in (await c.call_tool("read_file", {"path": f})).content[0].text
        assert "note.txt" in (await c.call_tool("list_files", {"path": str(root)})).content[0].text
        r = await c.call_tool("read_file", {"path": str(root / ".ssh" / "id_rsa")})
        assert r.is_error
