---
name: deskpilot
description: Click, scroll and type in desktop app windows on Hyprland with the deskpilot CLI or MCP server, while the user keeps their own pointer and keyboard focus. Works in native Wayland, XWayland and Wine apps such as the Battle.net launcher and games. Use for 'click the button in that app', 'type into the game', 'drive this desktop window', 'take a screenshot of a window', or any GUI task in a Hyprland session.
---

# deskpilot

Run `deskpilot --help` for the command list, and put `--json` before the command for JSON output. Over MCP the same
actions are tools: `windows`, `focus`, `shot`, `click`, `move`, `scroll`, `key`, `type_text` and `wait_for`.

If `deskpilot` is not on `PATH`, ask the user before installing it with
`uv tool install git+https://github.com/cjber/deskpilot`.

1. The first click, scroll or key loads the plugin for the running Hyprland, building it if needed, which can take
   a minute once per Hyprland version. `deskpilot plugin status` reports the loaded build.
2. Run `deskpilot windows` and pick the target by address, class, or part of its class or title. A name that matches
   several windows is an error that lists them: pick one by address rather than guessing. `visible` is false for a
   window on a hidden workspace or scrolled off screen; acting on it is refused unless you pass `--show`, which
   focuses it and moves the user's view, so ask the user first.
3. Run `deskpilot shot WINDOW`, read the image at the `path` it prints, and choose a point in that image. Note its
   `shot` id.
4. Act with that point as given, passing the id: `deskpilot click WINDOW X Y --shot ID` (`--button right`,
   `--double`), `deskpilot scroll WINDOW X Y DY --shot ID` (positive is up), or `deskpilot move WINDOW X Y --shot ID`
   to show the agent cursor there. Points are pixels in that shot, so never rescale them yourself; `--scale 1` means
   window pixels. A point outside the window, one another window covers, or a window resized since the shot is
   refused.
5. Type with `deskpilot type "TEXT" --window WINDOW` and press keys with `deskpilot key ctrl+a --window WINDOW`. Games
   read keys once per frame, so add `--delay 0.1` or more for them: each key is held that long, with the same gap
   before the next. Text is typed on a US layout, and a character it cannot type is an error. Several agents may act
   at once: actions run one after another in arrival order, and one `type` is never interleaved with another's keys.
6. Take a new `shot` after each action that changes the window, and confirm the result before the next step.
   `deskpilot wait-for WINDOW --timeout 120` waits for a window to open, and `--gone` for it to close.

Done when a fresh `shot` shows the state the user asked for.

## Taking the user's pointer or focus

By default the user's pointer and keyboard focus stay where they are, so the user can keep working. Two flags take
them instead, through a virtual input device that needs write access to `/dev/uinput`:

- `--take-pointer` on `click`, `move` or `scroll` moves the user's own pointer there. Use it only when the app must
  see a hover, such as a tooltip or a menu that opens on mouse-over.
- `--take-focus` on `key` or `type` focuses the window and leaves it focused. Use it only for an app that ignores
  keys while unfocused, after a plain `--window` attempt did nothing.

Both interrupt whatever the user is doing, so tell the user before using them. `key` and `type` without `--window`
also press into whatever has focus, which may be the user's own window: always pass `--window`.

## Errors

- "is not on screen": the window is on a hidden workspace or scrolled off. Ask the user before passing `--show`.
- "take a new shot": the window changed size since the shot. Shoot again and pick the point anew.
- A plugin build failure names the missing headers or compiler.
- A stale `HYPRLAND_INSTANCE_SIGNATURE` is handled: deskpilot finds the live Hyprland, and stops with a list when
  several are live. Ask the user which one to use.
- A refused click names why. Take a new `shot`, since the window may have moved, resized or been covered.
