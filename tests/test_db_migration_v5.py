#!/usr/bin/env python3
"""Test suite for database migration v4 -> v5.

Tests:
- Embedding columns added to lessons_learned and agent_episodes
- lesson_graph_edges table created with proper schema
- Indexes created on src_id and dst_id
- Migration is idempotent (can run multiple times)
- Version tracking correct

Copyright 2026 Forgeborn
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from db_migrate import (
    CURRENT_VERSION,
    get_db_version,
    migrate_v4_to_v5,
    run_migrations,
    set_db_version,
)


@pytest.fixture
def v4_db():
    """Create a v4 database with lessons_learned and agent_episodes tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db_path = Path(path)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Create minimal v4 schema
    cursor.execute("""
        CREATE TABLE lessons_learned (
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

    cursor.execute("""
        CREATE TABLE agent_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            role TEXT,
            task_type TEXT,
            project_id INTEGER,
            approach_summary TEXT,
            turns_used INTEGER,
            outcome TEXT,
            error_patterns TEXT,
            reflection TEXT,
            q_value REAL DEFAULT 0.5,
            created_at TEXT DEFAULT (datetime('now')),
            times_injected INTEGER DEFAULT 0
        )
    """)

    # Insert some test data
    cursor.execute("""
        INSERT INTO lessons_learned (lesson, role, error_type)
        VALUES ('Always validate input', 'developer', 'validation_error')
    """)

    cursor.execute("""
        INSERT INTO agent_episodes (role, outcome, reflection)
        VALUES ('developer', 'success', 'Used TDD approach')
    """)

    conn.commit()
    set_db_version(conn, 4)
    conn.close()

    yield db_path

    # Cleanup
    db_path.unlink(missing_ok=True)


def test_migration_adds_embedding_columns(v4_db):
    """Test that migration adds embedding column to both tables."""
    conn = sqlite3.connect(str(v4_db))

    # Verify columns don't exist before migration
    lessons_cols = [row[1] for row in conn.execute("PRAGMA table_info(lessons_learned)")]
    episodes_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_episodes)")]

    assert "embedding" not in lessons_cols
    assert "embedding" not in episodes_cols

    # Run migration
    migrate_v4_to_v5(conn)

    # Verify columns exist after migration
    lessons_cols = [row[1] for row in conn.execute("PRAGMA table_info(lessons_learned)")]
    episodes_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_episodes)")]

    assert "embedding" in lessons_cols
    assert "embedding" in episodes_cols

    # Verify existing data preserved
    lesson = conn.execute("SELECT lesson FROM lessons_learned WHERE id = 1").fetchone()
    assert lesson[0] == "Always validate input"

    episode = conn.execute("SELECT outcome FROM agent_episodes WHERE id = 1").fetchone()
    assert episode[0] == "success"

    conn.close()


def test_migration_creates_graph_table(v4_db):
    """Test that migration creates lesson_graph_edges table."""
    conn = sqlite3.connect(str(v4_db))

    # Verify table doesn't exist before migration
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    assert "lesson_graph_edges" not in tables

    # Run migration
    migrate_v4_to_v5(conn)

    # Verify table exists
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    assert "lesson_graph_edges" in tables

    # Verify schema
    cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(lesson_graph_edges)")}
    assert "id" in cols
    assert "src_id" in cols
    assert "dst_id" in cols
    assert "edge_type" in cols
    assert "weight" in cols
    assert "created_at" in cols

    # Verify UNIQUE constraint exists
    indexes = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='lesson_graph_edges'"
    ).fetchone()[0]
    assert "UNIQUE" in indexes
    assert "src_id, dst_id, edge_type" in indexes

    conn.close()


def test_migration_creates_indexes(v4_db):
    """Test that migration creates indexes on src_id and dst_id."""
    conn = sqlite3.connect(str(v4_db))

    # Run migration
    migrate_v4_to_v5(conn)

    # Verify indexes exist
    indexes = [row[1] for row in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type='index'"
    )]

    assert "idx_lesson_graph_src" in indexes
    assert "idx_lesson_graph_dst" in indexes

    # Verify index definitions
    src_idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_lesson_graph_src'"
    ).fetchone()[0]
    assert "src_id" in src_idx

    dst_idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_lesson_graph_dst'"
    ).fetchone()[0]
    assert "dst_id" in dst_idx

    conn.close()


def test_migration_idempotent(v4_db):
    """Test that migration can run multiple times safely."""
    conn = sqlite3.connect(str(v4_db))

    # Run migration twice
    migrate_v4_to_v5(conn)
    migrate_v4_to_v5(conn)  # Should not raise

    # Verify still works correctly
    lessons_cols = [row[1] for row in conn.execute("PRAGMA table_info(lessons_learned)")]
    episodes_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_episodes)")]
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]

    assert "embedding" in lessons_cols
    assert "embedding" in episodes_cols
    assert "lesson_graph_edges" in tables

    conn.close()


def test_graph_edge_insert(v4_db):
    """Test that we can insert edges into the graph table."""
    conn = sqlite3.connect(str(v4_db))

    # Run migration
    migrate_v4_to_v5(conn)

    # Insert a graph edge
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO lesson_graph_edges (src_id, dst_id, edge_type, weight)
        VALUES (1, 1, 'similar', 0.85)
    """)
    conn.commit()

    # Verify edge exists
    edge = conn.execute(
        "SELECT src_id, dst_id, edge_type, weight FROM lesson_graph_edges WHERE id = 1"
    ).fetchone()

    assert edge[0] == 1
    assert edge[1] == 1
    assert edge[2] == "similar"
    assert edge[3] == 0.85

    conn.close()


def test_graph_unique_constraint(v4_db):
    """Test that UNIQUE constraint prevents duplicate edges."""
    conn = sqlite3.connect(str(v4_db))

    # Run migration
    migrate_v4_to_v5(conn)

    # Insert first edge
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO lesson_graph_edges (src_id, dst_id, edge_type, weight)
        VALUES (1, 1, 'similar', 0.85)
    """)
    conn.commit()

    # Try to insert duplicate edge (should fail)
    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute("""
            INSERT INTO lesson_graph_edges (src_id, dst_id, edge_type, weight)
            VALUES (1, 1, 'similar', 0.90)
        """)

    conn.close()


def test_full_migration_v4_to_v5(v4_db):
    """Test run_migrations() upgrades v4 to v5 correctly."""
    # Verify starting at v4
    conn = sqlite3.connect(str(v4_db))
    assert get_db_version(conn) == 4
    conn.close()

    # Run migrations
    success, from_ver, to_ver = run_migrations(str(v4_db), silent=True)

    assert success is True
    assert from_ver == 4
    assert to_ver == CURRENT_VERSION

    # Verify final state
    conn = sqlite3.connect(str(v4_db))
    assert get_db_version(conn) == CURRENT_VERSION

    # Verify all changes applied
    lessons_cols = [row[1] for row in conn.execute("PRAGMA table_info(lessons_learned)")]
    episodes_cols = [row[1] for row in conn.execute("PRAGMA table_info(agent_episodes)")]
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]

    assert "embedding" in lessons_cols
    assert "embedding" in episodes_cols
    assert "lesson_graph_edges" in tables

    conn.close()


def test_embedding_can_store_json(v4_db):
    """Test that embedding column can store JSON-encoded vectors."""
    import json

    conn = sqlite3.connect(str(v4_db))
    migrate_v4_to_v5(conn)

    # Store a fake embedding vector
    vector = [0.1, 0.2, 0.3, 0.4, 0.5]
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE lessons_learned SET embedding = ? WHERE id = 1",
        (json.dumps(vector),)
    )
    conn.commit()

    # Retrieve and verify
    result = conn.execute(
        "SELECT embedding FROM lessons_learned WHERE id = 1"
    ).fetchone()[0]

    retrieved_vector = json.loads(result)
    assert retrieved_vector == vector

    conn.close()
