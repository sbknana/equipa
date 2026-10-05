"""Per-test deadlines: a hung test fails by name (task 3175, IR71-05).

A quadratic regression put back into a timing test ran for over 40 minutes
in its quarter-size measurement. On CI that ends as a job timeout naming no
test. pytest-timeout is not installed (and no package is added for this),
so this pytest plugin bounds every test phase (setup, call, teardown) with
the standard library, in two stages:

1. **Soft, on CPU time.** When a phase has used its deadline in CPU
   seconds, a kernel interval timer (``ITIMER_PROF``) sends ``SIGPROF`` and
   its handler raises ``DeadlineExceeded`` into the test: a named failure
   carrying the traceback of where the test was. The ``re`` engine holds
   the GIL while it backtracks, so a watchdog *thread* never gets to run
   (tried first: its signal was never sent); a kernel signal needs no GIL,
   and the engine runs the handler while it backtracks, so a catastrophic
   regex is cut as well as a Python loop. ``DeadlineExceeded`` is a
   ``BaseException``: code under test that fails closed on ``except
   Exception`` must not turn it into a verdict. ``SIGALRM`` is left alone:
   the agent runner's stop-cleanup watchdog arms only while nothing else
   uses it.
2. **Hard, on wall time.** A test that swallows that exception, sits in C
   code that never checks for signals, or waits without using CPU is
   stopped by ``faulthandler.dump_traceback_later(exit=True)`` once the
   deadline plus a grace has passed on the wall clock: every thread's
   traceback (the test's frame included) goes to the session's stderr and
   the process exits. Under xdist the controller reports "worker crashed
   while running <test>".

A test gets ``TEST_DEADLINE_SECONDS``, a test of a module that imports
``tests/host_timing.py`` gets ``TIMING_TEST_DEADLINE_SECONDS``, and
``@pytest.mark.deadline(seconds)`` sets any other. Deadlines nest: the
earliest one active is enforced. ``tests/conftest.py`` registers this
plugin; ``-p tests.deadline_watchdog`` loads it into any other run.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import faulthandler
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

import pytest

DEADLINE_TIMER = signal.ITIMER_PROF
DEADLINE_SIGNAL = signal.SIGPROF
# The slowest test took 92 s of wall time with every core busy (16
# workers); a CI runner is slower, and the CI job stops at 30 minutes.
TEST_DEADLINE_SECONDS = 600.0
# The slowest timing test took 19 s here. A budget-only corpus cap of 60 s
# at the highest host factor (4.0) is 240 s, still inside it.
TIMING_TEST_DEADLINE_SECONDS = 300.0
# The hard stop comes this long after the deadline on the wall clock (or as
# long again as a shorter deadline): a phase that waits uses no CPU time.
HARD_DEADLINE_GRACE_SECONDS = 300.0
# The handler ran while a deadline was being armed or disarmed: it looks
# again this many CPU seconds later.
RETRY_SECONDS = 0.01
TIMING_HELPER_MODULE = "tests.host_timing"


class DeadlineExceeded(BaseException):
    """A test (or a ``deadline`` block) ran past its deadline."""


@dataclass(eq=False)
class Deadline:
    """One armed deadline: ``label`` must finish within ``seconds`` of CPU
    time (soft) and ``seconds`` plus the grace of wall time (hard)."""

    seconds: float
    label: str
    cpu_started: float = field(default_factory=time.process_time)
    wall_started: float = field(default_factory=time.monotonic)
    raised: bool = False

    @property
    def soft_at(self) -> float:
        """On the ``time.process_time()`` clock."""
        return self.cpu_started + self.seconds

    @property
    def hard_at(self) -> float:
        """On the ``time.monotonic()`` clock."""
        return (self.wall_started + self.seconds
                + min(HARD_DEADLINE_GRACE_SECONDS, self.seconds))

    def message(self) -> str:
        return (f"{self.label} ran past its deadline of {self.seconds:g} s "
                f"of CPU time (tests/deadline_watchdog.py): a hang, or a "
                f"superlinear regression?")


class _Deadlines:
    """The deadlines armed in this process, innermost last. Only the main
    thread touches them; the signal handler runs on it too, between any
    two bytecodes, so it backs off while ``push``/``pop`` are at work."""

    def __init__(self, hard_stop_fd: int | None) -> None:
        self.armed: list[Deadline] = []
        self.hard_stop_fd = hard_stop_fd
        self.bookkeeping = False

    def push(self, deadline: Deadline) -> None:
        self.bookkeeping = True
        try:
            self.armed.append(deadline)
            self.arm_timers()
        finally:
            self.bookkeeping = False

    def pop(self, deadline: Deadline) -> None:
        self.bookkeeping = True
        try:
            if deadline in self.armed:
                self.armed.remove(deadline)
            self.arm_timers()
        finally:
            self.bookkeeping = False

    def arm_timers(self) -> None:
        pending = [deadline.soft_at for deadline in self.armed
                   if not deadline.raised]
        if pending:
            signal.setitimer(DEADLINE_TIMER, max(
                min(pending) - time.process_time(), 0.001))
        else:
            signal.setitimer(DEADLINE_TIMER, 0)
        if self.hard_stop_fd is None:
            return
        if not self.armed:
            faulthandler.cancel_dump_traceback_later()
            return
        hard_at = min(deadline.hard_at for deadline in self.armed)
        faulthandler.dump_traceback_later(
            max(hard_at - time.monotonic(), 0.001), exit=True,
            file=self.hard_stop_fd)

    def on_signal(self, signum: int, frame: object) -> None:
        if self.bookkeeping:
            signal.setitimer(DEADLINE_TIMER, RETRY_SECONDS)
            return
        now = time.process_time()
        expired = [deadline for deadline in self.armed
                   if not deadline.raised and deadline.soft_at <= now]
        for deadline in expired:
            deadline.raised = True
        # Re-arm for the next deadline, or again for this one if the kernel
        # counted its CPU time a little ahead of process_time().
        self.arm_timers()
        if expired:
            raise DeadlineExceeded(expired[0].message())


_deadlines: _Deadlines | None = None


def _on_deadline_signal(signum: int, frame: object) -> None:
    if _deadlines is not None:
        _deadlines.on_signal(signum, frame)


def install(hard_stop_fd: int | None = None) -> None:
    """Install, once per process and from the main thread (which runs the
    tests), the handler that raises ``DeadlineExceeded``."""
    global _deadlines
    if _deadlines is not None:
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("the deadline handler is installed from the main "
                           "thread, which runs the tests")
    _deadlines = _Deadlines(hard_stop_fd)
    signal.signal(DEADLINE_SIGNAL, _on_deadline_signal)


def active_deadlines() -> list[Deadline]:
    return [] if _deadlines is None else list(_deadlines.armed)


@contextlib.contextmanager
def deadline(seconds: float, label: str) -> Iterator[Deadline]:
    """Raise ``DeadlineExceeded`` into this block once it has used
    ``seconds`` of CPU time (and stop the process if that is swallowed, or
    if the block waits past ``seconds`` plus the grace)."""
    if not seconds > 0:
        raise ValueError(f"a deadline must be > 0 seconds, not {seconds!r}")
    if _deadlines is None:
        raise RuntimeError("deadline_watchdog.install() was not called")
    armed = Deadline(seconds, label)
    _deadlines.push(armed)
    try:
        yield armed
    finally:
        _deadlines.pop(armed)


_TIMING_MODULES: dict[str, bool] = {}


def _imports_timing_helper(module: object) -> bool:
    name = getattr(module, "__name__", "")
    if name not in _TIMING_MODULES:
        _TIMING_MODULES[name] = any(
            getattr(value, "__module__", None) == TIMING_HELPER_MODULE
            or getattr(value, "__name__", None) == TIMING_HELPER_MODULE
            for value in vars(module).values())
    return _TIMING_MODULES[name]


def deadline_seconds(item: pytest.Item) -> float:
    marker = item.get_closest_marker("deadline")
    if marker is not None:
        return float(marker.args[0])
    module = getattr(item, "module", None)
    if module is not None and _imports_timing_helper(module):
        return TIMING_TEST_DEADLINE_SECONDS
    return TEST_DEADLINE_SECONDS


# --- Plugin hooks -------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "deadline(seconds): fail the test (DeadlineExceeded) when a phase "
        "uses more CPU time; tests/deadline_watchdog.py")


def pytest_sessionstart(session: pytest.Session) -> None:
    # Output capture is suspended between conftest loading and the first
    # test, so descriptor 2 is the session's real stderr here.
    install(hard_stop_fd=os.dup(2))


def _phase(item: pytest.Item, phase: str) -> contextlib.AbstractContextManager:
    if _deadlines is None:
        return contextlib.nullcontext()
    return deadline(deadline_seconds(item), f"{item.nodeid} ({phase})")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    with _phase(item, "setup"):
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    with _phase(item, "call"):
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    with _phase(item, "teardown"):
        yield
