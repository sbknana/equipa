# Changelog

All notable changes to Project Pombal are documented here.

## [3.1.0] - 2026-03-05

### Added

- **Per-role agent skills.** Developer, tester, debugger, and code-reviewer agents now load specialized skills from `skills/` at task start. Skills teach concrete methods: codebase navigation (4-step method), implementation planning (complexity classification), error recovery (3-Strike Rule), systematic debugging (hypothesis-driven 5-step), architecture review (5-point checklist), change-impact analysis (blast radius), framework detection, and test generation.
- **Git worktree isolation.** Parallel tasks now run in isolated git worktrees (`forge-task-{id}` branches), preventing filesystem conflicts between concurrent agents. Merged branches are cleaned up; **unmerged branches are preserved** for manual recovery.
- **Post-task quality scoring** (`rubric_quality_scorer.py`) — 5-dimension quality scorer with pattern matching, role-specific weights, file heuristics, and DB storage. 221 tests.
- **Failure classification** — Structured failure taxonomy (`analysis_paralysis`, `build_failure`, `test_failure`, `import_error`, `timeout`, `wrong_approach`, `environment_error`, `max_turns`) integrated into SIMBA and GEPA. Replaces generic error_type strings.
- **Change-impact analysis** (`forgesmith_impact.py`) — Blast-radius assessment before ForgeSmith applies prompt mutations. Evaluates affected roles, task types, and risk level. HIGH-risk changes blocked from auto-apply.
- **Lesson sanitizer** (`lesson_sanitizer.py`) — Security invariant checks on lesson extraction pipeline. Prevents prompt injection via lesson content.
- **File change tracking.** Streaming monitor tracks Write/Edit/Bash tool calls. Agents that make file changes but crash before emitting a result message are treated as partial success instead of failure.
- **Non-negotiable code quality standard.** All agents receive a 7-point quality standard via `_common.md`: clean code, proper error handling, input validation, meaningful names, self-documenting code, consistent patterns, test what matters.

### Fixed

- **Worktree merge bug** — Previously deleted all task branches unconditionally during cleanup, even when merge failed. Agent work was permanently lost. Now only merged branches are deleted; unmerged branches preserved with warning.
- **`sys.exit(1)` in `get_db_connection()`** — Crashed the orchestrator instead of raising a catchable exception. Changed to `FileNotFoundError`.
- **`THEFORGE_DB` env var ignored** — Path was hardcoded. Now reads `os.environ.get("THEFORGE_DB", ...)`.
- **`NameError: args.project`** — Crash in main() completion message. Fixed to extract from sys.argv.
- **`lstrip("sudo ")` bypass** — Python lstrip strips characters, not prefix. Replaced with proper `startswith()` check.
- **`python -c` / `node -e` in SAFE_COMMAND_PREFIXES** — Removed: arbitrary code execution risk.
- **Ollama success detection** — OR instead of AND logic. Fixed.
- **Per-line readline timeout** — Increased from 120s to 300s. Was killing agents during long compiles.
- **Bash file-creating commands not tracked** — Added detection for `git commit`, `mkdir`, `cp`, `mv`, `touch`, `tee`, `>`.

### Changed

- Database schema version bumped to v4. Migration adds `impact_assessment` column to `forgesmith_changes`.
- `forgesmith_config.json` — Added quality rubric dimensions.
- SIMBA prompt updated to use `failure_class` taxonomy.
- Developer prompt removed anti-quality directive.

## [3.0.0] - 2026-03-04

### Added

- **Local LLM support via Ollama.** Run read-only agents on local models at zero API cost. (`ollama_agent.py`)
- **Provider abstraction.** Per-role provider selection in `dispatch_config.json`.
- **Inter-agent messaging.** Structured messages between agents across dev-test cycles.
- **Per-tool action logging.** Every tool call logged with input hashes, error classification, duration.
- **Forge Arena** (`forge_arena.py`) — Agent evaluation and training data generation.
- **Forge Dashboard** (`forge_dashboard.py`) — Terminal-based performance dashboard.
- **Performance Analyzer** (`analyze_performance.py`) — Historical agent performance analysis.
- **ForgeSmith Backfill** (`forgesmith_backfill.py`) — Backfill scoring data from historical logs.
- **QLoRA Training** (`train_qlora.py`, `train_qlora_peft.py`) — Fine-tune local models.
- **Training Data Preparation** (`prepare_training_data.py`) — Convert arena results to fine-tuning format.

### Changed

- Orchestrator: inter-agent messaging, action logging, context engineering.
- `schema.sql` — Added `agent_messages` and `agent_actions` tables.

### Sanitized

- Removed all personal paths. Config-based resolution only.

## [2.1.0] - 2026-02-27

### Added

- **GEPA** — DSPy-based automatic prompt evolution with A/B testing and rollback.
- **SIMBA** — Targeted rule generation from failure patterns with effectiveness scoring.
- **Context engineering** — Token-budget-aware prompt assembly with episode injection.
- **Rubric evolution** — Auto-adjusting rubric weights based on task success correlation.
- **Effectiveness scoring** — Before/after scoring with auto-rollback below -0.3 threshold.

### Changed

- ForgeSmith pipeline: COLLECT -> ANALYZE -> LESSONS -> SIMBA -> RUBRICS -> APPLY -> GEPA -> LOG.
- Database schema expanded to 28 tables.

## [1.0.0] - 2026-02-07

### Added

- Initial release of Project Pombal.
- Interactive setup wizard (`pombal_setup.py`).
- Database schema (19 tables, 5 views).
- Agent prompt files for all roles.
- Configuration file generation.
- Claude Code MCP integration.
