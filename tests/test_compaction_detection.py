"""Tests for compaction detection and enhanced checkpointing (Phase 2B).

Tests:
- detect_compaction_signals() in monitoring.py
- save_soft_checkpoint() / load_soft_checkpoint() in checkpoints.py
- build_compaction_recovery_context() in checkpoints.py
- _load_forge_state_json() in loops.py

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from equipa.monitoring import detect_compaction_signals
from equipa.checkpoints import (
    SOFT_CHECKPOINT_INTERVAL,
    SOFT_CHECKPOINT_TEXT_LIMIT,
    build_compaction_recovery_context,
    clear_checkpoints,
    load_soft_checkpoint,
    save_soft_checkpoint,
)
from equipa.loops import _load_forge_state_json


# ---------------------------------------------------------------------------
# detect_compaction_signals tests
# ---------------------------------------------------------------------------

class TestDetectCompactionSignals:
    """Tests for the compaction signal detection function."""

    def test_no_signals_on_normal_output(self):
        signals = detect_compaction_signals(
            text="Editing file src/main.py with new validation logic.",
            turn_count=5,
            files_read=set(),
            recent_tool_calls=["Edit|src/main.py"],
            turns_since_last_tool=0,
        )
        assert signals == []

    def test_reintroduction_signal(self):
        signals = detect_compaction_signals(
            text="Hello! How can I help you today?",
            turn_count=15,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=0,
        )
        assert len(signals) == 1
        assert signals[0]["type"] == "reintroduction"
        assert "how can i help" in signals[0]["detail"].lower()

    @pytest.mark.parametrize("phrase", [
        "What would you like me to work on?",
        "How can I assist you with this task?",
        "I'd be happy to help with that!",
        "Let me know what you'd like me to do.",
        "I'm ready to help you today.",
    ])
    def test_multiple_reintroduction_phrases(self, phrase: str):
        signals = detect_compaction_signals(
            text=phrase,
            turn_count=10,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=0,
        )
        reintro = [s for s in signals if s["type"] == "reintroduction"]
        assert len(reintro) == 1

    def test_file_reread_signal(self):
        signals = detect_compaction_signals(
            text="Let me read the file again.",
            turn_count=20,
            files_read={"/srv/project/src/main.py", "/srv/project/README.md"},
            recent_tool_calls=["Read|/srv/project/src/main.py"],
            turns_since_last_tool=0,
        )
        reread = [s for s in signals if s["type"] == "file_reread"]
        assert len(reread) == 1
        assert "main.py" in reread[0]["detail"]

    def test_file_reread_no_signal_for_new_file(self):
        signals = detect_compaction_signals(
            text="Reading a new file.",
            turn_count=20,
            files_read={"/srv/project/src/main.py"},
            recent_tool_calls=["Read|/srv/project/src/utils.py"],
            turns_since_last_tool=0,
        )
        reread = [s for s in signals if s["type"] == "file_reread"]
        assert len(reread) == 0

    def test_stale_tools_signal(self):
        signals = detect_compaction_signals(
            text="I'm thinking about what to do next...",
            turn_count=25,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=5,
        )
        stale = [s for s in signals if s["type"] == "stale_tools"]
        assert len(stale) == 1
        assert "5 turns" in stale[0]["detail"]

    def test_stale_tools_no_signal_below_threshold(self):
        signals = detect_compaction_signals(
            text="Working...",
            turn_count=10,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=3,
        )
        stale = [s for s in signals if s["type"] == "stale_tools"]
        assert len(stale) == 0

    def test_repetitive_output_signal(self):
        # Build text with many repeated sentences
        repeated = "This is a very important sentence that keeps appearing. "
        text = (repeated * 6) + "One unique sentence here."
        signals = detect_compaction_signals(
            text=text,
            turn_count=30,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=0,
        )
        repetitive = [s for s in signals if s["type"] == "repetitive_output"]
        assert len(repetitive) == 1

    def test_no_repetitive_signal_with_varied_text(self):
        text = (
            "First I will read the configuration file. "
            "Then I will modify the handler function. "
            "After that, I will update the test suite. "
            "Finally, I will commit the changes."
        )
        signals = detect_compaction_signals(
            text=text,
            turn_count=10,
            files_read=set(),
            recent_tool_calls=[],
            turns_since_last_tool=0,
        )
        repetitive = [s for s in signals if s["type"] == "repetitive_output"]
        assert len(repetitive) == 0

    def test_multiple_signals_at_once(self):
        """Multiple compaction signals can fire simultaneously."""
        signals = detect_compaction_signals(
            text="How can I help you? How can I help you? How can I help you? "
                 "How can I help you? How can I help you?",
            turn_count=20,
            files_read={"/srv/project/main.py"},
            recent_tool_calls=["Read|/srv/project/main.py"],
            turns_since_last_tool=6,
        )
        types = {s["type"] for s in signals}
        assert "reintroduction" in types
        assert "file_reread" in types
        assert "stale_tools" in types


# ---------------------------------------------------------------------------
# Soft checkpoint tests
# ---------------------------------------------------------------------------

class TestSoftCheckpoint:
    """Tests for soft checkpoint save/load/clear."""

    @pytest.fixture(autouse=True)
    def patch_checkpoint_dir(self, tmp_path, monkeypatch):
        """Redirect CHECKPOINT_DIR to a temp directory."""
        monkeypatch.setattr(
            "equipa.checkpoints.CHECKPOINT_DIR", tmp_path / "checkpoints"
        )
        self.checkpoint_dir = tmp_path / "checkpoints"

    def test_save_and_load_soft_checkpoint(self):
        path = save_soft_checkpoint(
            task_id=42,
            turn_count=10,
            files_changed={"src/main.py"},
            files_read={"src/main.py", "README.md"},
            last_result_text="Working on validation...",
            compaction_count=0,
            role="developer",
        )
        assert path is not None
        assert path.exists()

        loaded = load_soft_checkpoint(42, role="developer")
        assert loaded is not None
        assert loaded["task_id"] == 42
        assert loaded["turn_count"] == 10
        assert "src/main.py" in loaded["files_changed"]
        assert "README.md" in loaded["files_read"]
        assert loaded["compaction_count"] == 0
        assert "Working on validation" in loaded["last_result_text"]

    def test_load_returns_latest_soft_checkpoint(self):
        save_soft_checkpoint(
            task_id=42, turn_count=10,
            files_changed=set(), files_read=set(),
            last_result_text="first",
        )
        save_soft_checkpoint(
            task_id=42, turn_count=20,
            files_changed={"a.py"}, files_read={"a.py"},
            last_result_text="second",
        )
        loaded = load_soft_checkpoint(42)
        assert loaded is not None
        assert loaded["turn_count"] == 20
        assert "second" in loaded["last_result_text"]

    def test_load_returns_none_when_no_checkpoint(self):
        assert load_soft_checkpoint(999) is None

    def test_clear_removes_soft_checkpoints(self):
        save_soft_checkpoint(
            task_id=42, turn_count=10,
            files_changed=set(), files_read=set(),
            last_result_text="test",
        )
        assert load_soft_checkpoint(42) is not None
        clear_checkpoints(42)
        assert load_soft_checkpoint(42) is None

    def test_text_truncation(self):
        long_text = "x" * (SOFT_CHECKPOINT_TEXT_LIMIT + 500)
        save_soft_checkpoint(
            task_id=42, turn_count=10,
            files_changed=set(), files_read=set(),
            last_result_text=long_text,
        )
        loaded = load_soft_checkpoint(42)
        assert loaded is not None
        assert len(loaded["last_result_text"]) < len(long_text)
        assert "[...truncated...]" in loaded["last_result_text"]

    def test_compaction_signals_stored(self):
        signals = [
            {"type": "reintroduction", "detail": "Agent re-introduced"},
            {"type": "file_reread", "detail": "Re-read main.py"},
        ]
        save_soft_checkpoint(
            task_id=42, turn_count=15,
            files_changed=set(), files_read=set(),
            last_result_text="test",
            compaction_count=2,
            compaction_signals=signals,
        )
        loaded = load_soft_checkpoint(42)
        assert loaded is not None
        assert loaded["compaction_count"] == 2
        assert len(loaded["compaction_signals"]) == 2
        assert loaded["compaction_signals"][0]["type"] == "reintroduction"

    def test_soft_checkpoint_interval_constant(self):
        assert SOFT_CHECKPOINT_INTERVAL == 10


# ---------------------------------------------------------------------------
# build_compaction_recovery_context tests
# ---------------------------------------------------------------------------

class TestBuildCompactionRecoveryContext:
    """Tests for recovery context generation."""

    def test_basic_recovery_context(self):
        soft_cp = {
            "task_id": 42,
            "turn_count": 25,
            "files_changed": ["src/main.py"],
            "files_read": ["src/main.py", "README.md"],
            "last_result_text": "Implementing validation...",
            "compaction_count": 1,
            "compaction_signals": [],
        }
        ctx = build_compaction_recovery_context(soft_cp)
        assert "Context Recovery After Compaction" in ctx
        assert "context limit" in ctx
        assert "src/main.py" in ctx
        assert "README.md" in ctx
        assert "25" in ctx
        assert "RESUME NOW" in ctx

    def test_recovery_context_with_forge_state(self):
        soft_cp = {
            "task_id": 42,
            "turn_count": 15,
            "files_changed": [],
            "files_read": ["a.py"],
            "last_result_text": "",
            "compaction_count": 1,
            "compaction_signals": [],
        }
        forge_state = {
            "task_id": 42,
            "current_step": "implementing validation",
            "next_action": "Fix failing test",
            "files_changed": ["src/router.py"],
            "decisions": ["Using Pydantic for validation"],
        }
        ctx = build_compaction_recovery_context(soft_cp, forge_state)
        assert "implementing validation" in ctx
        assert "Fix failing test" in ctx
        assert "Pydantic" in ctx
        assert "src/router.py" in ctx

    def test_recovery_context_without_forge_state(self):
        soft_cp = {
            "task_id": 42,
            "turn_count": 10,
            "files_changed": [],
            "files_read": [],
            "last_result_text": "working",
            "compaction_count": 0,
            "compaction_signals": [],
        }
        ctx = build_compaction_recovery_context(soft_cp, None)
        assert "forge-state" not in ctx.lower() or "Agent state file" not in ctx


# ---------------------------------------------------------------------------
# _load_forge_state_json tests
# ---------------------------------------------------------------------------

class TestLoadForgeStateJson:
    """Tests for loading .forge-state.json from project dir."""

    def test_loads_valid_state_file(self, tmp_path):
        state_data = {
            "task_id": 42,
            "current_step": "testing",
            "next_action": "run tests",
        }
        state_file = tmp_path / ".forge-state.json"
        state_file.write_text(json.dumps(state_data))
        result = _load_forge_state_json(str(tmp_path))
        assert result is not None
        assert result["task_id"] == 42
        assert result["current_step"] == "testing"

    def test_returns_none_when_no_file(self, tmp_path):
        assert _load_forge_state_json(str(tmp_path)) is None

    def test_returns_none_for_none_dir(self):
        assert _load_forge_state_json(None) is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        state_file = tmp_path / ".forge-state.json"
        state_file.write_text("not valid json {{{")
        assert _load_forge_state_json(str(tmp_path)) is None

    def test_returns_none_for_nonexistent_dir(self):
        assert _load_forge_state_json("/nonexistent/path/12345") is None
