"""Per-agent launcher: contains an agent CLI and everything it starts (gate-05).

Copyright 2026 Forgeborn

The orchestrator does not spawn the agent CLI directly. It spawns this module
as a small supervisor process (``python -I agent_launcher.py ... -- <cmd>``)
that starts the CLI as its child, and guarantees that nothing the CLI started
outlives it:

* ``PR_SET_CHILD_SUBREAPER``: the real Claude CLI runs every Bash-tool command
  in a NEW session (setsid), so a ``nohup watcher &`` an agent starts leaves
  the CLI's process group. Orphans reparent to the nearest subreaper ancestor
  instead of init, so every such process stays a descendant of the launcher
  no matter how often it setsid()s or double-forks.
* When the CLI exits (normally, killed, or after a forwarded SIGTERM/SIGINT)
  the launcher walks ``/proc/<pid>/task/*/children`` recursively, SIGTERMs
  every descendant, waits a grace period, SIGKILLs whatever remains and reaps
  what reparented to it. It then exits with the CLI's exit status.
* ``PR_SET_PDEATHSIG``: if the orchestrator dies (SIGKILL, OOM, an unhandled
  signal) the kernel sends the launcher SIGTERM, which runs the same cleanup.
  The escalation is done by this separate process, so it cannot be cancelled
  by the orchestrator's event-loop shutdown. The kernel ties PDEATHSIG to the
  spawning THREAD; asyncio spawns from the thread running the event loop,
  which outlives every agent it supervises.

Every signal to a descendant is pinned to the process identity recorded when
the tree was walked (pidfd plus the /proc start time), so a recycled pid is
never signalled.

This file runs in isolated mode (``python -I``) and must import only the
standard library. The /proc helpers below are also used by the orchestrator
side in ``equipa.agent_runner``.

Linux only. On any other platform ``main`` simply execs the command, which is
the orchestrator's pre-launcher behaviour; ``equipa.agent_runner`` does not
use the launcher there at all.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import signal
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

# prctl(2) option numbers from <linux/prctl.h>.
_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36

# Default seconds the CLI gets to exit after a forwarded termination signal,
# and descendants get between SIGTERM and SIGKILL.
DEFAULT_GRACE_SECONDS = 3.0
# Upper bound on the SIGKILL phase of the descendant sweep. A process that
# survives SIGKILL this long is in uninterruptible sleep or owned by another
# user (EPERM); it is reported, not waited on forever.
KILL_PHASE_TIMEOUT_SECONDS = 2.0
_POLL_SECONDS = 0.05

# Exit status when the orchestrator was already gone before the CLI started.
EXIT_ORPHANED = 128 + signal.SIGTERM
# Exit status when the command could not be started (shell convention).
EXIT_SPAWN_FAILED = 127



def _existing_signals(*names: str) -> frozenset[int]:
    """The named signals this platform has. Windows lacks most POSIX ones,
    and ``equipa.agent_runner`` imports this module on every platform."""
    return frozenset(getattr(signal, name) for name in names
                     if hasattr(signal, name))


_TERMINATION_SIGNALS = _existing_signals("SIGTERM", "SIGINT", "SIGHUP")
_WAITED_SIGNALS = _TERMINATION_SIGNALS | _existing_signals("SIGCHLD")
# Dispositions reset to default in the CLI: the launcher blocks the waited
# signals, and Python itself ignores SIGPIPE/SIGXFSZ, all of which a spawned
# child would otherwise inherit.
_CHILD_DEFAULT_SIGNALS = tuple(
    sorted(_WAITED_SIGNALS | _existing_signals("SIGPIPE", "SIGXFSZ"))
)

LAUNCHER_PATH = Path(__file__).resolve()


def is_supported_platform() -> bool:
    """True where the launcher's kernel features (prctl, /proc) exist."""
    return sys.platform.startswith("linux")


# --- /proc helpers (shared with the orchestrator) ---------------------------


def read_proc_stat(pid: int) -> tuple[str, int, int, int] | None:
    """Return ``(state, ppid, pgrp, start_time)`` for ``pid``, or None if gone.

    ``start_time`` is field 22 of /proc/<pid>/stat (clock ticks since boot).
    Together with the pid it identifies a process uniquely, which is what the
    recycling guards compare against. The command name (field 2) may contain
    spaces and parentheses, so fields are split after its LAST ')'.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii",
                                                  errors="replace")
    except OSError:
        return None
    _, _, after_comm = raw.rpartition(")")
    fields = after_comm.split()
    try:
        return fields[0], int(fields[1]), int(fields[2]), int(fields[19])
    except (IndexError, ValueError):
        return None


def proc_start_time(pid: int) -> int | None:
    """Start time of ``pid`` (see ``read_proc_stat``), or None if gone."""
    stat = read_proc_stat(pid)
    return None if stat is None else stat[3]


def _is_live(stat: tuple[str, int, int, int] | None) -> bool:
    """A zombie (state Z) has exited and can no longer act; it only awaits
    reaping, so it does not count as a live descendant or group member."""
    return stat is not None and stat[0] != "Z"


def _proc_pids() -> list[int]:
    try:
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []


def _direct_children(pid: int) -> list[int] | None:
    """Children of ``pid`` from /proc/<pid>/task/*/children.

    None when the kernel lacks CONFIG_PROC_CHILDREN (the caller then falls
    back to a full /proc scan); [] when ``pid`` has exited.
    """
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = list(task_dir.iterdir())
    except OSError:
        return []
    children: list[int] = []
    for tid in tids:
        try:
            text = (tid / "children").read_text(encoding="ascii")
        except FileNotFoundError:
            if (tid / "stat").exists():
                return None  # the thread exists but the file does not
            continue  # the thread exited while we were listing
        except OSError:
            continue
        children.extend(int(token) for token in text.split())
    return children


def list_descendants(root_pid: int) -> dict[int, int]:
    """Map every live descendant pid of ``root_pid`` to its start time.

    Walks /proc/<pid>/task/*/children recursively. The walk is a snapshot:
    a process forked afterwards is found by the next call. Zombies are left
    out (see ``_is_live``).
    """
    found: dict[int, int] = {}
    queue = [root_pid]
    while queue:
        parent = queue.pop()
        children = _direct_children(parent)
        if children is None:
            return _list_descendants_by_scan(root_pid)
        for child in children:
            if child in found:
                continue
            stat = read_proc_stat(child)
            if _is_live(stat):
                found[child] = stat[3]
            if stat is not None:
                # A zombie has no children of its own, but walking it is
                # harmless and keeps the traversal simple.
                queue.append(child)
    return found


def _list_descendants_by_scan(root_pid: int) -> dict[int, int]:
    """Fallback for kernels without /proc/<pid>/task/*/children."""
    children_of: dict[int, list[int]] = {}
    stats: dict[int, tuple[str, int, int, int]] = {}
    for pid in _proc_pids():
        stat = read_proc_stat(pid)
        if stat is None:
            continue
        stats[pid] = stat
        children_of.setdefault(stat[1], []).append(pid)
    found: dict[int, int] = {}
    queue = [root_pid]
    while queue:
        for child in children_of.get(queue.pop(), []):
            if child in found:
                continue
            if _is_live(stats[child]):
                found[child] = stats[child][3]
            queue.append(child)
    return found


def list_group_members(pgid: int) -> dict[int, int]:
    """Map every live member of process group ``pgid`` to its start time."""
    members: dict[int, int] = {}
    for pid in _proc_pids():
        stat = read_proc_stat(pid)
        if _is_live(stat) and stat[2] == pgid:
            members[pid] = stat[3]
    return members


def signal_process_identity(pid: int, start_time: int, sig: int) -> bool:
    """Send ``sig`` to ``pid`` only if it is still the process that had
    ``start_time`` when it was observed. Returns True if the signal was sent.

    With pidfd support the pidfd is opened first and the start time checked
    afterwards: the pidfd then refers to exactly the process that was
    verified, so a pid recycled in between is never signalled. Without it
    the check and ``os.kill`` leave a microsecond window, which needs the
    pid space to wrap in between.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send_signal is not None:
        try:
            pidfd = pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError:
            pidfd = None  # e.g. ENOSYS on an old kernel
        if pidfd is not None:
            try:
                if proc_start_time(pid) != start_time:
                    return False
                pidfd_send_signal(pidfd, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                _report(f"not permitted to signal pid {pid} (signal {sig})")
                return False
            finally:
                os.close(pidfd)
    if proc_start_time(pid) != start_time:
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        _report(f"not permitted to signal pid {pid} (signal {sig})")
        return False
    return True


# --- Launcher process ---------------------------------------------------------


def _report(message: str) -> None:
    """Best-effort diagnostic on stderr (the orchestrator's pipe).

    Only real anomalies are reported, because the orchestrator classifies
    agent stderr. A closed pipe (dead orchestrator) must not abort cleanup.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"agent_launcher: {message}\n")
        sys.stderr.flush()


def _prctl(option: int, value: int) -> bool:
    """Call prctl(2) through libc. False (and a report) on failure."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(option, ctypes.c_ulong(value), ctypes.c_ulong(0),
                            ctypes.c_ulong(0), ctypes.c_ulong(0))
    except (OSError, AttributeError) as exc:
        _report(f"prctl({option}) unavailable: {exc}")
        return False
    if result != 0:
        _report(f"prctl({option}, {value}) failed: "
                f"{os.strerror(ctypes.get_errno())}")
        return False
    return True


def _reap_children(cli_pid: int) -> int | None:
    """Reap every exited child without blocking.

    Returns the CLI's raw wait status once it has been reaped. Orphans that
    reparented to this subreaper are reaped too, so they do not linger as
    zombies.
    """
    cli_status: int | None = None
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return cli_status
        if pid == 0:
            return cli_status
        if pid == cli_pid:
            cli_status = status


def _wait_for_signal(timeout: float | None,
                     signals: Iterable[int] = _WAITED_SIGNALS):
    """Wait for one of the blocked ``signals``. None on timeout."""
    if timeout is None:
        return signal.sigwaitinfo(signals)
    return signal.sigtimedwait(signals, max(0.0, timeout))


def _supervise_cli(cli_pid: int, grace_seconds: float) -> int:
    """Wait for the CLI and return its raw wait status.

    A termination signal is forwarded to the CLI once; if it has not exited
    ``grace_seconds`` later it is SIGKILLed. The CLI is our own child and is
    only ever signalled before it has been reaped, so its pid cannot have
    been recycled.
    """
    kill_deadline: float | None = None
    while True:
        status = _reap_children(cli_pid)
        if status is not None:
            return status
        timeout = None
        if kill_deadline is not None:
            timeout = kill_deadline - time.monotonic()
        info = _wait_for_signal(timeout)
        if info is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(cli_pid, signal.SIGKILL)
            kill_deadline = None  # now wait for its SIGCHLD
        elif info.si_signo in _TERMINATION_SIGNALS and kill_deadline is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(cli_pid, info.si_signo)
            kill_deadline = time.monotonic() + grace_seconds


def _wait_for_no_descendants(root_pid: int, timeout: float) -> bool:
    """Reap and poll until ``root_pid`` has no live descendants.

    Grandchildren are reaped by their own parents and do not raise SIGCHLD
    here, so this polls instead of waiting on SIGCHLD alone.
    """
    deadline = time.monotonic() + timeout
    while True:
        _reap_children(-1)
        if not list_descendants(root_pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _wait_for_signal(min(remaining, _POLL_SECONDS), {signal.SIGCHLD})


def terminate_descendants(root_pid: int, grace_seconds: float,
                          kill_timeout: float = KILL_PHASE_TIMEOUT_SECONDS
                          ) -> bool:
    """SIGTERM every descendant of ``root_pid``, wait ``grace_seconds``, then
    SIGKILL the survivors until none remain or ``kill_timeout`` passes.

    Returns True when no live descendant is left. Each SIGKILL round
    re-walks the tree, so children forked during the grace period are
    caught; a killed process cannot fork again, so the rounds converge.
    """
    targets = list_descendants(root_pid)
    if not targets:
        return True
    for pid, start_time in targets.items():
        signal_process_identity(pid, start_time, signal.SIGTERM)
    if _wait_for_no_descendants(root_pid, grace_seconds):
        return True
    deadline = time.monotonic() + kill_timeout
    while True:
        targets = list_descendants(root_pid)
        if not targets:
            return True
        for pid, start_time in targets.items():
            signal_process_identity(pid, start_time, signal.SIGKILL)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _report(f"descendants survived SIGKILL: {sorted(targets)}")
            return False
        if _wait_for_no_descendants(root_pid, min(remaining, _POLL_SECONDS)):
            return True


def _exit_like(status: int) -> int:
    """Leave with the CLI's wait status: its exit code, or its fatal signal.

    Re-raising the signal (rather than exiting 128+N) keeps the orchestrator's
    ``returncode`` identical to what a directly spawned CLI produced (-N).
    """
    code = os.waitstatus_to_exitcode(status)
    if code >= 0:
        return code
    sig = -code
    if sig not in (signal.SIGKILL, signal.SIGSTOP):  # these cannot be set
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, signal.SIG_DFL)
    with contextlib.suppress(OSError, ValueError):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {sig})
    with contextlib.suppress(OSError):
        os.kill(os.getpid(), sig)
    return 128 + sig


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent_launcher",
        description="Run an agent CLI and kill everything it started.",
    )
    parser.add_argument("--parent-pid", type=int, required=True,
                        help="pid of the orchestrator that spawned us")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_SECONDS,
                        help="seconds between SIGTERM and SIGKILL")
    parser.add_argument("--executable", required=True,
                        help="resolved path of the command to run")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command and its arguments")
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("no command given after --")
    if args.parent_pid <= 1:
        parser.error("--parent-pid must be a real process id")
    if not args.grace >= 0:
        parser.error("--grace must be a non-negative number")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the launcher. Returns the exit status to leave with."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if not is_supported_platform():
        os.execv(args.executable, args.command)

    # SIGCHLD must not be ignored or children are auto-reaped and the CLI's
    # status is lost. Block the waited signals BEFORE spawning so none is
    # delivered before the loop that consumes them.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_BLOCK, _WAITED_SIGNALS)
    # Without the subreaper, setsid'd grandchildren escape the tree walk.
    # Keep going: the orchestrator's process-group kill is still a layer.
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    # The orchestrator may have died before PDEATHSIG was armed; the kernel
    # would never tell us. We were then reparented, so the ppid changed.
    if os.getppid() != args.parent_pid:
        _report("orchestrator exited before the agent started")
        return EXIT_ORPHANED

    try:
        cli_pid = os.posix_spawn(
            args.executable, args.command, os.environ,
            setsigmask=(), setsigdef=_CHILD_DEFAULT_SIGNALS,
        )
    except OSError as exc:
        _report(f"cannot start {args.executable}: {exc}")
        return EXIT_SPAWN_FAILED

    status = _supervise_cli(cli_pid, args.grace)
    terminate_descendants(os.getpid(), args.grace)
    return _exit_like(status)


if __name__ == "__main__":
    sys.exit(main())
