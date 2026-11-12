"""Check popup grab ownership in an owned private desktop.

Give an explicit parent HYPRLAND_INSTANCE_SIGNATURE and an --output outside this
checkout. --plugin can load a baseline build for a red/green comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import time
from pathlib import Path

from input_calibration import ROOT, keyboard_control

from deskpilot import session
from deskpilot.desk import Desk
from deskpilot.plugin import Plugin, PluginError


class BaselinePlugin(Plugin):
    def _mismatch(self, refusal: PluginError) -> PluginError:
        return refusal


def build_client(output):
    scratch = output.parent / "popup-calibration"
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run(["keeper", "claim", str(scratch)], check=True, capture_output=True)
    xml = Path("/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
    for mode, name in (("client-header", "xdg-shell-client-protocol.h"), ("private-code", "xdg-shell-protocol.c")):
        subprocess.run(["wayland-scanner", mode, str(xml), str(scratch / name)], check=True, capture_output=True)
    binary = scratch / "popup-grab-client"
    subprocess.run(
        [
            "cc",
            str(ROOT / "scripts/popup_grab_client.c"),
            str(scratch / "xdg-shell-protocol.c"),
            "-I",
            str(scratch),
            "-lwayland-client",
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    return binary


def wait_until(predicate, message):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(message)


def probe(desk, binary, output, background, user, verify, unload=False):
    name = "unloaded_user" if unload else "user" if user else "agent"
    events = output.parent / f"popup-{name}-events.log"
    trigger = output.parent / f"popup-{name}-trigger-{os.getpid()}"
    target = desk.launch(
        [
            "env",
            f"PROBE_EVENTS={events}",
            f"PROBE_GRAB_FILE={trigger}",
            *(["PROBE_USER_KEY=1"] if user else []),
            str(binary),
        ]
    )
    desk.focus(target.address)
    background = desk.window(background)
    desk.hypr.move_cursor(background.x + background.width // 2 + 1, background.y + background.height // 2 + 1)
    time.sleep(0.1)
    target = desk.window(target.address)
    cursor = desk.hypr.cursor()
    verify(f"{name}_keyboard_on_target", target.focused)
    desk.plugin.path(target.address, "hidden", [f"m{target.x + 20},{target.y + 20}", "d50", "p272", "r272"])
    wait_until(lambda: events.exists() and "CLICK " in events.read_text(), "agent click did not reach probe")
    control = keyboard_control(output)
    assert control.stdin is not None and control.stdout is not None
    try:
        for command in ("key 29 1", "mods 4"):
            control.stdin.write(command + "\n")
            control.stdin.flush()
            if control.stdout.readline().strip() != "done":
                raise RuntimeError("private user keyboard failed")
        wait_until(lambda: "KEY 29 1" in events.read_text(), "intervening user input did not reach probe")
        if unload:
            desk.plugin.unload()
        trigger.write_text("grab")
        wait_until(lambda: "POPUP" in events.read_text(), "popup did not map")
        if unload:
            desk.plugin.load()
        wait_until(lambda: bool(desk.plugin.popups(target.address)), "compositor did not map popup")
        succeeded, error = True, None
        try:
            desk.plugin.path(target.address, "hidden", [f"m{target.x + 20},{target.y + 20}", "p272", "r272"])
        except PluginError as exc:
            succeeded, error = False, exc
        verify(
            f"{name}_grab_ownership",
            not succeeded and isinstance(error, PluginError) and error.code == "refused" if user else succeeded,
        )
        verify(f"{name}_cursor_preserved", desk.hypr.cursor() == cursor)
        verify(f"{name}_log_private", stat.S_IMODE(events.stat().st_mode) == 0o600)
    finally:
        if control.poll() is None:
            control.stdin.write("mods 0\nkey 29 0\nquit\n")
            control.stdin.flush()
            try:
                control.wait(timeout=5)
            except subprocess.TimeoutExpired:
                control.kill()
                control.wait()
        desk.hypr._dispatch(
            f'hl.dsp.window.close({{window="address:{target.address}"}})', f"closewindow address:{target.address}"
        )
        wait_until(lambda: all(w.address != target.address for w in desk.windows()), "probe window did not close")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=f"popup-calibration-{os.getpid()}")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plugin", type=Path)
    args = parser.parse_args()
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or os.environ.get("DESKPILOT_ISOLATED"):
        parser.error("give an explicit parent HYPRLAND_INSTANCE_SIGNATURE outside a private session")
    original = dict(os.environ)
    os.environ.update(DESKPILOT_AGENT=args.agent, DESKPILOT_OWNER_PID=str(os.getpid()))
    desk, checks, private_pid, private_birth, token = None, {}, None, None, None
    parent = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]

    def parent_state():
        return {
            name: json.loads(subprocess.check_output(["hyprctl", "-i", parent, "-j", name], text=True))
            for name in ("cursorpos", "activewindow")
        }

    before = parent_state()

    def verify(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError(f"popup calibration failed: {name}")

    try:
        os.environ.update(session.environment())
        record = session._read(session._directory() / "ready.json")
        private_pid, private_birth, token = record["pid"], record["birth"], record["token"]
        desk = Desk(plugin=BaselinePlugin() if args.plugin else None)
        if args.plugin:
            desk.plugin.run(["plugin", "load", str(args.plugin)])
        else:
            desk.plugin.load()
        # Full loose focus keeps the keyboard on the probe while the pointer enters the companion.
        result = desk.hypr._run(["eval", "hl.config({cursor={no_warps=true},input={follow_mouse=3}})"])
        verify("private_focus_configuration", result.strip() == "ok")
        binary = build_client(args.output)
        background = desk.launch(["/usr/bin/python3", str(ROOT / "scripts/input_client.py")])
        probe(desk, binary, args.output, background.address, False, verify)
        probe(desk, binary, args.output, background.address, True, verify)
        if not args.plugin:
            probe(desk, binary, args.output, background.address, True, verify, unload=True)
    finally:
        if desk:
            desk.close()
        os.environ.clear()
        os.environ.update(original)
        os.environ["DESKPILOT_AGENT"] = args.agent
        session.stop()
        if private_pid:
            checks.update(
                private_compositor_stopped=session.birth(private_pid) != private_birth,
                owned_processes_stopped=not session._tagged(token),
            )
        after = parent_state()
        checks.update(
            parent_cursor_preserved=before["cursorpos"] == after["cursorpos"],
            parent_focus_preserved=before["activewindow"].get("address") == after["activewindow"].get("address"),
        )
        args.output.write_text(json.dumps({"checks": checks}, indent=2) + "\n")
        print(args.output.read_text())
    if not all(checks.values()):
        raise RuntimeError("popup calibration checks failed")


if __name__ == "__main__":
    raise SystemExit(main())
