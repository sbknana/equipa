"""EQUIPA orchestrator-cycle session capture and restore.

Implements PLAN-1067 §2.B2 (Paperclip): persistence of in-flight agent state
across cycle boundaries (heartbeat ticks, flow revisions, crash/kill, context
compaction). Sits one layer above ``equipa.checkpoints`` — a soft checkpoint
is a within-task snapshot, an ``agent_sessions`` row is the cross-cycle
superset.

Tie-breaker (operator decision): when both exist for the same (task_id, role),
the session record wins. ``build_resume_prompt`` called on session state
encompasses everything ``build_compaction_recovery_context`` would produce.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from equipa.checkpoints import (
    SOFT_CHECKPOINT_TEXT_LIMIT,
    _format_recovery_prompt,
    load_soft_checkpoint,
)
from equipa.db import db_conn

# 32 KB total cap on state_json per PLAN-1067 §2.B2.
STATE_CAP_BYTES: int = 32 * 1024

# Default partial_reasoning truncation cap — mirrors the soft-checkpoint
# text cap so the two snapshot tiers behave consistently when one is
# promoted into the other.
PARTIAL_REASONING_LIMIT: int = SOFT_CHECKPOINT_TEXT_LIMIT

# Operator decision: 14-day TTL on captured sessions.
SESSION_TTL_DAYS: int = 14


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_byte_size(state: dict[str, Any]) -> int:
    """Return the UTF-8 byte length of ``state`` serialized as JSON."""
    return len(json.dumps(state, sort_keys=True).encode("utf-8"))


def _truncate_state(
    state: dict[str, Any],
    cap_bytes: int = STATE_CAP_BYTES,
) -> dict[str, Any]:
    """Apply the documented truncation order to fit ``state`` under ``cap_bytes``.

    Order (per PLAN-1067 §2.B2):
      1. ``partial_reasoning`` truncated to :data:`PARTIAL_REASONING_LIMIT`
         characters (mirrors the soft-checkpoint pattern).
      2. If still over cap, drop oldest entries from ``recent_tool_calls``.
      3. As a last resort, halve ``partial_reasoning`` until under cap.

    Returns a new dict — never mutates the caller's state.
    """
    truncated: dict[str, Any] = dict(state)

    partial = truncated.get("partial_reasoning")
    if isinstance(partial, str) and len(partial) > PARTIAL_REASONING_LIMIT:
        truncated["partial_reasoning"] = (
            partial[:PARTIAL_REASONING_LIMIT] + "\n[...truncated...]"
        )

    if _state_byte_size(truncated) <= cap_bytes:
        return truncated

    tool_calls = list(truncated.get("recent_tool_calls", []))
    while tool_calls and _state_byte_size(truncated) > cap_bytes:
        tool_calls.pop(0)
        truncated["recent_tool_calls"] = tool_calls

    while _state_byte_size(truncated) > cap_bytes:
        partial = truncated.get("partial_reasoning") or ""
        if not isinstance(partial, str) or len(partial) <= 64:
            truncated["partial_reasoning"] = ""
            break
        truncated["partial_reasoning"] = partial[: len(partial) // 2]

    return truncated


def _load_forge_state(project_id: int) -> dict | None:
    """Load ``.forge-state.json`` from the project's ``local_path`` if present."""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT local_path FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
    except Exception:
        return None

    if not row:
        return None
    local_path = row["local_path"] if "local_path" in row.keys() else row[0]
    if not local_path:
        return None

    state_file = Path(local_path) / ".forge-state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_soft_checkpoint_from_path(path: Path) -> dict | None:
    """Load a specific soft-checkpoint JSON file, or ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _assemble_state(
    task_id: int,
    role: str,
    project_id: int,
    soft_checkpoint_path: Path | None,
) -> dict[str, Any]:
    """Build the orchestrator-cycle state dict for a task.

    The result is a strict superset of the soft-checkpoint shape with the
    Paperclip B2 additions (``open_files``, ``recent_tool_calls``,
    ``partial_reasoning``, ``soft_checkpoint_path``).
    """
    soft_cp: dict | None = None
    if soft_checkpoint_path is not None:
        soft_cp = _load_soft_checkpoint_from_path(soft_checkpoint_path)
    if soft_cp is None:
        soft_cp = load_soft_checkpoint(task_id=task_id, role=role)

    forge_state = _load_forge_state(project_id)

    # Lazy import — agent_runner pulls in the heavy SDK and we only need
    # the lightweight accessor.
    try:
        from equipa.agent_runner import get_recent_tool_calls
        recent_tool_calls = get_recent_tool_calls(task_id, role, n=20)
    except Exception:
        recent_tool_calls = []

    files_changed: list[str] = []
    files_read: list[str] = []
    open_files: list[str] = []
    turn_count = 0
    compaction_count = 0
    partial_reasoning = ""

    if soft_cp:
        files_changed = list(soft_cp.get("files_changed") or [])
        files_read = list(soft_cp.get("files_read") or [])
        turn_count = int(soft_cp.get("turn_count") or 0)
        compaction_count = int(soft_cp.get("compaction_count") or 0)
        partial_reasoning = str(soft_cp.get("last_result_text") or "")

    if forge_state:
        # forge-state's files_changed augments the soft-checkpoint set.
        for f in forge_state.get("files_changed") or []:
            if f not in files_changed:
                files_changed.append(f)
        for f in forge_state.get("files_read") or []:
            if f not in files_read:
                files_read.append(f)
        for f in forge_state.get("open_files") or []:
            if f not in open_files:
                open_files.append(f)

    state: dict[str, Any] = {
        "open_files": open_files,
        "files_changed": files_changed,
        "files_read": files_read,
        "recent_tool_calls": list(recent_tool_calls),
        "partial_reasoning": partial_reasoning,
        "turn_count": turn_count,
        "compaction_count": compaction_count,
        "soft_checkpoint_path": (
            str(soft_checkpoint_path) if soft_checkpoint_path else ""
        ),
    }

    # Carry forward the soft-checkpoint compaction signals if present —
    # build_resume_prompt may render them and we want round-trip fidelity.
    if soft_cp and soft_cp.get("compaction_signals"):
        state["compaction_signals"] = list(soft_cp["compaction_signals"])

    # Optional forge-state hints (used by build_resume_prompt).
    if forge_state:
        for key in ("current_step", "next_action", "decisions"):
            value = forge_state.get(key)
            if value:
                state.setdefault("forge_state", {})[key] = value

    return state


def capture(
    task_id: int,
    role: str,
    project_id: int,
    cycle_id: str,
    *,
    soft_checkpoint_path: Path | None,
) -> int:
    """Capture an orchestrator-cycle session for ``(task_id, role)``.

    Assembles state from (in priority order):
      1. The most recent soft checkpoint (or the explicit
         ``soft_checkpoint_path`` if provided).
      2. ``.forge-state.json`` in the project's ``local_path`` if present.
      3. The live tool-call ring buffer from ``equipa.agent_runner``.

    The serialized payload is capped at :data:`STATE_CAP_BYTES` (32 KB) per
    PLAN-1067 §2.B2 with truncation order ``partial_reasoning`` →
    ``recent_tool_calls``. ``expires_at`` is set to ``created_at +
    SESSION_TTL_DAYS`` per the operator decision.

    Returns the inserted row's ``id``.
    """
    state = _assemble_state(
        task_id=task_id,
        role=role,
        project_id=project_id,
        soft_checkpoint_path=soft_checkpoint_path,
    )
    state = _truncate_state(state, cap_bytes=STATE_CAP_BYTES)
    state_json = json.dumps(state, sort_keys=True)
    byte_size = len(state_json.encode("utf-8"))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()

    with db_conn(write=True) as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_sessions (
                task_id, role, project_id, cycle_id,
                state_json, byte_size,
                created_at, last_seen_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                role,
                project_id,
                cycle_id,
                state_json,
                byte_size,
                created_at,
                created_at,
                expires_at,
            ),
        )
        return int(cursor.lastrowid)


def restore(task_id: int, role: str) -> dict | None:
    """Return the most recent non-expired session state for ``(task_id, role)``.

    Returns ``None`` if no record exists or all matching records have expired.
    """
    now_iso = _utcnow_iso()
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT state_json
              FROM agent_sessions
             WHERE task_id = ?
               AND role = ?
               AND (expires_at IS NULL OR expires_at >= ?)
             ORDER BY last_seen_at DESC, id DESC
             LIMIT 1
            """,
            (task_id, role, now_iso),
        ).fetchone()

    if not row:
        return None
    try:
        return json.loads(row["state_json"])
    except (ValueError, KeyError):
        return None


def build_resume_prompt(state: dict) -> str:
    """Build the resume-prompt prefix for a captured session.

    Delegates to the shared :func:`equipa.checkpoints._format_recovery_prompt`
    helper so the soft-checkpoint and orchestrator-cycle paths produce
    consistent output and we never duplicate formatting logic.

    The prompt is itself capped at :data:`STATE_CAP_BYTES`; if rendering a
    full state would exceed the cap we re-truncate (partial_reasoning first,
    then recent_tool_calls) before re-rendering.
    """
    forge_state = state.get("forge_state") if isinstance(state, dict) else None
    rendered = _format_recovery_prompt(state, forge_state=forge_state)

    if len(rendered.encode("utf-8")) <= STATE_CAP_BYTES:
        return rendered

    truncated = _truncate_state(state, cap_bytes=STATE_CAP_BYTES // 2)
    rendered = _format_recovery_prompt(truncated, forge_state=forge_state)

    if len(rendered.encode("utf-8")) > STATE_CAP_BYTES:
        encoded = rendered.encode("utf-8")[:STATE_CAP_BYTES]
        rendered = encoded.decode("utf-8", errors="ignore")
    return rendered


def purge_expired() -> int:
    """Delete all expired ``agent_sessions`` rows. Returns the deleted count.

    Wired by B3's heartbeat tick.
    """
    now_iso = _utcnow_iso()
    with db_conn(write=True) as conn:
        cursor = conn.execute(
            "DELETE FROM agent_sessions WHERE expires_at IS NOT NULL "
            "AND expires_at < ?",
            (now_iso,),
        )
        return int(cursor.rowcount or 0)
