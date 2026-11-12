"""MCP server over stdio. The agent cursor and the virtual input device live for the whole session."""

from __future__ import annotations

import functools
import os
from importlib.metadata import version
from typing import Annotated, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from . import batch as batching
from .batch import MAX_ACTIONS, Action
from .desk import EXPECTED, Desk, error_code, owner_from_env

mcp = MCPServer("deskpilot", version=version("deskpilot"))
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
    screen is an error unless show is set, which focuses it; hidden captures a window the agent launched or adopted."""
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
    hidden clicks a window the agent launched or adopted on its hidden workspace, with no agent cursor shown (not
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
    hidden scrolls a window the agent launched or adopted, out of view."""
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
    hovers a window the agent launched or adopted, out of view."""
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
    pointer stays put. hidden drags in a window the agent launched or adopted, out of view; no drag and drop there."""
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
    agent_keyboard: bool = False,
) -> str:
    """Press a key or chord such as Return, ctrl+a or alt+F4 in a window without focusing it. take_focus focuses
    the window first instead; with no window the keys go to the focused one. hold keeps each key down that many
    seconds, up to 10, for auto-repeat or a game's movement. A window not on screen is refused unless show brings it
    into view. hidden sends the keys to a window the agent launched or adopted, out of view; it needs a window and
    refuses XWayland windows and the app with the user's keyboard focus. agent_keyboard opts into the independent
    keyboard for native apps that support multiple seats, including GTK; it needs a window. Qt can silently discard
    that seat's keys, so verify the result. The default route remains appropriate for Qt and Chromium."""
    desk.key(combo, window, take_focus, hold, show=show, hidden=hidden, agent_keyboard=agent_keyboard)
    return "ok"


@_tool()
def type_text(
    text: str,
    window: str | None = None,
    delay: float = 0.0,
    take_focus: bool = False,
    show: bool = False,
    hidden: bool = False,
    agent_keyboard: bool = False,
) -> str:
    """Type text as written, on the user's keymap, into a window without focusing it. Keys go as fast as the
    app reads them by default. Games read keys once per frame: for them, each key is held `delay` seconds, 0.1 or
    more, and the next follows after the same gap. For long text, paste_text is faster still. show and hidden work
    as for key, including the explicit agent_keyboard option for native apps that support multiple seats."""
    desk.type(text, window, delay, take_focus, show=show, hidden=hidden, agent_keyboard=agent_keyboard)
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
    cannot type. Not for XWayland windows, which share one clipboard, nor for an app that holds the user's clipboard
    (owns_clipboard), which pastes its own copy: type the text there instead, noting each newline is sent as Return.
    show and hidden work as for key."""
    desk.paste(text, window, take_focus, keys, show=show, hidden=hidden)
    return "ok"


@_tool(structured_output=False)
def batch(
    window: str,
    actions: Annotated[list[Action], Field(min_length=1, max_length=MAX_ACTIONS)],
    hidden: bool = False,
    final_shot: bool = False,
    max_width: int = 1400,
) -> list | CallToolResult:
    """Run click, move, scroll, drag, key, type and paste steps on one window in order, in one call. The whole list
    is checked before anything runs. It is sequential, not atomic: it stops at the first failed step and undoes
    nothing, and other agents may act between steps. Steps act on the pinned window with its own pointer and
    keyboard (no take_pointer, take_focus or show); hidden applies to all of them. Points are pixels of the window's
    latest screenshot, or of a step's `shot`; prefer `element` ids from `elements`. Returns {ok, window, completed,
    results, failed, shot}: a failure carries the 0-based index, action, code and message of the step (index = the
    step count for final_shot) and is an error result. With final_shot, the window's screenshot follows the last
    step."""
    request = batching.parse(
        {"window": window, "actions": actions, "hidden": hidden, "final_shot": final_shot, "max_width": max_width}
    )
    result = desk.batch(request)
    if (failed := result.failed) is not None:
        where = "the window" if failed.index is None else f"step {failed.index} ({failed.action})"
        return CallToolResult(
            content=[TextContent(type="text", text=f"{failed.code}: {failed.message} at {where}")],
            structured_content=result.to_dict(),
            is_error=True,
        )
    return [Image(path=result.shot.path), result.to_dict()] if result.shot else [result.to_dict()]


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
def adopt(address: str) -> dict:
    """Explicitly hand one unfocused native Wayland window to this agent for hidden actions, by its exact address
    from windows. Refuses a client receiving the user's input, XWayland, grouped or pinned windows, and windows
    owned by another agent.
    Claims this window alone, not its process or sibling windows. Release, stop and plugin unload return it to its
    original workspace without closing it. Not available in a private --session."""
    return desk.adopt(address).to_dict()


@_tool()
def release(address: str) -> dict:
    """Return one individually adopted window to its original workspace, without closing its process.
    Give its exact address from windows. Refuses while the user's input is in its client."""
    return desk.release(address).to_dict()


@_tool()
def stop() -> str:
    """Close every app this agent launched. In isolated mode this ends the desktop; restart the MCP server to start
    another. Adopted windows return without closing. Later calls refuse the ended session rather than falling back
    to the user's desktop."""
    desk.stop()
    return "ok"


def serve() -> None:
    try:
        mcp.run()
    finally:
        desk.close()
