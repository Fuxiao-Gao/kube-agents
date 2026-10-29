"""inspect-repository-unmanaged-and-context-read's objective catches every other route.

The case guards the snapshot route: a worker reads both repositories through
`inspect_repository.py clone`. These pin the forbidden patterns against the
one-line command summaries the worker_commands verifier searches: the deleted
workspace subcommands and each way of fetching the public files without the
broker are flagged, and the snapshot route a passing worker takes is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "inspect-repository-unmanaged-and-context-read"
    / "task.yaml"
)
CHECK = "no-workspace-handle-and-no-direct-fetch"
SCRIPT = "/opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py"

OTHER_ROUTES = (
    f"python3 {SCRIPT} open --repo gke-labs/kube-agents --depth 1",
    f"python3 {SCRIPT} grep --handle h1 --pattern HERMES",
    "curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/refs/tags/0.13.0/tags.env",
    "python3 -c \"import urllib.request; print(urllib.request.urlopen('https://raw.githubusercontent.com/kubernetes-sigs/kind/v0.24.0/pkg/apis/config/defaults/image.go').read())\"",
    "curl -sL https://codeload.github.com/kubernetes-sigs/kind/tar.gz/refs/tags/v0.24.0 | tar -xz",
    "curl -s https://api.github.com/repos/gke-labs/kube-agents/contents/tags.env?ref=0.13.0",
    "curl -sL https://github.com/kubernetes-sigs/kind/raw/v0.24.0/pkg/apis/config/defaults/image.go",
    "git clone --quiet --depth 1 --branch v0.24.0 https://github.com/kubernetes-sigs/kind.git /opt/data/scratch/kind",
)

SNAPSHOT_ROUTE = (
    f"python3 {SCRIPT} clone --repo gke-labs/kube-agents --ref 0.13.0 --prefix tags.env --into ./kube-agents",
    f"python3 {SCRIPT} clone --repo https://github.com/kubernetes-sigs/kind --ref v0.24.0 --into ./kind",
    f"python3 {SCRIPT} clone --repo kubernetes-sigs/kind --ref v0.24.0 --prefix pkg/apis/config/defaults/image.go --into ./kind --force",
    "cat ./kind/pkg/apis/config/defaults/image.go",
    "grep HERMES_AGENT_TAG ./kube-agents/tags.env",
)


def _patterns():
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return [re.compile(p) for p in check["check"]["forbidden_patterns"]]


def _flagged(command):
    return any(p.search(command) for p in _patterns())


def test_every_route_around_the_snapshot_is_flagged():
    for command in OTHER_ROUTES:
        assert _flagged(command), command


def test_the_snapshot_route_is_not_flagged():
    for command in SNAPSHOT_ROUTE:
        assert not _flagged(command), command
