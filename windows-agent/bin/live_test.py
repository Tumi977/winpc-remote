"""实机冒烟测试：在服务器上经隧道调用真实 Windows Agent 的全部工具。

只操作自己打开的记事本，不保存文件，结束时关闭记事本。所有鼠标动作都限制在记事本窗口内部。
用法：
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
    m = re.search(r"前台窗口: (.*?)。", text_of(r))
    return m.group(1) if m else ""


async def step(name, coro, check=lambda r: True):
    try:
        r = await coro
        ok = (not r.is_error) and check(r)
        results.append((name, ok, text_of(r)[:110].replace("\n", " ")))
        return r
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"异常: {e!r}"[:110]))
        return None


def notepad_window(r):
    """从 list_windows 里取最上层的记事本：(handle, 截图坐标 x, y, w, h)。"""
    for line in text_of(r).splitlines():
        if "notepad.exe" in line.lower():
            h = re.search(r"handle=(\d+)", line)
            g = re.search(r"截图坐标 ≈\((-?\d+),(-?\d+)\) (\d+)x(\d+)", line)
            if h and g:
                return (int(h.group(1)), *map(int, g.groups()))
    return None


def require(cond, msg):
    if not cond:
        results.append(("安全检查", False, msg))
        raise Abort(msg)


async def main():
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=90, trust_env=False)
    async with Client(streamable_http_client(URL, http_client=http)) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        want = {"screenshot", "zoom", "click", "move_mouse", "drag", "scroll", "type_text", "press_key",
                "wait", "list_windows", "focus_window", "get_status"}
        results.append(("list_tools", want <= tools, f"缺少: {sorted(want - tools)}" if want - tools else "12 个工具齐全"))

        await step("get_status", c.call_tool("get_status", {}), lambda r: "agent_version" in text_of(r))
        # 安全前置检查：新版记事本会恢复上次的标签页，本测试会向前台记事本输入并以“不保存”关闭它。
        # 如果用户已有记事本窗口，可能丢失其中未保存的内容，所以必须先中止。
        pre = await step("list_windows(前置检查)", c.call_tool("list_windows", {}))
        require(pre is not None and "notepad.exe" not in text_of(pre).lower(),
                "检测到已打开的记事本窗口。本测试会向前台记事本输入文字并以“不保存”关闭它，可能丢掉你未保存的内容。"
                "请先手动保存并关闭所有记事本窗口，再运行本测试。")
        await step("screenshot", c.call_tool("screenshot", {}), has_image)
        await step("list_windows", c.call_tool("list_windows", {}))

        # Win+R 打开“运行”，确认对话框确实在前台后才输入，避免把文字打进别的窗口
        await step("press_key win+r", c.call_tool("press_key", {"keys": "win+r", "screenshot_after": False}))
        await asyncio.sleep(1.5)
        s = await step("screenshot(运行框)", c.call_tool("screenshot", {}), has_image)
        require(s and re.search(r"运行|Run", foreground(s)), "“运行”对话框没有成为前台窗口，已中止: " + (foreground(s) if s else ""))
        await step("type_text 'notepad'", c.call_tool("type_text", {"text": "notepad", "press_enter": True,
                                                                     "screenshot_after": False}))
        await asyncio.sleep(3)

        s = await step("screenshot(记事本)", c.call_tool("screenshot", {}), has_image)
        require(s and "notepad.exe" in foreground(s).lower(), "记事本没有成为前台窗口，已中止: " + (foreground(s) if s else ""))

        await step("type_text 中英文+emoji", c.call_tool(
            "type_text", {"text": "WinPC live test 你好🙂\n第二行\n第三行", "screenshot_after": True}), has_image)
        await step("press_key ctrl+a", c.call_tool("press_key", {"keys": "ctrl+a", "screenshot_after": False}))
        await step("press_key end x2", c.call_tool("press_key", {"keys": "end", "repeat": 2,
                                                                  "screenshot_after": False}))

        wl = await step("list_windows(找记事本)", c.call_tool("list_windows", {}))
        win = notepad_window(wl) if wl else None
        require(win, "list_windows 里找不到记事本窗口")
        handle, wx, wy, ww, wh = win
        cx, cy = wx + ww // 2, wy + wh // 2
        require(ww >= 40 and wh >= 40 and cx > 5 and cy > 5, f"记事本窗口太小或在屏外: {win}")

        await step("zoom(记事本区域)", c.call_tool("zoom", {"x1": wx, "y1": wy, "x2": wx + ww, "y2": wy + wh}), has_image)
        # 以下所有鼠标动作都落在记事本窗口内部
        await step("move_mouse", c.call_tool("move_mouse", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("click", c.call_tool("click", {"x": cx, "y": cy, "screenshot_after": False}))
        await step("scroll", c.call_tool("scroll", {"x": cx, "y": cy, "direction": "down", "amount": 3,
                                                    "screenshot_after": False}))
        await step("drag(选文本)", c.call_tool("drag", {"x1": cx - ww // 6, "y1": cy, "x2": cx + ww // 6, "y2": cy,
                                                        "screenshot_after": False}))
        await step("wait(1s)", c.call_tool("wait", {"seconds": 1}), has_image)
        await step("focus_window", c.call_tool("focus_window", {"handle": handle}))

        # 清理：仅在前台仍是记事本时关闭它，对“是否保存”选“不保存”
        s = await step("前台仍是记事本", c.call_tool("screenshot", {}), lambda r: "notepad.exe" in foreground(r).lower())
        require(s and "notepad.exe" in foreground(s).lower(), "前台已不是记事本，不发送 alt+f4，请手动关闭记事本")
        await step("press_key alt+f4", c.call_tool("press_key", {"keys": "alt+f4", "screenshot_after": False}))
        await asyncio.sleep(1)
        s = await step("screenshot(保存提示)", c.call_tool("screenshot", {}), has_image)
        if s and re.search(r"记事本|Notepad", foreground(s)):
            await step("press_key n (不保存)", c.call_tool("press_key", {"keys": "n", "screenshot_after": False}))
            await asyncio.sleep(1)
        await step("screenshot(收尾)", c.call_tool("screenshot", {}), has_image)


def report():
    width = max(len(n) for n, *_ in results)
    for n, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n.ljust(width)}  {info}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"，失败: {failed}" if failed else ""))
    return 1 if failed else 0


def find_abort(e: BaseException):
    """MCP 客户端的 TaskGroup 会把 Abort 包进异常组（Python 3.10 没有 except*），所以递归查找。"""
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
    print(f"已中止: {abort}")
sys.exit(report())
