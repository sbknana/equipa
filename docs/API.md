# API.md — EQUIPA

## Table of Contents

- [API.md — EQUIPA](#apimd-equipa)
  - [Overview](#overview)
  - [MCP Tools (Claude's Interface)](#mcp-tools-claudes-interface)
    - [**initialize** — MCP handshake](#initialize-mcp-handshake)
    - [**tools/list** — enumerate available tools](#toolslist-enumerate-available-tools)
    - [**task_status** — query task state](#task_status-query-task-state)
    - [**task_create** — add new task to queue](#task_create-add-new-task-to-queue)
    - [**dispatch** — start agent execution](#dispatch-start-agent-execution)
    - [**lessons** — retrieve knowledge base entries](#lessons-retrieve-knowledge-base-entries)
    - [**agent_logs** — retrieve agent execution history](#agent_logs-retrieve-agent-execution-history)
    - [**session_notes** — retrieve session journal](#session_notes-retrieve-session-journal)
    - [**project_context** — load project README + recent tasks](#project_context-load-project-readme-recent-tasks)
  - [CLI Tools (Human Interface)](#cli-tools-human-interface)
    - [**python -m equipa.cli** — main CLI entry point](#python-m-equipacli-main-cli-entry-point)
    - [**dispatch** — run a task](#dispatch-run-a-task)
    - [**status** — check task state](#status-check-task-state)
    - [**auto-dispatch** — run pending tasks in queue](#auto-dispatch-run-pending-tasks-in-queue)
    - [**lessons** — query knowledge base](#lessons-query-knowledge-base)
    - [**nightly-review** — generate portfolio report](#nightly-review-generate-portfolio-report)
  - [Python API (Importable Modules)](#python-api-importable-modules)
    - [**equipa.tasks** — task database queries](#equipatasks-task-database-queries)
    - [**equipa.db** — database utilities](#equipadb-database-utilities)
    - [**equipa.lessons** — knowledge base retrieval](#equipalessons-knowledge-base-retrieval)
    - [**equipa.prompts** — prompt construction](#equipaprompts-prompt-construction)
    - [**equipa.monitoring** — loop detection](#equipamonitoring-loop-detection)
  - [Error Handling](#error-handling)
    - [**Task already running**](#task-already-running)
    - [**Agent stuck in loop**](#agent-stuck-in-loop)
    - [**Cost breaker triggered**](#cost-breaker-triggered)
    - [**Preflight check failed**](#preflight-check-failed)
  - [Rate Limiting](#rate-limiting)
  - [Known Limitations](#known-limitations)
  - [Getting Started](#getting-started)
  - [Advanced: Custom Agent Roles](#advanced-custom-agent-roles)
  - [Contributing](#contributing)
  - [Support](#support)
  - [Related Documentation](#related-documentation)

## Overview

EQUIPA does not expose a traditional REST or GraphQL API. It is a **conversational orchestration platform** — you talk to Claude in plain English, and Claude dispatches EQUIPA tasks behind the scenes. The primary interface is the **MCP (Model Context Protocol) server**, which Claude uses to interact with EQUIPA's task system, knowledge base, and monitoring stack.

For automation or scripting, EQUIPA includes a CLI and Python-importable modules. Most users never touch the CLI directly — Claude handles task dispatch, error recovery, and result reporting.

**Base invocation:**
```bash
python -m equipa.cli --task-id 42
```

**MCP server (Claude's interface):**
```bash
python -m equipa.mcp_server
```

The MCP server runs on stdio (no HTTP listener). Claude connects via the MCP protocol spec. No authentication required — local-only by design.

---

## MCP Tools (Claude's Interface)

These are the tools Claude calls to manage EQUIPA tasks. Not intended for direct human use.

### **initialize** — MCP handshake
Claude calls this on connection to announce capabilities.

**Parameters:** None (protocol handshake)

**Response:**
```json
{
  "protocolVersion": "2024-11-05",
  "serverInfo": { "name": "equipa-mcp", "version": "3.1" },
  "capabilities": { "tools": {} }
}
```

---

### **tools/list** — enumerate available tools
Returns the list of tools Claude can call.

**Response:**
```json
{
  "tools": [
    { "name": "task_status", "description": "Check task state, logs, blockers", ... },
    { "name": "task_create", ... },
    { "name": "dispatch", ... },
    ...
  ]
}
```

---

### **task_status** — query task state
Check if a task is pending, running, complete, blocked, or failed.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | yes | Task ID from TheForge database |

**Example:**
```json
{
  "name": "task_status",
  "arguments": { "task_id": 42 }
}
```

**Response:**
```json
{
  "task_id": 42,
  "status": "blocked",
  "blocker_type": "test_failure",
  "last_updated": "2026-03-15T10:23:45Z",
  "role": "developer",
  "cycle": 3,
  "cost_usd": 0.12
}
```

If task does not exist: `{"error": "Task 42 not found"}`

---

### **task_create** — add new task to queue
Create a task in TheForge database. Does **not** dispatch — just adds to backlog.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | int | yes | TheForge project ID |
| `title` | str | yes | Short task name |
| `description` | str | yes | Full task spec |
| `priority` | int | no | 1-5 (default 3) |
| `complexity` | str | no | `trivial`/`medium`/`complex` (inferred if omitted) |
| `task_type` | str | no | `feature`/`bug`/`test`/`refactor`/`security`/`build_fix` |

**Example:**
```json
{
  "name": "task_create",
  "arguments": {
    "project_id": 23,
    "title": "Add rate limiting to /api/upload",
    "description": "Implement token bucket rate limiter. 10 req/min per IP. Store state in Redis.",
    "priority": 4,
    "task_type": "feature"
  }
}
```

**Response:**
```json
{ "task_id": 104, "status": "pending" }
```

---

### **dispatch** — start agent execution
Kicks off an agent run for a task. Task must be in `pending` or `blocked` state.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | yes | Task to run |
| `role` | str | no | Force role (default: auto-select based on task type) |
| `checkpoint_id` | str | no | Resume from checkpoint (UUID) |

**Example:**
```json
{
  "name": "dispatch",
  "arguments": { "task_id": 42 }
}
```

**Response:**
```json
{
  "task_id": 42,
  "agent_id": "dev-42-1711362845",
  "role": "developer",
  "status": "running"
}
```

If task already running: `{"error": "Task 42 already in progress"}`

---

### **lessons** — retrieve knowledge base entries
Fetch lessons (episodic memories) from failed/succeeded tasks. Used by Claude to inject context before dispatching.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | str | no | Filter by agent role |
| `error_type` | str | no | Filter by error class |
| `limit` | int | no | Max results (default 10) |

**Example:**
```json
{
  "name": "lessons",
  "arguments": { "role": "tester", "limit": 5 }
}
```

**Response:**
```json
{
  "lessons": [
    {
      "lesson_id": 17,
      "content": "When pytest fails with 'fixture not found', check conftest.py scope is session-level",
      "q_value": 0.82,
      "times_injected": 12
    },
    ...
  ]
}
```

---

### **agent_logs** — retrieve agent execution history
Get the turn-by-turn tool call log for a running/completed task.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | int | yes | Task ID |
| `max_turns` | int | no | Limit turns (default 100) |

**Example:**
```json
{
  "name": "agent_logs",
  "arguments": { "task_id": 42, "max_turns": 10 }
}
```

**Response:**
```json
{
  "task_id": 42,
  "turns": [
    { "turn": 1, "tool": "read_file", "args": "src/app.py", "result": "...", "cost": 0.003 },
    { "turn": 2, "tool": "bash", "args": "pytest tests/", "result": "FAILED", "error": "test_upload_rate_limit failed" },
    ...
  ]
}
```

---

### **session_notes** — retrieve session journal
Fetch human-written notes from TheForge `session_notes` table.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | int | no | Filter by project |
| `limit` | int | no | Max results (default 20) |

**Example:**
```json
{
  "name": "session_notes",
  "arguments": { "project_id": 23 }
}
```

**Response:**
```json
{
  "notes": [
    { "id": 5, "created_at": "2026-03-10T14:00:00Z", "content": "Redis host changed to redis.prod.internal" },
    ...
  ]
}
```

---

### **project_context** — load project README + recent tasks
Get a snapshot of a project's state for Claude to inject into task context.

**Arguments:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | int | yes | TheForge project ID |

**Example:**
```json
{
  "name": "project_context",
  "arguments": { "project_id": 23 }
}
```

**Response:**
```json
{
  "project_id": 23,
  "name": "EQUIPA",
  "repo_path": "/home/user/projects/EQUIPA",
  "readme_summary": "Multi-agent AI orchestration platform...",
  "recent_tasks": [
    { "task_id": 101, "status": "completed", "title": "Add loop detection" },
    { "task_id": 102, "status": "blocked", "title": "Fix tester retry logic" }
  ]
}
```

---

## CLI Tools (Human Interface)

For automation/scripting. Claude does not call these — they are for cron jobs, CI/CD, or manual debugging.

### **python -m equipa.cli** — main CLI entry point

**Usage:**
```bash
python -m equipa.cli [OPTIONS] COMMAND
```

**Global Options:**
| Flag | Description |
|------|-------------|
| `--db PATH` | Override TheForge database path |
| `--dry-run` | Simulate without writing to DB |
| `--verbose` | Print debug logs |

---

### **dispatch** — run a task

```bash
python -m equipa.cli dispatch --task-id 42 [--role developer] [--checkpoint UUID]
```

**Options:**
| Flag | Description |
|------|-------------|
| `--task-id ID` | Task to run (required) |
| `--role ROLE` | Force agent role (default: auto) |
| `--checkpoint UUID` | Resume from checkpoint |

**Example:**
```bash
python -m equipa.cli dispatch --task-id 42 --role tester
```

**Output:**
```
[EQUIPA] Dispatching task 42 (Tester)
[Turn 1] read_file → src/tests/test_upload.py
[Turn 2] bash → pytest tests/test_upload.py
[Turn 3] write_file → tests/test_upload.py (added rate_limit fixture)
[COMPLETE] Task 42 succeeded in 3 turns ($0.08)
```

---

### **status** — check task state

```bash
python -m equipa.cli status --task-id 42
```

**Output:**
```json
{
  "task_id": 42,
  "status": "completed",
  "role": "tester",
  "cycles": 1,
  "turns": 3,
  "cost_usd": 0.08,
  "files_changed": ["tests/test_upload.py"],
  "started_at": "2026-03-15T10:00:00Z",
  "completed_at": "2026-03-15T10:02:14Z"
}
```

---

### **auto-dispatch** — run pending tasks in queue

```bash
python -m equipa.cli auto-dispatch [--project-id 23] [--max-concurrent 3]
```

Scans `tasks` table for `status = 'pending'`, scores by priority/complexity, dispatches top N tasks.

**Options:**
| Flag | Description |
|------|-------------|
| `--project-id ID` | Filter by project |
| `--max-concurrent N` | Run N tasks in parallel (default 3) |
| `--priority-min N` | Only tasks with priority ≥ N |

**Example:**
```bash
python -m equipa.cli auto-dispatch --project-id 23 --max-concurrent 2
```

**Output:**
```
[EQUIPA] Scanning queue...
[EQUIPA] Found 7 pending tasks
[EQUIPA] Dispatching task 42 (priority 4, complexity medium)
[EQUIPA] Dispatching task 43 (priority 3, complexity trivial)
[EQUIPA] Task 42 completed ($0.08)
[EQUIPA] Task 43 completed ($0.02)
```

---

### **lessons** — query knowledge base

```bash
python -m equipa.cli lessons [--role developer] [--error-type test_failure] [--limit 10]
```

**Example:**
```bash
python -m equipa.cli lessons --role tester --limit 5
```

**Output:**
```
Lesson 17 (q=0.82): When pytest fails with 'fixture not found', check conftest.py scope
Lesson 23 (q=0.76): If import fails in tests, ensure __init__.py exists in package
...
```

---

### **nightly-review** — generate portfolio report

```bash
python -m scripts.nightly_review [--db PATH]
```

Prints a summary of:
- Tasks completed/blocked/pending
- Agent success rates by role
- Open questions
- Stale projects (no activity >7 days)

Used by ForgeSmith nightly cron job.

**Example output:**
```
=== Portfolio Summary ===
Total tasks: 127
Completed: 89 (70%)
Blocked: 8 (6%)
Pending: 30 (24%)

=== Agent Performance ===
Developer: 85% success (102 runs)
Tester: 92% success (67 runs)
...

=== Open Questions ===
- Q: Should we migrate to PostgreSQL? (asked 2026-03-10)
- Q: Redis timeout policy? (asked 2026-03-12)

=== Stale Projects ===
- Project 18: last activity 12 days ago
```

---

## Python API (Importable Modules)

For custom integrations. Not intended for end users — advanced use only.

### **equipa.tasks** — task database queries

```python
from equipa.tasks import fetch_task, fetch_next_todo

task = fetch_task(42)
print(task["status"])  # "blocked"

next_task = fetch_next_todo(project_id=23)
print(next_task["title"])  # "Add rate limiting"
```

**Functions:**
| Function | Signature | Description |
|----------|-----------|-------------|
| `fetch_task(task_id)` | `int → dict | None` | Retrieve task by ID |
| `fetch_next_todo(project_id)` | `int → dict | None` | Get highest-priority pending task |
| `fetch_project_context(project_id)` | `int → dict` | Load project summary + recent tasks |
| `verify_task_updated(task_id)` | `int → bool` | Check if task row changed since last read |

---

### **equipa.db** — database utilities

```python
from equipa.db import get_db_connection

conn = get_db_connection(write=False)
cursor = conn.execute("SELECT * FROM tasks WHERE status = 'blocked'")
blocked = cursor.fetchall()
conn.close()
```

**Functions:**
| Function | Signature | Description |
|----------|-----------|-------------|
| `get_db_connection(write=False)` | `bool → sqlite3.Connection` | Get pooled connection |
| `ensure_schema()` | `() → None` | Create tables if missing |
| `classify_error(error_text)` | `str → str` | Map error message to category |

**Error categories (inferred):**
- `test_failure` — pytest/unittest failures
- `import_error` — ModuleNotFoundError, ImportError
- `syntax_error` — SyntaxError, IndentationError
- `file_not_found` — FileNotFoundError, missing imports
- `permission_error` — PermissionError, access denied
- `timeout` — subprocess/API timeouts
- `unknown` — unclassified errors

---

### **equipa.lessons** — knowledge base retrieval

```python
from equipa.lessons import get_relevant_lessons

lessons = get_relevant_lessons(
    role="tester",
    error_type="test_failure",
    limit=5
)
for lesson in lessons:
    print(lesson["content"])
```

**Functions:**
| Function | Signature | Description |
|----------|-----------|-------------|
| `get_relevant_lessons(role, error_type, limit)` | `str, str, int → list[dict]` | Retrieve top-N lessons by Q-value |
| `update_lesson_injection_count(lesson_ids)` | `list[int] → None` | Increment injection counters |
| `get_active_simba_rules()` | `() → list[dict]` | Load all active SIMBA rules |

---

### **equipa.prompts** — prompt construction

```python
from equipa.prompts import build_system_prompt_cache_split

result = build_system_prompt_cache_split(
    role="developer",
    task={"description": "Fix Redis timeout", "project_id": 23},
    project_context={},
    config={}
)

print(result.static)  # cacheable portion (role prompt + common rules)
print(result.dynamic)  # task-specific portion (description, lessons)
print(result.full)  # complete prompt
```

**PromptResult attributes:**
| Attribute | Type | Description |
|-----------|------|-------------|
| `.static` | str | Role prompt + common instructions (cached) |
| `.dynamic` | str | Task description + injected lessons (not cached) |
| `.full` | str | Complete prompt (static + boundary + dynamic) |

**Why split?** Anthropic's prompt caching bills per unique prefix. By isolating task-specific content in `.dynamic`, the `.static` portion gets cached across tasks.

---

### **equipa.monitoring** — loop detection

```python
from equipa.monitoring import LoopDetector

detector = LoopDetector()
status = detector.record(
    result={"action": "read_file", "file": "src/app.py"},
    turn=1,
    files_changed=[]
)

if status == "warning":
    print(detector.warning_message())
elif status == "terminate":
    print(detector.termination_summary())
```

**LoopDetector methods:**
| Method | Returns | Description |
|--------|---------|-------------|
| `.record(result, turn, files_changed)` | `"ok" | "warning" | "terminate"` | Log agent output, check for loops |
| `.warning_message()` | `str` | Generate loop warning for agent context |
| `.termination_summary()` | `str` | Generate termination reason |

**Loop detection rules:**
- Same tool + args 4 times → terminate
- Alternating pattern (A→B→A→B) 6 cycles → terminate
- 3 consecutive text-only turns (no tool calls) → terminate (monologue)

---

## Error Handling

EQUIPA does not return HTTP status codes (no HTTP server). Errors are reported via:
1. **Task status field** — `blocked`, `failed`, `early_terminated`
2. **Blocker type** — `test_failure`, `timeout`, `permission_error`, etc.
3. **Agent logs** — tool call results include `"error"` key

**Common error patterns:**

### **Task already running**
Attempting to dispatch a task that is already in progress.

**MCP response:**
```json
{ "error": "Task 42 already in progress" }
```

**Resolution:** Wait for current run to complete, or kill via `abort_controller`.

---

### **Agent stuck in loop**
Agent repeats the same action 4+ times without file changes.

**Logged as:** `early_terminated` (status), `loop_detected` (blocker_type)

**Example log:**
```
[Turn 12] read_file → src/app.py (loop detected: read_file called 4 times)
[TERMINATE] Agent stuck in loop
```

**Resolution:** ForgeSmith autoresearch loop will retry with refined prompt. Human intervention rarely needed.

---

### **Cost breaker triggered**
Agent exceeded cost budget (scales with complexity).

**Logged as:** `early_terminated`, `cost_exceeded`

**Example:**
```
[Turn 8] Cost: $0.42 (limit: $0.40)
[TERMINATE] Cost limit exceeded
```

**Resolution:** Adjust `cost_limits` in `dispatch_config.toml` or increase task complexity tier.

---

### **Preflight check failed**
Project dependencies not installed (npm/pip/go/cargo).

**Logged as:** `preflight_failed`

**Example:**
```
[Preflight] npm install → Error: package-lock.json missing
[TERMINATE] Preflight check failed
```

**Resolution:** Agent retries after 60s. If repeated failures, human must fix dependencies.

---

## Rate Limiting

EQUIPA uses **exponential backoff** for Anthropic API calls:
- Base delay: 500ms
- Jitter: ±25%
- Cap: 32s
- Model fallback: After 3 `overloaded_error` responses, downgrade Opus → Sonnet

**Circuit breaker:**
- After 5 consecutive API failures, circuit opens
- Agent waits 60s before retry
- Success resets failure counter

**Cost controls:**
- Trivial tasks: $0.10 limit
- Medium tasks: $0.40 limit
- Complex tasks: $1.00 limit

Limits are per-task, not per-agent. Multi-cycle tasks (dev → test → fix) share the same budget.

---

## Known Limitations

Be honest — this is not magic.

1. **Agents still fail.** About 15% of tasks hit blockers requiring human intervention. Common causes: missing dependencies, ambiguous requirements, test flakiness.

2. **Git worktree isolation is finicky.** Merge conflicts occasionally require manual cleanup. The branch strategy (ephemeral worktrees per task) is solid, but edge cases remain.

3. **Self-improvement needs data.** ForgeSmith GEPA/SIMBA loops require 20-30 episodes per role before patterns emerge. Fresh installs start dumb.

4. **Tester role depends on test suites.** If your project has no tests, the tester will write one test, declare victory, and exit. It is not a test generator — it is a test runner.

5. **Early termination kills legitimate complex tasks.** The 10-turn reading limit occasionally terminates agents mid-analysis. Increase `max_turns_by_complexity` in `dispatch_config.toml` if your tasks need deep exploration.

6. **Knowledge graph reranking is experimental.** PageRank-based episode injection improves relevance by ~8% (per SIMBA evals), but occasionally surfaces stale lessons. Pruning logic still being refined.

7. **No auth, no multi-tenancy.** EQUIPA assumes single-user, local deployment. Do not expose the MCP server over the network. No ACLs, no API keys, no rate limiting per-user.

8. **Bash security is paranoid.** The command filter blocks 12+ injection patterns, which occasionally false-positives legitimate commands (e.g., `echo "$(date)"` gets blocked). If you hit a false positive, escape via `write_file` then `bash execute_script.sh`.

---

## Getting Started

1. **Install:** No pip dependencies. Clone and run.
2. **Setup database:** `python equipa_setup.py` (creates SQLite schema)
3. **Add to Claude Desktop:** Copy `.mcp/server_config.json` to `~/Library/Application Support/Claude/`
4. **Talk to Claude:** "Create a task to add rate limiting to /api/upload"

Claude will:
- Create the task in TheForge
- Dispatch a Developer agent
- Monitor progress
- Report results
- Retry on failure (up to 3 attempts with git cleanup between)

You never touch the CLI unless you are debugging or running cron jobs.

---

## Advanced: Custom Agent Roles

To add a new agent role (e.g., "Documenter"):

1. **Create prompt:** `prompts/documenter.md`
2. **Add to dispatch config:** `dispatch_config.toml`
   ```toml
   [roles]
   documenter.model = "claude-sonnet-4-20250514"
   documenter.max_turns = 12
   documenter.cost_limit = 0.30
   ```
3. **Add task type mapping:**
   ```toml
   [task_types]
   documentation = ["documenter"]
   ```
4. **Dispatch:** `python -m equipa.cli dispatch --task-id 42 --role documenter`

The agent will use `prompts/documenter.md` as its system prompt and inherit tool access from `prompts/common.md`.

---

## Contributing

See `CONTRIBUTING.md`. Key points:
- No dependencies added without consensus (keep it stdlib-only)
- All agent prompts must include few-shot examples
- Test coverage >80% (run `pytest tests/`)
- Commit messages follow Conventional Commits

---

## Support

- **Docs:** This file + `CLAUDE.md` + `README.md`
- **Issues:** GitHub Issues (no Discord, no Slack)
- **Logs:** `~/.theforge/logs/equipa/` (one file per agent run)
- **Database:** `~/.theforge/forge.db` (SQLite — readable with `sqlite3` CLI)

If Claude breaks, check `mcp_health.json` for server status. If stale, restart MCP server.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
