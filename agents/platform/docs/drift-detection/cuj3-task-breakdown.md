# CUJ 3 — Noise-filtered drift triage

**Type:** Implementation · **Domain:** Drift detection · **Phase:** 1
**Depends on:** none — this is the enabling CUJ; CUJs 1, 2, and 4 are blocked on it.

## Context

Out-of-band changes to a cluster are buried in reconciliation churn. On a real cluster,
roughly 78% of mutating calls come from `system:*` controllers and another 20% from CI
service accounts, leaving about 1% that a human actually made. Argo reports
`OutOfSync: 40` with no actor and no filter, so nobody looks at it.

The Phase 0 spike established that two static filters remove ~99% of that noise, leaving
roughly seven real human changes a day on an active cluster — a volume small enough for
per-change agent judgment. This ticket implements that filter and the attribution join
behind it, and emits the result as an inject on the AutoOps pipeline.

**Read before starting:**
[`drift-detection.md`](../drift-detection.md) (design) and
[`spike-findings.md`](./spike-findings.md) (measured results, verdict GO).

## Goal

Turn raw cluster mutation activity into a small, attributed stream of real human changes,
emitted as `gitops-drift` injects on the existing pipeline.

## Definition of done

- One out-of-band `kubectl` change produces **exactly one** inject within ~90s.
- One full CI deploy produces **zero** injects.
- Filter configuration is per-cluster config, not code.
- A measured noise report exists for at least one live cluster.

## Out of scope

Named explicitly, because each is a plausible-looking rabbit hole:

- **`_build_agent_query()` and the inject envelope.** Both are hardcoded k8s-event-shaped.
  Generalizing them is platform work with a separate owner.
- **Desired-state resolution and Helm/Kustomize rendering.** That is the diff work in a
  later phase, and it is unbounded.
- **Revert-or-codify judgment.** That is CUJ 1. This ticket reports what happened; it does
  not decide what to do about it.

---

## M0 · Ramp

### T1 · Reproduce Spike A by hand
Run `drift_attribute.sh` against a namespace. Make a `kubectl patch` and watch it appear in
both halves of the output. Make the same change as a service account and note the
difference.

**Accept:** you can explain, without looking it up, why `managedFields` alone cannot
separate CI from a human. Do not start M1 until this is true — the whole detector design
rests on it.

---

## M1 · The trigger

### T3 · Stand up the audit path
Cloud Audit Logs → Log Router sink → Pub/Sub topic → subscription. IAM: the sink writer
service account needs `pubsub.publisher`; the consumer needs subscriber.

**Accept:** `gcloud pubsub subscriptions pull` returns a real mutating-call entry.
**Note:** pair on the IAM rather than debugging permission errors alone.

### T4 · Consumer skeleton
Pull, parse, and log `principalEmail`, `methodName`, `resourceName`, `timestamp`. No
filtering yet.

**Accept:** a `kubectl patch` appears in the consumer log within 90s. The spike measured
30–60s ingestion delay; the margin is deliberate.

### T5 · The two static filters
Drop `principalEmail =~ "^system:"`. Then drop a configurable automation-service-account
allowlist. The allowlist must be configuration, not a constant — it is per-cluster and it is
the single most important tuning knob in the system.

**Accept:** unit tests over recorded audit payloads covering a system principal, a CI service
account, and a human. Capturing those fixtures is part of this task.

### T6 · Measure the noise profile
Run 24–48h against a live cluster. Produce a short report: events by tier, before and after
filtering.

**Accept:** the numbers land near the spike's (~78% system, ~20% automation, ~1% human).

**If the numbers are wildly off, that is a finding, not a bug.** Report it. Do not tune the
filters until reality matches the document.

---

## M2 · Attribution

### T7 · Port the join to the consumer
For each surviving event, fetch the live object and extract field-level `managedFields`
attribution. This is a direct port of `drift_attribute.sh`, which gives you a known-good
output to diff against.

**Accept:** output matches the shell script for the same namespace.

### T8 · Handle deletes
Deletes are audit-log-only — the object is gone, so there is nothing to read.

**Accept:** deleting a NetworkPolicy produces a clean attributed record and does not crash
the consumer.

### T9 · Emit the inject
POST a `gitops-drift` inject to `/sessions/{session_id}/inject`.

**Accept:** the definition of done above — one change, one inject; one CI deploy, zero
injects.

---

## M3 · Stretch

### T10 · Daily digest
A drift skill plus a minimal judgment prompt that reports the day's real human changes in
plain language. No revert-or-codify decision — just what happened.

Only start this if M1 and M2 have landed cleanly.

---

## Escalate immediately if

- The audit sink requires org-level IAM you do not have.
- The automation allowlist looks cluster-specific in a way configuration cannot express.
- Post-filter volume is an order of magnitude above the spike's ~7/day.

## Findings you should not re-derive

From the Phase 0 spike. Each is counterintuitive enough to be worth restating:

1. **The audit log is the primary trigger; `managedFields` is enrichment.** Inverting the
   order produces false positives on a real cluster.
2. **The audit principal is required, not optional.** CI applying client-side records
   `kubectl-client-side-apply` — the same manager string a human running `kubectl apply`
   produces.
3. **Two static filters remove ~99% of the noise.**
4. **Post-filter volume is low enough that no sampling is needed.**
