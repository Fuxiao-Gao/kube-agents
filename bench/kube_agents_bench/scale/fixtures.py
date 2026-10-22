"""Fixture objects the runner applies to the test clusters, and the pod churner.

Every fixture object carries ``FIXTURE_LABEL``, and every fixture namespace is
named with ``NAMESPACE_PREFIX``, so a level is applied and removed by label and
re-applying one is idempotent. Pods run the pause image with a token CPU and
memory request: the request keeps the HPAs computing (an HPA over a container
with no CPU request writes a Warning event every sync, which would turn the
object factor into a Warning-event factor).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kube_agents_bench.scale import kube

LOG = logging.getLogger(__name__)

FIXTURE_LABEL = "harness-scale/fixture"
NAMESPACE_PREFIX = "scale-"
OBJECTS_NAMESPACE_FORMAT = "scale-obj-{index:04d}"
LISTING_NAMESPACE = "scale-listing"
WARNING_NAMESPACE = "scale-warning"
CHURN_NAMESPACE = "scale-churn"
PAUSE_IMAGE = "registry.k8s.io/pause:3.10"
# A tag that does not exist, so each Pod fails its pull and writes Warning events.
BROKEN_IMAGE = "registry.k8s.io/pause:harness-scale-missing"
POD_CPU_REQUEST = "1m"
POD_MEMORY_REQUEST = "8Mi"
REPLICAS_PER_DEPLOYMENT = 2
HPA_MAX_REPLICAS = 10
HPA_CPU_UTILIZATION = 80
SERVICE_PORT = 80
CONFIGMAP_DATA_KEY = "settings"
CHURN_BATCH_SECONDS = 10
SECONDS_PER_MINUTE = 60
# The object mix per level (namespaces, Deployments, ConfigMaps); each Deployment
# brings 1 Service, 1 HPA and REPLICAS_PER_DEPLOYMENT Pods. Levels not listed
# scale the 4,000 row.
OBJECT_MIX = {100: (5, 15, 20), 4000: (100, 600, 900), 10000: (250, 1500, 2250)}
REFERENCE_LEVEL = 4000


@dataclass(frozen=True)
class Mix:
    namespaces: int
    deployments: int
    configmaps: int

    @property
    def pods(self) -> int:
        return self.deployments * REPLICAS_PER_DEPLOYMENT

    @property
    def total(self) -> int:
        # namespaces + Deployments + Services + HPAs + Pods + ConfigMaps
        return self.namespaces + self.deployments * 3 + self.pods + self.configmaps


def object_mix(level: int) -> Mix:
    if level in OBJECT_MIX:
        return Mix(*OBJECT_MIX[level])
    namespaces, deployments, configmaps = OBJECT_MIX[REFERENCE_LEVEL]
    scale = level / REFERENCE_LEVEL
    return Mix(
        max(1, round(namespaces * scale)),
        max(1, round(deployments * scale)),
        max(1, round(configmaps * scale)),
    )


def _labels(fixture: str) -> dict[str, str]:
    return {FIXTURE_LABEL: fixture}


def _namespace(name: str, fixture: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": _labels(fixture)}}


def _pod_spec(image: str, command: list[str] | None = None) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "app",
        "image": image,
        "resources": {"requests": {"cpu": POD_CPU_REQUEST, "memory": POD_MEMORY_REQUEST}},
    }
    if command:
        container["command"] = command
    return {"containers": [container], "terminationGracePeriodSeconds": 0}


def _deployment(name: str, namespace: str, fixture: str, replicas: int, image: str = PAUSE_IMAGE,
                command: list[str] | None = None) -> dict[str, Any]:
    selector = {"app": name}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": _labels(fixture)},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            "template": {"metadata": {"labels": {**selector, **_labels(fixture)}}, "spec": _pod_spec(image, command)},
        },
    }


def objects_manifests(level: int) -> list[dict[str, Any]]:
    """The object mix for one objects level, spread evenly across its namespaces."""
    mix = object_mix(level)
    fixture = "objects"
    names = [OBJECTS_NAMESPACE_FORMAT.format(index=index) for index in range(mix.namespaces)]
    manifests = [_namespace(name, fixture) for name in names]
    for index in range(mix.deployments):
        namespace = names[index % len(names)]
        app = f"app-{index:05d}"
        manifests.append(_deployment(app, namespace, fixture, REPLICAS_PER_DEPLOYMENT))
        manifests.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": app, "namespace": namespace, "labels": _labels(fixture)},
            "spec": {"selector": {"app": app}, "ports": [{"port": SERVICE_PORT}]},
        })
        manifests.append({
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": app, "namespace": namespace, "labels": _labels(fixture)},
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": app},
                "minReplicas": REPLICAS_PER_DEPLOYMENT,
                "maxReplicas": HPA_MAX_REPLICAS,
                "metrics": [{
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": HPA_CPU_UTILIZATION}},
                }],
            },
        })
    for index in range(mix.configmaps):
        manifests.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"config-{index:05d}", "namespace": names[index % len(names)], "labels": _labels(fixture)},
            "data": {CONFIGMAP_DATA_KEY: f"value-{index}"},
        })
    return manifests


def listing_manifests(pods: int) -> list[dict[str, Any]]:
    fixture = "listing"
    return [_namespace(LISTING_NAMESPACE, fixture), _deployment("listing", LISTING_NAMESPACE, fixture, pods)]


def warning_manifests(failing_pods: int) -> list[dict[str, Any]]:
    """One workload whose Pods fail their image pull, so each writes Warning events.

    The base point is one such Pod; the burst level is one fault on many Pods
    at once, the fan-out shape the per-Pod dedup key turns into many incidents.
    """
    fixture = "warning"
    return [
        _namespace(WARNING_NAMESPACE, fixture),
        _deployment("broken-image", WARNING_NAMESPACE, fixture, failing_pods, image=BROKEN_IMAGE),
    ]


def fixture_pods(values: dict[str, float], churn_lifetime_seconds: int) -> int:
    """Pods the fixtures place on each test cluster at one point, for node planning.

    The listing fixture lives on the first cluster only; the runner plans every
    cluster for the largest so the clusters stay alike.
    """
    churn_live = math.ceil(values["churn_per_minute"] * churn_lifetime_seconds / SECONDS_PER_MINUTE)
    mix = object_mix(int(values["objects"]))
    return mix.pods + int(values["warning_events"]) + int(values["listing_pods"]) + churn_live


def delete_fixture(context: str, fixture: str) -> None:
    """Delete every namespace of one fixture; their objects go with them."""
    kube.kubectl(context, "delete", "namespace", "-l", f"{FIXTURE_LABEL}={fixture}",
                 "--ignore-not-found", "--wait=false")


class Churner:
    """Creates and deletes Pods at a steady rate until stopped.

    Every ``CHURN_BATCH_SECONDS`` it creates one batch of bare pause Pods and
    deletes the batches older than ``lifetime_seconds``, so the cluster sees
    ``per_minute`` creations and as many deletions per minute, with about
    ``per_minute * lifetime_seconds / 60`` Pods alive at once.
    """

    def __init__(self, contexts: list[str], per_minute: float, lifetime_seconds: int) -> None:
        self.contexts = contexts
        self.per_minute = per_minute
        self.lifetime_seconds = lifetime_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.created = 0
        self.errors: list[str] = []

    def _batch_size(self) -> int:
        per_batch = self.per_minute * CHURN_BATCH_SECONDS / SECONDS_PER_MINUTE / max(1, len(self.contexts))
        return max(1, round(per_batch))

    def start(self) -> None:
        for context in self.contexts:
            kube.apply(context, [_namespace(CHURN_NAMESPACE, "churn")])
        self._thread = threading.Thread(target=self._loop, name="churner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _loop(self) -> None:
        live: list[tuple[float, str]] = []
        while not self._stop.is_set():
            started = time.monotonic()
            batch = uuid.uuid4().hex[:8]
            pods = [
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": f"churn-{batch}-{index:04d}",
                        "namespace": CHURN_NAMESPACE,
                        "labels": {**_labels("churn"), "harness-scale/batch": batch},
                    },
                    "spec": _pod_spec(PAUSE_IMAGE),
                }
                for index in range(self._batch_size())
            ]
            for context in self.contexts:
                try:
                    kube.apply(context, pods)
                    self.created += len(pods)
                except kube.CommandError as exc:
                    self.errors.append(str(exc))
            live.append((started, batch))
            while live and started - live[0][0] >= self.lifetime_seconds:
                _, old = live.pop(0)
                for context in self.contexts:
                    try:
                        kube.kubectl(context, "delete", "pods", "-n", CHURN_NAMESPACE, "-l",
                                     f"harness-scale/batch={old}", "--wait=false")
                    except kube.CommandError as exc:
                        self.errors.append(str(exc))
            self._stop.wait(max(0.0, CHURN_BATCH_SECONDS - (time.monotonic() - started)))
