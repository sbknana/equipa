"""Task #3168 (FF-3166) — a project that was not git at dispatch.

R3166-01: in such a project an agent can ``git init`` and plant a clean
filter; any git the orchestrator then runs there by discovery (the change
check after every tool result, the progress checks after a run) executes
it, outside agent containment. ``equipa.monitoring.dispatched_without_git``
records the project for the dispatch, every change check runs no git under
that record, and the streaming runner ends a run as soon as a repository
appears. The loop-level vector lives in the poison matrix
(``tests/test_worktree_poison_matrix_3158.py``); this module checks the
pieces one by one, with the same plant and the same marker programs.

Also here: R3166-05 (``expect_repository`` has no default), R3166-03
(format characters in a task-abort audit line) and R3166-04 (the CLI's
cleanup-failure print).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import equipa.agent_runner as agent_runner
import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod
from equipa.monitoring import (
    _check_git_changes,
    dispatched_without_git,
    get_starting_sha,
    git_checks_allowed,
    has_branch_commits,
    has_session_commits,
)
from equipa.parsing import verify_files_changed

from test_dispatch_modes_gated_3112 import (
    _init_repo,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)
from test_repository_identity_3146 import TASK_ID
from test_worktree_poison_matrix_3158 import (
    CHANGE_CHECK_VECTOR,
    FAKE_AGENT_CLI,
    Agent,
    GitRecorder,
    _fake_agent,
    _non_git_project,
    _tool_call,
)


def _planted_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Agent]:
    """A project the agent made a repository of, with its clean filter set
    for every path and a committed file changed."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _fake_agent(tmp_path, agent, project)
    subprocess.run(["/bin/sh", str(tmp_path / "fake-agent" / "plant.sh")], check=True)
    assert agent.ran() == [], "planting ran the filter"
    return project, agent


def _stream_agent(directory: Path, events: list[dict[str, Any]]) -> list[str]:
    """argv of a fake agent replaying ``events`` (``{"run": ...}`` runs a
    script from ``directory``, as in the matrix)."""
    directory.mkdir(exist_ok=True)
    (directory / "stream.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
    )
    cli = directory / "fake_claude.py"
    cli.write_text(FAKE_AGENT_CLI)
    return [sys.executable, str(cli)]


RESULT_EVENT = {"type": "result", "subtype": "success", "result": "RESULT: success",
                "num_turns": 1, "total_cost_usd": 0.0}


def _stream(cmd: list[str], project: Path) -> dict[str, Any]:
    return asyncio.run(agent_runner.run_agent_streaming(
        cmd, role="developer", output=None, max_turns=40, project_dir=str(project),
    ))


# --- The dispatch's non-git record ---------------------------------------------


def test_the_record_covers_the_project_and_what_is_under_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    sibling = tmp_path / "project2"
    sibling.mkdir()

    assert git_checks_allowed(project)
    with dispatched_without_git(project):
        assert not git_checks_allowed(project)
        assert not git_checks_allowed(str(project / "src"))
        assert not git_checks_allowed(project / "src" / ".." / "src")
        # A shared prefix is not "under it", and neither is the parent.
        assert git_checks_allowed(sibling)
        assert git_checks_allowed(tmp_path)
    assert git_checks_allowed(project)
    assert git_checks_allowed(None)


def test_the_record_matches_through_a_symlink_either_way(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with dispatched_without_git(real):
        assert not git_checks_allowed(link), "the resolved form must match"
    with dispatched_without_git(link):
        # The agent swaps the project path for a symlink to elsewhere: the
        # path as recorded still matches.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link.unlink()
        link.symlink_to(elsewhere)
        assert not git_checks_allowed(link)


def test_records_nest_and_unwind(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    with dispatched_without_git(first):
        with dispatched_without_git(second):
            assert not git_checks_allowed(first) and not git_checks_allowed(second)
        assert not git_checks_allowed(first)
        assert git_checks_allowed(second)


def test_concurrent_tasks_see_only_their_own_record(tmp_path: Path) -> None:
    """The parallel loop runs each task as its own asyncio task: a non-git
    task's record must not reach a git task running at the same time."""
    non_git, git_project = tmp_path / "non-git", tmp_path / "git"
    non_git.mkdir()
    git_project.mkdir()
    seen: dict[str, bool] = {}

    async def main() -> None:
        recorded = asyncio.Event()
        checked = asyncio.Event()

        async def non_git_task() -> None:
            with dispatched_without_git(non_git):
                recorded.set()
                await checked.wait()
                seen["own record"] = git_checks_allowed(non_git)

        async def other_task() -> None:
            await recorded.wait()
            seen["other task"] = git_checks_allowed(non_git)
            checked.set()

        await asyncio.gather(non_git_task(), other_task())

    asyncio.run(main())

    assert seen == {"own record": False, "other task": True}


# --- Every change check runs no git under the record ------------------------------

CHECKS = {
    "_check_git_changes": (lambda project: _check_git_changes(project), False),
    "get_starting_sha": (lambda project: get_starting_sha(project), None),
    "has_session_commits": (lambda project: has_session_commits(project, "0" * 40), False),
    "has_branch_commits": (lambda project: has_branch_commits(project), False),
    "verify_files_changed": (
        lambda project: verify_files_changed(["app.py"], project), ["app.py"],
    ),
}


@pytest.mark.parametrize("check", sorted(CHECKS))
def test_change_check_runs_no_git_in_a_project_recorded_as_non_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str,
) -> None:
    project, agent = _planted_project(tmp_path, monkeypatch)
    recorder = GitRecorder(monkeypatch)
    call, expected = CHECKS[check]

    with dispatched_without_git(project), recorder.recording():
        answer = call(str(project))

    assert agent.ran() == [], f"EXECUTED {CHANGE_CHECK_VECTOR}/{check}: {agent.ran()}"
    assert recorder.calls == [], [entry.argv for entry in recorder.calls]
    assert answer == expected


def test_without_the_record_the_change_check_still_reads_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record is specific: a git project's change check is unchanged."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "README.md").write_text("changed\n")
    with dispatched_without_git(tmp_path / "another-project"):
        assert _check_git_changes(str(repo)) is True


# --- The streaming runner ---------------------------------------------------------


def test_runner_without_a_record_ends_a_run_that_makes_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that records nothing: no .git when the run starts is enough
    for the runner to start no git and to end the run when one appears."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    cmd = _fake_agent(tmp_path, agent, project)
    recorder = GitRecorder(monkeypatch)

    with recorder.recording():
        result = _stream(cmd, project)

    assert agent.ran() == [], f"EXECUTED {CHANGE_CHECK_VECTOR}/runner: {agent.ran()}"
    assert (project / ".git").is_dir(), "the fake agent made no repository"
    assert recorder.discovery_in(project) == [], recorder.discovery_in(project)
    assert result["early_terminated"] is True
    assert f"a git repository appeared at {project / '.git'}" in result["early_term_reason"]


def test_runner_ends_a_run_whose_last_step_makes_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repository is made after the last tool result: the check once
    the process tree is gone still ends the run, so the dev-test loop sees
    a terminated attempt and runs no git diff on it."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _fake_agent(tmp_path, agent, project)
    directory = tmp_path / "fake-agent"
    cmd = _stream_agent(directory, [*_tool_call("toolu_ls", "ls"), {"run": "plant.sh"}, RESULT_EVENT])
    recorder = GitRecorder(monkeypatch)

    with dispatched_without_git(project), recorder.recording():
        result = _stream(cmd, project)

    assert agent.ran() == []
    assert (project / ".git").is_dir()
    assert recorder.discovery_in(project) == [], recorder.discovery_in(project)
    assert result["early_terminated"] is True
    assert "a git repository appeared at" in result["early_term_reason"]


def test_runner_starts_no_agent_in_a_recorded_project_that_holds_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1: an earlier agent of the dispatch left a repository; the next
    agent (a tester, a retry) is never started in it."""
    project, agent = _planted_project(tmp_path, monkeypatch)
    recorder = GitRecorder(monkeypatch)

    async def no_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an agent was started in the agent's repository")

    monkeypatch.setattr(agent_runner, "_spawn_agent_process", no_spawn)
    cmd = _stream_agent(tmp_path / "unused-agent", [RESULT_EVENT])

    with dispatched_without_git(project), recorder.recording():
        result = _stream(cmd, project)

    assert agent.ran() == []
    assert recorder.calls == [], [entry.argv for entry in recorder.calls]
    assert result["early_terminated"] is True
    assert result["files_changed_set"] == []
    assert f"a git repository appeared at {project / '.git'}" in result["early_term_reason"]


def test_runner_still_checks_git_in_a_git_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: with a repository when the run starts and no record, the
    per-tool change check runs git as before."""
    repo = _init_repo(tmp_path / "repo")
    cmd = _stream_agent(tmp_path / "fake-agent", [*_tool_call("toolu_ls", "ls"), RESULT_EVENT])
    recorder = GitRecorder(monkeypatch)

    with recorder.recording():
        result = _stream(cmd, repo)

    assert not result.get("early_terminated"), result
    assert any(
        "diff" in argv and "--stat" in argv for argv in recorder.discovery_in(repo)
    ), recorder.discovery_in(repo)


def test_the_termination_reason_is_not_read_as_analysis_paralysis(tmp_path: Path) -> None:
    """A paralysis-shaped reason makes the dev-test loop retry the cycle in
    the same place; this one must end the attempt."""
    from equipa.loops import _is_analysis_paralysis

    project = _non_git_project(tmp_path)
    (project / ".git").mkdir()
    reason = agent_runner._repository_appeared_reason(str(project))

    assert reason is not None
    assert not _is_analysis_paralysis(reason)
    assert agent_runner._repository_appeared_reason(str(tmp_path / "missing")) is None


# --- R3166-05: expect_repository must be stated ------------------------------------


def test_cleanup_failed_attempt_requires_expect_repository() -> None:
    parameter = inspect.signature(dispatch_mod.cleanup_failed_attempt).parameters[
        "expect_repository"
    ]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="expect_repository"):
        dispatch_mod.cleanup_failed_attempt(TASK_ID, "/nonexistent", [])


# --- R3166-03 / R3166-04: what an audit line and its neighbour print -----------------

FORMAT_CHARACTERS = (
    "\N{RIGHT-TO-LEFT OVERRIDE}",
    "\N{LEFT-TO-RIGHT ISOLATE}",
    "\N{ZERO WIDTH SPACE}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
    "\N{TAG LATIN CAPITAL LETTER A}",
)
DETAIL = "refs/heads/a" + "".join(FORMAT_CHARACTERS) + "kcolb\x1b[1A done"


def _format_characters(text: str) -> list[str]:
    return [char for char in text if unicodedata.category(char) in ("Cf", "Cc")]


def test_task_abort_audit_escapes_format_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[str] = []
    monkeypatch.setattr(
        dispatch_mod, "log_gate_audit",
        lambda message, task_id=None, **kwargs: records.append(message),
    )
    output: list[str] = []

    dispatch_mod._audit_task_abort(TASK_ID, "worktree-branch-mismatch", DETAIL, output)

    [record] = records
    operator_line = "\n".join(output)
    for text in (record, operator_line):
        assert _format_characters(text) == [], ascii(text)
        assert "\\u202e\\u2066\\u200b\\ufeff\\U000e0041kcolb\\x1b[1A done" in text, text


@pytest.mark.parametrize("text", ["plain ascii", "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
                                  "\N{CJK UNIFIED IDEOGRAPH-4E2D}\N{CJK UNIFIED IDEOGRAPH-6587}"])
def test_escape_audit_detail_keeps_printable_text(text: str) -> None:
    assert dispatch_mod.escape_audit_detail(text) == text


def test_cli_cleanup_failure_print_is_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """R3166-04: on a failed reset the CLI loop printed the error unescaped
    right after the escaped audit line, so its control characters could
    redraw that line."""
    monkeypatch.setattr(dispatch_mod, "log_gate_audit", lambda *args, **kwargs: None)

    async def on_branch(project_dir: str, task_branch: str) -> str:
        return "0" * 40

    async def attempt(task, project_dir, project_context, args, output=None):
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    async def failing_cleanup(*args: Any, **kwargs: Any) -> None:
        raise dispatch_mod.AttemptCleanupError(f"reset failed: {DETAIL}\r[GATE-AUDIT] forged")

    monkeypatch.setattr(cli_mod, "_require_task_branch", on_branch)
    monkeypatch.setattr(cli_mod, "run_dev_test_loop", attempt)
    monkeypatch.setattr(cli_mod, "cleanup_failed_attempt", failing_cleanup)
    args = SimpleNamespace(dispatch_config={
        "features": {"autoresearch": True}, "autoresearch_max_retries": 1,
    })

    _, _, outcome = asyncio.run(cli_mod._run_dev_test_mode(
        _task(TASK_ID), str(tmp_path), {}, args, task_branch=f"forge-task-{TASK_ID}",
    ))

    printed = capsys.readouterr().out
    assert outcome == "attempt_cleanup_failed"
    # The CR is whitespace, so it is collapsed to a space like a newline.
    [line] = [text for text in printed.split("\n") if "resetting the failed attempt failed" in text]
    assert _format_characters(line) == [], ascii(line)
    assert "\\u202e" in line and "\\x1b[1A done [GATE-AUDIT] forged" in line, line
