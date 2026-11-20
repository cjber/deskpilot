import io
import threading

import pytest
from mcp.types import CallToolResult
from test_core import make_desk

from deskpilot import cli, mcp_server, record, screen


def failed_recording(*args, **kwargs):
    def take():
        raise screen.NoFrame("window stopped supplying frames")

    return record.stream(take, lambda frame: None, threading.Event(), seconds=0.01)


@pytest.mark.parametrize("stdout", [False, True], ids=["mp4", "stdout"])
def test_record_cli_reports_failure_when_no_frame_was_captured(tmp_path, monkeypatch, capsys, stdout):
    desk, *_ = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr(desk, "record", failed_recording)
    monkeypatch.setattr(cli, "open_desk", lambda: desk)
    if stdout:
        # The CLI needs binary stdout for the frame stream.
        monkeypatch.setattr(cli.sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    path = "-" if stdout else str(tmp_path / "clip.mp4")
    status = cli.main(["record", "battle", "-o", path, "--seconds", "0.01"])
    assert status != 0, "zero captured frames must not be reported as a successful recording"


def test_record_stop_reports_capture_failure_as_an_mcp_error(tmp_path, monkeypatch):
    desk, *_ = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr(desk, "record", failed_recording)
    monkeypatch.setattr(mcp_server, "desk", desk)
    path = str(tmp_path / "clip.mp4")
    try:
        mcp_server.record_start("battle", path)
        result = mcp_server.record_stop(path)
    finally:
        if path in mcp_server._recordings:
            thread, stop, _ = mcp_server._recordings.pop(path)
            stop.set()
            thread.join()
    assert isinstance(result, CallToolResult) and result.is_error, "an absent clip must be an MCP tool failure"
