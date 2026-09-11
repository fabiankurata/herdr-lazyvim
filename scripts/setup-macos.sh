#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-check}
force=false
if [ "${2:-}" = "--force" ]; then
  force=true
elif [ -n "${2:-}" ]; then
  printf 'Unknown option: %s\n' "$2" >&2
  exit 2
fi

usage() {
  cat <<'EOF'
Usage: scripts/setup-macos.sh [check|plan|install] [--force]

  check    Report dependencies and managed-file status (default).
  plan     Print every file and command the installer would use.
  install  Install dependencies and copy the portable configuration.

Existing configuration is preserved unless install is passed --force. In
that case, every replaced file receives a timestamped backup first.
EOF
}

managed_files() {
  printf '%s\t%s\n' \
    "$root/setup/herdr/config.toml" "$HOME/.config/herdr/config.toml" \
    "$root/setup/alacritty/alacritty.toml" "$HOME/.config/alacritty/alacritty.toml" \
    "$root/setup/nvim/lua/plugins/languages.lua" "$HOME/.config/nvim/lua/plugins/languages.lua" \
    "$root/nvim/lua/plugins/herdr-lazyvim.lua" "$HOME/.config/nvim/lua/plugins/herdr-lazyvim.lua" \
    "$root/setup/herdr/plugin.toml" "$HOME/.config/herdr/plugins/config/fabiankurata.lazyvim/config.toml"
}

file_status() {
  source_file=$1
  destination=$2
  if [ ! -e "$destination" ]; then
    status=missing
  elif cmp -s "$source_file" "$destination"; then
    status=managed
  else
    status=custom
  fi
  printf '%-8s %s\n' "$status" "$destination"
}

check_setup() {
  printf 'Dependencies:\n'
  for command_name in brew git gh jq nvim terminal-notifier herdr; do
    if command -v "$command_name" >/dev/null 2>&1; then
      printf '  ok       %s\n' "$command_name"
    else
      printf '  missing  %s\n' "$command_name"
    fi
  done
  printf '\nManaged files:\n'
  while IFS="$(printf '\t')" read -r source_file destination; do
    file_status "$source_file" "$destination"
  done <<EOF
$(managed_files)
EOF
}

show_plan() {
  cat <<EOF
Homebrew bundle:
  $root/setup/Brewfile

Herdr (only when missing):
  download and run https://herdr.dev/install.sh

LazyVim starter (only when ~/.config/nvim/init.lua is missing):
  clone https://github.com/LazyVim/starter

Managed files:
EOF
  while IFS="$(printf '\t')" read -r source_file destination; do
    printf '  %s\n    -> %s\n' "$source_file" "$destination"
  done <<EOF
$(managed_files)
EOF
  cat <<EOF

Final setup:
  herdr plugin link "$root" --enabled
  herdr plugin install -y persiyanov/herdr-reviewr
  herdr integration install codex
  nvim --headless "+Lazy! sync" +qa
EOF
}

install_file() {
  source_file=$1
  destination=$2
  mkdir -p "$(dirname "$destination")"

  if [ -e "$destination" ] && ! cmp -s "$source_file" "$destination"; then
    if [ "$force" != true ]; then
      printf 'skip     %s (already customized; use --force to replace)\n' "$destination"
      return
    fi
    backup="$destination.backup-$(date +%Y%m%d-%H%M%S)"
    cp -p "$destination" "$backup"
    printf 'backup   %s\n' "$backup"
  fi

  cp "$source_file" "$destination"
  printf 'install  %s\n' "$destination"
}

install_setup() {
  [ "$(uname -s)" = Darwin ] || {
    printf 'This installer supports macOS only.\n' >&2
    exit 1
  }
  command -v brew >/dev/null 2>&1 || {
    printf 'Homebrew is required. Install it from https://brew.sh first.\n' >&2
    exit 1
  }

  brew bundle --file "$root/setup/Brewfile"

  if ! command -v herdr >/dev/null 2>&1; then
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT
    curl -fsSL https://herdr.dev/install.sh -o "$installer"
    sh "$installer"
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if [ ! -e "$HOME/.config/nvim/init.lua" ]; then
    git clone https://github.com/LazyVim/starter "$HOME/.config/nvim"
    rm -rf "$HOME/.config/nvim/.git"
  fi

  while IFS="$(printf '\t')" read -r source_file destination; do
    install_file "$source_file" "$destination"
  done <<EOF
$(managed_files)
EOF

  herdr plugin link "$root" --enabled
  herdr plugin install -y persiyanov/herdr-reviewr
  herdr integration install codex
  nvim --headless "+Lazy! sync" +qa
  herdr server reload-config >/dev/null 2>&1 || true

  printf '\nSetup complete. Run scripts/setup-macos.sh check to inspect it.\n'
}

case "$mode" in
  check) check_setup ;;
  plan) show_plan ;;
  install) install_setup ;;
  -h|--help|help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
