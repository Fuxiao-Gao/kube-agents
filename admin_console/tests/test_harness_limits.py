from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin_console import harness_limits as hl
from admin_console.charts import harness_limit_chart

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "admin_console" / "app.py"
PAGE_PATH = "views/harness_limits.py"
PLAN_PATH = REPO_ROOT / "bench" / "scale" / "phase1.yaml"
MIB = 1024**2
GIB = 1024**3
BASE_TOTALS = {"clusters": 2, "nodes": 4}


def record(
    factor: str = "base",
    level: float = 0,
    operation: str = "fleet_audit",
    run: int = 1,
    *,
    metrics: tuple = (),
    totals: dict | None = None,
    limits: dict | None = None,
    excluded: tuple = (),
    commit: str = "a" * 40,
    mode: str = "next",
    started: str = "",
) -> dict:
    return {
        "key": f"{commit[:8]}|{mode}|{factor}|{level:g}|{operation}|{run}",
        "commit": commit,
        "mode": mode,
        "phase": "phase1",
        "factor": factor,
        "level": level,
        "operation": operation,
        "run": run,
        "started": started or f"2026-10-23T{run:02d}:00:00Z",
        "ended": f"2026-10-23T{run:02d}:30:00Z",
        "totals": dict(BASE_TOTALS) if totals is None else totals,
        "limits": limits or {},
        "metrics": [
            {"name": name, "value": value, **({"container": container} if container else {})}
            for name, container, value in metrics
        ],
        "provider_429s": 1 if excluded else 0,
        "excluded_from": list(excluded),
    }


def parse(*items: dict) -> tuple[hl.Record, ...]:
    return hl.parse_records({"schema": 1, "records": list(items)})


def point(x: float, y: float, *, excluded: bool = False) -> hl.Point:
    return hl.Point(x, y, f"{x}:{y}", 1, False, excluded)


def memory(container: str, mib: float) -> tuple:
    return (hl.MEMORY_PEAK, container, mib * MIB)


def clusters_line(base: float = 100.0, at_eight: float = 160.0) -> tuple[hl.Record, ...]:
    """A metric of 100 at the base (2 clusters) and 160 at 8: 10 per cluster."""
    return parse(
        record(metrics=((hl.SWEEP, None, base),)),
        record(
            "clusters",
            8,
            metrics=((hl.SWEEP, None, at_eight),),
            totals={"clusters": 8, "nodes": 16},
        ),
    )


class LoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, name: str, records: list[dict], schema: int = hl.SCHEMA_VERSION) -> Path:
        path = self.root / name
        path.write_text(json.dumps({"schema": schema, "records": records}))
        return path

    def test_the_measured_file_wins_over_the_sample(self):
        data = self.write("data.json", [record()])
        sample = self.write("sample.json", [record(), record(run=2)])

        dataset = hl.load_dataset(data, sample)

        self.assertFalse(dataset.is_sample)
        self.assertEqual(dataset.path, data)
        self.assertEqual(len(dataset.records), 1)

    def test_a_missing_measured_file_loads_the_sample(self):
        sample = self.write("sample.json", [record(), record(run=2)])

        dataset = hl.load_dataset(self.root / "missing.json", sample)

        self.assertTrue(dataset.is_sample)
        self.assertEqual(len(dataset.records), 2)

    def test_another_schema_version_is_refused(self):
        data = self.write("data.json", [record()], schema=2)

        with self.assertRaisesRegex(hl.HarnessDataError, "schema 2, expected 1"):
            hl.load_dataset(data)

    def test_a_malformed_record_names_its_key(self):
        broken = record()
        broken["metrics"] = "none"

        with self.assertRaisesRegex(hl.HarnessDataError, r"aaaaaaaa\|next\|base\|0"):
            parse(broken)

    def test_a_record_without_its_identity_is_refused(self):
        broken = record()
        del broken["operation"]

        with self.assertRaisesRegex(hl.HarnessDataError, "operation is missing"):
            parse(broken)

    def test_a_reading_that_is_not_finite_is_dropped(self):
        (loaded,) = parse(record(metrics=((hl.SWEEP, None, math.nan), (hl.RESPONSE, None, 3.0))))

        self.assertIsNone(loaded.value(hl.SWEEP))
        self.assertEqual(loaded.value(hl.RESPONSE), 3.0)

    def test_fields_outside_the_schema_are_ignored(self):
        extra = record(metrics=((hl.SWEEP, None, 1.0),))
        extra["errors"] = ["scrape litellm: timed out"]

        (loaded,) = parse(extra)

        self.assertEqual(loaded.value(hl.SWEEP), 1.0)

    def test_the_checked_in_sample_is_clearly_fake_and_two_point(self):
        records = hl.load_dataset(REPO_ROOT / "missing.json").records

        self.assertEqual({item.commit for item in records}, {"0" * 40})
        self.assertEqual({item.phase for item in records}, {"sample"})
        self.assertEqual(hl.factors(records), [hl.CLUSTERS, hl.NODES, hl.OBJECTS])
        self.assertTrue(any(item.is_base for item in records))
        for operation in hl.operations(records):
            for factor in hl.factors(records, operation):
                with self.subTest(operation=operation, factor=factor):
                    points = hl.series_points(
                        records, operation, factor, hl.MEMORY_PEAK, "agent-api-auth"
                    )
                    self.assertTrue(hl.fit_line(points).two_point)


class FactorValueTest(unittest.TestCase):
    def test_a_total_named_after_the_factor_is_the_measured_value(self):
        (loaded,) = parse(record("clusters", 8, totals={"clusters": 7}))

        self.assertEqual(hl.factor_value(loaded, hl.CLUSTERS), 7)

    def test_objects_are_the_design_kinds_per_cluster(self):
        totals = {
            "clusters": 2,
            "namespaces": 30,
            "deployments": 30,
            "services": 46,
            "hpas": 30,
            "pods": 94,
            "configmaps": 126,
            "events": 5000,
        }
        (loaded,) = parse(record("objects", 4000, totals=totals))

        # 356 objects over 2 clusters; events are not objects.
        self.assertEqual(hl.factor_value(loaded, hl.OBJECTS), 178)

    def test_a_missing_total_falls_back_to_the_level(self):
        (loaded,) = parse(record(hl.CHURN, 1000))

        self.assertEqual(hl.factor_value(loaded, hl.CHURN), 1000)

    def test_a_per_cluster_total_is_divided_by_the_cluster_count(self):
        (loaded,) = parse(record(hl.NODES, 100, totals={"clusters": 2, "nodes": 206}))

        self.assertEqual(hl.factor_value(loaded, hl.NODES), 103)

    def test_a_missing_per_cluster_total_falls_back_to_the_level(self):
        (loaded,) = parse(record(hl.NODES, 100, totals={"clusters": 2}))

        self.assertEqual(hl.factor_value(loaded, hl.NODES), 100)

    def test_a_base_record_sits_at_its_totals_or_the_plan_base(self):
        (loaded,) = parse(record())

        self.assertEqual(hl.factor_value(loaded, hl.NODES), 2)
        self.assertEqual(hl.factor_value(loaded, hl.OBJECTS), hl.PLAN_BASE_LEVELS[hl.OBJECTS])
        self.assertEqual(hl.factor_value(loaded, hl.CHURN), hl.PLAN_BASE_LEVELS[hl.CHURN])

    def test_a_run_of_another_factor_is_not_on_the_axis(self):
        records = parse(
            record(metrics=((hl.SWEEP, None, 1.0),)),
            record("clusters", 8, metrics=((hl.SWEEP, None, 2.0),)),
            record(hl.CHURN, 1000, metrics=((hl.SWEEP, None, 3.0),)),
        )

        points = hl.series_points(records, "fleet_audit", hl.CHURN, hl.SWEEP)

        self.assertEqual([(item.x, item.y) for item in points], [(10.0, 1.0), (1000.0, 3.0)])


class FitTest(unittest.TestCase):
    def test_two_points_give_the_line_through_them(self):
        fit = hl.fit_line([point(2, 10), point(8, 40)])

        self.assertAlmostEqual(fit.slope, 5)
        self.assertAlmostEqual(fit.intercept, 0)
        self.assertTrue(fit.two_point)
        self.assertEqual(fit.label, "two-point estimate")

    def test_repeated_runs_at_two_levels_are_still_a_two_point_estimate(self):
        fit = hl.fit_line([point(2, 10), point(8, 40), point(8, 50)])

        self.assertAlmostEqual(fit.at(2), 10)
        self.assertAlmostEqual(fit.at(8), 45)
        self.assertTrue(fit.two_point)

    def test_three_levels_are_a_least_squares_fit(self):
        fit = hl.fit_line([point(0, 1), point(1, 3), point(2, 2)])

        self.assertAlmostEqual(fit.slope, 0.5)
        self.assertAlmostEqual(fit.intercept, 1.5)
        self.assertFalse(fit.two_point)
        self.assertEqual(fit.label, "least-squares fit")

    def test_one_level_has_no_fit(self):
        self.assertIsNone(hl.fit_line([point(2, 10), point(2, 12)]))

    def test_an_excluded_point_stays_out_of_the_fit(self):
        fit = hl.fit_line([point(2, 10), point(8, 40), point(8, 400, excluded=True)])

        self.assertAlmostEqual(fit.slope, 5)
        self.assertIsNone(hl.fit_line([point(2, 10), point(8, 400, excluded=True)]))

    def test_a_record_excludes_only_the_metrics_it_names(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 20.0), memory("agent-api-auth", 400))),
            record(
                "clusters",
                8,
                metrics=((hl.RESPONSE, None, 900.0), memory("agent-api-auth", 700)),
                totals={"clusters": 8},
                excluded=(hl.RESPONSE,),
            ),
        )

        response = hl.series_points(records, "fleet_audit", hl.CLUSTERS, hl.RESPONSE)
        used = hl.series_points(
            records, "fleet_audit", hl.CLUSTERS, hl.MEMORY_PEAK, "agent-api-auth"
        )

        self.assertEqual([item.excluded for item in response], [False, True])
        self.assertIsNone(hl.fit_line(response))
        self.assertIsNotNone(hl.fit_line(used))


class PredictionTest(unittest.TestCase):
    def fleet(self, records, **inputs: float) -> dict[str, float]:
        return {**hl.base_fleet(records), **inputs}

    def test_at_the_base_point_the_prediction_is_the_measured_base(self):
        records = clusters_line()

        prediction = hl.predict(records, hl.SWEEP, None, hl.base_fleet(records))

        self.assertAlmostEqual(prediction.value, 100)
        self.assertEqual(hl.basis(prediction), "measured at the base point")

    def test_inside_the_measured_range_the_line_interpolates(self):
        records = clusters_line()

        prediction = hl.predict(records, hl.SWEEP, None, self.fleet(records, clusters=5))

        self.assertAlmostEqual(prediction.value, 130)
        self.assertEqual(
            hl.basis(prediction), "within the measured range; two-point estimate"
        )

    def test_past_the_largest_measured_value_it_is_predicted_not_measured(self):
        records = clusters_line()

        prediction = hl.predict(records, hl.SWEEP, None, self.fleet(records, clusters=20))

        self.assertAlmostEqual(prediction.value, 280)
        self.assertEqual(
            hl.basis(prediction),
            "predicted, not measured: clusters 20 > 8; two-point estimate",
        )

    def test_each_factor_adds_its_increase_from_its_own_base(self):
        records = parse(
            record(metrics=((hl.SWEEP, None, 100.0),)),
            record(
                "clusters", 8, metrics=((hl.SWEEP, None, 160.0),), totals={"clusters": 8, "nodes": 16}
            ),
            record(
                "nodes", 100, metrics=((hl.SWEEP, None, 198.0),), totals={"clusters": 2, "nodes": 200}
            ),
        )

        # Clusters add 10 for each of 6 more; nodes add 1 for each of 8 more per cluster.
        prediction = hl.predict(
            records, hl.SWEEP, None, self.fleet(records, clusters=8, nodes=10)
        )

        self.assertAlmostEqual(prediction.value, 168)
        self.assertEqual(
            hl.grown_factors(records, self.fleet(records, clusters=8, nodes=10)),
            [hl.CLUSTERS, hl.NODES],
        )
        self.assertEqual(hl.grown_factors(records, self.fleet(records, clusters=8)), [hl.CLUSTERS])
        self.assertEqual(hl.grown_factors(records, hl.base_fleet(records)), [])

    def test_the_largest_base_and_the_largest_increase_count(self):
        records = parse(
            record(metrics=((hl.SWEEP, None, 100.0),)),
            record(
                "clusters", 8, metrics=((hl.SWEEP, None, 130.0),), totals={"clusters": 8}
            ),
            record(operation="fan_out", metrics=((hl.SWEEP, None, 80.0),)),
            record(
                "clusters",
                8,
                operation="fan_out",
                metrics=((hl.SWEEP, None, 140.0),),
                totals={"clusters": 8},
            ),
        )

        prediction = hl.predict(records, hl.SWEEP, None, self.fleet(records, clusters=8))

        # The fleet audit's base (100) plus the fan-out's increase (60).
        self.assertEqual(prediction.base_operation, "fleet_audit")
        self.assertEqual(prediction.contributions[0].operation, "fan_out")
        self.assertAlmostEqual(prediction.value, 160)

    def test_an_excluded_base_run_stays_out_of_the_base_value(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 20.0),)),
            record(run=2, metrics=((hl.RESPONSE, None, 500.0),), excluded=(hl.RESPONSE,)),
            record("clusters", 8, metrics=((hl.RESPONSE, None, 50.0),), totals={"clusters": 8}),
        )

        prediction = hl.predict(records, hl.RESPONSE, None, hl.base_fleet(records))

        self.assertAlmostEqual(prediction.value, 20)

    def test_without_base_runs_each_factors_lowest_level_is_the_base(self):
        records = parse(
            record("clusters", 2, metrics=((hl.SWEEP, None, 100.0),), totals={"clusters": 2}),
            record("clusters", 8, metrics=((hl.SWEEP, None, 160.0),), totals={"clusters": 8}),
        )

        prediction = hl.predict(records, hl.SWEEP, None, self.fleet(records, clusters=4))

        self.assertAlmostEqual(prediction.base, 100)
        self.assertAlmostEqual(prediction.value, 120)

    def test_a_factor_measured_at_one_level_is_reported_without_a_fit(self):
        records = parse(
            record(metrics=((hl.SWEEP, None, 100.0),)),
            record(hl.CHURN, 10, metrics=((hl.SWEEP, None, 101.0),)),
        )

        prediction = hl.predict(records, hl.SWEEP, None, self.fleet(records, churn_per_minute=1000))

        self.assertEqual(prediction.missing_fits, (hl.CHURN,))
        self.assertIn("no fit for churn_per_minute", hl.basis(prediction))

    def test_the_default_fleet_is_the_base_point_of_the_data(self):
        records = hl.load_dataset(REPO_ROOT / "missing.json").records

        fleet = hl.base_fleet(records)

        # 356 objects over the sample's 2 base clusters; churn is not in totals.
        self.assertEqual(fleet[hl.CLUSTERS], 2)
        self.assertEqual(fleet[hl.NODES], 2)
        self.assertEqual(fleet[hl.OBJECTS], 178)
        self.assertEqual(fleet[hl.CHURN], hl.PLAN_BASE_LEVELS[hl.CHURN])


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.records = parse(
            record(
                metrics=(memory("agent-api-auth", 500), (hl.CPU_PEAK, "agent-api-auth", 0.2)),
                limits={"agent-api-auth": {"memory_bytes": 2 * GIB, "cpu_cores": 1.0}},
            ),
            record(
                "clusters",
                8,
                metrics=(memory("agent-api-auth", 800), (hl.CPU_PEAK, "agent-api-auth", 0.3)),
                totals={"clusters": 8},
            ),
        )

    def checks(self, clusters: float, limits: dict) -> dict[str, hl.Check]:
        fleet = {**hl.base_fleet(self.records), hl.CLUSTERS: clusters}
        checks = hl.resource_checks(self.records, fleet, ["agent-api-auth"], limits)
        return {check.metric: check for check in checks}

    def test_headroom_and_status_against_the_limit(self):
        limits = hl.latest_limits(self.records)

        passing = self.checks(8, limits)[hl.MEMORY_PEAK]
        failing = self.checks(40, limits)[hl.MEMORY_PEAK]

        self.assertEqual(passing.status, hl.PASS)
        self.assertAlmostEqual(hl.headroom(passing), (2048 - 800) * MIB)
        # 500 MiB + 50 MiB per cluster x 38 clusters = 2,400 MiB.
        self.assertEqual(failing.status, hl.FAIL)
        self.assertAlmostEqual(hl.headroom(failing), (2048 - 2400) * MIB)
        self.assertAlmostEqual(failing.reached, (2048 - 500) / (2400 - 500))

    def test_the_limits_table_round_trips_in_mib_and_cores(self):
        table = hl.limit_table(hl.latest_limits(self.records), ["agent-api-auth", "other"])

        self.assertEqual(table[hl.LIMIT_MEMORY_COLUMN], [2048.0, 0.0])
        table[hl.LIMIT_CPU_COLUMN] = [None, 0.5]
        self.assertEqual(
            hl.limits_from_table(table),
            {
                "agent-api-auth": {hl.LIMIT_MEMORY: 2 * GIB, hl.LIMIT_CPU: 0.0},
                "other": {hl.LIMIT_MEMORY: 0.0, hl.LIMIT_CPU: 0.5},
            },
        )

    def test_an_edited_limit_replaces_the_measured_one(self):
        limits = {"agent-api-auth": {hl.LIMIT_MEMORY: 512 * MIB, hl.LIMIT_CPU: 1.0}}

        self.assertEqual(self.checks(8, limits)[hl.MEMORY_PEAK].status, hl.FAIL)

    def test_a_container_without_a_limit_is_reported(self):
        self.assertEqual(self.checks(8, {})[hl.CPU_PEAK].status, hl.NO_LIMIT)

    def test_the_response_gate_warns_above_30_s_and_fails_above_60_s(self):
        self.assertEqual(hl.evaluate(20, hl.RESPONSE_LINES), (hl.PASS, None))
        self.assertEqual(hl.evaluate(45, hl.RESPONSE_LINES)[0], hl.WARN)
        status, crossed = hl.evaluate(70, hl.RESPONSE_LINES)
        self.assertEqual((status, crossed.label), (hl.FAIL, "60 s: investigate"))

    def test_the_credential_proxy_fails_above_the_15_s_preflight(self):
        self.assertEqual(hl.evaluate(16, hl.PROXY_WAIT_LINES)[0], hl.FAIL)

    def test_any_count_above_zero_fails(self):
        self.assertEqual(hl.evaluate(0, hl.ABOVE_ZERO)[0], hl.PASS)
        self.assertEqual(hl.evaluate(0.2, hl.ABOVE_ZERO)[0], hl.FAIL)

    def test_reached_at_is_the_share_of_growth_that_crosses_the_threshold(self):
        base = hl.base_fleet(self.records)
        grown = hl.predict(
            self.records, hl.MEMORY_PEAK, "agent-api-auth", {**base, hl.CLUSTERS: 8}
        )
        at_base = hl.predict(self.records, hl.MEMORY_PEAK, "agent-api-auth", base)

        # 500 MiB at the base and 800 MiB at 8 clusters: 650 MiB is half way.
        self.assertAlmostEqual(hl.reached_at(grown, 650 * MIB), 0.5)
        self.assertEqual(hl.reached_at(grown, 400 * MIB), 0.0)
        self.assertIsNone(hl.reached_at(at_base, 2 * GIB))

    def test_the_first_limit_is_crossed_at_the_smallest_growth(self):
        checks = self.checks(40, hl.latest_limits(self.records))

        first = hl.first_limit(checks.values())

        # Memory crosses 2 GiB at 81% of the growth to 40 clusters; CPU only
        # crosses 1 core at 126%.
        self.assertEqual(first.metric, hl.MEMORY_PEAK)
        self.assertAlmostEqual(first.reached, (2048 - 500) / (2400 - 500))
        self.assertAlmostEqual(checks[hl.CPU_PEAK].reached, (1.0 - 0.2) / (0.2 / 6 * 19))

    def test_the_response_gate_reads_the_query_operations_only(self):
        records = parse(
            record(metrics=((hl.RESPONSE, None, 240.0),)),
            record("clusters", 8, metrics=((hl.RESPONSE, None, 720.0),), totals={"clusters": 8}),
            record(operation="fan_out", metrics=((hl.RESPONSE, None, 25.0),)),
            record(
                "clusters",
                8,
                operation="fan_out",
                metrics=((hl.RESPONSE, None, 28.0),),
                totals={"clusters": 8},
            ),
        )
        fleet = hl.base_fleet(records)

        (check,) = hl.threshold_checks(records, fleet, [])
        audit_chart = hl.metric_charts(records, "fleet_audit", hl.CLUSTERS, [], fleet)[0]
        fan_out_chart = hl.metric_charts(records, "fan_out", hl.CLUSTERS, [], fleet)[0]

        self.assertEqual((check.status, check.prediction.value), (hl.PASS, 25.0))
        self.assertEqual(check.name, "Response time (fan_out)")
        self.assertEqual(audit_chart.lines, ())
        self.assertEqual(fan_out_chart.lines, hl.RESPONSE_LINES)

    def test_the_pvc_is_checked_against_its_measured_capacity(self):
        records = parse(
            record(metrics=((hl.PVC_USED, None, 40 * GIB), (hl.PVC_CAPACITY, None, 50 * GIB))),
            record(
                "clusters",
                8,
                metrics=((hl.PVC_USED, None, 46 * GIB), (hl.PVC_CAPACITY, None, 50 * GIB)),
                totals={"clusters": 8},
            ),
        )
        fleet = {**hl.base_fleet(records), hl.CLUSTERS: 16}

        (check,) = hl.threshold_checks(records, fleet, [])

        self.assertEqual(check.lines[0].value, 50 * GIB)
        self.assertEqual(check.status, hl.FAIL)


class RecommendationTest(unittest.TestCase):
    def test_the_margin_is_thirty_percent(self):
        self.assertAlmostEqual(hl.with_margin(100), 130)

    def test_a_recommendation_is_the_peak_plus_the_margin_in_its_field(self):
        records = parse(
            record(
                metrics=(
                    memory("agent-api-auth", 440),
                    (hl.CPU_PEAK, "agent-api-auth", 0.5),
                    memory("manager", 40),
                    memory("litellm", 300),
                )
            )
        )

        items = {
            item.container: item
            for item in hl.recommendations(
                records, hl.base_fleet(records), ["agent-api-auth", "manager", "litellm"]
            )
        }

        auth = items["agent-api-auth"]
        self.assertAlmostEqual(auth.memory_bytes, 1.3 * 440 * MIB)
        self.assertAlmostEqual(auth.cpu_cores, 0.65)
        self.assertEqual(auth.field, "spec.deployment.agentAPIAuth.resources")
        self.assertIn("operator.resources", items["manager"].field)
        self.assertIsNone(items["manager"].cpu_cores)
        self.assertEqual(items["litellm"].field, hl.NO_FIELD)
        memory_total, cpu_total = hl.requested_total(items.values())
        self.assertAlmostEqual(memory_total, 1.3 * (440 + 40 + 300) * MIB)
        self.assertAlmostEqual(cpu_total, 0.65)

    def test_quantities_round_up(self):
        self.assertEqual(hl.memory_quantity(1.3 * 440 * MIB), "572Mi")
        self.assertEqual(hl.memory_quantity(572.1 * MIB), "573Mi")
        self.assertEqual(hl.cpu_quantity(0.65), "650m")
        self.assertEqual(hl.cpu_quantity(0.6501), "651m")
        self.assertEqual(hl.storage_quantity(1.5 * GIB), "2Gi")

    def test_the_smallest_machine_holds_both_memory_and_cpu(self):
        self.assertEqual(hl.smallest_machine(5 * GIB, 1.0).name, "e2-standard-2")
        self.assertEqual(hl.smallest_machine(5 * GIB, 1.95).name, "e2-standard-4")
        self.assertEqual(hl.smallest_machine(60 * GIB, 4.0).name, "n2-highmem-16")
        self.assertIsNone(hl.smallest_machine(200 * GIB, 1.0))
        self.assertIsNone(hl.smallest_machine(1 * GIB, 16.0))

    def test_the_machine_list_runs_from_smallest_to_largest(self):
        memory_sizes = [machine.allocatable_memory_gib for machine in hl.MACHINE_TYPES]
        cpu_sizes = [machine.allocatable_cpu for machine in hl.MACHINE_TYPES]

        self.assertEqual(memory_sizes, sorted(memory_sizes))
        self.assertEqual(cpu_sizes, sorted(cpu_sizes))
        for machine in hl.MACHINE_TYPES:
            with self.subTest(machine=machine.name):
                self.assertLess(machine.allocatable_cpu, machine.vcpu)
                self.assertLess(machine.allocatable_memory_gib, machine.memory_gib)


class PlanTest(unittest.TestCase):
    @unittest.skipUnless(PLAN_PATH.exists(), "no Phase 1 plan in this checkout")
    def test_the_base_levels_and_factors_match_the_plan(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        plan = yaml.safe_load(PLAN_PATH.read_text())

        self.assertEqual(
            {name: float(value) for name, value in plan["base"].items()},
            dict(hl.PLAN_BASE_LEVELS),
        )
        self.assertEqual(set(plan["factors"]), set(hl.FACTORS))
        self.assertIn(hl.QUERY_OPERATIONS[0], plan["operations"])


class ChartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.records = hl.load_dataset(REPO_ROOT / "missing.json").records
        self.containers = hl.containers(self.records)
        self.fleet = {**hl.base_fleet(self.records), hl.CLUSTERS: 64}

    def memory_chart(self) -> hl.MetricChart:
        charts = hl.metric_charts(
            self.records, "fleet_audit", hl.CLUSTERS, self.containers, self.fleet
        )
        return next(chart for chart in charts if chart.metric == hl.MEMORY_PEAK)

    def test_a_chart_has_a_series_per_container_with_its_limit(self):
        chart = self.memory_chart()

        self.assertEqual([item.name for item in chart.series], self.containers)
        self.assertEqual(chart.series[1].limit, 2 * GIB)
        self.assertTrue(all(item.fit.two_point for item in chart.series))
        self.assertEqual(chart.x_end, 64)
        self.assertEqual(chart.fleet_x, 64)

    def test_the_figure_draws_the_fit_and_the_prediction(self):
        colors = hl.container_colors(self.containers, ("#000001", "#000002"), "#999999")

        figure = harness_limit_chart(self.memory_chart(), colors)

        names = [trace.name for trace in figure.data]
        self.assertIn("agent-api-auth · two-point estimate", names)
        self.assertIn("agent-api-auth · predicted, not measured", names)
        self.assertIn("predicted, not measured", names)
        self.assertIn("agent-api-auth limit", names)
        self.assertIn("this fleet", names)
        self.assertEqual(figure.data[0].marker.color, "#000001")

    def test_colours_follow_the_container_not_the_selection(self):
        palette = ("#000001", "#000002")

        colors = hl.container_colors(["a", "b", "c"], palette, "#999999")

        self.assertEqual(colors, {None: "#000001", "a": "#000001", "b": "#000002", "c": "#999999"})


class HarnessLimitsPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.patches = [
            patch.dict(
                os.environ,
                {
                    "KUBE_AGENTS_ADMIN_USER": "admin@example.com",
                    "KUBE_AGENTS_GCLOUD_PROJECT": "test-project-01",
                    "KUBE_AGENTS_ADMIN_CONNECTION_STATE": str(root / "connection.json"),
                },
            ),
            # No measured file, so the page shows the checked-in sample.
            patch.object(hl, "DATA_PATH", root / "harness-limits.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def open_page(self) -> AppTest:
        app = AppTest.from_file(APP_PATH, default_timeout=60).run()
        return app.switch_page(PAGE_PATH).run()

    def test_the_page_renders_the_sample_without_a_connection(self):
        app = self.open_page()

        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertEqual(app.title[0].value, "Harness limits")
        self.assertTrue(any("Sample data" in item.value for item in app.warning))
        self.assertFalse(any("Connect a project" in item.value for item in app.info))
        self.assertTrue(any("checks pass" in item.value for item in app.success))
        self.assertTrue(any("e2-standard-4" in item.value for item in app.success))
        self.assertEqual(app.expander[-1].label, "Raw records")

    def test_a_larger_fleet_fails_and_is_predicted_not_measured(self):
        app = self.open_page()
        next(item for item in app.number_input if item.label == "Clusters").set_value(64.0)
        app = app.run()

        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])
        self.assertTrue(any("checks fail" in item.value for item in app.error))
        self.assertTrue(
            any("predicted, not measured" in item.value for item in app.warning)
        )


if __name__ == "__main__":
    unittest.main()
