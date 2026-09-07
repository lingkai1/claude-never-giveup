#!/usr/bin/env python3
"""claude_keepalive 单元测试（unittest，stdlib-only，Python 3.9+）。

通过 KEEPALIVE_BASE_DIR + importlib.reload 隔离运行时目录。
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
        os.environ["KEEPALIVE_BASE_DIR"] = cls.tmp
        import claude_keepalive as ck
        cls.ck = importlib.reload(ck)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("KEEPALIVE_BASE_DIR", None)


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
        tty, count = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count), ("/dev/ttys012", 1))

    def test_find_tty_none(self):
        self.ck.ps_claude_lines = lambda: ["12345 ttys012 claude -n other"]
        tty, count = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count), (None, 0))

    def test_find_tty_ambiguous(self):
        self.ck.ps_claude_lines = lambda: ["1 ttys001 claude -n worker", "2 ttys002 claude -n worker"]
        tty, count = self.ck.find_claude_tty_for_session("worker")
        self.assertEqual((tty, count), (None, 2))

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


if __name__ == "__main__":
    unittest.main()
