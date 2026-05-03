#!/usr/bin/env python3
"""Regression tests for `_check_dev_progress` extracted from `run_dev_test_loop`.

Covers the four logical branches:
1. Files changed -> made progress -> reset no_progress_count and last_error_type.
2. No files but >=3 turns -> still progress (anti-paralysis safety net).
3. Idle cycle with accumulated files / branch commits -> "continue", do not penalise.
4. Idle cycle with nothing accumulated -> increment counter; "block" once
   NO_PROGRESS_LIMIT is hit.

Copyright 2026 Forgeborn
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from equipa import loops
from equipa.constants import NO_PROGRESS_LIMIT


def _silent(*_args, **_kwargs):
    """No-op log sink; tests assert behaviour, not logging."""
    return None


def test_files_changed_resets_counter_and_error_type():
    """Files-changed cycle resets no_progress_count to 0 and signals error reset."""
    accumulated: set[str] = set()
    dev_result = {
        "result_text": "FILES_CHANGED:\n- equipa/loops.py\n- tests/foo.py",
        "num_turns": 5,
    }
    with patch.object(loops, "log", _silent):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated, no_progress_count=2,
            project_dir="/tmp", cycle=3, output=None,
        )
    assert action == "continue"
    assert npc == 0
    assert reset is None  # signal to caller: clear last_error_type
    assert accumulated == {"equipa/loops.py", "tests/foo.py"}


def test_three_turns_no_files_still_counts_as_progress():
    """Anti-paralysis safety net: >=3 turns without FILES_CHANGED still passes."""
    accumulated: set[str] = set()
    dev_result = {"result_text": "no files marker here", "num_turns": 3}
    with patch.object(loops, "log", _silent):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated, no_progress_count=1,
            project_dir="/tmp", cycle=1, output=None,
        )
    assert action == "continue"
    assert npc == 0
    assert reset is None
    assert accumulated == set()


def test_idle_cycle_with_accumulated_files_does_not_penalise():
    """Prior-cycle progress shields current idle cycle from counter increment."""
    accumulated = {"foo.py"}
    dev_result = {"result_text": "", "num_turns": 1}
    with patch.object(loops, "log", _silent), \
         patch.object(loops, "has_branch_commits", return_value=False):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated, no_progress_count=1,
            project_dir="/tmp", cycle=2, output=None,
        )
    assert action == "continue"
    assert npc == 1  # unchanged
    assert reset == ""  # do NOT reset last_error_type


def test_idle_cycle_with_branch_commits_does_not_penalise():
    """Branch-commit history also shields against penalty (worktree pre-commits)."""
    accumulated: set[str] = set()
    dev_result = {"result_text": "", "num_turns": 1}
    with patch.object(loops, "log", _silent), \
         patch.object(loops, "has_branch_commits", return_value=True):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated, no_progress_count=0,
            project_dir="/tmp", cycle=2, output=None,
        )
    assert action == "continue"
    assert npc == 0


def test_idle_cycle_with_nothing_accumulated_increments_counter():
    """Truly-idle cycle increments counter but does not block until limit."""
    accumulated: set[str] = set()
    dev_result = {"result_text": "", "num_turns": 1}
    with patch.object(loops, "log", _silent), \
         patch.object(loops, "has_branch_commits", return_value=False):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated, no_progress_count=0,
            project_dir="/tmp", cycle=1, output=None,
        )
    assert action == "continue"
    assert npc == 1
    assert reset == ""


def test_idle_cycles_at_limit_returns_block():
    """Hitting NO_PROGRESS_LIMIT returns 'block' so caller exits with no_progress."""
    accumulated: set[str] = set()
    dev_result = {"result_text": "", "num_turns": 1}
    with patch.object(loops, "log", _silent), \
         patch.object(loops, "has_branch_commits", return_value=False):
        action, npc, reset = loops._check_dev_progress(
            dev_result, accumulated,
            no_progress_count=NO_PROGRESS_LIMIT - 1,
            project_dir="/tmp", cycle=4, output=None,
        )
    assert action == "block"
    assert npc == NO_PROGRESS_LIMIT


def test_files_changed_accumulates_across_calls():
    """Same accumulated_files set accumulates across multiple cycles."""
    accumulated: set[str] = set()
    with patch.object(loops, "log", _silent):
        loops._check_dev_progress(
            {"result_text": "FILES_CHANGED:\n- a.py", "num_turns": 5},
            accumulated, 0, "/tmp", 1, None,
        )
        loops._check_dev_progress(
            {"result_text": "FILES_CHANGED:\n- b.py\n- c.py", "num_turns": 4},
            accumulated, 0, "/tmp", 2, None,
        )
    assert accumulated == {"a.py", "b.py", "c.py"}
