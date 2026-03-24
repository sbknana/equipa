# API.md — EQUIPA

## Table of Contents

- [API.md — EQUIPA](#apimd-equipa)
  - [Overview](#overview)
  - [CLI Interface](#cli-interface)
    - [Basic Usage](#basic-usage)
- [Dispatch the next pending task for a project](#dispatch-the-next-pending-task-for-a-project)
- [Dispatch specific tasks by ID](#dispatch-specific-tasks-by-id)
- [Run with goals file](#run-with-goals-file)
- [Run in parallel mode](#run-in-parallel-mode)
    - [CLI Configuration](#cli-configuration)
- [Config is auto-detected, but you can point to it explicitly](#config-is-auto-detected-but-you-can-point-to-it-explicitly)
- [See dispatch_config.example.json for the full shape](#see-dispatch_configexamplejson-for-the-full-shape)
    - [Key CLI Functions](#key-cli-functions)
  - [Python Module API](#python-module-api)
    - [`equipa/dispatch.py` — Task Dispatch & Scoring](#equipadispatchpy-task-dispatch-scoring)
- [Returns list of dicts with project info and pending task counts](#returns-list-of-dicts-with-project-info-and-pending-task-counts)
- [[101, 102, 103]](#101-102-103)
    - [`equipa/tasks.py` — Task Management](#equipataskspy-task-management)
    - [`equipa/db.py` — Database Layer](#equipadbpy-database-layer)
- ["import"](#import)
    - [`equipa/monitoring.py` — Loop Detection & Budget Management](#equipamonitoringpy-loop-detection-budget-management)
- [After each agent turn:](#after-each-agent-turn)
- [Returns "ok", "warn", or "terminate"](#returns-ok-warn-or-terminate)
    - [`equipa/lessons.py` — Episodic Memory](#equipalessonspy-episodic-memory)
    - [`equipa/parsing.py` — Output Parsing](#equipaparsingpy-output-parsing)
    - [`equipa/git_ops.py` — Git Operations](#equipagit_opspy-git-operations)
- [{"languages": ["typescript", "python"], "frameworks": ["nextjs", "fastapi"]}](#languages-typescript-python-frameworks-nextjs-fastapi)
    - [`equipa/messages.py` — Inter-Agent Communication](#equipamessagespy-inter-agent-communication)
    - [`equipa/security.py` — Security & Integrity](#equipasecuritypy-security-integrity)
    - [`equipa/checkpoints.py` — State Persistence](#equipacheckpointspy-state-persistence)
    - [`equipa/preflight.py` — Pre-Run Checks](#equipapreflightpy-pre-run-checks)
    - [`equipa/prompts.py` — Prompt Building](#equipapromptspy-prompt-building)
    - [`equipa/manager.py` — Planner & Evaluator](#equipamanagerpy-planner-evaluator)
  - [Ollama Agent API](#ollama-agent-api)
    - [Ollama Connection](#ollama-connection)
    - [Agent Tools](#agent-tools)
    - [Safety Functions](#safety-functions)
  - [ForgeSmith API — Self-Improvement System](#forgesmith-api-self-improvement-system)
    - [`forgesmith.py` — Core Analysis Engine](#forgesmithpy-core-analysis-engine)
    - [Key Analysis Functions](#key-analysis-functions)
    - [Rubric Scoring](#rubric-scoring)
- [{](#)
- ["total_score": 32,](#total_score-32)
- ["normalized_score": 0.64,](#normalized_score-064)
- ["dimensions": {](#dimensions-)
- ["naming_consistency": 6,](#naming_consistency-6)
- ["code_structure": 7,](#code_structure-7)
- ["test_coverage": 8,](#test_coverage-8)
- ["documentation": 4,](#documentation-4)
- ["error_handling": 7](#error_handling-7)
- [},](#)
- ["details": {...}](#details-)
- [}](#)
    - [`forgesmith_gepa.py` — Genetic Evolution of Prompt Attributes](#forgesmith_gepapy-genetic-evolution-of-prompt-attributes)
    - [GEPA Safety](#gepa-safety)
    - [`forgesmith_simba.py` — Situation-specific IMitative Behavioral Advice](#forgesmith_simbapy-situation-specific-imitative-behavioral-advice)
    - [`forgesmith_impact.py` — Change Impact Assessment](#forgesmith_impactpy-change-impact-assessment)
  - [Standalone Tools](#standalone-tools)
    - [`nightly_review.py` — Daily Performance Summary](#nightly_reviewpy-daily-performance-summary)
    - [`analyze_performance.py` — Deep Performance Analysis](#analyze_performancepy-deep-performance-analysis)
    - [`autoresearch_loop.py` — Automated Prompt Optimization](#autoresearch_looppy-automated-prompt-optimization)
    - [`lesson_sanitizer.py` — Input Sanitization](#lesson_sanitizerpy-input-sanitization)
    - [`db_migrate.py` — Database Migrations](#db_migratepy-database-migrations)
    - [`equipa_setup.py` — Interactive Setup](#equipa_setuppy-interactive-setup)
  - [Tools Directory](#tools-directory)
    - [`tools/forge_dashboard.py` — Terminal Dashboard](#toolsforge_dashboardpy-terminal-dashboard)
    - [`tools/forge_arena.py` — Agent Arena](#toolsforge_arenapy-agent-arena)
    - [`tools/prepare_training_data.py` — Training Data Prep](#toolsprepare_training_datapy-training-data-prep)
    - [`tools/benchmark_migrations.py` — Migration Benchmarking](#toolsbenchmark_migrationspy-migration-benchmarking)
  - [SARIF Helpers — Security Analysis](#sarif-helpers-security-analysis)
  - [Database Direct Access](#database-direct-access)
- [Get all pending tasks](#get-all-pending-tasks)
- [Get recent agent episodes](#get-recent-agent-episodes)
  - [Error Handling](#error-handling)
    - [Agent Errors](#agent-errors)
  - [Related Documentation](#related-documentation)

## Overview

EQUIPA isn't a web service with REST endpoints. It's a local multi-agent orchestration system that runs on your machine, coordinates AI agents through a SQLite database, and exposes its functionality through Python modules and CLI commands.

There's no HTTP API, no server to start, no authentication tokens. Everything talks through SQLite and the filesystem.

If you're looking to integrate with EQUIPA, you have three options:

1. **CLI** — run commands directly
2. **Python imports** — use the `equipa/` package modules
3. **SQLite** — read/write the database directly (30+ tables)

This doc covers all three.

---

## CLI Interface

The main entry point is `equipa/cli.py`. It handles task dispatch, agent coordination, and the dev-test iteration loop.

### Basic Usage

```bash
# Dispatch the next pending task for a project
python -m equipa --project 23

# Dispatch specific tasks by ID
python -m equipa --tasks 101,102,103

# Run with goals file
python -m equipa --goals goals.yaml

# Run in parallel mode
python -m equipa --parallel
```

### CLI Configuration

Config is loaded from a dispatch config file (JSON/YAML). The CLI reads provider settings, model preferences, and feature flags from this file.

```bash
# Config is auto-detected, but you can point to it explicitly
# See dispatch_config.example.json for the full shape
```

### Key CLI Functions

| Function | What it does |
|----------|-------------|
| `main()` | Entry point. Parses args, loads config, runs dispatch |
| `async_main()` | The actual async orchestration loop |
| `load_config()` | Reads dispatch config, merges with defaults |
| `get_provider(role, dispatch_config)` | Picks AI provider for a given agent role |
| `get_ollama_model(role, dispatch_config)` | Gets the Ollama model name for a role |
| `get_ollama_base_url(dispatch_config)` | Returns the Ollama server URL |

---

## Python Module API

### `equipa/dispatch.py` — Task Dispatch & Scoring

This is the brain that decides what work to do next and how to do it.

```python
from equipa.dispatch import (
    scan_pending_work,
    score_project,
    load_dispatch_config,
    is_feature_enabled,
    run_auto_dispatch,
    run_parallel_tasks,
    run_parallel_goals,
)
```

#### `scan_pending_work()`

Scans the database for pending tasks across all projects. Returns a list of work items with project context.

```python
work = scan_pending_work()
# Returns list of dicts with project info and pending task counts
```

#### `score_project(summary, config)`

Scores a project's priority based on pending work, blockers, and config weights.

| Parameter | Type | Description |
|-----------|------|-------------|
| `summary` | dict | Project summary from `scan_pending_work()` |
| `config` | dict | Dispatch configuration |

Returns: numeric score (higher = dispatch first)

#### `load_dispatch_config(filepath)`

Loads and validates dispatch config. Deep-merges with defaults so you only need to specify what you're changing.

| Parameter | Type | Description |
|-----------|------|-------------|
| `filepath` | str | Path to JSON config file |

Returns: dict with full config including all defaults filled in

#### `is_feature_enabled(dispatch_config, feature_name)`

Checks if a feature flag is on. Falls back to built-in defaults if the config doesn't mention the feature.

| Parameter | Type | Description |
|-----------|------|-------------|
| `dispatch_config` | dict or None | Config dict, or None for all defaults |
| `feature_name` | str | Feature flag name |

Returns: bool

```python
if is_feature_enabled(config, "episode_injection"):
    # inject episodic memory into agent prompts
    pass
```

#### `apply_dispatch_filters(work, config, args)`

Filters the work queue based on config rules and CLI args. Removes projects that shouldn't be dispatched right now.

#### `async run_auto_dispatch(scored, config, args)`

The main dispatch loop. Takes scored work items and runs agents against them. This is where the dev-test iteration happens — agents retry until tests pass or budget runs out.

#### `async run_parallel_tasks(task_ids, args)`

Dispatches specific tasks by ID in parallel.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_ids` | list[int] | Task IDs to dispatch |
| `args` | namespace | CLI arguments |

#### `async run_parallel_goals(resolved_goals, defaults, args)`

Runs goal-based dispatch. Goals are higher-level objectives that get broken into tasks.

#### `parse_task_ids(task_str)`

Parses comma-separated task ID strings into a list of ints.

```python
ids = parse_task_ids("101,102,103")
# [101, 102, 103]
```

#### `load_goals_file(filepath)` / `validate_goals(goals)`

Loads and validates a goals YAML file for goal-based dispatch.

---

### `equipa/tasks.py` — Task Management

All the database operations for reading and updating tasks.

```python
from equipa.tasks import (
    fetch_task,
    fetch_next_todo,
    fetch_project_context,
    fetch_project_info,
    fetch_tasks_by_ids,
    get_task_complexity,
    verify_task_updated,
    resolve_project_dir,
)
```

#### `fetch_task(task_id)`

Gets a single task by ID from the database.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | int | The task ID |

Returns: dict with task fields, or None

#### `fetch_next_todo(project_id)`

Gets the highest-priority pending task for a project.

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | int | Project to scan |

Returns: dict or None

#### `fetch_project_context(project_id)`

Gets project context including recent decisions, active tasks, and current state. This gets injected into agent prompts so they understand what's going on.

#### `fetch_tasks_by_ids(task_ids)`

Batch fetch. Returns tasks in the order you asked for them.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_ids` | list[int] | Task IDs |

Returns: list of task dicts

#### `get_task_complexity(task)`

Returns the complexity level of a task. This matters because it affects turn budgets and cost limits.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task` | dict | Task dict from fetch_task |

Returns: string — complexity level

#### `verify_task_updated(task_id)`

Checks that a task was actually modified after an agent run. Used to catch agents that claim they did work but didn't actually change anything.

#### `resolve_project_dir(task)`

Figures out the filesystem path for a task's project. Handles worktree paths.

---

### `equipa/db.py` — Database Layer

SQLite connection management and schema operations.

```python
from equipa.db import get_db_connection, ensure_schema, classify_error
```

#### `get_db_connection(write=False)`

Gets a database connection. Pass `write=True` if you need to modify data.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `write` | bool | `False` | Whether you need write access |

Returns: sqlite3.Connection

```python
conn = get_db_connection(write=True)
try:
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    conn.commit()
finally:
    conn.close()
```

#### `ensure_schema()`

Creates all required tables if they don't exist. Safe to call multiple times. The schema has 30+ tables covering tasks, projects, episodes, lessons, agent messages, and more.

#### `classify_error(error_text)`

Classifies an error string into a category. Used by ForgeSmith to learn from failures.

| Parameter | Type | Description |
|-----------|------|-------------|
| `error_text` | str | Error output from an agent run |

Returns: string — one of: `"timeout"`, `"file_not_found"`, `"permission"`, `"syntax"`, `"import"`, `"test_failure"`, `"unknown"`

```python
error_type = classify_error("ModuleNotFoundError: No module named 'flask'")
# "import"
```

---

### `equipa/monitoring.py` — Loop Detection & Budget Management

Catches agents that are going in circles or burning too much money.

```python
from equipa.monitoring import LoopDetector, calculate_dynamic_budget
```

#### `LoopDetector`

Watches agent behavior turn-by-turn and kills agents that are stuck.

```python
detector = LoopDetector()

# After each agent turn:
action = detector.record(result)
# Returns "ok", "warn", or "terminate"

if action == "warn":
    print(detector.warning_message())
elif action == "terminate":
    print(detector.termination_summary())
```

The detector tracks:
- **Fingerprinting** — hashes agent outputs to detect repetition
- **File changes** — resets the stuck counter when the agent actually modifies files
- **Tool loops** — catches agents calling the same tool with the same args repeatedly
- **Monologue detection** — kills agents that just talk without using tools (3+ consecutive text-only turns after the first 5)
- **Alternating patterns** — detects A→B→A→B cycles at 6 repetitions

#### `calculate_dynamic_budget(max_turns)`

Calculates budget warning thresholds based on total turn count.

| Parameter | Type | Description |
|-----------|------|-------------|
| `max_turns` | int | Maximum turns allowed |

Returns: dict with warning intervals and thresholds

---

### `equipa/lessons.py` — Episodic Memory

The learning system. Agents get injected with lessons from past successes and failures.

```python
from equipa.lessons import (
    get_active_simba_rules,
    update_lesson_injection_count,
    update_episode_injection_count,
)
```

#### `get_active_simba_rules()`

Returns active SIMBA rules — behavioral guidelines generated by ForgeSmith from analyzing agent performance patterns.

Returns: list of rule dicts

#### `update_lesson_injection_count(lesson_ids)`

Tracks how many times each lesson has been injected into agent prompts. Used to measure lesson effectiveness over time.

#### `update_episode_injection_count(episode_ids)`

Same as above but for episodic memories.

---

### `equipa/parsing.py` — Output Parsing

Extracts structured data from messy agent output.

```python
from equipa.parsing import (
    parse_reflection,
    parse_approach_summary,
    parse_tester_output,
    parse_developer_output,
    validate_output,
    compact_agent_output,
    estimate_tokens,
    build_test_failure_context,
    compute_initial_q_value,
)
```

#### `parse_reflection(result_text)`

Extracts the agent's self-reflection from its output. Reflections feed into the learning loop.

#### `parse_developer_output(result_text)` / `parse_tester_output(result_text)`

Role-specific parsers that extract structured results from agent output.

#### `validate_output(result)`

Checks if agent output meets minimum quality standards. Catches empty results, missing sections, etc.

#### `compact_agent_output(raw_output, max_words)`

Truncates agent output for storage without losing critical information.

| Parameter | Type | Description |
|-----------|------|-------------|
| `raw_output` | str | Full agent output |
| `max_words` | int | Max words to keep |

Returns: truncated string

#### `estimate_tokens(text)`

Quick token count estimate. Not exact, but close enough for budget tracking.

#### `compute_initial_q_value(outcome)`

Sets the starting Q-value for an episode based on outcome. Q-values are used to rank episodes for future injection — good episodes get shown to agents more often.

#### `build_test_failure_context(test_results, cycle)`

Formats test failure information for the dev-test retry loop. When tests fail, this builds the context that tells the developer agent what went wrong.

---

### `equipa/git_ops.py` — Git Operations

```python
from equipa.git_ops import detect_project_language, check_gh_installed, setup_all_repos
```

#### `detect_project_language(project_dir)`

Detects programming languages and frameworks used in a project by scanning for marker files.

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_dir` | str | Path to project root |

Returns: dict with `languages` (list) and `frameworks` (list)

```python
info = detect_project_language("/path/to/my-app")
# {"languages": ["typescript", "python"], "frameworks": ["nextjs", "fastapi"]}
```

Detected languages: Python, TypeScript, JavaScript, Go, Rust, C#, Java
Detected frameworks: Django, FastAPI, Next.js, Vue, Angular, Express

#### `check_gh_installed()`

Checks if the GitHub CLI (`gh`) is available. Returns bool.

#### `setup_all_repos(args)`

Sets up git repos and worktrees for all projects. Used during initial setup.

---

### `equipa/messages.py` — Inter-Agent Communication

Agents can leave messages for each other. This is how the dev-test loop communicates test failures back to the developer.

```python
from equipa.messages import format_messages_for_prompt
```

#### `format_messages_for_prompt(messages)`

Formats agent messages into a string suitable for prompt injection.

| Parameter | Type | Description |
|-----------|------|-------------|
| `messages` | list[dict] | Messages from the database |

Returns: formatted string

---

### `equipa/security.py` — Security & Integrity

```python
from equipa.security import (
    wrap_untrusted,
    generate_skill_manifest,
    write_skill_manifest,
    verify_skill_integrity,
)
```

#### `wrap_untrusted(content, delimiter)`

Wraps untrusted content with delimiter markers. Prevents prompt injection from user-supplied data leaking into agent instructions.

| Parameter | Type | Description |
|-----------|------|-------------|
| `content` | str | Untrusted content to wrap |
| `delimiter` | str | Delimiter tag name |

#### `generate_skill_manifest()` / `write_skill_manifest()` / `verify_skill_integrity()`

SHA-256 integrity checking for skill files and prompts. Detects if someone (or something) has tampered with agent instructions.

```python
if not verify_skill_integrity():
    print("WARNING: Skill files have been modified outside normal channels")
```

---

### `equipa/checkpoints.py` — State Persistence

```python
from equipa.checkpoints import clear_checkpoints
```

#### `clear_checkpoints(task_id, role)`

Removes checkpoint files for a task/role combo. Checkpoints are anti-compaction state — they preserve full agent context across interruptions so nothing gets lost.

---

### `equipa/preflight.py` — Pre-Run Checks

```python
from equipa.preflight import auto_install_dependencies
```

#### `async auto_install_dependencies(project_dir, output)`

Detects the project type and tries to install dependencies before an agent runs. Supports Node.js, Python, Go, and C# projects. Times out at 60 seconds.

This doesn't block the task if it fails — it just injects the failure info into the agent's context so the agent knows what's broken.

---

### `equipa/prompts.py` — Prompt Building

```python
from equipa.prompts import build_checkpoint_context
```

#### `build_checkpoint_context(checkpoint_text, attempt)`

Builds context from previous checkpoint state. Used when resuming interrupted work.

---

### `equipa/manager.py` — Planner & Evaluator

```python
from equipa.manager import parse_planner_output, parse_evaluator_output
```

#### `parse_planner_output(result_text)` / `parse_evaluator_output(result_text)`

Parsers for the planner and evaluator agent roles. These higher-level agents break down goals into tasks and evaluate completed work.

---

## Ollama Agent API

`ollama_agent.py` is a standalone agent runner that talks to local Ollama models. It has its own set of tool functions.

### Ollama Connection

```python
from ollama_agent import check_ollama_health, list_ollama_models, ollama_chat
```

#### `check_ollama_health(base_url)`

Pings the Ollama server. Returns bool.

| Parameter | Type | Description |
|-----------|------|-------------|
| `base_url` | str | Ollama server URL (e.g., `http://localhost:11434`) |

#### `list_ollama_models(base_url)`

Lists available models on the Ollama server.

#### `ollama_chat(base_url, model, messages, tools, timeout)`

Sends a chat request to Ollama with tool support.

| Parameter | Type | Description |
|-----------|------|-------------|
| `base_url` | str | Ollama server URL |
| `model` | str | Model name |
| `messages` | list | Chat messages |
| `tools` | list | Tool definitions (inferred) |
| `timeout` | int | Request timeout in seconds (inferred) |

### Agent Tools

These are the tools available to Ollama-powered agents:

| Function | Description | Write Access Needed |
|----------|-------------|-------------------|
| `exec_read_file(project_dir, args)` | Read a file's contents | No |
| `exec_list_directory(project_dir, args)` | List directory contents | No |
| `exec_search_files(project_dir, args)` | Search for files by pattern | No |
| `exec_grep(project_dir, args)` | Grep through project files | No |
| `exec_bash(project_dir, args, allow_write)` | Run a bash command | Depends |
| `exec_write_file(project_dir, args)` | Write/create a file | Yes |
| `exec_edit_file(project_dir, args)` | Edit a file in place | Yes |

### Safety Functions

```python
from ollama_agent import safe_path, is_safe_read_command, is_blocked_command
```

#### `safe_path(project_dir, relative_path)`

Validates that a path stays within the project directory. Prevents path traversal attacks from agents trying to read `/etc/passwd` or whatever.

#### `is_safe_read_command(command)`

Checks if a bash command is read-only. Used when agents run bash without write permission.

#### `is_blocked_command(command)`

Checks against a blocklist of dangerous commands. Things like `rm -rf /`, `curl | sh`, etc.

---

## ForgeSmith API — Self-Improvement System

ForgeSmith is the closed-loop self-improvement system. It analyzes agent performance and evolves prompts, rules, and config automatically. This is the ForgeSmith + GEPA + SIMBA stack.

> **Note:** Self-improvement needs 20-30 completed tasks before patterns emerge. Don't expect results on day one.

### `forgesmith.py` — Core Analysis Engine

```python
from forgesmith import run_full, run_report, run_rollback, run_propose_only
```

#### `run_full(cfg, dry_run)`

Full analysis + apply cycle. Collects agent run data, extracts lessons, analyzes patterns, and applies config/prompt changes.

| Parameter | Type | Description |
|-----------|------|-------------|
| `cfg` | dict | ForgeSmith configuration |
| `dry_run` | bool | If True, analyze but don't change anything |

#### `run_report(cfg)`

Generates a performance report without changing anything. Good for understanding what ForgeSmith would do.

#### `run_rollback(run_id_to_revert)`

Reverts all changes from a specific ForgeSmith run. Every change is tracked and reversible.

#### `run_propose_only(cfg, dry_run)`

Generates OPRO (Optimization by PROmpting) proposals without applying them. Uses Claude to suggest prompt improvements based on metrics.

### Key Analysis Functions

| Function | What it analyzes |
|----------|-----------------|
| `analyze_max_turns_hit(runs, cfg)` | Agents running out of turns — maybe they need more budget |
| `analyze_turn_underuse(runs, cfg)` | Agents finishing way under budget — maybe reduce turns to save money |
| `analyze_model_downgrade(runs, cfg)` | Whether cheaper models could handle certain tasks |
| `analyze_repeat_errors(runs, cfg)` | Same errors happening over and over |
| `analyze_blocked_tasks(blocked_tasks, runs, cfg)` | Tasks stuck in blocked state |
| `extract_lessons(runs, cfg)` | Pulls reusable lessons from agent runs |
| `evaluate_previous_changes(runs, cfg)` | Checks if past ForgeSmith changes actually helped |

### Rubric Scoring

```python
from forgesmith import compute_rubric_score, score_completed_runs
from rubric_quality_scorer import score_agent_output
```

#### `score_agent_output(result_text, files_changed, role)`

Scores agent output quality across 5 dimensions:

| Dimension | What it measures |
|-----------|-----------------|
| Naming consistency | Variable/function naming quality |
| Code structure | Organization and modularity |
| Test coverage | Presence of tests and test patterns |
| Documentation | Comments, docstrings, README updates |
| Error handling | Try/catch, validation, edge cases |

| Parameter | Type | Description |
|-----------|------|-------------|
| `result_text` | str | Agent's output text |
| `files_changed` | list | Files the agent modified |
| `role` | str | Agent role (affects weight distribution) |

Returns: dict with `total_score`, `normalized_score`, `dimensions` (dict of 5 scores), `details`

```python
scores = score_agent_output(
    result_text="Added error handling to the payment module...",
    files_changed=["payment.py", "test_payment.py"],
    role="developer"
)
# {
#     "total_score": 32,
#     "normalized_score": 0.64,
#     "dimensions": {
#         "naming_consistency": 6,
#         "code_structure": 7,
#         "test_coverage": 8,
#         "documentation": 4,
#         "error_handling": 7
#     },
#     "details": {...}
# }
```

---

### `forgesmith_gepa.py` — Genetic Evolution of Prompt Attributes

GEPA evolves agent prompts using genetic/evolutionary techniques. It collects episode data, runs optimization, and stores evolved prompts with A/B testing.

```python
from forgesmith_gepa import run_gepa, rollback_evolved_prompt, get_ab_test_status
```

#### `run_gepa(cfg, dry_run, role_filter, run_id)`

Runs prompt evolution for one or all roles.

| Parameter | Type | Description |
|-----------|------|-------------|
| `cfg` | dict | GEPA configuration |
| `dry_run` | bool | Analyze without applying |
| `role_filter` | str or None | Specific role to evolve, or None for all |
| `run_id` | str | Unique run identifier |

#### `get_ab_prompt_for_role(role)`

Returns the current A/B test prompt for a role, if one exists. The system randomly assigns agents to the evolved or original prompt to measure impact.

#### `rollback_evolved_prompt(role, version)`

Reverts a prompt to a previous version.

#### `get_ab_test_status(role)`

Returns A/B test results for a role — which prompt variant is winning.

### GEPA Safety

GEPA has guardrails to prevent prompt corruption:

| Function | What it checks |
|----------|---------------|
| `validate_evolved_prompt(current, evolved)` | Ensures evolved prompt isn't too different from current |
| `calculate_diff_ratio(old, new)` | Measures how much changed (blocks if > threshold) |
| `check_protected_sections(old, new)` | Ensures critical prompt sections aren't removed |

---

### `forgesmith_simba.py` — Situation-specific IMitative Behavioral Advice

SIMBA generates behavioral rules from analyzing agent success/failure patterns.

```python
from forgesmith_simba import run_simba, evaluate_simba_rules, prune_stale_rules
```

#### `run_simba(cfg, dry_run, role_filter)`

Analyzes episodes and generates new behavioral rules.

| Parameter | Type | Description |
|-----------|------|-------------|
| `cfg` | dict | SIMBA configuration |
| `dry_run` | bool | Generate rules without storing them |
| `role_filter` | str or None | Specific role, or None for all |

Returns: dict with generated rules per role

#### `evaluate_simba_rules()`

Checks if existing rules are actually helping. Needs minimum sample sizes to make judgments.

#### `prune_stale_rules(dry_run)`

Removes rules that haven't proven effective. Rules with negative or zero impact get pruned.

#### `validate_rule(rule, existing_rules)`

Validates a proposed rule before storage. Checks length, structure, error type validity, and deduplication.

| Parameter | Type | Description |
|-----------|------|-------------|
| `rule` | dict | Proposed rule |
| `existing_rules` | list | Currently active rules |

Returns: (bool, str) — (is_valid, reason)

---

### `forgesmith_impact.py` — Change Impact Assessment

Before ForgeSmith applies a change, it estimates the blast radius.

```python
from forgesmith_impact import (
    identify_affected_roles,
    compute_blast_radius,
    assess_risk_level,
)
```

#### `identify_affected_roles(change_type, target_file, old_value, new_value)`

Figures out which agent roles would be affected by a proposed change.

#### `compute_blast_radius(affected_roles)`

Returns a numeric score for how many things a change could break.

#### `assess_risk_level(change_type, affected_roles, blast_radius, diff_ratio)`

Returns risk level: probably something like "low", "medium", "high". Used to gate automatic vs. manual approval.

---

## Standalone Tools

### `nightly_review.py` — Daily Performance Summary

Generates a nightly review report from the database.

```bash
python nightly_review.py [--db /path/to/equipa.db]
```

Key functions:

| Function | What it reports |
|----------|----------------|
| `get_portfolio_stats(conn)` | Overall project portfolio health |
| `get_today_accomplishments(conn)` | What got done today |
| `get_blockers(conn)` | Currently blocked tasks |
| `get_stale_projects(conn)` | Projects with no recent activity |
| `get_stale_tasks(conn)` | Tasks that haven't moved |
| `get_agent_stats(conn)` | Agent success rates |
| `get_open_questions(conn)` | Unresolved questions needing human input |
| `get_upcoming_reminders(conn)` | Scheduled reminders |

### `analyze_performance.py` — Deep Performance Analysis

More detailed than the nightly review. Analyzes completion rates, time-to-complete, throughput trends.

```bash
python analyze_performance.py [--project PROJECT_ID] [--days 30]
```

### `autoresearch_loop.py` — Automated Prompt Optimization

Runs an optimization loop that mutates prompts, dispatches test tasks, waits for results, and keeps the mutations that work.

```bash
python autoresearch_loop.py --role developer --target-pct 85 --max-rounds 10
python autoresearch_loop.py --status  # Show current optimization state
```

### `lesson_sanitizer.py` — Input Sanitization

Sanitizes lesson content before injection into agent prompts. Strips XML injection, base64 payloads, ANSI escapes, and role override attempts.

```python
from lesson_sanitizer import sanitize_lesson_content, validate_lesson_structure

clean = sanitize_lesson_content(untrusted_text)
is_valid, reason = validate_lesson_structure(clean)
```

### `db_migrate.py` — Database Migrations

Handles schema migrations between EQUIPA versions.

```bash
python db_migrate.py [--db /path/to/equipa.db] [--silent]
```

Migrations are versioned (v0 → v1 → v2 → v3 → v4). Each migration backs up the database first.

### `equipa_setup.py` — Interactive Setup

Step-by-step setup wizard for new installations.

```bash
python equipa_setup.py
```

---

## Tools Directory

### `tools/forge_dashboard.py` — Terminal Dashboard

Shows task summaries, per-day throughput, project completion, blockers, and checkpoint analysis in the terminal.

```bash
python tools/forge_dashboard.py [--days 14]
```

### `tools/forge_arena.py` — Agent Arena

Runs structured multi-phase testing against the agent system. Creates tasks, dispatches them, checks for convergence, and exports LoRA training data.

```bash
python tools/forge_arena.py [--dry-run] [--max-iterations 5]
```

### `tools/prepare_training_data.py` — Training Data Prep

Converts agent interaction logs into training data format (conversation pairs) for fine-tuning.

```bash
python tools/prepare_training_data.py
```

### `tools/benchmark_migrations.py` — Migration Benchmarking

Tests database migrations for correctness and performance. Creates test databases at each version, runs migrations, and verifies data integrity.

```bash
python tools/benchmark_migrations.py
```

---

## SARIF Helpers — Security Analysis

`skills/security/static-analysis/skills/sarif-parsing/resources/sarif_helpers.py`

A standalone module for working with SARIF (Static Analysis Results Interchange Format) files. Used by the security reviewer agent role.

```python
from sarif_helpers import (
    load_sarif, extract_findings, filter_by_level,
    filter_by_file, group_by_file, deduplicate, summary
)
```

#### Key Functions

| Function | Description |
|----------|-------------|
| `load_sarif(path)` | Load a SARIF file |
| `extract_findings(sarif)` | Pull all findings from SARIF data |
| `filter_by_level(findings, *levels)` | Filter by severity level |
| `filter_by_file(findings, pattern)` | Filter by file path pattern |
| `filter_by_rule(findings, *rule_ids)` | Filter by specific rule IDs |
| `group_by_file(findings)` | Group findings by source file |
| `group_by_rule(findings)` | Group findings by rule |
| `deduplicate(findings)` | Remove duplicate findings (SHA-256 fingerprinting) |
| `merge_sarif_files(*paths)` | Merge multiple SARIF files |
| `summary(findings)` | Generate a human-readable summary |
| `to_csv_rows(findings)` | Export as CSV-ready rows |

---

## Database Direct Access

If you want to talk to the database directly, connect to the SQLite file. The schema is created by `ensure_schema()` and evolved by `db_migrate.py`.

```python
import sqlite3

conn = sqlite3.connect("/path/to/equipa.db")
conn.row_factory = sqlite3.Row

# Get all pending tasks
tasks = conn.execute(
    "SELECT id, title, status, priority FROM tasks WHERE status = 'todo' ORDER BY priority DESC"
).fetchall()

# Get recent agent episodes
episodes = conn.execute("""
    SELECT role, result, reflection, q_value, created_at 
    FROM episodes 
    WHERE created_at > datetime('now', '-7 days')
    ORDER BY created_at DESC
""").fetchall()
```

> **Warning:** Writing to the database while EQUIPA is running could cause conflicts. Use `get_db_connection(write=True)` from `equipa/db.py` if you're writing from within the system — it handles locking.

---

## Error Handling

EQUIPA doesn't use HTTP status codes (it's not a web service). Errors flow through these channels:

### Agent Errors

Classified by `classify_error()` into categories:

| Error Type | Example |
|-----------|---------|
| `timeout` |
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
