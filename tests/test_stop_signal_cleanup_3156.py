#!/usr/bin/env python3
"""Task 3156: a stopped orchestrator removes its live per-run config dirs.

F-3 of the 3153 review: SIGTERM (systemctl stop, timeout) ends the process
without atexit, so the per-run CLAUDE_CONFIG_DIR of a live run (0700, CLI
session state) stayed behind until a later process swept it a day later.
The orchestrator now installs a stop-signal handler, chained with the one
in place, that terminates its live agents, removes the directories this
process created, and re-sends the signal so the process still dies of it.

The end-to-end tests run a real orchestrator process (``_spawn_agent_process``
with a fake ``claude`` that writes into its config directory and sleeps)
and signal it. The unit tests call the handler directly, never raising a
real signal in the test process.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from equipa import agent_runner

REPO_ROOT = Path(__file__).resolve().parents[1]

# The fake CLI: writes into a subdirectory of its config directory (as the
# real CLI does), reports, then sleeps until it is stopped.
FAKE_CLAUDE = '''#!{python}
import json, os, time
here = os.path.dirname(os.path.abspath(__file__))
config_dir = os.environ["CLAUDE_CONFIG_DIR"]
session = os.path.join(config_dir, "projects", "run")
os.makedirs(session)
with open(os.path.join(session, "session.jsonl"), "w") as fh:
    fh.write("{{}}\\n")
report = {{"config_dir": config_dir, "pid": os.getpid()}}
with open(os.path.join(here, "report.tmp"), "w") as fh:
    json.dump(report, fh)
os.rename(os.path.join(here, "report.tmp"), os.path.join(here, "report.json"))
time.sleep(120)
'''

# The orchestrator: spawns the fake CLI through the real dispatch path.
# argv: parent dir, fake claude, project dir, SIGINT/SIGTERM setup mode.
ORCHESTRATOR = '''
import asyncio, os, signal, sys
from equipa import agent_runner, cli_isolation, isolation

parent, fake, project, mode = sys.argv[1:5]
if mode == "sigint-default":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
elif mode == "sigterm-exit":
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(70))
real_create = cli_isolation.create_run_config_dir
agent_runner.create_run_config_dir = lambda: real_create(parent)
isolation.isolation_enabled = lambda *args, **kwargs: False
agent_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": project, "CLAUDE_CODE_OAUTH_TOKEN": "fake-3156-token"}
agent_runner._agent_subprocess_env = lambda: dict(agent_env)

async def spawn():
    return await agent_runner._spawn_agent_process(
        [fake, "-p", "x", "--add-dir", project], project_dir=project)

async def main():
    if mode == "spawned-in-merge":
        # The run starts while a merge is shielded and outlives the merge.
        from equipa.merge_safety import MergeSignalShield
        async with MergeSignalShield(project, context="test merge"):
            process, agent = await spawn()
        merge_ended = os.path.join(os.path.dirname(parent), "merge-ended")
        open(merge_ended, "w").close()
    else:
        process, agent = await spawn()
    await process.wait()

asyncio.run(main())
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


@pytest.fixture
def orchestrator(tmp_path: Path):
    """Start an orchestrator with one live run; yield (process, report)."""
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
    started: list[subprocess.Popen] = []

    def start(mode: str = "default"):
        process = subprocess.Popen(
            [sys.executable, str(script), str(parent), str(fake),
             str(project), mode],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        started.append(process)
        report_path = bin_dir / "report.json"
        try:
            _wait_for(report_path.exists, 60, "the fake CLI to start")
        except AssertionError:
            process.kill()
            output = process.communicate()[0].decode(errors="replace")
            raise AssertionError(f"orchestrator never started the run:\n"
                                 f"{output[-3000:]}") from None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert Path(report["config_dir"]).parent == parent
        return process, report

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def _stop(process: subprocess.Popen, signum: int) -> str:
    process.send_signal(signum)
    output = process.communicate(timeout=90)[0]
    return output.decode(errors="replace")


@pytest.mark.parametrize("signum, mode", [
    (signal.SIGTERM, "default"),
    (signal.SIGINT, "sigint-default"),
])
def test_stop_signal_removes_the_live_run_config_dir(orchestrator, signum,
                                                     mode):
    process, report = orchestrator(mode)
    config_dir = Path(report["config_dir"])
    assert (config_dir / "projects" / "run" / "session.jsonl").exists()

    output = _stop(process, signum)

    assert process.returncode == -signum, output[-3000:]
    assert not os.path.lexists(config_dir), (
        f"per-run config dir left behind after {signal.Signals(signum).name}:"
        f" {sorted(p.name for p in config_dir.rglob('*'))}\n{output[-3000:]}")
    _wait_for(lambda: not _pid_alive(report["pid"]), 30,
              "the fake CLI to be stopped")


def test_chained_handler_that_exits_still_removes_the_dir(orchestrator):
    process, report = orchestrator("sigterm-exit")
    config_dir = Path(report["config_dir"])

    output = _stop(process, signal.SIGTERM)

    # The operator's own handler decided the exit status.
    assert process.returncode == 70, output[-3000:]
    assert not os.path.lexists(config_dir), output[-3000:]
    _wait_for(lambda: not _pid_alive(report["pid"]), 30,
              "the fake CLI to be stopped")


def test_a_run_started_during_a_merge_keeps_its_cleanup(orchestrator):
    """The shield restores the handler it found when the merge began, which
    predates the run; the cleanup must survive that restore."""
    process, report = orchestrator("spawned-in-merge")
    config_dir = Path(report["config_dir"])
    merge_ended = config_dir.parent.parent / "merge-ended"
    _wait_for(merge_ended.exists, 60, "the merge shield to end")

    output = _stop(process, signal.SIGTERM)

    assert process.returncode == -signal.SIGTERM, output[-3000:]
    assert not os.path.lexists(config_dir), (
        f"per-run config dir of a run started during a merge left behind:"
        f" {sorted(p.name for p in config_dir.rglob('*'))}\n{output[-3000:]}")
    _wait_for(lambda: not _pid_alive(report["pid"]), 30,
              "the fake CLI to be stopped")


# --- the handler itself, called directly -----------------------------------

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


def test_chained_handler_that_returns_leaves_live_runs_alone(live_dirs):
    """A handler that only records the request (a merge shield) keeps the
    process, and its runs, going."""
    own, foreign, registry = live_dirs
    calls = []
    handler = agent_runner._stop_signal_cleanup_handler(
        lambda signum, frame: calls.append(signum))

    handler(signal.SIGTERM, None)

    assert calls == [signal.SIGTERM]
    assert own.is_dir() and foreign.is_dir()
    assert set(registry) == {str(own), str(foreign)}


def test_chained_handler_that_raises_removes_only_own_dirs(live_dirs):
    own, foreign, registry = live_dirs

    def operator_handler(signum, frame):
        raise SystemExit(3)

    handler = agent_runner._stop_signal_cleanup_handler(operator_handler)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)

    assert not os.path.lexists(own)
    assert foreign.is_dir(), "a directory of another process was removed"
    assert registry == {str(foreign): os.getpid() + 1}


def test_cleanup_never_raises(live_dirs, monkeypatch):
    own, _foreign, _registry = live_dirs

    def broken_terminate():
        raise RuntimeError("launcher gone")

    monkeypatch.setattr(agent_runner, "_terminate_live_agents_at_exit",
                        broken_terminate)
    agent_runner._remove_live_cli_config_dirs_on_stop(terminate_agents=True)
    assert own.is_dir()
    assert agent_runner._stop_cleanup_running is False


def test_a_second_signal_during_cleanup_returns_at_once(live_dirs,
                                                        monkeypatch):
    own, _foreign, _registry = live_dirs
    calls = []
    handler = agent_runner._stop_signal_cleanup_handler(
        lambda signum, frame: calls.append(signum))
    monkeypatch.setattr(agent_runner, "_stop_cleanup_running", True)

    handler(signal.SIGTERM, None)

    assert calls == []
    assert own.is_dir()


# --- installation -------------------------------------------------------------

@pytest.fixture
def saved_handlers():
    saved = {signum: signal.getsignal(signum)
             for signum in agent_runner._STOP_SIGNALS}
    yield saved
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def _is_cleanup_handler(handler) -> bool:
    return getattr(handler, agent_runner._STOP_CLEANUP_MARKER, False)


def test_install_chains_sigterm_and_leaves_python_sigint(saved_handlers):
    operator_handler = signal.signal(signal.SIGTERM, lambda s, f: None)
    signal.signal(signal.SIGINT, signal.default_int_handler)

    agent_runner._install_stop_signal_cleanup()
    installed = signal.getsignal(signal.SIGTERM)
    agent_runner._install_stop_signal_cleanup()

    assert _is_cleanup_handler(installed)
    assert signal.getsignal(signal.SIGTERM) is installed, "installed twice"
    # asyncio.run only installs its Ctrl-C handler over default_int_handler.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    del operator_handler


def test_install_takes_over_default_dispositions(saved_handlers):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    agent_runner._install_stop_signal_cleanup()

    assert _is_cleanup_handler(signal.getsignal(signal.SIGTERM))
    assert _is_cleanup_handler(signal.getsignal(signal.SIGINT))


def test_install_during_a_merge_survives_the_shield_restore(saved_handlers,
                                                           tmp_path):
    import asyncio

    from equipa.merge_safety import MergeSignalShield

    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    async def merge_with_a_run_started_inside():
        async with MergeSignalShield(tmp_path, context="test merge"):
            agent_runner._install_stop_signal_cleanup()
            # A second run during the same merge changes nothing.
            agent_runner._install_stop_signal_cleanup()

    asyncio.run(merge_with_a_run_started_inside())

    for signum in agent_runner._STOP_SIGNALS:
        restored = signal.getsignal(signum)
        assert _is_cleanup_handler(restored), (
            f"{signal.Signals(signum).name}: the merge shield restored "
            f"{restored!r} and dropped the cleanup")


def test_a_shield_restoring_a_cleanup_handler_keeps_it(saved_handlers,
                                                       tmp_path):
    """Installed before the merge: the shield restores the same handler."""
    import asyncio

    from equipa.merge_safety import MergeSignalShield

    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    agent_runner._install_stop_signal_cleanup()
    installed = signal.getsignal(signal.SIGTERM)

    async def merge_with_a_run_started_inside():
        async with MergeSignalShield(tmp_path, context="test merge"):
            agent_runner._install_stop_signal_cleanup()

    asyncio.run(merge_with_a_run_started_inside())

    assert signal.getsignal(signal.SIGTERM) is installed


def test_install_leaves_ignored_signals_ignored(saved_handlers):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    agent_runner._install_stop_signal_cleanup()

    assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN


def test_install_is_a_no_op_off_the_main_thread(saved_handlers):
    import threading

    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    errors = []

    def install():
        try:
            agent_runner._install_stop_signal_cleanup()
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    worker = threading.Thread(target=install)
    worker.start()
    worker.join(timeout=10)
    assert errors == []
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_created_dirs_are_registered_and_released(tmp_path, monkeypatch,
                                                  saved_handlers):
    monkeypatch.setattr(agent_runner, "_LIVE_CLI_CONFIG_DIRS", {})
    real_create = agent_runner.create_run_config_dir
    monkeypatch.setattr(agent_runner, "create_run_config_dir",
                        lambda: real_create(str(tmp_path)))

    config_dir = agent_runner._create_cli_config_dir()

    assert agent_runner._LIVE_CLI_CONFIG_DIRS == {config_dir: os.getpid()}
    assert _is_cleanup_handler(signal.getsignal(signal.SIGTERM))
    agent_runner._remove_cli_config_dir(config_dir)
    assert agent_runner._LIVE_CLI_CONFIG_DIRS == {}
    assert not os.path.lexists(config_dir)
