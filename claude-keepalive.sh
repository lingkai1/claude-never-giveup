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

# ---------- 4. 配置 ----------
valid_session_name() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]; }

load_global_conf() {
  [[ -f "$CONF_FILE" ]]     && . "$CONF_FILE"
  [[ -f "$PATTERNS_FILE" ]] && . "$PATTERNS_FILE"
  return 0
}

# load_monitor_conf <path> — rc 0 成功；设置 M_* 五个变量
load_monitor_conf() {
  local path="$1" kv k v
  M_SESSION_NAME=""; M_MESSAGE="$DEFAULT_MESSAGE"; M_INTERVAL=$DEFAULT_INTERVAL
  M_ENABLED=1; M_DRY_RUN=0
  [[ -f "$path" ]] || return 1
  while IFS= read -r kv; do
    [[ -z "$kv" || "$kv" == \#* ]] && continue
    k="${kv%%=*}"; v="${kv#*=}"
    case "$k" in
      session_name) M_SESSION_NAME="$v" ;;
      message)      M_MESSAGE="$v" ;;
      interval)     [[ "$v" =~ ^[0-9]+$ ]] && M_INTERVAL=$v ;;
      enabled)      [[ "$v" == "0" ]] && M_ENABLED=0 || M_ENABLED=1 ;;
      dry_run)      [[ "$v" == "1" ]] && M_DRY_RUN=1 || M_DRY_RUN=0 ;;
    esac
  done < "$path"
  valid_session_name "$M_SESSION_NAME" || return 1
  (( M_INTERVAL >= 10 )) || M_INTERVAL=10
  return 0
}

write_monitor_conf() { # <path> <session_name> <message> <interval> <enabled> <dry_run>
  atomic_write "$1" "session_name=$2
message=$3
interval=$4
enabled=$5
dry_run=$6"
}

# ---------- 5. 状态 ----------
state_default() {
  ST_LAST_STATE="NEVER"; ST_LAST_CHECK=0; ST_LAST_INJECT=0
  ST_AWAITING=0; ST_CONSEC_FAIL=0
}

read_state() { # <name>
  state_default
  local f="$STATE_DIR/$1.state"
  [[ -f "$f" ]] && eval "$(grep -E '^ST_[A-Z_]+=' "$f" 2>/dev/null)"
  return 0
}

write_state() { # <name>
  atomic_write "$STATE_DIR/$1.state" "ST_LAST_STATE=$ST_LAST_STATE
ST_LAST_CHECK=$ST_LAST_CHECK
ST_LAST_INJECT=$ST_LAST_INJECT
ST_AWAITING=$ST_AWAITING
ST_CONSEC_FAIL=$ST_CONSEC_FAIL"
}

# ---------- 6. 状态分类与注入决策（纯函数） ----------
classify_tail() { # <contents text> — echo BUSY|DIALOG|IDLE|UNKNOWN
  local text="$1"
  local tail40 tail12 tail8 p line nonempty last6
  tail40=$(printf '%s\n' "$text" | strip_ansi | tail -n 40)
  tail12=$(printf '%s\n' "$tail40" | tail -n 12)
  tail8=$(printf '%s\n'  "$tail12" | tail -n 8)
  for p in ${BUSY_PATTERNS[@]+"${BUSY_PATTERNS[@]}"}; do
    printf '%s\n' "$tail8" | grep -qE -- "$p" && { echo BUSY; return 0; }
  done
  for p in ${DIALOG_PATTERNS[@]+"${DIALOG_PATTERNS[@]}"}; do
    printf '%s\n' "$tail12" | grep -qE -- "$p" && { echo DIALOG; return 0; }
  done
  nonempty=$(printf '%s\n' "$tail12" | grep -v '^[[:space:]]*$')
  last6=$(printf '%s\n' "$nonempty" | tail -n 6)
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ "$line" == "❯" ]] && { echo IDLE; return 0; }
  done <<< "$last6"
  echo UNKNOWN
  return 0
}

# 只在 classify_tail == IDLE 时调用
decide_action() { # <awaiting> <consec_fail> <content> <message>
  local awaiting="$1" consec="$2" content="$3" message="$4"
  if (( awaiting == 0 )); then echo INJECT_FIRST; return 0; fi
  if printf '%s\n' "$content" | strip_ansi | tail -n 100 | grep -qF -- "> $message"; then
    echo INJECT_AGAIN; return 0
  fi
  if (( consec + 1 >= BREAKER_LIMIT )); then echo TRIP; return 0; fi
  echo INJECT_RETRY
  return 0
}

# ---------- 7. ps 解析与定位 ----------
ps_claude_lines() {
  ps ax -o pid=,tty=,args= | grep -i 'claude' | grep -vE 'keepalive|grep' || true
}

ps_tty_of_line()  { printf '%s\n' "$1" | awk '{print $2}'; }
ps_args_of_line() { printf '%s\n' "$1" | awk '{ $1=""; $2=""; sub(/^ +/, ""); print }'; }

match_ps_line_for_session() { # <ps_line> <session_name> — rc 0 且 echo tty
  local line="$1" name="$2" tty args
  tty=$(ps_tty_of_line "$line")
  [[ -z "$tty" || "$tty" == "?" || "$tty" == "??" ]] && return 1
  args=$(ps_args_of_line "$line")
  case " $args " in
    *" -n $name "*|*" --name $name "*|*" --name=$name "*|*" --resume $name "*|*" -r $name "*)
      echo "$tty"; return 0 ;;
  esac
  return 1
}

extract_session_name_from_args() { # <args>
  printf '%s\n' "$1" | awk '{
    for (i = 1; i <= NF; i++) {
      if ($i == "-n" || $i == "--name" || $i == "--resume" || $i == "-r") { print $(i+1); exit }
      if ($i ~ /^--name=/) { sub(/^--name=/, "", $i); print $i; exit }
    }
  }'
}

find_claude_tty_for_session() { # <session_name> — rc0 echo /dev/ttysN | rc1 无 | rc2 歧义
  local name="$1" line tty found="" count=0
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if tty=$(match_ps_line_for_session "$line" "$name"); then
      count=$((count + 1)); found="$tty"
    fi
  done < <(ps_claude_lines)
  (( count == 1 )) && { echo "/dev/$found"; return 0; }
  (( count > 1 ))  && return 2
  return 1
}

scan_running_claude_sessions() { # 每行 "name<TAB>tty"
  local line args nm tty
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    tty=$(ps_tty_of_line "$line")
    [[ -z "$tty" || "$tty" == "?" || "$tty" == "??" ]] && continue
    args=$(ps_args_of_line "$line")
    nm=$(extract_session_name_from_args "$args")
    [[ -n "$nm" ]] && valid_session_name "$nm" && printf '%s\t%s\n' "$nm" "$tty"
  done < <(ps_claude_lines)
}

# ---------- 8. iTerm2 AppleScript ----------
run_osascript() { # <script> [args...] — 设 OS_OUT/OS_ERR/OS_RC
  local script="$1"; shift
  local errtmp; errtmp=$(mktemp)
  OS_OUT=$(/usr/bin/osascript -e "$script" "$@" 2>"$errtmp"); OS_RC=$?
  OS_ERR=$(<"$errtmp"); rm -f "$errtmp"
  return 0
}

iterm_read_contents() { # <devtty> — rc 0 ok(REPLY_CONTENTS) / 1 notfound / 2 error
  local script='
on run {ttyPath}
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s as text) is ttyPath then
            return contents of s
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "KEEPALIVE_NOTFOUND"
end run'
  run_osascript "$script" "$1"
  (( OS_RC != 0 )) && return 2
  [[ "$OS_OUT" == "KEEPALIVE_NOTFOUND" ]] && return 1
  REPLY_CONTENTS="$OS_OUT"
  return 0
}

iterm_write_text() { # <devtty> <message> — rc 0 ok / 1 notfound / 2 error
  local script='
on run {ttyPath, msg}
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s as text) is ttyPath then
            tell s to write text msg
            return "OK"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "KEEPALIVE_NOTFOUND"
end run'
  run_osascript "$script" "$1" "$2"
  (( OS_RC != 0 )) && return 2
  [[ "$OS_OUT" == "KEEPALIVE_NOTFOUND" ]] && return 1
  return 0
}

iterm_scan_tty_by_name() { # <session_name> — rc 0 设 SCAN_COUNT/SCAN_LIST / rc 2 error
  local script='
on run {sessName}
  tell application "iTerm2"
    set output to ""
    set n to 0
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (contents of s) contains sessName then
            set n to n + 1
            set output to output & (tty of s as text) & linefeed
          end if
        end repeat
      end repeat
    end repeat
    if n is 0 then return "0"
    return (n as text) & linefeed & output
  end tell
end run'
  run_osascript "$script" "$1"
  (( OS_RC != 0 )) && return 2
  SCAN_COUNT=$(printf '%s\n' "$OS_OUT" | head -n 1)
  SCAN_LIST=$(printf '%s\n' "$OS_OUT" | tail -n +2)
  return 0
}

# ---------- 9. 单监控检查管线 ----------
touch_state() { # <name> <state> — 非运行态记录并落盘
  ST_LAST_STATE="$2"; ST_LAST_CHECK=$(now)
  write_state "$1"
  log INFO "$1" "state=$2"
}

do_inject() { # <name> <devtty> <message>（依赖 M_DRY_RUN）
  if (( M_DRY_RUN == 1 )); then
    log INFO "$1" "[dry-run] 本应注入「$3」"
    ST_AWAITING=1; ST_LAST_INJECT=$(now)
    return 0
  fi
  if iterm_write_text "$2" "$3"; then
    ST_AWAITING=1; ST_LAST_INJECT=$(now)
    log INFO "$1" "已注入「$3」"
  else
    log ERROR "$1" "注入失败: $OS_ERR"
  fi
  return 0
}

check_monitor() { # <name>
  local name="$1"
  local conf="$MON_DIR/$name.conf"
  load_monitor_conf "$conf" || { log ERROR "$name" "conf 无效，跳过"; return 0; }
  (( M_ENABLED == 1 )) || return 0
  read_state "$name"

  local devtty rc contents state decision
  devtty=$(find_claude_tty_for_session "$M_SESSION_NAME"); rc=$?
  if (( rc == 1 )); then touch_state "$name" "NO_SESSION";   return 0; fi
  if (( rc == 2 )); then
    log ERROR "$name" "多个进程匹配 session_name=$M_SESSION_NAME，跳过"
    touch_state "$name" "AMBIGUOUS"; return 0
  fi

  if ! iterm_read_contents "$devtty"; then
    rc=$?
    if (( rc == 2 )); then
      log ERROR "$name" "osascript 失败: $OS_ERR"
      case "$OS_ERR" in
        *-1743*|*"not authorized"*|*"Not authorized"*)
          log ERROR "$name" "需授权：系统设置 → 隐私与安全性 → 自动化 → 允许控制 iTerm2" ;;
      esac
      touch_state "$name" "NO_ITERM"; return 0
    fi
    # tty 匹配不到 session → 按会话名扫内容（覆盖 /rename 情况）
    if ! iterm_scan_tty_by_name "$M_SESSION_NAME"; then
      touch_state "$name" "NO_ITERM"; return 0
    fi
    if   (( SCAN_COUNT == 0 )); then touch_state "$name" "NO_SESSION"; return 0
    elif (( SCAN_COUNT > 1 ));  then
      log ERROR "$name" "按会话名扫描命中 $SCAN_COUNT 个 iTerm2 session，跳过"
      touch_state "$name" "AMBIGUOUS"; return 0
    fi
    devtty=$(printf '%s\n' "$SCAN_LIST" | head -n 1)
    devtty="/dev/${devtty#/dev/}"
    iterm_read_contents "$devtty" || { touch_state "$name" "SESSION_NOT_FOUND"; return 0; }
  fi
  contents="$REPLY_CONTENTS"

  state=$(classify_tail "$contents")
  ST_LAST_CHECK=$(now); ST_LAST_STATE="$state"

  if [[ "$state" == "BUSY" ]]; then
    ST_AWAITING=0; ST_CONSEC_FAIL=0
    log INFO "$name" "state=BUSY（注入已生效，计数清零）"
  elif [[ "$state" == "IDLE" ]]; then
    decision=$(decide_action "$ST_AWAITING" "$ST_CONSEC_FAIL" "$contents" "$M_MESSAGE")
    case "$decision" in
      INJECT_FIRST|INJECT_AGAIN)
        [[ "$decision" == "INJECT_AGAIN" ]] && ST_CONSEC_FAIL=0
        do_inject "$name" "$devtty" "$M_MESSAGE" ;;
      INJECT_RETRY)
        ST_CONSEC_FAIL=$((ST_CONSEC_FAIL + 1))
        log WARN "$name" "上次注入未见生效（第 $ST_CONSEC_FAIL/$BREAKER_LIMIT 次）"
        do_inject "$name" "$devtty" "$M_MESSAGE" ;;
      TRIP)
        ST_CONSEC_FAIL=$((ST_CONSEC_FAIL + 1))
        log ERROR "$name" "连续 $ST_CONSEC_FAIL 次注入未生效，熔断：自动禁用该监控"
        M_ENABLED=0
        write_monitor_conf "$conf" "$M_SESSION_NAME" "$M_MESSAGE" "$M_INTERVAL" 0 "$M_DRY_RUN" ;;
    esac
  else
    log INFO "$name" "state=$state，不打扰"
  fi
  write_state "$name"
  return 0
}

# ---------- 14. 入口 ----------
if [[ "${KEEPALIVE_TEST_MODE:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
