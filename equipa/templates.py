"""EQUIPA project template exporter.

Implements PLAN-1067 §3.C1 — adapter-agnostic project template exporter.

Exports a project's TheForge state (tasks, decisions, lessons, etc.) plus
on-disk assets (CLAUDE.md, prompts/) into a runtime-neutral on-disk layout
or a single .tar.gz archive. Importer half (C2) consumes the same layout.

The manifest is intentionally **auth-agnostic** — auth mode (Max-subscription
vs API key) is a per-host orchestrator setting in dispatch_config.json, not
a per-project property of a template archive. See docs/template-spec.md.

Excluded tables (operator decisions, see PLAN-1067 §3.C1 Open Q):
  - api_keys: NEVER included (host-local secrets)
  - model_registry: NEVER included (host-local registry state)

Optional scrubbing:
  - scrub_costs=True: agent_runs.cost_usd is nulled in exported JSONL.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from equipa.db import db_conn

logger = logging.getLogger(__name__)


# --- Spec constants ---

MANIFEST_VERSION = "1.0"
SOURCE_RUNTIME = "equipa-py"

# Tables exported in this order (FK-dependency order — projects first,
# then tables that reference projects, then tables that reference tasks).
EXPORTED_TABLES: tuple[str, ...] = (
    "projects",
    "tasks",
    "decisions",
    "session_notes",
    "open_questions",
    "lessons_learned",
    "agent_episodes",
    "agent_runs",
)

# Tables that MUST NEVER be exported in a template archive.
# Enforced both by absence from EXPORTED_TABLES and by an explicit
# assertion in the test suite. Defense in depth.
FORBIDDEN_TABLES: frozenset[str] = frozenset({"api_keys", "model_registry"})

# Per-table predicate column for project scoping.
# Most tables have project_id; agent_runs is scoped via task_id but also
# carries project_id directly so we can use it uniformly.
_PROJECT_SCOPE_COLUMN: dict[str, str] = {
    "projects": "id",
    "tasks": "project_id",
    "decisions": "project_id",
    "session_notes": "project_id",
    "open_questions": "project_id",
    "lessons_learned": "project_id",
    "agent_episodes": "project_id",
    "agent_runs": "project_id",
}


# --- Public API ---

def export(
    project_id: int,
    dest_dir: Path,
    *,
    archive: bool = False,
    scrub_costs: bool = False,
) -> Path:
    """Export a project to a template directory or tar.gz archive.

    Args:
        project_id: Source project ID in TheForge.
        dest_dir: Output directory. If it exists it must be empty.
            When ``archive=True``, the on-disk layout is written here first
            and then packed into ``dest_dir.with_suffix('.tar.gz')``.
        archive: If True, produce a single ``.tar.gz`` archive next to
            ``dest_dir`` and remove the staging directory.
        scrub_costs: If True, null the ``cost_usd`` column in exported
            ``agent_runs`` rows (operator opt-in for cost-sensitive sharing).

    Returns:
        Path to the exported directory or archive file.
    """
    dest_dir = Path(dest_dir)
    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise FileExistsError(f"dest_dir is not empty: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    tables_dir = dest_dir / "tables"
    assets_dir = dest_dir / "assets"
    tables_dir.mkdir()
    assets_dir.mkdir()

    row_counts: dict[str, int] = {}
    file_sha: dict[str, str] = {}

    project_local_path: str | None = None

    with db_conn(write=False) as conn:
        # Verify project exists; capture local_path for asset discovery.
        project_row = conn.execute(
            "SELECT id, local_path FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project_row is None:
            raise ValueError(f"project_id={project_id} not found in TheForge")
        project_local_path = project_row["local_path"]

        for table in EXPORTED_TABLES:
            if table in FORBIDDEN_TABLES:
                raise RuntimeError(
                    f"Refusing to export forbidden table: {table}"
                )
            rows = _fetch_table_rows(conn, table, project_id)
            if scrub_costs and table == "agent_runs":
                rows = [_scrub_cost(row) for row in rows]
            jsonl_path = tables_dir / f"{table}.jsonl"
            count = _write_jsonl(jsonl_path, rows)
            row_counts[table] = count
            rel = jsonl_path.relative_to(dest_dir).as_posix()
            file_sha[rel] = _sha256_file(jsonl_path)

    # Copy assets (CLAUDE.md and prompts/ overrides) if discoverable.
    if project_local_path:
        copied = _copy_assets(Path(project_local_path), assets_dir)
        for rel_asset, sha in copied.items():
            file_sha[f"assets/{rel_asset}"] = sha

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_runtime": SOURCE_RUNTIME,
        "id_namespace": "source",
        "project_id_source": project_id,
        "table_list": list(EXPORTED_TABLES),
        "row_counts": row_counts,
        "file_sha": file_sha,
        "scrub_costs": bool(scrub_costs),
    }
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    _assert_no_claude_specific_fields(manifest)

    if archive:
        archive_path = dest_dir.with_suffix(".tar.gz")
        if archive_path.exists():
            raise FileExistsError(f"archive already exists: {archive_path}")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(dest_dir, arcname=dest_dir.name)
        shutil.rmtree(dest_dir)
        return archive_path

    return dest_dir


# --- Internal helpers ---

def _fetch_table_rows(
    conn: sqlite3.Connection,
    table: str,
    project_id: int,
) -> list[dict[str, Any]]:
    """Fetch rows for ``table`` scoped to ``project_id``.

    Table names are taken from the EXPORTED_TABLES allowlist — never from
    user input — so the f-string interpolation here is safe.
    """
    if table not in _PROJECT_SCOPE_COLUMN:
        raise ValueError(f"unknown table: {table}")
    column = _PROJECT_SCOPE_COLUMN[table]
    sql = f"SELECT * FROM {table} WHERE {column} = ? ORDER BY id"
    cursor = conn.execute(sql, (project_id,))
    return [dict(row) for row in cursor.fetchall()]


def _scrub_cost(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``row`` with ``cost_usd`` nulled."""
    scrubbed = dict(row)
    if "cost_usd" in scrubbed:
        scrubbed["cost_usd"] = None
    return scrubbed


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write rows as JSONL (one JSON object per line). Returns row count."""
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=_json_default))
            fh.write("\n")
            count += 1
    return count


def _json_default(value: Any) -> Any:
    """Fallback JSON encoder for sqlite types (bytes, etc.)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"unserializable type: {type(value).__name__}")


def _sha256_file(path: Path) -> str:
    """Return hex SHA-256 of file content."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _copy_assets(project_dir: Path, assets_dir: Path) -> dict[str, str]:
    """Copy known per-project assets into ``assets_dir``.

    Currently copies:
      - ``CLAUDE.md`` (if present at project root)
      - ``prompts/`` (entire subdirectory if present)

    Returns a dict mapping ``relative_path_in_assets_dir -> sha256``.
    """
    copied: dict[str, str] = {}
    if not project_dir.exists():
        logger.debug("project_dir missing, skipping assets: %s", project_dir)
        return copied

    claude_md = project_dir / "CLAUDE.md"
    if claude_md.is_file():
        target = assets_dir / "CLAUDE.md"
        shutil.copy2(claude_md, target)
        copied["CLAUDE.md"] = _sha256_file(target)

    prompts_src = project_dir / "prompts"
    if prompts_src.is_dir():
        prompts_dst = assets_dir / "prompts"
        shutil.copytree(prompts_src, prompts_dst)
        for path in sorted(prompts_dst.rglob("*")):
            if path.is_file():
                rel = path.relative_to(assets_dir).as_posix()
                copied[rel] = _sha256_file(path)

    return copied


# --- Manifest validation (used by tests; reusable by C3 `validate` CLI) ---

_REQUIRED_MANIFEST_FIELDS: frozenset[str] = frozenset({
    "version",
    "exported_at",
    "source_runtime",
    "id_namespace",
    "table_list",
    "row_counts",
    "file_sha",
})

# Substrings that, if present in any manifest key or string value, indicate
# Claude-specific leakage. Manifests must remain runtime-agnostic.
_CLAUDE_LEAK_SUBSTRINGS: tuple[str, ...] = (
    "claude_session_id",
    "claude",
    "opus",
    "sonnet",
    "haiku",
)


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate manifest schema. Raises ValueError on any violation."""
    missing = _REQUIRED_MANIFEST_FIELDS - manifest.keys()
    if missing:
        raise ValueError(f"manifest missing required fields: {sorted(missing)}")
    if manifest["source_runtime"] != SOURCE_RUNTIME:
        # Non-equipa-py runtimes are valid in principle, but the substring
        # leak check below excludes the literal "equipa-py" value from
        # triggering false positives.
        pass
    if not isinstance(manifest["table_list"], list):
        raise ValueError("table_list must be a list")
    if not isinstance(manifest["row_counts"], dict):
        raise ValueError("row_counts must be a dict")
    if not isinstance(manifest["file_sha"], dict):
        raise ValueError("file_sha must be a dict")
    for table in manifest["table_list"]:
        if table in FORBIDDEN_TABLES:
            raise ValueError(f"forbidden table in manifest: {table}")
    # Auth-agnostic constraint: manifest MUST NOT carry an auth mode.
    for forbidden_key in ("auth_mode", "auth", "api_key_provider"):
        if forbidden_key in manifest:
            raise ValueError(
                f"manifest must not declare auth mode (found {forbidden_key!r})"
            )
    _assert_no_claude_specific_fields(manifest)


def _assert_no_claude_specific_fields(manifest: dict[str, Any]) -> None:
    """Walk manifest looking for Claude-specific substrings in keys/values."""
    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _check_string(key)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            _check_string(node)

    def _check_string(text: str) -> None:
        lowered = text.lower()
        for needle in _CLAUDE_LEAK_SUBSTRINGS:
            if needle in lowered:
                raise ValueError(
                    f"Claude-specific token {needle!r} leaked into manifest: {text!r}"
                )

    _walk(manifest)
