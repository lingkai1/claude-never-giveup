# claude-keepalive Python 移植计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 bash v1.0.0 的 claude-keepalive 完整移植为 Python（stdlib-only）v2.0.0，行为与设计对齐，bash 版从工作区移除（git 历史保留）。

**Architecture:** 单文件 `claude_keepalive.py`（约 650 行，dataclass conf/state、subprocess 调 osascript/launchctl、signal 驱动 daemon、select 超时 TUI）+ `test/test_keepalive.py`（unittest，importlib.reload 隔离 BASE_DIR）。行为规格不变：多监控、四态判定、熔断、四层刹车、launchd。

**Tech Stack:** Python 3.9+（macOS 自带 /usr/bin/python3 3.9.6），stdlib only。

**Spec:** `docs/specs/2026-09-06-claude-keepalive-design.md`（含 §14 修订记录）

## Global Constraints

- Python 3.9 兼容：不用 match 语句、不用 `X | Y` 运行时类型联合（用 `Optional[T]`）、不用 3.10+ 特性；`Path.unlink(missing_ok=True)`（3.8+）可用。
- 禁止 `eval`/`exec`：state 文件改 JSON；`patterns.conf` 改 JSON（`{"BUSY_PATTERNS": [...], "DIALOG_PATTERNS": [...]}`）——这是对 bash 版的格式变更，README 同步。
- 行为对齐 bash v1.0.0：四态判定窗口（40/12/8 行、末 6 非空行找 `❯`）、marker `"> 消息"` 查末 100 行、熔断 3 次、tick 15s 切 1s 睡眠、launchd `SuccessfulExit=false` + 部署副本到 `~/.claude-keepalive/bin/`。
- conf 仍为人类可读 key=value；会话名 `^[A-Za-z0-9._-]+$`；所有写入 tmp+`os.replace` 原子化。
- osascript 调用加 30s 超时（bash 版没有，Python 顺手加固）。
- 测试断言数 ≥ bash 版的 62。

## 任务

### Task P1: 骨架 + helpers + conf/state + 分类/决策/ps 纯函数 + 对应单测（TDD）

**Files:** Create `claude_keepalive.py`、`test/test_keepalive.py`

按 bash 版逐块移植：常量与路径（env `KEEPALIVE_BASE_DIR`）、`now/ts/strip_ansi/atomic_write/log/valid_session_name`、`MonitorConf`（key=value load/save + 校验 + 默认值）、`MonitorState`（JSON load/save/touch）、`classify_tail`/`decide_action`、ps 五个纯函数 + `scan_running_claude_sessions`。单测覆盖 bash 版全部 62 断言的对应面（fixture 原样搬运）。

- [ ] 写 test_keepalive.py（helpers/conf/state/classify/decide/ps 六组）
- [ ] 跑 `/usr/bin/python3 -m unittest discover -s test -v` 确认失败
- [ ] 实现，跑过，commit

### Task P2: iTerm2 集成 + 检查管线 + 管线单测（stub 打桩）

**Files:** Modify `claude_keepalive.py`、`test/test_keepalive.py`

`run_osascript`（timeout=30，返回 out/err/rc）、三个 AppleScript 常量与函数、`_locate_by_title`、`do_inject`、`check_monitor`（含 ps-miss→标题兜底、权限错误 -1743 提示）。单测以模块属性替换打桩，覆盖 bash 版 Task6/Task9 补充的全部场景（首注入/落地清零/fail 1-3/熔断写回/再启用 BUSY/禁用跳过/兜底注入）。

- [ ] 测试先行 → 实现 → 跑过 → commit

### Task P3: daemon + 命令 + launchd

**Files:** Modify `claude_keepalive.py`、`test/test_keepalive.py`

`daemon_pid/daemon_run`（O_EXCL 单实例、SIGTERM/SIGINT 立即响应、1s 切片睡、finally 清 pid）、`cmd_start/stop/status/once`、`load_global_conf`（keepalive.conf key=value + patterns.conf JSON）、`launchd_loaded/cmd_install/cmd_uninstall`（plist 用 `/usr/bin/python3` + 部署副本）、`usage/main`。单测：cmd_status 表、daemon_pid 死 pid。实机验证：daemon 启停 <1s、launchd 装卸、once 对真实会话 dry-run。

- [ ] 测试 → 实现 → 实机验证 → commit

### Task P4: TUI + README + 收尾

**Files:** Modify `claude_keepalive.py`、`README.md`；`git rm claude-keepalive.sh test/test.sh`

菜单 REPL（`_ask` 回车保默认）、live list（select 5s 超时刷新）、添加（扫描在跑会话）、编辑/删除、daemon 控制、日志尾部。README 更新（python3 用法、patterns.conf JSON、开发=unittest）。收尾：全量测试、`python3 -m py_compile`、live drill、版本 2.0.0。

- [ ] 实现 → 全量验证 → 移除 bash 版 → commit
