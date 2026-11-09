"""Hyprland through hyprctl: windows, monitors, focus and the cursor.

Hyprland 0.57 replaced the string dispatchers (`movecursor 10 20`) with Lua ones (`hl.dsp.cursor.move({x=10,y=20})`).
Each dispatcher here tries the Lua form first and falls back to the string form, so both versions work.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[list[str]], str]


class HyprctlError(RuntimeError):
    pass


def socket_alive(path: Path) -> bool:
    """Whether a Hyprland control socket accepts a connection. A crashed or ended session leaves its file behind."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def pick_instance(signature: str | None, hypr_dir: Path, alive: Callable[[Path], bool] = socket_alive) -> str:
    """The Hyprland instance to talk to: the environment's, unless its socket is dead, then the only live one.

    A shell started before Hyprland restarted keeps the old session's HYPRLAND_INSTANCE_SIGNATURE, and hyprctl then
    fails on every call. Several live instances are an error naming them, never a guess.
    """
    if signature and alive(hypr_dir / signature / ".socket.sock"):
        return signature
    folders = sorted(p.name for p in hypr_dir.iterdir() if p.is_dir()) if hypr_dir.is_dir() else []
    live = [name for name in folders if alive(hypr_dir / name / ".socket.sock")]
    stale = f"HYPRLAND_INSTANCE_SIGNATURE={signature} has no live socket. " if signature else ""
    if len(live) == 1:
        return live[0]
    if not live:
        raise HyprctlError(f"{stale}No running Hyprland instance under {hypr_dir}.")
    raise HyprctlError(
        f"{stale}Several Hyprland instances are running, set HYPRLAND_INSTANCE_SIGNATURE to one: {', '.join(live)}"
    )


def current_instance() -> str:
    """Chosen per call, not once: a long-lived MCP server outlives a Hyprland restart. It costs one connect."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return pick_instance(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), Path(runtime) / "hypr")


def run_hyprctl(args: list[str]) -> str:
    cmd = ["hyprctl", "-i", current_instance(), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[0]
        raise HyprctlError(f"hyprctl {' '.join(args)} exited {result.returncode}: {detail}")
    return result.stdout


@dataclass(frozen=True)
class Window:
    address: str
    cls: str
    title: str
    x: int
    y: int
    width: int
    height: int
    monitor: int
    workspace: str
    xwayland: bool
    pid: int
    focused: bool = False

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "class": self.cls,
            "title": self.title,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "monitor": self.monitor,
            "workspace": self.workspace,
            "xwayland": self.xwayland,
            "pid": self.pid,
            "focused": self.focused,
        }


class WindowNotFound(LookupError):
    pass


class AmbiguousWindow(LookupError):
    pass


class Hyprland:
    def __init__(self, runner: Runner = run_hyprctl):
        self._run = runner

    def _json(self, *args: str):
        out = self._run(["-j", *args])
        return json.loads(out) if out.strip() else None

    def windows(self) -> list[Window]:
        active = self._json("activewindow") or {}
        found = []
        for c in self._json("clients") or []:
            if not c.get("mapped", True) or c.get("hidden"):
                continue
            x, y = c["at"]
            w, h = c["size"]
            found.append(
                Window(
                    address=c["address"],
                    cls=c.get("class", ""),
                    title=c.get("title", ""),
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    monitor=c.get("monitor", -1),
                    workspace=str((c.get("workspace") or {}).get("name", "")),
                    xwayland=bool(c.get("xwayland")),
                    pid=c.get("pid", 0),
                    focused=c["address"] == active.get("address"),
                )
            )
        return found

    def visible(self, window: Window) -> bool:
        """Whether the window's workspace is the one showing on its monitor, so a screenshot of its area shows it."""
        showing = {str((m.get("activeWorkspace") or {}).get("name", "")) for m in self._json("monitors") or []}
        return window.workspace in showing

    def find(self, query: str) -> Window:
        return find_window(self.windows(), query)

    def cursor(self) -> tuple[int, int]:
        pos = self._json("cursorpos") or {}
        return int(pos.get("x", 0)), int(pos.get("y", 0))

    def _dispatch(self, lua: str, legacy: str) -> None:
        """Run the Lua dispatcher, or the string one on Hyprland before 0.57. Either failing exits non-zero."""
        try:
            if self._run(["dispatch", lua]).strip() == "ok":
                return
        except HyprctlError:
            pass
        out = self._run(["dispatch", *legacy.split(" ", 1)])
        if out.strip() != "ok":
            raise HyprctlError(f"hyprctl dispatch {legacy} failed: {out.strip()}")

    def move_cursor(self, gx: int, gy: int) -> None:
        self._dispatch(f"hl.dsp.cursor.move({{x={gx},y={gy}}})", f"movecursor {gx} {gy}")

    def focus(self, window: Window) -> None:
        self._dispatch(f'hl.dsp.focus({{window="address:{window.address}"}})', f"focuswindow address:{window.address}")


def find_window(windows: list[Window], query: str) -> Window:
    """Match an address exactly, then a class exactly, then a substring of class or title, all case-insensitive.

    A query that matches more than one window at the same level is an error naming them, never a guess.
    """
    q = query.lower()
    levels = [
        [w for w in windows if w.address.lower() == q or w.address.lower() == f"0x{q}"],
        [w for w in windows if w.cls.lower() == q],
        [w for w in windows if q in w.cls.lower() or q in w.title.lower()],
    ]
    for matches in levels:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = "; ".join(f"{w.address} {w.cls} '{w.title[:40]}'" for w in matches)
            raise AmbiguousWindow(f"'{query}' matches {len(matches)} windows, use an address: {names}")
    raise WindowNotFound(f"no window matches '{query}'")
