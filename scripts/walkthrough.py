"""Record and export the README walkthrough: deskpilot operating a native GTK app in a throwaway Hyprland.

    uv run python scripts/walkthrough.py [--scratch DIR] [--export-only]

Run from a Hyprland session (WAYLAND_DISPLAY set). It starts a nested Hyprland (scripts/nested.py), launches
scripts/walkthrough_app.py in it with the system python3 (PyGObject, GTK 4, librsvg), has deskpilot select a file,
open the export dialog and confirm it, and records the nested output with wf-recorder, so the video holds the real
compositor cursor. Needs wf-recorder and ffmpeg. --export-only re-encodes DIR/capture.mp4 into assets/walkthrough/
without launching anything. Raw captures, logs and evidence stay in the scratch directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from nested import start, stop

from deskpilot.desk import Desk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "walkthrough"
APP = ROOT / "scripts" / "walkthrough_app.py"
TITLE = "Deskpilot local demo"
# The app tiles to this size on the 1280x800 nested output; any other layout means the click points below are wrong.
SIZE = (1238, 758)
# Window-local points in a shot of the app: the logo.svg checkbox, "Export selected", and "Export asset" in the dialog.
CHECKBOX, EXPORT, CONFIRM = (81, 247), (1110, 712), (734, 442)
RECORDER = [
    *("wf-recorder", "-y", "--no-dmabuf", "-D", "-r", "60", "-o", "DPTEST"),
    *("-c", "libx264", "-p", "preset=ultrafast", "-p", "crf=18", "-p", "threads=2", "-x", "yuv420p"),
]


def expect(state: dict, **fields) -> dict:
    wrong = {k: state.get(k) for k, v in fields.items() if state.get(k) != v}
    if wrong:
        raise RuntimeError(f"app state differs from {fields}: {wrong}")
    return state


def stop_process(process: subprocess.Popen | None, sig: int = signal.SIGTERM, timeout: float = 15) -> None:
    """Signal a process this script started and wait for it, then kill it if it is still up."""
    if process is None or process.poll() is not None:
        return
    process.send_signal(sig)
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def pause(procs: dict[str, subprocess.Popen], seconds: float) -> None:
    """A deliberate beat for the viewer; it fails at once if the app or the recorder has died."""
    end = time.monotonic() + seconds
    while True:
        for name, process in procs.items():
            if process.poll() is not None:
                raise RuntimeError(f"the {name} exited early with {process.returncode}")
        if time.monotonic() >= end:
            return
        time.sleep(0.05)


def drive(desk: Desk, session: dict, env: dict, scratch: Path, procs: dict[str, subprocess.Popen]) -> None:
    """Select logo.svg, open the export dialog and confirm it, checking the app's own state after every click."""
    app_dir = scratch / "app"

    def state() -> dict:
        return json.loads((app_dir / "state.json").read_text())

    window = desk.wait_for(TITLE, timeout=15, size=SIZE)
    assert window is not None
    desk.hypr.move_cursor(session["x"] + 8, session["y"] + 785)
    before = desk.shot(window.address, scratch / "before.png")
    desk.move(window.address, 720, 440, shot=before.id)
    with (scratch / "recorder.log").open("w") as log:
        procs["recorder"] = subprocess.Popen(
            [*RECORDER, "-f", str(scratch / "capture.mp4")], env=env, stdout=log, stderr=log
        )
    pause(procs, 1)  # reading beats for the viewer; the checks below are what prove each step
    desk.click(window.address, *CHECKBOX, shot=before.id)
    selected, _ = desk.wait_changed(window.address, before.id, timeout=5)
    chosen = expect(state(), selected=True, dialog=False, exported=False, files=["logo.svg"])
    pause(procs, 1.1)
    desk.click(window.address, *EXPORT, shot=selected.id)
    opened, _ = desk.wait_changed(window.address, selected.id, timeout=5)
    dialog = expect(state(), dialog=True, exported=False, files=["logo.svg"])
    pause(procs, 1.1)
    desk.click(window.address, *CONFIRM, shot=opened.id)
    finished, _ = desk.wait_changed(window.address, opened.id, timeout=5)
    done = expect(state(), dialog=False, exported=True, files=["logo.svg"])
    exported = (app_dir / "library" / "logo.svg").read_bytes()
    if exported != (ROOT / "assets" / "logo.svg").read_bytes():
        raise RuntimeError("the exported logo.svg differs from assets/logo.svg")
    pause(procs, 2)
    steps = ((CHECKBOX, selected, chosen), (EXPORT, opened, dialog), (CONFIRM, finished, done))
    evidence = {
        "window": window.to_dict(),
        "steps": [{"click": list(point), "shot": shot.to_dict(), "state": st} for point, shot, st in steps],
        "exported_bytes": len(exported),
    }
    (scratch / "evidence.json").write_text(json.dumps(evidence, indent=2))


def record(scratch: Path) -> None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("WAYLAND_DISPLAY is not set; run this inside a Hyprland session")
    app_dir = scratch / "app"
    for stale in (app_dir / "state.json", app_dir / "library" / "logo.svg"):
        stale.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    saved = dict(os.environ)
    session = desk = None
    procs: dict[str, subprocess.Popen] = {}
    try:
        session = start()
        env = {
            **os.environ,
            "WAYLAND_DISPLAY": session["wayland"],
            "HYPRLAND_INSTANCE_SIGNATURE": session["signature"],
            "DESKPILOT_AGENT": "DESKPILOT",
        }
        os.environ.update(env)
        desk = Desk()
        app_env = {**env, "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo", "DESKPILOT_DEMO_DIR": str(app_dir)}
        with (scratch / "app.log").open("w") as log:
            procs["app"] = subprocess.Popen(["/usr/bin/python3", str(APP)], env=app_env, stdout=log, stderr=log)
        drive(desk, session, env, scratch, procs)
    finally:
        # ExitStack runs every cleanup even if another resource fails to stop.
        with ExitStack() as cleanup:
            if session is not None:
                cleanup.callback(stop, session["signature"])
            cleanup.callback(os.environ.update, saved)
            cleanup.callback(os.environ.clear)
            cleanup.callback(stop_process, procs.get("app"))
            if desk is not None:
                cleanup.callback(desk.close)
            cleanup.callback(stop_process, procs.get("recorder"), signal.SIGINT)


def ffmpeg(*args: str | Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-threads",
            "2",
            "-filter_threads",
            "2",
            "-filter_complex_threads",
            "2",
            *map(str, args[:-1]),
            "-threads",
            "2",
            str(args[-1]),
        ],
        check=True,
    )


def export(scratch: Path) -> None:
    capture = scratch / "capture.mp4"
    if not capture.is_file() or capture.stat().st_size == 0:
        sys.exit(f"no capture at {capture}; run without --export-only first")
    OUT.mkdir(parents=True, exist_ok=True)
    video, palette = OUT / "walkthrough.mp4", scratch / "palette.png"
    ffmpeg(
        *("-i", capture, "-an", "-vf", "setparams=range=limited,fps=60", "-c:v", "libx264", "-crf", "18"),
        *("-preset", "fast", "-pix_fmt", "yuv420p", "-color_range", "tv", "-movflags", "+faststart", video),
    )
    ffmpeg("-ss", "6.3", "-i", video, "-frames:v", "1", OUT / "walkthrough.png")
    # Two passes, so ffmpeg never buffers the whole clip to split it for the palette.
    scale = "fps=50,scale=1120:-1:flags=lanczos"
    ffmpeg("-i", video, "-vf", f"{scale},palettegen=max_colors=128", "-frames:v", "1", "-update", "1", palette)
    ffmpeg(
        *("-i", video, "-i", palette, "-filter_complex", f"{scale}[x];[x][1:v]paletteuse", "-loop", "0"),
        OUT / "walkthrough.gif",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--scratch", type=Path, default=Path.home() / ".cache" / "agent-scratch" / "deskpilot-walkthrough"
    )
    parser.add_argument("--export-only", action="store_true", help="re-export SCRATCH/capture.mp4 without recording")
    args = parser.parse_args()
    scratch = args.scratch.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    if shutil.which("keeper"):
        subprocess.run(["keeper", "claim", str(scratch)], capture_output=True, check=False)
    if not args.export_only:
        (scratch / "capture.mp4").unlink(missing_ok=True)
        record(scratch)
    export(scratch)
    print(json.dumps({"scratch": str(scratch), "outputs": sorted(p.name for p in OUT.glob("walkthrough.*"))}))


if __name__ == "__main__":
    main()
