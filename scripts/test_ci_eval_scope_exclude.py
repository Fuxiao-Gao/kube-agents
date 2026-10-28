"""The per-case scope exclusion in hack/ci-eval-pr.sh (#3213), against its real text.

A case that declares `scope_exclude:` runs alone, after every other unit,
between a patch of the PlatformAgent's spec.scope and its restore. The shell
functions are lifted out of the script and run with kubectl stubbed, as
test_ci_eval_fanout.py does; bench/tests/test_scope_exclude.py covers the
helper that builds the patches.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

from test_ci_eval_fanout import SCRIPT, lifted

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ANNOTATION = "bench.kubeagents.x-k8s.io/scope-before-exclude"
SEEDED_B = {"projectId": "pool-1", "location": "us-central1-b", "clusterName": "seeded-b"}

# The globals the functions read, as the script sets them before the fan-out.
ENV = textwrap.dedent(
    f"""\
    SCRIPT_DIR="{REPO_ROOT / "hack"}"
    PROJECT_ID=pool-1
    AGENT_CLUSTER_CONTEXT=gke_pool-1_us-central1_platform-agent-host
    TARGET_NAMESPACE=kubeagents-system
    AGENT_SERVICE_NAME=platform-agent
    EVAL_SANDBOX_EXEC_TIMEOUT=30s
    EVAL_GATEWAY_CONTAINER=platform-agent
    EVAL_GATEWAY_PYTHON=python3
    EVAL_GATEWAY_HOME=/opt/data
    EVAL_SCOPE_FILE=/etc/kube-agents/scope.json
    EVAL_SCOPE_RECONCILE_SCRIPT=/opt/data/scripts/cluster_agent_reconcile.py
    EVAL_SCOPE_RECONCILE_TIMEOUT_SECONDS=60
    EVAL_SCOPE_RECONCILE_ATTEMPTS=3
    EVAL_SCOPE_RECONCILE_BUSY_EXIT=4
    EVAL_SCOPE_POLL_SECONDS=0
    EVAL_PAUSE_AUDITS_POD_WAIT_SECONDS=1
    """
)

FUNCTIONS = ["scope_exclude_target", "scope_exclude_helper", "scope_exclude_kubectl", "scope_exclude_apply", "scope_exclude_restore"]


def run_bash(body: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + body], cwd=cwd, capture_output=True, text=True, check=False
    )


def kubectl_stub(cr: dict) -> str:
    """kubectl that serves `cr` for a get and logs each patch's ops to ./patches."""
    return textwrap.dedent(
        f"""\
        kubectl() {{
          case " $* " in
            *" get platformagent "*) printf '%s' '{json.dumps(cr)}' ;;
            *" patch platformagent "*) shift $(( $# - 1 )); printf '%s\\n' "$1" >> patches ;;
            *) echo "unexpected kubectl $*" >&2; return 1 ;;
          esac
        }}
        scope_exclude_settle() {{ echo "SETTLED $1"; }}
        """
    )


class UnitPhaseTest(unittest.TestCase):
    def phase(self, name: str) -> str:
        body = "\n".join(
            [
                lifted("unit_phase"),
                "SCOPE_EXCLUDE_NAMES=scoped,scoped-writer",
                "INJECT_LANE_REQUESTING=writer,scoped-writer",
                "STOPPED_ASK_CASES=",
                f"unit_phase {name}",
            ]
        )
        return run_bash(body, ".").stdout.strip()

    def test_a_scope_excluding_case_is_phase_2_even_when_it_writes(self):
        self.assertEqual(self.phase("scoped"), "2")
        self.assertEqual(self.phase("scoped-writer"), "2")

    def test_the_other_phases_are_unchanged(self):
        self.assertEqual(self.phase("writer"), "1")
        self.assertEqual(self.phase("plain"), "0")
        # A name that only contains a scoped case's name is not one.
        self.assertEqual(self.phase("scope"), "0")


class ApplyRestoreTest(unittest.TestCase):
    def run_functions(self, cr: dict, call: str, context: str | None = "default") -> tuple[subprocess.CompletedProcess, list]:
        with tempfile.TemporaryDirectory() as tmp:
            fleet = pathlib.Path(tmp) / "fleet"
            fleet.mkdir()
            if context == "default":
                context = "project=pool-1\ncluster.b=seeded-b\nlocation.b=us-central1-b\n"
            if context is not None:
                (fleet / ".fleet-context").write_text(context)
            body = "\n".join(
                [ENV, f'BENCH_FLEET_KUBECONFIG_DIR="{fleet}"', *map(lifted, FUNCTIONS), kubectl_stub(cr), call]
            )
            result = run_bash(body, tmp)
            patches = pathlib.Path(tmp) / "patches"
            ops = [json.loads(line) for line in patches.read_text().splitlines()] if patches.exists() else []
            return result, ops

    def test_apply_patches_the_slots_cluster_then_settles(self):
        result, ops = self.run_functions({"metadata": {}, "spec": {}}, "scope_exclude_apply case-x b; echo rc=$?")
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertIn("SETTLED case-x", result.stdout)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0][-1], {"op": "add", "path": "/spec/scope", "value": {"exclude": {"clusters": [SEEDED_B]}}})

    def test_apply_without_the_fleet_context_patches_nothing(self):
        result, ops = self.run_functions({"metadata": {}, "spec": {}}, "scope_exclude_apply case-x b; echo rc=$?", context=None)
        self.assertIn("rc=1", result.stdout)
        self.assertIn("no .fleet-context", result.stderr)
        self.assertEqual(ops, [])

    def test_apply_with_a_slot_the_fleet_did_not_resolve_patches_nothing(self):
        result, ops = self.run_functions({"metadata": {}, "spec": {}}, "scope_exclude_apply case-x d; echo rc=$?")
        self.assertIn("rc=1", result.stdout)
        self.assertNotIn("SETTLED", result.stdout)
        self.assertEqual(ops, [])

    def test_apply_refuses_a_context_that_does_not_name_the_project(self):
        call = "AGENT_CLUSTER_CONTEXT=gke_other_us-central1_host; scope_exclude_apply case-x b; echo rc=$?"
        result, ops = self.run_functions({"metadata": {}, "spec": {}}, call)
        self.assertIn("rc=1", result.stdout)
        self.assertEqual(ops, [])

    def test_restore_puts_the_saved_scope_back_and_settles_only_when_asked(self):
        cr = {"metadata": {"annotations": {ANNOTATION: "null"}}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
        for call, settled in (("scope_exclude_restore exit", False), ("scope_exclude_restore lease settle", True)):
            with self.subTest(call=call):
                result, ops = self.run_functions(cr, call + "; echo rc=$?")
                self.assertIn("rc=0", result.stdout, result.stderr)
                self.assertEqual(ops, [[{"op": "remove", "path": "/spec/scope"}, {"op": "remove", "path": "/metadata/annotations/bench.kubeagents.x-k8s.io~1scope-before-exclude"}]])
                self.assertEqual("SETTLED" in result.stdout, settled)

    def test_restore_with_nothing_saved_is_a_no_op(self):
        result, ops = self.run_functions({"metadata": {}, "spec": {}}, "scope_exclude_restore lease settle; echo rc=$?")
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(ops, [])
        self.assertNotIn("SETTLED", result.stdout)


class SettleTest(unittest.TestCase):
    """scope_exclude_settle waits for the operator, the rollout and the mounted
    file, then runs one reconcile and waits out a run that holds its lock."""

    def settle(self, file_clusters: list, reconcile_rcs: str, seconds: int = 5) -> tuple[subprocess.CompletedProcess, str]:
        cr = {"metadata": {"generation": 3}, "status": {"observedGeneration": 3}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
        rendered = {"present": True, "exclude": {"projects": [], "clusters": file_clusters}}
        stub = textwrap.dedent(
            f"""\
            echo {reconcile_rcs} > rcs
            kubectl() {{
              case " $* " in
                *" jsonpath="*) printf '3 3' ;;
                *" get platformagent "*) printf '%s' '{json.dumps(cr)}' ;;
                *" get deployment/"*) echo deployment/platform-agent-gateway ;;
                *" rollout status "*) echo rolled ;;
                *" cat /etc/kube-agents/scope.json"*) printf '%s' '{json.dumps(rendered)}' ;;
                *" --require-create-pass"*)
                  echo "$*" >> reconciles
                  read -r rc rest < rcs; echo "$rest" > rcs
                  echo "Done: created=0 pruned=1"; return "$rc" ;;
                *) echo "unexpected kubectl $*" >&2; return 1 ;;
              esac
            }}
            ready_gateway_pod() {{ echo pod/platform-agent-gateway-0; }}
            # `timeout --foreground <seconds> cmd...`, where it exists, cannot run the stub.
            timeout() {{ shift 2; "$@"; }}
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            body = "\n".join(
                [ENV, f"EVAL_SCOPE_SETTLE_SECONDS={seconds}", *map(lifted, FUNCTIONS), lifted("scope_exclude_settle"), stub, "scope_exclude_settle case-x; echo rc=$?"]
            )
            result = run_bash(body, tmp)
            reconciles = pathlib.Path(tmp) / "reconciles"
            return result, reconciles.read_text() if reconciles.exists() else ""

    def test_a_matching_file_runs_the_reconcile_and_waits_out_a_busy_lock(self):
        result, reconciles = self.settle([SEEDED_B], "4 0")
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertEqual(len(reconciles.splitlines()), 2)
        self.assertIn("env HERMES_HOME=/opt/data python3 /opt/data/scripts/cluster_agent_reconcile.py", reconciles)

    def test_a_file_that_never_matches_fails_without_a_reconcile(self):
        result, reconciles = self.settle([], "0", seconds=1)
        self.assertIn("rc=1", result.stdout)
        self.assertIn("still reads []", result.stderr)
        self.assertEqual(reconciles, "")

    def test_a_reconcile_that_keeps_failing_fails_the_settle(self):
        result, reconciles = self.settle([SEEDED_B], "3 3 3")
        self.assertIn("rc=1", result.stdout)
        self.assertEqual(len(reconciles.splitlines()), 3)


class WiringTest(unittest.TestCase):
    """Where the script calls the functions: the order is the point."""

    src = SCRIPT.read_text(encoding="utf-8")

    def test_the_lease_repairs_a_leftover_before_the_audits_pause_and_stops_when_it_cannot(self):
        restore = self.src.index('scope_exclude_restore "lease" settle || {')
        self.assertLess(restore, self.src.index('scheduled_audits pause "lease"'))
        self.assertIn("exit 1", self.src[restore : restore + 300])

    def test_the_exit_trap_restores_without_waiting(self):
        trap = lifted("profile_and_dump_on_exit")
        self.assertIn('declare -F scope_exclude_restore >/dev/null && scope_exclude_restore "exit"\n', trap)

    def test_phase_2_runs_after_the_writers_one_case_at_a_time_between_apply_and_restore(self):
        writers = self.src.index('launch_units "${UNIT_QUEUE_WRITERS}"')
        loop = re.search(r'^for SCOPE_LINE in .*?^done$', self.src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(loop)
        self.assertLess(writers, loop.start())
        body = loop.group(0)
        apply, launch, restore = (body.index(s) for s in ("scope_exclude_apply", "launch_units", 'scope_exclude_restore "${SCOPE_CASE}" settle'))
        self.assertLess(apply, launch)
        self.assertLess(launch, restore)
        self.assertIn('1 "${SCOPE_PAUSE}"', body)
        # A scope edit that did not take effect records each repetition as infrastructure.
        self.assertIn('"scope-exclude: slot(s) ${SCOPE_SLOTS} could not be excluded', body)


if __name__ == "__main__":
    unittest.main()
