"""The backend for this platform: Hyprland on Linux, the native drivers on Windows and macOS."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from .errors import Unavailable

if TYPE_CHECKING:
    from .desk import Desk
    from .native import NativeDesk


def owner_from_env() -> int | None:
    """The process named by DESKPILOT_OWNER_PID, whose exit closes the agent's launched apps."""
    value = os.environ.get("DESKPILOT_OWNER_PID") or None
    if value is not None and not (value.isdigit() and int(value) > 1):
        raise ValueError(f"DESKPILOT_OWNER_PID={value!r}: give a process id")
    return int(value) if value else None


def open_desk(owner: int | None = None) -> Desk | NativeDesk:
    if sys.platform == "win32":
        from .native import NativeDesk
        from .win32 import Win32Driver

        return NativeDesk(Win32Driver(), owner)
    if sys.platform == "darwin":
        from .macos import MacDriver
        from .native import NativeDesk

        return NativeDesk(MacDriver(), owner)
    from .desk import Desk

    return Desk(owner=owner)


def hyprland_only(feature: str) -> None:
    """Refuse a feature that exists only through the Hyprland plugin."""
    if sys.platform in ("win32", "darwin"):
        raise Unavailable(f"{feature} is a Hyprland feature; it does not exist on {sys.platform}")
