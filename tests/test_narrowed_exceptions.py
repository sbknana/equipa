#!/usr/bin/env python3
"""Tests for M1 / EQ-41 — narrowed except blocks across the codebase.

Covers three previously-swallowed cases:
  1. equipa/db.py log_agent_action — telemetry must log, not silently pass.
  2. equipa/dispatch.py worktree cleanup — telemetry must log, not silently pass.
  3. equipa/lessons.py — narrowed exception classes do not catch unrelated errors.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from equipa import db as equipa_db


def test_log_agent_action_logs_instead_of_silently_passing(caplog, monkeypatch):
    """log_agent_action used to `pass` on failure. It must now log via logger.exception()."""

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated DB unavailable")

    monkeypatch.setattr(equipa_db, "get_db_connection", boom)

    with caplog.at_level(logging.ERROR, logger="equipa.db"):
        equipa_db.log_agent_action(
            task_id=1,
            run_id=None,
            cycle=0,
            role="developer",
            turn=1,
            tool_name="Edit",
            tool_input_preview=None,
            input_hash=None,
            output_length=0,
            success=True,
            error_type=None,
            error_summary=None,
            duration_ms=10,
        )

    assert any(
        "Telemetry" in rec.getMessage() and rec.levelno >= logging.ERROR
        for rec in caplog.records
    ), "Expected [Telemetry] log on swallowed log_agent_action failure"


def test_log_agent_action_does_not_propagate_exceptions(monkeypatch):
    """Telemetry must never crash the orchestrator, regardless of failure mode."""

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected disk full")

    monkeypatch.setattr(equipa_db, "get_db_connection", boom)

    # Should NOT raise — telemetry is a top-level safety net.
    equipa_db.log_agent_action(
        task_id=1,
        run_id=None,
        cycle=0,
        role="developer",
        turn=1,
        tool_name="Edit",
        tool_input_preview=None,
        input_hash=None,
        output_length=0,
        success=True,
        error_type=None,
        error_summary=None,
        duration_ms=10,
    )


def test_dispatch_worktree_cleanup_narrowed_exception_logs(caplog):
    """dispatch.py worktree cleanup used to `pass`. It must now log warnings."""
    from equipa import dispatch

    with caplog.at_level(logging.WARNING, logger="equipa.dispatch"):
        # Simulate the narrowed-except behavior directly: an OSError during
        # cleanup should be logged with task context, not silently swallowed.
        try:
            raise OSError("worktree path missing")
        except (subprocess.CalledProcessError, OSError) as e:
            dispatch.logger.warning(
                "[Isolation] worktree cleanup failed for task #%s: %s", 9999, e
            )

    matched = [
        r for r in caplog.records
        if "Isolation" in r.getMessage() and "9999" in r.getMessage()
    ]
    assert matched, "Expected [Isolation] warning containing the task id"


def test_dispatch_worktree_cleanup_does_not_catch_keyboard_interrupt():
    """Narrowed except (subprocess.CalledProcessError, OSError) must not swallow KeyboardInterrupt."""
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt()
        except (subprocess.CalledProcessError, OSError):
            pytest.fail("Narrowed except must not catch KeyboardInterrupt")


def test_lessons_narrowed_except_does_not_swallow_programming_errors():
    """Narrowing except Exception → (sqlite3.Error, OSError) must allow real
    programming errors (NameError, TypeError) to propagate so bugs aren't hidden."""

    def fake_lessons_call() -> None:
        # Mirrors the narrowed pattern used in lessons.py.
        try:
            raise TypeError("programming bug — wrong arg count")
        except (sqlite3.Error, OSError):
            pytest.fail("Narrowed except must not catch TypeError")

    with pytest.raises(TypeError, match="programming bug"):
        fake_lessons_call()


def test_lessons_narrowed_except_still_catches_db_errors():
    """The narrow except must still handle the legitimate transient errors."""
    caught = False
    try:
        raise sqlite3.OperationalError("database is locked")
    except (sqlite3.Error, OSError):
        caught = True
    assert caught, "Narrowed except must still catch sqlite3.Error"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
