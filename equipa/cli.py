"""EQUIPA CLI module — entry points, argument parsing, and configuration.

Extracts async_main, main, load_config, provider helpers, and
_handle_add_project from forge_orchestrator.py (Phase 5 split).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import equipa.constants as _equipa_constants
from equipa.constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    MAX_DEV_TEST_CYCLES,
    MAX_MANAGER_ROUNDS,
    MCP_CONFIG,
    PROJECT_DIRS,
    PROMPTS_DIR,
    THEFORGE_DB,
)
from equipa.agent_runner import (
    OVERLOADED_OUTCOME,
    build_cli_command,
    is_overloaded_result,
    run_agent_streaming,
    run_agent_with_retries,
)
from equipa.checkpoints import load_checkpoint
from equipa.db import log_gate_audit, record_agent_run, update_task_status
from equipa.git_ops import (
    GitRepositoryUnreadableError,
    GlobalConfigPinError,
    _is_git_repo,
    pin_global_git_config,
)
from equipa.merge_integrity import (
    DefaultBranchGuard,
    MergeIntegrityError,
    MergeOutcome,
)
from equipa.merge_safety import (
    exit_if_shutdown_requested,
    report_leftover_dispatch_state,
)
from equipa.config import is_security_review_enabled, set_active_dispatch_config
from equipa.dispatch import (
    EXIT_DISPATCH_REFUSED,
    REPOSITORY_APPEARED_OUTCOME,
    AttemptCleanupError,
    IsolatedTaskRun,
    _audit_task_abort,
    _build_dispatch_attempt_reflection,
    _gated_merge_task,
    _repository_appeared_in_non_git_project,
    _require_task_branch,
    _security_review_blocks_merge,
    apply_dispatch_filters,
    cleanup_failed_attempt,
    collect_refusals,
    escape_audit_detail,
    is_feature_enabled,
    load_dispatch_config,
    load_goals_file,
    outcome_after_merge,
    parse_task_ids,
    refuse_dispatch,
    run_auto_dispatch,
    run_parallel_goals,
    run_parallel_tasks,
    run_task_in_isolation,
    scan_pending_work,
    score_project,
    validate_goals,
)
from equipa.security_gate import (
    SecurityGateBypassError,
    record_reviewer_skipped_doc_only,
    reviewer_run_failure,
)
from equipa.isolation import (
    describe_isolation_state,
    isolation_requirement_refusal,
    uid_pool_refusal,
)
from equipa import templates as _templates
from equipa.git_ops import setup_all_repos
import equipa.hooks as _hooks_module
from equipa.lessons import update_injected_episode_q_values_for_task
from equipa.loops import (
    ensure_artifacts_dir,
    run_dev_test_loop,
    run_quality_scoring,
    run_security_review,
)
from equipa.manager import GOAL_REFUSED_OUTCOMES, run_manager_loop
from equipa.mcp_server import run_server
from equipa.monitoring import calculate_dynamic_budget, dispatched_without_git
from equipa.output import (
    log,
    print_dev_test_summary,
    print_dispatch_plan,
    print_manager_summary,
    print_summary,
)
from equipa.parsing import estimate_tokens
from equipa.plugins import load_plugins
from equipa.prompts import build_planner_prompt, build_system_prompt
from equipa.reflexion import maybe_run_reflexion
from equipa.roles import _discover_roles, get_role_model, get_role_turns
from equipa.routing import CircuitOpenError
from equipa.routing import record_model_outcome
from equipa.security import write_skill_manifest
from equipa.single_agent_guard import TasksCreatedDb
from equipa.security_gate import (
    get_changed_files_for_branch,
    is_doc_only_diff,
)
from equipa.tasks import (
    fetch_next_todo,
    fetch_project_context,
    fetch_project_info,
    fetch_task,
    fetch_tasks_by_ids,
    get_task_complexity,
    resolve_project_dir,
    verify_task_updated,
)


# --- Provider Abstraction (Claude / Ollama) ---

def get_provider(role: str, dispatch_config: dict | None = None) -> str:
    """Determine which provider to use for a given role.

    Checks dispatch_config for role-specific overrides like
    'provider_planner': 'ollama', falling back to the global 'provider' key,
    then defaulting to 'claude'.
    """
    if dispatch_config is None:
        return "claude"
    # Check role-specific override first
    role_key = f"provider_{role.replace('-', '_')}"
    provider = dispatch_config.get(role_key)
    if provider:
        return provider
    # Fall back to global provider setting
    return dispatch_config.get("provider", "claude")


def get_ollama_model(role: str, dispatch_config: dict | None = None) -> str:
    """Get the Ollama model name for a given role.

    Checks for role-specific model override like 'ollama_model_planner',
    then falls back to global 'ollama_model'.
    """
    if dispatch_config is None:
        return "qwen3.5:27b"
    role_key = f"ollama_model_{role.replace('-', '_')}"
    model = dispatch_config.get(role_key)
    if model:
        return model
    return dispatch_config.get("ollama_model", "qwen3.5:27b")


def get_ollama_base_url(dispatch_config: dict | None = None) -> str:
    """Get the Ollama base URL from config or environment."""
    if dispatch_config and "ollama_base_url" in dispatch_config:
        return dispatch_config["ollama_base_url"]
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


# --- Portable Configuration ---

def load_config() -> None:
    """Load forge_config.json if present alongside the orchestrator script.

    Overrides THEFORGE_DB, PROJECT_DIRS, GITHUB_OWNER, MCP_CONFIG, and
    PROMPTS_DIR with values from the config file.  Falls back silently to
    the hardcoded defaults above when no config file exists.
    """
    # Look for config alongside forge_orchestrator.py (project root)
    # We use the constants module's THEFORGE_DB to find the base dir,
    # then look for forge_config.json in common locations.
    config_candidates = [
        Path(__file__).parent.parent / "forge_config.json",  # project root
    ]

    config_path = None
    for candidate in config_candidates:
        if candidate.exists():
            config_path = candidate
            break

    if config_path is None:
        return  # backward compatible — use hardcoded values

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: Failed to read {config_path}: {exc}")
        return

    if "theforge_db" in cfg:
        _equipa_constants.THEFORGE_DB = Path(cfg["theforge_db"])
    if "project_dirs" in cfg:
        # Support PROJECT_BASE_DIR env var: if a project path starts with
        # $PROJECT_BASE_DIR/, resolve it against the env var value.
        base_dir = os.environ.get("PROJECT_BASE_DIR", "")
        raw_dirs = cfg["project_dirs"]
        resolved = {}
        for k, v in raw_dirs.items():
            if base_dir and v.startswith("$PROJECT_BASE_DIR/"):
                v = v.replace("$PROJECT_BASE_DIR", base_dir, 1)
            resolved[k.lower()] = v
        # Updated in place, never rebound: equipa.dispatch, equipa.tasks,
        # equipa.git_ops and equipa.output import this dict by name at
        # startup, so a new object here left them all with the empty
        # default and --auto-run / --parallel-goals resolved no project
        # directory (3112 review, task #3119).
        _equipa_constants.PROJECT_DIRS.clear()
        _equipa_constants.PROJECT_DIRS.update(resolved)
    if "github_owner" in cfg:
        _equipa_constants.GITHUB_OWNER = cfg["github_owner"]
    if "mcp_config" in cfg:
        _equipa_constants.MCP_CONFIG = Path(cfg["mcp_config"])
    if "prompts_dir" in cfg:
        _equipa_constants.PROMPTS_DIR = Path(cfg["prompts_dir"])


# --- Add Project ---

def _handle_add_project(name: str, project_dir: str) -> None:
    """Register a new project in the EQUIPA DB and update forge_config.json."""
    project_dir = str(Path(project_dir).resolve())

    # Insert into DB
    db_path = _equipa_constants.THEFORGE_DB
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        codename = name.lower().replace(" ", "")
        conn.execute(
            "INSERT INTO projects (name, codename, status) VALUES (?, ?, 'active')",
            (name, codename),
        )
        conn.commit()
        project_id = conn.execute(
            "SELECT id FROM projects WHERE codename = ?", (codename,)
        ).fetchone()[0]
        print(f"Created project '{name}' (codename: {codename}, id: {project_id})")
    except sqlite3.IntegrityError:
        print(f"ERROR: Project '{name}' already exists in TheForge")
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    # Update forge_config.json if it exists
    config_path = Path(__file__).parent.parent / "forge_config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("project_dirs", {})[codename] = project_dir
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
            print(f"Updated {config_path} with project directory: {project_dir}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: Could not update config: {exc}")
    else:
        print("NOTE: No forge_config.json found. Add project dir to PROJECT_DIRS manually.")

    print(f"\nProject '{name}' registered successfully.")
    print(f"  ID: {project_id}")
    print(f"  Dir: {project_dir}")


# --- TASKS_CREATED validator DB adapter (task #2371) ---
# Moved to equipa.single_agent_guard (task #3112) so goal mode can share it;
# the old private name stays importable from here.
_TasksCreatedDb = TasksCreatedDb


# --- Post-Task Telemetry ---

async def _post_task_telemetry(
    task: dict,
    result: dict,
    outcome: str,
    role: str,
    model: str,
    max_turns: int,
    cycle_number: int | None = None,
    output: list[str] | None = None,
    dispatch_config: dict | None = None,
    merged_sha: str | None = None,
) -> None:
    """Run all post-task telemetry: DB update, recording, scoring, reflexion, MemRL."""
    update_task_status(task["id"], outcome, output=output, merged_sha=merged_sha)
    record_agent_run(task, result, outcome, role=role, model=model,
                     max_turns=max_turns, cycle_number=cycle_number)
    if outcome in ("tests_passed", "no_tests"):
        run_quality_scoring(task, result, outcome, role=role, output=output,
                            dispatch_config=dispatch_config)
    await maybe_run_reflexion(task, result, outcome, role=role, output=output)
    update_injected_episode_q_values_for_task(task["id"], outcome, output=output)

    # Record model outcome for circuit breaker (cost routing)
    success = outcome in ("tests_passed", "no_tests")
    record_model_outcome(model, success)


async def _gated_post_merge(
    *,
    repo: str | os.PathLike,
    branch: str,
    outcome: str,
    task_id: int | None = None,
    security_review_enabled: bool = True,
    block_on_missing: bool = True,
    guard: DefaultBranchGuard | None = None,
) -> str:
    """Unified post-loop gated merge for single-task ``--dev-test`` mode.

    ``guard`` is the run's default-branch guard, snapshotted before the
    dev-test loop (task #3111); the gate records its per-task result on it.

    Task #2451 Phase B: this is a thin adapter that delegates to
    :func:`equipa.dispatch._gated_merge_task` — both single-task and parallel
    modes share one merge path so the gate semantics cannot diverge again.

    Task #2706: the previous ``review_blocks_merge`` / ``review_skipped_doc_
    only`` caller-flag transform is REMOVED. Those were exactly the
    caller-supplied trust signals that re-opened the merge-gate hole (a
    caller mis-setting ``review_skipped_doc_only`` would forward
    ``expect_artifact=False`` and disable the fail-closed invariant). The
    unified gate now computes a single ``GateDecision`` from ground truth
    (the real branch diff + the on-disk artifact) INSIDE
    ``_gated_merge_task``, so this adapter forwards ONLY the merge target,
    the test ``outcome``, and the two GLOBAL operator-policy flags
    (``security_review_enabled`` / ``block_on_missing``). There is no longer
    any per-task flag a caller can set to bypass the gate.
    """
    if task_id is None:
        try:
            task_id = int(branch.rsplit("-", 1)[-1])
        except (ValueError, IndexError) as e:
            raise ValueError(
                f"_gated_post_merge: cannot infer task_id from branch "
                f"{branch!r}; pass task_id= explicitly"
            ) from e

    return await _gated_merge_task(
        repo=repo,
        branch=branch,
        outcome=outcome,
        task_id=task_id,
        security_review_enabled=security_review_enabled,
        block_on_missing=block_on_missing,
        guard=guard,
    )


def _is_git_project(project_dir: str) -> bool:
    """:func:`_is_git_repo`, refusing the dispatch when git cannot read it.

    R3119-02 (task #3126): a repository git cannot read is never run as a
    non-git project (no worktree, no guard, no gate); the CLI exits with
    ``EXIT_DISPATCH_REFUSED`` instead.
    """
    try:
        return _is_git_repo(project_dir)
    except GitRepositoryUnreadableError as exc:
        refuse_dispatch(str(exc))


async def _snapshot_merge_guard(project_dir: str) -> DefaultBranchGuard | None:
    """Pin the default branch before the dev-test loop (dispatch-03).

    ``None`` when the project is not a git repo or its default branch cannot
    be pinned; the gated merge then refuses to mark the task done.
    """
    if not _is_git_project(project_dir):
        return None
    try:
        guard = await DefaultBranchGuard.snapshot(project_dir)
    except MergeIntegrityError as exc:
        print(f"  [Merge-Integrity] ERROR: {exc} — this task will not be merged")
        return None
    print(
        f"  [Merge-Integrity] '{guard.default_branch}' pinned at "
        f"{guard.baseline_sha[:12]} before dispatch"
    )
    return guard


# --- Template subcommand (PLAN-1067 §3.C3) ---

_TEMPLATE_FLAG_DISABLED_MSG = (
    "ERROR: project_templates feature flag is disabled.\n"
    "  Enable it in dispatch_config.json under features.project_templates "
    "before running 'equipa template' commands."
)


def _build_template_arg_parser() -> argparse.ArgumentParser:
    """Argparse parser for the ``equipa template <verb>`` subcommand surface."""
    parser = argparse.ArgumentParser(
        prog="equipa template",
        description="Export, import, or validate EQUIPA project templates.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    export_p = sub.add_parser("export", help="Export a project to a template directory or archive")
    export_p.add_argument("project_id", type=int, help="Source project ID in TheForge")
    export_p.add_argument("--out", type=str, default=None, metavar="PATH",
                          help="Output directory (default: ./equipa-template-<project_id>)")
    export_p.add_argument("--archive", action="store_true",
                          help="Pack the result into a single .tar.gz archive")
    export_p.add_argument("--scrub-costs", action="store_true",
                          help="Null out cost_usd column in exported agent_runs")

    import_p = sub.add_parser("import", help="Import a template archive into the local TheForge DB")
    import_p.add_argument("archive_path", type=str,
                          help="Template directory or .tar.gz archive to import")
    import_p.add_argument("--name", type=str, default=None,
                          help="Override the imported project's name")
    import_p.add_argument("--on-conflict", choices=["rename", "merge", "fail"],
                          default="rename",
                          help="Strategy when target name already exists (default: rename)")
    import_p.add_argument("--re-embed", action="store_true",
                          help="Regenerate embeddings for imported lessons")
    import_p.add_argument("--force", action="store_true",
                          help="Overwrite existing files in the target project working dir")

    validate_p = sub.add_parser("validate", help="Validate a template manifest (CI-friendly)")
    validate_p.add_argument("archive_path", type=str,
                            help="Template directory or .tar.gz archive to validate")

    return parser


def _template_feature_enabled(dispatch_config_arg: str | None) -> bool:
    """Resolve the project_templates feature flag using the same loader the
    main CLI uses. Kept separate so the template surface works without
    invoking the full async_main argparse path."""
    cfg = load_dispatch_config(dispatch_config_arg)
    return is_feature_enabled(cfg, "project_templates")


def _resolve_template_dir(archive_path: str) -> Path:
    """Resolve an archive path to the directory containing manifest.json.

    Accepts either a directory or a .tar.gz archive. Archives are extracted
    into a temp dir; the caller is responsible for cleanup if needed.
    For ``validate``, we extract to a temp dir and let the OS reap it on
    process exit — validate runs short and we never need the directory
    after returning.
    """
    import tempfile

    p = Path(archive_path)
    if not p.exists():
        raise FileNotFoundError(f"archive not found: {archive_path}")
    if p.is_dir():
        return p
    if p.is_file() and p.name.endswith(".tar.gz"):
        tmp = Path(tempfile.mkdtemp(prefix="equipa-tpl-validate-"))
        _templates._safe_extract_tar(p, tmp)
        return _templates._locate_manifest_dir(tmp)
    raise ValueError(
        f"archive_path must be a directory or .tar.gz archive: {archive_path}"
    )


def _handle_template_subcommand(argv: list[str]) -> int:
    """Entry point for ``equipa template ...`` invocations.

    Returns the desired process exit code. Feature-flag-gated: prints a
    guard message and returns 2 when the project_templates flag is off.
    """
    parser = _build_template_arg_parser()
    # Allow --dispatch-config FILE before the verb so feature-flag overrides
    # can be tested in isolation. argparse subparsers don't natively support
    # parent flags interleaved with the verb, so we strip it manually here.
    dispatch_cfg_path: str | None = None
    cleaned: list[str] = []
    skip_next = False
    for i, tok in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if tok == "--dispatch-config":
            if i + 1 >= len(argv):
                parser.error("--dispatch-config requires a value")
            dispatch_cfg_path = argv[i + 1]
            skip_next = True
            continue
        if tok.startswith("--dispatch-config="):
            dispatch_cfg_path = tok.split("=", 1)[1]
            continue
        cleaned.append(tok)
    args = parser.parse_args(cleaned)

    if not _template_feature_enabled(dispatch_cfg_path):
        print(_TEMPLATE_FLAG_DISABLED_MSG, file=sys.stderr)
        return 2

    if args.verb == "export":
        out_dir = (
            Path(args.out)
            if args.out
            else Path.cwd() / f"equipa-template-{args.project_id}"
        )
        try:
            result_path = _templates.export(
                args.project_id,
                out_dir,
                archive=args.archive,
                scrub_costs=args.scrub_costs,
            )
        except (FileExistsError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(str(result_path))
        return 0

    if args.verb == "import":
        try:
            new_project_id = _templates.import_archive(
                Path(args.archive_path),
                target_project_name=args.name,
                on_conflict=args.on_conflict,
                force=args.force,
                re_embed=args.re_embed,
            )
        except (FileExistsError, FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Imported project_id={new_project_id}")
        return 0

    if args.verb == "validate":
        try:
            template_dir = _resolve_template_dir(args.archive_path)
            manifest_path = template_dir / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError(f"manifest.json missing in {template_dir}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _templates.validate_manifest(manifest)
            _templates._verify_file_hashes(template_dir, manifest)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            return 1
        print(f"OK: {args.archive_path}")
        return 0

    parser.error(f"unknown verb: {args.verb}")
    return 2  # pragma: no cover (parser.error exits)


# --- Main Entry Points ---

def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for async_main.

    Extracted so the parser definition is testable in isolation and so
    async_main itself stays focused on dispatch.
    """
    parser = argparse.ArgumentParser(
        description="EQUIPA: Run AI agents on TheForge tasks"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", type=int, help="Task ID to work on")
    group.add_argument("--tasks", type=str, metavar="IDS",
                        help="Comma-separated task IDs or range (e.g. 109,110,111 or 109-114) for parallel execution")
    group.add_argument("--project", type=int, help="Project ID (auto-pick next todo task)")
    group.add_argument("--goal", type=str, help="High-level goal for Manager mode")
    group.add_argument("--parallel-goals", type=str, metavar="FILE",
                        help="Path to goals JSON file for parallel execution")
    group.add_argument("--setup-repos", action="store_true",
                        help="Init git + GitHub private repo for ALL projects")
    group.add_argument("--setup-repos-project", type=int, metavar="ID",
                        help="Init git + GitHub private repo for a single project")
    group.add_argument("--auto-run", action="store_true",
                        help="Auto-scan projects and dispatch work by priority")
    group.add_argument("--add-project", type=str, metavar="NAME",
                        help="Register a new project in EQUIPA DB and config")
    group.add_argument("--regenerate-manifest", action="store_true",
                        help="Regenerate skill_manifest.json with SHA-256 hashes of all prompt/skill files")
    group.add_argument("--mcp-server", action="store_true",
                        help="Run as MCP server (JSON-RPC over stdio)")
    group.add_argument("--config-cmd", choices=["snapshot", "list", "diff", "rollback"],
                        metavar="VERB",
                        help="Config-versioning subcommand: snapshot|list|diff|rollback")
    group.add_argument("--create-initiative", type=str, metavar="NAME",
                        help="Create a new initiative (requires --initiative-project and "
                             "--initiative-goal). Phase 1.")
    group.add_argument("--list-initiatives", action="store_true",
                        help="List active initiatives, optionally filtered by "
                             "--initiative-project codename.")
    group.add_argument("--initiative", type=int, metavar="ID",
                        help="Run an entire initiative end-to-end: walk the "
                             "sub-task DAG, dispatch in waves, halt on failure "
                             "or operator pause marker, track cost. Phase 2. "
                             "Combine with --max-waves N or --dry-run.")

    parser.add_argument("--qiao", choices=["on", "off", "default"], default="default",
                        help="Enable/disable the qiao experimental plugin for this invocation. "
                             "Overrides dispatch_config.qiao_enabled. Default reads the config.")
    parser.add_argument("--project-dir", type=str, metavar="PATH",
                        help="Project directory (used with --add-project)")
    parser.add_argument("--goal-project", type=int, help="Project ID (required with --goal)")
    parser.add_argument("--dispatch-config", type=str, metavar="FILE", default=None,
                        help="Path to dispatch config JSON (default: dispatch_config.json)")
    parser.add_argument("--max-tasks-per-project", type=int, default=None, metavar="N",
                        help="Cap tasks attempted per project per run")
    parser.add_argument("--only-project", type=int, action="append", default=None,
                        metavar="ID", help="Only run this project (repeatable)")
    parser.add_argument("--max-rounds", type=int, default=MAX_MANAGER_ROUNDS,
                        help=f"Max manager rounds (default: {MAX_MANAGER_ROUNDS})")
    parser.add_argument("--max-concurrent", type=int, default=None,
                        help="Override max concurrent goals (default: from goals file or 4)")
    parser.add_argument("--model", default=None, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help=f"Max agent turns (default: {DEFAULT_MAX_TURNS})")
    # Dynamically discover available roles from prompts directory
    prompts_dir = Path(__file__).parent.parent / "prompts"
    _available_roles = sorted([
        f.stem for f in prompts_dir.glob("*.md")
        if not f.name.startswith("_")
    ]) if prompts_dir.exists() else ["developer", "tester", "security-reviewer"]
    # NOTE: no argparse `choices=` here. Project-overlay roles
    # (<project_dir>/.equipa/roles/) are unknown at parse time because the
    # project dir is only known after a task is resolved, so a hard choices list
    # would reject valid project roles. The role is validated post-parse against
    # the project-aware resolver (see run_mode_task) and by build_system_prompt.
    # default=None (not "developer") so run_mode_task can tell an explicit
    # --role from an unset one: explicit wins, else the task's stored role
    # (tasks.role), else developer.
    parser.add_argument("--role", default=None,
                        help=f"Agent role. Base roles: {', '.join(_available_roles)}. "
                             f"Project roles in <project_dir>/.equipa/roles/ are also accepted. "
                             f"If unset, the task's stored role (tasks.role) is used, else developer.")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help=f"Max retry attempts (default: {DEFAULT_MAX_RETRIES})")
    parser.add_argument("--dev-test", action="store_true", help="Enable Dev+Tester iteration loop mode")
    parser.add_argument("--security-review", action="store_true", default=None,
                        help="Run security review after dev-test passes (default: from dispatch config)")
    parser.add_argument("--provider", choices=["claude", "ollama"], default=None,
                        help="Force provider for all agents (default: from dispatch config)")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # --- Config-versioning args (used with --config-cmd) ---
    parser.add_argument("--config-project", type=int, default=None,
                        metavar="N", help="Project id for --config-cmd (snapshot/list)")
    parser.add_argument("--config-message", "-m", type=str, default=None,
                        metavar="MSG", help="Commit message for --config-cmd snapshot")
    parser.add_argument("--config-version-a", type=int, default=None,
                        metavar="ID", help="First version id for --config-cmd diff")
    parser.add_argument("--config-version-b", type=int, default=None,
                        metavar="ID", help="Second version id for --config-cmd diff")
    parser.add_argument("--config-version", type=int, default=None,
                        metavar="ID", help="Target version id for --config-cmd rollback")
    parser.add_argument("--force", action="store_true",
                        help="Force --config-cmd rollback past dirty-file check")

    # --- Initiative helpers (Phase 1) ---
    parser.add_argument("--initiative-project", type=str, default=None,
                        metavar="CODENAME",
                        help="Project codename for --create-initiative / "
                             "--list-initiatives filter")
    parser.add_argument("--initiative-goal", type=str, default=None,
                        metavar="TEXT",
                        help="Goal statement for --create-initiative (locked at creation)")
    parser.add_argument("--max-waves", type=int, default=None, metavar="N",
                        help="Safety cap on the number of waves dispatched in "
                             "--initiative mode. Unset applies a default "
                             "backstop of 25 waves (a halt-with-pause, not a "
                             "crash); pass an explicit N to raise/lower it. "
                             "Note: there is no automatic cost cap in Phase 2 "
                             "— cost is tracked, not enforced.")

    return parser


# --- Per-Mode Handlers ---

def _auto_snapshot_dispatch(
    project_id: int | None,
    dispatch_config: dict | None,
    *,
    source: str = "auto-dispatch",
) -> None:
    """Take an auto-snapshot of tracked configs at dispatch entry.

    Cheap (dedup on content_sha → no DB write if config unchanged).
    Wrapped in try/except so a snapshot failure cannot crash dispatch.
    Skipped when ``features.config_versioning`` flag is False.
    """
    if not project_id:
        return
    if not is_feature_enabled(dispatch_config, "config_versioning"):
        return
    try:
        from equipa import config_versions
        config_versions.snapshot(int(project_id), source=source)
    except Exception:  # noqa: BLE001 — must not crash caller
        import logging
        logging.getLogger(__name__).exception(
            "[config_versions] auto-snapshot failed for project=%s source=%s",
            project_id, source,
        )


async def run_mode_mcp_server(args: argparse.Namespace) -> None:
    """Run as MCP server (JSON-RPC over stdio)."""
    run_server()


# Length caps enforced both at the CLI boundary AND at the DB layer via
# the CHECK constraint in scripts/migrate_initiative_schema.py. The CLI
# layer exists to produce friendly error messages; the DB layer exists
# to catch any caller that bypasses the CLI (raw SQL, future API, etc.).
INITIATIVE_NAME_MAX = 200
INITIATIVE_GOAL_MAX = 8192

# Control characters are stripped at the plan-file boundary by
# equipa.initiative._sanitize_agent_text, but we reject them here too so
# the initiative row in TheForge stays clean and downstream displays
# don't have to defend against terminal-control characters.
#
# S2486-04 fix: also reject Unicode bidirectional override (U+202A–U+202E,
# U+2066–U+2069) and zero-width characters (U+200B–U+200D, U+2060, U+FEFF).
# These enable the "Trojan Source" / CVE-2021-42574 attack class.
_INITIATIVE_CTRL_CHARS_RE = re.compile(
    "["
    "\x00-\x08\x0b-\x1f\x7f"
    "\u202a-\u202e"  # bidi override: LRE, RLE, PDF, LRO, RLO
    "\u2066-\u2069"  # bidi isolate: LRI, RLI, FSI, PDI
    "\u200b-\u200d"  # zero-width: ZWSP, ZWNJ, ZWJ
    "\u2060"          # word joiner
    "\ufeff"          # zero-width no-break space / BOM
    "]"
)


def _validate_initiative_input(*, name: str, goal: str) -> str | None:
    """Validate the two free-text fields the CLI inserts into ``initiatives``.

    Returns ``None`` if both fields pass, or a single human-readable
    error message describing the first violation. Cheap to call.
    """
    if len(name) > INITIATIVE_NAME_MAX:
        return (
            f"--create-initiative name exceeds {INITIATIVE_NAME_MAX} "
            f"chars (got {len(name)})"
        )
    if len(goal) > INITIATIVE_GOAL_MAX:
        return (
            f"--initiative-goal exceeds {INITIATIVE_GOAL_MAX} chars "
            f"(got {len(goal)})"
        )
    if _INITIATIVE_CTRL_CHARS_RE.search(name):
        return (
            "--create-initiative name contains disallowed control "
            "characters (ASCII C0/DEL, Unicode bidi-override, or "
            "zero-width); only newline and tab are allowed"
        )
    if _INITIATIVE_CTRL_CHARS_RE.search(goal):
        return (
            "--initiative-goal contains disallowed control characters "
            "(ASCII C0/DEL, Unicode bidi-override, or zero-width); "
            "only newline and tab are allowed"
        )
    # S2486-05: literal '<!--' check is intentionally byte-exact. The
    # orchestrator's END-marker recognition uses a literal str.find on
    # "<!-- END ORCHESTRATOR-MANAGED -->", so only the exact 4-byte ASCII
    # sequence \x3C\x21\x2D\x2D can spoof a marker. Unicode dash look-alikes
    # (U+2010-U+2014, U+2212) cannot — they would not match the orchestrator's
    # literal find either. Bidi-override and zero-width chars are already
    # rejected by _INITIATIVE_CTRL_CHARS_RE above, so a hidden \x2D cannot be
    # visually masked. Do NOT loosen this check without re-evaluating the
    # orchestrator's marker recognition.
    if "<!--" in name:
        return (
            "--create-initiative name contains literal '<!--' "
            "(HTML comment opener is reserved for orchestrator markers)"
        )
    if "<!--" in goal:
        return (
            "--initiative-goal contains literal '<!--' "
            "(HTML comment opener is reserved for orchestrator markers)"
        )
    return None


async def run_mode_create_initiative(args: argparse.Namespace) -> None:
    """Insert a new row into ``initiatives`` and print the new id."""
    name = (args.create_initiative or "").strip()
    project_codename = (args.initiative_project or "").strip()
    goal = (args.initiative_goal or "").strip()
    if not name:
        print("ERROR: --create-initiative requires a non-empty NAME")
        sys.exit(2)
    if not project_codename:
        print(
            "ERROR: --create-initiative requires --initiative-project "
            "<codename>"
        )
        sys.exit(2)
    if not goal:
        print(
            "ERROR: --create-initiative requires --initiative-goal <text>"
        )
        sys.exit(2)

    # S3 input validation. The CLI is the first writer to the initiatives
    # row, which then feeds the plan-file header (via _render_initial_template)
    # and the agent system prompt. Reject the same hostile inputs the
    # downstream sanitiser would catch — fail fast at the CLI boundary
    # rather than silently mangling state with truncation/escapes. The
    # DB CHECK constraint enforces the length caps even if a future
    # caller bypasses the CLI; keeping CLI errors clearer is the only
    # reason for the duplicated guard here.
    validation_error = _validate_initiative_input(name=name, goal=goal)
    if validation_error is not None:
        print(f"ERROR: {validation_error}")
        sys.exit(2)

    from equipa.db import get_db_connection

    conn = get_db_connection(write=True)
    try:
        # UX (#2491): match codename/name case-insensitively so the docs
        # example `--initiative-project equipa` resolves the `Equipa` project.
        row = conn.execute(
            "SELECT id FROM projects "
            "WHERE LOWER(codename) = LOWER(?) OR LOWER(name) = LOWER(?)",
            (project_codename, project_codename),
        ).fetchone()
        if row is None:
            print(f"ERROR: no project matches codename/name '{project_codename}'")
            sys.exit(1)
        project_id = row[0]
        cursor = conn.execute(
            "INSERT INTO initiatives (project_id, name, goal) VALUES (?, ?, ?)",
            (project_id, name, goal),
        )
        conn.commit()
        new_id = cursor.lastrowid
    finally:
        conn.close()

    print(f"Created initiative id={new_id} for project '{project_codename}' (id={project_id})")
    print(f"  Name: {name}")
    print(f"  Goal: {goal}")
    print(
        f"  Plan file will be created at "
        f"<target-repo>/.equipa/initiative-{new_id}.md on first dispatch."
    )


async def run_mode_list_initiatives(args: argparse.Namespace) -> None:
    """Print a readable table of initiatives, optionally project-filtered."""
    from equipa.db import get_db_connection

    project_codename = (args.initiative_project or "").strip() or None

    conn = get_db_connection(write=False)
    try:
        if project_codename:
            rows = conn.execute(
                """
                SELECT i.id, p.codename, i.name, i.status, i.created_at,
                       (SELECT COUNT(*) FROM tasks t WHERE t.initiative_id = i.id),
                       i.total_cost
                FROM initiatives i
                LEFT JOIN projects p ON p.id = i.project_id
                WHERE LOWER(p.codename) = LOWER(?) OR LOWER(p.name) = LOWER(?)
                ORDER BY i.status, i.created_at DESC
                """,
                (project_codename, project_codename),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT i.id, p.codename, i.name, i.status, i.created_at,
                       (SELECT COUNT(*) FROM tasks t WHERE t.initiative_id = i.id),
                       i.total_cost
                FROM initiatives i
                LEFT JOIN projects p ON p.id = i.project_id
                ORDER BY i.status, i.created_at DESC
                """,
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No initiatives found.")
        return

    header = (
        f"{'ID':>4}  {'PROJECT':<16}  {'STATUS':<11}  {'TASKS':>5}  "
        f"{'COST':>9}  {'CREATED':<19}  NAME"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        iid, codename, name, status, created_at, task_count, total_cost = r
        codename = (codename or "—")[:16]
        status = (status or "")[:11]
        created_at = (created_at or "")[:19]
        cost_str = f"${(total_cost or 0.0):.4f}"
        print(
            f"{iid:>4}  {codename:<16}  {status:<11}  "
            f"{task_count:>5}  {cost_str:>9}  {created_at:<19}  {name}"
        )


async def run_mode_initiative(args: argparse.Namespace) -> None:
    """Run an entire initiative end-to-end (Phase 2).

    Walks the sub-task DAG, dispatches in waves, halts on the first failure
    or operator pause marker, and accumulates cost into
    ``initiatives.total_cost``. A pause is the EXPECTED outcome of any
    failure — this handler exits 0 on a pause and only lets unexpected
    exceptions propagate non-zero.
    """
    from equipa.db import get_db_connection
    from equipa.initiative_runner import (
        CycleError,
        InitiativeError,
        InitiativeLockError,
        compute_waves,
        fetch_initiative,
        fetch_initiative_tasks,
        run_initiative,
    )

    initiative_id = int(args.initiative)

    # --dry-run: print the wave plan, dispatch nothing.
    if args.dry_run:
        conn = get_db_connection(write=False)
        try:
            initiative = fetch_initiative(conn, initiative_id)
            if initiative is None:
                print(f"ERROR: no initiative with id={initiative_id}")
                sys.exit(1)
            tasks = fetch_initiative_tasks(
                conn, initiative_id, force=getattr(args, "force", False)
            )
        finally:
            conn.close()

        print(f"\n--- DRY RUN (Initiative #{initiative_id}) ---")
        print(f"Name:   {initiative['name']}")
        print(f"Status: {initiative['status']}")
        print(f"Pending sub-tasks: {len(tasks)}")
        if not tasks:
            print("  (none — running would mark the initiative 'done')")
        else:
            try:
                waves = compute_waves(tasks)
            except CycleError as exc:
                print(f"  CYCLE DETECTED: {exc}")
                sys.exit(1)
            title_by_id = {int(t["id"]): t.get("title", "") for t in tasks}
            for i, wave in enumerate(waves):
                print(f"  Wave {i + 1}:")
                for tid in wave:
                    print(f"    - #{tid}: {title_by_id.get(tid, '')}")
        if args.max_waves is not None:
            print(f"Max waves cap: {args.max_waves}")
        print("--- END DRY RUN ---")
        return

    # Live run. run_initiative owns its own write connection.
    try:
        result = await run_initiative(
            initiative_id,
            args,
            max_waves=args.max_waves,
            force=getattr(args, "force", False),
        )
    except InitiativeLockError as exc:
        # S3: another live --initiative run holds the lock. Refuse rather
        # than double-dispatch the same wave against the same repo.
        print(f"ERROR: {exc}")
        sys.exit(1)
    except CycleError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    except InitiativeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"\n{'#' * 60}")
    print(f"INITIATIVE #{initiative_id} — {result.status.upper()}")
    print(f"{'#' * 60}")
    print(f"Waves dispatched: {result.waves_dispatched}/{len(result.waves_planned)}")
    print(f"Sub-tasks completed this run: {len(result.tasks_completed)}")
    if result.tasks_failed:
        print(f"Sub-tasks failed/blocked: "
              f"{', '.join(f'#{t}' for t in result.tasks_failed)}")
    print(f"Cost added this run: ${result.total_cost_added:.4f}")
    if result.halted:
        print(f"HALTED: {result.halt_reason}")
        if result.open_question_id is not None:
            print(f"Filed open_question #{result.open_question_id}.")
        print("Fix the blocker / resolve the pause marker, then re-run "
              f"--initiative {initiative_id} to resume.")
    print(f"{'#' * 60}")
    # A pause is an EXPECTED outcome, not an error — exit 0.


# --- Config-Versioning Helpers ---

def _resolve_config_project_id(args: argparse.Namespace) -> int:
    """Resolve the project id used by config-versioning commands.

    Order of precedence:
        1. Explicit --project flag
        2. --goal-project flag
        3. Single project registered in TheForge

    Exits with a clear error if none of the above can identify a project.
    """
    explicit = (getattr(args, "config_project", None)
                or getattr(args, "goal_project", None))
    if explicit:
        return int(explicit)

    db_path = _equipa_constants.THEFORGE_DB
    if not db_path.exists():
        print(f"ERROR: TheForge DB not found at {db_path}; pass --project N")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE status != 'archived'"
        ).fetchall()
    finally:
        conn.close()

    if len(rows) == 1:
        return int(rows[0][0])
    print("ERROR: --config-cmd requires --project N (multiple active projects).")
    for row in rows:
        print(f"  {row[0]}: {row[1]}")
    sys.exit(1)


async def run_mode_config(args: argparse.Namespace) -> None:
    """Dispatch the config-versioning subcommand selected by --config-cmd."""
    from equipa import config_versions

    verb = args.config_cmd

    if verb == "snapshot":
        project_id = _resolve_config_project_id(args)
        version_id = config_versions.snapshot(
            project_id,
            source="manual",
            commit_message=args.config_message,
        )
        print(f"snapshot: project={project_id} version_id={version_id}")
        return

    if verb == "list":
        project_id = _resolve_config_project_id(args)
        rows = config_versions.list_versions(project_id)
        if not rows:
            print(f"No config versions for project {project_id}.")
            return
        print(f"id\tcreated_at\tsource\tmessage")
        for row in rows:
            msg = row.get("commit_message") or ""
            print(f"{row['id']}\t{row['created_at']}\t{row['source']}\t{msg}")
        return

    if verb == "diff":
        if args.config_version_a is None or args.config_version_b is None:
            print("ERROR: --config-cmd diff requires "
                  "--config-version-a ID --config-version-b ID")
            sys.exit(1)
        diffs = config_versions.diff(args.config_version_a, args.config_version_b)
        if not diffs:
            print("(no differences)")
            return
        for path, text in diffs.items():
            print(f"--- {path} ---")
            print(text)
        return

    if verb == "rollback":
        if args.config_version is None:
            print("ERROR: --config-cmd rollback requires --config-version ID")
            sys.exit(1)
        try:
            paths = config_versions.rollback(
                args.config_version,
                dry_run=args.dry_run,
                force=args.force,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            sys.exit(2)
        verb_label = "would rewrite" if args.dry_run else "rewrote"
        print(f"rollback: {verb_label} {len(paths)} file(s) for version {args.config_version}")
        for p in paths:
            print(f"  {p}")
        return

    print(f"ERROR: unknown config verb: {verb}")
    sys.exit(1)


async def run_mode_regenerate_manifest(args: argparse.Namespace) -> None:
    """Regenerate skill_manifest.json with SHA-256 hashes."""
    write_skill_manifest()


async def run_mode_add_project(args: argparse.Namespace) -> None:
    """Register a new project in EQUIPA DB and config."""
    if not args.project_dir:
        # Re-create a minimal parser to surface the same error message format
        # used by the original implementation.
        _build_arg_parser().error("--add-project requires --project-dir <path>")
    _handle_add_project(args.add_project, args.project_dir)


async def run_mode_setup_repos(args: argparse.Namespace) -> None:
    """Init git + GitHub private repo for one or all projects."""
    setup_all_repos(args)


async def run_mode_auto_run(args: argparse.Namespace) -> None:
    """Auto-scan projects and dispatch work by priority."""
    dispatch_config = args.dispatch_config

    # CLI overrides
    if args.max_concurrent is not None:
        dispatch_config["max_concurrent"] = args.max_concurrent
    if args.max_tasks_per_project is not None:
        dispatch_config["max_tasks_per_project"] = args.max_tasks_per_project

    # Scan DB for pending work
    print("Scanning TheForge for pending work...")
    work = scan_pending_work()
    if not work:
        print("No projects with todo tasks found.")
        return

    # Apply filters
    work = apply_dispatch_filters(work, dispatch_config, args)
    if not work:
        print("No projects match filters (check --only-project, skip_projects, only_projects).")
        return

    # Score and sort
    for proj in work:
        score_project(proj, dispatch_config)
    work.sort(key=lambda p: p.get("score", 0), reverse=True)

    # Auto-snapshot tracked configs once per project at dispatch entry.
    for proj in work:
        _auto_snapshot_dispatch(proj.get("project_id"), dispatch_config)

    # Dry run: show plan and exit
    if args.dry_run:
        print("\n--- DRY RUN (Auto-Run) ---")
        print_dispatch_plan(work, dispatch_config)
        print("\n--- END DRY RUN ---")
        return

    # Show plan and confirm
    print_dispatch_plan(work, dispatch_config)

    if not args.yes:
        response = input("\nProceed with dispatch? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Dispatch
    _refuse_if_any_refused(await run_auto_dispatch(work, dispatch_config, args))


def _refuse_if_any_refused(results: list) -> None:
    """Exit with EXIT_DISPATCH_REFUSED when a project or goal was refused."""
    refusals = collect_refusals(results)
    if refusals:
        refuse_dispatch(
            f"{len(refusals)} refusal(s) in this run: " + "; ".join(refusals)
        )


async def run_mode_parallel_goals(args: argparse.Namespace) -> None:
    """Run multiple goals in parallel from a goals JSON file."""
    defaults, goals = load_goals_file(args.parallel_goals)
    resolved_goals = validate_goals(goals)

    # Apply per-goal defaults from file, allow CLI overrides
    if args.max_concurrent is not None:
        defaults["max_concurrent"] = args.max_concurrent

    if args.dry_run:
        print("\n--- DRY RUN (Parallel Goals) ---")
        print(f"Goals file: {args.parallel_goals}")
        print(f"Goals: {len(resolved_goals)}")
        print(f"Max concurrent: {defaults['max_concurrent']}")
        print(f"Default model: {defaults['model']}")
        print(f"Default max turns: {defaults['max_turns']}")
        print(f"Default max rounds: {defaults['max_rounds']}")
        print()
        for i, g in enumerate(resolved_goals):
            model = g.get("model", defaults["model"])
            print(f"  [{i + 1}] Project: {g['project_info']['name']} (ID: {g['project_id']})")
            print(f"      Goal: {g['goal'][:80]}")
            print(f"      Dir: {g['project_dir']}")
            print(f"      Model: {model}")
            planner_prompt = build_planner_prompt(
                g["goal"], g["project_id"], g["project_dir"],
                fetch_project_context(g["project_id"]),
            )
            print(f"      Planner prompt: {len(planner_prompt)} chars")
            print()
        print("--- END DRY RUN ---")
        return

    # Confirm
    if not args.yes:
        print(f"\nAbout to run {len(resolved_goals)} goals "
              f"(max {defaults['max_concurrent']} concurrent).")
        for i, g in enumerate(resolved_goals):
            print(f"  [{i + 1}] {g['project_info']['name']}: {g['goal'][:60]}")
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    _refuse_if_any_refused(await run_parallel_goals(resolved_goals, defaults, args))


async def run_mode_goal(args: argparse.Namespace) -> None:
    """Manager mode: high-level goal across multiple rounds."""
    project_info = fetch_project_info(args.goal_project)
    if not project_info:
        print(f"ERROR: Project {args.goal_project} not found in TheForge")
        sys.exit(1)

    # Resolve project directory from project info
    codename = project_info.get("codename", "").lower().strip()
    project_name = project_info.get("name", "").lower().strip()
    project_dir = _equipa_constants.PROJECT_DIRS.get(codename) or _equipa_constants.PROJECT_DIRS.get(project_name)

    if not project_dir:
        print(f"ERROR: Could not find project directory for '{project_info.get('name', 'Unknown')}'")
        print("Known projects:", ", ".join(sorted(_equipa_constants.PROJECT_DIRS.keys())))
        sys.exit(1)

    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        sys.exit(1)

    project_context = fetch_project_context(args.goal_project)

    # Auto-snapshot tracked configs once at dispatch entry.
    _auto_snapshot_dispatch(
        args.goal_project, getattr(args, "dispatch_config", None),
    )

    # Show goal info
    print(f"\nGoal: {args.goal}")
    print(f"Project: {project_info.get('name', 'Unknown')} (ID: {args.goal_project})")
    print(f"Directory: {project_dir}")
    print(f"Model: {args.model}")
    print(f"Max turns/agent: {args.max_turns}")
    print(f"Max rounds: {args.max_rounds}")

    if args.dry_run:
        planner_prompt = build_planner_prompt(
            args.goal, args.goal_project, project_dir, project_context,
        )
        print("\n--- DRY RUN (Manager Mode) ---")
        print(f"Planner prompt: {len(planner_prompt)} chars")
        print(f"\nManager loop would run up to {args.max_rounds} rounds.")
        print("Each round: Planner -> Dev+Test loop per task -> Evaluator")
        print("\n--- END DRY RUN ---")
        return

    # Confirm before running
    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Run the manager loop
    print(f"\nStarting Manager mode (max {args.max_rounds} rounds)...")
    outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
        args.goal, args.goal_project, project_dir, project_context, args,
    )

    print_manager_summary(args.goal, outcome, rounds, completed, blocked, cost, duration)
    if outcome in GOAL_REFUSED_OUTCOMES:
        # 3112 review (task #3119): a refused goal used to exit 0.
        refuse_dispatch(f"goal stopped: {outcome}")


async def run_mode_tasks(args: argparse.Namespace) -> None:
    """Parallel tasks mode: run several task IDs concurrently."""
    # dispatch-11 (task #3112): --tasks runs every task's Dev+Test loop with
    # the role stored on the task (tasks.role). An explicit --role used to be
    # dropped without a word; it is now refused so nothing is overridden
    # silently.
    if getattr(args, "role", None):
        refuse_dispatch(
            f"--tasks does not support --role ('{args.role}'): each task runs "
            f"a Dev+Test loop with its own tasks.role. Set tasks.role in "
            f"TheForge, or dispatch one task with --task <id> --role "
            f"{args.role}."
        )
    if not getattr(args, "dev_test", False):
        print("NOTE: --tasks always runs the Dev+Test loop (developer + tester) "
              "for every task; --dev-test is implied.")
    try:
        task_ids = parse_task_ids(args.tasks)
    except ValueError as exc:
        refuse_dispatch(f"--tasks: {exc}")
    if not task_ids:
        refuse_dispatch("Could not parse task IDs from --tasks argument.")

    # Auto-snapshot tracked configs once at dispatch entry.
    tasks_for_snapshot = fetch_tasks_by_ids(task_ids)
    if tasks_for_snapshot:
        _auto_snapshot_dispatch(
            tasks_for_snapshot[0].get("project_id"),
            getattr(args, "dispatch_config", None),
        )

    if args.dry_run:
        tasks = fetch_tasks_by_ids(task_ids)
        print("\n--- DRY RUN (Parallel Tasks) ---")
        print(f"Tasks: {len(tasks)}")
        for t in tasks:
            print(f"  - #{t['id']}: {t['title']} ({t.get('project_name', '?')})")
        print("\n--- END DRY RUN ---")
        return

    await run_parallel_tasks(task_ids, args)


def _fetch_and_resolve_task(args: argparse.Namespace):
    """Load the task, resolve its project directory and effective role, and
    gather project context.

    Extracted verbatim from ``run_mode_task`` (task 2705, pure refactor):
    task fetch, dispatch-config auto-snapshot, project-dir resolution
    (incl. scaffold bootstrap/clone), role resolution + validation, and the
    task-info / checkpoint banner. Exits the process on any unrecoverable
    error exactly as the inline code did. Returns
    ``(task, project_dir, project_context)``."""
    # --- Fetch task ---
    if args.task:
        task = fetch_task(args.task)
        if not task:
            print(f"ERROR: Task {args.task} not found in TheForge")
            sys.exit(1)
    else:
        task = fetch_next_todo(args.project)
        if not task:
            print(f"No todo tasks found for project {args.project}")
            sys.exit(0)

    # Auto-snapshot tracked configs once at dispatch entry.
    _auto_snapshot_dispatch(
        task.get("project_id"),
        getattr(args, "dispatch_config", None),
    )

    # Resolve project directory
    project_dir = resolve_project_dir(task)
    if not project_dir:
        # Scaffold-based projects may have a recorded local_path that does
        # not yet exist on disk. Detect that and let ensure_scaffold below
        # populate it.
        try:
            from equipa.dispatch import _bootstrap_scaffold_if_needed
            project_dir = _bootstrap_scaffold_if_needed(
                task, task.get("project_id")
            )
        except Exception:
            project_dir = None
    if not project_dir:
        print(f"ERROR: Could not find project directory for '{task.get('project_name', 'Unknown')}'")
        print("Known projects:", ", ".join(sorted(_equipa_constants.PROJECT_DIRS.keys())))
        sys.exit(1)

    # Resolve the effective dispatch role now that the task (and its project)
    # is known: an explicit --role wins; else the task's stored role
    # (tasks.role); else developer. Set args.role so every downstream consumer
    # sees the resolved value. Then validate it project-aware — argparse can't,
    # because project-overlay roles (<project_dir>/.equipa/roles/) are unknown
    # at parse time, so --role accepts any string and we check it here.
    if not getattr(args, "role", None):
        args.role = (task.get("role") or "developer")
    from equipa.role_resolver import role_exists, available_roles
    if not role_exists(args.role, project_dir):
        print(f"ERROR: Unknown role '{args.role}'. "
              f"Available for this project: {', '.join(available_roles(project_dir))}")
        sys.exit(1)

    # Auto-clone ForgeScaffold for empty/missing scaffold-based projects.
    try:
        from equipa.scaffold import ensure_scaffold, ScaffoldCloneError
        if ensure_scaffold(project_dir, task.get("project_id")):
            print(f"Auto-cloned ForgeScaffold into {project_dir}")
    except ScaffoldCloneError as exc:
        print(f"ERROR: Scaffold auto-clone failed: {exc}")
        sys.exit(1)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"WARN: Scaffold auto-clone raised {exc!r}")

    # Verify project directory exists
    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        sys.exit(1)

    # Fetch project context
    project_context = fetch_project_context(task.get("project_id", 0))

    # Show task info
    complexity = get_task_complexity(task)
    mode_label = "Dev+Test loop" if args.dev_test else f"{args.role} (single agent)"
    print(f"\nTask #{task['id']}: {task['title']}")
    print(f"Project: {task.get('project_name', 'Unknown')}")
    print(f"Priority: {task.get('priority', 'medium')}")
    print(f"Complexity: {complexity}")
    print(f"Mode: {mode_label}")
    print(f"Directory: {project_dir}")

    if args.dev_test:
        # Use task-specified role if available, otherwise default to developer
        task_role = (task.get('role') if isinstance(task, dict) else None) or "developer"
        dev_model = get_role_model(task_role, args, task=task)
        dev_turns = get_role_turns("developer", args, task=task)
        tester_model = get_role_model("tester", args, task=task)
        tester_turns = get_role_turns("tester", args, task=task)
        dev_budget, _ = calculate_dynamic_budget(dev_turns)
        tester_budget, _ = calculate_dynamic_budget(tester_turns)
        print(f"Developer: model={dev_model}, budget={dev_budget}/{dev_turns} (dynamic)")
        print(f"Tester: model={tester_model}, budget={tester_budget}/{tester_turns} (dynamic)")
        print(f"Max cycles: {MAX_DEV_TEST_CYCLES}")
        print("Compaction: Always (context engineering — never pass raw output between cycles)")
        # Check for checkpoint
        cp_text, cp_attempt = load_checkpoint(task['id'], role="developer")
        if cp_text:
            print(f"Checkpoint: Found from attempt #{cp_attempt} ({len(cp_text)} chars) — will auto-resume")
    else:
        role_model = get_role_model(args.role, args, task=task)
        role_turns = get_role_turns(args.role, args, task=task)
        role_budget, _ = calculate_dynamic_budget(role_turns)
        print(f"Model: {role_model}")
        print(f"Budget: {role_budget}/{role_turns} turns (dynamic)")
        print(f"Max retries: {args.retries}")
    return task, project_dir, project_context


def _run_task_dry_run(task, project_context, project_dir, args):
    """Dry-run branch: build a sample system prompt + CLI command and print
    their sizes without dispatching. Behaviour identical to the inline
    ``if args.dry_run`` block."""
    # Build a sample prompt to show size
    system_prompt = build_system_prompt(task, project_context, project_dir, role="developer",
                                              dispatch_config=getattr(args, "dispatch_config", None))
    dry_model = get_role_model("developer", args, task=task)
    dry_turns = get_role_turns("developer", args, task=task)

    # Use the contextmanager so the dry-run does NOT litter /tmp with
    # leftover prompt files — the scanner reads the size while still
    # inside the with-block, then the file is removed on exit.
    with build_cli_command(
        system_prompt, project_dir, dry_turns, dry_model, role="developer",
    ) as cmd:
        print("\n--- DRY RUN ---")
        print(f"System prompt: {len(system_prompt)} chars, ~{estimate_tokens(system_prompt)} tokens")
        print(f"Command ({len(cmd)} args):")
        for i, part in enumerate(cmd):
            if i > 0 and cmd[i - 1] == "--append-system-prompt":
                print(f"  [system prompt: {len(part)} chars]")
            elif i > 0 and cmd[i - 1] == "--append-system-prompt-file":
                try:
                    sz = os.path.getsize(part)
                except OSError:
                    sz = -1
                print(f"  [system prompt file: {part} ({sz} bytes)]")
            elif len(part) > 100:
                print(f"  {part[:100]}...")
            else:
                print(f"  {part}")

        if args.dev_test:
            print(f"\nDev-Test loop would run up to {MAX_DEV_TEST_CYCLES} cycles.")
            print("Each cycle: Developer agent -> Tester agent -> feedback loop.")

        print("\n--- END DRY RUN ---")


async def _run_dev_test_mode(
    task, project_dir, project_context, args, *, task_branch: str | None = None,
):
    """Dev+Tester iteration loop (Phase 2) with autoresearch retry.

    Handles circuit-breaker demotion and the autoresearch retry/cleanup
    loop. Returns ``(result, cycles, outcome)``.

    ``task_branch`` is set when ``project_dir`` is the task's isolation
    worktree (task #3112): the worktree must be on that branch before every
    attempt, and a failed attempt is reset to the commit it started from.
    A reset that fails (:class:`AttemptCleanupError`) stops the retries and
    leaves the task blocked with outcome ``attempt_cleanup_failed`` instead
    of crashing with the task stuck in_progress.

    Without ``task_branch`` the project was not git at dispatch. R3166-01 /
    N1 (task #3168): the attempts run under ``dispatched_without_git``, and
    a repository found there before or after an attempt stops the task with
    outcome ``REPOSITORY_APPEARED_OUTCOME``, as in
    :func:`equipa.dispatch.run_dev_test_loop_with_autoresearch`."""
    if task_branch is not None:
        return await _run_dev_test_attempts(
            task, project_dir, project_context, args, task_branch,
        )
    with dispatched_without_git(project_dir):
        return await _run_dev_test_attempts(
            task, project_dir, project_context, args, None,
        )


async def _run_dev_test_attempts(
    task, project_dir, project_context, args, task_branch: str | None,
):
    """The retry loop of :func:`_run_dev_test_mode`."""
    # Dev+Tester iteration loop (Phase 2) with autoresearch retry
    print(f"\nStarting Dev+Test loop (max {MAX_DEV_TEST_CYCLES} cycles)...")

    # Autoresearch config
    dc = getattr(args, "dispatch_config", None) or {}
    autoresearch_on = is_feature_enabled(dc, "autoresearch")
    max_retries = dc.get("autoresearch_max_retries", 3) if autoresearch_on else 0
    retry_count = 0
    attempt_reflections: list[str] = []
    base_sha: str | None = None
    result: dict = {"cost": 0.0, "duration": 0.0}
    cycles = 0

    while True:
        if task_branch is not None:
            try:
                head_sha = await _require_task_branch(project_dir, task_branch)
            except AttemptCleanupError as exc:
                _audit_task_abort(task["id"], "worktree-branch-mismatch", exc, None)
                outcome = "worktree_branch_mismatch"
                break
            if base_sha is None:
                base_sha = head_sha
        elif _repository_appeared_in_non_git_project(
            task["id"], project_dir, f"before attempt {retry_count + 1}", None,
        ):
            outcome = REPOSITORY_APPEARED_OUTCOME
            break
        try:
            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args,
            )
        except CircuitOpenError as exc:
            # S1 (2453, RT-02 follow-up): auto-routing fail-closed.
            # Demote to ``circuit_breaker_blocked`` so the task can be
            # re-tried after the breaker recovery window without
            # silently escalating cost to opus via DEFAULT_ROLE_MODELS.
            print(
                f"  [GATE-AUDIT] task={task['id']} event=circuit-blocked "
                f"role={exc.role} tier_attempted={exc.tier_attempted}"
            )
            # Task #2702: durably persist the gate event. This site emits
            # via print() (stdout), not _gate_audit_log() (stderr), so we
            # call the DB helper directly to avoid a duplicate stderr line.
            # Best-effort fail-open — log_gate_audit swallows all DB errors.
            log_gate_audit(
                f"task={task['id']} event=circuit-blocked "
                f"role={exc.role} tier_attempted={exc.tier_attempted}",
                task["id"],
                event="circuit-blocked",
            )
            print(
                f"  [Routing] Task #{task['id']} blocked by circuit "
                f"breaker ({exc}); deferring dispatch "
                f"(outcome=circuit_breaker_blocked)."
            )
            result = {"cost": 0.0, "duration": 0.0}
            cycles = 0
            outcome = "circuit_breaker_blocked"
            break

        # R3162-01 (task #3165): assert the branch AFTER every attempt too, as
        # the dispatch loop has since task #3111. Without it a worktree the
        # agent swapped during its attempt went straight to
        # cleanup_failed_attempt, and a successful attempt's commits on
        # another branch were treated as the task's result.
        if task_branch is not None:
            try:
                await _require_task_branch(project_dir, task_branch)
            except AttemptCleanupError as exc:
                _audit_task_abort(
                    task["id"], "worktree-branch-mismatch",
                    f"after attempt {retry_count + 1} ({outcome}): {exc}", None,
                )
                outcome = "worktree_branch_mismatch"
                break
        elif _repository_appeared_in_non_git_project(
            task["id"], project_dir, f"after attempt {retry_count + 1} ({outcome})", None,
        ):
            outcome = REPOSITORY_APPEARED_OUTCOME
            break

        # Success - break out
        if outcome in ("tests_passed", "no_tests", "early_completed_no_changes"):
            break

        # Capture reflection from the failed attempt for cross-attempt memory
        attempt_reflections.append(
            _build_dispatch_attempt_reflection(
                retry_count + 1, outcome, cycles, result,
            )
        )

        # Not retriable or exhausted
        if not autoresearch_on or retry_count >= max_retries:
            if retry_count > 0:
                print(f"  [Autoresearch] Exhausted {retry_count}/{max_retries} retries "
                      f"for task #{task['id']}. Final outcome: {outcome}")
            break

        retry_count += 1
        print(f"  [Autoresearch] Task #{task['id']} failed ({outcome}). "
              f"Retry {retry_count}/{max_retries}...")

        # Clean up failed branch and reset task (with reflection memory).
        # 3107 review N1: a failed reset must not crash the run and leave the
        # task in_progress — stop retrying and record why (same as
        # equipa.dispatch.run_dev_test_loop_with_autoresearch).
        # R3165-01 (task #3166): without a task branch the project was not
        # git at dispatch, so no git step may run in it.
        try:
            await cleanup_failed_attempt(
                task["id"], project_dir, attempt_reflections, base_sha=base_sha,
                expect_repository=task_branch is not None,
            )
        except AttemptCleanupError as exc:
            _audit_task_abort(task["id"], "attempt-cleanup-failed", exc, None)
            # R3166-04 (task #3168): escaped like the audit line just above,
            # which raw control characters here could otherwise redraw.
            print(
                f"  [Autoresearch] Task #{task['id']}: resetting the failed "
                f"attempt failed ({escape_audit_detail(exc)}); no further "
                f"retries, task left blocked."
            )
            outcome = "attempt_cleanup_failed"
            break
    return result, cycles, outcome


async def _run_security_review_and_gate(
    task, project_dir, project_context, args, outcome, *, guard=None,
):
    """Optional security review followed by the Bug #2450 unified gated
    post-merge. May demote ``outcome`` to ``security_review_blocked``.
    Returns the (possibly updated) ``outcome``.

    Task #3111 (dispatch-05): in a git project a merge-eligible outcome
    survives only if the gated merge verifiably put the task on the default
    branch and ``guard`` saw no foreign movement; otherwise it is demoted
    (``merge_blocked`` / ``merge_failed`` / ``merge_integrity_failed``) so
    the task is recorded as blocked, never done."""
    # Optional security review after successful dev-test. Must run
    # BEFORE _post_task_telemetry so that CRITICAL/HIGH findings can
    # demote the outcome to ``security_review_blocked`` and prevent
    # the task from being marked done (parity with parallel-mode in
    # equipa.dispatch.run_parallel_tasks). Bug 2448: single-task mode
    # used to call run_security_review here and ignore the result,
    # so the merge gate was silently bypassed in single-task mode
    # (concretely task #2382 on 2026-05-19: 4 HIGH findings reported
    # but the task was marked SUCCESS and merged to master).
    review_blocks_merge = False
    review_counts: dict | None = None
    # Phase H (F-01): track whether the doc-only short-circuit fired,
    # so the post-merge call can tell the defensive invariant inside
    # _merge_task_branch NOT to demand an artifact that was never
    # produced (doc-only diffs skip the reviewer per task #2358).
    review_skipped_doc_only = False
    if (
        is_security_review_enabled(args)
        and outcome in ("tests_passed", "no_tests")
    ):
        # Task 2360 defect 1: doc-only diffs (only .md/.txt/.rst etc.)
        # cannot introduce code-level vulnerabilities, so the security
        # gate must skip them rather than risk a prose-matching false
        # positive blocking the merge. Concrete trigger: task 2358 — a
        # CRYPTOTRADER-V3-ARCHITECTURE.md spec was blocked because the
        # document used the word "HIGH" and discussed API-key auth.
        # base_ref omitted → auto-detect default branch (task #2479).
        changed_files = await get_changed_files_for_branch(
            project_dir,
        )
        review_skipped_doc_only = is_doc_only_diff(changed_files)
        review_crashed = False
        sec_result = None
        if review_skipped_doc_only:
            record_reviewer_skipped_doc_only(task["id"])
            print(
                f"  [Task #{task['id']}] SECURITY GATE: skipping "
                f"review — doc-only change "
                f"({len(changed_files)} file(s), all docs)."
            )
        else:
            # Task 2341 S2 parity: if run_security_review raises, the
            # artifact check alone is not enough — a stale review
            # file from a prior run could exist and silently
            # re-authorise. Treat a crashed reviewer as fail-closed
            # regardless of artifact state.
            try:
                sec_result = await run_security_review(
                    task, project_dir, project_context, args,
                )
            except Exception:  # pragma: no cover - defensive
                review_crashed = True
                import logging
                logging.getLogger(__name__).exception(
                    "[Task #%s] security review crashed", task["id"],
                )
        if review_skipped_doc_only:
            # Doc-only short-circuit: no artifact expected, do not
            # let the missing-artifact fail-closed path block the
            # merge here.
            review_blocks_merge = False
            review_counts = None
        else:
            _config_for_flag = (
                getattr(args, "dispatch_config", None) or {}
            )
            _block_on_missing = is_feature_enabled(
                _config_for_flag,
                "security_review_block_on_missing_artifact",
            )
            review_blocks_merge, review_counts = (
                _security_review_blocks_merge(
                    project_dir, task["id"],
                    block_on_missing=_block_on_missing,
                )
            )
        if review_crashed:
            review_blocks_merge = True
            print(
                f"  [Task #{task['id']}] SECURITY GATE: blocking "
                f"merge — security review crashed; branch "
                f"forge-task-{task['id']} left unmerged for "
                f"operator review."
            )
            outcome = "security_review_blocked"
        elif is_overloaded_result(sec_result):
            # Task #2994 S1: the reviewer never ran (sustained 529), so any
            # artifact on disk (stale, or absent with block_on_missing off)
            # says nothing about THIS diff. Fail loudly as overloaded.
            review_blocks_merge = True
            print(
                f"  [Task #{task['id']}] SECURITY GATE: blocking merge — "
                f"security reviewer FAILED: model overloaded (529) through "
                f"every retry. Not downgrading the model; branch "
                f"forge-task-{task['id']} left unmerged."
            )
            outcome = OVERLOADED_OUTCOME
        elif (
            not review_skipped_doc_only
            and reviewer_run_failure(task["id"]) is not None
        ):
            # Task #3041: the reviewer failed or timed out (after its retry),
            # so no artifact on disk is this run's review — block regardless.
            review_blocks_merge = True
            print(
                f"  [Task #{task['id']}] SECURITY GATE: blocking merge — "
                f"security reviewer FAILED "
                f"({reviewer_run_failure(task['id'])}); no artifact on disk "
                f"is trusted. Branch forge-task-{task['id']} left unmerged "
                f"for operator review."
            )
            outcome = "security_review_blocked"
        elif review_blocks_merge:
            if review_counts is None:
                print(
                    f"  [Task #{task['id']}] SECURITY GATE: blocking "
                    f"merge — .equipa-artifacts/SECURITY-REVIEW-"
                    f"{task['id']}.md artifact is missing (fail-closed). Branch "
                    f"forge-task-{task['id']} left unmerged for "
                    f"operator review."
                )
            else:
                print(
                    f"  [Task #{task['id']}] SECURITY GATE: blocking "
                    f"merge — {review_counts.get('CRITICAL', 0)} "
                    f"CRITICAL, {review_counts.get('HIGH', 0)} "
                    f"HIGH finding(s). Branch forge-task-"
                    f"{task['id']} left unmerged for operator "
                    f"review."
                )
            outcome = "security_review_blocked"

    # Bug #2450: unified gated post-merge. Single-task ``--dev-test`` mode
    # now performs the worktree-branch merge here, AFTER the security gate
    # has run, via the same ``_merge_task_branch`` helper that parallel
    # mode uses (equipa.dispatch.run_parallel_tasks). If the gate blocked
    # (``review_blocks_merge`` truthy OR outcome demoted to
    # ``security_review_blocked``), no merge is attempted and the branch
    # is left intact for operator review. This couples the DB-gate and
    # the git-merge that were previously decoupled (task 2449 proof:
    # outcome=security_review_blocked but commits on master, branch gone).
    if args.dev_test:
        merge_eligible = outcome in ("tests_passed", "no_tests")
        merge_branch = f"forge-task-{task['id']}"
        # Task #2706: no per-task trust signal is forwarded — the unified
        # gate re-derives doc-only-ness from the real diff and re-reads the
        # artifact itself. ``review_blocks_merge`` / ``review_skipped_doc_
        # only`` above still drive the DB-status outcome demotion and the
        # operator log lines, but they NO LONGER steer the merge decision.
        if guard is None and _is_git_project(project_dir):
            # MI-06 (task #3116): the default branch was not pinned before
            # the agents ran, so a fresh snapshot now would adopt whatever
            # they did as the baseline. Parallel mode merges nothing in this
            # case; single-task mode now refuses the same way.
            merge_result = "refused"
            print(
                f"  [Task #{task['id']}] MERGE: refused — the default branch "
                f"was not pinned before dispatch; {merge_branch} left "
                f"unmerged for operator review."
            )
            log_gate_audit(
                f"task={task['id']} event=merge-refused branch={merge_branch} "
                f"reason=default-branch-not-pinned-before-dispatch",
                task["id"],
                event="merge-refused",
            )
        else:
            merge_result = await _gated_post_merge(
                repo=project_dir,
                branch=merge_branch,
                outcome=outcome,
                task_id=task["id"],
                security_review_enabled=is_security_review_enabled(args),
                block_on_missing=is_feature_enabled(
                    getattr(args, "dispatch_config", None) or {},
                    "security_review_block_on_missing_artifact",
                ),
                guard=guard,
            )
        if merge_result == "merged":
            print(
                f"  [Task #{task['id']}] MERGE: branch {merge_branch} "
                f"merged to master via gated post-merge."
            )
        elif merge_result == "merge_failed":
            print(
                f"  [Task #{task['id']}] MERGE: gated post-merge failed "
                f"for {merge_branch}; branch preserved for operator."
            )
        elif merge_result == "blocked":
            print(
                f"  [Task #{task['id']}] MERGE: skipped — security gate "
                f"blocked merge of {merge_branch}."
            )
        if merge_eligible and _is_git_project(project_dir):
            if guard is not None:
                await guard.verify("end-of-run", task_id=task["id"])
            merge_outcome = (
                guard.outcomes.get(task["id"]) if guard is not None else None
            )
            final_outcome, _merged_sha, reason = outcome_after_merge(
                outcome, merge_outcome, guard,
            )
            if final_outcome != outcome:
                single_line_reason = " ".join(reason.split())
                print(
                    f"  [Task #{task['id']}] STATUS: {final_outcome} — "
                    f"{single_line_reason}; task left blocked, not done."
                )
                log_gate_audit(
                    f"task={task['id']} event=task-status-after-merge "
                    f"outcome={final_outcome} merged_sha=none "
                    f"reason={single_line_reason}",
                    task["id"],
                    event="task-status-after-merge",
                )
            outcome = final_outcome
    return outcome


def _merged_sha_for(
    guard: DefaultBranchGuard | None, task_id: int, outcome: str,
) -> str | None:
    """The merged SHA to store with a ``done`` outcome, else None."""
    if guard is None or outcome not in ("tests_passed", "no_tests"):
        return None
    merge_outcome: MergeOutcome | None = guard.outcomes.get(task_id)
    if merge_outcome is None or not merge_outcome.done:
        return None
    return merge_outcome.merged_sha


async def _record_task_telemetry(
    task, result, outcome, cycles, args, *, merged_sha=None,
):
    """Post-task telemetry for the dev+test path: DB update / ForgeSmith
    recording, TheForge status verification, and the loop summary. Runs
    after the security gate so a blocked outcome is what gets persisted."""
    # Post-task telemetry (DB update, ForgeSmith recording, quality
    # scoring, reflexion, MemRL). Runs AFTER the security gate so a
    # ``security_review_blocked`` outcome is persisted to the task
    # row (rather than ``tests_passed``).
    task_role = task.get("role") or "developer"
    # S1 (2453): telemetry must survive a CircuitOpenError-demoted
    # outcome — no model was dispatched so log the sentinel
    # ``circuit_blocked`` rather than re-raising through bookkeeping.
    try:
        telemetry_model = get_role_model(task_role, args, task=task)
    except CircuitOpenError:
        telemetry_model = "circuit_blocked"
    await _post_task_telemetry(
        task, result, outcome, role=task_role,
        model=telemetry_model,
        max_turns=get_role_turns(task_role, args, task=task),
        cycle_number=cycles,
        dispatch_config=getattr(args, "dispatch_config", None),
        merged_sha=merged_sha)

    # Verify the task status in TheForge
    verified, verify_msg = verify_task_updated(task["id"])

    # Print loop summary
    print_dev_test_summary(task, result, cycles, outcome, verified, verify_msg)


@dataclass
class _SingleAgentRun:
    """One single-agent run: its result, outcome and the model/budget used."""

    result: dict
    outcome: str
    attempts: int
    model: str
    max_turns: int


async def _run_single_agent_mode(task, project_dir, project_context, args):
    """Single-agent mode (Phase 1 — with model tiering): dynamic budget,
    dispatch, outcome determination, the vacuous-pass / no-output guard
    (task #2371), telemetry and summary.

    Runs in ``project_dir`` as given; ``run_mode_task`` routes a git project
    through an isolation worktree instead (task #3112)."""
    run = await _execute_single_agent(task, project_dir, project_context, args)
    await _finish_single_agent(task, run, args)


async def _finish_single_agent(task, run: _SingleAgentRun, args, *, merged_sha=None):
    """Telemetry, TheForge status check and summary for a single-agent run."""
    await _post_task_telemetry(
        task, run.result, run.outcome, role=args.role,
        model=run.model, max_turns=run.max_turns,
        dispatch_config=getattr(args, "dispatch_config", None),
        merged_sha=merged_sha)

    # Verify the task status in TheForge
    verified, verify_msg = verify_task_updated(task["id"])

    # Print summary
    print_summary(task, run.result, verified, verify_msg)
    if run.attempts > 1:
        print(f"  Attempts: {run.attempts}/{args.retries}")


async def _execute_single_agent(task, project_dir, project_context, args) -> _SingleAgentRun:
    """Run the single agent in ``project_dir`` (the project checkout or the
    task's isolation worktree) and judge its outcome, including the
    no-output guard. Writes no status."""
    # Single-agent mode (Phase 1 — with model tiering)
    from equipa.role_resolver import is_role_early_term_exempt
    use_streaming = not is_role_early_term_exempt(args.role, project_dir)
    role_turns_max = get_role_turns(args.role, args, task=task)
    role_model = get_role_model(args.role, args, task=task)
    # Dynamic budget for single-agent mode
    role_turns_allocated, _ = calculate_dynamic_budget(role_turns_max)
    system_prompt = build_system_prompt(
        task, project_context, project_dir, role=args.role,
        dispatch_config=getattr(args, "dispatch_config", None),
        max_turns=role_turns_allocated,
    )
    print(f"Dynamic budget: {role_turns_allocated}/{role_turns_max} turns")
    # Mark the wall-clock run start so the no-output guard's filesystem
    # fallback can tell a fresh deliverable apart from pre-existing files
    # (the only output signal in a non-git project dir).
    from datetime import datetime as _dt
    run_started_at = _dt.now()
    with build_cli_command(
        system_prompt, project_dir, role_turns_allocated, role_model, role=args.role,
        streaming=use_streaming,
    ) as cmd:
        print(f"System prompt: {len(system_prompt)} chars, ~{estimate_tokens(system_prompt)} tokens")

        print(f"\nStarting {args.role} agent...")
        if use_streaming:
            # Streaming mode with early termination — no retries (kill is intentional)
            result = await run_agent_streaming(cmd, role=args.role)
            attempts = 1
        else:
            result, attempts = await run_agent_with_retries(cmd, task, args.retries)

    # Tag result with dynamic budget info for telemetry
    result["turns_allocated"] = role_turns_allocated
    result["turns_max"] = role_turns_max

    # Determine outcome. Sustained 529 is checked first so it is recorded as
    # the loud overloaded failure, never folded into a generic outcome.
    if is_overloaded_result(result):
        print(f"  [Task #{task['id']}] FAILED: model overloaded (529) through "
              f"every retry. Not downgrading the model.")
        single_outcome = OVERLOADED_OUTCOME
    elif result.get("early_terminated"):
        single_outcome = "early_terminated"
    elif result["success"]:
        single_outcome = "tests_passed"
    else:
        single_outcome = "developer_failed"

    # Vacuous-pass / no-output guard (task #2371).
    #
    # Single-agent runs (NOT --dev-test) bypassed the Dev+Test loop
    # vacuous-pass hook entirely, so a planner / code-reviewer run
    # that wrote zero files was happily marked SUCCESS and the task
    # row set to DONE (task #2361 on 2026-05-14 was the concrete
    # repro). We now require role-specific on-disk evidence before
    # accepting a single-agent run as ``tests_passed``.
    if single_outcome == "tests_passed":
        from equipa.single_agent_guard import (
            evaluate_single_agent_outcome,
            validate_tasks_created_claim,
        )
        outcome_check = evaluate_single_agent_outcome(
            role=args.role,
            task_id=task["id"],
            run_result=result,
            repo_path=Path(project_dir),
            run_started_at=run_started_at,
            project_dir=project_dir,
        )
        if outcome_check.is_blocked:
            print(
                f"  [no-output guard] Single-agent run produced no on-disk "
                f"evidence — downgrading to BLOCKED.\n"
                f"    role={args.role!r}, task=#{task['id']}\n"
                f"    reason: {outcome_check.reason}"
            )
            single_outcome = "no_output"
            result["no_output_reason"] = outcome_check.reason

        # Reject hallucinated TASKS_CREATED lines even when files
        # WERE written — the agent may still be lying about side-
        # effects (e.g. task #2361's "TASKS_CREATED: 78,79,80,81,82"
        # referenced unrelated pre-existing ForgeBridge tickets).
        if single_outcome == "tests_passed":
            try:
                from equipa.db import get_db_connection
                db_handle = _TasksCreatedDb(get_db_connection())
            except Exception:  # pragma: no cover — defensive
                db_handle = None
            if db_handle is not None:
                with db_handle:
                    tc_check = validate_tasks_created_claim(
                        stdout=result.get("stdout", "") or "",
                        run_started_at=result.get("started_at"),
                        expected_project_id=task.get("project_id"),
                        db=db_handle,
                    )
                if not tc_check.is_valid:
                    print(
                        f"  [no-output guard] Rejected hallucinated "
                        f"TASKS_CREATED claim: {tc_check.reason}"
                    )
                    single_outcome = "no_output"
                    result["tasks_created_rejection"] = tc_check.reason

    return _SingleAgentRun(
        result=result, outcome=single_outcome, attempts=attempts,
        model=role_model, max_turns=role_turns_max,
    )


# Report artifacts a single agent writes under .equipa-artifacts/ are
# gitignored, so they would vanish with its worktree; the task's own ones are
# copied back to the project checkout. SECURITY-REVIEW-* is excluded: only the
# orchestrator's reviewer run may write the gate's artifact.
_PRESERVED_ARTIFACT_MAX_BYTES = 1024 * 1024


def _preserve_task_artifacts(worktree_dir: str, project_dir: str, task_id: int) -> list[str]:
    """Copy this task's ``.equipa-artifacts`` files from its worktree back to
    the project checkout; returns the copied file names."""
    source_dir = Path(worktree_dir) / ".equipa-artifacts"
    if source_dir.is_symlink() or not source_dir.is_dir():
        return []
    task_name_re = re.compile(rf"-{int(task_id)}(?!\d)")
    copied: list[str] = []
    destination_dir: Path | None = None
    for source in sorted(source_dir.iterdir()):
        name = source.name
        if name.startswith("SECURITY-REVIEW") or not task_name_re.search(name):
            continue
        if source.is_symlink() or not source.is_file():
            continue
        if source.stat().st_size > _PRESERVED_ARTIFACT_MAX_BYTES:
            print(f"  [Isolation] NOT preserving {name}: larger than "
                  f"{_PRESERVED_ARTIFACT_MAX_BYTES} bytes")
            continue
        if destination_dir is None:
            destination_dir = ensure_artifacts_dir(project_dir)
        destination = destination_dir / name
        if destination.is_symlink():
            destination.unlink()
        shutil.copyfile(source, destination)
        copied.append(name)
    if copied:
        print(f"  [Isolation] Preserved task artifact(s) in the project "
              f"checkout: {', '.join(copied)}")
    return copied


async def _run_task_isolated(task, project_dir, project_context, args) -> None:
    """``--task`` / ``--project`` in a git project (dispatch-04/07).

    The Dev+Test loop (``--dev-test``) or the single agent runs in a
    ``forge-task-<id>`` worktree made by ``_create_isolation_worktrees``;
    ``run_task_in_isolation`` reviews the branch and merges it only through
    ``_gated_merge_task``, and the status written afterwards reflects the
    merge. A task refused before any agent ran (stale branch, leftover
    worktree, unpinnable default branch) is left blocked and the process
    exits with ``EXIT_DISPATCH_REFUSED``.
    """
    single_runs: list[_SingleAgentRun] = []

    async def execute(worktree_dir: str, task_branch: str) -> tuple[dict, int, str]:
        if args.dev_test:
            return await _run_dev_test_mode(
                task, worktree_dir, project_context, args, task_branch=task_branch,
            )
        run = await _execute_single_agent(task, worktree_dir, project_context, args)
        single_runs.append(run)
        try:
            _preserve_task_artifacts(worktree_dir, project_dir, task["id"])
        except OSError as exc:
            # Losing a report copy must not lose the run's gate and status.
            print(f"  [Isolation] WARNING: could not preserve task artifacts "
                  f"from {worktree_dir}: {exc}")
        return run.result, 1, run.outcome

    isolated: IsolatedTaskRun = await run_task_in_isolation(
        task, project_dir, project_context, args, execute=execute,
    )
    if isolated.outcome == "shutdown_requested":
        # Never started: the task keeps its status (dispatch-06).
        print(f"\nTask #{task['id']} NOT started: {isolated.reason}")
        exit_if_shutdown_requested()
        return
    if isolated.agent_outcome is None:
        update_task_status(task["id"], isolated.outcome)
        print(f"\nTask #{task['id']} NOT run: {isolated.reason}")
        sys.exit(EXIT_DISPATCH_REFUSED)
    if args.dev_test:
        await _record_task_telemetry(
            task, isolated.result, isolated.outcome, isolated.cycles, args,
            merged_sha=isolated.merged_sha,
        )
        return
    run = single_runs[-1]
    run.outcome = isolated.outcome
    await _finish_single_agent(task, run, args, merged_sha=isolated.merged_sha)


async def run_mode_task(args: argparse.Namespace) -> None:
    """Single-task or single-project mode (Phase 1 & 2).

    Thin orchestrator over the extracted phase helpers (task 2705): resolve
    the task, optionally dry-run, confirm, then dispatch to either the
    dev+test loop (with security gate and telemetry) or the single-agent
    path. Behaviour is identical to the pre-refactor monolith."""
    task, project_dir, project_context = _fetch_and_resolve_task(args)

    if args.dry_run:
        _run_task_dry_run(task, project_context, project_dir, args)
        return

    # Confirm before running
    print(f"\nDescription: {task.get('description', 'No description')[:200]}")
    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # --- Execute ---

    if _is_git_project(project_dir):
        # dispatch-04/07 (task #3112): in a git project the agent works in a
        # forge-task-<id> worktree and its commits reach the default branch
        # only through the gated merge — never by committing in the project
        # checkout.
        await report_leftover_dispatch_state(project_dir)
        await _run_task_isolated(task, project_dir, project_context, args)
        return

    # Not a git repo: there are no branches to protect or merge. R3166-01
    # (task #3168): no change check runs git in it for the rest of this
    # dispatch, the security review and the single-agent mode included.
    with dispatched_without_git(project_dir):
        if args.dev_test:
            # dispatch-03 (task #3111): pin the default branch before any agent
            # runs; only the gated merge below may move it.
            merge_guard = await _snapshot_merge_guard(project_dir)
            result, cycles, outcome = await _run_dev_test_mode(
                task, project_dir, project_context, args,
            )
            outcome = await _run_security_review_and_gate(
                task, project_dir, project_context, args, outcome,
                guard=merge_guard,
            )
            await _record_task_telemetry(
                task, result, outcome, cycles, args,
                merged_sha=_merged_sha_for(merge_guard, task["id"], outcome),
            )
        else:
            await _run_single_agent_mode(
                task, project_dir, project_context, args,
            )


# --- Mode Dispatcher ---

# Maps mode-detection predicate (callable on args) -> handler.
# Order matters: first matching predicate wins. The default --task/--project
# handler is selected last because args.task / args.project are always set
# when no other mode flag is present.
_MODE_DISPATCH: list[tuple[str, "callable"]] = [
    ("mcp_server", run_mode_mcp_server),
    ("regenerate_manifest", run_mode_regenerate_manifest),
    ("add_project", run_mode_add_project),
    ("setup_repos_or_project", run_mode_setup_repos),
    ("auto_run", run_mode_auto_run),
    ("parallel_goals", run_mode_parallel_goals),
    ("goal", run_mode_goal),
    ("tasks", run_mode_tasks),
    ("task_or_project", run_mode_task),
]


def _select_mode_handler(args: argparse.Namespace) -> "callable":
    """Pick the per-mode handler based on which mutually-exclusive flag is set.

    The argparse mutually-exclusive group guarantees exactly one mode flag is
    truthy, so this is a simple linear scan against the registry.
    """
    if args.mcp_server:
        return run_mode_mcp_server
    if getattr(args, "create_initiative", None):
        return run_mode_create_initiative
    if getattr(args, "list_initiatives", False):
        return run_mode_list_initiatives
    # S6: select on "is not None", not truthiness — otherwise --initiative 0
    # (a falsy but valid-shaped id) would silently fall through to another
    # mode (e.g. manifest regeneration) instead of running the initiative.
    if getattr(args, "initiative", None) is not None:
        return run_mode_initiative
    if getattr(args, "config_cmd", None):
        return run_mode_config
    if args.regenerate_manifest:
        return run_mode_regenerate_manifest
    if args.add_project:
        return run_mode_add_project
    if args.setup_repos or args.setup_repos_project:
        return run_mode_setup_repos
    if args.auto_run:
        return run_mode_auto_run
    if args.parallel_goals:
        return run_mode_parallel_goals
    if args.goal:
        return run_mode_goal
    if args.tasks:
        return run_mode_tasks
    # Default: --task or --project
    return run_mode_task


def warn_missing_dispatch_config(dispatch_config_arg: str | None) -> str | None:
    """The warning for a ``--dispatch-config`` path that does not exist
    (printed by async_main), or None. Such a run uses the defaults; agent
    isolation stays as the host config and marker have it (review F1)."""
    if dispatch_config_arg is None or Path(dispatch_config_arg).exists():
        return None
    return (f"WARNING: --dispatch-config '{dispatch_config_arg}' does not "
            f"exist; using the defaults (agent isolation stays as the host "
            f"config and marker have it).")


async def async_main() -> None:
    """Main entry point — parses args, validates, then dispatches to a mode handler."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Auto-detect non-TTY (nohup, SSH pipe, etc.) and default to --yes
    if not args.yes and not sys.stdin.isatty():
        args.yes = True
        print("Non-interactive mode detected (stdin is not a TTY). Auto-enabling --yes.")

    # Validate --goal requires --goal-project
    if args.goal and not args.goal_project:
        parser.error("--goal requires --goal-project <project_id>")

    # Warn if --dev-test combined with --role
    if args.dev_test and args.role != "developer":
        print(f"WARNING: --dev-test mode ignores --role ('{args.role}'). "
              f"Loop uses Developer + Tester automatically.")

    # Load dispatch config globally so model tiering and adaptive turns work in all modes
    missing_config = warn_missing_dispatch_config(args.dispatch_config)
    if missing_config:
        print(missing_config)
    args.dispatch_config = load_dispatch_config(args.dispatch_config)
    # Auxiliary model resolution (reflexion, RLM, ForgeSmith helpers) must use
    # THIS config — the one role resolution uses — never a CWD-relative file
    # (task #2994 S2).
    set_active_dispatch_config(args.dispatch_config)
    # Review F1 (task 3142): when the host requires agent isolation, a config
    # that turns it off refuses the run instead of dispatching unisolated.
    # Review F4: a UID pool would let isolated agents run side by side; it
    # is not implemented, so a config that names one is refused.
    isolation_refusal = (isolation_requirement_refusal(args.dispatch_config)
                         or uid_pool_refusal(args.dispatch_config))
    if isolation_refusal:
        refuse_dispatch(isolation_refusal)
    print(describe_isolation_state(args.dispatch_config))

    # --- Auth availability check (Max subscription OR API key) ---
    # Warn only when neither auth source is present. The Claude CLI accepts
    # either an ANTHROPIC_API_KEY env var OR a Max subscription credential
    # file at ~/.claude/.credentials.json.
    provider = (args.dispatch_config or {}).get("provider", "claude")
    if provider != "ollama" and args.provider != "ollama":
        has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        max_creds_path = Path.home() / ".claude" / ".credentials.json"
        has_max_creds = max_creds_path.is_file()
        if not has_api_key and not has_max_creds:
            print(
                "WARNING: No Claude credentials found.\n"
                "  Neither ANTHROPIC_API_KEY is set nor ~/.claude/.credentials.json exists.\n"
                "  Background/nohup processes do not source ~/.bashrc.\n"
                "  Fix one of:\n"
                "    - Sign in with `claude login` to use a Max subscription, OR\n"
                "    - Create a .env file in the EQUIPA project root with:\n"
                "        ANTHROPIC_API_KEY=sk-ant-...\n"
                "    - Or export it in /etc/environment for system-wide access."
            )

    # MI-04 (task #3116): pin the global git config before any mode can
    # dispatch an agent. Agents share the operator's UID and HOME, so every
    # orchestrator git call in this process must read the config as it was
    # before they ran. Not fatal here: the pre-dispatch merge guard retries
    # the pin and refuses to merge if it still fails.
    try:
        pin_global_git_config()
    except GlobalConfigPinError as exc:
        print(
            f"WARNING: the global git config could not be pinned ({exc}); "
            f"gated merges will be refused unless the pin succeeds later."
        )

    # Dispatch to the selected mode handler.
    handler = _select_mode_handler(args)
    await handler(args)


def _resolve_disabled_plugins(argv: list[str]) -> list[str]:
    """Build the list of plugin names to skip at startup.

    Precedence (highest first):
      1. CLI: ``--qiao on``  -> qiao enabled (not in disabled list)
      2. CLI: ``--qiao off`` -> qiao disabled
      3. ``dispatch_config.qiao_enabled`` value (true/false)
      4. Default if config absent or field missing: qiao disabled

    Runs before full argparse so we can decide which plugins to register
    BEFORE any hooks fire. Reads sys.argv directly and does a minimal
    JSON load of the dispatch config — heavier config plumbing happens
    later in async_main.
    """
    cli_choice: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--qiao" and i + 1 < len(argv):
            v = argv[i + 1].lower()
            if v in ("on", "off"):
                cli_choice = v
            break
        if a.startswith("--qiao="):
            v = a.split("=", 1)[1].lower()
            if v in ("on", "off"):
                cli_choice = v
            break
        i += 1

    if cli_choice == "on":
        return []
    if cli_choice == "off":
        return ["qiao"]

    cfg_path = os.environ.get("EQUIPA_DISPATCH_CONFIG") or "dispatch_config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # qiao_enabled may live at top level or under features (legacy layout).
        if cfg.get("qiao_enabled") is True:
            return []
        if isinstance(cfg.get("features"), dict) and cfg["features"].get("qiao_enabled") is True:
            return []
    except (OSError, json.JSONDecodeError):
        pass
    return ["qiao"]


def main() -> None:
    """Entry point that runs the async main.

    For --project mode, loops until no more todo tasks remain.
    For --task/--tasks mode, runs once and exits.
    """
    # Apply config, discover roles, and load plugins at startup
    load_config()
    _discover_roles()

    # Resolve which plugins to skip at startup. Default policy: skip the
    # qiao plugin unless dispatch_config.qiao_enabled is true OR --qiao on
    # was passed on the CLI. This keeps daily work isolated from the
    # experimental plugin while letting A/B benchmarks opt in per-invocation.
    disabled_plugins: list[str] = _resolve_disabled_plugins(sys.argv)
    load_plugins(_hooks_module, disabled=disabled_plugins)

    # 'equipa template <verb> ...' subcommand short-circuit. Handled here
    # so the existing mutually-exclusive flag group in async_main does not
    # need to learn about it.
    if len(sys.argv) >= 2 and sys.argv[1] == "template":
        sys.exit(_handle_template_subcommand(sys.argv[2:]))

    # Check sys.argv to determine if --project mode (no parse_args needed)
    is_project_mode = "--project" in sys.argv and "--task" not in sys.argv and "--tasks" not in sys.argv

    if is_project_mode:
        # --project mode: loop through all todo tasks
        task_count = 0
        while True:
            try:
                asyncio.run(async_main())
                # dispatch-06: a signal deferred during a merge ends the loop
                # here, after the run finished its merge and bookkeeping.
                exit_if_shutdown_requested()
                task_count += 1
                print(f"\n{'='*60}")
                print(f"Task complete ({task_count} so far). Checking for more...")
                print(f"{'='*60}\n")
            except SystemExit as e:
                if e.code == 0:
                    # Normal exit = no more tasks
                    project_name = ""
                    for i, arg in enumerate(sys.argv):
                        if arg == "--project" and i + 1 < len(sys.argv):
                            project_name = sys.argv[i + 1]
                            break
                    print(f"\nAll done! Completed {task_count} tasks for project {project_name}.")
                    break
                else:
                    # Error exit
                    print(f"\nOrchestrator exited with code {e.code} after {task_count} tasks.")
                    sys.exit(e.code)
            except KeyboardInterrupt:
                print(f"\nInterrupted after {task_count} tasks.")
                sys.exit(0)
            except Exception as e:
                print(f"\nError after {task_count} tasks: {e}")
                print("Stopping orchestrator loop.")
                sys.exit(1)
    else:
        # Single task or parallel tasks mode: run once
        asyncio.run(async_main())
        exit_if_shutdown_requested()
