"""EQUIPA prompts — system prompt construction and context engineering.

Layer 5: Imports from equipa.constants, equipa.parsing, equipa.lessons, equipa.security.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import sys
from typing import Any

from equipa.constants import (
    BUDGET_CHECK_INTERVAL,
    PROMPTS_DIR,
    ROLE_PROMPTS,
)
from equipa.lessons import (
    _injected_episodes_by_task,
    format_episodes_for_injection,
    format_lessons_for_injection,
    get_relevant_episodes,
    update_episode_injection_count,
    update_lesson_injection_count,
)
from equipa.parsing import (
    CHARS_PER_TOKEN,
    EPISODE_REDUCTION_THRESHOLD,
    SYSTEM_PROMPT_TOKEN_HARD_LIMIT,
    SYSTEM_PROMPT_TOKEN_TARGET,
    _trim_prompt_section,
    build_compaction_summary,  # noqa: F401 — re-exported for convenience
    compact_agent_output,
    deduplicate_lessons,
    estimate_tokens,
)
from equipa.security import _make_untrusted_delimiter, wrap_untrusted


# Track which prompt version was used per role (for A/B testing telemetry).
# Set by build_system_prompt(), read by record_agent_run().
_last_prompt_version: dict[str, str] = {}


def build_task_prompt(
    task: dict[str, Any],
    project_context: dict[str, Any],
    project_dir: str,
    delimiter: str | None = None,
) -> str:
    """Build the task-specific instruction block.

    Args:
        delimiter: Unpredictable boundary token from _make_untrusted_delimiter().
            When provided, all database-sourced content is additionally wrapped
            in <<<DELIMITER>>> ... <<<END_DELIMITER>>> markers so agents can
            distinguish data from instructions even if <task-input> tags are
            spoofed by injected content.
    """
    # Task metadata (safe — controlled by orchestrator, not user input)
    lines = [
        "## Assigned Task",
        f"- **Task ID:** {task['id']}",
        f"- **Project:** {task.get('project_name', 'Unknown')} (project_id: {task.get('project_id', '?')})",
        f"- **Priority:** {task.get('priority', 'medium')}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Helper: wrap content in both task-input tags AND unpredictable delimiter
    def _wrap(tag_type: str, content: str) -> str:
        inner = wrap_untrusted(content, delimiter) if delimiter else content
        return f'<task-input type="{tag_type}" trust="database">\n{inner}\n</task-input>'

    # Task title and description — from database, could contain injection
    lines.append(_wrap("task-title", task["title"]))
    lines.append("")
    lines.append(_wrap("task-description", task.get("description", "No description provided")))
    lines.append("")

    # Project context also wrapped — comes from database
    session = project_context.get("last_session")
    if session:
        ctx_lines = [f"Last session ({session.get('session_date', 'unknown')}):"]
        ctx_lines.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            ctx_lines.append(f"Next steps: {session['next_steps']}")
        lines.append("## Recent Project Context")
        lines.append(_wrap("session-context", "\n".join(ctx_lines)))
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        q_lines = []
        for q in questions:
            q_lines.append(f"- {q['question']}")
            if q.get("context"):
                q_lines.append(f"  Context: {q['context']}")
        lines.append("## Open Questions (unresolved)")
        lines.append(_wrap("open-questions", "\n".join(q_lines)))
        lines.append("")

    decisions = project_context.get("recent_decisions", [])
    if decisions:
        d_lines = []
        for d in decisions:
            d_lines.append(f"- {d['decision']} ({d.get('decided_at', 'unknown')})")
            if d.get("rationale"):
                d_lines.append(f"  Rationale: {d['rationale']}")
        lines.append("## Recent Decisions")
        lines.append(_wrap("decisions", "\n".join(d_lines)))
        lines.append("")

    return "\n".join(lines)


def build_system_prompt(
    task: dict[str, Any],
    project_context: dict[str, Any],
    project_dir: str,
    role: str = "developer",
    extra_context: str = "",
    dispatch_config: dict | None = None,
    error_type: str | None = None,
    max_turns: int | None = None,
) -> str:
    """Read _common.md + role prompt, replace placeholders, append task prompt.

    Applies context engineering principles:
    - Token budget management (8K target, trimming in priority order)
    - Lesson deduplication (60%+ word overlap removed, max 5)
    - Episode relevance scoring (keyword overlap + recency weighting)
    - Token count logging per dispatch
    - A/B prompt version selection (GEPA-evolved prompts)
    - Budget visibility (max_turns info injected into prompt)

    extra_context: optional string appended after the task prompt (used for
    compaction history and test failure feedback in dev-test loop).
    dispatch_config: optional config dict for task-type-specific prompt injection.
    error_type: optional error type to filter relevant lessons (e.g. 'timeout', 'max_turns').
    max_turns: optional int — the turn budget allocated for this agent run.
        When provided, a budget visibility line is injected into the prompt so
        the agent can make rational decisions about depth vs breadth.

    Returns the system prompt string. Also sets _last_prompt_version[role] for
    telemetry tracking.
    """
    # Late imports to avoid circular dependencies with monolith during transition
    from equipa.git_ops import detect_project_language

    from equipa.dispatch import is_feature_enabled  # noqa: F811

    try:
        from forgesmith import get_relevant_lessons
    except ImportError:
        def get_relevant_lessons(role=None, error_type=None, limit=5):
            return []

    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS.get(role)

    if not role_path:
        print(f"ERROR: Unknown role '{role}'. Available: {', '.join(ROLE_PROMPTS.keys())}")
        sys.exit(1)

    if not common_path.exists():
        print(f"ERROR: Common prompt not found at {common_path}")
        sys.exit(1)

    if not role_path.exists():
        print(f"ERROR: Role prompt not found at {role_path}")
        sys.exit(1)

    # A/B prompt version selection: try GEPA-evolved prompt if available
    # Gated by gepa_ab_testing feature flag
    prompt_version = "baseline"
    if is_feature_enabled(dispatch_config, "gepa_ab_testing"):
        try:
            from forgesmith_gepa import get_ab_prompt_for_role
            selected_path, prompt_version = get_ab_prompt_for_role(role)
            if selected_path.exists() and prompt_version != "baseline":
                role_path = selected_path
        except ImportError:
            pass  # forgesmith_gepa not available, use baseline

    # Track which version was used for telemetry
    _last_prompt_version[role] = prompt_version

    # Generate a per-prompt unpredictable delimiter for untrusted content
    # isolation.  This prevents injected content from spoofing or closing
    # its own boundary markers (addresses EQ-24, EQ-10, EQ-25).
    _untrusted_delimiter = _make_untrusted_delimiter()

    # Build prompt: common rules + role-specific prompt (never trimmed)
    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    # Replace placeholders
    prompt = template.replace("{task_id}", str(task["id"]))
    prompt = prompt.replace("{project_id}", str(task.get("project_id", "")))

    # --- Lesson injection with deduplication ---
    # Gated by forgesmith_lessons feature flag
    if is_feature_enabled(dispatch_config, "forgesmith_lessons"):
        # Fetch more candidates than we'll inject, then deduplicate
        lessons = get_relevant_lessons(role=role, error_type=error_type, limit=10)
        if lessons:
            # Deduplicate: remove 60%+ word overlap, cap at 5
            lessons = deduplicate_lessons(lessons)
            lessons_text = format_lessons_for_injection(lessons, delimiter=_untrusted_delimiter)
            prompt = prompt + "\n\n" + lessons_text
            # Update times_injected counter for each lesson
            update_lesson_injection_count([l["id"] for l in lessons])

    # --- Episode injection with relevance scoring ---
    # Gated by forgesmith_episodes feature flag
    task_id = task.get("id") if isinstance(task, dict) else task
    project_id = task.get("project_id") if isinstance(task, dict) else None
    task_type = task.get("task_type", "feature") if isinstance(task, dict) else None
    task_description = task.get("description", "") if isinstance(task, dict) else ""

    if is_feature_enabled(dispatch_config, "forgesmith_episodes"):
        # Check token budget to decide episode limit
        current_tokens = estimate_tokens(prompt)
        episode_limit = 3
        if current_tokens > EPISODE_REDUCTION_THRESHOLD:
            episode_limit = 2  # Reduce episodes when prompt is already large

        if project_id:
            episodes = get_relevant_episodes(
                role=role, project_id=project_id, task_type=task_type,
                min_q_value=0.3, limit=episode_limit,
                task_description=task_description,
            )
            if episodes:
                episodes_text = format_episodes_for_injection(episodes, delimiter=_untrusted_delimiter)
                prompt = prompt + "\n\n" + episodes_text
                # Track injected episode IDs for q-value updates after task completion
                ep_ids = [ep["id"] for ep in episodes]
                _injected_episodes_by_task[task_id] = ep_ids
                update_episode_injection_count(ep_ids)

    # Inject task-type-specific guidance if available
    task_type_supplement = ""
    if dispatch_config and "task_type_prompts" in dispatch_config:
        task_type = task.get("task_type", "feature") or "feature"
        task_type_prompts = dispatch_config["task_type_prompts"]
        if task_type in task_type_prompts:
            task_type_supplement = (
                f"\n\n## Task Type Guidance ({task_type})\n\n"
                f"{task_type_prompts[task_type]}\n"
            )

    # Append task-specific instructions (never trimmed)
    task_prompt = build_task_prompt(task, project_context, project_dir, delimiter=_untrusted_delimiter)
    prompt = prompt + "\n\n---\n\n" + task_prompt

    # Append task-type supplement after task prompt
    if task_type_supplement:
        prompt = prompt + task_type_supplement

    # --- Language-specific prompt injection ---
    # Detect project language and load corresponding guidance if available
    # Gated by language_prompts feature flag
    if project_dir and is_feature_enabled(dispatch_config, "language_prompts"):
        lang_info = detect_project_language(project_dir)
        lang_prompts_dir = PROMPTS_DIR / "languages"
        injected_langs: set[str] = set()
        for lang_key in lang_info.get("languages", []):
            lang_prompt_path = lang_prompts_dir / f"{lang_key}.md"
            if lang_prompt_path.exists() and lang_key not in injected_langs:
                try:
                    lang_text = lang_prompt_path.read_text(encoding="utf-8")
                    frameworks_note = ""
                    if lang_info.get("frameworks"):
                        detected = [f for f in lang_info["frameworks"]
                                    if f not in ("dotnet", "maven", "gradle")]
                        if detected:
                            frameworks_note = (
                                f"\n\nDetected frameworks: {', '.join(detected)}. "
                                f"Apply framework-specific patterns where relevant."
                            )
                    prompt = prompt + "\n\n" + lang_text + frameworks_note
                    injected_langs.add(lang_key)
                except OSError:
                    pass  # File read failed, skip silently

    # Budget visibility: tell the agent how many turns it has
    if max_turns and max_turns > 0:
        prompt = prompt + (
            f"\n\nYou have {max_turns} turns for this task. "
            f"The orchestrator will log budget updates every "
            f"{BUDGET_CHECK_INTERVAL} turns."
        )

    # Append extra context (compaction history, test failures) if provided
    if extra_context:
        prompt = prompt + "\n\n---\n\n" + extra_context

    # --- Token budget enforcement ---
    # Trim in priority order: old episodes first, then generic lessons
    # Never trim: role prompt, task description
    token_count = estimate_tokens(prompt)

    if token_count > SYSTEM_PROMPT_TOKEN_TARGET:
        # Priority 1: Trim old episodes (## Past Experience)
        if token_count > SYSTEM_PROMPT_TOKEN_TARGET:
            prompt = _trim_prompt_section(prompt, "## Past Experience",
                                          max_chars=CHARS_PER_TOKEN * 500)
            token_count = estimate_tokens(prompt)

        # Priority 2: Trim generic lessons (## Lessons from Previous Runs)
        if token_count > SYSTEM_PROMPT_TOKEN_HARD_LIMIT:
            prompt = _trim_prompt_section(prompt, "## Lessons from Previous Runs",
                                          max_chars=CHARS_PER_TOKEN * 300)
            token_count = estimate_tokens(prompt)

        # Priority 3: Trim extra context (## Prior Work Summary, etc.)
        if token_count > SYSTEM_PROMPT_TOKEN_HARD_LIMIT:
            prompt = _trim_prompt_section(prompt, "## Prior Work Summary",
                                          max_chars=CHARS_PER_TOKEN * 400)
            token_count = estimate_tokens(prompt)

    # Log token count for monitoring
    final_tokens = estimate_tokens(prompt)
    budget_status = "OK" if final_tokens <= SYSTEM_PROMPT_TOKEN_TARGET else "OVER"
    print(f"  [ContextEng] System prompt: {len(prompt)} chars, ~{final_tokens} tokens "
          f"({budget_status}, target: {SYSTEM_PROMPT_TOKEN_TARGET})")

    return prompt


def build_checkpoint_context(checkpoint_text: str, attempt: int) -> str:
    """Build context string from a checkpoint for the next agent attempt.

    Uses compact_agent_output() to extract structured data (RESULT, FILES_CHANGED,
    BLOCKERS, SUMMARY) instead of passing raw text, preventing context rot.
    """
    # Compact checkpoint to structured summary (max 200 words)
    compacted = compact_agent_output(checkpoint_text, max_words=200)

    return (
        f"## Previous Attempt (#{attempt}) — Continue From Here\n\n"
        f"**The previous agent ran out of turns. Start writing code IMMEDIATELY — "
        f"do not repeat the same research.**\n\n"
        f"**The previous agent FAILED because it spent all its time reading instead "
        f"of writing code. DO NOT make the same mistake. You are the replacement.**\n\n"
        f"Start writing code IMMEDIATELY. Your FIRST tool call must be Edit or Write — "
        f"not Read, not Glob, not Grep. You have the previous agent's summary below. "
        f"Use it to skip exploration entirely and go straight to implementation.\n\n"
        f"### Previous Agent Summary:\n"
        f"<task-input type=\"checkpoint\" trust=\"agent-output\">\n{compacted}\n</task-input>\n\n"
        f"**CRITICAL:** Do NOT repeat the previous agent's exploration. Do NOT re-read "
        f"files they already read. Do NOT analyze the codebase from scratch. Look at "
        f"what remains to be done and START CODING in your FIRST turn.\n\n"
        f"**You are a SENIOR engineer. Make decisions. Write code. Ship it.** "
        f"The orchestrator is watching — another failure means this task gets "
        f"permanently blocked and escalated to a human. Do not be the agent that "
        f"causes escalation."
    )


def build_planner_prompt(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
) -> str:
    """Build the system prompt for the Planner agent.

    The Planner gets the goal, project context, and codebase access.
    It does NOT get a task — it creates tasks.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["planner"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    # Replace project_id placeholder (planner doesn't have a task_id)
    prompt = template.replace("{project_id}", str(project_id))
    prompt = prompt.replace("{task_id}", "N/A")

    # Per-prompt unpredictable delimiter for untrusted content isolation
    _delim = _make_untrusted_delimiter()

    # Helper: wrap content in both task-input tags AND unpredictable delimiter
    def _wrap(tag_type: str, content: str) -> str:
        inner = wrap_untrusted(content, _delim)
        return f'<task-input type="{tag_type}" trust="user">\n{inner}\n</task-input>'

    # Append goal and project context
    lines = [
        "## Goal",
        "",
        _wrap("goal", goal),
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Add project context
    session = project_context.get("last_session")
    if session:
        ctx_parts = [f"Last session ({session.get('session_date', 'unknown')}):"]
        ctx_parts.append(session.get("summary", "No summary"))
        if session.get("next_steps"):
            ctx_parts.append(f"Next steps: {session['next_steps']}")
        lines.append("## Recent Project Context")
        lines.append(_wrap("session-context", "\n".join(ctx_parts)))
        lines.append("")

    questions = project_context.get("open_questions", [])
    if questions:
        q_lines = [f"- {q['question']}" for q in questions]
        lines.append("## Open Questions (unresolved)")
        lines.append(_wrap("open-questions", "\n".join(q_lines)))
        lines.append("")

    prompt = prompt + "\n\n---\n\n" + "\n".join(lines)
    return prompt


def build_evaluator_prompt(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    completed_tasks: list[dict],
    blocked_tasks: list[dict],
) -> str:
    """Build the system prompt for the Evaluator agent.

    The Evaluator gets the original goal, completed/blocked task info,
    and codebase access to verify work.
    """
    common_path = PROMPTS_DIR / "_common.md"
    role_path = ROLE_PROMPTS["evaluator"]

    common_text = common_path.read_text(encoding="utf-8")
    role_text = role_path.read_text(encoding="utf-8")
    template = common_text + "\n\n---\n\n" + role_text

    prompt = template.replace("{project_id}", str(project_id))
    prompt = prompt.replace("{task_id}", "N/A")

    # Per-prompt unpredictable delimiter for untrusted content isolation
    _delim = _make_untrusted_delimiter()

    def _wrap(tag_type: str, content: str, trust: str = "user") -> str:
        inner = wrap_untrusted(content, _delim)
        return f'<task-input type="{tag_type}" trust="{trust}">\n{inner}\n</task-input>'

    lines = [
        "## Original Goal",
        "",
        _wrap("goal", goal),
        "",
        f"## Project Info",
        f"- **Project ID:** {project_id}",
        f"- **Working Directory:** {project_dir}",
        "",
    ]

    # Show completed tasks — task titles/descriptions come from DB
    if completed_tasks:
        ct_lines = []
        for t in completed_tasks:
            ct_lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("## Completed Tasks")
        lines.append(_wrap("completed-tasks", "\n".join(ct_lines), trust="database"))
        lines.append("")

    # Show blocked tasks — task titles/descriptions come from DB
    if blocked_tasks:
        bt_lines = []
        for t in blocked_tasks:
            bt_lines.append(f"- **#{t['id']}** {t['title']} — {t.get('description', 'No description')[:200]}")
        lines.append("## Blocked Tasks")
        lines.append(_wrap("blocked-tasks", "\n".join(bt_lines), trust="database"))
        lines.append("")

    if not completed_tasks and not blocked_tasks:
        lines.append("## Task Results")
        lines.append("No tasks were completed or blocked. This is unexpected.")
        lines.append("")

    prompt = prompt + "\n\n---\n\n" + "\n".join(lines)
    return prompt
