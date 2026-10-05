"""Task #3173 (FF-3168): repositories without a ``.git`` and the goal loop.

The poison matrix (``tests/test_worktree_poison_matrix_3158.py``) drives the
three findings end to end; this module pins the pieces it relies on:

* S3168-01: ``dispatch._nearest_repository`` finds what git's discovery
  finds (a ``.git`` entry, or a directory that is itself a git directory)
  without running git; the dev-test loop's diff helpers and the single-agent
  guard run no git under the dispatch's non-git record, and the guard still
  sees the run's files through its filesystem scan.
* S3168-02: the goal loop's record covers its rounds and ends with them, and
  ``repository_appeared`` is a refused goal outcome.
* S3168-03: the N1 check records the repository outside the project; the
  refusal covers every project that would discover it, in its given and
  resolved form, fails closed on a record it cannot examine, and ends when
  the operator deletes the record.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod
import equipa.loops as loops
import equipa.manager as manager_mod
from equipa.git_ops import GitRepositoryUnreadableError
from equipa.monitoring import dispatched_without_git, git_checks_allowed
from equipa.single_agent_guard import _git_diff_files, evaluate_single_agent_outcome

from test_worktree_poison_matrix_3158 import GitRecorder

TASK_ID = 317301


def _run(coroutine):
    return asyncio.run(coroutine)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _repository(path: Path) -> Path:
    """A plain repository at ``path`` with one commit and a changed file."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('one')\n")
    _git(path, "add", "app.py")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "one")
    (path / "app.py").write_text("print('two')\n")
    return path


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    records: list[tuple[str, str | None]] = []

    def record(message: str, task_id: int | None = None, **kwargs: Any) -> None:
        records.append((message, kwargs.get("event")))

    monkeypatch.setattr(dispatch_mod, "log_gate_audit", record)
    return records


@pytest.fixture
def refusals_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Records of this test only: the store follows THEFORGE_DB."""
    database = tmp_path / "db" / "operator.db"
    database.parent.mkdir()
    monkeypatch.setattr(dispatch_mod._equipa_constants, "THEFORGE_DB", database)
    return database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME


# --- S3168-01: what the walk finds -----------------------------------------------


@pytest.mark.parametrize(
    ("entries", "found"),
    [
        ({".git": "dir"}, ".git"),
        ({".git": "file"}, ".git"),
        ({"HEAD": "file", "refs": "dir"}, "."),
        ({"HEAD": "file", "commondir": "file"}, "."),
        ({"HEAD": "file", "refs": "dir", "objects": "dir", "config": "file"}, "."),
        ({"HEAD": "file"}, None),
        ({"refs": "dir", "objects": "dir"}, None),
        ({"commondir": "file"}, None),
        ({}, None),
    ],
    ids=["dot-git-dir", "dot-git-file", "head-refs", "head-commondir", "full-bare",
         "head-only", "no-head", "commondir-only", "plain"],
)
def test_the_walk_finds_what_git_discovery_takes_for_a_repository(
    tmp_path: Path, entries: dict[str, str], found: str | None,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name, kind in entries.items():
        if kind == "dir":
            (project / name).mkdir()
        else:
            (project / name).write_text("ref: refs/heads/main\n")

    expected = None if found is None else (project / found if found != "." else project)
    assert dispatch_mod._nearest_repository(str(project)) == expected


def test_the_walk_finds_an_implicit_git_directory_above_the_project(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    (holder / "refs").mkdir(parents=True)
    (holder / "HEAD").write_text("ref: refs/heads/main\n")
    project = holder / "a" / "project"
    project.mkdir(parents=True)

    assert dispatch_mod._nearest_repository(str(project)) == holder


def test_the_walk_follows_the_resolved_project_path(tmp_path: Path) -> None:
    """Git discovers from the resolved working directory."""
    real = tmp_path / "bare"
    (real / "refs").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/main\n")
    (real / "project").mkdir()
    link = tmp_path / "elsewhere" / "project"
    link.parent.mkdir()
    link.symlink_to(real / "project", target_is_directory=True)

    assert dispatch_mod._nearest_repository(str(link)) == real


def test_the_walk_does_not_raise_on_a_symlink_loop(tmp_path: Path) -> None:
    """S3168-04 does not reach the new walk: ``os.path.realpath`` returns."""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")

    assert dispatch_mod._nearest_repository(str(tmp_path / "a")) is None


# --- S3168-01: the helpers under the record --------------------------------------


def test_the_loop_diff_helpers_and_the_guard_run_no_git_under_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _repository(tmp_path / "project")
    recorder = GitRecorder(monkeypatch)
    output: list[str] = []

    with recorder.recording(), dispatched_without_git(project):
        head = _run(loops._resolve_head_sha(str(project), output))
        empty = _run(loops._git_diff_is_empty(str(project), base_ref="HEAD"))
        context = _run(loops._capture_git_diff_context(str(project), 2, output, "HEAD"))
        files = _git_diff_files(project)

    assert recorder.calls == [], [call.argv for call in recorder.calls]
    assert (head, empty, context, files) == ("HEAD", False, "", [])
    assert any(
        "[Cycle 2] Project was not git at dispatch; no git diff" in line for line in output
    ), output


def test_the_loop_diff_helpers_and_the_guard_still_read_a_git_project(tmp_path: Path) -> None:
    """Control: nothing recorded, so each helper answers from git as before."""
    project = _repository(tmp_path / "project")

    assert _run(loops._resolve_head_sha(str(project))) == _git(project, "rev-parse", "HEAD")
    assert _run(loops._git_diff_is_empty(str(project))) is False
    assert "print('two')" in _run(loops._capture_git_diff_context(str(project), 1))
    assert _git_diff_files(project) == ["app.py"]


def test_the_guard_still_finds_the_runs_files_under_the_record(tmp_path: Path) -> None:
    """No git under the record is not "no output": the filesystem scan of
    the guard finds the file the run wrote."""
    project = _repository(tmp_path / "project")
    started = time.time() - 1
    (project / "report.md").write_text("# report\n")

    with dispatched_without_git(project):
        outcome = evaluate_single_agent_outcome(
            role="developer", task_id=TASK_ID, run_result={"files_changed": []},
            repo_path=project, run_started_at=started,
        )

    assert outcome.status == "success", outcome
    assert "report.md" in outcome.files_observed


# --- S3168-02: the goal loop's record ---------------------------------------------


@pytest.mark.parametrize("git_project", [False, True], ids=["non-git", "git"])
def test_the_goal_records_a_non_git_project_for_its_rounds_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusals_dir: Path, git_project: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    seen: list[bool] = []

    async def rounds(goal, project_id, project_dir, project_context, args, output, *,
                     is_git_project):
        seen.append(is_git_project)
        seen.append(git_checks_allowed(project_dir))
        return "goal_complete", 1, [], [], 0.0, 0.0

    monkeypatch.setattr(manager_mod, "_is_git_repo", lambda _dir: git_project)
    monkeypatch.setattr(manager_mod, "_run_goal_rounds", rounds)

    result = _run(manager_mod.run_manager_loop("goal", 9001, str(project), {}, None))

    assert result[0] == "goal_complete"
    # A git project is not recorded; a non-git one is, during the rounds.
    assert seen == [git_project, git_project]
    assert git_checks_allowed(project)


def test_a_stopped_goal_is_a_refused_dispatch() -> None:
    assert dispatch_mod.REPOSITORY_APPEARED_OUTCOME in manager_mod.GOAL_REFUSED_OUTCOMES


# --- S3168-03: the refusal record -------------------------------------------------


def test_the_n1_check_records_the_repository_and_refuses_projects_that_discover_it(
    tmp_path: Path, audit: list, refusals_dir: Path,
) -> None:
    holder = tmp_path / "holder"
    project = holder / "project"
    project.mkdir(parents=True)
    other_project = holder / "other" / "deep"
    other_project.mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (holder / ".git").mkdir()

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1 (tests_failed)", None,
    )

    [record] = sorted(refusals_dir.glob("*.json"))
    content = json.loads(record.read_text())
    assert content["repository"] == str(holder / ".git")
    assert content["project_dir"] == str(project)
    assert content["task_id"] == TASK_ID
    assert [event for _, event in audit] == ["repository-appeared"]
    assert f"later dispatches there are refused until {record} is deleted" in audit[0][0]
    for refused in (project, other_project, holder):
        with pytest.raises(dispatch_mod.AgentMadeRepositoryError, match="delete"):
            dispatch_mod.refuse_agent_made_repository(str(refused))
    dispatch_mod.refuse_agent_made_repository(str(sibling))

    record.unlink()

    dispatch_mod.refuse_agent_made_repository(str(project))


def test_an_implicit_git_directory_is_recorded_under_its_own_path(
    tmp_path: Path, audit: list, refusals_dir: Path,
) -> None:
    project = tmp_path / "project"
    (project / "refs").mkdir(parents=True)
    (project / "HEAD").write_text("ref: refs/heads/main\n")

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "before attempt 1", None,
    )

    assert f"before attempt 1: a git repository appeared at {project} in a project" in audit[0][0]
    record = dispatch_mod._agent_made_repository_record(str(project / "src"))
    assert record is not None
    assert json.loads(record.read_text())["repository"] == str(project)


def test_a_symlinked_project_is_recorded_and_refused_in_both_forms(
    tmp_path: Path, audit: list, refusals_dir: Path,
) -> None:
    real = tmp_path / "real" / "project"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    (real / ".git").mkdir()

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(link), "after attempt 1 (tests_failed)", None,
    )

    assert len(list(refusals_dir.glob("*.json"))) == 2
    for form in (link, real):
        with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
            dispatch_mod.refuse_agent_made_repository(str(form))


def test_a_record_that_cannot_be_examined_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusals_dir: Path,
) -> None:
    """Fails closed: only "no such file" means no record."""
    project = tmp_path / "project"
    project.mkdir()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path).startswith(str(refusals_dir)):
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(dispatch_mod.os, "lstat", lstat)

    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(project))


def test_a_record_that_cannot_be_written_still_blocks_the_task(
    tmp_path: Path, audit: list, refusals_dir: Path,
) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    refusals_dir.write_text("not a directory\n")

    assert dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "after attempt 1 (tests_failed)", None,
    )

    assert "the refusal of later dispatches could NOT be recorded" in audit[0][0]


def test_no_repository_records_nothing(
    tmp_path: Path, audit: list, refusals_dir: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    assert not dispatch_mod._repository_appeared_in_non_git_project(
        TASK_ID, str(project), "before attempt 1", None,
    )

    assert audit == []
    assert not refusals_dir.exists()


def test_the_refusal_is_an_unreadable_repository_to_every_dispatch_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: list, refusals_dir: Path,
) -> None:
    """Each dispatch already refuses a repository git cannot read; the
    refusal reaches the CLI's decision before ``_is_git_repo`` runs."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    dispatch_mod._repository_appeared_in_non_git_project(TASK_ID, str(project), "x", None)
    asked: list[str] = []
    monkeypatch.setattr(cli_mod, "_is_git_repo", lambda path: asked.append(path) or True)

    assert issubclass(dispatch_mod.AgentMadeRepositoryError, GitRepositoryUnreadableError)
    with pytest.raises(dispatch_mod.DispatchRefused) as refused:
        cli_mod._is_git_project(str(project))

    assert asked == []
    assert "Refusing to run git there" in refused.value.message


def test_the_refusals_follow_the_operator_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state" / "operator.db"
    monkeypatch.setattr(dispatch_mod._equipa_constants, "THEFORGE_DB", database)

    assert dispatch_mod._agent_repository_refusals_dir() == (
        tmp_path / "state" / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME
    )
