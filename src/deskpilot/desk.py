"""The actions an agent takes, shared by the CLI and the MCP server.

Clicks, scrolls and keys go through the deskpilot Hyprland plugin, which leaves the user's pointer and keyboard focus
where they were; the agent cursor overlay shows where clicks land. `take_pointer` instead moves the user's own pointer
and clicks through the uinput device, and `take_focus` focuses the window before its keys.
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageChops

from . import a11y, clipboard, keys, screen
from .hypr import AmbiguousWindow, HyprctlError, Hyprland, Window, WindowNotFound
from .overlay import AgentCursor
from .plugin import BUTTON_CODES, Plugin, PluginError


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

# A drag's pointer path: a point about every frame, so the app sees it travel, and a pause on each side of the press and
# release, as a hand makes, so a slider or a drag threshold sees the press before the motion.
DRAG_STEP = 1 / 60
DRAG_PAUSE_MS = 60
# How long beyond its own wait a hover holds the pointer for its screenshot. The plugin ends the hover when the shot is
# taken; this bounds it if deskpilot dies first.
HOVER_SHOT_MS = 5000


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
        x: float | None,
        y: float | None,
        scale: float | None = None,
        shot: str | None = None,
        show: bool = False,
        element: str | None = None,
    ) -> tuple[Window, int, int]:
        """Resolve a point to a global position. The point is in pixels of `shot`, or of the window's latest shot,
        or at `scale` (1 for window pixels); with no shot at all it is in window pixels. With `element` it is the
        centre of that element of the window's accessibility tree, where the app says it is now."""
        if (element is None) == (x is None or y is None):
            raise ValueError("give a point (x and y) or an element, not both or neither")
        win = self._on_screen(self.window(query), show)
        if element is not None:
            ex, ey, ew, eh = a11y.locate(win, element)
            return win, *screen.to_global(win, ex + ew / 2, ey + eh / 2, 1.0)
        assert x is not None and y is not None
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
        x: float | None,
        y: float | None,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
        element: str | None = None,
    ) -> tuple[int, int]:
        """Show the agent cursor at a point. take_pointer also moves the user's pointer there."""
        win, gx, gy = self.point(query, x, y, scale, shot, show, element)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
        return gx, gy

    def click(
        self,
        query: str,
        x: float | None,
        y: float | None,
        button: str = "left",
        count: int = 1,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
        take_focus: bool = False,
        element: str | None = None,
    ) -> tuple[int, int]:
        """Click a point. take_focus focuses the window first and leaves it focused, for a menu or dropdown that
        closes when its window is not the focused one."""
        if take_focus:
            self.focus(query)
        win, gx, gy = self.point(query, x, y, scale, shot, show, element)
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
        x: float | None,
        y: float | None,
        dy: int,
        scale: float | None = None,
        take_pointer: bool = False,
        *,
        shot: str | None = None,
        show: bool = False,
        element: str | None = None,
    ) -> tuple[int, int]:
        win, gx, gy = self.point(query, x, y, scale, shot, show, element)
        self.cursor.glide(gx, gy)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.scroll(dy=dy)
        else:
            self.plugin.scroll(win.address, gx, gy, dy)
        return gx, gy

    def drag(
        self,
        query: str,
        start: tuple[float, float],
        end: tuple[float, float],
        button: str = "left",
        duration: float = 0.3,
        scale: float | None = None,
        *,
        shot: str | None = None,
        show: bool = False,
    ) -> tuple[int, int]:
        """Press at `start`, move to `end` over `duration` seconds and release there, for a slider, a selection or a
        drag and drop within the window. Both points are in the same shot's pixels. The user's pointer stays put."""
        win, x1, y1 = self.point(query, *start, scale, shot, show)
        _, x2, y2 = self.point(query, *end, scale, shot, show)
        if not 0 < duration <= 5:
            raise ValueError("a drag takes more than 0 and at most 5 seconds")
        moves = max(2, round(duration / DRAG_STEP))
        wait = max(0, round(duration * 1000 / moves) - 8)
        code = BUTTON_CODES[button]
        steps = [f"m{x1},{y1}", f"d{DRAG_PAUSE_MS}", f"p{code}", f"d{DRAG_PAUSE_MS}"]
        for i in range(1, moves + 1):
            steps += [f"m{x1 + (x2 - x1) * i / moves:g},{y1 + (y2 - y1) * i / moves:g}", f"d{wait}"]
        steps += [f"d{DRAG_PAUSE_MS}", f"r{code}"]
        self.cursor.glide(x1, y1)
        self.plugin.path(win.address, steps)
        self.cursor.glide(x2, y2)
        return x2, y2

    def hover(
        self,
        query: str,
        x: float | None,
        y: float | None,
        wait: float = 1.0,
        scale: float | None = None,
        *,
        shot: str | None = None,
        show: bool = False,
        element: str | None = None,
        then_click: tuple[float, float] | None = None,
        button: str = "left",
        max_width: int = screen.DEFAULT_MAX_WIDTH,
    ) -> screen.Shot | tuple[int, int]:
        """Rest the pointer on a point for `wait` seconds without moving the user's, for a tooltip or a menu that
        opens on hover. Returns a screenshot taken while it rests there. With `then_click`, a point in the same
        pixels, it clicks there next instead, before the pointer leaves, and returns that point."""
        if not 0 <= wait <= 5:
            raise ValueError("a hover waits 0 to 5 seconds")
        win, gx, gy = self.point(query, x, y, scale, shot, show, element)
        self.cursor.glide(gx, gy)
        if then_click is not None:
            _, cx, cy = self.point(query, *then_click, scale, shot, show)
            code = BUTTON_CODES[button]
            steps = [f"m{gx},{gy}", f"d{round(wait * 1000)}", f"m{cx},{cy}", f"p{code}", f"r{code}"]
            self.plugin.path(win.address, steps)
            self.cursor.glide(cx, cy)
            return cx, cy
        reply = self.plugin.path(win.address, [f"m{gx},{gy}", "n", f"d{round(wait * 1000) + HOVER_SHOT_MS}"])
        try:
            time.sleep(wait)
            return screen.capture(win, max_width=max_width)
        finally:
            self.plugin.end(reply["ticket"])

    def elements(self, query: str, show: bool = False) -> a11y.Element:
        """The window's accessibility tree, whose element ids click, hover, move and scroll take."""
        return a11y.snapshot(self._on_screen(self.window(query), show))

    def key(self, combo: str, query: str | None = None, take_focus: bool = False, hold: float = 0.0) -> None:
        """Press a chord in a window without focusing it, or with take_focus after focusing it. With no window the
        keys go to the focused window. Each key is held `hold` seconds, up to 10, for an app that acts while a key is
        down (auto-repeat, a game's movement)."""
        if not 0 <= hold <= 10:
            raise ValueError("a key is held 0 to 10 seconds")
        self.plugin.keys(self._key_target(query, take_focus).address, keys.chord_words(combo), hold=hold)

    def type(self, text: str, query: str | None = None, delay: float = 0.03, take_focus: bool = False) -> None:
        """Type text as written, on the user's own keymap. Each key is held `delay` seconds, and the next follows
        `delay` seconds later."""
        target = self._key_target(query, take_focus)
        self.plugin.keys(target.address, keys.text_words(text), hold=delay, gap=delay)

    def paste(self, text: str, query: str | None = None, take_focus: bool = False, combo: str = "ctrl+v") -> None:
        """Paste text into a window: it becomes that window's clipboard alone, never the user's, and `combo` (the app's
        paste chord, ctrl+shift+v in most terminals) is pressed there. Any text pastes, whatever the keymap types."""
        target = self._key_target(query, take_focus)
        self.plugin.keys(target.address, keys.chord_words(combo), paste=text)

    def clipboard(self) -> str:
        """The user's clipboard text."""
        return clipboard.read()

    def set_clipboard(self, text: str) -> None:
        """Replace the user's clipboard with text."""
        clipboard.write(text)

    def _key_target(self, query: str | None, take_focus: bool) -> Window:
        if query is None:
            focused = next((w for w in self.hypr.windows() if w.focused), None)
            if focused is None:
                raise WindowNotFound("no window has keyboard focus; name one with a window query")
            return focused
        return self.focus(query) if take_focus else self.window(query)

    def wait_for(
        self,
        query: str,
        timeout: float = 60,
        gone: bool = False,
        focused: bool = False,
        title: str | None = None,
        size: tuple[int, int] | None = None,
    ) -> Window | None:
        """Wait until a window matching `query` exists (or, with gone, no longer does), and also has keyboard focus,
        a title containing `title` or the size `size` when asked. Returns the window as it was when it matched."""
        checks = [
            (what, check)
            for wanted, what, check in (
                (focused, "focused", lambda w: w.focused),
                (title is not None, f"titled with {title!r}", lambda w: title in w.title),
                (size is not None, "x".join(map(str, size or ())), lambda w: (w.width, w.height) == size),
            )
            if wanted
        ]
        deadline = time.monotonic() + timeout
        while True:
            try:
                win = self.window(query)
            except WindowNotFound:
                win = None
            if gone and win is None:
                return None
            unmet = [what for what, check in checks if win and not check(win)]
            if not gone and win and not unmet:
                return win
            if time.monotonic() >= deadline:
                if gone:
                    raise TimeoutError(f"window '{query}' still open within {timeout:g}s")
                if win is None:
                    raise TimeoutError(f"window '{query}' did not appear within {timeout:g}s")
                raise TimeoutError(f"window '{query}' is not {' and '.join(unmet)} after {timeout:g}s")
            time.sleep(0.5)

    def wait_changed(
        self, query: str, shot: str, timeout: float = 60, region: tuple[int, int, int, int] | None = None
    ) -> tuple[screen.Shot, tuple[int, int, int, int]]:
        """Wait until the window looks different from `shot`, in all of it or in `region` (x, y, width, height in
        that shot's pixels). Returns a shot of the change and the box that changed in its pixels. A pixel counts as
        changed when it differs at all; a window resized since the shot has changed everywhere."""
        win = self._on_screen(self.window(query), False)
        base = screen.load(shot)
        if win.address != base.address:
            raise screen.StaleShot(f"shot {shot} is of window {base.address}, not {win.address}")
        x, y, w, h = region or (0, 0, base.width, base.height)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > base.width or y + h > base.height:
            raise ValueError(f"region {x},{y} {w}x{h} is not inside shot {shot} ({base.width}x{base.height})")
        box = (x, y, x + w, y + h)
        with Image.open(base.path) as image:
            before = image.convert("RGB").crop(box)
        deadline = time.monotonic() + timeout
        while True:
            win = self._on_screen(self.window(query), False)
            now = screen.capture(win, max_width=base.width)
            # A resize, or a move to a monitor of another scale, changes the whole image.
            if (now.width, now.height) != (base.width, base.height):
                return now, (0, 0, now.width, now.height)
            with Image.open(now.path) as image:
                diff = ImageChops.difference(before, image.convert("RGB").crop(box)).getbbox()
            if diff:
                return now, (x + diff[0], y + diff[1], x + diff[2], y + diff[3])
            screen.discard(now)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{win.cls} did not change from shot {shot} within {timeout:g}s")
            time.sleep(min(0.5, remaining))
