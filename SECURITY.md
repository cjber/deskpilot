# Security

deskpilot sends clicks and keys into other people's windows on purpose, so a bug that delivers input somewhere it
was not asked to is a security issue. Relevant reports include:

- Input reaching a window other than the one named, such as a click passing through to the window behind, or keys
  landing in the focused window when `--window` was given.
- The user's own pointer, keyboard focus or typing being taken, lost or leaked into an agent's keystrokes outside
  `--take-pointer` and `--take-focus`.
- The compositor plugin crashing or hanging Hyprland, leaving a button or key held, or accepting a request it should
  refuse.
- The MCP server acting on anything other than its documented tools and arguments, or exposing screenshots beyond
  the window asked for.
- A path that loads a plugin build other than one deskpilot built from its own source.

Report a vulnerability through
[GitHub private vulnerability reporting](https://github.com/cjber/deskpilot/security/advisories/new), not a public
issue. Include the version, your Hyprland version, the steps to reproduce it and what was affected. You will get a
reply within a week.

The latest release is the only supported version.
