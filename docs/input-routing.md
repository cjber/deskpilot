# Input routing and hidden windows

[Back to the README](../README.md)

Deskpilot never blocks the user. Clicks, scrolls, drags and keys run inside the compositor through the deskpilot
Hyprland plugin, which sends them straight to the target app's own pointer and keyboard: the user's pointer, cursor,
keyboard focus and input are left alone while they keep working, and the target is not raised, even with
`focus_on_activate` set. Two cases cannot work that way, and there the plugin briefly holds the user's mouse input,
moves the seat's pointer onto the window, presses, and puts the pointer back, applying the motion the user made
meanwhile: an XWayland window (X11 and Wine apps, which read the one X pointer), and a window of the app the user's
pointer is on. Keys likewise hold the user's input for a stroke only when their keyboard focus is in the target's app
or the target is an XWayland window. In those cases the plugin refuses a click when another window covers the point.

The plugin also adds a second seat named `deskpilot`, with a pointer only. GTK and Qt apps bind every seat they find
at startup, and such an app takes pointer actions through that seat's pointer: the user's pointer can be anywhere,
on the same app included, and nothing of theirs is held. Apps that bind only the first seat (Chromium, Electron,
kitty), XWayland apps, and apps started before the plugin first loaded since login keep the routes above. Keys
always use the routes above.

An agent cursor shows where deskpilot acts: a flight-path marker, a ring with wings, centred on the point. The plugin
draws it above every window, separate from the user's pointer: it glides along an arc to each point before the action
starts, lock-on brackets close in around it as it lands, it shrinks and sends out a ring at each press, follows a drag
as it moves, and fades after a few idle seconds. Window screenshots never include it. `DESKPILOT_CURSOR=off` turns it
off.

Several agents can work on one desktop at once, each with a cursor of its own. Set `DESKPILOT_AGENT` to a name (up to
32 letters, digits, `-`, `_` or `.`), such as `DESKPILOT_AGENT=CLAUDE-1`, and that agent's cursor carries the name on a
tag, in a colour taken from the name and kept apart from the other cursors on screen. Without it, actions share one
unnamed cursor. Actions still run one at a time; the cursors show whose each one is.

An agent can run apps of its own out of the user's sight. `DESKPILOT_AGENT=NAME deskpilot launch CMD...` starts
an app on the hidden special workspace `special:agent-NAME` and prints its first window. Every window the app opens,
then or later, lands there without taking focus, so a dialog never pops up on the user's screen. `deskpilot show`
toggles that workspace on the focused monitor for the user to watch or step in, and `deskpilot stop` closes the
agent's apps. They also close when the process named by `DESKPILOT_OWNER_PID` exits (the MCP server names itself),
or with no owner after 30 minutes unused. For web pages and signed-in browsers use
[fastbrowse](https://github.com/agent-labs-dev/fastbrowse), which drives Chrome over CDP.

For an isolated desktop, use `DESKPILOT_AGENT=NAME deskpilot --session launch CMD...`, then put `--session`
before each command that acts on it. This starts a private nested Hyprland with its own clipboard, keyboard,
accessibility bus and persistent application profile. XWayland applications can run there without sharing the
user's X server. Regular keyboard actions focus their target inside this private desktop, so application shortcuts
work there. `--hidden` can bring a window into view inside that desktop without changing the user's view.
`deskpilot --session stop` closes the desktop and its applications. With no owner it closes after 30 minutes unused;
an MCP server started with `--session mcp` owns its desktop until it exits. Starting a desktop requires 2 GB of
available memory, plus Hyprland, D-Bus, at-spi2-core, wayland-scanner, a C compiler and xkbcommon development files.
Profiles, including a private home directory and Wine prefix, remain under deskpilot's data directory for the same agent name.
The private desktop starts with a US keyboard layout.

For an application already running, `DESKPILOT_AGENT=NAME deskpilot adopt ADDRESS` explicitly hands one existing
native Wayland window to the agent. Use the exact address from `deskpilot windows`. Adoption refuses a client
receiving the user's keyboard or pointer input, XWayland, grouped or pinned windows, and another agent's windows. It claims that
window alone, even when a browser process also owns the user's other windows. `deskpilot release ADDRESS`,
`deskpilot stop` and plugin unload return it to its original workspace without closing the application.
A window the user moves out of the agent workspace or adds to a group is left where they put it. Adoption is for the regular
desktop; a private `--session` already owns its applications. Launch does not guess which window an existing
browser opened: use a private profile or explicitly adopt the chosen window.

`--hidden` acts on one of the agent's launched or explicitly adopted windows, fully out of the user's view: no
workspace switch, no agent cursor, and the user's pointer, focus and screen stay as they are. The plugin
sends pointer input straight to that window's own client, and lets it draw while off screen so a screenshot after the
action shows the result. Tooltips and menus it opens show in that screenshot and take its clicks, and they never
grab the user's input. It works for native Wayland windows. An XWayland window (X11 and Wine apps) is refused,
since X clients read the one X pointer only the user's pointer moves; so is a window of the app the user's pointer is
on, unless that app took the `deskpilot` seat, and a drag and drop the press starts is cancelled. A single-instance
app that hands its window to a copy the user already runs opens nothing for the agent.

Hidden `key`, `type` and `paste` need `--window` naming one of the agent's launched or adopted windows, go to that window's own
client and never hold the user's input. By default keys use the regular keyboard, so an XWayland window is refused, and so
is a keystroke while the user's keyboard focus is in the window's app. A hidden `shot`, `hover` or
`wait-for --changed` of a window that lies outside its monitor's area, such as a column of the agent's workspace that
a scrolling layout puts off screen, is refused with `not_visible`: Hyprland sends no frame of it. A capture that gets
no frame in 5 seconds fails with `timeout`.

`key` and `type` offer `--agent-keyboard` for native apps that support independent seats. It sends keys through
the `deskpilot` seat, using the active keyboard's keymap, without holding or entering the user's keyboard. It requires
`--window` and a client that bound that seat's keyboard. GTK 4 supports it; Qt can bind the keyboard while discarding
its events, so confirm the result with a fresh capture or accessibility tree. XWayland and paste use the regular route.

Keys sent to a window (`--window`) go to that window only, with each stroke complete before focus returns, and
the user's modifiers never leak into them. X11 apps take keys only from the window with X input focus, so an
XWayland target gets X focus for each stroke and sees a focus-in and focus-out around it; Hyprland's own focus
does not move.

Text is typed as written on the user's own keymap, whatever its layout and with Caps Lock on: the plugin finds the
key that gives each character on the active keyboard's keymap, which every client has, and presses that keymap's own
modifier keys (Shift, AltGr) around it, as a physical keyboard does. A character no key on that keymap gives is an
error rather than skipped.

`--take-pointer` uses a virtual mouse made with `/dev/uinput` instead, which the compositor treats as real
hardware. Tools that synthesize input another way, such as `xdotool` (XTest), are ignored by some clients:
Battle.net drops `xdotool` clicks.

## Requirements

- Hyprland, with `hyprctl` on `PATH`. Version 0.57's Lua dispatchers and the older string ones both work.
- The plugin build tools: a C++26 compiler, `make`, Hyprland's headers (`pkg-config hyprland`, shipped with
  Arch's `hyprland` package), `libxcb`, `libxkbcommon` and `cairo`.
- `grim` for screenshots.
- `wl-clipboard` for `clipboard`.
- For `--take-pointer`: write access to `/dev/uinput`, usually through the `input` group or a udev ACL. Check with
  `test -w /dev/uinput`.
- Python 3.12 or later and `uv`.
