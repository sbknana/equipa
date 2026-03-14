# API.md — EQUIPA

## Table of Contents

- [API.md — EQUIPA](#apimd-equipa)
  - [Overview](#overview)
  - [CLI Entry Points](#cli-entry-points)
    - [Forge Orchestrator — `forge_orchestrator.py`](#forge-orchestrator-forge_orchestratorpy)
    - [Forgesmith — `forgesmith.py`](#forgesmith-forgesmithpy)
    - [Forgesmith SIMBA — `forgesmith_simba.py`](#forgesmith-simba-forgesmith_simbapy)
    - [Forgesmith GEPA — `forgesmith_gepa.py`](#forgesmith-gepa-forgesmith_gepapy)
    - [Forge Arena — `forge_arena.py`](#forge-arena-forge_arenapy)
    - [Database Migration — `db_migrate.py`](#database-migration-db_migratepy)
    - [Additional Tools](#additional-tools)
  - [Internal Python Interfaces](#internal-python-interfaces)
    - [Agent Messaging System](#agent-messaging-system)
    - [Agent Actions Logging](#agent-actions-logging)
    - [Loop Detection — `LoopDetector` class](#loop-detection-loopdetector-class)
    - [Lesson Injection System](#lesson-injection-system)
    - [Episode Memory System](#episode-memory-system)
    - [Lesson Sanitizer — `lesson_sanitizer.py`](#lesson-sanitizer-lesson_sanitizerpy)
    - [Rubric Quality Scoring — `rubric_quality_scorer.py`](#rubric-quality-scoring-rubric_quality_scorerpy)
    - [SARIF Helpers — `skills/security/static-analysis/skills/sarif-parsing/resources/sarif_helpers.py`](#sarif-helpers-skillssecuritystatic-analysisskillssarif-parsingresourcessarif_helperspy)
    - [Ollama Agent — `ollama_agent.py`](#ollama-agent-ollama_agentpy)
  - [Database Schema (Key Tables — Inferred)](#database-schema-key-tables-inferred)
  - [Error Handling](#error-handling)
    - [Agent Error Classification](#agent-error-classification)
    - [Early Termination Conditions](#early-termination-conditions)
    - [Budget Awareness Messages (inferred)](#budget-awareness-messages-inferred)
  - [Configuration](#configuration)
    - [Dispatch Configuration](#dispatch-configuration)
    - [Forgesmith Configuration](#forgesmith-configuration)
  - [Adding HTTP API Endpoints](#adding-http-api-endpoints)
  - [Related Documentation](#related-documentation)

## Overview

EQUIPA is a **multi-agent AI orchestration platform** built in pure Python with SQLite as its data store. It is **not a traditional REST/GraphQL/tRPC API service**. Instead, it operates as a collection of CLI tools, orchestration scripts, and internal Python function interfaces that coordinate AI agents (developers, testers, security reviewers) to execute software development tasks.

**There are no HTTP API endpoints exposed by this project.**

EQUIPA's "API" is an internal programmatic interface consisting of:

- **CLI entry points** (invoked via `python script.py [args]`)
- **SQLite database operations** (the central state store)
- **Inter-agent messaging** (via the `agent_messages` subsystem)
- **Internal Python function interfaces** used across modules

---

## CLI Entry Points

### Forge Orchestrator — `forge_orchestrator.py`

The primary entry point for dispatching and managing agent tasks.

| Command / Mode | Description |
|---|---|
| `python forge_orchestrator.py --task <id>` | Run a single task by ID (inferred) |
| `python forge_orchestrator.py --dispatch` | Auto-dispatch tasks across projects based on scoring (inferred) |
| `python forge_orchestrator.py --goals <file>` | Run parallel goals from a YAML/JSON goals file (inferred) |
| `python forge_orchestrator.py --tasks <id,id,...>` | Run multiple tasks in parallel (inferred) |
| `python forge_orchestrator.py --setup-repos` | Set up GitHub repos for all projects (inferred) |
| `python forge_orchestrator.py --scan` | Scan for pending work across projects (inferred) |

### Forgesmith — `forgesmith.py`

The self-improvement engine that analyzes agent performance and evolves prompts/config.

| Command / Mode | Description |
|---|---|
| `python forgesmith.py` | Run full analysis + apply changes (inferred) |
| `python forgesmith.py --dry-run` | Analyze without applying changes (inferred) |
| `python forgesmith.py --report` | Generate performance report only (inferred) |
| `python forgesmith.py --rollback <run_id>` | Revert changes from a specific run (inferred) |
| `python forgesmith.py --propose-only` | Generate O-PRO proposals without applying (inferred) |

### Forgesmith SIMBA — `forgesmith_simba.py`

Generates behavioral rules from high-variance agent episodes.

| Command / Mode | Description |
|---|---|
| `python forgesmith_simba.py` | Run SIMBA rule generation pipeline (inferred) |
| `python forgesmith_simba.py --dry-run` | Preview rules without storing (inferred) |
| `python forgesmith_simba.py --role <role>` | Filter to a specific agent role (inferred) |

### Forgesmith GEPA — `forgesmith_gepa.py`

Genetic/evolutionary prompt optimization for agent roles.

| Command / Mode | Description |
|---|---|
| `python forgesmith_gepa.py` | Run GEPA prompt evolution (inferred) |
| `python forgesmith_gepa.py --dry-run` | Preview evolved prompts without deploying (inferred) |
| `python forgesmith_gepa.py --role <role>` | Evolve prompts for a specific role (inferred) |

### Forge Arena — `forge_arena.py`

Multi-phase testing arena for agent evaluation.

| Command / Mode | Description |
|---|---|
| `python forge_arena.py` | Run arena evaluation phases (inferred) |
| `python forge_arena.py --dry-run` | Simulate without dispatching tasks (inferred) |
| `python forge_arena.py --export-lora` | Export training data for LoRA fine-tuning (inferred) |

### Database Migration — `db_migrate.py`

Schema migration tool for the SQLite database.

| Command / Mode | Description |
|---|---|
| `python db_migrate.py` | Run all pending migrations |
| `python db_migrate.py --silent` | Run migrations without output (inferred) |

### Additional Tools

| Script | Description |
|---|---|
| `equipa_setup.py` | Interactive setup wizard for new installations |
| `forge_dashboard.py` | Terminal dashboard for task/project status |
| `analyze_performance.py` | Performance analytics and reporting |
| `nightly_review.py` | Automated nightly portfolio review |
| `autoresearch_loop.py` | Automated prompt optimization loop |
| `autoresearch_prompts.py` | O-PRO prompt research and mutation |
| `forgesmith_backfill.py` | Backfill episode data from agent logs |
| `forgesmith_impact.py` | Impact assessment for configuration changes |
| `ingest_training_results.py` | Ingest fine-tuning results into the database |
| `prepare_training_data.py` | Prepare conversation data for model training |
| `train_qlora.py` | QLoRA training script |
| `train_qlora_peft.py` | QLoRA training with PEFT |
| `ollama_agent.py` | Local Ollama-based agent with sandboxed tool execution |
| `benchmark_migrations.py` | Benchmark and verify database migrations |

---

## Internal Python Interfaces

### Agent Messaging System

The inter-agent communication layer used by the orchestrator for multi-agent coordination.

#### `post_agent_message(task_id, cycle, from_role, to_role, msg_type, content)`

Post a message from one agent role to another.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | The task this message relates to |
| `cycle` | `int` | The dev-test cycle number |
| `from_role` | `str` | Sending role (e.g., `"developer"`, `"tester"`) |
| `to_role` | `str` | Receiving role |
| `msg_type` | `str` | Message type (e.g., `"test_results"`, `"feedback"`) |
| `content` | `str` | Message content (JSON or plain text) |

#### `read_agent_messages(task_id, to_role, max_cycle)`

Read unread messages for a given role.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | The task to read messages for |
| `to_role` | `str` | The role receiving messages |
| `max_cycle` | `int` | Maximum cycle number to include |

**Returns:** `list[dict]` — List of message records ordered by cycle and ID.

#### `mark_messages_read(task_id, to_role, cycle_number)`

Mark messages as read up to the given cycle.

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | The task ID |
| `to_role` | `str` | The role whose messages to mark read |
| `cycle_number` | `int` | Mark all messages up to this cycle |

#### `format_messages_for_prompt(messages)`

Format message records into a string suitable for injection into agent system prompts.

| Parameter | Type | Description |
|---|---|---|
| `messages` | `list[dict]` | Messages from `read_agent_messages()` |

**Returns:** `str` — Formatted messages block, or empty string if no messages.

---

### Agent Actions Logging

#### `classify_error(error_text)`

Classify an error string into a known category.

| Parameter | Type | Description |
|---|---|---|
| `error_text` | `str` | Raw error output text |

**Returns:** `str` — One of: `"timeout"`, `"file_not_found"`, `"permission"`, `"syntax"`, `"import"`, `"test_failure"`, `"unknown"`, or `""` for empty input.

#### `bulk_log_agent_actions(action_log, task_id, run_id, cycle, role)`

Batch-insert agent action records. Never raises exceptions.

| Parameter | Type | Description |
|---|---|---|
| `action_log` | `list` | List of action records |
| `task_id` | `int` | Associated task |
| `run_id` | `str` | Run identifier |
| `cycle` | `int` | Cycle number |
| `role` | `str` | Agent role |

---

### Loop Detection — `LoopDetector` class

Detects when agents are stuck in repetitive loops.

#### `LoopDetector(warn_threshold=3, terminate_threshold=5)` (inferred)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `warn_threshold` | `int` | `3` (inferred) | Consecutive identical fingerprints before warning |
| `terminate_threshold` | `int` | `5` (inferred) | Consecutive identical fingerprints before termination |

#### `record(result)` → `str`

Record a tool result and return loop status.

**Returns:** One of `"ok"`, `"warn"`, or `"terminate"`.

#### `warning_message()` → `str`

Human-readable warning message when a loop is detected.

#### `termination_summary()` → `str`

Human-readable summary when a loop triggers termination.

---

### Lesson Injection System

#### `get_relevant_lessons(role, error_type, limit)` — `forgesmith.py`

Retrieve lessons learned from past agent runs.

| Parameter | Type | Description |
|---|---|---|
| `role` | `str` | Agent role to filter lessons for |
| `error_type` | `str` | Error category to match (optional) |
| `limit` | `int` | Maximum number of lessons to return |

**Returns:** `list[dict]` — Relevant lessons sorted by relevance.

#### `format_lessons_for_injection(lessons)` — `forge_orchestrator.py`

Format lessons into a prompt-injectable string wrapped in `<task_input>` tags.

| Parameter | Type | Description |
|---|---|---|
| `lessons` | `list[dict]` | Lessons from `get_relevant_lessons()` |

**Returns:** `str` — Sanitized, formatted lessons text.

---

### Episode Memory System

#### `format_episodes_for_injection(episodes)` — `forge_orchestrator.py`

Format past agent episodes for injection into agent context.

| Parameter | Type | Description |
|---|---|---|
| `episodes` | `list[dict]` | Episode records from the database |

**Returns:** `str` — Formatted episode context.

#### `update_episode_q_values(injected_episode_ids, task_succeeded)` — `forge_orchestrator.py`

Update Q-values for episodes that were injected into a task's context, based on whether the task succeeded.

| Parameter | Type | Description |
|---|---|---|
| `injected_episode_ids` | `list[int]` | IDs of episodes that were injected |
| `task_succeeded` | `bool` | Whether the task using these episodes succeeded |

---

### Lesson Sanitizer — `lesson_sanitizer.py`

Security layer that sanitizes lessons before injection into agent prompts.

#### `sanitize_lesson_content(text)` → `str`

Strips XML injection tags, role override phrases, base64 payloads, ANSI escapes, dangerous code blocks, and enforces length limits.

#### `validate_lesson_structure(text)` → `bool`

Returns `True` if the lesson text has a valid structure for injection.

#### `wrap_lessons_in_task_input(lessons_text)` → `str`

Wraps sanitized lessons in `<task_input>` tags for safe prompt injection.

---

### Rubric Quality Scoring — `rubric_quality_scorer.py`

#### `score_agent_output(result_text, files_changed, role)` → `dict`

Score agent output across five quality dimensions.

| Parameter | Type | Description |
|---|---|---|
| `result_text` | `str` | Raw agent output text |
| `files_changed` | `list[str]` | List of files modified by the agent |
| `role` | `str` | Agent role (`"developer"`, `"tester"`, `"security_reviewer"`) |

**Returns:**

```json
{
  "total_score": 35,
  "normalized_score": 0.7,
  "max_possible": 50,
  "dimensions": {
    "naming_consistency": 7,
    "code_structure": 8,
    "test_coverage": 6,
    "documentation": 7,
    "error_handling": 7
  },
  "details": {
    "matched_patterns": ["..."]
  }
}
```

**(inferred)** All five dimensions score 0–10, max possible is 50.

---

### SARIF Helpers — `skills/security/static-analysis/skills/sarif-parsing/resources/sarif_helpers.py`

Utility library for parsing and analyzing SARIF (Static Analysis Results Interchange Format) files.

#### Key Functions

| Function | Description |
|---|---|
| `load_sarif(path)` | Load a SARIF file from disk |
| `save_sarif(sarif, path, indent)` | Write SARIF data to disk |
| `extract_findings(sarif)` | Extract all findings as `Finding` objects |
| `filter_by_level(findings, *levels)` | Filter findings by severity level |
| `filter_by_file(findings, pattern)` | Filter findings by file path pattern |
| `filter_by_rule(findings, *rule_ids)` | Filter findings by rule ID |
| `group_by_file(findings)` | Group findings by file path |
| `group_by_rule(findings)` | Group findings by rule ID |
| `deduplicate(findings)` | Remove duplicate findings by fingerprint |
| `merge_sarif_files(*paths)` | Merge multiple SARIF files into one |
| `summary(findings)` | Generate a summary string of findings |
| `to_csv_rows(findings)` | Convert findings to CSV-compatible rows |

---

### Ollama Agent — `ollama_agent.py`

Local agent interface for Ollama-hosted models with sandboxed tool execution.

#### `ollama_chat(base_url, model, messages, tools, timeout)`

Send a chat completion request to a local Ollama instance.

| Parameter | Type | Description |
|---|---|---|
| `base_url` | `str` | Ollama server URL (e.g., `http://localhost:11434`) |
| `model` | `str` | Model name (e.g., `qwen2.5-coder:32b`) |
| `messages` | `list[dict]` | Conversation messages |
| `tools` | `list[dict]` | Tool definitions |
| `timeout` | `int` | Request timeout in seconds |

#### `check_ollama_health(base_url)` → `bool`

Check if the Ollama server is reachable.

#### `list_ollama_models(base_url)` → `list`

List available models on the Ollama server.

#### Sandboxed Tool Execution

| Function | Description |
|---|---|
| `exec_read_file(project_dir, args)` | Read a file within the project sandbox |
| `exec_list_directory(project_dir, args)` | List directory contents |
| `exec_search_files(project_dir, args)` | Search for files by name |
| `exec_grep(project_dir, args)` | Search file contents |
| `exec_bash(project_dir, args, allow_write)` | Execute bash commands (with write protection) |
| `exec_write_file(project_dir, args)` | Write a file (sandboxed) |
| `exec_edit_file(project_dir, args)` | Edit a file (sandboxed) |

All file operations are sandboxed via `safe_path()` which prevents directory traversal. Write commands are validated via `is_blocked_command()`.

---

## Database Schema (Key Tables — Inferred)

The SQLite database (30+ tables) serves as the central state store. Key tables include:

| Table | Purpose (inferred) |
|---|---|
| `tasks` | Task definitions, status, assignments |
| `projects` | Project metadata and configuration |
| `agent_episodes` | Historical agent run data with Q-values |
| `agent_lessons` | Lessons learned from past runs |
| `agent_messages` | Inter-agent communication messages |
| `agent_actions` | Logged agent tool invocations |
| `rubric_scores` | Quality scores for agent outputs |
| `simba_rules` | SIMBA-generated behavioral rules |
| `forgesmith_changes` | Configuration change history |
| `forgesmith_runs` | Forgesmith analysis run logs |
| `schema_migrations` | Database migration tracking |
| `prompt_versions` | Prompt evolution history (inferred) |

Database migrations are managed by `db_migrate.py` and progress through versions v0 → v1 → v2 → v3 → v4.

---

## Error Handling

### Agent Error Classification

Errors from agent runs are classified into categories:

| Category | Pattern (inferred) |
|---|---|
| `timeout` | Command exceeded time limit |
| `file_not_found` | Referenced file does not exist |
| `permission` | Permission denied errors |
| `syntax` | Syntax errors in generated code |
| `import` | Missing module/import errors |
| `test_failure` | Test assertions failed |
| `unknown` | Unclassifiable errors |

### Early Termination Conditions

The orchestrator terminates agent runs when:

| Condition | Behavior |
|---|---|
| **Consecutive loop** | Same tool fingerprint repeated N times → warn then terminate |
| **Alternating pattern** | Two-tool alternation detected at 6 cycles → terminate |
| **Monologue detection** | 3+ consecutive text-only responses (no tool use) → terminate |
| **Stuck phrases** | Known stuck phrases detected in output (case-insensitive) |
| **Cost breaker** | Cumulative cost exceeds limit (scales with complexity) |
| **Budget exhaustion** | Turn count exceeds max_turns with periodic warnings |

### Budget Awareness Messages (inferred)

| Trigger | Message Type |
|---|---|
| Periodic interval | Remaining turns notification |
| 50% turns used | Halfway warning |
| ~80%+ turns used | Critical warning |

---

## Configuration

### Dispatch Configuration

Loaded via `load_dispatch_config(filepath)`. Controls:

- Per-role model selection
- Per-role turn limits
- Provider selection (Claude API vs Ollama)
- Task type routing and prompts
- Cost limits per complexity tier
- Concurrency settings

### Forgesmith Configuration

Loaded via `load_config()`. Controls:

- Lookback windows for analysis
- Thresholds for change proposals
- Rubric definitions and weights
- SIMBA/GEPA parameters
- Backup retention settings

---

## Adding HTTP API Endpoints

This project currently has **no HTTP API endpoints**. If you need to expose EQUIPA's functionality via a web API, consider:

1. **FastAPI wrapper**: Create a `forge_api.py` that wraps the orchestrator and database functions:
   ```python
   from fastapi import FastAPI
   app = FastAPI()
   
   @app.get("/tasks/{task_id}")
   async def get_task(task_id: int):
       return fetch_task(task_id)
   
   @app.post("/tasks/{task_id}/dispatch")
   async def dispatch_task(task_id: int):
       # Trigger orchestrator for this task
       ...
   ```

2. **MCP Server**: The project already generates MCP configuration via `step_generate_mcp_config()` in `equipa_setup.py`, suggesting Model Context Protocol integration is a supported pattern.

3. **SQLite direct access**: For read-only dashboards, query the SQLite database directly using the schema documented above.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
