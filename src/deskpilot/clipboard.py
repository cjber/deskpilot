"""The user's clipboard, read and written with wl-clipboard."""

from __future__ import annotations

import subprocess


class ClipboardError(RuntimeError):
    pass


def read() -> str:
    """The clipboard's text, or "" when nothing is copied."""
    types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, check=False, timeout=10)
    if types.returncode != 0:
        return ""
    out = subprocess.run(["wl-paste", "--no-newline", "--type", "text"], capture_output=True, check=False, timeout=10)
    if out.returncode != 0:
        offered, why = ", ".join(types.stdout.split()), out.stderr.decode(errors="replace").strip()
        raise ClipboardError(f"the clipboard holds no text (it offers {offered}): {why}")
    return out.stdout.decode(errors="replace")


def write(text: str) -> None:
    """Replace the clipboard with text. wl-copy stays behind to serve it, so its output is not waited on."""
    result = subprocess.run(
        ["wl-copy", "--type", "text/plain;charset=utf-8"],
        input=text.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise ClipboardError(f"wl-copy exited with {result.returncode}")
