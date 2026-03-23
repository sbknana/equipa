"""EQUIPA database layer — connection, schema, and core record functions.

Layer 2: Imports only from equipa.constants. All other DB-dependent modules
import from this module instead of the monolith.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from equipa.constants import THEFORGE_DB


# --- Connection ---

def get_db_connection(write: bool = False) -> sqlite3.Connection:
    """Open a connection to TheForge database.

    Args:
        write: If True, open in read-write mode. Default is read-only.
    """
    if not THEFORGE_DB.exists():
        raise FileNotFoundError(f"TheForge database not found at {THEFORGE_DB}")

    if write:
        conn = sqlite3.connect(str(THEFORGE_DB))
    else:
        uri = f"file:{THEFORGE_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- Schema ---

_SCHEMA_ENSURED = False


def ensure_schema() -> None:
    """Create all agent tables if they do not exist (called once at startup).

    Safety net — schema is primarily managed by db_migrate.py.
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        # get_db_connection raises FileNotFoundError if the DB file does not
        # exist.  For ensure_schema that is fine in production (db_migrate.py
        # creates the file first), but in test worktrees the DB may start
        # empty or absent.  Fall back to sqlite3.connect (which auto-creates
        # the file) when the DB does not exist yet.
        try:
            conn = get_db_connection(write=True)
        except FileNotFoundError:
            conn = sqlite3.connect(str(THEFORGE_DB))
            conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER, role TEXT, task_type TEXT,
                project_id INTEGER, approach_summary TEXT,
                turns_used INTEGER, outcome TEXT, error_patterns TEXT,
                reflection TEXT, q_value REAL DEFAULT 0.5,
                times_injected INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lessons_learned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                role TEXT,
                error_type TEXT,
                error_signature TEXT,
                lesson TEXT NOT NULL,
                source TEXT DEFAULT 'forgesmith',
                times_seen INTEGER DEFAULT 1,
                times_injected INTEGER DEFAULT 0,
                effectiveness_score REAL,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL, cycle_number INTEGER NOT NULL,
                from_role TEXT NOT NULL, to_role TEXT NOT NULL,
                message_type TEXT NOT NULL, content TEXT NOT NULL,
                read_by_cycle INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL, run_id INTEGER,
                cycle_number INTEGER NOT NULL, role TEXT NOT NULL,
                turn_number INTEGER NOT NULL, tool_name TEXT NOT NULL,
                tool_input_preview TEXT, input_hash TEXT,
                output_length INTEGER, success INTEGER NOT NULL DEFAULT 1,
                error_type TEXT, error_summary TEXT, duration_ms INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forgesmith_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                agent_runs_analyzed INTEGER DEFAULT 0,
                changes_made INTEGER DEFAULT 0,
                summary TEXT,
                mode TEXT DEFAULT 'auto'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forgesmith_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                change_type TEXT NOT NULL,
                target_file TEXT,
                old_value TEXT,
                new_value TEXT,
                rationale TEXT NOT NULL,
                evidence TEXT,
                effectiveness_score REAL,
                reverted_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rubric_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_run_id INTEGER NOT NULL,
                task_id INTEGER,
                project_id INTEGER,
                role TEXT NOT NULL,
                rubric_version INTEGER DEFAULT 1,
                criteria_scores TEXT NOT NULL,
                total_score REAL NOT NULL,
                max_possible REAL NOT NULL,
                normalized_score REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_actions_task "
            "ON agent_actions(task_id, cycle_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_actions_tool "
            "ON agent_actions(tool_name, success)"
        )
        conn.commit()
        conn.close()
        _SCHEMA_ENSURED = True
    except Exception as e:
        print(f"  [Schema] WARNING: Could not ensure tables: {e}")


# --- Record Functions ---

def record_agent_run(
    task: dict | int,
    result: dict | None,
    outcome: str,
    role: str = "developer",
    model: str = "opus",
    max_turns: int = 25,
    cycle_number: int = 1,
    continuation_count: int = 0,
    output: list | None = None,
    prompt_version: str | None = None,
) -> None:
    """Record agent execution telemetry to TheForge agent_runs table.

    Never crashes the orchestrator — all errors are logged and swallowed.
    Reads turns_allocated from result dict if available (set by dynamic budget system).
    prompt_version: which prompt version was used (e.g., "baseline", "v2").
        If None, reads from _last_prompt_version[role].
    """
    try:
        from equipa.output import log
        from equipa.tasks import get_task_complexity

        # Late import to avoid circular dependency
        from equipa.prompts import _last_prompt_version

        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None
        complexity = get_task_complexity(task) if isinstance(task, dict) else None
        success = 1 if outcome in ("tests_passed", "no_tests") else 0
        num_turns = result.get("num_turns", 0) if isinstance(result, dict) else 0
        duration = result.get("duration", 0) if isinstance(result, dict) else 0
        cost = result.get("cost") if isinstance(result, dict) else None
        errors = result.get("errors", []) if isinstance(result, dict) else []
        turns_allocated = result.get("turns_allocated") if isinstance(result, dict) else None
        error_type = None
        error_summary = None

        # Resolve prompt version from A/B testing tracker if not explicitly passed
        if prompt_version is None:
            prompt_version = _last_prompt_version.get(role, "baseline")

        # Early termination gets priority for error_type/error_summary
        if isinstance(result, dict) and result.get("early_terminated"):
            error_type = "early_terminated"
            error_summary = result.get("early_term_reason", "early termination")[:500]
        elif errors:
            error_summary = errors[0][:500] if errors[0] else None
            if "timed out" in (error_summary or "").lower():
                error_type = "timeout"
            elif "max_turns" in (error_summary or "").lower():
                error_type = "max_turns"
            elif "loop detected" in (error_summary or "").lower():
                error_type = "loop_detected"
            else:
                error_type = "agent_error"
        files_changed = result.get("files_changed_count", 0) if isinstance(result, dict) else 0

        conn = get_db_connection(write=True)
        conn.execute(
            """INSERT INTO agent_runs
               (task_id, project_id, role, model, complexity, num_turns,
                max_turns_allowed, duration_seconds, cost_usd, outcome,
                success, cycle_number, continuation_count, files_changed_count,
                error_type, error_summary, turns_allocated, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, project_id, role, model, complexity, num_turns,
             max_turns, duration, cost, outcome,
             success, cycle_number, continuation_count, files_changed,
             error_type, error_summary, turns_allocated, prompt_version),
        )
        conn.commit()
        conn.close()
        budget_info = f", allocated={turns_allocated}" if turns_allocated else ""
        version_info = f", prompt={prompt_version}" if prompt_version != "baseline" else ""
        log(f"  [Telemetry] Recorded agent run: role={role}, outcome={outcome}, "
            f"turns={num_turns}/{max_turns}{budget_info}{version_info}, "
            f"duration={duration:.0f}s", output)
    except Exception as e:
        # Use print fallback — log() might not be available
        print(f"  [Telemetry] WARNING: Failed to record agent run: {e}")


def _get_latest_agent_run_id(task_id: int) -> int | None:
    """Get the most recently inserted agent_run ID for a task.

    Returns the ID or None if not found. Never raises.
    """
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT id FROM agent_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        conn.close()
        return row["id"] if row else None
    except Exception:
        return None


# --- Action Logging ---

def classify_error(error_text: str) -> str:
    """Classify an error string into a category for the error_type field.

    Returns one of: timeout, file_not_found, permission, syntax_error,
    import_error, test_failure, unknown.
    """
    if not error_text:
        return "unknown"
    lower = error_text.lower()
    if "timed out" in lower:
        return "timeout"
    if "no such file" in lower or "not found" in lower:
        return "file_not_found"
    if "permission denied" in lower:
        return "permission"
    if "syntaxerror" in lower:
        return "syntax_error"
    if "modulenotfounderror" in lower or "importerror" in lower:
        return "import_error"
    if "failed" in lower or "assertionerror" in lower:
        return "test_failure"
    return "unknown"


def log_agent_action(
    task_id: int,
    run_id: int | None,
    cycle: int,
    role: str,
    turn: int,
    tool_name: str,
    tool_input_preview: str | None,
    input_hash: str | None,
    output_length: int | None,
    success: bool,
    error_type: str | None,
    error_summary: str | None,
    duration_ms: int | None,
) -> None:
    """Insert a single agent action record into the database.

    Never crashes the orchestrator — all errors are logged and swallowed.
    """
    try:
        conn = get_db_connection(write=True)
        conn.execute(
            """INSERT INTO agent_actions
               (task_id, run_id, cycle_number, role, turn_number, tool_name,
                tool_input_preview, input_hash, output_length, success,
                error_type, error_summary, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, run_id, cycle, role, turn, tool_name,
             tool_input_preview, input_hash, output_length,
             1 if success else 0, error_type, error_summary, duration_ms),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Never crash the orchestrator for telemetry


def bulk_log_agent_actions(
    action_log: list[dict],
    task_id: int,
    run_id: int | None,
    cycle: int,
    role: str,
) -> None:
    """Bulk insert all actions from an action_log list into the database.

    More efficient than individual inserts — uses a single transaction.
    Never crashes the orchestrator.
    """
    if not action_log:
        return
    try:
        ensure_schema()
        conn = get_db_connection(write=True)
        conn.executemany(
            """INSERT INTO agent_actions
               (task_id, run_id, cycle_number, role, turn_number, tool_name,
                tool_input_preview, input_hash, output_length, success,
                error_type, error_summary, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (task_id, run_id, cycle, role, a["turn"], a["tool"],
                 a.get("input_preview"), a.get("input_hash"),
                 a.get("output_length"), 1 if a.get("success", True) else 0,
                 a.get("error_type"), a.get("error_summary"),
                 a.get("duration_ms"))
                for a in action_log
            ],
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [ActionLog] WARNING: Failed to bulk insert actions: {e}")


# --- Task Status ---

def update_task_status(
    task_id: int,
    outcome: str,
    output: list | None = None,
) -> None:
    """Update task status in TheForge based on dev-test outcome.

    Called by the orchestrator after run_dev_test_loop completes, so agents
    don't need to handle DB updates themselves (they often run out of turns).

    Maps outcomes to statuses:
        tests_passed, no_tests -> done
        Everything else (blocked, failed, timeout, no_progress) -> blocked
    """
    from equipa.output import log

    success_outcomes = ("tests_passed", "no_tests")
    new_status = "done" if outcome in success_outcomes else "blocked"

    conn = get_db_connection(write=True)
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            log(f"  [DB] Task {task_id} not found — skipping status update.", output)
            return
        current = row["status"]

        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE completed_at END WHERE id = ?",
            (new_status, new_status, task_id),
        )
        conn.commit()
        log(f"  [DB] Task {task_id}: {current} -> {new_status} (outcome: {outcome})", output)
    except Exception as e:
        log(f"  [DB] ERROR updating task {task_id}: {e}", output)
    finally:
        conn.close()
