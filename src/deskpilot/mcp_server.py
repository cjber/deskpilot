"""MCP server over stdio. The agent cursor and the virtual input device live for the whole session."""

from __future__ import annotations

from mcp.server.mcpserver import Image, MCPServer

from .desk import Desk

mcp = MCPServer("deskpilot")
desk = Desk()


@mcp.tool()
def windows() -> list[dict]:
    """List mapped windows: address, class, title, global position and size, focus."""
    return [w.to_dict() for w in desk.windows()]


@mcp.tool()
def focus(window: str) -> dict:
    """Focus a window by address, class, or a substring of class or title."""
    return desk.focus(window).to_dict()


@mcp.tool()
def shot(window: str, max_width: int = 1400) -> Image:
    """Screenshot a window. Later move/click/scroll points are pixels in this image."""
    taken = desk.shot(window, max_width=max_width)
    return Image(path=str(taken.path))


@mcp.tool()
def click(
    window: str, x: float, y: float, button: str = "left", double: bool = False, take_pointer: bool = False
) -> dict:
    """Click a point given in pixels of the window's last screenshot. The user's pointer stays put unless
    take_pointer is set."""
    gx, gy = desk.click(window, x, y, button, 2 if double else 1, take_pointer=take_pointer)
    return {"x": gx, "y": gy}


@mcp.tool()
def move(window: str, x: float, y: float, take_pointer: bool = False) -> dict:
    """Show the agent cursor at a point of the window's last screenshot. take_pointer also moves the user's pointer
    there, for a hover the app must see."""
    gx, gy = desk.move(window, x, y, take_pointer=take_pointer)
    return {"x": gx, "y": gy}


@mcp.tool()
def scroll(window: str, x: float, y: float, dy: int, take_pointer: bool = False) -> dict:
    """Scroll at a point of the window's last screenshot; positive dy scrolls up."""
    gx, gy = desk.scroll(window, x, y, dy, take_pointer=take_pointer)
    return {"x": gx, "y": gy}


@mcp.tool()
def key(combo: str, window: str | None = None, take_focus: bool = False) -> str:
    """Press a key or chord such as Return, ctrl+a or alt+F4 in a window without focusing it. take_focus focuses
    the window first instead; with no window the keys go to the focused one."""
    desk.key(combo, window, take_focus)
    return "ok"


@mcp.tool()
def type_text(text: str, window: str | None = None, delay: float = 0.03, take_focus: bool = False) -> str:
    """Type text on a US layout into a window without focusing it. Use delay 0.1 or more for games."""
    desk.type(text, window, delay, take_focus)
    return "ok"


@mcp.tool()
def wait_for(window: str, timeout: float = 60, gone: bool = False) -> dict | str:
    """Wait for a window to appear, or with gone=True to close."""
    win = desk.wait_for(window, timeout, gone)
    return win.to_dict() if win else "gone"


def serve() -> None:
    try:
        mcp.run()
    finally:
        desk.close()
