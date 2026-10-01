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

from equipa.db import db_conn, ensure_schema


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
) -> list[dict]:
    """Fetch unread messages for a given role on a task."""
    try:
        ensure_schema()
        with db_conn() as conn:
            if max_cycle is not None:
                rows = conn.execute(
                    """SELECT id, task_id, cycle_number, from_role, to_role,
                              message_type, content, created_at
                       FROM agent_messages
                       WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL
                             AND cycle_number <= ?
                       ORDER BY cycle_number ASC, id ASC""",
                    (task_id, to_role, max_cycle),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, task_id, cycle_number, from_role, to_role,
                              message_type, content, created_at
                       FROM agent_messages
                       WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL
                       ORDER BY cycle_number ASC, id ASC""",
                    (task_id, to_role),
                ).fetchall()
        return [dict(row) for row in rows]
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


# How much of one message is scanned and shown. sanitize() costs up to about
# 0.2 s at its 64k input cap, and a prompt can carry several messages, so
# longer content is cut, with a marker, before scanning.
MESSAGE_SCAN_LIMIT: int = 8_000


def _sanitize_message_part(text: object) -> str:
    """Reject-mode sanitize one agent-authored message part.

    A rejected part becomes AGENT_OUTPUT_WITHHELD; an accepted one has every
    ``<`` / ``>`` escaped, so it cannot close the block it is shown in.
    """
    from lesson_sanitizer import sanitize  # HARD dependency
    from equipa.parsing import AGENT_OUTPUT_WITHHELD

    text = str(text)
    if len(text) > MESSAGE_SCAN_LIMIT:
        dropped = len(text) - MESSAGE_SCAN_LIMIT
        text = f"{text[:MESSAGE_SCAN_LIMIT]}\n[... {dropped} chars not shown]"
    return sanitize(text, label="agent message") or AGENT_OUTPUT_WITHHELD


def _message_content(content: object) -> str:
    """Render message *content* (JSON or plain text) for a prompt, sanitized.

    A JSON object is shown as ``key: value`` pairs, each checked on its own,
    so one hostile value (a Tester failure line) withholds only that pair.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return _sanitize_message_part(content)
    if isinstance(parsed, dict):
        return ", ".join(
            _sanitize_message_part(f"{key}: {value}")
            for key, value in parsed.items()
        )
    return _sanitize_message_part(parsed)


def format_messages_for_prompt(messages: list[dict]) -> str:
    """Format agent messages into a prompt-friendly string.

    Message content is agent-authored: it reaches this table from Tester
    output (loops.py posts raw failure details) or another agent. Each
    message is therefore reject-mode sanitized and boundary-escaped like
    every other agent-authored text before it goes in its ``<task-input>``
    block and per-prompt ``<<<UNTRUSTED_*>>>`` delimiter, so it can neither
    close the block nor end the delimiter (review F4 of task 3139). The role,
    type and cycle labels are boundary-escaped too.
    """
    if not messages:
        return ""

    from lesson_sanitizer import neutralize_boundaries  # HARD dependency
    from equipa.security import _make_untrusted_delimiter, wrap_untrusted

    _delim = _make_untrusted_delimiter()
    lines = ["## Messages from Other Agents\n"]
    for msg in messages:
        from_role = neutralize_boundaries(str(msg.get("from_role", "unknown")))
        msg_type = neutralize_boundaries(str(msg.get("message_type", "unknown")))
        cycle = neutralize_boundaries(str(msg.get("cycle_number", "?")))
        content_str = _message_content(msg.get("content", ""))
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
