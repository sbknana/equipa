"""Tests for cumulative_db.py module."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from benchmarks.cumulative_db import CumulativeDB


@pytest.fixture
def temp_master_db(tmp_path: Path) -> Path:
    """Create a temporary master DB path."""
    return tmp_path / "master.db"


@pytest.fixture
def temp_container_db(tmp_path: Path) -> Path:
    """Create a temporary container DB with test data."""
    db_path = tmp_path / "container.db"
    conn = sqlite3.connect(str(db_path))

    # Create tables matching TheForge schema
    conn.execute("""
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

    conn.execute("""
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
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT,
            alternatives_considered TEXT,
            decided_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Insert test data
    conn.execute("""
        INSERT INTO lessons_learned (project_id, lesson, role, error_type)
        VALUES (1, 'Always validate input before processing', 'developer', 'validation')
    """)

    conn.execute("""
        INSERT INTO agent_episodes (task_id, role, outcome, turns_used)
        VALUES (100, 'developer', 'success', 5)
    """)

    conn.execute("""
        INSERT INTO decisions (project_id, topic, decision)
        VALUES (1, 'Architecture', 'Use microservices')
    """)

    conn.commit()
    conn.close()

    return db_path


def test_cumulative_db_init(temp_master_db: Path) -> None:
    """Test CumulativeDB initialization."""
    db = CumulativeDB(str(temp_master_db))
    assert temp_master_db.exists()
    assert db.master_db_path == temp_master_db


def test_inject_into_container(temp_master_db: Path) -> None:
    """Test injecting master DB into container."""
    # Create master DB with some data
    db = CumulativeDB(str(temp_master_db))
    conn = sqlite3.connect(str(temp_master_db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons_learned (
            id INTEGER PRIMARY KEY,
            lesson TEXT
        )
    """)
    conn.execute("INSERT INTO lessons_learned (lesson) VALUES ('test lesson')")
    conn.commit()
    conn.close()

    # Mock container
    mock_container = Mock()
    mock_container.put_archive = Mock()

    # Inject
    db.inject_into_container(mock_container, "/app/theforge.db")

    # Verify put_archive was called
    assert mock_container.put_archive.called
    call_args = mock_container.put_archive.call_args
    assert call_args[0][0] == "/app"  # dest_dir

    # Verify tar archive contains DB
    tar_data = call_args[0][1]
    tar_data.seek(0)
    with tarfile.open(fileobj=tar_data) as tar:
        members = tar.getmembers()
        assert len(members) == 1
        assert members[0].name == "theforge.db"


def test_extract_and_merge(temp_master_db: Path, temp_container_db: Path) -> None:
    """Test extracting and merging container DB."""
    # Initialize master DB
    db = CumulativeDB(str(temp_master_db))

    # Create master schema
    conn = sqlite3.connect(str(temp_master_db))
    conn.execute("""
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
    conn.execute("""
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
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT,
            alternatives_considered TEXT,
            decided_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    # Create tar archive from container DB
    with open(temp_container_db, "rb") as f:
        db_content = f.read()

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        tarinfo = tarfile.TarInfo(name="theforge.db")
        tarinfo.size = len(db_content)
        tar.addfile(tarinfo, io.BytesIO(db_content))

    tar_buf.seek(0)

    # Mock container
    mock_container = Mock()
    mock_container.get_archive = Mock(return_value=(iter([tar_buf.getvalue()]), {}))

    # Extract and merge
    db.extract_and_merge(mock_container, "/app/theforge.db")

    # Verify merge stats
    stats = db.get_stats()
    assert stats["lessons_merged"] == 1
    assert stats["episodes_merged"] == 1
    assert stats["decisions_merged"] == 1

    # Verify data was merged
    conn = sqlite3.connect(str(temp_master_db))
    cursor = conn.execute("SELECT COUNT(*) FROM lessons_learned")
    assert cursor.fetchone()[0] == 1

    cursor = conn.execute("SELECT COUNT(*) FROM agent_episodes")
    assert cursor.fetchone()[0] == 1

    cursor = conn.execute("SELECT COUNT(*) FROM decisions")
    assert cursor.fetchone()[0] == 1

    conn.close()


def test_deduplication(temp_master_db: Path, temp_container_db: Path) -> None:
    """Test that duplicate lessons are not merged."""
    # Initialize master DB
    db = CumulativeDB(str(temp_master_db))

    # Create master schema and insert same lesson
    conn = sqlite3.connect(str(temp_master_db))
    conn.execute("""
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
    conn.execute("""
        INSERT INTO lessons_learned (project_id, lesson, role, error_type)
        VALUES (1, 'Always validate input before processing', 'developer', 'validation')
    """)
    conn.commit()
    conn.close()

    # Create tar archive from container DB (which has the same lesson)
    with open(temp_container_db, "rb") as f:
        db_content = f.read()

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        tarinfo = tarfile.TarInfo(name="theforge.db")
        tarinfo.size = len(db_content)
        tar.addfile(tarinfo, io.BytesIO(db_content))

    tar_buf.seek(0)

    # Mock container
    mock_container = Mock()
    mock_container.get_archive = Mock(return_value=(iter([tar_buf.getvalue()]), {}))

    # Extract and merge
    db.extract_and_merge(mock_container, "/app/theforge.db")

    # Verify no duplicate lessons
    stats = db.get_stats()
    assert stats["lessons_merged"] == 0  # Should not merge duplicate

    # Verify still only 1 lesson in master
    conn = sqlite3.connect(str(temp_master_db))
    cursor = conn.execute("SELECT COUNT(*) FROM lessons_learned")
    assert cursor.fetchone()[0] == 1
    conn.close()


def test_get_stats(temp_master_db: Path) -> None:
    """Test getting merge statistics."""
    db = CumulativeDB(str(temp_master_db))

    stats = db.get_stats()
    assert "lessons_merged" in stats
    assert "episodes_merged" in stats
    assert "decisions_merged" in stats
    assert "runs_merged" in stats
    assert "notes_merged" in stats
    assert all(v == 0 for v in stats.values())
