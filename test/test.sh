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
eq "缺省 message"  "$M_MESSAGE"  "$DEFAULT_MESSAGE"
eq "缺省 interval" "$M_INTERVAL" "300"
eq "缺省 enabled"  "$M_ENABLED"  "1"
eq "缺省 dry_run"  "$M_DRY_RUN"  "0"
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
eq "首次注入"     "$(decide_action 0 0 "$CT" "继续")"   "INJECT_FIRST"
eq "落地后再注入" "$(decide_action 1 0 "$CT" "继续")"   "INJECT_AGAIN"
CT_NO='别的输出
❯'
eq "未落地重试"   "$(decide_action 1 0 "$CT_NO" "继续")" "INJECT_RETRY"
eq "未落地达上限" "$(decide_action 1 2 "$CT_NO" "继续")" "TRIP"

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
eq "名字提取 -n"       "$(extract_session_name_from_args 'claude -n worker')" "worker"
eq "名字提取 --name="  "$(extract_session_name_from_args 'claude --name=ppt')" "ppt"
eq "名字提取 --resume" "$(extract_session_name_from_args 'claude --resume ppt')" "ppt"
eq "名字提取 无"       "$(extract_session_name_from_args 'claude -p hi')" ""
scan_running_claude_sessions | grep -q "keepalive" && bad "扫描不包含自身" || ok "扫描不包含自身"

# ===== Task6: 检查管线（stub 外部依赖） =====
iterm_read_contents()   { REPLY_CONTENTS="$STUB_CONTENTS"; return 0; }
iterm_write_text()      { STUB_WROTE="$2"; return 0; }
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
check_monitor pipe1
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
eq "熔断那次不注入" "$STUB_WROTE" "继续"

# 熔断已禁用监控 → 重新启用后验证 BUSY 路径（计划审查修正点）
write_monitor_conf "$CN6" pipe1 "继续" 60 1 0
STUB_CONTENTS='⠸ Working (esc to interrupt)
'
check_monitor pipe1; read_state pipe1
eq "busy 状态记录" "$ST_LAST_STATE" "BUSY"
eq "busy 清零 awaiting" "$ST_AWAITING" "0"

# 禁用的监控直接跳过
write_monitor_conf "$CN6" pipe1 "继续" 60 0 0
STUB_CONTENTS='❯'
STUB_WROTE=""
check_monitor pipe1
eq "disabled 不注入" "$STUB_WROTE" ""

# 恢复 stub（后续 task 不受影响）
unset -f iterm_read_contents iterm_write_text iterm_scan_tty_by_name find_claude_tty_for_session

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

echo
echo "PASS=$PASS FAIL=$FAIL"
rm -rf "$KEEPALIVE_BASE_DIR"
[[ $FAIL -eq 0 ]]
