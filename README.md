# claude-never-giveup

> Never gonna give you up, never gonna let you down.
> —— 这个项目名等这行歌词已经很久了

![platform](https://img.shields.io/badge/platform-macOS-black?logo=apple)
![python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![dependencies](https://img.shields.io/badge/dependencies-0-blue)
![tests](https://img.shields.io/badge/tests-70%20passed-brightgreen)
![license](https://img.shields.io/badge/license-MIT-yellow)
![监工](https://img.shields.io/badge/%E7%9B%91%E5%B7%A5-%E6%B0%B8%E4%B8%8D%E4%B8%8B%E7%8F%AD-red)
![摸鱼检测](https://img.shields.io/badge/%E6%91%B8%E9%B1%BC%E6%A3%80%E6%B5%8B-%E9%9B%B6%E5%AE%B9%E5%BF%8D-orange)

给你的 Claude Code 会话雇一个**永不下班的赛博监工**：它定时巡查跑在 iTerm2 里的指定会话，一旦发现 Claude 干完一轮、正瘫在输入框上等你回来打字，就替你敲下继续词（默认就是「继续」二字）。

事情通常是这样的：你给 Claude 派了个大活，去倒了杯咖啡。回来一看——它二十分钟前就干完了，正对着空空的提示符发呆，而你不在的这段时间，项目进度就停在那里。你需要的从来不是更强的模型，是一个坐在屏幕前替你按「继续」的人。

**本工具就是这个岗位的永久员工。** 单文件 Python（stdlib only），macOS 自带的 `/usr/bin/python3` 直接跑，零外部依赖；日常操作全在内置 TUI 里完成——增删改监控、启停 daemon、看状态、翻日志，不用手改任何文件。

## 快速开始：三步上岗

```bash
cd claude-never-giveup
/usr/bin/python3 claude_never_giveup.py setup
```

1. 选 **`2) 添加监控`** —— 告诉监工盯谁。会自动扫描在跑的命名会话供选；没扫到就选「手动输入会话名」，填 iTerm2 标签上显示的会话名（如 `aidoc-bugfix`）。注入词 / 间隔 / dry_run 一路回车取默认值即可。
2. 选 **`4) daemon 控制`** → `1` 启动。监工开始巡逻。
3. 选 **`1) 监控列表`** 看实时状态（每 5 秒刷新一次岗哨表）。

**新人先实习，再转正**：第一次建议把 dry_run 留开（`y`）——监工只动嘴不动手，在日志里喊「本应注入」但不真敲。等你在列表里确认 `state=IDLE`、日志出现 `[dry-run] 本应注入「继续」` 之后，进「编辑」把 dry_run 关掉，正式上岗。

## TUI 功能详解：监工管理后台

主菜单（全键盘操作，`q` 退出）：

```text
╭── claude-never-giveup v2.2.0 ──────────────╮
│  daemon: ● 运行中 (pid 16747)              │
│                                            │
│  1)  监控列表（实时刷新）                   │
│  2)  添加监控                              │
│  3)  编辑 / 启停 / 删除监控                 │
│  4)  daemon 控制（启动 / 停止 / launchd）   │
│  5)  查看日志尾部                          │
│                                            │
│  q)  退出                                  │
╰────────────────────────────────────────────╯
```

**1) 监控列表（实时）** —— 每 5 秒自动重绘一次岗哨表（回车立即刷新）：

```text
NAME               ON  STATE           LAST_CHECK           LAST_INJECT          FAIL
aidoc-bugfix       1   IDLE            09-07 12:53:04       09-07 12:48:02       0
daemon: ● 运行中 (pid 16747)
```

`STATE` 的含义见下文「状态判定」；`FAIL` 是注入未生效的计数，攒到 3 就熔断该监控（监工喊了三声没人应，认定这岗出事了，自动下班）。`q`+回车 返回。

**2) 添加监控** —— 招一名新监工。自动扫描在跑的 `claude -n` 命名会话列出供选（含 tty 信息）；没扫到就选手动输入。随后依次过一遍用工条款：

```text
注入词 [继续]:        ← 回车用默认，可改成任意催促语
检测间隔（秒） [300]: ← 想更跟手就调小（如 60）
dry_run 模式 [n]:     ← 想先实习（只喊不做）就输 y
```

重名会先确认再覆盖（不悄悄顶掉老员工）。

**3) 编辑 / 启停 / 删除监控** —— 调岗管理。选中后进入子菜单，三个动作一级可达：

- **编辑配置**：逐项重问，**回车 = 保留当前值**（session_name、注入词、间隔、`enabled(1/0)`、`dry_run(1/0)`）；
- **禁用 / 启用**：一键切换。被熔断下班的监工，在这里一键返岗；
- **删除**：确认后连配置带状态一起清（不影响会话本身）。

**4) daemon 控制（启动 / 停止 / launchd）** —— 考勤管理。显示 daemon 运行状态和 launchd 安装情况，四个动作：启动 / 停止 / 安装 launchd 常驻（开机自启，机器重启监工自动返岗）/ 卸载。

**5) 查看日志尾部** —— 翻监工的工作记录本，最近 30 行（注入、状态变化、熔断、错误都在里面）。

**生效时机**：监控的增删改在 daemon 下一个 tick（15 秒内）自动生效，**无需重启**——招人裁人不用关公司。

## 命令一览（写脚本用）

不想点菜单？TUI 之外的全部能力都有对应子命令：

| 命令 | 作用 |
|---|---|
| `setup` | TUI（主入口，见上） |
| `start` | 启动 daemon（清除 stop 文件；装了 launchd 则经 launchd 拉起） |
| `stop` | 停止 daemon（放 stop 文件 + SIGTERM，秒级退出） |
| `status` | 非交互状态表（同 TUI 列表的数据） |
| `once` | 立即检查一轮全部监控（急性子专用，不用等巡逻间隔） |
| `daemon` | 前台运行 daemon（launchd / 调试用） |
| `install` | 安装 launchd 常驻（脚本部署到 `~/.claude-never-giveup/bin/`） |
| `uninstall` | 卸载 launchd |

## 监工怎么认人（会话定位）

两条链路，前者优先：

1. **ps 参数匹配**：`claude -n <名字>`（或 `--name` / `--resume` / `-r`）启动的进程 → tty → iTerm2 session。
2. **iTerm2 标题兜底**：ps 匹配不到时，按 iTerm2 会话标题包含会话名定位——所以**中途 `/rename` 过、或根本没用 `-n` 启动的会话也能找到**（Claude Code 会把会话名写进终端标题）。

认出 0 个 → `NO_SESSION`（人压根没来上班）；认出多个 → `AMBIGUOUS`（一喊「继续」仨人回头）——**宁可漏不可错，一律不注入**。

> 长跑会话建议用 `claude -n <固定名>` 启动，或进去后 `/rename <固定名>`——给监工一张稳定的工作证，省得它天天认错人。

## 状态判定：监工的看岗手册

每轮检查读一遍 iTerm2 session 内容尾部，按序对号入座：

| 状态 | 特征 | 动作 |
|---|---|---|
| BUSY | spinner / `esc to interrupt` / `Retrying`、`Reconnecting` 横幅 | 不动（人家在干活） |
| DIALOG | 权限确认、编号选项菜单（`❯ 1.` 式） | 不动（注入会打乱菜单） |
| IDLE | 底部空输入框（整行恰为 `❯`，footer 再高也认得出） | **注入继续词** |
| UNKNOWN | 其余（含输入框有草稿） | 不动（绝不替你提交半截话） |

IDLE 时的注入决策：首次直接注入；已注入过又见 IDLE 时，监工不无脑连喊——先用三路信号确认上次**真的落地**了（90 秒 peek 窗口抓 BUSY → 屏幕找 `> 继续` 消息行 → 查该会话的 transcript 文件），确认则继续注入；三路全失败才计一次失败，连续 3 次熔断。委派型会话（主提示符长期空闲、活儿在后台 agent）因此也不会被冤枉熔断。

Claude Code 改版导致监工看不懂岗了？改 `~/.claude-never-giveup/patterns.conf`（**JSON**：`{"BUSY_PATTERNS": [...], "DIALOG_PATTERNS": [...]}`）——给监工换副眼镜，不用改代码。

## 防失控：监工也要守劳动法（四层刹车）

1. **dry_run（实习模式）**：每监控可配（TUI 添加/编辑里设置），只记日志不注入——嘴上喊，不动手。
2. **熔断（自动下班）**：连续 3 次（`BREAKER_LIMIT`）注入未生效 → 自动 `enabled=0` 并记 ERROR 日志，其他监工不受牵连；TUI 里重新启用。
3. **按监控禁用（强制休假）**：TUI 编辑里 `enabled=0`。
4. **全局刹车（一键解散）**：`touch ~/.claude-never-giveup/stop`，daemon 1 秒内退出；`start` 会清除。

> ⚠️ **永动机烧的不是煤，是你的额度💸**。「永不停止」字面意义上意味着持续消耗 quota——任务真的做完后，记得 `stop` 或禁用监控，别让监工对着空气喊「继续」。

## 配置：绕过 HR 直改档案（高级路径）

日常用 TUI 即可；以下为直接编辑文件的说明。运行时目录 `~/.claude-never-giveup/`：

```text
monitors/<名字>.conf   # 每监控一份（key=value，人类可读）：session_name / message / interval / enabled / dry_run
state/<名字>.json      # 运行时状态（JSON，daemon 落盘，status/TUI 直接读）
never-giveup.log       # 日志（>5MB 轮转保留一档）
never-giveup.conf      # 可选全局（key=value）：TICK_SECONDS / BREAKER_LIMIT / LOG_MAX_BYTES
patterns.conf          # 可选（JSON）：覆盖 BUSY_PATTERNS / DIALOG_PATTERNS
stop                   # 刹车文件（存在即停）
```

手写一个监控配置示例：

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

launchd 语义：`KeepAlive.SuccessfulExit=false`——daemon 正常退出（含 stop 刹车）不复活（正常下班不返聘），仅异常退出才拉起（工伤才抢救）。`install` 会把脚本拷到 `~/.claude-never-giveup/bin/`（launchd 进程没有 `~/Desktop` 等 TCC 目录的访问权），plist 用 `/usr/bin/python3` 调它；**改了主脚本要重跑 `install`**。

## 故障排查：监工不干活了？

**Q：日志报 `osascript 失败 ... -1743 / not authorized`**
A：监工没拿到机房的门禁卡。系统设置 → 隐私与安全性 → 自动化 → 给你的终端 App 勾上「控制 iTerm2」。

**Q：一直 `NO_SESSION`，说人没来？**
A：会话名和 iTerm2 标题对不上号。跑 `once` 看日志；确认会话用 `-n` 启动过，或 `/rename` 成固定名。

**Q：一直 `UNKNOWN`，装看不见？**
A：不是装的，是真看不懂了——Claude Code UI 改版，idle 签名变了 → 更新 `patterns.conf`。

**Q：注入了但 Claude 没反应？**
A：看 `status` 的 FAIL 列；连续 3 次未生效会自动熔断禁用。去 TUI 重新启用并排查（比如消息词被 Claude Code 过滤——监工喊话被保安拦下了）。

## 手动验收清单：新监工入职考试

1. 起一个试验会话：`claude -n never-giveup-test`，让它干个长活（比如「持续重构直到我说停」）。
2. TUI 添加监控，`dry_run=y`。
3. `claude_never_giveup.py once`，日志应出现 `state=IDLE` + `[dry-run] 本应注入「继续」`。
4. TUI 编辑把 `dry_run` 改 0，等会话停到输入框 → 下一轮 interval 内应看到「已注入「继续」」，会话继续干活。
5. 让 Claude 停在权限确认弹窗 → 确认日志 `state=DIALOG，不打扰`（监工很懂规矩）。
6. `touch ~/.claude-never-giveup/stop` → daemon 1 秒内退出；`start` 后恢复。

## 已知限制（监工的劳动合同附件）

- 监工靠读屏认状态（`❯`、`esc to interrupt` 这些文本特征）；Claude Code 改版后会安全落到 UNKNOWN 装死不动，需更新 patterns.conf。
- 读屏与注入之间有毫秒级窗口；注入落点若恰逢弹窗，文本会作为待输入排队，风险低，熔断兜底。
- 只管「正常停止」：进程崩溃、限流横幅、断流不归它管（那是 [claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) 的辖区，两者可叠加使用，双保险）。
- 巡逻有间隔：完成一轮到下一次注入之间最长空转一个 `interval`（默认 5 分钟，嫌它懒就调小）。

## 开发

监工也要持证上岗：

```bash
/usr/bin/python3 -m unittest discover -s test -v   # 70 个用例
```

- Python 3.9+ 兼容（macOS 自带 3.9.6 验证），stdlib only。
- 历史版本：v1.0.0 为 bash 实现，v2.0.0 起为 Python 重写，2026-09-07 由 claude-keepalive 更名为 claude-never-giveup（git 历史保留原名）。
- 设计文档：`docs/specs/2026-09-06-claude-keepalive-design.md`（含 §14 Python 修订）；移植计划：`docs/plans/2026-09-07-python-port.md`。

## 许可

MIT。监工免费领走，想雇几个雇几个。
