# claude-keepalive 设计 Spec

日期：2026-09-06
状态：已与用户确认（brainstorming 收敛版）

## 1. 背景与目标

用户在 iTerm2 里以 `claude -n <名字>` 方式运行若干长跑 Claude Code 会话，希望这些会话
「永不停止，一直干」：只要会话正常完成一轮、停在输入提示符，就自动注入一个继续词
（默认「继续」），让它接着跑。

工具形态：一个 macOS 下的 bash 守护脚本（单文件 + 配置目录），零外部依赖
（只用 bash 3.2+ / osascript / macOS 自带工具），通过 iTerm2 AppleScript 完成
定位、状态读取与文本注入，不需要抢焦点、不依赖 tmux。

## 2. 范围

**范围内**

- 按 Claude Code 会话名（`claude -n` 启动名）定位 iTerm2 中的会话
- 单守护进程同时监控多个会话，每个监控独立配置（间隔/继续词/dry_run/开关）
- 正常 Stop（idle）后注入继续词；busy / 弹窗时不打扰
- 熔断、dry-run、全局停止开关、日志、launchd 常驻
- TUI（纯 bash）：增删改监控、实时状态、启停 daemon、看日志

**范围外（明确排除）**

- 进程崩溃/退出后的自动重启或 resume
- 断网、终端假死等极端场景的恢复
- 限流/5xx 等 API 错误的等待与重试（claude-auto-retry 的领域）
- Stop Hook（轮询模型下不需要；连续 block 上限问题不存在）

## 3. 总体架构

```text
claude-keepalive.sh（单进程 daemon）
  └── tick 每 15s
        ├── 重读 monitors/ 目录（新增监控免重启）
        ├── 检查 stop 刹车文件
        └── 对每个 enabled 且到期的监控执行一轮检查：
              ① ps 找 claude 进程（-n/--name/--resume/-r <名字>）→ tty
              ② iTerm2 按 tty 匹配 session（降级：按会话名扫内容）
              ③ 读 session contents 尾部 → 分类 BUSY/DIALOG/IDLE/UNKNOWN
              ④ 仅 IDLE 时注入 write text <继续词>
              ⑤ marker 确认 + 熔断计数
```

多目标不开子进程：5 分钟级轮询下串行足够，状态天然聚合。

## 4. 目录与配置

运行时目录 `~/.claude-keepalive/`：

```text
monitors/<名字>.conf    # 每监控一份，shell key=value，原子写
state/<名字>.state      # 运行时状态（daemon 每轮落盘，status/TUI 直接读）
keepalive.log           # 主日志（>5MB 轮转保留一档）
keepalive.conf          # 可选全局覆盖：tick / breaker_limit / log_file
stop                    # 全局刹车文件，daemon 见到即退出
daemon.pid / daemon.out
```

`<名字>.conf` 字段：`session_name`（必填，限 `[A-Za-z0-9._-]`，无空格）、
`message`（默认 `继续`）、`interval`（默认 300 秒）、`enabled`（默认 1）、
`dry_run`（默认 0）。

状态文件字段（前缀 `ST_`，eval 读取）：`last_state / last_check / last_inject /
awaiting / consec_fail`。

## 5. 定位链路（名字 → iTerm2 session）

1. `ps ax -o pid=,tty=,args=`，行需同时满足：含 `claude`、参数含
   ` -n <名> ` / ` --name <名> ` / ` --name=<名> ` / ` --resume <名> ` / ` -r <名> `、
   tty 列非 `??`（排除后台 `-p` 会话）、非本工具自身进程。
2. 匹配数 0 → `NO_SESSION`（记日志，跳过）；>1 → `AMBIGUOUS`（记 ERROR，跳过，
   宁可漏不可错）；=1 → tty。
3. tty（`ttysNNN` → `/dev/ttysNNN`）交给 AppleScript 遍历 iTerm2
   windows/tabs/sessions 按 `tty of session` 精确匹配，读 `contents of session`。
4. 降级路径（tty 匹配不到 session 时）：遍历所有 session 的 contents，
   匹配输入框附近出现的会话名（覆盖中途 `/rename` 的情况）；命中 0 或 >1 同样跳过。

## 6. 状态判定

取 contents 尾部 40 行，防御性去 ANSI（perl），按下述顺序判定，**只有明确 IDLE 才注入**：

| 状态 | 判定（尾部窗口） | 动作 |
|---|---|---|
| BUSY | 末 8 行命中 busy 正则（如 `esc to interrupt`） | 不动 |
| DIALOG | 末 12 行命中弹窗正则（`Yes, and auto-accept`、`Do you want to`、`❯ 1.` 编号选项等） | 不动，记日志 |
| IDLE | 末 12 行的非空行中，末 6 行内存在整行恰为 `❯`（trim 后）的行 | 注入 |
| UNKNOWN | 其余（含输入框有草稿 `❯ xxx` 的情况） | 不动，记日志 |

正则清单内建默认值，可被 `~/.claude-keepalive/patterns.conf`（定义
`BUSY_PATTERNS/DIALOG_PATTERNS` 数组）整体覆盖，UI 改版只改配置。

输入框有用户草稿时绝不算 IDLE——避免替用户提交半截话。

## 7. 注入与防失控

- 注入：`osascript ... tell session ... write text "<继续词>"`（对指定 session，
  自带回车，不抢焦点）。`dry_run=1` 只记日志不注入。
- marker 确认：注入后置 `awaiting=1`；后续检查若见 BUSY → 注入生效，清零失败计数。
  若再次 IDLE：在 contents 末 100 行找 `> <继续词>` 用户消息行——找到说明上一条已
  落地被消化（正常续跑循环），重置计数并继续注入；找不到视为未落地，`consec_fail++`。
- 熔断：`consec_fail >= 3`（全局可配）→ 自动把该监控 `enabled=0` 写回 conf，
  ERROR 级日志，其余监控不受影响。TUI 里可重新启用。
- 全局刹车：`touch ~/.claude-keepalive/stop`，daemon 下一 tick 退出（exit 0，
  配合 launchd `KeepAlive.SuccessfulExit=false` 不复活）；`start` 会清掉该文件。

## 8. 守护进程生命周期

子命令：

```text
setup    # TUI
start    # 清 stop 文件；launchd 已装则 kickstart，否则 nohup 起 daemon
stop     # touch stop + TERM pid
status   # 非交互状态表（读 conf + state）
once     # 对全部到期监控跑一轮（调试；daemon 在跑时警告后仍执行）
daemon   # 前台循环（launchd 与 --foreground 调试用）
install / uninstall   # 生成/移除 LaunchAgent plist 并加载/卸载
```

- 单实例：pid 文件 noclobber 原子创建 + `kill -0` 存活检查。
- 信号：TERM/INT → 清理 pid、exit 0。
- launchd plist：`RunAtLoad=true`，`KeepAlive={SuccessfulExit=false}`（仅非零退出才
  拉起），`ProgramArguments=[/bin/bash, <脚本绝对路径>, daemon]`，输出重定向
  `daemon.out`。

## 9. TUI（`setup`，纯 bash + ANSI，无 gum/fzf）

- 主菜单：监控列表（实时）/ 添加监控 / 编辑监控 / 启停 daemon / 查看日志 / 退出。
- 实时列表：每 5 秒重绘状态表（名称、enabled、状态、上次注入、失败计数），q 返回。
- 添加：自动 `ps` 扫描在跑的 `claude -n` 会话列出供选（含手动输入兜底）→
  注入词[继续] → 间隔[300] → dry_run[n] → 写 conf。重名询问覆盖。
- 编辑：选中后逐项重问（回车保留原值），含启停与删除（删除需确认）。
- 原则：编辑做「逐项重问」而非字段级表单（纯 bash 成本/收益考量，已确认）。
- Ctrl-C trap 恢复终端（光标/回显）。

## 10. 日志

`[YYYY-MM-DD HH:MM:SS] [LEVEL] [monitor] message`。每次检查记 INFO（含判定结果与
命中规则）；状态变化、注入、熔断、错误必记。>5MB 轮转为 `.log.1`（保留一档）。

## 11. 错误处理

- iTerm2 未运行 / osascript 报错：记日志，状态 `NO_ITERM`，本轮跳过。
- osascript 权限错误（-1743 / not authorized）：ERROR 日志并给出
  「系统设置 → 隐私与安全性 → 自动化 → 允许控制 iTerm2」指引。
- conf 非法（缺 session_name、名字含空格等）：记 ERROR，跳过该监控。
- daemon 循环内单监控异常不中断整体（无 `set -e`，逐 target 捕获）。
- 所有 conf/state 写入走 tmp+mv 原子写。

## 12. 测试策略

- `test/test.sh`（自带微型断言框架）：以 `KEEPALIVE_TEST_MODE=1` source 主脚本，
  覆盖纯函数——四态分类（含草稿输入、footer 在 ❯ 下方等边界 fixture）、
  ps 行解析/名字提取、conf 读写与默认值、状态序列化往返。
- iTerm2 集成（读 contents / write text / 权限错误）在本机以检测类调用实机验证；
  真实注入验证留一条用户手动清单（README）。
- `bash -n` 语法检查 + bash 3.2 兼容（不使用 `declare -A`、`printf %()T` 等 4.x 特性）。

## 13. 已知限制

- 依赖当前 Claude Code TUI 的文本特征（`❯`、`esc to interrupt` 等）；改版后落到
  UNKNOWN 安全不动，需更新 patterns.conf。
- 读 contents / 注入之间存在毫秒级 TOCTOU 窗口；注入落点若恰逢弹窗，文本会作为
  待选输入排队，风险低，marker 确认与熔断兜底。
- 只处理「正常停止」；进程崩溃、限流横幅、断流不在本工具职责内。
- 会话完成一轮与下一轮注入之间最长空转一个 interval（默认 5 分钟，可调小）。
- 「永不停止」会持续消耗额度；熔断、dry_run、stop 文件、按监控禁用是四层刹车。
