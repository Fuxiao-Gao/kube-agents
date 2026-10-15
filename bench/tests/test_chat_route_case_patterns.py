"""The chat-route cases' collector and ledger patterns read a command, not a mention of one.

Seven cases grade a plain fleet-wide chat question that runs an audit
stream's collector and opens no ledger run (#2386): reliability, stockout,
AI security, upgrade and drift, plus the GCE and networking regression
guards. Each grades the collector run with one ``required_patterns`` entry and
forbids ``audit_report.py start``/``finish`` twice (the no-ledger objective and
the nothing-was-published safeguard), all built on one command-position
grammar. The worker_commands verifier runs ``re.search`` on each command, so
these load each pattern from its task file and pin it against the commands the
route and the SOPs dictate, real commands from dev runs, and the mentions,
help calls and look-alike scripts that must not count.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

TASKS = Path(__file__).resolve().parent.parent / "tasks"
SKILLS = "/opt/data/profiles/platform/skills"
SCOPE = "--scope-projects fuxiaogao-gkedemos"

# case id -> (collector check, script path under skills/, subcommand or None)
CASES = {
    "reliability-chat-request-runs-collector": (
        "the-reliability-collector-ran",
        "fleet-audit/scripts/collect.py",
        "obtainability-audit",
    ),
    "stockout-chat-request-runs-collector": (
        "the-stockout-collector-ran",
        "fleet-audit/scripts/fleet_stockout.py",
        None,
    ),
    "ai-security-chat-request-runs-collector": (
        "the-ai-security-collector-ran",
        "fleet-audit/scripts/collect.py",
        "ai-security-audit",
    ),
    "upgrade-chat-request-runs-collector": (
        "the-upgrade-collector-ran",
        "fleet-audit/scripts/patch_readiness.py",
        None,
    ),
    "drift-chat-request-runs-collector": (
        "the-drift-collector-ran",
        "fleet-audit/scripts/fleet_drift.py",
        None,
    ),
    "gce-chat-request-runs-collector": (
        "the-compute-collector-ran",
        "gce-compute-fleet-audit/scripts/compute_fleet_audit.py",
        None,
    ),
    "networking-chat-request-runs-collector": (
        "the-networking-collector-ran",
        "gcp-networking-fabric-audit/scripts/networking_audit.py",
        None,
    ),
}

# Commands the platform worker ran in dev runs of each case or its SOP-named
# control (results.json trajectories, terminal calls by agent "platform").
REAL_RUNS = {
    "reliability-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_obtainability-audit.json && ls -lh /opt/data/scratch/chat_manifest_obtainability-audit.json",
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit {SCOPE} > /opt/data/scratch/chat_manifest_obtainability-audit.json && head -n 30 /opt/data/scratch/chat_manifest_obtainability-audit.json",
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit --workspace /opt/data/gitops/obtainability-audit/gke-agentic__fuxiao-gkedemo-infra {SCOPE}",
    ],
    "stockout-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} > /opt/data/scratch/chat_manifest_stockout-prevention.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_stockout-prevention.json && cat /opt/data/scratch/chat_manifest_stockout-prevention.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} > /opt/data/scratch/manifest_stockout-prevention.json",
    ],
    "ai-security-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit {SCOPE} > /opt/data/scratch/chat_manifest_ai-security-audit.json",
        f"mkdir -p /opt/data/scratch && python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit {SCOPE} > /opt/data/scratch/chat_manifest_ai-security-audit.json && ls -la /opt/data/scratch/chat_manifest_ai-security-audit.json",
        f'python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit --workspace "/opt/data/gitops/ai-security-audit/gke-agentic__fuxiao-gkedemo-infra" {SCOPE} > /opt/data/scratch/manifest_ai-security-audit.json',
    ],
    "upgrade-chat-request-runs-collector": [
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"mkdir -p /opt/data/scratch/chat\nFLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE}",
    ],
    "drift-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && head -n 40 /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && cat /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && ls -la /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
    ],
    "gce-chat-request-runs-collector": [
        f"python3 {SKILLS}/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE}",
        f"mkdir -p /opt/data/scratch && python3 {SKILLS}/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE} > /opt/data/scratch/chat_manifest_gce-compute-fleet-audit.json && cat /opt/data/scratch/chat_manifest_gce-compute-fleet-audit.json",
        f"python3 ./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE} > /opt/data/scratch/manifest_gce-compute-fleet-audit.json",
    ],
    "networking-chat-request-runs-collector": [
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --output /opt/data/scratch/chat_manifest_gcp-networking-fabric-audit.json",
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --scope-unread declared-scope=unresolved --output /opt/data/scratch/chat_manifest_gcp-networking-fabric-audit.json",
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --output /tmp/manifest_networking.json && cat /tmp/manifest_networking.json",
    ],
}

# Ledger commands workers ran in the SOP-named controls: each opens or
# closes a ledger run, so a chat answer must not contain one.
REAL_LEDGER_RUNS = [
    "python3 ./skills/fleet-audit/scripts/audit_report.py start --audit ai-security-audit",
    f"cd $HERMES_KANBAN_WORKSPACE && python3 {SKILLS}/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
    "./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
    f"{SKILLS}/fleet-audit/scripts/audit_report.py finish --audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json --manifest-file /opt/data/scratch/manifest_compliance-audit.json",
    "python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit \\\n  --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json \\\n  --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py start obtainability-audit",
]

# Commands that mention audit_report.py without opening a ledger run.
NOT_LEDGER_RUNS = [
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py --help",
    "python3 ./skills/fleet-audit/scripts/audit_report.py finish --help",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py start -h",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py draft --audit ai-security-audit --manifest-file /opt/data/scratch/manifest_ai-security-audit.json --out /opt/data/scratch/findings_ai-security-audit.json",
    "python3 /opt/data/kanban/workspaces/t_4bde8545/check_audit_report.py",
    'grep -n "audit_report.py start" /opt/data/profiles/platform/skills/fleet-audit/SKILL.md',
    'echo "do not run audit_report.py start or finish"',
    "cat /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py",
]


def _check(case: str, name: str) -> dict:
    doc = yaml.safe_load((TASKS / case / "task.yaml").read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _required(case: str, name: str) -> re.Pattern[str]:
    (pattern,) = _check(case, name)["required_patterns"]
    return re.compile(pattern)


def _ledger_patterns(case: str) -> list[re.Pattern[str]]:
    (objective,) = _check(case, "no-ledger-run-was-opened")["forbidden_patterns"]
    published = [
        p
        for p in _check(case, "nothing-was-published")["forbidden_patterns"]
        if "audit_report" in p
    ]
    assert len(published) == 1, published
    return [re.compile(objective), re.compile(published[0])]


def _run_forms(script: str, sub: str | None) -> list[str]:
    """Every way the grammar must accept a run of the script."""
    run = f"{script} {sub}" if sub else script
    name = script.rsplit("/", 1)[-1]
    stream = sub or name.removesuffix(".py")
    return [
        # SOUL.md's chat command, with and without fleet_scope's args.
        f"python3 {SKILLS}/{run} {SCOPE} > /opt/data/scratch/chat_manifest_{stream}.json",
        f"python3 {SKILLS}/{run} > /opt/data/scratch/chat_manifest_{stream}.json",
        # The SOPs' form-1 command, relative to the profile.
        f'python3 ./skills/{run} --workspace "/opt/data/gitops/x" {SCOPE} > /opt/data/scratch/manifest_{stream}.json',
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SKILLS}/{run} {SCOPE}",
        f"env A=b python3 {SKILLS}/{run}",
        f"timeout -k 10 600 python3 {SKILLS}/{run} {SCOPE}",
        f"/usr/bin/python3 {SKILLS}/{run}",
        f"/usr/bin/env python3 {SKILLS}/{run}",
        f"python3.11 {SKILLS}/{run}",
        f"./skills/{run} {SCOPE}",
        f'python3 "{SKILLS}/{script}"' + (f" {sub}" if sub else ""),
        f'bash -c "python3 {SKILLS}/{run}"',
        f"mkdir -p /opt/data/scratch && python3 {SKILLS}/{run} > /opt/data/scratch/chat_manifest_{stream}.json",
        f"cd {SKILLS}/{script.rsplit('/', 1)[0]} && ./{name}" + (f" {sub}" if sub else ""),
    ]


def _non_runs(script: str, sub: str | None) -> list[str]:
    """Mentions, help calls and look-alikes the grammar must reject."""
    run = f"{script} {sub}" if sub else script
    name = script.rsplit("/", 1)[-1]
    folder = script.rsplit("/", 1)[0]
    forms = [
        f"python3 {SKILLS}/{run} -h",
        f"python3 {SKILLS}/{run} --help",
        f"python3 {SKILLS}/{run} {SCOPE} --help",
        f'echo "python3 {SKILLS}/{run}"',
        f'grep -n "{name}" {SKILLS}/{folder.split("/")[0]}/SKILL.md',
        f"python3 {SKILLS}/{folder}/test_{name}" + (f" {sub}" if sub else ""),
        f"python3 -c \"print(open('{SKILLS}/{script}').read())\"" + (f" {sub}" if sub else ""),
        f"python3 -c \"import runpy; runpy.run_path('{SKILLS}/{script}')\"",
        f"cat {SKILLS}/{script}",
        f"sed -n 1,40p {SKILLS}/{script}",
    ]
    if sub:
        forms += [
            "python3 /opt/data/repo/scripts/eval_dashboard/collect.py",
            f"python3 {SKILLS}/{script} compliance-audit {SCOPE}",
        ]
    return forms


@pytest.mark.parametrize("case", CASES)
def test_collector_check_matches_every_run_form(case):
    check, script, sub = CASES[case]
    pattern = _required(case, check)
    for command in _run_forms(script, sub) + REAL_RUNS[case]:
        assert pattern.search(command), command


@pytest.mark.parametrize("case", CASES)
def test_collector_check_rejects_mentions_and_help(case):
    check, script, sub = CASES[case]
    pattern = _required(case, check)
    for command in _non_runs(script, sub):
        assert not pattern.search(command), command


@pytest.mark.parametrize("case", CASES)
def test_quoted_separator_still_matches_as_documented_limit(case):
    # A regex cannot tell a quoted ; & | from a real one, so this mention
    # counts as a run. The case comment states it; this pins it.
    check, script, sub = CASES[case]
    run = f"{script} {sub}" if sub else script
    command = f'echo "rm -f out && python3 {SKILLS}/{run} {SCOPE}"'
    assert _required(case, check).search(command)


def test_networking_check_rejects_the_psc_only_run():
    case = "networking-chat-request-runs-collector"
    check, script, _ = CASES[case]
    pattern = _required(case, check)
    for command in (
        f"python3 {SKILLS}/{script} {SCOPE} --check psc-routing-deadlock",
        f"python3 {SKILLS}/{script} --check=psc-routing-deadlock --output /tmp/m.json",
    ):
        assert not pattern.search(command), command


@pytest.mark.parametrize("case", CASES)
def test_ledger_patterns_flag_a_ledger_run(case):
    for pattern in _ledger_patterns(case):
        for command in REAL_LEDGER_RUNS + [
            f'bash -c "python3 {SKILLS}/fleet-audit/scripts/audit_report.py finish --audit x"',
            f"timeout 60 /usr/bin/python3 '{SKILLS}/fleet-audit/scripts/audit_report.py' start --audit x",
        ]:
            assert pattern.search(command), (pattern.pattern, command)


@pytest.mark.parametrize("case", CASES)
def test_ledger_patterns_ignore_mentions_help_and_draft(case):
    for pattern in _ledger_patterns(case):
        for command in NOT_LEDGER_RUNS + REAL_RUNS[case]:
            assert not pattern.search(command), (pattern.pattern, command)


@pytest.mark.parametrize("case", CASES)
def test_published_pattern_keeps_remediate(case):
    _, published = _ledger_patterns(case)
    command = f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py remediate --audit x"
    assert published.search(command)


UPGRADE = "upgrade-chat-request-runs-collector"
SCRATCH_CHECK = "the-collector-used-the-chat-scratch-dir"
PATCH = f"{SKILLS}/fleet-audit/scripts/patch_readiness.py"


def test_upgrade_scratch_check_requires_the_assignment_before_the_run():
    pattern = _required(UPGRADE, SCRATCH_CHECK)
    for command in REAL_RUNS[UPGRADE][:2] + [
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH}",
        f"env FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH} {SCOPE}",
        f"A=1 FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat B=2 /usr/bin/python3 {PATCH}",
        f"mkdir -p /opt/data/scratch/chat && FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f'bash -c "FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH}"',
    ]:
        assert pattern.search(command), command


def test_upgrade_scratch_check_rejects_a_run_without_it():
    pattern = _required(UPGRADE, SCRATCH_CHECK)
    for command in (
        REAL_RUNS[UPGRADE][2],
        f"python3 {PATCH} {SCOPE} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"export FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat && python3 {PATCH}",
        f"FLEET_AUDIT_SCRATCH=/opt/data/scratch/chat python3 {PATCH}",
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH} --help",
        f'echo FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH}',
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SKILLS}/fleet-audit/scripts/fleet_upgrade_report.py",
    ):
        assert not pattern.search(command), command
