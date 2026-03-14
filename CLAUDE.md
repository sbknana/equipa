# CLAUDE.md — EQUIPA

## Project Overview

**TheForge project_id:** 23
**Status:** Active - v3.1

EQUIPA is a multi-agent AI orchestration platform. You talk to Claude in plain English. Claude manages task creation, agent dispatch, progress tracking, error recovery, and reporting. Named for the European Portuguese word for "team".

**Core:** Pure Python stdlib. Zero pip dependencies. SQLite-based. 30+ table schema.

---

## File Map

### Core Orchestration
| File | Purpose |
|------|---------|
| `forge_orchestrator.py` | Main orchestrator — task dispatch, dev-test loops, agent management (~5900 lines) |
| `ollama_agent.py` | Local LLM agent runner via Ollama |
| `equipa_setup.py` | Interactive setup wizard |
| `schema.sql` | Canonical database DDL (30 tables, 7 views) |
| `db_migrate.py` | Database migrations (v0 through v4) with backup and version detection |
| `dispatch_config.json` | Agent dispatch settings (models, turns, concurrency, providers) |

### ForgeSmith Self-Improvement
| File | Purpose |
|------|---------|
| `forgesmith.py` | Core analysis engine — lessons, config tuning, effectiveness scoring |
| `forgesmith_gepa.py` | GEPA — DSPy-based prompt evolution with A/B testing |
| `forgesmith_simba.py` | SIMBA — Rule synthesis from failure patterns |
| `forgesmith_impact.py` | Blast-radius assessment before applying prompt mutations |
| `forgesmith_config.json` | Rubric definitions, thresholds, evolution settings |
| `rubric_quality_scorer.py` | Post-task quality scoring (5 dimensions, role-specific weights) |
| `lesson_sanitizer.py` | Security invariant checks on lesson extraction |
| `forgesmith_backfill.py` | Backfill episode data from historical logs |

### Agent Prompts and Skills
| Directory | Purpose |
|-----------|---------|
| `prompts/` | Role prompts: `_common.md` (shared), plus per-role `.md` files |
| `skills/developer/` | Codebase navigation, implementation planning, error recovery |
| `skills/tester/` | Framework detection, test generation |
| `skills/debugger/` | Systematic debugging (hypothesis-driven 5-step) |
| `skills/code-reviewer/` | Architecture review, change-impact analysis |
| `skills/security/` | 7 Trail of Bits security skills |

### Training and Analysis
| File | Purpose |
|------|---------|
| `forge_arena.py` | Adversarial testing arena for training data generation |
| `forge_dashboard.py` | Terminal-based performance dashboard |
| `analyze_performance.py` | Historical performance reporting |
| `prepare_training_data.py` | Convert arena results to fine-tuning format |
| `train_qlora.py` | QLoRA fine-tuning (manual implementation) |
| `train_qlora_peft.py` | QLoRA fine-tuning (PEFT/Hugging Face) |
| `benchmark_migrations.py` | Migration correctness benchmarks |

### 9 Agent Roles
`developer`, `tester`, `planner`, `evaluator`, `security-reviewer`, `frontend-designer`, `integration-tester`, `debugger`, `code-reviewer`

---

## Key Commands

```bash
python equipa_setup.py                                    # Setup wizard
python forge_orchestrator.py --task 42 --dev-test -y      # Single task
python forge_orchestrator.py --tasks 42,43,44 --dev-test -y  # Parallel tasks
python forge_orchestrator.py --dispatch -y                # Auto-dispatch all
python forge_orchestrator.py --goal "Add feature X" --goal-project 1 -y  # Goal mode
python forgesmith.py --auto                               # Run ForgeSmith
python db_migrate.py theforge.db                          # Run migrations
```

---

## Architecture Decisions

- **SQLite only** — Single-file DB, zero dependencies, trivially portable
- **Dev-test loop** — Developer + Tester iterate until tests pass or budget exhausted
- **Git worktree isolation** — Parallel tasks get isolated branches, merged on success
- **Episodic memory with Q-values** — RL-style learning without training infrastructure
- **Three-tier self-improvement** — ForgeSmith (rule-based) + GEPA (prompt evolution) + SIMBA (rule synthesis)
- **Skills system** — Per-role skills loaded at task start, teaching concrete methods
- **Quality standard** — Non-negotiable 7-point code quality standard for all agents
