"""Tests for equipa.hooks — lifecycle callback registry.

Regression tests to ensure plugin callbacks that raise exceptions
(e.g. AttributeError from QIAO engine) never crash the orchestrator.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from equipa.hooks import (
    clear_registry,
    fire,
    fire_async,
    get_registered_count,
    register,
    unregister,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure a clean callback registry for each test."""
    clear_registry()
    yield
    clear_registry()


def _good_callback(**kwargs):
    """A callback that succeeds and returns a value."""
    return {"status": "ok", "event": kwargs.get("event")}


def _crashing_callback(**kwargs):
    """Simulates the QIAO temperature AttributeError crash."""
    engine = object()  # object() has no .temperature attribute
    engine.temperature = 0.5  # type: ignore[attr-defined]  # noqa: B009


def _raising_type_error(**kwargs):
    """A callback that raises TypeError."""
    raise TypeError("unexpected keyword argument 'foo'")


def _raising_runtime_error(**kwargs):
    """A callback that raises RuntimeError."""
    raise RuntimeError("connection lost")


class TestFireCallbacks:
    """fire() must never propagate exceptions from callbacks."""

    def test_successful_callback(self):
        register("post_agent_finish", _good_callback)
        results = fire("post_agent_finish", task_id=42)
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert results[0]["event"] == "post_agent_finish"

    def test_crashing_callback_returns_none(self, caplog):
        """Regression: QIAO AttributeError must not crash orchestrator."""
        register("post_agent_finish", _crashing_callback)
        with caplog.at_level(logging.WARNING):
            results = fire("post_agent_finish", task_id=42)
        assert len(results) == 1
        assert results[0] is None
        assert "AttributeError" in caplog.text
        assert "_crashing_callback" in caplog.text

    def test_multiple_callbacks_one_crashes(self, caplog):
        """Good callbacks still run even when one crashes."""
        register("post_agent_finish", _good_callback)
        register("post_agent_finish", _crashing_callback)
        register("post_agent_finish", _good_callback)

        with caplog.at_level(logging.WARNING):
            results = fire("post_agent_finish", task_id=42)

        assert len(results) == 3
        assert results[0]["status"] == "ok"
        assert results[1] is None  # crashed callback
        assert results[2]["status"] == "ok"

    def test_type_error_isolated(self, caplog):
        register("pre_cycle", _raising_type_error)
        with caplog.at_level(logging.WARNING):
            results = fire("pre_cycle")
        assert results == [None]
        assert "TypeError" in caplog.text

    def test_runtime_error_isolated(self, caplog):
        register("on_stuck_detected", _raising_runtime_error)
        with caplog.at_level(logging.WARNING):
            results = fire("on_stuck_detected")
        assert results == [None]
        assert "RuntimeError" in caplog.text

    def test_log_includes_module_info(self, caplog):
        """Warning log must include callback module for traceability."""
        register("post_agent_finish", _crashing_callback)
        with caplog.at_level(logging.WARNING):
            fire("post_agent_finish", task_id=42)
        # Module should be this test file's module
        assert "test_hooks" in caplog.text

    def test_empty_event_returns_empty_list(self):
        results = fire("pre_agent_start")
        assert results == []


class TestFireAsync:
    """fire_async() must also isolate callback failures."""

    def test_async_crashing_callback(self, caplog):
        register("post_agent_finish", _crashing_callback)
        with caplog.at_level(logging.WARNING):
            results = asyncio.run(
                fire_async("post_agent_finish", task_id=42)
            )
        assert len(results) == 1
        assert results[0] is None
        assert "AttributeError" in caplog.text

    def test_async_mixed_callbacks(self):
        register("post_agent_finish", _good_callback)
        register("post_agent_finish", _crashing_callback)
        results = asyncio.run(
            fire_async("post_agent_finish", task_id=42)
        )
        assert len(results) == 2
        assert results[0]["status"] == "ok"
        assert results[1] is None


class TestRegistration:
    """register/unregister behavior."""

    def test_register_invalid_event_raises(self):
        with pytest.raises(ValueError, match="Unknown lifecycle event"):
            register("not_a_real_event", _good_callback)

    def test_unregister_returns_true(self):
        register("pre_cycle", _good_callback)
        assert unregister("pre_cycle", _good_callback) is True

    def test_unregister_missing_returns_false(self):
        assert unregister("pre_cycle", _good_callback) is False

    def test_unregister_unknown_event(self):
        assert unregister("fake_event", _good_callback) is False

    def test_count_tracks_registrations(self):
        assert get_registered_count("pre_cycle") == 0
        register("pre_cycle", _good_callback)
        register("pre_cycle", _crashing_callback)
        assert get_registered_count("pre_cycle") == 2
        assert get_registered_count() >= 2
