# EQUIPA

## Table of Contents

- [EQUIPA](#equipa)
  - [What is this?](#what-is-this)
  - [Screenshots](#screenshots)
  - [Quick Start](#quick-start)
  - [How to Use](#how-to-use)
    - [The main way: just talk to Claude](#the-main-way-just-talk-to-claude)
    - [The dev-test loop](#the-dev-test-loop)
    - [The CLI (for automation)](#the-cli-for-automation)
  - [Features](#features)
  - [Limitations](#limitations)
  - [Installation](#installation)
    - [Prerequisites](#prerequisites)
    - [Step-by-step](#step-by-step)
- [Clone](#clone)
- [Run the interactive setup wizard](#run-the-interactive-setup-wizard)
    - [Manual setup (if you prefer)](#manual-setup-if-you-prefer)
- [Initialize the database](#initialize-the-database)
- [Copy dispatch_config.example.json and edit it](#copy-dispatch_configexamplejson-and-edit-it)
- [Set your API key](#set-your-api-key)
- [Start the MCP server](#start-the-mcp-server)
    - [Database migrations](#database-migrations)
  - [Configuration](#configuration)
  - [Tech Stack](#tech-stack)
  - [License](#license)
  - [Related Documentation](#related-documentation)

**Your AI development team — talk to Claude, get code written, tested, and reviewed.**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![Tests](https://img.shields.io/badge/tests-680%2B-green)

![EQUIPA Dashboard](screenshots/dashboard.png)

## What is this?

EQUIPA coordinates AI agents to write, test, review, and secure your code. You talk to Claude in plain English — "add pagination to the users endpoint" — and EQUIPA dispatches specialized agents, monitors their work, retries when tests fail, and reports back with results. It's a team of 9 AI roles that learn from their mistakes over time.

The name is European Portuguese for "team." That's what it is — an AI team you manage by talking to it.

## Screenshots

![Task Dashboard](screenshots/dashboard.png)
*The dashboard shows task status across all your projects — what's done, what's in progress, what's stuck.*

![Dispatch Summary](screenshots/dispatch-summary.png)
*After dispatching work, you get a summary of what each agent did, how much it cost, and whether tests passed.*

![ForgeSmith Report](screenshots/forgesmith-report.png)
*ForgeSmith's nightly self-improvement report — showing what it learned and what config changes it made.*

![Agent Logs](screenshots/agent-logs.png)
*Detailed agent logs let you see exactly what an agent tried, where it got stuck, and what it produced.*

## Quick Start

1. **Clone the repo and run the setup wizard:**
   ```bash
   git clone https://github.com/your-org/equipa.git
   cd equipa
   python equipa_setup.py
   ```

2. **The wizard walks you through everything** — database setup, config generation, MCP server config, and cron jobs. Follow the prompts.

3. **Add EQUIPA to your Claude project** by pointing Claude Desktop or Claude Code at the generated `.mcp.json` file.

4. **Talk to Claude:**
   > "Create a task to add input validation to the signup form, then dispatch it."

5. **Check results:**
   > "What's the status of my tasks?" or "Show me the dashboard."

That's it. Claude handles dispatching agents, monitoring progress, and reporting back.

## How to Use

### The main way: just talk to Claude

EQUIPA is designed to be conversational. You don't need to learn a CLI or memorize commands. Claude is the interface.

**Creating work:**
> "Add a task for project X: refactor the database connection pool to use async."

**Dispatching agents:**
> "Dispatch task 42" or "Run all pending tasks for project X."

Claude picks the right agent role (developer, tester, security reviewer, etc.), selects the appropriate model based on task complexity, and monitors the run.

**Checking progress:**
> "What happened with task 42?" or "Show me today's completed work."

**Handling failures:**
> "Task 42 failed — retry it" or "What went wrong with the last dispatch?"

The agents work in git worktrees so they don't step on each other. When a developer finishes, a tester automatically runs to verify the work. If tests fail, the developer gets another shot with the failure context injected.

### The dev-test loop

This is where EQUIPA earns its keep. When you dispatch a development task:

1. A **developer agent** writes the code
2. A **tester agent** runs the test suite
3. If tests fail, the developer gets the failure output and tries again
4. This loops until tests pass or the budget runs out

No manual intervention. The tester depends on your project having a working test suite — if there are no tests, it skips this step.

### The CLI (for automation)

Most users never touch this directly, but it exists:

```bash
python -m equipa --task 42 --role developer
python -m equipa --dispatch-auto --project myapp
python -m equipa --mcp-server  # starts the MCP server
```

Useful for CI pipelines, cron jobs, or if you just prefer terminals.

## Features

- **Talk to Claude, get results** — no CLI to learn. Say what you want in plain English, Claude runs EQUIPA behind the scenes.
- **9 specialized agent roles** — developer, tester, code reviewer, security reviewer, architect, integration tester, planner, evaluator, and documentation writer. Each has language-aware prompts tuned for their job.
- **Dev-test iteration loop** — agents retry until tests pass, not just until they think they're done.
- **Autoresearch loop** — failed tasks automatically retry up to 3 times with git branch cleanup between attempts. No babysitting.
- **Self-improving agents** — ForgeSmith analyzes completed work nightly. GEPA evolves agent prompts. SIMBA extracts behavioral rules. It's a closed loop that gets better over time.
- **Knowledge graph with PageRank** — lessons that help many tasks score higher and get injected more often into agent context.
- **Cost controls that actually work** — cost limits per task, per complexity tier. Runaway agents get killed. Model auto-downgrades (opus→sonnet) after repeated overload errors.
- **Compaction-safe state persistence** — agents maintain `.forge-state.json` so they can resume after context compaction instead of starting over.
- **Soft checkpoints** — the streaming monitor saves periodic progress. If an agent gets killed, its replacement picks up where it left off.
- **Retry with jitter** — API calls use exponential backoff (500ms base, 25% jitter, 32s cap) with automatic model fallback after overload errors.
- **Tool result persistence** — large outputs (>50KB) get saved to disk instead of staying in context. Prevents compaction thrashing on verbose test suites.
- **Bash security filter** — 12+ regex checks blocking command injection, IFS manipulation, process substitution, and more. Ported from Claude Code's production security model.
- **Abort controller** — WeakRef-based parent-child subprocess hierarchy. Clean kills, no orphan processes.
- **Prompt cache optimization** — base prompts are cached across tasks. Only task-specific context changes per dispatch, saving tokens.
- **Cross-platform scheduling** — ForgeSmith nightly runs on cron (Linux/WSL) or Windows Task Scheduler. The setup wizard auto-configures it.
- **Zero dependencies** — pure Python stdlib. Copy the files and run. No pip install, no venv wrestling.
- **Loop detection** — detects when agents are stuck repeating themselves (same tool calls, monologuing, alternating patterns) and kills them early.
- **Anti-compaction context** — persistent state survives Claude's context window compaction, so agents don't lose track of what they were doing.

## Limitations

Let's be honest about the rough edges:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent spends all its turns reading code and never writes anything. EQUIPA has paralysis detection and retry logic, but it's not perfect.
- **Git worktree merges occasionally need manual intervention.** The isolation works well most of the time, but merge conflicts happen. You'll need to step in sometimes.
- **Self-improvement needs 20-30 tasks to show results.** ForgeSmith, GEPA, and SIMBA need data to learn from. On a fresh install, they don't do much. Give it a few weeks of real work.
- **The tester role depends on your project having a working test suite.** If your project has no tests, the dev-test loop is just.. the dev loop. The tester will report "no tests found" and move on.
- **Early termination can be aggressive.** Agents get killed at 10 turns of reading without writing. Some legitimately complex tasks need more exploration time, and they get cut short.
- **Cost estimation is approximate.** Token counting is a rough heuristic. You won't get charged more than your limits, but the reported costs may not match your Anthropic invoice exactly.
- **Ollama/local model support exists but is less tested** than the Claude API path. Expect some roughness there.
- **The MCP server is the primary integration point.** If your Claude setup can't use MCP, you're stuck with the CLI.

This is not magic. Agents still fail, get stuck, and waste turns sometimes. But they fail less often as the system learns from its mistakes.

## Installation

### Prerequisites

- **Python 3.10+** (no packages to install — it's all stdlib)
- **Git** (for worktree isolation)
- **Claude CLI** or **Claude Desktop** with MCP support
- **An Anthropic API key** (for the agents themselves)
- Optional: **Ollama** for local embeddings (vector memory features)

### Step-by-step

```bash
# Clone
git clone https://github.com/your-org/equipa.git
cd equipa

# Run the interactive setup wizard
python equipa_setup.py
```

The wizard will:
1. Check your prerequisites
2. Create the SQLite database with all tables
3. Generate `dispatch_config.json` with your settings
4. Create the MCP server config for Claude
5. Generate a `CLAUDE.md` project brief
6. Optionally set up ForgeSmith nightly cron jobs

### Manual setup (if you prefer)

```bash
# Initialize the database
python db_migrate.py

# Copy dispatch_config.example.json and edit it
cp dispatch_config.example.json dispatch_config.json

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# Start the MCP server
python -m equipa --mcp-server
```

### Database migrations

If you're upgrading from an older version:

```bash
python db_migrate.py
```

It auto-detects the current schema version and runs incremental migrations. Backs up your database first.

## Configuration

All configuration lives in `dispatch_config.json`:

| Setting | What it does |
|---------|-------------|
| `max_turns` | How many turns an agent gets before being killed (default: 50) |
| `cost_limit` | Maximum spend per task in USD |
| `model_overrides` | Pin specific roles to specific models |
| `auto_routing` | Let EQUIPA pick the model based on task complexity |
| `features.vector_memory` | Enable Ollama-powered semantic memory |
| `features.knowledge_graph` | Enable PageRank-based lesson ranking |
| `features.rlm_decompose` | Enable code decomposition for large repos |

**Feature flags** control experimental features. They all default to sensible values — you don't need to touch them unless you want to.

**Standing orders** (in `standing_orders/`) let you give persistent instructions to each agent role. These are injected into every dispatch for that role.

**Language prompts** (in `language_prompts/`) provide language-specific guidance. EQUIPA auto-detects your project language and injects the right one.

## Tech Stack

For developers who want to contribute or understand the internals:

- **Python 3.10+** — zero external dependencies, pure stdlib
- **SQLite** — single-file database for tasks, episodes, lessons, and agent history
- **MCP protocol** — JSON-RPC over stdio for Claude integration
- **Git worktrees** — agent isolation so parallel tasks don't conflict
- **Claude API** — primary agent backend (Opus, Sonnet, Haiku tiers)
- **Ollama** (optional) — local embeddings for vector memory

**Key modules:**
- `equipa/` — core package (24 modules)
- `equipa/cli.py` — entry point and CLI
- `equipa/mcp_server.py` — MCP server for Claude
- `equipa/dispatch.py` — task scoring and parallel dispatch
- `equipa/agent_runner.py` — agent execution with retry logic
- `equipa/bash_security.py` — command security filter
- `equipa/monitoring.py` — loop detection and progress tracking
- `forgesmith.py` — nightly self-improvement engine
- `forgesmith_gepa.py` — prompt evolution (GEPA)
- `forgesmith_simba.py` — behavioral rule extraction (SIMBA)

**680+ tests** covering security filters, loop detection, database migrations, prompt caching, and more.

## License

See [LICENSE](LICENSE) for details.
---

## Related Documentation

- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
