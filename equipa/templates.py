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
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from equipa.db import db_conn, get_db_connection

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
    """Walk manifest looking for Claude-specific substrings in keys/values.

    The ``file_sha`` map's KEYS are on-disk file paths (e.g.
    ``assets/CLAUDE.md`` is a long-standing project-doc convention, not a
    Claude-runtime leak) — its keys are exempt from the substring check.
    Its values (SHA hex digests) are still checked.
    """
    def _walk(node: Any, *, in_file_sha: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not in_file_sha:
                    _check_string(key)
                next_in_file_sha = (key == "file_sha") or in_file_sha
                _walk(value, in_file_sha=next_in_file_sha)
        elif isinstance(node, list):
            for item in node:
                _walk(item, in_file_sha=in_file_sha)
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


# --- Importer (PLAN-1067 §3.C2) ---

# Per-table foreign key columns to remap during import. Source IDs are looked
# up in id_remap[(target_table, source_id)] and rewritten to the new ID
# assigned by the target DB before insertion. Tables with NO FK fields appear
# as an empty dict.
#
# Note: open_questions has only `resolved` (boolean) and `resolution` (text)
# columns in schema.sql — there is NO `resolved_by_task_id` FK column on
# open_questions despite the task description listing one. We remap only what
# actually exists in the schema.
_FK_REMAP: dict[str, dict[str, str]] = {
    "tasks": {"project_id": "projects"},
    "decisions": {
        "project_id": "projects",
        "resolved_by_task_id": "tasks",
    },
    "session_notes": {"project_id": "projects"},
    "open_questions": {"project_id": "projects"},
    "lessons_learned": {"project_id": "projects"},
    "agent_episodes": {
        "project_id": "projects",
        "task_id": "tasks",
    },
    "agent_runs": {
        "project_id": "projects",
        "task_id": "tasks",
    },
}

# Order in which tables are imported. Must satisfy FK dependencies: a table
# can only reference tables earlier in this list.
_IMPORT_ORDER: tuple[str, ...] = (
    "projects",
    "tasks",
    "decisions",
    "session_notes",
    "open_questions",
    "lessons_learned",
    "agent_episodes",
    "agent_runs",
)

# Conflict policies accepted by import_archive.
_VALID_ON_CONFLICT: frozenset[str] = frozenset({"rename", "merge", "fail"})


def import_archive(
    source: Path,
    target_project_name: str | None = None,
    *,
    on_conflict: str = "rename",
    force: bool = False,
    re_embed: bool = False,
    embed_fn: Callable[[str], list[float] | None] | None = None,
) -> int:
    """Import a project template archive into the local TheForge DB.

    Args:
        source: Path to either an exported template directory or a
            ``.tar.gz`` archive produced by :func:`export`.
        target_project_name: Name to use for the imported ``projects`` row.
            If ``None``, the original name from the archive is used.
        on_conflict: Strategy when target name already exists.
            ``'rename'`` appends ``-imported-N``; ``'merge'`` requires that
            ``target_project_name`` resolves to an existing project and
            appends child rows under it; ``'fail'`` aborts.
        force: Required to overwrite asset files in the target project's
            working directory when the destination already contains files.
        re_embed: If True, regenerate embeddings for imported
            ``lessons_learned`` rows against this host's embedding model.
            Default False (lessons imported as-is — see PLAN-1067 §3.C2).
        embed_fn: Override the embedding function (used for tests). If
            ``None`` and ``re_embed=True``, ``equipa.embeddings.get_embedding``
            is used.

    Returns:
        New ``project_id`` in the local TheForge DB.

    Raises:
        ValueError: manifest invalid or hash mismatch.
        FileExistsError: asset overwrite refused without ``force=True``.
        RuntimeError: ``on_conflict='fail'`` and target name already exists.
    """
    if on_conflict not in _VALID_ON_CONFLICT:
        raise ValueError(
            f"on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}, "
            f"got {on_conflict!r}"
        )

    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(f"source not found: {source}")

    # Stage: extract archive if needed, locate template_dir holding manifest.
    cleanup_dir: Path | None = None
    try:
        if source.is_file() and source.name.endswith(".tar.gz"):
            cleanup_dir = Path(tempfile.mkdtemp(prefix="equipa-import-"))
            _safe_extract_tar(source, cleanup_dir)
            template_dir = _locate_manifest_dir(cleanup_dir)
        elif source.is_dir():
            template_dir = source
        else:
            raise ValueError(
                f"source must be a directory or .tar.gz archive: {source}"
            )

        manifest = _load_and_validate_manifest(template_dir)
        _verify_file_hashes(template_dir, manifest)

        return _do_import(
            template_dir=template_dir,
            manifest=manifest,
            target_project_name=target_project_name,
            on_conflict=on_conflict,
            force=force,
            re_embed=re_embed,
            embed_fn=embed_fn,
        )
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


# --- Importer internals ---

def _safe_extract_tar(archive: Path, dest: Path) -> None:
    """Extract a tar archive, refusing entries that escape ``dest``.

    Mitigates CVE-2007-4559-style traversal where a member like
    ``../../etc/passwd`` writes outside the destination.
    """
    dest_resolved = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest_resolved / member.name).resolve()
            if not _is_within(target, dest_resolved):
                raise ValueError(
                    f"refusing to extract path outside destination: "
                    f"{member.name}"
                )
            if member.islnk() or member.issym():
                # Reject any link target that escapes dest.
                link_target = (target.parent / member.linkname).resolve()
                if not _is_within(link_target, dest_resolved):
                    raise ValueError(
                        f"refusing link target outside destination: "
                        f"{member.linkname}"
                    )
        tar.extractall(dest)


def _is_within(path: Path, parent: Path) -> bool:
    """Return True if ``path`` is the same as or inside ``parent``."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _locate_manifest_dir(root: Path) -> Path:
    """Return the directory containing manifest.json under ``root``.

    Templates are typically packed with a single top-level directory; the
    manifest may be at ``root/manifest.json`` or one level down.
    """
    if (root / "manifest.json").is_file():
        return root
    children = [p for p in root.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.json").is_file():
        return children[0]
    raise ValueError(f"manifest.json not found in {root}")


def _load_and_validate_manifest(template_dir: Path) -> dict[str, Any]:
    manifest_path = template_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"manifest.json missing in {template_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc
    validate_manifest(manifest)
    return manifest


def _verify_file_hashes(template_dir: Path, manifest: dict[str, Any]) -> None:
    """Ensure every file in manifest.file_sha matches its on-disk SHA-256."""
    for rel, expected_sha in manifest.get("file_sha", {}).items():
        # Defense in depth: refuse path traversal in manifest entries.
        candidate = (template_dir / rel).resolve()
        if not _is_within(candidate, template_dir.resolve()):
            raise ValueError(
                f"manifest file_sha entry escapes template dir: {rel!r}"
            )
        if not candidate.is_file():
            raise ValueError(f"manifest references missing file: {rel}")
        actual = _sha256_file(candidate)
        if actual != expected_sha:
            raise ValueError(
                f"file_sha mismatch for {rel}: "
                f"expected {expected_sha}, got {actual}"
            )


def _do_import(
    *,
    template_dir: Path,
    manifest: dict[str, Any],
    target_project_name: str | None,
    on_conflict: str,
    force: bool,
    re_embed: bool,
    embed_fn: Callable[[str], list[float] | None] | None,
) -> int:
    tables_dir = template_dir / "tables"
    if not tables_dir.is_dir():
        raise ValueError(f"tables/ subdir missing in {template_dir}")

    # Read source projects.jsonl to get the original project row(s).
    source_project_rows = list(_read_jsonl(tables_dir / "projects.jsonl"))
    if not source_project_rows:
        raise ValueError("projects.jsonl is empty — nothing to import")
    # In single-project export the file holds exactly one row.
    source_project = source_project_rows[0]
    source_project_id = source_project["id"]

    # Use a single write connection for the entire import so we can roll back
    # cleanly on any error. Manual BEGIN/COMMIT — bypass db_conn's auto-commit
    # to keep the whole import transactional.
    conn = get_db_connection(write=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")

        new_project_id, new_project_name = _resolve_project_target(
            conn=conn,
            source_project=source_project,
            target_project_name=target_project_name,
            on_conflict=on_conflict,
        )

        id_remap: dict[tuple[str, int], int] = {
            ("projects", source_project_id): new_project_id,
        }

        for table in _IMPORT_ORDER:
            if table == "projects":
                continue  # already inserted
            jsonl_path = tables_dir / f"{table}.jsonl"
            if not jsonl_path.is_file():
                # Adapter-agnostic: optional tables may be absent.
                continue
            for row in _read_jsonl(jsonl_path):
                _insert_row_with_remap(
                    conn=conn,
                    table=table,
                    row=row,
                    id_remap=id_remap,
                    target_project_id=new_project_id,
                )

        # Re-embed lessons if requested. Done inside the transaction so a
        # failure rolls back the whole import.
        if re_embed:
            _re_embed_lessons(
                conn=conn,
                project_id=new_project_id,
                embed_fn=embed_fn,
            )

        # Copy assets into the target project's working dir BEFORE commit so
        # an asset-copy failure (e.g. force=False) rolls back the DB import.
        _copy_assets_into_project(
            template_dir=template_dir,
            project_name=new_project_name,
            force=force,
        )

        conn.commit()
        return new_project_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_project_target(
    *,
    conn: sqlite3.Connection,
    source_project: dict[str, Any],
    target_project_name: str | None,
    on_conflict: str,
) -> tuple[int, str]:
    """Resolve the target project row for the import.

    For 'rename' and 'fail' a new project row is inserted. For 'merge' the
    existing project row is reused (no insert) and its id is returned.

    Returns ``(project_id, project_name)``.
    """
    desired_name = target_project_name or source_project.get("name")
    if not desired_name:
        raise ValueError("source project has no name and no override given")

    existing_id = _project_id_by_name(conn, desired_name)

    if on_conflict == "merge":
        if existing_id is None:
            raise ValueError(
                f"on_conflict='merge' but no existing project named "
                f"{desired_name!r}"
            )
        return existing_id, desired_name

    if existing_id is not None:
        if on_conflict == "fail":
            raise RuntimeError(
                f"target project name already exists: {desired_name!r}"
            )
        # 'rename' — pick the smallest -imported-N suffix that is unique.
        desired_name = _next_available_name(conn, desired_name)

    new_id = _insert_project(conn, source_project, desired_name)
    return new_id, desired_name


def _project_id_by_name(
    conn: sqlite3.Connection, name: str
) -> int | None:
    row = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (name,)
    ).fetchone()
    return row["id"] if row else None


def _next_available_name(conn: sqlite3.Connection, base: str) -> str:
    suffix_n = 1
    while True:
        candidate = f"{base}-imported-{suffix_n}"
        if _project_id_by_name(conn, candidate) is None:
            return candidate
        suffix_n += 1


def _insert_project(
    conn: sqlite3.Connection,
    source_project: dict[str, Any],
    name: str,
) -> int:
    """Insert a new projects row, copying source columns where possible.

    The ``id`` column from the source is dropped — the target DB assigns a
    new autoincrement id which becomes the head of id_remap.
    """
    target_columns = _target_table_columns(conn, "projects")
    row = {k: v for k, v in source_project.items() if k in target_columns}
    row["name"] = name  # always overwrite with resolved name
    row.pop("id", None)
    cur = conn.execute(*_build_insert("projects", row))
    return cur.lastrowid


def _insert_row_with_remap(
    *,
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    id_remap: dict[tuple[str, int], int],
    target_project_id: int,
) -> None:
    """Insert a row, rewriting FK columns via ``id_remap``."""
    target_columns = _target_table_columns(conn, table)
    fk_columns = _FK_REMAP.get(table, {})

    new_row: dict[str, Any] = {}
    source_id = row.get("id")
    for col, val in row.items():
        if col == "id":
            continue  # let target DB autoincrement
        if col not in target_columns:
            # Adapter-agnostic: source may carry extra columns we don't
            # have. Drop them silently.
            continue
        if col in fk_columns and val is not None:
            ref_table = fk_columns[col]
            remapped = id_remap.get((ref_table, val))
            if remapped is None:
                # FK target row was never imported (e.g. cross-project FK,
                # or a decisions.resolved_by_task_id pointing to a task
                # that was filtered out). Drop the dangling reference
                # rather than violating the FK.
                new_row[col] = None
            else:
                new_row[col] = remapped
        else:
            new_row[col] = val

    # Defense in depth: every project_id column MUST be the target project's
    # id. If the source row referenced a different project we either dropped
    # it above or remapped through id_remap.
    if "project_id" in target_columns and "project_id" in new_row:
        if new_row["project_id"] is None:
            new_row["project_id"] = target_project_id

    cur = conn.execute(*_build_insert(table, new_row))
    if source_id is not None:
        id_remap[(table, source_id)] = cur.lastrowid


# Cache of column lists per (connection-id, table). Avoids re-running PRAGMA
# table_info on every row insert.
_table_columns_cache: dict[tuple[int, str], frozenset[str]] = {}


def _target_table_columns(
    conn: sqlite3.Connection, table: str
) -> frozenset[str]:
    """Return the set of column names defined in the target DB for ``table``.

    Uses ``PRAGMA table_info`` — table name is taken from our internal
    allowlist (``_IMPORT_ORDER``) so f-string interpolation here is safe
    against SQL injection.
    """
    if table not in _IMPORT_ORDER:
        raise ValueError(f"unknown table: {table}")
    key = (id(conn), table)
    cached = _table_columns_cache.get(key)
    if cached is not None:
        return cached
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = frozenset(r["name"] for r in rows)
    _table_columns_cache[key] = cols
    return cols


def _build_insert(
    table: str, row: dict[str, Any]
) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterized INSERT statement for ``row``.

    Table name is allowlisted via ``_IMPORT_ORDER``; column names come from
    the target DB's ``PRAGMA table_info`` (also trusted). Values are bound
    as parameters.
    """
    if table not in _IMPORT_ORDER:
        raise ValueError(f"unknown table: {table}")
    if not row:
        raise ValueError(f"refusing to insert empty row into {table}")
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    return sql, tuple(row[c] for c in columns)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON objects from a JSONL file."""
    with path.open("r", encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name}:{line_num} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise ValueError(
                    f"{path.name}:{line_num} is not a JSON object"
                )
            yield obj


def _re_embed_lessons(
    *,
    conn: sqlite3.Connection,
    project_id: int,
    embed_fn: Callable[[str], list[float] | None] | None,
) -> None:
    """Regenerate embeddings for newly-imported lessons of ``project_id``.

    Calls ``embed_fn(lesson_text)`` (default: ``embeddings.get_embedding``)
    once per lesson and stores the result in ``lessons_learned.embedding``
    as a JSON-encoded list. Lessons whose embed call returns ``None`` keep
    their imported embedding.
    """
    if embed_fn is None:
        # Local import to avoid pulling Ollama deps unless re-embed is used.
        from equipa.embeddings import get_embedding

        embed_fn = get_embedding

    rows = conn.execute(
        "SELECT id, lesson FROM lessons_learned WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    for row in rows:
        text = row["lesson"]
        if not text:
            continue
        new_embedding = embed_fn(text)
        if new_embedding is None:
            logger.warning(
                "re-embed: embed_fn returned None for lesson id=%s", row["id"]
            )
            continue
        conn.execute(
            "UPDATE lessons_learned SET embedding = ? WHERE id = ?",
            (json.dumps(new_embedding), row["id"]),
        )


def _copy_assets_into_project(
    *,
    template_dir: Path,
    project_name: str,
    force: bool,
) -> None:
    """Copy assets/* into the target project's working directory.

    Resolution: project working dir comes from
    ``equipa.constants.PROJECT_DIRS`` keyed by codename
    (lowercase no-space). If no entry is registered we silently skip — the
    operator can copy assets manually after wiring up forge_config.json.

    Refuses to overwrite a non-empty destination unless ``force=True``.
    Uses an atomic-rename pattern: write each file to a sibling tempfile
    then ``os.replace`` into place so a crash mid-copy never leaves a
    half-written file at the target path.
    """
    assets_dir = template_dir / "assets"
    if not assets_dir.is_dir():
        return  # nothing to copy
    asset_files = [p for p in assets_dir.rglob("*") if p.is_file()]
    if not asset_files:
        return

    # Local import to avoid a hard dep cycle at module-import time.
    from equipa import constants as _equipa_constants

    codename = project_name.lower().replace(" ", "")
    project_dir_str = _equipa_constants.PROJECT_DIRS.get(codename)
    if not project_dir_str:
        logger.info(
            "no PROJECT_DIRS entry for %r — skipping asset copy", codename
        )
        return

    project_dir = Path(project_dir_str)
    project_dir_resolved = project_dir.resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    # Refuse to overwrite a non-empty destination unless forced.
    if not force and any(project_dir.iterdir()):
        # Only refuse if any of the asset target paths would clobber an
        # existing file. An empty dir, or a dir whose existing contents do
        # not collide with assets, is fine.
        for src in asset_files:
            rel = src.relative_to(assets_dir)
            tgt = (project_dir / rel).resolve()
            if not _is_within(tgt, project_dir_resolved):
                raise ValueError(
                    f"asset path escapes project dir: {rel}"
                )
            if tgt.exists():
                raise FileExistsError(
                    f"asset destination has files; pass force=True to "
                    f"overwrite: {project_dir}"
                )

    for src in asset_files:
        rel = src.relative_to(assets_dir)
        tgt = (project_dir / rel).resolve()
        if not _is_within(tgt, project_dir_resolved):
            raise ValueError(f"asset path escapes project dir: {rel}")
        tgt.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: copy to tmpfile beside target, then os.replace.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{tgt.name}.", suffix=".tmp", dir=str(tgt.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            shutil.copy2(src, tmp_path)
            os.replace(tmp_path, tgt)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
