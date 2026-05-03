"""Tests for the CLI ``equipa config <verb>`` surface and the
auto-snapshot hooks at dispatch and heartbeat sweep entry (PLAN-1067 §1.A3).

Covers:
  - ``--config-cmd snapshot`` writes a row.
  - ``--config-cmd list`` prints the snapshot row.
  - ``--config-cmd diff`` emits a unified diff.
  - ``--config-cmd rollback`` restores the file (with --force) and
    ``--dry-run`` reports without writing.
  - Two consecutive dispatches with unchanged config produce only ONE
    new ``config_versions`` row (dedup proof for the auto-dispatch hook).
  - Heartbeat ``run_once`` calls ``snapshot`` once per active project.
  - Feature flag OFF -> no auto-snapshot fires for either hook.

Each test runs against a tmp DB + tmp REPO_ROOT so production data is
never touched (TS-01 lesson).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT_REAL = Path(__file__).resolve().parent.parent


# --- Fixtures ---

@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate DB, REPO_ROOT, and PROMPTS_DIR.

    Returns ``(repo_root, db_path, project_id)``.
    """
    db_path = tmp_path / "test_theforge.db"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    prompts_dir = repo_root / "prompts"
    prompts_dir.mkdir()

    schema_sql = (REPO_ROOT_REAL / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, codename) VALUES (?, ?)",
        ("CliConfigTest", "cct"),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()

    (repo_root / "dispatch_config.json").write_text(
        json.dumps({"model": "sonnet", "max_turns": 25}, indent=2)
    )
    (repo_root / "forge_config.json").write_text(
        json.dumps({"project_dirs": {"cct": "/tmp/cct"}}, indent=2)
    )
    (prompts_dir / "developer.md").write_text("# Developer prompt\n")

    import equipa.constants as constants
    import equipa.db as db_mod
    import equipa.config_versions as cv
    import equipa.config as cfg_mod

    monkeypatch.setattr(constants, "THEFORGE_DB", db_path)
    monkeypatch.setattr(db_mod, "THEFORGE_DB", db_path)
    monkeypatch.setattr(cfg_mod, "THEFORGE_DB", db_path)
    monkeypatch.setattr(cv, "REPO_ROOT", repo_root.resolve())
    monkeypatch.setattr(cv, "PROMPTS_DIR", prompts_dir)

    return repo_root, db_path, int(project_id)


def _config_args(**overrides) -> argparse.Namespace:
    """Build an argparse.Namespace mimicking the CLI surface used by
    :func:`equipa.cli.run_mode_config`."""
    base = dict(
        config_cmd=None,
        config_project=None,
        goal_project=None,
        config_message=None,
        config_version_a=None,
        config_version_b=None,
        config_version=None,
        dry_run=False,
        force=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _count_rows(db_path: Path, project_id: int) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM config_versions WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]


# --- Subcommand: snapshot ---

def test_cli_snapshot_writes_row(isolated_env, capsys):
    from equipa.cli import run_mode_config
    _, db_path, project_id = isolated_env

    args = _config_args(
        config_cmd="snapshot", config_project=project_id, config_message="m1",
    )
    asyncio.run(run_mode_config(args))

    out = capsys.readouterr().out
    assert f"project={project_id}" in out
    assert "version_id=" in out
    assert _count_rows(db_path, project_id) == 1


# --- Subcommand: list ---

def test_cli_list_prints_versions(isolated_env, capsys):
    from equipa.cli import run_mode_config
    _, _db_path, project_id = isolated_env

    asyncio.run(run_mode_config(_config_args(
        config_cmd="snapshot", config_project=project_id, config_message="initial",
    )))
    capsys.readouterr()  # drain

    asyncio.run(run_mode_config(_config_args(
        config_cmd="list", config_project=project_id,
    )))
    out = capsys.readouterr().out
    assert "initial" in out
    assert "manual" in out


def test_cli_list_empty_project(isolated_env, capsys):
    from equipa.cli import run_mode_config
    _, _db_path, project_id = isolated_env

    asyncio.run(run_mode_config(_config_args(
        config_cmd="list", config_project=project_id,
    )))
    out = capsys.readouterr().out
    assert "No config versions" in out


# --- Subcommand: diff ---

def test_cli_diff_emits_unified_diff(isolated_env, capsys):
    from equipa.cli import run_mode_config
    repo_root, _db_path, project_id = isolated_env

    asyncio.run(run_mode_config(_config_args(
        config_cmd="snapshot", config_project=project_id, config_message="v1",
    )))
    out_v1 = capsys.readouterr().out
    v1 = int(out_v1.split("version_id=")[1].strip())

    (repo_root / "dispatch_config.json").write_text(
        json.dumps({"model": "opus", "max_turns": 30}, indent=2)
    )
    asyncio.run(run_mode_config(_config_args(
        config_cmd="snapshot", config_project=project_id, config_message="v2",
    )))
    out_v2 = capsys.readouterr().out
    v2 = int(out_v2.split("version_id=")[1].strip())

    asyncio.run(run_mode_config(_config_args(
        config_cmd="diff", config_version_a=v1, config_version_b=v2,
    )))
    out = capsys.readouterr().out
    assert "dispatch_config.json" in out
    assert "sonnet" in out
    assert "opus" in out


def test_cli_diff_requires_both_ids(isolated_env, capsys):
    from equipa.cli import run_mode_config
    _, _db_path, _project_id = isolated_env

    with pytest.raises(SystemExit):
        asyncio.run(run_mode_config(_config_args(
            config_cmd="diff", config_version_a=1,
        )))


# --- Subcommand: rollback ---

def test_cli_rollback_restores_file(isolated_env, capsys):
    from equipa.cli import run_mode_config
    repo_root, _db_path, project_id = isolated_env

    asyncio.run(run_mode_config(_config_args(
        config_cmd="snapshot", config_project=project_id,
    )))
    out_v1 = capsys.readouterr().out
    v1 = int(out_v1.split("version_id=")[1].strip())

    target = repo_root / "dispatch_config.json"
    original = target.read_text()
    target.write_text(json.dumps({"model": "haiku"}, indent=2))

    args = _config_args(
        config_cmd="rollback", config_version=v1, force=True,
    )
    asyncio.run(run_mode_config(args))

    assert json.loads(target.read_text()) == json.loads(original)


def test_cli_rollback_dry_run(isolated_env, capsys):
    from equipa.cli import run_mode_config
    repo_root, _db_path, project_id = isolated_env

    asyncio.run(run_mode_config(_config_args(
        config_cmd="snapshot", config_project=project_id,
    )))
    v1 = int(capsys.readouterr().out.split("version_id=")[1].strip())

    target = repo_root / "dispatch_config.json"
    target.write_text(json.dumps({"model": "haiku"}, indent=2))

    asyncio.run(run_mode_config(_config_args(
        config_cmd="rollback", config_version=v1, dry_run=True, force=True,
    )))
    out = capsys.readouterr().out
    assert "would rewrite" in out
    # File NOT actually changed.
    assert json.loads(target.read_text()) == {"model": "haiku"}


def test_cli_rollback_requires_version(isolated_env):
    from equipa.cli import run_mode_config
    _, _db_path, _project_id = isolated_env

    with pytest.raises(SystemExit):
        asyncio.run(run_mode_config(_config_args(config_cmd="rollback")))


# --- Auto-snapshot hook (dispatch) ---

def test_auto_snapshot_dispatch_dedup_unchanged_config(isolated_env):
    """Two consecutive dispatch entries with identical config produce
    exactly ONE config_versions row."""
    from equipa.cli import _auto_snapshot_dispatch
    _, db_path, project_id = isolated_env

    dc = {"features": {"config_versioning": True}}
    _auto_snapshot_dispatch(project_id, dc)
    _auto_snapshot_dispatch(project_id, dc)
    assert _count_rows(db_path, project_id) == 1


def test_auto_snapshot_dispatch_writes_new_row_on_change(isolated_env):
    from equipa.cli import _auto_snapshot_dispatch
    repo_root, db_path, project_id = isolated_env

    dc = {"features": {"config_versioning": True}}
    _auto_snapshot_dispatch(project_id, dc)

    (repo_root / "forge_config.json").write_text(
        json.dumps({"project_dirs": {"cct": "/tmp/changed"}}, indent=2)
    )
    _auto_snapshot_dispatch(project_id, dc)

    assert _count_rows(db_path, project_id) == 2


def test_auto_snapshot_dispatch_skipped_when_flag_off(isolated_env):
    from equipa.cli import _auto_snapshot_dispatch
    _, db_path, project_id = isolated_env

    dc = {"features": {"config_versioning": False}}
    _auto_snapshot_dispatch(project_id, dc)
    _auto_snapshot_dispatch(project_id, dc)
    assert _count_rows(db_path, project_id) == 0

    # Default (key absent) is also OFF.
    _auto_snapshot_dispatch(project_id, {})
    assert _count_rows(db_path, project_id) == 0


def test_auto_snapshot_dispatch_swallows_errors(isolated_env, monkeypatch):
    """Snapshot failures must NOT crash the caller."""
    from equipa.cli import _auto_snapshot_dispatch
    _, _db_path, project_id = isolated_env

    def boom(*_a, **_kw):
        raise RuntimeError("simulated DB outage")

    import equipa.config_versions as cv
    monkeypatch.setattr(cv, "snapshot", boom)

    # No exception propagates.
    _auto_snapshot_dispatch(project_id, {"features": {"config_versioning": True}})


# --- Auto-snapshot hook (heartbeat) ---

def _make_heartbeat_config(db_path: Path):
    from equipa.heartbeat import HeartbeatConfig
    return HeartbeatConfig(
        db_path=db_path,
        interval_seconds=600,
        dispatch_timeout_seconds=3600,
        container_timeout_seconds=21600,
        enable_redispatch=False,
        enable_container_check=False,
        enable_orphan_prune=False,
        dry_run=True,
    )


def test_heartbeat_auto_snapshot_runs_when_flag_on(
    isolated_env, monkeypatch, tmp_path,
):
    """Heartbeat sweep snapshots active projects when the feature is ON."""
    from equipa import heartbeat
    repo_root, db_path, project_id = isolated_env

    # Seed dispatch_config.json next to the DB so load_dispatch_config()
    # picks up the feature-enabled config (its default location).
    (db_path.parent / "dispatch_config.json").write_text(json.dumps({
        "features": {"config_versioning": True},
    }))

    cfg = _make_heartbeat_config(db_path)
    heartbeat.run_once(cfg)

    assert _count_rows(db_path, project_id) == 1


def test_heartbeat_auto_snapshot_skipped_when_flag_off(
    isolated_env, monkeypatch,
):
    from equipa import heartbeat
    _, db_path, project_id = isolated_env

    (db_path.parent / "dispatch_config.json").write_text(json.dumps({
        "features": {"config_versioning": False},
    }))

    cfg = _make_heartbeat_config(db_path)
    heartbeat.run_once(cfg)

    assert _count_rows(db_path, project_id) == 0


def test_heartbeat_auto_snapshot_swallows_errors(
    isolated_env, monkeypatch, capsys,
):
    """A snapshot failure must not break the heartbeat sweep."""
    from equipa import heartbeat
    _, db_path, _project_id = isolated_env

    (db_path.parent / "dispatch_config.json").write_text(json.dumps({
        "features": {"config_versioning": True},
    }))

    import equipa.config_versions as cv
    monkeypatch.setattr(
        cv, "snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    cfg = _make_heartbeat_config(db_path)
    # Must not raise.
    heartbeat.run_once(cfg)
