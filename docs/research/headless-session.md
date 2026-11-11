# Can a nested Hyprland run with no window in the user's session?

Research for [#72](https://github.com/cjber/deskpilot/issues/72), part of [#71](https://github.com/cjber/deskpilot/issues/71).

## Answer

**Headless-only: no.** Hyprland 0.57 cannot start on aquamarine's headless backend alone. The headless backend has no
DRM fd, aquamarine refuses to start without an allocator, and Hyprland hardcodes its backend list with no env var to
change it.

**No window: yes.** Keep the Wayland backend connected to the user's compositor, which gives the instance the GPU
render node, and disable its `WAYLAND-1` output in the nested config. The `xdg_toplevel` is created but never gets a
buffer, so it never maps: no window, no `openwindow` event, nothing in the parent's `clients`. Rendering, apps, input
and `grim` all go to a headless output created with `hyprctl output create headless NAME`. Checked live (below).

The exact recipe:

```lua
-- nested hyprland.lua
hl.monitor({ output = "WAYLAND-1", disabled = true })
hl.monitor({ output = "DPTEST", mode = "1280x800", position = "0x0", scale = 1 })
```

```sh
# launched through the parent's exec_cmd table form, so a regression lands on special:dptest, not ws1
env -u DISPLAY LIBSEAT_BACKEND=<none> HYPRLAND_NO_SD_VARS=1 HYPRLAND_NO_SD_NOTIFY=1 HYPRLAND_NO_CRASHREPORTER=1 \
    Hyprland -c hyprland.lua
HYPRLAND_INSTANCE_SIGNATURE=<nested> hyprctl output create headless DPTEST
```

`LIBSEAT_BACKEND` set to a name libseat does not have makes the DRM backend fail at once and in a known way, instead
of asking logind for the user's session.

## Evidence

Sources: Hyprland at the installed commit `11cbc6c5` (0.57.2) as `hl:`, aquamarine `v0.16.0` (the installed
0.16.0-1) as `aq:`. Links: [hl] `https://github.com/hyprwm/Hyprland/blob/11cbc6c577b84ffb1edf4df3fc5b6128c3ae6947/`,
[aq] `https://github.com/hyprwm/aquamarine/blob/v0.16.0/`, plus the path and `#Lnn`.

### Backend selection is hardcoded

- `hl:src/Compositor.cpp:316-326` builds a fixed list: HEADLESS (mandatory), DRM (if available), WAYLAND (fallback).
  `hl:src/Compositor.cpp:337` is `// TODO: headless only`.
- aquamarine has no `AQ_BACKENDS`. Every env var it reads: `AQ_DRM_DEVICES`, `AQ_FORCE_LINEAR_BLIT`,
  `AQ_LIBINPUT_NO_PLUGINS`, `AQ_MGPU_NO_EXPLICIT`, `AQ_NO_ATOMIC`, `AQ_NO_KMS_REQUIREMENT`, `AQ_NO_MODIFIERS`,
  `AQ_TRACE` (grep of `aq:src` for `getenv`/`envEnabled`). None selects a backend.
- `FALLBACK` is not consulted at start: `aq:src/backend/Backend.cpp:134-146` starts every implementation in the list.

### Headless alone has no allocator

- `aq:src/backend/Headless.cpp:133-139`: `drmFD()` and `drmRenderNodeFD()` return `-1`.
- `aq:src/backend/Backend.cpp:162-179`: the GBM allocator is built from the first implementation with a DRM fd; with
  none, and the first not being `NULL`, it logs `Cannot open backend: no allocator available` and fails. Hyprland
  never asks for the `NULL` backend.
- DRM needs a libseat session first (`aq:src/backend/drm/DRM.cpp:296-302`); the user's seat is taken by the user's
  Hyprland, and taking it is out of the question anyway.
- The Wayland backend gets its fd from the parent's `zwp_linux_dmabuf_v1` main device
  (`aq:src/backend/Wayland.cpp:388-465`), and Hyprland's GL renderer uses that fd
  (`hl:src/Compositor.cpp:360-363`, `hl:src/render/OpenGL.cpp:297`).

Live check A, run from a plain shell with `WAYLAND_DISPLAY` pointing at no socket and a bogus `LIBSEAT_BACKEND`:

```
ERR from aquamarine ]: Failed to open a session
ERR from aquamarine ]: DRM Backend failed
ERR from aquamarine ]: Wayland backend cannot start: wl_display_connect failed (is a wayland compositor running?)
CRIT from aquamarine ]: Cannot open backend: no allocator available
  what():  CBackend::create() failed!        (exit 134)
```

### Why a disabled Wayland output shows nothing

- At start the Wayland backend always creates one output (`aq:src/backend/Wayland.cpp:154`). It sets the title and app
  id, then commits with a null buffer (`aq:src/backend/Wayland.cpp:566-575`): role assigned, nothing mapped.
- The surface only gets a buffer in `CWaylandOutput::commit` (`aq:src/backend/Wayland.cpp:682-719`). A disabled
  monitor never renders, so that never happens.
- `hl:src/output/Monitor.cpp:275-276` marks Wayland and headless outputs `m_createdByUser`, so `hyprctl output remove
  WAYLAND-1` is also allowed (`hl:src/ipc/s1/Commands.cpp:1870-1878`). Its `destroy()` attaches null and commits
  (`aq:src/backend/Wayland.cpp:634-645`), but the window would map first. The config rule never maps it at all.
- Until an enabled output exists, Hyprland adds a headless `FALLBACK` output after 2 s
  (`hl:src/state/FallbackState.cpp:13`, `:100-117`). That is harmless.

Live check B (`hl.dsp.exec_cmd(..., { workspace = "special:dptest silent" })` from the user's instance, config as
above, nested pid 2079647):

| Check | Result |
| --- | --- |
| Parent events during start | only `windowtitlev2>>...,aquamarine - WAYLAND-1`, no `openwindow` |
| Parent `clients` with class `aquamarine` | two, pids 2035559 and 2048245 (other sessions' nested hosts); none with pid 2079647 |
| Nested monitors | `WAYLAND-1` disabled, `DPTEST` 1920x1080 (no rule matched it, so the headless default) |
| Nested log | `zwp_linux_dmabuf_v1: Got node /dev/dri/renderD128` |
| `grim -o DPTEST` | rc 0, 0.06 to 0.08 s, twice |
| kitty launched inside, then `wtype` on the nested socket | text drawn on DPTEST (first character dropped, see risks) |
| `grim -T <stableId>` on the kitty window | rc 0, 1836x996 |
| Stop | `hl.dsp.exit()` on the nested instance; pid 2079647 and kitty gone |

### Frames, input and capture on a headless output

- Frames: a headless output runs a timer at its mode's refresh rate whenever a frame is scheduled
  (`aq:src/backend/Headless.cpp:56-105`) and presents on every commit (`:27-34`). It needs nothing from the parent, as
  `scripts/nested.py` already relies on.
- Input: the headless backend has no input devices and no cursor plane (`aq:src/backend/Headless.cpp:145-151`,
  `:193-195`). The Wayland backend binds the parent's `wl_seat` (`aq:src/backend/Wayland.cpp:353-365`), but the parent
  only sends pointer and keyboard events to a focused, mapped surface, so an unmapped one gets none. Agent input goes
  through the nested instance's own `zwlr_virtual_pointer_manager_v1` and `zwp_virtual_keyboard_manager_v1`
  (`hl:src/managers/ProtocolManager.cpp:194-195`), or the deskpilot plugin loaded into the nested instance.
- Clipboard: aquamarine's Wayland backend binds no data device (no `data_device` in `aq:src/backend/Wayland.cpp`), so
  the nested clipboard is separate from the user's.
- Capture: `zwlr_screencopy_manager_v1`, `hyprland_toplevel_export_manager_v1` and ext-image-copy-capture are
  registered with no backend condition (`hl:src/managers/ProtocolManager.cpp:231-234`); the DRM-only protocols
  (lease, syncobj) are the gated ones (`:241-257`). dmabuf is offered when the renderer has formats (`:260-265`).

## Compared with `scripts/nested.py`

`nested.py` uses the same backends but leaves `WAYLAND-1` enabled, so the nested compositor maps a real window on
`special:dptest` and depends on the "kill if it opens elsewhere" guard. Its `DPTEST` headless output sits at
`10000x0` only because `grim -o` would otherwise touch the starved window output. With `WAYLAND-1` disabled, both
workarounds go away: there is no window to place or guard, and `DPTEST` can sit at `0x0`.

## What a session backend should do

1. Start one nested Hyprland per `DESKPILOT_AGENT` from the parent with the exec_cmd table form on
   `special:dptest silent`, so any regression that maps the toplevel still lands hidden.
2. Config: `WAYLAND-1` disabled, a rule for the agent's headless output, no animations, no logo or splash, the plugin
   loaded if input goes through it. Set a bogus `LIBSEAT_BACKEND`, `HYPRLAND_NO_SD_*` and
   `HYPRLAND_NO_CRASHREPORTER`.
3. Create the headless output by name, focus it, and point every `Desk` call at the nested
   `HYPRLAND_INSTANCE_SIGNATURE` and `WAYLAND_DISPLAY`.
4. Replace the `openwindow` guard with a check that the parent has no mapped client with the nested pid, and stop the
   session if one appears.
5. Dismiss the start-hyprland banner (`hyprctl dismissnotify`); it shows up in captures.
6. Stop with `hl.dsp.exit()`, wait on the pid, SIGKILL after a timeout, and remove the run's `$XDG_RUNTIME_DIR/hypr/<sig>`
   directory, which Hyprland leaves behind.

## Open risks

- The nested instance is still a Wayland client of the user's compositor. If the user's Hyprland restarts, every
  agent session dies with it.
- "Never maps" depends on aquamarine never committing a buffer for a disabled output. That holds in v0.16.0 (above)
  but is not a documented contract; the hidden-workspace launch and the pid check are the backstop.
- The parent still holds an unmapped `xdg_toplevel` per session and emits `windowtitlev2` for it. Tools that list
  unmapped toplevels could see it.
- `wtype` lost its first character into a freshly started kitty. Likely the virtual keyboard keymap arriving after the
  first key; not investigated. The `type` path needs its own check.
- A true headless-only instance would need an upstream change: a Hyprland option to drop the Wayland and DRM
  backends, plus an aquamarine allocator that opens a render node on its own (`AQ_DRM_DEVICES` only applies to DRM).
