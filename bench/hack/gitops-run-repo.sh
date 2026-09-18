#!/usr/bin/env bash
#
# One GitOps repository per benchmark run (gke-labs/kube-agents#1773). Merged
# pull requests cannot be deleted on GitHub, so a run that must not see an
# earlier run's fix needs a repository no earlier run wrote to, not a fresh
# branch. This script creates that repository with one neutral root commit
# (the files under bench/tf/prebuilt/gitops-fix-cycle/repo: a README and the
# merge-on-green workflow; no task directory, no history), wires it into the
# install so the agent can mint a token for it, proves the mint, and archives
# it when the campaign is done.
#
# Actions:
#   minter-mount      one-time: mount the minter's whole ConfigMap at
#                     /etc/minty/<org>/ (keys <repo>.yaml) instead of one file
#                     by subPath, so adding a repository is a ConfigMap key.
#   create <name>     create <org>/<name>, push the root commit, add the
#                     repository to the GitHub App installation and to the
#                     minter's config, restart the minter. Prints the root
#                     commit SHA on stdout; progress goes to stderr.
#   check <name>      from the shell sandbox pod, mint a token for the
#                     repository through the credential proxy (what the agent
#                     does before its first push). Exit 0 means the wiring
#                     holds end to end.
#   archive <name>    mark the repository read-only and drop its minter entry.
#
# Env: GITOPS_ORG (gke-agentic), GITOPS_TOKEN_FILE (~/.config/gitops-pilot/
#   github-token; must be an org admin's token: repository creation and the
#   installation edit need it), AGENT_HOST_CONTEXT, AGENT_NAMESPACE,
#   GITOPS_APP_INSTALLATION_ID (the minter App's installation on the org).
set -euo pipefail

: "${GITOPS_ORG:=gke-agentic}"
: "${GITOPS_TOKEN_FILE:=${HOME}/.config/gitops-pilot/github-token}"
: "${GCP_PROJECT_ID:=fuxiaogao-gkedemos}"
: "${AGENT_HOST_CONTEXT:=gke_${GCP_PROJECT_ID}_us-central1_platform-agent-host}"
: "${AGENT_NAMESPACE:=kubeagents-system}"
# Installation of the `kube-agents-demo` GitHub App (the one the pilot
# install's minter signs for) on the gke-agentic org. It is installed on
# selected repositories, so every new repository must be added to it.
: "${GITOPS_APP_INSTALLATION_ID:=153292524}"

readonly GITHUB_API="https://api.github.com"
readonly TEMPLATE_DIR="$(cd "$(dirname "$0")/.." && pwd)/tf/prebuilt/gitops-fix-cycle/repo"
readonly ROOT_COMMIT_MESSAGE="Initial import of the platform manifests"
readonly ROOT_AUTHOR_NAME="platform-team"
readonly ROOT_AUTHOR_EMAIL="platform-team@users.noreply.github.com"
readonly MINTER_CONFIGMAP="github-token-minter-config"
readonly MINTER_DEPLOYMENT="github-token-minter"
readonly MINTER_VOLUME="config-volume"
readonly MINTER_CONFIGS_DIR="/etc/minty"
readonly MINTER_ROLLOUT_TIMEOUT="180s"
readonly MINTER_SCOPE_NAME="platform-agent-scope"
readonly PLATFORM_AGENT_CR="platformagents.kubeagents.x-k8s.io/platform-agent"
readonly GSA_ANNOTATION="iam.gke.io/gcp-service-account"
readonly SHELL_POD="platform-agent-shell-0"
readonly REFRESH_SCRIPT="/opt/data/scripts/github_token_refresh.py"
readonly REPO_NAME_PATTERN='^kage-eval-[a-z0-9-]+$'

ACTION="${1:?usage: $0 minter-mount | create <name> | check <name> | archive <name>}"
NAME="${2:-}"
K=(kubectl --context "${AGENT_HOST_CONTEXT}" -n "${AGENT_NAMESPACE}")

log() { echo "$*" >&2; }
die() { log "gitops-run-repo: $*"; exit 1; }

token() { tr -d '\r\n' < "${GITOPS_TOKEN_FILE/#\~/$HOME}"; }
gh_api() { GH_TOKEN="$(token)" gh api -H "Accept: application/vnd.github+json" "$@"; }

need_name() {
  [ -n "${NAME}" ] || die "${ACTION} needs a repository name"
}
# create and archive write to the org; only campaign-named repositories.
need_campaign_name() {
  need_name
  [[ "${NAME}" =~ ${REPO_NAME_PATTERN} ]] || die "refusing '${NAME}': campaign repositories are named kage-eval-<...>"
}

# --- minter ----------------------------------------------------------------
minter_mounts_dir() {
  "${K[@]}" get deploy "${MINTER_DEPLOYMENT}" -o json | python3 -c '
import json, sys
org, d = sys.argv[1], json.load(sys.stdin)
for c in d["spec"]["template"]["spec"]["containers"]:
    for m in c.get("volumeMounts", []):
        if m["mountPath"].rstrip("/") == f"/etc/minty/{org}" and not m.get("subPath"):
            sys.exit(0)
sys.exit(1)' "${GITOPS_ORG}"
}

minter_scope_yaml() {
  local gsa
  gsa="$("${K[@]}" get "${PLATFORM_AGENT_CR}" -o jsonpath="{.spec.security.serviceAccountAnnotations.${GSA_ANNOTATION//./\\.}}")"
  [ -n "${gsa}" ] || die "could not read the agent's GSA from ${PLATFORM_AGENT_CR}"
  cat <<YAML
version: 'minty.abcxyz.dev/v2'
rule:
  if: "assertion.iss == 'https://accounts.google.com'"
scope:
  ${MINTER_SCOPE_NAME}:
    rule:
      if: "assertion.email in ['${gsa}']"
    repositories:
      - '${1}'
    permissions:
      contents: 'write'
      pull_requests: 'write'
      issues: 'write'
YAML
}

# minter_set_key <key> <file|-> ; minter_drop_key <key>
minter_set_key() {
  "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
key, path, d = sys.argv[1], sys.argv[2], json.load(sys.stdin)
d["data"][key] = open(path).read()
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "$1" "$2" | "${K[@]}" apply -f - >&2
}
minter_drop_key() {
  "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
key, d = sys.argv[1], json.load(sys.stdin)
d["data"].pop(key, None)
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "$1" | "${K[@]}" apply -f - >&2
}
minter_restart() {
  "${K[@]}" rollout restart deploy/"${MINTER_DEPLOYMENT}" >&2
  "${K[@]}" rollout status deploy/"${MINTER_DEPLOYMENT}" --timeout="${MINTER_ROLLOUT_TIMEOUT}" >&2
}

case "${ACTION}" in
  minter-mount)
    if minter_mounts_dir; then log "minter already mounts ${MINTER_CONFIGS_DIR}/${GITOPS_ORG}/ as a directory"; exit 0; fi
    # Re-key <org>-<repo>.yaml -> <repo>.yaml, keep the old keys until the
    # mount has moved, then swap the subPath mount for the directory mount.
    "${K[@]}" get cm "${MINTER_CONFIGMAP}" -o json | python3 -c '
import json, sys
org, d = sys.argv[1], json.load(sys.stdin)
for k in list(d["data"]):
    if k.startswith(org + "-"): d["data"][k[len(org) + 1:]] = d["data"][k]
for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"): d["metadata"].pop(k, None)
d["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
json.dump(d, sys.stdout)' "${GITOPS_ORG}" | "${K[@]}" apply -f - >&2
    patch="$("${K[@]}" get deploy "${MINTER_DEPLOYMENT}" -o json | python3 -c '
import json, sys
org, vol, d = sys.argv[1], sys.argv[2], json.load(sys.stdin)
c = d["spec"]["template"]["spec"]["containers"][0]
mounts = [m for m in c.get("volumeMounts", []) if m["name"] != vol]
mounts.append({"name": vol, "mountPath": f"/etc/minty/{org}", "readOnly": True})
print(json.dumps({"spec": {"template": {"spec": {"containers": [{"name": c["name"], "volumeMounts": mounts}]}}}}))' "${GITOPS_ORG}" "${MINTER_VOLUME}")"
    "${K[@]}" patch deploy "${MINTER_DEPLOYMENT}" --type strategic -p "${patch}" >&2
    "${K[@]}" rollout status deploy/"${MINTER_DEPLOYMENT}" --timeout="${MINTER_ROLLOUT_TIMEOUT}" >&2
    log "minter now reads ${MINTER_CONFIGS_DIR}/${GITOPS_ORG}/<repo>.yaml from the ConfigMap; old <org>-<repo>.yaml keys left in place"
    ;;

  create)
    need_campaign_name
    minter_mounts_dir || die "the minter mounts one file by subPath; run '$0 minter-mount' once first"
    [ -d "${TEMPLATE_DIR}/.github/workflows" ] || die "template ${TEMPLATE_DIR} missing"
    log "==> creating ${GITOPS_ORG}/${NAME}"
    repo_id="$(gh_api -X POST "orgs/${GITOPS_ORG}/repos" -f name="${NAME}" -F private=true -F has_wiki=false -F has_projects=false \
      -f description="kube-agents benchmark run repository (gke-labs/kube-agents#1773); archived after the run" --jq .id)"
    work="$(mktemp -d)"; trap 'rm -rf "${work}"' EXIT
    cp -R "${TEMPLATE_DIR}/." "${work}/"
    # A global url.insteadOf that rewrites https to ssh would send the push
    # down a key path this token cannot use; the global config stays out.
    export GIT_CONFIG_GLOBAL=/dev/null
    git -C "${work}" init -q -b main
    git -C "${work}" add -A
    git -C "${work}" -c user.name="${ROOT_AUTHOR_NAME}" -c user.email="${ROOT_AUTHOR_EMAIL}" commit -q -m "${ROOT_COMMIT_MESSAGE}"
    root="$(git -C "${work}" rev-parse HEAD)"
    # The token reaches git through a helper, not the URL, so it never lands
    # in the process table or a reflog.
    git -C "${work}" -c credential.helper="!f() { echo username=x-access-token; echo password=$(token); }; f" \
      push -q "https://github.com/${GITOPS_ORG}/${NAME}.git" main:main >&2
    log "    root commit ${root}"
    # GitHub answers this with 403 for an OAuth token (tested 2026-09-18), so
    # it is attempted, not required: `check` is what proves the installation
    # reaches the repository, and the manual path is one click per run.
    log "==> adding to App installation ${GITOPS_APP_INSTALLATION_ID}"
    if ! gh_api -X PUT "user/installations/${GITOPS_APP_INSTALLATION_ID}/repositories/${repo_id}" >&2 2>/dev/null; then
      log "    not added through the API; add ${NAME} at https://github.com/organizations/${GITOPS_ORG}/settings/installations/${GITOPS_APP_INSTALLATION_ID} (Repository access), then run: $0 check ${NAME}"
    fi
    log "==> minter entry ${NAME}.yaml"
    scope="$(mktemp)"; minter_scope_yaml "${NAME}" > "${scope}"
    minter_set_key "${NAME}.yaml" "${scope}"; rm -f "${scope}"
    minter_restart
    echo "${root}"
    ;;

  check)
    need_name
    log "==> minting a token for ${GITOPS_ORG}/${NAME} from ${SHELL_POD} (credential proxy -> minter -> GitHub)"
    "${K[@]}" exec "${SHELL_POD}" -- python3 "${REFRESH_SCRIPT}" "${GITOPS_ORG}/${NAME}" >&2
    log "    ok"
    ;;

  archive)
    need_campaign_name
    log "==> archiving ${GITOPS_ORG}/${NAME}"
    gh_api -X PATCH "repos/${GITOPS_ORG}/${NAME}" -F archived=true --jq '.archived' >&2
    minter_drop_key "${NAME}.yaml"
    minter_restart
    ;;

  *) die "unknown action ${ACTION}" ;;
esac
