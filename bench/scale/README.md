# bench/scale

Load and sizing measurements of the agent harness at fleet scale. The plan, the metrics and their
thresholds are in
[`docs/designs/agent-harness-bottlenecks.md`](../../docs/designs/agent-harness-bottlenecks.md).
The code is `kube_agents_bench/scale/`; this directory holds what it reads and writes.

- `phase1.yaml`: the Phase 1 plan: target, factors and levels, operations and run counts. A later
  phase is another file of the same shape.
- `harness-limits.json`: every measured run. A run merges its records in; nothing is removed.
- `harness-limits.sample.json`: a small fake data set the admin console page and its tests use when
  `harness-limits.json` does not exist.

Run `make scale-plan` to print the matrix and `make scale-run` to execute it; `SCALE_CONFIG` picks
another plan file. The target comes from the plan's `target` block, and any key there can be
overridden as `SCALE_<KEY>` in the environment (`SCALE_PROJECT_ID`, `SCALE_HOST_CONTEXT`, ...). A
run skips every record already in `harness-limits.json`, so an interrupted run resumes;
`python -m kube_agents_bench.scale.runner run <plan> --force` re-runs and replaces. The runner
creates and deletes test clusters named `<cluster_prefix>NN` in the target zone, so point it at a
project and prefix that hold nothing else.

## `harness-limits.json`

```json
{
  "schema": 1,
  "records": [
    {
      "key": "eeb8a95c|next|clusters|8|fleet_audit|1",
      "commit": "eeb8a95ceb0a9efd66434161f87820b35b65360c",
      "mode": "next",
      "phase": "phase1",
      "factor": "clusters",
      "level": 8,
      "operation": "fleet_audit",
      "run": 1,
      "started": "2026-10-23T01:02:03Z",
      "ended": "2026-10-23T01:31:40Z",
      "totals": { "clusters": 8, "nodes": 16, "pods": 210, "namespaces": 80 },
      "limits": {
        "agent-api-auth": { "memory_bytes": 2147483648, "cpu_cores": 1.0 }
      },
      "metrics": [
        {
          "name": "memory_peak_bytes",
          "container": "agent-api-auth",
          "value": 734003200
        },
        { "name": "response_seconds", "value": 812.4 }
      ],
      "provider_429s": 0,
      "excluded_from": []
    }
  ]
}
```

`key` is `commit[:8]|mode|factor|level|operation|run`; a record with the same key replaces the old
one. `level` is the planned level; `totals` holds what the clusters really had, and fits use the
totals. `limits` are the containers' limits during the run, read from the live pods. A metric
without `container` belongs to the run. `excluded_from` names the fits the record is left out of,
for example `["response_seconds"]` when the run saw a provider 429.

Metric names: `memory_peak_bytes`, `cpu_peak_cores`, `restarts`, `oomkills` (per container);
`pvc_used_bytes`, `pvc_capacity_bytes`, `profile_bytes_max`; `proxy_wait_p50_seconds`,
`proxy_wait_max_seconds`, `proxy_refusals`; `watcher_events_seen`, `watcher_session_creates`,
`watcher_events_injected`, `watcher_quota_suppressed`; `triage_lost`, `run_slot_refusals`;
`tokens_input`, `tokens_output`, `tokens_cached` (LiteLLM's counters over the run's window, every caller
included) and `run_tokens_input`, `run_tokens_output`, `run_tokens_cached` (what the run's own prompts
used, as the harness reports it); `response_seconds` (the slowest prompt of the run).
