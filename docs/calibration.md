# Development and calibration

[Back to the README](../README.md)


[AGENTS.md](../AGENTS.md) has the checks, the layout and the rules a change must keep, and
[CONTRIBUTING.md](../CONTRIBUTING.md) how to report a bug or send a change.
[SECURITY.md](../SECURITY.md) says how to report a vulnerability privately.

For live input calibration, use an explicit parent compositor and a private desktop:

```sh
HYPRLAND_INSTANCE_SIGNATURE=... uv run python scripts/input_calibration.py --chrome --wine --output /path/to/results.json
```

This checks GTK 3 pointer and paste input, GTK 4 independent keyboards, native and XWayland Chrome, Wine
punctuation and ordinary/raw button events, selections and parent focus/cursor preservation. It then stops its
desktop and checks for surviving owned processes. Omit `--chrome` or `--wine` when those clients are unavailable;
Wine checks require `winegcc`. GTK checks require system Python with GTK 3 and GTK 4 introspection. For a
deterministic preservation check, use a parent created by `scripts/nested.py` so other desktop activity cannot
change the measured cursor or focus.

`scripts/popup_calibration.py --output /path/to/popups.json`, with the same explicit parent, checks a delayed
agent popup after concurrent user input, genuine user popup grabs, and plugin unload/reload. It stops its private
desktop and verifies owned-process cleanup and parent pointer/focus preservation. `--plugin /path/to/build.so`
loads a baseline build in that private desktop for a red/green comparison.

`scripts/adoption_calibration.py --output /path/to/adoption.json`, with the same explicit parent, checks exact-window
handoff in a shared browser process, hidden typing, refusal while the user uses its client, release, stop and
plugin unload. It verifies sibling isolation, user-moved windows, pointer/focus preservation and process cleanup.

`scripts/containment_calibration.py SIGNATURE` and its `--negative` and `--negative-constraints` controls run only against a compositor owned by
`scripts/nested.py`, testing agent-seat relative motion and pointer constraints.
