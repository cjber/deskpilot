"""What every backend shares: batches, and waiting for a window or a change in it. Each backend supplies `window`,
`_target`, `_shootable` and `_capture`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from PIL import Image, ImageChops

from . import screen
from .hypr import Window, WindowNotFound

if TYPE_CHECKING:
    from .batch import BatchRequest, BatchResult
    from .desk import Desk
    from .native import NativeDesk


class BaseDesk:
    def window(self, query: str) -> Window:
        raise NotImplementedError

    def _target(self, query: str, show: bool, hidden: bool, refresh: bool = False) -> Window:
        raise NotImplementedError

    def _shootable(self, win: Window) -> Window:
        raise NotImplementedError

    def _capture(self, win: Window, max_width: int) -> screen.Shot:
        raise NotImplementedError

    def batch(self, request: BatchRequest) -> BatchResult:
        """Run a validated batch of actions on one window in order, stopping at the first expected failure (see
        `batch`)."""
        from . import batch

        return batch.run(cast("Desk | NativeDesk", self), request)

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
        self,
        query: str,
        shot: str,
        timeout: float = 60,
        region: tuple[int, int, int, int] | None = None,
        hidden: bool = False,
    ) -> tuple[screen.Shot, tuple[int, int, int, int]]:
        """Wait until the window looks different from `shot`, in all of it or in `region` (x, y, width, height in
        that shot's pixels). Returns a shot of the change and the box that changed in its pixels. A pixel counts as
        changed when it differs at all; a window resized since the shot has changed everywhere."""
        win = self._target(query, False, hidden)
        base = screen.load(shot)
        if win.address != base.address:
            raise screen.StaleShot(f"shot {shot} is of window {base.address}, not {win.address}")
        base.check_identity(win)
        x, y, w, h = region or (0, 0, base.width, base.height)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > base.width or y + h > base.height:
            raise ValueError(f"region {x},{y} {w}x{h} is not inside shot {shot} ({base.width}x{base.height})")
        box = (x, y, x + w, y + h)
        with Image.open(base.path) as image:
            before = image.convert("RGB").crop(box)
        deadline = time.monotonic() + timeout
        while True:
            win = self._target(query, False, hidden)
            now = self._capture(self._shootable(win), base.width)
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
