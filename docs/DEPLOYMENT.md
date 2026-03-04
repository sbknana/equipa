# DEPLOYMENT.md — Itzamna (ForgeTeam)

## Table of Contents

- [DEPLOYMENT.md — Itzamna (ForgeTeam)](#deploymentmd-itzamna-forgeteam)
  - [TL;DR](#tldr)
- [Follow the interactive prompts — it handles DB, config, and file placement](#follow-the-interactive-prompts-it-handles-db-config-and-file-placement)
- [Then run your first task:](#then-run-your-first-task)
  - [Prerequisites](#prerequisites)
    - [Verify prerequisites](#verify-prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repository](#1-clone-the-repository)
    - [2. Run the interactive installer](#2-run-the-interactive-installer)
    - [3. (Alternative) Manual database setup](#3-alternative-manual-database-setup)
- [Create the database directory](#create-the-database-directory)
- [Run the schema — the setup script normally does this, but you can do it manually:](#run-the-schema-the-setup-script-normally-does-this-but-you-can-do-it-manually)
- [Run migrations to ensure you're at the latest version](#run-migrations-to-ensure-youre-at-the-latest-version)
    - [4. Verify the database](#4-verify-the-database)
- [Should report current schema version and apply any pending migrations](#should-report-current-schema-version-and-apply-any-pending-migrations)
    - [5. Run a smoke test](#5-run-a-smoke-test)
- [Scan for pending work across all projects](#scan-for-pending-work-across-all-projects)
- [Or run the test suite](#or-run-the-test-suite)
    - [6. Run your first task](#6-run-your-first-task)
- [Run a specific task by ID](#run-a-specific-task-by-id)
- [Run with dev+test loop](#run-with-devtest-loop)
- [Run with a specific role](#run-with-a-specific-role)
  - [Environment Variables](#environment-variables)
- [Set your API key](#set-your-api-key)
  - [Running in Production](#running-in-production)
    - [Option A: Cron-based scheduling (recommended)](#option-a-cron-based-scheduling-recommended)
- [Edit crontab](#edit-crontab)
- [Run ForgeSmith analysis every 6 hours](#run-forgesmith-analysis-every-6-hours)
- [Run SIMBA rule generation daily](#run-simba-rule-generation-daily)
- [Run dispatch scan every 30 minutes](#run-dispatch-scan-every-30-minutes)
    - [Option B: systemd service](#option-b-systemd-service)
- [/etc/systemd/system/forgeteam-dispatch.service](#etcsystemdsystemforgeteam-dispatchservice)
- [/etc/systemd/system/forgeteam-dispatch.timer](#etcsystemdsystemforgeteam-dispatchtimer)
    - [Option C: Run commands directly](#option-c-run-commands-directly)
- [Full orchestration for a task](#full-orchestration-for-a-task)
- [Auto-dispatch across all projects](#auto-dispatch-across-all-projects)
- [Parallel goal execution](#parallel-goal-execution)
- [Run ForgeSmith analysis (dry run first!)](#run-forgesmith-analysis-dry-run-first)
- [Run ForgeSmith SIMBA rule generation](#run-forgesmith-simba-rule-generation)
- [Run ForgeSmith GEPA prompt evolution](#run-forgesmith-gepa-prompt-evolution)
- [Backfill episode data from logs](#backfill-episode-data-from-logs)
- [Performance dashboard](#performance-dashboard)
- [Performance analysis report](#performance-analysis-report)
- [Database migrations](#database-migrations)
- [ForgeArena adversarial testing](#forgearena-adversarial-testing)
  - [Docker](#docker)
- [Install system dependencies](#install-system-dependencies)
- [Install Node.js (for Claude Code CLI)](#install-nodejs-for-claude-code-cli)
- [Install Claude Code CLI](#install-claude-code-cli)
- [Set up working directory](#set-up-working-directory)
- [Copy project files](#copy-project-files)
- [Install Python dependencies (if any requirements file exists)](#install-python-dependencies-if-any-requirements-file-exists)
- [Create data directory](#create-data-directory)
- [Environment](#environment)
- [Run setup non-interactively or start with a specific command](#run-setup-non-interactively-or-start-with-a-specific-command)
- [Build](#build)
- [Run with API key and persistent data volume](#run-with-api-key-and-persistent-data-volume)
- [Run interactive setup](#run-interactive-setup)
- [Run a specific task](#run-a-specific-task)
  - [Troubleshooting](#troubleshooting)
    - [`ANTHROPIC_API_KEY` not set](#anthropic_api_key-not-set)
- [Or add to your shell profile:](#or-add-to-your-shell-profile)
    - [Claude Code CLI not found](#claude-code-cli-not-found)
- [Verify:](#verify)
- [If installed but not found, check your PATH:](#if-installed-but-not-found-check-your-path)
    - [Database locked / concurrent access errors](#database-locked-concurrent-access-errors)
- [SQLite doesn't handle heavy concurrent writes well](#sqlite-doesnt-handle-heavy-concurrent-writes-well)
- [Ensure only one dispatch process runs at a time](#ensure-only-one-dispatch-process-runs-at-a-time)
- [Check for stuck processes:](#check-for-stuck-processes)
- [Kill stale ones if needed:](#kill-stale-ones-if-needed)
    - [Database schema out of date](#database-schema-out-of-date)
- [This will detect the current version and apply all pending migrations](#this-will-detect-the-current-version-and-apply-all-pending-migrations)
    - [Port in use (Ollama)](#port-in-use-ollama)
- [Start Ollama if it's not running:](#start-ollama-if-its-not-running)
- [Or check if something else is on that port:](#or-check-if-something-else-is-on-that-port)
- [Use a custom URL:](#use-a-custom-url)
    - [Permission denied on project directories](#permission-denied-on-project-directories)
- [Ensure the running user has read/write access to project directories](#ensure-the-running-user-has-readwrite-access-to-project-directories)
- [For the database:](#for-the-database)
    - [Tests failing](#tests-failing)
- [Run the full test suite to identify issues:](#run-the-full-test-suite-to-identify-issues)
- [Run individual test files:](#run-individual-test-files)
    - [ForgeSmith changes seem wrong](#forgesmith-changes-seem-wrong)
- [Always dry-run first to preview changes:](#always-dry-run-first-to-preview-changes)
- [Check the ForgeSmith run history:](#check-the-forgesmith-run-history)
- [Rollback a specific run:](#rollback-a-specific-run)
    - ["No tasks found" when running dispatch](#no-tasks-found-when-running-dispatch)
- [Check what work is available:](#check-what-work-is-available)
- [Verify tasks exist in the database:](#verify-tasks-exist-in-the-database)
    - [Checkpoint recovery after crash](#checkpoint-recovery-after-crash)
- [The orchestrator will detect and resume from the last checkpoint automatically](#the-orchestrator-will-detect-and-resume-from-the-last-checkpoint-automatically)
- [To force a fresh start (discard checkpoints):](#to-force-a-fresh-start-discard-checkpoints)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
git clone <your-repo-url> && cd itzamna
python3 itzamna_setup.py
# Follow the interactive prompts — it handles DB, config, and file placement
# Then run your first task:
python3 forge_orchestrator.py --task <TASK_ID>
```

## Prerequisites

| Tool | Minimum Version | Required? | Install |
|------|----------------|-----------|---------|
| **Python** | 3.10+ | ✅ Yes | [python.org](https://www.python.org/downloads/) |
| **SQLite3** | 3.35+ | ✅ Yes | Usually bundled with Python; `sqlite3 --version` |
| **Claude Code CLI** (`claude`) | Latest | ✅ Yes | `npm install -g @anthropic-ai/claude-code` |
| **Git** | 2.30+ | ✅ Yes | [git-scm.com](https://git-scm.com/downloads) |
| **Node.js / npm** | 18+ | ✅ Yes (for Claude Code) | [nodejs.org](https://nodejs.org/) |
| **GitHub CLI** (`gh`) | 2.0+ | ⚠️ Optional (for repo setup) | [cli.github.com](https://cli.github.com/) |
| **Ollama** | Latest | ⚠️ Optional (local models) | [ollama.com](https://ollama.com/download) |
| **DSPy** | Latest | ⚠️ Optional (GEPA prompt evolution) | `pip install dspy-ai` |

### Verify prerequisites

```bash
python3 --version        # 3.10+
sqlite3 --version        # 3.35+
claude --version         # Should return a version
git --version            # 2.30+
node --version           # 18+
```

## Step-by-Step Setup

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd itzamna
```

### 2. Run the interactive installer

The project ships with a guided setup wizard that handles everything:

```bash
python3 itzamna_setup.py
```

This will walk you through:
- Prerequisites verification
- Install path selection
- SQLite database creation (runs schema SQL files)
- Configuration file generation
- MCP config setup
- CLAUDE.md generation
- Optional Sentinel and ForgeBot setup
- Optional ForgeSmith cron scheduling

### 3. (Alternative) Manual database setup

If you prefer manual setup or the wizard fails at the DB step:

```bash
# Create the database directory
mkdir -p ~/.forgeteam

# Run the schema — the setup script normally does this, but you can do it manually:
sqlite3 ~/.forgeteam/forge.db < schema.sql

# Run migrations to ensure you're at the latest version
python3 db_migrate.py
```

### 4. Verify the database

```bash
python3 db_migrate.py
# Should report current schema version and apply any pending migrations
```

### 5. Run a smoke test

```bash
# Scan for pending work across all projects
python3 forge_orchestrator.py --scan

# Or run the test suite
python3 -m pytest test_loop_detection.py test_early_termination.py test_agent_messages.py test_agent_actions.py -v
```

### 6. Run your first task

```bash
# Run a specific task by ID
python3 forge_orchestrator.py --task <TASK_ID>

# Run with dev+test loop
python3 forge_orchestrator.py --task <TASK_ID> --dev-test

# Run with a specific role
python3 forge_orchestrator.py --task <TASK_ID> --role developer
```

## Environment Variables

| Variable | Description | Example | Required? |
|----------|-------------|---------|-----------|
| `ANTHROPIC_API_KEY` | API key for Claude / Anthropic calls | `sk-ant-...` | ✅ Yes |
| `FORGE_DB_PATH` | Override path to the SQLite database | `~/.forgeteam/forge.db` | ❌ No (defaults set by config) |
| `FORGE_CONFIG_PATH` | Override path to config file | `~/.forgeteam/config.json` | ❌ No |
| `OLLAMA_BASE_URL` | Base URL for local Ollama instance | `http://localhost:11434` | ❌ No (only if using Ollama) |
| `OLLAMA_MODEL` | Default Ollama model name | `codellama:34b` | ❌ No |
| `GITHUB_TOKEN` | GitHub token for `gh` CLI operations | `ghp_...` | ❌ No (only for repo setup) |

> **Note:** Most configuration is managed through config files generated by `itzamna_setup.py`, not environment variables. The API key is the critical one to set.

```bash
# Set your API key
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

## Running in Production

### Option A: Cron-based scheduling (recommended)

The setup wizard can configure ForgeSmith cron jobs automatically. To do it manually:

```bash
# Edit crontab
crontab -e

# Run ForgeSmith analysis every 6 hours
0 */6 * * * cd /path/to/itzamna && python3 forgesmith.py --full >> /var/log/forgesmith.log 2>&1

# Run SIMBA rule generation daily
0 2 * * * cd /path/to/itzamna && python3 forgesmith_simba.py >> /var/log/forgesmith-simba.log 2>&1

# Run dispatch scan every 30 minutes
*/30 * * * * cd /path/to/itzamna && python3 forge_orchestrator.py --dispatch >> /var/log/forge-dispatch.log 2>&1
```

### Option B: systemd service

```ini
# /etc/systemd/system/forgeteam-dispatch.service
[Unit]
Description=ForgeTeam Auto-Dispatch
After=network.target

[Service]
Type=oneshot
User=your-user
WorkingDirectory=/path/to/itzamna
Environment=ANTHROPIC_API_KEY=sk-ant-your-key-here
ExecStart=/usr/bin/python3 forge_orchestrator.py --dispatch
StandardOutput=append:/var/log/forgeteam-dispatch.log
StandardError=append:/var/log/forgeteam-dispatch.log

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/forgeteam-dispatch.timer
[Unit]
Description=Run ForgeTeam dispatch every 30 minutes

[Timer]
OnCalendar=*:0/30
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now forgeteam-dispatch.timer
sudo systemctl status forgeteam-dispatch.timer
```

### Option C: Run commands directly

```bash
# Full orchestration for a task
python3 forge_orchestrator.py --task 42 --dev-test

# Auto-dispatch across all projects
python3 forge_orchestrator.py --dispatch

# Parallel goal execution
python3 forge_orchestrator.py --goals goals.json --parallel 3

# Run ForgeSmith analysis (dry run first!)
python3 forgesmith.py --full --dry-run
python3 forgesmith.py --full

# Run ForgeSmith SIMBA rule generation
python3 forgesmith_simba.py --dry-run
python3 forgesmith_simba.py

# Run ForgeSmith GEPA prompt evolution
python3 forgesmith_gepa.py --dry-run
python3 forgesmith_gepa.py

# Backfill episode data from logs
python3 forgesmith_backfill.py

# Performance dashboard
python3 forge_dashboard.py

# Performance analysis report
python3 analyze_performance.py --days 30

# Database migrations
python3 db_migrate.py

# ForgeArena adversarial testing
python3 forge_arena.py --dry-run
```

## Docker

```dockerfile
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    sqlite3 \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Set up working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies (if any requirements file exists)
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

# Create data directory
RUN mkdir -p /data

# Environment
ENV FORGE_DB_PATH=/data/forge.db
ENV PYTHONUNBUFFERED=1

# Run setup non-interactively or start with a specific command
ENTRYPOINT ["python3"]
CMD ["forge_orchestrator.py", "--help"]
```

```bash
# Build
docker build -t forgeteam .

# Run with API key and persistent data volume
docker run -it \
  -e ANTHROPIC_API_KEY="sk-ant-your-key-here" \
  -v forgeteam-data:/data \
  -v /path/to/your/projects:/projects \
  forgeteam forge_orchestrator.py --scan

# Run interactive setup
docker run -it \
  -e ANTHROPIC_API_KEY="sk-ant-your-key-here" \
  -v forgeteam-data:/data \
  forgeteam itzamna_setup.py

# Run a specific task
docker run -it \
  -e ANTHROPIC_API_KEY="sk-ant-your-key-here" \
  -v forgeteam-data:/data \
  -v /path/to/your/projects:/projects \
  forgeteam forge_orchestrator.py --task 42 --dev-test
```

## Troubleshooting

### `ANTHROPIC_API_KEY` not set

```
Error: API key not configured
```

**Fix:**
```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
# Or add to your shell profile:
echo 'export ANTHROPIC_API_KEY="sk-ant-your-key-here"' >> ~/.bashrc
source ~/.bashrc
```

### Claude Code CLI not found

```
FileNotFoundError: claude command not found
```

**Fix:**
```bash
npm install -g @anthropic-ai/claude-code
# Verify:
claude --version
# If installed but not found, check your PATH:
which claude || echo "Not in PATH"
```

### Database locked / concurrent access errors

```
sqlite3.OperationalError: database is locked
```

**Fix:**
```bash
# SQLite doesn't handle heavy concurrent writes well
# Ensure only one dispatch process runs at a time
# Check for stuck processes:
ps aux | grep forge_orchestrator
# Kill stale ones if needed:
kill <PID>
```

### Database schema out of date

```
Error: table X has no column named Y
```

**Fix:**
```bash
python3 db_migrate.py
# This will detect the current version and apply all pending migrations
```

### Port in use (Ollama)

```
Error: Connection refused on localhost:11434
```

**Fix:**
```bash
# Start Ollama if it's not running:
ollama serve
# Or check if something else is on that port:
lsof -i :11434
# Use a custom URL:
export OLLAMA_BASE_URL="http://localhost:11435"
```

### Permission denied on project directories

```
PermissionError: [Errno 13] Permission denied
```

**Fix:**
```bash
# Ensure the running user has read/write access to project directories
chmod -R u+rw /path/to/your/project
# For the database:
chmod 664 ~/.forgeteam/forge.db
```

### Tests failing

```bash
# Run the full test suite to identify issues:
python3 -m pytest test_*.py -v

# Run individual test files:
python3 test_loop_detection.py
python3 test_early_termination.py
python3 test_agent_messages.py
python3 test_agent_actions.py
python3 test_lessons_injection.py
python3 test_episode_injection.py
python3 test_forgesmith_simba.py
python3 test_rubric_scoring.py
python3 test_task_type_routing.py
```

### ForgeSmith changes seem wrong

```bash
# Always dry-run first to preview changes:
python3 forgesmith.py --full --dry-run

# Check the ForgeSmith run history:
python3 forgesmith.py --report

# Rollback a specific run:
python3 forgesmith.py --rollback <RUN_ID>
```

### "No tasks found" when running dispatch

```bash
# Check what work is available:
python3 forge_orchestrator.py --scan

# Verify tasks exist in the database:
sqlite3 ~/.forgeteam/forge.db "SELECT id, title, status FROM tasks WHERE status = 'todo' LIMIT 10;"
```

### Checkpoint recovery after crash

ForgeTeam automatically saves checkpoints during long-running tasks. If a task crashes mid-execution:

```bash
# The orchestrator will detect and resume from the last checkpoint automatically
python3 forge_orchestrator.py --task <TASK_ID>

# To force a fresh start (discard checkpoints):
rm -rf checkpoints/<TASK_ID>_*
python3 forge_orchestrator.py --task <TASK_ID>
```
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
