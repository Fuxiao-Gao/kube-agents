"""Run a measurement plan: apply each level, time each operation, merge the records.

    python -m kube_agents_bench.scale.runner plan <plan.yaml> [--only-factor F] [--only-operation O] [--limit N]
    python -m kube_agents_bench.scale.runner run <plan.yaml> [--only-factor F] [--only-operation O] [--limit N] [--force]
    python -m kube_agents_bench.scale.runner apply <plan.yaml> --factor F --level L
    python -m kube_agents_bench.scale.runner teardown <plan.yaml> [--delete-fleet]

``run`` skips every run whose key is already in the data file, so an
interrupted run resumes where it stopped; ``--force`` re-runs and replaces.
The target (project, host cluster, test-cluster prefix and label) comes from
the plan's ``target`` block and ``SCALE_*`` environment variables.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from kube_agents_bench.scale import collect, fixtures, kube, operations, records
from kube_agents_bench.scale.config import BASE_FACTOR, Plan, RunSpec, load_plan

LOG = logging.getLogger("harness-scale")

TEST_CLUSTER_FORMAT = "{prefix}{index:02d}"
TEST_CLUSTER_MACHINE_TYPE = "e2-standard-2"
DEFAULT_POOL_NODES = 2
# Cloud Monitoring needs a few minutes before a window's last points are queryable.
MONITORING_DELAY_SECONDS = 240
REGISTRATION_POLL_SECONDS = 60
# Time for a restarted watcher to list and sync every cluster before it is asked what it watches.
WATCHER_START_SECONDS = 180
RESPONSE_FITS = ["response_seconds"]
LEVEL_LABEL = "harness-scale/level"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_DATA_FILE = "harness-limits.json"
SECONDS_PER_MINUTE = 60
FIXTURES = ("objects", "listing", "warning", "churn")
# The tag of an image pinned by commit is the commit; anything else is recorded as the image only.
COMMIT_LENGTH = 40
HEX_DIGITS = set("0123456789abcdef")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, ISO_FORMAT).replace(tzinfo=timezone.utc).timestamp()


def commit_of(image: str) -> str:
    """The commit an image is pinned to, when its tag is a full commit SHA; else ''."""
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    return tag if len(tag) == COMMIT_LENGTH and set(tag) <= HEX_DIGITS else ""


class Runner:
    def __init__(self, plan: Plan, data_file: Path) -> None:
        self.plan = plan
        self.data_file = data_file
        target = plan.target
        self.project = target["project_id"]
        self.zone = target["zone"]
        self.prefix = target["cluster_prefix"]
        self.label = target["cluster_label"]
        self.install = collect.Install(
            self.project, target["host_context"], target["namespace"], target["host_cluster"], target["host_location"]
        )
        self.settings = plan.settings
        self.churner: fixtures.Churner | None = None
        # Problems that make a level differ from its plan; every record of the level carries them.
        self.level_errors: list[str] = []

    # --- the fleet ----------------------------------------------------------------------------

    def cluster_names(self, count: int) -> list[str]:
        return [TEST_CLUSTER_FORMAT.format(prefix=self.prefix, index=index) for index in range(1, count + 1)]

    def contexts(self, count: int) -> list[str]:
        return [kube.context_name(self.project, self.zone, name) for name in self.cluster_names(count)]

    def ensure_clusters(self, count: int) -> None:
        """Create the missing test clusters and delete the extra ones, then make the watcher watch them."""
        wanted = self.cluster_names(count)
        existing = kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label)
        missing = [name for name in wanted if name not in existing]
        extra = [name for name in existing if name not in wanted]
        if missing or extra:
            with ThreadPoolExecutor(max_workers=len(missing) + len(extra)) as pool:
                jobs = [pool.submit(self._create_cluster, name) for name in missing]
                jobs += [pool.submit(kube.delete_cluster, self.project, self.zone, name) for name in extra]
                for job in jobs:
                    job.result()
        # One at a time: gcloud writes the kubeconfig without a lock.
        for name in wanted:
            if not kube.has_context(kube.context_name(self.project, self.zone, name)):
                kube.run([kube.GCLOUD, "container", "clusters", "get-credentials", name,
                          f"--zone={self.zone}", f"--project={self.project}"])
        self.wait_registered(count)

    def _create_cluster(self, name: str) -> None:
        LOG.info("creating test cluster %s", name)
        kube.create_cluster(self.project, self.zone, name, TEST_CLUSTER_MACHINE_TYPE, DEFAULT_POOL_NODES, self.label)

    def wait_registered(self, count: int) -> None:
        """Wait for a Cluster Agent profile per test cluster, then make the event watcher watch exactly them.

        The watcher builds its watch set once, at start, from the profiles that
        exist then: a profile the reconcile scaffolds later is not watched, and
        a deleted cluster stays watched, until the watcher restarts. So once the
        profiles match, a watch set that differs from the test fleet is fixed by
        restarting the gateway pod, which is what an operator would have to do.
        Whatever is still wrong after that is recorded on every run of the level.
        """
        names = set(self.cluster_names(count))
        timeout = float(self.settings.get("cluster_registration_timeout_seconds", 0))
        deadline = time.monotonic() + timeout
        profiles = self.install.profile_clusters()
        while profiles & names != names:
            if time.monotonic() >= deadline:
                self.level_errors.append(f"no Cluster Agent profile for {sorted(names - profiles)} after {timeout:.0f}s")
                break
            LOG.info("waiting for Cluster Agent profiles: %s of %s", len(profiles & names), count)
            time.sleep(REGISTRATION_POLL_SECONDS)
            profiles = self.install.profile_clusters()
        test_watched = {name for name in self.install.watched_clusters() if name.startswith(self.prefix)}
        if test_watched != names:
            LOG.warning("event watcher watches %s, test fleet is %s; restarting the gateway so it re-reads profiles",
                        sorted(test_watched), sorted(names))
            operations.restart(self.install.context, self.install.namespace)
            time.sleep(WATCHER_START_SECONDS)
            test_watched = {name for name in self.install.watched_clusters() if name.startswith(self.prefix)}
            if test_watched != names:
                self.level_errors.append(f"event watcher watches {sorted(test_watched)}, test fleet is {sorted(names)}")

    def ensure_nodes(self, values: dict[str, float]) -> None:
        """Size each test cluster's fixture pool for the level's node count and the Pods it must hold."""
        names = self.cluster_names(int(values["clusters"]))
        lifetime = int(self.settings["churn_pod_lifetime_seconds"])
        per_node = int(self.settings["pods_per_node"])
        sizes = {}
        for index, name in enumerate(names):
            pods = fixtures.fixture_pods(values, lifetime, first=index == 0)
            sizes[name] = max(int(values["nodes"]), DEFAULT_POOL_NODES + math.ceil(pods / per_node)) - DEFAULT_POOL_NODES
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            jobs = [
                pool.submit(kube.ensure_node_pool, self.project, self.zone, name, self.settings["fixture_pool"],
                            self.settings["fixture_machine_type"], size)
                for name, size in sizes.items()
            ]
            for job in jobs:
                job.result()

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
        kube.apply(first, fixtures.warning_manifests(int(values["failing_pods"])))

    def _applied_level(self, context: str, fixture: str) -> int | None:
        items = kube.kubectl_json(context, "get", "namespaces", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}")["items"]
        if not items:
            return None
        levels = {item["metadata"].get("labels", {}).get(LEVEL_LABEL, "") for item in items}
        # Namespaces of two levels mean an interrupted change; report a mismatch so it is redone.
        return int(levels.pop()) if len(levels) == 1 and next(iter(levels)).isdigit() else -1

    def _wait_fixture_gone(self, context: str, fixture: str) -> None:
        kube.kubectl(context, "wait", "--for=delete", "namespace", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}",
                     f"--timeout={kube.APPLY_TIMEOUT_SECONDS}s", timeout=kube.APPLY_TIMEOUT_SECONDS)

    def apply_level(self, factor: str, level: float) -> dict[str, float]:
        values = self.plan.level_values(factor, level)
        LOG.info("applying %s=%s: %s", factor, level, values)
        self.level_errors = []
        self.stop_churn()
        self.ensure_clusters(int(values["clusters"]))
        self.ensure_nodes(values)
        self.ensure_fixtures(values)
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
        count = int(values["clusters"])
        contexts = self.contexts(count)
        totals = kube.totals(contexts)
        limits = self.install.limits()
        replicas = self.install.replicas()
        image = self.install.image()
        commit = commit_of(image)
        errors = list(self.level_errors)
        if not commit:
            errors.append(f"image {image!r} is not pinned by commit; the record is keyed by its tag")
            commit = image.rsplit(":", 1)[-1] or "unknown"
        before = self.install.snapshot()
        churned_before = self.churner.created if self.churner else 0
        started_iso = _now_iso()
        LOG.info("run %s %s=%s #%s", op.name, spec.factor, spec.level, spec.run)
        if op.kind == "prompt":
            env = operations.agent_env(self.install.context, self.install.namespace)
            outcome = operations.run_prompts(op, env, int(values["concurrent"]), self.cluster_names(count)[0])
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
        minutes = max((_epoch(ended_iso) - _epoch(started_iso)) / SECONDS_PER_MINUTE, 1 / SECONDS_PER_MINUTE)
        after = self.install.snapshot()
        metrics = collect.counter_metrics(before, after)
        metrics += self.install.pvc_metrics()
        logs = self.install.log_counts(started_iso)
        seen = next((m.value for m in metrics if m.name == "watcher_events_seen"), None)
        if seen is not None:
            metrics.append(records.Metric("watcher_events_seen_per_minute", seen / minutes))
        if self.churner:
            metrics.append(records.Metric("churn_achieved_per_minute", (self.churner.created - churned_before) / minutes))
            errors += self.churner.errors
            self.churner.errors = []
        metrics.append(records.Metric("triage_lost", float(logs["triage_lost"])))
        # The same refusal can reach both the client and the agent's log; count it once.
        metrics.append(records.Metric("run_slot_refusals", float(max(logs["run_slot_refusals"], outcome.run_slot_refusals))))
        metrics.append(records.Metric("failed_prompts", float(outcome.failed_prompts)))
        if outcome.response_seconds:
            metrics.append(records.Metric("response_seconds", max(outcome.response_seconds)))
        # tokens_* (from LiteLLM) count every model call in the window, background jobs included;
        # run_tokens_* (from the harness) count the sessions this run's prompts opened.
        for name, value in outcome.tokens.items():
            metrics.append(records.Metric(f"run_{name}", value))
        errors += outcome.errors + before.errors + after.errors
        provider_429s = logs["provider_429s"] + outcome.provider_429s
        excluded = RESPONSE_FITS if provider_429s and op.kind == "prompt" else []
        return records.Record(
            commit=commit, mode=self.plan.target["mode"], phase=self.plan.phase,
            factor=spec.factor, level=spec.level, operation=op.name, run=spec.run,
            started=started_iso, ended=ended_iso, image=image, values=values, totals=totals,
            limits=limits, replicas=replicas, metrics=metrics,
            provider_429s=provider_429s, excluded_from=list(excluded), errors=errors,
        )

    def pending(self, specs: list[RunSpec]) -> list[RunSpec]:
        """The runs whose key is not in the data file yet, for the build the install runs now."""
        done = records.existing_keys(self.data_file)
        image = self.install.image()
        commit = commit_of(image) or image.rsplit(":", 1)[-1] or "unknown"
        mode = self.plan.target["mode"]
        return [s for s in specs
                if records.record_key(commit, mode, s.factor, s.level, s.operation.name, s.run) not in done]

    def run(self, specs: list[RunSpec], force: bool) -> None:
        todo = specs if force else self.pending(specs)
        LOG.info("%s runs pending of %s", len(todo), len(specs))
        awaiting: list[tuple[float, records.Record]] = []
        relay = operations.Relay(self.install.context, self.install.namespace)
        relay.start()
        try:
            for (factor, level), group in itertools.groupby(todo, key=lambda s: (s.factor, s.level)):
                try:
                    values = self.apply_level(factor, level)
                except Exception:  # a level that cannot be applied is skipped; a resume retries it
                    LOG.exception("could not apply %s=%s; skipping its runs", factor, level)
                    continue
                for spec in group:
                    try:
                        if spec.operation.kind != "reset_soak":
                            # A reset (or a restart the agent did itself) leaves the watcher on the host
                            # cluster only; every other operation is measured with the fleet watched.
                            self.wait_registered(int(values["clusters"]))
                        record = self.execute(spec, values)
                    except Exception:  # one failed run is skipped; its key stays pending for a resume
                        LOG.exception("run %s %s=%s #%s failed; skipping it", spec.operation.name, factor, level, spec.run)
                        continue
                    # Merged at once without its peaks, so a crash loses nothing; merged again with them.
                    records.merge(self.data_file, [record])
                    awaiting.append((time.time(), record))
                    awaiting = self.flush_peaks(awaiting, wait=False)
        finally:
            self.stop_churn()
            relay.stop()
            self.flush_peaks(awaiting, wait=True)

    def flush_peaks(self, awaiting: list[tuple[float, records.Record]], wait: bool) -> list[tuple[float, records.Record]]:
        """Add Cloud Monitoring peaks to the records whose window is old enough, and merge them again.

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
        try:
            peaks = self.install.container_peaks(_epoch(record.started), _epoch(record.ended))
        except Exception as exc:  # a monitoring failure costs this record its peaks, not the campaign
            record.errors.append(f"monitoring: {type(exc).__name__}: {exc}")
            return
        for container, values in peaks.items():
            for name, value in values.items():
                record.metrics.append(records.Metric(name, value, container))

    def teardown(self, delete_fleet: bool) -> None:
        """Remove every fixture and shrink the pools; ``delete_fleet`` deletes the test clusters too."""
        self.stop_churn()
        existing = kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label)
        if delete_fleet:
            with ThreadPoolExecutor(max_workers=max(1, len(existing))) as pool:
                for job in [pool.submit(kube.delete_cluster, self.project, self.zone, name) for name in existing]:
                    job.result()
            return
        for name in existing:
            context = kube.context_name(self.project, self.zone, name)
            for fixture in FIXTURES:
                fixtures.delete_fixture(context, fixture)
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
    parser.add_argument("--limit", type=int, default=0, help="stop after this many runs (0: all)")
    parser.add_argument("--delete-fleet", action="store_true", help="teardown: delete the test clusters")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    plan = load_plan(args.config, validate_target=args.command != "plan")
    specs = [
        s for s in plan.matrix()
        if (not args.only_factor or s.factor == args.only_factor)
        and (not args.only_operation or s.operation.name == args.only_operation)
    ]
    if args.limit:
        specs = specs[: args.limit]
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
        runner.teardown(args.delete_fleet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
