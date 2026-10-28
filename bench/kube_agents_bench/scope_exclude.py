# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The seeded clusters a case excludes from the install's scope, for ``hack/ci-eval-pr.sh``.

A case whose precondition is an install that leaves some seeded clusters out of
its scope declares ``scope_exclude: {slots: [b]}``. The runner runs such a case
alone, after every other unit: it adds each slot's cluster to the PlatformAgent's
``spec.scope.exclude.clusters`` for the case's repetitions, then puts the scope
back. Slots, not cluster names, because a case runs unchanged on every pool
project; the cluster behind a slot comes from the ``.fleet-context`` file
``hack/fleet-kubeconfigs.sh`` writes, the one place a seeded cluster is resolved.

The scope the patch replaced is saved on the CR itself, in ANNOTATION, so the
restore needs nothing from the run that patched it: the EXIT trap and the next
lease's preflight both restore from the CR alone, and a patch over a leftover
starts from the saved scope rather than from the leftover.

Subcommands, each printing to stdout and exiting 1 with the reason on stderr:

``cases <task.yaml>...``
    ``<case> <slot>...`` for each task that declares the key, in the order given.
``patch --fleet-context <file> --slots <slot>...`` (the CR as JSON on stdin)
    The JSON patch that excludes the slots' clusters and saves the scope.
``restore`` (the CR as JSON on stdin)
    The JSON patch for the next step back to the saved scope, or nothing when
    none is saved. A CR that had no scope block takes two steps, with a
    reconcile between them (restore_ops says why).
``clusters`` (a CR, or the operator's rendered scope file, as JSON on stdin)
    The excluded clusters, sorted, as one line of JSON, so the runner can tell
    when the pod reads the scope the CR declares.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

KEY = "scope_exclude"
SLOTS_KEY = "slots"
# A catalogue slot (bench/tf/fleet/fixtures.json, `cluster_slots`); the lint
# checks the slot exists, this checks only what the runner splices into a lookup.
SLOT = re.compile(r"\A[a-z]\Z")
# The scope the patch replaced, as JSON (`null` for a CR with no scope block).
ANNOTATION = "bench.kubeagents.x-k8s.io/scope-before-exclude"
# The first restore step for a CR that had no scope block: a block that excludes
# nothing, which the reconcile reads as a declaration (restore_ops says why).
CLEARED_SCOPE: dict[str, Any] = {"exclude": {"clusters": []}}
# `.fleet-context` keys, as hack/fleet-kubeconfigs.sh writes them.
CONTEXT_PROJECT = "project"
CONTEXT_CLUSTER = "cluster.{slot}"
CONTEXT_LOCATION = "location.{slot}"


class ScopeExcludeError(Exception):
    pass


def declared_slots(task_yaml: Path) -> list[str]:
    """The slots a task excludes; empty when it declares none."""
    # Imported here: `patch`, `restore` and `clusters` run under a bare python3 in
    # the EXIT trap and the lease preflight, before the bench virtualenv exists.
    import yaml

    try:
        doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScopeExcludeError(f"{task_yaml}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeExcludeError(f"{task_yaml}: expected a YAML mapping at the top level")
    if KEY not in doc:
        return []
    value = doc[KEY]
    slots = value.get(SLOTS_KEY) if isinstance(value, dict) and set(value) == {SLOTS_KEY} else None
    if (
        not isinstance(slots, list)
        or not slots
        or not all(isinstance(s, str) and SLOT.match(s) for s in slots)
        or len(set(slots)) != len(slots)
    ):
        raise ScopeExcludeError(
            f"{task_yaml}: '{KEY}:' must be {{{SLOTS_KEY}: [<slot>, ...]}} with distinct slot letters, got {value!r}"
        )
    return slots


def _fleet_context(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScopeExcludeError(f"{path}: {exc}") from exc
    return dict(line.split("=", 1) for line in lines if "=" in line)


def excluded_clusters(context_path: Path, slots: list[str]) -> list[dict[str, str]]:
    """Each slot's cluster as the triple `spec.scope.exclude.clusters` takes."""
    context = _fleet_context(context_path)
    project = context.get(CONTEXT_PROJECT)
    if not project:
        raise ScopeExcludeError(f"{context_path}: records no '{CONTEXT_PROJECT}='")
    clusters = []
    for slot in slots:
        name = context.get(CONTEXT_CLUSTER.format(slot=slot))
        location = context.get(CONTEXT_LOCATION.format(slot=slot))
        if not name or not location:
            raise ScopeExcludeError(f"{context_path}: slot '{slot}' has no resolved cluster and location")
        clusters.append({"projectId": project, "location": location, "clusterName": name})
    return clusters


def _annotation_path() -> str:
    # RFC 6901: `~` and `/` in a key are escaped as `~0` and `~1`.
    return "/metadata/annotations/" + ANNOTATION.replace("~", "~0").replace("/", "~1")


def _saved_scope(cr: dict[str, Any]) -> tuple[bool, Any]:
    annotations = (cr.get("metadata") or {}).get("annotations") or {}
    if ANNOTATION not in annotations:
        return False, None
    try:
        return True, json.loads(annotations[ANNOTATION])
    except ValueError as exc:
        raise ScopeExcludeError(f"annotation {ANNOTATION} is not JSON: {exc}") from exc


def patch_ops(cr: dict[str, Any], clusters: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Exclude `clusters` on top of the scope the CR had before any exclusion."""
    saved, base = _saved_scope(cr)
    if not saved:
        base = (cr.get("spec") or {}).get("scope")
    scope = copy.deepcopy(base) if isinstance(base, dict) else {}
    exclude = scope.get("exclude") if isinstance(scope.get("exclude"), dict) else {}
    merged = list(exclude.get("clusters") or [])
    merged += [c for c in clusters if c not in merged]
    scope["exclude"] = {**exclude, "clusters": merged}
    ops: list[dict[str, Any]] = []
    if not (cr.get("metadata") or {}).get("annotations"):
        ops.append({"op": "add", "path": "/metadata/annotations", "value": {}})
    ops.append({"op": "add", "path": _annotation_path(), "value": json.dumps(base)})
    ops.append({"op": "add", "path": "/spec/scope", "value": scope})
    return ops


def restore_ops(cr: dict[str, Any]) -> list[dict[str, Any]]:
    """The next step back to the saved scope; empty when none is saved.

    One step when the CR had a scope block: put it back and drop the annotation.
    Two when it had none, because the Cluster Agent reconcile reads an absent
    block as "keep the last declaration's exclusions": removing the block while
    it still excludes would leave the clusters excluded for good. So the first
    step empties the exclusions and keeps the annotation, and only once a
    reconcile has run under that does the second remove the block.
    """
    saved, base = _saved_scope(cr)
    if not saved:
        return []
    remove_annotation = {"op": "remove", "path": _annotation_path()}
    if base is not None:
        return [{"op": "add", "path": "/spec/scope", "value": base}, remove_annotation]
    scope = (cr.get("spec") or {}).get("scope")
    if scope is None:
        return [remove_annotation]
    if scope_clusters({"spec": {"scope": scope}}):
        return [{"op": "add", "path": "/spec/scope", "value": copy.deepcopy(CLEARED_SCOPE)}]
    return [{"op": "remove", "path": "/spec/scope"}, remove_annotation]


def scope_clusters(doc: dict[str, Any]) -> list[dict[str, str]]:
    """The excluded clusters of a CR or of the rendered scope file, sorted."""
    scope = (doc.get("spec") or {}).get("scope") if "spec" in doc else doc
    exclude = (scope or {}).get("exclude") or {}
    return sorted(exclude.get("clusters") or [], key=lambda c: json.dumps(c, sort_keys=True))


def _stdin_json() -> dict[str, Any]:
    try:
        doc = json.load(sys.stdin)
    except ValueError as exc:
        raise ScopeExcludeError(f"stdin is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScopeExcludeError("stdin is not a JSON object")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kube_agents_bench.scope_exclude")
    sub = parser.add_subparsers(dest="command", required=True)
    cases = sub.add_parser("cases")
    cases.add_argument("tasks", nargs="*")
    patch = sub.add_parser("patch")
    patch.add_argument("--fleet-context", required=True, type=Path)
    patch.add_argument("--slots", required=True, nargs="+")
    sub.add_parser("restore")
    sub.add_parser("clusters")
    args = parser.parse_args(argv)
    try:
        if args.command == "cases":
            for task in args.tasks:
                slots = declared_slots(Path(task))
                if slots:
                    print(" ".join([Path(task).parent.name, *slots]))
        elif args.command == "patch":
            print(json.dumps(patch_ops(_stdin_json(), excluded_clusters(args.fleet_context, args.slots))))
        elif args.command == "restore":
            ops = restore_ops(_stdin_json())
            if ops:
                print(json.dumps(ops))
        else:
            print(json.dumps(scope_clusters(_stdin_json()), sort_keys=True))
    except ScopeExcludeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
