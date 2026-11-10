# deskpilot

Desktop control for agents on Hyprland: screenshot a window, click or type at a point in it, and leave the user's
own pointer and keyboard focus where they were. The CLI and the MCP server are thin layers over the same `Desk`.

## Checks

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run pytest
python3 scripts/changelog.py --check
claude plugin validate --strict .   # if the Claude Code CLI is installed
```

CI also runs actionlint, zizmor and gitleaks. The plugin is not built in CI: it needs the exact Hyprland it loads
into, so check a change to `plugin/deskpilot.cpp` with `deskpilot plugin load` on a real session.

## Layout

- `src/deskpilot/cli.py`: argument parsing and output; every command calls `Desk`
- `src/deskpilot/mcp_server.py`: the same actions as MCP tools over stdio, one `Desk` for the session
- `src/deskpilot/desk.py`: the actions an agent takes, shared by the CLI and the MCP server
- `src/deskpilot/hypr.py`: hyprctl: windows, monitors, focus, the cursor, and picking the live instance
- `src/deskpilot/plugin.py`: builds, loads and talks to the compositor plugin
- `src/deskpilot/plugin/deskpilot.cpp`: the Hyprland plugin that runs each click, scroll and keystroke inside the
  compositor and puts the user's pointer and focus back
- `src/deskpilot/screen.py`: grim toplevel screenshots, shot ids, and the mapping from shot pixels back to the screen
- `src/deskpilot/overlay.py`: the agent cursor, drawn by `cua-driver`
- `src/deskpilot/uinput.py`: the virtual mouse behind `--take-pointer`
- `src/deskpilot/keys.py`: key names, chords and text as the plugin's key events
- `src/deskpilot/clipboard.py`: the user's clipboard through wl-clipboard
- `src/deskpilot/a11y.py`: the target app's AT-SPI accessibility tree over D-Bus, and element ids for actions

## Rules

- The user's pointer and focus are theirs. Only `--take-pointer`, `--take-focus` and `--show` may move them; every
  other action goes through the plugin, which must restore both and replay input the user made meanwhile. A window
  that is not on screen is an error, never a reason to focus it.
- A point is resolved against a recorded shot. A shot is of one window at one size; any other use is refused.
- A plugin build loads only into the Hyprland it was built against. Builds are named after the Hyprland commit and
  the plugin source hash, so a changed source or an upgrade rebuilds rather than reusing a stale build.
- Each dispatcher tries Hyprland 0.57's Lua form first and falls back to the string form. Keep both working.
- A window name that matches several windows is an error listing them, and a point outside the window or under
  another window is refused. Never guess a target.
- The agent cursor is feedback, never input: any overlay failure turns it off and the action goes ahead.
- Text is typed as written on the user's active keymap, resolved in the plugin, never through a fixed layout table.
  A character no key on that keymap gives is an error, never skipped.
- A paste offers its text to the target window's client alone and never sets the seat's selection, so the user's
  clipboard is untouched. A client that can only read the shared clipboard (XWayland) is refused.
- A pointer action (click, scroll, drag, hover) holds the user's pointer input while it runs, so it is bounded in
  steps and time, and it lets go of every button it pressed however it ends.
- An element is targeted where the app reports it when the action runs, never from a stale snapshot. An app that
  exposes no accessibility tree is an error, never a guessed tree.
- Tests never touch the real compositor, `/dev/uinput`, `grim` or `cua-driver`. Pass fakes into `Desk` and
  `Hyprland`, as `tests/test_core.py` does.
- Every user-visible change gets a line under `## [Unreleased]` in `CHANGELOG.md`, and a command or flag change
  updates `README.md` and `skills/deskpilot/SKILL.md` too.
