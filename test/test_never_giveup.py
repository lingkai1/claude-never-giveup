#!/usr/bin/env python3
"""claude_never_giveup 单元测试（unittest，stdlib-only，Python 3.9+）。

通过 NEVER_GIVEUP_BASE_DIR + importlib.reload 隔离运行时目录。
运行：/usr/bin/python3 -m unittest discover -s test -v
"""
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class KeepaliveBase(unittest.TestCase):
    """每个测试类独立 tmp 目录 + 重载模块（路径常量按 env 重算）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ck-py-test-")
        os.environ["NEVER_GIVEUP_BASE_DIR"] = cls.tmp
        import claude_never_giveup as ck
        cls.ck = importlib.reload(ck)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("NEVER_GIVEUP_BASE_DIR", None)


class TestHelpers(KeepaliveBase):
    def test_now_is_int(self):
        self.assertIsInstance(self.ck.now(), int)

    def test_atomic_write_overwrite(self):
        p = Path(self.tmp) / "w.txt"
        p.write_text("abc")
        self.ck.atomic_write(p, "line1")
        self.assertEqual(p.read_text().strip(), "line1")

    def test_strip_ansi(self):
        self.assertNotIn("\x1b", self.ck.strip_ansi("\x1b[31mred\x1b[0m plain"))
        self.assertEqual(self.ck.strip_ansi("\x1b[32m❯\x1b[0m"), "❯")

    def test_log_write(self):
        self.ck.log("INFO", "testmon", "hello")
        content = self.ck.LOG_FILE.read_text()
        self.assertIn("[INFO] [testmon] hello", content)

    def test_valid_session_name(self):
        self.assertTrue(self.ck.valid_session_name("a-b_1.2"))
        self.assertFalse(self.ck.valid_session_name("a b"))
        self.assertFalse(self.ck.valid_session_name(""))
        self.assertFalse(self.ck.valid_session_name("has space"))


class TestConfState(KeepaliveBase):
    def test_conf_roundtrip(self):
        cn = self.ck.MON_DIR / "testmon.conf"
        self.ck.MonitorConf(session_name="testmon", message="继续吧",
                            interval=120, enabled=True, dry_run=False).save(cn)
        c = self.ck.MonitorConf.load(cn)
        self.assertEqual(c.session_name, "testmon")
        self.assertEqual(c.message, "继续吧")
        self.assertEqual(c.interval, 120)
        self.assertTrue(c.enabled)
        self.assertFalse(c.dry_run)

    def test_conf_defaults(self):
        cn = self.ck.MON_DIR / "abc.conf"
        cn.parent.mkdir(parents=True, exist_ok=True)
        cn.write_text("session_name=abc\n")
        c = self.ck.MonitorConf.load(cn)
        self.assertEqual(c.message, self.ck.DEFAULT_MESSAGE)
        self.assertEqual(c.interval, 300)
        self.assertTrue(c.enabled)
        self.assertFalse(c.dry_run)

    def test_conf_invalid_name(self):
        cn = self.ck.MON_DIR / "bad.conf"
        cn.parent.mkdir(parents=True, exist_ok=True)
        cn.write_text("session_name=has space\n")
        self.assertIsNone(self.ck.MonitorConf.load(cn))

    def test_conf_missing_file(self):
        self.assertIsNone(self.ck.MonitorConf.load(self.ck.MON_DIR / "nope.conf"))

    def test_conf_interval_floor(self):
        cn = self.ck.MON_DIR / "low.conf"
        cn.parent.mkdir(parents=True, exist_ok=True)
        cn.write_text("session_name=abc\ninterval=1\n")
        self.assertEqual(self.ck.MonitorConf.load(cn).interval, 10)

    def test_state_roundtrip(self):
        st = self.ck.MonitorState(last_state="IDLE", last_check=123, consec_fail=2)
        st.save("testmon")
        st2 = self.ck.MonitorState.load("testmon")
        self.assertEqual((st2.last_state, st2.last_check, st2.consec_fail),
                         ("IDLE", 123, 2))

    def test_state_missing_file(self):
        st = self.ck.MonitorState.load("never-existed")
        self.assertEqual(st.last_state, "NEVER")
        self.assertEqual(st.last_check, 0)

    def test_state_corrupt_json(self):
        self.ck.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (self.ck.STATE_DIR / "corrupt.json").write_text("{not json")
        st = self.ck.MonitorState.load("corrupt")
        self.assertEqual(st.last_state, "NEVER")


class TestClassify(KeepaliveBase):
    def test_idle_with_footer(self):
        fix = "> 继续\n  已完成第一部分\n✻ 一些输出\n❯\n⏵⏵ accept edits on · claude-opus-5 · ~/proj  ? for shortcuts"
        self.assertEqual(self.ck.classify_tail(fix), "IDLE")

    def test_idle_prompt_last_line(self):
        self.assertEqual(self.ck.classify_tail("结果如下\n❯"), "IDLE")

    def test_busy(self):
        self.assertEqual(self.ck.classify_tail("> 继续\n⠸ Generating… (esc to interrupt)\n"), "BUSY")

    def test_dialog(self):
        fix = ("  ⎿ Updated 3 files\nDo you want to proceed?\n❯ 1. Yes\n"
               "  2. Yes, and auto-accept edits\n"
               "  3. No, and tell Claude what to do differently (esc)\n")
        self.assertEqual(self.ck.classify_tail(fix), "DIALOG")

    def test_draft_is_not_idle(self):
        self.assertEqual(self.ck.classify_tail("❯ 请继续修改测试\n⏵⏵ accept edits on"), "UNKNOWN")

    def test_unknown(self):
        self.assertEqual(self.ck.classify_tail("hello world\nplain text\n"), "UNKNOWN")

    def test_busy_beats_dialog(self):
        self.assertEqual(self.ck.classify_tail("❯ 1. Yes\n⠹ Working (esc to interrupt)\n"), "BUSY")

    def test_ansi_wrapped_prompt(self):
        self.assertEqual(self.ck.classify_tail("\x1b[32m❯\x1b[0m\n"), "IDLE")

    def test_idle_with_tall_footer(self):
        """实况复刻：状态栏换行 2 行 + auto-update 提示，❯ 之后有 8 行内容。"""
        fix = (
            "   … +7 completed \n"
            "                                    ✘ Auto-update failed · R\n"
            "──────────────────────────────────────\n"
            "❯\xa0 \n"
            "──────────────────────────────────────\n"
            "  [gpt-6-astra[1m]] ██░░░░ 29% | relay-claw git:(main) | aidoc-bugfix |\n"
            "  ⏱️   38h 57m | Ship it. ⚡ \n"
            "  ──────────────────────────────\n"
            "  ✓ Bash ×10 | ✓ TaskOutput ×6 | ✓ Read ×2 | ✓ Agent ×1 \n"
            "  ⏵⏵ bypass permissions on · 1 shell · ← for agents \n"
        )
        self.assertEqual(self.ck.classify_tail(fix), "IDLE")


class TestDecide(KeepaliveBase):
    def test_inject_first(self):
        self.assertEqual(self.ck.decide_action(0, 0, "> 继续\n❯", "继续"), "INJECT_FIRST")

    def test_inject_again_after_landed(self):
        self.assertEqual(self.ck.decide_action(1, 0, "> 继续\n❯", "继续"), "INJECT_AGAIN")

    def test_retry_when_not_landed(self):
        self.assertEqual(self.ck.decide_action(1, 0, "别的输出\n❯", "继续"), "INJECT_RETRY")

    def test_trip_at_limit(self):
        self.assertEqual(self.ck.decide_action(1, 2, "别的输出\n❯", "继续"), "TRIP")


class TestPs(KeepaliveBase):
    def test_tty_of_line(self):
        self.assertEqual(self.ck.ps_tty_of_line("12345 ttys012 claude -n worker"), "ttys012")

    def test_args_of_line(self):
        self.assertEqual(self.ck.ps_args_of_line("12345 ttys012 node /x/cli.js -n worker"),
                         "node /x/cli.js -n worker")

    def test_match_n_flag(self):
        self.assertEqual(self.ck.match_ps_line_for_session(
            "12345 ttys012 claude -n worker", "worker"), "ttys012")

    def test_match_resume_flag(self):
        self.assertEqual(self.ck.match_ps_line_for_session(
            "12345 ttys012 claude --resume ppt", "ppt"), "ttys012")

    def test_match_name_eq(self):
        self.assertEqual(self.ck.match_ps_line_for_session(
            "12345 ttys012 claude --name=ppt", "ppt"), "ttys012")

    def test_no_tty_no_match(self):
        self.assertIsNone(self.ck.match_ps_line_for_session(
            "12345 ?? claude -n worker", "worker"))

    def test_different_name_no_match(self):
        self.assertIsNone(self.ck.match_ps_line_for_session(
            "12345 ttys012 claude -n other", "worker"))

    def test_tail_n_not_matched(self):
        self.assertIsNone(self.ck.match_ps_line_for_session(
            "12345 ttys012 tail -n worker.log claude", "worker"))

    def test_trailing_args_still_match(self):
        self.assertIsNotNone(self.ck.match_ps_line_for_session(
            "12345 ttys012 claude -n worker extra", "worker"))

    def test_extract_name_n(self):
        self.assertEqual(self.ck.extract_session_name_from_args("claude -n worker"), "worker")

    def test_extract_name_eq(self):
        self.assertEqual(self.ck.extract_session_name_from_args("claude --name=ppt"), "ppt")

    def test_extract_name_resume(self):
        self.assertEqual(self.ck.extract_session_name_from_args("claude --resume ppt"), "ppt")

    def test_extract_name_none(self):
        self.assertEqual(self.ck.extract_session_name_from_args("claude -p hi"), "")

    def test_find_tty_unique(self):
        self.ck.ps_claude_lines = lambda: ["12345 ttys012 claude -n worker"]
        tty, count, pid = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count, pid), ("/dev/ttys012", 1, 12345))

    def test_find_tty_none(self):
        self.ck.ps_claude_lines = lambda: ["12345 ttys012 claude -n other"]
        tty, count, pid = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count, pid), (None, 0, None))

    def test_find_tty_ambiguous(self):
        self.ck.ps_claude_lines = lambda: ["1 ttys001 claude -n worker", "2 ttys002 claude -n worker"]
        tty, count, pid = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count, pid), (None, 2, None))

    def test_scan_running_sessions(self):
        self.ck.ps_claude_lines = lambda: [
            "1 ttys001 claude -n worker",
            "2 ?? claude -p hi",
            "3 ttys003 claude --name=ppt extra",
        ]
        sessions = self.ck.scan_running_claude_sessions()
        self.assertIn(("worker", "ttys001"), sessions)
        self.assertIn(("ppt", "ttys003"), sessions)
        self.assertEqual(len(sessions), 2)

    def test_find_pid_by_tty(self):
        self.ck.ps_claude_lines = lambda: [
            "1 ttys001 claude --settings x.json",
            "2 ttys002 node /x/cli.js -n other",
        ]
        self.assertEqual(self.ck.find_claude_pid_by_tty("/dev/ttys001"), 1)
        self.assertIsNone(self.ck.find_claude_pid_by_tty("/dev/ttys003"))


class TestLoadGlobalConf(KeepaliveBase):
    def test_global_conf_and_patterns_json(self):
        self.ck.CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.ck.CONF_FILE.write_text("TICK_SECONDS=2\nBREAKER_LIMIT=5\n")
        self.ck.PATTERNS_FILE.write_text(json.dumps(
            {"BUSY_PATTERNS": ["spinner!"], "DIALOG_PATTERNS": ["menu!"]}))
        try:
            self.ck.load_global_conf()
            self.assertEqual(self.ck.TICK_SECONDS, 2)
            self.assertEqual(self.ck.BREAKER_LIMIT, 5)
            self.assertEqual(self.ck.BUSY_PATTERNS, ["spinner!"])
            self.assertEqual(self.ck.DIALOG_PATTERNS, ["menu!"])
        finally:
            self.ck.TICK_SECONDS = 15
            self.ck.BREAKER_LIMIT = 3


class TestPipeline(KeepaliveBase):
    """stub 外部依赖，验证 check_monitor 决策路径与熔断写回。"""

    def setUp(self):
        ck = self.ck
        self._saved = (ck.iterm_read_contents, ck.iterm_write_text,
                       ck.iterm_scan_tty_by_name, ck.find_claude_tty_for_session)
        ck.MON_DIR.mkdir(parents=True, exist_ok=True)
        ck.STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 每个用例从干净状态开始（状态文件在用例间会残留）
        try:
            (ck.STATE_DIR / "pipe1.json").unlink()
        except FileNotFoundError:
            pass
        self.cn = ck.MON_DIR / "pipe1.conf"
        self.stub_tty = "/dev/ttys999"
        self.stub_wrote = []
        self.stub_contents = ""
        self.stub_scan = (0, [])

        def fake_read(tty):
            return 0, self.stub_contents

        def fake_write(tty, msg):
            self.stub_wrote.append(msg)
            return 0, ""

        def fake_scan(name):
            return True, self.stub_scan

        ck.iterm_read_contents = fake_read
        ck.iterm_write_text = fake_write
        ck.iterm_scan_tty_by_name = fake_scan
        ck.find_claude_tty_for_session = lambda n: (self.stub_tty, 1, 4242)

    def tearDown(self):
        ck = self.ck
        (ck.iterm_read_contents, ck.iterm_write_text,
         ck.iterm_scan_tty_by_name, ck.find_claude_tty_for_session) = self._saved

    def _write_conf(self, enabled=True, dry_run=False):
        self.ck.MonitorConf(session_name="pipe1", message="继续", interval=60,
                            enabled=enabled, dry_run=dry_run).save(self.cn)

    def test_idle_first_inject(self):
        self._write_conf()
        self.stub_contents = "> 继续\n完成\n❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, ["继续"])
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.awaiting, 1)
        self.assertEqual(st.last_state, "IDLE")

    def test_landed_resets_fail(self):
        self._write_conf()
        self.stub_contents = "> 继续\n❯"
        self.ck.check_monitor("pipe1")
        self.ck.check_monitor("pipe1")  # INJECT_AGAIN 路径
        self.assertEqual(self.ck.MonitorState.load("pipe1").consec_fail, 0)

    def test_breaker_trips_and_writes_back(self):
        self._write_conf()
        self.stub_contents = "无关内容\n❯"
        for _ in range(4):  # 首注(INJECT_FIRST) + 3 次未落地 → TRIP
            self.ck.check_monitor("pipe1")
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.consec_fail, 3)
        conf = self.ck.MonitorConf.load(self.cn)
        self.assertFalse(conf.enabled)
        # TRIP 那次不注入：4 次检查只有前 3 次注入
        self.assertEqual(len(self.stub_wrote), 3)

    def test_busy_resets_counters(self):
        self._write_conf()
        self.stub_contents = "⠸ Working (esc to interrupt)\n"
        self.ck.check_monitor("pipe1")
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.last_state, "BUSY")
        self.assertEqual(st.awaiting, 0)
        self.assertEqual(self.stub_wrote, [])

    def test_disabled_skips(self):
        self._write_conf(enabled=False)
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, [])

    def test_dry_run_no_inject(self):
        self._write_conf(dry_run=True)
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, [])
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.awaiting, 1)  # dry-run 也置 awaiting

    def test_ps_miss_title_fallback_injects(self):
        self.ck.find_claude_tty_for_session = lambda n: (None, 0, None)
        self.stub_scan = (1, ["/dev/ttys777"])
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, ["继续"])
        self.assertEqual(self.ck.MonitorState.load("pipe1").last_state, "IDLE")

    def test_ps_miss_title_ambiguous_skips(self):
        self.ck.find_claude_tty_for_session = lambda n: (None, 0, None)
        self.stub_scan = (2, ["/dev/ttys001", "/dev/ttys002"])
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, [])
        self.assertEqual(self.ck.MonitorState.load("pipe1").last_state, "AMBIGUOUS")

    def test_read_error_sets_no_iterm(self):
        self.ck.iterm_read_contents = lambda tty: (2, "execution error: ... (-1743)")
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.stub_wrote, [])
        self.assertEqual(self.ck.MonitorState.load("pipe1").last_state, "NO_ITERM")

    def test_inject_opens_peek_window(self):
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        st = self.ck.MonitorState.load("pipe1")
        self.assertGreater(st.peek_until, self.ck.now())

    def test_peek_busy_confirms_and_clears(self):
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")          # 注入，开 peek 窗口
        self.stub_contents = "⠸ Working (esc to interrupt)"
        self.ck.check_monitor("pipe1", peek=True)  # peek 抓到 BUSY
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.awaiting, 0)
        self.assertEqual(st.consec_fail, 0)
        self.assertEqual(st.peek_until, 0)
        self.assertEqual(len(self.stub_wrote), 1)   # peek 不再注入

    def test_peek_idle_does_not_inject_or_count(self):
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")          # 注入
        self.ck.check_monitor("pipe1", peek=True)  # peek 时仍 IDLE
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.consec_fail, 0)        # peek 不计数
        self.assertEqual(len(self.stub_wrote), 1)  # peek 不注入
        self.assertEqual(st.awaiting, 1)

    def test_transcript_rescues_retry(self):
        """marker 不可见但 transcript 证实落地 → 不计数，直接续注。"""
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")                       # 首注
        self.ck.proc_cwd = lambda pid: "/fake/cwd"
        self.ck.transcript_landed = lambda cwd, msg, since: True
        self.stub_contents = "无关内容\n❯"                   # marker 不可见
        self.ck.check_monitor("pipe1")
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.consec_fail, 0)                  # 被挽救，不计数
        self.assertEqual(len(self.stub_wrote), 2)            # 续注一次
        self.assertEqual(st.awaiting, 1)

    def test_transcript_absent_still_counts(self):
        self._write_conf()
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")
        self.ck.proc_cwd = lambda pid: "/fake/cwd"
        self.ck.transcript_landed = lambda cwd, msg, since: False
        self.stub_contents = "无关内容\n❯"
        self.ck.check_monitor("pipe1")
        self.assertEqual(self.ck.MonitorState.load("pipe1").consec_fail, 1)

    def test_title_fallback_pid_backfill_rescues(self):
        """标题兜底定位（ps 无匹配）也能经 tty 反查 pid 走 transcript 挽救。"""
        self._write_conf()
        self.ck.find_claude_tty_for_session = lambda n: (None, 0, None)
        self.stub_scan = (1, ["/dev/ttys777"])
        self.stub_contents = "❯"
        self.ck.check_monitor("pipe1")                       # 兜底注入
        self.ck.find_claude_pid_by_tty = lambda t: 4242
        self.ck.proc_cwd = lambda pid: "/fake/cwd"
        self.ck.transcript_landed = lambda cwd, msg, since: True
        self.stub_contents = "无关内容\n❯"
        self.ck.check_monitor("pipe1")
        st = self.ck.MonitorState.load("pipe1")
        self.assertEqual(st.consec_fail, 0)
        self.assertEqual(len(self.stub_wrote), 2)


class TestTranscriptLanded(KeepaliveBase):
    """用假的 CLAUDE_CONFIG_DIR 验证 transcript 落地判定。"""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="ck-cfg-")
        self._old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        self.proj = Path(self.cfg) / "projects" / "-fake-cwd"
        self.proj.mkdir(parents=True)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old
        shutil.rmtree(self.cfg, ignore_errors=True)

    def _write_transcript(self, entries):
        (self.proj / "s.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    @staticmethod
    def _user_entry(text, iso_ts):
        return {"type": "user", "timestamp": iso_ts,
                "message": {"role": "user", "content": text}}

    def test_landed_after_since(self):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        now_ep = datetime.now(timezone.utc).timestamp()
        self._write_transcript([
            self._user_entry("继续", "2020-01-01T00:00:00.000Z"),
            self._user_entry("继续", now_iso),
        ])
        self.assertTrue(self.ck.transcript_landed("/fake/cwd", "继续", int(now_ep) - 60))

    def test_not_landed_before_since(self):
        self._write_transcript([
            self._user_entry("继续", "2020-01-01T00:00:00.000Z"),
        ])
        self.assertFalse(self.ck.transcript_landed("/fake/cwd", "继续", 1600000000))

    def test_not_landed_wrong_text(self):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write_transcript([
            self._user_entry("别的消息", now_iso),
        ])
        self.assertFalse(self.ck.transcript_landed("/fake/cwd", "继续", 1600000000))

    def test_missing_project_dir(self):
        self.assertFalse(self.ck.transcript_landed("/no/such/cwd", "继续", 0))

    def test_munging(self):
        self.assertEqual(
            self.ck.re.sub(r"[^A-Za-z0-9]", "-", "/a/b_c.d"),
            "-a-b-c-d")


class TestStatusAndDaemonPid(KeepaliveBase):
    def test_status_table_and_dead_pid(self):
        ck = self.ck
        ck.MON_DIR.mkdir(parents=True, exist_ok=True)
        ck.MonitorConf(session_name="s7", message="继续", interval=60,
                       enabled=True, dry_run=False).save(ck.MON_DIR / "s7.conf")
        st = ck.MonitorState(last_state="IDLE", last_check=ck.now())
        st.save("s7")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ck.cmd_status()
        out = buf.getvalue()
        self.assertIn("s7", out)
        self.assertIn("daemon: stopped", out)
        # 死 pid 不算运行
        ck.PID_FILE.write_text("999999999")
        self.assertIsNone(ck.daemon_pid())
        # pid 文件缺失
        ck.PID_FILE.unlink()
        self.assertIsNone(ck.daemon_pid())


class TestTuiHelpers(KeepaliveBase):
    def test_disp_width(self):
        self.assertEqual(self.ck._disp_width("abc"), 3)
        self.assertEqual(self.ck._disp_width("中文"), 4)
        self.assertEqual(self.ck._disp_width("a中b"), 4)
        # ANSI 色码不计宽
        self.assertEqual(self.ck._disp_width("\x1b[32m中文\x1b[0m"), 4)

    def test_pad_visible_width(self):
        self.assertEqual(self.ck._pad("中", 4), "中  ")
        self.assertEqual(self.ck._pad("abcd", 2), "abcd")  # 超宽不截断

    def test_box_aligned(self):
        box = self.ck._box("标题", ["中文abc", "plain", self.ck._c("彩色", self.ck._C_OK)])
        lines = box.splitlines()
        self.assertTrue(lines[0].startswith("╭──"))
        self.assertTrue(lines[-1].startswith("╰"))
        # 所有行可见宽度一致（面板对齐）
        widths = {self.ck._disp_width(l) for l in lines}
        self.assertEqual(len(widths), 1)
        # 标题在顶边框里
        self.assertIn("标题", self.ck._SGR_RE.sub("", lines[0]))

    def test_state_badge_has_color(self):
        badge = self.ck._state_badge("IDLE")
        self.assertIn("IDLE", badge)
        self.assertIn(self.ck._C_OK, badge)
        self.assertIn(self.ck._C_RESET, badge)


if __name__ == "__main__":
    unittest.main()
