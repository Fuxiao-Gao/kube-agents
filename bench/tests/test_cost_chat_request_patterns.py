"""cost-chat-request-runs-collector's collector and ledger patterns sit at command position.

The worker_commands verifier runs ``re.search`` on each command summary. These
load the task file's patterns and pin them: the red check accepts the
collector command SOUL.md gives the chat route and the one the SOP's form 1
runs, behind the prefixes a worker adds (env, VAR=value, timeout, an
interpreter by path, a quoted script path, ``bash -c``, ``&&``), and rejects a
--help or -h call, an echo or grep that only quotes the command, a ``cat`` or
``python3 -c`` that names the script (bar the documented limits), and
look-alike file names. The case has no passing run yet, so the real commands
here are the ones its red run on main issued, which must not match, and
``audit_report.py`` commands from other cases' runs for the
nothing-was-published safeguard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "cost-chat-request-runs-collector"
    / "task.yaml"
)
SCRIPT = "/opt/data/profiles/platform/skills/fleet-audit/scripts/fleet_waste.py"
REPORT = "/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py"


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _collector() -> re.Pattern[str]:
    (pattern,) = _check("the-cost-collector-ran")["required_patterns"]
    return re.compile(pattern)


def _audit_report() -> re.Pattern[str]:
    patterns = _check("nothing-was-published")["forbidden_patterns"]
    (pattern,) = [p for p in patterns if "audit_report" in p]
    return re.compile(pattern)


def test_collector_runs_match():
    pattern = _collector()
    for command in (
        # SOUL.md's chat route, with and without fleet_scope's collector_args.
        f"python3 {SCRIPT} > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
        f"python3 {SCRIPT} --scope-projects fuxiaogao-gkedemos > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
        # The SOP's form 1 (fleet_wide_cost_analysis_sop.md section 2).
        'python3 ./skills/fleet-audit/scripts/fleet_waste.py --workspace "/opt/data/gitops/fleet-wide-cost-analysis/gke-agentic__infra" --scope-projects p > /opt/data/scratch/manifest_fleet-wide-cost-analysis.json',
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SCRIPT}",
        f"env A=b python3 {SCRIPT} > /tmp/m.json",
        f"timeout -k 10 600 python3 {SCRIPT} > /tmp/m.json",
        f"timeout 600 python3 {SCRIPT}",
        f"/usr/bin/python3 {SCRIPT}",
        f"/usr/bin/env python3 {SCRIPT}",
        f"python3.11 {SCRIPT}",
        f"python3 -u {SCRIPT} > /tmp/m.json",
        "./skills/fleet-audit/scripts/fleet_waste.py --scope-projects p",
        f'python3 "{SCRIPT}" > /tmp/m.json',
        f'bash -c "python3 {SCRIPT} > /tmp/m.json"',
        f"mkdir -p /opt/data/scratch && python3 {SCRIPT} > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
        f"cd /opt/data/profiles/platform && python3 {SCRIPT}; echo done",
        f"python3 {SCRIPT}",
    ):
        assert pattern.search(command), command


def test_non_runs_do_not_match():
    pattern = _collector()
    for command in (
        f"python3 {SCRIPT} -h",
        f"python3 {SCRIPT} --help",
        f"python3 {SCRIPT} --scope-projects p --help",
        f'echo "python3 {SCRIPT}"',
        'grep -n "fleet_waste.py" /opt/data/profiles/platform/SOUL.md',
        "grep -n 'python3 .*fleet_waste.py' /opt/data/profiles/platform/governance/fleet_wide_cost_analysis_sop.md",
        "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/test_fleet_waste.py",
        f"python3 -c \"print(len(open('{SCRIPT}', 'rb').read()))\"",
        f"python3 -c \"print(open('{SCRIPT}').read())\"",
        f"cat {SCRIPT}",
        f"sed -n 1,80p {SCRIPT}",
        f"python3 {SCRIPT}.bak",
        "python3 /opt/x/fleet_waste.pyc",
        "kubectl top nodes --context seeded-a",
        # The red run on main (the gke-cost-analysis route) issued these.
        "python3 /opt/data/kanban/workspaces/t_20d420d1/parse_clusters.py",
        "python3 -c '\nimport json\n\npath = \"/opt/data/profiles/platform/cache/spillover/call_527758.txt\"\nwith open(path) as f:\n    raw = json.load(f)\n'",
    ):
        assert not pattern.search(command), command


def test_documented_limits_match():
    pattern = _collector()
    for command in (f'echo "a && python3 {SCRIPT}"',):
        assert pattern.search(command), command


def test_ledger_writes_are_flagged():
    pattern = _audit_report()
    for command in (
        # Real commands from other cases' runs.
        f"python3 {REPORT} start --audit stockout-prevention",
        "./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
        "cd $HERMES_KANBAN_WORKSPACE && python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
        f"python3 {REPORT} finish --audit stockout-prevention --findings-file /opt/data/scratch/findings_stockout-prevention.json --manifest-file /opt/data/scratch/manifest_stockout-prevention.json",
        "python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit \\\n  --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json \\\n  --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json",
        f"{REPORT} finish --audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json --manifest-file /opt/data/scratch/manifest_compliance-audit.json",
        # Shapes the shared grammar accepts.
        f"python3 {REPORT} remediate --audit fleet-wide-cost-analysis",
        f'python3 "{REPORT}" start --audit fleet-wide-cost-analysis',
        f"FLEET_AUDIT_SCRATCH_DIR=/tmp timeout -k 10 600 /usr/bin/env python3 {REPORT} finish --audit fleet-wide-cost-analysis",
        f"bash -c 'python3 {REPORT} start --audit fleet-wide-cost-analysis'",
    ):
        assert pattern.search(command), command


def test_non_writes_are_not_flagged():
    pattern = _audit_report()
    for command in (
        # Real commands from other cases' runs.
        f"python3 {REPORT} finish --help",
        f"python3 {REPORT} start compliance-audit --help",
        f"python3 {REPORT} --help",
        f"python3 {REPORT} draft --audit compliance-audit --manifest-file /opt/data/scratch/manifest_compliance-audit.json --out /opt/data/scratch/findings_compliance-audit.json",
        "python3 /opt/data/kanban/workspaces/t_4bde8545/check_audit_report.py",
        # Mentions, not runs.
        'grep -n "audit_report.py start" /opt/data/profiles/platform/skills/fleet-audit/SKILL.md',
        f'echo "python3 {REPORT} finish"',
        f"python3 {REPORT} start -h",
    ):
        assert not pattern.search(command), command
