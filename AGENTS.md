# deskpilot

Desktop control for agents on Linux (Hyprland), Windows and macOS: screenshot a window, click or type at a point
or element in it, and leave the user's own pointer and keyboard focus where they were. The CLI and the MCP server
are thin layers over the desk `open_desk` picks: `Desk` on Hyprland, `NativeDesk` with a platform driver elsewhere.

## Checks

- `scripts/gate`: ruff, ty, pytest and the changelog check. `scripts/gate --changed` lints only files changed against
  `origin/main`. Run `claude plugin validate --strict .` too when the Claude Code CLI is installed.
- CI also runs actionlint, zizmor and gitleaks. The plugin is not built in CI: it needs the exact Hyprland it loads
  into, so check a change to `plugin/deskpilot.cpp` with `deskpilot plugin load` on a real session. To check one
  without touching the user's screen, `python3 scripts/nested.py start` runs a throwaway Hyprland inside theirs, its
  window on the hidden `special:dptest` workspace, with a headless output to act on and capture
  (`HYPRLAND_INSTANCE_SIGNATURE=<signature>` for deskpilot, `WAYLAND_DISPLAY=<wayland> grim -o DPTEST` to see it);
  `python3 scripts/nested.py stop <signature>` ends it.

## Layout

- `src/deskpilot/cli.py`: argument parsing and output; every command calls the desk from `open_desk`
- `src/deskpilot/mcp_server.py`: the same actions as MCP tools over stdio, one desk for the session
- `src/deskpilot/backend.py`: picks the desk for the platform; `hyprland_only` refuses Hyprland-only features
- `src/deskpilot/base.py`: what every desk shares: batches and waits
- `src/deskpilot/errors.py`: the expected failures and `error_code`
- `src/deskpilot/desk.py`: the Hyprland desk
- `src/deskpilot/native.py`: the Windows and macOS desk, over the `Driver` protocol, with element snapshots
- `src/deskpilot/win32.py`: the Windows driver: window messages, PrintWindow, UI Automation
- `src/deskpilot/macos.py`: the macOS driver: CoreGraphics events, screencapture, AX
- `scripts/smoke_native.py`: every action through the CLI against a window that records the input it gets
  (`scripts/smoke_app.py` in Tk; `scripts/smoke_app_macos.py` in Cocoa, since Aqua Tk ignores input posted to it), and
  the platform editor, on the Windows and macOS CI runners
- `src/deskpilot/hypr.py`: hyprctl: windows, monitors, focus, the cursor, and picking the live instance
- `src/deskpilot/plugin.py`: builds, loads and talks to the compositor plugin
- `src/deskpilot/plugin/deskpilot.cpp`: the Hyprland plugin that runs each pointer path and keystroke inside the
  compositor, puts the user's pointer and focus back, drives a hidden window's own pointer, and draws the agent
  cursor
- `src/deskpilot/screen.py`: grim toplevel screenshots, shot ids, and the mapping from shot pixels back to the screen
- `src/deskpilot/uinput.py`: the virtual mouse behind `--take-pointer`
- `src/deskpilot/keys.py`: key names, chords and text as the plugin's key events
- `src/deskpilot/clipboard.py`: the user's clipboard through wl-clipboard
- `scripts/nested.py`: a throwaway nested Hyprland for live checks of the plugin off the user's screen
- `scripts/wordmark.py`: the wordmark, its dark variant and the GitHub social preview, from `assets/logo.svg` and
  Liberation Sans (`uv run python scripts/wordmark.py`)
- `src/deskpilot/a11y.py`: the target app's AT-SPI accessibility tree over D-Bus, and element ids for actions
- `src/deskpilot/recipe.py`: saved recipes (steps, `pause`, `wait`, `shot`), the window pinned once, filmed
  through `Desk.record`; `recipes/` holds the examples

## Rules

- The user's pointer and focus are theirs. Only `--take-pointer`, `--take-focus` and `--show` may move them; every
  other action goes through the plugin, which sends input to the target app's own pointer and keyboard and never
  holds the user's input, except where the target can only be reached through the seat (an XWayland window, or the
  app the user's pointer or keyboard focus is in): there it must restore both and replay input the user made
  meanwhile. A window that is not on screen is an error unless the action is hidden, never a reason to focus it. A hidden
  action reaches only the windows its agent launched or explicitly adopted by exact address, on `special:agent-NAME`.
  Adoption claims one stable window identity, never its shared process; it refuses a client receiving user input,
  XWayland, grouped or pinned windows, and another agent's windows. Release, stop and plugin unload return adopted windows without closing
  them. A window the user moved out or grouped is left where they put it.
- A point is resolved against a recorded shot. A shot is of one window at one size; any other use is refused.
- A plugin build loads only into the Hyprland it was built against. Builds are named after the Hyprland commit and
  the plugin source hash, so a changed source or an upgrade rebuilds rather than reusing a stale build.
- Each dispatcher tries Hyprland 0.57's Lua form first and falls back to the string form. Keep both working.
- A window name that matches several windows is an error listing them, and a point outside the window or under
  another window is refused. Never guess a target.
- The agent cursor is feedback, never input: it never changes what an action does. Only a window capture that asks for a
  cursor (`grim -c`, which `record` uses) carries it, drawn by the plugin so a recording needs no cursor of its own;
  `shot` never asks, so what a model reads never includes it, and no capture includes the user's own pointer. A
  hidden cursor is drawn into captures of its window only, never on an output.
- A hidden action never moves the user's pointer, focus or view. Where it cannot reach a window without doing so
  (XWayland, the app under the user's pointer, a drag and drop), it is refused, never quietly run on screen.
- Text is typed as written on the user's active keymap, resolved in the plugin, never through a fixed layout table.
  A character no key on that keymap gives is an error, never skipped.
- A paste offers its text to the target window's client alone and never sets the seat's selection, so the user's
  clipboard is untouched. A client that can only read the shared clipboard (XWayland) is refused.
- On Windows and macOS, input goes to the target window or process without moving the user's pointer or focus
  (posted window messages, CoreGraphics events to the pid). Seat input (`--take-pointer`, `--take-focus`) first
  checks the target really is in front and refuses otherwise. An element click uses the app's own press action
  when it has one. Hyprland-only features raise `Unavailable` there, never an approximation.
- A pointer action (click, scroll, drag, hover) is bounded in steps and time, and it lets go of every button it
  pressed however it ends.
- An element is targeted where the app reports it when the action runs, never from a stale snapshot: every node on
  its path keeps its role and name, or it is `stale`. An app that
  exposes no accessibility tree is an error, never a guessed tree.
- An expected failure carries a code (`error_code` in `errors.py`), set where the failure is produced, as the plugin
  does for `covered` and `gone`. Never derive a code from message text.
- Tests never touch the real compositor, `/dev/uinput`, or `grim`. Pass fakes into `Desk` and
  `Hyprland`, as `tests/test_core.py` does, and a fake `Driver` into `NativeDesk`, as `tests/test_native.py` does.
  The Windows and macOS CI jobs run that file and the smoke script; the Linux gate cannot import their drivers.
- Every user-visible change gets a line under `## [Unreleased]` in `CHANGELOG.md`, and a command or flag change
  updates `README.md` and `skills/deskpilot/SKILL.md` too.

## Standards

- `repo-standards`: the RS-* requirements every cjber and agent-labs-dev repo meets; its `audit.py` checks this repo.
