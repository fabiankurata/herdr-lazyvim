#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/config"

cat >"$TMP/bin/herdr" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TEST_LOG"
case "$1 $2" in
  "pane list") cat "$TEST_PANES" ;;
  "pane process-info")
    pane="$4"
    if grep -qx "$pane" "$TEST_MARKERS" 2>/dev/null; then
      printf '{"result":{"process_info":{"foreground_processes":[{"argv":["nvim","--cmd","let g:herdr_lazyvim_plugin=1"]}]}}}\n'
    else
      printf '{"result":{"process_info":{"foreground_processes":[]}}\n'
    fi
    ;;
  "pane get")
    printf '{"result":{"pane":{"pane_id":"%s","tab_id":"ws:t2"}}}\n' "$3"
    ;;
  "workspace get")
    printf '{"result":{"workspace":{"workspace_id":"%s","label":"demo","focused":%s,"active_tab_id":"%s"}}}\n' \
      "$3" "${TEST_WORKSPACE_FOCUSED:-false}" "${TEST_ACTIVE_TAB:-ws:t1}"
    ;;
  "pane move")
    printf '{"result":{"move_result":{"pane":{"pane_id":"%s"}}}}\n' "${TEST_MOVED_PANE:-ws:p8}"
    ;;
  "plugin pane")
    if [ "$3" = focus ] && [ "${TEST_FOCUS_FAIL:-0}" = 1 ]; then exit 1; fi
    ;;
esac
FAKE
chmod +x "$TMP/bin/herdr"
cat >"$TMP/bin/terminal-notifier" <<'FAKE'
#!/usr/bin/env bash
printf 'terminal-notifier %s\n' "$*" >>"$TEST_LOG"
FAKE
cat >"$TMP/bin/open" <<'FAKE'
#!/usr/bin/env bash
printf 'open %s\n' "$*" >>"$TEST_LOG"
FAKE
cat >"$TMP/bin/sleep" <<'FAKE'
#!/usr/bin/env bash
printf 'sleep %s\n' "$*" >>"$TEST_LOG"
FAKE
chmod +x "$TMP/bin/terminal-notifier" "$TMP/bin/open" "$TMP/bin/sleep"

cat >"$TMP/panes.json" <<'JSON'
{"result":{"panes":[
  {"pane_id":"ws:p1","tab_id":"ws:t1","agent":"codex","foreground_cwd":"/project"},
  {"pane_id":"ws:p2","tab_id":"ws:t1","agent":"","foreground_cwd":"/project"},
  {"pane_id":"ws:p3","tab_id":"ws:t2","agent":"","foreground_cwd":"/project"},
  {"pane_id":"ws:p4","tab_id":"ws:t2","agent":"","foreground_cwd":"/project"}
]}}
JSON
cat >"$TMP/parked.json" <<'JSON'
{"result":{"panes":[
  {"pane_id":"ws:p1","tab_id":"ws:t1","agent":"codex","foreground_cwd":"/project"},
  {"pane_id":"ws:p2","tab_id":"ws:t1","agent":"","foreground_cwd":"/project"},
  {"pane_id":"ws:p3","tab_id":"ws:t3","agent":"","foreground_cwd":"/project"}
]}}
JSON
: >"$TMP/markers"

export TEST_LOG="$TMP/log"
DEFAULT_PANES="$TMP/panes.json"
export TEST_PANES="$DEFAULT_PANES"
export TEST_MARKERS="$TMP/markers"
export HERDR_BIN_PATH="$TMP/bin/herdr"
export HERDR_LAZYVIM_SLEEP_BIN="$TMP/bin/sleep"
export HERDR_PLUGIN_ROOT="$ROOT"
export HERDR_PLUGIN_ID="fabiankurata.lazyvim"
export HERDR_PLUGIN_CONFIG_DIR="$TMP/config"

context() {
  export HERDR_PLUGIN_CONTEXT_JSON="{\"workspace_id\":\"ws\",\"tab_id\":\"$1\",\"focused_pane_id\":\"$2\"}"
}

reset_case() {
  : >"$TEST_LOG"
  : >"$TEST_MARKERS"
  : >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
  export TEST_PANES="$DEFAULT_PANES"
  unset TEST_FOCUS_FAIL
  unset TEST_MOVED_PANE
  unset TEST_WORKSPACE_FOCUSED TEST_ACTIVE_TAB
}

assert_log() {
  if ! grep -F -- "$1" "$TEST_LOG" >/dev/null; then
    printf 'FAIL: expected log to contain: %s\n--- log ---\n' "$1" >&2
    cat "$TEST_LOG" >&2
    exit 1
  fi
}

assert_no_log() {
  if grep -F -- "$1" "$TEST_LOG" >/dev/null; then
    printf 'FAIL: expected log not to contain: %s\n--- log ---\n' "$1" >&2
    cat "$TEST_LOG" >&2
    exit 1
  fi
}

run_pane() {
  "$ROOT/herdr/pane.sh" "$1" >/dev/null
}

# Agent tabs open a split rooted at the agent's project.
reset_case
context ws:t1 ws:p2
run_pane open
assert_log "plugin pane open --plugin fabiankurata.lazyvim --entrypoint editor --placement split --target-pane ws:p1 --direction right --cwd /project --focus"

# Terminal-only tabs use a zoomed pane.
reset_case
context ws:t2 ws:p3
run_pane open
assert_log "plugin pane open --plugin fabiankurata.lazyvim --entrypoint editor --placement zoomed --target-pane ws:p3 --cwd /project --focus"

# Invoking the shortcut from another tab resumes the editor without reopening it.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
context ws:t1 ws:p2
run_pane toggle
assert_log "plugin pane focus ws:p3"
assert_no_log "pane close ws:p3"
assert_no_log "plugin pane open"

# Invoking toggle from the editor itself parks the live process in a hidden tab.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
context ws:t2 ws:p3
run_pane toggle
assert_log "pane zoom ws:p3 --off"
assert_log "pane move ws:p3 --new-tab --workspace ws --label LazyVim --no-focus"
assert_log "pane zoom ws:p4 --on"
assert_log "pane zoom ws:p4 --off"
assert_no_log "pane close ws:p3"

# A parked editor is restored beside the current agent without restarting it.
reset_case
export TEST_PANES="$TMP/parked.json"
printf 'ws:p3\n' >"$TEST_MARKERS"
context ws:t1 ws:p2
run_pane toggle
assert_log "pane move ws:p3 --tab ws:t1 --split right --target-pane ws:p1 --focus"
assert_log "sleep 0.12"
assert_log "pane resize --pane ws:p8 --direction left --amount 0.01"
assert_log "pane resize --pane ws:p8 --direction right --amount 0.01"
assert_no_log "plugin pane open"
assert_no_log "pane close ws:p3"

# Open always resumes, even when already focused.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
context ws:t2 ws:p3
run_pane open
assert_log "plugin pane focus ws:p3"
assert_no_log "pane close ws:p3"

# Close is explicit and harmless when no editor exists.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
context ws:t1 ws:p2
run_pane close
assert_log "pane close ws:p3"
reset_case
run_pane close
assert_no_log "pane close"

# Duplicate panes are repaired while retaining the focused instance.
reset_case
printf 'ws:p1\nws:p3\n' >"$TEST_MARKERS"
context ws:t2 ws:p3
run_pane open
assert_log "pane close ws:p1"
assert_log "plugin pane focus ws:p3"

# A server-restart registry miss falls back to the existing pane's tab.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
export TEST_FOCUS_FAIL=1
context ws:t1 ws:p2
run_pane open
assert_log "plugin pane focus ws:p3"
assert_log "tab focus ws:t2"

# Placement and toggle behavior are configuration-driven.
reset_case
cat >"$HERDR_PLUGIN_CONFIG_DIR/config.toml" <<'TOML'
agent_placement = "tab"
terminal_placement = "overlay"
direction = "down"
toggle_behavior = "open_only"
TOML
context ws:t1 ws:p1
run_pane open
assert_log "--placement tab --workspace ws"
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
printf 'toggle_behavior = "open_only"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t2 ws:p3
run_pane toggle
assert_log "plugin pane focus ws:p3"
assert_no_log "pane close ws:p3"

reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
printf 'toggle_behavior = "close"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t2 ws:p3
run_pane toggle
assert_log "pane close ws:p3"

# Invalid config fails before changing panes.
reset_case
printf 'direction = "left"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p1
if run_pane open 2>/dev/null; then
  printf 'FAIL: invalid direction was accepted\n' >&2
  exit 1
fi
assert_no_log "plugin pane open"

# The resize workaround can be disabled and follows vertical split direction.
reset_case
export TEST_PANES="$TMP/parked.json"
printf 'ws:p3\n' >"$TEST_MARKERS"
cat >"$HERDR_PLUGIN_CONFIG_DIR/config.toml" <<'TOML'
direction = "down"
resize_workaround = "nudge"
TOML
context ws:t1 ws:p2
run_pane toggle
assert_log "pane resize --pane ws:p8 --direction up --amount 0.01"
assert_log "pane resize --pane ws:p8 --direction down --amount 0.01"

reset_case
export TEST_PANES="$TMP/parked.json"
printf 'ws:p3\n' >"$TEST_MARKERS"
printf 'resize_workaround = "none"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p2
run_pane toggle
assert_no_log "pane resize"

reset_case
printf 'toggle_behavior = "minimize"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p1
if run_pane open 2>/dev/null; then
  printf 'FAIL: invalid toggle behavior was accepted\n' >&2
  exit 1
fi
assert_no_log "plugin pane open"

reset_case
printf 'reuse_existing = sometimes\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p1
if run_pane open 2>/dev/null; then
  printf 'FAIL: invalid boolean was accepted\n' >&2
  exit 1
fi
assert_no_log "plugin pane open"

reset_case
printf 'resize_settle_delay = "later"\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p1
if run_pane open 2>/dev/null; then
  printf 'FAIL: invalid resize settle delay was accepted\n' >&2
  exit 1
fi
assert_no_log "plugin pane open"

# Disabling reuse replaces the previous process instead of creating a duplicate.
reset_case
printf 'ws:p3\n' >"$TEST_MARKERS"
printf 'reuse_existing = false\n' >"$HERDR_PLUGIN_CONFIG_DIR/config.toml"
context ws:t1 ws:p2
run_pane open
assert_log "pane close ws:p3"
assert_log "plugin pane open"

# The launcher expands ~/ and loads the bundled review runtime.
reset_case
cat >"$TMP/bin/editor" <<'EDITOR'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$TEST_EDITOR_LOG"
EDITOR
chmod +x "$TMP/bin/editor"
cat >"$HERDR_PLUGIN_CONFIG_DIR/config.toml" <<TOML
editor = "$TMP/bin/editor"
target = "~/project"
review_enabled = true
comment_completion = false
comment_display = "virtual_line"
comment_range_style = "subtle"
comment_card_position = "below"
comment_card_width = "72"
comment_card_background = "#16161e"
comment_card_border = "#ff9e64"
comment_editor_background = "#20283a"
comment_editor_layout = "inline"
comment_editor_winblend = "40"
transparent_background = true
mode_emphasis = "cursorline"
TOML
export TEST_EDITOR_LOG="$TMP/editor-log"
"$ROOT/herdr/launch.sh"
grep -Fx "let g:herdr_lazyvim_plugin=1" "$TEST_EDITOR_LOG" >/dev/null
grep -Fx "set runtimepath^=$ROOT/nvim" "$TEST_EDITOR_LOG" >/dev/null
grep -Fx "lua package.path = '$ROOT/nvim/lua/?.lua;$ROOT/nvim/lua/?/init.lua;' .. package.path; require('herdr_lazyvim').setup({ comment_completion = false, comment_display = 'virtual_line', comment_range_style = 'subtle', comment_card_position = 'below', comment_card_width = 72, comment_card_background = '#16161e', comment_card_border = '#ff9e64', comment_editor_background = '#20283a', comment_editor_layout = 'inline', comment_editor_winblend = 40, transparent_background = true, mode_emphasis = 'cursorline' })" "$TEST_EDITOR_LOG" >/dev/null
grep -Fx "$HOME/project" "$TEST_EDITOR_LOG" >/dev/null

# Actionable notifications carry the originating pane into their click command
# and suppress events from the tab already on screen.
reset_case
cat >"$HERDR_PLUGIN_CONFIG_DIR/config.toml" <<'TOML'
actionable_notifications = true
notification_app = "Alacritty"
notification_sound = "none"
TOML
export HERDR_LAZYVIM_PLATFORM=Darwin
export TERMINAL_NOTIFIER_BIN="$TMP/bin/terminal-notifier"
export HERDR_SOCKET_PATH="$TMP/herdr.sock"
export HERDR_PLUGIN_EVENT_JSON='{"event":"pane_agent_status_changed","data":{"type":"pane_agent_status_changed","agent_status":"blocked","pane_id":"ws:p3","workspace_id":"ws","agent":"codex","title":"Review auth"}}'
"$ROOT/herdr/notify.sh"
assert_log "terminal-notifier -title codex needs input -message Review auth"
assert_log "notification-focus.sh"
assert_log "ws:p3"

reset_case
export TEST_WORKSPACE_FOCUSED=true
export TEST_ACTIVE_TAB=ws:t2
"$ROOT/herdr/notify.sh"
assert_no_log "terminal-notifier"

reset_case
export OPEN_BIN="$TMP/bin/open"
"$ROOT/herdr/notification-focus.sh" "$TMP/herdr.sock" "$TMP/bin/herdr" ws:p3 Alacritty
assert_log "agent focus ws:p3"
assert_log "open -a Alacritty"
unset HERDR_LAZYVIM_PLATFORM TERMINAL_NOTIFIER_BIN HERDR_PLUGIN_EVENT_JSON OPEN_BIN

bash -n "$ROOT/herdr/config.sh" "$ROOT/herdr/launch.sh" "$ROOT/herdr/pane.sh" "$ROOT/herdr/notify.sh" "$ROOT/herdr/notification-focus.sh"
printf 'plugin tests: ok\n'
