"""Tests for the harness-scale measurement runner's pure parts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench.scale import collect, fixtures, records
from kube_agents_bench.scale.collect import Snapshot
from kube_agents_bench.scale.config import load_plan

REPO_BENCH = Path(__file__).resolve().parents[1]
PHASE1 = REPO_BENCH / "scale" / "phase1.yaml"


def _record(run: int = 1, commit: str = "a" * 40, value: float = 1.0) -> records.Record:
    return records.Record(
        commit=commit, mode="next", phase="phase1", factor="clusters", level=8,
        operation="fleet_audit", run=run, started=f"2026-10-23T00:0{run}:00Z", ended="2026-10-23T00:30:00Z",
        metrics=[records.Metric("response_seconds", value), records.Metric("memory_peak_bytes", 5.0, "agent-api-auth")],
    )


def test_record_key_names_one_run():
    assert _record().key == "aaaaaaaa|next|clusters|8|fleet_audit|1"
    assert records.record_key("b" * 40, "today", "objects", 4000.0, "onboarding", 2) == "bbbbbbbb|today|objects|4000|onboarding|2"


def test_merge_replaces_same_key_and_keeps_others(tmp_path):
    path = tmp_path / "data.json"
    assert records.merge(path, [_record(1), _record(2)]) == 0
    assert records.merge(path, [_record(1, value=9.0), _record(1, commit="c" * 40)]) == 1
    data = json.loads(path.read_text())
    assert data["schema"] == records.SCHEMA_VERSION
    by_key = {r["key"]: r for r in data["records"]}
    assert len(by_key) == 3
    assert by_key["aaaaaaaa|next|clusters|8|fleet_audit|1"]["metrics"][0] == {"name": "response_seconds", "value": 9.0}
    assert by_key["aaaaaaaa|next|clusters|8|fleet_audit|1"]["metrics"][1]["container"] == "agent-api-auth"
    assert records.existing_keys(path) == set(by_key)


def test_load_refuses_another_schema(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"schema": 99, "records": []}))
    with pytest.raises(ValueError):
        records.load(path)


def test_phase1_matrix_runs_base_once_and_each_factor_only_for_its_operations():
    plan = load_plan(PHASE1, environ={})
    specs = plan.matrix()
    base = [s for s in specs if s.factor == "base"]
    assert len(base) == sum(op.runs for op in plan.operations)
    # A factor's base value is not re-run under the factor.
    assert not [s for s in specs if s.factor == "clusters" and s.level == plan.base["clusters"]]
    # Every non-base run uses an operation that lists its factor.
    assert all(s.factor in s.operation.factors for s in specs if s.factor != "base")
    assert plan.level_values("objects", 4000)["objects"] == 4000
    assert plan.level_values("objects", 4000)["clusters"] == plan.base["clusters"]


def test_environment_overrides_the_target():
    plan = load_plan(PHASE1, environ={"SCALE_PROJECT_ID": "other-project", "SCALE_MODE": "today"})
    assert plan.target["project_id"] == "other-project"
    assert plan.target["mode"] == "today"


def test_object_mix_matches_the_design_table():
    for level in (100, 4000, 10000):
        assert fixtures.object_mix(level).total == level
    assert fixtures.object_mix(4000).pods == 1200


def test_objects_manifests_hold_the_level_and_carry_the_label():
    manifests = fixtures.objects_manifests(100)
    deployments = [m for m in manifests if m["kind"] == "Deployment"]
    pods = sum(m["spec"]["replicas"] for m in deployments)
    assert len(manifests) + pods == 100
    assert all(m["metadata"]["labels"][fixtures.FIXTURE_LABEL] == "objects" for m in manifests)
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    requests = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    assert requests["cpu"] == fixtures.POD_CPU_REQUEST


def test_fixture_pods_counts_every_fixture():
    values = {"objects": 4000, "warning_events": 100, "listing_pods": 100, "churn_per_minute": 1000}
    assert fixtures.fixture_pods(values, 30) == 1200 + 100 + 100 + 500


PROMETHEUS_TEXT = """# HELP k8s_event_watcher_events_seen_total Events seen.
# TYPE k8s_event_watcher_events_seen_total counter
k8s_event_watcher_events_seen_total{cluster="a"} 10
k8s_event_watcher_events_seen_total{cluster="b"} 5
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="1"} 4
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="15"} 9
kubeagents_credential_proxy_admission_wait_seconds_bucket{le="+Inf"} 10
litellm_input_tokens_metric_total{model="m"} 1000
"""


def test_parse_prometheus_sums_families_and_reads_buckets():
    samples = collect.parse_prometheus(PROMETHEUS_TEXT)
    assert collect.family_sum(samples, "k8s_event_watcher_events_seen_total") == 15
    buckets = collect.histogram_buckets(samples, "kubeagents_credential_proxy_admission_wait_seconds")
    assert buckets == {1.0: 4, 15.0: 9, float("inf"): 10}
    assert collect.histogram_quantile(buckets, 0.5) == 15.0
    assert collect.histogram_quantile({}, 0.5) is None


def test_counter_metrics_report_growth_across_the_window():
    before = Snapshot(restarts={"agent-api-auth": 1}, watcher=collect.parse_prometheus(PROMETHEUS_TEXT))
    after_text = PROMETHEUS_TEXT.replace('{cluster="b"} 5', '{cluster="b"} 25').replace("1000", "1600")
    after = Snapshot(restarts={"agent-api-auth": 2}, oomkills={"agent-api-auth": 1},
                     watcher=collect.parse_prometheus(after_text), litellm=collect.parse_prometheus(after_text))
    before.litellm = collect.parse_prometheus(PROMETHEUS_TEXT)
    metrics = {(m.name, m.container): m.value for m in collect.counter_metrics(before, after)}
    assert metrics[("restarts", "agent-api-auth")] == 1
    assert metrics[("oomkills", "agent-api-auth")] == 1
    assert metrics[("watcher_events_seen", None)] == 20
    assert metrics[("tokens_input", None)] == 600


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2Gi", 2 * 1024**3), ("128Mi", 128 * 1024**2), ("500m", 0.5), ("1", 1.0), ("1G", 1e9)],
)
def test_parse_quantity(text, expected):
    assert collect.parse_quantity(text) == pytest.approx(expected)
