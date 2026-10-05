"""The per-test deadline (task 3175, IR71-05): tests/deadline_watchdog.py.

A quadratic regression put back into a timing test ran past 40 minutes; on
CI that is a job timeout naming no test. Every test now runs under a
deadline that fails it by name: a signal raises ``DeadlineExceeded`` into
the test, and a faulthandler stop ends the process if that is swallowed.

The child pytest runs below load the plugin with ``-p`` in a scratch
directory, so their tiny deadlines never touch this session.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests import deadline_watchdog
from tests.deadline_watchdog import (
    TEST_DEADLINE_SECONDS,
    TIMING_TEST_DEADLINE_SECONDS,
    DeadlineExceeded,
    deadline,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Catastrophic backtracking: minutes, or far longer, without a deadline.
CATASTROPHIC = re.compile(r"(a+)+$")
CATASTROPHIC_INPUT = "a" * 64 + "b"


def test_tests_run_on_the_main_thread_the_handler_runs_on():
    assert threading.current_thread() is threading.main_thread()


def test_a_watchdog_thread_could_not_have_cut_a_regex():
    """Why the soft stage is a kernel timer: the regex engine holds the GIL,
    so a thread waiting to send a signal never runs during the match."""
    ticks = []
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(0.01):
            ticks.append(time.monotonic())

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    time.sleep(0.05)
    with pytest.raises(DeadlineExceeded):
        with deadline(0.5, "gil probe"):
            started = time.monotonic()
            CATASTROPHIC.match(CATASTROPHIC_INPUT)
    ended = time.monotonic()
    stop.set()
    thread.join()
    assert ended - started >= 0.4
    assert not [tick for tick in ticks if started + 0.05 < tick < ended - 0.05]


def test_every_test_phase_runs_under_a_deadline(request):
    labels = [armed.label for armed in deadline_watchdog.active_deadlines()]
    assert f"{request.node.nodeid} (call)" in labels
    outer = deadline_watchdog.active_deadlines()[0]
    assert outer.seconds == TEST_DEADLINE_SECONDS


def test_a_timing_module_gets_the_timing_deadline(request):
    from tests import test_review_gate_polish_3170 as timing_module

    item = request.node
    assert deadline_watchdog._imports_timing_helper(timing_module)
    assert not deadline_watchdog._imports_timing_helper(sys.modules[__name__])
    assert deadline_watchdog.deadline_seconds(item) == TEST_DEADLINE_SECONDS
    assert TIMING_TEST_DEADLINE_SECONDS < TEST_DEADLINE_SECONDS


@pytest.mark.deadline(123)
def test_the_marker_sets_the_deadline(request):
    assert deadline_watchdog.deadline_seconds(request.node) == 123
    assert deadline_watchdog.active_deadlines()[0].seconds == 123


def test_a_catastrophic_regex_is_cut_at_its_deadline():
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="regex probe ran past its "
                                               "deadline of 0.5 s"):
        with deadline(0.5, "regex probe"):
            CATASTROPHIC.match(CATASTROPHIC_INPUT)
    assert time.monotonic() - started < 10


def test_a_python_loop_is_cut_at_its_deadline():
    with pytest.raises(DeadlineExceeded):
        with deadline(0.3, "loop"):
            while True:
                pass


def test_except_exception_does_not_swallow_the_deadline():
    """Code under test that fails closed on ``except Exception`` must not
    turn the deadline into a verdict and carry on."""
    caught = []
    with pytest.raises(DeadlineExceeded):
        with deadline(0.3, "fail-closed"):
            while True:
                try:
                    CATASTROPHIC.match(CATASTROPHIC_INPUT)
                except Exception as error:
                    caught.append(error)
    assert caught == []


def test_work_that_finishes_in_time_is_not_interrupted_later():
    with deadline(0.2, "quick"):
        pass
    busy_until = time.process_time() + 0.5
    while time.process_time() < busy_until:
        pass
    assert all(armed.label != "quick"
               for armed in deadline_watchdog.active_deadlines())


def test_sigalrm_is_left_to_the_agent_runner_watchdog():
    """equipa/agent_runner.py arms its stop-cleanup watchdog only while
    SIGALRM has its default disposition and no real-time timer runs."""
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getitimer(deadline_watchdog.DEADLINE_TIMER)[0] > 0


def test_the_outer_deadline_is_back_after_an_inner_one(request):
    before = deadline_watchdog.active_deadlines()
    with deadline(5, "inner"):
        assert len(deadline_watchdog.active_deadlines()) == len(before) + 1
    assert deadline_watchdog.active_deadlines() == before


@pytest.mark.parametrize("seconds", [0, -1, float("nan")])
def test_a_deadline_must_be_positive(seconds):
    with pytest.raises(ValueError):
        with deadline(seconds, "bad"):
            pass


SCRATCH_TESTS = '''
import re
import pytest

CATASTROPHIC = re.compile(r"(a+)+$")


@pytest.mark.deadline(1)
def test_hangs_in_a_regex():
    CATASTROPHIC.match("a" * 64 + "b")


@pytest.mark.deadline(1)
def test_hangs_in_a_loop():
    while True:
        pass


def test_runs_after_the_hung_ones():
    pass


@pytest.mark.deadline(1)
def test_swallows_the_deadline():
    if not {swallow}:
        return
    while True:
        try:
            CATASTROPHIC.match("a" * 64 + "b")
        except BaseException:
            pass
'''


def _run_scratch(tmp_path: Path, *options: str,
                 swallow: bool = False) -> subprocess.CompletedProcess:
    (tmp_path / "test_scratch.py").write_text(
        SCRATCH_TESTS.format(swallow=swallow), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTEST_", "EQUIPA_"))}
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.deadline_watchdog", *options, "test_scratch.py"],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        timeout=120, check=False)


def test_a_hung_test_fails_by_name_and_the_run_goes_on(tmp_path):
    completed = _run_scratch(tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "2 failed, 2 passed" in output, output
    for name in ("test_hangs_in_a_regex", "test_hangs_in_a_loop"):
        assert f"FAILED test_scratch.py::{name} - " in output, output
        assert (f"DeadlineExceeded: test_scratch.py::{name} (call) ran past "
                f"its deadline of 1 s of CPU time") in output, output


def test_a_swallowed_deadline_stops_the_process_and_names_the_test(tmp_path):
    started = time.monotonic()
    completed = _run_scratch(tmp_path, "-k", "swallows", swallow=True)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert re.search(r"^Timeout \(0:00:0[0-2][.\d]*\)!$", completed.stderr,
                     re.MULTILINE), completed.stderr
    assert re.search(r'File ".*test_scratch.py", line \d+ in '
                     r"test_swallows_the_deadline", completed.stderr), (
        completed.stderr)
    assert time.monotonic() - started < 60


def test_under_xdist_a_swallowed_deadline_is_a_named_crash(tmp_path):
    completed = _run_scratch(tmp_path, "-n", "1", "-k", "swallows or after",
                             swallow=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert ("crashed while running 'test_scratch.py::test_swallows_the_deadline'"
            in output), output
    assert "1 failed, 1 passed" in output, output
