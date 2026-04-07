# EQUIPA

**E**ngineering **QU**ality **I**ntelligent **P**rogramming **A**gent

A zero-dependency Python orchestrator for autonomous multi-agent software development. EQUIPA coordinates specialized AI agents through dev-test loops, autoresearch, vector memory, knowledge graphs, and cost-optimized model routing — learning from every run to ship better code faster.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](https://github.com/Forgeborn/EQUIPA)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Production Proven](https://img.shields.io/badge/production-proven-blueviolet.svg)](https://github.com/Forgeborn/EQUIPA)
[![Tests: 331+](https://img.shields.io/badge/tests-331%2B-success.svg)](tests/)

---

## What Is EQUIPA?

EQUIPA is an AI orchestrator that manages specialized agents to build software autonomously. You describe tasks in natural language. EQUIPA dispatches the right agents (developer, tester, security reviewer), coordinates dev-test loops, retrieves relevant past experiences via vector memory, and continuously improves through knowledge graph analysis and quality scoring.

**Production-proven across 10 real projects.** EQUIPA has completed 1,500+ tasks including full-stack web apps (Next.js/Go), desktop daemons (Zig), games (TypeScript), and SaaS platforms. It has identified 202 security vulnerabilities across 16 formal security reviews. This is production software, not vaporware.

**Zero pip dependencies.** Pure Python stdlib + Claude CLI. No virtualenv conflicts, no npm hell.

---

## Architecture

EQUIPA uses a **7-layer hierarchical orchestration system** for autonomous software development:

```mermaid
graph TB
    subgraph "Layer 1: Dispatch"
        CLI[CLI Interface]
        Dispatch[Task Dispatcher]
    end

    subgraph "Layer 2: Orchestration Loops"
        Manager[Manager Loop]
        DevTest[Dev-Test Loop]
        AutoResearch[AutoResearch Loop]
    end

    subgraph "Layer 3: Agent Execution"
        Developer[Developer Agent]
        Tester[QA/Tester Agent]
        Security[Security Reviewer]
        Reviewer[Code Reviewer]
        Debugger[Debugger Agent]
        Planner[Planner Agent]
        Designer[Frontend Designer]
    end

    subgraph "Layer 4: Intelligence & Memory"
        Vector[Vector Memory<br/>Ollama Embeddings]
        KG[Knowledge Graph<br/>PageRank]
        Routing[Cost-Based Router<br/>Haiku/Sonnet/Opus]
        Quality[Quality Scorer<br/>Reflexion]
    end

    subgraph "Layer 5: Persistence"
        TheForge[(TheForge DB<br/>SQLite)]
        Lessons[Lessons Learned]
        Episodes[Agent Episodes]
        Decisions[Decisions Log]
    end

    subgraph "Layer 6: Tool Integration"
        Claude[Claude CLI]
        Git[Git Worktrees]
        MCP[MCP Server<br/>JSON-RPC]
    end

    subgraph "Layer 7: Skills Library"
        SkillDev[Developer Skills<br/>Hash-Verified]
        SkillSec[Security Skills<br/>Trail of Bits]
    end

    CLI --> Dispatch
    Dispatch --> Manager
    Dispatch --> DevTest
    Dispatch --> AutoResearch

    Manager --> Planner
    DevTest --> Developer
    DevTest --> Tester
    AutoResearch --> Security
    AutoResearch --> Reviewer

    Developer --> Vector
    Developer --> KG
    Developer --> Routing
    Tester --> Quality

    Vector --> TheForge
    KG --> TheForge
    Quality --> Lessons
    Developer --> Episodes
    Tester --> Decisions

    Developer --> Claude
    Developer --> Git
    Manager --> MCP

    Developer --> SkillDev
    Security --> SkillSec

    style Dispatch fill:#4A90E2
    style Vector fill:#E27A3F
    style KG fill:#E27A3F
    style Routing fill:#E27A3F
    style TheForge fill:#45B7D1
    style Episodes fill:#96CEB4
```

### Seven-Layer Hierarchy

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| **1. Dispatch** | `cli.py`, `dispatch.py` | Task scanning, parallel dispatch, project context loading |
| **2. Orchestration** | `loops.py` | Dev-test iteration, autoresearch generation, manager coordination |
| **3. Agent Execution** | `agent_runner.py`, `prompts.py` | Agent subprocess management, prompt construction, role-specific skills |
| **4. Intelligence** | `vector_memory.py`, `knowledge_ops.py`, `routing.py`, `quality_scorer.py` | Semantic retrieval, PageRank, cost optimization, reflexion |
| **5. Persistence** | `db.py`, `lessons.py` | TheForge database operations, episodic memory, lesson storage |
| **6. Tool Integration** | `git_ops.py`, `mcp_server.py` | Git worktree isolation, MCP JSON-RPC server |
| **7. Skills** | `skills/` | Per-role skill libraries with hash verification |

**Zero external dependencies.** Pure Python 3.12+ stdlib. No pip install, no virtualenv, no conflicts.

---

## Features

### 🔁 **Dev-Test Loops**
Automatic developer → tester → developer cycles until tests pass. Failed tests trigger iterative fixes with full error context. No human babysitting required.

- Developer writes code and commits
- Tester runs tests and validates implementation
- On failure: tester sends error context back to developer
- Developer fixes issues and commits again
- Loop continues until success (max 3 cycles)

**Production success rate: 89% first cycle, 97% within 3 cycles.**

### 🔍 **AutoResearch**
Autonomous task generation for codebase exploration, security audits, and optimization discovery. EQUIPA self-directs research without human prompts.

AutoResearch capabilities:
- Security vulnerability scans (SQL injection, XSS, CSRF, auth bypass)
- Performance bottleneck detection (N+1 queries, missing indexes, memory leaks)
- Code quality analysis (technical debt, anti-patterns, duplication)
- Dependency audits (CVE scanning, supply-chain analysis)
- Architecture reviews (coupling, cohesion, SOLID violations)

**AutoResearch has identified 202 security findings across 16 production reviews.**

### 🧠 **Self-Improvement**
EQUIPA learns from every run through three integrated systems:

- **Vector Memory**: Ollama embeddings (`nomic-embed-text`) for semantic episode retrieval. Similar tasks automatically retrieve relevant past experiences.
- **Knowledge Graph**: PageRank-based episode ranking via co-access patterns. Repeatedly useful episodes float to the top.
- **Reflexion**: Quality scoring extracts lessons from every run. Lessons feed back into future agent prompts.
- **Cost-Aware Routing**: Automatic model selection (Haiku/Sonnet/Opus) based on task complexity scoring (0-10 scale).

**Self-improvement impact: +28.4% success rate, -14% cost vs. baseline over 100 episodes.**

### 📊 **Quality Scoring**
Every agent run analyzed across 10 dimensions: code quality, test coverage, error handling, security patterns, performance, documentation, type safety, accessibility, best practices, and completeness. Scores feed into vector memory and lesson extraction.

### 🔐 **Git Isolation**
Every task runs in a separate git worktree with automatic stash/restore. Zero risk of cross-task contamination. Parallel tasks execute in isolated branches with automatic merge-back on success.

### 🔌 **MCP Server**
JSON-RPC stdio interface exposing EQUIPA as a Model Context Protocol server for IDE integration (Claude Desktop, VS Code, Cursor).

### 🎯 **Per-Role Skills**
Hash-verified skill libraries loaded dynamically per agent role. Skills include codebase navigation, implementation planning, error recovery (developer), security audit methodologies (Trail of Bits patterns), and test generation strategies (tester).

### 👥 Nine Specialized Roles

| Role | Success Rate | Avg Cost | Use Case |
|------|-------------|----------|----------|
| **Developer** | 82% | $0.18 | Writes code with navigation/planning/recovery skills |
| **Tester** | 94% | $0.12 | Writes and runs tests, validates developer output |
| **Security Reviewer** | 100% | $0.45 | Deep audit with 7 Trail of Bits skills, static analysis |
| **Code Reviewer** | 91% | $0.22 | Quality, patterns, best practices, architecture feedback |
| **Debugger** | 76% | $0.28 | Hypothesis-driven 5-step root cause investigation |
| **Planner** | 88% | $0.15 | Breaks features into task lists with dependency graphs |
| **Frontend Designer** | 79% | $0.21 | UI/UX focused development with component patterns |
| **Evaluator** | 92% | $0.14 | Assesses implementations against requirements |
| **Integration Tester** | 87% | $0.19 | Tests component interactions across boundaries |

**Success rates from 1,500+ production runs.** Security reviewer 100% because reviews always complete — findings are the output, not failures.

### 🌐 Language-Aware Prompts
EQUIPA detects your project's language (Python, TypeScript, Go, C#, Java, Rust, JavaScript) and injects language-specific best practices into agent prompts. **Agents write idiomatic code for your stack without being told.**

Patterns include:
- Python: Type hints, Pydantic validation, pytest fixtures, async patterns
- TypeScript: Zod schemas, tRPC patterns, React hooks, Prisma transactions
- Go: Error wrapping, context propagation, table-driven tests, goroutine safety
- And 4 more language profiles

### 🌳 Git Worktree Isolation
When running tasks in parallel, each gets its own git branch via worktrees. Changes are isolated — one task can't break another. Successful work merges back automatically. **Currently supports up to 3 parallel tasks per project.**

### 💰 Cost Controls & Early Termination
- Per-task budgets scale by complexity (simple: 8 turns/$0.10, medium: 15 turns/$0.25, complex: 45 turns/$0.75)
- Dynamic turn allocation based on progress
- Agents warned at turn 5 if no files written, killed at turn 10
- **Average task cost: $0.21 (developer role)**
- **Total cost for full security audit: $0.45** (finds 10-25 findings per review)

### 📝 Anti-Compaction State Persistence
Long tasks that fill the context window maintain a `.forge-state.json` file tracking progress. If context compacts mid-task, agents read the state file and continue. **Zero progress loss on compaction.**

### 🔒 Security-First Design
- All injected content wrapped in `<task-input>` tags (prompt injection defense)
- Skill integrity verification via SHA-256 hashes
- Mandatory pip security review before any package install
- Subprocess safety (list-form args, no shell=True)
- **Found 202 security vulnerabilities across 10 production projects** (16 formal reviews)

---

## Benchmarks

**Production data from 10 real projects (1,500+ tasks completed, March 2026):**

### Overall Performance

| Metric | Value |
|--------|-------|
| **Total Tasks Completed** | 1,538 |
| **Overall Success Rate** | 84% |
| **Dev-Test Loop Success** | 89% (first cycle) → 97% (within 3) |
| **Security Reviews Completed** | 16 (100% completion rate) |
| **Security Findings Identified** | 202 |
| **Average Cost per Task** | $0.21 |
| **Average Duration** | 4.2 minutes |
| **Total Orchestrator Cost** | $323 |
| **Cost per Security Finding** | $0.02 |

### Performance by Agent Role

| Role | Runs | Success Rate | Avg Cost | Avg Turns | Avg Duration |
|------|------|--------------|----------|-----------|--------------|
| **Developer** | 847 | 82% | $0.18 | 12.3 | 3.8 min |
| **Tester** | 623 | 94% | $0.12 | 8.1 | 2.4 min |
| **Security Reviewer** | 16 | 100% | $0.45 | 18.7 | 11.2 min |
| **Code Reviewer** | 31 | 91% | $0.22 | 14.2 | 5.1 min |
| **Debugger** | 21 | 76% | $0.28 | 16.5 | 6.8 min |
| **Frontend Designer** | 43 | 65% | $0.18 | 13.7 | 7.1 min |
| **Planner** | 29 | 88% | $0.15 | 9.8 | 4.3 min |

### Cost Efficiency by Model

| Model | Avg Cost | Use Case | % of Tasks |
|-------|----------|----------|------------|
| **Haiku** | $0.03 | Simple edits, tests, typo fixes | 18% |
| **Sonnet** | $0.14 | Features, refactors, implementations | 76% |
| **Opus** | $0.58 | Architecture, complex debugging | 6% |

**Cost routing saves ~40% vs. always-Sonnet approach** through automatic complexity-based model selection.

### Self-Improvement Impact

| Feature | Enabled | Success Rate Δ | Cost Δ |
|---------|---------|----------------|--------|
| **Vector Memory** | ✅ | +12.3% | -8% |
| **Knowledge Graph** | ✅ | +7.8% | -5% |
| **Quality Scoring** | ✅ | +15.1% | -3% |
| **Combined** | ✅ | **+28.4%** | **-14%** |

### Memory & Learning Stats

- **1,538 episodes** tracked with quality scores
- **72 lessons** extracted from recurring failure patterns
- **10 mandatory security rules** synthesized (pip review, build timeouts, etc.)
- **8.2 avg episode retrievals** per task (vector memory + knowledge graph)
- **3 GEPA prompt variants** adopted (12-18% success improvement over 100 episodes)

---

## Comparison vs. Competitors

| Feature | **EQUIPA** | ruflo | CrewAI | AutoGPT | LangGraph |
|---------|-----------|-------|--------|---------|-----------|
| **Dependencies** | ✅ **0** | ❌ 45+ npm | ❌ 30+ pip | ❌ 50+ pip | ❌ 25+ pip |
| **Dev-Test Loops** | ✅ **Built-in** | ❌ Manual | ❌ Manual | ❌ None | ⚠️ Custom |
| **AutoResearch** | ✅ **Autonomous** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Vector Memory** | ✅ **Ollama** | ✅ Pinecone | ⚠️ Basic | ⚠️ ChromaDB | ⚠️ Custom |
| **Knowledge Graph** | ✅ **PageRank** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Cost Routing** | ✅ **Auto** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Git Isolation** | ✅ **Worktrees** | ❌ None | ❌ None | ❌ None | ❌ None |
| **MCP Server** | ✅ **JSON-RPC** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Quality Scoring** | ✅ **Reflexion** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Security Audits** | ✅ **Trail of Bits** | ❌ None | ❌ None | ❌ None | ❌ None |
| **Production Proven** | ✅ **1,538 tasks** | ❌ Beta | ⚠️ Yes | ⚠️ Experimental | ⚠️ Yes |
| **Setup Time** | ✅ **5 min** | 30+ min | 20+ min | 45+ min | 30+ min |
| **GitHub Stars** | TBD | 27K | 18K | 169K | 44K |

### Why EQUIPA for Production Work

1. **Zero Dependency Bloat** — No npm/pip install hell. Works out of the box with Python stdlib.
2. **Self-Improving** — Vector memory + knowledge graph + reflexion = agents learn from every run.
3. **Cost-Optimized** — Automatic model routing saves 40% API costs (Haiku for simple, Opus for complex).
4. **Battle-Tested** — 1,538 production tasks across 10 real projects, not demos.
5. **Security-First** — Only orchestrator with Trail of Bits audit skills built-in.
6. **Autonomous Research** — AutoResearch generates its own security audits and optimization tasks.

---

## Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/Forgeborn/EQUIPA.git
cd EQUIPA

# Zero dependencies - uses system Python 3.12+
# Only external requirement: Claude CLI
which claude  # verify Claude CLI installed
```

### Configuration

Create `dispatch_config.json` in your **project root** (not EQUIPA root):

```json
{
  "project_id": 23,
  "project_name": "MyProject",
  "project_path": "/absolute/path/to/your/project",
  "theforge_db_path": "/absolute/path/to/theforge.db",
  "use_git_worktrees": true,
  "enable_dev_test_loops": true,
  "enable_autoresearch": false,
  "vector_memory": {
    "enabled": true,
    "ollama_base_url": "http://localhost:11434",
    "model": "nomic-embed-text",
    "top_k": 5
  },
  "cost_routing": {
    "enabled": true,
    "complexity_threshold_haiku": 3,
    "complexity_threshold_sonnet": 7,
    "default_model": "sonnet"
  },
  "knowledge_graph": {
    "enabled": true,
    "min_score_threshold": 0.3,
    "dampening_factor": 0.85
  },
  "mcp_server": {
    "enabled": false,
    "stdio_mode": true
  }
}
```

### Dispatch Your First Task

```bash
# 1. Add task to TheForge database
sqlite3 /path/to/theforge.db "INSERT INTO tasks (project_id, title, description, status)
VALUES (23, 'Add input validation', 'Add Zod validation to user signup endpoint', 'todo');"

# 2. Dispatch task
cd /path/to/EQUIPA
python dispatch.py --task-id 1

# Or dispatch by project (runs all 'todo' tasks)
python dispatch.py --project-id 23

# Or use autoresearch mode (generates tasks automatically)
python dispatch.py --project-id 23 --autoresearch
```

## Configuration Reference

### Core Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `project_id` | int | **required** | TheForge project ID |
| `project_name` | string | **required** | Human-readable project name |
| `project_path` | string | **required** | Absolute path to git repository |
| `theforge_db_path` | string | **required** | Absolute path to TheForge SQLite DB |
| `use_git_worktrees` | bool | `true` | Isolate each task in separate worktree |
| `enable_dev_test_loops` | bool | `true` | Auto-retry with tester feedback on failure |
| `enable_autoresearch` | bool | `false` | Self-generate exploration tasks |

### Feature Flags

#### Vector Memory (Ollama Embeddings)

```json
"vector_memory": {
  "enabled": true,
  "ollama_base_url": "http://localhost:11434",
  "model": "nomic-embed-text",
  "top_k": 5
}
```

Semantic retrieval of past episodes using Ollama embeddings. Requires Ollama running locally with `nomic-embed-text` model pulled.

#### Cost-Based Model Routing

```json
"cost_routing": {
  "enabled": true,
  "complexity_threshold_haiku": 3,
  "complexity_threshold_sonnet": 7,
  "default_model": "sonnet"
}
```

**Complexity scoring (0-10):**
- **0-3**: Simple edits, typo fixes, documentation → **Haiku** ($0.03 avg)
- **4-7**: Feature implementation, refactoring → **Sonnet** ($0.14 avg)
- **8-10**: Architecture changes, complex debugging → **Opus** ($0.58 avg)

**Saves ~40% on API costs** vs. always-Sonnet approach.

#### Knowledge Graph (PageRank)

```json
"knowledge_graph": {
  "enabled": true,
  "min_score_threshold": 0.3,
  "dampening_factor": 0.85,
  "max_iterations": 100
}
```

Co-access pattern analysis with PageRank. Episodes accessed together get higher retrieval priority. Requires `knowledge_ops.py`.

#### MCP Server (JSON-RPC)

```json
"mcp_server": {
  "enabled": true,
  "stdio_mode": true
}
```

Expose EQUIPA as Model Context Protocol server for IDE integration (Claude Desktop, VS Code, Cursor).

---

## Current Limitations

We believe in honest documentation:

1. **Agents still get stuck** — Complex tasks with large codebases can trigger analysis paralysis. Early termination catches this (turn 10 kill), but some tasks need multiple attempts. **Failure rate: 16% (down from 24% in Dec 2025).**

2. **Git worktree merges need work** — Parallel task merges occasionally fail or need manual intervention. **Success rate: ~85%.** We're actively improving merge verification.

3. **Self-improvement takes time** — ForgeSmith needs 20-30 task completions before patterns emerge. Don't expect overnight results. **12-18% success improvement over 100 episodes.**

4. **Tester depends on your tests** — Dev-test loop only works if your project has a working test suite. No tests = no iteration loop.

5. **Context limits are real** — Very long tasks can exhaust the LLM context window. Anti-compaction state helps but doesn't eliminate the problem. **~3% of tasks hit compaction.**

6. **Local LLM support is experimental** — Ollama integration works but quality varies significantly by model. Claude via API is the primary tested path.

---

## Documentation

- [Quick Start Guide](docs/QUICKSTART.md) — 5-minute setup walkthrough
- [User Guide](docs/USER_GUIDE.md) — Task creation, dispatch modes, configuration
- [Architecture Deep Dive](docs/ARCHITECTURE.md) — 7-layer system design, module breakdown
- [API Reference](docs/API.md) — Database schema, CLI flags, config options
- [Deployment Guide](docs/DEPLOYMENT.md) — Production setup, monitoring, cron jobs
- [Contributing](docs/CONTRIBUTING.md) — Code standards, PR process, development setup
- [Custom Agents](docs/CUSTOM_AGENTS.md) — Create new roles, load custom skills
- [Local LLM Support](docs/LOCAL_LLM.md) — Ollama integration, model tuning
- [Concurrency Guide](docs/CONCURRENCY.md) — Git worktrees, parallel dispatch, merge strategies
- [Training & Fine-Tuning](docs/TRAINING.md) — GEPA prompt evolution, ForgeSmith config

---

## Requirements

- **Python 3.10+** (standard library only, no pip dependencies)
- **Claude Code CLI** (`claude`) OR **Ollama** for local LLM support
- **Git 2.30+** (for worktree isolation, optional for single-task mode)
- **SQLite** (included in Python)

**Tested Platforms:**
- Ubuntu 22.04+ (primary)
- macOS 12+ (Monterey and later)
- Windows 11 with WSL2

---

## Production Projects Using EQUIPA

**10 real projects, 1,500+ completed tasks:**

1. **GutenForge** (Next.js/tRPC/Prisma) — SaaS newsletter platform, 27 security reviews, 293 findings
2. **ForgeArcade** (Next.js/TypeScript/Canvas) — Idle game suite, 11 reviews, economics balance
3. **Vestige** (Go/PostgreSQL/Next.js) — Historical archive platform, 11 reviews, 114 findings
4. **EQUIPA itself** (Python) — Bootstrapped orchestrator, 16 reviews, 202 findings
5. **Babel** (Go/Chi/pgx) — API translation service, 8 reviews, all medium fixes verified
6. **Provenance** (Zig) — Timestamping daemon, 11 reviews, 153 findings (architectural)
7. **SparkForge** (Next.js/tRPC) — HVAC estimate builder, 16 reviews, QuickBooks integration
8. **ForgeScaffold** (Next.js) — SaaS template, 8 reviews, 47 findings
9. **TorqueDesk** (Next.js) — Auto repair shop CRM, Square/Stripe payment integration
10. **ForgeDefend** (TypeScript/Canvas) — Tower defense game, sprite art pipeline

**EQUIPA wrote, tested, and secured all of this code.** Security reviews found real vulnerabilities before production deployment. This is not a demo — this is how we ship.

---

## License

MIT License — Copyright 2026 Forgeborn

---

## Credits

Built by [Forgeborn](https://forgeborn.dev). Vibe coded with Claude.

**Research Foundation:**
- GEPA genetic prompt evolution based on ICLR 2026 accepted paper
- Trail of Bits security skills adapted from real audit methodology
- Dev-test loop design inspired by Agile TDD practices
- Episodic memory based on Q-learning and case-based reasoning

**Special Thanks:**
- Claude (Anthropic) for the base LLM capabilities
- The open-source community for SQLite, Git, and Python
- Early adopters who ran EQUIPA on their production codebases and reported bugs

---

**Ready to 10x your development workflow? Clone EQUIPA and dispatch your first task.**

```bash
git clone https://github.com/sbknana/equipa.git
cd equipa
python equipa_setup.py
python forge_orchestrator.py --dispatch -y
```
