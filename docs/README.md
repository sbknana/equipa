# EQUIPA

## Table of Contents

- [EQUIPA](#equipa)
  - [What is this?](#what-is-this)
  - [Screenshots](#screenshots)
  - [Quick Start](#quick-start)
  - [How to Use](#how-to-use)
    - [Dispatching Tasks](#dispatching-tasks)
    - [The Dev-Test Loop](#the-dev-test-loop)
    - [Watching Progress](#watching-progress)
    - [Agent Roles](#agent-roles)
    - [Self-Improvement (ForgeSmith)](#self-improvement-forgesmith)
    - [Cost Controls](#cost-controls)
  - [Features](#features)
  - [Limitations](#limitations)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [Setup](#setup)
    - [Manual Setup](#manual-setup)
- [Create the database](#create-the-database)
- [Copy config](#copy-config)
- [Edit dispatch_config.json with your paths and API settings](#edit-dispatch_configjson-with-your-paths-and-api-settings)
- [Run migrations to make sure the schema is current](#run-migrations-to-make-sure-the-schema-is-current)
- [Verify](#verify)
    - [Environment Variables](#environment-variables)
  - [Configuration](#configuration)
  - [Tech Stack](#tech-stack)
  - [License](#license)
  - [Related Documentation](#related-documentation)

A multi-agent orchestrator for AI coding tasks. Tell it what you want in plain English, and it coordinates specialized agents to get it done.

![EQUIPA Dashboard](screenshots/dashboard.png)

## What is this?

EQUIPA manages a team of AI agents that write code, run tests, review security, and fix bugs — all from a single task description. You describe what you need, it figures out which agent to send, monitors progress, and retries until tests pass. Named after the Portuguese word for "team", because that's what it builds: a team of agents working on your codebase.

It's built in pure Python with zero dependencies. Copy it, run it, done.

## Screenshots

![Task Dashboard](screenshots/dashboard.png)
*The main dashboard — shows active projects, task counts, completion rates, and what agents are doing right now.*

![Dispatch Summary](screenshots/dispatch-summary.png)
*After a dispatch run, you get a summary of what was sent where and how it went.*

![Nightly Review](screenshots/nightly-review.png)
*The nightly review report — today's accomplishments, blockers, stale tasks, and agent performance stats.*

![ForgeSmith Analysis](screenshots/forgesmith-report.png)
*ForgeSmith self-improvement output — showing what it learned and what config changes it's proposing.*

## Quick Start

1. **Clone the repo**
   ```bash
   git clone https://github.com/your-org/equipa.git
   cd equipa
   ```

2. **Run the setup wizard**
   ```bash
   python equipa_setup.py
   ```
   This walks you through database creation, config generation, and optional components. It takes about 2 minutes.

3. **Add your first project**
   ```bash
   sqlite3 ~/.equipa/equipa.db "INSERT INTO projects (name, repo_path, status) VALUES ('my-app', '/path/to/my-app', 'active');"
   ```

4. **Create a task**
   ```bash
   sqlite3 ~/.equipa/equipa.db "INSERT INTO tasks (project_id, title, description, status, priority) VALUES (1, 'Add login page', 'Create a login page with email/password fields and form validation', 'todo', 'high');"
   ```

5. **Dispatch it**
   ```bash
   python -m equipa --auto
   ```
   EQUIPA picks up the task, selects the right agent role, and starts working.

## How to Use

### Dispatching Tasks

The simplest way is auto-dispatch. EQUIPA scans your database for pending tasks, scores them by priority and project health, and sends agents to work:

```bash
python -m equipa --auto
```

Want to run a specific task? Pass the ID:

```bash
python -m equipa --task 42
```

Need to run several tasks in parallel:

```bash
python -m equipa --tasks 42,43,44
```

### The Dev-Test Loop

This is the core of EQUIPA. When an agent finishes writing code, EQUIPA doesn't just mark it done. It runs tests. If tests fail, it sends the failure output back to the agent and says "try again." This loop continues until tests pass or the budget runs out.

This is why EQUIPA actually finishes tasks instead of producing code that looks right but doesn't work.

### Watching Progress

Run the dashboard to see what's happening across all your projects:

```bash
python tools/forge_dashboard.py
```

For a daily summary of what got done, what's blocked, and what needs attention:

```bash
python nightly_review.py
```

### Agent Roles

EQUIPA has 9 specialized agent roles, each with language-aware prompts that change based on whether your project is Python, TypeScript, Go, Rust, C#, or Java:

- **Developer** — writes code, fixes bugs, implements features
- **Tester** — writes and runs tests, reports coverage gaps
- **Security Reviewer** — audits code for vulnerabilities, parses SARIF reports
- **Planner** — breaks down large tasks into subtasks
- **Evaluator** — reviews completed work and decides if it's actually done
- **Researcher** — investigates technical questions and documents findings
- **Architect** — designs system structure and API contracts
- **Documenter** — writes docs, READMEs, API references
- **DevOps** — handles CI/CD, deployment configs, infrastructure

### Self-Improvement (ForgeSmith)

This is the part that makes EQUIPA weird in a good way. After agents run tasks, ForgeSmith analyzes what happened — what worked, what failed, what patterns keep repeating — and adjusts the system:

- **ForgeSmith** — the main loop. Analyzes agent runs, extracts lessons, adjusts configs, tunes max turns.
- **GEPA** (Genetic Episodic Prompt Adaptation) — evolves agent prompts based on success/failure patterns. It A/B tests new prompts against the current ones.
- **SIMBA** (Situation-specific IMprovement via Behavioral Analysis) — generates tactical rules like "when you see error X in role Y, try Z first."

Run it manually:
```bash
python forgesmith.py --full
python forgesmith_gepa.py --run
python forgesmith_simba.py --run
```

Or set it up as a cron job (the setup wizard offers this).

The self-improvement system keeps episodic memory — it remembers past successes and failures and injects relevant episodes into agent prompts. Over time, agents stop making the same mistakes.

### Cost Controls

Agents can burn through API credits fast if you're not careful. EQUIPA has hard limits:

- **Turn limits** — each task gets a max number of turns based on complexity
- **Cost breakers** — if an agent exceeds a dollar threshold, it's killed immediately
- **Early termination** — detects stuck agents (monologuing, tool loops, alternating patterns) and kills them before they waste more turns
- **Budget warnings** — agents get told how many turns they have left at intervals, so they can wrap up

## Features

- **Dev-test iteration loop** — agents retry until tests actually pass, not just until code compiles
- **9 specialized agent roles** with prompts tuned per programming language
- **Zero dependencies** — pure Python stdlib, copy the files and run
- **Self-improving agents** — ForgeSmith + GEPA + SIMBA with episodic memory form a closed loop that gets better over time
- **Cost controls that actually kill runaway agents** — not just warnings, hard kills
- **Anti-compaction state persistence** — long-running tasks don't lose context when the conversation gets long
- **Loop detection** — catches agents stuck in read-only loops, monologues, and alternating tool patterns
- **Language-aware prompts** — different guidance for Python vs TypeScript vs Go vs Rust vs C# vs Java
- **Nightly review reports** — automated summary of portfolio health, blockers, and agent performance
- **Git worktree isolation** — agents work in separate worktrees so they don't step on each other
- **Skill integrity verification** — SHA-256 manifests ensure prompts and skills haven't been tampered with
- **Lesson sanitization** — injected lessons are scrubbed for prompt injection attempts, XML tags, base64 payloads
- **334+ passing tests** — the system tests itself pretty thoroughly
- **Database migrations** — schema versioning with automatic backups before migration
- **Agent messaging** — agents can leave messages for other agents on the same task

## Limitations

Be honest with yourself before diving in:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent reads files for 10 turns trying to understand the codebase instead of just writing code. The early termination system catches this at 10 turns of reading, but some legitimately complex tasks need more exploration time. It's a trade-off.

- **Git worktree merges occasionally need manual intervention.** Most of the time it works fine. Sometimes you get merge conflicts that need a human to sort out. We're refining this but don't call it bulletproof.

- **Self-improvement needs 20-30 tasks before patterns emerge.** GEPA and SIMBA need data to work with. If you've only run 5 tasks, ForgeSmith won't have enough signal to make useful adjustments. Give it time.

- **The Tester role depends on your project having a working test suite.** If your project has no tests and no test framework configured, the tester agent can't do much. It works best when there's something to run.

- **Early termination can be aggressive.** Killing agents at 10 turns of reading means some legitimate deep-exploration tasks get cut short. You can adjust the thresholds, but the defaults are tuned for cost savings over completeness.

- **Ollama support works but is slower.** The system supports local models via Ollama, but the agent quality drops noticeably compared to Claude. Good for experimentation, not great for production tasks.

- **SQLite as the database.** It works fine for single-user or small-team setups. If you need concurrent writes from many machines, you'll hit SQLite's limitations.

- **No web UI.** Everything is CLI and SQLite queries. The dashboard is a terminal report, not a browser app.

## Installation

### Prerequisites

- Python 3.10+ (stdlib only, no pip install needed)
- SQLite 3.35+ (comes with Python)
- Claude CLI or Anthropic API key (for the AI agents)
- Git (for worktree isolation)
- Optionally: Ollama (for local model support)

### Setup

The setup wizard handles most of this:

```bash
python equipa_setup.py
```

It will:
1. Check prerequisites
2. Ask where you want EQUIPA installed (default: `~/.equipa`)
3. Create the SQLite database with the full 30+ table schema
4. Generate your `dispatch_config.json`
5. Generate MCP config files if you're using Claude Desktop
6. Set up the `CLAUDE.md` project file
7. Optionally set up ForgeSmith as a cron job
8. Optionally install Sentinel (file watcher) and ForgeBot (Slack integration)

### Manual Setup

If you prefer doing it yourself:

```bash
# Create the database
sqlite3 ~/.equipa/equipa.db < sql/schema.sql

# Copy config
cp dispatch_config.example.json dispatch_config.json
# Edit dispatch_config.json with your paths and API settings

# Run migrations to make sure the schema is current
python db_migrate.py --db ~/.equipa/equipa.db

# Verify
python -m equipa --help
```

### Environment Variables

```bash
EQUIPA_DB=~/.equipa/equipa.db          # Path to the SQLite database
ANTHROPIC_API_KEY=sk-ant-...            # Your Anthropic API key
EQUIPA_CONFIG=./dispatch_config.json    # Path to dispatch config
```

## Configuration

The main config file is `dispatch_config.json`. Key settings:

```json
{
  "db_path": "~/.equipa/equipa.db",
  "checkpoints_dir": "~/.equipa/checkpoints",
  "max_parallel": 3,
  "default_max_turns": 30,
  "cost_limit_per_task": 2.50,
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "features": {
    "episodic_memory": true,
    "simba_rules": true,
    "lesson_injection": true,
    "preflight_checks": true,
    "early_termination": true,
    "language_detection": true
  }
}
```

Feature flags let you toggle individual systems on/off. All are on by default.

ForgeSmith has its own config section for tuning the self-improvement loop — how often it runs, how aggressively it changes prompts, rollback thresholds, etc.

## Tech Stack

- **Python 3.10+** — stdlib only, no external packages
- **SQLite** — 30+ table schema, handles everything from tasks to episodic memory to prompt versioning
- **Claude API** (Anthropic) — primary AI provider for agents
- **Ollama** — optional local model support
- **Git worktrees** — agent isolation during parallel execution
- **DSPy-style prompt optimization** — GEPA uses evolutionary prompt tuning inspired by DSPy/OPRO
- **SARIF parsing** — security agents can consume static analysis output from any tool that produces SARIF

## License

See [LICENSE](LICENSE) for details.
---

## Related Documentation

- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
