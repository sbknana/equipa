"""EQUIPA dispatch module — auto-run scanning, scoring, filtering, and execution.

Extracts dispatch/auto-run logic from forge_orchestrator.py (Phase 5 split).
Includes: scan_pending_work, score_project, apply_dispatch_filters,
run_project_tasks, run_project_dispatch, run_auto_dispatch,
run_parallel_tasks, run_single_goal, run_parallel_goals,
parse_task_ids, load_goals_file, validate_goals.

Feature-flag and dispatch-config primitives now live in equipa.config
(layer 2). They are re-exported here for backward compatibility with
existing `from equipa.dispatch import is_feature_enabled` callers.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger(__name__)

from equipa.agent_runner import OVERLOADED_OUTCOME, is_overloaded_result
from equipa.config import (
    DEFAULT_DISPATCH_CONFIG,
    DEFAULT_FEATURE_FLAGS,
    is_feature_enabled,
    is_security_review_enabled,
    load_dispatch_config,
)
import equipa.constants as _equipa_constants
from equipa.constants import (
    ATTEMPT_REFLECTIONS_MAX_CHARS,
    ATTEMPT_SECTION_TRIM_CHARS,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    MAX_MANAGER_ROUNDS,
    MAX_TASK_RANGE,
    PRIORITY_ORDER,
)
from equipa.db import (
    db_conn,
    get_db_connection,
    log_gate_audit,
    record_agent_run,
    update_task_status,
)
from equipa.hooks import fire_async as fire_hook
from equipa.git_ops import (
    GitRepositoryUnreadableError,
    PinnedGitRepository,
    PinnedRepositoryError,
    UntrustedDefaultBranchError,
    _is_git_repo,
    check_pinned_directory,
    fd_pinning_available,
    get_default_branch,
    get_trusted_default_branch,
    git_repositories_pinned,
    git_run,
    git_run_async,
    git_toplevel,
    git_toplevel_async,
    open_pinned_directory,
    pinned_repository,
)
from equipa.generated_files import ConflictResolution, resolve_generated_conflicts
from equipa.isolation import concurrency_refusal
from equipa.lessons import update_injected_episode_q_values_for_task
from equipa.merge_safety import (
    MergeSignalShield,
    main_checkout_dirty_reason,
    report_leftover_dispatch_state,
    shutdown_requested,
)
from equipa.merge_integrity import (
    DefaultBranchGuard,
    MergeAttempt,
    MergeIntegrityError,
    MergeOutcome,
    RepositoryIdentity,
    find_repo_execution_hazards,
    is_ancestor,
    rebased_range_problem,
    resolve_commit,
    reviewed_commit_refusal,
)
from equipa.loops import (
    _count_findings_in_review_file,
    ensure_artifacts_dir,
    find_review_artifact,
    review_artifact_path,
    run_dev_test_loop,
    run_quality_scoring,
    run_security_review,
)
from equipa.manager import GOAL_REFUSED_OUTCOMES, run_manager_loop
from equipa.parsing import _extract_section
from equipa.output import (
    log,
    print_dispatch_plan,
    print_dispatch_summary,
    print_manager_summary,
    print_parallel_summary,
)
from equipa.prompts import build_planner_prompt
from equipa.reflexion import maybe_run_reflexion
from equipa.roles import get_role_model, get_role_turns
from equipa.routing import CircuitOpenError
from equipa.security_gate import (
    GateDecision,
    SecurityGateBypassError,
    _gate_audit_log,
    decide_merge_gate,
    describe_submodule_pointer_changes,
    format_counts,
    get_changed_files_for_branch,
    get_reviewer_run,
    is_doc_only_diff,
    record_reviewer_skipped_doc_only,
    reviewer_run_failure,
    unrecorded_reviewer_runs_permitted,
    verify_reviewer_provenance,
)
from equipa.single_agent_guard import (
    SingleAgentOutcome,
    TasksCreatedValidation,
    evaluate_single_agent_outcome,
    validate_tasks_created_claim,
)
from equipa.tasks import (
    fetch_project_context,
    fetch_project_info,
    fetch_task,
    fetch_tasks_by_ids,
    resolve_project_dir,
)


__all_reexports__ = (
    "DEFAULT_FEATURE_FLAGS",
    "DEFAULT_DISPATCH_CONFIG",
    "is_feature_enabled",
    "load_dispatch_config",
)


def _bootstrap_scaffold_if_needed(task: dict, project_id: int | None) -> str | None:
    """Resolve a scaffold-based project's local_path even when the dir is empty.

    ``resolve_project_dir`` returns ``None`` when the recorded ``local_path``
    does not point at an existing directory. For scaffold-based projects
    that is exactly the case auto-clone is supposed to recover from: the
    project row has a ``local_path`` set, the directory has not yet been
    created, and we should create it and copy ForgeScaffold in. This
    helper returns the candidate path (after creating it) so the calling
    code can hand it to ``ensure_scaffold``.
    """
    if not project_id:
        return None
    try:
        from equipa.db import db_conn
        from equipa.scaffold import is_scaffold_project
        if not is_scaffold_project(project_id):
            return None
        with db_conn() as conn:
            row = conn.execute(
                "SELECT local_path FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
    except Exception:  # pragma: no cover - defensive
        return None
    if not row:
        return None
    try:
        local_path = row["local_path"]
    except (KeyError, IndexError):
        local_path = None
    if not local_path:
        return None
    # Translate Windows-style paths to the Samba mount, mirroring
    # ``tasks.resolve_project_dir``.
    candidate = local_path.rstrip("/").rstrip("\\")
    if candidate.startswith(("Z:\\AI_Stuff", "Z:/AI_Stuff")):
        candidate = (
            "/srv/forge-share/AI_Stuff"
            + candidate[len("Z:\\AI_Stuff"):].replace("\\", "/")
        )
    # Containment check: a DB-supplied ``local_path`` is untrusted input.
    # Without this, a value like ``Z:\AI_Stuff\..\..\etc\evil`` translates to
    # ``/srv/forge-share/AI_Stuff/../../etc/evil`` and ``mkdir(parents=True)``
    # would silently create ``/etc/evil``. Reject ``..`` segments and require
    # the resolved path to live inside an allowlisted root.
    try:
        from equipa.scaffold import assert_contained_path, ScaffoldCloneError
        safe_path = assert_contained_path(candidate)
    except Exception as exc:  # ScaffoldCloneError or import failure
        logger.warning(
            "scaffold bootstrap: refusing unsafe local_path %r: %s",
            candidate,
            exc,
        )
        return None
    try:
        safe_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(safe_path)


# --- Cross-Attempt Memory Helpers ---

_ATTEMPT_MARKER = "\n\n--- PREVIOUS ATTEMPTS ---\n"


def _build_dispatch_attempt_reflection(
    attempt: int,
    outcome: str,
    cycles: int,
    result: dict,
) -> str:
    """Build a concise reflection from a failed dev-test loop attempt.

    Extracts key failure signals from the loop result and outcome to help
    the next attempt avoid repeating the same mistakes.

    Args:
        attempt: 1-based attempt number
        outcome: Outcome string from run_dev_test_loop (e.g. "cycles_exhausted")
        cycles: Number of dev-test cycles completed
        result: Result dict from the dev-test loop

    Returns:
        A concise reflection string (<300 chars).
    """
    duration = result.get("duration", 0)
    cost = result.get("cost", 0)

    # Determine failure category
    if outcome == "cycles_exhausted":
        reason = f"exhausted {cycles} dev-test cycles without passing tests"
    elif outcome == "cost_limit":
        reason = f"hit cost limit (${cost:.2f}) after {cycles} cycle(s)"
    elif outcome == "early_completed_blocked":
        reason = "agent reported blocked"
    elif outcome == "loop_detected":
        reason = "loop detected — agent kept trying the same approach"
    elif outcome == "tests_inconclusive":
        # Task #2242: tester reported "pass" but every test was skipped
        # (typically missing env vars). The agent must either set up the
        # env vars on the test runner OR rewrite the tests to run without
        # them — a vanilla retry without that signal will reproduce the
        # exact same skip pattern.
        reason = (
            "tests_inconclusive — every test was skipped (likely missing "
            "env vars or unmet prerequisites). Real validation did NOT "
            "occur. Either provide the required env defaults so the tests "
            "execute, or rewrite the tests to exercise the code paths "
            "without env gating."
        )
    else:
        reason = outcome

    # Extract structured fields from agent output if available
    raw_output = result.get("raw_output", "")
    files_info = ""
    blockers_info = ""
    reflection_info = ""

    if raw_output:
        files_text = _extract_section(raw_output, "FILES_CHANGED")
        if files_text and "none" not in files_text.lower():
            # Strip the marker prefix
            files_text = files_text.replace("FILES_CHANGED:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if files_text:
                files_info = f"\n  Files touched: {files_text}"

        blockers_text = _extract_section(raw_output, "BLOCKERS")
        if blockers_text and "none" not in blockers_text.lower():
            blockers_text = blockers_text.replace("BLOCKERS:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if blockers_text:
                blockers_info = f"\n  Blockers: {blockers_text}"

        reflection_text = _extract_section(raw_output, "REFLECTION", max_lines=3)
        if reflection_text:
            reflection_text = reflection_text.replace("REFLECTION:", "").strip()[
                :ATTEMPT_SECTION_TRIM_CHARS
            ]
            if reflection_text:
                reflection_info = f"\n  Agent reflection: {reflection_text}"

    parts = [
        f"ATTEMPT {attempt} FAILED ({reason}, {cycles} cycles, {duration:.0f}s):",
    ]
    if files_info:
        parts.append(files_info)
    if blockers_info:
        parts.append(blockers_info)
    if reflection_info:
        parts.append(reflection_info)
    parts.append("  DO NOT repeat this approach. Try a different strategy.")

    return "\n".join(parts)


def _inject_attempt_reflections(
    conn: object,
    task_id: int,
    reflections: list[str],
) -> None:
    """Inject accumulated attempt reflections into a task's description.

    Appends a PREVIOUS ATTEMPTS block to the task description so the next
    agent attempt knows what was already tried and what failed.

    Args:
        conn: SQLite connection (caller manages commit)
        task_id: Task ID to update
        reflections: List of reflection strings from prior attempts
    """
    cur = conn.execute(  # type: ignore[union-attr]
        "SELECT description FROM tasks WHERE id = ?", (task_id,)
    )
    row = cur.fetchone()
    if not row:
        return

    desc = row[0] or ""

    # Strip any existing reflection block to avoid unbounded growth
    if _ATTEMPT_MARKER in desc:
        desc = desc[: desc.index(_ATTEMPT_MARKER)]

    # Build and append the new block
    reflections_block = "\n\n".join(reflections)

    # Enforce token budget (~500 tokens ≈ ~2000 chars)
    if len(reflections_block) > ATTEMPT_REFLECTIONS_MAX_CHARS:
        reflections_block = (
            reflections_block[:ATTEMPT_REFLECTIONS_MAX_CHARS]
            + "\n[...earlier attempts trimmed...]"
        )

    desc += _ATTEMPT_MARKER + reflections_block

    conn.execute(  # type: ignore[union-attr]
        "UPDATE tasks SET description = ? WHERE id = ?", (desc, task_id)
    )


class AttemptCleanupError(RuntimeError):
    """The git reset between two autoresearch attempts failed.

    Raised instead of logging a warning and carrying on: a half-finished
    reset leaves the next attempt on the wrong branch or on the failed
    attempt's commits (dispatch-02/12). The caller must stop retrying and
    leave the task blocked.
    """


async def _git_checked(
    args: list[str],
    cwd: str,
    *,
    timeout: int,
    action: str,
) -> str:
    """Run git and return its stripped stdout; raise if it did not succeed.

    Raises:
        AttemptCleanupError: git exited non-zero, timed out, or could not be
            started. ``action`` names the step in the error message.
    """
    try:
        result = await git_run_async(args, cwd, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise AttemptCleanupError(f"{action} in {cwd}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise AttemptCleanupError(
            f"{action} in {cwd} failed (rc={result.returncode}): {detail}"
        )
    return (result.stdout or "").strip()


async def _current_branch(cwd: str) -> str | None:
    """Short name of the branch checked out in ``cwd``; None when detached.

    Raises:
        AttemptCleanupError: git could not be run in ``cwd``.
    """
    try:
        result = await git_run_async(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise AttemptCleanupError(f"read HEAD in {cwd}: {exc}") from exc
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise AttemptCleanupError(
            f"read HEAD in {cwd} failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:300]}"
        )
    return (result.stdout or "").strip() or None


async def _is_linked_worktree(cwd: str) -> bool:
    """True when ``cwd`` is a linked worktree rather than the main checkout.

    A linked worktree has its own git dir under the shared common dir.
    """
    dirs = await _git_checked(
        ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        cwd, timeout=10, action="locate git dirs",
    )
    lines = dirs.splitlines()
    if len(lines) != 2:
        raise AttemptCleanupError(f"unexpected rev-parse output in {cwd}: {dirs!r}")
    return Path(lines[0]).resolve() != Path(lines[1]).resolve()


async def _worktrees_holding_branch(cwd: str, branch_name: str) -> list[str]:
    """Paths of every worktree (main checkout included) on ``branch_name``."""
    listing = await _git_checked(
        ["worktree", "list", "--porcelain"], cwd, timeout=10,
        action="list worktrees",
    )
    holders: list[str] = []
    current_path: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch_name}" and current_path:
            holders.append(current_path)
    return holders


async def _reset_task_worktree(
    worktree_dir: str,
    branch_name: str,
    base_sha: str | None,
    emit: Callable[[str], None],
) -> None:
    """Discard a failed attempt inside its own worktree, staying on its branch.

    The worktree never leaves ``branch_name``: checking out the default
    branch here would let the next attempt commit straight onto it, and the
    branch cannot be deleted while this worktree holds it (dispatch-02).
    """
    current = await _current_branch(worktree_dir)
    if current != branch_name:
        raise AttemptCleanupError(
            f"worktree {worktree_dir} is on {current or 'a detached HEAD'!r}, "
            f"not {branch_name!r}; refusing to reset it"
        )
    if not base_sha:
        # No recorded base: fall back to the fork point from the operator's
        # default branch, never the checked-out HEAD or origin/HEAD.
        try:
            default_branch = get_trusted_default_branch(worktree_dir)
        except UntrustedDefaultBranchError as exc:
            raise AttemptCleanupError(str(exc)) from exc
        base_sha = await _git_checked(
            ["merge-base", "HEAD", f"refs/heads/{default_branch}"],
            worktree_dir, timeout=10,
            action=f"find the fork point of {branch_name} from {default_branch}",
        )
    failed_head = await _git_checked(
        ["rev-parse", "--verify", "HEAD^{commit}"], worktree_dir, timeout=10,
        action="read the failed attempt's HEAD",
    )
    await _git_checked(
        ["reset", "--hard", base_sha], worktree_dir, timeout=60,
        action=f"reset {branch_name} to {base_sha[:12]}",
    )
    await _git_checked(
        ["clean", "-fd"], worktree_dir, timeout=60,
        action=f"clean untracked files from {branch_name}",
    )
    after_branch = await _current_branch(worktree_dir)
    after_head = await _git_checked(
        ["rev-parse", "--verify", "HEAD^{commit}"], worktree_dir, timeout=10,
        action="read HEAD after the reset",
    )
    if after_branch != branch_name or after_head != base_sha:
        raise AttemptCleanupError(
            f"worktree {worktree_dir} ended on {after_branch!r}@{after_head[:12]} "
            f"after the reset, expected {branch_name!r}@{base_sha[:12]}"
        )
    emit(
        f"  [Autoresearch] Reset worktree branch {branch_name} "
        f"{failed_head[:12]} -> {base_sha[:12]} (failed attempt recoverable "
        f"via reflog)"
    )


async def _delete_task_branch_in_main_checkout(
    project_dir: str,
    branch_name: str,
    emit: Callable[[str], None],
) -> None:
    """Drop a failed attempt's branch when the agent ran in the main checkout.

    Leaves ``branch_name`` for the operator-trusted default branch only if
    the main checkout is on it, and refuses to delete the branch while any
    worktree still holds it.
    """
    try:
        default_branch = get_trusted_default_branch(project_dir)
    except UntrustedDefaultBranchError as exc:
        raise AttemptCleanupError(str(exc)) from exc
    if await _current_branch(project_dir) == branch_name:
        await _git_checked(
            ["checkout", default_branch], project_dir, timeout=30,
            action=f"check out {default_branch} to leave {branch_name}",
        )
    try:
        exists = await git_run_async(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            project_dir, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise AttemptCleanupError(f"look up {branch_name}: {exc}") from exc
    if exists.returncode != 0:
        emit(f"  [Autoresearch] Branch {branch_name} does not exist; nothing to delete")
        return
    holders = await _worktrees_holding_branch(project_dir, branch_name)
    if holders:
        raise AttemptCleanupError(
            f"{branch_name} is checked out in {', '.join(holders)}; "
            f"refusing to delete it"
        )
    failed_head = (exists.stdout or "").strip()
    await _git_checked(
        ["branch", "-D", branch_name], project_dir, timeout=10,
        action=f"delete {branch_name}",
    )
    emit(
        f"  [Autoresearch] Cleaned up branch {branch_name} (was "
        f"{failed_head[:12]}, recoverable via reflog)"
    )


async def cleanup_failed_attempt(
    task_id: int,
    project_dir: str,
    reflections: list[str],
    output: list[str] | None = None,
    *,
    base_sha: str | None = None,
) -> None:
    """Reset a failed task for a fresh autoresearch attempt.

    Shared by the parallel/auto dispatch path and the single-task ``--task``
    CLI path. The git step depends on where the agent ran:

    * ``project_dir`` is the task's isolation worktree: reset it with
      ``reset --hard <base_sha>`` plus ``clean -fd`` and keep it on
      ``forge-task-<id>``. The default branch is never checked out there
      and the branch is never deleted. Without ``base_sha`` the fork point
      from the trusted default branch is used.
    * ``project_dir`` is the main checkout: leave ``forge-task-<id>`` for the
      trusted default branch if it is checked out, then delete it unless a
      worktree still holds it.

    Every git step is checked. On failure :class:`AttemptCleanupError` is
    raised BEFORE the task is reset to ``todo``; the task must not be
    retried on top of a half-finished reset.

    After the git step, the task's status is reset to ``todo`` and any
    accumulated cross-attempt reflections are injected so the next attempt
    remembers what was already tried.

    Args:
        task_id: Task ID being retried.
        project_dir: Worktree or main checkout the attempt ran in.
        reflections: Reflection strings from prior failed attempts. Empty
            list is allowed (skips reflection injection).
        output: Optional buffer for ``log()`` calls; if ``None``, prints.
        base_sha: Commit the task worktree was created on.

    Raises:
        AttemptCleanupError: a git step failed or the worktree is not on
            ``forge-task-<id>``.
    """
    branch_name = f"forge-task-{task_id}"

    def emit(message: str) -> None:
        if output is not None:
            log(message, output)
        else:
            print(message)

    try:
        is_git = _is_git_repo(project_dir)
    except GitRepositoryUnreadableError as exc:
        raise AttemptCleanupError(str(exc)) from exc
    if is_git:
        if await _is_linked_worktree(project_dir):
            # R3119-07 (task #3126): a nested project's attempt ran in a
            # sub-directory of its worktree; ``git clean`` there would leave
            # the rest of the worktree dirty for the next attempt.
            worktree_root = await git_toplevel_async(project_dir)
            if worktree_root is None:
                raise AttemptCleanupError(
                    f"no work-tree root for {project_dir}; not resetting"
                )
            await _reset_task_worktree(
                str(worktree_root), branch_name, base_sha, emit,
            )
        else:
            await _delete_task_branch_in_main_checkout(
                project_dir, branch_name, emit,
            )

    conn = get_db_connection(write=True)
    try:
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        if reflections:
            _inject_attempt_reflections(conn, task_id, reflections)
        conn.commit()
    finally:
        conn.close()

    emit(
        f"  [Autoresearch] Reset task #{task_id} to todo with "
        f"{len(reflections)} attempt reflection(s)"
    )


# --- DB Scanning & Scoring ---


def _maybe_record_initiative_completion(
    task: dict,
    project_dir: str,
    outcome: str,
    result: dict,
    output: list[str] | None,
) -> None:
    """Append a sub-task entry to the initiative plan file, if applicable.

    No-op (and never raises) when:
      * ``task.initiative_id`` is NULL → backward-compat path
      * ``project_dir`` is missing/empty
      * The plan file or DB row is unavailable
      * Anything goes wrong parsing the agent output

    Best-effort by design: initiative tracking must NEVER block dispatch.
    """
    if not isinstance(task, dict):
        return
    initiative_id = task.get("initiative_id")
    if not initiative_id:
        return
    if not project_dir:
        return

    try:
        from pathlib import Path

        from equipa.initiative import record_task_completion

        branch = f"forge-task-{task['id']}"
        agent_output = (result or {}).get("result_text", "") or ""
        status_label = "done" if outcome != "early_completed_no_changes" else "done (no changes)"

        success = record_task_completion(
            repo_path=Path(project_dir),
            initiative_id=int(initiative_id),
            task_id=int(task["id"]),
            title=str(task.get("title", "")),
            status=status_label,
            branch=branch,
            agent_output=agent_output,
        )

        if success and output is not None:
            output.append(
                f"  [Initiative] Plan file updated for initiative={initiative_id} "
                f"task={task['id']}",
            )

        # Commit the plan file change to the task's branch so it merges
        # with the rest of the task's commits. Failure to commit is
        # logged but never aborts dispatch.
        if success:
            _commit_initiative_plan(project_dir, int(initiative_id), int(task["id"]))
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Initiative completion hook failed (task=%s)", task.get("id"),
        )


def _commit_initiative_plan(project_dir: str, initiative_id: int, task_id: int) -> None:
    """Stage and commit ``.equipa/initiative-<id>.md`` to the task branch.

    Task #3112 (3108 review): runs the hardened ``git_run``, so the
    orchestrator's commit runs no repository hook, fsmonitor or driver an
    agent may have configured in the worktree.
    """
    plan_rel = f".equipa/initiative-{initiative_id}.md"
    plan_abs = Path(project_dir) / plan_rel
    if not plan_abs.exists():
        return
    try:
        git_run(["add", "--", plan_rel], project_dir)
        # Only commit if there are staged changes for the plan file.
        diff = git_run(["diff", "--cached", "--quiet", "--", plan_rel], project_dir)
        if diff.returncode == 0:
            return  # nothing to commit
        git_run(
            [
                "commit", "-m",
                f"chore(initiative-{initiative_id}): record task #{task_id} completion",
                "--", plan_rel,
            ],
            project_dir,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception(
            "Failed to git-commit initiative plan for task=%s", task_id,
        )


async def _require_task_branch(worktree_dir: str, task_branch: str) -> str:
    """Return HEAD's SHA if ``worktree_dir`` is on ``task_branch``.

    Raises:
        AttemptCleanupError: the worktree is on another branch, detached, or
            git could not be run there.
    """
    current = await _current_branch(worktree_dir)
    if current != task_branch:
        raise AttemptCleanupError(
            f"worktree {worktree_dir} is on {current or 'a detached HEAD'!r}, "
            f"expected {task_branch!r}"
        )
    return await _git_checked(
        ["rev-parse", "--verify", "HEAD^{commit}"], worktree_dir, timeout=10,
        action=f"read the HEAD of {task_branch}",
    )


def _audit_task_abort(
    task_id: int,
    event: str,
    detail: object,
    output: list[str] | None,
) -> None:
    """Log a task abort to the operator output and the durable gate audit."""
    # git stderr can span lines; keep each audit record on one line.
    single_line_detail = " ".join(str(detail).split())
    line = f"task={task_id} event={event} detail={single_line_detail}"
    log(f"  [GATE-AUDIT] {line}", output)
    log_gate_audit(line, task_id, event=event)


async def run_dev_test_loop_with_autoresearch(
    task: dict,
    project_dir: str,
    project_context: dict,
    args,
    config: dict,
    output: list[str] | None = None,
    *,
    task_branch: str | None = None,
):
    """Run run_dev_test_loop with autoresearch retry on failure.

    Bug 2282: extracted from the inline pattern that lived only in
    run_dispatch (single-task path). run_parallel_tasks did not have this
    wrapper, so any failure in --tasks N,M,O dispatch was final. This
    helper is the canonical retry entry point - both call sites use it.

    ``task_branch`` is set when ``project_dir`` is the task's isolation
    worktree. Before and after every attempt (task #3111) the worktree must
    still be on that branch; otherwise the task is aborted with outcome
    ``worktree_branch_mismatch`` instead of letting the agent commit
    elsewhere. The HEAD seen before the first attempt is the base the
    worktree is reset to between attempts. A failed reset aborts the task
    with outcome ``attempt_cleanup_failed``. Both outcomes leave the task
    blocked.

    Returns:
        (result, cycles, outcome, loop_total_cost, loop_total_duration, task)

    The caller adds loop_total_cost / loop_total_duration to its own
    running totals (caller may track preflight + loop costs separately).
    The task dict is returned because autoresearch may re-fetch it
    between attempts to pick up reflection-injected context.
    """
    task_id = task["id"]
    autoresearch_on = is_feature_enabled(config, "autoresearch")
    max_retries = config.get("autoresearch_max_retries", 3) if autoresearch_on else 0
    retry_count = 0
    attempt_reflections: list[str] = []
    loop_total_cost = 0.0
    loop_total_duration = 0.0
    base_sha: str | None = None
    result: dict = {"cost": 0.0, "duration": 0.0}
    cycles = 0

    while True:
        if task_branch is not None:
            try:
                head_sha = await _require_task_branch(project_dir, task_branch)
            except AttemptCleanupError as exc:
                _audit_task_abort(task_id, "worktree-branch-mismatch", exc, output)
                outcome = "worktree_branch_mismatch"
                break
            if base_sha is None:
                base_sha = head_sha
        try:
            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args, output=output,
            )
        except CircuitOpenError as exc:
            # S1 (RT-02 follow-up): auto-routing fail-closed signal — every
            # circuit is OPEN. Demote to ``circuit_breaker_blocked`` so the
            # task can be retried after the breaker recovery window without
            # silently escalating cost to opus via DEFAULT_ROLE_MODELS.
            log(
                f"  [GATE-AUDIT] task={task_id} event=circuit-blocked "
                f"role={exc.role} tier_attempted={exc.tier_attempted}",
                output,
            )
            # Task #2702: durably persist the gate event too. This site emits
            # via log() (operator stdout), not _gate_audit_log() (stderr), so
            # we call the DB helper directly to avoid a duplicate stderr line.
            # Best-effort fail-open — log_gate_audit swallows all DB errors.
            log_gate_audit(
                f"task={task_id} event=circuit-blocked "
                f"role={exc.role} tier_attempted={exc.tier_attempted}",
                task_id,
                event="circuit-blocked",
            )
            log(
                f"  [Routing] Task #{task_id} blocked by circuit breaker "
                f"({exc}); deferring dispatch (outcome=circuit_breaker_blocked).",
                output,
            )
            result = {"cost": 0.0, "duration": 0.0}
            cycles = 0
            outcome = "circuit_breaker_blocked"
            break
        loop_total_duration += result.get("duration", 0)
        if result.get("cost"):
            loop_total_cost += result["cost"]

        # Task #3111 (3107 review R1): assert the branch AFTER every attempt
        # too. An agent that checks out another branch (the default branch)
        # during its final, successful attempt would otherwise have its
        # commits there treated as the task's result.
        if task_branch is not None:
            try:
                await _require_task_branch(project_dir, task_branch)
            except AttemptCleanupError as exc:
                _audit_task_abort(
                    task_id, "worktree-branch-mismatch",
                    f"after attempt {retry_count + 1} ({outcome}): {exc}",
                    output,
                )
                outcome = "worktree_branch_mismatch"
                break

        # Success - break out of retry loop
        if outcome in ("tests_passed", "no_tests", "early_completed_no_changes"):
            _maybe_record_initiative_completion(
                task, project_dir, outcome, result, output,
            )
            break

        # Capture reflection on failed attempt for cross-attempt memory
        attempt_reflection = _build_dispatch_attempt_reflection(
            retry_count + 1, outcome, cycles, result,
        )
        attempt_reflections.append(attempt_reflection)

        # Not retriable or retries exhausted
        if not autoresearch_on or retry_count >= max_retries:
            if retry_count > 0:
                log(
                    f"  [Autoresearch] Exhausted {retry_count}/{max_retries} retries "
                    f"for task #{task_id}. Final outcome: {outcome}",
                    output,
                )
            break

        retry_count += 1
        log(
            f"  [Autoresearch] Task #{task_id} failed ({outcome}). "
            f"Retry {retry_count}/{max_retries}...",
            output,
        )

        # Clean up failed git branch and reset task for next attempt.
        try:
            await cleanup_failed_attempt(
                task_id, project_dir, attempt_reflections, output,
                base_sha=base_sha,
            )
        except AttemptCleanupError as exc:
            _audit_task_abort(task_id, "attempt-cleanup-failed", exc, output)
            outcome = "attempt_cleanup_failed"
            break

        # Re-fetch task to get clean state (with injected reflections)
        refreshed = fetch_task(task_id)
        if not refreshed:
            log(
                f"  [Autoresearch] Task #{task_id} disappeared from DB. "
                f"Aborting retries.",
                output,
            )
            break
        task = refreshed

    return result, cycles, outcome, loop_total_cost, loop_total_duration, task


def scan_pending_work() -> list[dict]:
    """Query DB for all projects with todo tasks, grouped by priority.

    Returns a list of dicts:
    [
        {
            "project_id": 21,
            "project_name": "EQUIPA",
            "codename": "equipa",
            "status": "active",
            "tasks": [<task dicts sorted by priority>],
            "counts": {"critical": 0, "high": 2, "medium": 1, "low": 0},
            "total_todo": 3,
        },
        ...
    ]
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.description, t.priority, t.project_id,
                   p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as codename,
                   p.status as project_status
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'todo'
            ORDER BY t.project_id, t.created_at ASC
            """,
        ).fetchall()

        # Group by project
        projects: dict[int, dict] = {}
        for row in rows:
            row = dict(row)
            pid = row["project_id"]
            if pid not in projects:
                projects[pid] = {
                    "project_id": pid,
                    "project_name": row["project_name"],
                    "codename": row["codename"],
                    "status": (row.get("project_status") or "unknown").lower(),
                    "tasks": [],
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "total_todo": 0,
                }
            projects[pid]["tasks"].append(row)
            projects[pid]["total_todo"] += 1
            priority = str(row.get("priority", "low")).lower()
            if priority in projects[pid]["counts"]:
                projects[pid]["counts"][priority] += 1

        # Sort tasks within each project by priority descending
        for proj in projects.values():
            proj["tasks"].sort(
                key=lambda t: PRIORITY_ORDER.get(
                    str(t.get("priority", "low")).lower(), 0
                ),
                reverse=True,
            )

        return list(projects.values())
    finally:
        conn.close()


def score_project(summary: dict, config: dict) -> int:
    """Score a project for dispatch priority.

    score = (critical*10) + (high*5) + (medium*2) + (low*1)
           + 3 if project status is 'active'
           + priority_boost from config
    """
    counts = summary["counts"]
    score = (
        counts.get("critical", 0) * 10
        + counts.get("high", 0) * 5
        + counts.get("medium", 0) * 2
        + counts.get("low", 0) * 1
    )

    if summary.get("status") == "active":
        score += 3

    # Apply manual boost from config
    codename = summary.get("codename", "").lower()
    boost = config.get("priority_boost", {})
    if codename in boost:
        score += boost[codename]
    # Also check by project_id string
    pid_str = str(summary.get("project_id", ""))
    if pid_str in boost:
        score += boost[pid_str]

    summary["score"] = score
    return score


# --- Config Loading & Filters ---

def apply_dispatch_filters(work: list[dict], config: dict, args) -> list[dict]:
    """Apply skip_projects, only_projects, and --only-project filters.

    Returns filtered list of project summaries.
    """
    filtered = list(work)

    # --only-project CLI args take highest priority
    cli_only = getattr(args, "only_project", None) or []
    if cli_only:
        cli_only_set = set(cli_only)
        filtered = [p for p in filtered if p["project_id"] in cli_only_set]
        return filtered

    # Config-level only_projects (whitelist mode)
    config_only = config.get("only_projects", [])
    if config_only:
        only_set: set[int] = set()
        for item in config_only:
            if isinstance(item, int):
                only_set.add(item)
            elif isinstance(item, str):
                # Match by codename
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        only_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] in only_set]
        return filtered

    # Config-level skip_projects
    skip_list = config.get("skip_projects", [])
    if skip_list:
        skip_set: set[int] = set()
        for item in skip_list:
            if isinstance(item, int):
                skip_set.add(item)
            elif isinstance(item, str):
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        skip_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] not in skip_set]

    return filtered


# --- Per-Project Task Runner ---

async def run_project_tasks(
    project_summary: dict,
    config: dict,
    args,
    output: list[str] | None = None,
) -> dict:
    """Run Dev+Test loops on todo tasks for one project, in priority order.

    Returns a dict with results per task.
    """
    project_id = project_summary["project_id"]
    codename = project_summary.get("codename", "unknown")
    tasks = project_summary["tasks"]

    # Apply max_tasks_per_project cap
    max_tasks = getattr(args, "max_tasks_per_project", None)
    if max_tasks is None:
        max_tasks = config.get("max_tasks_per_project", 5)
    if len(tasks) > max_tasks:
        log(f"  [{codename}] Capping to {max_tasks} tasks (of {len(tasks)} todo)", output)
        tasks = tasks[:max_tasks]

    # Resolve project directory
    codename_lower = codename.lower().strip()
    project_dir = _equipa_constants.PROJECT_DIRS.get(codename_lower)
    if not project_dir:
        log(f"  [{codename}] ERROR: No directory mapped. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "No directory mapped",
            "refusals": ["no directory mapped for the project"],
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    # Auto-clone ForgeScaffold for scaffold-based projects whose directory
    # does not yet exist or is uninitialised. Returns True only when a
    # clone was actually performed; benign no-op for non-scaffold projects.
    try:
        from equipa.scaffold import ensure_scaffold, ScaffoldCloneError
        cloned = ensure_scaffold(project_dir, project_id, config=config)
        if cloned:
            log(f"  [{codename}] Auto-cloned ForgeScaffold into {project_dir}", output)
    except ScaffoldCloneError as exc:
        log(f"  [{codename}] ERROR: Scaffold auto-clone failed: {exc}", output)
    except Exception as exc:  # pragma: no cover - defensive
        log(f"  [{codename}] WARN: Scaffold auto-clone raised {exc!r}", output)

    if not Path(project_dir).exists():
        log(f"  [{codename}] ERROR: Directory does not exist: {project_dir}. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "Directory does not exist",
            "refusals": [f"project directory does not exist: {project_dir}"],
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    project_context = fetch_project_context(project_id)

    completed = []
    blocked = []
    refusals: list[str] = []
    total_cost = 0.0
    total_duration = 0.0

    # Build args namespace for dev-test loop
    task_args = argparse.Namespace(
        model=config.get("model", args.model),
        max_turns=config.get("max_turns", args.max_turns),
        dispatch_config=config,  # pass config so get_role_turns can read per-role limits
        # dispatch-07 (task #3112): the isolated review honours the operator's
        # --no-security-review choice; absent means the config decides.
        security_review=getattr(args, "security_review", None),
    )

    # dispatch-07 (task #3112): in a git project every task runs in its own
    # worktree and reaches the default branch only through the gated merge.
    # One guard per project: every orchestrator merge advances it, anything
    # else trips it and the remaining tasks are refused.
    project_guard: DefaultBranchGuard | None = None
    try:
        use_isolation = _is_git_repo(project_dir)
    except GitRepositoryUnreadableError as exc:
        # R3119-02 (task #3126): never run an unreadable repo ungated.
        log(f"  [{codename}] REFUSED: {exc}", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": str(exc),
            "refusals": [str(exc)],
            "total_cost": 0.0,
            "total_duration": 0.0,
        }
    if use_isolation:
        await report_leftover_dispatch_state(project_dir)
        try:
            project_guard = await DefaultBranchGuard.snapshot(project_dir)
        except MergeIntegrityError as exc:
            log(
                f"  [{codename}] ERROR: default branch could not be pinned "
                f"({exc}); no task is dispatched in this project.",
                output,
            )
            return {
                "project_id": project_id,
                "codename": codename,
                "tasks_attempted": 0,
                "tasks_completed": [],
                "tasks_blocked": [],
                "tasks_skipped": len(tasks),
                "error": f"default branch could not be pinned: {exc}",
                "refusals": [f"default branch could not be pinned: {exc}"],
                "total_cost": 0.0,
                "total_duration": 0.0,
            }

    for i, task_row in enumerate(tasks, 1):
        task_id = task_row["id"]
        log(f"\n  [{codename}] Task {i}/{len(tasks)}: #{task_id} - {task_row['title']}", output)

        # Re-fetch task to get full data with project info
        task = fetch_task(task_id)
        if not task:
            log(f"  [{codename}] Task #{task_id} not found in DB. Skipping.", output)
            continue

        # Check if still todo
        if task.get("status") != "todo":
            log(f"  [{codename}] Task #{task_id} status is '{task.get('status')}'. Skipping.", output)
            continue

        # --- Lifecycle hooks: pre_dispatch ---
        await fire_hook(
            "pre_dispatch",
            task_id=task_id, project_dir=project_dir, codename=codename,
            title=task.get("title", ""),
        )

        # --- Autoresearch retry loop with cross-attempt memory ---
        # Bug 2282: extracted into module-level helper so run_parallel_tasks
        # can reuse the same retry semantics. Both code paths now share one
        # canonical autoresearch wrapper.
        merged_sha: str | None = None
        if use_isolation:
            loop_totals: dict = {}

            async def execute_in_worktree(
                worktree_dir: str, task_branch: str, task: dict = task,
                loop_totals: dict = loop_totals,
            ) -> tuple[dict, int, str]:
                loop_result, loop_cycles, loop_outcome, cost, duration, refreshed = (
                    await run_dev_test_loop_with_autoresearch(
                        task, worktree_dir, project_context, task_args, config,
                        output=output, task_branch=task_branch,
                    )
                )
                loop_totals.update(cost=cost, duration=duration, task=refreshed)
                return loop_result, loop_cycles, loop_outcome

            isolated = await run_task_in_isolation(
                task, project_dir, project_context, task_args,
                execute=execute_in_worktree, guard=project_guard, output=output,
            )
            if isolated.outcome == "shutdown_requested":
                # Never started: the task and the rest of the queue stay todo.
                log(f"  [{codename}] {isolated.reason}; not starting further tasks.", output)
                break
            if isolated.agent_outcome is None:
                # Refused before any agent ran: recorded blocked, no agent
                # telemetry, and the CLI exits with EXIT_DISPATCH_REFUSED.
                update_task_status(task_id, isolated.outcome, output=output)
                refusals.append(f"task #{task_id} {isolated.outcome}: {isolated.reason}")
                blocked.append(task)
                log(f"  [{codename}] Task #{task_id}: REFUSED ({isolated.reason})", output)
                continue
            result, cycles, outcome = isolated.result, isolated.cycles, isolated.outcome
            merged_sha = isolated.merged_sha
            task = loop_totals.get("task", task)
            _loop_cost = loop_totals.get("cost", 0.0)
            _loop_duration = loop_totals.get("duration", 0.0)
        else:
            # Not a git repo: there are no branches to protect.
            result, cycles, outcome, _loop_cost, _loop_duration, task = (
                await run_dev_test_loop_with_autoresearch(
                    task, project_dir, project_context, task_args, config, output=output,
                )
            )
        total_cost += _loop_cost
        total_duration += _loop_duration

        # Orchestrator-side DB update (don't rely on agent). In a git project
        # the outcome already reflects the gated merge (dispatch-05).
        update_task_status(task_id, outcome, output=output, merged_sha=merged_sha)

        # ForgeSmith telemetry
        task_role = task.get("role") or "developer"
        # S1 (2453): telemetry must survive a CircuitOpenError-demoted
        # outcome — at that point no model was actually dispatched, so
        # log the sentinel ``circuit_blocked`` rather than re-raising
        # through the post-loop bookkeeping.
        try:
            telemetry_model = get_role_model(task_role, task_args, task=task)
        except CircuitOpenError:
            telemetry_model = "circuit_blocked"
        record_agent_run(
            task, result, outcome, role=task_role,
            model=telemetry_model,
            max_turns=get_role_turns(task_role, task_args, task=task),
            cycle_number=cycles, output=output,
        )

        # Post-task quality scoring (on success only)
        if outcome in ("tests_passed", "no_tests"):
            run_quality_scoring(task, result, outcome, role=task_role, output=output,
                                dispatch_config=config)

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, outcome, role=task_role, output=output)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task_id, outcome, output=output)

        # --- Lifecycle hooks: post_task_complete ---
        await fire_hook(
            "post_task_complete",
            task_id=task_id, project_dir=project_dir, codename=codename,
            outcome=outcome, cycles=cycles,
            cost=result.get("cost"), duration=result.get("duration", 0),
        )

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            log(f"  [{codename}] Task #{task_id}: COMPLETED ({outcome})", output)
        else:
            blocked.append(task)
            log(f"  [{codename}] Task #{task_id}: BLOCKED ({outcome})", output)

    return {
        "project_id": project_id,
        "codename": codename,
        "tasks_attempted": len(tasks),
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": 0,
        "error": None,
        "refusals": refusals,
        "total_cost": total_cost,
        "total_duration": total_duration,
    }


async def run_project_dispatch(
    project_summary: dict,
    semaphore: asyncio.Semaphore,
    config: dict,
    args,
) -> dict:
    """Wrapper for concurrent execution of one project's tasks.

    Acquires semaphore slot, runs tasks, returns result with buffered output.
    """
    codename = project_summary.get("codename", "unknown")
    output: list[str] = []

    log(f"\n[{codename}] Queued ({project_summary['total_todo']} todo tasks, "
        f"score: {project_summary.get('score', '?')})", output)

    async with semaphore:
        log(f"[{codename}] Acquired slot, starting...", output)

        try:
            result = await run_project_tasks(
                project_summary, config, args, output=output,
            )
        except Exception as e:
            # TELEMETRY safety net: run_project_tasks transitively invokes
            # AI APIs, subprocess, git, and DB calls — any of which can raise
            # unbounded exception types. Keep broad but log with traceback so
            # failures aren't silently swallowed.
            logger.exception(
                "[Dispatch] project '%s' raised during run_project_tasks", codename
            )
            log(f"[{codename}] EXCEPTION: {e}", output)
            result = {
                "project_id": project_summary["project_id"],
                "codename": codename,
                "tasks_attempted": 0,
                "tasks_completed": [],
                "tasks_blocked": [],
                "tasks_skipped": project_summary["total_todo"],
                "error": str(e),
                # R3119-04 (task #3126): a crashed project exits non-zero.
                "refusals": [f"exception: {e}"],
                "total_cost": 0.0,
                "total_duration": 0.0,
            }

    result["output"] = output
    return result


async def run_auto_dispatch(scored: list[dict], config: dict, args) -> list:
    """Run all project dispatches concurrently with semaphore.

    Prints each project's buffered output as it completes, then summary.
    Returns the per-project results; their ``refusals`` feed
    :func:`collect_refusals`.
    """
    max_concurrent = config.get("max_concurrent", 4)
    refusal = concurrency_refusal(max_concurrent)
    if refusal:
        refuse_dispatch(refusal)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nDispatching {len(scored)} projects "
          f"(max {max_concurrent} concurrent)...\n")

    coros = [
        run_project_dispatch(proj, semaphore, config, args)
        for proj in scored
    ]

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Print each project's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  PROJECT EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: {r.get('codename', '?')}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_dispatch_summary(results)
    return results


# --- Goals ---

def load_goals_file(filepath: str | Path) -> tuple[dict, list[dict]]:
    """Parse and validate a goals JSON file.

    Returns (defaults_dict, goals_list) tuple.
    Exits with error on invalid input.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: Goals file not found: {filepath}")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in goals file: {e}")
        sys.exit(1)

    if "goals" not in data or not isinstance(data["goals"], list):
        print("ERROR: Goals file must contain a 'goals' array")
        sys.exit(1)

    if not data["goals"]:
        print("ERROR: Goals array is empty")
        sys.exit(1)

    # Extract defaults
    defaults = {
        "max_concurrent": data.get("max_concurrent", 4),
        "model": data.get("model", DEFAULT_MODEL),
        "max_turns": data.get("max_turns", DEFAULT_MAX_TURNS),
        "max_rounds": data.get("max_rounds", MAX_MANAGER_ROUNDS),
    }

    # Validate each goal
    for i, g in enumerate(data["goals"]):
        if "goal" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'goal' field")
            sys.exit(1)
        if "project_id" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'project_id' field")
            sys.exit(1)

    return defaults, data["goals"]


def validate_goals(goals: list[dict]) -> list[dict]:
    """Validate goals: check project_ids exist, dirs exist, no duplicates.

    Returns list of resolved goal dicts with project_dir and project_info added.
    Exits with error on validation failure.
    """
    # Check for duplicate project_ids
    project_ids = [g["project_id"] for g in goals]
    seen: set[int] = set()
    for pid in project_ids:
        if pid in seen:
            print(f"ERROR: Duplicate project_id {pid} in goals file. "
                  f"Two goals cannot target the same project (they'd write to the same directory).")
            sys.exit(1)
        seen.add(pid)

    resolved = []
    for i, g in enumerate(goals):
        project_info = fetch_project_info(g["project_id"])
        if not project_info:
            print(f"ERROR: Goal #{i + 1}: Project {g['project_id']} not found in TheForge")
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = (
            _equipa_constants.PROJECT_DIRS.get(codename)
            or _equipa_constants.PROJECT_DIRS.get(pname)
        )

        if not project_dir:
            print(f"ERROR: Goal #{i + 1}: No directory mapped for project "
                  f"'{project_info.get('name', 'Unknown')}'")
            sys.exit(1)

        if not Path(project_dir).exists():
            print(f"ERROR: Goal #{i + 1}: Directory does not exist: {project_dir}")
            sys.exit(1)

        resolved.append({
            **g,
            "project_dir": project_dir,
            "project_info": project_info,
        })

    return resolved


async def run_single_goal(
    goal_entry: dict,
    semaphore: asyncio.Semaphore,
    index: int,
    defaults: dict,
    args,
) -> dict:
    """Run a single Manager loop for one goal, respecting the semaphore.

    Returns a result dict with goal info and outcome.
    """
    goal_text = goal_entry["goal"]
    project_id = goal_entry["project_id"]
    project_dir = goal_entry["project_dir"]
    project_name = goal_entry["project_info"].get("name", "Unknown")

    # Per-goal overrides or defaults
    model = goal_entry.get("model", defaults["model"])
    max_turns = goal_entry.get("max_turns", defaults["max_turns"])
    max_rounds = goal_entry.get("max_rounds", defaults["max_rounds"])

    # Create a namespace that looks like args for the manager loop. The
    # dispatch config and --no-security-review carry through so the isolated
    # review and gated merge follow the operator's policy (task #3112).
    goal_args = argparse.Namespace(
        model=model,
        max_turns=max_turns,
        max_rounds=max_rounds,
        dispatch_config=getattr(args, "dispatch_config", None),
        security_review=getattr(args, "security_review", None),
    )

    output: list[str] = []  # Buffer all output for this goal
    log(f"\n[Goal {index + 1}] {goal_text}", output)
    log(f"  Project: {project_name} (ID: {project_id})", output)
    log(f"  Directory: {project_dir}", output)
    log(f"  Model: {model}, Max turns: {max_turns}, Max rounds: {max_rounds}", output)

    async with semaphore:
        log(f"\n[Goal {index + 1}] Acquired slot, starting...", output)
        project_context = fetch_project_context(project_id)

        try:
            outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
                goal_text, project_id, project_dir, project_context,
                goal_args, output=output,
            )
        except Exception as e:
            # TELEMETRY safety net: run_manager_loop invokes AI APIs, subprocess,
            # git, and DB calls. Keep broad — narrowing risks dropping a real
            # failure path — but log with traceback for diagnostics.
            logger.exception(
                "[Goal %d] '%s' raised during run_manager_loop",
                index + 1, project_name,
            )
            log(f"\n[Goal {index + 1}] EXCEPTION: {e}", output)
            return {
                "index": index,
                "goal": goal_text,
                "project_name": project_name,
                "project_id": project_id,
                "outcome": "exception",
                "error": str(e),
                # R3119-04 (task #3126): a crashed goal exits non-zero.
                "refusals": [f"exception: {e}"],
                "rounds": 0,
                "completed": [],
                "blocked": [],
                "cost": 0.0,
                "duration": 0.0,
                "output": output,
            }

        print_manager_summary(
            goal_text, outcome, rounds, completed, blocked, cost, duration,
            output=output,
        )

    return {
        "index": index,
        "goal": goal_text,
        "project_name": project_name,
        "project_id": project_id,
        "outcome": outcome,
        "refusals": (
            [f"goal stopped: {outcome}"] if outcome in GOAL_REFUSED_OUTCOMES else []
        ),
        "rounds": rounds,
        "completed": completed,
        "blocked": blocked,
        "cost": cost,
        "duration": duration,
        "output": output,
    }


async def run_parallel_goals(resolved_goals: list[dict], defaults: dict, args) -> list:
    """Run multiple Manager loops concurrently with a semaphore.

    Prints each goal's buffered output as it completes, then a combined
    summary. Returns the per-goal results (an exception for a goal that
    raised); their ``refusals`` feed :func:`collect_refusals`.
    """
    max_concurrent = args.max_concurrent or defaults["max_concurrent"]
    refusal = concurrency_refusal(max_concurrent)
    if refusal:
        refuse_dispatch(refusal)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nStarting {len(resolved_goals)} parallel goals "
          f"(max {max_concurrent} concurrent)...\n")

    # Launch all goals
    tasks = [
        run_single_goal(g, semaphore, i, defaults, args)
        for i, g in enumerate(resolved_goals)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Print each goal's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  GOAL EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: Goal {r['index'] + 1} — {r['project_name']}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_parallel_summary(results)
    return results


# --- Parallel Tasks ---

_TASK_ID_PART_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_task_ids(task_str: str) -> list[int]:
    """Parse comma-separated IDs or ranges into a list of ints.

    Examples: "109,110,111" -> [109, 110, 111]
              "109-114" -> [109, 110, 111, 112, 113, 114]
              "109,112-114" -> [109, 112, 113, 114]

    Each "start-end" range is bounded by MAX_TASK_RANGE to prevent memory
    exhaustion (e.g. "1-999999999" would otherwise materialise ~1B ints).

    dispatch-15 (task #3112): duplicates are dropped (first occurrence
    wins), so "5,5" or "4-6,5" never dispatches one task twice. Every part
    must be a positive id or an ascending "start-end" range; anything else
    ("-5", "7-3", "0", "abc", an empty part) raises ValueError naming the
    offending part instead of a bare int() error.
    """
    ids: list[int] = []
    seen: set[int] = set()
    for raw_part in task_str.split(","):
        part = raw_part.strip()
        match = _TASK_ID_PART_RE.match(part)
        if match is None:
            raise ValueError(
                f"invalid task id {part!r}: expected a positive id or an "
                f"ascending range like 109-114"
            )
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if start < 1:
            raise ValueError(f"invalid task id {part!r}: ids start at 1")
        if end < start:
            raise ValueError(
                f"invalid task range {part!r}: end is below start"
            )
        if end - start > MAX_TASK_RANGE:
            raise ValueError(
                f"task range too large: {part} (max {MAX_TASK_RANGE})"
            )
        for task_id in range(start, end + 1):
            if task_id not in seen:
                seen.add(task_id)
                ids.append(task_id)
    return ids


def _copy_hooks_to_worktree(main_repo_dir: str, worktree_dir: str) -> None:
    """Copy git hooks from the main repo into a worktree.

    Worktrees do NOT inherit .git/hooks from the parent repo. This means
    pre-commit hooks (like plugin boundary checks) won't fire in worktrees
    unless we explicitly copy them.
    """
    import shutil
    main_hooks = Path(main_repo_dir) / ".git" / "hooks"
    if not main_hooks.is_dir():
        return

    # Worktree .git is a file pointing to the main repo's worktree dir.
    # The actual hooks dir for a worktree is in the main repo at:
    # .git/worktrees/<worktree-name>/hooks (doesn't exist by default)
    # But we can also just copy hooks into the worktree and configure.
    #
    # Simplest approach: copy the pre-commit hook and any other hooks
    # to the worktree's common hooks dir.
    wt_git_path = Path(worktree_dir) / ".git"
    if wt_git_path.is_file():
        # .git is a file like "gitdir: /path/to/.git/worktrees/task-123"
        gitdir = wt_git_path.read_text().strip().replace("gitdir: ", "")
        wt_hooks = Path(gitdir) / "hooks"
        wt_hooks.mkdir(exist_ok=True)
        for hook in main_hooks.iterdir():
            if hook.is_file() and not hook.name.endswith(".sample"):
                dest = wt_hooks / hook.name
                shutil.copy2(str(hook), str(dest))
    # Also copy .plugin-boundary-markers if it exists (for the pre-commit hook)
    markers_src = Path(main_repo_dir) / ".plugin-boundary-markers"
    markers_dst = Path(worktree_dir) / ".plugin-boundary-markers"
    if markers_src.exists() and not markers_dst.exists():
        shutil.copy2(str(markers_src), str(markers_dst))


async def _pin_role_overlay_ref(project_dir: str) -> None:
    """Pin project role overlays to the pre-dispatch default-branch SHA.

    SR-2994 S1: overlays are agent instructions, so they are read from the
    stable project root at the commit the default branch had BEFORE any
    agent ran — nothing committed during the dispatch (even onto the default
    branch) can change them.

    SR-2997 S1: the branch is the operator-named one from
    ``get_trusted_default_branch`` — never ``refs/remotes/origin/HEAD`` or the
    checked-out HEAD, which any agent worktree can repoint. The pin must also
    follow the previous pin: same branch, and a descendant of the previous
    SHA. Any failure fails CLOSED (project overlays disabled) and a refused
    pin raises a GATE-AUDIT alarm.
    """
    from equipa.git_ops import UntrustedDefaultBranchError, get_trusted_default_branch
    from equipa.role_resolver import block_overlays, current_overlay_pin, pin_overlay_ref

    def refuse(reason: str) -> None:
        block_overlays(project_dir, reason)
        print(f"  [Isolation] WARNING: role overlays DISABLED for {project_dir}: {reason}")
        _gate_audit_log(
            f"event=overlay-pin-refused project={project_dir} reason={reason}",
            event="overlay-pin-refused",
        )

    try:
        default_branch = get_trusted_default_branch(project_dir)
    except UntrustedDefaultBranchError as exc:
        refuse(str(exc))
        return
    try:
        sha_res = await git_run_async(
            ["rev-parse", "--verify", "--quiet",
             f"refs/heads/{default_branch}^{{commit}}"],
            project_dir, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        refuse(f"could not resolve '{default_branch}': {exc}")
        return
    sha = (sha_res.stdout or "").strip()
    if sha_res.returncode != 0 or not sha:
        refuse(f"'{default_branch}' did not resolve to a commit")
        return
    previous = current_overlay_pin(project_dir)
    if previous is not None:
        previous_branch, previous_sha = previous
        if previous_branch is not None and previous_branch != default_branch:
            refuse(f"default branch changed from '{previous_branch}' to "
                   f"'{default_branch}' since the previous pin")
            return
        if previous_sha != sha:
            try:
                ancestry = await git_run_async(
                    ["merge-base", "--is-ancestor", previous_sha, sha],
                    project_dir, timeout=10,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                refuse(f"could not check ancestry of {sha[:12]}: {exc}")
                return
            if ancestry.returncode != 0:
                refuse(f"'{default_branch}'@{sha[:12]} does not descend from "
                       f"the previous pin {previous_sha[:12]}")
                return
    try:
        pin_overlay_ref(project_dir, sha, branch=default_branch)
    except ValueError as exc:
        refuse(str(exc))
        return
    print(f"  [Isolation] Role overlays pinned to {default_branch}@{sha[:12]}")


async def _create_isolation_worktrees(
    tasks: list[dict],
    project_dir: str,
    worktree_base: Path,
    *,
    force: bool = False,
    refusals: dict[int, str] | None = None,
) -> dict[int, str]:
    """Create per-task git worktrees for filesystem isolation.

    Returns a map of task_id -> worktree directory path. A task missing
    from the map has NO isolation worktree and must not run at all: the
    caller refuses it rather than running it in ``project_dir``, where its
    commits would bypass the security gate (dispatch-01). The reason for
    each missing task is stored in ``refusals`` when the caller passes a
    dict.

    Stale-branch policy (tasks #2490, #3107): when ``forge-task-<id>``
    already exists, the task is refused and nothing is touched — neither
    the branch nor a leftover worktree holding it. The unmerged commits on
    the branch are logged as a recovery anchor. Only ``force=True`` deletes
    the branch (after logging the commits it would discard) and recreates
    the worktree.

    A leftover worktree directory is removed only after its uncommitted
    work has been stashed. If that fails, or the directory is not a
    registered worktree, the task is refused and the directory kept
    (dispatch-13).

    Uses ``git_run_async`` so the per-task git invocations do not block
    the event loop while parallel task dispatch is queued.
    """
    from equipa.role_resolver import register_worktree_root

    worktree_dirs: dict[int, str] = {}
    worktree_base.mkdir(exist_ok=True)
    await _pin_role_overlay_ref(project_dir)

    def refuse(task_id: int, reason: str) -> None:
        print(f"  [Isolation] WARNING: task #{task_id} REFUSED, not run: {reason}")
        if refusals is not None:
            refusals[task_id] = reason

    for t in tasks:
        task_id = t["id"]
        branch_name = f"forge-task-{task_id}"
        wt_path = worktree_base / f"task-{task_id}"
        try:
            branch_res = await git_run_async(
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
                project_dir, timeout=10,
            )
            if branch_res.returncode not in (0, 1):
                refuse(task_id, (
                    f"could not check for a stale branch {branch_name} "
                    f"(rc={branch_res.returncode}: "
                    f"{(branch_res.stderr or '').strip()[:200]})"
                ))
                continue
            branch_exists = branch_res.returncode == 0
            if branch_exists:
                await _log_stale_branch_commits(project_dir, branch_name)
                if not force:
                    refuse(task_id, (
                        f"stale branch {branch_name} exists; preserved, "
                        f"resolve by hand"
                    ))
                    continue
            if wt_path.exists():
                leftover_problem = await _retire_leftover_worktree(
                    project_dir, wt_path, task_id, branch_name,
                )
                if leftover_problem:
                    refuse(task_id, leftover_problem)
                    continue
            if branch_exists:
                print(
                    f"  [Isolation] WARNING: force=True, deleting stale "
                    f"branch '{branch_name}' for task #{task_id}"
                )
                delete_res = await git_run_async(
                    ["branch", "-D", branch_name], project_dir, timeout=10,
                )
                if delete_res.returncode != 0:
                    refuse(task_id, (
                        f"could not delete stale branch {branch_name}: "
                        f"{(delete_res.stderr or '').strip()[:200]}"
                    ))
                    continue
            add_res = await git_run_async(
                ["worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
                project_dir, timeout=60,
            )
            if add_res.returncode != 0:
                err_preview = (
                    add_res.stderr.strip()[:200] if add_res.stderr
                    else f"rc={add_res.returncode}"
                )
                refuse(task_id, f"could not create worktree ({err_preview})")
                continue
            worktree_dirs[task_id] = str(wt_path)
            register_worktree_root(wt_path, project_dir)
            _copy_hooks_to_worktree(project_dir, str(wt_path))
            print(f"  [Isolation] Task #{task_id} -> {wt_path.name}")
        except (subprocess.SubprocessError, OSError) as e:
            refuse(task_id, f"worktree creation errored: {e}")
    return worktree_dirs


def project_dir_in_worktree(
    project_dir: str, worktree_dir: str,
) -> tuple[str | None, str]:
    """The project's directory inside a task worktree of its repository.

    Task #3119: a project nested inside another repository gets a worktree
    of the ENCLOSING repository, so its agents must run in the project's
    sub-directory of that worktree, not at the worktree root. For a project
    at the root of its own repository this is ``worktree_dir`` itself.

    Returns ``(directory, "")``, or ``(None, reason)`` when the project is
    not part of the checked-out tree (for example untracked or ignored in
    the enclosing repository); the task must then be refused, never run in
    ``project_dir``.
    """
    toplevel = git_toplevel(project_dir)
    if toplevel is None:
        return None, f"{project_dir} is not inside a git work tree"
    try:
        relative = Path(project_dir).resolve().relative_to(toplevel.resolve())
    except ValueError:
        return None, (
            f"{project_dir} does not resolve inside its repository {toplevel}"
        )
    if relative == Path("."):
        return worktree_dir, ""
    nested_dir = Path(worktree_dir) / relative
    if not nested_dir.is_dir():
        return None, (
            f"'{relative}' is not tracked by the enclosing repository "
            f"{toplevel}, so the task worktree has no such directory"
        )
    # R3119-06 (task #3126): no component of ``relative`` may be a symlink,
    # or the agent would run wherever it points (e.g. the main checkout).
    if nested_dir.resolve() != Path(worktree_dir).resolve() / relative:
        return None, (
            f"'{relative}' in the task worktree resolves to "
            f"{nested_dir.resolve()}, outside the worktree's own '{relative}'"
        )
    return str(nested_dir), ""


async def _log_stale_branch_commits(project_dir: str, branch_name: str) -> None:
    """Log the commits on a stale task branch that are not on the default.

    Gives the operator a recovery anchor (``git reflog``) before the branch
    is refused or, with ``force=True``, deleted.
    """
    default_branch = get_default_branch(project_dir)
    try:
        sha_res = await git_run_async(
            ["rev-list", f"{default_branch}..{branch_name}"],
            project_dir, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  [Isolation] WARNING: could not inspect '{branch_name}': {exc}")
        return
    if sha_res.returncode != 0:
        print(
            f"  [Isolation] WARNING: could not inspect '{branch_name}': "
            f"{(sha_res.stderr or '').strip()[:200]}"
        )
        return
    unmerged = (sha_res.stdout or "").split()
    print(
        f"  [Isolation] Branch '{branch_name}' already exists with "
        f"{len(unmerged)} unmerged commit(s) ahead of '{default_branch}'"
    )
    if unmerged:
        print(
            f"  [Isolation] Unmerged SHAs for '{branch_name}' "
            f"(recoverable via reflog): {' '.join(unmerged)}"
        )


async def _retire_leftover_worktree(
    project_dir: str,
    wt_path: Path,
    task_id: int,
    branch_name: str,
) -> str | None:
    """Remove a leftover task worktree without losing uncommitted work.

    Uncommitted changes (untracked files included) are stashed first. The
    worktree is force-removed only once ``git status`` reads clean.

    Returns:
        None once the worktree is gone, otherwise the reason it was kept.
    """
    wt = str(wt_path)
    toplevel = await git_run_async(["rev-parse", "--show-toplevel"], wt, timeout=10)
    toplevel_path = (toplevel.stdout or "").strip()
    if toplevel.returncode != 0 or Path(toplevel_path).resolve() != wt_path.resolve():
        # Not a worktree of its own: git run there would act on the
        # enclosing checkout, so neither stash nor remove it.
        if wt_path.is_dir() and not any(wt_path.iterdir()):
            wt_path.rmdir()
            return None
        return (
            f"leftover {wt} is not a registered worktree; preserved, "
            f"resolve by hand"
        )
    status = await git_run_async(
        ["status", "--porcelain", "--ignore-submodules=all"], wt, timeout=15,
    )
    if status.returncode != 0:
        return (
            f"could not read the status of leftover worktree {wt}; "
            f"preserved, resolve by hand"
        )
    if status.stdout.strip():
        await _stash_uncommitted_in_worktree(wt, task_id, branch_name)
        recheck = await git_run_async(
            ["status", "--porcelain", "--ignore-submodules=all"], wt, timeout=15,
        )
        if recheck.returncode != 0 or recheck.stdout.strip():
            return (
                f"leftover worktree {wt} has uncommitted work that could not "
                f"be stashed; preserved, resolve by hand"
            )
    remove = await git_run_async(
        ["worktree", "remove", "--force", wt], project_dir, timeout=30,
    )
    if remove.returncode != 0:
        return (
            f"could not remove leftover worktree {wt}: "
            f"{(remove.stderr or '').strip()[:200]}"
        )
    return None


async def _merge_task_branch(
    project_dir: str,
    task_id: int,
    branch_name: str,
    *,
    expect_artifact: bool = True,
    merge_sha: str | None = None,
    worktree_dir: str | None = None,
    merge_record: MergeAttempt | None = None,
    artifact_dir: str | None = None,
    pinned_default_sha: str | None = None,
) -> bool:
    """Merge a single task branch into the main repo's current branch.

    ``project_dir`` is where git runs (the work-tree root); ``artifact_dir``,
    when given, is where the review artifact is read from — the project's
    own directory, which for a project nested in its repository is a
    sub-directory of that root (R3119-01, task #3126).

    Task #3111 (gate-01): the merge target is a commit SHA, never the branch
    name. ``merge_sha`` is the commit the gate approved (the reviewed SHA);
    when omitted, the branch tip is resolved ONCE and that SHA is merged, so
    a commit landing on the branch mid-merge cannot ride along. When
    ``merge_record`` is given it is filled in with what actually landed
    (``merged_sha`` — the pinned commit or its rebased copy — plus the
    default-branch SHA before and after, and a failure ``reason``).

    dispatch-08: the conflict fallback rebases the pinned commit onto the
    default branch INSIDE the task worktree (``worktree_dir``), then
    fast-forwards the default branch from the main checkout with
    ``--ff-only``. Success requires the default branch to have actually moved
    to the rebased SHA. Without a worktree there is no fallback.

    Task #3131: before that fallback, a merge that conflicts ONLY in files
    declared in :data:`equipa.generated_files.GENERATED_FILES` is completed
    by regenerating them from the merged tree (``merge_record`` then carries
    ``regenerated_paths`` and the resolution commit as ``post_head``). If the
    task branch changed the generator, or the generator fails or times out,
    the merge is aborted and fails with that reason; any other conflict takes
    the unchanged abort / rebase path. Task #3141 (I-01): the resolution is
    attempted only with ``pinned_default_sha`` — the run guard's expected
    default-branch SHA — which is the generator's trust anchor; it is
    refused unless the merge started from that SHA.

    Returns True if the merge succeeded (HEAD advanced), False otherwise.
    All failures are logged to stdout — the function NEVER swallows errors
    silently. On any failure path, the branch is preserved (not deleted).

    Defensive invariant (task #2451): before issuing ``git merge``, re-read
    SECURITY-REVIEW-{task_id}.md from ``project_dir`` and raise
    :class:`SecurityGateBypassError` if it reports a HIGH or CRITICAL
    finding. This makes it impossible to reach ``git merge`` past a
    known-blocking review even if a caller forgets the gate.

    ``expect_artifact`` (task #2451 Phase H, F-01 fix): callers that know
    the diff is doc-only (the security reviewer was skipped on purpose by
    the doc-only short-circuit, per task #2358) MUST pass
    ``expect_artifact=False`` so the defensive invariant does NOT demand
    a SECURITY-REVIEW-{task_id}.md that was never written. Without this
    hint, every pure .md/.rst/.txt change is blocked at the merge —
    a regression introduced when the Phase-A fail-closed-on-None rule
    landed in attempt-2. When ``False``, the invariant is skipped
    entirely; doc-only diffs cannot introduce code-level findings, so
    there is nothing to gate on.

    Task #2706: ``expect_artifact`` is NO LONGER a caller-supplied signal.
    The sole caller (``_gated_merge_task``) DERIVES it from the ground-truth
    ``GateDecision.doc_only`` (re-computed from the real branch diff), so a
    caller can no longer set it wrongly to disable this last line of
    defence. It remains a parameter here (default ``True`` = fail-closed) so
    the invariant stays independently unit-testable in the hermetic gate
    tests.

    Uses ``git_run_async`` so the 6-12 git invocations per merge do not
    block the event loop.
    """
    # Task 2476: artifacts now live under .equipa-artifacts/. Tolerate
    # legacy repo-root artifacts via find_review_artifact for in-flight
    # runs still on the old layout.
    review_path = find_review_artifact(
        os.fspath(artifact_dir or project_dir), "SECURITY-REVIEW", task_id,
    )
    if not expect_artifact:
        _gate_audit_log(
            f"task={task_id} event=defensive-invariant-skipped "
            f"reason=doc-only-no-artifact-expected",
            task_id=task_id,
            event="defensive-invariant-skipped",
        )
    else:
        provenance = verify_reviewer_provenance(task_id, review_path)
        if not provenance.trusted:
            _gate_audit_log(
                f"task={task_id} event=defensive-invariant-fired "
                f"reason={provenance.reason} "
                f"{provenance.fingerprint.describe()}",
                task_id=task_id,
                event="defensive-invariant-fired",
            )
            raise SecurityGateBypassError(
                f"Refusing to merge branch {branch_name!r}: "
                f"SECURITY-REVIEW-{task_id}.md was not written by this "
                f"cycle's security-reviewer run ({provenance.reason}). The "
                f"defensive invariant fails closed (task #3041)."
            )
        # Task #3063 (SR41-02): parse the exact bytes provenance verified.
        counts = (
            _count_findings_in_review_file(
                review_path, task_id=task_id, text=provenance.text,
            )
            if provenance.text is not None else None
        )
        if counts is None:
            _gate_audit_log(
                f"task={task_id} event=defensive-invariant-fired "
                f"{provenance.fingerprint.describe()} "
                f"reason=artifact-unparseable-or-missing "
                f"{format_counts(None)}",
                task_id=task_id,
                event="defensive-invariant-fired",
            )
            raise SecurityGateBypassError(
                f"Refusing to merge branch {branch_name!r}: "
                f"SECURITY-REVIEW-{task_id}.md is missing or unparseable "
                f"(fallback dump, or no findings table). The defensive "
                f"invariant fails closed (task #2451) — operator must "
                f"resolve before merge."
            )
        if counts.get("CRITICAL", 0) > 0 or counts.get("HIGH", 0) > 0:
            _gate_audit_log(
                f"task={task_id} event=defensive-invariant-fired "
                f"{provenance.fingerprint.describe()} {format_counts(counts)}",
                task_id=task_id,
                event="defensive-invariant-fired",
                counts=counts,
            )
            raise SecurityGateBypassError(
                f"Refusing to merge branch {branch_name!r}: "
                f"SECURITY-REVIEW-{task_id}.md reports "
                f"{counts.get('CRITICAL', 0)} CRITICAL, "
                f"{counts.get('HIGH', 0)} HIGH finding(s)."
            )
        # Task #3041: the last gate step before the merge names the exact
        # bytes it cleared, so a counts/file mismatch is visible in the log.
        _gate_audit_log(
            f"task={task_id} event=defensive-invariant-passed "
            f"provenance={provenance.reason} "
            f"{provenance.describe_reviewer()} "
            f"{provenance.fingerprint.describe()} {format_counts(counts)}",
            task_id=task_id,
            event="defensive-invariant-passed",
            counts=counts,
        )
    record = merge_record if merge_record is not None else MergeAttempt()
    try:
        # Task #2493: always merge INTO the repo's DEFAULT branch, never the
        # branch HEAD happens to sit on. In single-task (--task) mode the main
        # checkout has HEAD ON the per-task branch, so the old "merge into the
        # current branch / count HEAD..<branch>" logic produced an empty range
        # and skipped the merge as a silent no-op (the never-merged bug). We
        # resolve the default branch explicitly (do NOT hardcode master/main)
        # and check it out first whenever HEAD is not already on it. This
        # generalises the previous empty-current_branch fallback to also cover
        # the single-task case, and matches parallel-mode behaviour (which
        # already runs the main checkout on the default branch).
        # SR-2997 S1 sibling: the merge TARGET must be the operator-named
        # branch, never origin/HEAD (agent-writable) — fail closed otherwise.
        try:
            default_branch = get_trusted_default_branch(project_dir)
        except UntrustedDefaultBranchError as exc:
            print(
                f"  [Isolation] ERROR: Task #{task_id}: no trusted merge "
                f"target: {exc}"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = f"no trusted merge target: {exc}"
            return False
        # dispatch-06 (task #3112): never stash the operator's uncommitted
        # work to make room for a merge. The old stash / merge / stash-pop
        # sequence stranded the edits in `git stash` when the orchestrator
        # was killed in between. A dirty main checkout is refused instead.
        dirty_reason = await main_checkout_dirty_reason(project_dir)
        if dirty_reason:
            print(
                f"  [Isolation] Merge REFUSED for task #{task_id}: "
                f"{dirty_reason}"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = f"main checkout is not clean: {dirty_reason}"
            return False
        current = await git_run_async(
            ["branch", "--show-current"], project_dir, timeout=10,
        )
        current_branch = current.stdout.strip()
        if current_branch != default_branch:
            checkout_res = await git_run_async(
                ["checkout", default_branch], project_dir, timeout=30,
            )
            if checkout_res.returncode != 0:
                print(
                    f"  [Isolation] ERROR: Task #{task_id}: could not check "
                    f"out default branch '{default_branch}' before merge "
                    f"(was on '{current_branch or 'DETACHED'}'): "
                    f"{checkout_res.stderr[:200]}"
                )
                print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
                record.reason = (
                    f"could not check out default branch '{default_branch}'"
                )
                return False
            current_branch = default_branch
        print(f"  [Isolation] Merging on branch: {current_branch} in {project_dir}")

        pre_head_res = await git_run_async(
            ["rev-parse", "HEAD"], project_dir, timeout=10,
        )
        pre_head = pre_head_res.stdout.strip()
        record.pre_head = pre_head

        # Task #2488: distinguish "branch missing" from "no commits ahead".
        # Previously a missing branch produced empty `log HEAD..<branch>`
        # output and was silently classified as "NO commits ahead — skipping
        # merge", masking the underlying worktree-isolation bug (agent
        # commits landing on the wrong branch). Surface it as a hard ERROR
        # so the operator can investigate rather than treating a missing
        # branch as a successful no-op.
        branch_check = await git_run_async(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            project_dir, timeout=10,
        )
        if branch_check.returncode != 0:
            print(
                f"  [Isolation] ERROR: Task #{task_id} branch "
                f"'{branch_name}' does NOT exist in {project_dir}. "
                f"This usually means the per-task worktree was never "
                f"created (task #2488 — branch reuse / no-isolation bug). "
                f"Agent commits may have landed on an unexpected branch. "
                f"Do NOT treat as 'no commits ahead' — investigate."
            )
            record.reason = f"branch '{branch_name}' does not exist"
            return False
        # gate-01 (task #3111): from here on the merge names a commit, never
        # the branch, so the branch moving mid-merge changes nothing.
        target_sha = merge_sha or branch_check.stdout.strip()
        print(
            f"  [Isolation] Task #{task_id}: merging commit {target_sha[:12]} "
            f"({'gate-approved' if merge_sha else 'pinned tip of'} "
            f"'{branch_name}')"
        )

        # Task #2493: count commits-ahead against the DEFAULT branch, not
        # HEAD. HEAD is now always the default branch here (checked out
        # above), but naming the default branch explicitly keeps the count
        # correct and intent-revealing regardless of which branch HEAD was on
        # at entry. Mirrors the <default>..<branch> rev-list used elsewhere
        # in this module.
        ahead = await git_run_async(
            ["log", "--oneline", f"{default_branch}..{target_sha}"],
            project_dir, timeout=15,
        )
        if not ahead.stdout.strip():
            print(
                f"  [Isolation] Task #{task_id}: branch '{branch_name}' has "
                f"NO commits ahead of '{default_branch}' — skipping merge"
            )
            record.reason = f"no commits ahead of '{default_branch}'"
            return False

        commits_ahead = len(ahead.stdout.strip().split("\n"))
        print(
            f"  [Isolation] Task #{task_id}: branch '{branch_name}' has "
            f"{commits_ahead} commit(s) to merge"
        )

        merge_result = await git_run_async(
            ["merge", "--no-edit", "-m",
             f"Merge {branch_name} at {target_sha[:12]} (task #{task_id})",
             target_sha],
            project_dir, timeout=60,
        )
        post_head_res = await git_run_async(
            ["rev-parse", "HEAD"], project_dir, timeout=10,
        )
        post_head = post_head_res.stdout.strip()

        if merge_result.returncode == 0 and post_head != pre_head:
            print(
                f"  [Isolation] Merged task #{task_id} into main "
                f"({pre_head[:8]} -> {post_head[:8]})"
            )
            record.merged_sha = target_sha
            record.post_head = post_head
            return True
        if merge_result.returncode == 0 and post_head == pre_head:
            print(
                f"  [Isolation] WARNING: Merge returned 0 for task "
                f"#{task_id} but HEAD unchanged ({pre_head[:8]})"
            )
            print(
                f"  [Isolation] Merge output: "
                f"{_git_output(merge_result)}"
            )
            record.reason = "merge returned 0 but the default branch did not move"
            return False

        # dispatch-17: git reports conflicts on stdout, so log both.
        merge_output = _git_output(merge_result)
        print(
            f"  [Isolation] Merge of task #{task_id} failed "
            f"(rc={merge_result.returncode}): {merge_output}"
        )
        # Task #3131: a conflict confined to declared generated files is
        # completed by regenerating them from the merged tree. Any other
        # conflict (or a project without the generator) is not "applicable"
        # and keeps the abort / rebase-fallback path below unchanged.
        # Task #3141 (I-01): only against the guard's pinned SHA, never the
        # re-read pre_head — without one, no generator is trusted.
        if shutdown_requested() is None and pinned_default_sha:
            resolution = await _resolve_generated_conflict(
                project_dir, task_id, branch_name, pre_head, target_sha,
                pinned_default_sha, default_branch,
            )
            if resolution.resolved:
                record.merged_sha = target_sha
                record.post_head = resolution.commit
                record.regenerated_paths = resolution.paths
                record.regenerated_blobs = dict(resolution.blobs)
                return True
            if resolution.applicable:
                await git_run_async(
                    ["merge", "--abort"], project_dir, timeout=15,
                )
                print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
                record.reason = (
                    f"conflict in generated file(s) "
                    f"{', '.join(resolution.paths)} not regenerated: "
                    f"{resolution.reason}"
                )
                return False
        await git_run_async(
            ["merge", "--abort"], project_dir, timeout=15,
        )
        if shutdown_requested() is not None:
            # dispatch-06: the operator asked the orchestrator to stop; do
            # not start the rebase fallback on the way out.
            print(
                f"  [Isolation] Merge of task #{task_id} aborted: shutdown "
                f"requested; rebase fallback skipped"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = f"merge interrupted by shutdown: {merge_output}"
            return False

        # dispatch-08: rebase the pinned commit onto the default branch in
        # the task's OWN worktree, then fast-forward the default branch
        # from the main checkout. The old in-place `rebase HEAD <branch>`
        # checked the task branch out here and then merged it into itself
        # ("Already up to date", rc 0), reporting a merge that never
        # happened.
        if not worktree_dir:
            print(
                f"  [Isolation] Merge FAILED for task #{task_id}: "
                f"{merge_output} (no task worktree to rebase in)"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = f"merge conflict: {merge_output}"
            return False
        wt_branch = await git_run_async(
            ["branch", "--show-current"], worktree_dir, timeout=10,
        )
        wt_head = await git_run_async(
            ["rev-parse", "HEAD"], worktree_dir, timeout=10,
        )
        if (
            wt_branch.stdout.strip() != branch_name
            or wt_head.stdout.strip() != target_sha
        ):
            print(
                f"  [Isolation] Merge FAILED for task #{task_id}: "
                f"{merge_output}; worktree {worktree_dir} is on "
                f"'{wt_branch.stdout.strip() or 'DETACHED'}' at "
                f"{wt_head.stdout.strip()[:12] or 'unknown'}, not "
                f"'{branch_name}' at {target_sha[:12]} — not rebasing"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = (
                f"merge conflict; worktree not at the approved commit: "
                f"{merge_output}"
            )
            return False
        rebase_result = await git_run_async(
            ["rebase", pre_head], worktree_dir, timeout=60,
        )
        if rebase_result.returncode != 0:
            await git_run_async(
                ["rebase", "--abort"], worktree_dir, timeout=15,
            )
            print(
                f"  [Isolation] Merge FAILED for task #{task_id}: "
                f"{merge_output}; rebase onto {pre_head[:8]} also failed: "
                f"{_git_output(rebase_result)}"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
            record.reason = f"merge and rebase conflict: {merge_output}"
            return False
        rebased = await git_run_async(
            ["rev-parse", "HEAD"], worktree_dir, timeout=10,
        )
        rebased_sha = rebased.stdout.strip()
        # MI-03 (task #3116): the worktree HEAD is agent-writable, so the
        # SHA read above is only merged once its range is verified to be
        # exactly the approved commits; from here on only that SHA is used.
        range_problem = await rebased_range_problem(
            project_dir, pre_head, target_sha, rebased_sha,
        )
        if range_problem:
            print(
                f"  [Isolation] Merge FAILED for task #{task_id}: "
                f"{merge_output}; rebased {rebased_sha[:12] or 'unknown'} "
                f"is not the approved commits: {range_problem}"
            )
            print(f"  [Isolation] Branch '{branch_name}' PRESERVED (rebased)")
            record.reason = (
                f"rebased commits are not the approved commits: {range_problem}"
            )
            return False
        fast_forward = await git_run_async(
            ["merge", "--ff-only", rebased_sha], project_dir, timeout=60,
        )
        ff_head_res = await git_run_async(
            ["rev-parse", "HEAD"], project_dir, timeout=10,
        )
        ff_head = ff_head_res.stdout.strip()
        if (
            fast_forward.returncode == 0
            and rebased_sha
            and ff_head == rebased_sha
            and ff_head != pre_head
        ):
            print(
                f"  [Isolation] Merged task #{task_id} after rebase "
                f"({pre_head[:8]} -> {ff_head[:8]}, rebased from "
                f"{target_sha[:8]})"
            )
            record.merged_sha = rebased_sha
            record.post_head = ff_head
            return True
        print(
            f"  [Isolation] Merge FAILED for task #{task_id}: "
            f"fast-forward to rebased {rebased_sha[:8] or 'unknown'} did "
            f"not move '{default_branch}' ({pre_head[:8]} -> "
            f"{ff_head[:8]}): {_git_output(fast_forward)}"
        )
        print(f"  [Isolation] Branch '{branch_name}' PRESERVED (rebased)")
        record.reason = "fast-forward after rebase did not move the default branch"
        return False
    except (subprocess.SubprocessError, OSError) as e:
        # Explicit error log — do NOT silently swallow. Branch is preserved
        # because we did not add it to the merged set.
        print(f"  [Isolation] Merge error for task #{task_id}: {e}")
        print(f"  [Isolation] Branch '{branch_name}' PRESERVED (merge errored)")
        record.reason = f"merge errored: {e}"
        return False


async def _resolve_generated_conflict(
    project_dir: str,
    task_id: int,
    branch_name: str,
    pre_head: str,
    target_sha: str,
    pinned_sha: str,
    default_branch: str | None = None,
) -> ConflictResolution:
    """Try the task #3131 generated-file resolution of a conflicted merge.

    Runs in the main checkout while ``git merge`` of ``target_sha`` into
    ``pre_head`` is in progress; see :mod:`equipa.generated_files`. Logs a
    GATE-AUDIT line naming the files whenever the resolution applies. A git
    or OS error while resolving is a refusal, so the caller aborts the merge.

    ``pinned_sha`` is the default-branch SHA the run's guard pinned: every
    generator blob comparison uses it, and the resolution is refused unless
    ``pre_head`` (and HEAD mid-merge) equal it (task #3141, I-01).
    """
    try:
        resolution = await resolve_generated_conflicts(
            project_dir,
            ours=pinned_sha,
            theirs=target_sha,
            head_before_merge=pre_head,
            message=(
                f"Merge {branch_name} at {target_sha[:12]} (task #{task_id})\n\n"
                f"Conflict in generated file(s) resolved by regenerating them "
                f"from the merged tree (task #3131)."
            ),
            default_branch=default_branch,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        resolution = ConflictResolution(
            True, (), None, f"generated-file resolution errored: {exc}",
        )
    except Exception as exc:
        # The main checkout is mid-merge here. Any unexpected error must
        # still reach the caller's ``merge --abort``, never unwind past it.
        logger.exception(
            "[Generated-Files] resolution of task #%s errored mid-merge", task_id,
        )
        resolution = ConflictResolution(
            True, (), None,
            f"generated-file resolution errored: {type(exc).__name__}: {exc}",
        )
    if not resolution.applicable:
        return resolution
    files = ",".join(resolution.paths) or "unknown"
    if resolution.resolved:
        print(
            f"  [Isolation] Task #{task_id}: merge conflicted only in generated "
            f"file(s) {files}; regenerated from the merged tree and committed "
            f"{resolution.commit[:12]}"
        )
        _gate_audit_log(
            f"task={task_id} event=generated-files-regenerated files={files} "
            f"branch={branch_name} sha={target_sha} before={pre_head} "
            f"merge_commit={resolution.commit}",
            task_id=task_id,
            event="generated-files-regenerated",
        )
    else:
        print(
            f"  [Isolation] Merge FAILED for task #{task_id}: conflict in "
            f"generated file(s) {files} not regenerated: {resolution.reason}"
        )
        _gate_audit_log(
            f"task={task_id} event=generated-files-not-regenerated "
            f"files={files} branch={branch_name} sha={target_sha} "
            f"pinned={pinned_sha} before={pre_head} "
            f"reason={resolution.reason}",
            task_id=task_id,
            event="generated-files-not-regenerated",
        )
    return resolution


def _git_output(result: subprocess.CompletedProcess, limit: int = 400) -> str:
    """stdout and stderr of a git call on one line, for failure logs.

    dispatch-17: ``git merge`` writes conflict details to stdout and leaves
    stderr empty, so logging stderr alone printed an empty reason.
    """
    parts = [
        f"{label}: {' '.join(text.split())}"
        for label, text in (("stdout", result.stdout), ("stderr", result.stderr))
        if text and text.strip()
    ]
    combined = " | ".join(parts) or "(no output)"
    return combined[:limit]


async def _stash_uncommitted_in_worktree(
    wt_path: str,
    task_id: int,
    branch_name: str,
) -> None:
    """Stash any uncommitted changes inside ``wt_path`` onto its branch.

    Runs inside the worktree itself so the stash lands on ``branch_name``'s
    HEAD. Includes untracked files (``-u``) so newly-created agent files
    are preserved. Stash message is tagged so it can be located later by
    rescue tooling: ``equipa-early-term task-<id>``.

    Silent failure paths (missing git, no changes, locked index) are
    logged but never raised — cleanup must continue even if the stash
    cannot be saved.
    """
    if not Path(wt_path).exists():
        return
    try:
        status = await git_run_async(
            ["status", "--porcelain", "--ignore-submodules=all"], wt_path, timeout=15,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return
        stash_msg = f"equipa-early-term task-{task_id} branch-{branch_name}"
        result = await git_run_async(
            ["stash", "push", "-u", "-m", stash_msg],
            wt_path, timeout=30,
        )
        if result.returncode == 0 and "No local changes" not in result.stdout:
            print(
                f"  [Isolation] Task #{task_id}: stashed uncommitted work "
                f"on '{branch_name}' as '{stash_msg}'"
            )
    except (subprocess.SubprocessError, OSError) as e:
        print(
            f"  [Isolation] Could not stash uncommitted work for task "
            f"#{task_id} on '{branch_name}': {e}"
        )


async def _cleanup_worktrees(
    project_dir: str,
    worktree_dirs: dict[int, str],
    merged_tasks: set[int],
    worktree_base: Path,
) -> None:
    """Remove worktree directories; delete merged branches; preserve unmerged.

    Per-task failures are logged (not silently swallowed) so that data-loss
    investigations have evidence of which step failed.

    Uses ``git_run_async`` so the per-task ``git worktree remove`` and
    ``git branch -D`` calls do not block the event loop.
    """
    for task_id, wt_path in worktree_dirs.items():
        branch_name = f"forge-task-{task_id}"
        try:
            state_file = Path(wt_path) / ".forge-state.json"
            if state_file.exists():
                state_file.unlink()
            # Preserve uncommitted work on UNMERGED branches before the
            # `git worktree remove --force` discards the working tree.
            # Without this, an early_terminated agent that wrote 32 turns of
            # edits but did not commit loses everything when the worktree is
            # torn down. Stash on the worktree's own branch (HEAD) so the
            # work can be salvaged later by checking out the branch and
            # running `git stash pop`.
            if task_id not in merged_tasks:
                await _stash_uncommitted_in_worktree(
                    wt_path, task_id, branch_name,
                )
            await git_run_async(
                ["worktree", "remove", "--force", wt_path],
                project_dir, timeout=30,
            )
            if task_id in merged_tasks:
                await git_run_async(
                    ["branch", "-D", branch_name], project_dir, timeout=10,
                )
            else:
                print(
                    f"  [Isolation] Keeping branch '{branch_name}' "
                    f"(unmerged work)"
                )
        except (subprocess.SubprocessError, OSError) as e:
            print(
                f"  [Isolation] Cleanup error for task #{task_id} "
                f"(branch '{branch_name}'): {e}"
            )
    # Clean up worktree base dir if empty (rmdir on non-empty dir raises
    # OSError — that's expected when other worktrees exist, not an error).
    try:
        worktree_base.rmdir()
    except OSError:
        pass


def _security_review_blocks_merge(
    project_dir: str,
    task_id: int,
    *,
    block_on_missing: bool = True,
) -> tuple[bool, dict | None]:
    """Return (blocks_merge, counts) by reading SECURITY-REVIEW-{task_id}.md.

    blocks_merge is True when:
      * the artifact exists AND reports at least one CRITICAL or HIGH
        finding, OR
      * the artifact is MISSING and ``block_on_missing`` is True
        (fail-closed; default).

    The fail-closed default is task 2341's S1 hardening over the original
    bug-2321 fix: a reviewer agent that crashes, times out, or is steered
    off-task by content in the task description can satisfy the
    pre-2341 (False, None) contract simply by not writing the artifact —
    same shape as the original 2321 vulnerability, narrower scope.
    Operators who need the legacy fail-open behaviour can disable the
    ``security_review_block_on_missing_artifact`` feature flag.

    CAVEAT (task #2706): disabling that flag relaxes ONLY this policy layer.
    For a non-doc (code) diff the unified gate derives ``expect_artifact=True``,
    so the defensive invariant in :func:`_merge_task_branch` still re-reads the
    artifact and fails closed on a missing one — the flag is effectively inert
    for code diffs and only takes effect on doc-only / review-disabled paths
    (where no artifact is expected). See ``decide_merge_gate`` for the full
    layering note.
    """
    # Task 2476: read from .equipa-artifacts/ first, then fall back to
    # the legacy repo-root path so in-flight artifacts still parse.
    review_path = find_review_artifact(project_dir, "SECURITY-REVIEW", task_id)
    # Task #3041: only an artifact written by THIS cycle's reviewer run is
    # parsed. A failed/timed-out reviewer, or a file the reviewer did not
    # write (a developer self-review on the branch, a stale prior review),
    # blocks REGARDLESS of block_on_missing — that flag is about a missing
    # review, and an untrusted file is not a review.
    # Task #3063: no reviewer record for this cycle BLOCKS (SR41-03), and the
    # counts are parsed from the same bytes provenance hashed (SR41-02).
    provenance = verify_reviewer_provenance(task_id, review_path)
    counts = (
        _count_findings_in_review_file(
            review_path, task_id=task_id, text=provenance.text,
        )
        if provenance.trusted and provenance.text is not None else None
    )
    _gate_audit_log(
        f"task={task_id} event=blocks-merge-eval "
        f"artifact_exists={provenance.fingerprint.exists} "
        f"{provenance.fingerprint.describe()} "
        f"provenance={provenance.reason} {provenance.describe_reviewer()} "
        f"{format_counts(counts)} block_on_missing={block_on_missing}",
        task_id=task_id,
        event="blocks-merge-eval",
        counts=counts,
    )
    if not provenance.trusted:
        _gate_audit_log(
            f"task={task_id} event={provenance.audit_event} "
            f"reason={provenance.reason} {provenance.fingerprint.describe()} "
            f"— merge blocked regardless of artifact contents",
            task_id=task_id,
            event=provenance.audit_event,
        )
        return True, None
    if counts is None:
        if block_on_missing:
            logger.warning(
                "[security-review] missing artifact %s — gating merge "
                "(fail-closed). Disable features."
                "security_review_block_on_missing_artifact to allow "
                "legacy fail-open behaviour.",
                review_path,
            )
            return True, None
        logger.warning(
            "[security-review] missing artifact %s — fail-open mode "
            "(features.security_review_block_on_missing_artifact=False); "
            "merge will proceed without findings data.",
            review_path,
        )
        return False, None
    blocks = counts.get("CRITICAL", 0) > 0 or counts.get("HIGH", 0) > 0
    return blocks, counts


@dataclass(frozen=True)
class _HazardScan:
    """Hazards found and the resolved directories that were scanned."""

    hazards: list[str]
    directories: frozenset[Path]


async def _collect_repo_hazards(
    directories: list[str | None],
    *,
    already_scanned: frozenset[Path] = frozenset(),
) -> _HazardScan:
    """:func:`find_repo_execution_hazards` for each directory, deduplicated.

    ``None`` entries and directories in ``already_scanned`` (compared
    resolved) are skipped.
    """
    hazards: list[str] = []
    scanned = set(already_scanned)
    for directory in directories:
        if directory is None:
            continue
        resolved = Path(directory).resolve()
        if resolved in scanned:
            continue
        scanned.add(resolved)
        for hazard in await find_repo_execution_hazards(directory):
            if hazard not in hazards:
                hazards.append(hazard)
    return _HazardScan(hazards, frozenset(scanned))


async def _git_common_dir(directory: str | os.PathLike) -> Path | None:
    """Resolved git common dir of ``directory``, None when git cannot say."""
    try:
        result = await git_run_async(
            ["rev-parse", "--git-common-dir"], directory, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("[git] no common dir for %s: %s", directory, exc)
        return None
    printed = (result.stdout or "").strip()
    if result.returncode != 0 or not printed:
        return None
    common = Path(printed)
    if not common.is_absolute():
        common = Path(directory) / common
    return common.resolve()


async def _common_dir_mismatch(
    repo_root: str | os.PathLike, worktree_root: str | os.PathLike,
) -> str | None:
    """Why ``worktree_root`` is not a worktree of ``repo_root``'s repository."""
    repo_common = await _git_common_dir(repo_root)
    worktree_common = await _git_common_dir(worktree_root)
    if repo_common is None or worktree_common is None:
        return (
            f"could not locate the git common dir of {repo_root} or "
            f"{worktree_root}"
        )
    if repo_common != worktree_common:
        return (
            f"{repo_root} uses the repository at {repo_common} but its task "
            f"worktree {worktree_root} uses {worktree_common} (a .git file or "
            f"similar GIT_DIR redirect)"
        )
    return None


@dataclass
class _MergePins:
    """Pinned repositories for one merge and the descriptors they hold."""
    repositories: list[PinnedGitRepository]
    fds: list[int]

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()


async def _pin_merge_repositories(
    guard: DefaultBranchGuard, work_tree: str, worktree_dir: str | None,
) -> _MergePins:
    """Open the repositories the merge must use, as recorded at the snapshot.

    R3146-01 (task #3151): ``guard.verify`` proved the identity a moment ago,
    but git would discover the repository again on every call, so a ``.git``
    swapped in between redirected the merge (and its filter drivers) into
    another repository. The main checkout's git dir and common dir are
    opened by their snapshot realpaths and must have their snapshot inodes.
    The task worktree's git dir is opened INSIDE the pinned common dir (its
    ``worktrees/<name>`` entry, no symlink), so it cannot be elsewhere; the
    merge then checks its branch and HEAD through that pin.

    git's files ref backend reads refs through the ``commondir`` file of the
    git dir, whatever ``GIT_COMMON_DIR`` says (git 2.43); config, objects and
    so every driver do follow the pin. A ``commondir`` that does not name the
    pinned common dir is refused here. One planted during the merge can move
    only refs, never run another repository's drivers, and the post-merge
    identity check (``record_merge``) trips on it.

    A guard without an identity (hand-built or a test double) pins nothing.
    Raises :class:`PinnedRepositoryError` when anything does not match.
    """
    identity = getattr(guard, "identity", None)
    if identity is None:
        return _MergePins([], [])
    if os.path.realpath(work_tree) != identity.work_tree:
        raise PinnedRepositoryError(
            f"the merge would run in {work_tree}, not in the work tree "
            f"{identity.work_tree} pinned at the snapshot"
        )
    if not fd_pinning_available():
        return await _pin_merge_repositories_by_path(identity, work_tree, worktree_dir)
    pins = _MergePins([], [])
    try:
        common_fd = open_pinned_directory(identity.common_dir, identity.common_dir_id)
        pins.fds.append(common_fd)
        git_dir_fd = common_fd
        if identity.git_dir_id != identity.common_dir_id:
            git_dir_fd = open_pinned_directory(identity.git_dir, identity.git_dir_id)
            pins.fds.append(git_dir_fd)
        _check_commondir_file(
            git_dir_fd, identity.git_dir, identity.common_dir,
            linked=git_dir_fd != common_fd,
        )
        # Keyed by the path the merge passes (F2, task #3155); its realpath
        # was checked against the snapshot above.
        pins.repositories.append(pinned_repository(
            work_tree, git_dir_fd, common_fd,
            git_dir=identity.git_dir, common_dir=identity.common_dir,
        ))
        if worktree_dir is not None:
            admin = await _worktree_admin_name(worktree_dir, identity.common_dir)
            worktrees_fd = open_pinned_directory("worktrees", None, dir_fd=common_fd)
            try:
                admin_fd = open_pinned_directory(admin, None, dir_fd=worktrees_fd)
            finally:
                os.close(worktrees_fd)
            pins.fds.append(admin_fd)
            admin_dir = os.path.join(identity.common_dir, "worktrees", admin)
            _check_commondir_file(admin_fd, admin_dir, identity.common_dir, linked=True)
            pins.repositories.append(pinned_repository(
                worktree_dir, admin_fd, common_fd,
                git_dir=admin_dir, common_dir=identity.common_dir,
            ))
    except BaseException:
        pins.close()
        raise
    return pins


async def _pin_merge_repositories_by_path(
    identity: RepositoryIdentity, work_tree: str, worktree_dir: str | None,
) -> _MergePins:
    """:func:`_pin_merge_repositories` without ``/proc/self/fd``.

    git is given the snapshot realpaths after an inode check. That is never
    a fresh discovery, but a rename between the check and a git call still
    redirects it; the descriptor pin is the rename-proof form.
    """
    check_pinned_directory(identity.common_dir, identity.common_dir_id)
    check_pinned_directory(identity.git_dir, identity.git_dir_id)
    _check_commondir_file(
        None, identity.git_dir, identity.common_dir,
        linked=identity.git_dir_id != identity.common_dir_id,
    )
    repositories = [PinnedGitRepository(
        identity.work_tree, identity.git_dir, identity.common_dir, path=work_tree,
    )]
    if worktree_dir is not None:
        admin = await _worktree_admin_name(worktree_dir, identity.common_dir)
        admin_dir = os.path.join(identity.common_dir, "worktrees", admin)
        _check_commondir_file(None, admin_dir, identity.common_dir, linked=True)
        repositories.append(PinnedGitRepository(
            os.path.realpath(worktree_dir), admin_dir, identity.common_dir,
            path=worktree_dir,
        ))
    return _MergePins(repositories, [])


# git writes a worktree's commondir as a short relative path ("../..").
_COMMONDIR_READ_LIMIT = 4096


def _check_commondir_file(
    git_dir_fd: int | None, git_dir: str, common_dir: str, *, linked: bool,
) -> None:
    """Refuse a ``commondir`` file in the pinned git dir that does not name
    ``common_dir``. A repository's own git dir (``linked=False``) has none;
    a linked worktree's names the common dir, usually relatively. Read
    through ``git_dir_fd`` when given, else by path.

    F3 (task #3155): the agent can plant a FIFO or a directory under that
    name. Only a regular file is read (``lstat`` first, then the opened
    descriptor must be that same regular file); the open never blocks, and
    every OS error is a :class:`PinnedRepositoryError`, so a plant blocks
    the merge instead of hanging the event loop or crashing the run.
    """
    target = "commondir" if git_dir_fd is not None else os.path.join(git_dir, "commondir")
    try:
        planted = os.stat(target, dir_fd=git_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if linked:
            raise PinnedRepositoryError(
                f"the worktree git dir {git_dir} has no commondir file"
            ) from None
        return
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot read {git_dir}/commondir: {exc.strerror}"
        ) from exc
    if not stat.S_ISREG(planted.st_mode):
        raise PinnedRepositoryError(
            f"{git_dir}/commondir is not a regular file "
            f"(mode {stat.filemode(planted.st_mode)})"
        )
    if planted.st_size > _COMMONDIR_READ_LIMIT:
        raise PinnedRepositoryError(
            f"{git_dir}/commondir is {planted.st_size} bytes, longer than any "
            f"common dir path git writes"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags, dir_fd=git_dir_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (planted.st_dev, planted.st_ino):
                raise PinnedRepositoryError(
                    f"{git_dir}/commondir was replaced while it was being read"
                )
            content = os.read(fd, _COMMONDIR_READ_LIMIT + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot read {git_dir}/commondir: {exc.strerror}"
        ) from exc
    named = content.decode("utf-8", "replace").rstrip("\r\n")
    if not linked or os.path.normpath(os.path.join(git_dir, named)) != common_dir:
        raise PinnedRepositoryError(
            f"{git_dir}/commondir names {named[:200]!r}, not the pinned git "
            f"common dir {common_dir}"
        )


async def _worktree_admin_name(worktree_dir: str, common_dir: str) -> str:
    """Name of ``worktree_dir``'s git dir under ``<common_dir>/worktrees``."""
    try:
        result = await git_run_async(
            ["rev-parse", "--absolute-git-dir"], worktree_dir, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise PinnedRepositoryError(
            f"cannot read the git dir of the task worktree {worktree_dir}: {exc}"
        ) from exc
    printed = (result.stdout or "").strip()
    admin = os.path.realpath(printed) if result.returncode == 0 and printed else ""
    if os.path.dirname(admin) != os.path.join(common_dir, "worktrees"):
        raise PinnedRepositoryError(
            f"the task worktree {worktree_dir} uses the git dir "
            f"{admin or 'none'}, not one of the worktrees of the pinned "
            f"repository {common_dir}"
        )
    return os.path.basename(admin)


async def _gated_merge_task(
    *,
    repo: str | os.PathLike,
    branch: str,
    outcome: str,
    task_id: int,
    project_context: dict | None = None,
    security_review_enabled: bool = True,
    block_on_missing: bool = True,
    guard: DefaultBranchGuard | None = None,
    worktree_dir: str | None = None,
) -> str:
    """Unified, gated merge entry point used by BOTH dispatch modes.

    Task #3111 — merge integrity, in this order:

      1. fail closed on repository hazards (``refs/replace/*``, local config
         defining filter / merge / diff drivers, ``core.worktree`` or a
         conditional include) BEFORE any gate evaluation;
      2. ``guard`` (the run's :class:`DefaultBranchGuard`, snapshotted before
         dispatch) must still see the default branch at its expected SHA;
      3. the task branch is pinned to ONE commit; the gate diffs that commit,
         and for a reviewed (code) diff it must equal the reviewer's recorded
         ``reviewed_sha`` — otherwise ``blocked`` ("branch moved after
         review"). The merge then names that SHA, never the branch;
      4. after the merge the guard checks the new tip is exactly this merge
         and advances its expected-SHA chain; any other movement trips the
         guard and every later merge in the run is refused.

    The per-task result (status, reason, merged SHA) is stored in
    ``guard.outcomes[task_id]`` so callers can write the task status only
    after the merge. Without a ``guard`` one is snapshotted here, which still
    covers the window around this merge. ``worktree_dir`` enables the
    dispatch-08 rebase fallback.

    Task #2451: single-task ``--dev-test`` (``cli.run_mode_task``) and
    parallel ``--tasks`` (``run_parallel_tasks``) both funnel through this
    helper so the security gate cannot be bypassed by either path.

    Task #2706 — SINGLE GateDecision, no caller-trust hole. Previously this
    helper trusted two caller-supplied signals — a tri-state
    ``review_blocks_merge`` and an ``expect_artifact`` doc-only hint — whose
    correct combination lived only in prose comments. The
    ``expect_artifact=False`` short-circuit skipped the fail-closed
    invariant entirely, so a caller passing it wrongly silently disabled the
    last line of defence. Both parameters are now REMOVED. This helper
    computes a single :class:`GateDecision` INSIDE itself from ground truth:

      * the ACTUAL branch diff — ``get_changed_files_for_branch`` diffs the
        ``forge-task-<id>`` ref against the default branch (same result in
        single-task and parallel modes because worktrees share one ref
        store), never a caller flag.
      * doc-only-ness re-derived by calling the EXISTING
        ``security_gate.is_doc_only_diff`` on that real file list (it fails
        closed on an empty list — a failed/empty diff is treated as code,
        never as doc-only).
      * the on-disk ``SECURITY-REVIEW-<id>.md`` artifact via
        ``_security_review_blocks_merge`` (fail-closed on missing when
        ``block_on_missing``).

    ``expect_artifact`` for the defensive invariant is derived from
    ``decision.doc_only`` (task #2451/#2488/#2493 provenance preserved): the
    invariant inside ``_merge_task_branch`` still re-reads the artifact and
    still raises ``SecurityGateBypassError`` on HIGH/CRITICAL/missing.
    ``security_review_enabled`` and ``block_on_missing`` are GLOBAL operator
    feature-flag policy (derived at the call sites from ``args`` /
    ``dispatch_config``), NOT per-task caller trust signals — they are the
    same operator-config trust boundary as today, threaded through so
    unification does not silently change behaviour. ``security_review_
    enabled=False`` is the operator's explicit opt-out (no artifact expected,
    gate does not block); ``block_on_missing`` is the ``security_review_
    block_on_missing_artifact`` fail-open escape hatch. Both default
    fail-closed.

    Returns one of:
      * ``"skipped"``  — outcome is not merge-eligible (e.g. tests failed).
      * ``"blocked"``  — gate fired; branch left intact, no merge attempted.
      * ``"merged"``   — branch merged into master, HEAD advanced.
      * ``"merge_failed"`` — gate passed but ``_merge_task_branch`` returned
        False (conflict, etc.). Branch preserved.
      * ``"noop"`` — the approved commit is already on the default branch
        (the task produced no commits); nothing to merge (task #3111).
    """
    project_dir = os.fspath(repo)
    run_guard = guard

    def finish(status: str, reason: str, merged_sha: str | None = None) -> str:
        if run_guard is not None:
            run_guard.outcomes[task_id] = MergeOutcome(status, reason, merged_sha)
        return status

    # An outcome already demoted to ``security_review_blocked`` upstream is
    # honoured as blocked (the gate fired) — this is strictly stricter and
    # keeps the operator-facing "blocked vs skipped" distinction. Note the
    # ground-truth GateDecision below would ALSO block a HIGH artifact even
    # if the outcome were (wrongly) left at tests_passed, so this branch is
    # a convenience/audit refinement, not the trust anchor.
    if outcome == "security_review_blocked":
        _gate_audit_log(
            f"task={task_id} event=merge-skipped reason=outcome-security-blocked",
            task_id=task_id,
            event="merge-skipped",
        )
        return finish("blocked", "security review blocked the merge")

    if outcome not in ("tests_passed", "no_tests"):
        _gate_audit_log(
            f"task={task_id} event=merge-skipped reason=outcome-not-eligible "
            f"outcome={outcome}",
            task_id=task_id,
            event="merge-skipped",
        )
        return finish("skipped", f"outcome {outcome} is not merge-eligible")

    def refuse_hazards(hazards: list[str]) -> str:
        detail = "; ".join(hazards)
        print(
            f"  [Merge-Integrity] Task #{task_id}: refusing to gate or merge "
            f"— {detail}"
        )
        _gate_audit_log(
            f"task={task_id} event=repo-hazard-blocked detail={detail}",
            task_id=task_id,
            event="repo-hazard-blocked",
        )
        return finish("blocked", f"repository hazard: {detail}")

    # Task #3111: repository state the hardened git helper cannot neutralise
    # blocks before ANY gate evaluation — the reviewer agent's git honours
    # replace refs, and driver config runs programs during the merge. The
    # task worktree is scanned too: the rebase fallback runs git there, and
    # its own config.worktree is invisible from the main checkout.
    #
    # IND-02 (task #3132): the scan runs on the OPERATOR's paths BEFORE any
    # work-tree root is resolved. ``rev-parse --show-toplevel`` follows an
    # agent-written ``core.worktree``; scanning the resolved root scanned
    # whichever repository the agent named, and the gate and the merge then
    # ran there while this project's task was recorded merged.
    scanned = await _collect_repo_hazards([project_dir, worktree_dir])
    if scanned.hazards:
        return refuse_hazards(scanned.hazards)

    # R3119-01 (task #3126): every git call of the gate and the merge runs
    # at the work-tree ROOT. A project nested in a sub-directory of its
    # repository keeps ``project_dir`` only for its review artifact; from
    # the sub-directory, agent-written config could narrow the gate diff.
    # git_toplevel_async names no root that does not contain the directory
    # (IND-02, task #3132).
    repo_root = await git_toplevel_async(project_dir)
    worktree_root = (
        await git_toplevel_async(worktree_dir) if worktree_dir is not None else None
    )
    if repo_root is None or (worktree_dir is not None and worktree_root is None):
        unreadable = project_dir if repo_root is None else worktree_dir
        _gate_audit_log(
            f"task={task_id} event=merge-skipped reason=no-work-tree-root "
            f"dir={unreadable}",
            task_id=task_id,
            event="merge-skipped",
        )
        return finish("blocked", f"no readable git work tree at {unreadable}")
    git_dir = str(repo_root)
    git_worktree_dir = str(worktree_root) if worktree_root is not None else None

    # IND-02 (task #3132): a ``.git`` file planted in a nested project's
    # directory (a GIT_DIR-style redirect) makes that directory the root of
    # ANOTHER repository. The orchestrator made the task worktree from the
    # project's repository, so both must share one git common dir.
    if worktree_root is not None:
        mismatch = await _common_dir_mismatch(repo_root, worktree_root)
        if mismatch:
            return refuse_hazards([mismatch])

    # Roots not scanned above (a nested project's) are scanned as well.
    root_hazards = await _collect_repo_hazards(
        [git_dir, git_worktree_dir], already_scanned=scanned.directories,
    )
    if root_hazards.hazards:
        return refuse_hazards(root_hazards.hazards)

    if guard is None:
        try:
            guard = await DefaultBranchGuard.snapshot(git_dir)
        except MergeIntegrityError as exc:
            _gate_audit_log(
                f"task={task_id} event=merge-skipped "
                f"reason=default-branch-unpinned detail={exc}",
                task_id=task_id,
                event="merge-skipped",
            )
            return finish("blocked", f"default branch could not be pinned: {exc}")
    # IND3132-01 (task #3146): the repository the gate is about to diff and
    # merge in, and the task worktree, must be the one the guard pinned
    # before any agent ran.
    if not await guard.verify(
        f"pre-gate task={task_id}", task_id=task_id,
        directories=(git_dir, git_worktree_dir),
    ):
        return finish("blocked", guard.alert or "default branch moved")

    # gate-01: pin the branch to ONE commit. The gate diffs that commit and
    # the merge names it, so the branch moving afterwards changes nothing.
    branch_sha = await resolve_commit(git_dir, f"refs/heads/{branch}")

    # === Single GateDecision, computed from ground truth (task #2706) ===
    # Diff the task BRANCH ref (not HEAD) so the file list is identical in
    # single-task mode (main checkout on the task branch) and parallel mode
    # (main checkout on the default branch, work on a shared branch ref).
    # base_ref omitted -> auto-detect default branch (#2479).
    changed_files = await get_changed_files_for_branch(
        git_dir, head_ref=branch_sha or branch,
    )
    decision: GateDecision = decide_merge_gate(
        changed_files,
        security_review_blocks_merge=_security_review_blocks_merge,
        project_dir=project_dir,
        task_id=task_id,
        security_review_enabled=security_review_enabled,
        block_on_missing=block_on_missing,
    )
    _gate_audit_log(
        f"task={task_id} event=gate-decision reason={decision.reason} "
        f"doc_only={decision.doc_only} blocks={decision.blocks_merge} "
        f"files={len(decision.changed_files)} "
        f"{format_counts(decision.counts)}",
        task_id=task_id,
        event="gate-decision",
        counts=decision.counts,
    )
    if decision.blocks_merge:
        _gate_audit_log(
            f"task={task_id} event=merge-skipped "
            f"reason={decision.reason} {format_counts(decision.counts)}",
            task_id=task_id,
            event="merge-skipped",
            counts=decision.counts,
        )
        return finish("blocked", f"security gate: {decision.reason}")

    merge_sha = branch_sha
    if decision.expect_artifact:
        # A reviewed (code) diff merges exactly the commit the reviewer read.
        review_record = get_reviewer_run(task_id)
        if review_record is not None:
            refusal = reviewed_commit_refusal(review_record, branch_sha)
        elif unrecorded_reviewer_runs_permitted():
            refusal = None  # hermetic tests only: no reviewer ran in-process
        else:
            refusal = "no reviewer run recorded"
        if refusal is not None:
            print(
                f"  [Merge-Integrity] Task #{task_id}: refusing to merge "
                f"'{branch}' — {refusal}"
            )
            _gate_audit_log(
                f"task={task_id} event=reviewed-sha-refused branch={branch} "
                f"branch_sha={branch_sha or 'MISSING'} reason={refusal}",
                task_id=task_id,
                event="reviewed-sha-refused",
            )
            return finish("blocked", refusal)
        if review_record is not None:
            merge_sha = review_record.reviewed_sha

    if merge_sha is not None and await is_ancestor(
        git_dir, merge_sha, guard.expected_sha,
    ):
        _gate_audit_log(
            f"task={task_id} event=merge-noop branch={branch} "
            f"sha={merge_sha} default={guard.default_branch}@"
            f"{guard.expected_sha[:12]}",
            task_id=task_id,
            event="merge-noop",
        )
        print(
            f"  [Isolation] Task #{task_id}: {merge_sha[:12]} is already on "
            f"'{guard.default_branch}' — nothing to merge"
        )
        return finish("noop", "approved commit already on the default branch", merge_sha)

    if not await guard.verify(
        f"pre-merge task={task_id}", task_id=task_id,
        directories=(git_dir, git_worktree_dir),
    ):
        return finish("blocked", guard.alert or "default branch moved")
    shutdown_signal = shutdown_requested()
    if shutdown_signal is not None:
        # dispatch-06: a SIGTERM/SIGINT deferred during an earlier merge
        # stops every later merge; the branch is kept for the operator.
        reason = (
            f"orchestrator shutting down "
            f"({signal.Signals(shutdown_signal).name}); merge not started"
        )
        _gate_audit_log(
            f"task={task_id} event=merge-skipped reason=shutdown-requested "
            f"branch={branch}",
            task_id=task_id,
            event="merge-skipped",
        )
        return finish("merge_failed", reason)
    # R3146-01 (task #3151): from here on git never discovers the repository.
    # The pinned git dirs are opened and inode-checked now; every merge-path
    # git call in the main checkout or the task worktree runs on them.
    try:
        pins = await _pin_merge_repositories(guard, git_dir, git_worktree_dir)
    except PinnedRepositoryError as exc:
        guard.trip(
            f"pre-merge task={task_id}", await guard.current_sha(),
            task_id=task_id, detail=str(exc),
        )
        return finish("blocked", guard.alert or str(exc))
    _gate_audit_log(
        f"task={task_id} event=merge-attempt branch={branch} "
        f"sha={merge_sha or 'MISSING'} doc_only={decision.doc_only} "
        f"default={guard.default_branch}@{guard.expected_sha[:12]}",
        task_id=task_id,
        event="merge-attempt",
    )
    attempt = MergeAttempt()
    try:
        # expect_artifact is DERIVED from the ground-truth decision, never a
        # caller flag. Doc-only diffs (re-derived from the real diff) skip
        # the artifact requirement; everything else demands it fail-closed.
        # dispatch-06: SIGTERM/SIGINT are deferred for the merge, so it
        # finishes or is aborted before the orchestrator acts on them.
        with git_repositories_pinned(*pins.repositories):
            async with MergeSignalShield(git_dir, context=f"merge of {branch}"):
                merged = await _merge_task_branch(
                    git_dir, task_id, branch,
                    expect_artifact=decision.expect_artifact,
                    merge_sha=merge_sha,
                    worktree_dir=git_worktree_dir,
                    merge_record=attempt,
                    artifact_dir=project_dir,
                    pinned_default_sha=guard.expected_sha,
                )
    except SecurityGateBypassError as exc:
        _gate_audit_log(
            f"task={task_id} event=defensive-invariant-blocked detail={exc}",
            task_id=task_id,
            event="defensive-invariant-blocked",
        )
        return finish("blocked", f"defensive invariant: {exc}")
    except PinnedRepositoryError as exc:
        # F2 (task #3155): a work tree swapped during the merge, or a git
        # call outside the pinned work trees, was refused before git ran.
        guard.trip(
            f"merge task={task_id}", await guard.current_sha(),
            task_id=task_id, detail=str(exc),
        )
        return finish("blocked", guard.alert or str(exc))
    finally:
        pins.close()
    if merged:
        landed_sha = attempt.merged_sha or merge_sha
        if landed_sha is None or not await guard.record_merge(
            task_id, landed_sha, post_head=attempt.post_head,
            regenerated_paths=attempt.regenerated_paths,
            regenerated_blobs=attempt.regenerated_blobs,
        ):
            return finish(
                "blocked",
                guard.alert or "merged commit could not be verified on the "
                "default branch",
            )
        reason = "merged"
        regenerated_note = ""
        if attempt.regenerated_paths:
            # Task #3131: the regenerated content is part of what was merged,
            # so the recorded merged SHA is the resolution commit itself (the
            # guard has just verified it differs from the approved merge in
            # the regenerated generated files only).
            landed_sha = attempt.post_head
            files = ",".join(attempt.regenerated_paths)
            reason = f"merged; regenerated generated file(s) {files}"
            regenerated_note = f" regenerated={files}"
        _gate_audit_log(
            f"task={task_id} event=merge-succeeded branch={branch} "
            f"merged_sha={landed_sha} "
            f"default_after={guard.expected_sha}{regenerated_note}",
            task_id=task_id,
            event="merge-succeeded",
        )
        return finish("merged", reason, landed_sha)
    if not await guard.verify(f"after-failed-merge task={task_id}", task_id=task_id):
        return finish("blocked", guard.alert or "default branch moved")
    reason = attempt.reason or "merge failed"
    _gate_audit_log(
        f"task={task_id} event=merge-failed branch={branch} reason={reason}",
        task_id=task_id,
        event="merge-failed",
    )
    return finish("merge_failed", reason)


MERGE_ELIGIBLE_OUTCOMES: tuple[str, ...] = ("tests_passed", "no_tests")

# An agent that reports "already implemented / no changes needed" ends with
# this outcome, which update_task_status records as done WITHOUT a merge.
NO_CHANGES_OUTCOME = "early_completed_no_changes"
# That claim contradicted by the task branch (commits or uncommitted work):
# recorded as blocked, nothing reviewed or merged, the branch kept.
NO_CHANGES_CONTRADICTED_OUTCOME = "no_changes_claim_contradicted"


async def _no_changes_claim_problem(
    project_dir: str,
    worktree_dir: str,
    task_branch: str,
    trusted_sha: str | None,
) -> str | None:
    """Why a "no changes needed" claim is false for this branch, or None.

    3112 review LOW (task #3119): the claim is true only when the task
    branch's tip is already on the trusted default branch and the worktree
    holds no uncommitted work. Anything else means the agent changed the
    project, and marking the task done would strand those changes
    unreviewed on the branch.
    """
    tip = await resolve_commit(project_dir, f"refs/heads/{task_branch}")
    if tip is None:
        return f"{task_branch} could not be resolved"
    if trusted_sha is None or not await is_ancestor(project_dir, tip, trusted_sha):
        return f"{task_branch} has commits that are not on the default branch"
    dirty = await _worktree_dirty_reason(worktree_dir)
    if dirty is not None:
        return f"the worktree has uncommitted work ({dirty})"
    return None


async def review_task_branch(
    task: dict,
    task_dir: str,
    project_dir: str,
    project_context: dict,
    args,
    outcome: str,
    output: list[str] | None = None,
) -> tuple[str, bool]:
    """Security-review a finished task in its worktree before the gated merge.

    Returns ``(outcome, review_blocks_merge)``. A merge-eligible outcome is
    demoted to ``security_review_blocked`` (or the overloaded outcome) when
    the reviewer crashed, failed, was overloaded, or its artifact reports a
    CRITICAL/HIGH finding or is missing (fail-closed). Doc-only diffs skip
    the reviewer (task #2358). Shared by ``--tasks`` and every isolated mode
    (task #3112) so the review semantics cannot diverge between modes.

    The reviewer runs against ``task_dir`` and its artifact is persisted to,
    and read back from, the stable ``project_dir`` (task #2447).
    """
    task_id = task["id"]
    if not is_security_review_enabled(args) or outcome not in MERGE_ELIGIBLE_OUTCOMES:
        return outcome, False
    # Task 2360: doc-only diffs cannot introduce code-level vulnerabilities;
    # the gate skips them rather than risk a prose false positive.
    changed_files = await get_changed_files_for_branch(task_dir)
    if is_doc_only_diff(changed_files):
        record_reviewer_skipped_doc_only(task_id)
        log(
            f"[Task #{task_id}] SECURITY GATE: skipping review — doc-only "
            f"change ({len(changed_files)} file(s), all docs).",
            output,
        )
        return outcome, False

    # IND3132-02 (task #3146): a submodule pointer bump can be hidden from
    # the reviewer's own `git diff`, so it is named in the context the
    # reviewer is given (its prompt quotes the task description).
    review_task = task
    pointer_note = describe_submodule_pointer_changes(changed_files)
    if pointer_note is not None:
        review_task = {
            **task,
            "description": f"{task.get('description') or ''}\n\n{pointer_note}",
        }
        log(
            f"[Task #{task_id}] SECURITY GATE: reviewer told about submodule "
            f"pointer change(s).",
            output,
        )

    review_crashed = False
    sec_result = None
    try:
        sec_result = await run_security_review(
            review_task, task_dir, project_context, args,
            output=output,
            stable_project_dir=project_dir,
        )
    except Exception:  # pragma: no cover - defensive
        # Task 2341 S2: a crashed reviewer always blocks, whatever artifact
        # (possibly stale) is on disk.
        review_crashed = True
        logger.exception("[Task #%s] security review crashed", task_id)
        try:
            from equipa.loops import _persist_security_review_artifact
            _persist_security_review_artifact(
                worktree_dir=task_dir,
                stable_dir=project_dir,
                task_id=task_id,
                result_text="",
                agent_succeeded=False,
                output=output,
            )
        except Exception:  # pragma: no cover
            logger.exception(
                "[Task #%s] failed to persist post-crash security-review "
                "artifact", task_id,
            )

    block_on_missing = is_feature_enabled(
        getattr(args, "dispatch_config", None) or {},
        "security_review_block_on_missing_artifact",
    )
    review_blocks_merge, review_counts = _security_review_blocks_merge(
        project_dir, task_id, block_on_missing=block_on_missing,
    )
    branch = f"forge-task-{task_id}"
    if review_crashed:
        log(
            f"[Task #{task_id}] SECURITY GATE: blocking merge — security "
            f"review crashed; branch {branch} left unmerged for operator "
            f"review.",
            output,
        )
        return "security_review_blocked", True
    if is_overloaded_result(sec_result):
        # Task #2994 S1: the reviewer never ran (sustained 529).
        log(
            f"[Task #{task_id}] SECURITY GATE: blocking merge — security "
            f"reviewer FAILED: model overloaded (529) through every retry. "
            f"Not downgrading the model; branch {branch} left unmerged.",
            output,
        )
        return OVERLOADED_OUTCOME, True
    failure = reviewer_run_failure(task_id)
    if failure is not None:
        # Task #3041: no artifact on disk is this run's review.
        log(
            f"[Task #{task_id}] SECURITY GATE: blocking merge — security "
            f"reviewer FAILED ({failure}); no artifact on disk is trusted. "
            f"Branch {branch} left unmerged for operator review.",
            output,
        )
        return "security_review_blocked", True
    if review_blocks_merge:
        if review_counts is None:
            log(
                f"[Task #{task_id}] SECURITY GATE: blocking merge — "
                f"SECURITY-REVIEW-{task_id}.md artifact is missing "
                f"(fail-closed). Branch {branch} left unmerged for operator "
                f"review.",
                output,
            )
        else:
            log(
                f"[Task #{task_id}] SECURITY GATE: blocking merge — "
                f"{review_counts.get('CRITICAL', 0)} CRITICAL, "
                f"{review_counts.get('HIGH', 0)} HIGH finding(s). Branch "
                f"{branch} left unmerged for operator review.",
                output,
            )
        return "security_review_blocked", True
    return outcome, False


@dataclass
class IsolatedTaskRun:
    """What one task's isolated run produced (task #3112).

    ``outcome`` is the status-bearing outcome to record: the agent outcome
    after the security review and the gated merge (``tests_passed`` /
    ``no_tests`` only when the work verifiably reached the default branch).
    """

    outcome: str
    result: dict
    cycles: int
    merged_sha: str | None = None
    agent_outcome: str | None = None
    reason: str = ""


# ``execute(worktree_dir, task_branch)`` runs the agent(s) for one task inside
# its isolation worktree and returns ``(result, cycles, outcome)``.
TaskExecutor = Callable[[str, str], Awaitable[tuple[dict, int, str]]]


def _empty_run_result() -> dict:
    return {"cost": 0.0, "duration": 0.0}


async def _worktree_dirty_reason(worktree_dir: str) -> str | None:
    """Uncommitted work (untracked files included) left in a task worktree.

    ``.forge-state.json`` is agent scratch state, not work; it is removed
    first, as :func:`_cleanup_worktrees` would remove it anyway.
    """
    state_file = Path(worktree_dir) / ".forge-state.json"
    if state_file.is_file():
        state_file.unlink()
    status = await git_run_async(
        ["status", "--porcelain", "--ignore-submodules=all"], worktree_dir, timeout=30,
    )
    if status.returncode != 0:
        return f"git status failed (rc={status.returncode})"
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if not dirty:
        return None
    return f"{len(dirty)} uncommitted change(s), e.g. {dirty[0].strip()!r}"


async def run_task_in_isolation(
    task: dict,
    project_dir: str,
    project_context: dict,
    args,
    *,
    execute: TaskExecutor,
    guard: DefaultBranchGuard | None = None,
    output: list[str] | None = None,
) -> IsolatedTaskRun:
    """Run one task in its own worktree; merge only through the gated merge.

    dispatch-04/07 (task #3112): every dispatch mode that lets an agent write
    to a git project (``--task``, ``--project``, ``--auto-run``, ``--goal``,
    ``--parallel-goals``) goes through here, as ``--tasks`` already did:

    1. the default branch is pinned (``guard``; snapshotted here when the
       caller has none) — a run whose guard already tripped is refused;
    2. a ``forge-task-<id>`` worktree is created from the trusted default
       branch by :func:`_create_isolation_worktrees` (a stale branch or
       leftover worktree refuses the task, outcome ``worktree_refused``);
    3. ``execute`` runs the agent(s) in the worktree;
    4. a task branch with commits is security-reviewed
       (:func:`review_task_branch`) and merged ONLY by
       :func:`_gated_merge_task`; a branch with no commits has nothing to
       merge (``noop``), unless the agent left uncommitted work;
    5. the outcome is reconciled with the merge (:func:`outcome_after_merge`),
       so ``done`` means the work is on the default branch;
    6. the worktree is removed; the branch is deleted only when merged or
       empty, otherwise kept (uncommitted work stashed on it).

    No status is written here: the caller records ``outcome`` (with
    ``merged_sha``) through its own telemetry path. Nothing in this helper
    commits to the default branch.
    """
    task_id = task["id"]
    task_branch = f"forge-task-{task_id}"
    shutdown_signal = shutdown_requested()
    if shutdown_signal is not None:
        # dispatch-06: a signal was deferred during an earlier merge; no new
        # agent is started on the way out.
        reason = (
            f"orchestrator shutting down ({signal.Signals(shutdown_signal).name})"
        )
        log(f"[Task #{task_id}] NOT started: {reason}", output)
        return IsolatedTaskRun(
            "shutdown_requested", _empty_run_result(), 0, reason=reason,
        )
    if guard is None:
        try:
            guard = await DefaultBranchGuard.snapshot(project_dir)
        except MergeIntegrityError as exc:
            reason = f"default branch could not be pinned: {exc}"
            log(f"[Task #{task_id}] REFUSED: {reason}", output)
            _audit_task_abort(task_id, "isolation-refused", reason, output)
            return IsolatedTaskRun(
                "merge_integrity_failed", _empty_run_result(), 0, reason=reason,
            )
        log(
            f"  [Merge-Integrity] '{guard.default_branch}' pinned at "
            f"{guard.baseline_sha[:12]} before dispatch",
            output,
        )
    if guard.tripped:
        reason = guard.alert or "default branch moved"
        log(f"[Task #{task_id}] REFUSED: {reason}", output)
        _audit_task_abort(task_id, "isolation-refused", reason, output)
        return IsolatedTaskRun(
            "merge_integrity_failed", _empty_run_result(), 0, reason=reason,
        )

    worktree_base = Path(project_dir) / ".forge-worktrees"
    refusals: dict[int, str] = {}
    worktrees = await _create_isolation_worktrees(
        [task], project_dir, worktree_base, refusals=refusals,
    )
    worktree_dir = worktrees.get(task_id)
    if worktree_dir is None:
        reason = refusals.get(task_id, "no isolation worktree was created")
        log(f"[Task #{task_id}] REFUSED: {reason}", output)
        _audit_task_abort(task_id, "worktree-refused", reason, output)
        return IsolatedTaskRun(
            "worktree_refused", _empty_run_result(), 0, reason=reason,
        )

    delete_branch = False
    try:
        # A nested project runs in its sub-directory of the worktree.
        agent_dir, agent_dir_problem = project_dir_in_worktree(
            project_dir, worktree_dir,
        )
        if agent_dir is None:
            # Nothing ran: the branch is still at its fork point.
            delete_branch = True
            reason = f"no project directory in the task worktree: {agent_dir_problem}"
            log(f"[Task #{task_id}] REFUSED: {reason}", output)
            _audit_task_abort(task_id, "worktree-refused", reason, output)
            return IsolatedTaskRun(
                "worktree_refused", _empty_run_result(), 0, reason=reason,
            )
        if agent_dir != worktree_dir:
            log(
                f"  [Isolation] Nested project: task #{task_id} runs in "
                f"{Path(agent_dir).relative_to(worktree_dir)} of its worktree",
                output,
            )
        base_sha = await resolve_commit(worktree_dir, "HEAD")
        result, cycles, agent_outcome = await execute(agent_dir, task_branch)
        if agent_dir != worktree_dir:
            # R3119-05 (task #3126): the agent controls this path; only a
            # regular file is removed, so a directory there cannot abort the
            # review, gate and audit record below.
            state_file = Path(agent_dir) / ".forge-state.json"
            if state_file.is_file() and not state_file.is_symlink():
                state_file.unlink()
        # Task #3111 (3107 review R1): an agent that left its worktree on
        # another branch must not have that branch's commits treated as the
        # task's result.
        try:
            await _require_task_branch(worktree_dir, task_branch)
        except AttemptCleanupError as exc:
            _audit_task_abort(
                task_id, "worktree-branch-mismatch",
                f"after the run ({agent_outcome}): {exc}", output,
            )
            agent_outcome = "worktree_branch_mismatch"
        # dispatch-03: catch an agent that moved the default branch.
        await guard.verify(f"after-agent task={task_id}", task_id=task_id)
        branch_sha = await resolve_commit(project_dir, f"refs/heads/{task_branch}")
        # Nothing to merge: the branch never moved from its fork point and
        # that commit is already on the default branch.
        nothing_to_merge = (
            branch_sha is not None
            and branch_sha == base_sha
            and await is_ancestor(project_dir, branch_sha, guard.expected_sha)
        )
        outcome = agent_outcome
        reason = ""
        claim_problem = (
            await _no_changes_claim_problem(
                project_dir, worktree_dir, task_branch, guard.expected_sha,
            )
            if outcome == NO_CHANGES_OUTCOME else None
        )
        if claim_problem is not None:
            # 3112 review LOW: "no changes needed" is recorded as done without
            # a merge, so commits behind it would stay unreviewed on the branch.
            outcome = NO_CHANGES_CONTRADICTED_OUTCOME
            reason = (
                f"agent reported no changes needed, but {claim_problem}; "
                f"not reviewed or merged, {task_branch} kept"
            )
            guard.outcomes[task_id] = MergeOutcome("skipped", reason)
            log(f"[Task #{task_id}] {reason}", output)
            _audit_task_abort(task_id, "no-changes-claim-contradicted", reason, output)
        elif outcome in MERGE_ELIGIBLE_OUTCOMES and nothing_to_merge:
            dirty = await _worktree_dirty_reason(worktree_dir)
            if dirty:
                guard.outcomes[task_id] = MergeOutcome(
                    "merge_failed",
                    f"{task_branch} has no commits but the agent left "
                    f"uncommitted work ({dirty}); it is stashed on the branch",
                )
            else:
                guard.outcomes[task_id] = MergeOutcome(
                    "noop", f"{task_branch} has no commits; nothing to merge",
                    base_sha,
                )
                log(
                    f"[Task #{task_id}] {task_branch} has no commits; "
                    f"nothing to review or merge",
                    output,
                )
        else:
            if not guard.tripped:
                # A tripped guard blocks the merge anyway; no reviewer is paid for.
                outcome, _ = await review_task_branch(
                    task, agent_dir, project_dir, project_context, args,
                    outcome, output=output,
                )
            try:
                await _gated_merge_task(
                    repo=project_dir,
                    branch=task_branch,
                    outcome=outcome,
                    task_id=task_id,
                    project_context=project_context,
                    security_review_enabled=is_security_review_enabled(args),
                    block_on_missing=is_feature_enabled(
                        getattr(args, "dispatch_config", None) or {},
                        "security_review_block_on_missing_artifact",
                    ),
                    guard=guard,
                    worktree_dir=worktree_dir,
                )
            except SecurityGateBypassError as exc:
                _gate_audit_log(
                    f"task={task_id} event=defensive-invariant-blocked "
                    f"branch={task_branch} detail={exc}",
                    task_id=task_id,
                    event="defensive-invariant-blocked",
                )
                guard.outcomes[task_id] = MergeOutcome(
                    "blocked", f"defensive invariant: {exc}",
                )
        merged_sha: str | None = None
        if outcome in MERGE_ELIGIBLE_OUTCOMES:
            await guard.verify(f"end-of-task task={task_id}", task_id=task_id)
            final_outcome, merged_sha, reason = outcome_after_merge(
                outcome, guard.outcomes.get(task_id), guard,
            )
            single_line_reason = " ".join(reason.split())
            log(
                f"[Task #{task_id}] Final status after merge: {final_outcome} "
                f"({single_line_reason}"
                + (f"; merged_sha={merged_sha[:12]}" if merged_sha else "")
                + ")",
                output,
            )
            _gate_audit_log(
                f"task={task_id} event=task-status-after-merge "
                f"outcome={final_outcome} merged_sha={merged_sha or 'none'} "
                f"reason={single_line_reason}",
                task_id=task_id,
                event="task-status-after-merge",
            )
            outcome = final_outcome
        # The branch goes only when nothing on it can be lost: it was merged,
        # or its tip is already on the default branch and the worktree holds
        # no uncommitted work. Otherwise it stays (a re-dispatch is refused
        # until the operator resolves it). A tripped guard keeps everything.
        merge_status = guard.outcomes.get(task_id)
        if not guard.tripped:
            if merge_status is not None and merge_status.status == "merged":
                delete_branch = True
            else:
                tip = await resolve_commit(project_dir, f"refs/heads/{task_branch}")
                delete_branch = (
                    tip is not None
                    and await is_ancestor(project_dir, tip, guard.expected_sha)
                    and await _worktree_dirty_reason(worktree_dir) is None
                )
        return IsolatedTaskRun(
            outcome, result, cycles, merged_sha=merged_sha,
            agent_outcome=agent_outcome, reason=reason,
        )
    finally:
        await _cleanup_worktrees(
            project_dir, {task_id: worktree_dir},
            {task_id} if delete_branch else set(), worktree_base,
        )


def outcome_after_merge(
    agent_outcome: str,
    merge_outcome: MergeOutcome | None,
    guard: DefaultBranchGuard | None,
    guard_error: str | None = None,
) -> tuple[str, str | None, str]:
    """``(outcome, merged_sha, reason)`` to record once the merge has run.

    dispatch-05 (task #3111): a merge-eligible task is ``done`` only when its
    approved commit is verifiably on the default branch (``merged``, or
    ``noop`` when it was already there) and the default branch moved only
    through the orchestrator's merges. Every other result maps to a
    non-success outcome, which ``update_task_status`` records as ``blocked``.
    """
    if guard is None:
        return (
            "merge_integrity_failed", None,
            guard_error or "default branch was not pinned before dispatch",
        )
    if guard.tripped:
        return "merge_integrity_failed", None, guard.alert or "default branch moved"
    if merge_outcome is None:
        return "merge_failed", None, "no merge result was recorded"
    if merge_outcome.done:
        return agent_outcome, merge_outcome.merged_sha, merge_outcome.status
    status_outcomes = {
        "blocked": "merge_blocked",
        "merge_failed": "merge_failed",
        "skipped": "merge_skipped",
    }
    return (
        status_outcomes.get(merge_outcome.status, "merge_failed"),
        None,
        merge_outcome.reason or merge_outcome.status,
    )


def _write_status_after_merge(
    run_result: dict,
    guard: DefaultBranchGuard | None,
    guard_error: str | None,
) -> None:
    """Write the deferred task status once the gated merge has run."""
    task_id = run_result["task"]["id"]
    merge_outcome = guard.outcomes.get(task_id) if guard is not None else None
    final_outcome, merged_sha, reason = outcome_after_merge(
        run_result["outcome"], merge_outcome, guard, guard_error,
    )
    single_line_reason = " ".join(reason.split())
    print(
        f"  [Task #{task_id}] Final status after merge: {final_outcome} "
        f"({single_line_reason}"
        + (f"; merged_sha={merged_sha[:12]}" if merged_sha else "")
        + ")"
    )
    _gate_audit_log(
        f"task={task_id} event=task-status-after-merge outcome={final_outcome} "
        f"merged_sha={merged_sha or 'none'} reason={single_line_reason}",
        task_id=task_id,
        event="task-status-after-merge",
    )
    update_task_status(task_id, final_outcome, merged_sha=merged_sha)
    run_result["final_outcome"] = final_outcome
    run_result["merged_sha"] = merged_sha


def _refuse_task_without_worktree(
    task: dict,
    reason: str,
    flow_id: int | None,
    output: list[str],
) -> dict:
    """Block a task that has no isolation worktree instead of running it.

    Records the refusal in the gate audit, sets the task to ``blocked``
    (outcome ``worktree_refused``) and returns the per-task result that
    ``run_parallel_tasks`` collects. No agent runs and nothing is merged.
    """
    task_id = task["id"]
    log(f"\n[Task #{task_id}] REFUSED: {reason}", output)
    _audit_task_abort(task_id, "worktree-refused", reason, output)
    outcome = "worktree_refused"
    update_task_status(task_id, outcome, output=output)
    if flow_id is not None:
        try:
            from equipa import flows as _flows
            _flows.update_child_state(
                flow_id, task_id, "failed",
                payload={"outcome": outcome, "reason": reason},
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("[flows] child refused update failed")
    return {
        "task": task,
        "result": {"cost": 0, "duration": 0},
        "cycles": 0,
        "outcome": outcome,
        "output": output,
        "merge_ok": False,
        "needs_merge": False,
    }


# Exit status of a dispatch refused before any agent ran (dispatch-15): a
# nohup log or wrapper script must be able to tell "refused" from "ran".
EXIT_DISPATCH_REFUSED = 2


class DispatchRefused(SystemExit):
    """A dispatch refused before any agent ran (dispatch-15).

    A ``SystemExit``, so the CLI exits with :data:`EXIT_DISPATCH_REFUSED`.
    In-process callers that dispatch on their own schedule (the initiative
    wave dispatcher) catch it and record a failed wave instead of ending
    the whole process mid-run.
    """

    def __init__(self, message: str) -> None:
        super().__init__(EXIT_DISPATCH_REFUSED)
        self.message = message


def refuse_dispatch(message: str) -> NoReturn:
    """Print ``ERROR: message`` and raise :class:`DispatchRefused`."""
    print(f"ERROR: {message}")
    raise DispatchRefused(message)


def collect_refusals(results: list) -> list[str]:
    """Every refusal recorded in ``--auto-run`` or ``--parallel-goals`` results.

    3112 review (task #3119): a project or goal refused inside a
    multi-project run (unmapped directory, unpinnable default branch, a task
    refused its worktree, a goal stopped because the default branch moved)
    used to leave the process exiting 0. Each entry is prefixed with its
    project so the CLI can name it when it exits non-zero.

    R3119-04 (task #3126): an exception returned by
    ``gather(..., return_exceptions=True)`` is a refusal too.
    """
    refusals: list[str] = []
    for entry in results:
        if isinstance(entry, BaseException):
            refusals.append(f"exception: {type(entry).__name__}: {entry}")
            continue
        if not isinstance(entry, dict):
            continue
        label = entry.get("codename") or entry.get("project_name") or "?"
        refusals.extend(f"{label}: {reason}" for reason in entry.get("refusals") or ())
    return refusals


def resolve_max_concurrent(args) -> int:
    """Concurrency cap for ``--tasks``: CLI flag, else dispatch config, else 4.

    dispatch-10 (task #3112): ``max_concurrent`` from dispatch_config.json
    used to be ignored here, so an operator who lowered it for a RAM-limited
    host still got 4 agents at once. A cap below 1 (or not an integer) is
    refused rather than silently replaced by the default.
    """
    cli_value = getattr(args, "max_concurrent", None)
    if cli_value is not None:
        value, source = cli_value, "--max-concurrent"
    else:
        config = getattr(args, "dispatch_config", None) or {}
        value = config.get("max_concurrent", 4)
        source = "dispatch config max_concurrent"
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        refuse_dispatch(f"{source} must be a positive integer, got {value!r}")
    refusal = concurrency_refusal(value, source)
    if refusal:
        refuse_dispatch(refusal)
    return value


async def run_parallel_tasks(task_ids: list[int], args) -> None:
    """Run multiple tasks concurrently with dev-test loops.

    All tasks must belong to the same project (for safety).

    When ``args.use_flow`` is true (or the parallel fanout has more than
    one task), a row in the ``flows`` table tracks the orchestration so
    the run survives a Claudinator restart and supports sticky cancel.
    """
    # dispatch-15 (task #3112): de-duplicate, and refuse (exit non-zero)
    # rather than silently running a subset when an id does not exist.
    task_ids = list(dict.fromkeys(task_ids))
    max_concurrent = resolve_max_concurrent(args)
    # Fetch all tasks
    tasks = fetch_tasks_by_ids(task_ids)
    if not tasks:
        refuse_dispatch("No tasks found for given IDs.")
    missing_ids = sorted(set(task_ids) - {t["id"] for t in tasks})
    if missing_ids:
        refuse_dispatch(
            f"task id(s) not found in TheForge: "
            f"{', '.join(str(i) for i in missing_ids)}. Nothing was run; "
            f"fix the --tasks list and re-dispatch."
        )

    # Verify all tasks are from the same project
    project_ids = set(t.get("project_id") for t in tasks)
    if len(project_ids) > 1:
        refuse_dispatch(
            f"--tasks requires all tasks from the same project. "
            f"Found project IDs: {project_ids}"
        )

    project_id = tasks[0].get("project_id")
    project_dir = resolve_project_dir(tasks[0])
    if not project_dir:
        # Last-chance fallback: if the project is registered as
        # scaffold-based and has a local_path configured but the directory
        # is empty, auto-clone the scaffold so dispatch can proceed.
        project_dir = _bootstrap_scaffold_if_needed(tasks[0], project_id)
    if not project_dir:
        refuse_dispatch("Could not resolve project directory.")
    try:
        from equipa.scaffold import ensure_scaffold, ScaffoldCloneError
        if ensure_scaffold(project_dir, project_id):
            print(f"Auto-cloned ForgeScaffold into {project_dir}")
    except ScaffoldCloneError as exc:
        refuse_dispatch(f"Scaffold auto-clone failed: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"WARN: Scaffold auto-clone raised {exc!r}")
    if not Path(project_dir).exists():
        refuse_dispatch(f"Project directory does not exist: {project_dir}")

    project_context = fetch_project_context(project_id)
    semaphore = asyncio.Semaphore(max_concurrent)

    # --- Task Flow tracking (durable revisions + sticky cancel) ---
    # The flow row is created up front so a mid-run restart can find it.
    flow_id: int | None = None
    use_flow = getattr(args, "use_flow", None)
    if use_flow is None:
        use_flow = len(tasks) > 1
    if use_flow:
        try:
            from equipa import flows as _flows
            flow = _flows.create_flow(
                project_id=project_id,
                title=(getattr(args, "flow_title", None)
                       or f"parallel fanout: {len(tasks)} tasks"),
                metadata={
                    "task_ids": [t["id"] for t in tasks],
                    "max_concurrent": max_concurrent,
                },
            )
            flow_id = flow.id
            for t in tasks:
                _flows.add_child(
                    flow_id,
                    t["id"],
                    role=t.get("role") or "developer",
                    relationship="mirrored",
                )
            _flows.transition(
                flow_id,
                "running",
                event="dispatch_start",
                payload={"task_count": len(tasks)},
            )
            print(f"  [Flow] tracking fanout under flow_id={flow_id}")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[flows] could not create flow: %s", e)
            flow_id = None

    print(f"\nParallel task execution: {len(tasks)} tasks, max {max_concurrent} concurrent")
    for t in tasks:
        print(f"  - #{t['id']}: {t['title']}")

    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Create per-task git worktrees for filesystem isolation.
    # Always isolate when the project is a git repo — single-task --tasks
    # dispatches need isolation just as much as multi-task fan-out; the
    # historical `len(tasks) > 1` guard silently dropped isolation for N=1
    # and let agents write directly to master's working tree.
    worktree_base = Path(project_dir) / ".forge-worktrees"
    try:
        use_worktrees = _is_git_repo(project_dir)
    except GitRepositoryUnreadableError as exc:
        # R3119-02 (task #3126): never run an unreadable repo ungated.
        refuse_dispatch(str(exc))
    if use_worktrees:
        # dispatch-06: surface what earlier runs left behind (report only).
        await report_leftover_dispatch_state(project_dir)
    # dispatch-03 / gate-12 (task #3111): pin the default branch BEFORE any
    # agent runs. From here on only the orchestrator's own merges may move
    # it; anything else trips the guard and nothing further is merged.
    merge_guard: DefaultBranchGuard | None = None
    merge_guard_error: str | None = None
    if use_worktrees:
        try:
            merge_guard = await DefaultBranchGuard.snapshot(project_dir)
            print(
                f"  [Merge-Integrity] '{merge_guard.default_branch}' pinned at "
                f"{merge_guard.baseline_sha[:12]} before dispatch"
            )
        except MergeIntegrityError as exc:
            merge_guard_error = f"default branch could not be pinned: {exc}"
            print(
                f"  [Merge-Integrity] ERROR: {merge_guard_error} — no task in "
                f"this run will be merged"
            )
    # Worktree creation issues 2-3 git commands per task. The helper is
    # natively async (uses git_run_async) so the event loop is not
    # blocked while subprocesses run.
    worktree_refusals: dict[int, str] = {}
    worktree_dirs: dict[int, str] = (
        await _create_isolation_worktrees(
            tasks, project_dir, worktree_base, refusals=worktree_refusals,
        )
        if use_worktrees else {}
    )
    # Task #3119: a nested project runs in its sub-directory of each
    # worktree; a task whose worktree lacks that directory is refused.
    agent_dirs: dict[int, str] = {}
    for task_id, worktree_dir in list(worktree_dirs.items()):
        agent_dir, agent_dir_problem = project_dir_in_worktree(project_dir, worktree_dir)
        if agent_dir is None:
            worktree_refusals[task_id] = (
                f"no project directory in the task worktree: {agent_dir_problem}"
            )
            del worktree_dirs[task_id]
            await _cleanup_worktrees(
                project_dir, {task_id: worktree_dir}, {task_id}, worktree_base,
            )
            continue
        agent_dirs[task_id] = agent_dir

    async def run_one_task(task):
        output = []
        task_dir = agent_dirs.get(task["id"])
        if task_dir is None and use_worktrees:
            # dispatch-01: never fall back to the shared main checkout. A
            # task run there commits straight onto whatever it has checked
            # out, and the merge loop never gates it.
            reason = worktree_refusals.get(
                task["id"], "no isolation worktree was created",
            )
            return _refuse_task_without_worktree(task, reason, flow_id, output)
        if task_dir is None:
            # Not a git repo: there are no branches to protect.
            task_dir = project_dir
        # Honour sticky cancel: if the flow was cancelled before we reached
        # this task, skip the dev-test loop entirely.
        if flow_id is not None:
            try:
                from equipa import flows as _flows
                if _flows.is_cancelled(flow_id):
                    log(
                        f"[Task #{task['id']}] Skipped — flow_id={flow_id} "
                        f"is sticky-cancelled",
                        output,
                    )
                    return {
                        "task": task,
                        "result": {"cost": 0, "duration": 0},
                        "cycles": 0,
                        "outcome": "cancelled",
                        "output": output,
                        "merge_ok": False,
                        "needs_merge": False,
                    }
            except Exception:  # pragma: no cover
                logger.exception("[flows] cancel check failed")

        async with semaphore:
            # Re-check after acquiring the semaphore — the flow may have
            # been cancelled while we were queued.
            if flow_id is not None:
                try:
                    from equipa import flows as _flows
                    if _flows.is_cancelled(flow_id):
                        return {
                            "task": task,
                            "result": {"cost": 0, "duration": 0},
                            "cycles": 0,
                            "outcome": "cancelled",
                            "output": output,
                            "merge_ok": False,
                            "needs_merge": False,
                        }
                    _flows.update_child_state(flow_id, task["id"], "running")
                except Exception:  # pragma: no cover
                    logger.exception("[flows] child running update failed")

            log(f"\n[Task #{task['id']}] Starting: {task['title']}", output)
            # Bug 2282 fix: parallel mode now gets the same autoresearch
            # retry wrapper as single-task mode. Without this, BashSecurity
            # false positives or analysis-paralysis early-terms in any of
            # the N parallel tasks would be final with no reflection-
            # injected retry. The helper is module-level above.
            _config_for_loop = getattr(args, "dispatch_config", None) or {}
            result, cycles, outcome, _, _, task = (
                await run_dev_test_loop_with_autoresearch(
                    task, task_dir, project_context, args, _config_for_loop, output=output,
                    task_branch=(
                        f"forge-task-{task['id']}"
                        if task["id"] in worktree_dirs else None
                    ),
                )
            )

            if outcome == NO_CHANGES_OUTCOME and task["id"] in worktree_dirs:
                claim_problem = await _no_changes_claim_problem(
                    project_dir, worktree_dirs[task["id"]],
                    f"forge-task-{task['id']}",
                    merge_guard.expected_sha if merge_guard is not None else None,
                )
                if claim_problem is not None:
                    # 3112 review LOW: never "done" with unreviewed commits.
                    outcome = NO_CHANGES_CONTRADICTED_OUTCOME
                    _audit_task_abort(
                        task["id"], "no-changes-claim-contradicted",
                        f"agent reported no changes needed, but {claim_problem}; "
                        f"branch kept", output,
                    )

            # Bug 2321: review BEFORE the task can be marked done; CRITICAL/
            # HIGH findings demote the outcome so the task stays blocked and
            # the post-gather merge skips the branch. The artifact persists
            # to the stable project root (task 2447). Shared with every
            # isolated mode since task #3112.
            outcome, review_blocks_merge = await review_task_branch(
                task, task_dir, project_dir, project_context, args, outcome,
                output=output,
            )

            if merge_guard is not None:
                # dispatch-03: catch an agent that moved the default branch
                # (checkout + commit, update-ref, merge) as soon as it ends.
                await merge_guard.verify(
                    f"after-agent task={task['id']}", task_id=task["id"],
                )
            needs_merge = (
                task["id"] in worktree_dirs
                and outcome in ("tests_passed", "no_tests")
                and not review_blocks_merge
            )
            if needs_merge:
                # dispatch-05: "done" is written only after the merge (see
                # _write_status_after_merge); until then the task stays as is.
                log(
                    f"[Task #{task['id']}] Status deferred until the gated "
                    f"merge ({outcome})",
                    output,
                )
            else:
                update_task_status(task["id"], outcome, output=output)
            log(f"[Task #{task['id']}] Done: {outcome} ({cycles} cycles)", output)
            if flow_id is not None:
                try:
                    from equipa import flows as _flows
                    child_state = (
                        "done"
                        if outcome in ("tests_passed", "no_tests")
                        else "failed"
                    )
                    _flows.update_child_state(
                        flow_id, task["id"], child_state,
                        payload={"outcome": outcome, "cycles": cycles},
                    )
                except _flows.FlowCancelled:
                    # Sticky cancel beat us to the punch — that's fine.
                    pass
                except Exception:  # pragma: no cover
                    logger.exception("[flows] child terminal update failed")
            # Record telemetry
            task_role = task.get("role") or "developer"
            try:
                telemetry_model = get_role_model(task_role, args, task=task)
            except CircuitOpenError:
                telemetry_model = "circuit_blocked"
            record_agent_run(
                task, result, outcome, role=task_role,
                model=telemetry_model,
                max_turns=get_role_turns(task_role, args, task=task),
                cycle_number=cycles, output=output,
            )

            # Mark for post-gather sequential merge (avoid parallel merge conflicts)
            merge_ok = False

            return {
                "task": task,
                "result": result,
                "cycles": cycles,
                "outcome": outcome,
                "output": output,
                "merge_ok": merge_ok,
                "needs_merge": needs_merge,
                "review_blocks_merge": review_blocks_merge,
            }

    results = await asyncio.gather(
        *[run_one_task(t) for t in tasks],
        return_exceptions=True,
    )

    # Reconcile flow state from final child outcomes.
    if flow_id is not None:
        try:
            from equipa import flows as _flows
            _flows.reconcile_after_restart(flow_id)
        except Exception:  # pragma: no cover
            logger.exception("[flows] post-gather reconcile failed")

    # Print results
    print(f"\n{'#' * 60}")
    print("PARALLEL TASKS SUMMARY")
    print(f"{'#' * 60}")

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  EXCEPTION: {r}")
            continue

        task = r["task"]
        outcome = r["outcome"]
        result = r["result"]

        # Print buffered output
        for line in r.get("output", []):
            print(line)

        cost = result.get("cost", 0) or 0
        duration = result.get("duration", 0)
        total_cost += cost
        total_duration += duration

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            print(f"\n  #{task['id']}: COMPLETED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")
        else:
            blocked.append(task)
            print(f"\n  #{task['id']}: BLOCKED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")

    print(f"\nTotal: {len(completed)} completed, {len(blocked)} blocked")
    print(f"Duration: {total_duration:.0f}s total")
    if total_cost > 0:
        print(f"Cost: ${total_cost:.4f}")
    print(f"{'#' * 60}")

    # Sequential merge — task #2451 unified gate. All merge decisions
    # now flow through `_gated_merge_task`, which re-runs the
    # security-review check and the defensive invariant inside
    # `_merge_task_branch`. This replaces the older filter-then-merge
    # loop that could miss a blocked task whose outcome was mutated
    # back to tests_passed.
    merged_tasks_seq: set[int] = set()
    # Global operator-policy inputs to the unified gate (task #2706): the
    # review-enabled feature and the fail-open escape hatch. These are the
    # SAME operator-config trust boundary as today — NOT per-task caller
    # trust signals — so threading them keeps unification behaviour-identical
    # while the per-task hole (review_blocks_merge / doc-only assertion) is
    # gone. Computed once outside the loop.
    _gate_security_review_enabled = is_security_review_enabled(args)
    _gate_block_on_missing = is_feature_enabled(
        getattr(args, "dispatch_config", None) or {},
        "security_review_block_on_missing_artifact",
    )
    if use_worktrees:
        for r in results:
            if isinstance(r, Exception):
                continue
            task_id = r["task"]["id"]
            if task_id not in worktree_dirs:
                continue
            if merge_guard is None:
                # The default branch was never pinned, so a merge could not
                # be checked against it: nothing is merged this run.
                continue
            branch_name = f"forge-task-{task_id}"
            try:
                # Task #2706: the unified gate now computes its own single
                # GateDecision from ground truth (the real branch diff +
                # on-disk artifact) INSIDE _gated_merge_task. The parallel
                # loop no longer forwards any per-task caller trust signal
                # (the old review_blocks_merge / review_skipped_doc_only
                # short-circuit that could disable the gate). doc-only-ness
                # is re-derived from the diff; the defensive invariant in
                # _merge_task_branch still re-reads SECURITY-REVIEW-<id>.md
                # and fails closed (task #2451/#2488/#2493 preserved).
                merge_status = await _gated_merge_task(
                    repo=project_dir,
                    branch=branch_name,
                    outcome=r["outcome"],
                    task_id=task_id,
                    project_context=project_context,
                    security_review_enabled=_gate_security_review_enabled,
                    block_on_missing=_gate_block_on_missing,
                    guard=merge_guard,
                    worktree_dir=worktree_dirs[task_id],
                )
            except SecurityGateBypassError as exc:
                # Phase K (F-04): narrow from `except Exception` so genuine
                # bugs surface instead of being silently swallowed and a
                # merge gets recorded as a no-op. The defensive invariant
                # firing is an EXPECTED gating event — we log it and
                # continue to the next task. Any other exception type
                # propagates normally (treated by asyncio.gather's caller
                # the same way other unhandled errors are).
                _gate_audit_log(
                    f"task={task_id} event=defensive-invariant-blocked "
                    f"branch={branch_name} detail={exc}",
                    task_id=task_id,
                    event="defensive-invariant-blocked",
                )
                merge_guard.outcomes[task_id] = MergeOutcome(
                    "blocked", f"defensive invariant: {exc}",
                )
                continue
            if merge_status == "merged":
                r["merge_ok"] = True
                merged_tasks_seq.add(task_id)

    # dispatch-03: the default branch must still be where the orchestrator's
    # own merges left it. Checked BEFORE any status is written, so a late
    # movement (a reset that dropped a merge, say) leaves every task blocked.
    if merge_guard is not None:
        await merge_guard.verify("end-of-run")
        if merge_guard.tripped:
            # Keep every task branch: the default branch may no longer hold
            # the commits merged from them.
            merged_tasks_seq.clear()
    for r in results:
        if isinstance(r, Exception) or not r.get("needs_merge"):
            continue
        _write_status_after_merge(r, merge_guard, merge_guard_error)

    # Clean up worktrees — only delete branches that were successfully merged.
    # Helper is natively async; per-task `git worktree remove` and
    # `git branch -D` calls do not block the event loop.
    if use_worktrees:
        await _cleanup_worktrees(
            project_dir, worktree_dirs, merged_tasks_seq, worktree_base,
        )
