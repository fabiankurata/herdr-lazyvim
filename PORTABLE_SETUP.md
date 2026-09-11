# Portable macOS setup

This repository contains two parts:

1. The Herdr plugin implements the persistent LazyVim pane, review comments,
   review hub, mode theme, and targeted agent notifications.
2. `setup/` captures the host configuration that connects Herdr, Alacritty,
   LazyVim, language tooling, and the Nerd Font into the same workflow.

The workstation configuration is small and reviewable. Project runtimes,
credentials, SSH keys, GitHub authentication, and repository-specific secrets
stay outside this repository.

## Preview a new machine

Clone the repository, then inspect what the bootstrap would install:

```sh
git clone https://github.com/fabiankurata/herdr-lazyvim.git
cd herdr-lazyvim
./scripts/setup-macos.sh plan
./scripts/setup-macos.sh check
```

`check` labels each destination as `managed`, `custom`, or `missing`. The
installer preserves customized files by default.

## Install

Install Homebrew first, then run:

```sh
./scripts/setup-macos.sh install
```

This installs the packages from `setup/Brewfile`, installs Herdr with its
official installer when needed, creates a LazyVim starter configuration when
needed, copies the managed overlays, links this checkout as a Herdr plugin,
installs `herdr-reviewr`, installs the Codex integration, and synchronizes
Neovim plugins.

To replace an existing managed configuration, use:

```sh
./scripts/setup-macos.sh install --force
```

Every differing file receives a timestamped backup before replacement.

## What is captured

- `setup/herdr/config.toml`: workspace, tab, pane, sidebar, and plugin actions.
- `setup/alacritty/alacritty.toml`: opacity, Nerd Font, and macOS key passthrough.
- `setup/nvim/lua/plugins/languages.lua`: Vue, TypeScript, Go, Rust, Terraform,
  Docker, JSON, YAML, TOML, and Makefile support.
- `nvim/lua/plugins/herdr-lazyvim.lua`: editor dependencies and plugin runtime.
- `setup/herdr/plugin.toml`: the enabled persistent editor, review, theme, and
  actionable notification behavior. `config.example.toml` documents defaults
  for other users.
- `setup/Brewfile`: command-line tools, Alacritty, and the font.

After changing a host file, update its corresponding file under `setup/` and
commit both the plugin and setup changes together.
