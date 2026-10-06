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
   stopped once the deadline plus a grace has passed on the wall clock: a
   POSIX timer on ``CLOCK_MONOTONIC`` (``timer_create`` through ``ctypes``)
   sends ``HARD_STOP_SIGNAL``, ``faulthandler`` writes every thread's
   traceback (the test's frame included) to the session's stderr from C,
   and the signal's default action then ends the process. Under xdist the
   controller reports "worker crashed while running <test>". The timer
   runs no thread. ``faulthandler.dump_traceback_later`` starts one each
   time it is armed: one more task per xdist worker, and at ``-n 16``
   under a task cap (systemd ``TasksMax=64``) the run had already peaked
   at 69 tasks without it. Where ``timer_create`` is missing (not 64-bit
   Linux), that thread is the hard stop.

A test gets ``TEST_DEADLINE_SECONDS``, a test of a module that imports
``tests/host_timing.py`` gets ``TIMING_TEST_DEADLINE_SECONDS``, and
``@pytest.mark.deadline(seconds)`` sets any other. Once a test phase has
run past its deadline, every later phase of that process gets at most
``AFTER_A_HANG_DEADLINE_SECONDS``: a regression that hangs every test of a
module costs one full deadline, then that much per test, and the run still
ends in named failures. A hard stop ends the worker with that record, so
each process also keeps the phase it is running in a file of a directory
the whole session shares (``SESSION_DIRECTORY_VARIABLE``); a phase left
there by a process that no longer runs was stopped mid-run, and every
later phase of the session is cut the same way (IR75-02, task 3178: under
heavy load the replacement worker gave the next hung test a full deadline
again). The run's summary names such phases. Deadlines nest: the earliest
one active is enforced. ``tests/conftest.py`` registers this
plugin; ``-p tests.deadline_watchdog`` loads it into any other run.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import ctypes
import faulthandler
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import pytest

DEADLINE_TIMER = signal.ITIMER_PROF
DEADLINE_SIGNAL = signal.SIGPROF
# The hard stop's signal: a real-time one nothing else here uses (glibc
# keeps the lowest ones for its threads), whose default action ends the
# process. None where the platform has no real-time signals.
HARD_STOP_SIGNAL: int | None = (
    signal.SIGRTMAX - 3 if hasattr(signal, "SIGRTMAX") else None)
# The slowest test took 92 s of wall time with every core busy (16
# workers); a CI runner is slower, and the CI job stops at 30 minutes.
TEST_DEADLINE_SECONDS = 600.0
# The slowest timing test (test_nothing_an_older_tree_blocked_merges) took
# 55 s with every core busy, 220 s at the highest host factor (4.0); a
# budget-only corpus cap of 60 s is 240 s there. Both are inside it.
TIMING_TEST_DEADLINE_SECONDS = 300.0
# Once a phase of this process has run past its deadline, the run has failed
# and named that test, and every later phase gets at most this much. A
# reintroduced quadratic regex hangs many tests of one module, and
# ``--dist loadfile`` runs them one after another in one worker: I3164-01's
# regex put back hung 13 tests of tests/test_review_gate_linear_3167.py, 65
# minutes at a full deadline each (a CI job timeout), 11 with this cut.
AFTER_A_HANG_DEADLINE_SECONDS = 30.0
# The hard stop comes this long after the deadline on the wall clock: a
# phase that waits uses no CPU time. A busy host gives a worker a fraction
# of a core, so a CPU-time deadline lasts longer on the wall clock: a
# shorter deadline gets HARD_STOP_SLOWDOWN times itself as grace, never
# under HARD_STOP_MIN_GRACE_SECONDS. A test that still uses CPU then
# reaches its soft deadline (a named failure, the worker goes on) before
# the hard stop ends the worker, if it gets seconds / (seconds + grace) of
# a core: a fifth up to 75 s, half at 300 s, two thirds at 600 s. A 0.5 s
# deadline was hard-stopped 1 s in with 16 CPU burners beside 16 workers
# on 16 cores, and at a load of 90 on those cores a worker got a tenth of
# one. With less, the hard stop still names the test (its frame in the
# dump, xdist's "crashed while running"): a quadratic regex put back under
# a 300 s deadline, beside 14 other workers at a load near 60 on 16 cores,
# ended that way. The 600 s deadline is still stopped 15 minutes in,
# inside the 30-minute CI job.
HARD_DEADLINE_GRACE_SECONDS = 300.0
HARD_STOP_SLOWDOWN = 4.0
HARD_STOP_MIN_GRACE_SECONDS = 60.0
# The handler ran while a deadline was being armed or disarmed: it looks
# again this many CPU seconds later.
RETRY_SECONDS = 0.01
TIMING_HELPER_MODULE = "tests.host_timing"
# "<controller pid>:<directory>" of the session's shared phase records, set
# by the process that starts the xdist workers (they inherit it).
SESSION_DIRECTORY_VARIABLE = "EQUIPA_DEADLINE_SESSION"
PHASE_FILE_PREFIX = "phase-"


class DeadlineExceeded(BaseException):
    """A test (or a ``deadline`` block) ran past its deadline."""


def hard_stop_grace(seconds: float) -> float:
    """How long after a deadline of ``seconds`` of CPU time the hard stop
    comes on the wall clock."""
    return min(HARD_DEADLINE_GRACE_SECONDS,
               max(HARD_STOP_SLOWDOWN * seconds, HARD_STOP_MIN_GRACE_SECONDS))


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
        return self.wall_started + self.seconds + hard_stop_grace(self.seconds)

    def message(self) -> str:
        return (f"{self.label} ran past its deadline of {self.seconds:g} s "
                f"of CPU time (tests/deadline_watchdog.py): a hang, or a "
                f"superlinear regression?")


# --- The hard stop ------------------------------------------------------------

_CLOCK_MONOTONIC = 1
_SIGEV_SIGNAL = 0
_SIGEVENT_BYTES = 64


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _Itimerspec(ctypes.Structure):
    _fields_ = [("it_interval", _Timespec), ("it_value", _Timespec)]


class _Sigevent(ctypes.Structure):
    """``struct sigevent`` on 64-bit Linux: the ``sigval`` union, the signal,
    the notification kind, then a union padding the struct to 64 bytes."""

    _fields_ = [("sigev_value", ctypes.c_void_p),
                ("sigev_signo", ctypes.c_int),
                ("sigev_notify", ctypes.c_int),
                ("_padding", ctypes.c_int * 12)]


def _timer_functions() -> tuple[Callable[..., int], Callable[..., int]]:
    """``timer_create`` and ``timer_settime``: in the C library since glibc
    2.34, in librt before. Raises ``OSError`` where they cannot be used."""
    if not (sys.platform.startswith("linux")
            and ctypes.sizeof(ctypes.c_void_p) == 8
            and ctypes.sizeof(ctypes.c_long) == 8
            and ctypes.sizeof(_Sigevent) == _SIGEVENT_BYTES):
        raise OSError("POSIX timers are only used on 64-bit Linux here")
    for library_name in (None, "librt.so.1"):
        try:
            library = ctypes.CDLL(library_name, use_errno=True)
            create, settime = library.timer_create, library.timer_settime
        except (OSError, AttributeError):
            continue
        create.argtypes = [ctypes.c_int, ctypes.POINTER(_Sigevent),
                           ctypes.POINTER(ctypes.c_void_p)]
        create.restype = ctypes.c_int
        settime.argtypes = [ctypes.c_void_p, ctypes.c_int,
                            ctypes.POINTER(_Itimerspec), ctypes.c_void_p]
        settime.restype = ctypes.c_int
        return create, settime
    raise OSError("timer_create was not found in the C library or librt")


def _raise_errno(call: str) -> None:
    error = ctypes.get_errno()
    raise OSError(error, f"{call}: {os.strerror(error)}")


class _SignalTimerHardStop:
    """A one-shot POSIX timer on the monotonic clock that sends
    ``HARD_STOP_SIGNAL`` to this process, on which ``faulthandler`` dumps
    every thread's traceback and then lets the default action end the
    process (``chain=True`` restores the default disposition and raises
    the signal again). No thread is started."""

    mechanism = "signal timer"

    def __init__(self, file_descriptor: int) -> None:
        if HARD_STOP_SIGNAL is None:
            raise OSError("no real-time signal for the hard stop")
        if signal.getsignal(HARD_STOP_SIGNAL) != signal.SIG_DFL:
            raise OSError(f"signal {HARD_STOP_SIGNAL} is already in use")
        create, self._settime = _timer_functions()
        event = _Sigevent(sigev_signo=HARD_STOP_SIGNAL,
                          sigev_notify=_SIGEV_SIGNAL)
        self._timer_id = ctypes.c_void_p()
        if create(_CLOCK_MONOTONIC, ctypes.byref(event),
                  ctypes.byref(self._timer_id)) != 0:
            _raise_errno("timer_create")
        # A forked child inherits neither the timer nor its id's meaning.
        self._owner_pid = os.getpid()
        faulthandler.register(HARD_STOP_SIGNAL, file=file_descriptor,
                              all_threads=True, chain=True)

    def _set(self, seconds: float) -> None:
        if os.getpid() != self._owner_pid:
            return
        whole = int(seconds)
        expiry = _Itimerspec(it_value=_Timespec(
            whole, min(int((seconds - whole) * 1e9), 999_999_999)))
        if self._settime(self._timer_id, 0, ctypes.byref(expiry), None) != 0:
            _raise_errno("timer_settime")

    def arm(self, seconds: float) -> None:
        self._set(seconds)

    def cancel(self) -> None:
        # An all-zero expiry disarms the timer.
        self._set(0.0)


class _ThreadHardStop:
    """``faulthandler.dump_traceback_later(exit=True)``: a watchdog thread,
    started again each time the stop is armed."""

    mechanism = "faulthandler thread"

    def __init__(self, file_descriptor: int) -> None:
        self._file_descriptor = file_descriptor

    def arm(self, seconds: float) -> None:
        faulthandler.dump_traceback_later(seconds, exit=True,
                                          file=self._file_descriptor)

    def cancel(self) -> None:
        faulthandler.cancel_dump_traceback_later()


def _hard_stop(file_descriptor: int
               ) -> _SignalTimerHardStop | _ThreadHardStop:
    try:
        return _SignalTimerHardStop(file_descriptor)
    except OSError:
        return _ThreadHardStop(file_descriptor)


# --- Deadlines ----------------------------------------------------------------


class _Deadlines:
    """The deadlines armed in this process, innermost last. Only the main
    thread touches them; the signal handler runs on it too, between any
    two bytecodes, so it backs off while ``push``/``pop`` are at work."""

    def __init__(self, hard_stop_fd: int | None) -> None:
        self.armed: list[Deadline] = []
        self.hard_stop = (None if hard_stop_fd is None
                          else _hard_stop(hard_stop_fd))
        self.bookkeeping = False
        # The first test phase of this process that ran past its own
        # deadline (a ``deadline`` block a test sets and expects to expire
        # does not count).
        self.first_hung_phase: str | None = None

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
        if self.hard_stop is None:
            return
        if not self.armed:
            self.hard_stop.cancel()
            return
        hard_at = min(deadline.hard_at for deadline in self.armed)
        self.hard_stop.arm(max(hard_at - time.monotonic(), 0.001))

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


# --- The phases of the session (IR75-02) ------------------------------------------


def _process_runs(pid: int) -> bool:
    """Whether ``pid`` still runs. A worker the hard stop ended stays a
    zombie until xdist's controller reaps it, which can be after the
    replacement worker starts; ``kill(pid, 0)`` succeeds on a zombie, so
    its state is read from /proc where there is one."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat_file:
            state = stat_file.read().rsplit(b")", 1)[-1].split()[:1]
        return state not in ([b"Z"], [b"X"])
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionPhases:
    """The phase each process of a pytest session is running, one file per
    process (``phase-<pid>``) in a directory they share: written before a
    phase's deadline is armed, emptied when the phase ends. A hard stop
    ends a worker mid-phase, so its file keeps that phase."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        # A forked child inherits this object; only the process that made
        # the record writes or removes it.
        self._owner_pid = os.getpid()
        self._path = os.path.join(directory, f"{PHASE_FILE_PREFIX}{self._owner_pid}")
        self._fd: int | None = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                           | getattr(os, "O_CLOEXEC", 0), 0o600)

    def _writable(self) -> bool:
        return self._fd is not None and os.getpid() == self._owner_pid

    def running(self, label: str) -> None:
        if self._writable():
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, label.encode("utf-8", "replace"), 0)

    def finished(self) -> None:
        if self._writable():
            os.ftruncate(self._fd, 0)

    def stopped_mid_phase(self) -> list[str]:
        """The phases that processes no longer running were in when they
        ended, oldest file first."""
        stopped: list[tuple[float, str]] = []
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        for name in names:
            pid_text = name[len(PHASE_FILE_PREFIX):]
            if not name.startswith(PHASE_FILE_PREFIX) or not pid_text.isdigit():
                continue
            pid = int(pid_text)
            if pid == os.getpid() or _process_runs(pid):
                continue
            path = os.path.join(self.directory, name)
            try:
                with open(path, "rb") as phase_file:
                    label = phase_file.read().decode("utf-8", "replace")
                modified = os.stat(path).st_mtime
            except FileNotFoundError:
                continue
            if label:
                stopped.append((modified, label))
        return [label for _, label in sorted(stopped)]

    def close(self) -> None:
        """A process that ends normally leaves no record. Closed, the
        record writes nothing (its descriptor number may be reused)."""
        if self._fd is None:
            return
        owner = os.getpid() == self._owner_pid
        os.close(self._fd)
        self._fd = None
        if owner:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._path)


_session_phases: SessionPhases | None = None


def _inherited_session_directory() -> str | None:
    """The directory the process that started this one (the xdist
    controller) shares, or None when it is not this process's parent (a
    pytest run started by a test inherits the variable too)."""
    owner, separator, directory = os.environ.get(
        SESSION_DIRECTORY_VARIABLE, "").partition(":")
    if separator and owner.isdigit() and int(owner) == os.getppid():
        return directory if os.path.isdir(directory) else None
    return None


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


def hard_stop_mechanism() -> str | None:
    """How this process stops a test past its hard deadline: "signal
    timer", "faulthandler thread", or None (no hard stop installed)."""
    if _deadlines is None or _deadlines.hard_stop is None:
        return None
    return _deadlines.hard_stop.mechanism


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


# The shared directory this process made (and removes), as the controller,
# and the configs that made it and ``_session_phases``: only those undo
# them, so a pytest run started in-process by a test leaves both alone.
_owned_session_directory: str | None = None
_directory_config: pytest.Config | None = None
_phases_config: pytest.Config | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _owned_session_directory, _directory_config
    config.addinivalue_line(
        "markers",
        "deadline(seconds): fail the test (DeadlineExceeded) when a phase "
        "uses more CPU time; tests/deadline_watchdog.py")
    if (hasattr(config, "workerinput") or _owned_session_directory is not None
            or _session_phases is not None):
        return
    # Before xdist starts its workers (at session start), so they inherit it.
    _owned_session_directory = tempfile.mkdtemp(prefix="equipa-deadlines-")
    _directory_config = config
    os.environ[SESSION_DIRECTORY_VARIABLE] = (
        f"{os.getpid()}:{_owned_session_directory}")


def pytest_sessionstart(session: pytest.Session) -> None:
    global _session_phases, _phases_config
    # Output capture is suspended between conftest loading and the first
    # test, so descriptor 2 is the session's real stderr here.
    install(hard_stop_fd=os.dup(2))
    if _session_phases is not None:
        return
    directory = (_owned_session_directory
                 if not hasattr(session.config, "workerinput")
                 else _inherited_session_directory())
    if directory is not None:
        _session_phases = SessionPhases(directory)
        _phases_config = session.config


def pytest_terminal_summary(terminalreporter, exitstatus: int,
                            config: pytest.Config) -> None:
    """Name the phases a hard stop (or any crash) ended mid-run."""
    if _session_phases is None or hasattr(config, "workerinput"):
        return
    stopped = _session_phases.stopped_mid_phase()
    if stopped:
        terminalreporter.section("deadline watchdog: stopped mid-phase")
        for label in stopped:
            terminalreporter.line(
                f"{label}: its process ended before the phase did (the hard "
                f"stop of tests/deadline_watchdog.py, or a crash)")


def pytest_unconfigure(config: pytest.Config) -> None:
    global _session_phases, _phases_config
    global _owned_session_directory, _directory_config
    if _session_phases is not None and config is _phases_config:
        _session_phases.close()
        _session_phases = _phases_config = None
    if _owned_session_directory is not None and config is _directory_config:
        shutil.rmtree(_owned_session_directory, ignore_errors=True)
        if os.environ.get(SESSION_DIRECTORY_VARIABLE, "").endswith(
                f":{_owned_session_directory}"):
            del os.environ[SESSION_DIRECTORY_VARIABLE]
        _owned_session_directory = _directory_config = None


def _session_hang() -> str | None:
    """The first phase of this session a process ended mid-run (cached as
    this process's own first hang once found)."""
    if _deadlines is None or _session_phases is None:
        return None
    stopped = _session_phases.stopped_mid_phase()
    if not stopped:
        return None
    _deadlines.first_hung_phase = (
        f"{stopped[0]}, ended by the hard stop or a crash,")
    return _deadlines.first_hung_phase


def phase_deadline(item: pytest.Item, phase: str) -> tuple[float, str]:
    """The deadline and label of one test phase: ``deadline_seconds``, cut
    to ``AFTER_A_HANG_DEADLINE_SECONDS`` once a phase of this process has
    run past its own, or a phase of this session was stopped mid-run (the
    label then names that one)."""
    seconds = deadline_seconds(item)
    label = f"{item.nodeid} ({phase})"
    hung = None if _deadlines is None else _deadlines.first_hung_phase
    if hung is None and seconds > AFTER_A_HANG_DEADLINE_SECONDS:
        hung = _session_hang()
    if hung is not None and seconds > AFTER_A_HANG_DEADLINE_SECONDS:
        seconds = AFTER_A_HANG_DEADLINE_SECONDS
        label = (f"{item.nodeid} ({phase}; cut to {seconds:g} s because "
                 f"{hung} ran past its deadline first)")
    return seconds, label


@contextlib.contextmanager
def _phase(item: pytest.Item, phase: str) -> Iterator[None]:
    if _deadlines is None:
        yield
        return
    seconds, label = phase_deadline(item, phase)
    phases = _session_phases
    if phases is not None:
        phases.running(f"{item.nodeid} ({phase})")
    try:
        with deadline(seconds, label) as armed:
            try:
                yield
            finally:
                if armed.raised and _deadlines.first_hung_phase is None:
                    _deadlines.first_hung_phase = f"{item.nodeid} ({phase})"
    finally:
        if phases is not None:
            phases.finished()


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
