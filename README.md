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
- **Checkpoint/Resume.** When an agent times out or hits its turn limit, ForgeTeam saves its progress. Next run of that task automatically picks up where it left off — no wasted work.
- **Adaptive complexity.** Tag tasks as `simple`, `medium`, `complex`, or `epic` and ForgeTeam adjusts turn limits automatically (0.5x to 2x). No tag? It infers complexity from the task description.
- **Model tiering.** Use Haiku for testers, Sonnet for developers, Opus for epic tasks. Per-role and per-complexity model overrides in one config file.
- **Self-improving agents (ForgeSmith).** A nightly self-learning pipeline that analyzes agent performance, extracts lessons from failures, auto-tunes turn limits, and patches agent prompts with targeted advice. Includes DSPy-based prompt evolution (GEPA) and failure-pattern rule generation (SIMBA). Agents get smarter every day without manual intervention.
- **Automatic prompt evolution (GEPA).** DSPy's GEPA optimizer reflects on failure traces and evolves role prompts automatically. Evolved prompts are A/B tested against baselines — underperformers are rolled back automatically. Max 20% change per cycle to prevent drift.
- **Targeted rule generation (SIMBA).** Contrasts successful vs failed episodes to generate specific improvement rules using Claude. Rules are effectiveness-scored and pruned if they don't improve outcomes after 50+ injections.
- **Context engineering.** Token-budget-aware prompt assembly with relevance-scored episode injection, lesson deduplication, and priority-based trimming. Keeps system prompts under 10K tokens without losing critical context.
- **Reflexion system.** After every task, agents reflect on what worked, what didn't, and what they'd do differently. These reflections are stored as episodes with Q-values and injected into future similar tasks — giving agents "experience" to draw on.
- **Rubric-based scoring.** Every agent run is scored against role-specific rubrics (code quality, test coverage, turn efficiency). ForgeSmith uses these scores to evolve rubric weights over time.
- **Zero dependencies.** Pure Python stdlib. No pip, no npm, no Docker. Just Python + Claude Code + a SQLite database.
- **Security by default.** Prompt injection defenses, safe git staging (`git add -u`), and strict MCP config isolation. Agents use the orchestrator's MCP config only.

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
3. Creates a fresh SQLite database (28 tables, 7 views)
4. Copies the orchestrator, ForgeSmith, agent prompts, and security skills
5. Generates all config files (`forge_config.json`, `mcp_config.json`, `.mcp.json`, `CLAUDE.md`)
6. Sets up ForgeSmith nightly cron job for self-improvement
7. Verifies everything works (10 automated checks)

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
                    |  (pure Python)  |  Context engineering, token budgets,
                    |                 |  A/B prompt selection, episode injection
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
                    |  (28 tables)    |  Tasks, decisions, episodes, lessons...
                    +--------+--------+
                             |
        +--------------------+--------------------+
        |                    |                    |
  +-----v------+    +-------v-------+    +-------v-------+
  |  ForgeSmith |    |     SIMBA     |    |     GEPA      |
  | forgesmith  |    | forgesmith_   |    | forgesmith_   |
  |   .py       |    |  simba.py     |    |  gepa.py      |
  | Lessons,    |    | Targeted rule |    | DSPy prompt   |
  | rubrics,    |    | generation    |    | evolution +   |
  | turn tuning |    | via Claude    |    | A/B testing   |
  +-------------+    +---------------+    +---------------+
```

**Pipeline order:** COLLECT → ANALYZE → LESSONS → SIMBA → RUBRICS → APPLY → GEPA → LOG

**How agents communicate:** Every agent gets MCP access to the same SQLite database. The Developer logs decisions and records session notes. The Tester reads the codebase and reports failures. The Planner creates tasks. The Evaluator reviews results. All through the shared database — no custom protocols, no message queues.

**Orchestrator-managed status:** Task status (done/blocked) is updated by the orchestrator based on dev-test outcomes, not by the agents themselves. This eliminates the most common failure mode — agents running out of turns before updating the database.

## Agent Roles

| Role | Job | Can Edit Files | Can Run Builds | DB Access |
|------|-----|:-:|:-:|:-:|
| **Developer** | Write code, fix bugs, implement features | Yes | Yes | Read + Write |
| **Tester** | Run unit tests, report failures | No | Test runners only | Read only |
| **Planner** | Break goals into 2-8 ordered tasks | No | Explore only | Read + Write (tasks) |
| **Evaluator** | Verify goal completion, create follow-ups | No | Explore only | Read + Write (tasks) |
| **Security Reviewer** | 4-phase code security audit | No | Scanning tools only | Read only |
| **Frontend Designer** | Create polished, production-grade UI/UX | Yes | Dev servers | Read + Write |
| **Integration Tester** | Deploy, start, and test full applications end-to-end | No | Full stack | Read only |
| **Debugger** | Trace errors to root cause, fix them, verify | Yes | Yes | Read + Write |
| **Code Reviewer** | Review code quality, consistency, correctness | No | Linters only | Read only |
| **Custom** | Drop a `.md` in `prompts/` — auto-discovered | Configurable | Configurable | Configurable |

## Security Model

ForgeTeam takes a defense-in-depth approach:

- **`--permission-mode bypassPermissions`** — agents run in a controlled sandbox with orchestrator-managed tool access
- **Prompt injection defense** — database content is sanitized before injection into prompts (XML tag escaping, injection pattern filtering)
- **Safe git staging** — `git add -u` instead of `git add .` to prevent accidental secret exposure
- **Strict MCP config** — agents use the orchestrator's MCP config only, ignoring project-level overrides
- **Orchestrator-managed status** — agents can't mark their own tasks done; the orchestrator decides based on test outcomes

## Cost Tracking

Every agent run is logged to the `agent_runs` table with model, duration, turns, and estimated cost:

```sql
-- Cost per project
SELECT * FROM v_cost_by_project;

-- Cost per role
SELECT * FROM v_cost_by_role;
```

## Database

ForgeTeam's persistent memory lives in a SQLite database with 28 tables:

| Group | Tables |
|-------|--------|
| **Core** | projects, tasks, decisions, open_questions, session_notes |
| **Content** | social_media_posts, posting_schedule, content_tickler, writing_style |
| **Research** | research, competitors, product_opportunities |
| **Assets** | code_artifacts, documents, project_assets, components, build_info |
| **System** | cross_references, reminders, agent_runs, voice_messages, api_keys |
| **ForgeSmith** | lessons_learned, agent_episodes, forgesmith_runs, forgesmith_changes, rubric_scores, rubric_evolution_history |

Plus 7 views for dashboards, stale task alerts, content alerts, and cost reports.

Agents access the database through [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) via `mcp-server-sqlite`. No custom database code — just standard SQL through a standard protocol.

## Configuration

| File | Purpose | Generated by setup? |
|------|---------|:---:|
| `forge_config.json` | Project paths, DB path, GitHub owner | Yes |
| `mcp_config.json` | MCP server config for agents | Yes |
| `.mcp.json` | MCP config for your own Claude sessions | Yes |
| `dispatch_config.json` | Auto-run settings (concurrency, model, per-role turns) | Included |
| `forgesmith_config.json` | ForgeSmith self-improvement settings | Included |
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
    "security_review": true,
    "model_tester": "haiku",
    "model_epic": "opus"
}
```

**Per-role turn limits** let you give developers more room for complex tasks while keeping testers lean. The `security_review` flag automatically runs a security audit after every successful dev-test loop.

**Model tiering** lets you assign different models per role or per task complexity. Keys follow the pattern `model_{role}` or `model_{complexity}`:

| Key | Effect |
|-----|--------|
| `model_developer` | Model for Developer agents |
| `model_tester` | Model for Tester agents (default: `haiku`) |
| `model_security_reviewer` | Model for Security Review agents |
| `model_simple` | Model for simple-complexity tasks |
| `model_complex` | Model for complex-complexity tasks |
| `model_epic` | Model for epic-complexity tasks (default: `opus`) |

Priority: complexity model > role model > CLI `--model` > global `model`.

## Checkpoint/Resume

When an agent times out or hits its turn limit, ForgeTeam saves its output to `.forge-checkpoints/`. The next time you run that task, the orchestrator automatically loads the checkpoint and tells the agent to continue where the previous attempt left off.

```
  [Checkpoint] Loaded checkpoint from attempt #1 (4200 chars). Agent will continue from there.
```

This eliminates the biggest source of wasted work — complex tasks that need more than one agent session to complete. Checkpoints are automatically cleared when a task succeeds.

## Adaptive Complexity

Tasks can have a `complexity` level that adjusts turn limits and model selection:

| Complexity | Turn Multiplier | Example |
|:----------:|:---------------:|---------|
| `simple` | 0.5x | Fix a typo, update a config |
| `medium` | 1.0x | Add a new endpoint, write tests |
| `complex` | 1.5x | Refactor auth system, add WebSocket support |
| `epic` | 2.0x | Build an ML pipeline, full-stack feature |

Set complexity explicitly in the database:
```sql
UPDATE tasks SET complexity = 'epic' WHERE id = 42;
```

Or leave it unset — ForgeTeam infers complexity from the task description length. A developer with a base of 50 turns working on an `epic` task gets 100 turns. A tester with a base of 20 turns on a `simple` task gets 10.

## ForgeSmith — Self-Improving Agents

ForgeSmith is ForgeTeam's self-learning pipeline. It runs nightly (via cron) and makes your agents better automatically through a multi-stage optimization process.

### Pipeline Overview

```
COLLECT → ANALYZE → LESSONS → SIMBA → RUBRICS → APPLY → GEPA → LOG
```

| Stage | What It Does |
|-------|-------------|
| **Collect** | Gather agent runs, blocked tasks, and previous change results |
| **Analyze** | Detect max-turns hits, turn underuse, model downgrades, repeat errors |
| **Lessons** | Extract recurring error patterns into reusable lessons |
| **SIMBA** | Generate targeted improvement rules from failure analysis |
| **Rubrics** | Score agent runs and evolve rubric weights |
| **Apply** | Execute config/prompt changes (turn limits, model swaps, patches) |
| **GEPA** | Evolve role prompts using DSPy with A/B testing |
| **Log** | Record the run and all changes for auditing |

### Lesson Extraction

Analyzes failed agent runs, identifies recurring errors (3+ occurrences), and distills actionable lessons. Lessons are stored in `lessons_learned` with an `error_signature` for deduplication. At prompt-build time, the orchestrator injects the most relevant lessons (max 5, deduplicated at 60% word overlap).

### Agent Episodes (Reflexion + MemRL)

After every task, agents write a reflection. ForgeSmith stores these as episodes with Q-values (reinforcement learning signal). When a similar task comes up:

1. The orchestrator scores episodes by **keyword overlap** with the new task description
2. **Recency weighting** favors recent episodes
3. **Q-value filtering** excludes episodes below 0.3
4. Top 2-3 episodes are injected as "past experience"
5. After the task completes, Q-values are updated: +0.1 on success, -0.05 on failure

### SIMBA — Targeted Rule Generation

**S**ystematic **I**dentification of **M**istakes and **B**ehavioral **A**djustments. SIMBA goes beyond generic lesson extraction by using Claude to analyze high-variance tasks — roles that have both successes AND failures on similar work.

**How it works:**
1. Finds roles with mixed outcomes (high variance = most useful for learning)
2. Identifies the "hardest cases" (Q-value < 0.3, early-terminated)
3. Builds a contrast prompt: "Here's what worked vs what didn't"
4. Claude generates up to 3 specific rules per role (max 200 chars each)
5. Rules are validated (length, uniqueness, error type) and stored with unique signatures

**Rule lifecycle:**
- Rules are injected into agent prompts automatically
- After 10+ injections, effectiveness is scored (before vs after success rate)
- After 50+ injections with no improvement, rules are pruned (deactivated)
- Max 3 active SIMBA rules per role at any time

```bash
# Run SIMBA standalone
python forgesmith.py --simba

# SIMBA for a specific role
python forgesmith.py --simba developer

# Prune stale rules
python forgesmith_simba.py --prune
```

### GEPA — Automatic Prompt Evolution

**G**eneralized **E**fficient **P**rompt **A**daptation. Uses DSPy's GEPA optimizer to evolve entire role prompts based on historical episode data. This is the most powerful optimization — it rewrites the prompt instructions themselves.

**How it works:**
1. Collects 60 days of episodes for a role (minimum 20 required)
2. Converts episodes to DSPy Examples (input: task description, output: success/failure)
3. GEPA reflects on failure traces and proposes instruction improvements
4. Evolved prompts are validated against safety rails
5. Version-stamped files are created (e.g., `developer_v2.md`)
6. A/B testing: 50/50 split between evolved and baseline prompts
7. After 10+ tasks on each version, success rates are compared
8. Underperforming versions are automatically rolled back

**Safety rails:**
- Max 20% text change per evolution cycle
- Protected sections never removed (Output Format, RESULT block, Git Commit Requirements)
- Minimum prompt length ≥ 70% of original
- Max 1 evolution per role per week
- Automatic rollback if evolved version underperforms baseline

**Default model:** `ollama_chat/devstral-small-2:24b` (free, local). Set `ANTHROPIC_API_KEY` to use Claude instead.

```bash
# Run GEPA standalone
python forgesmith.py --gepa

# GEPA for a specific role
python forgesmith.py --gepa developer

# Check A/B test status
python forgesmith_gepa.py --status

# Dry run
python forgesmith_gepa.py --dry-run
```

### Context Engineering

The orchestrator's `build_system_prompt()` function assembles agent prompts with token-budget awareness. This ensures agents get the most relevant context without exceeding limits.

**Token budget targets:**
| Metric | Value |
|--------|-------|
| Target prompt size | 8,000 tokens |
| Hard limit | 10,000 tokens |
| Episode reduction threshold | 6,000 tokens (switch from 3→2 episodes) |
| Token estimation | ~4 chars per token |

**Assembly order (never trimmed → first trimmed):**
1. Common rules + role prompt (never trimmed)
2. A/B prompt version selection (GEPA evolved if available)
3. Lesson injection (max 5, deduplicated)
4. Episode injection (relevance-scored, 2-3 based on budget)
5. Task-type guidance (bug_fix, feature, refactor, test)
6. Task description and instructions (never trimmed)
7. Extra context (checkpoint history, test failures)

**Priority trim order** (when over budget):
1. Old episodes (`## Past Experience`, max 500 tokens)
2. Generic lessons (`## Lessons from Previous Runs`, max 300 tokens)
3. Extra context (`## Prior Work Summary`, max 400 tokens)

### Rubric Scoring

Every agent run is scored against role-specific criteria:

| Role | Criteria |
|------|----------|
| **Developer** | result_success (5.5), files_changed (3), tests_written (3), turns_efficiency (2.2), output_compliance (2) |
| **Tester** | tests_pass (5), edge_cases (3), coverage_meaningful (2), false_positives (-2) |
| **Security Reviewer** | vulns_found (3), severity_accuracy (2), false_alarms (-1) |

Weights evolve automatically: ForgeSmith correlates each criterion with actual task success, then adjusts weights by up to ±10% per cycle. Evolution requires 10+ scored runs and looks back 30 days.

### Turn Limit Tuning

- If >30% of runs hit max turns → increase limit by 10 (ceiling: 75)
- If <40% of budget used → decrease limit by 5 (floor: 10)
- Changes logged in `forgesmith_changes` with effectiveness tracking

### Running ForgeSmith

```bash
# Full pipeline (nightly cron)
python forgesmith.py --auto

# Dry run — see what would change
python forgesmith.py --dry-run

# JSON analysis report only
python forgesmith.py --report

# Run specific phases
python forgesmith.py --simba              # SIMBA only
python forgesmith.py --gepa               # GEPA only
python forgesmith.py --propose            # OPRO proposals only

# Inspect current state
python forgesmith.py --lessons            # Show active lessons
python forgesmith.py --rubrics            # Show rubric scores

# Rollback a specific run
python forgesmith.py --rollback RUN_ID
```

### ForgeSmith Configuration

`forgesmith_config.json` controls all ForgeSmith behavior:

```json
{
    "lookback_days": 7,
    "min_sample_size": 5,
    "max_changes_per_run": 5,
    "max_prompt_patches_per_run": 2,
    "blocked_task_max_hours": 24,
    "blocked_task_max_attempts": 3,
    "thresholds": {
        "max_turns_hit_rate": 0.3,
        "turn_underuse_rate": 0.4,
        "simple_task_success_rate": 0.8,
        "repeat_error_count": 3
    },
    "limits": {
        "max_turns_ceiling": 75,
        "max_turns_floor": 10,
        "turn_increase_step": 10,
        "turn_decrease_step": 5
    },
    "rubric_definitions": { "..." },
    "rubric_evolution": {
        "max_weight_change_pct": 10,
        "min_sample_size": 10,
        "evolution_lookback_days": 30
    },
    "protected_files": ["_common.md", "forge_orchestrator.py", "forgesmith.py"],
    "rollback_threshold": -0.3,
    "suppression_cooldown_days": 14
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `lookback_days` | 7 | Days of history to analyze |
| `min_sample_size` | 5 | Minimum runs before making changes |
| `max_changes_per_run` | 5 | Cap on changes per ForgeSmith run |
| `max_prompt_patches_per_run` | 2 | Cap on prompt file modifications |
| `rollback_threshold` | -0.3 | Score below which changes are auto-reverted |
| `suppression_cooldown_days` | 14 | Days before retrying a suppressed change |
| `rubric_definitions` | (per-role) | Scoring criteria and weights |
| `protected_files` | (list) | Files ForgeSmith will never modify |

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
  Built by <a href="https://github.com/[owner]">Forgeborn</a> — vibe coded with Claude
</p>
