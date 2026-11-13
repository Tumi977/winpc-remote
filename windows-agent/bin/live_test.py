"""Live smoke test: calls all tools of the real Windows Agent from the server through the tunnel.

Only operates a Notepad that it opened itself, never saves a file, and closes Notepad at the end. All mouse actions stay inside the Notepad window.
Usage:
    WINPC_TOKEN=... python live_test.py [url]
"""
import asyncio
import os
import re
import sys

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"
TOKEN = os.environ["WINPC_TOKEN"]
results: list[tuple[str, bool, str]] = []


class Abort(Exception):
    pass


def text_of(r) -> str:
    return " ".join(c.text for c in r.content if c.type == "text")


def has_image(r) -> bool:
    return any(c.type == "image" and c.mime_type == "image/jpeg" for c in r.content)


def foreground(r) -> str:
    m = re.search(r"Foreground window: (.*?)\.(?: |$)", text_of(r))
    return m.group(1) if m else ""


async def step(name, coro, check=lambda r: True):
    try:
        r = await coro
        ok = (not r.is_error) and check(r)
        results.append((name, ok, text_of(r)[:110].replace("\n", " ")))
        return r
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"exception: {e!r}"[:110]))
        return None


def notepad_window(r):
    """Pick the topmost Notepad from list_windows: (handle, screenshot coords x, y, w, h)."""
    for line in text_of(r).splitlines():
        if "notepad.exe" in line.lower():
            h = re.search(r"handle=(\d+)", line)
            g = re.search(r"screenshot coords ≈\((-?\d+),(-?\d+)\) (\d+)x(\d+)", line)
            if h and g:
                return (int(h.group(1)), *map(int, g.groups()))
    return None


def require(cond, msg):
    if not cond:
        results.append(("safety check", False, msg))
        raise Abort(msg)


async def main():
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=90, trust_env=False)
    async with Client(streamable_http_client(URL, http_client=http)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        want = {"screenshot", "zoom", "click", "move_mouse", "drag", "scroll", "type_text", "press_key",
                "wait", "list_windows", "focus_window", "get_status"}
        results.append(("list_tools", want <= tools, f"missing: {sorted(want - tools)}" if want - tools else "all 12 tools present"))

        await step("get_status", c.call_tool("get_status", {}), lambda r: "agent_version" in text_of(r))
        # Safety pre-check: the new Notepad restores the tabs of the last session, and this test types into the foreground Notepad and closes it with "Don't save".
        # If the user already has a Notepad window, unsaved content could be lost, so the test must abort first.
        pre = await step("list_windows (pre-check)", c.call_tool("list_windows", {}))
        require(pre is not None and "notepad.exe" not in text_of(pre).lower(),
                "An open Notepad window was detected. This test types text into the foreground Notepad and closes it with \"Don't save\", which could lose your unsaved content. "
                "Save and close all Notepad windows manually, then run this test again.")
        await step("screenshot", c.call_tool("screenshot", {}), has_image)
        await step("list_windows", c.call_tool("list_windows", {}))

        # Win+R opens "Run"; type only after confirming that the dialog really is in the foreground, so text never goes into another window
        await step("press_key win+r", c.call_tool("press_key", {"keys": "win+r", "screenshot_after": False}))
        await asyncio.sleep(1.5)
        s = await step("screenshot (Run dialog)", c.call_tool("screenshot", {}), has_image)
        require(s and re.search(r"Run|\u8fd0\u884c", foreground(s)), "The Run dialog did not become the foreground window; aborted: " + (foreground(s) if s else ""))
        await step("type_text 'notepad'", c.call_tool("type_text", {"text": "notepad", "press_enter": True,
                                                                     "screenshot_after": False}))
        await asyncio.sleep(3)

        s = await step("screenshot (Notepad)", c.call_tool("screenshot", {}), has_image)
        require(s and "notepad.exe" in foreground(s).lower(), "Notepad did not become the foreground window; aborted: " + (foreground(s) if s else ""))

        await step("type_text Chinese + English + emoji", c.call_tool(
            "type_text", {"text": "WinPC live test \u4f60\u597d\U0001f642\n\u7b2c\u4e8c\u884c\n\u7b2c\u4e09\u884c", "screenshot_after": True}), has_image)
        await step("press_key ctrl+a", c.call_tool("press_key", {"keys": "ctrl+a", "screenshot_after": False}))
        await step("press_key end x2", c.call_tool("press_key", {"keys": "end", "repeat": 2,
                                                                  "screenshot_after": False}))

        wl = await step("list_windows (find Notepad)", c.call_tool("list_windows", {}))
        win = notepad_window(wl) if wl else None
        require(win, "No Notepad window found in list_windows")
        handle, wx, wy, ww, wh = win
        cx, cy = wx + ww // 2, wy + wh // 2
        require(ww >= 40 and wh >= 40 and cx > 5 and cy > 5, f"The Notepad window is too small or off screen: {win}")

        await step("zoom (Notepad area)", c.call_tool("zoom", {"x1": wx, "y1": wy, "x2": wx + ww, "y2": wy + wh}), has_image)
        # all mouse actions below stay inside the Notepad window
        await step("move_mouse", c.call_tool("move_mouse", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("click", c.call_tool("click", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("scroll", c.call_tool("scroll", {"x": cx, "y": cy, "direction": "down", "amount": 3,
                                                    "screenshot_after": False}))
        await step("drag (select text)", c.call_tool("drag", {"x1": cx - ww // 6, "y1": cy, "x2": cx + ww // 6, "y2": cy,
                                                        "screenshot_after": False}))
        await step("wait(1s)", c.call_tool("wait", {"seconds": 1}), has_image)
        await step("focus_window", c.call_tool("focus_window", {"handle": handle}))

        # Cleanup: close Notepad only if it is still the foreground window, answering "Don't save" to the save prompt
        s = await step("Notepad still in foreground", c.call_tool("screenshot", {}), lambda r: "notepad.exe" in foreground(r).lower())
        require(s and "notepad.exe" in foreground(s).lower(), "The foreground is no longer Notepad; alt+f4 not sent, please close Notepad manually")
        await step("press_key alt+f4", c.call_tool("press_key", {"keys": "alt+f4", "screenshot_after": False}))
        await asyncio.sleep(1)
        s = await step("screenshot (save prompt)", c.call_tool("screenshot", {}), has_image)
        if s and re.search(r"Notepad|\u8bb0\u4e8b\u672c", foreground(s)):
            await step("press_key n (Don't save)", c.call_tool("press_key", {"keys": "n", "screenshot_after": False}))
            await asyncio.sleep(1)
        await step("screenshot (wrap-up)", c.call_tool("screenshot", {}), has_image)


def report():
    width = max(len(n) for n, *_ in results)
    for n, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(width)}  {info}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f", failed: {failed}" if failed else ""))
    return 1 if failed else 0


def find_abort(e: BaseException):
    """The MCP client's TaskGroup wraps Abort in an exception group (Python 3.10 has no except*), so search recursively."""
    if isinstance(e, Abort):
        return e
    for sub in getattr(e, "exceptions", ()):
        found = find_abort(sub)
        if found:
            return found
    return None


try:
    asyncio.run(main())
except BaseException as e:  # noqa: BLE001
    abort = find_abort(e)
    if abort is None:
        raise
    print(f"Aborted: {abort}")
sys.exit(report())
