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
import sqlite3
import sys
from pathlib import Path

import equipa.constants as _equipa_constants
from equipa.constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    EARLY_TERM_EXEMPT_ROLES,
    MAX_DEV_TEST_CYCLES,
    MAX_MANAGER_ROUNDS,
    MCP_CONFIG,
    PROJECT_DIRS,
    PROMPTS_DIR,
    THEFORGE_DB,
)
from equipa.agent_runner import build_cli_command, run_agent_streaming, run_agent_with_retries
from equipa.checkpoints import load_checkpoint
from equipa.db import record_agent_run, update_task_status
from equipa.dispatch import (
    apply_dispatch_filters,
    is_feature_enabled,
    load_dispatch_config,
    load_goals_file,
    parse_task_ids,
    run_auto_dispatch,
    run_parallel_goals,
    run_parallel_tasks,
    scan_pending_work,
    score_project,
    validate_goals,
)
from equipa.git_ops import setup_all_repos
from equipa.lessons import update_injected_episode_q_values_for_task
from equipa.loops import (
    run_dev_test_loop,
    run_quality_scoring,
    run_security_review,
)
from equipa.manager import run_manager_loop
from equipa.monitoring import calculate_dynamic_budget
from equipa.output import (
    log,
    print_dev_test_summary,
    print_dispatch_plan,
    print_manager_summary,
    print_summary,
)
from equipa.parsing import estimate_tokens
from equipa.prompts import build_planner_prompt, build_system_prompt
from equipa.reflexion import maybe_run_reflexion
from equipa.roles import _discover_roles, get_role_model, get_role_turns
from equipa.security import write_skill_manifest
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
        _equipa_constants.PROJECT_DIRS = resolved
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
) -> None:
    """Run all post-task telemetry: DB update, recording, scoring, reflexion, MemRL."""
    update_task_status(task["id"], outcome, output=output)
    record_agent_run(task, result, outcome, role=role, model=model,
                     max_turns=max_turns, cycle_number=cycle_number)
    if outcome in ("tests_passed", "no_tests"):
        run_quality_scoring(task, result, outcome, role=role, output=output,
                            dispatch_config=dispatch_config)
    await maybe_run_reflexion(task, result, outcome, role=role, output=output)
    update_injected_episode_q_values_for_task(task["id"], outcome, output=output)

    # Record model outcome for circuit breaker (cost routing)
    from equipa.routing import record_model_outcome
    success = outcome in ("tests_passed", "no_tests")
    record_model_outcome(model, success)


# --- Main Entry Points ---

async def async_main() -> None:
    """Main entry point."""
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
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help=f"Max agent turns (default: {DEFAULT_MAX_TURNS})")
    # Dynamically discover available roles from prompts directory
    prompts_dir = Path(__file__).parent.parent / "prompts"
    _available_roles = sorted([
        f.stem for f in prompts_dir.glob("*.md")
        if not f.name.startswith("_")
    ]) if prompts_dir.exists() else ["developer", "tester", "security-reviewer"]
    parser.add_argument("--role", default="developer", choices=_available_roles,
                        help=f"Agent role (available: {', '.join(_available_roles)}) (default: developer)")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help=f"Max retry attempts (default: {DEFAULT_MAX_RETRIES})")
    parser.add_argument("--dev-test", action="store_true", help="Enable Dev+Tester iteration loop mode")
    parser.add_argument("--security-review", action="store_true", default=None,
                        help="Run security review after dev-test passes (default: from dispatch config)")
    parser.add_argument("--provider", choices=["claude", "ollama"], default=None,
                        help="Force provider for all agents (default: from dispatch config)")
    parser.add_argument("--dry-run", action="store_true", help="Print command without executing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

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
    args.dispatch_config = load_dispatch_config(args.dispatch_config)

    # --- MCP server mode ---
    if args.mcp_server:
        from equipa.mcp_server import run_server
        run_server()
        return

    # --- Regenerate skill manifest mode ---
    if args.regenerate_manifest:
        write_skill_manifest()
        return

    # --- Add project mode ---
    if args.add_project:
        if not args.project_dir:
            parser.error("--add-project requires --project-dir <path>")
        _handle_add_project(args.add_project, args.project_dir)
        return

    # --- Setup repos mode (Phase 4B) ---
    if args.setup_repos or args.setup_repos_project:
        setup_all_repos(args)
        return

    # --- Auto-run mode (Phase 5) ---
    if args.auto_run:
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
        await run_auto_dispatch(work, dispatch_config, args)
        return

    # --- Parallel goals mode (Phase 4A) ---
    if args.parallel_goals:
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

        await run_parallel_goals(resolved_goals, defaults, args)
        return

    # --- Manager mode (Phase 3) ---
    if args.goal:
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

        # Show goal info
        print(f"\nGoal: {args.goal}")
        print(f"Project: {project_info.get('name', 'Unknown')} (ID: {args.goal_project})")
        print(f"Directory: {project_dir}")
        print(f"Model: {args.model}")
        print(f"Max turns/agent: {args.max_turns}")
        print(f"Max rounds: {args.max_rounds}")

        if args.dry_run:
            # Show what the planner prompt would look like
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

        # Print manager summary
        print_manager_summary(args.goal, outcome, rounds, completed, blocked, cost, duration)
        return

    # --- Parallel tasks mode ---
    if args.tasks:
        task_ids = parse_task_ids(args.tasks)
        if not task_ids:
            print("ERROR: Could not parse task IDs from --tasks argument.")
            sys.exit(1)

        if args.dry_run:
            tasks = fetch_tasks_by_ids(task_ids)
            print("\n--- DRY RUN (Parallel Tasks) ---")
            print(f"Tasks: {len(tasks)}")
            for t in tasks:
                print(f"  - #{t['id']}: {t['title']} ({t.get('project_name', '?')})")
            print("\n--- END DRY RUN ---")
            return

        await run_parallel_tasks(task_ids, args)
        return

    # --- Task/Project mode (Phase 1 & 2) ---

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

    # Resolve project directory
    project_dir = resolve_project_dir(task)
    if not project_dir:
        print(f"ERROR: Could not find project directory for '{task.get('project_name', 'Unknown')}'")
        print("Known projects:", ", ".join(sorted(_equipa_constants.PROJECT_DIRS.keys())))
        sys.exit(1)

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

    if args.dry_run:
        # Build a sample prompt to show size
        system_prompt = build_system_prompt(task, project_context, project_dir, role="developer",
                                                  dispatch_config=getattr(args, "dispatch_config", None))
        dry_model = get_role_model("developer", args, task=task)
        dry_turns = get_role_turns("developer", args, task=task)
        cmd = build_cli_command(system_prompt, project_dir, dry_turns, dry_model, role="developer")

        print("\n--- DRY RUN ---")
        print(f"System prompt: {len(system_prompt)} chars, ~{estimate_tokens(system_prompt)} tokens")
        print(f"Command ({len(cmd)} args):")
        for i, part in enumerate(cmd):
            if i > 0 and cmd[i - 1] == "--append-system-prompt":
                print(f"  [system prompt: {len(part)} chars]")
            elif len(part) > 100:
                print(f"  {part[:100]}...")
            else:
                print(f"  {part}")

        if args.dev_test:
            print(f"\nDev-Test loop would run up to {MAX_DEV_TEST_CYCLES} cycles.")
            print("Each cycle: Developer agent -> Tester agent -> feedback loop.")

        print("\n--- END DRY RUN ---")
        return

    # Confirm before running
    print(f"\nDescription: {task.get('description', 'No description')[:200]}")
    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # --- Execute ---

    if args.dev_test:
        # Dev+Tester iteration loop (Phase 2) with autoresearch retry
        print(f"\nStarting Dev+Test loop (max {MAX_DEV_TEST_CYCLES} cycles)...")

        # Autoresearch config
        dc = getattr(args, "dispatch_config", None) or {}
        autoresearch_on = is_feature_enabled(dc, "autoresearch")
        max_retries = dc.get("autoresearch_max_retries", 3) if autoresearch_on else 0
        retry_count = 0

        while True:
            result, cycles, outcome = await run_dev_test_loop(
                task, project_dir, project_context, args,
            )

            # Success - break out
            if outcome in ("tests_passed", "no_tests", "early_completed_no_changes"):
                break

            # Not retriable or exhausted
            if not autoresearch_on or retry_count >= max_retries:
                if retry_count > 0:
                    print(f"  [Autoresearch] Exhausted {retry_count}/{max_retries} retries "
                          f"for task #{task['id']}. Final outcome: {outcome}")
                break

            retry_count += 1
            print(f"  [Autoresearch] Task #{task['id']} failed ({outcome}). "
                  f"Retry {retry_count}/{max_retries}...")

            # Clean up failed git branch
            import subprocess as _sp
            branch_name = f"forge-task-{task['id']}"
            try:
                from equipa.git_ops import _is_git_repo
                if _is_git_repo(project_dir):
                    cp = _sp.run(["git", "rev-parse", "--verify", "main"],
                                 cwd=project_dir, capture_output=True)
                    default_branch = "main" if cp.returncode == 0 else "master"
                    _sp.run(["git", "checkout", default_branch],
                            cwd=project_dir, capture_output=True)
                    _sp.run(["git", "branch", "-D", branch_name],
                            cwd=project_dir, capture_output=True)
                    print(f"  [Autoresearch] Cleaned up branch {branch_name}")
            except Exception as e:
                print(f"  [Autoresearch] Git cleanup warning: {e}")

            # Reset task to todo
            from equipa.db import get_db_connection
            conn = get_db_connection(write=True)
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task["id"],))
            conn.commit()
            conn.close()
            print(f"  [Autoresearch] Reset task #{task['id']} to todo for fresh attempt")

        # Post-task telemetry (DB update, ForgeSmith recording, quality scoring, reflexion, MemRL)
        task_role = task.get("role") or "developer"
        await _post_task_telemetry(
            task, result, outcome, role=task_role,
            model=get_role_model(task_role, args, task=task),
            max_turns=get_role_turns(task_role, args, task=task),
            cycle_number=cycles,
            dispatch_config=getattr(args, "dispatch_config", None))

        # Optional security review after successful dev-test
        # CLI --security-review flag takes precedence, then dispatch config top-level key,
        # then features.security_review flag (all must agree for review to run)
        dc = getattr(args, "dispatch_config", None) or {}
        security_review_enabled = args.security_review
        if security_review_enabled is None:
            security_review_enabled = dc.get("security_review", False)
        # Feature flag can disable even if top-level key is True
        if not is_feature_enabled(dc, "security_review"):
            security_review_enabled = False

        if security_review_enabled and outcome in ("tests_passed", "no_tests"):
            await run_security_review(task, project_dir, project_context, args)

        # Verify the task status in TheForge
        verified, verify_msg = verify_task_updated(task["id"])

        # Print loop summary
        print_dev_test_summary(task, result, cycles, outcome, verified, verify_msg)

    else:
        # Single-agent mode (Phase 1 — with model tiering)
        use_streaming = args.role not in EARLY_TERM_EXEMPT_ROLES
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
        cmd = build_cli_command(
            system_prompt, project_dir, role_turns_allocated, role_model, role=args.role,
            streaming=use_streaming,
        )
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

        # Determine outcome
        if result.get("early_terminated"):
            single_outcome = "early_terminated"
        elif result["success"]:
            single_outcome = "tests_passed"
        else:
            single_outcome = "developer_failed"

        # Post-task telemetry
        await _post_task_telemetry(
            task, result, single_outcome, role=args.role,
            model=role_model, max_turns=role_turns_max,
            dispatch_config=getattr(args, "dispatch_config", None))

        # Verify the task status in TheForge
        verified, verify_msg = verify_task_updated(task["id"])

        # Print summary
        print_summary(task, result, verified, verify_msg)
        if attempts > 1:
            print(f"  Attempts: {attempts}/{args.retries}")


def main() -> None:
    """Entry point that runs the async main.

    For --project mode, loops until no more todo tasks remain.
    For --task/--tasks mode, runs once and exits.
    """
    # Apply config and discover roles at startup
    load_config()
    _discover_roles()

    # Check sys.argv to determine if --project mode (no parse_args needed)
    is_project_mode = "--project" in sys.argv and "--task" not in sys.argv and "--tasks" not in sys.argv

    if is_project_mode:
        # --project mode: loop through all todo tasks
        task_count = 0
        while True:
            try:
                asyncio.run(async_main())
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
