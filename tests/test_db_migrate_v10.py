#!/usr/bin/env python3
"""Test suite for database migration v9 -> v10.

Covers PLAN-1067 A1: the Paperclip config-version tracking tables
(``config_versions`` and ``config_version_files``).

Tests:
- Both tables and their indexes exist after migrating a v9 DB.
- ``ON DELETE CASCADE`` removes ``config_version_files`` rows when their
  parent ``config_versions`` row is deleted.
- The dedup contract holds: a second snapshot with the same project_id and
  the same ``content_sha`` as the latest existing row is short-circuited
  (no new ``config_versions`` row is inserted).
- Migration is idempotent (safe to run twice).
- ``PRAGMA user_version`` is bumped to 10.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from db_migrate import (
    CURRENT_VERSION,
    get_db_version,
    migrate_v8_to_v9,
    migrate_v9_to_v10,
    set_db_version,
)


@pytest.fixture
def v9_db():
    """Create a v9 database with the minimal tables v10 references."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db_path = Path(path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()

    # Minimal v9 schema: just the projects table (FK target for config_versions).
    cursor.execute(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codename TEXT NOT NULL
        )
        """
    )
    cursor.execute(
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
    # Run v8->v9 so the DB really is at v9 before we migrate to v10.
    migrate_v8_to_v9(conn)
    set_db_version(conn, 9)
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


def test_current_version_is_ten():
    """The migrator's CURRENT_VERSION constant tracks the latest schema."""
    assert CURRENT_VERSION == 10


def test_migration_creates_both_tables(v9_db):
    """v9 -> v10 creates config_versions and config_version_files."""
    conn, _ = v9_db

    assert "config_versions" not in _table_names(conn)
    assert "config_version_files" not in _table_names(conn)

    migrate_v9_to_v10(conn)
    set_db_version(conn, 10)

    tables = _table_names(conn)
    assert "config_versions" in tables
    assert "config_version_files" in tables

    indexes = _index_names(conn)
    assert "idx_config_versions_project_created" in indexes
    assert "idx_cvf_version" in indexes

    assert get_db_version(conn) == 10


def test_migration_is_idempotent(v9_db):
    """Re-running migrate_v9_to_v10 on an already-v10 DB is a no-op."""
    conn, _ = v9_db
    migrate_v9_to_v10(conn)
    # Second call must not raise (CREATE TABLE IF NOT EXISTS).
    migrate_v9_to_v10(conn)

    tables = _table_names(conn)
    assert "config_versions" in tables
    assert "config_version_files" in tables


def test_foreign_key_cascade_deletes_files(v9_db):
    """Deleting a config_versions row cascades to config_version_files."""
    conn, _ = v9_db
    migrate_v9_to_v10(conn)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.execute(
        """
        INSERT INTO config_versions
            (id, project_id, created_at, source, commit_message, content_sha)
        VALUES (1, 1, '2026-05-03T00:00:00', 'manual', 'init', 'sha-aaa')
        """
    )
    conn.executemany(
        """
        INSERT INTO config_version_files
            (version_id, file_path, content_blob, file_sha, byte_size)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, "config.py", "ALPHA = 1\n", "fsha-1", 10),
            (1, "prompts/dev.md", "# dev\n", "fsha-2", 6),
        ],
    )
    conn.commit()

    file_rows = conn.execute(
        "SELECT COUNT(*) FROM config_version_files WHERE version_id = 1"
    ).fetchone()[0]
    assert file_rows == 2

    conn.execute("DELETE FROM config_versions WHERE id = 1")
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) FROM config_version_files WHERE version_id = 1"
    ).fetchone()[0]
    assert remaining == 0, "ON DELETE CASCADE should have removed the file rows"


def test_dedup_short_circuits_on_matching_content_sha(v9_db):
    """Inserting a snapshot whose content_sha matches the latest row for the
    same project must not produce a new config_versions row.

    The migration only owns the schema, so this test exercises the dedup
    contract at the SQL level the same way a caller would: check whether
    the latest row already has the same content_sha before inserting.
    """
    conn, _ = v9_db
    migrate_v9_to_v10(conn)

    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.execute(
        """
        INSERT INTO config_versions
            (project_id, created_at, source, commit_message, content_sha)
        VALUES (1, '2026-05-03T00:00:00', 'manual', 'first', 'sha-DUP')
        """
    )
    conn.commit()

    def insert_if_new(project_id: int, content_sha: str, message: str) -> bool:
        row = conn.execute(
            """
            SELECT content_sha
            FROM config_versions
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is not None and row[0] == content_sha:
            return False
        conn.execute(
            """
            INSERT INTO config_versions
                (project_id, created_at, source, commit_message, content_sha)
            VALUES (?, '2026-05-03T00:01:00', 'manual', ?, ?)
            """,
            (project_id, message, content_sha),
        )
        conn.commit()
        return True

    # Identical content_sha -> short-circuit, no new row.
    assert insert_if_new(1, "sha-DUP", "noop") is False
    count_after_dup = conn.execute(
        "SELECT COUNT(*) FROM config_versions WHERE project_id = 1"
    ).fetchone()[0]
    assert count_after_dup == 1

    # Different content_sha -> new row is inserted.
    assert insert_if_new(1, "sha-NEW", "real change") is True
    count_after_new = conn.execute(
        "SELECT COUNT(*) FROM config_versions WHERE project_id = 1"
    ).fetchone()[0]
    assert count_after_new == 2


def test_unique_constraint_on_version_file_path(v9_db):
    """(version_id, file_path) is unique in config_version_files."""
    conn, _ = v9_db
    migrate_v9_to_v10(conn)

    conn.execute("INSERT INTO projects (id, codename) VALUES (1, 'equipa')")
    conn.execute(
        """
        INSERT INTO config_versions
            (id, project_id, created_at, source, content_sha)
        VALUES (1, 1, '2026-05-03T00:00:00', 'manual', 'sha-aaa')
        """
    )
    conn.execute(
        """
        INSERT INTO config_version_files
            (version_id, file_path, content_blob, file_sha, byte_size)
        VALUES (1, 'config.py', 'A', 'fsha-1', 1)
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO config_version_files
                (version_id, file_path, content_blob, file_sha, byte_size)
            VALUES (1, 'config.py', 'B', 'fsha-2', 1)
            """
        )
