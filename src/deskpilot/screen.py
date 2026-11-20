"""Window screenshots with grim, and the mapping from screenshot pixels back to the screen.

An agent reads a scaled-down screenshot and names a point in it. Each shot gets an id and keeps the window's size
and the image's scale, so a later click in its pixels lands on the same spot, and a click against a window that has
since changed size is refused instead of landing somewhere else.

grim captures the window itself by its toplevel identifier, so another window on top does not show in the image.
The scale is read from the image grim wrote, never assumed: on a scaled output the buffer is larger than the
window's logical size.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from .hypr import Window

DEFAULT_MAX_WIDTH = 1400
# Shots kept per window. Older ones are deleted, so an id that old is stale.
KEEP = 20


# How long grim may wait for the compositor's frame of a window. A frame comes within a few monitor refreshes, so one
# that has not come by then never will, as for a window whose monitor is off.
GRIM_TIMEOUT = 5


class StaleShot(ValueError):
    code = "stale"


class NoFrame(TimeoutError):
    code = "timeout"


def state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "deskpilot"


def shots_dir(folder: Path | None = None) -> Path:
    return (folder or state_dir()) / "shots"


@dataclass(frozen=True)
class Shot:
    id: str
    path: str
    address: str
    width: int  # image pixels
    height: int
    scale: float  # image pixels per window pixel
    window_width: int
    window_height: int
    stable_id: str = ""
    pid: int = 0
    cls: str = ""
    instance: str = ""

    def to_dict(self) -> dict:
        return {
            "shot": self.id,
            "path": self.path,
            "window": self.address,
            "scale": self.scale,
            "width": self.width,
            "height": self.height,
        }

    def check(self, window: Window) -> None:
        """Refuse the shot for a point in `window` if it is of another window or the window has changed size."""
        if window.address != self.address:
            raise StaleShot(f"shot {self.id} is of window {self.address}, not {window.address}")
        self.check_identity(window)
        if (window.width, window.height) != (self.window_width, self.window_height):
            raise StaleShot(
                f"{window.cls} is {window.width}x{window.height} now, {self.window_width}x{self.window_height} in "
                f"shot {self.id}: take a new shot"
            )

    def of(self, window: Window) -> bool:
        """Whether this shot is of `window` itself. Hyprland gives a closed window's address to a later one, so the
        address alone does not name a window: its compositor, process and stable id do too."""
        return (self.address, self.instance, self.pid, self.stable_id) == (
            window.address,
            window.instance,
            window.pid,
            window.stable_id,
        )

    def check_identity(self, window: Window) -> None:
        """Titles may change during navigation; stable ids, process ids and instances identify the target."""
        if (self.address, self.instance) == (window.address, window.instance) and not self.of(window):
            raise StaleShot(
                f"shot {self.id} is of an earlier window at {window.address}, not the {window.cls} there now: take a "
                "new shot"
            )
        if (self.stable_id, self.pid, self.cls, self.instance) != (
            window.stable_id,
            window.pid,
            window.cls,
            window.instance,
        ):
            raise StaleShot(f"window {window.address} has changed since shot {self.id}: take a new shot")


def _grim(window: Window, path: Path | None = None, cursor: bool = False) -> bytes:
    """Capture `window` to `path`, or with no path return a PPM of it. `cursor` asks the compositor for a capture with
    a cursor, which is what makes the plugin draw the agent cursors into it (see `frame`)."""
    cmd = ["grim", "-T", window.stable_id] if window.stable_id else ["grim", "-s", "1", "-g", _geometry(window)]
    if cursor:
        cmd.insert(1, "-c")
    if path is None:
        cmd[1:1] = ["-t", "ppm"]
    try:
        result = subprocess.run(
            [*cmd, str(path) if path else "-"],
            capture_output=True,
            check=False,
            timeout=GRIM_TIMEOUT,
            env={**os.environ, "WAYLAND_DISPLAY": window.wayland} if window.wayland else None,
        )
    except subprocess.TimeoutExpired as error:
        raise NoFrame(
            f"grim got no frame of {window.cls} ({window.address}) from the compositor within {GRIM_TIMEOUT}s"
        ) from error
    if result.returncode != 0:
        detail = _text(result.stderr).strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"grim could not capture {window.cls} ({window.address}): {detail}")
    return result.stdout


def _text(output: bytes | str) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output


def frame(window: Window) -> bytes:
    """One PPM frame of the window as a recording takes it: with the agent cursor the plugin draws, brackets closing in
    as it lands and the marker squeezing on a press, wherever the window is, hidden included. This is the one capture
    that carries the cursor; `capture`, which a model reads, never does. It shows no pointer of the user's."""
    if not window.stable_id:
        raise RuntimeError(f"{window.cls} ({window.address}) has no stable id, so the compositor cannot film it")
    return _grim(window, cursor=True)


def _geometry(window: Window) -> str:
    return f"{window.x},{window.y} {window.width}x{window.height}"


def capture(
    window: Window,
    out: Path | None = None,
    max_width: int = DEFAULT_MAX_WIDTH,
    grab: Callable[[Window, Path], None] | None = None,
) -> Shot:
    """Capture the window to a recorded shot. `grab` writes the window's image to a path: grim on Hyprland, the
    platform's own window capture elsewhere."""
    folder = shots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    shot_id = f"{window.address.removeprefix('0x')}-{time.time_ns()}"
    path = out or folder / f"{shot_id}.png"
    (grab or _grim)(window, path)
    with Image.open(path) as image:
        width, height = image.size
        if width > max_width:
            height = round(height * max_width / width)
            width = max_width
            image.resize((width, height), Image.Resampling.LANCZOS).save(path)
    shot = Shot(
        shot_id,
        str(path),
        window.address,
        width,
        height,
        width / window.width,
        window.width,
        window.height,
        window.stable_id,
        window.pid,
        window.cls,
        window.instance,
    )
    (folder / f"{shot_id}.json").write_text(json.dumps(asdict(shot)))
    _prune(folder, window.address)
    return shot


def _records(folder: Path, address: str) -> list[Path]:
    """This window's shot records, oldest first. Ids end in timestamps, so name order is time order."""
    return sorted(folder.glob(f"{address.removeprefix('0x')}-*.json"))


def _prune(folder: Path, address: str) -> None:
    for record in _records(folder, address)[:-KEEP]:
        record.with_suffix(".png").unlink(missing_ok=True)
        record.unlink(missing_ok=True)


def discard(shot: Shot) -> None:
    """Remove a shot nobody will use, such as a poll that saw nothing new."""
    Path(shot.path).unlink(missing_ok=True)
    (shots_dir() / f"{shot.id}.json").unlink(missing_ok=True)


def load(shot_id: str, folder: Path | None = None) -> Shot:
    try:
        return Shot(**json.loads((shots_dir(folder) / f"{shot_id}.json").read_text()))
    except (OSError, ValueError, TypeError) as error:
        raise StaleShot(f"no shot {shot_id}: take a new one") from error


def latest(window: Window, folder: Path | None = None) -> Shot | None:
    """The newest shot of `window` itself, or None. A shot of an earlier window at the same address is not one."""
    for record in reversed(_records(shots_dir(folder), window.address)):
        shot = load(record.stem, folder)
        if shot.of(window):
            return shot
    return None


def last_at(window: Window, folder: Path | None = None) -> Shot | None:
    """The newest shot at `window`'s address on its compositor, of whatever window was there, or None. Coordinates
    given without a shot id are in this shot: one of an earlier window at the address refuses them, never re-aims them
    at the window there now."""
    for record in reversed(_records(shots_dir(folder), window.address)):
        shot = load(record.stem, folder)
        if shot.instance == window.instance:
            return shot
    return None


def to_global(window: Window, x: float, y: float, scale: float) -> tuple[int, int]:
    """A point in a screenshot of `window` taken at `scale`, as a global compositor position.

    Points outside the window are refused: a click there would land on whatever is behind it.
    """
    wx, wy = x / scale, y / scale
    if not (0 <= wx < window.width and 0 <= wy < window.height):
        raise ValueError(
            f"({x}, {y}) at scale {scale} is outside {window.cls} ({window.width}x{window.height} window pixels)"
        )
    return window.x + round(wx), window.y + round(wy)


def window_to_global(window: Window, x: float, y: float) -> tuple[int, int]:
    """A point in window pixels as a global compositor position, wherever it is. Only for a point on a popup, which may
    reach past the window and is checked against the popup instead of the window."""
    return window.x + round(x), window.y + round(y)
