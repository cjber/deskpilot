"""The failures an agent reads and acts on, and the stable code each carries. Shared by every backend."""

from __future__ import annotations

from .hypr import AmbiguousWindow, HyprctlError, WindowNotFound


class SessionLocked(RuntimeError):
    code = "refused"


class NotVisible(LookupError):
    code = "not_visible"


class Covered(RuntimeError):
    code = "covered"


class Unavailable(RuntimeError):
    """An action this platform's backend cannot do without breaking a rule, such as moving the user's pointer."""

    code = "unavailable"


# Failures an agent should read and act on: a refusal, a missing window, a tool that failed. Anything else is a bug.
EXPECTED = (
    WindowNotFound,
    AmbiguousWindow,
    NotVisible,
    HyprctlError,
    ValueError,
    TimeoutError,
    RuntimeError,
)


def error_code(error: BaseException) -> str:
    """The kind of an expected failure, one stable word a program branches on rather than reading the message:
    not_found, ambiguous, not_visible, offscreen, stale, covered, gone, owns_clipboard, refused, busy, no_tree,
    unavailable, timeout or invalid."""
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "invalid"
    return "refused"
