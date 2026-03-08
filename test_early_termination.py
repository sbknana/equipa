#!/usr/bin/env python3
"""
Test suite for early termination signals in forge_orchestrator.py.

Tests verify:
1. _check_stuck_phrases detects stuck signal phrases correctly
2. _check_stuck_phrases is case-insensitive
3. _check_stuck_phrases returns None for clean text
4. _check_repeated_tool_calls detects 4 identical consecutive calls
5. _check_repeated_tool_calls returns False for varied calls
6. _check_repeated_tool_calls handles short history gracefully
7. EARLY_TERM_EXEMPT_ROLES includes all expected roles
8. EARLY_TERM_WARN_TURNS, EARLY_TERM_FINAL_WARN_TURNS, and EARLY_TERM_KILL_TURNS have correct values
9. _detect_tool_loop integrates correctly with early termination thresholds
10. Escalating warning thresholds are ordered correctly (warn < final_warn < kill)

Copyright 2026 Forgeborn
"""

import sys
from pathlib import Path

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent))

from forge_orchestrator import (
    _check_stuck_phrases,
    _check_repeated_tool_calls,
    _detect_tool_loop,
    EARLY_TERM_WARN_TURNS,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_STUCK_PHRASES,
    EARLY_TERM_EXEMPT_ROLES,
)


# --- Tests for _check_stuck_phrases ---

def test_stuck_phrases_detects_each():
    """Test that each stuck phrase triggers a match."""
    for phrase in EARLY_TERM_STUCK_PHRASES:
        text = f"Here is some context. {phrase} So I will stop."
        matched = _check_stuck_phrases(text)
        assert matched is not None, f"no match for text containing '{phrase}'"
        # The match might be a shorter phrase that is a substring
        # (e.g., "i cannot" matches before "i cannot complete this task")
        # That's OK — we just need SOME match to fire
        assert matched in text.lower(), f"matched '{matched}' is not in text"


def test_stuck_phrases_case_insensitive():
    """Test that matching is case-insensitive."""
    result = _check_stuck_phrases("I AM UNABLE TO complete this task")
    assert result is not None, "should have matched uppercase 'I AM UNABLE TO'"

    result2 = _check_stuck_phrases("I Cannot access the file")
    assert result2 is not None, "should have matched mixed-case 'I Cannot'"


def test_stuck_phrases_returns_none_for_clean_text():
    """Test that clean text returns None."""
    clean_texts = [
        "I have successfully completed the task.",
        "The file was written to disk.",
        "Running tests now.",
        "Edit the configuration file.",
        "",
    ]
    for text in clean_texts:
        result = _check_stuck_phrases(text)
        assert result is None, f"expected None for '{text}', got '{result}'"


def test_stuck_phrases_partial_match():
    """Test that phrases embedded in longer words still match (substring match)."""
    # "i cannot" is in the phrase list - should match even in longer sentence
    result = _check_stuck_phrases("unfortunately i cannot proceed with this")
    assert result is not None, "should have matched 'i cannot' in sentence"


def test_stuck_phrases_returns_first_match():
    """Test that the first matching phrase is returned when multiple match."""
    # Text contains multiple stuck phrases
    text = "i am unable to do this and i'm stuck on the problem"
    result = _check_stuck_phrases(text)
    # Should return "i am unable to" since it comes first in the list
    assert result == "i am unable to", f"expected 'i am unable to', got '{result}'"


# --- Tests for _check_repeated_tool_calls ---

def test_repeated_tools_detects_4_identical():
    """Test that 4 identical tool calls are detected."""
    history = ["Read|/src/foo.py", "Read|/src/foo.py", "Read|/src/foo.py", "Read|/src/foo.py"]
    result = _check_repeated_tool_calls(history, window=4)
    assert result, "should have detected 4 identical calls"


def test_repeated_tools_no_detection_for_varied():
    """Test that varied tool calls are not flagged."""
    history = ["Read|/a.py", "Edit|/a.py", "Read|/b.py", "Bash|npm test"]
    result = _check_repeated_tool_calls(history, window=4)
    assert not result, "should NOT flag varied calls"


def test_repeated_tools_short_history():
    """Test that short history (< window) returns False."""
    for length in range(4):
        history = ["Read|/src/foo.py"] * length
        result = _check_repeated_tool_calls(history, window=4)
        assert not result, f"should return False for history length {length}"


def test_repeated_tools_only_checks_last_window():
    """Test that only the last N calls matter."""
    # Older calls are varied, but last 4 are identical
    history = ["Edit|/a.py", "Bash|test", "Read|/x", "Read|/x", "Read|/x", "Read|/x"]
    result = _check_repeated_tool_calls(history, window=4)
    assert result, "should detect last 4 identical despite earlier variety"

    # Last 4 are not all identical (3 same + 1 different at end)
    history2 = ["Read|/x", "Read|/x", "Read|/x", "Edit|/x"]
    result2 = _check_repeated_tool_calls(history2, window=4)
    assert not result2, "should NOT flag when last call differs"


def test_repeated_tools_custom_window():
    """Test that custom window size works."""
    history = ["Read|/x", "Read|/x"]
    # Window of 2 should detect
    result = _check_repeated_tool_calls(history, window=2)
    assert result, "window=2 should detect 2 identical calls"

    # Window of 3 should not detect (only 2 items)
    result2 = _check_repeated_tool_calls(history, window=3)
    assert not result2, "window=3 should not detect with only 2 items"


# --- Tests for constants ---

def test_constants_values():
    """Test that early termination constants have expected values."""
    assert EARLY_TERM_WARN_TURNS == 8, f"EARLY_TERM_WARN_TURNS should be 8, got {EARLY_TERM_WARN_TURNS}"
    assert EARLY_TERM_FINAL_WARN_TURNS == 12, f"EARLY_TERM_FINAL_WARN_TURNS should be 12, got {EARLY_TERM_FINAL_WARN_TURNS}"
    assert EARLY_TERM_KILL_TURNS == 15, f"EARLY_TERM_KILL_TURNS should be 15, got {EARLY_TERM_KILL_TURNS}"
    assert EARLY_TERM_WARN_TURNS < EARLY_TERM_FINAL_WARN_TURNS < EARLY_TERM_KILL_TURNS, \
        f"WARN_TURNS ({EARLY_TERM_WARN_TURNS}) < FINAL_WARN_TURNS ({EARLY_TERM_FINAL_WARN_TURNS}) < KILL_TURNS ({EARLY_TERM_KILL_TURNS})"


def test_exempt_roles_contains_expected():
    """Test that exempt roles include research/planning roles."""
    required_roles = {"planner", "evaluator", "security-reviewer", "code-reviewer", "researcher"}
    missing = required_roles - EARLY_TERM_EXEMPT_ROLES
    assert not missing, f"missing exempt roles: {missing}"

    # Developer and tester should NOT be exempt
    assert "developer" not in EARLY_TERM_EXEMPT_ROLES, "'developer' should NOT be exempt"
    assert "tester" not in EARLY_TERM_EXEMPT_ROLES, "'tester' should NOT be exempt"


def test_stuck_phrases_list_not_empty():
    """Test that stuck phrases list is populated."""
    assert EARLY_TERM_STUCK_PHRASES, "stuck phrases list is empty"
    assert len(EARLY_TERM_STUCK_PHRASES) >= 5, \
        f"expected at least 5 stuck phrases, got {len(EARLY_TERM_STUCK_PHRASES)}"

    # All phrases should be lowercase
    for phrase in EARLY_TERM_STUCK_PHRASES:
        assert phrase == phrase.lower(), f"phrase '{phrase}' is not lowercase"


# --- Tests for _detect_tool_loop integration ---

def test_detect_tool_loop_integrates_with_early_term():
    """Test _detect_tool_loop's terminate threshold catches stuck agents."""
    history = ["Bash|git status"] * 5
    errors = ["error"] * 5
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", f"expected 'terminate', got '{action}'"
    assert count == 5, f"expected count 5, got {count}"


def test_detect_tool_loop_warn_at_3():
    """Test _detect_tool_loop warns at 3 consecutive identical."""
    history = ["Read|/x.py"] * 3
    errors = ["error"] * 3
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "warn", f"expected 'warn', got '{action}'"


# --- Main ---

def main():
    tests = [
        test_stuck_phrases_detects_each,
        test_stuck_phrases_case_insensitive,
        test_stuck_phrases_returns_none_for_clean_text,
        test_stuck_phrases_partial_match,
        test_stuck_phrases_returns_first_match,
        test_repeated_tools_detects_4_identical,
        test_repeated_tools_no_detection_for_varied,
        test_repeated_tools_short_history,
        test_repeated_tools_only_checks_last_window,
        test_repeated_tools_custom_window,
        test_constants_values,
        test_exempt_roles_contains_expected,
        test_stuck_phrases_list_not_empty,
        test_detect_tool_loop_integrates_with_early_term,
        test_detect_tool_loop_warn_at_3,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print(f"  Early Termination Test Suite")
    print(f"  Testing _check_stuck_phrases, _check_repeated_tool_calls,")
    print(f"  constants, and _detect_tool_loop integration")
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
