"""EQUIPA manager — Manager loop: Plan -> Execute -> Evaluate -> Repeat.

Layer 7: Imports from equipa.agent_runner, equipa.constants, equipa.db, equipa.loops,
         equipa.output, equipa.prompts, equipa.roles, equipa.tasks.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from equipa.agent_runner import (
    OVERLOADED_OUTCOME,
    _run_started_at_utc,
    build_cli_command,
    is_overloaded_result,
    run_agent,
)
from equipa.constants import (
    MANAGER_COST_LIMIT,
    MAX_FOLLOWUP_TASKS,
    MAX_TASKS_PER_PLAN,
)
from equipa.db import get_db_connection, update_task_status
from equipa.git_ops import GitRepositoryUnreadableError, _is_git_repo
from equipa.loops import run_dev_test_loop
from equipa.merge_integrity import DefaultBranchGuard, MergeIntegrityError
from equipa.merge_safety import report_leftover_dispatch_state, shutdown_requested
from equipa.output import log
from equipa.prompts import build_evaluator_prompt, build_planner_prompt
from equipa.roles import get_role_turns
from equipa.single_agent_guard import TasksCreatedDb, validate_tasks_created_claim
from equipa.tasks import _get_task_status, fetch_tasks_by_ids

logger = logging.getLogger(__name__)

# dispatch-07 (task #3119): the planner and evaluator run in the project's
# MAIN checkout, where a commit lands straight on the default branch. They
# only read the project and return the tasks they want in a TASKS_JSON
# block (the manager inserts them, R3126-03, task #3132), so their built-in
# tools are limited to this read-only allowlist (``--tools``): no Write,
# Edit, NotebookEdit or Bash, hence nothing to commit with.
GOAL_AGENT_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

# Goal outcomes that mean the dispatch was refused rather than run to an
# end: the default branch moved outside the gated merge or could not be
# pinned, or a task was refused its isolation worktree. The CLI exits with
# EXIT_DISPATCH_REFUSED for these (3112 review, task #3119).
GOAL_REFUSED_OUTCOMES: frozenset[str] = frozenset({
    "merge_integrity_failed",
    "worktree_refused",
})


# R3126-03 / IND-04 (task #3132): the MCP server set the goal agents get.
# TheForge's ``write_query`` runs arbitrary SQL on the orchestrator's DB
# (resolving security_finding decisions, flipping task status), so the
# planner and evaluator get no MCP server at all. They return the tasks they
# want in a ``TASKS_JSON`` block and the manager inserts them itself.
GOAL_AGENT_MCP_CONFIG = '{"mcpServers": {}}'


def _without_mcp_servers(cmd: list[str]) -> list[str]:
    """``cmd`` with every ``--mcp-config`` value replaced by the empty set.

    ``--mcp-config`` takes several values (files or JSON strings) up to the
    next option, and ``--mcp-config=<value>`` is accepted too.
    """
    result: list[str] = []
    index = 0
    while index < len(cmd):
        token = cmd[index]
        if token.startswith("--mcp-config="):
            result.append(f"--mcp-config={GOAL_AGENT_MCP_CONFIG}")
            index += 1
            continue
        result.append(token)
        index += 1
        if token == "--mcp-config":
            result.append(GOAL_AGENT_MCP_CONFIG)
            while index < len(cmd) and not cmd[index].startswith("-"):
                index += 1
    return result


def restrict_to_read_only_tools(cmd: list[str]) -> list[str]:
    """``cmd`` with the agent's built-in tools limited to the read-only set.

    ``--tools`` is an allowlist of built-in tools, so a tool added to the
    CLI later is excluded too.

    ``--tools`` does not cover MCP servers. ``--strict-mcp-config`` keeps
    only the servers EQUIPA passes with ``--mcp-config``, so no user- or
    project-scope server (one that writes files, for example) reaches the
    read-only agents (R3119-03, task #3126), and EQUIPA passes none, so
    TheForge's DB write tools do not reach them either (R3126-03, task
    #3132).
    """
    if "--tools" in cmd:
        raise ValueError("agent command already selects its tools (--tools)")
    restricted = _without_mcp_servers(cmd)
    strict_mcp = [] if "--strict-mcp-config" in cmd else ["--strict-mcp-config"]
    return [*restricted, *strict_mcp, "--tools", ",".join(GOAL_AGENT_READ_ONLY_TOOLS)]


def _read_only_notice(project_dir: str) -> str:
    """Tell a goal agent up front that it cannot change the checkout."""
    return (
        f"Project dir: {project_dir}. You are read-only: your only tools are "
        f"{', '.join(GOAL_AGENT_READ_ONLY_TOOLS)}, and you have no database "
        f"access. Do not try to edit files, run commands or commit; return "
        f"any task you want created in a {TASKS_JSON_MARKER} block."
    )


# --- Tasks proposed by the goal agents (R3126-03, task #3132) ----------------

TASKS_JSON_MARKER = "TASKS_JSON:"
PLANNED_TASK_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
MAX_PLANNED_TASK_TITLE_CHARS = 200
MAX_PLANNED_TASK_DESCRIPTION_CHARS = 8000


class PlannedTasksError(ValueError):
    """A ``TASKS_JSON`` block that cannot be turned into tasks."""


@dataclass(frozen=True)
class PlannedTask:
    """One task a goal agent asked the manager to create."""

    title: str
    description: str
    priority: str


def _planned_task(index: int, entry: object) -> PlannedTask:
    """Validate one ``TASKS_JSON`` entry."""
    if not isinstance(entry, dict):
        raise PlannedTasksError(f"entry {index} is not an object")
    title = entry.get("title")
    description = entry.get("description", "")
    priority = entry.get("priority", "medium")
    if not isinstance(title, str) or not title.strip():
        raise PlannedTasksError(f"entry {index} has no title")
    if len(title) > MAX_PLANNED_TASK_TITLE_CHARS:
        raise PlannedTasksError(
            f"entry {index} title is longer than {MAX_PLANNED_TASK_TITLE_CHARS} chars"
        )
    if not isinstance(description, str):
        raise PlannedTasksError(f"entry {index} description is not text")
    if len(description) > MAX_PLANNED_TASK_DESCRIPTION_CHARS:
        raise PlannedTasksError(
            f"entry {index} description is longer than "
            f"{MAX_PLANNED_TASK_DESCRIPTION_CHARS} chars"
        )
    if priority not in PLANNED_TASK_PRIORITIES:
        raise PlannedTasksError(
            f"entry {index} priority {priority!r} is not one of "
            f"{', '.join(PLANNED_TASK_PRIORITIES)}"
        )
    return PlannedTask(title.strip(), description.strip(), priority)


def parse_tasks_json(result_text: str) -> list[PlannedTask]:
    """Tasks from the last ``TASKS_JSON:`` block of an agent's output.

    The marker is followed by a JSON array of objects with ``title``,
    ``description`` and ``priority`` (optionally inside a ```` ``` ```` fence).
    Returns an empty list when there is no block; raises
    :class:`PlannedTasksError` when the block is malformed, so a garbled
    plan creates nothing rather than part of itself.
    """
    if not result_text:
        return []
    position = result_text.rfind(TASKS_JSON_MARKER)
    if position < 0:
        return []
    remainder = result_text[position + len(TASKS_JSON_MARKER):].lstrip()
    if remainder.startswith("```"):
        remainder = remainder.split("\n", 1)[1] if "\n" in remainder else ""
    try:
        entries, _end = json.JSONDecoder().raw_decode(remainder.lstrip())
    except ValueError as exc:
        raise PlannedTasksError(f"{TASKS_JSON_MARKER} is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise PlannedTasksError(f"{TASKS_JSON_MARKER} is not a JSON array")
    return [_planned_task(index, entry) for index, entry in enumerate(entries)]


def insert_planned_tasks(project_id: int, tasks: list[PlannedTask]) -> list[int]:
    """Create ``tasks`` in ``project_id`` in one transaction; their new ids.

    The orchestrator, not the agent, writes the rows: the project, status and
    creation time are fixed here, whatever the agent asked for.
    """
    if not tasks:
        return []
    conn = get_db_connection(write=True)
    try:
        with conn:
            return [
                conn.execute(
                    "INSERT INTO tasks (project_id, title, description, status, "
                    "priority, created_at) "
                    "VALUES (?, ?, ?, 'todo', ?, datetime('now'))",
                    (project_id, task.title, task.description, task.priority),
                ).lastrowid
                for task in tasks
            ]
    finally:
        conn.close()


def _create_proposed_tasks(
    role: str,
    result_text: str,
    project_id: int,
    limit: int,
    output: Any,
) -> list[int] | None:
    """Insert the tasks of a ``TASKS_JSON`` block; None when there is none.

    A malformed block or a failed insert creates nothing and returns ``[]``.
    """
    try:
        planned = parse_tasks_json(result_text)
    except PlannedTasksError as exc:
        log(f"  [{role}] REJECTED {TASKS_JSON_MARKER} block: {exc}. "
            f"No task created.", output)
        return []
    if not planned:
        return None
    if len(planned) > limit:
        log(f"  [{role}] Proposed {len(planned)} tasks (max {limit}). "
            f"Creating the first {limit}.", output)
        planned = planned[:limit]
    try:
        return insert_planned_tasks(project_id, planned)
    except (sqlite3.Error, OSError) as exc:
        logger.exception("[%s] could not create the proposed tasks", role)
        log(f"  [{role}] Could not create the proposed tasks: {exc}", output)
        return []


def parse_planner_output(result_text: str) -> list[int]:
    """Extract TASKS_CREATED list from Planner agent output.

    Returns a list of integer task IDs, or empty list on failure.
    """
    if not result_text:
        return []

    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if not value or value.lower() == "none":
                return []
            ids: list[int] = []
            for part in value.split(","):
                part = part.strip()
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
            return ids

    return []


def parse_evaluator_output(result_text: str) -> dict[str, Any]:
    """Extract GOAL_STATUS, TASKS_CREATED, EVALUATION, BLOCKERS from Evaluator output.

    Returns a dict with parsed fields.
    """
    parsed: dict[str, Any] = {
        "goal_status": "blocked",
        "tasks_created": [],
        "evaluation": "",
        "blockers": "none",
    }

    if not result_text:
        return parsed

    for line in result_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("GOAL_STATUS:"):
            status = stripped.split(":", 1)[1].strip().lower()
            if status in ("complete", "needs_more", "blocked"):
                parsed["goal_status"] = status

        elif stripped.startswith("TASKS_CREATED:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value.lower() != "none":
                for part in value.split(","):
                    part = part.strip()
                    try:
                        parsed["tasks_created"].append(int(part))
                    except ValueError:
                        continue

        elif stripped.startswith("EVALUATION:"):
            parsed["evaluation"] = stripped.split(":", 1)[1].strip()

        elif stripped.startswith("BLOCKERS:"):
            parsed["blockers"] = stripped.split(":", 1)[1].strip()

    return parsed


def _reject_planner_claim(
    task_ids: list[int],
    project_id: int,
    run_started_at: str | None,
) -> str | None:
    """Why the planner's ``TASKS_CREATED`` ids must not run, or None if valid.

    dispatch-16 (task #3112): goal mode used to execute whatever ids the
    planner printed, so a hallucinated line naming another project's tasks
    ran them in this project's directory and flipped their status. The exact
    ids the manager would execute are checked with
    ``validate_tasks_created_claim``: each must exist, belong to
    ``project_id`` and have been created during this planner run. A failed
    lookup rejects the claim (fail closed).
    """
    claim_text = "TASKS_CREATED: " + ",".join(str(task_id) for task_id in task_ids)
    try:
        with TasksCreatedDb(get_db_connection()) as db:
            verdict = validate_tasks_created_claim(
                stdout=claim_text,
                run_started_at=run_started_at,
                expected_project_id=project_id,
                db=db,
            )
    except (sqlite3.Error, OSError) as exc:
        logger.exception("[Planner] TASKS_CREATED validation could not read TheForge")
        return f"could not verify the claimed ids against TheForge: {exc}"
    return None if verdict.is_valid else verdict.reason


async def run_planner_agent(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], list[int]]:
    """Spawn the Planner agent to break a goal into tasks.

    Returns (result, task_ids) tuple.
    """
    log("\n  [Planner] Building prompt...", output)
    system_prompt = build_planner_prompt(goal, project_id, project_dir, project_context)

    with build_cli_command(
        system_prompt,
        project_dir,
        get_role_turns("planner", args),
        args.model,
        role="planner",
        prompt_message=f"Break this goal into tasks. {_read_only_notice(project_dir)}",
    ) as cmd:
        log(f"  [Planner] Spawning agent (prompt: {len(system_prompt)} chars, "
            f"read-only tools)...", output)
        run_started_at = _run_started_at_utc()
        result = await run_agent(restrict_to_read_only_tools(cmd))

    if is_overloaded_result(result):
        log("  [Planner] Agent FAILED: model overloaded (529) through every "
            "retry. Not downgrading the model; no tasks planned.", output)
        return result, []
    if not result["success"]:
        log(f"  [Planner] Agent failed: {result.get('errors', [])}", output)
        return result, []

    created = _create_proposed_tasks(
        "Planner", result.get("result_text", ""), project_id,
        MAX_TASKS_PER_PLAN, output,
    )
    if created is not None:
        if created:
            log(f"  [Planner] Created {len(created)} tasks: {created}", output)
        return result, created

    # Legacy TASKS_CREATED claim: the planner has no DB write tool any more,
    # so only ids something else created in this project during the run can
    # pass the validation below.
    task_ids = parse_planner_output(result.get("result_text", ""))

    if len(task_ids) > MAX_TASKS_PER_PLAN:
        log(f"  [Planner] Created {len(task_ids)} tasks (max {MAX_TASKS_PER_PLAN}). "
            f"Using first {MAX_TASKS_PER_PLAN}.", output)
        task_ids = task_ids[:MAX_TASKS_PER_PLAN]

    if task_ids:
        rejection = _reject_planner_claim(task_ids, project_id, run_started_at)
        if rejection:
            log(f"  [Planner] REJECTED TASKS_CREATED claim {task_ids}: {rejection}. "
                f"No task from this plan is executed.", output)
            return result, []

    if task_ids:
        log(f"  [Planner] Created {len(task_ids)} tasks: {task_ids}", output)
    else:
        log(f"  [Planner] No task IDs found in output.", output)

    return result, task_ids


async def run_evaluator_agent(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    completed_tasks: list[dict],
    blocked_tasks: list[dict],
    args: Any,
    output: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Spawn the Evaluator agent to assess goal completion.

    Returns (result, parsed_eval) tuple.
    """
    log("\n  [Evaluator] Building prompt...", output)
    system_prompt = build_evaluator_prompt(
        goal, project_id, project_dir, project_context,
        completed_tasks, blocked_tasks,
    )

    with build_cli_command(
        system_prompt,
        project_dir,
        get_role_turns("evaluator", args),
        args.model,
        role="evaluator",
        prompt_message=(
            f"Evaluate whether this goal is complete. {_read_only_notice(project_dir)}"
        ),
    ) as cmd:
        log(f"  [Evaluator] Spawning agent (prompt: {len(system_prompt)} chars, "
            f"read-only tools)...", output)
        result = await run_agent(restrict_to_read_only_tools(cmd))

    if is_overloaded_result(result):
        # Never parse an overloaded run: it did no evaluation (#2994 S1).
        log("  [Evaluator] Agent FAILED: model overloaded (529) through every "
            "retry. Not downgrading the model; goal left blocked.", output)
        return result, {"goal_status": "blocked", "tasks_created": [],
                        "evaluation": "Evaluator agent failed: model overloaded",
                        "blockers": OVERLOADED_OUTCOME}
    if not result["success"]:
        log(f"  [Evaluator] Agent failed: {result.get('errors', [])}", output)
        return result, {"goal_status": "blocked", "tasks_created": [],
                        "evaluation": "Evaluator agent failed", "blockers": "Agent error"}

    parsed = parse_evaluator_output(result.get("result_text", ""))
    created = _create_proposed_tasks(
        "Evaluator", result.get("result_text", ""), project_id,
        MAX_FOLLOWUP_TASKS, output,
    )
    if created is not None:
        parsed["tasks_created"] = created

    if len(parsed["tasks_created"]) > MAX_FOLLOWUP_TASKS:
        log(f"  [Evaluator] Created {len(parsed['tasks_created'])} follow-up tasks "
            f"(max {MAX_FOLLOWUP_TASKS}). Using first {MAX_FOLLOWUP_TASKS}.", output)
        parsed["tasks_created"] = parsed["tasks_created"][:MAX_FOLLOWUP_TASKS]

    log(f"  [Evaluator] Goal status: {parsed['goal_status']}", output)
    log(f"  [Evaluator] Evaluation: {parsed['evaluation'][:200]}", output)
    if parsed["tasks_created"]:
        log(f"  [Evaluator] Follow-up tasks: {parsed['tasks_created']}", output)

    return result, parsed


async def run_manager_loop(
    goal: str,
    project_id: int,
    project_dir: str,
    project_context: dict[str, Any],
    args: Any,
    output: Any = None,
) -> tuple[str, int, list[dict], list[dict], float, float]:
    """Run the full Manager loop: Plan -> Execute -> Evaluate -> Repeat.

    Returns (outcome, total_rounds, all_completed, all_blocked, total_cost, total_duration).
    """
    max_rounds = args.max_rounds
    cost_limit = getattr(args, "manager_cost_limit", None)
    if cost_limit is None:
        cost_limit = MANAGER_COST_LIMIT
    all_completed: list[dict] = []
    all_blocked: list[dict] = []
    total_cost = 0.0
    total_duration = 0.0

    # dispatch-07 (task #3112): in a git project each task runs in its own
    # worktree and reaches the default branch only through the gated merge
    # (equipa.dispatch.run_task_in_isolation). The planner and evaluator
    # run in the project checkout with read-only tools (task #3119), and the
    # goal's guard is still verified after each of them: if the default
    # branch moved anyway, the goal stops, nothing further is merged and the
    # CLI exits non-zero (GOAL_REFUSED_OUTCOMES).
    goal_guard: DefaultBranchGuard | None = None
    try:
        is_git_project = _is_git_repo(project_dir)
    except GitRepositoryUnreadableError as exc:
        # R3119-02 (task #3126): never run an unreadable repo ungated.
        log(f"\n  [Manager] {exc} Not running the goal.", output)
        return "merge_integrity_failed", 0, all_completed, all_blocked, total_cost, total_duration
    if is_git_project:
        # Imported here: equipa.dispatch imports this module.
        from equipa.dispatch import run_task_in_isolation

        await report_leftover_dispatch_state(project_dir)
        try:
            goal_guard = await DefaultBranchGuard.snapshot(project_dir)
        except MergeIntegrityError as exc:
            log(f"\n  [Manager] Default branch could not be pinned ({exc}). "
                f"Not running the goal.", output)
            return "merge_integrity_failed", 0, all_completed, all_blocked, total_cost, total_duration

    async def default_branch_untouched(stage: str) -> bool:
        if goal_guard is None or await goal_guard.verify(stage):
            return True
        log(f"\n  [Manager] {goal_guard.alert}. Aborting the goal.", output)
        return False

    for round_num in range(1, max_rounds + 1):
        if goal_guard is not None and shutdown_requested() is not None:
            # dispatch-06: a signal deferred during a merge stops the goal.
            log("\n  [Manager] Shutdown requested during a merge. Stopping the goal.", output)
            return "interrupted", round_num, all_completed, all_blocked, total_cost, total_duration
        log(f"\n{'#' * 60}", output)
        log(f"  MANAGER ROUND {round_num}/{max_rounds}", output)
        log(f"{'#' * 60}", output)

        # --- Phase 1: Plan ---
        log(f"\n--- Phase 1: Planning ---", output)
        planner_result, task_ids = await run_planner_agent(
            goal, project_id, project_dir, project_context, args, output=output,
        )
        total_duration += planner_result.get("duration", 0)
        if planner_result.get("cost"):
            total_cost += planner_result["cost"]

        if not await default_branch_untouched(f"after-planner round={round_num}"):
            return (
                "merge_integrity_failed", round_num, all_completed, all_blocked,
                total_cost, total_duration,
            )

        if not task_ids:
            log(f"\n  [Manager] Planner failed to create tasks. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # Fetch the created tasks
        tasks = fetch_tasks_by_ids(task_ids)
        if not tasks:
            log(f"\n  [Manager] Could not fetch tasks {task_ids} from TheForge. Aborting.", output)
            return "planner_failed", round_num, all_completed, all_blocked, total_cost, total_duration

        # --- Phase 2: Execute each task via Dev+Tester loop ---
        log(f"\n--- Phase 2: Executing {len(tasks)} tasks ---", output)
        round_completed: list[dict] = []
        round_blocked: list[dict] = []

        for i, task in enumerate(tasks, 1):
            log(f"\n{'=' * 50}", output)
            log(f"  TASK {i}/{len(tasks)}: #{task['id']} - {task['title']}", output)
            log(f"{'=' * 50}", output)

            current_status = _get_task_status(task["id"])
            if current_status == "done":
                log(f"  Task already marked done. Skipping.", output)
                round_completed.append(task)
                continue

            if goal_guard is not None:
                async def execute_in_worktree(
                    worktree_dir: str, _task_branch: str, task: dict = task,
                ) -> tuple[dict[str, Any], int, str]:
                    return await run_dev_test_loop(
                        task, worktree_dir, project_context, args, output=output,
                    )

                isolated = await run_task_in_isolation(
                    task, project_dir, project_context, args,
                    execute=execute_in_worktree, guard=goal_guard, output=output,
                )
                if isolated.outcome == "shutdown_requested":
                    # Never started: the task keeps its status.
                    log(f"\n  [Manager] {isolated.reason}. Stopping the goal.", output)
                    return (
                        "interrupted", round_num, all_completed, all_blocked,
                        total_cost, total_duration,
                    )
                result, cycles, outcome = isolated.result, isolated.cycles, isolated.outcome
                total_duration += result.get("duration", 0)
                if result.get("cost"):
                    total_cost += result["cost"]
                # The outcome already reflects the gated merge (dispatch-05).
                update_task_status(
                    task["id"], outcome, output=output, merged_sha=isolated.merged_sha,
                )
                if isolated.agent_outcome is None or goal_guard.tripped:
                    # Refused before any agent ran (stale branch, leftover
                    # worktree), or the default branch moved outside the
                    # gated merge: the operator has to look, so the goal
                    # stops here instead of paying for more agents.
                    goal_outcome = (
                        "merge_integrity_failed" if goal_guard.tripped else outcome
                    )
                    log(f"\n  [Manager] Task #{task['id']} {outcome}: "
                        f"{isolated.reason or goal_guard.alert}. Stopping the goal.",
                        output)
                    all_completed.extend(round_completed)
                    all_blocked.extend([*round_blocked, task])
                    return (
                        goal_outcome, round_num, all_completed, all_blocked,
                        total_cost, total_duration,
                    )
            else:
                # Not a git repo: there are no branches to protect.
                result, cycles, outcome = await run_dev_test_loop(
                    task, project_dir, project_context, args, output=output,
                )
                total_duration += result.get("duration", 0)
                if result.get("cost"):
                    total_cost += result["cost"]

                update_task_status(task["id"], outcome, output=output)

            if outcome in ("tests_passed", "no_tests"):
                round_completed.append(task)
                log(f"  Task #{task['id']}: COMPLETED ({outcome})", output)
            else:
                round_blocked.append(task)
                log(f"  Task #{task['id']}: BLOCKED ({outcome})", output)

        all_completed.extend(round_completed)
        all_blocked.extend(round_blocked)

        # --- Phase 3: Evaluate ---
        log(f"\n--- Phase 3: Evaluating ---", output)
        log(f"  Completed: {len(round_completed)}, Blocked: {len(round_blocked)}", output)

        eval_result, eval_parsed = await run_evaluator_agent(
            goal, project_id, project_dir, project_context,
            all_completed, all_blocked, args, output=output,
        )
        total_duration += eval_result.get("duration", 0)
        if eval_result.get("cost"):
            total_cost += eval_result["cost"]

        if not await default_branch_untouched(f"after-evaluator round={round_num}"):
            return (
                "merge_integrity_failed", round_num, all_completed, all_blocked,
                total_cost, total_duration,
            )

        if eval_parsed["goal_status"] == "complete":
            log(f"\n  [Manager] Goal COMPLETE!", output)
            return "goal_complete", round_num, all_completed, all_blocked, total_cost, total_duration

        elif eval_parsed["goal_status"] == "blocked":
            log(f"\n  [Manager] Goal BLOCKED: {eval_parsed['blockers']}", output)
            return "goal_blocked", round_num, all_completed, all_blocked, total_cost, total_duration

        if total_cost >= cost_limit:
            log(
                f"\n  [Manager] Aggregate cost ${total_cost:.2f} reached limit "
                f"${cost_limit:.2f}. Aborting.",
                output,
            )
            return (
                "cost_limit_exceeded", round_num, all_completed, all_blocked,
                total_cost, total_duration,
            )

        if eval_parsed["goal_status"] == "needs_more":
            if round_num >= max_rounds:
                log(f"\n  [Manager] Needs more work but max rounds reached.", output)
                break
            log(f"\n  [Manager] Needs more work. Continuing to round {round_num + 1}...", output)
            if eval_parsed["tasks_created"]:
                continue

    return "rounds_exhausted", max_rounds, all_completed, all_blocked, total_cost, total_duration
