"""Task #3119 (P1-G): finish dispatch-07, isolate nested projects, 3112 LOWs.

Follow-up to the independent review of task #3112. One or more tests per
finding; each fails on the pre-change code (cba0e0c) on its assertion:

1. dispatch-07 — the ``--goal`` / ``--parallel-goals`` planner and evaluator
   run in the main checkout, so they get read-only built-in tools
   (``--tools Read,Glob,Grep``), and a goal the guard stops exits with
   ``EXIT_DISPATCH_REFUSED`` instead of 0.
2. A project nested inside another git repository is a git project: it gets
   a worktree of the enclosing repository, its agent runs in the project's
   sub-directory of that worktree and its commits reach the default branch
   only through the gated merge. A project the enclosing repository does not
   track is refused, never run in place.
3. LOWs: a "no changes needed" claim with commits (or uncommitted work) is
   blocked and its branch kept; the planner claim check rejects a NULL
   ``created_at`` and handles ``Z`` / offset timestamps; overlapping merge
   signal shields restore the operator's SIGTERM handler; refusals in
   ``--goal``, ``--auto-run`` and ``--parallel-goals`` exit non-zero.
4. ``--auto-run`` and ``--parallel-goals`` resolve the project directories
   the CLI loaded from ``forge_config.json``.

A few controls (named ``..._control``) pin behaviour that must NOT change and
pass on both sides.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import equipa.cli as cli_mod
import equipa.constants as constants_mod
import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
import equipa.manager as manager_mod
import equipa.merge_safety as merge_safety_mod
from equipa.single_agent_guard import validate_tasks_created_claim

from test_dispatch_modes_gated_3112 import (
    GateProbe,
    _branch_sha,
    _commit_files,
    _git,
    _init_repo,
    _is_ancestor,
    _master,
    _task,
)

# equipa.dispatch.EXIT_DISPATCH_REFUSED
EXIT_REFUSED = 2
RUN_STARTED_AT = "2026-09-30 10:00:00"


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """A deferred SIGTERM must not leak into other tests."""
    merge_safety_mod.reset_shutdown_request()
    yield
    merge_safety_mod.reset_shutdown_request()


@pytest.fixture
def project_dirs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The shared ``PROJECT_DIRS`` dict; its binding and contents are restored."""
    shared = constants_mod.PROJECT_DIRS
    saved = dict(shared)
    monkeypatch.setattr(constants_mod, "PROJECT_DIRS", shared)
    yield shared
    shared.clear()
    shared.update(saved)


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

def _patch_auto_run(monkeypatch, task: dict, probe: GateProbe, dev_test_loop) -> None:
    async def no_op(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch_mod, "fetch_task", lambda _id: dict(task))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "fire_hook", no_op)
    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop", dev_test_loop)
    monkeypatch.setattr(dispatch_mod, "update_task_status", probe.record_status)
    monkeypatch.setattr(dispatch_mod, "record_agent_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "run_quality_scoring", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "maybe_run_reflexion", no_op)
    monkeypatch.setattr(
        dispatch_mod, "update_injected_episode_q_values_for_task", lambda *a, **k: None,
    )
    monkeypatch.setattr(dispatch_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(dispatch_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)


def _committing_loop(probe: GateProbe, outcome: str = "tests_passed"):
    async def dev_test_loop(task, project_dir, project_context, args, output=None, **_):
        probe.agent_commits(project_dir, task["id"])
        return {"cost": 0.0, "duration": 0.0}, 1, outcome

    return dev_test_loop


def _run_project(codename: str, task_id: int) -> dict:
    summary = {"project_id": 9001, "codename": codename, "total_todo": 1,
               "tasks": [{"id": task_id, "title": f"task {task_id}"}]}
    args = SimpleNamespace(model="m", max_turns=10, max_tasks_per_project=None,
                           security_review=True)
    return asyncio.run(dispatch_mod.run_project_tasks(
        summary, {"features": {"autoresearch": False}}, args,
    ))


class GoalCalls:
    """What the faked planner / evaluator did during a goal."""

    def __init__(self) -> None:
        self.evaluator_runs = 0


def _patch_goal(monkeypatch, task: dict, probe: GateProbe, *,
                planner_commits_on_master: bool = False) -> GoalCalls:
    calls = GoalCalls()

    async def fake_planner(goal, project_id, project_dir, project_context, args,
                           output=None):
        if planner_commits_on_master:
            _commit_files(Path(project_dir), {"PLAN.md": "plan\n"}, "planner wrote on master")
        return {"cost": 0.0, "duration": 0.0}, [task["id"]]

    async def fake_evaluator(goal, project_id, project_dir, project_context,
                             completed, blocked, args, output=None):
        calls.evaluator_runs += 1
        return ({"cost": 0.0, "duration": 0.0},
                {"goal_status": "complete", "tasks_created": [],
                 "evaluation": "done", "blockers": "none"})

    monkeypatch.setattr(manager_mod, "run_planner_agent", fake_planner)
    monkeypatch.setattr(manager_mod, "run_evaluator_agent", fake_evaluator)
    monkeypatch.setattr(manager_mod, "run_dev_test_loop", _committing_loop(probe))
    monkeypatch.setattr(manager_mod, "fetch_tasks_by_ids", lambda ids: [dict(task)])
    monkeypatch.setattr(manager_mod, "_get_task_status", lambda _id: "todo")
    monkeypatch.setattr(manager_mod, "update_task_status", probe.record_status)
    return calls


def _goal_cli_args(**overrides) -> SimpleNamespace:
    values = dict(goal="ship the feature", goal_project=9001, model="m",
                  max_turns=10, max_rounds=1, manager_cost_limit=100.0,
                  dry_run=False, yes=True, dispatch_config={}, security_review=True)
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_goal_cli(monkeypatch, project_dirs: dict, repo: Path) -> None:
    project_dirs["gatedproj"] = str(repo)
    project_info = {"codename": "gatedproj", "name": "GatedProject"}
    monkeypatch.setattr(cli_mod, "fetch_project_info", lambda _pid: project_info)
    monkeypatch.setattr(dispatch_mod, "fetch_project_info", lambda _pid: project_info)
    monkeypatch.setattr(cli_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(cli_mod, "_auto_snapshot_dispatch", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1. dispatch-07: read-only planner / evaluator, non-zero exit on refusal
# ---------------------------------------------------------------------------

def _captured_agent_command(monkeypatch, run) -> list[str]:
    captured: list[list[str]] = []

    async def fake_run_agent(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return {"success": False, "errors": ["stubbed"], "duration": 0.0}

    monkeypatch.setattr(manager_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager_mod, "build_planner_prompt", lambda *a, **k: "plan it")
    monkeypatch.setattr(manager_mod, "build_evaluator_prompt", lambda *a, **k: "judge it")
    asyncio.run(run())
    assert len(captured) == 1, "the agent was not spawned exactly once"
    return captured[0]


def _built_in_tools(cmd: list[str]) -> set[str]:
    assert cmd.count("--tools") == 1, f"no single --tools allowlist in {cmd}"
    return set(cmd[cmd.index("--tools") + 1].split(","))


@pytest.mark.parametrize("role", ["planner", "evaluator"])
def test_goal_planner_and_evaluator_get_read_only_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    args = SimpleNamespace(model="m", max_turns=10, dispatch_config={})

    async def run() -> None:
        if role == "planner":
            await manager_mod.run_planner_agent("goal", 9001, str(tmp_path), {}, args)
        else:
            await manager_mod.run_evaluator_agent(
                "goal", 9001, str(tmp_path), {}, [], [], args,
            )

    cmd = _captured_agent_command(monkeypatch, run)

    tools = _built_in_tools(cmd)
    assert tools == {"Read", "Glob", "Grep"}
    assert not tools & {"Write", "Edit", "NotebookEdit", "Bash"}
    assert "read-only" in cmd[cmd.index("-p") + 1]


def test_goal_cli_exits_non_zero_when_the_planner_moves_the_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    calls = _patch_goal(monkeypatch, _task(3331), probe, planner_commits_on_master=True)
    _patch_goal_cli(monkeypatch, project_dirs, repo)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(cli_mod.run_mode_goal(_goal_cli_args()))

    assert exc_info.value.code == EXIT_REFUSED
    assert probe.agent_dirs == [], "a task ran after the default branch moved"
    assert calls.evaluator_runs == 0


def test_goal_stops_and_exits_non_zero_when_a_task_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    """A stale forge-task-<id> branch refuses the task; the goal stops there
    (no evaluator is paid for) and the CLI exits with EXIT_DISPATCH_REFUSED."""
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "forge-task-3332")
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    calls = _patch_goal(monkeypatch, _task(3332), probe)
    _patch_goal_cli(monkeypatch, project_dirs, repo)

    outcome, *_ = asyncio.run(manager_mod.run_manager_loop(
        "ship the feature", 9001, str(repo), {}, _goal_cli_args(),
    ))
    assert outcome == "worktree_refused"
    assert calls.evaluator_runs == 0
    assert probe.statuses == [(3332, "worktree_refused", None)]

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(cli_mod.run_mode_goal(_goal_cli_args()))
    assert exc_info.value.code == EXIT_REFUSED
    assert probe.agent_dirs == []
    assert _master(repo) == probe.baseline


def test_parallel_goals_cli_exits_non_zero_when_a_goal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_goal(monkeypatch, _task(3333), probe, planner_commits_on_master=True)
    _patch_goal_cli(monkeypatch, project_dirs, repo)
    goals_file = tmp_path / "goals.json"
    goals_file.write_text(json.dumps({
        "max_concurrent": 1, "max_rounds": 1,
        "goals": [{"goal": "ship the feature", "project_id": 9001}],
    }), encoding="utf-8")
    args = SimpleNamespace(parallel_goals=str(goals_file), max_concurrent=None,
                           dry_run=False, yes=True, dispatch_config={},
                           security_review=True)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(cli_mod.run_mode_parallel_goals(args))

    assert exc_info.value.code == EXIT_REFUSED
    assert probe.agent_dirs == []


# ---------------------------------------------------------------------------
# 2. A project nested inside another repository
# ---------------------------------------------------------------------------

def _nested_project(tmp_path: Path) -> tuple[Path, Path]:
    outer = _init_repo(tmp_path / "outer")
    _commit_files(outer, {"apps/web/app.py": "print('web')\n"}, "add nested project")
    return outer, outer / "apps" / "web"


def test_nested_project_is_a_git_project(tmp_path: Path) -> None:
    outer, project = _nested_project(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_ops_mod._is_git_repo(project), "nested project not seen as git"
    assert git_ops_mod._is_git_repo(outer)
    assert not git_ops_mod._is_git_repo(plain)
    assert not git_ops_mod._is_git_repo(tmp_path / "missing")
    # R3119-02 (task #3126): a ".git" directory is inside a repository but
    # has no work tree; it is refused, never "not git".
    with pytest.raises(git_ops_mod.GitRepositoryUnreadableError):
        git_ops_mod._is_git_repo(outer / ".git")


@pytest.mark.parametrize("review_high", [1, 0], ids=["review-blocks", "review-clean"])
def test_nested_project_runs_in_its_worktree_subdir_and_merges_only_through_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
    review_high: int,
) -> None:
    outer, project = _nested_project(tmp_path)
    task = _task(3341)
    probe = GateProbe(outer, review_high)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, task, probe, _committing_loop(probe))
    project_dirs["nestedproj"] = str(project)

    _run_project("nestedproj", 3341)

    worktree_subdir = project / ".forge-worktrees" / "task-3341" / "apps" / "web"
    assert probe.agent_dirs == [str(worktree_subdir)], (
        f"agent ran in {probe.agent_dirs}; it committed in the project "
        f"checkout instead of its worktree"
    )
    if review_high:
        assert _master(outer) == probe.baseline, "a blocked commit reached master"
        assert probe.statuses == [(3341, "security_review_blocked", None)]
        branch = _branch_sha(outer, "forge-task-3341")
        assert branch is not None and branch != probe.baseline
    else:
        assert probe.master_at_gate == [probe.baseline]
        [(task_id, outcome, merged_sha)] = probe.statuses
        assert (task_id, outcome) == (3341, "tests_passed")
        assert merged_sha and _is_ancestor(outer, merged_sha, _master(outer))
        assert (project / "src" / "feature.py").is_file()
        assert not (project / ".forge-worktrees" / "task-3341").exists()


def test_nested_project_untracked_by_the_enclosing_repo_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    outer = _init_repo(tmp_path / "outer")
    _commit_files(outer, {".gitignore": "scratch/\n"}, "ignore scratch")
    project = outer / "scratch" / "proj"
    project.mkdir(parents=True)
    (project / "app.py").write_text("print('scratch')\n")
    task = _task(3342)
    probe = GateProbe(outer, 0)
    probe.install(monkeypatch)

    async def records_where_it_ran(task, project_dir, project_context, args,
                                   output=None, **_):
        probe.agent_dirs.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    _patch_auto_run(monkeypatch, task, probe, records_where_it_ran)
    project_dirs["scratchproj"] = str(project)

    result = _run_project("scratchproj", 3342)

    assert probe.agent_dirs == [], "the agent ran outside any worktree"
    assert _master(outer) == probe.baseline
    assert probe.statuses == [(3342, "worktree_refused", None)]
    assert len(result["refusals"]) == 1 and "#3342" in result["refusals"][0]
    assert _branch_sha(outer, "forge-task-3342") is None, "empty branch left behind"
    assert not (project / ".forge-worktrees" / "task-3342").exists()


# ---------------------------------------------------------------------------
# 3. LOW: "no changes needed" after committing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("left_behind", ["commit", "uncommitted"])
def test_no_changes_claim_with_changes_is_blocked_and_branch_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
    left_behind: str,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3343)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)

    async def claims_no_changes(task, project_dir, project_context, args,
                                output=None, **_):
        probe.agent_dirs.append(project_dir)
        if left_behind == "commit":
            _commit_files(Path(project_dir), {"src/feature.py": "x = 1\n"}, "sneaky")
        else:
            (Path(project_dir) / "notes.txt").write_text("never committed\n")
        return {"cost": 0.0, "duration": 0.0}, 1, "early_completed_no_changes"

    _patch_auto_run(monkeypatch, task, probe, claims_no_changes)
    project_dirs["gatedproj"] = str(repo)

    _run_project("gatedproj", 3343)

    assert probe.statuses == [(3343, "no_changes_claim_contradicted", None)], (
        "a 'no changes needed' claim with changes behind it was marked done"
    )
    assert probe.review_dirs == [] and probe.master_at_gate == []
    assert _master(repo) == probe.baseline
    assert _branch_sha(repo, "forge-task-3343") is not None, "the agent's work was dropped"
    if left_behind == "uncommitted":
        assert "equipa-early-term task-3343" in _git(repo, "stash", "list")


def test_no_changes_claim_without_changes_stays_done_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3344)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)

    async def truly_no_changes(task, project_dir, project_context, args,
                               output=None, **_):
        probe.agent_dirs.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "early_completed_no_changes"

    _patch_auto_run(monkeypatch, task, probe, truly_no_changes)
    project_dirs["gatedproj"] = str(repo)

    _run_project("gatedproj", 3344)

    assert probe.statuses == [(3344, "early_completed_no_changes", None)]
    assert _branch_sha(repo, "forge-task-3344") is None


def test_tasks_mode_no_changes_claim_with_commits_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3345)
    statuses: list[tuple[int, str]] = []

    async def claims_no_changes(task, task_dir, project_context, args, config,
                                output=None, task_branch=None):
        _commit_files(Path(task_dir), {"src/feature.py": "x = 1\n"}, "sneaky")
        return ({"cost": 0.0, "duration": 0.0}, 1, "early_completed_no_changes",
                0.0, 0.0, task)

    monkeypatch.setattr(dispatch_mod, "fetch_tasks_by_ids", lambda ids: [dict(task)])
    monkeypatch.setattr(dispatch_mod, "resolve_project_dir", lambda _t: str(repo))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop_with_autoresearch",
                        claims_no_changes)
    monkeypatch.setattr(dispatch_mod, "update_task_status",
                        lambda task_id, outcome, **k: statuses.append((task_id, outcome)))
    monkeypatch.setattr(dispatch_mod, "record_agent_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(dispatch_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)
    baseline = _master(repo)
    args = SimpleNamespace(yes=True, max_concurrent=1, use_flow=False,
                           security_review=False, dispatch_config={})

    asyncio.run(dispatch_mod.run_parallel_tasks([3345], args))

    assert statuses == [(3345, "no_changes_claim_contradicted")]
    assert _master(repo) == baseline
    assert _branch_sha(repo, "forge-task-3345") is not None


# ---------------------------------------------------------------------------
# 3. LOW: planner TASKS_CREATED claim timestamps
# ---------------------------------------------------------------------------

def _tasks_db(created_at: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER, created_at TEXT)"
    )
    conn.execute("INSERT INTO tasks VALUES (7, 9001, ?)", (created_at,))
    return conn


@pytest.mark.parametrize(("created_at", "accepted"), [
    ("2026-09-30T10:00:05Z", True),
    ("2026-09-30T12:00:05+02:00", True),
    ("2026-09-30T09:59:00Z", False),
    (None, False),
    ("not a timestamp", False),
], ids=["z-after-start", "offset-after-start", "z-before-start", "null", "garbage"])
def test_planner_claim_timestamps(
    monkeypatch: pytest.MonkeyPatch, created_at: Any, accepted: bool,
) -> None:
    conn = _tasks_db(created_at)
    monkeypatch.setattr(manager_mod, "get_db_connection", lambda *a, **k: conn)

    rejection = manager_mod._reject_planner_claim([7], 9001, RUN_STARTED_AT)

    assert (rejection is None) == accepted, rejection


def test_tasks_created_claim_with_null_created_at_is_rejected() -> None:
    class Rows:
        def fetch_tasks_by_ids(self, ids):
            return [{"id": 7, "project_id": 9001, "created_at": None}]

    verdict = validate_tasks_created_claim(
        stdout="TASKS_CREATED: 7", run_started_at=RUN_STARTED_AT,
        expected_project_id=9001, db=Rows(),
    )

    assert not verdict.is_valid
    assert verdict.invalid_ids == (7,)


def test_naive_created_at_after_start_is_accepted_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _tasks_db("2026-09-30 10:00:05")
    monkeypatch.setattr(manager_mod, "get_db_connection", lambda *a, **k: conn)

    assert manager_mod._reject_planner_claim([7], 9001, RUN_STARTED_AT) is None


# ---------------------------------------------------------------------------
# 3. LOW: overlapping merge signal shields
# ---------------------------------------------------------------------------

def test_overlapping_merge_shields_restore_the_operator_sigterm_handler(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    received_after: list[int] = []

    def operator_handler(signum, _frame):
        received_after.append(signum)

    previous = signal.signal(signal.SIGTERM, operator_handler)
    try:
        async def overlapping_merges():
            first = merge_safety_mod.MergeSignalShield(repo, context="merge A")
            second = merge_safety_mod.MergeSignalShield(repo, context="merge B")
            await first.__aenter__()
            await second.__aenter__()
            os.kill(os.getpid(), signal.SIGTERM)
            # A finishes before B: the shields do not exit in LIFO order.
            await first.__aexit__(None, None, None)
            await second.__aexit__(None, None, None)
            return first, second

        first, second = asyncio.run(overlapping_merges())
        handler_after = signal.getsignal(signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert first.received == signal.SIGTERM and second.received == signal.SIGTERM
    assert merge_safety_mod.shutdown_requested() == signal.SIGTERM
    assert handler_after is operator_handler, (
        f"SIGTERM handler left as {handler_after!r}; later SIGTERMs are "
        f"swallowed instead of stopping the process"
    )
    assert received_after == [signal.SIGTERM]


# ---------------------------------------------------------------------------
# 3. LOW / 4: --auto-run refusals and project directories from the CLI config
# ---------------------------------------------------------------------------

def test_auto_run_cli_exits_non_zero_when_a_task_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "forge-task-3351")
    task = _task(3351)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, task, probe, _committing_loop(probe))
    project_dirs["gatedproj"] = str(repo)
    work = [{"project_id": 9001, "codename": "gatedproj", "total_todo": 1,
             "tasks": [{"id": 3351, "title": "task 3351"}]}]
    monkeypatch.setattr(cli_mod, "scan_pending_work", lambda: [dict(p) for p in work])
    monkeypatch.setattr(cli_mod, "apply_dispatch_filters", lambda w, *a: w)
    monkeypatch.setattr(cli_mod, "score_project", lambda *a: None)
    monkeypatch.setattr(cli_mod, "_auto_snapshot_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "print_dispatch_plan", lambda *a: None)
    args = SimpleNamespace(dispatch_config={"features": {"autoresearch": False}},
                           max_concurrent=None, max_tasks_per_project=None,
                           dry_run=False, yes=True, model="m", max_turns=10,
                           security_review=True)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(cli_mod.run_mode_auto_run(args))

    assert exc_info.value.code == EXIT_REFUSED
    assert probe.agent_dirs == []
    assert probe.statuses == [(3351, "worktree_refused", None)]


def _load_cli_config(monkeypatch, tmp_path: Path, project_dirs_config: dict) -> None:
    """Run the CLI's forge_config.json loader against a config in tmp_path."""
    (tmp_path / "forge_config.json").write_text(
        json.dumps({"project_dirs": project_dirs_config}), encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "__file__", str(tmp_path / "equipa" / "cli.py"))
    cli_mod.load_config()


def test_parallel_goals_resolve_project_dirs_loaded_by_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _load_cli_config(monkeypatch, tmp_path, {"GatedProj": str(repo)})
    monkeypatch.setattr(dispatch_mod, "fetch_project_info",
                        lambda _pid: {"codename": "gatedproj", "name": "Gated"})

    [resolved] = dispatch_mod.validate_goals([{"goal": "g", "project_id": 9001}])

    assert resolved["project_dir"] == str(repo)


def test_auto_run_resolves_project_dirs_loaded_by_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _load_cli_config(monkeypatch, tmp_path, {"GatedProj": str(repo)})
    task = _task(3352)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, task, probe, _committing_loop(probe))

    result = _run_project("gatedproj", 3352)

    assert result["error"] is None, result["error"]
    assert probe.agent_dirs == [str(repo / ".forge-worktrees" / "task-3352")]
    [(task_id, outcome, _)] = probe.statuses
    assert (task_id, outcome) == (3352, "tests_passed")
