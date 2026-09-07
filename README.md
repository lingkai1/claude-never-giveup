# claude-keepalive

让指定名字的 Claude Code 会话（跑在 iTerm2 里）**持续工作不停止**：守护进程定时检查每个会话，发现它正常完成一轮、停在输入提示符，就自动注入一个继续词（默认「继续」），让它接着干。

单文件 Python（stdlib only），macOS 自带 `/usr/bin/python3` 直接跑，零外部依赖。

## 快速开始

```bash
cd claude-keepalive
/usr/bin/python3 claude_keepalive.py setup   # TUI：添加监控（自动扫描在跑的会话）、启停 daemon
# 或纯命令行：
mkdir -p ~/.claude-keepalive/monitors
cat > ~/.claude-keepalive/monitors/worker.conf <<'EOF'
session_name=worker
message=继续
interval=300
enabled=1
dry_run=1
EOF
/usr/bin/python3 claude_keepalive.py start   # 前台调试用 daemon 子命令
```

**建议先 dry_run=1 跑一轮**（`once` 看日志判定），确认无误再关掉 dry_run。

## 命令

| 命令 | 作用 |
|---|---|
| `setup` | TUI：监控列表（实时）/ 添加 / 编辑删除 / 启停 daemon / 看日志 |
| `start` | 启动 daemon（清除 stop 文件；装了 launchd 则经 launchd 拉起） |
| `stop` | 停止 daemon（放 stop 文件 + SIGTERM，秒级退出） |
| `status` | 非交互状态表 |
| `once` | 立即检查一轮全部监控（调试） |
| `daemon` | 前台运行（launchd / 调试用） |
| `install` | 安装 launchd 常驻（脚本部署到 `~/.claude-keepalive/bin/`） |
| `uninstall` | 卸载 launchd |

## 会话怎么定位

两条链路，前者优先：

1. **ps 参数匹配**：`claude -n <名字>`（或 `--name` / `--resume` / `-r`）启动的进程 → tty → iTerm2 session。
2. **iTerm2 标题兜底**：ps 匹配不到时，按 iTerm2 会话标题包含会话名定位——所以**中途 `/rename` 过、或根本没用 `-n` 启动的会话也能找到**（Claude Code 会把会话名写进终端标题）。

命中 0 个 → `NO_SESSION`；命中多个 → `AMBIGUOUS`（宁可漏不可错，一律不注入）。

> 长跑会话建议用 `claude -n <固定名>` 启动，或进去后 `/rename <固定名>`，给监控一个稳定锚点。

## 状态判定（每轮检查）

读取 iTerm2 session 内容尾部，按序判定：

| 状态 | 特征 | 动作 |
|---|---|---|
| BUSY | spinner / `esc to interrupt` | 不动 |
| DIALOG | 权限确认、编号选项菜单 | 不动（注入会打乱菜单） |
| IDLE | 底部空输入框 `❯` | **注入继续词** |
| UNKNOWN | 其余（含输入框有草稿） | 不动（绝不替你提交半截话） |

Claude Code UI 改版导致误判时，改 `~/.claude-keepalive/patterns.conf`（**JSON**：`{"BUSY_PATTERNS": [...], "DIALOG_PATTERNS": [...]}`），不用改代码。

## 防失控（四层刹车）

1. **dry_run**：每监控可配，只记日志不注入。
2. **熔断**：连续 3 次（`BREAKER_LIMIT`）注入后从未观察到 BUSY、也找不到注入痕迹 → 自动 `enabled=0` 写回并 ERROR 记日志，其他监控不受影响。
3. **按监控禁用**：`enabled=0`，或 TUI 里编辑。
4. **全局刹车**：`touch ~/.claude-keepalive/stop`，daemon 1 秒内退出；`start` 会清除。

注意：**「永不停止」意味着持续消耗额度**。任务真的做完后记得 `stop` 或禁用监控。

## 配置

`~/.claude-keepalive/`：

```text
monitors/<名字>.conf   # 每监控一份（key=value，人类可读）：session_name / message / interval / enabled / dry_run
state/<名字>.json      # 运行时状态（JSON，daemon 落盘，status/TUI 直接读）
keepalive.log          # 日志（>5MB 轮转保留一档）
keepalive.conf         # 可选全局（key=value）：TICK_SECONDS / BREAKER_LIMIT / LOG_MAX_BYTES
patterns.conf          # 可选（JSON）：覆盖 BUSY_PATTERNS / DIALOG_PATTERNS
stop                   # 刹车文件（存在即停）
```

监控 conf 字段：`session_name`（必填，仅 `[A-Za-z0-9._-]`）、`message`（默认 `继续`）、`interval`（默认 300 秒）、`enabled`（默认 1）、`dry_run`（默认 0）。新增/修改 conf **无需重启 daemon**，下一个 tick 自动生效。

launchd 语义：`KeepAlive.SuccessfulExit=false`——daemon 正常退出（含 stop 刹车）不复活，仅异常退出才拉起。`install` 会把脚本拷到 `~/.claude-keepalive/bin/`（launchd 进程没有 `~/Desktop` 等 TCC 目录的访问权），plist 用 `/usr/bin/python3` 调它；**改了主脚本要重跑 `install`**。

## 故障排查

- **日志报 `osascript 失败 ... -1743 / not authorized`**：系统设置 → 隐私与安全性 → 自动化 → 给你的终端 App 勾上「控制 iTerm2」。
- **一直 NO_SESSION**：会话名对不上 iTerm2 标题。跑 `once` 看日志；确认会话用 `-n` 启动过或 `/rename` 成固定名。
- **一直 UNKNOWN**：Claude Code UI 改版，idle 签名变了 → 更新 `patterns.conf`。
- **注入了但没反应**：看 `status` 的 FAILS 列；连续 3 次未生效会自动熔断禁用，去 TUI 重新启用并排查（比如消息词被 Claude Code 过滤）。

## 手动验收清单

1. 起一个试验会话：`claude -n keepalive-test`，让它干个长活（比如「持续重构直到我说停」）。
2. 配监控（TUI 添加或手写 conf），`dry_run=1`。
3. `claude_keepalive.py once`，日志应出现 `state=IDLE` + `[dry-run] 本应注入「继续」`。
4. 把 `dry_run` 改 0，等会话停到输入框 → 下一轮 interval 内应看到「已注入「继续」」，会话继续干活。
5. 让 Claude 停在权限确认弹窗 → 确认日志 `state=DIALOG，不打扰`。
6. `touch ~/.claude-keepalive/stop` → daemon 1 秒内退出；`start` 后恢复。

## 已知限制

- 依赖 Claude Code TUI 文本特征（`❯`、`esc to interrupt` 等）；改版后安全落到 UNKNOWN 不动，需更新 patterns.conf。
- 读屏与注入之间有毫秒级窗口；注入落点若恰逢弹窗，文本会作为待输入排队，风险低，熔断兜底。
- 只管「正常停止」：进程崩溃、限流横幅、断流不在职责内（那是 [claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) 的领域，两者可叠加使用）。
- 完成一轮到下一轮注入之间最长空转一个 `interval`（默认 5 分钟，想要更跟手就调小）。

## 开发

```bash
/usr/bin/python3 -m unittest discover -s test -v   # 53 个用例
```

- Python 3.9+ 兼容（macOS 自带 3.9.6 验证），stdlib only。
- 历史版本：v1.0.0 为 bash 实现（本仓库 git 历史），v2.0.0 起为 Python 重写。
- 设计文档：`docs/specs/2026-09-06-claude-keepalive-design.md`（含 §14 Python 修订）；移植计划：`docs/plans/2026-09-07-python-port.md`。
