# CUJ 3 — Noise-filtered drift triage

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

## Before you start

Run `drift_attribute.sh` against a namespace, make a `kubectl patch`, and watch it land in both
halves of the output. Then make the same change as a service account and compare. You should be
able to explain why `managedFields` alone cannot separate CI from a human before writing any code —
the whole detector design rests on that.

**Placement decision to confirm with your reviewer:** the existing adapter
(`k8s-operator/cmd/k8s-event-watcher`) is Go, so the natural home is a sibling
`k8s-operator/cmd/drift-detector`. Confirm before scaffolding.

---

## T1 · Audit-log ingestion path

Stand up the transport and a consumer that parses what comes out of it.

**Infrastructure.** A Log Router sink at project scope, a Pub/Sub topic, and a pull subscription.
Sink filter, taken from the spike:

```
logName="projects/PROJECT/logs/cloudaudit.googleapis.com%2Factivity"
resource.type="k8s_cluster"
resource.labels.cluster_name="CLUSTER"
protoPayload.methodName=~"create|patch|update|delete"
```

Do the principal filtering in the consumer, not the sink — you want the unfiltered volume visible
for T2's measurement, and sink filters are awkward to iterate on.

**IAM.** The sink's writer identity needs `roles/pubsub.publisher` on the topic; the consumer's
service account needs `roles/pubsub.subscriber` on the subscription. Pair on this rather than
burning days on permission errors.

**Consumer.** Pull loop, ack on successful parse, structured log per message. Extract:

| Field | Path |
|---|---|
| Principal | `protoPayload.authenticationInfo.principalEmail` |
| Verb | `protoPayload.methodName` |
| Resource | `protoPayload.resourceName` |
| User agent | `protoPayload.callerSuppliedUserAgent` |
| Time | `timestamp` |

`resourceName` arrives as a path like `core/v1/namespaces/foo/services/web`. Parse it into
`(group, version, namespace, kind, name)` — you need those components for the live-object lookup
in T3, and getting the cluster-scoped and core-group variants right is the fiddly part.

**Accept:** a `kubectl patch` appears in the consumer's structured log within 90s, with all five
fields populated and the resource path decomposed. The spike measured 30–60s ingestion delay; the
margin is deliberate.

---

## T2 · Principal classification and the noise profile

**Two filters, both config-driven.** Drop `principalEmail` matching `^system:`. Then drop principals
in an automation allowlist. That allowlist is per-cluster configuration, not a constant — it is the
single most important tuning knob in the system, and getting it wrong makes every CI deploy look
like drift. Something in the shape of:

```yaml
drift:
  automation_principals:
    - github-deploy-sa@PROJECT.iam.gserviceaccount.com
    - gitops-infra-sa@PROJECT.iam.gserviceaccount.com
  gitops_managers: ["argocd", "flux", "helm", "kube-controller-manager", ".*-controller$"]
```

Classify every message into one of three tiers — `system`, `automation`, `human` — and emit a
counter per tier rather than silently discarding. You need those counts for the measurement below
and for debugging a misconfigured allowlist later.

**Tests.** Table-driven, over recorded audit payloads. Capture real fixtures from the subscription;
do not hand-write them. Cover at minimum: a `system:` controller, a CI service account applying
client-side, a human `kubectl patch`, and a delete.

**Measurement.** Run 24–48h against a live cluster and produce a short report of counts by tier,
before and after filtering.

**Accept:** tier counts land near the spike's (~78% system, ~20% automation, ~1% human), and the
allowlist can be changed without a rebuild.

**If the numbers are wildly off, that is a finding, not a bug.** Report it. Do not tune the filters
until reality matches the document.

---

## T3 · `managedFields` attribution join

For each message classified `human`, fetch the live object using the components parsed in T1 and
extract field-level ownership. This is a direct port of the first half of `drift_attribute.sh`, so
you have a known-good output to diff against.

Keep entries where `operation == "Update"` and the manager does *not* match `gitops_managers`.
`fieldsV1` arrives as a nested structure with `f:`-prefixed keys — flatten it to dotted paths
(`spec.replicas`, `spec.template.spec.containers[0].securityContext.privileged`) so the output is
readable by a human and by the agent downstream. That flattening is the substantive part of this
task; the lookup is not.

**Deletes have no live object.** They are audit-log-only, so the join must short-circuit rather than
404 and crash the consumer. Carry the audit record through with an explicit "object gone" marker.

**Accept:** output matches `drift_attribute.sh` for the same namespace, and deleting a NetworkPolicy
produces a clean attributed record with no live-object lookup attempted.

---

## T4 · Emit the `gitops-drift` inject

Mint a session (`POST /sessions`), then `POST /sessions/{session_id}/inject`.

Match the envelope the event watcher already sends — see `injector.go`. Note that the payload is
marshalled to JSON and then **wrapped as a string** in `{"message": "<escaped JSON>"}`; it is not
posted as a nested object. Carry the principal, verb, resource, timestamp, and the attributed field
list, with `kind: gitops-drift`.

**Accept:** the ticket's definition of done — one out-of-band change produces exactly one inject
within ~90s; one full CI deploy produces zero.

---

## T5 · Daily digest *(stretch — only if T1–T4 land clean)*

A drift skill under `agents/platform/skills/` plus a minimal judgment prompt that reports the day's
real human changes in plain language. No revert-or-codify decision — that is CUJ 1. This one just
answers "what actually changed on this cluster today, and who did it."

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
