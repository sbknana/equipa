#!/usr/bin/env python3
"""Test suite for lessons_learned injection into agent prompts.

Validates Task #638 acceptance criteria:
1. Query lessons_learned for agent role and error_type before spawning
2. Append up to 3 relevant lessons to agent prompt
3. Track times_injected counter
4. Only inject active lessons (active = 1)
"""

import sqlite3
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from forge_orchestrator import (
    format_lessons_for_injection,
    update_lesson_injection_count,
    build_system_prompt,
    THEFORGE_DB,
)
from forgesmith import get_relevant_lessons


def setup_test_data():
    """Insert test lessons into lessons_learned table."""
    conn = sqlite3.connect(THEFORGE_DB)
    conn.execute("DELETE FROM lessons_learned")  # Clean slate

    # Insert test lessons
    test_lessons = [
        ("developer", "timeout", "Timeout in build step",
         "Run build commands with explicit timeout flags", 1, 5, 0, 0.8),
        ("developer", "max_turns", "Max turns hit during refactor",
         "Plan before coding, make fewer larger edits", 1, 3, 0, 0.6),
        ("tester", "test_failure", "Tests fail with import error",
         "Verify virtual environment is activated before running tests", 1, 2, 0, 0.7),
        ("developer", None, "Generic developer tip",
         "Read existing code before making changes", 1, 10, 0, 0.9),
        ("developer", "timeout", "Another timeout pattern",
         "This lesson is inactive and should not be injected", 0, 1, 0, 0.0),
    ]

    conn.executemany(
        """INSERT INTO lessons_learned
           (role, error_type, error_signature, lesson, active, times_seen, times_injected, effectiveness_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        test_lessons
    )
    conn.commit()
    conn.close()
    print("✓ Test data inserted")


def test_get_relevant_lessons_by_role():
    """Test 1: Query lessons by role only."""
    print("\n--- Test 1: get_relevant_lessons(role='developer') ---")
    lessons = get_relevant_lessons(role="developer", limit=5)

    assert len(lessons) > 0, "Should return at least one lesson for developer role"
    assert all(l.get("lesson") for l in lessons), "All lessons should have lesson text"

    # Verify only active lessons returned
    for lesson in lessons:
        print(f"  - {lesson['lesson'][:60]}... (times_seen={lesson['times_seen']})")

    # Check that inactive lesson is NOT included
    inactive_found = any("inactive" in l["lesson"].lower() for l in lessons)
    assert not inactive_found, "Inactive lessons should not be returned"

    print(f"✓ Returned {len(lessons)} active developer lessons")


def test_get_relevant_lessons_by_role_and_error():
    """Test 2: Query lessons by role AND error_type."""
    print("\n--- Test 2: get_relevant_lessons(role='developer', error_type='timeout') ---")
    lessons = get_relevant_lessons(role="developer", error_type="timeout", limit=5)

    assert len(lessons) >= 1, "Should return timeout-related lessons"

    for lesson in lessons:
        print(f"  - {lesson['lesson']}")
        # Should only return active timeout lessons
        assert lesson.get("lesson") != "This lesson is inactive and should not be injected"

    print(f"✓ Returned {len(lessons)} active timeout lessons for developer")


def test_get_relevant_lessons_limit():
    """Test 3: Verify limit parameter works (max 3 for injection)."""
    print("\n--- Test 3: get_relevant_lessons(role='developer', limit=3) ---")
    lessons = get_relevant_lessons(role="developer", limit=3)

    assert len(lessons) <= 3, f"Should return at most 3 lessons, got {len(lessons)}"
    print(f"✓ Limit working: returned {len(lessons)} lessons (max 3)")


def test_format_lessons_for_injection():
    """Test 4: Verify formatting for prompt injection."""
    print("\n--- Test 4: format_lessons_for_injection() ---")
    lessons = get_relevant_lessons(role="developer", limit=3)
    formatted = format_lessons_for_injection(lessons)

    assert "## Lessons from Previous Runs" in formatted, "Should include section heading"
    assert formatted.count("\n- ") >= len(lessons), "Should have bullet points for each lesson"

    print("Formatted output preview:")
    print(formatted[:300] + "..." if len(formatted) > 300 else formatted)
    print("✓ Formatting correct")


def test_update_lesson_injection_count():
    """Test 5: Verify times_injected counter increments."""
    print("\n--- Test 5: update_lesson_injection_count() ---")

    # Get a lesson and check current count
    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT id, times_injected FROM lessons_learned WHERE role = 'developer' AND active = 1 LIMIT 1"
    ).fetchone()

    lesson_id = before["id"]
    count_before = before["times_injected"]
    conn.close()

    # Update counter
    update_lesson_injection_count([lesson_id])

    # Verify increment
    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row
    after = conn.execute(
        "SELECT times_injected FROM lessons_learned WHERE id = ?", (lesson_id,)
    ).fetchone()
    count_after = after["times_injected"]
    conn.close()

    assert count_after == count_before + 1, f"Expected {count_before + 1}, got {count_after}"
    print(f"✓ Injection count incremented: {count_before} → {count_after}")


def test_build_system_prompt_with_lessons():
    """Test 6: Verify lessons are injected into system prompt."""
    print("\n--- Test 6: build_system_prompt() with lessons ---")

    # Create a minimal task
    task = {
        "id": 999,
        "project_id": 28,
        "title": "Test task",
        "description": "Testing lessons injection",
        "task_type": "feature",
    }

    # Mock project context
    project_context = {
        "title": "Test Project",
        "description": "Test project context",
        "working_directory": "/tmp/test-projects/example",
    }

    # Build prompt with lessons (developer role, timeout error)
    prompt = build_system_prompt(
        task=task,
        project_context=project_context,
        project_dir="/tmp/test-projects/example",
        role="developer",
        error_type="timeout",
    )

    # Verify lessons section is present
    assert "## Lessons from Previous Runs" in prompt, "Lessons section should be in prompt"
    assert "timeout" in prompt.lower() or "build" in prompt.lower(), \
        "Should contain timeout-related lesson"

    # Verify it's after the role prompt but before task details
    lessons_pos = prompt.find("## Lessons from Previous Runs")
    task_pos = prompt.find("## Assigned Task")
    assert lessons_pos < task_pos, "Lessons should come before task section"

    print("✓ Lessons successfully injected into system prompt")
    print(f"  Prompt length: {len(prompt)} chars")

    # Extract and show the lessons section
    lessons_section = prompt[lessons_pos:task_pos].strip()
    print("\nInjected lessons section:")
    print(lessons_section[:400] + "..." if len(lessons_section) > 400 else lessons_section)


def test_only_active_lessons():
    """Test 7: Verify inactive lessons are never returned."""
    print("\n--- Test 7: Verify active=0 lessons excluded ---")

    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row

    # Count inactive lessons in DB
    inactive_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM lessons_learned WHERE active = 0"
    ).fetchone()["cnt"]

    conn.close()

    print(f"Inactive lessons in DB: {inactive_count}")

    # Query with role that has inactive lessons
    lessons = get_relevant_lessons(role="developer", limit=10)

    # Verify none of them are the inactive one
    inactive_found = any("inactive" in l["lesson"].lower() for l in lessons)
    assert not inactive_found, "Inactive lessons must be excluded"

    print(f"✓ All {len(lessons)} returned lessons are active (active=1)")


def test_times_injected_tracking():
    """Test 8: End-to-end test of injection counter tracking."""
    print("\n--- Test 8: End-to-end injection counter tracking ---")

    # Use get_relevant_lessons for both "before" snapshot and injection
    # so we compare the exact same lesson IDs (avoids ordering mismatches).
    lessons = get_relevant_lessons(role="developer", limit=3)
    lesson_ids = [l["id"] for l in lessons]
    print(f"Selected {len(lesson_ids)} lessons for injection: {lesson_ids}")

    # Snapshot current counts for the exact IDs we will update
    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        f"""SELECT id, times_injected
           FROM lessons_learned
           WHERE id IN ({','.join('?' * len(lesson_ids))})""",
        lesson_ids
    ).fetchall()
    conn.close()

    before_counts = {row["id"]: row["times_injected"] for row in before}
    print(f"Before injection: {before_counts}")

    # Perform the injection count update
    update_lesson_injection_count(lesson_ids)

    # Verify all counters incremented
    conn = sqlite3.connect(THEFORGE_DB)
    conn.row_factory = sqlite3.Row
    after = conn.execute(
        f"""SELECT id, times_injected
           FROM lessons_learned
           WHERE id IN ({','.join('?' * len(lesson_ids))})""",
        lesson_ids
    ).fetchall()
    conn.close()

    for row in after:
        before_count = before_counts[row["id"]]
        after_count = row["times_injected"]
        assert after_count == before_count + 1, \
            f"Lesson {row['id']} count did not increment: {before_count} → {after_count}"
        print(f"  Lesson {row['id']}: {before_count} → {after_count} ✓")

    print(f"✓ All {len(lesson_ids)} injection counters incremented correctly")


def run_all_tests():
    """Execute all test cases."""
    print("=" * 70)
    print("LESSONS INJECTION TEST SUITE — Task #638")
    print("=" * 70)

    try:
        setup_test_data()
        test_get_relevant_lessons_by_role()
        test_get_relevant_lessons_by_role_and_error()
        test_get_relevant_lessons_limit()
        test_format_lessons_for_injection()
        test_update_lesson_injection_count()
        test_build_system_prompt_with_lessons()
        test_only_active_lessons()
        test_times_injected_tracking()

        print("\n" + "=" * 70)
        print("✓ ALL TESTS PASSED — Feature working as specified")
        print("=" * 70)
        return 0

    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())

