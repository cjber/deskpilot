"""Lifecycle checks with fake processes and sockets, never a compositor."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("nested", Path(__file__).parents[1] / "scripts/nested.py")
assert spec and spec.loader
nested = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nested)


def recorded_run(tmp_path, monkeypatch):
    scratch, runtime = tmp_path / "scratch", tmp_path / "runtime"
    run = scratch / "run-test"
    run.mkdir(parents=True)
    (run / "signature").write_text("owned")
    (run / "pid").write_text("123")
    (run / "birth").write_text("456")
    instance = runtime / "owned"
    instance.mkdir(parents=True)
    (instance / "hyprland.log").write_text("log")
    (instance / ".socket2.sock").touch()
    monkeypatch.setattr(nested, "SCRATCH", scratch)
    monkeypatch.setattr(nested, "RUNTIME", runtime)
    return run, instance


def test_stop_removes_only_the_owned_dead_instance(tmp_path, monkeypatch):
    run, instance = recorded_run(tmp_path, monkeypatch)
    other = instance.parent / "other"
    other.mkdir()
    monkeypatch.setattr(nested, "alive", lambda signature: False)
    monkeypatch.setattr(nested, "process_birth", lambda pid: None)
    nested.stop("owned")
    assert not run.exists() and not instance.exists()
    assert other.exists()


def test_stop_keeps_runtime_when_pid_was_reused(tmp_path, monkeypatch):
    run, instance = recorded_run(tmp_path, monkeypatch)
    monkeypatch.setattr(nested, "alive", lambda signature: False)
    monkeypatch.setattr(nested, "process_birth", lambda pid: "different")
    with pytest.raises(SystemExit, match="cannot verify"):
        nested.stop("owned")
    assert run.exists() and instance.exists()


def test_stop_refuses_a_different_live_process(tmp_path, monkeypatch):
    run, instance = recorded_run(tmp_path, monkeypatch)
    monkeypatch.setattr(nested, "alive", lambda signature: True)
    monkeypatch.setattr(nested, "hyprctl", lambda *args: json.dumps([{"instance": "owned", "pid": 999}]))
    with pytest.raises(SystemExit, match="does not belong"):
        nested.stop("owned")
    assert run.exists() and instance.exists()
