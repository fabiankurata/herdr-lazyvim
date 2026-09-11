#!/usr/bin/env bash
set -euo pipefail

plugin_root="${HERDR_PLUGIN_ROOT:?HERDR_PLUGIN_ROOT is required}"
# shellcheck source=config.sh
source "$plugin_root/herdr/config.sh"

editor=$(expand_home "$(config_string editor "nvim")")
target=$(expand_home "$(config_string target ".")")
review_enabled=$(config_bool review_enabled true)
comment_completion=$(config_bool comment_completion false)
comment_display=$(config_string comment_display "card")
comment_range_style=$(config_string comment_range_style "subtle")
comment_card_position=$(config_string comment_card_position "below")
comment_card_width=$(config_string comment_card_width "72")
comment_card_background=$(config_string comment_card_background "#16161e")
comment_card_border=$(config_string comment_card_border "#ff9e64")
transparent_background=$(config_bool transparent_background true)
mode_emphasis=$(config_string mode_emphasis "cursorline")

case "$comment_display" in
  card | virtual_line | eol) ;;
  *)
    printf 'Herdr LazyVim: invalid comment_display: %s\n' "$comment_display" >&2
    exit 1
    ;;
esac
case "$comment_card_position" in
  above | below) ;;
  *)
    printf 'Herdr LazyVim: invalid comment_card_position: %s\n' "$comment_card_position" >&2
    exit 1
    ;;
esac
if ! printf '%s' "$comment_card_width" | grep -Eq '^[0-9]+$' \
  || [ "$comment_card_width" -lt 32 ] \
  || [ "$comment_card_width" -gt 120 ]; then
  printf 'Herdr LazyVim: comment_card_width must be between 32 and 120\n' >&2
  exit 1
fi
for color_name in comment_card_background comment_card_border; do
  color_value=${!color_name}
  if ! printf '%s' "$color_value" | grep -Eq '^#[0-9a-fA-F]{6}$'; then
    printf 'Herdr LazyVim: invalid %s: %s\n' "$color_name" "$color_value" >&2
    exit 1
  fi
done
case "$comment_range_style" in
  subtle | gutter | selection) ;;
  *)
    printf 'Herdr LazyVim: invalid comment_range_style: %s\n' "$comment_range_style" >&2
    exit 1
    ;;
esac
case "$mode_emphasis" in
  cursorline | window | none) ;;
  *)
    printf 'Herdr LazyVim: invalid mode_emphasis: %s\n' "$mode_emphasis" >&2
    exit 1
    ;;
esac

if [ ! -x "$editor" ]; then
  resolved=$(command -v "$editor" 2>/dev/null || true)
  if [ -z "$resolved" ]; then
    printf 'Herdr LazyVim: editor is not executable: %s\n' "$editor" >&2
    exit 1
  fi
  editor="$resolved"
fi

set -- \
  --cmd "let g:herdr_lazyvim_plugin=1" \
  --cmd "let g:herdr_lazyvim_root='${plugin_root//\'/\'\'}'"

if [ "$review_enabled" = true ]; then
  lua_root=${plugin_root//\'/\'\'}
  set -- "$@" \
    -c "set runtimepath^=${plugin_root// /\\ }/nvim" \
    -c "lua package.path = '$lua_root/nvim/lua/?.lua;$lua_root/nvim/lua/?/init.lua;' .. package.path; require('herdr_lazyvim').setup({ comment_completion = $comment_completion, comment_display = '$comment_display', comment_range_style = '$comment_range_style', comment_card_position = '$comment_card_position', comment_card_width = $comment_card_width, comment_card_background = '$comment_card_background', comment_card_border = '$comment_card_border', transparent_background = $transparent_background, mode_emphasis = '$mode_emphasis' })"
fi

exec "$editor" "$@" "$target"
