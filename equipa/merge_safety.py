"""Interrupt-safe merges and leftover dispatch state (task #3112, dispatch-06).

Three guarantees for the orchestrator's merge into the default branch:

* :class:`MergeSignalShield` defers SIGTERM / SIGINT while a merge runs. The
  merge then either finishes or is aborted (``git merge --abort``) before the
  signal is acted on, so a kill never leaves ``MERGE_HEAD`` behind. The
  deferred signal is recorded as a shutdown request: later merges in the run
  are refused, statuses and worktree cleanup still run, and the CLI exits
  with ``128 + signal`` at the end (:func:`exit_if_shutdown_requested`).
* :func:`main_checkout_dirty_reason` lets the merge refuse a main checkout
  with uncommitted tracked changes instead of stashing the operator's work
  (a stash that a kill between ``stash`` and ``stash pop`` used to strand).
* :func:`report_leftover_dispatch_state` lists what an earlier (or a
  concurrent) run left behind: ``.forge-worktrees/*``, ``forge-task-*``
  branches, ``MERGE_HEAD`` and EQUIPA-tagged stashes. It only reports;
  nothing is deleted.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import FrameType, TracebackType

from equipa.db import log_gate_audit
from equipa.git_ops import _is_git_repo, git_run_async

logger = logging.getLogger(__name__)

# Signals deferred while a merge runs.
SHIELDED_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

# Name of the directory that holds per-task isolation worktrees.
WORKTREE_DIR_NAME = ".forge-worktrees"

# Stash messages EQUIPA writes (``equipa-early-term task-<id> ...``) or that
# name a task branch; any of these marks a stash as left by a dispatch.
_STASH_TAGS = ("equipa", "forge-task-")

_shutdown_signal: int | None = None
_shutdown_lock = threading.Lock()


def request_shutdown(signum: int) -> None:
    """Record that the operator asked the orchestrator to stop."""
    global _shutdown_signal
    with _shutdown_lock:
        if _shutdown_signal is None:
            _shutdown_signal = signum


def shutdown_requested() -> int | None:
    """The deferred signal number, or None while no shutdown was requested."""
    return _shutdown_signal


def reset_shutdown_request() -> None:
    """Forget a recorded shutdown request (tests and long-lived callers)."""
    global _shutdown_signal
    with _shutdown_lock:
        _shutdown_signal = None


def exit_if_shutdown_requested() -> None:
    """Exit with ``128 + signal`` when a signal was deferred during a merge."""
    signum = shutdown_requested()
    if signum is None:
        return
    name = signal.Signals(signum).name
    print(
        f"[Merge-Safety] {name} was received during a merge and deferred; "
        f"the merge was finished or aborted and the run wound down. Exiting."
    )
    sys.exit(128 + signum)


async def merge_in_progress(repo: str | os.PathLike) -> bool:
    """True when ``repo`` has an unfinished merge (``MERGE_HEAD`` exists)."""
    result = await git_run_async(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], repo, timeout=10,
    )
    return result.returncode == 0


async def abort_unfinished_merge(repo: str | os.PathLike, *, context: str) -> bool:
    """Run ``git merge --abort`` if ``repo`` has an unfinished merge.

    Returns True when a merge was aborted. Failures are logged, never raised:
    this runs on the shutdown path.
    """
    try:
        if not await merge_in_progress(repo):
            return False
        aborted = await git_run_async(["merge", "--abort"], repo, timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("[Merge-Safety] could not abort the merge in %s: %s", repo, exc)
        return False
    if aborted.returncode != 0:
        logger.error(
            "[Merge-Safety] git merge --abort failed in %s: %s",
            repo, (aborted.stderr or aborted.stdout).strip()[:200],
        )
        return False
    print(f"  [Merge-Safety] Aborted an unfinished merge in {repo} ({context})")
    log_gate_audit(
        f"event=merge-aborted-on-signal repo={repo} context={context}",
        None,
        event="merge-aborted-on-signal",
    )
    return True


class MergeSignalShield:
    """Async context manager that defers SIGTERM / SIGINT around a merge.

    Inside the block both signals only record a shutdown request. On exit the
    previous handlers are restored; if a signal arrived, an unfinished merge
    left in ``repo`` is aborted so the main checkout is clean. A second
    signal after the block uses the restored handler (normally: terminate).

    Signal handlers can only be installed from the main thread. Elsewhere
    the shield is inert and says so in the log.
    """

    def __init__(self, repo: str | os.PathLike, *, context: str) -> None:
        self._repo = os.fspath(repo)
        self._context = context
        self._previous: dict[signal.Signals, object] = {}
        self.received: int | None = None

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.received is None:
            self.received = signum
        request_shutdown(signum)

    async def __aenter__(self) -> MergeSignalShield:
        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "[Merge-Safety] not on the main thread; %s runs without the "
                "signal shield", self._context,
            )
            return self
        for signum in SHIELDED_SIGNALS:
            self._previous[signum] = signal.signal(signum, self._handle)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for signum, previous in self._previous.items():
            # None means the previous handler was not installed from Python;
            # the closest restorable equivalent is the default action.
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
        self._previous.clear()
        if self.received is None:
            return
        name = signal.Signals(self.received).name
        print(
            f"  [Merge-Safety] {name} received during {self._context}; "
            f"deferred until the merge finished. No further merges this run."
        )
        await abort_unfinished_merge(self._repo, context=f"{name} during {self._context}")


async def main_checkout_dirty_reason(repo: str | os.PathLike) -> str | None:
    """Why the main checkout must not be merged into, or None when clean.

    Uncommitted changes to tracked files block the merge: the old code
    stashed them and popped the stash afterwards, which a kill in between
    stranded in ``git stash``. Untracked files are left alone — git itself
    refuses a merge that would overwrite one. Submodules are not inspected
    (``--ignore-submodules=all``), so no submodule config is executed.
    """
    status = await git_run_async(
        ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"],
        repo, timeout=30,
    )
    if status.returncode != 0:
        return (
            f"git status failed (rc={status.returncode}): "
            f"{(status.stderr or '').strip()[:200]}"
        )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if not dirty:
        return None
    return (
        f"{len(dirty)} uncommitted tracked change(s) in the main checkout, "
        f"e.g. {dirty[0].strip()!r}; commit or stash them yourself, then "
        f"re-run the merge"
    )


async def find_leftover_dispatch_state(repo: str | os.PathLike) -> list[str]:
    """Describe leftovers of earlier dispatches in ``repo``; empty when none.

    Lists ``.forge-worktrees/*`` entries, ``forge-task-*`` branches, a
    ``MERGE_HEAD`` in the main checkout and EQUIPA-tagged stashes. Read-only.
    """
    repo_path = Path(repo)
    findings: list[str] = []
    worktree_base = repo_path / WORKTREE_DIR_NAME
    if worktree_base.is_dir():
        entries = sorted(entry.name for entry in worktree_base.iterdir())
        if entries:
            findings.append(
                f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in "
                f"{WORKTREE_DIR_NAME}/: {', '.join(entries)}"
            )
    if not _is_git_repo(repo_path):
        return findings
    try:
        branches = await git_run_async(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/forge-task-*"],
            repo_path, timeout=15,
        )
        branch_names = [line.strip() for line in branches.stdout.splitlines() if line.strip()]
        if branch_names:
            findings.append(
                f"{len(branch_names)} forge-task-* branch(es): {', '.join(branch_names)}"
            )
        if await merge_in_progress(repo_path):
            findings.append("MERGE_HEAD present: an unfinished merge in the main checkout")
        stashes = await git_run_async(
            ["stash", "list", "--format=%gd %s"], repo_path, timeout=15,
        )
        tagged = [
            line.strip() for line in stashes.stdout.splitlines()
            if any(tag in line.lower() for tag in _STASH_TAGS)
        ]
        if tagged:
            findings.append(f"{len(tagged)} EQUIPA-tagged stash(es): {'; '.join(tagged)}")
    except (subprocess.SubprocessError, OSError) as exc:
        findings.append(f"could not inspect git state: {exc}")
    return findings


async def report_leftover_dispatch_state(repo: str | os.PathLike) -> list[str]:
    """Print (and audit) :func:`find_leftover_dispatch_state`; delete nothing."""
    findings = await find_leftover_dispatch_state(repo)
    if not findings:
        return findings
    print(f"  [Startup] Leftover dispatch state in {os.fspath(repo)} "
          f"(from an earlier or concurrent run; NOT deleted):")
    for finding in findings:
        print(f"  [Startup]   - {finding}")
    print("  [Startup] A leftover forge-task-<id> branch makes that task refuse to "
          "run until it is resolved by hand.")
    log_gate_audit(
        f"event=leftover-dispatch-state repo={os.fspath(repo)} "
        f"detail={' | '.join(findings)}",
        None,
        event="leftover-dispatch-state",
    )
    return findings
