# Changelog

All notable changes to ForgeTeam / Itzamna are documented here.

## [2.1.0] - 2026-02-27

### Added

- **GEPA (Generalized Efficient Prompt Adaptation)** — DSPy-based automatic prompt evolution with A/B testing and automatic rollback. Evolves role prompts by reflecting on failure traces. Safety-capped at 20% change per cycle with protected sections. (`forgesmith_gepa.py`)
- **SIMBA (Systematic Identification of Mistakes and Behavioral Adjustments)** — Targeted rule generation from failure patterns. Contrasts successful vs failed episodes using Claude to generate specific improvement rules. Rules are effectiveness-scored and auto-pruned. (`forgesmith_simba.py`)
- **Context engineering** — Token-budget-aware prompt assembly in the orchestrator. Includes relevance-scored episode injection, lesson deduplication (60% overlap threshold), and priority-based trimming to keep prompts under 10K tokens.
- **Rubric evolution** — Rubric weights auto-adjust based on correlation with task success. Max ±10% weight change per criterion per cycle.
- **Effectiveness scoring** — All ForgeSmith changes (turn limits, prompt patches, SIMBA rules) are scored before/after for measurable impact. Changes below the rollback threshold (-0.3) are automatically reverted.
- **GEPA A/B testing** — Evolved prompts are tested in 50/50 split against baselines. Underperformers rolled back after 10+ tasks.
- **Episode Q-value updates (MemRL)** — Q-values updated after task completion: +0.1 on success, -0.05 on failure.
- **Comprehensive test suite** for `forgesmith_simba.py` (39 tests)

### Changed

- Updated README.md with full ForgeSmith prompt optimization documentation
- Updated User Guide with SIMBA, GEPA, context engineering, and rubric sections
- Updated architecture diagram to show ForgeSmith pipeline (SIMBA + GEPA + DSPy)
- ForgeSmith pipeline order expanded: COLLECT → ANALYZE → LESSONS → SIMBA → RUBRICS → APPLY → GEPA → LOG
- Database schema expanded to 28 tables (added ForgeSmith tables: lessons_learned, agent_episodes, forgesmith_runs, forgesmith_changes, rubric_scores, rubric_evolution_history)

### Configuration

New keys in `forgesmith_config.json`:
- `rubric_definitions` — Per-role scoring criteria and weights
- `rubric_evolution` — Weight evolution settings (max_weight_change_pct, min_sample_size, evolution_lookback_days)
- `suppression_cooldown_days` — Days before retrying suppressed changes
- `thresholds.max_turns_hit_rate` — Rate above which turn limits are increased
- `thresholds.turn_underuse_rate` — Rate below which turn limits are decreased

New keys in `dispatch_config.json`:
- `task_type_prompts` — Per-task-type prompt guidance (bug_fix, feature, refactor, test)

## [1.0.0] - 2026-02-07

### Added

- Initial release of Itzamna — portable installer for ForgeTeam
- Interactive setup wizard (`itzamna_setup.py`)
- Database schema (19 tables, 5 views)
- Agent prompt files for all roles
- Configuration file generation
- Claude Code MCP integration
- Documentation (Quick Start, User Guide, Custom Agents, Concurrency)
