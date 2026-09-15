# Seeded-condition assertions for b-0011, sourced by setup.sh after the
# Application reports Synced. wait_for, EXPECT, WAIT_TIMEOUT and kubectl's
# context come from the caller.
#
# Two ready, not the original's three: the original reaches 3 only because its
# in-place rollout leaves one old 64Mi pod behind. A single sync of the broken
# state never creates that pod. Sync waves in the manifests (gating, then
# pricer, then the rest) keep pricer at 2/2, which the task's ready-floor
# safeguard requires from the first sample. See render-broken-base.sh.
EXPECT=256Mi wait_for "checkout memory request" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].resources.requests.memory}'
EXPECT=hashicorp/http-echo:1.0.0 wait_for "checkout image" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.template.spec.containers[?(@.name=="web")].image}'
EXPECT=4 wait_for "checkout spec.replicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.spec.replicas}'
EXPECT=2 wait_for "pricer readyReplicas" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy pricer -o jsonpath='{.status.readyReplicas}'
EXPECT=2 wait_for "checkout readyReplicas (quota-bound)" "${WAIT_TIMEOUT}" \
  kubectl -n payments get deploy checkout -o jsonpath='{.status.readyReplicas}'
wait_for "quota-denied checkout pod event" "${WAIT_TIMEOUT}" \
  bash -c "kubectl -n payments get events --field-selector reason=FailedCreate -o name | head -1"
wait_for "metrics API returns pod data" "${WAIT_TIMEOUT}" \
  bash -c "kubectl top pods -n payments --no-headers 2>/dev/null | head -1"
SEED_SUMMARY="payments: checkout 2/4 ready (quota-bound at 256Mi), pricer 2/2"
