"""deskpilot: drive Hyprland desktop windows from an agent.

Coordinates for move, click and scroll are pixels in the window's last `deskpilot shot`, so read the screenshot,
pick a point, click it. Pass --scale 1 to give window pixels instead.

Clicks, scrolls, keys and text run inside the compositor through the deskpilot Hyprland plugin (`deskpilot plugin
load`), so the user's pointer and keyboard focus stay where they are, and text arrives as written on the user's
keymap. --take-pointer and --take-focus move the user's own pointer or focus instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .desk import EXPECTED, Desk


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    elif isinstance(data, list):
        for row in data:
            print(row if isinstance(row, str) else json.dumps(row))
    else:
        print(data if isinstance(data, str) else json.dumps(data))


SHOW_HELP = "bring a window that is not on screen into view by focusing it, instead of refusing"


def _point_parser(sub, name: str, helptext: str) -> argparse.ArgumentParser:
    """A command that acts at a point of a window's screenshot."""
    s = sub.add_parser(name, help=helptext)
    s.add_argument("window")
    s.add_argument("x", type=float)
    s.add_argument("y", type=float)
    s.add_argument("--scale", type=float, help="the scale the point is in; 1 for window pixels")
    s.add_argument("--shot", help="the shot id the point is in (default: the window's latest shot)")
    s.add_argument("--show", action="store_true", help=SHOW_HELP)
    s.add_argument(
        "--take-pointer", action="store_true", help="move the user's own pointer there instead of leaving it in place"
    )
    return s


def _text(arg: str) -> str:
    return sys.stdin.read() if arg == "-" else arg


def _size(text: str) -> tuple[int, int]:
    w, _, h = text.partition("x")
    try:
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"size must be WIDTHxHEIGHT, not {text!r}") from None


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
    s.add_argument("--show", action="store_true", help=SHOW_HELP)

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

    s = sub.add_parser("plugin", help="build, load or check the deskpilot Hyprland plugin")
    s.add_argument("action", choices=["build", "load", "status"])

    focus_help = "focus the window first and leave it focused, for an app that ignores keys while unfocused"
    s = sub.add_parser("key", help="press a key or chord, e.g. Return, ctrl+a, alt+F4")
    s.add_argument("combo")
    s.add_argument("--window", help="press it in this window; without it the keys go to the focused window")
    s.add_argument("--take-focus", action="store_true", help=focus_help)

    s = sub.add_parser("type", help="type text as written, on the user's keymap")
    s.add_argument("text")
    s.add_argument("--window", help="type into this window; without it the keys go to the focused window")
    s.add_argument("--take-focus", action="store_true", help=focus_help)
    s.add_argument(
        "--delay", type=float, default=0.03, help="seconds each key is held and between keys; games want 0.1 or more"
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
            return desk.shot(args.window, args.out, args.max_width, args.show).to_dict()
        case "move":
            gx, gy = desk.move(
                args.window, args.x, args.y, args.scale, args.take_pointer, shot=args.shot, show=args.show
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
            )
            return {"x": gx, "y": gy}
        case "scroll":
            gx, gy = desk.scroll(
                args.window, args.x, args.y, args.dy, args.scale, args.take_pointer, shot=args.shot, show=args.show
            )
            return {"x": gx, "y": gy}
        case "key":
            desk.key(args.combo, args.window, args.take_focus)
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
                shot, box = desk.wait_changed(args.window, args.changed, args.timeout, args.region)
                return {**shot.to_dict(), "changed": list(box)}
            if args.region:
                raise ValueError("--region needs --changed")
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
        print(f"deskpilot: {error}", file=sys.stderr)
        return 2
    finally:
        desk.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
