"""Tests for calculate_dynamic_budget effort scaling.

Background: --effort high makes each Claude turn more thoughtful but does
not change the turn count. Without budget scaling, high-effort agents run
out of turns mid-task. calculate_dynamic_budget(max_turns, effort=...)
multiplies the budget by EFFORT_BUDGET_MULTIPLIERS to compensate.
"""

from __future__ import annotations

import pytest

from equipa.constants import (
    DYNAMIC_BUDGET_MIN_TURNS,
    DYNAMIC_BUDGET_START_RATIO,
    EFFORT_BUDGET_MULTIPLIERS,
)
from equipa.monitoring import calculate_dynamic_budget


class TestDefaultBehavior:
    """Behavior with no effort or effort=None must match the pre-fix path."""

    def test_no_effort_arg_unchanged(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20)
        assert max_turns == 20
        # 20 * 0.8 = 16, above the 15 minimum
        assert starting == 16

    def test_effort_none_unchanged(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort=None)
        assert max_turns == 20
        assert starting == 16

    def test_effort_default_string_unchanged(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="default")
        assert max_turns == 20
        assert starting == 16

    def test_min_floor_still_applied(self) -> None:
        # 5 * 0.8 = 4, below the 15 floor → starting clamped to min(15, 5)
        starting, max_turns = calculate_dynamic_budget(5)
        assert max_turns == 5
        assert starting == min(DYNAMIC_BUDGET_MIN_TURNS, 5)


class TestEffortScaling:
    """Effort multiplies both the effective max and the starting budget."""

    def test_high_boosts_max_by_1_5x(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="high")
        assert max_turns == 30  # 20 * 1.5
        # 30 * 0.8 = 24
        assert starting == 24

    def test_xhigh_doubles_max(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="xhigh")
        assert max_turns == 40  # 20 * 2.0
        assert starting == 32   # 40 * 0.8

    def test_max_effort_2_5x(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="max")
        assert max_turns == 50  # 20 * 2.5
        assert starting == 40   # 50 * 0.8

    def test_low_reduces_budget(self) -> None:
        starting, max_turns = calculate_dynamic_budget(40, effort="low")
        assert max_turns == 28  # 40 * 0.7
        # 28 * 0.8 = 22.4 → 22, above min floor
        assert starting == 22

    def test_high_effort_strictly_greater_than_default(self) -> None:
        default_start, default_max = calculate_dynamic_budget(20)
        high_start, high_max = calculate_dynamic_budget(20, effort="high")
        assert high_max > default_max
        assert high_start > default_start


class TestUnknownEffortFallback:
    """Unknown effort strings must fall back to multiplier 1.0, not crash."""

    @pytest.mark.parametrize("bogus", ["ultra", "extreme", "", "none"])
    def test_unknown_effort_acts_like_default(self, bogus: str) -> None:
        if bogus == "":
            # empty string is falsy → treated as no effort
            expected_start, expected_max = calculate_dynamic_budget(20)
        else:
            expected_start, expected_max = calculate_dynamic_budget(20)
        starting, max_turns = calculate_dynamic_budget(20, effort=bogus)
        assert (starting, max_turns) == (expected_start, expected_max)

    def test_unknown_effort_does_not_raise(self) -> None:
        # Should not raise KeyError or similar
        calculate_dynamic_budget(20, effort="garbage-value-xyz")


class TestCaseInsensitive:
    """dispatch_config keys may be capitalized; matching must tolerate that."""

    def test_uppercase_high(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="HIGH")
        assert max_turns == 30
        assert starting == 24

    def test_mixed_case_xhigh(self) -> None:
        starting, max_turns = calculate_dynamic_budget(20, effort="XHigh")
        assert max_turns == 40


class TestMultiplierTable:
    """Pin the published multiplier table so future edits surface as test failures."""

    def test_known_keys(self) -> None:
        assert EFFORT_BUDGET_MULTIPLIERS["low"] == 0.7
        assert EFFORT_BUDGET_MULTIPLIERS["default"] == 1.0
        assert EFFORT_BUDGET_MULTIPLIERS["high"] == 1.5
        assert EFFORT_BUDGET_MULTIPLIERS["xhigh"] == 2.0
        assert EFFORT_BUDGET_MULTIPLIERS["max"] == 2.5

    def test_start_ratio_unchanged(self) -> None:
        # Sanity: scaling relies on the start ratio still being ≤ 1.
        assert 0 < DYNAMIC_BUDGET_START_RATIO <= 1.0
