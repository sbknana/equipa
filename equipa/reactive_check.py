"""Reactive Bash security check in a separate process (review finding P2A-01).

Copyright 2026 Forgeborn

The orchestrator re-checks every Bash command an agent issues with
``equipa.bash_security.check_bash_command``. That check is regex-driven, and
CPython's ``re`` engine holds the GIL while it matches, so running it in a
worker THREAD does not protect the event loop: a command that makes one
pattern backtrack catastrophically freezes the loop that monitors every
parallel agent, and the deadline cannot fire until the match ends.

:class:`ReactiveBashChecker` runs the check in one persistent worker
PROCESS instead (this file, run as ``python -I reactive_check.py``). The
orchestrator side only waits on a pipe with ``select``, which releases the
GIL. A check that misses its deadline, a crashed worker or a malformed reply
all return None, which the caller treats as a block (fail closed); the
worker is then killed and the next check starts a fresh one.

The worker loads the checker module by FILE PATH, as the PreToolUse gate
does, so it never imports the orchestrator package and never resolves a
module through ``sys.path`` or its working directory.

Everything at module level is standard library: the worker runs isolated
(``-I``) without the repository on ``sys.path``.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import importlib.util
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).resolve()
DEFAULT_CHECKER_FILE = WORKER_PATH.with_name("bash_security.py")
DEFAULT_CHECKER_FUNC = "check_bash_command"

# Seconds a new worker gets to load the checker and report ready. Not part of
# any command's deadline; a worker that cannot start fails every check closed.
WORKER_START_TIMEOUT_SECONDS = 15.0
# Seconds to wait for a killed worker to be reaped.
_WORKER_REAP_TIMEOUT_SECONDS = 5.0
_READ_CHUNK = 65536
# A reply longer than this is not a checker verdict.
_MAX_REPLY_BYTES = 1 << 20

_PR_SET_PDEATHSIG = 1


class _WorkerFailure(Exception):
    """The worker missed its deadline, died, or answered garbage."""


class ReactiveBashChecker:
    """Runs a Bash checker in a persistent, recyclable worker process.

    Thread-safe: checks are serialised, and each command's deadline starts
    when the worker receives it, so waiting behind another agent's check
    never counts against it.
    """

    def __init__(
        self,
        checker_file: str | os.PathLike[str] = DEFAULT_CHECKER_FILE,
        checker_func: str = DEFAULT_CHECKER_FUNC,
        max_command_bytes: int | None = None,
    ) -> None:
        """
        Args:
            max_command_bytes: Size cap applied before the worker sees a
                command. None uses ``bash_security.MAX_COMMAND_BYTES``, the
                cap the checker itself enforces (IR-10).
        """
        self.checker_file = Path(checker_file).resolve()
        self.checker_func = checker_func
        self.max_command_bytes = max_command_bytes
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._owner_pid = os.getpid()
        self._buffer = b""
        self._atexit_registered = False

    async def check(self, command: str, timeout: float) -> Any | None:
        """Check ``command``; None when the worker missed ``timeout`` or failed.

        The blocking pipe exchange runs in a thread. It only blocks in
        ``select``/``os.read``, which release the GIL, so the event loop keeps
        running whatever the checker does in its own process.
        """
        import asyncio  # local: the worker side never needs asyncio

        return await asyncio.to_thread(self.check_blocking, command, timeout)

    def check_blocking(self, command: str, timeout: float) -> Any | None:
        """Synchronous :meth:`check`, for callers outside an event loop."""
        if not isinstance(command, str):
            raise TypeError(f"command must be str, not {type(command).__name__}")
        try:
            oversize = self._oversize_verdict(command)
        except (ImportError, AttributeError) as exc:
            logger.error("[BashSecurity] cannot load the command size cap; "
                         "failing closed: %s", exc)
            return None
        if oversize is not None:
            return oversize
        with self._lock:
            try:
                proc = self._ensure_worker()
                reply = self._exchange(proc, {"command": command}, timeout)
            except _WorkerFailure as exc:
                logger.error("[BashSecurity] reactive check failed closed: %s",
                             exc)
                self._discard_worker()
                return None
        return _to_result(reply)

    def _oversize_verdict(self, command: str) -> Any | None:
        """A block verdict for a command over the size cap, else None.

        The checker refuses such a command anyway (check 24), but only after
        it has been encoded, piped and decoded; a multi-megabyte command
        could miss the deadline on a loaded host and be killed as a timeout
        with a misleading reason. Refusing here is immediate and says why
        (IR-10). Still a block: the caller handles it like any flagged
        command.
        """
        from equipa.bash_security import (
            MAX_COMMAND_BYTES,
            BashSecurityResult,
            CheckID,
        )

        limit = (MAX_COMMAND_BYTES if self.max_command_bytes is None
                 else self.max_command_bytes)
        if len(command) * 4 <= limit:
            return None  # even all-4-byte UTF-8 fits: skip the encode
        size = (len(command) if len(command) > limit
                else len(command.encode("utf-8", errors="surrogatepass")))
        if size <= limit:
            return None
        logger.warning("[BashSecurity] command over the %d-byte limit blocked "
                       "before the reactive check", limit)
        return BashSecurityResult(
            safe=False, check_id=CheckID.COMMAND_TOO_LONG,
            message=(f"Command is over the {limit}-byte limit of the Bash "
                     f"security check ({size}+ bytes); blocked before the "
                     f"check ran. Write long content to a file and run a "
                     f"short command that reads it"),
        )

    def worker_pid(self) -> int | None:
        """Pid of the live worker, if any (for diagnostics and tests)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return None
        return proc.pid

    def close(self) -> None:
        """Kill the worker, if this process started one."""
        with self._lock:
            self._discard_worker()

    # -- internals (called with the lock held) --------------------------------

    def _ensure_worker(self) -> subprocess.Popen[bytes]:
        if self._owner_pid != os.getpid():
            # A forked child inherited the handle; the worker is the parent's.
            self._proc = None
            self._buffer = b""
            self._owner_pid = os.getpid()
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if self._proc is not None:
            # Died while idle (not during a check): replace it quietly.
            logger.warning("[BashSecurity] reactive check worker exited with "
                           "%s while idle; starting a new one",
                           self._proc.returncode)
            self._discard_worker()
        return self._start_worker()

    def _start_worker(self) -> subprocess.Popen[bytes]:
        argv = [
            sys.executable, "-I", str(WORKER_PATH),
            "--checker-file", str(self.checker_file),
            "--checker-func", self.checker_func,
            "--parent-pid", str(os.getpid()),
        ]
        # The worker only runs orchestrator code on command strings, but it
        # has no use for credentials, so it gets none.
        env = {"PATH": os.environ.get("PATH", os.defpath)}
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, cwd=str(WORKER_PATH.parent),
                close_fds=True,
            )
        except OSError as exc:
            raise _WorkerFailure(f"cannot start the checker worker: {exc}") from exc
        self._proc = proc
        self._buffer = b""
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True
        ready = self._read_reply(proc, time.monotonic()
                                 + WORKER_START_TIMEOUT_SECONDS)
        if ready.get("ready") is not True:
            raise _WorkerFailure(
                f"checker worker did not start: {ready.get('error', ready)!r}")
        return proc

    def _exchange(self, proc: subprocess.Popen[bytes], request: dict,
                  timeout: float) -> dict:
        deadline = time.monotonic() + max(0.0, timeout)
        payload = json.dumps(request).encode("utf-8") + b"\n"
        if proc.stdin is None:
            raise _WorkerFailure("checker worker has no stdin pipe")
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise _WorkerFailure(f"cannot send to the checker worker: {exc}") from exc
        reply = self._read_reply(proc, deadline)
        if "error" in reply:
            raise _WorkerFailure(f"checker raised: {reply['error']}")
        return reply

    def _read_reply(self, proc: subprocess.Popen[bytes], deadline: float) -> dict:
        """One JSON line from the worker, or _WorkerFailure at the deadline."""
        if proc.stdout is None:
            raise _WorkerFailure("checker worker has no stdout pipe")
        fd = proc.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerFailure("no verdict before the deadline")
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise _WorkerFailure(f"cannot wait on the worker: {exc}") from exc
            if not readable:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError as exc:
                raise _WorkerFailure(f"cannot read the worker: {exc}") from exc
            if not chunk:
                raise _WorkerFailure("checker worker exited mid-check")
            self._buffer += chunk
            if len(self._buffer) > _MAX_REPLY_BYTES:
                raise _WorkerFailure("oversized reply from the checker worker")
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise _WorkerFailure(f"malformed reply: {exc}") from exc
        if not isinstance(reply, dict):
            raise _WorkerFailure("malformed reply: not an object")
        return reply

    def _discard_worker(self) -> None:
        proc, self._proc = self._proc, None
        self._buffer = b""
        if proc is None or self._owner_pid != os.getpid():
            return
        with contextlib.suppress(OSError):
            proc.kill()
        try:
            proc.wait(timeout=_WORKER_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error("[BashSecurity] checker worker %d survived SIGKILL",
                         proc.pid)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def _to_result(reply: dict) -> Any | None:
    """Turn a worker reply into a ``BashSecurityResult``; None if malformed."""
    from equipa.bash_security import BashSecurityResult

    safe, check_id, message = (reply.get("safe"), reply.get("check_id"),
                               reply.get("message"))
    if (not isinstance(safe, bool) or not isinstance(check_id, int)
            or isinstance(check_id, bool) or not isinstance(message, str)):
        logger.error("[BashSecurity] malformed checker verdict; failing closed")
        return None
    return BashSecurityResult(safe=safe, check_id=check_id, message=message)


# --- Worker process -----------------------------------------------------------


def _load_checker(checker_file: str, checker_func: str) -> Any:
    name = "_equipa_reactive_checker"
    spec = importlib.util.spec_from_file_location(name, checker_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {checker_file}")
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses resolve annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    check = getattr(module, checker_func)
    if not callable(check):
        raise TypeError(f"{checker_func} in {checker_file} is not callable")
    return check


def _reply(stream: Any, payload: dict) -> None:
    stream.write(json.dumps(payload).encode("utf-8") + b"\n")
    stream.flush()


def _die_with_parent(parent_pid: int) -> bool:
    """Arm PDEATHSIG (Linux); False when the parent is already gone."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, AttributeError):
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(_PR_SET_PDEATHSIG, ctypes.c_ulong(signal.SIGKILL),
                       ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    return os.getppid() == parent_pid


def _worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checker-file", required=True)
    parser.add_argument("--checker-func", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args(argv)
    out = sys.stdout.buffer
    if not _die_with_parent(args.parent_pid):
        return 1
    try:
        check = _load_checker(args.checker_file, args.checker_func)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, then exit
        _reply(out, {"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _reply(out, {"ready": True})
    for line in sys.stdin.buffer:
        try:
            command = json.loads(line)["command"]
            if not isinstance(command, str):
                raise TypeError("command is not a string")
        except (ValueError, KeyError, TypeError) as exc:
            _reply(out, {"error": f"bad request: {exc}"})
            continue
        try:
            result = check(command)
            # Sent as returned, never coerced (IR-09): bool("yes") is True,
            # so a checker answering anything but a real bool must reach
            # _to_result's type check and fail closed there. A value JSON
            # cannot encode raises here and is reported as an error.
            _reply(out, {"safe": result.safe,
                         "check_id": result.check_id,
                         "message": result.message})
        except Exception as exc:  # noqa: BLE001 - any checker crash is a verdict
            _reply(out, {"error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1:]))
