<p align="center">
  <img src="ItzamnaIcon.png" alt="ForgeTeam" width="200">
</p>

## Table of Contents

  - [What is this?](#what-is-this)
  - [Screenshots](#screenshots)
    - [Dashboard Overview](#dashboard-overview)
    - [Task Dispatch](#task-dispatch)
    - [Performance Analytics](#performance-analytics)
    - [Forgesmith Self-Improvement](#forgesmith-self-improvement)
  - [Quick Start](#quick-start)
  - [How to Use](#how-to-use)
    - [Planning Work](#planning-work)
    - [Running Tasks](#running-tasks)
- [Single task](#single-task)
- [Auto-dispatch across all projects](#auto-dispatch-across-all-projects)
- [Parallel tasks](#parallel-tasks)
    - [Checking Progress](#checking-progress)
    - [Performance Reports](#performance-reports)
    - [Self-Improvement with Forgesmith](#self-improvement-with-forgesmith)
- [See what Forgesmith would change (dry run)](#see-what-forgesmith-would-change-dry-run)
- [Apply improvements](#apply-improvements)
    - [Security Reviews](#security-reviews)
  - [Features](#features)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [Guided Setup (Recommended)](#guided-setup-recommended)
    - [Manual Setup](#manual-setup)
- [1. Clone the repository](#1-clone-the-repository)
- [2. Initialize the database](#2-initialize-the-database)
- [3. Run migrations to latest version](#3-run-migrations-to-latest-version)
- [4. Copy the example config and edit it](#4-copy-the-example-config-and-edit-it)
- [Edit config.json with your paths and preferences](#edit-configjson-with-your-paths-and-preferences)
- [5. Verify everything works](#5-verify-everything-works)
  - [Configuration](#configuration)
    - [Key Configuration Options](#key-configuration-options)
    - [Dispatch Configuration](#dispatch-configuration)
    - [Ollama (Local Models)](#ollama-local-models)
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

ForgeTeam is an AI-powered development team that works on your codebase autonomously. It assigns tasks to specialized AI agents — a planner breaks down goals, a developer writes code, a tester validates it, and a reviewer checks quality — all coordinated through a central orchestrator. Over time, ForgeTeam learns from its successes and failures, automatically tuning its own prompts and strategies to get better at working on *your* projects.

Think of it as a self-improving AI dev team that remembers what worked, avoids past mistakes, and handles the full development cycle from task planning to code review.

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

**3. Run your first task:**
```bash
python forge_orchestrator.py --task <task_id>
```

**4. Or let ForgeTeam pick what to work on automatically:**
```bash
python forge_orchestrator.py --dispatch
```

**5. Check results on the dashboard:**
```bash
python forge_dashboard.py
```

That's it. The installer handles database setup, config files, and MCP integration for you.

## How to Use

### Planning Work
Give ForgeTeam a high-level goal and it breaks it down into concrete tasks:
```bash
python forge_orchestrator.py --plan --goal "Add user authentication to the API"
```
The planner agent creates prioritized, dependency-aware tasks in your database.

### Running Tasks
Run a specific task by ID, or let the dispatcher choose the highest-priority work:
```bash
# Single task
python forge_orchestrator.py --task 42

# Auto-dispatch across all projects
python forge_orchestrator.py --dispatch

# Parallel tasks
python forge_orchestrator.py --tasks 42,43,44
```

Each task goes through a **dev→test loop**: the developer agent writes code, the tester agent validates it, and they iterate until tests pass or the budget is exhausted.

### Checking Progress
The dashboard shows you everything at a glance:
```bash
python forge_dashboard.py
```
You'll see task completion rates, blocked items, open questions from agents, and session activity over time.

### Performance Reports
Dive deeper into how your agents are performing:
```bash
python analyze_performance.py --days 30
```

### Self-Improvement with Forgesmith
ForgeTeam gets smarter over time. Forgesmith analyzes completed runs and:
- Extracts lessons from failures
- Tunes agent prompts automatically
- Adjusts configuration (max turns, model selection)
- Prunes strategies that aren't working

```bash
# See what Forgesmith would change (dry run)
python forgesmith.py --dry-run

# Apply improvements
python forgesmith.py
```

### Security Reviews
Run a dedicated security review on any task:
```bash
python forge_orchestrator.py --task 42 --security
```

## Features

- **Autonomous dev→test loops** — Developer and tester agents iterate until the code actually works, not just compiles
- **Persistent project memory** — Every run is recorded as an episode; agents learn from past successes and failures
- **Smart task dispatch** — Automatically prioritizes work across multiple projects based on urgency, complexity, and dependencies
- **Self-improving prompts** — Forgesmith (SIMBA + GEPA) evolves agent prompts based on real performance data
- **Lesson learning** — Automatically extracts and injects relevant lessons from past failures into future runs
- **Loop detection** — Catches agents that get stuck repeating the same actions and terminates gracefully
- **Early termination** — Detects stuck phrases and repeated tool calls, saving time and API costs
- **Inter-agent messaging** — Agents communicate findings, blockers, and context to each other across cycles
- **Checkpoint & resume** — Long-running tasks can be paused and resumed without losing progress
- **Rubric-based scoring** — Every run is scored on multiple dimensions, enabling data-driven improvement
- **Multi-model support** — Works with Claude Code, Ollama (local models), and configurable model selection per role
- **Security scanning** — Dedicated security review agent with SARIF parsing for static analysis results
- **Arena mode** — Adversarial testing where agents try to break and fix each other's work
- **Database migrations** — Schema evolves safely with automatic backups and versioned migrations
- **Zero pip dependencies** — Pure Python stdlib, SQLite database. Requires Python 3.10+, Claude Code CLI, git, and uvx as runtime prerequisites

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
| `max_turns` | Maximum dev→test iterations per task |
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

- **Python** — Core orchestrator, all tooling, no framework dependencies
- **SQLite** — All persistent state: tasks, episodes, lessons, metrics, messages
- **Claude Code CLI** — Primary AI backend for agent execution
- **Ollama** — Optional local model support
- **DSPy** — Used by Forgesmith GEPA for prompt evolution
- **SARIF** — Standard format for security analysis findings
- **QLoRA/PEFT** — Optional fine-tuning pipeline for training custom models on ForgeTeam data

## License

See [LICENSE](LICENSE) for details.
---

## Related Documentation

- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
