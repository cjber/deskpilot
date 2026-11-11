"""MCP server over stdio. The agent cursor and the virtual input device live for the whole session."""

from __future__ import annotations

import functools
import os
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import CallToolResult, TextContent

from .desk import EXPECTED, Desk, error_code, owner_from_env

mcp = MCPServer("deskpilot")
# The server is the agent's owner process: its apps close when it exits, unless DESKPILOT_OWNER_PID names another.
desk = Desk(owner=owner_from_env() or os.getpid())


def _tool(**options):
    """Register a tool whose expected failures come back as an error result: `<code>: <message>` for the model to
    read, and `{"code", "message"}` as structured content, the code one stable word (see `error_code`) a program
    branches on. Returning the result rather than raising keeps the SDK's `Error executing tool <name>` prefix off it,
    and any other exception reaches the agent as only that prefix, which would hide why a click was refused."""

    def register(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except EXPECTED as error:
                code = error_code(error)
                return CallToolResult(
                    content=[TextContent(type="text", text=f"{code}: {error}")],
                    structured_content={"code": code, "message": str(error)},
                    is_error=True,
                )

        return mcp.tool(**options)(wrapper)

    return register


@_tool()
def windows() -> list[dict]:
    """List mapped windows: address, class, title, global position and size, focus, and whether any of it is on
    screen. A window that is not visible is reached with show (brought into view); one the agent launched (see launch)
    with hidden, out of view."""
    return [w.to_dict() for w in desk.windows()]


@_tool()
def focus(window: str) -> dict:
    """Focus a window by address, class, or a substring of class or title."""
    return desk.focus(window).to_dict()


@_tool(structured_output=False)
def shot(window: str, max_width: int = 1400, show: bool = False, hidden: bool = False) -> list:
    """Screenshot a window, returning the image and its shot id. Later move/click/scroll points are pixels in this
    image; pass its id as `shot` to refuse the action if the window has changed size since. A window that is not on
    screen is an error unless show is set, which focuses it; hidden captures a window the agent launched (launch)."""
    taken = desk.shot(window, max_width=max_width, show=show, hidden=hidden)
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
    hidden: bool = False,
) -> dict:
    """Click a point given in pixels of `shot`, or of the window's latest screenshot, or the centre of an element id
    from `elements`. The user's pointer and keyboard focus stay put unless take_pointer is set, or take_focus, which
    focuses the window first and leaves it focused: use it for a menu or dropdown that closes as soon as it opens.
    hidden clicks a window the agent launched (see launch) on its hidden workspace, with no agent cursor shown (not
    XWayland windows)."""
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
        hidden=hidden,
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
    hidden: bool = False,
) -> dict:
    """Scroll at a point of `shot` or the window's latest screenshot, or at an element; positive dy scrolls up.
    hidden scrolls a window the agent launched, out of view."""
    gx, gy = desk.scroll(
        window, x, y, dy, take_pointer=take_pointer, shot=shot, show=show, element=element, hidden=hidden
    )
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
    hidden: bool = False,
) -> list:
    """Rest the pointer on a point (or element) for `wait` seconds, up to 5, without moving the user's pointer, and
    return a screenshot taken while it rests there: tooltips and hover states show in it. With then_click, a point
    in the same pixels, click there next before the pointer leaves, as a menu that opens on hover needs. hidden
    hovers a window the agent launched, out of view."""
    result = desk.hover(
        window,
        x,
        y,
        wait,
        shot=shot,
        show=show,
        element=element,
        then_click=then_click,
        button=button,
        hidden=hidden,
    )
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
    hidden: bool = False,
) -> dict:
    """Press at (x1, y1), move to (x2, y2) over duration seconds (up to 5) and release, in pixels of `shot` or the
    window's latest screenshot: sliders, text selection, drawing and drag and drop within the window. The user's
    pointer stays put. hidden drags in a window the agent launched, out of view; no drag and drop there."""
    gx, gy = desk.drag(window, (x1, y1), (x2, y2), button, duration, shot=shot, show=show, hidden=hidden)
    return {"x": gx, "y": gy}


@_tool()
def elements(window: str, show: bool = False, hidden: bool = False, peek: bool = False) -> dict:
    """The window's accessibility tree: each element's id, role, name, states, text and bounds in window pixels,
    and on the root the snapshot id. click, hover, move and scroll take an id as `element` and act on where the app
    says it is now. Each snapshot numbers its elements afresh: `<snapshot>:<id>` is refused (stale) once a newer
    snapshot replaces that one, and an element whose role or name changed since is stale too. An app that exposes no
    tree is an error (no_tree). peek reads the tree without replacing the latest snapshot, so its ids stay good, to
    check what changed before acting; a peek's own ids name nothing and it has no snapshot id."""
    return desk.elements(window, show, hidden, peek).to_dict()


@_tool()
def key(
    combo: str,
    window: str | None = None,
    take_focus: bool = False,
    hold: float = 0.0,
    show: bool = False,
    hidden: bool = False,
) -> str:
    """Press a key or chord such as Return, ctrl+a or alt+F4 in a window without focusing it. take_focus focuses
    the window first instead; with no window the keys go to the focused one. hold keeps each key down that many
    seconds, up to 10, for auto-repeat or a game's movement. A window not on screen is refused unless show brings it
    into view. hidden sends the keys to a window the agent launched (see launch), out of view; it needs a window and
    refuses XWayland windows and the app with the user's keyboard focus."""
    desk.key(combo, window, take_focus, hold, show=show, hidden=hidden)
    return "ok"


@_tool()
def type_text(
    text: str,
    window: str | None = None,
    delay: float = 0.0,
    take_focus: bool = False,
    show: bool = False,
    hidden: bool = False,
) -> str:
    """Type text as written, on the user's keymap, into a window without focusing it. Keys go as fast as the
    app reads them by default. Games read keys once per frame: for them, each key is held `delay` seconds, 0.1 or
    more, and the next follows after the same gap. For long text, paste_text is faster still. show and hidden work
    as for key."""
    desk.type(text, window, delay, take_focus, show=show, hidden=hidden)
    return "ok"


@_tool()
def paste_text(
    text: str,
    window: str | None = None,
    take_focus: bool = False,
    keys: str = "ctrl+v",
    show: bool = False,
    hidden: bool = False,
) -> str:
    """Paste text into a window: it becomes that window's clipboard alone, never the user's, and `keys` (the app's
    paste chord, ctrl+shift+v in most terminals) is pressed there. Pastes any text, including characters the keymap
    cannot type. Not for XWayland windows, which share one clipboard. show and hidden work as for key."""
    desk.paste(text, window, take_focus, keys, show=show, hidden=hidden)
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
def wait_changed(
    window: str,
    shot: str,
    timeout: float = 60,
    region: tuple[int, int, int, int] | None = None,
    hidden: bool = False,
) -> list:
    """Wait until a window looks different from one of its shots, in all of it or in region (x, y, width, height in
    that shot's pixels). Any pixel difference counts. Returns a shot of the change, its id and the box that
    changed."""
    taken, box = desk.wait_changed(window, shot, timeout, region, hidden)
    return [Image(path=taken.path), {**taken.to_dict(), "changed": list(box)}]


@_tool()
def launch(command: list[str], timeout: float = 30) -> dict:
    """Start an app, as its command and arguments, on this agent's own hidden workspace and return its first window.
    It and every window it opens later stay out of the user's view and focus; drive them with hidden. Needs
    DESKPILOT_AGENT set for the server. The apps close when this server exits. Browsers and web pages are
    fastbrowse's job, not this."""
    return desk.launch(command, timeout).to_dict()


@_tool()
def stop() -> str:
    """Close every app this agent launched, as the user closing its windows would."""
    desk.stop()
    return "ok"


def serve() -> None:
    try:
        mcp.run()
    finally:
        desk.close()
