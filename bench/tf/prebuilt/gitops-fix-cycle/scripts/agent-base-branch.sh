#!/usr/bin/env bash
#
# Pin the platform agent's pull-request base to the run branch for one run
# (gke-labs/kube-agents#1970). Called by main.tf's
# null_resource.agent_base_branch when gitops_pin_agent_base_branch is set:
# `pin` from the create-time provisioner, after the seed; `unpin` from the
# destroy-time one, before the run branch is deleted.
#
# pin:   refuses when spec.integration.baseBranch already names another
#        branch (an administrator's base is never overwritten, so unpin never
#        removes it), then sets it to the run branch and reads it back. When
#        the installed CRD declares the field, the read-back must match, and
#        the step waits for the operator to render the base into the
#        credential broker's environment for this repository and for the
#        broker to roll.
#        The broker is its own Deployment, and the base is rendered there and
#        nowhere else, so the gateway does not roll. When the CRD has no such
#        field, the API server drops it; that is logged, and is the case's red
#        on such an install, not a setup failure. The CRD is read before the
#        patch, and one that cannot be read fails the pin with nothing written
#        rather than passing for one without the field. A
#        failure from the patch on (including a patch the server applied but
#        kubectl reported as failed) removes the field again, when it still names this run's branch,
#        before exiting non-zero: a failed create-time provisioner taints the
#        resource, and tofu runs no destroy-time provisioner on a tainted one,
#        so unpin would never run.
# unpin: removes spec.integration.baseBranch when it still names this run's
#        branch, and waits for the broker to roll back; anything else is left.
#        The removal is a JSON patch that tests the value first, so a retry
#        never removes a base another run has set since, and between tries the
#        field is read again: once it no longer names this run's branch, the
#        removal is done.
#        Once the field is removed, the waits only confirm: a timeout there is
#        a warning, not a failure, because a failed destroy-time provisioner
#        keeps this resource in state and stops tofu from destroying the task
#        cluster and the run branch. A PlatformAgent that cannot be read, and
#        a removal that still fails after REMOVE_BASE_ATTEMPTS tries, are
#        warnings for the same reason; the run wrapper's leak check reports a
#        pin left behind (or "unknown" when it cannot read the field), and the
#        next run's wrapper refuses to start while it is there.
#
# kubectl patch sends no field validation, so the API server's default (warn)
# applies: an unknown field is dropped with a warning instead of failing the
# request, which is what lets one stack run on installs with and without the
# field.
#
# Env: AGENT_HOST_CONTEXT, AGENT_NAMESPACE, GITOPS_REPO (https URL),
#      GITOPS_RUN_BRANCH; optionally AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC and
#      AGENT_BASE_BRANCH_POLL_SECONDS (whole seconds, above zero).
set -euo pipefail

readonly CR="platformagents.kubeagents.x-k8s.io/platform-agent"
readonly CRD="platformagents.kubeagents.x-k8s.io"
readonly BROKER="deploy/platform-agent-credential-proxy"
readonly BASE_BRANCH_ENV="CREDENTIAL_PROXY_BASE_BRANCH"
readonly BASE_REPOSITORY_ENV="CREDENTIAL_PROXY_BASE_REPOSITORY"
# How long the operator may take to render a spec change into the broker's
# pod template, and how often to look. Overridable from the environment so the
# tests can drive the timeout path; prefixed, because tofu's local-exec
# inherits the operator's whole environment.
readonly RENDER_TIMEOUT_SEC="${AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC:-300}"
readonly POLL_SECONDS="${AGENT_BASE_BRANCH_POLL_SECONDS:-5}"
# upgrade.sh's budget for the same Deployment (SANDBOX_ROLLOUT_TIMEOUT).
readonly BROKER_ROLLOUT_TIMEOUT=180s
# Tries at removing the field on unpin, POLL_SECONDS apart, before it is left
# to the run wrapper's leak check with a warning.
readonly REMOVE_BASE_ATTEMPTS=3

ACTION="${1:?usage: $0 pin|unpin}"
: "${AGENT_HOST_CONTEXT:?}" "${AGENT_NAMESPACE:?}" "${GITOPS_REPO:?}" "${GITOPS_RUN_BRANCH:?}"

# A zero poll would hammer the API server for the whole timeout, and a value
# that is not a number fails sleep or reads as 0 in arithmetic.
for setting in "AGENT_BASE_BRANCH_RENDER_TIMEOUT_SEC=${RENDER_TIMEOUT_SEC}" "AGENT_BASE_BRANCH_POLL_SECONDS=${POLL_SECONDS}"; do
  [[ "${setting#*=}" =~ ^[1-9][0-9]*$ ]] \
    || { echo "agent-base-branch: ${setting%%=*} must be a whole number of seconds above zero, not '${setting#*=}'" >&2; exit 1; }
done

case "${GITOPS_RUN_BRANCH}" in
  run/*) ;;
  *) echo "agent-base-branch: refusing '${GITOPS_RUN_BRANCH}': only run/** branches are pinned" >&2; exit 1 ;;
esac

K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")
# Removes the field only while it still names this run's branch: the test op
# fails the whole patch otherwise.
readonly REMOVE_BASE_PATCH="[{\"op\":\"test\",\"path\":\"/spec/integration/baseBranch\",\"value\":\"${GITOPS_RUN_BRANCH}\"},{\"op\":\"remove\",\"path\":\"/spec/integration/baseBranch\"}]"

slug="${GITOPS_REPO#https://github.com/}"
slug="${slug%.git}"
slug="${slug%/}"

current_base() { "${K[@]}" get "${CR}" -o jsonpath='{.spec.integration.baseBranch}'; }
remove_base() { "${K[@]}" patch "${CR}" --type=json -p "${REMOVE_BASE_PATCH}"; }

# EXIT trap of pin, armed before the patch: on a non-zero exit, take the pin
# back when it still names this run's branch (the refusal above has already
# returned, so a base this run did not set is never removed). Best effort; the exit status
# stays the pin's.
undo_failed_pin() {
  local rc=$? got
  trap - EXIT
  if [ "${rc}" -ne 0 ]; then
    got="$(current_base || true)"
    if [ "${got}" = "${GITOPS_RUN_BRANCH}" ]; then
      echo "agent-base-branch: pin failed; removing spec.integration.baseBranch (${got})" >&2
      remove_base || echo "agent-base-branch: removing it failed; remove spec.integration.baseBranch by hand" >&2
    fi
  fi
  exit "${rc}"
}

# Whether any served version of the CRD declares spec.integration.baseBranch:
# 0 yes, 1 no, anything else when the CRD could not be read (the helper exits
# 2 on input it cannot parse, which is what a failed kubectl leaves it).
crd_has_base_branch() {
  kubectl --context "${AGENT_HOST_CONTEXT}" get crd "${CRD}" -o json | python3 -c '
import json, sys
try:
    versions = json.load(sys.stdin)["spec"]["versions"]
except (ValueError, KeyError, TypeError):
    sys.exit(2)
for v in versions:
    props = (v.get("schema", {}).get("openAPIV3Schema", {}).get("properties", {})
             .get("spec", {}).get("properties", {}).get("integration", {}).get("properties", {}))
    if v.get("served") and "baseBranch" in props:
        sys.exit(0)
sys.exit(1)'
}

# The value of an env var on any container of the broker's pod template.
broker_env() {
  "${K[@]}" get "${BROKER}" -o json | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]["template"]["spec"]
for c in spec.get("initContainers", []) + spec.get("containers", []):
    for e in c.get("env", []):
        if e.get("name") == sys.argv[1]:
            print(e.get("value", ""))
            sys.exit(0)' "$1"
}

# wait_broker_env <name> <expected value, empty for absent>. A failed read
# counts as not there yet, until the deadline.
wait_broker_env() {
  local name="$1" want="$2" got="" deadline=$((SECONDS + RENDER_TIMEOUT_SEC))
  while :; do
    if got="$(broker_env "${name}")"; then
      [ "${got}" = "${want}" ] && return 0
    else
      got="(unreadable)"
    fi
    if (( SECONDS >= deadline )); then
      echo "agent-base-branch: ${BROKER} ${name} is '${got}' after ${RENDER_TIMEOUT_SEC}s, expected '${want}'" >&2
      return 1
    fi
    sleep "${POLL_SECONDS}"
  done
}

case "${ACTION}" in
  pin)
    # The resourceVersion read with the base goes into the patch, so a base
    # written between this read and the patch fails it (409) rather than being
    # overwritten.
    read="$("${K[@]}" get "${CR}" -o jsonpath='{.metadata.resourceVersion} {.spec.integration.baseBranch}')"
    resource_version="${read%% *}"
    got="${read#* }"
    if [ -n "${got}" ] && [ "${got}" != "${GITOPS_RUN_BRANCH}" ]; then
      echo "agent-base-branch: spec.integration.baseBranch is already '${got}'; refusing to overwrite it (another run in flight, or the install's own base)" >&2
      exit 1
    fi
    # Before the patch, so a CRD that cannot be read fails with nothing written.
    crd_rc=0
    crd_has_base_branch || crd_rc=$?
    case "${crd_rc}" in
      0|1) ;;
      *)
        echo "agent-base-branch: cannot read the CRD ${CRD} on ${AGENT_HOST_CONTEXT}, so whether it declares spec.integration.baseBranch is unknown" >&2
        exit 1 ;;
    esac
    echo "==> agent-base-branch: spec.integration.baseBranch <- ${GITOPS_RUN_BRANCH} on ${AGENT_HOST_CONTEXT}"
    trap undo_failed_pin EXIT
    "${K[@]}" patch "${CR}" --type=merge \
      -p "{\"metadata\":{\"resourceVersion\":\"${resource_version}\"},\"spec\":{\"integration\":{\"baseBranch\":\"${GITOPS_RUN_BRANCH}\"}}}" \
      || { echo "agent-base-branch: the patch failed; if ${CR} changed since it was read (another run pinning?), it was not overwritten" >&2; exit 1; }
    got="$(current_base)"
    echo "    read back: spec.integration.baseBranch='${got}'"
    if [ "${crd_rc}" -eq 1 ]; then
      [ -z "${got}" ] || { echo "agent-base-branch: the CRD declares no baseBranch, yet it reads back '${got}'" >&2; exit 1; }
      echo "    the installed CRD has no spec.integration.baseBranch, so the API server dropped it: this install pins no base, and the agent's pull request base falls back to the repository default"
      exit 0
    fi
    [ "${got}" = "${GITOPS_RUN_BRANCH}" ] || { echo "agent-base-branch: baseBranch reads back '${got}', not ${GITOPS_RUN_BRANCH}" >&2; exit 1; }
    wait_broker_env "${BASE_BRANCH_ENV}" "${GITOPS_RUN_BRANCH}"
    # The operator renders the base only for an accepted GitOps repository;
    # one naming another repository would pin that one instead.
    wait_broker_env "${BASE_REPOSITORY_ENV}" "${slug}"
    echo "    broker env: ${BASE_BRANCH_ENV}=${GITOPS_RUN_BRANCH} ${BASE_REPOSITORY_ENV}=${slug}"
    "${K[@]}" rollout status "${BROKER}" --timeout="${BROKER_ROLLOUT_TIMEOUT}"
    ;;
  unpin)
    if ! got="$(current_base)"; then
      echo "WARN agent-base-branch: cannot read ${CR} on ${AGENT_HOST_CONTEXT}; nothing to unpin" >&2
      exit 0
    fi
    if [ "${got}" != "${GITOPS_RUN_BRANCH}" ]; then
      echo "==> agent-base-branch: baseBranch is '${got}', not this run's; leaving it"
      exit 0
    fi
    echo "==> agent-base-branch: removing spec.integration.baseBranch (${got}) on ${AGENT_HOST_CONTEXT}"
    attempt=1
    until remove_base; do
      # A removal the server applied but kubectl reported as failed, or a base
      # another run has set since, ends the retries.
      if now="$(current_base)" && [ "${now}" != "${GITOPS_RUN_BRANCH}" ]; then
        if [ -n "${now}" ]; then
          echo "==> agent-base-branch: baseBranch is now '${now}', not this run's; leaving it"
          exit 0
        fi
        echo "    spec.integration.baseBranch is gone"
        break
      fi
      if (( attempt >= REMOVE_BASE_ATTEMPTS )); then
        echo "WARN agent-base-branch: removing spec.integration.baseBranch (${got}) failed ${REMOVE_BASE_ATTEMPTS} times; remove it by hand" >&2
        exit 0
      fi
      attempt=$((attempt + 1))
      sleep "${POLL_SECONDS}"
    done
    if ! wait_broker_env "${BASE_BRANCH_ENV}" "" \
      || ! "${K[@]}" rollout status "${BROKER}" --timeout="${BROKER_ROLLOUT_TIMEOUT}"; then
      echo "WARN agent-base-branch: baseBranch is removed, but ${BROKER} has not rolled off it yet; check it by hand" >&2
    fi
    ;;
  *)
    echo "usage: $0 pin|unpin" >&2; exit 1 ;;
esac
