# Orchestrator CLI Reference

> Most users should use [Coordinator Mode](README.md) — just talk to Claude. This page documents the CLI for automation, scripting, and advanced usage.

---

## Running Tasks

```bash
# Single task by ID
python forge_orchestrator.py --task 42 --dev-test -y

# Multiple tasks in parallel
python forge_orchestrator.py --tasks 42,43,44 --dev-test -y

# Task range
python forge_orchestrator.py --tasks 100-104 --dev-test -y

# Auto-dispatch: scan all projects, prioritize, run
python forge_orchestrator.py --dispatch -y

# Dry run: see what would be dispatched without running
python forge_orchestrator.py --dispatch --dry-run
```

## Goal-Driven Mode

Give a high-level goal and the planner agent breaks it into tasks:

```bash
python forge_orchestrator.py --goal "Add JWT authentication to the API" --goal-project 1 -y
```

The planner creates prioritized, dependency-aware tasks. Then the orchestrator dispatches agents for each one.

## Security Review

```bash
# After a dev-test run
python forge_orchestrator.py --task 42 --dev-test --security-review -y

# Standalone security review
python forge_orchestrator.py --task 42 --role security-reviewer -y
```

Security reviews auto-dispatch after dev-test when `security_review: true` is set in `dispatch_config.json` (enabled by default).

## Specific Agent Roles

Run a specific role on a task:

```bash
python forge_orchestrator.py --task 42 --role developer -y
python forge_orchestrator.py --task 42 --role tester -y
python forge_orchestrator.py --task 42 --role security-reviewer -y
python forge_orchestrator.py --task 42 --role code-reviewer -y
python forge_orchestrator.py --task 42 --role debugger -y
```

## Project Management

```bash
# Add a new project
python forge_orchestrator.py --add-project "MyProject" --project-dir "/path/to/code"

# List registered projects
python forge_orchestrator.py --list-projects
```

## ForgeSmith Self-Improvement

```bash
# See what ForgeSmith would change (dry run)
python forgesmith.py --dry-run

# Apply improvements
python forgesmith.py --auto

# Check ForgeSmith run history
python forgesmith.py --status
```

ForgeSmith runs nightly via cron (configured during setup). It analyzes agent performance and tunes prompts, turn limits, and model assignments automatically.

## Dashboard

```bash
python forge_dashboard.py
```

Terminal-based view of task completion rates, blocked items, agent performance, and session activity.

## Database Migration

```bash
# Detect version and apply pending migrations (v0 through v4)
python db_migrate.py /path/to/theforge.db

# Run the migration benchmark (reproducible demo)
python benchmark_migrations.py
```

## Manual Setup

If you prefer not to use the guided installer:

```bash
# 1. Clone the repository
git clone <repo-url> pombal
cd pombal

# 2. Initialize the database
sqlite3 theforge.db < schema.sql

# 3. Run migrations (sets PRAGMA user_version)
python db_migrate.py theforge.db

# 4. Copy and edit the config
cp config.example.json forge_config.json
# Edit forge_config.json with your paths

# 5. Generate MCP config for Claude Code
# (or let pombal_setup.py do it)

# 6. Verify
python forge_orchestrator.py --help
```

## Configuration

### forge_config.json

| Setting | What it does |
|---|---|
| `theforge_db` | Path to the SQLite database |
| `project_dirs` | Map of project names to local paths |
| `github_owner` | GitHub username for repo operations |
| `prompts_dir` | Path to agent prompt files |
| `mcp_config` | Path to MCP server config |

### dispatch_config.json

| Setting | What it does |
|---|---|
| `model` | Default model for all agents |
| `model_<role>` | Model override per role (e.g., `model_tester`) |
| `max_turns_<role>` | Turn budget per role |
| `provider` | Default provider (`claude` or `ollama`) |
| `provider_<role>` | Provider override per role |
| `security_review` | Auto-dispatch security review after dev-test (default: true) |
| `security_review_timeout` | Timeout for security reviews in seconds |
| `max_concurrent_agents` | Parallel dispatch limit (default: 4) |
| `task_type_prompts` | Per-task-type prompt supplements |

### Ollama (Local Models)

```json
{
  "provider": "claude",
  "provider_planner": "ollama",
  "provider_evaluator": "ollama",
  "ollama_base_url": "http://localhost:11434",
  "ollama_model": "qwen3.5:27b"
}
```

Read-only roles (planner, evaluator, code-reviewer, researcher) work well on local models at zero API cost.

---

<p align="center">
  <em>For day-to-day use, just talk to Claude. See the <a href="README.md">README</a>.</em>
</p>
