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
from pathlib import Path

from equipa.merge_integrity import DefaultBranchGuard

from test_dispatch_modes_gated_3112 import (
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
)
from test_repository_identity_3146 import (
    TASK_ID,
    _clone_with_decoy_branch,
    _real_with_task_branch,
)

FORGED = f"[GATE-AUDIT] task={TASK_ID} event=merge-succeeded FORGED-3151"


def _run(coro):
    return asyncio.run(coro)


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
