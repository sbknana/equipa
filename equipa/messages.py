"""EQUIPA messages module — inter-agent messaging via TheForge DB.

Layer 3: Depends on equipa.db (get_db_connection, ensure_schema) and
monolith functions (_make_untrusted_delimiter, wrap_untrusted) for
content isolation.

Extracted from forge_orchestrator.py as part of Phase 2 monolith split.
Updated in Phase 3 to import from equipa.db instead of late monolith imports.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import time

from equipa.db import db_conn, ensure_schema

# Bounds of the message channel (RR3145-A). Agents can INSERT into
# agent_messages through the theforge MCP server, so the number, size and
# shape of messages are attacker-chosen, and the prompt is built
# synchronously in the orchestrator loop.
#
# How much of one message is scanned and shown. sanitize() costs about
# 0.04 s on 8k of its slowest input, so longer content is cut, with a marker,
# before it is parsed or scanned.
MESSAGE_SCAN_LIMIT: int = 8_000
# Unread messages read (and shown) per prompt build: the newest ones.
MESSAGE_READ_LIMIT: int = 10
# Sanitize time per prompt build. Checked between messages and between the
# fields of a rejected message; whatever is left is not shown.
MESSAGES_TIME_BUDGET_SECONDS: float = 0.2
_FIELD_SEPARATOR = ", "


def post_agent_message(
    task_id: int,
    cycle: int,
    from_role: str,
    to_role: str,
    msg_type: str,
    content: str,
) -> None:
    """Insert a structured message from one agent role to another."""
    try:
        ensure_schema()
        with db_conn(write=True) as conn:
            conn.execute(
                """INSERT INTO agent_messages
                   (task_id, cycle_number, from_role, to_role, message_type, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, cycle, from_role, to_role, msg_type, content),
            )
    except Exception as e:
        print(f"  [Messages] WARNING: Failed to post agent message: {e}")


def read_agent_messages(
    task_id: int,
    to_role: str,
    max_cycle: int | None = None,
    limit: int = MESSAGE_READ_LIMIT,
) -> list[dict]:
    """Fetch unread messages for a given role on a task, oldest first.

    At most ``limit`` messages are returned: the newest ones (RR3145-A).
    Older unread messages beyond that are reported in a warning and not
    returned; mark_messages_read still consumes them.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    try:
        ensure_schema()
        with db_conn() as conn:
            # One row past the limit tells whether anything was left out.
            if max_cycle is not None:
                rows = conn.execute(
                    """SELECT id, task_id, cycle_number, from_role, to_role,
                              message_type, content, created_at
                       FROM agent_messages
                       WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL
                             AND cycle_number <= ?
                       ORDER BY cycle_number DESC, id DESC
                       LIMIT ?""",
                    (task_id, to_role, max_cycle, limit + 1),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, task_id, cycle_number, from_role, to_role,
                              message_type, content, created_at
                       FROM agent_messages
                       WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL
                       ORDER BY cycle_number DESC, id DESC
                       LIMIT ?""",
                    (task_id, to_role, limit + 1),
                ).fetchall()
        if len(rows) > limit:
            print(f"  [Messages] WARNING: more than {limit} unread messages for "
                  f"{to_role} on task {task_id}; only the newest {limit} are "
                  f"shown")
            rows = rows[:limit]
        return [dict(row) for row in reversed(rows)]
    except Exception as e:
        print(f"  [Messages] WARNING: Failed to read agent messages: {e}")
        return []


def mark_messages_read(
    task_id: int, to_role: str, cycle_number: int
) -> None:
    """Mark all unread messages for a role as consumed by a given cycle."""
    try:
        ensure_schema()
        with db_conn(write=True) as conn:
            conn.execute(
                """UPDATE agent_messages
                   SET read_by_cycle = ?
                   WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL""",
                (cycle_number, task_id, to_role),
            )
    except Exception as e:
        print(f"  [Messages] WARNING: Failed to mark messages as read: {e}")


def _cap_text(text: str, limit: int = MESSAGE_SCAN_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {len(text) - limit} chars not shown]"


def _sanitize_message_part(text: object) -> str:
    """Reject-mode sanitize one agent-authored message part, cut to
    MESSAGE_SCAN_LIMIT first.

    A rejected part becomes AGENT_OUTPUT_WITHHELD; an accepted one has every
    ``<`` / ``>`` escaped, so it cannot close the block it is shown in.
    """
    from lesson_sanitizer import sanitize  # HARD dependency
    from equipa.parsing import AGENT_OUTPUT_WITHHELD

    text = "" if text is None else str(text)
    if not text.strip():
        return ""
    return sanitize(_cap_text(text), label="agent message") or AGENT_OUTPUT_WITHHELD


def _capped_fields(parsed: dict) -> tuple[list[str], int]:
    """``key: value`` fields of *parsed* whose joined length stays within
    MESSAGE_SCAN_LIMIT (the last one cut to fit), and how many were left out.
    """
    fields: list[str] = []
    remaining = MESSAGE_SCAN_LIMIT
    items = list(parsed.items())
    for index, (key, value) in enumerate(items):
        if remaining <= 0:
            return fields, len(items) - index
        field = f"{key}: {value}"
        if len(field) > remaining:
            field = field[:remaining]
        fields.append(field)
        remaining -= len(field) + len(_FIELD_SEPARATOR)
    return fields, 0


def _recheck_fields(fields: list[str], deadline: float) -> str:
    """Sanitize each field of a rejected message on its own, so one hostile
    value (a Tester failure line) withholds only that field (review F4 of
    3139). The fields are slices of the capped payload, so together they
    are no longer than the pass that rejected them; the time budget is
    checked between fields."""
    from equipa.parsing import AGENT_OUTPUT_WITHHELD

    shown: list[str] = []
    for index, field in enumerate(fields):
        if time.monotonic() > deadline:
            shown.append(f"{AGENT_OUTPUT_WITHHELD} [{len(fields) - index} "
                         f"field(s) not checked: time budget spent]")
            break
        shown.append(_sanitize_message_part(field))
    return _FIELD_SEPARATOR.join(shown)


def _message_content(content: object, deadline: float | None = None) -> str:
    """Render message *content* (JSON or plain text) for a prompt, sanitized.

    The content is cut to MESSAGE_SCAN_LIMIT before it is parsed (a cut
    JSON object is shown as plain text). A JSON object is shown as
    ``key: value`` fields joined into one payload, also within
    MESSAGE_SCAN_LIMIT, and that payload is sanitized in one pass
    (RR3145-A: one pass per field made one message of 200 large fields cost
    seconds). Only when the joined payload is rejected are its fields
    re-checked one by one, inside the same capped payload, so a hostile
    value withholds only its own field.
    """
    text = "" if content is None else str(content)
    if not text.strip():
        return ""
    if len(text) > MESSAGE_SCAN_LIMIT:
        return _sanitize_message_part(text)
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        # Not JSON, or JSON that cannot be loaded (an integer past
        # sys.get_int_max_str_digits, nesting past the recursion limit).
        return _sanitize_message_part(text)
    if not isinstance(parsed, dict):
        return _sanitize_message_part(parsed)
    fields, left_out = _capped_fields(parsed)
    if left_out:
        fields.append(f"[... {left_out} more field(s) not shown]")
    whole = _sanitize_message_part(_FIELD_SEPARATOR.join(fields))
    from equipa.parsing import AGENT_OUTPUT_WITHHELD
    if whole != AGENT_OUTPUT_WITHHELD or len(fields) < 2:
        return whole
    if deadline is None:
        deadline = time.monotonic() + MESSAGES_TIME_BUDGET_SECONDS
    return _recheck_fields(fields, deadline)


def format_messages_for_prompt(messages: list[dict]) -> str:
    """Format agent messages into a prompt-friendly string.

    Message content is agent-authored: it reaches this table from Tester
    output (loops.py posts raw failure details) or another agent. Each
    message is therefore reject-mode sanitized and boundary-escaped like
    every other agent-authored text before it goes in its ``<task-input>``
    block and per-prompt ``<<<UNTRUSTED_*>>>`` delimiter, so it can neither
    close the block nor end the delimiter (review F4 of task 3139). The role,
    type and cycle labels are boundary-escaped too.

    The cost is bounded (RR3145-A): only the newest MESSAGE_READ_LIMIT
    messages are shown, each is scanned within MESSAGE_SCAN_LIMIT, and once
    MESSAGES_TIME_BUDGET_SECONDS of scanning is spent the remaining messages
    are left out with a note saying so.
    """
    if not messages:
        return ""

    from lesson_sanitizer import neutralize_boundaries  # HARD dependency
    from equipa.security import _make_untrusted_delimiter, wrap_untrusted

    deadline = time.monotonic() + MESSAGES_TIME_BUDGET_SECONDS
    _delim = _make_untrusted_delimiter()
    lines = ["## Messages from Other Agents\n"]
    shown = messages[-MESSAGE_READ_LIMIT:]
    if len(messages) > len(shown):
        lines.append(f"[{len(messages) - len(shown)} earlier message(s) not "
                     f"shown]")
    for index, msg in enumerate(shown):
        if index and time.monotonic() > deadline:
            lines.append(f"[{len(shown) - index} more message(s) not shown: "
                         f"the time budget for scanning messages is spent]")
            break
        from_role = neutralize_boundaries(str(msg.get("from_role", "unknown")))
        msg_type = neutralize_boundaries(str(msg.get("message_type", "unknown")))
        cycle = neutralize_boundaries(str(msg.get("cycle_number", "?")))
        content_str = _message_content(msg.get("content", ""), deadline)
        # Wrap inter-agent message content in untrusted markers — these come
        # from agent_messages table and could contain prompt injection from a
        # compromised agent (addresses EQ-24 variant for inter-agent channel).
        wrapped = wrap_untrusted(content_str, _delim)
        lines.append(
            f'<task-input type="agent-message" trust="derived">\n'
            f"**[{from_role}]** (cycle {cycle}, {msg_type}): {wrapped}\n"
            f"</task-input>"
        )
    return "\n".join(lines)
