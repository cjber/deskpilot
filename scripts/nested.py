"""A throwaway Hyprland inside the user's, for live checks of the plugin that must not touch the user's screen.

    python3 scripts/nested.py start    # prints {"signature", "wayland", "output", "x", "y", "width", "height", "pid"}
    python3 scripts/nested.py stop SIGNATURE

The nested compositor's own window opens silently on the hidden special:dptest workspace of the user's session and is
killed if it lands anywhere else. Inside it, a headless output (DPTEST) renders on its own timer, so it needs no frames
from the parent; it sits far from the window's own output, which gets none. Point deskpilot at it with
HYPRLAND_INSTANCE_SIGNATURE=<signature>, and capture it with `WAYLAND_DISPLAY=<wayland> grim -o DPTEST`. It reads a
minimal config of its own, so none of the user's autostarts run, and HYPRLAND_NO_SD_* keep it out of the user's
systemd environment. Hyprland shows a banner for being started without start-hyprland; `hyprctl dismissnotify` clears
it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr"
WORKSPACE = "special:dptest"
OUTPUT = "DPTEST"
CONFIG = """
hl.monitor({ output = "", mode = "1280x800", position = "0x0", scale = 1 })
-- Well clear of the nested window's own output: `grim -o` captures every output that intersects the one named, and
-- that one gets no frames while its window is on a hidden workspace, so an overlap stalls or fails the capture.
hl.monitor({ output = "DPTEST", mode = "1280x800", position = "10000x0", scale = 1 })
hl.config({
    misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
    animations = { enabled = false },
})
"""


def alive(signature: str) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(str(RUNTIME / signature / ".socket.sock"))
        except OSError:
            return False
    return True


def hyprctl(signature: str, *args: str) -> str:
    env = {**os.environ, "HYPRLAND_INSTANCE_SIGNATURE": signature}
    return subprocess.run(["hyprctl", *args], env=env, capture_output=True, text=True, check=True).stdout


def user_instance() -> str:
    listed = subprocess.run(["hyprctl", "-j", "instances"], capture_output=True, text=True, check=True)
    instances = json.loads(listed.stdout)
    live = [i["instance"] for i in instances if alive(i["instance"])]
    own = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if own is not None and own in live:
        return own
    if len(live) != 1:
        sys.exit(f"cannot tell the user's Hyprland apart: {live}")
    return live[0]


def start() -> dict:
    parent = user_instance()
    scratch = Path.home() / ".cache" / "agent-scratch" / "deskpilot-nested"
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run(["keeper", "claim", str(scratch)], capture_output=True, check=False)
    config, pidfile, log = scratch / "hyprland.lua", scratch / "pid", scratch / "log"
    config.write_text(CONFIG)
    pidfile.unlink(missing_ok=True)
    launcher = scratch / "run.sh"
    launcher.write_text(
        f"#!/bin/sh\necho $$ > {pidfile}\nexec env -u DISPLAY HYPRLAND_NO_SD_VARS=1 HYPRLAND_NO_SD_NOTIFY=1 "
        f"HYPRLAND_NO_CRASHREPORTER=1 Hyprland -c {config} > {log} 2>&1\n"
    )
    launcher.chmod(0o755)
    before = {p.name for p in RUNTIME.iterdir()}

    # Watch the parent's events before launching, so the nested window's placement is seen as it opens.
    events = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    events.connect(str(RUNTIME / parent / ".socket2.sock"))
    events.settimeout(0.2)
    # The Lua table form: the "[workspace ...]" string prefix is ignored by 0.57's exec_cmd.
    hyprctl(parent, "dispatch", f'hl.dsp.exec_cmd("{launcher}", {{ workspace = "{WORKSPACE} silent" }})')

    deadline, buffer, placed, signature = time.monotonic() + 15, "", False, None
    while time.monotonic() < deadline and not (placed and signature):
        with contextlib.suppress(TimeoutError):
            buffer += events.recv(65536).decode(errors="replace")
        *lines, buffer = buffer.split("\n")
        for line in lines:
            if line.startswith("openwindow>>") and line.split(",")[2] == "aquamarine":
                if line.split(",")[1] != WORKSPACE:
                    stop_pid(pidfile)
                    sys.exit(f"the nested window opened outside {WORKSPACE} ({line}); it was killed")
                placed = True
        new = [n for n in {p.name for p in RUNTIME.iterdir()} - before if alive(n)]
        signature = new[0] if new else None
    events.close()
    if not (placed and signature):
        stop_pid(pidfile)
        sys.exit(f"the nested Hyprland did not come up; see {log}")

    hyprctl(signature, "output", "create", "headless", OUTPUT)
    for _ in range(50):
        monitors = json.loads(hyprctl(signature, "-j", "monitors"))
        if any(m["name"] == OUTPUT for m in monitors):
            break
        time.sleep(0.1)
    monitor = next(m for m in monitors if m["name"] == OUTPUT)
    hyprctl(signature, "dispatch", f'hl.dsp.focus({{ monitor = "{OUTPUT}" }})')
    wayland = next(i["wl_socket"] for i in json.loads(hyprctl(parent, "-j", "instances")) if i["instance"] == signature)
    return {
        "signature": signature,
        "wayland": wayland,
        "output": OUTPUT,
        "x": monitor["x"],
        "y": monitor["y"],
        "width": monitor["width"],
        "height": monitor["height"],
        "pid": int(pidfile.read_text()),
    }


def stop_pid(pidfile: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        os.kill(int(pidfile.read_text()), signal.SIGTERM)


def stop(signature: str) -> None:
    pidfile = Path.home() / ".cache" / "agent-scratch" / "deskpilot-nested" / "pid"
    if alive(signature):
        hyprctl(signature, "dispatch", "hl.dsp.exit()")
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            print(json.dumps({"stopped": signature}))
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGTERM)
    print(json.dumps({"stopped": signature, "killed": pid}))


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["start"]:
            print(json.dumps(start()))
        case ["stop", signature]:
            stop(signature)
        case _:
            sys.exit(__doc__)
