"""The actions an agent takes, shared by the CLI and the MCP server."""

from __future__ import annotations

import time
from pathlib import Path

from . import screen
from .hypr import Hyprland, Window, WindowNotFound


class Desk:
    def __init__(self, hypr: Hyprland | None = None, input_factory=None):
        self.hypr = hypr or Hyprland()
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

    def shot(self, query: str, out: Path | None = None, max_width: int = screen.DEFAULT_MAX_WIDTH) -> screen.Shot:
        win = self.window(query)
        if not self.hypr.visible(win):
            # grim captures the screen, not the window: show the window's workspace first.
            self.hypr.focus(win)
            time.sleep(0.4)
            win = self.window(win.address)
        return screen.capture(win, out, max_width)

    def point(self, query: str, x: float, y: float, scale: float | None = None) -> tuple[Window, int, int]:
        """Resolve a point in the window's last screenshot (or at `scale`) to a global position."""
        win = self.window(query)
        used = screen.last_scale(win.address) if scale is None else scale
        gx, gy = screen.to_global(win, x, y, used)
        return win, gx, gy

    def move(self, query: str, x: float, y: float, scale: float | None = None) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale)
        self.hypr.focus(win)
        self.hypr.move_cursor(gx, gy)
        time.sleep(0.15)
        self.input.nudge()
        return gx, gy

    def click(
        self, query: str, x: float, y: float, button: str = "left", count: int = 1, scale: float | None = None
    ) -> tuple[int, int]:
        gx, gy = self.move(query, x, y, scale)
        self.input.click(button, count)
        return gx, gy

    def scroll(self, query: str, x: float, y: float, dy: int, scale: float | None = None) -> tuple[int, int]:
        gx, gy = self.move(query, x, y, scale)
        self.input.scroll(dy=dy)
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
