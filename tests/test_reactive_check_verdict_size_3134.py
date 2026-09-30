#!/usr/bin/env python3
"""Task 3134 IR-09 / IR-10: reactive checker verdict type and size cap.

IR-09: the worker coerced the checker's verdict with ``bool()``, so a
checker answering ``safe="yes"`` was reported safe. Anything but a real bool
must now fail closed (None, which the caller treats as a block).

IR-10: a command over the checker's own size cap (bash_security
MAX_COMMAND_BYTES) is blocked with a clear reason BEFORE it reaches the
worker, so a multi-megabyte command is never killed as a 5 s timeout with a
misleading reason. It is still a block.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from equipa.bash_security import MAX_COMMAND_BYTES, CheckID
from equipa.reactive_check import ReactiveBashChecker

# A checker whose verdict fields come from the command, and which records
# every command it is asked about next to itself.
SHAPED_CHECKER = '''import json, os
from dataclasses import dataclass
here = os.path.dirname(os.path.abspath(__file__))
@dataclass(frozen=True)
class Result:
    safe: object
    check_id: object = 0
    message: object = ""
def check(command):
    with open(os.path.join(here, "calls.log"), "a", encoding="utf-8") as fh:
        fh.write(str(len(command)) + "\\n")
    if command.startswith("{"):
        return Result(**json.loads(command))
    return Result(safe=True)
'''


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


@pytest.fixture
def shaped(tmp_path: Path):
    checker_file = tmp_path / "shaped_checker.py"
    checker_file.write_text(SHAPED_CHECKER, encoding="utf-8")
    checker = ReactiveBashChecker(checker_file, "check")
    yield checker, tmp_path / "calls.log"
    checker.close()


def _calls(log: Path) -> list[int]:
    if not log.exists():
        return []
    return [int(line) for line in log.read_text(encoding="utf-8").split()]


# --- IR-09 -------------------------------------------------------------------


@pytest.mark.parametrize("verdict", [
    {"safe": "yes"},
    {"safe": "false"},
    {"safe": 1},
    {"safe": 0},
    {"safe": None},
    {"safe": [True]},
    {"safe": True, "check_id": True},
    {"safe": True, "check_id": "8"},
    {"safe": True, "message": None},
], ids=lambda v: json.dumps(v))
def test_a_non_bool_verdict_fails_closed(shaped, verdict):
    checker, _log = shaped
    assert checker.check_blocking(json.dumps(verdict), 10.0) is None


def test_real_bools_still_pass_through(shaped):
    checker, _log = shaped
    safe = checker.check_blocking(json.dumps({"safe": True}), 10.0)
    flagged = checker.check_blocking(json.dumps(
        {"safe": False, "check_id": 8, "message": "nope"}), 10.0)
    assert safe.safe is True
    assert (flagged.safe, flagged.check_id, flagged.message) == (False, 8, "nope")


# --- IR-10 -------------------------------------------------------------------


def test_oversize_command_is_blocked_without_reaching_the_worker(shaped):
    checker, log = shaped
    command = "echo " + "a" * MAX_COMMAND_BYTES

    started = time.monotonic()
    verdict = checker.check_blocking(command, 5.0)

    assert time.monotonic() - started < 0.5
    assert verdict.safe is False
    assert verdict.check_id == CheckID.COMMAND_TOO_LONG
    assert f"{MAX_COMMAND_BYTES}-byte limit" in verdict.message
    assert checker.worker_pid() is None, "the worker was started"
    assert _calls(log) == []


def test_multibyte_command_over_the_byte_cap_is_blocked(shaped):
    checker, log = shaped
    command = "echo " + "é" * (MAX_COMMAND_BYTES // 2)  # chars < cap < bytes
    assert len(command) < MAX_COMMAND_BYTES < len(command.encode())

    verdict = checker.check_blocking(command, 5.0)

    assert verdict.safe is False and verdict.check_id == CheckID.COMMAND_TOO_LONG
    assert _calls(log) == []


def test_command_at_the_cap_still_reaches_the_worker(shaped):
    checker, log = shaped
    command = "e" * MAX_COMMAND_BYTES

    verdict = checker.check_blocking(command, 10.0)

    assert verdict.safe is True
    assert _calls(log) == [MAX_COMMAND_BYTES]


def test_four_mb_command_with_the_real_checker_is_blocked_immediately():
    checker = ReactiveBashChecker()
    try:
        started = time.monotonic()
        verdict = checker.check_blocking("cat <<EOF\n" + "x" * (4 << 20), 5.0)
        elapsed = time.monotonic() - started
    finally:
        checker.close()

    assert elapsed < 0.5, f"{elapsed:.2f}s"
    assert verdict.safe is False and verdict.check_id == CheckID.COMMAND_TOO_LONG


FAKE_CLI = '''import os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(here, "stream.jsonl"), encoding="utf-8"):
    sys.stdout.write(line)
    sys.stdout.flush()
    time.sleep(0.01)
'''


def test_streaming_run_blocks_an_oversize_command_with_its_real_reason(
        tmp_path, monkeypatch, shaped):
    """End to end: the agent is stopped for check 24, not for a timeout."""
    checker, log = shaped
    monkeypatch.setattr(agent_runner, "_REACTIVE_CHECKER", checker)
    project = tmp_path / "project"
    project.mkdir()
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    big = "printf '%s' " + "b" * (2 * MAX_COMMAND_BYTES)
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": big}}]}},
        {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 2, "total_cost_usd": 0.0},
    ]
    (tmp_path / "stream.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    result = asyncio.run(agent_runner._run_agent_streaming_impl(
        [sys.executable, str(fake)], role="developer", timeout=60,
        max_turns=10, output=[], project_dir=str(project)))

    assert result.get("early_terminated")
    reason = result["early_term_reason"]
    assert f"check {int(CheckID.COMMAND_TOO_LONG)}" in reason
    assert "byte limit" in reason and "did not finish" not in reason
    assert _calls(log) == []
