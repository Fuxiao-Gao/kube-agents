"""Thin wrappers over kubectl and gcloud for the measurement runner.

Every call names its context or project explicitly, so the runner never acts on
whatever the caller's current context happens to be.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

KUBECTL = "kubectl"
GCLOUD = "gcloud"
DEFAULT_TIMEOUT_SECONDS = 300
APPLY_TIMEOUT_SECONDS = 900
RESIZE_TIMEOUT_SECONDS = 1800
CREATE_CLUSTER_TIMEOUT_SECONDS = 1800
APPLY_CHUNK = 500
FIELD_MANAGER = "harness-scale"
CONTEXT_FORMAT = "gke_{project}_{location}_{cluster}"
# Kinds the runner counts in every test cluster; the record's totals use these names.
COUNTED_KINDS = {
    "namespaces": "namespaces",
    "nodes": "nodes",
    "pods": "pods",
    "deployments": "deployments.apps",
    "services": "services",
    "hpas": "horizontalpodautoscalers.autoscaling",
    "configmaps": "configmaps",
    "events": "events",
    "pvcs": "persistentvolumeclaims",
}


class CommandError(RuntimeError):
    pass


def run(args: list[str], *, stdin: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    LOG.debug("run: %s", " ".join(args))
    proc = subprocess.run(
        args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise CommandError(f"{' '.join(args[:6])}... exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def context_name(project: str, location: str, cluster: str) -> str:
    return CONTEXT_FORMAT.format(project=project, location=location, cluster=cluster)


def kubectl(context: str, *args: str, stdin: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    return run([KUBECTL, "--context", context, *args], stdin=stdin, timeout=timeout)


def kubectl_json(context: str, *args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    return json.loads(kubectl(context, *args, "-o", "json", timeout=timeout))


def apply(context: str, manifests: Iterable[dict[str, Any]]) -> int:
    """Server-side apply in chunks; returns how many objects were applied."""
    batch: list[dict[str, Any]] = []
    applied = 0
    for manifest in manifests:
        batch.append(manifest)
        if len(batch) >= APPLY_CHUNK:
            applied += _apply_batch(context, batch)
            batch = []
    if batch:
        applied += _apply_batch(context, batch)
    return applied


def _apply_batch(context: str, batch: list[dict[str, Any]]) -> int:
    body = yaml.safe_dump_all(batch, sort_keys=False)
    kubectl(
        context,
        "apply",
        "--server-side",
        "--force-conflicts",
        f"--field-manager={FIELD_MANAGER}",
        "-f",
        "-",
        stdin=body,
        timeout=APPLY_TIMEOUT_SECONDS,
    )
    return len(batch)


def count(context: str, resource: str) -> int:
    out = kubectl(context, "get", resource, "-A", "--no-headers", "-o", "name", timeout=APPLY_TIMEOUT_SECONDS)
    return sum(1 for line in out.splitlines() if line.strip())


def totals(contexts: list[str]) -> dict[str, int]:
    """Object counts by kind, summed over the test clusters, plus the cluster count."""
    result = {name: 0 for name in COUNTED_KINDS}
    for context in contexts:
        for name, resource in COUNTED_KINDS.items():
            result[name] += count(context, resource)
    result["clusters"] = len(contexts)
    return result


def gcloud(*args: str, project: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    return run([GCLOUD, *args, f"--project={project}", "--quiet"], timeout=timeout)


def clusters_with_prefix(project: str, zone: str, prefix: str) -> list[str]:
    out = gcloud(
        "container", "clusters", "list", f"--zone={zone}",
        f"--filter=name~^{prefix}[0-9]+$", "--format=value(name)", project=project,
    )
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def node_pool_size(project: str, zone: str, cluster: str, pool: str) -> int | None:
    """The pool's live node count, or ``None`` when the pool does not exist."""
    try:
        gcloud(
            "container", "node-pools", "describe", pool, f"--cluster={cluster}", f"--zone={zone}",
            "--format=value(name)", project=project,
        )
    except CommandError:
        return None
    nodes = kubectl(
        context_name(project, zone, cluster), "get", "nodes", "-l",
        f"cloud.google.com/gke-nodepool={pool}", "--no-headers", "-o", "name",
    )
    return sum(1 for line in nodes.splitlines() if line.strip())


def ensure_node_pool(project: str, zone: str, cluster: str, pool: str, machine_type: str, size: int) -> None:
    """Create the fixture pool at ``size`` nodes, or resize it when it exists."""
    current = node_pool_size(project, zone, cluster, pool)
    if current is None:
        gcloud(
            "container", "node-pools", "create", pool, f"--cluster={cluster}", f"--zone={zone}",
            f"--machine-type={machine_type}", f"--num-nodes={size}", "--disk-size=20",
            project=project, timeout=RESIZE_TIMEOUT_SECONDS,
        )
        return
    if current != size:
        gcloud(
            "container", "clusters", "resize", cluster, f"--node-pool={pool}", f"--zone={zone}",
            f"--num-nodes={size}", project=project, timeout=RESIZE_TIMEOUT_SECONDS,
        )


def create_cluster(project: str, zone: str, cluster: str, machine_type: str, nodes: int, labels: str) -> None:
    gcloud(
        "container", "clusters", "create", cluster, f"--zone={zone}", f"--num-nodes={nodes}",
        f"--machine-type={machine_type}", "--release-channel=regular", f"--labels={labels}",
        f"--workload-pool={project}.svc.id.goog",
        project=project, timeout=CREATE_CLUSTER_TIMEOUT_SECONDS,
    )


def delete_cluster(project: str, zone: str, cluster: str) -> None:
    gcloud("container", "clusters", "delete", cluster, f"--zone={zone}", project=project,
           timeout=CREATE_CLUSTER_TIMEOUT_SECONDS)
