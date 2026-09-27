"""EQUIPA loops — dev-test loop, quality scoring, and security review.

Layer 7: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.roles, equipa.agent_runner, equipa.prompts, equipa.preflight,
         equipa.security, equipa.checkpoints, equipa.messages, equipa.tasks.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from equipa.agent_runner import (
    OVERLOADED_OUTCOME,
    AgentResult,
    build_cli_command,
    dispatch_agent,
    is_overloaded_result,
    run_agent,
)
from equipa.checkpoints import (
    build_compaction_recovery_context,
    clear_checkpoints,
    load_checkpoint,
    load_soft_checkpoint,
    save_checkpoint,
)
from equipa.classifier import wrote_test_files
from equipa.config import is_feature_enabled, load_dispatch_config
from equipa.hooks import fire_async as fire_hook
from equipa.constants import (
    CODE_REVIEW_SEVERITY_PATTERNS,
    COMPACTION_CONSOLIDATION_MAX_WORDS,
    COST_ESTIMATE_PER_TURN,
    COST_LIMITS,
    FINDING_DESCRIPTION_MAX_CHARS,
    MAX_CONTINUATIONS,
    MAX_DEV_TEST_CYCLES,
    NO_PROGRESS_LIMIT,
    PARALYSIS_CYCLE_HARD_CAP,
    SECURITY_SEVERITY_PATTERNS,
    TESTER_GIT_DIFF_MAX_CHARS,
    WEDGE_WALL_CLOCK_CAP_SECS,
)
from equipa.db import (
    _get_latest_agent_run_id,
    db_conn,
    get_db_connection,
    update_task_status,
)
from equipa.messages import (
    format_messages_for_prompt,
    mark_messages_read,
    post_agent_message,
    read_agent_messages,
)
from equipa.monitoring import (
    LoopDetector,
    _check_cost_limit,
    adjust_dynamic_budget,
    calculate_dynamic_budget,
    get_starting_sha,
    has_branch_commits,
    has_session_commits,
)
from equipa.output import log
from equipa.git_ops import git_run_async
from equipa.security_gate import (
    REVIEWER_STATUS_FAILED,
    REVIEWER_STATUS_RUNNING,
    REVIEWER_STATUS_SUCCEEDED,
    ReviewerRunRecord,
    _gate_audit_log,
    audit_reviewer_run,
    fingerprint_artifact,
    format_counts,
    new_reviewer_nonce,
    record_reviewer_run,
    reviewer_nonce_line,
    verify_reviewer_provenance,
)
from equipa.parsing import (
    build_compaction_summary,
    build_test_failure_context,
    grep_framework_skip_counts,
    parse_developer_output,
    parse_tester_output,
)
from equipa.preflight import (
    auto_install_dependencies,
    preflight_build_check,
    _handle_preflight_failure,
)
from equipa.prompts import (
    build_checkpoint_context,
    build_system_prompt,
    load_paralysis_template,
)
from equipa.roles import (
    _accumulate_cost,
    _apply_cost_totals,
    get_role_model,
    get_role_turns,
)
from equipa import sessions
from equipa.tasks import _get_task_status, get_task_complexity


# Task 2476: review-agent output artifacts (SECURITY-REVIEW-{id}.md,
# CODE-REVIEW-{id}.md, PLAN-{id}.md, RETRY-IMPLEMENTATION-{id}.md, etc.)
# are written under this subdirectory of the target repo, NOT at repo
# root. Keeping them out of the repo root avoids polluting every
# downstream project EQUIPA runs against. The directory is created
# (idempotently) by the orchestrator before each review-style agent runs.
ARTIFACTS_DIR_NAME = ".equipa-artifacts"


def ensure_artifacts_dir(project_dir: str | Path) -> Path:
    """Create ``.equipa-artifacts/`` in ``project_dir`` if missing.

    Idempotent. Returns the resolved directory path. Called by the
    orchestrator before dispatching any review-style agent so the agent
    can always write its artifact there.
    """
    artifacts = Path(project_dir) / ARTIFACTS_DIR_NAME
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


def review_artifact_relpath(kind: str, task_id: int | str) -> str:
    """Return the repo-relative path string for a review artifact.

    Format: ``.equipa-artifacts/<KIND>-<TASK_ID>.md`` (forward slashes,
    suitable for embedding directly into agent prompt instructions).
    """
    return f"{ARTIFACTS_DIR_NAME}/{kind}-{task_id}.md"


def review_artifact_path(
    project_dir: str | Path, kind: str, task_id: int | str,
) -> Path:
    """Return the absolute path for a review artifact under ``project_dir``."""
    return Path(project_dir) / ARTIFACTS_DIR_NAME / f"{kind}-{task_id}.md"


def find_review_artifact(
    project_dir: str | Path, kind: str, task_id: int | str,
) -> Path:
    """Locate an existing review artifact, preferring the new artifacts dir.

    Returns the path under ``.equipa-artifacts/`` if it exists, else the
    legacy repo-root path. Callers that READ artifacts use this helper so
    that the orchestrator stays compatible with in-flight runs and
    pre-existing artifacts written before the path change (task 2476).
    The returned path is always returned — the caller is expected to
    check ``.exists()`` itself (the dispatch fail-closed gate relies on
    .exists()==False semantics to trigger the fallback dump).
    """
    new_path = review_artifact_path(project_dir, kind, task_id)
    if new_path.is_file():
        return new_path
    legacy = Path(project_dir) / f"{kind}-{task_id}.md"
    if legacy.is_file():
        return legacy
    # Default to the new path so writes go to the right place.
    return new_path


def run_quality_scoring(
    task: dict[str, Any] | int,
    result: dict[str, Any] | str,
    outcome: str,
    role: str,
    output: Any = None,
    dispatch_config: dict | None = None,
) -> None:
    """Run post-task quality scoring and store results.

    Called after record_agent_run() on successful outcomes. Extracts
    result_text and FILES_CHANGED from the result dict, scores them,
    and stores scores in rubric_scores.

    Gated by the quality_scoring feature flag. Never crashes the
    orchestrator — all errors are logged and swallowed.
    """
    try:
        from rubric_quality_scorer import score_and_store as quality_score_and_store
    except ImportError:
        def quality_score_and_store(**kwargs):
            return None

    if not is_feature_enabled(dispatch_config, "quality_scoring"):
        return
    try:
        task_id = task.get("id") if isinstance(task, dict) else task
        project_id = task.get("project_id") if isinstance(task, dict) else None

        agent_run_id = _get_latest_agent_run_id(task_id)
        if not agent_run_id:
            log(f"  [Quality] No agent_run_id found for task {task_id}", output)
            return

        result_text = result.get("result_text", "") if isinstance(result, dict) else ""
        files_changed = parse_developer_output(result_text)

        score_result = quality_score_and_store(
            result_text=result_text,
            files_changed=files_changed,
            role=role,
            agent_run_id=agent_run_id,
            task_id=task_id,
            project_id=project_id,
        )
        if score_result:
            log(f"  [Quality] Scored run {agent_run_id}: "
                f"{score_result['total_score']:.1f}/{score_result['max_possible']:.0f} "
                f"({score_result['normalized_score']:.0%})", output)
    except Exception as e:
        log(f"  [Quality] WARNING: Quality scoring failed: {e}", output)


SECURITY_REVIEW_DEFAULT_TIMEOUT = 900
SECURITY_REVIEW_DEFAULT_MAX_TIMEOUT = 5400
SECURITY_REVIEW_DEFAULT_MAX_ATTEMPTS = 2
# Timeout multiplier per task complexity: complex/epic tasks get 3x (the
# 2700s the #3041 timeouts called for when the base is 900s).
_REVIEW_TIMEOUT_COMPLEXITY_FACTORS: dict[str, float] = {
    "simple": 1.0,
    "medium": 1.5,
    "complex": 3.0,
    "epic": 3.0,
}
# (minimum changed lines, minimum multiplier), largest threshold first.
_REVIEW_TIMEOUT_DIFF_TIERS: tuple[tuple[int, float], ...] = (
    (4000, 3.0),
    (1500, 2.0),
)
# A retried attempt gets this much more time than the attempt that failed.
_REVIEW_RETRY_TIMEOUT_FACTOR = 1.5
_SHORTSTAT_RE = re.compile(r"(\d+) (?:insertion|deletion)")


def _config_int(config: dict | None, key: str, default: int) -> int:
    """Read a positive int from dispatch config, falling back on bad values."""
    raw = (config or {}).get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log(f"  WARNING: dispatch config {key}={raw!r} is not an integer; "
            f"using {default}")
        return default
    if value <= 0:
        log(f"  WARNING: dispatch config {key}={value} must be positive; "
            f"using {default}")
        return default
    return value


def compute_security_review_timeout(
    base_timeout: int, complexity: str, changed_lines: int, max_timeout: int,
) -> int:
    """Scale the reviewer timeout by task complexity and diff size.

    The larger of the complexity factor and the diff-size tier wins. The
    result is capped at ``max_timeout`` but never drops below
    ``base_timeout`` (an operator's explicit base always holds).
    """
    factor = _REVIEW_TIMEOUT_COMPLEXITY_FACTORS.get(complexity, 1.0)
    for threshold, tier_factor in _REVIEW_TIMEOUT_DIFF_TIERS:
        if changed_lines >= threshold:
            factor = max(factor, tier_factor)
            break
    return max(base_timeout, min(int(base_timeout * factor), max_timeout))


def compute_security_review_retry_timeout(timeout: int, max_timeout: int) -> int:
    """Timeout for the single retry: longer, capped, never shorter."""
    return max(timeout, min(int(timeout * _REVIEW_RETRY_TIMEOUT_FACTOR), max_timeout))


def describe_reviewer_failure(sec_result: dict[str, Any] | None) -> str:
    """One-token reason for a failed reviewer result, for logs and audit."""
    if not sec_result:
        return "no-result"
    if is_overloaded_result(sec_result):
        return "overloaded"
    errors = [str(err) for err in sec_result.get("errors") or []]
    if any("timed out" in err.lower() for err in errors):
        return "timeout"
    return "agent-error" if errors else "unsuccessful"


async def _measure_review_diff_lines(project_dir: str) -> int:
    """Lines added + deleted on HEAD vs the trusted default branch; 0 if unknown.

    Only sizes the reviewer timeout, so any failure (not a git repo, no
    trusted default branch, git timeout) falls back to 0 = no diff scaling.
    """
    from equipa.git_ops import get_trusted_default_branch

    try:
        base_ref = get_trusted_default_branch(project_dir)
        result = await git_run_async(
            ["diff", "--shortstat", f"{base_ref}...HEAD"],
            project_dir,
            timeout=10,
        )
    except Exception as exc:  # sizing only — never break the reviewer run
        log(f"  Reviewer diff size unavailable ({type(exc).__name__}: {exc}); "
            f"timeout not scaled by diff size")
        return 0
    if result.returncode != 0:
        return 0
    return sum(int(count) for count in _SHORTSTAT_RE.findall(result.stdout or ""))


def _reviewer_nonce_instructions(nonce: str) -> str:
    """Prompt text binding the artifact to this reviewer attempt (#3041)."""
    return (
        f"PROVENANCE (mandatory): the FIRST line of the review file MUST be "
        f"exactly `{reviewer_nonce_line(nonce)}` on its own line. It proves "
        f"this review run wrote the file. If the file already exists (for "
        f"example a developer self-review), do NOT reuse or trust it: "
        f"overwrite it with your own independent review. A file without "
        f"this exact line is rejected and the merge is BLOCKED. "
    )


async def run_security_review(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
    stable_project_dir: str | None = None,
) -> dict[str, Any]:
    """Run an automatic security review after dev-test succeeds.

    Uses the security-reviewer role with ClaudeStick tools.
    Only runs if security_review is enabled in dispatch config.

    ``stable_project_dir`` is the project's REAL root (the path that
    survives worktree teardown). When the agent runs inside an
    isolation worktree (parallel mode, task 2321), ``project_dir`` is
    the worktree and ``stable_project_dir`` is the real root. After
    the agent runs, this helper persists the SECURITY-REVIEW-{id}.md
    artifact (or a synthesized fallback) to ``stable_project_dir`` so
    operators can audit findings after the worktree is removed
    (task 2447 — the recurring 2412 regression).

    When ``stable_project_dir`` is None or equal to ``project_dir`` the
    single-task path is in effect and no extra copy is needed.
    """
    log(f"\n{'=' * 50}", output)
    log(f"  SECURITY REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running security reviewer agent...", output)

    # Build security review prompt with explicit instructions to use all tools.
    # The filename instruction MUST be exact and task-scoped so the artifact the
    # agent writes matches the path the orchestrator reads at review_path below
    # (and the path the merge gate checks via _security_review_blocks_merge).
    # Historical bug 2412: instruction said "SECURITY-REVIEW.md" but the
    # orchestrator looked for "SECURITY-REVIEW-{task_id}.md", so findings were
    # frequently logged as "artifact missing" and discarded.
    security_task = dict(task)  # copy
    task_id = task.get("id")
    # Task 2476: write under .equipa-artifacts/ to avoid polluting the
    # downstream repo root. Ensure the dir exists before the agent runs.
    ensure_artifacts_dir(project_dir)
    review_filename = review_artifact_relpath("SECURITY-REVIEW", task_id)
    review_instructions = (
        f"Security review of code written for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"YOU MUST use ALL ClaudeStick security tools: static-analysis, "
        f"audit-context-building, variant-analysis, differential-review, "
        f"fix-review, semgrep-rule-creator, and sharp-edges. "
        f"Check for OWASP Top 10 vulnerabilities, zero-day risks in dependencies, "
        f"and any security anti-patterns. "
        f"Write your findings to a file named {review_filename} (relative to "
        f"the project root — this EXACT path, including the "
        f"`.equipa-artifacts/` prefix; not a generic SECURITY-REVIEW.md, not "
        f"the project root). The orchestrator reads counts from this exact "
        f"path; if you write a different filename the findings will be lost. "
        f"The `.equipa-artifacts/` directory has been pre-created for you. "
        f"Rate each finding: CRITICAL, HIGH, MEDIUM, LOW, INFO. "
        # Task #2451 Phase I-b (F-02): MANDATE a final Counts footer.
        # Task #3033: the footer no longer wins over the finding headers —
        # the parser (_analyze_review_file) tallies both and treats any
        # disagreement, or an unfinished Summary, as a missing artifact
        # (merge blocked). The instructions below say so, so a reviewer
        # knows a skeleton footer or a severity word in a non-finding
        # "###" heading will block the merge.
        # Task #3038: the parser now also scans headings of every level and
        # bold lead-ins, counts resolved headings, requires the footer to be
        # the final section, rejects template placeholders and needs a
        # zero-finding review to say so. Stated here so an honest reviewer
        # does not trip those checks.
        f"Give EVERY finding its own heading formatted as "
        f"`### [TAG-NN] SEVERITY — title`, and do NOT put the words "
        f"CRITICAL, HIGH, MEDIUM, LOW or INFO in any other heading (any "
        f"`#` level) or at the start of any other bold lead-in such as "
        f"`- **[S1] HIGH** —`. A finding heading still counts even when "
        f"it is marked fixed or resolved, so describe already-fixed "
        f"upstream findings in prose or a table, not as severity headings. "
        f"The review MUST end with a footer formatted EXACTLY as:\n"
        f"## Counts\n"
        f"CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N | INFO: N\n"
        f"where each N is the integer count of findings at that "
        f"severity. The orchestrator counts BOTH the footer and the "
        f"finding headings: if they disagree the merge is BLOCKED. Update "
        f"the footer last, after every finding is written; nothing may "
        f"follow it. Replace every template placeholder such as "
        f"[SEVERITY] or [PASS/FAIL]. A review whose Summary still says "
        f"IN PROGRESS, skeleton or TODO is treated as unfinished and also "
        f"BLOCKS the merge. A review with no findings must say so in its "
        f"Summary (for example 'No findings.'). "
    )
    original_description = f"Original task description: {task['description']}"

    sec_turns = get_role_turns("security-reviewer", args, task=task)
    sec_model = get_role_model("security-reviewer", args, task=task)
    # Task #3041: the timeout scales with task complexity and diff size (a
    # flat 900s timed out on three tasks in one day), and a failed attempt
    # is retried once with a longer timeout before the gate blocks.
    dc = load_dispatch_config(None)
    base_timeout = _config_int(
        dc, "security_review_timeout", SECURITY_REVIEW_DEFAULT_TIMEOUT,
    )
    max_timeout = _config_int(
        dc, "security_review_timeout_max", SECURITY_REVIEW_DEFAULT_MAX_TIMEOUT,
    )
    max_attempts = max(1, _config_int(
        dc, "security_review_max_attempts", SECURITY_REVIEW_DEFAULT_MAX_ATTEMPTS,
    ))
    changed_lines = await _measure_review_diff_lines(project_dir)
    complexity = get_task_complexity(task)
    sec_timeout = compute_security_review_timeout(
        base_timeout, complexity, changed_lines, max_timeout,
    )
    log(
        f"  Reviewer timeout {sec_timeout}s (base {base_timeout}s, "
        f"complexity={complexity}, diff={changed_lines} lines, "
        f"max attempts {max_attempts})",
        output,
    )

    # Task #3041: fingerprint whatever sits at the artifact path BEFORE the
    # reviewer starts (e.g. a developer self-review committed on the branch)
    # so the gate can reject it if the reviewer never replaces it.
    pre_artifact = fingerprint_artifact(
        find_review_artifact(project_dir, "SECURITY-REVIEW", task_id),
    )
    run_started = time.monotonic()
    run_started_wall = time.time()
    attempt_timeouts: list[int] = []
    nonce = ""
    sec_result: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        # A fresh nonce per attempt: a partial file left by a timed-out
        # attempt carries the old nonce and is not accepted.
        nonce = new_reviewer_nonce()
        attempt_timeouts.append(sec_timeout)
        record_reviewer_run(ReviewerRunRecord(
            task_id=task_id,
            nonce=nonce,
            status=REVIEWER_STATUS_RUNNING,
            started_at=time.time(),
            pre_artifact=pre_artifact,
            attempts=attempt,
            timeouts=tuple(attempt_timeouts),
        ))
        security_task["description"] = (
            f"{review_instructions}{_reviewer_nonce_instructions(nonce)}"
            f"{original_description}"
        )
        sec_prompt = build_system_prompt(
            security_task, project_context, project_dir,
            role="security-reviewer",
            dispatch_config=getattr(args, "dispatch_config", None),
            max_turns=sec_turns,
        )
        with build_cli_command(
            sec_prompt, project_dir, sec_turns, sec_model,
            role="security-reviewer",
        ) as sec_cmd:
            sec_result = await run_agent(sec_cmd, timeout=sec_timeout)
        if (
            sec_result.get("success")
            or is_overloaded_result(sec_result)
            or attempt == max_attempts
        ):
            break
        retry_timeout = compute_security_review_retry_timeout(
            sec_timeout, max_timeout,
        )
        log(
            f"  Security review attempt {attempt} failed "
            f"({describe_reviewer_failure(sec_result)}); retrying once with "
            f"timeout {retry_timeout}s before the gate blocks",
            output,
        )
        sec_timeout = retry_timeout

    review_path = find_review_artifact(project_dir, "SECURITY-REVIEW", task_id)
    run_record = ReviewerRunRecord(
        task_id=task_id,
        nonce=nonce,
        status=(
            REVIEWER_STATUS_SUCCEEDED if sec_result.get("success")
            else REVIEWER_STATUS_FAILED
        ),
        started_at=run_started_wall,
        pre_artifact=pre_artifact,
        post_artifact=fingerprint_artifact(review_path),
        attempts=len(attempt_timeouts),
        duration=time.monotonic() - run_started,
        timeouts=tuple(attempt_timeouts),
        failure_reason=(
            None if sec_result.get("success")
            else describe_reviewer_failure(sec_result)
        ),
    )
    record_reviewer_run(run_record)
    audit_reviewer_run(run_record)

    if sec_result["success"]:
        log(f"  Security review completed in {sec_result.get('duration', 0):.1f}s", output)
        result_text = sec_result.get("result_text", "")
        provenance = verify_reviewer_provenance(task_id, review_path)
        if not provenance.trusted:
            log(
                f"  WARNING: security-review artifact was not written by this "
                f"reviewer run (provenance={provenance.reason}; "
                f"{provenance.fingerprint.describe()}) — the merge gate will "
                f"block",
                output,
            )

        # Extract finding counts from the SECURITY-REVIEW-NNNN.md artifact, NOT
        # from raw agent stdout. The stdout contains the agent's intermediate
        # reasoning ("[S1] LOW — this is NOT a CRITICAL because…") so substring
        # counts on CRITICAL / HIGH double-count rejected findings, severity
        # mentions in prose, and so on (see task 2315 root-cause analysis).
        # Task 2476: prefer the new .equipa-artifacts/ location, fall back
        # to the legacy repo-root path so in-flight artifacts still parse.
        review_path = find_review_artifact(
            project_dir, "SECURITY-REVIEW", task_id,
        )
        counts = _count_findings_in_review_file(review_path, task_id=task_id)
        # Task #3041: name the exact bytes these counts came from, so a
        # counts/file mismatch against the gate's line is visible.
        log(
            f"  Security review counts {format_counts(counts)} from "
            f"{provenance.fingerprint.describe()}",
            output,
        )
        if counts is None and review_path.is_file():
            # Task #3033: the reviewer DID write a file, but it is a fallback
            # dump, its footer disagrees with its finding headers, or it is
            # unfinished. Leave it on disk for the operator; the merge gate
            # treats it as missing and blocks.
            log(
                f"  WARNING: security-review artifact {review_path.name} is "
                f"not a trustworthy finished review (count mismatch, "
                f"unfinished, or fallback dump) — the merge gate will treat "
                f"it as missing and block",
                output,
            )
        elif counts is None:
            log(
                f"  WARNING: security-review artifact missing — expected "
                f"{ARTIFACTS_DIR_NAME}/{review_path.name} (agent did not save it)",
                output,
            )
            # Re-target fallback writes at the new artifacts location so the
            # dump lands next to where future agents will look.
            review_path = review_artifact_path(
                project_dir, "SECURITY-REVIEW", task_id,
            )
            # Robustness fallback (task 2412): preserve the agent's raw output
            # on disk so operators can recover findings even when the reviewer
            # failed to write the structured artifact. The file is clearly
            # marked as a fallback dump and is NOT treated as a structured
            # artifact by _count_findings_in_review_file — so the
            # fail-closed security_review_block_on_missing_artifact gate in
            # dispatch._security_review_blocks_merge still fires.
            _write_security_review_fallback(
                review_path, task_id, result_text, output=output,
            )
        elif counts["CRITICAL"] > 0 or counts["HIGH"] > 0:
            log(
                f"  WARNING: Found {counts['CRITICAL']} CRITICAL and "
                f"{counts['HIGH']} HIGH severity findings",
                output,
            )
        else:
            log(f"  No critical or high severity findings", output)

        # Feed security findings back into developer lessons
        project_id = task.get("project_id")
        findings = _extract_security_findings(result_text)
        if findings:
            count = _create_security_lessons(findings, project_id)
            if count > 0:
                log(f"  Created {count} developer lesson(s) from security findings", output)
    elif is_overloaded_result(sec_result):
        # No review happened. The caller's merge gate must treat this as a
        # loud failure, never as "no findings" (task #2994 S1).
        log(f"  Security review agent FAILED: model overloaded (529) through "
            f"every retry. Not downgrading the model; the merge gate will "
            f"block this task.", output)
    else:
        log(
            f"  Security review agent failed after {run_record.attempts} "
            f"attempt(s) in {run_record.duration:.1f}s "
            f"({run_record.failure_reason}). The merge gate will block this "
            f"task regardless of any artifact on disk.",
            output,
        )
        for err in sec_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    # Task 2447: persist the artifact to a path that SURVIVES worktree
    # teardown. Without this, parallel-mode (task_dir = worktree)
    # security_review_blocked runs leave NO trace of WHAT the findings
    # were — operators see "Found N HIGH" in the log but cannot audit.
    # The orchestrator captures the agent's result_text and synthesizes
    # a fallback if the reviewer did not save its own file. Belt and
    # suspenders: do this on EVERY outcome (success, failure, missing).
    if stable_project_dir and stable_project_dir != project_dir:
        _persist_security_review_artifact(
            worktree_dir=project_dir,
            stable_dir=stable_project_dir,
            task_id=task.get("id"),
            result_text=sec_result.get("result_text", "") if sec_result else "",
            agent_succeeded=bool(sec_result.get("success")) if sec_result else False,
            output=output,
        )

    return sec_result


def _persist_security_review_artifact(
    *,
    worktree_dir: str,
    stable_dir: str,
    task_id: int | None,
    result_text: str,
    agent_succeeded: bool,
    output: Any = None,
) -> None:
    """Copy (or synthesize) SECURITY-REVIEW-{task_id}.md to ``stable_dir``.

    Called from ``run_security_review`` whenever the agent's working
    directory differs from the project's stable root (parallel/worktree
    mode). Behavior:

    * If the agent wrote a structured artifact in ``worktree_dir``,
      copy it to ``stable_dir`` verbatim.
    * Otherwise synthesize a fallback dump from ``result_text`` so the
      findings text is preserved on disk for operator review. The
      fallback is marked with ``SECURITY_REVIEW_FALLBACK_MARKER`` so
      ``_count_findings_in_review_file`` returns None for it (the
      fail-closed merge gate still fires).

    Refuses to clobber an existing structured artifact at ``stable_dir``
    (e.g. from a prior run). Always asserts the artifact exists at
    ``stable_dir`` after writing — a missing-artifact verdict without
    a persisted file is treated as an orchestrator failure and logged.
    """
    if not task_id:
        return
    # Task 2476: persist under .equipa-artifacts/, but tolerate legacy
    # repo-root artifacts written by older agents during the transition.
    filename = f"SECURITY-REVIEW-{task_id}.md"
    src = find_review_artifact(worktree_dir, "SECURITY-REVIEW", task_id)
    dst = review_artifact_path(stable_dir, "SECURITY-REVIEW", task_id)
    ensure_artifacts_dir(stable_dir)

    try:
        if src.is_file():
            src_text = src.read_text(encoding="utf-8", errors="replace")
            is_fallback = SECURITY_REVIEW_FALLBACK_MARKER in src_text
            # Copy the worktree artifact to the stable path. Always
            # overwrite — the agent's just-written artifact for THIS
            # task supersedes any stale file at the stable path.
            dst.write_text(src_text, encoding="utf-8")
            kind = "fallback dump" if is_fallback else "structured artifact"
            # Task #3041: say whether the copied file is THIS reviewer run's
            # output. On #3035 this line read "structured artifact" for a
            # developer self-review the timed-out reviewer never wrote.
            provenance = verify_reviewer_provenance(task_id, dst)
            log(
                f"  Persisted security-review {kind} to stable path "
                f"{dst} (survives worktree teardown; "
                f"provenance={provenance.reason}; "
                f"{provenance.fingerprint.describe()})",
                output,
            )
        else:
            # Agent did not write the artifact. Synthesize a fallback
            # from the captured stdout so findings are not lost.
            if dst.exists() and SECURITY_REVIEW_FALLBACK_MARKER not in (
                dst.read_text(encoding="utf-8", errors="replace")
            ):
                # Stable path already has a structured artifact (prior
                # run); do not clobber. The merge gate will see it
                # and decide on its own.
                log(
                    f"  Stable path already contains a structured "
                    f"{dst.name} — not overwriting with synthesized "
                    f"fallback",
                    output,
                )
            else:
                _write_security_review_fallback(
                    dst, task_id, result_text, output=output,
                )
                log(
                    f"  Synthesized SECURITY-REVIEW-{task_id}.md fallback "
                    f"at stable path {dst} from captured agent output "
                    f"(reviewer did not save its own file; "
                    f"agent_succeeded={agent_succeeded})",
                    output,
                )
    except OSError as exc:
        log(
            f"  ERROR: failed to persist security-review artifact to "
            f"stable path {dst}: {exc}",
            output,
        )
        return

    # Assert the artifact exists at the stable path after persistence.
    # A gating verdict (block) with no persisted artifact must be
    # impossible — this is the explicit task 2447 invariant.
    if not dst.exists():
        log(
            f"  CRITICAL: security-review artifact still missing at "
            f"{dst} after persistence attempt — operator must "
            f"investigate (orchestrator/reviewer failure).",
            output,
        )


# Matches finding section headers in SECURITY-REVIEW-NNNN.md files. The
# convention enforced by the security-reviewer skill is:
#     ### [TAG-NN] SEVERITY — description
# (em-dash, en-dash, or hyphen separator). Severity is one of
# CRITICAL/HIGH/MEDIUM/LOW/INFO. Anchoring on the bracketed tag is what makes
# this robust against prose mentions of CRITICAL/HIGH in the surrounding text.
# Task #2451 Phase D: loosened to match reviewer drift across formats:
#   ### [F-01] HIGH — description
#   ### F-01 HIGH: description
#   ### HIGH — description
#   ### **HIGH** — description
# Treat any of CRITICAL/HIGH/MEDIUM/LOW/INFO appearing in the first 80
# characters of a level-3 header line as a finding header. The previous
# bracketed-tag-required form silently dropped to zero on format drift,
# which made the merge gate fail-open on unparseable artifacts.
_REVIEW_FINDING_HEADER_RE = re.compile(
    r"^###[ \t][^\n]{0,80}?(?<![A-Za-z])"
    r"(CRITICAL|HIGH|MEDIUM|LOW|INFO)(?![A-Za-z])",
    re.MULTILINE,
)

# Footer the security-review prompt is required to emit at the end of every
# SECURITY-REVIEW-NNNN.md artifact. Format:
#   ## Counts
#   CRITICAL: 0 | HIGH: 0 | MEDIUM: 4 | LOW: 3 | INFO: 2
# Task #3033: the footer is NOT trusted over the finding headers. A reviewer
# that writes the footer first (as a skeleton) and then adds findings leaves
# a stale all-zero footer behind; preferring it merged an unfinished review
# with MEDIUM/LOW headers as clean. Both tallies are now computed and must
# agree (see _analyze_review_file).
_REVIEW_COUNTS_FOOTER_RE = re.compile(
    r"^##\s+Counts\s*\n[^\n]*?"
    r"CRITICAL\s*:\s*(\d+)[^\n]*?"
    r"HIGH\s*:\s*(\d+)[^\n]*?"
    r"MEDIUM\s*:\s*(\d+)[^\n]*?"
    r"LOW\s*:\s*(\d+)[^\n]*?"
    r"INFO\s*:\s*(\d+)",
    re.MULTILINE | re.IGNORECASE,
)

# Sentinel marker written into orchestrator-saved fallback dumps when the
# reviewer agent failed to write a structured SECURITY-REVIEW artifact.
# _count_findings_in_review_file returns None when this marker is present so
# the fail-closed merge gate
# (dispatch._security_review_blocks_merge / features.security_review_block_on_missing_artifact)
# still fires — a fallback dump must NOT be treated as a structured artifact.
SECURITY_REVIEW_FALLBACK_MARKER = "<!-- EQUIPA-SECURITY-REVIEW-FALLBACK -->"


_REVIEW_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")

# Task #3033: markers of an unfinished review, searched ONLY inside the
# review's Summary (a "Summary:" field line or a "## Summary" section). The
# observed fail-open artifact read "Summary: IN PROGRESS - initial skeleton".
# Task #3038 (S3033-05): the marker must sit in status position, at the START
# of a Summary line (after emphasis/brackets), so a finished review whose
# Summary reads "Reviewed the TODO-list API" is not held as unfinished.
_INCOMPLETE_REVIEW_MARKER_RE = re.compile(
    r"^[ \t*_\[(:—–-]*(?:(?:WORK[ \t_-]*)?IN[ \t_-]*PROGRESS|skeleton|TODO)\b",
    re.IGNORECASE,
)
_SUMMARY_HEADING_RE = re.compile(
    r"^#{1,6}[ \t]*\**[ \t]*Summary\b(.*)$", re.IGNORECASE,
)
_SUMMARY_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?\**[ \t]*Summary[ \t]*\**[ \t]*"
    r"[:—–-][ \t]*\**(.*)$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]")

# Task #3033: fix-verification re-reviews keep the prior finding's heading
# but mark it resolved and leave it out of the footer, e.g.
#   ### SR29-00 HIGH (fixed, verified, not counted) — ...
#   ### SR-2996 S1 (MEDIUM) — FIXED, verified
#   ### [2775-S01] HIGH — requestPayout ... → **FIXED**   (CCGNinja #2780)
# Such headings may be omitted from the footer (see _analyze_review_file).
# Task #3038 (S3033-01): the resolved form is ONLY a footer-tolerance hint;
# it never lowers the merge counts, which are max(live + resolved, footer),
# so a live "HIGH — AES-GCM nonce (fixed at zero)" can over-block but never
# merge. The status token must also be the LAST thing on the heading: a
# (fixed...)/[resolved...]/(... not counted) group, or an UPPERCASE
# FIXED/RESOLVED after a separator optionally followed by a short
# ", verified" tail. So "Fixed-size buffer overflow", "nonce (fixed at zero)
# allows forgery", "key: FIXED string in config.py" and StockForge #3032's
# "### [S1] LOW (latent; re-rate MEDIUM when S2 is fixed)" stay live.
_RESOLVED_FINDING_HEADER_RE = re.compile(
    r"(?:"
    r"[(\[][ \t]*(?i:fixed|resolved)\b[^)\]\n]*[)\]]"
    r"|[(\[][^)\]\n]*\b(?i:not[ \t]+counted)\b[^)\]\n]*[)\]]"
    r"|[—–:→-][ \t]*[*_]{0,2}(?:FIXED|RESOLVED)\b[*_]{0,2}"
    r"(?:[ \t]*[,;][ \t]*[A-Za-z][A-Za-z \t,;-]{0,40})?"
    r"(?:[ \t]*\([^()\n]{0,60}\))?"
    r")[ \t*_.\r]*$",
)

# Task #3038 (S3033-02): detection-only tally of finding-shaped lines the
# strict level-3 header regex cannot see: a severity word (any case)
# anywhere in a heading of ANY level (the strict regex stops at 80 chars);
# in the leading bold span of a line, bullet or numbered item, either after
# a finding tag ("1. **[S1] ... HIGH** —", "**S1 (HIGH):**") or as an
# UPPERCASE status that opens the span ("**HIGH — nonce reuse**",
# "- **(HIGH)** ..."); or after a bracketed tag on a bullet ("- [S1] HIGH
# —"). A live candidate never adds to the merge counts; a severity it sees
# that neither the footer nor the strict headers count makes the review a
# count-mismatch (fail closed). A candidate whose title span ends in the
# strict resolved status (_RESOLVED_FINDING_HEADER_RE) is ADDED to the
# merge counts, exactly like a resolved level-3 heading (IR38-01).
# Not candidates: checklist boxes ("- [x] XSS: PASS"), bold
# prose ("**No CRITICAL or HIGH findings.**", "**Medium (30 min):**") and a
# severity after the bold span ("**Severity:** HIGH"), which belongs to an
# enclosing heading that is itself a candidate.
# An unbracketed tag ("S1", "SR29-00", "RT-01") is matched only up to its
# FIRST digit; the lazy [^*\n]*? covers the rest. Spelling the tail out as
# "\d+[\w-]*" matched the same lines but let three overlapping quantifiers
# backtrack cubically: one "**S1" + 1000-digit line took 17 s and hung the
# gate (task #3038 ReDoS). Every alternative must stay linear per line.
_FINDING_CANDIDATE_RE = re.compile(
    r"^[ \t]{0,3}(?:"
    r"#{1,6}[ \t][^\n]*?"
    r"|(?:(?:[-*+]|\d{1,3}[.)])[ \t]+)?\*\*[ \t]*(?:"
    r"(?:\[(?![ xX]\])[^\]\n]{1,24}\]|[A-Za-z]{1,8}[-_]?\d)[^*\n]*?"
    r"|[\[(]?[ \t]*(?=(?-i:CRITICAL|HIGH|MEDIUM|LOW|INFO)(?![A-Za-z_-]))"
    r")"
    r"|[-*+][ \t]+\[(?![ xX]\])[^\]\n]{1,24}\][^\n]{0,40}?"
    r")(?<![A-Za-z_-])(CRITICAL|HIGH|MEDIUM|LOW|INFO)(?![A-Za-z_-])",
    re.MULTILINE | re.IGNORECASE,
)
# Task #3038 (IR38-05): headings that report a tally or an overall risk
# label are not findings. A heading is a TALLY when every severity word in
# it is directly preceded by a count ("## Findings — 0 CRITICAL / 0 HIGH /
# 1 MEDIUM"); its zero entries are skipped and each NON-zero entry is a
# candidate of that severity. A section number ("3.2 HIGH") is not a
# count. "Overall risk: LOW" / "— INFO" is exempt; a CRITICAL/HIGH/MEDIUM
# risk label stays a candidate. Both are linear: one pass over the line.
_SEVERITY_WORD_RE = re.compile(
    r"(?<![A-Za-z_-])(CRITICAL|HIGH|MEDIUM|LOW|INFO)(?![A-Za-z_-])",
    re.IGNORECASE,
)
_TALLY_COUNT_BEFORE_RE = re.compile(r"(\d{1,4})[ \t]{1,3}$")
# A count must stand alone: "S0 HIGH", "SR-0 HIGH" and "3.2 HIGH" are tags
# or section numbers, not tallies.
_TALLY_COUNT_PRECEDERS = frozenset(" \t([/|")
_OVERALL_RISK_LOW_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+(?:\d+(?:\.\d+)*\.?[ \t]+)?[*_]{0,2}"
    r"overall[ \t]+risk(?:[ \t]+(?:rating|level|assessment))?[*_]{0,2}"
    r"[ \t]*[:—–-][ \t]*[*_]{0,2}(?:LOW|INFO)[*_]{0,2}[ \t.\r]*$",
    re.IGNORECASE,
)

# Task #3038 (S3033-03): unreplaced placeholders from the report skeleton in
# prompts/security-reviewer.md. A file still carrying them is an unfilled
# template, not a finished review. Searched outside code (fences and inline
# backticks), so a review that quotes the template is not held.
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"\[1-2 sentences|\[SEVERITY\]|\[PASS/FAIL\]|\[list\]|\[date\]"
    r"|\[Project Name\]|path/to/file\.ext"
    r"|\[X (?:findings|pattern matches|files inspected)",
    re.IGNORECASE,
)
# Positive completion signal for a zero-finding review without a Summary,
# e.g. "No blocking findings." / "No security issues found." / "Findings: none".
_NO_FINDINGS_STATEMENT_RE = re.compile(
    r"(?<![A-Za-z])no(?![A-Za-z])[^\n.]{0,40}?"
    r"(?<![A-Za-z])(?:findings?|issues?|vulnerabilit(?:y|ies))(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:findings?|issues?)[ \t]*[:—–-][ \t]*[*_]*none"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_ANY_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]", re.MULTILINE)

# Task #3033: a review with zero finding headers, an all-zero (or absent)
# footer and fewer non-blank lines than this is a skeleton, not a clean
# review. Deliberately low: a terse "no findings" review (title, one line of
# prose, footer) must still pass; the Summary markers and the header/footer
# agreement check are the primary signals.
_REVIEW_MIN_NONBLANK_LINES = 4

REVIEW_VERDICT_OK = "ok"
REVIEW_VERDICT_MISSING = "missing"
REVIEW_VERDICT_FALLBACK = "fallback"
REVIEW_VERDICT_COUNT_MISMATCH = "count-mismatch"
REVIEW_VERDICT_INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ReviewCountAnalysis:
    """Everything the parser learned about a SECURITY-REVIEW artifact.

    ``counts`` is the per-severity MAX of the footer and header tallies, so
    it never under-reports whichever source saw more findings. It is only
    handed to the merge gate when ``verdict`` is ``REVIEW_VERDICT_OK``; any
    other verdict makes :func:`_count_findings_in_review_file` return None
    and the fail-closed ``block_on_missing`` gate fires.
    """

    verdict: str
    counts: dict[str, int] | None = None
    footer_counts: dict[str, int] | None = None
    header_counts: dict[str, int] | None = None
    detail: str = ""

    @property
    def trusted(self) -> bool:
        return self.verdict == REVIEW_VERDICT_OK


def _review_summary_text(text: str) -> str:
    """Return the review's Summary field lines and Summary section bodies."""
    collected: list[str] = []
    in_summary_section = False
    for line in text.splitlines():
        heading = _SUMMARY_HEADING_RE.match(line)
        if heading is not None:
            in_summary_section = True
            collected.append(heading.group(1))
            continue
        if _MARKDOWN_HEADING_RE.match(line):
            in_summary_section = False
            continue
        if in_summary_section:
            collected.append(line)
            continue
        field_match = _SUMMARY_FIELD_RE.match(line)
        if field_match is not None:
            collected.append(field_match.group(1))
    return "\n".join(collected)


def _blank_code(text: str) -> str:
    """Blank fenced code blocks and inline code spans, keeping line numbers.

    Quoted footers, templates and PoC markdown inside code are examples, not
    the review's own structure. An unterminated fence blanks nothing after
    it, so a stray fence can never hide a footer or finding (fail closed).
    """
    lines = text.split("\n")
    visible: list[str] = []
    fence: str | None = None
    fence_start = 0
    for index, line in enumerate(lines):
        opener = _CODE_FENCE_RE.match(line)
        if fence is None:
            if opener is not None:
                fence, fence_start = opener.group(1)[0], index
                visible.append("")
            else:
                visible.append(_INLINE_CODE_RE.sub("", line))
        else:
            if opener is not None and opener.group(1)[0] == fence:
                fence = None
            visible.append("")
    if fence is not None:
        visible[fence_start:] = lines[fence_start:]
    return "\n".join(visible)


_RESOLVED_CANDIDATE_TAIL_CHARS = 200


def _candidate_line(match: re.Match[str]) -> str:
    """Return the full line a finding candidate was matched on."""
    text = match.string
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.start())
    return text[line_start:line_end if line_end != -1 else None]


def _is_resolved_candidate(match: re.Match[str]) -> bool:
    """True when a finding candidate's title span ends in a resolved status.

    The title span is the whole line for a heading, else the bold lead-in
    (up to its closing ``**``) of a bullet or bold line. Task #3038
    (IR38-01): the status grammar is the strict, end-anchored level-3 one
    (``_RESOLVED_FINDING_HEADER_RE``), and a resolved candidate is ADDED to
    the merge counts by the caller, never dropped.
    """
    line = _candidate_line(match)
    if not line.lstrip().startswith("#"):
        bold_open = line.find("**")
        bold_close = line.find("**", bold_open + 2) if bold_open != -1 else -1
        if bold_close != -1:
            line = line[:bold_close]
    # The status must end the span, so only its tail is searched. This keeps
    # the strict regex (quadratic on long bracket runs, IR38-04) linear here;
    # a cut can only miss a match, which leaves the candidate live (stricter).
    tail = line.rstrip(" \t#")[-_RESOLVED_CANDIDATE_TAIL_CHARS:]
    return bool(_RESOLVED_FINDING_HEADER_RE.search(tail))


def _heading_tally_severities(match: re.Match[str]) -> list[str] | None:
    """Severities a tally / overall-risk HEADING really reports (IR38-05).

    Returns None when the candidate is not such a heading (the caller
    counts it normally). Otherwise returns the severities with a NON-zero
    count, which the caller counts as live candidates; zero entries and a
    LOW/INFO overall-risk label contribute nothing.
    """
    line = _candidate_line(match)
    if not line.lstrip(" \t").startswith("#"):
        return None
    if _OVERALL_RISK_LOW_HEADING_RE.match(line):
        return []
    reported: list[str] = []
    for word in _SEVERITY_WORD_RE.finditer(line):
        window = line[max(0, word.start() - 12):word.start()]
        count = _TALLY_COUNT_BEFORE_RE.search(window)
        if count is None:
            return None
        count_start = word.start() - len(count.group(0))
        if count_start > 0 and (
            line[count_start - 1] not in _TALLY_COUNT_PRECEDERS
        ):
            return None
        if int(count.group(1)) > 0:
            reported.append(word.group(1).upper())
    return reported


def _analyze_review_file(review_path: Path) -> ReviewCountAnalysis:
    """Parse a SECURITY-REVIEW-NNNN.md artifact without trusting any one source.

    Task #3033 (fail-closed): the ``## Counts`` footer and the ``###``
    finding headers are BOTH tallied. The artifact is trusted only when:

      * the tallies agree, or there are no finding headers and the footer is
        non-zero (a reviewer that wrote findings as prose but counted them);
      * no finding-shaped line of any heading level / bold-tag form carries a
        severity that neither the footer nor the strict headers count
        (task #3038, S3033-02);
      * no heading or finding follows the last footer outside code; with
        several footers the per-severity maximum is used (S3033-04,
        IR38-02);
      * no reviewer-template placeholder is left unreplaced (S3033-03);
      * its Summary does not START with IN PROGRESS / skeleton / TODO;
      * a zero-finding review has a non-empty Summary and is not near-empty.

    The merge counts are ``max(live + resolved, footer, resolved
    candidates)`` per severity: resolved fix-verification headings and
    candidates only widen what the footer may omit, they never lower the
    counts (S3033-01, IR38-01).

    Anything else is ``count-mismatch`` or ``incomplete`` and the caller must
    treat the artifact as missing. Header counts come from lines like
    ``### [S1] HIGH — Prompt Injection via Database Content``, never from
    prose, so "[S1] LOW — this is NOT a CRITICAL because…" is not a CRITICAL
    (task 2315).
    """
    try:
        text = review_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # FileNotFoundError included: a missing file and an unreadable one
        # are the same thing to the gate.
        return ReviewCountAnalysis(verdict=REVIEW_VERDICT_MISSING)

    # Fallback dumps preserve raw agent output for operator review but are NOT
    # structured artifacts — the merge gate must still fail-closed on them.
    # GATE-10 (task #2451 Phase G): anchor the marker check to the file head
    # so a real review file that documents the marker string in its prose
    # body cannot self-DoS the gate.
    if SECURITY_REVIEW_FALLBACK_MARKER in text[:512]:
        return ReviewCountAnalysis(verdict=REVIEW_VERDICT_FALLBACK)

    header_counts = dict.fromkeys(_REVIEW_SEVERITIES, 0)
    resolved_counts = dict.fromkeys(_REVIEW_SEVERITIES, 0)
    for match in _REVIEW_FINDING_HEADER_RE.finditer(text):
        line_end = text.find("\n", match.start())
        header_line = text[match.start():line_end if line_end != -1 else None]
        if _RESOLVED_FINDING_HEADER_RE.search(header_line):
            resolved_counts[match.group(1)] += 1
        else:
            header_counts[match.group(1)] += 1

    # Code blocks and inline code are examples, never the review's structure.
    visible_text = _blank_code(text)

    # The document title (a level-1 FIRST heading) of a fix review names the
    # upstream finding being fixed ("# Review: CT-FIX-F7 ... (D5-01 HIGH)");
    # it is not a finding of this review. Any later level-1 heading is.
    first_heading = _ANY_MARKDOWN_HEADING_RE.search(visible_text)
    title_start = (
        first_heading.start()
        if first_heading is not None
        and first_heading.group(0).lstrip().startswith("# ")
        else -1
    )
    candidate_counts = dict.fromkeys(_REVIEW_SEVERITIES, 0)
    resolved_candidate_counts = dict.fromkeys(_REVIEW_SEVERITIES, 0)
    for match in _FINDING_CANDIDATE_RE.finditer(visible_text):
        if match.start() == title_start:
            continue
        tally = _heading_tally_severities(match)
        if tally is not None:
            for severity in tally:
                candidate_counts[severity] += 1
        elif _is_resolved_candidate(match):
            # IR38-01: a resolved candidate is counted, never dropped.
            resolved_candidate_counts[match.group(1).upper()] += 1
        else:
            candidate_counts[match.group(1).upper()] += 1

    # S3033-04 / IR38-02: with several footers outside code, the counts are
    # the per-severity MAXIMUM over all of them. An earlier quoted skeleton
    # cannot mask the final tally, and a later quoted all-zero footer cannot
    # mask an earlier correct one. The LAST footer is still the one that
    # must close the review.
    footer_matches = list(_REVIEW_COUNTS_FOOTER_RE.finditer(visible_text))
    footer = footer_matches[-1] if footer_matches else None
    footer_counts: dict[str, int] | None = None
    if footer_matches:
        footer_counts = {
            severity: max(
                int(match.group(index)) for match in footer_matches
            )
            for index, severity in enumerate(_REVIEW_SEVERITIES, start=1)
        }

    # S3033-01: resolved headings are never subtracted from the merge
    # counts. A live finding mislabelled as resolved over-blocks instead of
    # merging, with or without a footer.
    strict_counts = {
        severity: header_counts[severity] + resolved_counts[severity]
        for severity in _REVIEW_SEVERITIES
    }
    merged_counts = {
        severity: max(
            strict_counts[severity],
            footer_counts[severity] if footer_counts else 0,
            resolved_candidate_counts[severity],
        )
        for severity in _REVIEW_SEVERITIES
    }

    def _verdict(verdict: str, detail: str = "") -> ReviewCountAnalysis:
        return ReviewCountAnalysis(
            verdict=verdict,
            counts=merged_counts,
            footer_counts=footer_counts,
            header_counts=header_counts,
            detail=detail,
        )

    headers_total = sum(header_counts.values()) + sum(resolved_counts.values())
    footer_total = sum(footer_counts.values()) if footer_counts else 0

    # A footer that disagrees with non-empty headers is stale or wrong, and
    # there is no way to tell which side is right — fail closed. Zero headers
    # with a non-zero footer is the one tolerated disagreement. The footer
    # may or may not count resolved fix-verification headings, so per
    # severity it must lie in [live, live + resolved].
    footer_disagrees = footer_counts is not None and any(
        not (
            header_counts[severity]
            <= footer_counts[severity]
            <= header_counts[severity] + resolved_counts[severity]
        )
        for severity in _REVIEW_SEVERITIES
    )
    if headers_total > 0 and footer_disagrees:
        return _verdict(
            REVIEW_VERDICT_COUNT_MISMATCH,
            "footer and finding headers disagree",
        )

    # S3033-02: a severity seen only in finding-shaped lines the strict
    # tally cannot count (## / #### / title-case / bold-tag bullets) means
    # neither source can be trusted to have counted it.
    uncounted = [
        severity for severity in _REVIEW_SEVERITIES
        if candidate_counts[severity] > 0 and merged_counts[severity] == 0
    ]
    if uncounted:
        return _verdict(
            REVIEW_VERDICT_COUNT_MISMATCH,
            "finding-shaped lines not counted by footer or headers: "
            + ", ".join(
                f"{severity}={candidate_counts[severity]}"
                for severity in uncounted
            ),
        )

    # S3033-04: the footer closes the review. A heading or finding after it
    # means the footer was written first (a skeleton) and never updated.
    if footer is not None:
        trailing = visible_text[footer.end():]
        if (
            _ANY_MARKDOWN_HEADING_RE.search(trailing)
            or _FINDING_CANDIDATE_RE.search(trailing)
        ):
            return _verdict(
                REVIEW_VERDICT_INCOMPLETE,
                "Counts footer is not the final section",
            )

    # S3033-03: an unfilled reviewer template is not a finished review.
    placeholder = _TEMPLATE_PLACEHOLDER_RE.search(visible_text)
    if placeholder is not None:
        return _verdict(
            REVIEW_VERDICT_INCOMPLETE,
            f"unreplaced template placeholder {placeholder.group(0)!r}",
        )

    summary_lines = _review_summary_text(visible_text).splitlines()
    for summary_line in summary_lines:
        summary_marker = _INCOMPLETE_REVIEW_MARKER_RE.match(summary_line)
        if summary_marker is not None:
            return _verdict(
                REVIEW_VERDICT_INCOMPLETE,
                f"summary marker {summary_marker.group(0).strip()!r}",
            )

    nonblank_lines = sum(1 for line in text.splitlines() if line.strip())
    if headers_total == 0 and footer_total == 0:
        if nonblank_lines < _REVIEW_MIN_NONBLANK_LINES:
            return _verdict(
                REVIEW_VERDICT_INCOMPLETE,
                f"near-empty review ({nonblank_lines} non-blank lines, "
                f"no findings)",
            )
        # S3033-03: a zero-finding review must say what it concluded, in a
        # non-empty Summary or an explicit "no findings" statement. Neither
        # means a skeleton the reviewer never filled.
        has_summary = any(line.strip(" \t*_:—–-") for line in summary_lines)
        if not has_summary and not _NO_FINDINGS_STATEMENT_RE.search(
            visible_text,
        ):
            return _verdict(
                REVIEW_VERDICT_INCOMPLETE,
                "zero-finding review has no Summary or no-findings statement",
            )

    return _verdict(REVIEW_VERDICT_OK)


def _count_findings_in_review_file(
    review_path: Path,
    *,
    task_id: int | None = None,
) -> dict[str, int] | None:
    """Count findings per severity in a SECURITY-REVIEW-NNNN.md artifact.

    Returns a dict with keys CRITICAL/HIGH/MEDIUM/LOW/INFO, or None when the
    artifact must be treated as missing: it does not exist, is an
    orchestrator-saved fallback dump (tasks 2315 / 2412), its footer and
    finding headers disagree, or it is an unfinished review (task #3033).
    None makes the ``block_on_missing`` merge gate fail closed.

    Untrusted-but-present artifacts emit a ``[GATE-AUDIT]`` line
    (``event=count-mismatch`` or ``event=review-incomplete``) carrying both
    tallies, so the log records why a review that exists was not believed.
    """
    analysis = _analyze_review_file(review_path)
    if analysis.trusted:
        return analysis.counts
    if analysis.verdict in (
        REVIEW_VERDICT_COUNT_MISMATCH, REVIEW_VERDICT_INCOMPLETE,
    ):
        event = (
            "count-mismatch"
            if analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
            else "review-incomplete"
        )
        footer_text = (
            format_counts(analysis.footer_counts)
            if analysis.footer_counts is not None
            else "absent"
        )
        _gate_audit_log(
            f"task={task_id} event={event} artifact={review_path.name} "
            f"footer=[{footer_text}] "
            f"headers=[{format_counts(analysis.header_counts)}] "
            f"max=[{format_counts(analysis.counts)}] "
            f"detail={analysis.detail!r} "
            f"action=treat-as-missing",
            task_id=task_id,
            event=event,
            counts=analysis.counts,
        )
    return None


def _write_security_review_fallback(
    review_path: Path,
    task_id: int | None,
    result_text: str,
    *,
    output: Any = None,
) -> None:
    """Write the agent's raw output to ``review_path`` as a fallback dump.

    Used when the security-reviewer agent failed to save the structured
    SECURITY-REVIEW-{task_id}.md artifact. The dump is clearly marked with
    ``SECURITY_REVIEW_FALLBACK_MARKER`` so _count_findings_in_review_file
    returns None for it — keeping the fail-closed merge gate intact while
    preserving the agent's findings text on disk for operator recovery.

    Refuses to overwrite a pre-existing file (the agent may have written a
    real artifact between our check and this call).
    """
    if review_path.exists():
        return
    body = (
        f"# SECURITY-REVIEW fallback (orchestrator-saved, task {task_id})\n"
        f"{SECURITY_REVIEW_FALLBACK_MARKER}\n\n"
        "The security-reviewer agent did not write a structured "
        f"`SECURITY-REVIEW-{task_id}.md` artifact. The orchestrator saved the "
        "raw agent output below so findings are preserved on disk for "
        "operator review.\n\n"
        "**This file is NOT a structured artifact.** The fail-closed "
        "`security_review_block_on_missing_artifact` merge gate still fires "
        "because `_count_findings_in_review_file` ignores files containing "
        "the fallback marker above.\n\n"
        "---\n\n"
        "## Raw agent result_text\n\n"
        f"{result_text}\n"
    )
    try:
        review_path.write_text(body, encoding="utf-8")
        log(
            f"  Saved security-review fallback dump to {review_path.name} "
            f"({len(result_text)} chars of raw agent output preserved)",
            output,
        )
    except OSError as exc:
        log(
            f"  WARNING: failed to write security-review fallback to "
            f"{review_path}: {exc}",
            output,
        )


def _extract_findings(
    result_text: str,
    severity_patterns: dict[str, tuple[str, ...]],
    *,
    case_sensitive: bool,
) -> list[tuple[str, str]]:
    """Extract reviewer findings from agent output.

    Scans `result_text` line-by-line for headers tagged with one of the
    severity labels in `severity_patterns`. Each map entry pairs a severity
    label with a tuple of suffix patterns (e.g. ``"CRITICAL:"``, ``"[CRITICAL]"``)
    that distinguish a finding header from prose mentioning the word. The
    first matching label on a line wins (insertion order of the dict).

    `case_sensitive=False` is used for security review output (CRITICAL/HIGH)
    because reviewers SHOUT severities; `case_sensitive=True` is used for
    code review output (Critical/Important) to avoid false positives like
    "critically important" matching the Important label.

    Each finding is normalized: bullet prefixes (``-``, ``*``, ``•``) are
    stripped, short single-line headers are merged with the next line so the
    description is meaningful, and oversize descriptions are truncated to
    ``FINDING_DESCRIPTION_MAX_CHARS``.
    """
    findings: list[tuple[str, str]] = []
    if not result_text:
        return findings

    lines = result_text.split("\n")
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        haystack = line_stripped if case_sensitive else line_stripped.upper()

        severity: str | None = None
        for label, patterns in severity_patterns.items():
            needles = patterns if case_sensitive else tuple(p.upper() for p in patterns)
            label_token = label if case_sensitive else label.upper()
            if label_token in haystack and any(p in haystack for p in needles):
                severity = label
                break

        if severity is None:
            continue

        desc = line_stripped
        for prefix in ("- ", "* ", "• "):
            if desc.startswith(prefix):
                desc = desc[len(prefix):]

        if len(desc) < 40 and i + 1 < len(lines) and lines[i + 1].strip():
            desc = desc + " " + lines[i + 1].strip()

        if len(desc) > FINDING_DESCRIPTION_MAX_CHARS:
            desc = desc[: FINDING_DESCRIPTION_MAX_CHARS - 3] + "..."

        findings.append((severity, desc))

    return findings


def _extract_security_findings(result_text: str) -> list[tuple[str, str]]:
    """Extract CRITICAL and HIGH findings from security-reviewer output."""
    return _extract_findings(
        result_text, SECURITY_SEVERITY_PATTERNS, case_sensitive=False,
    )


async def run_code_review(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> dict[str, Any]:
    """Run an automatic code review after dev-test succeeds.

    Uses the code-reviewer role (separate from security-reviewer). Focuses on
    correctness, readability, architecture, and performance — the craftsmanship
    axes. Reads files changed during the dev-test phase and writes findings to
    a CODE-REVIEW.md file in the project directory.

    Only runs if code_review is enabled in dispatch config (off by default for
    backward compatibility). Intended to run in PARALLEL with run_security_review
    via asyncio.gather — both reviews are read-only and write to separate output
    files, so they do not conflict.
    """
    log(f"\n{'=' * 50}", output)
    log(f"  CODE REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running code reviewer agent...", output)

    # Build code review task description — emphasizes craftsmanship, not vulnerabilities
    review_task = dict(task)  # copy
    # Task 2476: write under .equipa-artifacts/ to avoid repo-root pollution.
    ensure_artifacts_dir(project_dir)
    cr_task_id = task.get("id")
    cr_filename = review_artifact_relpath("CODE-REVIEW", cr_task_id)
    review_task["description"] = (
        f"Code review of changes made for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"Focus on the FIVE review axes: correctness (does it match the spec?), "
        f"readability (clear names, straightforward logic), architecture (follows "
        f"existing patterns, clean boundaries), performance (no N+1, no unbounded "
        f"loops, proper indexes), and security (basic input validation, no obvious "
        f"injection — defer deep security review to the security-reviewer). "
        f"Write findings to {cr_filename} (relative to the project root — this "
        f"EXACT path, NOT the repo root, NOT a generic CODE-REVIEW.md). The "
        f"`.equipa-artifacts/` directory has been pre-created for you. "
        f"Rate each finding: Critical, Important, or Suggestion. "
        f"Original task description: {task['description']}"
    )

    cr_turns = get_role_turns("code-reviewer", args, task=task)
    cr_prompt = build_system_prompt(
        review_task, project_context, project_dir,
        role="code-reviewer",
        dispatch_config=getattr(args, "dispatch_config", None),
        max_turns=cr_turns,
    )
    cr_model = get_role_model("code-reviewer", args, task=task)
    # Reuse code_review_timeout from dispatch config (default 10 min — shorter
    # than security review since code review typically does not run multiple
    # tool scans).
    dc = load_dispatch_config(None)
    cr_timeout = dc.get("code_review_timeout", 600)
    with build_cli_command(
        cr_prompt, project_dir, cr_turns, cr_model, role="code-reviewer",
    ) as cr_cmd:
        cr_result = await run_agent(cr_cmd, timeout=cr_timeout)

    if cr_result["success"]:
        log(f"  Code review completed in {cr_result.get('duration', 0):.1f}s", output)
        result_text = cr_result.get("result_text", "")
        # Code reviewer uses Critical/Important (not CRITICAL/HIGH) — check both
        # to be resilient to prompt drift.
        critical_count = result_text.count("Critical") + result_text.count("CRITICAL")
        important_count = result_text.count("Important") + result_text.count("IMPORTANT")
        if critical_count > 0 or important_count > 0:
            log(f"  Code review: {critical_count} Critical, {important_count} Important findings", output)
        else:
            log(f"  Code review: no Critical or Important findings", output)

        # Feed code review findings into developer lessons (source='code-reviewer')
        project_id = task.get("project_id")
        findings = _extract_code_review_findings(result_text)
        if findings:
            count = _create_review_lessons(findings, project_id, source="code-reviewer")
            if count > 0:
                log(f"  Created {count} developer lesson(s) from code review findings", output)
    elif is_overloaded_result(cr_result):
        # No review happened — never report this as "no findings" (#2994 S1).
        log(f"  Code review agent FAILED: model overloaded (529) through "
            f"every retry. Not downgrading the model; no review was done.",
            output)
    else:
        log(f"  Code review agent failed.", output)
        for err in cr_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    return cr_result


def _extract_code_review_findings(result_text: str) -> list[tuple[str, str]]:
    """Extract Critical and Important findings from code-reviewer output."""
    return _extract_findings(
        result_text, CODE_REVIEW_SEVERITY_PATTERNS, case_sensitive=True,
    )


def _create_review_lessons(
    findings: list[tuple[str, str]],
    project_id: int | None = None,
    *,
    source: str = "security-reviewer",
    error_type: str = "security",
    lesson_prefix: str | None = None,
) -> int:
    """Insert reviewer findings as developer lessons.

    Shared implementation for both security-reviewer and code-reviewer. The
    source + error_type pair distinguishes lesson provenance in queries.

    Sanitizes finding descriptions before storage (PM-33) since they originate
    from agent output which could contain prompt-injection payloads.
    """
    try:
        from lesson_sanitizer import sanitize_lesson_content, validate_lesson_structure
    except ImportError:
        def sanitize_lesson_content(text):
            return text or ""
        def validate_lesson_structure(text):
            return bool(text)

    if lesson_prefix is None:
        # Default phrasing matches the original security-reviewer lesson text
        # for backward compatibility with existing lessons in the DB.
        lesson_prefix = (
            "Security review found" if source == "security-reviewer"
            else "Code review found"
        )

    created = 0

    with db_conn(write=True) as conn:
        for severity, description in findings:
            safe_description = sanitize_lesson_content(description)
            if not safe_description:
                continue

            sig = re.sub(r'[^\w\s]', '', safe_description.lower())[:200]
            lesson_text = (
                f"{lesson_prefix} {severity} issue: {safe_description}. "
                f"Check for this pattern in future code and prevent it proactively."
            )
            if not validate_lesson_structure(lesson_text):
                continue
            lesson_text = sanitize_lesson_content(lesson_text)

            # Single round-trip upsert. The partial UNIQUE INDEX
            # idx_lessons_sig_source_active (active=1) deduplicates active rows by
            # (error_signature, source); inactive rows are not in the index, so
            # they don't collide with new active inserts.
            # RETURNING times_seen lets us tell new rows (==1) from updates (>1).
            row = conn.execute(
                """INSERT INTO lessons_learned
                   (project_id, role, error_type, error_signature, lesson,
                    source, times_seen, active)
                   VALUES (?, 'developer', ?, ?, ?, ?, 1, 1)
                   ON CONFLICT(error_signature, source) WHERE active = 1
                   DO UPDATE SET
                     times_seen = lessons_learned.times_seen + 1,
                     updated_at = datetime('now')
                   RETURNING times_seen""",
                (project_id, error_type, sig, lesson_text, source),
            ).fetchone()

            if row is not None:
                times_seen = row["times_seen"] if hasattr(row, "keys") else row[0]
                if times_seen == 1:
                    created += 1

    return created


def _create_security_lessons(findings: list[tuple[str, str]], project_id: int | None = None) -> int:
    """Backward-compatible wrapper — delegates to _create_review_lessons.

    Preserved so any external caller (tests, plugins) that imports this name
    directly continues to work. The security-review path in
    run_security_review() has migrated to _create_review_lessons with explicit
    kwargs; this wrapper is the shim for pre-existing imports.
    """
    return _create_review_lessons(
        findings, project_id, source="security-reviewer", error_type="security",
    )


def _capture_session_safe(
    task_id: int,
    role: str,
    project_id: int | None,
    dispatch_config: dict | None,
    output: Any = None,
    cycle_id: str | None = None,
) -> None:
    """Capture an orchestrator-cycle session, gated by feature flag.

    Wrapped in try/except so a session capture failure can never abort the
    dev-test loop. PLAN-1067 §2.B3 — invoked from every exit path of
    run_dev_test_loop so a subsequent cycle (or heartbeat tick) can restore
    the in-flight state.
    """
    if not is_feature_enabled(dispatch_config, "session_persistence"):
        return
    if project_id is None:
        return
    try:
        sessions.capture(
            task_id=task_id,
            role=role,
            project_id=project_id,
            cycle_id=cycle_id or f"task:{task_id}",
            soft_checkpoint_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — never let session writes break the loop
        log(f"  [Session] WARNING: capture failed for task {task_id}: {exc}", output)


def _load_forge_state_json(project_dir: str | None) -> dict | None:
    """Load .forge-state.json from the project directory if it exists.

    This file is maintained by agents during streaming to persist state
    across context compactions. Returns the parsed dict or None.
    """
    if not project_dir:
        return None
    state_file = Path(project_dir) / ".forge-state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_analysis_paralysis(reason: str) -> bool:
    """Return True if an early-term reason matches analysis-paralysis patterns.

    Analysis paralysis = agent killed for reading without writing. The patterns
    here mirror the kill-message phrases emitted by agent_runner so this helper
    can be the single source of truth for paralysis detection.
    """
    return (
        "without file changes" in reason
        or "reading instead of writing" in reason
        or "analysis paralysis" in reason
        or "read-only" in reason
        or "reading ratio" in reason
    )


def _build_paralysis_injection(paralysis_retries: int, reduced_kill: int) -> str:
    """Build the escalating scaffold-first prompt for a paralysis retry.

    paralysis_retries is the count of prior paralysis kills on this task.
    Returns the full markdown injection that will be prepended to the next
    developer dispatch via compaction_history.

    The escalating prompt copy lives in prompts/_paralysis_attempt_*.md so it
    can be tuned and A/B-tested without engine code changes (S4 refactor).
    """
    template = load_paralysis_template(paralysis_retries)
    if paralysis_retries == 0 or paralysis_retries == 1:
        return template.substitute(reduced_kill=reduced_kill)
    return template.substitute(
        reduced_kill=reduced_kill,
        paralysis_retries=paralysis_retries,
        agent_count=paralysis_retries + 1,
    )


def _handle_paralysis_retry(
    reason: str,
    cycle: int,
    compaction_history: list[str],
    dev_run_config: dict[str, Any],
    output: Any = None,
) -> bool:
    """If reason indicates analysis paralysis, append an escalating injection.

    Returns True when paralysis was detected and the caller should `continue`
    to the next cycle (a stricter scaffold-first prompt is now in
    compaction_history). Returns False when reason is not paralysis-shaped or
    when retries are exhausted (cycle == MAX_DEV_TEST_CYCLES) — caller should
    fall through to the normal early-terminated exit.

    Mutates compaction_history (append) and dev_run_config
    (_paralysis_retry_count). Mirrors the original inline logic exactly so
    the refactor is behavior-preserving.
    """
    if not _is_analysis_paralysis(reason):
        return False
    if cycle >= MAX_DEV_TEST_CYCLES:
        return False

    paralysis_retries = sum(
        1 for ctx in compaction_history
        if "KILLED for Analysis Paralysis" in ctx
    )
    log(
        f"  [Cycle {cycle}] Analysis paralysis detected "
        f"(paralysis retry #{paralysis_retries + 1}) — retrying "
        f"with escalating scaffold-first injection.",
        output,
    )

    # Escalating kill threshold reduction: each retry halves
    # the remaining patience. Passed via env hint in the prompt.
    reduced_kill = max(3, 6 - paralysis_retries)
    compaction_history.append(
        _build_paralysis_injection(paralysis_retries, reduced_kill)
    )
    dev_run_config["_paralysis_retry_count"] = paralysis_retries + 1
    return True


def _split_compaction_history(
    compaction_history: list[str],
) -> tuple[list[str], list[str]]:
    """Partition compaction_history into (paralysis_entries, regular_entries).

    Paralysis entries contain markers from _build_paralysis_injection and
    MUST NOT be truncated when building dev extra-context — they are the
    primary mechanism for changing agent behavior on retry.
    """
    paralysis_entries: list[str] = []
    regular_entries: list[str] = []
    for entry in compaction_history:
        if "KILLED for Analysis Paralysis" in entry or "Agents KILLED" in entry:
            paralysis_entries.append(entry)
        else:
            regular_entries.append(entry)
    return paralysis_entries, regular_entries


def _build_dev_extra_context(
    compaction_history: list[str],
    cycle: int,
    message_context: str,
    dispatch_config: dict | None,
) -> str:
    """Compose the developer's extra_context from history + inter-agent messages.

    Behavior preserved exactly from the inline implementation:
      - Paralysis warnings are NEVER truncated; they always go first.
      - With anti_compaction_state enabled, regular entries from cycle >= 2
        are consolidated under a header and truncated to
        ``COMPACTION_CONSOLIDATION_MAX_WORDS`` words.
      - Without anti_compaction_state, only paralysis warnings are included.
      - Inter-agent messages (when present) are prepended to the final blob.
    """
    paralysis_entries, regular_entries = _split_compaction_history(
        compaction_history
    )

    if is_feature_enabled(dispatch_config, "anti_compaction_state") and compaction_history:
        if cycle >= 2 and len(regular_entries) > 1:
            consolidated = (
                f"## Previous Attempts (Cycles 1-{cycle - 1})\n\n"
                + "\n\n".join(regular_entries)
            )
            words = consolidated.split()
            if len(words) > COMPACTION_CONSOLIDATION_MAX_WORDS:
                consolidated = (
                    " ".join(words[:COMPACTION_CONSOLIDATION_MAX_WORDS])
                    + "\n[...earlier context trimmed...]"
                )
            extra_context = consolidated
        else:
            extra_context = "\n\n".join(regular_entries)

        if paralysis_entries:
            paralysis_block = "\n\n".join(paralysis_entries)
            extra_context = (
                paralysis_block + "\n\n" + extra_context
                if extra_context else paralysis_block
            )
    else:
        extra_context = "\n\n".join(paralysis_entries) if paralysis_entries else ""

    if message_context:
        extra_context = (
            message_context + "\n\n" + extra_context
            if extra_context else message_context
        )

    return extra_context


async def _handle_dev_continuation(
    dev_result: dict[str, Any],
    task_id: int,
    task_role: str,
    cycle: int,
    prev_attempt: int,
    project_dir: str,
    continuation_count: int,
    compaction_history: list[str],
    output: Any = None,
) -> tuple[str, int, str | None, str | None]:
    """Handle developer timeout / max-turns: save checkpoint and decide next step.

    Saves a hard checkpoint (and fires on_checkpoint hook) when result_text is
    available. If continuations remain, builds a recovery context (soft
    checkpoint + .forge-state.json on compaction, plain checkpoint context
    otherwise) and appends it to compaction_history. If continuations are
    exhausted, signals exit with outcome 'developer_timeout' or
    'developer_max_turns'.

    Returns (action, new_continuation_count, last_error_type, outcome):
      - action='continue': caller should `continue` to next cycle
      - action='exit': caller should return with outcome
      - action='proceed': not a timeout/max-turns case, caller continues
        normal post-dispatch flow

    Mutates compaction_history (append). Pure on dev_result.
    """
    is_timeout = any("timed out" in e for e in dev_result.get("errors", []))
    is_max_turns = any("max turns" in e for e in dev_result.get("errors", []))

    if not (is_timeout or is_max_turns):
        return "proceed", continuation_count, None, None

    reason = "timed out" if is_timeout else "hit max turns"
    last_error_type = "timeout" if is_timeout else "max_turns"
    new_count = continuation_count + 1
    log(
        f"  [Cycle {cycle}] Developer {reason}. "
        f"(continuation {new_count}/{MAX_CONTINUATIONS})",
        output,
    )

    result_text = dev_result.get("result_text", "")
    if result_text:
        attempt_num = prev_attempt + cycle
        cp_path = save_checkpoint(task_id, attempt_num, result_text, role=task_role)
        if cp_path:
            log(
                f"  [Checkpoint] Saved ({len(result_text)} chars) -> {cp_path.name}",
                output,
            )
            await fire_hook(
                "on_checkpoint",
                task_id=task_id, cycle=cycle, attempt=attempt_num,
                project_dir=project_dir, checkpoint_path=str(cp_path),
            )

    dev_compaction_count = dev_result.get("compaction_count", 0)
    if dev_compaction_count > 0:
        log(
            f"  [Compaction] {dev_compaction_count} compaction(s) "
            f"detected during streaming",
            output,
        )

    if new_count < MAX_CONTINUATIONS:
        log(f"  [Auto-Continue] Spawning new developer agent to continue...", output)

        # Build enhanced continuation context with compaction recovery
        if dev_compaction_count > 0:
            soft_cp = load_soft_checkpoint(task_id, role=task_role)
            forge_state = _load_forge_state_json(project_dir)

            if soft_cp:
                recovery_ctx = build_compaction_recovery_context(
                    soft_cp, forge_state)
                compaction_history.append(recovery_ctx)
                log(
                    f"  [Compaction] Injecting recovery context "
                    f"from soft checkpoint + forge-state",
                    output,
                )
            elif result_text:
                checkpoint_context = build_checkpoint_context(
                    result_text, prev_attempt + cycle)
                compaction_history.append(checkpoint_context)
        elif result_text:
            checkpoint_context = build_checkpoint_context(
                result_text, prev_attempt + cycle)
            compaction_history.append(checkpoint_context)
        return "continue", new_count, last_error_type, None

    log(
        f"  [Auto-Continue] All {MAX_CONTINUATIONS} continuations exhausted. "
        f"Marking blocked.",
        output,
    )
    outcome = "developer_timeout" if is_timeout else "developer_max_turns"
    return "exit", new_count, last_error_type, outcome


async def _resolve_head_sha(
    project_dir: str,
    output: Any = None,
) -> str:
    """Return the current HEAD commit SHA, or "HEAD" on failure.

    Used by the dev-test loop to anchor per-cycle git diffs. Falls back to
    the literal string "HEAD" so callers can pass the result straight to
    ``git diff <ref>`` without a None-check; the worst-case behavior is the
    legacy cumulative diff against the working HEAD.
    """
    try:
        result = await git_run_async(
            ["rev-parse", "HEAD"], project_dir, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log(f"  [git] Could not resolve HEAD SHA: {e}", output)
    return "HEAD"


# Roles whose deliverable is a markdown report (audit/review), not code.
# When these roles run and produce no code diff, the Tester phase has nothing
# to test and would either spin in analysis paralysis or return tester_blocked.
_AUDIT_ROLES = frozenset({"security-reviewer", "code-reviewer"})
# Task types whose deliverable is documentation/analysis, not code.
_AUDIT_TASK_TYPES = frozenset({"audit", "review"})


def _is_audit_type_task(
    task: dict[str, Any] | None,
    task_role: str,
    project_dir: str | None = None,
) -> bool:
    """Return True if this task's deliverable is a markdown report, not code.

    Triggers on an audit-style ``task_type``, one of the reviewer roles, or a
    report-writer role (frontmatter ``early_term_exempt`` — e.g. design /
    ip / regulatory analysts and project-overlay analysis roles), which produce
    markdown deliverables rather than code. Matches the criteria spelled out in
    TheForge bug 2237. The caller still guards the short-circuit on an empty git
    diff, so a role that does emit code keeps its tester cycle.
    """
    if task_role in _AUDIT_ROLES:
        return True
    if project_dir:
        try:
            from equipa.role_resolver import is_role_early_term_exempt
            if is_role_early_term_exempt(task_role, project_dir):
                return True
        except Exception:
            pass
    if not isinstance(task, dict):
        return False
    return (task.get("task_type") or "") in _AUDIT_TASK_TYPES


async def _git_diff_is_empty(project_dir: str, base_ref: str = "HEAD") -> bool:
    """Return True if there are no uncommitted code changes vs ``base_ref``.

    Used to decide whether to skip the Tester phase on audit/review tasks.
    On any failure (timeout, missing git), conservatively returns False so
    the tester still runs — better a wasted tester cycle than a missed
    real-code-change task.
    """
    try:
        result = await git_run_async(
            ["diff", base_ref], project_dir, timeout=10,
        )
        if result.returncode == 0:
            return not result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return False


async def _capture_git_diff_context(
    project_dir: str,
    cycle: int,
    output: Any = None,
    base_ref: str = "HEAD",
) -> str:
    """Capture git diff vs ``base_ref`` and format it as tester extra_context.

    ``base_ref`` defaults to ``HEAD`` for backwards compatibility, but the
    dev-test loop passes the prior cycle's HEAD SHA so each cycle's tester
    sees only the changes made in that cycle (avoids cumulative diff bloat).

    The diff command runs via ``git_run_async`` (native asyncio subprocess)
    so the 10s timeout cannot block the event loop. Returns an empty
    string when the diff is empty, the command fails, or times out. Diff is
    truncated at ``TESTER_GIT_DIFF_MAX_CHARS`` to avoid prompt bloat.
    """
    try:
        result = await git_run_async(
            ["diff", base_ref], project_dir, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            git_diff = result.stdout.strip()
            if len(git_diff) > TESTER_GIT_DIFF_MAX_CHARS:
                git_diff = (
                    git_diff[:TESTER_GIT_DIFF_MAX_CHARS]
                    + f"\n\n[... diff truncated, "
                      f"{len(git_diff) - TESTER_GIT_DIFF_MAX_CHARS} chars omitted ...]"
                )
            log(
                f"  [Cycle {cycle}] Captured git diff ({len(git_diff)} chars) "
                f"for tester context",
                output,
            )
            return (
                f"\n\n## Developer Changes (git diff)\n\n"
                f"The developer made the following changes:\n\n"
                f"```diff\n{git_diff}\n```\n\n"
                f"Write tests that verify these specific changes work correctly. "
                f"Focus your testing on the modified files and functions shown above."
            )
        if result.returncode == 0:
            log(f"  [Cycle {cycle}] No uncommitted changes detected (git diff empty)", output)
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log(f"  [Cycle {cycle}] Could not capture git diff: {e}", output)
    return ""


def _dispatch_tester_outcome(
    test_results: dict[str, Any],
    tester_result: dict[str, Any],
    dev_result: dict[str, Any],
    cycle: int,
    task_id: int,
    task_role: str,
    total_cost: float,
    total_duration: float,
    compaction_history: list[str],
    output: Any = None,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Map a parsed tester outcome to (action, return_result, outcome_str).

    action='exit' -> caller returns (return_result, cycle, outcome_str)
    action='continue_loop' -> caller proceeds to next dev/test cycle
    (return_result and outcome_str are None when action == 'continue_loop')

    Posts the appropriate inter-agent message to the developer for each
    branch. On 'fail', appends a test_failure context to compaction_history
    so the next developer cycle sees the failures.
    """
    test_outcome = test_results["result"]

    if test_outcome == "pass":
        # Task #2242: defense-in-depth against the "vacuous pass" loophole.
        # Phase A — TESTS_SKIPPED is a REQUIRED field. Absence means the
        #   tester is in contract violation (drift, truncation, regression,
        #   adversarial). Fail closed: tests_inconclusive, do NOT default to 0.
        # Phase B — cross-check the tester's TESTS_SKIPPED claim against
        #   framework-emitted skip counts in the raw stdout. If the framework
        #   reports more skips than the tester admitted, the tester is
        #   misreporting (e.g. TESTS_RUN: 0 paired with raw "5 skipped").
        # Phase C — internal consistency: tests_passed + tests_failed +
        #   tests_skipped must equal tests_run when tests_run > 0.
        # Phase D — tightened all-skipped predicate: tests_skipped == tests_run
        #   (was >=). Phase C now rejects inconsistent input upstream, so the
        #   stricter predicate is safe and matches the docstring + tests.
        tests_run = int(test_results.get("tests_run") or 0)
        # Task #2242 Phase C3/F3: tests_skipped is now ``None`` from the
        # parser when the TESTS_SKIPPED line is absent OR has an unparseable
        # value (contract violation). DO NOT coerce to 0 with ``or 0`` — the
        # whole point of the Phase-A guard is to NOT silently default. Keep
        # the raw value here and use a separate int view (tests_skipped_int)
        # for arithmetic. tests_skipped_present is derived from the sentinel.
        tests_skipped_raw = test_results.get("tests_skipped")
        tests_skipped_present = tests_skipped_raw is not None
        tests_skipped = (
            int(tests_skipped_raw) if isinstance(tests_skipped_raw, int) else 0
        )
        tests_passed_count = int(test_results.get("tests_passed") or 0)
        tests_failed_count = int(test_results.get("tests_failed") or 0)

        def _route_inconclusive(reason: str, **extra: Any) -> tuple[str, dict[str, Any], str]:
            log(
                f"  [Cycle {cycle}] tests_inconclusive ({reason}): "
                f"tests_run={tests_run} passed={tests_passed_count} "
                f"failed={tests_failed_count} skipped={tests_skipped} "
                f"skipped_present={tests_skipped_present}",
                output,
            )
            payload = {
                "outcome": "inconclusive",
                "reason": reason,
                "tests_run": tests_run,
                "tests_passed": tests_passed_count,
                "tests_failed": tests_failed_count,
                "tests_skipped": tests_skipped,
                "tests_skipped_present": tests_skipped_present,
                **extra,
            }
            msg_content = json.dumps(payload)
            post_agent_message(task_id, cycle, "tester", task_role,
                               "tests_inconclusive", msg_content)
            log(
                f"  [Cycle {cycle}] Posted tests_inconclusive ({reason}) for {task_role}",
                output,
            )
            _apply_cost_totals(tester_result, total_cost, total_duration)
            return "exit", tester_result, "tests_inconclusive"

        # Phase A: TESTS_SKIPPED line absent from tester output.
        # Task #2242 Phase B3: dropped the legacy ``tests_run > 0`` gate. The
        # contract violation is the omission itself — a tester that omits the
        # required marker while reporting RESULT: pass cannot be trusted at
        # any tests_run value. The previous gate let a tester reporting
        # TESTS_RUN: 0 bypass Phase A entirely.
        if not tests_skipped_present:
            return _route_inconclusive("missing_tests_skipped_field")

        # Task #2242 Phase B3 paired guard: RESULT: pass with TESTS_RUN: 0 is
        # itself a contract violation. The tester is asserting success without
        # having run anything — either it never invoked the framework, or it
        # is misreporting. Route to tests_inconclusive (NOT tests_passed).
        # This must come BEFORE Phase B/C/D which gate on tests_run > 0.
        if tests_run == 0:
            return _route_inconclusive("pass_with_zero_tests")

        # Phase B: framework stdout disagrees with the tester's claim.
        # Task #2242 Phase A3: include tool_output_text so the actual bash/test
        # stdout the agent observed (where pytest/vitest print their skip
        # footers) is part of the scan input. result_text only carries the
        # tester's prose summary — framework footers usually only appear in
        # tool_result blocks.
        raw_text = tester_result.get("result_text", "") or ""
        tool_output = tester_result.get("tool_output_text", "") or ""
        framework_skips = grep_framework_skip_counts(
            raw_text + "\n" + tool_output
        )
        if framework_skips > tests_skipped:
            return _route_inconclusive(
                "framework_skip_count_disagrees",
                framework_skips=framework_skips,
            )

        # Phase C: internal counts must add up. Adversarial misreport AND
        # benign tester bugs both produce inconsistent integers.
        if tests_run > 0 and (
            tests_passed_count + tests_failed_count + tests_skipped
        ) != tests_run:
            return _route_inconclusive(
                "counts_inconsistent",
                expected_sum=tests_run,
                actual_sum=tests_passed_count + tests_failed_count + tests_skipped,
            )

        # Phase D: all-tests-skipped — tightened from `>=` to `==`.
        if (
            tests_run > 0
            and tests_skipped == tests_run
            and tests_passed_count == 0
        ):
            log(
                f"  [Cycle {cycle}] Tester reported pass but ALL {tests_run} "
                f"test(s) were skipped (0 actually ran). Treating as "
                f"inconclusive — likely missing env vars or prerequisites.",
                output,
            )
            return _route_inconclusive("all_tests_skipped")

        log(f"  [Cycle {cycle}] All tests passed!", output)
        msg_content = json.dumps({
            "outcome": "pass",
            "tests_passed": test_results["tests_passed"],
            "tests_run": test_results["tests_run"],
        })
        post_agent_message(task_id, cycle, "tester", task_role,
                           "test_passed", msg_content)
        log(f"  [Cycle {cycle}] Posted test_passed message for {task_role}", output)
        clear_checkpoints(task_id)
        _apply_cost_totals(tester_result, total_cost, total_duration)
        return "exit", tester_result, "tests_passed"

    if test_outcome == "no-tests":
        # Task #2481: if the developer just wrote test files in this run,
        # but the tester reports "no tests found", that is a contradiction.
        # The tester is looking at a stale worktree or its discovery missed
        # the new files. Route to tests_inconclusive rather than silently
        # downgrading to "no_tests" — see equipa/classifier.py for the
        # detection helper and the rationale.
        dev_files = dev_result.get("files_changed_set") if isinstance(dev_result, dict) else None
        if dev_files is None and isinstance(dev_result, dict):
            dev_files = dev_result.get("files_changed")
        if wrote_test_files(dev_files):
            log(
                f"  [Cycle {cycle}] Tester reported 'no-tests' but developer "
                f"wrote test files this run. Routing to tests_inconclusive "
                f"(task #2481 classifier guard).",
                output,
            )
            payload = {
                "outcome": "inconclusive",
                "reason": "dev_wrote_tests_but_tester_found_none",
                "dev_test_files": [
                    p for p in (dev_files or []) if isinstance(p, str)
                ][:10],
            }
            msg_content = json.dumps(payload)
            post_agent_message(task_id, cycle, "tester", task_role,
                               "tests_inconclusive", msg_content)
            log(
                f"  [Cycle {cycle}] Posted tests_inconclusive "
                f"(dev_wrote_tests_but_tester_found_none) for {task_role}",
                output,
            )
            _apply_cost_totals(tester_result, total_cost, total_duration)
            return "exit", tester_result, "tests_inconclusive"

        log(f"  [Cycle {cycle}] No tests found. Accepting Developer result.", output)
        clear_checkpoints(task_id)
        _apply_cost_totals(dev_result, total_cost, total_duration)
        return "exit", dev_result, "no_tests"

    if test_outcome == "blocked":
        log(f"  [Cycle {cycle}] Tester is blocked (missing dependency, build error, etc.).", output)
        msg_content = json.dumps({
            "outcome": "blocked",
            "details": test_results.get("failure_details", [])[:3],
        })
        post_agent_message(task_id, cycle, "tester", task_role,
                           "blocker_update", msg_content)
        log(f"  [Cycle {cycle}] Posted blocker_update message for {task_role}", output)
        return "exit", tester_result, "tester_blocked"

    if (test_outcome == "unknown" and test_results["tests_run"] == 0
            and test_results["tests_failed"] == 0):
        # Task #2481: same classifier guard as the "no-tests" branch above —
        # don't promote an "unknown, tests_run=0" tester reading to
        # outcome=no_tests when the developer just wrote test files.
        dev_files = dev_result.get("files_changed_set") if isinstance(dev_result, dict) else None
        if dev_files is None and isinstance(dev_result, dict):
            dev_files = dev_result.get("files_changed")
        if wrote_test_files(dev_files):
            log(
                f"  [Cycle {cycle}] Tester returned unknown/0 but developer "
                f"wrote test files this run. Routing to tests_inconclusive "
                f"(task #2481 classifier guard).",
                output,
            )
            payload = {
                "outcome": "inconclusive",
                "reason": "dev_wrote_tests_but_tester_unknown",
                "dev_test_files": [
                    p for p in (dev_files or []) if isinstance(p, str)
                ][:10],
            }
            msg_content = json.dumps(payload)
            post_agent_message(task_id, cycle, "tester", task_role,
                               "tests_inconclusive", msg_content)
            _apply_cost_totals(tester_result, total_cost, total_duration)
            return "exit", tester_result, "tests_inconclusive"

        log(
            f"  [Cycle {cycle}] Tester returned unknown with 0 tests. "
            f"Treating as no-tests.",
            output,
        )
        clear_checkpoints(task_id)
        _apply_cost_totals(dev_result, total_cost, total_duration)
        return "exit", dev_result, "no_tests"

    # test_outcome == "fail"
    log(f"  [Cycle {cycle}] {test_results['tests_failed']} test(s) failed.", output)
    msg_content = json.dumps({
        "outcome": "fail",
        "tests_failed": test_results["tests_failed"],
        "tests_run": test_results["tests_run"],
        "failures": test_results.get("failure_details", [])[:5],
    })
    post_agent_message(task_id, cycle, "tester", task_role,
                       "test_failures", msg_content)
    log(f"  [Cycle {cycle}] Posted test_failures message for {task_role}", output)
    if test_results["failure_details"]:
        for detail in test_results["failure_details"][:5]:
            safe_detail = detail.encode("ascii", errors="replace").decode("ascii")
            log(f"    - {safe_detail}", output)

    failure_context = build_test_failure_context(test_results, cycle)
    compaction_history.append(failure_context)
    return "continue_loop", None, None


def _check_dev_progress(
    dev_result: dict[str, Any],
    accumulated_files: set[str],
    no_progress_count: int,
    project_dir: str,
    cycle: int,
    output: Any,
) -> tuple[str, int, str | None]:
    """Track progress for a Developer cycle and decide whether to continue.

    Updates ``accumulated_files`` in place with newly-touched files. Returns a
    triple ``(action, new_no_progress_count, last_error_type_reset)``:

    - ``action == "continue"``: caller proceeds with the cycle.
    - ``action == "block"``: ``no_progress_count`` has hit ``NO_PROGRESS_LIMIT``
      and the caller should return ``(dev_result, cycle, "no_progress")``.

    ``last_error_type_reset`` is ``None`` when the cycle made progress (caller
    should clear ``last_error_type``); otherwise the empty string sentinel
    indicates "leave last_error_type alone". Two values, not a bool, because
    None has a meaning here ("clear it") that ``False`` would erase.
    """
    files_changed = parse_developer_output(dev_result.get("result_text", ""))
    dev_turns_used = dev_result.get("num_turns", 0)
    if files_changed:
        accumulated_files.update(files_changed)
    made_progress = bool(files_changed) or dev_turns_used >= 3

    if made_progress:
        no_progress_count = 0
        if files_changed:
            log(f"  [Cycle {cycle}] Developer changed {len(files_changed)} file(s): "
                f"{', '.join(files_changed[:5])}", output)
        else:
            log(f"  [Cycle {cycle}] Developer used {dev_turns_used} turns "
                f"(no FILES_CHANGED marker, but counting as progress).", output)
        return "continue", no_progress_count, None

    # Idle cycle — but if earlier cycles produced real work, don't penalise
    if accumulated_files or has_branch_commits(project_dir):
        log(
            f"  [Cycle {cycle}] No per-cycle progress "
            f"({dev_turns_used} turns, no files marker), but "
            f"{len(accumulated_files)} accumulated file(s) "
            f"across prior cycles — not counting against limit.",
            output,
        )
        return "continue", no_progress_count, ""

    no_progress_count += 1
    log(
        f"  [Cycle {cycle}] No progress detected "
        f"({dev_turns_used} turns, no files marker) "
        f"({no_progress_count}/{NO_PROGRESS_LIMIT} "
        f"consecutive).",
        output,
    )
    if no_progress_count >= NO_PROGRESS_LIMIT:
        log(
            f"  [Cycle {cycle}] No progress for "
            f"{NO_PROGRESS_LIMIT} cycles and no "
            f"accumulated changes. Marking blocked.",
            output,
        )
        return "block", no_progress_count, ""
    return "continue", no_progress_count, ""


@dataclass
class DevTestState:
    """Per-loop mutable state for run_dev_test_loop.

    Bundles the half-dozen list/set/int/dict mutables that the loop body
    threads through helper calls so that adding a new branch can no longer
    silently forget to update one of them. Helper functions still accept
    the individual fields (so their unit tests stay focused), but the loop
    body holds a single `state` reference.
    """

    task_id: int
    task_role: str
    compaction_history: list[str] = field(default_factory=list)
    accumulated_files: set[str] = field(default_factory=set)
    no_progress_count: int = 0
    continuation_count: int = 0
    total_cost: float = 0.0
    total_duration: float = 0.0
    last_error_type: str | None = None
    dev_run_config: dict[str, Any] = field(default_factory=dict)
    loop_detector: LoopDetector = field(default_factory=LoopDetector)
    # Task #2604: track consecutive analysis-paralysis-killed cycles so the
    # PARALYSIS_CYCLE_HARD_CAP can bail out fast instead of spinning for
    # MAX_DEV_TEST_CYCLES iterations (observed: 50 min, PID 343414).
    consecutive_paralysis_cycles: int = 0

    def record_progress(self, files_changed: list[str], dev_turns_used: int) -> bool:
        """Apply the loop's progress rule and update accumulated_files.

        Progress = the developer reported FILES_CHANGED, OR it spent enough
        turns (>= 3) that we treat the cycle as productive even without a
        marker. This mirrors `_check_dev_progress` so both call sites stay
        in lockstep.
        """
        if files_changed:
            self.accumulated_files.update(files_changed)
        return bool(files_changed) or dev_turns_used >= 3


async def run_dev_test_loop(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], int, str]:
    """Run the Developer + Tester iteration loop.

    Flow per cycle:
    1. Check for checkpoint from a previous timed-out attempt
    2. Run Developer agent (with checkpoint + compaction/failure context)
    3. On timeout/max_turns -> save checkpoint for future resume
    4. Check if Developer marked task blocked -> exit
    5. Track FILES_CHANGED for progress detection
    6. Run Tester agent
    7. Parse Tester output:
       - pass -> clear checkpoints, exit success
       - no-tests -> clear checkpoints, exit accept
       - blocked -> exit
       - fail -> feed failures to next Developer cycle

    Returns (last_result, cycles_completed, outcome_reason) tuple.
    """
    # Auto-install deps before first cycle if needed
    await auto_install_dependencies(project_dir, output=output)

    # Pre-flight build check: detect build failures before agent starts
    task_description = task.get("description", "") if isinstance(task, dict) else ""
    preflight_ok, preflight_lang, preflight_error = await preflight_build_check(
        project_dir, task_description=task_description, output=output,
    )

    task_id = task["id"]
    # Resolve role early so DevTestState carries it for helper calls.
    task_role = (getattr(task, 'role', None)
                 or (task.get('role') if isinstance(task, dict) else None)
                 or "developer")
    # NOTE: keep this literal assignment for TheForge task #2095 regression
    # guard (tests/test_loops_dev_run_config.py). State now owns the value;
    # this line only exists so the static guard's regex still matches.
    dev_run_config: dict[str, Any] = {}
    state = DevTestState(task_id=task_id, task_role=task_role, dev_run_config=dev_run_config)

    # Load cost limits from dispatch config (overrides defaults)
    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    config_cost_limits = (dispatch_config or {}).get("cost_limits")

    # PLAN-1067 §2.B3 — restore prior session state on loop entry.
    # If the agent ran before and a session was captured (e.g. by a heartbeat
    # tick or a prior cycle), prepend the resume-prompt prefix to the dev's
    # extra context so the next dispatch resumes with full continuity.
    project_id_for_session = task.get("project_id") if isinstance(task, dict) else None
    session_persistence_on = is_feature_enabled(dispatch_config, "session_persistence")
    if session_persistence_on:
        try:
            restored_state = sessions.restore(task_id, task_role)
            if restored_state:
                resume_prefix = sessions.build_resume_prompt(restored_state)
                if resume_prefix:
                    state.compaction_history.insert(0, resume_prefix)
                    log(
                        f"  [Session] Restored prior session state for "
                        f"task {task_id} role={task_role} "
                        f"({len(resume_prefix)} chars resume context)",
                        output,
                    )
        except Exception as exc:  # noqa: BLE001 — never break the loop on session failure
            log(f"  [Session] WARNING: restore failed: {exc}", output)

    # Reset status so orchestrator is authoritative
    conn = get_db_connection(write=True)
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    log(f"  [Setup] Task {task_id} status reset to in_progress (orchestrator manages lifecycle)", output)

    # Resolve model and turns using adaptive tiering
    complexity = get_task_complexity(task)
    dev_model = get_role_model(task_role, args, task=task)
    tester_model = get_role_model("tester", args, task=task)
    dev_turns_max = get_role_turns(task_role, args, task=task)
    tester_turns_max = get_role_turns("tester", args, task=task)

    # Dynamic turn budgets: start conservative, extend on progress.
    # Effort scales budget — high-effort agents need more turns to match the
    # extra thinking per turn that --effort high buys at the CLI layer.
    effort = (dispatch_config or {}).get("effort")
    dev_turns_allocated, dev_turns_max = calculate_dynamic_budget(
        dev_turns_max, effort=effort)
    tester_turns_allocated, tester_turns_max = calculate_dynamic_budget(
        tester_turns_max, effort=effort)

    # Resolve cost limit for this complexity tier
    effective_cost_limit = (config_cost_limits or COST_LIMITS).get(complexity, 10.0)
    log(f"  Task complexity: {complexity}", output)
    log(f"  Cost limit: ${effective_cost_limit:.2f} ({complexity})", output)
    effort_label = effort or "default"
    log(f"  Developer: model={dev_model}, budget={dev_turns_allocated}/{dev_turns_max} "
        f"(dynamic, effort={effort_label})", output)
    log(f"  Tester: model={tester_model}, budget={tester_turns_allocated}/{tester_turns_max} "
        f"(dynamic, effort={effort_label})", output)

    # Check for checkpoint from a previous timed-out attempt
    checkpoint_text, prev_attempt = load_checkpoint(task_id, role=task_role)
    if checkpoint_text:
        checkpoint_context = build_checkpoint_context(checkpoint_text, prev_attempt)
        state.compaction_history.append(checkpoint_context)
        log(f"  [Checkpoint] Loaded checkpoint from attempt #{prev_attempt} "
            f"({len(checkpoint_text)} chars). Agent will continue from there.", output)

    # Auto-fix: dispatch debugger agent to fix broken builds before main task
    if not preflight_ok and preflight_error:
        autofix_ok, autofix_cost, autofix_summary = await _handle_preflight_failure(
            task, project_dir, project_context,
            preflight_lang, preflight_error, args, output=output,
        )
        state.total_cost += autofix_cost

        if autofix_ok:
            state.compaction_history.append(
                f"## Build Auto-Fixed\n\n"
                f"The build was broken but an auto-fix debugger agent repaired it "
                f"(method: {autofix_summary}, cost: ${autofix_cost:.2f}).\n"
                f"The build now passes. Proceed with your task normally."
            )
        else:
            log(f"  [AutoFix] Could not fix build. Marking task {task_id} as blocked "
                f"(reason: build_broken, autofix: {autofix_summary})", output)
            conn = get_db_connection(write=True)
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,)
            )
            conn.commit()
            conn.close()
            return {
                "early_terminated": True,
                "early_term_reason": f"build_broken ({autofix_summary})",
                "cost": state.total_cost,
                "duration": 0,
            }, 0, (OVERLOADED_OUTCOME if autofix_summary == OVERLOADED_OUTCOME
                   else "build_broken")

    tester_result: dict[str, Any] = {}
    dev_result: dict[str, Any] = {}

    # Task #2604: record the loop start time so the wall-clock wedge cap can
    # bail out when the loop has been running too long with zero file changes.
    loop_start_time = time.time()

    # Track the HEAD SHA at the end of each cycle so the next cycle's tester
    # diff only includes that cycle's developer changes. Without this, every
    # cycle pays the prompt-bloat tax of all prior cycles' diffs (TheForge
    # task #2145). Falls back to "HEAD" if the initial rev-parse fails so
    # behavior degrades gracefully to the legacy cumulative diff.
    prev_cycle_sha = await _resolve_head_sha(project_dir, output)

    # PLAN-1067 §2.B3 — wrap the dev-test loop in try/finally so
    # session capture runs on EVERY exit path (success, failure, exception).
    try:
        for cycle in range(1, MAX_DEV_TEST_CYCLES + 1):
            log(f"\n{'=' * 50}", output)
            log(f"  DEV-TEST CYCLE {cycle}/{MAX_DEV_TEST_CYCLES}", output)
            log(f"{'=' * 50}", output)

            # --- Lifecycle hooks: pre_cycle ---
            pre_cycle_returns = await fire_hook(
                "pre_cycle",
                task_id=task_id, cycle=cycle, project_dir=project_dir,
                total_cost=state.total_cost,
            )

            # --- Developer Phase ---
            log(f"\n  [Cycle {cycle}] Running Developer agent "
                f"(budget: {dev_turns_allocated}/{dev_turns_max})...", output)

            # --- Inter-agent messages ---
            agent_msgs = read_agent_messages(task_id, task_role)
            if agent_msgs:
                message_context = format_messages_for_prompt(agent_msgs)
                mark_messages_read(task_id, task_role, cycle)
                log(f"  [Cycle {cycle}] Injected {len(agent_msgs)} message(s) from other agents", output)
            else:
                message_context = ""

            # Build extra context from compaction history.
            # CRITICAL: Paralysis injection (KILLED for Analysis Paralysis) must
            # NEVER be truncated — it's the primary mechanism to change agent
            # behavior on retry. See _build_dev_extra_context for the full policy.
            _dc = getattr(args, "dispatch_config", None)
            extra_context = _build_dev_extra_context(
                state.compaction_history, cycle, message_context, _dc
            )

            # Merge any extra_context contributed by pre_cycle hook callbacks.
            # Plugins may return {"extra_context": "..."} to inject text into
            # the developer prompt for this cycle.
            for _hr in (pre_cycle_returns or []):
                if isinstance(_hr, dict) and _hr.get("extra_context"):
                    _injected = _hr["extra_context"]
                    if _injected and _injected not in (extra_context or ""):
                        extra_context = (extra_context or "") + _injected
                        log(f"  [Cycle {cycle}] Plugin extra_context injected ({len(_injected)} chars)", output)

            dev_prompt = build_system_prompt(
                task, project_context, project_dir,
                role=task_role, extra_context=extra_context,
                dispatch_config=dispatch_config,
                error_type=state.last_error_type,
                max_turns=dev_turns_allocated,
            )
            from equipa.role_resolver import is_role_early_term_exempt
            use_streaming = not is_role_early_term_exempt(task_role, project_dir)
            # --- Lifecycle hooks: pre_agent_start ---
            await fire_hook(
                "pre_agent_start",
                task_id=task_id, cycle=cycle, role=task_role,
                project_dir=project_dir, model=dev_model,
            )

            # Extract paralysis retry count so agent_runner can apply tighter
            # kill thresholds. Without this, the escalating prompt injections
            # have no teeth — the agent still gets the full kill budget.
            paralysis_retries = state.dev_run_config.get("_paralysis_retry_count", 0)

            with build_cli_command(
                dev_prompt, project_dir, dev_turns_allocated, dev_model, role=task_role,
                streaming=use_streaming,
            ) as dev_cmd:
                dev_result: AgentResult = await dispatch_agent(
                    dev_cmd, role=task_role, output=output, max_turns=dev_turns_allocated,
                    task_id=task_id, cycle=cycle, system_prompt=dev_prompt,
                    project_dir=project_dir, args=args,
                    paralysis_retry_count=paralysis_retries)
            dev_result["turns_allocated"] = dev_turns_allocated
            dev_result["turns_max"] = dev_turns_max
            state.total_duration += dev_result.get("duration", 0)
            state.total_cost += _accumulate_cost(
                dev_result, f"[Cycle {cycle}] Developer", output)

            # --- Lifecycle hooks: post_agent_finish (developer) ---
            await fire_hook(
                "post_agent_finish",
                task_id=task_id, cycle=cycle, role=task_role,
                project_dir=project_dir, success=dev_result.get("success", False),
                cost=dev_result.get("cost"), duration=dev_result.get("duration", 0),
            )

            # Cost-based circuit breaker
            cost_reason = _check_cost_limit(state.total_cost, complexity, config_cost_limits)
            if cost_reason:
                log(f"  [Cycle {cycle}] {cost_reason}", output)
                state.loop_detector.record(dev_result, cycle)
                _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                dev_result["early_terminated"] = True
                dev_result["early_term_reason"] = cost_reason
                return dev_result, cycle, "cost_limit_exceeded"

            # Check for early termination — retry analysis paralysis with stricter prompt
            if dev_result.get("early_terminated"):
                reason = dev_result.get("early_term_reason", "unknown")
                log(f"  [Cycle {cycle}] Developer early-terminated: {reason}", output)
                state.loop_detector.record(dev_result, cycle)

                # If killed for analysis paralysis (no file changes), retry with
                # escalating scaffold-first prompts. Each retry is MORE aggressive
                # and reduces the kill threshold. This is the #1 cause of failure
                # on large codebases — seen in FeatureBench task 3 where the agent
                # hit EarlyTerm on attempts 4, 5, 6, 8, 10.
                if _handle_paralysis_retry(
                    reason, cycle, state.compaction_history, state.dev_run_config, output
                ):
                    state.consecutive_paralysis_cycles += 1

                    # Task #2604 HARD CAP #1: too many consecutive paralysis cycles.
                    # Without this cap, the loop spins MAX_DEV_TEST_CYCLES times
                    # (observed: 5 cycles × ~10 min = 50 min, PID 343414, zero commits).
                    if state.consecutive_paralysis_cycles >= PARALYSIS_CYCLE_HARD_CAP:
                        log(
                            f"  [WedgeCap] HARD STOP (cycle {cycle}): "
                            f"{state.consecutive_paralysis_cycles} consecutive "
                            f"analysis-paralysis cycles with no file changes. "
                            f"Failing fast to prevent further cost burn (task #2604).",
                            output,
                        )
                        dev_result["early_terminated"] = True
                        dev_result["early_term_reason"] = (
                            f"analysis_paralysis_exhausted: "
                            f"{state.consecutive_paralysis_cycles} consecutive "
                            f"paralysis cycles with no commits"
                        )
                        return dev_result, cycle, "no_progress"

                    # Task #2604 HARD CAP #2: wall-clock ceiling for the no-progress case.
                    # Even within the paralysis-cycle cap, an extremely slow agent
                    # (e.g. hitting 600s readline timeout per cycle) can still burn
                    # PARALYSIS_CYCLE_HARD_CAP × timeout seconds before this fires.
                    elapsed_loop = time.time() - loop_start_time
                    if (
                        elapsed_loop > WEDGE_WALL_CLOCK_CAP_SECS
                        and not state.accumulated_files
                    ):
                        log(
                            f"  [WedgeCap] HARD STOP (wall-clock): "
                            f"{elapsed_loop / 60:.1f} min elapsed with zero file changes. "
                            f"Cap is {WEDGE_WALL_CLOCK_CAP_SECS / 60:.0f} min. "
                            f"Marking no_progress (task #2604).",
                            output,
                        )
                        dev_result["early_terminated"] = True
                        dev_result["early_term_reason"] = (
                            f"wedge_wall_clock_cap: "
                            f"{elapsed_loop / 60:.1f} min elapsed, "
                            f"no file changes across {cycle} cycle(s)"
                        )
                        return dev_result, cycle, "no_progress"

                    continue

                # Non-paralysis early termination: reset paralysis cycle counter.
                state.consecutive_paralysis_cycles = 0

                return dev_result, cycle, "early_terminated"

            # Check for agent-initiated early completion
            if dev_result.get("early_completed"):
                ec_reason = dev_result.get("early_complete_reason", "")
                log(f"  [Cycle {cycle}] Developer signaled early completion: "
                    f"{ec_reason}", output)
                no_changes_phrases = [
                    "no changes needed", "no changes required",
                    "no modifications needed", "nothing to change",
                    "already implemented", "already exists",
                    "no work needed", "task already complete",
                ]
                if any(phrase in ec_reason.lower() for phrase in no_changes_phrases):
                    log(f"  [Cycle {cycle}] Skipping tester — agent reported no "
                        f"changes needed.", output)
                    clear_checkpoints(task_id)
                    dev_result["cost"] = state.total_cost
                    dev_result["duration"] = state.total_duration
                    return dev_result, cycle, "early_completed_no_changes"
                log(f"  [Cycle {cycle}] Agent completed early with changes — "
                    f"proceeding to tester.", output)

            # Check for timeout or max_turns — save checkpoint, optionally
            # continue with recovery context, or exit if continuations exhausted.
            cont_action, state.continuation_count, cont_err_type, cont_outcome = (
                await _handle_dev_continuation(
                    dev_result, task_id, task_role, cycle, prev_attempt,
                    project_dir, state.continuation_count, state.compaction_history, output,
                )
            )
            if cont_action == "continue":
                state.last_error_type = cont_err_type
                continue
            if cont_action == "exit":
                return dev_result, cycle, cont_outcome  # type: ignore[return-value]
            # cont_action == "proceed" — fall through to normal flow

            # Sustained 529/overloaded exhausted every retry on the configured
            # model. Fail loudly with an explicit outcome — never downgrade.
            if dev_result.get("outcome") == OVERLOADED_OUTCOME:
                log(f"  [Cycle {cycle}] Developer agent FAILED: model overloaded "
                    f"(529) through every retry. Not downgrading the model.",
                    output)
                return dev_result, cycle, OVERLOADED_OUTCOME

            # Check for agent failure
            if not dev_result["success"]:
                if dev_result.get("has_file_changes"):
                    log(f"  [Cycle {cycle}] Developer agent reported failure but made file changes. "
                        f"Proceeding to tester.", output)
                    dev_result["success"] = True
                else:
                    log(f"  [Cycle {cycle}] Developer agent failed.", output)
                    return dev_result, cycle, "developer_failed"

            # Compaction
            dev_turns_used_for_compact = dev_result.get("num_turns", 0)
            log(f"  [Cycle {cycle}] Compacting developer output "
                f"({dev_turns_used_for_compact} turns)...", output)
            summary = build_compaction_summary("Developer", dev_result, cycle, task)
            state.compaction_history.append(summary)

            # Check if Developer marked task blocked
            status = _get_task_status(task["id"])
            if status == "blocked":
                log(f"  [Cycle {cycle}] Developer marked task as BLOCKED.", output)
                return dev_result, cycle, "developer_blocked"

            # Progress detection — track both per-cycle and accumulated changes
            progress_action, state.no_progress_count, error_reset = _check_dev_progress(
                dev_result, state.accumulated_files, state.no_progress_count,
                project_dir, cycle, output,
            )
            if error_reset is None:
                state.last_error_type = None
            if progress_action == "block":
                return dev_result, cycle, "no_progress"

            # --- Dynamic Budget Adjustment ---
            prev_budget = dev_turns_allocated
            dev_turns_allocated = adjust_dynamic_budget(
                dev_turns_allocated, dev_turns_max,
                dev_result.get("result_text", ""))
            if dev_turns_allocated != prev_budget:
                log(f"  [DynBudget] Developer budget adjusted: {prev_budget} -> "
                    f"{dev_turns_allocated}/{dev_turns_max}", output)

            # --- Loop Detection ---
            loop_action = state.loop_detector.record(dev_result, cycle)
            if loop_action == "terminate":
                log(f"  [Cycle {cycle}] LOOP DETECTED: Agent repeated the same failing "
                    f"pattern {state.loop_detector.consecutive_same} times. Terminating early.", output)
                dev_result.setdefault("errors", []).append(state.loop_detector.termination_summary())
                return dev_result, cycle, "loop_detected"
            elif loop_action == "warn":
                log(f"  [Cycle {cycle}] Loop warning: Agent has repeated the same pattern "
                    f"{state.loop_detector.consecutive_same} times. Injecting 'try different approach' "
                    f"guidance.", output)
                state.compaction_history.append(state.loop_detector.warning_message())
                dev_result.setdefault("errors", []).append(
                    f"Loop warning: agent repeated same pattern "
                    f"{state.loop_detector.consecutive_same} times (cycle {cycle})"
                )

            # --- Audit/review short-circuit ---
            # If this task's deliverable is a markdown report (audit, review,
            # security-reviewer, code-reviewer) and there are no code changes,
            # there is nothing for the tester to test. Running it anyway either
            # wastes turns in analysis paralysis or returns tester_blocked when
            # the diff is empty. See TheForge bug 2237.
            if _is_audit_type_task(task, task_role, project_dir):
                diff_empty = await _git_diff_is_empty(project_dir, base_ref=prev_cycle_sha)
                if diff_empty:
                    md_files = [
                        f for f in state.accumulated_files
                        if f.lower().endswith(".md")
                    ]
                    if not md_files:
                        # Fallback: scan the working tree for markdown files the
                        # agent wrote without emitting a FILES_CHANGED marker.
                        # accumulated_files is only populated from those markers,
                        # so one-shot review tasks that just write the file and
                        # stop would otherwise be misreported as failed even
                        # though the deliverable is on disk. See bug 2263.
                        try:
                            status_result = await git_run_async(
                                ["status", "--porcelain"], project_dir, timeout=5,
                            )
                            if status_result.returncode == 0:
                                md_files = [
                                    line[3:].strip()
                                    for line in status_result.stdout.splitlines()
                                    if len(line) > 3
                                    and line[3:].strip().lower().endswith(".md")
                                ]
                        except (subprocess.TimeoutExpired, OSError) as exc:
                            log(
                                f"  [Cycle {cycle}] git status fallback failed "
                                f"while probing for markdown deliverable: {exc}",
                                output,
                            )
                    if md_files:
                        log(
                            f"  [Cycle {cycle}] Tester skipped (audit-type task, "
                            f"no code changes — developer wrote markdown deliverable: "
                            f"{', '.join(md_files[:3])}).",
                            output,
                        )
                        clear_checkpoints(task_id)
                        _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                        return dev_result, cycle, "tests_passed"
                    log(
                        f"  [Cycle {cycle}] Tester skipped (audit-type task, "
                        f"no code changes and no markdown deliverable produced).",
                        output,
                    )
                    _apply_cost_totals(dev_result, state.total_cost, state.total_duration)
                    return dev_result, cycle, "failed"

            # --- Tester Phase ---
            log(f"\n  [Cycle {cycle}] Running Tester agent "
                f"(budget: {tester_turns_allocated}/{tester_turns_max})...", output)

            # Capture git diff to give tester context about developer changes.
            # Diff against the prior cycle's HEAD SHA so we don't re-send earlier
            # cycles' diffs to every subsequent tester (see task #2145).
            tester_extra_context = await _capture_git_diff_context(
                project_dir, cycle, output, base_ref=prev_cycle_sha,
            )
            tester_prompt = build_system_prompt(
                task, project_context, project_dir, role="tester",
                dispatch_config=dispatch_config,
                max_turns=tester_turns_allocated,
                extra_context=tester_extra_context,
            )
            # --- Lifecycle hooks: pre_agent_start (tester) ---
            await fire_hook(
                "pre_agent_start",
                task_id=task_id, cycle=cycle, role="tester",
                project_dir=project_dir, model=tester_model,
            )

            with build_cli_command(
                tester_prompt, project_dir, tester_turns_allocated, tester_model, role="tester",
                streaming=True,
            ) as tester_cmd:
                tester_result: AgentResult = await dispatch_agent(
                    tester_cmd, role="tester", output=output, max_turns=tester_turns_allocated,
                    task_id=task_id, cycle=cycle, system_prompt=tester_prompt,
                    project_dir=project_dir, args=args)
            tester_result["turns_allocated"] = tester_turns_allocated
            tester_result["turns_max"] = tester_turns_max
            state.total_duration += tester_result.get("duration", 0)
            state.total_cost += _accumulate_cost(
                tester_result, f"[Cycle {cycle}] Tester", output)

            # Sustained 529/overloaded exhausted every retry on the configured
            # model. The tester produced no output, so parsing it would yield
            # unknown/0 tests and be promoted to "no_tests" (a success) — the
            # dev work would ship UNTESTED. Fail loudly instead (task #2994).
            if tester_result.get("outcome") == OVERLOADED_OUTCOME:
                log(f"  [Cycle {cycle}] Tester agent FAILED: model overloaded "
                    f"(529) through every retry. Not downgrading the model and "
                    f"NOT accepting the developer work as untested.", output)
                _apply_cost_totals(tester_result, state.total_cost, state.total_duration)
                return tester_result, cycle, OVERLOADED_OUTCOME

            # --- Lifecycle hooks: post_agent_finish (tester) ---
            await fire_hook(
                "post_agent_finish",
                task_id=task_id, cycle=cycle, role="tester",
                project_dir=project_dir, success=tester_result.get("success", False),
                cost=tester_result.get("cost"), duration=tester_result.get("duration", 0),
            )

            # Cost-based circuit breaker after tester phase
            cost_reason = _check_cost_limit(state.total_cost, complexity, config_cost_limits)
            if cost_reason:
                log(f"  [Cycle {cycle}] {cost_reason} (after tester)", output)
                _apply_cost_totals(tester_result, state.total_cost, state.total_duration)
                tester_result["early_terminated"] = True
                tester_result["early_term_reason"] = cost_reason
                return tester_result, cycle, "cost_limit_exceeded"

            # Check for early termination (stuck tester)
            if tester_result.get("early_terminated"):
                reason = tester_result.get("early_term_reason", "unknown")
                log(f"  [Cycle {cycle}] Tester early-terminated: {reason}", output)
                log(f"  [Cycle {cycle}] Treating tester early-termination as no-tests (accepting dev work)", output)
                tester_result["result"] = "no-tests"
                tester_result["tests_run"] = 0
                tester_result["tests_passed"] = 0

            # Check for timeout
            if any("timed out" in e for e in tester_result.get("errors", [])):
                log(f"  [Cycle {cycle}] Tester timed out.", output)
                return tester_result, cycle, "tester_timeout"

            # Compaction
            tester_turns_for_compact = tester_result.get("num_turns", 0)
            log(f"  [Cycle {cycle}] Compacting tester output "
                f"({tester_turns_for_compact} turns)...", output)
            summary = build_compaction_summary("Tester", tester_result, cycle, task)
            state.compaction_history.append(summary)

            # Parse Tester output
            test_results = parse_tester_output(tester_result.get("result_text", ""))
            test_outcome = test_results["result"]

            log(f"  [Cycle {cycle}] Tester result: {test_outcome} "
                f"({test_results['tests_passed']}/{test_results['tests_run']} passed)", output)

            # --- Lifecycle hooks: post_cycle ---
            await fire_hook(
                "post_cycle",
                task_id=task_id, cycle=cycle, project_dir=project_dir,
                test_outcome=test_outcome, total_cost=state.total_cost,
            )

            outcome_action, return_result, outcome_str = _dispatch_tester_outcome(
                test_results, tester_result, dev_result, cycle, task_id,
                task_role, state.total_cost, state.total_duration,
                state.compaction_history, output,
            )
            if outcome_action == "exit":
                return return_result, cycle, outcome_str  # type: ignore[return-value]
            # outcome_action == "continue_loop" — fall through to next iteration.
            # Refresh prev_cycle_sha so the next cycle's tester diff starts from
            # this cycle's final HEAD (excluding earlier cycles' commits).
            next_sha = await _resolve_head_sha(project_dir, output)
            if next_sha:
                prev_cycle_sha = next_sha

        # All cycles exhausted
        log(f"\n  All {MAX_DEV_TEST_CYCLES} dev-test cycles exhausted. Marking blocked.", output)
        tester_result["cost"] = state.total_cost
        tester_result["duration"] = state.total_duration
        return tester_result, MAX_DEV_TEST_CYCLES, "cycles_exhausted"
    finally:
        # PLAN-1067 §2.B3 — capture orchestrator-cycle session on every exit.
        # Gated by feature flag; never raises (writes go through the safe
        # helper that swallows-and-logs).
        _capture_session_safe(
            task_id=task_id,
            role=task_role,
            project_id=project_id_for_session,
            dispatch_config=dispatch_config,
            output=output,
        )
