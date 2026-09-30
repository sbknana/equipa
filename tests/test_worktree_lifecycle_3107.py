"""Regression tests for task #3107 (review findings dispatch-01/02/12/13).

The worktree lifecycle must never run a task in the main checkout and never
let a task's commits land on the default branch:

* dispatch-01: a stale ``forge-task-<id>`` branch used to route the task to
  the shared main checkout, where the agent committed straight onto master
  and the merge gate never saw it. The task must be refused instead.
* dispatch-02/12: ``cleanup_failed_attempt`` used to run ``git checkout
  master`` and ``git branch -D forge-task-<id>`` inside the task worktree.
  With the main checkout off the default branch, every retry then committed
  directly to master. The worktree must be reset in place and stay on its
  branch, and every git failure must be loud.
* dispatch-13: a leftover worktree was force-removed without stashing its
  uncommitted work.

Every test drives real git on a temp repo. Only the agent itself
(``run_dev_test_loop``), the DB status writers and the merge gate are
replaced; the worktree creation, the per-attempt checks and the cleanup run
for real.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from equipa import dispatch as dispatch_mod


# --- git helpers -------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, text=True, capture_output=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "test@forgeborn.local")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")


def _commit_file(cwd: Path, name: str, message: str) -> None:
    (cwd / name).write_text(f"{message}\n", encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", ref)


def _branch_exists(repo: Path, name: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=str(repo), capture_output=True,
    ).returncode == 0


def _checked_out_branch(cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(cwd), text=True, capture_output=True,
    )
    return result.stdout.strip() or None


# --- fakes -------------------------------------------------------------------


class FakeAgent:
    """Stands in for ``run_dev_test_loop``.

    Records the directory and branch each attempt ran on, commits one file
    per attempt (like a real developer agent), optionally runs
    ``after_commit`` to misbehave, and returns the scripted outcome.
    """

    def __init__(self, outcomes: list[str], after_commit=None) -> None:
        self.outcomes = outcomes
        self.after_commit = after_commit
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, task, project_dir, project_context, args, output=None):
        attempt = len(self.calls) + 1
        workdir = Path(project_dir)
        self.calls.append((str(project_dir), _checked_out_branch(workdir)))
        _commit_file(
            workdir,
            f"agent-{task['id']}-attempt-{attempt}.txt",
            f"agent work {task['id']} attempt {attempt}",
        )
        if self.after_commit is not None:
            self.after_commit(workdir, attempt)
        return {"cost": 0.0, "duration": 0.0}, 1, self.outcomes[attempt - 1]


class _NoCloseConn:
    """In-memory sqlite wrapper whose close() is a no-op."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self) -> None:
        pass


def _memory_tasks_db(task_ids: list[int]) -> _NoCloseConn:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT, description TEXT)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, status, description) VALUES (?, 'in_progress', 'd')",
        [(task_id,) for task_id in task_ids],
    )
    conn.commit()
    return _NoCloseConn(conn)


def _task(task_id: int) -> dict:
    return {
        "id": task_id, "project_id": 1, "title": f"task {task_id}",
        "description": "d", "role": "developer",
    }


def _run_parallel(
    repo: Path,
    task_ids: list[int],
    agent: FakeAgent,
    *,
    max_retries: int = 0,
) -> list[tuple[int, str]]:
    """Run ``run_parallel_tasks`` on ``repo``; return the status updates."""
    tasks = [_task(task_id) for task_id in task_ids]
    args = SimpleNamespace(
        yes=True,
        max_concurrent=1,
        use_flow=False,
        security_review=False,
        dispatch_config={
            "features": {"autoresearch": max_retries > 0},
            "autoresearch_max_retries": max_retries,
        },
    )
    status_updates: list[tuple[int, str]] = []
    db = _memory_tasks_db(task_ids)
    with patch.object(dispatch_mod, "fetch_tasks_by_ids", return_value=tasks), \
            patch.object(dispatch_mod, "resolve_project_dir", return_value=str(repo)), \
            patch.object(dispatch_mod, "fetch_project_context", return_value={}), \
            patch.object(dispatch_mod, "run_dev_test_loop", new=agent), \
            patch.object(
                dispatch_mod, "fetch_task",
                side_effect=lambda task_id: _task(task_id),
            ), \
            patch.object(dispatch_mod, "get_db_connection", lambda write=False: db), \
            patch.object(
                dispatch_mod, "update_task_status",
                side_effect=lambda task_id, outcome, output=None:
                    status_updates.append((task_id, outcome)),
            ), \
            patch.object(dispatch_mod, "record_agent_run", MagicMock()), \
            patch.object(dispatch_mod, "get_role_model", return_value="test-model"), \
            patch.object(dispatch_mod, "get_role_turns", return_value=10), \
            patch.object(
                dispatch_mod, "run_security_review",
                AsyncMock(side_effect=AssertionError("reviewer must not run")),
            ), \
            patch.object(
                dispatch_mod, "_gated_merge_task",
                AsyncMock(return_value="merge-skipped"),
            ), \
            patch("equipa.scaffold.ensure_scaffold", return_value=False):
        asyncio.run(dispatch_mod.run_parallel_tasks(task_ids, args))
    return status_updates


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    _init_repo(path)
    return path


# --- dispatch-01: stale branch -> refuse, never the shared checkout ---------


def test_stale_branch_task_is_refused_and_default_branch_untouched(repo: Path) -> None:
    """Reviewer probe P2b: re-dispatching a task whose branch survived a
    crashed run must not run the agent in the main checkout."""
    _git(repo, "checkout", "-q", "-b", "forge-task-5")
    _commit_file(repo, "crashed.txt", "work from the crashed run of task 5")
    _git(repo, "checkout", "-q", "master")
    stale_sha = _sha(repo, "forge-task-5")
    master_before = _sha(repo, "master")
    agent = FakeAgent(["tests_passed", "tests_passed"])

    status_updates = _run_parallel(repo, [5, 6], agent)

    # Task 5 never ran; task 6 still ran, isolated in its own worktree.
    assert agent.calls == [
        (str(repo / ".forge-worktrees" / "task-6"), "forge-task-6"),
    ]
    assert _sha(repo, "master") == master_before, "agent work reached master"
    assert _sha(repo, "forge-task-5") == stale_sha, "stale branch was not preserved"
    assert (5, "worktree_refused") in status_updates
    assert (6, "tests_passed") in status_updates


def test_unregistered_leftover_directory_refuses_task(repo: Path) -> None:
    """A leftover task directory that is not a worktree can be neither
    stashed nor removed safely: keep it and refuse the task (dispatch-13),
    never fall back to the main checkout (dispatch-01)."""
    leftover = repo / ".forge-worktrees" / "task-10"
    leftover.mkdir(parents=True)
    (leftover / "notes.txt").write_text("uncommitted notes\n", encoding="utf-8")
    master_before = _sha(repo, "master")
    agent = FakeAgent(["tests_passed"])

    status_updates = _run_parallel(repo, [10], agent)

    assert agent.calls == []
    assert _sha(repo, "master") == master_before
    assert (leftover / "notes.txt").read_text(encoding="utf-8") == "uncommitted notes\n"
    assert status_updates == [(10, "worktree_refused")]


# --- dispatch-02/12: cleanup stays inside the worktree, on its branch --------


def test_retry_after_cleanup_stays_on_task_branch_when_master_is_free(
    repo: Path,
) -> None:
    """Reviewer probe P3: with the main checkout off the default branch,
    the failed-attempt cleanup must not move the worktree onto master."""
    _git(repo, "checkout", "-q", "-b", "operator-wip")
    master_before = _sha(repo, "master")
    worktree = str(repo / ".forge-worktrees" / "task-7")
    agent = FakeAgent(["cycles_exhausted", "cycles_exhausted"])

    status_updates = _run_parallel(repo, [7], agent, max_retries=1)

    assert agent.calls == [
        (worktree, "forge-task-7"),
        (worktree, "forge-task-7"),
    ], "the retry did not run on forge-task-7 in the task worktree"
    assert _sha(repo, "master") == master_before, "a retry committed to master"
    history = _git(repo, "log", "--format=%s", "forge-task-7")
    assert "agent work 7 attempt 2" in history
    assert "agent work 7 attempt 1" not in history, "failed attempt was not reset"
    assert _checked_out_branch(repo) == "operator-wip"
    assert status_updates == [(7, "cycles_exhausted")]


def test_worktree_left_off_its_branch_blocks_the_task(repo: Path) -> None:
    """An attempt that leaves the worktree off forge-task-<id> must stop the
    task: no reset, no retry, the committed work preserved on the branch."""
    _git(repo, "checkout", "-q", "-b", "operator-wip")
    master_before = _sha(repo, "master")

    def wander_off(workdir: Path, attempt: int) -> None:
        _git(workdir, "checkout", "-q", "--detach")

    agent = FakeAgent(["cycles_exhausted", "tests_passed"], after_commit=wander_off)

    status_updates = _run_parallel(repo, [8], agent, max_retries=3)

    assert len(agent.calls) == 1, "a retry ran after the worktree left its branch"
    assert _sha(repo, "master") == master_before
    assert "agent work 8 attempt 1" in _git(repo, "log", "--format=%s", "forge-task-8")
    assert status_updates == [(8, "attempt_cleanup_failed")]


def test_attempt_refused_when_worktree_is_not_on_task_branch(repo: Path) -> None:
    """Requirement 3: the branch is asserted before EVERY attempt, the first
    one included."""
    worktree = repo / ".forge-worktrees" / "task-9"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-9", str(worktree))
    _git(worktree, "checkout", "-q", "--detach")
    agent = FakeAgent(["tests_passed"])

    with patch.object(dispatch_mod, "run_dev_test_loop", new=agent):
        _, _, outcome, _, _, _ = asyncio.run(
            dispatch_mod.run_dev_test_loop_with_autoresearch(
                _task(9), str(worktree), {}, SimpleNamespace(), {},
                output=[], task_branch="forge-task-9",
            )
        )

    assert outcome == "worktree_branch_mismatch"
    assert agent.calls == []


def test_cleanup_on_worktree_resets_to_fork_point_without_base_sha(
    repo: Path, monkeypatch,
) -> None:
    """Direct call without a recorded base (the old call signature): the
    worktree is reset to its fork point from the trusted default branch."""
    _git(repo, "checkout", "-q", "-b", "operator-wip")
    master_before = _sha(repo, "master")
    worktree = repo / ".forge-worktrees" / "task-11"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-11", str(worktree), "master")
    _commit_file(worktree, "failed.txt", "failed attempt")
    (worktree / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    monkeypatch.setattr(dispatch_mod, "get_db_connection",
                        lambda write=False: _memory_tasks_db([11]))

    asyncio.run(dispatch_mod.cleanup_failed_attempt(11, str(worktree), [], output=[]))

    assert _checked_out_branch(worktree) == "forge-task-11"
    assert _sha(repo, "forge-task-11") == master_before
    assert _sha(repo, "master") == master_before
    assert not (worktree / "failed.txt").exists()
    assert not (worktree / "scratch.txt").exists()


# --- dispatch-12: main-checkout cleanup is trusted and fails loud ------------


def test_main_checkout_cleanup_fails_loud_and_keeps_status(
    repo: Path, monkeypatch,
) -> None:
    """A git step that fails must raise before the task is reset to todo;
    the old code logged "Cleaned up branch" and retried anyway."""
    from equipa.dispatch import AttemptCleanupError

    _git(repo, "checkout", "-q", "-b", "forge-task-12")
    _commit_file(repo, "README.md", "task 12 edits the readme")
    (repo / "README.md").write_text("uncommitted edit\n", encoding="utf-8")
    db_opened = MagicMock()
    monkeypatch.setattr(dispatch_mod, "get_db_connection", db_opened)

    with pytest.raises(AttemptCleanupError):
        asyncio.run(dispatch_mod.cleanup_failed_attempt(12, str(repo), [], output=[]))

    db_opened.assert_not_called()
    assert _checked_out_branch(repo) == "forge-task-12"
    assert _branch_exists(repo, "forge-task-12")


def test_main_checkout_cleanup_refuses_branch_held_by_a_worktree(
    repo: Path, monkeypatch,
) -> None:
    """Never delete the task branch while its worktree exists."""
    from equipa.dispatch import AttemptCleanupError

    worktree = repo / ".forge-worktrees" / "task-13"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-13", str(worktree))
    _commit_file(worktree, "work.txt", "task 13 work")
    branch_sha = _sha(repo, "forge-task-13")
    monkeypatch.setattr(dispatch_mod, "get_db_connection", MagicMock())

    with pytest.raises(AttemptCleanupError, match="checked out"):
        asyncio.run(dispatch_mod.cleanup_failed_attempt(13, str(repo), [], output=[]))

    assert _sha(repo, "forge-task-13") == branch_sha
    assert _checked_out_branch(worktree) == "forge-task-13"


def test_main_checkout_cleanup_refuses_ambiguous_default_branch(
    repo: Path, monkeypatch,
) -> None:
    """With both main and master present there is no trusted default; the
    old code guessed main (an agent can create either)."""
    from equipa.dispatch import AttemptCleanupError

    _git(repo, "branch", "main")
    _git(repo, "checkout", "-q", "-b", "forge-task-14")
    _commit_file(repo, "work.txt", "task 14 work")
    monkeypatch.setattr(dispatch_mod, "get_db_connection", MagicMock())

    with pytest.raises(AttemptCleanupError, match="trusted default branch"):
        asyncio.run(dispatch_mod.cleanup_failed_attempt(14, str(repo), [], output=[]))

    assert _checked_out_branch(repo) == "forge-task-14"
    assert _branch_exists(repo, "forge-task-14")


def test_main_checkout_cleanup_deletes_task_branch(repo: Path, monkeypatch) -> None:
    """Regression guard for the single-task path: leave the task branch for
    the trusted default branch, delete it, reset the task to todo."""
    _git(repo, "checkout", "-q", "-b", "forge-task-15")
    _commit_file(repo, "work.txt", "task 15 work")
    db = _memory_tasks_db([15])
    monkeypatch.setattr(dispatch_mod, "get_db_connection", lambda write=False: db)

    asyncio.run(dispatch_mod.cleanup_failed_attempt(15, str(repo), [], output=[]))

    assert _checked_out_branch(repo) == "master"
    assert not _branch_exists(repo, "forge-task-15")
    assert db.execute("SELECT status FROM tasks WHERE id = 15").fetchone()[0] == "todo"


# --- dispatch-13: leftover worktree is stashed before removal ---------------


def test_leftover_worktree_uncommitted_work_is_stashed_before_removal(
    repo: Path,
) -> None:
    worktree_base = repo / ".forge-worktrees"
    leftover = worktree_base / "task-16"
    _git(repo, "worktree", "add", "-q", "--detach", str(leftover))
    (leftover / "README.md").write_text("tracked edit\n", encoding="utf-8")
    (leftover / "new_file.txt").write_text("untracked work\n", encoding="utf-8")

    worktree_dirs = asyncio.run(
        dispatch_mod._create_isolation_worktrees(
            [{"id": 16}], str(repo), worktree_base,
        )
    )

    assert worktree_dirs == {16: str(leftover)}
    assert _checked_out_branch(leftover) == "forge-task-16"
    stashes = _git(repo, "stash", "list", "--format=%H %s").splitlines()
    tagged = [line.split()[0] for line in stashes if "task-16" in line]
    assert len(tagged) == 1, f"leftover work was not stashed: {stashes!r}"
    stash_sha = tagged[0]
    assert _git(repo, "show", f"{stash_sha}:README.md") == "tracked edit"
    assert _git(repo, "show", f"{stash_sha}^3:new_file.txt") == "untracked work"
