# DEPLOYMENT.md — Project Pombal

## Table of Contents

- [DEPLOYMENT.md — Project Pombal](#deploymentmd-project-pombal)
  - [TL;DR](#tldr)
- [Follow the interactive setup wizard — it handles DB, config, and file placement](#follow-the-interactive-setup-wizard-it-handles-db-config-and-file-placement)
  - [Prerequisites](#prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repository](#1-clone-the-repository)
    - [2. Verify Python version](#2-verify-python-version)
- [Must be 3.10 or higher](#must-be-310-or-higher)
    - [3. Run the interactive setup wizard](#3-run-the-interactive-setup-wizard)
    - [4. Initialize / migrate the database (if not using the wizard)](#4-initialize-migrate-the-database-if-not-using-the-wizard)
    - [5. Verify the installation](#5-verify-the-installation)
    - [6. Run a basic test to confirm everything works](#6-run-a-basic-test-to-confirm-everything-works)
  - [Environment Variables](#environment-variables)
  - [Running in Production](#running-in-production)
    - [Option A: Direct execution (simplest)](#option-a-direct-execution-simplest)
- [Dispatch tasks automatically based on priority scoring](#dispatch-tasks-automatically-based-on-priority-scoring)
- [Run a specific task by ID](#run-a-specific-task-by-id)
- [Run multiple tasks in parallel](#run-multiple-tasks-in-parallel)
- [Run the planner to generate tasks from a goal](#run-the-planner-to-generate-tasks-from-a-goal)
    - [Option B: Cron-based scheduling (recommended for production)](#option-b-cron-based-scheduling-recommended-for-production)
- [Edit crontab](#edit-crontab)
- [Run ForgeSmith analysis every 6 hours](#run-forgesmith-analysis-every-6-hours)
- [Run SIMBA rule generation weekly](#run-simba-rule-generation-weekly)
- [Nightly review report](#nightly-review-report)
- [Auto-dispatch pending work every 30 minutes](#auto-dispatch-pending-work-every-30-minutes)
    - [Option C: systemd service](#option-c-systemd-service)
    - [Monitoring & Dashboards](#monitoring-dashboards)
- [Performance dashboard](#performance-dashboard)
- [Performance analysis report](#performance-analysis-report)
- [Nightly review summary](#nightly-review-summary)
    - [ForgeSmith (Self-Optimization Engine)](#forgesmith-self-optimization-engine)
- [Dry run — see what changes would be made](#dry-run-see-what-changes-would-be-made)
- [Full auto-optimization](#full-auto-optimization)
- [Report only](#report-only)
- [Rollback a specific run](#rollback-a-specific-run)
- [SIMBA rule generation](#simba-rule-generation)
- [GEPA prompt evolution](#gepa-prompt-evolution)
- [Autoresearch optimization loop](#autoresearch-optimization-loop)
  - [Docker](#docker)
- [Install git (needed for diff detection) and sqlite3 CLI (optional debugging)](#install-git-needed-for-diff-detection-and-sqlite3-cli-optional-debugging)
- [Copy all project files](#copy-all-project-files)
- [Create directories for persistent data](#create-directories-for-persistent-data)
- [Run database migrations](#run-database-migrations)
- [Default: show help](#default-show-help)
- [Build](#build)
- [Run with persistent database](#run-with-persistent-database)
- [Interactive setup](#interactive-setup)
- [Run tests](#run-tests)
  - [Troubleshooting](#troubleshooting)
    - [Database issues](#database-issues)
    - [Agent execution issues](#agent-execution-issues)
    - [Ollama-specific issues](#ollama-specific-issues)
    - [ForgeSmith issues](#forgesmith-issues)
    - [General issues](#general-issues)
    - [Running the full test suite](#running-the-full-test-suite)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
git clone <your-repo-url> && cd ProjectPombal
python3 pombal_setup.py
# Follow the interactive setup wizard — it handles DB, config, and file placement
python3 forge_orchestrator.py --help
```

That's it. Pure Python stdlib, zero pip dependencies, SQLite-based.

---

## Prerequisites

| Requirement | Minimum Version | Purpose | Install |
|---|---|---|---|
| **Python** | 3.10+ | Runtime (stdlib only, no pip needed) | [python.org](https://www.python.org/downloads/) |
| **SQLite** | 3.35+ | Database (bundled with Python) | Usually pre-installed |
| **Claude CLI** | Latest | Agent execution backend | [claude.ai/docs](https://docs.anthropic.com/en/docs/claude-cli) |
| **Git** | 2.x | Version control, diff detection | [git-scm.com](https://git-scm.com/) |
| **gh** (GitHub CLI) | Optional | Repo setup automation | [cli.github.com](https://cli.github.com/) |
| **Ollama** | Optional | Local model support | [ollama.com](https://ollama.com/) |

> **Key point:** Project Pombal has **zero pip dependencies**. Everything runs on Python's standard library.

---

## Step-by-Step Setup

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd ProjectPombal
```

### 2. Verify Python version

```bash
python3 --version
# Must be 3.10 or higher
```

### 3. Run the interactive setup wizard

```bash
python3 pombal_setup.py
```

The wizard will walk you through:
- Choosing an install path
- Creating and initializing the SQLite database (30+ table schema)
- Copying project files
- Generating configuration files
- Generating MCP config and `.mcp` files
- Generating `CLAUDE.md` for your instance
- Setting up ForgeSmith cron (optional)
- Setting up Sentinel monitoring (optional)
- Setting up ForgeBot (optional)

### 4. Initialize / migrate the database (if not using the wizard)

```bash
python3 db_migrate.py
```

This auto-detects your current schema version and applies all pending migrations (v0→v1→v2→v3→v4). It creates a backup before each migration.

### 5. Verify the installation

```bash
python3 forge_orchestrator.py --help
```

### 6. Run a basic test to confirm everything works

```bash
python3 test_early_termination.py
python3 test_loop_detection.py
python3 test_agent_messages.py
python3 test_agent_actions.py
python3 test_lesson_sanitizer.py
```

---

## Environment Variables

| Variable | Description | Example | Required? |
|---|---|---|---|
| `POMBAL_DB` | Path to the SQLite database file | `/home/user/pombal/theforge.db` | No — auto-detected from config |
| `POMBAL_BASE` | Base install directory | `/home/user/pombal` | No — set during setup |
| `ANTHROPIC_API_KEY` | API key for Claude API calls (ForgeSmith, autoresearch) | `sk-ant-...` | Only if using API-based features |
| `OLLAMA_BASE_URL` | Base URL for Ollama instance | `http://localhost:11434` | Only if using Ollama agents |
| `OLLAMA_MODEL` | Default Ollama model name | `qwen2.5-coder:32b` | Only if using Ollama agents |

> **Note:** Most configuration is stored in config files generated by `pombal_setup.py`, not in environment variables. The system is designed to be configured via its dispatch config and internal SQLite tables.

---

## Running in Production

### Option A: Direct execution (simplest)

```bash
# Dispatch tasks automatically based on priority scoring
python3 forge_orchestrator.py --dispatch

# Run a specific task by ID
python3 forge_orchestrator.py --task <task_id>

# Run multiple tasks in parallel
python3 forge_orchestrator.py --tasks 101,102,103

# Run the planner to generate tasks from a goal
python3 forge_orchestrator.py --plan --goal "Build a REST API for user management" --project <project_id>
```

### Option B: Cron-based scheduling (recommended for production)

```bash
# Edit crontab
crontab -e
```

Add entries (**suggestion** — adjust paths and schedules to your needs):

```cron
# Run ForgeSmith analysis every 6 hours
0 */6 * * * cd /home/user/pombal && python3 forgesmith.py --auto >> /home/user/pombal/logs/forgesmith.log 2>&1

# Run SIMBA rule generation weekly
0 3 * * 0 cd /home/user/pombal && python3 forgesmith_simba.py >> /home/user/pombal/logs/simba.log 2>&1

# Nightly review report
0 22 * * * cd /home/user/pombal && python3 nightly_review.py >> /home/user/pombal/logs/nightly.log 2>&1

# Auto-dispatch pending work every 30 minutes
*/30 * * * * cd /home/user/pombal && python3 forge_orchestrator.py --dispatch >> /home/user/pombal/logs/dispatch.log 2>&1
```

### Option C: systemd service

Create `/etc/systemd/system/pombal-dispatch.service`:

```ini
[Unit]
Description=Project Pombal Auto-Dispatch
After=network.target

[Service]
Type=simple
User=pombal
WorkingDirectory=/home/pombal/ProjectPombal
ExecStart=/usr/bin/python3 forge_orchestrator.py --dispatch --loop
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable pombal-dispatch
sudo systemctl start pombal-dispatch
sudo journalctl -u pombal-dispatch -f
```

### Monitoring & Dashboards

```bash
# Performance dashboard
python3 forge_dashboard.py

# Performance analysis report
python3 analyze_performance.py --days 30

# Nightly review summary
python3 nightly_review.py
```

### ForgeSmith (Self-Optimization Engine)

```bash
# Dry run — see what changes would be made
python3 forgesmith.py --dry-run

# Full auto-optimization
python3 forgesmith.py --auto

# Report only
python3 forgesmith.py --report

# Rollback a specific run
python3 forgesmith.py --rollback <run_id>

# SIMBA rule generation
python3 forgesmith_simba.py --dry-run
python3 forgesmith_simba.py

# GEPA prompt evolution
python3 forgesmith_gepa.py --dry-run
python3 forgesmith_gepa.py

# Autoresearch optimization loop
python3 autoresearch_loop.py --role developer --target 85
```

---

## Docker

> **Suggestion:** Since Project Pombal is pure Python stdlib with SQLite, Docker is straightforward but optional. The system is designed to run directly on the host alongside Claude CLI.

```dockerfile
FROM python:3.12-slim

# Install git (needed for diff detection) and sqlite3 CLI (optional debugging)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy all project files
COPY . .

# Create directories for persistent data
RUN mkdir -p /data/db /data/logs /data/backups /data/checkpoints

# Run database migrations
RUN python3 db_migrate.py || true

# Default: show help
CMD ["python3", "forge_orchestrator.py", "--help"]
```

```bash
# Build
docker build -t pombal .

# Run with persistent database
docker run -v pombal-data:/data \
  -e POMBAL_DB=/data/db/theforge.db \
  pombal python3 forge_orchestrator.py --dispatch

# Interactive setup
docker run -it -v pombal-data:/data pombal python3 pombal_setup.py

# Run tests
docker run pombal python3 test_early_termination.py
```

**Docker Compose** (suggestion):

```yaml
version: '3.8'
services:
  pombal:
    build: .
    volumes:
      - pombal-db:/data/db
      - pombal-logs:/data/logs
      - pombal-backups:/data/backups
      - ./projects:/projects  # Mount your project directories
    environment:
      - POMBAL_DB=/data/db/theforge.db
    command: python3 forge_orchestrator.py --dispatch

volumes:
  pombal-db:
  pombal-logs:
  pombal-backups:
```

> **Important:** Claude CLI must be available inside the container for agent execution. If using Claude CLI, you'll need to mount your Claude configuration or set up authentication inside the container.

---

## Troubleshooting

### Database issues

| Problem | Solution |
|---|---|
| `sqlite3.OperationalError: no such table` | Run `python3 db_migrate.py` to apply migrations |
| `sqlite3.OperationalError: database is locked` | Another process has a write lock. Check for running ForgeSmith or dispatch processes: `ps aux \| grep python3` |
| Migration fails | A backup is automatically created before each migration. Check for `*.backup.*` files next to your DB |
| Need to verify migration state | `python3 benchmark_migrations.py` runs a full migration benchmark |

### Agent execution issues

| Problem | Solution |
|---|---|
| `claude: command not found` | Install Claude CLI and ensure it's on your `PATH` |
| Agent times out | Default timeout varies by complexity. Check `get_role_turns()` in `forge_orchestrator.py`. Increase with `--max-turns` flag |
| Agent stuck in loop | Loop detection is built-in (see `LoopDetector` class). Threshold defaults: warning at 3, terminate at 5 |
| Monologue detection firing | Agent producing text-only responses without tool use. This is expected behavior — the agent is redirected after 3 consecutive text-only turns |
| Early termination too aggressive | Adjust thresholds in the dispatch config file |

### Ollama-specific issues

| Problem | Solution |
|---|---|
| `Connection refused` to Ollama | Ensure Ollama is running: `ollama serve` or check `OLLAMA_BASE_URL` |
| Model not found | List available models: `ollama list`. Pull needed model: `ollama pull <model>` |
| Blocked command detected | `ollama_agent.py` has safety filters. Review `is_blocked_command()` for the blocklist |

### ForgeSmith issues

| Problem | Solution |
|---|---|
| `Not enough episodes for SIMBA` | Need minimum sample count. Run more agent tasks first |
| GEPA prompt evolution rejected | Validation checks diff ratio and protected sections. Use `--dry-run` to preview |
| Rollback needed | `python3 forgesmith.py --rollback <run_id>` |
| Impact assessment missing | Run `python3 forgesmith_impact.py` to backfill the column |

### General issues

| Problem | Solution |
|---|---|
| `python3: command not found` | Install Python 3.10+ or use `python` instead of `python3` |
| Permission denied on DB file | Check file permissions: `chmod 664 theforge.db` and ensure the running user owns it |
| Tests failing | Run individual test files to isolate: `python3 test_loop_detection.py` |
| Config file not found | Re-run `python3 pombal_setup.py` or check that config was generated in the expected path |
| Backfill historical data | `python3 forgesmith_backfill.py` parses log files to populate episode data |
| Port/process conflicts | Pombal doesn't bind to ports (no web server). Conflicts would be with Ollama (default 11434) |

### Running the full test suite

```bash
python3 test_early_termination.py
python3 test_early_termination_monologue.py
python3 test_loop_detection.py
python3 test_agent_messages.py
python3 test_agent_actions.py
python3 test_lesson_sanitizer.py
python3 test_lessons_injection.py
python3 test_episode_injection.py
python3 test_task_type_routing.py
python3 test_rubric_scoring.py
python3 test_rubric_quality_scorer.py
python3 test_forgesmith_simba.py
```

Or run them all at once:

```bash
for f in test_*.py; do echo "=== $f ===" && python3 "$f" && echo "PASS" || echo "FAIL"; done
```
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
