"""A virtual mouse through /dev/uinput, behind `--take-pointer`.

The compositor sees the device as real hardware, so its clicks reach every client, XWayland and Wine included, where
XTest (`xdotool`) can be ignored. Pointer position is set by the compositor (see hypr.Hyprland.move_cursor); this
device only presses buttons, scrolls and nudges.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

BUTTONS = {"left": "BTN_LEFT", "right": "BTN_RIGHT", "middle": "BTN_MIDDLE"}
# libinput adds a new device asynchronously; events sent before it is added are lost.
SETTLE_SECONDS = 0.8


class VirtualInput:
    def __init__(self, settle: float = SETTLE_SECONDS):
        from evdev import UInput
        from evdev import ecodes as e

        self._e = e
        key_codes = sorted(getattr(e, name) for name in BUTTONS.values())
        caps: dict[int, Sequence[int]] = {e.EV_KEY: key_codes, e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]}
        self._ui = UInput(caps, name="deskpilot")
        time.sleep(settle)

    def close(self) -> None:
        self._ui.close()

    def __enter__(self) -> VirtualInput:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _emit(self, etype: int, code: int, value: int) -> None:
        self._ui.write(etype, code, value)
        self._ui.syn()

    def nudge(self) -> None:
        """A one-pixel move and back, so the client under the cursor sees pointer motion from this device."""
        e = self._e
        self._emit(e.EV_REL, e.REL_X, 1)
        time.sleep(0.03)
        self._emit(e.EV_REL, e.REL_X, -1)
        time.sleep(0.05)

    def button(self, name: str, down: bool) -> None:
        self._emit(self._e.EV_KEY, getattr(self._e, BUTTONS[name]), 1 if down else 0)

    def click(self, name: str = "left", count: int = 1, hold: float = 0.06) -> None:
        for i in range(count):
            self.button(name, True)
            time.sleep(hold)
            self.button(name, False)
            if i + 1 < count:
                time.sleep(0.08)

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        e = self._e
        for _ in range(abs(dy)):
            self._emit(e.EV_REL, e.REL_WHEEL, 1 if dy > 0 else -1)
            time.sleep(0.03)
        for _ in range(abs(dx)):
            self._emit(e.EV_REL, e.REL_HWHEEL, 1 if dx > 0 else -1)
            time.sleep(0.03)
