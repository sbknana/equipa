"""Merge integrity: merge only reviewed commits and watch the default branch.

Task #3111 (gate-01, gate-12, dispatch-03). The security gate decides whether
a task may merge. This module makes sure the merge that follows is the one the
gate approved, and that nothing else moved the default branch meanwhile:

* :func:`snapshot_reviewed_tree` records the exact commit a security reviewer
  is about to read (``HEAD`` of the task worktree) and whether the worktree
  is clean. The merge later uses that SHA, never the branch name, so a commit
  added to the branch after the review cannot ride along.
* :class:`DefaultBranchGuard` snapshots the default branch before dispatch and
  keeps an expected-SHA chain. The only legitimate movement is the
  orchestrator's own merge; any other movement trips a loud ALERT, and every
  later merge in the run is refused.
* :func:`find_repo_execution_hazards` fails closed on repository state the
  hardened git helper cannot neutralise: ``refs/replace/*`` (the reviewer
  agent's own git still honours them, so it could be shown a decoy) and local
  config that makes checkout/merge run programs or write elsewhere.

Agents share the repository's ref store and ``.git/config`` with the
orchestrator (same UID), so none of this is a sandbox. It turns silent
bypasses into loud, blocked runs.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from equipa.git_ops import (
    DefaultBranchDetectionError,
    get_trusted_default_branch,
    git_run_async,
)
from equipa.security_gate import _gate_audit_log

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15

# Local-config keys that make checkout / merge / diff run a program, or move
# the checkout somewhere else. The hardened git helper pins program-valued
# keys it can name up front, but these are keyed by an attribute-chosen
# driver name (or redirect the work tree), so they cannot be pinned away.
_HAZARDOUS_CONFIG_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^filter\..+\.(clean|smudge|process)$"), "filter driver"),
    (re.compile(r"^merge\..+\.driver$"), "merge driver"),
    (re.compile(r"^diff\..+\.(textconv|command)$"), "diff driver"),
    (re.compile(r"^core\.worktree$"), "work-tree redirect"),
    # ``--includes`` already lists what an include pulls in, under the
    # including file's scope. A conditional include is only followed when its
    # condition holds where the listing runs (onbranch:, gitdir:), which need
    # not be where the merge runs, so any local includeIf fails closed.
    (re.compile(r"^includeif\..+\.path$"), "conditional include"),
)

# Config scopes an agent can write through the shared git dir. System and
# global config belong to the operator; "command" is the helper's own -c pins.
_AGENT_WRITABLE_SCOPES = frozenset({"local", "worktree"})


class MergeIntegrityError(RuntimeError):
    """The default branch could not be pinned before dispatch."""


# Gate statuses after which a task counts as done: its approved commit is on
# the default branch (merged), or was already there (noop — the branch had
# nothing to merge, the degenerate docs-only change).
MERGE_DONE_STATUSES = frozenset({"merged", "noop"})


@dataclass
class MergeAttempt:
    """What one ``dispatch._merge_task_branch`` call actually did.

    ``merged_sha`` is the task commit now on the default branch: the approved
    commit itself, or its rebased copy when the rebase fallback was used.
    """

    merged_sha: str | None = None
    pre_head: str | None = None
    post_head: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class MergeOutcome:
    """Final gate result for one task, kept on the run's guard.

    ``status`` is the ``_gated_merge_task`` return value; ``merged_sha`` is
    set for ``merged`` / ``noop`` so a ``done`` task can be checked with
    ``git merge-base --is-ancestor <merged_sha> <default>``.
    """

    status: str
    reason: str = ""
    merged_sha: str | None = None

    @property
    def done(self) -> bool:
        return self.status in MERGE_DONE_STATUSES and bool(self.merged_sha)


def _short(sha: str | None) -> str:
    return (sha or "none")[:12]


async def resolve_commit(repo: str | os.PathLike, ref: str) -> str | None:
    """Full commit SHA ``ref`` names in ``repo``, or None if it does not exist."""
    result = await git_run_async(
        ["rev-parse", "--verify", "--quiet", "--end-of-options",
         f"{ref}^{{commit}}"],
        repo, timeout=_GIT_TIMEOUT,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


async def is_ancestor(repo: str | os.PathLike, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``."""
    result = await git_run_async(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        repo, timeout=_GIT_TIMEOUT,
    )
    return result.returncode == 0


async def commit_parents(repo: str | os.PathLike, sha: str) -> list[str] | None:
    """Parent SHAs of ``sha`` in order, or None when git cannot tell."""
    result = await git_run_async(
        ["rev-list", "--parents", "-n", "1", sha], repo, timeout=_GIT_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[1:]


async def find_repo_execution_hazards(repo: str | os.PathLike) -> list[str]:
    """Repository state that makes an orchestrator merge unsafe.

    Returns one human-readable reason per hazard, empty when the repo is safe
    to gate and merge. A git failure while checking is itself a hazard: this
    check fails closed.

    * ``refs/replace/*`` — the orchestrator's git ignores replace refs, but
      the security-reviewer agent's git does not, so it can review a decoy.
    * agent-writable (local / worktree) config, including files pulled in via
      ``include`` / ``includeIf``, that defines a filter, merge or diff driver
      program, or ``core.worktree``.
    """
    hazards: list[str] = []
    replace_refs = await git_run_async(
        ["for-each-ref", "--format=%(refname)", "refs/replace/"],
        repo, timeout=_GIT_TIMEOUT,
    )
    if replace_refs.returncode != 0:
        hazards.append(
            f"could not list refs/replace/ (rc={replace_refs.returncode}: "
            f"{replace_refs.stderr.strip()[:200]})"
        )
    else:
        names = replace_refs.stdout.split()
        if names:
            shown = ", ".join(names[:3]) + (" ..." if len(names) > 3 else "")
            hazards.append(
                f"replace refs present ({len(names)}: {shown}); the reviewer "
                f"agent would see replaced objects"
            )

    config = await git_run_async(
        ["config", "--list", "--includes", "--show-scope", "--name-only"],
        repo, timeout=_GIT_TIMEOUT,
    )
    if config.returncode != 0:
        hazards.append(
            f"could not read git config (rc={config.returncode}: "
            f"{config.stderr.strip()[:200]})"
        )
        return hazards
    for line in config.stdout.splitlines():
        scope, _, key = line.partition("\t")
        if scope not in _AGENT_WRITABLE_SCOPES:
            continue
        for pattern, label in _HAZARDOUS_CONFIG_KEYS:
            if pattern.match(key.lower()):
                hazards.append(f"{scope} config defines {label} '{key}'")
                break
    return hazards


@dataclass(frozen=True)
class TreeSnapshot:
    """What a reviewer is (or was) looking at in a task worktree."""

    sha: str | None
    branch: str | None
    clean: bool
    detail: str = ""

    def describe(self) -> str:
        return (
            f"reviewed_sha={_short(self.sha)} branch={self.branch or 'DETACHED'} "
            f"clean={self.clean}"
            + (f" detail={self.detail}" if self.detail else "")
        )


async def snapshot_reviewed_tree(worktree_dir: str | os.PathLike) -> TreeSnapshot:
    """Record HEAD of ``worktree_dir`` and whether its tracked files are clean.

    "Clean" means no staged or unstaged change to a tracked file: the reviewer
    then reads exactly the tree of ``sha``. Untracked files are allowed — they
    are never merged, so they cannot make the merge differ from the review.
    Repository hazards are checked first (``git status`` could otherwise run
    an agent-configured filter); any hazard makes the snapshot unclean.
    """
    sha = await resolve_commit(worktree_dir, "HEAD")
    branch_res = await git_run_async(
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        worktree_dir, timeout=_GIT_TIMEOUT,
    )
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else None
    if sha is None:
        return TreeSnapshot(None, branch, False, "HEAD does not resolve")
    hazards = await find_repo_execution_hazards(worktree_dir)
    if hazards:
        return TreeSnapshot(sha, branch, False, "; ".join(hazards))
    status = await git_run_async(
        ["status", "--porcelain", "--untracked-files=no"],
        worktree_dir, timeout=30,
    )
    if status.returncode != 0:
        return TreeSnapshot(
            sha, branch, False,
            f"git status failed rc={status.returncode}: "
            f"{status.stderr.strip()[:200]}",
        )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        return TreeSnapshot(
            sha, branch, False,
            f"{len(dirty)} uncommitted tracked change(s), e.g. {dirty[0].strip()}",
        )
    return TreeSnapshot(sha, branch, True)


def reviewed_commit_refusal(record, branch_sha: str | None) -> str | None:
    """Why the reviewed commit must not be merged, or None when it may be.

    ``record`` is the task's ``security_gate.ReviewerRunRecord``. The merge is
    allowed only when the reviewer started and finished on the same clean
    commit and the task branch still points at it.
    """
    if not record.reviewed_sha:
        return "reviewed SHA not recorded at reviewer start"
    if not record.reviewed_tree_clean:
        return (
            f"worktree was not clean when the review started "
            f"({record.reviewed_tree_detail or 'unknown'})"
        )
    if record.reviewed_sha_end != record.reviewed_sha:
        return (
            f"branch moved during review ({_short(record.reviewed_sha)} -> "
            f"{_short(record.reviewed_sha_end)})"
        )
    if branch_sha != record.reviewed_sha:
        return (
            f"branch moved after review (reviewed {_short(record.reviewed_sha)}, "
            f"branch now {_short(branch_sha)})"
        )
    return None


@dataclass
class DefaultBranchGuard:
    """Expected-SHA chain for the default branch over one dispatch run.

    ``expected_sha`` starts at the pre-dispatch snapshot and advances only
    through :meth:`record_merge` (the orchestrator's own merge). Any other
    movement trips the guard: :attr:`alert` is set, an ALERT naming both SHAs
    is printed and audited, and every later :meth:`verify` returns False so
    the caller merges nothing further.
    """

    project_dir: str
    default_branch: str
    baseline_sha: str
    expected_sha: str
    alert: str | None = None
    merges: list[tuple[int, str, str]] = field(default_factory=list)
    # Per-task gate result for this run, written by dispatch._gated_merge_task.
    outcomes: dict[int, MergeOutcome] = field(default_factory=dict)

    @classmethod
    async def snapshot(cls, project_dir: str | os.PathLike) -> DefaultBranchGuard:
        """Pin the trusted default branch's current SHA.

        Raises :class:`MergeIntegrityError` when the default branch cannot be
        trusted or resolved: a run that cannot be watched must not merge.
        """
        repo = os.fspath(project_dir)
        try:
            default_branch = get_trusted_default_branch(repo)
        except DefaultBranchDetectionError as exc:
            raise MergeIntegrityError(
                f"no trusted default branch in {repo}: {exc}"
            ) from exc
        sha = await resolve_commit(repo, f"refs/heads/{default_branch}")
        if sha is None:
            raise MergeIntegrityError(
                f"default branch '{default_branch}' does not resolve in {repo}"
            )
        return cls(repo, default_branch, sha, sha)

    @property
    def tripped(self) -> bool:
        return self.alert is not None

    async def current_sha(self) -> str | None:
        return await resolve_commit(
            self.project_dir, f"refs/heads/{self.default_branch}",
        )

    async def verify(self, stage: str, *, task_id: int | None = None) -> bool:
        """True while the default branch is still at the expected SHA."""
        if self.tripped:
            return False
        current = await self.current_sha()
        if current == self.expected_sha:
            return True
        self.trip(stage, current, task_id=task_id)
        return False

    async def record_merge(self, task_id: int, merged_sha: str) -> bool:
        """Advance the chain after the orchestrator merged ``merged_sha``.

        The new tip must be exactly the orchestrator's merge: either a merge
        commit whose parents are (expected, merged_sha), or a fast-forward to
        ``merged_sha`` from a descendant of ``expected``. Anything else means
        another writer moved the branch around the merge, and trips the guard.
        """
        if self.tripped:
            return False
        current = await self.current_sha()
        previous = self.expected_sha
        legitimate = False
        if current is not None and current != previous:
            if current == merged_sha:
                legitimate = await is_ancestor(self.project_dir, previous, current)
            else:
                parents = await commit_parents(self.project_dir, current)
                legitimate = parents == [previous, merged_sha]
        if not legitimate:
            self.trip(f"post-merge task={task_id}", current, task_id=task_id)
            return False
        self.expected_sha = current
        self.merges.append((task_id, previous, current))
        _gate_audit_log(
            f"task={task_id} event=default-branch-advanced "
            f"branch={self.default_branch} before={_short(previous)} "
            f"after={_short(current)} merged_sha={_short(merged_sha)}",
            task_id=task_id,
            event="default-branch-advanced",
        )
        return True

    def trip(self, stage: str, actual: str | None, *, task_id: int | None = None) -> None:
        """Record an unexpected default-branch movement and raise the alarm."""
        if self.tripped:
            return
        self.alert = (
            f"default branch '{self.default_branch}' moved outside the "
            f"orchestrator's merges (stage={stage}): expected "
            f"{self.expected_sha} but found {actual or 'MISSING'}"
        )
        banner = "!" * 72
        print(f"\n{banner}")
        print(f"  [Merge-Integrity] ALERT: {self.alert}")
        print(
            f"  [Merge-Integrity] ALERT: baseline {self.baseline_sha}; "
            f"orchestrator merges this run: {len(self.merges)}. Nothing further "
            f"is merged and the affected tasks are left blocked."
        )
        print(banner)
        logger.error("[Merge-Integrity] ALERT: %s", self.alert)
        _gate_audit_log(
            f"task={task_id if task_id is not None else '-'} "
            f"event=default-branch-moved branch={self.default_branch} "
            f"stage={stage} expected={self.expected_sha} "
            f"actual={actual or 'MISSING'} baseline={self.baseline_sha}",
            task_id=task_id,
            event="default-branch-moved",
        )
