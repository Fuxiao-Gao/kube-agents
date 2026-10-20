"""Refuse, once, a rightsizing answer that drops its dependency lines or its checks.

Installed into the image at ``/opt/hermes/tools/kanban_rightsizing_checks.py``.
Importing it appends :func:`rightsizing_rule` to
``kanban_verified_claims.RULES``, so it runs inside that module's
``kanban_complete`` gate and shares its one refusal per card.
``deploy/docker/patches/apply_kanban_rightsizing_checks.py`` adds the import to
``tools/kanban_tools.py``.

Why
---
The Cluster Agent skill ``gke-rightsizing-checks`` asks a card to open its
``result`` with one line per workload naming the dependency that blocks a cut
(an HPA, Guaranteed QoS), and to show five checks. The Platform Agent writes the
skill's name in the body of each Cluster Agent card it sends a request cut to.
In the eval case ``cost-rightsizing-names-dependencies`` the Cluster Agent did
that, and the Platform Agent's fan-out summary still dropped a dependency in one
run out of three, three builds in a row (#3199, #3507). Prompt text did not hold
it, so this gate does (#3491).

What it checks
--------------
Only a card the skill was asked for is checked, or a card above one:

- **A card whose body names the skill.** Its ``result`` opens with at least one
  dependency line (``<workload>: <dependency>``, or ``no dependency found``)
  within its first :data:`OPENING_LINES` non-blank lines, and mentions each of
  the five checks (:data:`CHECKS`).
- **A card that created such cards** (``kanban_verified_claims.descendant_card_ids``).
  Its ``result`` carries every dependency line those cards opened with, word for
  word after Markdown emphasis and whitespace are folded.

A card the skill was not asked for, and a card that created none, pass
untouched. Nothing is parsed out of free text to decide whether a card is a
rightsizing answer: the body naming the skill is the trigger.

Refusal
-------
The gate's: the first completion with a problem is refused with every rule's
reasons in one message, and the retry is accepted. On the retry this rule
changes nothing and only logs, so the stored ``result`` is the one the worker
wrote.
"""

from __future__ import annotations

import logging
import re

try:
    from tools import kanban_verified_claims as gate
except ImportError:  # Unit tests import the patch modules flat.
    import kanban_verified_claims as gate

logger = logging.getLogger(__name__)

#: The Cluster Agent skill whose name in a card's body turns this rule on.
SKILL_NAME = "gke-rightsizing-checks"

#: How far into a ``result`` the dependency lines may start. The skill puts them
#: first; a bold one-sentence answer above them is allowed.
OPENING_LINES = 8

#: ``<workload>: <dependency>``: a Kubernetes name, then a colon and text.
DEPENDENCY_LINE = re.compile(r"^\s*(?:[-*]\s+)?([a-z0-9][a-z0-9.-]*[a-z0-9])\s*:\s+\S.*$")

#: Labels an answer puts before a colon that are not workloads: the checks'
#: own names and the answer's section words. ``- HPA: none`` is a check line.
LABEL_WORDS = frozenset(
    {"hpa", "keda", "qos", "shared", "source", "packing", "check", "checks", "proposal",
     "arithmetic", "result", "finding", "cluster", "note", "summary", "evidence", "cause", "fix"}
)

#: The five checks the skill requires, each as the words that name it.
CHECKS = {
    "HPA or KEDA": re.compile(r"\b(hpa|horizontalpodautoscaler|horizontal pod autoscaler|keda|scaledobject)\b"),
    "QoS and requests against limits": re.compile(r"\b(qos|guaranteed|burstable|besteffort)\b"),
    "objects sharing its pods or volumes": re.compile(r"\b(pdb|poddisruptionbudget|disruption budget|pvc|persistentvolumeclaim|selector)s?\b"),
    "files shared across clusters": re.compile(r"\b(helm|argo ?cd|argocd|flux|config ?sync|configsync|kustomize|values file|tracking-id)\b"),
    "per-node packing": re.compile(r"\b(allocatable|per node|per-node|packing|nodes? needed|machine type)\b"),
}

#: The statuses of a card whose ``result`` is final.
SETTLED_STATUSES = ("done", "archived")

CARD_BODY_SQL = "SELECT COALESCE(body, '') FROM tasks WHERE id = ?"
CHILD_RESULT_SQL = "SELECT COALESCE(body, ''), COALESCE(result, ''), status FROM tasks WHERE id = ?"

MARKDOWN_NOISE = str.maketrans({"*": None, "_": None, "`": None})
WHITESPACE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Lower case, no Markdown emphasis or code marks, whitespace collapsed."""
    return WHITESPACE.sub(" ", str(text).translate(MARKDOWN_NOISE)).strip().lower()


def _is_dependency_line(line: str) -> bool:
    match = DEPENDENCY_LINE.match(line)
    return bool(match) and match.group(1) not in LABEL_WORDS


def dependency_lines(result: object) -> list[str]:
    """The block of ``<workload>: <dependency>`` lines a ``result`` opens with, folded.

    The block starts at the first such line within the first
    :data:`OPENING_LINES` non-blank lines and ends at the next blank or other
    line, so the check lines further down (``- HPA: ...``) are not taken for it.
    """
    block: list[str] = []
    seen = 0
    for raw in str(result or "").splitlines():
        line = _fold(raw)
        if block:
            if not line or not _is_dependency_line(line):
                break
            block.append(line)
            continue
        if not line:
            continue
        seen += 1
        if seen > OPENING_LINES:
            break
        if _is_dependency_line(line):
            block.append(line)
    return block


def missing_checks(result: object) -> list[str]:
    """The checks from :data:`CHECKS` that ``result`` never names."""
    text = _fold(result or "")
    return [name for name, pattern in CHECKS.items() if not pattern.search(text)]


def _own_reasons(result: object) -> list[str]:
    reasons = []
    if not dependency_lines(result):
        reasons.append(
            f"{SKILL_NAME}: open `result` with one line per workload, `<workload>: <dependency>` "
            "(or `<workload>: no dependency found`), before anything else"
        )
    missing = missing_checks(result)
    if missing:
        reasons.append(f"{SKILL_NAME}: `result` does not show these checks: {', '.join(missing)}")
    return reasons


def _child_reasons(conn, task_id: str, result: object) -> list[str]:
    folded = _fold(result or "")
    reasons = []
    for child in gate.descendant_card_ids(conn, task_id):
        row = conn.execute(CHILD_RESULT_SQL, (child,)).fetchone()
        if not row or SKILL_NAME not in str(row[0]) or row[2] not in SETTLED_STATUSES:
            continue
        for line in dependency_lines(row[1]):
            if line not in folded:
                reasons.append(f"card {child} reported `{line}`; carry that line into `result` word for word")
    return reasons


def rightsizing_rule(
    *, task_id: str, result: object, metadata: object, retry: bool, connect=None, **_
) -> tuple[list, object, object]:
    """The completion rule; see ``kanban_verified_claims.RULES`` for the contract."""
    if connect is None:
        return [], result, metadata
    try:
        conn = connect()
        try:
            row = conn.execute(CARD_BODY_SQL, (task_id,)).fetchone()
            reasons = _own_reasons(result) if row and SKILL_NAME in str(row[0]) else []
            reasons += _child_reasons(conn, task_id, result)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a failed read must not block a completion
        logger.warning("[kanban] card %s: rightsizing check not run: %r", task_id, exc)
        return [], result, metadata
    if retry:
        for reason in reasons:
            logger.warning("[kanban] card %s: accepted on the retry with: %s", task_id, reason)
        return [], result, metadata
    return reasons, result, metadata


if rightsizing_rule not in gate.RULES:
    gate.RULES.append(rightsizing_rule)
