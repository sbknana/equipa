# Itzamna Quick Start Guide

Get ForgeTeam running in 5 minutes.

---

## Prerequisites

Before running the installer, make sure you have:

| Tool | Minimum Version | Install |
|------|----------------|---------|
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) |
| git | 2.40+ | [git-scm.com](https://git-scm.com/) |
| gh (GitHub CLI) | 2.0+ | [cli.github.com](https://cli.github.com/) |
| Claude Code CLI | Latest | `npm install -g @anthropic-ai/claude-code` |
| uvx / uv | Latest | `pip install uv` or [docs.astral.sh](https://docs.astral.sh/uv/) |

You also need a Claude Max subscription or Anthropic API key.

---

## Step 1: Run the Installer

```bash
python itzamna_setup.py
```

The wizard will:
1. Check all prerequisites
2. Ask where to install ForgeTeam
3. Create a fresh database with the full schema
4. Copy orchestrator, prompts, and security skills
5. Generate `forge_config.json` and `mcp_config.json`
6. Verify everything works

---

## Step 2: Add Your First Project

```bash
python forge_orchestrator.py --add-project "MyApp" --project-dir "C:\path\to\myapp"
```

This creates a project entry in the database and registers the directory in your config.

---

## Step 3: Create a Task

Using Claude with MCP (recommended):
> "Create a todo task for MyApp: Set up the project README"

Or via direct SQL:
```bash
python -c "
import sqlite3
conn = sqlite3.connect('theforge.db')
conn.execute(\"INSERT INTO tasks (project_id, title, status, priority) VALUES (1, 'Set up project README', 'todo', 'medium')\")
conn.commit()
"
```

---

## Step 4: Run Your First Agent

Single developer agent:
```bash
python forge_orchestrator.py --task 1 -y
```

Dev+Test loop (recommended — developer writes code, tester validates):
```bash
python forge_orchestrator.py --task 1 --dev-test -y
```

---

## Step 5: Watch It Work

The orchestrator will:
1. Read the task from the database
2. Load the project context (recent decisions, open questions, last session)
3. Spawn a Developer agent with MCP access to the database
4. The agent writes code, updates the task status, and logs session notes
5. (If `--dev-test`) Spawn a Tester agent to validate, loop if tests fail

---

## What's Next?

- **Add more projects:** `--add-project` for each codebase
- **Goal-driven mode:** `--goal "Add dark mode" --goal-project 1` for autonomous planning
- **Auto-run:** `--auto-run` to scan all projects and dispatch work by priority
- **Custom agents:** Drop a `.md` file in `prompts/` — see [CUSTOM_AGENTS.md](CUSTOM_AGENTS.md)
- **Full reference:** See [USER_GUIDE.md](USER_GUIDE.md)
