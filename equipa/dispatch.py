"""EQUIPA dispatch module — auto-run scanning, scoring, filtering, and execution.

Extracts dispatch/auto-run logic from forge_orchestrator.py (Phase 5 split).
Includes: scan_pending_work, score_project, apply_dispatch_filters,
run_project_tasks, run_project_dispatch, run_auto_dispatch,
run_parallel_tasks, run_single_goal, run_parallel_goals,
parse_task_ids, load_goals_file, validate_goals,
load_dispatch_config, is_feature_enabled, DEFAULT_DISPATCH_CONFIG,
DEFAULT_FEATURE_FLAGS.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from equipa.constants import (
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    MAX_MANAGER_ROUNDS,
    PRIORITY_ORDER,
    PROJECT_DIRS,
)
from equipa.db import (
    get_db_connection,
    record_agent_run,
    update_task_status,
)
from equipa.git_ops import _is_git_repo
from equipa.lessons import update_injected_episode_q_values_for_task
from equipa.loops import (
    run_dev_test_loop,
    run_quality_scoring,
)
from equipa.manager import run_manager_loop
from equipa.output import (
    log,
    print_dispatch_plan,
    print_dispatch_summary,
    print_manager_summary,
    print_parallel_summary,
)
from equipa.prompts import build_planner_prompt
from equipa.reflexion import maybe_run_reflexion
from equipa.roles import get_role_model, get_role_turns
from equipa.tasks import (
    fetch_project_context,
    fetch_project_info,
    fetch_task,
    fetch_tasks_by_ids,
    resolve_project_dir,
)


# --- Feature Flags ---

DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "language_prompts": True,
    "hooks": False,
    "mcp_health": False,
    "forgesmith_lessons": True,
    "forgesmith_episodes": True,
    "gepa_ab_testing": False,
    "security_review": True,
    "quality_scoring": True,
    "anti_compaction_state": True,
}

DEFAULT_DISPATCH_CONFIG: dict = {
    "max_concurrent": 8,
    "model": "sonnet",
    "max_turns": 25,
    "max_tasks_per_project": 3,
    "skip_projects": [],
    "priority_boost": {},
    "only_projects": [],
    "security_review": False,
    "features": dict(DEFAULT_FEATURE_FLAGS),
}


def is_feature_enabled(dispatch_config: dict | None, feature_name: str) -> bool:
    """Check if a feature flag is enabled.

    Reads from dispatch_config["features"][feature_name]. Falls back to
    DEFAULT_FEATURE_FLAGS if the feature is not in the config.

    Returns True/False. Unknown features default to False.
    """
    if dispatch_config is None:
        return DEFAULT_FEATURE_FLAGS.get(feature_name, False)
    features = dispatch_config.get("features", {})
    return features.get(feature_name, DEFAULT_FEATURE_FLAGS.get(feature_name, False))


def load_dispatch_config(filepath: str | Path | None) -> dict:
    """Load dispatch_config.json preferences.

    Returns a config dict with defaults for any missing keys.
    Falls back to defaults entirely if file not found.
    """
    config = dict(DEFAULT_DISPATCH_CONFIG)

    if filepath is None:
        # Try default location — use the script's parent directory
        # (forge_orchestrator.py lives alongside dispatch_config.json)
        import equipa.constants as _c
        filepath = Path(_c.THEFORGE_DB).parent / "dispatch_config.json"
        if not filepath.exists():
            # Fall back to CWD-relative
            filepath = Path("dispatch_config.json")
    else:
        filepath = Path(filepath)

    if not filepath.exists():
        return config

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Could not load dispatch config '{filepath}': {e}")
        print("  Using defaults.")
        return config

    # Merge loaded values over defaults
    for key in DEFAULT_DISPATCH_CONFIG:
        if key in data:
            config[key] = data[key]

    # Deep-merge features: user's partial features dict is overlaid on defaults
    # so specifying e.g. {"features": {"hooks": true}} does not wipe other flags.
    if "features" in data and isinstance(data["features"], dict):
        merged_features = dict(DEFAULT_FEATURE_FLAGS)
        merged_features.update(data["features"])
        config["features"] = merged_features

    # Also merge any extra keys not in defaults (model_developer, model_epic, etc.)
    for key in data:
        if key not in config:
            config[key] = data[key]

    return config


# --- DB Scanning & Scoring ---

def scan_pending_work() -> list[dict]:
    """Query DB for all projects with todo tasks, grouped by priority.

    Returns a list of dicts:
    [
        {
            "project_id": 21,
            "project_name": "EQUIPA",
            "codename": "equipa",
            "status": "active",
            "tasks": [<task dicts sorted by priority>],
            "counts": {"critical": 0, "high": 2, "medium": 1, "low": 0},
            "total_todo": 3,
        },
        ...
    ]
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.title, t.description, t.priority, t.project_id,
                   p.name as project_name,
                   COALESCE(p.codename, LOWER(REPLACE(p.name, ' ', ''))) as codename,
                   p.status as project_status
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'todo'
            ORDER BY t.project_id, t.created_at ASC
            """,
        ).fetchall()

        # Group by project
        projects: dict[int, dict] = {}
        for row in rows:
            row = dict(row)
            pid = row["project_id"]
            if pid not in projects:
                projects[pid] = {
                    "project_id": pid,
                    "project_name": row["project_name"],
                    "codename": row["codename"],
                    "status": (row.get("project_status") or "unknown").lower(),
                    "tasks": [],
                    "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                    "total_todo": 0,
                }
            projects[pid]["tasks"].append(row)
            projects[pid]["total_todo"] += 1
            priority = str(row.get("priority", "low")).lower()
            if priority in projects[pid]["counts"]:
                projects[pid]["counts"][priority] += 1

        # Sort tasks within each project by priority descending
        for proj in projects.values():
            proj["tasks"].sort(
                key=lambda t: PRIORITY_ORDER.get(
                    str(t.get("priority", "low")).lower(), 0
                ),
                reverse=True,
            )

        return list(projects.values())
    finally:
        conn.close()


def score_project(summary: dict, config: dict) -> int:
    """Score a project for dispatch priority.

    score = (critical*10) + (high*5) + (medium*2) + (low*1)
           + 3 if project status is 'active'
           + priority_boost from config
    """
    counts = summary["counts"]
    score = (
        counts.get("critical", 0) * 10
        + counts.get("high", 0) * 5
        + counts.get("medium", 0) * 2
        + counts.get("low", 0) * 1
    )

    if summary.get("status") == "active":
        score += 3

    # Apply manual boost from config
    codename = summary.get("codename", "").lower()
    boost = config.get("priority_boost", {})
    if codename in boost:
        score += boost[codename]
    # Also check by project_id string
    pid_str = str(summary.get("project_id", ""))
    if pid_str in boost:
        score += boost[pid_str]

    summary["score"] = score
    return score


# --- Config Loading & Filters ---

def apply_dispatch_filters(work: list[dict], config: dict, args) -> list[dict]:
    """Apply skip_projects, only_projects, and --only-project filters.

    Returns filtered list of project summaries.
    """
    filtered = list(work)

    # --only-project CLI args take highest priority
    cli_only = getattr(args, "only_project", None) or []
    if cli_only:
        cli_only_set = set(cli_only)
        filtered = [p for p in filtered if p["project_id"] in cli_only_set]
        return filtered

    # Config-level only_projects (whitelist mode)
    config_only = config.get("only_projects", [])
    if config_only:
        only_set: set[int] = set()
        for item in config_only:
            if isinstance(item, int):
                only_set.add(item)
            elif isinstance(item, str):
                # Match by codename
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        only_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] in only_set]
        return filtered

    # Config-level skip_projects
    skip_list = config.get("skip_projects", [])
    if skip_list:
        skip_set: set[int] = set()
        for item in skip_list:
            if isinstance(item, int):
                skip_set.add(item)
            elif isinstance(item, str):
                for p in filtered:
                    if p.get("codename", "").lower() == item.lower():
                        skip_set.add(p["project_id"])
        filtered = [p for p in filtered if p["project_id"] not in skip_set]

    return filtered


# --- Per-Project Task Runner ---

async def run_project_tasks(
    project_summary: dict,
    config: dict,
    args,
    output: list[str] | None = None,
) -> dict:
    """Run Dev+Test loops on todo tasks for one project, in priority order.

    Returns a dict with results per task.
    """
    project_id = project_summary["project_id"]
    codename = project_summary.get("codename", "unknown")
    tasks = project_summary["tasks"]

    # Apply max_tasks_per_project cap
    max_tasks = getattr(args, "max_tasks_per_project", None)
    if max_tasks is None:
        max_tasks = config.get("max_tasks_per_project", 5)
    if len(tasks) > max_tasks:
        log(f"  [{codename}] Capping to {max_tasks} tasks (of {len(tasks)} todo)", output)
        tasks = tasks[:max_tasks]

    # Resolve project directory
    codename_lower = codename.lower().strip()
    project_dir = PROJECT_DIRS.get(codename_lower)
    if not project_dir:
        log(f"  [{codename}] ERROR: No directory mapped. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "No directory mapped",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    if not Path(project_dir).exists():
        log(f"  [{codename}] ERROR: Directory does not exist: {project_dir}. Skipping.", output)
        return {
            "project_id": project_id,
            "codename": codename,
            "tasks_attempted": 0,
            "tasks_completed": [],
            "tasks_blocked": [],
            "tasks_skipped": len(tasks),
            "error": "Directory does not exist",
            "total_cost": 0.0,
            "total_duration": 0.0,
        }

    project_context = fetch_project_context(project_id)

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    # Build args namespace for dev-test loop
    task_args = argparse.Namespace(
        model=config.get("model", args.model),
        max_turns=config.get("max_turns", args.max_turns),
        dispatch_config=config,  # pass config so get_role_turns can read per-role limits
    )

    for i, task_row in enumerate(tasks, 1):
        task_id = task_row["id"]
        log(f"\n  [{codename}] Task {i}/{len(tasks)}: #{task_id} - {task_row['title']}", output)

        # Re-fetch task to get full data with project info
        task = fetch_task(task_id)
        if not task:
            log(f"  [{codename}] Task #{task_id} not found in DB. Skipping.", output)
            continue

        # Check if still todo
        if task.get("status") != "todo":
            log(f"  [{codename}] Task #{task_id} status is '{task.get('status')}'. Skipping.", output)
            continue

        result, cycles, outcome = await run_dev_test_loop(
            task, project_dir, project_context, task_args, output=output,
        )
        total_duration += result.get("duration", 0)
        if result.get("cost"):
            total_cost += result["cost"]

        # Orchestrator-side DB update (don't rely on agent)
        update_task_status(task_id, outcome, output=output)

        # ForgeSmith telemetry
        task_role = task.get("role") or "developer"
        record_agent_run(
            task, result, outcome, role=task_role,
            model=get_role_model(task_role, task_args, task=task),
            max_turns=get_role_turns(task_role, task_args, task=task),
            cycle_number=cycles, output=output,
        )

        # Post-task quality scoring (on success only)
        if outcome in ("tests_passed", "no_tests"):
            run_quality_scoring(task, result, outcome, role=task_role, output=output,
                                dispatch_config=config)

        # Reflexion: record episode and capture self-reflection
        await maybe_run_reflexion(task, result, outcome, role=task_role, output=output)

        # MemRL: update q_values of episodes that were injected into this task's prompt
        update_injected_episode_q_values_for_task(task_id, outcome, output=output)

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            log(f"  [{codename}] Task #{task_id}: COMPLETED ({outcome})", output)
        else:
            blocked.append(task)
            log(f"  [{codename}] Task #{task_id}: BLOCKED ({outcome})", output)

    return {
        "project_id": project_id,
        "codename": codename,
        "tasks_attempted": len(tasks),
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": 0,
        "error": None,
        "total_cost": total_cost,
        "total_duration": total_duration,
    }


async def run_project_dispatch(
    project_summary: dict,
    semaphore: asyncio.Semaphore,
    config: dict,
    args,
) -> dict:
    """Wrapper for concurrent execution of one project's tasks.

    Acquires semaphore slot, runs tasks, returns result with buffered output.
    """
    codename = project_summary.get("codename", "unknown")
    output: list[str] = []

    log(f"\n[{codename}] Queued ({project_summary['total_todo']} todo tasks, "
        f"score: {project_summary.get('score', '?')})", output)

    async with semaphore:
        log(f"[{codename}] Acquired slot, starting...", output)

        try:
            result = await run_project_tasks(
                project_summary, config, args, output=output,
            )
        except Exception as e:
            log(f"[{codename}] EXCEPTION: {e}", output)
            result = {
                "project_id": project_summary["project_id"],
                "codename": codename,
                "tasks_attempted": 0,
                "tasks_completed": [],
                "tasks_blocked": [],
                "tasks_skipped": project_summary["total_todo"],
                "error": str(e),
                "total_cost": 0.0,
                "total_duration": 0.0,
            }

    result["output"] = output
    return result


async def run_auto_dispatch(scored: list[dict], config: dict, args) -> None:
    """Run all project dispatches concurrently with semaphore.

    Prints each project's buffered output as it completes, then summary.
    """
    max_concurrent = config.get("max_concurrent", 4)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nDispatching {len(scored)} projects "
          f"(max {max_concurrent} concurrent)...\n")

    coros = [
        run_project_dispatch(proj, semaphore, config, args)
        for proj in scored
    ]

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Print each project's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  PROJECT EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: {r.get('codename', '?')}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_dispatch_summary(results)


# --- Goals ---

def load_goals_file(filepath: str | Path) -> tuple[dict, list[dict]]:
    """Parse and validate a goals JSON file.

    Returns (defaults_dict, goals_list) tuple.
    Exits with error on invalid input.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"ERROR: Goals file not found: {filepath}")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in goals file: {e}")
        sys.exit(1)

    if "goals" not in data or not isinstance(data["goals"], list):
        print("ERROR: Goals file must contain a 'goals' array")
        sys.exit(1)

    if not data["goals"]:
        print("ERROR: Goals array is empty")
        sys.exit(1)

    # Extract defaults
    defaults = {
        "max_concurrent": data.get("max_concurrent", 4),
        "model": data.get("model", DEFAULT_MODEL),
        "max_turns": data.get("max_turns", DEFAULT_MAX_TURNS),
        "max_rounds": data.get("max_rounds", MAX_MANAGER_ROUNDS),
    }

    # Validate each goal
    for i, g in enumerate(data["goals"]):
        if "goal" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'goal' field")
            sys.exit(1)
        if "project_id" not in g:
            print(f"ERROR: Goal #{i + 1} missing 'project_id' field")
            sys.exit(1)

    return defaults, data["goals"]


def validate_goals(goals: list[dict]) -> list[dict]:
    """Validate goals: check project_ids exist, dirs exist, no duplicates.

    Returns list of resolved goal dicts with project_dir and project_info added.
    Exits with error on validation failure.
    """
    # Check for duplicate project_ids
    project_ids = [g["project_id"] for g in goals]
    seen: set[int] = set()
    for pid in project_ids:
        if pid in seen:
            print(f"ERROR: Duplicate project_id {pid} in goals file. "
                  f"Two goals cannot target the same project (they'd write to the same directory).")
            sys.exit(1)
        seen.add(pid)

    resolved = []
    for i, g in enumerate(goals):
        project_info = fetch_project_info(g["project_id"])
        if not project_info:
            print(f"ERROR: Goal #{i + 1}: Project {g['project_id']} not found in TheForge")
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(f"ERROR: Goal #{i + 1}: No directory mapped for project "
                  f"'{project_info.get('name', 'Unknown')}'")
            sys.exit(1)

        if not Path(project_dir).exists():
            print(f"ERROR: Goal #{i + 1}: Directory does not exist: {project_dir}")
            sys.exit(1)

        resolved.append({
            **g,
            "project_dir": project_dir,
            "project_info": project_info,
        })

    return resolved


async def run_single_goal(
    goal_entry: dict,
    semaphore: asyncio.Semaphore,
    index: int,
    defaults: dict,
    args,
) -> dict:
    """Run a single Manager loop for one goal, respecting the semaphore.

    Returns a result dict with goal info and outcome.
    """
    goal_text = goal_entry["goal"]
    project_id = goal_entry["project_id"]
    project_dir = goal_entry["project_dir"]
    project_name = goal_entry["project_info"].get("name", "Unknown")

    # Per-goal overrides or defaults
    model = goal_entry.get("model", defaults["model"])
    max_turns = goal_entry.get("max_turns", defaults["max_turns"])
    max_rounds = goal_entry.get("max_rounds", defaults["max_rounds"])

    # Create a namespace that looks like args for the manager loop
    goal_args = argparse.Namespace(
        model=model,
        max_turns=max_turns,
        max_rounds=max_rounds,
    )

    output: list[str] = []  # Buffer all output for this goal
    log(f"\n[Goal {index + 1}] {goal_text}", output)
    log(f"  Project: {project_name} (ID: {project_id})", output)
    log(f"  Directory: {project_dir}", output)
    log(f"  Model: {model}, Max turns: {max_turns}, Max rounds: {max_rounds}", output)

    async with semaphore:
        log(f"\n[Goal {index + 1}] Acquired slot, starting...", output)
        project_context = fetch_project_context(project_id)

        try:
            outcome, rounds, completed, blocked, cost, duration = await run_manager_loop(
                goal_text, project_id, project_dir, project_context,
                goal_args, output=output,
            )
        except Exception as e:
            log(f"\n[Goal {index + 1}] EXCEPTION: {e}", output)
            return {
                "index": index,
                "goal": goal_text,
                "project_name": project_name,
                "project_id": project_id,
                "outcome": "exception",
                "error": str(e),
                "rounds": 0,
                "completed": [],
                "blocked": [],
                "cost": 0.0,
                "duration": 0.0,
                "output": output,
            }

        print_manager_summary(
            goal_text, outcome, rounds, completed, blocked, cost, duration,
            output=output,
        )

    return {
        "index": index,
        "goal": goal_text,
        "project_name": project_name,
        "project_id": project_id,
        "outcome": outcome,
        "rounds": rounds,
        "completed": completed,
        "blocked": blocked,
        "cost": cost,
        "duration": duration,
        "output": output,
    }


async def run_parallel_goals(resolved_goals: list[dict], defaults: dict, args) -> None:
    """Run multiple Manager loops concurrently with a semaphore.

    Prints each goal's buffered output as it completes, then a combined summary.
    """
    max_concurrent = args.max_concurrent or defaults["max_concurrent"]
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nStarting {len(resolved_goals)} parallel goals "
          f"(max {max_concurrent} concurrent)...\n")

    # Launch all goals
    tasks = [
        run_single_goal(g, semaphore, i, defaults, args)
        for i, g in enumerate(resolved_goals)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Print each goal's buffered output
    for r in results:
        if isinstance(r, Exception):
            print(f"\n{'!' * 60}")
            print(f"  GOAL EXCEPTION: {r}")
            print(f"{'!' * 60}")
            continue

        print(f"\n{'=' * 60}")
        print(f"  OUTPUT: Goal {r['index'] + 1} — {r['project_name']}")
        print(f"{'=' * 60}")
        for line in r.get("output", []):
            print(line)

    # Print combined summary
    print_parallel_summary(results)


# --- Parallel Tasks ---

def parse_task_ids(task_str: str) -> list[int]:
    """Parse comma-separated IDs or ranges into a list of ints.

    Examples: "109,110,111" -> [109, 110, 111]
              "109-114" -> [109, 110, 111, 112, 113, 114]
              "109,112-114" -> [109, 112, 113, 114]
    """
    ids: list[int] = []
    for part in task_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            ids.extend(range(int(start), int(end) + 1))
        else:
            ids.append(int(part))
    return ids


async def run_parallel_tasks(task_ids: list[int], args) -> None:
    """Run multiple tasks concurrently with dev-test loops.

    All tasks must belong to the same project (for safety).
    """
    # Fetch all tasks
    tasks = fetch_tasks_by_ids(task_ids)
    if not tasks:
        print("ERROR: No tasks found for given IDs.")
        return

    # Verify all tasks are from the same project
    project_ids = set(t.get("project_id") for t in tasks)
    if len(project_ids) > 1:
        print(f"ERROR: --tasks requires all tasks from the same project. "
              f"Found project IDs: {project_ids}")
        return

    project_id = tasks[0].get("project_id")
    project_dir = resolve_project_dir(tasks[0])
    if not project_dir:
        print("ERROR: Could not resolve project directory.")
        return
    if not Path(project_dir).exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        return

    project_context = fetch_project_context(project_id)
    max_concurrent = getattr(args, "max_concurrent", None) or 4
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\nParallel task execution: {len(tasks)} tasks, max {max_concurrent} concurrent")
    for t in tasks:
        print(f"  - #{t['id']}: {t['title']}")

    if not args.yes:
        response = input("\nProceed? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Create per-task git worktrees for filesystem isolation
    worktree_dirs: dict[int, str] = {}
    worktree_base = Path(project_dir) / ".forge-worktrees"
    use_worktrees = len(tasks) > 1 and _is_git_repo(project_dir)

    if use_worktrees:
        worktree_base.mkdir(exist_ok=True)
        for t in tasks:
            branch_name = f"forge-task-{t['id']}"
            wt_path = worktree_base / f"task-{t['id']}"
            try:
                # Clean up stale worktree if exists
                if wt_path.exists():
                    subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)],
                                   cwd=project_dir, capture_output=True)
                # Create worktree from current HEAD
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
                    cwd=project_dir, capture_output=True, check=True,
                )
                worktree_dirs[t["id"]] = str(wt_path)
                print(f"  [Isolation] Task #{t['id']} -> {wt_path.name}")
            except subprocess.CalledProcessError:
                # Fallback: if worktree creation fails, try without -b (branch may exist)
                try:
                    subprocess.run(["git", "branch", "-D", branch_name],
                                   cwd=project_dir, capture_output=True)
                    subprocess.run(
                        ["git", "worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
                        cwd=project_dir, capture_output=True, check=True,
                    )
                    worktree_dirs[t["id"]] = str(wt_path)
                    print(f"  [Isolation] Task #{t['id']} -> {wt_path.name} (retry)")
                except Exception:
                    print(f"  [Isolation] WARNING: Could not create worktree for task #{t['id']}, using shared dir")

    async def run_one_task(task):
        output = []
        # Use worktree if available, otherwise shared project_dir
        task_dir = worktree_dirs.get(task["id"], project_dir)
        async with semaphore:
            log(f"\n[Task #{task['id']}] Starting: {task['title']}", output)
            result, cycles, outcome = await run_dev_test_loop(
                task, task_dir, project_context, args, output=output,
            )
            update_task_status(task["id"], outcome, output=output)
            log(f"[Task #{task['id']}] Done: {outcome} ({cycles} cycles)", output)
            # Record telemetry
            task_role = task.get("role") or "developer"
            record_agent_run(
                task, result, outcome, role=task_role,
                model=get_role_model(task_role, args, task=task),
                max_turns=get_role_turns(task_role, args, task=task),
                cycle_number=cycles, output=output,
            )

            # Mark for post-gather sequential merge (avoid parallel merge conflicts)
            merge_ok = False
            needs_merge = task["id"] in worktree_dirs and outcome in ("tests_passed", "no_tests")

            return {
                "task": task,
                "result": result,
                "cycles": cycles,
                "outcome": outcome,
                "output": output,
                "merge_ok": merge_ok,
                "needs_merge": needs_merge,
            }

    results = await asyncio.gather(
        *[run_one_task(t) for t in tasks],
        return_exceptions=True,
    )

    # Print results
    print(f"\n{'#' * 60}")
    print("PARALLEL TASKS SUMMARY")
    print(f"{'#' * 60}")

    completed = []
    blocked = []
    total_cost = 0.0
    total_duration = 0.0

    for r in results:
        if isinstance(r, Exception):
            print(f"\n  EXCEPTION: {r}")
            continue

        task = r["task"]
        outcome = r["outcome"]
        result = r["result"]

        # Print buffered output
        for line in r.get("output", []):
            print(line)

        cost = result.get("cost", 0) or 0
        duration = result.get("duration", 0)
        total_cost += cost
        total_duration += duration

        if outcome in ("tests_passed", "no_tests"):
            completed.append(task)
            print(f"\n  #{task['id']}: COMPLETED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")
        else:
            blocked.append(task)
            print(f"\n  #{task['id']}: BLOCKED ({outcome}, {r['cycles']} cycles, {duration:.0f}s)")

    print(f"\nTotal: {len(completed)} completed, {len(blocked)} blocked")
    print(f"Duration: {total_duration:.0f}s total")
    if total_cost > 0:
        print(f"Cost: ${total_cost:.4f}")
    print(f"{'#' * 60}")

    # Sequential merge — merge task branches one at a time to avoid conflicts
    merged_tasks_seq: set[int] = set()
    if use_worktrees:
        merge_candidates = []
        for r in results:
            if isinstance(r, Exception):
                continue
            if r.get("needs_merge", False) or (r["task"]["id"] in worktree_dirs and r["outcome"] in ("tests_passed", "no_tests")):
                merge_candidates.append(r)

        for r in merge_candidates:
            task_id = r["task"]["id"]
            branch_name = f"forge-task-{task_id}"
            try:
                # Ensure we're on the default branch in the MAIN repo
                current_branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=project_dir, capture_output=True, text=True,
                ).stdout.strip()
                if not current_branch:
                    for candidate in ["master", "main"]:
                        check = subprocess.run(
                            ["git", "show-ref", "--verify", f"refs/heads/{candidate}"],
                            cwd=project_dir, capture_output=True,
                        )
                        if check.returncode == 0:
                            subprocess.run(["git", "checkout", candidate],
                                           cwd=project_dir, capture_output=True)
                            current_branch = candidate
                            break
                print(f"  [Isolation] Merging on branch: {current_branch} in {project_dir}")

                # Capture HEAD before merge to verify it changed
                pre_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_dir, capture_output=True, text=True,
                ).stdout.strip()

                # Check branch has commits ahead of HEAD
                ahead = subprocess.run(
                    ["git", "log", "--oneline", f"HEAD..{branch_name}"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                if not ahead.stdout.strip():
                    print(f"  [Isolation] Task #{task_id}: branch '{branch_name}' has NO commits ahead of HEAD — skipping merge")
                    continue

                commits_ahead = len(ahead.stdout.strip().split(chr(10)))
                print(f"  [Isolation] Task #{task_id}: branch '{branch_name}' has {commits_ahead} commit(s) to merge")

                # Stash any uncommitted changes
                stash_result = subprocess.run(
                    ["git", "stash"], cwd=project_dir, capture_output=True, text=True,
                )
                had_stash = "Saved working directory" in stash_result.stdout

                merge_result = subprocess.run(
                    ["git", "merge", "--no-edit", branch_name],
                    cwd=project_dir, capture_output=True, text=True,
                )

                # Verify HEAD actually changed
                post_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_dir, capture_output=True, text=True,
                ).stdout.strip()

                if merge_result.returncode == 0 and post_head != pre_head:
                    print(f"  [Isolation] Merged task #{task_id} into main ({pre_head[:8]} -> {post_head[:8]})")
                    r["merge_ok"] = True
                    merged_tasks_seq.add(task_id)
                elif merge_result.returncode == 0 and post_head == pre_head:
                    print(f"  [Isolation] WARNING: Merge returned 0 for task #{task_id} but HEAD unchanged ({pre_head[:8]})")
                    print(f"  [Isolation] Merge stdout: {merge_result.stdout[:200]}")
                else:
                    # Try rebase-then-merge for conflicts
                    subprocess.run(["git", "merge", "--abort"],
                                   cwd=project_dir, capture_output=True)
                    # Attempt rebase
                    rebase_result = subprocess.run(
                        ["git", "rebase", "HEAD", branch_name],
                        cwd=project_dir, capture_output=True, text=True,
                    )
                    if rebase_result.returncode == 0:
                        # Try merge again after rebase
                        merge2 = subprocess.run(
                            ["git", "merge", "--no-edit", branch_name],
                            cwd=project_dir, capture_output=True, text=True,
                        )
                        if merge2.returncode == 0:
                            print(f"  [Isolation] Merged task #{task_id} (after rebase)")
                            r["merge_ok"] = True
                            merged_tasks_seq.add(task_id)
                        else:
                            subprocess.run(["git", "merge", "--abort"],
                                           cwd=project_dir, capture_output=True)
                            print(f"  [Isolation] Merge FAILED for task #{task_id} (conflict after rebase)")
                            print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
                    else:
                        subprocess.run(["git", "rebase", "--abort"],
                                       cwd=project_dir, capture_output=True)
                        print(f"  [Isolation] Merge FAILED for task #{task_id}: {merge_result.stderr[:200]}")
                        print(f"  [Isolation] Branch '{branch_name}' PRESERVED")
                # Pop stash only if we actually stashed something
                if had_stash:
                    subprocess.run(["git", "stash", "pop"], cwd=project_dir,
                                   capture_output=True, text=True)
            except Exception as e:
                print(f"  [Isolation] Merge error for task #{task_id}: {e}")

    # Clean up worktrees — only delete branches that were successfully merged
    if use_worktrees:
        for task_id, wt_path in worktree_dirs.items():
            try:
                branch_name = f"forge-task-{task_id}"
                # Clean up ephemeral agent state file before removing worktree
                state_file = Path(wt_path) / ".forge-state.json"
                if state_file.exists():
                    state_file.unlink()
                # Always remove the worktree directory (it's a working copy)
                subprocess.run(["git", "worktree", "remove", "--force", wt_path],
                               cwd=project_dir, capture_output=True)
                if task_id in merged_tasks_seq:
                    # Branch was merged — safe to delete
                    subprocess.run(["git", "branch", "-D", branch_name],
                                   cwd=project_dir, capture_output=True)
                else:
                    # Branch was NOT merged — PRESERVE it so work isn't lost
                    print(f"  [Isolation] Keeping branch '{branch_name}' (unmerged work)")
            except Exception:
                pass
        # Clean up worktree base dir if empty
        try:
            worktree_base.rmdir()
        except OSError:
            pass
