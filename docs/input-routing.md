# Input routing and hidden windows

[Back to the README](../README.md)

Clicks, scrolls, drags and keys run inside the compositor through the deskpilot
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

`--hidden` acts on one of the agent's launched windows, never the user's own, fully out of the user's view: no
workspace switch, no agent cursor, and the user's pointer, focus and screen stay as they are. The plugin
sends pointer input straight to that window's own client, and lets it draw while off screen so a screenshot after the
action shows the result. Tooltips and menus it opens show in that screenshot and take its clicks, and they never
grab the user's input. It works for native Wayland windows. An XWayland window (X11 and Wine apps) is refused,
since X clients read the one X pointer only the user's pointer moves; so is a window of the app the user's pointer is
on, unless that app took the `deskpilot` seat, and a drag and drop the press starts is cancelled. A single-instance
app that hands its window to a copy the user already runs opens nothing for the agent.

Hidden `key`, `type` and `paste` need `--window` naming one of the agent's launched windows, go to that window's own
client and never hold the user's input: keys never use the `deskpilot` seat, so an XWayland window is refused, and so
is a keystroke while the user's keyboard focus is in the window's app. A hidden `shot`, `hover` or
`wait-for --changed` of a window that lies outside its monitor's area, such as a column of the agent's workspace that
a scrolling layout puts off screen, is refused with `not_visible`: Hyprland sends no frame of it. A capture that gets
no frame in 5 seconds fails with `timeout`.

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

