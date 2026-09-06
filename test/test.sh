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

echo
echo "PASS=$PASS FAIL=$FAIL"
rm -rf "$KEEPALIVE_BASE_DIR"
[[ $FAIL -eq 0 ]]
