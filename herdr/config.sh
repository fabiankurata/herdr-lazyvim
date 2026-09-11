#!/usr/bin/env bash

config_file="${HERDR_PLUGIN_CONFIG_DIR:-$HOME/.config/herdr/plugins/config/${HERDR_PLUGIN_ID:-fabiankurata.lazyvim}}/config.toml"

config_string() {
  local key="$1"
  local fallback="$2"
  local value=""
  if [ -r "$config_file" ]; then
    value=$(sed -nE "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\\1/p" "$config_file" | tail -n 1)
  fi
  printf '%s' "${value:-$fallback}"
}

config_bool() {
  local key="$1"
  local fallback="$2"
  local value=""
  if [ -r "$config_file" ]; then
    value=$(sed -nE "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*(.*)$/\\1/p" "$config_file" | tail -n 1)
  fi
  value="${value%%#*}"
  value=$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
  value="${value:-$fallback}"
  case "$value" in
    true | false) printf '%s' "$value" ;;
    *)
      printf 'Herdr LazyVim: invalid boolean for %s: %s\n' "$key" "$value" >&2
      return 1
      ;;
  esac
}

expand_home() {
  case "$1" in
    "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}
