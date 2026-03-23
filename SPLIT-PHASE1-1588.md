# Phase 1: Monolith Split — Extract Leaf Modules

**Task:** #1588
**Date:** 2026-03-23
**Status:** Complete

## Summary

Extracted 3 leaf modules (zero dependents) from `forge_orchestrator.py` into a new `equipa/` package using the Strangler Fig pattern. All 331 tests pass with zero changes.

## Modules Extracted

### 1. `equipa/constants.py` (~175 lines)
All constants, defaults, limits, and enums previously defined inline in `forge_orchestrator.py`:
- Core paths: `THEFORGE_DB`, `MCP_CONFIG`, `PROMPTS_DIR`, `SKILLS_BASE_DIR`
- Role configs: `ROLE_SKILLS`, `ROLE_PROMPTS`, `DEFAULT_ROLE_TURNS`, `DEFAULT_ROLE_MODELS`
- Agent defaults: `DEFAULT_MODEL`, `DEFAULT_MAX_TURNS`, `DEFAULT_MAX_RETRIES`, `PROCESS_TIMEOUT`
- Early termination: `EARLY_TERM_*`, `MONOLOGUE_*`
- Budget/cost: `BUDGET_*`, `COST_LIMITS`, `DYNAMIC_BUDGET_*`
- Dev-test loop: `MAX_DEV_TEST_CYCLES`, `NO_PROGRESS_LIMIT`, `MAX_CONTINUATIONS`
- Manager mode: `MAX_MANAGER_ROUNDS`, `MAX_TASKS_PER_PLAN`, `MAX_FOLLOWUP_TASKS`
- Pre-flight/auto-fix: `PREFLIGHT_*`, `AUTOFIX_*`
- Repo setup: `PROJECT_DIRS`, `GITHUB_OWNER`, `GITIGNORE_TEMPLATES`, `PRIORITY_ORDER`
- Skill integrity: `SKILL_MANIFEST_FILE`

### 2. `equipa/checkpoints.py` (~80 lines)
- `save_checkpoint()` — save agent output for resume on retry
- `load_checkpoint()` — load most recent checkpoint for a task+role
- `clear_checkpoints()` — remove checkpoint files for completed tasks

Note: `build_checkpoint_context()` stays in the monolith because it depends on `compact_agent_output()` (a non-leaf function).

### 3. `equipa/git_ops.py` (~280 lines)
- `_is_git_repo()` — check if directory has `.git`
- `detect_project_language()` — scan marker files for language/framework detection
- `_get_repo_env()` — build env dict with git/gh on PATH
- `_git_run()` — run git/gh commands with standard options
- `check_gh_installed()` — verify gh CLI installed and authenticated
- `setup_single_repo()` — init git + create GitHub private repo
- `setup_all_repos()` — batch repo setup for all projects

### 4. `equipa/__init__.py` (~140 lines)
Re-exports all public symbols from the 3 modules for backward compatibility.

## Architecture Decisions

1. **Mutable globals sync:** `load_config()` and `_discover_roles()` in the monolith update both the monolith's module-level names AND `equipa.constants` module attributes via `_equipa_constants` alias. This ensures git_ops.py (which imports `PROJECT_DIRS`, `GITHUB_OWNER` from constants) sees config-file overrides.

2. **Lazy import in `setup_all_repos()`:** Uses `from forge_orchestrator import fetch_project_info` at call time to avoid circular imports. This function is called rarely (only `--setup-repos` mode).

3. **`build_checkpoint_context()` stays in monolith:** It calls `compact_agent_output()` which has many internal dependencies. Moving it would pull too much into the leaf layer.

4. **No function signatures changed.** All extracted functions maintain identical signatures and behavior. The `equipa/__init__.py` re-exports make this invisible to callers.

## Test Results

```
331 passed, 1 warning in 4.76s
```

All 331 existing tests pass without modification. The 1 warning is a pre-existing `DeprecationWarning` for `asyncio.get_event_loop()`.

## Lines Removed from Monolith

~520 lines of inline definitions replaced with ~75 lines of imports. Net reduction: ~445 lines from `forge_orchestrator.py`.

## Next Steps (Phase 1 continued)

- Task #1589: Extract low-coupling modules (provider abstraction, config loading)
- Task #1590: Extract DB layer
- Task #1591: Extract core engine
- Task #1592: Entry points + final shim
