#!/usr/bin/env python3
"""Task 3127 / review finding P2A-01: the reactive Bash check runs in a process.

``re`` holds the GIL while it matches, so the pre-3127 thread-based check
still froze the event loop (and every parallel agent's monitoring) for as
long as a catastrophically backtracking regex ran; its deadline could not
fire. The check now runs in a persistent worker process that is killed and
recycled when it misses its deadline, and a miss is a block.

The fake checkers below are CPU-bound in C (a catastrophic ``re.match``),
not ``time.sleep``, which is the shape that exposed the freeze.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from equipa.reactive_check import ReactiveBashChecker

# Exponential backtracking: seconds of GIL-holding C code at this length,
# and minutes past it. The worker is killed long before it finishes.
CATASTROPHIC_SUBJECT_LENGTH = 26

CPU_CHECKER = f'''import re
from dataclasses import dataclass
@dataclass(frozen=True)
class Result:
    safe: bool
    check_id: int = 0
    message: str = ""
def check(command):
    if command.startswith("catastrophic"):
        re.match(r"^(a+)+$", "a" * {CATASTROPHIC_SUBJECT_LENGTH} + "!")
    if command.startswith("crash"):
        raise RuntimeError("checker crashed")
    if command.startswith("flag"):
        return Result(safe=False, check_id=99, message="fake flag")
    return Result(safe=True)
'''

DEADLINE = 0.5


def _catastrophic_in_process(command: str):
    """The same CPU-bound work in THIS process (the pre-3127 injection point)."""
    re.match(r"^(a+)+$", "a" * CATASTROPHIC_SUBJECT_LENGTH + "!")
    from equipa.bash_security import BashSecurityResult
    return BashSecurityResult(safe=True)


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


@pytest.fixture
def cpu_checker(tmp_path):
    checker_file = tmp_path / "cpu_checker.py"
    checker_file.write_text(CPU_CHECKER, encoding="utf-8")
    checker = ReactiveBashChecker(checker_file, "check")
    yield checker
    checker.close()


async def _with_heartbeat(coro):
    """Run ``coro`` while measuring the longest event-loop stall."""
    done = asyncio.Event()
    gaps: list[float] = []

    async def beat() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def run():
        try:
            return await coro
        finally:
            done.set()

    result, _ = await asyncio.gather(run(), beat())
    return result, max(gaps)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_catastrophic_regex_checker_does_not_freeze_the_event_loop(
        cpu_checker, monkeypatch):
    monkeypatch.setattr(agent_runner, "_REACTIVE_CHECKER", cpu_checker)
    # Pre-3127 the thread-based check called this module attribute; setting
    # it keeps the test a behavioural regression test on that code too.
    monkeypatch.setattr(agent_runner, "check_bash_command",
                        _catastrophic_in_process, raising=False)
    monkeypatch.setattr(agent_runner, "_SLOW_CHECK_SECONDS", DEADLINE)
    cpu_checker.check_blocking("warm-up", 10.0)  # start-up is not measured

    started = time.monotonic()
    result, longest_stall = asyncio.run(_with_heartbeat(
        agent_runner._reactive_bash_check("catastrophic aaaa")))
    elapsed = time.monotonic() - started

    assert longest_stall < DEADLINE, f"event loop frozen for {longest_stall:.2f}s"
    assert result is None  # a missed deadline is a block
    assert elapsed < DEADLINE + 2.0


def test_timed_out_worker_is_killed_and_the_next_check_gets_a_fresh_one(
        cpu_checker):
    assert cpu_checker.check_blocking("ls", 10.0).safe is True
    first_pid = cpu_checker.worker_pid()
    assert first_pid is not None

    assert cpu_checker.check_blocking("catastrophic", DEADLINE) is None

    assert cpu_checker.worker_pid() is None
    assert not _pid_alive(first_pid)
    verdict = cpu_checker.check_blocking("flag this", 10.0)
    assert (verdict.safe, verdict.check_id, verdict.message) == (
        False, 99, "fake flag")
    assert cpu_checker.worker_pid() not in (None, first_pid)


def test_checker_crash_fails_closed_then_recovers(cpu_checker):
    assert cpu_checker.check_blocking("crash now", 10.0) is None
    assert cpu_checker.check_blocking("ls", 10.0).safe is True


def test_worker_that_cannot_load_its_checker_fails_closed(tmp_path):
    broken = tmp_path / "broken_checker.py"
    broken.write_text("raise ImportError('fake broken checker')\n",
                      encoding="utf-8")
    checker = ReactiveBashChecker(broken, "check")
    try:
        assert checker.check_blocking("ls", 5.0) is None
        assert checker.worker_pid() is None
    finally:
        checker.close()


def test_idle_worker_death_is_replaced_without_blocking_a_check(cpu_checker):
    assert cpu_checker.check_blocking("ls", 10.0).safe is True
    pid = cpu_checker.worker_pid()
    os.kill(pid, 9)
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        cpu_checker._proc.poll()  # reap the zombie
        time.sleep(0.01)
    assert cpu_checker.check_blocking("ls", 10.0).safe is True


def test_real_checker_runs_in_the_worker():
    checker = ReactiveBashChecker()
    try:
        assert checker.check_blocking("ls -la", 10.0).safe is True
        command = "ls -la <(echo hi)"
        flagged = checker.check_blocking(command, 10.0)
        assert flagged.safe is False
        # Same verdict as the in-process checker (whatever its check ids are
        # at the time: bash_security.py is maintained separately).
        from equipa.bash_security import check_bash_command
        expected = check_bash_command(command)
        assert (flagged.safe, flagged.check_id, flagged.message) == (
            expected.safe, expected.check_id, expected.message)
        assert checker.worker_pid() != os.getpid()
    finally:
        checker.close()


def test_streaming_agent_is_blocked_when_the_check_misses_its_deadline(
        cpu_checker, monkeypatch, tmp_path):
    monkeypatch.setattr(agent_runner, "_REACTIVE_CHECKER", cpu_checker)
    monkeypatch.setattr(agent_runner, "check_bash_command",
                        _catastrophic_in_process, raising=False)
    monkeypatch.setattr(agent_runner, "_SLOW_CHECK_SECONDS", DEADLINE)
    fake = tmp_path / "fake_claude.py"
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "catastrophic aaaa"}}]}},
        {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 2, "total_cost_usd": 0.0},
    ]
    stream = tmp_path / "stream.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in events),
                      encoding="utf-8")
    fake.write_text(
        "import sys, time\n"
        f"for line in open({str(stream)!r}, encoding='utf-8'):\n"
        "    sys.stdout.write(line); sys.stdout.flush(); time.sleep(0.01)\n",
        encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    cpu_checker.check_blocking("warm-up", 10.0)
    output: list[str] = []

    result, longest_stall = asyncio.run(_with_heartbeat(
        agent_runner._run_agent_streaming_impl(
            [sys.executable, str(fake)], role="developer", timeout=60,
            max_turns=40, output=output, project_dir=str(project))))

    assert longest_stall < DEADLINE, f"event loop frozen for {longest_stall:.2f}s"
    assert result.get("early_terminated")
    assert "did not finish" in result["early_term_reason"]
