"""Task #3155 — follow-ups F2 and F3 of the independent review of task #3151.

* F2 (R3151-01): the merge-path pins were looked up by the realpath of a git
  call's ``cwd``, and a ``cwd`` that matched no pin ran unpinned. Swapping
  the main checkout (or the task worktree) for a symlink to the agent's clone
  therefore sent the merge through a fresh discovery into the clone, and the
  clone's filter driver ran inside the orchestrator. Pins are now keyed by
  the path the merge passes, the directory there must still be the pinned
  one, and inside a pinned block anything else is refused and trips the
  guard.
* F3 (R3151-02): a FIFO named ``commondir`` hung the pin (a blocking open on
  the event loop) and a directory under that name raised an uncaught
  ``IsADirectoryError``. Both now block the merge cleanly within a second.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
from equipa.git_ops import (
    PinnedRepositoryError,
    git_repositories_pinned,
    git_run,
    git_run_async,
)
from equipa.merge_integrity import DefaultBranchGuard

from test_dispatch_modes_gated_3112 import (
    _commit_files,
    _git,
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
)
from test_merge_path_pinned_3151 import _gate, _marking_filter
from test_repository_identity_3146 import (
    TASK_BRANCH,
    _clone_with_decoy_branch,
    _master_in,
    _real_with_task_branch,
)

# What a refused pin lookup says (F2); every swap below must produce it.
SWAPPED = "no longer the work tree pinned"
NOT_PINNED = "not a work tree pinned"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(params=["proc-fd", "no-proc-fd"])
def pin_mode(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Descriptor pins, and the realpath pins of a system without
    ``/proc/self/fd``; both must refuse the same swaps."""
    if request.param == "no-proc-fd":
        monkeypatch.setattr(git_ops_mod, "_FD_DIRECTORY", str(tmp_path / "no-proc-fd"))
    return request.param


def _swap_directory_for_symlink(directory: Path, target: Path) -> Path:
    """The agent's swap: the whole work tree renamed away, a symlink to its
    own checkout put at the same path. Returns where the original went."""
    moved = directory.with_name(directory.name + ".moved")
    os.rename(directory, moved)
    os.symlink(target, directory)
    return moved


# --- F2: the work tree itself swapped at merge time -------------------------


def test_main_checkout_swapped_for_a_symlink_at_the_merge_trips_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_mode: str,
) -> None:
    """The review's probe w1. The task branch adds ``*.txt filter=x``; only the
    agent's clone defines ``filter.x``. Right at ``git merge`` the main
    checkout is renamed away and a symlink to the clone takes its path. On
    the task #3151 code the realpath lookup missed, git discovered the
    clone, merged there and ran the clone's filter in the orchestrator. Now
    git is never started: the merge is blocked and the guard trips."""
    real = _init_repo(tmp_path / "real")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(
        real, {".gitattributes": "*.txt filter=x\n", "notes.txt": "hello\n"}, "task",
    )
    _git(real, "checkout", "-q", "master")
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    smudge, marker = _marking_filter(tmp_path)
    _git(other, "config", "filter.x.smudge", str(smudge))
    real_master, other_master = _master(real), _master(other)

    real_git = dispatch_mod.git_run_async
    moved: list[Path] = []

    async def swap_at_the_merge(args, cwd, *rest, **kwargs):
        if list(args[:1]) == ["merge"] and "--abort" not in args and not moved:
            moved.append(_swap_directory_for_symlink(real, other))
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(dispatch_mod, "git_run_async", swap_at_the_merge)

    status = _gate(real, guard)

    assert moved, "the swap was not injected"
    assert not marker.exists(), "the clone's filter driver ran"
    assert _master(other) == other_master, "the merge went into the clone"
    assert _master_in(moved[0] / ".git") == real_master, "git ran after the swap"
    assert status == "blocked"
    assert guard.tripped and SWAPPED in guard.alert, guard.alert


def _conflicting_project(tmp_path: Path) -> tuple[Path, Path, str]:
    """A project whose task branch conflicts with master, so the merge falls
    back to the rebase in the task worktree. master (the rebase target)
    carries ``*.txt filter=x`` and a ``.txt`` file the task branch lacks, so
    checking it out in a repository that defines ``filter.x`` runs it."""
    real = _init_repo(tmp_path / "real")
    _commit_files(real, {".gitattributes": "*.txt filter=x\n", "conflict.py": "BASE\n"}, "base")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(real, {"conflict.py": "TASK\n"}, "task change")
    _git(real, "checkout", "-q", "master")
    _commit_files(real, {"conflict.py": "MAIN\n", "notes.txt": "hello\n"}, "main change")
    task_sha = _git(real, "rev-parse", TASK_BRANCH)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    return real, worktree, task_sha


def test_task_worktree_swapped_for_a_symlink_in_the_rebase_trips_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_mode: str,
) -> None:
    """The review's probe w3. The merge conflicts and the fallback rebases in
    the task worktree. At its first git call there (``branch
    --show-current``) the worktree directory is swapped for a symlink to a
    worktree of the agent's clone, on the same branch and commit. On the
    task #3151 code the rebase ran in the clone, the clone's filter ran and
    the run ended ``merge_failed`` with no alarm. Now no git runs in the
    swapped directory, and the guard trips."""
    real, worktree, task_sha = _conflicting_project(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(real), str(other)], check=True, capture_output=True)
    _git(other, "branch", TASK_BRANCH, f"origin/{TASK_BRANCH}")
    other_worktree = tmp_path / "other-wt"
    _git(other, "worktree", "add", "-q", str(other_worktree), TASK_BRANCH)
    # Defined only now, so the checkout above did not run it.
    smudge, marker = _marking_filter(tmp_path)
    _git(other, "config", "filter.x.smudge", str(smudge))
    real_master = _master(real)
    worktree_root = os.path.realpath(worktree)

    real_git = dispatch_mod.git_run_async
    moved: list[Path] = []
    calls: list[list[str]] = []

    async def swap_in_the_worktree(args, cwd, *rest, **kwargs):
        calls.append(list(args))
        # Only the rebase fallback's first call: earlier calls in the
        # worktree (the pin's own admin-dir lookup) already refuse a swap.
        if (
            list(args) == ["branch", "--show-current"]
            and os.fspath(cwd) == worktree_root and not moved
        ):
            moved.append(_swap_directory_for_symlink(worktree, other_worktree))
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(dispatch_mod, "git_run_async", swap_in_the_worktree)

    status = _gate(real, guard, worktree)

    assert ["merge", "--abort"] in calls, "the merge did not conflict"
    assert moved, "the swap was not injected"
    assert not any(args[:1] == ["rebase"] for args in calls[calls.index(["merge", "--abort"]):])
    assert not marker.exists(), "the clone's filter driver ran"
    assert _git(other_worktree, "rev-parse", "HEAD") == task_sha
    assert _master(other) == real_master
    assert _master_in(real / ".git") == real_master
    assert status == "blocked"
    assert guard.tripped and SWAPPED in guard.alert, guard.alert


# --- F2: the lookup itself ---------------------------------------------------


def _pinned_project(tmp_path: Path) -> tuple[Path, DefaultBranchGuard, dispatch_mod._MergePins]:
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), None))
    return real, guard, pins


def test_a_cwd_that_names_no_pinned_work_tree_is_refused(
    tmp_path: Path, pin_mode: str,
) -> None:
    """Inside a pinned block git never discovers a repository: a call in any
    other repository is refused, sync and async (it used to run there)."""
    real, _guard, pins = _pinned_project(tmp_path)
    stranger = _init_repo(tmp_path / "stranger")

    async def call_elsewhere() -> None:
        with git_repositories_pinned(*pins.repositories):
            with pytest.raises(PinnedRepositoryError, match=NOT_PINNED):
                await git_run_async(["rev-parse", "HEAD"], stranger)
            with pytest.raises(PinnedRepositoryError, match=NOT_PINNED):
                git_run(["rev-parse", "HEAD"], stranger)
            # The pinned work tree itself still works.
            pinned = await git_run_async(["rev-parse", "HEAD"], real)
            assert pinned.returncode == 0 and pinned.stdout.strip() == _master(real)

    try:
        _run(call_elsewhere())
    finally:
        pins.close()
    # Outside the block nothing is pinned: the control.
    assert git_run(["rev-parse", "HEAD"], stranger).returncode == 0


def test_an_alias_of_the_pinned_work_tree_is_not_its_path(
    tmp_path: Path, pin_mode: str,
) -> None:
    """Pins are keyed by the path the merge passes, not by a realpath: a
    symlink that leads to the pinned work tree today names no pin."""
    real, _guard, pins = _pinned_project(tmp_path)
    alias = tmp_path / "alias"
    os.symlink(real, alias)

    async def call_through_the_alias() -> None:
        with git_repositories_pinned(*pins.repositories):
            await git_run_async(["rev-parse", "HEAD"], alias)

    try:
        with pytest.raises(PinnedRepositoryError, match=NOT_PINNED):
            _run(call_through_the_alias())
    finally:
        pins.close()


def test_a_real_directory_moved_into_the_pinned_path_is_refused(
    tmp_path: Path, pin_mode: str,
) -> None:
    """A swap with no symlink anywhere: the checkout is renamed away and the
    clone renamed into its path, so even the realpath is unchanged. The
    work tree's device and inode were pinned, so git does not run."""
    real, _guard, pins = _pinned_project(tmp_path)
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    os.rename(real, tmp_path / "real.moved")
    os.rename(other, real)

    async def status_after_the_move() -> None:
        with git_repositories_pinned(*pins.repositories):
            await git_run_async(["status", "--porcelain"], real)

    try:
        with pytest.raises(PinnedRepositoryError, match=SWAPPED):
            _run(status_after_the_move())
    finally:
        pins.close()


def test_pins_record_the_path_the_merge_passes_and_its_inode(tmp_path: Path) -> None:
    real, _guard, pins = _pinned_project(tmp_path)
    try:
        (pin,) = pins.repositories
        assert pin.key == os.path.abspath(real)
        info = os.stat(real)
        assert pin.work_tree_id == (info.st_dev, info.st_ino)
        assert pin.work_tree == os.path.realpath(real)
    finally:
        pins.close()


# --- F3: commondir that is not a regular file -------------------------------


def _plant(path: Path, kind: str) -> None:
    if path.exists():
        path.unlink()
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)


def _release_blocked_reader(fifo: Path) -> None:
    """Open the FIFO's write end so a reader stuck in open() returns."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # no reader is waiting


def _pin_in_a_thread(guard, real: Path, worktree: Path | None) -> tuple[object, float, bool]:
    """Run the pin off the test's thread, so a blocking open cannot hang the
    suite; returns (result or exception, seconds, finished within 1 s)."""
    outcome: list[object] = []

    def pin() -> None:
        try:
            pins = _run(dispatch_mod._pin_merge_repositories(
                guard, str(real), str(worktree) if worktree is not None else None,
            ))
            pins.close()
            outcome.append(pins)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome.append(exc)

    worker = threading.Thread(target=pin, daemon=True)
    start = time.monotonic()
    worker.start()
    worker.join(timeout=1.0)
    elapsed = time.monotonic() - start
    finished = not worker.is_alive()
    return (outcome[0] if outcome else None), elapsed, finished


@pytest.mark.parametrize("kind", ["directory", "fifo"])
@pytest.mark.parametrize("where", ["main git dir", "worktree git dir"])
def test_commondir_that_is_not_a_regular_file_blocks_within_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin_mode: str,
    kind: str, where: str,
) -> None:
    """The review's probes: a FIFO named ``commondir`` used to block the open
    forever (on the event loop), and a directory raised IsADirectoryError,
    which no caller catches. The plant lands after the last git call that
    reads it (git itself would refuse it), as an agent racing the merge
    would do it. Both now raise PinnedRepositoryError at once."""
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    if where == "main git dir":
        planted = real / ".git" / "commondir"
        _plant(planted, kind)
    else:
        planted = Path(_git(worktree, "rev-parse", "--absolute-git-dir")) / "commondir"
        real_admin_name = dispatch_mod._worktree_admin_name

        async def plant_after_the_git_call(*args, **kwargs):
            name = await real_admin_name(*args, **kwargs)
            _plant(planted, kind)
            return name

        monkeypatch.setattr(dispatch_mod, "_worktree_admin_name", plant_after_the_git_call)

    try:
        result, elapsed, finished = _pin_in_a_thread(guard, real, worktree)
    finally:
        if kind == "fifo":
            _release_blocked_reader(planted)

    assert finished, f"the pin still blocked after {elapsed:.1f} s"
    assert elapsed < 1.0
    assert isinstance(result, PinnedRepositoryError), repr(result)
    assert "not a regular file" in str(result)


def test_directory_commondir_at_the_merge_blocks_it_without_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a directory named ``commondir`` planted in the task
    worktree's git dir right before the pin used to escape
    ``_gated_merge_task`` as IsADirectoryError and end the dispatch run.
    Now the task is blocked and the guard trips."""
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    planted = Path(_git(worktree, "rev-parse", "--absolute-git-dir")) / "commondir"
    real_admin_name = dispatch_mod._worktree_admin_name

    async def plant_after_the_git_call(*args, **kwargs):
        name = await real_admin_name(*args, **kwargs)
        _plant(planted, "directory")
        return name

    monkeypatch.setattr(dispatch_mod, "_worktree_admin_name", plant_after_the_git_call)
    master_before = _master(real)

    status = _gate(real, guard, worktree)

    assert planted.is_dir(), "the plant was not injected"
    assert status == "blocked"
    assert guard.tripped and "not a regular file" in guard.alert, guard.alert
    assert _master(real) == master_before


def test_overlong_commondir_is_refused_before_it_is_read(tmp_path: Path) -> None:
    """A regular file longer than any path git writes is refused by its size,
    through the descriptor and by path, and the real one still passes."""
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    admin = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    common = os.path.realpath(real / ".git")
    original = (admin / "commondir").read_text(encoding="utf-8")
    admin_fd = os.open(admin, os.O_RDONLY | os.O_DIRECTORY)
    try:
        dispatch_mod._check_commondir_file(admin_fd, str(admin), common, linked=True)
        (admin / "commondir").write_text(original + "x" * 8192, encoding="utf-8")
        for git_dir_fd in (admin_fd, None):
            with pytest.raises(PinnedRepositoryError, match="longer than any"):
                dispatch_mod._check_commondir_file(
                    git_dir_fd, str(admin), common, linked=True,
                )
    finally:
        os.close(admin_fd)
