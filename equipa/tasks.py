"""EQUIPA tasks module — task fetching, project context, and status queries.

Layer 2: Imports from equipa.constants and equipa.db.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from equipa.constants import (
    COMPLEXITY_MULTIPLIERS,
    PRIORITY_ORDER,
    PROJECT_DIRS,
    THEFORGE_DB,
)
from equipa.db import get_db_connection


def fetch_task(task_id: int) -> dict | None:
    """Fetch a specific task by ID, including project info."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_next_todo(project_id: int) -> dict | None:
    """Find the highest-priority todo task for a project."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.project_id = ? AND t.status = 'todo'
            ORDER BY t.created_at ASC
            """,
            (project_id,),
        ).fetchall()

        if not rows:
            return None

        # Sort by priority text mapping (critical > high > medium > low)
        tasks = [dict(r) for r in rows]
        tasks.sort(
            key=lambda t: PRIORITY_ORDER.get(
                str(t.get("priority", "low")).lower(), 0
            ),
            reverse=True,
        )
        return tasks[0]
    finally:
        conn.close()


def fetch_project_context(project_id: int) -> dict:
    """Get recent project context: last session, open questions, recent decisions."""
    conn = get_db_connection()
    try:
        context: dict = {}

        # Last session notes
        row = conn.execute(
            """
            SELECT summary, next_steps, session_date
            FROM session_notes
            WHERE project_id = ?
            ORDER BY session_date DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        context["last_session"] = dict(row) if row else None

        # Open questions
        rows = conn.execute(
            """
            SELECT question, context
            FROM open_questions
            WHERE project_id = ? AND resolved = 0
            """,
            (project_id,),
        ).fetchall()
        context["open_questions"] = [dict(r) for r in rows]

        # Recent decisions
        rows = conn.execute(
            """
            SELECT decision, rationale, decided_at
            FROM decisions
            WHERE project_id = ?
            ORDER BY decided_at DESC
            LIMIT 5
            """,
            (project_id,),
        ).fetchall()
        context["recent_decisions"] = [dict(r) for r in rows]

        return context
    finally:
        conn.close()


def _get_task_status(task_id: int) -> str | None:
    """Quick read of task status string from DB."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def fetch_project_info(project_id: int) -> dict | None:
    """Get project name and codename from project_id."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT id, name,
                   COALESCE(codename, LOWER(REPLACE(name, ' ', ''))) as codename
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_tasks_by_ids(task_ids: list[int]) -> list[dict]:
    """Fetch multiple tasks by their IDs.

    Returns a list of task dicts in the same order as the input IDs.
    Missing tasks are skipped.
    """
    if not task_ids:
        return []

    conn = get_db_connection()
    try:
        placeholders = ", ".join("?" for _ in task_ids)
        rows = conn.execute(
            f"""
            SELECT t.*, p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as project_codename
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id IN ({placeholders})
            """,
            task_ids,
        ).fetchall()

        # Build a dict keyed by ID for ordering
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[tid] for tid in task_ids if tid in by_id]
    finally:
        conn.close()


def get_task_complexity(task: dict | None) -> str:
    """Resolve task complexity.

    Checks the task's 'complexity' field first (set in DB), then infers from
    description length as a fallback.

    Returns one of: 'simple', 'medium', 'complex', 'epic'
    """
    # Explicit complexity in the task record
    explicit = ((task or {}).get("complexity") or "").strip().lower()
    if explicit in COMPLEXITY_MULTIPLIERS:
        return explicit

    # Infer from description length
    desc = (task or {}).get("description", "") or ""
    desc_len = len(desc)
    if desc_len < 100:
        return "simple"
    elif desc_len < 400:
        return "medium"
    elif desc_len < 800:
        return "complex"
    else:
        return "epic"


def verify_task_updated(task_id: int) -> tuple[bool, str]:
    """Check if the agent updated the task status in TheForge."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

        if not row:
            return False, f"Task {task_id} not found in database"

        status = row["status"]
        if status == "done":
            return True, f"Task {task_id} marked as DONE"
        elif status == "blocked":
            return True, f"Task {task_id} marked as BLOCKED (agent reported blocker)"
        elif status == "in_progress":
            return False, f"Task {task_id} still IN_PROGRESS (agent may not have finished)"
        else:
            return False, f"Task {task_id} status is '{status}' (expected done or blocked)"
    finally:
        conn.close()


def resolve_project_dir(task: dict) -> str | None:
    """Find the project directory for a task's project.

    Resolution order:
    1. dispatch_config/forge_config project_dirs (exact match)
    2. TheForge DB local_path (source of truth)

    Uses exact match only — no partial/substring matching to prevent
    path traversal via crafted project codenames (security finding #3).
    """
    codename = task.get("project_codename", "").lower().strip()
    project_name = task.get("project_name", "").lower().strip()

    # 1. Check config file overrides first (exact match)
    if codename and codename in PROJECT_DIRS:
        return PROJECT_DIRS[codename]
    if project_name and project_name in PROJECT_DIRS:
        return PROJECT_DIRS[project_name]

    # 2. Fall back to TheForge DB local_path (source of truth)
    project_id = task.get("project_id")
    if project_id and THEFORGE_DB:
        try:
            conn = sqlite3.connect(str(THEFORGE_DB))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT local_path FROM projects WHERE id = ? AND local_path IS NOT NULL",
                (project_id,)
            ).fetchone()
            conn.close()
            if row and row["local_path"]:
                db_path = row["local_path"].rstrip("/").rstrip("\\")
                if Path(db_path).exists():
                    return db_path
        except Exception as e:
            print(f"WARNING: DB lookup for project dir failed: {e}")

    return None
