# Changelog

## 0.1.0 - 2026-09-11

- Keep one LazyVim process per Herdr workspace, hiding and restoring the live
  pane without restarting Neovim or its language servers.
- Leave zoom mode before parking the editor so a terminal-only tab toggles
  directly between the full editor and the full terminal.
- Refresh both sides of the layout after hiding or restoring an editor, using
  the new pane ID returned by Herdr after a move.
- Delay PTY refreshes until Herdr's asynchronous layout has settled.
- Add transparent editor surfaces and stronger mode indicators.
- Make Snacks explorer and picker surfaces inherit terminal transparency.
- Show explicit Herdr agent state text with distinct status symbols.
- Focus the originating agent when its macOS notification is clicked.
- Choose split, zoomed, tab, or overlay placement from plugin configuration.
- Keep Git navigation, diffs, and pull-request tooling in ordinary LazyVim
  configuration while the Herdr integration owns only its comment workflow.
- Add workstation Diffview mappings for uncommitted changes, branch changes,
  current-file history, and closing the view.
- Close the complete Diffview with `q` from its diff and file panels.
- Send accumulated review comments to one of several agents in the workspace.
- Present comments as wrapped dark cards below their connected gutter ranges,
  with configurable placement, width, colors, and range emphasis.
- Restore the source window and its previous Normal or Visual mode after the
  Insert-mode comment editor closes, including saves triggered from Insert.
- Give the linked plugin's Lua modules precedence over legacy copies in the
  user's Neovim configuration.
- Add a guarded macOS bootstrap with portable Herdr, Alacritty, LazyVim,
  language-tooling, Homebrew, and font configuration.
