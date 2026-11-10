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
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hypr import HyprctlError, Runner, run_hyprctl

SOURCE = Path(__file__).parent / "plugin"
BUTTON_CODES = {"left": 272, "right": 273, "middle": 274}

# Hyprland takes a request as whatever its socket holds when it reads, so a request must reach it in one piece. The
# kernel queues a Unix stream write of up to 32 KiB of page fragments (UNIX_SKB_FRAGS_SZ) as one buffer; a longer key
# sequence is staged in parts that size.
REQUEST_BYTES = 32 * 1024


class PluginError(RuntimeError):
    pass


class PluginNotLoaded(PluginError):
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
        raise PluginNotLoaded("the deskpilot Hyprland plugin is not loaded: run `deskpilot plugin load`")
    try:
        reply = json.loads(text)
    except ValueError as error:
        raise PluginError(f"unexpected reply from the deskpilot plugin: {text[:200]}") from error
    if not reply.get("ok"):
        raise PluginError(reply.get("error", text))
    return reply


def request_size(args: list[str]) -> int:
    """Bytes hyprctl sends for `hyprctl deskpilot <args>`: its flags (none here), a slash, then the words."""
    return len("/" + " ".join(["deskpilot", *args]))


def fitting(words: list[str], head: list[str]) -> int:
    """How many of `words`, from the first, fit in one request after `head`."""
    size = request_size(head)
    for count, word in enumerate(words):
        size += 1 + len(word)
        if size > REQUEST_BYTES:
            return count
    return len(words)


@dataclass
class Plugin:
    run: Runner = run_hyprctl

    def _send(self, *args: str) -> dict:
        try:
            out = self.run(["deskpilot", *args])
        except HyprctlError as error:
            raise PluginError(str(error)) from error
        return parse_reply(out)

    def _call(self, *args: str) -> dict:
        """The reply once the action has finished, loading the plugin first if Hyprland has none, as after a restart
        or an upgrade. A loaded build of other source is left alone: another checkout may be using it. Actions queue in
        the plugin, which answers within hyprctl's five second read timeout: an action still queued or running answers
        with a ticket to wait on again."""
        try:
            try:
                reply = self._send(*args)
            except PluginNotLoaded:
                self.load()
                reply = self._send(*args)
            while "pending" in reply:
                reply = self._send("wait", str(reply["pending"]))
        except PluginError as error:
            raise self._mismatch(error) from error
        return reply

    def _mismatch(self, refusal: PluginError) -> PluginError:
        """A refusal from a loaded build of other source names that build: its requests may differ from this CLI's, so
        the refusal can blame arguments the caller got right."""
        try:
            version = self.status().get("version")
        except PluginError:
            return refusal
        if version == source_hash():
            return refusal
        return PluginError(
            f"the loaded deskpilot plugin is build {version or 'unknown'}, not this CLI's {source_hash()}, so it may"
            f" not understand the request: run `deskpilot plugin load` to replace it. The plugin answered: {refusal}"
        )

    def loaded(self) -> bool:
        try:
            return self.status().get("version") == source_hash()
        except PluginError:
            return False

    def status(self) -> dict:
        return self._send("status")

    def click(self, address: str, gx: int, gy: int, button: str = "left", count: int = 1) -> dict:
        return self._call("click", address, str(gx), str(gy), str(BUTTON_CODES[button]), str(count))

    def scroll(self, address: str, gx: int, gy: int, dy: int) -> dict:
        return self._call("scroll", address, str(gx), str(gy), str(dy))

    def keys(self, address: str, words: list[str], hold: float = 0.0, gap: float = 0.0) -> dict:
        """Send key events (see keys.py) to a window without focusing it, as one action. Each press is held `hold`
        seconds, and keystrokes (runs of events that end with no key down) are `gap` seconds apart. A sequence too long
        for one request is staged ahead in parts and queued whole by the last, so no other keys land in between."""
        head = ["keys", address, str(round(hold * 1000)), str(round(gap * 1000))]
        if request_size([*head, "none", *words]) <= REQUEST_BYTES:
            return self._call(*head, "none", *words)
        txn = secrets.token_hex(8)
        last = len(words) - fitting(words[::-1], [*head, txn])
        start = 0
        while start < last:
            count = fitting(words[start:last], ["stage", txn])
            self._call("stage", txn, *words[start : start + count])
            start += count
        return self._call(*head, txn, *words[last:])

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
