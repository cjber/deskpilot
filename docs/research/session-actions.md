# Typing, paste, elements and XWayland inside a nested session

Research for #73, part of the map #71. Question: inside a nested Hyprland with the deskpilot plugin loaded, which of
`type`, `key`, `paste`, `elements` and XWayland work as they are, and which need the session backend to change.

Setup for every live check: `python3 scripts/nested.py start` (Hyprland 0.57.2, aquamarine 0.16.0, at-spi2-core
2.62.1), `deskpilot plugin load` against its signature, apps launched with `hl.dsp.exec_cmd` inside it, deskpilot run
with `HYPRLAND_INSTANCE_SIGNATURE=<nested>` and `WAYLAND_DISPLAY=<nested>`. Test apps: kitty, a GTK 3 window (an entry
and a button) run natively and with `GDK_BACKEND=x11`. Everything started was stopped and confirmed gone by pid.

## Results

| Action | Verdict | Evidence |
| --- | --- | --- |
| `type`, `key` | blocked as is; works once the session has a keyboard | See "No keyboard" below. With a full-keymap virtual keyboard added, `type "Zq! {x}"` and `key Return` into kitty arrived exactly (`Zq! {x}\n` in the file kitty's `cat` wrote); `key ctrl+u`, `key ctrl+a` worked; `type` into a GTK entry, native and XWayland, set its text (read back with `elements --peek`). |
| `paste` (Wayland client) | blocked as is (same keyboard gap); then works | With the virtual keyboard: `paste "Grüße, 日本 PASTED73" --keys ctrl+shift+v` into kitty and `paste " pasted日本"` into the GTK entry both arrived. The nested seat's own selection stayed empty (`wl-paste` on the nested display: "Nothing is copied"), and the user's clipboard never held any test string. |
| `paste` (XWayland client) | needs change | Refused by the plugin (`deskpilot.cpp:2395`, "would replace the user's clipboard"). In a session that is false: the X clipboard is the session's own Xwayland's. `wl-copy` on the nested display then `key ctrl+v` into the XWayland GTK entry pasted that text, and the user's clipboard was untouched. |
| `clipboard`, `clipboard --set` | needs change | `clipboard.py:24-48` shells out to `wl-paste`/`wl-copy`, which follow `WAYLAND_DISPLAY`. With the nested display set it read the session's clipboard (`xclip73`); with the user's it reads theirs. The backend must pass the session's display explicitly, never rely on the caller's environment. |
| `elements` (native GTK) | works as is, but on the user's bus | Full tree (frame, filler, text, button) with window-relative bounds; `click --element e4` pressed the button (entry text became `clicked`). |
| `elements` (XWayland GTK) | works as is | Same tree for the `GDK_BACKEND=x11` window. |
| XWayland apps, on-screen path | works as is | The nested Hyprland starts its own Xwayland (`Xwayland :3`, parent pid the nested Hyprland) and gives its children `DISPLAY=:3`, `WAYLAND_DISPLAY=wayland-3`. `click --element` and `type` on the XWayland window worked through the seat path. |
| XWayland apps, `--hidden` | needs change | `click --hidden` on it is refused (`deskpilot.cpp:1543-1544`): the hidden path still assumes the seat is the user's. |

## Findings

### No keyboard

The nested session has no keyboard device: `hyprctl -j devices` lists one mouse (`wl_pointer`) and `"keyboards": []`,
and the nested log shows only "New wayland pointer". Every key action fails with `refused: no keyboard`, raised in
`startKeys` (`deskpilot.cpp:2268-2270`) because `g_pSeatManager->m_keyboard` is null. Even past that check, a seat with
no keyboard capability gives clients no `wl_keyboard`, so `keyboardsOf` (`deskpilot.cpp:1453`, used at `2076-2083`)
would find none. Aquamarine's Wayland backend makes a keyboard only when the parent seat advertises one
(aquamarine v0.16.0 `src/backend/Wayland.cpp:354-362`); a headless backend makes none at all.

A keyboard added to the session fixes it. Hyprland treats a `zwp_virtual_keyboard_v1` like any keyboard:
`newVirtualKeyboard` calls `setupKeyboard`, which makes it the seat keyboard (Hyprland v0.57.2
`src/managers/input/InputManager.cpp:1129-1134`, `1199`), and the capability follows from the HIDs
(`InputManager.cpp:1928-1940`). The keymap matters: with `wtype`'s keymap (only the keys it was asked for) `type
"hello"` worked but `Z` was refused ("no key on the active keymap gives U+005A") and `key Return` sent evdev 28, which
that keymap does not map, so nothing arrived. A 40-line client holding a virtual keyboard with a full `us` keymap made
every check pass.

### Clipboard isolation

The plugin's paste never touches any seat selection: it sends its own data source to the target client's data device
only (`offerPaste`, `deskpilot.cpp:1872-1885`) and restores what that client would otherwise see
(`restorePaste`, `1856-1869`). In a session the "seat" is the session's, so nothing reaches the user either way; the
nested compositor does not bridge its clipboard to the parent (observed: different contents on each display).
Note the user's clipboard changed during the run from their own activity; the check that held was that it never
contained any test text.

### Accessibility bus

Apps launched in the session inherit the user's `DBUS_SESSION_BUS_ADDRESS` (seen in kitty's environment), so they
register on the user's AT-SPI registry: the registry listed 8 apps, the two nested GTK apps among the user's 6.
deskpilot tells them apart by pid alone: `_app_for_pid` (`a11y.py:376-400`) matches
`GetConnectionUnixProcessID` against the window's Hyprland pid, then `_choose_toplevel` (`a11y.py:403-423`) picks the
only frame, or the one whose name equals the window title. Pids are unique on the machine, so this is correct, but the
shared bus is not isolation:

- **Single-instance apps escape the session.** A `Gtk.Application` with a fixed app id launched in session A, then
  again in session B, opened its second window in A (both windows in A's client list, one process, nothing in B).
  Launched in an agent session while the user has the same app open, it opens on the user's screen. GApplication,
  and anything else that claims a well-known name on the session bus, does this.
- **A private bus per session works, with one extra step.** With its own `dbus-daemon --session` and the app started
  on it, `elements` against the user's bus said `no_tree`, as it should. Against the private bus it failed until
  `at-spi2-registryd` was started by hand: the AT-SPI bus here runs under `dbus-broker-launch`, which activates
  services through the systemd user manager, and that is not on a private bus (dbus-daemon log: activating
  `org.freedesktop.systemd1` "exited with status 1"; deskpilot: "Could not activate remote peer
  'org.a11y.atspi.Registry': unit failed"). With the registry started before the app, the full tree came back.
  An app started before the registry did not register and stayed `no_tree`.
- `at-spi-bus-launcher` names its socket `bus` plus the `DISPLAY` number (`at-spi2-core
  bus/at-spi-bus-launcher.c:557`, 2.56.0), and writes the `AT_SPI_BUS` property on the X root window when `DISPLAY`
  is set and `WAYLAND_DISPLAY` is not (`:594-600`). A session bus must therefore be started with the session's own
  `DISPLAY` and `WAYLAND_DISPLAY`, or its socket collides with another launcher's and the property lands on the
  wrong X server.

### XWayland

`nested.py:82` launches Hyprland with `env -u DISPLAY`; Hyprland still starts its own Xwayland (`xwayland:enabled` is
true) on a free display and exports it to its children. One Xwayland per session, so the X pointer, X focus and X
clipboard are all the session's. The plugin's XWayland refusals exist because those are shared with the user
(`deskpilot.cpp:1474`, `1543-1544`, `2395`); inside a session that reason is gone.

## What the session backend must change

1. **Give every session a keyboard with a full keymap**, before any app starts. Either the plugin creates one on
   load (an `Aquamarine::IKeyboard` passed to `g_pInputManager->newKeyboard`, `InputManager.cpp:1112`, which also
   takes the session config's `kb_layout`), or the session runs a small helper holding a virtual keyboard. The plugin
   route needs no extra process. The layout should be fixed per session (for example `us`), so `type` resolves
   against a known keymap rather than "the user's".
2. **Run actions in a session on the on-screen path (`plain` or `shown`), not `hidden`.** The seat is the agent's,
   so holding its input, moving its pointer and giving X focus touch nobody. That makes XWayland clicks, drags and
   keys work, and drops the need for the hidden pointer machinery there (`desk.py:165-169` picks the mode today).
3. **Let paste reach XWayland in a session**: setting the session seat's selection is safe there, so the refusal at
   `deskpilot.cpp:2395` becomes "outside a session only".
4. **Pass the session's environment explicitly** to everything deskpilot shells out to or connects: `WAYLAND_DISPLAY`
   for `clipboard.py` and `grim`, `HYPRLAND_INSTANCE_SIGNATURE` for hyprctl, and the session's D-Bus address for
   `a11y._bus_context` (`a11y.py:187` opens `bus="SESSION"` from the process environment).
5. **Give each session its own D-Bus session bus** (decision for the map), started with the session's `DISPLAY`
   and `WAYLAND_DISPLAY`, with `at-spi2-registryd` started on it before any app, and `org.a11y.Status.IsEnabled`
   set so Chromium and Electron expose their trees. This stops single-instance apps reaching the user's instances
   and keeps the agent's apps off the user's accessibility registry.
6. **Key per-window state by instance as well as address.** Shots and element snapshots live under one
   `~/.cache/deskpilot` keyed by window address (`a11y.py:175-176`, `screen.py:34-36`); addresses from different
   Hyprland processes are independent pointers and can collide. Within one session a relaunched window reused the
   exact address of the window it replaced (`0x561a332f4dd0`).

## Bugs found (not fixed)

- `a11y.py:35-41`, `240`: `NameHasNoOwner` and `ServiceUnknown` are mapped to `stale`, so a missing AT-SPI registry
  (no element involved) is reported as `stale: Could not call ...GetChildren on /org/a11y/atspi/accessible/root`
  instead of `unavailable`. The code should come from where the call failed (the registry root versus an element).
- `deskpilot.cpp:2395`: the XWayland paste refusal claims a paste "would replace the user's clipboard"; inside a
  nested session it would not (see change 3).
- `scripts/nested.py` gives the nested session no keyboard and no D-Bus isolation, so key, paste and accessibility
  checks cannot run in it as is, and an app it launches can open on the user's screen through D-Bus activation.

## Open risks

- Why the parent seat gave the nested session no keyboard was not traced (aquamarine only creates one on the
  keyboard capability); if a parent ever does advertise one, the user's real keystrokes could reach a session whose
  window gains focus in the parent. The plugin-created keyboard should be the only one, and the session should not
  take parent keyboard input at all.
- A private bus also cuts the session off from user services apps expect on the session bus (portals, secrets,
  notifications, `dconf`). Chrome and Firefox sign-in may need a keyring in the session; untested.
- Registry start order is a race: an app started before `at-spi2-registryd` stays without a tree until restarted.
- Only GTK 3 was tested for `elements`; Chromium, Electron and Qt on a private bus are unverified.
