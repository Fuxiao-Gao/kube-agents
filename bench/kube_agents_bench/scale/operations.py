"""The agent operations a run times: prompts through the agent API, soaks, a restart, a reset.

Prompts go through :class:`kube_agents_bench.harness.KubeAgentsHarness`, the
transport the evals use, so a prompt here is sent, delegated and awaited the
same way an eval's is. Concurrent prompts run in separate processes, because
the harness keeps its port-forward state per process.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.scale import kube
from kube_agents_bench.scale.config import Operation

LOG = logging.getLogger(__name__)

AGENT_SECRET = "platform-agent-secrets"
AGENT_SECRET_KEY = "API_SERVER_KEY"
GATEWAY_FRAGMENT = "gateway"
RESET_TIMEOUT_SECONDS = 1800
READY_POLL_SECONDS = 10
TOKEN_BUCKETS = {"tokens_input": "input", "tokens_output": "output", "tokens_cached": "cache_read"}
RATE_LIMIT_MARKERS = ("429", "RESOURCE_EXHAUSTED", "rate limit", "Too many concurrent runs")


@dataclass
class OperationResult:
    response_seconds: list[float] = field(default_factory=list)
    tokens: dict[str, float] = field(default_factory=dict)
    provider_429s: int = 0
    run_slot_refusals: int = 0
    errors: list[str] = field(default_factory=list)


def agent_env(context: str, namespace: str) -> dict[str, str]:
    """The environment the harness reads, for the experiment install's agent API."""
    encoded = kube.kubectl(context, "get", "secret", AGENT_SECRET, "-n", namespace,
                           "-o", f"jsonpath={{.data.{AGENT_SECRET_KEY}}}")
    return {
        "AGENT_CLUSTER_CONTEXT": context,
        "AGENT_NAMESPACE": namespace,
        "PLATFORM_AGENT_TOKEN": base64.b64decode(encoded).decode(),
    }


def _prompt_worker(prompt: str, env: dict[str, str]) -> dict[str, Any]:
    """Run one prompt in this process and return the parts a record needs."""
    os.environ.update(env)
    from kube_agents_bench.harness import KubeAgentsHarness  # imported here: the harness reads env at import

    started = time.monotonic()
    result = KubeAgentsHarness().run(prompt)
    return {
        "seconds": time.monotonic() - started,
        "tokens": dict(result.tokens or {}),
        "errors": list(result.errors or []),
        "output_chars": len(result.output or ""),
    }


def run_prompts(operation: Operation, env: dict[str, str], concurrent: int) -> OperationResult:
    """The operation's prompt, plus ``concurrent - 1`` follow-up questions sent at the same moment."""
    prompts = [operation.prompt] + [operation.follow_up or operation.prompt] * max(0, concurrent - 1)
    outcome = OperationResult()
    with ProcessPoolExecutor(max_workers=len(prompts)) as pool:
        futures = [pool.submit(_prompt_worker, prompt, env) for prompt in prompts]
        for future in futures:
            try:
                answer = future.result(timeout=operation.timeout_seconds or None)
            except Exception as exc:  # a prompt that dies is recorded, and the run goes on
                outcome.errors.append(f"prompt failed: {exc}")
                continue
            outcome.response_seconds.append(answer["seconds"])
            for metric, bucket in TOKEN_BUCKETS.items():
                value = answer["tokens"].get(bucket)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    outcome.tokens[metric] = outcome.tokens.get(metric, 0.0) + value
            for error in answer["errors"]:
                outcome.errors.append(error)
                if "Too many concurrent runs" in error:
                    outcome.run_slot_refusals += 1
                elif any(marker in error for marker in RATE_LIMIT_MARKERS):
                    outcome.provider_429s += 1
    return outcome


def soak(window_seconds: int) -> OperationResult:
    time.sleep(window_seconds)
    return OperationResult()


def _gateway_pod(context: str, namespace: str) -> str | None:
    out = kube.kubectl(context, "get", "pods", "-n", namespace, "--no-headers", "-o", "name")
    for line in out.splitlines():
        if GATEWAY_FRAGMENT in line:
            return line.strip()
    return None


def wait_gateway_ready(context: str, namespace: str, timeout: float = RESET_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = _gateway_pod(context, namespace)
        if pod:
            try:
                kube.kubectl(context, "wait", "-n", namespace, "--for=condition=Ready", pod, "--timeout=60s")
                return
            except kube.CommandError:
                pass
        time.sleep(READY_POLL_SECONDS)
    raise kube.CommandError("gateway pod did not become Ready")


def restart(context: str, namespace: str) -> None:
    """Delete the gateway pod; the new one replays the fleet's standing Warning events."""
    pod = _gateway_pod(context, namespace)
    if pod:
        kube.kubectl(context, "delete", "-n", namespace, pod, "--wait=true", timeout=RESET_TIMEOUT_SECONDS)
    wait_gateway_ready(context, namespace)


def reset(context: str, namespace: str) -> None:
    """Give the agent a fresh data volume, as a new install has, then wait for it.

    The PVC is deleted while mounted, so it waits in Terminating; deleting the
    pod releases it, and the operator renders a new claim and pod. The onboarding
    jobs then run as on a first install, without re-running Terraform.
    """
    claims = kube.kubectl(context, "get", "pvc", "-n", namespace, "--no-headers", "-o", "name")
    for claim in claims.splitlines():
        if claim.strip():
            kube.kubectl(context, "delete", "-n", namespace, claim.strip(), "--wait=false")
    restart(context, namespace)
