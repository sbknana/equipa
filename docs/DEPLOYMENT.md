# DEPLOYMENT.md

## Table of Contents

- [DEPLOYMENT.md](#deploymentmd)
  - [TL;DR](#tldr)
- [Open Claude Desktop / claude.ai and start talking. That's it.](#open-claude-desktop-claudeai-and-start-talking-thats-it)
- [Say: "Create a task to add input validation to the login form"](#say-create-a-task-to-add-input-validation-to-the-login-form)
  - [Prerequisites](#prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repo](#1-clone-the-repo)
    - [2. Run the setup wizard](#2-run-the-setup-wizard)
    - [3. Set your API key](#3-set-your-api-key)
    - [4. Verify the install](#4-verify-the-install)
    - [5. Start talking to Claude](#5-start-talking-to-claude)
  - [Environment Variables](#environment-variables)
  - [How It Actually Works](#how-it-actually-works)
    - [The Conversational Model (Primary)](#the-conversational-model-primary)
    - [The CLI (For Automation / Scripting)](#the-cli-for-automation-scripting)
- [Dispatch a specific task](#dispatch-a-specific-task)
- [Auto-dispatch: scan for pending work and run it](#auto-dispatch-scan-for-pending-work-and-run-it)
- [Dispatch with parallel goals](#dispatch-with-parallel-goals)
- [Run the MCP server directly](#run-the-mcp-server-directly)
    - [The Dev-Test Loop](#the-dev-test-loop)
    - [The Autoresearch Loop](#the-autoresearch-loop)
  - [Running in Production](#running-in-production)
    - [With systemd (Linux)](#with-systemd-linux)
    - [ForgeSmith Nightly Self-Improvement](#forgesmith-nightly-self-improvement)
- [Add:](#add)
  - [Docker](#docker)
- [No pip install needed — zero dependencies](#no-pip-install-needed-zero-dependencies)
  - [Database Migrations](#database-migrations)
  - [Running Tests](#running-tests)
- [Run a specific test file](#run-a-specific-test-file)
- [Run with coverage (if you have pytest-cov installed)](#run-with-coverage-if-you-have-pytest-cov-installed)
  - [Key Features Worth Knowing About](#key-features-worth-knowing-about)
  - [Troubleshooting](#troubleshooting)
    - ["ANTHROPIC_API_KEY not set"](#anthropic_api_key-not-set)
- [Or add to .env file in the project root](#or-add-to-env-file-in-the-project-root)
    - [Database "no such table" errors](#database-no-such-table-errors)
    - [MCP server won't connect to Claude](#mcp-server-wont-connect-to-claude)
    - [Agent stuck in a loop / analysis paralysis](#agent-stuck-in-a-loop-analysis-paralysis)
    - [Git worktree merge conflicts](#git-worktree-merge-conflicts)
    - [Port 3000 already in use (MCP server)](#port-3000-already-in-use-mcp-server)
- [Or change the port in your MCP config](#or-change-the-port-in-your-mcp-config)
    - ["Overloaded" errors from Anthropic API](#overloaded-errors-from-anthropic-api)
    - [Tests fail on fresh clone](#tests-fail-on-fresh-clone)
    - [ForgeSmith says "insufficient data"](#forgesmith-says-insufficient-data)
    - [Ollama connection refused](#ollama-connection-refused)
  - [Current Limitations](#current-limitations)
  - [Project Structure (Quick Reference)](#project-structure-quick-reference)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
git clone https://github.com/anthropics/equipa.git
cd equipa
python3 equipa_setup.py          # interactive wizard — sets up DB, config, everything
# Open Claude Desktop / claude.ai and start talking. That's it.
# Say: "Create a task to add input validation to the login form"
```

The setup wizard handles database creation, config generation, MCP server wiring, and optional ForgeSmith cron scheduling. You don't need to touch the CLI directly — just talk to Claude.

---

## Prerequisites

| Tool | Version | Why | Install |
|------|---------|-----|---------|
| Python | 3.10+ | Everything runs on it | [python.org](https://www.python.org/downloads/) |
| Git | 2.20+ | Worktree isolation, branch management | [git-scm.com](https://git-scm.com/) |
| SQLite3 | 3.35+ | Ships with Python, but check it's not ancient | Usually bundled |
| Claude CLI or Claude Desktop | Latest | The conversational interface — how you actually use EQUIPA | [claude.ai](https://claude.ai/) |
| Anthropic API key | — | Agents need it to think | [console.anthropic.com](https://console.anthropic.com/) |

**Optional:**

| Tool | Version | Why |
|------|---------|-----|
| Ollama | 0.1.0+ | Local model support, vector embeddings | [ollama.com](https://ollama.com/) |
| `gh` CLI | 2.0+ | Auto repo setup, PR creation | [cli.github.com](https://cli.github.com/) |

Verify you're good:

```bash
python3 --version   # 3.10+
git --version        # 2.20+
sqlite3 --version    # 3.35+
```

---

## Step-by-Step Setup

### 1. Clone the repo

```bash
git clone https://github.com/anthropics/equipa.git
cd equipa
```

### 2. Run the setup wizard

```bash
python3 equipa_setup.py
```

This walks you through everything interactively:
- Checks prerequisites
- Creates the SQLite database and runs all migrations
- Generates `dispatch_config.json` with your preferences
- Wires up the MCP server config (so Claude can talk to EQUIPA)
- Generates a `.claude.md` with standing instructions
- Optionally sets up ForgeSmith nightly cron (Linux/WSL) or Windows Task Scheduler

### 3. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

EQUIPA's env loader picks this up automatically.

### 4. Verify the install

```bash
python3 -m equipa --help
```

You should see the CLI options. But again — most people never use the CLI directly.

### 5. Start talking to Claude

Open Claude Desktop or your Claude interface. The MCP server connects EQUIPA to Claude automatically. Just say things like:

- *"What tasks are open right now?"*
- *"Create a task to refactor the payment module"*
- *"Dispatch task 42 — it's a medium complexity Python fix"*
- *"Show me the agent logs for the last run"*
- *"What's blocking progress on project 3?"*

Claude reads the database, dispatches agents, monitors progress, and reports results back to you in plain English. No commands to memorize.

---

## Environment Variables

| Name | Description | Example | Required? |
|------|-------------|---------|-----------|
| `ANTHROPIC_API_KEY` | API key for Claude agents | `sk-ant-api03-...` | **Yes** |
| `EQUIPA_DB_PATH` | Path to SQLite database | `./forge.db` | No (default: `./forge.db`) |
| `EQUIPA_CONFIG_PATH` | Path to dispatch config | `./dispatch_config.json` | No (default: auto-detected) |
| `EQUIPA_LOG_DIR` | Directory for agent logs | `./logs/` | No (default: `./logs/`) |
| `EQUIPA_CHECKPOINTS_DIR` | Checkpoint storage | `./checkpoints/` | No (default: `./checkpoints/`) |
| `OLLAMA_BASE_URL` | Ollama server for local models | `http://localhost:11434` | No (only if using Ollama) |
| `OLLAMA_MODEL` | Default Ollama model name | `codellama:13b` | No |
| `EQUIPA_MAX_COST` | Global cost limit (USD) | `5.00` | No (default: per-config) |
| `EQUIPA_PROJECT_DIR` | Override project working directory | `/home/user/myproject` | No |

---

## How It Actually Works

### The Conversational Model (Primary)

You talk to Claude. Claude runs EQUIPA. That's the main loop.

```
You: "The login page has an XSS vulnerability in the search field. Fix it."
  ↓
Claude: Creates a task, picks the right agent role (security_reviewer + developer),
        dispatches it, monitors progress, reports back.
  ↓
You: "Did the tests pass?"
  ↓
Claude: Checks the tester agent results, shows you what passed and what didn't.
```

The MCP server exposes tools like `task_create`, `dispatch`, `task_status`, `agent_logs`, `lessons`, and `session_notes` — but Claude calls these for you. You just talk.

### The CLI (For Automation / Scripting)

If you need to script things or run from CI:

```bash
# Dispatch a specific task
python3 -m equipa --task 42

# Auto-dispatch: scan for pending work and run it
python3 -m equipa --auto

# Dispatch with parallel goals
python3 -m equipa --goals goals.json

# Run the MCP server directly
python3 -m equipa --mcp-server
```

### The Dev-Test Loop

When an agent writes code, EQUIPA automatically:
1. Runs the developer agent to write the fix
2. Runs the tester agent to verify it
3. If tests fail, feeds failures back to the developer
4. Repeats until tests pass or budget runs out

### The Autoresearch Loop

Failed tasks automatically retry up to 3 times. Between attempts:
- Git branches get cleaned up
- Cross-attempt memory injects what went wrong last time
- The agent gets a different angle of attack each retry

No manual intervention needed.

---

## Running in Production

### With systemd (Linux)

Create `/etc/systemd/system/equipa-mcp.service`:

```ini
[Unit]
Description=EQUIPA MCP Server
After=network.target

[Service]
Type=simple
User=equipa
WorkingDirectory=/opt/equipa
Environment=ANTHROPIC_API_KEY=sk-ant-...
Environment=EQUIPA_DB_PATH=/opt/equipa/forge.db
ExecStart=/usr/bin/python3 -m equipa --mcp-server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable equipa-mcp
sudo systemctl start equipa-mcp
sudo journalctl -u equipa-mcp -f   # watch logs
```

### ForgeSmith Nightly Self-Improvement

The setup wizard can configure this for you, but manually:

**Linux/WSL (cron):**
```bash
crontab -e
# Add:
0 3 * * * cd /opt/equipa && python3 forgesmith.py --full >> /var/log/forgesmith.log 2>&1
```

**Windows (Task Scheduler):**
```powershell
schtasks /create /tn "ForgeSmith Nightly" /tr "python3 C:\equipa\forgesmith.py --full" /sc daily /st 03:00
```

ForgeSmith analyzes agent performance, extracts lessons, evolves prompts via GEPA, generates SIMBA rules, and prunes what doesn't work. It needs 20-30 completed tasks before patterns emerge — don't expect magic on day one.

---

## Docker

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y git sqlite3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# No pip install needed — zero dependencies
RUN python3 equipa_setup.py --non-interactive || true

ENV ANTHROPIC_API_KEY=""
ENV EQUIPA_DB_PATH="/app/data/forge.db"

VOLUME ["/app/data", "/app/logs", "/app/checkpoints"]

EXPOSE 3000

CMD ["python3", "-m", "equipa", "--mcp-server"]
```

```bash
docker build -t equipa .
docker run -d \
  --name equipa \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -v equipa-data:/app/data \
  -v equipa-logs:/app/logs \
  equipa
```

**Note:** The `--non-interactive` flag on setup is a suggestion — you may need to run the setup wizard manually the first time and mount the resulting config.

---

## Database Migrations

The database schema evolves. Migrations run automatically during setup, but if you're upgrading an existing install:

```bash
python3 db_migrate.py
```

This auto-detects your current version, backs up the database, and migrates forward. Currently at schema v7.

To benchmark migrations (if you're paranoid):
```bash
python3 tools/benchmark_migrations.py
```

---

## Running Tests

```bash
python3 -m pytest tests/ -v
```

There are 680+ tests. They run fast because everything is pure Python stdlib — no fixtures downloading half the internet.

```bash
# Run a specific test file
python3 -m pytest tests/test_early_termination.py -v

# Run with coverage (if you have pytest-cov installed)
python3 -m pytest tests/ --cov=equipa --cov-report=term-missing
```

---

## Key Features Worth Knowing About

**Compaction-safe state persistence** — Agents write `.forge-state.json` as they work. If context gets compacted (Claude's memory management), they can pick up where they left off instead of starting over.

**Soft checkpoints** — The streaming monitor saves periodic snapshots. If an agent gets killed, the replacement inherits the dead agent's progress.

**Retry with jitter** — API calls use exponential backoff (500ms base, 25% jitter, 32s cap). After 3 overloaded errors, it automatically falls back from opus to sonnet.

**Tool result persistence** — Outputs over 50KB get saved to disk instead of stuffing them into context. Prevents compaction thrashing when test suites are verbose.

**Bash security filter** — 12+ regex checks blocking command injection, IFS manipulation, process substitution, and more. Ported from Claude Code's production security model.

**Abort controller** — WeakRef-based parent-child subprocess hierarchy. When you kill a parent, children die cleanly. No orphan processes.

**Prompt cache optimization** — Static prompt sections are cached across tasks. Only task-specific context changes per dispatch, which saves money.

**Knowledge graph with PageRank** — Lessons that help many tasks score higher and get injected more often. Episodes that only help one niche task get deprioritized.

**Cost controls** — Configurable per-task cost limits that actually kill runaway agents. The cost breaker scales with task complexity.

**9 specialized agent roles** — Developer, tester, code reviewer, security reviewer, architect, planner, evaluator, integration tester, and documentation writer. Each gets language-aware prompts.

---

## Troubleshooting

### "ANTHROPIC_API_KEY not set"

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# Or add to .env file in the project root
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### Database "no such table" errors

Migrations probably didn't run. Fix it:

```bash
python3 db_migrate.py
```

### MCP server won't connect to Claude

Check that `equipa_setup.py` generated the MCP config. Look for:
- `.mcp.json` in the project root
- Or the Claude Desktop config pointing to the right path

```bash
cat .mcp.json   # should reference the MCP server command
```

### Agent stuck in a loop / analysis paralysis

This happens. EQUIPA has loop detection (fingerprinting repeated outputs) and early termination (stuck phrase detection, monologue detection, cost breakers). But complex tasks can still confuse agents.

Things to try:
- Break the task into smaller pieces
- Add more context to the task description
- Check if the project's test suite actually works before dispatching a tester

### Git worktree merge conflicts

Worktree isolation means each agent works on its own branch. Merges usually work, but occasionally need manual intervention:

```bash
git worktree list          # see what's active
git worktree remove <path> # clean up a stuck worktree
git branch -D <branch>     # delete the branch if needed
```

### Port 3000 already in use (MCP server)

```bash
lsof -i :3000              # find what's using it
kill -9 <PID>              # kill it
# Or change the port in your MCP config
```

### "Overloaded" errors from Anthropic API

EQUIPA handles these automatically with exponential backoff and model fallback. If it's persistent, you're hitting rate limits. Wait a bit or check your API plan.

### Tests fail on fresh clone

Make sure you're on Python 3.10+. Some tests use features not available on 3.9.

```bash
python3 --version   # check this first
```

### ForgeSmith says "insufficient data"

It needs 20-30 completed tasks before it can extract meaningful patterns. Keep using EQUIPA and it'll get there. This part isn't instant.

### Ollama connection refused

If you're using local models:

```bash
ollama serve                    # start the server
curl http://localhost:11434     # verify it's up
ollama list                     # check you have models pulled
```

---

## Current Limitations

Honest list. Read this before you get frustrated.

- **Agents still get stuck on complex tasks.** Analysis paralysis is real. The paralysis retry system helps, but some tasks are just too ambiguous for an agent to figure out alone. Break them down.
- **Git worktree merges occasionally need manual intervention.** The isolation works most of the time, but merge conflicts happen. Don't call it safe or reliable yet — it's being refined.
- **Self-improvement takes time.** ForgeSmith + GEPA + SIMBA need 20-30 completed tasks before patterns emerge. The first week, you're training it. It's not magic.
- **Tester role depends on your project having a working test suite.** If your tests are broken or nonexistent, the tester agent can't help. Fix your tests first.
- **Early termination can be too aggressive.** The 10-turn reading limit kills agents that are legitimately analyzing complex codebases. Some tasks need more exploration time than the system allows.
- **Agents still waste turns sometimes.** They read files they already read, they restate the problem instead of solving it, they write plans instead of code. The early termination and monologue detection catch some of this, but not all.
- **The knowledge graph needs volume.** PageRank-based lesson scoring only gets interesting with dozens of episodes. Small installs won't see much benefit from graph reranking.
- **Vector memory requires Ollama.** If you're not running Ollama locally, the embedding-based similarity features are disabled. Keyword matching still works, but it's less precise.

---

## Project Structure (Quick Reference)

```
equipa/              # Core package — CLI, agents, dispatch, config, prompts
  cli.py             # Entry point
  dispatch.py        # Task scanning and parallel dispatch  
  agent_runner.py    # Subprocess management, retry logic
  bash_security.py   # Command injection prevention
  mcp_server.py      # MCP protocol server for Claude
  prompts.py         # Prompt building with cache split
  monitoring.py      # Loop detection, budget tracking
  ...

forgesmith.py        # Self-improvement engine (nightly analysis)
forgesmith_gepa.py   # Genetic prompt evolution
forgesmith_simba.py  # Rule generation from episodes
forgesmith_litm.py   # Lost-in-the-middle attention tuner

scripts/             # Standalone utilities
tools/               # Dashboard, arena, benchmarks
tests/               # 680+ tests
hooks/               # Pre/post agent hooks (lint, build check)
skills/              # Agent skill definitions (SARIF parsing, etc.)
```
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
