#!/usr/bin/env python3
"""fleet_health.py — one read-only sweep that says which clusters in the fleet are unhealthy.

A fleet health question ("are my clusters all healthy?") used to become one
kanban card per cluster: each card cold-starts a Cluster Agent that improvises
with `kubectl`, and the board runs five user cards at a time, so a 209-cluster
fleet queued for more than half an hour (#3302). Every signal those cards read
has a fleet-wide source that costs a fixed number of calls per project, not
per cluster:

- `gcloud container clusters list`, once per project: each cluster's and each
  node pool's `status`, `statusMessage` and `conditions`.
- Managed Service for Prometheus, through the credential broker's read-only
  relay (`docs/designs/gcp-api-relay.md`): NotReady nodes
  (`kubernetes_io:node_status_condition`), containers that restarted
  repeatedly in the last hour (`kubernetes_io:container_restart_count`), and
  pods stuck Pending (kube-state-metrics `kube_pod_status_phase`, which GKE
  exports when the cluster's `POD` metrics package is on).

Each metric is read twice per project: once for the findings and once for
which clusters report the metric at all, so a cluster with no series is
"not evaluated", never silently healthy.

The manifest follows docs/designs/fleet-audit-collector-manifest.md. It feeds a
chat answer only: no ledger stream, no `audit_report.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
AUDIT_ID = "fleet-health"

REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:REVISION_DIGEST_CHARS]

DEFAULT_TIMEOUT_S = 60
TIMEOUT_RC = 124
# The credential proxy's admitted count at the operator's default limit;
# collect.py's MAX_WORKERS comment has the reasoning.
MAX_WORKERS = 4
ERROR_EXCERPT_CHARS = 300
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SUMMARY_MAX_CLUSTER_NAMES = 5

OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
PROJECT_TARGET_PREFIX = "project/"
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read."
)
SHARED_SCRIPT_DIRS = (
    "/opt/defaults/scripts",
    "/opt/data/scripts",
    *([str(Path(__file__).resolve().parents[3] / "scripts")] if len(Path(__file__).resolve().parents) > 3 else []),
)
for _shared_dir in SHARED_SCRIPT_DIRS:
    if _shared_dir not in sys.path:
        sys.path.append(_shared_dir)
import fleet_scope_args  # noqa: E402

declared_scope = fleet_scope_args.DeclaredScope()

# fleet_drift.py's API_DISABLED_MARKERS and ZONE_TIMEOUT_MARKER have the reasoning.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
ZONE_TIMEOUT_MARKER = "did not respond"

# Cluster and node pool states. RUNNING is healthy; the transitional states are
# reported as minor because an upgrade or repair passes through them; ERROR and
# DEGRADED are what the question is asking about.
HEALTHY_STATUS = "RUNNING"
CLUSTER_CRITICAL_STATUSES = frozenset({"ERROR", "DEGRADED"})
POOL_CRITICAL_STATUSES = frozenset({"ERROR"})
POOL_MAJOR_STATUSES = frozenset({"RUNNING_WITH_ERROR"})
TRANSITIONAL_STATUSES = frozenset({"PROVISIONING", "RECONCILING", "STOPPING"})
# A cluster in these states serves metrics; any other gives none to read.
METRICS_STATUSES = frozenset({"RUNNING", "RECONCILING"})
# The cluster's monitoringConfig packages each metric needs.
SYSTEM_METRICS_PACKAGE = "SYSTEM_COMPONENTS"
POD_METRICS_PACKAGE = "POD"

CHECK_CLUSTER_STATUS = "cluster-not-running"
CHECK_CLUSTER_CONDITION = "cluster-condition"
CHECK_POOL_STATUS = "node-pool-not-running"
CHECK_POOL_CONDITION = "node-pool-condition"
CHECK_NODE_NOT_READY = "node-not-ready"
CHECK_RESTARTING = "container-restarting"
CHECK_PENDING = "pod-pending"
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"

# The metric reads. Restarts: at least RESTART_THRESHOLD in RESTART_WINDOW is a
# crash loop, not one rescheduled pod. Pending: the phase held for the whole
# PENDING_WINDOW, so a pod that is merely being scheduled does not count.
RESTART_THRESHOLD = 3
RESTART_WINDOW = "1h"
PENDING_WINDOW = "10m"
PROMQL_URL = "https://monitoring.googleapis.com/v1/projects/{project}/location/global/prometheus/api/v1/query"
MONITORING_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
MONITORING_TIMEOUT_S = 60
CREDENTIAL_PROXY_URL_ENV = "CREDENTIAL_PROXY_URL"
HTTP_OK = 200
NODE_QUERY = (
    'kubernetes_io:node_status_condition{{monitored_resource="k8s_node",project_id="{project}",'
    'condition="Ready",status!="True"}} == 1'
)
NODE_PRESENCE_QUERY = (
    'count by (location, cluster_name) (kubernetes_io:node_status_condition'
    '{{monitored_resource="k8s_node",project_id="{project}",condition="Ready"}})'
)
RESTART_QUERY = (
    "sum by (location, cluster_name, namespace_name, pod_name, container_name) "
    '(increase(kubernetes_io:container_restart_count{{monitored_resource="k8s_container",'
    'project_id="{project}"}}[' + RESTART_WINDOW + "])) >= " + str(RESTART_THRESHOLD)
)
RESTART_PRESENCE_QUERY = (
    "count by (location, cluster_name) (kubernetes_io:container_restart_count"
    '{{monitored_resource="k8s_container",project_id="{project}"}})'
)
PENDING_QUERY = (
    'min_over_time(kube_pod_status_phase{{project_id="{project}",phase="Pending"}}['
    + PENDING_WINDOW + "]) == 1"
)
PENDING_PRESENCE_QUERY = 'count by (location, cluster) (kube_pod_status_phase{{project_id="{project}"}})'
# The label each metric family names its cluster with: the GKE system metrics
# use the monitored resource's `cluster_name`, kube-state-metrics `cluster`.
SYSTEM_CLUSTER_LABEL = "cluster_name"
KSM_CLUSTER_LABEL = "cluster"

# A cluster with hundreds of restarting pods would otherwise put them all in
# the manifest; the answer names the first ones and counts the rest.
MAX_METRIC_CANDIDATES_PER_CHECK = 20

IMPACT = {
    CHECK_CLUSTER_STATUS: "The control plane is not serving normally; workloads and kubectl may be affected.",
    CHECK_CLUSTER_CONDITION: "GKE reports a problem condition on the cluster.",
    CHECK_POOL_STATUS: "Nodes in this pool may be missing or failing to join.",
    CHECK_POOL_CONDITION: "GKE reports a problem condition on the node pool.",
    CHECK_NODE_NOT_READY: "Pods on this node are not being scheduled or may be evicted.",
    CHECK_RESTARTING: "The container is crash-looping; its workload is degraded or down.",
    CHECK_PENDING: "The pod has not been scheduled or cannot start.",
}
METRIC_CHECKS = (CHECK_NODE_NOT_READY, CHECK_RESTARTING, CHECK_PENDING)


def log(msg: str) -> None:
    print(f"[fleet_health] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]
# `query(project, promql) -> (result rows, error or None)`.
QueryFn = Callable[[str, str], "tuple[list[dict], str | None]"]


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def _record(argv_str: str, rc: int, duration_s: float, output: str) -> dict:
    return {
        "command": argv_str,
        "rc": rc,
        "duration_s": round(duration_s, 2),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def default_session():
    """A session for the Monitoring read API: the broker's relay in the shell
    sandbox (no token ever reaches the sandbox), else this host's ADC.
    fleet_waste.default_monitoring_session has the reasoning."""
    if os.environ.get(CREDENTIAL_PROXY_URL_ENV):
        import credential_proxy_client

        return credential_proxy_client.ApiSession()
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=[MONITORING_SCOPE])
    return AuthorizedSession(credentials)


def promql_query_fn(session) -> QueryFn:
    def query(project: str, promql: str) -> tuple[list[dict], str | None]:
        try:
            response = session.get(
                PROMQL_URL.format(project=project), params={"query": promql}, timeout=MONITORING_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 -- any transport failure is one unevaluated check
            return [], f"{type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS]
        if response.status_code != HTTP_OK:
            return [], f"HTTP {response.status_code}: {response.text}"[:ERROR_EXCERPT_CHARS]
        try:
            body = response.json()
        except ValueError:
            return [], "the Prometheus API answered with a body that is not JSON"
        if body.get("status") != "success":
            return [], f"{body.get('errorType', 'error')}: {body.get('error', '')}"[:ERROR_EXCERPT_CHARS]
        return list((body.get("data") or {}).get("result") or []), None

    return query


class Discovery(NamedTuple):
    projects: list[str]
    error: str | None
    partial: str | None = None


def discover_fleet(base_project: str | None, *, run: RunFn) -> Discovery:
    """fleet_drift.discover_fleet's rules: `--project`, else the declared scope
    from the fleet_scope tool, else the active project plus `projects list`."""
    if base_project:
        override_error = declared_scope.override_error(base_project)
        if override_error:
            return Discovery([], override_error)
        return Discovery([base_project], None, SCOPED_RUN_NOTE.format(project=base_project))
    if declared_scope.declared:
        if not declared_scope.projects:
            return Discovery([], declared_scope.empty_error())
        return Discovery(list(declared_scope.projects), None, declared_scope.note())
    active = run(["gcloud", "config", "get-value", "project"])
    base = active.stdout.strip() if active.rc == 0 else ""
    projects = [base] if base else []
    listed = run(["gcloud", "projects", "list", "--format", "value(projectId)"])
    if listed.rc != 0:
        if not projects:
            return Discovery([], f"project discovery failed: `gcloud projects list` rc={listed.rc}: "
                             f"{listed.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}")
        return Discovery(projects, None, f"`gcloud projects list` rc={listed.rc}: only the active "
                         f"project {base!r} was read; how many other projects the fleet holds is unknown.")
    names = [p.strip() for p in listed.stdout.splitlines() if p.strip()]
    projects += [p for p in names if p not in projects]
    if not projects:
        return Discovery([], "project discovery named no project")
    if base and base not in names:
        return Discovery(projects, None, f"`gcloud projects list` did not name the active project {base!r}, "
                         "so it is filtered; other projects in the fleet may be missing.")
    return Discovery(projects, None)


def list_clusters(project: str, *, run: RunFn) -> tuple[list[dict], dict | None, str | None]:
    """One `clusters list`. Returns `(clusters, command_record, error)`; a
    disabled Kubernetes Engine API is zero clusters and no error."""
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    result = run(argv)
    parsed = None
    if result.rc == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, list):
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            return [], None, None
        return [], None, f"clusters list rc={result.rc}: {result.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
    parsed = declared_scope.drop_excluded(project, parsed)
    record = _record(shlex.join(argv), result.rc, result.duration_s, result.stdout)
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        return parsed, record, f"clusters list rc=0 but incomplete: {' '.join(incomplete)[:ERROR_EXCERPT_CHARS]}"
    return parsed, record, None


def _candidate(check: str, severity: str, obj: str, excerpt: str, namespace: str = "") -> dict:
    return {
        "check": check,
        "namespace": namespace,
        "object": obj,
        "severity": severity,
        "excerpt": excerpt,
        "impact": IMPACT[check],
        "needs_triage": None,
    }


def _conditions_text(conditions: list) -> list[str]:
    texts = []
    for cond in conditions or []:
        if isinstance(cond, dict):
            code = cond.get("canonicalCode") or cond.get("code") or "UNKNOWN"
            texts.append(f"{code}: {cond.get('message', '')}".strip())
    return texts


def control_plane_candidates(cluster: dict) -> list[dict]:
    """The cluster's own and its node pools' status and conditions."""
    name = cluster.get("name", "")
    out = []
    status = cluster.get("status", "")
    if status != HEALTHY_STATUS:
        severity = SEVERITY_MINOR if status in TRANSITIONAL_STATUSES else (
            SEVERITY_CRITICAL if status in CLUSTER_CRITICAL_STATUSES else SEVERITY_MAJOR)
        message = cluster.get("statusMessage", "")
        out.append(_candidate(CHECK_CLUSTER_STATUS, severity, f"Cluster/{name}",
                              f"status {status}" + (f": {message}" if message else "")))
    for text in _conditions_text(cluster.get("conditions")):
        out.append(_candidate(CHECK_CLUSTER_CONDITION, SEVERITY_MAJOR, f"Cluster/{name}", text))
    for pool in cluster.get("nodePools") or []:
        pool_name = pool.get("name", "")
        pool_status = pool.get("status", "")
        if pool_status != HEALTHY_STATUS:
            if pool_status in POOL_CRITICAL_STATUSES:
                severity = SEVERITY_CRITICAL
            elif pool_status in POOL_MAJOR_STATUSES:
                severity = SEVERITY_MAJOR
            elif pool_status in TRANSITIONAL_STATUSES:
                severity = SEVERITY_MINOR
            else:
                severity = SEVERITY_MAJOR
            message = pool.get("statusMessage", "")
            out.append(_candidate(CHECK_POOL_STATUS, severity, f"NodePool/{pool_name}",
                                  f"status {pool_status}" + (f": {message}" if message else "")))
        for text in _conditions_text(pool.get("conditions")):
            out.append(_candidate(CHECK_POOL_CONDITION, SEVERITY_MAJOR, f"NodePool/{pool_name}", text))
    return out


def _cluster_key(labels: dict, cluster_label: str) -> tuple[str, str]:
    return labels.get("location", ""), labels.get(cluster_label, "")


def _value(row: dict) -> str:
    value = row.get("value") or []
    return str(value[1]) if len(value) > 1 else ""


def read_metrics(project: str, *, query: QueryFn) -> dict:
    """The three metric checks for one project, keyed by `(location, cluster)`.

    Returns `{"present": {check: set(keys)}, "hits": {check: {key: [candidate]}},
    "errors": {check: error}, "queries": [record]}`."""
    reads = (
        (CHECK_NODE_NOT_READY, NODE_QUERY, NODE_PRESENCE_QUERY, SYSTEM_CLUSTER_LABEL),
        (CHECK_RESTARTING, RESTART_QUERY, RESTART_PRESENCE_QUERY, SYSTEM_CLUSTER_LABEL),
        (CHECK_PENDING, PENDING_QUERY, PENDING_PRESENCE_QUERY, KSM_CLUSTER_LABEL),
    )
    present: dict[str, set] = {}
    hits: dict[str, dict] = {}
    errors: dict[str, str] = {}
    records = []
    for check, finding_q, presence_q, cluster_label in reads:
        for promql, is_presence in ((presence_q, True), (finding_q, False)):
            text = promql.format(project=project)
            t0 = time.monotonic()
            rows, error = query(project, text)
            records.append(_record(f"promql {project}: {text}", 0 if error is None else 1,
                                   time.monotonic() - t0, json.dumps(rows, sort_keys=True)))
            if error is not None:
                errors[check] = error
                break
            for row in rows:
                labels = row.get("metric") or {}
                key = _cluster_key(labels, cluster_label)
                if is_presence:
                    present.setdefault(check, set()).add(key)
                    continue
                if check == CHECK_NODE_NOT_READY:
                    node = labels.get("node_name", "")
                    cand = _candidate(check, SEVERITY_MAJOR, f"Node/{node}",
                                      f"Ready condition is {labels.get('status', 'not True')}")
                elif check == CHECK_RESTARTING:
                    restarts = round(float(_value(row) or 0))
                    cand = _candidate(check, SEVERITY_MAJOR, f"Pod/{labels.get('pod_name', '')}",
                                      f"container {labels.get('container_name', '')} restarted {restarts} times "
                                      f"in the last {RESTART_WINDOW}", labels.get("namespace_name", ""))
                else:
                    cand = _candidate(check, SEVERITY_MAJOR, f"Pod/{labels.get('pod', '')}",
                                      f"phase Pending for at least {PENDING_WINDOW}", labels.get("namespace", ""))
                hits.setdefault(check, {}).setdefault(key, []).append(cand)
    return {"present": present, "hits": hits, "errors": errors, "queries": records}


def _location(cluster: dict) -> str:
    return cluster.get("location") or cluster.get("zone") or ""


def _packages(cluster: dict) -> set[str]:
    config = ((cluster.get("monitoringConfig") or {}).get("componentConfig") or {})
    return set(config.get("enableComponents") or [])


def cluster_entry(project: str, cluster: dict, record: dict | None, metrics: dict) -> dict:
    name = cluster.get("name", "")
    location = _location(cluster)
    key = (location, name)
    candidates = control_plane_candidates(cluster)
    unevaluated = []
    limitations = []
    status = cluster.get("status", "")
    packages = _packages(cluster)
    for check in METRIC_CHECKS:
        needed = POD_METRICS_PACKAGE if check == CHECK_PENDING else SYSTEM_METRICS_PACKAGE
        if status not in METRICS_STATUSES:
            unevaluated.append({"check": check, "reason": f"cluster status is {status}; it reports no metrics"})
        elif check in metrics["errors"]:
            unevaluated.append({"check": check, "reason": f"metric read failed: {metrics['errors'][check]}"})
        elif key not in metrics["present"].get(check, set()):
            reason = (f"the cluster's {needed} metrics package is off" if packages and needed not in packages
                      else "the cluster reported no series for this metric")
            unevaluated.append({"check": check, "reason": reason})
        else:
            found = metrics["hits"].get(check, {}).get(key, [])
            if len(found) > MAX_METRIC_CANDIDATES_PER_CHECK:
                extra = len(found) - MAX_METRIC_CANDIDATES_PER_CHECK
                found = found[:MAX_METRIC_CANDIDATES_PER_CHECK]
                limitations.append(f"{check}: {extra} more not listed")
            candidates.extend(found)
    entry = {
        "name": f"{project}/{location}/{name}",
        "project": project,
        "location": location,
        "autopilot": bool((cluster.get("autopilot") or {}).get("enabled")),
        "status": status,
        "outcome": OUTCOME_COLLECTED,
        "commands": ([record] if record else []) + metrics["queries"],
        "candidates": candidates,
    }
    if unevaluated:
        entry["checks_unevaluated"] = unevaluated
    if limitations:
        entry["limitations"] = limitations
    return entry


def collect_project(project: str, *, run: RunFn, query: QueryFn) -> tuple[list[dict], str | None]:
    clusters, record, error = list_clusters(project, run=run)
    if not clusters:
        return [], error
    metrics = read_metrics(project, query=query)
    return [cluster_entry(project, c, record, metrics) for c in clusters], error


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, query: QueryFn | None = None,
                  max_workers: int = MAX_WORKERS) -> dict:
    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    discovery = discover_fleet(project, run=run)
    if discovery.projects and query is None:
        query = promql_query_fn(default_session())
    entries: list[dict] = []
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run, query=query): p for p in discovery.projects}
        for future in as_completed(futures):
            name = futures[future]
            try:
                found, error = future.result()
            except Exception as exc:  # noqa: BLE001 -- one bad project must not lose the fleet
                found, error = [], f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS]
            entries.extend(found)
            if error:
                failed[name] = error
    entries.sort(key=lambda e: e["name"])
    entries += [
        {"name": f"{PROJECT_TARGET_PREFIX}{p}", "project": p, "location": "global",
         "outcome": OUTCOME_GATE_FAILED, "error": e}
        for p, e in sorted(failed.items())
    ]
    if discovery.partial:
        entries.append({"name": UNENUMERATED_PROJECTS_TARGET, "project": "", "location": "global",
                        "outcome": OUTCOME_GATE_FAILED, "error": discovery.partial[:ERROR_EXCERPT_CHARS]})
    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": time.strftime(TIMESTAMP_FORMAT, time.gmtime()),
        "clusters": entries,
    }
    collected = [e for e in entries if e["outcome"] == OUTCOME_COLLECTED]
    if discovery.error:
        manifest["error"] = discovery.error
    elif failed and not collected:
        first, error = sorted(failed.items())[0]
        manifest["error"] = (f"no cluster could be read: {len(failed)} of {len(discovery.projects)} "
                             f"project(s) failed `clusters list`; {first}: {error}")[:ERROR_EXCERPT_CHARS]
    return manifest


def summary_lines(manifest: dict) -> list[str]:
    """The closing stderr lines: clusters read, unhealthy clusters and the checks
    that fired, then every check left unevaluated and where, so an answer never
    reports an unread check as healthy."""
    clusters = [c for c in manifest.get("clusters") or [] if isinstance(c, dict)]
    collected = [c for c in clusters if c.get("outcome") == OUTCOME_COLLECTED]
    unread = [c for c in clusters if c.get("outcome") == OUTCOME_GATE_FAILED]
    by_check: dict[str, set] = {}
    for cluster in collected:
        for cand in cluster.get("candidates") or []:
            by_check.setdefault(cand["check"], set()).add(cluster["name"])
    unhealthy = {name for names in by_check.values() for name in names}
    head = f"{len(collected)} cluster(s) read, {len(unhealthy)} with a finding"
    if unread:
        head += f", {len(unread)} target(s) unread"
    parts = []
    for check in sorted(by_check):
        names = sorted(by_check[check])
        shown = ", ".join(names[:SUMMARY_MAX_CLUSTER_NAMES])
        if len(names) > SUMMARY_MAX_CLUSTER_NAMES:
            shown += f", and {len(names) - SUMMARY_MAX_CLUSTER_NAMES} more"
        parts.append(f"{check}: {len(names)} ({shown})")
    lines = [head + (" -- " + "; ".join(parts) if parts else "")]
    skipped: dict[str, list[str]] = {}
    for cluster in collected:
        for item in cluster.get("checks_unevaluated") or []:
            skipped.setdefault(item["check"], []).append(f"{cluster['name']} ({item['reason']})")
    if skipped:
        lines.append(
            "not evaluated, so not healthy either -- name each in the answer: "
            + "; ".join(f"{check} on {', '.join(sorted(where))}" for check, where in sorted(skipped.items()))
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="one project to read (on an install with a declared scope, one the "
                        "fleet_scope tool lists); omit to sweep --scope-projects, else every project gcloud lists")
    fleet_scope_args.add_scope_arguments(parser)
    args = parser.parse_args(argv)
    declared_scope.set(args.scope_projects, args.scope_unread, args.scope_exclude_clusters)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    if manifest.get("error"):
        log(f"WARNING: {manifest['error']}")
        return 1
    for line in summary_lines(manifest):
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
