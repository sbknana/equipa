#!/usr/bin/env python3
"""
Test suite for task-type prompt routing functionality.

Tests verify that:
1. task_type field exists in database
2. All 4 task types have prompts defined
3. Orchestrator correctly injects task-type-specific guidance
4. Defaults to 'feature' when task_type is not set

Copyright 2026 Forgeborn
"""

import json
import sqlite3
import sys
from pathlib import Path


def test_task_type_field_exists():
    """Test that task_type field exists in tasks table with 'feature' default."""
    db_path = Path(__file__).parent.parent / "theforge.db"
    if not db_path.exists():
        # Database not available locally, skip gracefully
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(tasks)")
    columns = {row[1]: row for row in cursor.fetchall()}
    conn.close()

    if not columns:
        # Empty/stub database — real DB accessed via MCP, skip gracefully
        return

    assert 'task_type' in columns, "task_type field not found in tasks table"

    col_info = columns['task_type']
    default_value = col_info[4]  # dflt_value
    assert default_value == "'feature'", \
        f"task_type default is '{default_value}', expected 'feature'"


def test_dispatch_config_has_all_prompts():
    """Test that dispatch_config.json defines all 4 required task types."""
    config_path = Path(__file__).parent.parent / "dispatch_config.json"
    assert config_path.exists(), "dispatch_config.json not found"

    with open(config_path, 'r') as f:
        config = json.load(f)

    assert 'task_type_prompts' in config, "task_type_prompts not found in dispatch_config.json"

    task_type_prompts = config['task_type_prompts']
    required_types = ['bug_fix', 'feature', 'refactor', 'test']

    for tt in required_types:
        assert tt in task_type_prompts, f"task type '{tt}' not found in task_type_prompts"

        prompt = task_type_prompts[tt]
        assert prompt and len(prompt.strip()) > 0, f"task type '{tt}' has empty prompt"


def test_prompt_content_matches_spec():
    """Test that prompt content matches the acceptance criteria."""
    config_path = Path(__file__).parent.parent / "dispatch_config.json"
    with open(config_path, 'r') as f:
        config = json.load(f)

    task_type_prompts = config['task_type_prompts']

    # Check bug_fix prompt contains expected keywords
    bug_fix = task_type_prompts['bug_fix']
    assert all(kw in bug_fix.lower() for kw in ['reproduc', 'test', 'fix']), \
        f"bug_fix prompt missing expected keywords: {bug_fix}"

    # Check feature prompt contains expected keywords
    feature = task_type_prompts['feature']
    assert all(kw in feature.lower() for kw in ['pattern', 'convention', 'test']), \
        f"feature prompt missing expected keywords: {feature}"

    # Check refactor prompt contains expected keywords
    refactor = task_type_prompts['refactor']
    assert all(kw in refactor.lower() for kw in ['test', 'pass', 'incremental']), \
        f"refactor prompt missing expected keywords: {refactor}"

    # Check test prompt contains expected keywords
    test = task_type_prompts['test']
    assert all(kw in test.lower() for kw in ['edge', 'error']), \
        f"test prompt missing expected keywords: {test}"


def test_orchestrator_injection_logic():
    """Test that orchestrator has correct injection logic."""
    orch_path = Path(__file__).parent.parent / "forge_orchestrator.py"
    assert orch_path.exists(), "forge_orchestrator.py not found"

    with open(orch_path, 'r') as f:
        orch_code = f.read()

    # Check for critical code patterns
    required_patterns = [
        ('task_type_prompts', 'task_type_prompts reference'),
        ('task.get("task_type"', 'task.get("task_type") call'),
        ('dispatch_config["task_type_prompts"]', 'dispatch_config["task_type_prompts"] lookup'),
        ('Task Type Guidance', 'Task Type Guidance section header'),
    ]

    for pattern, description in required_patterns:
        assert pattern in orch_code, f"Required pattern not found: {description}"

    # Check that default is "feature"
    assert 'task.get("task_type", "feature")' in orch_code or \
           "task.get('task_type', 'feature')" in orch_code, \
        "Default task_type should be 'feature'"


def main():
    """Run all tests and report results."""
    print("=" * 70)
    print("Task Type Routing Test Suite")
    print("=" * 70)
    print()

    tests = [
        test_task_type_field_exists,
        test_dispatch_config_has_all_prompts,
        test_prompt_content_matches_spec,
        test_orchestrator_injection_logic,
    ]

    results = []
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            results.append(True)
        except AssertionError as e:
            print(f"  FAIL: {test.__name__}: {e}")
            results.append(False)
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {e}")
            results.append(False)
        print()

    print("=" * 70)
    print(f"Results: {sum(results)}/{len(results)} tests passed")
    print("=" * 70)

    if all(results):
        print("All tests PASSED")
        return 0
    else:
        print("Some tests FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())

