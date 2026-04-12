"""Cumulative knowledge DB for Docker-based benchmark runners.

Implements the extract/merge pattern for accumulating lessons, decisions, and
episodes across multiple Docker container runs. Each container starts with the
merged knowledge from all prior runs, enabling cumulative learning.
"""

from __future__ import annotations

import hashlib
import io
import logging
import sqlite3
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import docker.models.containers

logger = logging.getLogger(__name__)


class CumulativeDB:
    """Manages cumulative knowledge extraction and merging for Docker benchmarks.

    Workflow:
    1. Initialize with path to master DB on host
    2. Inject master DB into container before task execution
    3. Extract container DB after task completes
    4. Merge new knowledge into master DB (deduplicated)
    5. Repeat for next container
    """

    MERGE_TABLES = [
        "lessons_learned",
        "agent_episodes",
        "decisions",
        "agent_runs",
        "session_notes",
    ]

    def __init__(self, master_db_path: str) -> None:
        """Initialize with path to master DB on host.

        Args:
            master_db_path: Absolute path to master theforge.db on host filesystem.
                           Created if it doesn't exist.
        """
        self.master_db_path = Path(master_db_path)
        self._ensure_master_db_exists()
        self._stats = {
            "lessons_merged": 0,
            "episodes_merged": 0,
            "decisions_merged": 0,
            "runs_merged": 0,
            "notes_merged": 0,
        }

    def _ensure_master_db_exists(self) -> None:
        """Create master DB if it doesn't exist."""
        if self.master_db_path.exists():
            return

        self.master_db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create empty DB with schema from schema.sql if available
        schema_path = self.master_db_path.parent.parent / "schema.sql"

        conn = sqlite3.connect(str(self.master_db_path))
        try:
            if schema_path.exists():
                with open(schema_path, "r", encoding="utf-8") as f:
                    conn.executescript(f.read())
            conn.commit()
        finally:
            conn.close()

        logger.info(f"Created master DB: {self.master_db_path}")

    def inject_into_container(
        self, container: docker.models.containers.Container, dest_path: str
    ) -> None:
        """Copy master DB into Docker container via tar archive.

        Args:
            container: Docker container to inject DB into.
            dest_path: Absolute path inside container (e.g., "/app/theforge.db").
        """
        # Read master DB into memory
        with open(self.master_db_path, "rb") as f:
            db_content = f.read()

        # Create tar archive in memory
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            tarinfo = tarfile.TarInfo(name=Path(dest_path).name)
            tarinfo.size = len(db_content)
            tar.addfile(tarinfo, io.BytesIO(db_content))

        tar_buf.seek(0)

        # Put into container
        dest_dir = str(Path(dest_path).parent)
        container.put_archive(dest_dir, tar_buf)

        logger.info(
            f"Injected master DB ({len(db_content)} bytes) -> {dest_path}"
        )

    def extract_and_merge(
        self, container: docker.models.containers.Container, source_path: str
    ) -> None:
        """Extract DB from container and merge new rows into master.

        Args:
            container: Docker container to extract DB from.
            source_path: Absolute path inside container (e.g., "/app/theforge.db").
        """
        # Extract DB from container via tar archive
        try:
            bits, _ = container.get_archive(source_path)
        except Exception as e:
            logger.error(f"DB extraction failed: {e}")
            return

        # Decode tar stream
        raw = b"".join(bits)
        tar_buf = io.BytesIO(raw)

        container_db_content = None
        with tarfile.open(fileobj=tar_buf) as tar:
            member = tar.getmembers()[0]
            f = tar.extractfile(member)
            if f:
                container_db_content = f.read()

        if not container_db_content:
            logger.error("No content extracted from container DB")
            return

        # Write to temp file for SQLite to read
        temp_db_path = self.master_db_path.with_suffix(".container.tmp")
        try:
            with open(temp_db_path, "wb") as f:
                f.write(container_db_content)

            # Merge into master
            self._merge_container_db(temp_db_path)

            logger.info(
                f"Merged container DB ({len(container_db_content)} bytes): "
                f"{self._stats}"
            )
        finally:
            if temp_db_path.exists():
                temp_db_path.unlink()

    def _merge_container_db(self, container_db_path: Path) -> None:
        """Merge new rows from container DB into master DB.

        Uses content hashing for lessons_learned to prevent duplicates.
        For other tables, relies on SQLite AUTOINCREMENT to avoid ID collisions.
        """
        master_conn = sqlite3.connect(str(self.master_db_path))
        container_conn = sqlite3.connect(str(container_db_path))

        try:
            master_conn.execute("BEGIN TRANSACTION")

            # Merge lessons_learned (dedupe by content hash)
            self._merge_lessons(master_conn, container_conn)

            # Merge agent_episodes (no dedup — each run is unique)
            self._merge_table(
                master_conn, container_conn, "agent_episodes", "episodes_merged"
            )

            # Merge decisions (no dedup — track all decisions)
            self._merge_table(
                master_conn, container_conn, "decisions", "decisions_merged"
            )

            # Merge agent_runs (telemetry — all unique)
            self._merge_table(
                master_conn, container_conn, "agent_runs", "runs_merged"
            )

            # Merge session_notes (summaries — all unique)
            self._merge_table(
                master_conn, container_conn, "session_notes", "notes_merged"
            )

            master_conn.commit()
        except Exception as e:
            master_conn.rollback()
            logger.error(f"Merge failed: {e}")
            raise
        finally:
            master_conn.close()
            container_conn.close()

    def _merge_lessons(
        self, master_conn: sqlite3.Connection, container_conn: sqlite3.Connection
    ) -> None:
        """Merge lessons with content-based deduplication."""
        # Get existing lesson content hashes from master
        existing_hashes = set()
        cursor = master_conn.execute("SELECT content FROM lessons_learned")
        for (content,) in cursor:
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            existing_hashes.add(content_hash)

        # Insert new lessons from container
        cursor = container_conn.execute(
            "SELECT project_id, content, lesson_type, task_id, created_at "
            "FROM lessons_learned"
        )

        inserted = 0
        for row in cursor:
            project_id, content, lesson_type, task_id, created_at = row
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            if content_hash not in existing_hashes:
                master_conn.execute(
                    "INSERT INTO lessons_learned "
                    "(project_id, content, lesson_type, task_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (project_id, content, lesson_type, task_id, created_at),
                )
                existing_hashes.add(content_hash)
                inserted += 1

        self._stats["lessons_merged"] += inserted

    def _merge_table(
        self,
        master_conn: sqlite3.Connection,
        container_conn: sqlite3.Connection,
        table_name: str,
        stat_key: str,
    ) -> None:
        """Merge all rows from container table to master table.

        Assumes AUTOINCREMENT primary keys — no ID conflicts.
        Copies all columns except the primary key (which is auto-assigned).
        """
        # Get column names excluding primary key
        cursor = container_conn.execute(f"PRAGMA table_info({table_name})")
        columns = [
            row[1] for row in cursor if row[5] == 0  # pk=0 means not primary key
        ]

        if not columns:
            # Table might not exist in container DB
            return

        # Copy all rows
        col_list = ", ".join(columns)
        placeholders = ", ".join("?" * len(columns))

        cursor = container_conn.execute(f"SELECT {col_list} FROM {table_name}")
        rows = cursor.fetchall()

        if rows:
            master_conn.executemany(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",
                rows,
            )
            self._stats[stat_key] += len(rows)

    def get_stats(self) -> dict[str, Any]:
        """Return accumulated merge statistics.

        Returns:
            Dict with counts of merged lessons, episodes, decisions, runs, notes.
        """
        return self._stats.copy()
