"""Tests for equipa.hooks — lifecycle hook system.

Tests cover:
- Callback registration and firing
- External hook config loading and validation
- Event name validation
- Error handling (callbacks that raise never crash)
- Unregister / clear operations
- Hook environment variable building
- Async fire_async wrapper

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from equipa.hooks import (
    LIFECYCLE_EVENTS,
    _build_hook_env,
    clear_external_hooks,
    clear_registry,
    fire,
    fire_async,
    get_external_hook_count,
    get_registered_count,
    load_hooks_config,
    register,
    run_external_hook,
    unregister,
)


# --- Fixtures ---

@pytest.fixture(autouse=True)
def clean_registry():
    """Reset hook registry before and after each test."""
    clear_registry()
    clear_external_hooks()
    yield
    clear_registry()
    clear_external_hooks()


# --- Registration Tests ---

class TestRegister:
    def test_register_valid_event(self):
        """Registering a callback for a known event succeeds."""
        cb = MagicMock()
        register("pre_cycle", cb)
        assert get_registered_count("pre_cycle") == 1

    def test_register_multiple_callbacks(self):
        """Multiple callbacks can be registered for the same event."""
        cb1 = MagicMock()
        cb2 = MagicMock()
        register("post_cycle", cb1)
        register("post_cycle", cb2)
        assert get_registered_count("post_cycle") == 2

    def test_register_invalid_event_raises(self):
        """Registering for an unknown event raises ValueError."""
        with pytest.raises(ValueError, match="Unknown lifecycle event"):
            register("invalid_event", MagicMock())

    def test_register_all_lifecycle_events(self):
        """All 9 lifecycle events accept registration."""
        for event in LIFECYCLE_EVENTS:
            register(event, MagicMock())
        assert get_registered_count() == 9

    def test_lifecycle_events_count(self):
        """There are exactly 9 lifecycle events defined."""
        assert len(LIFECYCLE_EVENTS) == 9


# --- Unregister Tests ---

class TestUnregister:
    def test_unregister_existing_callback(self):
        """Unregistering a previously registered callback returns True."""
        cb = MagicMock()
        register("pre_cycle", cb)
        assert unregister("pre_cycle", cb) is True
        assert get_registered_count("pre_cycle") == 0

    def test_unregister_nonexistent_callback(self):
        """Unregistering an unregistered callback returns False."""
        assert unregister("pre_cycle", MagicMock()) is False

    def test_unregister_unknown_event(self):
        """Unregistering from an unknown event returns False."""
        assert unregister("bogus_event", MagicMock()) is False


# --- Fire Tests ---

class TestFire:
    def test_fire_calls_registered_callbacks(self):
        """fire() invokes all registered callbacks with event and kwargs."""
        cb = MagicMock(return_value="ok")
        register("pre_agent_start", cb)

        results = fire("pre_agent_start", task_id=42, cycle=1)

        cb.assert_called_once_with(event="pre_agent_start", task_id=42, cycle=1)
        assert results == ["ok"]

    def test_fire_multiple_callbacks_in_order(self):
        """fire() calls callbacks in registration order."""
        call_order = []
        cb1 = MagicMock(side_effect=lambda **kw: call_order.append(1))
        cb2 = MagicMock(side_effect=lambda **kw: call_order.append(2))
        register("post_agent_finish", cb1)
        register("post_agent_finish", cb2)

        fire("post_agent_finish")
        assert call_order == [1, 2]

    def test_fire_callback_exception_doesnt_crash(self):
        """A failing callback logs a warning but doesn't crash fire()."""
        bad_cb = MagicMock(side_effect=RuntimeError("boom"))
        good_cb = MagicMock(return_value="ok")
        register("pre_cycle", bad_cb)
        register("pre_cycle", good_cb)

        results = fire("pre_cycle")

        # Bad callback returns None, good callback returns "ok"
        assert results == [None, "ok"]
        good_cb.assert_called_once()

    def test_fire_no_callbacks(self):
        """fire() with no registered callbacks returns empty list."""
        results = fire("pre_cycle")
        assert results == []

    def test_fire_unknown_event_returns_empty(self):
        """fire() for an event with no callbacks returns empty list."""
        results = fire("on_checkpoint")
        assert results == []


# --- Fire Async Tests ---

class TestFireAsync:
    def test_fire_async_calls_callbacks(self):
        """fire_async() invokes registered callbacks."""
        cb = MagicMock(return_value="ok")
        register("pre_dispatch", cb)

        results = asyncio.get_event_loop().run_until_complete(
            fire_async("pre_dispatch", task_id=99)
        )

        cb.assert_called_once_with(event="pre_dispatch", task_id=99)
        assert results == ["ok"]


# --- Load Hooks Config Tests ---

class TestLoadHooksConfig:
    def test_load_valid_config(self, tmp_path):
        """Valid hooks.json is loaded and parsed correctly."""
        config = {
            "pre_agent_start": [
                {"command": "echo hello", "timeout": 10, "block_on_fail": True}
            ],
            "post_agent_finish": [
                {"command": "echo done"}
            ],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))

        loaded = load_hooks_config(config_path)

        assert "pre_agent_start" in loaded
        assert len(loaded["pre_agent_start"]) == 1
        assert loaded["pre_agent_start"][0]["command"] == "echo hello"
        assert loaded["pre_agent_start"][0]["timeout"] == 10
        assert loaded["pre_agent_start"][0]["block_on_fail"] is True

        assert "post_agent_finish" in loaded
        assert loaded["post_agent_finish"][0]["timeout"] == 30  # default
        assert loaded["post_agent_finish"][0]["block_on_fail"] is False  # default

    def test_load_missing_file_returns_empty(self, tmp_path):
        """Missing hooks.json returns empty dict."""
        result = load_hooks_config(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_invalid_json_returns_empty(self, tmp_path):
        """Malformed JSON returns empty dict."""
        bad_file = tmp_path / "hooks.json"
        bad_file.write_text("{not valid json!!}")
        result = load_hooks_config(bad_file)
        assert result == {}

    def test_load_skips_unknown_events(self, tmp_path):
        """Unknown event names in hooks.json are skipped."""
        config = {
            "unknown_event": [{"command": "echo bad"}],
            "pre_cycle": [{"command": "echo good"}],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))

        loaded = load_hooks_config(config_path)
        assert "unknown_event" not in loaded
        assert "pre_cycle" in loaded

    def test_load_skips_invalid_hook_entries(self, tmp_path):
        """Hook entries without 'command' key are skipped."""
        config = {
            "pre_cycle": [
                {"timeout": 10},  # missing command
                {"command": "echo valid"},
            ],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))

        loaded = load_hooks_config(config_path)
        assert len(loaded["pre_cycle"]) == 1
        assert loaded["pre_cycle"][0]["command"] == "echo valid"

    def test_load_non_dict_returns_empty(self, tmp_path):
        """hooks.json that's a list (not dict) returns empty."""
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps([1, 2, 3]))

        result = load_hooks_config(config_path)
        assert result == {}

    def test_get_external_hook_count(self, tmp_path):
        """get_external_hook_count reflects loaded hooks."""
        config = {
            "pre_cycle": [{"command": "echo a"}, {"command": "echo b"}],
            "post_cycle": [{"command": "echo c"}],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))

        load_hooks_config(config_path)
        assert get_external_hook_count("pre_cycle") == 2
        assert get_external_hook_count("post_cycle") == 1
        assert get_external_hook_count() == 3


# --- External Hook Execution Tests ---

class TestRunExternalHook:
    def test_run_successful_command(self, tmp_path):
        """A successful external hook returns exit code 0."""
        exit_code = run_external_hook(
            "echo test_output",
            context={"task_id": "42"},
            project_dir=str(tmp_path),
            timeout=10,
        )
        assert exit_code == 0

    def test_run_failing_command(self, tmp_path):
        """A failing external hook returns non-zero exit code."""
        exit_code = run_external_hook(
            "exit 1",
            context={},
            project_dir=str(tmp_path),
            timeout=10,
        )
        assert exit_code == 1

    def test_run_timeout_returns_negative_one(self, tmp_path):
        """A timed-out external hook returns -1."""
        exit_code = run_external_hook(
            "sleep 30",
            context={},
            project_dir=str(tmp_path),
            timeout=1,
        )
        assert exit_code == -1

    def test_run_nonexistent_command_returns_error(self, tmp_path):
        """A command that can't execute returns -2 or shell error."""
        # Using an absolutely nonexistent binary
        exit_code = run_external_hook(
            "/nonexistent/binary/abc123",
            context={},
            project_dir=str(tmp_path),
            timeout=5,
        )
        # Shell will return 127 for command not found, or -2 for OS error
        assert exit_code != 0


# --- Hook Environment Tests ---

class TestBuildHookEnv:
    def test_env_includes_context_vars(self):
        """Context dict is converted to EQUIPA_HOOK_* env vars."""
        env = _build_hook_env({"task_id": 42, "cycle": 3})
        assert env["EQUIPA_HOOK_TASK_ID"] == "42"
        assert env["EQUIPA_HOOK_CYCLE"] == "3"

    def test_env_inherits_process_env(self):
        """Hook env includes the parent process environment."""
        env = _build_hook_env({})
        assert "PATH" in env

    def test_env_skips_none_values(self):
        """None values in context are not added to env."""
        env = _build_hook_env({"task_id": 42, "missing": None})
        assert "EQUIPA_HOOK_TASK_ID" in env
        assert "EQUIPA_HOOK_MISSING" not in env

    def test_env_truncates_long_values(self):
        """Values longer than 500 chars are truncated."""
        env = _build_hook_env({"big": "x" * 1000})
        assert len(env["EQUIPA_HOOK_BIG"]) == 500


# --- Fire with External Hooks Tests ---

class TestFireWithExternalHooks:
    def test_fire_runs_external_hooks(self, tmp_path):
        """fire() also runs external command hooks."""
        config = {
            "pre_cycle": [
                {"command": "echo external_hook_ran", "timeout": 10}
            ],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))
        load_hooks_config(config_path)

        results = fire("pre_cycle", project_dir=str(tmp_path))
        assert len(results) == 1
        assert results[0]["exit_code"] == 0

    def test_fire_combines_python_and_external(self, tmp_path):
        """fire() calls both Python callbacks and external hooks."""
        # Register Python callback
        py_cb = MagicMock(return_value="python_result")
        register("pre_cycle", py_cb)

        # Load external hook
        config = {"pre_cycle": [{"command": "echo hi"}]}
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))
        load_hooks_config(config_path)

        results = fire("pre_cycle", project_dir=str(tmp_path))
        assert len(results) == 2
        assert results[0] == "python_result"
        assert results[1]["exit_code"] == 0

    def test_fire_blocking_hook_returns_blocked(self, tmp_path):
        """A failing block_on_fail hook returns blocked info."""
        config = {
            "pre_dispatch": [
                {"command": "exit 1", "timeout": 5, "block_on_fail": True}
            ],
        }
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))
        load_hooks_config(config_path)

        results = fire("pre_dispatch", project_dir=str(tmp_path))
        assert len(results) == 1
        assert results[0]["blocked"] is True
        assert results[0]["exit_code"] == 1


# --- Clear Operations Tests ---

class TestClearOperations:
    def test_clear_registry(self):
        """clear_registry() removes all registered callbacks."""
        register("pre_cycle", MagicMock())
        register("post_cycle", MagicMock())
        assert get_registered_count() == 2

        clear_registry()
        assert get_registered_count() == 0

    def test_clear_external_hooks(self, tmp_path):
        """clear_external_hooks() removes all loaded external hooks."""
        config = {"pre_cycle": [{"command": "echo x"}]}
        config_path = tmp_path / "hooks.json"
        config_path.write_text(json.dumps(config))
        load_hooks_config(config_path)
        assert get_external_hook_count() == 1

        clear_external_hooks()
        assert get_external_hook_count() == 0
