"""Task 3172 (S3168-04): the repository-appearance walk and a symlink loop.

``git_ops._nearest_git_entry`` walked ``path.resolve()``, which raises
``RuntimeError: Symlink loop`` on Python 3.10 to 3.12 when the project path
(or a directory above it) is a symlink loop. The error escaped every
repository-appearance check (N1, task 3168): the retry loops' check after an
attempt, the streaming runner's per-tool check. A loop cannot be walked, so
nothing shows that no repository is reachable through it: the walk now
returns the looping path itself (fail closed), and N1 blocks the task with
its audit record. No git runs there.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import equipa.agent_runner as agent_runner
import equipa.dispatch as dispatch_mod
from equipa.git_ops import _nearest_git_entry
from test_dispatch_modes_gated_3112 import (
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
)
from test_worktree_poison_matrix_3158 import (
    REPOSITORY_APPEARED,
    GitRecorder,
    _non_git_project,
    _patch_non_git_loops,
    _record_gate_audit,
    _run,
    _run_non_git_loop,
)


def _self_loop(path: Path) -> Path:
    path.symlink_to(path)
    return path


def _pair_loop(path: Path) -> Path:
    partner = path.with_name(path.name + "-partner")
    partner.symlink_to(path)
    path.symlink_to(partner)
    return path


def _below_a_loop(path: Path) -> Path:
    return _self_loop(path) / "sub" / "deeper"


@pytest.mark.parametrize("make_loop", [_self_loop, _pair_loop, _below_a_loop],
                         ids=["self", "pair", "below-a-loop"])
def test_a_symlink_loop_is_returned_as_its_own_entry(tmp_path, make_loop):
    path = make_loop(tmp_path / "project")
    assert _nearest_git_entry(path) == path.absolute()


def test_the_walk_still_finds_and_misses_what_it_did(tmp_path):
    """Controls: no loop, the walk is unchanged."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _nearest_git_entry(plain) is None
    assert _nearest_git_entry(tmp_path / "missing" / "child") is None
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / ".git").mkdir()
    assert _nearest_git_entry(repository / "src") == repository / ".git"
    # A symlink (not a loop) to a directory inside a repository: the
    # resolved form is walked too.
    link = tmp_path / "link"
    link.symlink_to(repository / "src")
    assert _nearest_git_entry(link) == repository / ".git"


def test_the_runner_ends_a_run_whose_project_became_a_symlink_loop(tmp_path):
    loop = _self_loop(tmp_path / "project")
    reason = agent_runner._repository_appeared_reason(str(loop))
    assert reason is not None
    assert str(loop) in reason


def test_the_n1_check_blocks_a_project_that_is_a_symlink_loop(
        tmp_path, monkeypatch):
    audit = _record_gate_audit(monkeypatch)
    loop = _pair_loop(tmp_path / "project")
    output: list[str] = []
    assert dispatch_mod._repository_appeared_in_non_git_project(
        3172, str(loop), "after attempt 1", output) is True
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert f"after attempt 1: a git repository appeared at {loop}" in audit[0][0]


@pytest.fixture
def refusals_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Refusal records of this test only: the store follows THEFORGE_DB."""
    database = tmp_path / "db" / "operator.db"
    database.parent.mkdir()
    monkeypatch.setattr(dispatch_mod._equipa_constants, "THEFORGE_DB", database)
    return database.parent / dispatch_mod.AGENT_REPOSITORY_REFUSALS_DIRNAME


@pytest.mark.parametrize("make_loop", [_self_loop, _pair_loop, _below_a_loop],
                         ids=["self", "pair", "below-a-loop"])
def test_the_n1_check_blocks_every_loop_shape_and_later_dispatches_refuse_it(
        tmp_path, monkeypatch, refusals_dir, make_loop):
    """Task 3174: task 3173's walk (``_nearest_repository``) reads a loop as
    "no repository", so the check falls back to ``_nearest_git_entry``,
    which returns the looping path. The block is recorded like any other N1
    find (S3168-03), so the next dispatch of the project is refused too."""
    audit = _record_gate_audit(monkeypatch)
    path = make_loop(tmp_path / "project")
    assert dispatch_mod._nearest_repository(str(path)) is None
    assert dispatch_mod._repository_appeared_in_non_git_project(
        3174, str(path), "after attempt 1", []) is True
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert f"a git repository appeared at {path} in a project" in audit[0][0]
    assert list(refusals_dir.iterdir()), "no refusal record written"
    with pytest.raises(dispatch_mod.AgentMadeRepositoryError):
        dispatch_mod.refuse_agent_made_repository(str(path))


def test_an_implicit_bare_repository_above_a_loop_is_the_one_reported(
        tmp_path, monkeypatch, refusals_dir):
    """Both intents at once: task 3173's walk finds a git directory with no
    ``.git`` above the looping project first, and that repository is what
    the check reports."""
    audit = _record_gate_audit(monkeypatch)
    holder = tmp_path / "holder"
    (holder / "refs").mkdir(parents=True)
    (holder / "HEAD").write_text("ref: refs/heads/main\n")
    loop = _self_loop(holder / "project")
    assert dispatch_mod._repository_appeared_in_non_git_project(
        3174, str(loop), "after attempt 1", []) is True
    assert f"a git repository appeared at {holder} in a project" in audit[0][0]


def test_a_project_that_is_gone_without_a_loop_is_not_blocked(
        tmp_path, monkeypatch, refusals_dir):
    """Control: only a loop fails closed. A project path that no longer
    exists (moved away, nothing left at it) holds no repository."""
    audit = _record_gate_audit(monkeypatch)
    project = _non_git_project(tmp_path)
    shutil.move(str(project), str(tmp_path / "moved-away"))
    assert dispatch_mod._repository_appeared_in_non_git_project(
        3174, str(project), "after attempt 1", []) is False
    assert audit == []
    assert not refusals_dir.exists()


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_an_attempt_that_leaves_a_symlink_loop_blocks_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: str,
) -> None:
    """Both retry loops, as production calls them for a non-git project:
    the agent's attempt moves the project away and leaves a symlink loop at
    its path. The check after the attempt blocks the task (no retry, no
    git), instead of raising out of the loop."""
    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        attempts.append(project_dir)
        shutil.move(str(project), str(tmp_path / "moved-away"))
        os.symlink(project, project)
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
