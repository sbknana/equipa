#!/usr/bin/env python3
"""EQUIPA Database Migration System.

Detects the current schema version of an existing TheForge database and applies
incremental migrations to bring it up to the latest version. Backs up the DB
before any changes.

Version detection:
- PRAGMA user_version (set by migrations and schema.sql for new installs)
- Fingerprinting (for legacy DBs that predate the migration system)

Usage:
    # Standalone
    python db_migrate.py /path/to/theforge.db

    # From equipa_setup.py (called automatically)
    from db_migrate import run_migrations
    success, from_ver, to_ver = run_migrations("/path/to/theforge.db")

Stdlib only — no pip dependencies required.

Copyright 2026 Forgeborn
"""

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# The schema version that matches the current schema.sql
CURRENT_VERSION = 4


# ============================================================
# Version detection
# ============================================================

def get_db_version(conn):
    """Read the schema version from SQLite's PRAGMA user_version.

    Returns 0 for databases that have never been versioned.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_db_version(conn, version):
    """Set the schema version via PRAGMA user_version.

    PRAGMA statements can't use parameters, so we validate the int first.
    """
    version = int(version)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def detect_legacy_version(conn):
    """Fingerprint unversioned databases by which tables exist.

    Used for databases created before the migration system was added.
    Returns the detected version number (0, 1, 2, or 3).
    """
    tables = set(
        row[0] for row in
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )

    # v3: has the agent messaging/observability tables
    if "agent_messages" in tables and "agent_actions" in tables:
        return 3

    # v2: has ForgeSmith self-improvement tables
    if "lessons_learned" in tables and "rubric_scores" in tables:
        return 2

    # v1: has the core project management tables
    if "projects" in tables and "tasks" in tables:
        return 1

    # v0: fresh or near-empty database
    return 0


def get_effective_version(conn):
    """Get the database version, falling back to fingerprinting.

    Checks PRAGMA user_version first. If 0, tries fingerprinting to
    detect legacy databases that predate the version marker.
    """
    version = get_db_version(conn)
    if version == 0:
        detected = detect_legacy_version(conn)
        if detected > 0:
            return detected
    return version


# ============================================================
# Backup
# ============================================================

def backup_database(db_path):
    """Create a timestamped backup of the database file.

    Returns the Path to the backup file.
    Raises OSError if the copy fails.
    """
    db_path = Path(db_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}_backup_{ts}{db_path.suffix}"
    backup_path = db_path.parent / backup_name
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


# ============================================================
# Audit log
# ============================================================

def ensure_schema_migrations_table(conn):
    """Create the schema_migrations audit log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL,
            description TEXT,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def log_migration(conn, from_ver, to_ver, description):
    """Record a completed migration step in the audit log."""
    conn.execute(
        "INSERT INTO schema_migrations (from_version, to_version, description) "
        "VALUES (?, ?, ?)",
        (from_ver, to_ver, description),
    )
    conn.commit()


# ============================================================
# Migration functions
# ============================================================

def migrate_v0_to_v1(conn):
    """Stamp existing v1 databases. No schema changes needed.

    v1 databases already have the core 19 tables (projects, tasks,
    decisions, etc.). This migration just sets the version marker so
    future upgrades know where we are.
    """
    pass  # No-op — v1 schema is the baseline


def migrate_v1_to_v2(conn):
    """Add ForgeSmith and agent tracking tables (v1.0 -> v2.1).

    Adds 9 tables:
      agent_runs, voice_messages, api_keys,
      lessons_learned, agent_episodes, forgesmith_runs,
      forgesmith_changes, rubric_scores, rubric_evolution_history

    Adds 2 indexes on agent_runs.
    Adds 2 views: v_cost_by_project, v_cost_by_role.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            project_id INTEGER,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            turns_used INTEGER DEFAULT 0,
            duration_s REAL DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            success INTEGER DEFAULT 0,
            outcome TEXT,
            output_tail TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS voice_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reply_to INTEGER,
            metadata TEXT,
            created_at DATETIME DEFAULT (datetime('now')),
            processed_at DATETIME
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            label TEXT NOT NULL,
            api_key TEXT NOT NULL,
            notes TEXT,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lessons_learned (
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
        );

        CREATE TABLE IF NOT EXISTS agent_episodes (
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
        );

        CREATE TABLE IF NOT EXISTS forgesmith_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            agent_runs_analyzed INTEGER DEFAULT 0,
            changes_made INTEGER DEFAULT 0,
            summary TEXT,
            mode TEXT DEFAULT 'auto'
        );

        CREATE TABLE IF NOT EXISTS forgesmith_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            target_file TEXT,
            old_value TEXT,
            new_value TEXT,
            rationale TEXT NOT NULL,
            evidence TEXT,
            effectiveness_score REAL,
            reverted_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rubric_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id INTEGER NOT NULL,
            task_id INTEGER,
            project_id INTEGER,
            role TEXT NOT NULL,
            rubric_version INTEGER DEFAULT 1,
            criteria_scores TEXT NOT NULL,
            total_score REAL NOT NULL,
            max_possible REAL NOT NULL,
            normalized_score REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rubric_evolution_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rubric_version INTEGER NOT NULL,
            role TEXT NOT NULL,
            criterion TEXT NOT NULL,
            old_weight REAL NOT NULL,
            new_weight REAL NOT NULL,
            correlation REAL NOT NULL,
            sample_size_success INTEGER,
            sample_size_failure INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_agent_runs_project
            ON agent_runs(project_id);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_role
            ON agent_runs(role);

        CREATE VIEW IF NOT EXISTS v_cost_by_project AS
        SELECT
            p.codename,
            COUNT(ar.id) as total_runs,
            SUM(ar.turns_used) as total_turns,
            ROUND(SUM(ar.duration_s), 1) as total_duration_s,
            ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
            SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs,
            SUM(CASE WHEN ar.success = 0 THEN 1 ELSE 0 END) as failed_runs
        FROM agent_runs ar
        JOIN projects p ON ar.project_id = p.id
        GROUP BY p.codename
        ORDER BY total_cost_usd DESC;

        CREATE VIEW IF NOT EXISTS v_cost_by_role AS
        SELECT
            ar.role,
            COUNT(ar.id) as total_runs,
            SUM(ar.turns_used) as total_turns,
            ROUND(SUM(ar.duration_s), 1) as total_duration_s,
            ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
            ROUND(AVG(ar.cost_usd), 4) as avg_cost_per_run,
            SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs
        FROM agent_runs ar
        GROUP BY ar.role
        ORDER BY total_cost_usd DESC;
    """)


def migrate_v2_to_v3(conn):
    """Add agent messaging and action logging tables (v2.1 -> v3.0).

    Adds 2 tables: agent_messages, agent_actions.
    Adds 2 indexes on agent_actions.
    Adds prompt_version column to agent_runs (for A/B testing).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            cycle_number INTEGER NOT NULL,
            from_role TEXT NOT NULL,
            to_role TEXT NOT NULL,
            message_type TEXT NOT NULL,
            content TEXT NOT NULL,
            read_by_cycle INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

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
        );

        CREATE INDEX IF NOT EXISTS idx_agent_actions_task
            ON agent_actions(task_id, cycle_number);
        CREATE INDEX IF NOT EXISTS idx_agent_actions_tool
            ON agent_actions(tool_name, success);
    """)

    # Add prompt_version column to agent_runs if it doesn't exist.
    # SQLite's ALTER TABLE ADD COLUMN doesn't support IF NOT EXISTS,
    # so we catch the "duplicate column" error.
    try:
        conn.execute(
            "ALTER TABLE agent_runs ADD COLUMN prompt_version TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


def migrate_v3_to_v4(conn):
    """Add impact_assessment column to forgesmith_changes (v3.0 -> v4.0).

    Stores JSON impact analysis results (affected roles, task types, risk level)
    for each ForgeSmith change. HIGH-risk changes are blocked from auto-apply.
    """
    try:
        conn.execute(
            "ALTER TABLE forgesmith_changes "
            "ADD COLUMN impact_assessment TEXT DEFAULT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# Migration registry: version -> (description, function)
MIGRATIONS = {
    1: ("Baseline schema stamp (v0 -> v1)", migrate_v0_to_v1),
    2: ("ForgeSmith + agent tracking (v1 -> v2)", migrate_v1_to_v2),
    3: ("Agent messaging + action logging (v2 -> v3)", migrate_v2_to_v3),
    4: ("Impact assessment for ForgeSmith changes (v3 -> v4)", migrate_v3_to_v4),
}


# ============================================================
# Main migration runner
# ============================================================

def run_migrations(db_path, silent=False):
    """Run all pending migrations on a database.

    Steps:
    1. Detect current version (PRAGMA or fingerprint)
    2. Back up the database
    3. Apply each migration in order
    4. Update PRAGMA user_version after each step
    5. Log each migration to schema_migrations table

    Args:
        db_path: Path to the SQLite database file.
        silent: If True, suppress print output.

    Returns:
        (success, from_version, to_version) tuple.
        success is True if all migrations applied cleanly.
    """
    db_path = Path(db_path)

    if not db_path.exists():
        if not silent:
            print(f"  ERROR: Database not found: {db_path}")
        return False, 0, 0

    conn = sqlite3.connect(str(db_path))
    try:
        from_version = get_effective_version(conn)
    finally:
        conn.close()

    if from_version >= CURRENT_VERSION:
        if not silent:
            print(f"  Database is up to date (v{from_version}).")
        return True, from_version, from_version

    # Back up before making any changes
    if not silent:
        print(f"  Detected schema version: v{from_version} (current is v{CURRENT_VERSION})")

    try:
        backup_path = backup_database(db_path)
        if not silent:
            print(f"  Backup created: {backup_path.name}")
    except OSError as e:
        if not silent:
            print(f"  WARNING: Could not create backup: {e}")
            print(f"  Proceeding without backup is risky.")
            response = input("  Continue anyway? (y/N): ").strip().lower()
            if response not in ("y", "yes"):
                print("  Migration cancelled.")
                return False, from_version, from_version
        else:
            return False, from_version, from_version

    # Apply migrations sequentially
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema_migrations_table(conn)

        current = from_version
        for target_ver in range(from_version + 1, CURRENT_VERSION + 1):
            if target_ver not in MIGRATIONS:
                if not silent:
                    print(f"  ERROR: No migration defined for v{target_ver}")
                return False, from_version, current

            description, migrate_fn = MIGRATIONS[target_ver]

            try:
                migrate_fn(conn)
                set_db_version(conn, target_ver)
                log_migration(conn, current, target_ver, description)
                current = target_ver
                if not silent:
                    print(f"    Applied migration {target_ver}: {description}")
            except Exception as e:
                if not silent:
                    print(f"  ERROR during migration {target_ver}: {e}")
                    print(f"  Database may be in a partial state.")
                    print(f"  Restore from backup: {backup_path.name}")
                return False, from_version, current

    finally:
        conn.close()

    if not silent:
        print(f"  Database upgraded successfully: v{from_version} -> v{current}")

    return True, from_version, current


# ============================================================
# CLI entry point
# ============================================================

def main():
    """Standalone migration tool.

    Usage: python db_migrate.py /path/to/theforge.db
    """
    if len(sys.argv) < 2:
        print("Usage: python db_migrate.py <path-to-theforge.db>")
        print()
        print("Detects the current schema version and applies any pending")
        print("migrations to bring the database up to date.")
        sys.exit(1)

    db_path = sys.argv[1]
    if not os.path.exists(db_path):
        print(f"ERROR: File not found: {db_path}")
        sys.exit(1)

    print(f"EQUIPA Database Migration Tool (target: v{CURRENT_VERSION})")
    print(f"Database: {db_path}")
    print()

    success, from_ver, to_ver = run_migrations(db_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
