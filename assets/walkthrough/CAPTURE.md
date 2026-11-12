# Walkthrough recording

Deskpilot selects `logo.svg`, clicks **Export selected**, and confirms **Export asset** in a local GTK 4 demo app.
The fixture displays the shipped `assets/logo.svg` and `assets/wordmark-dark.svg`. It copies the selected asset into
its scratch library, and the recording script verifies the resulting file against the source byte for byte.
The app is illustrative; the input, agent cursor, lock-on brackets and press pulses are real Deskpilot output.

The recording runs on the `DPTEST` output of an owned, isolated Hyprland created by `scripts/nested.py`. It never
acts on the user's compositor or launches games. It records that output with `wf-recorder` so the plugin's cursor
is included; window screenshots intentionally exclude that overlay. Actions use coordinates from recorded window
shots at the fixture's checked 1238 by 758 layout. Each action is followed by a changed shot and a state check.

## Reproduce

Use a running Hyprland session with the usual Deskpilot plugin build dependencies, `grim`, `wf-recorder`, `ffmpeg`,
and system Python bindings for GTK 4, Cairo and Rsvg. On Arch the additional packages are `python-gobject`,
`python-cairo`, `gtk4`, `librsvg`, `wf-recorder` and `ffmpeg`. The app uses Liberation Sans.

```sh
uv sync --locked
uv run python scripts/walkthrough.py
```

The script creates its own nested compositor and launches only the fixture into it. It closes the recorder,
fixture and compositor even when an action fails. Raw recordings, state and command evidence stay in the scratch
folder printed by the script. `--scratch PATH` chooses that directory; `--export-only` re-exports an existing
`capture.mp4` there without launching a compositor or app.

- [Fixture source](../../scripts/walkthrough_app.py)
- [Capture and export script](../../scripts/walkthrough.py)
- [60 fps MP4](walkthrough.mp4), for a controllable preview
- [50 fps GIF](walkthrough.gif), for the README
- [Static result](walkthrough.png)

The MP4 uses H.264 with two encoding threads. The GIF uses a separate palette pass to keep memory bounded; its
20 ms frames avoid rounding 60 fps motion into GIF's centisecond timings. No cursor or app state is painted onto
the recording afterwards. The stationary white pointer belongs to the isolated desktop, not the user's session.
