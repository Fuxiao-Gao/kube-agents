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
  repeatedly in the last hour and still exist
  (`kubernetes_io:container_restart_count`), and pods Pending for the whole
  window (kube-state-metrics `kube_pod_status_phase`, which GKE exports when
  the cluster's `POD` metrics package is on).

Each metric is read twice per project: once for the findings and once for
which clusters report the metric at all, so a cluster with no series is
"not evaluated", never silently healthy.

The manifest follows docs/designs/fleet-audit-collector-manifest.md, with the
per-project metric reads recorded once under `project_reads` rather than on
every cluster. It feeds a chat answer only: no ledger stream, no
`audit_report.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
# Project reads stop starting this long after the run starts, so a run over a
# credential that lists many projects still prints its manifest inside the
# chat route's 600 s terminal timeout. fleet_waste.py's PROJECT_READ_DEADLINE_S
# has the reasoning.
PROJECT_READ_DEADLINE_S = 420
# `gcloud projects list`'s own timeout: under DEFAULT_TIMEOUT_S a credential that
# sees hundreds of projects is killed mid-listing and the sweep falls back to the
# active project alone. fleet_waste.py's PROJECTS_LIST_TIMEOUT_S has the reasoning.
PROJECTS_LIST_TIMEOUT_S = 240
# The whole run's budget, under the chat route's 600 s terminal timeout. A read
# is never started past it: a metric read left for later is unevaluated, and a
# `clusters list` gets only the time that remains, so the manifest prints
# before the terminal kills the run. The margin is one Monitoring read.
RUN_BUDGET_S = 500
BUDGET_REASON = "not read: the sweep's time budget ran out before this read"
PROJECT_DEADLINE_ERROR = (
    "not read: the sweep stops starting project reads {budget} s after it starts, so it can "
    "end inside its terminal timeout, and this project's turn came after that. Rerun with "
    "`--project {project}` to read it on its own."
)
ERROR_EXCERPT_CHARS = 300
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SUMMARY_MAX_CLUSTER_NAMES = 5

OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
PROJECT_TARGET_PREFIX = "project/"
PROJECT_TARGET_LOCATION = "global"
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
TRANSITIONAL_STATUSES = frozenset({"PROVISIONING", "RECONCILING", "STOPPING"})
# A GKE condition's own code (GCE_STOCKOUT) says more than its generic
# canonicalCode (RESOURCE_EXHAUSTED); either may be absent.
UNKNOWN_CONDITION_CODE = "UNKNOWN"
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
CONTROL_PLANE_CHECKS = (CHECK_CLUSTER_STATUS, CHECK_CLUSTER_CONDITION, CHECK_POOL_STATUS, CHECK_POOL_CONDITION)
METRIC_CHECKS = (CHECK_NODE_NOT_READY, CHECK_RESTARTING, CHECK_PENDING)
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"

# Metric label names. The GKE system metrics name their monitored resource's
# labels `cluster_name`, `namespace_name` and so on; kube-state-metrics uses
# the bare names. The PromQL `by (...)` lists and the reads below must agree,
# so both use these.
LABEL_LOCATION = "location"
LABEL_SYSTEM_CLUSTER = "cluster_name"
LABEL_SYSTEM_NAMESPACE = "namespace_name"
LABEL_SYSTEM_POD = "pod_name"
LABEL_SYSTEM_CONTAINER = "container_name"
LABEL_NODE = "node_name"
LABEL_CONDITION_STATUS = "status"
LABEL_KSM_CLUSTER = "cluster"
LABEL_KSM_NAMESPACE = "namespace"
LABEL_KSM_POD = "pod"
NOT_READY_FALLBACK_STATUS = "not True"

# The metric reads. Restarts: at least RESTART_THRESHOLD in RESTART_WINDOW is a
# crash loop, not one rescheduled pod, and the container must still report, so
# a pod a rollout already replaced is not listed. Pending: the phase held over
# the whole PENDING_WINDOW and also at its start, so a pod created a minute ago
# and still being scheduled does not count.
RESTART_THRESHOLD = 3
RESTART_WINDOW = "1h"
PENDING_WINDOW = "10m"
PROMQL_URL = "https://monitoring.googleapis.com/v1/projects/{project}/location/global/prometheus/api/v1/query"
MONITORING_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
MONITORING_TIMEOUT_S = 60
CREDENTIAL_PROXY_URL_ENV = "CREDENTIAL_PROXY_URL"
HTTP_OK = 200
NO_MONITORING_SESSION = "no Cloud Monitoring session: {error}"
QUERY_OK_RC = 0
QUERY_FAILED_RC = 1

# Query templates for `.format(project=..., exclude=...)`: `{{`/`}}` are the
# PromQL braces, `{project}` and `{exclude}` the fields.
SYSTEM_BY = LABEL_LOCATION + ", " + LABEL_SYSTEM_CLUSTER
NODE_SELECTOR = '{{monitored_resource="k8s_node",project_id="{project}",condition="Ready"{exclude}'
NODE_QUERY = "kubernetes_io:node_status_condition" + NODE_SELECTOR + ',status!="True"}} == 1'
NODE_PRESENCE_QUERY = "count by (" + SYSTEM_BY + ") (kubernetes_io:node_status_condition" + NODE_SELECTOR + "}})"
RESTART_SELECTOR = '{{monitored_resource="k8s_container",project_id="{project}"{exclude}}}'
RESTART_BY = ", ".join((SYSTEM_BY, LABEL_SYSTEM_NAMESPACE, LABEL_SYSTEM_POD, LABEL_SYSTEM_CONTAINER))
RESTART_QUERY = (
    "(sum by (" + RESTART_BY + ") (increase(kubernetes_io:container_restart_count" + RESTART_SELECTOR
    + "[" + RESTART_WINDOW + "])) >= " + str(RESTART_THRESHOLD) + ") and on (" + RESTART_BY
    + ") (count by (" + RESTART_BY + ") (kubernetes_io:container_restart_count" + RESTART_SELECTOR + "))"
)
RESTART_PRESENCE_QUERY = "count by (" + SYSTEM_BY + ") (kubernetes_io:container_restart_count" + RESTART_SELECTOR + ")"
KSM_BY = LABEL_LOCATION + ", " + LABEL_KSM_CLUSTER
PENDING_BY = ", ".join((KSM_BY, LABEL_KSM_NAMESPACE, LABEL_KSM_POD))
PENDING_SELECTOR = '{{project_id="{project}",phase="Pending"{exclude}}}'
PENDING_QUERY = (
    "count by (" + PENDING_BY + ") ((min_over_time(kube_pod_status_phase" + PENDING_SELECTOR + "["
    + PENDING_WINDOW + "]) == 1) and (kube_pod_status_phase" + PENDING_SELECTOR + " offset "
    + PENDING_WINDOW + " == 1))"
)
PENDING_PRESENCE_QUERY = "count by (" + KSM_BY + ') (kube_pod_status_phase{{project_id="{project}"{exclude}}})'
# The excluded clusters' names, so their series never leave the broker.
EXCLUDE_MATCHER = ',{label}!~"{names}"'
# A GKE cluster name: nothing in it is special in a regex or a PromQL string,
# so it goes into the matcher as written. Any other name is left out of the
# matcher, and its series are dropped after the read.
GKE_CLUSTER_NAME = re.compile(r"^[a-z0-9-]+$")

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


def _record(check: str, argv_str: str, rc: int, duration_s: float, output: str) -> dict:
    return {
        "check": check,
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


def unavailable_query_fn(error: str) -> QueryFn:
    """Every metric read fails with `error`, so each metric check is unevaluated
    and the control-plane checks, which need no Monitoring credential, still ship."""
    def query(project: str, promql: str) -> tuple[list[dict], str | None]:
        return [], error

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
    listed = run(["gcloud", "projects", "list", "--format", "value(projectId)"], timeout=PROJECTS_LIST_TIMEOUT_S)
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


def _remaining(stop_at: float | None) -> float | None:
    return None if stop_at is None else stop_at - time.monotonic()


def list_clusters(project: str, *, run: RunFn, stop_at: float | None = None) -> tuple[list[dict], dict | None, str | None]:
    """One `clusters list`, given no more than the run's remaining budget.
    Returns `(clusters, command_record, error)`; a disabled Kubernetes Engine
    API is zero clusters and no error."""
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    left = _remaining(stop_at)
    result = run(argv) if left is None else run(argv, timeout=max(1, min(DEFAULT_TIMEOUT_S, int(left))))
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
    record = _record(",".join(CONTROL_PLANE_CHECKS), shlex.join(argv), result.rc, result.duration_s, result.stdout)
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
            code = cond.get("code")
            if not code or code == UNKNOWN_CONDITION_CODE:
                code = cond.get("canonicalCode") or UNKNOWN_CONDITION_CODE
            texts.append(f"{code}: {cond.get('message', '')}".strip())
    return texts


def _status_severity(status: str, critical: frozenset) -> str:
    if status in critical:
        return SEVERITY_CRITICAL
    if status in TRANSITIONAL_STATUSES:
        return SEVERITY_MINOR
    return SEVERITY_MAJOR


def control_plane_candidates(cluster: dict) -> list[dict]:
    """The cluster's own and its node pools' status and conditions."""
    name = cluster.get("name", "")
    out = []
    status = cluster.get("status", "")
    if status != HEALTHY_STATUS:
        message = cluster.get("statusMessage", "")
        out.append(_candidate(CHECK_CLUSTER_STATUS, _status_severity(status, CLUSTER_CRITICAL_STATUSES),
                              f"Cluster/{name}", f"status {status}" + (f": {message}" if message else "")))
    for text in _conditions_text(cluster.get("conditions")):
        out.append(_candidate(CHECK_CLUSTER_CONDITION, SEVERITY_MAJOR, f"Cluster/{name}", text))
    for pool in cluster.get("nodePools") or []:
        pool_name = pool.get("name", "")
        pool_status = pool.get("status", "")
        if pool_status != HEALTHY_STATUS:
            message = pool.get("statusMessage", "")
            out.append(_candidate(CHECK_POOL_STATUS, _status_severity(pool_status, POOL_CRITICAL_STATUSES),
                                  f"NodePool/{pool_name}",
                                  f"status {pool_status}" + (f": {message}" if message else "")))
        for text in _conditions_text(pool.get("conditions")):
            out.append(_candidate(CHECK_POOL_CONDITION, SEVERITY_MAJOR, f"NodePool/{pool_name}", text))
    return out


def _location(cluster: dict) -> str:
    return cluster.get("location") or cluster.get("zone") or ""


def _exclude_matcher(project: str, listed: list[dict], label: str) -> str:
    """A matcher that keeps an excluded cluster's series out of the read. By
    name, which PromQL can match; a name that an included cluster in another
    location of the same project also carries is left in, and its series are
    dropped by location after the read instead."""
    included = {c.get("name", "") for c in listed}
    names = sorted({name for proj, _loc, name in declared_scope.excluded_clusters
                    if proj == project and name not in included and GKE_CLUSTER_NAME.match(name)})
    if not names:
        return ""
    return EXCLUDE_MATCHER.format(label=label, names="|".join(names))


def _value(row: dict) -> str:
    value = row.get("value") or []
    return str(value[1]) if len(value) > 1 else ""


def read_metrics(project: str, listed: list[dict], *, query: QueryFn, stop_at: float | None = None) -> dict:
    """The three metric checks for one project, keyed by `(location, cluster)`.

    Returns `{"present": {check: set(keys)}, "hits": {check: {key: [candidate]}},
    "errors": {check: error}, "records": [record]}`."""
    system_exclude = _exclude_matcher(project, listed, LABEL_SYSTEM_CLUSTER)
    ksm_exclude = _exclude_matcher(project, listed, LABEL_KSM_CLUSTER)
    reads = (
        (CHECK_NODE_NOT_READY, NODE_QUERY, NODE_PRESENCE_QUERY, LABEL_SYSTEM_CLUSTER, system_exclude),
        (CHECK_RESTARTING, RESTART_QUERY, RESTART_PRESENCE_QUERY, LABEL_SYSTEM_CLUSTER, system_exclude),
        (CHECK_PENDING, PENDING_QUERY, PENDING_PRESENCE_QUERY, LABEL_KSM_CLUSTER, ksm_exclude),
    )
    present: dict[str, set] = {}
    hits: dict[str, dict] = {}
    errors: dict[str, str] = {}
    records = []
    for check, finding_q, presence_q, cluster_label, exclude in reads:
        for promql, is_presence in ((presence_q, True), (finding_q, False)):
            left = _remaining(stop_at)
            if left is not None and left <= 0:
                errors[check] = BUDGET_REASON
                break
            text = promql.format(project=project, exclude=exclude)
            t0 = time.monotonic()
            rows, error = query(project, text)
            records.append(_record(check, f"promql {project}: {text}", QUERY_OK_RC if error is None else QUERY_FAILED_RC,
                                   time.monotonic() - t0, json.dumps(rows, sort_keys=True)))
            if error is not None:
                errors[check] = error
                break
            for row in rows:
                labels = row.get("metric") or {}
                key = (labels.get(LABEL_LOCATION, ""), labels.get(cluster_label, ""))
                if is_presence:
                    present.setdefault(check, set()).add(key)
                    continue
                if check == CHECK_NODE_NOT_READY:
                    cand = _candidate(check, SEVERITY_MAJOR, f"Node/{labels.get(LABEL_NODE, '')}",
                                      f"Ready condition is {labels.get(LABEL_CONDITION_STATUS, NOT_READY_FALLBACK_STATUS)}")
                elif check == CHECK_RESTARTING:
                    restarts = round(float(_value(row) or 0))
                    cand = _candidate(check, SEVERITY_MAJOR, f"Pod/{labels.get(LABEL_SYSTEM_POD, '')}",
                                      f"container {labels.get(LABEL_SYSTEM_CONTAINER, '')} restarted {restarts} times "
                                      f"in the last {RESTART_WINDOW}", labels.get(LABEL_SYSTEM_NAMESPACE, ""))
                else:
                    cand = _candidate(check, SEVERITY_MAJOR, f"Pod/{labels.get(LABEL_KSM_POD, '')}",
                                      f"phase Pending for at least {PENDING_WINDOW}", labels.get(LABEL_KSM_NAMESPACE, ""))
                hits.setdefault(check, {}).setdefault(key, []).append(cand)
    return {"present": present, "hits": hits, "errors": errors, "records": records}


def _packages(cluster: dict) -> set[str]:
    config = ((cluster.get("monitoringConfig") or {}).get("componentConfig") or {})
    return set(config.get("enableComponents") or [])


def cluster_entry(project: str, cluster: dict, record: dict | None, metrics: dict) -> dict:
    name = cluster.get("name", "")
    location = _location(cluster)
    key = (location, name)
    candidates = control_plane_candidates(cluster)
    unevaluated = []
    truncated = []
    packages = _packages(cluster)
    for check in METRIC_CHECKS:
        needed = POD_METRICS_PACKAGE if check == CHECK_PENDING else SYSTEM_METRICS_PACKAGE
        if check in metrics["errors"]:
            error = metrics["errors"][check]
            reason = error if error == BUDGET_REASON else f"metric read failed: {error}"
            unevaluated.append({"check": check, "reason": reason})
        elif key not in metrics["present"].get(check, set()):
            if packages and needed not in packages:
                reason = f"the cluster's {needed} metrics package is off"
            else:
                reason = f"the cluster (status {cluster.get('status', '')}) reported no series for this metric"
            unevaluated.append({"check": check, "reason": reason})
        else:
            found = metrics["hits"].get(check, {}).get(key, [])
            if len(found) > MAX_METRIC_CANDIDATES_PER_CHECK:
                truncated.append(f"{check} lists {MAX_METRIC_CANDIDATES_PER_CHECK} of {len(found)}")
                found = found[:MAX_METRIC_CANDIDATES_PER_CHECK]
            candidates.extend(found)
    entry = {
        "name": f"{project}/{location}/{name}",
        "project": project,
        "location": location,
        "autopilot": bool((cluster.get("autopilot") or {}).get("enabled")),
        "status": cluster.get("status", ""),
        "outcome": OUTCOME_COLLECTED,
        "commands": [record] if record else [],
        "candidates": candidates,
    }
    if unevaluated:
        entry["checks_unevaluated"] = unevaluated
    if truncated:
        entry["limitations"] = "; ".join(truncated) + "; the rest are not listed."
    return entry


def collect_project(project: str, *, run: RunFn, query: QueryFn,
                    stop_at: float | None = None) -> tuple[list[dict], list[dict], str | None]:
    """`(cluster entries, metric read records, error)` for one project."""
    clusters, record, error = list_clusters(project, run=run, stop_at=stop_at)
    if not clusters:
        return [], [], error
    metrics = read_metrics(project, clusters, query=query, stop_at=stop_at)
    return [cluster_entry(project, c, record, metrics) for c in clusters], metrics["records"], error


def _session_query() -> QueryFn:
    try:
        return promql_query_fn(default_session())
    except Exception as exc:  # noqa: BLE001 -- no credential still leaves the control-plane checks
        error = NO_MONITORING_SESSION.format(error=f"{type(exc).__name__}: {exc}")[:ERROR_EXCERPT_CHARS]
        log(f"WARNING: {error}")
        return unavailable_query_fn(error)


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, query: QueryFn | None = None,
                  max_workers: int = MAX_WORKERS, project_budget_s: float = PROJECT_READ_DEADLINE_S,
                  run_budget_s: float = RUN_BUDGET_S) -> dict:
    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    deadline = time.monotonic() + project_budget_s
    stop_at = time.monotonic() + run_budget_s
    discovery = discover_fleet(project, run=run)
    if discovery.projects and query is None:
        query = _session_query()

    def bounded(name: str):
        if time.monotonic() > deadline:
            return None
        return collect_project(name, run=run, query=query, stop_at=stop_at)

    entries: list[dict] = []
    project_reads: dict[str, list[dict]] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(bounded, p): p for p in discovery.projects}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 -- one bad project must not lose the fleet
                result = ([], [], f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS])
            if result is None:
                failed[name] = PROJECT_DEADLINE_ERROR.format(budget=int(project_budget_s), project=name)
                continue
            found, records, error = result
            entries.extend(found)
            if records:
                project_reads[name] = records
            if error:
                failed[name] = error
    entries.sort(key=lambda e: e["name"])
    entries += [
        {"name": f"{PROJECT_TARGET_PREFIX}{p}", "project": p, "location": PROJECT_TARGET_LOCATION,
         "outcome": OUTCOME_GATE_FAILED, "error": e}
        for p, e in sorted(failed.items())
    ]
    if discovery.partial:
        entries.append({"name": UNENUMERATED_PROJECTS_TARGET, "project": "", "location": PROJECT_TARGET_LOCATION,
                        "outcome": OUTCOME_GATE_FAILED, "error": discovery.partial[:ERROR_EXCERPT_CHARS]})
    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_ID,
        "started_at": started_at,
        "finished_at": time.strftime(TIMESTAMP_FORMAT, time.gmtime()),
        "clusters": entries,
        "project_reads": dict(sorted(project_reads.items())),
    }
    collected = [e for e in entries if e["outcome"] == OUTCOME_COLLECTED]
    if discovery.error:
        manifest["error"] = discovery.error
    elif failed and not collected:
        first, error = sorted(failed.items())[0]
        manifest["error"] = (f"no cluster could be read: {len(failed)} of {len(discovery.projects)} "
                             f"project(s) were not read; {first}: {error}")[:ERROR_EXCERPT_CHARS]
    return manifest


def _named(names: list[str]) -> str:
    shown = ", ".join(sorted(names)[:SUMMARY_MAX_CLUSTER_NAMES])
    if len(names) > SUMMARY_MAX_CLUSTER_NAMES:
        shown += f", and {len(names) - SUMMARY_MAX_CLUSTER_NAMES} more"
    return shown


def summary_lines(manifest: dict) -> list[str]:
    """The closing stderr lines: clusters read, unhealthy clusters and the checks
    that fired, then the unevaluated checks grouped by check and reason, so an
    answer never reports an unread check as healthy."""
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
    parts = [f"{check}: {len(names)} ({_named(list(names))})" for check, names in sorted(by_check.items())]
    lines = [head + (" -- " + "; ".join(parts) if parts else "")]
    skipped: dict[tuple[str, str], list[str]] = {}
    for cluster in collected:
        for item in cluster.get("checks_unevaluated") or []:
            skipped.setdefault((item["check"], item["reason"]), []).append(cluster["name"])
    if skipped:
        lines.append(
            "not evaluated, so not healthy either -- name each in the answer: "
            + "; ".join(f"{check} on {len(names)} cluster(s) ({_named(names)}): {reason}"
                        for (check, reason), names in sorted(skipped.items()))
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
