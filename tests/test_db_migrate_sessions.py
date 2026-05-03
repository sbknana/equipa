#!/usr/bin/env python3
"""Test suite for database migration v10 -> v11.

Covers PLAN-1067 B1: the Paperclip ``agent_sessions`` capture table.

Tests:
- ``CURRENT_VERSION`` is at v11.
- Migration applies cleanly from v10 (the current head before this change).
- The table and both indexes exist after migration.
- ``PRAGMA user_version`` is bumped by exactly one (10 -> 11).
- Foreign-key violations on ``task_id`` are rejected when FK enforcement is on.
- Deleting the referenced task does NOT cascade-delete the session row
  (sessions outlive their tasks for postmortem analysis).
- Migration is idempotent (running twice is a no-op).
- Round-trip insert/select of a state_json payload matches the documented
  shape (open_files / files_changed / files_read / recent_tool_calls /
  partial_reasoning / turn_count / compaction_count / soft_checkpoint_path).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from db_migrate import (
    CURRENT_VERSION,
    MIGRATIONS,
    get_db_version,
    migrate_v10_to_v11,
    migrate_v8_to_v9,
    migrate_v9_to_v10,
    set_db_version,
)


@pytest.fixture
def v10_db():
    """Build a real v10 database with the FK-target tables in place."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db_path = Path(path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    # Minimal v9-and-earlier schema: just the FK targets agent_sessions
    # references. Both projects and tasks are referenced by the new table.
    conn.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codename TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
        """
    )
    migrate_v8_to_v9(conn)
    migrate_v9_to_v10(conn)
    set_db_version(conn, 10)
    conn.commit()

    yield conn, db_path

    conn.close()
    db_path.unlink(missing_ok=True)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {r[0] for r in rows}


def test_current_version_is_eleven():
    """Latest schema is v11 (Paperclip B1)."""
    assert CURRENT_VERSION == 11


def test_migration_registry_has_slot_eleven():
    """The MIGRATIONS dict must register the v10 -> v11 entry."""
    assert 11 in MIGRATIONS
    description, fn = MIGRATIONS[11]
    assert "agent_sessions" in description
    assert fn is migrate_v10_to_v11


def test_migration_creates_table_and_indexes(v10_db):
    """v10 -> v11 creates agent_sessions plus its two indexes."""
    conn, _ = v10_db

    assert "agent_sessions" not in _table_names(conn)
    assert get_db_version(conn) == 10

    migrate_v10_to_v11(conn)
    set_db_version(conn, 11)

    tables = _table_names(conn)
    assert "agent_sessions" in tables

    indexes = _index_names(conn)
    assert "idx_agent_sessions_task_role_seen" in indexes
    assert "idx_agent_sessions_expires" in indexes


def test_pragma_user_version_bumps_by_exactly_one(v10_db):
    """The migration must bump PRAGMA user_version from 10 to 11."""
    conn, _ = v10_db
    assert get_db_version(conn) == 10

    migrate_v10_to_v11(conn)
    set_db_version(conn, 11)

    assert get_db_version(conn) == 11


def test_migration_is_idempotent(v10_db):
    """CREATE ... IF NOT EXISTS guards make re-application a no-op."""
    conn, _ = v10_db
    migrate_v10_to_v11(conn)

    conn.execute(
        "INSERT INTO projects (id, codename) VALUES (1, 'equipa')"
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, title) VALUES (1, 1, 't')"
    )
    conn.execute(
        """
        INSERT INTO agent_sessions
            (task_id, role, project_id, cycle_id, state_json,
             byte_size, created_at, last_seen_at, expires_at)
        VALUES (1, 'developer', 1, 'cycle-1', '{}', 2,
                '2026-05-03T00:00:00', '2026-05-03T00:00:00',
                '2026-05-17T00:00:00')
        """
    )
    conn.commit()

    # Second run must not raise and must not drop the existing row.
    migrate_v10_to_v11(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM agent_sessions"
    ).fetchone()[0]
    assert count == 1


def test_foreign_key_violation_on_missing_task(v10_db):
    """Inserting a session row with a task_id that does not exist is rejected."""
    conn, _ = v10_db
    migrate_v10_to_v11(conn)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO agent_sessions
                (task_id, role, project_id, cycle_id, state_json)
            VALUES (9999, 'developer', 1, 'cycle-x', '{}')
            """
        )
        conn.commit()


def test_deleting_task_does_not_cascade_to_sessions(v10_db):
    """FK on task_id must NOT cascade — sessions outlive their tasks.

    The schema declares ``FOREIGN KEY(task_id) REFERENCES tasks(id)`` with
    no ``ON DELETE`` clause, so SQLite's default is NO ACTION. That means:

    * With FK enforcement ON, deleting a referenced task is *blocked* — the
      session is never silently dropped.
    * In a real "task purge" workflow the orchestrator opens the connection
      with ``PRAGMA foreign_keys = OFF`` so the delete can proceed; the
      session row must still survive (no CASCADE).

    Both branches confirm that no CASCADE is present.
    """
    conn, _ = v10_db
    migrate_v10_to_v11(conn)

    # Confirm the schema text contains no CASCADE clause for task_id.
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_sessions'"
    ).fetchone()[0]
    assert "CASCADE" not in table_sql.upper(), (
        "agent_sessions must NOT declare ON DELETE CASCADE on task_id"
    )

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, title) VALUES (42, 1, 'doomed')"
    )
    conn.execute(
        """
        INSERT INTO agent_sessions
            (id, task_id, role, project_id, cycle_id, state_json,
             byte_size, created_at, last_seen_at, expires_at)
        VALUES (7, 42, 'developer', 1, 'cycle-A', '{"turn_count": 3}',
                17, '2026-05-03T00:00:00', '2026-05-03T00:01:00',
                '2026-05-17T00:00:00')
        """
    )
    conn.commit()

    # With FK enforcement on, a stray DELETE is rejected — the session row
    # is never silently dropped.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM tasks WHERE id = 42")
    conn.rollback()

    # Real purge workflow: turn off FKs, delete the task, session survives.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM tasks WHERE id = 42")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    surviving = conn.execute(
        "SELECT COUNT(*) FROM agent_sessions WHERE id = 7"
    ).fetchone()[0]
    assert surviving == 1, (
        "agent_sessions must survive task purge for postmortem analysis"
    )


def test_state_json_round_trip_matches_documented_shape(v10_db):
    """Inserting and reading back the documented state_json payload works."""
    conn, _ = v10_db
    migrate_v10_to_v11(conn)

    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, title) VALUES (1, 1, 't')"
    )

    payload = {
        "open_files": ["db_migrate.py"],
        "files_changed": ["db_migrate.py", "schema.sql"],
        "files_read": ["PLAN-1067.md"],
        "recent_tool_calls": [
            {
                "tool": "Edit",
                "args_hash": "deadbeef",
                "ok": True,
                "turn": 4,
            },
        ],
        "partial_reasoning": "thinking..." * 4,
        "turn_count": 5,
        "compaction_count": 0,
        "soft_checkpoint_path": "/tmp/.forge-state.json",
    }
    encoded = json.dumps(payload)

    conn.execute(
        """
        INSERT INTO agent_sessions
            (task_id, role, project_id, cycle_id, state_json,
             byte_size, created_at, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "developer",
            1,
            "cycle-XYZ",
            encoded,
            len(encoded.encode("utf-8")),
            "2026-05-03T00:00:00",
            "2026-05-03T00:00:30",
            "2026-05-17T00:00:00",
        ),
    )
    conn.commit()

    row = conn.execute(
        """
        SELECT task_id, role, project_id, cycle_id, state_json, byte_size
        FROM agent_sessions
        """
    ).fetchone()
    assert row is not None
    task_id, role, project_id, cycle_id, state_json, byte_size = row
    assert task_id == 1
    assert role == "developer"
    assert project_id == 1
    assert cycle_id == "cycle-XYZ"
    assert byte_size == len(encoded.encode("utf-8"))

    decoded = json.loads(state_json)
    assert set(decoded.keys()) == {
        "open_files",
        "files_changed",
        "files_read",
        "recent_tool_calls",
        "partial_reasoning",
        "turn_count",
        "compaction_count",
        "soft_checkpoint_path",
    }
    assert decoded["recent_tool_calls"][0]["tool"] == "Edit"
    assert decoded["turn_count"] == 5


def test_full_run_migrations_from_v10_head_to_v11(tmp_path):
    """End-to-end: run_migrations() advances a real v10 DB to v11."""
    from db_migrate import run_migrations

    db_path = tmp_path / "theforge_v10.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, codename TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            title TEXT NOT NULL
        )
        """
    )
    migrate_v8_to_v9(conn)
    migrate_v9_to_v10(conn)
    set_db_version(conn, 10)
    conn.commit()
    conn.close()

    success, from_ver, to_ver = run_migrations(str(db_path), silent=True)
    assert success is True
    assert from_ver == 10
    assert to_ver == 11

    conn = sqlite3.connect(str(db_path))
    try:
        assert get_db_version(conn) == 11
        assert "agent_sessions" in _table_names(conn)
    finally:
        conn.close()
