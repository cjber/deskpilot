# Changelog

What changed in each release, in the terms someone using deskpilot would notice. Dates are UTC.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each version's entry is also its GitHub release notes.

## [Unreleased]

- Windows and macOS support: `windows`, `shot`, `elements`, `click`, `move`, `scroll`, `drag`, `hover`, `key`, `type`, `clipboard`, `launch` and the waits work there too, over UI Automation and window messages on Windows and AX and CoreGraphics events on macOS. An element click uses the app's own press action when it has one. Hidden windows, adoption, `paste`, `record`, `cursor_press`, `recipe run` and `--session` stay Hyprland only and report `unavailable` elsewhere.
- deskpilot now lives at [agent-labs-dev/deskpilot](https://github.com/agent-labs-dev/deskpilot). Install the Claude Code plugin with `/plugin install deskpilot --marketplace agent-labs-dev/deskpilot`.

## [0.8.0] - 2026-11-19

- `deskpilot record WINDOW` (MCP `record_start` and `record_stop`) films a window, hidden ones included, with the real agent cursor the plugin draws: brackets closing in as it lands and the marker squeezing on a press. It writes an mp4, or with `-o -` PPM frames at a constant rate on stdout for another encoder. Only a capture that asks for a cursor carries it, so `shot` stays cursor-free, and the user's own pointer is never in the frames. While a window is being filmed, a hidden action on it shows its cursor in the recording alone, never on screen.
- MCP `cursor_press` squeezes and rings the agent cursor where it is, and lets go, for show only: it sends no input, so an app driven another way, such as a browser over CDP, can mark its own presses and a `record` clip shows them.
- `launch`, `clipboard` and a private XWayland paste now start their app, wl-paste or wl-copy on the Hyprland instance deskpilot drives, the one `HYPRLAND_INSTANCE_SIGNATURE` selects, whatever `WAYLAND_DISPLAY` says. An X11 app gets that instance's XWayland as `DISPLAY`, and no `DISPLAY` when the instance has no XWayland, never the caller's. Before, an app could open on another compositor, such as the user's own session while deskpilot drove a nested one, and `launch` timed out.
- `key "ctrl++"` presses Ctrl and the `+` key, with the modifiers that key needs on the user's keymap (Ctrl+Shift+= on a US layout). Before, it pressed Ctrl alone and reported success. A chord with an empty key, such as `ctrl+`, is an error. A chord's character that no key gives unmodified is pressed with the modifiers it needs instead of refused.
- `launch` turns off GTK's desktop portal for the app it starts, so a GTK app's file dialog is its own window, placed on the agent's workspace and instance with it, instead of a window of the user's portal on the user's screen. The app keeps the user's session bus and accessibility bus.
- `deskpilot --session stop` asks each app of the private desktop to close before ending it. The close it sent on Hyprland 0.57 named the window in a form Hyprland accepted and ignored.
- `launch` of a single-instance app that hands its window to a copy already running says so when it times out, instead of only that no window opened.
- `release` of a window the agent has not adopted fails with `not_found`, not `gone`, which means the window closed.
- `elements` reports an element whose extents the app gives a negative size, such as a GTK 4 tab panel for a page that is not realized, with zero bounds instead of a nonsense rectangle, and clicking it is refused `offscreen`.
- With `--json`, an error is printed as `{"code": ..., "message": ...}` on stderr instead of the plain `deskpilot: CODE: message` line.
- `paste ""` is refused, as `type ""` is.
- An MCP tool refuses an argument it does not take instead of ignoring it, so `wait_for` with `hidden` is an error, as `wait-for --hidden` without `--changed` is in the CLI. Each tool's schema says so with `additionalProperties: false`.
- Coordinates refused against a shot of an earlier window at the same address now say so, and `wait-for --changed` refuses such a shot instead of comparing against it.
- `--session` runs in a container with no GPU and no compositor around it: with `DESKPILOT_STANDALONE=1` its Hyprland drives the host's vkms virtual display and renders in software. `container/Dockerfile` builds an image with a pinned Hyprland and a prebuilt plugin, run with `--device /dev/dri` after `modprobe vkms` on the host.
- `deskpilot --session env` starts the agent's private desktop if needed and prints what another tool needs to reach it (its Wayland display, Hyprland instance, runtime directory and buses), as shell exports or JSON, so other tools can capture or record it.
- The session's seat now has a pointer as well as a keyboard, so clicks reach apps in a desktop that has no other input device.
- The free memory a session needs is checked against the container's memory limit as well as the machine's, so a small box refuses to start one instead of running out.
- `deskpilot plugin build` works with no Hyprland running, building against the installed headers, and also builds the `--session` keyboard.
- `deskpilot recipe run FILE` replays a saved recipe: one JSON file naming a window, 1 to 200 steps and optionally the app it launches, with `pause`, `wait` and `shot` steps between the batch actions. `--record MP4` films the window as `deskpilot record` does, so the video holds the agent cursor deskpilot draws and nothing else on screen; the recording is stopped and kept even when a step fails, which exits 2. An app that exits with an error before its window appears fails the run at once with its own exit status and last words, instead of after the whole timeout. `deskpilot recipe check` validates a file without touching the desktop, and `deskpilot recipe schema` prints the format as JSON Schema. `recipes/export-an-asset.json` records the README walkthrough. A recipe never uses `--show`, `--take-focus` or `--take-pointer`.

## [0.7.4] - 2026-11-16

- A window that Hyprland gives a closed window's address, such as a freshly launched app, is no longer refused as changed since the closed window's shot. Shots and accessibility snapshots are matched to the window by its compositor, process and stable id as well as its address, so a new window at a reused address simply has none yet, while a window that changed since its own shot is still refused, and so are coordinates given without a shot id or scale, which are in the closed window's shot.

## [0.7.3] - 2026-11-16

- A modifier the user holds on any of their keyboards while their keyboard focus is in another window of the target's app no longer reaches a click, scroll, drag or hover there, so a held Ctrl never turns the action's click on a link into a new tab. Until the action ends, their keys wait behind it, as for XWayland windows.
- After a click or keystroke, the target's app gets back exactly the modifiers Hyprland last sent it. A modifier an input method kept to itself no longer reaches that app after the action, including one kept before the plugin loaded, where it turned the input method's next key into a shortcut. When the plugin loads, the app with the user's keyboard focus is given the modifiers they hold there, so a Shift held from before the load still reaches their next key after a keystroke, while a Ctrl an input method's keyboard kept from an earlier text field is not given back.
- A click, scroll, drag or hover on an XWayland window now reaches it where Hyprland shows it over another X window that took focus later, such as a floating Wine window over a focused tiled one. Before, the action reported success while its press went to the other window. The action raises its window in the X stack first, as a click of the user's does, without moving focus, and an action whose point another X window still holds, or that its window has left by the time the raise is done, is refused as covered.

## [0.7.2] - 2026-11-16

- A click, scroll, drag or hover on a native window of the app the user's pointer is in no longer holds the user's typing until it ends. Their keys wait only behind their own pointer input, after they change a modifier while that app has their keyboard focus (so the action's click never becomes a Ctrl-click), and for XWayland windows, whose modifiers the action replaces. Releasing a key that reached their window, before or during the action, goes through at once so it never repeats, unless input for the same key from another keyboard is waiting, which it then follows in order.

## [0.7.1] - 2026-11-16

- Window captures report a locked desktop before waiting for a frame when Hyprland exposes its lock state. Unlock the desktop to capture again; plugin availability is independent.

## [0.7.0] - 2026-11-12

- `adopt ADDRESS` hands one unfocused native window to an agent for hidden actions; `release ADDRESS`, stop and plugin unload return it without closing its shared application.

- Identify popup grabs by their input serial so concurrent user input cannot give an agent-opened popup the user’s pointer or keyboard.

- Keep held user modifiers out of XWayland pointer actions and preserve keyboard state when replaying input received during an action.

- Unfocused native keyboard sequences wait for serial-matched client replies before advancing, so Chrome shortcuts are not discarded as a single event batch.
- GTK 3 menu items use the reported popup layer to distinguish menus at the window origin.

- Hidden launches also recognize registered processes that erase their launch environment.

- Private launches resolve relative commands and paths from the caller's working directory.

- `key` and `type` accept `--agent-keyboard` for native applications that support an independent keyboard seat.
- Typing parentheses in Wine uses number-row chords instead of extended duplicate keys Wine cannot translate.

- `--session` runs an agent's applications in a private desktop with a separate clipboard, accessibility bus and persistent profile, including XWayland applications.
- Paste reaches every clipboard device of a multi-seat client, including GTK after an agent pointer click.
- Hidden selection shortcuts preserve the user's clipboard and primary selection.

- Nested test shutdown removes its owned runtime sockets and logs after verifying process exit, and refuses recycled process ids.

- Captures use the same compositor as the target window, even when the inherited Wayland display differs.
- Recorded shots detect reused window addresses before keys, typing, paste and pointer actions; take a new shot to select the replacement window.

- Run up to 32 known actions against one pinned window with CLI and MCP `batch`, with partial results on failure and an optional final screenshot.

- Show Deskpilot operating a local demo app with its real cursor and click feedback, a shorter README and linked command and input-routing guides.

- **A logo, a wordmark, and the README opens with them.** The mark is a flight-path ring with wings locked on a
  cursor, the same shapes the agent cursor draws on screen; the wordmark sets it beside "deskpilot" and follows the
  reader's dark mode. A 1280×640 social preview is in `assets/` for the repository card, and the README also
  carries CI, version and licence badges.

## [0.6.0] - 2026-11-11

- **`--hidden` now drives apps the agent launched, never the user's own windows.** `deskpilot launch CMD...` (with
  `DESKPILOT_AGENT` set) starts an app on the agent's hidden workspace, and every window it opens, then or later,
  stays there without taking focus. `deskpilot show` lets the user see that workspace, and `deskpilot stop` closes
  the agent's apps, which also close when the owner process (`DESKPILOT_OWNER_PID`, or the MCP server) exits, or
  after 30 minutes unused. The MCP server gains `launch` and `stop`. A hidden action on a window the agent did not
  launch is now refused as `not_found`.
- **The MCP server reports its version** to clients on initialize, rather than an empty string.
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
- **`click --element` on an item of a GTK 3 popup menu clicks the item,** such as a country in an open combo box or
  an entry of a submenu, instead of the window behind it at the item's popup-relative position. `elements` reports
  such items in window pixels, so they line up with the shot, and gives zero bounds to one whose popup cannot be told
  apart. An element action is refused `stale` or `ambiguous` when its popup cannot be identified exactly, and
  `covered` when another surface takes the point. `move`, `scroll` and `hover` with `--element` do the same.
- **`paste` into an app that holds your clipboard is refused with `owns_clipboard`** instead of pasting your
  clipboard. Apps such as Chrome paste their own copy while they own the clipboard, so a paste into Chrome after you
  copied in Chrome inserted your text, not the given one. Type the text instead, or copy something in another app.
- **`key`, `type` and `paste` take `--hidden` and `--show`, as `hidden` and `show` over MCP.** `--hidden` sends the
  keys to a named window the agent launched, on its hidden workspace, without focusing it or holding the user's
  input; an XWayland window, or a window of the app the user's keyboard focus is in, is refused with `refused`, and
  so is the rest of the text when the user focuses that app while it is typed. `--show` needs a named window and no
  `--take-focus`.
- **Keys to a window that is not on screen are now refused with `not_visible`,** as clicks already were. Add
  `--show` to bring it into view first; scripts that sent keys to an off-screen window without a flag need it, or
  the app started with `deskpilot launch` and `--hidden`.
- **A hidden `shot`, `hover` or `wait-for --changed` of a window outside its monitor's area is refused with
  `not_visible`** instead of hanging, since Hyprland sends no frame of it; a column of the agent's workspace that a
  scrolling layout puts off screen is one. Any capture that gets no frame now fails after 5 seconds with code
  `timeout` rather than a traceback.
- **The first action after a Hyprland restart no longer fails with "Cannot load a plugin twice!"** when several
  deskpilot calls start at once. One call loads the plugin while the others wait and then use it, and a call that
  cannot reach Hyprland says so instead of trying to load the plugin again.
- **Unloading the plugin while it types no longer leaves a key held down in the window.** The keys the keystroke
  had pressed are let go first, so the app does not repeat the last one.
- **`type` keeps capitals in Wine and other X apps that read Shift from key presses.** A typed character that needs
  Shift or AltGr now presses that modifier's own key around it, as a physical keyboard does, instead of only setting
  the modifier, which Wine ignored.
- **Typing into a window keeps every key when you switch between other apps meanwhile.** Switching focus left the
  window's keyboard between keystrokes, and the rest of the text was dropped while the command still reported
  success; the next keystroke now enters the window again. A chord held with `--hold` also keeps its Ctrl or Shift
  when you switch windows during it.

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
