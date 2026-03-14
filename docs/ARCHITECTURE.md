# ARCHITECTURE.md — EQUIPA

## Table of Contents

- [ARCHITECTURE.md — EQUIPA](#architecturemd-equipa)
  - [How It Works](#how-it-works)
  - [System Overview](#system-overview)
  - [Data Flow](#data-flow)
    - [Typical Task Execution](#typical-task-execution)
    - [Self-Improvement Cycle (Forgesmith)](#self-improvement-cycle-forgesmith)
  - [Database](#database)
  - [Project Structure](#project-structure)
  - [Key Design Decisions](#key-design-decisions)
    - [Pure Python stdlib, zero pip dependencies](#pure-python-stdlib-zero-pip-dependencies)
    - [SQLite as the single source of truth](#sqlite-as-the-single-source-of-truth)
    - [Closed-loop self-improvement](#closed-loop-self-improvement)
    - [Aggressive early termination](#aggressive-early-termination)
    - [Lesson sanitization as a security boundary](#lesson-sanitization-as-a-security-boundary)
    - [Dev-test loop as the core execution pattern](#dev-test-loop-as-the-core-execution-pattern)
    - [Multi-tier model routing](#multi-tier-model-routing)
    - [Prompt evolution with safety rails](#prompt-evolution-with-safety-rails)
  - [Related Documentation](#related-documentation)

## How It Works

EQUIPA is a multi-agent AI orchestration platform where you describe what you want built in plain English, and a fleet of AI agents (developer, tester, security reviewer, planner) execute the work autonomously. Here's how it works in practice:

**When you give EQUIPA a task**, the `forge_orchestrator.py` scans a SQLite database for pending work, figures out which agent role should handle it (developer, tester, security reviewer), builds a detailed prompt with relevant context (project info, past lessons learned, prior episode outcomes), and dispatches the task to Claude (or optionally a local Ollama model). The developer agent writes code, the tester agent validates it, and they iterate in a dev-test loop until the task passes or budget/turn limits are hit.

**While agents run**, the orchestrator tracks everything: it detects stuck loops (agents repeating the same failed action), monologue behavior (agents talking instead of using tools), cost overruns, and alternating error patterns. If an agent gets stuck, the system warns it, and if it doesn't recover, terminates early. Every action, error, and outcome is logged to SQLite as "episodes" — structured records of what happened, what worked, and what didn't.

**After tasks complete**, the system learns from its own history. `forgesmith.py` is the self-improvement engine: it analyzes agent runs, extracts lessons from failures, adjusts configuration (max turns, model selection), and evolves prompts. `forgesmith_gepa.py` uses DSPy-style prompt evolution to iteratively improve role-specific prompts. `forgesmith_simba.py` generates behavioral rules from high-variance episodes. These learned lessons and rules get injected back into future agent prompts, creating a feedback loop.

**The dashboard and analysis tools** (`forge_dashboard.py`, `analyze_performance.py`, `nightly_review.py`) query the database to show task completion rates, blocked work, agent performance metrics, and portfolio health — giving you visibility into what the system is doing and how well it's performing.

---

## System Overview

```mermaid
graph TD
    User[User / CLI] -->|describe tasks| Orchestrator[forge_orchestrator.py]
    Orchestrator -->|dispatch| Claude[Claude API]
    Orchestrator -->|dispatch| Ollama[Ollama Local Models]
    Orchestrator -->|read/write| DB[(SQLite Database<br/>30+ tables)]
    Orchestrator -->|dev-test loop| EarlyTerm[Early Termination<br/>& Loop Detection]
    DB -->|episode data| Forgesmith[forgesmith.py<br/>Self-Improvement]
    Forgesmith -->|evolved prompts| GEPA[forgesmith_gepa.py<br/>Prompt Evolution]
    Forgesmith -->|behavioral rules| SIMBA[forgesmith_simba.py<br/>Rule Generation]
    Forgesmith -->|lessons & config| DB
    DB -->|metrics| Dashboard[forge_dashboard.py<br/>analyze_performance.py]
    DB -->|inject lessons/episodes| Orchestrator
```

---

## Data Flow

### Typical Task Execution

```mermaid
sequenceDiagram
    participant User
    participant Orch as forge_orchestrator
    participant DB as SQLite
    participant Agent as Claude / Ollama
    participant Loop as Loop Detector

    User->>Orch: Submit task (CLI args or dispatch)
    Orch->>DB: fetch_task() / scan_pending_work()
    DB-->>Orch: Task details + project context
    Orch->>DB: get_relevant_lessons() + get_relevant_episodes()
    DB-->>Orch: Lessons & episodes for injection
    Orch->>Orch: build_task_prompt() with context
    
    loop Dev-Test Cycle
        Orch->>Agent: Run developer agent
        Agent-->>Orch: Code changes + result
        Orch->>Loop: Check for stuck/monologue/loops
        Loop-->>Orch: ok / warn / terminate
        Orch->>Agent: Run tester agent
        Agent-->>Orch: Test results
        Orch->>Loop: Check test loop status
        Loop-->>Orch: ok / warn / terminate
    end
    
    Orch->>DB: record_agent_episode()
    Orch->>DB: update_task_status()
    Orch->>DB: update_episode_q_values()
    Orch->>DB: bulk_log_agent_actions()
    Orch-->>User: Print summary
```

### Self-Improvement Cycle (Forgesmith)

```mermaid
sequenceDiagram
    participant Cron as Cron / Manual
    participant FS as forgesmith.py
    participant DB as SQLite
    participant Claude as Claude API
    participant GEPA as forgesmith_gepa.py
    participant SIMBA as forgesmith_simba.py

    Cron->>FS: run_full()
    FS->>DB: collect_agent_runs()
    DB-->>FS: Recent episodes & metrics
    FS->>FS: analyze_max_turns_hit() / analyze_repeat_errors()
    FS->>DB: extract_lessons() & store
    FS->>FS: evaluate_previous_changes()
    FS->>FS: score_completed_runs() via rubric
    FS->>GEPA: run_gepa() for prompt evolution
    GEPA->>Claude: Propose evolved prompts
    Claude-->>GEPA: New prompt candidates
    GEPA->>DB: store_evolved_prompt()
    FS->>SIMBA: run_simba() for rule generation
    SIMBA->>Claude: Analyze high-variance episodes
    Claude-->>SIMBA: Behavioral rules
    SIMBA->>DB: store_rules()
    FS->>DB: log_run() with summary
```

---

## Database

```mermaid
erDiagram
    projects ||--o{ tasks : contains
    tasks ||--o{ agent_episodes : generates
    tasks ||--o{ agent_actions : logs
    tasks ||--o{ agent_messages : exchanges
    tasks ||--o{ checkpoints : saves
    agent_episodes ||--o{ lessons : produces
    agent_episodes }o--|| rubric_scores : evaluated_by
    forgesmith_runs ||--o{ forgesmith_changes : applies
    simba_rules }o--|| agent_episodes : derived_from
    prompt_versions }o--|| forgesmith_runs : evolved_in

    projects {
        int id PK
        string codename
        string project_dir
        string status
    }
    tasks {
        int id PK
        int project_id FK
        string title
        string description
        string status
        string priority
        string complexity
        string task_type
    }
    agent_episodes {
        int id PK
        int task_id FK
        string role
        string outcome
        float q_value
        string reflection
        string error_patterns
        int times_injected
    }
    agent_actions {
        int id PK
        int task_id FK
        string run_id
        int cycle
        string role
        string error_class
        int success
    }
    agent_messages {
        int id PK
        int task_id FK
        int cycle
        string from_role
        string to_role
        string msg_type
        string content
        int read_by_cycle
    }
    lessons {
        int id PK
        string role
        string error_type
        string content
        int active
        int times_injected
    }
    rubric_scores {
        int id PK
        int task_id FK
        string role
        float normalized_score
        string dimensions
    }
    forgesmith_runs {
        int id PK
        string run_id
        string mode
        int runs_analyzed
        string summary
    }
    forgesmith_changes {
        int id PK
        string run_id FK
        string change_type
        string rationale
    }
    simba_rules {
        int id PK
        string role
        string rule_text
        string error_type
        float effectiveness
    }
    prompt_versions {
        int id PK
        string role
        int version
        string content
        string run_id FK
    }
```

---

## Project Structure

```
Equipa/
├── forge_orchestrator.py        # Core orchestrator — task dispatch, dev-test loop, episode recording
├── forgesmith.py                # Self-improvement engine — analyzes runs, extracts lessons, tunes config
├── forgesmith_gepa.py           # GEPA prompt evolution — DSPy-style iterative prompt optimization
├── forgesmith_simba.py          # SIMBA rule generation — behavioral rules from high-variance episodes
├── forgesmith_impact.py         # Impact assessment — blast radius analysis for config changes
├── forgesmith_backfill.py       # Backfill tool — retroactively parses logs into structured episodes
├── forge_dashboard.py           # Dashboard — task summaries, checkpoint analysis, project health
├── forge_arena.py               # Arena — automated multi-phase stress testing and LoRA data export
├── ollama_agent.py              # Local model agent — sandboxed tool execution via Ollama
├── lesson_sanitizer.py          # Security — strips injection attacks from lesson content
├── rubric_quality_scorer.py     # Quality scoring — multi-dimensional rubric for agent outputs
├── nightly_review.py            # Nightly report — portfolio stats, blockers, stale projects
├── analyze_performance.py       # Performance analytics — completion rates, throughput, trends
├── autoresearch_prompts.py      # Prompt optimization — OPRO-style prompt research with LLMs
├── autoresearch_loop.py         # Optimization loop — deploy/test/measure/rollback prompt variants
├── db_migrate.py                # Schema migrations — versioned DB upgrades with backup
├── benchmark_migrations.py      # Migration benchmarks — validates migration correctness & speed
├── equipa_setup.py              # Setup wizard — interactive installer for new deployments
├── prepare_training_data.py     # Training data prep — converts episodes to fine-tuning format
├── train_qlora_peft.py          # QLoRA training — fine-tune with PEFT/LoRA
├── train_qlora.py               # QLoRA training — alternative training pipeline
├── ingest_training_results.py   # Training ingestion — imports training metrics back to DB
├── skills/                      # Skill modules
│   └── security/
│       └── static-analysis/
│           └── sarif_helpers.py # SARIF parsing — load, filter, deduplicate security findings
├── tests/
│   ├── test_early_termination.py        # Tests for stuck detection, loop detection, budget tracking
│   ├── test_early_termination_monologue.py  # Tests for monologue detection
│   ├── test_loop_detection.py           # Tests for LoopDetector class
│   ├── test_task_type_routing.py        # Tests for task type dispatch config
│   ├── test_rubric_scoring.py           # Tests for rubric scoring system
│   ├── test_rubric_quality_scorer.py    # Tests for quality scorer dimensions
│   ├── test_lessons_injection.py        # Tests for lesson retrieval and injection
│   ├── test_lesson_sanitizer.py         # Tests for sanitization and security
│   ├── test_episode_injection.py        # Tests for episode retrieval and Q-value updates
│   ├── test_forgesmith_simba.py         # Tests for SIMBA rule generation pipeline
│   ├── test_agent_messages.py           # Tests for inter-agent messaging
│   └── test_agent_actions.py            # Tests for action logging and error classification
└── CLAUDE.md                    # Project context file for Claude
```

---

## Key Design Decisions

### Pure Python stdlib, zero pip dependencies
The core system uses only Python's standard library and SQLite. This eliminates dependency hell, makes deployment trivial (copy files + run setup), and ensures the system works anywhere Python runs. The only external dependencies are the AI providers themselves (Claude API, optionally Ollama).

### SQLite as the single source of truth
Everything lives in one SQLite database with 30+ tables: tasks, episodes, lessons, rules, config changes, rubric scores, prompt versions. This means no Redis, no Postgres, no message queues — just a single file that's easy to back up, inspect with `sqlite3`, and migrate with `db_migrate.py`. The tradeoff is concurrency, but since agent dispatch is controlled by the orchestrator, write contention is managed.

### Closed-loop self-improvement
The system learns from its own failures through three mechanisms: **lessons** (text patterns extracted from failed runs), **episodes with Q-values** (reinforcement-learning-style scoring of past approaches), and **SIMBA rules** (behavioral guidelines generated by Claude from high-variance outcomes). These all feed back into future prompts, creating a system that genuinely improves over time.

### Aggressive early termination
Rather than letting agents burn tokens spinning in circles, the `LoopDetector` tracks fingerprints of agent actions, detects consecutive repetition (same tool + same result), alternating patterns (A-B-A-B oscillation), and monologue behavior (text-only responses without tool use). Warnings come first, then hard termination. Cost breakers scale with task complexity.

### Lesson sanitization as a security boundary
Since lessons generated from past runs get injected into future prompts, `lesson_sanitizer.py` strips XML injection tags, role override phrases, base64 payloads, ANSI escapes, and dangerous code blocks. This prevents a compromised or adversarial output from poisoning the learning pipeline.

### Dev-test loop as the core execution pattern
Most code tasks run through `run_dev_test_loop()`: a developer agent writes code, a tester agent validates it, and they iterate with compacted context from prior cycles. This mirrors how human developers work — write, test, fix — and the cycle count adapts dynamically based on progress signals.

### Multi-tier model routing
The orchestrator routes tasks to different models based on role, complexity, and configuration. Simple tasks might use cheaper/faster models, complex tasks get the most capable model, and the dispatch config can override per-task-type. Ollama provides a local fallback for cost-sensitive or offline scenarios.

### Prompt evolution with safety rails
`forgesmith_gepa.py` evolves prompts using Claude itself, but with guardrails: diff ratio limits, protected section checks, validation of evolved outputs, A/B testing support, and versioned rollback. Changes are logged to the database so every prompt mutation is traceable and reversible.
---

## Related Documentation

- [Readme](README.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
- [Contributing](CONTRIBUTING.md)
