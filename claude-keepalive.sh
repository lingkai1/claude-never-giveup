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
  # 匹配 iTerm2 session 标题（Claude Code 会把会话名写进终端标题，/rename 后同步更新）
  local script='
on run {sessName}
  tell application "iTerm2"
    set output to ""
    set n to 0
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (name of s) contains sessName then
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
  if (( rc == 2 )); then
    log ERROR "$name" "多个进程匹配 session_name=${M_SESSION_NAME}，跳过"
    touch_state "$name" "AMBIGUOUS"; return 0
  fi
  if (( rc == 1 )); then
    # ps 无匹配（未用 -n 启动等）→ 按 iTerm2 会话标题兜底定位
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
    if ! iterm_read_contents "$devtty"; then
      touch_state "$name" "SESSION_NOT_FOUND"; return 0
    fi
    contents="$REPLY_CONTENTS"
  else
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
      # ps 找到进程但 tty 匹配不到 iTerm2 session（别的终端 App 或已退出）
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
      if ! iterm_read_contents "$devtty"; then
        touch_state "$name" "SESSION_NOT_FOUND"; return 0
      fi
    fi
    contents="$REPLY_CONTENTS"
  fi

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
    log INFO "$name" "state=${state}，不打扰"
  fi
  write_state "$name"
  return 0
}

# ---------- 10. daemon ----------
daemon_pid() {
  local p; p=$(cat "$PID_FILE" 2>/dev/null)
  [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && echo "$p"
  return 0
}

daemon_exit() { rm -f "$PID_FILE"; log INFO daemon "退出"; exit 0; }

daemon_run() {
  ensure_dirs
  load_global_conf
  if [[ -e "$STOP_FILE" ]]; then log INFO daemon "启动时存在 stop 文件，退出"; exit 0; fi
  if ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
    :
  else
    local old; old=$(cat "$PID_FILE" 2>/dev/null)
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      log ERROR daemon "已有实例 pid=${old}，退出"; exit 1
    fi
    echo "$$" > "$PID_FILE"
  fi
  trap daemon_exit INT TERM
  trap 'rm -f "$PID_FILE"' EXIT
  log INFO daemon "启动（pid $$，tick=${TICK_SECONDS}s）"
  local conf name slept
  while :; do
    [[ -e "$STOP_FILE" ]] && { log INFO daemon "检测到 stop 文件，退出"; exit 0; }
    for conf in "$MON_DIR"/*.conf; do
      [[ -e "$conf" ]] || continue
      name="${conf##*/}"; name="${name%.conf}"
      if load_monitor_conf "$conf"; then
        if (( M_ENABLED == 1 )); then
          read_state "$name"
          if (( $(now) - ST_LAST_CHECK >= M_INTERVAL )); then
            check_monitor "$name" || log ERROR "$name" "检查异常"
          fi
        fi
      else
        log ERROR "$name" "conf 无效：$conf"
      fi
    done
    # 1 秒切片睡眠：TERM 信号与 stop 文件的响应延迟不超过 1s
    slept=0
    while (( slept < TICK_SECONDS )); do
      [[ -e "$STOP_FILE" ]] && { log INFO daemon "检测到 stop 文件，退出"; exit 0; }
      sleep 1
      slept=$((slept + 1))
    done
  done
}

# ---------- 11. 命令 ----------
cmd_start() {
  ensure_dirs
  rm -f "$STOP_FILE"
  local p; p=$(daemon_pid)
  if [[ -n "$p" ]]; then echo "daemon 已在运行 (pid $p)"; return 0; fi
  if launchd_loaded; then
    launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" && echo "已通过 launchd 拉起"
  else
    nohup /bin/bash "$SCRIPT_PATH" daemon >>"$DAEMON_OUT" 2>&1 &
    echo "daemon 已后台启动 (pid $!)"
  fi
  return 0
}

cmd_stop() {
  ensure_dirs
  touch "$STOP_FILE"
  local p; p=$(daemon_pid)
  if [[ -n "$p" ]]; then
    kill "$p" 2>/dev/null
    echo "已通知 daemon (pid $p) 退出"
  else
    echo "daemon 未在运行（stop 文件已放置）"
  fi
  return 0
}

cmd_status() {
  ensure_dirs
  printf '%-18s %-8s %-15s %-20s %-20s %s\n' NAME ENABLED STATE LAST_CHECK LAST_INJECT FAILS
  local conf name lc li
  for conf in "$MON_DIR"/*.conf; do
    [[ -e "$conf" ]] || continue
    name="${conf##*/}"; name="${name%.conf}"
    if ! load_monitor_conf "$conf"; then
      printf '%-18s %-8s %s\n' "$name" "-" "CONF_INVALID"; continue
    fi
    read_state "$name"
    if (( ST_LAST_CHECK  > 0 )); then lc=$(date -r "$ST_LAST_CHECK"  '+%m-%d %H:%M:%S'); else lc="-"; fi
    if (( ST_LAST_INJECT > 0 )); then li=$(date -r "$ST_LAST_INJECT" '+%m-%d %H:%M:%S'); else li="-"; fi
    printf '%-18s %-8s %-15s %-20s %-20s %s\n' \
      "$M_SESSION_NAME" "$M_ENABLED" "$ST_LAST_STATE" "$lc" "$li" "$ST_CONSEC_FAIL"
  done
  local p; p=$(daemon_pid)
  if [[ -n "$p" ]]; then echo "daemon: running (pid $p)"; else echo "daemon: stopped"; fi
  return 0
}

cmd_once() {
  load_global_conf
  if [[ -n $(daemon_pid) ]]; then
    echo "警告：daemon 正在运行，状态文件并发写入（原子写，风险低）"
  fi
  local conf name any=0
  for conf in "$MON_DIR"/*.conf; do
    [[ -e "$conf" ]] || continue
    any=1; name="${conf##*/}"; name="${name%.conf}"
    if ! load_monitor_conf "$conf"; then echo "[skip] $name: conf 无效"; continue; fi
    (( M_ENABLED == 1 )) || { echo "[skip] $name: disabled"; continue; }
    echo "== 检查 ${name}（session=${M_SESSION_NAME}）"
    check_monitor "$name"
  done
  (( any == 0 )) && echo "没有监控配置：$MON_DIR 下放 .conf，或运行 setup"
  return 0
}

# ---------- 12. launchd ----------
launchd_loaded() { launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; }

cmd_install() {
  ensure_dirs
  # launchd 进程无 ~/Desktop 等 TCC 目录的访问权，部署一份到家目录再由 plist 指向它
  local deploy_dir="$BASE_DIR/bin"
  local deploy="$deploy_dir/claude-keepalive.sh"
  mkdir -p "$deploy_dir"
  cp -f "$SCRIPT_PATH" "$deploy" && chmod +x "$deploy"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$deploy</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$DAEMON_OUT</string>
  <key>StandardErrorPath</key><string>$DAEMON_OUT</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null
  if launchctl bootstrap "gui/$(id -u)" "$LAUNCHD_PLIST" 2>/dev/null || launchctl load -w "$LAUNCHD_PLIST" 2>/dev/null; then
    echo "已安装并加载 LaunchAgent：$LAUNCHD_PLIST"
    echo "脚本已部署到 ${deploy}（改主脚本后需重跑 install）"
  else
    echo "plist 已写入但加载失败，请手动执行：launchctl load $LAUNCHD_PLIST"
  fi
  return 0
}

cmd_uninstall() {
  launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null
  launchctl unload -w "$LAUNCHD_PLIST" 2>/dev/null
  rm -f "$LAUNCHD_PLIST"
  echo "已卸载 LaunchAgent"
  return 0
}

# ---------- 12.5 TUI ----------
tui_bold() { printf '\033[1m%s\033[0m\n' "$*"; }

tui_ask() { # <var> <default> <prompt> — 回车保留默认
  local __var="$1" __def="$2" __p="$3" __ans
  read -r -p "$__p [$__def]: " __ans
  [[ -z "$__ans" ]] && __ans="$__def"
  printf -v "$__var" '%s' "$__ans"
}

tui_pick_monitor() { # echo 选中 name；rc 1 取消
  local conf name i=1
  local arr=()
  for conf in "$MON_DIR"/*.conf; do
    [[ -e "$conf" ]] || continue
    name="${conf##*/}"; name="${name%.conf}"
    printf '  %d) %s\n' "$i" "$name"
    arr[$i]="$name"; i=$((i+1))
  done
  if [[ ${#arr[@]} -eq 0 ]]; then echo "（暂无监控配置）" >&2; return 1; fi
  local pick
  tui_ask pick 1 "选择编号"
  if [[ -z "${arr[$pick]:-}" ]]; then echo "无效编号" >&2; return 1; fi
  echo "${arr[$pick]}"
  return 0
}

tui_live_list() {
  local ans
  while :; do
    printf '\033[2J\033[H'
    tui_bold "== 监控列表（5s 刷新，q 返回） =="
    cmd_status
    printf '\n(q 返回) '
    if read -r -t 5 -n 1 ans; then
      [[ "$ans" == "q" ]] && break
    fi
  done
  echo
  return 0
}

tui_add_monitor() {
  echo; tui_bold "== 添加监控 =="
  echo "扫描运行中的 claude 会话..."
  local name tty i=1 pick session_name
  local names=()
  while IFS=$'\t' read -r name tty; do
    [[ -z "$name" ]] && continue
    printf '  %d) %s  (tty %s)\n' "$i" "$name" "$tty"
    names[$i]="$name"; i=$((i+1))
  done < <(scan_running_claude_sessions | sort -u)
  printf '  %d) 手动输入会话名\n' "$i"
  names[$i]="__manual__"
  tui_ask pick "$i" "选择"
  if [[ "${names[$pick]:-}" == "__manual__" ]]; then
    read -r -p "claude -n 的会话名: " session_name
  else
    session_name="${names[$pick]:-}"
  fi
  if ! valid_session_name "$session_name"; then
    echo "会话名只能含 [A-Za-z0-9._-]，已取消"; return 1
  fi
  local conf="$MON_DIR/$session_name.conf"
  if [[ -f "$conf" ]]; then
    local yn; read -r -p "监控 $session_name 已存在，覆盖? [y/N]: " yn
    [[ "$yn" == y* ]] || { echo "已取消"; return 0; }
  fi
  local message interval dry
  tui_ask message  "$DEFAULT_MESSAGE"  "注入词"
  tui_ask interval "$DEFAULT_INTERVAL" "检测间隔（秒）"
  tui_ask dry      "n"                 "dry_run 模式 [y/n]"
  [[ "$dry" == y* ]] && dry=1 || dry=0
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=$DEFAULT_INTERVAL
  write_monitor_conf "$conf" "$session_name" "$message" "$interval" 1 "$dry"
  echo "已写入 ${conf}（daemon 在跑则下一 tick 自动生效）"
  return 0
}

tui_edit_monitor() {
  echo; tui_bold "== 编辑 / 删除监控 =="
  local name; name=$(tui_pick_monitor) || return 0
  local conf="$MON_DIR/$name.conf"
  load_monitor_conf "$conf" || { echo "conf 无效"; return 1; }
  echo "回车 = 保留当前值"
  local v sn msg itv en dr
  tui_ask v "$M_SESSION_NAME" "session_name"; sn="$v"
  tui_ask v "$M_MESSAGE"     "注入词";         msg="$v"
  tui_ask v "$M_INTERVAL"    "间隔（秒）";     itv="$v"
  tui_ask v "$M_ENABLED"     "enabled(1/0)";   en="$v"
  tui_ask v "$M_DRY_RUN"     "dry_run(1/0)";   dr="$v"
  [[ "$itv" =~ ^[0-9]+$ ]] || itv=$DEFAULT_INTERVAL
  [[ "$en" == "1" ]] || en=0
  [[ "$dr" == "1" ]] || dr=0
  if ! valid_session_name "$sn"; then echo "会话名非法，保留原名"; sn="$M_SESSION_NAME"; fi
  write_monitor_conf "$conf" "$sn" "$msg" "$itv" "$en" "$dr"
  echo "已更新 $conf"
  local yn; read -r -p "是否删除该监控? [y/N]: " yn
  if [[ "$yn" == y* ]]; then
    rm -f "$conf" "$STATE_DIR/$sn.state"
    echo "已删除"
  fi
  return 0
}

tui_daemon_menu() {
  echo
  local p; p=$(daemon_pid)
  if [[ -n "$p" ]]; then echo "daemon: 运行中 (pid $p)"; else echo "daemon: 未运行"; fi
  echo "  1) 启动  2) 停止  3) 安装 launchd 常驻  4) 卸载 launchd  5) 返回"
  local c; read -r -p "选择: " c
  case "$c" in
    1) cmd_start ;;
    2) cmd_stop ;;
    3) cmd_install ;;
    4) cmd_uninstall ;;
  esac
  return 0
}

tui_show_logs() {
  echo
  if [[ ! -f "$LOG_FILE" ]]; then echo "暂无日志"; return 0; fi
  tail -n 30 "$LOG_FILE"
  echo
  printf '\033[2m（完整日志：%s）\033[0m\n' "$LOG_FILE"
  return 0
}

tui_main() {
  ensure_dirs
  trap 'echo; exit 130' INT
  local choice
  while :; do
    printf '\033[2J\033[H'
    tui_bold "== claude-keepalive v$VERSION =="
    echo "  1) 监控列表（实时刷新）"
    echo "  2) 添加监控"
    echo "  3) 编辑 / 删除监控"
    echo "  4) 启动 / 停止 daemon"
    echo "  5) 查看日志尾部"
    echo "  6) 退出"
    read -r -p "选择: " choice
    case "$choice" in
      1) tui_live_list ;;
      2) tui_add_monitor ;;
      3) tui_edit_monitor ;;
      4) tui_daemon_menu ;;
       5) tui_show_logs ;;
      6) break ;;
    esac
  done
  echo
  return 0
}

# ---------- 13. usage 与入口 ----------
usage() {
  cat <<EOF
claude-keepalive v$VERSION — 让指定名字的 Claude Code 会话（iTerm2）持续工作

用法: claude-keepalive.sh <命令>
  setup        TUI：添加/编辑监控、启停 daemon、看日志
  start        启动 daemon（清除 stop 文件）
  stop         停止 daemon（放置 stop 文件）
  status       非交互状态表
  once         立即检查一轮全部监控（调试）
  daemon       前台运行 daemon（launchd / 调试用）
  install      安装 launchd 常驻
  uninstall    卸载 launchd

配置目录: $BASE_DIR
全局刹车: touch $STOP_FILE   （start 会清除）
EOF
}

main() {
  load_global_conf
  case "${1:-help}" in
    setup)      tui_main ;;
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    status)     cmd_status ;;
    once)       cmd_once ;;
    daemon)     daemon_run ;;
    install)    cmd_install ;;
    uninstall)  cmd_uninstall ;;
    help|-h|--help) usage ;;
    *) usage; exit 1 ;;
  esac
}

# ---------- 14. 入口 ----------
if [[ "${KEEPALIVE_TEST_MODE:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
