"""Backend 抽象：WindowsBackend（真实）/ FakeBackend（测试）。

本模块在 Linux 上必须可以被 import：所有 Windows 专用调用都放在
WindowsBackend 的构造函数/方法里，平台判断也在构造函数里。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageDraw


# --------------------------------------------------------------------------- #
# 数据类型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Monitor:
    left: int
    top: int
    width: int
    height: int


@dataclass
class WindowInfo:
    handle: int
    title: str
    process: str  # 小写进程名，如 "chrome.exe"；未知时为 ""
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    elevated: bool = False  # 目标进程是否以管理员身份运行


class FailSafeTriggered(Exception):
    """用户把鼠标甩到主屏左上角（紧急停止）。"""


class BackendError(Exception):
    """可直接展示给模型的后端错误（如 UIPI、激活窗口失败）。"""


# --------------------------------------------------------------------------- #
# 抽象
# --------------------------------------------------------------------------- #
class Backend(ABC):
    name = "abstract"

    @abstractmethod
    def monitors(self) -> list[Monitor]:
        """各显示器（顺序对应工具参数 monitor=1..n），物理像素。"""

    @abstractmethod
    def grab(self, monitor: Monitor) -> Image.Image:
        """以物理分辨率截取指定显示器，返回 RGB 图（不含鼠标指针）。"""

    @abstractmethod
    def cursor_pos(self) -> tuple[int, int]: ...

    @abstractmethod
    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """物理坐标下的顶层窗口（GetAncestor(GA_ROOT)）。"""

    @abstractmethod
    def foreground_window(self) -> WindowInfo | None: ...

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]: ...

    @abstractmethod
    def focus_window(self, handle: int) -> bool: ...

    @abstractmethod
    def click(self, x: int, y: int, button: str, clicks: int, modifiers: list[str]) -> None: ...

    @abstractmethod
    def move_mouse(self, x: int, y: int) -> None: ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str) -> None: ...

    @abstractmethod
    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def press_keys(self, keys: list[str], repeat: int) -> None:
        """按下组合键 keys（已规范化的名字，如 ["ctrl","shift","escape"]）。"""

    @abstractmethod
    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        """本机弹出置顶 是/否 框；超时或否 → False。"""

    def paste_text(self, text: str) -> str:
        """通过剪贴板粘贴文本（原子操作，不受目标程序按键处理卡顿影响）。返回一条说明（可为空）。"""
        raise BackendError("该后端不支持粘贴输入。")

    def is_self_elevated(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# FakeBackend（测试用）
# --------------------------------------------------------------------------- #
class FakeBackend(Backend):
    """内存画布，记录所有动作。

    actions: 动作记录，如 {"op": "click", "x": 1280, "y": 720, ...}
    foreground_process / point_process: 前台窗口 / 坐标下窗口的进程名
    confirm_result: 弹窗返回值；confirm_calls: 询问记录
    failsafe_armed: 置 True 则下一个输入动作抛 FailSafeTriggered
    black: 置 True 则截图全黑（模拟锁屏）
    """

    name = "fake"

    def __init__(self, monitors: list[Monitor] | None = None) -> None:
        self._monitors = monitors or [Monitor(0, 0, 2560, 1440)]
        self.actions: list[dict] = []
        self.foreground_process = "notepad.exe"
        self.foreground_title = "无标题 - 记事本"
        self.point_process: str | None = None  # None → 与前台相同
        self.windows: list[WindowInfo] = [
            WindowInfo(1001, "无标题 - 记事本", "notepad.exe", 100, 100, 800, 600),
            WindowInfo(1002, "Google Chrome", "chrome.exe", 200, 150, 1200, 800),
        ]
        self.confirm_result = True
        self.during_confirm = None        # 可选回调：在 confirm 期间执行
        self.confirm_calls: list[tuple[str, str, float]] = []
        self.failsafe_armed = False
        self.black = False
        self.elevated_processes: set[str] = set()
        self.self_elevated = False
        self._cursor = (10, 10)

    # -- 查询 --
    def monitors(self) -> list[Monitor]:
        return list(self._monitors)

    def grab(self, monitor: Monitor) -> Image.Image:
        if self.black:
            return Image.new("RGB", (monitor.width, monitor.height), (0, 0, 0))
        idx = self._monitors.index(monitor)
        palette = [(40, 90, 160), (160, 90, 40), (60, 140, 80)]
        img = Image.new("RGB", (monitor.width, monitor.height), palette[idx % len(palette)])
        d = ImageDraw.Draw(img)
        d.rectangle([monitor.width // 4, monitor.height // 4, monitor.width * 3 // 4, monitor.height * 3 // 4],
                    fill=(230, 230, 230))
        d.text((monitor.width // 4 + 20, monitor.height // 4 + 20), f"fake monitor {idx + 1}", fill=(0, 0, 0))
        return img

    def cursor_pos(self) -> tuple[int, int]:
        return self._cursor

    def _win(self, process: str | None, title: str) -> WindowInfo | None:
        if not process:
            return None
        return WindowInfo(sum(map(ord, process)), title, process, elevated=process in self.elevated_processes)

    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        return self._win(self.point_process or self.foreground_process, self.foreground_title)

    def foreground_window(self) -> WindowInfo | None:
        return self._win(self.foreground_process, self.foreground_title)

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def is_self_elevated(self) -> bool:
        return self.self_elevated

    # -- 动作 --
    def _act(self, **rec) -> None:
        if self.failsafe_armed:
            self.failsafe_armed = False
            raise FailSafeTriggered()
        self.actions.append(rec)
        if "x" in rec and "y" in rec:
            self._cursor = (rec["x"], rec["y"])

    def focus_window(self, handle: int) -> bool:
        for w in self.windows:
            if w.handle == handle:
                self.foreground_process, self.foreground_title = w.process, w.title
                self.actions.append({"op": "focus_window", "handle": handle})
                return True
        return False

    def click(self, x, y, button, clicks, modifiers):
        self._act(op="click", x=x, y=y, button=button, clicks=clicks, modifiers=list(modifiers))

    def move_mouse(self, x, y):
        self._act(op="move_mouse", x=x, y=y)

    def drag(self, x1, y1, x2, y2, button):
        self._act(op="drag", x1=x1, y1=y1, x2=x2, y2=y2, button=button)

    def scroll(self, x, y, direction, amount):
        self._act(op="scroll", x=x, y=y, direction=direction, amount=amount)

    def type_text(self, text):
        self._act(op="type_text", text=text)

    def press_keys(self, keys, repeat):
        self._act(op="press_keys", keys=list(keys), repeat=repeat)

    def paste_text(self, text):
        self._act(op="paste_text", text=text)
        return ""

    def confirm(self, title, message, timeout_s):
        self.confirm_calls.append((title, message, timeout_s))
        if self.during_confirm:
            self.during_confirm()      # 模拟确认框抢走焦点，导致前台窗口/鼠标下的窗口变化
        return self.confirm_result


# --------------------------------------------------------------------------- #
# WindowsBackend（真实）
# --------------------------------------------------------------------------- #
# SendInput 结构体（在 Linux 上定义也不会出错，只是不会被使用）
_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x0002, 0x0004
_MOUSEEVENTF = {
    "left": (0x0002, 0x0004),    # LEFTDOWN, LEFTUP
    "right": (0x0008, 0x0010),   # RIGHTDOWN, RIGHTUP
    "middle": (0x0020, 0x0040),  # MIDDLEDOWN, MIDDLEUP
}
_MOUSEEVENTF_WHEEL, _MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
_WHEEL_DELTA = 120
_VK_RETURN, _VK_TAB, _VK_MENU = 0x0D, 0x09, 0x12
_CF_TEXT, _CF_OEMTEXT, _CF_UNICODETEXT, _CF_LOCALE = 1, 7, 13, 16
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DIALOG_YES, _DIALOG_NO = "WINPC_CONFIRM:YES", "WINPC_CONFIRM:NO"


class WindowsBackend(Backend):
    name = "windows"

    def __init__(self, type_chunk_chars: int = 1, type_delay_ms: float = 5,
                 type_newline_delay_ms: float = 150, paste_restore_delay_ms: float = 600) -> None:
        # type_text 的节奏：每 type_chunk_chars 个字符一次 SendInput，批与批之间睡 type_delay_ms 毫秒。
        # 一次塞进几十个字符时，新版记事本（WinUI）会丢字或把整行变成同一个字符重复，所以默认逐字符 + 5ms。
        self.type_chunk_chars = max(1, int(type_chunk_chars))
        self.type_delay_s = max(0.0, float(type_delay_ms)) / 1000
        # 回车会让部分应用（如新版记事本）卡顿一下，期间堆积的按键会被合并成同一个字符，所以回车后多等一会
        self.type_newline_delay_s = max(0.0, float(type_newline_delay_ms)) / 1000
        # 粘贴后要等应用真正读走剪贴板，才能恢复原来的剪贴板内容
        self.paste_restore_delay_s = max(0.0, float(paste_restore_delay_ms)) / 1000
        if sys.platform != "win32":
            raise RuntimeError("WindowsBackend 只能在 Windows 上使用（可用 --backend fake 测试）")
        # DPI 感知：必须在任何截图/坐标操作之前（第 5 节第 1 条）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        import mss  # noqa: F401  延迟导入，避免 Linux 无显示环境下失败
        import pyautogui

        pyautogui.FAILSAFE = True  # 鼠标在主屏左上角 → FailSafeException
        pyautogui.PAUSE = 0
        self._pag = pyautogui
        self._mss = mss

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._setup_prototypes()
        self._pid_cache: dict[int, tuple[str, bool]] = {}

    # -- ctypes 原型：64 位下 HWND 必须声明，否则会被截断 --
    def _setup_prototypes(self) -> None:
        u, k, a = self.user32, self.kernel32, self.advapi32
        u.WindowFromPoint.argtypes = [wt.POINT]
        u.WindowFromPoint.restype = wt.HWND
        u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        u.GetAncestor.restype = wt.HWND
        u.GetForegroundWindow.restype = wt.HWND
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.IsIconic.argtypes = [wt.HWND]
        u.IsWindow.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        u.SendInput.restype = wt.UINT
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, _ULONG_PTR]
        # 剪贴板（指针/句柄一律用 c_void_p，64 位下不能被截断）
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [wt.HWND], wt.BOOL
        u.CloseClipboard.restype = wt.BOOL
        u.EmptyClipboard.restype = wt.BOOL
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [wt.UINT], ctypes.c_void_p
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [wt.UINT, ctypes.c_void_p], ctypes.c_void_p
        u.EnumClipboardFormats.argtypes, u.EnumClipboardFormats.restype = [wt.UINT], wt.UINT
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]
        u.CreateWindowExW.restype = wt.HWND
        u.DestroyWindow.argtypes, u.DestroyWindow.restype = [wt.HWND], wt.BOOL
        u.GetWindowLongPtrW.argtypes, u.GetWindowLongPtrW.restype = [wt.HWND, ctypes.c_int], ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
        u.SetWindowPos.restype = wt.BOOL
        u.MessageBeep.argtypes = [wt.UINT]
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [wt.UINT, ctypes.c_size_t], ctypes.c_void_p
        k.GlobalLock.argtypes, k.GlobalLock.restype = [ctypes.c_void_p], ctypes.c_void_p
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [ctypes.c_void_p], wt.BOOL
        k.GlobalFree.argtypes, k.GlobalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        k.GlobalSize.argtypes, k.GlobalSize.restype = [ctypes.c_void_p], ctypes.c_size_t
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        a.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        a.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD)]

    # -- 截图 --
    def monitors(self) -> list[Monitor]:
        with self._mss.mss() as sct:
            return [Monitor(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]

    def grab(self, monitor: Monitor) -> Image.Image:
        region = {"left": monitor.left, "top": monitor.top, "width": monitor.width, "height": monitor.height}
        with self._mss.mss() as sct:  # mss 实例不跨线程复用
            shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def cursor_pos(self) -> tuple[int, int]:
        pt = wt.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    # -- 窗口 / 进程 --
    def _process_info(self, pid: int) -> tuple[str, bool]:
        """返回 (小写进程名, 是否提权)。"""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        TOKEN_QUERY = 0x0008
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # 打不开多半是更高权限进程；名字未知，按提权处理
            return "", True
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(len(buf))
            name = ""
            if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            elevated = False
            token = wt.HANDLE()
            if self.advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
                try:
                    elev = wt.DWORD(0)
                    ret = wt.DWORD(0)
                    TokenElevation = 20
                    if self.advapi32.GetTokenInformation(token, TokenElevation, ctypes.byref(elev),
                                                         ctypes.sizeof(elev), ctypes.byref(ret)):
                        elevated = bool(elev.value)
                finally:
                    self.kernel32.CloseHandle(token)
            return name, elevated
        finally:
            self.kernel32.CloseHandle(h)

    def _window_info(self, hwnd) -> WindowInfo | None:
        if not hwnd:
            return None
        u = self.user32
        n = u.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, title_buf, n + 1)
        rect = wt.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name, elevated = self._process_info(pid.value)
        return WindowInfo(int(hwnd), title_buf.value, name, rect.left, rect.top,
                          rect.right - rect.left, rect.bottom - rect.top, elevated)

    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        hwnd = self.user32.WindowFromPoint(wt.POINT(x, y))
        if not hwnd:
            return None
        root = self.user32.GetAncestor(hwnd, 2)  # GA_ROOT
        return self._window_info(root or hwnd)

    def foreground_window(self) -> WindowInfo | None:
        return self._window_info(self.user32.GetForegroundWindow())

    def list_windows(self) -> list[WindowInfo]:
        result: list[WindowInfo] = []
        dwmapi = ctypes.WinDLL("dwmapi")

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _lparam):
            try:
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                cloaked = wt.DWORD(0)
                dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                if cloaked.value:
                    return True
                info = self._window_info(hwnd)
                if info and info.title:
                    result.append(info)
            except Exception:
                pass
            return True

        self.user32.EnumWindows(cb, 0)
        return result

    def focus_window(self, handle: int) -> bool:
        u = self.user32
        if not u.IsWindow(handle):
            return False
        # 后台进程调用 SetForegroundWindow 受限：先合成一次 Alt 按键（第 7 节第 7 条）
        u.keybd_event(_VK_MENU, 0, 0, 0)
        u.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
        if u.IsIconic(handle):
            u.ShowWindow(handle, 9)  # SW_RESTORE
        u.SetForegroundWindow(handle)
        time.sleep(0.15)
        fg = u.GetForegroundWindow()
        return bool(fg) and int(fg) == int(handle)

    # -- 输入 --
    def _failsafe_check(self) -> None:
        """与 pyautogui 的 FAILSAFE 语义一致：动作开始前鼠标在主屏左上角 → 紧急停止。"""
        if self.cursor_pos() == (0, 0):
            raise FailSafeTriggered()

    def _send(self, inputs: list[_INPUT]) -> None:
        if not inputs:
            return
        arr = (_INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            raise BackendError(
                "SendInput 被拒绝：目标窗口可能以管理员身份运行（UIPI），或当前处于安全桌面/UAC 弹窗。"
            )

    def _mouse_event(self, flags: int, data: int = 0) -> None:
        inp = _INPUT(type=_INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        self._send([inp])

    def _move(self, x: int, y: int) -> None:
        # SetCursorPos 使用虚拟桌面物理坐标，多显示器/负坐标都正确
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise BackendError(f"移动鼠标到 ({x},{y}) 失败")
        time.sleep(0.03)

    def _translate_failsafe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except self._pag.FailSafeException as e:
            raise FailSafeTriggered() from e

    def click(self, x, y, button, clicks, modifiers):
        self._failsafe_check()
        down, up = _MOUSEEVENTF[button]
        held: list[str] = []
        try:
            for m in modifiers:
                self._translate_failsafe(lambda m=m: self._pag.keyDown(m))
                held.append(m)
            self._move(x, y)
            for _ in range(clicks):
                self._mouse_event(down)
                self._mouse_event(up)
                time.sleep(0.05)
        finally:
            for m in reversed(held):
                self._pag.keyUp(m)

    def move_mouse(self, x, y):
        self._failsafe_check()
        self._move(x, y)

    def drag(self, x1, y1, x2, y2, button):
        self._failsafe_check()
        down, up = _MOUSEEVENTF[button]
        self._move(x1, y1)
        self._mouse_event(down)
        try:
            steps = 15
            for i in range(1, steps + 1):
                self._move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
                time.sleep(0.01)
            time.sleep(0.1)
        finally:
            self._mouse_event(up)

    def scroll(self, x, y, direction, amount):
        self._failsafe_check()
        self._move(x, y)
        delta = amount * _WHEEL_DELTA  # 第 7 节第 10 条：1 格 = 120
        if direction == "up":
            self._mouse_event(_MOUSEEVENTF_WHEEL, delta)
        elif direction == "down":
            self._mouse_event(_MOUSEEVENTF_WHEEL, -delta)
        elif direction == "right":
            self._mouse_event(_MOUSEEVENTF_HWHEEL, delta)
        else:
            self._mouse_event(_MOUSEEVENTF_HWHEEL, -delta)

    @staticmethod
    def _char_events(text: str) -> list[list[_INPUT]]:
        """把文本变成“每个字符一组”的按键事件。一个字符的全部事件必须在同一组里：
        emoji 是 UTF-16 代理对（两个码元），拆开发送会得到乱码。"""
        def key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
            inp = _INPUT(type=_INPUT_KEYBOARD)
            inp.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
            return inp

        groups: list[list[_INPUT]] = []
        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            if ch == "\n":
                groups.append([key(_VK_RETURN), key(_VK_RETURN, flags=_KEYEVENTF_KEYUP)])
            elif ch == "\t":
                groups.append([key(_VK_TAB), key(_VK_TAB, flags=_KEYEVENTF_KEYUP)])
            else:
                data, events = ch.encode("utf-16-le"), []
                for i in range(0, len(data), 2):
                    unit = int.from_bytes(data[i:i + 2], "little")
                    events += [key(0, unit, _KEYEVENTF_UNICODE), key(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)]
                groups.append(events)
        return groups

    def type_text(self, text: str) -> None:
        self._failsafe_check()
        groups = self._char_events(text)
        for i in range(0, len(groups), self.type_chunk_chars):
            chunk = groups[i:i + self.type_chunk_chars]
            self._send([e for g in chunk for e in g])
            pause = self.type_delay_s            # 给目标程序时间按序处理，否则会丢字/错字
            if any(g[0].ki.wVk == _VK_RETURN for g in chunk):
                pause = max(pause, self.type_newline_delay_s)
            if pause:
                time.sleep(pause)

    # -- 剪贴板粘贴 --
    def _clip_open(self, hwnd) -> None:
        for _ in range(20):                      # 剪贴板可能被其他程序短暂占用
            if self.user32.OpenClipboard(hwnd):
                return
            time.sleep(0.05)
        raise BackendError("剪贴板被其他程序占用，无法粘贴。")

    def _clip_get_text(self) -> str | None:
        h = self.user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        size = self.kernel32.GlobalSize(h)
        p = self.kernel32.GlobalLock(h)
        try:
            raw = ctypes.string_at(p, size)             # 有界读取，显式按 UTF-16-LE 解码（与平台的 wchar_t 宽度无关）
        finally:
            self.kernel32.GlobalUnlock(h)
        return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]

    def _clip_set_text(self, text: str) -> None:
        data = (text + "\0").encode("utf-16-le")
        h = self.kernel32.GlobalAlloc(0x0002, len(data))            # GMEM_MOVEABLE
        if not h:
            raise BackendError("分配剪贴板内存失败。")
        ctypes.memmove(self.kernel32.GlobalLock(h), data, len(data))
        self.kernel32.GlobalUnlock(h)
        if not self.user32.SetClipboardData(_CF_UNICODETEXT, h):
            self.kernel32.GlobalFree(h)
            raise BackendError("写入剪贴板失败。")

    def paste_text(self, text: str) -> str:
        """备份文本剪贴板 → 写入 → Ctrl+V → 等应用读走 → 恢复。OpenClipboard(NULL) 之后 SetClipboardData 会失败，
        所以用一个临时的隐藏窗口作为剪贴板所有者。"""
        self._failsafe_check()
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        if not text:
            return ""
        u = self.user32
        owner = u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        if not owner:
            raise BackendError("无法创建剪贴板所有者窗口。")
        orig, other_formats = None, 0
        try:
            self._clip_open(owner)
            try:
                fmts, f = [], u.EnumClipboardFormats(0)
                while f:
                    fmts.append(f)
                    f = u.EnumClipboardFormats(f)
                if _CF_UNICODETEXT in fmts:
                    orig = self._clip_get_text()
                other_formats = len([f for f in fmts if f not in (_CF_TEXT, _CF_OEMTEXT, _CF_UNICODETEXT, _CF_LOCALE)])
                u.EmptyClipboard()
                self._clip_set_text(text)
            finally:
                u.CloseClipboard()
            try:
                def key(vk, up=False):
                    inp = _INPUT(type=_INPUT_KEYBOARD)
                    inp.ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP if up else 0, 0, 0)
                    return inp
                self._send([key(0x11), key(0x56), key(0x56, True), key(0x11, True)])   # Ctrl+V
                time.sleep(self.paste_restore_delay_s)
            finally:
                try:                                  # 无论粘贴是否成功都要恢复剪贴板
                    self._clip_open(owner)
                    try:
                        u.EmptyClipboard()
                        if orig is not None:
                            self._clip_set_text(orig)
                    finally:
                        u.CloseClipboard()
                except BackendError:
                    pass
        finally:
            u.DestroyWindow(owner)
        if other_formats:
            return f"（注意：原剪贴板里有 {other_formats} 种非文本格式，已被替换；只恢复了其中的文本。）"
        return ""

    def press_keys(self, keys, repeat):
        unknown = [k for k in keys if k not in self._pag.KEYBOARD_KEYS]
        if unknown:
            raise BackendError(f"不支持的按键: {', '.join(unknown)}")
        for _ in range(repeat):
            if len(keys) == 1:
                self._translate_failsafe(lambda: self._pag.press(keys[0]))
            else:
                self._translate_failsafe(lambda: self._pag.hotkey(*keys))
            time.sleep(0.03)

    # -- 弹窗 --
    dialog_slack_s = 15.0       # 子进程比倒计时多给的宽限（启动慢、系统繁忙时）

    def _confirm_tk(self, title: str, message: str, timeout_s: float) -> bool | None:
        """在**独立的子进程**里显示授权框（屏幕顶部居中、置顶、不抢焦点），Agent 只读它的结果。

        不能在 Agent 的工作线程里跑 tkinter：Tk 对象必须在创建它的线程里销毁，授权框关闭后被别的线程的
        垃圾回收销毁时，Tcl 会 `Tcl_AsyncDelete: async handler deleted by the wrong thread` 直接 abort 整个进程
        （实机上授权框超时后 Agent 就是这样崩溃的）。进程隔离后，子进程出任何问题都只会让本函数返回 None
        （由调用方回退到系统消息框），不会影响 Agent。返回 None = 子进程不可用；True/False = 用户的选择。
        """
        import json
        import subprocess
        cmd = self._dialog_command()
        payload = json.dumps({"title": title, "message": message, "timeout_s": timeout_s}, ensure_ascii=False)
        try:
            r = subprocess.run(cmd, input=payload, capture_output=True, text=True, encoding="utf-8",
                               timeout=timeout_s + self.dialog_slack_s, creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False                      # 子进程被 run() 杀掉；超时视为拒绝
        except Exception:                     # noqa: BLE001  无法启动子进程
            return None
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip() in (_DIALOG_YES, _DIALOG_NO):
                return line.strip() == _DIALOG_YES
        return None                           # 子进程崩溃/没有输出：回退

    @staticmethod
    def _dialog_command() -> list[str]:
        return [sys.executable, os.path.abspath(__file__), "--confirm-dialog"]

    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        try:
            answer = self._confirm_tk(title, message, timeout_s)
            if answer is not None:
                return answer
        except Exception:
            pass                                       # 任何问题都回退到系统消息框，保证一定能让用户做出选择
        return self._confirm_messagebox(title, message, timeout_s)

    def _confirm_messagebox(self, title: str, message: str, timeout_s: float) -> bool:
        MB_YESNO, MB_ICONQUESTION = 0x4, 0x20
        MB_SETFOREGROUND, MB_SYSTEMMODAL, MB_TOPMOST = 0x10000, 0x1000, 0x40000
        flags = MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        IDYES = 6
        try:
            fn = self.user32.MessageBoxTimeoutW  # 未公开但长期存在
        except AttributeError:
            fn = None
        if fn is not None:
            fn.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT, wt.WORD, wt.DWORD]
            fn.restype = ctypes.c_int
            return fn(None, message, title, flags, 0, int(timeout_s * 1000)) == IDYES
        mb = self.user32.MessageBoxW
        mb.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
        mb.restype = ctypes.c_int
        return mb(None, message, title, flags) == IDYES

    def is_self_elevated(self) -> bool:
        try:
            return bool(self.shell32.IsUserAnAdmin())
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# 授权框（在独立子进程里运行：python backends.py --confirm-dialog，参数从 stdin 读 JSON）
# 这里的 tkinter 全部在本进程的主线程里创建和销毁，不存在跨线程销毁的问题。
# --------------------------------------------------------------------------- #
def _make_noactivate(hwnd: int) -> None:
    """让窗口置顶、且点击它不会抢走前台窗口。"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindowLongPtrW.argtypes, u.GetWindowLongPtrW.restype = [wt.HWND, ctypes.c_int], ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    GWL_EXSTYLE = -20
    style = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | 0x8 | 0x80 | 0x08000000)    # TOPMOST|TOOLWINDOW|NOACTIVATE
    u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x20)
    try:
        u.MessageBeep(0x20)
    except Exception:       # noqa: BLE001
        pass


def run_confirm_dialog(title: str, message: str, timeout_s: float) -> bool:
    import tkinter as tk
    result = {"yes": False}
    root = tk.Tk()
    after_id = None
    try:
        root.withdraw()
        root.overrideredirect(True)                 # 无标题栏的弹出窗口：显示时不会被激活
        root.attributes("-topmost", True)
        font = "Microsoft YaHei UI"
        frame = tk.Frame(root, bg="#202020", highlightbackground="#e0a800", highlightthickness=3)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="⚠ " + title, bg="#e0a800", fg="black", anchor="w", padx=12, pady=4,
                 font=(font, 11, "bold")).pack(fill="x")
        tk.Label(frame, text=message, bg="#202020", fg="white", justify="left", wraplength=640,
                 padx=16, pady=10, font=(font, 10)).pack()
        countdown = tk.Label(frame, text="", bg="#202020", fg="#aaaaaa", font=(font, 9))
        countdown.pack()
        btns = tk.Frame(frame, bg="#202020")
        btns.pack(pady=10)

        def done(v: bool) -> None:
            result["yes"] = v
            root.quit()
        tk.Button(btns, text="允许", width=14, bg="#2e7d32", fg="white", activebackground="#388e3c",
                  font=(font, 10, "bold"), command=lambda: done(True)).pack(side="left", padx=12)
        tk.Button(btns, text="拒绝", width=14, bg="#c62828", fg="white", activebackground="#d32f2f",
                  font=(font, 10, "bold"), command=lambda: done(False)).pack(side="left", padx=12)
        deadline = time.time() + timeout_s

        def tick() -> None:
            nonlocal after_id
            remain = int(deadline - time.time() + 0.999)
            if remain <= 0:
                done(False)
                return
            countdown.config(text=f"{remain} 秒内未选择视为拒绝")
            after_id = root.after(250, tick)
        tick()
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"{w}x{h}+{max(0, (root.winfo_screenwidth() - w) // 2)}+60")   # 屏幕顶部居中
        root.deiconify()
        root.update()
        _make_noactivate(root.winfo_id())
        root.mainloop()
    finally:
        if after_id is not None:
            try:
                root.after_cancel(after_id)
            except Exception:       # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:           # noqa: BLE001
            pass
    return result["yes"]


def _dialog_main() -> int:
    import json
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:       # noqa: BLE001
        pass
    # 必须读字节并显式按 UTF-8 解码：sys.stdin 默认按系统代码页（中文 Windows 是 GBK）解码，授权框里的中文会乱码；
    # 同时容忍 BOM（PowerShell 5.1 往管道写字符串会带 UTF-8 BOM）。
    args = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    yes = run_confirm_dialog(args["title"], args["message"], float(args["timeout_s"]))
    print(_DIALOG_YES if yes else _DIALOG_NO, flush=True)
    return 0


if __name__ == "__main__":
    if "--confirm-dialog" in sys.argv:
        sys.exit(_dialog_main())
