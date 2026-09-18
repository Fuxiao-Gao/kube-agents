#!/usr/bin/env python3
"""Isolation audit of a GitOps fix-cycle run record (gke-labs/kube-agents#1773).

Reads a devops-bench ``results.json`` whose ``trajectory`` carries the worker
entries #1746 records (each tagged with ``agent``, ``task``, ``session``,
``at``) and the harness's ``gitops_fix_cycle`` entry, and prints the three
values a handoff states per run:

- ``cluster_reads_before_fix``: worker calls that read the task cluster
  (kubectl / MCP cluster tools / Cluster Agent delegation) before the fix was
  submitted;
- ``repo_lookups_before_fix``: worker calls that could have shown another run's
  work before the fix: pull-request listing or viewing, foreign card views,
  session or memory search;
- ``fix_submitted_at``: the first worker call that submits the fix (a
  submit-suggestion run, a ``gh pr create`` or a ``git push``), falling back to
  the pull request's merge time from the harness entry.

Usage: gitops-audit.py <results.json> [--json]
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

GITOPS_ENTRY_NAME = "gitops_fix_cycle"
#: Worker call names or argument fragments that read a cluster.
CLUSTER_READ_RE = re.compile(
    r"kubectl\s+(get|describe|logs|top|events|explain|rollout\s+history|api-resources)"
    r"|mcp__gke|mcp__k8s|mcp__kubernetes|gke_|k8s_|kubernetes_",
    re.IGNORECASE,
)
#: Delegating to a Cluster Agent counts as a cluster read by proxy.
DELEGATION_RE = re.compile(r"kanban_create|delegate", re.IGNORECASE)
#: Calls that could surface another run's work.
REPO_LOOKUP_RE = re.compile(
    r"gh\s+pr\s+(list|view)|gh\s+api\s+\S*pulls|gh\s+search|git\s+log|session_search|memory_search|kanban_show|kanban_list",
    re.IGNORECASE,
)
FIX_SUBMIT_RE = re.compile(r"submit_suggestion|submit-suggestion|gh\s+pr\s+create|git\s+push", re.IGNORECASE)
#: Names of the front agent's own calls that are not evidence either way.
FRONT_AGENT = ""


def _text(entry: dict) -> str:
    args = entry.get("args")
    return f'{entry.get("name", "")} {args if isinstance(args, str) else json.dumps(args or {})}'


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def audit(record: dict) -> dict:
    trajectory = record.get("trajectory") or []
    workers = [e for e in trajectory if e.get("agent")]
    harness = next((e for e in trajectory if e.get("name") == GITOPS_ENTRY_NAME), None)
    outcome = (harness or {}).get("result") or {}

    fix_at = None
    for e in workers:
        if FIX_SUBMIT_RE.search(_text(e)):
            fix_at = _epoch(e.get("at"))
            break
    if fix_at is None:
        fix_at = _epoch(outcome.get("merged_at"))

    def before_fix(e: dict) -> bool:
        at = _epoch(e.get("at"))
        return fix_at is None or at is None or at <= fix_at

    cluster_reads = [e for e in workers if before_fix(e) and (CLUSTER_READ_RE.search(_text(e)) or DELEGATION_RE.search(e.get("name", "")))]
    repo_lookups = [e for e in workers if before_fix(e) and REPO_LOOKUP_RE.search(_text(e))]
    agents = sorted({e["agent"] for e in workers})
    return {
        "worker_entries": len(workers),
        "agents": agents,
        "delegated_to_cluster_agent": any(a != "platform" for a in agents),
        "fix_submitted_at": datetime.fromtimestamp(fix_at, timezone.utc).isoformat() if fix_at else None,
        "cluster_reads_before_fix": len(cluster_reads),
        "repo_lookups_before_fix": len(repo_lookups),
        "repo_lookups": [_text(e)[:160] for e in repo_lookups],
        "gitops_outcome": outcome.get("outcome"),
        "pr_url": outcome.get("pr_url"),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        record = json.load(fh)
    if isinstance(record, list):
        record = record[0]
    report = audit(record)
    if "--json" in argv:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
