# Changelog

What changed in each release, in the terms someone using deskpilot would notice. Dates are UTC.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each version's entry is also its GitHub release notes.

## [Unreleased]

- **Clicks keep your keyboard focus.** A click or scroll no longer focuses or raises the target window while it runs,
  so keys you type meanwhile stay in your own window. An XWayland app that asks to be activated when clicked is not
  given focus while deskpilot acts on it.

## [0.1.0] - 2026-11-09

- **Clicks that leave your pointer alone.** `deskpilot click`, `scroll` and keys sent with `--window` run inside
  Hyprland through the deskpilot plugin: your pointer and keyboard focus go back where they were, and any mouse
  motion you made meanwhile is applied. Native Wayland, XWayland and Wine windows all receive a real click.
- **Screenshot coordinates.** `deskpilot shot` saves a window's screenshot and remembers its scale, so `click`,
  `move` and `scroll` take a point straight from the image. A point outside the window, or under another window, is
  refused.
- **No guessed windows.** A window is named by address, class, or part of its class or title, and a name that
  matches several windows is an error listing them.
- **Typing.** `deskpilot type` and `deskpilot key` type US-layout text and chords. `--delay` spaces keys for games,
  which read input once per frame.
- **Taking over when an app needs it.** `--take-pointer` and `--take-focus` move your own pointer or focus and press
  through a virtual input device, for a hover the app must see or an app that ignores keys while unfocused.
- **An agent cursor.** With `cua-driver` installed, an overlay arrow glides to each point deskpilot acts on.
- **Plugin management.** `deskpilot plugin load` builds the plugin for the running Hyprland and loads it; run it
  again after each Hyprland upgrade.
- **MCP server.** `deskpilot mcp` serves the same actions over stdio, keeping one input device and one agent cursor
  open for the session.
- **Works with Hyprland 0.57 and older.** Each dispatcher tries the Lua form first and falls back to the string
  form, and a shell with a stale `HYPRLAND_INSTANCE_SIGNATURE` reaches the one live Hyprland.
