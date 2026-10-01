"""Task #3146 — IND3132-01: the repository identity is pinned with the guard.

The default-branch guard is snapshotted before any agent runs. Since task
#3146 it also records which repository the project directory is: the
realpath (and inode) of the git common dir, the kind and inode of the
``.git`` entry, and the default branch's commit read through that common
dir. The gate and every guard check compare against it, so an agent that
swaps the project's ``.git`` directory for a ``gitdir:`` file pointing at a
clone of its own cannot make the orchestrator merge into that clone.

The reviewer's probes, on real git:

* p1: single plant, non-nested project, no task worktree;
* p1b: single plant in a nested project (the clone's files copied in);
* p2: the double plant (nested project and its task worktree both redirected);
* single-task ``--task --dev-test`` mode, where the agent plants after the
  guard snapshot and the gate runs on the redirected repository.

On the task #3141 code each of these ends ``merged`` into the clone. In
single-task mode a clone left on ``master`` is already stopped by the
worktree branch check (only the identity alarm is new); a clone left on the
task branch passes that check and merges into the clone.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod
from equipa.merge_integrity import DefaultBranchGuard

from test_dispatch_modes_gated_3112 import (
    GateProbe,
    _cli_args,
    _commit_files,
    _git,
    _init_repo,
    _master,
    _patch_cli_task_basics,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)

TASK_ID = 3146
TASK_BRANCH = f"forge-task-{TASK_ID}"
PAYLOAD = "import os\nos.system('echo FAKE-SENTINEL-3146')\n"


def _run(coro):
    return asyncio.run(coro)


def _master_in(git_dir: Path) -> str:
    """master as stored in ``git_dir`` itself, whatever any ``.git`` says."""
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "rev-parse", "refs/heads/master"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _clone_with_decoy_branch(real: Path, other: Path) -> None:
    """The agent's own clone: same master, a README-only task branch."""
    subprocess.run(
        ["git", "clone", "-q", str(real), str(other)], check=True,
        capture_output=True,
    )
    _git(other, "config", "user.email", "agent@forgeborn.dev")
    _git(other, "config", "user.name", "Agent")
    _git(other, "checkout", "-q", "-B", "master", "origin/master")
    _git(other, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(other, {"README.md": "decoy docs\n"}, "decoy doc change")
    _git(other, "checkout", "-q", "master")


def _plant_git_file(directory: Path, target_git_dir: Path) -> None:
    (directory / ".git").write_text(f"gitdir: {target_git_dir}\n", encoding="utf-8")


def _mirror_checkout(clone: Path, directory: Path) -> None:
    """Copy the clone's files into ``directory`` so that, seen through a
    planted ``.git`` file, it is a clean checkout of the clone."""
    shutil.copytree(
        clone, directory, dirs_exist_ok=True,
        ignore=lambda folder, names: [".git"] if Path(folder) == clone else [],
    )


def _real_with_task_branch(tmp_path: Path) -> Path:
    real = _init_repo(tmp_path / "real")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(real, {"lib/code.py": PAYLOAD}, "task code")
    _git(real, "checkout", "-q", "master")
    return real


def _gate(repo: Path, guard: DefaultBranchGuard, worktree: Path | None = None) -> str:
    return _run(dispatch_mod._gated_merge_task(
        repo=str(repo), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID, guard=guard,
        worktree_dir=str(worktree) if worktree is not None else None,
    ))


# --- the reviewer's probes at the gate ---------------------------------------


def test_p1_git_directory_swapped_for_a_file_is_refused(tmp_path, capsys):
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    real_master, other_master = _master(real), _master(other)
    # The agent's plant, after the snapshot.
    os.rename(real / ".git", real / ".git.bak")
    _plant_git_file(real, other / ".git")

    status = _gate(real, guard)

    assert status == "blocked"
    assert _master(other) == other_master, "the clone's master moved"
    assert _master_in(real / ".git.bak") == real_master
    assert guard.tripped and "repository" in guard.alert
    assert "event=repository-identity-changed" in capsys.readouterr().err


def test_p1b_single_plant_in_a_nested_project_is_refused(tmp_path, capsys):
    real = _real_with_task_branch(tmp_path)
    _commit_files(real, {"sub/app.py": "print('sub')\n"}, "nested project")
    project = real / "sub"
    guard = _run(DefaultBranchGuard.snapshot(project))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    _mirror_checkout(other, project)
    real_master, other_master = _master(real), _master(other)
    _plant_git_file(project, other / ".git")

    status = _gate(project, guard)

    assert status == "blocked"
    assert _master(other) == other_master
    assert _master(real) == real_master
    assert "event=repository-identity-changed" in capsys.readouterr().err


def test_p2_double_plant_in_the_project_and_its_worktree_is_refused(tmp_path, capsys):
    real = _real_with_task_branch(tmp_path)
    _commit_files(real, {"sub/app.py": "print('sub')\n"}, "nested project")
    _git(real, "branch", "-f", TASK_BRANCH, "master")
    _git(real, "checkout", "-q", TASK_BRANCH)
    _commit_files(real, {"lib/code.py": PAYLOAD}, "task code")
    _git(real, "checkout", "-q", "master")
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real / "sub"))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    real_master, other_master = _master(real), _master(other)
    # Both sides redirected to the same clone: their common dirs agree with
    # each other, so only the pinned identity can tell.
    _mirror_checkout(other, real / "sub")
    _plant_git_file(real / "sub", other / ".git")
    _plant_git_file(worktree / "sub", other / ".git")

    status = _gate(real / "sub", guard, worktree / "sub")

    assert status == "blocked"
    assert _master(other) == other_master
    assert _master(real) == real_master
    assert "event=repository-identity-changed" in capsys.readouterr().err


def test_git_directory_replaced_by_another_directory_is_refused(tmp_path):
    """Same path, different directory: the common dir's inode changed."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    os.rename(real / ".git", tmp_path / "real-git-moved")
    shutil.copytree(other / ".git", real / ".git", symlinks=True)

    assert not _run(guard.verify("after-agent", task_id=TASK_ID))
    assert "common_dir_id" in guard.alert


def test_every_guard_check_sees_the_plant(tmp_path):
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    assert _run(guard.verify("after-agent", task_id=TASK_ID))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    os.rename(real / ".git", real / ".git.bak")
    _plant_git_file(real, other / ".git")

    assert not _run(guard.verify("end-of-task", task_id=TASK_ID))
    assert "git_entry" in guard.alert or "common_dir" in guard.alert


def test_record_merge_refuses_a_merge_into_another_repository(tmp_path):
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    _git(other, "merge", "-q", "--no-edit", TASK_BRANCH)
    landed = _master(other)
    os.rename(real / ".git", real / ".git.bak")
    _plant_git_file(real, other / ".git")

    assert not _run(guard.record_merge(TASK_ID, landed, post_head=landed))
    assert guard.tripped and guard.expected_sha == _master_in(real / ".git.bak")


# --- identity contents and controls ------------------------------------------


def test_snapshot_records_the_repository_identity(tmp_path):
    real = _real_with_task_branch(tmp_path)
    _commit_files(real, {"sub/app.py": "x = 1\n"}, "nested")
    guard = _run(DefaultBranchGuard.snapshot(real))
    nested = _run(DefaultBranchGuard.snapshot(real / "sub"))

    assert guard.identity.common_dir == os.path.realpath(real / ".git")
    assert guard.identity.git_entry == "dir"
    assert guard.identity.default_head == _master(real)
    assert nested.identity.common_dir == guard.identity.common_dir
    assert nested.identity.git_entry == "absent"


def test_unchanged_repository_and_its_worktree_pass_control(tmp_path):
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "wt"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))

    assert _run(guard.repository_problem(real, worktree)) is None
    assert _run(guard.verify("pre-gate", task_id=TASK_ID, directories=(real, worktree)))


def test_plain_default_branch_move_is_still_reported_as_a_move_control(tmp_path):
    """A foreign commit on master is the SHA chain's alarm, not an identity
    change: the alert keeps naming both full SHAs."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    baseline = _master(real)
    _commit_files(real, {"foreign.txt": "x\n"}, "foreign")

    assert not _run(guard.verify("after-agent", task_id=TASK_ID))
    assert baseline in guard.alert and _master(real) in guard.alert
    assert "moved outside the orchestrator's merges" in guard.alert


def test_unrelated_worktree_directory_is_refused(tmp_path):
    real = _real_with_task_branch(tmp_path)
    stranger = _init_repo(tmp_path / "stranger")
    guard = _run(DefaultBranchGuard.snapshot(real))

    problem = _run(guard.repository_problem(stranger))
    assert problem is not None and "not the pinned" in problem


# --- single-task mode --------------------------------------------------------


def test_single_task_mode_plant_after_the_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """``--task --dev-test``: the guard is snapshotted before the agent runs;
    the agent then swaps the project's ``.git`` for a clone of its own whose
    task branch is a README edit, and points its worktree there too. Nothing
    is merged anywhere and the run raises the repository-identity alarm."""
    repo = _init_repo(tmp_path / "repo")
    other = tmp_path / "other"
    task = _task(TASK_ID)
    probe = GateProbe(repo, review_high=0)
    probe.install(monkeypatch)
    captured = _patch_cli_task_basics(monkeypatch, repo, task, probe)
    masters: dict[str, str] = {}

    async def planting_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        _clone_with_decoy_branch(repo, other)
        masters["repo"], masters["other"] = _master(repo), _master(other)
        os.rename(repo / ".git", repo / ".git.bak")
        _plant_git_file(repo, other / ".git")
        _plant_git_file(Path(project_dir), other / ".git")
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", planting_dev_test_loop)

    asyncio.run(cli_mod.run_mode_task(_cli_args(task=TASK_ID)))

    assert masters, "the agent never ran"
    assert _master(other) == masters["other"], "the clone's master moved"
    assert _master_in(repo / ".git.bak") == masters["repo"]
    assert captured.get("outcome") != "tests_passed"
    assert not captured.get("merged_sha")
    assert "event=repository-identity-changed" in capsys.readouterr().err


def _replace_with_clone_files(directory: Path, clone: Path) -> None:
    """Empty ``directory`` (keeping the git entries and a nested
    ``.forge-worktrees``) and copy the clone's files in, so it reads as a
    clean checkout of the clone."""
    for entry in directory.iterdir():
        if entry.name in (".git", ".git.bak", ".forge-worktrees"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    _mirror_checkout(clone, directory)


def test_single_task_mode_clone_left_on_the_task_branch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """The reviewer's p1 in ``--task --dev-test`` mode, carried through: the
    agent leaves its clone on the task branch and makes the project and its
    worktree clean checkouts of the clone, so the worktree branch check
    passes through the planted ``.git`` files. On the task #3141 code the
    README-only decoy branch is doc-only, needs no review, and merges into
    the clone's master. The pinned repository identity refuses it."""
    repo = _init_repo(tmp_path / "repo")
    other = tmp_path / "other"
    task = _task(TASK_ID)
    probe = GateProbe(repo, review_high=0)
    probe.install(monkeypatch)
    captured = _patch_cli_task_basics(monkeypatch, repo, task, probe)
    masters: dict[str, str] = {}

    async def planting_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        _clone_with_decoy_branch(repo, other)
        _git(other, "checkout", "-q", TASK_BRANCH)
        with open(other / ".git" / "info" / "exclude", "a", encoding="utf-8") as exclude:
            exclude.write(".forge-worktrees/\n")
        masters["repo"], masters["other"] = _master(repo), _master(other)
        os.rename(repo / ".git", repo / ".git.bak")
        for directory in (repo, Path(project_dir)):
            _replace_with_clone_files(directory, other)
            _plant_git_file(directory, other / ".git")
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", planting_dev_test_loop)

    asyncio.run(cli_mod.run_mode_task(_cli_args(task=TASK_ID)))

    assert masters, "the agent never ran"
    assert _master(other) == masters["other"], "merged into the agent's clone"
    assert _master_in(repo / ".git.bak") == masters["repo"]
    assert captured.get("outcome") != "tests_passed"
    assert not captured.get("merged_sha")
    assert "event=repository-identity-changed" in capsys.readouterr().err
