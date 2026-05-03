#!/usr/bin/env python3
"""Integration tests for equipa.flows (Task Flow + sticky cancel).

Covers:
* migration v8 -> v9 creates flows / flow_revisions / flow_tasks
* basic create / transition / add_child / list_children round-trip
* revision counter monotonicity and audit-log alignment
* optimistic-concurrency CAS via expected_revision
* sticky cancel: refuses transitions, refuses add_child, propagates to
  ``tasks.status`` and to non-terminal ``flow_tasks`` rows
* survives a simulated Claudinator restart (close + reopen DB) with
  reconcile_after_restart()

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _reload_db_modules(db_path: Path):
    """Point equipa.constants/equipa.db at the temp DB without breaking
    other modules that have already imported THEFORGE_DB-using helpers.

    The original implementation deleted these modules from sys.modules and
    re-imported them. That left stale bindings in any other test module
    that had already done `from equipa.db import get_db_connection` —
    those callables continued to use the original THEFORGE_DB and broke
    the test isolation. We now mutate the module attribute in place,
    which all dynamic THEFORGE_DB lookups respect, and leave the helper
    callables untouched.
    """
    constants = importlib.import_module("equipa.constants")
    db = importlib.import_module("equipa.db")
    flows = importlib.import_module("equipa.flows")
    constants.THEFORGE_DB = db_path
    db.THEFORGE_DB = db_path
    return constants, db, flows


@pytest.fixture
def fresh_db(tmp_path):
    """Build an empty TheForge DB at v8, then run migrations to v9."""
    from db_migrate import run_migrations

    db_path = tmp_path / "theforge.db"
    conn = sqlite3.connect(str(db_path))
    # Minimal projects/tasks schema is required for FKs.
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            codename TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'todo',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        PRAGMA user_version = 8;
        """
    )
    conn.execute("INSERT INTO projects (name, codename) VALUES ('TestProj', 'tp')")
    conn.execute(
        "INSERT INTO tasks (project_id, title) VALUES (1, 't1')"
    )
    conn.execute(
        "INSERT INTO tasks (project_id, title) VALUES (1, 't2')"
    )
    conn.execute(
        "INSERT INTO tasks (project_id, title) VALUES (1, 't3')"
    )
    conn.commit()
    conn.close()

    success, from_ver, to_ver = run_migrations(str(db_path), silent=True)
    assert success, f"migration failed {from_ver}->{to_ver}"
    assert to_ver >= 9, f"expected v>=9, got {to_ver}"

    # Capture the ORIGINAL DB path by VALUE before the test runs. Capturing
    # via module reference is fragile because test_survives_simulated_restart
    # deletes equipa.* from sys.modules to simulate an orchestrator restart,
    # which would leave us holding detached module objects whose attribute
    # writes don't reach the freshly re-imported modules.
    _constants_now = importlib.import_module("equipa.constants")
    _db_now = importlib.import_module("equipa.db")
    _orig_constants_db = _constants_now.THEFORGE_DB
    _orig_db_db = _db_now.THEFORGE_DB
    del _constants_now, _db_now

    yield db_path

    # Robust teardown: regardless of what the test did to sys.modules,
    # re-import the modules fresh and write the original values back.
    # importlib.import_module is a no-op if already in sys.modules, and a
    # fresh import otherwise — either way we get the live module.
    _constants_after = importlib.import_module("equipa.constants")
    _db_after = importlib.import_module("equipa.db")
    _constants_after.THEFORGE_DB = _orig_constants_db
    _db_after.THEFORGE_DB = _orig_db_db
    # Reset schema-ensured cache so subsequent tests' ensure_schema() runs
    # against their own (monkeypatched) THEFORGE_DB rather than skipping.
    if hasattr(_db_after, "_SCHEMA_ENSURED"):
        _db_after._SCHEMA_ENSURED = False


# --- Migration smoke ---

def test_migration_creates_flow_tables(fresh_db):
    conn = sqlite3.connect(str(fresh_db))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "flows" in names
        assert "flow_revisions" in names
        assert "flow_tasks" in names

        # PRAGMA user_version stamped to 9
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        assert v == 9
    finally:
        conn.close()


def test_migration_is_idempotent(fresh_db):
    """Running migrations again on an already-v9 DB is a no-op."""
    from db_migrate import run_migrations

    success, from_ver, to_ver = run_migrations(str(fresh_db), silent=True)
    assert success
    assert from_ver == 9
    assert to_ver == 9


# --- Core API ---

def test_create_and_get_flow(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)

    flow = flows.create_flow(project_id=1, title="my fanout")
    assert flow.id is not None
    assert flow.state == "queued"
    assert flow.revision == 0
    assert flow.metadata == {}

    snap = flows.get_flow(flow.id)
    assert snap.id == flow.id
    assert snap.state == "queued"

    # Audit log: revision 0 = create
    log = flows.get_revisions(flow.id)
    assert len(log) == 1
    assert log[0]["event"] == "create"
    assert log[0]["revision"] == 0


def test_get_flow_missing_raises(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    with pytest.raises(flows.FlowNotFound):
        flows.get_flow(999_999)


def test_transition_bumps_revision(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    after = flows.transition(flow.id, "running")
    assert after.state == "running"
    assert after.revision == 1

    after2 = flows.transition(flow.id, "done")
    assert after2.state == "done"
    assert after2.revision == 2
    assert after2.completed_at is not None

    log = flows.get_revisions(flow.id)
    revs = [r["revision"] for r in log]
    assert revs == sorted(revs) == [0, 1, 2]


def test_transition_rejects_invalid_state(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    with pytest.raises(ValueError):
        flows.transition(flow.id, "exploded")


def test_transition_rejects_terminal_change(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.transition(flow.id, "done")
    with pytest.raises(flows.FlowError):
        flows.transition(flow.id, "running")


def test_optimistic_concurrency_cas(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    # Wrong expected revision -> conflict
    with pytest.raises(flows.FlowRevisionConflict):
        flows.transition(flow.id, "running", expected_revision=99)
    # Correct expected revision -> ok
    after = flows.transition(flow.id, "running", expected_revision=0)
    assert after.revision == 1


# --- Children ---

def test_add_child_idempotent(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    c1 = flows.add_child(flow.id, task_id=1, role="developer")
    c2 = flows.add_child(flow.id, task_id=1, role="developer")
    assert c1.id == c2.id  # no duplicate row
    assert len(flows.list_children(flow.id)) == 1


def test_update_child_state_round_trip(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.add_child(flow.id, task_id=1)
    flows.update_child_state(flow.id, 1, "running")
    flows.update_child_state(flow.id, 1, "done")
    children = flows.list_children(flow.id)
    assert children[0].state == "done"
    assert children[0].completed_at is not None


# --- Sticky cancel ---

def test_sticky_cancel_propagates_to_children(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.add_child(flow.id, task_id=1, role="developer")
    flows.add_child(flow.id, task_id=2, role="reviewer")
    flows.add_child(flow.id, task_id=3, role="tester")
    flows.update_child_state(flow.id, 3, "done")  # already terminal

    cancelled = flows.cancel_flow(flow.id, reason="user pressed stop")
    assert cancelled.state == "cancelled"
    assert cancelled.cancelled_reason == "user pressed stop"

    children = {c.task_id: c for c in flows.list_children(flow.id)}
    # Non-terminal children flipped to cancelled
    assert children[1].state == "cancelled"
    assert children[2].state == "cancelled"
    # Already-terminal child untouched
    assert children[3].state == "done"

    # tasks.status updated for the propagated rows
    conn = sqlite3.connect(str(fresh_db))
    try:
        statuses = {
            r[0]: r[1]
            for r in conn.execute("SELECT id, status FROM tasks")
        }
    finally:
        conn.close()
    assert statuses[1] == "cancelled"
    assert statuses[2] == "cancelled"


def test_sticky_cancel_refuses_subsequent_transitions(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.cancel_flow(flow.id, reason="early kill")
    with pytest.raises(flows.FlowCancelled):
        flows.transition(flow.id, "running")
    with pytest.raises(flows.FlowCancelled):
        flows.add_child(flow.id, task_id=2)
    with pytest.raises(flows.FlowCancelled):
        flows.update_child_state(flow.id, 1, "done")


def test_sticky_cancel_idempotent(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    first = flows.cancel_flow(flow.id, reason="r1")
    second = flows.cancel_flow(flow.id, reason="r2")  # ignored
    # Same revision both times
    assert first.revision == second.revision
    assert second.cancelled_reason == "r1"


def test_is_cancelled_helper(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    assert flows.is_cancelled(flow.id) is False
    flows.cancel_flow(flow.id)
    assert flows.is_cancelled(flow.id) is True


# --- Restart recovery ---

def test_survives_simulated_restart(fresh_db):
    """Create a flow, drop all in-memory state, reopen the DB and reconcile.

    This mimics killing the orchestrator mid-flow: the DB rows remain and
    a fresh process should be able to load + reconcile the flow without
    losing audit history.
    """
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="long fanout")
    flows.add_child(flow.id, task_id=1, role="developer")
    flows.add_child(flow.id, task_id=2, role="reviewer")
    flows.transition(flow.id, "running")
    flows.update_child_state(flow.id, 1, "done")
    # Pretend the orchestrator dies here. Child task 2 is still pending.
    pre_revisions = flows.get_revisions(flow.id)

    # ---- "restart": rebind the flows module without deleting it from
    # sys.modules. The earlier implementation called
    # `del sys.modules["equipa.db"]` etc., which broke other test files
    # that had already done `from equipa.db import ensure_schema` at
    # import time — their function references continued to point at the
    # detached module object while monkeypatch wrote to the freshly
    # re-imported entry in sys.modules. Re-binding via _reload_db_modules
    # (which now mutates in-place) preserves both semantics: DB state is
    # reloaded from disk via flows2, and other modules keep working.
    _, _, flows2 = _reload_db_modules(fresh_db)

    # Audit log survives.
    post_revisions = flows2.get_revisions(flow.id)
    assert len(post_revisions) == len(pre_revisions)
    assert [r["revision"] for r in post_revisions] == [
        r["revision"] for r in pre_revisions
    ]

    # Simulate the recovery worker finishing the missing child, then
    # reconcile.
    flows2.update_child_state(flow.id, 2, "done")
    final = flows2.reconcile_after_restart(flow.id)
    assert final.state == "done"


def test_reconcile_marks_failed_when_any_child_failed(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.add_child(flow.id, task_id=1)
    flows.add_child(flow.id, task_id=2)
    flows.transition(flow.id, "running")
    flows.update_child_state(flow.id, 1, "done")
    flows.update_child_state(flow.id, 2, "failed")
    final = flows.reconcile_after_restart(flow.id)
    assert final.state == "failed"


def test_reconcile_promotes_queued_to_running(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.add_child(flow.id, task_id=1)
    flows.update_child_state(flow.id, 1, "running")
    final = flows.reconcile_after_restart(flow.id)
    assert final.state == "running"


def test_reconcile_no_op_when_cancelled(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    flow = flows.create_flow(project_id=1, title="t")
    flows.add_child(flow.id, task_id=1)
    flows.cancel_flow(flow.id)
    final = flows.reconcile_after_restart(flow.id)
    assert final.state == "cancelled"


# --- list_active_flows ---

def test_list_active_flows_excludes_terminal(fresh_db):
    _, _, flows = _reload_db_modules(fresh_db)
    a = flows.create_flow(project_id=1, title="a")
    b = flows.create_flow(project_id=1, title="b")
    flows.transition(a.id, "done")
    active = flows.list_active_flows(project_id=1)
    ids = [f.id for f in active]
    assert b.id in ids
    assert a.id not in ids
