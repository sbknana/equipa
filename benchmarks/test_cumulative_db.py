"""Tests for cumulative_db module."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from benchmarks.cumulative_db import CumulativeDB


@pytest.fixture
def temp_master_db(tmp_path: Path) -> Path:
    """Create a temporary master DB with schema."""
    db_path = tmp_path / "master.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE lessons_learned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                content TEXT NOT NULL,
                lesson_type TEXT,
                task_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE agent_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                agent_role TEXT,
                task_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                topic TEXT,
                decision TEXT,
                created_at TEXT
            );
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                started_at TEXT
            );
            CREATE TABLE session_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                summary TEXT,
                created_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def temp_container_db(tmp_path: Path) -> Path:
    """Create a temporary container DB with test data."""
    db_path = tmp_path / "container.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE lessons_learned (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                content TEXT NOT NULL,
                lesson_type TEXT,
                task_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE agent_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                agent_role TEXT,
                task_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                topic TEXT,
                decision TEXT,
                created_at TEXT
            );

            INSERT INTO lessons_learned (project_id, content, lesson_type, task_id, created_at)
            VALUES (1, 'Use transactions for atomicity', 'best_practice', 100, '2026-01-01');

            INSERT INTO agent_episodes (project_id, agent_role, task_id, created_at)
            VALUES (1, 'developer', 100, '2026-01-01');

            INSERT INTO decisions (project_id, topic, decision, created_at)
            VALUES (1, 'Architecture', 'Use SQLite for persistence', '2026-01-01');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_init_creates_master_db(tmp_path: Path) -> None:
    """Test that initialization creates master DB if it doesn't exist."""
    db_path = tmp_path / "new_master.db"
    assert not db_path.exists()

    cumulative_db = CumulativeDB(str(db_path))

    assert db_path.exists()
    assert cumulative_db.master_db_path == db_path


def test_merge_container_db_deduplicates_lessons(
    temp_master_db: Path, temp_container_db: Path
) -> None:
    """Test that lessons are deduplicated by content hash."""
    cumulative_db = CumulativeDB(str(temp_master_db))

    # First merge — should insert 1 lesson
    cumulative_db._merge_container_db(temp_container_db)
    assert cumulative_db._stats["lessons_merged"] == 1

    # Second merge of same DB — should insert 0 lessons (dedupe)
    cumulative_db._merge_container_db(temp_container_db)
    assert cumulative_db._stats["lessons_merged"] == 1  # Still 1, not 2

    # Verify DB has exactly 1 lesson
    conn = sqlite3.connect(str(temp_master_db))
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM lessons_learned")
        assert cursor.fetchone()[0] == 1
    finally:
        conn.close()


def test_merge_container_db_merges_episodes(
    temp_master_db: Path, temp_container_db: Path
) -> None:
    """Test that episodes are merged without deduplication."""
    cumulative_db = CumulativeDB(str(temp_master_db))

    cumulative_db._merge_container_db(temp_container_db)
    assert cumulative_db._stats["episodes_merged"] == 1

    # Verify DB has 1 episode
    conn = sqlite3.connect(str(temp_master_db))
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM agent_episodes")
        assert cursor.fetchone()[0] == 1
    finally:
        conn.close()


def test_merge_container_db_merges_decisions(
    temp_master_db: Path, temp_container_db: Path
) -> None:
    """Test that decisions are merged."""
    cumulative_db = CumulativeDB(str(temp_master_db))

    cumulative_db._merge_container_db(temp_container_db)
    assert cumulative_db._stats["decisions_merged"] == 1


def test_get_stats_returns_copy(temp_master_db: Path) -> None:
    """Test that get_stats returns a copy of stats dict."""
    cumulative_db = CumulativeDB(str(temp_master_db))

    stats1 = cumulative_db.get_stats()
    stats1["lessons_merged"] = 999

    stats2 = cumulative_db.get_stats()
    assert stats2["lessons_merged"] == 0  # Original unchanged


def test_inject_into_container() -> None:
    """Test DB injection into container (mocked)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "master.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()

        cumulative_db = CumulativeDB(str(db_path))

        # Mock container
        container = MagicMock()
        container.put_archive = Mock()

        cumulative_db.inject_into_container(container, "/app/theforge.db")

        # Verify put_archive was called
        assert container.put_archive.called
        call_args = container.put_archive.call_args
        assert call_args[0][0] == "/app"  # dest_dir
