<p align="center">
  <img src="ItzamnaIcon.png" alt="Itzamna" width="200">
</p>

<h1 align="center">Itzamna</h1>

<p align="center">
  <strong>Portable installer and onboarding system for ForgeTeam</strong><br>
  Named after the Mayan god of creation, writing, and knowledge
</p>

---

Itzamna makes ForgeTeam — a multi-agent AI orchestration platform — usable by anyone. It replaces all hardcoded paths with portable configuration, creates a fresh database with the full schema, sets up MCP, and gets you from zero to running agents in about 5 minutes.

## What It Does

- **Interactive setup wizard** — prerequisites check, install path selection, DB creation, file copying, config generation, verification
- **Database creation** — empty SQLite database with the full schema (19 tables, 5 views, 7 indexes)
- **Config generation** — `forge_config.json` replaces all hardcoded paths; `mcp_config.json` sets up the MCP server
- **Claude Code integration** — generates `.mcp.json` (MCP access) and `CLAUDE.md` (full context) so you can `cd` into the install and start talking to Claude immediately
- **Custom agents** — drop a `.md` prompt file in `prompts/` and ForgeTeam auto-discovers it
- **Project registration** — `--add-project` CLI command to register new projects in the database and config
- **Full documentation** — quick start guide, user guide, custom agent guide, concurrency benchmarks

## Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | [python.org](https://www.python.org/downloads/) |
| git | [git-scm.com](https://git-scm.com/) |
| gh (GitHub CLI) | [cli.github.com](https://cli.github.com/) |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` |
| uvx / uv | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |

You also need a **Claude Max subscription** or **Anthropic API key**.

## Installation

```bash
git clone https://github.com/sbknana/Itzamna.git
cd Itzamna
python itzamna_setup.py
```

The wizard walks you through everything:

1. Checks all prerequisites are installed
2. Asks where to install ForgeTeam
3. Creates a fresh database with the full schema
4. Copies the orchestrator, agent prompts, and security skills
5. Generates `forge_config.json` and `mcp_config.json`
6. Generates `.mcp.json` for Claude Code MCP access
7. Generates `CLAUDE.md` with full context (commands, queries, agent roles)
8. Verifies the installation (9 automated checks)

## Quick Start

After installation, open Claude Code in your install directory:

```bash
cd ~/ForgeTeam    # or wherever you installed
claude
```

Claude now has MCP access to the database and knows all the commands. Ask it to add projects, create tasks, or run the orchestrator.

Or use the CLI directly:

```bash
# Add your first project
python forge_orchestrator.py --add-project "MyApp" --project-dir "/path/to/myapp"

# Create a task (via MCP or direct SQL)
# Then run a Dev+Test loop
python forge_orchestrator.py --task 1 --dev-test -y

# Or use goal-driven mode for autonomous planning + execution
python forge_orchestrator.py --goal "Add user auth" --goal-project 1 -y

# Auto-run: scan all projects, prioritize, dispatch
python forge_orchestrator.py --auto-run --dry-run
```

## Documentation

| Doc | Description |
|-----|-------------|
| [Quick Start](docs/QUICKSTART.md) | 5-minute getting started guide |
| [User Guide](docs/USER_GUIDE.md) | Full reference — all CLI modes, config, troubleshooting |
| [Custom Agents](docs/CUSTOM_AGENTS.md) | Create new agent roles with a markdown file |
| [Concurrency](docs/CONCURRENCY.md) | Benchmark results (16+ parallel agents) and tuning |

## How It Works

ForgeTeam orchestrates multiple AI agents (Developer, Tester, Planner, Evaluator, SecurityReviewer) that work autonomously on your projects. Agents communicate through a shared SQLite database via MCP, and the orchestrator manages task assignment, iteration loops, and parallel execution.

Itzamna makes this portable by:
- Extracting all hardcoded paths into `forge_config.json`
- Dynamically discovering agent roles from `prompts/*.md` files
- Providing a setup wizard that creates the database and config from scratch

---

*Built by [TheForge, LLC](https://github.com/sbknana) — vibe coded with Claude*
