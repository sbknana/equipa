"""Training Results Auto-Ingest Script.

Scans the training results directory (TRAINING_RESULTS_DIR env var) for unprocessed completion
marker JSON files, inserts records into model_registry via TheForge SQLite,
and moves processed markers to ./processed/.

Designed to run as a nightly cron job:
  0 6 * * * /usr/bin/python3 $EQUIPA_BASE/ingest_training_results.py

Can also be run manually:
  python ingest_training_results.py
  python ingest_training_results.py --results-dir /custom/path
  python ingest_training_results.py --dry-run

Copyright 2026 Forgeborn. All rights reserved.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Default paths — resolved from env vars, fallback to script-relative
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = os.environ.get(
    "TRAINING_RESULTS_DIR",
    str(_SCRIPT_DIR.parent / "training-results"),
)
DEFAULT_DB_PATH = os.environ.get(
    "THEFORGE_DB",
    str(_SCRIPT_DIR / "theforge.db"),
)

# Project ID for cryptotrader-v2 in TheForge
# Look up dynamically if possible, fall back to this default
DEFAULT_PROJECT_ID = 56

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ingest_training_results")


def get_project_id(conn: sqlite3.Connection, project_name: str = "cryptotrader") -> int:
    """Look up the project_id for a given project name.

    Falls back to DEFAULT_PROJECT_ID if not found.
    """
    cursor = conn.execute(
        "SELECT id FROM projects WHERE name LIKE ? OR codename LIKE ? LIMIT 1",
        (f"%{project_name}%", f"%{project_name}%"),
    )
    row = cursor.fetchone()
    if row:
        return row[0]
    logger.warning(
        "Project '%s' not found in database, using default project_id=%d",
        project_name,
        DEFAULT_PROJECT_ID,
    )
    return DEFAULT_PROJECT_ID


def validate_marker(marker: dict) -> list[str]:
    """Validate that a marker JSON has required fields.

    Returns a list of validation error messages (empty if valid).
    """
    errors = []

    required_fields = ["model_type", "version", "trained_on", "trained_at"]
    for field in required_fields:
        if not marker.get(field):
            errors.append(f"Missing required field: {field}")

    # Validate model_type is a reasonable string
    model_type = marker.get("model_type", "")
    if model_type and len(model_type) > 100:
        errors.append(f"model_type too long: {len(model_type)} chars")

    # Validate numeric fields are in reasonable ranges
    avg_da = marker.get("avg_directional_accuracy")
    if avg_da is not None and not (0.0 <= avg_da <= 100.0):
        errors.append(f"avg_directional_accuracy out of range: {avg_da}")

    return errors


def insert_model_record(
    conn: sqlite3.Connection,
    project_id: int,
    marker: dict,
) -> int:
    """Insert a model_registry record from a marker dict.

    Returns the inserted row id.
    """
    cursor = conn.execute(
        """INSERT INTO model_registry (
            project_id, model_type, version, symbol_count,
            avg_directional_accuracy, median_da, above_50_pct,
            top_symbol, top_da, trained_on, model_path,
            synced_to, trained_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            marker["model_type"],
            marker["version"],
            marker.get("symbol_count"),
            marker.get("avg_directional_accuracy"),
            marker.get("median_da"),
            marker.get("above_50_pct"),
            marker.get("top_symbol"),
            marker.get("top_da"),
            marker["trained_on"],
            marker.get("model_path"),
            marker.get("synced_to"),
            marker["trained_at"],
            marker.get("notes"),
        ),
    )
    return cursor.lastrowid


def process_marker_file(
    filepath: Path,
    conn: sqlite3.Connection,
    project_id: int,
    processed_dir: Path,
    dry_run: bool = False,
) -> bool:
    """Process a single marker JSON file.

    Returns True if successfully processed, False otherwise.
    """
    logger.info("Processing marker: %s", filepath.name)

    try:
        with open(filepath) as f:
            marker = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", filepath.name, exc)
        return False

    # Validate
    errors = validate_marker(marker)
    if errors:
        logger.error(
            "Validation failed for %s: %s",
            filepath.name,
            "; ".join(errors),
        )
        return False

    if dry_run:
        logger.info(
            "[DRY RUN] Would insert: model_type=%s version=%s avg_da=%.1f%% trained_on=%s",
            marker["model_type"],
            marker["version"],
            marker.get("avg_directional_accuracy", 0),
            marker["trained_on"],
        )
        return True

    try:
        row_id = insert_model_record(conn, project_id, marker)
        conn.commit()
        logger.info(
            "Inserted model_registry id=%d: %s %s (avg DA: %.1f%%)",
            row_id,
            marker["model_type"],
            marker["version"],
            marker.get("avg_directional_accuracy", 0),
        )
    except sqlite3.Error as exc:
        logger.error("Database insert failed for %s: %s", filepath.name, exc)
        conn.rollback()
        return False

    # Move to processed directory
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / filepath.name

    # Handle filename collision by appending counter
    if dest.exists():
        stem = filepath.stem
        suffix = filepath.suffix
        counter = 1
        while dest.exists():
            dest = processed_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        shutil.move(str(filepath), str(dest))
        logger.info("Moved to processed: %s", dest.name)
    except OSError as exc:
        logger.warning(
            "Insert succeeded but failed to move %s to processed: %s",
            filepath.name,
            exc,
        )

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest training completion markers into TheForge model_registry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Cron setup (nightly at 6 AM):
  0 6 * * * /usr/bin/python3 $EQUIPA_BASE/ingest_training_results.py >> /var/log/training-ingest.log 2>&1

Environment variables:
  THEFORGE_DB  Override the default TheForge database path
        """,
    )
    parser.add_argument(
        "--results-dir",
        default=DEFAULT_RESULTS_DIR,
        help=f"Directory to scan for marker files (default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to TheForge SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be ingested without modifying the database",
    )
    parser.add_argument(
        "--project-name",
        default="cryptotrader",
        help="Project name to look up in TheForge (default: cryptotrader)",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        logger.info("Results directory does not exist: %s (nothing to process)", results_dir)
        return

    # Find unprocessed marker files
    marker_files = sorted(results_dir.glob("*.json"))
    if not marker_files:
        logger.info("No marker files found in %s", results_dir)
        return

    logger.info("Found %d marker file(s) in %s", len(marker_files), results_dir)

    processed_dir = results_dir / "processed"

    conn = None
    try:
        if not args.dry_run:
            conn = sqlite3.connect(args.db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

        project_id = DEFAULT_PROJECT_ID
        if conn:
            project_id = get_project_id(conn, args.project_name)
        logger.info("Using project_id=%d", project_id)

        success_count = 0
        error_count = 0

        for filepath in marker_files:
            ok = process_marker_file(
                filepath,
                conn,
                project_id,
                processed_dir,
                dry_run=args.dry_run,
            )
            if ok:
                success_count += 1
            else:
                error_count += 1

        logger.info(
            "Ingest complete: %d succeeded, %d failed, %d total",
            success_count,
            error_count,
            len(marker_files),
        )

    finally:
        if conn:
            conn.close()

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

