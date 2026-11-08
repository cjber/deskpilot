# deskpilot

Desktop control for agents on Hyprland. An agent takes a screenshot of a window, picks a point in it, and clicks
or types there, in any app: native Wayland, XWayland and Wine programs such as the Battle.net launcher and games.

Clicks and keys go through a virtual input device made with `/dev/uinput`, which the compositor treats as real
hardware. Tools that synthesize input another way, such as `xdotool` (XTest) or `wtype` (the virtual keyboard
protocol), are ignored by some clients: Battle.net drops `xdotool` clicks, and a game drops `wtype` keys. The
cursor is placed by Hyprland itself, so positions are always in the compositor's own coordinates, with no
XWayland offset to correct.

## Requirements

- Hyprland, with `hyprctl` on `PATH`. Version 0.57's Lua dispatchers and the older string ones both work.
- `grim` for screenshots.
- Write access to `/dev/uinput`, usually through the `input` group or a udev ACL. Check with `test -w /dev/uinput`.
- Python 3.12 or later and `uv`.

## Install

```sh
uv tool install git+https://github.com/cjber/deskpilot
```

## Use

```sh
deskpilot windows                         # every window, with its address, class, title and geometry
deskpilot shot battle.net.exe             # screenshot; prints the image path and its scale
deskpilot click battle.net.exe 920 367    # a point in that screenshot
deskpilot type "/reload" --window wow.exe --delay 0.12
deskpilot key Return
deskpilot wait-for wow.exe --timeout 120
```

A window is named by its address, its class, or a substring of its class or title. A name that matches more
than one window is an error listing them, so a click never lands on a guessed window.

`move`, `click` and `scroll` take pixels in the window's last `shot`, whatever scale it was taken at. Pass
`--scale 1` to give window pixels instead. A point outside the window is refused.

Games read keys once per frame, so type into them with `--delay 0.1` or more.

## MCP

`deskpilot mcp` serves the same actions over stdio: `windows`, `focus`, `shot` (returns the image), `click`,
`move`, `scroll`, `key`, `type_text` and `wait_for`. It keeps one input device open for the session, so actions
skip the device's settle delay.

```sh
claude mcp add deskpilot -- deskpilot mcp
```

## Limits

- Text is typed on a US layout; a character that layout cannot type is an error rather than skipped.
- Each CLI action opens a new input device and waits about a second for libinput to add it. The MCP server
  avoids that wait.
- Hyprland only, Linux only.
