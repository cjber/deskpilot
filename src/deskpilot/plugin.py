"""The deskpilot Hyprland plugin: building it, loading it and asking it to click and press keys.

The plugin (`plugin/deskpilot.cpp`) runs each click, scroll and keystroke inside the compositor: it holds the user's
input, delivers the event to the target window and puts the user's pointer and keyboard focus back. Hyprland plugins
must be built against the exact running Hyprland, so the build is named after its commit and rebuilt after an
upgrade.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hypr import HyprctlError, Runner, run_hyprctl

SOURCE = Path(__file__).parent / "plugin"
BUTTON_CODES = {"left": 272, "right": 273, "middle": 274}


class PluginError(RuntimeError):
    pass


def build_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "deskpilot"


def source_hash() -> str:
    """Names a build after its source, so a changed plugin is rebuilt and reloaded rather than mistaken for current."""
    digest = hashlib.sha256()
    for name in ("deskpilot.cpp", "Makefile"):
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()[:12]


def build_path(commit: str) -> Path:
    return build_dir() / f"deskpilot-{commit[:12]}-{source_hash()}.so"


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
            return self._call("status").get("version") == source_hash()
        except PluginError:
            return False

    def status(self) -> dict:
        return self._call("status")

    def click(self, address: str, gx: int, gy: int, button: str = "left", count: int = 1) -> dict:
        return self._call("click", address, str(gx), str(gy), str(BUTTON_CODES[button]), str(count))

    def scroll(self, address: str, gx: int, gy: int, dy: int) -> dict:
        return self._call("scroll", address, str(gx), str(gy), str(dy))

    def keys(self, address: str, events: list[tuple[int, bool]]) -> dict:
        """Press and release key codes in a window without focusing it."""
        return self._call("keys", address, *(f"{'+' if down else '-'}{code}" for code, down in events))

    def unload(self) -> None:
        """Unload whatever deskpilot build is loaded. A build too old to report its path names it in the error."""
        try:
            status = self.status()
        except PluginError:
            return
        if not status.get("path"):
            raise PluginError("an older deskpilot plugin is loaded: unload it with `hyprctl plugin unload <its .so>`")
        out = self.run(["plugin", "unload", status["path"]]).strip()
        if out not in ("ok", ""):
            raise PluginError(f"hyprctl plugin unload {status['path']}: {out}")

    def commit(self) -> str:
        return json.loads(self.run(["-j", "version"]))["commit"]

    def build(self) -> Path:
        out = build_path(self.commit())
        out.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["make", "-C", str(SOURCE), f"OUT={out}", f"SOURCE_HASH={source_hash()}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            raise PluginError(f"building the plugin failed:\n{result.stderr.strip()[-2000:]}")
        return out

    def load(self) -> Path:
        """Build the plugin for the running Hyprland if needed and load it, replacing a loaded build of other source."""
        path = build_path(self.commit())
        if self.loaded():
            return path
        self.unload()
        if not path.exists():
            path = self.build()
        out = self.run(["plugin", "load", str(path)]).strip()
        if out != "ok":
            raise PluginError(f"hyprctl plugin load {path}: {out}")
        return path
