"""Backend abstraction: WindowsBackend (real) / FakeBackend (for tests).

This module must be importable on Linux: every Windows-specific call lives in
the constructor/methods of WindowsBackend, and the platform check is in the constructor as well.
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
# Data types
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
    process: str  # lowercase process name, e.g. "chrome.exe"; "" when unknown
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    elevated: bool = False  # whether the target process runs as administrator


class FailSafeTriggered(Exception):
    """The user moved the mouse to the top-left corner of the primary screen (emergency stop)."""


class BackendError(Exception):
    """A backend error that can be shown directly to the model (e.g. UIPI, failure to activate a window)."""


# --------------------------------------------------------------------------- #
# Abstraction
# --------------------------------------------------------------------------- #
class Backend(ABC):
    name = "abstract"

    @abstractmethod
    def monitors(self) -> list[Monitor]:
        """All monitors (order matches the tool parameter monitor=1..n), in physical pixels."""

    @abstractmethod
    def grab(self, monitor: Monitor) -> Image.Image:
        """Capture the given monitor at physical resolution and return an RGB image (without the mouse pointer)."""

    @abstractmethod
    def cursor_pos(self) -> tuple[int, int]: ...

    @abstractmethod
    def window_at_point(self, x: int, y: int) -> WindowInfo | None:
        """Top-level windows in physical coordinates (GetAncestor(GA_ROOT))."""

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
        """Press the key combination `keys` (normalized names, e.g. ["ctrl","shift","escape"])."""

    @abstractmethod
    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        """Show a topmost yes/no dialog on this machine; timeout or no -> False."""

    def paste_text(self, text: str) -> str:
        """Paste text through the clipboard (atomic; unaffected by the target app stalling on key handling). Returns a note (may be empty)."""
        raise BackendError("This backend does not support paste input.")

    def is_self_elevated(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# FakeBackend (for tests)
# --------------------------------------------------------------------------- #
class FakeBackend(Backend):
    """In-memory canvas that records all actions.

    actions: the action log, e.g. {"op": "click", "x": 1280, "y": 720, ...}
    foreground_process / point_process: process names of the foreground window / the window under the coordinates
    confirm_result: the dialog return value; confirm_calls: the log of questions asked
    failsafe_armed: when True, the next input action raises FailSafeTriggered
    black: when True, screenshots are completely black (simulates a locked screen)
    """

    name = "fake"

    def __init__(self, monitors: list[Monitor] | None = None) -> None:
        self._monitors = monitors or [Monitor(0, 0, 2560, 1440)]
        self.actions: list[dict] = []
        self.foreground_process = "notepad.exe"
        self.foreground_title = "Untitled - Notepad"
        self.point_process: str | None = None  # None -> same as the foreground
        self.windows: list[WindowInfo] = [
            WindowInfo(1001, "Untitled - Notepad", "notepad.exe", 100, 100, 800, 600),
            WindowInfo(1002, "Google Chrome", "chrome.exe", 200, 150, 1200, 800),
        ]
        self.confirm_result = True
        self.during_confirm = None        # optional callback: runs during confirm
        self.confirm_calls: list[tuple[str, str, float]] = []
        self.failsafe_armed = False
        self.black = False
        self.elevated_processes: set[str] = set()
        self.self_elevated = False
        self._cursor = (10, 10)

    # -- queries --
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

    # -- actions --
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
            self.during_confirm()      # simulate the dialog stealing focus, which changes the foreground window / the window under the mouse
        return self.confirm_result


# --------------------------------------------------------------------------- #
# WindowsBackend (real)
# --------------------------------------------------------------------------- #
# SendInput structures (defining them on Linux is harmless; they are just not used)
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
        # type_text pacing: one SendInput per type_chunk_chars characters, sleeping type_delay_ms milliseconds between batches.
        # When dozens of characters are pushed at once, the new Notepad (WinUI) drops characters or repeats one character for the whole line, so the default is per character + 5 ms.
        self.type_chunk_chars = max(1, int(type_chunk_chars))
        self.type_delay_s = max(0.0, float(type_delay_ms)) / 1000
        # Enter stalls some apps (e.g. the new Notepad) for a moment, and keys that pile up meanwhile get merged into one character, so wait longer after Enter
        self.type_newline_delay_s = max(0.0, float(type_newline_delay_ms)) / 1000
        # After pasting, wait until the app has really read the clipboard before restoring the original clipboard contents
        self.paste_restore_delay_s = max(0.0, float(paste_restore_delay_ms)) / 1000
        if sys.platform != "win32":
            raise RuntimeError("WindowsBackend can only be used on Windows (use --backend fake for testing)")
        # DPI awareness: must be set before any screenshot/coordinate operation
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        import mss  # noqa: F401  imported lazily so that import does not fail on Linux without a display
        import pyautogui

        pyautogui.FAILSAFE = True  # mouse in the top-left corner of the primary screen -> FailSafeException
        pyautogui.PAUSE = 0
        self._pag = pyautogui
        self._mss = mss

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._setup_prototypes()
        self._pid_cache: dict[int, tuple[str, bool]] = {}

    # -- ctypes prototypes: on 64-bit, HWND must be declared or it gets truncated --
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
        # clipboard (always use c_void_p for pointers/handles; they must not be truncated on 64-bit)
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

    # -- screenshots --
    def monitors(self) -> list[Monitor]:
        with self._mss.mss() as sct:
            return [Monitor(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]

    def grab(self, monitor: Monitor) -> Image.Image:
        region = {"left": monitor.left, "top": monitor.top, "width": monitor.width, "height": monitor.height}
        with self._mss.mss() as sct:  # mss instances are not reused across threads
            shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def cursor_pos(self) -> tuple[int, int]:
        pt = wt.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    # -- windows / processes --
    def _process_info(self, pid: int) -> tuple[str, bool]:
        """Return (lowercase process name, whether elevated)."""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        TOKEN_QUERY = 0x0008
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # If it cannot be opened it is most likely a higher-privilege process; the name is unknown, so treat it as elevated
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
        # SetForegroundWindow is restricted for background processes: synthesize an Alt key press first
        u.keybd_event(_VK_MENU, 0, 0, 0)
        u.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
        if u.IsIconic(handle):
            u.ShowWindow(handle, 9)  # SW_RESTORE
        u.SetForegroundWindow(handle)
        time.sleep(0.15)
        fg = u.GetForegroundWindow()
        return bool(fg) and int(fg) == int(handle)

    # -- input --
    def _failsafe_check(self) -> None:
        """Same semantics as pyautogui FAILSAFE: if the mouse is in the top-left corner of the primary screen before an action starts -> emergency stop."""
        if self.cursor_pos() == (0, 0):
            raise FailSafeTriggered()

    def _send(self, inputs: list[_INPUT]) -> None:
        if not inputs:
            return
        arr = (_INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            raise BackendError(
                "SendInput was rejected: the target window may run as administrator (UIPI), or the desktop is currently a secure desktop / UAC prompt."
            )

    def _mouse_event(self, flags: int, data: int = 0) -> None:
        inp = _INPUT(type=_INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        self._send([inp])

    def _move(self, x: int, y: int) -> None:
        # SetCursorPos uses physical coordinates of the virtual desktop; multiple monitors / negative coordinates are handled correctly
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise BackendError(f"Failed to move the mouse to ({x},{y})")
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
        delta = amount * _WHEEL_DELTA  # 1 notch = 120
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
        """Turn text into key events grouped "one group per character". All events of one character must be in the same group:
        an emoji is a UTF-16 surrogate pair (two code units), and sending them separately produces garbage."""
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
            pause = self.type_delay_s            # give the target app time to process in order, otherwise characters are dropped/garbled
            if any(g[0].ki.wVk == _VK_RETURN for g in chunk):
                pause = max(pause, self.type_newline_delay_s)
            if pause:
                time.sleep(pause)

    # -- clipboard paste --
    def _clip_open(self, hwnd) -> None:
        for _ in range(20):                      # the clipboard may be briefly held by another program
            if self.user32.OpenClipboard(hwnd):
                return
            time.sleep(0.05)
        raise BackendError("The clipboard is held by another program; cannot paste.")

    def _clip_get_text(self) -> str | None:
        h = self.user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        size = self.kernel32.GlobalSize(h)
        p = self.kernel32.GlobalLock(h)
        try:
            raw = ctypes.string_at(p, size)             # bounded read, explicitly decoded as UTF-16-LE (independent of the platform wchar_t width)
        finally:
            self.kernel32.GlobalUnlock(h)
        return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]

    def _clip_set_text(self, text: str) -> None:
        data = (text + "\0").encode("utf-16-le")
        h = self.kernel32.GlobalAlloc(0x0002, len(data))            # GMEM_MOVEABLE
        if not h:
            raise BackendError("Failed to allocate clipboard memory.")
        ctypes.memmove(self.kernel32.GlobalLock(h), data, len(data))
        self.kernel32.GlobalUnlock(h)
        if not self.user32.SetClipboardData(_CF_UNICODETEXT, h):
            self.kernel32.GlobalFree(h)
            raise BackendError("Failed to write to the clipboard.")

    def paste_text(self, text: str) -> str:
        """Back up the text clipboard -> write -> Ctrl+V -> wait for the app to read it -> restore. After OpenClipboard(NULL) SetClipboardData fails,
        so a temporary hidden window is used as the clipboard owner."""
        self._failsafe_check()
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        if not text:
            return ""
        u = self.user32
        owner = u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        if not owner:
            raise BackendError("Cannot create the clipboard owner window.")
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
                try:                                  # restore the clipboard whether or not the paste succeeded
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
            return f"(Note: the original clipboard held {other_formats} non-text format(s), which were replaced; only the text was restored.)"
        return ""

    def press_keys(self, keys, repeat):
        unknown = [k for k in keys if k not in self._pag.KEYBOARD_KEYS]
        if unknown:
            raise BackendError(f"Unsupported key: {', '.join(unknown)}")
        for _ in range(repeat):
            if len(keys) == 1:
                self._translate_failsafe(lambda: self._pag.press(keys[0]))
            else:
                self._translate_failsafe(lambda: self._pag.hotkey(*keys))
            time.sleep(0.03)

    # -- dialog --
    dialog_slack_s = 15.0       # extra grace the subprocess gets beyond the countdown (slow start, busy system)

    def _confirm_tk(self, title: str, message: str, timeout_s: float) -> bool | None:
        """Show the approval dialog in a **separate subprocess** (centered at the top of the screen, topmost, without stealing focus); the Agent only reads its result.

        tkinter must not run in an Agent worker thread: a Tk object has to be destroyed in the thread that created it. When the closed dialog is destroyed by garbage collection in another thread,
        Tcl aborts the whole process with `Tcl_AsyncDelete: async handler deleted by the wrong thread`
        (this is how the Agent crashed on a real machine after a dialog timeout). With process isolation, any problem in the subprocess only makes this function return None
        (the caller then falls back to a system message box) and never affects the Agent. Returns None = subprocess unavailable; True/False = the user's choice.
        """
        import json
        import subprocess
        cmd = self._dialog_command()
        payload = json.dumps({"title": title, "message": message, "timeout_s": timeout_s}, ensure_ascii=False)
        try:
            r = subprocess.run(cmd, input=payload, capture_output=True, text=True, encoding="utf-8",
                               timeout=timeout_s + self.dialog_slack_s, creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False                      # the subprocess was killed by run(); a timeout counts as denial
        except Exception:                     # noqa: BLE001  the subprocess could not be started
            return None
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip() in (_DIALOG_YES, _DIALOG_NO):
                return line.strip() == _DIALOG_YES
        return None                           # the subprocess crashed / produced no output: fall back

    @staticmethod
    def _dialog_command() -> list[str]:
        return [sys.executable, os.path.abspath(__file__), "--confirm-dialog"]

    def confirm(self, title: str, message: str, timeout_s: float) -> bool:
        try:
            answer = self._confirm_tk(title, message, timeout_s)
            if answer is not None:
                return answer
        except Exception:
            pass                                       # any problem falls back to the system message box, so the user can always decide
        return self._confirm_messagebox(title, message, timeout_s)

    def _confirm_messagebox(self, title: str, message: str, timeout_s: float) -> bool:
        MB_YESNO, MB_ICONQUESTION = 0x4, 0x20
        MB_SETFOREGROUND, MB_SYSTEMMODAL, MB_TOPMOST = 0x10000, 0x1000, 0x40000
        flags = MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        IDYES = 6
        try:
            fn = self.user32.MessageBoxTimeoutW  # undocumented but long-standing
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
# Approval dialog (runs in a separate subprocess: python backends.py --confirm-dialog, parameters read as JSON from stdin)
# All tkinter objects here are created and destroyed in this process's main thread, so there is no cross-thread destruction problem.
# --------------------------------------------------------------------------- #
def _make_noactivate(hwnd: int) -> None:
    """Make the window topmost, and make clicking it not steal the foreground window."""
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
        root.overrideredirect(True)                 # a popup without a title bar: it is not activated when shown
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
        tk.Button(btns, text="Allow", width=14, bg="#2e7d32", fg="white", activebackground="#388e3c",
                  font=(font, 10, "bold"), command=lambda: done(True)).pack(side="left", padx=12)
        tk.Button(btns, text="Deny", width=14, bg="#c62828", fg="white", activebackground="#d32f2f",
                  font=(font, 10, "bold"), command=lambda: done(False)).pack(side="left", padx=12)
        deadline = time.time() + timeout_s

        def tick() -> None:
            nonlocal after_id
            remain = int(deadline - time.time() + 0.999)
            if remain <= 0:
                done(False)
                return
            countdown.config(text=f"No choice within {remain} s counts as denial")
            after_id = root.after(250, tick)
        tick()
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"{w}x{h}+{max(0, (root.winfo_screenwidth() - w) // 2)}+60")   # centered at the top of the screen
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
    # Must read bytes and decode explicitly as UTF-8: sys.stdin decodes with the system code page by default (GBK on Chinese Windows), which garbles non-ASCII text in the dialog;
    # also tolerate a BOM (PowerShell 5.1 prepends a UTF-8 BOM when writing a string to a pipe).
    args = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    yes = run_confirm_dialog(args["title"], args["message"], float(args["timeout_s"]))
    print(_DIALOG_YES if yes else _DIALOG_NO, flush=True)
    return 0


if __name__ == "__main__":
    if "--confirm-dialog" in sys.argv:
        sys.exit(_dialog_main())
