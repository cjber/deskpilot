"""An agent's private compositor, bus and keyboard, started lazily by --session.

The worker owns every child and publishes its environment only after all services are ready.
The parent Wayland output is disabled before it can map; the fallback launch workspace is hidden.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .hypr import HyprctlError, current_instance, socket_alive
from .plugin import agent_from_env, build_dir


class SessionBusy(RuntimeError):
    code = "busy"


IDLE_SECONDS = 1800
START_TIMEOUT = 30
NATIVE = Path(__file__).with_name("native")
CONFIG = """
hl.monitor({ output = "WAYLAND-1", disabled = true })
hl.monitor({ output = "DESKPILOT", mode = "1280x800", position = "0x0", scale = 1 })
hl.config({
    misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
    animations = { enabled = false },
})
"""


def birth(pid: object) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _run(args: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise HyprctlError(f"{args[0]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _ctl(signature: str, *args: str) -> str:
    return _run(["hyprctl", "-i", signature, *args], dict(os.environ))


def keyboard_binary() -> Path:
    digest = hashlib.sha256(
        b"".join((NATIVE / n).read_bytes() for n in ("keyboard.c", "virtual-keyboard.xml"))
    ).hexdigest()[:16]
    folder = build_dir() / f"keyboard-{digest}"
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / "keyboard"
    with (folder / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.exists():
            return binary
        for kind, name in (("client-header", "virtual-keyboard.h"), ("private-code", "virtual-keyboard-protocol.c")):
            subprocess.run(
                ["wayland-scanner", kind, str(NATIVE / "virtual-keyboard.xml"), str(folder / name)], check=True
            )
        flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "wayland-client", "xkbcommon"], text=True)
        # Built beside the binary and renamed into place, so a killed build never leaves a half-written executable.
        temporary = folder / f".keyboard.{os.getpid()}"
        try:
            subprocess.run(
                [
                    "cc",
                    "-O2",
                    "-I",
                    str(folder),
                    str(NATIVE / "keyboard.c"),
                    str(folder / "virtual-keyboard-protocol.c"),
                    "-o",
                    str(temporary),
                    *shlex.split(flags),
                ],
                check=True,
            )
            os.replace(temporary, binary)
        finally:
            temporary.unlink(missing_ok=True)
    return binary


TOKEN_ENV = "DESKPILOT_SESSION_TOKEN"
PROFILE_DIRS = (
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("HOME", "home"),
    ("WINEPREFIX", "wine"),
)


def _profile() -> dict[str, str]:
    """The agent's private profile directories and the variables that point at them, created once per worker.

    build_dir() reads the worker's own environment, so the keyboard build cache stays where it was.
    """
    root = build_dir() / "agents" / str(agent_from_env())
    values: dict[str, str] = {}
    for key, suffix in PROFILE_DIRS:
        folder = root / suffix
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        values[key] = str(folder)
    values["CHROME_CONFIG_HOME"] = values["XDG_CONFIG_HOME"]
    return values


def _token_ok(token: object) -> bool:
    return isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token) is not None


def _tagged(token: str) -> dict[int, str]:
    """Every process of this user whose launch environment holds exactly this session's token, with its birth."""
    pair = f"{TOKEN_ENV}={token}".encode()
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        started = birth(pid)
        if started is None:
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if pair in environ.split(b"\0") and birth(pid) == started:
            found[pid] = started
    return found


def _signal(pid: int, started: str, sig: int) -> None:
    """Signal a process only while it still has the recorded birth; the pidfd pins it against pid reuse."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return
    try:
        if birth(pid) == started:
            signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def _stop_tagged(token: object, timeout: float = 3) -> bool:
    """Stop every process carrying the session token: TERM, then KILL. True once none remain."""
    if not _token_ok(token):
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGKILL):
        found = _tagged(str(token))
        if not found:
            return True
        for pid, started in found.items():
            _signal(pid, started, sig)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(birth(p) == b for p, b in found.items()):
            time.sleep(0.1)
    return not _tagged(str(token))


def _runtime() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def parent_instance() -> str:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and not os.environ.get("WAYLAND_DISPLAY"):
        agent = agent_from_env()
        records = [_read(p) for p in (_runtime() / "deskpilot" / "sessions").glob(f"{agent}-*/ready.json")]
        parents = {
            r["parent"]
            for r in records
            if r.get("parent") and live(r) and socket_alive(_runtime() / "hypr" / r["parent"] / ".socket.sock")
        }
        if len(parents) == 1:
            return parents.pop()
    try:
        return current_instance()
    except HyprctlError:
        # An agent's own running sessions are not candidate parent desktops.
        listed = json.loads(_run(["hyprctl", "-j", "instances"], dict(os.environ)))
        own = {_read(p).get("signature") for p in (_runtime() / "deskpilot" / "sessions").glob("*/ready.json")}
        candidates = [i for i in listed if i["instance"] not in own]
        display = os.environ.get("WAYLAND_DISPLAY")
        if display:
            candidates = [i for i in candidates if i["wl_socket"] == display]
        if len(candidates) == 1:
            return candidates[0]["instance"]
        raise


VERIFIED_ENV = (
    TOKEN_ENV,
    "WAYLAND_DISPLAY",
    "HYPRLAND_INSTANCE_SIGNATURE",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "AT_SPI_BUS_ADDRESS",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "CHROME_CONFIG_HOME",
    "HOME",
    "WINEPREFIX",
)


def verify_current() -> str:
    """Verify that the caller is inside this agent's live isolated session."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    marker = os.environ.get("DESKPILOT_ISOLATED")
    agent = agent_from_env()
    if not signature or marker != signature or not agent:
        raise HyprctlError("not running in a verified isolated session")
    records = [_read(p) for p in (_runtime() / "deskpilot" / "sessions").glob(f"{agent}-*/ready.json")]
    record = next((r for r in records if r.get("signature") == signature), None)
    if not record or not live(record):
        raise HyprctlError("isolated session identity is not live")
    expected = record.get("env")
    if not isinstance(expected, dict):
        raise HyprctlError("isolated session record has no environment")
    for key in VERIFIED_ENV:
        if not expected.get(key) or os.environ.get(key) != expected[key]:
            raise HyprctlError("isolated session environment changed")
    return signature


def _directory() -> Path:
    agent = agent_from_env()
    if not agent:
        raise ValueError("--session needs DESKPILOT_AGENT to name its private desktop")
    if os.environ.get("DESKPILOT_ISOLATED"):
        signature = verify_current()
        return next(
            p.parent
            for p in (_runtime() / "deskpilot" / "sessions").glob(f"{agent}-*/ready.json")
            if _read(p).get("signature") == signature
        )
    parent = parent_instance()
    tag = hashlib.sha256(parent.encode()).hexdigest()[:12]
    path = _runtime() / "deskpilot" / "sessions" / f"{agent}-{tag}"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_services(path: Path) -> list[dict]:
    """The recorded service births, oldest first; malformed entries are dropped."""
    try:
        value = json.loads((path / "services.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [
        e
        for e in value
        if isinstance(e, dict)
        and isinstance(e.get("pid"), int)
        and not isinstance(e["pid"], bool)
        and e["pid"] > 1
        and isinstance(e.get("birth"), str)
    ]


def _record_service(path: Path, name: str, pid: int) -> None:
    """Record an owned service's exact birth before anything waits on it, so a killed worker leaves it findable."""
    started = birth(pid)
    if started is None:
        return
    entries = [e for e in _read_services(path) if e["pid"] != pid]
    entries.append({"name": name, "pid": pid, "birth": started})
    _atomic_write(path / "services.json", json.dumps(entries))


def _children(pid: int) -> list[int]:
    found: list[int] = []
    for task in Path(f"/proc/{pid}/task").glob("*/children"):
        with contextlib.suppress(OSError, ValueError):
            found.extend(int(c) for c in task.read_text().split())
    return found


def _stop_service(pid: int, started: str) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if birth(pid) != started:
            break
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        if _wait_dead(pid, started, 3):
            break
    return birth(pid) != started


def _reap_services(path: Path) -> bool:
    """Stop every recorded service whose exact birth is still alive, newest first. True once none remain."""
    remaining = [
        e
        for e in reversed(_read_services(path))
        if not (birth(e["pid"]) != e["birth"] or _stop_service(e["pid"], e["birth"]))
    ]
    if remaining:
        _atomic_write(path / "services.json", json.dumps(remaining[::-1]))
        return False
    (path / "services.json").unlink(missing_ok=True)
    shutil.rmtree(path / "services", ignore_errors=True)
    return True


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(value)
    os.replace(temporary, path)


def _prune_owners(path: Path) -> dict[str, str]:
    owners = _read(path / "owners.json")
    living = {pid: started for pid, started in owners.items() if pid.isdigit() and birth(int(pid)) == started}
    if living != owners:
        _atomic_write(path / "owners.json", json.dumps(living))
    return living


def _pipe_line(stream, timeout: float) -> str:
    if not stream:
        raise HyprctlError("private service did not report readiness")
    try:
        if not select.select([stream], [], [], timeout)[0]:
            raise HyprctlError("private service did not report readiness")
    except io.UnsupportedOperation:
        pass
    line = stream.readline().strip()
    if not line:
        raise HyprctlError("private service did not report readiness")
    return line


def _pid_birth(path: Path) -> tuple[int, str | None]:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return 0, None
    return pid, birth(pid)


def live(record: dict) -> bool:
    pid, signature = record.get("pid"), record.get("signature")
    return bool(
        isinstance(signature, str)
        and signature
        and "/" not in signature
        and record.get("birth")
        and record.get("worker_birth")
        and record.get("birth") == birth(pid)
        and record.get("worker_birth") == birth(record.get("worker"))
        and socket_alive(_runtime() / "hypr" / signature / ".socket.sock")
    )


def environment() -> dict[str, str]:
    """Return a verified ready session, serializing concurrent lazy starts without changing the caller's environment."""
    path = _directory()
    with (path / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = _read(path / "ready.json")
        owner = os.environ.get("DESKPILOT_OWNER_PID")
        if owner:
            if not owner.isdigit() or int(owner) <= 1 or birth(int(owner)) is None:
                raise ValueError("DESKPILOT_OWNER_PID must name a live process")
            owners = _prune_owners(path)
            owners[owner] = birth(int(owner)) or ""
            _atomic_write(path / "owners.json", json.dumps(owners))
        if not live(record):
            _reap(path)
            minimum = int(os.environ.get("DESKPILOT_MIN_FREE_MB", "2048"))
            available = next(
                int(row.split()[1]) // 1024
                for row in Path("/proc/meminfo").read_text().splitlines()
                if row.startswith("MemAvailable:")
            )
            if available < minimum:
                running = [str(p.parent.name) for p in path.parent.glob("*/ready.json") if live(_read(p))]
                raise SessionBusy(
                    f"{available} MB available, need {minimum} MB; running sessions: {', '.join(running)}"
                )
            (path / "ready.json").unlink(missing_ok=True)
            (path / "error").unlink(missing_ok=True)
            with (path / "worker.log").open("w") as log:
                worker = subprocess.Popen(
                    [sys.executable, "-m", "deskpilot.session", "serve", str(path), parent_instance()],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                record = _read(path / "ready.json")
                if live(record):
                    break
                if (path / "error").exists() or worker.poll() is not None:
                    detail = (path / "error").read_text() if (path / "error").exists() else "worker exited"
                    raise HyprctlError(f"isolated session failed: {detail}; see {path / 'worker.log'}")
                time.sleep(0.1)
            else:
                worker.terminate()
                worker.wait(timeout=10)
                raise HyprctlError(f"isolated session did not become ready; see {path / 'worker.log'}")
        (path / "used").touch()
        return record["env"]


def stop() -> None:
    path = _directory()
    with (path / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _reap(path)


def _reap(path: Path) -> None:
    record = _read(path / "ready.json") or _read(path / "starting.json")
    try:
        if record:
            _reap_compositor(record)
    finally:
        # Apps and activated services the session started carry its token; records stay until all are gone.
        apps_gone = _stop_tagged(record.get("token"))
        services_gone = _reap_services(path)
    if not (apps_gone and services_gone):
        raise HyprctlError("isolated session apps or services did not stop")
    for name in ("ready.json", "starting.json"):
        (path / name).unlink(missing_ok=True)


def _reap_compositor(record: dict) -> None:
    worker, started = record.get("worker"), record.get("worker_birth")
    if worker and started and birth(worker) == started:
        os.kill(worker, signal.SIGTERM)
        if not _wait_dead(worker, started, 15):
            raise HyprctlError("isolated session worker did not stop")
    pid, started = record.get("pid"), record.get("birth")
    if pid and started and birth(pid) == started:
        _stop_compositor(record.get("signature") or "", pid, started, _runtime())
        if birth(pid) == started:
            raise HyprctlError("isolated session compositor did not stop")


def _service(name: str) -> str:
    for root in ("/usr/lib", "/usr/libexec", "/usr/lib/at-spi2-core"):
        candidate = Path(root) / name
        if candidate.is_file():
            return str(candidate)
    raise HyprctlError(f"install at-spi2-core: {name} was not found")


def _dispatch(signature: str, lua: str, legacy: str) -> str:
    try:
        result = _ctl(signature, "dispatch", lua)
        if result.strip() == "ok":
            return result
    except HyprctlError:
        pass
    return _ctl(signature, "dispatch", *legacy.split(" ", 1))


def _start_compositor(
    path: Path, parent: str, env: dict[str, str], overrides: dict[str, str]
) -> tuple[int, str, str, str]:
    (path / "hyprland.lua").write_text(CONFIG)
    launcher = path / "launch.py"
    launcher.write_text(
        f"#!{sys.executable}\nimport os, json\nfrom pathlib import Path\n"
        f"path = Path({str(path)!r})\n"
        "started = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]\n"
        f"record = {{'pid': os.getpid(), 'birth': started, 'signature': '', 'worker': {os.getpid()}, "
        f"'worker_birth': {birth(os.getpid())!r}, 'token': {overrides.get(TOKEN_ENV)!r}}}\n"
        "temporary = path / '.starting-launcher'\n"
        "temporary.write_text(json.dumps(record))\nos.replace(temporary, path / 'starting.json')\n"
        "(path / 'pid').write_text(str(os.getpid()))\n"
        "for key in ('DISPLAY', 'HL_INITIAL_WORKSPACE_TOKEN'): os.environ.pop(key, None)\n"
        f"os.environ.update({overrides!r})\n"
        "os.environ.update(LIBSEAT_BACKEND='deskpilot-no-drm', HYPRLAND_NO_SD_VARS='1', "
        "HYPRLAND_NO_SD_NOTIFY='1', HYPRLAND_NO_CRASHREPORTER='1')\n"
        "log = os.open(path / 'hyprland.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "os.dup2(log, 1)\nos.dup2(log, 2)\nos.close(log)\n"
        "os.execvp('Hyprland', ['Hyprland', '-c', str(path / 'hyprland.lua')])\n"
    )
    launcher.chmod(0o700)
    (path / "pid").unlink(missing_ok=True)
    command = json.dumps(str(launcher))
    workspace = json.dumps(f"special:agent-{agent_from_env()} silent")
    legacy = f"exec [workspace {workspace.strip(chr(34))}] {launcher}"
    _dispatch(parent, f"hl.dsp.exec_cmd({command}, {{ workspace = {workspace} }})", legacy)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        starting = _read(path / "starting.json")
        candidate, candidate_birth = starting.get("pid", 0), starting.get("birth")
        if candidate_birth and birth(candidate) == candidate_birth:
            instances = json.loads(_ctl(parent, "-j", "instances"))
            match = next((i for i in instances if i["pid"] == candidate), None)
            if match:
                env["WAYLAND_DISPLAY"] = match["wl_socket"]
                env["HYPRLAND_INSTANCE_SIGNATURE"] = match["instance"]
                return candidate, candidate_birth, match["instance"], match["wl_socket"]
        time.sleep(0.1)
    raise HyprctlError("nested Hyprland did not start")


def _wait_dead(pid: int, started: str, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and birth(pid) == started:
        time.sleep(0.1)
    return birth(pid) != started


def _stop_compositor(signature: str, pid: int, started: str, runtime: Path) -> None:
    if birth(pid) != started:
        return
    if not signature:
        with contextlib.suppress(Exception):
            listed = json.loads(_run(["hyprctl", "-j", "instances"], dict(os.environ)))
            signature = next((i["instance"] for i in listed if i["pid"] == pid), "")
    with contextlib.suppress(Exception):
        if signature and socket_alive(runtime / "hypr" / signature / ".socket.sock"):
            for window in json.loads(_ctl(signature, "-j", "clients")):
                address = window["address"]
                _dispatch(
                    signature,
                    f"hl.dsp.window.close({json.dumps('address:' + address)})",
                    f"closewindow address:{address}",
                )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and json.loads(_ctl(signature, "-j", "clients")):
                time.sleep(0.1)
            _dispatch(signature, "hl.dsp.exit()", "exit")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if birth(pid) != started:
            break
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        if _wait_dead(pid, started, 3):
            break
    if signature and birth(pid) is None:
        shutil.rmtree(runtime / "hypr" / signature, ignore_errors=True)


def _start_services(path: Path, env: dict[str, str], keyboard: Path, children: list[subprocess.Popen]) -> Path:
    service_runtime = path / "services"
    service_runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    service_env = {**env, "XDG_RUNTIME_DIR": str(service_runtime)}
    bus = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        env=service_env,
        stdout=subprocess.PIPE,
        text=True,
    )
    children.append(bus)
    _record_service(path, "dbus", bus.pid)
    assert bus.stdout
    env["DBUS_SESSION_BUS_ADDRESS"] = _pipe_line(bus.stdout, 5)
    service_env["DBUS_SESSION_BUS_ADDRESS"] = env["DBUS_SESSION_BUS_ADDRESS"]
    env["NO_AT_BRIDGE"] = "0"
    launcher_proc = subprocess.Popen([_service("at-spi-bus-launcher"), "--launch-immediately"], env=service_env)
    children.append(launcher_proc)
    _record_service(path, "at-spi-bus-launcher", launcher_proc.pid)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        owned = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.NameHasOwner",
                "org.a11y.Bus",
            ],
            service_env,
        )
        if owned.strip() == "(true,)":
            break
        if launcher_proc.poll() is not None:
            raise HyprctlError("accessibility bus launcher exited")
        time.sleep(0.05)
    else:
        raise HyprctlError("accessibility bus did not become ready")
    for child in _children(launcher_proc.pid):
        _record_service(path, "at-spi-bus", child)
    address = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.a11y.Bus",
            "--object-path",
            "/org/a11y/bus",
            "--method",
            "org.a11y.Bus.GetAddress",
        ],
        service_env,
    )
    env["AT_SPI_BUS_ADDRESS"] = ast.literal_eval(address)[0]
    _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.a11y.Bus",
            "--object-path",
            "/org/a11y/bus",
            "--method",
            "org.freedesktop.DBus.Properties.Set",
            "org.a11y.Status",
            "IsEnabled",
            "<true>",
        ],
        service_env,
    )
    registry = subprocess.Popen([_service("at-spi2-registryd"), "--use-gnome-session=false"], env=service_env)
    children.append(registry)
    _record_service(path, "at-spi2-registryd", registry.pid)
    registry_bus_env = {**service_env, "DBUS_SESSION_BUS_ADDRESS": env["AT_SPI_BUS_ADDRESS"]}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        owned = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.NameHasOwner",
                "org.a11y.atspi.Registry",
            ],
            registry_bus_env,
        )
        if "true" in owned:
            break
        if registry.poll() is not None:
            raise HyprctlError("accessibility registry exited")
        time.sleep(0.05)
    else:
        raise HyprctlError("accessibility registry did not become ready")
    kb = subprocess.Popen([str(keyboard)], env=env, stdout=subprocess.PIPE, text=True)
    children.append(kb)
    _record_service(path, "keyboard", kb.pid)
    assert kb.stdout
    if _pipe_line(kb.stdout, 5) != "ready":
        raise HyprctlError("session keyboard did not become ready")
    return service_runtime


def _publish_record(path: Path, env: dict[str, str], pid: int, started: str, signature: str, parent: str) -> None:
    (path / "used").touch()
    record = {
        "pid": pid,
        "birth": started,
        "signature": signature,
        "parent": parent,
        "worker": os.getpid(),
        "worker_birth": birth(os.getpid()),
        "token": env[TOKEN_ENV],
        "env": {
            k: env[k]
            for k in (
                "WAYLAND_DISPLAY",
                "HYPRLAND_INSTANCE_SIGNATURE",
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
                "NO_AT_BRIDGE",
                "AT_SPI_BUS_ADDRESS",
                "DESKPILOT_ISOLATED",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "CHROME_CONFIG_HOME",
                "HOME",
                "WINEPREFIX",
                TOKEN_ENV,
            )
        },
    }
    _atomic_write(path / "ready.json", json.dumps(record))


def _try_lock(path: Path):
    """The session lock if nobody holds it, else None. The worker never waits on it: environment() holds it while
    it waits for this very worker to become ready, and stop() holds it while it waits for the worker to exit."""
    lock = (path / "lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def _session_loop(path: Path, parent: str, pid: int, started: str, signature: str, children: list[subprocess.Popen]):
    """Run until the session should end. On an owner or idle exit, return the held session lock: it stays held
    through teardown so no environment() call can adopt a session that is already going away."""
    had_owners = bool(_read(path / "owners.json"))

    def finished() -> bool:
        nonlocal had_owners
        living = _prune_owners(path)
        had_owners = had_owners or bool(living)
        if had_owners and not living:
            return True
        return not living and time.time() - (path / "used").stat().st_mtime >= IDLE_SECONDS

    while True:
        if birth(pid) != started or any(c.poll() is not None for c in children):
            raise HyprctlError("an isolated session service exited")
        if any(c.get("pid") == pid and c.get("mapped", True) for c in json.loads(_ctl(parent, "-j", "clients"))):
            raise HyprctlError("nested compositor mapped a parent window")
        if finished():
            lock = _try_lock(path)
            if lock is not None:
                # environment() registers an owner and touches `used` under this lock: decide again with it held.
                if finished():
                    return lock
                lock.close()
        time.sleep(1)


def serve(path: Path, parent: str) -> None:
    children: list[subprocess.Popen] = []
    compositor_pid: int | None = None
    compositor_birth: str | None = None
    signature = ""
    held = None
    runtime = _runtime()
    # A fresh token per worker, never one inherited from the environment: it names exactly what this session started.
    token = secrets.token_hex(16)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        keyboard = keyboard_binary()
        # The profile is computed once, before the compositor and every service, and all of them get the same one.
        overrides = {**_profile(), TOKEN_ENV: token}
        env.update(overrides)
        _atomic_write(
            path / "starting.json",
            json.dumps({"worker": os.getpid(), "worker_birth": birth(os.getpid()), "token": token}),
        )
        compositor_pid, compositor_birth, signature, _ = _start_compositor(path, parent, env, overrides)
        _atomic_write(
            path / "starting.json",
            json.dumps(
                {
                    "pid": compositor_pid,
                    "birth": compositor_birth,
                    "signature": signature,
                    "worker": os.getpid(),
                    "worker_birth": birth(os.getpid()),
                    "token": token,
                }
            ),
        )
        _ctl(signature, "output", "create", "headless", "DESKPILOT")
        _dispatch(signature, 'hl.dsp.focus({ monitor = "DESKPILOT" })', "focusmonitor DESKPILOT")
        _ctl(signature, "dismissnotify")
        env.pop("DISPLAY", None)
        env.pop("AT_SPI_BUS_ADDRESS", None)
        _start_services(path, env, keyboard, children)
        env["DESKPILOT_ISOLATED"] = signature
        _publish_record(path, env, compositor_pid, compositor_birth, signature, parent)
        held = _session_loop(path, parent, compositor_pid, compositor_birth, signature, children)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        (path / "error").write_text(str(error))
    finally:
        if compositor_pid is None:
            starting = _read(path / "starting.json")
            compositor_pid, compositor_birth = starting.get("pid"), starting.get("birth")
            signature = starting.get("signature", "")
        _teardown(path, token, children, compositor_pid, compositor_birth, signature, held)


def _teardown(
    path: Path,
    token: str,
    children: list[subprocess.Popen],
    pid: int | None,
    started: str | None,
    signature: str,
    held,
) -> None:
    try:
        if pid and started:
            _stop_compositor(signature, pid, started, _runtime())
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        apps_gone = _stop_tagged(token)
        services_gone = _reap_services(path)
        compositor_gone = not (pid and started and birth(pid) == started)
        if compositor_gone and apps_gone and services_gone:
            # Records go only once everything the session started is gone; otherwise _reap finishes the job.
            (path / "ready.json").unlink(missing_ok=True)
            (path / "starting.json").unlink(missing_ok=True)
        if held is not None:
            held.close()


if __name__ == "__main__":
    serve(Path(sys.argv[2]), sys.argv[3])
