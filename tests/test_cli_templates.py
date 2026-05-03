"""Tests for the ``equipa template`` CLI surface (PLAN-1067 §3.C3).

Covers:
  - ``equipa template export <project_id>`` exit 0, writes manifest.
  - ``equipa template import <archive>`` exit 0, creates project.
  - ``equipa template validate <archive>`` exit 0 on valid fixture.
  - ``equipa template validate`` rejects malformed manifests (exit 1).
  - The non-Claude adapter fixture validates and imports cleanly.
  - Feature flag off → guard message + non-zero exit (no DB writes).

The CLI is exercised via ``subprocess`` so we cover the real entry point.
Each test gets an isolated temp TheForge DB injected via the
``THEFORGE_DB`` env var honoured by ``forge_config.json`` resolution.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "template-non-claude"


# --- Helpers ---

def _apply_schema(db_path: Path) -> None:
    schema_sql = (REPO_ROOT / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


def _write_dispatch_config(path: Path, *, templates_enabled: bool) -> None:
    cfg = {"features": {"project_templates": templates_enabled}}
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _seed_minimal_project(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (name, codename, summary) VALUES (?, ?, ?)",
            ("CLITestSource", "clitestsource", "exporter CLI test source"),
        )
        project_id = cur.lastrowid
        cur.execute(
            "INSERT INTO tasks (project_id, title, status) VALUES (?, ?, ?)",
            (project_id, "Sample task", "todo"),
        )
        conn.commit()
        return project_id
    finally:
        conn.close()


def _run_cli(
    args: list[str],
    *,
    db_path: Path,
    cfg_path: Path,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Invoke ``python -m equipa <args>`` with isolated DB + config."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # THEFORGE_DB env var is honoured by equipa.constants at module-load
    # time — exporting it before launching the subprocess gives the child
    # a fully isolated DB without touching the production forge_config.json.
    env["THEFORGE_DB"] = str(db_path)

    full_args = [
        sys.executable, "-m", "equipa",
        "template",
    ] + ["--dispatch-config", str(cfg_path)] + args
    return subprocess.run(
        full_args,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- Fixtures ---

@pytest.fixture
def isolated_env(tmp_path):
    """Provide a fresh DB + an enabled-flag dispatch config."""
    db_path = tmp_path / "theforge.db"
    _apply_schema(db_path)
    cfg_path = tmp_path / "dispatch_config.json"
    _write_dispatch_config(cfg_path, templates_enabled=True)
    return db_path, cfg_path, tmp_path


# --- Module entry point sanity ---

def test_equipa_module_runs_template_subcommand(isolated_env):
    """`python -m equipa template --help` must succeed even before flag check."""
    db_path, cfg_path, _ = isolated_env
    result = _run_cli(["--help"], db_path=db_path, cfg_path=cfg_path)
    # argparse --help exits 0; if the entry point is mis-wired we'd get 2.
    assert result.returncode == 0, result.stderr
    assert "export" in result.stdout
    assert "import" in result.stdout
    assert "validate" in result.stdout


# --- export ---

def test_export_exit_zero_and_writes_manifest(isolated_env, tmp_path):
    db_path, cfg_path, _ = isolated_env
    project_id = _seed_minimal_project(db_path)
    out_dir = tmp_path / "out"

    result = _run_cli(
        ["export", str(project_id), "--out", str(out_dir)],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode == 0, result.stderr
    assert (out_dir / "manifest.json").is_file(), result.stdout
    assert (out_dir / "tables" / "projects.jsonl").is_file()


def test_export_archive_flag_produces_tarball(isolated_env, tmp_path):
    db_path, cfg_path, _ = isolated_env
    project_id = _seed_minimal_project(db_path)
    out_dir = tmp_path / "archout"

    result = _run_cli(
        ["export", str(project_id), "--out", str(out_dir), "--archive"],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode == 0, result.stderr
    archive_path = out_dir.with_suffix(".tar.gz")
    assert archive_path.is_file(), f"archive missing: {result.stdout}"


# --- import ---

def test_import_accepts_non_claude_fixture(isolated_env):
    """The hand-crafted non-Claude fixture imports without complaint."""
    db_path, cfg_path, _ = isolated_env

    result = _run_cli(
        [
            "import", str(FIXTURE_DIR),
            "--name", "from-other-agent",
        ],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode == 0, result.stderr
    # Verify the project landed in the target DB.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, name FROM projects WHERE name = ?",
            ("from-other-agent",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "imported project not present in DB"


# --- validate ---

def test_validate_accepts_non_claude_fixture(isolated_env):
    db_path, cfg_path, _ = isolated_env
    result = _run_cli(
        ["validate", str(FIXTURE_DIR)],
        db_path=db_path,
        cfg_path=cfg_path,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_validate_rejects_malformed_manifest(isolated_env, tmp_path):
    """A manifest missing required fields exits non-zero with INVALID."""
    db_path, cfg_path, _ = isolated_env
    bad_dir = tmp_path / "bad-template"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text(
        # Missing version, table_list, row_counts, file_sha, etc.
        json.dumps({"source_runtime": "x"}),
        encoding="utf-8",
    )

    result = _run_cli(
        ["validate", str(bad_dir)],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode == 1, result.stdout
    assert "INVALID" in result.stderr or "INVALID" in result.stdout


def test_validate_rejects_missing_manifest(isolated_env, tmp_path):
    db_path, cfg_path, _ = isolated_env
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    result = _run_cli(
        ["validate", str(empty_dir)],
        db_path=db_path,
        cfg_path=cfg_path,
    )
    assert result.returncode == 1


# --- feature-flag gating ---

def test_export_blocked_when_flag_off(tmp_path):
    db_path = tmp_path / "theforge.db"
    _apply_schema(db_path)
    cfg_path = tmp_path / "dispatch_config.json"
    _write_dispatch_config(cfg_path, templates_enabled=False)

    project_id = _seed_minimal_project(db_path)
    result = _run_cli(
        ["export", str(project_id), "--out", str(tmp_path / "out")],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode != 0
    assert "project_templates" in result.stderr
    # No archive must have been written.
    assert not (tmp_path / "out" / "manifest.json").exists()


def test_import_blocked_when_flag_off(tmp_path):
    db_path = tmp_path / "theforge.db"
    _apply_schema(db_path)
    cfg_path = tmp_path / "dispatch_config.json"
    _write_dispatch_config(cfg_path, templates_enabled=False)

    result = _run_cli(
        ["import", str(FIXTURE_DIR), "--name", "blocked-import"],
        db_path=db_path,
        cfg_path=cfg_path,
    )

    assert result.returncode != 0
    assert "project_templates" in result.stderr
    # Confirm no project was inserted.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM projects WHERE name = ?",
            ("blocked-import",),
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_validate_blocked_when_flag_off(tmp_path):
    db_path = tmp_path / "theforge.db"
    _apply_schema(db_path)
    cfg_path = tmp_path / "dispatch_config.json"
    _write_dispatch_config(cfg_path, templates_enabled=False)

    result = _run_cli(
        ["validate", str(FIXTURE_DIR)],
        db_path=db_path,
        cfg_path=cfg_path,
    )
    assert result.returncode != 0
    assert "project_templates" in result.stderr
