"""gitops-audit.py: isolation counts from a run record's worker trajectory."""

from __future__ import annotations

import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hack" / "gitops-audit.py"
_spec = importlib.util.spec_from_loader("gitops_audit", importlib.machinery.SourceFileLoader("gitops_audit", str(_SCRIPT)))
gitops_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gitops_audit)


def _worker(name, args, at, agent="platform"):
    return {"name": name, "args": args, "result": "", "status": "ok", "agent": agent, "task": "t_1", "session": "s_1", "at": at}


def test_counts_reads_and_lookups_before_the_fix_only():
    record = {
        "trajectory": [
            {"name": "kanban_create", "args": {}, "result": "t_1", "status": "ok"},
            _worker("terminal", '{"command": "kubectl get pods -n payments"}', 100),
            _worker("mcp__gke__describe", '{"kind": "deployment"}', 110, agent="cluster-x"),
            _worker("terminal", '{"command": "gh pr list --repo o/r"}', 120),
            _worker("skill_view", '{"name": "submit-suggestion"}', 105),
            _worker("kanban_show", '{"task_id": "t_1"}', 106),
            _worker("kanban_show", '{"task_id": "t_other"}', 107),
            _worker("terminal", '{"command": "kubectl --kubeconfig=/p/k.yaml -n payments describe rs checkout-1"}', 108, agent="cluster-x"),
            _worker("kanban_show", '{}', 109, agent="cluster-x"),
            _worker("terminal", '{"command": "python3 submit_suggestion.py submit --help"}', 112),
            _worker("terminal", '{"command": "python3 submit_suggestion.py prepare --repo o/r"}', 125),
            _worker("terminal", '{"command": "python3 \\"$S/submit_suggestion.py\\" submit \\\\\n --handle h"}', 130),
            _worker("terminal", '{"command": "gh pr view 7"}', 140),
            _worker("terminal", '{"command": "kubectl get pods"}', 150),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "pr_url": "u", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["worker_entries"] == 13
    assert report["delegated_to_cluster_agent"] is True
    assert report["cluster_reads_before_fix"] == 3
    assert report["repo_lookups_before_fix"] == 2
    assert report["repo_lookups"] == ['terminal {"command": "gh pr list --repo o/r"}', 'kanban_show {"task_id": "t_other"}']
    assert report["gitops_outcome"] == "merged"


def test_falls_back_to_the_merge_time_without_a_submit_call():
    record = {
        "trajectory": [
            _worker("terminal", '{"command": "kubectl get pods"}', 1_758_153_600),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["fix_submitted_at"] == "2026-09-18T00:00:00+00:00"
    assert report["cluster_reads_before_fix"] == 1


def test_record_without_worker_entries():
    report = gitops_audit.audit({"trajectory": [{"name": "kanban_create", "args": {}, "result": "", "status": "ok"}]})
    assert report["worker_entries"] == 0
    assert report["fix_submitted_at"] is None
    assert report["cluster_reads_before_fix"] == 0
