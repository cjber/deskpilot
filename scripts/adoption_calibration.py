"""Verify exact-window handoff inside an owned private desktop, never on user applications."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from deskpilot import session
from deskpilot.desk import Desk
from deskpilot.hypr import WindowNotFound
from deskpilot.plugin import Plugin, PluginError

ROOT = Path(__file__).resolve().parents[1]


def nodes(element):
    yield element
    for child in element.children:
        yield from nodes(child)


def main():  # noqa: PLR0915 - sequential live acceptance and owned-resource teardown
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or os.environ.get("DESKPILOT_ISOLATED"):
        parser.error("give an explicit parent HYPRLAND_INSTANCE_SIGNATURE outside a private session")
    chrome = shutil.which("google-chrome-stable") or shutil.which("chromium")
    if not chrome:
        parser.error("Chrome or Chromium is required")
    original = dict(os.environ)
    parent = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    os.environ.update(DESKPILOT_AGENT="adoption-calibration", DESKPILOT_OWNER_PID=str(os.getpid()))
    desk, record, checks, details = None, {}, {}, {}

    def state(signature):
        return {
            key: json.loads(subprocess.check_output(["hyprctl", "-i", signature, "-j", key], text=True))
            for key in ("cursorpos", "activewindow", "monitors")
        }

    def verify(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError(f"adoption calibration failed: {name}")

    def refused(name, action, code):
        try:
            action()
        except PluginError as error:
            verify(name, error.code == code)
        else:
            verify(name, False)

    before = state(parent)
    try:
        os.environ.update(session.environment())
        record = session._read(session._directory() / "ready.json")
        desk = Desk()
        desk.plugin.load()
        desk.hypr._run(["eval", "hl.config({cursor={no_warps=true},input={follow_mouse=3}})"])
        target = desk.launch(
            [
                chrome,
                "--ozone-platform=wayland",
                "--force-renderer-accessibility",
                "--no-first-run",
                "--no-default-browser-check",
                "data:text/html,<title>Adopt target</title><textarea autofocus></textarea>",
            ]
        )
        sibling = desk.launch([chrome, "--new-window", "data:text/html,<title>Sibling</title><textarea></textarea>"])
        background = desk.launch(["/usr/bin/python3", str(ROOT / "scripts/input_client.py")])
        x11 = desk.launch(["env", "GDK_BACKEND=x11", "/usr/bin/python3", str(ROOT / "scripts/input_client.py")])
        desk.focus(background.address)
        background = desk.window(background.address)
        desk.hypr.move_cursor(background.x + 20, background.y + 20)
        time.sleep(0.1)
        # Exercise regular-desktop handoff and hidden input, entirely inside this private compositor.
        desk._isolated = lambda: False
        verify("shared_browser_process", target.pid == sibling.pid)
        private_before = state(os.environ["HYPRLAND_INSTANCE_SIGNATURE"])
        refused("focused_client_refused", lambda: desk.adopt(background.address), "refused")
        sibling = desk.window(sibling.address)
        desk.hypr.move_cursor(sibling.x + sibling.width // 2, sibling.y + sibling.height // 2)
        time.sleep(0.1)
        refused("pointer_in_sibling_refused", lambda: desk.adopt(target.address), "refused")
        desk.hypr.move_cursor(background.x + 20, background.y + 20)
        time.sleep(0.1)
        refused("xwayland_refused", lambda: desk.adopt(x11.address), "refused")
        refused("stale_identity_refused", lambda: desk.plugin.adopt(target.address, "0"), "stale")
        adopted = desk.adopt(target.address)
        workspace = adopted.workspace
        verify("hidden_workspace", workspace == "special:agent-adoption-calibration")
        verify("sibling_unclaimed", desk.plugin.owned_windows([target.address, sibling.address]) == {target.address})
        verify("sibling_workspace_unchanged", desk.window(sibling.address).workspace == sibling.workspace)
        other = Plugin(run=desk.plugin.run, agent="adoption-other")
        other.register()
        refused("other_agent_refused", lambda: other.adopt(target.address, target.stable_id), "refused")
        desk.type("ADOPTED_EXACT", target.address, hidden=True)
        tree = nodes(desk.elements(target.address, hidden=True))
        verify("hidden_text_exact", any(e.text == "ADOPTED_EXACT" and "editable" in e.states for e in tree))
        try:
            desk._target(sibling.address, False, True)
        except WindowNotFound:
            verify("hidden_sibling_refused", True)
        else:
            verify("hidden_sibling_refused", False)
        verify("release_returns_workspace", desk.release(target.address).workspace == target.workspace)
        verify("release_drops_ownership", not desk.plugin.owned_windows([target.address]))
        desk.adopt(target.address)
        desk.hypr._dispatch(
            f'hl.dsp.window.move({{workspace="2",follow=false,window="address:{target.address}"}})',
            f"movetoworkspacesilent 2,address:{target.address}",
        )
        verify("user_move_applied", desk.window(target.address).workspace == "2")
        verify("release_preserves_user_move", desk.release(target.address).workspace == "2")
        desk.hypr._dispatch(
            f'hl.dsp.window.move({{workspace="{target.workspace}",follow=false,window="address:{target.address}"}})',
            f"movetoworkspacesilent {target.workspace},address:{target.address}",
        )
        desk.adopt(target.address)
        desk.stop()
        verify("stop_returns_workspace", desk.window(target.address).workspace == target.workspace)
        verify("stop_keeps_sibling", desk.window(sibling.address).pid == target.pid)
        # Use a separate agent to avoid the stopped agent's five-second process-cleanup grace period.
        keeper = Desk(plugin=Plugin(run=desk.plugin.run, agent="adoption-unload"))
        keeper._isolated = lambda: False
        keeper.adopt(target.address)
        path = str(desk.plugin.build())
        desk.plugin.run(["plugin", "unload", path])
        verify("unload_returns_workspace", desk.window(target.address).workspace == target.workspace)
        verify("unload_keeps_sibling", desk.window(sibling.address).pid == target.pid)
        private_after = state(os.environ["HYPRLAND_INSTANCE_SIGNATURE"])
        details["private_state_differences"] = {
            key: {
                field: [value, private_after[key].get(field)]
                for field, value in private_before[key].items()
                if private_after[key].get(field) != value
            }
            for key in ("activewindow",)
        }
        verify("private_cursor_preserved", private_before["cursorpos"] == private_after["cursorpos"])
        verify(
            "private_focus_preserved",
            private_before["activewindow"]["address"] == private_after["activewindow"]["address"],
        )
        verify(
            "private_view_preserved",
            [(m["id"], m["activeWorkspace"], m["specialWorkspace"]) for m in private_before["monitors"]]
            == [(m["id"], m["activeWorkspace"], m["specialWorkspace"]) for m in private_after["monitors"]],
        )
    finally:
        if desk:
            desk.close()
        os.environ.clear()
        os.environ.update(original)
        os.environ["DESKPILOT_AGENT"] = "adoption-calibration"
        session.stop()
        if record:
            checks["private_compositor_stopped"] = session.birth(record["pid"]) != record["birth"]
            checks["owned_processes_stopped"] = not session._tagged(record["token"])
        after = state(parent)
        checks["parent_cursor_preserved"] = before["cursorpos"] == after["cursorpos"]
        checks["parent_focus_preserved"] = before["activewindow"].get("address") == after["activewindow"].get("address")
        args.output.write_text(json.dumps({"checks": checks, **details}, indent=2) + "\n")
        print(args.output.read_text())
    if not all(checks.values()):
        raise RuntimeError("adoption calibration failed")


if __name__ == "__main__":
    main()
