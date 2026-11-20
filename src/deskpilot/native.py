"""The actions an agent takes on Windows and macOS, over a small per-platform `Driver`.

The driver sends each pointer and key event to the target window's own process (posted window messages on Windows,
events posted to the process on macOS), so the user's pointer stays where it is and their keyboard focus stays in
their window. Only `take_pointer` and `take_focus` use the system's input path, which moves the user's pointer or
focus as the flag says. Neither platform gives an agent a desktop of its own, so hidden actions, adoption and private
paste are refused as `unavailable` rather than run where the user would see or feel them.

Shots, points and element ids follow the same rules as on Hyprland: a point is resolved against a recorded shot of one
window at one size, and an element is found again in the app's tree when the action runs, refused if it changed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn, Protocol

from . import screen
from .a11y import Element
from .base import BaseDesk
from .errors import Covered, NotVisible, SessionLocked, Unavailable
from .hypr import Window, WindowNotFound, find_window
from .keys import NAMED

BUTTONS = ("left", "right", "middle")
# The same timings as the Hyprland path (see desk.py): a rest before a press, a pause around a drag's press and
# release, and a point about every frame of a drag.
HOLD = 0.06
DRAG_STEP = 1 / 60
DRAG_PAUSE = 0.06
SHOW_TIMEOUT = 3.0
LAUNCH_TIMEOUT = 30.0
MAX_DEPTH = 30
MAX_NODES = 2000


@dataclass(frozen=True)
class Node:
    """One element as the platform's accessibility API reports it now. Bounds are global, in the same units as
    `Window` positions."""

    role: str
    name: str
    states: tuple[str, ...]
    text: str | None
    x: int
    y: int
    width: int
    height: int


class ElementError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class Driver(Protocol):
    """One platform's window list, capture, input and accessibility tree. Every input call names its target window;
    `seat` is set only for take_pointer and take_focus, where the event goes through the system's own input path; macOS
    keys still go to the target's process, which is in front by then, so a key never lands in an app the user switched
    to meanwhile."""

    instance: str

    def windows(self) -> list[Window]: ...
    def locked(self) -> bool: ...
    def grab(self, window: Window, path: Path) -> None: ...
    def focus(self, window: Window) -> None: ...
    def cursor(self) -> tuple[int, int]: ...
    def top_at(self, x: int, y: int) -> str | None:
        """The address of the top-level window that gets input at global (x, y), menus and dialogs included."""
        ...

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
        """`event` is move, down, up or wheel at global (x, y). `held` is the buttons down during a move, `clicks` the
        press's place in a multi-click, `dy` wheel notches, positive up."""
        ...

    def key(self, window: Window, key: str, down: bool, *, held: frozenset[str], seat: bool = False) -> None:
        """Press or release a named key (keys.NAMED) or the key of one character, with the modifiers in `held`."""
        ...

    def text(self, window: Window, char: str, *, seat: bool = False) -> None:
        """Type one character as itself, whatever key would give it."""
        ...

    def press(self, window: Window, node: object) -> bool:
        """Activate an element through the accessibility API (UIA Invoke, AX Press), as a left click on it would.
        False when it offers no such action, so the click goes to its centre instead."""
        ...

    def root(self, window: Window) -> object:
        """The window's accessibility root. An app with no tree raises ElementError with code no_tree."""
        ...

    def children(self, node: object) -> list[object]: ...
    def describe(self, node: object) -> Node: ...
    def read_clipboard(self) -> str: ...
    def write_clipboard(self, text: str) -> None: ...
    def launch(self, command: list[str]) -> int: ...
    def family(self, pid: int) -> set[int]:
        """The process and every process descended from it."""
        ...


MODIFIERS = frozenset({"ctrl", "control", "shift", "alt", "super", "meta", "win", "cmd", "command"})


def chord(combo: str) -> list[str]:
    """'ctrl+shift+t' -> its keys in order. A lone character, '+' included, is a chord of that character."""
    parts = [part for part in combo.split("+") if part] if combo != "+" else ["+"]
    if not parts:
        raise ValueError("name a key")
    for part in parts:
        if len(part) != 1 and part.lower() not in NAMED and part.lower() not in MODIFIERS:
            raise ValueError(f"unknown key '{part}'; use a character or one of: {', '.join(sorted(NAMED))}")
    return [part if len(part) == 1 else part.lower() for part in parts]


def descendants(pid: int, parents: dict[int, int]) -> set[int]:
    """`pid` and every process whose parent chain reaches it."""
    family = {pid}
    grew = True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in family and child not in family:
                family.add(child)
                grew = True
    return family


def _elements_path(window: Window) -> Path:
    return screen.state_dir() / "elements" / f"{window.address}.json"


def _identity(window: Window) -> list:
    return [window.address, window.instance, window.pid, window.stable_id]


class NativeDesk(BaseDesk):
    def __init__(self, driver: Driver, owner: int | None = None, sleep: Callable[[float], None] = time.sleep):
        self.driver = driver
        self.owner = owner
        self._sleep = sleep

    def close(self) -> None:
        pass

    # Windows

    def windows(self) -> list[Window]:
        return self.driver.windows()

    def window(self, query: str) -> Window:
        return find_window(self.driver.windows(), query)

    def focus(self, query: str) -> Window:
        win = self.window(query)
        self.driver.focus(win)
        return win

    def cursor(self) -> tuple[int, int]:
        return self.driver.cursor()

    def _hidden_unavailable(self, what: str) -> Unavailable:
        return Unavailable(
            f"{what} needs an agent workspace the user cannot see, which deskpilot has only on Hyprland; on "
            f"{self.driver.instance} act on a window on screen"
        )

    def _target(self, query: str, show: bool, hidden: bool, refresh: bool = False) -> Window:
        if show and hidden:
            raise ValueError("show brings a window into view and hidden acts out of view: pass one")
        if hidden:
            raise self._hidden_unavailable("a hidden action")
        win = self.window(query)
        if not refresh:
            taken = screen.latest(win)
            if taken:
                taken.check_identity(win)
        if win.visible:
            return win
        if not show:
            raise NotVisible(
                f"{win.cls} ({win.address}) is not on screen: it is minimized or on another desktop. Pass --show (show "
                "over MCP) to bring it into view, which focuses it"
            )
        self.driver.focus(win)
        deadline = time.monotonic() + SHOW_TIMEOUT
        while time.monotonic() < deadline:
            win = self.window(win.address)
            if win.visible:
                return win
            self._sleep(0.05)
        raise NotVisible(f"{win.cls} ({win.address}) did not come on screen within {SHOW_TIMEOUT}s of focusing it")

    def _shootable(self, win: Window) -> Window:
        if self.driver.locked():
            raise SessionLocked("the session is locked: unlock the desktop before capturing a window")
        if not win.visible:
            raise NotVisible(f"{win.cls} ({win.address}) is not on screen, so it cannot be shot")
        return win

    def _capture(self, win: Window, max_width: int, out: Path | None = None) -> screen.Shot:
        return screen.capture(win, out, max_width, grab=self.driver.grab)

    def shot(
        self,
        query: str,
        out: Path | None = None,
        max_width: int = screen.DEFAULT_MAX_WIDTH,
        show: bool = False,
        hidden: bool = False,
    ) -> screen.Shot:
        return self._capture(self._shootable(self._target(query, show, hidden, refresh=True)), max_width, out)

    # Points

    def point(
        self,
        query: str,
        x: float | None,
        y: float | None,
        scale: float | None = None,
        shot: str | None = None,
        show: bool = False,
        element: str | None = None,
        hidden: bool = False,
    ) -> tuple[Window, int, int]:
        if (element is None) == (x is None or y is None):
            raise ValueError("give a point (x and y) or an element, not both or neither")
        win = self._target(query, show, hidden)
        if element is not None:
            at = self.locate(win, element)
            gx, gy = win.x + at.x + at.width // 2, win.y + at.y + at.height // 2
        else:
            assert x is not None and y is not None
            taken = screen.load(shot) if shot else screen.last_at(win)
            if taken and (shot or scale is None):
                taken.check(win)
            if scale is None:
                scale = taken.scale if taken else 1.0
            gx, gy = screen.to_global(win, x, y, scale)
        self._check_point(win, gx, gy)
        return win, gx, gy

    def _check_point(self, win: Window, x: int, y: int) -> None:
        top = self.driver.top_at(x, y)
        if top != win.address:
            raise Covered(
                f"({x}, {y}) in {win.cls} ({win.address}) is under another window ({top or 'none'}); move or close "
                "it, or pick a point that shows"
            )

    # Pointer

    def _mouse(self, win: Window, event: str, x: int, y: int, **options: Any) -> None:
        """One pointer event. Every event but a release checks at dispatch that the point still shows the target, so
        a window that came up during a pause or along a drag's path is never clicked through; a release always goes
        out, so no button stays held."""
        if event != "up":
            self._check_point(win, x, y)
        self.driver.mouse(win, event, x, y, **options)

    def _seat_check(self, win: Window, seat: bool) -> None:
        """Seat keys go to whatever is in front: check at each one that it is still the target, since the user may
        have switched windows since the last."""
        if seat and next((w.address for w in self.driver.windows() if w.focused), None) != win.address:
            raise RuntimeError(  # refused
                f"{win.cls} ({win.address}) is no longer in front, so the keys would go to another window; stopped"
            )

    @contextmanager
    def _buttons(self, win: Window, seat: bool) -> Generator[set[str]]:
        """The buttons an action holds, each let go however the action ends."""
        held: set[str] = set()
        try:
            yield held
        finally:
            for button in list(held):
                x, y = self.driver.cursor() if seat else (win.x, win.y)
                self._mouse(win, "up", x, y, button=button, seat=seat)

    def _press(self, win: Window, held: set[str], x: int, y: int, button: str, clicks: int, seat: bool) -> None:
        self._mouse(win, "down", x, y, button=button, held=frozenset(held), clicks=clicks, seat=seat)
        held.add(button)
        self._mouse(win, "up", x, y, button=button, held=frozenset(held - {button}), clicks=clicks, seat=seat)
        held.discard(button)

    def _focus_for_seat(self, win: Window) -> None:
        self.driver.focus(win)
        self._sleep(0.15)

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
        win, gx, gy = self.point(query, x, y, scale, shot, show, element)
        if take_pointer:
            self._focus_for_seat(win)
        self._mouse(win, "move", gx, gy, seat=take_pointer)
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
        hidden: bool = False,
    ) -> tuple[int, int]:
        if hidden and (take_pointer or take_focus):
            raise ValueError(
                "a hidden click leaves the user's pointer and focus alone: drop take_pointer and take_focus"
            )
        if button not in BUTTONS:
            raise ValueError(f"button is one of {', '.join(BUTTONS)}")
        if take_focus:
            self.focus(query)
        if element is not None and button == "left" and count == 1 and not take_pointer:
            win = self._target(query, show, hidden)
            ref, info = self._find(win, element)
            if self.driver.press(win, ref):
                return info.x + info.width // 2, info.y + info.height // 2
        win, gx, gy = self.point(query, x, y, scale, shot, show, element, hidden)
        if take_pointer:
            self._focus_for_seat(win)
        with self._buttons(win, take_pointer) as held:
            self._mouse(win, "move", gx, gy, seat=take_pointer)
            self._sleep(HOLD)
            for n in range(1, count + 1):
                self._press(win, held, gx, gy, button, n, take_pointer)
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
        hidden: bool = False,
    ) -> tuple[int, int]:
        if hidden and take_pointer:
            raise ValueError("a hidden scroll leaves the user's pointer alone: drop take_pointer")
        if dy == 0 or abs(dy) > 50:
            raise ValueError("scroll 1 to 50 notches, positive up")
        win, gx, gy = self.point(query, x, y, scale, shot, show, element, hidden)
        if take_pointer:
            self._focus_for_seat(win)
        self._mouse(win, "move", gx, gy, seat=take_pointer)
        self._sleep(HOLD)
        self._mouse(win, "wheel", gx, gy, dy=dy, seat=take_pointer)
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
        hidden: bool = False,
    ) -> tuple[int, int]:
        win, x1, y1 = self.point(query, *start, scale, shot, show, hidden=hidden)
        _, x2, y2 = self.point(query, *end, scale, shot, show, hidden=hidden)
        if not 0 < duration <= 5:
            raise ValueError("a drag takes more than 0 and at most 5 seconds")
        if button not in BUTTONS:
            raise ValueError(f"button is one of {', '.join(BUTTONS)}")
        moves = max(2, round(duration / DRAG_STEP))
        with self._buttons(win, False) as held:
            self._mouse(win, "move", x1, y1)
            self._sleep(DRAG_PAUSE)
            self._mouse(win, "down", x1, y1, button=button)
            held.add(button)
            self._sleep(DRAG_PAUSE)
            for i in range(1, moves + 1):
                x, y = round(x1 + (x2 - x1) * i / moves), round(y1 + (y2 - y1) * i / moves)
                self._mouse(win, "move", x, y, held=frozenset(held))
                self._sleep(duration / moves)
            self._sleep(DRAG_PAUSE)
            self._mouse(win, "up", x2, y2, button=button)
            held.discard(button)
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
        hidden: bool = False,
    ) -> screen.Shot | tuple[int, int]:
        if not 0 <= wait <= 5:
            raise ValueError("a hover waits 0 to 5 seconds")
        win, gx, gy = self.point(query, x, y, scale, shot, show, element, hidden)
        self._mouse(win, "move", gx, gy)
        self._sleep(wait)
        if then_click is None:
            return self._capture(self._shootable(win), max_width)
        # Resolved after the wait, so a popup the hover opened over the point is seen.
        win, cx, cy = self.point(query, *then_click, scale, shot, show, hidden=hidden)
        with self._buttons(win, False) as held:
            self._mouse(win, "move", cx, cy)
            self._press(win, held, cx, cy, button, 1, False)
        return cx, cy

    # Keys

    def _key_target(self, query: str | None, take_focus: bool, show: bool, hidden: bool) -> Window:
        if hidden:
            raise self._hidden_unavailable("hidden keys")
        if show and (query is None or take_focus):
            raise ValueError(
                "show brings a named window into view: name a window and drop take_focus, which does it too"
            )
        if query is None:
            focused = next((w for w in self.driver.windows() if w.focused), None)
            if focused is None:
                raise WindowNotFound("no window has keyboard focus; name one with a window query")
            return focused
        win = self._target(query, show or take_focus, False)
        if take_focus:
            self._focus_for_seat(win)
        return win

    def key(
        self,
        combo: str,
        query: str | None = None,
        take_focus: bool = False,
        hold: float = 0.0,
        *,
        show: bool = False,
        hidden: bool = False,
        agent_keyboard: bool = False,
    ) -> None:
        if not 0 <= hold <= 10:
            raise ValueError("a key is held 0 to 10 seconds")
        if agent_keyboard:
            raise Unavailable("an agent keyboard is a Hyprland plugin feature; send keys without it")
        parts = chord(combo)
        win = self._key_target(query, take_focus, show, hidden)
        seat = take_focus
        if len(parts) == 1 and len(parts[0]) == 1:
            self._seat_check(win, seat)
            self.driver.text(win, parts[0], seat=seat)
            return
        held: list[str] = []
        try:
            for part in parts:
                self._seat_check(win, seat)
                self.driver.key(win, part, True, held=frozenset(held), seat=seat)
                held.append(part)
            self._sleep(hold)
        finally:
            for part in reversed(held):
                held.remove(part)
                self.driver.key(win, part, False, held=frozenset(held), seat=seat)

    def type(
        self,
        text: str,
        query: str | None = None,
        delay: float = 0.0,
        take_focus: bool = False,
        *,
        show: bool = False,
        hidden: bool = False,
        agent_keyboard: bool = False,
    ) -> None:
        if not text:
            raise ValueError("no text to type")
        if agent_keyboard:
            raise Unavailable("an agent keyboard is a Hyprland plugin feature; type without it")
        win = self._key_target(query, take_focus, show, hidden)
        seat = take_focus
        for char in text:
            self._seat_check(win, seat)
            self.driver.text(win, char, seat=seat)
            self._sleep(delay)

    def paste(
        self,
        text: str,
        query: str | None = None,
        take_focus: bool = False,
        combo: str = "ctrl+v",
        *,
        show: bool = False,
        hidden: bool = False,
    ) -> None:
        raise Unavailable(
            f"a paste offers its text to one app alone, which {self.driver.instance} cannot do without replacing the "
            "user's clipboard: use type"
        )

    def clipboard(self) -> str:
        return self.driver.read_clipboard()

    def set_clipboard(self, text: str) -> None:
        self.driver.write_clipboard(text)

    # Elements

    def elements(self, query: str, show: bool = False, hidden: bool = False, peek: bool = False) -> Element:
        """The window's accessibility tree, bounds in window pixels. Each id records its path from the window's root
        and what it was, so an action finds it again in the tree as it is then."""
        win = self._target(query, show, hidden)
        records: dict[str, dict] = {}
        count = 0
        truncated = False

        def walk(node: object, path: list[int], trail: list[list[str]], depth: int) -> Element:
            nonlocal count, truncated
            count += 1
            element_id = f"e{count}"
            info = self.driver.describe(node)
            trail = [*trail, [info.role, info.name]]
            records[element_id] = {"path": path, "trail": trail}
            children: list[Element] = []
            refs = self.driver.children(node)
            if refs and depth >= MAX_DEPTH:
                truncated = True
            elif refs:
                for index, ref in enumerate(refs):
                    if count >= MAX_NODES:
                        truncated = True
                        break
                    children.append(walk(ref, [*path, index], trail, depth + 1))
            x, y = (info.x - win.x, info.y - win.y) if info.width and info.height else (0, 0)
            return Element(element_id, info.role, info.name, info.states, info.text, x, y, info.width, info.height,
                           tuple(children))  # fmt: skip

        root = walk(self.driver.root(win), [], [], 0)
        if peek:
            return replace(root, truncated=truncated)
        snapshot_id = f"{win.address.removeprefix('0x')}-{time.time_ns()}"
        path = _elements_path(win)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"window": _identity(win), "snapshot": snapshot_id, "elements": records}))
        return replace(root, truncated=truncated, snapshot=snapshot_id)

    def _find(self, win: Window, element: str) -> tuple[object, Node]:
        """An element of the latest snapshot as the app reports it now. Every node on its path from the window must
        still have the role and name it had, so an inserted or removed sibling is stale, never a guess."""
        snapshot_id, _, element_id = element.rpartition(":")
        try:
            payload = json.loads(_elements_path(win).read_text())
            if payload["window"] != _identity(win):
                raise KeyError(element_id)
            record = payload["elements"][element_id]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ElementError(
                f"Element {element_id!r} is unknown for window {win.address}; take a new accessibility snapshot.",
                "stale",
            ) from error
        if snapshot_id and snapshot_id != payload["snapshot"]:
            raise ElementError(
                f"Snapshot {snapshot_id} was replaced by {payload['snapshot']}; take the id from the new one.",
                "stale",
            )
        node = self.driver.root(win)
        trail = record["trail"]
        info = self.driver.describe(node)
        for depth, index in enumerate([*record["path"], None]):
            role, name = trail[depth]
            if (info.role, info.name) != (role, name):
                raise ElementError(
                    f"Element {element_id}'s {'place' if depth < len(trail) - 1 else 'element'} was {role} {name!r} "
                    f"and is now {info.role} {info.name!r}; take a new snapshot.",
                    "stale",
                )
            if index is None:
                break
            refs = self.driver.children(node)
            if index >= len(refs):
                raise ElementError(f"Element {element_id} is gone; take a new snapshot.", "stale")
            node = refs[index]
            info = self.driver.describe(node)
        return node, info

    def locate(self, win: Window, element: str) -> Node:
        """Where an element of the latest snapshot is now, in window pixels. The id is `e5` or `<snapshot>:e5`."""
        _, info = self._find(win, element)
        element_id = element.rpartition(":")[2]
        if info.width <= 0 or info.height <= 0 or "offscreen" in info.states:
            raise ElementError(f"Element {element_id} is not showing; scroll it into view or take a new snapshot.",
                               "offscreen")  # fmt: skip
        at = replace(info, x=info.x - win.x, y=info.y - win.y)
        if not (0 <= at.x + at.width // 2 < win.width and 0 <= at.y + at.height // 2 < win.height):
            raise ElementError(
                f"Element {element_id}'s centre is outside the window; scroll it into view.", "offscreen"
            )
        return at

    # Apps

    def launch(self, command: list[str], timeout: float = LAUNCH_TIMEOUT) -> Window:
        """Start an app and return its first new window. Neither platform has an agent workspace, so it opens on the
        user's desktop like any app they start."""
        if not command:
            raise ValueError("name the app to launch")
        before = {(w.address, w.stable_id) for w in self.driver.windows()}
        pid = self.driver.launch(command)
        deadline = time.monotonic() + timeout
        while True:
            family = self.driver.family(pid)
            new = [w for w in self.driver.windows() if w.pid in family and (w.address, w.stable_id) not in before]
            if new:
                return new[0]
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{command[0]} opened no window within {timeout:g}s")
            self._sleep(0.1)

    def adopt(self, address: str) -> Window:
        raise self._hidden_unavailable("adopting a window")

    def record(self, query: str, out: object, stop: object, *args: object, **kwargs: object) -> NoReturn:
        raise Unavailable("record films the agent cursor the Hyprland plugin draws, and this platform has none")

    def press_cursor(self, down: bool) -> NoReturn:
        raise Unavailable("the agent cursor is drawn by the Hyprland plugin; there is no plugin on this platform")

    def release(self, address: str) -> Window:
        raise self._hidden_unavailable("releasing a window")

    def show_agent(self) -> None:
        raise self._hidden_unavailable("showing the agent's workspace")

    def stop(self) -> None:
        raise self._hidden_unavailable("stopping the agent's apps")
