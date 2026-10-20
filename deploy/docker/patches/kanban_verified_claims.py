"""Back every claim a worker marks "verified" with a command that ran in its card.

Installed into the image at ``/opt/hermes/tools/kanban_verified_claims.py`` and
wired in by ``deploy/docker/patches/apply_kanban_verified_claims.py``:

``tools/terminal_tool_result.py``
    :func:`record_terminal_call` keeps each foreground terminal call of a
    kanban worker (command, exit code, the output the model saw, the time) in
    memory, keyed by the card in ``HERMES_KANBAN_TASK``.

``tools/kanban_tools.py``
    :func:`apply_completion_rules` runs each rule in :data:`RULES` before the
    card closes. The first, :func:`verified_claims_rule`, checks
    ``kanban_complete``'s new ``verified_claims`` parameter against that
    record. Another rule can read the record through
    :func:`card_terminal_calls` and share the one refusal.

Why
---
An external pilot on 0.9.0 asked a cost review to verify its claims. The report
came back with "Verification / Falsification check" blocks and "verified"
labels, and no command or output behind any of them (#3200). A "verified"
label with nothing behind it tells the reader they do not need to check.

The fleet-audit findings file already carries ``evidence.command``, but
``audit_report.py`` runs as a subprocess and cannot see the agent's tool calls,
so it can only require the field, not check it. The worker process can: every
terminal call passes through it, and ``kanban_complete`` runs in it.

Upstream's ``agent/verification_evidence.py`` is not a substitute. It records
only test, lint and build commands inside a code project, and only when
verify-on-stop is on; a ``kubectl`` or ``gcloud`` read is never in it.

What a claim is
---------------
Only an entry in ``verified_claims`` is a verified claim. Prose in ``result``
is not parsed. Each entry gives ``claim``, ``command`` and ``excerpt``:

- ``command`` must equal, after whitespace is collapsed, a terminal command
  this card ran;
- ``excerpt`` must appear, after whitespace is collapsed, in the output of a
  run of that command.

The gate copies ``exit_code`` and ``ran_at`` from the recorded call; the model
does not supply them. Accepted claims are stored in ``metadata.verified_claims``
and rendered as an evidence section at the end of ``result``, which is the text
the notifier posts to chat, so every reader of the card sees the same evidence.

Never wedging the card
----------------------
Same policy as ``kanban_result_required.py``, for the same reasons: the first
completion with a problem is refused with every rule's reasons in one message;
the retry inside :data:`NUDGE_TTL_SECONDS` is accepted, the reasons are logged
at WARNING, and the card closes. The refusal is shared by all rules, so two
rules cannot refuse one card twice. On the retry the bad claims are dropped, so
a dropped claim is never shown as verified.

Limits
------
- The record is per process. A caller with no ``HERMES_KANBAN_TASK`` (the
  gateway completing a child card) has no record, so its claims are refused
  once and then dropped.
- The record holds the output the model saw: after the head-and-tail cut. An
  excerpt read later from a spill file is not in it.
- Background processes are not recorded.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The card a dispatcher-spawned worker is running, as the dispatcher sets it.
TASK_ENV = "HERMES_KANBAN_TASK"

#: The parameter ``kanban_complete`` takes, and the ``metadata`` key it fills.
PARAM = "verified_claims"
METADATA_KEY = "verified_claims"

#: The most terminal calls kept per card. Each holds output already cut to
#: Hermes' terminal output limit; a worker that runs more than this has its
#: oldest calls forgotten, and a claim citing one is refused like any other.
MAX_RECORDED_CALLS = 200

#: The most claims one completion may carry, and the longest excerpt and claim.
#: The evidence section is appended to ``result``, which the notifier clips at
#: ``kanban_notifier.RESULT_LIMIT`` (30000); these keep the section well
#: inside it.
MAX_CLAIMS = 10
MAX_EXCERPT_CHARS = 600
MAX_CLAIM_CHARS = 300

#: How long a refusal waits for its retry. The same window as
#: ``kanban_result_required.NUDGE_TTL_SECONDS``; see that module for why the
#: memory expires.
NUDGE_TTL_SECONDS = 15 * 60

#: The longest piece of a bad claim quoted back in the refusal.
QUOTE_CHARS = 80

#: ``ran_at``'s format: UTC, to the second.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

EVIDENCE_HEADING = "**Evidence for verified claims**"
FENCE = "```"
WHITESPACE = re.compile(r"\s+")

PARAM_DESCRIPTION = (
    "Optional. Every claim you present as verified, checked or confirmed, with "
    "the command that proves it. Each item is {claim, command, excerpt}: "
    "``command`` is a terminal command you ran in this card, copied exactly; "
    "``excerpt`` is the lines of its output that prove the claim, copied "
    "exactly (at most 600 characters). The tool checks that the command ran and "
    "the excerpt is in its output, adds the exit code and the time it ran, and "
    "appends the evidence to ``result``. A claim that fails the check is "
    "refused, and is dropped on the retry. Do not call anything verified in "
    "``result`` unless it is listed here."
)

PARAM_SCHEMA = {
    "type": "array",
    "maxItems": MAX_CLAIMS,
    "items": {
        "type": "object",
        "properties": {
            "claim": {"type": "string", "description": "The claim, in one sentence."},
            "command": {
                "type": "string",
                "description": "The terminal command that proves it, exactly as you ran it.",
            },
            "excerpt": {
                "type": "string",
                "description": "The lines of that command's output that prove it, copied exactly.",
            },
        },
        "required": ["claim", "command", "excerpt"],
    },
    "description": PARAM_DESCRIPTION,
}

REFUSAL_HEAD = (
    "kanban_complete refused: {count} problem(s) below. Your task is still in "
    "flight. Fix each one and call kanban_complete again with the same result. "
    "The next call is accepted: a verified claim that still fails is dropped, "
    "not shown as verified."
)

#: Card id to the calls it ran, oldest first.
_calls: dict[str, deque] = {}

#: Card id to ``time.monotonic()`` of its last refusal.
_refused_at: dict[str, float] = {}


def _collapse(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def _quote(text: object) -> str:
    text = _collapse(str(text))
    return text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1] + "…"


def record_terminal_call(command: object, exit_code: object, output: object, now: float | None = None) -> None:
    """Remember one foreground terminal call of the current card, if there is one."""
    task_id = os.environ.get(TASK_ENV)
    if not task_id or not isinstance(command, str) or not command.strip():
        return
    calls = _calls.setdefault(task_id, deque(maxlen=MAX_RECORDED_CALLS))
    calls.append(
        {
            "command": _collapse(command),
            "exit_code": exit_code,
            "output": _collapse(str(output or "")),
            "ran_at": datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).strftime(TIME_FORMAT),
        }
    )


def _check_claim(calls: list, item: object) -> tuple[dict | None, str | None]:
    """Return ``(accepted, None)`` or ``(None, reason)`` for one claim."""
    if not isinstance(item, dict):
        return None, "is not an object with claim, command and excerpt"
    for field in ("claim", "command", "excerpt"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            return None, f"has no {field}"
    claim, command, excerpt = item["claim"].strip(), item["command"], item["excerpt"]
    if len(claim) > MAX_CLAIM_CHARS:
        return None, f"claim is longer than {MAX_CLAIM_CHARS} characters"
    if len(excerpt) > MAX_EXCERPT_CHARS:
        return None, f"excerpt is longer than {MAX_EXCERPT_CHARS} characters; keep the lines that prove the claim"
    wanted = _collapse(command)
    runs = [call for call in calls if call["command"] == wanted]
    if not runs:
        return None, f"command {_quote(command)!r} did not run in this card; copy it exactly as you ran it"
    needle = _collapse(excerpt)
    for call in reversed(runs):
        if needle in call["output"]:
            return {
                "claim": claim,
                "command": command.strip(),
                "excerpt": excerpt.strip("\n"),
                "exit_code": call["exit_code"],
                "ran_at": call["ran_at"],
            }, None
    return None, f"excerpt {_quote(excerpt)!r} is not in the output of {_quote(command)!r}; copy it exactly"


def render_evidence(claims: list) -> str:
    """The section appended to ``result``: each claim, then its command and excerpt."""
    parts = [EVIDENCE_HEADING]
    for claim in claims:
        parts.append(f"{claim['claim']} (exit {claim['exit_code']}, {claim['ran_at']})")
        parts.append(f"{FENCE}\n$ {claim['command']}\n{claim['excerpt']}\n{FENCE}")
    return "\n\n".join(parts)


def card_terminal_calls(task_id: object) -> list[dict]:
    """The terminal calls recorded for ``task_id``, oldest first.

    Each is ``{command, exit_code, output, ran_at}``, with whitespace in
    ``command`` and ``output`` collapsed. The read API for a completion rule.
    """
    return list(_calls.get(str(task_id), ()))


def verified_claims_rule(*, args: dict, result: object, metadata: object, calls: list, retry: bool, redact, **_) -> tuple[list, object, object]:
    """The completion rule for ``verified_claims``; see :data:`RULES` for the contract.

    On the retry the claims that fail are dropped and the rest are kept, so a
    dropped claim is never shown as verified.
    """
    claims = args.get(PARAM)
    if claims is None or claims == []:
        return [], result, metadata
    items = claims if isinstance(claims, list) else [claims]

    accepted, reasons = [], []
    for index, item in enumerate(items):
        if index >= MAX_CLAIMS:
            claim, reason = None, f"is past the limit of {MAX_CLAIMS} claims"
        else:
            claim, reason = _check_claim(calls, item)
        if reason:
            label = _quote(item.get("claim", "")) if isinstance(item, dict) else ""
            reasons.append(f"verified_claims[{index}] {label!r}: {reason}")
        else:
            accepted.append(claim)

    if (reasons and not retry) or not accepted:
        return reasons, result, metadata
    if metadata is not None and not isinstance(metadata, dict):
        # Upstream's _require_dict_metadata refuses this next; nothing to add to.
        return reasons, result, metadata
    stored = [
        {**claim, **{field: redact(claim[field]) for field in ("claim", "command", "excerpt")}}
        for claim in accepted
    ]
    metadata = {**(metadata or {}), METADATA_KEY: stored}
    section = render_evidence(stored)
    body = str(result).rstrip() if result else ""
    result = f"{body}\n\n{section}" if body else section
    return reasons, result, metadata


#: The rules :func:`apply_completion_rules` runs, in order. Each is called with
#: keyword arguments ``task_id``, ``args`` (the ``kanban_complete`` arguments),
#: ``result``, ``metadata``, ``calls`` (:func:`card_terminal_calls`), ``retry``
#: and ``redact``, and returns ``(reasons, result, metadata)``. ``reasons`` is
#: the list of problems, each one line the worker can act on. ``retry`` is true
#: when this card was refused inside :data:`NUDGE_TTL_SECONDS`: the completion
#: is then accepted whatever the reasons, so a rule must return a ``result``
#: and ``metadata`` that are safe to store as they are. A rule should accept
#: ``**kwargs`` so that a new argument does not break it.
RULES: list = [verified_claims_rule]


def apply_completion_rules(
    task_id: object,
    args: dict,
    result: object,
    metadata: object,
    redact=lambda value: value,
) -> tuple[str | None, object, object]:
    """Run every rule in :data:`RULES` on one ``kanban_complete`` call.

    Returns ``(error, result, metadata)``. When ``error`` is set the completion
    must not be written. One refusal per card covers every rule: the first
    completion with a problem is refused with all the reasons in one message,
    and the retry is accepted. ``redact`` is applied to every string a rule
    stores, as ``kanban_complete`` applies it to ``result``.
    """
    key = str(task_id)
    refused_at = _refused_at.pop(key, None)
    retry = refused_at is not None and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    calls = card_terminal_calls(key)

    reasons, new_result, new_metadata = [], result, metadata
    for rule in RULES:
        found, new_result, new_metadata = rule(
            task_id=key, args=args, result=new_result, metadata=new_metadata,
            calls=calls, retry=retry, redact=redact,
        )
        reasons += found

    if reasons and not retry:
        _refused_at[key] = time.monotonic()
        lines = [REFUSAL_HEAD.format(count=len(reasons))] + [f"- {reason}" for reason in reasons]
        return "\n".join(lines), result, metadata
    for reason in reasons:
        logger.warning("[kanban] card %s: accepted on the retry with: %s", key, reason)
    return None, new_result, new_metadata


def apply_schema(schema: dict) -> dict:
    """Add the ``verified_claims`` parameter to ``KANBAN_COMPLETE_SCHEMA`` in place."""
    properties = schema["parameters"]["properties"]
    if PARAM in properties:
        raise RuntimeError(f"kanban_complete already has a {PARAM!r} parameter; re-derive this patch")
    properties[PARAM] = PARAM_SCHEMA
    return schema
