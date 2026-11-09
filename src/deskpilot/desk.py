"""The actions an agent takes, shared by the CLI and the MCP server.

Clicks and scrolls go through the deskpilot Hyprland plugin, which leaves the user's pointer and keyboard focus
where they were; the agent cursor overlay shows where they land. `take_pointer` instead moves the user's own pointer
and presses through the uinput device, for a hover the app must see or a machine without the plugin.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import screen
from .hypr import Hyprland, Window, WindowNotFound
from .overlay import AgentCursor
from .plugin import Plugin


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

    def _shown(self, win: Window) -> Window:
        """The window, with its workspace showing: a screenshot or a click reaches only what is on screen."""
        if self.hypr.visible(win):
            return win
        self.hypr.focus(win)
        time.sleep(0.4)
        return self.window(win.address)

    def shot(self, query: str, out: Path | None = None, max_width: int = screen.DEFAULT_MAX_WIDTH) -> screen.Shot:
        return screen.capture(self._shown(self.window(query)), out, max_width)

    def point(self, query: str, x: float, y: float, scale: float | None = None) -> tuple[Window, int, int]:
        """Resolve a point in the window's last screenshot (or at `scale`) to a global position."""
        win = self._shown(self.window(query))
        used = screen.last_scale(win.address) if scale is None else scale
        gx, gy = screen.to_global(win, x, y, used)
        return win, gx, gy

    def _take_pointer(self, win: Window, gx: int, gy: int) -> None:
        self.hypr.focus(win)
        self.hypr.move_cursor(gx, gy)
        time.sleep(0.15)
        self.input.nudge()

    def move(
        self, query: str, x: float, y: float, scale: float | None = None, take_pointer: bool = False
    ) -> tuple[int, int]:
        """Show the agent cursor at a point. take_pointer also moves the user's pointer, for a hover."""
        win, gx, gy = self.point(query, x, y, scale)
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
    ) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.click(button, count)
        else:
            self.plugin.click(win.address, gx, gy, button, count)
        return gx, gy

    def scroll(
        self, query: str, x: float, y: float, dy: int, scale: float | None = None, take_pointer: bool = False
    ) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.scroll(dy=dy)
        else:
            self.plugin.scroll(win.address, gx, gy, dy)
        return gx, gy

    def key(self, combo: str, query: str | None = None) -> None:
        if query:
            self.focus(query)
            time.sleep(0.1)
        self.input.chord(combo)

    def type(self, text: str, query: str | None = None, delay: float = 0.03) -> None:
        if query:
            self.focus(query)
            time.sleep(0.1)
        self.input.type_text(text, delay)

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
