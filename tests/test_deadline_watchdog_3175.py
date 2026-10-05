"""The per-test deadline (task 3175, IR71-05): tests/deadline_watchdog.py.

A quadratic regression put back into a timing test ran past 40 minutes; on
CI that is a job timeout naming no test. Every test now runs under a
deadline that fails it by name: a signal raises ``DeadlineExceeded`` into
the test, and a timer signal (faulthandler writing every traceback first)
ends the process if that is swallowed.

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
    AFTER_A_HANG_DEADLINE_SECONDS,
    HARD_STOP_SIGNAL,
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


def test_after_a_hang_every_later_phase_is_cut(request, monkeypatch):
    monkeypatch.setattr(deadline_watchdog._deadlines, "first_hung_phase",
                        "tests/test_x.py::test_hung (call)")
    seconds, label = deadline_watchdog.phase_deadline(request.node, "call")
    assert seconds == AFTER_A_HANG_DEADLINE_SECONDS
    assert label == (f"{request.node.nodeid} (call; cut to 30 s because "
                     f"tests/test_x.py::test_hung (call) ran past its "
                     f"deadline first)")
    assert AFTER_A_HANG_DEADLINE_SECONDS < TIMING_TEST_DEADLINE_SECONDS


@pytest.mark.deadline(10)
def test_a_deadline_under_the_cut_is_kept(request, monkeypatch):
    monkeypatch.setattr(deadline_watchdog._deadlines, "first_hung_phase",
                        "tests/test_x.py::test_hung (call)")
    assert deadline_watchdog.phase_deadline(request.node, "setup") == (
        10, f"{request.node.nodeid} (setup)")


def test_a_deadline_a_test_expects_to_expire_is_not_a_hang():
    before = deadline_watchdog._deadlines.first_hung_phase
    with pytest.raises(DeadlineExceeded):
        with deadline(0.2, "expected"):
            while True:
                pass
    assert deadline_watchdog._deadlines.first_hung_phase == before


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


AFTER_A_HANG_SCRATCH_TESTS = '''
import pytest
from tests import deadline_watchdog
from tests.deadline_watchdog import DeadlineExceeded, deadline

deadline_watchdog.AFTER_A_HANG_DEADLINE_SECONDS = 1.0


def spin(seconds):
    busy_until = deadline_watchdog.time.process_time() + seconds
    while deadline_watchdog.time.process_time() < busy_until:
        pass


def test_0_expects_its_own_deadline_to_expire():
    with pytest.raises(DeadlineExceeded):
        with deadline(0.2, "expected"):
            spin(5)


def test_1_runs_past_the_cut_before_any_hang():
    spin(1.5)


@pytest.mark.deadline(2)
def test_2_hangs():
    spin(60)


def test_3_hangs_too():
    spin(60)


@pytest.mark.deadline(0.5)
def test_4_keeps_a_shorter_deadline():
    spin(0.1)
'''


def _scratch_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTEST_", "EQUIPA_"))}
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return environment


def _scratch_command(*options: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "tests.deadline_watchdog", *options, "test_scratch.py"]


def _run_scratch(tmp_path: Path, *options: str, swallow: bool = False,
                 source: str = SCRATCH_TESTS) -> subprocess.CompletedProcess:
    (tmp_path / "test_scratch.py").write_text(
        source.format(swallow=swallow) if source is SCRATCH_TESTS else source,
        encoding="utf-8")
    return subprocess.run(
        _scratch_command(*options), cwd=tmp_path, env=_scratch_environment(),
        capture_output=True, text=True, timeout=120, check=False)


def test_a_hung_test_fails_by_name_and_the_run_goes_on(tmp_path):
    completed = _run_scratch(tmp_path)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "2 failed, 2 passed" in output, output
    for name in ("test_hangs_in_a_regex", "test_hangs_in_a_loop"):
        assert f"FAILED test_scratch.py::{name} - " in output, output
        assert (f"DeadlineExceeded: test_scratch.py::{name} (call) ran past "
                f"its deadline of 1 s of CPU time") in output, output


def test_after_a_hang_the_next_hang_is_cut_short_and_named(tmp_path):
    """A regression that hangs every test of a module costs one full
    deadline, then the short one per test, and each fails by name."""
    started = time.monotonic()
    completed = _run_scratch(tmp_path, source=AFTER_A_HANG_SCRATCH_TESTS)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "2 failed, 3 passed" in output, output
    assert ("DeadlineExceeded: test_scratch.py::test_2_hangs (call) ran past "
            "its deadline of 2 s of CPU time") in output, output
    assert ("DeadlineExceeded: test_scratch.py::test_3_hangs_too (call; cut "
            "to 1 s because test_scratch.py::test_2_hangs (call) ran past its "
            "deadline first) ran past its deadline of 1 s of CPU time"
            ) in output, output
    assert time.monotonic() - started < 60


def test_a_swallowed_deadline_stops_the_process_and_names_the_test(tmp_path):
    """The deadline is 1 s, so the hard stop comes 6 s in on the wall clock
    (the 5 s minimum grace): the timer's signal ends the pytest process
    after faulthandler has written the test's frame."""
    started = time.monotonic()
    completed = _run_scratch(tmp_path, "-k", "swallows", swallow=True)
    assert completed.returncode == -HARD_STOP_SIGNAL, (
        completed.stdout + completed.stderr)
    assert "(most recent call first)" in completed.stderr, completed.stderr
    assert re.search(r'File ".*test_scratch.py", line \d+ in '
                     r"test_swallows_the_deadline", completed.stderr), (
        completed.stderr)
    assert time.monotonic() - started < 60


IGNORE_HARD_STOP_SIGNAL_PLUGIN = '''
import signal
from tests.deadline_watchdog import HARD_STOP_SIGNAL

signal.signal(HARD_STOP_SIGNAL, signal.SIG_IGN)
'''


def test_with_the_signal_taken_the_thread_stops_a_swallowed_deadline(
        tmp_path):
    """A process whose hard-stop signal is not at its default disposition
    (here: ignored, as a parent could leave it) falls back to faulthandler's
    watchdog thread, which stops the process as well."""
    (tmp_path / "ignore_hard_stop_signal.py").write_text(
        IGNORE_HARD_STOP_SIGNAL_PLUGIN, encoding="utf-8")
    started = time.monotonic()
    completed = _run_scratch(tmp_path, "-p", "ignore_hard_stop_signal", "-k",
                             "swallows", swallow=True)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    # Armed for what is left of the 1 s deadline plus its 5 s grace.
    assert re.search(r"^Timeout \(0:00:0[0-6][.\d]*\)!$", completed.stderr,
                     re.MULTILINE), completed.stderr
    assert re.search(r'File ".*test_scratch.py", line \d+ in '
                     r"test_swallows_the_deadline", completed.stderr), (
        completed.stderr)
    assert time.monotonic() - started < 60


def test_this_session_stops_a_hang_without_a_thread():
    assert deadline_watchdog.hard_stop_mechanism() == "signal timer"


NO_HIDDEN_THREAD_SCRATCH_TESTS = '''
import os
import threading

from tests import deadline_watchdog


def test_no_hidden_thread_while_a_deadline_is_armed():
    """faulthandler's watchdog thread is a C thread: the kernel counts it,
    the threading module does not."""
    assert deadline_watchdog.active_deadlines()
    assert len(os.listdir("/proc/self/task")) == threading.active_count()
'''


def test_an_armed_deadline_costs_the_process_no_task(tmp_path):
    """A thread per armed deadline is a task per xdist worker: at -n 16
    under systemd TasksMax=64 the run peaked at 69 tasks without one."""
    completed = _run_scratch(tmp_path,
                             source=NO_HIDDEN_THREAD_SCRATCH_TESTS)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "1 passed" in output, output


def test_under_xdist_a_swallowed_deadline_is_a_named_crash(tmp_path):
    completed = _run_scratch(tmp_path, "-n", "1", "-k", "swallows or after",
                             swallow=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert ("crashed while running 'test_scratch.py::test_swallows_the_deadline'"
            in output), output
    assert "1 failed, 1 passed" in output, output


@pytest.mark.parametrize("seconds, grace", [
    (0.3, 5.0), (1, 5.0), (2, 8.0), (30, 120.0),
    (TIMING_TEST_DEADLINE_SECONDS, 300.0), (TEST_DEADLINE_SECONDS, 300.0),
])
def test_the_hard_stop_leaves_a_contended_worker_room(seconds, grace):
    """Four times a short deadline (a worker on a fifth of a core still
    reaches its soft deadline first), at least 5 s, at most 300 s: the
    longest deadline is still stopped inside the 30-minute CI job."""
    assert deadline_watchdog.hard_stop_grace(seconds) == grace
    assert TEST_DEADLINE_SECONDS + deadline_watchdog.hard_stop_grace(
        TEST_DEADLINE_SECONDS) < 30 * 60


SHORT_DEADLINE_SCRATCH_TESTS = '''
import pytest


@pytest.mark.deadline(0.3)
def test_spins_past_a_short_deadline():
    while True:
        pass
'''


def test_a_worker_on_a_third_of_a_core_fails_by_name_not_by_crash(tmp_path):
    """The child is stopped two thirds of the time (SIGSTOP / SIGCONT), as a
    worker sharing its core with two busy processes. Its 0.3 s of CPU time
    then take about 0.9 s on the wall clock: past a hard stop at twice the
    deadline (0.6 s), which crashed workers under contention (task 3175),
    and well inside the 5.3 s it now has."""
    (tmp_path / "test_scratch.py").write_text(SHORT_DEADLINE_SCRATCH_TESTS,
                                              encoding="utf-8")
    output_path = tmp_path / "output.txt"
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            _scratch_command(), cwd=tmp_path, env=_scratch_environment(),
            stdout=output, stderr=subprocess.STDOUT)
        try:
            # At most about 120 s (4000 cycles of 30 ms) before the kill.
            for _ in range(4000):
                if process.poll() is not None:
                    break
                os.kill(process.pid, signal.SIGSTOP)
                time.sleep(0.02)
                os.kill(process.pid, signal.SIGCONT)
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    text = output_path.read_text(encoding="utf-8")
    assert process.returncode == 1, text
    assert "1 failed" in text, text
    assert ("DeadlineExceeded: test_scratch.py::test_spins_past_a_short_deadline "
            "(call) ran past its deadline of 0.3 s of CPU time") in text, text
