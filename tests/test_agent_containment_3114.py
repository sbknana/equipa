"""Task 3114 (gate-05): agents are contained by ancestry, not process group.

Copyright 2026 Forgeborn

The real Claude CLI runs every Bash-tool command in a NEW session, so a
``nohup watcher &`` an agent starts is outside the CLI's process group and
survives ``killpg``. The fake ``claude`` here behaves the same way: its "tool
shell" is started with setsid, backgrounds ``nohup sleep 300`` and exits, so
the watcher is orphaned in an unrelated session. Each end-to-end test asserts
the watcher is dead once ``run_agent`` returns, or once the orchestrator's
event loop is closed, or once the orchestrator itself has been SIGKILLed.

Every test here fails on the pre-3114 code, where the CLI is spawned directly
and only its pid is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import agent_launcher, agent_runner
from equipa.abort_controller import AbortController

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fake CLI. The tool shell is started in a new session (as the real CLI's
# Node ``detached: true`` spawn does) and leaves a nohup watcher behind. An
# optional second tool process ignores SIGTERM and SIGHUP, so only SIGKILL
# removes it.
FAKE_CLAUDE_SOURCE = textwrap.dedent('''\
    import json
    import os
    import signal
    import subprocess
    import sys
    import time

    QUIET = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
             "stderr": subprocess.DEVNULL}
    WATCHER_SHELL = (
        'nohup sleep 300 >/dev/null 2>&1 & echo $! > "$FAKE_WATCHER_PIDFILE"'
    )
    STUBBORN_CODE = (
        "import signal, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\n"
        "time.sleep(300)\\n"
    )

    subprocess.run(["sh", "-c", WATCHER_SHELL], start_new_session=True,
                   check=True, **QUIET)
    stubborn_pidfile = os.environ.get("FAKE_STUBBORN_PIDFILE")
    if stubborn_pidfile:
        stubborn = subprocess.Popen([sys.executable, "-c", STUBBORN_CODE],
                                    start_new_session=True, **QUIET)
        with open(stubborn_pidfile + ".tmp", "w") as handle:
            handle.write(str(stubborn.pid))
        os.replace(stubborn_pidfile + ".tmp", stubborn_pidfile)

    mode = os.environ.get("FAKE_CLAUDE_MODE", "exit")
    if mode == "exit":
        print(json.dumps({"type": "result", "subtype": "success",
                          "result": "done", "num_turns": 1,
                          "is_error": False}), flush=True)
        sys.exit(0)
    if mode == "exit_code":
        sys.exit(int(os.environ["FAKE_EXIT_CODE"]))
    if mode == "self_kill":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "hang":
        time.sleep(300)
    sys.exit(f"unknown FAKE_CLAUDE_MODE {mode!r}")
''')

# A minimal orchestrator: runs one agent and blocks until it is SIGKILLed.
ORCHESTRATOR_SOURCE = textwrap.dedent('''\
    import asyncio
    import sys

    sys.path.insert(0, sys.argv[1])
    from equipa import agent_runner

    agent_runner.AGENT_TERMINATION_GRACE_SECONDS = 1.0
    asyncio.run(agent_runner.run_agent(["claude", "-p", "x"], timeout=120,
                                       max_retries=1))
''')

# An orchestrator that exits with a run still pending: the loop is neither
# cancelled nor closed, so only the exit handlers stand between the agent and
# an unsupervised afterlife.
ORCHESTRATOR_EXIT_SOURCE = textwrap.dedent('''\
    import asyncio
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from equipa import agent_runner

    agent_runner.AGENT_TERMINATION_GRACE_SECONDS = 1.0
    ready_files = [Path(arg) for arg in sys.argv[2:]]


    async def spawned() -> None:
        while not all(path.is_file() and path.read_text().strip()
                      for path in ready_files):
            await asyncio.sleep(0.05)


    loop = asyncio.new_event_loop()
    loop.create_task(agent_runner.run_agent(["claude", "-p", "x"], timeout=120,
                                            max_retries=1))
    loop.run_until_complete(asyncio.wait_for(spawned(), 20))
    sys.exit(0)
''')


def _alive(pid: int, start_time: int | None = None) -> bool:
    """True while ``pid`` runs (a zombie counts as dead) as the same process."""
    stat = agent_launcher.read_proc_stat(pid)
    if stat is None or stat[0] == "Z":
        return False
    return start_time is None or stat[3] == start_time


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _FakeClaude:
    """A fake ``claude`` on PATH plus bookkeeping of what it spawned."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "claude"
        script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE_SOURCE}")
        script.chmod(0o755)
        self.watcher_pidfile = tmp_path / "watcher.pid"
        self.stubborn_pidfile = tmp_path / "stubborn.pid"
        self.env_path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self._monkeypatch = monkeypatch
        self._seen: dict[int, int] = {}
        monkeypatch.setenv("PATH", self.env_path)
        monkeypatch.setenv("FAKE_WATCHER_PIDFILE", str(self.watcher_pidfile))
        # A short grace keeps the SIGTERM-ignoring child's tests quick.
        # raising=False so that, run against pre-3114 code, these tests fail
        # on their assertions (the watcher is alive) rather than in setup.
        monkeypatch.setattr(agent_runner, "AGENT_TERMINATION_GRACE_SECONDS",
                            1.0, raising=False)

    def mode(self, mode: str, *, stubborn: bool = False) -> None:
        self._monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        if stubborn:
            self._monkeypatch.setenv("FAKE_STUBBORN_PIDFILE",
                                     str(self.stubborn_pidfile))

    def spawned_ready(self, stubborn: bool = False) -> bool:
        files = [self.watcher_pidfile]
        if stubborn:
            files.append(self.stubborn_pidfile)
        return all(path.is_file() and path.read_text().strip() for path in files)

    def pid(self, path: Path) -> int:
        pid = int(path.read_text().strip())
        start_time = agent_launcher.proc_start_time(pid)
        if start_time is not None:
            self._seen.setdefault(pid, start_time)
        return pid

    def watcher(self) -> int:
        return self.pid(self.watcher_pidfile)

    def stubborn(self) -> int:
        return self.pid(self.stubborn_pidfile)

    def kill_leftovers(self) -> None:
        """Teardown: SIGKILL anything the fake left (only on a failing run)."""
        for path in (self.watcher_pidfile, self.stubborn_pidfile):
            if path.is_file() and path.read_text().strip():
                self.pid(path)
        for pid, start_time in self._seen.items():
            agent_launcher.signal_process_identity(pid, start_time,
                                                   signal.SIGKILL)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    fake = _FakeClaude(tmp_path, monkeypatch)
    yield fake
    fake.kill_leftovers()


def _run_streaming(timeout: int, **kwargs):
    return agent_runner._run_agent_streaming_impl(
        ["claude", "-p", "x"], timeout=timeout, **kwargs)


def _run_plain(timeout: int, **kwargs):
    return agent_runner.run_agent(
        ["claude", "-p", "x"], timeout=timeout, max_retries=1, **kwargs)


RUNNERS = [
    pytest.param(_run_plain, id="run_agent"),
    pytest.param(_run_streaming, id="streaming"),
]


# --- End to end: the watcher dies on every exit path ------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_normal_exit_kills_setsid_nohup_watcher(fake_claude, runner):
    fake_claude.mode("exit")

    result = asyncio.run(runner(30))

    watcher = fake_claude.watcher()
    assert not _alive(watcher), (
        f"nohup watcher {watcher} started in its own session outlived the run")
    assert not agent_runner._LIVE_CONTAINED_AGENTS
    if runner is _run_plain:
        assert result["success"] is True
        assert result["result_text"] == "done"


@pytest.mark.parametrize("runner", RUNNERS)
def test_timeout_kills_setsid_nohup_watcher(fake_claude, runner):
    fake_claude.mode("hang")

    result = asyncio.run(runner(2))

    assert result["success"] is False
    # run_agent says "timed out"; the streaming monitor's silence timer
    # (capped at the overall timeout) says "overall timeout".
    assert any("timed out" in error or "timeout" in error
               for error in result["errors"])
    watcher = fake_claude.watcher()
    assert not _alive(watcher), f"watcher {watcher} survived the timeout kill"


def test_killed_cli_leaves_no_watcher(fake_claude):
    fake_claude.mode("self_kill", stubborn=True)

    result = asyncio.run(_run_plain(30))

    assert result["success"] is False
    assert not _alive(fake_claude.watcher())
    assert not _alive(fake_claude.stubborn()), (
        "a SIGTERM-ignoring child survived; SIGKILL escalation is missing")


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("how", ["cancel_task", "loop_shutdown"])
def test_cancelled_run_then_closed_loop_leaves_nothing(fake_claude, runner, how):
    """PT-02: the SIGKILL escalation must not depend on an asyncio task.

    ``cancel_task`` cancels the run and awaits it; ``loop_shutdown`` returns
    from main() with the run still pending, so ``asyncio.run`` cancels it
    while closing the loop. Both must leave nothing running, including a
    child that ignores SIGTERM, by the time ``asyncio.run`` returns.
    """
    fake_claude.mode("hang", stubborn=True)

    async def orchestrator() -> None:
        task = asyncio.get_running_loop().create_task(runner(120))
        deadline = time.monotonic() + 20
        while not fake_claude.spawned_ready(stubborn=True):
            assert time.monotonic() < deadline, "the fake never started"
            assert not task.done(), task.result()
            await asyncio.sleep(0.05)
        if how == "cancel_task":
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(orchestrator())

    watcher, stubborn = fake_claude.watcher(), fake_claude.stubborn()
    assert not _alive(watcher), f"watcher {watcher} outlived the closed loop"
    assert not _alive(stubborn), (
        f"SIGTERM-ignoring child {stubborn} outlived the closed loop")
    assert not agent_runner._LIVE_CONTAINED_AGENTS


@pytest.mark.parametrize("how", ["timeout", "cancel_task"])
def test_slow_launcher_backstop_kills_setsid_descendants(fake_claude,
                                                          monkeypatch, how):
    """If the launcher has not finished its sweep in time, the orchestrator
    kills the launcher's descendants itself before the group SIGKILL, which
    would otherwise orphan a setsid'd SIGTERM-ignoring child to init."""
    fake_claude.mode("hang", stubborn=True)
    # The launcher would wait 60 s on the SIGTERM-ignoring child; the
    # orchestrator gives it half a second.
    monkeypatch.setattr(agent_runner, "AGENT_TERMINATION_GRACE_SECONDS", 60.0)
    monkeypatch.setattr(agent_runner, "_LAUNCHER_EXIT_TIMEOUT_SECONDS", 0.5)

    async def orchestrator() -> None:
        task = asyncio.get_running_loop().create_task(
            _run_plain(3 if how == "timeout" else 120))
        deadline = time.monotonic() + 20
        while not fake_claude.spawned_ready(stubborn=True):
            assert time.monotonic() < deadline, "the fake never started"
            await asyncio.sleep(0.05)
        if how == "cancel_task":
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(orchestrator())

    assert time.monotonic() - started < 30, "waited out the launcher grace"
    stubborn = fake_claude.stubborn()
    assert not _alive(stubborn), (
        f"setsid'd child {stubborn} survived the launcher-timeout backstop")
    assert not _alive(fake_claude.watcher())


def test_orchestrator_sigkill_kills_agent_tree(fake_claude, tmp_path):
    """PT-02: PR_SET_PDEATHSIG makes the launcher clean up after a dead
    orchestrator (SIGKILL runs no Python cleanup at all)."""
    fake_claude.mode("hang", stubborn=True)
    script = tmp_path / "orchestrator.py"
    script.write_text(ORCHESTRATOR_SOURCE)
    orchestrator = subprocess.Popen(
        [sys.executable, str(script), str(REPO_ROOT)], cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_until(lambda: fake_claude.spawned_ready(stubborn=True),
                           20), "the fake agent never started"
        watcher, stubborn = fake_claude.watcher(), fake_claude.stubborn()
        assert _alive(watcher) and _alive(stubborn)
    finally:
        orchestrator.kill()
        orchestrator.wait(timeout=10)

    assert _wait_until(lambda: not _alive(watcher) and not _alive(stubborn),
                       15), "the agent tree outlived its SIGKILLed orchestrator"


def test_interpreter_exit_with_a_pending_run_kills_agent_tree(fake_claude,
                                                              tmp_path):
    """PT-02: the exit handler finishes the kill synchronously, before the
    orchestrator process is gone, including a SIGTERM-ignoring child."""
    fake_claude.mode("hang", stubborn=True)
    script = tmp_path / "orchestrator_exit.py"
    script.write_text(ORCHESTRATOR_EXIT_SOURCE)

    completed = subprocess.run(
        [sys.executable, str(script), str(REPO_ROOT),
         str(fake_claude.watcher_pidfile), str(fake_claude.stubborn_pidfile)],
        cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    watcher, stubborn = fake_claude.watcher(), fake_claude.stubborn()
    assert not _alive(watcher), f"watcher {watcher} outlived the orchestrator"
    assert not _alive(stubborn), (
        f"SIGTERM-ignoring child {stubborn} outlived the orchestrator")


# --- Launcher behaviour --------------------------------------------------------


def _launch(tmp_path: Path, *command: str, parent_pid: int | None = None,
            executable: str = "/bin/sh") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(agent_launcher.LAUNCHER_PATH),
         "--parent-pid", str(parent_pid or os.getpid()), "--grace", "1",
         "--executable", executable, "--", *command],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=30,
    )


def test_launcher_exits_with_the_cli_exit_code(tmp_path):
    assert _launch(tmp_path, "sh", "-c", "exit 7").returncode == 7


def test_launcher_reports_a_fatal_signal_like_a_direct_spawn(tmp_path):
    assert _launch(tmp_path, "sh", "-c", "kill -9 $$").returncode == -signal.SIGKILL


def test_launcher_cli_receives_termination_signals(tmp_path):
    """The launcher blocks SIGTERM for itself; the CLI must not inherit that
    mask, or a forwarded SIGTERM would never reach it."""
    completed = _launch(tmp_path, "sh", "-c", "kill -TERM $$; sleep 5; exit 3")
    assert completed.returncode == -signal.SIGTERM


def test_launcher_refuses_to_start_when_the_orchestrator_is_gone(tmp_path):
    marker = tmp_path / "ran"
    completed = _launch(tmp_path, "sh", "-c", f"touch {marker}",
                        parent_pid=os.getppid())
    assert completed.returncode == agent_launcher.EXIT_ORPHANED
    assert not marker.exists()


def test_launcher_reports_an_unstartable_command(tmp_path):
    completed = _launch(tmp_path, "nope", executable=str(tmp_path / "missing"))
    assert completed.returncode == agent_launcher.EXIT_SPAWN_FAILED
    assert "cannot start" in completed.stderr


def test_missing_cli_still_reports_command_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = asyncio.run(agent_runner.run_agent(
        ["claude", "-p", "x"], timeout=10, max_retries=1))
    assert result["success"] is False
    assert any("command not found" in error for error in result["errors"])


# --- Fail closed and never signal a recycled pid (PT-04 / PT-05) ------------


def test_launcher_not_leading_its_own_group_fails_closed(fake_claude,
                                                         monkeypatch):
    fake_claude.mode("exit")
    monkeypatch.setattr(agent_runner.os, "getpgid", lambda _pid: os.getpgrp())

    result = asyncio.run(_run_plain(30))

    assert result["success"] is False
    assert any("containment check failed" in error
               for error in result["errors"])
    assert not agent_runner._LIVE_CONTAINED_AGENTS
    if fake_claude.spawned_ready():
        assert not _alive(fake_claude.watcher())


def test_spawn_stand_in_without_a_pid_fails_closed():
    agent = agent_runner._ContainedAgent(SimpleNamespace(returncode=None))
    with pytest.raises(agent_runner.AgentContainmentError):
        agent.verify()


@pytest.fixture
def sleeper():
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    yield process
    process.kill()
    process.wait(timeout=10)


@pytest.fixture
def signals_sent(monkeypatch):
    """Record, instead of send, every signal the group kill could send."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(agent_runner.os, "killpg",
                        lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(
        agent_launcher, "signal_process_identity",
        lambda pid, _start_time, sig: sent.append((pid, sig)) or True)
    return sent


def test_group_kill_sigkills_every_live_member(sleeper):
    """The second layer: a member of the launcher's group that is still
    alive (here the stand-in leader itself) is SIGKILLed."""
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=sleeper.pid, returncode=None))
    agent.start_time = agent_launcher.proc_start_time(sleeper.pid)

    assert agent._kill_group() is False  # a member was signalled
    assert sleeper.wait(timeout=10) == -signal.SIGKILL
    assert agent._kill_group() is True  # and the group is now empty


def test_group_kill_never_uses_a_raw_group_signal(sleeper, signals_sent):
    """Every member is signalled pinned to its identity, never by killpg,
    which would reach whichever group holds the id at that instant."""
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=sleeper.pid, returncode=None))
    agent.start_time = agent_launcher.proc_start_time(sleeper.pid)

    assert agent._kill_group() is False
    assert signals_sent == [(sleeper.pid, signal.SIGKILL)]


def test_recycled_group_id_is_never_signalled(sleeper, signals_sent):
    """A leader pid now held by a different process (another start time)
    means the group emptied and the pid was reused: hands off."""
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=sleeper.pid, returncode=None))
    agent.start_time = agent_launcher.proc_start_time(sleeper.pid) - 1

    assert agent._kill_group() is True
    assert signals_sent == []


def test_group_recycled_during_member_scan_is_never_signalled(sleeper,
                                                               monkeypatch,
                                                               signals_sent):
    """The group is re-checked after the /proc member scan: an id that was
    recycled while the scan ran must not be signalled."""
    ownership = iter([True, False])
    monkeypatch.setattr(agent_runner._ContainedAgent, "_group_is_ours",
                        lambda self: next(ownership))
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=sleeper.pid, returncode=None))

    assert agent_launcher.list_group_members(sleeper.pid), "sleeper not seen"
    assert agent._kill_group() is True
    assert signals_sent == []


def test_exit_handler_in_a_forked_child_leaves_parent_agents_alone(
        monkeypatch):
    """A fork of the orchestrator inherits the registry and the atexit hook;
    its exit must not stop agents that the parent is still running."""
    stopped: list[str] = []
    agent = agent_runner._ContainedAgent(SimpleNamespace(pid=12345,
                                                         returncode=None))
    agent.owner_pid = os.getpid() + 1  # spawned by "the parent"
    monkeypatch.setattr(agent, "request_termination",
                        lambda: stopped.append("request"))
    monkeypatch.setattr(agent, "terminate_sync",
                        lambda: stopped.append("sync"))
    monkeypatch.setattr(agent_runner, "_LIVE_CONTAINED_AGENTS", {agent})

    agent_runner._terminate_live_agents_at_exit()

    assert stopped == []


def test_exit_handler_stops_agents_this_process_spawned(monkeypatch):
    stopped: list[str] = []
    agent = agent_runner._ContainedAgent(SimpleNamespace(pid=12345,
                                                         returncode=None))
    monkeypatch.setattr(agent, "request_termination",
                        lambda: stopped.append("request"))
    monkeypatch.setattr(agent, "terminate_sync",
                        lambda: stopped.append("sync"))
    monkeypatch.setattr(agent_runner, "_LIVE_CONTAINED_AGENTS", {agent})

    agent_runner._terminate_live_agents_at_exit()

    assert stopped == ["request", "sync"]


def test_own_process_group_is_never_signalled(signals_sent):
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=os.getpgrp(), returncode=None))
    agent.start_time = agent_launcher.proc_start_time(os.getpgrp())

    assert agent._kill_group() is True
    assert signals_sent == []


def test_released_agent_is_never_signalled_again(sleeper):
    agent = agent_runner._ContainedAgent(
        SimpleNamespace(pid=sleeper.pid, returncode=None))
    agent.verify()
    agent.release()

    assert agent.signal_leader(signal.SIGKILL) is False
    assert sleeper.poll() is None


def test_signal_to_a_recycled_pid_is_refused(sleeper):
    start_time = agent_launcher.proc_start_time(sleeper.pid)
    assert agent_launcher.signal_process_identity(
        sleeper.pid, start_time + 1, signal.SIGKILL) is False
    assert sleeper.poll() is None


def test_late_parent_abort_does_not_signal_a_finished_run(fake_claude,
                                                          monkeypatch):
    fake_claude.mode("exit")
    requests: list[object] = []
    monkeypatch.setattr(agent_runner, "_request_agent_termination",
                        lambda process, agent: requests.append(agent))
    parent = AbortController()

    result = asyncio.run(_run_plain(30, abort_controller=parent))
    parent.abort()

    assert result["success"] is True
    assert requests == []


# --- Non-Linux fallback ----------------------------------------------------------


def test_unsupported_platform_spawns_the_cli_directly(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = list(argv), kwargs

        async def communicate():
            return b"plain output", b""

        return SimpleNamespace(returncode=0, communicate=communicate,
                               kill=lambda: None)

    monkeypatch.setattr(agent_launcher, "is_supported_platform", lambda: False)
    monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec",
                        fake_exec)

    result = asyncio.run(agent_runner.run_agent(
        ["claude", "-p", "x"], timeout=10, max_retries=1))

    assert seen["argv"] == ["claude", "-p", "x"]
    assert "start_new_session" not in seen["kwargs"]
    assert result["success"] is True


def test_launcher_module_imports_without_posix_only_signals(monkeypatch):
    """agent_runner imports agent_launcher everywhere, including Windows,
    whose signal module has no SIGHUP, SIGCHLD, SIGPIPE or SIGXFSZ."""
    for name in ("SIGHUP", "SIGCHLD", "SIGPIPE", "SIGXFSZ"):
        monkeypatch.delattr(signal, name)
    monkeypatch.setattr(sys, "platform", "win32")
    spec = importlib.util.spec_from_file_location(
        "agent_launcher_windows_probe", agent_launcher.LAUNCHER_PATH)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    assert probe.is_supported_platform() is False
    assert probe._WAITED_SIGNALS == {signal.SIGTERM, signal.SIGINT}
