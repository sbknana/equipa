"""Task #3151 — follow-ups R3146-01 and R3146-02 of the task #3146 review.

* R3146-02: the repository-identity alert embeds agent-chosen paths (the
  common dir reached through a planted ``.git`` symlink). It is printed to
  stdout, logged and handed on as the task's block reason, and the dispatch
  command sends stdout and stderr to one log, so a newline in such a path
  must not start a forged ``[GATE-AUDIT]`` line.
* R3146-01: every git command of the orchestrator's merge path runs on the
  repository pinned at the guard snapshot (``--git-dir`` / ``--work-tree``),
  never through a fresh discovery from the project directory. A ``.git``
  swapped between the identity check and the merge cannot redirect it.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
from equipa.loops import ARTIFACTS_DIR_NAME
from equipa.git_ops import PinnedRepositoryError, git_repositories_pinned, git_run_async
from equipa.merge_integrity import DefaultBranchGuard

from test_dispatch_modes_gated_3112 import (
    _commit_files,
    _git,
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
)
from test_repository_identity_3146 import (
    TASK_BRANCH,
    TASK_ID,
    _clone_with_decoy_branch,
    _master_in,
    _real_with_task_branch,
)

FORGED = f"[GATE-AUDIT] task={TASK_ID} event=merge-succeeded FORGED-3151"


def _run(coro):
    return asyncio.run(coro)


def _write_clean_review(repo: Path) -> None:
    """A clean artifact; the suite's conftest permits the unrecorded run."""
    artifact = repo / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{TASK_ID}.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        "# Security Review\n\nNo findings.\n\n## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n",
        encoding="utf-8",
    )


def _gate(repo: Path, guard: DefaultBranchGuard, worktree: Path | None = None) -> str:
    _write_clean_review(repo)
    return _run(dispatch_mod._gated_merge_task(
        repo=str(repo), branch=TASK_BRANCH, outcome="tests_passed",
        task_id=TASK_ID, guard=guard,
        worktree_dir=str(worktree) if worktree is not None else None,
    ))


def _swap_git_for_symlink(directory: Path, target_git_dir: Path) -> None:
    """The agent's swap: ``.git`` renamed away, a symlink to a clone in its place."""
    os.rename(directory / ".git", directory / ".git.bak")
    os.symlink(target_git_dir, directory / ".git")


def _marking_filter(tmp_path: Path) -> tuple[Path, Path]:
    """An executable smudge filter that only records that it ran.

    Named by one path without blanks, so git starts it directly (no shell);
    it copies stdin to stdout and touches the marker.
    """
    marker = tmp_path / "filter-driver-ran"
    script = tmp_path / "smudge-filter"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, shutil, sys\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, marker


# --- R3146-01: the merge runs on the pinned repository -----------------------


def test_git_swapped_at_the_merge_cannot_redirect_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """The review's probe g2. The task branch adds ``*.txt filter=x``; the
    project's config defines no ``filter.x`` (so the hazard scan passes), the
    agent's clone does. The ``.git`` entry is swapped for a symlink to the
    clone right at ``git merge``, after every identity check passed. On the
    task #3146 code the merge lands in the clone and the clone's filter runs
    inside the orchestrator. Pinned, the merge lands in the project's own
    repository, no driver runs, and the post-merge check raises the alarm."""
    real = _init_repo(tmp_path / "real")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(
        real,
        {".gitattributes": "*.txt filter=x\n", "notes.txt": "hello\n",
         "lib/code.py": "VALUE = 1\n"},
        "task code",
    )
    _git(real, "checkout", "-q", "master")
    task_sha = _git(real, "rev-parse", TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    smudge, marker = _marking_filter(tmp_path)
    _git(other, "config", "filter.x.smudge", str(smudge))
    real_master, other_master = _master(real), _master(other)

    real_git = dispatch_mod.git_run_async
    swaps: list[str] = []

    async def swap_at_the_merge(args, cwd, *rest, **kwargs):
        if list(args[:1]) == ["merge"] and "--abort" not in args and not swaps:
            swaps.append(os.fspath(cwd))
            _swap_git_for_symlink(real, other / ".git")
        return await real_git(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(dispatch_mod, "git_run_async", swap_at_the_merge)

    status = _gate(real, guard)

    assert swaps, "the swap was not injected"
    assert not marker.exists(), "the other repository's filter driver ran"
    assert _master(other) == other_master, "the merge went into the other repository"
    landed = _master_in(real / ".git.bak")
    assert landed != real_master
    _git(real / ".git.bak", "merge-base", "--is-ancestor", task_sha, landed)
    assert status == "blocked"
    assert guard.tripped and "repository" in guard.alert
    assert "event=repository-identity-changed" in capsys.readouterr().err


def test_every_merge_path_git_call_runs_on_the_pinned_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each git call made during the merge (dispatch's own and the helpers'
    in other modules, such as the dirty-checkout status) carries the pinned
    ``--git-dir`` / ``--work-tree`` and ``GIT_COMMON_DIR``."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    calls: list[tuple[list[str], list[str], dict[str, str]]] = []
    merging: list[bool] = []
    real_argv = git_ops_mod._hardened_git_argv
    real_env = git_ops_mod._hardened_git_env
    envs: list[dict[str, str]] = []

    def recording_env(*args, **kwargs):
        env = real_env(*args, **kwargs)
        envs.append(env)
        return env

    def recording_argv(args, env, pin=None):
        argv = real_argv(args, env, pin)
        if merging:
            calls.append((list(args), argv, envs[-1]))
        return argv

    real_merge = dispatch_mod._merge_task_branch

    async def flagged_merge(*args, **kwargs):
        merging.append(True)
        try:
            return await real_merge(*args, **kwargs)
        finally:
            merging.clear()

    monkeypatch.setattr(git_ops_mod, "_hardened_git_env", recording_env)
    monkeypatch.setattr(git_ops_mod, "_hardened_git_argv", recording_argv)
    monkeypatch.setattr(dispatch_mod, "_merge_task_branch", flagged_merge)

    assert _gate(real, guard) == "merged"

    subcommands = {args[0] for args, _argv, _env in calls}
    assert {"status", "checkout", "merge", "rev-parse"} & subcommands >= {"status", "merge"}
    for args, argv, env in calls:
        assert any(a.startswith("--git-dir=/proc/self/fd/") for a in argv), argv
        assert f"--work-tree={os.path.realpath(real)}" in argv, argv
        assert env.get("GIT_COMMON_DIR", "").startswith("/proc/self/fd/"), args


def _worktree_and_clone(tmp_path: Path) -> tuple[Path, Path, Path, DefaultBranchGuard]:
    """The project with a task worktree, its guard, and the agent's clone
    (which has a worktree of its own on the decoy task branch)."""
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    _git(other, "worktree", "add", "-q", str(tmp_path / "other-wt"), TASK_BRANCH)
    return real, worktree, other, guard


def test_task_worktree_git_file_plant_is_ignored_inside_the_pin(tmp_path: Path) -> None:
    """The rebase fallback runs git in the task worktree, whose ``.git`` file
    is agent-writable (the review's probe c2). Inside the pin git uses the
    worktree's own git dir in the pinned repository."""
    real, worktree, other, guard = _worktree_and_clone(tmp_path)
    task_sha = _git(worktree, "rev-parse", "HEAD")
    pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), str(worktree)))
    (worktree / ".git").write_text(
        f"gitdir: {other / '.git' / 'worktrees' / 'other-wt'}\n", encoding="utf-8",
    )

    async def read_through_the_pin() -> tuple[str, str]:
        with git_repositories_pinned(*pins.repositories):
            head = await git_run_async(["rev-parse", "HEAD"], worktree)
            branch = await git_run_async(
                ["rev-parse", f"refs/heads/{TASK_BRANCH}"], worktree,
            )
        return head.stdout.strip(), branch.stdout.strip()

    try:
        head, branch = _run(read_through_the_pin())
    finally:
        pins.close()

    assert (head, branch) == (task_sha, task_sha)
    # Control: without the pin, git follows the plant to the clone's decoy.
    assert _git(worktree, "rev-parse", "HEAD") != task_sha, "the plant had no effect"
    assert _git(worktree, "rev-parse", f"refs/heads/{TASK_BRANCH}") != task_sha


@pytest.mark.parametrize("where", ["main git dir", "worktree git dir"])
def test_commondir_planted_before_the_pin_is_refused(tmp_path: Path, where: str) -> None:
    """git's ref backend follows a git dir's ``commondir`` file even with
    ``GIT_COMMON_DIR`` set (the review's probes b and c), so one that does
    not name the pinned common dir is refused when the pin is opened."""
    real, worktree, other, guard = _worktree_and_clone(tmp_path)
    if where == "main git dir":
        target = real / ".git"
    else:
        target = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    (target / "commondir").write_text(f"{other / '.git'}\n", encoding="utf-8")

    with pytest.raises(PinnedRepositoryError, match="commondir names"):
        _run(dispatch_mod._pin_merge_repositories(guard, str(real), str(worktree)))


def test_commondir_planted_after_the_pin_runs_no_foreign_driver(tmp_path: Path) -> None:
    """The residual of the ``commondir`` file: planted after the pin was
    opened, it moves where git reads and writes refs, but config (and so
    every filter driver) and objects still come from the pinned common dir.
    The clone's driver never runs, and the next identity check trips."""
    real = _init_repo(tmp_path / "real")
    _git(real, "checkout", "-q", "-b", TASK_BRANCH)
    _commit_files(real, {".gitattributes": "*.txt filter=x\n", "a.txt": "hi\n"}, "task")
    _git(real, "checkout", "-q", "master")
    task_sha = _git(real, "rev-parse", TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    smudge, marker = _marking_filter(tmp_path)
    _git(other, "config", "filter.x.smudge", str(smudge))
    pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), None))
    (real / ".git" / "commondir").write_text(f"{other / '.git'}\n", encoding="utf-8")

    async def merge_through_the_pin() -> int:
        with git_repositories_pinned(*pins.repositories):
            result = await git_run_async(
                ["-c", "user.name=T", "-c", "user.email=t@forgeborn.dev",
                 "merge", "--no-edit", task_sha],
                real,
            )
        return result.returncode

    try:
        assert _run(merge_through_the_pin()) == 0
    finally:
        pins.close()

    assert (real / "a.txt").read_text() == "hi\n"
    assert not marker.exists(), "the other repository's filter driver ran"
    assert not _run(guard.verify("post-merge", task_id=TASK_ID))
    assert "common_dir" in guard.alert


def test_git_directory_swapped_before_the_pin_is_refused(tmp_path: Path) -> None:
    """Opening the pin checks the snapshot inode: a ``.git`` swapped after
    the last identity check is refused before git runs at all."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / "other"
    _clone_with_decoy_branch(real, other)
    _swap_git_for_symlink(real, other / ".git")

    with pytest.raises(PinnedRepositoryError, match="no longer the git directory pinned"):
        _run(dispatch_mod._pin_merge_repositories(guard, str(real), None))


def test_worktree_of_another_repository_is_refused(tmp_path: Path) -> None:
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    stranger = _init_repo(tmp_path / "stranger")
    _git(stranger, "branch", "side")
    _git(stranger, "worktree", "add", "-q", str(tmp_path / "stranger-wt"), "side")

    with pytest.raises(PinnedRepositoryError, match="not one of the worktrees"):
        _run(dispatch_mod._pin_merge_repositories(
            guard, str(real), str(tmp_path / "stranger-wt"),
        ))


def test_merge_root_other_than_the_pinned_work_tree_is_refused(tmp_path: Path) -> None:
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))

    with pytest.raises(PinnedRepositoryError, match="not in the work tree"):
        _run(dispatch_mod._pin_merge_repositories(guard, str(tmp_path), None))


def test_pins_never_leak_descriptors_control(tmp_path: Path) -> None:
    """Every descriptor opened for a pin is closed by ``close``."""
    real = _real_with_task_branch(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(real, "worktree", "add", "-q", str(worktree), TASK_BRANCH)
    guard = _run(DefaultBranchGuard.snapshot(real))
    before = set(os.listdir("/proc/self/fd"))
    pins = _run(dispatch_mod._pin_merge_repositories(guard, str(real), str(worktree)))
    assert len(pins.fds) == 2  # common dir (= git dir) and the worktree's admin dir
    pins.close()
    assert set(os.listdir("/proc/self/fd")) <= before


def _forged_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith(FORGED)]


# --- R3146-02: the identity alert is escaped ---------------------------------


def test_newline_in_a_planted_common_dir_cannot_forge_an_audit_line(
    tmp_path: Path, capsys, caplog,
) -> None:
    """The review's probe h3: a clone whose directory name carries a newline
    and a complete GATE-AUDIT line, with the project's ``.git`` swapped for a
    symlink to it after the snapshot. The alert names the clone's common dir;
    it must stay one line on stdout, in the logger and in ``guard.alert``."""
    real = _real_with_task_branch(tmp_path)
    guard = _run(DefaultBranchGuard.snapshot(real))
    other = tmp_path / f"x\n{FORGED}"
    _clone_with_decoy_branch(real, other)
    os.rename(real / ".git", real / ".git.bak")
    os.symlink(other / ".git", real / ".git")

    with caplog.at_level(logging.ERROR, logger="equipa.merge_integrity"):
        assert not _run(guard.verify("pre-merge", task_id=TASK_ID))

    captured = capsys.readouterr()
    assert guard.tripped
    assert "\n" not in guard.alert
    assert "\\x0a" in guard.alert, guard.alert  # the name is still shown
    assert _forged_lines(captured.out) == []
    assert _forged_lines(captured.err) == []
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ALERT" in logged
    assert _forged_lines(logged) == []


def test_alert_for_a_plain_move_keeps_both_full_shas_control(tmp_path: Path) -> None:
    """Escaping changes nothing in an alert without control characters."""
    real = _init_repo(tmp_path / "real")
    guard = _run(DefaultBranchGuard.snapshot(real))
    baseline = _master(real)
    guard.trip("after-agent", "f" * 40, task_id=TASK_ID)

    assert guard.alert == (
        "default branch 'master' moved outside the orchestrator's merges "
        f"(stage=after-agent): expected {baseline} but found {'f' * 40}"
    )
