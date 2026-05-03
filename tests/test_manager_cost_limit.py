"""Tests for CB-01: aggregate cost-limit enforcement in run_manager_loop.

Without this guard, a pathological evaluator that keeps returning needs_more
plus high-cost dev-test cycles can run unbounded across manager rounds.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from equipa import manager
from equipa.constants import MANAGER_COST_LIMIT


def _make_args(max_rounds: int = 5, manager_cost_limit: float | None = None) -> SimpleNamespace:
    """Build a minimal args namespace for run_manager_loop."""
    return SimpleNamespace(
        max_rounds=max_rounds,
        manager_cost_limit=(
            manager_cost_limit if manager_cost_limit is not None else MANAGER_COST_LIMIT
        ),
        max_turns=20,
        model="sonnet",
    )


def test_manager_loop_aborts_when_cost_limit_reached(monkeypatch):
    """A high-cost dev-test cycle plus needs_more evaluator must be cut off
    once total_cost crosses manager_cost_limit — instead of looping forever.
    """
    rounds_observed: list[int] = []
    planner_calls: list[int] = []

    async def fake_planner(goal, project_id, project_dir, project_context, args, output=None):
        planner_calls.append(1)
        return ({"cost": 1.0, "duration": 1.0}, [101])

    async def fake_evaluator(
        goal, project_id, project_dir, project_context,
        all_completed, all_blocked, args, output=None,
    ):
        return (
            {"cost": 1.0, "duration": 1.0},
            {"goal_status": "needs_more", "tasks_created": [], "evaluation": "more", "blockers": ""},
        )

    async def fake_dev_test(task, project_dir, project_context, args, output=None):
        rounds_observed.append(task["id"])
        return ({"cost": 12.0, "duration": 5.0}, 1, "tests_passed")

    def fake_fetch_tasks(task_ids):
        return [{"id": tid, "title": f"task {tid}"} for tid in task_ids]

    def fake_get_task_status(task_id):
        return "open"

    def fake_update_task_status(task_id, outcome, output=None):
        return None

    monkeypatch.setattr(manager, "run_planner_agent", fake_planner)
    monkeypatch.setattr(manager, "run_evaluator_agent", fake_evaluator)
    monkeypatch.setattr(manager, "run_dev_test_loop", fake_dev_test)
    monkeypatch.setattr(manager, "fetch_tasks_by_ids", fake_fetch_tasks)
    monkeypatch.setattr(manager, "_get_task_status", fake_get_task_status)
    monkeypatch.setattr(manager, "update_task_status", fake_update_task_status)

    args = _make_args(max_rounds=10, manager_cost_limit=20.0)

    outcome, rounds, completed, blocked, total_cost, total_duration = asyncio.run(
        manager.run_manager_loop(
            goal="test goal",
            project_id=1,
            project_dir="/tmp",
            project_context={},
            args=args,
            output=None,
        )
    )

    assert outcome == "cost_limit_exceeded", (
        f"Expected cost_limit_exceeded, got {outcome!r} after {rounds} rounds, "
        f"total_cost=${total_cost:.2f}"
    )
    assert rounds < 10, "Loop must abort before max_rounds when cost limit is hit"
    assert total_cost >= 20.0
    # Each round contributes 1 (planner) + 12 (dev_test) + 1 (evaluator) = 14.
    # After round 1, total=14 (< 20, continue). After round 2, total=28 (>= 20, abort).
    assert len(planner_calls) == 2


def test_manager_loop_respects_default_cost_limit(monkeypatch):
    """When args lacks manager_cost_limit, the MANAGER_COST_LIMIT default applies."""

    async def fake_planner(goal, project_id, project_dir, project_context, args, output=None):
        return ({"cost": 100.0, "duration": 1.0}, [201])

    async def fake_evaluator(
        goal, project_id, project_dir, project_context,
        all_completed, all_blocked, args, output=None,
    ):
        return (
            {"cost": 0.0, "duration": 0.0},
            {"goal_status": "needs_more", "tasks_created": [], "evaluation": "", "blockers": ""},
        )

    async def fake_dev_test(task, project_dir, project_context, args, output=None):
        return ({"cost": 0.0, "duration": 0.0}, 1, "tests_passed")

    monkeypatch.setattr(manager, "run_planner_agent", fake_planner)
    monkeypatch.setattr(manager, "run_evaluator_agent", fake_evaluator)
    monkeypatch.setattr(manager, "run_dev_test_loop", fake_dev_test)
    monkeypatch.setattr(manager, "fetch_tasks_by_ids", lambda ids: [{"id": i, "title": "t"} for i in ids])
    monkeypatch.setattr(manager, "_get_task_status", lambda tid: "open")
    monkeypatch.setattr(manager, "update_task_status", lambda *a, **kw: None)

    args = SimpleNamespace(max_rounds=5, max_turns=20, model="sonnet")

    outcome, rounds, completed, blocked, total_cost, total_duration = asyncio.run(
        manager.run_manager_loop(
            goal="g", project_id=1, project_dir="/tmp",
            project_context={}, args=args, output=None,
        )
    )

    assert outcome == "cost_limit_exceeded"
    assert total_cost >= MANAGER_COST_LIMIT
    assert rounds == 1


def test_manager_loop_under_cost_limit_completes_normally(monkeypatch):
    """When costs stay under the limit, the loop must complete via goal_complete
    and never trip the cost-limit branch."""

    async def fake_planner(goal, project_id, project_dir, project_context, args, output=None):
        return ({"cost": 0.5, "duration": 1.0}, [301])

    async def fake_evaluator(
        goal, project_id, project_dir, project_context,
        all_completed, all_blocked, args, output=None,
    ):
        return (
            {"cost": 0.5, "duration": 1.0},
            {"goal_status": "complete", "tasks_created": [], "evaluation": "done", "blockers": ""},
        )

    async def fake_dev_test(task, project_dir, project_context, args, output=None):
        return ({"cost": 1.0, "duration": 1.0}, 1, "tests_passed")

    monkeypatch.setattr(manager, "run_planner_agent", fake_planner)
    monkeypatch.setattr(manager, "run_evaluator_agent", fake_evaluator)
    monkeypatch.setattr(manager, "run_dev_test_loop", fake_dev_test)
    monkeypatch.setattr(manager, "fetch_tasks_by_ids", lambda ids: [{"id": i, "title": "t"} for i in ids])
    monkeypatch.setattr(manager, "_get_task_status", lambda tid: "open")
    monkeypatch.setattr(manager, "update_task_status", lambda *a, **kw: None)

    args = _make_args(max_rounds=3, manager_cost_limit=30.0)

    outcome, rounds, completed, blocked, total_cost, total_duration = asyncio.run(
        manager.run_manager_loop(
            goal="g", project_id=1, project_dir="/tmp",
            project_context={}, args=args, output=None,
        )
    )

    assert outcome == "goal_complete"
    assert rounds == 1
    assert total_cost < 30.0
