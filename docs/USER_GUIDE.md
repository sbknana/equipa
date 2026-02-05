# ForgeTeam User Guide

Complete reference for installing, configuring, and using ForgeTeam — a multi-agent orchestration system for AI-assisted software development.

---

## Table of Contents

1. [Installation](#installation)
2. [Configuration Reference](#configuration-reference)
3. [CLI Modes](#cli-modes)
4. [Agent Roles](#agent-roles)
5. [Project Management](#project-management)
6. [MCP Setup](#mcp-setup)
7. [Database Structure](#database-structure)
8. [Troubleshooting](#troubleshooting)

---

## Installation

### Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| Python 3.10+ | Runs the orchestrator | [python.org](https://www.python.org/downloads/) |
| git | Version control | [git-scm.com](https://git-scm.com/) |
| gh (GitHub CLI) | GitHub repo management | [cli.github.com](https://cli.github.com/) |
| Claude Code CLI | Agent runtime | `npm install -g @anthropic-ai/claude-code` |
| uvx / uv | MCP server runner | `pip install uv` |

### Running the Installer

```bash
cd Itzamna
python itzamna_setup.py
```

The wizard guides you through:
1. Prerequisite verification
2. Install path selection
3. Database creation (empty, with full schema)
4. File copying (orchestrator, prompts, skills)
5. Config file generation (`forge_config.json`, `mcp_config.json`)
6. Claude Code MCP integration (`.mcp.json`)
7. Claude Code context generation (`CLAUDE.md`)
8. Installation verification (9 automated checks)

After setup, open Claude Code in the install directory and it has full MCP access to the database plus context about all commands.

### Manual Installation

If you prefer manual setup:

1. Copy `forge_orchestrator.py`, `dispatch_config.json`, `prompts/`, and `skills/` to your install directory
2. Create a database: `python -c "import sqlite3; sqlite3.connect('theforge.db').executescript(open('schema.sql').read())"`
3. Create `forge_config.json` (see Configuration Reference below)
4. Create `mcp_config.json` pointing to your database
5. Create `.mcp.json` (same format as `mcp_config.json`) for Claude Code MCP access
6. Create `CLAUDE.md` with project context (or copy from an existing installation)

---

## Configuration Reference

### forge_config.json

This file overrides all hardcoded paths in the orchestrator. If absent, the orchestrator falls back to its built-in defaults (backward compatible).

```json
{
    "theforge_db": "/path/to/theforge.db",
    "project_dirs": {
        "myproject": "/path/to/myproject",
        "webapp": "/path/to/webapp"
    },
    "github_owner": "YourGitHubUsername",
    "mcp_config": "/path/to/mcp_config.json",
    "prompts_dir": "/path/to/prompts"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `theforge_db` | string | Absolute path to the SQLite database |
| `project_dirs` | object | Map of project codenames to directory paths |
| `github_owner` | string | GitHub username for repo creation |
| `mcp_config` | string | Path to the MCP configuration file |
| `prompts_dir` | string | Path to the directory containing agent prompt `.md` files |

### dispatch_config.json

Controls auto-run mode behavior.

```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "max_turns": 25,
    "max_tasks_per_project": 5,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": []
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `max_concurrent` | 4 | Max projects running in parallel |
| `model` | sonnet | Default Claude model |
| `max_turns` | 25 | Max conversation turns per agent |
| `max_tasks_per_project` | 5 | Cap tasks per project per run |
| `skip_projects` | [] | Project IDs/codenames to exclude |
| `priority_boost` | {} | Manual priority overrides |
| `only_projects` | [] | Whitelist (empty = all projects) |

### mcp_config.json

MCP server configuration passed to each agent.

```json
{
    "mcpServers": {
        "theforge": {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "mcp-server-sqlite",
                "--db-path",
                "/path/to/theforge.db"
            ]
        }
    }
}
```

---

## CLI Modes

### Single-Agent Mode

Run one agent on one task.

```bash
# Developer agent on a specific task
python forge_orchestrator.py --task 42 -y

# Tester agent
python forge_orchestrator.py --task 42 --role tester -y

# Security reviewer
python forge_orchestrator.py --task 42 --role security-reviewer -y

# Auto-pick next todo task for a project
python forge_orchestrator.py --project 1 -y
```

### Dev+Test Loop Mode

Developer writes code, Tester validates, loop until tests pass (max 3 cycles).

```bash
python forge_orchestrator.py --task 42 --dev-test -y
```

Flow per cycle:
1. Developer agent writes/fixes code
2. Tester agent runs tests (read-only)
3. If tests pass → done. If tests fail → feed failures back to Developer
4. After 3 cycles or no progress → mark task blocked

### Manager Mode (Goal-Driven)

Provide a natural-language goal. The system autonomously plans, executes, and evaluates.

```bash
python forge_orchestrator.py --goal "Add user authentication" --goal-project 1 -y
```

Flow per round:
1. **Planner** agent explores codebase, creates 2-8 tasks
2. **Dev+Test** loops execute each task sequentially
3. **Evaluator** agent reviews results: `complete`, `needs_more`, or `blocked`
4. If `needs_more`, evaluator creates follow-up tasks and loop continues

Options:
- `--max-rounds N` — Max plan-execute-evaluate rounds (default: 3)
- `--model opus` — Use a more capable model

### Parallel Goals Mode

Run multiple goals across different projects concurrently.

```bash
python forge_orchestrator.py --parallel-goals goals.json -y
```

Goals file format:
```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "goals": [
        {"goal": "Add dark mode", "project_id": 4},
        {"goal": "Fix login bug", "project_id": 9, "model": "opus"}
    ]
}
```

Each goal runs a full Manager loop. No two goals can target the same project.

### Auto-Run Mode

Scan all projects for pending work, prioritize, and dispatch automatically.

```bash
# See what would run (dry run)
python forge_orchestrator.py --auto-run --dry-run

# Run with confirmation
python forge_orchestrator.py --auto-run

# Skip confirmation
python forge_orchestrator.py --auto-run -y

# Only run one project
python forge_orchestrator.py --auto-run --only-project 1 -y

# Cap tasks per project
python forge_orchestrator.py --auto-run --max-tasks-per-project 2 -y
```

Priority scoring:
```
score = (critical * 10) + (high * 5) + (medium * 2) + (low * 1)
      + 3 if project is 'active'
      + priority_boost from config
```

### Add Project Mode

Register a new project in the database and config.

```bash
python forge_orchestrator.py --add-project "MyApp" --project-dir "C:\code\myapp"
```

### Setup Repos Mode

Initialize git and create GitHub private repos.

```bash
# All projects
python forge_orchestrator.py --setup-repos --dry-run
python forge_orchestrator.py --setup-repos -y

# Single project
python forge_orchestrator.py --setup-repos-project 1 -y
```

### Common Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | sonnet | Claude model (sonnet, opus, haiku) |
| `--max-turns` | 25 | Max agent conversation turns |
| `--retries` | 3 | Max retry attempts on failure |
| `--dry-run` | — | Show plan without executing |
| `--yes` / `-y` | — | Skip confirmation prompts |

---

## Agent Roles

### Built-in Roles

| Role | File | Job | Access |
|------|------|-----|--------|
| Developer | `developer.md` | Write code, fix bugs, implement features | Read/Write/Edit/Bash + MCP |
| Tester | `tester.md` | Run tests, report failures (read-only) | Read/Bash + MCP |
| Planner | `planner.md` | Break goals into 2-8 ordered tasks | Read/Bash (read-only) + MCP |
| Evaluator | `evaluator.md` | Verify goal completion, create follow-ups | Read/Bash (read-only) + MCP |
| SecurityReviewer | `security-reviewer.md` | 4-phase code security review | Read/Bash + MCP + Skills |

### Shared Rules (`_common.md`)

All agents receive the `_common.md` rules prepended to their role prompt. This includes:
- Branding (TheForge, LLC attribution)
- Windows-specific instructions
- Database interaction patterns
- Output format requirements (JSON with `RESULT:` marker)

### Custom Agents

Drop a `.md` file in `prompts/` and it's automatically discovered. See [CUSTOM_AGENTS.md](CUSTOM_AGENTS.md) for details.

---

## Project Management

### Adding Projects

```bash
python forge_orchestrator.py --add-project "ProjectName" --project-dir "/path/to/code"
```

This:
1. Creates a row in the `projects` table with status `active`
2. Adds the project directory to `forge_config.json`

### Removing Projects

Projects aren't deleted — set their status to `archived`:

```sql
UPDATE projects SET status = 'archived' WHERE id = 42;
```

Remove the entry from `forge_config.json`'s `project_dirs` manually.

### Organizing Work

Tasks have four statuses:
- `todo` — Ready to be picked up
- `in_progress` — Currently being worked on
- `done` — Completed
- `blocked` — Stuck, needs intervention

Priorities: `critical`, `high`, `medium`, `low`

---

## MCP Setup

ForgeTeam uses MCP (Model Context Protocol) in two ways:

1. **Agent MCP** (`mcp_config.json`) — passed to orchestrator-spawned agents so they can access the database
2. **User MCP** (`.mcp.json`) — gives your own Claude Code sessions direct database access

Both are generated automatically by the Itzamna setup wizard.

### How Agent MCP Works

1. The orchestrator passes `--mcp-config mcp_config.json` to each `claude -p` invocation
2. Claude Code starts the MCP server (`uvx mcp-server-sqlite`)
3. The agent can read/write the database using MCP tools: `read_query`, `write_query`, `list_tables`, etc.

### How User MCP Works (`.mcp.json`)

The setup wizard generates `.mcp.json` in your install directory. When you run `claude` from that directory, Claude Code auto-detects it and connects to the database. You can then:

- Query projects, tasks, decisions, and session notes
- Create tasks and update statuses
- Log decisions and session summaries
- Run orchestrator commands via Bash

The wizard also generates a `CLAUDE.md` in the install directory that gives Claude full context about available commands, common SQL queries, agent roles, and database tables.

### `.mcp.json` Format

```json
{
    "mcpServers": {
        "itzamna": {
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-sqlite", "--db-path", "/path/to/theforge.db"]
        }
    }
}
```

### Verifying MCP

Test the MCP server manually:
```bash
uvx mcp-server-sqlite --db-path /path/to/theforge.db
```

If this command starts without error, MCP is working.

---

## Database Structure

The database has 19 tables organized into these groups:

### Core Tables

| Table | Purpose |
|-------|---------|
| `projects` | Project metadata (name, codename, status, summary) |
| `tasks` | Work items (title, status, priority, blocked_by) |
| `decisions` | Architectural decisions with rationale |
| `open_questions` | Unresolved questions and blockers |
| `session_notes` | Session summaries and next steps |

### Content & Marketing

| Table | Purpose |
|-------|---------|
| `social_media_posts` | Social media content library |
| `posting_schedule` | Content calendar |
| `content_tickler` | Content inventory alerts |
| `writing_style` | Per-project writing style guides |

### Research & Analysis

| Table | Purpose |
|-------|---------|
| `research` | Research findings and sources |
| `competitors` | Competitive analysis |
| `product_opportunities` | Market opportunities |

### Assets & Build

| Table | Purpose |
|-------|---------|
| `code_artifacts` | Important code references |
| `documents` | Project documentation |
| `project_assets` | Logos, icons, images |
| `components` | Hardware/software components |
| `build_info` | Build commands and output paths |

### System

| Table | Purpose |
|-------|---------|
| `cross_references` | Links between any two records |
| `reminders` | Scheduled reminders |

### Views

| View | Purpose |
|------|---------|
| `v_project_dashboard` | Active projects with task counts |
| `v_stale_tasks` | In-progress tasks older than 3 days |
| `v_stale_questions` | Unresolved questions older than 7 days |
| `v_upcoming_reminders` | Reminders due within 7 days |
| `v_content_alerts` | Content inventory below threshold |

---

## Troubleshooting

### "Database not found"

The orchestrator can't find the SQLite database.

**Fix:** Check `forge_config.json` has the correct `theforge_db` path. Or verify the default path in `forge_orchestrator.py` matches your setup.

### "Could not find project directory"

The project's codename doesn't match any entry in `PROJECT_DIRS` or `forge_config.json`.

**Fix:** Register the project with `--add-project` or add it manually to `forge_config.json`'s `project_dirs`.

### Agent times out (600s)

The task is too complex for a single agent turn.

**Fix:** Break the task into smaller pieces. Use Manager mode (`--goal`) for large tasks — the Planner will decompose them automatically.

### "No todo tasks found"

Auto-run or `--project` mode found no tasks with status `todo`.

**Fix:** Create tasks in the database. Use MCP, direct SQL, or the Manager mode (`--goal`).

### MCP server won't start

`uvx mcp-server-sqlite` fails.

**Fix:**
1. Ensure `uv` is installed: `pip install uv`
2. Test manually: `uvx mcp-server-sqlite --db-path /path/to/theforge.db`
3. Check `mcp_config.json` has the correct database path

### Tests fail but code looks correct

The Tester agent is read-only — it can't modify code.

**Fix:** Check the test framework auto-detection. The Tester looks for `pytest`, `jest`, `dotnet test`, etc. If your project uses a non-standard test setup, add instructions to the task description.

### "forge_config.json not found" warning

This is informational, not an error. Without a config file, the orchestrator uses its hardcoded defaults.

**Fix:** Run `itzamna_setup.py` to generate the config file, or create one manually (see Configuration Reference above).

### Agent produces no output

The agent may have hit a context limit or encountered an unexpected error.

**Fix:**
1. Try a more capable model: `--model opus`
2. Increase max turns: `--max-turns 50`
3. Check the task description — vague tasks produce poor results
4. Run with `--dry-run` to inspect the system prompt size

### Google Drive sync corruption

Git doesn't work well with cloud sync services. The `.git/index` file can corrupt.

**Fix:** Use GitHub as your backup. If corruption occurs, delete the local `.git` directory and re-clone from GitHub.
