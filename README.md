# Herdr LazyVim

Herdr LazyVim keeps one project editor alive in each Herdr workspace. Open it
from any tab and the plugin resumes the existing pane with the same buffers,
cursor positions, window layout, undo history, and running language servers.

The Neovim integration adds line comments that can be sent to any Herdr agent
in the workspace. LazyVim remains responsible for project navigation, LSP,
LazyGit, diffs, and pull requests. The integration can annotate buffers opened
by those tools, but it does not install or replace them and does not depend on
`herdr-reviewr`.

To reproduce the complete macOS setup, follow the
[`PORTABLE_SETUP.md`](PORTABLE_SETUP.md) guide. It covers Herdr, Alacritty,
LazyVim, language support, the font, and Homebrew packages.

## Requirements

- Herdr 0.9 or newer
- Neovim 0.10 or newer; LazyVim is recommended
- `jq` and `git`
- `terminal-notifier` for actionable macOS notifications

## Install

Install the Herdr plugin:

```sh
herdr plugin install fabiankurata/herdr-lazyvim
```

For a local checkout:

```sh
git clone https://github.com/fabiankurata/herdr-lazyvim.git
cd herdr-lazyvim
herdr plugin link --enabled "$PWD"
```

Copy the LazyVim spec that loads the Herdr integration in plugin-owned editor
processes:

```sh
cp nvim/lua/plugins/herdr-lazyvim.lua ~/.config/nvim/lua/plugins/herdr-lazyvim.lua
```

Copy `config.example.toml` to the plugin configuration directory. The default
editor command is `nvim`:

```sh
mkdir -p ~/.config/herdr/plugins/config/fabiankurata.lazyvim
cp config.example.toml ~/.config/herdr/plugins/config/fabiankurata.lazyvim/config.toml
```

Add actions to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "cmd+shift+e"
type = "plugin_action"
command = "fabiankurata.lazyvim.toggle"
description = "toggle LazyVim editor"

[[keys.command]]
key = "cmd+shift+o"
type = "plugin_action"
command = "fabiankurata.lazyvim.open"
description = "open or resume LazyVim editor"
```

Reload the running server after changing the Herdr configuration:

```sh
herdr server reload-config
```

## Editor lifecycle

`open` creates an editor or focuses the existing editor in the workspace.
`toggle` parks the live editor in a background tab when invoked from the
focused editor, then restores the same pane on the next invocation. `close`
explicitly ends the editor process. Set `toggle_behavior` to `close` for the
destructive behavior or `open_only` when the shortcut should never hide it.

`resize_workaround = "nudge"` corrects stale terminal dimensions in both
directions. After hiding the editor, a zoom round-trip refreshes the remaining
agent pane at full width. After restoring the editor, the plugin reads the new
pane ID returned by Herdr before nudging the new split. The configurable
`resize_settle_delay` lets each asynchronous layout finish first. Set the
workaround to `none` when Herdr no longer needs it.

The editor canvas and project sidebars inherit the terminal transparency by
default, while floating dialogs retain an opaque background for readability.
`mode_emphasis = "cursorline"` gives Normal, Insert, Visual, Replace, Command,
and Terminal modes distinct active-line colors, cursor shapes, and a prominent
statusline label. Use `window` for a full-window mode tint or `none` to disable
the active-line treatment.

By default, an agent tab gets a split editor and a terminal-only tab gets a
zoomed editor. Supported placements are `split`, `zoomed`, `tab`, and `overlay`.
See [`config.example.toml`](config.example.toml) for every option.

## Comment workflow

The integration is loaded only in editor processes opened by this plugin. It
works in ordinary project buffers and has an optional adapter for Diffview
buffers when Diffview is already installed. It does not define Git navigation,
diff scopes, file explorers, or pull-request behavior.

The Herdr-specific mappings are:

| Mapping | Action |
| --- | --- |
| `<leader>rc` | Comment on the current line or visual selection |
| `<leader>re` | Edit the comment under the cursor |
| `<leader>rd` | Delete the comment under the cursor |
| `<leader>rl` | List saved comments |
| `<leader>rs` | Send all comments to a selected Herdr agent |

In the comment editor, `Cmd+Enter` or `Ctrl+S` saves and `q` cancels. Comment
completion is disabled by default. Comments are persisted per Git worktree
until sent or deleted. Saving or cancelling restores the source window and the
mode that was active before the editor opened, including the original Visual
selection.

Review writers coordinate through a kernel-released file lock held across the
complete read, update, and replacement transaction. Process termination closes
the lock descriptor, so abandoned owner metadata or pathname reclamation cannot
block or displace another writer. The stable lock file is not an ownership
record and remains on disk after release. The state file changes only after a
complete temporary-file write and close. A successful same-directory rename is
the commit point: failures before it preserve the previous file. This protects
cooperating editor processes and partial-write failures, but does not claim
power-loss durability beyond the guarantees of the host filesystem.

Commented ranges use a connected gutter marker and subtle tint. A wrapped,
dark review card with a rounded orange border appears below the final selected
line and identifies the file and complete range. `comment_card_position`,
`comment_card_width`, `comment_card_background`, and `comment_card_border` are
configurable. Set `comment_range_style` to `gutter` for no tint or `selection`
for the stronger Visual-mode background.

Use the Git workflow you already prefer around these mappings. LazyVim opens
LazyGit with `<leader>gg`. A separately installed Diffview can review a working
tree with `:DiffviewOpen`, changes since `HEAD` with `:DiffviewOpen HEAD`, or a
branch with a range such as `:DiffviewOpen origin/main...HEAD`. Select lines in
the resulting source buffer and use `<leader>rc`; the Herdr comment keeps the
file, range, and selected source text as agent context.

The portable workstation configuration adds these regular LazyVim mappings:

| Mapping | Action |
| --- | --- |
| `<leader>gv` | Review uncommitted changes in Diffview |
| `<leader>gV` | Review the branch against `origin/main` using local buffers |
| `<leader>gH` | Open Diffview history for the current file |
| `<leader>gq` | Close Diffview |

`<leader>gh` remains LazyVim's existing hunk-action prefix.
Inside a Diffview, plain `q` also closes the complete view from the diff, file
panel, or file-history panel.

## Agent status and macOS notifications

The portable Herdr configuration shows both a status symbol and status text in
the sidebar. It disables the generic system toast because this plugin can emit
an actionable macOS notification for `blocked` and `done` agent states. Clicking
that notification focuses the originating Herdr agent and then activates
Alacritty.

Install `terminal-notifier` and set `actionable_notifications = true` in the
plugin configuration to enable it. The workstation setup enables it. The
public example leaves it opt-in. `notification_app` and
`notification_sound` are configurable. Herdr must be running in the named
terminal application for the final activation step to find the correct window.

## Development

Run the shell lifecycle fixtures and Neovim smoke tests with:

```sh
make test
```

The test suite uses a fake Herdr CLI and does not modify a live workspace.

Before publishing a release, run the repository privacy audit:

```sh
make audit
```
