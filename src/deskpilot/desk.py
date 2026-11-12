"""The actions an agent takes, shared by the CLI and the MCP server.

Clicks, scrolls, drags, hovers and keys go through the deskpilot Hyprland plugin, which leaves the user's pointer and
keyboard focus where they were; the agent cursor the plugin draws shows where they land. `hidden` acts on a window the
agent launched with `launch`, on its own hidden workspace, with nothing shown and the user's pointer untouched; the
user's own windows are never hidden targets. `take_pointer` instead moves the user's own pointer and clicks through the
uinput device, and `take_focus` focuses the window before its keys.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops

from . import a11y, clipboard, keys, screen
from .hypr import AmbiguousWindow, HyprctlError, Hyprland, Window, WindowNotFound, find_window
from .plugin import BUTTON_CODES, Plugin, PluginError

if TYPE_CHECKING:
    from .batch import BatchRequest, BatchResult


class NotVisible(LookupError):
    code = "not_visible"


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


def error_code(error: BaseException) -> str:
    """The kind of an expected failure, one stable word a program branches on rather than reading the message:
    not_found, ambiguous, not_visible, offscreen, stale, covered, gone, owns_clipboard, refused, busy, no_tree,
    unavailable, timeout or invalid."""
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "invalid"
    return "refused"


# How long `show` waits for a focused window to come on screen: a workspace switch or a scrolling layout's
# animation has to finish before the window's area is its own.
SHOW_TIMEOUT = 3.0

# A drag's pointer path: a point about every frame, so the app sees it travel, and a pause on each side of the press and
# release, as a hand makes, so a slider or a drag threshold sees the press before the motion.
DRAG_STEP = 1 / 60
DRAG_PAUSE_MS = 60
# How long the pointer rests on a point before pressing there, on top of the plugin's 8 ms after each move: the
# 60 ms a hand takes, which a client that reads input once a frame still sees as separate events.
HOLD_MS = 52
# How long beyond its own wait a hover holds the pointer for its screenshot. The plugin ends the hover when the shot is
# taken; this bounds it if deskpilot dies first.
HOVER_SHOT_MS = 5000


# How long `launch` waits for the app's first window.
LAUNCH_TIMEOUT = 30.0


def launched_by(pid: int, token: str) -> bool:
    """Whether the process carries an agent's launch token, which `launch` set and the app's children inherit."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"HL_EXEC_RULE_TOKEN={token}".encode() in environ.split(b"\0")


def owner_from_env() -> int | None:
    """The process named by DESKPILOT_OWNER_PID, whose exit closes the agent's launched apps."""
    value = os.environ.get("DESKPILOT_OWNER_PID") or None
    if value is not None and not (value.isdigit() and int(value) > 1):
        raise ValueError(f"DESKPILOT_OWNER_PID={value!r}: give a process id")
    return int(value) if value else None


class Desk:
    def __init__(self, hypr: Hyprland | None = None, input_factory=None, plugin=None, owner: int | None = None):
        self.hypr = hypr or Hyprland()
        self.plugin = plugin or Plugin()
        self._input_factory = input_factory
        self._input = None
        # The process whose exit stops this agent's launched apps; without one they stop after 30 minutes unused.
        self.owner = owner if owner is not None else owner_from_env()
        self._launched: list[subprocess.Popen] = []

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

    def _register(self) -> dict:
        """The agent's hidden workspace and launch token, registering the agent with the plugin, which marks it in
        use."""
        return self.plugin.register(self.owner)

    def _target(self, query: str, show: bool, hidden: bool) -> Window:
        """The window to act on. A hidden action reaches only the agent's own launched windows, on its hidden
        workspace. A visible action reaches only what is showing, so a window off screen is an error unless `show`,
        which focuses it, since that switches the user's workspace or scrolls their layout."""
        if show and hidden:
            raise ValueError("show brings a window into view and hidden acts out of view: pass one")
        if hidden:
            agent = self._register()
            # A window the user moved there is still theirs: only the agent's launched apps carry its token.
            mine = [
                w
                for w in self.hypr.windows()
                if w.workspace == agent["workspace"] and launched_by(w.pid, agent["token"])
            ]
            try:
                return find_window(mine, query)
            except WindowNotFound:
                raise WindowNotFound(
                    f"no window of agent {self.plugin.agent} matches '{query}': hidden acts only on the apps the agent "
                    "started with `deskpilot launch` (launch over MCP), never on the user's own windows"
                ) from None
        win = self.window(query)
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

    def launch(self, command: list[str], timeout: float = LAUNCH_TIMEOUT) -> Window:
        """Start an app for this agent and return its first window. It and every window its processes open later are
        on the agent's hidden workspace, out of the user's view and focus, for hidden actions to drive. An app that
        hands its window to a copy already running for the user (a single-instance app) opens nothing here."""
        if not command:
            raise ValueError("name the app to launch")
        agent = self._register()
        workspace = agent["workspace"]
        # Hyprland reuses a closed window's address, so a window is known by its stable id too.
        before = {(w.address, w.stable_id) for w in self.hypr.windows()}
        env = {**os.environ, "HL_EXEC_RULE_TOKEN": agent["token"]}
        # Hyprland places a window carrying this on the workspace its launcher was started from, ahead of rules.
        env.pop("HL_INITIAL_WORKSPACE_TOKEN", None)
        self._launched = [p for p in self._launched if p.poll() is None]
        try:
            proc = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise ValueError(f"cannot start {command[0]}: {error.strerror}") from error
        self._launched.append(proc)
        deadline = time.monotonic() + timeout
        while True:
            new = [
                w for w in self.hypr.windows() if w.workspace == workspace and (w.address, w.stable_id) not in before
            ]
            if new:
                return new[0]
            if proc.poll() not in (None, 0):
                raise RuntimeError(f"{command[0]} exited {proc.returncode} before opening a window")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{command[0]} opened no window within {timeout:g}s")
            time.sleep(0.1)

    def show_agent(self) -> None:
        """Show the agent's hidden workspace on the user's focused monitor, or hide it again. Input the user gives
        there is theirs; the agent's actions go on and keep their refusals."""
        self.hypr.toggle_special(self._register()["workspace"].removeprefix("special:"))

    def stop(self) -> None:
        """Close the agent's launched windows, as the user closing them would, and end its apps' processes left five
        seconds later."""
        self.plugin.stop()

    def shot(
        self,
        query: str,
        out: Path | None = None,
        max_width: int = screen.DEFAULT_MAX_WIDTH,
        show: bool = False,
        hidden: bool = False,
    ) -> screen.Shot:
        return screen.capture(self._shootable(self._target(query, show, hidden)), out, max_width)

    @staticmethod
    def _shootable(win: Window) -> Window:
        """The window, if a shot of it can get a frame. Hyprland copies a window's frame only while the window lies
        partly in its monitor's area, so a hidden shot of a scrolling layout's off-screen column is refused here
        rather than left to time out."""
        if not win.on_monitor:
            raise NotVisible(
                f"{win.cls} ({win.address}) lies outside its monitor's area, where Hyprland sends no frame of it, so "
                "it cannot be shot even hidden. Pass --show (show) to bring it into view, which focuses it"
            )
        return win

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
        """Resolve a point to a global position. The point is in pixels of `shot`, or of the window's latest shot,
        or at `scale` (1 for window pixels); with no shot at all it is in window pixels. With `element` it is the
        centre of that element of the window's accessibility tree, where the app says it is now."""
        win, gx, gy, _ = self._aim(query, x, y, scale, shot, show, element, hidden)
        return win, gx, gy

    def _aim(
        self,
        query: str,
        x: float | None,
        y: float | None,
        scale: float | None,
        shot: str | None,
        show: bool,
        element: str | None,
        hidden: bool,
    ) -> tuple[Window, int, int, list[str]]:
        """`point`, and the path steps that go before the first move: for an element, the surface it is on, which the
        plugin checks the point hits."""
        if (element is None) == (x is None or y is None):
            raise ValueError("give a point (x and y) or an element, not both or neither")
        win = self._target(query, show, hidden)
        if element is not None:
            return win, *self._element_point(win, element)
        assert x is not None and y is not None
        if scale is None:
            taken = screen.load(shot) if shot else screen.latest(win.address)
            if taken:
                taken.check(win)
            scale = taken.scale if taken else 1.0
        gx, gy = screen.to_global(win, x, y, scale)
        return win, gx, gy, []

    def _popups(self, win: Window) -> list[a11y.Popup]:
        """The window's mapped popups, as the plugin reports them."""
        return [a11y.Popup(**popup) for popup in self.plugin.popups(win.address)]

    def _element_point(self, win: Window, element: str) -> tuple[int, int, list[str]]:
        """An element's centre as a global position, and its surface step. A GTK 3 popup element is placed through
        its popup's surface, and its centre must lie in the popup."""
        at = a11y.locate(win, element)
        cx, cy = at.x + at.width / 2, at.y + at.height / 2
        popup = a11y.resolve(self._popups(win), at.keys) if at.known else None
        if popup is None:
            gx, gy = screen.to_global(win, cx, cy, 1.0)
            return gx, gy, ["s"] if at.known else []
        px, py = popup.sx + cx, popup.sy + cy
        if not (popup.x <= px < popup.x + popup.width and popup.y <= py < popup.y + popup.height):
            raise a11y.A11yError(
                f"Element {element}'s centre is outside its popup; scroll it into view or take a new snapshot.",
                "offscreen",
            )
        return *screen.window_to_global(win, px, py), [f"s{popup.sx},{popup.sy}"]

    @staticmethod
    def _mode(hidden: bool, cursor: bool = True) -> str:
        """How the plugin reaches the window: hidden, or on screen with the agent cursor unless DESKPILOT_CURSOR=off
        or the action has none, as keys have not."""
        if hidden:
            return "hidden"
        return "plain" if not cursor or os.environ.get("DESKPILOT_CURSOR") == "off" else "shown"

    def _path(self, win: Window, steps: list[str], hidden: bool) -> dict:
        return self.plugin.path(win.address, self._mode(hidden), steps)

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
        if self._mode(False) == "shown":
            self.plugin.cursor(gx, gy)
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
        hidden: bool = False,
    ) -> tuple[int, int]:
        """Click a point. take_focus focuses the window first and leaves it focused, for a menu or dropdown that
        closes when its window is not the focused one."""
        if hidden and (take_pointer or take_focus):
            raise ValueError(
                "a hidden click leaves the user's pointer and focus alone: drop take_pointer and take_focus"
            )
        if take_focus:
            self.focus(query)
        win, gx, gy, surface = self._aim(query, x, y, scale, shot, show, element, hidden)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.click(button, count)
        else:
            code = BUTTON_CODES[button]
            self._path(win, [*surface, f"m{gx},{gy}", f"d{HOLD_MS}", *[f"p{code}", f"r{code}"] * count], hidden)
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
        """Turn the wheel `dy` notches at a point, up to 50; positive is up."""
        if hidden and take_pointer:
            raise ValueError("a hidden scroll leaves the user's pointer alone: drop take_pointer")
        if dy == 0 or abs(dy) > 50:
            raise ValueError("scroll 1 to 50 notches, positive up")
        win, gx, gy, surface = self._aim(query, x, y, scale, shot, show, element, hidden)
        if take_pointer:
            self._take_pointer(win, gx, gy)
            self.input.scroll(dy=dy)
        else:
            self._path(win, [*surface, f"m{gx},{gy}", f"d{HOLD_MS}", f"w{dy}"], hidden)
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
        """Press at `start`, move to `end` over `duration` seconds and release there, for a slider, a selection or a
        drag and drop within the window. Both points are in the same shot's pixels. The user's pointer stays put."""
        win, x1, y1 = self.point(query, *start, scale, shot, show, hidden=hidden)
        _, x2, y2 = self.point(query, *end, scale, shot, show, hidden=hidden)
        if not 0 < duration <= 5:
            raise ValueError("a drag takes more than 0 and at most 5 seconds")
        moves = max(2, round(duration / DRAG_STEP))
        wait = max(0, round(duration * 1000 / moves) - 8)
        code = BUTTON_CODES[button]
        steps = [f"m{x1},{y1}", f"d{DRAG_PAUSE_MS}", f"p{code}", f"d{DRAG_PAUSE_MS}"]
        for i in range(1, moves + 1):
            steps += [f"m{x1 + (x2 - x1) * i / moves:g},{y1 + (y2 - y1) * i / moves:g}", f"d{wait}"]
        steps += [f"d{DRAG_PAUSE_MS}", f"r{code}"]
        self._path(win, steps, hidden)
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
        """Rest the pointer on a point for `wait` seconds without moving the user's, for a tooltip or a menu that
        opens on hover. Returns a screenshot taken while it rests there. With `then_click`, a point in the same
        pixels, it clicks there next instead, before the pointer leaves, and returns that point."""
        if not 0 <= wait <= 5:
            raise ValueError("a hover waits 0 to 5 seconds")
        win, gx, gy, surface = self._aim(query, x, y, scale, shot, show, element, hidden)
        if then_click is not None:
            _, cx, cy = self.point(query, *then_click, scale, shot, show, hidden=hidden)
            code = BUTTON_CODES[button]
            steps = [*surface, f"m{gx},{gy}", f"d{round(wait * 1000)}", f"m{cx},{cy}", f"p{code}", f"r{code}"]
            self._path(win, steps, hidden)
            return cx, cy
        self._shootable(win)
        reply = self._path(win, [*surface, f"m{gx},{gy}", "n", f"d{round(wait * 1000) + HOVER_SHOT_MS}"], hidden)
        try:
            time.sleep(wait)
            return screen.capture(win, max_width=max_width)
        finally:
            self.plugin.end(reply["ticket"])

    def elements(self, query: str, show: bool = False, hidden: bool = False, peek: bool = False) -> a11y.Element:
        """The window's accessibility tree, whose element ids click, hover, move and scroll take. A peek leaves the
        latest snapshot, and the ids taken from it, in place."""
        win = self._target(query, show, hidden)
        return a11y.snapshot(win, peek=peek, popups=lambda: self._popups(win))

    def key(
        self,
        combo: str,
        query: str | None = None,
        take_focus: bool = False,
        hold: float = 0.0,
        *,
        show: bool = False,
        hidden: bool = False,
    ) -> None:
        """Press a chord in a window without focusing it, or with take_focus after focusing it. With no window the
        keys go to the focused window. Each key is held `hold` seconds, up to 10, for an app that acts while a key is
        down (auto-repeat, a game's movement)."""
        if not 0 <= hold <= 10:
            raise ValueError("a key is held 0 to 10 seconds")
        target = self._key_target(query, take_focus, show, hidden)
        self.plugin.keys(target.address, keys.chord_words(combo), hold=hold, mode=self._mode(hidden, cursor=False))

    def type(
        self,
        text: str,
        query: str | None = None,
        delay: float = 0.0,
        take_focus: bool = False,
        *,
        show: bool = False,
        hidden: bool = False,
    ) -> None:
        """Type text as written, on the user's own keymap. Each key is held `delay` seconds, and the next follows
        `delay` seconds later."""
        target = self._key_target(query, take_focus, show, hidden)
        self.plugin.keys(
            target.address, keys.text_words(text), hold=delay, gap=delay, mode=self._mode(hidden, cursor=False)
        )

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
        """Paste text into a window: it becomes that window's clipboard alone, never the user's, and `combo` (the app's
        paste chord, ctrl+shift+v in most terminals) is pressed there. Any text pastes, whatever the keymap types."""
        target = self._key_target(query, take_focus, show, hidden)
        self.plugin.keys(target.address, keys.chord_words(combo), paste=text, mode=self._mode(hidden, cursor=False))

    def clipboard(self) -> str:
        """The user's clipboard text."""
        return clipboard.read()

    def set_clipboard(self, text: str) -> None:
        """Replace the user's clipboard with text."""
        clipboard.write(text)

    def _key_target(self, query: str | None, take_focus: bool, show: bool = False, hidden: bool = False) -> Window:
        """The window keys go to, resolved as for every other action: a named one must be on screen unless `show`
        brings it there, and hidden keys reach only the agent's launched windows. Hidden keys need a named window, since
        the focused one has the user's keyboard, and so does show, since the focused window is in view already and
        take_focus brings one there itself."""
        if hidden and (query is None or take_focus):
            raise ValueError(
                "hidden keys go to a named window and leave the user's focus alone: name a window and drop take_focus"
            )
        if show and (query is None or take_focus):
            raise ValueError(
                "show brings a named window into view: name a window and drop take_focus, which does it too"
            )
        if query is None:
            focused = next((w for w in self.hypr.windows() if w.focused), None)
            if focused is None:
                raise WindowNotFound("no window has keyboard focus; name one with a window query")
            return focused
        win = self.focus(query) if take_focus else self._target(query, show, hidden)
        # A hidden target registered the agent already; keys to its window shown on screen mark it in use too.
        if not hidden and self.plugin.agent and win.workspace == f"special:agent-{self.plugin.agent}":
            self._register()
        return win

    def batch(self, request: BatchRequest) -> BatchResult:
        """Run a validated batch of actions on one window in order, stopping at the first expected failure (see
        `batch`)."""
        from . import batch

        return batch.run(self, request)

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
        x, y, w, h = region or (0, 0, base.width, base.height)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > base.width or y + h > base.height:
            raise ValueError(f"region {x},{y} {w}x{h} is not inside shot {shot} ({base.width}x{base.height})")
        box = (x, y, x + w, y + h)
        with Image.open(base.path) as image:
            before = image.convert("RGB").crop(box)
        deadline = time.monotonic() + timeout
        while True:
            win = self._target(query, False, hidden)
            now = screen.capture(self._shootable(win), max_width=base.width)
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
