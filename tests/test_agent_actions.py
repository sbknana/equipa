#!/usr/bin/env python3
"""Test suite for agent_actions structured action logging.

Tests verify Task #923:
1. ensure_schema creates table + indexes idempotently
2. classify_error maps error strings to correct categories
3. log_agent_action inserts single records correctly
4. bulk_log_agent_actions inserts multiple records in one transaction

Copyright 2026 Forgeborn
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from forge_orchestrator import (
    classify_error,
    log_agent_action,
    bulk_log_agent_actions,
    ensure_schema,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _NoCloseConnection:
    """Wrapper around sqlite3.Connection that makes close() a no-op.

    Functions under test call conn.close() which would destroy an in-memory
    DB before we can assert on its contents. This proxy delegates everything
    except close().
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass  # no-op so the in-memory DB survives

    def real_close(self):
        self._conn.close()


def _make_temp_db():
    """Create an in-memory SQLite DB with agent_actions table.

    Returns a _NoCloseConnection wrapper. Call .real_close() when done.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            run_id INTEGER,
            cycle_number INTEGER NOT NULL,
            role TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            tool_input_preview TEXT,
            input_hash TEXT,
            output_length INTEGER,
            success INTEGER NOT NULL DEFAULT 1,
            error_type TEXT,
            error_summary TEXT,
            duration_ms INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
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
    return _NoCloseConnection(conn)


def _insert_action(conn, task_id, run_id, cycle, role, turn, tool,
                    success=True, error_type=None, error_summary=None,
                    duration_ms=100, output_length=500):
    """Helper to insert a single action row directly."""
    conn.execute(
        """INSERT INTO agent_actions
           (task_id, run_id, cycle_number, role, turn_number, tool_name,
            tool_input_preview, input_hash, output_length, success,
            error_type, error_summary, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, run_id, cycle, role, turn, tool,
         "preview", "hash123", output_length,
         1 if success else 0, error_type, error_summary, duration_ms),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests for classify_error()
# ---------------------------------------------------------------------------

def test_classify_error_timeout():
    assert classify_error("Process timed out after 600s") == "timeout"


def test_classify_error_file_not_found():
    assert classify_error("No such file or directory: /foo/bar") == "file_not_found"
    assert classify_error("File not found: test.py") == "file_not_found"


def test_classify_error_permission():
    assert classify_error("Permission denied: /etc/shadow") == "permission"


def test_classify_error_syntax():
    assert classify_error("SyntaxError: invalid syntax") == "syntax_error"


def test_classify_error_import():
    assert classify_error("ModuleNotFoundError: No module named 'foo'") == "import_error"
    assert classify_error("ImportError: cannot import name 'bar'") == "import_error"


def test_classify_error_test_failure():
    assert classify_error("FAILED test_foo.py::test_bar") == "test_failure"
    assert classify_error("AssertionError: expected 1 got 2") == "test_failure"


def test_classify_error_unknown():
    assert classify_error("Something completely unexpected") == "unknown"


def test_classify_error_empty():
    assert classify_error("") == "unknown"
    assert classify_error(None) == "unknown"


# ---------------------------------------------------------------------------
# Tests for ensure_schema()
# ---------------------------------------------------------------------------

def test_ensure_schema_creates_table():
    """ensure_schema creates table and indexes."""
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.row_factory = sqlite3.Row
    conn = _NoCloseConnection(raw_conn)

    def mock_get_db(write=False):
        return conn

    import equipa.db as _db_mod
    _db_mod._SCHEMA_ENSURED = False
    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        ensure_schema()

    # Verify table exists
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_actions'"
    ).fetchall()
    assert len(tables) == 1, "agent_actions table should exist"

    # Verify indexes
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_agent_actions%'"
    ).fetchall()
    index_names = {row["name"] for row in indexes}
    assert "idx_agent_actions_task" in index_names
    assert "idx_agent_actions_tool" in index_names
    conn.real_close()


def test_ensure_schema_idempotent():
    """Calling ensure_schema twice doesn't crash."""
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.row_factory = sqlite3.Row
    conn = _NoCloseConnection(raw_conn)

    def mock_get_db(write=False):
        return conn

    import equipa.db as _db_mod
    _db_mod._SCHEMA_ENSURED = False
    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        ensure_schema()
        _db_mod._SCHEMA_ENSURED = False  # reset so second call also runs
        ensure_schema()  # should not raise

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_actions'"
    ).fetchall()
    assert len(tables) == 1
    conn.real_close()


# ---------------------------------------------------------------------------
# Tests for log_agent_action()
# ---------------------------------------------------------------------------

def test_log_agent_action_inserts_record():
    """log_agent_action inserts a single row with correct values."""
    conn = _make_temp_db()

    def mock_get_db(write=False):
        return conn

    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        log_agent_action(
            task_id=100, run_id=5, cycle=1, role="developer",
            turn=3, tool_name="Read",
            tool_input_preview="/path/to/file.py",
            input_hash="abc123",
            output_length=1500,
            success=True, error_type=None, error_summary=None,
            duration_ms=42,
        )

    rows = conn.execute("SELECT * FROM agent_actions").fetchall()
    assert len(rows) == 1

    row = rows[0]
    assert row["task_id"] == 100
    assert row["run_id"] == 5
    assert row["cycle_number"] == 1
    assert row["role"] == "developer"
    assert row["turn_number"] == 3
    assert row["tool_name"] == "Read"
    assert row["tool_input_preview"] == "/path/to/file.py"
    assert row["input_hash"] == "abc123"
    assert row["output_length"] == 1500
    assert row["success"] == 1
    assert row["error_type"] is None
    assert row["error_summary"] is None
    assert row["duration_ms"] == 42
    conn.real_close()


def test_log_agent_action_failure_record():
    """log_agent_action correctly stores error info for failed actions."""
    conn = _make_temp_db()

    def mock_get_db(write=False):
        return conn

    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        log_agent_action(
            task_id=100, run_id=5, cycle=1, role="tester",
            turn=7, tool_name="Bash",
            tool_input_preview="pytest /foo",
            input_hash="def456",
            output_length=3000,
            success=False, error_type="test_failure",
            error_summary="FAILED test_foo::test_bar",
            duration_ms=1200,
        )

    rows = conn.execute("SELECT * FROM agent_actions").fetchall()
    assert len(rows) == 1

    row = rows[0]
    assert row["success"] == 0
    assert row["error_type"] == "test_failure"
    assert row["error_summary"] == "FAILED test_foo::test_bar"
    conn.real_close()


def test_log_agent_action_never_crashes():
    """log_agent_action swallows exceptions without crashing."""
    # Simulate DB error by returning a connection that raises on execute
    broken_conn = MagicMock()
    broken_conn.execute.side_effect = sqlite3.OperationalError("disk full")

    with patch("equipa.db.get_db_connection", return_value=broken_conn):
        # Should NOT raise
        log_agent_action(
            task_id=1, run_id=1, cycle=1, role="developer",
            turn=1, tool_name="Read",
            tool_input_preview="x", input_hash="y",
            output_length=0, success=True,
            error_type=None, error_summary=None, duration_ms=0,
        )


# ---------------------------------------------------------------------------
# Tests for bulk_log_agent_actions()
# ---------------------------------------------------------------------------

def test_bulk_log_agent_actions_inserts_all():
    """bulk_log_agent_actions inserts multiple records in one transaction."""
    conn = _make_temp_db()

    def mock_get_db(write=False):
        return conn

    action_log = [
        {
            "turn": 1, "tool": "Read",
            "input_preview": "file.py", "input_hash": "h1",
            "output_length": 100, "success": True,
            "error_type": None, "error_summary": None,
            "duration_ms": 10,
        },
        {
            "turn": 2, "tool": "Edit",
            "input_preview": "edit file.py", "input_hash": "h2",
            "output_length": 200, "success": True,
            "error_type": None, "error_summary": None,
            "duration_ms": 20,
        },
        {
            "turn": 3, "tool": "Bash",
            "input_preview": "pytest", "input_hash": "h3",
            "output_length": 5000, "success": False,
            "error_type": "test_failure", "error_summary": "FAILED",
            "duration_ms": 3000,
        },
    ]

    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        bulk_log_agent_actions(action_log, task_id=50, run_id=10, cycle=2, role="developer")

    rows = conn.execute("SELECT * FROM agent_actions ORDER BY turn_number").fetchall()
    assert len(rows) == 3

    assert rows[0]["tool_name"] == "Read"
    assert rows[0]["turn_number"] == 1
    assert rows[0]["success"] == 1

    assert rows[1]["tool_name"] == "Edit"
    assert rows[1]["turn_number"] == 2

    assert rows[2]["tool_name"] == "Bash"
    assert rows[2]["turn_number"] == 3
    assert rows[2]["success"] == 0
    assert rows[2]["error_type"] == "test_failure"
    conn.real_close()


def test_bulk_log_agent_actions_empty_list():
    """bulk_log_agent_actions does nothing for empty list."""
    conn = _make_temp_db()

    def mock_get_db(write=False):
        return conn

    with patch("equipa.db.get_db_connection", side_effect=mock_get_db):
        bulk_log_agent_actions([], task_id=1, run_id=1, cycle=1, role="developer")

    rows = conn.execute("SELECT * FROM agent_actions").fetchall()
    assert len(rows) == 0
    conn.real_close()


def test_bulk_log_agent_actions_never_crashes():
    """bulk_log_agent_actions swallows exceptions without crashing."""
    broken_conn = MagicMock()
    broken_conn.executemany.side_effect = sqlite3.OperationalError("table locked")

    with patch("equipa.db.get_db_connection", return_value=broken_conn):
        with patch("equipa.db.ensure_schema"):
            # Should NOT raise
            bulk_log_agent_actions(
                [{"turn": 1, "tool": "Read"}],
                task_id=1, run_id=1, cycle=1, role="dev",
            )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    """Run all tests and report results."""
    print("=" * 70)
    print("Agent Actions Test Suite (Task #923)")
    print("=" * 70)

    tests = [
        test_classify_error_timeout,
        test_classify_error_file_not_found,
        test_classify_error_permission,
        test_classify_error_syntax,
        test_classify_error_import,
        test_classify_error_test_failure,
        test_classify_error_unknown,
        test_classify_error_empty,
        test_ensure_schema_creates_table,
        test_ensure_schema_idempotent,
        test_log_agent_action_inserts_record,
        test_log_agent_action_failure_record,
        test_log_agent_action_never_crashes,
        test_bulk_log_agent_actions_inserts_all,
        test_bulk_log_agent_actions_empty_list,
        test_bulk_log_agent_actions_never_crashes,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {test_fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test_fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test_fn.__name__}: {e}")
            failed += 1

    print()
    print("=" * 70)
    print(f"Results: {passed}/{passed + failed} tests passed")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
