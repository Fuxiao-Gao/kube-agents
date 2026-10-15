"""Unit tests for the audit-ledger completion gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kanban_audit_ledger as kal
from kanban_audit_ledger import (
    CONTENT_JSON_PREFIX,
    NUDGE_TTL_SECONDS,
    SESSION_ID_ENV,
    WORKER_TASK_ENV,
    audit_run,
    require_audit_ledger,
)

# The #2856 shape: the compliance worker ran `start`, its `finish` was
# rejected (exit 2), and it closed the card claiming the ledger was published.
CARD = "t_a65e3664"
SESSION = "sess-worker"
START = (
    "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py "
    "start --audit compliance-audit"
)
FINISH = (
    "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py "
    "finish --audit compliance-audit --manifest-file /opt/data/scratch/m.json "
    "--findings-file /opt/data/scratch/findings_compliance-audit.json"
)
LEDGER = "https://github.com/acme/infra/issues/248"
CLAIM = "The detailed ledger and automated fix PRs have been published."
REJECTED = "FINDINGS REJECTED: scope: required object with 'clusters' and 'skipped'"

STATE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, parent_session_id TEXT, started_at REAL NOT NULL, end_reason TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT
);
"""


def finish_line(status="OPENED", url=LEDGER):
    return json.dumps({"status": status, "issue_url": url, "new": 3, "silent_ok": False})


class Fixture(unittest.TestCase):
    def setUp(self):
        kal._refused_at.clear()
        self.addCleanup(kal._refused_at.clear)
        tmp = Path(tempfile.mkdtemp())
        self.state = str(tmp / "state.db")
        self.board_path = str(tmp / "kanban.db")
        conn = sqlite3.connect(self.state)
        conn.executescript(STATE_SCHEMA)
        conn.commit()
        conn.close()
        self.session(SESSION)
        board = sqlite3.connect(self.board_path)
        board.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT)")
        board.execute("INSERT INTO tasks VALUES (?, 'running', NULL)", (CARD,))
        board.commit()
        board.close()
        self.calls = 0
        env = mock.patch.dict(os.environ, {WORKER_TASK_ENV: CARD, SESSION_ID_ENV: SESSION})
        env.start()
        self.addCleanup(env.stop)

    def session(self, sid, parent=None, end_reason=None, started_at=1.0):
        with sqlite3.connect(self.state) as conn:
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, parent, started_at, end_reason)
            )

    def run_tool(self, command, output, exit_code=0, sid=SESSION, tool="terminal", raw=None):
        """One assistant tool call and its result, as Hermes stores them."""
        self.calls += 1
        call_id = f"call_{self.calls}"
        calls = [{"id": call_id, "type": "function",
                  "function": {"name": tool, "arguments": json.dumps({"command": command})}}]
        content = raw if raw is not None else json.dumps(
            {"output": output, "exit_code": exit_code, "error": None}
        )
        with sqlite3.connect(self.state) as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?, 'assistant', '', ?)",
                (sid, json.dumps(calls)),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name) "
                "VALUES (?, 'tool', ?, ?, ?)",
                (sid, content, call_id, tool),
            )

    def connect(self):
        return sqlite3.connect(self.board_path)

    def complete(self, result=CLAIM):
        return require_audit_ledger(CARD, self.connect, result, state_db=self.state)

    def card_row(self):
        with sqlite3.connect(self.board_path) as conn:
            return conn.execute("SELECT status, result FROM tasks WHERE id = ?", (CARD,)).fetchone()


class TestTheIncidentShapes(Fixture):
    def test_a_rejected_finish_then_done_is_refused(self):
        self.run_tool(START, '{"workspace": "/opt/data/gitops/compliance-audit/acme__infra"}')
        self.run_tool(FINISH, REJECTED, exit_code=2)
        err, _ = self.complete()
        self.assertIsNotNone(err)
        self.assertIn("compliance-audit", err)
        self.assertIn("kanban_block", err)

    def test_start_with_no_finish_at_all_is_refused(self):
        self.run_tool(START, "{}")
        self.run_tool("python3 collect.py compliance-audit", "Command timed out after 60s", exit_code=124)
        err, _ = self.complete()
        self.assertIsNotNone(err)

    def test_a_background_finish_never_read_is_refused(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, "", raw=json.dumps(
            {"output": "", "exit_code": None, "status": "yielded_to_background", "session_id": "proc_1"}
        ))
        err, _ = self.complete()
        self.assertIsNotNone(err)

    def test_the_refusal_saves_the_result_and_leaves_the_card_open(self):
        self.run_tool(START, "{}")
        self.complete()
        self.assertEqual(self.card_row(), ("running", CLAIM))


class TestPublishedRuns(Fixture):
    def test_a_finish_url_missing_from_the_result_is_appended(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, "label-ensure ok\n" + finish_line())
        err, result = self.complete("Audit complete, 24 findings.")
        self.assertIsNone(err)
        self.assertEqual(result, "Audit complete, 24 findings.\n\nLedger: " + LEDGER)

    def test_a_result_that_names_the_ledger_is_unchanged(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, finish_line())
        report = f"Opened the ledger: {LEDGER}"
        self.assertEqual(self.complete(report), (None, report))

    def test_a_finish_read_back_from_a_background_process_counts(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, "", raw=json.dumps(
            {"output": "", "exit_code": None, "status": "yielded_to_background", "session_id": "proc_1"}
        ))
        self.run_tool("", finish_line(), tool="process")
        err, result = self.complete("Done.")
        self.assertIsNone(err)
        self.assertIn(LEDGER, result)

    def test_no_forge_has_no_url_and_is_accepted(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, finish_line(status="NO_FORGE", url=None))
        self.assertEqual(self.complete("4 findings."), (None, "4 findings."))

    def test_a_clean_run_with_no_earlier_ledger_is_accepted(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, finish_line(status="CLEAN", url=None))
        self.assertEqual(self.complete("Clean."), (None, "Clean."))

    def test_a_retry_after_a_rejection_that_publishes_is_accepted(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, REJECTED, exit_code=2)
        self.run_tool(FINISH, finish_line())
        self.assertEqual(self.complete(LEDGER), (None, LEDGER))


class TestCardsThatAreNotAuditRuns(Fixture):
    def test_a_card_that_never_ran_start_is_untouched(self):
        self.run_tool("kubectl get pods -A", "NAME READY")
        self.assertEqual(self.complete("pods are fine"), (None, "pods are fine"))

    def test_a_refused_start_is_not_a_run(self):
        self.run_tool(START, "START REFUSED: compliance-audit is already running", exit_code=2)
        self.assertEqual(self.complete("already running"), (None, "already running"))

    def test_reading_the_skill_is_not_a_run(self):
        self.run_tool("grep -n start skills/fleet-audit/SKILL.md", "audit_report.py start --audit x")
        self.assertEqual(self.complete("read it"), (None, "read it"))

    def test_another_card_is_untouched(self):
        self.run_tool(START, "{}")
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: "t_other"}):
            self.assertEqual(self.complete(), (None, CLAIM))

    def test_a_gateway_session_with_no_worker_card_is_untouched(self):
        self.run_tool(START, "{}")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.complete(), (None, CLAIM))


class TestTheNudgeIsNeverAWedge(Fixture):
    def setUp(self):
        super().setUp()
        self.run_tool(START, "{}")
        self.err, _ = self.complete()

    def test_the_retry_is_accepted_and_says_not_published(self):
        self.assertIsNotNone(self.err)
        err, result = self.complete()
        self.assertIsNone(err)
        self.assertTrue(result.startswith("⚠️ **Not published:** the compliance-audit audit"))
        self.assertTrue(result.endswith(CLAIM))

    def test_a_stale_refusal_earns_a_fresh_refusal(self):
        kal._refused_at[CARD] -= NUDGE_TTL_SECONDS + 1
        err, _ = self.complete()
        self.assertIsNotNone(err)

    def test_a_failed_save_accepts_and_says_not_published(self):
        kal._refused_at.clear()
        ro = lambda: sqlite3.connect(f"file:{self.board_path}?mode=ro", uri=True)
        err, result = require_audit_ledger(CARD, ro, CLAIM, state_db=self.state)
        self.assertIsNone(err)
        self.assertIn("Not published", result)
        self.assertNotIn(CARD, kal._refused_at)


class TestTheRunBoundary(Fixture):
    def test_a_second_start_forgets_the_first_finish(self):
        self.run_tool(START, "{}")
        self.run_tool(FINISH, finish_line())
        self.run_tool(START, "{}")
        err, _ = self.complete(LEDGER)
        self.assertIsNotNone(err)

    def test_a_finish_line_before_any_start_does_not_count(self):
        self.run_tool("cat /opt/data/fleet-audit/reports/latest.json", finish_line())
        self.run_tool(START, "{}")
        err, _ = self.complete()
        self.assertIsNotNone(err)

    def test_the_audit_id_is_read_from_the_equals_form(self):
        self.run_tool(START.replace("--audit compliance-audit", "--audit=ai-security-audit"), "{}")
        err, _ = self.complete()
        self.assertIn("ai-security-audit", err)


class TestCompression(Fixture):
    def test_a_finish_in_the_compressed_child_is_found_from_the_parent_id(self):
        with sqlite3.connect(self.state) as conn:
            conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = ?", (SESSION,))
        self.session("sess-child", parent=SESSION, started_at=2.0)
        self.run_tool(START, "{}")
        self.run_tool(FINISH, finish_line(), sid="sess-child")
        err, result = self.complete("done")
        self.assertIsNone(err)
        self.assertIn(LEDGER, result)

    def test_a_start_in_the_compressed_parent_is_found_from_the_child_id(self):
        with sqlite3.connect(self.state) as conn:
            conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = ?", (SESSION,))
        self.session("sess-child", parent=SESSION, started_at=2.0)
        self.run_tool(START, "{}")
        with mock.patch.dict(os.environ, {SESSION_ID_ENV: "sess-child"}):
            err, _ = self.complete()
        self.assertIsNotNone(err)

    def test_a_parent_that_did_not_end_in_compression_is_not_walked(self):
        self.session("sess-other", started_at=0.5)
        with sqlite3.connect(self.state) as conn:
            conn.execute("UPDATE sessions SET parent_session_id = 'sess-other' WHERE id = ?", (SESSION,))
        self.run_tool(START, "{}", sid="sess-other")
        self.assertEqual(self.complete(), (None, CLAIM))


class TestStoredShapes(Fixture):
    def test_json_prefixed_content_is_decoded(self):
        rows = [
            (1, "assistant", "", None, json.dumps([{"id": "c1", "function": {
                "name": "terminal", "arguments": json.dumps({"command": START})}}])),
            (2, "tool", CONTENT_JSON_PREFIX + json.dumps({"output": "{}", "exit_code": 0}), "c1", None),
        ]
        self.assertEqual(audit_run(rows), ("compliance-audit", None))

    def test_arguments_stored_as_a_dict_are_read(self):
        rows = [
            (1, "assistant", "", None, json.dumps([{"id": "c1", "function": {
                "name": "terminal", "arguments": {"command": START}}}])),
            (2, "tool", json.dumps({"output": "{}", "exit_code": 0}), "c1", None),
        ]
        self.assertEqual(audit_run(rows)[0], "compliance-audit")


class TestFailOpen(Fixture):
    def test_no_session_id_fails_open(self):
        self.run_tool(START, "{}")
        with mock.patch.dict(os.environ, {SESSION_ID_ENV: ""}):
            self.assertEqual(self.complete(), (None, CLAIM))

    def test_a_missing_store_fails_open(self):
        self.assertEqual(
            require_audit_ledger(CARD, self.connect, CLAIM, state_db="/nonexistent/state.db"),
            (None, CLAIM),
        )

    def test_an_unreadable_store_fails_open(self):
        Path(self.state).write_text("not a database")
        self.assertEqual(self.complete(), (None, CLAIM))

    def test_a_blank_task_id_fails_open(self):
        self.assertEqual(require_audit_ledger("", self.connect, CLAIM, state_db=self.state), (None, CLAIM))


class TestTheApplier(unittest.TestCase):
    """Drive apply_kanban_audit_ledger against a fixture tree."""

    def setUp(self):
        import apply_kanban_audit_ledger as applier
        from kanban_result_required import NEW_GATE

        self.applier = applier
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(
            "def _handle_complete(args):\n"
            "    tid = args.get('tid')\n"
            "    summary = args.get('summary')\n"
            "    result = args.get('result')\n"
            + NEW_GATE
            + "    return kb.complete_task(tid, summary=summary, result=result)\n"
            "\n"
            "def _handle_create(args):\n"
            "    with _board(args.get('board')) as (kb, conn):\n"
            "        new_tid = kb.create_task(conn, title=title)\n"
            "        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)\n"
            "        return _ok(task_id=new_tid, **landed)\n"
            "\n"
            "def _handle_block(args):\n"
            "    with _board(args.get('board')) as (kb, conn):\n"
            "        ok = kb.block_task(conn, tid, reason=reason, kind=kind, "
            "expected_run_id=_worker_run_id(tid))\n"
            "        return _ok_landed(kb, conn, tid, 'blocked', block_kind=kind)\n",
            encoding="utf-8",
        )

    def patched(self):
        return self.target.read_text(encoding="utf-8")

    def test_the_gate_lands_after_the_result_gate_and_the_file_parses(self):
        self.applier.apply(self.tmp)
        out = self.patched()
        self.assertLess(out.index("_require_result("), out.index("_require_audit_ledger("))
        self.assertLess(out.index("_require_audit_ledger("), out.index("kb.complete_task("))
        self.assertIn("from tools.kanban_audit_ledger import", out)
        ast.parse(out)

    def test_a_second_run_refuses(self):
        self.applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_missing_anchor_fails_the_build(self):
        self.target.write_text(self.patched().replace("_require_result", "_require_result_v2"))
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_it_stacks_with_the_children_gate_in_either_order(self):
        import apply_kanban_children_settled as children

        for first, second in ((children, self.applier), (self.applier, children)):
            self.setUp()
            first.apply(self.tmp)
            second.apply(self.tmp)
            out = self.patched()
            self.assertLess(out.index("_require_children_settled("), out.index("_require_result("))
            self.assertLess(out.index("_require_result("), out.index("_require_audit_ledger("))
            ast.parse(out)


if __name__ == "__main__":
    unittest.main()
