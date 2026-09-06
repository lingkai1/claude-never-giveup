#!/usr/bin/env bash
# claude-keepalive — 让指定名字的 Claude Code 会话（iTerm2）持续工作
# 设计文档: docs/specs/2026-09-06-claude-keepalive-design.md
set -u

VERSION="1.0.0"
SCRIPT_PATH=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

# ---------- 1. 路径与默认值 ----------
BASE_DIR="${KEEPALIVE_BASE_DIR:-$HOME/.claude-keepalive}"
MON_DIR="$BASE_DIR/monitors"
STATE_DIR="$BASE_DIR/state"
LOG_FILE="$BASE_DIR/keepalive.log"
CONF_FILE="$BASE_DIR/keepalive.conf"
PATTERNS_FILE="$BASE_DIR/patterns.conf"
STOP_FILE="$BASE_DIR/stop"
PID_FILE="$BASE_DIR/daemon.pid"
DAEMON_OUT="$BASE_DIR/daemon.out"
LAUNCHD_LABEL="com.lingkai.claude-keepalive"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"

TICK_SECONDS=15
BREAKER_LIMIT=3
LOG_MAX_BYTES=5242880
DEFAULT_INTERVAL=300
DEFAULT_MESSAGE="继续"

BUSY_PATTERNS=(
  '[Ee]sc to interrupt'
  '· *[Rr]etrying'
  '[Rr]econnecting'
)
DIALOG_PATTERNS=(
  'Yes, and auto-accept'
  'No, and tell Claude'
  'Do you want to'
  '[Ww]ould you like to'
  'Enter to confirm'
  'Choose an option'
  '❯ *[0-9]+\.'
)

# ---------- 2. 通用 helpers ----------
now() { date +%s; }
ts()  { date '+%Y-%m-%d %H:%M:%S'; }

ensure_dirs() { mkdir -p "$MON_DIR" "$STATE_DIR"; }

atomic_write() { # atomic_write <path> <content>
  local path="$1" tmp
  tmp="${path}.tmp.$$"
  printf '%s\n' "$2" > "$tmp" && mv -f "$tmp" "$path"
}

strip_ansi() { # stdin -> stdin
  perl -pe 's/\e\[[0-9;:?=><]*[A-Za-z]//g; s/\e\][^\a]*(\a|\e\\)//g; s/\e[()][0-9A-Za-z]//g'
}

log() { # log <LEVEL> <monitor> <message...>
  local level="$1" mon="$2"; shift 2
  ensure_dirs
  local size=0
  [[ -f "$LOG_FILE" ]] && size=$(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
  if (( size > LOG_MAX_BYTES )); then mv -f "$LOG_FILE" "$LOG_FILE.1"; fi
  printf '[%s] [%s] [%s] %s\n' "$(ts)" "$level" "$mon" "$*" >> "$LOG_FILE"
}

# ---------- 14. 入口 ----------
if [[ "${KEEPALIVE_TEST_MODE:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
