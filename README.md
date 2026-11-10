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

Text is typed as written on the user's own keymap, whatever its layout and with Caps Lock on: the plugin finds the
key that gives each character on the active keyboard's keymap, which every client has, and sets the modifiers it
needs directly. A character no key on that keymap gives is an error rather than skipped.

`--take-pointer` uses a virtual mouse made with `/dev/uinput` instead, which the compositor treats as real
hardware. Tools that synthesize input another way, such as `xdotool` (XTest), are ignored by some clients:
Battle.net drops `xdotool` clicks.

## Requirements

- Hyprland, with `hyprctl` on `PATH`. Version 0.57's Lua dispatchers and the older string ones both work.
- The plugin build tools: a C++26 compiler, `make`, Hyprland's headers (`pkg-config hyprland`, shipped with
  Arch's `hyprland` package), `libxcb` and `libxkbcommon`.
- Optional: `cua-driver` for the agent cursor.
- `grim` for screenshots.
- `wl-clipboard` for `clipboard`.
- For `--take-pointer`: write access to `/dev/uinput`, usually through the `input` group or a udev ACL. Check with
  `test -w /dev/uinput`.
- Python 3.12 or later and `uv`.

## Install

```sh
uv tool install deskpilot
```

The agent skill, which teaches an agent the steps below, installs in Claude Code with:

```text
/plugin install deskpilot --marketplace cjber/deskpilot
```

## Use

```sh
deskpilot windows                         # every window: address, class, title, geometry, whether on screen
deskpilot shot battle.net.exe             # screenshot; prints its id, image path and scale
deskpilot click battle.net.exe 920 367 --shot ID    # a point in that screenshot
deskpilot drag gimp 100 400 500 400       # press, move and release: sliders, selections, drag and drop
deskpilot hover firefox 300 40 --wait 1   # rest the pointer there; prints a shot showing the tooltip
deskpilot elements gedit                  # the app's accessibility tree, with ids
deskpilot click gedit --element e12       # the centre of that element, where the app says it is now
deskpilot key Right --window kitty --hold 2         # hold a key down two seconds
deskpilot type "/reload" --window wow.exe --delay 0.12
deskpilot key ctrl+a --window kitty
deskpilot paste "Grüße, 日本" --window firefox    # any text, through a clipboard only that window sees
deskpilot clipboard                       # the user's clipboard text; --set TEXT replaces it
deskpilot wait-for wow.exe --timeout 120
deskpilot wait-for wow.exe --changed ID --region 0 0 400 80   # until that part of shot ID looks different
```

A window is named by its address, its class, or a substring of its class or title. A name that matches more
than one window is an error listing them, so a click never lands on a guessed window.

`shot` captures the window itself, so a window on top does not show in it, and saves it under an id. `move`,
`click` and `scroll` take pixels in the shot named by `--shot`, or the window's latest one, whatever scale it was
taken at. A window resized since that shot is refused, as is a point outside the window. Pass `--scale 1` to give
window pixels instead.

A window on a hidden workspace or scrolled off its monitor is reported as `"visible": false`, and `shot`, `move`,
`click` and `scroll` refuse it rather than move the user's view. `--show` focuses it to bring it on screen first.

The first action that needs the plugin builds and loads it for the running Hyprland, so it comes back by itself
after Hyprland restarts or upgrades. `deskpilot plugin load` does the same ahead of time.

Some apps close a menu or dropdown as soon as it opens when their window is not the focused one, as Chrome does
with many web page dropdowns. `click --take-focus` focuses the window first and leaves it focused, so the list stays
open for the next click.

`move` only shows the agent cursor. Pass `--take-pointer` to `move`, `click` or `scroll` to move the user's own
pointer there and press through the input device instead.

`drag` presses at one point, moves to another over `--duration` seconds (0.3 by default, up to 5) and releases, with
the user's pointer left where it is: sliders, text selection, drawing, and drag and drop within the window. `hover`
rests the pointer on a point for `--wait` seconds and prints a screenshot taken while it is there, so a tooltip or a
hover state shows in it; `--click X Y` clicks a second point next instead, before the pointer leaves, for a menu
that opens on hover. Both hold the user's pointer input for as long as they take and replay it after, so they are
bounded: a drag or hover that fails, or whose window closes, still lets go of its button.

`elements` prints the window's accessibility tree (AT-SPI): each element's id, role, name, states, text and bounds
in window pixels. `click`, `move`, `scroll` and `hover` take `--element ID` instead of a point, and act on the
element's centre where the app reports it at that moment. GTK and Qt apps expose a tree; Chromium, Electron and
Firefox expose one only when started with accessibility on (`--force-renderer-accessibility` for Chromium and
Electron), and Wine games expose none. An app with no tree is an error saying so, never a guessed one.

A shell started before Hyprland restarted keeps a stale `HYPRLAND_INSTANCE_SIGNATURE`. deskpilot then talks to
the one instance whose socket answers, and with several live instances it stops and names them.

`key --hold SECONDS` holds each key down that long, up to 10, for auto-repeat or a game's movement.

`key` and `type` without `--window` press keys into the focused window. `--take-focus` focuses the window first and
leaves it focused, for an app that ignores keys while unfocused. Keys go to the window's client, not through the
compositor, so they never trigger a Hyprland keybind: use `hyprctl dispatch` for those.

`paste` hands text to a window as a clipboard only that window sees, then presses its paste chord (`--keys`,
`ctrl+v` by default; most terminals want `ctrl+shift+v`). It pastes any text, including characters the keymap cannot
type, and the user's clipboard stays as it is. XWayland windows share one clipboard, so `paste` refuses them: use
`type`. `clipboard` reads the user's clipboard, and `clipboard --set` replaces it.

Games read keys once per frame, so type into them with `--delay 0.1` or more: each key is held that long, with the
same gap before the next.

`wait-for` waits for a window to appear, or with `--gone` to close. `--focused`, `--title TEXT` and `--size WxH` also
wait for it to have keyboard focus, a title containing TEXT or that size. `--changed SHOT` waits until the window
looks different from one of its shots, in all of it or in `--region X Y W H` of that shot's pixels: any pixel
difference counts, and it prints a shot of the change with the box that changed.

Clicks, scrolls and keys from several agents at once run one after another in the order they arrive, so one `type`
is never interleaved with another's keys. Up to 32 wait behind the running one; past that an action is refused.

## MCP

`deskpilot mcp` serves the same actions over stdio: `windows`, `focus`, `shot` (returns the image and its shot
id), `click`, `move`, `scroll`, `hover` (returns the image), `drag`, `elements`, `key`, `type_text`, `paste_text`,
`clipboard`, `copy_text`, `wait_for` and `wait_changed`. `click`, `move` and `scroll` take `shot`, `show` and
`take_pointer`; `click`, `move`, `scroll` and `hover` take `element`; `click`, `key`, `type_text` and `paste_text`
take `take_focus`; `key` takes `hold`. It keeps one input device and one agent cursor open for the session.

```sh
claude mcp add deskpilot -- deskpilot mcp
```

## Limits

- `type` can only send characters the active keymap types; a character it lacks (such as `é` on a US layout) is
  an error rather than skipped. `paste` takes any text, except into XWayland windows.
- Hyprland plugins load only into the exact Hyprland they were built against, so `deskpilot plugin load` builds
  one per Hyprland commit and plugin source under `~/.local/share/deskpilot`, and replaces a loaded build of
  other source.
- Each CLI action that uses the input device opens a new one and waits about a second for libinput to add it. The MCP server
  avoids that wait.
- `drag` stays within one window: a drag and drop into another app is not supported.
- Hyprland only, Linux only.

## Development

[AGENTS.md](AGENTS.md) has the checks, the layout and the rules a change must keep, and
[CONTRIBUTING.md](CONTRIBUTING.md) how to report a bug or send a change.
