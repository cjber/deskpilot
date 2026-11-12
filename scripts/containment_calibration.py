"""Run the agent-seat pointer containment probe in an isolated nested compositor.

The probe loads deskpilot before connecting, binds the named agent seat, requests inert
relative-pointer and pointer-constraint objects, and keeps them alive while this runner
dispatches real compositor cursor motion. It exits nonzero on protocol errors or events.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nested import SCRATCH, process_birth, recorded

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "scripts" / "containment_client.c"


def prepare(signature: str) -> tuple[dict, dict[str, str], Path]:
    env = {**os.environ, "HYPRLAND_INSTANCE_SIGNATURE": signature}
    wayland = subprocess.check_output(["hyprctl", "-j", "instances"], env=env, text=True)
    instance = next(row for row in json.loads(wayland) if row["instance"] == signature)
    runs = [r for r in SCRATCH.glob("run-*") if recorded(r) == signature]
    if len(runs) != 1:
        raise RuntimeError("refusing a compositor not owned by scripts/nested.py")
    run = runs[0]
    if int((run / "pid").read_text()) != instance["pid"] or (run / "birth").read_text() != process_birth(
        instance["pid"]
    ):
        raise RuntimeError("nested compositor process identity changed")
    runtime = Path.home() / ".cache" / "agent-scratch" / "deskpilot-containment-run"
    runtime.mkdir(parents=True, exist_ok=True)
    subprocess.run(["keeper", "claim", str(runtime)], check=False, stdout=subprocess.DEVNULL)
    monitors = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"], env=env, text=True))
    if not any(m.get("name") == "DPTEST" for m in monitors):
        raise RuntimeError("refusing non-nested compositor: headless DPTEST output is absent")
    proc_cmdline = Path(f"/proc/{instance['pid']}/cmdline")
    if not proc_cmdline.is_file() or b"Hyprland" not in proc_cmdline.read_bytes():
        raise RuntimeError("refusing compositor whose recorded pid is not Hyprland")
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run([sys.executable, "-m", "deskpilot.cli", "plugin", "load"], env=env, check=True)
    return instance, env, runtime


def main() -> int:
    if len(sys.argv) not in (2, 3) or (
        len(sys.argv) == 3 and sys.argv[2] not in ("--negative", "--negative-constraints")
    ):
        print(f"usage: {sys.argv[0]} <nested-signature> [--negative|--negative-constraints]", file=sys.stderr)
        return 2
    negative = len(sys.argv) == 3
    constraints = negative and sys.argv[2] == "--negative-constraints"
    instance, env, runtime = prepare(sys.argv[1])
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=runtime))
    proc = None
    try:
        proto = Path("/usr/share/wayland-protocols/unstable")
        rel_xml = proto / "relative-pointer/relative-pointer-unstable-v1.xml"
        lock_xml = proto / "pointer-constraints/pointer-constraints-unstable-v1.xml"
        xdg_xml = Path("/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
        virtual_xml = ROOT / "scripts/protocols/virtual-pointer.xml"
        for xml, stem in (
            (rel_xml, "relative-pointer-unstable-v1"),
            (lock_xml, "pointer-constraints-unstable-v1"),
            (xdg_xml, "xdg-shell"),
            (virtual_xml, "wlr-virtual-pointer-unstable-v1"),
        ):
            subprocess.run(
                ["wayland-scanner", "client-header", str(xml), str(run_dir / f"{stem}-client-protocol.h")], check=True
            )
            subprocess.run(
                ["wayland-scanner", "private-code", str(xml), str(run_dir / f"{stem}-protocol.c")], check=True
            )
        binary = run_dir / "containment-client"
        subprocess.run(
            [
                "cc",
                "-O2",
                str(CLIENT),
                str(run_dir / "relative-pointer-unstable-v1-protocol.c"),
                str(run_dir / "pointer-constraints-unstable-v1-protocol.c"),
                str(run_dir / "xdg-shell-protocol.c"),
                str(run_dir / "wlr-virtual-pointer-unstable-v1-protocol.c"),
                "-I",
                str(run_dir),
                "-lwayland-client",
                "-o",
                str(binary),
            ],
            check=True,
        )
        client_env = {
            **env,
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "WAYLAND_DISPLAY": instance["wl_socket"],
        }
        proc = subprocess.Popen(
            [str(binary), "3", "constraints" if constraints else "negative"] if negative else [str(binary), "3"],
            env=client_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = proc.stdout.readline() if proc.stdout and select.select([proc.stdout], [], [], 5)[0] else ""
        if ready.strip() != "READY":
            detail = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"containment client did not become ready: {ready.strip()} stderr={detail.strip()}")
        for i in range(15):
            x, y = (10020 + i * 10, 100 + i * 5)
            subprocess.run(
                ["hyprctl", "dispatch", f"hl.dsp.cursor.move({{x={x},y={y}}})"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                text=True,
            )
            time.sleep(0.1)
        out, error = proc.communicate(timeout=5)
        print(out, end="")
        if error:
            print(error, file=sys.stderr, end="")
        if negative and proc.returncode == 0:
            raise RuntimeError("negative control unexpectedly passed: user-seat constraints were inert")
        if not negative and proc.returncode != 0:
            raise RuntimeError(f"positive containment failed with status {proc.returncode}")
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for path in sorted(run_dir.iterdir(), reverse=True):
            path.unlink()
        run_dir.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
