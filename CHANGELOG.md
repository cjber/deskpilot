# Changelog

What changed in each release, in the terms someone using deskpilot would notice. Dates are UTC.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each version's entry is also its GitHub release notes.

## [Unreleased]

- **`--hidden` now drives apps the agent launched, never the user's own windows.** `deskpilot launch CMD...` (with
  `DESKPILOT_AGENT` set) starts an app on the agent's hidden workspace, and every window it opens, then or later,
  stays there without taking focus. `deskpilot show` lets the user see that workspace, and `deskpilot stop` closes
  the agent's apps, which also close when the owner process (`DESKPILOT_OWNER_PID`, or the MCP server) exits, or
  after 30 minutes unused. The MCP server gains `launch` and `stop`. A hidden action on a window the agent did not
  launch is now refused as `not_found`.
- **GTK and Qt apps get a pointer of deskpilot's own.** The plugin adds a second seat, `deskpilot`, with a pointer
  only. An app that binds it takes clicks, scrolls, drags and hovers there, so a hidden action works on a window of
  the app the user's pointer is on, and nothing of the user's is held. Apps that bind only the first seat (Chromium,
  Electron, kitty), XWayland apps, apps started before the plugin first loaded, and keys keep the routes they had.
- **A menu deskpilot opens no longer grabs the user's pointer and keyboard**, in any app or mode, and a click
  outside it, the user's or deskpilot's, closes it as the user's own menus close.
- **A click into a covered window is refused, not reported as done.** An on-screen click, scroll, drag or hover
  on a native Wayland window at a point another window covered went to the window beneath and reported success,
  though the user saw nothing happen there. It is now refused as `covered`, as it already was for XWayland windows.
- **Clicks reach a window's own menus when it is under your pointer.** A click into a dropdown or menu the target
  window had opened was refused as `covered` whenever the action had to go through your pointer.
- **A missing accessibility registry is reported as `unavailable`.** When no AT-SPI registry was running, `elements`
  failed with `stale`, which tells a program to take a new snapshot; it now fails with `unavailable`.
- **A new agent cursor, one per agent.** The sharp arrow is now a flight-path marker, a ring with wings centred on
  the point, with a soft shadow and lock-on brackets that close in as it lands. Set `DESKPILOT_AGENT` to a name and
  that agent gets a cursor of its own, with the name on a tag and a colour of its own, so any number of agents on one
  desktop can be told apart.
- **The first action after a Hyprland restart no longer fails with "Cannot load a plugin twice!"** when several
  deskpilot calls start at once. One call loads the plugin while the others wait and then use it, and a call that
  cannot reach Hyprland says so instead of trying to load the plugin again.
- **`type` keeps capitals in Wine and other X apps that read Shift from key presses.** A typed character that needs
  Shift or AltGr now presses that modifier's own key around it, as a physical keyboard does, instead of only setting
  the modifier, which Wine ignored.

## [0.5.2] - 2026-11-11

- **A click no longer lets the app take the user's focus afterwards.** An app such as Chrome asks to be activated
  after each click, using the click's input serial, and its request arrived after the click had ended. Input
  deskpilot sends now carries serials Hyprland does not record, so it earns no activation or pointer warp.
- **`plugin load` refuses after a Hyprland upgrade instead of unloading the working plugin.** Until Hyprland
  restarts, the installed headers are the new version's and no build can load, so the loaded build is left in place.

## [0.5.1] - 2026-11-10

- **A combo box's chosen item reads as selected.** GTK reports a combo box's active item only through the
  combo box's AT-SPI Selection, so `elements` showed no choice at all and a program had to take a screenshot to
  read it. An item any container reports as chosen now carries the `selected` state.

## [0.5.0] - 2026-11-10

- **Read a tree without replacing the snapshot.** `elements --peek` (MCP `peek`) reads the accessibility tree and
  leaves the latest snapshot in place, so the ids taken from it stay good while a program checks what changed.

## [0.4.0] - 2026-11-10

- **Typing no longer flickers a window's title bar.** Keys went to a window with a keyboard enter and leave around
  each keystroke, so an app that draws its own title bar, such as Chrome, redrew it focused and unfocused for every
  character. The window is now entered once for the whole sequence.
- **Typing is much faster.** `type` (MCP `type_text`) no longer holds each key 30 ms and waits 30 ms between keys
  by default: 9.4 KB reaches Chrome in about 12 seconds rather than 9 minutes. Pass `--delay 0.1` for games, as before.
- **Paste works in Chrome.** Chrome reads a pasted text as soon as it is offered, and the window's clipboard was
  taken back after that first read, so Chrome pasted nothing. The text now stays the window's clipboard for 10
  seconds, or until the next paste.
- **Act on windows out of view with `--hidden`.** `shot`, `click`, `scroll`, `drag`, `hover`, `elements` and
  `wait-for --changed` take `--hidden` (MCP `hidden`) to act on a window wherever it is, a hidden or special workspace
  included, without switching workspace, showing a cursor, or touching your pointer, focus or screen. Tooltips and
  menus the window opens show in its screenshots and take clicks, and your own pointer moving meanwhile does not close
  them. Native Wayland
  windows only: XWayland windows, and windows of the app your pointer is on, are refused.
- **Deskpilot no longer blocks you.** Clicks, scrolls, drags, hovers and keys go straight to the target app, so your
  pointer, cursor, focus and input keep working while an agent acts. XWayland apps and the app your pointer or
  keyboard is in still take a brief hold, as before.
- **The agent cursor is drawn by the plugin.** It no longer needs `cua-driver`. It glides along an arc to each point
  before the action starts, shrinks with a ring at each press, follows drags as they move, and fades when idle.
- **Scroll is bounded.** `scroll` takes 1 to 50 notches either way.
- **Drag, hover and hold keys without taking your pointer.** `deskpilot drag` presses, moves and releases for
  sliders, text selection and drag and drop within a window. `deskpilot hover` rests the pointer on a point and
  returns a screenshot taken meanwhile, so tooltips show, and `--click` clicks next for a menu that opens on hover.
  `key --hold` keeps a key down. Each holds your pointer input only while it runs and always lets go of its button
  (MCP `drag`, `hover`, and `hold` on `key`).
- **Target elements of the accessibility tree.** `deskpilot elements` prints an app's AT-SPI tree with roles, names,
  states, text and bounds, and `--element ID` on `click`, `move`, `scroll` and `hover` acts where the app says the
  element is now (MCP `elements` and `element`). An app that exposes no tree says so.
- **Element ids name their snapshot.** Each `elements` call has a snapshot id, and `SNAPSHOT:e5` refers to that
  snapshot's element: once a newer snapshot replaces it, the id is refused as stale rather than read against the
  new numbering. An element whose role or name changed since the snapshot is stale too, and one no longer showing
  is offscreen.
- **Errors carry a code.** Every expected failure starts with one stable word a program can branch on: not_found,
  ambiguous, not_visible, offscreen, stale, covered, gone, refused, busy, no_tree, unavailable, timeout or invalid.
  The CLI prints `deskpilot: CODE: message`; MCP returns an error result whose text is `CODE: message` and whose
  structured content is `{"code", "message"}`. A click refused because another window is over the point is
  `covered`.
- **Clicks move the pointer before pressing.** Each click and scroll now sends a motion event at its point before
  the press, as a real pointer does, for clients that track the pointer from motion.

## [0.3.0] - 2026-11-10

- **Text arrives as written on any keyboard layout.** `type` and `key` used US key positions, so another layout or
  Caps Lock typed other characters. The plugin now finds the key for each character on the active keymap and sets
  the modifiers it needs, keeping Caps Lock and the layout. A character the keymap cannot type is refused with its
  code point. `key A` types a capital A, and a chord's letter (`ctrl+a`) is the key that gives it on the layout.
- **Keys always go through the plugin.** `--take-focus` focuses the window and then sends keys as `--window` does,
  and `key` and `type` without `--window` go to the focused window, so neither needs `/dev/uinput` any more. These
  keys do not trigger Hyprland keybinds.
- **Paste any text into a window without touching your clipboard.** `deskpilot paste TEXT --window W` (MCP
  `paste_text`) gives the window a clipboard only it sees and presses its paste chord (`--keys`, `ctrl+v` by default),
  so text the keymap cannot type and long text arrive whole. XWayland windows share one clipboard and are refused.
- **Read and write the clipboard.** `deskpilot clipboard` prints the clipboard's text and `--set TEXT` replaces it (MCP
  `clipboard` and `copy_text`).
- **Wait for what is on screen.** `wait-for` takes `--focused`, `--title TEXT` and `--size WxH`, and `--changed SHOT`
  (with an optional `--region X Y W H`) waits until the window looks different from that shot, then prints a shot of
  the change and the box that changed (MCP `wait_for` and `wait_changed`).
- **Dropdowns stay open with `click --take-focus`.** Chrome and other apps close a menu or dropdown when their
  window is not the focused one, so a click that left the user's focus in place closed the list it opened.
  `click --take-focus` (MCP `take_focus`) focuses the window first and leaves it focused.
- **Your mouse moves as usual during an agent's click.** Motion made while a click or scroll ran was added up and
  applied as one jump, skipping pointer acceleration and the relative motion a pointer-locked game reads, and a
  tablet or other absolute device could move the pointer mid-click. All of it is now held and replayed in order
  through Hyprland's own handling.

## [0.2.1] - 2026-11-10

- **Reloading the plugin no longer crashes Hyprland.** Loading a plugin build that had already been loaded and
  unloaded in the same Hyprland session, then unloading it again, aborted Hyprland (a double free in the plugin's exit).
  The plugin is now built so that unloading really unloads it, and its exit leaves nothing behind.
- **A stale plugin is named, not blamed on your arguments.** When a loaded plugin build of other source refuses
  a request, the error names the loaded build and the fix (`deskpilot plugin load`) instead of an argument error
  such as `hold and gap must be 0 to 10000 milliseconds`.

## [0.2.0] - 2026-11-10

- **Install from PyPI.** Each release is published to PyPI, so `uv tool install deskpilot` installs it.
- **Windows off screen are refused, not focused.** `shot`, `click`, `move` and `scroll` on a window on a hidden
  workspace or scrolled off its monitor used to focus it, switching your workspace. They now refuse it, and
  `--show` brings it into view on request. `windows` reports `visible` for each window.
- **Screenshots of the window itself.** `shot` captures the window by its toplevel, so a window on top no longer
  shows in the image, and reads its scale from the image, so scaled outputs map clicks correctly.
- **Shot ids.** Each `shot` has an id; `--shot ID` (or `shot` over MCP) ties a click to that image, and a window
  resized since is refused instead of clicked in the wrong place. The MCP `shot` tool returns the id with the image.
- **MCP errors say why.** A refused or failed action over MCP returned only `Error executing tool <name>`; it now
  returns the reason, such as the window covering the point or the names an ambiguous window matched.
- **The plugin loads itself.** The first action after Hyprland restarts or upgrades builds and loads the plugin.
- **Clicks keep your keyboard focus.** A click or scroll no longer focuses or raises the target window while it runs,
  so keys you type meanwhile stay in your own window. An XWayland app that asks to be activated when clicked is not
  given focus while deskpilot acts on it.
- **Agents can act at the same time.** Clicks, scrolls and keys from several callers queue and run in turn instead of
  failing with `busy`. Up to 32 wait at once; past that an action is refused with a clear error.
- **Typed text arrives whole.** `deskpilot type` and the MCP `type_text` send the text as one action, so another
  agent's keys never land in the middle of it, and long text works. `--delay` now sets how long each key is held as
  well as the gap before the next.
- **Typing leaves your window alone.** Keys go only to the target app, so the window you are using no longer sees
  its keyboard focus leave and return, and its pointer no longer flickers while an agent types.
- **Scrolling ignores your compositor settings.** A scroll reaches the app as plain wheel notches, without your
  Hyprland mouse binds or scroll factors acting on it.
- **Clicks check what is on top before every press.** Each press and wheel notch asks Hyprland what is at the point,
  so a window, bar or popup that appears mid-click is refused instead of clicked.

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
