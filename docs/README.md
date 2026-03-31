# EQUIPA

## Table of Contents

- [EQUIPA](#equipa)
  - [What is this?](#what-is-this)
  - [Screenshots](#screenshots)
  - [Quick Start](#quick-start)
  - [How to Use](#how-to-use)
    - [The conversational workflow (this is how most people use it)](#the-conversational-workflow-this-is-how-most-people-use-it)
    - [The CLI (for automation and scripting)](#the-cli-for-automation-and-scripting)
- [Dispatch a specific task](#dispatch-a-specific-task)
- [Auto-dispatch pending work across all projects](#auto-dispatch-pending-work-across-all-projects)
- [Run the MCP server manually](#run-the-mcp-server-manually)
- [Run multiple tasks in parallel](#run-multiple-tasks-in-parallel)
    - [The self-improvement loop](#the-self-improvement-loop)
  - [Features](#features)
  - [Limitations](#limitations)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [Step by step](#step-by-step)
- [1. Clone](#1-clone)
- [2. Run the interactive setup](#2-run-the-interactive-setup)
- [3. Verify everything works](#3-verify-everything-works)
- [4. Run database migrations (if upgrading from an older version)](#4-run-database-migrations-if-upgrading-from-an-older-version)
    - [Setting up the self-improvement cron](#setting-up-the-self-improvement-cron)
- [Run ForgeSmith nightly at 2 AM](#run-forgesmith-nightly-at-2-am)
- [Run SIMBA weekly on Sundays](#run-simba-weekly-on-sundays)
  - [Configuration](#configuration)
  - [Tech Stack](#tech-stack)
  - [License](#license)
  - [Related Documentation](#related-documentation)

**Your AI development team, managed through conversation.**

Talk to Claude in plain English. Claude dispatches specialized AI agents to build, test, review, and secure your code — then reports back what happened.

![EQUIPA Dashboard](screenshots/dashboard.png)

---

## What is this?

EQUIPA is a multi-agent orchestrator for AI coding tasks. You describe what you want in conversation with Claude, and it figures out which agents to send, monitors their progress, and tells you when they're done. Named after the European Portuguese word for "team."

It's pure Python with zero dependencies. Copy the files, point it at a SQLite database, and go.

## Screenshots

![Task Dashboard](screenshots/dashboard.png)
*The dashboard shows all your projects, task completion rates, blocked work, and agent activity at a glance.*

![Dispatch Summary](screenshots/dispatch-summary.png)
*After agents finish, you get a summary of what was done, what passed, and what needs attention.*

![ForgeSmith Analysis](screenshots/forgesmith-report.png)
*ForgeSmith reviews agent performance and suggests configuration changes — the self-improvement loop in action.*

![Nightly Review](screenshots/nightly-review.png)
*The nightly review gives you a portfolio-level view: accomplishments, blockers, stale projects, upcoming reminders.*

---

## Quick Start

1. **Clone the repo and run setup:**
   ```bash
   git clone https://github.com/sbknana/equipa.git
   cd equipa
   python equipa_setup.py
   ```

2. **The setup wizard walks you through everything** — database creation, config generation, MCP server config, and CLAUDE.md placement.

3. **Open Claude Desktop** (or any Claude interface with MCP support). EQUIPA registers as an MCP server, so Claude can see your tasks, dispatch agents, and check results.

4. **Talk to Claude:**
   > "Create a task to add input validation to the user registration endpoint in the webapp project."

   Claude creates the task, picks the right agent role, dispatches it, and reports back.

5. **Check results:**
   > "How did that task go? Any test failures?"

   Claude pulls the agent logs, test results, and gives you a summary.

That's it. You talk, Claude orchestrates, agents do the work.

---


---

## How It Actually Works

```
You (human) ──talk to──> Claude ──dispatches──> EQUIPA Orchestrator ──spawns──> Agents
                              <──reports back──         <──results──           (dev, test, review)
```

**You never run the orchestrator directly.** You talk to Claude. Claude reads your TheForge database (via MCP), creates tasks, and dispatches agents. When they're done, Claude tells you what happened.

The only time you interact with EQUIPA directly is during initial setup (`python equipa_setup.py`). After that, everything goes through Claude.

## How to Use

### The conversational workflow (this is how most people use it)

You don't need to learn commands. Just talk to Claude:

- **"What's pending in the webapp project?"** — Claude queries your task database and tells you.
- **"Dispatch the top 3 tasks"** — Claude picks the highest-priority work, assigns agent roles, and runs them.
- **"The login endpoint needs rate limiting. Create a task for that."** — Claude creates it with the right priority and complexity.
- **"Run the security reviewer on task 47"** — Claude dispatches a specific agent role.
- **"Show me today's progress"** — Claude pulls completion stats, blockers, and open questions.

Claude handles task creation, agent dispatch, progress monitoring, error recovery, and reporting. You stay in the conversation.

### The CLI (for automation and scripting)

If you need to script things or run EQUIPA from CI:

```bash
# Dispatch a specific task
python -m equipa dispatch --task 42

# Auto-dispatch pending work across all projects
python -m equipa dispatch --auto

# Run the MCP server manually
python -m equipa --mcp-server

# Run multiple tasks in parallel
python -m equipa dispatch --tasks 42,43,44
```

### The self-improvement loop

This is where things get interesting. EQUIPA has three systems that learn from past agent runs:

- **ForgeSmith** analyzes agent performance — which tasks hit max turns, which errors repeat, which prompts underperform. It suggests (and optionally applies) config changes.
- **GEPA** (Guided Evolution of Prompt Architecture) evolves agent prompts based on success/failure patterns. It runs A/B tests between prompt versions.
- **SIMBA** (Situation-Informed Memory-Based Adaptation) extracts tactical rules from agent episodes. "When you see X error, try Y approach first."

Run them manually or set up a cron job:
```bash
python forgesmith.py --full
python forgesmith.py --report
```

These systems need data to work with. After 20-30 completed tasks, patterns start emerging and the suggestions get useful.

---

## Features

### Core: You Talk, Claude Works
- **Conversational interface** — describe what you want in plain English. Claude creates tasks, dispatches agents, and reports back. You never touch the CLI directly.
- **Multi-agent orchestration** — developer, tester, security reviewer, code reviewer, frontend designer, planner, debugger, integration tester, and more. Claude picks the right role for the job.
- **Dev-test loop** — agents write code, run tests, fix failures, and iterate. Up to 5 cycles per task before giving up.

### Self-Improvement (runs automatically)
- **ForgeSmith reflexion** — after every task, agents reflect on what worked and what didn't. Lessons are stored and injected into future prompts.
- **GEPA (Genetic Evolutionary Prompt Architecture)** — A/B tests prompt variants overnight. Winners get promoted. Losers get retired. Your agents get better while you sleep.
- **SIMBA (vector memory)** — embeds past episodes and retrieves the most relevant ones for each new task. Uses Ollama locally — no external API needed.
- **MemRL (q-value learning)** — tracks which reflexion episodes actually helped and adjusts injection weights over time.
- **Nightly scheduled job** — ForgeSmith runs on cron (Linux/WSL) or Windows Task Scheduler. Setup wizard configures it automatically for your OS.

### Reliability & Recovery
- **Autoresearch loop** — when a task fails (blocked, early-terminated, rate-limited), the orchestrator automatically cleans up, resets, and retries with a fresh agent. Up to 3 retries per task with git branch cleanup between attempts.
- **Compaction protection** — agents maintain `.forge-state.json` with current progress. If context compaction hits mid-task, the agent resumes from where it left off instead of starting over.
- **Soft checkpoints** — streaming monitor saves periodic checkpoints during long agent runs. If an agent is killed, the next attempt gets the checkpoint context.
- **Retry with jitter** — API calls use exponential backoff (500ms base, 25% jitter, 32s cap) with automatic model fallback after 3 consecutive overloaded errors.
- **Tool result persistence** — large agent outputs (>50KB) are saved to disk instead of stuffing the context window. Prevents compaction thrashing.

### Security
- **Bash security filter** — 12+ regex-based security checks ported from Claude Code's production system. Blocks command injection, IFS manipulation, process substitution, and more.
- **Lesson sanitizer** — agent-generated lessons are sanitized before storage to prevent prompt injection via the learning loop.
- **Skill integrity verification** — SHA-256 manifest of all prompt/skill files. Detects tampering before agent dispatch.
- **Randomized untrusted content delimiters** — task descriptions wrapped in unpredictable boundaries to prevent injection from task content.

### Architecture
- **Pure Python, zero dependencies** — stdlib only. No pip install, no requirements.txt, no virtual environments. Copy the files and go.
- **Cost-based model routing** — simple tasks go to Sonnet (cheaper, faster), complex tasks go to Opus (smarter). Configurable per-role.
- **Abort controller hierarchy** — parent-child subprocess management with WeakRef-based cleanup. No orphaned processes.
- **Prompt cache optimization** — static prompt sections cached across tasks; only dynamic context (task description, lessons, episodes) changes per dispatch.
- **Knowledge graph** — PageRank-based episode importance scoring. Lessons that help many tasks bubble up.


## Limitations

Being honest here:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent will spend 8 turns reading files and never write a line of code. The "bias for action" prompts help, but don't eliminate it.
- **Git worktree merges occasionally need manual intervention.** When agents work in isolated worktrees, the merge back to main doesn't always go cleanly. You might need to resolve conflicts yourself.
- **Self-improvement needs 20-30 tasks before patterns emerge.** ForgeSmith, GEPA, and SIMBA are great once they have data. On a fresh install, they don't do much.
- **The tester role depends on your project having a working test suite.** If your tests are broken or don't exist, the dev-test loop can't help.
- **Early termination kills agents at 10 turns of pure reading.** This is intentional — it prevents wasted money on stuck agents. But some legitimately complex tasks need more exploration time, and they get killed too early.
- **Ollama integration is optional and a bit fiddly.** Vector memory and local model routing require a running Ollama instance. Setup isn't hard, but it's another moving part.
- **The dashboard is a CLI script, not a web UI.** It prints a text report. Works fine, but don't expect charts.
- **Not magic.** Agents still fail, get confused, waste turns, and produce mediocre code sometimes. EQUIPA makes them *better* over time, but it doesn't make them perfect.

---

## Installation

### Prerequisites

- **Python 3.10+** (no packages to install — it's all stdlib)
- **Claude CLI** or **Claude Desktop** with MCP support
- **Git** (for worktree isolation and agent operations)
- **SQLite3** (comes with Python)
- **Ollama** (optional — for vector memory and local model routing)

### Step by step

```bash
# 1. Clone
git clone https://github.com/sbknana/equipa.git
cd equipa

# 2. Run the interactive setup
python equipa_setup.py
```

The setup wizard handles:
- Creating the SQLite database with the full schema (30+ tables)
- Generating `dispatch_config.json` with sensible defaults
- Creating the MCP server config for Claude Desktop
- Generating a `.mcp.json` for project-level MCP registration
- Creating `CLAUDE.md` so Claude understands your project

```bash
# 3. Verify everything works
python -m equipa --help

# 4. Run database migrations (if upgrading from an older version)
python db_migrate.py
```

### Setting up the self-improvement cron

```bash
# Run ForgeSmith nightly at 2 AM
0 2 * * * cd /path/to/equipa && python forgesmith.py --full >> /var/log/forgesmith.log 2>&1

# Run SIMBA weekly on Sundays
0 3 * * 0 cd /path/to/equipa && python forgesmith_simba.py >> /var/log/simba.log 2>&1
```

---

## Configuration

The main config file is `dispatch_config.json`. Key settings:

| Setting | What it does |
|---------|-------------|
| `max_turns` | How many conversation turns an agent gets before being stopped. Default: 25. |
| `cost_limit` | Maximum spend per task in USD. Scales with task complexity. |
| `auto_routing` | When `true`, picks the cheapest model that can handle the task complexity. |
| `model_overrides` | Pin specific roles to specific models. e.g., always use Opus for security reviews. |
| `features.vector_memory` | Enable semantic search over past episodes (requires Ollama). |
| `features.knowledge_graph` | Enable graph-based lesson ranking with PageRank. |
| `features.prompt_cache_split` | Split system prompts for better cache hit rates. |

Feature flags live under a `features` key and default to sensible values. You only need to touch them if you want to turn something on or off.

---

## Tech Stack

For folks who want to contribute or understand the internals:

- **Python 3.10+** — pure stdlib, no external packages
- **SQLite** — all state lives in one database file (30+ tables, versioned migrations)
- **MCP (Model Context Protocol)** — how Claude talks to EQUIPA
- **Claude API** — via CLI subprocess calls (no SDK dependency)
- **Ollama** (optional) — local embeddings for vector memory
- **Git worktrees** — agent isolation so parallel work doesn't collide

The codebase is about 21 modules in `equipa/`, plus ForgeSmith and friends as standalone scripts. 334+ tests passing.

---

## License

See [LICENSE](LICENSE) for details.
---

## Related Documentation

- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
