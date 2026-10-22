# Agent harness bottlenecks at fleet scale

## High level summary

kube-agents runs the Platform Agent and its supporting components in one GKE cluster. This document calls them the harness. The harness watches and queries a fleet of clusters. A large fleet puts load on the harness. Too much load makes responses slow and causes OOMKills. An OOMKill stops a container that uses more memory than its limit.

This research measures the harness on small fleets. Each experiment increases one stress point. A stress point is one property of the fleet or of its use, for example the number of clusters. The research fits a model to the data and predicts the limits for larger fleets. The result is a chart that answers two questions. First: are the current container limits enough for a given fleet? Second: which node size and container limits does a given fleet need?

# Context

When kube-agents is installed to large projects, depending on the shape of the fleet and usage pattern, the agent harness is stressed in various ways. This results in slow response time, or even OOMKills.

## Estimating Limits

Driven by client use cases, some projects have thousands of clusters, some have a large volume of logs, some have very short pod lifecycles; they all stress the harness in different ways.

Many are infeasible to replicate in a testing environment. So we estimate system limits by:

1. Set up a base project, starting from a reasonable size
2. Progressively experiment on different sizes of key stress points, one at a time
3. Collect data points, 3 runs per point (Phase 1 runs the 2-hour onboarding window and the event watcher soak once, so those points have no spread)
4. Fit a model per metric: a line where the data is linear (memory against objects, disk against activity), and the measured knee where a queue saturates (run slots, credential proxy admission)
5. Visualize known points via plot
6. Predict larger values on a dynamic plot, with an error band that widens past the largest measured point
7. Suggest kube-agents deployment size according to fleet size

Model responses vary from run to run, and a provider 429 can look like a harness limit. Each run records the provider 429s it saw. A run that hit one is excluded from the response-time fit, but its memory, CPU and disk values stay: a model 429 slows the agent down and does not lower its memory. The onboarding window is the exception: a fresh install can use up its own TPM quota ([#3388](https://github.com/gke-labs/kube-agents/issues/3388)), so there the 429 count is a result, not a reason to exclude the run.

## Metrics

Each metric has a threshold. The breaking point of a metric is the stress level where it reaches its threshold.

| Metric                                                                                                              | Source                                                                                                                                                   | Threshold                                                                                     |
| ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Memory per container: working set over the run's time window                                                        | Cloud Monitoring `kubernetes.io/container/memory/used_bytes`; cgroup `memory.peak` as a check only, because it counts from container start               | The container's memory limit                                                                  |
| OOMKills and restarts per container (`agent-api-auth`, `envoy-credential-proxy`, the controller-manager)            | `lastState`                                                                                                                                              | Any                                                                                           |
| CPU usage and throttling per container                                                                              | cAdvisor                                                                                                                                                 | The container's CPU limit                                                                     |
| Disk usage of the agent data PVC, in total and per Cluster Agent profile                                            | `du` on the data volume                                                                                                                                  | PVC capacity                                                                                  |
| Network throughput                                                                                                  | cAdvisor                                                                                                                                                 | None; recorded only                                                                           |
| Credential proxy queue                                                                                              | `kubeagents_credential_proxy_slots_in_use`, `kubeagents_credential_proxy_admission_wait_seconds`, `kubeagents_credential_proxy_admission_refusals_total` | Wait above 15s (the Cluster Agent preflight); refusal at 60s                                  |
| Event pipeline                                                                                                      | `k8s_event_watcher_events_seen_total`, `k8s_event_watcher_session_creates_total`, `k8s_event_watcher_events_injected_total`                              | Session creates per injected event; recorded for the fit                                      |
| Triage starts lost                                                                                                  | No metric today: count `Aborting troubleshooting trigger` log lines, or add a counter first                                                              | Any                                                                                           |
| Run slots occupied and `429 Too many concurrent runs` refusals                                                      | No metric today: the counter is in Hermes memory; count 429s at the client, or add a gauge first                                                         | 10 slots                                                                                      |
| Kanban `ready` queue depth and wait                                                                                 | `kanban_board_health.py`                                                                                                                                 | Wait longer than the response time gate                                                       |
| Model token counts per operation (input, output, cached) and per minute, provider 429s, time to first token         | LiteLLM, Vertex AI quota metrics                                                                                                                         | The project's TPM quota                                                                       |
| Scheduled sweep duration (hourly Cluster Agent reconcile, Controller Stall Watch)                                   | Job logs                                                                                                                                                 | The job's schedule interval                                                                   |
| Response time of typical queries: first card, final answer; acknowledgement only where a chat platform is connected | The runner's own timings and the kanban card timestamps; traces where the install exports them                                                           | [#3140](https://github.com/gke-labs/kube-agents/issues/3140) gates: <30s OK, >60s investigate |

## Testing Stress Points

### Base numbers

2 clusters, 2 nodes each, about 100 fixture objects per cluster, 10 pods created per minute, 1 Warning event per minute, 1 concurrent request, and a 100-pod listing. Each experiment starts from the base and changes one factor, so the first level of each factor is the base.

### Progressive experiments

| Factor                                                                     | Levels                                               | Notes                                                                                                                                                                                                                               |
| -------------------------------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Number of clusters                                                         | 2, 4, 8, 64                                          | Real small clusters                                                                                                                                                                                                                 |
| Nodes per cluster                                                          | 2, 10, 100, 200                                      | Real `e2-small` nodes in an autoscaled pool; 64 × 200 is a large quota and cost request                                                                                                                                             |
| Objects per cluster (see below)                                            | 100, 4,000, 10,000                                   | Real objects; Pods run the pause image with no resource requests, so many fit on small nodes (at most 110 Pods per node)                                                                                                            |
| Pods created/deleted per minute                                            | 10, 1,000, 100,000                                   | Across the fleet; 100,000 per minute on one cluster exceeds what its control plane accepts. The watcher sees churn through the Events it produces                                                                                   |
| Warning events per minute across the fleet                                 | 1, 10, 100, 1,000, and one fault on 100 pods at once | Drives session creation and triage fan-out ([#660](https://github.com/gke-labs/kube-agents/issues/660), [#3567](https://github.com/gke-labs/kube-agents/issues/3567), [#3583](https://github.com/gke-labs/kube-agents/issues/3583)) |
| Concurrent requests: user questions while scheduled audits and triages run | 1, 15, 50, 100                                       | Above 10, this measures refusal and queueing at the run slots                                                                                                                                                                       |
| Size of one listing the agent reads (pods in one `kubectl get -A`)         | 100, 1,000, 4,000                                    | Credential proxy child memory ([#2045](https://github.com/gke-labs/kube-agents/issues/2045)); a separate fixture of that many Pods in one namespace, because the object levels hold fewer Pods                                      |

### What "objects per cluster" means

An object is one Kubernetes API resource in a fleet cluster that the harness reads, lists or caches. Two kinds of read load the harness differently:

- **Cached for the life of a process.** The event watcher holds every `core/v1` Event of every watched cluster in an informer cache ([#2648](https://github.com/gke-labs/kube-agents/issues/2648)). Event count follows from the other objects: each Pod start, failure, scheduling attempt and node change writes Events, which the API server keeps for its event TTL (1 hour by default). The operator caches Pods, ConfigMaps, Deployments, Services, Roles and RoleBindings, but only in the cluster where kube-agents runs ([#3493](https://github.com/gke-labs/kube-agents/issues/3493)).
- **Listed on demand.** Agent operations list objects through the credential proxy: Pods, Nodes, Namespaces, Services, Deployments, HPAs, PersistentVolumeClaims and custom resources such as Config Connector resources (233 CRDs on one Config Connector cluster, [#3142](https://github.com/gke-labs/kube-agents/issues/3142)). One listing holds all its objects in memory in the `kubectl` child at once ([#2045](https://github.com/gke-labs/kube-agents/issues/2045)).

Examples, for one application called `checkout` in one fleet cluster:

| Object                    | Example                                                                   | What it is                                                                                                                               |
| ------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                 | `payments`                                                                | A named group of objects. A team's applications usually share one namespace.                                                             |
| Deployment                | `checkout-api`                                                            | Declares that 2 copies of the `checkout-api` container must run, and replaces a copy that stops.                                         |
| Pod                       | `checkout-api-7d9f8c-x2k4p`                                               | One running copy of the container. A Deployment with 2 replicas has 2 Pods; each restart or rollout creates new Pods with new names.     |
| Service                   | `checkout-api`                                                            | A fixed network address that sends traffic to the Deployment's Pods.                                                                     |
| HPA                       | `checkout-api`                                                            | Changes the Deployment's replica count between 2 and 10 from CPU usage.                                                                  |
| ConfigMap                 | `checkout-config`                                                         | Settings that the Pods read, for example a database host name.                                                                           |
| PersistentVolumeClaim     | `orders-data`                                                             | A request for a disk that a Pod mounts.                                                                                                  |
| Node                      | `gke-prod-pool-1-a1b2`                                                    | One VM that runs Pods.                                                                                                                   |
| Event                     | `Back-off restarting failed container` on Pod `checkout-api-7d9f8c-x2k4p` | A record that the cluster writes when something happens to an object. A Pod that crash-loops writes a new Event at each restart attempt. |
| Config Connector resource | `SQLInstance orders-db`                                                   | Declares a Google Cloud resource, here a Cloud SQL instance, as a Kubernetes object.                                                     |

One application in this example is 1 Deployment, 1 Service, 1 HPA, 2 Pods and 1 or 2 ConfigMaps. A cluster with 600 such applications in 100 namespaces is the 4,000 level below.

The experiment builds each level from a fixed mix modelled on the high-cardinality fleet in [#1986](https://github.com/gke-labs/kube-agents/issues/1986), where every cluster replicates its namespaces, Services and HPAs:

| Level  | Namespaces | Deployments (1 Service and 1 HPA each) | Pods (2 per Deployment) | ConfigMaps |
| ------ | ---------- | -------------------------------------- | ----------------------- | ---------- |
| 100    | 5          | 15                                     | 30                      | 20         |
| 4,000  | 100        | 600                                    | 1,200                   | 900        |
| 10,000 | 250        | 1,500                                  | 3,000                   | 2,250      |

The level is the sum of these fixture objects (namespaces + Deployments + Services + HPAs + Pods + ConfigMaps, ignoring ReplicaSets and Endpoints). A GKE cluster holds objects of its own before any fixture: an empty test cluster had 78 of these kinds (17 Pods, 43 ConfigMaps, 8 Services, 10 namespaces). Each run therefore records the cluster's real totals by kind, and the fit uses the totals, not the level. Events are measured, not set: the pod-churn and Warning-event factors drive them.

## Agent Operations

Each operation runs against the factors that load it, not against every factor.

| Operation                                                                                                         | Factors                                            |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| First 2 hours after installation, with the onboarding jobs running in the background                              | Clusters, objects per cluster                      |
| The event watcher, always on                                                                                      | Nodes per cluster, pods per minute, Warning events |
| Run a fleet audit (`fleet-audit`, cost analysis) across every cluster                                             | Clusters, objects per cluster, listing size        |
| Run the Upgrade Readiness Report                                                                                  | Clusters                                           |
| Restart the agent pod while the fleet holds standing Warning events (boot burst)                                  | Warning events                                     |
| Ask "which clusters, if any, are stressed?" (a fan-out)                                                           | Clusters, concurrent requests                      |
| Ask "which clusters do you monitor?" while a fan-out runs                                                         | Concurrent requests                                |
| Run the scheduled jobs: hourly Cluster Agent reconcile, Controller Stall Watch, drift detection                   | Clusters                                           |
| Read every Config Connector resource on one cluster (needs Config Connector installed on a test cluster; Phase 2) | Objects per cluster                                |

# Delivery / Success Criteria

- For each stress point, a table and plot of every metric above against the stress level, with the fitted model
- The breaking point of each metric, against the threshold in the Metrics table
- Visualize the data as a chart in the UI of `scripts/admin_portal.sh`, answering two questions:
  1. **Are these container limits enough for this fleet?** Input: the fleet's stress points (clusters, nodes and objects per cluster, pod churn, Warning events, concurrent requests) and the container limits (the defaults or the install's `PlatformAgent` values). Output: per container, the predicted peak memory and CPU against its limit, with the headroom; the same check for the limits resources do not move (run slots, credential proxy queue, kanban caps, model TPM quota, PVC size, sweep duration); and the first limit the fleet reaches.
  2. **What node size and container limits does this fleet need?** Input: the fleet's stress points. Output: a limit per container (predicted peak plus a safety margin) written as the `PlatformAgent` fields to set; the smallest node, from a list of typical machine types, that fits the pods scheduled on it; the PVC size; and each limit that resources cannot fix, with the setting that moves it or the statement that none does.
- Show proof of the collected data in the UI, in a toggled section

## Decisions

1. **Where the data lives.** The measured data is committed to the repository as `bench/scale/harness-limits.json`: one record per run, with the kube-agents commit, the mode, the date, the factor and its level, the cluster's real object totals, the operation, and each metric's value. A page in the admin console (`scripts/admin_portal.sh`) loads that file, so the chart works without a connected install. A new run merges its records into the file, keyed by commit, mode, factor, level, operation and run number; it replaces only a record with the same key. Records from earlier commits stay, so the page shows the latest commit by default and can compare it with an earlier one.
2. **Safety margin.** Question 2 sets each container limit 30% above its predicted peak. The margin covers three things the fit does not: the spread between the 3 runs at a point, bursts shorter than the sampling interval (the boot burst, a fan-out), and garbage-collector headroom: the event watcher sets `GOMEMLIMIT` to half its container limit, so its heap is collected harder as it approaches that point. Inside the measured range, the 30% is the whole margin. Past the largest measured point, the chart adds the fit's error band on top, and labels the result as predicted, not measured. A point measured once has no spread, so its band is marked unknown.
3. **Node placement.** All kube-agents pods run on one node pool. The smallest node is the one that fits the largest pod's requests; the pool's size follows from the sum of all pods' requests plus the node's system reservation. The node-size answer picks from five machine types. Allocatable memory is what remains after GKE's system reservation and the 100Mi eviction threshold; the values are approximate, from GKE's reservation formula:

   | Machine type     | vCPU | Memory | Allocatable memory (about) | Why it is on the list                      |
   | ---------------- | ---- | ------ | -------------------------- | ------------------------------------------ |
   | `e2-standard-2`  | 2    | 8 GB   | 6.1 GB                     | Smallest size on the list; the lower bound |
   | `e2-standard-4`  | 4    | 16 GB  | 13.3 GB                    | The installer's Standard-mode default      |
   | `e2-standard-8`  | 8    | 32 GB  | 28.3 GB                    | Next step up, same family                  |
   | `n2-standard-16` | 16   | 64 GB  | 58.4 GB                    | Large fleet, CPU-bound fan-outs            |
   | `n2-highmem-16`  | 16   | 128 GB | 118.4 GB                   | Large fleet, memory-bound caches           |

   The experiment's host cluster is Standard, so the node-size answer can be checked. The installer's default is Autopilot, which has no node choice; there, question 2 answers with pod requests only.

4. **Container limits and node size.** A larger node does not raise the container limits by itself. The operator renders defaults (`agent-api-auth` 2Gi, the controller-manager 128Mi), and the `PlatformAgent` CR moves them: `spec.deployment.resources`, `spec.deployment.credentialProxy.resources` and `spec.deployment.agentAPIAuth.resources`; the controller-manager's limit is a chart value. Question 2 therefore derives the node from the limits, not the limits from the node.
5. **Where the experiments run.** A second kube-agents install in the author's dev project, not the dev install itself: the onboarding runs reinstall from scratch, which deletes the install's PVC, memory and profiles. The second install has its own Standard host cluster (`e2-standard-4`), its own service-account names (`PLATFORM_AGENT_GSA_NAME`, `LITELLM_GSA_NAME`, `TF_VAR_agent_ksa_name`), mode `next`, and one pinned, validated release-candidate tag for the whole phase. Its drift detector is off in Phase 1, because the project's drift sink and subscription belong to the dev install. It shares the project's model quota with the dev install, so runs record provider 429s (see Estimating Limits).
6. **The test fleet.** New Standard zonal clusters, labelled `purpose=harness-scale`, created for the experiment and deleted after it. The seeded fleet is not used: its planted failures already produce Warning events, so it cannot hold a controlled base, and eval cases depend on it. Scope is per project, so both installs see every cluster in the project: the experiment install excludes every cluster but the test fleet (`spec.scope.exclude.clusters`), and the dev install excludes the test fleet and the experiment's host cluster.
7. **Safety and the API.** The experiment install has no chat platform and no GitOps repository or GitHub token, so a planted fault cannot post to a channel or open a pull request ([#660](https://github.com/gke-labs/kube-agents/issues/660)). The runner sends every request through the agent API and reads triage results from the API and the kanban board. The alert ceilings stay at their defaults.
8. **Real nodes and Pods.** Every level uses real nodes and Pods. Fixture Pods run the pause image (`registry.k8s.io/pause`) with no resource requests, on an autoscaled pool of `e2-small` nodes, so the cost is mostly nodes: 100 `e2-small` nodes cost about $1.70 per hour. A node holds at most 110 Pods, so a level with many Pods also needs more nodes: 1,200 Pods need at least 11 nodes, and 4,000 need at least 37. The factors therefore do not change fully alone; each run records the cluster's real totals by kind, including nodes, and the fit uses those totals.

9. **Reusable tooling.** The measurement repeats as the codebase changes, so the tooling is a tool, not a one-off script:
   - The plan is data: factors, levels, operations and run counts live in a config file under `bench/scale/`, so a later run changes the config, not the code. Phase 1 and Phase 2 are two configs.
   - The target is a parameter: project, host cluster, test clusters and mode come from flags or the environment; nothing names the author's project.
   - Fixtures are idempotent: each level applies and removes its objects by label, so a level can be re-applied or torn down after a failed run.
   - Each stage runs on its own (create the fleet, apply a level, run an operation, collect, tear down), and a run resumes from the records already in the file.
   - Each record carries the kube-agents commit and image, so results from two commits compare directly.
   - Tests sit beside the code and run on every pull request (a `PYTHON_TEST_DIRS` glob in the `Makefile`); one `make` target with a `## description` starts a run.

## Open questions

1. **Fleet size has more than one dimension.** Memory depends on total watched objects (clusters × objects per cluster), but single-factor experiments vary each factor alone. Plot memory against total watched objects so the two experiments fall on one axis, and confirm with one combined point (for example 8 clusters × 4,000 objects).
2. **Which mode.** `spec.mode: today` and `spec.mode: next` load the harness differently (session pods, the A2A bus, kanban dispatch). The results hold only for the mode measured; each record carries it.

# Phases

The work runs in two phases. Phase 1 takes about 3 days of elapsed time and shows a first result in the admin console. Phase 2 is decided from what Phase 1 shows.

## Phase 1: two data points per factor

Each factor is measured at the base and at one higher level, the one the known issues point at:

| Factor                          | Base         | Second point                  | Why this point                                                                                  |
| ------------------------------- | ------------ | ----------------------------- | ----------------------------------------------------------------------------------------------- |
| Number of clusters              | 2            | 8                             | New zonal test clusters; 64 needs a quota check                                                 |
| Nodes per cluster               | 2            | 100                           | Event and Node volume of a mid-size cluster                                                     |
| Objects per cluster             | 100          | 4,000                         | The high-cardinality fleet in [#1986](https://github.com/gke-labs/kube-agents/issues/1986)      |
| Pods created/deleted per minute | 10           | 1,000                         | Within what one control plane accepts                                                           |
| Warning events                  | 1 per minute | One fault on 100 Pods at once | The fan-out shape in [#660](https://github.com/gke-labs/kube-agents/issues/660)                 |
| Concurrent requests             | 1            | 15                            | Past the 10 Hermes run slots ([#3724](https://github.com/gke-labs/kube-agents/issues/3724))     |
| Size of one listing             | 100 pods     | 4,000 pods                    | The credential proxy child size in [#2045](https://github.com/gke-labs/kube-agents/issues/2045) |

That is 8 points: the base and 7 second points. Two points fit a line but cannot show a knee or a curve, so every Phase 1 prediction is labelled as a two-point estimate.

Phase 1 runs the 2-hour onboarding window and the event watcher soak once per point, and the other operations 3 times. It changes neither the agent nor the operator: triage starts lost and run-slot refusals are counted from log lines and client-side 429s.

| Operation                                              | Points × runs | Time per run | Total                   |
| ------------------------------------------------------ | ------------- | ------------ | ----------------------- |
| First 2 hours after installation, with a fresh install | 3 × 1         | ~2.7 h       | ~8 h                    |
| Event watcher at steady state                          | 4 × 1         | 1 h          | 4 h                     |
| Fleet audit                                            | 4 × 3         | ~30 min      | 6 h                     |
| Fan-out question, with the metadata question during it | 3 × 3         | ~20 min      | 3 h                     |
| Upgrade Readiness Report                               | 2 × 3         | ~20 min      | 2 h                     |
| Boot burst                                             | 2 × 3         | ~15 min      | 1.5 h                   |
| Scheduled jobs                                         | 2             | 3 h          | Overlaps the runs above |
| Level changes                                          | 8             | ~20 min      | 3 h                     |
| **Total**                                              |               |              | **~28 h**               |

Building comes first and takes about 2–3 days, so Phase 1 is about 5 days of elapsed time: 2–3 days to build, then about 28 h of runs, unattended and overnight where possible. Every run records its model token counts per operation.

Steps, in order:

1. The experiment install, its Standard host cluster, the test clusters, and the scope exclusions on both installs (Decisions 5–7).
2. The fixtures and runner (`bench/kube_agents_bench/scale/`, with the Phase 1 plan in `bench/scale/phase1.yaml`), built to Decision 9, and their tests: the object mix for the 4,000 level, the node-pool scaling, the churn and burst generators, the 4,000-pod listing, and a loop that runs the operations and merges records into `bench/scale/harness-limits.json`.
3. The base run.
4. The other runs.
5. An admin console page that loads `bench/scale/harness-limits.json` and shows, per metric, the two points against the metric's threshold, with the raw data in a toggled section. It answers the two sizing questions from the two-point estimates.

## Phase 2: decided from the Phase 1 result

Phase 1's chart decides what follows. The candidates:

- The remaining planned levels, for the metrics whose line comes close to a threshold inside realistic fleet sizes.
- More points near a threshold, to find the knee for the queue metrics.
- The 64-cluster level, after a quota check (cluster management costs about $0.10 per cluster-hour).
- The combined point (8 clusters × 4,000 objects).
- 3 runs for the onboarding window and the watcher soak.
- The Config Connector read, on a test cluster with Config Connector installed.
- New stress points the Phase 1 data suggests.
- The instrumentation: a counter for triage starts lost ([#3583](https://github.com/gke-labs/kube-agents/issues/3583)) and a gauge for occupied Hermes run slots ([#3724](https://github.com/gke-labs/kube-agents/issues/3724)).
- An issue for each limit the data shows below a realistic fleet size, linked from the Known Issues table.
- A recurring run: the Phase 1 config on each release candidate, with the page flagging a metric that moved past a set fraction of the previous release.

All planned points at 3 runs each would take about 95–115 h of machine time, 1.5–2 weeks of calendar time with re-runs.

# Out of scope

For now we won't cover these topics, which are limitations of this research.

## Breaking points

Some issues only happen after a timing threshold is crossed, for example a retry storm after a timeout. We do not search for them, but a run that shows one is reported: retry loops caused known OOMKills ([#2652](https://github.com/gke-labs/kube-agents/issues/2652), [#1333](https://github.com/gke-labs/kube-agents/issues/1333)).

We do aim to find hard breaking points such as OOMKills, by comparing the predicted memory with the container limit.

## Correlated factors

Some stress points may amplify each other, for example cluster count and concurrent audit requests. Each experiment changes one factor, so these interactions are not measured, apart from the one combined point that checks the total-objects axis.

# Appendix

## Known Issues

Each row is a GitHub issue that reports a scale bottleneck with evidence. "Scale" is the fleet size where the reporter saw the bottleneck.

### Memory: unbounded caches against fixed limits

| Issue                                                        | Bottleneck                                                                                | Scale                                                               | Evidence                                                                                                     |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| [#2648](https://github.com/gke-labs/kube-agents/issues/2648) | Event watcher informers cache every Event of every cluster, with no selector or transform | 26 clusters, 4 at 1,600–1,950 nodes                                 | `agent-api-auth` goes from 440Mi to over 2Gi within minutes; 8+ restarts in 30 min                           |
| [#1333](https://github.com/gke-labs/kube-agents/issues/1333) | Forbidden Event informers retry forever on every cluster                                  | 26 clusters                                                         | OOM at the 2Gi limit; each restart resets profile directory permissions and blocks multi-cluster tasks       |
| [#1986](https://github.com/gke-labs/kube-agents/issues/1986) | Platform Agent OOM on high-cardinality clusters                                           | 39 clusters, ~2M cores, ~4,000 services/namespaces/HPAs per cluster | OOM "every few seconds" after 2–3 clusters; object count per cluster matters more than cluster count         |
| [#2018](https://github.com/gke-labs/kube-agents/issues/2018) | The watcher embedded in the Python credential proxy grows ~100Mi/min                      | same fleet as #1986                                                 | 6 OOMKills in 50 min against a 1Gi limit (2Gi before); the workaround was to disable the event watcher       |
| [#3493](https://github.com/gke-labs/kube-agents/issues/3493) | The operator cache is unscoped (every Pod, ConfigMap, Role) under a 128Mi limit           | 15- and 89-node clusters                                            | Repeated controller-manager restarts while 1–3 node installs stay clean; mechanism confirmed, OOM not proven |
| [#1934](https://github.com/gke-labs/kube-agents/issues/1934) | The drift detector has no `GOMEMLIMIT` and shares a container with the watcher            | —                                                                   | Missing guard; no observed failure                                                                           |

### Credential proxy: one broker for every command

| Issue                                                        | Bottleneck                                                                                                         | Scale                                   | Evidence                                                                                                                                    |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| [#2324](https://github.com/gke-labs/kube-agents/issues/2324) | 8 concurrent `gcloud` children exceed the broker's memory limit                                                    | 143 clusters, 36 projects               | The Stall Watch cron fans out 8 at once; `memory.peak` 948MB against 1Gi                                                                    |
| [#2045](https://github.com/gke-labs/kube-agents/issues/2045) | `kubectl` children are capped by count, not by size                                                                | 4,000 pods                              | One `-o yaml` listing takes 1,281 MiB; the cgroup v2 group OOM killer stops the whole container                                             |
| [#2652](https://github.com/gke-labs/kube-agents/issues/2652) | An audit retries large-cluster reads into the proxy it crashed; `collect.py` runs 8-wide against 4 admission slots | 7-cluster audit on the 26-cluster fleet | Proxy at 827Mi, 6+ OOMKills; the audit never finishes                                                                                       |
| [#2637](https://github.com/gke-labs/kube-agents/issues/2637) | One replica handles all brokered execs; the admission budget turns the OOM into a queue                            | 143 clusters                            | ~6,000 execs/hour at the CPU limit; up to 1,225 queued requests, p50 wait 22.5s, max 56.7s; the broker refuses at 60s, the preflight at 15s |
| [#3142](https://github.com/gke-labs/kube-agents/issues/3142) | The agent reads CRDs one at a time through the broker                                                              | 233 Config Connector CRDs               | 769s at 3.3s per RPC; batched reads take 30s                                                                                                |

### Event volume: fan-out per pod, not per fault

| Issue                                                                                                                    | Bottleneck                                                                | Scale                 | Evidence                                                                              |
| ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------- |
| [#660](https://github.com/gke-labs/kube-agents/issues/660), [#2512](https://github.com/gke-labs/kube-agents/issues/2512) | The dedup key is `(pod UID, reason)`, so a fault on N pods is N incidents | one registry-wide 429 | 60 sessions, 39 cards, 36 PRs (35 auto-merged), although dedup suppressed 482 repeats |
| [#3567](https://github.com/gke-labs/kube-agents/issues/3567)                                                             | The watcher opens a session before the inject decision                    | seeded fleet          | 101,492 session creates against 15 injected events, ~7 creates/min                    |
| [#3272](https://github.com/gke-labs/kube-agents/issues/3272)                                                             | No per-cluster or per-namespace alert controls                            | seeded fleet, 1 hour  | 27 triage cards and 20 alerts; triage holds 4 of 5 slots with 14 queued               |
| [#3192](https://github.com/gke-labs/kube-agents/issues/3192)                                                             | A Pub/Sub pull over the 8 MiB relay cap is redelivered forever            | audit-log bursts      | The batch is never acknowledged (not confirmed)                                       |

### Concurrency: slots, queues and bursts

| Issue                                                                                                                      | Bottleneck                                                                                    | Scale                  | Evidence                                                                                              |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------- |
| [#3724](https://github.com/gke-labs/kube-agents/issues/3724), [#1097](https://github.com/gke-labs/kube-agents/issues/1097) | The watcher's boot burst fills Hermes's 10 run slots; the triage route does not check the cap | 5 clusters             | User turns get `429 Too many concurrent runs` seconds after a pod restart                             |
| [#3583](https://github.com/gke-labs/kube-agents/issues/3583)                                                               | A triage start makes 1 attempt with a 10s timeout and is lost silently on failure             | bursts of 16–21 alerts | 6 of 16 and 10 of 21 triages lost; the ledger still records them as delivered                         |
| [#3111](https://github.com/gke-labs/kube-agents/issues/3111), [#2032](https://github.com/gke-labs/kube-agents/issues/2032) | Kanban runs 1 task per profile at a time; the global `max_in_progress` defaulted to 2         | 8 clusters             | 4 alerts on one cluster take >35 min in series; an 8-cluster node count takes 8–10 min                |
| [#3299](https://github.com/gke-labs/kube-agents/issues/3299), [#3310](https://github.com/gke-labs/kube-agents/issues/3310) | Background audits starve interactive queries; fan-out cards stall in `ready`                  | 5 clusters             | "List clusters" queues 5+ min and becomes a 15–20 min fan-out; 5 cards sit in `ready` for 40 min      |
| [#3286](https://github.com/gke-labs/kube-agents/issues/3286)                                                               | A multi-cluster fan-out adds a parent LLM turn before the cluster cards                       | 3-cluster prompts      | Time to first card 33–41s, against 13–28s for a single cluster                                        |
| [#3220](https://github.com/gke-labs/kube-agents/issues/3220)                                                               | A fixed 600s session wall clock                                                               | production traces      | 8–9% of gateway and triage runs abort; deep skills need 19–22 turns at ~15s each                      |
| [#3388](https://github.com/gke-labs/kube-agents/issues/3388), [#1214](https://github.com/gke-labs/kube-agents/issues/1214) | Fan-out uses up the model quota (TPM)                                                         | fresh install, eval    | A new install uses up its own Vertex TPM quota; 429 storms push runs to the 45-min delegation timeout |

### Linear per-cluster cost: disk and sweeps

| Issue                                                        | Bottleneck                                                                 | Scale                     | Evidence                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#3325](https://github.com/gke-labs/kube-agents/issues/3325) | Per-profile files fill the fixed 10 GiB data PVC, and nothing detects it   | 209 clusters, 42 projects | PVC full 36 min after a fleet health check; kanban writes fail with `disk I/O error` while the CR stays `Ready`; per-profile tirith copies took 6.5 GiB and npm caches ~100 MiB each ([#3344](https://github.com/gke-labs/kube-agents/pull/3344), [#3610](https://github.com/gke-labs/kube-agents/pull/3610)) |
| [#3704](https://github.com/gke-labs/kube-agents/issues/3704) | Nothing prunes a profile's `state.db` or logs                              | 202 profiles              | An active profile reaches 16.5 MB in 2 weeks, so a 50 GiB volume holds ~3,200 clusters, and fewer over time                                                                                                                                                                                                   |
| [#3703](https://github.com/gke-labs/kube-agents/issues/3703) | The hourly reconcile runs one `describe` per cluster on shared sandbox CPU | 202 profiles              | 484s at 2.4s per profile; the sweep overruns the hour at ~1,500 clusters                                                                                                                                                                                                                                      |
| [#1768](https://github.com/gke-labs/kube-agents/issues/1768) | One slow cluster uses up the drift detector's shared batch join budget     | multi-cluster             | 6 slow records exhaust a 60s budget and leave healthy clusters unenriched (not reproduced)                                                                                                                                                                                                                    |

### Measurement and customer reports

- [#2348](https://github.com/gke-labs/kube-agents/issues/2348): scheduled tests cover 4 small clusters. The proposal is a nightly run over ~140 pool clusters plus one high-cardinality cluster.
- [#1913](https://github.com/gke-labs/kube-agents/issues/1913): one Platform Agent with 200 cluster profiles. It measures cluster count, not object count per cluster.
- Customer reports: [#2596](https://github.com/gke-labs/kube-agents/issues/2596) (39 clusters, ~2M cores), [#2599](https://github.com/gke-labs/kube-agents/issues/2599) (20 clusters, 2,800 namespaces), [#2453](https://github.com/gke-labs/kube-agents/issues/2453) (credential proxy memory).
- [#3140](https://github.com/gke-labs/kube-agents/issues/3140): agent response latency gates (<10s great, <30s OK, >60s investigate).
