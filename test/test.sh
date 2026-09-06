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
