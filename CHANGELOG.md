# Changelog

All notable changes to EQUIPA are documented here.

## [Unreleased]

### Changed

- **Project path translation is configuration** (task #3183, IR80-05).
  - **Change:** EQUIPA no longer carries a built-in mapping from one Windows share to one Linux mount.
  - **New config key:** list the mappings under `path_translations` in `dispatch_config.json`, for example
    `[{"from": "X:\\share", "to": "/srv/share"}]`. It is empty by default.
  - **Where it applies:** `equipa.tasks.resolve_project_dir` and the scaffold bootstrap both use
    `equipa.config.translate_local_path`. The bootstrap keeps its containment check after translation.
  - **Scaffold:** auto-clone has no built-in location either.
    - The source comes from `EQUIPA_FORGESCAFFOLD_DIR` or `forgescaffold_dir`.
    - The allowed roots come from `EQUIPA_SCAFFOLD_ALLOWED_ROOTS`, or else from the `to` prefixes of `path_translations`.
  - **Scripts:** their defaults now sit beside the checkout that holds them.
  - **Docs:** see "Project Paths Recorded on Another Host" in `docs/DEPLOYMENT.md`.
  - **Upgrading:** an install that relied on the built-in mapping must add its own `path_translations` entry and
    `forgescaffold_dir` before it deploys.

- **Non-git project guard reads command lines and the inherited environment** (task #3183, IR80-02, IR80-03).
  - **Change:** while a project that was not a git repository at dispatch is recorded, the git runners refuse a
    call whose config pairs or variables carry a command line (`core.sshCommand`, `core.pager`, `diff.external`,
    `credential.helper=!...`, `core.fsmonitor=<cmd>`, `GIT_EXTERNAL_DIFF`, `GIT_SSH_COMMAND`, `GIT_PAGER` and
    similar), as they already refused aliases. EQUIPA's own hardening pins and values that run nothing are allowed.
  - **Inherited environment:** a runner given no `env` is now guarded on the environment its child really gets
    (`_get_repo_env()`), not on an empty mapping.
  - **Operator note:** `GIT_SSH_COMMAND` in the orchestrator's environment reaches git, so it refuses every git call
    for the whole run of a non-git project. Set `GIT_SSH` (a program path, carried into the `core.sshCommand` pin)
    instead.
- **Windows refusal store** (task #3183, IR80-01): OWNER RIGHTS (`S-1-3-4`) is a trusted writer, so the store
  `os.mkdir(mode=0o700)` creates on CPython 3.13 and 3.12.4+ is no longer refused. Ownership is still checked first.

- **Review-agent output path** (task #2476) — security-reviewer,
  code-reviewer, and other review-style agents now write their output
  artifacts to `.equipa-artifacts/<TYPE>-<TASK_ID>.md` (e.g.
  `.equipa-artifacts/SECURITY-REVIEW-1234.md`) in the target repo
  instead of dumping `SECURITY-REVIEW-1234.md` / `CODE-REVIEW-1234.md`
  at the repo root. The orchestrator pre-creates the
  `.equipa-artifacts/` directory before each review-style dispatch
  (`equipa.loops.ensure_artifacts_dir`). The merge gate
  (`equipa.dispatch._security_review_blocks_merge` and
  `equipa.dispatch._merge_task_branch`) reads the new location first,
  with a legacy-path fallback (`equipa.loops.find_review_artifact`) so
  in-flight artifacts from older agents still parse during the
  transition. The "EQUIPA agents MUST save output files" lesson
  remains in force — only the destination changed.

  Why: the repo-root pollution had been bleeding into every project
  EQUIPA touched (companion cleanup task moved historical strays). The
  prompt templates (`prompts/security-reviewer.md`,
  `prompts/code-reviewer.md`, `standing_orders/security-reviewer.md`)
  and the agent prompt builders (`run_security_review`,
  `run_code_review` in `equipa/loops.py`) have all been updated; a
  regression test (`tests/test_review_artifact_paths.py`, 18 cases)
  asserts the contract end-to-end and guards against future drift.

- **Merge integrity** (tasks #3111, #3116) — the gated merge names the
  reviewed commit, never the branch, and the default branch is pinned
  before dispatch; any other movement raises an ALERT and stops merging.
  Operator-visible effects:
  - The security reviewer now works in an orchestrator-made, read-only
    checkout of the reviewed commit in a temp directory (only
    `.equipa-artifacts/` is writable); its artifact is copied back to the
    task worktree. Index entries with skip-worktree or assume-unchanged
    set make the review unclean, so the task is not merged.
  - Orchestrator git ignores the system git config and system attributes
    (`GIT_CONFIG_NOSYSTEM=1`, `GIT_ATTR_NOSYSTEM=1`) and reads a copy of
    the global config taken when the orchestrator starts
    (`GIT_CONFIG_GLOBAL`). Edits to `~/.gitconfig` made while it runs are
    not seen until the next start; `includeIf` sections of the global
    config are not carried over.
  - Filter, merge and diff driver programs in any config scope (including
    submodule configs) block the merge, except git-lfs's standard
    programs (`merge_integrity.DRIVER_CONFIG_ALLOWLIST`). So does
    `.git/info/attributes` or the global attributes file selecting a
    driver other than `lfs` or git's built-in ones.
  - A task is `done` only after its merge landed (`tasks.merged_sha`,
    schema v12); a single-task run whose pre-dispatch pin failed is not
    merged.

- **Generated-file merge conflicts** (task #3131) — a gated merge that
  conflicts only in declared generated files
  (`equipa.generated_files.GENERATED_FILES`, initially
  `equipa/MODULE_DEPENDENCY_REPORT.md` from `scripts/gen_module_report.py`)
  is completed by regenerating them from the merged tree instead of ending
  `merge_failed`. The generator runs only when the task branch left it
  unchanged, isolated from the orchestrator and with a timeout; the recorded
  merged SHA is the resolution commit, and the merge-integrity check accepts
  it only if it differs from `git merge-tree`'s merge in the regenerated
  files alone. Any other conflict behaves as before. See CONTRIBUTING.md §7.

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

- Initial release of EQUIPA.
- Interactive setup wizard (`equipa_setup.py`).
- Database schema (19 tables, 5 views).
- Agent prompt files for all roles.
- Configuration file generation.
- Claude Code MCP integration.

