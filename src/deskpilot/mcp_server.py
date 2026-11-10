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
    x: float | None = None,
    y: float | None = None,
    button: Literal["left", "right", "middle"] = "left",
    double: bool = False,
    shot: str | None = None,
    show: bool = False,
    take_pointer: bool = False,
    take_focus: bool = False,
    element: str | None = None,
) -> dict:
    """Click a point given in pixels of `shot`, or of the window's latest screenshot, or the centre of an element id
    from `elements`. The user's pointer and keyboard focus stay put unless take_pointer is set, or take_focus, which
    focuses the window first and leaves it focused: use it for a menu or dropdown that closes as soon as it opens."""
    gx, gy = desk.click(
        window,
        x,
        y,
        button,
        2 if double else 1,
        take_pointer=take_pointer,
        shot=shot,
        show=show,
        take_focus=take_focus,
        element=element,
    )
    return {"x": gx, "y": gy}


@_tool()
def move(
    window: str,
    x: float | None = None,
    y: float | None = None,
    shot: str | None = None,
    show: bool = False,
    take_pointer: bool = False,
    element: str | None = None,
) -> dict:
    """Show the agent cursor at a point of the window's last screenshot, or at an element. take_pointer also moves
    the user's pointer there. For a hover the app must see, use hover, which leaves the user's pointer alone."""
    gx, gy = desk.move(window, x, y, take_pointer=take_pointer, shot=shot, show=show, element=element)
    return {"x": gx, "y": gy}


@_tool()
def scroll(
    window: str,
    dy: int,
    x: float | None = None,
    y: float | None = None,
    shot: str | None = None,
    show: bool = False,
    take_pointer: bool = False,
    element: str | None = None,
) -> dict:
    """Scroll at a point of `shot` or the window's latest screenshot, or at an element; positive dy scrolls up."""
    gx, gy = desk.scroll(window, x, y, dy, take_pointer=take_pointer, shot=shot, show=show, element=element)
    return {"x": gx, "y": gy}


@_tool(structured_output=False)
def hover(
    window: str,
    x: float | None = None,
    y: float | None = None,
    wait: float = 1.0,
    shot: str | None = None,
    show: bool = False,
    element: str | None = None,
    then_click: tuple[float, float] | None = None,
    button: Literal["left", "right", "middle"] = "left",
) -> list:
    """Rest the pointer on a point (or element) for `wait` seconds, up to 5, without moving the user's pointer, and
    return a screenshot taken while it rests there: tooltips and hover states show in it. With then_click, a point
    in the same pixels, click there next before the pointer leaves, as a menu that opens on hover needs."""
    result = desk.hover(window, x, y, wait, shot=shot, show=show, element=element, then_click=then_click, button=button)
    if isinstance(result, tuple):
        return [{"x": result[0], "y": result[1]}]
    return [Image(path=result.path), result.to_dict()]


@_tool()
def drag(
    window: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    button: Literal["left", "right", "middle"] = "left",
    duration: float = 0.3,
    shot: str | None = None,
    show: bool = False,
) -> dict:
    """Press at (x1, y1), move to (x2, y2) over duration seconds (up to 5) and release, in pixels of `shot` or the
    window's latest screenshot: sliders, text selection, drawing and drag and drop within the window. The user's
    pointer stays put."""
    gx, gy = desk.drag(window, (x1, y1), (x2, y2), button, duration, shot=shot, show=show)
    return {"x": gx, "y": gy}


@_tool()
def elements(window: str, show: bool = False) -> dict:
    """The window's accessibility tree: each element's id, role, name, states, text and bounds in window pixels.
    click, hover, move and scroll take an id as `element` and act on where the app says it is now. An app that
    exposes no tree is an error saying so."""
    return desk.elements(window, show).to_dict()


@_tool()
def key(combo: str, window: str | None = None, take_focus: bool = False, hold: float = 0.0) -> str:
    """Press a key or chord such as Return, ctrl+a or alt+F4 in a window without focusing it. take_focus focuses
    the window first instead; with no window the keys go to the focused one. hold keeps each key down that many
    seconds, up to 10, for auto-repeat or a game's movement."""
    desk.key(combo, window, take_focus, hold)
    return "ok"


@_tool()
def type_text(text: str, window: str | None = None, delay: float = 0.03, take_focus: bool = False) -> str:
    """Type text as written, on the user's keymap, into a window without focusing it. Each key is held `delay`
    seconds and the next follows after the same gap; use 0.1 or more for games."""
    desk.type(text, window, delay, take_focus)
    return "ok"


@_tool()
def paste_text(text: str, window: str | None = None, take_focus: bool = False, keys: str = "ctrl+v") -> str:
    """Paste text into a window: it becomes that window's clipboard alone, never the user's, and `keys` (the app's
    paste chord, ctrl+shift+v in most terminals) is pressed there. Pastes any text, including characters the keymap
    cannot type. Not for XWayland windows, which share one clipboard."""
    desk.paste(text, window, take_focus, keys)
    return "ok"


@_tool()
def clipboard() -> str:
    """The user's clipboard text. An empty clipboard, or one holding no text, is an error saying so."""
    return desk.clipboard()


@_tool()
def copy_text(text: str) -> str:
    """Replace the user's clipboard with text."""
    desk.set_clipboard(text)
    return "ok"


@_tool()
def wait_for(
    window: str,
    timeout: float = 60,
    gone: bool = False,
    focused: bool = False,
    title: str | None = None,
    size: tuple[int, int] | None = None,
) -> dict | str:
    """Wait for a window to appear, or with gone=True to close. focused, title (a substring) and size (window pixels)
    also wait for it to have keyboard focus, that title or that size. Returns the window as it matched."""
    win = desk.wait_for(window, timeout, gone, focused, title, size)
    return win.to_dict() if win else "gone"


@_tool()
def wait_changed(window: str, shot: str, timeout: float = 60, region: tuple[int, int, int, int] | None = None) -> list:
    """Wait until a window looks different from one of its shots, in all of it or in region (x, y, width, height in
    that shot's pixels). Any pixel difference counts. Returns a shot of the change, its id and the box that
    changed."""
    taken, box = desk.wait_changed(window, shot, timeout, region)
    return [Image(path=taken.path), {**taken.to_dict(), "changed": list(box)}]


def serve() -> None:
    try:
        mcp.run()
    finally:
        desk.close()
