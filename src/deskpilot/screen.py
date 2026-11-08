"""Window screenshots with grim, and the mapping from screenshot pixels back to the screen.

An agent reads a scaled-down screenshot and names a point in it. `shot` records each window's last scale, so a
later click in screenshot coordinates lands on the same spot whatever size the image was.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hypr import Window

DEFAULT_MAX_WIDTH = 1400


def state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "deskpilot"


@dataclass(frozen=True)
class Shot:
    path: Path
    window: Window
    scale: float
    width: int
    height: int

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "window": self.window.address,
            "scale": self.scale,
            "width": self.width,
            "height": self.height,
        }


def scale_for(width: int, max_width: int) -> float:
    return 1.0 if width <= max_width else round(max_width / width, 4)


def capture(window: Window, out: Path | None = None, max_width: int = DEFAULT_MAX_WIDTH) -> Shot:
    scale = scale_for(window.width, max_width)
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = out or folder / f"shot-{window.address.removeprefix('0x')}.png"
    geometry = f"{window.x},{window.y} {window.width}x{window.height}"
    cmd = ["grim", "-g", geometry]
    if scale != 1.0:
        cmd += ["-s", str(scale)]
    subprocess.run([*cmd, str(path)], check=True, timeout=20)
    shot = Shot(path, window, scale, round(window.width * scale), round(window.height * scale))
    remember_scale(window.address, scale, folder)
    return shot


def _scales_file(folder: Path) -> Path:
    return folder / "scales.json"


def remember_scale(address: str, scale: float, folder: Path | None = None) -> None:
    folder = folder or state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = _scales_file(folder)
    try:
        scales = json.loads(path.read_text())
    except (OSError, ValueError):
        scales = {}
    scales[address] = scale
    path.write_text(json.dumps(scales))


def last_scale(address: str, folder: Path | None = None) -> float:
    try:
        return float(json.loads(_scales_file(folder or state_dir()).read_text()).get(address, 1.0))
    except (OSError, ValueError):
        return 1.0


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
