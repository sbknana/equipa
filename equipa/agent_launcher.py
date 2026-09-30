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

Isolated mode (``--isolated``, feature flag ``agent_isolation``, task 3135)
------------------------------------------------------------------------------
``equipa.isolation`` starts ``agent_launcher.py --isolated`` through
``systemd-run --user --scope`` and ``sudo -u <agent user>``, so the launcher
already runs as the unprivileged agent user inside its own cgroup. Those
exact arguments are what the sudoers rule allows; everything else arrives on
stdin as a handoff (see ``_read_handoff``). Before the CLI starts the
launcher re-checks from the inside that isolation holds and refuses
otherwise (``EXIT_ISOLATION_REFUSED``): it runs as the expected user, which
is not the orchestrator's and has no privileged group; it sits in the
expected cgroup with the expected limits; it cannot read the paths the
orchestrator listed as secret or write the paths listed as protected. It
then builds the agent's own git clone from the handoff bundle and runs the
CLI there. After the CLI exits and the tree is swept, the launcher exports
the clone's state as a bundle for the orchestrator to import. The handoff
stays open as a stop channel: EOF on stdin (the orchestrator closed it or
died) is handled like SIGTERM.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

# prctl(2) option numbers from <linux/prctl.h>.
_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4
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
# Exit status of ``--isolated`` when isolation could not be established or
# verified. Nothing was started.
EXIT_ISOLATION_REFUSED = 125

# --- Isolated-mode protocol (shared with equipa.isolation) -------------------
ISOLATED_ARGV = ("--isolated",)
# First handoff line: b"EQUIPA-HANDOFF <version> <header bytes> <bundle bytes>\n"
HANDOFF_MAGIC = "EQUIPA-HANDOFF"
HANDOFF_VERSION = 1
HANDOFF_MAX_HEADER_BYTES = 64 * 1024 * 1024
_HANDOFF_MAX_PREAMBLE_BYTES = 80
# Type of the one JSON line the launcher writes to stdout before the CLI
# starts, so the orchestrator knows whether the agent runs isolated.
HANDSHAKE_TYPE = "equipa_isolation"
# The single ref of an export bundle: a "state commit" whose FIRST parent is
# the task branch tip and whose tree is the agent's working tree (tracked,
# untracked and the carried ignored paths). A second parent, if any, is HEAD.
# A branch ref equal to the bundle's excluded base would be silently dropped
# from the bundle by git, which is why the tip travels as a parent.
EXPORT_REF = "refs/equipa/worktree-state"
STATE_COMMIT_MESSAGE = "equipa: agent working-tree state (isolation export)"
_STATE_IDENTITY = {
    "GIT_AUTHOR_NAME": "EQUIPA isolation",
    "GIT_AUTHOR_EMAIL": "equipa-isolation@localhost",
    "GIT_COMMITTER_NAME": "EQUIPA isolation",
    "GIT_COMMITTER_EMAIL": "equipa-isolation@localhost",
}
_CGROUP_ROOT = Path("/sys/fs/cgroup")
# Agent-side layout under the agent user's HOME.
AGENT_STATE_DIRNAME = ".equipa-agent"
# Per-user locations the launcher points into the unit's own state directory;
# values for them in the handoff environment are never used.
_UNIT_ENV_NAMES = frozenset({
    "HOME", "CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "TMPDIR", "PWD",
})
# Everything in a unit's state directory except the clone. Removed even when
# the export fails and the clone is kept for recovery.
_UNIT_PRIVATE_ENTRIES = ("home", "gitconfig", "files", "tmp")
# Exported bundles older than this are removed from the exchange directory.
_STALE_EXPORT_SECONDS = 24 * 3600
_GIT_TIMEOUT_SECONDS = 900
# A cron or at job, or a lingering systemd user manager, runs outside the
# unit's scope, so cgroup.kill never ends it and it outlives the unit
# (review R3136-04). The agent user must be denied all three.
_LINGER_DIR = "/var/lib/systemd/linger"
_SCHEDULERS = (("crontab", ("/usr/bin/crontab", "/bin/crontab")),
               ("at", ("/usr/bin/at", "/bin/at")))
_SCHEDULER_DENIED = ("not allowed", "permission", "not permitted")
_SCHEDULER_TIMEOUT_SECONDS = 30
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNIT_RE = re.compile(r"^equipa-agent-[0-9]+-[0-9]+-[0-9a-f]{8,32}$")
_REF_RE = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


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


# --- Isolated mode (task 3135) --------------------------------------------------


class IsolationRefused(Exception):
    """Isolation does not hold or the handoff is unusable; nothing starts."""


def snapshot_worktree_state(
    run_git: Callable[[list[str], Mapping[str, str]], str],
    repo_dir: str | os.PathLike[str],
    carry_paths: Sequence[str],
    index_path: str,
    branch_ref: str,
) -> str:
    """Record a working tree as a state commit (see ``EXPORT_REF``).

    The commit's tree holds every tracked and untracked file, plus
    ``carry_paths`` even when they are ignored (review artifacts live in a
    gitignored directory). Its first parent is the tip of ``branch_ref``;
    HEAD is a second parent when it differs. A throwaway index
    (``index_path``) is used, so HEAD, the real index and every ref stay
    untouched. ``run_git(args, extra_env)`` runs git in ``repo_dir`` and
    returns its stripped stdout: the orchestrator passes its hardened
    ``git_run``, the launcher its own runner.
    """
    index_env = {"GIT_INDEX_FILE": index_path}
    run_git(["read-tree", "HEAD"], index_env)
    run_git(["add", "-A", "--", "."], index_env)
    for relative in carry_paths:
        if os.path.lexists(os.path.join(os.fspath(repo_dir), relative)):
            run_git(["add", "-A", "-f", "--", relative], index_env)
    tree = run_git(["write-tree"], index_env)
    tip = run_git(["rev-parse", "--verify", f"{branch_ref}^{{commit}}"], {})
    head = run_git(["rev-parse", "--verify", "HEAD^{commit}"], {})
    parents = ["-p", tip]
    if head != tip:
        parents += ["-p", head]
    return run_git(["commit-tree", tree, *parents, "-m", STATE_COMMIT_MESSAGE],
                   _STATE_IDENTITY)


def _emit_handshake(status: str, reason: str = "") -> None:
    """Write the one isolation status line to stdout (before the CLI runs)."""
    line = json.dumps({"type": HANDSHAKE_TYPE, "status": status,
                       "reason": reason}) + "\n"
    data = line.encode("utf-8")
    with contextlib.suppress(OSError):
        while data:
            data = data[os.write(1, data):]


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 1 << 20))
        if not chunk:
            raise IsolationRefused("the handoff ended early (the orchestrator "
                                   "closed the pipe)")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_handoff_header(fd: int) -> tuple[dict, int]:
    """Read the preamble and JSON header; return (header, bundle size).

    Unbuffered reads only: the rest of stdin is the stop channel, and a
    buffered reader could swallow its EOF.
    """
    preamble = bytearray()
    while not preamble.endswith(b"\n"):
        if len(preamble) >= _HANDOFF_MAX_PREAMBLE_BYTES:
            raise IsolationRefused("malformed handoff preamble")
        byte = os.read(fd, 1)
        if not byte:
            raise IsolationRefused("no handoff received on stdin")
        preamble += byte
    parts = preamble.decode("ascii", "replace").split()
    if (len(parts) != 4 or parts[0] != HANDOFF_MAGIC
            or parts[1] != str(HANDOFF_VERSION)
            or not parts[2].isdigit() or not parts[3].isdigit()):
        raise IsolationRefused("malformed handoff preamble")
    header_size, bundle_size = int(parts[2]), int(parts[3])
    if header_size > HANDOFF_MAX_HEADER_BYTES:
        raise IsolationRefused("handoff header too large")
    try:
        header = json.loads(_read_exact(fd, header_size).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IsolationRefused(f"handoff header is not JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise IsolationRefused("handoff header is not a JSON object")
    return header, bundle_size


def _field(mapping: Mapping, key: str, kind: type | tuple[type, ...]):
    value = mapping.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise IsolationRefused(f"handoff field {key!r} is missing or invalid")
    return value


def _str_list(mapping: Mapping, key: str) -> list[str]:
    value = _field(mapping, key, list)
    if not all(isinstance(item, str) for item in value):
        raise IsolationRefused(f"handoff field {key!r} must list strings")
    return value


def _abs_path_list(mapping: Mapping, key: str) -> list[str]:
    value = _str_list(mapping, key)
    if not all(os.path.isabs(item) for item in value):
        raise IsolationRefused(f"handoff field {key!r} must list absolute paths")
    return value


def _own_cgroup() -> str | None:
    """This process's cgroup v2 path (``0::<path>`` in /proc/self/cgroup)."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    return None


def _read_cgroup_value(cgroup: str, name: str) -> str | None:
    try:
        return (_CGROUP_ROOT / cgroup.lstrip("/") / name).read_text(
            encoding="ascii").strip()
    except OSError:
        return None


def _can_open_for_reading(path: str) -> bool:
    """True if this process can open ``path`` (file or directory) to read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _can_enter_directory(path: str) -> bool:
    """True if ``path`` is a directory this process may search (X_OK)."""
    return os.path.isdir(path) and os.access(path, os.X_OK)


def _remove_tree(path: Path) -> None:
    """rmtree that also removes entries the agent made read-only."""
    def make_writable_and_retry(function, target, _exc_info) -> None:
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(target), 0o700)
            os.chmod(target, 0o700)
        with contextlib.suppress(OSError):
            function(target)
    shutil.rmtree(path, onerror=make_writable_and_retry)


class _IsolatedSession:
    """One isolated agent run, driven by the orchestrator's handoff header."""

    def __init__(self, header: Mapping) -> None:
        self.unit = _field(header, "unit", str)
        if not _UNIT_RE.match(self.unit):
            raise IsolationRefused(f"invalid unit name {self.unit!r}")
        self.argv = _str_list(header, "argv")
        self.executable = _field(header, "executable", str)
        if not self.argv or not os.path.isabs(self.executable):
            raise IsolationRefused("the handoff has no absolute executable")
        env = _field(header, "env", dict)
        if not all(isinstance(k, str) and isinstance(v, str)
                   for k, v in env.items()):
            raise IsolationRefused("handoff env must map strings to strings")
        self.env = dict(env)
        self.files = _field(header, "files", list)
        for entry in self.files:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("index"), int)
                    or not 0 < entry["index"] < len(self.argv)
                    or not _FILE_NAME_RE.match(str(entry.get("name", "")))
                    or not isinstance(entry.get("content"), str)):
                raise IsolationRefused("invalid handoff file entry")
        self.workdir_sources = _abs_path_list(header, "workdir_sources")
        identity = _field(header, "identity", dict)
        self.user = _field(identity, "user", str)
        self.orchestrator_uid = _field(identity, "orchestrator_uid", int)
        self.privileged_groups = _str_list(identity, "privileged_groups")
        cgroup = _field(header, "cgroup", dict)
        self.cgroup_path = _field(cgroup, "path", str)
        self.limits = {name: _field(cgroup, name, int)
                       for name in ("pids_max", "memory_max", "cpu_weight")}
        self.deny_read = _abs_path_list(header, "deny_read")
        self.deny_write = _abs_path_list(header, "deny_write")
        self.must_execute = _abs_path_list(header, "must_execute")
        self.must_read = _abs_path_list(header, "must_read")
        git = _field(header, "git", dict)
        self.git_executable = _field(git, "executable", str)
        self.git_args = _str_list(git, "hardening_args")
        git_env = _field(git, "hardening_env", dict)
        self.git_env = {str(k): str(v) for k, v in git_env.items()}
        self.git_identity = {key: git[key] for key in ("user_name", "user_email")
                             if isinstance(git.get(key), str) and git[key]}
        # No workspace: a helper agent without a project directory (e.g.
        # reflexion) runs in an empty private directory; nothing is cloned
        # or exported.
        self.has_workspace = header.get("workspace") is not None
        self.handoff_ref = self.branch_ref = self.base_sha = ""
        self.export_path = ""
        self.carry_paths: list[str] = []
        if self.has_workspace:
            self._parse_workspace(_field(header, "workspace", dict))
        self.grace = float(_field(header, "grace", (int, float)))
        self.home = ""
        self.shell = "/bin/sh"
        self.state_dir: Path | None = None
        self.repo_dir: Path | None = None
        self.unit_home: Path | None = None
        self.unit_git_config: Path | None = None

    def _parse_workspace(self, workspace: Mapping) -> None:
        self.handoff_ref = _field(workspace, "handoff_ref", str)
        self.branch_ref = _field(workspace, "branch_ref", str)
        if not (_REF_RE.match(self.handoff_ref)
                and self.branch_ref.startswith("refs/heads/")
                and _REF_RE.match(self.branch_ref)):
            raise IsolationRefused("invalid handoff ref names")
        self.base_sha = _field(workspace, "base_sha", str)
        if not _SHA_RE.match(self.base_sha):
            raise IsolationRefused("invalid base commit id")
        self.export_path = _field(workspace, "export_path", str)
        self.carry_paths = _str_list(workspace, "carry_paths")
        if not os.path.isabs(self.export_path) or any(
                os.path.isabs(p) or ".." in Path(p).parts
                for p in self.carry_paths):
            raise IsolationRefused("invalid export paths")

    # -- checks ---------------------------------------------------------------

    def verify(self) -> None:
        """Every isolation property the agent's safety rests on, checked
        from the inside. The first failure refuses the run."""
        self._verify_identity()
        self._verify_no_scheduler()
        self._verify_cgroup()
        self._verify_denied_access()
        self._verify_required_access()

    def _verify_no_scheduler(self) -> None:
        """The agent user may neither linger nor use cron or at."""
        if os.path.lexists(os.path.join(_LINGER_DIR, self.user)):
            raise IsolationRefused(
                f"lingering is enabled for {self.user}, so it has a systemd "
                f"user manager outside the unit (loginctl disable-linger)")
        for name, candidates in _SCHEDULERS:
            executable = next((path for path in candidates
                               if os.access(path, os.X_OK)), None)
            if executable is None:
                continue
            try:
                result = subprocess.run(
                    [executable, "-l"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                    timeout=_SCHEDULER_TIMEOUT_SECONDS, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise IsolationRefused(
                    f"cannot check whether {self.user} may use {name}: "
                    f"{exc}") from exc
            output = (result.stdout + result.stderr).decode("utf-8", "replace")
            if result.returncode != 0 and any(
                    marker in output.lower() for marker in _SCHEDULER_DENIED):
                continue
            deny_file = "/etc/cron.deny" if name == "crontab" else "/etc/at.deny"
            raise IsolationRefused(
                f"the agent user may use {name} ({executable} -l: "
                f"{output.strip()[:200]!r}); a scheduled job would outlive "
                f"the unit - list {self.user} in {deny_file} "
                f"(docs/AGENT_ISOLATION.md step 1)")

    def _verify_identity(self) -> None:
        import grp
        import pwd

        uid = os.getuid()
        if os.geteuid() != uid or uid == 0:
            raise IsolationRefused(f"running with uid {uid}/euid "
                                   f"{os.geteuid()}, not as the agent user")
        if uid == self.orchestrator_uid:
            raise IsolationRefused("running as the orchestrator's own user")
        try:
            entry = pwd.getpwuid(uid)
        except KeyError as exc:
            raise IsolationRefused(f"uid {uid} has no passwd entry") from exc
        if entry.pw_name != self.user:
            raise IsolationRefused(f"running as {entry.pw_name!r}, expected "
                                   f"{self.user!r}")
        groups = set(os.getgroups()) | {os.getgid(), os.getegid()}
        for name in self.privileged_groups:
            try:
                gid = grp.getgrnam(name).gr_gid
            except KeyError:
                continue
            if gid in groups:
                raise IsolationRefused(f"the agent user is in the privileged "
                                       f"group {name!r}")
        # The passwd HOME is shared by every unit, so the agent must not be
        # able to write it: anything planted there (~/.ssh/config, which ssh
        # reads from passwd rather than $HOME, dot-files, caches) would reach
        # later units (review ISO-02). Units get their own HOME below it.
        if not os.path.isabs(entry.pw_dir) or not os.path.isdir(entry.pw_dir):
            raise IsolationRefused(f"agent HOME {entry.pw_dir!r} must be an "
                                   f"existing absolute directory")
        if os.access(entry.pw_dir, os.W_OK):
            raise IsolationRefused(
                f"the agent user can write its passwd HOME {entry.pw_dir}; "
                f"make it root-owned and not agent-writable "
                f"(docs/AGENT_ISOLATION.md step 1)")
        self.home = entry.pw_dir
        self.shell = next((shell for shell in ("/bin/bash", "/bin/sh")
                           if os.access(shell, os.X_OK)), "/bin/sh")

    def _verify_cgroup(self) -> None:
        own = _own_cgroup()
        if own is None or own != self.cgroup_path:
            raise IsolationRefused(f"running in cgroup {own!r}, expected "
                                   f"{self.cgroup_path!r}")
        expected = {"pids.max": self.limits["pids_max"],
                    "memory.max": self.limits["memory_max"],
                    "cpu.weight": self.limits["cpu_weight"]}
        for name, value in expected.items():
            actual = _read_cgroup_value(own, name)
            if actual != str(value):
                raise IsolationRefused(f"cgroup {name} is {actual!r}, "
                                       f"expected {value}")

    def _verify_denied_access(self) -> None:
        for path in self.deny_read:
            if _can_open_for_reading(path):
                raise IsolationRefused(f"the agent user can read {path}")
            # Search permission alone opens every world-readable file at a
            # known name inside: backups beside the TheForge DB, dot-files
            # in a 0711 HOME (reviews ISO-03, ISO-07).
            if _can_enter_directory(path):
                raise IsolationRefused(f"the agent user can enter {path}")
        for path in self.deny_write:
            if os.access(path, os.W_OK):
                raise IsolationRefused(f"the agent user can write {path}")

    def _verify_required_access(self) -> None:
        for path in [self.executable, self.git_executable, *self.must_execute]:
            if not os.access(path, os.X_OK):
                raise IsolationRefused(f"the agent user cannot execute {path}")
        for path in self.must_read:
            if not os.access(path, os.R_OK):
                raise IsolationRefused(f"the agent user cannot read {path}")

    # -- workspace ------------------------------------------------------------

    def _git(self, args: list[str], extra_env: Mapping[str, str] | None = None,
             cwd: Path | None = None) -> str:
        env = {name: value for name, value in self.build_env().items()
               if not name.endswith("_TOKEN")}
        env.update(extra_env or {})
        env.update(self.git_env)
        # The launcher's own git (clone, export) reads no global config at
        # all, not even the unit's, which the agent could edit while it ran.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        where = cwd if cwd is not None else self.repo_dir
        try:
            result = subprocess.run(
                [self.git_executable, *self.git_args, "-C", str(where), *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IsolationRefused(f"git {args[0]} failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
            raise IsolationRefused(f"git {args[0]} failed: {detail}")
        return result.stdout.decode("utf-8", "replace").strip()

    def receive_workspace(self, fd: int, bundle_size: int) -> None:
        """Create the private state directory, store the handoff bundle and
        build the agent's own clone from it."""
        root = Path(self.home) / AGENT_STATE_DIRNAME
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except PermissionError as exc:
            raise IsolationRefused(
                f"cannot create {root}; with a root-owned HOME the operator "
                f"creates it (docs/AGENT_ISOLATION.md step 1)") from exc
        root_stat = os.lstat(root)
        if not os.path.isdir(root) or os.path.islink(root) \
                or root_stat.st_uid != os.getuid():
            raise IsolationRefused(f"{root} is not a directory owned by the "
                                   f"agent user")
        os.chmod(root, 0o700)
        try:
            (root / self.unit).mkdir(mode=0o700)
        except FileExistsError as exc:
            raise IsolationRefused(f"state directory for {self.unit} already "
                                   f"exists") from exc
        self.state_dir = root / self.unit
        (self.state_dir / "files").mkdir(mode=0o700)
        (self.state_dir / "tmp").mkdir(mode=0o700)
        self.repo_dir = self.state_dir / "repo"
        self._create_unit_home()
        if not self.has_workspace:
            if bundle_size:
                raise IsolationRefused("a bundle was sent without a workspace")
            self.repo_dir.mkdir(mode=0o700)
            return
        bundle_path = self.state_dir / "handoff.bundle"
        out = os.open(bundle_path,
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600)
        try:
            remaining = bundle_size
            while remaining:
                chunk = _read_exact(fd, min(remaining, 1 << 20))
                _write_all(out, chunk)
                remaining -= len(chunk)
        finally:
            os.close(out)
        self._build_clone(bundle_path)
        os.unlink(bundle_path)
        _remove_stale_exports(Path(self.export_path).parent)

    def _create_unit_home(self) -> None:
        """A fresh, empty HOME and git global config for this unit only.

        HOME, CLAUDE_CONFIG_DIR and the XDG directories point into it (see
        build_env), so settings, hooks, CLAUDE.md, git config, shell rc
        files and caches one agent leaves behind are never loaded by a later
        developer, tester or reviewer (review ISO-02). ``discard`` removes
        it. The git global config holds only the identity the orchestrator
        handed over.
        """
        self.unit_home = self.state_dir / "home"
        self.unit_home.mkdir(mode=0o700)
        (self.unit_home / ".claude").mkdir(mode=0o700)
        self.unit_git_config = self.state_dir / "gitconfig"
        fd = os.open(self.unit_git_config,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        for key, value in self.git_identity.items():
            self._git(["config", "--file", str(self.unit_git_config),
                       key.replace("_", "."), value], cwd=self.state_dir)

    def _build_clone(self, bundle_path: Path) -> None:
        self._git(["init", "-q", str(self.repo_dir)], cwd=self.state_dir)
        self._git(["fetch", "-q", "--no-tags", "--no-write-fetch-head",
                   str(bundle_path), f"+{self.handoff_ref}:refs/equipa/handoff"])
        state = self._git(["rev-parse", "--verify", "refs/equipa/handoff^{commit}"])
        tip = self._git(["rev-parse", "--verify", f"{state}^1"])
        self._git(["update-ref", self.branch_ref, tip])
        self._git(["symbolic-ref", "HEAD", self.branch_ref])
        # The state tree is the orchestrator worktree as it was, including
        # uncommitted work of an earlier attempt: reproduce it as such.
        self._git(["read-tree", "-u", "--reset", state])
        self._git(["reset", "-q"])
        self._git(["update-ref", "-d", "refs/equipa/handoff"])
        for key, value in self.git_identity.items():
            self._git(["config", "--local", key.replace("_", "."), value])

    def _substitute(self, text: str) -> str:
        """Point every mention of the orchestrator worktree at the clone."""
        sources = sorted(set(self.workdir_sources), key=len, reverse=True)
        if not sources:
            return text
        pattern = re.compile(
            "(?:" + "|".join(re.escape(source) for source in sources) + ")"
            r"(?![A-Za-z0-9._-])")
        target = str(self.repo_dir)
        return pattern.sub(lambda _match: target, text)

    def materialize_argv(self) -> list[str]:
        """The CLI argv with worktree paths rewritten and every handed-over
        file written into the private files directory."""
        argv = [self._substitute(arg) for arg in self.argv]
        for entry in self.files:
            path = self.state_dir / "files" / entry["name"]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
            _write_all(fd, self._substitute(entry["content"]).encode("utf-8"))
            os.close(fd)
            argv[entry["index"]] = str(path)
        return argv

    def build_env(self) -> dict[str, str]:
        """The CLI's environment. Once the unit's state directory exists,
        every per-user location (HOME, CLAUDE_CONFIG_DIR, XDG directories,
        TMPDIR, the git global config) is inside it."""
        env = {name: value for name, value in self.env.items()
               if name not in _UNIT_ENV_NAMES and not name.startswith("XDG_")}
        env.update(HOME=self.home, USER=self.user, LOGNAME=self.user,
                   SHELL=self.shell)
        if self.state_dir is not None:
            home = self.unit_home
            env.update(
                HOME=str(home),
                CLAUDE_CONFIG_DIR=str(home / ".claude"),
                XDG_CONFIG_HOME=str(home / ".config"),
                XDG_CACHE_HOME=str(home / ".cache"),
                XDG_DATA_HOME=str(home / ".local" / "share"),
                XDG_STATE_HOME=str(home / ".local" / "state"),
                GIT_CONFIG_GLOBAL=str(self.unit_git_config),
                TMPDIR=str(self.state_dir / "tmp"),
                PWD=str(self.repo_dir),
            )
        return env

    def export(self) -> None:
        """Bundle the clone's state for the orchestrator (see EXPORT_REF)."""
        out = Path(self.export_path)
        partial = out.with_name(f".{out.name}.partial")
        index = self.state_dir / "export.index"
        for leftover in (index, partial):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(leftover)
        state = snapshot_worktree_state(
            lambda args, env: self._git(args, env), self.repo_dir,
            self.carry_paths, str(index), self.branch_ref)
        self._git(["update-ref", EXPORT_REF, state])
        self._git(["bundle", "create", "-q", str(partial), EXPORT_REF,
                   "--not", self.base_sha])
        os.chmod(partial, 0o644)
        os.replace(partial, out)

    def discard(self) -> None:
        if self.state_dir is not None and self.state_dir.exists():
            _remove_tree(self.state_dir)

    def discard_private(self) -> None:
        """Remove the unit's HOME, git config, handed-over files (the
        system prompt with the review nonces) and TMPDIR, keeping only the
        clone for recovery after a failed export."""
        if self.state_dir is None:
            return
        for name in _UNIT_PRIVATE_ENTRIES:
            path = self.state_dir / name
            if path.is_dir() and not path.is_symlink():
                _remove_tree(path)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _remove_stale_exports(exchange_dir: Path) -> None:
    """Best effort: drop exports nobody imported within a day."""
    now = time.time()
    with contextlib.suppress(OSError), os.scandir(exchange_dir) as entries:
        for entry in entries:
            if not entry.name.endswith((".bundle", ".partial")):
                continue
            with contextlib.suppress(OSError):
                info = entry.stat(follow_symlinks=False)
                if now - info.st_mtime > _STALE_EXPORT_SECONDS:
                    os.unlink(entry.path)


def _start_stop_channel_watcher(fd: int) -> None:
    """EOF on the rest of stdin means stop: the orchestrator closed the pipe
    to ask for termination, or it died. Delivered as SIGTERM to ourselves,
    which the supervision loop forwards to the CLI."""
    def watch() -> None:
        with contextlib.suppress(OSError):
            while os.read(fd, 4096):
                pass
        with contextlib.suppress(OSError):
            os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=watch, name="stop-channel", daemon=True).start()


def _prepare_isolated_session() -> tuple[_IsolatedSession, list[str],
                                         dict[str, str]]:
    if not _prctl(_PR_SET_DUMPABLE, 0):
        raise IsolationRefused("cannot make the launcher non-dumpable; it "
                               "holds the agent's token")
    if not _prctl(_PR_SET_CHILD_SUBREAPER, 1):
        raise IsolationRefused("cannot become a child subreaper")
    header, bundle_size = _read_handoff_header(0)
    session = _IsolatedSession(header)
    session.verify()
    try:
        session.receive_workspace(0, bundle_size)
        argv = session.materialize_argv()
    except BaseException:
        session.discard()
        raise
    return session, argv, session.build_env()


def _run_isolated() -> int:
    """``agent_launcher.py --isolated``: see the module docstring."""
    if not is_supported_platform():
        _emit_handshake("refused", "agent isolation is Linux-only")
        return EXIT_ISOLATION_REFUSED
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_BLOCK, _WAITED_SIGNALS)
    try:
        session, argv, env = _prepare_isolated_session()
    except (IsolationRefused, OSError, ValueError) as exc:
        _report(f"isolation refused: {exc}")
        _emit_handshake("refused", str(exc))
        return EXIT_ISOLATION_REFUSED

    _start_stop_channel_watcher(0)
    os.chdir(session.repo_dir)
    _emit_handshake("ready")
    try:
        cli_pid = os.posix_spawn(
            session.executable, argv, env,
            file_actions=[(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0)],
            setsigmask=(), setsigdef=_CHILD_DEFAULT_SIGNALS,
        )
    except OSError as exc:
        _report(f"cannot start {session.executable}: {exc}")
        session.discard()
        return EXIT_SPAWN_FAILED

    status = _supervise_cli(cli_pid, session.grace)
    terminate_descendants(os.getpid(), session.grace)
    try:
        if session.has_workspace:
            session.export()
    except (IsolationRefused, OSError) as exc:
        _report(f"export failed; the agent's work is kept in "
                f"{session.repo_dir}: {exc}")
        session.discard_private()
    else:
        session.discard()
    return _exit_like(status)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the launcher. Returns the exit status to leave with."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if tuple(arguments) == ISOLATED_ARGV:
        return _run_isolated()
    args = _parse_args(arguments)
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
