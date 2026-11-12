---
name: deskpilot
description: Click, scroll and type in desktop app windows on Hyprland with the deskpilot CLI or MCP server, while the user keeps their own pointer and keyboard focus. Works in native Wayland, XWayland and Wine apps such as the Battle.net launcher and games. Use for 'click the button in that app', 'type into the game', 'drive this desktop window', 'take a screenshot of a window', or any GUI task in a Hyprland session.
---

# deskpilot

When other agents may be driving the same desktop, set `DESKPILOT_AGENT` to a short name for yourself (such as
`CLAUDE-1`), so the user sees which cursor is yours. Run `deskpilot --help` for the command list, and put `--json` before the command for JSON output. Over MCP the same
actions are tools: `windows`, `focus`, `shot`, `click`, `move`, `scroll`, `hover`, `drag`, `elements`, `key`,
`type_text`, `paste_text`, `clipboard`, `copy_text`, `wait_for` and `wait_changed`.

If `deskpilot` is not on `PATH`, ask the user before installing it with
`uv tool install deskpilot`.

1. The first click, scroll or key loads the plugin for the running Hyprland, building it if needed, which can take
   a minute once per Hyprland version. `deskpilot plugin status` reports the loaded build.
2. Run `deskpilot windows` and pick the target by address, class, or part of its class or title. A name that matches
   several windows is an error that lists them: pick one by address rather than guessing. `visible` is false for a
   window on a hidden workspace or scrolled off screen; acting on it is refused unless you pass `--show`, which
   focuses it and moves the user's view, so ask the user before `--show`.
   To work out of the user's sight, set `DESKPILOT_AGENT` to a name and start your own copy of the app with
   `deskpilot launch CMD...`: it and every window it opens later stay on your hidden workspace, and `--hidden` acts
   on them, or windows explicitly handed to you with `adopt ADDRESS`, on `shot`, `click`, `scroll`, `drag`, `hover`,
   `elements`, `key`,
   `type`, `paste` and `wait-for --changed`. An XWayland window is refused hidden. Hidden keys need `--window` and
   are refused while the user's keyboard focus is in that window's app. A hidden `shot`, `hover` or
   `wait-for --changed` of a window scrolled outside its monitor's area is refused, since Hyprland sends no frame of
   it. `deskpilot stop` closes your apps when you are done; `deskpilot show` lets the user see them. For web pages
   and browsers use fastbrowse instead.
   For an existing native app, `deskpilot adopt ADDRESS` hands only the exact window from `windows` to your agent.
   It refuses XWayland, grouped or pinned windows, a client receiving the user's input, or another agent's window. Never infer a forwarded
   launch's window from its title or timing. `deskpilot release ADDRESS`, stop and plugin unload return adopted
   windows to their original workspace without closing their shared process. A user-moved or regrouped window stays where
   the user put it. Adoption is unavailable inside `--session`.
3. Run `deskpilot shot WINDOW`, read the image at the `path` it prints, and choose a point in that image. Note its
   `shot` id.
4. Act with that point as given, passing the id: `deskpilot click WINDOW X Y --shot ID` (`--button right`,
   `--double`), `deskpilot scroll WINDOW X Y DY --shot ID` (positive is up), or `deskpilot move WINDOW X Y --shot ID`
   to show the agent cursor there. Points are pixels in that shot, so never rescale them yourself; `--scale 1` means
   window pixels. A point outside the window, one another window covers, or a window resized since the shot is
   refused. `deskpilot drag WINDOW X1 Y1 X2 Y2 --shot ID` presses, moves and releases (sliders, selections, drag and
   drop within the window). `deskpilot hover WINDOW X Y --shot ID` rests the pointer there and prints a shot taken
   meanwhile, for tooltips; `--click X2 Y2` clicks next, for a menu that opens on hover.
   For a GTK or Qt app, `deskpilot elements WINDOW` lists its accessibility tree with ids, and
   `--element ID` replaces X Y on `click`, `move`, `scroll` and `hover`. Prefer it where the app has a tree: the
   app reports where the element is. An app without one says so; use a shot instead. An item of an open GTK 3 menu
   or combo box is clicked on its popup; an item `elements` lists with zero bounds is on a popup that cannot be told
   apart, so take a new snapshot or use the keyboard.
5. Type with `deskpilot type "TEXT" --window WINDOW` and press keys with `deskpilot key ctrl+a --window WINDOW`. Games
   read keys once per frame, so add `--delay 0.1` or more for them (other apps take keys with no delay; use `paste` for long text), and `key --hold SECONDS` to keep a key down: each key is held that long, with the same gap
   before the next. Text arrives as written on the user's keymap; a character no key on it types is an error, so
   send such text, or a long one, with `deskpilot paste "TEXT" --window WINDOW` (`--keys ctrl+shift+v` in a
   terminal). A paste uses a clipboard only that window sees; XWayland windows refuse it, and so does an app the user last copied in (`owns_clipboard`). `deskpilot clipboard`
   reads the user's clipboard, and `--set TEXT` replaces it: only do that when the user asks.
   Several agents may act at once: actions run one after another in arrival order, and one `type` is never
   interleaved with another's keys.
6. Take a new `shot` after each action that changes the window, and confirm the result before the next step.
   `deskpilot wait-for WINDOW --timeout 120` waits for a window to open, and `--gone` for it to close; `--focused`,
   `--title TEXT` and `--size WxH` wait for those. After an action whose result takes time to show, wait with
   `deskpilot wait-for WINDOW --changed ID` (optionally `--region X Y W H` in that shot's pixels) instead of
   sleeping: it returns a shot of the change, so read that shot rather than taking another.

Done when a fresh `shot` shows the state the user asked for.

## Isolated desktops

For an app that needs its own X server or clipboard, set `DESKPILOT_AGENT=NAME` and use
`deskpilot --session launch CMD...`. Put `--session` before every later command for that desktop, including
`shot`, `key`, `elements` and `stop`. Its compositor, clipboard, accessibility bus and application profiles are
separate from the user's desktop. `--hidden` may focus a window inside it without changing the user's view.
Use a stable agent name to reuse its persistent profile. Stop it with `deskpilot --session stop` when done.
For browsers, prefer fastbrowse. A new isolated profile does not contain the user's signed-in browser sessions.

For native GTK 4 apps, `key` and `type` support `--agent-keyboard` (`agent_keyboard` over MCP). This uses the
independent seat's keyboard and the active keymap, including while the user's keyboard focus is in the same app.
It requires a named window and a client that bound the keyboard. Confirm the result: Qt can bind it and discard
the events. Use the regular route for Qt, Chromium, XWayland and paste.

## Taking the user's pointer or focus

By default the user's pointer and keyboard focus stay where they are, so the user can keep working. Two flags take
them instead:

- `--take-pointer` on `click`, `move` or `scroll` moves the user's own pointer there, through a virtual mouse that
  needs write access to `/dev/uinput`. Use it only when a plain click or `hover` did not work.
- `--take-focus` on `key` or `type` focuses the window and leaves it focused. Use it only for an app that ignores
  keys while unfocused, after a plain `--window` attempt did nothing.
- `--take-focus` on `click` does the same before clicking. Use it when a menu or dropdown you clicked open is gone
  in the next `shot`: its app closes popups while its window is not the focused one.

Both interrupt whatever the user is doing, so tell the user before using them. `key` and `type` without `--window`
press into the focused window, which may be the user's own: always pass `--window`.

## Errors

Each error starts with a code: `deskpilot: CODE: message`, or in MCP the result's structured `code`.

- "is not on screen": the window is on a hidden workspace or scrolled off. Ask the user before passing `--show`,
  or `deskpilot launch` your own copy of the app and act on it with `--hidden`.
- `not_found` with `--hidden`: hidden acts only on windows you launched or explicitly adopted.
- "take a new shot": the window changed size since the shot. Shoot again and pick the point anew.
- A plugin build failure names the missing headers or compiler.
- A stale `HYPRLAND_INSTANCE_SIGNATURE` is handled: deskpilot finds the live Hyprland, and stops with a list when
  several are live. Ask the user which one to use.
- `covered`: another window or layer is over the point, or for an element another surface of the same window, such
  as a submenu over its menu, hidden or not. `gone`: the window closed. Take a new `shot`, since the window may have
  moved, resized or been covered.
- `owns_clipboard`: the paste's target app holds the user's clipboard and would paste that instead. Use `type` if the
  text has no newlines or the box takes Return safely (type sends each newline as Return), or ask the user to copy
  something in another app. Never set the clipboard to get around it.
- `stale`: the element id is from a replaced snapshot, the element's role or name changed, or the popup it is on
  closed. Run `elements`
  again and pick the id anew; pass ids as `SNAPSHOT:e5` so an old one cannot match a renumbered tree. To look
  again without invalidating the ids you hold, use `elements --peek`.
- `offscreen`: the element is not showing, as in a collapsed menu or a scrolled-away row.
- `no_tree`: the app exposes no accessibility tree. Use a shot and points instead.

For a popup-grab regression in this checkout, run `scripts/popup_calibration.py --output /path/to/results.json`
with an explicit parent `HYPRLAND_INSTANCE_SIGNATURE`. It uses and cleans up its own private desktop. The optional
`--plugin /path/to/build.so` tests a baseline build there without loading it into the user's compositor.
