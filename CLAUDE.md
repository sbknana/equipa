# CLAUDE.md — Itzamna

## Project Overview

**TheForge project_id:** 23
**Status:** Active - v2.1 IMPLEMENTED

Itzamna is the portable installer and onboarding system for the ForgeTeam multi-agent orchestration platform. Named after the Mayan god of creation, writing, and knowledge, Itzamna makes ForgeTeam usable by anyone — not just the original developer.

**What it does:**
- Guided setup: prerequisites check, install path selection, DB creation
- SQLite database creation with the full Itzamna schema (empty, ready to use)
- MCP server configuration (`mcp_config.json` for agents)
- Claude Code integration (`.mcp.json` for user sessions + `CLAUDE.md` for context)
- Config file generation (replacing all hardcoded paths)
- Custom agent role creation (drop a markdown file, it just works)
- Project registration (`--add-project`)
- Quick start guide + full user guide
- Concurrency benchmarks and limits documentation

**Why it exists:**
ForgeTeam is currently hardcoded to one developer's synced storage paths, project mappings, and DB location. Itzamna extracts all of that into a portable, configurable system that anyone can install and use.

---

## Key Architecture Decisions

- Installer is a Python script (same language as ForgeTeam, no additional dependencies)
- All configuration stored in `forge_config.json`
- `forge_orchestrator.py` refactored with `load_config()` to read `forge_config.json` at startup (required — no hardcoded paths)
- `_discover_roles()` dynamically scans `prompts/` directory for agent role `.md` files
- `--add-project` CLI command registers new projects in the DB and config
- Custom agents: drop a `.md` prompt file in the prompts directory, orchestrator auto-discovers it
- Itzamna DB schema is the canonical DDL extracted from the live database
- MCP server setup handled by installer (generates `mcp_config.json` pointing to user's DB)
- Claude Code integration: `.mcp.json` gives user sessions MCP access; `CLAUDE.md` provides full context (commands, queries, roles)
- Stdlib only — no pip dependencies for the installer

---

## Parent Project

Itzamna is the installer/distribution layer for **ForgeTeam** (project_id: 21). ForgeTeam is the orchestration engine; Itzamna makes it installable.

---

## Important Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | This file — project context |
| `itzamna_setup.py` | Interactive setup wizard (9 steps, ~550 lines) |
| `forge_orchestrator.py` | Main orchestrator with context engineering |
| `forgesmith.py` | ForgeSmith self-learning pipeline (nightly cron) |
| `forgesmith_gepa.py` | GEPA — DSPy-based automatic prompt evolution with A/B testing |
| `forgesmith_simba.py` | SIMBA — Targeted rule generation from failure patterns |
| `forgesmith_config.json` | ForgeSmith settings (rubrics, thresholds, limits) |
| `dispatch_config.json` | Agent dispatch settings (models, turns, concurrency) |
| `schema.sql` | Canonical database DDL (28 tables, 7 views) |
| `CHANGELOG.md` | Version history and release notes |
| `docs/QUICKSTART.md` | 5-minute getting started guide |
| `docs/USER_GUIDE.md` | Full user documentation (CLI, config, ForgeSmith, troubleshooting) |
| `docs/CUSTOM_AGENTS.md` | Custom agent creation guide |
| `docs/CONCURRENCY.md` | Benchmark results and tuning guide |

---

## Common Commands

```bash
# Run the setup wizard
python itzamna_setup.py

# After installation, from the install directory:

# Add a project
python forge_orchestrator.py --add-project "MyApp" --project-dir "/path/to/myapp"

# Run a task
python forge_orchestrator.py --task 1 --dev-test -y

# Goal-driven mode
python forge_orchestrator.py --goal "Add feature X" --goal-project 1 -y

# Auto-run all projects
python forge_orchestrator.py --auto-run --dry-run
```

---

## Changes Made to ForgeTeam

The following changes were made to `forge_orchestrator.py` for portability:

1. **`import os`** — Added missing import (used in `_get_repo_env()`)
2. **`load_config()`** — Reads `forge_config.json` at startup, overrides hardcoded paths
3. **`_discover_roles()`** — Dynamically scans `prompts/` for agent role `.md` files
4. **`--add-project`** — CLI command to register new projects in DB + config
5. **`_handle_add_project()`** — Implementation of the add-project command

All changes are platform-independent. `forge_config.json` is required — run `itzamna_setup.py` to generate it.
