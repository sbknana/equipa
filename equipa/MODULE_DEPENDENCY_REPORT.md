# EQUIPA Module Dependency Report

**Generated:** 2026-03-24
**Task:** #1601
**Modules analyzed:** 21 (equipa/ package)

## Summary

The `equipa/` package contains 21 Python modules totaling **7,849 lines of code**. The dependency graph is 10 layers deep (L0–L9), with `constants.py` as the sole leaf module and `__init__.py` as the re-export hub at the top. Late (deferred) imports are used in 12 modules to break circular dependencies — no top-level circular imports exist.

## Module Dependency Table

| Module | Lines | Layer | Imports From (equipa) | Public Exports |
|---|---:|:---:|---|---|
| `constants.py` | 209 | L0 | *(none)* | 49 constants |
| `parsing.py` | 572 | L1 | constants | `CHARS_PER_TOKEN`, `SYSTEM_PROMPT_TOKEN_TARGET`, `SYSTEM_PROMPT_TOKEN_HARD_LIMIT`, `EPISODE_REDUCTION_THRESHOLD`, `estimate_tokens`, `compute_keyword_overlap`, `deduplicate_lessons`, `compact_agent_output`, `parse_reflection`, `parse_approach_summary`, `classify_agent_failure`, `parse_error_patterns`, `compute_initial_q_value`, `parse_tester_output`, `parse_developer_output`, `build_compaction_summary`, `build_test_failure_context`, `validate_output` (18) |
| `security.py` | 121 | L1 | constants | `wrap_untrusted`, `generate_skill_manifest`, `write_skill_manifest`, `verify_skill_integrity` (4) |
| `checkpoints.py` | 83 | L1 | constants | `save_checkpoint`, `load_checkpoint`, `clear_checkpoints` (3) |
| `git_ops.py` | 368 | L1 | constants, tasks^late | `detect_project_language`, `check_gh_installed`, `setup_single_repo`, `setup_all_repos` (4) |
| `db.py` | 415 | L1 | constants, output^late, tasks^late, prompts^late | `get_db_connection`, `ensure_schema`, `record_agent_run`, `classify_error`, `log_agent_action`, `bulk_log_agent_actions`, `update_task_status` (7) |
| `tasks.py` | 273 | L2 | constants, db | `fetch_task`, `fetch_next_todo`, `fetch_project_context`, `fetch_project_info`, `fetch_tasks_by_ids`, `get_task_complexity`, `verify_task_updated`, `resolve_project_dir` (8) |
| `monitoring.py` | 690 | L2 | constants, parsing^late | `LOOP_WARNING_THRESHOLD`, `LOOP_TERMINATE_THRESHOLD`, `LoopDetector`, `calculate_dynamic_budget`, `adjust_dynamic_budget` (5) |
| `output.py` | 292 | L2 | constants, monitoring^late | `log`, `print_manager_summary`, `print_summary`, `print_dev_test_summary`, `print_parallel_summary`, `print_dispatch_summary`, `print_dispatch_plan` (7) |
| `roles.py` | 165 | L2 | constants, tasks^late, output^late | `get_role_turns`, `get_role_model` (2) |
| `messages.py` | 130 | L2 | db, security^late | `post_agent_message`, `read_agent_messages`, `mark_messages_read`, `format_messages_for_prompt` (4) |
| `lessons.py` | 462 | L2 | constants, db, parsing, security^late, output^late | `format_lessons_for_injection`, `update_lesson_injection_count`, `get_relevant_episodes`, `format_episodes_for_injection`, `record_agent_episode`, `update_episode_injection_count`, `update_episode_q_values`, `update_injected_episode_q_values_for_task` (8) |
| `prompts.py` | 494 | L3 | constants, lessons, parsing, security, git_ops^late, dispatch^late | `build_task_prompt`, `build_system_prompt`, `build_checkpoint_context`, `build_planner_prompt`, `build_evaluator_prompt` (5) |
| `reflexion.py` | 144 | L3 | db, lessons, output, parsing, agent_runner^late | `REFLEXION_PROMPT`, `INITIAL_Q_VALUE`, `run_reflexion_agent`, `maybe_run_reflexion` (4) |
| `agent_runner.py` | 662 | L4 | constants, db, monitoring, output, parsing, security, tasks, cli^late | `build_cli_command`, `run_agent`, `run_agent_streaming`, `run_agent_with_retries`, `dispatch_agent` (5) |
| `preflight.py` | 307 | L4 | constants, output, agent_runner^late, prompts^late, roles^late | `auto_install_dependencies`, `preflight_build_check` (2) |
| `loops.py` | 751 | L5 | agent_runner, checkpoints, constants, db, messages, monitoring, output, parsing, preflight, prompts, roles, tasks, dispatch^late | `run_quality_scoring`, `run_security_review`, `run_dev_test_loop` (3) |
| `manager.py` | 310 | L6 | agent_runner, constants, db, loops, output, prompts, roles, tasks | `parse_planner_output`, `parse_evaluator_output`, `run_planner_agent`, `run_evaluator_agent`, `run_manager_loop` (5) |
| `dispatch.py` | 981 | L7 | constants, db, git_ops, lessons, loops, manager, output, prompts, reflexion, roles, tasks | `DEFAULT_FEATURE_FLAGS`, `DEFAULT_DISPATCH_CONFIG`, `is_feature_enabled`, `load_dispatch_config`, `scan_pending_work`, `score_project`, `apply_dispatch_filters`, `run_project_tasks`, `run_project_dispatch`, `run_auto_dispatch`, `load_goals_file`, `validate_goals`, `run_single_goal`, `run_parallel_goals`, `parse_task_ids`, `run_parallel_tasks` (16) |
| `cli.py` | 756 | L8 | constants, agent_runner, checkpoints, db, dispatch, git_ops, lessons, loops, manager, monitoring, output, parsing, prompts, reflexion, roles, security, tasks | `get_provider`, `get_ollama_model`, `get_ollama_base_url`, `load_config`, `async_main`, `main` (6) |
| `__init__.py` | 527 | L9 | *(all 20 modules)* | 226 re-exported symbols |

**Total:** 7,849 lines | 170 public exports

## Dependency Layer Diagram

```
L9  __init__.py ──────────────────────── re-export hub (527 LOC)
     │
L8  cli.py ───────────────────────────── entry point (756 LOC)
     │
L7  dispatch.py ──────────────────────── orchestration dispatch (981 LOC)
     │
L6  manager.py ───────────────────────── planner/evaluator loop (310 LOC)
     │
L5  loops.py ─────────────────────────── dev-test / quality / security loops (751 LOC)
     │
L4  agent_runner.py, preflight.py ────── agent execution (662 + 307 = 969 LOC)
     │
L3  prompts.py, reflexion.py ─────────── prompt construction + RL (494 + 144 = 638 LOC)
     │
L2  tasks.py, monitoring.py, output.py,  data access + monitoring (2,012 LOC)
     roles.py, messages.py, lessons.py
     │
L1  parsing.py, security.py,            leaf utilities (1,559 LOC)
     checkpoints.py, git_ops.py, db.py
     │
L0  constants.py ─────────────────────── configuration (209 LOC)
```

## Circular Import Analysis

**No top-level circular imports exist.** The codebase uses late (deferred) imports inside functions to break 6 potential cycles:

| Cycle | Broken By |
|---|---|
| `db.py` → `output.py` → `monitoring.py` → `parsing.py` | `db` uses late import of `output`, `tasks`, `prompts` |
| `output.py` ↔ `monitoring.py` | `output` uses late import of `monitoring` |
| `prompts.py` ↔ `dispatch.py` | `prompts` uses late import of `dispatch` |
| `agent_runner.py` ↔ `cli.py` | `agent_runner` uses late import of `cli` |
| `loops.py` ↔ `dispatch.py` | `loops` uses late import of `dispatch` |
| `git_ops.py` → `tasks.py` → `db.py` | `git_ops` uses late import of `tasks` |

## Late Import Inventory

12 of 21 modules use late/deferred imports to avoid circular dependencies:

| Module | Late Imports |
|---|---|
| `db.py` | output, tasks, prompts |
| `git_ops.py` | tasks |
| `lessons.py` | security, output |
| `monitoring.py` | parsing |
| `output.py` | monitoring |
| `roles.py` | tasks, output |
| `messages.py` | security |
| `prompts.py` | git_ops, dispatch |
| `reflexion.py` | agent_runner |
| `agent_runner.py` | cli |
| `preflight.py` | agent_runner, prompts, roles |
| `loops.py` | dispatch (×2) |

## Import Count per Module (how many other equipa modules each imports)

| Module | Top-level | Late | Total |
|---|---:|---:|---:|
| `constants.py` | 0 | 0 | **0** |
| `parsing.py` | 1 | 0 | **1** |
| `security.py` | 1 | 0 | **1** |
| `checkpoints.py` | 1 | 0 | **1** |
| `git_ops.py` | 1 | 1 | **2** |
| `db.py` | 1 | 3 | **4** |
| `tasks.py` | 2 | 0 | **2** |
| `monitoring.py` | 1 | 1 | **2** |
| `output.py` | 1 | 1 | **2** |
| `roles.py` | 1 | 2 | **3** |
| `messages.py` | 1 | 1 | **2** |
| `lessons.py` | 3 | 2 | **5** |
| `prompts.py` | 4 | 2 | **6** |
| `reflexion.py` | 4 | 1 | **5** |
| `agent_runner.py` | 7 | 1 | **8** |
| `preflight.py` | 2 | 3 | **5** |
| `loops.py` | 12 | 1 | **13** |
| `manager.py` | 8 | 0 | **8** |
| `dispatch.py` | 11 | 0 | **11** |
| `cli.py` | 17 | 0 | **17** |
| `__init__.py` | 20 | 0 | **20** |

## Observations

1. **`loops.py` is the most coupled module** (13 imports from other equipa modules), acting as the integration point for dev-test, quality scoring, and security review workflows.

2. **`cli.py` imports 17 modules** — expected for a CLI entry point, but refactoring opportunities exist (e.g., lazy-loading subcommand dependencies).

3. **`dispatch.py` is the second-most complex** (11 imports, 981 LOC) — it orchestrates project-level task routing and goal management.

4. **6 late-import cycles** suggest the dependency graph could benefit from further decoupling (e.g., extracting shared interfaces or using dependency injection).

5. **`constants.py` is the only true leaf** — all other L1 modules import from it. This makes `constants.py` a change amplifier: any constant rename requires updating all 20 downstream modules.

6. **`__init__.py` re-exports 226 symbols** — this "flat namespace" pattern simplifies external usage (`from equipa import X`) but means importing `equipa` triggers loading all 20 submodules.
