"""Refuse "done" on a fleet-audit card whose run published no ledger.

Installed into the image at ``/opt/hermes/tools/kanban_audit_ledger.py`` and
wired into ``tools/kanban_tools.py`` by
``deploy/docker/patches/apply_kanban_audit_ledger.py``.

Why
---
A fleet-audit stream publishes its report as a ledger issue:
``audit_report.py start`` opens the run and ``finish`` publishes it, printing
one JSON line with ``status`` and ``issue_url``. The skill says a card whose
audit produced no ledger must not close ``done`` (``fleet-audit/SKILL.md``),
but that is an instruction only. Issue #2856 measured the cost: on the
``compliance-rbac-overgrant`` case more than half of the repetitions closed
the card with a chat summary ("audit complete, 24 findings") and no ledger.
The worker ran ``start``, then stopped when ``collect.py`` timed out, or ran
``finish`` and did not read its rejection, or started ``finish`` in the
background and never read it, and then called ``kanban_complete`` anyway. The
user was told the audit was done, sometimes that the ledger was published, and
no issue existed.

What this changes
-----------------
At ``kanban_complete`` the gate reads this card's own worker session from
Hermes's session store and looks for two things: a successful ``audit_report.py
start``, and after it a ``finish`` result line (a JSON object with ``status``
and ``issue_url``).

* No successful ``start``: not an audit run. Nothing changes.
* A ``finish`` line with an ``issue_url`` that ``result`` does not contain:
  the URL is appended to ``result``. The ledger exists; the report just did
  not name it.
* A ``finish`` line with a null ``issue_url`` (``NO_FORGE``, or a ``CLEAN``
  run with no earlier ledger): accepted unchanged. These runs have no URL to
  give.
* ``start`` with no ``finish`` line after it: refused once, with the next
  step. The submitted ``result`` is saved on the card first, as
  ``kanban_children_settled`` does, so a run that ends after the refusal does
  not lose the report.
* The retry, still with no ``finish`` line: accepted, with ``result``
  prefixed by a "Not published" line. The card never wedges, and the person
  who asked reads that no ledger exists instead of a report that implies one.

Why the session store
---------------------
``audit_report.py`` runs in the agent's shell. On a sandboxed install that is
the sandbox pod, whose ``/opt/data`` is a separate volume, so the run's lease
and report files are not visible from the gateway pod where this handler runs
(``docs/designs/fleet-audit-report-store.md``). What the worker process does
see is every tool call and its result, and Hermes writes each tool round to
``state.db`` before the next model call. The worker's session id is
``HERMES_SESSION_ID``, rebound to the child session after a compression;
this gate also walks compression parents and children, so a long audit turn
that compressed is read whole.

Scope and failure
-----------------
The gate runs only for the caller's own card (``HERMES_KANBAN_TASK`` equal to
the card id). A gateway session closing someone else's card, the CLI and the
scheduler are not affected, and a scheduled audit is a cron run with no card.
Every read fails open: a missing or unreadable store, an unknown schema or a
tool output that a turn budget moved out of the message lets the completion
through unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing

logger = logging.getLogger(__name__)

#: The env var the dispatcher pins into every worker it spawns: the id of the
#: card the worker is running.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: The worker's session id. Read through ``gateway.session_context`` when it
#: exists, because a compression rebinds the ContextVar there, not os.environ.
SESSION_ID_ENV = "HERMES_SESSION_ID"

HERMES_HOME_ENV = "HERMES_HOME"
STATE_DB_NAME = "state.db"
DB_TIMEOUT_SECONDS = 2.0

#: ``sessions.end_reason`` of a session that a compression continued in a
#: child (the value ``kanban_event_routing`` walks on).
COMPRESSION_END_REASON = "compression"
#: Upper bound on the compression links walked in each direction.
COMPRESSION_WALK_MAX = 32

#: Prefix ``hermes_state`` puts on list or dict message content it stored as
#: JSON (``SessionDB._CONTENT_JSON_PREFIX``).
CONTENT_JSON_PREFIX = "\x00json:"

#: How long a refusal stays on file: the same window as the other completion
#: gates (``DEFAULT_CLAIM_TTL_SECONDS`` in ``hermes_cli/kanban_db.py``).
NUDGE_TTL_SECONDS = 15 * 60

#: A call that runs the audit lifecycle's ``start``.
START_RE = re.compile(r"audit_report\.py\b[^\n]*?\sstart\b")
#: The stream a ``start`` names.
AUDIT_ID_RE = re.compile(r"--audit[=\s]+[\"']?([A-Za-z0-9][A-Za-z0-9_-]*)")
#: Text used when the ``start`` command names no stream.
UNKNOWN_AUDIT = "this"

#: The two keys every ``finish`` result line carries, published or not.
FINISH_KEYS = ("status", "issue_url")

_MESSAGES_SQL = (
    "SELECT id, role, content, tool_call_id, tool_calls FROM messages "
    "WHERE session_id IN ({marks}) ORDER BY id"
)
_PARENT_SQL = (
    "SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
    "WHERE s.id = ? AND p.end_reason = ?"
)
_CHILD_SQL = (
    "SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
    "WHERE p.id = ? AND p.end_reason = ? ORDER BY c.started_at"
)

REFUSAL = (
    "This card ran `audit_report.py start --audit {audit}`, but no `finish` "
    "result line came back after it, so the {audit} ledger is not published "
    "and this report has no ledger URL. Your result is saved on the card. "
    "Run `audit_report.py finish` for {audit} in the foreground, not as a "
    "background process, and read the JSON line it prints. If it prints an "
    "`issue_url`, call kanban_complete again with that URL in `result`. If "
    "`finish` exits 2, fix what its error names and run `finish` again; do not "
    "run `start` again. If the audit cannot publish, call kanban_block with "
    "the reason instead of kanban_complete."
)

NOT_PUBLISHED_PREFIX = (
    "⚠️ **Not published:** the {audit} audit ran `start`, but no `finish` "
    "published its ledger, so no ledger issue exists for this report.\n\n"
)

LEDGER_LINE = "\n\nLedger: {url}"

#: Cards refused once and not yet completed, against ``time.monotonic()`` at
#: the refusal. Popped by the completion that answers them; see
#: ``kanban_result_required`` for why the memory must expire.
_refused_at: dict[str, float] = {}


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:  # not inside Hermes (tests), or a Hermes without it
        return os.environ.get(name, "")


def state_db_path() -> str:
    """Hermes's session store for this process's profile, or "" when unknown."""
    home = ""
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home() or "")
    except Exception:  # not inside Hermes (tests), or a Hermes without it
        home = os.environ.get(HERMES_HOME_ENV, "").strip()
    return os.path.join(home, STATE_DB_NAME) if home else ""


def _lineage(conn, session_id: str) -> list[str]:
    """``session_id`` plus the sessions it continues and is continued by."""
    found = [session_id]
    for sql in (_PARENT_SQL, _CHILD_SQL):
        current = session_id
        for _ in range(COMPRESSION_WALK_MAX):
            row = conn.execute(sql, (current, COMPRESSION_END_REASON)).fetchone()
            if not row or not row[0] or row[0] in found:
                break
            current = row[0]
            found.append(current)
    return found


def _decode(content):
    """A stored message's content as Python data where it is JSON, else text."""
    if content is None:
        return ""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    text = str(content)
    if text.startswith(CONTENT_JSON_PREFIX):
        text = text[len(CONTENT_JSON_PREFIX):]
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _command_text(call) -> str:
    """The command a tool call ran, or its raw arguments."""
    if not isinstance(call, dict):
        return ""
    function = call.get("function") or {}
    args = function.get("arguments") if isinstance(function, dict) else None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    if isinstance(args, dict):
        command = args.get("command")
        return command if isinstance(command, str) else json.dumps(args)
    return ""


def _exit_code(result):
    return result.get("exit_code") if isinstance(result, dict) else None


def _output_text(result) -> str:
    if isinstance(result, dict):
        output = result.get("output")
        return output if isinstance(output, str) else ""
    return result if isinstance(result, str) else ""


def _finish_line(result):
    """The last ``finish`` result line in a tool result, or None."""
    for line in reversed(_output_text(result).splitlines()):
        text = line.strip()
        if not text.startswith("{") or FINISH_KEYS[1] not in text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict) and all(k in parsed for k in FINISH_KEYS):
            return parsed
    return None


def audit_run(rows) -> tuple[str | None, dict | None]:
    """``(audit, finish)`` for the last successful ``start`` in ``rows``.

    ``rows`` are ``(id, role, content, tool_call_id, tool_calls)`` in id
    order. ``audit`` is ``None`` when no ``start`` succeeded. ``finish`` is the
    last ``finish`` result line after that ``start``, or ``None``. A later
    successful ``start`` begins a new run and forgets an earlier ``finish``.
    """
    pending: dict[str, str] = {}
    audit = None
    finish = None
    for _row_id, role, content, tool_call_id, tool_calls in rows:
        if role == "assistant" and tool_calls:
            calls = _decode(tool_calls)
            for call in calls if isinstance(calls, list) else []:
                command = _command_text(call)
                if START_RE.search(command) and isinstance(call, dict) and call.get("id"):
                    match = AUDIT_ID_RE.search(command)
                    pending[str(call["id"])] = match.group(1) if match else UNKNOWN_AUDIT
            continue
        if role != "tool":
            continue
        result = _decode(content)
        started = pending.pop(str(tool_call_id), None) if tool_call_id else None
        if started is not None:
            if _exit_code(result) == 0:
                audit, finish = started, None
            continue
        if audit is not None:
            line = _finish_line(result)
            if line is not None:
                finish = line
    return audit, finish


def _read_rows(state_db: str, session_id: str) -> list:
    with closing(
        sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)
    ) as conn:
        sessions = _lineage(conn, session_id)
        marks = ",".join("?" for _ in sessions)
        return conn.execute(_MESSAGES_SQL.format(marks=marks), sessions).fetchall()


def _stash_submitted_result(connect, task_id: str, result) -> None:
    """Save a refused completion's ``result`` on the card, status untouched.

    Raises on failure; the caller then accepts the completion, because a
    refusal that has not saved what it refuses can lose the report.
    """
    text = "" if result is None else str(result)
    if not text.strip():
        return
    conn = connect()
    try:
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (text, task_id))
        if getattr(conn, "in_transaction", False):
            conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def require_audit_ledger(task_id, connect, result, state_db=None):
    """The completion gate. Returns ``(error, result_to_store)``.

    ``error`` is ``None`` when the completion may proceed with
    ``result_to_store``. ``connect`` is ``hermes_cli.kanban_db_connect.connect``,
    injected by the applier's trailer, used only to save a refused result.
    """
    key = str(task_id or "").strip()
    if not key or os.environ.get(WORKER_TASK_ENV) != key:
        return None, result
    session_id = _session_env(SESSION_ID_ENV).strip()
    path = state_db if state_db is not None else state_db_path()
    if not session_id or not path or not os.path.exists(path):
        return None, result
    refused_at = _refused_at.pop(key, None)
    nudge_spent = (
        refused_at is not None and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    )
    try:
        audit, finish = audit_run(_read_rows(path, session_id))
    except Exception as exc:  # noqa: BLE001 — sqlite, schema, JSON: fail open
        logger.warning(
            "kanban_audit_ledger: could not read %s's session %s (allowing the "
            "completion): %r", key, session_id, exc,
        )
        return None, result
    if audit is None:
        return None, result
    text = "" if result is None else str(result)
    if finish is not None:
        url = finish.get(FINISH_KEYS[1])
        if url and str(url) not in text:
            return None, text + LEDGER_LINE.format(url=url)
        return None, result
    if nudge_spent:
        logger.warning(
            "kanban_audit_ledger: %s is completing the %s audit with no finish "
            "result on its retry; marking it not published", key, audit,
        )
        return None, NOT_PUBLISHED_PREFIX.format(audit=audit) + text
    try:
        _stash_submitted_result(connect, key, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "kanban_audit_ledger: could not save %s's submitted result "
            "(allowing the completion, marked not published): %r", key, exc,
        )
        return None, NOT_PUBLISHED_PREFIX.format(audit=audit) + text
    _refused_at[key] = time.monotonic()
    return REFUSAL.format(audit=audit), result
