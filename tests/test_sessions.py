"""Tests for equipa.sessions — orchestrator-cycle capture / restore.

Covers PLAN-1067 §2.B2:
- Round-trip: ``capture`` -> ``restore`` returns equivalent dict.
- ``restore`` returns the most recent of multiple captures.
- Expired sessions are skipped.
- ``build_resume_prompt`` carries all soft-checkpoint fields plus the
  Paperclip B2 additions.
- ``build_resume_prompt`` is bounded by the 32 KB cap with truncation order
  ``partial_reasoning`` first, then ``recent_tool_calls``.
- The legacy ``build_compaction_recovery_context`` did not regress after
  the shared-helper refactor.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db_migrate import (  # noqa: E402
    migrate_v0_to_v1,
    migrate_v1_to_v2,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
    migrate_v4_to_v5,
    migrate_v5_to_v6,
    migrate_v6_to_v7,
    migrate_v7_to_v8,
    migrate_v8_to_v9,
    migrate_v9_to_v10,
    migrate_v10_to_v11,
    set_db_version,
)


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    """Build a v11 SQLite DB on disk and point equipa.db at it."""
    db_path = tmp_path / "session_test.db"

    conn = sqlite3.connect(str(db_path))
    # Apply the minimum FK targets, then run all migrations to v11.
    conn.execute(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "codename TEXT NOT NULL, local_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "project_id INTEGER, title TEXT NOT NULL, status TEXT DEFAULT 'open', "
        "FOREIGN KEY (project_id) REFERENCES projects(id))"
    )
    # Seed a project and task that capture() will reference.
    conn.execute(
        "INSERT INTO projects (id, codename, local_path) VALUES (?, ?, ?)",
        (1, "equipa-test", str(tmp_path)),
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, title) VALUES (?, ?, ?)",
        (1, 1, "test-task"),
    )

    # Walk the migration ladder. Each migrate_* expects the previous
    # schema; running the whole chain mirrors what production startup does.
    for fn in (
        migrate_v0_to_v1, migrate_v1_to_v2, migrate_v2_to_v3,
        migrate_v3_to_v4, migrate_v4_to_v5, migrate_v5_to_v6,
        migrate_v6_to_v7, migrate_v7_to_v8, migrate_v8_to_v9,
        migrate_v9_to_v10, migrate_v10_to_v11,
    ):
        try:
            fn(conn)
        except sqlite3.Error:
            # Some early migrations may collide with the seed schema we
            # created above; the only invariant we need is that
            # ``agent_sessions`` exists at the end.
            pass
    set_db_version(conn, 11)
    conn.commit()
    conn.close()

    # Redirect equipa.db to the tmp DB. Both modules cache the path.
    import equipa.constants as constants
    import equipa.db as equipa_db

    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(equipa_db, "THEFORGE_DB", db_path)

    yield db_path


@pytest.fixture(autouse=True)
def _reset_tool_buffer():
    """Wipe the agent_runner ring buffer between tests."""
    from equipa import agent_runner

    agent_runner._RECENT_TOOL_CALLS.clear()
    yield
    agent_runner._RECENT_TOOL_CALLS.clear()


# ---------------------------------------------------------------------------
# capture / restore round-trip
# ---------------------------------------------------------------------------


def test_capture_then_restore_returns_equivalent_dict(session_db):
    from equipa import agent_runner, sessions

    agent_runner._record_tool_call(
        task_id=1, role="developer", tool="Edit",
        turn=3, ok=True, args_hash="abc",
    )
    agent_runner._record_tool_call(
        task_id=1, role="developer", tool="Bash",
        turn=4, ok=False, args_hash="def",
    )

    row_id = sessions.capture(
        task_id=1, role="developer", project_id=1,
        cycle_id="cycle-1", soft_checkpoint_path=None,
    )
    assert row_id > 0

    restored = sessions.restore(task_id=1, role="developer")
    assert restored is not None
    # All documented fields are present, no surprises.
    for key in (
        "open_files", "files_changed", "files_read",
        "recent_tool_calls", "partial_reasoning",
        "turn_count", "compaction_count", "soft_checkpoint_path",
    ):
        assert key in restored
    assert len(restored["recent_tool_calls"]) == 2
    assert restored["recent_tool_calls"][0]["tool"] == "Edit"
    assert restored["recent_tool_calls"][1]["tool"] == "Bash"


def test_restore_returns_most_recent(session_db):
    from equipa import sessions

    first = sessions.capture(
        task_id=1, role="developer", project_id=1,
        cycle_id="cycle-A", soft_checkpoint_path=None,
    )
    # Force a distinct last_seen_at for the second insert.
    later = (datetime.now(timezone.utc) + timedelta(seconds=5)).replace(
        microsecond=0
    ).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=14)).replace(
        microsecond=0
    ).isoformat()
    state = {"marker": "second", "recent_tool_calls": [], "partial_reasoning": ""}
    encoded = json.dumps(state)

    conn = sqlite3.connect(str(session_db))
    conn.execute(
        """
        INSERT INTO agent_sessions (
            task_id, role, project_id, cycle_id, state_json, byte_size,
            created_at, last_seen_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, "developer", 1, "cycle-B", encoded,
         len(encoded.encode("utf-8")), later, later, expires),
    )
    conn.commit()
    conn.close()

    restored = sessions.restore(task_id=1, role="developer")
    assert restored is not None
    assert restored.get("marker") == "second"
    assert first  # silence unused warning


def test_expired_sessions_are_skipped(session_db):
    from equipa import sessions

    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        microsecond=0
    ).isoformat()
    state = {"marker": "expired"}
    encoded = json.dumps(state)

    conn = sqlite3.connect(str(session_db))
    conn.execute(
        """
        INSERT INTO agent_sessions (
            task_id, role, project_id, cycle_id, state_json, byte_size,
            created_at, last_seen_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, "developer", 1, "cycle-OLD", encoded,
         len(encoded.encode("utf-8")), past, past, past),
    )
    conn.commit()
    conn.close()

    assert sessions.restore(task_id=1, role="developer") is None


def test_purge_expired_deletes_only_expired(session_db):
    from equipa import sessions

    past = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
        microsecond=0
    ).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        microsecond=0
    ).isoformat()

    conn = sqlite3.connect(str(session_db))
    conn.executemany(
        """
        INSERT INTO agent_sessions (
            task_id, role, project_id, cycle_id, state_json, byte_size,
            created_at, last_seen_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "developer", 1, "old-1", "{}", 2, past, past, past),
            (1, "developer", 1, "old-2", "{}", 2, past, past, past),
            (1, "developer", 1, "fresh", "{}", 2, future, future, future),
        ],
    )
    conn.commit()
    conn.close()

    deleted = sessions.purge_expired()
    assert deleted == 2

    conn = sqlite3.connect(str(session_db))
    remaining = conn.execute(
        "SELECT cycle_id FROM agent_sessions ORDER BY id"
    ).fetchall()
    conn.close()
    assert [r[0] for r in remaining] == ["fresh"]


# ---------------------------------------------------------------------------
# build_resume_prompt
# ---------------------------------------------------------------------------


def test_resume_prompt_contains_all_soft_checkpoint_fields():
    from equipa import sessions

    state = {
        "open_files": ["src/a.py"],
        "files_changed": ["src/a.py", "src/b.py"],
        "files_read": ["README.md"],
        "recent_tool_calls": [
            {"tool": "Edit", "args_hash": "x", "ok": True, "turn": 4},
        ],
        "partial_reasoning": "Mid-thought reasoning text",
        "turn_count": 7,
        "compaction_count": 1,
        "soft_checkpoint_path": "/tmp/cp.json",
    }
    prompt = sessions.build_resume_prompt(state)
    assert "Context Recovery" in prompt
    assert "src/a.py" in prompt
    assert "src/b.py" in prompt
    assert "README.md" in prompt
    assert "Mid-thought reasoning text" in prompt
    assert "Edit" in prompt
    assert "7" in prompt
    assert "RESUME NOW" in prompt


def test_resume_prompt_caps_at_32kb_truncating_partial_reasoning_first():
    from equipa import sessions

    huge_reasoning = "R" * (60 * 1024)
    state = {
        "open_files": [],
        "files_changed": [],
        "files_read": [],
        "recent_tool_calls": [
            {"tool": "Edit", "args_hash": str(i), "ok": True, "turn": i}
            for i in range(5)
        ],
        "partial_reasoning": huge_reasoning,
        "turn_count": 10,
        "compaction_count": 0,
        "soft_checkpoint_path": "",
    }
    prompt = sessions.build_resume_prompt(state)
    assert len(prompt.encode("utf-8")) <= sessions.STATE_CAP_BYTES
    # partial_reasoning must shrink before recent_tool_calls — at least one
    # tool call still rendered, and the reasoning must NOT be the full 60 KB.
    assert prompt.count("R" * 100) < (huge_reasoning.count("R" * 100))
    assert "Edit" in prompt


def test_truncate_state_drops_partial_reasoning_before_tool_calls():
    from equipa import sessions

    state = {
        "open_files": [],
        "files_changed": [],
        "files_read": [],
        "recent_tool_calls": [
            {"tool": "Edit", "args_hash": "h", "ok": True, "turn": i}
            for i in range(3)
        ],
        "partial_reasoning": "Z" * 10_000,
        "turn_count": 1,
        "compaction_count": 0,
        "soft_checkpoint_path": "",
    }
    truncated = sessions._truncate_state(state)
    # partial_reasoning is hit first — it must be shorter than original.
    assert len(truncated["partial_reasoning"]) < len(state["partial_reasoning"])
    # And tool calls are untouched because the cap is comfortably met
    # after the first-pass partial_reasoning trim.
    assert len(truncated["recent_tool_calls"]) == 3


# ---------------------------------------------------------------------------
# back-compat: legacy build_compaction_recovery_context still works
# ---------------------------------------------------------------------------


def test_legacy_compaction_recovery_context_still_works():
    """The refactor extracted a private helper but the public API is
    unchanged. Existing callers must keep getting the same shape of output."""
    from equipa.checkpoints import build_compaction_recovery_context

    soft_cp = {
        "task_id": 42,
        "turn_count": 25,
        "files_changed": ["src/main.py"],
        "files_read": ["src/main.py", "README.md"],
        "last_result_text": "Implementing validation...",
        "compaction_count": 1,
        "compaction_signals": [],
    }
    ctx = build_compaction_recovery_context(soft_cp)
    assert "Context Recovery After Compaction" in ctx
    assert "context limit" in ctx
    assert "src/main.py" in ctx
    assert "README.md" in ctx
    assert "25" in ctx
    assert "RESUME NOW" in ctx


def test_legacy_compaction_recovery_context_with_forge_state():
    from equipa.checkpoints import build_compaction_recovery_context

    soft_cp = {
        "task_id": 42,
        "turn_count": 15,
        "files_changed": [],
        "files_read": ["a.py"],
        "last_result_text": "",
        "compaction_count": 1,
        "compaction_signals": [],
    }
    forge_state = {
        "task_id": 42,
        "current_step": "implementing validation",
        "next_action": "Fix failing test",
        "files_changed": ["src/router.py"],
        "decisions": ["Using Pydantic for validation"],
    }
    ctx = build_compaction_recovery_context(soft_cp, forge_state)
    assert "implementing validation" in ctx
    assert "Fix failing test" in ctx
    assert "Pydantic" in ctx
    assert "src/router.py" in ctx


# ---------------------------------------------------------------------------
# accessor on agent_runner
# ---------------------------------------------------------------------------


def test_get_recent_tool_calls_returns_last_n_in_order():
    from equipa import agent_runner

    for i in range(25):
        agent_runner._record_tool_call(
            task_id=99, role="developer", tool=f"T{i}",
            turn=i, ok=True, args_hash="",
        )
    last_5 = agent_runner.get_recent_tool_calls(99, "developer", n=5)
    assert [c["tool"] for c in last_5] == ["T20", "T21", "T22", "T23", "T24"]
    default = agent_runner.get_recent_tool_calls(99, "developer")
    assert len(default) == 20


def test_get_recent_tool_calls_unknown_pair_returns_empty():
    from equipa import agent_runner

    assert agent_runner.get_recent_tool_calls(7777, "ghost") == []
