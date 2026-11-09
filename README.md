# deskpilot

Desktop control for agents on Hyprland. An agent takes a screenshot of a window, picks a point in it, and clicks
or types there, in any app: native Wayland, XWayland and Wine programs such as the Battle.net launcher and games.

The user keeps their own pointer and keyboard focus. Clicks, scrolls and keys run inside the compositor through
the deskpilot Hyprland plugin: in one step it holds the user's mouse input, delivers the event to the target window
through the user's own seat, then puts the pointer back where it was, applying any mouse motion the user made
meanwhile. Only pointer focus moves, so keyboard focus stays on the user's window and the target is not raised, even
with `focus_on_activate` set. Wayland, XWayland and Wine clients all receive the press as a real click. The plugin refuses
a click when another window covers the point.

An agent cursor shows where deskpilot acts: [cua-driver](https://github.com/trycua/cua)'s overlay arrow glides to
each point, drawn above every window and separate from the user's pointer. Without `cua-driver` on `PATH`, or with
`DESKPILOT_CURSOR=off`, actions run without it.

Keys sent to a window (`--window`) go to that window only, with each stroke complete before focus returns, and
the user's modifiers never leak into them. X11 apps take keys only from the window with X input focus, so an
XWayland target gets X focus for each stroke and sees a focus-in and focus-out around it; Hyprland's own focus
does not move.

`--take-pointer` and `--take-focus` use a virtual input device made with `/dev/uinput` instead, which the
compositor treats as real hardware. Tools that synthesize input another way, such as `xdotool` (XTest) or `wtype`
(the virtual keyboard protocol), are ignored by some clients: Battle.net drops `xdotool` clicks, and a game drops
`wtype` keys.

## Requirements

- Hyprland, with `hyprctl` on `PATH`. Version 0.57's Lua dispatchers and the older string ones both work.
- The plugin build tools: a C++26 compiler, `make`, Hyprland's headers (`pkg-config hyprland`, shipped with
  Arch's `hyprland` package), `libxcb` and `libxkbcommon`.
- Optional: `cua-driver` for the agent cursor.
- `grim` for screenshots.
- For `--take-pointer`, `--take-focus` and keys without `--window`: write access to `/dev/uinput`, usually through
  the `input` group or a udev ACL. Check with `test -w /dev/uinput`.
- Python 3.12 or later and `uv`.

## Install

```sh
uv tool install git+https://github.com/cjber/deskpilot
```

The agent skill, which teaches an agent the steps below, installs in Claude Code with:

```text
/plugin install deskpilot --marketplace cjber/deskpilot
```

## Use

```sh
deskpilot plugin load                     # build for the running Hyprland and load; again after each upgrade
deskpilot windows                         # every window, with its address, class, title and geometry
deskpilot shot battle.net.exe             # screenshot; prints the image path and its scale
deskpilot click battle.net.exe 920 367    # a point in that screenshot
deskpilot type "/reload" --window wow.exe --delay 0.12
deskpilot key ctrl+a --window kitty
deskpilot wait-for wow.exe --timeout 120
```

A window is named by its address, its class, or a substring of its class or title. A name that matches more
than one window is an error listing them, so a click never lands on a guessed window.

`move`, `click` and `scroll` take pixels in the window's last `shot`, whatever scale it was taken at. Pass
`--scale 1` to give window pixels instead. A point outside the window is refused.

`move` only shows the agent cursor. Pass `--take-pointer` to `move`, `click` or `scroll` to move the user's own
pointer there and press through the input device instead, for a hover the app must see.

A shell started before Hyprland restarted keeps a stale `HYPRLAND_INSTANCE_SIGNATURE`. deskpilot then talks to
the one instance whose socket answers, and with several live instances it stops and names them.

`key` and `type` without `--window` press keys into whatever has focus. `--take-focus` focuses the window and
leaves it focused, for an app that ignores keys while unfocused.

Games read keys once per frame, so type into them with `--delay 0.1` or more.

## MCP

`deskpilot mcp` serves the same actions over stdio: `windows`, `focus`, `shot` (returns the image), `click`,
`move`, `scroll`, `key`, `type_text` and `wait_for`. `click`, `move` and `scroll` take `take_pointer`; `key` and
`type_text` take `take_focus`. It keeps one input
device and one agent cursor open for the session.

```sh
claude mcp add deskpilot -- deskpilot mcp
```

## Limits

- Text is typed on a US layout; a character that layout cannot type is an error rather than skipped.
- Hyprland plugins load only into the exact Hyprland they were built against, so `deskpilot plugin load` builds
  one per Hyprland commit and plugin source under `~/.local/share/deskpilot`, and replaces a loaded build of
  other source.
- Each CLI action that uses the input device opens a new one and waits about a second for libinput to add it. The MCP server
  avoids that wait.
- Hyprland only, Linux only.

## Development

[AGENTS.md](AGENTS.md) has the checks, the layout and the rules a change must keep, and
[CONTRIBUTING.md](CONTRIBUTING.md) how to report a bug or send a change.
