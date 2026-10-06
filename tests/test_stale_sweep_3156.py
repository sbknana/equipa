#!/usr/bin/env python3
"""Task 3156: the stale per-run config dir sweep (F-7, F-8 of the 3153 review).

F-7: the sweep's ``lstat`` filter was not pinned on its own. A mutant that
followed symlinks (``entry.stat(follow_symlinks=True)``) survived the suite,
because ``shutil.rmtree`` refuses a symlink root anyway. The test below
records every directory the sweep tries to remove: a prefixed symlink to an
old owned directory must never be one of them.

F-8: liveness was judged by top-level mtimes only, but the CLI writes into
nested subdirectories (``projects/<cwd>/<session>.jsonl``), which change
neither. The sweep now takes the newest mtime anywhere in the tree, with a
bounded walk; a tree past the bound is kept.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from equipa import cli_isolation
from equipa.cli_isolation import RUN_CONFIG_DIR_PREFIX, sweep_stale_run_config_dirs

DAY = 24 * 3600
OLD = DAY + 600


def _age_tree(root: Path, seconds: float) -> None:
    """Set every mtime in ``root`` (not following symlinks) ``seconds`` back,
    deepest first so a parent's mtime is not refreshed afterwards."""
    stamp = time.time() - seconds
    for directory, subdirectories, files in os.walk(root, topdown=False):
        for name in files + subdirectories:
            os.utime(os.path.join(directory, name), (stamp, stamp),
                     follow_symlinks=False)
    os.utime(root, (stamp, stamp), follow_symlinks=False)


def _run_dir(parent: Path, name: str) -> Path:
    """A per-run dir shaped like the CLI's: a session file two levels down."""
    run = parent / f"{RUN_CONFIG_DIR_PREFIX}{name}"
    session = run / "projects" / "-srv-project"
    session.mkdir(parents=True)
    (session / "session.jsonl").write_text("{}\n", encoding="utf-8")
    (run / ".claude.json").write_text("{}", encoding="utf-8")
    return run


@pytest.fixture
def removal_calls(monkeypatch):
    """Record every path the sweep asks to remove, then remove it."""
    calls: list[str] = []
    real_remove = cli_isolation.remove_run_config_dir

    def recording_remove(path: str) -> list[str]:
        calls.append(path)
        return real_remove(path)

    monkeypatch.setattr(cli_isolation, "remove_run_config_dir",
                        recording_remove)
    return calls


# --- F-7: symlinks are never candidates ----------------------------------------

def test_a_prefixed_symlink_to_an_old_owned_dir_is_never_removed(
        tmp_path, removal_calls):
    parent = tmp_path / "parent"
    parent.mkdir()
    target = _run_dir(tmp_path, "target")
    _age_tree(target, OLD)
    link = parent / f"{RUN_CONFIG_DIR_PREFIX}link"
    link.symlink_to(target, target_is_directory=True)
    os.utime(link, (time.time() - OLD,) * 2, follow_symlinks=False)

    removed = sweep_stale_run_config_dirs(str(parent))

    assert removed == []
    assert removal_calls == [], "the sweep followed a symlink"
    assert link.is_symlink()
    assert (target / "projects" / "-srv-project" / "session.jsonl").exists()


def test_the_symlink_check_is_on_the_entry_not_its_target(
        tmp_path, removal_calls):
    """Control for the test above: the same old directory, as a real
    prefixed directory, is removed, so only the symlink kept it."""
    parent = tmp_path / "parent"
    parent.mkdir()
    real = _run_dir(parent, "real")
    _age_tree(real, OLD)

    assert sweep_stale_run_config_dirs(str(parent)) == [str(real)]
    assert removal_calls == [str(real)]


# --- F-8: the newest mtime anywhere in the tree ------------------------------------

def test_a_run_writing_only_deep_in_its_tree_is_kept(tmp_path):
    live = _run_dir(tmp_path, "live")
    _age_tree(live, OLD)
    session = live / "projects" / "-srv-project" / "session.jsonl"
    # The CLI appends to its session file: only that file's mtime changes.
    with session.open("a", encoding="utf-8") as handle:
        handle.write("{}\n")
    assert live.stat().st_mtime < time.time() - DAY
    assert (live / "projects").stat().st_mtime < time.time() - DAY

    assert sweep_stale_run_config_dirs(str(tmp_path)) == []
    assert session.exists()


def test_an_old_tree_is_removed(tmp_path):
    stale = _run_dir(tmp_path, "stale")
    _age_tree(stale, OLD)

    assert sweep_stale_run_config_dirs(str(tmp_path)) == [str(stale)]
    assert not os.path.lexists(stale)


def test_a_fresh_symlink_inside_counts_without_being_followed(tmp_path):
    stale = _run_dir(tmp_path, "stale")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "fresh.txt").write_text("fresh", encoding="utf-8")
    (stale / "projects" / "link").symlink_to(outside)
    _age_tree(stale, OLD)

    # Everything in the run is old; the fresh file behind the link is not
    # looked at, and the link's target survives the removal.
    assert sweep_stale_run_config_dirs(str(tmp_path)) == [str(stale)]
    assert (outside / "fresh.txt").read_text(encoding="utf-8") == "fresh"


def test_a_tree_past_the_entry_bound_is_kept(tmp_path, monkeypatch):
    big = _run_dir(tmp_path, "big")
    for index in range(10):
        (big / f"file-{index}").write_text("x", encoding="utf-8")
    _age_tree(big, OLD)
    monkeypatch.setattr(cli_isolation, "STALE_SCAN_MAX_ENTRIES", 5)

    assert sweep_stale_run_config_dirs(str(tmp_path)) == []
    assert big.is_dir()


def test_a_tree_past_the_depth_bound_is_kept(tmp_path, monkeypatch):
    deep = _run_dir(tmp_path, "deep")
    nested = deep
    for level in range(4):
        nested = nested / f"level-{level}"
    nested.mkdir(parents=True)
    _age_tree(deep, OLD)
    monkeypatch.setattr(cli_isolation, "STALE_SCAN_MAX_DEPTH", 3)

    assert sweep_stale_run_config_dirs(str(tmp_path)) == []
    assert deep.is_dir()


def test_newest_mtime_reads_every_level(tmp_path):
    run = _run_dir(tmp_path, "run")
    _age_tree(run, OLD)
    deepest = run / "projects" / "-srv-project" / "session.jsonl"
    stamp = time.time() - 30
    os.utime(deepest, (stamp, stamp))

    newest = cli_isolation._newest_mtime(str(run), os.lstat(run))

    assert newest == pytest.approx(stamp, abs=1)
