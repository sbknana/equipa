"""Regression tests for _handle_dev_continuation.

Covers timeout/max-turns handling that was previously inlined in
run_dev_test_loop. The helper drives:
  - hard checkpoint save + on_checkpoint hook
  - compaction-recovery context injection
  - auto-continue vs exhausted-continuations exit

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from equipa.constants import MAX_CONTINUATIONS
from equipa.loops import _handle_dev_continuation


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def patch_helpers(monkeypatch, tmp_path):
    """Stub out checkpoint/hook side-effects so tests stay hermetic."""
    saved = {}
    monkeypatch.setattr(
        "equipa.loops.save_checkpoint",
        lambda tid, attempt, text, role: tmp_path / f"cp-{tid}-{attempt}.md",
    )
    monkeypatch.setattr(
        "equipa.loops.fire_hook",
        _async_noop,
    )
    monkeypatch.setattr(
        "equipa.loops.load_soft_checkpoint",
        lambda task_id, role: saved.get("soft_cp"),
    )
    monkeypatch.setattr(
        "equipa.loops._load_forge_state_json",
        lambda project_dir: saved.get("forge_state"),
    )
    monkeypatch.setattr(
        "equipa.loops.build_compaction_recovery_context",
        lambda soft_cp, forge_state: "## Recovery context\nfrom soft cp",
    )
    monkeypatch.setattr(
        "equipa.loops.build_checkpoint_context",
        lambda text, attempt: f"## Checkpoint attempt {attempt}\n{text[:50]}",
    )
    return saved


async def _async_noop(*args, **kwargs):
    return None


class TestProceedWhenNoTimeout:
    def test_no_errors_returns_proceed(self):
        history: list[str] = []
        action, count, err_type, outcome = _run(
            _handle_dev_continuation(
                dev_result={"errors": []}, task_id=1, task_role="developer",
                cycle=1, prev_attempt=0, project_dir="/tmp",
                continuation_count=0, compaction_history=history,
            )
        )
        assert action == "proceed"
        assert count == 0
        assert err_type is None
        assert outcome is None
        assert history == []

    def test_other_error_returns_proceed(self):
        history: list[str] = []
        action, *_ = _run(
            _handle_dev_continuation(
                dev_result={"errors": ["some random failure"]}, task_id=1,
                task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp", continuation_count=0,
                compaction_history=history,
            )
        )
        assert action == "proceed"
        assert history == []


class TestContinueOnTimeout:
    def test_timeout_under_limit_returns_continue(self):
        history: list[str] = []
        action, count, err_type, outcome = _run(
            _handle_dev_continuation(
                dev_result={
                    "errors": ["agent timed out after 600s"],
                    "result_text": "some progress so far",
                },
                task_id=42, task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp", continuation_count=0,
                compaction_history=history,
            )
        )
        assert action == "continue"
        assert count == 1
        assert err_type == "timeout"
        assert outcome is None
        # Plain checkpoint context appended (no compaction signal)
        assert any("Checkpoint attempt 1" in e for e in history)

    def test_max_turns_under_limit_returns_continue(self):
        history: list[str] = []
        action, count, err_type, outcome = _run(
            _handle_dev_continuation(
                dev_result={
                    "errors": ["max turns hit"],
                    "result_text": "wip",
                },
                task_id=42, task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp", continuation_count=0,
                compaction_history=history,
            )
        )
        assert action == "continue"
        assert err_type == "max_turns"

    def test_compaction_with_soft_cp_uses_recovery_context(self, patch_helpers):
        patch_helpers["soft_cp"] = {"some": "soft cp data"}
        history: list[str] = []
        _run(
            _handle_dev_continuation(
                dev_result={
                    "errors": ["timed out"],
                    "result_text": "wip",
                    "compaction_count": 2,
                },
                task_id=1, task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp", continuation_count=0,
                compaction_history=history,
            )
        )
        assert any("Recovery context" in e for e in history)
        assert not any("Checkpoint attempt" in e for e in history)

    def test_compaction_without_soft_cp_falls_back_to_checkpoint(self):
        history: list[str] = []
        _run(
            _handle_dev_continuation(
                dev_result={
                    "errors": ["timed out"],
                    "result_text": "wip text",
                    "compaction_count": 1,
                },
                task_id=1, task_role="developer", cycle=2, prev_attempt=3,
                project_dir="/tmp", continuation_count=0,
                compaction_history=history,
            )
        )
        # prev_attempt(3) + cycle(2) = 5
        assert any("Checkpoint attempt 5" in e for e in history)


class TestExitWhenExhausted:
    def test_exhausted_returns_exit_with_correct_outcome(self):
        history: list[str] = []
        action, count, err_type, outcome = _run(
            _handle_dev_continuation(
                dev_result={"errors": ["timed out"], "result_text": "x"},
                task_id=1, task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp",
                continuation_count=MAX_CONTINUATIONS - 1,  # +1 inside -> exhausted
                compaction_history=history,
            )
        )
        assert action == "exit"
        assert count == MAX_CONTINUATIONS
        assert outcome == "developer_timeout"

    def test_exhausted_max_turns_outcome(self):
        history: list[str] = []
        action, _, _, outcome = _run(
            _handle_dev_continuation(
                dev_result={"errors": ["max turns"], "result_text": "x"},
                task_id=1, task_role="developer", cycle=1, prev_attempt=0,
                project_dir="/tmp",
                continuation_count=MAX_CONTINUATIONS - 1,
                compaction_history=history,
            )
        )
        assert action == "exit"
        assert outcome == "developer_max_turns"
