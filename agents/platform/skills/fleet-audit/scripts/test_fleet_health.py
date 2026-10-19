#!/usr/bin/env python3
"""Tests for fleet_health.py, the fleet health sweep."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import fleet_health as fh  # noqa: E402

# fleet_drift's suite has the reasoning: the host's scope guard must not turn
# these listing tests into refusals.
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)
import fleet_scope_args as _fleet_scope_args  # noqa: E402

_fleet_scope_args.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"


def run_of(rc, stdout="", stderr=""):
    return fh.Run(["x"], rc, stdout, stderr, 0.01)


def cluster(name, location="us-central1", status="RUNNING", pools=None, packages=("SYSTEM_COMPONENTS", "POD"), **extra):
    doc = {
        "name": name,
        "location": location,
        "status": status,
        "nodePools": pools if pools is not None else [{"name": "default-pool", "status": "RUNNING"}],
        "monitoringConfig": {"componentConfig": {"enableComponents": list(packages)}},
    }
    doc.update(extra)
    return doc


def lister(clusters_by_project, *, projects=None):
    """A `run` that answers discovery and one `clusters list` per project."""
    projects = projects or list(clusters_by_project)

    def run(argv, **kwargs):
        if argv[:4] == ["gcloud", "config", "get-value", "project"]:
            return run_of(0, projects[0])
        if argv[:3] == ["gcloud", "projects", "list"]:
            return run_of(0, "\n".join(projects))
        if argv[:4] == ["gcloud", "container", "clusters", "list"]:
            project = argv[argv.index("--project") + 1]
            value = clusters_by_project[project]
            if isinstance(value, fh.Run):
                return value
            return run_of(0, json.dumps(value))
        raise AssertionError(f"unexpected command: {argv}")

    return run


def series(cluster_name, location="us-central1", ksm=False, value="1", **labels):
    key = "cluster" if ksm else "cluster_name"
    return {"metric": {key: cluster_name, "location": location, **labels}, "value": [0, value]}


def querier(hits=None, present=None, errors=None):
    """A `query` keyed on the metric name in the PromQL: `present` lists the
    clusters reporting each metric (default: every name in `hits`, plus `all`)."""
    hits = hits or {}
    errors = errors or {}
    calls = []

    def query(project, promql):
        calls.append((project, promql))
        for metric in ("node_status_condition", "container_restart_count", "kube_pod_status_phase"):
            if metric in promql:
                break
        if metric in errors:
            return [], errors[metric]
        if promql.startswith("count by"):
            ksm = metric == "kube_pod_status_phase"
            return [series(c, loc, ksm=ksm) for c, loc in (present or {}).get(metric, [])], None
        return hits.get(metric, []), None

    query.calls = calls
    return query


ALL_PRESENT = {
    "node_status_condition": [("a", "us-central1")],
    "container_restart_count": [("a", "us-central1")],
    "kube_pod_status_phase": [("a", "us-central1")],
}


def checks(entry):
    return [(c["check"], c["severity"], c["object"]) for c in entry["candidates"]]


class ControlPlaneTest(unittest.TestCase):
    def test_a_healthy_cluster_has_no_candidate(self):
        self.assertEqual(fh.control_plane_candidates(cluster("a")), [])

    def test_cluster_status_severity(self):
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="ERROR"))}),
                         [("cluster-not-running", "critical", "Cluster/a")])
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="DEGRADED"))})[0][1],
                         "critical")
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="RECONCILING"))})[0][1],
                         "minor")

    def test_the_status_message_is_in_the_excerpt(self):
        found = fh.control_plane_candidates(cluster("a", status="DEGRADED", statusMessage="nodes unreachable"))
        self.assertEqual(found[0]["excerpt"], "status DEGRADED: nodes unreachable")

    def test_conditions_and_pool_states(self):
        pools = [
            {"name": "p1", "status": "RUNNING_WITH_ERROR", "conditions": [{"code": "GCE_STOCKOUT", "message": "no capacity"}]},
            {"name": "p2", "status": "ERROR"},
            {"name": "p3", "status": "PROVISIONING"},
        ]
        found = fh.control_plane_candidates(cluster("a", pools=pools, conditions=[{"canonicalCode": "UNAVAILABLE", "message": "x"}]))
        self.assertEqual(checks({"candidates": found}), [
            ("cluster-condition", "major", "Cluster/a"),
            ("node-pool-not-running", "major", "NodePool/p1"),
            ("node-pool-condition", "major", "NodePool/p1"),
            ("node-pool-not-running", "critical", "NodePool/p2"),
            ("node-pool-not-running", "minor", "NodePool/p3"),
        ])
        self.assertEqual(found[2]["excerpt"], "GCE_STOCKOUT: no capacity")


class MetricsTest(unittest.TestCase):
    def setUp(self):
        fh.declared_scope.set(None, None)

    def entry(self, query, c=None):
        return fh.collect_fleet(run=lister({"acme": [c or cluster("a")]}), query=query)["clusters"][0]

    def test_each_metric_hit_lands_on_its_cluster(self):
        query = querier(present=ALL_PRESENT, hits={
            "node_status_condition": [series("a", node_name="n1", status="Unknown")],
            "container_restart_count": [series("a", value="12.0", namespace_name="seeded-debug",
                                               pod_name="payments-api-1", container_name="api"),
                                        series("other", namespace_name="x", pod_name="y", container_name="z")],
            "kube_pod_status_phase": [series("a", ksm=True, namespace="shop", pod="web-1")],
        })
        entry = self.entry(query)
        self.assertEqual(checks(entry), [
            ("node-not-ready", "major", "Node/n1"),
            ("container-restarting", "major", "Pod/payments-api-1"),
            ("pod-pending", "major", "Pod/web-1"),
        ])
        restart = entry["candidates"][1]
        self.assertEqual(restart["namespace"], "seeded-debug")
        self.assertEqual(restart["excerpt"], "container api restarted 12 times in the last 1h")
        self.assertNotIn("checks_unevaluated", entry)

    def test_every_query_is_pinned_to_its_project(self):
        query = querier(present=ALL_PRESENT)
        self.entry(query)
        self.assertEqual(len(query.calls), 6)
        for project, promql in query.calls:
            self.assertEqual(project, "acme")
            self.assertIn('project_id="acme"', promql)

    def test_a_cluster_with_no_series_is_unevaluated_not_healthy(self):
        entry = self.entry(querier(present={"node_status_condition": [("a", "us-central1")]}))
        reasons = {u["check"]: u["reason"] for u in entry["checks_unevaluated"]}
        self.assertEqual(set(reasons), {"container-restarting", "pod-pending"})
        self.assertIn("no series", reasons["container-restarting"])

    def test_a_metrics_package_that_is_off_is_named(self):
        entry = self.entry(querier(present=ALL_PRESENT | {"kube_pod_status_phase": []}),
                           cluster("a", packages=("SYSTEM_COMPONENTS",)))
        self.assertEqual(entry["checks_unevaluated"],
                         [{"check": "pod-pending", "reason": "the cluster's POD metrics package is off"}])

    def test_a_failed_read_is_unevaluated_with_its_error(self):
        entry = self.entry(querier(present=ALL_PRESENT, errors={"container_restart_count": "HTTP 403: denied"}))
        self.assertEqual(entry["checks_unevaluated"],
                         [{"check": "container-restarting", "reason": "metric read failed: HTTP 403: denied"}])

    def test_a_cluster_that_is_not_running_reports_no_metrics(self):
        entry = self.entry(querier(present=ALL_PRESENT), cluster("a", status="ERROR"))
        self.assertEqual(checks(entry), [("cluster-not-running", "critical", "Cluster/a")])
        self.assertEqual({u["check"] for u in entry["checks_unevaluated"]}, set(fh.METRIC_CHECKS))

    def test_clusters_with_one_name_in_two_locations_stay_apart(self):
        query = querier(present={"container_restart_count": [("a", "us-central1"), ("a", "europe-west1")]},
                        hits={"container_restart_count": [series("a", location="europe-west1", value="5",
                                                                 namespace_name="n", pod_name="p", container_name="c")]})
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("a", location="europe-west1")]}), query=query)
        by_name = {e["name"]: e for e in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/a"]["candidates"], [])
        self.assertEqual(len(by_name["acme/europe-west1/a"]["candidates"]), 1)

    def test_a_long_list_is_capped_and_counted(self):
        many = [series("a", value="9", namespace_name="n", pod_name=f"p{i}", container_name="c")
                for i in range(fh.MAX_METRIC_CANDIDATES_PER_CHECK + 3)]
        entry = self.entry(querier(present=ALL_PRESENT, hits={"container_restart_count": many}))
        self.assertEqual(len(entry["candidates"]), fh.MAX_METRIC_CANDIDATES_PER_CHECK)
        self.assertEqual(entry["checks_unevaluated"], [{"check": "container-restarting", "reason": "3 more not listed"}])


class FleetTest(unittest.TestCase):
    def setUp(self):
        fh.declared_scope.set(None, None)

    def tearDown(self):
        fh.declared_scope.set(None, None)

    def test_a_disabled_api_is_no_cluster_and_no_error(self):
        disabled = run_of(1, stderr="ERROR: Kubernetes Engine API has not been used in project acme")
        manifest = fh.collect_fleet(run=lister({"acme": disabled}), query=querier())
        self.assertEqual(manifest["clusters"], [])
        self.assertNotIn("error", manifest)

    def test_a_failed_listing_is_a_gate_failed_row(self):
        manifest = fh.collect_fleet(
            run=lister({"acme": [cluster("a")], "beta": run_of(1, stderr="PERMISSION_DENIED")}),
            query=querier(present=ALL_PRESENT))
        rows = {e["name"]: e["outcome"] for e in manifest["clusters"]}
        self.assertEqual(rows, {"acme/us-central1/a": "collected", "project/beta": "gate-failed"})
        self.assertNotIn("error", manifest)

    def test_no_readable_cluster_sets_the_error(self):
        manifest = fh.collect_fleet(run=lister({"acme": run_of(1, stderr="PERMISSION_DENIED")}), query=querier())
        self.assertIn("no cluster could be read", manifest["error"])

    def test_an_incomplete_listing_keeps_its_clusters_and_flags_the_project(self):
        partial = run_of(0, json.dumps([cluster("a")]), stderr="WARNING: The following zones did not respond: x")
        manifest = fh.collect_fleet(run=lister({"acme": partial}), query=querier(present=ALL_PRESENT))
        self.assertEqual([e["outcome"] for e in manifest["clusters"]], ["collected", "gate-failed"])

    def test_the_declared_scope_is_swept_and_nothing_is_listed(self):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([cluster("a")]))
            raise AssertionError(f"the declared scope is the sweep; nothing should be listed: {argv}")

        fh.declared_scope.set("acme", "beta=denied")
        manifest = fh.collect_fleet(run=run, query=querier(present=ALL_PRESENT))
        self.assertEqual([e["name"] for e in manifest["clusters"]], ["acme/us-central1/a", "project/UNENUMERATED_PROJECTS"])
        self.assertIn("beta (denied)", manifest["clusters"][1]["error"])

    def test_an_excluded_cluster_is_dropped_and_never_reported(self):
        fh.declared_scope.set("acme", None, "acme/us-central1/secret-ops")
        query = querier(present={"container_restart_count": [("a", "us-central1"), ("secret-ops", "us-central1")]},
                        hits={"container_restart_count": [series("secret-ops", value="9", namespace_name="n",
                                                                 pod_name="p", container_name="c")]})
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("secret-ops")]}), query=query)
        self.assertEqual([e["name"] for e in manifest["clusters"]], ["acme/us-central1/a"])
        self.assertNotIn("secret-ops", json.dumps(manifest["clusters"][0]["candidates"]))

    def test_a_project_that_raises_does_not_lose_the_fleet(self):
        def query(project, promql):
            if project == "beta":
                raise RuntimeError("boom")
            return [], None

        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a")], "beta": [cluster("b")]}), query=query)
        rows = {e["name"]: e["outcome"] for e in manifest["clusters"]}
        self.assertEqual(rows, {"acme/us-central1/a": "collected", "project/beta": "gate-failed"})

    def test_the_summary_counts_clusters_with_a_finding(self):
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a", status="ERROR"), cluster("b")]}),
                                    query=querier(present=ALL_PRESENT))
        self.assertEqual(fh.summary_lines(manifest),
                         ["2 cluster(s) read, 1 with a finding -- cluster-not-running: 1 (acme/us-central1/a)"])


class PromqlQueryFnTest(unittest.TestCase):
    class Response:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    def query_with(self, response):
        session = mock.Mock()
        session.get.return_value = response
        rows, error = fh.promql_query_fn(session)("acme", "up")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://monitoring.googleapis.com/v1/projects/acme/location/global/prometheus/api/v1/query")
        return rows, error

    def test_a_success_returns_the_rows(self):
        rows, error = self.query_with(self.Response(200, {"status": "success", "data": {"result": [{"metric": {}}]}}))
        self.assertEqual((rows, error), ([{"metric": {}}], None))

    def test_a_refusal_an_error_and_a_bad_body_are_errors(self):
        self.assertTrue(self.query_with(self.Response(403, "denied"))[1].startswith("HTTP 403"))
        self.assertIn("bad_data", self.query_with(self.Response(200, {"status": "error", "errorType": "bad_data"}))[1])
        self.assertIn("not JSON", self.query_with(self.Response(200, ValueError("x")))[1])


class MainTest(unittest.TestCase):
    def test_help_lists_the_scope_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            fh.main(["--help"])
        for flag in ("--scope-projects", "--scope-unread", "--scope-exclude-clusters", "--project"):
            self.assertIn(flag, out.getvalue())

    def test_main_passes_the_scope_flags_to_the_declared_scope(self):
        with mock.patch.object(fh, "collect_fleet", return_value={"clusters": []}) as collect, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fh.main(["--scope-projects", "acme", "--scope-exclude-clusters", "acme/us-central1/x"]), 0)
        collect.assert_called_once_with(None)
        self.assertEqual(list(fh.declared_scope.projects), ["acme"])
        fh.declared_scope.set(None, None)


if __name__ == "__main__":
    unittest.main()
