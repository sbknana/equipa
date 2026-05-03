"""EQUIPA database layer — connection, schema, and core record functions.

Layer 2: Imports only from equipa.constants. All other DB-dependent modules
import from this module instead of the monolith.

Extracted from forge_orchestrator.py as part of Phase 3 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from equipa.constants import THEFORGE_DB

logger = logging.getLogger(__name__)


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

# Canonical schema lives at the repo root.  This module is at
# <repo>/equipa/db.py, so the schema file is one directory up.
SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class SchemaNotInitialised(RuntimeError):
    """Raised when TheForge DB schema cannot be applied or located.

    Production callers should run ``db_migrate.py`` (or ``equipa_setup.py``)
    before importing modules that touch the DB.  This exception signals that
    the safety-net path failed and the orchestrator must not continue.
    """


def _make_schema_idempotent(schema_sql: str) -> str:
    """Rewrite CREATE statements in schema.sql to be idempotent.

    Mirrors the logic in tests/conftest.py:_ensure_full_schema so we can
    treat schema.sql as the single source of truth without duplicating
    table definitions inside this module.
    """
    for keyword in ("TABLE", "VIEW", "TRIGGER", "INDEX"):
        schema_sql = re.sub(
            rf"CREATE {keyword}(?!\s+IF\s+NOT\s+EXISTS)",
            f"CREATE {keyword} IF NOT EXISTS",
            schema_sql,
            flags=re.IGNORECASE,
        )
    schema_sql = re.sub(
        r"CREATE UNIQUE INDEX(?!\s+IF\s+NOT\s+EXISTS)",
        "CREATE UNIQUE INDEX IF NOT EXISTS",
        schema_sql,
        flags=re.IGNORECASE,
    )
    return schema_sql


def ensure_schema() -> None:
    """Apply the canonical ``schema.sql`` to TheForge DB if needed.

    Safety net: the primary schema management path is ``db_migrate.py``
    (for upgrades) and ``equipa_setup.py`` (for fresh installs).  This
    function exists so library callers can safely touch the DB even when
    those entry points have not run — but unlike the previous inline
    ``CREATE TABLE`` block it does not duplicate table definitions.

    Raises:
        SchemaNotInitialised: if ``schema.sql`` is missing or applying it
            fails.  Callers must NOT swallow this — a missing schema means
            downstream telemetry and lesson lookups will silently lose data.
    """
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return

    if not SCHEMA_SQL_PATH.exists():
        raise SchemaNotInitialised(
            f"Canonical schema file not found at {SCHEMA_SQL_PATH}. "
            "Run equipa_setup.py or db_migrate.py before using the DB."
        )

    try:
        schema_sql = _make_schema_idempotent(SCHEMA_SQL_PATH.read_text())
    except OSError as e:
        raise SchemaNotInitialised(
            f"Could not read schema file at {SCHEMA_SQL_PATH}: {e}"
        ) from e

    # get_db_connection raises FileNotFoundError if the DB file does not
    # exist.  In production db_migrate.py / equipa_setup.py creates the
    # file first; in test worktrees the DB may start absent, so fall back
    # to sqlite3.connect (which auto-creates) before applying schema.sql.
    try:
        conn = get_db_connection(write=True)
    except FileNotFoundError:
        conn = sqlite3.connect(str(THEFORGE_DB))
        conn.row_factory = sqlite3.Row

    try:
        conn.executescript(schema_sql)
        conn.commit()
    except sqlite3.Error as e:
        logger.exception("[Schema] Failed to apply schema.sql: %s", e)
        raise SchemaNotInitialised(
            f"Failed to apply {SCHEMA_SQL_PATH.name}: {e}"
        ) from e
    finally:
        conn.close()

    _SCHEMA_ENSURED = True


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
    except Exception:
        # Telemetry must NEVER crash the orchestrator. Catch broadly here, but
        # use logger.exception() so ops can grep for [Telemetry] tracebacks.
        logger.exception("[Telemetry] Failed to record agent run")


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
    except sqlite3.Error:
        logger.exception("[Telemetry] Failed to look up latest agent run id")
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
        # Telemetry must NEVER crash the orchestrator (broad except is intentional
        # here). Previously this was a silent `pass` — now ops can grep
        # [Telemetry] in logs for swallowed errors.
        logger.exception("[Telemetry] Failed to log agent action")


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
    except Exception:
        # Telemetry must NEVER crash the orchestrator.
        logger.exception("[Telemetry] Failed to bulk insert agent actions")


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

    success_outcomes = ("tests_passed", "no_tests", "early_completed_no_changes")
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
    except sqlite3.Error as e:
        # Real logic path (not telemetry) — narrow to sqlite errors only so
        # programmer bugs (KeyError, AttributeError) still surface as crashes.
        log(f"  [DB] ERROR updating task {task_id}: {e}", output)
        logger.exception("[DB] Failed to update task %s status", task_id)
    finally:
        conn.close()
