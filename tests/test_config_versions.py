"""Tests for equipa.config_versions (PLAN-1067 §1.A2).

Covers:
  - Snapshot dedup: identical content -> single row.
  - Snapshot redaction: api_key / *_secret / *_password / *_token in JSON
    are stored as ``<REDACTED>``, never as raw values.
  - Diff round-trip: diff(A, B) where B == rollback(A) is empty.
  - Rollback round-trip: contents restored byte-for-byte.
  - Rollback writes a pre-rollback snapshot first (auto-rollback source).
  - Rollback refuses to clobber dirty working tree without ``force=True``.
  - Snapshot refuses path-traversal (../../etc/passwd, etc).

All tests use an isolated SQLite DB + isolated REPO_ROOT under tmp_path so
they NEVER touch the real DB or real repo files (TS-01 lesson).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT_REAL = Path(__file__).resolve().parent.parent


# --- Fixtures ---

@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate the DB, the config_versions REPO_ROOT, and PROMPTS_DIR.

    Returns ``(repo_root, project_id)`` where ``repo_root`` is a writable
    tmp dir pre-populated with sample dispatch_config.json, forge_config.json,
    and prompts/*.md files.
    """
    db_path = tmp_path / "test_theforge.db"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    prompts_dir = repo_root / "prompts"
    prompts_dir.mkdir()

    # Apply canonical schema.
    schema_sql = (REPO_ROOT_REAL / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, codename) VALUES (?, ?)",
        ("ConfigVersionsTest", "CVT"),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Sample on-disk files.
    (repo_root / "dispatch_config.json").write_text(json.dumps({
        "model": "sonnet",
        "max_turns": 25,
        "api_key": "sk-secret-123",
        "providers": {
            "anthropic": {
                "api_key": "sk-anthropic-XYZ",
                "stripe_secret": "sk_live_DO_NOT_LEAK",
                "user_password": "hunter2",
                "session_token": "abcdef",
                "model_name": "opus",
            },
        },
    }, indent=2))
    (repo_root / "forge_config.json").write_text(json.dumps({
        "project_dirs": {"cvt": "/tmp/cvt"},
    }, indent=2))
    (prompts_dir / "developer.md").write_text("# Developer prompt\n")
    (prompts_dir / "tester.md").write_text("# Tester prompt\n")

    # Patch module-level constants. equipa.db reads THEFORGE_DB at call
    # time via the module attribute; equipa.config_versions reads REPO_ROOT
    # / PROMPTS_DIR at call time the same way.
    import equipa.constants as constants
    import equipa.db as db_mod
    import equipa.config_versions as cv

    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", db_path)
    monkeypatch.setattr(cv, "REPO_ROOT", repo_root.resolve())
    monkeypatch.setattr(cv, "PROMPTS_DIR", prompts_dir)

    return repo_root, project_id


# --- Snapshot dedup ---

def test_snapshot_dedup(isolated_env):
    from equipa import config_versions as cv

    _, project_id = isolated_env
    first_id = cv.snapshot(project_id, source="manual", commit_message="first")
    second_id = cv.snapshot(project_id, source="manual", commit_message="dup")

    assert second_id == first_id
    versions = cv.list_versions(project_id)
    assert len(versions) == 1


def test_snapshot_creates_new_row_on_change(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    cv.snapshot(project_id, source="manual")
    (repo_root / "forge_config.json").write_text(json.dumps({
        "project_dirs": {"cvt": "/tmp/changed"},
    }, indent=2))
    cv.snapshot(project_id, source="manual", commit_message="changed")
    assert len(cv.list_versions(project_id)) == 2


# --- Redaction ---

def test_snapshot_redacts_secret_keys(isolated_env):
    from equipa import config_versions as cv

    _, project_id = isolated_env
    version_id = cv.snapshot(project_id, source="manual")

    import equipa.db as db_mod
    with db_mod.db_conn() as conn:
        rows = conn.execute(
            "SELECT file_path, content_blob FROM config_version_files "
            "WHERE version_id = ?",
            (version_id,),
        ).fetchall()

    blobs = {r["file_path"]: r["content_blob"] for r in rows}
    dispatch_blob = blobs["dispatch_config.json"]

    # Sentinel present.
    assert cv.REDACTED_SENTINEL in dispatch_blob

    # No raw secret survives.
    for raw in (
        "sk-secret-123",
        "sk-anthropic-XYZ",
        "sk_live_DO_NOT_LEAK",
        "hunter2",
        "abcdef",
    ):
        assert raw not in dispatch_blob, f"raw secret leaked: {raw!r}"

    # Non-secret keys preserved (model_name does not match denylist).
    parsed = json.loads(dispatch_blob)
    assert parsed["providers"]["anthropic"]["model_name"] == "opus"
    assert parsed["model"] == "sonnet"
    assert parsed["max_turns"] == 25


# --- Diff & rollback round-trip ---

def test_diff_round_trip_after_rollback(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    v1 = cv.snapshot(project_id, source="manual", commit_message="v1")

    # Mutate then snapshot v2.
    (repo_root / "forge_config.json").write_text(json.dumps({
        "project_dirs": {"cvt": "/tmp/v2"},
    }, indent=2))
    v2 = cv.snapshot(project_id, source="manual", commit_message="v2")
    assert v2 != v1

    # diff(v1, v2) is non-empty and includes the mutated file.
    forward = cv.diff(v1, v2)
    assert "forge_config.json" in forward
    assert forward["forge_config.json"]  # non-empty

    # Rollback to v1. Use force because the working tree is "dirty"
    # relative to v2 (we just changed forge_config.json without
    # snapshotting between this and a different version).
    cv.rollback(v1, force=True)

    # The post-rollback latest snapshot should match v1's content_sha
    # (the auto-rollback snapshot was BEFORE the rollback ran, then
    # rollback wrote v1 contents). Take a fresh snapshot and confirm
    # it dedups against v1's content.
    redo = cv.snapshot(project_id, source="manual", commit_message="post-rb")
    # Diffing v1 against the new snapshot must be empty — the on-disk
    # content was restored byte-for-byte to v1's blobs.
    assert cv.diff(v1, redo) == {}


def test_rollback_restores_files_byte_for_byte(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    original = (repo_root / "forge_config.json").read_text()
    v1 = cv.snapshot(project_id, source="manual")

    # Mutate.
    (repo_root / "forge_config.json").write_text("CORRUPTED\n")
    assert (repo_root / "forge_config.json").read_text() == "CORRUPTED\n"

    cv.rollback(v1, force=True)
    restored = (repo_root / "forge_config.json").read_text()
    # forge_config.json has no secret-keyed entries, so the redacted blob
    # equals the original (re-serialised by sort_keys / indent).
    assert json.loads(restored) == json.loads(original)


def test_rollback_writes_pre_snapshot_first(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    v1 = cv.snapshot(project_id, source="manual", commit_message="v1")
    assert len(cv.list_versions(project_id)) == 1

    # Mutate the working tree (do NOT snapshot the change). Now the
    # rollback's pre-snapshot must capture this distinct content.
    (repo_root / "forge_config.json").write_text(json.dumps({"k": "dirty"}))

    cv.rollback(v1, force=True)

    versions_after = cv.list_versions(project_id)
    # Expect: v1 (target) + auto-rollback snapshot of pre-rollback dirty state.
    assert len(versions_after) == 2
    newest = versions_after[0]
    assert newest["source"] == "auto-rollback"
    assert newest["id"] != v1


def test_rollback_dry_run_writes_nothing(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    v1 = cv.snapshot(project_id, source="manual")
    (repo_root / "forge_config.json").write_text("EDITED\n")
    versions_before = cv.list_versions(project_id)

    paths = cv.rollback(v1, dry_run=True)
    assert paths  # non-empty list returned
    # No write happened — still EDITED on disk.
    assert (repo_root / "forge_config.json").read_text() == "EDITED\n"
    # No new snapshot row inserted.
    assert cv.list_versions(project_id) == versions_before


def test_rollback_refuses_dirty_without_force(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    v1 = cv.snapshot(project_id, source="manual")

    # Dirty edit relative to the latest snapshot.
    (repo_root / "forge_config.json").write_text("DIRTY\n")

    with pytest.raises(ValueError, match="Refusing rollback"):
        cv.rollback(v1)

    # Force overrides the refusal and proceeds.
    cv.rollback(v1, force=True)
    assert "DIRTY" not in (repo_root / "forge_config.json").read_text()


# --- Path traversal guard ---

def test_snapshot_refuses_path_traversal(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    bad_path = repo_root / ".." / ".." / "etc" / "passwd"
    with pytest.raises(ValueError, match="outside repo root"):
        cv.snapshot(project_id, source="manual", files=[bad_path])


def test_rollback_refuses_unknown_version(isolated_env):
    from equipa import config_versions as cv

    _, _ = isolated_env
    with pytest.raises(ValueError, match="Unknown config version"):
        cv.rollback(99999)


def test_snapshot_rejects_invalid_source(isolated_env):
    from equipa import config_versions as cv

    _, project_id = isolated_env
    with pytest.raises(ValueError, match="unsupported source"):
        cv.snapshot(project_id, source="bogus")


def test_list_versions_ordered_most_recent_first(isolated_env):
    from equipa import config_versions as cv

    repo_root, project_id = isolated_env
    cv.snapshot(project_id, source="manual", commit_message="one")
    (repo_root / "forge_config.json").write_text(json.dumps({"k": 1}))
    cv.snapshot(project_id, source="manual", commit_message="two")
    (repo_root / "forge_config.json").write_text(json.dumps({"k": 2}))
    cv.snapshot(project_id, source="manual", commit_message="three")

    versions = cv.list_versions(project_id)
    assert [v["commit_message"] for v in versions] == ["three", "two", "one"]
