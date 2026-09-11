# Changelog

## 0.1.0 - 2026-09-11

- Keep one LazyVim process per Herdr workspace, hiding and restoring the live
  pane without restarting Neovim or its language servers.
- Refresh both sides of the layout after hiding or restoring an editor, using
  the new pane ID returned by Herdr after a move.
- Delay PTY refreshes until Herdr's asynchronous layout has settled.
- Add transparent editor surfaces and stronger mode indicators.
- Make Snacks explorer and picker surfaces inherit terminal transparency.
- Show explicit Herdr agent state text with distinct status symbols.
- Focus the originating agent when its macOS notification is clicked.
- Choose split, zoomed, tab, or overlay placement from plugin configuration.
- Bundle project navigation, Git review scopes, PR tools, and line comments.
- Send accumulated review comments to one of several agents in the workspace.
- Present comments as wrapped dark cards below their connected gutter ranges,
  with configurable placement, width, colors, and range emphasis.
- Restore the source window and its previous Normal or Visual mode after the
  comment editor closes.
- Give the linked plugin's Lua modules precedence over legacy copies in the
  user's Neovim configuration.
- Add a guarded macOS bootstrap with portable Herdr, Alacritty, LazyVim,
  language-tooling, Homebrew, and font configuration.
