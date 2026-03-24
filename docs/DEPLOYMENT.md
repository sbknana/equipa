# DEPLOYMENT.md — EQUIPA

## Table of Contents

- [DEPLOYMENT.md — EQUIPA](#deploymentmd-equipa)
  - [TL;DR](#tldr)
  - [Prerequisites](#prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repo](#1-clone-the-repo)
    - [2. Run the setup wizard](#2-run-the-setup-wizard)
    - [3. Run database migrations](#3-run-database-migrations)
    - [4. Verify the install](#4-verify-the-install)
    - [5. Configure your dispatch config](#5-configure-your-dispatch-config)
    - [6. Run your first task](#6-run-your-first-task)
- [Run a specific task by ID](#run-a-specific-task-by-id)
- [Auto-dispatch — let EQUIPA pick what to work on](#auto-dispatch-let-equipa-pick-what-to-work-on)
- [Parallel tasks](#parallel-tasks)
  - [Environment Variables](#environment-variables)
  - [Running in Production](#running-in-production)
    - [Option A: Cron-based dispatch (recommended for starters)](#option-a-cron-based-dispatch-recommended-for-starters)
    - [Option B: Systemd service](#option-b-systemd-service)
    - [ForgeSmith (self-improvement loop)](#forgesmith-self-improvement-loop)
    - [Nightly review](#nightly-review)
  - [Docker](#docker)
- [Create the database](#create-the-database)
- [Volume for persistent data](#volume-for-persistent-data)
- [Default: run auto-dispatch](#default-run-auto-dispatch)
  - [Troubleshooting](#troubleshooting)
    - ["No module named equipa"](#no-module-named-equipa)
    - [Database not found](#database-not-found)
    - [Ollama connection refused](#ollama-connection-refused)
- [Check Ollama is running](#check-ollama-is-running)
- [If not running:](#if-not-running)
    - [Agent stuck in a loop / hitting max turns](#agent-stuck-in-a-loop-hitting-max-turns)
    - ["Permission denied" on project directory](#permission-denied-on-project-directory)
    - [ForgeSmith not producing changes](#forgesmith-not-producing-changes)
    - [Migrations fail](#migrations-fail)
- [Check current version](#check-current-version)
- [Force re-run with backup](#force-re-run-with-backup)
    - [Port / process conflicts with parallel agents](#port-process-conflicts-with-parallel-agents)
- [Check for stuck worktrees](#check-for-stuck-worktrees)
  - [Current Limitations](#current-limitations)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
git clone https://github.com/your-org/equipa-repo.git && cd equipa-repo
python3 equipa_setup.py                    # interactive setup wizard
python3 db_migrate.py                      # ensure DB schema is current
python3 -m equipa.cli --task 1             # run a single task
```

That's it. No `pip install`, no Docker, no virtualenv. Pure Python stdlib.

---

## Prerequisites

| Tool | Version | Why | Install |
|------|---------|-----|---------|
| **Python** | 3.10+ | Everything runs on it | [python.org](https://www.python.org/downloads/) |
| **SQLite** | 3.35+ | Comes with Python, but CLI is handy for debugging | Usually pre-installed |
| **Git** | 2.30+ | Worktree isolation for parallel agents | [git-scm.com](https://git-scm.com/) |
| **Claude CLI** _or_ **Ollama** | Latest | The actual AI brains — you need at least one provider | [Claude](https://docs.anthropic.com/en/docs/claude-cli) / [Ollama](https://ollama.ai) |
| **gh** (optional) | 2.0+ | Only if agents need to create PRs | [cli.github.com](https://cli.github.com/) |

Check you're good:

```bash
python3 --version    # 3.10+
git --version        # 2.30+
sqlite3 --version    # 3.35+
```

---

## Step-by-Step Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-org/equipa-repo.git
cd equipa-repo
```

### 2. Run the setup wizard

```bash
python3 equipa_setup.py
```

This walks you through everything interactively — database creation, config generation, MCP config, optional components. It'll ask where you want things installed and set up the SQLite database with the full 30+ table schema.

### 3. Run database migrations

Even if the setup wizard created the DB, always run migrations to make sure you're on the latest schema:

```bash
python3 db_migrate.py
```

It auto-detects your current version and applies what's needed. It also backs up the DB before migrating.. just in case.

### 4. Verify the install

```bash
python3 -m pytest tests/ -x
```

There are 334+ tests. They all run against an in-memory SQLite DB — no external services needed.

### 5. Configure your dispatch config

Copy the example config if the wizard didn't make one:

```bash
cp dispatch_config.example.json dispatch_config.json
```

Edit `dispatch_config.json` with your preferred models, providers, and feature flags. The defaults are sane, but you'll probably want to tweak the provider settings.

### 6. Run your first task

```bash
# Run a specific task by ID
python3 -m equipa.cli --task 1

# Auto-dispatch — let EQUIPA pick what to work on
python3 -m equipa.cli --auto

# Parallel tasks
python3 -m equipa.cli --tasks 1,2,3
```

---

## Environment Variables

| Variable | Description | Example | Required? |
|----------|-------------|---------|-----------|
| `EQUIPA_DB` | Path to SQLite database | `/home/user/equipa/theforge.db` | Yes |
| `EQUIPA_CONFIG` | Path to dispatch config JSON | `/home/user/equipa/dispatch_config.json` | No (defaults to `./dispatch_config.json`) |
| `ANTHROPIC_API_KEY` | API key for Claude provider | `sk-ant-...` | If using Claude API |
| `OLLAMA_BASE_URL` | Base URL for Ollama instance | `http://localhost:11434` | If using Ollama |
| `EQUIPA_LOG_DIR` | Where agent logs go | `/home/user/equipa/logs` | No (defaults to `./logs`) |
| `EQUIPA_CHECKPOINTS_DIR` | Checkpoint storage for long-running tasks | `/home/user/equipa/checkpoints` | No (defaults to `./checkpoints`) |
| `EQUIPA_BACKUP_DIR` | Where ForgeSmith stores config backups | `/home/user/equipa/backups` | No |

You can also set these in your `dispatch_config.json` instead of environment variables. The config file wins if both are set.

---

## Running in Production

### Option A: Cron-based dispatch (recommended for starters)

Run auto-dispatch every 15 minutes:

```bash
crontab -e
```

```cron
*/15 * * * * cd /home/user/equipa && python3 -m equipa.cli --auto >> /var/log/equipa/dispatch.log 2>&1
```

### Option B: Systemd service

Create `/etc/systemd/system/equipa-dispatch.service`:

```ini
[Unit]
Description=EQUIPA Auto-Dispatch
After=network.target

[Service]
Type=oneshot
User=equipa
WorkingDirectory=/home/user/equipa
Environment=EQUIPA_DB=/home/user/equipa/theforge.db
ExecStart=/usr/bin/python3 -m equipa.cli --auto
StandardOutput=append:/var/log/equipa/dispatch.log
StandardError=append:/var/log/equipa/dispatch-error.log

[Install]
WantedBy=multi-user.target
```

Pair it with a timer — `/etc/systemd/system/equipa-dispatch.timer`:

```ini
[Unit]
Description=Run EQUIPA dispatch every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now equipa-dispatch.timer
```

### ForgeSmith (self-improvement loop)

ForgeSmith should run nightly. It looks at agent performance, extracts lessons, evolves prompts, and prunes bad rules.

```cron
0 2 * * * cd /home/user/equipa && python3 forgesmith.py --full >> /var/log/equipa/forgesmith.log 2>&1
0 3 * * * cd /home/user/equipa && python3 forgesmith_simba.py >> /var/log/equipa/simba.log 2>&1
0 4 * * * cd /home/user/equipa && python3 forgesmith_gepa.py >> /var/log/equipa/gepa.log 2>&1
```

### Nightly review

Get a summary of what happened today:

```cron
0 22 * * * cd /home/user/equipa && python3 nightly_review.py >> /var/log/equipa/nightly.log 2>&1
```

---

## Docker

EQUIPA doesn't need Docker (it's pure Python, no deps), but if you want isolation or are deploying to a server:

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y git sqlite3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Create the database
RUN python3 equipa_setup.py --noninteractive || true
RUN python3 db_migrate.py

# Volume for persistent data
VOLUME ["/app/data"]

ENV EQUIPA_DB=/app/data/theforge.db
ENV EQUIPA_LOG_DIR=/app/data/logs
ENV EQUIPA_CHECKPOINTS_DIR=/app/data/checkpoints

# Default: run auto-dispatch
CMD ["python3", "-m", "equipa.cli", "--auto"]
```

```bash
docker build -t equipa .
docker run -v equipa-data:/app/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  equipa
```

> **Note:** If you're using Ollama, it needs to be accessible from inside the container. Set `OLLAMA_BASE_URL` to your host's IP, not `localhost`.

---

## Troubleshooting

### "No module named equipa"

You need to run from the repo root, or the directory containing the `equipa/` package:

```bash
cd /path/to/equipa-repo
python3 -m equipa.cli --task 1
```

### Database not found

```
sqlite3.OperationalError: unable to open database file
```

Either `EQUIPA_DB` isn't set or points to a non-existent path. Fix it:

```bash
export EQUIPA_DB=/full/path/to/theforge.db
python3 db_migrate.py  # creates tables if DB exists but is empty
```

### Ollama connection refused

```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# If not running:
ollama serve
```

Make sure `OLLAMA_BASE_URL` matches where Ollama is actually listening.

### Agent stuck in a loop / hitting max turns

This is a known limitation. EQUIPA has loop detection that kills agents after they repeat the same actions, but sometimes legitimate complex work looks like looping. Options:

- Increase `max_turns` in dispatch config for complex tasks
- Break the task into smaller pieces (this usually works better)
- Check the agent logs in `$EQUIPA_LOG_DIR` to see what it was doing

### "Permission denied" on project directory

Agents need read/write access to the project directories they work in. Check ownership:

```bash
ls -la /path/to/project/
```

### ForgeSmith not producing changes

Self-improvement needs data. It won't do anything useful until it has 20-30 completed tasks with episode data to analyze. Just let it run — it'll start making suggestions once there's enough history.

### Migrations fail

```bash
# Check current version
python3 -c "import sqlite3; c=sqlite3.connect('theforge.db'); print(c.execute('SELECT version FROM schema_version').fetchone())"

# Force re-run with backup
python3 db_migrate.py
```

Migrations always create a backup before running. If things go sideways, look for `.bak` files next to your database.

### Port / process conflicts with parallel agents

If you're running multiple agents and they're touching the same files, you might see git merge conflicts. Git worktree isolation helps but isn't bulletproof:

```bash
# Check for stuck worktrees
git worktree list
git worktree prune
```

---

## Current Limitations

Being honest about what doesn't work perfectly yet:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent reads 10 files and then reads the same 10 files again. The early termination system catches most of these (kills at 10 turns of just reading), but some legitimate complex tasks actually need more exploration time.

- **Git worktree merges occasionally need manual intervention.** Parallel agents working on the same repo can produce merge conflicts that need a human to sort out. It's getting better but it's not hands-off.

- **Self-improvement takes time.** ForgeSmith + GEPA + SIMBA form a closed loop, but they need 20-30 completed tasks before patterns emerge. Don't expect magic after 5 tasks.

- **Tester role depends on your project having a working test suite.** If your project doesn't have tests, the tester agent doesn't have much to work with. It can write new tests, but it can't verify existing behavior without a test runner.

- **Early termination is a blunt instrument.** Killing agents at 10 turns of reading-without-writing catches most stuck agents, but some tasks genuinely require deep code exploration first. There's a tension between "stop wasting tokens" and "let it think."

- **Cost controls kill agents hard.** The cost breaker terminates agents that exceed budget. This is usually good (prevents $50 runaway tasks), but occasionally an agent is 90% done and gets killed on the last mile.

- **Ollama support is functional but slower.** Local models through Ollama work, but they're notably less capable than Claude for complex multi-step tasks. Good for simple changes and testing.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
