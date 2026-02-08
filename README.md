<p align="center">
  <img src="ItzamnaIcon.png" alt="ForgeTeam" width="200">
</p>

<h1 align="center">ForgeTeam</h1>

<p align="center">
  <strong>Multi-agent AI orchestrator for Claude Code</strong><br>
  Zero dependencies. Persistent project memory. Dev+Test loops that actually work.
</p>

<p align="center">
  <em>Named after Itzamna, the Mayan god of creation, writing, and knowledge</em>
</p>

---

ForgeTeam turns Claude Code into a team. Instead of one AI doing everything, you get specialized agents — Developer, Tester, Planner, Evaluator, Security Reviewer — that collaborate on your codebase through a shared database. Give it a goal like "Add user authentication" and walk away. It plans the work, writes the code, tests it, evaluates the results, and loops until it's done.

## Why ForgeTeam?

**You're a solo dev or small team using Claude Code.** You've got 5 projects, 30 tasks, and not enough hours. ForgeTeam fixes that:

- **Persistent memory across sessions.** Decisions, task history, session notes, open questions — all stored in SQLite via MCP. No more re-explaining your project every conversation.
- **Dev+Test loops.** Developer writes code. Tester validates. If tests fail, Developer gets the failures and tries again. Up to 3 cycles, automatically. The orchestrator handles DB status updates — agents never run out of turns before housekeeping.
- **Parallel task execution.** Run multiple tasks concurrently with `--tasks 109-114`. Independent tasks execute in parallel (up to 4 by default), cutting batch times by 3-4x.
- **Goal-driven autonomy.** Say "Add dark mode" and the Planner breaks it into tasks, Dev+Test executes each one, the Evaluator reviews results and creates follow-ups. Multiple rounds until the goal is complete.
- **Auto-dispatch across all projects.** One command scans every project, prioritizes by task urgency, and runs up to 16 parallel agents. Leave it running overnight.
- **Auto security reviews.** After every successful dev-test loop, an optional Security Reviewer agent audits the code using ClaudeStick tools and checks for zero-day vulnerabilities.
- **Auto dependency install.** Detects `pyproject.toml` or `package.json` and installs dependencies before the first agent spawns. No more wasting turns on `pip install`.
- **Per-role turn limits.** Developers get 50 turns, Testers get 20, Security Reviewers get 40. Configurable in `dispatch_config.json`. Agents use their budget where it matters.
- **Zero dependencies.** Pure Python stdlib. No pip, no npm, no Docker. Just Python + Claude Code + a SQLite database.
- **Security by default.** Role-based permission tiers — Testers can't edit files, Planners can't run builds, Security Reviewers can't write to the database. Prompt injection defenses built in.

## Quick Start

### Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | [python.org](https://www.python.org/downloads/) |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` |
| git | [git-scm.com](https://git-scm.com/) |
| uvx / uv | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |

You also need a **Claude Pro/Max subscription** or **Anthropic API key**.

### Install (5 minutes)

```bash
git clone https://github.com/[owner]/Itzamna.git
cd Itzamna
python itzamna_setup.py
```

The setup wizard:
1. Checks prerequisites
2. Asks where to install ForgeTeam
3. Creates a fresh SQLite database (20 tables, 7 views)
4. Copies the orchestrator, agent prompts, and security skills
5. Generates all config files (`forge_config.json`, `mcp_config.json`, `.mcp.json`, `CLAUDE.md`)
6. Verifies everything works (9 automated checks)

### Your first run

```bash
cd ~/ForgeTeam   # wherever you installed

# Register a project
python forge_orchestrator.py --add-project "MyApp" --project-dir "/path/to/myapp"

# Create a task (use Claude with MCP, or direct SQL)
# Then run Dev+Test
python forge_orchestrator.py --task 1 --dev-test -y
```

That's it. The Developer agent writes code, the Tester validates, and they loop until tests pass.

## Modes of Operation

### 1. Single Agent
Run one agent on one task.
```bash
python forge_orchestrator.py --task 42 -y
python forge_orchestrator.py --task 42 --role tester -y
python forge_orchestrator.py --task 42 --role security-reviewer -y
```

### 2. Dev+Test Loop
Developer writes, Tester validates, loop until green.
```bash
python forge_orchestrator.py --task 42 --dev-test -y
```

### 3. Parallel Tasks
Run multiple independent tasks concurrently within a project.
```bash
# Comma-separated IDs
python forge_orchestrator.py --tasks 109,110,111 --dev-test -y

# Range syntax
python forge_orchestrator.py --tasks 109-114 --dev-test -y

# Preview first
python forge_orchestrator.py --tasks 109-114 --dev-test --dry-run
```

### 4. Manager Mode (Goal-Driven)
Give a goal. Planner creates tasks. Dev+Test executes. Evaluator reviews. Repeat.
```bash
python forge_orchestrator.py --goal "Add user authentication" --goal-project 1 -y
```

### 5. Parallel Goals
Run multiple goals across different projects concurrently.
```bash
python forge_orchestrator.py --parallel-goals goals.json -y
```

### 6. Auto-Run
Scan all projects, prioritize, and dispatch agents automatically.
```bash
# Preview what would run
python forge_orchestrator.py --auto-run --dry-run

# Run it
python forge_orchestrator.py --auto-run -y
```

## Architecture

```
                    +-----------------+
                    |  forge_config   |  Your projects, paths, settings
                    +--------+--------+
                             |
                    +--------v--------+
                    |  Orchestrator   |  forge_orchestrator.py
                    |  (pure Python)  |  Reads tasks, spawns agents, manages loops
                    +--------+--------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v---+  +------v------+  +----v-------+
     |  Developer  |  |   Tester    |  |  Planner   |  ...agents
     |  (claude)   |  |  (claude)   |  |  (claude)  |
     +--------+----+  +------+------+  +----+-------+
              |              |              |
              +--------------+--------------+
                             |
                    +--------v--------+
                    |   TheForge DB   |  SQLite via MCP
                    |  (20 tables)    |  Tasks, decisions, sessions, research...
                    +-----------------+
```

**How agents communicate:** Every agent gets MCP access to the same SQLite database. The Developer logs decisions and records session notes. The Tester reads the codebase and reports failures. The Planner creates tasks. The Evaluator reviews results. All through the shared database — no custom protocols, no message queues.

**Orchestrator-managed status:** Task status (done/blocked) is updated by the orchestrator based on dev-test outcomes, not by the agents themselves. This eliminates the most common failure mode — agents running out of turns before updating the database.

## Agent Roles

| Role | Job | Can Edit Files | Can Run Builds | DB Access |
|------|-----|:-:|:-:|:-:|
| **Developer** | Write code, fix bugs, implement features | Yes | Yes | Read + Write |
| **Tester** | Run tests, report failures | No | Test runners only | Read only |
| **Planner** | Break goals into 2-8 ordered tasks | No | Explore only | Read + Write (tasks) |
| **Evaluator** | Verify goal completion, create follow-ups | No | Explore only | Read + Write (tasks) |
| **Security Reviewer** | 4-phase code security audit | No | Scanning tools only | Read only |
| **Custom** | Drop a `.md` in `prompts/` — auto-discovered | Configurable | Configurable | Configurable |

## Security Model

ForgeTeam uses **tiered role-based permissions** instead of blanket access:

- **`--permission-mode dontAsk`** — auto-denies tools unless explicitly whitelisted
- **`--allowedTools`** — each role gets only the tools it needs
- **`--disallowedTools`** — dangerous tools removed from agent context entirely
- **Prompt injection defense** — database content is sanitized before injection into prompts (XML tag escaping, injection pattern filtering)
- **Safe git staging** — `git add -u` instead of `git add .` to prevent accidental secret exposure
- **Strict MCP config** — agents use the orchestrator's MCP config only, ignoring project-level overrides

Permission overrides are configurable per-role in `forge_config.json`:
```json
{
    "role_permissions": {
        "developer": {
            "extra_allowed_tools": ["Bash(make *)"],
            "extra_disallowed_tools": []
        }
    }
}
```

## Cost Tracking

Every agent run is logged to the `agent_runs` table with model, duration, turns, and estimated cost:

```sql
-- Cost per project
SELECT * FROM v_cost_by_project;

-- Cost per role
SELECT * FROM v_cost_by_role;
```

## Database

ForgeTeam's persistent memory lives in a SQLite database with 20 tables:

| Group | Tables |
|-------|--------|
| **Core** | projects, tasks, decisions, open_questions, session_notes |
| **Content** | social_media_posts, posting_schedule, content_tickler, writing_style |
| **Research** | research, competitors, product_opportunities |
| **Assets** | code_artifacts, documents, project_assets, components, build_info |
| **System** | cross_references, reminders, agent_runs |

Plus 7 views for dashboards, stale task alerts, content alerts, and cost reports.

Agents access the database through [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) via `mcp-server-sqlite`. No custom database code — just standard SQL through a standard protocol.

## Configuration

| File | Purpose | Generated by setup? |
|------|---------|:---:|
| `forge_config.json` | Project paths, DB path, GitHub owner | Yes |
| `mcp_config.json` | MCP server config for agents | Yes |
| `.mcp.json` | MCP config for your own Claude sessions | Yes |
| `dispatch_config.json` | Auto-run settings (concurrency, model, per-role turns) | Included |
| `CLAUDE.md` | Full context for Claude Code | Yes |

### dispatch_config.json

```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "max_turns": 25,
    "max_turns_developer": 50,
    "max_turns_tester": 20,
    "max_turns_security_reviewer": 40,
    "max_tasks_per_project": 5,
    "security_review": true
}
```

Per-role turn limits let you give developers more room for complex tasks while keeping testers lean. The `security_review` flag automatically runs a security audit after every successful dev-test loop.

## Documentation

| Doc | Description |
|-----|-------------|
| [Quick Start](docs/QUICKSTART.md) | 5-minute getting started guide |
| [User Guide](docs/USER_GUIDE.md) | Full reference — all CLI modes, config, troubleshooting |
| [Custom Agents](docs/CUSTOM_AGENTS.md) | Create new agent roles with a markdown file |
| [Concurrency](docs/CONCURRENCY.md) | Benchmark results (16 parallel agents) and tuning |

## Concurrency

ForgeTeam was benchmarked at 16 concurrent multi-turn agents with zero throttling on Claude Max. The local machine is the bottleneck, not the API:

| Machine | Recommended `max_concurrent` |
|---------|:---:|
| 8 GB RAM | 2-3 |
| 16 GB RAM | 4-6 |
| 32 GB RAM | 8-12 |
| 64 GB+ RAM | 12-16 |

See [CONCURRENCY.md](docs/CONCURRENCY.md) for full benchmark data.

## Contributing

Contributions welcome. ForgeTeam is built for the vibe coding community — if you're using Claude Code to build software, this is for you.

1. Fork the repo
2. Create a branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Test with `--dry-run` to verify prompt generation
5. Submit a PR

## License

[Apache License 2.0](LICENSE) — use it commercially, modify it, distribute it. Just include the license and attribution.

---

<p align="center">
  Built by <a href="https://github.com/[owner]">TheForge, LLC</a> — vibe coded with Claude
</p>
