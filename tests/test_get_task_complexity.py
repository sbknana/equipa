"""Tests for equipa.tasks.get_task_complexity.

Covers the three layers of complexity resolution:
  1. Length-based fallback (legacy behavior).
  2. Site-count escalation from description scope hints.
  3. Wave-prefix title override.
"""

from __future__ import annotations

import pytest

from equipa.tasks import get_task_complexity


def _task(*, title: str = "", description: str = "", complexity: str | None = None) -> dict:
    task: dict = {"title": title, "description": description}
    if complexity is not None:
        task["complexity"] = complexity
    return task


# --- Length-based fallback (preserved when no scope hints / wave prefix) ---


@pytest.mark.parametrize(
    "desc_len,expected",
    [
        (50, "simple"),
        (200, "medium"),
        (500, "complex"),
        (1200, "epic"),
    ],
)
def test_length_fallback(desc_len: int, expected: str) -> None:
    desc = "x" * desc_len
    assert get_task_complexity(_task(description=desc)) == expected


def test_explicit_db_value_used_without_clues() -> None:
    # Short description, no scope hints, no wave prefix — explicit wins.
    assert get_task_complexity(_task(description="short", complexity="complex")) == "complex"


def test_none_task_returns_simple() -> None:
    assert get_task_complexity(None) == "simple"


# --- Site-count escalation ---


def test_site_count_escalates_simple_to_medium() -> None:
    desc = "Refactor 7 pairs in the dispatch path."
    assert get_task_complexity(_task(description=desc)) == "medium"


def test_site_count_escalates_to_complex() -> None:
    desc = "Migrate across 22 files in equipa/."
    assert get_task_complexity(_task(description=desc)) == "complex"


def test_site_count_escalates_to_epic() -> None:
    desc = "Audit 78 pairs spanning the orchestrator."
    assert get_task_complexity(_task(description=desc)) == "epic"


def test_explicit_simple_overridden_by_site_count() -> None:
    # Even with explicit DB value of "simple", a 78-site description forces epic.
    desc = "Touch 78 call sites that need migration."
    assert get_task_complexity(_task(description=desc, complexity="simple")) == "epic"


def test_low_site_count_does_not_escalate() -> None:
    # 2 sites is below the 4-site floor; length classification (simple) wins.
    desc = "Fix 2 places that mishandle None."
    assert get_task_complexity(_task(description=desc)) == "simple"


def test_max_site_count_used_when_multiple_hints() -> None:
    desc = "Fix 5 files now and audit 60 call sites later."
    assert get_task_complexity(_task(description=desc)) == "epic"


def test_plus_suffix_pattern_matches() -> None:
    desc = "Migrate 20+ git calls to async."
    assert get_task_complexity(_task(description=desc)) == "complex"


def test_explicit_higher_than_site_count_floor_kept() -> None:
    # Explicit "epic" must not be downgraded by a small site count.
    desc = "Touch 5 files."
    assert get_task_complexity(_task(description=desc, complexity="epic")) == "epic"


# --- Wave-prefix override ---


@pytest.mark.parametrize("prefix", ["P1", "P5", "M1", "M9", "D1", "D9"])
def test_wave_prefix_forces_minimum_medium(prefix: str) -> None:
    task = _task(title=f"{prefix} Refactor something", description="tiny")
    assert get_task_complexity(task) == "medium"


def test_wave_prefix_does_not_downgrade_higher_complexity() -> None:
    desc = "Audit 78 files."  # forces epic via site count
    task = _task(title="P2 Big refactor", description=desc)
    assert get_task_complexity(task) == "epic"


@pytest.mark.parametrize("prefix", ["P0", "P6", "M0", "D0", "X1", "PP1"])
def test_non_wave_prefix_does_not_override(prefix: str) -> None:
    task = _task(title=f"{prefix} something", description="tiny")
    assert get_task_complexity(task) == "simple"


def test_wave_prefix_combined_with_explicit_simple() -> None:
    task = _task(title="M3 small change", description="x", complexity="simple")
    assert get_task_complexity(task) == "medium"
