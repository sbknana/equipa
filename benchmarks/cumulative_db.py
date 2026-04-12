#!/usr/bin/env python3
"""
CumulativeDB — Accumulate and inject knowledge across benchmark runs.

Extracts lessons_learned, agent_episodes, and decisions from each container's
theforge.db and merges them into a master cumulative database. On subsequent
runs, inject this accumulated knowledge into new containers so agents benefit
from prior experience.

(c) 2026 Forgeborn
"""

import io
import sqlite3
import tarfile
from pathlib import Path
from typing import Optional


class CumulativeDB:
    """Manages cumulative knowledge database for warm-start benchmarking."""

    def __init__(self, master_db_path: str):
        """Initialize with path to master cumulative database.

        Args:
            master_db_path: Path to the master SQLite database file.
                           Created if it doesn't exist.
        """
        self.master_path = Path(master_db_path)
        self.master_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_master_db()

    def _init_master_db(self):
        """Initialize master DB schema if it doesn't exist."""
        conn = sqlite3.connect(str(self.master_path))
        try:
            # Create tables matching TheForge schema for knowledge tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lessons_learned (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    category TEXT,
                    lesson TEXT,
                    context TEXT,
                    outcome TEXT,
                    effectiveness_score REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source_instance TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    task_id INTEGER,
                    agent_role TEXT,
                    turns INTEGER,
                    outcome TEXT,
                    summary TEXT,
                    key_decisions TEXT,
                    cost REAL,
                    duration REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source_instance TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    topic TEXT,
                    decision TEXT,
                    rationale TEXT,
                    alternatives_considered TEXT,
                    decision_type TEXT DEFAULT 'general',
                    status TEXT DEFAULT 'open',
                    resolved_by_task_id INTEGER,
                    verified_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    source_instance TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def inject_into_container(self, container, equipa_docker_dir: str = "/opt/equipa"):
        """Seed a container's theforge.db with accumulated knowledge.

        Args:
            container: Docker container object
            equipa_docker_dir: Path to EQUIPA directory inside container
        """
        if not self.master_path.exists():
            return  # No cumulative data yet

        conn = sqlite3.connect(str(self.master_path))
        try:
            # Read all lessons
            lessons = conn.execute(
                "SELECT category, lesson, context, outcome, effectiveness_score "
                "FROM lessons_learned"
            ).fetchall()

            # Read all episodes
            episodes = conn.execute(
                "SELECT agent_role, turns, outcome, summary, key_decisions, cost, duration "
                "FROM agent_episodes"
            ).fetchall()

            # Read all decisions
            decisions = conn.execute(
                "SELECT topic, decision, rationale, alternatives_considered, "
                "decision_type, status "
                "FROM decisions"
            ).fetchall()
        finally:
            conn.close()

        if not (lessons or episodes or decisions):
            return  # No data to inject

        # Build injection script
        inject_script = f"""#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('{equipa_docker_dir}/theforge.db')
cursor = conn.cursor()

# Inject lessons
lessons = {repr(lessons)}
for lesson in lessons:
    cursor.execute('''
        INSERT INTO lessons_learned (project_id, category, lesson, context, outcome, effectiveness_score)
        VALUES (23, ?, ?, ?, ?, ?)
    ''', lesson)

# Inject episodes
episodes = {repr(episodes)}
for episode in episodes:
    cursor.execute('''
        INSERT INTO agent_episodes (project_id, agent_role, turns, outcome, summary, key_decisions, cost, duration)
        VALUES (23, ?, ?, ?, ?, ?, ?, ?)
    ''', episode)

# Inject decisions
decisions = {repr(decisions)}
for decision in decisions:
    cursor.execute('''
        INSERT INTO decisions (project_id, topic, decision, rationale, alternatives_considered, decision_type, status)
        VALUES (23, ?, ?, ?, ?, ?, ?)
    ''', decision)

conn.commit()
conn.close()
print(f"Injected {{len(lessons)}} lessons, {{len(episodes)}} episodes, {{len(decisions)}} decisions")
"""

        # Write script to container
        script_bytes = inject_script.encode("utf-8")
        inject_tar = io.BytesIO()
        with tarfile.open(fileobj=inject_tar, mode="w") as tar:
            info = tarfile.TarInfo(name="inject_cumulative.py")
            info.size = len(script_bytes)
            tar.addfile(info, io.BytesIO(script_bytes))
        inject_tar.seek(0)
        container.put_archive("/tmp", inject_tar)

        # Execute injection
        result = container.exec_run(
            f'/bin/bash -c "cd /tmp && python3 inject_cumulative.py"',
            user="root",
        )
        if result.exit_code == 0:
            print(f"    Cumulative DB injected: {result.output.decode().strip()}")
        else:
            print(f"    WARNING: Cumulative injection failed: {result.output.decode()[:200]}")

    def extract_and_merge(self, container, equipa_docker_dir: str = "/opt/equipa",
                         instance_id: str = "unknown"):
        """Extract knowledge from container DB and merge into master.

        Args:
            container: Docker container object
            equipa_docker_dir: Path to EQUIPA directory inside container
            instance_id: Benchmark instance ID for tracking source
        """
        try:
            # Extract theforge.db from container
            bits, _ = container.get_archive(f"{equipa_docker_dir}/theforge.db")
            raw = b"".join(bits)
            tar_buf = io.BytesIO(raw)

            # Extract DB file to temp location
            with tarfile.open(fileobj=tar_buf) as tar:
                member = tar.getmembers()[0]
                f = tar.extractfile(member)
                if not f:
                    print(f"    WARNING: Could not extract DB for {instance_id}")
                    return

                container_db_data = f.read()

            # Write to temp file for SQLite access
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp.write(container_db_data)
                tmp_path = tmp.name

            try:
                # Read from container DB
                src_conn = sqlite3.connect(tmp_path)
                dst_conn = sqlite3.connect(str(self.master_path))

                try:
                    # Merge lessons_learned
                    lessons = src_conn.execute("""
                        SELECT project_id, category, lesson, context, outcome,
                               effectiveness_score
                        FROM lessons_learned
                    """).fetchall()

                    for lesson in lessons:
                        dst_conn.execute("""
                            INSERT INTO lessons_learned
                            (project_id, category, lesson, context, outcome,
                             effectiveness_score, source_instance)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (*lesson, instance_id))

                    # Merge agent_episodes
                    episodes = src_conn.execute("""
                        SELECT project_id, task_id, agent_role, turns, outcome,
                               summary, key_decisions, cost, duration
                        FROM agent_episodes
                    """).fetchall()

                    for episode in episodes:
                        dst_conn.execute("""
                            INSERT INTO agent_episodes
                            (project_id, task_id, agent_role, turns, outcome,
                             summary, key_decisions, cost, duration, source_instance)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (*episode, instance_id))

                    # Merge decisions
                    decisions = src_conn.execute("""
                        SELECT project_id, topic, decision, rationale,
                               alternatives_considered, decision_type, status
                        FROM decisions
                    """).fetchall()

                    for decision in decisions:
                        dst_conn.execute("""
                            INSERT INTO decisions
                            (project_id, topic, decision, rationale,
                             alternatives_considered, decision_type, status,
                             source_instance)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (*decision, instance_id))

                    dst_conn.commit()

                    print(f"    Cumulative: merged {len(lessons)} lessons, "
                          f"{len(episodes)} episodes, {len(decisions)} decisions")

                finally:
                    src_conn.close()
                    dst_conn.close()

            finally:
                # Clean up temp file
                Path(tmp_path).unlink(missing_ok=True)

        except Exception as e:
            print(f"    WARNING: Cumulative merge failed for {instance_id}: {e}")

    def get_stats(self) -> dict:
        """Get cumulative statistics.

        Returns:
            Dict with counts of lessons, episodes, decisions
        """
        if not self.master_path.exists():
            return {"lessons": 0, "episodes": 0, "decisions": 0}

        conn = sqlite3.connect(str(self.master_path))
        try:
            lessons = conn.execute("SELECT COUNT(*) FROM lessons_learned").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM agent_episodes").fetchone()[0]
            decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            return {
                "lessons": lessons,
                "episodes": episodes,
                "decisions": decisions
            }
        finally:
            conn.close()
