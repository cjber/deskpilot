"""Calibrate real input in an owned private desktop, never on the user's windows.

Run with uv, an explicit parent HYPRLAND_INSTANCE_SIGNATURE, and --output outside
this checkout. Optional Chrome checks use the private profile, not a signed-in browser.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from deskpilot import clipboard, keys, session
from deskpilot.a11y import A11yError
from deskpilot.desk import Desk
from deskpilot.hypr import AmbiguousWindow

ROOT = Path(__file__).resolve().parents[1]


def descendants(element):
    yield element
    for child in element.children:
        yield from descendants(child)


def calibrate_wine(desk, output, verify):
    scratch = output.parent / "wine-calibration"
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run(["keeper", "claim", str(scratch)], check=True, capture_output=True)
    binary = scratch / "probe.exe"
    subprocess.run(
        ["winegcc", "-m64", "-mwindows", str(ROOT / "scripts/wine_input_client.c"), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    events = scratch / "events.log"
    with contextlib.suppress(AmbiguousWindow):
        desk.launch(["env", f"DESKPILOT_CALIBRATION_LOG={events}", "WINEDEBUG=-all", str(binary)], timeout=60)
    target = desk.wait_for("Deskpilot Wine calibration", 60)
    assert target is not None
    verify(
        "wine_uses_private_prefix",
        os.environ["WINEPREFIX"].startswith(os.environ["XDG_DATA_HOME"].rsplit("/data", 1)[0]),
    )
    desk.focus(target.address)
    expected = "/run print(type(ForeverCore))"
    desk.type(expected, target.address, delay=0.02, hidden=True)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = events.read_text().splitlines()
        text = "".join(chr(int(row.split()[1])) for row in rows if row.startswith("CHAR "))
        if text == expected:
            break
        time.sleep(0.05)
    verify("wine_punctuation_exact", text == expected)
    desk.shot(target.address, hidden=True)
    desk.click(target.address, 100, 100, hidden=True)
    time.sleep(0.2)
    rows = events.read_text().splitlines()
    verify("wine_pointer_buttons", "DOWN" in rows and "UP" in rows)
    verify("wine_raw_pointer_buttons", "RAW_DOWN" in rows and "RAW_UP" in rows)


def calibrate_x11_pointer(desk, chrome, background, verify):
    html = """<title>Deskpilot X11 pointer calibration</title>
<button id=t onpointerdown="events.value=JSON.stringify({type:event.type,
button:event.button,pointerType:event.pointerType,ctrlKey:event.ctrlKey});
if(event.button===0&&event.pointerType==='mouse'&&!event.ctrlKey)result.value='OPEN'">Trigger</button>
<input id=result aria-label=result value=CLOSED><textarea id=events aria-label=events></textarea>"""
    url = "data:text/html;base64," + base64.b64encode(html.encode()).decode()
    browser = desk.launch(
        [
            chrome,
            "--ozone-platform=x11",
            "--force-renderer-accessibility",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={os.environ['HOME']}/chrome-x11",
            url,
        ]
    )
    verify("chrome_x11_window", browser.xwayland)
    desk.focus(background)
    deadline = time.monotonic() + 5
    trigger = None
    while time.monotonic() < deadline:
        tree = list(descendants(desk.elements(browser.address)))
        trigger = next((e for e in tree if e.name == "Trigger" and e.role == "button"), None)
        if trigger is not None:
            break
        time.sleep(0.05)
    if trigger is None:
        raise RuntimeError("XWayland calibration page did not expose its Trigger button")
    desk.shot(browser.address)
    desk.click(browser.address, None, None, element=trigger.id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        tree = list(descendants(desk.elements(browser.address)))
        if any(e.name == "result" and e.text == "OPEN" for e in tree):
            break
        time.sleep(0.05)
    verify("chrome_x11_unfocused_pointerdown", any(e.name == "result" and e.text == "OPEN" for e in tree))


def main() -> int:  # noqa: PLR0915 - a bounded, sequential live calibration with one cleanup boundary
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=f"calibration-{os.getpid()}")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chrome", action="store_true")
    parser.add_argument("--wine", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or os.environ.get("DESKPILOT_ISOLATED"):
        parser.error("give an explicit parent HYPRLAND_INSTANCE_SIGNATURE outside a private session")
    original = dict(os.environ)
    os.environ.update(DESKPILOT_AGENT=args.agent, DESKPILOT_OWNER_PID=str(os.getpid()))
    parent = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    checks = {}
    processes = []
    desk = None
    private_pid, private_birth, token = 0, None, None
    after = {}

    def parent_state():
        return {
            name: json.loads(subprocess.check_output(["hyprctl", "-i", parent, "-j", name], text=True))
            for name in ("cursorpos", "activewindow")
        }

    before = parent_state()

    def verify(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError(f"calibration failed: {name}")

    def nodes(address):
        assert desk is not None
        return list(descendants(desk.elements(address, hidden=True)))

    def expect_text(name, address, text):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with contextlib.suppress(A11yError):
                if any(e.text == text and "editable" in e.states for e in nodes(address)):
                    verify(name, True)
                    return
            time.sleep(0.05)
        verify(name, False)

    try:
        private_env = session.environment()
        os.environ.update(private_env)
        record = session._read(session._directory() / "ready.json")
        private_pid, private_birth, token = record["pid"], record["birth"], record["token"]
        verify("private_session_identity", session.verify_current() != parent)
        desk = Desk()
        desk.plugin.load()
        target = desk.launch(["/usr/bin/python3", str(ROOT / "scripts/input_client.py")])
        punctuation = "/run print(type(ForeverCore)) []{}!@#$%^&*"
        # This probe intentionally creates two windows in one process.
        with contextlib.suppress(AmbiguousWindow):
            desk.launch(["/usr/bin/python3", str(ROOT / "scripts/gtk4_keyboard_client.py")])
        gtk4 = desk.wait_for("Deskpilot GTK4 target", 5)
        companion = desk.wait_for("Deskpilot GTK4 companion", 5)
        assert gtk4 is not None and companion is not None
        desk.focus(companion.address)
        desk.type(punctuation, gtk4.address, hidden=True, agent_keyboard=True)
        expect_text("gtk4_agent_keyboard_exact", gtk4.address, punctuation)
        verify("gtk4_same_client_focus_preserved", desk.window(companion.address).focused)
        desk.type("COMPANION_EXACT", companion.address)
        expect_text("gtk4_regular_keyboard_after_agent", companion.address, "COMPANION_EXACT")
        desk.type(punctuation, target.address, hidden=True)
        expect_text("regular_keyboard_exact", target.address, punctuation)
        taken = desk.shot(target.address, hidden=True)
        verify("fresh_hidden_capture", Path(taken.path).is_file())
        button = next(e for e in nodes(target.address) if e.name == "Clicks: 0")
        desk.click(target.address, None, None, element=button.id, hidden=True)
        verify("pointer_click", any(e.name == "Clicks: 1" for e in nodes(target.address)))
        # Foreground owners are explicit children, so this probe also cleans them on failure.
        for primary in (False, True):
            command = ["wl-copy", "--foreground", "--type", "text/plain;charset=utf-8"]
            if primary:
                command.append("--primary")
            child = subprocess.Popen(command, stdin=subprocess.PIPE)
            processes.append(child)
            assert child.stdin is not None
            child.stdin.write(b"DESKPILOT_CALIBRATION_SENTINEL")
            child.stdin.close()
        time.sleep(0.2)
        entry = next(e for e in nodes(target.address) if "editable" in e.states)
        desk.click(target.address, None, None, element=entry.id, hidden=True)
        desk.key("ctrl+a", target.address, hidden=True)
        desk.key("ctrl+c", target.address, hidden=True)
        for primary in (False, True):
            command = ["wl-paste", "--no-newline"] + (["--primary"] if primary else [])
            text = subprocess.check_output(command, text=True, timeout=5)
            verify("primary_preserved" if primary else "clipboard_preserved", text == "DESKPILOT_CALIBRATION_SENTINEL")
        desk.key("ctrl+a", target.address, hidden=True)
        desk.paste("EXACT_PRIVATE_PASTE", target.address, hidden=True)
        expect_text("gtk_paste_exact", target.address, "EXACT_PRIVATE_PASTE")
        verify("paste_preserves_clipboard", clipboard.read() == "DESKPILOT_CALIBRATION_SENTINEL")
        if args.chrome:
            chrome = shutil.which("google-chrome-stable") or shutil.which("chromium")
            if not chrome:
                raise RuntimeError("--chrome requires Chrome or Chromium")
            browser = desk.launch(
                [
                    chrome,
                    "--ozone-platform=wayland",
                    "--force-renderer-accessibility",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "data:text/html,<textarea autofocus></textarea>",
                ]
            )
            desk.focus(target.address)
            desk.paste("CHROME_EXACT_PASTE", browser.address)
            expect_text("chrome_private_paste", browser.address, "CHROME_EXACT_PASTE")
            verify("chrome_paste_preserves_clipboard", clipboard.read() == "DESKPILOT_CALIBRATION_SENTINEL")
            desk.key("ctrl+a", browser.address)
            desk.key("BackSpace", browser.address)
            expect_text("chrome_private_select_delete", browser.address, "")
            desk.type("CHROME_TYPED_" * 10, browser.address)
            expect_text("chrome_private_exact_type", browser.address, "CHROME_TYPED_" * 10)
            desk.key("ctrl+l", browser.address)
            desk.type("data:text/html,<textarea autofocus>AFTER</textarea>", browser.address)
            desk.key("Return", browser.address)
            expect_text("chrome_private_chord_type", browser.address, "AFTER")
            verify("chrome_private_focus", desk.window(browser.address).focused)
            # Exercise the ordinary unfocused route inside the private compositor, without the session's focus helper.
            desk.focus(target.address)
            desk.plugin.keys(browser.address, keys.chord_words("ctrl+l"))
            desk.plugin.keys(
                browser.address, keys.text_words("data:text/html,<textarea autofocus>UNFOCUSED</textarea>")
            )
            desk.plugin.keys(browser.address, keys.chord_words("Return"))
            expect_text("chrome_unfocused_chord_navigation", browser.address, "UNFOCUSED")
            desk.plugin.keys(browser.address, keys.chord_words("ctrl+a"))
            desk.plugin.keys(browser.address, keys.chord_words("BackSpace"))
            expect_text("chrome_unfocused_select_delete", browser.address, "")
            verify("chrome_unfocused_focus_preserved", desk.window(target.address).focused)
            calibrate_x11_pointer(desk, chrome, target.address, verify)
        if args.wine:
            calibrate_wine(desk, args.output, verify)
    except Exception as error:
        checks["error"] = f"{type(error).__name__}: {error}"
        if desk and "target" in locals():
            checks["failure_tree"] = desk.elements(target.address, hidden=True).to_dict()
        if desk and "browser" in locals():
            checks["browser_failure_tree"] = desk.elements(browser.address, hidden=True).to_dict()
    finally:
        if desk:
            desk.close()
        for child in processes:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
        os.environ.clear()
        os.environ.update(original)
        os.environ["DESKPILOT_AGENT"] = args.agent
        try:
            session.stop()
            if private_birth:
                checks["private_compositor_stopped"] = session.birth(private_pid) != private_birth
                assert isinstance(token, str)
                checks["owned_processes_stopped"] = not session._tagged(token)
            after = parent_state()
            checks["parent_cursor_unchanged"] = before["cursorpos"] == after["cursorpos"]
            checks["parent_focus_unchanged"] = before["activewindow"].get("address") == after["activewindow"].get(
                "address"
            )
        except Exception as error:
            checks["cleanup_error"] = str(error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "agent": args.agent,
                    "checks": checks,
                    "parent": {
                        "before": {"cursor": before["cursorpos"], "focus": before["activewindow"].get("address")},
                        "after": {
                            "cursor": after.get("cursorpos"),
                            "focus": after.get("activewindow", {}).get("address"),
                        },
                    },
                },
                indent=2,
            )
            + "\n"
        )
        print(args.output.read_text(), end="")
    return int(any(value is not True for value in checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
