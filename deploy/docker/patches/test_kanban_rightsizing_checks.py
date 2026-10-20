"""Unit tests for the rightsizing rule in the kanban_complete gate.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import os
import pathlib
import sqlite3
import tempfile
import unittest

from apply_kanban_rightsizing_checks import ANCHOR, KANBAN_RELATIVE, MARKER, apply

import kanban_rightsizing_checks as krc
import kanban_verified_claims as kvc

PARENT = "t_platform"
CHILD = "t_seeded_a"
SKILL_BODY = "Lower the requests of api-server. Run the `gke-rightsizing-checks` skill first."

GOOD_CARD_RESULT = """**No cut is safe for either workload.**

api-server: an HPA scales it on CPU, so a lower request adds replicas
queue-worker: Guaranteed QoS, so a lower request is also a lower limit

- HPA: `kubectl get hpa -n web` shows api-server at 60% CPU.
- QoS: queue-worker is Guaranteed, requests equal limits.
- Shared: one PDB selects api-server; no PVC.
- Source: meta.helm.sh/release-name=web, so a Helm values file may render other clusters.
- Packing: 940m allocatable per node; nodes needed = max(ceil(1.2/0.94), ceil(2.1/2.8)) = 2.
"""


class RuleTestCase(unittest.TestCase):
    def setUp(self):
        kvc._calls.clear()
        kvc._refused_at.clear()
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT, body TEXT)")
        conn.execute(
            "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def card(self, task_id, body="", status="running", result=None, creator=None):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)", (task_id, status, result, body))
        if creator:
            conn.execute("INSERT INTO kanban_worker_children VALUES (?, ?, 0)", (task_id, creator))
        conn.commit()
        conn.close()

    def connect(self):
        return sqlite3.connect(self.path)

    def rule(self, task_id, result, retry=False, connect="default"):
        return krc.rightsizing_rule(
            task_id=task_id,
            args={},
            result=result,
            metadata={"a": 1},
            calls=[],
            retry=retry,
            redact=lambda value: value,
            connect=self.connect if connect == "default" else connect,
        )


class RegistrationTest(unittest.TestCase):
    def test_importing_the_module_registers_the_rule_once(self):
        self.assertEqual(kvc.RULES.count(krc.rightsizing_rule), 1)


class UntriggeredTest(RuleTestCase):
    def test_a_card_the_skill_was_not_asked_for_passes(self):
        self.card(CHILD, body="Why is payments-api crashlooping?")
        self.assertEqual(self.rule(CHILD, "It is OOMKilled."), ([], "It is OOMKilled.", {"a": 1}))

    def test_no_board_connection_passes(self):
        self.assertEqual(self.rule(CHILD, "anything", connect=None)[0], [])

    def test_a_board_that_cannot_be_read_passes(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(CHILD, "anything", connect=broken)[0], [])


class OwnResultTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(CHILD, body=SKILL_BODY)

    def test_a_result_with_the_lines_and_the_checks_passes(self):
        self.assertEqual(self.rule(CHILD, GOOD_CARD_RESULT)[0], [])

    def test_no_dependency_line_is_refused(self):
        result = GOOD_CARD_RESULT.replace("api-server: an HPA", "The HPA").replace("queue-worker: Guaranteed", "Guaranteed")
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("<workload>: <dependency>", reason)

    def test_no_dependency_found_counts_as_a_line(self):
        result = "batch-worker: no dependency found\n\nHPA none. QoS Burstable. No PDB. Helm. 2 nodes needed."
        self.assertEqual(self.rule(CHILD, result)[0], [])

    def test_a_line_below_the_opening_does_not_count(self):
        filler = "\n".join(f"Paragraph {i}." for i in range(krc.OPENING_LINES))
        result = f"{filler}\napi-server: an HPA scales it\nQoS Guaranteed. PDB. Helm. nodes needed."
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("open `result`", reason)

    def test_a_missing_check_is_named(self):
        result = GOOD_CARD_RESULT.replace("- Packing: 940m allocatable per node; nodes needed = max(ceil(1.2/0.94), ceil(2.1/2.8)) = 2.\n", "")
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("per-node packing", reason)
        self.assertNotIn("HPA or KEDA", reason)

    def test_markdown_around_the_line_still_counts(self):
        result = GOOD_CARD_RESULT.replace("api-server: an HPA", "- **`api-server`**: an HPA")
        self.assertEqual(self.rule(CHILD, result)[0], [])

    def test_the_retry_is_accepted_unchanged(self):
        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(CHILD, "No figures.", retry=True), ([], "No figures.", {"a": 1}))


class ChildLinesTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(PARENT, body="Lower the requests of api-server and queue-worker.")
        self.card(CHILD, body=SKILL_BODY, status="done", result=GOOD_CARD_RESULT, creator=PARENT)

    def test_a_summary_that_carries_every_line_passes(self):
        summary = (
            "**No cut is safe.**\n\n"
            "- **api-server**: an HPA scales it on CPU, so a lower request adds replicas\n"
            "- `queue-worker: Guaranteed QoS, so a lower request is also a lower limit`\n"
        )
        self.assertEqual(self.rule(PARENT, summary)[0], [])

    def test_a_summary_that_drops_a_line_is_refused_with_the_line(self):
        summary = "**No cut is safe.** api-server: an HPA scales it on CPU, so a lower request adds replicas"
        [reason] = self.rule(PARENT, summary)[0]
        self.assertIn(CHILD, reason)
        self.assertIn("queue-worker: guaranteed qos", reason)

    def test_a_grandchild_counts_too(self):
        self.card("t_grandchild", body=SKILL_BODY, status="done", result="db: no dependency found\nHPA QoS PDB Helm nodes needed", creator=CHILD)
        summary = "api-server: an HPA scales it on CPU, so a lower request adds replicas\nqueue-worker: Guaranteed QoS, so a lower request is also a lower limit"
        [reason] = self.rule(PARENT, summary)[0]
        self.assertIn("t_grandchild", reason)

    def test_a_child_still_running_or_without_the_skill_is_skipped(self):
        self.card("t_running", body=SKILL_BODY, status="running", result="x: y", creator=PARENT)
        self.card("t_other", body="Check pods.", status="done", result="pods: all running", creator=PARENT)
        summary = "api-server: an HPA scales it on CPU, so a lower request adds replicas\nqueue-worker: Guaranteed QoS, so a lower request is also a lower limit"
        self.assertEqual(self.rule(PARENT, summary)[0], [])


class SharedRefusalTest(RuleTestCase):
    def test_the_gate_refuses_once_then_accepts(self):
        self.card(CHILD, body=SKILL_BODY)
        err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIn("gke-rightsizing-checks", err)
        self.assertEqual(result, "No figures.")
        with self.assertLogs(krc.logger, "WARNING"):
            err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIsNone(err)
        self.assertEqual(result, "No figures.")


class ApplierTest(unittest.TestCase):
    def kanban_tools(self, text):
        root = pathlib.Path(tempfile.mkdtemp())
        path = root / KANBAN_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return root, path

    def test_the_import_follows_the_verified_claims_preamble(self):
        root, path = self.kanban_tools(f"KANBAN_COMPLETE_SCHEMA = {{}}\n{ANCHOR}_TOOLS = []\n")
        apply(root)
        patched = path.read_text()
        self.assertLess(patched.index(ANCHOR), patched.index("import tools.kanban_rightsizing_checks"))
        self.assertIn(MARKER, patched)

    def test_a_second_run_is_refused(self):
        root, _ = self.kanban_tools(f"KANBAN_COMPLETE_SCHEMA = {{}}\n{ANCHOR}_TOOLS = []\n")
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_file_without_the_verified_claims_patch_is_refused(self):
        root, _ = self.kanban_tools("_TOOLS = []\n")
        with self.assertRaises(SystemExit):
            apply(root)


if __name__ == "__main__":
    unittest.main()
