"""The deskpilot Hyprland plugin: building it, loading it and asking it to click and press keys.

The plugin (`plugin/deskpilot.cpp`) runs each click, scroll and keystroke inside the compositor: it holds the user's
input, delivers the event to the target window and puts the user's pointer and keyboard focus back. Hyprland plugins
must be built against the exact running Hyprland, so the build is named after its commit and rebuilt after an
upgrade.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

from .hypr import HyprctlError, Runner, run_hyprctl

SOURCE = Path(__file__).parent / "plugin"
BUTTON_CODES = {"left": 272, "right": 273, "middle": 274}

# Hyprland takes a request as whatever its socket holds when it reads, so a request must reach it in one piece. The
# kernel queues a Unix stream write of up to 32 KiB of page fragments (UNIX_SKB_FRAGS_SZ) as one buffer; a longer key
# sequence is staged in parts that size.
REQUEST_BYTES = 32 * 1024
# What the plugin takes as an agent's name: it is shown on the agent's cursor and travels as one word of a request.
AGENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,32}")


class PluginError(RuntimeError):
    """The plugin's refusal. `code` is the plugin's word for its kind: covered (another window or layer is over the
    point), gone (the window or surface went away), owns_clipboard (a paste's target app holds the user's clipboard),
    busy (its queue is full), unavailable, or refused."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.code = code


class PluginNotLoaded(PluginError):
    def __init__(self, message: str):
        super().__init__(message, "unavailable")


def build_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "deskpilot"


def _digest(*names: str) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()[:12]


def source_hash() -> str:
    """Names a build after its source, so a changed plugin is rebuilt and reloaded rather than mistaken for current."""
    return _digest("deskpilot.cpp", "seat.cpp", "seat.hpp", "Makefile")


def seat_hash() -> str:
    """Names the agent seat library after its own source: the plugin keeps one open for the life of Hyprland."""
    return _digest("seat.cpp", "seat.hpp")


def headers_commit() -> str | None:
    """The Hyprland commit the installed headers are from, which is what `make` compiles the plugin against."""
    flags = subprocess.run(["pkg-config", "--cflags-only-I", "hyprland"], capture_output=True, text=True, check=False)
    for flag in flags.stdout.split():
        version = Path(flag.removeprefix("-I")) / "src/version.h"
        if version.is_file():
            for line in version.read_text().splitlines():
                if line.startswith("#define GIT_COMMIT_HASH"):
                    return line.split('"')[1]
    return None


def build_path(commit: str) -> Path:
    return build_dir() / f"deskpilot-{commit[:12]}-{source_hash()}.so"


def seat_path(commit: str) -> Path:
    return build_dir() / f"deskpilot-seat-{commit[:12]}-{seat_hash()}.so"


def parse_reply(text: str) -> dict:
    """The plugin's JSON answer, or a PluginError carrying its refusal. `unknown request` means it is not loaded.
    Hyprland answers every request it reads, so an empty reply means it dropped the connection, not that the plugin is
    missing."""
    text = text.strip()
    if text == "unknown request":
        raise PluginNotLoaded("the deskpilot Hyprland plugin is not loaded: run `deskpilot plugin load`")
    if not text:
        raise PluginError("Hyprland closed the connection without answering: try again", "unavailable")
    try:
        reply = json.loads(text)
    except ValueError as error:
        raise PluginError(f"unexpected reply from the deskpilot plugin: {text[:200]}", "unavailable") from error
    if not reply.get("ok"):
        raise PluginError(reply.get("error", text), reply.get("code", "refused"))
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


def agent_from_env() -> str | None:
    """The agent named by DESKPILOT_AGENT, whose cursor shows its actions; None (unset or empty) for the unnamed one."""
    name = os.environ.get("DESKPILOT_AGENT") or None
    if name is not None and not AGENT_NAME.fullmatch(name):
        raise ValueError(f"DESKPILOT_AGENT={name!r}: use up to 32 letters, digits, '-', '_' or '.'")
    return name


@dataclass
class Plugin:
    run: Runner = run_hyprctl
    # Whose agent cursor shows this caller's actions. Each name has a cursor of its own, in a colour of its own.
    agent: str | None = field(default_factory=agent_from_env)

    def _agent(self) -> list[str]:
        return [f"agent={self.agent}"] if self.agent else []

    def _send(self, *args: str) -> dict:
        try:
            out = self.run(["deskpilot", *args])
        except HyprctlError as error:
            raise PluginError(str(error), "unavailable") from error
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
            f" not understand the request: run `deskpilot plugin load` to replace it. The plugin answered: {refusal}",
            refusal.code,
        )

    def loaded(self) -> bool:
        status = self.loaded_status()
        return status is not None and status.get("version") == source_hash()

    def loaded_status(self) -> dict | None:
        """The loaded build's status, or None when Hyprland has no deskpilot plugin. Failing to ask Hyprland raises,
        so it is never taken for a missing plugin and answered with a blind load."""
        try:
            return self.status()
        except PluginNotLoaded:
            return None

    def status(self) -> dict:
        return self._send("status")

    def path(self, address: str, mode: str, steps: list[str]) -> dict:
        """Run a pointer path in a window as one action. `mode` is "shown" (on screen, with the agent cursor gliding
        there first), "plain" (on screen, no cursor) or "hidden" (the window's own pointer, wherever the window is).
        `mX,Y` moves to a global point (the first step enters the window there), `pCODE` and `rCODE` press and
        release a button, `wN` turns the wheel N notches (positive up), `dMS` waits, and `n` answers here with the
        action's ticket while it goes on, until it ends or `end` ends it."""
        return self._call("path", *self._agent(), address, mode, *steps)

    def cursor(self, gx: int, gy: int) -> dict:
        """Glide this agent's cursor to a global point, in turn with the actions, and return once it lands."""
        return self._call("cursor", *self._agent(), str(gx), str(gy))

    def register(self, owner: int | None = None) -> dict:
        """Have every window this agent launches open on its hidden workspace, and mark the agent in use. `owner` is
        the process whose exit stops the agent; with none it stops after 30 minutes unused. Returns the workspace and
        the token `launch` puts in the app's environment."""
        return self._call("agent", self._named(), *([f"owner={owner}"] if owner else []))

    def stop(self) -> None:
        """Close this agent's windows, and end its apps' processes left five seconds later."""
        self._call("agent", self._named(), "stop")

    def _named(self) -> str:
        if not self.agent:
            raise ValueError(
                "name the agent with DESKPILOT_AGENT: an agent's launched apps and its hidden workspace are its own"
            )
        return self.agent

    def end(self, ticket: int) -> None:
        """End the path with this ticket now, letting go of what it holds. One that has ended already is fine."""
        self._send("end", str(ticket))

    def keys(
        self,
        address: str,
        words: list[str],
        hold: float = 0.0,
        gap: float = 0.0,
        paste: str | None = None,
        mode: str = "plain",
    ) -> dict:
        """Send key events (see keys.py) to a window without focusing it, as one action. Each press is held `hold`
        seconds, and keystrokes (runs of events that end with no key down) are `gap` seconds apart. A sequence too long
        for one request is staged ahead in parts and queued whole by the last, so no other keys land in between. With
        `paste`, that text is the window's clipboard, and its alone, when the keys (its paste chord) arrive. `mode`
        "hidden" never holds the user's input: the plugin refuses an XWayland window and a stroke while the user's
        keyboard focus is in the window's app."""
        if paste is None:
            return self._keys(["keys", address, mode], words, hold, gap)
        fd, path = tempfile.mkstemp(prefix="deskpilot-paste-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(paste.encode())
            return self._keys(["paste", address, mode, path], words, hold, gap)
        finally:
            Path(path).unlink(missing_ok=True)

    def _keys(self, verb: list[str], words: list[str], hold: float, gap: float) -> dict:
        head = [*verb, str(round(hold * 1000)), str(round(gap * 1000))]
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
        status = self.loaded_status()
        if status is None:
            return
        if not status.get("path"):
            raise PluginError(
                "an older deskpilot plugin is loaded: unload it with `hyprctl plugin unload <its .so>`", "unavailable"
            )
        out = self.run(["plugin", "unload", status["path"]]).strip()
        if out not in ("ok", ""):
            raise PluginError(f"hyprctl plugin unload {status['path']}: {out}", "unavailable")

    def commit(self) -> str:
        return json.loads(self.run(["-j", "version"]))["commit"]

    def running_commit(self) -> str:
        """The running Hyprland's commit, once the installed headers are shown to be from it. After an upgrade the
        headers are the new version's until Hyprland restarts, and a build against them would not load, so this is
        checked before anything is built or the loaded build is unloaded."""
        running, headers = self.commit(), headers_commit()
        if headers != running:
            raise PluginError(
                f"the installed Hyprland headers are from commit {(headers or 'unknown')[:12]}, but the running"
                f" Hyprland is {running[:12]}: Hyprland was upgraded and needs a restart before the plugin can be"
                " built or reloaded. The loaded plugin is left in place.",
                "unavailable",
            )
        return running

    def build(self) -> Path:
        commit = self.running_commit()
        out = build_path(commit)
        out.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["make", "-C", str(SOURCE), f"OUT={out}", f"SEAT={seat_path(commit)}", f"SOURCE_HASH={source_hash()}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            raise PluginError(f"building the plugin failed:\n{result.stderr.strip()[-2000:]}", "unavailable")
        return out

    def load(self) -> Path:
        """Build the plugin for the running Hyprland if needed and load it, replacing a loaded build of other source.
        Succeeds whenever this source's build is loaded afterwards, whoever loaded it."""
        if self.loaded():
            return build_path(self.commit())
        path = build_path(self.running_commit())
        with load_lock():
            # Another deskpilot process may have loaded it while this one waited for the lock.
            if self.loaded():
                return path
            self.unload()
            if not path.exists():
                path = self.build()
            out = self.run(["plugin", "load", str(path)]).strip()
            if out != "ok":
                # A loader outside the lock, such as a Hyprland config `plugin =` line, may have loaded this very
                # build first: the outcome decides, not Hyprland's message.
                with contextlib.suppress(PluginError):
                    status = self.loaded_status() or {}
                    if status.get("version") == source_hash() and status.get("path") == str(path):
                        return path
                raise PluginError(f"hyprctl plugin load {path}: {out}", "unavailable")
        return path


@contextlib.contextmanager
def load_lock() -> Generator[None]:
    """Holds off other deskpilot processes sharing the build directory, so only one builds and loads a build at once.
    The kernel releases the lock when its holder exits, however it exits."""
    lock = build_dir() / "load.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        file = lock.open("a")
    except OSError as error:
        raise PluginError(f"cannot open the plugin load lock {lock}: {error}", "unavailable") from error
    with file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX)
        except OSError as error:
            raise PluginError(f"cannot lock {lock}: {error}", "unavailable") from error
        yield
