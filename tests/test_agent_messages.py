#!/usr/bin/env python3
"""
Test suite for inter-agent message channel (Task #922).

Tests verify:
1. ensure_schema creates the table idempotently
2. post_agent_message inserts messages correctly
3. read_agent_messages returns only unread messages for the correct role
4. read_agent_messages respects max_cycle filter
5. mark_messages_read marks messages and prevents re-reading
6. format_messages_for_prompt produces correct prompt text
7. format_messages_for_prompt handles JSON and plain-text content
8. format_messages_for_prompt returns empty string for empty list
9. End-to-end: post → read → format → mark → re-read returns empty

Copyright 2026 Forgeborn
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add project root to path so we can import the orchestrator module
sys.path.insert(0, str(Path(__file__).parent.parent))

import forge_orchestrator


# --- Test Helpers ---

def make_temp_db():
    """Create a temporary SQLite database and point THEFORGE_DB at it."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def setup_test_db():
    """Create a temp DB and patch THEFORGE_DB to use it.

    Returns:
        Tuple of (db_path, original_db_path) so the caller can restore after.
    """
    original_db = forge_orchestrator.THEFORGE_DB
    db_path = make_temp_db()
    forge_orchestrator.THEFORGE_DB = db_path
    forge_orchestrator._SCHEMA_ENSURED = False
    return db_path, original_db


def teardown_test_db(db_path, original_db=None):
    """Clean up the temp DB file and restore the original THEFORGE_DB path."""
    try:
        db_path.unlink(missing_ok=True)
    except Exception:
        pass
    if original_db is not None:
        forge_orchestrator.THEFORGE_DB = original_db


# --- Tests for ensure_schema ---

def test_ensure_creates_table():
    """Test that ensure_schema creates the table."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator._SCHEMA_ENSURED = False
        forge_orchestrator.ensure_schema()
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_messages'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1, f"expected 1 table, found {len(tables)}"
    finally:
        teardown_test_db(db_path, original_db)


def test_ensure_is_idempotent():
    """Test that calling ensure twice does not error."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator._SCHEMA_ENSURED = False
        forge_orchestrator.ensure_schema()
        forge_orchestrator.ensure_schema()  # second call should not raise
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_messages'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1, "table should exist exactly once after two ensure calls"
    finally:
        teardown_test_db(db_path, original_db)


# --- Tests for post_agent_message ---

def test_post_inserts_message():
    """Test that post_agent_message inserts a row."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(
            task_id=100, cycle=1, from_role="tester", to_role="developer",
            msg_type="test_failures", content='{"tests_failed": 3}'
        )
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM agent_messages").fetchall()
        conn.close()
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
        row = dict(rows[0])
        assert row["task_id"] == 100
        assert row["cycle_number"] == 1
        assert row["from_role"] == "tester"
        assert row["to_role"] == "developer"
        assert row["message_type"] == "test_failures"
        assert row["content"] == '{"tests_failed": 3}'
        assert row["read_by_cycle"] is None, "new messages should be unread"
    finally:
        teardown_test_db(db_path, original_db)


def test_post_multiple_messages():
    """Test posting multiple messages for the same task."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(100, 2, "tester", "developer", "test_passed", '{"b":2}')
        forge_orchestrator.post_agent_message(100, 1, "developer", "tester", "code_notes", '{"c":3}')

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]
        conn.close()
        assert count == 3, f"expected 3 messages, got {count}"
    finally:
        teardown_test_db(db_path, original_db)


# --- Tests for read_agent_messages ---

def test_read_returns_unread_for_role():
    """Test that read_agent_messages returns only messages addressed to the specified role."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(100, 1, "developer", "tester", "code_notes", '{"b":2}')

        dev_msgs = forge_orchestrator.read_agent_messages(100, "developer")
        assert len(dev_msgs) == 1, f"developer should get 1 message, got {len(dev_msgs)}"
        assert dev_msgs[0]["from_role"] == "tester"

        tester_msgs = forge_orchestrator.read_agent_messages(100, "tester")
        assert len(tester_msgs) == 1, f"tester should get 1 message, got {len(tester_msgs)}"
        assert tester_msgs[0]["from_role"] == "developer"
    finally:
        teardown_test_db(db_path, original_db)


def test_read_filters_by_task():
    """Test that read_agent_messages only returns messages for the specified task."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(200, 1, "tester", "developer", "test_failures", '{"b":2}')

        msgs_100 = forge_orchestrator.read_agent_messages(100, "developer")
        assert len(msgs_100) == 1, f"task 100 should get 1 message, got {len(msgs_100)}"

        msgs_200 = forge_orchestrator.read_agent_messages(200, "developer")
        assert len(msgs_200) == 1, f"task 200 should get 1 message, got {len(msgs_200)}"

        msgs_999 = forge_orchestrator.read_agent_messages(999, "developer")
        assert len(msgs_999) == 0, f"task 999 should get 0 messages, got {len(msgs_999)}"
    finally:
        teardown_test_db(db_path, original_db)


def test_read_respects_max_cycle():
    """Test that max_cycle filters out messages from later cycles."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"c1":1}')
        forge_orchestrator.post_agent_message(100, 2, "tester", "developer", "test_failures", '{"c2":2}')
        forge_orchestrator.post_agent_message(100, 3, "tester", "developer", "test_passed", '{"c3":3}')

        msgs = forge_orchestrator.read_agent_messages(100, "developer", max_cycle=2)
        assert len(msgs) == 2, f"max_cycle=2 should return 2 messages, got {len(msgs)}"
        cycles = [m["cycle_number"] for m in msgs]
        assert 3 not in cycles, "cycle 3 should be filtered out"
    finally:
        teardown_test_db(db_path, original_db)


def test_read_returns_ordered_by_cycle_and_id():
    """Test that messages are ordered by cycle_number ASC, id ASC."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 2, "tester", "developer", "test_failures", '{"second":2}')
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"first":1}')
        forge_orchestrator.post_agent_message(100, 2, "tester", "developer", "code_notes", '{"third":3}')

        msgs = forge_orchestrator.read_agent_messages(100, "developer")
        assert len(msgs) == 3
        assert msgs[0]["cycle_number"] == 1, "first message should be cycle 1"
        assert msgs[1]["cycle_number"] == 2
        assert msgs[2]["cycle_number"] == 2
    finally:
        teardown_test_db(db_path, original_db)


def test_read_returns_empty_list_for_no_messages():
    """Test that read returns empty list when no messages exist."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.ensure_schema()
        msgs = forge_orchestrator.read_agent_messages(999, "developer")
        assert msgs == [], f"expected empty list, got {msgs}"
    finally:
        teardown_test_db(db_path, original_db)


# --- Tests for mark_messages_read ---

def test_mark_read_prevents_re_reading():
    """Test that mark_messages_read makes messages invisible to subsequent reads."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "code_notes", '{"b":2}')

        # First read should return 2 messages
        msgs = forge_orchestrator.read_agent_messages(100, "developer")
        assert len(msgs) == 2, f"expected 2 messages before mark, got {len(msgs)}"

        # Mark as read
        forge_orchestrator.mark_messages_read(100, "developer", cycle_number=2)

        # Second read should return 0
        msgs_after = forge_orchestrator.read_agent_messages(100, "developer")
        assert len(msgs_after) == 0, f"expected 0 messages after mark, got {len(msgs_after)}"
    finally:
        teardown_test_db(db_path, original_db)


def test_mark_read_sets_correct_cycle():
    """Test that mark_messages_read sets read_by_cycle to the given cycle number."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.mark_messages_read(100, "developer", cycle_number=3)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT read_by_cycle FROM agent_messages WHERE id = 1").fetchone()
        conn.close()
        assert row["read_by_cycle"] == 3, f"expected read_by_cycle=3, got {row['read_by_cycle']}"
    finally:
        teardown_test_db(db_path, original_db)


def test_mark_read_only_affects_target_role():
    """Test that mark_messages_read only marks messages for the specified role."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(100, 1, "developer", "tester", "code_notes", '{"b":2}')

        # Mark developer's messages as read
        forge_orchestrator.mark_messages_read(100, "developer", cycle_number=2)

        # Tester's messages should still be unread
        tester_msgs = forge_orchestrator.read_agent_messages(100, "tester")
        assert len(tester_msgs) == 1, f"tester should still have 1 unread message, got {len(tester_msgs)}"
    finally:
        teardown_test_db(db_path, original_db)


def test_mark_read_only_affects_target_task():
    """Test that mark_messages_read only marks messages for the specified task."""
    db_path, original_db = setup_test_db()
    try:
        forge_orchestrator.post_agent_message(100, 1, "tester", "developer", "test_failures", '{"a":1}')
        forge_orchestrator.post_agent_message(200, 1, "tester", "developer", "test_failures", '{"b":2}')

        forge_orchestrator.mark_messages_read(100, "developer", cycle_number=2)

        # Task 200 messages should still be unread
        msgs_200 = forge_orchestrator.read_agent_messages(200, "developer")
        assert len(msgs_200) == 1, f"task 200 should still have 1 unread, got {len(msgs_200)}"
    finally:
        teardown_test_db(db_path, original_db)


# --- Tests for format_messages_for_prompt ---

def test_format_empty_list():
    """Test that format_messages_for_prompt returns empty string for empty list."""
    result = forge_orchestrator.format_messages_for_prompt([])
    assert result == "", f"expected empty string, got '{result}'"


def test_format_json_content():
    """Test that JSON content is unpacked into key-value format."""
    messages = [{
        "from_role": "tester",
        "message_type": "test_failures",
        "content": json.dumps({"tests_run": 10, "tests_failed": 2}),
        "cycle_number": 1,
    }]
    result = forge_orchestrator.format_messages_for_prompt(messages)
    assert "## Messages from Other Agents" in result
    assert "**[tester]**" in result
    assert "cycle 1" in result
    assert "test_failures" in result
    assert "tests_run: 10" in result
    assert "tests_failed: 2" in result


def test_format_plain_text_content():
    """Test that non-JSON content is used as-is."""
    messages = [{
        "from_role": "developer",
        "message_type": "code_notes",
        "content": "Fixed the auth middleware",
        "cycle_number": 2,
    }]
    result = forge_orchestrator.format_messages_for_prompt(messages)
    assert "Fixed the auth middleware" in result
    assert "**[developer]**" in result
    assert "cycle 2" in result


def test_format_multiple_messages():
    """Test formatting multiple messages."""
    messages = [
        {
            "from_role": "tester",
            "message_type": "test_failures",
            "content": json.dumps({"tests_failed": 3}),
            "cycle_number": 1,
        },
        {
            "from_role": "tester",
            "message_type": "test_passed",
            "content": json.dumps({"tests_run": 15}),
            "cycle_number": 2,
        },
    ]
    result = forge_orchestrator.format_messages_for_prompt(messages)
    # Header is "## Messages from Other Agents\n" (has trailing \n), then 2 message lines
    # So split produces: header, empty line, msg1, msg2 = 4 lines
    non_empty = [l for l in result.strip().split("\n") if l.strip()]
    assert len(non_empty) == 3, f"expected 3 non-empty lines (header + 2 msgs), got {len(non_empty)}: {non_empty}"
    assert non_empty[0].startswith("## Messages from Other Agents")
    assert "cycle 1" in non_empty[1]
    assert "cycle 2" in non_empty[2]


def test_format_handles_missing_keys():
    """Test that format handles messages with missing keys gracefully."""
    messages = [{}]  # empty dict
    result = forge_orchestrator.format_messages_for_prompt(messages)
    assert "unknown" in result, "should use 'unknown' for missing keys"


def test_format_handles_non_dict_json():
    """Test that format handles JSON arrays and other non-dict types."""
    messages = [{
        "from_role": "tester",
        "message_type": "test_results",
        "content": json.dumps([1, 2, 3]),
        "cycle_number": 1,
    }]
    result = forge_orchestrator.format_messages_for_prompt(messages)
    assert "[1, 2, 3]" in result


# --- End-to-end test ---

def test_end_to_end_post_read_format_mark():
    """End-to-end: post → read → format → mark → re-read returns empty."""
    db_path, original_db = setup_test_db()
    try:
        # Post messages
        forge_orchestrator.post_agent_message(
            task_id=42, cycle=1, from_role="tester", to_role="developer",
            msg_type="test_failures",
            content=json.dumps({"tests_run": 10, "tests_failed": 2, "failure_details": ["test_foo failed"]})
        )
        forge_orchestrator.post_agent_message(
            task_id=42, cycle=1, from_role="tester", to_role="developer",
            msg_type="code_notes",
            content="Check the auth module"
        )

        # Read unread messages
        msgs = forge_orchestrator.read_agent_messages(42, "developer")
        assert len(msgs) == 2, f"expected 2 unread messages, got {len(msgs)}"

        # Format for prompt
        formatted = forge_orchestrator.format_messages_for_prompt(msgs)
        assert "## Messages from Other Agents" in formatted
        assert "**[tester]**" in formatted
        assert "tests_failed: 2" in formatted
        assert "Check the auth module" in formatted

        # Mark as read
        forge_orchestrator.mark_messages_read(42, "developer", cycle_number=2)

        # Re-read should be empty
        msgs_after = forge_orchestrator.read_agent_messages(42, "developer")
        assert len(msgs_after) == 0, f"expected 0 after mark, got {len(msgs_after)}"
    finally:
        teardown_test_db(db_path, original_db)


# --- Main ---

def main():
    tests = [
        test_ensure_creates_table,
        test_ensure_is_idempotent,
        test_post_inserts_message,
        test_post_multiple_messages,
        test_read_returns_unread_for_role,
        test_read_filters_by_task,
        test_read_respects_max_cycle,
        test_read_returns_ordered_by_cycle_and_id,
        test_read_returns_empty_list_for_no_messages,
        test_mark_read_prevents_re_reading,
        test_mark_read_sets_correct_cycle,
        test_mark_read_only_affects_target_role,
        test_mark_read_only_affects_target_task,
        test_format_empty_list,
        test_format_json_content,
        test_format_plain_text_content,
        test_format_multiple_messages,
        test_format_handles_missing_keys,
        test_format_handles_non_dict_json,
        test_end_to_end_post_read_format_mark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print(f"  Agent Messages Test Suite (Task #922)")
    print(f"  Testing inter-agent message channel functions")
    print(f"{'=' * 60}\n")

    # Save original DB path to restore after tests
    original_db = forge_orchestrator.THEFORGE_DB

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

    # Restore original DB path
    forge_orchestrator.THEFORGE_DB = original_db

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print(f"  Failed tests: {', '.join(errors)}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
