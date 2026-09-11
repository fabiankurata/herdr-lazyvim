#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

mode="${1:-toggle}"
case "$mode" in
  open | toggle | close) ;;
  *)
    printf 'Usage: pane.sh {open|toggle|close}\n' >&2
    exit 2
    ;;
esac

plugin_root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=config.sh
source "$plugin_root/herdr/config.sh"

H="${HERDR_BIN_PATH:-herdr}"
SLEEP="${HERDR_LAZYVIM_SLEEP_BIN:-sleep}"
plugin_id="${HERDR_PLUGIN_ID:-fabiankurata.lazyvim}"
context="${HERDR_PLUGIN_CONTEXT_JSON:-}"
[ -n "$context" ] || context='{}'

agent_placement=$(config_string agent_placement "split")
terminal_placement=$(config_string terminal_placement "zoomed")
direction=$(config_string direction "right")
reuse_existing=$(config_bool reuse_existing true)
toggle_behavior=$(config_string toggle_behavior "hide")
prefer_agent_in_tab=$(config_bool prefer_agent_in_tab true)
deduplicate_existing=$(config_bool deduplicate_existing true)
hide_tab_label=$(config_string hide_tab_label "LazyVim")
resize_workaround=$(config_string resize_workaround "nudge")
resize_settle_delay=$(config_string resize_settle_delay "0.12")

valid_placement() {
  case "$1" in
    split | zoomed | tab | overlay) return 0 ;;
    *) return 1 ;;
  esac
}

for setting in agent_placement terminal_placement; do
  value="${!setting}"
  valid_placement "$value" || {
    printf 'Herdr LazyVim: invalid %s: %s\n' "$setting" "$value" >&2
    exit 1
  }
done
case "$direction" in
  right | down) ;;
  *)
    printf 'Herdr LazyVim: invalid direction: %s\n' "$direction" >&2
    exit 1
    ;;
esac
case "$toggle_behavior" in
  hide | close | open_only) ;;
  *)
    printf 'Herdr LazyVim: invalid toggle_behavior: %s\n' "$toggle_behavior" >&2
    exit 1
    ;;
esac
case "$resize_workaround" in
  nudge | none) ;;
  *)
    printf 'Herdr LazyVim: invalid resize_workaround: %s\n' "$resize_workaround" >&2
    exit 1
    ;;
esac
if ! printf '%s' "$resize_settle_delay" | grep -Eq '^[0-9]+([.][0-9]+)?$'; then
  printf 'Herdr LazyVim: invalid resize_settle_delay: %s\n' "$resize_settle_delay" >&2
  exit 1
fi

workspace=$(printf '%s' "$context" | jq -r '.workspace_id // empty')
tab=$(printf '%s' "$context" | jq -r '.tab_id // empty')
focused=$(printf '%s' "$context" | jq -r '.focused_pane_id // empty')
[ -n "$workspace" ] || workspace="${HERDR_WORKSPACE_ID:-${HERDR_ACTIVE_WORKSPACE_ID:-}}"
[ -n "$tab" ] || tab="${HERDR_TAB_ID:-${HERDR_ACTIVE_TAB_ID:-}}"
[ -n "$focused" ] || focused="${HERDR_PANE_ID:-${HERDR_ACTIVE_PANE_ID:-}}"
[ -n "$workspace" ] || {
  printf 'Herdr LazyVim: no active workspace\n' >&2
  exit 1
}

panes=$("$H" pane list --workspace "$workspace")
existing=("")
existing_count=0
while IFS= read -r pane; do
  [ -n "$pane" ] || continue
  info=$("$H" pane process-info --pane "$pane" 2>/dev/null || true)
  if printf '%s' "$info" | jq -e '
    [.result.process_info.foreground_processes[]?.argv[]?]
    | any(. == "let g:herdr_lazyvim_plugin=1")
  ' >/dev/null 2>&1; then
    existing+=("$pane")
    existing_count=$((existing_count + 1))
  fi
done < <(printf '%s' "$panes" | jq -r '.result.panes[].pane_id // empty')

primary=""
if [ "$existing_count" -gt 0 ]; then
  primary="${existing[1]}"
  for pane in "${existing[@]}"; do
    if [ "$pane" = "$focused" ]; then
      primary="$pane"
      break
    fi
  done
fi

if [ "$mode" = close ]; then
  for pane in "${existing[@]}"; do
    [ -n "$pane" ] || continue
    "$H" pane close "$pane" >/dev/null
  done
  if [ "$existing_count" -gt 0 ]; then
    printf 'closed LazyVim in %s\n' "$workspace"
  fi
  exit 0
fi

if [ -n "$primary" ] && [ "$deduplicate_existing" = true ]; then
  for pane in "${existing[@]}"; do
    [ -n "$pane" ] || continue
    if [ "$pane" != "$primary" ]; then
      "$H" pane close "$pane" >/dev/null
    fi
  done
fi

close_existing() {
  [ -n "$primary" ] || return 0
  "$H" pane close "$primary" >/dev/null
  printf 'closed LazyVim in %s\n' "$workspace"
}

existing_tab=""
existing_tab_count=0
if [ -n "$primary" ]; then
  existing_tab=$(printf '%s' "$panes" | jq -r --arg pane "$primary" '
    first(.result.panes[] | select(.pane_id == $pane) | .tab_id) // empty
  ')
  existing_tab_count=$(printf '%s' "$panes" | jq -r --arg tab "$existing_tab" '
    [.result.panes[] | select(.tab_id == $tab)] | length
  ')
fi

# Prefer the focused agent, then an agent in the active tab when configured.
agent=$(printf '%s' "$panes" | jq -r --arg focused "$focused" '
  first(.result.panes[] | select(.pane_id == $focused and (.agent // "") != "") | .pane_id) // empty
')
if [ -z "$agent" ] && [ "$prefer_agent_in_tab" = true ] && [ -n "$tab" ]; then
  agent=$(printf '%s' "$panes" | jq -r --arg tab "$tab" '
    first(.result.panes[] | select(.tab_id == $tab and (.agent // "") != "") | .pane_id) // empty
  ')
fi

target="$focused"
placement="$terminal_placement"
if [ -n "$agent" ]; then
  target="$agent"
  placement="$agent_placement"
fi
if [ -z "$target" ] || [ "$target" = "$primary" ]; then
  target=$(printf '%s' "$panes" | jq -r --arg tab "$tab" --arg primary "$primary" '
    first(.result.panes[] | select(.tab_id == $tab and .pane_id != $primary) | .pane_id)
      // first(.result.panes[] | select(.pane_id != $primary) | .pane_id)
      // empty
  ')
fi

focus_existing() {
  if "$H" plugin pane focus "$primary" >/dev/null 2>&1; then
    printf 'resumed LazyVim in %s\n' "$workspace"
    return 0
  fi

  # The process marker survives a server restart, while the plugin registry may
  # not. Returning to its tab is the safe fallback and retains Neovim state.
  if [ -n "$existing_tab" ]; then
    "$H" tab focus "$existing_tab" >/dev/null
    printf 'resumed LazyVim tab in %s\n' "$workspace"
    return 0
  fi
  printf 'Herdr LazyVim: found the editor but could not focus it\n' >&2
  return 1
}

hide_existing() {
  # Moving the pane keeps Neovim, its buffers, and its LSP clients alive while
  # removing the editor split from the active tab.
  if [ "$existing_tab_count" -le 1 ]; then
    printf 'Herdr LazyVim: cannot hide an editor that is the tab\047s only pane\n' >&2
    return 1
  fi
  move_result=$("$H" pane move "$primary" \
    --new-tab \
    --workspace "$workspace" \
    --label "$hide_tab_label" \
    --no-focus)
  moved_pane=$(printf '%s' "$move_result" | jq -r '.result.move_result.pane.pane_id // empty')
  [ -z "$moved_pane" ] || primary="$moved_pane"
  if [ "$resize_workaround" = nudge ] && [ -n "$target" ]; then
    # Removing a split can leave the remaining terminal with its old PTY size.
    # A zoom round-trip forces Herdr to publish the full-pane dimensions.
    "$SLEEP" "$resize_settle_delay"
    "$H" pane zoom "$target" --on >/dev/null 2>&1 || true
    "$H" pane zoom "$target" --off >/dev/null 2>&1 || true
  fi
  printf 'hid LazyVim in %s\n' "$workspace"
}

restore_existing() {
  [ -n "$target" ] || {
    printf 'Herdr LazyVim: no pane to restore beside\n' >&2
    return 1
  }
  case "$placement" in
    split)
      move_result=$("$H" pane move "$primary" \
        --tab "$tab" \
        --split "$direction" \
        --target-pane "$target" \
        --focus)
      moved_pane=$(printf '%s' "$move_result" | jq -r '.result.move_result.pane.pane_id // empty')
      [ -z "$moved_pane" ] || primary="$moved_pane"
      if [ "$resize_workaround" = nudge ]; then
        # Herdr applies the new split tree asynchronously. Wait until that
        # layout has settled before generating the follow-up PTY resize event.
        "$SLEEP" "$resize_settle_delay"
        if [ "$direction" = right ]; then
          "$H" pane resize --pane "$primary" --direction left --amount 0.01 >/dev/null
          "$H" pane resize --pane "$primary" --direction right --amount 0.01 >/dev/null
        else
          "$H" pane resize --pane "$primary" --direction up --amount 0.01 >/dev/null
          "$H" pane resize --pane "$primary" --direction down --amount 0.01 >/dev/null
        fi
      fi
      ;;
    zoomed)
      move_result=$("$H" pane move "$primary" \
        --tab "$tab" \
        --split "$direction" \
        --target-pane "$target" \
        --focus)
      moved_pane=$(printf '%s' "$move_result" | jq -r '.result.move_result.pane.pane_id // empty')
      [ -z "$moved_pane" ] || primary="$moved_pane"
      "$H" pane zoom "$primary" --on >/dev/null
      ;;
    tab | overlay)
      focus_existing
      return
      ;;
  esac
  printf 'restored LazyVim (%s) in %s\n' "$placement" "$workspace"
}

if [ -n "$primary" ] && [ "$reuse_existing" = true ]; then
  if [ "$mode" = toggle ] && [ "$primary" = "$focused" ]; then
    case "$toggle_behavior" in
      hide) hide_existing ;;
      close) close_existing ;;
      open_only) focus_existing ;;
    esac
  elif [ "$existing_tab" != "$tab" ] && [ "$existing_tab_count" -eq 1 ]; then
    restore_existing
  else
    focus_existing
  fi
  exit 0
fi

if [ -n "$primary" ]; then
  close_existing
  primary=""
fi

if [ -z "$target" ]; then
  target=$(printf '%s' "$panes" | jq -r '.result.panes[0].pane_id // empty')
fi
[ -n "$target" ] || {
  printf 'Herdr LazyVim: workspace has no pane to attach to\n' >&2
  exit 1
}

cwd=$(printf '%s' "$panes" | jq -r --arg pane "$target" '
  first(.result.panes[] | select(.pane_id == $pane) | .foreground_cwd // .cwd // empty) // empty
')
[ -n "$cwd" ] || cwd=$(printf '%s' "$context" | jq -r '.focused_pane_cwd // .workspace_cwd // empty')
[ -n "$cwd" ] || cwd="$HOME"

case "$placement" in
  split) set -- --placement split --target-pane "$target" --direction "$direction" ;;
  zoomed) set -- --placement zoomed --target-pane "$target" ;;
  tab) set -- --placement tab --workspace "$workspace" ;;
  overlay) set -- --placement overlay ;;
esac

"$H" plugin pane open \
  --plugin "$plugin_id" \
  --entrypoint editor \
  "$@" \
  --cwd "$cwd" \
  --focus >/dev/null

printf 'opened LazyVim (%s) in %s\n' "$placement" "$workspace"
