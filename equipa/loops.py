"""EQUIPA loops — dev-test loop, quality scoring, and security review.

Layer 7: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.roles, equipa.agent_runner, equipa.prompts, equipa.preflight,
         equipa.security, equipa.checkpoints, equipa.messages, equipa.tasks.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import re
from typing import Any

from equipa.agent_runner import (
    build_cli_command,
    dispatch_agent,
    run_agent,
)
from equipa.checkpoints import (
    build_compaction_recovery_context,
    clear_checkpoints,
    load_checkpoint,
    load_soft_checkpoint,
    save_checkpoint,
)
from equipa.hooks import fire_async as fire_hook
from equipa.constants import (
    COST_ESTIMATE_PER_TURN,
    COST_LIMITS,
    EARLY_TERM_EXEMPT_ROLES,
    MAX_CONTINUATIONS,
    MAX_DEV_TEST_CYCLES,
    NO_PROGRESS_LIMIT,
)
from equipa.db import (
    _get_latest_agent_run_id,
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
)
from equipa.output import log
from equipa.parsing import (
    build_compaction_summary,
    build_test_failure_context,
    parse_developer_output,
    parse_tester_output,
)
from equipa.preflight import (
    auto_install_dependencies,
    preflight_build_check,
    _handle_preflight_failure,
)
from equipa.prompts import build_checkpoint_context, build_system_prompt
from equipa.roles import (
    _accumulate_cost,
    _apply_cost_totals,
    get_role_model,
    get_role_turns,
)
from equipa.tasks import _get_task_status, get_task_complexity


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
    from equipa.dispatch import is_feature_enabled

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


async def run_security_review(
    task: dict[str, Any],
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> dict[str, Any]:
    """Run an automatic security review after dev-test succeeds.

    Uses the security-reviewer role with ClaudeStick tools.
    Only runs if security_review is enabled in dispatch config.
    """
    from equipa.dispatch import load_dispatch_config

    log(f"\n{'=' * 50}", output)
    log(f"  SECURITY REVIEW", output)
    log(f"{'=' * 50}", output)
    log(f"\n  Running security reviewer agent...", output)

    # Build security review prompt with explicit instructions to use all tools
    security_task = dict(task)  # copy
    security_task["description"] = (
        f"Security review of code written for: {task['title']}. "
        f"Review ALL files changed in the project directory. "
        f"YOU MUST use ALL ClaudeStick security tools: static-analysis, "
        f"audit-context-building, variant-analysis, differential-review, "
        f"fix-review, semgrep-rule-creator, and sharp-edges. "
        f"Check for OWASP Top 10 vulnerabilities, zero-day risks in dependencies, "
        f"and any security anti-patterns. "
        f"Write findings to a SECURITY-REVIEW.md file in the project directory. "
        f"Rate each finding: CRITICAL, HIGH, MEDIUM, LOW, INFO. "
        f"Original task description: {task['description']}"
    )

    sec_turns = get_role_turns("security-reviewer", args, task=task)
    sec_prompt = build_system_prompt(
        security_task, project_context, project_dir,
        role="security-reviewer",
        dispatch_config=getattr(args, "dispatch_config", None),
        max_turns=sec_turns,
    )
    sec_model = get_role_model("security-reviewer", args, task=task)
    sec_cmd = build_cli_command(
        sec_prompt, project_dir, sec_turns, sec_model, role="security-reviewer",
    )

    # Use security_review_timeout from dispatch config (default 15 min)
    dc = load_dispatch_config(None)
    sec_timeout = dc.get("security_review_timeout", 900)
    sec_result = await run_agent(sec_cmd, timeout=sec_timeout)

    if sec_result["success"]:
        log(f"  Security review completed in {sec_result.get('duration', 0):.1f}s", output)
        # Parse for critical findings
        result_text = sec_result.get("result_text", "")
        critical_count = result_text.lower().count("critical")
        high_count = result_text.lower().count("high")
        if critical_count > 0 or high_count > 0:
            log(f"  WARNING: Found {critical_count} CRITICAL and {high_count} HIGH severity findings", output)
        else:
            log(f"  No critical or high severity findings", output)

        # Feed security findings back into developer lessons
        project_id = task.get("project_id")
        findings = _extract_security_findings(result_text)
        if findings:
            count = _create_security_lessons(findings, project_id)
            if count > 0:
                log(f"  Created {count} developer lesson(s) from security findings", output)
    else:
        log(f"  Security review agent failed.", output)
        for err in sec_result.get("errors", []):
            log(f"    Error: {err[:200]}", output)

    return sec_result


def _extract_security_findings(result_text: str) -> list[tuple[str, str]]:
    """Extract individual CRITICAL and HIGH severity findings from security review output.

    Looks for lines containing severity markers and extracts the finding description.
    Returns a list of (severity, description) tuples.
    """
    findings: list[tuple[str, str]] = []
    if not result_text:
        return findings

    lines = result_text.split("\n")
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        line_upper = line_stripped.upper()

        # Match lines that contain severity ratings
        severity = None
        if "CRITICAL" in line_upper and any(
            p in line_upper for p in ("CRITICAL:", "CRITICAL**", "[CRITICAL]", "CRITICAL —", "CRITICAL -")
        ):
            severity = "CRITICAL"
        elif "HIGH" in line_upper and any(
            p in line_upper for p in ("HIGH:", "HIGH**", "[HIGH]", "HIGH —", "HIGH -")
        ):
            severity = "HIGH"

        if severity:
            desc = line_stripped
            for prefix in ("- ", "* ", "• "):
                if desc.startswith(prefix):
                    desc = desc[len(prefix):]

            if len(desc) < 40 and i + 1 < len(lines) and lines[i + 1].strip():
                desc = desc + " " + lines[i + 1].strip()

            if len(desc) > 500:
                desc = desc[:497] + "..."

            findings.append((severity, desc))

    return findings


def _create_security_lessons(findings: list[tuple[str, str]], project_id: int | None = None) -> int:
    """Insert security findings as developer lessons.

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

    conn = get_db_connection(write=True)
    created = 0

    for severity, description in findings:
        safe_description = sanitize_lesson_content(description)
        if not safe_description:
            continue

        sig = re.sub(r'[^\w\s]', '', safe_description.lower())[:200]

        existing = conn.execute(
            """SELECT id FROM lessons_learned
               WHERE error_signature = ? AND source = 'security-reviewer' AND active = 1""",
            (sig,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE lessons_learned
                   SET times_seen = times_seen + 1, updated_at = datetime('now')
                   WHERE id = ?""",
                (existing["id"],),
            )
        else:
            lesson_text = (
                f"Security review found {severity} issue: {safe_description}. "
                f"Check for this pattern in future code and prevent it proactively."
            )
            if not validate_lesson_structure(lesson_text):
                continue
            lesson_text = sanitize_lesson_content(lesson_text)
            conn.execute(
                """INSERT INTO lessons_learned
                   (project_id, role, error_type, error_signature, lesson, source, times_seen)
                   VALUES (?, 'developer', 'security', ?, ?, 'security-reviewer', 1)""",
                (project_id, sig, lesson_text),
            )
            created += 1

    conn.commit()
    conn.close()
    return created


def _load_forge_state_json(project_dir: str | None) -> dict | None:
    """Load .forge-state.json from the project directory if it exists.

    This file is maintained by agents during streaming to persist state
    across context compactions. Returns the parsed dict or None.
    """
    if not project_dir:
        return None
    from pathlib import Path
    state_file = Path(project_dir) / ".forge-state.json"
    if not state_file.exists():
        return None
    try:
        import json as _json
        return _json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
    from equipa.dispatch import is_feature_enabled

    # Auto-install deps before first cycle if needed
    await auto_install_dependencies(project_dir, output=output)

    # Pre-flight build check: detect build failures before agent starts
    task_description = task.get("description", "") if isinstance(task, dict) else ""
    preflight_ok, preflight_lang, preflight_error = await preflight_build_check(
        project_dir, task_description=task_description, output=output,
    )

    compaction_history: list[str] = []
    no_progress_count = 0
    continuation_count = 0
    total_cost = 0.0
    total_duration = 0.0
    task_id = task["id"]
    last_error_type: str | None = None
    loop_detector = LoopDetector()

    # Load cost limits from dispatch config (overrides defaults)
    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    config_cost_limits = (dispatch_config or {}).get("cost_limits")

    # Reset status so orchestrator is authoritative
    conn = get_db_connection(write=True)
    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    log(f"  [Setup] Task {task_id} status reset to in_progress (orchestrator manages lifecycle)", output)

    # Resolve model and turns using adaptive tiering
    complexity = get_task_complexity(task)
    task_role = (getattr(task, 'role', None)
                 or (task.get('role') if isinstance(task, dict) else None)
                 or "developer")
    dev_model = get_role_model(task_role, args, task=task)
    tester_model = get_role_model("tester", args, task=task)
    dev_turns_max = get_role_turns(task_role, args, task=task)
    tester_turns_max = get_role_turns("tester", args, task=task)

    # Dynamic turn budgets: start conservative, extend on progress
    dev_turns_allocated, _ = calculate_dynamic_budget(dev_turns_max)
    tester_turns_allocated, _ = calculate_dynamic_budget(tester_turns_max)

    # Resolve cost limit for this complexity tier
    effective_cost_limit = (config_cost_limits or COST_LIMITS).get(complexity, 10.0)
    log(f"  Task complexity: {complexity}", output)
    log(f"  Cost limit: ${effective_cost_limit:.2f} ({complexity})", output)
    log(f"  Developer: model={dev_model}, budget={dev_turns_allocated}/{dev_turns_max} "
        f"(dynamic)", output)
    log(f"  Tester: model={tester_model}, budget={tester_turns_allocated}/{tester_turns_max} "
        f"(dynamic)", output)

    # Check for checkpoint from a previous timed-out attempt
    checkpoint_text, prev_attempt = load_checkpoint(task_id, role=task_role)
    if checkpoint_text:
        checkpoint_context = build_checkpoint_context(checkpoint_text, prev_attempt)
        compaction_history.append(checkpoint_context)
        log(f"  [Checkpoint] Loaded checkpoint from attempt #{prev_attempt} "
            f"({len(checkpoint_text)} chars). Agent will continue from there.", output)

    # Auto-fix: dispatch debugger agent to fix broken builds before main task
    if not preflight_ok and preflight_error:
        autofix_ok, autofix_cost, autofix_summary = await _handle_preflight_failure(
            task, project_dir, project_context,
            preflight_lang, preflight_error, args, output=output,
        )
        total_cost += autofix_cost

        if autofix_ok:
            compaction_history.append(
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
                "cost": total_cost,
                "duration": 0,
            }, 0, "build_broken"

    tester_result: dict[str, Any] = {}
    dev_result: dict[str, Any] = {}

    for cycle in range(1, MAX_DEV_TEST_CYCLES + 1):
        log(f"\n{'=' * 50}", output)
        log(f"  DEV-TEST CYCLE {cycle}/{MAX_DEV_TEST_CYCLES}", output)
        log(f"{'=' * 50}", output)

        # --- Lifecycle hooks: pre_cycle ---
        await fire_hook(
            "pre_cycle",
            task_id=task_id, cycle=cycle, project_dir=project_dir,
            total_cost=total_cost,
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

        # Build extra context from compaction history
        _dc = getattr(args, "dispatch_config", None)
        if is_feature_enabled(_dc, "anti_compaction_state") and compaction_history:
            if cycle >= 2 and len(compaction_history) > 1:
                consolidated = (
                    f"## Previous Attempts (Cycles 1-{cycle - 1})\n\n"
                    + "\n\n".join(compaction_history)
                )
                words = consolidated.split()
                if len(words) > 400:
                    consolidated = " ".join(words[:400]) + "\n[...earlier context trimmed...]"
                extra_context = consolidated
            else:
                extra_context = "\n\n".join(compaction_history)
        else:
            extra_context = ""

        if message_context:
            extra_context = message_context + "\n\n" + extra_context if extra_context else message_context

        dev_prompt = build_system_prompt(
            task, project_context, project_dir,
            role=task_role, extra_context=extra_context,
            dispatch_config=dispatch_config,
            error_type=last_error_type,
            max_turns=dev_turns_allocated,
        )
        use_streaming = task_role not in EARLY_TERM_EXEMPT_ROLES
        dev_cmd = build_cli_command(
            dev_prompt, project_dir, dev_turns_allocated, dev_model, role=task_role,
            streaming=use_streaming,
        )

        # --- Lifecycle hooks: pre_agent_start ---
        await fire_hook(
            "pre_agent_start",
            task_id=task_id, cycle=cycle, role=task_role,
            project_dir=project_dir, model=dev_model,
        )

        dev_result = await dispatch_agent(
            dev_cmd, role=task_role, output=output, max_turns=dev_turns_allocated,
            task_id=task_id, cycle=cycle, system_prompt=dev_prompt,
            project_dir=project_dir, args=args)
        dev_result["turns_allocated"] = dev_turns_allocated
        dev_result["turns_max"] = dev_turns_max
        total_duration += dev_result.get("duration", 0)
        total_cost += _accumulate_cost(
            dev_result, f"[Cycle {cycle}] Developer", output)

        # --- Lifecycle hooks: post_agent_finish (developer) ---
        await fire_hook(
            "post_agent_finish",
            task_id=task_id, cycle=cycle, role=task_role,
            project_dir=project_dir, success=dev_result.get("success", False),
            cost=dev_result.get("cost"), duration=dev_result.get("duration", 0),
        )

        # Cost-based circuit breaker
        cost_reason = _check_cost_limit(total_cost, complexity, config_cost_limits)
        if cost_reason:
            log(f"  [Cycle {cycle}] {cost_reason}", output)
            loop_detector.record(dev_result, cycle)
            _apply_cost_totals(dev_result, total_cost, total_duration)
            dev_result["early_terminated"] = True
            dev_result["early_term_reason"] = cost_reason
            return dev_result, cycle, "cost_limit_exceeded"

        # Check for early termination
        if dev_result.get("early_terminated"):
            reason = dev_result.get("early_term_reason", "unknown")
            log(f"  [Cycle {cycle}] Developer early-terminated: {reason}", output)
            loop_detector.record(dev_result, cycle)
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
                dev_result["cost"] = total_cost
                dev_result["duration"] = total_duration
                return dev_result, cycle, "early_completed_no_changes"
            log(f"  [Cycle {cycle}] Agent completed early with changes — "
                f"proceeding to tester.", output)

        # Check for timeout or max_turns
        is_timeout = any("timed out" in e for e in dev_result.get("errors", []))
        is_max_turns = any("max turns" in e for e in dev_result.get("errors", []))

        if is_timeout or is_max_turns:
            reason = "timed out" if is_timeout else "hit max turns"
            last_error_type = "timeout" if is_timeout else "max_turns"
            continuation_count += 1
            log(f"  [Cycle {cycle}] Developer {reason}. "
                f"(continuation {continuation_count}/{MAX_CONTINUATIONS})", output)

            result_text = dev_result.get("result_text", "")
            if result_text:
                attempt_num = prev_attempt + cycle
                cp_path = save_checkpoint(task_id, attempt_num, result_text, role=task_role)
                if cp_path:
                    log(f"  [Checkpoint] Saved ({len(result_text)} chars) -> {cp_path.name}", output)
                    # --- Lifecycle hooks: on_checkpoint ---
                    await fire_hook(
                        "on_checkpoint",
                        task_id=task_id, cycle=cycle, attempt=attempt_num,
                        project_dir=project_dir, checkpoint_path=str(cp_path),
                    )

            # Check for compaction signals from streaming
            dev_compaction_count = dev_result.get("compaction_count", 0)
            if dev_compaction_count > 0:
                log(f"  [Compaction] {dev_compaction_count} compaction(s) "
                    f"detected during streaming", output)

            if continuation_count < MAX_CONTINUATIONS:
                log(f"  [Auto-Continue] Spawning new developer agent to continue...", output)

                # Build enhanced continuation context with compaction recovery
                if dev_compaction_count > 0:
                    # Load soft checkpoint + .forge-state.json for richer context
                    soft_cp = load_soft_checkpoint(task_id, role=task_role)
                    forge_state = _load_forge_state_json(project_dir)

                    if soft_cp:
                        recovery_ctx = build_compaction_recovery_context(
                            soft_cp, forge_state)
                        compaction_history.append(recovery_ctx)
                        log(f"  [Compaction] Injecting recovery context "
                            f"from soft checkpoint + forge-state", output)
                    elif result_text:
                        checkpoint_context = build_checkpoint_context(
                            result_text, prev_attempt + cycle)
                        compaction_history.append(checkpoint_context)
                elif result_text:
                    checkpoint_context = build_checkpoint_context(
                        result_text, prev_attempt + cycle)
                    compaction_history.append(checkpoint_context)
                continue

            log(f"  [Auto-Continue] All {MAX_CONTINUATIONS} continuations exhausted. "
                f"Marking blocked.", output)
            outcome = "developer_timeout" if is_timeout else "developer_max_turns"
            return dev_result, cycle, outcome

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
        compaction_history.append(summary)

        # Check if Developer marked task blocked
        status = _get_task_status(task["id"])
        if status == "blocked":
            log(f"  [Cycle {cycle}] Developer marked task as BLOCKED.", output)
            return dev_result, cycle, "developer_blocked"

        # Progress detection
        files_changed = parse_developer_output(dev_result.get("result_text", ""))
        dev_turns_used = dev_result.get("num_turns", 0)
        made_progress = bool(files_changed) or dev_turns_used >= 3

        if made_progress:
            last_error_type = None

        if not made_progress:
            no_progress_count += 1
            log(f"  [Cycle {cycle}] No progress detected ({dev_turns_used} turns, no files marker) "
                f"({no_progress_count}/{NO_PROGRESS_LIMIT} consecutive).", output)
            if no_progress_count >= NO_PROGRESS_LIMIT:
                log(f"  [Cycle {cycle}] No progress for {NO_PROGRESS_LIMIT} cycles. "
                    f"Marking blocked.", output)
                return dev_result, cycle, "no_progress"
        else:
            no_progress_count = 0
            if files_changed:
                log(f"  [Cycle {cycle}] Developer changed {len(files_changed)} file(s): "
                    f"{', '.join(files_changed[:5])}", output)
            else:
                log(f"  [Cycle {cycle}] Developer used {dev_turns_used} turns "
                    f"(no FILES_CHANGED marker, but counting as progress).", output)

        # --- Dynamic Budget Adjustment ---
        prev_budget = dev_turns_allocated
        dev_turns_allocated = adjust_dynamic_budget(
            dev_turns_allocated, dev_turns_max,
            dev_result.get("result_text", ""))
        if dev_turns_allocated != prev_budget:
            log(f"  [DynBudget] Developer budget adjusted: {prev_budget} -> "
                f"{dev_turns_allocated}/{dev_turns_max}", output)

        # --- Loop Detection ---
        loop_action = loop_detector.record(dev_result, cycle)
        if loop_action == "terminate":
            log(f"  [Cycle {cycle}] LOOP DETECTED: Agent repeated the same failing "
                f"pattern {loop_detector.consecutive_same} times. Terminating early.", output)
            dev_result.setdefault("errors", []).append(loop_detector.termination_summary())
            return dev_result, cycle, "loop_detected"
        elif loop_action == "warn":
            log(f"  [Cycle {cycle}] Loop warning: Agent has repeated the same pattern "
                f"{loop_detector.consecutive_same} times. Injecting 'try different approach' "
                f"guidance.", output)
            compaction_history.append(loop_detector.warning_message())
            dev_result.setdefault("errors", []).append(
                f"Loop warning: agent repeated same pattern "
                f"{loop_detector.consecutive_same} times (cycle {cycle})"
            )

        # --- Tester Phase ---
        log(f"\n  [Cycle {cycle}] Running Tester agent "
            f"(budget: {tester_turns_allocated}/{tester_turns_max})...", output)

        tester_prompt = build_system_prompt(
            task, project_context, project_dir, role="tester",
            dispatch_config=dispatch_config,
            max_turns=tester_turns_allocated,
        )
        tester_cmd = build_cli_command(
            tester_prompt, project_dir, tester_turns_allocated, tester_model, role="tester",
            streaming=True,
        )

        # --- Lifecycle hooks: pre_agent_start (tester) ---
        await fire_hook(
            "pre_agent_start",
            task_id=task_id, cycle=cycle, role="tester",
            project_dir=project_dir, model=tester_model,
        )

        tester_result = await dispatch_agent(
            tester_cmd, role="tester", output=output, max_turns=tester_turns_allocated,
            task_id=task_id, cycle=cycle, system_prompt=tester_prompt,
            project_dir=project_dir, args=args)
        tester_result["turns_allocated"] = tester_turns_allocated
        tester_result["turns_max"] = tester_turns_max
        total_duration += tester_result.get("duration", 0)
        total_cost += _accumulate_cost(
            tester_result, f"[Cycle {cycle}] Tester", output)

        # --- Lifecycle hooks: post_agent_finish (tester) ---
        await fire_hook(
            "post_agent_finish",
            task_id=task_id, cycle=cycle, role="tester",
            project_dir=project_dir, success=tester_result.get("success", False),
            cost=tester_result.get("cost"), duration=tester_result.get("duration", 0),
        )

        # Cost-based circuit breaker after tester phase
        cost_reason = _check_cost_limit(total_cost, complexity, config_cost_limits)
        if cost_reason:
            log(f"  [Cycle {cycle}] {cost_reason} (after tester)", output)
            _apply_cost_totals(tester_result, total_cost, total_duration)
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
        compaction_history.append(summary)

        # Parse Tester output
        test_results = parse_tester_output(tester_result.get("result_text", ""))
        test_outcome = test_results["result"]

        log(f"  [Cycle {cycle}] Tester result: {test_outcome} "
            f"({test_results['tests_passed']}/{test_results['tests_run']} passed)", output)

        # --- Lifecycle hooks: post_cycle ---
        await fire_hook(
            "post_cycle",
            task_id=task_id, cycle=cycle, project_dir=project_dir,
            test_outcome=test_outcome, total_cost=total_cost,
        )

        if test_outcome == "pass":
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
            return tester_result, cycle, "tests_passed"

        elif test_outcome == "no-tests":
            log(f"  [Cycle {cycle}] No tests found. Accepting Developer result.", output)
            clear_checkpoints(task_id)
            _apply_cost_totals(dev_result, total_cost, total_duration)
            return dev_result, cycle, "no_tests"

        elif test_outcome == "blocked":
            log(f"  [Cycle {cycle}] Tester is blocked (missing dependency, build error, etc.).", output)
            msg_content = json.dumps({
                "outcome": "blocked",
                "details": test_results.get("failure_details", [])[:3],
            })
            post_agent_message(task_id, cycle, "tester", task_role,
                               "blocker_update", msg_content)
            log(f"  [Cycle {cycle}] Posted blocker_update message for {task_role}", output)
            return tester_result, cycle, "tester_blocked"

        elif (test_outcome == "unknown" and test_results["tests_run"] == 0
              and test_results["tests_failed"] == 0):
            log(f"  [Cycle {cycle}] Tester returned unknown with 0 tests. "
                f"Treating as no-tests.", output)
            clear_checkpoints(task_id)
            _apply_cost_totals(dev_result, total_cost, total_duration)
            return dev_result, cycle, "no_tests"

        else:
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

    # All cycles exhausted
    log(f"\n  All {MAX_DEV_TEST_CYCLES} dev-test cycles exhausted. Marking blocked.", output)
    tester_result["cost"] = total_cost
    tester_result["duration"] = total_duration
    return tester_result, MAX_DEV_TEST_CYCLES, "cycles_exhausted"
