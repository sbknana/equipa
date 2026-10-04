"""Additional probes for task #3107 (dispatch-01/02/12/13), tester pass.

Complements ``test_worktree_lifecycle_3107.py`` with the paths it does not
reach:

* the retry resets to the SHA the worktree was created on, not to the fork
  point from the default branch (the main checkout can be ahead of it);
* an agent that checks out the default branch inside its worktree must not
  get that branch reset or committed to by the cleanup;
* the branch assertion runs before the RETRY attempt too, not only the first;
* the three new abort outcomes really land as ``blocked`` in TheForge;
* refusals and aborts leave a one-line durable gate-audit record;
* a stale branch whose leftover worktree holds uncommitted work is refused
  without touching (stashing or removing) that worktree;
* clean or empty leftovers are replaced, ``force=True`` still deletes a stale
  branch after logging its commits, and a non-git project still runs.

Every test drives real git on a temp repo; only the agent, the merge gate
and the DB connection are replaced.

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
from equipa.dispatch import AttemptCleanupError


# --- git helpers -------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, text=True, capture_output=True,
    ).stdout.strip()


def _commit_file(cwd: Path, name: str, message: str) -> str:
    (cwd / name).write_text(f"{message}\n", encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", ref)


def _checked_out_branch(cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(cwd), text=True, capture_output=True,
    )
    return result.stdout.strip() or None


def _stash_list(repo: Path) -> list[str]:
    return _git(repo, "stash", "list", "--format=%s").splitlines()


def _registered_worktrees(repo: Path) -> list[str]:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return [
        str(Path(line[len("worktree "):]).resolve())
        for line in listing.splitlines() if line.startswith("worktree ")
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "test@forgeborn.local")
    _git(path, "config", "user.name", "Test")
    _commit_file(path, "README.md", "base")
    return path


@pytest.fixture
def repo_on_wip_ahead_of_master(repo: Path) -> Path:
    """Main checkout on ``operator-wip``, one commit ahead of master.

    The default branch is then free (an agent worktree can check it out)
    and the worktree base differs from the fork point from master.
    """
    _git(repo, "checkout", "-q", "-b", "operator-wip")
    _commit_file(repo, "operator.txt", "operator base")
    return repo


# --- fakes -------------------------------------------------------------------


class FakeAgent:
    """Stands in for ``run_dev_test_loop``: commits once per attempt."""

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
    project_dir: Path,
    task_ids: list[int],
    agent,
    *,
    max_retries: int = 0,
    gate_audit: MagicMock | None = None,
) -> list[tuple[int, str]]:
    """Run ``run_parallel_tasks`` on ``project_dir``; return status updates."""
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
            patch.object(
                dispatch_mod, "resolve_project_dir", return_value=str(project_dir),
            ), \
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
            patch.object(
                dispatch_mod, "log_gate_audit", gate_audit or MagicMock(),
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


# --- dispatch-02/12: the retry base and the default branch ------------------


def test_retry_resets_to_worktree_base_not_default_fork_point(
    repo_on_wip_ahead_of_master: Path,
) -> None:
    """The worktree is created from the main checkout's HEAD. A reset to
    the fork point from master would silently drop the operator's base
    commit from the retry; the recorded base must be used."""
    repo = repo_on_wip_ahead_of_master
    master_before = _sha(repo, "master")
    wip_before = _sha(repo, "operator-wip")
    worktree = str(repo / ".forge-worktrees" / "task-21")
    agent = FakeAgent(["cycles_exhausted", "cycles_exhausted"])

    status_updates = _run_parallel(repo, [21], agent, max_retries=1)

    assert agent.calls == [(worktree, "forge-task-21"), (worktree, "forge-task-21")]
    assert _sha(repo, "forge-task-21^") == wip_before, (
        "the retry was not based on the SHA the worktree was created on"
    )
    history = _git(repo, "log", "--format=%s", "forge-task-21")
    assert "agent work 21 attempt 2" in history
    assert "agent work 21 attempt 1" not in history
    assert _sha(repo, "master") == master_before
    assert _sha(repo, "operator-wip") == wip_before
    assert status_updates == [(21, "cycles_exhausted")]


def test_cleanup_never_resets_default_branch_checked_out_in_worktree(
    repo_on_wip_ahead_of_master: Path,
) -> None:
    """An agent that checks out master inside its worktree (possible
    because the main checkout is elsewhere) must stop the task: the cleanup
    must neither ``reset --hard`` master to the worktree base nor let a
    retry commit onto it."""
    repo = repo_on_wip_ahead_of_master
    master_before = _sha(repo, "master")

    def checkout_default(workdir: Path, attempt: int) -> None:
        _git(workdir, "checkout", "-q", "master")

    agent = FakeAgent(
        ["cycles_exhausted", "tests_passed", "tests_passed"],
        after_commit=checkout_default,
    )

    status_updates = _run_parallel(repo, [22], agent, max_retries=2)

    assert len(agent.calls) == 1, "a retry ran on the default branch"
    assert _sha(repo, "master") == master_before, "the cleanup moved master"
    assert "agent work 22 attempt 1" in _git(
        repo, "log", "--format=%s", "forge-task-22",
    ), "the failed attempt's commit was not preserved on its branch"
    # Task #3111: caught by the post-attempt branch assertion, before cleanup.
    assert status_updates == [(22, "worktree_branch_mismatch")]


def test_cleanup_with_unreachable_base_fails_loud_before_status_reset(
    repo: Path, monkeypatch,
) -> None:
    """A failing ``git reset`` must raise; the old code logged a warning,
    reset the task to todo and retried on top of the failed attempt."""
    worktree = repo / ".forge-worktrees" / "task-31"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-31", str(worktree))
    failed_sha = _commit_file(worktree, "failed.txt", "failed attempt")
    db_opened = MagicMock()
    monkeypatch.setattr(dispatch_mod, "get_db_connection", db_opened)

    with pytest.raises(AttemptCleanupError, match="reset forge-task-31"):
        asyncio.run(dispatch_mod.cleanup_failed_attempt(
            31, str(worktree), [], output=[], base_sha="0" * 40,
        ))

    db_opened.assert_not_called()
    assert _checked_out_branch(worktree) == "forge-task-31"
    assert _sha(repo, "forge-task-31") == failed_sha


def test_main_checkout_cleanup_without_task_branch_still_resets_todo(
    repo: Path, monkeypatch,
) -> None:
    """No branch to delete is not an error: the task is reset to todo and
    the main checkout stays where it was."""
    db = _memory_tasks_db([32])
    monkeypatch.setattr(dispatch_mod, "get_db_connection", lambda write=False: db)
    output: list[str] = []

    asyncio.run(dispatch_mod.cleanup_failed_attempt(32, str(repo), [], output=output))

    assert any("does not exist" in line for line in output), output
    assert _checked_out_branch(repo) == "master"
    assert db.execute("SELECT status FROM tasks WHERE id = 32").fetchone()[0] == "todo"


# --- requirement 3: the branch is asserted before EVERY attempt -------------


def test_branch_is_rechecked_before_the_retry_attempt(repo: Path) -> None:
    """Something that moves the worktree off its branch between attempts
    (here a cleanup that ends detached) must abort the retry."""
    worktree = repo / ".forge-worktrees" / "task-33"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-33", str(worktree))
    agent = FakeAgent(["cycles_exhausted", "tests_passed"])
    gate_audit = MagicMock()
    expected_repository: list[bool] = []

    async def cleanup_that_detaches(task_id, project_dir, reflections,
                                    output=None, *, base_sha=None,
                                    expect_repository=True):
        # Task 3166 (R3165-01): a loop with a task branch says a repository
        # is expected.
        expected_repository.append(expect_repository)
        _git(Path(project_dir), "checkout", "-q", "--detach")

    with patch.object(dispatch_mod, "run_dev_test_loop", new=agent), \
            patch.object(dispatch_mod, "cleanup_failed_attempt",
                         new=cleanup_that_detaches), \
            patch.object(dispatch_mod, "fetch_task",
                         side_effect=lambda task_id: _task(task_id)), \
            patch.object(dispatch_mod, "log_gate_audit", gate_audit):
        _, _, outcome, _, _, _ = asyncio.run(
            dispatch_mod.run_dev_test_loop_with_autoresearch(
                _task(33), str(worktree), {}, SimpleNamespace(),
                {"features": {"autoresearch": True}, "autoresearch_max_retries": 3},
                output=[], task_branch="forge-task-33",
            )
        )

    assert outcome == "worktree_branch_mismatch"
    assert len(agent.calls) == 1, "the retry ran on a detached HEAD"
    events = [call.kwargs.get("event") for call in gate_audit.call_args_list]
    assert events == ["worktree-branch-mismatch"]
    assert expected_repository == [True]


# --- abort outcomes and their audit trail -----------------------------------


@pytest.mark.parametrize(
    ("task_id", "outcome"),
    [
        (931071, "worktree_refused"),
        (931072, "worktree_branch_mismatch"),
        (931073, "attempt_cleanup_failed"),
    ],
)
def test_abort_outcomes_leave_task_blocked(task_id: int, outcome: str) -> None:
    """Requirement 1 asks for status blocked; the new outcomes go through
    the real status writer against the hermetic test DB."""
    from equipa.db import db_conn, update_task_status

    with db_conn(write=True) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects (name) VALUES ('t3107-probe-project')"
        )
        project_id = conn.execute(
            "SELECT id FROM projects WHERE name = 't3107-probe-project'"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, project_id, title, status) "
            "VALUES (?, ?, 'probe', 'in_progress')",
            (task_id, project_id),
        )

    update_task_status(task_id, outcome, output=[])

    with db_conn() as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
    assert status == "blocked"


def test_refused_task_leaves_one_line_gate_audit_record(repo: Path) -> None:
    _git(repo, "branch", "forge-task-34")
    gate_audit = MagicMock()
    agent = FakeAgent(["tests_passed"])

    status_updates = _run_parallel(repo, [34], agent, gate_audit=gate_audit)

    assert agent.calls == []
    assert status_updates == [(34, "worktree_refused")]
    gate_audit.assert_called_once()
    message, task_id = gate_audit.call_args.args
    assert task_id == 34
    assert gate_audit.call_args.kwargs == {"event": "worktree-refused"}
    assert "stale branch forge-task-34 exists" in message
    assert "\n" not in message


def test_audit_detail_spanning_lines_is_collapsed(monkeypatch) -> None:
    gate_audit = MagicMock()
    monkeypatch.setattr(dispatch_mod, "log_gate_audit", gate_audit)
    output: list[str] = []

    dispatch_mod._audit_task_abort(
        35, "attempt-cleanup-failed", "fatal: bad\n  revision\tabc", output,
    )

    gate_audit.assert_called_once_with(
        "task=35 event=attempt-cleanup-failed detail=fatal: bad revision abc",
        35, event="attempt-cleanup-failed",
    )
    assert any("[GATE-AUDIT]" in line for line in output), output


# --- dispatch-01/13: leftover worktrees -------------------------------------


def test_stale_branch_leftover_worktree_is_left_untouched(repo: Path) -> None:
    """The stale-branch refusal comes first: a leftover worktree holding
    the branch keeps its uncommitted work in place, unstashed and
    registered. The old code force-removed it before anything else."""
    worktree_base = repo / ".forge-worktrees"
    leftover = worktree_base / "task-36"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-36", str(leftover))
    (leftover / "unsaved.txt").write_text("uncommitted\n", encoding="utf-8")
    refusals: dict[int, str] = {}

    worktree_dirs = asyncio.run(dispatch_mod._create_isolation_worktrees(
        [{"id": 36}], str(repo), worktree_base, refusals=refusals,
    ))

    assert worktree_dirs == {}
    assert refusals == {
        36: "stale branch forge-task-36 exists; preserved, resolve by hand",
    }
    assert (leftover / "unsaved.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert str(leftover.resolve()) in _registered_worktrees(repo)
    assert _stash_list(repo) == []


def test_clean_leftover_worktree_is_replaced_without_a_stash(repo: Path) -> None:
    worktree_base = repo / ".forge-worktrees"
    leftover = worktree_base / "task-37"
    _git(repo, "worktree", "add", "-q", "--detach", str(leftover))
    refusals: dict[int, str] = {}

    worktree_dirs = asyncio.run(dispatch_mod._create_isolation_worktrees(
        [{"id": 37}], str(repo), worktree_base, refusals=refusals,
    ))

    assert worktree_dirs == {37: str(leftover)}
    assert refusals == {}
    assert _checked_out_branch(leftover) == "forge-task-37"
    assert _stash_list(repo) == []


def test_empty_leftover_directory_is_replaced(repo: Path) -> None:
    worktree_base = repo / ".forge-worktrees"
    leftover = worktree_base / "task-38"
    leftover.mkdir(parents=True)
    refusals: dict[int, str] = {}

    worktree_dirs = asyncio.run(dispatch_mod._create_isolation_worktrees(
        [{"id": 38}], str(repo), worktree_base, refusals=refusals,
    ))

    assert worktree_dirs == {38: str(leftover)}
    assert refusals == {}
    assert _checked_out_branch(leftover) == "forge-task-38"


def test_force_deletes_stale_branch_after_logging_its_commits(
    repo: Path, capsys,
) -> None:
    """Regression guard for the explicit override: ``force=True`` still
    recreates the worktree, and the discarded commits are logged first."""
    master_sha = _sha(repo, "master")
    _git(repo, "checkout", "-q", "-b", "forge-task-39")
    stale_sha = _commit_file(repo, "stale.txt", "stale work")
    _git(repo, "checkout", "-q", "master")
    worktree_base = repo / ".forge-worktrees"
    refusals: dict[int, str] = {}

    worktree_dirs = asyncio.run(dispatch_mod._create_isolation_worktrees(
        [{"id": 39}], str(repo), worktree_base, force=True, refusals=refusals,
    ))

    assert worktree_dirs == {39: str(worktree_base / "task-39")}
    assert refusals == {}
    assert _sha(repo, "forge-task-39") == master_sha
    assert stale_sha in capsys.readouterr().out, "discarded commit was not logged"


# --- non-git projects keep running in place ---------------------------------


def test_non_git_project_still_runs_in_project_dir(tmp_path: Path) -> None:
    """The refusal applies only where a worktree was expected; a project
    that is not a git repo has no branch to protect."""
    project_dir = tmp_path / "plain"
    project_dir.mkdir()
    seen: list[str] = []

    async def plain_agent(task, project_dir, project_context, args, output=None):
        seen.append(str(project_dir))
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    status_updates = _run_parallel(project_dir, [40], plain_agent)

    assert seen == [str(project_dir)]
    assert status_updates == [(40, "tests_passed")]
