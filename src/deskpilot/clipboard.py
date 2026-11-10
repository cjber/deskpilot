"""The user's clipboard, read and written with wl-clipboard."""

from __future__ import annotations

import subprocess


class ClipboardError(RuntimeError):
    pass


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=False, timeout=10, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise ClipboardError(f"{cmd[0]} did not finish within 10s: the clipboard's owner is not answering") from error


# The plain text types, in the order a client that wants UTF-8 asks for them (the plugin offers a paste as these).
# Other text/ types, such as text/html or Firefox's UTF-16 text/x-moz-url, are not plain text.
TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")


def read() -> str:
    """The clipboard's plain text. An empty clipboard, or one with no plain text, is an error saying so."""
    listed = _run(["wl-paste", "--list-types"], capture_output=True, text=True)
    if listed.returncode != 0:
        raise ClipboardError(f"wl-paste: {listed.stderr.strip()}")
    offered = listed.stdout.split()
    kind = next((t for t in TEXT_TYPES if t in offered), None)
    if kind is None:
        raise ClipboardError(f"the clipboard holds no plain text; it offers {', '.join(offered)}")
    out = _run(["wl-paste", "--no-newline", "--type", kind], capture_output=True)
    if out.returncode != 0:
        raise ClipboardError(f"wl-paste: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout.decode(errors="replace")


def write(text: str) -> None:
    """Replace the clipboard with text. wl-copy stays behind to serve it, so its output is not waited on."""
    result = _run(
        ["wl-copy", "--type", "text/plain;charset=utf-8"],
        input=text.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise ClipboardError(f"wl-copy exited with {result.returncode}")
