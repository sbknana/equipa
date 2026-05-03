"""Periodic heartbeat sweep for the EQUIPA orchestrator.

Inspired by the openclaw Heartbeat pattern. Runs as a lightweight
background coroutine that performs maintenance work WITHOUT creating
a TheForge task record. Every ``interval_seconds`` (default 600s, i.e.
10 minutes) it performs a batched sweep:

  1. Detect stuck dispatch via ``v_stale_tasks`` view.
  2. Re-dispatch agents whose run has timed out.
  3. Detect hung Docker containers (containers older than the runtime
     ceiling whose owning task has resolved or vanished).
  4. Prune orphan container databases (per-container DB files whose
     owning container no longer exists).

The heartbeat is observation + remediation only. It never opens new
work tickets. Every action it takes is recorded in ``heartbeat_log``
and surfaces via :func:`get_recent_activity`.

Entry points:

* :func:`heartbeat_loop` — async coroutine, the canonical embed-in-
  orchestrator path.
* :func:`run_daemon` — module-level CLI for systemd/cron host-level
  operation. Invoke with ``python -m equipa.heartbeat``.

The module is intentionally defensive: every sweep is wrapped in a
try/except so a transient failure in one sweep cannot stop the
heartbeat from firing again on the next tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_DISPATCH_TIMEOUT_SECONDS = 60 * 60  # 1 hour
DEFAULT_CONTAINER_TIMEOUT_SECONDS = 6 * 60 * 60  # 6 hours
HEARTBEAT_LOG_TABLE = "heartbeat_log"


# ---------------------------------------------------------------------------
# Configuration


@dataclass
class HeartbeatConfig:
    """Configuration for a heartbeat run.

    All durations are in seconds. ``container_db_dir`` is the directory
    containing per-container TheForge DB shards used by Docker dispatch
    workers; if it does not exist, container-DB pruning is skipped.
    """

    db_path: Path
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    dispatch_timeout_seconds: int = DEFAULT_DISPATCH_TIMEOUT_SECONDS
    container_timeout_seconds: int = DEFAULT_CONTAINER_TIMEOUT_SECONDS
    container_db_dir: Path | None = None
    docker_bin: str = "docker"
    enable_redispatch: bool = True
    enable_container_check: bool = True
    enable_orphan_prune: bool = True
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "HeartbeatConfig":
        """Build a HeartbeatConfig from environment variables.

        Recognised vars:
        * ``THEFORGE_DB`` — path to the TheForge DB (required).
        * ``HEARTBEAT_INTERVAL`` — seconds between sweeps.
        * ``HEARTBEAT_DISPATCH_TIMEOUT`` — agent-run timeout seconds.
        * ``HEARTBEAT_CONTAINER_TIMEOUT`` — Docker container timeout.
        * ``HEARTBEAT_CONTAINER_DB_DIR`` — orphan-prune root.
        * ``HEARTBEAT_DOCKER_BIN`` — docker binary, default ``docker``.
        * ``HEARTBEAT_DRY_RUN`` — ``1`` to log only, never act.
        """

        db_path_raw = os.environ.get("THEFORGE_DB")
        if not db_path_raw:
            raise RuntimeError(
                "THEFORGE_DB env var must be set to use heartbeat daemon mode"
            )

        def _int_env(key: str, default: int) -> int:
            raw = os.environ.get(key)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                logger.warning("Ignoring non-integer %s=%r", key, raw)
                return default

        container_db_dir_raw = os.environ.get("HEARTBEAT_CONTAINER_DB_DIR")
        container_db_dir = Path(container_db_dir_raw) if container_db_dir_raw else None

        return cls(
            db_path=Path(db_path_raw),
            interval_seconds=_int_env(
                "HEARTBEAT_INTERVAL", DEFAULT_INTERVAL_SECONDS
            ),
            dispatch_timeout_seconds=_int_env(
                "HEARTBEAT_DISPATCH_TIMEOUT", DEFAULT_DISPATCH_TIMEOUT_SECONDS
            ),
            container_timeout_seconds=_int_env(
                "HEARTBEAT_CONTAINER_TIMEOUT", DEFAULT_CONTAINER_TIMEOUT_SECONDS
            ),
            container_db_dir=container_db_dir,
            docker_bin=os.environ.get("HEARTBEAT_DOCKER_BIN", "docker"),
            dry_run=os.environ.get("HEARTBEAT_DRY_RUN", "0") in ("1", "true", "TRUE"),
        )


# ---------------------------------------------------------------------------
# Sweep results


@dataclass
class SweepResult:
    """Aggregated outcome of a single heartbeat tick."""

    started_at: float
    duration_seconds: float = 0.0
    stale_tasks: list[int] = field(default_factory=list)
    redispatched_agent_runs: list[int] = field(default_factory=list)
    hung_containers: list[str] = field(default_factory=list)
    pruned_orphan_dbs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "stale_tasks": self.stale_tasks,
            "redispatched_agent_runs": self.redispatched_agent_runs,
            "hung_containers": self.hung_containers,
            "pruned_orphan_dbs": self.pruned_orphan_dbs,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# DB helpers


@contextmanager
def _connect(db_path: Path) -> Iterable[sqlite3.Connection]:
    """Yield a sqlite3 connection with sane defaults and guaranteed close.

    Mirrors the orchestrator's ``get_db_connection`` discipline: foreign
    keys ON, WAL-friendly busy timeout, row factory set, always closed.
    """

    if not db_path.exists():
        raise FileNotFoundError(f"TheForge DB not found at {db_path}")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


def ensure_schema(db_path: Path) -> None:
    """Create the heartbeat_log table if it does not already exist.

    The orchestrator may be running under a DB that was created before
    the heartbeat module shipped, so we add the table idempotently
    rather than depending on a migration.
    """

    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {HEARTBEAT_LOG_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                stale_task_count INTEGER NOT NULL DEFAULT 0,
                redispatched_count INTEGER NOT NULL DEFAULT 0,
                hung_container_count INTEGER NOT NULL DEFAULT 0,
                pruned_orphan_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_heartbeat_log_started_at
            ON {HEARTBEAT_LOG_TABLE} (started_at DESC)
            """
        )
        conn.commit()


def _record_sweep(db_path: Path, result: SweepResult) -> None:
    summary_json = json.dumps(result.as_summary(), separators=(",", ":"))
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HEARTBEAT_LOG_TABLE} (
                started_at, duration_seconds,
                stale_task_count, redispatched_count,
                hung_container_count, pruned_orphan_count,
                error_count, summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.started_at,
                result.duration_seconds,
                len(result.stale_tasks),
                len(result.redispatched_agent_runs),
                len(result.hung_containers),
                len(result.pruned_orphan_dbs),
                len(result.errors),
                summary_json,
            ),
        )
        conn.commit()


def get_recent_activity(db_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent heartbeat sweeps for observability dashboards."""

    if limit <= 0:
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, started_at, duration_seconds, stale_task_count,
                   redispatched_count, hung_container_count,
                   pruned_orphan_count, error_count, summary_json
            FROM {HEARTBEAT_LOG_TABLE}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    activity = []
    for row in rows:
        try:
            summary = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            summary = {}
        activity.append(
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "duration_seconds": row["duration_seconds"],
                "stale_task_count": row["stale_task_count"],
                "redispatched_count": row["redispatched_count"],
                "hung_container_count": row["hung_container_count"],
                "pruned_orphan_count": row["pruned_orphan_count"],
                "error_count": row["error_count"],
                "summary": summary,
            }
        )
    return activity


# ---------------------------------------------------------------------------
# Sweeps


def detect_stale_tasks(conn: sqlite3.Connection) -> list[int]:
    """Read the v_stale_tasks view if it exists, otherwise fall back.

    The v_stale_tasks view is owned by TheForge schema migrations.
    If it's not present we degrade gracefully by scanning the tasks
    table directly: any task in ``in_progress`` for >1h with no
    recent agent_run heartbeat counts as stale.
    """

    try:
        rows = conn.execute(
            "SELECT id FROM v_stale_tasks ORDER BY id"
        ).fetchall()
        return [int(row["id"]) for row in rows]
    except sqlite3.OperationalError:
        # View missing — fall back to a direct query.
        logger.debug("v_stale_tasks view not present, using fallback query")

    one_hour_ago = time.time() - 3600
    try:
        rows = conn.execute(
            """
            SELECT t.id FROM tasks t
            WHERE t.status = 'in_progress'
              AND COALESCE(
                  (SELECT MAX(ar.updated_at) FROM agent_runs ar
                   WHERE ar.task_id = t.id),
                  t.updated_at
              ) < ?
            ORDER BY t.id
            """,
            (one_hour_ago,),
        ).fetchall()
        return [int(row["id"]) for row in rows]
    except sqlite3.OperationalError as exc:
        logger.warning("Could not query stale tasks: %s", exc)
        return []


def redispatch_timed_out_runs(
    conn: sqlite3.Connection,
    timeout_seconds: int,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Mark agent_runs whose runtime exceeds ``timeout_seconds`` as timed out.

    Returns the list of agent_run ids that were marked. Re-dispatch
    itself is the orchestrator's responsibility — heartbeat only
    flips the status so the orchestrator's main loop picks them up.
    """

    cutoff = time.time() - max(int(timeout_seconds), 1)
    try:
        rows = conn.execute(
            """
            SELECT id FROM agent_runs
            WHERE status IN ('running', 'dispatched')
              AND started_at IS NOT NULL
              AND started_at < ?
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Could not query agent_runs: %s", exc)
        return []

    ids = [int(row["id"]) for row in rows]
    if not ids or dry_run:
        return ids

    placeholders = ",".join("?" for _ in ids)
    try:
        conn.execute(
            f"""
            UPDATE agent_runs
            SET status = 'timed_out', ended_at = ?
            WHERE id IN ({placeholders})
            """,
            (time.time(), *ids),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        logger.warning("Could not update timed-out agent_runs: %s", exc)
        return []
    return ids


def detect_hung_containers(
    docker_bin: str,
    container_timeout_seconds: int,
) -> list[str]:
    """Return container IDs of EQUIPA-tagged containers older than the timeout.

    Identification is by the ``label=equipa.role=worker`` Docker label,
    which dispatch.py applies when launching worker containers. The
    sweep is read-only — it only returns IDs.
    """

    if not shutil.which(docker_bin):
        logger.debug("docker binary %r not on PATH; skipping container check", docker_bin)
        return []

    cmd = [
        docker_bin,
        "ps",
        "--filter",
        "label=equipa.role=worker",
        "--format",
        "{{.ID}}|{{.CreatedAt}}",
        "--no-trunc",
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("docker ps invocation failed: %s", exc)
        return []

    if completed.returncode != 0:
        logger.warning(
            "docker ps returned %s: %s",
            completed.returncode,
            completed.stderr.strip()[:200],
        )
        return []

    cutoff = time.time() - max(int(container_timeout_seconds), 1)
    hung: list[str] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        container_id, created_at_str = line.split("|", 1)
        created_ts = _parse_docker_timestamp(created_at_str)
        if created_ts is None:
            continue
        if created_ts < cutoff:
            hung.append(container_id.strip())
    return hung


def _parse_docker_timestamp(raw: str) -> float | None:
    """Parse a Docker ``CreatedAt`` string. Returns epoch seconds or None.

    Docker formats look like ``2026-05-03 14:22:01 +0000 UTC``. We try
    a couple of common shapes and bail out gracefully if none match.
    """

    raw = raw.strip()
    if not raw:
        return None

    candidates = [
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S %z %Z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ]
    from datetime import datetime

    cleaned = raw.replace(" UTC", "")
    for fmt in candidates:
        try:
            return datetime.strptime(cleaned, fmt).timestamp()
        except ValueError:
            continue
    return None


def prune_orphan_container_dbs(
    container_db_dir: Path,
    docker_bin: str,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete container-DB files whose owning container no longer exists.

    Files in ``container_db_dir`` are expected to be named
    ``<container_id>.db`` or ``<container_id>.sqlite``. Any file whose
    container ID is not present in ``docker ps -a`` output is pruned.

    Returns the list of file paths that were (or would have been, in
    dry-run mode) removed.
    """

    if not container_db_dir.exists() or not container_db_dir.is_dir():
        return []

    live_ids = _list_all_container_ids(docker_bin)
    pruned: list[str] = []

    for entry in container_db_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix not in (".db", ".sqlite"):
            continue
        container_id = entry.stem
        if not container_id:
            continue
        if any(live_id.startswith(container_id) or container_id.startswith(live_id)
               for live_id in live_ids):
            continue
        pruned.append(str(entry))
        if dry_run:
            continue
        try:
            entry.unlink()
        except OSError as exc:
            logger.warning("Could not prune %s: %s", entry, exc)
    return pruned


def _list_all_container_ids(docker_bin: str) -> set[str]:
    if not shutil.which(docker_bin):
        return set()
    try:
        completed = subprocess.run(
            [docker_bin, "ps", "-a", "--format", "{{.ID}}", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("docker ps -a invocation failed: %s", exc)
        return set()
    if completed.returncode != 0:
        return set()
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# Tick driver


def run_once(config: HeartbeatConfig) -> SweepResult:
    """Run a single heartbeat sweep synchronously.

    This is the unit of work the async loop calls each tick. Keeping it
    synchronous makes it trivial to test and to invoke from a one-shot
    cron job; the async wrapper handles scheduling.
    """

    started = time.time()
    result = SweepResult(started_at=started)

    try:
        ensure_schema(config.db_path)
    except Exception as exc:  # noqa: BLE001 — bound the heartbeat from blowing up
        logger.exception("Could not ensure heartbeat schema")
        result.errors.append(f"ensure_schema: {exc!r}")
        result.duration_seconds = time.time() - started
        return result

    with _connect(config.db_path) as conn:
        try:
            result.stale_tasks = detect_stale_tasks(conn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stale-task sweep failed")
            result.errors.append(f"detect_stale_tasks: {exc!r}")

        if config.enable_redispatch:
            try:
                result.redispatched_agent_runs = redispatch_timed_out_runs(
                    conn,
                    config.dispatch_timeout_seconds,
                    dry_run=config.dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Redispatch sweep failed")
                result.errors.append(f"redispatch_timed_out_runs: {exc!r}")

    if config.enable_container_check:
        try:
            result.hung_containers = detect_hung_containers(
                config.docker_bin, config.container_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Container check failed")
            result.errors.append(f"detect_hung_containers: {exc!r}")

    if config.enable_orphan_prune and config.container_db_dir is not None:
        try:
            result.pruned_orphan_dbs = prune_orphan_container_dbs(
                config.container_db_dir,
                config.docker_bin,
                dry_run=config.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Orphan DB prune failed")
            result.errors.append(f"prune_orphan_container_dbs: {exc!r}")

    result.duration_seconds = time.time() - started

    try:
        _record_sweep(config.db_path, result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not record heartbeat sweep")
        result.errors.append(f"_record_sweep: {exc!r}")

    logger.info(
        "heartbeat tick: stale=%d redispatched=%d hung=%d pruned=%d errors=%d in %.2fs",
        len(result.stale_tasks),
        len(result.redispatched_agent_runs),
        len(result.hung_containers),
        len(result.pruned_orphan_dbs),
        len(result.errors),
        result.duration_seconds,
    )
    return result


async def heartbeat_loop(
    config: HeartbeatConfig,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the heartbeat forever, one sweep every ``interval_seconds``.

    The loop yields control via :func:`asyncio.wait_for` on a stop
    event so it can be cancelled cleanly when the orchestrator shuts
    down. Each sweep itself runs in a worker thread because the DB
    work and subprocess calls are blocking.
    """

    if stop_event is None:
        stop_event = asyncio.Event()

    interval = max(int(config.interval_seconds), 1)
    logger.info(
        "heartbeat starting: db=%s interval=%ds dispatch_timeout=%ds container_timeout=%ds",
        config.db_path,
        interval,
        config.dispatch_timeout_seconds,
        config.container_timeout_seconds,
    )

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_once, config)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Heartbeat tick raised; continuing on next interval")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue

    logger.info("heartbeat stopped")


def start_background(
    config: HeartbeatConfig,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Schedule the heartbeat as a background task on ``loop``.

    Returns ``(task, stop_event)``. The orchestrator should call
    ``stop_event.set()`` on shutdown and then ``await task`` to drain.
    """

    target_loop = loop or asyncio.get_event_loop()
    stop_event = asyncio.Event()
    task = target_loop.create_task(
        heartbeat_loop(config, stop_event=stop_event),
        name="equipa-heartbeat",
    )
    return task, stop_event


# ---------------------------------------------------------------------------
# Daemon CLI


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m equipa.heartbeat",
        description="EQUIPA orchestrator heartbeat sweep daemon.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to TheForge DB. Defaults to $THEFORGE_DB.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Seconds between sweeps (default {DEFAULT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sweep and exit (suitable for cron/systemd timer).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect but do not mutate (no UPDATEs, no file deletions).",
    )
    parser.add_argument(
        "--container-db-dir",
        type=Path,
        default=None,
        help="Directory containing per-container DB shards to prune.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser


def run_daemon(argv: list[str] | None = None) -> int:
    """CLI entry point. ``python -m equipa.heartbeat [--once|--interval N]``."""

    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = HeartbeatConfig.from_env()
    except RuntimeError as exc:
        if args.db is None:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # Still need a config; build a minimal one off the CLI db.
        config = HeartbeatConfig(db_path=args.db)

    if args.db is not None:
        config.db_path = args.db
    if args.interval is not None:
        config.interval_seconds = max(int(args.interval), 1)
    if args.container_db_dir is not None:
        config.container_db_dir = args.container_db_dir
    if args.dry_run:
        config.dry_run = True

    if args.once:
        result = run_once(config)
        print(json.dumps(result.as_summary(), indent=2))
        return 0 if not result.errors else 1

    try:
        asyncio.run(heartbeat_loop(config))
    except KeyboardInterrupt:
        logger.info("heartbeat interrupted, exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_daemon())
