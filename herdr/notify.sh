#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

plugin_root="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=config.sh
source "$plugin_root/herdr/config.sh"

[ "$(config_bool actionable_notifications false)" = true ] || exit 0
[ "${HERDR_LAZYVIM_PLATFORM:-$(uname -s)}" = Darwin ] || exit 0

event="${HERDR_PLUGIN_EVENT_JSON:-}"
[ -n "$event" ] || exit 0
status=$(printf '%s' "$event" | jq -r '.data.agent_status // empty')
case "$status" in
  blocked) suffix="needs input" ;;
  done) suffix="finished" ;;
  *) exit 0 ;;
esac

pane=$(printf '%s' "$event" | jq -r '.data.pane_id // empty')
workspace=$(printf '%s' "$event" | jq -r '.data.workspace_id // empty')
[ -n "$pane" ] && [ -n "$workspace" ] || exit 0

H="${HERDR_BIN_PATH:-herdr}"
pane_json=$("$H" pane get "$pane" 2>/dev/null || true)
tab=$(printf '%s' "$pane_json" | jq -r '.result.pane.tab_id // empty' 2>/dev/null)
workspace_json=$("$H" workspace get "$workspace" 2>/dev/null || true)

# Match Herdr's policy: an agent in the tab already on screen needs no popup.
workspace_focused=$(printf '%s' "$workspace_json" | jq -r '.result.workspace.focused // false' 2>/dev/null)
active_tab=$(printf '%s' "$workspace_json" | jq -r '.result.workspace.active_tab_id // empty' 2>/dev/null)
if [ "$workspace_focused" = true ] && [ -n "$tab" ] && [ "$tab" = "$active_tab" ]; then
  exit 0
fi

notifier="${TERMINAL_NOTIFIER_BIN:-}"
[ -n "$notifier" ] || notifier=$(command -v terminal-notifier 2>/dev/null || true)
[ -n "$notifier" ] || exit 0

agent=$(printf '%s' "$event" | jq -r '.data.display_agent // .data.agent // "Agent"')
detail=$(printf '%s' "$event" | jq -r '.data.title // empty')
workspace_label=$(printf '%s' "$workspace_json" | jq -r '.result.workspace.label // empty' 2>/dev/null)
[ -n "$detail" ] || detail="${workspace_label:-$workspace}"

focus_app=$(config_string notification_app "Alacritty")
sound=$(config_string notification_sound "default")
focus_script="$plugin_root/herdr/notification-focus.sh"
printf -v click_command '%q %q %q %q %q' \
  "$focus_script" \
  "${HERDR_SOCKET_PATH:-}" \
  "$H" \
  "$pane" \
  "$focus_app"

set -- \
  -title "$agent $suffix" \
  -message "$detail" \
  -group "herdr-$pane" \
  -execute "$click_command"
if [ "$sound" != none ] && [ -n "$sound" ]; then
  set -- "$@" -sound "$sound"
fi
"$notifier" "$@"
