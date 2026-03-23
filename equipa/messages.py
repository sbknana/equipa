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

from equipa.db import ensure_schema, get_db_connection


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
        conn = get_db_connection(write=True)
        conn.execute(
            """INSERT INTO agent_messages
               (task_id, cycle_number, from_role, to_role, message_type, content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, cycle, from_role, to_role, msg_type, content),
        )
        conn.commit()
        conn.close()
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
        conn = get_db_connection()
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
        conn.close()
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
        conn = get_db_connection(write=True)
        conn.execute(
            """UPDATE agent_messages
               SET read_by_cycle = ?
               WHERE task_id = ? AND to_role = ? AND read_by_cycle IS NULL""",
            (cycle_number, task_id, to_role),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [Messages] WARNING: Failed to mark messages as read: {e}")


def format_messages_for_prompt(messages: list[dict]) -> str:
    """Format agent messages into a prompt-friendly string."""
    if not messages:
        return ""

    from equipa.security import _make_untrusted_delimiter, wrap_untrusted

    _delim = _make_untrusted_delimiter()
    lines = ["## Messages from Other Agents\n"]
    for msg in messages:
        from_role = msg.get("from_role", "unknown")
        msg_type = msg.get("message_type", "unknown")
        content = msg.get("content", "")
        cycle = msg.get("cycle_number", "?")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                content_str = ", ".join(f"{k}: {v}" for k, v in parsed.items())
            else:
                content_str = str(parsed)
        except (json.JSONDecodeError, TypeError):
            content_str = content
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
