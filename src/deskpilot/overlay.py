"""The agent cursor: cua-driver's overlay arrow, drawn where deskpilot acts and nowhere near the user's pointer.

`cua-driver mcp` runs as a child of this process and draws a layer surface on every output. `move_cursor` with
`scope: "window"` only moves that overlay; the user's pointer and focus stay where they are. It is feedback, never
input: every failure (no binary, an old driver, a refused call) turns the overlay off for the rest of the process
and the action goes ahead without it.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys

SESSION = "deskpilot"
CALL_SECONDS = 4.0


class AgentCursor:
    def __init__(self, binary: str | None = None):
        self._binary = binary if binary is not None else shutil.which("cua-driver")
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._buffer = b""
        self._off = self._binary is None or os.environ.get("DESKPILOT_CURSOR") == "off"

    def _send(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        assert self._proc and self._proc.stdin and self._proc.stdout
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._next_id += 1
            message["id"] = self._next_id
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        self._proc.stdin.flush()
        if notify:
            return None
        while True:
            reply = json.loads(self._read_line())
            if reply.get("id") == message["id"]:
                if "error" in reply:
                    raise OSError(str(reply["error"]))
                return reply.get("result")

    def _read_line(self) -> bytes:
        assert self._proc and self._proc.stdout
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._buffer:
            ready, _, _ = select.select([fd], [], [], CALL_SECONDS)
            if not ready:
                raise OSError(f"cua-driver did not answer within {CALL_SECONDS:g}s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise OSError("cua-driver exited")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    def _start(self) -> None:
        env = {**os.environ, "CUA_DRIVER_RS_ENABLE_WAYLAND": "1"}
        self._proc = subprocess.Popen(
            [self._binary, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self._send(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "deskpilot", "version": "1"}},
        )
        self._send("notifications/initialized", notify=True)

    def glide(self, x: int, y: int) -> None:
        """Glide the agent cursor to a global position and return once it has landed."""
        if self._off:
            return
        try:
            if self._proc is None:
                self._start()
            result = self._send(
                "tools/call",
                {"name": "move_cursor", "arguments": {"x": x, "y": y, "session": SESSION, "scope": "window"}},
            )
            if result and result.get("isError"):
                raise OSError(json.dumps(result.get("content", ""))[:200])
        except (OSError, ValueError) as error:
            self._off = True
            if os.environ.get("DESKPILOT_LOG"):
                print(f"deskpilot: agent cursor off: {error}", file=sys.stderr)
            self.close()

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
