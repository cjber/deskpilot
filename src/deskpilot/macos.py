"""The macOS driver: Quartz events posted to the target app's process, so the user's pointer and focus stay theirs.

Coordinates are global points with the origin at the top left of the main display, as Quartz reports windows. A shot is
in image pixels, so on a Retina display its scale is 2 and points resolve back through it like any other shot.
Accessibility (for events and the AX tree) and Screen Recording (for window titles and shots) are permissions the user
grants the app running deskpilot; without them actions fail as `unavailable`, naming the System Settings pane.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .errors import Unavailable
from .hypr import Window, WindowNotFound
from .native import ElementError, Node, descendants

INSTANCE = "macos"

# kVK_ codes from HIToolbox/Events.h for keys.NAMED and the modifiers.
KEYCODES = {
    "esc": 53, "escape": 53, "backspace": 51, "tab": 48, "return": 36, "enter": 36, "space": 49,
    "ctrl": 59, "control": 59, "shift": 56, "alt": 58, "super": 55, "meta": 55, "win": 55, "cmd": 55, "command": 55,
    "capslock": 57, "delete": 117, "del": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "up": 126, "down": 125, "left": 123, "right": 124, "insert": 114,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}  # fmt: skip
# kCGEventFlagMask* for each modifier.
FLAGS = {
    "shift": 1 << 17, "ctrl": 1 << 18, "control": 1 << 18, "alt": 1 << 19,
    "super": 1 << 20, "meta": 1 << 20, "win": 1 << 20, "cmd": 1 << 20, "command": 1 << 20,
}  # fmt: skip
# Mouse event fields from CGEventTypes.h: the window under the pointer, so an app that is not frontmost routes the
# event to the right window.
WINDOW_UNDER_POINTER = 91
WINDOW_THAT_CAN_HANDLE = 92
CLICK_STATE = 1
# The private fields the WindowServer stamps on an event it routes to a window: 51 holds the window number and 58
# marks it valid. An event posted to a pid skips that routing, and without them AppKit delivers it with no window
# (windowNumber 0), so no view ever receives it. Warp and Cua post background clicks the same way.
WINDOW_NUMBER = 51
WINDOW_NUMBER_VALID = 58
# How long focus() may take, from its first call to the app until the window has keyboard focus. Activation usually
# lands within a frame or two; a busy app can take longer, and past this focus() fails rather than wait on a hung app.
FOCUS_TIMEOUT = 2.0
# How long one accessibility call may wait on an app. AX's default is 6 seconds per call, so a hung app held focus()
# and each key's focused-window check far past FOCUS_TIMEOUT.
AX_TIMEOUT = 1.0


class _Point(ctypes.Structure):
    _fields_ = (("x", ctypes.c_double), ("y", ctypes.c_double))


def _window_location_setter() -> Any:
    """CoreGraphics' private CGEventSetWindowLocation, which stamps the point within the window that AppKit reads as
    the event's locationInWindow; a posted event otherwise carries none."""
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    setter = getattr(graphics, "CGEventSetWindowLocation", None)
    if setter is None:
        raise Unavailable("this macOS has no CGEventSetWindowLocation, so input posted to a window cannot reach it")
    setter.argtypes = (ctypes.c_void_p, _Point)
    setter.restype = None
    return setter


def _window_id_getter() -> Any:
    """HIServices' private _AXUIElementGetWindow, the window number of an AX window. AX has no public window number,
    and matching a window by title and frame picked the wrong one of two alike, or a floating panel over the key
    window."""
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    getter = getattr(services, "_AXUIElementGetWindow", None)
    if getter is None:
        raise Unavailable("this macOS has no _AXUIElementGetWindow, so deskpilot cannot tell which window has focus")
    getter.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    getter.restype = ctypes.c_int32
    return getter


SETTINGS = "System Settings > Privacy & Security"


def modifier_flags(held: frozenset[str]) -> int:
    flags = 0
    for key in held:
        flags |= FLAGS.get(key, 0)
    return flags


class MacDriver:
    instance = INSTANCE

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise Unavailable("the macOS driver runs only on macOS")
        import AppKit
        import ApplicationServices
        import Quartz

        self.q: Any = Quartz
        self.ax: Any = ApplicationServices
        self.appkit: Any = AppKit
        self._set_window_location: Any = None
        self._get_window_id: Any = None
        # Set on the system-wide element, the timeout applies to every AX call this process makes.
        ApplicationServices.AXUIElementSetMessagingTimeout(ApplicationServices.AXUIElementCreateSystemWide(),
                                                           AX_TIMEOUT)  # fmt: skip

    def _trusted(self) -> None:
        if not self.ax.AXIsProcessTrusted():
            raise Unavailable(
                f"macOS has not given this app Accessibility access: allow it in {SETTINGS} > Accessibility, then run "
                "deskpilot again"
            )

    # Windows

    def windows(self) -> list[Window]:
        """Normal app windows, front to back, as Quartz lists them; a minimized window is listed as not visible."""
        q = self.q
        info = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll | q.kCGWindowListExcludeDesktopElements,
                                            q.kCGNullWindowID) or []  # fmt: skip
        focused_number = self._focused_window()[1]
        found: list[Window] = []
        for entry in info:
            # App window levels run from normal (0) through floating, modal panel and utility windows (19, where a
            # Tk -topmost window sits); the Dock (20), menu bar and desktop sit outside that range.
            if not 0 <= entry.get(q.kCGWindowLayer, -1) <= q.kCGUtilityWindowLevel or not entry.get(
                q.kCGWindowAlpha, 1
            ):
                continue
            bounds = entry.get(q.kCGWindowBounds) or {}
            width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
            if width < 2 or height < 2:
                continue
            onscreen = bool(entry.get(q.kCGWindowIsOnscreen, False))
            title = entry.get(q.kCGWindowName) or ""
            if not onscreen and not title:
                continue
            pid = int(entry.get(q.kCGWindowOwnerPID, 0))
            number = int(entry[q.kCGWindowNumber])
            found.append(
                Window(
                    address=hex(number),
                    cls=entry.get(q.kCGWindowOwnerName) or "",
                    title=title,
                    x=int(bounds.get("X", 0)),
                    y=int(bounds.get("Y", 0)),
                    width=width,
                    height=height,
                    monitor=0,
                    workspace="",
                    xwayland=False,
                    pid=pid,
                    focused=number == focused_number,
                    instance=INSTANCE,
                    stable_id=str(number),
                    visible=onscreen,
                )
            )
        return found

    def locked(self) -> bool:
        session = self.q.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked", False))

    def _window_number(self, element: Any) -> int | None:
        if self._get_window_id is None:
            self._get_window_id = _window_id_getter()
        number = ctypes.c_uint32(0)
        error = self._get_window_id(element.__c_void_p__(), ctypes.byref(number))
        return number.value if error == 0 and number.value else None

    def _focused_window(self) -> tuple[int, int | None]:
        """The app with keyboard focus and the number of its focused window, read live from the accessibility
        server: the window keys go to, not the topmost one, which may be a floating panel over it. NSWorkspace's
        frontmostApplication is updated only on a run loop this process never runs, so it kept naming the app that
        was in front at the first call."""
        app = self._attr(self.ax.AXUIElementCreateSystemWide(), "AXFocusedApplication")
        if app is None:
            return -1, None
        error, pid = self.ax.AXUIElementGetPid(app, None)
        focused = self._attr(app, "AXFocusedWindow")
        return (pid if error == 0 else -1), (None if focused is None else self._window_number(focused))

    def focus(self, window: Window) -> None:
        """Raise the window, activate its app, and return once that window has keyboard focus; fail otherwise, as
        when it is on another Space, since seat input that follows would go to whatever does have it."""
        self._trusted()
        deadline = time.monotonic() + FOCUS_TIMEOUT
        app = self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(window.pid)
        if app is None:
            raise RuntimeError(f"{window.cls} ({window.address}) has quit")
        ax = self._ax_window(window)
        if ax is None:
            raise Unavailable(
                f"{window.cls} lists no accessibility window {window.address} on this Space, so deskpilot cannot "
                "focus it: switch to its Space first"
            )
        if self._attr(ax, "AXMinimized"):
            self.ax.AXUIElementSetAttributeValue(ax, "AXMinimized", False)
        # Main before activation, so the app makes this window key rather than the one it last had.
        self.ax.AXUIElementSetAttributeValue(ax, "AXMain", True)
        raised = self.ax.AXUIElementPerformAction(ax, "AXRaise")
        if raised != 0:
            raise Unavailable(f"macOS would not raise {window.cls} ({window.address}): AX error {raised}")
        if not app.activateWithOptions_(self.appkit.NSApplicationActivateIgnoringOtherApps):
            raise Unavailable(f"macOS would not activate {window.cls} ({window.address})")
        # Activation is asynchronous: wait until this window has keyboard focus, so seat input that follows reaches
        # it.
        target = int(window.address, 16)
        while self._focused_window() != (window.pid, target):
            if time.monotonic() > deadline:
                raise Unavailable(
                    f"{window.cls} ({window.address}) did not take keyboard focus within {FOCUS_TIMEOUT:g} s; another "
                    "of its windows or another app kept it"
                )
            time.sleep(0.02)

    def cursor(self) -> tuple[int, int]:
        point = self.q.CGEventGetLocation(self.q.CGEventCreate(None))
        return round(point.x), round(point.y)

    def top_at(self, x: int, y: int) -> str | None:
        """The window a click at this point would reach, as the accessibility hit test finds it, so a window that
        lets clicks through (the Dock's full-screen layer) never counts as covering the point. Quartz lists window
        rectangles only, and AppKit's own hit test answers only inside a running app."""
        self._trusted()
        system = self.ax.AXUIElementCreateSystemWide()
        error, element = self.ax.AXUIElementCopyElementAtPosition(system, float(x), float(y), None)
        if error != 0 or element is None:
            return None
        _, pid = self.ax.AXUIElementGetPid(element, None)
        found = element if self._attr(element, "AXRole") == "AXWindow" else self._attr(element, "AXWindow")
        number = None if found is None else self._window_number(found)
        return f"pid {pid}" if number is None else hex(number)

    # Capture

    def grab(self, window: Window, path: Path) -> None:
        if not self.q.CGPreflightScreenCaptureAccess():
            raise Unavailable(
                f"macOS has not given this app Screen Recording access: allow it in {SETTINGS} > Screen & System Audio "
                "Recording, then run deskpilot again"
            )
        number = str(int(window.address, 16))
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-o", f"-l{number}", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            raise Unavailable(
                f"macOS gave no image of {window.cls} ({window.address}): allow Screen Recording for this app in "
                f"{SETTINGS} > Screen & System Audio Recording. {result.stderr.strip()}"
            )

    # Input

    def _post(self, window: Window, event: Any, seat: bool) -> None:
        # macOS drops posted events without a word when this app may not post them, so check first.
        if not self.q.CGPreflightPostEventAccess():
            raise Unavailable(
                f"macOS has not given this app permission to send input: allow it in {SETTINGS} > Accessibility, "
                "then run deskpilot again"
            )
        if seat:
            self.q.CGEventPost(self.q.kCGHIDEventTap, event)
        else:
            self.q.CGEventPostToPid(window.pid, event)

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
        self._trusted()
        q = self.q
        if event == "wheel":
            made = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, dy)
            q.CGEventSetLocation(made, (x, y))
        else:
            kinds = {
                "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGEventLeftMouseDragged,
                         q.kCGMouseButtonLeft),
                "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGEventRightMouseDragged,
                          q.kCGMouseButtonRight),
                "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGEventOtherMouseDragged,
                           q.kCGMouseButtonCenter),
            }  # fmt: skip
            pressed = next(iter(held), None)
            if event == "move":
                kind = kinds[pressed][2] if pressed else q.kCGEventMouseMoved
                code = kinds[pressed][3] if pressed else q.kCGMouseButtonLeft
            else:
                down, up, _, code = kinds[button]
                kind = down if event == "down" else up
            made = q.CGEventCreateMouseEvent(None, kind, (x, y), code)
            if event in ("down", "up"):
                q.CGEventSetIntegerValueField(made, CLICK_STATE, clicks)
        if not seat:
            self._address(made, window, x, y)
        self._post(window, made, seat)

    def _address(self, made: Any, window: Window, x: int, y: int) -> None:
        """Stamp what the WindowServer would on an event it routed to this window, since a posted one skips that."""
        q = self.q
        number = int(window.address, 16)
        for field, value in ((WINDOW_UNDER_POINTER, number), (WINDOW_THAT_CAN_HANDLE, number),
                             (WINDOW_NUMBER, number), (WINDOW_NUMBER_VALID, 1),
                             (q.kCGEventTargetUnixProcessID, window.pid)):  # fmt: skip
            q.CGEventSetIntegerValueField(made, field, value)
        if self._set_window_location is None:
            self._set_window_location = _window_location_setter()
        # Window-local, top-left origin, as the window's frame is listed.
        self._set_window_location(made.__c_void_p__(), _Point(x - window.x, y - window.y))

    def _keys_reach(self, window: Window) -> None:
        """Keys posted to a process go to its focused window, whichever that is: refuse unless it is this one, so
        typing never edits another document of the same app."""
        focused = self._attr(self.ax.AXUIElementCreateApplication(window.pid), "AXFocusedWindow")
        if focused is None or self._window_number(focused) != int(window.address, 16):
            raise RuntimeError(  # refused
                f"{window.cls}'s focused window is not {window.address}, so keys would go to another of its windows: "
                "click in this one first, or pass take_focus"
            )

    # Keys go to the target's process even as seat input (take_focus). Posted to the HID stream, a key reaches
    # whichever app is in front when it lands, and the user can switch apps between the check that the target is in
    # front and the post. With take_focus the app is active, so its key window gets the key as typed there.

    def key(self, window: Window, key: str, down: bool, *, held: frozenset[str], seat: bool = False) -> None:
        self._trusted()
        if down:
            self._keys_reach(window)
        q = self.q
        code = KEYCODES.get(key)
        if code is None:
            made = q.CGEventCreateKeyboardEvent(None, 0, down)
            q.CGEventKeyboardSetUnicodeString(made, len(key.encode("utf-16-le")) // 2, key)
        else:
            made = q.CGEventCreateKeyboardEvent(None, code, down)
        q.CGEventSetFlags(made, modifier_flags(held | ({key} if down and key in FLAGS else set())))
        self._post(window, made, seat=False)

    def text(self, window: Window, char: str, *, seat: bool = False) -> None:
        self._trusted()
        self._keys_reach(window)
        q = self.q
        if char in ("\n", "\r"):
            for down in (True, False):
                self._post(window, q.CGEventCreateKeyboardEvent(None, KEYCODES["return"], down), seat=False)
            return
        units = len(char.encode("utf-16-le")) // 2
        for down in (True, False):
            made = q.CGEventCreateKeyboardEvent(None, 0, down)
            q.CGEventKeyboardSetUnicodeString(made, units, char)
            q.CGEventSetFlags(made, 0)
            self._post(window, made, seat=False)

    # Accessibility

    def _attr(self, element: Any, name: str) -> Any:
        error, value = self.ax.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == 0 else None

    def _ax_window(self, window: Window) -> Any:
        """The app's AX window with this window's number. AX lists only the windows on the current Space."""
        app = self.ax.AXUIElementCreateApplication(window.pid)
        target = int(window.address, 16)
        return next((w for w in self._attr(app, "AXWindows") or [] if self._window_number(w) == target), None)

    def root(self, window: Window) -> object:
        self._trusted()
        found = self._ax_window(window)
        if found is None:
            raise ElementError(f"{window.cls} ({window.address}) exposes no accessibility window with its frame",
                               "no_tree")  # fmt: skip
        return found

    def press(self, window: Window, node: Any) -> bool:
        error, actions = self.ax.AXUIElementCopyActionNames(node, None)
        if error != 0 or "AXPress" not in (actions or []):
            return False
        return self.ax.AXUIElementPerformAction(node, "AXPress") == 0

    def children(self, node: object) -> list[object]:
        return list(self._attr(node, "AXChildren") or [])

    def describe(self, node: Any) -> Node:
        ax = self.ax
        position = self._attr(node, "AXPosition")
        size = self._attr(node, "AXSize")
        x = y = width = height = 0
        if position is not None:
            _, point = ax.AXValueGetValue(position, ax.kAXValueCGPointType, None)
            x, y = round(point.x), round(point.y)
        if size is not None:
            _, extent = ax.AXValueGetValue(size, ax.kAXValueCGSizeType, None)
            width, height = round(extent.width), round(extent.height)
        states = []
        if self._attr(node, "AXEnabled") is False:
            states.append("disabled")
        if self._attr(node, "AXFocused"):
            states.append("focused")
        value = self._attr(node, "AXValue")
        role = (self._attr(node, "AXRole") or "").removeprefix("AX")
        name = self._attr(node, "AXTitle") or self._attr(node, "AXDescription") or ""
        return Node(str(role), str(name), tuple(states), value if isinstance(value, str) else None, x, y, width, height)

    # Clipboard

    def read_clipboard(self) -> str:
        return subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True, check=True).stdout

    def write_clipboard(self, text: str) -> None:
        subprocess.run(["/usr/bin/pbcopy"], input=text, text=True, check=True)

    # Processes

    def launch(self, command: list[str]) -> int:
        """Start the app in the background, so the user's focus stays where it is: an app bundle or an app name that
        is not a command on PATH opens with `open -g`, anything else runs as a process."""
        if not (command[0].endswith(".app") or shutil.which(command[0]) is None):
            return subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).pid
        workspace = self.appkit.NSWorkspace.sharedWorkspace()
        path = command[0] if command[0].endswith(".app") else workspace.fullPathForApplication_(command[0])
        bundle = self.appkit.NSBundle.bundleWithPath_(path) if path else None
        if bundle is None or not bundle.bundleIdentifier():
            raise WindowNotFound(f"no app named {command[0]!r} is installed")
        ident = bundle.bundleIdentifier()
        running = self.appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_
        before = {app.processIdentifier() for app in running(ident)}
        args = ["--args", *command[1:]] if command[1:] else []
        subprocess.run(["/usr/bin/open", "-g", "-n", "-a", path, *args], check=True, capture_output=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # A new process of this app's bundle alone, so an unrelated app starting meanwhile is never taken.
            for app in running(ident):
                if app.processIdentifier() not in before:
                    return int(app.processIdentifier())
            time.sleep(0.1)
        raise RuntimeError(f"{command[0]} was already running or did not start; deskpilot waits for a new process")

    def family(self, pid: int) -> set[int]:
        out = subprocess.run(["/bin/ps", "-Ao", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
        parents = {int(a): int(b) for a, b in (line.split() for line in out.splitlines() if line.strip())}
        return descendants(pid, parents)
