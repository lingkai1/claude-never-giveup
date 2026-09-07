#!/usr/bin/env python3
# claude-keepalive — 让指定名字的 Claude Code 会话（iTerm2）持续工作
# 设计文档: docs/specs/2026-09-06-claude-keepalive-design.md（v2 Python 版，见 §14 修订记录）
# 依赖：macOS 自带 python3（3.9+），stdlib only。

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

VERSION = "2.0.0"
SCRIPT_PATH = Path(__file__).resolve()

# ---------- 1. 路径与默认值 ----------
BASE_DIR = Path(os.environ.get("KEEPALIVE_BASE_DIR") or Path.home() / ".claude-keepalive")
MON_DIR = BASE_DIR / "monitors"
STATE_DIR = BASE_DIR / "state"
LOG_FILE = BASE_DIR / "keepalive.log"
CONF_FILE = BASE_DIR / "keepalive.conf"
PATTERNS_FILE = BASE_DIR / "patterns.conf"
STOP_FILE = BASE_DIR / "stop"
PID_FILE = BASE_DIR / "daemon.pid"
DAEMON_OUT = BASE_DIR / "daemon.out"
LAUNCHD_LABEL = "com.lingkai.claude-keepalive"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / (LAUNCHD_LABEL + ".plist")

TICK_SECONDS = 15
BREAKER_LIMIT = 3
PEEK_WINDOW_SECONDS = 90
LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_INTERVAL = 300
DEFAULT_MESSAGE = "继续"

BUSY_PATTERNS = [r"[Ee]sc to interrupt", r"· *[Rr]etrying", r"[Rr]econnecting"]
DIALOG_PATTERNS = [
    r"Yes, and auto-accept",
    r"No, and tell Claude",
    r"Do you want to",
    r"[Ww]ould you like to",
    r"Enter to confirm",
    r"Choose an option",
    r"❯ *[0-9]+\.",
]

SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?=><]*[A-Za-z]"      # CSI 序列
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\)"  # OSC 序列
    r"|\x1b[()][0-9A-Za-z]"            # 字符集选择
)

# ---------- 2. 通用 helpers ----------
def now() -> int:
    return int(time.time())


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def ensure_dirs() -> None:
    MON_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def log(level: str, monitor: str, message: str) -> None:
    ensure_dirs()
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1"))
    except OSError:
        pass
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts()}] [{level}] [{monitor}] {message}\n")


def valid_session_name(name: str) -> bool:
    return bool(name) and bool(SESSION_NAME_RE.match(name))


# ---------- 3. 配置与状态 ----------
@dataclass
class MonitorConf:
    session_name: str = ""
    message: str = DEFAULT_MESSAGE
    interval: int = DEFAULT_INTERVAL
    enabled: bool = True
    dry_run: bool = False

    @classmethod
    def load(cls, path: Path) -> Optional["MonitorConf"]:
        if not path.is_file():
            return None
        c = cls()
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "session_name":
                c.session_name = v
            elif k == "message":
                c.message = v
            elif k == "interval" and v.isdigit():
                c.interval = int(v)
            elif k == "enabled":
                c.enabled = v != "0"
            elif k == "dry_run":
                c.dry_run = v == "1"
        if not valid_session_name(c.session_name):
            return None
        c.interval = max(10, c.interval)
        return c

    def save(self, path: Path) -> None:
        atomic_write(path, (
            f"session_name={self.session_name}\n"
            f"message={self.message}\n"
            f"interval={self.interval}\n"
            f"enabled={1 if self.enabled else 0}\n"
            f"dry_run={1 if self.dry_run else 0}\n"
        ))


@dataclass
class MonitorState:
    last_state: str = "NEVER"
    last_check: int = 0
    last_inject: int = 0
    awaiting: int = 0
    consec_fail: int = 0
    peek_until: int = 0

    @classmethod
    def load(cls, name: str) -> "MonitorState":
        p = STATE_DIR / f"{name}.json"
        if not p.is_file():
            return cls()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            s = cls()
            s.last_state = str(d.get("last_state", s.last_state))
            s.last_check = int(d.get("last_check", 0))
            s.last_inject = int(d.get("last_inject", 0))
            s.awaiting = int(d.get("awaiting", 0))
            s.consec_fail = int(d.get("consec_fail", 0))
            s.peek_until = int(d.get("peek_until", 0))
            return s
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, name: str) -> None:
        ensure_dirs()
        atomic_write(STATE_DIR / f"{name}.json", json.dumps({
            "last_state": self.last_state,
            "last_check": self.last_check,
            "last_inject": self.last_inject,
            "awaiting": self.awaiting,
            "consec_fail": self.consec_fail,
            "peek_until": self.peek_until,
        }, ensure_ascii=False, indent=1))

    def touch(self, name: str, state: str) -> None:
        self.last_state = state
        self.last_check = now()
        self.save(name)
        log("INFO", name, f"state={state}")


def load_global_conf() -> None:
    """keepalive.conf（key=value）与 patterns.conf（JSON）覆盖全局默认。"""
    global TICK_SECONDS, BREAKER_LIMIT, LOG_MAX_BYTES, BUSY_PATTERNS, DIALOG_PATTERNS
    if CONF_FILE.is_file():
        for raw in CONF_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "TICK_SECONDS" and v.isdigit():
                TICK_SECONDS = max(1, int(v))
            elif k == "BREAKER_LIMIT" and v.isdigit():
                BREAKER_LIMIT = max(1, int(v))
            elif k == "LOG_MAX_BYTES" and v.isdigit():
                LOG_MAX_BYTES = int(v)
    if PATTERNS_FILE.is_file():
        try:
            d = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
            if isinstance(d.get("BUSY_PATTERNS"), list):
                BUSY_PATTERNS = [str(x) for x in d["BUSY_PATTERNS"]]
            if isinstance(d.get("DIALOG_PATTERNS"), list):
                DIALOG_PATTERNS = [str(x) for x in d["DIALOG_PATTERNS"]]
        except (OSError, ValueError):
            pass


# ---------- 4. 状态分类与注入决策（纯函数） ----------
def classify_tail(text: str) -> str:
    """末 40 行窗口 → BUSY | DIALOG | IDLE | UNKNOWN（顺序判定，仅 IDLE 注入）。"""
    lines = strip_ansi(text).splitlines()[-40:]
    tail12 = lines[-12:]
    tail8 = tail12[-8:]
    nonempty = [l for l in tail12 if l.strip()]
    last6 = nonempty[-6:]
    if any(re.search(p, "\n".join(tail8)) for p in BUSY_PATTERNS):
        return "BUSY"
    if any(re.search(p, "\n".join(tail12)) for p in DIALOG_PATTERNS):
        return "DIALOG"
    if any(l.strip() == "❯" for l in last6):
        return "IDLE"
    return "UNKNOWN"


def decide_action(awaiting: int, consec: int, content: str, message: str) -> str:
    """仅在 classify_tail == IDLE 时调用。"""
    if awaiting == 0:
        return "INJECT_FIRST"
    # marker 在整个可见内容里找（提交的消息行可能被任务面板挤出短窗口）
    if f"> {message}" in strip_ansi(content):
        return "INJECT_AGAIN"
    if consec + 1 >= BREAKER_LIMIT:
        return "TRIP"
    return "INJECT_RETRY"


# ---------- 5. ps 解析与定位 ----------
def ps_claude_lines() -> List[str]:
    try:
        out = subprocess.run(
            ["ps", "ax", "-o", "pid=,tty=,args="],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    lines = []
    for l in out.splitlines():
        low = l.lower()
        if "claude" in low and "keepalive" not in low and "grep" not in low:
            lines.append(l)
    return lines


def ps_tty_of_line(line: str) -> str:
    parts = line.split(None, 2)
    return parts[1] if len(parts) >= 2 else ""


def ps_args_of_line(line: str) -> str:
    parts = line.split(None, 2)
    return parts[2] if len(parts) >= 3 else ""


def match_ps_line_for_session(line: str, name: str) -> Optional[str]:
    tty = ps_tty_of_line(line)
    if not tty or tty in ("?", "??"):
        return None
    padded = f" {ps_args_of_line(line)} "
    for pat in (f" -n {name} ", f" --name {name} ", f" --name={name} ",
                f" --resume {name} ", f" -r {name} "):
        if pat in padded:
            return tty
    return None


def extract_session_name_from_args(args: str) -> str:
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok in ("-n", "--name", "--resume", "-r"):
            return tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.startswith("--name="):
            return tok[len("--name="):]
    return ""


def find_claude_tty_for_session(name: str) -> Tuple[Optional[str], int]:
    """返回 (tty 或 None, 匹配进程数)。count==1 时返回 /dev/ttysN。"""
    found: Optional[str] = None
    count = 0
    for line in ps_claude_lines():
        tty = match_ps_line_for_session(line, name)
        if tty is not None:
            count += 1
            found = tty
    if count == 1:
        return f"/dev/{found}", 1
    return None, count


def scan_running_claude_sessions() -> List[Tuple[str, str]]:
    result = []
    for line in ps_claude_lines():
        tty = ps_tty_of_line(line)
        if not tty or tty in ("?", "??"):
            continue
        nm = extract_session_name_from_args(ps_args_of_line(line))
        if nm and valid_session_name(nm):
            result.append((nm, tty))
    return result


# ---------- 6. iTerm2 AppleScript ----------
def run_osascript(script: str, *args: str) -> Tuple[str, str, int]:
    try:
        p = subprocess.run(
            ["/usr/bin/osascript", "-e", script, *args],
            capture_output=True, text=True, timeout=30,
        )
        return p.stdout.rstrip("\n"), p.stderr.strip(), p.returncode
    except subprocess.TimeoutExpired:
        return "", "osascript 超时（30s）", 2
    except OSError as e:
        return "", str(e), 2


_READ_SCRIPT = """
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
end run
"""


def iterm_read_contents(devtty: str) -> Tuple[int, str]:
    """rc 0 ok（返回内容）/ 1 notfound / 2 error（返回 err）。"""
    out, err, rc = run_osascript(_READ_SCRIPT, devtty)
    if rc != 0:
        return 2, err
    if out == "KEEPALIVE_NOTFOUND":
        return 1, ""
    return 0, out


_WRITE_SCRIPT = """
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
end run
"""


def iterm_write_text(devtty: str, message: str) -> Tuple[int, str]:
    out, err, rc = run_osascript(_WRITE_SCRIPT, devtty, message)
    if rc != 0:
        return 2, err
    if out == "KEEPALIVE_NOTFOUND":
        return 1, ""
    return 0, ""


_SCAN_SCRIPT = """
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
end run
"""


def iterm_scan_tty_by_name(session_name: str) -> Tuple[bool, Tuple[int, List[str]]]:
    """按 iTerm2 会话标题定位（Claude Code 把会话名写进终端标题）。"""
    out, _err, rc = run_osascript(_SCAN_SCRIPT, session_name)
    if rc != 0:
        return False, (0, [])
    lines_ = out.splitlines()
    if not lines_ or not lines_[0].strip().isdigit():
        return False, (0, [])
    n = int(lines_[0])
    return True, (n, [l.strip() for l in lines_[1:] if l.strip()])


# ---------- 7. 单监控检查管线 ----------
def _locate_by_title(name: str, session_name: str) -> Tuple[Optional[str], Optional[str]]:
    """按 iTerm2 会话标题定位。返回 (tty, None) 或 (None, 状态码)。"""
    ok, (n, ttys) = iterm_scan_tty_by_name(session_name)
    if not ok:
        return None, "NO_ITERM"
    if n == 0:
        return None, "NO_SESSION"
    if n > 1:
        log("ERROR", name, f"按会话名扫描命中 {n} 个 iTerm2 session，跳过")
        return None, "AMBIGUOUS"
    return ttys[0], None


def do_inject(name: str, devtty: str, conf: MonitorConf, st: MonitorState) -> None:
    if conf.dry_run:
        log("INFO", name, f"[dry-run] 本应注入「{conf.message}」")
        st.awaiting = 1
        st.last_inject = now()
        st.peek_until = now() + PEEK_WINDOW_SECONDS
        return
    rc, err = iterm_write_text(devtty, conf.message)
    if rc == 0:
        st.awaiting = 1
        st.last_inject = now()
        st.peek_until = now() + PEEK_WINDOW_SECONDS
        log("INFO", name, f"已注入「{conf.message}」")
    else:
        log("ERROR", name, f"注入失败({rc}): {err}")


def check_monitor(name: str, peek: bool = False) -> None:
    conf_path = MON_DIR / f"{name}.conf"
    conf = MonitorConf.load(conf_path)
    if conf is None:
        log("ERROR", name, "conf 无效，跳过")
        return
    if not conf.enabled:
        return
    st = MonitorState.load(name)

    tty, count = find_claude_tty_for_session(conf.session_name)
    if count > 1:
        log("ERROR", name, f"多个进程匹配 session_name={conf.session_name}，跳过")
        st.touch(name, "AMBIGUOUS")
        return

    if tty is None:
        # ps 无匹配（未用 -n 启动等）→ 按 iTerm2 会话标题兜底定位
        tty2, code = _locate_by_title(name, conf.session_name)
        if code:
            st.touch(name, code)
            return
        rc, contents = iterm_read_contents(tty2)
        if rc != 0:
            st.touch(name, "SESSION_NOT_FOUND")
            return
        devtty = tty2
    else:
        rc, contents = iterm_read_contents(tty)
        if rc == 2:
            log("ERROR", name, f"osascript 失败: {contents}")
            if "-1743" in contents or "not authorized" in contents.lower():
                log("ERROR", name, "需授权：系统设置 → 隐私与安全性 → 自动化 → 允许控制 iTerm2")
            st.touch(name, "NO_ITERM")
            return
        if rc == 1:
            # ps 找到进程但 tty 匹配不到 iTerm2 session（别的终端 App 等）→ 标题兜底
            tty2, code = _locate_by_title(name, conf.session_name)
            if code:
                st.touch(name, code)
                return
            rc2, contents = iterm_read_contents(tty2)
            if rc2 != 0:
                st.touch(name, "SESSION_NOT_FOUND")
                return
            devtty = tty2
        else:
            devtty = tty

    state = classify_tail(contents)
    st.last_check = now()
    st.last_state = state

    if state == "BUSY":
        st.awaiting = 0
        st.consec_fail = 0
        if st.peek_until:
            log("INFO", name, "peek 窗口内确认注入生效（BUSY），清零计数")
            st.peek_until = 0
        log("INFO", name, "state=BUSY（注入已生效，计数清零）")
    elif state == "IDLE":
        if peek:
            # peek 只找 BUSY；IDLE 不注入不计数，等正式到期检查再判定
            log("INFO", name, "peek: 仍 IDLE，等待正式检查")
            st.save(name)
            return
        decision = decide_action(st.awaiting, st.consec_fail, contents, conf.message)
        if decision in ("INJECT_FIRST", "INJECT_AGAIN"):
            if decision == "INJECT_AGAIN":
                st.consec_fail = 0
            do_inject(name, devtty, conf, st)
        elif decision == "INJECT_RETRY":
            st.consec_fail += 1
            log("WARN", name, f"上次注入未见生效（第 {st.consec_fail}/{BREAKER_LIMIT} 次）")
            do_inject(name, devtty, conf, st)
        else:  # TRIP
            st.consec_fail += 1
            log("ERROR", name, f"连续 {st.consec_fail} 次注入未生效，熔断：自动禁用该监控")
            conf.enabled = False
            conf.save(conf_path)
    else:
        log("INFO", name, f"state={state}，不打扰")
    st.save(name)


# ---------- 8. daemon ----------
def daemon_pid() -> Optional[int]:
    try:
        p = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(p, 0)
        return p
    except OSError:
        return None


def daemon_run() -> int:
    ensure_dirs()
    load_global_conf()
    if STOP_FILE.exists():
        log("INFO", "daemon", "启动时存在 stop 文件，退出")
        return 0
    # 单实例：O_EXCL 原子创建 pid 文件
    try:
        fd = os.open(PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        old = daemon_pid()
        if old is not None:
            log("ERROR", "daemon", f"已有实例 pid={old}，退出")
            return 1
        PID_FILE.write_text(str(os.getpid()))  # 陈旧 pid 文件，覆盖

    stop_flag = {"v": False}

    def on_signal(signum, frame):
        stop_flag["v"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("INFO", "daemon", f"启动（pid {os.getpid()}，tick={TICK_SECONDS}s）")
    try:
        while True:
            if stop_flag["v"] or STOP_FILE.exists():
                log("INFO", "daemon", "收到停止信号/stop 文件，退出")
                return 0
            for conf_file in sorted(MON_DIR.glob("*.conf")):
                name = conf_file.stem
                mconf = MonitorConf.load(conf_file)
                if mconf is None:
                    log("ERROR", name, f"conf 无效：{conf_file}")
                    continue
                if not mconf.enabled:
                    continue
                st = MonitorState.load(name)
                if now() - st.last_check >= mconf.interval:
                    try:
                        check_monitor(name)
                    except Exception as e:  # 单监控异常不中断整体
                        log("ERROR", name, f"检查异常: {e!r}")
                elif st.peek_until > now():
                    # 注入后的 peek 窗口：每 tick 快速探测，抓 BUSY 以确认落地
                    try:
                        check_monitor(name, peek=True)
                    except Exception as e:
                        log("ERROR", name, f"peek 异常: {e!r}")
            # 1 秒切片睡眠：信号与 stop 文件的响应延迟不超过 1s
            deadline = time.time() + TICK_SECONDS
            while time.time() < deadline:
                if stop_flag["v"] or STOP_FILE.exists():
                    break
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


# ---------- 9. 命令 ----------
def launchd_loaded() -> bool:
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=30)
        return LAUNCHD_LABEL in (r.stdout or "")
    except (subprocess.TimeoutExpired, OSError):
        return False


def launchd_python() -> str:
    return "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


def cmd_start() -> int:
    ensure_dirs()
    STOP_FILE.unlink(missing_ok=True)
    p = daemon_pid()
    if p:
        print(f"daemon 已在运行 (pid {p})")
        return 0
    if launchd_loaded():
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        print("已通过 launchd 拉起")
    else:
        DAEMON_OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(DAEMON_OUT, "a", encoding="utf-8") as f:
            subprocess.Popen(
                [sys.executable, str(SCRIPT_PATH), "daemon"],
                stdout=f, stderr=f, start_new_session=True,
            )
        print("daemon 已后台启动")
    return 0


def cmd_stop() -> int:
    ensure_dirs()
    STOP_FILE.touch()
    p = daemon_pid()
    if p:
        try:
            os.kill(p, signal.SIGTERM)
            print(f"已通知 daemon (pid {p}) 退出")
        except OSError:
            print("daemon 未在运行（stop 文件已放置）")
    else:
        print("daemon 未在运行（stop 文件已放置）")
    return 0


def _fmt_time(epoch: int) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(epoch)) if epoch > 0 else "-"


def cmd_status() -> int:
    ensure_dirs()
    print(f"{'NAME':<18} {'ENABLED':<8} {'STATE':<15} {'LAST_CHECK':<20} {'LAST_INJECT':<20} FAILS")
    for conf_file in sorted(MON_DIR.glob("*.conf")):
        name = conf_file.stem
        conf = MonitorConf.load(conf_file)
        if conf is None:
            print(f"{name:<18} {'-':<8} CONF_INVALID")
            continue
        st = MonitorState.load(name)
        print(f"{conf.session_name:<18} {1 if conf.enabled else 0:<8} {st.last_state:<15} "
              f"{_fmt_time(st.last_check):<20} {_fmt_time(st.last_inject):<20} {st.consec_fail}")
    p = daemon_pid()
    print(f"daemon: running (pid {p})" if p else "daemon: stopped")
    return 0


def cmd_once() -> int:
    load_global_conf()
    if daemon_pid() is not None:
        print("警告：daemon 正在运行，状态文件并发写入（原子写，风险低）")
    any_mon = False
    for conf_file in sorted(MON_DIR.glob("*.conf")):
        any_mon = True
        name = conf_file.stem
        conf = MonitorConf.load(conf_file)
        if conf is None:
            print(f"[skip] {name}: conf 无效")
            continue
        if not conf.enabled:
            print(f"[skip] {name}: disabled")
            continue
        print(f"== 检查 {name}（session={conf.session_name}）")
        check_monitor(name)
    if not any_mon:
        print(f"没有监控配置：{MON_DIR} 下放 .conf，或运行 setup")
    return 0


# ---------- 10. launchd ----------
def cmd_install() -> int:
    ensure_dirs()
    # launchd 进程无 ~/Desktop 等 TCC 目录的访问权，部署一份到家目录再由 plist 指向它
    deploy_dir = BASE_DIR / "bin"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    deploy = deploy_dir / "claude_keepalive.py"
    shutil.copyfile(SCRIPT_PATH, deploy)
    deploy.chmod(0o755)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    pybin = launchd_python()
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{pybin}</string>
    <string>{deploy}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{DAEMON_OUT}</string>
  <key>StandardErrorPath</key><string>{DAEMON_OUT}</string>
</dict>
</plist>
"""
    LAUNCHD_PLIST.write_text(plist, encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCHD_PLIST)],
                       capture_output=True)
    ok = r.returncode == 0
    if not ok:
        r2 = subprocess.run(["launchctl", "load", "-w", str(LAUNCHD_PLIST)], capture_output=True)
        ok = r2.returncode == 0
    if ok:
        print(f"已安装并加载 LaunchAgent：{LAUNCHD_PLIST}")
        print(f"脚本已部署到 {deploy}（改主脚本后需重跑 install）")
    else:
        print(f"plist 已写入但加载失败，请手动执行：launchctl load {LAUNCHD_PLIST}")
    return 0


def cmd_uninstall() -> int:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "unload", "-w", str(LAUNCHD_PLIST)], capture_output=True)
    LAUNCHD_PLIST.unlink(missing_ok=True)
    print("已卸载 LaunchAgent")
    return 0


# ---------- 11. TUI ----------
def _ask(prompt: str, default: str) -> str:
    try:
        ans = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def tui_pick_monitor() -> Optional[str]:
    confs = sorted(MON_DIR.glob("*.conf"))
    if not confs:
        print("（暂无监控配置）")
        return None
    for i, p in enumerate(confs, 1):
        print(f"  {i}) {p.stem}")
    pick = _ask("选择编号", "1")
    if not pick.isdigit() or not (1 <= int(pick) <= len(confs)):
        print("无效编号")
        return None
    return confs[int(pick) - 1].stem


def tui_live_list() -> None:
    while True:
        print("\033[2J\033[H", end="")
        print("\033[1m== 监控列表（5s 刷新，q+回车 返回） ==\033[0m")
        cmd_status()
        print("\n(q 返回)")
        try:
            r, _, _ = select.select([sys.stdin], [], [], 5.0)
        except (OSError, ValueError):
            time.sleep(5)
            continue
        if r:
            line = sys.stdin.readline().strip().lower()
            if line == "q":
                break


def tui_add_monitor() -> None:
    print()
    print("\033[1m== 添加监控 ==\033[0m")
    print("扫描运行中的 claude 会话...")
    sessions = sorted(set(scan_running_claude_sessions()))
    names: List[Optional[str]] = []
    for nm, tty in sessions:
        print(f"  {len(names) + 1}) {nm}  (tty {tty})")
        names.append(nm)
    print(f"  {len(names) + 1}) 手动输入会话名")
    names.append(None)
    pick = _ask("选择", str(len(names)))
    if not pick.isdigit() or not (1 <= int(pick) <= len(names)):
        print("无效选择")
        return
    chosen = names[int(pick) - 1]
    if chosen is None:
        chosen = input("claude -n 的会话名: ").strip()
    if not valid_session_name(chosen):
        print("会话名只能含 [A-Za-z0-9._-]，已取消")
        return
    conf_path = MON_DIR / f"{chosen}.conf"
    if conf_path.exists():
        yn = _ask(f"监控 {chosen} 已存在，覆盖? [y/N]", "n")
        if not yn.lower().startswith("y"):
            print("已取消")
            return
    message = _ask("注入词", DEFAULT_MESSAGE)
    interval = _ask("检测间隔（秒）", str(DEFAULT_INTERVAL))
    dry = _ask("dry_run 模式 [y/n]", "n")
    if not interval.isdigit():
        interval = str(DEFAULT_INTERVAL)
    conf = MonitorConf(
        session_name=chosen, message=message, interval=int(interval),
        enabled=True, dry_run=dry.lower().startswith("y"),
    )
    conf.save(conf_path)
    print(f"已写入 {conf_path}（daemon 在跑则下一 tick 自动生效）")


def tui_edit_monitor() -> None:
    print()
    print("\033[1m== 编辑 / 删除监控 ==\033[0m")
    name = tui_pick_monitor()
    if not name:
        return
    conf_path = MON_DIR / f"{name}.conf"
    conf = MonitorConf.load(conf_path)
    if conf is None:
        print("conf 无效")
        return
    print("回车 = 保留当前值")
    sn = _ask("session_name", conf.session_name)
    msg = _ask("注入词", conf.message)
    itv = _ask("间隔（秒）", str(conf.interval))
    en = _ask("enabled(1/0)", "1" if conf.enabled else "0")
    dr = _ask("dry_run(1/0)", "1" if conf.dry_run else "0")
    if not sn or not valid_session_name(sn):
        print("会话名非法，保留原名")
        sn = conf.session_name
    conf.session_name = sn
    conf.message = msg
    conf.interval = int(itv) if itv.isdigit() else conf.interval
    conf.enabled = en == "1"
    conf.dry_run = dr == "1"
    conf.save(conf_path)
    print(f"已更新 {conf_path}")
    yn = _ask("是否删除该监控? [y/N]", "n")
    if yn.lower().startswith("y"):
        conf_path.unlink(missing_ok=True)
        (STATE_DIR / f"{sn}.json").unlink(missing_ok=True)
        print("已删除")


def tui_daemon_menu() -> None:
    print()
    p = daemon_pid()
    print(f"daemon: 运行中 (pid {p})" if p else "daemon: 未运行")
    print("  1) 启动  2) 停止  3) 安装 launchd 常驻  4) 卸载 launchd  5) 返回")
    c = _ask("选择", "5")
    if c == "1":
        cmd_start()
    elif c == "2":
        cmd_stop()
    elif c == "3":
        cmd_install()
    elif c == "4":
        cmd_uninstall()


def tui_show_logs() -> None:
    print()
    if not LOG_FILE.is_file():
        print("暂无日志")
        return
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    for l in lines[-30:]:
        print(l)
    print(f"\033[2m（完整日志：{LOG_FILE}）\033[0m")


def tui_main() -> int:
    ensure_dirs()
    while True:
        print("\033[2J\033[H", end="")
        print(f"\033[1m== claude-keepalive v{VERSION} ==\033[0m")
        print("  1) 监控列表（实时刷新）")
        print("  2) 添加监控")
        print("  3) 编辑 / 删除监控")
        print("  4) 启动 / 停止 daemon")
        print("  5) 查看日志尾部")
        print("  6) 退出")
        choice = _ask("选择", "6")
        if choice == "1":
            tui_live_list()
        elif choice == "2":
            tui_add_monitor()
        elif choice == "3":
            tui_edit_monitor()
        elif choice == "4":
            tui_daemon_menu()
        elif choice == "5":
            tui_show_logs()
        elif choice == "6":
            break
    return 0


# ---------- 12. 入口 ----------
USAGE = f"""claude-keepalive v{VERSION} — 让指定名字的 Claude Code 会话（iTerm2）持续工作

用法: claude_keepalive.py <命令>
  setup        TUI：添加/编辑监控、启停 daemon、看日志
  start        启动 daemon（清除 stop 文件）
  stop         停止 daemon（放置 stop 文件）
  status       非交互状态表
  once         立即检查一轮全部监控（调试）
  daemon       前台运行 daemon（launchd / 调试用）
  install      安装 launchd 常驻
  uninstall    卸载 launchd

配置目录: {BASE_DIR}
全局刹车: touch {STOP_FILE}   （start 会清除）
"""


def main(argv: List[str]) -> int:
    load_global_conf()
    cmd = argv[0] if argv else "help"
    if cmd == "setup":
        return tui_main()
    if cmd == "start":
        return cmd_start()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "status":
        return cmd_status()
    if cmd == "once":
        return cmd_once()
    if cmd == "daemon":
        return daemon_run()
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
