# claude-never-giveup

让指定名字的 Claude Code 会话（跑在 iTerm2 里）**持续工作不停止**：守护进程定时检查每个会话，发现它正常完成一轮、停在输入提示符，就自动注入一个继续词（默认「继续」），让它接着干。

单文件 Python（stdlib only），macOS 自带 `/usr/bin/python3` 直接跑，零外部依赖。**所有日常操作都在内置 TUI 里完成**——添加/编辑/删除监控、启停 daemon、看实时状态和日志，不用手改任何文件。

## 快速开始（TUI 三步走）

```bash
cd claude-never-giveup
/usr/bin/python3 claude_never_giveup.py setup
```

1. 选 **`2) 添加监控`** —— 自动扫描在跑的命名会话供选；没扫到就选「手动输入会话名」，填 iTerm2 标签上显示的会话名（如 `aidoc-bugfix`）。注入词 / 间隔 / dry_run 一路回车取默认值即可。
2. 选 **`4) 启动 / 停止 daemon`** → `1` 启动。
3. 选 **`1) 监控列表`** 看实时状态（每 5 秒刷新）。

完成。第一次建议把 dry_run 留开（`y`），在列表里确认 `state=IDLE`、日志出现 `[dry-run] 本应注入「继续」` 后，再进「编辑」关掉 dry_run 开始真注入。

## TUI 功能详解

主菜单：

```text
== claude-never-giveup v2.1.0 ==
  1) 监控列表（实时刷新）
  2) 添加监控
  3) 编辑 / 删除监控
  4) 启动 / 停止 daemon
  5) 查看日志尾部
  6) 退出
```

**1) 监控列表（实时）** —— 每 5 秒自动重绘状态表：

```text
NAME               ENABLED  STATE           LAST_CHECK           LAST_INJECT          FAILS
aidoc-bugfix       1        IDLE            09-07 12:53:04       09-07 12:48:02       0
daemon: running (pid 16747)
```

`STATE` 一列见下文「状态判定」；`FAILS` 是注入未生效计数，到 3 会熔断该监控。`q`+回车 返回。

**2) 添加监控** —— 自动扫描在跑的 `claude -n` 命名会话列出供选（含 tty 信息）；没有命名会话时选手动输入。随后依次询问：

```text
注入词 [继续]:           ← 回车用默认，可改成任意催促语
检测间隔（秒） [300]:    ← 想更跟手就调小（如 60）
dry_run 模式 [y/n]: [n]  ← 先 y 验证，再关
```

重名会先确认再覆盖。

**3) 编辑 / 删除监控** —— 选中后逐项重问，**回车 = 保留当前值**：session_name、注入词、间隔、`enabled(1/0)`、`dry_run(1/0)`；最后确认是否删除（连状态一起清）。被熔断禁用的监控在这里把 enabled 改回 1 即可重新武装。

**4) 启动 / 停止 daemon** —— 显示 daemon 运行状态，四个动作：启动 / 停止 / 安装 launchd 常驻（开机自启）/ 卸载。

**5) 查看日志尾部** —— 最近 30 行日志（注入、状态变化、熔断、错误都在里面）。

**生效时机**：监控的增删改在 daemon 下一个 tick（15 秒内）自动生效，**无需重启**。

## 命令一览（脚本化 / 远程用）

TUI 之外的全部能力都有对应子命令，适合写进脚本：

| 命令 | 作用 |
|---|---|
| `setup` | TUI（主入口，见上） |
| `start` | 启动 daemon（清除 stop 文件；装了 launchd 则经 launchd 拉起） |
| `stop` | 停止 daemon（放 stop 文件 + SIGTERM，秒级退出） |
| `status` | 非交互状态表（同 TUI 列表的数据） |
| `once` | 立即检查一轮全部监控（调试） |
| `daemon` | 前台运行 daemon（launchd / 调试用） |
| `install` | 安装 launchd 常驻（脚本部署到 `~/.claude-never-giveup/bin/`） |
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

IDLE 时的注入决策：首次直接注入；已注入过又见 IDLE 时，先用三路信号确认上次**真的落地**了（90 秒 peek 窗口抓 BUSY → 屏幕找 `> 继续` 消息行 → 查该会话的 transcript 文件），确认则继续注入；三路全失败才计一次失败，连续 3 次熔断。委派型会话（主提示符长期空闲、活儿在后台 agent）因此也不会误熔断。

Claude Code UI 改版导致误判时，改 `~/.claude-never-giveup/patterns.conf`（**JSON**：`{"BUSY_PATTERNS": [...], "DIALOG_PATTERNS": [...]}`），不用改代码。

## 防失控（四层刹车）

1. **dry_run**：每监控可配（TUI 添加/编辑里设置），只记日志不注入。
2. **熔断**：连续 3 次（`BREAKER_LIMIT`）注入未生效 → 自动 `enabled=0` 并 ERROR 记日志，其他监控不受影响；TUI 里重新启用。
3. **按监控禁用**：TUI 编辑里 `enabled=0`。
4. **全局刹车**：`touch ~/.claude-never-giveup/stop`，daemon 1 秒内退出；`start` 会清除。

注意：**「永不停止」意味着持续消耗额度**。任务真的做完后记得 `stop` 或禁用监控。

## 配置（手改文件的高级路径）

日常用 TUI 即可；以下为直接编辑文件的说明。运行时目录 `~/.claude-never-giveup/`：

```text
monitors/<名字>.conf   # 每监控一份（key=value，人类可读）：session_name / message / interval / enabled / dry_run
state/<名字>.json      # 运行时状态（JSON，daemon 落盘，status/TUI 直接读）
never-giveup.log       # 日志（>5MB 轮转保留一档）
never-giveup.conf      # 可选全局（key=value）：TICK_SECONDS / BREAKER_LIMIT / LOG_MAX_BYTES
patterns.conf          # 可选（JSON）：覆盖 BUSY_PATTERNS / DIALOG_PATTERNS
stop                   # 刹车文件（存在即停）
```

手写一个监控示例：

```bash
cat > ~/.claude-never-giveup/monitors/worker.conf <<'EOF'
session_name=worker
message=继续
interval=300
enabled=1
dry_run=1
EOF
```

字段：`session_name`（必填，仅 `[A-Za-z0-9._-]`）、`message`（默认 `继续`）、`interval`（默认 300 秒）、`enabled`（默认 1）、`dry_run`（默认 0）。

launchd 语义：`KeepAlive.SuccessfulExit=false`——daemon 正常退出（含 stop 刹车）不复活，仅异常退出才拉起。`install` 会把脚本拷到 `~/.claude-never-giveup/bin/`（launchd 进程没有 `~/Desktop` 等 TCC 目录的访问权），plist 用 `/usr/bin/python3` 调它；**改了主脚本要重跑 `install`**。

## 故障排查

- **日志报 `osascript 失败 ... -1743 / not authorized`**：系统设置 → 隐私与安全性 → 自动化 → 给你的终端 App 勾上「控制 iTerm2」。
- **一直 NO_SESSION**：会话名对不上 iTerm2 标题。跑 `once` 看日志；确认会话用 `-n` 启动过或 `/rename` 成固定名。
- **一直 UNKNOWN**：Claude Code UI 改版，idle 签名变了 → 更新 `patterns.conf`。
- **注入了但没反应**：看 `status` 的 FAILS 列；连续 3 次未生效会自动熔断禁用，去 TUI 重新启用并排查（比如消息词被 Claude Code 过滤）。

## 手动验收清单

1. 起一个试验会话：`claude -n never-giveup-test`，让它干个长活（比如「持续重构直到我说停」）。
2. TUI 添加监控，`dry_run=y`。
3. `claude_never_giveup.py once`，日志应出现 `state=IDLE` + `[dry-run] 本应注入「继续」`。
4. TUI 编辑把 `dry_run` 改 0，等会话停到输入框 → 下一轮 interval 内应看到「已注入「继续」」，会话继续干活。
5. 让 Claude 停在权限确认弹窗 → 确认日志 `state=DIALOG，不打扰`。
6. `touch ~/.claude-never-giveup/stop` → daemon 1 秒内退出；`start` 后恢复。

## 已知限制

- 依赖 Claude Code TUI 文本特征（`❯`、`esc to interrupt` 等）；改版后安全落到 UNKNOWN 不动，需更新 patterns.conf。
- 读屏与注入之间有毫秒级窗口；注入落点若恰逢弹窗，文本会作为待输入排队，风险低，熔断兜底。
- 只管「正常停止」：进程崩溃、限流横幅、断流不在职责内（那是 [claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) 的领域，两者可叠加使用）。
- 完成一轮到下一轮注入之间最长空转一个 `interval`（默认 5 分钟，想要更跟手就调小）。

## 开发

```bash
/usr/bin/python3 -m unittest discover -s test -v   # 66 个用例
```

- Python 3.9+ 兼容（macOS 自带 3.9.6 验证），stdlib only。
- 历史版本：v1.0.0 为 bash 实现，v2.0.0 起为 Python 重写，2026-09-07 由 claude-keepalive 更名为 claude-never-giveup（git 历史保留原名）。
- 设计文档：`docs/specs/2026-09-06-claude-keepalive-design.md`（含 §14 Python 修订）；移植计划：`docs/plans/2026-09-07-python-port.md`。
