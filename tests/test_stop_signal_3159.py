#!/usr/bin/env python3
"""Task 3159: the stop-signal handler of task 3156 never loses the signal.

Follow-ups of the independent review of task 3156:

* R1: a log call in the stop path raised out of the handler (the main
  thread blocked writing to a full stderr pipe makes every stderr write a
  "reentrant call" RuntimeError, which logging's own error report raises
  again), so the handler never re-sent SIGTERM and the process kept running.
  Logging in the stop path can no longer raise, and the signal is re-sent
  in a ``finally``.
* R4: agents are terminated before their directories are removed; a CLI
  still running would write into the directory after its removal.
* R6: a stop signal between ``mkdtemp`` and the registration of the new
  directory (or before the first handler install) left that directory to
  the 24 h sweep.

The end-to-end tests run a real orchestrator process (``_spawn_agent_process``
with a fake ``claude``) and signal it. The unit tests call the handler
directly with ``os.kill`` and ``signal.signal`` replaced, never raising a
real signal in the test process.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import array
import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from equipa import agent_runner

REPO_ROOT = Path(__file__).resolve().parents[1]

# The fake CLI keeps writing into its config directory, as the real CLI does
# while it works, and writes its session once more when it is stopped. A
# directory removed while it still runs comes back.
FAKE_CLAUDE = '''#!{python}
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
config_dir = os.environ["CLAUDE_CONFIG_DIR"]
session = os.path.join(config_dir, "projects", "run")

def write(name):
    os.makedirs(session, exist_ok=True)
    with open(os.path.join(session, name), "w") as fh:
        fh.write("{{}}\\n")

def stop(signum, frame):
    write("stopped.jsonl")
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
write("session.jsonl")
report = {{"config_dir": config_dir, "pid": os.getpid()}}
with open(os.path.join(here, "report.tmp"), "w") as fh:
    json.dump(report, fh)
os.rename(os.path.join(here, "report.tmp"), os.path.join(here, "report.json"))
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    write("session.jsonl")
    time.sleep(0.01)
'''

# The orchestrator: spawns the fake CLI through the real dispatch path.
# argv: parent dir, fake claude, project dir, mode. The stderr modes make
# every removal report an error, so the stop path logs a warning, and then
# fill stderr: "stderr-blocking" blocks in a write to the full pipe,
# "stderr-nonblocking" switches it to O_NONBLOCK, fills it and waits.
ORCHESTRATOR = '''
import asyncio, os, sys
from equipa import agent_runner, cli_isolation, isolation

parent, fake, project, mode = sys.argv[1:5]
marker = os.path.join(os.path.dirname(parent), "stderr-full")
real_create = cli_isolation.create_run_config_dir
agent_runner.create_run_config_dir = lambda: real_create(parent)
isolation.isolation_enabled = lambda *args, **kwargs: False
agent_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": project, "CLAUDE_CODE_OAUTH_TOKEN": "fake-3159-token"}
agent_runner._agent_subprocess_env = lambda: dict(agent_env)
if mode.startswith("stderr-"):
    real_remove = agent_runner.remove_run_config_dir
    agent_runner.remove_run_config_dir = (
        lambda path: real_remove(path) + ["forced error for the test"])
    # The blocked log line waits for the watchdog; keep the test short.
    agent_runner._STOP_CLEANUP_BUDGET_SECONDS = 5.0

async def main():
    process, agent = await agent_runner._spawn_agent_process(
        [fake, "-p", "x", "--add-dir", project], project_dir=project)
    if mode == "stderr-blocking":
        open(marker, "w").close()
        sys.stderr.write("x" * 8_000_000)
    elif mode == "stderr-nonblocking":
        # os.write: sys.stderr.write returns quietly on a full O_NONBLOCK
        # pipe (CPython 3.12), so it never reports the pipe as full.
        os.set_blocking(2, False)
        try:
            while True:
                os.write(2, b"x" * 65536)
        except BlockingIOError:
            pass
        open(marker, "w").close()
    await process.wait()

asyncio.run(main())
'''

# A stop signal arrives while the per-run directory is being created:
# after mkdtemp, before _create_cli_config_dir registers the directory.
# argv: parent dir, mode ("first-run" or "after-a-run").
CREATE_THEN_STOP = '''
import signal, sys
from equipa import agent_runner, cli_isolation

parent, mode = sys.argv[1:3]
real_create = cli_isolation.create_run_config_dir
agent_runner.create_run_config_dir = lambda: real_create(parent)
if mode == "after-a-run":
    agent_runner._create_cli_config_dir()

def create_then_stop():
    config_dir = real_create(parent)
    signal.raise_signal(signal.SIGTERM)
    return config_dir

agent_runner.create_run_config_dir = create_then_stop
agent_runner._create_cli_config_dir()
print("the stop signal was lost", flush=True)
'''


def _wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat_file:
            return stat_file.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def _pipe_is_full(read_fd: int) -> bool:
    """True once the pipe holds as many bytes as it can take."""
    pending = array.array("i", [0])
    fcntl.ioctl(read_fd, termios.FIONREAD, pending, True)
    return pending[0] >= fcntl.fcntl(read_fd, fcntl.F_GETPIPE_SZ)


def _tail(data: bytes) -> str:
    """The end of an output, without the filler the stderr modes write."""
    return data.decode(errors="replace").replace("x", "")[-3000:]


@pytest.fixture
def orchestrator(tmp_path: Path):
    """Start an orchestrator with one live run; return (process, report).

    stdout and stderr are separate pipes that nothing reads while the
    orchestrator runs."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable),
                    encoding="utf-8")
    fake.chmod(0o755)
    parent = tmp_path / "orchestrator-tmp"
    parent.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir()
    script = tmp_path / "orchestrator.py"
    script.write_text(ORCHESTRATOR, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    started: list[tuple[subprocess.Popen, dict]] = []

    def start(mode: str = "default"):
        process = subprocess.Popen(
            [sys.executable, str(script), str(parent), str(fake),
             str(project), mode],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        report: dict = {}
        started.append((process, report))
        report_path = bin_dir / "report.json"
        try:
            _wait_for(report_path.exists, 60, "the fake CLI to start")
        except AssertionError:
            process.kill()
            _out, err = process.communicate()
            raise AssertionError(f"orchestrator never started the run:\n"
                                 f"{_tail(err)}") from None
        report.update(json.loads(report_path.read_text(encoding="utf-8")))
        assert Path(report["config_dir"]).parent == parent
        return process, report

    yield start
    for process, report in started:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)
        if report and _pid_alive(report["pid"]):
            os.kill(report["pid"], signal.SIGKILL)


def _assert_died_of(process: subprocess.Popen, signum: int, why: str) -> None:
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        _out, err = process.communicate(timeout=30)
        raise AssertionError(f"{why}: the process was still running 30 s "
                             f"after {signal.Signals(signum).name}\n"
                             f"{_tail(err)}") from None
    assert process.returncode == -signum, (
        f"{why}: exit status {process.returncode}\n"
        f"{_tail(process.communicate(timeout=30)[1])}")


# --- R1: a failing log call never costs the signal ------------------------------

@pytest.mark.parametrize("mode", ["stderr-blocking", "stderr-nonblocking"])
def test_sigterm_with_stderr_a_full_pipe_still_kills(orchestrator, tmp_path,
                                                     mode):
    """stderr is a pipe nobody reads, and the cleanup logs a warning.

    "stderr-blocking": the main thread is blocked in a large write to the
    full pipe when SIGTERM arrives; the handler's log line then waits for
    the same space, for good (CPython 3.12), until the watchdog cuts it.
    "stderr-nonblocking": the pipe is full and O_NONBLOCK; log writes fail
    or return at once."""
    process, report = orchestrator(mode)
    config_dir = Path(report["config_dir"])
    marker = tmp_path / "stderr-full"
    try:
        _wait_for(marker.exists, 60, "the orchestrator to fill stderr")
        _wait_for(lambda: _pipe_is_full(process.stderr.fileno()), 60,
                  "the stderr pipe to be full")
    except AssertionError as exc:
        process.kill()
        out, err = process.communicate(timeout=30)
        raise AssertionError(f"{exc} (exit status {process.returncode})\n"
                             f"{_tail(out)}\n{_tail(err)}") from None

    process.send_signal(signal.SIGTERM)

    _assert_died_of(process, signal.SIGTERM, "SIGTERM lost")
    assert not os.path.lexists(config_dir)
    _wait_for(lambda: not _pid_alive(report["pid"]), 30,
              "the fake CLI to be stopped")


# --- R4: agents are terminated before their directories are removed ---------

def test_a_cli_writing_until_it_is_stopped_leaves_no_dir(orchestrator):
    """The fake CLI writes its session once more when SIGTERMed. Removing
    the directory before the CLI is stopped (``terminate_agents=False`` on
    the SIG_DFL path) lets that write bring the directory back."""
    process, report = orchestrator("default")
    config_dir = Path(report["config_dir"])

    process.send_signal(signal.SIGTERM)

    _assert_died_of(process, signal.SIGTERM, "SIGTERM")
    _wait_for(lambda: not _pid_alive(report["pid"]), 30,
              "the fake CLI to be stopped")
    assert not os.path.lexists(config_dir), (
        "the CLI wrote into its directory after the removal: "
        f"{sorted(p.name for p in config_dir.rglob('*'))}")


@pytest.fixture
def live_dirs(tmp_path: Path, monkeypatch):
    """Two registered live dirs: one of this process, one of another PID."""
    own = tmp_path / "equipa-claude-config-own"
    foreign = tmp_path / "equipa-claude-config-foreign"
    for path in (own, foreign):
        (path / "projects").mkdir(parents=True)
        (path / "projects" / "state.json").write_text("{}", encoding="utf-8")
    registry = {str(own): os.getpid(), str(foreign): os.getpid() + 1}
    monkeypatch.setattr(agent_runner, "_LIVE_CLI_CONFIG_DIRS", registry)
    monkeypatch.setattr(agent_runner, "_terminate_live_agents_at_exit",
                        lambda: None)
    return own, foreign, registry


@pytest.fixture
def resend(monkeypatch):
    """Record the SIG_DFL restore and the re-sent signal, never sending it.

    The SIGALRM watchdog is left out (it has tests of its own below), so
    no real timer is armed in the test process."""
    monkeypatch.setattr(agent_runner, "_stop_cleanup_watchdog",
                        contextlib.nullcontext)
    calls: list[tuple] = []
    monkeypatch.setattr(agent_runner.signal, "signal",
                        lambda signum, handler: calls.append(
                            ("signal", signum, handler)))
    monkeypatch.setattr(agent_runner.os, "kill",
                        lambda pid, signum: calls.append(("kill", pid, signum)))
    return calls


def _default_path_handler():
    return agent_runner._stop_signal_cleanup_handler(signal.SIG_DFL)


def test_agents_are_terminated_before_the_dirs_are_removed(live_dirs, resend,
                                                           monkeypatch):
    own, _foreign, _registry = live_dirs
    order: list[str] = []
    real_remove = agent_runner.remove_run_config_dir
    monkeypatch.setattr(agent_runner, "_terminate_live_agents_at_exit",
                        lambda: order.append("terminate"))

    def remove(path):
        order.append("remove")
        return real_remove(path)

    monkeypatch.setattr(agent_runner, "remove_run_config_dir", remove)

    _default_path_handler()(signal.SIGTERM, None)

    assert order == ["terminate", "remove"]
    assert not os.path.lexists(own)
    assert resend == [("signal", signal.SIGTERM, signal.SIG_DFL),
                      ("kill", os.getpid(), signal.SIGTERM)]


class _RaisingHandler(logging.Handler):
    """A log handler whose emit raises, as a closed, full or busy stream
    makes a handler do when it does not route the error to handleError."""

    def __init__(self, error: Exception) -> None:
        super().__init__(level=logging.DEBUG)
        self.error = error

    def emit(self, record: logging.LogRecord) -> None:
        raise self.error


class _BusyStream:
    """stderr while the main thread is blocked writing to it."""

    def write(self, text: str) -> int:
        raise RuntimeError("reentrant call inside "
                           "<_io.BufferedWriter name='<stderr>'>")

    def flush(self) -> None:
        raise RuntimeError("reentrant call inside "
                           "<_io.BufferedWriter name='<stderr>'>")


@pytest.fixture
def stop_path_logger(monkeypatch):
    """The agent_runner logger with only the handler a test adds."""
    test_logger = agent_runner.logger
    monkeypatch.setattr(test_logger, "handlers", [])
    monkeypatch.setattr(test_logger, "propagate", False)
    monkeypatch.setattr(test_logger, "disabled", False)
    saved_level = test_logger.level
    test_logger.setLevel(logging.DEBUG)
    yield test_logger
    test_logger.setLevel(saved_level)


def _force_remove_error(monkeypatch) -> None:
    real_remove = agent_runner.remove_run_config_dir
    monkeypatch.setattr(agent_runner, "remove_run_config_dir",
                        lambda path: real_remove(path) + ["forced error"])


@pytest.mark.parametrize("error", [
    BrokenPipeError(32, "Broken pipe"),
    BlockingIOError(11, "Resource temporarily unavailable"),
    OSError(5, "Input/output error"),
    RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>"),
], ids=lambda error: type(error).__name__)
def test_a_log_handler_that_raises_never_costs_the_signal(
        live_dirs, resend, monkeypatch, stop_path_logger, error):
    own, foreign, _registry = live_dirs
    stop_path_logger.addHandler(_RaisingHandler(error))
    _force_remove_error(monkeypatch)

    _default_path_handler()(signal.SIGTERM, None)

    assert resend[-1] == ("kill", os.getpid(), signal.SIGTERM)
    assert not os.path.lexists(own)
    assert foreign.is_dir()
    assert agent_runner._stop_cleanup_running is False


def test_a_busy_stderr_never_costs_the_signal(live_dirs, resend, monkeypatch,
                                              stop_path_logger):
    """The review's case, in process: the stream handler fails, and
    logging's own error report on the same stderr fails again."""
    own, _foreign, _registry = live_dirs
    busy = _BusyStream()
    monkeypatch.setattr(sys, "stderr", busy)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    stop_path_logger.addHandler(logging.StreamHandler(busy))
    _force_remove_error(monkeypatch)

    _default_path_handler()(signal.SIGTERM, None)

    assert resend[-1] == ("kill", os.getpid(), signal.SIGTERM)
    assert not os.path.lexists(own)
    assert logging.raiseExceptions is True, "the logging setting was not restored"


def test_a_failing_agent_termination_still_resends_the_signal(
        live_dirs, resend, monkeypatch, stop_path_logger):
    """Terminating the agents fails and so does the log line about it."""
    own, _foreign, _registry = live_dirs
    stop_path_logger.addHandler(_RaisingHandler(BrokenPipeError(32, "EPIPE")))

    def broken_terminate():
        raise RuntimeError("launcher gone")

    monkeypatch.setattr(agent_runner, "_terminate_live_agents_at_exit",
                        broken_terminate)

    _default_path_handler()(signal.SIGTERM, None)

    assert resend[-1] == ("kill", os.getpid(), signal.SIGTERM)
    # A CLI that may still run is not raced: the stale sweep takes the dir.
    assert own.is_dir()


def test_the_signal_is_resent_even_when_the_cleanup_raises(live_dirs, resend,
                                                           monkeypatch):
    """Whatever escapes the cleanup (here a KeyboardInterrupt), the
    SIG_DFL path still restores SIG_DFL and re-sends the signal."""
    def interrupted(*, terminate_agents):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_runner, "_remove_live_cli_config_dirs_on_stop",
                        interrupted)

    with pytest.raises(KeyboardInterrupt):
        _default_path_handler()(signal.SIGTERM, None)

    assert resend == [("signal", signal.SIGTERM, signal.SIG_DFL),
                      ("kill", os.getpid(), signal.SIGTERM)]


def test_failing_log_calls_in_agent_termination_try_every_agent(
        monkeypatch, stop_path_logger):
    """``_terminate_live_agents_at_exit`` runs in the stop path too: a log
    call about one agent that fails does not skip the next agent."""
    stop_path_logger.addHandler(_RaisingHandler(BrokenPipeError(32, "EPIPE")))
    terminated: list[str] = []

    class Agent:
        def __init__(self, name: str, fails: bool) -> None:
            self.name = name
            self.fails = fails
            self.pid = 4242
            self.owner_pid = os.getpid()

        def request_termination(self) -> None:
            pass

        def terminate_sync(self) -> None:
            if self.fails:
                raise OSError("launcher gone")
            terminated.append(self.name)

    agents = [Agent("first", True), Agent("second", False)]
    monkeypatch.setattr(agent_runner, "_LIVE_CONTAINED_AGENTS", agents)

    agent_runner._terminate_live_agents_at_exit()

    assert terminated == ["second"]


@pytest.fixture
def alarm_state():
    """Restore SIGALRM and the interval timer after a watchdog test."""
    saved_handler = signal.getsignal(signal.SIGALRM)
    saved_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, saved_handler)
    if saved_timer[0] > 0:
        signal.setitimer(signal.ITIMER_REAL, *saved_timer)


def test_the_watchdog_cuts_a_hanging_cleanup_short(alarm_state, monkeypatch):
    """A blocked call (here a sleep; in the field a write to a full stderr
    pipe) is interrupted, and the exception gets past ``except Exception``."""
    monkeypatch.setattr(agent_runner, "_STOP_CLEANUP_BUDGET_SECONDS", 0.2)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    started = time.monotonic()

    with pytest.raises(agent_runner._StopCleanupTimeout):
        with agent_runner._stop_cleanup_watchdog():
            try:
                time.sleep(10)
            except Exception:  # noqa: BLE001 - must not catch the watchdog
                pass

    assert time.monotonic() - started < 5
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_the_watchdog_is_disarmed_after_a_cleanup_in_time(alarm_state):
    signal.signal(signal.SIGALRM, signal.SIG_DFL)

    with agent_runner._stop_cleanup_watchdog():
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= agent_runner._STOP_CLEANUP_BUDGET_SECONDS

    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_the_watchdog_leaves_an_operator_sigalrm_alone(alarm_state):
    def operator_alarm(signum, frame):
        pass

    signal.signal(signal.SIGALRM, operator_alarm)

    with agent_runner._stop_cleanup_watchdog():
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    assert signal.getsignal(signal.SIGALRM) is operator_alarm


def test_the_default_path_runs_the_cleanup_under_the_watchdog(
        live_dirs, resend, monkeypatch):
    """The SIG_DFL path, not only the context manager, is bounded."""
    entered: list[str] = []

    @contextlib.contextmanager
    def watchdog():
        entered.append("armed")
        yield
        entered.append("disarmed")

    monkeypatch.setattr(agent_runner, "_stop_cleanup_watchdog", watchdog)
    monkeypatch.setattr(agent_runner, "_terminate_live_agents_at_exit",
                        lambda: entered.append("terminate"))

    _default_path_handler()(signal.SIGTERM, None)

    assert entered == ["armed", "terminate", "disarmed"]
    assert resend[-1] == ("kill", os.getpid(), signal.SIGTERM)


# --- R6: a directory is registered before a stop signal can see it ------------

@pytest.mark.parametrize("mode", ["first-run", "after-a-run"])
def test_a_stop_signal_during_creation_removes_the_new_dir(tmp_path, mode):
    """SIGTERM between mkdtemp and the registration: on the first run there
    was no handler yet, after a run the handler missed the new directory."""
    parent = tmp_path / "orchestrator-tmp"
    parent.mkdir(mode=0o700)
    script = tmp_path / "create_then_stop.py"
    script.write_text(CREATE_THEN_STOP, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(parent), mode],
        cwd=str(REPO_ROOT), env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        capture_output=True, text=True, timeout=60)

    assert result.returncode == -signal.SIGTERM, result.stdout + result.stderr
    assert sorted(os.listdir(parent)) == [], (
        "a per-run directory created while SIGTERM arrived was left behind")


def test_the_handler_defers_while_this_process_creates_a_dir(live_dirs,
                                                             resend,
                                                             monkeypatch):
    own, _foreign, _registry = live_dirs
    monkeypatch.setattr(agent_runner, "_cli_config_dir_creator_pid",
                        os.getpid())
    monkeypatch.setattr(agent_runner, "_stop_signal_during_creation", None)

    _default_path_handler()(signal.SIGTERM, None)

    assert resend == []
    assert own.is_dir()
    assert agent_runner._stop_signal_during_creation == signal.SIGTERM


def test_a_forked_child_does_not_defer_on_its_parents_creation(live_dirs,
                                                               resend,
                                                               monkeypatch):
    """A child forked while its parent created a directory inherits the
    creator PID; its own stop signal must not wait for that creation."""
    monkeypatch.setattr(agent_runner, "_cli_config_dir_creator_pid",
                        os.getpid() + 1)
    monkeypatch.setattr(agent_runner, "_stop_signal_during_creation", None)

    _default_path_handler()(signal.SIGTERM, None)

    assert resend[-1] == ("kill", os.getpid(), signal.SIGTERM)
    assert agent_runner._stop_signal_during_creation is None


def test_a_deferred_signal_is_resent_after_registration(tmp_path, monkeypatch):
    raised: list[int] = []
    monkeypatch.setattr(agent_runner, "_LIVE_CLI_CONFIG_DIRS", {})
    monkeypatch.setattr(agent_runner, "_install_stop_signal_cleanup",
                        lambda: None)
    monkeypatch.setattr(agent_runner.signal, "raise_signal", raised.append)
    real_create = agent_runner.create_run_config_dir

    def create_while_signalled():
        config_dir = real_create(str(tmp_path))
        # What the handler does when SIGTERM arrives now.
        agent_runner._stop_signal_during_creation = signal.SIGTERM
        assert config_dir not in agent_runner._LIVE_CLI_CONFIG_DIRS
        return config_dir

    monkeypatch.setattr(agent_runner, "create_run_config_dir",
                        create_while_signalled)

    config_dir = agent_runner._create_cli_config_dir()

    assert raised == [signal.SIGTERM]
    assert agent_runner._LIVE_CLI_CONFIG_DIRS == {config_dir: os.getpid()}
    assert agent_runner._stop_signal_during_creation is None
    assert agent_runner._cli_config_dir_creator_pid is None
    agent_runner._remove_cli_config_dir(config_dir)
