"""Regression tests for task #3111 — merge integrity.

* gate-01: the review is bound to the commit the reviewer read. A commit added
  to the task branch after (or during) a clean review is refused, and the
  merge names the reviewed SHA, never the branch.
* gate-12 / dispatch-03: the default branch is pinned before dispatch. Any
  movement other than the orchestrator's own merge raises an ALERT naming
  both SHAs and nothing further is merged.
* dispatch-05: a task is ``done`` only after its merge landed; merge_failed,
  blocked and invariant failures leave it blocked, and ``merged_sha`` is kept.
* dispatch-08 / dispatch-17: the rebase fallback can never report a merge
  that did not move the default branch, and merge failures log stdout+stderr.
* 3107 review R1: the worktree branch is asserted after every attempt.
* 3108 review: replace refs and driver / work-tree config fail closed before
  any gate evaluation.

Every test drives real git on temp repositories. Only the agents (developer
and security reviewer) and the DB writers are replaced.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db_migrate
from equipa import cli, loops
from equipa import dispatch as dispatch_mod
from equipa.dispatch import _gated_merge_task, _merge_task_branch, outcome_after_merge
from equipa.loops import ARTIFACTS_DIR_NAME, run_security_review
from equipa.merge_integrity import (
    DefaultBranchGuard,
    MergeAttempt,
    MergeOutcome,
    find_repo_execution_hazards,
    snapshot_reviewed_tree,
)
from equipa.security_gate import (
    get_reviewer_run,
    review_complete_line,
    reviewer_nonce_line,
    set_unrecorded_reviewer_runs_permitted,
)

TASK = 3111
BRANCH = f"forge-task-{TASK}"


# --- git helpers -------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _contains(repo: Path, ancestor: str, ref: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, ref],
        cwd=str(repo), capture_output=True,
    ).returncode == 0


def _checked_out_branch(cwd: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(cwd), text=True, capture_output=True,
    )
    return result.stdout.strip() or None


def _commit(cwd: Path, name: str, text: str, message: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)
    return _sha(cwd, "HEAD")


def _files_on(repo: Path, ref: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def _foreign_commit(repo: Path, branch: str = "main") -> str:
    """Move ``branch`` the way another process would: straight through the
    shared ref store, without touching any checkout (same tree, new commit)."""
    tree = _git(repo, "rev-parse", f"{branch}^{{tree}}")
    parent = _sha(repo, branch)
    new = _git(repo, "commit-tree", tree, "-p", parent, "-m", "foreign commit")
    _git(repo, "update-ref", f"refs/heads/{branch}", new)
    return new


def _add_task_worktree(repo: Path, task_id: int = TASK) -> Path:
    worktree = repo / ".forge-worktrees" / f"task-{task_id}"
    _git(repo, "worktree", "add", "-q", "-b", f"forge-task-{task_id}",
         str(worktree), "main")
    return worktree


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@forgeborn.local")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(
        f"{ARTIFACTS_DIR_NAME}/\n.forge-worktrees/\n", encoding="utf-8",
    )
    (path / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture(autouse=True)
def _production_provenance():
    """Gate as production does: no artifact is trusted without a record."""
    previous = set_unrecorded_reviewer_runs_permitted(False)
    yield
    set_unrecorded_reviewer_runs_permitted(previous)


def _gate(repo: Path, **kwargs) -> str:
    kwargs.setdefault("outcome", "tests_passed")
    kwargs.setdefault("task_id", TASK)
    kwargs.setdefault("branch", BRANCH)
    return asyncio.run(_gated_merge_task(repo=repo, **kwargs))


# --- reviewer harness: the real run_security_review, fake reviewer agent -----


def _clean_review(nonce: str) -> str:
    return (
        f"{reviewer_nonce_line(nonce)}\n# Security Review\n\n"
        f"## Summary\n1 low-severity finding(s).\n\n"
        f"### [R1] LOW — verbose error message\nDetails.\n\n"
        f"## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n"
        f"{review_complete_line(nonce)}\n"
    )


@pytest.fixture
def run_clean_review(monkeypatch):
    """``run(task_id, worktree, stable, during_review=None)`` runs the real
    reviewer pipeline; the fake reviewer writes an honest clean review in the
    directory it was told to work in (``state.review_dir``, the review
    checkout since #3116) and may call ``during_review()`` to simulate a
    concurrent writer. ``state.seen`` records what the reviewer read."""
    state = SimpleNamespace(
        task_id=None, worktree=None, during_review=None, review_dir=None,
        seen={},
    )

    async def fake_run_agent(_cmd, timeout=None):
        record = get_reviewer_run(state.task_id)
        review_dir = Path(state.review_dir)
        state.seen = {
            p.relative_to(review_dir).as_posix(): p.read_text(encoding="utf-8")
            for p in review_dir.rglob("*.py")
        }
        path = review_dir / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{state.task_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_clean_review(record.nonce), encoding="utf-8")
        if state.during_review is not None:
            state.during_review()
        return {"success": True, "result_text": "done", "errors": []}

    @contextlib.contextmanager
    def fake_cli(_prompt, review_dir, *_args, **_kwargs):
        state.review_dir = review_dir
        yield ["claude"]

    async def no_diff(_project_dir):
        return 0

    monkeypatch.setattr(loops, "run_agent", fake_run_agent)
    monkeypatch.setattr(loops, "build_cli_command", fake_cli)
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: "opus")
    monkeypatch.setattr(
        loops, "load_dispatch_config",
        lambda _p: {"security_review_timeout": 30},
    )
    monkeypatch.setattr(loops, "_measure_review_diff_lines", no_diff)
    monkeypatch.setattr(loops, "_extract_security_findings", lambda _t: [])

    def run(task_id: int, worktree: Path, stable: Path, during_review=None) -> dict:
        state.task_id, state.worktree = task_id, worktree
        state.during_review = during_review
        task = {"id": task_id, "title": "t", "description": "d", "project_id": 1}
        return asyncio.run(run_security_review(
            task, str(worktree), {}, SimpleNamespace(dispatch_config=None),
            output=[], stable_project_dir=str(stable),
        ))

    run.state = state
    return run


# --- gate-01: merge only the reviewed SHA ------------------------------------


def test_clean_review_merges_exactly_the_reviewed_commit(repo, run_clean_review):
    """Positive control: the reviewed commit is what lands, and the guard
    records it as the merged SHA."""
    worktree = _add_task_worktree(repo)
    reviewed = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    run_clean_review(TASK, worktree, repo)

    record = get_reviewer_run(TASK)
    assert record.reviewed_sha == reviewed
    assert record.reviewed_sha_end == reviewed
    assert record.reviewed_tree_clean is True

    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    assert _gate(repo, guard=guard, worktree_dir=str(worktree)) == "merged"
    assert guard.outcomes[TASK] == MergeOutcome("merged", "merged", reviewed)
    assert _contains(repo, reviewed, "main")
    assert guard.expected_sha == _sha(repo, "main")


def test_commit_added_after_clean_review_is_refused(repo, run_clean_review, capsys):
    """Probe A of gate-01: an unreviewed commit lands on the branch after a
    clean review. The merge must be refused and main left untouched."""
    worktree = _add_task_worktree(repo)
    _commit(worktree, "feature.py", "print('ok')\n", "feature")
    run_clean_review(TASK, worktree, repo)
    _commit(worktree, "evil.py", "import os\n", "unreviewed commit")
    main_before = _sha(repo, "main")

    status = _gate(repo)

    assert status == "blocked"
    assert _sha(repo, "main") == main_before
    assert "evil.py" not in _files_on(repo, "main")
    assert "branch moved after review" in capsys.readouterr().out


def test_commit_added_during_review_is_refused(repo, run_clean_review):
    """A commit landing while the reviewer runs: the reviewer may have read
    either tree, so the merge is refused even if the branch is not moved
    again afterwards."""
    worktree = _add_task_worktree(repo)
    _commit(worktree, "feature.py", "print('ok')\n", "feature")
    run_clean_review(
        TASK, worktree, repo,
        during_review=lambda: _commit(worktree, "evil.py", "x = 1\n", "sneaky"),
    )
    record = get_reviewer_run(TASK)
    assert record.reviewed_sha != record.reviewed_sha_end
    main_before = _sha(repo, "main")

    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    assert _gate(repo, guard=guard) == "blocked"
    assert "branch moved during review" in guard.outcomes[TASK].reason
    assert _sha(repo, "main") == main_before


def test_dirty_worktree_at_review_start_is_refused(repo, run_clean_review):
    """Uncommitted tracked changes mean the reviewer read a tree that is not
    the commit that would be merged."""
    worktree = _add_task_worktree(repo)
    _commit(worktree, "feature.py", "print('ok')\n", "feature")
    (worktree / "feature.py").write_text("print('decoy')\n", encoding="utf-8")
    run_clean_review(TASK, worktree, repo)
    assert get_reviewer_run(TASK).reviewed_tree_clean is False
    main_before = _sha(repo, "main")

    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))
    assert _gate(repo, guard=guard) == "blocked"
    assert "not clean" in guard.outcomes[TASK].reason
    assert _sha(repo, "main") == main_before


def test_merge_names_the_pinned_sha_not_the_branch(repo):
    """_merge_task_branch merges ``merge_sha`` even when the branch has
    moved on since, so a late commit never rides along."""
    worktree = _add_task_worktree(repo)
    approved = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    _commit(worktree, "evil.py", "import os\n", "late commit")
    attempt = MergeAttempt()

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False,
        merge_sha=approved, merge_record=attempt,
    ))

    assert merged is True
    assert attempt.merged_sha == approved
    assert "feature.py" in _files_on(repo, "main")
    assert "evil.py" not in _files_on(repo, "main")


# --- gate-12 / dispatch-03 / dispatch-05: full parallel runs -----------------


def _task(task_id: int) -> dict:
    return {
        "id": task_id, "project_id": 1, "title": f"task {task_id}",
        "description": "d", "role": "developer",
    }


class _NoCloseConn:
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


class _Agent:
    """Stands in for ``run_dev_test_loop``: one commit per task, then an
    optional ``side_effect(workdir, task)`` to misbehave."""

    def __init__(self, write=None, side_effect=None) -> None:
        self.write = write or (lambda task: (f"task-{task['id']}.txt", "work\n"))
        self.side_effect = side_effect
        # task id -> the commit the agent made (merged branches are deleted).
        self.commits: dict[int, str] = {}

    async def __call__(self, task, project_dir, project_context, args, output=None):
        workdir = Path(project_dir)
        name, text = self.write(task)
        self.commits[task["id"]] = _commit(
            workdir, name, text, f"task {task['id']} work",
        )
        if self.side_effect is not None:
            self.side_effect(workdir, task)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"


def _run_parallel(repo: Path, task_ids: list[int], agent, *, merge=None):
    """Run the real run_parallel_tasks; return ``(task_id, outcome,
    merged_sha)`` for every status write."""
    updates: list[tuple[int, str, str | None]] = []

    def record_status(task_id, outcome, output=None, merged_sha=None):
        updates.append((task_id, outcome, merged_sha))

    args = SimpleNamespace(
        yes=True, max_concurrent=1, use_flow=False, security_review=False,
        dispatch_config={"features": {"autoresearch": False}},
    )
    patches = [
        patch.object(dispatch_mod, "fetch_tasks_by_ids",
                     return_value=[_task(t) for t in task_ids]),
        patch.object(dispatch_mod, "resolve_project_dir", return_value=str(repo)),
        patch.object(dispatch_mod, "fetch_project_context", return_value={}),
        patch.object(dispatch_mod, "run_dev_test_loop", new=agent),
        patch.object(dispatch_mod, "fetch_task", side_effect=_task),
        patch.object(dispatch_mod, "get_db_connection",
                     lambda write=False: _memory_tasks_db(task_ids)),
        patch.object(dispatch_mod, "update_task_status", side_effect=record_status),
        patch.object(dispatch_mod, "record_agent_run", MagicMock()),
        patch.object(dispatch_mod, "get_role_model", return_value="test-model"),
        patch.object(dispatch_mod, "get_role_turns", return_value=10),
        patch.object(
            dispatch_mod, "run_security_review",
            AsyncMock(side_effect=AssertionError("reviewer must not run")),
        ),
        patch("equipa.scaffold.ensure_scaffold", return_value=False),
    ]
    if merge is not None:
        patches.append(patch.object(dispatch_mod, "_merge_task_branch", new=merge))
    with contextlib.ExitStack() as stack:
        for active in patches:
            stack.enter_context(active)
        asyncio.run(dispatch_mod.run_parallel_tasks(task_ids, args))
    return updates


def _is_done(outcome: str) -> bool:
    return outcome in ("tests_passed", "no_tests")


def test_default_branch_moved_during_run_alerts_and_merges_nothing(repo, capsys):
    """dispatch-03: another process moves main while the agents run. ALERT
    with both SHAs, no task merged, every task left blocked."""
    baseline = _sha(repo, "main")
    moved: list[str] = []

    def foreign_push(_workdir, task):
        if task["id"] == 31:
            moved.append(_foreign_commit(repo))

    updates = _run_parallel(repo, [31, 32], _Agent(side_effect=foreign_push))

    out = capsys.readouterr().out
    assert "ALERT" in out
    assert baseline in out and moved[0] in out
    for task_id in (31, 32):
        assert not _contains(repo, _sha(repo, f"forge-task-{task_id}"), "main")
    assert updates and all(not _is_done(outcome) for _, outcome, _ in updates)
    assert _sha(repo, "main") == moved[0]


def test_default_branch_moved_between_merges_stops_further_merges(repo, capsys):
    """gate-12: main moves right after the orchestrator's first merge. The
    chain check trips, the second task is never merged, and neither task is
    recorded as done."""
    real_merge = dispatch_mod._merge_task_branch
    merged_tasks: list[int] = []

    async def merge_then_foreign_move(project_dir, task_id, branch_name, **kwargs):
        merged = await real_merge(project_dir, task_id, branch_name, **kwargs)
        merged_tasks.append(task_id)
        if len(merged_tasks) == 1:
            _foreign_commit(repo)
        return merged

    updates = _run_parallel(repo, [41, 42], _Agent(), merge=merge_then_foreign_move)

    assert merged_tasks == [41]
    assert not _contains(repo, _sha(repo, "forge-task-42"), "main")
    assert "ALERT" in capsys.readouterr().out
    assert updates and all(not _is_done(outcome) for _, outcome, _ in updates)


def test_merge_failed_task_is_blocked_not_done(repo, capsys):
    """dispatch-05 (+ dispatch-17): two tasks edit the same line. The first
    merges and is done with its merged SHA; the second conflicts and stays
    blocked, and the conflict text (stdout) reaches the log."""
    def same_line(task):
        return "app.py", f"VALUE = 'task {task['id']}'\n"

    agent = _Agent(write=same_line)
    updates = _run_parallel(repo, [51, 52], agent)

    by_task = {task_id: (outcome, sha) for task_id, outcome, sha in updates}
    assert by_task[51] == ("tests_passed", agent.commits[51])
    assert _contains(repo, agent.commits[51], "main")
    assert by_task[52] == ("merge_failed", None)
    # The conflicting branch is preserved, unmerged.
    assert _sha(repo, "forge-task-52") == agent.commits[52]
    assert not _contains(repo, agent.commits[52], "main")
    assert _checked_out_branch(repo) == "main"
    assert "CONFLICT" in capsys.readouterr().out


def test_agent_committing_on_default_branch_is_caught(tmp_path, capsys):
    """3107 review R1: the agent's final, successful attempt checks out the
    default branch and commits there. The post-attempt branch check blocks
    the task and the default-branch chain raises the ALERT."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@forgeborn.local")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "app.py", "VALUE = 'base'\n", "base")
    # The operator's checkout sits elsewhere, so main is free to check out.
    _git(repo, "checkout", "-q", "-b", "operator-wip")

    def commit_on_main(workdir, _task):
        _git(workdir, "checkout", "-q", "main")
        _commit(workdir, "sneaky.py", "x = 1\n", "ungated commit on main")

    updates = _run_parallel(repo, [61], _Agent(side_effect=commit_on_main))

    assert updates == [(61, "worktree_branch_mismatch", None)]
    out = capsys.readouterr().out
    assert "ALERT" in out
    assert not _contains(repo, _sha(repo, "forge-task-61"), "main")


# --- dispatch-05: the status mapping and the stored SHA ----------------------


def test_outcome_after_merge_mapping():
    guard = DefaultBranchGuard("/repo", "main", "a" * 40, "a" * 40)
    guard.outcomes = {
        1: MergeOutcome("merged", "merged", "b" * 40),
        2: MergeOutcome("merge_failed", "merge conflict"),
        3: MergeOutcome("blocked", "branch moved after review"),
        4: MergeOutcome("noop", "already there", "c" * 40),
    }
    assert outcome_after_merge("tests_passed", guard.outcomes[1], guard) == (
        "tests_passed", "b" * 40, "merged",
    )
    assert outcome_after_merge("tests_passed", guard.outcomes[2], guard)[:2] == (
        "merge_failed", None,
    )
    assert outcome_after_merge("no_tests", guard.outcomes[3], guard) == (
        "merge_blocked", None, "branch moved after review",
    )
    assert outcome_after_merge("no_tests", guard.outcomes[4], guard)[:2] == (
        "no_tests", "c" * 40,
    )
    assert outcome_after_merge("tests_passed", None, guard)[0] == "merge_failed"
    assert outcome_after_merge("tests_passed", None, None, "unpinned")[0] == (
        "merge_integrity_failed"
    )
    guard.alert = "moved"
    assert outcome_after_merge("tests_passed", guard.outcomes[1], guard) == (
        "merge_integrity_failed", None, "moved",
    )


def _tasks_db(tmp_path: Path, *, with_merged_sha: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "forge.db"))
    conn.row_factory = sqlite3.Row
    column = ", merged_sha TEXT" if with_merged_sha else ""
    conn.execute(
        f"CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT, "
        f"completed_at DATETIME{column})"
    )
    conn.execute("INSERT INTO tasks (id, status) VALUES (7, 'in_progress')")
    conn.commit()
    return conn


def _patch_db_conn(monkeypatch, conn: sqlite3.Connection) -> None:
    import equipa.db as db

    @contextlib.contextmanager
    def fake_db_conn(write=False):
        yield conn
        conn.commit()

    monkeypatch.setattr(db, "db_conn", fake_db_conn)


def test_update_task_status_stores_and_clears_merged_sha(tmp_path, monkeypatch):
    from equipa.db import update_task_status

    conn = _tasks_db(tmp_path, with_merged_sha=True)
    _patch_db_conn(monkeypatch, conn)

    update_task_status(7, "tests_passed", output=[], merged_sha="d" * 40)
    assert tuple(conn.execute(
        "SELECT status, merged_sha FROM tasks WHERE id = 7",
    ).fetchone()) == ("done", "d" * 40)

    update_task_status(7, "merge_failed", output=[])
    assert tuple(conn.execute(
        "SELECT status, merged_sha FROM tasks WHERE id = 7",
    ).fetchone()) == ("blocked", None)


def test_update_task_status_without_column_still_writes_status(tmp_path, monkeypatch):
    """An unmigrated DB keeps working; the lost SHA is logged loudly."""
    from equipa.db import update_task_status

    conn = _tasks_db(tmp_path, with_merged_sha=False)
    _patch_db_conn(monkeypatch, conn)
    output: list[str] = []

    update_task_status(7, "tests_passed", output=output, merged_sha="e" * 40)

    assert conn.execute("SELECT status FROM tasks WHERE id = 7").fetchone()[0] == "done"
    assert any("merged_sha missing" in line for line in output)


def test_migration_v12_adds_merged_sha_idempotently(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "v11.db"))
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")
    db_migrate.migrate_v11_to_v12(conn)
    db_migrate.migrate_v11_to_v12(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "merged_sha" in columns
    assert db_migrate.MIGRATIONS[12][1] is db_migrate.migrate_v11_to_v12


def test_single_task_merge_failed_demotes_outcome(repo, monkeypatch):
    """Single-task mode: a merge_failed result demotes tests_passed, so the
    task is recorded as blocked, not done."""
    guard = asyncio.run(DefaultBranchGuard.snapshot(repo))

    async def failed_merge(**kwargs):
        kwargs["guard"].outcomes[kwargs["task_id"]] = MergeOutcome(
            "merge_failed", "merge conflict: CONFLICT (content)",
        )
        return "merge_failed"

    monkeypatch.setattr(cli, "_gated_post_merge", failed_merge)
    monkeypatch.setattr(cli, "is_security_review_enabled", lambda _a: False)
    args = SimpleNamespace(dispatch_config={}, dev_test=True)

    outcome = asyncio.run(cli._run_security_review_and_gate(
        {"id": TASK}, str(repo), {}, args, "tests_passed", guard=guard,
    ))

    assert outcome == "merge_failed"
    assert cli._merged_sha_for(guard, TASK, outcome) is None


# --- dispatch-08: the rebase fallback ----------------------------------------


def test_conflicting_merge_without_worktree_is_not_reported_merged(repo):
    """Review probe P5: single-task shape, no live worktree, a real
    conflict. The old in-place ``rebase HEAD <branch>`` merged the branch
    into itself and returned True with main unchanged."""
    _git(repo, "checkout", "-q", "-b", BRANCH)
    _commit(repo, "app.py", "VALUE = 'branch'\n", "branch edit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "app.py", "VALUE = 'main'\n", "main edit")
    _git(repo, "checkout", "-q", BRANCH)
    main_before = _sha(repo, "main")

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False,
    ))

    assert merged is False
    assert _sha(repo, "main") == main_before
    assert _checked_out_branch(repo) == "main"


def _fail_first_merge(monkeypatch, *, fake_fast_forward: bool = False) -> None:
    """Make the first plain merge fail (a conflict git could not resolve) so
    the rebase fallback runs; optionally make ``merge --ff-only`` claim
    success without moving anything."""
    real = dispatch_mod.git_run_async
    plain_merges: list[list[str]] = []

    async def flaky(args, cwd, *rest, **kwargs):
        if args[:2] == ["merge", "--no-edit"]:
            plain_merges.append(list(args))
        if args[:2] == ["merge", "--no-edit"] and len(plain_merges) == 1:
            return subprocess.CompletedProcess(
                args, 1, stdout="CONFLICT (simulated): app.py\n", stderr="",
            )
        if fake_fast_forward and args[:2] == ["merge", "--ff-only"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return await real(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(dispatch_mod, "git_run_async", flaky)


def test_rebase_fallback_without_worktree_never_reports_phantom_merge(
    repo, monkeypatch,
):
    """dispatch-08, review probe P5: single-task shape (no worktree), the
    merge fails, the old in-place ``rebase HEAD <branch>`` then succeeded on
    the task branch and ``merge <branch>`` merged it into itself ("Already
    up to date", rc 0) — reported as merged with main unchanged."""
    _git(repo, "checkout", "-q", "-b", BRANCH)
    task_sha = _commit(repo, "feature.py", "print('ok')\n", "feature")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "other.py", "y = 2\n", "main moved on")
    main_before = _sha(repo, "main")
    _fail_first_merge(monkeypatch)

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False,
    ))

    assert merged is False
    assert _sha(repo, "main") == main_before
    assert _checked_out_branch(repo) == "main"
    assert _sha(repo, BRANCH) == task_sha, "the task branch was rebased in place"


def test_rebase_fallback_rebases_in_worktree_and_fast_forwards(repo, monkeypatch):
    worktree = _add_task_worktree(repo)
    approved = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    _commit(repo, "other.py", "y = 2\n", "main moved on")
    main_before = _sha(repo, "main")
    _fail_first_merge(monkeypatch)
    attempt = MergeAttempt()

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=approved,
        worktree_dir=str(worktree), merge_record=attempt,
    ))

    assert merged is True
    rebased = _sha(worktree, "HEAD")
    assert rebased != approved
    assert attempt.merged_sha == rebased
    assert _sha(repo, "main") == rebased
    assert _git(repo, "rev-parse", f"{rebased}^") == main_before
    assert _checked_out_branch(repo) == "main"


def test_rebase_fallback_cannot_report_merge_when_main_did_not_move(
    repo, monkeypatch, capsys,
):
    """dispatch-08: ``merge --ff-only`` exits 0 but main does not move. The
    fallback must report failure, never a merge that did not happen."""
    worktree = _add_task_worktree(repo)
    approved = _commit(worktree, "feature.py", "print('ok')\n", "feature")
    _commit(repo, "other.py", "y = 2\n", "main moved on")
    main_before = _sha(repo, "main")
    _fail_first_merge(monkeypatch, fake_fast_forward=True)
    attempt = MergeAttempt()

    merged = asyncio.run(_merge_task_branch(
        str(repo), TASK, BRANCH, expect_artifact=False, merge_sha=approved,
        worktree_dir=str(worktree), merge_record=attempt,
    ))

    assert merged is False
    assert attempt.merged_sha is None
    assert _sha(repo, "main") == main_before
    assert "did not move" in attempt.reason
    assert "CONFLICT (simulated)" in capsys.readouterr().out


# --- 3108 review: repository hazards fail closed ------------------------------


def _task_branch_with(repo: Path, files: dict[str, str]) -> Path:
    worktree = _add_task_worktree(repo)
    for name, text in files.items():
        (worktree / name).write_text(text, encoding="utf-8")
        _git(worktree, "add", name)
    _git(worktree, "commit", "-q", "-m", "task work")
    return worktree


def test_replace_refs_block_before_the_gate(repo, capsys):
    """The reviewer agent's git honours replace refs (it could be shown a
    decoy), so any refs/replace/* blocks before the gate is evaluated."""
    worktree = _task_branch_with(repo, {"feature.py": "print('ok')\n"})
    _git(repo, "replace", _sha(worktree, "HEAD"), _sha(repo, "main"))
    main_before = _sha(repo, "main")

    status = _gate(repo, security_review_enabled=False)

    assert status == "blocked"
    assert "replace refs present" in capsys.readouterr().out
    assert _sha(repo, "main") == main_before


def test_filter_driver_never_runs_during_merge(repo, tmp_path):
    """A smudge filter configured in .git/config runs during the merge's
    checkout. The gate must refuse before git ever gets there."""
    marker = tmp_path / "filter-ran"
    _task_branch_with(repo, {
        ".gitattributes": "*.txt filter=evil\n",
        "notes.txt": "hello\n",
    })
    _git(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    _git(repo, "config", "filter.evil.clean", "cat")
    main_before = _sha(repo, "main")

    status = _gate(repo, security_review_enabled=False)

    assert status == "blocked"
    assert not marker.exists(), "the filter driver ran during the merge"
    assert _sha(repo, "main") == main_before


def test_core_worktree_redirect_blocks_merge(repo, tmp_path):
    """core.worktree would send the merge checkout into another directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _task_branch_with(repo, {"feature.py": "print('ok')\n"})
    _git(repo, "config", "core.worktree", str(elsewhere))
    main_before = _sha(repo, "main")

    status = _gate(repo, security_review_enabled=False)

    assert status == "blocked"
    assert list(elsewhere.iterdir()) == []
    assert _sha(repo, "main") == main_before


def test_task_worktree_config_hazard_blocks_merge(repo, capsys):
    """Per-worktree config (extensions.worktreeConfig) in the TASK worktree
    is invisible from the main checkout, yet the rebase fallback runs git
    there, so the gate scans the task worktree too."""
    worktree = _task_branch_with(repo, {"feature.py": "print('ok')\n"})
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(worktree, "config", "--worktree", "merge.evil.driver", "true")
    assert asyncio.run(find_repo_execution_hazards(repo)) == []
    main_before = _sha(repo, "main")

    status = _gate(repo, security_review_enabled=False, worktree_dir=str(worktree))

    assert status == "blocked"
    assert "worktree config defines merge driver" in capsys.readouterr().out
    assert _sha(repo, "main") == main_before


def test_hazard_scan_follows_includes(repo, tmp_path):
    included = tmp_path / "included.cfg"
    included.write_text('[merge "x"]\n\tdriver = true\n', encoding="utf-8")
    assert asyncio.run(find_repo_execution_hazards(repo)) == []

    _git(repo, "config", "include.path", str(included))
    hazards = asyncio.run(find_repo_execution_hazards(repo))
    assert any("merge driver" in hazard for hazard in hazards)

    _git(repo, "config", "--unset", "include.path")
    _git(repo, "config", "includeIf.onbranch:main.path", str(tmp_path / "none.cfg"))
    hazards = asyncio.run(find_repo_execution_hazards(repo))
    assert any("conditional include" in hazard for hazard in hazards)


def test_snapshot_reports_hazard_as_unclean(repo):
    worktree = _task_branch_with(repo, {"feature.py": "print('ok')\n"})
    _git(repo, "config", "diff.x.textconv", "cat")
    snapshot = asyncio.run(snapshot_reviewed_tree(worktree))
    assert snapshot.sha == _sha(worktree, "HEAD")
    assert snapshot.clean is False
    assert "diff driver" in snapshot.detail
