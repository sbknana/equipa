"""Tests for equipa.templates exporter (PLAN-1067 §3.C1).

Covers:
  - Manifest schema validator on sample data.
  - No Claude-specific fields leak into manifest.
  - Forbidden tables (api_keys, model_registry) NEVER appear.
  - Tar archive round-trip (extract + re-validate).
  - scrub_costs=True nulls agent_runs.cost_usd.
  - Auth-agnostic constraint (manifest must not declare auth mode).

The tests use a per-test isolated SQLite DB via monkeypatch — they NEVER
touch the production TheForge DB. This obeys past lesson TS-01 ("test
suites must not modify production DB").

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest


# --- Fixtures ---

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create a per-test SQLite DB pre-populated with one project worth of data.

    Returns ``(db_path, project_id)``.
    """
    db_path = tmp_path / "test_theforge.db"

    # Apply the canonical schema.
    schema_sql = (REPO_ROOT / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)

    # Insert sample project + child rows.
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, codename, summary, local_path) "
        "VALUES (?, ?, ?, ?)",
        ("TemplateTest", "TT", "Test project for template export", None),
    )
    project_id = cur.lastrowid

    cur.execute(
        "INSERT INTO tasks (project_id, title, description, status) "
        "VALUES (?, ?, ?, ?)",
        (project_id, "Sample task", "A task body", "todo"),
    )
    task_id = cur.lastrowid

    cur.execute(
        "INSERT INTO decisions (project_id, topic, decision, rationale) "
        "VALUES (?, ?, ?, ?)",
        (project_id, "DB choice", "SQLite", "Simple"),
    )
    cur.execute(
        "INSERT INTO session_notes (project_id, summary) VALUES (?, ?)",
        (project_id, "Kickoff"),
    )
    cur.execute(
        "INSERT INTO open_questions (project_id, question) VALUES (?, ?)",
        (project_id, "What is the deployment story?"),
    )
    cur.execute(
        "INSERT INTO lessons_learned (project_id, role, lesson, embedding) "
        "VALUES (?, ?, ?, ?)",
        (project_id, "developer", "Read schema before writing SQL", "[0.1, 0.2]"),
    )
    cur.execute(
        "INSERT INTO agent_runs "
        "(task_id, project_id, role, model, cost_usd, num_turns) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, project_id, "developer", "claude-opus-4-7", 1.23, 10),
    )

    # Insert a row in a DIFFERENT project — must NOT appear in export.
    cur.execute(
        "INSERT INTO projects (name) VALUES (?)",
        ("OtherProject",),
    )
    other_project_id = cur.lastrowid
    cur.execute(
        "INSERT INTO tasks (project_id, title) VALUES (?, ?)",
        (other_project_id, "Should not be exported"),
    )

    # Insert a row in api_keys — must NEVER be exported, regardless.
    cur.execute(
        "INSERT INTO api_keys (provider, label, api_key) "
        "VALUES (?, ?, ?)",
        ("anthropic", "test", "sk-FAKE-DO-NOT-EXPORT"),
    )

    conn.commit()
    conn.close()

    # Point both equipa.constants and equipa.db at the isolated DB.
    import equipa.constants as constants
    import equipa.db as db_mod

    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", db_path)

    return db_path, project_id


# --- Tests ---

def test_export_creates_expected_layout(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    out = templates.export(project_id, dest)

    assert out == dest
    assert (dest / "manifest.json").is_file()
    assert (dest / "tables").is_dir()
    assert (dest / "assets").is_dir()
    for table in templates.EXPORTED_TABLES:
        assert (dest / "tables" / f"{table}.jsonl").is_file()


def test_manifest_schema_validates(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    templates.export(project_id, dest)

    manifest = json.loads((dest / "manifest.json").read_text())
    # Required fields present.
    for field in (
        "version", "exported_at", "source_runtime",
        "id_namespace", "table_list", "row_counts", "file_sha",
    ):
        assert field in manifest
    assert manifest["source_runtime"] == "equipa-py"
    assert manifest["id_namespace"] == "source"
    # Validator passes without raising.
    templates.validate_manifest(manifest)


def test_validator_rejects_auth_mode_field():
    from equipa import templates

    bad = {
        "version": "1.0",
        "exported_at": "2026-05-03T00:00:00+00:00",
        "source_runtime": "equipa-py",
        "id_namespace": "source",
        "table_list": list(templates.EXPORTED_TABLES),
        "row_counts": {},
        "file_sha": {},
        "auth_mode": "max-subscription",
    }
    with pytest.raises(ValueError, match="auth"):
        templates.validate_manifest(bad)


def test_validator_rejects_forbidden_tables_in_manifest():
    from equipa import templates

    bad = {
        "version": "1.0",
        "exported_at": "2026-05-03T00:00:00+00:00",
        "source_runtime": "equipa-py",
        "id_namespace": "source",
        "table_list": ["projects", "api_keys"],
        "row_counts": {},
        "file_sha": {},
    }
    with pytest.raises(ValueError, match="forbidden"):
        templates.validate_manifest(bad)


def test_no_claude_specific_fields_in_manifest(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    templates.export(project_id, dest)
    manifest_text = (dest / "manifest.json").read_text().lower()

    for needle in ("claude_session_id", "claude", "opus", "sonnet", "haiku"):
        assert needle not in manifest_text, (
            f"Claude-specific token {needle!r} leaked into manifest"
        )


def test_forbidden_tables_never_in_tables_dir(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    templates.export(project_id, dest)

    table_files = {p.name for p in (dest / "tables").iterdir()}
    assert "api_keys.jsonl" not in table_files
    assert "model_registry.jsonl" not in table_files
    # And FORBIDDEN_TABLES is not in EXPORTED_TABLES (defense in depth).
    assert templates.FORBIDDEN_TABLES.isdisjoint(set(templates.EXPORTED_TABLES))


def test_export_is_project_scoped(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    templates.export(project_id, dest)

    tasks_lines = (dest / "tables" / "tasks.jsonl").read_text().splitlines()
    assert len(tasks_lines) == 1
    task = json.loads(tasks_lines[0])
    assert task["project_id"] == project_id
    assert task["title"] == "Sample task"


def test_scrub_costs_nulls_agent_runs_cost(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db

    # Without scrub: cost_usd is preserved.
    dest_a = tmp_path / "export_with_cost"
    templates.export(project_id, dest_a, scrub_costs=False)
    runs = [
        json.loads(line)
        for line in (dest_a / "tables" / "agent_runs.jsonl")
        .read_text().splitlines()
    ]
    assert runs[0]["cost_usd"] == 1.23

    # With scrub: cost_usd is null.
    dest_b = tmp_path / "export_scrubbed"
    templates.export(project_id, dest_b, scrub_costs=True)
    runs = [
        json.loads(line)
        for line in (dest_b / "tables" / "agent_runs.jsonl")
        .read_text().splitlines()
    ]
    assert runs[0]["cost_usd"] is None
    manifest = json.loads((dest_b / "manifest.json").read_text())
    assert manifest["scrub_costs"] is True


def test_archive_roundtrip(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    archive_path = templates.export(project_id, dest, archive=True)

    assert archive_path.suffix == ".gz"
    assert archive_path.is_file()
    # Staging directory is removed after packing.
    assert not dest.exists()

    # Extract and re-validate.
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    inner = extract_dir / dest.name
    assert (inner / "manifest.json").is_file()
    manifest = json.loads((inner / "manifest.json").read_text())
    templates.validate_manifest(manifest)

    # SHA-256 of each tracked file matches the manifest.
    import hashlib
    for rel, expected_sha in manifest["file_sha"].items():
        path = inner / rel
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected_sha, f"hash mismatch for {rel}"


def test_export_refuses_nonempty_dest(isolated_db, tmp_path):
    from equipa import templates

    _, project_id = isolated_db
    dest = tmp_path / "export"
    dest.mkdir()
    (dest / "stale.txt").write_text("leftover")
    with pytest.raises(FileExistsError):
        templates.export(project_id, dest)


def test_export_unknown_project_raises(isolated_db, tmp_path):
    from equipa import templates

    dest = tmp_path / "export"
    with pytest.raises(ValueError, match="not found"):
        templates.export(999_999, dest)


def test_assets_copied_when_local_path_present(isolated_db, tmp_path, monkeypatch):
    """If projects.local_path resolves to a real dir with CLAUDE.md/prompts, copy them."""
    from equipa import templates

    db_path, project_id = isolated_db

    # Create a project work dir with CLAUDE.md and prompts/.
    project_dir = tmp_path / "project_workdir"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# Project rules\n")
    prompts = project_dir / "prompts"
    prompts.mkdir()
    (prompts / "developer.md").write_text("Be terse.\n")

    # Update local_path on the project row.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE projects SET local_path = ? WHERE id = ?",
        (str(project_dir), project_id),
    )
    conn.commit()
    conn.close()

    dest = tmp_path / "export"
    templates.export(project_id, dest)

    assert (dest / "assets" / "CLAUDE.md").is_file()
    assert (dest / "assets" / "prompts" / "developer.md").is_file()

    manifest = json.loads((dest / "manifest.json").read_text())
    assert "assets/CLAUDE.md" in manifest["file_sha"]
    assert "assets/prompts/developer.md" in manifest["file_sha"]
