"""Task #3158 — findings R3155-01, R3155-02 and R3155-03 of the task #3155 review.

* R3155-01 (MEDIUM): cleanup of an unmerged task ran ``git status`` and
  ``git stash push -u`` in the agent's worktree by discovery, so a filter
  driver the agent planted (in the worktree config, the repository config,
  ``info/attributes``, or a repository its ``.git`` names) ran inside the
  orchestrator. Every check here plants such a driver, leaves uncommitted
  work behind and asserts the driver never ran while the work was still
  saved, on the cleanup helpers and on the success, failure and
  early-termination paths of a whole isolated run.
* R3155-02 (LOW): the merge-path work tree was checked by path and then used
  by path. A swap between the two reached git; now git starts in the pinned
  descriptor and a swap still in place afterwards is raised.
* R3155-03 (LOW): a swap detected during generated-file resolution was
  swallowed as a refused regeneration; it now trips the guard.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.dispatch as dispatch_mod
from equipa.git_ops import (
    PinnedRepositoryError,
    git_repositories_pinned,
    git_run,
    git_run_async,
)
from equipa.merge_integrity import DefaultBranchGuard

from test_dispatch_modes_gated_3112 import (
    _git,
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)
from test_merge_path_pinned_3151 import _gate
from test_merge_path_pinned_3155 import _conflicting_project
from test_repository_identity_3146 import TASK_BRANCH, TASK_ID, _real_with_task_branch

PLANTS = (
    "worktree-config",
    "repository-config",
    "info-attributes",
    "process-driver",
    "redirected-gitfile",
)
STASH_TAG = f"equipa-early-term task-{TASK_ID}"
UNSAVED = "print('unsaved agent work')"


def _run(coro):
    return asyncio.run(coro)


def _with_project_dir(func, *args, project_dir: Path):
    """Call ``func`` the way the code under test accepts: task #3158 added
    ``project_dir``; without it the call is the pre-fix one, so the checks
    below fail on the old code for what it does, not for a TypeError."""
    if "project_dir" in inspect.signature(func).parameters:
        return func(*args, project_dir=str(project_dir))
    return func(*args)


class Drivers:
    """Executables an agent names as filter drivers; each records that it ran."""

    def __init__(self, tmp_path: Path) -> None:
        self.marker = tmp_path / "agent-driver-ran"
        touch = f"import pathlib\npathlib.Path({str(self.marker)!r}).touch()\n"
        self.filter = tmp_path / "agent-filter"
        self.filter.write_text(
            f"#!{sys.executable}\n{touch}"
            "import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n"
        )
        # A long-running filter process; this one exits at once.
        self.process = tmp_path / "agent-process-filter"
        self.process.write_text(f"#!{sys.executable}\n{touch}")
        for script in (self.filter, self.process):
            script.chmod(0o755)

    def ran(self) -> bool:
        return self.marker.exists()


def _define_filter(directory: Path, drivers: Drivers, *scope: str) -> None:
    _git(directory, "config", *scope, "filter.probe.clean", str(drivers.filter))
    _git(directory, "config", *scope, "filter.probe.smudge", str(drivers.filter))


def _plant(kind: str, repo: Path, worktree: Path, drivers: Drivers, tmp_path: Path) -> None:
    """The agent's plant: a driver defined where git reads config, selected
    for every path, in a place the agent can write."""
    if kind == "worktree-config":
        _git(repo, "config", "extensions.worktreeConfig", "true")
        _define_filter(worktree, drivers, "--worktree")
        (worktree / ".gitattributes").write_text("* filter=probe\n")
    elif kind == "repository-config":
        _define_filter(repo, drivers)
        (worktree / ".gitattributes").write_text("* filter=probe\n")
    elif kind == "info-attributes":
        _define_filter(repo, drivers)
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* filter=probe\n")
    elif kind == "process-driver":
        _git(repo, "config", "filter.probe.process", str(drivers.process))
        (worktree / ".gitattributes").write_text("* filter=probe\n")
    elif kind == "redirected-gitfile":
        agent = _init_repo(tmp_path / "agent-repo")
        _define_filter(agent, drivers)
        (agent / ".git" / "info").mkdir(exist_ok=True)
        (agent / ".git" / "info" / "attributes").write_text("* filter=probe\n")
        (worktree / ".git").write_text(f"gitdir: {agent / '.git'}\n")
    else:  # pragma: no cover - a typo in PLANTS
        raise AssertionError(kind)


def _leave_unsaved_work(worktree: Path) -> None:
    # Same size as the committed "seed\n": git has to hash it to see the
    # change, which is when a clean filter runs.
    (worktree / "README.md").write_text("SEED\n")
    (worktree / "work.py").write_text(f"{UNSAVED}\n")


def _task_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path / "repo")
    worktree = repo / ".forge-worktrees" / f"task-{TASK_ID}"
    _git(repo, "worktree", "add", "-q", "-b", TASK_BRANCH, str(worktree), "master")
    return repo, worktree


def _assert_work_stashed(repo: Path) -> None:
    """The stash is in the repository, on the task branch, with the work."""
    assert STASH_TAG in _git(repo, "stash", "list")
    assert f"On {TASK_BRANCH}:" in _git(repo, "stash", "list")
    assert _git(repo, "cat-file", "-p", "stash@{0}:README.md") == "SEED"
    assert _git(repo, "cat-file", "-p", "stash@{0}^3:work.py") == UNSAVED
    assert _git(repo, "rev-parse", "stash@{0}^1") == _git(repo, "rev-parse", TASK_BRANCH)


# --- R3155-01: the cleanup helpers -------------------------------------------


@pytest.mark.parametrize("kind", PLANTS)
def test_stash_never_runs_an_agent_driver_and_saves_the_work(
    tmp_path: Path, kind: str, capsys,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    _leave_unsaved_work(worktree)

    _run(_with_project_dir(
        dispatch_mod._stash_uncommitted_in_worktree,
        str(worktree), TASK_ID, TASK_BRANCH, project_dir=repo,
    ))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    assert "stashed uncommitted work" in capsys.readouterr().out
    _assert_work_stashed(repo)


@pytest.mark.parametrize("kind", PLANTS)
def test_dirty_check_never_runs_an_agent_driver(tmp_path: Path, kind: str) -> None:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    _leave_unsaved_work(worktree)

    dirty = _run(_with_project_dir(
        dispatch_mod._worktree_dirty_reason, str(worktree), project_dir=repo,
    ))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    assert dirty and "uncommitted change" in dirty, dirty


@pytest.mark.parametrize("kind", ("repository-config", "info-attributes"))
def test_clean_worktree_reads_clean_without_running_an_agent_driver(
    tmp_path: Path, kind: str,
) -> None:
    """Nothing but the plant outside the work tree: the work tree is clean
    (a racily-clean README is hashed raw and matches), and nothing ran."""
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    (worktree / ".gitattributes").unlink(missing_ok=True)
    # Same content, new mtime: git must hash it to know it is unchanged.
    os.utime(worktree / "README.md", ns=(1, 1))

    dirty = _run(_with_project_dir(
        dispatch_mod._worktree_dirty_reason, str(worktree), project_dir=repo,
    ))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    assert dirty is None


def test_index_copy_keeps_the_index_timestamps(tmp_path: Path) -> None:
    """git re-hashes an entry whose file is not older than the index
    ("racily clean"). A copy stamped now would let a same-size edit made in
    the second of the checkout read as unchanged, and leave it unstashed."""
    from equipa.git_ops import _copy_regular_file

    source = tmp_path / "index"
    source.write_bytes(b"DIRC index bytes")
    os.utime(source, ns=(1_000_000_000, 2_000_000_123))

    assert _copy_regular_file(str(source), str(tmp_path / "copy"), 1024)
    assert (tmp_path / "copy").read_bytes() == b"DIRC index bytes"
    assert os.stat(tmp_path / "copy").st_mtime_ns == 2_000_000_123
    assert not _copy_regular_file(str(tmp_path / "absent"), str(tmp_path / "x"), 1024)


def test_repository_stat_settings_are_kept(tmp_path: Path) -> None:
    """A repository on a share without exec bits (``core.filemode=false``)
    must not read every file whose mode differs as uncommitted work."""
    repo, worktree = _task_worktree(tmp_path)
    _git(repo, "config", "core.filemode", "false")
    (worktree / "README.md").chmod(0o755)

    dirty = _run(_with_project_dir(
        dispatch_mod._worktree_dirty_reason, str(worktree), project_dir=repo,
    ))

    assert dirty is None


@pytest.mark.parametrize("kind", PLANTS)
def test_cleanup_of_an_unmerged_task_never_runs_an_agent_driver(
    tmp_path: Path, kind: str,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    _leave_unsaved_work(worktree)

    _run(dispatch_mod._cleanup_worktrees(
        str(repo), {TASK_ID: str(worktree)}, set(), repo / ".forge-worktrees",
    ))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    _assert_work_stashed(repo)
    assert _git(repo, "rev-parse", "--verify", TASK_BRANCH)


@pytest.mark.parametrize("kind", PLANTS)
def test_retiring_a_leftover_worktree_never_runs_an_agent_driver(
    tmp_path: Path, kind: str,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    _leave_unsaved_work(worktree)

    problem = _run(dispatch_mod._retire_leftover_worktree(
        str(repo), worktree, TASK_ID, TASK_BRANCH,
    ))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    _assert_work_stashed(repo)
    if kind != "redirected-gitfile":
        # git itself refuses to remove a worktree whose .git was rewritten.
        assert problem is None, problem
        assert not worktree.exists()


def test_worktree_swapped_for_a_symlink_to_the_main_checkout_is_not_stashed(
    tmp_path: Path, capsys,
) -> None:
    """The worktree path made a symlink to the operator's checkout: on the
    old code the cleanup stashed (and reset) the operator's own work."""
    repo, worktree = _task_worktree(tmp_path)
    (repo / "README.md").write_text("operator work in progress\n")
    moved = worktree.with_name(worktree.name + ".moved")
    os.rename(worktree, moved)
    os.symlink(repo, worktree)

    problem = _run(_with_project_dir(
        dispatch_mod._stash_uncommitted_in_worktree,
        str(worktree), TASK_ID, TASK_BRANCH, project_dir=repo,
    ))

    assert (repo / "README.md").read_text() == "operator work in progress\n"
    assert _git(repo, "stash", "list") == ""
    assert problem
    captured = capsys.readouterr()
    assert "Could not stash uncommitted work" in captured.out
    assert "event=worktree-stash-skipped" in captured.err


def test_unregistered_worktree_is_not_stashed(tmp_path: Path, capsys) -> None:
    """A directory no ``worktrees/`` entry of the repository names (its
    ``gitdir`` file rewritten) is not inspected through its own ``.git``."""
    repo, worktree = _task_worktree(tmp_path)
    _leave_unsaved_work(worktree)
    admin = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    (admin / "gitdir").write_text(str(tmp_path / "elsewhere" / ".git") + "\n")

    problem = _run(_with_project_dir(
        dispatch_mod._stash_uncommitted_in_worktree,
        str(worktree), TASK_ID, TASK_BRANCH, project_dir=repo,
    ))

    assert problem and "not a registered worktree" in problem
    assert _git(repo, "stash", "list") == ""
    assert "event=worktree-stash-skipped" in capsys.readouterr().err


# --- R3155-01: whole isolated runs -------------------------------------------


def _isolated_run(
    tmp_path: Path, kind: str, outcome: str, *, commit: bool,
) -> tuple[Path, Drivers, dispatch_mod.IsolatedTaskRun, str]:
    repo = _init_repo(tmp_path / "repo")
    master_before = _master(repo)
    drivers = Drivers(tmp_path)

    async def agent(agent_dir: str, task_branch: str):
        worktree = Path(agent_dir)
        if commit:
            (worktree / "NOTES.md").write_text("agent notes\n")
            _git(worktree, "add", "NOTES.md")
            _git(worktree, "commit", "-q", "-m", "agent notes")
        _plant(kind, repo, worktree, drivers, tmp_path)
        _leave_unsaved_work(worktree)
        return {"cost": 0.0, "duration": 0.0}, 1, outcome

    args = SimpleNamespace(security_review=False, dispatch_config={})
    run = _run(dispatch_mod.run_task_in_isolation(
        _task(TASK_ID), str(repo), {}, args, execute=agent,
    ))
    return repo, drivers, run, master_before


@pytest.mark.parametrize("kind", PLANTS)
@pytest.mark.parametrize(
    ("outcome", "commit"),
    [
        ("tests_passed", True),    # success with commits
        ("tests_passed", False),   # success, nothing committed
        ("tests_failed", True),    # failure
        ("early_terminated", False),  # early termination
    ],
    ids=["success", "success-no-commits", "failure", "early-termination"],
)
def test_isolated_run_never_runs_an_agent_driver_after_the_agent(
    tmp_path: Path, kind: str, outcome: str, commit: bool,
) -> None:
    repo, drivers, run, master_before = _isolated_run(tmp_path, kind, outcome, commit=commit)

    assert not drivers.ran(), (
        f"the agent's {kind} driver ran in the orchestrator ({run.outcome})"
    )
    # The plant is a repository hazard, so nothing is merged; the agent's
    # uncommitted work is kept as a stash on its branch.
    assert run.merged_sha is None
    assert _master(repo) == master_before
    _assert_work_stashed(repo)


# --- R3155-02: the work tree is used through its descriptor -------------------


def _swapped_in_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "swapped-in"
    tree.mkdir()
    (tree / "README.md").write_text("seed\n")
    (tree / "ONLY_IN_THE_SWAPPED_TREE.txt").write_text("not the pinned tree\n")
    return tree


@pytest.mark.parametrize("flavour", ["sync", "async"])
def test_a_swap_as_git_starts_is_not_followed_and_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flavour: str,
) -> None:
    """The review's probe P2: the checkout swapped right after the pin
    lookup passed. git must still work in the pinned tree, and the swap
    (still in place) must be raised once git is done."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    swapped_in = _swapped_in_tree(tmp_path)
    outputs: list[str] = []
    swapped: list[Path] = []

    def swap() -> None:
        if not swapped:
            swapped.append(real.with_name("real.moved"))
            os.rename(real, swapped[0])
            os.rename(swapped_in, real)

    real_subprocess_run = subprocess.run
    real_exec = asyncio.create_subprocess_exec

    def run_after_swap(argv, *args, **kwargs):
        if "status" in argv:
            swap()
        result = real_subprocess_run(argv, *args, **kwargs)
        if "status" in argv:
            outputs.append(result.stdout)
        return result

    async def exec_after_swap(*argv, **kwargs):
        if "status" in argv:
            swap()
        proc = await real_exec(*argv, **kwargs)
        if "status" in argv:
            communicate = proc.communicate

            async def recording(input=None):
                out, err = await communicate(input)
                outputs.append(out.decode())
                return out, err

            proc.communicate = recording
        return proc

    pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), None))
    try:
        with git_repositories_pinned(*pins.repositories):
            if flavour == "sync":
                monkeypatch.setattr(subprocess, "run", run_after_swap)
                with pytest.raises(PinnedRepositoryError, match="no longer the work tree"):
                    git_run(["status", "--porcelain"], str(real))
            else:
                monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_after_swap)
                with pytest.raises(PinnedRepositoryError, match="no longer the work tree"):
                    _run(git_run_async(["status", "--porcelain"], str(real)))
    finally:
        monkeypatch.undo()
        pins.close()

    assert swapped, "the swap was not injected"
    assert outputs, "git did not run"
    assert "ONLY_IN_THE_SWAPPED_TREE" not in outputs[0], outputs[0]


def test_a_swap_while_the_pin_is_made_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin-time window of R3155-02: the checkout swapped right after it
    was looked up for the pin. The pin must not record the swapped-in
    directory as the pinned work tree."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    swapped_in = _swapped_in_tree(tmp_path)
    swapped: list[bool] = []

    def swap_once() -> None:
        if not swapped:
            swapped.append(True)
            os.rename(real, real.with_name("real.moved"))
            os.rename(swapped_in, real)

    real_realpath = os.path.realpath
    real_open = os.open

    def realpath_then_swap(path, *args, **kwargs):
        result = real_realpath(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(real):
            swap_once()
        return result

    def open_then_swap(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if (
            isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(real)
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            swap_once()
        return fd

    monkeypatch.setattr(os.path, "realpath", realpath_then_swap)
    monkeypatch.setattr(os, "open", open_then_swap)
    try:
        with pytest.raises(PinnedRepositoryError, match="not in the work tree"):
            pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), None))
            pins.close()
    finally:
        monkeypatch.undo()
    assert swapped, "the swap was not injected"


# --- R3155-03: a swap during generated-file resolution trips the guard --------


def test_resolution_reraises_a_pinned_repository_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def swapped_during_resolution(*args, **kwargs):
        raise PinnedRepositoryError("checkout swapped after the pin")

    monkeypatch.setattr(dispatch_mod, "resolve_generated_conflicts", swapped_during_resolution)
    with pytest.raises(PinnedRepositoryError, match="swapped"):
        _run(dispatch_mod._resolve_generated_conflict(
            str(tmp_path), TASK_ID, TASK_BRANCH, "a" * 40, "b" * 40, "a" * 40, "master",
        ))


def test_swap_undone_after_resolution_still_trips_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's probe P1: the checkout swapped only while the conflict
    is being resolved, and restored before the abort. On the old code this
    ended as an ordinary merge_failed with no alarm."""
    real, _worktree, _task_sha = _conflicting_project(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    master_before = _master(real)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    swaps: list[bool] = []

    async def swap_during_resolution(project_dir, **kwargs):
        moved = Path(project_dir).with_name("real.moved")
        os.rename(project_dir, moved)
        os.symlink(elsewhere, project_dir)
        swaps.append(True)
        try:
            await git_run_async(["status", "--porcelain"], project_dir)
        finally:
            os.unlink(project_dir)
            os.rename(moved, project_dir)
        raise AssertionError("the swapped checkout was used")  # pragma: no cover

    monkeypatch.setattr(dispatch_mod, "resolve_generated_conflicts", swap_during_resolution)

    status = _gate(real, guard)

    assert swaps, "generated-file resolution was not reached"
    assert status == "blocked"
    assert guard.tripped, guard.alert
    assert "swapped" in guard.alert, guard.alert
    assert not (real / ".git" / "MERGE_HEAD").exists(), "the merge was not aborted"
    assert _master(real) == master_before
