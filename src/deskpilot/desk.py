"""The actions an agent takes, shared by the CLI and the MCP server.

Clicks, scrolls and keys go through the deskpilot Hyprland plugin, which leaves the user's pointer and keyboard focus
where they were; the agent cursor overlay shows where clicks land. `take_pointer` instead moves the user's own pointer
and clicks through the uinput device, and `take_focus` focuses the window before its keys.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import keys, screen
from .hypr import AmbiguousWindow, HyprctlError, Hyprland, Window, WindowNotFound
from .overlay import AgentCursor
from .plugin import Plugin, PluginError


class NotVisible(LookupError):
    pass


# Failures an agent should read and act on: a refusal, a missing window, a tool that failed. Anything else is a bug.
EXPECTED = (
    WindowNotFound,
    AmbiguousWindow,
    NotVisible,
    HyprctlError,
    PluginError,
    ValueError,
    TimeoutError,
    RuntimeError,
)

# How long `show` waits for a focused window to come on screen: a workspace switch or a scrolling layout's
# animation has to finish before the window's area is its own.
SHOW_TIMEOUT = 3.0


class Desk:
    def __init__(self, hypr: Hyprland | None = None, input_factory=None, plugin=None, cursor=None):
        self.hypr = hypr or Hyprland()
        self.plugin = plugin or Plugin()
        self.cursor = cursor or AgentCursor()
        self._input_factory = input_factory
        self._input = None

    @property
    def input(self):
        if self._input is None:
            if self._input_factory is None:
                from .uinput import VirtualInput

                self._input_factory = VirtualInput
            self._input = self._input_factory()
        return self._input

    def close(self) -> None:
        self.cursor.close()
        if self._input is not None:
            self._input.close()
            self._input = None

    def windows(self) -> list[Window]:
        return self.hypr.windows()

    def window(self, query: str) -> Window:
        return self.hypr.find(query)

    def focus(self, query: str) -> Window:
        win = self.window(query)
        self.hypr.focus(win)
        return win

    def _on_screen(self, win: Window, show: bool) -> Window:
        """The window, on screen: a screenshot or a click reaches only what is showing. A hidden window is an error
        unless `show`, which focuses it, since that switches the user's workspace or scrolls their layout."""
        if win.visible:
            return win
        if not show:
            raise NotVisible(
                f"{win.cls} ({win.address}) is not on screen: its workspace {win.workspace} is not showing or it is "
                "scrolled off its monitor. Pass --show (show over MCP) to bring it into view, which focuses it"
            )
        self.hypr.focus(win)
        deadline = time.monotonic() + SHOW_TIMEOUT
        while time.monotonic() < deadline:
            win = self.window(win.address)
            if win.visible:
                return win
            time.sleep(0.05)
        raise NotVisible(f"{win.cls} ({win.address}) did not come on screen within {SHOW_TIMEOUT}s of focusing it")

    def shot(
        self, query: str, out: Path | None = None, max_width: int = screen.DEFAULT_MAX_WIDTH, show: bool = False
    ) -> screen.Shot:
        return screen.capture(self._on_screen(self.window(query), show), out, max_width)

    def point(
        self,
        query: str,
        x: float,
        y: float,
        scale: float | None = None,
        shot: str | None = None,
        show: bool = False,
    ) -> tuple[Window, int, int]:
        """Resolve a point to a global position. The point is in pixels of `shot`, or of the window's latest shot,
        or at `scale` (1 for window pixels); with no shot at all it is in window pixels."""
        win = self._on_screen(self.window(query), show)
        if scale is None:
            taken = screen.load(shot) if shot else screen.latest(win.address)
            if taken:
                taken.check(win)
            scale = taken.scale if taken else 1.0
        gx, gy = screen.to_global(win, x, y, scale)
        return win, gx, gy

    def _take_pointer(self, win: Window, gx: int, gy: int) -> None:
        self.hypr.focus(win)
        self.hypr.move_cursor(gx, gy)
        time.sleep(0.15)
        self.input.nudge()

    def move(
        self,
        query: str,
        x: float,
        y: float,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
    ) -> tuple[int, int]:
        """Show the agent cursor at a point. take_pointer also moves the user's pointer, for a hover."""
        win, gx, gy = self.point(query, x, y, scale, shot, show)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
        return gx, gy

    def click(
        self,
        query: str,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
    ) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale, shot, show)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.click(button, count)
        else:
            self.plugin.click(win.address, gx, gy, button, count)
        return gx, gy

    def scroll(
        self,
        query: str,
        x: float,
        y: float,
        dy: int,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
    ) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale, shot, show)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.scroll(dy=dy)
        else:
            self.plugin.scroll(win.address, gx, gy, dy)
        return gx, gy

    def key(self, combo: str, query: str | None = None, take_focus: bool = False) -> None:
        """Press a chord in a window without focusing it, or with take_focus after focusing it. With no window the
        keys go to the focused window."""
        self.plugin.keys(self._key_target(query, take_focus).address, keys.chord_words(combo))

    def type(self, text: str, query: str | None = None, delay: float = 0.03, take_focus: bool = False) -> None:
        """Type text as written, on the user's own keymap. Each key is held `delay` seconds, and the next follows
        `delay` seconds later."""
        target = self._key_target(query, take_focus)
        self.plugin.keys(target.address, keys.text_words(text), hold=delay, gap=delay)

    def _key_target(self, query: str | None, take_focus: bool) -> Window:
        if query is None:
            focused = next((w for w in self.hypr.windows() if w.focused), None)
            if focused is None:
                raise WindowNotFound("no window has keyboard focus; name one with a window query")
            return focused
        return self.focus(query) if take_focus else self.window(query)

    def wait_for(self, query: str, timeout: float = 60, gone: bool = False) -> Window | None:
        """Wait until a window matching `query` exists (or, with gone, no longer does)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                win = self.window(query)
                if not gone:
                    return win
            except WindowNotFound:
                if gone:
                    return None
            if time.monotonic() >= deadline:
                state = "still open" if gone else "did not appear"
                raise TimeoutError(f"window '{query}' {state} within {timeout:g}s")
            time.sleep(0.5)
