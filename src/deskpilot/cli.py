"""deskpilot: drive Hyprland desktop windows from an agent.

Coordinates for move, click, scroll, hover and drag are pixels in the window's last `deskpilot shot`, so read the
screenshot, pick a point, click it. Pass --scale 1 to give window pixels instead, or --element with an id from
`deskpilot elements` to act on that element of the app's accessibility tree.

Clicks, scrolls, drags, hovers, keys and text run inside the compositor through the deskpilot Hyprland plugin
(`deskpilot plugin load`), so the user's pointer and keyboard focus stay where they are, and text arrives as written on
the user's keymap; a marker the plugin draws shows where the agent acts, one per DESKPILOT_AGENT name. --hidden acts
on a window wherever it is, on screen or not, with nothing shown. --take-pointer and --take-focus move the user's
own pointer or focus instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .desk import EXPECTED, Desk, error_code


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    elif isinstance(data, list):
        for row in data:
            print(row if isinstance(row, str) else json.dumps(row))
    else:
        print(data if isinstance(data, str) else json.dumps(data))


def _view_args(s: argparse.ArgumentParser) -> None:
    """How the command reaches a window that may be off screen."""
    group = s.add_mutually_exclusive_group()
    group.add_argument(
        "--show",
        action="store_true",
        help="bring a window that is not on screen into view by focusing it, instead of refusing",
    )
    group.add_argument(
        "--hidden",
        action="store_true",
        help="act on the window wherever it is, on screen or not, with no agent cursor and the user's pointer, focus "
        "and view untouched (not for XWayland windows' pointer input)",
    )


def _point_parser(sub, name: str, helptext: str, take_pointer: bool = True) -> argparse.ArgumentParser:
    """A command that acts at a point of a window's screenshot, or at an element of its accessibility tree."""
    s = sub.add_parser(name, help=helptext)
    s.add_argument("window")
    s.add_argument("x", type=float, nargs="?")
    s.add_argument("y", type=float, nargs="?")
    s.add_argument(
        "--element",
        help="an element id from `deskpilot elements`, instead of x and y: e5, or SNAPSHOT:e5 to refuse it once a newer"
        " snapshot has renumbered the elements",
    )
    _shot_args(s)
    if take_pointer:
        s.add_argument(
            "--take-pointer",
            action="store_true",
            help="move the user's own pointer there instead of leaving it in place",
        )
    return s


def _shot_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--scale", type=float, help="the scale the point is in; 1 for window pixels")
    s.add_argument("--shot", help="the shot id the point is in (default: the window's latest shot)")
    _view_args(s)


def _tree(element: dict, depth: int = 0) -> list[str]:
    """An accessibility tree as indented lines: id, role, name, bounds in window pixels, states and text."""
    name = f" {element['name']!r}" if element.get("name") else ""
    text = f" text={element['text']!r}" if element.get("text") else ""
    states = f" [{', '.join(element['states'])}]" if element.get("states") else ""
    line = (
        f"{'  ' * depth}{element['id']} {element['role']}{name} "
        f"{element['x']},{element['y']} {element['width']}x{element['height']}{states}{text}"
    )
    return [line] + [row for child in element.get("children", []) for row in _tree(child, depth + 1)]


def _text(arg: str) -> str:
    return sys.stdin.read() if arg == "-" else arg


def _size(text: str) -> tuple[int, int]:
    w, _, h = text.partition("x")
    try:
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"size must be WIDTHxHEIGHT, not {text!r}") from None


def _path_commands(sub) -> None:
    """hover, drag and elements: pointer paths and the accessibility tree that names their targets."""
    s = _point_parser(
        sub,
        "hover",
        "rest the pointer on a point for a tooltip or a hover menu and print a screenshot taken meanwhile",
        take_pointer=False,
    )
    s.add_argument("--wait", type=float, default=1.0, help="seconds to rest there before the screenshot (up to 5)")
    s.add_argument(
        "--click",
        type=float,
        nargs=2,
        metavar=("X", "Y"),
        help="click this point next, as in a menu that opened on hover",
    )
    s.add_argument("--button", choices=["left", "right", "middle"], default="left")
    s.add_argument("--max-width", type=int, default=1400, help="scale down wider windows to this width")

    s = sub.add_parser("drag", help="press at one point, move to another and release, for a slider or a selection")
    s.add_argument("window")
    s.add_argument("x1", type=float)
    s.add_argument("y1", type=float)
    s.add_argument("x2", type=float)
    s.add_argument("y2", type=float)
    s.add_argument("--button", choices=["left", "right", "middle"], default="left")
    s.add_argument("--duration", type=float, default=0.3, help="seconds the move takes (up to 5)")
    _shot_args(s)

    s = sub.add_parser("elements", help="print a window's accessibility tree, with ids that --element takes")
    s.add_argument("window")
    s.add_argument(
        "--peek",
        action="store_true",
        help="read the tree without replacing the latest snapshot, whose ids stay good; a peek's ids name nothing",
    )
    _view_args(s)


def _wait_command(sub) -> None:
    s = sub.add_parser(
        "wait-for", help="wait for a window to appear, close, take focus, change title or size, or look different"
    )
    s.add_argument("window")
    s.add_argument("--timeout", type=float, default=60)
    s.add_argument("--gone", action="store_true", help="wait for it to close")
    s.add_argument("--focused", action="store_true", help="wait until it has keyboard focus")
    s.add_argument("--title", help="wait until its title contains this")
    s.add_argument("--size", type=_size, metavar="WxH", help="wait until it is this size in window pixels")
    s.add_argument(
        "--changed", metavar="SHOT", help="wait until it looks different from this shot; prints a shot of the change"
    )
    s.add_argument(
        "--region", type=int, nargs=4, metavar=("X", "Y", "W", "H"), help="with --changed, only this part of the shot"
    )
    s.add_argument(
        "--hidden", action="store_true", help="with --changed, watch the window wherever it is, on screen or not"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deskpilot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true", help="print JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("windows", help="list mapped windows with geometry")

    s = sub.add_parser("focus", help="focus a window")
    s.add_argument("window")

    s = sub.add_parser("shot", help="screenshot a window; later clicks use its pixels")
    s.add_argument("window")
    s.add_argument("-o", "--out", type=Path)
    s.add_argument("--max-width", type=int, default=1400, help="scale down wider windows to this width")
    _view_args(s)

    _point_parser(sub, "move", "show the agent cursor at a point")
    s = _point_parser(sub, "click", "click a point")
    s.add_argument("--button", choices=["left", "right", "middle"], default="left")
    s.add_argument("--double", action="store_true")
    s.add_argument(
        "--take-focus",
        action="store_true",
        help="focus the window first and leave it focused, for a menu or dropdown that closes when it is not focused",
    )
    s = _point_parser(sub, "scroll", "scroll at a point; positive is up")
    s.add_argument("dy", type=int)

    _path_commands(sub)

    s = sub.add_parser("plugin", help="build, load or check the deskpilot Hyprland plugin")
    s.add_argument("action", choices=["build", "load", "status"])

    focus_help = "focus the window first and leave it focused, for an app that ignores keys while unfocused"
    s = sub.add_parser("key", help="press a key or chord, e.g. Return, ctrl+a, alt+F4")
    s.add_argument("combo")
    s.add_argument("--window", help="press it in this window; without it the keys go to the focused window")
    s.add_argument("--take-focus", action="store_true", help=focus_help)
    s.add_argument("--hold", type=float, default=0.0, help="seconds to hold each key down (up to 10)")

    s = sub.add_parser("type", help="type text as written, on the user's keymap")
    s.add_argument("text")
    s.add_argument("--window", help="type into this window; without it the keys go to the focused window")
    s.add_argument("--take-focus", action="store_true", help=focus_help)
    s.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds each key is held and between keys, none by default; games want 0.1 or more",
    )

    s = sub.add_parser(
        "paste", help="paste text into a window through a clipboard only it sees; any text, whatever the keymap types"
    )
    s.add_argument("text", help="the text, or - to read it from stdin")
    s.add_argument("--window", help="paste into this window; without it the text goes to the focused window")
    s.add_argument("--take-focus", action="store_true", help=focus_help)
    s.add_argument("--keys", default="ctrl+v", help="the app's paste chord, e.g. ctrl+shift+v in a terminal")

    s = sub.add_parser("clipboard", help="print the user's clipboard text, or replace it with --set")
    s.add_argument("--set", metavar="TEXT", help="the text to copy, or - to read it from stdin")

    _wait_command(sub)

    sub.add_parser("cursor", help="print the cursor's global position")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    return p


def run(args: argparse.Namespace, desk: Desk) -> object:
    match args.cmd:
        case "windows":
            return [w.to_dict() for w in desk.windows()]
        case "focus":
            return desk.focus(args.window).to_dict()
        case "shot":
            return desk.shot(args.window, args.out, args.max_width, args.show, args.hidden).to_dict()
        case "move":
            if args.hidden:
                raise ValueError("move only shows the agent cursor, which a hidden action never shows")
            gx, gy = desk.move(
                args.window,
                args.x,
                args.y,
                args.scale,
                args.take_pointer,
                shot=args.shot,
                show=args.show,
                element=args.element,
            )
            return {"x": gx, "y": gy}
        case "click":
            count = 2 if args.double else 1
            gx, gy = desk.click(
                args.window,
                args.x,
                args.y,
                args.button,
                count,
                args.scale,
                args.take_pointer,
                shot=args.shot,
                show=args.show,
                take_focus=args.take_focus,
                element=args.element,
                hidden=args.hidden,
            )
            return {"x": gx, "y": gy}
        case "scroll":
            gx, gy = desk.scroll(
                args.window,
                args.x,
                args.y,
                args.dy,
                args.scale,
                args.take_pointer,
                shot=args.shot,
                show=args.show,
                element=args.element,
                hidden=args.hidden,
            )
            return {"x": gx, "y": gy}
        case "hover":
            result = desk.hover(
                args.window,
                args.x,
                args.y,
                args.wait,
                args.scale,
                shot=args.shot,
                show=args.show,
                element=args.element,
                then_click=tuple(args.click) if args.click else None,
                button=args.button,
                max_width=args.max_width,
                hidden=args.hidden,
            )
            if isinstance(result, tuple):
                return {"x": result[0], "y": result[1]}
            return result.to_dict()
        case "drag":
            gx, gy = desk.drag(
                args.window,
                (args.x1, args.y1),
                (args.x2, args.y2),
                args.button,
                args.duration,
                args.scale,
                shot=args.shot,
                show=args.show,
                hidden=args.hidden,
            )
            return {"x": gx, "y": gy}
        case "elements":
            tree = desk.elements(args.window, args.show, args.hidden, args.peek).to_dict()
            if args.json:
                return tree
            return [f"snapshot {tree['snapshot']}", *_tree(tree)] if "snapshot" in tree else _tree(tree)
        case "key":
            desk.key(args.combo, args.window, args.take_focus, args.hold)
            return "ok"
        case "type":
            desk.type(args.text, args.window, args.delay, args.take_focus)
            return "ok"
        case "paste":
            desk.paste(_text(args.text), args.window, args.take_focus, args.keys)
            return "ok"
        case "clipboard":
            if args.set is None:
                return desk.clipboard()
            desk.set_clipboard(_text(args.set))
            return "ok"
        case "wait-for":
            if args.changed:
                if args.gone or args.focused or args.title is not None or args.size:
                    raise ValueError("--changed waits on pixels alone: drop --gone, --focused, --title and --size")
                shot, box = desk.wait_changed(args.window, args.changed, args.timeout, args.region, args.hidden)
                return {**shot.to_dict(), "changed": list(box)}
            if args.region or args.hidden:
                raise ValueError("--region and --hidden need --changed")
            win = desk.wait_for(args.window, args.timeout, args.gone, args.focused, args.title, args.size)
            return win.to_dict() if win else "gone"
        case "plugin":
            if args.action == "build":
                return {"path": str(desk.plugin.build())}
            if args.action == "load":
                return {"path": str(desk.plugin.load()), **desk.plugin.status()}
            return desk.plugin.status()
        case "cursor":
            x, y = desk.hypr.cursor()
            return {"x": x, "y": y}
    raise AssertionError(args.cmd)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import serve

        serve()
        return 0
    desk = Desk()
    try:
        _print(run(args, desk), args.json)
    except EXPECTED as error:
        print(f"deskpilot: {error_code(error)}: {error}", file=sys.stderr)
        return 2
    finally:
        desk.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
