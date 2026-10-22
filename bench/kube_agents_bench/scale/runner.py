"""Run a measurement plan: apply each level, time each operation, merge the records.

    python -m kube_agents_bench.scale.runner plan bench/scale/phase1.yaml
    python -m kube_agents_bench.scale.runner run bench/scale/phase1.yaml [--only-factor F] [--only-operation O] [--force]
    python -m kube_agents_bench.scale.runner apply bench/scale/phase1.yaml --factor F --level L
    python -m kube_agents_bench.scale.runner teardown bench/scale/phase1.yaml

``run`` skips every run whose key is already in the data file, so an
interrupted run resumes where it stopped; ``--force`` re-runs and replaces.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from kube_agents_bench.scale import collect, fixtures, kube, operations, records
from kube_agents_bench.scale.config import BASE_FACTOR, Plan, RunSpec, load_plan

LOG = logging.getLogger("harness-scale")

TEST_CLUSTER_FORMAT = "{prefix}{index:02d}"
TEST_CLUSTER_LABELS = "purpose=harness-scale"
TEST_CLUSTER_MACHINE_TYPE = "e2-standard-2"
DEFAULT_POOL_NODES = 2
# Cloud Monitoring needs a few minutes before a window's last points are queryable.
MONITORING_DELAY_SECONDS = 240
REGISTRATION_POLL_SECONDS = 60
REGISTERED_JSONPATH = "{.status.usage.clustersRegistered}"
PLATFORM_AGENT_RESOURCE = "platformagents"
RESPONSE_FITS = ["response_seconds", "first_card_seconds"]
LEVEL_LABEL = "harness-scale/level"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_DATA_FILE = "harness-limits.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


class Runner:
    def __init__(self, plan: Plan, data_file: Path) -> None:
        self.plan = plan
        self.data_file = data_file
        target = plan.target
        self.project = target["project_id"]
        self.zone = target["zone"]
        self.prefix = target["cluster_prefix"]
        self.install = collect.Install(
            self.project, target["host_context"], target["namespace"], target["host_cluster"], target["host_location"]
        )
        self.settings = plan.settings
        self.churner: fixtures.Churner | None = None

    # --- the fleet ----------------------------------------------------------------------------

    def cluster_names(self, count: int) -> list[str]:
        return [TEST_CLUSTER_FORMAT.format(prefix=self.prefix, index=index) for index in range(1, count + 1)]

    def contexts(self, count: int) -> list[str]:
        return [kube.context_name(self.project, self.zone, name) for name in self.cluster_names(count)]

    def ensure_clusters(self, count: int) -> None:
        """Create the missing test clusters and delete the extra ones, then wait for registration."""
        wanted = self.cluster_names(count)
        existing = kube.clusters_with_prefix(self.project, self.zone, self.prefix)
        for name in wanted:
            if name not in existing:
                LOG.info("creating test cluster %s", name)
                kube.create_cluster(self.project, self.zone, name, TEST_CLUSTER_MACHINE_TYPE,
                                    DEFAULT_POOL_NODES, TEST_CLUSTER_LABELS)
                kube.run([kube.GCLOUD, "container", "clusters", "get-credentials", name,
                          f"--zone={self.zone}", f"--project={self.project}"])
        for name in existing:
            if name not in wanted:
                LOG.info("deleting test cluster %s", name)
                kube.delete_cluster(self.project, self.zone, name)
        self.wait_registered(count)

    def wait_registered(self, count: int) -> None:
        timeout = float(self.settings.get("cluster_registration_timeout_seconds", 0))
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = kube.kubectl(self.install.context, "get", PLATFORM_AGENT_RESOURCE, "-n",
                                     self.install.namespace, "-o", f"jsonpath={REGISTERED_JSONPATH}")
                registered = int(value.split()[0]) if value.strip() else -1
            except (kube.CommandError, ValueError):
                registered = -1
            if registered == count:
                return
            if time.monotonic() >= deadline:
                LOG.warning("install registers %s clusters, expected %s; continuing", registered, count)
                return
            LOG.info("waiting for registration: %s of %s clusters", registered, count)
            time.sleep(REGISTRATION_POLL_SECONDS)

    def ensure_nodes(self, values: dict[str, float]) -> None:
        pods = fixtures.fixture_pods(values, int(self.settings["churn_pod_lifetime_seconds"]))
        for_pods = DEFAULT_POOL_NODES + math.ceil(pods / int(self.settings["pods_per_node"]))
        nodes = max(int(values["nodes"]), for_pods)
        for name in self.cluster_names(int(values["clusters"])):
            kube.ensure_node_pool(self.project, self.zone, name, self.settings["fixture_pool"],
                                  self.settings["fixture_machine_type"], nodes - DEFAULT_POOL_NODES)

    # --- fixtures -----------------------------------------------------------------------------

    def ensure_fixtures(self, values: dict[str, float]) -> None:
        contexts = self.contexts(int(values["clusters"]))
        objects = int(values["objects"])
        for context in contexts:
            if self._applied_level(context, "objects") not in (None, objects):
                fixtures.delete_fixture(context, "objects")
                self._wait_fixture_gone(context, "objects")
            manifests = fixtures.objects_manifests(objects)
            for manifest in manifests:
                if manifest["kind"] == "Namespace":
                    manifest["metadata"]["labels"][LEVEL_LABEL] = str(objects)
            kube.apply(context, manifests)
        first = contexts[0]
        kube.apply(first, fixtures.listing_manifests(int(values["listing_pods"])))
        kube.apply(first, fixtures.warning_manifests(int(values["warning_events"])))

    def _applied_level(self, context: str, fixture: str) -> int | None:
        out = kube.kubectl(context, "get", "namespaces", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}",
                           "-o", f"jsonpath={{.items[0].metadata.labels.harness-scale/level}}")
        return int(out) if out.strip().isdigit() else None

    def _wait_fixture_gone(self, context: str, fixture: str) -> None:
        kube.kubectl(context, "wait", "--for=delete", "namespace", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}",
                     f"--timeout={kube.APPLY_TIMEOUT_SECONDS}s")

    def apply_level(self, factor: str, level: float) -> dict[str, float]:
        values = self.plan.level_values(factor, level)
        LOG.info("applying %s=%s: %s", factor, level, values)
        self.ensure_clusters(int(values["clusters"]))
        self.ensure_nodes(values)
        self.ensure_fixtures(values)
        self.stop_churn()
        self.churner = fixtures.Churner(self.contexts(int(values["clusters"])), values["churn_per_minute"],
                                        int(self.settings["churn_pod_lifetime_seconds"]))
        self.churner.start()
        settle = int(self.settings.get("settle_seconds", 0))
        LOG.info("settling for %ss", settle)
        time.sleep(settle)
        return values

    def stop_churn(self) -> None:
        if self.churner:
            self.churner.stop()
            self.churner = None

    # --- one run ------------------------------------------------------------------------------

    def execute(self, spec: RunSpec, values: dict[str, float]) -> records.Record:
        op = spec.operation
        contexts = self.contexts(int(values["clusters"]))
        totals = kube.totals(contexts)
        limits = self.install.limits()
        before = self.install.snapshot()
        started_iso = _now_iso()
        LOG.info("run %s %s=%s #%s", op.name, spec.factor, spec.level, spec.run)
        if op.kind == "prompt":
            env = operations.agent_env(self.install.context, self.install.namespace)
            outcome = operations.run_prompts(op, env, int(values["concurrent"]))
        elif op.kind == "soak":
            outcome = operations.soak(op.window_seconds)
        elif op.kind == "restart_soak":
            operations.restart(self.install.context, self.install.namespace)
            outcome = operations.soak(op.window_seconds)
        elif op.kind == "reset_soak":
            operations.reset(self.install.context, self.install.namespace)
            outcome = operations.soak(op.window_seconds)
        else:
            raise ValueError(f"unknown operation kind {op.kind!r}")
        ended_iso = _now_iso()
        after = self.install.snapshot()
        metrics = collect.counter_metrics(before, after)
        metrics += self.install.pvc_metrics()
        logs = self.install.log_counts(started_iso)
        metrics.append(records.Metric("triage_lost", float(logs["triage_lost"])))
        metrics.append(records.Metric("run_slot_refusals", float(logs["run_slot_refusals"] + outcome.run_slot_refusals)))
        if outcome.response_seconds:
            metrics.append(records.Metric("response_seconds", max(outcome.response_seconds)))
        for name, value in outcome.tokens.items():
            metrics = [m for m in metrics if m.name != name] + [records.Metric(name, value)]
        errors = list(outcome.errors) + before.errors + after.errors
        provider_429s = logs["provider_429s"] + outcome.provider_429s
        excluded = RESPONSE_FITS if provider_429s and op.kind == "prompt" else []
        return records.Record(
            commit=self.plan.target["commit"], mode=self.plan.target["mode"], phase=self.plan.phase,
            factor=spec.factor, level=spec.level, operation=op.name, run=spec.run,
            started=started_iso, ended=ended_iso, totals=totals, limits=limits, metrics=metrics,
            provider_429s=provider_429s, excluded_from=list(excluded), errors=errors,
        )

    def run(self, specs: list[RunSpec], force: bool) -> None:
        done = set() if force else records.existing_keys(self.data_file)
        pending = [s for s in specs if records.record_key(self.plan.target["commit"], self.plan.target["mode"],
                                                          s.factor, s.level, s.operation.name, s.run) not in done]
        LOG.info("%s runs pending of %s", len(pending), len(specs))
        awaiting: list[tuple[float, records.Record]] = []
        try:
            for (factor, level), group in itertools.groupby(pending, key=lambda s: (s.factor, s.level)):
                values = self.apply_level(factor, level)
                for spec in group:
                    record = self.execute(spec, values)
                    awaiting.append((time.time(), record))
                    awaiting = self.flush_peaks(awaiting, wait=False)
        finally:
            self.stop_churn()
            self.flush_peaks(awaiting, wait=True)

    def flush_peaks(self, awaiting: list[tuple[float, records.Record]], wait: bool) -> list[tuple[float, records.Record]]:
        """Add Cloud Monitoring peaks to the records whose window is old enough, and merge them.

        Monitoring needs a few minutes before a window's last points are
        queryable, so a record waits here while the next run proceeds.
        """
        left: list[tuple[float, records.Record]] = []
        for finished, record in awaiting:
            remaining = finished + MONITORING_DELAY_SECONDS - time.time()
            if remaining > 0 and not wait:
                left.append((finished, record))
                continue
            if remaining > 0:
                time.sleep(remaining)
            self.add_peaks(record)
            records.merge(self.data_file, [record])
            LOG.info("recorded %s", record.key)
        return left

    def add_peaks(self, record: records.Record) -> None:
        start = datetime.strptime(record.started, ISO_FORMAT).replace(tzinfo=timezone.utc).timestamp()
        end = datetime.strptime(record.ended, ISO_FORMAT).replace(tzinfo=timezone.utc).timestamp()
        try:
            for container, peaks in self.install.container_peaks(start, end).items():
                for name, value in peaks.items():
                    record.metrics.append(records.Metric(name, value, container))
        except OSError as exc:
            record.errors.append(f"monitoring: {exc}")

    def teardown(self) -> None:
        self.stop_churn()
        base = self.plan.level_values(BASE_FACTOR, 0)
        self.ensure_clusters(int(base["clusters"]))
        for context in self.contexts(int(base["clusters"])):
            for fixture in ("objects", "listing", "warning", "churn"):
                fixtures.delete_fixture(context, fixture)
        for name in self.cluster_names(int(base["clusters"])):
            kube.ensure_node_pool(self.project, self.zone, name, self.settings["fixture_pool"],
                                  self.settings["fixture_machine_type"], 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "run", "apply", "teardown"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--only-factor")
    parser.add_argument("--only-operation")
    parser.add_argument("--factor", default=BASE_FACTOR)
    parser.add_argument("--level", type=float, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    plan = load_plan(args.config)
    specs = [
        s for s in plan.matrix()
        if (not args.only_factor or s.factor == args.only_factor)
        and (not args.only_operation or s.operation.name == args.only_operation)
    ]
    if args.command == "plan":
        for spec in specs:
            print(f"{spec.factor:18} {spec.level:>8g} {spec.operation.name:18} run {spec.run}")
        print(f"{len(specs)} runs")
        return 0
    data_file = args.config.resolve().parent / plan.target.get("data_file", DEFAULT_DATA_FILE)
    runner = Runner(plan, data_file)
    if args.command == "run":
        runner.run(specs, args.force)
    elif args.command == "apply":
        runner.apply_level(args.factor, args.level)
        runner.stop_churn()
    else:
        runner.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
