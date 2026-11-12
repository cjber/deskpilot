import io
import json
from pathlib import Path

import pytest

from deskpilot import session


def test_atomic_write_never_leaves_partial_json(tmp_path):
    target = tmp_path / "owners.json"
    session._atomic_write(target, json.dumps({"123": "birth"}))
    assert json.loads(target.read_text()) == {"123": "birth"}
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*owners.json.*"))


def test_pipe_line_reads_ready_without_waiting():
    assert session._pipe_line(io.StringIO("ready\n"), 0.01) == "ready"


def test_pipe_line_rejects_empty_service():
    with pytest.raises(session.HyprctlError, match="readiness"):
        session._pipe_line(io.StringIO(""), 0.01)


def test_prune_owners_removes_dead_and_malformed_entries(tmp_path, monkeypatch):
    registry = tmp_path / "owners.json"
    registry.write_text(json.dumps({"12": "live", "bad": "x"}))
    monkeypatch.setattr(session, "birth", lambda pid: "live" if pid == 12 else None)
    assert session._prune_owners(tmp_path) == {"12": "live"}
    assert json.loads(registry.read_text()) == {"12": "live"}


@pytest.fixture
def private_record(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": "nested",
        "DESKPILOT_ISOLATED": "nested",
        "WAYLAND_DISPLAY": "wayland-nested",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/dbus",
        "AT_SPI_BUS_ADDRESS": "unix:path=/private/a11y",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "HOME": str(tmp_path / "profile-home"),
        "WINEPREFIX": str(tmp_path / "wine"),
        "DESKPILOT_SESSION_TOKEN": "a" * 32,
        "XDG_CACHE_HOME": "/private/cache",
        "XDG_CONFIG_HOME": "/private/config",
        "XDG_DATA_HOME": "/private/data",
        "CHROME_CONFIG_HOME": "/private/config",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    record = {
        "pid": 101,
        "birth": "compositor",
        "worker": 102,
        "worker_birth": "worker",
        "signature": "nested",
        "env": env,
    }
    folder = tmp_path / "deskpilot" / "sessions" / "tester-parent"
    folder.mkdir(parents=True)
    target = folder / "ready.json"
    target.write_text(json.dumps(record))
    monkeypatch.setattr(session, "birth", {101: "compositor", 102: "worker"}.get)
    monkeypatch.setattr(session, "socket_alive", lambda path: True)
    return target, record


def test_verified_private_environment_selects_its_compositor(private_record):
    from deskpilot.hypr import current_instance

    assert session.verify_current() == current_instance() == "nested"


@pytest.mark.parametrize(
    "field,value",
    [("pid", 999), ("worker", 999), ("birth", "recycled"), ("worker_birth", "recycled"), ("signature", "user")],
)
def test_replaced_session_identity_is_refused(private_record, field, value):
    target, record = private_record
    record[field] = value
    target.write_text(json.dumps(record))
    with pytest.raises(session.HyprctlError):
        session.verify_current()


@pytest.mark.parametrize(
    "name",
    [
        "WAYLAND_DISPLAY",
        "AT_SPI_BUS_ADDRESS",
        "DBUS_SESSION_BUS_ADDRESS",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "DESKPILOT_AGENT",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "CHROME_CONFIG_HOME",
    ],
)
def test_session_environment_substitution_is_refused(private_record, monkeypatch, name):
    monkeypatch.setenv(name, "user-value")
    with pytest.raises(session.HyprctlError):
        session.verify_current()


@pytest.mark.parametrize("name", ["XDG_CONFIG_HOME", "AT_SPI_BUS_ADDRESS"])
def test_unset_or_unrecorded_environment_is_refused(private_record, monkeypatch, name):
    target, record = private_record
    monkeypatch.delenv(name)
    with pytest.raises(session.HyprctlError):
        session.verify_current()
    monkeypatch.setenv(name, record["env"][name])
    del record["env"][name]
    target.write_text(json.dumps(record))
    with pytest.raises(session.HyprctlError):
        session.verify_current()


@pytest.mark.parametrize("text", ["[]", '"x"', "7", "{", '{"pid": "../x", "birth": "b", "worker_birth": "w"}'])
def test_malformed_records_are_not_live_and_do_not_raise(tmp_path, text):
    (tmp_path / "ready.json").write_text(text)
    assert session.live(session._read(tmp_path / "ready.json")) is False
    assert session.live({"pid": 5, "birth": "b", "worker": 6, "worker_birth": "w"}) is False


def test_birth_refuses_non_integer_and_init_pids():
    assert session.birth("../self") is None
    assert session.birth(1) is None
    assert session.birth(True) is None


def test_dead_session_does_not_fall_back_to_user_compositor(private_record, monkeypatch):
    from deskpilot import hypr

    monkeypatch.setattr(session, "socket_alive", lambda path: False)
    monkeypatch.setattr(hypr, "pick_instance", lambda *args: pytest.fail("fell back to user desktop"))
    with pytest.raises(session.HyprctlError):
        hypr.current_instance()


def test_unrecorded_isolation_marker_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "user")
    monkeypatch.setenv("DESKPILOT_ISOLATED", "user")
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    with pytest.raises(session.HyprctlError):
        session.verify_current()


def test_crashed_worker_reaps_only_exact_owned_compositor(tmp_path, monkeypatch):
    record = {"worker": 102, "worker_birth": "old-worker", "pid": 101, "birth": "owned", "signature": "nested"}
    (tmp_path / "starting.json").write_text(json.dumps(record))
    alive = {101: "owned"}
    monkeypatch.setattr(session, "birth", alive.get)
    stopped = []
    monkeypatch.setattr(session, "_stop_compositor", lambda *args: (stopped.append(args), alive.clear()))
    monkeypatch.setattr(session.os, "kill", lambda *args: pytest.fail("signalled dead worker"))
    session._reap(tmp_path)
    assert stopped == [("nested", 101, "owned", session._runtime())]
    assert not (tmp_path / "starting.json").exists()


def test_recycled_compositor_is_never_controlled_or_signalled(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "birth", lambda pid: "replacement")
    monkeypatch.setattr(session, "_ctl", lambda *args: pytest.fail("controlled replacement compositor"))
    monkeypatch.setattr(session.os, "kill", lambda *args: pytest.fail("signalled replacement compositor"))
    session._stop_compositor("nested", 101, "original", tmp_path)


def test_stop_waits_worker_even_when_control_socket_is_gone(tmp_path, monkeypatch):
    record = {"worker": 102, "worker_birth": "worker", "pid": 101, "birth": "compositor", "signature": "nested"}
    (tmp_path / "ready.json").write_text(json.dumps(record))
    running = {102: "worker", 101: "compositor"}
    monkeypatch.setattr(session, "birth", running.get)
    monkeypatch.setattr(session, "socket_alive", lambda path: False)
    signalled, waited = [], []
    monkeypatch.setattr(session.os, "kill", lambda *args: signalled.append(args))

    def exited(pid, started, timeout):
        waited.append((pid, started))
        running.clear()
        return True

    monkeypatch.setattr(session, "_wait_dead", exited)
    session._reap(tmp_path)
    assert waited == [(102, "worker")]
    assert signalled == [(102, session.signal.SIGTERM)]


def test_low_memory_refuses_before_starting_any_process(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_directory", lambda: tmp_path)
    monkeypatch.setenv("DESKPILOT_MIN_FREE_MB", "2048")
    monkeypatch.delenv("DESKPILOT_OWNER_PID", raising=False)
    original = session.Path.read_text
    monkeypatch.setattr(
        session.Path,
        "read_text",
        lambda path, *a, **kw: (
            "MemAvailable: 102400 kB\n" if str(path) == "/proc/meminfo" else original(path, *a, **kw)
        ),
    )
    monkeypatch.setattr(session.subprocess, "Popen", lambda *a, **kw: pytest.fail("started with insufficient memory"))
    with pytest.raises(session.SessionBusy, match="100 MB available"):
        session.environment()


def test_owner_exit_ends_session_without_waiting_for_idle_timeout(tmp_path, monkeypatch):
    (tmp_path / "owners.json").write_text(json.dumps({"103": "owner"}))
    monkeypatch.setattr(session, "birth", lambda pid: "compositor" if pid == 101 else None)
    monkeypatch.setattr(session, "_ctl", lambda *args: "[]")
    monkeypatch.setattr(session.time, "sleep", lambda *args: pytest.fail("waited after owner exited"))
    session._session_loop(tmp_path, "parent", 101, "compositor", "nested", [])


def test_dispatch_tries_legacy_after_lua_refusal(monkeypatch):
    calls = []

    def ctl(*args):
        calls.append(args)
        return "bad dispatcher" if len(calls) == 1 else "ok"

    monkeypatch.setattr(session, "_ctl", ctl)
    assert session._dispatch("nested", "hl.dsp.exit()", "exit") == "ok"
    assert calls == [("nested", "dispatch", "hl.dsp.exit()"), ("nested", "dispatch", "exit")]


def _fake_processes(monkeypatch, alive):
    """Fake process table: pid -> birth. A signal ends the pid unless it is listed in `stubborn`."""
    signalled = []

    def kill(pid, sig):
        signalled.append((pid, sig))
        alive.pop(pid, None)

    monkeypatch.setattr(session, "birth", alive.get)
    monkeypatch.setattr(session.os, "kill", kill)
    monkeypatch.setattr(session.time, "sleep", lambda *a: None)
    return signalled


def _record_services(tmp_path, entries):
    (tmp_path / "services").mkdir()
    (tmp_path / "services.json").write_text(json.dumps(entries))


def test_dead_worker_services_are_reaped_by_exact_birth(tmp_path, monkeypatch):
    (tmp_path / "starting.json").write_text(
        json.dumps({"worker": 102, "worker_birth": "old", "pid": 101, "birth": "comp", "signature": "nested"})
    )
    _record_services(
        tmp_path,
        [
            {"name": "dbus", "pid": 201, "birth": "bus"},
            {"name": "launcher", "pid": 202, "birth": "launcher"},
            {"name": "keyboard", "pid": 203, "birth": "kb"},
        ],
    )
    alive = {101: "comp", 201: "bus", 202: "launcher", 203: "kb"}
    signalled = _fake_processes(monkeypatch, alive)
    order = []
    monkeypatch.setattr(
        session, "_stop_compositor", lambda sig, pid, started, rt: order.append("compositor") or alive.pop(pid)
    )
    session._reap(tmp_path)
    assert order == ["compositor"]
    assert [pid for pid, _ in signalled] == [203, 202, 201]
    assert not alive
    assert not (tmp_path / "services.json").exists()
    assert not (tmp_path / "services").exists()
    assert not (tmp_path / "starting.json").exists()


def test_recycled_service_pid_is_never_signalled(tmp_path, monkeypatch):
    _record_services(tmp_path, [{"name": "dbus", "pid": 201, "birth": "original"}])
    signalled = _fake_processes(monkeypatch, {201: "someone-else"})
    session._reap(tmp_path)
    assert signalled == []
    assert not (tmp_path / "services.json").exists()


def test_services_stop_even_when_compositor_cleanup_raises_and_records_stay(tmp_path, monkeypatch):
    (tmp_path / "ready.json").write_text(
        json.dumps({"worker": 102, "worker_birth": "w", "pid": 101, "birth": "comp", "signature": "nested"})
    )
    _record_services(tmp_path, [{"name": "dbus", "pid": 201, "birth": "bus"}])
    alive = {101: "comp", 201: "bus"}
    _fake_processes(monkeypatch, alive)

    def boom(*args):
        raise RuntimeError("compositor stuck")

    monkeypatch.setattr(session, "_stop_compositor", boom)
    with pytest.raises(RuntimeError):
        session._reap(tmp_path)
    assert 201 not in alive
    assert (tmp_path / "ready.json").exists()


def test_unkillable_service_keeps_its_record_and_runtime(tmp_path, monkeypatch):
    _record_services(
        tmp_path, [{"name": "dbus", "pid": 201, "birth": "bus"}, {"name": "kb", "pid": 202, "birth": "kb"}]
    )
    (tmp_path / "ready.json").write_text("{}")
    alive = {201: "bus", 202: "kb"}
    _fake_processes(monkeypatch, alive)
    monkeypatch.setattr(session.os, "kill", lambda pid, sig: alive.pop(pid) if pid == 202 else None)
    monkeypatch.setattr(session, "_wait_dead", lambda pid, started, timeout=5: session.birth(pid) != started)
    with pytest.raises(session.HyprctlError, match="services did not stop"):
        session._reap(tmp_path)
    assert [e["pid"] for e in json.loads((tmp_path / "services.json").read_text())] == [201]
    assert (tmp_path / "services").is_dir()
    assert (tmp_path / "ready.json").exists()


def test_malformed_service_records_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "services.json").write_text(json.dumps([7, {"pid": "x"}, {"pid": 1, "birth": "b"}, {"pid": 2}]))
    signalled = _fake_processes(monkeypatch, {1: "b", 2: "b"})
    session._reap(tmp_path)
    assert signalled == []


class _Proc:
    def __init__(self, pid):
        self.pid = pid
        self.stdout = io.StringIO("")

    def poll(self):
        return None


def test_each_service_is_recorded_before_its_readiness_wait(tmp_path, monkeypatch):
    """A worker killed while waiting on the bus leaves the bus recorded with its birth."""
    monkeypatch.setattr(session, "birth", {301: "bus"}.get)
    monkeypatch.setattr(session.subprocess, "Popen", lambda *a, **kw: _Proc(301))
    seen = []

    def wait_for_address(stream, timeout):
        seen.append(session._read_services(tmp_path))
        raise session.HyprctlError("killed here")

    monkeypatch.setattr(session, "_pipe_line", wait_for_address)
    children = []
    with pytest.raises(session.HyprctlError):
        session._start_services(tmp_path, {}, tmp_path / "keyboard", children)
    assert seen == [[{"name": "dbus", "pid": 301, "birth": "bus"}]]
    assert len(children) == 1


# --- private profile, launcher environment and publication ---------------------------------------------------------

TOKEN = "ab" * 16


def test_profile_is_private_and_leaves_the_process_environment_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    monkeypatch.setenv("HOME", "/parent-home")
    base = session.build_dir()
    profile = session._profile()
    root = base / "agents" / "tester"
    assert profile["HOME"] == str(root / "home")
    assert profile["WINEPREFIX"] == str(root / "wine")
    assert profile["CHROME_CONFIG_HOME"] == profile["XDG_CONFIG_HOME"] == str(root / "config")
    assert all((root / d).is_dir() and (root / d).stat().st_mode & 0o777 == 0o700 for d in ("home", "wine", "cache"))
    assert session.build_dir() == base
    assert session.os.environ["HOME"] == "/parent-home"


def test_publish_record_publishes_the_given_profile_and_token_without_recomputing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    env = {
        k: f"/given/{k}"
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
        )
    }
    env[session.TOKEN_ENV] = TOKEN
    session._publish_record(tmp_path, env, 101, "comp", "nested", "parent")
    record = json.loads((tmp_path / "ready.json").read_text())
    assert record["token"] == TOKEN
    assert record["env"]["HOME"] == "/given/HOME"
    assert record["env"]["WINEPREFIX"] == "/given/WINEPREFIX"
    assert record["env"][session.TOKEN_ENV] == TOKEN
    assert not (tmp_path / "data").exists()


def test_launcher_overrides_the_parent_environment_with_the_session_profile(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "Hyprland"
    fake.write_text("#!/bin/sh\nenv\n")
    fake.chmod(0o755)
    path = tmp_path / "session"
    path.mkdir()
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    launched = {}

    def dispatch(parent, lua, legacy):
        launched["env"] = {
            **session.os.environ,
            "PATH": f"{fake_bin}:{session.os.environ['PATH']}",
            "HOME": "/parent-home",
            "XDG_DATA_HOME": "/parent-data",
            "WINEPREFIX": "/parent-wine",
            "DESKPILOT_SESSION_TOKEN": "stale",
        }
        session.subprocess.run([str(path / "launch.py")], env=launched["env"], check=True)
        raise session.HyprctlError("launched")

    monkeypatch.setattr(session, "_dispatch", dispatch)
    overrides = {"HOME": "/profile/home", "XDG_DATA_HOME": "/profile/data", "WINEPREFIX": "/profile/wine"}
    overrides[session.TOKEN_ENV] = TOKEN
    with pytest.raises(session.HyprctlError, match="launched"):
        session._start_compositor(path, "parent", {}, overrides)
    seen = dict(line.split("=", 1) for line in (path / "hyprland.log").read_text().splitlines() if "=" in line)
    assert {k: seen[k] for k in overrides} == overrides
    assert json.loads((path / "starting.json").read_text())["token"] == TOKEN


# --- serve: one profile before everything, same environment everywhere, records kept until all is gone -------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("DESKPILOT_AGENT", "tester")
    monkeypatch.setenv(session.TOKEN_ENV, "inherited")
    path = tmp_path / "session"
    path.mkdir()
    calls = []
    state: dict = {"apps": True, "services": True, "compositor": "comp"}

    def keyboard_binary():
        calls.append(("keyboard", session.build_dir()))
        return tmp_path / "keyboard"

    def start_compositor(p, parent, env, overrides):
        calls.append(("compositor", dict(env), dict(overrides), json.loads((p / "starting.json").read_text())))
        env["WAYLAND_DISPLAY"], env["HYPRLAND_INSTANCE_SIGNATURE"] = "wl", "nested"
        return 501, "comp", "nested", "wl"

    def start_services(p, env, keyboard, children):
        calls.append(("services", dict(env)))
        return p

    def publish(p, env, *args):
        calls.append(("publish", dict(env)))
        (p / "ready.json").write_text("{}")

    monkeypatch.setattr(session, "keyboard_binary", keyboard_binary)
    monkeypatch.setattr(session, "_start_compositor", start_compositor)
    monkeypatch.setattr(session, "_start_services", start_services)
    monkeypatch.setattr(session, "_publish_record", publish)
    monkeypatch.setattr(session, "_ctl", lambda *a: "")
    monkeypatch.setattr(session, "_dispatch", lambda *a: "ok")
    monkeypatch.setattr(session, "_session_loop", lambda *a: None)
    monkeypatch.setattr(session.signal, "signal", lambda *a: None)
    monkeypatch.setattr(session, "birth", lambda pid: state["compositor"] if pid == 501 else "w")
    monkeypatch.setattr(session, "_stop_compositor", lambda *a: state.update(compositor=None))
    monkeypatch.setattr(session, "_stop_tagged", lambda token: calls.append(("apps", token)) or state["apps"])
    monkeypatch.setattr(session, "_reap_services", lambda p: state["services"])
    return path, calls, state


def test_profile_and_token_are_decided_once_before_compositor_and_services(served):
    path, calls, _ = served
    session.serve(path, "parent")
    names = [c[0] for c in calls]
    assert names == ["keyboard", "compositor", "services", "publish", "apps"]
    compositor_env, overrides, starting = calls[1][1:]
    services_env, published_env = calls[2][1], calls[3][1]
    token = overrides[session.TOKEN_ENV]
    assert session._token_ok(token) and token != "inherited"
    assert starting["token"] == token  # recorded before the compositor launched
    for env in (compositor_env, services_env, published_env):
        assert {k: env[k] for k in overrides} == overrides
    assert calls[0][1] == session.build_dir()  # keyboard cache is not under the profile
    assert calls[-1] == ("apps", token)


def test_records_are_kept_until_apps_and_services_have_stopped(served):
    path, _calls, state = served
    state["apps"] = False
    (path / "ready.json").write_text("{}")
    session.serve(path, "parent")
    assert (path / "ready.json").exists() and (path / "starting.json").exists()
    state["apps"] = True
    (path / "ready.json").write_text("{}")
    session.serve(path, "parent")
    assert not (path / "ready.json").exists() and not (path / "starting.json").exists()


def test_two_workers_never_share_a_token(served):
    path, calls, _ = served
    session.serve(path, "parent")
    session.serve(path, "parent")
    tokens = [c[2][session.TOKEN_ENV] for c in calls if c[0] == "compositor"]
    assert len(set(tokens)) == 2


# --- token ownership: exact environment pair only -------------------------------------------------------------------


def _spawn(token_pair=None, argv=("sleep", "60")):
    env = {k: v for k, v in session.os.environ.items() if k != session.TOKEN_ENV}
    if token_pair:
        env.update(token_pair)
    return session.subprocess.Popen(list(argv), env=env)


def test_only_exact_token_pair_processes_are_stopped_not_lookalikes():
    owned = _spawn({session.TOKEN_ENV: TOKEN})
    other_token = _spawn({session.TOKEN_ENV: TOKEN + "0"})
    renamed = _spawn({"X" + session.TOKEN_ENV: TOKEN})
    untagged = _spawn()
    try:
        deadline = session.time.monotonic() + 5
        while session.birth(owned.pid) is None and session.time.monotonic() < deadline:
            session.time.sleep(0.05)
        assert set(session._tagged(TOKEN)) == {owned.pid}
        assert session._stop_tagged(TOKEN) is True
        assert owned.wait(timeout=5) != 0
        for survivor in (other_token, renamed, untagged):
            assert survivor.poll() is None
    finally:
        for proc in (owned, other_token, renamed, untagged):
            proc.kill()
            proc.wait()


def test_tagged_stop_escalates_to_kill_for_a_process_that_ignores_term():
    ignoring = _spawn(
        {session.TOKEN_ENV: TOKEN},
        (
            "python3",
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);print(flush=True);time.sleep(60)",
        ),
    )
    try:
        deadline = session.time.monotonic() + 5
        while session.birth(ignoring.pid) is None and session.time.monotonic() < deadline:
            session.time.sleep(0.05)
        session.time.sleep(0.5)
        assert session._stop_tagged(TOKEN, timeout=0.5) is True
        assert ignoring.wait(timeout=5) == -session.signal.SIGKILL
    finally:
        ignoring.kill()
        ignoring.wait()


def test_signal_skips_a_process_whose_birth_changed():
    proc = _spawn()
    try:
        session._signal(proc.pid, "recycled-birth", session.signal.SIGKILL)
        session.time.sleep(0.2)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("token", [None, "", "short", "AB" * 16, "x" * 32, 7])
def test_malformed_token_never_scans_or_signals(token, monkeypatch):
    monkeypatch.setattr(session, "_tagged", lambda t: pytest.fail("scanned with a malformed token"))
    assert session._stop_tagged(token) is True


def test_reap_stops_apps_after_the_compositor_and_before_services(tmp_path, monkeypatch):
    (tmp_path / "ready.json").write_text(
        json.dumps({"worker": 102, "worker_birth": "w", "pid": 101, "birth": "comp", "signature": "n", "token": TOKEN})
    )
    order = []
    monkeypatch.setattr(session, "_reap_compositor", lambda record: order.append("compositor"))
    monkeypatch.setattr(session, "_stop_tagged", lambda token: order.append(("apps", token)) or True)
    monkeypatch.setattr(session, "_reap_services", lambda path: order.append("services") or True)
    session._reap(tmp_path)
    assert order == ["compositor", ("apps", TOKEN), "services"]
    assert not (tmp_path / "ready.json").exists()


def test_reap_keeps_the_crash_record_while_an_app_survives(tmp_path, monkeypatch):
    (tmp_path / "starting.json").write_text(json.dumps({"token": TOKEN}))
    monkeypatch.setattr(session, "_stop_tagged", lambda token: False)
    monkeypatch.setattr(session, "_reap_services", lambda path: True)
    with pytest.raises(session.HyprctlError, match="did not stop"):
        session._reap(tmp_path)
    assert json.loads((tmp_path / "starting.json").read_text())["token"] == TOKEN


# --- idle exit races environment() ----------------------------------------------------------------------------------


class _Wait(Exception):
    pass


@pytest.fixture
def idle_loop(tmp_path, monkeypatch):
    (tmp_path / "used").touch()
    session.os.utime(tmp_path / "used", (0, 0))
    monkeypatch.setattr(session, "birth", lambda pid: "comp" if pid == 101 else None)
    monkeypatch.setattr(session, "_ctl", lambda *a: "[]")

    def sleep(_):
        raise _Wait

    monkeypatch.setattr(session.time, "sleep", sleep)
    return tmp_path


def _flock(path, blocking=False):
    handle = (path / "lock").open("a")
    session.fcntl.flock(handle, session.fcntl.LOCK_EX | (0 if blocking else session.fcntl.LOCK_NB))
    return handle


def test_idle_worker_defers_while_environment_holds_the_lock_and_never_blocks(idle_loop):
    environment_lock = _flock(idle_loop)
    try:
        with pytest.raises(_Wait):
            session._session_loop(idle_loop, "parent", 101, "comp", "nested", [])
    finally:
        environment_lock.close()


def test_idle_exit_is_rechecked_under_the_lock(idle_loop, monkeypatch):
    real = session._try_lock

    def late_environment(path):
        lock = real(path)
        (path / "owners.json").write_text(json.dumps({"7": "owner"}))  # environment() registered an owner
        monkeypatch.setattr(session, "birth", {101: "comp", 7: "owner"}.get)
        return lock

    monkeypatch.setattr(session, "_try_lock", late_environment)
    with pytest.raises(_Wait):
        session._session_loop(idle_loop, "parent", 101, "comp", "nested", [])
    assert _flock(idle_loop)  # the lock was released again, not leaked


def test_fresh_use_under_the_lock_cancels_idle_exit(idle_loop, monkeypatch):
    real = session._try_lock

    def touched(path):
        lock = real(path)
        (path / "used").touch()
        return lock

    monkeypatch.setattr(session, "_try_lock", touched)
    with pytest.raises(_Wait):
        session._session_loop(idle_loop, "parent", 101, "comp", "nested", [])


def test_idle_exit_returns_with_the_lock_held_through_teardown(idle_loop):
    held = session._session_loop(idle_loop, "parent", 101, "comp", "nested", [])
    try:
        with pytest.raises(BlockingIOError):
            _flock(idle_loop)
    finally:
        held.close()
    _flock(idle_loop).close()


# --- the keyboard binary is built atomically ------------------------------------------------------------------------


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    state: dict = {"fail": False, "seen": []}

    def run(args, **kw):
        if args[0] != "cc":
            return None
        out = Path(args[args.index("-o") + 1])
        out.write_text("partial")
        state["seen"].append(out)
        assert not (out.parent / "keyboard").exists()
        if state["fail"]:
            raise session.subprocess.CalledProcessError(1, args)
        return None

    monkeypatch.setattr(session.subprocess, "run", run)
    monkeypatch.setattr(session.subprocess, "check_output", lambda *a, **kw: "")
    return state


def test_keyboard_is_compiled_beside_the_binary_and_renamed_into_place(build):
    binary = session.keyboard_binary()
    assert binary.read_text() == "partial"
    assert build["seen"][0] != binary and build["seen"][0].parent == binary.parent
    assert not build["seen"][0].exists()
    assert session.keyboard_binary() == binary and len(build["seen"]) == 1


def test_failed_keyboard_build_leaves_no_binary_and_is_retried(build):
    build["fail"] = True
    with pytest.raises(session.subprocess.CalledProcessError):
        session.keyboard_binary()
    folder = build["seen"][0].parent
    assert not (folder / "keyboard").exists() and not build["seen"][0].exists()
    build["fail"] = False
    assert session.keyboard_binary().exists()
    assert len(build["seen"]) == 2
