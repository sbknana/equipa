"""Tests for equipa.templates importer (PLAN-1067 §3.C2).

Covers:
  - Full export → import round-trip with row-count parity.
  - FK validity in target DB (every FK resolves or is NULL).
  - on_conflict='rename' creates {name}-imported-1 if base name taken.
  - on_conflict='fail' raises if target name exists.
  - Asset overwrite refused without force; succeeds with force.
  - --re-embed flag triggers embedding regeneration (mocked embed_fn).
  - Adapter-agnostic acceptance: manifests with non-equipa-py source_runtime
    are accepted as long as the schema validates.
  - Manifest hash mismatch is refused.

The tests use a per-test isolated SQLite DB via monkeypatch — they NEVER
touch the production TheForge DB.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Fixtures ---

def _apply_schema(db_path: Path) -> None:
    schema_sql = (REPO_ROOT / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


def _seed_source_project(db_path: Path) -> int:
    """Insert a source project with one row in each child table.

    Returns the source project_id.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (name, codename, summary, local_path) "
            "VALUES (?, ?, ?, ?)",
            ("ImportSource", "importsource", "round-trip source", None),
        )
        project_id = cur.lastrowid

        cur.execute(
            "INSERT INTO tasks (project_id, title, description, status) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "Task A", "first task", "todo"),
        )
        task_a_id = cur.lastrowid
        cur.execute(
            "INSERT INTO tasks (project_id, title, description, status) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "Task B", "second task", "done"),
        )
        task_b_id = cur.lastrowid

        # Decision resolved by task A — exercises FK remap.
        cur.execute(
            "INSERT INTO decisions "
            "(project_id, topic, decision, rationale, "
            " decision_type, status, resolved_by_task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id, "Architecture", "Use SQLite",
                "Simple and embedded", "architectural",
                "resolved", task_a_id,
            ),
        )
        # Decision with NULL resolved_by_task_id — must stay NULL after import.
        cur.execute(
            "INSERT INTO decisions "
            "(project_id, topic, decision, rationale) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "Naming", "snake_case", "PEP 8"),
        )

        cur.execute(
            "INSERT INTO session_notes (project_id, summary) VALUES (?, ?)",
            (project_id, "Kickoff notes"),
        )
        cur.execute(
            "INSERT INTO open_questions (project_id, question) VALUES (?, ?)",
            (project_id, "What about CI?"),
        )
        cur.execute(
            "INSERT INTO lessons_learned "
            "(project_id, role, lesson, embedding) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "developer", "Read schema first", "[0.1, 0.2]"),
        )
        cur.execute(
            "INSERT INTO lessons_learned "
            "(project_id, role, lesson, embedding) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "tester", "Cover error paths", "[0.3, 0.4]"),
        )

        cur.execute(
            "INSERT INTO agent_runs "
            "(task_id, project_id, role, model, num_turns) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_b_id, project_id, "developer", "neutral-model-v1", 7),
        )

        conn.commit()
        return project_id
    finally:
        conn.close()


@pytest.fixture
def source_db(tmp_path, monkeypatch):
    """Isolated DB seeded with a single source project + child rows."""
    db_path = tmp_path / "source.db"
    _apply_schema(db_path)
    project_id = _seed_source_project(db_path)

    import equipa.constants as constants
    import equipa.db as db_mod
    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", db_path)

    return db_path, project_id


def _swap_db(monkeypatch, new_path: Path) -> None:
    """Repoint constants/db modules at a different DB during a test."""
    import equipa.constants as constants
    import equipa.db as db_mod
    monkeypatch.setattr(constants, "THEFORGE_DB", new_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", new_path)


def _row_count(db_path: Path, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# --- Tests ---

def test_full_round_trip(source_db, tmp_path, monkeypatch):
    from equipa import templates

    src_db_path, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    # Switch to a fresh target DB and import.
    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    new_id = templates.import_archive(export_dir, target_project_name="Imported")
    assert isinstance(new_id, int)
    assert new_id > 0

    # Source counts vs target counts (target should match source 1:1).
    for table in ("tasks", "decisions", "session_notes",
                  "open_questions", "lessons_learned", "agent_runs"):
        src_count = _row_count(
            src_db_path,
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",
            (project_id,),
        )
        tgt_count = _row_count(
            target_db,
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",
            (new_id,),
        )
        assert tgt_count == src_count, (
            f"row count mismatch for {table}: src={src_count} tgt={tgt_count}"
        )


def test_fk_validity_in_target(source_db, tmp_path, monkeypatch):
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)
    new_id = templates.import_archive(export_dir)

    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    try:
        # Every tasks.project_id resolves to an existing project.
        bad = conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN projects p "
            "ON t.project_id = p.id WHERE p.id IS NULL"
        ).fetchall()
        assert bad == []

        # Every decisions.resolved_by_task_id resolves OR is NULL.
        bad = conn.execute(
            "SELECT d.id, d.resolved_by_task_id FROM decisions d "
            "LEFT JOIN tasks t ON d.resolved_by_task_id = t.id "
            "WHERE d.resolved_by_task_id IS NOT NULL AND t.id IS NULL"
        ).fetchall()
        assert bad == []

        # The decision that pointed at task A in source must now point at
        # the remapped task A id in target (not the source id).
        remapped = conn.execute(
            "SELECT resolved_by_task_id FROM decisions "
            "WHERE topic = 'Architecture'"
        ).fetchone()
        assert remapped["resolved_by_task_id"] is not None
        # The remapped task A exists in target.
        tgt_task = conn.execute(
            "SELECT id FROM tasks WHERE id = ?",
            (remapped["resolved_by_task_id"],),
        ).fetchone()
        assert tgt_task is not None

        # The NULL-resolved decision stays NULL.
        null_dec = conn.execute(
            "SELECT resolved_by_task_id FROM decisions WHERE topic = 'Naming'"
        ).fetchone()
        assert null_dec["resolved_by_task_id"] is None
    finally:
        conn.close()


def test_conflict_rename_appends_suffix(source_db, tmp_path, monkeypatch):
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    # Pre-create a project with the same name we'll import under.
    conn = sqlite3.connect(target_db)
    conn.execute("INSERT INTO projects (name) VALUES (?)", ("ImportSource",))
    conn.commit()
    conn.close()

    _swap_db(monkeypatch, target_db)
    new_id = templates.import_archive(export_dir, on_conflict="rename")

    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (new_id,)
        ).fetchone()
        assert row["name"] == "ImportSource-imported-1"
    finally:
        conn.close()


def test_conflict_fail_raises_on_collision(source_db, tmp_path, monkeypatch):
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    conn = sqlite3.connect(target_db)
    conn.execute("INSERT INTO projects (name) VALUES (?)", ("ImportSource",))
    conn.commit()
    conn.close()

    _swap_db(monkeypatch, target_db)
    with pytest.raises(RuntimeError, match="already exists"):
        templates.import_archive(export_dir, on_conflict="fail")


def test_asset_overwrite_refused_without_force(
    source_db, tmp_path, monkeypatch
):
    from equipa import templates
    import equipa.constants as constants

    src_db_path, project_id = source_db

    # Source project has a working dir with CLAUDE.md so export packs assets.
    src_workdir = tmp_path / "src_workdir"
    src_workdir.mkdir()
    (src_workdir / "CLAUDE.md").write_text("# source rules\n")
    conn = sqlite3.connect(src_db_path)
    conn.execute(
        "UPDATE projects SET local_path = ? WHERE id = ?",
        (str(src_workdir), project_id),
    )
    conn.commit()
    conn.close()

    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    # Target DB and a target project working dir with a pre-existing file.
    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    target_workdir = tmp_path / "tgt_workdir"
    target_workdir.mkdir()
    (target_workdir / "CLAUDE.md").write_text("# pre-existing\n")

    # Register the target project codename → workdir mapping.
    monkeypatch.setattr(
        constants,
        "PROJECT_DIRS",
        {"importsource": str(target_workdir)},
    )

    with pytest.raises(FileExistsError):
        templates.import_archive(export_dir, force=False)

    # On import failure the DB transaction must have rolled back — no
    # imported project row should be present in the target DB.
    assert _row_count(target_db, "SELECT COUNT(*) FROM projects") == 0

    # And the pre-existing file must still hold its original content.
    assert (target_workdir / "CLAUDE.md").read_text() == "# pre-existing\n"

    # With force=True the import succeeds and the asset is overwritten.
    new_id = templates.import_archive(export_dir, force=True)
    assert isinstance(new_id, int)
    assert (target_workdir / "CLAUDE.md").read_text() == "# source rules\n"


def test_re_embed_invokes_embed_fn_per_lesson(
    source_db, tmp_path, monkeypatch
):
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    calls: list[str] = []

    def fake_embed(text: str) -> list[float]:
        calls.append(text)
        return [0.99, 0.98, 0.97]

    new_id = templates.import_archive(
        export_dir, re_embed=True, embed_fn=fake_embed
    )

    # Two lessons in the source — embed_fn invoked once per lesson.
    assert len(calls) == 2
    assert "Read schema first" in calls
    assert "Cover error paths" in calls

    # And the stored embeddings are the new ones, not the imported placeholders.
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT embedding FROM lessons_learned WHERE project_id = ?",
            (new_id,),
        ).fetchall()
        for row in rows:
            assert json.loads(row["embedding"]) == [0.99, 0.98, 0.97]
    finally:
        conn.close()


def test_adapter_agnostic_accepts_foreign_runtime(
    source_db, tmp_path, monkeypatch
):
    """A manifest with source_runtime != 'equipa-py' must still import.

    This is the regression test that proves adapter-agnosticism is real.
    """
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    # Mutate manifest to a non-equipa source_runtime, then re-stamp file_sha
    # (manifest itself is not in file_sha so we leave that map alone).
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_runtime"] = "other-runtime-py"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    # validate_manifest must still pass on the mutated copy.
    templates.validate_manifest(manifest)

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    new_id = templates.import_archive(export_dir)
    assert isinstance(new_id, int)


def test_hash_mismatch_refused(source_db, tmp_path, monkeypatch):
    from equipa import templates

    _, project_id = source_db
    export_dir = tmp_path / "exported"
    templates.export(project_id, export_dir)

    # Tamper with a tracked file so its sha no longer matches the manifest.
    tasks_path = export_dir / "tables" / "tasks.jsonl"
    with tasks_path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": 9999, "title": "smuggled"}\n')

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    with pytest.raises(ValueError, match="file_sha mismatch"):
        templates.import_archive(export_dir)


def test_archive_round_trip(source_db, tmp_path, monkeypatch):
    """Importing a .tar.gz archive (not a directory) works end-to-end."""
    from equipa import templates

    _, project_id = source_db
    staging = tmp_path / "exported"
    archive_path = templates.export(project_id, staging, archive=True)
    assert archive_path.suffix == ".gz"
    assert not staging.exists()  # exporter packed and removed staging

    target_db = tmp_path / "target.db"
    _apply_schema(target_db)
    _swap_db(monkeypatch, target_db)

    new_id = templates.import_archive(archive_path, target_project_name="FromArchive")
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (new_id,)
        ).fetchone()
        assert row["name"] == "FromArchive"
    finally:
        conn.close()
