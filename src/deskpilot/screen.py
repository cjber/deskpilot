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

    def check_identity(self, window: Window) -> None:
        """Titles may change during navigation; stable ids, process ids and instances identify the target."""
        if (self.stable_id, self.pid, self.cls, self.instance) != (
            window.stable_id,
            window.pid,
            window.cls,
            window.instance,
        ):
            raise StaleShot(f"window {window.address} has changed since shot {self.id}: take a new shot")


def _grim(window: Window, path: Path) -> None:
    cmd = ["grim", "-T", window.stable_id] if window.stable_id else ["grim", "-s", "1", "-g", _geometry(window)]
    try:
        result = subprocess.run(
            [*cmd, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=GRIM_TIMEOUT,
            env={**os.environ, "WAYLAND_DISPLAY": window.wayland} if window.wayland else None,
        )
    except subprocess.TimeoutExpired as error:
        raise NoFrame(
            f"grim got no frame of {window.cls} ({window.address}) from the compositor within {GRIM_TIMEOUT}s"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"grim could not capture {window.cls} ({window.address}): {detail}")


def _geometry(window: Window) -> str:
    return f"{window.x},{window.y} {window.width}x{window.height}"


def capture(window: Window, out: Path | None = None, max_width: int = DEFAULT_MAX_WIDTH) -> Shot:
    folder = shots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    shot_id = f"{window.address.removeprefix('0x')}-{time.time_ns() // 1_000_000}"
    path = out or folder / f"{shot_id}.png"
    _grim(window, path)
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
    """This window's shot records, oldest first. Ids end in milliseconds, so name order is time order."""
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


def latest(address: str, folder: Path | None = None) -> Shot | None:
    records = _records(shots_dir(folder), address)
    return load(records[-1].stem, folder) if records else None


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
