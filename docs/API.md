# API.md — Itzamna / ForgeTeam

## Table of Contents

- [API.md — Itzamna / ForgeTeam](#apimd-itzamna-forgeteam)
  - [Overview](#overview)
  - [Architecture](#architecture)
  - [CLI Entry Points](#cli-entry-points)
    - [forge_orchestrator.py](#forge_orchestratorpy)
- [Run a single task](#run-a-single-task)
- [Run multiple tasks in parallel](#run-multiple-tasks-in-parallel)
- [Auto-dispatch across projects](#auto-dispatch-across-projects)
- [Plan from a goal](#plan-from-a-goal)
- [Run parallel goals from a file](#run-parallel-goals-from-a-file)
- [Setup repositories](#setup-repositories)
    - [forgesmith.py](#forgesmithpy)
- [Full analysis + apply changes](#full-analysis-apply-changes)
- [Dry run — analyze without applying](#dry-run-analyze-without-applying)
- [Propose-only mode (OPRO prompt optimization)](#propose-only-mode-opro-prompt-optimization)
- [Generate report](#generate-report)
- [Rollback a previous change](#rollback-a-previous-change)
    - [forgesmith_simba.py](#forgesmith_simbapy)
- [Run SIMBA rule generation](#run-simba-rule-generation)
- [Dry run](#dry-run)
- [Filter by role](#filter-by-role)
    - [forgesmith_gepa.py](#forgesmith_gepapy)
- [Run GEPA prompt evolution](#run-gepa-prompt-evolution)
- [Dry run](#dry-run)
- [Filter by role](#filter-by-role)
    - [forge_arena.py](#forge_arenapy)
    - [forge_dashboard.py](#forge_dashboardpy)
    - [analyze_performance.py](#analyze_performancepy)
- [Full report](#full-report)
- [Filter by project](#filter-by-project)
- [Limit time range](#limit-time-range)
    - [db_migrate.py](#db_migratepy)
- [Run all pending migrations](#run-all-pending-migrations)
- [Silent mode](#silent-mode)
    - [itzamna_setup.py](#itzamna_setuppy)
  - [Internal Python APIs](#internal-python-apis)
    - [Database Connection](#database-connection)
- [forge_orchestrator.py](#forge_orchestratorpy)
    - [Task Management](#task-management)
    - [Agent Messaging System](#agent-messaging-system)
    - [Agent Actions (Telemetry)](#agent-actions-telemetry)
    - [Agent Episodes (Reinforcement Learning Memory)](#agent-episodes-reinforcement-learning-memory)
    - [Lessons Learned System](#lessons-learned-system)
    - [Loop Detection](#loop-detection)
    - [Early Termination](#early-termination)
- [Constants (inferred from tests)](#constants-inferred-from-tests)
    - [Ollama Integration (Local LLM Client)](#ollama-integration-local-llm-client)
    - [File Operations (Sandboxed)](#file-operations-sandboxed)
    - [Rubric Scoring](#rubric-scoring)
    - [SIMBA Rules Engine](#simba-rules-engine)
    - [SARIF Security Helpers](#sarif-security-helpers)
    - [Database Migration](#database-migration)
  - [Error Handling](#error-handling)
    - [Error Classification](#error-classification)
    - [Loop Detection States](#loop-detection-states)
    - [Agent Retry Strategy](#agent-retry-strategy)
  - [Database Schema](#database-schema)
  - [Configuration](#configuration)
    - [Dispatch Configuration](#dispatch-configuration)
  - [Adding API Endpoints](#adding-api-endpoints)
  - [Related Documentation](#related-documentation)

## Overview

**ForgeTeam** is a multi-agent AI orchestration platform for Claude Code. It is **not a web API service** — it is a collection of CLI tools, orchestration scripts, and SQLite-backed utilities that coordinate AI agents for software development tasks.

There are **no HTTP/REST API endpoints, GraphQL schemas, or tRPC procedures** detected in this project. ForgeTeam operates through:

- **CLI entry points** (Python scripts invoked from the command line)
- **SQLite database** for persistent state, episodes, lessons, and agent messages
- **Inter-agent messaging** via database-backed message passing
- **Ollama integration** for local LLM inference (HTTP client, not server)

This document describes the **internal programmatic interfaces** — the key function-level APIs that developers integrating with or extending ForgeTeam will use.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 CLI Entry Points                 │
│  forge_orchestrator.py  forgesmith.py  etc.      │
├─────────────────────────────────────────────────┤
│              Internal Python APIs                │
│  Orchestration │ Episodes │ Lessons │ Messaging  │
├─────────────────────────────────────────────────┤
│              SQLite Database Layer               │
│  Tasks │ Projects │ Agent Episodes │ Actions     │
└─────────────────────────────────────────────────┘
```

---

## CLI Entry Points

### forge_orchestrator.py

The primary orchestrator for dispatching tasks to AI agents.

```bash
# Run a single task
python forge_orchestrator.py --task <task_id>

# Run multiple tasks in parallel
python forge_orchestrator.py --tasks "101,102,103"

# Auto-dispatch across projects
python forge_orchestrator.py --dispatch

# Plan from a goal
python forge_orchestrator.py --goal "Implement user authentication"

# Run parallel goals from a file
python forge_orchestrator.py --goals-file goals.json

# Setup repositories
python forge_orchestrator.py --setup-repos
```

| Argument | Description |
|---|---|
| `--task <id>` | Run a single task by ID |
| `--tasks <ids>` | Comma-separated task IDs for parallel execution |
| `--dispatch` | Auto-dispatch mode — scans pending work and dispatches |
| `--goal <text>` | Plan and execute a single goal |
| `--goals-file <path>` | JSON file with multiple goals for parallel execution |
| `--setup-repos` | Initialize git repositories for projects |
| `--dry-run` | (inferred) Preview without making changes |

---

### forgesmith.py

The self-improvement engine — analyzes agent performance and evolves configuration.

```bash
# Full analysis + apply changes
python forgesmith.py

# Dry run — analyze without applying
python forgesmith.py --dry-run

# Propose-only mode (OPRO prompt optimization)
python forgesmith.py --propose-only

# Generate report
python forgesmith.py --report

# Rollback a previous change
python forgesmith.py --rollback <run_id>
```

| Argument | Description |
|---|---|
| `--dry-run` | Analyze and propose changes without applying (inferred) |
| `--propose-only` | Run OPRO prompt optimization proposals only (inferred) |
| `--report` | Generate performance report (inferred) |
| `--rollback <run_id>` | Revert changes from a specific run (inferred) |

---

### forgesmith_simba.py

SIMBA (Self-Improving Model-Based Advisor) — generates rules from agent episode data.

```bash
# Run SIMBA rule generation
python forgesmith_simba.py

# Dry run
python forgesmith_simba.py --dry-run

# Filter by role
python forgesmith_simba.py --role developer
```

| Argument | Description |
|---|---|
| `--dry-run` | Generate rules without storing (inferred) |
| `--role <role>` | Only generate rules for a specific role (inferred) |

---

### forgesmith_gepa.py

GEPA (Generative Episode Prompt Adaptation) — evolves system prompts using DSPy-style optimization.

```bash
# Run GEPA prompt evolution
python forgesmith_gepa.py

# Dry run
python forgesmith_gepa.py --dry-run

# Filter by role
python forgesmith_gepa.py --role developer
```

| Argument | Description |
|---|---|
| `--dry-run` | Evolve prompts without storing (inferred) |
| `--role <role>` | Only evolve prompts for a specific role (inferred) |

---

### forge_arena.py

Adversarial self-play arena for testing and improving agent capabilities.

```bash
python forge_arena.py
```

---

### forge_dashboard.py

Performance dashboard and reporting.

```bash
python forge_dashboard.py
```

---

### analyze_performance.py

Detailed performance analysis and reporting.

```bash
# Full report
python analyze_performance.py

# Filter by project
python analyze_performance.py --project <project_id>

# Limit time range
python analyze_performance.py --days 30
```

| Argument | Description |
|---|---|
| `--project <id>` | Filter analysis to a specific project (inferred) |
| `--days <n>` | Limit analysis to the last N days (inferred) |

---

### db_migrate.py

Database schema migration tool.

```bash
# Run all pending migrations
python db_migrate.py

# Silent mode
python db_migrate.py --silent
```

---

### itzamna_setup.py

Guided interactive setup wizard.

```bash
python itzamna_setup.py
```

---

## Internal Python APIs

### Database Connection

All modules use a consistent SQLite connection pattern.

```python
# forge_orchestrator.py
def get_db_connection(write: bool) -> sqlite3.Connection
```

| Parameter | Type | Description |
|---|---|---|
| `write` | `bool` | If `True`, opens with write access; `False` for read-only |

**Returns:** `sqlite3.Connection`

---

### Task Management

#### Fetch a Task

```python
def fetch_task(task_id: int) -> dict
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | The task ID to retrieve |

**Returns:** Task dictionary with fields including `id`, `title`, `description`, `status`, `project_id`, `priority`, `complexity` (inferred).

#### Fetch Next TODO Task

```python
def fetch_next_todo(project_id: int) -> dict | None
```

| Parameter | Type | Description |
|---|---|---|
| `project_id` | `int` | Project to search for pending tasks |

**Returns:** Next unstarted task or `None`.

#### Update Task Status

```python
def update_task_status(task_id: int, outcome: str, output: str) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | Task to update |
| `outcome` | `str` | Result status (e.g., `"done"`, `"failed"`, `"blocked"`) (inferred) |
| `output` | `str` | Agent output text |

---

### Agent Messaging System

Inter-agent communication via database-backed message passing.

#### Post a Message

```python
def post_agent_message(
    task_id: int, cycle: int, from_role: str,
    to_role: str, msg_type: str, content: str
) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | Associated task |
| `cycle` | `int` | Dev-test cycle number |
| `from_role` | `str` | Sender role (e.g., `"developer"`, `"tester"`) |
| `to_role` | `str` | Recipient role |
| `msg_type` | `str` | Message type (e.g., `"test_results"`, `"feedback"`) (inferred) |
| `content` | `str` | Message body (may be JSON) |

#### Read Messages

```python
def read_agent_messages(
    task_id: int, to_role: str, max_cycle: int
) -> list[dict]
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | Associated task |
| `to_role` | `str` | Recipient role to filter by |
| `max_cycle` | `int` | Only return messages up to this cycle |

**Returns:** List of unread message dicts, ordered by cycle and ID.

#### Mark Messages Read

```python
def mark_messages_read(
    task_id: int, to_role: str, cycle_number: int
) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | Associated task |
| `to_role` | `str` | Role whose messages to mark |
| `cycle_number` | `int` | Mark messages read up to this cycle |

#### Format Messages for Prompt Injection

```python
def format_messages_for_prompt(messages: list[dict]) -> str
```

**Returns:** Formatted string suitable for including in agent system prompts.

---

### Agent Actions (Telemetry)

#### Log an Action

```python
def log_agent_action(action: dict) -> None  # (inferred signature)
```

#### Bulk Log Actions

```python
def bulk_log_agent_actions(
    action_log: list, task_id: int,
    run_id: str, cycle: int, role: str
) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `action_log` | `list` | List of action records |
| `task_id` | `int` | Associated task |
| `run_id` | `str` | Unique run identifier |
| `cycle` | `int` | Dev-test cycle number |
| `role` | `str` | Agent role |

*This function is designed to never crash — errors are silently caught.* (inferred)

#### Get Action Summary

```python
def get_action_summary(task_id: int, cycle: int = None) -> dict
```

| Parameter | Type | Description |
|---|---|---|
| `task_id` | `int` | Task to summarize |
| `cycle` | `int \| None` | Specific cycle, or all cycles if `None` |

**Returns:** Summary dict with action counts and statistics (inferred).

#### Classify Errors

```python
def classify_error(error_text: str) -> str
```

| Parameter | Type | Description |
|---|---|---|
| `error_text` | `str` | Raw error output |

**Returns:** One of: `"timeout"`, `"file_not_found"`, `"permission"`, `"syntax"`, `"import"`, `"test_failure"`, `"unknown"` (inferred from tests).

---

### Agent Episodes (Reinforcement Learning Memory)

#### Record an Episode

```python
def record_agent_episode(
    task: dict, result: str, outcome: str,
    role: str, output: str
) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `task` | `dict` | Task object |
| `result` | `str` | Agent result text |
| `outcome` | `str` | Success/failure outcome |
| `role` | `str` | Agent role |
| `output` | `str` | Full agent output |

#### Get Relevant Episodes for Injection

```python
def get_relevant_episodes(
    role: str, project_id: int = None,
    limit: int = 5
) -> list[dict]  # (inferred signature)
```

Episodes are filtered by Q-value and optionally by project, with cross-project fallback (inferred from tests).

#### Format Episodes for Injection

```python
def format_episodes_for_injection(episodes: list[dict]) -> str
```

**Returns:** Formatted string for system prompt injection.

#### Update Q-Values

```python
def update_episode_q_values(
    injected_episode_ids: list[int],
    task_succeeded: bool
) -> None
```

| Parameter | Type | Description |
|---|---|---|
| `injected_episode_ids` | `list[int]` | Episode IDs that were injected into the prompt |
| `task_succeeded` | `bool` | Whether the task using these episodes succeeded |

Q-values are bounded (inferred from tests) and updated based on task outcome.

---

### Lessons Learned System

#### Get Relevant Lessons

```python
def get_relevant_lessons(
    role: str, error_type: str = None,
    limit: int = 5
) -> list[dict]
```

| Parameter | Type | Description |
|---|---|---|
| `role` | `str` | Agent role to find lessons for |
| `error_type` | `str \| None` | Optional error type filter |
| `limit` | `int` | Maximum lessons to return |

**Returns:** List of active lesson dicts, filtered by role and optionally error type.

#### Format Lessons for Injection

```python
def format_lessons_for_injection(lessons: list[dict]) -> str
```

**Returns:** Formatted string for system prompt injection.

#### Update Lesson Injection Count

```python
def update_lesson_injection_count(lesson_ids: list[int]) -> None
```

Tracks how many times each lesson has been injected.

---

### Loop Detection

The `LoopDetector` class prevents agents from getting stuck in repetitive behavior.

```python
class LoopDetector:
    def __init__(self, warn_threshold: int = 3, terminate_threshold: int = 5):
        ...

    def record(self, result: dict) -> str:
        """Returns 'ok', 'warn', or 'terminate'"""

    def warning_message(self) -> str:
        """Human-readable warning about detected loop"""

    def termination_summary(self) -> str:
        """Summary when terminating due to loop"""
```

| Method | Returns | Description |
|---|---|---|
| `record(result)` | `"ok"` \| `"warn"` \| `"terminate"` | Record a tool result and check for loops |
| `warning_message()` | `str` | Warning text when loop detected |
| `termination_summary()` | `str` | Summary text when loop causes termination |

The detector fingerprints results, tracks repeated identical fingerprints, and resets when files change (inferred from tests).

---

### Early Termination

```python
# Constants (inferred from tests)
STUCK_PHRASES: list[str]  # Non-empty list of phrases indicating stuck agents
EXEMPT_ROLES: set[str]    # Roles exempt from early termination
```

#### Detect Stuck Phrases

```python
def detect_stuck_phrases(text: str) -> str | None  # (inferred)
```

Case-insensitive detection. Returns the matched phrase or `None`.

#### Detect Repeated Tools

```python
def detect_repeated_tools(
    history: list, window: int = 4
) -> bool  # (inferred)
```

Detects when the last N tool calls are identical.

---

### Ollama Integration (Local LLM Client)

#### Health Check

```python
def check_ollama_health(base_url: str) -> bool
```

| Parameter | Type | Description |
|---|---|---|
| `base_url` | `str` | Ollama server URL (e.g., `http://localhost:11434`) |

**Returns:** `True` if Ollama is responsive.

#### List Models

```python
def list_ollama_models(base_url: str) -> list
```

**Returns:** List of available models on the Ollama server.

#### Chat Completion

```python
def ollama_chat(
    base_url: str, model: str, messages: list,
    tools: list = None, timeout: int = None
) -> dict
```

| Parameter | Type | Description |
|---|---|---|
| `base_url` | `str` | Ollama server URL |
| `model` | `str` | Model name |
| `messages` | `list` | Chat messages in OpenAI format (inferred) |
| `tools` | `list \| None` | Tool definitions for function calling (inferred) |
| `timeout` | `int \| None` | Request timeout in seconds |

**Returns:** Chat completion response dict (inferred).

---

### File Operations (Sandboxed)

All file operations in `ollama_agent.py` are sandboxed to a project directory.

```python
def safe_path(project_dir: str, relative_path: str) -> str
```

Validates that the resolved path stays within `project_dir`.

| Function | Description |
|---|---|
| `exec_read_file(project_dir, args)` | Read a file's contents |
| `exec_list_directory(project_dir, args)` | List directory contents |
| `exec_search_files(project_dir, args)` | Search for files by pattern |
| `exec_grep(project_dir, args)` | Grep for text in files |
| `exec_bash(project_dir, args, allow_write)` | Execute a bash command |
| `exec_write_file(project_dir, args)` | Write content to a file |
| `exec_edit_file(project_dir, args)` | Edit a file with patches |

```python
def is_safe_read_command(command: str) -> bool
def is_blocked_command(command: str) -> bool
```

---

### Rubric Scoring

```python
def compute_rubric_score(run: dict, cfg: dict) -> dict
```

| Parameter | Type | Description |
|---|---|---|
| `run` | `dict` | Agent run data |
| `cfg` | `dict` | Configuration with rubric definitions |

**Returns:** Scored rubric dict (inferred).

```python
def score_completed_runs(runs: list, cfg: dict) -> list
def analyze_rubric_correlations(cfg: dict) -> dict
def evolve_rubric_weights(cfg: dict) -> None
def get_rubric_report(role: str, limit: int) -> dict
```

---

### SIMBA Rules Engine

```python
def find_high_variance_episodes(lookback_days: int) -> list
def find_hardest_cases(lookback_days: int) -> list
def get_existing_simba_rules(role: str = None) -> list
def build_simba_prompt(role, successes, failures, hardest_cases, existing_rules) -> str
def call_claude_for_rules(prompt: str, cfg: dict) -> list
def validate_rule(rule: dict, existing_rules: list) -> tuple  # (inferred)
def store_rules(role: str, rules: list, dry_run: bool) -> None
def evaluate_simba_rules() -> dict
def prune_stale_rules(dry_run: bool) -> None
def run_simba(cfg: dict, dry_run: bool, role_filter: str = None) -> dict
```

#### Rule Validation

Rules are validated for:
- Minimum and maximum length (inferred)
- Valid error types (inferred)
- Non-empty content (inferred)
- Duplicate detection via similarity checking (inferred)
- Must be a `dict` type (inferred)

---

### SARIF Security Helpers

Utility library for parsing SARIF (Static Analysis Results Interchange Format) files.

```python
from sarif_helpers import *
```

#### Loading & Saving

```python
def load_sarif(path: str) -> dict
def save_sarif(sarif: dict, path: str, indent: int = 2) -> None
```

#### Querying Findings

```python
def extract_findings(sarif: dict) -> list[Finding]
def filter_by_level(findings: list, *levels: str) -> list
def filter_by_file(findings: list, pattern: str) -> list
def filter_by_rule(findings: list, *rule_ids: str) -> list
def sort_by_severity(findings: list, reverse: bool = True) -> list
```

#### Grouping & Counting

```python
def group_by_file(findings: list) -> dict[str, list]
def group_by_rule(findings: list) -> dict[str, list]
def count_by_level(findings: list) -> dict[str, int]
def count_by_rule(findings: list) -> dict[str, int]
```

#### Deduplication & Merging

```python
def deduplicate(findings: list) -> list
def merge_sarif_files(*paths: str) -> dict
def compute_fingerprint(result: dict, include_message: bool = False) -> str
```

#### Export

```python
def to_csv_rows(findings: list) -> list[list]
def summary(findings: list) -> str
```

---

### Database Migration

```python
def run_migrations(db_path: str, silent: bool = False) -> None
```

| Migration | Description |
|---|---|
| `v0 → v1` | Initial schema setup (inferred) |
| `v1 → v2` | Schema additions (inferred) |
| `v2 → v3` | Latest schema changes (inferred) |

Migrations are tracked in a `schema_migrations` table. Database is automatically backed up before migration.

---

## Error Handling

### Error Classification

The system classifies errors into known categories for pattern analysis:

| Category | Trigger Pattern (inferred) |
|---|---|
| `timeout` | Timeout-related errors |
| `file_not_found` | Missing file errors |
| `permission` | Permission denied errors |
| `syntax` | Syntax errors in code |
| `import` | Import/module errors |
| `test_failure` | Test assertion failures |
| `unknown` | All other errors |

### Loop Detection States

| State | Meaning |
|---|---|
| `ok` | No loop detected, continue normally |
| `warn` | Potential loop detected (default: 3 identical fingerprints), warning injected |
| `terminate` | Confirmed loop (default: 5 identical fingerprints), agent is terminated |

Warning is issued only once per loop. Counter resets when files change (new files modified) or when a different fingerprint is recorded.

### Agent Retry Strategy

```python
async def run_agent_with_retries(
    cmd: str, task: dict, max_retries: int
) -> str
```

Agents are retried with checkpoint recovery on failure. Checkpoints save intermediate state:

```python
def save_checkpoint(task_id, attempt, output_text, role) -> None
def load_checkpoint(task_id, role) -> str | None
def clear_checkpoints(task_id, role) -> None
```

---

## Database Schema

The SQLite database contains the following tables (inferred from code):

| Table | Purpose |
|---|---|
| `tasks` | Task definitions, status, and metadata |
| `projects` | Project information and directories |
| `agent_episodes` | Reinforcement learning memory with Q-values |
| `agent_messages` | Inter-agent communication |
| `agent_actions` | Telemetry and action logging |
| `lessons` | Extracted lessons with injection tracking |
| `rubric_scores` | Performance scoring results |
| `rubric_evolution` | Rubric weight evolution history (inferred) |
| `schema_migrations` | Migration tracking |
| `simba_rules` | SIMBA-generated rules (inferred) |
| `forgesmith_runs` | Self-improvement run history (inferred) |
| `prompt_versions` | GEPA prompt evolution history (inferred) |

---

## Configuration

Configuration is loaded from a config file (inferred):

```python
def load_config() -> dict   # forgesmith.py
def load_config() -> dict   # forge_orchestrator.py
```

### Dispatch Configuration

```python
def load_dispatch_config(filepath: str) -> dict
```

Controls how tasks are dispatched across projects, including:
- Provider selection per role (inferred)
- Model selection per role (inferred)
- Task type routing with role-specific prompts (inferred from tests)
- Ollama base URL configuration (inferred)

---

## Adding API Endpoints

This project currently operates as a CLI toolkit. To add HTTP API endpoints, consider:

1. **FastAPI wrapper**: Create `api.py` with FastAPI routes wrapping existing functions
   ```python
   from fastapi import FastAPI
   app = FastAPI()

   @app.get("/tasks/{task_id}")
   async def get_task(task_id: int):
       return fetch_task(task_id)

   @app.post("/tasks/{task_id}/dispatch")
   async def dispatch(task_id: int):
       # Wrap dispatch logic
       ...
   ```

2. **MCP Server**: The project already references MCP configuration (`step_generate_mcp_config`), suggesting Model Context Protocol integration is planned or available.

3. **Dashboard API**: `forge_dashboard.py` contains query functions that could be exposed as read-only endpoints.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
