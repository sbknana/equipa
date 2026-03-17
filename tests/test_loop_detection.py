#!/usr/bin/env python3
"""
Test suite for loop detection in forge_orchestrator.py.

Tests verify:
1. LoopDetector fingerprinting extracts correct patterns
2. Consecutive identical results trigger warning at threshold 3
3. Consecutive identical results trigger termination at threshold 5
4. Legitimate retries (different files changed) reset the counter
5. Different fingerprints reset the counter
6. Warning is only issued once per detector instance
7. Empty/missing results are handled gracefully
8. termination_summary() and warning_message() produce valid output
9. _detect_tool_loop() function works for streaming-level detection

Run with: python3 -m pytest test_loop_detection.py -v
Or standalone: python3 test_loop_detection.py

Copyright 2026 Forgeborn
"""

import sys
from pathlib import Path

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the classes and functions we need to test
from forge_orchestrator import (
    LoopDetector,
    LOOP_WARNING_THRESHOLD,
    LOOP_TERMINATE_THRESHOLD,
    _detect_tool_loop,
)


def make_result(result_status="failed", summary="some error occurred",
                blocker=None, errors=None, files_changed=None):
    """Helper: build a mock agent result dict."""
    lines = []
    lines.append(f"RESULT: {result_status}")
    lines.append(f"SUMMARY: {summary}")
    if blocker:
        lines.append(f"BLOCKERS: {blocker}")
    else:
        lines.append("BLOCKERS: none")
    if files_changed:
        lines.append("FILES_CHANGED:")
        for f in files_changed:
            lines.append(f"- {f}")
    else:
        lines.append("FILES_CHANGED: none")

    result_text = "\n".join(lines)
    return {
        "result_text": result_text,
        "errors": errors or [],
        "success": result_status == "success",
    }


# --- Fingerprint Tests ---

def test_fingerprint_extracts_result_and_summary():
    """Test that _fingerprint extracts RESULT and SUMMARY from output."""
    detector = LoopDetector()
    result = make_result(result_status="blocked", summary="missing dep X")
    fp = detector._fingerprint(result)

    assert "blocked" in fp, f"'blocked' not in fingerprint: {fp}"
    assert "summary:missing dep x" in fp, f"'summary:missing dep x' not in fingerprint: {fp}"


def test_fingerprint_extracts_blockers():
    """Test that _fingerprint includes non-none blockers."""
    detector = LoopDetector()
    result = make_result(blocker="cannot install package Y")
    fp = detector._fingerprint(result)

    assert "blocker:cannot install package y" in fp, f"blocker not in fingerprint: {fp}"


def test_fingerprint_ignores_none_blockers():
    """Test that 'none' blockers are excluded from fingerprint."""
    detector = LoopDetector()
    result = make_result(blocker=None)
    fp = detector._fingerprint(result)

    assert "blocker:" not in fp, f"'none' blocker included in fingerprint: {fp}"


def test_fingerprint_includes_errors():
    """Test that error messages are included in fingerprint."""
    detector = LoopDetector()
    result = make_result(errors=["TypeError: cannot read prop"])
    fp = detector._fingerprint(result)

    assert "error:typeerror: cannot read prop" in fp, f"error not in fingerprint: {fp}"


def test_fingerprint_limits_errors_to_three():
    """Test that only first 3 errors are included in fingerprint."""
    detector = LoopDetector()
    result = make_result(errors=["err1", "err2", "err3", "err4", "err5"])
    fp = detector._fingerprint(result)

    assert "error:err4" not in fp, f"more than 3 errors in fingerprint: {fp}"
    assert "error:err5" not in fp, f"more than 3 errors in fingerprint: {fp}"
    assert "error:err1" in fp, f"first 3 errors not in fingerprint: {fp}"
    assert "error:err3" in fp, f"first 3 errors not in fingerprint: {fp}"


def test_fingerprint_empty_result():
    """Test that empty/non-dict results produce 'empty' fingerprint."""
    detector = LoopDetector()

    assert detector._fingerprint({}) == "empty", "empty dict should produce 'empty'"
    assert detector._fingerprint(None) == "empty", "None should produce 'empty'"
    assert detector._fingerprint("string result") == "empty", "string should produce 'empty'"


# --- Record/Threshold Tests ---

def test_record_ok_for_different_fingerprints():
    """Test that different fingerprints always return 'ok'."""
    detector = LoopDetector()

    for i in range(10):
        result = make_result(summary=f"unique error {i}")
        action = detector.record(result, cycle=i + 1)
        assert action == "ok", f"cycle {i + 1} returned '{action}' instead of 'ok'"

    assert detector.consecutive_same == 1, \
        f"consecutive_same should be 1, got {detector.consecutive_same}"


def test_warning_at_threshold():
    """Test that warning is returned at LOOP_WARNING_THRESHOLD consecutive same results."""
    detector = LoopDetector()

    same_result = make_result(summary="same error every time")
    actions = []
    for i in range(LOOP_WARNING_THRESHOLD):
        action = detector.record(same_result, cycle=i + 1)
        actions.append(action)

    # Last should be 'warn'
    assert actions[-1] == "warn", \
        f"cycle {LOOP_WARNING_THRESHOLD} should be 'warn', got '{actions[-1]}' (all: {actions})"

    # All earlier should be 'ok'
    for i in range(LOOP_WARNING_THRESHOLD - 1):
        assert actions[i] == "ok", f"cycle {i + 1} should be 'ok', got '{actions[i]}'"


def test_terminate_at_threshold():
    """Test that termination is returned at LOOP_TERMINATE_THRESHOLD consecutive same results."""
    detector = LoopDetector()

    same_result = make_result(summary="same error every time")
    actions = []
    for i in range(LOOP_TERMINATE_THRESHOLD):
        action = detector.record(same_result, cycle=i + 1)
        actions.append(action)

    assert actions[-1] == "terminate", \
        f"cycle {LOOP_TERMINATE_THRESHOLD} should be 'terminate', got '{actions[-1]}' (all: {actions})"


def test_warning_only_issued_once():
    """Test that warning is only issued once, then it's 'ok' until terminate."""
    detector = LoopDetector()

    same_result = make_result(summary="same error every time")
    warn_count = 0
    for i in range(LOOP_TERMINATE_THRESHOLD):
        action = detector.record(same_result, cycle=i + 1)
        if action == "warn":
            warn_count += 1

    assert warn_count == 1, f"expected exactly 1 warn, got {warn_count}"


def test_files_changed_resets_counter():
    """Test that different files_changed between cycles resets the loop counter."""
    detector = LoopDetector()

    # Cycle 1-2: same error, same files
    result1 = make_result(summary="build failed", files_changed=["src/foo.py"])
    detector.record(result1, cycle=1)
    detector.record(result1, cycle=2)
    assert detector.consecutive_same == 2, \
        f"after 2 same results, consecutive_same should be 2, got {detector.consecutive_same}"

    # Cycle 3: same error, DIFFERENT files -> should reset
    result2 = make_result(summary="build failed", files_changed=["src/bar.py"])
    action = detector.record(result2, cycle=3)
    assert detector.consecutive_same == 1, \
        f"after files changed, consecutive_same should be 1, got {detector.consecutive_same}"
    assert action == "ok", f"expected 'ok' after files changed, got '{action}'"


def test_same_files_does_not_reset():
    """Test that same files_changed does NOT reset the counter."""
    detector = LoopDetector()

    result = make_result(summary="build failed", files_changed=["src/foo.py"])
    for i in range(LOOP_WARNING_THRESHOLD):
        detector.record(result, cycle=i + 1)

    assert detector.consecutive_same == LOOP_WARNING_THRESHOLD, \
        f"expected {LOOP_WARNING_THRESHOLD}, got {detector.consecutive_same}"


def test_no_files_changed_does_not_reset():
    """Test that empty files_changed list is NOT treated as a legitimate retry."""
    detector = LoopDetector()

    result = make_result(summary="same error")
    for i in range(4):
        detector.record(result, cycle=i + 1)

    assert detector.consecutive_same == 4, \
        f"expected 4, got {detector.consecutive_same}"


# --- Edge Case Tests ---

def test_alternating_patterns_no_loop():
    """Test that alternating between two different errors doesn't trigger loop."""
    detector = LoopDetector()

    result_a = make_result(summary="error A")
    result_b = make_result(summary="error B")

    for i in range(10):
        r = result_a if i % 2 == 0 else result_b
        action = detector.record(r, cycle=i + 1)
        assert action == "ok", f"cycle {i + 1} returned '{action}', expected 'ok'"


def test_warning_message_content():
    """Test that warning_message() produces useful content."""
    detector = LoopDetector()

    result = make_result(summary="same error")
    for i in range(LOOP_WARNING_THRESHOLD):
        detector.record(result, cycle=i + 1)

    msg = detector.warning_message()
    assert "LOOP DETECTED" in msg, "'LOOP DETECTED' not in warning message"
    assert "different approach" in msg.lower(), "'different approach' not in warning message"
    assert str(detector.consecutive_same) in msg, "consecutive count not in warning message"


def test_termination_summary_content():
    """Test that termination_summary() captures useful info."""
    detector = LoopDetector()

    result = make_result(summary="same error")
    for i in range(LOOP_TERMINATE_THRESHOLD):
        detector.record(result, cycle=i + 1)

    summary = detector.termination_summary()
    assert "loop detected" in summary.lower(), "'loop detected' not in summary"
    assert str(LOOP_TERMINATE_THRESHOLD) in summary, "threshold count not in summary"
    assert detector.last_fingerprint[:50] in summary, "fingerprint not in summary"


def test_custom_thresholds():
    """Test that custom warning/terminate thresholds work."""
    detector = LoopDetector(warning_threshold=2, terminate_threshold=4)

    result = make_result(summary="same error")
    actions = []
    for i in range(4):
        action = detector.record(result, cycle=i + 1)
        actions.append(action)

    assert actions[1] == "warn", f"expected 'warn' at cycle 2, got '{actions[1]}'"
    assert actions[3] == "terminate", f"expected 'terminate' at cycle 4, got '{actions[3]}'"


def test_fingerprint_is_sorted_deterministic():
    """Test that fingerprint produces the same output regardless of line order."""
    detector = LoopDetector()

    result1 = {
        "result_text": "RESULT: failed\nSUMMARY: test error\nBLOCKERS: dep missing",
        "errors": ["err1"],
    }
    result2 = {
        "result_text": "BLOCKERS: dep missing\nSUMMARY: test error\nRESULT: failed",
        "errors": ["err1"],
    }

    fp1 = detector._fingerprint(result1)
    fp2 = detector._fingerprint(result2)

    assert fp1 == fp2, f"fingerprints differ: {fp1} != {fp2}"


def test_reset_after_success():
    """Test that a successful result breaks the loop pattern."""
    detector = LoopDetector()

    fail_result = make_result(summary="build failed")
    success_result = make_result(result_status="success", summary="build passed")

    # Two failures
    detector.record(fail_result, cycle=1)
    detector.record(fail_result, cycle=2)
    assert detector.consecutive_same == 2, \
        f"expected 2 consecutive after 2 failures, got {detector.consecutive_same}"

    # Success breaks the pattern
    action = detector.record(success_result, cycle=3)
    assert detector.consecutive_same == 1, \
        f"success should reset counter to 1, got {detector.consecutive_same}"
    assert action == "ok", f"expected 'ok' after success, got '{action}'"


# --- Tests for _detect_tool_loop (streaming-level detection) ---

def test_detect_tool_loop_ok():
    """Test _detect_tool_loop returns ok for non-repeating patterns."""
    history = ["Read|src/a.py", "Edit|src/a.py", "Read|src/b.py", "Bash|npm test"]
    errors = ["err", "err", "err", "err"]
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "ok", f"expected 'ok', got '{action}'"


def test_detect_tool_loop_warn():
    """Test _detect_tool_loop returns warn at threshold."""
    history = ["Read|x", "Edit|x", "Edit|x", "Edit|x"]
    errors = ["err", "err", "err", "err"]
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "warn", f"expected 'warn', got '{action}' (count={count})"
    assert count == 3, f"expected count 3, got {count}"


def test_detect_tool_loop_terminate():
    """Test _detect_tool_loop returns terminate at 5 repeats."""
    history = ["Read|x"] * 5
    errors = ["err"] * 5
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", f"expected 'terminate', got '{action}' (count={count})"
    assert count == 5, f"expected count 5, got {count}"


def test_detect_tool_loop_empty():
    """Test _detect_tool_loop handles empty/short lists."""
    action1, _, _ = _detect_tool_loop([], [])
    action2, _, _ = _detect_tool_loop(["Read|x"], ["err"])
    assert action1 == "ok", "expected 'ok' for empty list"
    assert action2 == "ok", "expected 'ok' for single-element list"


def test_detect_tool_loop_broken_by_different():
    """Test that a different tool in between resets the count."""
    history = ["Edit|x", "Edit|x", "Read|y", "Edit|x", "Edit|x"]
    errors = ["err", "err", "err", "err", "err"]
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "ok", f"expected 'ok', got '{action}' (count={count})"
    assert count == 2, f"expected count 2, got {count}"


def test_detect_tool_loop_ok_when_last_succeeds():
    """Test _detect_tool_loop returns ok when the last operation succeeded."""
    history = ["Edit|x", "Edit|x", "Edit|x"]
    errors = ["err", "err", None]  # last op succeeded
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "ok", f"expected 'ok' when last op succeeds, got '{action}'"
    assert count == 0, f"expected count 0 for successful last op, got {count}"


# --- Main (standalone runner) ---

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

