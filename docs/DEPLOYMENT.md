# DEPLOYMENT.md

## Table of Contents

- [DEPLOYMENT.md](#deploymentmd)
  - [TL;DR](#tldr)
- [Open Claude Desktop / Claude Code, point it at your project](#open-claude-desktop-claude-code-point-it-at-your-project)
- [Talk to Claude: "Create a task to add input validation to the user signup endpoint"](#talk-to-claude-create-a-task-to-add-input-validation-to-the-user-signup-endpoint)
  - [How EQUIPA Actually Works](#how-equipa-actually-works)
  - [Prerequisites](#prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repo](#1-clone-the-repo)
    - [2. Run the setup wizard](#2-run-the-setup-wizard)
    - [3. Set your API key](#3-set-your-api-key)
    - [4. Initialize the database](#4-initialize-the-database)
    - [5. Configure Claude Desktop (MCP Server)](#5-configure-claude-desktop-mcp-server)
    - [6. Verify it works](#6-verify-it-works)
    - [7. Run the tests (optional, but recommended)](#7-run-the-tests-optional-but-recommended)
  - [Environment Variables](#environment-variables)
  - [Running in Production](#running-in-production)
    - [Option A: Systemd service (Linux)](#option-a-systemd-service-linux)
    - [Option B: ForgeSmith cron (self-improvement loop)](#option-b-forgesmith-cron-self-improvement-loop)
- [Run ForgeSmith every 6 hours](#run-forgesmith-every-6-hours)
- [Run SIMBA (rule generation) daily](#run-simba-rule-generation-daily)
- [Run GEPA (prompt evolution) weekly](#run-gepa-prompt-evolution-weekly)
    - [Option C: Nightly review](#option-c-nightly-review)
  - [Docker](#docker)
- [That's it. No pip install. No requirements.txt. Zero dependencies.](#thats-it-no-pip-install-no-requirementstxt-zero-dependencies)
- [Create data directory for SQLite DB and logs](#create-data-directory-for-sqlite-db-and-logs)
- [Initialize the database](#initialize-the-database)
- [MCP server](#mcp-server)
    - [Docker Compose (if you also run Ollama locally)](#docker-compose-if-you-also-run-ollama-locally)
  - [Troubleshooting](#troubleshooting)
    - ["ANTHROPIC_API_KEY not set"](#anthropic_api_key-not-set)
- [or add to ~/.bashrc and source it](#or-add-to-bashrc-and-source-it)
    - [Database migration errors](#database-migration-errors)
- [Back up first, then force re-migrate](#back-up-first-then-force-re-migrate)
    - [MCP server not connecting in Claude Desktop](#mcp-server-not-connecting-in-claude-desktop)
    - ["Permission denied" on project directories](#permission-denied-on-project-directories)
    - [Agent stuck in a loop](#agent-stuck-in-a-loop)
- [Find the relevant agent log and read it](#find-the-relevant-agent-log-and-read-it)
    - [Port already in use](#port-already-in-use)
- [Find what's using the port](#find-whats-using-the-port)
- [Kill it](#kill-it)
    - [SQLite "database is locked"](#sqlite-database-is-locked)
- [Check for stuck processes](#check-for-stuck-processes)
- [Kill any zombie processes](#kill-any-zombie-processes)
    - [Tests failing](#tests-failing)
- [Run with verbose output to see what's wrong](#run-with-verbose-output-to-see-whats-wrong)
    - [Ollama connection refused](#ollama-connection-refused)
- [Check if Ollama is running](#check-if-ollama-is-running)
- [If not, start it](#if-not-start-it)
  - [Current Limitations](#current-limitations)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
git clone https://github.com/sbknana/equipa.git && cd equipa-repo
python3 equipa_setup.py          # interactive setup wizard — does everything
# Open Claude Desktop / Claude Code, point it at your project
# Talk to Claude: "Create a task to add input validation to the user signup endpoint"
```

That's it. You talk to Claude, Claude runs EQUIPA. Most users never touch the CLI directly.

---

## How EQUIPA Actually Works

You don't run EQUIPA commands. You **talk to Claude**.

Claude reads your project, creates tasks, dispatches AI agents (developer, tester, reviewer, security auditor, etc.), monitors their progress, and reports back. The conversation looks like:

> **You:** "The login endpoint has no rate limiting. Fix it and add tests."
>
> **Claude:** Creates a task, dispatches a developer agent, waits for it to finish, runs the tester agent, checks results, reports back with what changed.

The CLI exists for automation and scripting, but the primary interface is conversation.

---

## Prerequisites

| Tool | Version | Why | Install |
|------|---------|-----|---------|
| Python | 3.10+ | Everything runs on Python stdlib | [python.org](https://www.python.org/downloads/) |
| Git | 2.x+ | Worktree isolation for parallel agents | [git-scm.com](https://git-scm.com/) |
| SQLite | 3.35+ | Ships with Python, but check version | Bundled with Python |
| Claude Desktop or Claude Code | Latest | The conversational interface — how you actually use EQUIPA | [claude.ai/download](https://claude.ai/download) |
| Anthropic API key | — | Agents need to call Claude API | [console.anthropic.com](https://console.anthropic.com/) |

**Optional:**

| Tool | Why |
|------|-----|
| Ollama | Local model support for cost-sensitive tasks |
| `gh` CLI | Auto-creates PRs from agent work |

Check your setup:

```bash
python3 --version   # needs 3.10+
git --version       # needs 2.x+
sqlite3 --version   # needs 3.35+
```

---

## Step-by-Step Setup

### 1. Clone the repo

```bash
git clone https://github.com/sbknana/equipa.git
cd equipa-repo
```

### 2. Run the setup wizard

```bash
python3 equipa_setup.py
```

This walks you through everything interactively:
- Checks prerequisites
- Picks an install path
- Creates the SQLite database (30+ tables)
- Generates config files
- Sets up MCP server config for Claude Desktop
- Generates the `.claude/` directory and `CLAUDE.md` for Claude Code
- Optionally sets up ForgeSmith cron job (the self-improvement loop)

### 3. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add it to your shell profile so it persists:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
source ~/.bashrc
```

### 4. Initialize the database

The setup wizard handles this, but if you need to do it manually or run migrations on an existing install:

```bash
python3 db_migrate.py
```

This is idempotent — safe to run multiple times. It auto-detects your current schema version and migrates forward.

### 5. Configure Claude Desktop (MCP Server)

The setup wizard generates this, but here's what goes in your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "equipa": {
      "command": "python3",
      "args": ["/path/to/equipa/equipa/mcp_server.py"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "EQUIPA_DB": "/path/to/equipa/forge.db"
      }
    }
  }
}
```

Replace the paths with your actual install location. Restart Claude Desktop after editing.

### 6. Verify it works

```bash
python3 -m equipa.cli --help
```

Or just open Claude and say: *"Show me the current task status for all projects."*

If the MCP server is configured correctly, Claude will query your EQUIPA database and respond.

### 7. Run the tests (optional, but recommended)

```bash
python3 -m pytest tests/ -v
```

334+ tests, all pure Python, no dependencies to install.

---

## Environment Variables

| Variable | Description | Example | Required? |
|----------|-------------|---------|-----------|
| `ANTHROPIC_API_KEY` | API key for Claude — agents use this | `sk-ant-api03-...` | **Yes** |
| `EQUIPA_DB` | Path to SQLite database | `/home/user/equipa/forge.db` | Yes (set by setup wizard) |
| `EQUIPA_BASE` | Base directory for EQUIPA install | `/home/user/equipa` | Yes (set by setup wizard) |
| `OLLAMA_BASE_URL` | Ollama server URL for local models | `http://localhost:11434` | No |
| `EQUIPA_LOG_DIR` | Where agent logs go | `/home/user/equipa/logs` | No (defaults to `./logs`) |
| `EQUIPA_MAX_COST` | Global cost cap per agent run (USD) | `2.00` | No (defaults from config) |
| `EQUIPA_DRY_RUN` | Set to `1` to prevent actual changes | `1` | No |

---

## Running in Production

### Option A: Systemd service (Linux)

Create `/etc/systemd/system/equipa-mcp.service`:

```ini
[Unit]
Description=EQUIPA MCP Server
After=network.target

[Service]
Type=simple
User=youruser
Environment=ANTHROPIC_API_KEY=sk-ant-...
Environment=EQUIPA_DB=/home/youruser/equipa/forge.db
ExecStart=/usr/bin/python3 /home/youruser/equipa/equipa/mcp_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable equipa-mcp
sudo systemctl start equipa-mcp
sudo systemctl status equipa-mcp
```

### Option B: ForgeSmith cron (self-improvement loop)

The setup wizard offers to set this up. It runs ForgeSmith periodically to analyze agent performance and adjust prompts/config:

```bash
# The setup wizard (equipa_setup.py) auto-configures this for your OS:
# - Linux/WSL: cron job
# - Windows: Task Scheduler (schtasks)
#
# To set up manually:

# Linux/WSL — add to crontab:
0 0 * * * cd /path/to/equipa && python3 forgesmith.py --auto >> forgesmith.log 2>&1

# Windows — create scheduled task:
# schtasks /create /tn "EQUIPA_ForgeSmith_Nightly" /tr "python forgesmith.py --auto" /sc daily /st 00:00 /f

# GEPA prompt evolution (weekly recommended):
0 3 * * 0 cd /path/to/equipa && python3 forgesmith_gepa.py --evolve >> gepa.log 2>&1

# SIMBA vector memory maintenance (daily):
30 0 * * * cd /path/to/equipa && python3 forgesmith.py --simba-maintenance >> simba.log 2>&1

### Option C: Nightly review

Get a daily digest of what happened across all projects:

```bash
0 22 * * * python3 /home/youruser/equipa/scripts/nightly_review.py >> /home/youruser/equipa/logs/nightly.log 2>&1
```

---

## Docker

EQUIPA has zero dependencies, so the Dockerfile is refreshingly simple:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# That's it. No pip install. No requirements.txt. Zero dependencies.
COPY . .

# Create data directory for SQLite DB and logs
RUN mkdir -p /data/logs

ENV EQUIPA_DB=/data/forge.db
ENV EQUIPA_LOG_DIR=/data/logs

# Initialize the database
RUN python3 db_migrate.py || true

# MCP server
EXPOSE 8080

CMD ["python3", "equipa/mcp_server.py"]
```

Build and run:

```bash
docker build -t equipa .
docker run -d \
  --name equipa \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -v equipa-data:/data \
  equipa
```

The `-v equipa-data:/data` persists your database and logs across container restarts.

### Docker Compose (if you also run Ollama locally)

```yaml
version: '3.8'
services:
  equipa:
    build: .
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OLLAMA_BASE_URL=http://ollama:11434
      - EQUIPA_DB=/data/forge.db
    volumes:
      - equipa-data:/data
      - ./projects:/projects  # mount your project repos here
    depends_on:
      - ollama

  ollama:
    image: ollama/ollama
    volumes:
      - ollama-models:/root/.ollama

volumes:
  equipa-data:
  ollama-models:
```

---

## Troubleshooting

### "ANTHROPIC_API_KEY not set"

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# or add to ~/.bashrc and source it
```

### Database migration errors

```bash
# Back up first, then force re-migrate
cp forge.db forge.db.backup
python3 db_migrate.py
```

Migrations are idempotent. Running them twice won't break anything.

### MCP server not connecting in Claude Desktop

1. Check the config path is correct in `claude_desktop_config.json`
2. Make sure the Python path is absolute, not relative
3. Restart Claude Desktop completely (quit and reopen, not just close the window)
4. Check logs: `tail -f ~/Library/Logs/Claude/mcp*.log` (macOS)

### "Permission denied" on project directories

Agents need read/write access to the project directory they're working on:

```bash
chmod -R u+rw /path/to/your/project
```

### Agent stuck in a loop

This happens. EQUIPA has loop detection that kills agents after repeated identical outputs, but sometimes agents spin on a problem without making progress. Check the logs:

```bash
ls -la logs/
# Find the relevant agent log and read it
```

You can also ask Claude: *"What's the status of task 42? Is the agent stuck?"*

### Port already in use

```bash
# Find what's using the port
lsof -i :8080
# Kill it
kill -9 <PID>
```

### SQLite "database is locked"

Usually means two processes are trying to write simultaneously. EQUIPA uses WAL mode to minimize this, but it can still happen:

```bash
# Check for stuck processes
ps aux | grep equipa
# Kill any zombie processes
```

### Tests failing

```bash
# Run with verbose output to see what's wrong
python3 -m pytest tests/ -v --tb=short
```

Make sure you're not running tests against a production database. Tests use temporary databases.

### Ollama connection refused

If you're using local models:

```bash
# Check if Ollama is running
curl http://localhost:11434/api/version

# If not, start it
ollama serve
```

---

## Current Limitations

Being honest here:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent reads code for 10 turns without making a change. The early termination system kills these, but that means the task doesn't get done. You'll need to break complex tasks into smaller pieces.

- **Git worktree merges occasionally need manual intervention.** Parallel agents work in isolated worktrees, which is great, until they both modify the same file. Merge conflicts happen. You'll sometimes need to resolve them by hand.

- **Self-improvement takes time.** ForgeSmith + GEPA + SIMBA need 20-30 completed tasks before patterns emerge and the system starts making useful adjustments. Don't expect magic on day one.

- **Tester role needs a working test suite.** If your project doesn't have tests, the tester agent doesn't have much to work with. It'll try to create tests, but it works way better when there's an existing test framework to build on.

- **Early termination is aggressive.** Agents get killed after 10 turns of just reading without making changes. Most of the time this is correct (the agent is stuck), but some genuinely complex tasks need more exploration time. This threshold is configurable in `dispatch_config.json`.

- **Cost can add up.** Each agent run calls the Claude API. Complex tasks with retries can cost $1-5 per task. The cost controls help, but keep an eye on your API usage, especially early on while you're tuning things.

- **It's not magic.** Agents still fail, get confused, produce wrong code, and waste turns. EQUIPA makes AI agents useful enough to actually ship code, but someone still needs to review what comes out.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
