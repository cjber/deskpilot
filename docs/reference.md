# Command guide

[Back to the README](../README.md)

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

An on-screen action is refused as `covered` at a point another window, bar or popup is over, since the user
would see it land on that instead.

A window on a hidden workspace or scrolled off its monitor is reported as `"visible": false`, and `shot`, `move`,
`click`, `scroll`, `drag`, `hover`, `elements`, `key`, `type` and `paste` refuse it rather than move the user's
view; `--show` focuses it to bring it on screen first.

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
that opens on hover. Both are bounded, and a drag or hover that fails, or whose window closes, still lets go of its
button.

`elements` prints the window's accessibility tree (AT-SPI): each element's id, role, name, states, text and bounds
in window pixels. `click`, `move`, `scroll` and `hover` take `--element ID` instead of a point, and act on the
element's centre where the app reports it at that moment. Each `elements` call prints a snapshot id, and
`SNAPSHOT:e5` pins an id to that snapshot: it is refused as stale once a newer snapshot replaces it, since every
snapshot numbers its elements afresh. An element whose role or name changed since is stale too, and one no longer
showing is offscreen. `elements --peek` reads the tree without replacing the latest snapshot, so a program can check
what changed before it acts on an id it already holds. An item of a GTK 3 popup menu (an open combo box, a menu or
submenu) is placed on the popup it is in, so its bounds line up with a shot showing the popup and a click lands on it;
when the popup cannot be told exactly the action is refused (`stale` once it closed, `ambiguous` when several could be
it) and `elements` reports the item with zero bounds. An element's click is also refused `covered` when another
surface, such as a submenu over its menu, takes the point. GTK and Qt apps expose a tree; Chromium, Electron and
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
`type`. An app that holds the user's clipboard, because they last copied in it, pastes its own copy instead, so a
paste into any of its windows is refused with `owns_clipboard`: `type` the text (each newline becomes Return, which
can submit a form), or ask the user to copy something in another app. `clipboard` reads the user's clipboard, and `clipboard --set` replaces it.

`type` sends keys as fast as the app reads them, about 1 KB a second into Chrome. Games read keys once per frame,
so type into them with `--delay 0.1` or more: each key is held that long, with the same gap before the next. For
long text, `paste` is faster still.

`wait-for` waits for a window to appear, or with `--gone` to close. `--focused`, `--title TEXT` and `--size WxH` also
wait for it to have keyboard focus, a title containing TEXT or that size. `--changed SHOT` waits until the window
looks different from one of its shots, in all of it or in `--region X Y W H` of that shot's pixels: any pixel
difference counts, and it prints a shot of the change with the box that changed.

Clicks, scrolls and keys from several agents at once run one after another in the order they arrive, so one `type`
is never interleaved with another's keys. Up to 32 wait behind the running one; past that an action is refused.

## MCP

`deskpilot mcp` serves the same actions over stdio: `windows`, `focus`, `shot` (returns the image and its shot
id), `click`, `move`, `scroll`, `hover` (returns the image), `drag`, `elements`, `key`, `type_text`, `paste_text`,
`clipboard`, `copy_text`, `wait_for`, `wait_changed`, `launch`, `adopt`, `release` and `stop`. `click`, `move` and `scroll` take `shot`, `show` and
`take_pointer`; `click`, `move`, `scroll` and `hover` take `element`; `shot`, `click`, `scroll`, `hover`, `drag`, `elements`,
`key`, `type_text`, `paste_text` and `wait_changed` take `hidden`, for windows from `launch` or `adopt`, and `key`, `type_text` and
`paste_text` take `show`; `click`, `key`, `type_text` and `paste_text` take `take_focus`; `key` takes `hold`. It
keeps one input device open for the session, and the apps it launched close when it exits.

Every expected failure has a code, one stable word to branch on: `not_found`, `ambiguous`, `not_visible`,
`offscreen`, `stale`, `covered` (another window is over the point), `gone` (the window closed), `owns_clipboard`
(a paste's target app holds the user's clipboard), `refused`, `busy`, `no_tree`, `unavailable`, `timeout` or
`invalid`. The CLI prints `deskpilot: CODE: message`. An MCP tool returns an error result whose text is `CODE: message` and whose structured content is `{"code": ..., "message": ...}`.

```sh
claude mcp add deskpilot -- deskpilot mcp
```

## Limits

- `type` can only send characters the active keymap types; a character it lacks (such as `é` on a US layout) is
  an error rather than skipped. `paste` takes any text, except into XWayland windows and apps that hold the user's clipboard.
- Hyprland plugins load only into the exact Hyprland they were built against, so `deskpilot plugin load` builds
  one per Hyprland commit and plugin source under `~/.local/share/deskpilot`, and replaces a loaded build of
  other source.
- Each CLI action that uses the input device opens a new one and waits about a second for libinput to add it. The MCP server
  avoids that wait.
- `drag` stays within one window: a drag and drop into another app is not supported.
- Hyprland only, Linux only.

## Batch

`deskpilot batch WINDOW [FILE]` reads a JSON array from `FILE`, or from stdin when omitted or `-`.
Use `--json` before `batch` for JSON output, `--hidden` for your launched or adopted window, and `--final-shot`
for a capture after all steps succeed. `--max-width` controls that capture's maximum width, 1400 by default.

Each of the 1 to 32 objects has an `action` and its fields:

| Action | Required fields | Optional fields |
|---|---|---|
| `click` | `x`, `y`, or `element` | `shot`, `button`, `double` |
| `move` | `x`, `y`, or `element` | `shot` |
| `scroll` | `x`, `y`, or `element`, plus `dy` | `shot` |
| `drag` | `x1`, `y1`, `x2`, `y2` | `shot`, `button`, `duration` |
| `key` | `combo` | `hold` |
| `type` | `text` | `delay` |
| `paste` | `text` | `keys` |

`button` is `left`, `right` or `middle`; `double` defaults to false. `dy` is 1 to 50 notches either way,
positive up. Drag `duration` defaults to 0.3 seconds, above zero and at most 5. Key `hold` and type `delay`
default to zero and are at most 10 seconds. Paste `keys` defaults to `ctrl+v`.

There is one window and hidden mode for the whole batch. Per-step window, focus, pointer and show overrides
are not accepted. `move` only displays the agent cursor and is unavailable in a hidden batch.
Unknown fields, malformed targets and invalid chords are rejected before any step runs. Target identity,
visibility, hidden ownership, element identity and recorded shot size are checked as steps execute.
These checks do not establish that a previous click left every other control in the same place: chain only
steps whose targets and expected effects are already known. Pause for a new observation when the next
choice depends on an earlier result.

Results have `ok`, `window`, `completed`, `results`, `failed` and `shot`. Successful step results carry
zero-based `index` and `action`, plus `x` and `y` for pointer actions. A failure carries `index`, `action`,
`code` and `message`; the CLI exits 2 and MCP marks the result as an error. Failure to resolve the initial
window has index null and action `window`; failure of the requested final capture has index equal to the
number of steps and action `shot`. Completed results are retained in either case.

A batch is sequential, with no rollback and no exclusive lock across its steps. It saves CLI process startups
and tool round trips. Each step still runs through the compositor's normal action path. The MCP `batch` tool
takes the same `window`, `actions`, `hidden`, `final_shot` and `max_width` fields and returns the optional final
image alongside its metadata.
