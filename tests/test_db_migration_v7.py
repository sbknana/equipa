#!/usr/bin/env python3
"""Test suite for database migration v6 -> v7.

Tests:
- decision_type, status, resolved_by_task_id, verified_at columns added
- Indexes created on decision_type, status, resolved_by_task_id
- v_open_security_findings view created and filters correctly
- v_stale_decisions updated to exclude resolved/wont_fix decisions
- Migration is idempotent (can run multiple times)
- Version tracking correct
- Default values work for existing rows

Copyright 2026 Forgeborn
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from db_migrate import (
    CURRENT_VERSION,
    get_db_version,
    migrate_v6_to_v7,
    run_migrations,
    set_db_version,
)


@pytest.fixture
def v6_db():
    """Create a v6 database with decisions and projects tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db_path = Path(path)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Create minimal v6 schema
    cursor.execute("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codename TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT,
            alternatives_considered TEXT,
            decided_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_validated DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)

    # Create the v6 stale decisions view
    cursor.execute("""
        CREATE VIEW v_stale_decisions AS
        SELECT d.*, p.codename as project_name,
               julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at))
               as days_since_validation
        FROM decisions d
        JOIN projects p ON d.project_id = p.id
        WHERE julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) > 60
    """)

    # Insert test data
    cursor.execute(
        "INSERT INTO projects (id, codename) VALUES (1, 'TestProject')"
    )
    cursor.execute(
        "INSERT INTO tasks (id, project_id, title) VALUES (100, 1, 'Fix auth bug')"
    )
    cursor.execute("""
        INSERT INTO decisions (project_id, topic, decision, rationale)
        VALUES (1, 'Use JWT tokens', 'Chose JWT over sessions', 'Stateless auth')
    """)
    cursor.execute("""
        INSERT INTO decisions (project_id, topic, decision, rationale)
        VALUES (1, 'FR-01: No auth', 'All endpoints unprotected', 'Auth middleware missing')
    """)

    conn.commit()
    set_db_version(conn, 6)
    conn.close()

    yield db_path

    # Cleanup
    db_path.unlink(missing_ok=True)
    # Clean up backup files
    for backup in db_path.parent.glob(f"{db_path.stem}_backup_*{db_path.suffix}"):
        backup.unlink(missing_ok=True)


def test_migration_adds_columns(v6_db):
    """Test that migration adds decision_type, status, resolved_by_task_id, verified_at."""
    conn = sqlite3.connect(str(v6_db))

    # Verify columns don't exist before migration
    cols = [row[1] for row in conn.execute("PRAGMA table_info(decisions)")]
    assert "decision_type" not in cols
    assert "status" not in cols
    assert "resolved_by_task_id" not in cols
    assert "verified_at" not in cols

    # Run migration
    migrate_v6_to_v7(conn)

    # Verify columns exist after migration
    cols = [row[1] for row in conn.execute("PRAGMA table_info(decisions)")]
    assert "decision_type" in cols
    assert "status" in cols
    assert "resolved_by_task_id" in cols
    assert "verified_at" in cols

    conn.close()


def test_migration_default_values(v6_db):
    """Test that existing rows get correct default values."""
    conn = sqlite3.connect(str(v6_db))

    migrate_v6_to_v7(conn)

    # Check defaults applied to existing rows
    rows = conn.execute(
        "SELECT decision_type, status, resolved_by_task_id, verified_at "
        "FROM decisions"
    ).fetchall()

    for row in rows:
        assert row[0] == "general", "decision_type should default to 'general'"
        assert row[1] == "open", "status should default to 'open'"
        assert row[2] is None, "resolved_by_task_id should be NULL"
        assert row[3] is None, "verified_at should be NULL"

    # Verify existing data preserved
    decision = conn.execute(
        "SELECT topic, decision FROM decisions WHERE id = 1"
    ).fetchone()
    assert decision[0] == "Use JWT tokens"
    assert decision[1] == "Chose JWT over sessions"

    conn.close()


def test_migration_creates_indexes(v6_db):
    """Test that migration creates indexes on new columns."""
    conn = sqlite3.connect(str(v6_db))

    migrate_v6_to_v7(conn)

    indexes = [row[1] for row in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type='index'"
    )]

    assert "idx_decisions_type" in indexes
    assert "idx_decisions_status" in indexes
    assert "idx_decisions_resolved_by" in indexes

    conn.close()


def test_open_security_findings_view(v6_db):
    """Test v_open_security_findings view filters correctly."""
    conn = sqlite3.connect(str(v6_db))

    migrate_v6_to_v7(conn)

    # Initially no security findings (all rows are decision_type='general')
    findings = conn.execute("SELECT * FROM v_open_security_findings").fetchall()
    assert len(findings) == 0

    # Insert a security finding
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status) "
        "VALUES (1, 'XSS-01: Stored XSS', 'innerHTML used unsafely', "
        "'input not escaped', 'security_finding', 'open')"
    )

    # Insert a resolved security finding (should NOT appear)
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status, "
        "resolved_by_task_id) "
        "VALUES (1, 'XSS-02: Reflected XSS', 'Fixed via escaping', "
        "'DOMPurify added', 'security_finding', 'resolved', 100)"
    )

    # Insert a non-security decision (should NOT appear)
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status) "
        "VALUES (1, 'Use Redis', 'Chose Redis for caching', "
        "'Fast KV store', 'architectural', 'open')"
    )
    conn.commit()

    findings = conn.execute("SELECT * FROM v_open_security_findings").fetchall()
    assert len(findings) == 1
    assert findings[0][3] == "XSS-01: Stored XSS"  # topic column

    conn.close()


def test_stale_decisions_excludes_resolved(v6_db):
    """Test that updated v_stale_decisions excludes resolved/wont_fix decisions."""
    conn = sqlite3.connect(str(v6_db))

    migrate_v6_to_v7(conn)

    # Insert an old resolved decision (should NOT appear as stale)
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status, "
        "decided_at) "
        "VALUES (1, 'Old resolved', 'Was fixed', 'Done', "
        "'general', 'resolved', '2025-01-01')"
    )

    # Insert an old wont_fix decision (should NOT appear as stale)
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status, "
        "decided_at) "
        "VALUES (1, 'Old wontfix', 'Not worth fixing', 'Risk accepted', "
        "'security_finding', 'wont_fix', '2025-01-01')"
    )

    # Insert an old open decision (SHOULD appear as stale)
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status, "
        "decided_at) "
        "VALUES (1, 'Old open finding', 'Still needs fix', 'Unresolved', "
        "'security_finding', 'open', '2025-01-01')"
    )
    conn.commit()

    stale = conn.execute(
        "SELECT topic FROM v_stale_decisions ORDER BY topic"
    ).fetchall()

    stale_topics = [row[0] for row in stale]
    assert "Old resolved" not in stale_topics
    assert "Old wontfix" not in stale_topics
    assert "Old open finding" in stale_topics

    conn.close()


def test_migration_idempotent(v6_db):
    """Test that migration can run multiple times safely."""
    conn = sqlite3.connect(str(v6_db))

    # Run migration twice
    migrate_v6_to_v7(conn)
    migrate_v6_to_v7(conn)  # Should not raise

    # Verify columns still exist
    cols = [row[1] for row in conn.execute("PRAGMA table_info(decisions)")]
    assert "decision_type" in cols
    assert "status" in cols
    assert "resolved_by_task_id" in cols
    assert "verified_at" in cols

    # Verify views still work
    conn.execute("SELECT * FROM v_open_security_findings").fetchall()
    conn.execute("SELECT * FROM v_stale_decisions").fetchall()

    conn.close()


def test_resolution_workflow(v6_db):
    """Test the full resolution workflow: finding -> resolution -> verified."""
    conn = sqlite3.connect(str(v6_db))

    migrate_v6_to_v7(conn)

    # Step 1: Security reviewer logs a finding
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status) "
        "VALUES (1, 'AUTH-01: No rate limiting', 'Login endpoint has no rate limit', "
        "'Brute force possible', 'security_finding', 'open')"
    )
    conn.commit()

    finding_id = conn.execute(
        "SELECT id FROM decisions WHERE topic = 'AUTH-01: No rate limiting'"
    ).fetchone()[0]

    # Verify it appears in open findings
    open_findings = conn.execute(
        "SELECT * FROM v_open_security_findings"
    ).fetchall()
    assert len(open_findings) == 1

    # Step 2: Developer fixes and logs resolution
    conn.execute(
        "INSERT INTO decisions "
        "(project_id, topic, decision, rationale, decision_type, status, "
        "resolved_by_task_id) "
        "VALUES (1, 'AUTH-01: resolved', 'Added express-rate-limit middleware', "
        "'100 req/15min per IP', 'resolution', 'open', 100)"
    )

    # Step 3: Mark original finding as resolved
    conn.execute(
        "UPDATE decisions SET status = 'resolved', "
        "resolved_by_task_id = 100, "
        "verified_at = datetime('now') "
        "WHERE id = ?",
        (finding_id,),
    )
    conn.commit()

    # Verify finding no longer appears in open findings
    open_findings = conn.execute(
        "SELECT * FROM v_open_security_findings"
    ).fetchall()
    assert len(open_findings) == 0

    # Verify the resolution record exists
    resolution = conn.execute(
        "SELECT decision_type, resolved_by_task_id FROM decisions "
        "WHERE topic = 'AUTH-01: resolved'"
    ).fetchone()
    assert resolution[0] == "resolution"
    assert resolution[1] == 100

    # Verify the original is marked resolved with timestamp
    original = conn.execute(
        "SELECT status, resolved_by_task_id, verified_at FROM decisions "
        "WHERE id = ?",
        (finding_id,),
    ).fetchone()
    assert original[0] == "resolved"
    assert original[1] == 100
    assert original[2] is not None  # verified_at set

    conn.close()


def test_full_migration_v6_to_v7(v6_db):
    """Test run_migrations() upgrades v6 to v7 correctly."""
    # Verify starting at v6
    conn = sqlite3.connect(str(v6_db))
    assert get_db_version(conn) == 6
    conn.close()

    # Run migrations
    success, from_ver, to_ver = run_migrations(str(v6_db), silent=True)

    assert success is True
    assert from_ver == 6
    assert to_ver == CURRENT_VERSION

    # Verify final state
    conn = sqlite3.connect(str(v6_db))
    assert get_db_version(conn) == CURRENT_VERSION

    # Verify all changes applied
    cols = [row[1] for row in conn.execute("PRAGMA table_info(decisions)")]
    assert "decision_type" in cols
    assert "status" in cols
    assert "resolved_by_task_id" in cols
    assert "verified_at" in cols

    # Verify views exist
    views = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    )]
    assert "v_open_security_findings" in views
    assert "v_stale_decisions" in views

    conn.close()


def test_decision_type_values(v6_db):
    """Test that all valid decision_type values can be inserted."""
    conn = sqlite3.connect(str(v6_db))
    migrate_v6_to_v7(conn)

    valid_types = [
        "general", "security_finding", "architectural",
        "trade_off", "resolution",
    ]

    for dtype in valid_types:
        conn.execute(
            "INSERT INTO decisions "
            "(project_id, topic, decision, decision_type, status) "
            "VALUES (1, ?, 'Test', ?, 'open')",
            (f"Test {dtype}", dtype),
        )
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE decision_type != 'general'"
    ).fetchone()[0]
    # 4 non-general types (security_finding, architectural, trade_off, resolution)
    assert count == 4

    conn.close()


def test_status_values(v6_db):
    """Test that all valid status values can be inserted."""
    conn = sqlite3.connect(str(v6_db))
    migrate_v6_to_v7(conn)

    valid_statuses = [
        "open", "resolved", "superseded", "wont_fix", "failed_resolution",
    ]

    for status in valid_statuses:
        conn.execute(
            "INSERT INTO decisions "
            "(project_id, topic, decision, decision_type, status) "
            "VALUES (1, ?, 'Test', 'general', ?)",
            (f"Test {status}", status),
        )
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(DISTINCT status) FROM decisions"
    ).fetchone()[0]
    assert count == 5  # All 5 statuses used

    conn.close()
