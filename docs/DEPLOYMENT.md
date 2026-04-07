# DEPLOYMENT.md

## Table of Contents

- [DEPLOYMENT.md](#deploymentmd)
  - [TL;DR](#tldr)
- [Install (Linux/macOS/WSL)](#install-linuxmacoswsl)
- [Or manual setup](#or-manual-setup)
  - [Prerequisites](#prerequisites)
  - [Step-by-Step Setup](#step-by-step-setup)
    - [1. Clone the repository](#1-clone-the-repository)
    - [2. Run the setup wizard](#2-run-the-setup-wizard)
    - [3. Set your API key](#3-set-your-api-key)
    - [4. Verify installation](#4-verify-installation)
  - [Environment Variables](#environment-variables)
  - [Running in Production](#running-in-production)
    - [Option 1: CLI (automation/scripting)](#option-1-cli-automationscripting)
- [Dispatch a single task](#dispatch-a-single-task)
- [Auto-dispatch from goals.txt](#auto-dispatch-from-goalstxt)
- [Run multiple tasks in parallel](#run-multiple-tasks-in-parallel)
    - [Option 2: MCP Server (conversational with Claude Desktop)](#option-2-mcp-server-conversational-with-claude-desktop)
    - [Option 3: systemd Service (Linux)](#option-3-systemd-service-linux)
    - [Option 4: PM2 (Node.js process manager)](#option-4-pm2-nodejs-process-manager)
  - [Docker](#docker)
  - [Troubleshooting](#troubleshooting)
    - [Port already in use (MCP server)](#port-already-in-use-mcp-server)
- [Find process using the port](#find-process-using-the-port)
- [Kill it](#kill-it)
- [Or change MCP server port in dispatch_config.json](#or-change-mcp-server-port-in-dispatch_configjson)
    - [Database locked](#database-locked)
    - [Missing dependencies (if you installed manually)](#missing-dependencies-if-you-installed-manually)
- [Make sure you are in the equipa directory](#make-sure-you-are-in-the-equipa-directory)
- [Run setup again](#run-setup-again)
    - [ForgeSmith not running nightly](#forgesmith-not-running-nightly)
- [Should see: 0 2 * * * cd ~/.forge && python3 -m scripts.forgesmith --full](#should-see-0-2-cd-forge-python3-m-scriptsforgesmith-full)
- [If missing, re-run setup wizard and answer yes to cron setup](#if-missing-re-run-setup-wizard-and-answer-yes-to-cron-setup)
    - [Agent gets stuck / infinite loop](#agent-gets-stuck-infinite-loop)
    - [Cost overrun](#cost-overrun)
    - [Tests fail after dispatch](#tests-fail-after-dispatch)
    - [Ollama embeddings not working](#ollama-embeddings-not-working)
- [Check Ollama is running](#check-ollama-is-running)
- [Should return list of models](#should-return-list-of-models)
- [If not, start Ollama](#if-not-start-ollama)
- [Pull embedding model if missing](#pull-embedding-model-if-missing)
    - [Git worktree merge conflicts](#git-worktree-merge-conflicts)
    - [Preflight check fails](#preflight-check-fails)
  - [Current Limitations](#current-limitations)
  - [What's Next](#whats-next)
  - [Related Documentation](#related-documentation)

## TL;DR

```bash
# Install (Linux/macOS/WSL)
curl -sSL https://raw.githubusercontent.com/yourusername/equipa/main/equipa_setup.py | python3 -

# Or manual setup
git clone https://github.com/yourusername/equipa.git
cd equipa
python3 equipa_setup.py
python3 -m equipa.cli --help
```

Done. You now have a working EQUIPA install. Talk to Claude, tell it what to build.

---

## Prerequisites

**Required:**
- Python 3.10+ ([python.org](https://www.python.org/downloads/))
- SQLite3 (usually bundled with Python)
- Git ([git-scm.com](https://git-scm.com/downloads))
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com/))

**Optional (for Ollama support):**
- Ollama ([ollama.ai](https://ollama.ai/download))

**Platform notes:**
- Windows: Use WSL2. Native Windows support exists but WSL is smoother.
- macOS: Works out of the box.
- Linux: Works out of the box.

Check versions:
```bash
python3 --version  # Should be 3.10 or higher
git --version
sqlite3 --version
```

---

## Step-by-Step Setup

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/equipa.git
cd equipa
```

### 2. Run the setup wizard
```bash
python3 equipa_setup.py
```

This wizard:
- Checks prerequisites (Python, Git, SQLite)
- Creates `~/.forge/` directory structure
- Initializes SQLite database with 30+ tables
- Copies prompts, skills, and scripts
- Generates `dispatch_config.json` and MCP server configs
- Offers to set up ForgeSmith nightly cron (Linux) or Task Scheduler (Windows)

**Answer the prompts:**
- Install path: Default is `~/.forge/` — hit Enter or specify custom path
- Database path: Default is `~/.forge/equipa.db` — hit Enter or specify custom path
- MCP server setup: Say yes if using Claude Desktop
- ForgeSmith cron: Say yes for automatic self-improvement (recommended)

### 3. Set your API key
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Add to `~/.bashrc` or `~/.zshrc` to persist:
```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
source ~/.bashrc
```

### 4. Verify installation
```bash
python3 -m equipa.cli --version
python3 -m equipa.cli task status
```

If you see version info and a task table (even if empty), you are good.

---

## Environment Variables

| Name | Description | Example | Required? |
|------|-------------|---------|-----------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key for Claude | `sk-ant-api03-...` | **Yes** |
| `EQUIPA_DB_PATH` | Path to SQLite database | `~/.forge/equipa.db` | No (defaults to install path) |
| `OLLAMA_BASE_URL` | Ollama server URL for embeddings/local models | `http://localhost:11434` | No (only if using Ollama) |
| `EQUIPA_LOG_LEVEL` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | No (defaults to `INFO`) |
| `EQUIPA_MAX_COST` | Global cost limit per task (USD) | `5.00` | No (per-task limits in dispatch config) |

---

## Running in Production

### Option 1: CLI (automation/scripting)
```bash
# Dispatch a single task
python3 -m equipa.cli dispatch --task-id 42

# Auto-dispatch from goals.txt
python3 -m equipa.cli auto-dispatch --goals goals.txt

# Run multiple tasks in parallel
python3 -m equipa.cli parallel --task-ids 10,11,12
```

### Option 2: MCP Server (conversational with Claude Desktop)
**This is the primary usage model.**

Add to Claude Desktop's MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "equipa": {
      "command": "python3",
      "args": ["-m", "equipa.mcp_server"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Restart Claude Desktop. Now you can:
- "Create a task to add login validation"
- "Dispatch task 15"
- "Show me the last 5 lessons learned"
- "What's the status of project NewsReader?"

Claude handles task creation, agent dispatch, progress tracking, and reporting. You never touch the CLI.

### Option 3: systemd Service (Linux)
Create `/etc/systemd/system/equipa-worker.service`:

```ini
[Unit]
Description=EQUIPA Auto-Dispatch Worker
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/.forge
Environment="ANTHROPIC_API_KEY=sk-ant-..."
ExecStart=/usr/bin/python3 -m equipa.cli auto-dispatch --goals /home/youruser/.forge/goals.txt --loop
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable equipa-worker
sudo systemctl start equipa-worker
sudo systemctl status equipa-worker
```

### Option 4: PM2 (Node.js process manager)
```bash
npm install -g pm2
pm2 start python3 --name equipa -- -m equipa.cli auto-dispatch --goals goals.txt --loop
pm2 save
pm2 startup  # Follow instructions to persist across reboots
```

---

## Docker

**Dockerfile:**
```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN python3 equipa_setup.py --non-interactive --install-path /root/.forge

ENV ANTHROPIC_API_KEY=""
ENV EQUIPA_DB_PATH="/root/.forge/equipa.db"

CMD ["python3", "-m", "equipa.cli", "auto-dispatch", "--goals", "/root/.forge/goals.txt", "--loop"]
```

**Build and run:**
```bash
docker build -t equipa:latest .
docker run -d \
  --name equipa-worker \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -v $(pwd)/goals.txt:/root/.forge/goals.txt \
  -v equipa-data:/root/.forge \
  equipa:latest
```

**docker-compose.yml:**
```yaml
version: '3.8'
services:
  equipa:
    build: .
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    volumes:
      - ./goals.txt:/root/.forge/goals.txt
      - equipa-data:/root/.forge
    restart: unless-stopped

volumes:
  equipa-data:
```

Run with:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
docker-compose up -d
```

---

## Troubleshooting

### Port already in use (MCP server)
**Symptom:** `OSError: [Errno 48] Address already in use`

**Fix:**
```bash
# Find process using the port
lsof -i :8765
# Kill it
kill -9 <PID>
# Or change MCP server port in dispatch_config.json
```

### Database locked
**Symptom:** `sqlite3.OperationalError: database is locked`

**Fix:**
- Only one writer at a time. Check for running agents:
  ```bash
  ps aux | grep equipa
  pkill -9 -f equipa
  ```
- Or enable WAL mode (setup wizard does this automatically):
  ```bash
  sqlite3 ~/.forge/equipa.db "PRAGMA journal_mode=WAL;"
  ```

### Missing dependencies (if you installed manually)
**Symptom:** `ModuleNotFoundError: No module named 'equipa'`

**Fix:**
```bash
# Make sure you are in the equipa directory
cd /path/to/equipa
# Run setup again
python3 equipa_setup.py
```

### ForgeSmith not running nightly
**Symptom:** No self-improvement changes after a week

**Fix (Linux/WSL):**
```bash
crontab -l  # Check if cron job exists
# Should see: 0 2 * * * cd ~/.forge && python3 -m scripts.forgesmith --full
# If missing, re-run setup wizard and answer yes to cron setup
```

**Fix (Windows Task Scheduler):**
- Open Task Scheduler
- Look for "EQUIPA ForgeSmith Nightly"
- If missing, re-run setup wizard

### Agent gets stuck / infinite loop
**Symptom:** Agent uses all 15 turns reading files, never writes code

**Fix:**
- Check agent logs: `~/.forge/logs/<task_id>/agent_<role>.log`
- Look for loop detection warnings (EQUIPA kills agents after 3 identical outputs)
- If legitimate complex task, increase max turns in `dispatch_config.json`:
  ```json
  "cost_controls": {
    "default_max_turns": 20
  }
  ```

### Cost overrun
**Symptom:** Task costs $10 instead of expected $0.50

**Fix:**
- Check dispatch config cost limits:
  ```json
  "cost_controls": {
    "cost_limit_low_usd": 0.50,
    "cost_limit_medium_usd": 2.00,
    "cost_limit_high_usd": 5.00
  }
  ```
- Review agent logs — long conversations mean model is struggling
- Consider complexity routing: complex tasks get Opus (expensive), trivial tasks get Haiku (cheap)

### Tests fail after dispatch
**Symptom:** Agent says "tests pass" but `pytest` shows failures

**Fix:**
- Agent assumes your test suite works. If tests are broken, fix them first.
- Check test logs: `~/.forge/logs/<task_id>/test_output.txt`
- Tester role only works if `pytest`, `npm test`, `go test`, etc. are runnable

### Ollama embeddings not working
**Symptom:** No vector similarity in lesson retrieval

**Fix:**
```bash
# Check Ollama is running
curl http://localhost:11434/api/tags
# Should return list of models
# If not, start Ollama
ollama serve
# Pull embedding model if missing
ollama pull nomic-embed-text
```

### Git worktree merge conflicts
**Symptom:** `git worktree prune` shows conflicts after agent run

**Fix:**
- EQUIPA uses worktrees to isolate agent changes
- If merge fails, check `.forge-worktrees/` directory
- Manual merge:
  ```bash
  cd .forge-worktrees/<task_id>
  git status
  # Resolve conflicts
  git add .
  git commit -m "Resolve conflicts"
  cd ../..
  python3 -m equipa.cli task status <task_id>  # Retry merge
  ```

### Preflight check fails
**Symptom:** "Preflight build failed — dependencies not installed"

**Fix:**
- Preflight auto-installs deps if `auto_install_dependencies: true` in dispatch config
- Manual install:
  ```bash
  cd /path/to/project
  # Python
  pip install -r requirements.txt
  # Node
  npm install
  # Go
  go mod download
  ```

---

## Current Limitations

Be honest — here's what does not work well yet:

- **Agents get stuck on complex tasks.** If a task needs 20+ turns of reading, the agent might hit max turns before writing code. Analysis paralysis is real.
- **Git worktree merges occasionally need manual intervention.** Automatic merge works 90% of the time. The other 10% you will fix conflicts by hand.
- **Self-improvement needs 20-30 tasks before patterns emerge.** ForgeSmith learns from failures. If you have run 5 tasks, it has nothing to learn from yet.
- **Tester role depends on a working test suite.** If `pytest` or `npm test` does not run, Tester cannot verify anything.
- **Early termination kills agents at 10 turns of reading.** Some legitimate complex tasks need more. Tune `max_turns` in dispatch config if this happens.
- **Cost routing is not magic.** Haiku is cheap and dumb. Opus is expensive and smart. Medium tasks might get routed wrong — check logs.
- **No web UI.** CLI and MCP only. A dashboard exists (`tools/forge_dashboard.py`) but it is read-only stats, not a control panel.

---

## What's Next

After setup:
1. **Talk to Claude.** Say "Create a task to add input validation to the login form."
2. **Let agents run.** Claude dispatches tasks, monitors progress, reports results.
3. **Check results.** Look at git diffs, test output, agent logs.
4. **Iterate.** If agents fail, they retry up to 3 times automatically. If still stuck, task goes to blockers queue.

ForgeSmith runs nightly to analyze failures and improve prompts. After a few weeks, agents get better at your project's patterns.

**Read next:**
- `README.md` — architecture, roles, self-improvement loop
- `CLAUDE.md` — file map, schema, integration points
- `docs/` — deep dives on specific subsystems
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Contributing](CONTRIBUTING.md)
