# deskpilot

Desktop control for agents on Hyprland. An agent takes a screenshot of a window, picks a point in it, and clicks
or types there, in any app: native Wayland, XWayland and Wine programs such as the Battle.net launcher and games.

The user keeps their own pointer. Clicks and scrolls run inside the compositor through the deskpilot Hyprland
plugin: in one step it holds the user's mouse input, presses on the target window, then puts the pointer and
keyboard focus back where they were, applying any mouse motion the user made meanwhile. A press goes through
Hyprland's own input path, so Wayland, XWayland and Wine clients all receive it as a real click. The plugin refuses
a click when another window covers the point.

An agent cursor shows where deskpilot acts: [cua-driver](https://github.com/trycua/cua)'s overlay arrow glides to
each point, drawn above every window and separate from the user's pointer. Without `cua-driver` on `PATH`, or with
`DESKPILOT_CURSOR=off`, actions run without it.

Keys go through a virtual input device made with `/dev/uinput`, which the compositor treats as real hardware.
Tools that synthesize input another way, such as `xdotool` (XTest) or `wtype` (the virtual keyboard protocol), are
ignored by some clients: Battle.net drops `xdotool` clicks, and a game drops `wtype` keys.

## Requirements

- Hyprland, with `hyprctl` on `PATH`. Version 0.57's Lua dispatchers and the older string ones both work.
- For clicks and scrolls, the plugin build tools: a C++26 compiler, `make` and Hyprland's headers (`pkg-config
  hyprland`, shipped with Arch's `hyprland` package).
- Optional: `cua-driver` for the agent cursor.
- `grim` for screenshots.
- Write access to `/dev/uinput`, usually through the `input` group or a udev ACL. Check with `test -w /dev/uinput`.
- Python 3.12 or later and `uv`.

## Install

```sh
uv tool install git+https://github.com/cjber/deskpilot
```

## Use

```sh
deskpilot plugin load                     # build for the running Hyprland and load; again after each upgrade
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

`move` only shows the agent cursor. Pass `--take-pointer` to `move`, `click` or `scroll` to move the user's own
pointer there and press through the input device instead, for a hover the app must see.

A shell started before Hyprland restarted keeps a stale `HYPRLAND_INSTANCE_SIGNATURE`. deskpilot then talks to
the one instance whose socket answers, and with several live instances it stops and names them.

Games read keys once per frame, so type into them with `--delay 0.1` or more.

## MCP

`deskpilot mcp` serves the same actions over stdio: `windows`, `focus`, `shot` (returns the image), `click`,
`move`, `scroll`, `key`, `type_text` and `wait_for`. `click`, `move` and `scroll` take `take_pointer`. It keeps one input
device and one agent cursor open for the session.

```sh
claude mcp add deskpilot -- deskpilot mcp
```

## Limits

- Text is typed on a US layout; a character that layout cannot type is an error rather than skipped.
- Keys focus the target window and leave it focused.
- Hyprland plugins load only into the exact Hyprland they were built against, so `deskpilot plugin load` builds
  one per Hyprland commit under `~/.local/share/deskpilot`.
- Each CLI action that uses the input device opens a new one and waits about a second for libinput to add it. The MCP server
  avoids that wait.
- Hyprland only, Linux only.
