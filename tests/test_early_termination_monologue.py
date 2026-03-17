#!/usr/bin/env python3
"""
Test suite for monologue detection in forge_orchestrator.py.

Tests the _check_monologue helper function which detects when an agent
sends 3+ consecutive assistant messages with NO tool calls (pure text/reasoning).
This catches "thinking in circles" — a stuck pattern.

Copyright 2026 Forgeborn
"""

import sys
from pathlib import Path

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent.parent))

from forge_orchestrator import (
    _check_stuck_phrases,
    _check_monologue,
    MONOLOGUE_THRESHOLD,
    MONOLOGUE_EXEMPT_TURNS,
)


# --- Tests for _check_monologue (consecutive text-only turns) ---

def test_monologue_detected_at_3_consecutive_text_only():
    """Test that 3 consecutive text-only turns trigger termination after exempt period."""
    action = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=10,
    )
    assert action == "terminate", (
        f"expected 'terminate' at {MONOLOGUE_THRESHOLD} text-only turns, got '{action}'"
    )

    # Also test with higher counts
    action2 = _check_monologue(consecutive_text_only_turns=5, turn_count=15)
    assert action2 == "terminate", f"expected 'terminate' at 5 text-only turns, got '{action2}'"


def test_monologue_resets_on_tool_use():
    """Test that monologue counter of 0 (after reset) does not trigger."""
    action = _check_monologue(consecutive_text_only_turns=0, turn_count=10)
    assert action is None, f"expected None for 0 text-only turns, got '{action}'"

    action2 = _check_monologue(consecutive_text_only_turns=1, turn_count=10)
    assert action2 is None, f"expected None for 1 text-only turn, got '{action2}'"


def test_monologue_exempt_first_5_turns():
    """Test that monologue detection does NOT trigger during the first 5 turns."""
    for turn in range(1, MONOLOGUE_EXEMPT_TURNS + 1):
        action = _check_monologue(
            consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
            turn_count=turn,
        )
        assert action is None, (
            f"should NOT trigger at turn {turn} (exempt period), got '{action}'"
        )

    # But turn MONOLOGUE_EXEMPT_TURNS + 1 should trigger
    action_after = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=MONOLOGUE_EXEMPT_TURNS + 1,
    )
    assert action_after == "terminate", (
        f"should trigger at turn {MONOLOGUE_EXEMPT_TURNS + 1}, got '{action_after}'"
    )


def test_monologue_warning_at_2():
    """Test that 2 consecutive text-only turns emit a warning (not termination)."""
    action = _check_monologue(
        consecutive_text_only_turns=2,
        turn_count=10,
    )
    assert action == "warn", f"expected 'warn' at 2 text-only turns, got '{action}'"


def test_monologue_does_not_interfere_with_stuck_phrases():
    """Test that monologue detection is independent of stuck phrase detection."""
    action = _check_monologue(consecutive_text_only_turns=0, turn_count=10)
    assert action is None, "monologue should be None when no text-only turns"

    stuck = _check_stuck_phrases("I am unable to proceed with this task")
    assert stuck is not None, "stuck phrase should still be detected independently"

    action2 = _check_monologue(
        consecutive_text_only_turns=MONOLOGUE_THRESHOLD,
        turn_count=10,
    )
    assert action2 == "terminate", "monologue should still trigger independently"


def test_monologue_constants():
    """Test that monologue constants have expected values."""
    assert MONOLOGUE_THRESHOLD == 3, (
        f"MONOLOGUE_THRESHOLD should be 3, got {MONOLOGUE_THRESHOLD}"
    )
    assert MONOLOGUE_EXEMPT_TURNS == 5, (
        f"MONOLOGUE_EXEMPT_TURNS should be 5, got {MONOLOGUE_EXEMPT_TURNS}"
    )
    assert MONOLOGUE_THRESHOLD > 0, "threshold must be positive"
    assert MONOLOGUE_EXEMPT_TURNS > 0, "exempt turns must be positive"


def test_monologue_below_threshold_no_action():
    """Test that counts below warning threshold return None."""
    action = _check_monologue(consecutive_text_only_turns=1, turn_count=10)
    assert action is None, f"expected None for 1 text-only turn, got '{action}'"


# --- Main ---

def main():
    tests = [
        test_monologue_detected_at_3_consecutive_text_only,
        test_monologue_resets_on_tool_use,
        test_monologue_exempt_first_5_turns,
        test_monologue_warning_at_2,
        test_monologue_does_not_interfere_with_stuck_phrases,
        test_monologue_constants,
        test_monologue_below_threshold_no_action,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print(f"  Monologue Detection Test Suite")
    print(f"  Testing _check_monologue for consecutive text-only turns")
    print(f"{'=' * 60}\n")

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
            print(f"  PASS: {test_fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL: {test_fn.__name__}: {e}")
            failed += 1
            errors.append(test_fn.__name__)
        except Exception as e:
            print(f"  ERROR: {test_fn.__name__}: {e}")
            failed += 1
            errors.append(f"{test_fn.__name__} (exception: {e})")

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print(f"  Failed tests: {', '.join(errors)}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
