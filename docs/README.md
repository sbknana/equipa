# EQUIPA

## Table of Contents

- [EQUIPA](#equipa)
  - [What is this?](#what-is-this)
  - [Quick Start](#quick-start)
  - [How to Use](#how-to-use)
    - [Describe your goal](#describe-your-goal)
    - [Dispatch agents](#dispatch-agents)
- [Dispatch a single task](#dispatch-a-single-task)
- [Auto-dispatch across all projects](#auto-dispatch-across-all-projects)
- [Run multiple tasks in parallel](#run-multiple-tasks-in-parallel)
    - [Monitor progress](#monitor-progress)
    - [Review the nightly report](#review-the-nightly-report)
    - [Analyze performance](#analyze-performance)
    - [Let the system improve itself](#let-the-system-improve-itself)
- [Full analysis + changes](#full-analysis-changes)
- [Dry run (see what it would change)](#dry-run-see-what-it-would-change)
- [Just generate a report](#just-generate-a-report)
  - [Features](#features)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [Detailed Setup](#detailed-setup)
    - [Database Migrations](#database-migrations)
  - [Configuration](#configuration)
    - [`forge_config.json`](#forge_configjson)
    - [`dispatch_config.json`](#dispatch_configjson)
    - [Forgesmith Configuration](#forgesmith-configuration)
  - [Tech Stack](#tech-stack)
  - [License](#license)
  - [Related Documentation](#related-documentation)

**Tell Claude what you want built. Agents do the rest.**

<p align="center">
  <img src="Equipa.png" alt="EQUIPA" width="200">
</p>

*Named after the [Marquis de EQUIPA](https://en.wikipedia.org/wiki/Marquis_of_EQUIPA), who coordinated the rebuilding of Lisbon after the 1755 earthquake.*

## What is this?

EQUIPA is a multi-agent AI orchestration platform. You describe what you want built in plain English, and EQUIPA coordinates a team of AI agents — developers, testers, and security reviewers — to actually build it. It handles task creation, dispatching work to agents, tracking progress, recovering from errors, and learning from past results to get better over time.

It's for developers who want to use AI agents as a real workforce: plan a project, break it into tasks, and let agents execute while you review.

## Quick Start

1. **Clone the repo**
   ```bash
   git clone https://github.com/your-org/Equipa.git
   cd Equipa
   ```

2. **Run the setup wizard**
   ```bash
   python equipa_setup.py
   ```
   This walks you through prerequisites, database creation, config generation, and optional components. No pip installs needed — it's pure Python stdlib.

3. **Create your first project and tasks** using Claude or by inserting rows into the SQLite database directly.

4. **Dispatch work to agents**
   ```bash
   python forge_orchestrator.py --dispatch
   ```

5. **Check progress**
   ```bash
   python forge_dashboard.py
   ```

## How to Use

### Describe your goal
Tell Claude what you want in plain English. EQUIPA breaks your goal into discrete tasks with priorities, complexity estimates, and role assignments (developer, tester, security reviewer).

### Dispatch agents
Run the orchestrator to send tasks to AI agents. EQUIPA picks the right agent role for each task, manages retries, detects when agents get stuck in loops, and terminates unproductive runs early.

```bash
# Dispatch a single task
python forge_orchestrator.py --task-id 42

# Auto-dispatch across all projects
python forge_orchestrator.py --dispatch

# Run multiple tasks in parallel
python forge_orchestrator.py --parallel-tasks 10,11,12
```

### Monitor progress
The dashboard gives you a snapshot of task status, project completion, blocked work, and session activity.

```bash
python forge_dashboard.py
```

### Review the nightly report
Get a portfolio-level summary of what happened today, what's blocked, and what needs attention.

```bash
python nightly_review.py
```

### Analyze performance
Deep-dive into completion rates, throughput, complexity breakdowns, and agent effectiveness.

```bash
python analyze_performance.py
```

### Let the system improve itself
**Forgesmith** is EQUIPA's self-improvement engine. It analyzes agent runs, extracts lessons from failures, tunes configuration, evolves prompts, and prunes what doesn't work.

```bash
# Full analysis + changes
python forgesmith.py --full

# Dry run (see what it would change)
python forgesmith.py --full --dry-run

# Just generate a report
python forgesmith.py --report
```

## Features

- **Plain English task creation** — describe what you want, agents figure out how
- **Multi-agent coordination** — developer, tester, and security reviewer agents work in cycles
- **Security pipeline** — 7 Trail of Bits security skills (static analysis, variant analysis, audit context building, differential review, fix review, semgrep rule creation, sharp-edge detection) auto-dispatched after dev-test
- **Smart loop detection** — detects when agents are stuck repeating themselves and terminates early
- **Monologue detection** — catches agents that talk instead of acting
- **Automatic retries with checkpoints** — agents resume from where they left off after failures
- **Preflight build checks** — validates the project compiles before wasting agent turns
- **Lessons learned database** — failures get recorded and injected into future runs so agents don't repeat mistakes
- **Episode memory with Q-values** — past experiences are ranked by usefulness and surfaced to agents
- **Self-evolving prompts (GEPA)** — EQUIPA automatically mutates and A/B tests agent prompts
- **Autoresearch prompt optimization** — automated mutation loop that benchmarks prompt changes against real tasks, targeting per-role success rates
- **Rule generation (SIMBA)** — analyzes failure patterns and generates reusable rules for agents
- **Rubric-based quality scoring** — every agent output is scored on naming, structure, test coverage, documentation, and error handling
- **Budget awareness** — agents get periodic reminders of remaining turns and cost limits
- **Nightly portfolio review** — automated summary of progress, blockers, and stale work
- **Inter-agent messaging** — agents can leave messages for each other across cycles
- **Zero pip dependencies** — pure Python stdlib + SQLite. Nothing to install.
- **Database migrations** — schema evolves safely with versioned migrations and automatic backups
- **Local model support** — can use Ollama for local inference alongside Claude
- **Arena mode** — run structured evaluation loops to benchmark agent configurations
- **Training data export** — generate fine-tuning datasets from agent interactions
- **Autoresearch benchmarking** — automated prompt optimization loop with per-role success tracking (6/7 roles at 100%)

## Installation

### Prerequisites

- **Python 3.10+** (no pip packages needed)
- **SQLite 3** (included with Python)
- **Claude CLI** (`claude` command available in PATH) — or Ollama for local models
- **Git** (for project management features)
- **GitHub CLI** (`gh`) — optional, for repo setup automation

### Detailed Setup

1. **Clone and enter the project:**
   ```bash
   git clone https://github.com/your-org/Equipa.git
   cd Equipa
   ```

2. **Run the interactive setup:**
   ```bash
   python equipa_setup.py
   ```
   The wizard will:
   - Check that prerequisites are installed
   - Ask where to install (default: `~/.forge/`)
   - Create and initialize the SQLite database (30+ tables)
   - Copy core files to the install directory
   - Generate `forge_config.json`, MCP config, and `CLAUDE.md`
   - Optionally set up Forgesmith cron job, Sentinel monitoring, and ForgeBot

3. **Verify the installation:**
   ```bash
   python forge_dashboard.py
   ```

4. **(Optional) Set up the Forgesmith cron for continuous improvement:**
   The setup wizard offers this, or you can add it manually:
   ```bash
   # Run Forgesmith every 6 hours
   0 */6 * * * cd /path/to/forge && python forgesmith.py --full >> /tmp/forgesmith.log 2>&1
   ```

### Database Migrations

If upgrading from an earlier version:
```bash
python db_migrate.py
```
This automatically detects your current schema version, creates a backup, and applies migrations incrementally.

## Configuration

### `forge_config.json`
Generated by the setup wizard. Key settings:

| Setting | Description |
|---------|-------------|
| `db_path` | Path to the SQLite database |
| `project_dir` | Default working directory for agent operations |
| `max_turns` | Default max turns per agent run |
| `cost_limit` | Maximum cost per run (scales with task complexity) |
| `parallel_limit` | Max concurrent agent dispatches |

### `dispatch_config.json`
Controls automatic dispatching behavior:

| Setting | Description |
|---------|-------------|
| `provider` | Default AI provider (`claude` or `ollama`) per role |
| `model` | Model to use per role |
| `max_parallel` | Concurrency limit for auto-dispatch |
| `filters` | Which projects/priorities to include |

### Forgesmith Configuration
Embedded in `forgesmith.py` config. Controls:
- Lookback period for analysis
- Change suppression (cooldown between similar changes)
- GEPA prompt evolution parameters
- SIMBA rule generation settings
- Rubric scoring weights

## Tech Stack

- **Language:** Python 3.10+ (pure stdlib — zero dependencies)
- **Database:** SQLite with 30+ table schema, versioned migrations
- **AI Backend:** Claude CLI (primary), Ollama (local models)
- **Self-improvement:** Forgesmith engine with GEPA (prompt evolution), SIMBA (rule generation), O-PRO (optimization proposals), Autoresearch (automated prompt benchmarking)
- **Testing:** Custom test suite (no pytest dependency)
- **Architecture:** Single-node orchestrator, async agent dispatch, checkpoint-based recovery

## License

See [LICENSE](LICENSE) for details.
---

## Key Files

| File | Purpose |
|------|---------|
| `forge_orchestrator.py` | Core orchestrator — task dispatch, agent management, dev-test loop |
| `forgesmith.py` | Self-improvement engine (GEPA, SIMBA, lessons, rubrics) |
| `autoresearch_loop.py` | Automated prompt optimization — mutates prompts via Opus, benchmarks against real tasks |
| `autoresearch_prompts.py` | Prompt mutation generator — tiered LLM approach (Ollama/Sonnet/Opus) |
| `dispatch_config.json` | Per-role model assignments, turn budgets, concurrency settings |
| `forge_dashboard.py` | Terminal-based project/task dashboard |
| `nightly_review.py` | Portfolio-level daily summary |
| `equipa_setup.py` | Interactive setup wizard |

## Related Documentation

- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
