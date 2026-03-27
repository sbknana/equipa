"""EQUIPA package — modular extraction of forge_orchestrator.py.

Phase 1 leaf modules: constants, checkpoints, git_ops.
Phase 2 low-coupling modules: output, messages, parsing, monitoring.
Phase 3 database layer: db, tasks, lessons, roles.
Phase 4 core engine: security, prompts, reflexion, agent_runner, preflight, loops, manager.
Phase 5 entry points: dispatch, cli.
All public symbols are re-exported here for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

# --- Constants (equipa.constants) ---
from equipa.constants import (
    AUTOFIX_COST_LIMIT,
    AUTOFIX_DEBUGGER_BUDGET,
    AUTOFIX_MAX_DEBUGGER_CYCLES,
    AUTOFIX_PLANNER_BUDGET,
    BUDGET_CHECK_INTERVAL,
    BUDGET_CRITICAL_THRESHOLD,
    BUDGET_HALFWAY_THRESHOLD,
    CHECKPOINT_DIR,
    COMPLEXITY_MULTIPLIERS,
    COST_ESTIMATE_PER_TURN,
    COST_LIMITS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_ROLE_MODELS,
    DEFAULT_ROLE_TURNS,
    DEV_COMPACTION_THRESHOLD,
    DYNAMIC_BUDGET_BLOCKED_RATIO,
    DYNAMIC_BUDGET_EXTEND_TURNS,
    DYNAMIC_BUDGET_MIN_TURNS,
    DYNAMIC_BUDGET_START_RATIO,
    EARLY_TERM_EXEMPT_ROLES,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_STUCK_PHRASES,
    EARLY_TERM_WARN_TURNS,
    GITHUB_OWNER,
    GITIGNORE_TEMPLATES,
    MAX_CONTINUATIONS,
    MAX_DEV_TEST_CYCLES,
    MAX_FOLLOWUP_TASKS,
    MAX_MANAGER_ROUNDS,
    MAX_TASKS_PER_PLAN,
    MCP_CONFIG,
    MONOLOGUE_EXEMPT_TURNS,
    MONOLOGUE_THRESHOLD,
    NO_PROGRESS_LIMIT,
    PREFLIGHT_SKIP_KEYWORDS,
    PREFLIGHT_TIMEOUT,
    PRIORITY_ORDER,
    PROCESS_TIMEOUT,
    PROJECT_DIRS,
    PROMPTS_DIR,
    ROLE_PROMPTS,
    ROLE_SKILLS,
    SKILL_MANIFEST_FILE,
    SKILLS_BASE_DIR,
    TESTER_COMPACTION_THRESHOLD,
    THEFORGE_DB,
)

# --- Checkpoints (equipa.checkpoints) ---
from equipa.checkpoints import (
    SOFT_CHECKPOINT_INTERVAL,
    SOFT_CHECKPOINT_TEXT_LIMIT,
    build_compaction_recovery_context,
    clear_checkpoints,
    load_checkpoint,
    load_soft_checkpoint,
    save_checkpoint,
    save_soft_checkpoint,
)

# --- Git Operations (equipa.git_ops) ---
from equipa.git_ops import (
    _get_repo_env,
    _git_run,
    _is_git_repo,
    check_gh_installed,
    detect_project_language,
    setup_all_repos,
    setup_single_repo,
)

# --- Output (equipa.output) ---
from equipa.output import (
    _print_batch_summary,
    _print_task_summary,
    log,
    print_dev_test_summary,
    print_dispatch_plan,
    print_dispatch_summary,
    print_manager_summary,
    print_parallel_summary,
    print_summary,
)

# --- Messages (equipa.messages) ---
from equipa.messages import (
    format_messages_for_prompt,
    mark_messages_read,
    post_agent_message,
    read_agent_messages,
)

# --- Parsing (equipa.parsing) ---
from equipa.parsing import (
    CHARS_PER_TOKEN,
    EPISODE_REDUCTION_THRESHOLD,
    SYSTEM_PROMPT_TOKEN_HARD_LIMIT,
    SYSTEM_PROMPT_TOKEN_TARGET,
    _DEVELOPER_FILES_SCHEMA,
    _FAILURE_KEYWORD_PATTERNS,
    _FAILURE_PRIORITY,
    _TESTER_SCHEMA,
    _extract_marker_value,
    _extract_section,
    _parse_structured_output,
    _trim_prompt_section,
    build_compaction_summary,
    build_test_failure_context,
    classify_agent_failure,
    compact_agent_output,
    compute_initial_q_value,
    compute_keyword_overlap,
    deduplicate_lessons,
    estimate_tokens,
    parse_approach_summary,
    parse_developer_output,
    parse_error_patterns,
    parse_reflection,
    parse_tester_output,
    validate_output,
)

# --- Monitoring (equipa.monitoring) ---
from equipa.monitoring import (
    LOOP_TERMINATE_THRESHOLD,
    LOOP_WARNING_THRESHOLD,
    LoopDetector,
    _build_streaming_result,
    _build_tool_signature,
    _check_cost_limit,
    _check_git_changes,
    _check_monologue,
    _check_stuck_phrases,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
    _TOOL_SIG_KEY,
    adjust_dynamic_budget,
    calculate_dynamic_budget,
)

# --- Database (equipa.db) ---
from equipa.db import (
    _get_latest_agent_run_id,
    bulk_log_agent_actions,
    classify_error,
    ensure_schema,
    get_db_connection,
    log_agent_action,
    record_agent_run,
    update_task_status,
)

# --- Tasks (equipa.tasks) ---
from equipa.tasks import (
    _get_task_status,
    fetch_next_todo,
    fetch_project_context,
    fetch_project_info,
    fetch_task,
    fetch_tasks_by_ids,
    get_task_complexity,
    resolve_project_dir,
    verify_task_updated,
)

# --- Lessons (equipa.lessons) ---
from equipa.lessons import (
    _injected_episodes_by_task,
    format_episodes_for_injection,
    format_lessons_for_injection,
    get_relevant_episodes,
    record_agent_episode,
    update_episode_injection_count,
    update_episode_q_values,
    update_injected_episode_q_values_for_task,
    update_lesson_injection_count,
)

# --- Roles (equipa.roles) ---
from equipa.roles import (
    _accumulate_cost,
    _apply_cost_totals,
    _discover_roles,
    get_role_model,
    get_role_turns,
)

# --- Security (equipa.security) ---
from equipa.security import (
    _make_untrusted_delimiter,
    generate_skill_manifest,
    verify_skill_integrity,
    wrap_untrusted,
    write_skill_manifest,
)

# --- Prompts (equipa.prompts) ---
from equipa.prompts import (
    _last_prompt_version,
    build_checkpoint_context,
    build_evaluator_prompt,
    build_planner_prompt,
    build_system_prompt,
    build_task_prompt,
)

# --- Reflexion (equipa.reflexion) ---
from equipa.reflexion import (
    INITIAL_Q_VALUE,
    REFLEXION_PROMPT,
    maybe_run_reflexion,
    run_reflexion_agent,
)

# --- Agent Runner (equipa.agent_runner) ---
from equipa.agent_runner import (
    build_cli_command,
    dispatch_agent,
    run_agent,
    run_agent_streaming,
    run_agent_with_retries,
)

# --- Preflight (equipa.preflight) ---
from equipa.preflight import (
    _dispatch_autofix_agent,
    _handle_preflight_failure,
    _resolve_build_command,
    _run_install_cmd,
    auto_install_dependencies,
    preflight_build_check,
)

# --- Hooks (equipa.hooks) ---
from equipa.hooks import (
    LIFECYCLE_EVENTS,
    clear_external_hooks,
    clear_registry,
    fire,
    fire_async,
    get_external_hook_count,
    get_registered_count,
    load_hooks_config,
    register,
    run_external_hook,
    run_external_hook_async,
    unregister,
)

# --- Loops (equipa.loops) ---
from equipa.loops import (
    _create_security_lessons,
    _extract_security_findings,
    run_dev_test_loop,
    run_quality_scoring,
    run_security_review,
)

# --- Manager (equipa.manager) ---
from equipa.manager import (
    parse_evaluator_output,
    parse_planner_output,
    run_evaluator_agent,
    run_manager_loop,
    run_planner_agent,
)

# --- Dispatch (equipa.dispatch) ---
from equipa.dispatch import (
    DEFAULT_DISPATCH_CONFIG,
    DEFAULT_FEATURE_FLAGS,
    apply_dispatch_filters,
    is_feature_enabled,
    load_dispatch_config,
    load_goals_file,
    parse_task_ids,
    run_auto_dispatch,
    run_parallel_goals,
    run_parallel_tasks,
    run_project_dispatch,
    run_project_tasks,
    run_single_goal,
    scan_pending_work,
    score_project,
    validate_goals,
)

# --- MCP Health (equipa.mcp_health) ---
from equipa.mcp_health import (
    DEFAULT_BACKOFF,
    HEALTH_CACHE,
    HEALTHY_TTL,
    MAX_BACKOFF,
    MCPHealthMonitor,
)

# --- MCP Server (equipa.mcp_server) ---
from equipa.mcp_server import (
    run_server,
)

# --- CLI (equipa.cli) ---
from equipa.cli import (
    _handle_add_project,
    _post_task_telemetry,
    async_main,
    get_ollama_base_url,
    get_ollama_model,
    get_provider,
    load_config,
    main,
)

__all__ = [
    # Constants
    "AUTOFIX_COST_LIMIT",
    "AUTOFIX_DEBUGGER_BUDGET",
    "AUTOFIX_MAX_DEBUGGER_CYCLES",
    "AUTOFIX_PLANNER_BUDGET",
    "BUDGET_CHECK_INTERVAL",
    "BUDGET_CRITICAL_THRESHOLD",
    "BUDGET_HALFWAY_THRESHOLD",
    "CHECKPOINT_DIR",
    "COMPLEXITY_MULTIPLIERS",
    "COST_ESTIMATE_PER_TURN",
    "COST_LIMITS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "DEFAULT_ROLE_MODELS",
    "DEFAULT_ROLE_TURNS",
    "DEV_COMPACTION_THRESHOLD",
    "DYNAMIC_BUDGET_BLOCKED_RATIO",
    "DYNAMIC_BUDGET_EXTEND_TURNS",
    "DYNAMIC_BUDGET_MIN_TURNS",
    "DYNAMIC_BUDGET_START_RATIO",
    "EARLY_TERM_EXEMPT_ROLES",
    "EARLY_TERM_FINAL_WARN_TURNS",
    "EARLY_TERM_KILL_TURNS",
    "EARLY_TERM_STUCK_PHRASES",
    "EARLY_TERM_WARN_TURNS",
    "GITHUB_OWNER",
    "GITIGNORE_TEMPLATES",
    "MAX_CONTINUATIONS",
    "MAX_DEV_TEST_CYCLES",
    "MAX_FOLLOWUP_TASKS",
    "MAX_MANAGER_ROUNDS",
    "MAX_TASKS_PER_PLAN",
    "MCP_CONFIG",
    "MONOLOGUE_EXEMPT_TURNS",
    "MONOLOGUE_THRESHOLD",
    "NO_PROGRESS_LIMIT",
    "PREFLIGHT_SKIP_KEYWORDS",
    "PREFLIGHT_TIMEOUT",
    "PRIORITY_ORDER",
    "PROCESS_TIMEOUT",
    "PROJECT_DIRS",
    "PROMPTS_DIR",
    "ROLE_PROMPTS",
    "ROLE_SKILLS",
    "SKILL_MANIFEST_FILE",
    "SKILLS_BASE_DIR",
    "TESTER_COMPACTION_THRESHOLD",
    "THEFORGE_DB",
    # Checkpoints
    "clear_checkpoints",
    "load_checkpoint",
    "save_checkpoint",
    # Git Operations
    "_get_repo_env",
    "_git_run",
    "_is_git_repo",
    "check_gh_installed",
    "detect_project_language",
    "setup_all_repos",
    "setup_single_repo",
    # Output
    "log",
    "print_manager_summary",
    "_print_task_summary",
    "print_summary",
    "print_dev_test_summary",
    "_print_batch_summary",
    "print_parallel_summary",
    "print_dispatch_plan",
    "print_dispatch_summary",
    # Messages
    "post_agent_message",
    "read_agent_messages",
    "mark_messages_read",
    "format_messages_for_prompt",
    # Parsing
    "CHARS_PER_TOKEN",
    "SYSTEM_PROMPT_TOKEN_TARGET",
    "SYSTEM_PROMPT_TOKEN_HARD_LIMIT",
    "EPISODE_REDUCTION_THRESHOLD",
    "estimate_tokens",
    "compute_keyword_overlap",
    "deduplicate_lessons",
    "_extract_section",
    "compact_agent_output",
    "_trim_prompt_section",
    "_extract_marker_value",
    "parse_reflection",
    "parse_approach_summary",
    "_FAILURE_KEYWORD_PATTERNS",
    "_FAILURE_PRIORITY",
    "classify_agent_failure",
    "parse_error_patterns",
    "compute_initial_q_value",
    "_parse_structured_output",
    "_TESTER_SCHEMA",
    "_DEVELOPER_FILES_SCHEMA",
    "parse_tester_output",
    "parse_developer_output",
    "build_compaction_summary",
    "build_test_failure_context",
    "validate_output",
    # Monitoring
    "LOOP_WARNING_THRESHOLD",
    "LOOP_TERMINATE_THRESHOLD",
    "_check_stuck_phrases",
    "_check_monologue",
    "_get_budget_message",
    "_check_cost_limit",
    "_check_git_changes",
    "_parse_early_complete",
    "_compute_output_hash",
    "_TOOL_SIG_KEY",
    "_build_tool_signature",
    "_detect_tool_loop",
    "_build_streaming_result",
    "LoopDetector",
    "calculate_dynamic_budget",
    "adjust_dynamic_budget",
    # Database
    "get_db_connection",
    "ensure_schema",
    "record_agent_run",
    "_get_latest_agent_run_id",
    "update_task_status",
    "classify_error",
    "log_agent_action",
    "bulk_log_agent_actions",
    # Tasks
    "fetch_task",
    "fetch_next_todo",
    "fetch_project_context",
    "_get_task_status",
    "fetch_project_info",
    "fetch_tasks_by_ids",
    "get_task_complexity",
    "verify_task_updated",
    "resolve_project_dir",
    # Lessons
    "format_lessons_for_injection",
    "update_lesson_injection_count",
    "_injected_episodes_by_task",
    "get_relevant_episodes",
    "format_episodes_for_injection",
    "record_agent_episode",
    "update_episode_injection_count",
    "update_episode_q_values",
    "update_injected_episode_q_values_for_task",
    # Roles
    "get_role_turns",
    "get_role_model",
    "_discover_roles",
    "_accumulate_cost",
    "_apply_cost_totals",
    # Security
    "_make_untrusted_delimiter",
    "wrap_untrusted",
    "generate_skill_manifest",
    "write_skill_manifest",
    "verify_skill_integrity",
    # Prompts
    "_last_prompt_version",
    "build_task_prompt",
    "build_system_prompt",
    "build_checkpoint_context",
    "build_planner_prompt",
    "build_evaluator_prompt",
    # Reflexion
    "REFLEXION_PROMPT",
    "INITIAL_Q_VALUE",
    "run_reflexion_agent",
    "maybe_run_reflexion",
    # Agent Runner
    "build_cli_command",
    "run_agent",
    "run_agent_streaming",
    "run_agent_with_retries",
    "dispatch_agent",
    # Preflight
    "_run_install_cmd",
    "auto_install_dependencies",
    "_resolve_build_command",
    "preflight_build_check",
    "_dispatch_autofix_agent",
    "_handle_preflight_failure",
    # Hooks
    "LIFECYCLE_EVENTS",
    "register",
    "unregister",
    "fire",
    "fire_async",
    "load_hooks_config",
    "run_external_hook",
    "run_external_hook_async",
    "clear_registry",
    "clear_external_hooks",
    "get_registered_count",
    "get_external_hook_count",
    # Loops
    "run_quality_scoring",
    "run_security_review",
    "_extract_security_findings",
    "_create_security_lessons",
    "run_dev_test_loop",
    # Manager
    "parse_planner_output",
    "parse_evaluator_output",
    "run_planner_agent",
    "run_evaluator_agent",
    "run_manager_loop",
    # Dispatch
    "DEFAULT_FEATURE_FLAGS",
    "DEFAULT_DISPATCH_CONFIG",
    "is_feature_enabled",
    "load_dispatch_config",
    "scan_pending_work",
    "score_project",
    "apply_dispatch_filters",
    "run_project_tasks",
    "run_project_dispatch",
    "run_auto_dispatch",
    "load_goals_file",
    "validate_goals",
    "run_single_goal",
    "run_parallel_goals",
    "parse_task_ids",
    "run_parallel_tasks",
    # MCP Health
    "HEALTH_CACHE",
    "DEFAULT_BACKOFF",
    "MAX_BACKOFF",
    "HEALTHY_TTL",
    "MCPHealthMonitor",
    # MCP Server
    "run_server",
    # CLI
    "get_provider",
    "get_ollama_model",
    "get_ollama_base_url",
    "load_config",
    "_handle_add_project",
    "_post_task_telemetry",
    "async_main",
    "main",
]
