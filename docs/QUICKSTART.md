# Itzamna Quick Start Guide

Get ForgeTeam running in 5 minutes.

---

## Prerequisites

Before running the installer, make sure you have:

| Tool | Minimum Version | Install |
|------|----------------|---------|
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) |
| git | 2.40+ | [git-scm.com](https://git-scm.com/) |
| Claude Code CLI | Latest | `npm install -g @anthropic-ai/claude-code` |
| uvx / uv | Latest | `pip install uv` or [docs.astral.sh](https://docs.astral.sh/uv/) |
| gh (GitHub CLI) | 2.0+ | [cli.github.com](https://cli.github.com/) *(optional — only for `--setup-repos`)* |

You also need a Claude Pro/Max subscription or Anthropic API key.

---

## Step 1: Run the Installer

```bash
python itzamna_setup.py
```

The wizard will:
1. Check all prerequisites
2. Ask where to install ForgeTeam
3. Create a fresh database with the full schema (20 tables, 7 views)
4. Copy orchestrator, prompts, and security skills
5. Generate `forge_config.json` and `mcp_config.json`
6. Generate `.mcp.json` (Claude Code MCP integration)
7. Generate `CLAUDE.md` (Claude Code context — commands, queries, roles)
8. Verify everything works (9 automated checks)

---

## Step 2: Open Claude Code

```bash
cd ~/ForgeTeam   # or wherever you installed
claude
```

Claude now has MCP access to the database and full context about all ForgeTeam commands. You can talk to it naturally:

> "Show me all projects"
> "Add a new project called MyApp"
> "Create a todo task for MyApp: Set up the project README"

---

## Step 3: Add Your First Project

Ask Claude, or use the CLI directly:

```bash
python forge_orchestrator.py --add-project "MyApp" --project-dir "C:\path\to\myapp"
```

This creates a project entry in the database and registers the directory in your config.

---

## Step 4: Create a Task

Ask Claude (recommended — it has MCP access):
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

## Step 5: Run Your First Agent

Single developer agent:
```bash
python forge_orchestrator.py --task 1 -y
```

Dev+Test loop (recommended — developer writes code, tester validates):
```bash
python forge_orchestrator.py --task 1 --dev-test -y
```

---

## Step 6: Watch It Work

The orchestrator will:
1. Read the task from the database
2. Load the project context (recent decisions, open questions, last session)
3. Spawn a Developer agent with MCP access to the database
4. The agent writes code and logs session notes
5. (If `--dev-test`) Spawn a Tester agent to validate, loop if tests fail
6. The orchestrator updates the task status based on outcomes (agents don't manage their own status)

---

## What's Next?

- **Use Claude Code** to manage projects, tasks, and decisions — it has full MCP access
- **Add more projects:** `--add-project` for each codebase
- **Goal-driven mode:** `--goal "Add dark mode" --goal-project 1` for autonomous planning
- **Auto-run:** `--auto-run` to scan all projects and dispatch work by priority
- **Custom agents:** Drop a `.md` file in `prompts/` — see [CUSTOM_AGENTS.md](CUSTOM_AGENTS.md)
- **Full reference:** See [USER_GUIDE.md](USER_GUIDE.md)
