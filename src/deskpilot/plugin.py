"""The deskpilot Hyprland plugin: building it, loading it and asking it to click.

The plugin (`plugin/deskpilot.cpp`) runs each click or scroll inside the compositor: it holds the user's mouse
input, presses on the target window and puts the user's pointer and keyboard focus back. Hyprland plugins must be
built against the exact running Hyprland, so the build is named after its commit and rebuilt after an upgrade.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hypr import HyprctlError, Runner, run_hyprctl

SOURCE = Path(__file__).parent / "plugin"
VERSION = "1"
BUTTON_CODES = {"left": 272, "right": 273, "middle": 274}


class PluginError(RuntimeError):
    pass


def build_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "deskpilot"


def build_path(commit: str) -> Path:
    return build_dir() / f"deskpilot-{commit[:12]}.so"


def parse_reply(text: str) -> dict:
    """The plugin's JSON answer, or a PluginError carrying its refusal. `unknown request` means it is not loaded."""
    text = text.strip()
    if text == "unknown request" or not text:
        raise PluginError("the deskpilot Hyprland plugin is not loaded: run `deskpilot plugin load`")
    try:
        reply = json.loads(text)
    except ValueError as error:
        raise PluginError(f"unexpected reply from the deskpilot plugin: {text[:200]}") from error
    if not reply.get("ok"):
        raise PluginError(reply.get("error", text))
    return reply


@dataclass
class Plugin:
    run: Runner = run_hyprctl

    def _call(self, *args: str) -> dict:
        try:
            out = self.run(["deskpilot", *args])
        except HyprctlError as error:
            raise PluginError(str(error)) from error
        return parse_reply(out)

    def loaded(self) -> bool:
        try:
            return self._call("status").get("version") == VERSION
        except PluginError:
            return False

    def status(self) -> dict:
        return self._call("status")

    def click(self, address: str, gx: int, gy: int, button: str = "left", count: int = 1) -> dict:
        return self._call("click", address, str(gx), str(gy), str(BUTTON_CODES[button]), str(count))

    def scroll(self, address: str, gx: int, gy: int, dy: int) -> dict:
        return self._call("scroll", address, str(gx), str(gy), str(dy))

    def commit(self) -> str:
        return json.loads(self.run(["-j", "version"]))["commit"]

    def build(self) -> Path:
        out = build_path(self.commit())
        out.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["make", "-C", str(SOURCE), f"OUT={out}"], capture_output=True, text=True, check=False, timeout=600
        )
        if result.returncode != 0:
            raise PluginError(f"building the plugin failed:\n{result.stderr.strip()[-2000:]}")
        return out

    def load(self) -> Path:
        """Build the plugin for the running Hyprland if needed and load it. A loaded current version is left as is."""
        path = build_path(self.commit())
        if self.loaded():
            return path
        if not path.exists():
            path = self.build()
        out = self.run(["plugin", "load", str(path)]).strip()
        if out != "ok":
            raise PluginError(f"hyprctl plugin load {path}: {out}")
        return path
