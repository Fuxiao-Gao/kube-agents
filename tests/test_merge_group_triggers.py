"""Every workflow behind a required check on `main` declares `merge_group`.

A GitHub merge queue runs the required checks on its own temporary branch and
waits for each one to report there. A required check whose workflow has no
`merge_group` trigger never reports, and the queue holds every group until the
status-check timeout. Nothing else notices the trigger going missing: a
workflow edited without it still runs on every pull request exactly as before.
This roster is the ten required contexts as of the Tide-to-merge-queue
migration (gke-labs/kube-agents#1363), less `cla/google`, which is an external
app's check run rather than a workflow, plus `Run A2A Module Tests`, which
became required on 2026-10-13. `merge-gate.yml` posts the two checks that
stand in for Tide in the queue, and is held to the same trigger. Every job in
these workflows also sets its own `timeout-minutes`, because the queue holds all
merges behind a hung required job.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_MERGE_GROUP = "merge_group"
_REQUIRED_CHECK_WORKFLOWS = (
    "a2a-test.yml",
    "actionlint.yml",
    "docker-build.yml",
    "docs-check.yml",
    "k8s-operator-test.yml",
    "prettier.yml",
    "python-tests.yml",
    "validate-pr-title.yml",
    "validate.yml",
)


def _on(path: pathlib.Path):
    doc = yaml.safe_load(path.read_text())
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    return doc.get("on", doc.get(True))


def _triggers(path: pathlib.Path) -> set[str]:
    on = _on(path)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    return {str(key) for key in on}


#: The queue's own gate checks. `main`-only, so the release-line test below does not apply.
_GATE_WORKFLOW = "merge-gate.yml"

#: The check names `main`'s merge-queue ruleset requires from a workflow job (`cla/google` is an
#: external app's). A queue run waits for these alone, so any other job there only spends runners.
_REQUIRED_JOB_NAMES = frozenset({
    "actionlint",
    "build",
    "prettier",
    "validate",
    "Run Controller Tests",
    "Run Python Unit Tests",
    "Documentation Checks",
    "Validate Conventional Commit PR Title",
    "Agent instructions cite assets that exist",
    "Run A2A Module Tests",
    "Smoke gate",
    "Merge labels",
})
#: The condition that keeps a job off merge-queue runs.
_SKIP_ON_MERGE_GROUP = "github.event_name != 'merge_group'"
#: Jobs the rule below does not read by name, and why.
_QUEUE_JOB_EXEMPTIONS = {
    ("merge-gate.yml", "gate"): "its matrix names the jobs `Smoke gate` and `Merge labels`",
    ("merge-gate.yml", "pull-request"): "it runs only on pull_request_target and status events",
}

_PULL_REQUEST = "pull_request"
_RELEASE_LINE_GLOB = "release/**"

#: Well under the 360-minute default. A queue that requires every check holds
#: all merges behind a hung job at its front, so each job carries its own bound.
_MAX_JOB_TIMEOUT_MINUTES = 60


def _timeout_problem(minutes) -> str | None:
    """Why `minutes` is not a usable `timeout-minutes`, or None when it is.

    PyYAML reads `true`, `yes` and `on` as booleans, and `bool` is a subclass of
    `int`, so a typo passes an `int` check alone.
    """
    if minutes is None:
        return "sets no timeout-minutes"
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return f"sets timeout-minutes to {minutes!r}, not a number of minutes"
    if minutes <= 0:
        return f"sets timeout-minutes to {minutes}, which bounds nothing"
    if minutes > _MAX_JOB_TIMEOUT_MINUTES:
        return f"sets timeout-minutes to {minutes}, over {_MAX_JOB_TIMEOUT_MINUTES}"
    return None


def _merge_group_workflows() -> list[pathlib.Path]:
    """Every workflow whose triggers include `merge_group`."""
    return [path for path in sorted(_WORKFLOWS.glob("*.yml")) if _MERGE_GROUP in _triggers(path)]


class MergeGroupTriggerTest(unittest.TestCase):
    def test_required_check_workflows_run_on_merge_group(self) -> None:
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn(_MERGE_GROUP, _triggers(_WORKFLOWS / name))

    def test_required_check_workflows_run_for_pull_requests_against_a_release_line(self) -> None:
        """A pull request against a `release/` branch must be able to earn the same contexts.

        Tide reads the target branch's protection, and protection that requires
        a context a workflow never posts holds the pull request forever. A
        workflow whose `pull_request` trigger is filtered to `main` never posts
        on such a pull request. A filter is allowed; one that omits the release
        branches is not.
        """
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertIn(_PULL_REQUEST, on, f"{name} does not run on pull_request")
                branches = (on[_PULL_REQUEST] or {}).get("branches")
                if branches is not None:
                    self.assertIn(_RELEASE_LINE_GLOB, branches, f"{name} filters pull_request to {branches}")

    def test_every_job_in_the_queue_sets_a_timeout(self) -> None:
        """A job with no `timeout-minutes` runs for 360 minutes when it hangs.

        In the queue a hung required job at the front holds every merge until it
        times out: `Run Python Unit Tests` hung for six hours twice on 2026-10-08.
        Each job's bound is about twice its longest successful job run in the 14
        days before it was set, capped at the ceiling below
        (gke-labs/kube-agents#1363).
        """
        for name in (*_REQUIRED_CHECK_WORKFLOWS, _GATE_WORKFLOW):
            jobs = yaml.safe_load((_WORKFLOWS / name).read_text())["jobs"]
            for job_id, job in jobs.items():
                with self.subTest(workflow=name, job=job_id):
                    self.assertIsNone(_timeout_problem(job.get("timeout-minutes")), f"{name} job {job_id}")

    def test_a_timeout_that_bounds_nothing_is_refused(self) -> None:
        """A boolean (`true`, `yes`, `on`), zero or a negative number is not a bound."""
        for minutes in (None, True, False, 0, -5, "30", _MAX_JOB_TIMEOUT_MINUTES + 1):
            with self.subTest(minutes=minutes):
                self.assertIsNotNone(_timeout_problem(minutes))
        for minutes in (1, 30, _MAX_JOB_TIMEOUT_MINUTES):
            with self.subTest(minutes=minutes):
                self.assertIsNone(_timeout_problem(minutes))

    def test_only_required_checks_run_in_the_queue(self) -> None:
        """A job that is not a required check is kept off merge-queue runs.

        The queue waits only for the required checks and merges past a red in
        any other job, so an optional job there gives no protection and takes
        runners from the shared budget: on 2026-10-22 the two optional Python
        jobs were about 17 of the 45 runner-minutes of each queue test. Such a
        job still runs on pull requests and on pushes to `main`. Every
        workflow that declares `merge_group` is read, so a new one is held to
        this too. Two jobs are exempt by name (`_QUEUE_JOB_EXEMPTIONS`).
        """
        for path in _merge_group_workflows():
            jobs = yaml.safe_load(path.read_text())["jobs"]
            for job_id, job in jobs.items():
                job_name = job.get("name", job_id)
                if job_name in _REQUIRED_JOB_NAMES or (path.name, job_id) in _QUEUE_JOB_EXEMPTIONS:
                    continue
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertEqual(
                        _SKIP_ON_MERGE_GROUP,
                        str(job.get("if", "")).strip(),
                        f"{path.name} job {job_id} ({job_name!r}) is not a required check but runs on merge_group",
                    )

    def test_no_required_check_skips_the_queue(self) -> None:
        """The reverse: a required check never carries the merge-queue skip.

        A skipped job reports success to a required check, so a job that is
        promoted to required with the skip still on it would pass every queue
        group without running.
        """
        for path in _merge_group_workflows():
            jobs = yaml.safe_load(path.read_text())["jobs"]
            for job_id, job in jobs.items():
                if job.get("name", job_id) not in _REQUIRED_JOB_NAMES:
                    continue
                with self.subTest(workflow=path.name, job=job_id):
                    self.assertNotIn(_SKIP_ON_MERGE_GROUP, str(job.get("if", "")), f"{path.name} job {job_id}")

    def test_the_gate_workflow_runs_on_merge_group_for_main(self) -> None:
        on = _on(_WORKFLOWS / _GATE_WORKFLOW)
        self.assertEqual((on[_MERGE_GROUP] or {}).get("branches"), ["main"])

    def test_merge_group_stays_on_main(self) -> None:
        """The merge queue is `main`'s; a release line merges through Tide alone."""
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertEqual((on[_MERGE_GROUP] or {}).get("branches"), ["main"])


if __name__ == "__main__":
    unittest.main()
