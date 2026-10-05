"""Task 3177: the repository-appearance check (N1) on a project the walk
cannot complete.

* IR3174-04 (S3174-02): ``os.path.lexists`` reads every error as "no such
  entry". A project holding ``.git`` (or an implicit bare repository) that
  the agent made mode 0600 read as "no repository" in both walks: N1 blocked
  nothing and recorded nothing, and once the operator restored the mode the
  next dispatch adopted the agent's repository (the S3168-03 chain). N1 now
  fails closed on any error other than ENOENT/ENOTDIR while walking: the
  task is blocked, never retried, no git runs there, and later dispatches of
  the project are refused.
* IR3174-05: the goal loop's N1 checks (before and after each task) block a
  symlink loop the planner or the developer leaves at the project path, as
  the retry loops do (tests/test_repository_appeared_symlink_loop_3172.py).

These tests need a user that mode bits deny (not root): each checks that
the mode denies it and fails, rather than skips, when it does not.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import equipa.dispatch as dispatch_mod
from test_dispatch_modes_gated_3112 import (
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
)
from test_worktree_poison_matrix_3158 import (
    REPOSITORY_APPEARED,
    TASK_ID,
    GitRecorder,
    _goal_args,
    _non_git_project,
    _patch_goal_planner,
    _patch_non_git_loops,
    _record_gate_audit,
    _run,
    _run_non_git_loop,
    _stub_dev_test_agents,
)


@pytest.fixture
def refusals_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Refusal records of this test only: the store follows THEFORGE_DB."""
    database = tmp_path / "db" / "operator.db"
    database.parent.mkdir()
    monkeypatch.setattr(dispatch_mod._equipa_constants, "THEFORGE_DB", database)
    return database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME


@pytest.fixture
def deny_search() -> Iterator[Callable[[Path], None]]:
    """``deny_search(directory)`` makes ``directory`` mode 0600, as an agent
    can; every such directory is searchable again at teardown."""
    denied: list[Path] = []

    def deny(directory: Path) -> None:
        directory.chmod(0o600)
        denied.append(directory)
        try:
            os.lstat(directory / ".git")
        except PermissionError:
            return
        except OSError as exc:
            pytest.fail(f"{directory}: expected EACCES, got {exc!r}")
        pytest.fail(f"mode 0600 does not deny uid {os.geteuid()} the search of "
                    f"{directory}: these tests need a non-root user")

    yield deny
    for directory in denied:
        if directory.exists():
            directory.chmod(0o755)


def _plant_dot_git(project: Path) -> Path:
    """A repository of the agent's: ``.git`` with a clean filter."""
    git_dir = project / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "objects").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(
        "[filter \"agent\"]\n\tclean = touch ran-in-the-orchestrator\n")
    return git_dir


def _plant_implicit_bare(project: Path) -> Path:
    """An implicit bare repository (S3168-01): no ``.git`` anywhere."""
    (project / "refs" / "heads").mkdir(parents=True)
    (project / "objects").mkdir()
    (project / "HEAD").write_text("ref: refs/heads/main\n")
    return project


PLANTS = {"dot-git": _plant_dot_git, "implicit-bare": _plant_implicit_bare}


# --- IR3174-04: a project the walk cannot search ----------------------------

@pytest.mark.parametrize("layout", sorted(PLANTS))
def test_an_unsearchable_project_holding_a_repository_is_blocked_and_refused(
        tmp_path, monkeypatch, refusals_dir, deny_search, layout):
    audit = _record_gate_audit(monkeypatch)
    project = _non_git_project(tmp_path)
    PLANTS[layout](project)
    deny_search(project)
    # Both walks of task 3173 and task 3172 read the entries as absent.
    assert dispatch_mod._nearest_repository(str(project)) is None
    assert dispatch_mod._nearest_git_entry(project) is None
    output: list[str] = []
    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", output) is True
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (f"after attempt 1: a git repository appeared at {project} in a "
            f"project") in audit[0][0]
    assert list(refusals_dir.iterdir()), "no refusal record written"
    # The operator restores the mode: the next dispatch is refused, and does
    # not adopt the agent's repository as the project's checkout.
    project.chmod(0o755)
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project / "sub"))


def test_an_unsearchable_directory_above_the_project_is_blocked(
        tmp_path, monkeypatch, refusals_dir, deny_search):
    """The parent is 0600, so no entry of the project or of the parent can
    be examined; the first such directory, the project, is reported."""
    audit = _record_gate_audit(monkeypatch)
    holder = tmp_path / "holder"
    holder.mkdir()
    project = _non_git_project(holder)
    _plant_dot_git(project)
    deny_search(holder)
    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", []) is True
    assert f"a git repository appeared at {project} in a project" in audit[0][0]
    holder.chmod(0o755)
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


def test_the_walk_still_reads_absent_entries_as_absent(tmp_path):
    """Controls: only an error other than "no such file" or "not a
    directory" fails closed."""
    plain = _non_git_project(tmp_path)
    assert dispatch_mod._unexaminable_directory(str(plain)) is None
    assert dispatch_mod._unexaminable_directory(
        str(tmp_path / "missing" / "child")) is None
    regular_file = plain / "app.py"
    assert dispatch_mod._unexaminable_directory(
        str(regular_file / "below-a-file")) is None


def test_a_searchable_non_git_project_is_still_not_blocked(
        tmp_path, monkeypatch, refusals_dir):
    audit = _record_gate_audit(monkeypatch)
    project = _non_git_project(tmp_path)
    (project / "HEAD").write_text("not a repository: no refs beside it\n")
    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", []) is False
    assert audit == []
    assert not refusals_dir.exists()


def test_an_unexaminable_entry_fails_closed_whatever_the_error(
        tmp_path, monkeypatch, refusals_dir):
    """An I/O error on one entry of a searchable directory blocks too."""
    import errno

    audit = _record_gate_audit(monkeypatch)
    project = _non_git_project(tmp_path)
    real_lstat = os.lstat

    def failing_lstat(path, *args, **kwargs):
        if os.fspath(path) == os.path.join(str(project), "HEAD"):
            raise OSError(errno.EIO, "Input/output error", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(dispatch_mod.os, "lstat", failing_lstat)
    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1", []) is True
    assert f"a git repository appeared at {project} in a project" in audit[0][0]


@pytest.mark.parametrize("layout", sorted(PLANTS))
@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_an_attempt_that_leaves_an_unsearchable_repository_blocks_the_task(
        tmp_path, monkeypatch, refusals_dir, deny_search, loop, layout):
    """Both retry loops, as production calls them for a non-git project:
    the agent's attempt makes the project a repository and removes search
    permission from it. The task is blocked, not retried, and no git runs."""
    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        attempts.append(project_dir)
        PLANTS[layout](project)
        deny_search(project)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    _patch_non_git_loops(monkeypatch, attempt)
    with recorder.recording():
        outcome = _run(_run_non_git_loop(loop, project, []))

    assert attempts == [str(project)]
    assert outcome == REPOSITORY_APPEARED
    assert recorder.calls == [], [call.argv for call in recorder.calls]
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (f"after attempt 1 (tests_failed): a git repository appeared at "
            f"{project} in a project") in audit[0][0]
    project.chmod(0o755)
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


# --- IR3174-05: the goal loop and symlink loops -----------------------------

def _leave_self_loop(project: Path) -> None:
    shutil.move(str(project), str(project.with_name("moved-away")))
    os.symlink(project, project)


def _leave_pair_loop(project: Path) -> None:
    shutil.move(str(project), str(project.with_name("moved-away")))
    partner = project.with_name(project.name + "-partner")
    partner.symlink_to(project)
    project.symlink_to(partner)


LOOPS = {"self": _leave_self_loop, "pair": _leave_pair_loop}


def _run_goal(project: Path, recorder: GitRecorder) -> tuple:
    import equipa.manager as manager_mod

    with recorder.recording():
        return _run(manager_mod.run_manager_loop(
            "ship it", 9001, str(project), {}, _goal_args(), [],
        ))


@pytest.mark.parametrize("make_loop", sorted(LOOPS))
def test_goal_loop_stops_before_a_task_when_the_planner_left_a_symlink_loop(
        tmp_path, monkeypatch, refusals_dir, make_loop):
    """The planner's turn leaves a loop at the project path: N1 before the
    first task blocks it, no agent of the task runs, no git runs after the
    loop exists, and the next dispatch of the project is refused."""
    import equipa.manager as manager_mod

    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    calls_before_loop: list[int] = []

    async def planner(goal_text, project_id, project_dir, project_context, args,
                      output=None):
        calls_before_loop.append(len(recorder.calls))
        LOOPS[make_loop](project)
        return {"cost": 0.0, "duration": 0.0}, [TASK_ID]

    dispatched = _stub_dev_test_agents(monkeypatch, lambda project_dir: None)
    goal = _patch_goal_planner(monkeypatch, [TASK_ID])
    monkeypatch.setattr(manager_mod, "run_planner_agent", planner)

    result = _run_goal(project, recorder)

    assert result[0] == REPOSITORY_APPEARED, result
    assert dispatched == []
    assert goal.statuses == {TASK_ID: REPOSITORY_APPEARED}
    assert goal.evaluator_runs == 0
    [loop_made_at] = calls_before_loop
    assert recorder.calls[loop_made_at:] == [], [
        call.argv for call in recorder.calls[loop_made_at:]]
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (f"before the task: a git repository appeared at {project} in a "
            f"project") in audit[0][0]
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


@pytest.mark.parametrize("make_loop", sorted(LOOPS))
def test_goal_loop_stops_after_a_task_whose_developer_left_a_symlink_loop(
        tmp_path, monkeypatch, refusals_dir, make_loop):
    """The developer of the first task leaves a loop at the project path:
    N1 after the task blocks it, the second task and the evaluator never
    run, no git runs after the loop exists, and the next dispatch of the
    project is refused."""
    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    calls_before_loop: list[int] = []

    def developer(project_dir: str) -> None:
        if not calls_before_loop:
            calls_before_loop.append(len(recorder.calls))
            LOOPS[make_loop](project)

    dispatched = _stub_dev_test_agents(monkeypatch, developer)
    goal = _patch_goal_planner(monkeypatch, [TASK_ID, TASK_ID + 2])

    result = _run_goal(project, recorder)

    assert result[0] == REPOSITORY_APPEARED, result
    assert dispatched == ["developer", "tester"], dispatched
    assert goal.statuses == {TASK_ID: REPOSITORY_APPEARED}, goal.statuses
    assert goal.evaluator_runs == 0
    [loop_made_at] = calls_before_loop
    assert recorder.calls[loop_made_at:] == [], [
        call.argv for call in recorder.calls[loop_made_at:]]
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert "after the task (" in audit[0][0]
    assert f"a git repository appeared at {project} in a project" in audit[0][0]
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


def test_goal_loop_stops_after_a_task_whose_developer_denied_search(
        tmp_path, monkeypatch, refusals_dir, deny_search):
    """IR3174-04 in the goal loop: the developer makes the project a
    repository and mode 0600."""
    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    planted: list[Path] = []

    def developer(project_dir: str) -> None:
        if not planted:
            planted.append(_plant_dot_git(project))
            deny_search(project)

    _stub_dev_test_agents(monkeypatch, developer)
    goal = _patch_goal_planner(monkeypatch, [TASK_ID, TASK_ID + 2])

    result = _run_goal(project, recorder)

    assert result[0] == REPOSITORY_APPEARED, result
    assert goal.statuses == {TASK_ID: REPOSITORY_APPEARED}, goal.statuses
    assert goal.evaluator_runs == 0
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert f"a git repository appeared at {project} in a project" in audit[0][0]
