"""Offline tests for the gitops-fix-cycle stack's pinned-base guards.

bench/tf/prebuilt/gitops-fix-cycle/scripts/run-branch.sh and
agent-base-branch.sh write to a GitHub repository and to the PlatformAgent on a
shared install. Their refusal and cleanup paths run for the first time in a
live campaign run otherwise, and a regression there moves a default branch it
should not, or removes an administrator's base. Each test runs the real script
with `curl` or `kubectl` replaced by a stub on PATH that answers from a JSON
state file and logs every call, then asserts on the exit status, the calls
made, and the state left behind. run-gitops-pilot.sh is run from its TASK/CASE
resolution and leaked-pin refusal through its base-mode decision (step 2), and
stops at the token read that follows; its post-run pull-request listing is
gitops-run-prs.sh, run here with `gh` stubbed. The wrapper's post-teardown leak
check is not covered: reaching it means stubbing the whole run.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK_SCRIPTS = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "gitops-fix-cycle" / "scripts"
_RUN_BRANCH = _STACK_SCRIPTS / "run-branch.sh"
_AGENT_BASE_BRANCH = _STACK_SCRIPTS / "agent-base-branch.sh"
_MANIFESTS = _STACK_SCRIPTS.parent / "manifests"
_WRAPPER = _REPO_ROOT / "bench" / "hack" / "run-gitops-pilot.sh"
_RUN_PRS = _REPO_ROOT / "bench" / "hack" / "gitops-run-prs.sh"

_SLUG = "example-org/gitops-run"
_REPO = f"https://github.com/{_SLUG}"
_RUN = "run/gitops-pilot-t1/b-0022b"
_OTHER_RUN = "run/gitops-pilot-t2/b-0022b"
_BASE_SHA = "a" * 40
_ELSEWHERE_SHA = "e" * 40
_COMMIT_SHA = "c" * 40
_CR = "platformagents.kubeagents.x-k8s.io/platform-agent"
_BROKER = "deploy/platform-agent-credential-proxy"
# Exit status of the stub uv once the wrapper is past what these tests cover.
_UV_STOP = 97
# Exit status of the stub kubectl at the wrapper's token read (step 3), the
# first call after its base-mode decision.
_KUBECTL_STOP = 98

# curl as run-branch.sh's api() calls it: -o <body> -w '%{http_code}', headers,
# then [-X METHOD] URL [-d DATA]. Answers with the first route in the state
# file whose method and path (after the API host) match.
_STUB_CURL = """\
import json, os, re, sys
state = json.load(open(os.environ["STUB_STATE"]))
method, out, data, url = "GET", None, None, None
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] in ("-o", "-w", "-H", "-X", "-d"):
        flag, value = args[i], args[i + 1]
        i += 2
        if flag == "-o":
            out = value
        elif flag == "-X":
            method = value
        elif flag == "-d":
            data = value
        continue
    if args[i].startswith("https://"):
        url = args[i]
    i += 1
path = url.split("https://api.github.com", 1)[1]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"method": method, "path": path, "data": data}) + "\\n")
for route in state["routes"]:
    if route["method"] == method and re.fullmatch(route["path"], path):
        code, body = route["code"], route.get("body", {})
        break
else:
    code, body = 599, {"message": "stub curl: no route"}
with open(out, "w") as f:
    json.dump(body, f)
sys.stdout.write(str(code))
"""

# kubectl as agent-base-branch.sh calls it, against one PlatformAgent, its CRD
# and the credential broker's Deployment, all held in the state file. For
# run-gitops-pilot.sh, `exec` (gitops-run-repo.sh check) succeeds, and the
# secret read records the default-branch switch the wrapper exported before it,
# then stops the wrapper.
_STUB_KUBECTL = """\
import json, os, sys
path = os.environ["STUB_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
rest = []
i = 0
while i < len(args):
    if args[i] in ("--context", "-n"):
        i += 2
        continue
    rest.append(args[i])
    i += 1

def save():
    json.dump(state, open(path, "w"))

def flag(prefix):
    for j, a in enumerate(rest):
        if a == prefix:
            return rest[j + 1]
        if a.startswith(prefix + "="):
            return a.split("=", 1)[1]
    return None

def missing_cr():
    sys.stderr.write('Error from server (NotFound): platformagents "platform-agent" not found\\n')
    sys.exit(1)

verb, target = rest[0], rest[1]
if verb == "get" and target == "crd":
    if state.get("crd_unreadable"):
        sys.stderr.write('Error from server (Forbidden): customresourcedefinitions.apiextensions.k8s.io is forbidden\\n')
        sys.exit(1)
    props = {"github": {"type": "object"}}
    if state["crd_has_field"]:
        props["baseBranch"] = {"type": "string"}
    schema = {"properties": {"spec": {"properties": {"integration": {"properties": props}}}}}
    print(json.dumps({"spec": {"versions": [{"name": "v1alpha1", "served": True, "schema": {"openAPIV3Schema": schema}}]}}))
elif verb == "get" and target == "%(cr)s":
    if not state["cr_exists"]:
        missing_cr()
    if "resourceVersion" in (flag("-o") or ""):
        sys.stdout.write("%%d %%s" %% (state.get("rv", 1), state["base"]))
    else:
        sys.stdout.write(state["base"])
elif verb == "get" and target == "%(broker)s":
    if state.get("broker_read_failures", 0) > 0:
        state["broker_read_failures"] -= 1
        save()
        sys.stderr.write("Unable to connect to the server: net/http: TLS handshake timeout\\n")
        sys.exit(1)
    env = []
    if state["base"]:
        env = [{"name": "CREDENTIAL_PROXY_BASE_BRANCH", "value": state["base"]},
               {"name": "CREDENTIAL_PROXY_BASE_REPOSITORY", "value": state["repository"]}]
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "proxy", "env": env}]}}}}))
elif verb == "patch" and target == "%(cr)s":
    if not state["cr_exists"]:
        missing_cr()
    payload = json.loads(flag("-p"))
    if flag("--type") == "merge":
        if state.get("written_before_patch") is not None:
            # Another writer between pin's read and its patch.
            state["base"] = state.pop("written_before_patch")
            state["rv"] = state.get("rv", 1) + 1
            save()
        sent = payload.get("metadata", {}).get("resourceVersion")
        if sent is not None and sent != str(state.get("rv", 1)):
            sys.stderr.write('Error from server (Conflict): Operation cannot be fulfilled on platformagents "platform-agent": the object has been modified\\n')
            sys.exit(1)
        if state["crd_has_field"]:
            state["base"] = payload["spec"]["integration"]["baseBranch"]
        save()
        if state.get("patch_fails_after_apply"):
            sys.stderr.write("error: stream error: context deadline exceeded\\n")
            sys.exit(1)
    else:
        state.setdefault("json_patches", []).append(payload)
        if state.get("remove_fails"):
            if state.get("remove_fails_sets_base") is not None:
                state["base"] = state.pop("remove_fails_sets_base")
            save()
            sys.stderr.write("error: stream error: context deadline exceeded\\n")
            sys.exit(1)
        tested = [op["value"] for op in payload if op["op"] == "test"]
        if not state["base"] or any(v != state["base"] for v in tested):
            save()
            sys.stderr.write("The request is invalid: the server rejected our request due to an error in our request\\n")
            sys.exit(1)
        state["base"] = ""
        save()
        if state.pop("remove_fails_after_apply", False):
            save()
            sys.stderr.write("error: stream error: context deadline exceeded\\n")
            sys.exit(1)
elif verb == "exec":
    pass
elif verb == "get" and target == "secret":
    state["switch_at_token_read"] = os.environ.get("TF_VAR_gitops_switch_default_branch")
    save()
    sys.exit(%(stop)d)
elif verb == "rollout" and target == "status":
    if state.get("rollout_sets_base") is not None:
        state["base"] = state["rollout_sets_base"]
        save()
    if state.get("rollout_fails"):
        sys.stderr.write("error: timed out waiting for the condition\\n")
        sys.exit(1)
else:
    sys.stderr.write("stub kubectl: unexpected call %%r\\n" %% (args,))
    sys.exit(2)
""" % {"cr": _CR, "broker": _BROKER, "stop": _KUBECTL_STOP}

# uv as run-gitops-pilot.sh calls it up to its base-mode decision: `uv sync`
# succeeds, case_var's `uv run --no-sync python - <task.yaml> <name>` runs the
# real snippet, the HOLD_SUPPORTED probe (the same call with no arguments)
# answers no, and anything else stops the wrapper.
_STUB_UV = """\
import os, sys
args = sys.argv[1:]
if args[:1] == ["sync"]:
    sys.exit(0)
if args[:4] == ["run", "--no-sync", "python", "-"] and len(args) == 6 and args[4].endswith("task.yaml"):
    os.execv(sys.executable, [sys.executable, "-"] + args[4:])
if args == ["run", "--no-sync", "python", "-"]:
    print("no")
    sys.exit(0)
sys.exit(%d)
""" % _UV_STOP


# gh as gitops-run-prs.sh calls it: `gh api <path>`, answered from the state
# file, or failing when the state says so.
_STUB_GH = """\
import json, os, sys
state = json.load(open(os.environ["STUB_STATE"]))
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if state.get("fails"):
    sys.stderr.write("HTTP 502: Bad Gateway\\n")
    sys.exit(1)
print(json.dumps(state["pulls"]))
"""


class _StubbedScriptTest(unittest.TestCase):
    """A temp dir with stub executables first on PATH, a state file and a call log."""

    stubs = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        # The scripts' own python3 calls get this interpreter, which has pyyaml.
        # A wrapper, not a symlink: a venv is found from the path the interpreter
        # was invoked by, so a symlink elsewhere runs the base interpreter without
        # the venv's packages (render-broken-base.sh imports yaml through it).
        python3 = self.bin_dir / "python3"
        python3.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n")
        python3.chmod(0o755)
        for name, source in self.stubs.items():
            stub = self.bin_dir / name
            stub.write_text(f"#!{sys.executable}\n{source}")
            stub.chmod(0o755)
        self.state_path = self.tmp / "state.json"
        self.log_path = self.tmp / "calls.log"
        self.log_path.write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_path.read_text())

    def calls(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def run_script(self, argv, env):
        base = get_isolated_test_env(bin_dir=self.bin_dir)
        for key in list(base):
            if key.startswith(("GITOPS_", "TF_VAR_", "AGENT_", "BENCH_", "DEVOPS_BENCH_")) or key in ("TASK", "CASE", "BASE_BRANCH_MODE"):
                del base[key]
        base.update({"STUB_STATE": str(self.state_path), "STUB_LOG": str(self.log_path)})
        base.update(env)
        return subprocess.run(argv, capture_output=True, text=True, env=base, timeout=60)


class RunBranchSeedTest(_StubbedScriptTest):
    """run-branch.sh create with GITOPS_SEED_DEFAULT_BRANCH=true."""

    stubs = {"curl": _STUB_CURL}

    def setUp(self):
        super().setUp()
        token = self.tmp / "token"
        token.write_text("ghp_test\n")
        self.env = {
            "GITOPS_REPO": _REPO,
            "GITOPS_RUN_BRANCH": _RUN,
            "GITOPS_TOKEN_FILE": str(token),
            "GITOPS_BASE_SHA": _BASE_SHA,
            "GITOPS_TASK": "b-0022b",
            "GITOPS_TASK_PATH": "tasks/b-0022b",
            "GITOPS_MANIFESTS_DIR": str(_MANIFESTS / "b-0022b"),
            "GITOPS_SEED_DEFAULT_BRANCH": "true",
        }

    def routes(self, base_has_task, default_head, fast_forward_code=200, base_parents=()):
        repo = f"/repos/{_SLUG}"
        routes = [
            {"method": "GET", "path": f"{repo}/contents/tasks/b-0022b\\?ref={_BASE_SHA}",
             "code": 200 if base_has_task else 404},
            # check_default_seed's root check, and commit_stage for a base
            # without the task directory.
            {"method": "GET", "path": f"{repo}/git/commits/{_BASE_SHA}", "code": 200,
             "body": {"tree": {"sha": "t" * 40}, "parents": [{"sha": p} for p in base_parents]}},
            {"method": "POST", "path": f"{repo}/git/blobs", "code": 201, "body": {"sha": "b" * 40}},
            {"method": "POST", "path": f"{repo}/git/trees", "code": 201, "body": {"sha": "d" * 40}},
            {"method": "GET", "path": "/user", "code": 200, "body": {"login": "bench-bot"}},
            {"method": "POST", "path": f"{repo}/git/commits", "code": 201, "body": {"sha": _COMMIT_SHA}},
            # check_default_seed.
            {"method": "GET", "path": repo, "code": 200, "body": {"default_branch": "main"}},
            {"method": "GET", "path": f"{repo}/git/refs/heads/main", "code": 200,
             "body": {"object": {"sha": default_head}}},
            {"method": "POST", "path": f"{repo}/git/refs", "code": 201, "body": {}},
            {"method": "PATCH", "path": f"{repo}/git/refs/heads/main", "code": fast_forward_code,
             "body": {} if fast_forward_code == 200 else {"message": "Update is not a fast forward"}},
            {"method": "DELETE", "path": f"{repo}/git/refs/heads/{_RUN}", "code": 204},
        ]
        self.write_state({"routes": routes})

    def test_default_branch_away_from_the_base_is_refused_before_the_run_branch_is_created(self):
        self.routes(base_has_task=True, default_head=_ELSEWHERE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("refusing to move it", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "a refused seed must write nothing, the run branch included")

    def test_failed_fast_forward_deletes_the_run_branch(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA, fast_forward_code=422)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        writes = [(c["method"], c["path"]) for c in self.calls() if c["path"].startswith(f"/repos/{_SLUG}/git/refs")
                  and c["method"] != "GET"]
        self.assertEqual(writes, [
            ("POST", f"/repos/{_SLUG}/git/refs"),
            ("PATCH", f"/repos/{_SLUG}/git/refs/heads/main"),
            ("DELETE", f"/repos/{_SLUG}/git/refs/heads/{_RUN}"),
        ])

    def test_default_branch_at_the_base_is_fast_forwarded_onto_the_run_branch_commit(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        patches = [c for c in self.calls() if c["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["path"], f"/repos/{_SLUG}/git/refs/heads/main")
        self.assertEqual(json.loads(patches[0]["data"]), {"sha": _COMMIT_SHA, "force": False})
        self.assertFalse([c for c in self.calls() if c["method"] == "DELETE"])
        # A render that fails inside commit_stage's command substitution does not
        # stop create; it shows only as blobs with nothing in them.
        blobs = [json.loads(c["data"]) for c in self.calls() if c["method"] == "POST" and c["path"].endswith("/git/blobs")]
        self.assertTrue(blobs and all(b["content"] for b in blobs), "the blobs carry the broken render's files")

    def test_base_with_history_is_refused_before_any_write(self):
        self.routes(base_has_task=False, default_head=_BASE_SHA, base_parents=[_ELSEWHERE_SHA])
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("has parents", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "a base that is not a root commit must write nothing, git objects included")

    def test_staged_history_is_refused_before_its_healthy_commit_is_written(self):
        self.routes(base_has_task=True, default_head=_BASE_SHA)
        state = self.state()
        # What commit_stage reads of the staged history's parent.
        state["routes"].insert(0, {"method": "GET", "path": f"/repos/{_SLUG}/git/commits/{_ELSEWHERE_SHA}",
                                   "code": 200, "body": {"tree": {"sha": "t" * 40}, "parents": []}})
        self.write_state(state)
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"],
                               {**self.env, "GITOPS_HISTORY_PARENT_SHA": _ELSEWHERE_SHA})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not offered for staged history", proc.stderr)
        writes = [c for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [], "staged history must be refused before any git object is written")

    def test_default_already_at_the_target_needs_no_write_on_a_base_with_history(self):
        self.routes(base_has_task=True, default_head=_BASE_SHA, base_parents=[_ELSEWHERE_SHA])
        proc = self.run_script(["bash", str(_RUN_BRANCH), "create"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already at", proc.stdout)
        writes = [(c["method"], c["path"]) for c in self.calls() if c["method"] != "GET"]
        self.assertEqual(writes, [("POST", f"/repos/{_SLUG}/git/refs")])


class AgentBaseBranchTest(_StubbedScriptTest):
    """agent-base-branch.sh pin and unpin."""

    stubs = {"kubectl": _STUB_KUBECTL}

    env = {
        "AGENT_HOST_CONTEXT": "agent-host",
        "AGENT_NAMESPACE": "kubeagents-system",
        "GITOPS_REPO": _REPO,
        "GITOPS_RUN_BRANCH": _RUN,
        # Short, so a wait that never matches fails the test in seconds.
        "AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC": "2",
        "AGENT_BASE_BRANCH_POLL_SECONDS": "1",
    }

    def given(self, **overrides):
        state = {"base": "", "crd_has_field": True, "cr_exists": True, "repository": _SLUG}
        state.update(overrides)
        self.write_state(state)

    def run_action(self, action, **env):
        return self.run_script(["bash", str(_AGENT_BASE_BRANCH), action], {**self.env, **env})

    def patches(self, patch_type):
        return [c for c in self.calls() if c[4:5] == ["patch"] and f"--type={patch_type}" in c]

    def test_pin_sets_the_base_and_waits_for_the_broker(self):
        self.given()
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["base"], _RUN)
        self.assertTrue([c for c in self.calls() if c[4:6] == ["rollout", "status"]])

    def test_pin_refuses_an_existing_different_base_and_leaves_it(self):
        self.given(base="production")
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertEqual(self.state()["base"], "production")
        self.assertEqual(self.patches("merge") + self.patches("json"), [])

    def test_a_base_written_between_the_read_and_the_patch_is_not_overwritten(self):
        self.given(written_before_patch=_OTHER_RUN)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Conflict", proc.stderr)
        self.assertEqual(self.state()["base"], _OTHER_RUN)
        self.assertEqual(self.patches("json"), [], "the undo must leave the other run's base")

    def test_failed_wait_after_the_patch_removes_this_runs_base(self):
        self.given(rollout_fails=True)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.state()["base"], "")
        self.assertEqual(len(self.patches("json")), 1)

    def test_undo_leaves_a_base_that_no_longer_names_this_run(self):
        self.given(rollout_fails=True, rollout_sets_base=_OTHER_RUN)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.state()["base"], _OTHER_RUN)
        self.assertEqual(self.patches("json"), [])

    def test_patch_applied_but_reported_failed_is_undone(self):
        self.given(patch_fails_after_apply=True)
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.state()["base"], "", "the undo trap must be armed before the patch")

    def test_broker_naming_another_repository_is_refused_and_the_pin_undone(self):
        self.given(repository="example-org/another-repo")
        proc = self.run_action("pin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CREDENTIAL_PROXY_BASE_REPOSITORY is 'example-org/another-repo'", proc.stderr)
        self.assertEqual(self.state()["base"], "")
        self.assertEqual(len(self.patches("json")), 1)

    def test_failed_broker_reads_in_the_wait_count_as_not_yet(self):
        self.given(broker_read_failures=2)
        # A ceiling with margin: bash's SECONDS ticks on wall-clock boundaries,
        # so a 2s timeout can expire after about one second of waiting.
        proc = self.run_action("pin", AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC="10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["base"], _RUN)
        self.assertEqual(self.state()["broker_read_failures"], 0)

    def test_unreadable_crd_fails_the_pin_before_any_write(self):
        self.given(crd_unreadable=True)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("cannot read the CRD", proc.stderr)
        self.assertNotIn("API server dropped it", proc.stdout)
        self.assertEqual(self.state()["base"], "")
        self.assertEqual(self.patches("merge") + self.patches("json"), [], "an unreadable CRD must write nothing")

    def test_pin_on_a_crd_without_the_field_reads_back_logs_it_and_succeeds(self):
        self.given(crd_has_field=False)
        proc = self.run_action("pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("API server dropped it", proc.stdout)
        self.assertEqual(self.state()["base"], "")
        self.assertEqual(self.patches("json"), [])
        # The CRD read carries no -n, so drop the context and namespace flags.
        verbs = [[a for i, a in enumerate(c) if a not in ("--context", "-n") and c[i - 1] not in ("--context", "-n")][:2]
                 for c in self.calls()]
        crd_read = verbs.index(["get", "crd"])
        merge = verbs.index(["patch", _CR])
        self.assertLess(crd_read, merge, "the CRD is read before the patch")
        self.assertIn(["get", _CR], verbs[merge + 1:], "the base is read back after the patch")
        self.assertIn("read back: spec.integration.baseBranch=''", proc.stdout)

    def test_poll_and_timeout_settings_must_be_whole_seconds_above_zero(self):
        for name, value in (
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "0"),
            ("AGENT_BASE_BRANCH_POLL_SECONDS", "fast"),
            ("AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC", "5m"),
        ):
            with self.subTest(name=name, value=value):
                self.given()
                self.log_path.write_text("")
                proc = self.run_action("pin", **{name: value})
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(f"{name} must be a whole number of seconds above zero, not '{value}'", proc.stderr)
                self.assertEqual(self.calls(), [], "a bad setting is refused before kubectl runs")

    def test_unpin_whose_removal_keeps_failing_retries_then_warns_and_succeeds(self):
        self.given(base=_RUN, remove_fails=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("failed 3 times", proc.stderr)
        self.assertEqual(len(self.patches("json")), 3)
        self.assertEqual(self.state()["base"], _RUN)

    def test_unpin_removal_tests_the_run_branch_before_removing(self):
        self.given(base=_RUN)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["base"], "")
        self.assertEqual(self.state()["json_patches"], [[
            {"op": "test", "path": "/spec/integration/baseBranch", "value": _RUN},
            {"op": "remove", "path": "/spec/integration/baseBranch"},
        ]])

    def test_unpin_removal_applied_but_reported_failed_stops_retrying(self):
        self.given(base=_RUN, remove_fails_after_apply=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("failed 3 times", proc.stderr)
        self.assertNotIn("WARN", proc.stderr)
        self.assertIn("baseBranch is gone", proc.stdout)
        self.assertEqual(len(self.patches("json")), 1)
        self.assertEqual(self.state()["base"], "")
        self.assertTrue([c for c in self.calls() if c[4:6] == ["rollout", "status"]],
                        "the broker wait still runs once the field is gone")

    def test_unpin_retry_leaves_a_base_another_run_set_in_between(self):
        self.given(base=_RUN, remove_fails=True, remove_fails_sets_base=_OTHER_RUN)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"baseBranch is now '{_OTHER_RUN}', not this run's; leaving it", proc.stdout)
        self.assertEqual(len(self.patches("json")), 1)
        self.assertEqual(self.state()["base"], _OTHER_RUN)

    def test_unpin_with_a_failing_rollout_wait_removes_the_base_and_succeeds(self):
        self.given(base=_RUN, rollout_fails=True)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["base"], "")
        self.assertIn("WARN", proc.stderr)

    def test_unpin_leaves_another_base(self):
        self.given(base="production")
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["base"], "production")
        self.assertEqual(self.patches("json"), [])

    def test_unpin_without_the_platformagent_warns_and_succeeds(self):
        self.given(cr_exists=False)
        proc = self.run_action("unpin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARN", proc.stderr)


class WrapperCaseSelectionTest(_StubbedScriptTest):
    """run-gitops-pilot.sh from its TASK/CASE resolution to its base-mode decision."""

    stubs = {"uv": _STUB_UV, "kubectl": _STUB_KUBECTL}

    def setUp(self):
        super().setUp()
        self.given_base("")

    def given_base(self, base, cr_exists=True):
        self.write_state({"base": base, "crd_has_field": True, "cr_exists": cr_exists, "repository": _SLUG})

    def run_wrapper(self, wrapper=_WRAPPER, **env):
        token = self.tmp / "token"
        token.write_text("ghp_test\n")
        return self.run_script(["bash", str(wrapper)], {
            "GCP_PROJECT_ID": "example-project",
            "AGENT_HOST_CONTEXT": "agent-host",
            "GITOPS_REPO": _REPO,
            "GITOPS_TOKEN_FILE": str(token),
            "CLUSTER_NAME": "gitops-pilot-t1",
            # Given, so the wrapper reads neither the repository nor LiteLLM.
            "GITOPS_BROKEN_BASE_SHA": _BASE_SHA,
            "AGENT_MODEL": "example-model",
            # The task copy's mktemp lands here, so a run killed at the timeout
            # leaves nothing outside the test's directory.
            "TMPDIR": str(self.tmp),
            **env,
        })

    def wrapper_with_case(self, case, text):
        """A copy of the wrapper whose ./tasks holds one more case, as a run's frozen copy is."""
        bench = self.tmp / "bench"
        (bench / "hack").mkdir(parents=True)
        for script in (_WRAPPER, _WRAPPER.parent / "gitops-run-repo.sh"):
            shutil.copy(script, bench / "hack" / script.name)
        (bench / "tasks" / case).mkdir(parents=True)
        (bench / "tasks" / case / "task.yaml").write_text(text)
        return bench / "hack" / _WRAPPER.name

    def test_task_that_disagrees_with_the_case_is_refused(self):
        proc = self.run_wrapper(TASK="b-0011", CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("disagrees with b-0022b-gitops-pinned-base's gitops_task b-0022b", proc.stderr)
        self.assertNotIn("==> run", proc.stdout)

    def test_case_alone_takes_the_task_from_the_case(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn(
            "==> run gitops-pilot-t1: case b-0022b-gitops-pinned-base, branch run/gitops-pilot-t1/b-0022b",
            proc.stdout,
        )

    def test_another_runs_pin_on_the_platformagent_is_refused_as_leaked(self):
        self.given_base(_OTHER_RUN)
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"spec.integration.baseBranch is '{_OTHER_RUN}', not this run's {_RUN}: a leaked pin",
                      proc.stderr)
        self.assertIn('patch platformagents.kubeagents.x-k8s.io/platform-agent --type=json', proc.stderr)
        self.assertEqual(self.state()["base"], _OTHER_RUN, "the wrapper only refuses; it removes nothing")

    def test_this_runs_own_pin_or_an_install_base_does_not_stop_the_wrapper(self):
        for base in (_RUN, "production"):
            with self.subTest(base=base):
                self.given_base(base)
                proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
                self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
                self.assertNotIn("leaked pin", proc.stderr)

    def test_unreadable_platformagent_stops_the_wrapper(self):
        self.given_base("", cr_exists=False)
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("whether an earlier run left a pin there is unknown", proc.stderr)

    def test_a_case_that_pins_the_base_leaves_the_default_branch_switch_off(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops-pinned-base")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn("==> base branch via the PlatformAgent's spec.integration.baseBranch", proc.stdout)
        self.assertIsNone(self.state()["switch_at_token_read"])

    def test_a_case_that_pins_nothing_switches_the_default_branch(self):
        proc = self.run_wrapper(CASE="b-0022b-gitops")
        self.assertEqual(proc.returncode, _KUBECTL_STOP, proc.stderr)
        self.assertIn("==> base branch via repository default", proc.stdout)
        self.assertEqual(self.state()["switch_at_token_read"], "true")

    def test_a_case_that_turns_the_switch_off_and_pins_nothing_is_refused(self):
        pinned = (_WRAPPER.parents[1] / "tasks" / "b-0022b-gitops-pinned-base" / "task.yaml").read_text()
        pin_line = "    gitops_pin_agent_base_branch: true\n"
        self.assertIn(pin_line, pinned)
        case = "b-0022b-gitops-no-base"
        proc = self.run_wrapper(wrapper=self.wrapper_with_case(case, pinned.replace(pin_line, "")), CASE=case)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(f"{case} sets gitops_switch_default_branch false and pins no base", proc.stderr)
        self.assertNotIn("switch_at_token_read", self.state())



class RunPullRequestsTest(_StubbedScriptTest):
    """gitops-run-prs.sh, the wrapper's post-run pull-request listing."""

    stubs = {"gh": _STUB_GH}

    started = "2026-10-05T12:00:00Z"

    def pull(self, number, base, head, created_at):
        return {"number": number, "base": {"ref": base}, "head": {"ref": head},
                "html_url": f"https://github.com/{_SLUG}/pull/{number}", "created_at": created_at,
                "title": "ignored"}

    def run_listing(self):
        return self.run_script(["bash", str(_RUN_PRS), _SLUG, self.started, "main"], {"GH_TOKEN": "ghp_test"})

    def test_pull_requests_since_the_run_started_are_listed_with_their_bases(self):
        self.write_state({"pulls": [
            self.pull(3, "main", "platform-agent/fix-checkout-main", "2026-10-05T12:30:00Z"),
            self.pull(2, _RUN, "platform-agent/fix-checkout-run", "2026-10-05T12:00:00Z"),
            self.pull(1, "main", "platform-agent/earlier", "2026-10-05T11:59:59Z"),
        ]})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            {"number": 3, "base": "main", "head": "platform-agent/fix-checkout-main",
             "url": f"https://github.com/{_SLUG}/pull/3", "created_at": "2026-10-05T12:30:00Z"},
            {"number": 2, "base": _RUN, "head": "platform-agent/fix-checkout-run",
             "url": f"https://github.com/{_SLUG}/pull/2", "created_at": "2026-10-05T12:00:00Z"},
        ])
        self.assertIn(f"pull requests opened since {self.started}: 2, 1 onto main", proc.stderr)
        self.assertIn(f"#3 base main head platform-agent/fix-checkout-main https://github.com/{_SLUG}/pull/3",
                      proc.stderr)
        (call,) = self.calls()
        self.assertEqual(call[:1], ["api"])
        self.assertTrue(call[1].startswith(f"repos/{_SLUG}/pulls?state=all&sort=created&direction=desc"), call)

    def test_failed_listing_yields_null_not_an_empty_list(self):
        self.write_state({"fails": True})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), None)
        self.assertIn("WARN could not list the pull requests", proc.stderr)

    def test_no_pull_requests_since_the_start_is_an_empty_list(self):
        self.write_state({"pulls": [self.pull(1, "main", "platform-agent/earlier", "2026-10-04T09:00:00Z")]})
        proc = self.run_listing()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])
        self.assertIn("2026-10-05T12:00:00Z: 0, 0 onto main", proc.stderr)


if __name__ == "__main__":
    unittest.main()
