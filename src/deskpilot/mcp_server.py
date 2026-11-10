"""MCP server over stdio. The agent cursor and the virtual input device live for the whole session."""

from __future__ import annotations

import functools
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .desk import EXPECTED, Desk

mcp = MCPServer("deskpilot")
desk = Desk()


def _tool(**options):
    """Register a tool whose expected failures reach the agent as their message. The SDK reports any other
    exception as only `Error executing tool <name>`, which hides why a click was refused."""

    def register(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except EXPECTED as error:
                raise ToolError(str(error)) from error

        return mcp.tool(**options)(wrapper)

    return register


@_tool()
def windows() -> list[dict]:
    """List mapped windows: address, class, title, global position and size, focus, and whether any of it is on
    screen. A window that is not visible cannot be screenshot or clicked unless show is passed."""
    return [w.to_dict() for w in desk.windows()]


@_tool()
def focus(window: str) -> dict:
    """Focus a window by address, class, or a substring of class or title."""
    return desk.focus(window).to_dict()


@_tool(structured_output=False)
def shot(window: str, max_width: int = 1400, show: bool = False) -> list:
    """Screenshot a window, returning the image and its shot id. Later move/click/scroll points are pixels in this
    image; pass its id as `shot` to refuse the action if the window has changed size since. A window that is not on
    screen is an error unless show is set, which focuses it."""
    taken = desk.shot(window, max_width=max_width, show=show)
    return [Image(path=taken.path), taken.to_dict()]


@_tool()
def click(
    window: str,
    x: float,
    y: float,
    button: Literal["left", "right", "middle"] = "left",
    double: bool = False,
    shot: str | None = None,
    show: bool = False,
    take_pointer: bool = False,
) -> dict:
    """Click a point given in pixels of `shot`, or of the window's latest screenshot. The user's pointer and
    keyboard focus stay put unless take_pointer is set."""
    gx, gy = desk.click(window, x, y, button, 2 if double else 1, take_pointer=take_pointer, shot=shot, show=show)
    return {"x": gx, "y": gy}


@_tool()
def move(
    window: str, x: float, y: float, shot: str | None = None, show: bool = False, take_pointer: bool = False
) -> dict:
    """Show the agent cursor at a point of the window's last screenshot. take_pointer also moves the user's pointer
    there, for a hover the app must see."""
    gx, gy = desk.move(window, x, y, take_pointer=take_pointer, shot=shot, show=show)
    return {"x": gx, "y": gy}


@_tool()
def scroll(
    window: str, x: float, y: float, dy: int, shot: str | None = None, show: bool = False, take_pointer: bool = False
) -> dict:
    """Scroll at a point of `shot` or the window's latest screenshot; positive dy scrolls up."""
    gx, gy = desk.scroll(window, x, y, dy, take_pointer=take_pointer, shot=shot, show=show)
    return {"x": gx, "y": gy}


@_tool()
def key(combo: str, window: str | None = None, take_focus: bool = False) -> str:
    """Press a key or chord such as Return, ctrl+a or alt+F4 in a window without focusing it. take_focus focuses
    the window first instead; with no window the keys go to the focused one."""
    desk.key(combo, window, take_focus)
    return "ok"


@_tool()
def type_text(text: str, window: str | None = None, delay: float = 0.03, take_focus: bool = False) -> str:
    """Type text on a US layout into a window without focusing it. Each key is held `delay` seconds and the next
    follows after the same gap; use 0.1 or more for games."""
    desk.type(text, window, delay, take_focus)
    return "ok"


@_tool()
def wait_for(window: str, timeout: float = 60, gone: bool = False) -> dict | str:
    """Wait for a window to appear, or with gone=True to close."""
    win = desk.wait_for(window, timeout, gone)
    return win.to_dict() if win else "gone"


def serve() -> None:
    try:
        mcp.run()
    finally:
        desk.close()
