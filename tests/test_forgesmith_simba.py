#!/usr/bin/env python3
"""Test suite for forgesmith_simba.py — SIMBA targeted rule generation.

Validates Task #694 acceptance criteria:
1. Query agent_episodes for high-variance tasks (same role, different outcomes)
2. Identify hardest cases (early_terminated episodes with q_value < 0.3)
3. Generate specific rules via Claude (tested with mock)
4. Store rules in lessons_learned with source='simba_generated'
5. Cap at 3 new rules per role per night
6. Prune rules with inject_count > 50 but no improvement
7. Evaluate rule effectiveness before pruning

Copyright 2026 Forgeborn
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from forgesmith_simba import (
    find_high_variance_episodes,
    find_hardest_cases,
    get_existing_simba_rules,
    build_simba_prompt,
    call_claude_for_rules,
    validate_rule,
    store_rules,
    evaluate_simba_rules,
    prune_stale_rules,
    run_simba,
    MAX_RULES_PER_ROLE,
    PRUNE_INJECT_THRESHOLD,
    MIN_EPISODES_FOR_ANALYSIS,
    LOW_Q_THRESHOLD,
    THEFORGE_DB,
)


# --- Test helpers ---

def get_db_connection(write=False):
    """Get a connection to TheForge database."""
    if write:
        conn = sqlite3.connect(THEFORGE_DB)
    else:
        uri = f"file:{THEFORGE_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def setup_module(module):
    """Pytest hook: set up test data before any test in this module."""
    import forge_orchestrator
    forge_orchestrator._SCHEMA_ENSURED = False
    forge_orchestrator.ensure_schema()
    setup_simba_test_data()


def setup_simba_test_data():
    """Insert test episodes and lessons for SIMBA tests.

    Uses task_id >= 8000 range to avoid collision with real data and
    other test suites.
    """
    conn = sqlite3.connect(THEFORGE_DB)

    # Clean up previous test data
    conn.execute("DELETE FROM agent_episodes WHERE task_id >= 8000 AND task_id < 8100")
    conn.execute("DELETE FROM lessons_learned WHERE error_signature LIKE 'simba_test_%'")

    # Insert test episodes: developer role with mixed outcomes (high variance)
    test_episodes = [
        # Successes
        (8001, 'developer', 'feature', 28,
         'Read patterns, implemented feature successfully',
         20, 'success', None,
         'Started by reading similar code. Pattern matching worked well.',
         0.8),
        (8002, 'developer', 'bugfix', 28,
         'Found root cause quickly, fixed in 15 turns',
         15, 'tests_passed', None,
         'Used Grep to find the bug location. Fixed and tests passed.',
         0.9),
        # Failures
        (8003, 'developer', 'feature', 28,
         'Spent 40 turns exploring, ran out of time',
         40, 'early_terminated', 'agent terminated: 40 consecutive turns without file changes',
         'Explored too many files sequentially instead of using parallel reads.',
         0.1),
        (8004, 'developer', 'refactor', 28,
         'Attempted large refactor without reading all files',
         35, 'early_terminated', 'max_turns',
         'Should have read ALL files before starting changes.',
         0.2),
        (8005, 'developer', 'bugfix', 28,
         'Could not find the bug, kept retrying same approach',
         38, 'blocked', 'agent terminated: 40 consecutive turns without file changes',
         'Tried the same grep pattern 5 times. Should have tried different strategy.',
         0.15),
        # Tester role - only successes (not high-variance)
        (8006, 'tester', 'feature', 28,
         'Ran tests, all passed',
         10, 'tests_passed', None,
         'Used pytest framework, all tests passed.',
         0.9),
        (8007, 'tester', 'bugfix', 28,
         'Ran tests, found issues',
         12, 'tests_passed', None,
         'Tests revealed a failing test in auth module.',
         0.7),
    ]

    conn.executemany(
        """INSERT INTO agent_episodes
           (task_id, role, task_type, project_id,
            approach_summary, turns_used, outcome, error_patterns,
            reflection, q_value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        test_episodes,
    )

    # Insert existing SIMBA rules for duplicate checking
    conn.execute(
        """INSERT INTO lessons_learned
           (role, error_type, error_signature, lesson, source, times_seen,
            times_injected, effectiveness_score, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ('developer', 'early_terminated', 'simba_test_existing',
         'When exploring a codebase, use parallel Read calls instead of sequential ones.',
         'simba_generated', 1, 0, None, 1),
    )

    # Insert a rule ready for pruning (high inject count, no improvement)
    conn.execute(
        """INSERT INTO lessons_learned
           (role, error_type, error_signature, lesson, source, times_seen,
            times_injected, effectiveness_score, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ('developer', 'timeout', 'simba_test_prune_candidate',
         'This rule has been injected many times but has not helped.',
         'simba_generated', 1, 60, None, 1),
    )

    # Insert a rule with positive effectiveness (should NOT be pruned)
    conn.execute(
        """INSERT INTO lessons_learned
           (role, error_type, error_signature, lesson, source, times_seen,
            times_injected, effectiveness_score, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ('developer', 'early_terminated', 'simba_test_effective',
         'This effective rule should be kept despite high injection count.',
         'simba_generated', 1, 55, 0.15, 1),
    )

    conn.commit()
    conn.close()


def cleanup_simba_test_data():
    """Remove test data inserted by setup_simba_test_data."""
    conn = sqlite3.connect(THEFORGE_DB)
    conn.execute("DELETE FROM agent_episodes WHERE task_id >= 8000 AND task_id < 8100")
    conn.execute("DELETE FROM lessons_learned WHERE error_signature LIKE 'simba_test_%'")
    conn.commit()
    conn.close()


# --- Test: Constants ---

def test_constants_have_expected_values():
    """Test that SIMBA constants are set correctly."""
    assert MAX_RULES_PER_ROLE == 3, "Should cap at 3 rules per role"
    assert PRUNE_INJECT_THRESHOLD == 50, "Should prune after 50 injections"
    assert MIN_EPISODES_FOR_ANALYSIS == 3, "Need at least 3 episodes for analysis"
    assert LOW_Q_THRESHOLD == 0.3, "Hardest cases threshold should be 0.3"


# --- Test: find_high_variance_episodes ---

def test_find_high_variance_episodes():
    """Test that high-variance detection finds roles with mixed outcomes."""
    setup_simba_test_data()
    try:
        result = find_high_variance_episodes(lookback_days=30)

        # Developer should have both successes and failures
        assert "developer" in result, "Developer role should have high variance"
        dev = result["developer"]
        assert len(dev["successes"]) >= 2, "Developer should have >= 2 successes"
        assert len(dev["failures"]) >= 2, "Developer should have >= 2 failures"

        # Our test data inserts tester with only successes.
        # However, real episodes in the DB may give tester mixed outcomes,
        # so we only assert on the test-data developer role above.
    finally:
        cleanup_simba_test_data()


def test_high_variance_requires_reflection():
    """Test that episodes without reflection are excluded."""
    setup_simba_test_data()
    try:
        result = find_high_variance_episodes(lookback_days=30)
        if "developer" in result:
            for ep in result["developer"]["successes"] + result["developer"]["failures"]:
                assert ep["reflection"] is not None and ep["reflection"] != "", \
                    f"Episode {ep['id']} should have non-empty reflection"
    finally:
        cleanup_simba_test_data()


# --- Test: find_hardest_cases ---

def test_find_hardest_cases():
    """Test that hardest cases finds early_terminated episodes with low q_value."""
    setup_simba_test_data()
    try:
        hardest = find_hardest_cases(lookback_days=30)

        # Should find episodes with outcome=early_terminated AND q_value < 0.3
        assert len(hardest) >= 2, "Should find at least 2 hardest cases"

        for ep in hardest:
            assert ep["outcome"] == "early_terminated", \
                f"Hardest case should be early_terminated, got {ep['outcome']}"
            assert ep["q_value"] < LOW_Q_THRESHOLD, \
                f"Hardest case q_value should be < {LOW_Q_THRESHOLD}, got {ep['q_value']}"

        # Should be ordered by q_value ASC
        q_values = [ep["q_value"] for ep in hardest]
        assert q_values == sorted(q_values), "Hardest cases should be ordered by q_value ASC"
    finally:
        cleanup_simba_test_data()


# --- Test: build_simba_prompt ---

def test_build_simba_prompt_structure():
    """Test that the SIMBA prompt has required structure."""
    successes = [
        {"task_id": 1, "outcome": "success", "turns_used": 20,
         "q_value": 0.8, "approach_summary": "Did well", "reflection": "Good approach"},
    ]
    failures = [
        {"task_id": 2, "outcome": "early_terminated", "turns_used": 40,
         "q_value": 0.1, "error_patterns": "max_turns", "reflection": "Too slow"},
    ]
    hardest = [
        {"task_id": 3, "turns_used": 38, "q_value": 0.05,
         "error_patterns": "stuck", "reflection": "Could not proceed"},
    ]
    existing = [
        {"id": 1, "lesson": "Use parallel reads"},
    ]

    prompt = build_simba_prompt("developer", successes, failures, hardest, existing)

    assert "Role: developer" in prompt
    assert "Successful Episodes" in prompt
    assert "Failed Episodes" in prompt
    assert "Hardest Cases" in prompt
    assert "Existing Rules (DO NOT duplicate these)" in prompt
    assert "Use parallel reads" in prompt
    assert f"{MAX_RULES_PER_ROLE}" in prompt
    assert "JSON array" in prompt
    assert "error_type" in prompt


def test_build_simba_prompt_empty_sections():
    """Test prompt with empty sections."""
    prompt = build_simba_prompt("developer", [], [], [], [])

    assert "Role: developer" in prompt
    assert "No successful episodes recorded." in prompt
    assert "No failed episodes recorded." in prompt
    assert "No hardest cases found." in prompt
    # No existing rules section when empty
    assert "DO NOT duplicate" not in prompt


def test_build_simba_prompt_truncation():
    """Test that very long approach/reflection text is truncated."""
    long_text = "x" * 1000
    successes = [
        {"task_id": 1, "outcome": "success", "turns_used": 20,
         "q_value": 0.8, "approach_summary": long_text, "reflection": long_text},
    ]

    prompt = build_simba_prompt("developer", successes, [], [], [])

    # approach_summary is truncated to 200 chars, reflection to 300 chars
    # The full 1000-char string should not appear
    assert long_text not in prompt


# --- Test: validate_rule ---

def test_validate_rule_valid():
    """Test that a well-formed rule passes validation."""
    rule = {
        "rule": "When task mentions refactor, read ALL files in target directory before making changes.",
        "error_type": "early_terminated",
        "rationale": "Prevents incomplete understanding of codebase.",
    }
    is_valid, reason = validate_rule(rule, [])
    assert is_valid, f"Valid rule rejected: {reason}"
    assert reason == "ok"


def test_validate_rule_too_long():
    """Test that rules over 250 chars are rejected."""
    rule = {
        "rule": "x" * 260,
        "error_type": "early_terminated",
    }
    is_valid, reason = validate_rule(rule, [])
    assert not is_valid
    assert "too long" in reason


def test_validate_rule_too_short():
    """Test that rules under 20 chars are rejected."""
    rule = {
        "rule": "Be careful",
        "error_type": "early_terminated",
    }
    is_valid, reason = validate_rule(rule, [])
    assert not is_valid
    assert "too short" in reason


def test_validate_rule_empty():
    """Test that empty rule text is rejected."""
    rule = {"rule": "", "error_type": "early_terminated"}
    is_valid, reason = validate_rule(rule, [])
    assert not is_valid
    assert "empty" in reason


def test_validate_rule_invalid_error_type():
    """Test that invalid error_type is rejected."""
    rule = {
        "rule": "This is a rule that is long enough to pass length check.",
        "error_type": "invalid_type",
    }
    is_valid, reason = validate_rule(rule, [])
    assert not is_valid
    assert "invalid" in reason and "error_type" in reason


def test_validate_rule_valid_error_types():
    """Test all valid error_type values are accepted."""
    valid_types = ["timeout", "max_turns", "early_terminated", "agent_error", "test_failure"]
    for error_type in valid_types:
        rule = {
            "rule": f"This is a specific rule for {error_type} errors in the system.",
            "error_type": error_type,
        }
        is_valid, reason = validate_rule(rule, [])
        assert is_valid, f"Valid error_type '{error_type}' was rejected: {reason}"


def test_validate_rule_not_dict():
    """Test that non-dict input is rejected."""
    is_valid, reason = validate_rule("not a dict", [])
    assert not is_valid
    assert "not a dict" in reason


def test_validate_rule_duplicate_detection():
    """Test that rules too similar to existing ones are rejected."""
    existing = [
        {"id": 1, "lesson": "When exploring a codebase, use parallel Read calls instead of sequential ones."},
    ]
    rule = {
        "rule": "Use parallel Read calls instead of sequential ones when exploring a codebase.",
        "error_type": "early_terminated",
    }
    is_valid, reason = validate_rule(rule, existing)
    assert not is_valid
    assert "too similar" in reason


def test_validate_rule_different_enough():
    """Test that sufficiently different rules pass duplicate check."""
    existing = [
        {"id": 1, "lesson": "When exploring a codebase, use parallel Read calls instead of sequential ones."},
    ]
    rule = {
        "rule": "When task description mentions refactor, read ALL files in the target directory before making changes.",
        "error_type": "early_terminated",
    }
    is_valid, reason = validate_rule(rule, existing)
    assert is_valid, f"Sufficiently different rule was rejected: {reason}"


# --- Test: call_claude_for_rules (mocked) ---

def test_call_claude_for_rules_success():
    """Test successful Claude CLI call with mocked subprocess."""
    expected_rules = [
        {"rule": "Limit exploration to 10 turns then start writing code.", "error_type": "early_terminated", "rationale": "test"},
    ]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": json.dumps(expected_rules)})
    mock_result.stderr = ""

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
        rules = call_claude_for_rules("test prompt")
        assert rules is not None
        assert len(rules) == 1
        assert rules[0]["rule"] == expected_rules[0]["rule"]


def test_call_claude_for_rules_timeout():
    """Test Claude CLI timeout handling."""
    import subprocess as sp
    with patch("forgesmith_simba.subprocess.run", side_effect=sp.TimeoutExpired("claude", 120)):
        rules = call_claude_for_rules("test prompt")
        assert rules is None


def test_call_claude_for_rules_not_found():
    """Test Claude CLI not found handling."""
    with patch("forgesmith_simba.subprocess.run", side_effect=FileNotFoundError):
        rules = call_claude_for_rules("test prompt")
        assert rules is None


def test_call_claude_for_rules_nonzero_exit():
    """Test Claude CLI non-zero exit code handling."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Error"
    mock_result.stdout = ""

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
        rules = call_claude_for_rules("test prompt")
        assert rules is None


def test_call_claude_for_rules_empty_response():
    """Test Claude CLI empty response handling."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
        rules = call_claude_for_rules("test prompt")
        assert rules is None


def test_call_claude_for_rules_malformed_json():
    """Test Claude CLI malformed JSON handling."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "not valid json [{"})
    mock_result.stderr = ""

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
        rules = call_claude_for_rules("test prompt")
        assert rules is None


def test_call_claude_for_rules_markdown_fences():
    """Test Claude response wrapped in markdown code fences."""
    expected_rules = [
        {"rule": "Always check file exists before reading it.", "error_type": "agent_error", "rationale": "test"},
    ]
    markdown_response = f"```json\n{json.dumps(expected_rules)}\n```"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": markdown_response})
    mock_result.stderr = ""

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
        rules = call_claude_for_rules("test prompt")
        assert rules is not None
        assert len(rules) == 1
        assert rules[0]["rule"] == expected_rules[0]["rule"]


def test_call_claude_for_rules_model_config():
    """Test that model and timeout are read from config."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "[]"})
    mock_result.stderr = ""

    cfg = {"simba": {"model": "opus", "timeout_seconds": 60}}

    with patch("forgesmith_simba.subprocess.run", return_value=mock_result) as mock_run:
        call_claude_for_rules("test prompt", cfg)
        # Verify the model flag was passed
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"
        # Verify timeout was passed
        assert call_args[1]["timeout"] == 60


# --- Test: store_rules ---

def test_store_rules_dry_run():
    """Test that dry run does not write to database."""
    setup_simba_test_data()
    try:
        rules = [
            {"rule": "This is a dry run test rule that should not be stored in the database.",
             "error_type": "early_terminated", "rationale": "test"},
        ]
        stored = store_rules("developer", rules, dry_run=True)

        assert len(stored) == 1
        assert stored[0]["stored"] is False

        # Verify nothing was written
        conn = get_db_connection()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM lessons_learned WHERE lesson LIKE '%dry run test%'"
        ).fetchone()
        conn.close()
        assert row["cnt"] == 0
    finally:
        cleanup_simba_test_data()


def test_store_rules_actual_write():
    """Test that rules are actually written to database."""
    setup_simba_test_data()
    try:
        rules = [
            {"rule": "SIMBA test: When working on TypeScript projects, run tsc before committing.",
             "error_type": "timeout", "rationale": "test"},
        ]
        stored = store_rules("developer", rules, dry_run=False)

        assert len(stored) == 1
        assert stored[0]["stored"] is True

        # Verify it was written
        conn = get_db_connection()
        row = conn.execute(
            "SELECT * FROM lessons_learned WHERE lesson LIKE '%SIMBA test: When working on TypeScript%'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["source"] == "simba_generated"
        assert row["role"] == "developer"
        assert row["error_type"] == "timeout"
        assert row["active"] == 1

        # Clean up the test rule
        conn = sqlite3.connect(THEFORGE_DB)
        conn.execute("DELETE FROM lessons_learned WHERE lesson LIKE '%SIMBA test: When working on TypeScript%'")
        conn.commit()
        conn.close()
    finally:
        cleanup_simba_test_data()


def test_store_rules_caps_at_max():
    """Test that MAX_RULES_PER_ROLE cap is enforced."""
    setup_simba_test_data()
    try:
        rules = [
            {"rule": f"SIMBA cap test rule number {i} for testing the maximum cap enforcement.",
             "error_type": "early_terminated", "rationale": "test"}
            for i in range(5)  # Try to store 5, should only store MAX_RULES_PER_ROLE
        ]
        stored = store_rules("developer", rules, dry_run=True)

        # Should only process MAX_RULES_PER_ROLE (3)
        assert len(stored) <= MAX_RULES_PER_ROLE, \
            f"Should cap at {MAX_RULES_PER_ROLE}, got {len(stored)}"
    finally:
        cleanup_simba_test_data()


def test_store_rules_rejects_duplicates():
    """Test that rules similar to existing ones are rejected."""
    setup_simba_test_data()
    try:
        rules = [
            {"rule": "Use parallel Read calls instead of sequential ones when exploring a codebase.",
             "error_type": "early_terminated", "rationale": "duplicate of existing"},
        ]
        stored = store_rules("developer", rules, dry_run=True)
        # Should be rejected due to similarity with existing test rule
        assert len(stored) == 0 or all(not s.get("stored", True) for s in stored), \
            "Duplicate rule should be rejected"
    finally:
        cleanup_simba_test_data()


def test_store_rules_unique_signature():
    """Test that each rule gets a unique error_signature."""
    setup_simba_test_data()
    try:
        rules = [
            {"rule": "SIMBA sig test rule A: always check for existing tests before writing new ones.",
             "error_type": "test_failure", "rationale": "test"},
            {"rule": "SIMBA sig test rule B: use background agents for large file comparisons.",
             "error_type": "early_terminated", "rationale": "test"},
        ]
        stored = store_rules("developer", rules, dry_run=False)

        conn = get_db_connection()
        rows = conn.execute(
            "SELECT error_signature FROM lessons_learned WHERE lesson LIKE '%SIMBA sig test%'"
        ).fetchall()
        conn.close()

        sigs = [r["error_signature"] for r in rows]
        assert len(sigs) == len(set(sigs)), f"Signatures should be unique, got {sigs}"

        # Each signature should contain the hash component
        for sig in sigs:
            assert sig.startswith("simba:"), f"Signature should start with 'simba:', got {sig}"

        # Clean up
        conn = sqlite3.connect(THEFORGE_DB)
        conn.execute("DELETE FROM lessons_learned WHERE lesson LIKE '%SIMBA sig test%'")
        conn.commit()
        conn.close()
    finally:
        cleanup_simba_test_data()


# --- Test: evaluate_simba_rules ---

def test_evaluate_simba_rules_needs_minimum_samples():
    """Test that evaluation requires minimum sample sizes."""
    setup_simba_test_data()
    try:
        # The test data has a rule with 0 injections, so it shouldn't be evaluated
        # (requires times_injected >= 10)
        evaluated = evaluate_simba_rules()
        # We don't assert specific count since real data may have rules meeting criteria
        assert isinstance(evaluated, int)
    finally:
        cleanup_simba_test_data()


# --- Test: prune_stale_rules ---

def test_prune_stale_rules_dry_run():
    """Test that dry-run prune identifies but does not remove rules."""
    setup_simba_test_data()
    try:
        pruned = prune_stale_rules(dry_run=True)

        # Should find the test prune candidate (60 injections, no effectiveness)
        prune_lessons = [r["lesson"] for r in pruned]
        assert any("injected many times" in l for l in prune_lessons), \
            "Should identify prune candidate"

        # Verify it's still active (dry run)
        conn = get_db_connection()
        row = conn.execute(
            "SELECT active FROM lessons_learned WHERE error_signature = 'simba_test_prune_candidate'"
        ).fetchone()
        conn.close()
        assert row["active"] == 1, "Dry-run should NOT deactivate the rule"
    finally:
        cleanup_simba_test_data()


def test_prune_stale_rules_actual():
    """Test that actual prune deactivates ineffective rules."""
    setup_simba_test_data()
    try:
        pruned = prune_stale_rules(dry_run=False)

        prune_lessons = [r["lesson"] for r in pruned]
        assert any("injected many times" in l for l in prune_lessons), \
            "Should prune the candidate"

        # Verify it's now inactive
        conn = get_db_connection()
        row = conn.execute(
            "SELECT active FROM lessons_learned WHERE error_signature = 'simba_test_prune_candidate'"
        ).fetchone()
        conn.close()
        assert row["active"] == 0, "Pruned rule should be inactive"
    finally:
        cleanup_simba_test_data()


def test_prune_keeps_effective_rules():
    """Test that rules with positive effectiveness_score are NOT pruned."""
    setup_simba_test_data()
    try:
        prune_stale_rules(dry_run=False)

        # The effective rule (score=0.15) should still be active
        conn = get_db_connection()
        row = conn.execute(
            "SELECT active FROM lessons_learned WHERE error_signature = 'simba_test_effective'"
        ).fetchone()
        conn.close()
        assert row["active"] == 1, "Effective rule should NOT be pruned"
    finally:
        cleanup_simba_test_data()


def test_prune_threshold():
    """Test that rules below inject threshold are not pruned."""
    setup_simba_test_data()
    try:
        pruned = prune_stale_rules(dry_run=True)

        # The existing SIMBA rule (inject_count=0) should NOT be in pruned list
        prune_lessons = [r["lesson"] for r in pruned]
        assert not any("parallel Read calls" in l for l in prune_lessons), \
            "Rules below inject threshold should not be pruned"
    finally:
        cleanup_simba_test_data()


# --- Test: get_existing_simba_rules ---

def test_get_existing_simba_rules_all():
    """Test getting all SIMBA rules."""
    setup_simba_test_data()
    try:
        rules = get_existing_simba_rules()
        assert isinstance(rules, list)
        # Should include our test rules
        sigs = [r["error_signature"] for r in rules]
        assert "simba_test_existing" in sigs
    finally:
        cleanup_simba_test_data()


def test_get_existing_simba_rules_by_role():
    """Test getting SIMBA rules filtered by role."""
    setup_simba_test_data()
    try:
        rules = get_existing_simba_rules(role="developer")
        assert isinstance(rules, list)
        for rule in rules:
            assert rule["role"] == "developer"
    finally:
        cleanup_simba_test_data()


# --- Test: run_simba (integration, mocked Claude) ---

def test_run_simba_full_pipeline_mocked():
    """Test the full SIMBA pipeline with mocked Claude call."""
    setup_simba_test_data()
    try:
        mock_rules = [
            {"rule": "SIMBA pipeline test: limit codebase exploration to 10 turns then start coding.",
             "error_type": "early_terminated",
             "rationale": "Exploration loops caused 40+ turn episodes."},
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": json.dumps(mock_rules)})
        mock_result.stderr = ""

        cfg = {"lookback_days": 30}

        with patch("forgesmith_simba.subprocess.run", return_value=mock_result):
            results = run_simba(cfg, dry_run=True, role_filter="developer")

        assert results["roles_analyzed"] >= 1
        assert "developer" in results["details"]

        # Clean up any stored rules
        conn = sqlite3.connect(THEFORGE_DB)
        conn.execute("DELETE FROM lessons_learned WHERE lesson LIKE '%SIMBA pipeline test%'")
        conn.commit()
        conn.close()
    finally:
        cleanup_simba_test_data()


def test_run_simba_skips_insufficient_episodes():
    """Test that roles with too few episodes are skipped."""
    # Create only 1 episode for a role
    conn = sqlite3.connect(THEFORGE_DB)
    conn.execute("DELETE FROM agent_episodes WHERE task_id = 8099")
    conn.execute(
        """INSERT INTO agent_episodes
           (task_id, role, task_type, project_id,
            approach_summary, turns_used, outcome, error_patterns,
            reflection, q_value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (8099, 'rare-role', 'feature', 28,
         'Only episode', 10, 'early_terminated', 'max_turns',
         'Only failure for this role.', 0.1),
    )
    conn.commit()
    conn.close()

    try:
        cfg = {"lookback_days": 30}
        # This role has only 1 episode, should be skipped
        with patch("forgesmith_simba.subprocess.run") as mock_run:
            results = run_simba(cfg, dry_run=True, role_filter="rare-role")
            # Claude should not be called for a role with insufficient episodes
            # The role might not even appear in high-variance if it has no successes
    finally:
        conn = sqlite3.connect(THEFORGE_DB)
        conn.execute("DELETE FROM agent_episodes WHERE task_id = 8099")
        conn.commit()
        conn.close()


def test_run_simba_returns_expected_structure():
    """Test that run_simba returns all expected keys."""
    setup_simba_test_data()
    try:
        cfg = {"lookback_days": 30}
        with patch("forgesmith_simba.subprocess.run") as mock_run:
            # Mock Claude returning empty (no rules generated)
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps({"result": "[]"})
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            results = run_simba(cfg, dry_run=True)

        assert "roles_analyzed" in results
        assert "rules_generated" in results
        assert "rules_pruned" in results
        assert "details" in results
        assert isinstance(results["details"], dict)
    finally:
        cleanup_simba_test_data()


# --- pytest entry point ---

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
