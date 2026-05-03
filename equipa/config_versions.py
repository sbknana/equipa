"""EQUIPA config snapshot / diff / rollback service.

Implements PLAN-1067 §1.A2 — the snapshotting layer on top of the v10 schema
(``config_versions`` + ``config_version_files``) created in A1.

Public surface:
    - :func:`snapshot` — capture the current bytes of tracked config files.
    - :func:`list_versions` — list snapshots for a project (most recent first).
    - :func:`diff` — unified diff between two snapshot ids.
    - :func:`rollback` — restore tracked files to a prior snapshot's contents.

Safety contract:
    - All writes use atomic-rename (``write to tmp + os.replace``).
    - All file paths are resolved and confirmed to live under ``REPO_ROOT``
      via :meth:`Path.is_relative_to` — re-uses the EQ-39 mitigation pattern.
    - Secrets in JSON config files are redacted before being written to the
      DB blob (denylist: ``api_key`` and ``*_secret`` / ``*_password`` /
      ``*_token`` suffixes).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from equipa.constants import PROMPTS_DIR
from equipa.db import db_conn

logger = logging.getLogger(__name__)


# --- Repo-root resolution ---

# Repo root is the parent of the ``equipa`` package directory. Mirrors the
# anchor used by ``equipa.constants`` (PROMPTS_DIR, THEFORGE_DB defaults).
REPO_ROOT: Path = Path(__file__).parent.parent.resolve()

DEFAULT_CONFIG_FILES: tuple[str, ...] = (
    "dispatch_config.json",
    "forge_config.json",
)

REDACTED_SENTINEL = "<REDACTED>"

# Denylist of key names whose values must be redacted before storing the
# blob. Matched on the leaf key only. Patterns ending in ``*`` denote a
# suffix wildcard ("*_secret" matches "stripe_secret" but not "secret_id").
_REDACT_EXACT: frozenset[str] = frozenset({"api_key"})
_REDACT_SUFFIXES: tuple[str, ...] = ("_secret", "_password", "_token")


# --- Internal helpers ---

def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp matching the format used by other equipa rows."""
    return datetime.now(timezone.utc).isoformat()


def _is_secret_key(key: str) -> bool:
    """Whether ``key`` matches the redaction denylist."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _REDACT_EXACT:
        return True
    return any(lowered.endswith(suffix) for suffix in _REDACT_SUFFIXES)


def _redact(value: object) -> object:
    """Recursively walk ``value`` redacting any dict entries whose key
    matches the denylist. Returns a new object — does not mutate input."""
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if _is_secret_key(k):
                out[k] = REDACTED_SENTINEL
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redacted_blob(file_path: Path, raw_text: str) -> str:
    """Return the storage blob for ``file_path``.

    JSON files are parsed, redacted, and re-serialised with stable
    formatting. Files that fail to parse or are not JSON are stored
    verbatim — prompt markdown has no key/value structure to redact.
    """
    if file_path.suffix.lower() != ".json":
        return raw_text
    try:
        parsed = json.loads(raw_text)
    except (ValueError, json.JSONDecodeError):
        # Malformed JSON — store as-is rather than dropping content.
        logger.warning(
            "[config_versions] %s did not parse as JSON; storing raw text",
            file_path,
        )
        return raw_text
    redacted = _redact(parsed)
    # sort_keys=True so re-snapshots of equivalent content produce a stable
    # blob (and therefore a stable file_sha) even if a key reorder happened.
    return json.dumps(redacted, indent=2, sort_keys=True)


def _resolve_under_repo(path: Path) -> Path:
    """Resolve ``path`` and refuse anything that escapes ``REPO_ROOT``.

    Uses :meth:`Path.is_relative_to` (Python 3.9+) on the resolved paths so
    symlinks pointing outside the repo are rejected, mirroring the EQ-39
    mitigation pattern used elsewhere in the codebase.
    """
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        raise ValueError(
            f"Refusing to operate on path outside repo root: {path} "
            f"(resolved={resolved}, repo_root={REPO_ROOT})"
        )
    return resolved


def _file_sha(blob: str) -> str:
    """SHA-256 of the storage blob (post-redaction)."""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _aggregate_sha(file_entries: list[tuple[str, str]]) -> str:
    """SHA-256 over sorted (file_path, file_sha) pairs.

    Matches the contract documented in schema.sql for ``content_sha``.
    """
    sha = hashlib.sha256()
    for rel_path, fsha in sorted(file_entries):
        sha.update(rel_path.encode("utf-8"))
        sha.update(b"\0")
        sha.update(fsha.encode("utf-8"))
        sha.update(b"\0")
    return sha.hexdigest()


def _default_files() -> list[Path]:
    """The set of files snapshotted when caller doesn't supply ``files``."""
    out: list[Path] = []
    for name in DEFAULT_CONFIG_FILES:
        candidate = REPO_ROOT / name
        if candidate.exists():
            out.append(candidate)
    if PROMPTS_DIR.exists():
        out.extend(sorted(PROMPTS_DIR.glob("*.md")))
    return out


def _rel_path_str(path: Path) -> str:
    """Stable, forward-slash, repo-relative string for a tracked file."""
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    Writes to a sibling tmp file in the same directory then ``os.replace``s
    it into position so a crash mid-write cannot leave a half-written file
    on disk.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the tmp file if anything goes wrong.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _latest_version_for(conn, project_id: int) -> tuple[int, str] | None:
    row = conn.execute(
        """
        SELECT id, content_sha
        FROM config_versions
        WHERE project_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"]), str(row["content_sha"])


def _build_file_entries(
    files: Iterable[Path],
) -> list[tuple[str, str, str, int]]:
    """Read ``files`` and return ``(rel_path, blob, file_sha, byte_size)``
    tuples. Each path is path-traversal-checked first."""
    entries: list[tuple[str, str, str, int]] = []
    for raw_path in files:
        path = _resolve_under_repo(Path(raw_path))
        if not path.is_file():
            raise FileNotFoundError(f"Tracked file does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        blob = _redacted_blob(path, text)
        entries.append((
            _rel_path_str(path),
            blob,
            _file_sha(blob),
            len(blob.encode("utf-8")),
        ))
    return entries


# --- Public API ---

def snapshot(
    project_id: int,
    source: str,
    commit_message: str | None = None,
    files: Iterable[Path] | None = None,
) -> int:
    """Capture the current bytes of tracked config files as a new version.

    Args:
        project_id: TheForge project id this snapshot belongs to.
        source: One of ``manual`` / ``auto-dispatch`` / ``auto-cli`` /
            ``auto-rollback`` (matches the schema CHECK constraint).
        commit_message: Optional human description.
        files: Iterable of file paths to snapshot. When ``None`` (default),
            uses :func:`_default_files` — the dispatch + forge configs and
            every ``prompts/*.md``.

    Returns:
        The ``id`` of the new ``config_versions`` row, OR the id of the
        existing latest row if the aggregate hash matches (dedup).
    """
    if not isinstance(project_id, int) or project_id <= 0:
        raise ValueError(f"project_id must be a positive int, got {project_id!r}")
    if source not in {"manual", "auto-dispatch", "auto-cli", "auto-rollback"}:
        raise ValueError(f"unsupported source: {source!r}")

    file_list = list(files) if files is not None else _default_files()
    if not file_list:
        raise ValueError("No files to snapshot (and no defaults found on disk)")

    entries = _build_file_entries(file_list)
    content_sha = _aggregate_sha([(rel, fsha) for rel, _, fsha, _ in entries])

    with db_conn(write=True) as conn:
        latest = _latest_version_for(conn, project_id)
        if latest is not None and latest[1] == content_sha:
            return latest[0]

        parent_version_id = latest[0] if latest is not None else None
        cursor = conn.execute(
            """
            INSERT INTO config_versions
                (project_id, created_at, source, commit_message,
                 content_sha, parent_version_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                _utcnow_iso(),
                source,
                commit_message,
                content_sha,
                parent_version_id,
            ),
        )
        new_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO config_version_files
                (version_id, file_path, content_blob, file_sha, byte_size)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (new_id, rel, blob, fsha, size)
                for rel, blob, fsha, size in entries
            ],
        )
        return new_id


def list_versions(project_id: int, limit: int = 50) -> list[dict]:
    """Return the most recent ``limit`` snapshots for ``project_id``."""
    if limit <= 0:
        return []
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, source, commit_message, content_sha
            FROM config_versions
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "created_at": r["created_at"],
            "source": r["source"],
            "commit_message": r["commit_message"],
            "content_sha": r["content_sha"],
        }
        for r in rows
    ]


def _files_for_version(conn, version_id: int) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT file_path, content_blob
        FROM config_version_files
        WHERE version_id = ?
        """,
        (version_id,),
    ).fetchall()
    if not rows:
        # Distinguish "version exists but has no files" (impossible by
        # contract) from "version doesn't exist at all".
        version_row = conn.execute(
            "SELECT id FROM config_versions WHERE id = ?", (version_id,),
        ).fetchone()
        if version_row is None:
            raise ValueError(f"Unknown config version id: {version_id}")
    return {str(r["file_path"]): str(r["content_blob"]) for r in rows}


def diff(version_a_id: int, version_b_id: int) -> dict[str, str]:
    """Return ``{file_path: unified_diff_text}`` for the two versions.

    Files only in A are reported as deletions (B is empty); files only in
    B are reported as additions (A is empty). Files identical in both
    versions are omitted from the result.
    """
    with db_conn() as conn:
        files_a = _files_for_version(conn, version_a_id)
        files_b = _files_for_version(conn, version_b_id)

    diffs: dict[str, str] = {}
    for path in sorted(set(files_a) | set(files_b)):
        a_text = files_a.get(path, "")
        b_text = files_b.get(path, "")
        if a_text == b_text:
            continue
        a_label = f"a/{path}" if path in files_a else "/dev/null"
        b_label = f"b/{path}" if path in files_b else "/dev/null"
        chunks = difflib.unified_diff(
            a_text.splitlines(keepends=True),
            b_text.splitlines(keepends=True),
            fromfile=a_label,
            tofile=b_label,
            lineterm="",
        )
        diffs[path] = "".join(chunks)
    return diffs


def _dirty_files(tracked_paths: list[Path], project_id: int) -> list[Path]:
    """Return tracked files whose on-disk blob differs from the latest
    snapshot's stored blob. Used by :func:`rollback` to refuse to clobber
    uncommitted edits unless ``force=True``."""
    with db_conn() as conn:
        latest = _latest_version_for(conn, project_id)
        if latest is None:
            return []
        files = _files_for_version(conn, latest[0])

    dirty: list[Path] = []
    for path in tracked_paths:
        rel = _rel_path_str(path)
        stored = files.get(rel)
        if stored is None:
            # Not previously tracked — treat as new/dirty so operator
            # consciously decides to overwrite via force.
            dirty.append(path)
            continue
        if not path.is_file():
            dirty.append(path)
            continue
        on_disk_text = path.read_text(encoding="utf-8")
        on_disk_blob = _redacted_blob(path, on_disk_text)
        if on_disk_blob != stored:
            dirty.append(path)
    return dirty


def rollback(
    version_id: int,
    dry_run: bool = False,
    force: bool = False,
) -> list[Path]:
    """Restore tracked files to the contents stored at ``version_id``.

    A fresh ``snapshot(source='auto-rollback')`` of the current state is
    always taken first so the rollback itself is reversible.

    Raises ``ValueError`` if any tracked file has uncommitted edits relative
    to the most recent snapshot, unless ``force=True``.

    Args:
        version_id: target snapshot id.
        dry_run: if True, return the file list without writing.
        force: if True, ignore the dirty-file check.

    Returns:
        The list of absolute paths that were (or would be) rewritten.
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM config_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown config version id: {version_id}")
        project_id = int(row["project_id"])
        files = _files_for_version(conn, version_id)

    if not files:
        raise ValueError(
            f"Config version {version_id} has no files to restore"
        )

    target_paths: list[Path] = []
    for rel_path in sorted(files):
        absolute = _resolve_under_repo(REPO_ROOT / rel_path)
        target_paths.append(absolute)

    if not force and not dry_run:
        dirty = _dirty_files(target_paths, project_id)
        if dirty:
            dirty_list = ", ".join(_rel_path_str(p) for p in dirty)
            logger.warning(
                "[config_versions] rollback refused — dirty files: %s",
                dirty_list,
            )
            raise ValueError(
                "Refusing rollback — tracked files have uncommitted edits "
                f"relative to latest snapshot: {dirty_list}. "
                "Re-run with force=True to override."
            )

    if dry_run:
        return target_paths

    # Always snapshot the pre-rollback state so the operation is reversible.
    snapshot(
        project_id,
        source="auto-rollback",
        commit_message=f"pre-rollback snapshot before restoring v{version_id}",
    )

    for rel_path in sorted(files):
        absolute = _resolve_under_repo(REPO_ROOT / rel_path)
        _atomic_write(absolute, files[rel_path])

    return target_paths
