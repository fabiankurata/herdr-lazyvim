#!/bin/sh
set -eu

socket=${1:-}
herdr=${2:-herdr}
pane=${3:-}
app=${4:-Alacritty}
open_bin=${OPEN_BIN:-/usr/bin/open}

if [ -n "$pane" ]; then
  if [ -n "$socket" ]; then
    HERDR_SOCKET_PATH="$socket" "$herdr" agent focus "$pane" >/dev/null 2>&1 || true
  else
    "$herdr" agent focus "$pane" >/dev/null 2>&1 || true
  fi
fi
"$open_bin" -a "$app"
