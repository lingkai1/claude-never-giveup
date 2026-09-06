# claude-keepalive 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个 macOS bash 守护脚本，按会话名监控多个跑在 iTerm2 里的 Claude Code 会话，检测到 idle 就注入继续词，实现「永不停止，一直干」。

**Architecture:** 单进程 daemon（15s tick）+ 每监控独立 conf/state 文件；定位链路 ps(-n 名字)→tty→iTerm2 AppleScript session；读 contents 尾部分类 BUSY/DIALOG/IDLE/UNKNOWN，仅 IDLE 注入 `write text`；marker 确认 + 3 次熔断 + stop 刹车文件；launchd 常驻；纯 bash TUI。

**Tech Stack:** bash 3.2 兼容（macOS 自带 /bin/bash）、osascript（iTerm2 AppleScript）、perl（去 ANSI）、launchd。零外部依赖。

**Spec:** `docs/specs/2026-09-06-claude-keepalive-design.md`

## Global Constraints

- bash 3.2 兼容：禁止 `declare -A`、`printf %()T`、`${var,,}`、`mapfile`。
- `set -u` 下展开可能为空的数组必须用 `${arr[@]+"${arr[@]}"}` 守卫。
- 所有 conf/state 写入走 `atomic_write`（tmp+mv）。
- 会话名仅允许 `^[A-Za-z0-9._-]+$`（无空格）。
- 默认值：interval=300、message=继续、tick=15、breaker=3、日志轮转 5MB。
- 注入只允许发生在 classify_tail 返回 IDLE 时；BUSY/DIALOG/UNKNOWN 一律不动。
- 单文件 `claude-keepalive.sh` + `test/test.sh` + `README.md`；运行时目录 `~/.claude-keepalive/`。
- 测试以 `KEEPALIVE_TEST_MODE=1` + `KEEPALIVE_BASE_DIR=$(mktemp -d)` source 主脚本。

---

### Task 1: 脚本骨架 + helpers + 测试框架

**Files:**
- Create: `claude-keepalive.sh`
- Create: `test/test.sh`

**Interfaces:**
- Produces: `now()`、`ts()`、`ensure_dirs()`、`atomic_write <path> <content>`、`strip_ansi`（stdin→stdin）、`log <LEVEL> <monitor> <msg...>`；常量 `BASE_DIR/MON_DIR/STATE_DIR/LOG_FILE/CONF_FILE/PATTERNS_FILE/STOP_FILE/PID_FILE/DAEMON_OUT/LAUNCHD_LABEL/LAUNCHD_PLIST/SCRIPT_PATH`、`TICK_SECONDS/BREAKER_LIMIT/LOG_MAX_BYTES/DEFAULT_INTERVAL/DEFAULT_MESSAGE`、patterns 数组 `BUSY_PATTERNS/DIALOG_PATTERNS`。

- [ ] **Step 1: 写主脚本骨架与 helpers**

`claude-keepalive.sh`（本任务写入第 1-3 节，后续任务按节追加）：

```bash
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
```

文件末尾（Task 1 先占位，后续任务不断上移）：

```bash
# ---------- 14. 入口 ----------
if [[ "${KEEPALIVE_TEST_MODE:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
```

- [ ] **Step 2: 写测试框架**

`test/test.sh`：

```bash
#!/usr/bin/env bash
# claude-keepalive 单元测试（纯函数为主；bash 3.2 兼容）
set -u
KEEPALIVE_TEST_MODE=1
export KEEPALIVE_BASE_DIR="$(mktemp -d /tmp/ck-test.XXXXXX)"
source "$(cd "$(dirname "$0")" && pwd)/../claude-keepalive.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok  - $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL- $1"; }
eq() { # eq <desc> <got> <want>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got=[$2] want=[$3])"; fi
}

# ===== Task1: helpers =====
eq "now 输出数字" "$(now | grep -c '^[0-9]*$')" "1"
printf 'abc' > "$KEEPALIVE_BASE_DIR/w.txt"
atomic_write "$KEEPALIVE_BASE_DIR/w.txt" "line1"
eq "atomic_write 覆盖" "$(cat "$KEEPALIVE_BASE_DIR/w.txt")" "line1"
printf '\033[31mred\033[0m plain' | strip_ansi | grep -q $'\033' && bad "strip_ansi 去除ANSI" || ok "strip_ansi 去除ANSI"
log INFO testmon "hello"
grep -q '\[INFO\] \[testmon\] hello' "$LOG_FILE" && ok "log 写入" || bad "log 写入"

echo
echo "PASS=$PASS FAIL=$FAIL"
rm -rf "$KEEPALIVE_BASE_DIR"
[[ $FAIL -eq 0 ]]
```

- [ ] **Step 3: 语法检查 + 跑测试**

Run: `cd /Users/lingkai/Desktop/project/claude-keepalive && bash -n claude-keepalive.sh && bash test/test.sh`
Expected: `PASS=6 FAIL=0`

- [ ] **Step 4: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: 脚本骨架与 helpers（路径常量/原子写/ANSI清理/日志）"
```

---

### Task 2: 配置与状态子系统

**Files:**
- Modify: `claude-keepalive.sh`（追加第 4-5 节，插在「---------- 14. 入口 ----------」之前）
- Modify: `test/test.sh`（追加测试）

**Interfaces:**
- Consumes: `atomic_write`、`DEFAULT_*` 常量。
- Produces: `valid_session_name <name>`（rc 0/1）；`load_global_conf()`（source keepalive.conf 与 patterns.conf，可多次调用）；`load_monitor_conf <path>`（成功 rc 0 并设置 `M_SESSION_NAME/M_MESSAGE/M_INTERVAL/M_ENABLED/M_DRY_RUN`，非法 rc 1）；`write_monitor_conf <path> <session_name> <message> <interval> <enabled> <dry_run>`；`state_default()`、`read_state <name>`、`write_state <name>`（操作 `ST_LAST_STATE/ST_LAST_CHECK/ST_LAST_INJECT/ST_AWAITING/ST_CONSEC_FAIL`）。

- [ ] **Step 1: 追加失败测试到 test/test.sh（`echo; echo "PASS..."` 之前）**

```bash
# ===== Task2: 配置与状态 =====
mkdir -p "$MON_DIR"
CN="$MON_DIR/testmon.conf"
write_monitor_conf "$CN" testmon "继续吧" 120 1 0
load_monitor_conf "$CN"
eq "conf 往返 name"    "$M_SESSION_NAME" "testmon"
eq "conf 往返 message" "$M_MESSAGE"      "继续吧"
eq "conf 往返 interval" "$M_INTERVAL"    "120"
eq "conf 往返 enabled" "$M_ENABLED"      "1"
eq "conf 往返 dry_run" "$M_DRY_RUN"      "0"
printf 'session_name=abc\n' > "$CN"
load_monitor_conf "$CN"
eq "缺省 message"  "$M_MESSAGE"      "$DEFAULT_MESSAGE"
eq "缺省 interval" "$M_INTERVAL"     "300"
eq "缺省 enabled"  "$M_ENABLED"      "1"
eq "缺省 dry_run"  "$M_DRY_RUN"      "0"
printf 'session_name=has space\n' > "$CN"
load_monitor_conf "$CN" && bad "非法名应失败" || ok "非法名拒绝"
valid_session_name a-b_1.2 && ok "合法名" || bad "合法名"
valid_session_name "a b" && bad "空格名应拒绝" || ok "空格名拒绝"
valid_session_name "" && bad "空名应拒绝" || ok "空名拒绝"
state_default
ST_LAST_STATE=IDLE; ST_LAST_CHECK=123; ST_CONSEC_FAIL=2
write_state testmon
state_default
read_state testmon
eq "state 往返" "$ST_LAST_STATE/$ST_LAST_CHECK/$ST_CONSEC_FAIL" "IDLE/123/2"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `bash test/test.sh`
Expected: FAIL（`write_monitor_conf: command not found` 一类）

- [ ] **Step 3: 实现第 4-5 节**

```bash
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `bash test/test.sh`
Expected: `PASS=22 FAIL=0`

- [ ] **Step 5: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: 监控 conf 与运行时 state 读写（原子写、默认值、名字校验）"
```

---

### Task 3: 分类与决策纯函数

**Files:**
- Modify: `claude-keepalive.sh`（追加第 6 节）
- Modify: `test/test.sh`

**Interfaces:**
- Consumes: `BUSY_PATTERNS/DIALOG_PATTERNS`、`BREAKER_LIMIT`、`strip_ansi`。
- Produces: `classify_tail <text>`（echo `BUSY|DIALOG|IDLE|UNKNOWN`）；`decide_action <awaiting> <consec_fail> <content> <message>`（echo `INJECT_FIRST|INJECT_AGAIN|INJECT_RETRY|TRIP`，只在 IDLE 时被调用）。

- [ ] **Step 1: 追加失败测试**

```bash
# ===== Task3: 分类与决策 =====
FIX_IDLE='
> 继续
  已完成第一部分
✻ 一些输出
❯
⏵⏵ accept edits on · claude-opus-5 · ~/proj  ? for shortcuts'
eq "idle: 空输入框+footer" "$(classify_tail "$FIX_IDLE")" "IDLE"
FIX_IDLE2='
结果如下
❯'
eq "idle: ❯ 为最后一行" "$(classify_tail "$FIX_IDLE2")" "IDLE"
FIX_BUSY='
> 继续
⠸ Generating… (esc to interrupt)
'
eq "busy: esc to interrupt" "$(classify_tail "$FIX_BUSY")" "BUSY"
FIX_DIALOG='
  ⎿ Updated 3 files
Do you want to proceed?
❯ 1. Yes
  2. Yes, and auto-accept edits
  3. No, and tell Claude what to do differently (esc)
'
eq "dialog: 权限菜单" "$(classify_tail "$FIX_DIALOG")" "DIALOG"
FIX_DRAFT='
❯ 请继续修改测试
⏵⏵ accept edits on'
eq "draft: 有草稿不算 IDLE" "$(classify_tail "$FIX_DRAFT")" "UNKNOWN"
FIX_UNKNOWN='
hello world
plain text
'
eq "unknown: 无特征" "$(classify_tail "$FIX_UNKNOWN")" "UNKNOWN"
FIX_MIX='
❯ 1. Yes
⠹ Working (esc to interrupt)
'
eq "busy 优先于 dialog" "$(classify_tail "$FIX_MIX")" "BUSY"
FIX_ANSI="$(printf '\033[32m❯\033[0m\n')"
eq "idle: ANSI 包裹的 ❯" "$(classify_tail "$FIX_ANSI")" "IDLE"
CT='> 继续
❯'
eq "首次注入"      "$(decide_action 0 0 "$CT" "继续")"   "INJECT_FIRST"
eq "落地后再注入"  "$(decide_action 1 0 "$CT" "继续")"   "INJECT_AGAIN"
CT_NO='别的输出
❯'
eq "未落地重试"    "$(decide_action 1 0 "$CT_NO" "继续")" "INJECT_RETRY"
eq "未落地达上限"  "$(decide_action 1 2 "$CT_NO" "继续")" "TRIP"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `bash test/test.sh` → FAIL

- [ ] **Step 3: 实现第 6 节**

```bash
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `bash test/test.sh`
Expected: `PASS=35 FAIL=0`

- [ ] **Step 5: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: 四态分类与注入决策纯函数（含 ANSI/草稿/优先级边界）"
```

---

### Task 4: ps 解析

**Files:**
- Modify: `claude-keepalive.sh`（追加第 7 节）
- Modify: `test/test.sh`

**Interfaces:**
- Consumes: `valid_session_name`。
- Produces: `ps_claude_lines()`（echo ps 行）；`ps_tty_of_line <line>`；`ps_args_of_line <line>`；`match_ps_line_for_session <line> <name>`（rc 0 且 echo tty）；`extract_session_name_from_args <args>`；`find_claude_tty_for_session <name>`（rc 0 且 echo `/dev/ttysN` / rc 1 无 / rc 2 歧义）；`scan_running_claude_sessions()`（每行 `name<TAB>tty`）。

- [ ] **Step 1: 追加失败测试**

```bash
# ===== Task4: ps 解析 =====
eq "tty 提取"  "$(ps_tty_of_line '12345 ttys012 claude -n worker')" "ttys012"
eq "args 提取" "$(ps_args_of_line '12345 ttys012 node /x/cli.js -n worker')" "node /x/cli.js -n worker"
eq "-n 取 tty" "$(match_ps_line_for_session '12345 ttys012 claude -n worker' worker)" "ttys012"
eq "--resume 取 tty" "$(match_ps_line_for_session '12345 ttys012 claude --resume ppt' ppt)" "ttys012"
eq "--name= 取 tty" "$(match_ps_line_for_session '12345 ttys012 claude --name=ppt' ppt)" "ttys012"
match_ps_line_for_session '12345 ?? claude -n worker' worker >/dev/null && bad "无tty不匹配" || ok "无tty不匹配"
match_ps_line_for_session '12345 ttys012 claude -n other' worker >/dev/null && bad "名字不同不匹配" || ok "名字不同不匹配"
match_ps_line_for_session '12345 ttys012 tail -n worker.log claude' worker >/dev/null && bad "tail -n 误匹配" || ok "tail -n 不误匹配"
match_ps_line_for_session '12345 ttys012 claude -n worker extra' worker >/dev/null && ok "名字后有参数仍匹配" || bad "名字后有参数仍匹配"
eq "名字提取 -n"      "$(extract_session_name_from_args 'claude -n worker')" "worker"
eq "名字提取 --name=" "$(extract_session_name_from_args 'claude --name=ppt')" "ppt"
eq "名字提取 --resume" "$(extract_session_name_from_args 'claude --resume ppt')" "ppt"
eq "名字提取 无"      "$(extract_session_name_from_args 'claude -p hi')" ""
scan_running_claude_sessions | grep -q "$(basename "$0")" && bad "扫描不包含自身" || ok "扫描不包含自身"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `bash test/test.sh` → FAIL

- [ ] **Step 3: 实现第 7 节**

```bash
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
    for (i = 1; i < NF; i++) {
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `bash test/test.sh`
Expected: `PASS=49 FAIL=0`

- [ ] **Step 5: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: ps 定位链路（-n/--name/--resume 名字匹配、tty 提取、会话扫描）"
```

---

### Task 5: iTerm2 AppleScript 集成层

**Files:**
- Modify: `claude-keepalive.sh`（追加第 8 节）

**Interfaces:**
- Consumes: 无（纯 osascript 封装）。
- Produces: `run_osascript <script> [args...]`（设 `OS_OUT/OS_ERR/OS_RC`）；`iterm_read_contents <devtty>`（rc 0 设 `REPLY_CONTENTS` / rc 1 未找到 / rc 2 错误）；`iterm_write_text <devtty> <message>`（rc 0/1/2 同上）；`iterm_scan_tty_by_name <name>`（rc 0 设 `SCAN_COUNT/SCAN_LIST`，SCAN_LIST 每行一个 tty / rc 2 错误）。

- [ ] **Step 1: 实现第 8 节（无法单测 osascript，实机验证）**

```bash
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
```

- [ ] **Step 2: 语法检查 + 实机检测验证**

Run: `bash -n claude-keepalive.sh`
Expected: 无输出

实机验证（检测类调用，不注入）：取当前任一 iTerm2 会话的 tty，直接调用读取：

```bash
bash -c 'source ./claude-keepalive.sh 2>/dev/null; \
  tty=$(ps ax -o tty=,args= | grep -i claude | grep -v grep | head -1 | awk "{print \$1}"); \
  echo "probe tty=$tty"; \
  osascript -e "tell application \"iTerm2\" to count windows"'
```

Expected: iTerm2 未运行 → 报错（预期，记录）；运行中 → 输出窗口数。若报 -1743 权限错误，记录为用户待办（系统设置授权），不阻塞后续任务。

- [ ] **Step 3: Commit**

```bash
git add claude-keepalive.sh
git commit -m "feat: iTerm2 AppleScript 集成（按 tty 读内容/写文本/按名扫描）"
```

---

### Task 6: 单监控检查管线

**Files:**
- Modify: `claude-keepalive.sh`（追加第 9 节）
- Modify: `test/test.sh`

**Interfaces:**
- Consumes: `load_monitor_conf`、`read_state/write_state`、`find_claude_tty_for_session`、`iterm_*`、`classify_tail`、`decide_action`、`log`、`write_monitor_conf`。
- Produces: `touch_state <name> <state>`；`do_inject <name> <devtty> <message>`（读 `M_DRY_RUN`，写 `ST_AWAITING/ST_LAST_INJECT`）；`check_monitor <name>`（完整检查一轮并落盘状态）。

- [ ] **Step 1: 追加失败测试（stub 掉 osascript 依赖，验证纯决策路径与熔断写回）**

```bash
# ===== Task6: 检查管线（stub 外部依赖） =====
iterm_read_contents() { REPLY_CONTENTS="$STUB_CONTENTS"; return 0; }
iterm_write_text()    { STUB_WROTE="$2"; return 0; }
iterm_scan_tty_by_name() { SCAN_COUNT=0; SCAN_LIST=""; return 0; }
find_claude_tty_for_session() { echo "$STUB_TTY"; return 0; }

mkdir -p "$MON_DIR" "$STATE_DIR"
CN6="$MON_DIR/pipe1.conf"
write_monitor_conf "$CN6" pipe1 "继续" 60 1 0
STUB_TTY="/dev/ttys999"
STUB_CONTENTS='> 继续
完成
❯'
STUB_WROTE=""
check_monitor pipe1
eq "idle 注入消息" "$STUB_WROTE" "继续"
read_state pipe1
eq "注入后 awaiting=1" "$ST_AWAITING" "1"
eq "状态记 IDLE" "$ST_LAST_STATE" "IDLE"

STUB_CONTENTS='> 继续
❯'
check_monitor pipe1            # INJECT_AGAIN 路径
read_state pipe1
eq "落地后 fail 清零" "$ST_CONSEC_FAIL" "0"

STUB_CONTENTS='无关内容
❯'
check_monitor pipe1; read_state pipe1
eq "未落地 fail=1" "$ST_CONSEC_FAIL" "1"
check_monitor pipe1; read_state pipe1
eq "未落地 fail=2" "$ST_CONSEC_FAIL" "2"
check_monitor pipe1; read_state pipe1
eq "第三次触发熔断 fail=3" "$ST_CONSEC_FAIL" "3"
load_monitor_conf "$CN6"
eq "熔断写回 enabled=0" "$M_ENABLED" "0"
eq "熔断不注入本次" "$STUB_WROTE" "继续"

STUB_CONTENTS='⠸ Working (esc to interrupt)
'
check_monitor pipe1; read_state pipe1
eq "busy 状态记录" "$ST_LAST_STATE" "BUSY"

# 恢复 stub（后续 task 不受影响）
unset -f iterm_read_contents iterm_write_text iterm_scan_tty_by_name find_claude_tty_for_session
```

- [ ] **Step 2: 跑测试确认失败**

Run: `bash test/test.sh` → FAIL（check_monitor 未定义）

- [ ] **Step 3: 实现第 9 节**

```bash
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `bash test/test.sh`
Expected: `PASS=62 FAIL=0`

- [ ] **Step 5: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: 单监控检查管线（定位→读取→分类→决策→注入/熔断）"
```

---

### Task 7: daemon 循环与 start/stop/status/once

**Files:**
- Modify: `claude-keepalive.sh`（追加第 10-11 节，并在第 14 节入口前追加第 13 节 usage/main）
- Modify: `test/test.sh`

**Interfaces:**
- Consumes: 前面全部。
- Produces: `daemon_pid()`（echo pid 或空）；`daemon_run()`；`daemon_exit()`；`cmd_start/cmd_stop/cmd_status/cmd_once`；`usage()`；`main()`；入口 dispatch（`setup|start|stop|status|once|daemon|install|uninstall|help`）。

- [ ] **Step 1: 追加测试**

```bash
# ===== Task7: 入口与状态表 =====
mkdir -p "$MON_DIR" "$STATE_DIR"
write_monitor_conf "$MON_DIR/s7.conf" s7 "继续" 60 1 0
state_default; ST_LAST_STATE=IDLE; ST_LAST_CHECK=$(now); write_state s7
cmd_status | grep -q 's7' && ok "status 表含监控" || bad "status 表含监控"
cmd_status | grep -q 'daemon: stopped' && ok "status 显示 daemon 状态" || bad "status 显示 daemon 状态"
daemon_pid | grep -q . && bad "无 daemon 时不返回 pid" || ok "无 daemon 时不返回 pid"
echo '999999999' > "$PID_FILE"
daemon_pid | grep -q . && bad "死 pid 不算运行" || ok "死 pid 不算运行"
rm -f "$PID_FILE"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `bash test/test.sh` → FAIL

- [ ] **Step 3: 实现第 10、11、13 节**

```bash
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
      log ERROR daemon "已有实例 pid=$old，退出"; exit 1
    fi
    echo "$$" > "$PID_FILE"
  fi
  trap daemon_exit INT TERM
  trap 'rm -f "$PID_FILE"' EXIT
  log INFO daemon "启动（pid $$，tick=${TICK_SECONDS}s）"
  local conf name
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
    sleep "$TICK_SECONDS"
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
    if   (( ST_LAST_CHECK  > 0 )); then lc=$(date -r "$ST_LAST_CHECK"  '+%m-%d %H:%M:%S'); else lc="-"; fi
    if   (( ST_LAST_INJECT > 0 )); then li=$(date -r "$ST_LAST_INJECT" '+%m-%d %H:%M:%S'); else li="-"; fi
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
    echo "== 检查 $name（session=${M_SESSION_NAME}）"
    check_monitor "$name"
  done
  (( any == 0 )) && echo "没有监控配置：$MON_DIR 下放 .conf，或运行 setup"
  return 0
}
```

第 13 节（usage 与 main）：

```bash
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `bash test/test.sh`
Expected: `PASS=67 FAIL=0`（若 setup/install 相关分支因函数未定义报错，本任务先给 `tui_main/cmd_install/cmd_uninstall/launchd_loaded` 留最小占位：`launchd_loaded() { launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; }`，其余三个 Task 8/9 实现；占位会导致 `main` 引用未定义函数——因此本任务把 usage/main 放第 13 节，并在测试里不触达这些分支）

- [ ] **Step 5: 手动冒烟（不影响真实环境）**

```bash
KEEPALIVE_BASE_DIR=$(mktemp -d) bash claude-keepalive.sh status
KEEPALIVE_BASE_DIR=$(mktemp -d) bash claude-keepalive.sh help
KEEPALIVE_BASE_DIR=$(mktemp -d) bash claude-keepalive.sh once
```
Expected: status 输出表头 + `daemon: stopped`；help 输出用法；once 提示无配置。临时目录即丢。

- [ ] **Step 6: Commit**

```bash
git add claude-keepalive.sh test/test.sh
git commit -m "feat: daemon 循环与 start/stop/status/once 入口"
```

---

### Task 8: launchd install/uninstall

**Files:**
- Modify: `claude-keepalive.sh`（追加第 12 节 launchd 部分）

**Interfaces:**
- Consumes: `LAUNCHD_LABEL/LAUNCHD_PLIST/DAEMON_OUT/SCRIPT_PATH`。
- Produces: `launchd_loaded()`（rc 0/1）；`cmd_install()`（写 plist 并 bootstrap）；`cmd_uninstall()`（bootout 并删 plist）。

- [ ] **Step 1: 实现第 12 节**

```bash
# ---------- 12. launchd ----------
launchd_loaded() { launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; }

cmd_install() {
  ensure_dirs
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
    <string>$SCRIPT_PATH</string>
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
```

- [ ] **Step 2: 验证**

Run: `bash -n claude-keepalive.sh && bash test/test.sh`
Expected: 全部通过；`KEEPALIVE_BASE_DIR=$(mktemp -d) bash claude-keepalive.sh install` 执行后 `ls ~/Library/LaunchAgents/com.lingkai.claude-keepalive.plist` 存在、`launchctl list | grep claude-keepalive` 有行；随后 `uninstall` 清干净。（真实环境执行 install 后立即 uninstall，不留常驻。）

- [ ] **Step 3: Commit**

```bash
git add claude-keepalive.sh
git commit -m "feat: launchd 常驻安装/卸载（SuccessfulExit=false 语义）"
```

---

### Task 9: TUI

**Files:**
- Modify: `claude-keepalive.sh`（追加第 12.5 节 TUI，置于 usage 之前）

**Interfaces:**
- Consumes: `scan_running_claude_sessions/write_monitor_conf/load_monitor_conf/cmd_status/cmd_start/cmd_stop/cmd_install/cmd_uninstall/daemon_pid/log`。
- Produces: `tui_main()`（入口，被 main 的 setup 分支调用）及内部函数 `tui_ask/tui_pick_monitor/tui_live_list/tui_add_monitor/tui_edit_monitor/tui_daemon_menu/tui_show_logs`。

- [ ] **Step 1: 实现 TUI**

```bash
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
  tui_ask message   "$DEFAULT_MESSAGE"  "注入词"
  tui_ask interval  "$DEFAULT_INTERVAL" "检测间隔（秒）"
  tui_ask dry       "n"                 "dry_run 模式 [y/n]"
  [[ "$dry" == y* ]] && dry=1 || dry=0
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=$DEFAULT_INTERVAL
  write_monitor_conf "$conf" "$session_name" "$message" "$interval" 1 "$dry"
  echo "已写入 $conf（daemon 在跑则下一 tick 自动生效）"
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
  write_monitor_conf "$conf" "$sn" "$msg" "$itv" "$en" "$dr"
  echo "已更新 $conf"
  local yn; read -r -p "是否删除该监控? [y/N]: " yn
  if [[ "$yn" == y* ]]; then
    rm -f "$conf" "$STATE_DIR/$sn.state"
    echo "已删除"
  fi
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
}

tui_show_logs() {
  echo
  if [[ ! -f "$LOG_FILE" ]]; then echo "暂无日志"; return 0; fi
  tail -n 30 "$LOG_FILE"
  echo
  printf '\033[2m（完整日志：%s）\033[0m\n' "$LOG_FILE"
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
```

- [ ] **Step 2: 验证**

Run: `bash -n claude-keepalive.sh && bash test/test.sh`
Expected: 全部通过。TUI 交互留用户验收（README 手动清单），脚本层验证 `KEEPALIVE_BASE_DIR=$(mktemp -d) bash -c 'source ./claude-keepalive.sh; KEEPALIVE_TEST_MODE=1; scan_running_claude_sessions'` 不报错（本机无 named claude 会话则输出空）。

- [ ] **Step 3: Commit**

```bash
git add claude-keepalive.sh
git commit -m "feat: 纯 bash TUI（添加/编辑/删除/实时列表/daemon 控制/日志）"
```

---

### Task 10: README + 收尾验证

**Files:**
- Create: `README.md`
- Modify: `claude-keepalive.sh`（`chmod +x`）

**Interfaces:**
- Consumes: 全部。
- Produces: 面向用户的文档（安装、用法、安全刹车、故障排查、手动验收清单）。

- [ ] **Step 1: 写 README**

内容必须覆盖：项目一句话说明；快速开始（`./claude-keepalive.sh setup` → `start`）；命令表；配置字段表与目录结构；四层刹车（stop 文件 / per-monitor enabled / 熔断 / dry_run）；首次运行需在 macOS 弹窗中允许「自动化→控制 iTerm2」（-1743 排查）；已知限制（UI 特征依赖、TOCTOU、只管正常停止、额度消耗警告）；手动验收清单（起一个 `claude -n test-xxx` 会话 → 配 dry_run=1 → `once` 看日志判定 → 关 dry_run 看真实注入 → `stop` 验证刹车）。

- [ ] **Step 2: 全量验证**

```bash
bash -n claude-keepalive.sh
bash test/test.sh           # PASS=67 FAIL=0
chmod +x claude-keepalive.sh
./claude-keepalive.sh help
```

- [ ] **Step 3: Commit**

```bash
git add README.md claude-keepalive.sh
git commit -m "docs: README（用法/刹车/排查/验收清单）"
```

---

## Self-Review 记录

- Spec 覆盖：§3 架构→Task 6/7；§4 配置→Task 2；§5 定位→Task 4/5；§6 判定→Task 3；§7 注入防失控→Task 3/6；§8 生命周期→Task 7/8；§9 TUI→Task 9；§10 日志→Task 1；§11 错误处理→Task 5/6；§12 测试→各任务 + Task 10；§13 限制→README（Task 10）。无缺口。
- 占位符扫描：无 TBD/TODO；每个代码步骤含完整代码。
- 命名一致性：`classify_tail/decide_action/check_monitor/do_inject/touch_state/load_monitor_conf/write_monitor_conf/read_state/write_state/state_default/iterm_*/run_osascript/ps_*/find_claude_tty_for_session/scan_running_claude_sessions/daemon_pid/daemon_run/cmd_*/launchd_loaded/tui_*` 各任务间一致。
- bash 3.2 兼容复核：无 `declare -A`、无 `printf %()T`、数组空展开已用 `${arr[@]+"${arr[@]}"}` 守卫（Task 3 分类循环、Task 9 `tui_pick_monitor` 的 `${arr[$pick]:-}`）。
