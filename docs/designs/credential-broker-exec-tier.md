# The Credential Broker Scales Out: an Exec Tier Beside One Stateful Broker

> **STATUS — Draft for review. Measurements pending (§8); the sweep load is blocked by #3933.**
> Nothing is built. This document proposes how the credential broker grows past one pod. It is
> meant to replace [`credential-proxy-single-pod.md`](credential-proxy-single-pod.md) once the
> measurements in §8 exist and the design is reviewed. Issue: #4082. Partner: the sizing tiger
> team (#3753).

**Scope:** how many broker pods an install runs, which routes run where, how callers reach them,
how admission orders interactive and background work, and the contract a fleet cache must meet
to take reads off the broker.
**Owns:** the exec tier, the stateful broker's remaining role, priority admission, the cache
contract, and the recommendation on per-profile or per-project brokers.
**Does not own:** the child memory budget and slot cap of one pod
([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md)); the
fleet cache itself; sweep width (#4081, #4084); per-task worker credentials (#3889).

Number labels used in this document:

- **[measured]**: from a run, with its source.
- **[code]**: read from `main` (file and line).
- **[predicted]**: arithmetic from a stated assumption. It is not a measurement.

## 1. The shape in one picture

**The one change:** commands (`kubectl`, `gcloud`, the `/v1/exec` route) move out of the single
broker pod into a new exec tier that can run N copies. Everything else stays on today's pod,
including the Cloud API relay (`/v1/gcp/`) as a first step (§3.1, open question 4).

Both pictures use the same layout. Callers are on the left; the broker pods are on the right.

```
TODAY: one pod does everything

  commands (all callers)  ───────────┐
    interactive shell                │
    session pods                     │      ┌───────────────────────────────────┐
    sweeps, reconcile, stall watch   ├────► │  credential broker   × 1 pod      │
                                     │      │   exec: 8 slots, one FIFO queue   │
  chat relays, workspace,  ──────────┘      │   relays, workspace, vcs/forge,   │
  vcs/forge, watchers                       │   watchers                        │
                                            └───────────────────────────────────┘
                                              ▲ the 8 slots and 1 CPU are shared by
                                                every caller, with no priority


PROPOSED: commands get their own tier

  commands (all callers)  ───────────┐      ┌───────────────────────────────────┐
    interactive shell   [interactive]│      │  NEW  exec tier   × N pods        │
    session pods        [interactive]├────► │   exec only: 8 slots per pod      │
    sweeps, reconcile,  [background] │      │   background ≤ admitted − 2       │
    stall watch                      │      └───────────────────────────────────┘
                                                 via NEW CREDENTIAL_PROXY_EXEC_URL

  chat relays, workspace,  ─────────────────► ┌───────────────────────────────────┐
  vcs/forge, watchers                         │  credential broker   × 1 pod      │
                                              │   relays, workspace, vcs/forge,   │
                                              │   watchers  (exec removed)        │
                                              └───────────────────────────────────┘
                                                 via CREDENTIAL_PROXY_URL (unchanged)
```

What changes, side by side:

|                                             | Today                                        | Proposed                                                                        |
| ------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------- |
| Who runs commands                           | The one broker pod                           | **NEW:** the exec tier, N pods                                                  |
| Command capacity                            | 8 slots in total                             | **8 slots × N**                                                                 |
| Priority                                    | None: one first-in, first-out queue          | **NEW:** 2 classes; background work holds at most 2 fewer than a pod admits     |
| Chat relays, workspace, vcs/forge, watchers | The one broker pod                           | Same pod, unchanged                                                             |
| Cloud API relay (`/v1/gcp/`)                | The one broker pod                           | Same pod as a first step; it takes no slot and can move later (open question 4) |
| How callers reach commands                  | `CREDENTIAL_PROXY_URL`                       | **NEW:** `CREDENTIAL_PROXY_EXEC_URL`; if it is not set, as today                |
| Upgrade of the command path                 | `Recreate`: commands stop during the restart | `RollingUpdate`: no outage, if open question 1 holds                            |
| Identity and permissions                    | One KSA / GSA                                | Same KSA / GSA, no new IAM                                                      |
| Code                                        | `credential_proxy.py`                        | Same file and image, with `CREDENTIAL_PROXY_ROLE=exec` on the new pods          |

Later, with a fleet cache (§6, contract only in this round): fleet-wide sweeps read the cache
first, and fall back to the exec tier when the cache is stale or missing.

The rule in one line: **the relays and the content workspace stay on one pod, exec scales out,
and each caller routes to the right one.**

## 2. The current shape and its limits

### 2.1 What one pod admits [code]

- Slot cap: `DEFAULT_MAX_CONCURRENT_COMMANDS = 8` (`credential_proxy.py:261`). The operator pins
  it, and a CR cannot override it (`platformagent_manifests.go:82`).
- Child memory budget: (limit − 320 MiB) / 176 MiB per request. That gives 9 at the 2 GiB default
  and 4 on Autopilot without bursting (limit clamped to the 1 GiB request). The smaller of the
  two numbers binds.
- Session role: its own cap of 2, refused at once and not queued (`credential_proxy.py:322`).
- Admission is one arrival-order queue with a 60 s timeout, then `503 CREDENTIAL_PROXY_BUSY`
  (`_admit`, `credential_proxy.py:6354`).
- **No priority.** A caller authenticates with a projected ServiceAccount token. The broker
  learns the workload and a role (shell, chat, a2a-chat, session, watcher, mcp) from it. The
  `caller` field of `Principal` is always `None` (`credential_proxy.py:2182`). Background work
  reaches the broker as the shell role, the same as an interactive command:
  - the fleet audits are agent cron jobs (`agents/platform/cron/jobs.json`, skill `fleet-audit`).
    The model runs their collectors in the sandbox shell, and they call `kubectl` and `gcloud`
    through the shim and the Cloud API relay through `ApiSession`;
  - the no-agent watches (stall watch, service-account role watch, upgrade readiness watch) and
    the hourly reconcile run through `sandbox_exec` and then the shim. `sandbox_exec` also runs
    the platform MCP server's interactive tools (`platform_mcp_server.py:590`).

  **The broker cannot tell interactive work from background work today.**

### 2.2 What blocks more replicas [single-pod doc §2 and §3, 2026-10-08 spike]

| Route             | State                                                             | Two replicas behind one Service                                                                |
| ----------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Exec (`/v1/exec`) | Caches only: managed kubeconfigs, gcloud config                   | [measured] Works: 24 sandbox calls succeeded; all shell exec traffic in the window split 69/51 |
| Google Chat relay | `receipt → ack_id` in the process                                 | [measured] Ack reached the wrong pod about half the time                                       |
| Slack relay       | One Socket Mode connection per pod, in-process queue and receipts | Not run; breaks by construction (§2 there)                                                     |
| Content workspace | Handles in the pod's emptyDir                                     | Not run; breaks by construction (`NoSuchHandle`)                                               |

### 2.3 What the load looks like [measured, #3935 Phase 1]

Source: `bench/scale/harness-limits.json` on #3935, 46 runs, `spec.mode: next`, image
`eeb8a95c`, Standard install, broker at 1 CPU and 2 GiB, 2 to 8 test clusters.

- **CPU was the first limit.** Broker CPU peaked at 1.05 cores (onboarding, 8 clusters), 1.06
  and 0.94 (upgrade readiness, 8 clusters) and 0.90 (fan-out, 8 clusters), against a 1-core
  limit. The broker's memory was at most 712 MiB of 2 GiB.
- **Slot waits were rare.** One run of 46 had an admission wait above 0.1 s: onboarding at 8
  clusters, 10 to 15 s (a histogram bucket). The upgrade readiness run at 1.06 cores had no wait.
  No run had a refusal.
- **The sweep load is not measured.** The fleet audits at 8 clusters cancelled or were declined
  (#3933, and no GitOps repository), and broker CPU in those runs was 0.01.
- **Interactive bursts hit the agent first.** With 15 questions at once, the agent's 10 run slots
  refused 5 (#3724), while the broker wait stayed at 0.1 s or less.

Phases 2 and 3 (#4107) measured the same install on 2 to 22
test clusters, with the broker's limits raised to 4 CPU and 4 GiB at 2 and 16 clusters. As that
pull request reports them [measured, #4107]: the broker is the first bottleneck; with the
first-run audits running together it reaches its 1-core limit at every fleet size; at 16 clusters
it needs about 2.5 cores; and its memory line reaches the 2 GiB limit at about 15 clusters. It
also found that the sandbox caps the brokered command path at about 1.5 commands per second
(#4027), on the caller side, before the broker. §8 uses #4107's runner features
(`proxy_load`, raised limits).

So the single-pod doc's reopen condition (§6 there: reopen when either criterion holds) is met:
one pod's CPU saturates at 8 test clusters. The other criterion, a median wait over 10 s with
refusals, was not seen. The product target
is 1,000 to 2,500 clusters. One pod cannot get there vertically: the slot cap is fixed at 8, and
one pod's CPU is one limit.

## 3. The exec tier

### 3.1 Which routes move

| Route                                                       | Where                        | Why                                                                          |
| ----------------------------------------------------------- | ---------------------------- | ---------------------------------------------------------------------------- |
| `POST /v1/exec`                                             | Exec tier                    | No state across calls if open question 1 holds; most of the slot-taking load |
| `GET /v1/exec/admission` (#4084)                            | Exec tier                    | Reports the tier's capacity (§3.5)                                           |
| `/v1/gcp/` Cloud API relay                                  | Stateful broker (first step) | Takes no slot; can move later                                                |
| `/v1/vcs/<verb>`, `/v1/forge/refresh`, `/v1/github/refresh` | Stateful broker              | Keeps one forge-token coalesce cache, so GitHub mints do not multiply by N   |
| `/v1/workspace/*`                                           | Stateful broker              | Handles live in one emptyDir                                                 |
| `/v1/chat/*`, `/v1/mcp/*`, `/v1/pubsub/*`                   | Stateful broker              | In-process receipts, one Socket Mode connection                              |
| `/v1/watcher/*`, `/v1/k8s/*`, `/v1/feedback/submit`         | Stateful broker              | Unchanged; small load                                                        |

The stateful broker keeps a slot cap and an admission queue: `/v1/vcs/*` and
`/v1/feedback/submit` take slots there. The fleet audits also load the Cloud API relay, which
stays on the stateful pod as a first step. So "most of the load moves" is a claim for §8 to
check, because CPU, not slots, was the first limit (§2.3, open question 4).

The exec tier runs the same image and the same `credential_proxy.py`. `CREDENTIAL_PROXY_ROLE`
exists today (`combined`, `broker`, `api-proxy`; `resolve_role()` in `credential_proxy.py`), and
the stateful pod runs `broker`. Today the value selects which processes start. The design adds
`exec`, which serves only the two exec routes and answers 404 to the rest. This keeps one code
base and makes a wrong route fail loudly.

### 3.2 How each caller routes

| Caller                                | Today                                                                                          | Proposed                                                                                           |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Sandbox shim, `execute()`             | `CREDENTIAL_PROXY_URL`                                                                         | `CREDENTIAL_PROXY_EXEC_URL` when set, else `CREDENTIAL_PROXY_URL`                                  |
| Sandbox shim, `Workspace`, vcs, forge | `CREDENTIAL_PROXY_URL`                                                                         | Unchanged                                                                                          |
| Session pods                          | `CREDENTIAL_PROXY_URL`, from the gateway's `A2A_CREDENTIAL_PROXY_URL` (`a2a/gateway/spawn.go`) | Also get `CREDENTIAL_PROXY_EXEC_URL`, through a new `A2A_CREDENTIAL_PROXY_EXEC_URL` on the gateway |
| Fleet audits (agent cron jobs)        | The model's shell, then the shim                                                               | Same path; the shim adds the class from the cron context (§4)                                      |
| Watches, reconcile                    | Through `sandbox_exec` and the shim                                                            | Same path; each caller sets the class (§4)                                                         |
| Gateway relays, watchers              | Their own URL variables                                                                        | Unchanged                                                                                          |

The fallback means an exec tier that is off, or a new shim against an old operator, behaves as
today.

### 3.3 Deployment

- A second Deployment, `<agent>-credential-proxy-exec`, and Service, port 8765. Replicas from a new
  CR field (§9). Strategy `RollingUpdate` if open question 1 confirms that no state crosses
  calls. This also removes the exec outage during an upgrade.
- Same KSA, so the same Workload Identity GSA and the same grants. No new IAM.
- Same resources per replica as today (requests 500m CPU / 1 GiB, limits 1 CPU / 2 GiB [code,
  `credential_proxy_manifests.go:140-149`]).
  The slot cap and the memory budget stay per replica.
- A NetworkPolicy target for the exec tier: ingress on 8765 from the sandbox and session pods, and
  the metrics port from GMP. The egress rules that select the broker's pod labels also need the
  exec tier as a peer: the sandbox (`shell_sandbox_manifests.go`), session pods
  (`platformagent_a2a_manifests.go`) and the agent (`platformagent_egress_policy.go`). The
  stateful broker's policy drops session pods if they no longer need it.
- The chart's footprint and the quota preflight add N × the exec replica's requests.

### 3.4 Identity and tokens per replica

- **Google access tokens:** each replica gets its own from the metadata server through Workload
  Identity. No coordination is needed.
- **Managed kubeconfigs:** each replica fills its own cache, and keeps a kubeconfig until the pod
  restarts (`_ensure_managed_kubeconfig`, `credential_proxy.py:7878`). A cluster costs up to N−1
  extra cold `get-credentials` per pod lifetime. The `get-credentials` calls that callers send
  each land on one replica, so they do not multiply. The peak is just after a rollout: about
  clusters × N cold calls in a burst. [predicted] At 2,500 clusters and 4 replicas that is about
  10,000 `get-credentials` after each rollout. Open question 5: build the kubeconfig from the
  cluster's endpoint and CA without `gcloud`, or stagger the rollout.
- **TokenReview cache:** per replica; one extra review per caller per replica. Small.
- **Forge tokens:** not on the exec tier (§3.1), so mints do not multiply.

### 3.5 Capacity the tier reports

The admission route `GET /v1/exec/admission` (#4084) reports what one pod admits, and the sweeps
size their pools from it. Behind a Service,
one replica answers. With N replicas it must report the tier, or the sweeps under-read by N. The
operator sets `CREDENTIAL_PROXY_EXEC_REPLICAS`, and the route reports
`admitted × replicas` with the per-replica numbers beside it. It is a target, not a promise: a
replica that is not ready lowers the real number until the rollout ends.

## 4. Priority admission

### 4.1 The class

Each exec request carries a class: `interactive` or `background`.

- The shim sends a header, `X-Broker-Class`. It sets `background` when it runs inside a cron job:
  the Hermes cron run exports a variable that the shim reads (to be defined). The no-agent
  watches and the reconcile set `background` per caller, not inside `sandbox_exec`, because
  `sandbox_exec` also runs the MCP server's interactive tools. A command with no class is
  `interactive`.
- Session pods are `interactive` by role. The broker derives it from the token, not the header.
- The header is a hint inside one trust domain, not a security control. A shell command that
  claims `interactive` can only use the interactive share (§4.2). It gains no access.

### 4.2 The rule, per replica

- Background work may hold at most `B = admitted − R` of a replica's admissions, where `admitted`
  is the smaller of the slot cap and what the memory budget admits. The proposal is `R = 2`:
  6 of 8 at the 2 GiB default, 2 of 4 on Autopilot at 1 GiB. The reserve is budget room as well
  as slots, so a background child cannot take the memory an interactive request needs. This is
  like the session cap of 2 today, but it reserves capacity instead of only limiting it.
- Interactive work may use any free slot.
- Inside a class the order stays arrival order. Interactive requests go ahead of queued background
  requests. Interactive work has no limit of its own, so a long interactive burst can hold
  background work until its 60 s admission timeout (`COMMAND_SLOT_WAIT_SECONDS`) and it gets 503
  (§10).
- The memory budget applies to both classes as it does today.
- The wait histogram and the refusal counter get a `class` label.

### 4.3 Across replicas

There is no shared queue. Each replica runs §4.2 on its own share of the traffic.

- The shim opens one connection per call, and kube-proxy balances per connection [measured, spike:
  69/51 over two pods]. So each replica gets about 1/N of each class.
- New: the shim retries a `503 CREDENTIAL_PROXY_BUSY` once. The retry is a new connection, so it
  usually reaches another replica. It does not retry a dropped connection
  (`BrokerDisconnected`), because the command may have run.
- Rejected for now: a central queue in front of the tier (a new component, and a new single point
  of failure), and a shared slot count in a store (a network round trip on every admission).
  Reopen them if §8 shows uneven load across replicas.

## 5. Audit as one stream

Every replica writes the same JSON lines (`tool_execution_audit`) to stdout, and the GKE log
agent collects them [code, `configure_logging` in `credential_proxy.py`]. Each line already
carries the principal and the pod. The design adds:

- `broker_tier` (`exec` or `stateful`) and the class;
- the shim's existing `requestId`, reused on a retry, so a retry on another replica is one request
  in the log.

"One stream" means one log name, one schema and one query across all pods. No shared file is
needed. The audit docs and the log query in the site's security page list both tiers.

## 6. The cache contract (option 3)

The fleet cache does not exist yet. This design does not wait for it. It writes down what the
cache must give, so the broker design stays valid whether the cache lands soon or later.

| Read                                                        | Moves to the cache | Freshness the caller needs                                     | Notes                          |
| ----------------------------------------------------------- | ------------------ | -------------------------------------------------------------- | ------------------------------ |
| Clusters, node pools, versions, locations                   | Yes                | Minutes (change feed: Cloud Asset Inventory or Admin Activity) | Sweeps, inventory questions    |
| Workload metadata (namespaces, Deployments, images, labels) | Yes                | Minutes to an hour                                             | Sweeps; not Pod status         |
| Pod status, events, logs, `describe`                        | No                 | Live                                                           | Drill-downs and triage         |
| Anything a write or a remediation depends on                | No                 | Live                                                           | Always read live before acting |

What a reader does:

- The cache answers with the data and its age.
- If the age is past the read's freshness, or the cache has no entry, the reader does a
  **bounded live read through the exec tier**, as `background` class, for the clusters in
  question only.
- A sweep may not fall back to a live read of the whole fleet. If more than a set share of
  clusters is stale, the sweep reports "cache stale" instead.
- The cache fills itself through the exec tier or its own feeds. Its live reads count as
  `background`.

## 7. Option 2: brokers per profile or per project

**Recommendation: do not build per-profile or per-project brokers for throughput.** The exec tier
gives throughput. A broker per profile multiplies the footprint and the caches by the profile
count, and changes the operator, the NetworkPolicy and the audit.

**For isolation, enforce scope inside the exec tier.**

- Once #3889 gives each task a signed statement of its target, the exec tier checks each request:
  the target cluster or project is inside the task's scope.
- This gives confinement per task without a broker per profile.
- Design the exec tier so it can run as **one pool per credential tier** (Read, Unattended), each
  with its own KSA and GSA, when a tier stronger than Read gets its own credentials. That is a
  second exec Deployment, not one per profile. This needs a decision when #3659's tier CRs land.

## 8. Measurement plan

The #3935 runner is the tool. The tiger team owns it, so the plan is a new plan file,
`bench/scale/broker.yaml`, run as part of #3753 Phase 2. This document owns what it measures.

### 8.1 Metrics to add

| Metric                                                                                                 | Source                                                                                | Status                                            |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Slots in use (peak) and admitted                                                                       | `kubeagents_credential_proxy_slots_in_use` gauge [code]; `/v1/exec/admission` (#4084) | The gauge exists. The runner does not read it yet |
| Wait p50 and p95, refusals, **by class**                                                               | Wait histogram with a new `class` label                                               | Needs §4 or a temporary label                     |
| Traffic mix: requests and slot-seconds by caller (sweep, reconcile, stall watch, interactive, session) | Audit lines with the class and a caller name from the shim                            | New                                               |
| CPU throttling                                                                                         | cAdvisor CFS throttled periods through GMP                                            | New in the runner                                 |
| Per-replica balance                                                                                    | The same metrics per pod                                                              | New in the runner                                 |

### 8.2 Points

| Run set           | Points                                                                | Why                                                         |
| ----------------- | --------------------------------------------------------------------- | ----------------------------------------------------------- |
| Baseline, one pod | 2, 8, 16, 32 clusters, with a fleet audit that really runs            | Where waits and refusals start, and the sweep's share       |
| Mixed concurrency | 8, 10, 12, 15 interactive questions during a sweep                    | Interactive waits behind background work, today and with §4 |
| Vertical control  | Broker CPU raised, at 8 and 16 clusters (#4107 ran 4 CPU at 2 and 16) | CPU-bound or slot-bound                                     |
| Autopilot         | One point at the 1 GiB clamp (budget admits 4)                        | The worst default                                           |
| Mode              | One baseline point in `spec.mode: today`                              | Phase 1 covered `next` only                                 |
| Exec tier         | 1, 2 and 3 replicas at 16 and 32 clusters                             | #4082 "Done when": the chosen shape at two or more sizes    |

Each point runs 3 times. The planned points are only a few times the Phase 1 run time [predicted],
because most points reuse the fan-out and audit operations.

### 8.3 The #3933 blocker

The sweep is the load this design is about, and Phase 1 could not measure it:

- At 8 clusters, the fleet audit cancelled in 2 of 3 runs. It claimed every managed cluster was
  excluded. This is the #3933 bug: it fires when the number of excluded clusters equals the number
  of managed clusters. The experiment install excludes every cluster in the project except the
  test fleet, so the counts can match.
- In the third run, the audit was declined because the install has no GitOps repository.

Before the baseline runs, one of these must hold: #3933 is fixed, or the experiment runs in a
project where the install's scope needs no exclusions. The audit also needs a path that runs
without a GitOps repository, or a repository whose writes are blocked. Without this, the baseline
measures everything except the sweep.

### 8.4 Footprint at 250 / 1,000 / 2,500 clusters

The doc must give this, so the formula is fixed now, and the numbers come from §8.2:

```
  exec replicas = max( ceil( background demand / B ),
                       ceil( all exec demand / admitted per replica ) )
  peak demand   = sweep slots (clusters × slot-minutes per cluster / sweep window)
                + interactive slots + reconcile and stall-watch slots
```

[predicted, for scale only] #4081 quotes a code comment: a sweep of about 16 clusters takes about
30 minutes at 4 wide. That is about 7.5 slot-minutes per cluster. A 1,000-cluster sweep in one
hour would then need about 125 background slots, which is 21 replicas at B = 6. If the cache
takes 80% of the sweep's reads (an assumption), that becomes about 25 slots, or 5 replicas. This is why the
design needs both the exec tier and the cache contract. §8.2 replaces these numbers with
measured ones.

## 9. Rollout

1. **Off by default.** New CR field `spec.deployment.credentialProxy.exec.replicas`. The value 0
   (the default) means no exec tier and no `CREDENTIAL_PROXY_EXEC_URL`, so every install behaves
   as today.
2. **Class header and per-class metrics first,** on the single pod. They are useful alone. They
   give the traffic mix (§8.1) before anything scales.
3. **Exec tier on the eval install and the experiment install,** at 2 replicas. Run §8.2.
4. **Default on** at a replica count from the measurements, with the chart footprint updated.
5. **Then** mark `credential-proxy-single-pod.md` superseded. It already points here.

To revert, set replicas to 0. The shim falls back to `CREDENTIAL_PROXY_URL`.

## 10. Risks

- **Uneven load.** Per-connection balancing is not per-load balancing. Long `kubectl` reads on one
  replica can fill it while another is idle. The retry in §4.3 and the per-replica metrics in §8.1
  are the checks.
- **Caches filled N times.** More cold `get-credentials` (§3.4) and more TokenReviews.
- **Footprint.** Each replica adds 500m CPU and 1 GiB to the requests. On small installs, 0
  replicas stays the default until a fleet needs it.
- **A wrong class.** A caller that sends `background` for interactive work waits longer. A caller
  that claims `interactive` uses only the reserved share. Neither gains access.
- **Two addresses.** A caller that uses the wrong URL gets a 404 from the exec role, which is loud
  and easy to find in tests.
- **Background starvation.** Interactive work goes first with no limit of its own, so a long
  interactive burst can push queued sweep commands past the 60 s timeout. §8.2's mixed-concurrency
  runs show whether an aging rule or an interactive cap is needed.

## 11. Open questions

1. Does any `/v1/exec` call refer to a content-workspace handle or a path in the stateful pod's
   emptyDir? If yes, that call must stay on the stateful broker. (The code survey found the
   workspace and the exec route on the same emptyDir; this needs a check per call site.)
2. Is a reserve of `R = 2` right? The measured mix (§8.1) decides it.
3. Do the dispatcher's profile workers reach the broker through the sandbox shim as the shell role?
   This was not checked.
4. Should the Cloud API relay move to the exec tier? It takes no slot today, and the measurements
   show whether it matters.
5. Kubeconfig without `gcloud` (§3.4): worth it at 2,500 clusters?
6. Who owns `bench/scale/broker.yaml`: the tiger team, as part of Phase 2, with this doc as its
   requirements? To agree with them before §8 starts.
7. #3760 (open) adds a k8s-api-proxy and routes the sandbox's `kubectl` through it for the root
   and `platform` profiles, off `/v1/exec`. If it merges, the exec tier carries `gcloud` and the
   remaining `kubectl`, and the k8s-api-proxy (one pod, `Recreate`) is a second path that must
   scale. Does this design cover both, or does the k8s-api-proxy get its own?
8. #4027 caps the command path at about 1.5 commands per second in the sandbox. Until it is fixed,
   a measurement can show the caller's limit and not the broker's. §8 needs it fixed, or its
   effect recorded.
