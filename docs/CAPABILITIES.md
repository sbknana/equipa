# EQUIPA: Multi-Agent AI Orchestration Platform

EQUIPA is a self-improving, multi-agent orchestration platform that you control through natural conversation. Tell Claude what you want done -- it handles task management, agent dispatch, progress tracking, and reporting. You make decisions; the system handles execution.

Built in pure Python stdlib on SQLite. Zero pip dependencies. Runs anywhere Python and an MCP-compatible AI coding tool are installed.

---

## Table of Contents

- [Conversational Development](#conversational-development)
- [Multi-Agent Orchestration](#multi-agent-orchestration)
- [ForgeSmith Self-Improvement Engine](#forgesmith-self-improvement-engine)
- [Security Pipeline](#security-pipeline)
- [Database Migration System](#database-migration-system)
- [Inter-Agent Communication](#inter-agent-communication)
- [Observability and Cost Tracking](#observability-and-cost-tracking)
- [Extensibility](#extensibility)
- [Autoresearch: Automated Prompt Optimization](#autoresearch-automated-prompt-optimization)
- [Technical Highlights](#technical-highlights)

---

## Conversational Development

The primary interface to EQUIPA is **natural language conversation with Claude**. Claude has MCP access to the full project database and knows how to use every tool in the system. You never need to memorize CLI commands, look up task IDs, or write SQL.

### What it looks like in practice

| You say | What happens behind the scenes |
|---------|-------------------------------|
| "What's the status of our Loom project?" | Claude queries project dashboard, recent sessions, open tasks, and blockers |
| "What tasks are outstanding?" | Claude pulls all pending/in-progress tasks, groups by project and priority |
| "Work on the next high-priority Loom task" | Claude finds the task, loads project context, dispatches developer + tester agents, reports results |
| "Add a task for Loom: implement dark mode" | Claude creates the task with correct project ID and priority |
| "Run a security review on what we just shipped" | Claude dispatches security-reviewer with Trail of Bits tooling |
| "How much have we spent this month?" | Claude queries cost tracking views, breaks down by project and role |
| "What did we decide about the auth approach?" | Claude searches the decisions table and returns the rationale |
| "Update TheForge with what we did today" | Claude logs session notes, updates task statuses, records decisions |

### You make decisions. Claude manages everything else.

The entire project management layer -- task creation, prioritization, context loading, agent dispatch, progress tracking, session logging, and reporting -- is handled conversationally. You stay focused on **what** to build. The system handles **how** to coordinate the work.

### The CLI still exists

Everything above can also be done from the command line for automation (cron jobs, CI/CD, scripted workflows). But for day-to-day work, you never touch it. See [CLI Reference](#cli-reference) in the README.

---

## Multi-Agent Orchestration

Under the hood, EQUIPA dispatches work to **12+ specialized agent roles**, each with tailored system prompts, turn budgets, model assignments, and injected context from past experience.

| Role | Purpose | Default Model | Turn Budget | Success Rate |
|------|---------|---------------|-------------|--------------|
| **Developer** | Writes code, implements features, fixes bugs | Sonnet | 45 | 100% |
| **Tester** | Validates code, writes tests, verifies fixes | Sonnet | 75 | 100% |
| **Planner** | Breaks goals into prioritized, dependency-aware tasks | Opus | 20 | — |
| **Evaluator** | Scores agent output against rubrics | Sonnet | 25 | — |
| **Security Reviewer** | Deep security audit with Trail of Bits tooling | Opus | 50 | 100% |
| **Frontend Designer** | UI/UX implementation and design review | Opus | 40 | 100% |
| **Integration Tester** | Cross-component and API testing | Sonnet | 20 | — |
| **Debugger** | Root-cause analysis and targeted fixes | Opus | 30 | 83.4% |
| **Code Reviewer** | Quality, style, and architecture review | Sonnet | 75 | — |
| **Economy Tester** | Validates economic/transaction logic | Sonnet | 30 | 100% |
| **Story Tester** | Validates narrative/content logic | Sonnet | 30 | 100% |
| **World Builder** | World/environment design and lore | Sonnet | 30 | — |

*Success rates from autoresearch benchmarks (last 15 runs per role). Roles marked "—" are not yet benchmarked or use different evaluation criteria.*

### Dev-Test Cycle

The core execution loop pairs a Developer agent with a Tester agent. The developer writes code; the tester validates it. They iterate until tests pass or the turn budget is exhausted. Failed test output feeds directly into the developer's next attempt with full context.

### Goal-Driven Autonomous Mode

Tell Claude your goal in plain English and it decomposes the work into concrete tasks, prioritizes them by dependency and complexity, and dispatches agents automatically.

> "Add JWT authentication to the API" --> Planner creates 5 tasks --> Developer + Tester work through each one --> Security reviewer audits the result

### Auto-Run Scanning

Auto-run mode scans all registered projects for pending tasks, scores them by priority, and dispatches work across projects in parallel -- up to 4 concurrent agents by default.

```mermaid
flowchart TD
    subgraph Dispatch
        CLI["CLI / Cron"] --> Orch["Orchestrator"]
        Orch --> Score["Score & Prioritize Tasks"]
        Score --> Route["Route by Role + Complexity"]
    end

    subgraph "Dev-Test Loop"
        Route --> Dev["Developer Agent"]
        Dev --> |"Code changes"| Test["Tester Agent"]
        Test --> |"PASS"| Done["Mark Complete"]
        Test --> |"FAIL + context"| Dev
    end

    subgraph "Post-Completion"
        Done --> Episode["Record Episode + Q-Value"]
        Done --> SecCheck{"Security Review Enabled?"}
        SecCheck --> |"Yes"| SecAgent["Security Reviewer"]
        SecCheck --> |"No"| Fin["Done"]
        SecAgent --> Fin
    end

    subgraph "Learning"
        Episode --> FS["ForgeSmith Analysis"]
        FS --> Lessons["Extract Lessons"]
        FS --> Tune["Tune Config"]
        FS --> Evolve["Evolve Prompts"]
    end
```

---

## ForgeSmith Self-Improvement Engine

ForgeSmith is a three-tier learning system that analyzes completed agent runs and evolves the platform's behavior over time. It operates on different granularities and timescales, from quick config adjustments to full prompt rewrites.

### Tier 1: Core ForgeSmith

Rule-based analysis of recent agent runs. Detects patterns and makes targeted adjustments.

- **Lesson extraction** -- Identifies repeated failures and generates lessons injected into future prompts
- **Turn limit tuning** -- Detects turn overuse and underuse; adjusts budgets per role
- **Model assignment** -- Routes complex tasks to stronger models, simple tasks to faster ones
- **Config patching** -- Adjusts thresholds, concurrency limits, and dispatch weights
- **Effectiveness scoring** -- Scores all changes before/after. Changes below the rollback threshold (-0.3) are automatically reverted

### Tier 2: GEPA Prompt Evolution

Generalized Efficient Prompt Adaptation. Uses DSPy-style optimization to evolve entire system prompts.

- **Evolutionary rewriting** -- Reflects on failure traces and proposes prompt mutations
- **A/B testing** -- Evolved prompts run in 50/50 split against baselines. Underperformers rolled back after 10+ tasks
- **Safety rails** -- Maximum 20% prompt change per cycle. Protected sections cannot be modified
- **Rollback** -- Automatic reversion if evolved prompts underperform the baseline

### Tier 3: SIMBA Rule Synthesis

Systematic Identification of Mistakes and Behavioral Adjustments. Targets the hardest cases.

- **Contrastive analysis** -- Compares successful vs. failed episodes for the same task type
- **Rule generation** -- Uses Claude to synthesize specific behavioral rules from failure patterns
- **Effectiveness tracking** -- Each rule carries an effectiveness score; stale or ineffective rules are auto-pruned
- **Signature deduplication** -- Prevents redundant rules from accumulating

```mermaid
flowchart LR
    subgraph "Tier 1: Core ForgeSmith"
        Collect["Collect Recent Runs"] --> Analyze["Pattern Analysis"]
        Analyze --> Lessons["Extract Lessons"]
        Analyze --> ConfigTune["Tune Turn Limits"]
        Analyze --> EvalPrev["Evaluate Previous Changes"]
        EvalPrev --> |"Score < -0.3"| Rollback["Auto-Rollback"]
    end

    subgraph "Tier 2: GEPA"
        FailTraces["Failure Traces"] --> Reflect["Reflect on Failures"]
        Reflect --> Mutate["Propose Prompt Mutation"]
        Mutate --> AB["A/B Test vs Baseline"]
        AB --> |"Outperforms"| Promote["Promote New Prompt"]
        AB --> |"Underperforms"| Revert["Revert to Baseline"]
    end

    subgraph "Tier 3: SIMBA"
        HardCases["High-Variance Episodes"] --> Contrast["Success vs Failure Contrast"]
        Contrast --> Rules["Generate Behavioral Rules"]
        Rules --> Score["Effectiveness Scoring"]
        Score --> |"Effective"| Inject["Inject into Future Runs"]
        Score --> |"Stale"| Prune["Auto-Prune"]
    end

    Analyze --> FailTraces
    Analyze --> HardCases
    Promote --> Inject
```

### Episodic Memory with Q-Values

Every agent run is recorded as an episode with a reinforcement learning Q-value. High-Q episodes are preferentially injected into future runs. Q-values update based on outcomes: +0.1 on success, -0.05 on failure. This creates a reinforcement learning loop without explicit training infrastructure.

---

## Security Pipeline

EQUIPA's security review capability is not a checkbox -- it is a full-depth analysis pipeline built on Trail of Bits security skills.

### 7 Security Skills

| Skill | What It Does |
|-------|-------------|
| **Static Analysis** | Semgrep + CodeQL automated vulnerability scanning |
| **Audit Context Building** | Deep architectural analysis -- trust boundaries, data flows, external inputs |
| **Variant Analysis** | Finds similar vulnerabilities across the entire codebase |
| **Differential Review** | Security-focused review of code changes (diffs) |
| **Fix Review** | Validates that security fixes are correct and complete |
| **Semgrep Rule Creator** | Generates custom detection rules for recurring vulnerability patterns |
| **Sharp Edges** | Identifies dangerous APIs and footgun designs |

### How It Works

1. **Context building** -- The security reviewer reads the target codebase, maps trust boundaries, and identifies data flows
2. **Automated scanning** -- Runs Bandit (Python), Semgrep, and CodeQL against the project
3. **Manual deep review** -- 10-category manual analysis covering the OWASP Top 10 and beyond: injection, auth/authz, secrets exposure, input validation, cryptography, error handling, dependency risks, file operations, network security, and business logic
4. **Findings logged** -- Each finding gets a severity rating (Critical/High/Medium/Low/Info) and is recorded in the database

### Configuration

The security reviewer runs with **Opus** and a **50-turn budget** -- the deepest analysis allocation of any role. It is **exempt from early termination**, meaning the loop detector will not cut a security review short. When `security_review: true` is set in dispatch config, security reviews auto-dispatch after every completed dev-test cycle.

---

## Database Migration System

`db_migrate.py` implements a linear migration chain (v0 through v3) with automatic version detection, backup, and audit logging. It is stdlib-only and has been benchmarked for correctness.

### Version Detection

Two-strategy version detection handles both new and legacy databases:

1. **PRAGMA user_version** -- The standard SQLite version marker, set by migrations and `schema.sql`
2. **Table fingerprinting** -- For databases that predate the migration system, detects version by which tables exist (e.g., `agent_messages` + `agent_actions` = v3, `lessons_learned` + `rubric_scores` = v2)

### Migration Guarantees

- **Timestamped backup** before any schema change
- **Sequential migration application** -- Each step is atomic; partial failures leave the DB in a known state
- **Audit logging** -- Every migration step recorded in `schema_migrations` with from/to version and timestamp
- **Idempotent operations** -- `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, duplicate-column-safe `ALTER TABLE`

### Benchmark Results

Reproducible benchmarks via `benchmark_migrations.py`. The benchmark creates realistic databases at each schema version with populated data, runs the full migration chain, and verifies zero data loss.

| Upgrade Path | Time | Data Preserved | Status |
|-------------|------|----------------|--------|
| **v1 -> v3** | 103ms | 88 rows across all tables | PASS |
| **v2 -> v3** | 37ms | 207 rows across all tables | PASS |
| **v3 -> v3 (no-op)** | < 1ms | All data untouched | PASS |
| **Fresh install** | < 1ms | N/A | PASS |

**Verification suite:**
- Per-table row counts verified before and after migration
- Spot checks on specific row content (project names, task titles, decision text)
- Backup file integrity verification (valid SQLite, row counts match pre-migration)
- New tables confirmed empty (no phantom data)
- PRAGMA user_version set correctly after each step
- Audit log entries validated for every migration step

---

## Inter-Agent Communication

Added in v3.0, the inter-agent messaging system enables structured communication between agents across dev-test cycles.

### Structured Message Passing

Agents post typed messages to each other via the `agent_messages` table. The tester posts test results (pass/fail/blocked) as structured messages. The developer reads unread messages at the start of each cycle. Messages are scoped by task and cycle, with read-tracking so agents never process the same message twice.

```sql
-- Tester posts results for the developer
INSERT INTO agent_messages (task_id, cycle_number, from_role, to_role, message_type, content)
VALUES (42, 3, 'tester', 'developer', 'test_result', 'FAIL: 2 of 5 tests failing...');

-- Developer reads unread messages at cycle start
SELECT * FROM agent_messages WHERE task_id = 42 AND to_role = 'developer' AND read_by_cycle IS NULL;
```

### Per-Tool Action Logging

Every tool call during agent execution is logged to `agent_actions` with:

- **Input hashing** (SHA256) -- Enables dedup detection. If an agent calls the same tool with the same input twice, it is likely stuck
- **Error classification** -- Errors are categorized by type for ForgeSmith pattern analysis
- **Output length tracking** -- Detects abnormally large or empty outputs
- **Duration tracking** -- Per-action timing for performance profiling

### Error Classification and Dedup Detection

The orchestrator classifies errors into typed categories (compilation, test failure, timeout, permission, etc.) and tracks them per run. Combined with input hashing on agent actions, this surfaces repeated failing approaches and feeds the LoopDetector -- which uses deterministic fingerprinting of agent outputs to detect and terminate stuck loops.

---

## Observability and Cost Tracking

### Per-Run Cost Tracking

Every agent run records model used, turns consumed, wall-clock duration, and estimated cost in USD. This data feeds into two built-in views:

- **`v_cost_by_project`** -- Total runs, turns, duration, and cost broken down by project
- **`v_cost_by_role`** -- Same breakdown by agent role, including average cost per run

### ForgeSmith Analysis Pipeline

ForgeSmith runs are themselves logged with full metadata: number of runs analyzed, changes proposed, changes applied, and summary. Each change carries an effectiveness score computed from before/after comparison of agent performance.

### Episode Q-Values

The episodic memory system assigns Q-values to every agent episode. These values decay or improve based on real outcomes, creating a lightweight reinforcement learning signal without any training infrastructure. High-Q episodes are preferentially selected for injection into future agent prompts.

### Dashboard

`forge_dashboard.py` provides a terminal-based view of:

- Task completion rates by project and priority
- Blocked items and open questions
- Session activity over time
- Agent performance trends

---

## Extensibility

### Drop-In Custom Agents

Create a new agent role by dropping a Markdown file into the `prompts/` directory. The orchestrator auto-discovers it via `_discover_roles()`. No code changes required.

```
prompts/
  developer.md
  tester.md
  security-reviewer.md
  my-custom-role.md      <-- auto-discovered
```

### Configurable Permissions Per Role

`dispatch_config.json` controls per-role settings:

- Model assignment (Opus for complex work, Sonnet for review tasks)
- Turn budgets per role
- Provider selection per role (Claude API, local Ollama)
- Task-type-specific prompt guidance (bug fix, feature, refactor, test)

### Multi-Tool MCP Integration

EQUIPA auto-detects and configures MCP for all major AI coding tools during setup:

| Tool | Config Location | Detection |
|------|----------------|-----------|
| **Claude Code** | `.mcp.json` (project root) | `claude --version` |
| **Roo Code** | `~/.roo/mcp.json` | Extension directory |
| **Cline** | `~/cline_mcp_settings.json` | Extension directory |
| **Cursor** | `.cursor/mcp.json` (project root) | `cursor --version` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | Config directory |
| **Continue.dev** | `~/.continue/config.yaml` | Config directory |

The installer detects which tools are installed, prompts for confirmation, and merges the MCP server entry into each tool's config. Existing configurations are preserved — EQUIPA only adds its own server entry.

### Local LLM Support via Ollama

Read-only roles (planner, evaluator, code-reviewer, researcher) can run on local models via Ollama at zero API cost. The provider abstraction handles routing transparently -- configure once in `dispatch_config.json` and the orchestrator handles the rest.

```json
{
    "provider": "claude",
    "provider_planner": "ollama",
    "provider_evaluator": "ollama",
    "ollama_base_url": "http://localhost:11434",
    "ollama_model": "qwen3.5:27b"
}
```

---

## Autoresearch: Automated Prompt Optimization

Autoresearch is the automated prompt optimization loop. It takes agent prompts, mutates them using Opus, benchmarks the mutations against real tasks, and keeps changes that improve success rates. Think of it as A/B testing on autopilot — but with an AI generating the variants.

### How It Works

1. **Collect metrics** — Pull recent success rates from the `agent_runs` table (sliding window of last 15 runs per role)
2. **Analyze failures** — Identify the top failure patterns (early termination, timeout, cycles exhausted, etc.)
3. **Mutate prompt** — Feed the current prompt + failure analysis to Opus, which generates a targeted rewrite
4. **Reset project state** — Hard-reset benchmark projects to a tagged baseline (undoes previous benchmark commits)
5. **Deploy and benchmark** — Write the mutated prompt, create benchmark tasks, dispatch agents via the orchestrator
6. **Evaluate** — Compare round results. Keep mutations that hit the target; revert regressions
7. **Loop** — Repeat until target success rate is reached or max rounds exhausted

### Two Components

| File | Role |
|------|------|
| `autoresearch_loop.py` | The full optimization loop — mutate, deploy, benchmark, evaluate, commit/revert |
| `autoresearch_prompts.py` | Standalone prompt mutation generator with tiered LLM support (Ollama → Sonnet → Opus) |

`autoresearch_loop.py` is the production system. It runs end-to-end on Claudinator, dispatching real agents on real tasks and measuring actual outcomes. `autoresearch_prompts.py` is the lighter tool for generating prompt mutations without running the full benchmark loop.

### Current Results

As of March 2026, autoresearch has optimized 7 roles:

| Role | Success Rate | Target | Status |
|------|-------------|--------|--------|
| Developer | 100% | 80% | Hit |
| Tester | 100% | 85% | Hit |
| Frontend Designer | 100% | 80% | Hit |
| Security Reviewer | 100% | 85% | Hit |
| Economy Tester | 100% | 80% | Hit |
| Story Tester | 100% | 80% | Hit |
| Debugger | 83.4% | 80% | Hit |

### CLI Usage

```bash
# Check current success rates for all roles
python3 autoresearch_loop.py --status

# Optimize a single role (target 80% by default)
python3 autoresearch_loop.py --role developer --target 80

# Optimize all underperforming roles
python3 autoresearch_loop.py --all --target 80

# Limit optimization rounds
python3 autoresearch_loop.py --role debugger --max-rounds 5

# Rollback a role to its last backup
python3 autoresearch_loop.py --rollback developer
```

### How It Relates to ForgeSmith

ForgeSmith and Autoresearch solve different problems at different scales:

- **ForgeSmith/GEPA** — Incremental prompt evolution. Small mutations, A/B tested over weeks. Conservative. Runs on a cron schedule.
- **Autoresearch** — Aggressive prompt search. Larger rewrites, benchmarked in hours. Runs on-demand when a role needs improvement.
- **ForgeSmith/SIMBA** — Generates behavioral rules injected alongside prompts. Complementary to both.

In practice: use Autoresearch to get a role to its target success rate, then let ForgeSmith maintain it over time.

### Benchmark Task Design

Each role has 3 rotating pools of benchmark tasks (rotated per round to avoid "already exists" collisions). Tasks are intentionally simple and well-scoped — the goal is to test the agent's ability to follow instructions and produce working code, not to test the model's raw intelligence.

Benchmark projects are reset to a tagged baseline (`autoresearch-baseline`) between rounds via `git reset --hard`. This ensures each round starts from a clean state.

---

## Technical Highlights

| Property | Detail |
|----------|--------|
| **Zero pip dependencies** | Core platform is stdlib-only Python. No `requirements.txt`. Requires Python 3.10+, an MCP-compatible AI coding tool, git, and uvx as runtime prerequisites |
| **Multi-tool MCP support** | Auto-configures Claude Code, Roo Code, Cline, Cursor, Windsurf, and Continue.dev. Detects installed tools and merges config non-destructively |
| **SQLite-based** | Single-file database. Trivially portable, trivially backupable. No database server required |
| **Cross-platform** | Runs on Linux and Windows. Tested on Ubuntu 24.04 and Windows 10/11 |
| **CLI-first** | Every operation available from the command line. No web UI required (dashboard is terminal-based) |
| **Git worktree isolation** | Parallel tasks run in isolated branches. Merged on success, preserved on failure -- no work lost |
| **Per-role skills** | Specialized skills loaded per agent role at task start. Concrete methods, checklists, and decision tables |
| **Quality scoring** | 5-dimension post-task quality scoring with role-specific weights, stored in DB for ForgeSmith analysis |
| **Failure classification** | Structured taxonomy (`analysis_paralysis`, `build_failure`, `timeout`, etc.) feeds targeted SIMBA/GEPA improvements |
| **Change-impact analysis** | Blast-radius assessment blocks HIGH-risk ForgeSmith mutations from auto-apply |
| **Checkpoint resilience** | Long-running tasks save checkpoints. Interrupted work resumes from the last saved state |
| **Loop detection** | Deterministic fingerprinting of agent output detects stuck loops. File changes reset the counter -- progress is recognized |
| **Dynamic budgets** | Turn budgets adapt based on task complexity and intermediate results. Prevents both premature termination and runaway loops |
| **30+ table schema** | Full relational model covering projects, tasks, episodes, lessons, messages, actions, rules, prompt versions, rubric scores, and migration history |
| **Reproducible benchmarks** | Migration benchmarks generate realistic test data, run the full chain, and verify zero data loss |
| **Arena mode** | Adversarial testing system that stress-tests agents and exports results as LoRA fine-tuning data |
| **Autoresearch** | Automated prompt optimization loop — mutates prompts via Opus, benchmarks against real tasks, keeps improvements. 6/7 roles at 100% success |

---

<p align="center">
  <strong>Built by Forgeborn</strong><br>
  Vibe coded with Claude<br>
  <em>&copy; 2026 Forgeborn</em>
</p>
