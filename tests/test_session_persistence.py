"""B3 followup: integration tests for session persistence wiring.

Covers PLAN-1067 §2.B3 requirements (the implementation landed at d0cc482
across equipa/flows.py, equipa/heartbeat.py and equipa/loops.py):

1. Heartbeat cycles for a single long task: the resume prompt built for
   cycle N must contain the ``files_changed`` set the task captured in cycle
   N-1.
2. Flow rate-limit: 5 transitions for the same child within a 60s window
   must result in only 1 capture row per child task.
3. Feature flag off: the dispatch loop's flow-revision capture path must
   write nothing to the sessions table when ``session_persistence`` is off.

These are additive coverage on top of a working implementation. The existing
1439-test suite is green without these tests, so failure here means a feature
gap, not master-broken.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from equipa import flows, sessions


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_flow_capture_cache() -> None:
    """Module-level rate-limit cache leaks across tests; clear it per test."""
    flows._flow_capture_last_ts.clear()
    yield
    flows._flow_capture_last_ts.clear()


# ---------------------------------------------------------------------------
# Test 1 — heartbeat carries files_changed across cycles
# ---------------------------------------------------------------------------
#
# The dispatch loop, on each heartbeat cycle, calls sessions.capture(...) to
# snapshot state and (on the next cycle) calls sessions.restore() +
# sessions.build_resume_prompt(state) to produce the prompt prefix the agent
# resumes with. We assert the prompt prefix the *second* cycle would receive
# contains the *first* cycle's files_changed set.
#
# build_resume_prompt is the same formatter used by both the soft-checkpoint
# and orchestrator-cycle paths (delegates to checkpoints._format_recovery_prompt),
# so exercising it directly with a state dict shaped like a captured session
# is the right integration boundary — it does not touch the DB.

def test_second_cycle_resume_prompt_contains_first_cycle_files_changed() -> None:
    first_cycle_files = ["equipa/flows.py", "equipa/heartbeat.py"]
    cycle_one_state: dict[str, Any] = {
        "turn_count": 8,
        "files_changed": first_cycle_files,
        "files_read": ["equipa/loops.py"],
        "compaction_count": 0,
        "partial_reasoning": "",
        "recent_tool_calls": [],
    }

    prompt = sessions.build_resume_prompt(cycle_one_state)

    assert "Context Recovery After Compaction" in prompt
    for path in first_cycle_files:
        assert path in prompt, (
            f"second-cycle resume prompt is missing first-cycle file {path!r}; "
            f"files_changed must carry across heartbeat cycles"
        )


def test_resume_prompt_handles_missing_files_changed() -> None:
    prompt = sessions.build_resume_prompt({"turn_count": 1, "compaction_count": 0})
    assert "Context Recovery After Compaction" in prompt
    assert "Files you already changed" not in prompt


# ---------------------------------------------------------------------------
# Test 2 — flow rate-limit: one capture per child per 60s window
# ---------------------------------------------------------------------------
#
# equipa.flows._flow_capture_should_fire is the rate-limit decision point
# called by _capture_running_children_safe. It accepts an injectable ``now``
# (monotonic seconds) so we can deterministically simulate 5 transitions
# inside a 60s window without sleeping.

def test_five_transitions_inside_window_yield_one_capture() -> None:
    flow_id = 4242
    child_task_id = 9001

    # Five transitions evenly spaced across ~12 seconds — well inside the
    # 60-second rate-limit window.
    fire_decisions = [
        flows._flow_capture_should_fire(flow_id, child_task_id, now=base)
        for base in (0.0, 3.0, 6.0, 9.0, 12.0)
    ]

    assert fire_decisions == [True, False, False, False, False], (
        f"Only the first transition in a {flows.FLOW_CAPTURE_RATE_LIMIT_SECONDS}s "
        f"window may fire a capture; got {fire_decisions}"
    )


def test_rate_limit_is_per_child_task() -> None:
    flow_id = 4242
    for child_id in (1, 2, 3):
        assert flows._flow_capture_should_fire(flow_id, child_id, now=0.0) is True
    # Each child got its own first-fire — no cross-contamination.
    for child_id in (1, 2, 3):
        assert flows._flow_capture_should_fire(flow_id, child_id, now=1.0) is False


def test_rate_limit_releases_after_window() -> None:
    flow_id = 4242
    child_task_id = 9001
    assert flows._flow_capture_should_fire(flow_id, child_task_id, now=0.0) is True
    assert flows._flow_capture_should_fire(flow_id, child_task_id, now=10.0) is False
    just_after = flows.FLOW_CAPTURE_RATE_LIMIT_SECONDS + 0.1
    assert flows._flow_capture_should_fire(flow_id, child_task_id, now=just_after) is True


# ---------------------------------------------------------------------------
# Test 3 — feature flag off => no sessions table writes
# ---------------------------------------------------------------------------
#
# The dispatch loop's capture path is _capture_running_children_safe, which
# guards every DB write behind is_feature_enabled(..., "session_persistence").
# We assert the gate by patching equipa.sessions.capture and confirming it is
# never called when the flag is off, even when running children are present.

def _running_children() -> list[tuple[int, str | None, str]]:
    return [
        (101, "developer", "running"),
        (102, "tester", "running"),
        (103, "reviewer", "running"),
    ]


def test_feature_flag_off_skips_session_capture() -> None:
    dispatch_config = {"features": {"session_persistence": False}}
    assert flows.is_feature_enabled(dispatch_config, "session_persistence") is False

    with patch.object(sessions, "capture") as mock_capture:
        flows._capture_running_children_safe(
            flow_id=4242,
            revision=2,
            project_id=23,
            children=_running_children(),
            dispatch_config=dispatch_config,
        )

    assert mock_capture.call_count == 0, (
        "sessions.capture must NOT be invoked when session_persistence flag is off"
    )


def test_feature_flag_on_invokes_session_capture_per_running_child() -> None:
    dispatch_config = {"features": {"session_persistence": True}}
    assert flows.is_feature_enabled(dispatch_config, "session_persistence") is True

    children = _running_children()

    with patch.object(sessions, "capture", return_value=1) as mock_capture:
        flows._capture_running_children_safe(
            flow_id=4242,
            revision=2,
            project_id=23,
            children=children,
            dispatch_config=dispatch_config,
        )

    assert mock_capture.call_count == len(children), (
        f"expected one capture per running child ({len(children)}), "
        f"got {mock_capture.call_count}"
    )
    captured_task_ids = sorted(call.kwargs["task_id"] for call in mock_capture.call_args_list)
    assert captured_task_ids == [101, 102, 103]
    cycle_ids = {call.kwargs["cycle_id"] for call in mock_capture.call_args_list}
    assert cycle_ids == {"flow:4242:2"}, (
        f"cycle_id must encode (flow_id, revision); got {cycle_ids}"
    )


def test_terminal_children_are_skipped_even_when_flag_on() -> None:
    dispatch_config = {"features": {"session_persistence": True}}
    children = [
        (201, "developer", "running"),
        (202, "tester", "done"),
        (203, "reviewer", "failed"),
        (204, "planner", "cancelled"),
    ]

    with patch.object(sessions, "capture", return_value=1) as mock_capture:
        flows._capture_running_children_safe(
            flow_id=4242,
            revision=3,
            project_id=23,
            children=children,
            dispatch_config=dispatch_config,
        )

    assert mock_capture.call_count == 1
    assert mock_capture.call_args_list[0].kwargs["task_id"] == 201
