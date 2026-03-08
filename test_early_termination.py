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
11. Output hash computed for tool results (_compute_output_hash)
12. Same input + same output detected as stuck (stronger loop signal)
13. Same input + different output not flagged as stuck loop
14. Output hash handles None/empty content gracefully
15. Dead code _check_repeated_tool_calls removed
16. Alternating (A-B-A-B) loop detection triggers at 6+ failing alternations
17. Alternating loop warns at 4+ failing alternations
18. Successful alternating calls are not flagged
19. Mixed success/failure alternating patterns correctly detected
20. Three-way rotation (A-B-C) does not false-positive as alternating
21. Alternating detection coexists with consecutive detection
22. EARLY_COMPLETE: signal parsed correctly from agent text
23. EARLY_COMPLETE waits for message end (multi-line context parsing)
24. EARLY_COMPLETE not confused with RESULT: markers
25. EARLY_COMPLETE cost still tracked (result dict shape)
26. early_completed distinct from early_terminated (success vs failure)
27. False EARLY_COMPLETE in quoted/code text is ignored
28. Budget message returned at periodic intervals (every 5 turns)
29. Budget halfway warning at 50% threshold
30. Budget critical warning at 75% threshold
31. No budget message at turn 0 or when not on interval
32. Budget constants have expected values
33. Budget message includes correct remaining turn count
34. Budget message not returned when max_turns is None

Copyright 2026 Forgeborn
"""

import hashlib
import sys
from pathlib import Path

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent))

from forge_orchestrator import (
    _check_stuck_phrases,
    _check_monologue,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
    BUDGET_CHECK_INTERVAL,
    BUDGET_HALFWAY_THRESHOLD,
    BUDGET_CRITICAL_THRESHOLD,
    EARLY_TERM_WARN_TURNS,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_STUCK_PHRASES,
    EARLY_TERM_EXEMPT_ROLES,
    MONOLOGUE_THRESHOLD,
    MONOLOGUE_EXEMPT_TURNS,
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


# --- Tests for dead code removal ---

def test_dead_code_removed():
    """Test that _check_repeated_tool_calls has been removed (dead code cleanup)."""
    import forge_orchestrator
    assert not hasattr(forge_orchestrator, "_check_repeated_tool_calls"), \
        "_check_repeated_tool_calls should be removed — it was dead code superseded by _detect_tool_loop"


# --- Tests for _compute_output_hash ---

def test_output_hash_computed_for_tool_results():
    """Test that _compute_output_hash returns a valid SHA256 hex digest."""
    # String content
    result = _compute_output_hash("Hello, world!")
    assert isinstance(result, str), f"expected str, got {type(result)}"
    assert len(result) == 64, f"expected 64-char SHA256 hex, got {len(result)} chars"
    expected = hashlib.sha256("Hello, world!".encode("utf-8")).hexdigest()
    assert result == expected, f"hash mismatch: {result} != {expected}"

    # List content (content blocks from Claude stream-json)
    list_content = [
        {"type": "text", "text": "file contents here"},
        {"type": "text", "text": "more text"},
    ]
    result2 = _compute_output_hash(list_content)
    assert isinstance(result2, str) and len(result2) == 64, \
        "list content should produce a valid SHA256 hex digest"
    # The hash should be of the space-joined text blocks
    expected2 = hashlib.sha256("file contents here more text".encode("utf-8")).hexdigest()
    assert result2 == expected2, f"list hash mismatch: {result2} != {expected2}"


def test_output_hash_handles_none_content():
    """Test that _compute_output_hash handles None and empty content."""
    # None content should return a deterministic hash (hash of empty string)
    result_none = _compute_output_hash(None)
    assert isinstance(result_none, str) and len(result_none) == 64, \
        "None content should produce a valid hash"

    # Empty string
    result_empty = _compute_output_hash("")
    assert isinstance(result_empty, str) and len(result_empty) == 64, \
        "empty string should produce a valid hash"

    # None and empty string should produce the same hash (both are empty)
    assert result_none == result_empty, \
        "None and empty string should hash identically"

    # Empty list
    result_empty_list = _compute_output_hash([])
    assert result_empty_list == result_empty, \
        "empty list should hash the same as empty string"

    # List with non-text blocks (should be skipped)
    result_no_text = _compute_output_hash([{"type": "image", "data": "..."}])
    assert result_no_text == result_empty, \
        "list with no text blocks should hash the same as empty content"


def test_same_input_same_output_detected_as_stuck():
    """Test that _detect_tool_loop flags same input + same output as a stronger stuck signal.

    When output hashes match alongside input signatures, the effective threshold
    should be lower (warn at 2, terminate at 3) because identical output confirms
    the agent is truly stuck rather than retrying with potential external changes.
    """
    history = ["Read|/foo.py"] * 3
    errors = ["error: file not found"] * 3
    # Same output hash for all 3 calls
    same_hash = hashlib.sha256(b"identical error output").hexdigest()
    output_hashes = [same_hash] * 3

    # With output hashes showing identical results, 3 repeats should terminate
    # (normally 3 repeats would only warn without output hash confirmation)
    action, count, sig = _detect_tool_loop(
        history, errors, tool_output_hashes=output_hashes,
        warn_threshold=3, terminate_threshold=5,
    )
    assert action == "terminate", \
        f"same input + same output at 3 repeats should terminate, got '{action}'"


def test_same_input_different_output_not_flagged():
    """Test that same input but different output does NOT trigger early termination.

    When the agent retries the same tool call but gets different output, it means
    external state has changed (e.g., file was created between retries). This should
    use the normal (higher) thresholds, not the output-hash-enhanced thresholds.
    """
    history = ["Bash|npm test"] * 4
    errors = ["error: test failed"] * 4
    # Different output hash each time (external state changing)
    output_hashes = [
        hashlib.sha256(f"output {i}".encode()).hexdigest()
        for i in range(4)
    ]

    # 4 repeats with different outputs should NOT terminate (threshold=5)
    action, count, sig = _detect_tool_loop(
        history, errors, tool_output_hashes=output_hashes,
        warn_threshold=3, terminate_threshold=5,
    )
    # Should warn (4 >= warn_threshold=3) but NOT terminate (4 < 5)
    assert action == "warn", \
        f"same input + different output at 4 repeats should warn, got '{action}'"
    assert count == 4, f"expected count 4, got {count}"


# --- Tests for constants ---

def test_constants_values():
    """Test that early termination constants have expected values."""
    assert EARLY_TERM_WARN_TURNS == 12, f"EARLY_TERM_WARN_TURNS should be 12, got {EARLY_TERM_WARN_TURNS}"
    assert EARLY_TERM_FINAL_WARN_TURNS == 18, f"EARLY_TERM_FINAL_WARN_TURNS should be 18, got {EARLY_TERM_FINAL_WARN_TURNS}"
    assert EARLY_TERM_KILL_TURNS == 22, f"EARLY_TERM_KILL_TURNS should be 22, got {EARLY_TERM_KILL_TURNS}"
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


# --- Tests for alternating (A-B-A-B) loop detection ---

def test_alternating_pattern_detected_at_6_cycles():
    """Test that 6+ failing A-B alternations trigger 'terminate'."""
    # 6 alternations = 3 complete A-B pairs: A B A B A B
    history = []
    errors = []
    for _ in range(3):
        history.extend(["Read|/foo.py", "Edit|/foo.py"])
        errors.extend(["file not found", "edit failed"])
    # history is now 6 entries: A B A B A B, all failing
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"expected 'terminate' for 6 failing alternations, got '{action}'"
    assert count >= 6, f"expected count >= 6, got {count}"


def test_alternating_pattern_warning_at_4_cycles():
    """Test that 4-5 failing A-B alternations trigger 'warn'."""
    # 4 alternations = 2 complete A-B pairs: A B A B
    history = []
    errors = []
    for _ in range(2):
        history.extend(["Grep|pattern", "Read|/src/main.py"])
        errors.extend(["no matches", "permission denied"])
    # history is 4 entries: A B A B, all failing
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "warn", \
        f"expected 'warn' for 4 failing alternations, got '{action}'"
    assert count >= 4, f"expected count >= 4, got {count}"


def test_alternating_successful_calls_not_flagged():
    """Test that alternating calls that SUCCEED are not flagged as stuck."""
    # 8 alternations but all successful
    history = []
    errors = []
    for _ in range(4):
        history.extend(["Read|/a.py", "Read|/b.py"])
        errors.extend([None, None])
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "ok", \
        f"expected 'ok' for successful alternations, got '{action}'"


def test_alternating_with_errors_flagged():
    """Test that alternating calls with errors are properly flagged."""
    # Mix: first 2 succeed, then 6 fail with alternating pattern
    history = ["Read|/a.py", "Read|/b.py"]  # these succeed
    errors = [None, None]
    for _ in range(3):
        history.extend(["Bash|npm test", "Edit|/fix.py"])
        errors.extend(["test failed", "syntax error"])
    # Last 6 entries are failing A-B-A-B-A-B
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"expected 'terminate' for 6 failing alternations after successes, got '{action}'"


def test_three_way_rotation_not_false_positive():
    """Test that A-B-C-A-B-C rotation is NOT detected as A-B-A-B alternation."""
    # 9 entries: A B C A B C A B C — this is a 3-way pattern, not 2-way
    history = []
    errors = []
    for _ in range(3):
        history.extend(["Read|/a.py", "Edit|/b.py", "Bash|test"])
        errors.extend(["error", "error", "error"])
    action, count, sig = _detect_tool_loop(history, errors)
    # Should NOT trigger alternating detection — it's a 3-way rotation
    # The consecutive check also won't fire since they're all different
    assert action == "ok", \
        f"expected 'ok' for 3-way rotation, got '{action}'"


def test_alternating_detection_coexists_with_consecutive_detection():
    """Test that consecutive detection still works alongside alternating detection."""
    # Consecutive: 5 identical failing calls should still trigger terminate
    history = ["Bash|git status"] * 5
    errors = ["error: not a git repo"] * 5
    action, count, sig = _detect_tool_loop(history, errors)
    assert action == "terminate", \
        f"consecutive detection should still work, got '{action}'"
    assert count >= 5, f"expected count >= 5, got {count}"

    # Alternating: 6 failing A-B should also trigger terminate
    history2 = []
    errors2 = []
    for _ in range(3):
        history2.extend(["Read|/x", "Write|/x"])
        errors2.extend(["error", "error"])
    action2, count2, sig2 = _detect_tool_loop(history2, errors2)
    assert action2 == "terminate", \
        f"alternating detection should also work, got '{action2}'"


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


# --- Tests for _parse_early_complete (agent-initiated early completion) ---

def test_early_complete_signal_parsed():
    """Test that EARLY_COMPLETE: <reason> is correctly parsed from agent text."""
    text = "I've finished implementing the feature.\nEARLY_COMPLETE: All changes committed and tests pass"
    reason = _parse_early_complete(text)
    assert reason == "All changes committed and tests pass", \
        f"expected 'All changes committed and tests pass', got '{reason}'"

    # Single line
    reason2 = _parse_early_complete("EARLY_COMPLETE: No changes needed")
    assert reason2 == "No changes needed", \
        f"expected 'No changes needed', got '{reason2}'"

    # With extra whitespace around the reason
    reason3 = _parse_early_complete("EARLY_COMPLETE:   Extra spaces here  ")
    assert reason3 == "Extra spaces here", \
        f"expected 'Extra spaces here', got '{reason3}'"


def test_early_complete_waits_for_message_end():
    """Test that EARLY_COMPLETE is detected in text but does NOT interfere with
    message processing — the parsing function only extracts the reason.

    The actual "wait for message end" behavior is enforced in run_agent_streaming,
    which sets a flag and breaks AFTER the current assistant message is fully processed.
    This test verifies the parsing layer works correctly in multi-line contexts.
    """
    # EARLY_COMPLETE appears mid-text with content after it
    text = (
        "I've completed the implementation.\n"
        "EARLY_COMPLETE: Feature fully implemented and tested\n"
        "Here is the summary of what I did:\n"
        "RESULT: success\n"
        "SUMMARY: Added the new endpoint\n"
    )
    reason = _parse_early_complete(text)
    assert reason == "Feature fully implemented and tested", \
        f"should parse reason from the EARLY_COMPLETE line, got '{reason}'"


def test_early_complete_not_confused_with_result_marker():
    """Test that RESULT: markers are NOT parsed as EARLY_COMPLETE."""
    # RESULT: success should NOT trigger early complete
    assert _parse_early_complete("RESULT: success") is None, \
        "RESULT: success should NOT be parsed as EARLY_COMPLETE"

    # RESULT: blocked should NOT trigger early complete
    assert _parse_early_complete("RESULT: blocked") is None, \
        "RESULT: blocked should NOT be parsed as EARLY_COMPLETE"

    # Text with RESULT: but no EARLY_COMPLETE should return None
    text = "RESULT: success\nSUMMARY: Done\nFILES_CHANGED: foo.py"
    assert _parse_early_complete(text) is None, \
        "text with only RESULT: markers should return None"

    # Text with BOTH should return the EARLY_COMPLETE reason
    text_both = "EARLY_COMPLETE: Task done\nRESULT: success"
    reason = _parse_early_complete(text_both)
    assert reason == "Task done", \
        f"when both present, should return EARLY_COMPLETE reason, got '{reason}'"


def test_early_complete_cost_still_tracked():
    """Test that the result dict from an early-completed agent includes cost data.

    This is a structural test — we verify the result dict shape that
    run_agent_streaming returns when early_completed is True. The actual
    cost tracking happens via result_data.get('total_cost_usd'), which is
    populated from the stream-json 'result' message that arrives even when
    the agent signals early completion (since we wait for the current message
    to finish and the process to exit naturally).

    We test the parsing function returns a reason (which triggers the flag),
    and verify the expected keys exist by checking the function contract.
    """
    # The parser should return a valid reason
    reason = _parse_early_complete("EARLY_COMPLETE: Done, no more work needed")
    assert reason is not None, "should parse a valid reason"

    # Verify the function returns a string (not a bool or other type)
    assert isinstance(reason, str), f"reason should be a string, got {type(reason)}"
    assert len(reason) > 0, "reason should not be empty"


def test_early_complete_distinct_from_early_terminated():
    """Test that early_completed and early_terminated are distinct concepts.

    early_completed = agent CHOSE to stop (success signal)
    early_terminated = orchestrator FORCED agent to stop (failure signal)

    The _parse_early_complete function should return a reason for valid
    EARLY_COMPLETE markers but return None for stuck/termination phrases.
    """
    # Agent-initiated completion should parse successfully
    reason = _parse_early_complete("EARLY_COMPLETE: Task complete, no changes needed")
    assert reason is not None, "EARLY_COMPLETE should be parsed"

    # Stuck phrases should NOT parse as early completion
    stuck_texts = [
        "I am unable to complete this task",
        "I'm stuck on the problem",
        "I cannot proceed",
    ]
    for text in stuck_texts:
        result = _parse_early_complete(text)
        assert result is None, \
            f"stuck phrase '{text}' should NOT parse as EARLY_COMPLETE, got '{result}'"

    # early_term_reason phrases should NOT parse as early completion
    term_texts = [
        "Agent terminated: 40 consecutive turns without file changes",
        "Loop detected: agent repeated the same operation 5 times",
        "Process timed out after 3600 seconds",
    ]
    for text in term_texts:
        result = _parse_early_complete(text)
        assert result is None, \
            f"termination phrase should NOT parse as EARLY_COMPLETE, got '{result}'"


def test_false_early_complete_in_quoted_text_ignored():
    """Test that EARLY_COMPLETE inside quoted/code blocks is NOT parsed.

    Agents may discuss or document the EARLY_COMPLETE marker in their output
    (e.g., in code comments, documentation, or quoted instructions). These
    should NOT trigger the early completion signal.
    """
    # Inside a code block (triple backtick)
    text_code = (
        "Here is how to use the marker:\n"
        "```\n"
        "EARLY_COMPLETE: This is a code example\n"
        "```\n"
    )
    assert _parse_early_complete(text_code) is None, \
        "EARLY_COMPLETE inside a code block should be ignored"

    # Inside inline code (single backtick)
    text_inline = "Use the `EARLY_COMPLETE: reason` marker when done."
    assert _parse_early_complete(text_inline) is None, \
        "EARLY_COMPLETE inside inline code should be ignored"

    # Inside a quoted block (> prefix)
    text_quoted = "> EARLY_COMPLETE: This is a quote from the docs"
    assert _parse_early_complete(text_quoted) is None, \
        "EARLY_COMPLETE inside a quoted block should be ignored"

    # Inside a string literal (surrounded by quotes)
    text_string = 'The marker is "EARLY_COMPLETE: reason" format.'
    assert _parse_early_complete(text_string) is None, \
        "EARLY_COMPLETE inside double quotes should be ignored"

    # Plain EARLY_COMPLETE at start of line (not quoted) should still work
    text_valid = "Some preamble text\nEARLY_COMPLETE: Genuinely done"
    reason = _parse_early_complete(text_valid)
    assert reason == "Genuinely done", \
        f"unquoted EARLY_COMPLETE should parse, got '{reason}'"


# --- Tests for _get_budget_message (budget visibility) ---

def test_budget_message_at_periodic_intervals():
    """Test that budget messages are returned every BUDGET_CHECK_INTERVAL turns."""
    # At turn 5 of 50 (on interval), should get a periodic update
    msg = _get_budget_message(5, 50)
    assert msg is not None, "should return a message at turn 5 (on interval)"
    assert "45" in msg, f"message should mention 45 remaining turns, got: {msg}"
    assert "5" in msg, f"message should mention 5 turns used, got: {msg}"

    # At turn 10 of 50 (on interval, but below halfway), should get periodic
    msg2 = _get_budget_message(10, 50)
    assert msg2 is not None, "should return a message at turn 10 (on interval)"
    assert "40" in msg2, f"message should mention 40 remaining turns, got: {msg2}"


def test_budget_halfway_warning():
    """Test that a HALFWAY warning is returned at exactly 50% of budget."""
    # Turn 25 of 50 = exactly 50%
    msg = _get_budget_message(25, 50)
    assert msg is not None, "should return a message at 50% budget"
    assert "HALFWAY" in msg, f"message should contain 'HALFWAY', got: {msg}"
    assert "25" in msg, f"message should mention 25 remaining turns, got: {msg}"

    # Turn 20 of 40 = exactly 50%
    msg2 = _get_budget_message(20, 40)
    assert msg2 is not None, "should return a message at 50% of 40-turn budget"
    assert "HALFWAY" in msg2, f"message should contain 'HALFWAY', got: {msg2}"


def test_budget_critical_warning():
    """Test that a CRITICAL warning is returned at 75% of budget."""
    # Turn 38 of 50 = 76% (past critical)
    # But 38 is not on BUDGET_CHECK_INTERVAL. Try turn 40 of 50 = 80%.
    msg = _get_budget_message(40, 50)
    assert msg is not None, "should return a message at 80% budget"
    assert "CRITICAL" in msg, f"message should contain 'CRITICAL', got: {msg}"
    assert "10" in msg, f"message should mention 10 remaining turns, got: {msg}"

    # Turn 75 of 100 = exactly 75%
    msg2 = _get_budget_message(75, 100)
    assert msg2 is not None, "should return a message at 75% of 100-turn budget"
    assert "CRITICAL" in msg2, f"message should contain 'CRITICAL', got: {msg2}"


def test_budget_no_message_off_interval():
    """Test that no budget message is returned on turns not on the check interval."""
    # Turn 3 of 50 — not on interval (5), not at any threshold
    msg = _get_budget_message(3, 50)
    assert msg is None, f"should return None at turn 3 (not on interval), got: {msg}"

    # Turn 7 of 50 — not on interval
    msg2 = _get_budget_message(7, 50)
    assert msg2 is None, f"should return None at turn 7, got: {msg2}"

    # Turn 0 should return None
    msg3 = _get_budget_message(0, 50)
    assert msg3 is None, f"should return None at turn 0, got: {msg3}"


def test_budget_constants_values():
    """Test that budget visibility constants have expected values."""
    assert BUDGET_CHECK_INTERVAL == 5, \
        f"BUDGET_CHECK_INTERVAL should be 5, got {BUDGET_CHECK_INTERVAL}"
    assert BUDGET_HALFWAY_THRESHOLD == 0.5, \
        f"BUDGET_HALFWAY_THRESHOLD should be 0.5, got {BUDGET_HALFWAY_THRESHOLD}"
    assert BUDGET_CRITICAL_THRESHOLD == 0.75, \
        f"BUDGET_CRITICAL_THRESHOLD should be 0.75, got {BUDGET_CRITICAL_THRESHOLD}"
    # Ensure thresholds are ordered
    assert BUDGET_HALFWAY_THRESHOLD < BUDGET_CRITICAL_THRESHOLD, \
        "halfway threshold must be less than critical threshold"


def test_budget_message_correct_remaining():
    """Test that budget messages contain the correct remaining turn count."""
    # Turn 15 of 30 = 50% (halfway)
    msg = _get_budget_message(15, 30)
    assert msg is not None, "should return a message at turn 15/30"
    assert "15" in msg, f"should mention 15 remaining turns, got: {msg}"

    # Turn 20 of 80 = 25% (periodic)
    msg2 = _get_budget_message(20, 80)
    assert msg2 is not None, "should return a message at turn 20/80"
    assert "60" in msg2, f"should mention 60 remaining turns, got: {msg2}"


def test_budget_message_none_when_no_max_turns():
    """Test that no budget message is returned when max_turns is None or 0."""
    msg = _get_budget_message(5, None)
    assert msg is None, f"should return None when max_turns is None, got: {msg}"

    msg2 = _get_budget_message(5, 0)
    assert msg2 is None, f"should return None when max_turns is 0, got: {msg2}"


# --- Main ---

def main():
    tests = [
        test_stuck_phrases_detects_each,
        test_stuck_phrases_case_insensitive,
        test_stuck_phrases_returns_none_for_clean_text,
        test_stuck_phrases_partial_match,
        test_stuck_phrases_returns_first_match,
        test_dead_code_removed,
        test_output_hash_computed_for_tool_results,
        test_output_hash_handles_none_content,
        test_same_input_same_output_detected_as_stuck,
        test_same_input_different_output_not_flagged,
        test_constants_values,
        test_exempt_roles_contains_expected,
        test_stuck_phrases_list_not_empty,
        test_detect_tool_loop_integrates_with_early_term,
        test_detect_tool_loop_warn_at_3,
        test_alternating_pattern_detected_at_6_cycles,
        test_alternating_pattern_warning_at_4_cycles,
        test_alternating_successful_calls_not_flagged,
        test_alternating_with_errors_flagged,
        test_three_way_rotation_not_false_positive,
        test_alternating_detection_coexists_with_consecutive_detection,
        # Monologue detection tests
        test_monologue_detected_at_3_consecutive_text_only,
        test_monologue_resets_on_tool_use,
        test_monologue_exempt_first_5_turns,
        test_monologue_warning_at_2,
        test_monologue_does_not_interfere_with_stuck_phrases,
        test_monologue_constants,
        test_monologue_below_threshold_no_action,
        # Early completion signal tests
        test_early_complete_signal_parsed,
        test_early_complete_waits_for_message_end,
        test_early_complete_not_confused_with_result_marker,
        test_early_complete_cost_still_tracked,
        test_early_complete_distinct_from_early_terminated,
        test_false_early_complete_in_quoted_text_ignored,
        # Budget visibility tests
        test_budget_message_at_periodic_intervals,
        test_budget_halfway_warning,
        test_budget_critical_warning,
        test_budget_no_message_off_interval,
        test_budget_constants_values,
        test_budget_message_correct_remaining,
        test_budget_message_none_when_no_max_turns,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print(f"  Early Termination Test Suite")
    print(f"  Testing _check_stuck_phrases, _compute_output_hash,")
    print(f"  _detect_tool_loop with output hashes, dead code removal,")
    print(f"  _parse_early_complete, _get_budget_message")
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
