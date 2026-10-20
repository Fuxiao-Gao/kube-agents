"""Unit tests for the verified-claims gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import copy
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import kanban_verified_claims as kvc
from apply_kanban_verified_claims import (
    KANBAN_RELATIVE,
    MARKER,
    NEW_COMPLETE_METADATA,
    NEW_TERMINAL_RETURN,
    OLD_COMPLETE_METADATA,
    OLD_TERMINAL_RETURN,
    TERMINAL_RELATIVE,
    apply,
)

CARD = "t_3200"

# Shaped like the pilot's cost review: one figure read from a command's output.
NODES_CMD = "kubectl get nodes -o wide --context prod"
NODES_OUT = "NAME      STATUS   ROLES\nnode-a    Ready    <none>\nnode-b    NotReady <none>\n"
GOOD = {"claim": "node-b is NotReady.", "command": NODES_CMD, "excerpt": "node-b    NotReady <none>"}


def record(command=NODES_CMD, exit_code=0, output=NODES_OUT, card=CARD, now=None):
    with mock.patch.dict(os.environ, {kvc.TASK_ENV: card}):
        kvc.record_terminal_call(command, exit_code, output, now=now)


def run_rules(card, claims, *rest, **kw):
    return kvc.apply_completion_rules(card, {kvc.PARAM: claims}, *rest, **kw)


class GateTestCase(unittest.TestCase):
    def setUp(self):
        kvc._calls.clear()
        kvc._refused_at.clear()


class RecordTest(GateTestCase):
    def test_a_call_outside_a_card_is_not_recorded(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            kvc.record_terminal_call(NODES_CMD, 0, NODES_OUT)
        self.assertEqual(kvc._calls, {})

    def test_the_record_is_bounded(self):
        for i in range(kvc.MAX_RECORDED_CALLS + 5):
            record(command=f"echo {i}", output=str(i))
        calls = kvc._calls[CARD]
        self.assertEqual(len(calls), kvc.MAX_RECORDED_CALLS)
        self.assertEqual(calls[0]["command"], "echo 5")

    def test_each_card_has_its_own_record(self):
        record(card="t_other")
        err, _, _ = run_rules(CARD, [GOOD], "r", None)
        self.assertIsNotNone(err)


class AcceptTest(GateTestCase):
    def test_no_claims_changes_nothing(self):
        for claims in (None, []):
            self.assertEqual(run_rules(CARD, claims, "r", {"a": 1}), (None, "r", {"a": 1}))

    def test_a_backed_claim_is_stored_and_rendered(self):
        record(exit_code=0, now=0)
        err, result, metadata = run_rules(CARD, [GOOD], "**node-b is down.**", {"a": 1})
        self.assertIsNone(err)
        self.assertEqual(metadata["a"], 1)
        [stored] = metadata[kvc.METADATA_KEY]
        self.assertEqual(stored["exit_code"], 0)
        self.assertEqual(stored["ran_at"], "1970-01-01T00:00:00Z")
        self.assertTrue(result.startswith("**node-b is down.**\n\n" + kvc.EVIDENCE_HEADING))
        self.assertIn(f"$ {NODES_CMD}\nnode-b    NotReady <none>\n```", result)

    def test_whitespace_differences_do_not_matter(self):
        record()
        claim = dict(GOOD, command="kubectl  get nodes\n-o wide --context prod", excerpt="node-b NotReady\n<none>")
        err, _, metadata = run_rules(CARD, [claim], "r", None)
        self.assertIsNone(err)
        self.assertEqual(len(metadata[kvc.METADATA_KEY]), 1)

    def test_the_run_whose_output_holds_the_excerpt_is_cited(self):
        record(exit_code=1, output="error: timeout", now=0)
        record(exit_code=0, now=60)
        record(exit_code=1, output="error: timeout", now=120)
        _, _, metadata = run_rules(CARD, [GOOD], "r", None)
        self.assertEqual(metadata[kvc.METADATA_KEY][0]["ran_at"], "1970-01-01T00:01:00Z")

    def test_a_failed_command_is_cited_with_its_exit_code(self):
        record(command="kubectl get ns ghost", exit_code=1, output='Error from server (NotFound): namespaces "ghost" not found')
        claim = {"claim": "ghost does not exist.", "command": "kubectl get ns ghost", "excerpt": "(NotFound)"}
        _, result, metadata = run_rules(CARD, [claim], "r", None)
        self.assertEqual(metadata[kvc.METADATA_KEY][0]["exit_code"], 1)
        self.assertIn("(exit 1,", result)

    def test_stored_text_is_redacted(self):
        record()
        _, result, metadata = run_rules(CARD, [GOOD], "r", None, redact=str.upper)
        self.assertEqual(metadata[kvc.METADATA_KEY][0]["command"], NODES_CMD.upper())
        self.assertIn(NODES_CMD.upper(), result)

    def test_non_dict_metadata_is_left_for_upstream_to_refuse(self):
        record()
        self.assertEqual(run_rules(CARD, [GOOD], "r", "oops"), (None, "r", "oops"))


class RefuseTest(GateTestCase):
    def assertRefused(self, claim, reason):
        err, result, metadata = run_rules(CARD, [claim], "r", None)
        self.assertIsNotNone(err)
        self.assertIn(reason, err)
        self.assertIn("still in flight", err)
        self.assertEqual((result, metadata), ("r", None))

    def test_the_pilot_shape_is_refused(self):
        # A "verified" label with no command behind it at all.
        self.assertRefused({"claim": "No mismatch exceeds 30%.", "command": "", "excerpt": ""}, "has no command")

    def test_a_command_that_never_ran_is_refused(self):
        record()
        self.assertRefused(dict(GOOD, command="kubectl get pods"), "did not run in this card")

    def test_an_excerpt_not_in_the_output_is_refused(self):
        record()
        self.assertRefused(dict(GOOD, excerpt="node-c Ready"), "is not in the output")

    def test_an_overlong_excerpt_is_refused(self):
        record(output="x" * 2000)
        self.assertRefused(dict(GOOD, excerpt="x" * (kvc.MAX_EXCERPT_CHARS + 1)), "longer than")

    def test_too_many_claims_are_refused(self):
        record()
        err, _, _ = run_rules(CARD, [GOOD] * (kvc.MAX_CLAIMS + 1), "r", None)
        self.assertIn(f"verified_claims[{kvc.MAX_CLAIMS}]", err)

    def test_a_non_object_claim_is_refused(self):
        self.assertRefused("node-b is NotReady", "is not an object")


class NeverWedgedTest(GateTestCase):
    def test_the_retry_drops_bad_claims_and_closes(self):
        record()
        bad = dict(GOOD, claim="node-c is Ready.", excerpt="node-c Ready")
        first, _, _ = run_rules(CARD, [GOOD, bad], "r", None)
        self.assertIn("verified_claims[1]", first)
        with self.assertLogs(kvc.logger, "WARNING") as logs:
            err, result, metadata = run_rules(CARD, [GOOD, bad], "r", None)
        self.assertIsNone(err)
        self.assertEqual([c["claim"] for c in metadata[kvc.METADATA_KEY]], [GOOD["claim"]])
        self.assertNotIn("node-c", result)
        self.assertIn("verified_claims[1]", logs.output[0])

    def test_a_retry_with_no_good_claim_closes_with_nothing_added(self):
        first, _, _ = run_rules(CARD, [GOOD], "r", None)
        self.assertIsNotNone(first)
        with self.assertLogs(kvc.logger, "WARNING"):
            self.assertEqual(run_rules(CARD, [GOOD], "r", None), (None, "r", None))

    def test_a_stale_refusal_gets_a_new_nudge(self):
        run_rules(CARD, [GOOD], "r", None)
        kvc._refused_at[CARD] = time.monotonic() - kvc.NUDGE_TTL_SECONDS - 1
        err, _, _ = run_rules(CARD, [GOOD], "r", None)
        self.assertIsNotNone(err)

    def test_a_clean_completion_clears_the_refusal(self):
        run_rules(CARD, [GOOD], "r", None)
        record()
        run_rules(CARD, [GOOD], "r", None)
        self.assertNotIn(CARD, kvc._refused_at)


class RulesTest(GateTestCase):
    def setUp(self):
        super().setUp()
        self.seen = []

        def second_rule(*, task_id, args, result, metadata, calls, retry, **_):
            self.seen.append((task_id, [c["command"] for c in calls], retry))
            return ([] if "dependency line" in str(result) else ["no dependency line"]), result, metadata

        patcher = mock.patch.object(kvc, "RULES", kvc.RULES + [second_rule])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_second_rule_reads_the_record(self):
        record()
        run_rules(CARD, None, "dependency line", None)
        self.assertEqual(self.seen, [(CARD, [NODES_CMD], False)])

    def test_every_rule_shares_one_refusal(self):
        record()
        bad = dict(GOOD, excerpt="node-c Ready")
        err, _, _ = run_rules(CARD, [bad], "r", None)
        self.assertIn("2 problem(s)", err)
        self.assertIn("- no dependency line", err)
        self.assertIn("- verified_claims[0]", err)
        with self.assertLogs(kvc.logger, "WARNING"):
            err, result, _ = run_rules(CARD, [bad], "r", None)
        self.assertIsNone(err)
        self.assertEqual(result, "r")
        self.assertEqual([retry for *_, retry in self.seen], [False, True])

    def test_the_card_api_returns_a_copy(self):
        record()
        kvc.card_terminal_calls(CARD).clear()
        self.assertEqual(len(kvc.card_terminal_calls(CARD)), 1)


class SchemaTest(unittest.TestCase):
    SCHEMA = {"parameters": {"properties": {"result": {}}, "required": ["result"]}}

    def test_the_parameter_is_added_and_optional(self):
        schema = kvc.apply_schema(copy.deepcopy(self.SCHEMA))
        self.assertEqual(schema["parameters"]["properties"][kvc.PARAM]["maxItems"], kvc.MAX_CLAIMS)
        self.assertNotIn(kvc.PARAM, schema["parameters"]["required"])

    def test_the_description_states_the_limits_it_enforces(self):
        self.assertIn(str(kvc.MAX_EXCERPT_CHARS), kvc.PARAM_DESCRIPTION)

    def test_an_upstream_parameter_of_the_same_name_fails(self):
        schema = kvc.apply_schema(copy.deepcopy(self.SCHEMA))
        with self.assertRaises(RuntimeError):
            kvc.apply_schema(schema)


# Trimmed copies of the v2026.9.14 functions the applier edits.
UPSTREAM_TERMINAL = (
    "def finalize_foreground_result(*, command, result):\n"
    "    result_dict = {}\n"
    + OLD_TERMINAL_RETURN
)
UPSTREAM_KANBAN = (
    "def _handle_block(args, **kw):\n"
    "    return None\n"
    "\n"
    "def _handle_complete(args, **kw):\n"
    "    tid = 't'\n"
    + OLD_COMPLETE_METADATA
    + "    return None\n"
    "\n"
    "_TOOLS = (\n"
    '    ("kanban_complete", KANBAN_COMPLETE_SCHEMA, _handle_complete, "x"),\n'
    ")\n"
)


class ApplierTest(unittest.TestCase):
    def _stage(self, terminal=UPSTREAM_TERMINAL, kanban=UPSTREAM_KANBAN):
        root = pathlib.Path(tempfile.mkdtemp())
        for relative, source in ((TERMINAL_RELATIVE, terminal), (KANBAN_RELATIVE, kanban)):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(source)
        return root

    def test_both_files_are_patched(self):
        root = self._stage()
        apply(root)
        terminal = (root / TERMINAL_RELATIVE).read_text()
        kanban = (root / KANBAN_RELATIVE).read_text()
        self.assertIn(NEW_TERMINAL_RETURN, terminal)
        self.assertIn(NEW_COMPLETE_METADATA, kanban)
        self.assertLess(kanban.index("_apply_verified_claims_schema(KANBAN_COMPLETE_SCHEMA)"), kanban.index("_TOOLS = ("))

    def test_a_second_run_fails(self):
        root = self._stage()
        apply(root)
        with self.assertRaises(SystemExit) as raised:
            apply(root)
        self.assertIn(MARKER, str(raised.exception))

    def test_the_metadata_anchor_in_another_handler_fails_the_build(self):
        moved = UPSTREAM_KANBAN.replace(OLD_COMPLETE_METADATA, "").replace(
            "def _handle_block(args, **kw):\n", "def _handle_block(args, **kw):\n" + OLD_COMPLETE_METADATA
        )
        root = self._stage(kanban=moved)
        with self.assertRaises(SystemExit) as raised:
            apply(root)
        self.assertIn("_handle_complete", str(raised.exception))
        self.assertNotIn(MARKER, (root / TERMINAL_RELATIVE).read_text())


if __name__ == "__main__":
    unittest.main()
