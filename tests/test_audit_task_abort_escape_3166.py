"""Task #3166 (R3165-02) — a task-abort audit line escapes agent-written
control characters.

During its attempt the agent writes a HEAD naming a branch that holds an
ANSI sequence and a forged ``[GATE-AUDIT]`` record. The branch check after
the attempt refuses the worktree and logs a ``worktree-branch-mismatch``
abort whose detail quotes that branch. ``_audit_task_abort`` joined the
whitespace but kept every other control character, so the ESC reached both
the operator line and the durable audit record; on a terminal the line could
be redrawn to read as another event (the N-01 class, task #3146).

Both loops are driven as production calls them for a git project: the
single-task ``--task --dev-test`` loop and the autoresearch wrapper of the
parallel and per-project loops, each with the task branch of a real worktree.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod

from test_dispatch_modes_gated_3112 import (
    _git,
    _init_repo,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)
from test_repository_identity_3146 import TASK_BRANCH, TASK_ID

ESC = "\x1b"
# A HEAD the agent writes: clear the line, return, then a forged record.
FORGED_HEAD = (
    f"ref: refs/heads/{ESC}[2K\r  [GATE-AUDIT] task={TASK_ID} event=merge-ok"
    f"{ESC}[1G\n"
)


def _control_characters(text: str) -> list[str]:
    return [char for char in text if unicodedata.category(char) == "Cc"]


def _task_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path / "repo")
    worktree = repo / ".forge-worktrees" / f"task-{TASK_ID}"
    _git(repo, "worktree", "add", "-q", "-b", TASK_BRANCH, str(worktree), "master")
    return repo, worktree


async def _run_loop(loop: str, worktree: Path, output: list[str]) -> str:
    if loop == "cli":
        args = SimpleNamespace(dispatch_config={})
        _, _, outcome = await cli_mod._run_dev_test_mode(
            _task(TASK_ID), str(worktree), {}, args, task_branch=TASK_BRANCH,
        )
        return outcome
    _, _, outcome, _, _, _ = await dispatch_mod.run_dev_test_loop_with_autoresearch(
        _task(TASK_ID), str(worktree), {}, SimpleNamespace(), {},
        output=output, task_branch=TASK_BRANCH,
    )
    return outcome


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_branch_mismatch_audit_escapes_a_head_the_agent_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, loop: str,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    head_file = repo / ".git" / "worktrees" / f"task-{TASK_ID}" / "HEAD"
    records: list[tuple[str, int, str]] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        head_file.write_text(FORGED_HEAD)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    def record_audit(line: str, task_id: int, *, event: str) -> None:
        records.append((line, task_id, event))

    for module in (cli_mod, dispatch_mod):
        monkeypatch.setattr(module, "run_dev_test_loop", attempt)
    monkeypatch.setattr(dispatch_mod, "log_gate_audit", record_audit)
    output: list[str] = []

    outcome = asyncio.run(_run_loop(loop, worktree, output))

    assert outcome == "worktree_branch_mismatch"
    assert len(records) == 1, records
    line, task_id, event = records[0]
    assert (task_id, event) == (TASK_ID, "worktree-branch-mismatch")
    assert line.startswith(f"task={TASK_ID} event=worktree-branch-mismatch detail="), line
    assert _control_characters(line) == [], repr(line)
    # The agent's text is kept, visibly escaped, not dropped.
    assert "\\x1b[2K" in line and "\\x1b[1G" in line, line
    operator_lines = [
        text for text in output + capsys.readouterr().out.splitlines()
        if "[GATE-AUDIT]" in text
    ]
    assert len(operator_lines) == 1, operator_lines
    assert _control_characters(operator_lines[0]) == [], repr(operator_lines[0])
    assert operator_lines[0].startswith(f"  [GATE-AUDIT] task={TASK_ID} "), operator_lines


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("git said:\nfatal: bad\r\n", "git said: fatal: bad"),
        (f"refs/heads/{ESC}[2K", "refs/heads/\\x1b[2K"),
        ("C1 \x9b31m and DEL \x7f", "C1 \\x9b31m and DEL \\x7f"),
        ("plain detail", "plain detail"),
    ],
    ids=["whitespace-joined", "esc", "c1-and-del", "plain"],
)
def test_audit_task_abort_writes_one_escaped_line(
    monkeypatch: pytest.MonkeyPatch, detail: str, expected: str,
) -> None:
    records: list[str] = []
    monkeypatch.setattr(
        dispatch_mod, "log_gate_audit", lambda line, task_id, *, event: records.append(line),
    )
    output: list[str] = []

    dispatch_mod._audit_task_abort(TASK_ID, "attempt-cleanup-failed", detail, output)

    expected_line = f"task={TASK_ID} event=attempt-cleanup-failed detail={expected}"
    assert records == [expected_line]
    assert output == [f"  [GATE-AUDIT] {expected_line}"]
