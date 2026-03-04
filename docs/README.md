<p align="center">
  <img src="ItzamnaIcon.png" alt="ForgeTeam" width="200">
</p>

## Table of Contents

  - [What is this?](#what-is-this)
  - [How It Actually Works](#how-it-actually-works)
  - [Screenshots](#screenshots)
  - [Quick Start](#quick-start)
  - [Features](#features)
  - [CLI Reference](#cli-reference)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Tech Stack](#tech-stack)
  - [License](#license)
  - [Related Documentation](#related-documentation)

<h1 align="center">ForgeTeam</h1>

<p align="center">
  <strong>A multi-agent AI system that manages your entire software development workflow — planning, coding, testing, reviewing, and learning from every run.</strong>
</p>

<p align="center">
  <em>Named after Itzamna, the Mayan god of creation, writing, and knowledge</em>
</p>

---

![ForgeTeam Dashboard](screenshots/dashboard.png)

## What is this?

ForgeTeam is an AI-powered development team that works on your codebase. You talk to Claude in plain English. Claude manages everything else — querying the database, creating tasks, dispatching agents, tracking progress, and reporting results. You make decisions; Claude handles execution.

No CLI commands to memorize. No task IDs to look up. No SQL to write. Just conversation.

## How It Actually Works

ForgeTeam is designed to be used through **natural language conversation with Claude**. Claude has full access to the project database via MCP and knows how to use every tool in the system. Here's what that looks like in practice:

### You talk. Claude works.

> **You:** "What's the status of our Loom project?"
>
> **Claude:** *queries the database, pulls recent session notes, checks open tasks and blockers, and gives you a full status report*

> **You:** "What tasks are currently outstanding?"
>
> **Claude:** *runs the query, groups by project and priority, highlights blocked items and stale tasks*

> **You:** "Work on the next high-priority Loom task"
>
> **Claude:** *finds the highest-priority pending task, dispatches a developer agent, monitors the dev-test loop, and reports results when done*

> **You:** "Add a task for Loom: implement dark mode toggle"
>
> **Claude:** *creates the task in the database with the right project ID, sets priority, confirms it's ready*

> **You:** "Run a security review on what we just shipped"
>
> **Claude:** *dispatches the security-reviewer agent with Trail of Bits tooling, reports findings with severity ratings*

> **You:** "How much have we spent on agent runs this month?"
>
> **Claude:** *queries cost tracking views, breaks down by project and role, shows trends*

### What's happening under the hood

When you ask Claude to "work on the next Loom task," here's what actually happens — all automatically:

1. Claude queries the database for pending tasks on the Loom project
2. Scores them by priority and dependencies
3. Loads the project context (last session notes, open questions, lessons learned)
4. Dispatches a developer agent with the full context
5. The developer writes code; a tester agent validates it
6. They iterate until tests pass (or the budget runs out)
7. If security review is enabled, a security agent audits the changes
8. Claude records the episode, updates the task status, and reports back to you

You see: "Task done, tests passing, here's what changed." Claude handled 8 steps behind the scenes.

### The CLI still exists — you just don't need it

Everything Claude does conversationally can also be done from the command line. The CLI is there for automation (cron jobs, CI/CD pipelines, scripted workflows) and for advanced users who want direct control. But for day-to-day development work, you never touch it.

## Screenshots

### Dashboard Overview
![Dashboard](screenshots/dashboard.png)
*The dashboard gives you a bird's-eye view of all your projects — task completion rates, blocked items, session activity, and priority breakdowns.*

### Task Dispatch
![Dispatch](screenshots/dispatch.png)
*The auto-dispatch view shows which projects have pending work, scores them by priority, and runs agents in parallel across your codebase.*

### Performance Analytics
![Analytics](screenshots/analytics.png)
*Track completion rates by project, complexity, and priority. See throughput trends over time and identify bottlenecks.*

### Forgesmith Self-Improvement
![Forgesmith](screenshots/forgesmith.png)
*Forgesmith analyzes past agent runs and automatically proposes configuration changes, prompt patches, and new lessons learned.*

## Quick Start

**1. Run the guided installer:**
```bash
python itzamna_setup.py
```

**2. The installer walks you through everything:** prerequisites check, database creation, config generation, and optional components. Just answer the prompts.

**3. Open Claude Code in your ForgeTeam directory:**
```bash
cd ~/ForgeTeam
claude
```

**4. Start talking:**
> "Show me all active projects"
>
> "Add a new project called MyApp with the code at /home/user/myapp"
>
> "Create a task for MyApp: set up the database schema"
>
> "Work on that task"

That's it. Claude knows the database, the orchestrator, the config — everything. Just tell it what you want.

## Features

- **Conversational interface** — Talk to Claude in plain English. No commands to memorize, no task IDs to track
- **Autonomous dev-test loops** — Developer and tester agents iterate until the code works, not just compiles
- **Persistent project memory** — Every run is recorded; agents learn from past successes and failures on *your* projects
- **Smart task dispatch** — Automatically prioritizes work across multiple projects
- **Self-improving prompts** — ForgeSmith evolves agent prompts based on real performance data
- **Security pipeline** — Trail of Bits tooling (Semgrep, CodeQL) with auto-dispatch after dev-test
- **Inter-agent messaging** — Agents share findings, blockers, and context with each other
- **Loop detection** — Catches stuck agents and terminates gracefully
- **Checkpoint and resume** — Long tasks can be paused and resumed
- **Multi-model support** — Claude Code, Ollama (local models), configurable per role
- **Database migrations** — Schema evolves safely with automatic backups and zero data loss
- **Zero pip dependencies** — Pure Python stdlib, SQLite database. Requires Python 3.10+, Claude Code CLI, git, and uvx as runtime prerequisites

## CLI Reference

The CLI is the engine under the hood. You rarely need it directly, but it's there for automation and scripting.

<details>
<summary>Click to expand CLI commands</summary>

### Running Tasks
```bash
# Single task
python forge_orchestrator.py --task 42

# Auto-dispatch across all projects
python forge_orchestrator.py --dispatch

# Parallel tasks
python forge_orchestrator.py --tasks 42,43,44
```

### Planning
```bash
python forge_orchestrator.py --plan --goal "Add user authentication to the API"
```

### Security Review
```bash
python forge_orchestrator.py --task 42 --security
```

### ForgeSmith Self-Improvement
```bash
# Dry run — see what would change
python forgesmith.py --dry-run

# Apply improvements
python forgesmith.py
```

### Dashboard
```bash
python forge_dashboard.py
```

### Database Migration
```bash
python db_migrate.py /path/to/theforge.db
```

</details>

## Installation

### Prerequisites
- **Python 3.10+**
- **Claude Code CLI** (`claude` command available in PATH)
- **SQLite 3** (included with Python)
- **Git** (for repository management features)
- **GitHub CLI** (`gh`) — optional, for automated repo setup

### Guided Setup (Recommended)
```bash
git clone <repo-url> forgeteam
cd forgeteam
python itzamna_setup.py
```

The installer will:
1. Check all prerequisites
2. Let you choose an install path
3. Create and initialize the SQLite database
4. Copy all necessary files
5. Generate configuration files
6. Set up MCP integration for Claude Code
7. Optionally configure Sentinel (monitoring) and ForgeBot (automation)

### Manual Setup
```bash
# 1. Clone the repository
git clone <repo-url> forgeteam
cd forgeteam

# 2. Initialize the database
sqlite3 forge.db < schema.sql

# 3. Run migrations to latest version
python db_migrate.py

# 4. Copy the example config and edit it
cp config.example.json config.json
# Edit config.json with your paths and preferences

# 5. Verify everything works
python forge_dashboard.py
```

## Configuration

ForgeTeam uses a JSON configuration file (`config.json`) and an optional dispatch configuration (`dispatch_config.json`).

### Key Configuration Options

| Setting | What it does |
|---|---|
| `db_path` | Path to your SQLite database |
| `project_dir` | Root directory for your projects |
| `max_turns` | Maximum dev-test iterations per task |
| `model` | Default AI model for agents |
| `checkpoint_dir` | Where to store task checkpoints |
| `forgesmith.lookback_days` | How far back Forgesmith looks for patterns |
| `forgesmith.dry_run` | Preview changes without applying them |

### Dispatch Configuration
The dispatch config (`dispatch_config.json`) controls:
- Which model each agent role uses
- Per-role system prompts
- Provider selection (Claude, Ollama)
- Task type routing and filtering
- Concurrency limits for parallel dispatch

### Ollama (Local Models)
To use local models via Ollama, set the provider in your dispatch config:
```json
{
  "provider": "ollama",
  "ollama_base_url": "http://localhost:11434",
  "ollama_model": "codellama:34b"
}
```

## Tech Stack

- **Python** — Core orchestrator, all tooling, stdlib only
- **SQLite** — All persistent state: tasks, episodes, lessons, metrics, messages
- **Claude Code CLI** — Primary AI backend for agent execution
- **Ollama** — Optional local model support
- **DSPy** — Used by ForgeSmith GEPA for prompt evolution (optional)
- **SARIF** — Standard format for security analysis findings

## License

See [LICENSE](LICENSE) for details.
---

## Related Documentation

- [Capabilities](CAPABILITIES.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
