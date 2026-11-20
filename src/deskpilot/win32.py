"""The Windows driver: window messages posted to the target window, so the user's pointer and focus stay theirs.

Coordinates are physical pixels: the process is per-monitor DPI aware (v2) before it reads any window. Pointer events
are posted to the deepest child window under the point in client coordinates; keys are posted to the target thread's
focused control. A posted message carries its buttons and modifiers in the target thread's key state too, for apps
that read them with GetKeyState rather than from the message.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PIL import Image

from .errors import Unavailable
from .hypr import Window
from .native import MODIFIERS, ElementError, Node, descendants

try:
    from _ctypes import COMError  # UI Automation's failures: an element gone, a provider that does not answer
except ImportError:  # not Windows
    COMError = OSError

INSTANCE = "win32"

# Messages, flags and constants from WinUser.h and dwmapi.h.
WM_NULL, WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0000, 0x0100, 0x0101, 0x0102, 0x0104, 0x0105
SMTO_ABORTIFHUNG = 0x0002
BUTTON_VK = {"left": 0x01, "right": 0x02, "middle": 0x04}
# A modifier's generic key and its left-hand key, which apps read alike.
MODIFIER_SIDES = {0x11: 0xA2, 0x10: 0xA0, 0x12: 0xA4, 0x5B: 0x5B}
WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
BUTTON_MESSAGES = {
    # button: (down, up, double-click, MK_ flag)
    "left": (0x0201, 0x0202, 0x0203, 0x0001),
    "right": (0x0204, 0x0205, 0x0206, 0x0002),
    "middle": (0x0207, 0x0208, 0x0209, 0x0010),
}
WHEEL_DELTA = 120
GA_ROOT = 2
HWND_MESSAGE = -3
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 2
CWP_SKIPINVISIBLE, CWP_SKIPDISABLED, CWP_SKIPTRANSPARENT = 0x1, 0x2, 0x4
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4
MAPVK_VK_TO_VSC = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF = {
    ("left", True): 0x0002,
    ("left", False): 0x0004,
    ("right", True): 0x0008,
    ("right", False): 0x0010,
    ("middle", True): 0x0020,
    ("middle", False): 0x0040,
}
MOUSEEVENTF_WHEEL = 0x0800

# Virtual-key codes for keys.NAMED and the modifiers.
VK = {
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "space": 0x20,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "super": 0x5B, "meta": 0x5B, "win": 0x5B,
    "cmd": 0x11, "command": 0x11, "capslock": 0x14, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}  # fmt: skip
# UIA control type ids (UIA_ButtonControlTypeId = 50000 onwards) as stable, unlocalized role names.
CONTROL_TYPES = (
    "button", "calendar", "check box", "combo box", "edit", "hyperlink", "image", "list item", "list", "menu",
    "menu bar", "menu item", "progress bar", "radio button", "scroll bar", "slider", "spinner", "status bar", "tab",
    "tab item", "text", "tool bar", "tool tip", "tree", "tree item", "custom", "group", "thumb", "data grid",
    "data item", "document", "split button", "window", "pane", "header", "header item", "table", "title bar",
    "separator", "semantic zoom", "app bar",
)  # fmt: skip
UIA_INVOKE_PATTERN = 10000
UIA_VALUE_PATTERN = 10002
EXTENDED = frozenset({0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B})


def lparam_point(x: int, y: int) -> int:
    """MAKELPARAM of two signed 16-bit coordinates."""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def key_state(state: bytes, down: set[int]) -> bytes:
    """A 256-byte keyboard state (GetKeyboardState) with exactly the buttons and keys in `down` pressed."""
    held = set(down) | {MODIFIER_SIDES[vk] for vk in down if vk in MODIFIER_SIDES}
    tracked = set(BUTTON_VK.values()) | set(MODIFIER_SIDES) | set(MODIFIER_SIDES.values())
    out = bytearray(state)
    for vk in tracked | held:
        out[vk] = (out[vk] | 0x80) if vk in held else (out[vk] & 0x7F)
    return bytes(out)


def restore_state(saved: bytes, wanted: bytes, now: bytes) -> bytes:
    """The key state after a posted message: each byte deskpilot changed goes back to what it was, unless the user's
    own input has changed it since."""
    return bytes(s if w != s and n == w else n for s, w, n in zip(saved, wanted, now, strict=True))


def key_lparam(scan: int, down: bool, extended: bool, alt: bool = False) -> int:
    """The lParam of a posted WM_KEYDOWN or WM_KEYUP: repeat count 1, the scan code, and the transition bits."""
    value = 1 | (scan & 0xFF) << 16
    if extended:
        value |= 1 << 24
    if alt:
        value |= 1 << 29
    if not down:
        value |= 1 << 30 | 1 << 31
    return value


def wheel_wparam(notches: int, held: int = 0) -> int:
    """WM_MOUSEWHEEL's wParam: the signed delta in the high word, the MK_ flags in the low one."""
    return ((notches * WHEEL_DELTA) & 0xFFFF) << 16 | held


def mk_flags(held: frozenset[str]) -> int:
    return sum(BUTTON_MESSAGES[b][3] for b in held if b in BUTTON_MESSAGES)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    )


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class Win32Driver:
    instance = INSTANCE

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Unavailable("the Windows driver runs only on Windows")
        self.user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32: Any = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwmapi: Any = ctypes.WinDLL("dwmapi")
        self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        for name in ("WindowFromPoint", "GetAncestor", "ChildWindowFromPointEx", "GetForegroundWindow", "GetWindow"):
            getattr(self.user32, name).restype = wintypes.HWND
        self.user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        self.user32.ChildWindowFromPointEx.argtypes = (wintypes.HWND, wintypes.POINT, wintypes.UINT)
        self.user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalAlloc.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
        self.kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        self.user32.GetClipboardData.restype = ctypes.c_void_p
        self.user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        )  # fmt: skip
        self.user32.OpenClipboard.argtypes = (wintypes.HWND,)
        self.kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
        self._pressed: dict[str, Any] = {}  # window address: the control a held button was pressed on
        self._uia: Any = None

    # Windows

    def _dwm_rect(self, hwnd: int) -> wintypes.RECT:
        rect = wintypes.RECT()
        if self.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        ):
            self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        return rect

    def _cloaked(self, hwnd: int) -> bool:
        value = wintypes.DWORD()
        self.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), 4)
        return bool(value.value)

    def _pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return pid.value

    def _process(self, pid: int) -> tuple[str, int]:
        """The process's executable name and creation time, which with the HWND names one window for good."""
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "", 0
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            times = [wintypes.FILETIME() for _ in range(4)]
            self.kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
            created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
            return Path(buffer.value).stem, created
        finally:
            self.kernel32.CloseHandle(handle)

    def windows(self) -> list[Window]:
        """Top-level windows a user could see, front to back (EnumWindows walks the z-order from the top)."""
        handles: list[int] = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
            lambda hwnd, _: handles.append(hwnd or 0) or True
        )
        self.user32.EnumWindows(callback, 0)
        foreground = self.user32.GetForegroundWindow() or 0
        found: list[Window] = []
        for hwnd in handles:
            if not self.user32.IsWindowVisible(wintypes.HWND(hwnd)) or self._cloaked(hwnd):
                continue
            if self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                continue
            length = self.user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
            if not length:
                continue
            title = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(wintypes.HWND(hwnd), title, length + 1)
            rect = self._dwm_rect(hwnd)
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width <= 0 or height <= 0:
                continue
            pid = self._pid(hwnd)
            exe, created = self._process(pid)
            found.append(
                Window(
                    address=hex(hwnd),
                    cls=exe,
                    title=title.value,
                    x=rect.left,
                    y=rect.top,
                    width=width,
                    height=height,
                    monitor=0,
                    workspace="",
                    xwayland=False,
                    pid=pid,
                    focused=hwnd == foreground,
                    instance=INSTANCE,
                    stable_id=f"{hwnd:x}-{created:x}",
                    visible=not self.user32.IsIconic(wintypes.HWND(hwnd)),
                )
            )
        return found

    def locked(self) -> bool:
        """The input desktop is the Winlogon one, not Default, while the session is locked."""
        desktop = self.user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        try:
            name = ctypes.create_unicode_buffer(256)
            self.user32.GetUserObjectInformationW(desktop, UOI_NAME, name, ctypes.sizeof(name), None)
            return name.value.lower() != "default"
        finally:
            self.user32.CloseDesktop(desktop)

    def focus(self, window: Window) -> None:
        """Bring the window to the foreground, and refuse when Windows keeps it from taking focus: SendInput would
        then type into whatever the user has in front."""
        hwnd = wintypes.HWND(int(window.address, 16))
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
        self.user32.SetForegroundWindow(hwnd)
        if (self.user32.GetForegroundWindow() or 0) != hwnd.value:
            raise Unavailable(
                f"Windows did not let {window.cls} ({window.address}) take focus (an app may only take focus from "
                "the one in front); focus it yourself or act without take_focus"
            )

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def top_at(self, x: int, y: int) -> str | None:
        hwnd = self.user32.WindowFromPoint(wintypes.POINT(x, y))
        root = self.user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
        return hex(root) if root else None

    # Capture

    def grab(self, window: Window, path: Path) -> None:
        """PrintWindow with PW_RENDERFULLCONTENT draws the window from its own content, so a window partly under
        another still comes out whole; the frame is cropped to the bounds DWM shows."""
        hwnd = wintypes.HWND(int(window.address, 16))
        outer = wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(outer))
        width, height = outer.right - outer.left, outer.bottom - outer.top
        screen_dc = self.user32.GetDC(None)
        dc = self.gdi32.CreateCompatibleDC(screen_dc)
        bitmap = self.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        try:
            previous = self.gdi32.SelectObject(dc, bitmap)
            drawn = self.user32.PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)
            self.gdi32.SelectObject(dc, previous)  # GetDIBits needs the bitmap out of every DC
            if not drawn:
                raise RuntimeError(f"Windows drew no image of {window.cls} ({window.address})")
            header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if self.gdi32.GetDIBits(dc, bitmap, 0, height, pixels, ctypes.byref(header), 0) != height:
                raise RuntimeError(f"Windows gave no pixels for {window.cls} ({window.address})")
        finally:
            self.gdi32.DeleteObject(bitmap)
            self.gdi32.DeleteDC(dc)
            self.user32.ReleaseDC(None, screen_dc)
        image = Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
        left, top = window.x - outer.left, window.y - outer.top
        image.crop((left, top, left + window.width, top + window.height)).save(path)

    # Input

    def _deepest(self, window: Window, x: int, y: int) -> tuple[Any, int, int]:
        """The child window under (x, y) that would get the event, and the point in its client coordinates."""
        hwnd = wintypes.HWND(int(window.address, 16))
        flags = CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT
        while True:
            point = wintypes.POINT(x, y)
            self.user32.ScreenToClient(hwnd, ctypes.byref(point))
            child = self.user32.ChildWindowFromPointEx(hwnd, point, flags)
            if not child or child == hwnd.value:
                return hwnd, point.x, point.y
            hwnd = wintypes.HWND(child)

    def _post_message(
        self, window: Window, target: Any, message: int, wparam: int, lparam: int, down: frozenset[int] = frozenset()
    ) -> None:
        """Post a message with the buttons and keys in `down` held in the target thread's key state until the app has
        handled it. Apps such as Tk read buttons and modifiers with GetKeyState, not from the message, so the state
        must agree with it. deskpilot attaches to the thread's input queue, sets the state, posts, waits and puts the
        state back, as AutoHotkey's ControlSend does. Only the bytes it set are put back, and only while they still
        hold what it set: in the foreground app the state follows the user's real keyboard, and a key the user pressed
        meanwhile stays as the system recorded it. A key the user holds is up for the message, so their Ctrl never
        turns the agent's click into a Ctrl-click."""
        thread = self.user32.GetWindowThreadProcessId(target, None)
        me = self.kernel32.GetCurrentThreadId()
        if not self.user32.AttachThreadInput(me, thread, True):
            raise RuntimeError(  # refused
                f"Windows would not share {window.cls}'s key state, so it would read this input without its buttons "
                "or modifiers"
            )
        try:
            saved = (ctypes.c_ubyte * 256)()
            self.user32.GetKeyboardState(saved)
            wanted = key_state(bytes(saved), set(down))
            self.user32.SetKeyboardState((ctypes.c_ubyte * 256).from_buffer_copy(wanted))
            try:
                self._post(window, target, message, wparam, lparam)
                # A sent message is handled the next time the thread reads its queue. The first returns once the
                # app takes the posted message from its queue, the second once it has handled it and reads again.
                for _ in range(2):
                    self.user32.SendMessageTimeoutW(target, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 2000, None)
            finally:
                now = (ctypes.c_ubyte * 256)()
                self.user32.GetKeyboardState(now)
                restored = restore_state(bytes(saved), wanted, bytes(now))
                self.user32.SetKeyboardState((ctypes.c_ubyte * 256).from_buffer_copy(restored))
        finally:
            self.user32.AttachThreadInput(me, thread, False)

    def _post(self, window: Window, target: Any, message: int, wparam: int, lparam: int) -> None:
        if not self.user32.PostMessageW(target, message, wparam, lparam):
            raise RuntimeError(  # refused
                f"Windows would not deliver input to {window.cls} ({window.address}); an app running as administrator "
                "takes input only from one that is too"
            )

    def mouse(
        self,
        window: Window,
        event: str,
        x: int,
        y: int,
        *,
        button: str = "left",
        held: frozenset[str] = frozenset(),
        clicks: int = 1,
        dy: int = 0,
        seat: bool = False,
    ) -> None:
        if seat:
            self._seat_mouse(event, x, y, button, dy)
            return
        # While a button is held, every event goes to the control it was pressed on, as Windows' own mouse capture
        # does, so a drag across child controls, or the release after a failure, ends on the one it began on.
        pressed = self._pressed.get(window.address)
        if pressed is not None and (held or event == "up"):
            target = pressed
            point = wintypes.POINT(x, y)
            self.user32.ScreenToClient(target, ctypes.byref(point))
            cx, cy = point.x, point.y
        else:
            target, cx, cy = self._deepest(window, x, y)
        flags = mk_flags(held)
        buttons = frozenset(BUTTON_VK[b] for b in held | ({button} if event == "down" else set()))
        if event == "move":
            self._post_message(window, target, WM_MOUSEMOVE, flags, lparam_point(cx, cy), buttons)
        elif event == "wheel":
            self._post_message(window, target, WM_MOUSEWHEEL, wheel_wparam(dy, flags), lparam_point(x, y), buttons)
        else:
            down, up, double, flag = BUTTON_MESSAGES[button]
            if event == "down":
                message = double if clicks % 2 == 0 else down
                self._post_message(window, target, message, flags | flag, lparam_point(cx, cy), buttons)
                self._pressed[window.address] = target
            else:
                if not held:
                    self._pressed.pop(window.address, None)
                self._post_message(window, target, up, flags, lparam_point(cx, cy), buttons)

    def _send(self, *inputs: _INPUT) -> None:
        array = (_INPUT * len(inputs))(*inputs)
        if self.user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT)) != len(inputs):
            raise RuntimeError("Windows refused the input (another app may be elevated or the desktop is locked)")

    def _seat_mouse(self, event: str, x: int, y: int, button: str, dy: int) -> None:
        self.user32.SetCursorPos(x, y)
        if event == "move":
            return
        if event == "wheel":
            flags, data = MOUSEEVENTF_WHEEL, (dy * WHEEL_DELTA) & 0xFFFFFFFF
        else:
            flags, data = MOUSEEVENTF[(button, event == "down")], 0
        self._send(_INPUT(INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(0, 0, data, flags, 0, 0))))

    def _focused_control(self, window: Window) -> Any:
        hwnd = wintypes.HWND(int(window.address, 16))
        thread = self.user32.GetWindowThreadProcessId(hwnd, None)
        if not thread:  # thread 0 would ask about the foreground thread instead
            raise RuntimeError(f"{window.cls} ({window.address}) has closed")
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if self.user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.hwndFocus:
            # Windows on one thread share its focus: it must be a control of this window, not a sibling's.
            if self.user32.GetAncestor(info.hwndFocus, GA_ROOT) != hwnd.value:
                raise RuntimeError(  # refused
                    f"keyboard focus in {window.cls} is in another of its windows, not {window.address}: click a field "
                    "in this one first"
                )
            return info.hwndFocus
        raise RuntimeError(  # refused: keys would go nowhere
            f"{window.cls} ({window.address}) has no control with keyboard focus to type into: click the field first"
        )

    def _vk(self, key: str) -> tuple[int, int]:
        """A key's virtual-key code and the shift state its character needs on the user's layout (VkKeyScanW)."""
        if key in VK:
            return VK[key], 0
        scan = self.user32.VkKeyScanW(ctypes.c_wchar(key))
        if scan == -1 or scan & 0xFFFF == 0xFFFF:
            raise ValueError(f"no key on the active keyboard layout types {key!r}")
        return scan & 0xFF, (scan >> 8) & 0xFF

    def key(self, window: Window, key: str, down: bool, *, held: frozenset[str], seat: bool = False) -> None:
        vk, _ = self._vk(key)
        if seat:
            flags = 0 if down else KEYEVENTF_KEYUP
            self._send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, 0))))
            return
        mods = {VK[m] for m in held & MODIFIERS}
        pressed = frozenset(mods | ({vk} if down else set()))
        # Windows reports a key pressed with Alt and without Ctrl as a system key, as menus expect.
        alt = VK["alt"] in pressed | {vk} and VK["ctrl"] not in mods
        scan = self.user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        message = (WM_SYSKEYDOWN if down else WM_SYSKEYUP) if alt else (WM_KEYDOWN if down else WM_KEYUP)
        target = self._focused_control(window)
        self._post_message(window, target, message, vk, key_lparam(scan, down, vk in EXTENDED, alt), pressed)

    def text(self, window: Window, char: str, *, seat: bool = False) -> None:
        units = char.encode("utf-16-le")
        codes = [int.from_bytes(units[i : i + 2], "little") for i in range(0, len(units), 2)]
        if seat:
            for code in codes:
                self._send(
                    _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))),
                    _INPUT(
                        INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))
                    ),
                )
            return
        target = self._focused_control(window)
        if char in ("\n", "\r"):
            scan = self.user32.MapVirtualKeyW(VK["return"], MAPVK_VK_TO_VSC)
            for down in (True, False):
                message = WM_KEYDOWN if down else WM_KEYUP
                self._post_message(window, target, message, VK["return"], key_lparam(scan, down, False))
            return
        for code in codes:
            self._post_message(window, target, WM_CHAR, code, 1)

    # Accessibility (UI Automation)

    def _automation(self) -> Any:
        if self._uia is None:
            import comtypes
            import comtypes.client

            comtypes.CoInitialize()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia

            self._uia_module = uia
            self._uia = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
            self._walker = self._uia.ControlViewWalker
        return self._uia

    def root(self, window: Window) -> object:
        try:
            return self._automation().ElementFromHandle(int(window.address, 16))
        except (OSError, COMError, ImportError) as error:
            raise ElementError(
                f"{window.cls} ({window.address}) exposes no UI Automation tree: {error}", "no_tree"
            ) from error

    def children(self, node: Any) -> list[object]:
        self._automation()
        found = []
        try:
            child = self._walker.GetFirstChildElement(node)
            while child:
                found.append(child)
                child = self._walker.GetNextSiblingElement(child)
        except (OSError, COMError) as error:
            raise ElementError(f"an element went away while its children were read: {error}", "stale") from error
        return found

    def _pattern(self, node: Any, pattern_id: int, interface: Any) -> Any:
        try:
            pattern = node.GetCurrentPattern(pattern_id)
        except (OSError, COMError):
            return None
        return pattern.QueryInterface(interface) if pattern else None

    def describe(self, node: Any) -> Node:
        uia = self._uia_module
        try:
            rect = node.CurrentBoundingRectangle
            states = []
            if node.CurrentIsOffscreen:
                states.append("offscreen")
            if not node.CurrentIsEnabled:
                states.append("disabled")
            if node.CurrentHasKeyboardFocus:
                states.append("focused")
            value = self._pattern(node, UIA_VALUE_PATTERN, uia.IUIAutomationValuePattern)
            control = node.CurrentControlType - 50000
            role = CONTROL_TYPES[control] if 0 <= control < len(CONTROL_TYPES) else "unknown"
            return Node(
                role=role,
                name=node.CurrentName or "",
                states=tuple(states),
                text=value.CurrentValue if value else None,
                x=rect.left,
                y=rect.top,
                width=max(0, rect.right - rect.left),
                height=max(0, rect.bottom - rect.top),
            )
        except (OSError, COMError) as error:
            raise ElementError(f"an element went away while it was read: {error}", "stale") from error

    def press(self, window: Window, node: Any) -> bool:
        invoke = self._pattern(node, UIA_INVOKE_PATTERN, self._uia_module.IUIAutomationInvokePattern)
        if invoke is None:
            return False
        invoke.Invoke()
        return True

    # Clipboard

    def read_clipboard(self) -> str:
        if not self.user32.OpenClipboard(None):
            raise RuntimeError("another app holds the clipboard open")
        try:
            handle = self.user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = self.kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def write_clipboard(self, text: str) -> None:
        """The text is ready before the clipboard is emptied, and a window of ours opens it: with no owner,
        EmptyClipboard leaves SetClipboardData to fail and the user's clipboard gone."""
        data = text.encode("utf-16-le") + b"\0\0"
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        pointer = self.kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            raise MemoryError("no memory for the clipboard text")
        ctypes.memmove(pointer, data, len(data))
        self.kernel32.GlobalUnlock(handle)
        owner = self.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
        try:
            if not self.user32.OpenClipboard(owner):
                raise RuntimeError("another app holds the clipboard open")
            try:
                self.user32.EmptyClipboard()
                if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                    raise RuntimeError("Windows refused the clipboard text")
                handle = None  # the clipboard owns it now
            finally:
                self.user32.CloseClipboard()
        finally:
            if handle:
                self.kernel32.GlobalFree(handle)
            if owner:
                self.user32.DestroyWindow(owner)

    # Processes

    def launch(self, command: list[str]) -> int:
        """Start the app without activating its window, so the user's focus stays where it is."""
        info = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        info.wShowWindow = SW_SHOWNOACTIVATE
        process = subprocess.Popen(
            command,
            startupinfo=info,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        return process.pid

    def family(self, pid: int) -> set[int]:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        parents: dict[int, int] = {}
        try:
            entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
            more = self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                more = self.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(snapshot)
        return descendants(pid, parents)
