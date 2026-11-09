# Contributing

Bug reports and pull requests are welcome. [AGENTS.md](AGENTS.md) has the layout and the rules a change must keep.

## Before you start

- **Bugs:** open an issue with the command you ran, the target window from `deskpilot --json windows`, your
  Hyprland version and `deskpilot plugin status`. A click or key that reached the wrong window is the most serious
  kind of bug, so say so in the title.
- **New actions and larger changes:** open an issue first, so we can agree how the user's pointer and focus are
  kept.
- **Security issues:** report them privately, as [SECURITY.md](SECURITY.md) describes.

## Making a change

Run the checks in [AGENTS.md](AGENTS.md#checks). A change needs:

- passing checks
- a test for each new refusal, including the case that must refuse
- a line under Unreleased in `CHANGELOG.md` if a user would notice it
- matching updates to `README.md` and `skills/deskpilot/SKILL.md` if a command or flag changed

Tests must never touch the real desktop. Pass fakes into `Desk` and `Hyprland` as `tests/test_core.py` does. A change
to the plugin also needs a manual check on a running Hyprland, with the clients you tried named in the PR.

Keep PRs to one idea and use [Conventional Commits](https://www.conventionalcommits.org/) for titles.

## Releasing

Bump `version` in `pyproject.toml` and `.claude-plugin/plugin.json`, run `uv lock`, move the Unreleased lines under
a new `## [X.Y.Z] - YYYY-MM-DD` heading, and merge. Then tag the merged commit `vX.Y.Z` and push the tag. The release
workflow checks the tag is on main and matches both versions, and publishes a GitHub release whose notes are that
version's changelog entry.
