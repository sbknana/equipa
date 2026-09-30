"""Task #3112 (P1-F): every dispatch mode goes through a worktree and the gated merge.

One or more tests per requirement of the 2026-09-29 dispatch review:

* dispatch-04 — ``--task --dev-test`` runs in a ``forge-task-<id>`` worktree
  and its work reaches the default branch only through ``_gated_merge_task``;
* dispatch-07 — ``--project``, ``--auto-run``, ``--goal`` and
  ``--parallel-goals`` do the same (and goal mode stops when its planner
  moves the default branch);
* dispatch-06 — SIGTERM during a merge is deferred and the merge aborted, a
  dirty main checkout is refused instead of stashed, and leftovers are
  reported (not deleted) at startup;
* dispatch-10 — ``--tasks`` honours ``max_concurrent`` from the config;
* dispatch-11 — ``--tasks --role`` is refused;
* dispatch-15/16 — ids are de-duplicated and validated, missing ids and
  other refusals exit non-zero, and a planner's ``TASKS_CREATED`` ids are
  validated against the goal's project;
* 3107 review N1 — a failed attempt reset in ``--task`` mode leaves the task
  blocked instead of crashing.

The mode-routing tests use real git repositories. Each one checks that the
default branch did not move before the gated merge ran (a spy records the
default-branch SHA when ``_gated_merge_task`` is entered), and that a blocked
review leaves it exactly where it was.

Only modules that already existed before the change are imported at module
level, so on the pre-change code every test fails on its assertion rather
than on an import error.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod
import equipa.manager as manager_mod


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "test@forgeborn.dev")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def _master(repo: Path) -> str:
    return _git(repo, "rev-parse", "refs/heads/master")


def _commit_files(directory: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(directory, "add", ".")
    _git(directory, "commit", "-q", "-m", message)


def _branch_sha(repo: Path, branch: str) -> str | None:
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return out or None


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo, capture_output=True, check=False,
    ).returncode == 0


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

AGENT_FILES = {"src/feature.py": "def feature():\n    return 42\n"}


def _review_body(high: int) -> str:
    lines = ["# Security Review\n", "## Findings\n"]
    for index in range(high):
        lines.append(f"### [H{index + 1}] HIGH — simulated\n\nDetails...\n")
    if high == 0:
        lines.append("### [M1] MEDIUM — cosmetic\n\nDetails...\n")
    return "".join(lines)


class GateProbe:
    """Records where agents ran, what the reviewer saw and the default-branch
    SHA each time the gated merge was entered."""

    def __init__(self, repo: Path, review_high: int) -> None:
        self.repo = repo
        self.review_high = review_high
        self.baseline = _master(repo)
        self.agent_dirs: list[str] = []
        self.review_dirs: list[tuple[str, str | None]] = []
        self.master_at_gate: list[str] = []
        self.statuses: list[tuple[int, str, str | None]] = []

    def agent_commits(self, directory: str, task_id: int) -> None:
        self.agent_dirs.append(directory)
        _commit_files(Path(directory), AGENT_FILES, f"feat: task {task_id}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = self

        async def fake_security_review(
            task, project_dir, project_context, args, output=None,
            stable_project_dir=None,
        ):
            probe.review_dirs.append((project_dir, stable_project_dir))
            target = Path(stable_project_dir or project_dir)
            (target / f"SECURITY-REVIEW-{task['id']}.md").write_text(
                _review_body(probe.review_high), encoding="utf-8",
            )

        monkeypatch.setattr(dispatch_mod, "run_security_review", fake_security_review)
        monkeypatch.setattr(cli_mod, "run_security_review", fake_security_review)

        real_gated_merge = dispatch_mod._gated_merge_task

        async def spy_gated_merge(**kwargs):
            probe.master_at_gate.append(_master(probe.repo))
            return await real_gated_merge(**kwargs)

        monkeypatch.setattr(dispatch_mod, "_gated_merge_task", spy_gated_merge)
        monkeypatch.setattr(cli_mod, "_gated_merge_task", spy_gated_merge)

    def record_status(self, task_id, outcome, output=None, **kwargs) -> None:
        self.statuses.append((task_id, outcome, kwargs.get("merged_sha")))

    # --- assertions ------------------------------------------------------

    def assert_ran_isolated(self, task_id: int) -> None:
        worktree = str(self.repo / ".forge-worktrees" / f"task-{task_id}")
        assert self.agent_dirs, "the agent never ran"
        assert all(d == worktree for d in self.agent_dirs), (
            f"agent ran in {self.agent_dirs}, expected only the isolation "
            f"worktree {worktree} — it worked in the project checkout"
        )

    def assert_default_branch_untouched(self, task_id: int) -> None:
        assert _master(self.repo) == self.baseline, (
            "a commit reached master although the security review blocked "
            "the merge — it did not go through the gated merge"
        )
        branch = _branch_sha(self.repo, f"forge-task-{task_id}")
        assert branch is not None and branch != self.baseline, (
            f"the task's commit is not on forge-task-{task_id}"
        )

    def assert_merged_only_by_gate(self, task_id: int) -> None:
        assert self.master_at_gate, "the gated merge was never entered"
        assert self.master_at_gate[0] == self.baseline, (
            "master had already moved when the gated merge was entered — the "
            "work reached it outside the gated merge"
        )
        assert _master(self.repo) != self.baseline, "the gated merge merged nothing"
        assert (self.repo / "src" / "feature.py").is_file()
        assert not (self.repo / ".forge-worktrees" / f"task-{task_id}").exists()


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """A deferred SIGTERM must not leak into other tests."""
    try:
        from equipa.merge_safety import reset_shutdown_request
    except ImportError:  # pre-change code: no such module
        yield
        return
    reset_shutdown_request()
    yield
    reset_shutdown_request()


def _task(task_id: int, project_id: int = 9001, role: str = "developer") -> dict:
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "gated dispatch test",
        "priority": "high",
        "project_name": "GatedProject",
        "project_id": project_id,
        "role": role,
        "status": "todo",
    }


def _patch_cli_task_basics(monkeypatch, repo: Path, task: dict, probe: GateProbe) -> dict:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli_mod, "fetch_task", lambda _id: task)
    monkeypatch.setattr(cli_mod, "fetch_next_todo", lambda _project: task)
    monkeypatch.setattr(cli_mod, "resolve_project_dir", lambda _t: str(repo))
    monkeypatch.setattr(cli_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(cli_mod, "_auto_snapshot_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "get_task_complexity", lambda _t: "medium")
    monkeypatch.setattr(cli_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(cli_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr(cli_mod, "calculate_dynamic_budget", lambda turns, **k: (turns, turns))
    monkeypatch.setattr(cli_mod, "load_checkpoint", lambda *a, **k: (None, None))
    monkeypatch.setattr(cli_mod, "verify_task_updated", lambda _id: (True, "ok"))
    monkeypatch.setattr(cli_mod, "print_dev_test_summary", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "print_summary", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "update_task_status", probe.record_status)

    async def fake_telemetry(task, result, outcome, *a, **kwargs):
        captured["outcome"] = outcome
        captured["merged_sha"] = kwargs.get("merged_sha")

    monkeypatch.setattr(cli_mod, "_post_task_telemetry", fake_telemetry)
    return captured


def _cli_args(**overrides) -> argparse.Namespace:
    values = dict(
        task=None, project=None, role="developer", dev_test=True, dry_run=False,
        yes=True, retries=0, dispatch_config={}, security_review=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


# ---------------------------------------------------------------------------
# dispatch-04: --task --dev-test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("review_high", [1, 0], ids=["review-blocks", "review-clean"])
def test_task_dev_test_runs_in_worktree_and_merges_only_through_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_high: int,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3301)
    probe = GateProbe(repo, review_high)
    probe.install(monkeypatch)
    captured = _patch_cli_task_basics(monkeypatch, repo, task, probe)

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", fake_dev_test_loop)

    asyncio.run(cli_mod.run_mode_task(_cli_args(task=3301)))

    probe.assert_ran_isolated(3301)
    if review_high:
        probe.assert_default_branch_untouched(3301)
        assert captured["outcome"] == "security_review_blocked"
    else:
        probe.assert_merged_only_by_gate(3301)
        assert captured["outcome"] == "tests_passed"
        assert captured["merged_sha"] and _is_ancestor(repo, captured["merged_sha"], _master(repo))


@pytest.mark.parametrize("leave_uncommitted", [False, True], ids=["clean", "uncommitted"])
def test_task_dev_test_without_commits_merges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leave_uncommitted: bool,
) -> None:
    """No commits: nothing is reviewed or merged. A clean run is done at the
    fork point and leaves no stale branch behind; uncommitted work blocks the
    task and is kept (stashed) on its branch."""
    repo = _init_repo(tmp_path / "repo")
    task = _task(3322)
    probe = GateProbe(repo, 1)
    probe.install(monkeypatch)
    captured = _patch_cli_task_basics(monkeypatch, repo, task, probe)

    async def idle_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_dirs.append(project_dir)
        if leave_uncommitted:
            (Path(project_dir) / "notes.txt").write_text("never committed\n")
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", idle_dev_test_loop)

    asyncio.run(cli_mod.run_mode_task(_cli_args(task=3322)))

    probe.assert_ran_isolated(3322)
    assert probe.review_dirs == [], "a reviewer was run on an empty branch"
    assert _master(repo) == probe.baseline
    assert not (repo / ".forge-worktrees" / "task-3322").exists()
    if leave_uncommitted:
        assert captured["outcome"] == "merge_failed"
        assert _branch_sha(repo, "forge-task-3322") is not None
        assert "equipa-early-term task-3322" in _git(repo, "stash", "list")
    else:
        assert captured["outcome"] == "tests_passed"
        assert captured["merged_sha"] == probe.baseline
        assert _branch_sha(repo, "forge-task-3322") is None, "stale branch left behind"


def test_task_dev_test_refuses_stale_branch_and_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover forge-task-<id> is never reused as the agent's checkout."""
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "branch", "forge-task-3302")
    task = _task(3302)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_cli_task_basics(monkeypatch, repo, task, probe)

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", fake_dev_test_loop)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli_mod.run_mode_task(_cli_args(task=3302)))

    assert excinfo.value.code == 2
    assert probe.agent_dirs == [], "an agent ran although its task was refused"
    assert _master(repo) == probe.baseline
    assert probe.statuses == [(3302, "worktree_refused", None)]


# ---------------------------------------------------------------------------
# dispatch-07: --project (single agent), --auto-run, --goal, --parallel-goals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("review_high", [1, 0], ids=["review-blocks", "review-clean"])
def test_project_single_agent_runs_in_worktree_and_merges_only_through_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_high: int,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3303)
    probe = GateProbe(repo, review_high)
    probe.install(monkeypatch)
    captured = _patch_cli_task_basics(monkeypatch, repo, task, probe)
    monkeypatch.setattr(cli_mod, "build_system_prompt", lambda *a, **k: "prompt")

    @contextlib.contextmanager
    def fake_build_cli_command(system_prompt, project_dir, *args, **kwargs):
        yield ["fake-claude", project_dir]

    async def fake_agent(cmd, *args, **kwargs):
        probe.agent_commits(cmd[1], 3303)
        result = {
            "success": True, "result_text": "done", "stdout": "", "cost": 0.0,
            "duration": 0.0, "files_changed": list(AGENT_FILES),
        }
        return result if "role" in kwargs else (result, 1)

    monkeypatch.setattr(cli_mod, "build_cli_command", fake_build_cli_command)
    monkeypatch.setattr(cli_mod, "run_agent_streaming", fake_agent)
    monkeypatch.setattr(cli_mod, "run_agent_with_retries", fake_agent)

    asyncio.run(cli_mod.run_mode_task(_cli_args(project=9001, dev_test=False)))

    probe.assert_ran_isolated(3303)
    if review_high:
        probe.assert_default_branch_untouched(3303)
        assert captured["outcome"] == "security_review_blocked"
    else:
        probe.assert_merged_only_by_gate(3303)
        assert captured["outcome"] == "tests_passed"


def _patch_auto_run(monkeypatch, repo: Path, task: dict, probe: GateProbe) -> None:
    async def fake_hook(*args, **kwargs):
        return None

    async def fake_reflexion(*args, **kwargs):
        return None

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(dispatch_mod, "PROJECT_DIRS", {"gatedproj": str(repo)})
    monkeypatch.setattr(dispatch_mod, "fetch_task", lambda _id: dict(task))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "fire_hook", fake_hook)
    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop", fake_dev_test_loop)
    monkeypatch.setattr(dispatch_mod, "update_task_status", probe.record_status)
    monkeypatch.setattr(dispatch_mod, "record_agent_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "run_quality_scoring", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "maybe_run_reflexion", fake_reflexion)
    monkeypatch.setattr(
        dispatch_mod, "update_injected_episode_q_values_for_task", lambda *a, **k: None,
    )
    monkeypatch.setattr(dispatch_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(dispatch_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)


@pytest.mark.parametrize("review_high", [1, 0], ids=["review-blocks", "review-clean"])
def test_auto_run_runs_in_worktree_and_merges_only_through_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_high: int,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3304)
    probe = GateProbe(repo, review_high)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, repo, task, probe)
    summary = {"project_id": 9001, "codename": "gatedproj", "total_todo": 1,
               "tasks": [{"id": 3304, "title": "task 3304"}]}
    args = SimpleNamespace(model="m", max_turns=10, max_tasks_per_project=None,
                           security_review=True)

    result = asyncio.run(dispatch_mod.run_project_tasks(
        summary, {"features": {"autoresearch": False}}, args,
    ))

    probe.assert_ran_isolated(3304)
    if review_high:
        probe.assert_default_branch_untouched(3304)
        assert probe.statuses == [(3304, "security_review_blocked", None)]
        assert [t["id"] for t in result["tasks_blocked"]] == [3304]
    else:
        probe.assert_merged_only_by_gate(3304)
        [(task_id, outcome, merged_sha)] = probe.statuses
        assert (task_id, outcome) == (3304, "tests_passed")
        assert merged_sha and _is_ancestor(repo, merged_sha, _master(repo))


def _patch_goal(monkeypatch, repo: Path, task: dict, probe: GateProbe,
                planner_commits_on_master: bool = False) -> None:
    async def fake_planner(goal, project_id, project_dir, project_context, args, output=None):
        if planner_commits_on_master:
            _commit_files(Path(project_dir), {"PLAN.md": "plan\n"}, "planner wrote on master")
        return {"cost": 0.0, "duration": 0.0}, [task["id"]]

    async def fake_evaluator(goal, project_id, project_dir, project_context,
                             completed, blocked, args, output=None):
        return ({"cost": 0.0, "duration": 0.0},
                {"goal_status": "complete", "tasks_created": [],
                 "evaluation": "done", "blockers": "none"})

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_commits(project_dir, task["id"])
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(manager_mod, "run_planner_agent", fake_planner)
    monkeypatch.setattr(manager_mod, "run_evaluator_agent", fake_evaluator)
    monkeypatch.setattr(manager_mod, "run_dev_test_loop", fake_dev_test_loop)
    monkeypatch.setattr(manager_mod, "fetch_tasks_by_ids", lambda ids: [dict(task)])
    monkeypatch.setattr(manager_mod, "_get_task_status", lambda _id: "todo")
    monkeypatch.setattr(manager_mod, "update_task_status", probe.record_status)


def _goal_args() -> SimpleNamespace:
    return SimpleNamespace(max_rounds=1, manager_cost_limit=100.0, max_turns=10,
                           model="m", security_review=True, dispatch_config={})


def _run_goal(entry: str, repo: Path) -> str:
    if entry == "goal":
        outcome, *_ = asyncio.run(manager_mod.run_manager_loop(
            "ship the feature", 9001, str(repo), {}, _goal_args(),
        ))
        return outcome
    goal_entry = {"goal": "ship the feature", "project_id": 9001,
                  "project_dir": str(repo), "project_info": {"name": "GatedProject"}}
    defaults = {"model": "m", "max_turns": 10, "max_rounds": 1, "max_concurrent": 1}
    result = asyncio.run(dispatch_mod.run_single_goal(
        goal_entry, asyncio.Semaphore(1), 0, defaults,
        SimpleNamespace(security_review=True, dispatch_config={}),
    ))
    return result["outcome"]


@pytest.mark.parametrize("entry", ["goal", "parallel-goals"])
@pytest.mark.parametrize("review_high", [1, 0], ids=["review-blocks", "review-clean"])
def test_goal_modes_run_tasks_in_worktree_and_merge_only_through_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_high: int, entry: str,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3305)
    probe = GateProbe(repo, review_high)
    probe.install(monkeypatch)
    _patch_goal(monkeypatch, repo, task, probe)

    _run_goal(entry, repo)

    probe.assert_ran_isolated(3305)
    if review_high:
        probe.assert_default_branch_untouched(3305)
        assert probe.statuses == [(3305, "security_review_blocked", None)]
    else:
        probe.assert_merged_only_by_gate(3305)
        [(task_id, outcome, merged_sha)] = probe.statuses
        assert (task_id, outcome) == (3305, "tests_passed")
        assert merged_sha and _is_ancestor(repo, merged_sha, _master(repo))


def test_goal_stops_when_the_planner_moves_the_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner runs in the project checkout; a commit it lands on the
    default branch trips the goal's guard and no task runs or merges."""
    repo = _init_repo(tmp_path / "repo")
    task = _task(3306)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_goal(monkeypatch, repo, task, probe, planner_commits_on_master=True)

    outcome = _run_goal("goal", repo)

    assert outcome == "merge_integrity_failed"
    assert probe.agent_dirs == [], "tasks ran after the default branch moved"
    assert probe.master_at_gate == []


# ---------------------------------------------------------------------------
# dispatch-06: signals, dirty main checkout, leftover report
# ---------------------------------------------------------------------------

class _KilledBySignal(Exception):
    """Raised by the test's SIGTERM handler: the signal was NOT deferred."""


def _repo_with_task_branch(tmp_path: Path, task_id: int, files: dict[str, str]) -> Path:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", f"forge-task-{task_id}")
    _commit_files(repo, files, f"feat: task {task_id}")
    _git(repo, "checkout", "-q", "master")
    return repo


def test_sigterm_during_merge_is_deferred_and_the_merge_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo_with_task_branch(tmp_path, 3307, {"docs/a.md": "# a\n"})
    # A commit that conflicts with master, to leave a real MERGE_HEAD behind.
    _git(repo, "checkout", "-q", "-b", "conflicting")
    _commit_files(repo, {"README.md": "conflicting\n"}, "conflicting edit")
    _git(repo, "checkout", "-q", "master")
    _commit_files(repo, {"README.md": "master edit\n"}, "master edit")
    baseline = _master(repo)

    async def interrupted_merge(project_dir, task_id, branch_name, **kwargs):
        # The merge is half done (conflict, MERGE_HEAD written) when the
        # operator's SIGTERM arrives.
        subprocess.run(["git", "merge", "--no-edit", "conflicting"], cwd=repo,
                       capture_output=True, check=False)
        assert (repo / ".git" / "MERGE_HEAD").exists()
        os.kill(os.getpid(), signal.SIGTERM)
        return False

    monkeypatch.setattr(dispatch_mod, "_merge_task_branch", interrupted_merge)

    def killed(signum, frame):
        raise _KilledBySignal(f"signal {signum} was not deferred during the merge")

    previous = signal.signal(signal.SIGTERM, killed)
    try:
        async def run() -> tuple[str, str, Any]:
            guard = await dispatch_mod.DefaultBranchGuard.snapshot(str(repo))
            first = await dispatch_mod._gated_merge_task(
                repo=str(repo), branch="forge-task-3307", outcome="tests_passed",
                task_id=3307, security_review_enabled=False, guard=guard,
            )
            second = await dispatch_mod._gated_merge_task(
                repo=str(repo), branch="forge-task-3307", outcome="tests_passed",
                task_id=3308, security_review_enabled=False, guard=guard,
            )
            return first, second, guard

        first, second, guard = asyncio.run(run())
        assert signal.getsignal(signal.SIGTERM) is killed, "handler not restored"
    finally:
        signal.signal(signal.SIGTERM, previous)

    from equipa.merge_safety import shutdown_requested

    assert first == "merge_failed"
    assert not (repo / ".git" / "MERGE_HEAD").exists(), "the interrupted merge was not aborted"
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _master(repo) == baseline
    assert shutdown_requested() == signal.SIGTERM
    # Every later merge in the run is refused while shutting down.
    assert second == "merge_failed"
    assert "shutting down" in guard.outcomes[3308].reason


def test_sigterm_during_a_merge_stops_further_tasks_after_finishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-run, two tasks: SIGTERM lands while task 1 merges. Task 1's merge
    completes and is recorded; task 2 is never started and keeps its status."""
    repo = _init_repo(tmp_path / "repo")
    tasks = {3320: _task(3320), 3321: _task(3321)}
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, repo, tasks[3320], probe)
    monkeypatch.setattr(dispatch_mod, "fetch_task", lambda task_id: dict(tasks[task_id]))

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_dirs.append(project_dir)
        _commit_files(Path(project_dir), {f"src/t{task['id']}.py": "x = 1\n"}, "work")
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop", fake_dev_test_loop)
    real_merge = dispatch_mod._merge_task_branch

    async def merge_then_sigterm(*args, **kwargs):
        merged = await real_merge(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return merged

    monkeypatch.setattr(dispatch_mod, "_merge_task_branch", merge_then_sigterm)
    summary = {"project_id": 9001, "codename": "gatedproj", "total_todo": 2,
               "tasks": [{"id": 3320, "title": "a"}, {"id": 3321, "title": "b"}]}
    args = SimpleNamespace(model="m", max_turns=10, max_tasks_per_project=None,
                           security_review=True)

    def killed(signum, frame):
        raise _KilledBySignal(f"signal {signum} was not deferred during the merge")

    previous = signal.signal(signal.SIGTERM, killed)
    try:
        asyncio.run(dispatch_mod.run_project_tasks(
            summary, {"features": {"autoresearch": False}}, args,
        ))
    finally:
        signal.signal(signal.SIGTERM, previous)

    worktree_1 = str(repo / ".forge-worktrees" / "task-3320")
    assert probe.agent_dirs == [worktree_1], "task 2 started after the shutdown request"
    [(task_id, outcome, merged_sha)] = probe.statuses
    assert (task_id, outcome) == (3320, "tests_passed")
    assert merged_sha and _is_ancestor(repo, merged_sha, _master(repo))


def test_dirty_main_checkout_is_refused_not_stashed(tmp_path: Path) -> None:
    repo = _repo_with_task_branch(tmp_path, 3309, {"docs/b.md": "# b\n"})
    baseline = _master(repo)
    (repo / "README.md").write_text("operator work in progress\n")
    record = dispatch_mod.MergeAttempt()

    merged = asyncio.run(dispatch_mod._merge_task_branch(
        str(repo), 3309, "forge-task-3309", expect_artifact=False,
        merge_record=record,
    ))

    assert merged is False
    assert "not clean" in (record.reason or "")
    assert _master(repo) == baseline
    assert _git(repo, "stash", "list") == "", "the operator's work was stashed"
    assert (repo / "README.md").read_text() == "operator work in progress\n"


def test_leftover_dispatch_state_is_reported_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    worktree = repo / ".forge-worktrees" / "task-3310"
    _git(repo, "worktree", "add", "-q", "-b", "forge-task-3310", str(worktree), "master")
    (repo / "README.md").write_text("interrupted edit\n")
    _git(repo, "stash", "push", "-q", "-m", "equipa-early-term task-3310 branch-forge-task-3310")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_files(repo, {"README.md": "side\n"}, "side edit")
    _git(repo, "checkout", "-q", "master")
    _commit_files(repo, {"README.md": "main\n"}, "main edit")
    subprocess.run(["git", "merge", "--no-edit", "side"], cwd=repo,
                   capture_output=True, check=False)
    assert (repo / ".git" / "MERGE_HEAD").exists()

    monkeypatch.setattr(dispatch_mod, "fetch_tasks_by_ids", lambda ids: [_task(3310)])
    monkeypatch.setattr(dispatch_mod, "resolve_project_dir", lambda _t: str(repo))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)
    args = SimpleNamespace(yes=True, max_concurrent=1, use_flow=False,
                           security_review=False, dispatch_config={})

    asyncio.run(dispatch_mod.run_parallel_tasks([3310], args))

    out = capsys.readouterr().out
    assert "Leftover dispatch state" in out
    assert ".forge-worktrees" in out and "task-3310" in out
    assert "forge-task-3310" in out
    assert "MERGE_HEAD" in out
    assert "equipa-early-term task-3310" in out
    # Reported, never cleaned up.
    assert worktree.is_dir()
    assert _branch_sha(repo, "forge-task-3310") is not None
    assert (repo / ".git" / "MERGE_HEAD").exists()
    assert "equipa-early-term task-3310" in _git(repo, "stash", "list")


# ---------------------------------------------------------------------------
# dispatch-10 / 11 / 15 / 16
# ---------------------------------------------------------------------------

def _patch_parallel_non_git(monkeypatch, project_dir: Path, tasks: list[dict]) -> dict:
    stats = {"running": 0, "peak": 0, "ran": []}

    async def fake_loop(task, task_dir, project_context, args, config, output=None,
                        task_branch=None):
        stats["running"] += 1
        stats["peak"] = max(stats["peak"], stats["running"])
        stats["ran"].append(task["id"])
        await asyncio.sleep(0.05)
        stats["running"] -= 1
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed", 0.0, 0.0, task

    monkeypatch.setattr(dispatch_mod, "fetch_tasks_by_ids",
                        lambda ids: [t for t in tasks if t["id"] in ids])
    monkeypatch.setattr(dispatch_mod, "resolve_project_dir", lambda _t: str(project_dir))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop_with_autoresearch", fake_loop)
    monkeypatch.setattr(dispatch_mod, "update_task_status", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "record_agent_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(dispatch_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)
    return stats


def test_tasks_honours_max_concurrent_from_dispatch_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "not-a-git-repo"
    project_dir.mkdir()
    tasks = [_task(i) for i in (3311, 3312, 3313)]
    stats = _patch_parallel_non_git(monkeypatch, project_dir, tasks)
    args = SimpleNamespace(yes=True, max_concurrent=None, use_flow=False,
                           security_review=False, dispatch_config={"max_concurrent": 1})

    asyncio.run(dispatch_mod.run_parallel_tasks([3311, 3312, 3313], args))

    assert sorted(stats["ran"]) == [3311, 3312, 3313]
    assert stats["peak"] == 1, f"ran {stats['peak']} tasks at once with max_concurrent=1"


def test_tasks_with_role_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[Any] = []

    async def fake_run_parallel_tasks(task_ids, args):
        dispatched.append(task_ids)

    monkeypatch.setattr(cli_mod, "run_parallel_tasks", fake_run_parallel_tasks)
    monkeypatch.setattr(cli_mod, "fetch_tasks_by_ids", lambda ids: [])
    monkeypatch.setattr(cli_mod, "_auto_snapshot_dispatch", lambda *a, **k: None)
    args = argparse.Namespace(tasks="50", role="code-reviewer", dev_test=False,
                              dry_run=False, dispatch_config={})

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli_mod.run_mode_tasks(args))

    assert excinfo.value.code == 2
    assert dispatched == [], "--tasks ran and silently ignored --role"


def test_parse_task_ids_deduplicates_preserving_order() -> None:
    assert dispatch_mod.parse_task_ids("5,5,4-6,4") == [5, 4, 6]


@pytest.mark.parametrize("bad", ["-5", "7-3", "0", "abc", "4,,5", "3-"])
def test_parse_task_ids_rejects_malformed_parts_clearly(bad: str) -> None:
    with pytest.raises(ValueError, match=r"invalid task (id|range)"):
        dispatch_mod.parse_task_ids(bad)


def test_tasks_with_missing_ids_is_refused_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "not-a-git-repo"
    project_dir.mkdir()
    stats = _patch_parallel_non_git(monkeypatch, project_dir, [_task(3314)])
    args = SimpleNamespace(yes=True, max_concurrent=1, use_flow=False,
                           security_review=False, dispatch_config={})

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(dispatch_mod.run_parallel_tasks([3314, 999999], args))

    assert excinfo.value.code == 2
    assert stats["ran"] == [], "a subset of the requested tasks was run"


def test_tasks_cross_project_refusal_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "not-a-git-repo"
    project_dir.mkdir()
    stats = _patch_parallel_non_git(
        monkeypatch, project_dir, [_task(3315, project_id=1), _task(3316, project_id=2)],
    )
    args = SimpleNamespace(yes=True, max_concurrent=1, use_flow=False,
                           security_review=False, dispatch_config={})

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(dispatch_mod.run_parallel_tasks([3315, 3316], args))

    assert excinfo.value.code == 2
    assert stats["ran"] == []


def _tasks_db(tmp_path: Path, rows: list[tuple[int, int, str]]) -> Path:
    db_path = tmp_path / "claims.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER, created_at TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # 3318 belongs to another project: the whole claim is rejected.
        ([(3317, 23, "2999-01-01 00:00:00"), (3318, 77, "2999-01-01 00:00:00")], []),
        # 3317 predates the planner run: a pre-existing id, rejected.
        ([(3317, 23, "2000-01-01 00:00:00"), (3318, 23, "2999-01-01 00:00:00")], []),
        # Both created by this run in this project: accepted.
        ([(3317, 23, "2999-01-01 00:00:00"), (3318, 23, "2999-01-01 00:00:00")], [3317, 3318]),
    ],
    ids=["other-project", "pre-existing", "valid"],
)
def test_planner_tasks_created_claim_is_validated_against_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[int, int, str]], expected: list[int],
) -> None:
    db_path = _tasks_db(tmp_path, rows)

    @contextlib.contextmanager
    def fake_build_cli_command(*args, **kwargs):
        yield ["fake-claude"]

    async def fake_run_agent(cmd):
        return {"success": True, "cost": 0.0, "duration": 0.0,
                "result_text": "Planned.\nTASKS_CREATED: 3317, 3318\n"}

    monkeypatch.setattr(manager_mod, "build_planner_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(manager_mod, "build_cli_command", fake_build_cli_command)
    monkeypatch.setattr(manager_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager_mod, "get_role_turns", lambda *a, **k: 10)
    monkeypatch.setattr(manager_mod, "get_db_connection",
                        lambda *a, **k: sqlite3.connect(db_path), raising=False)

    _, task_ids = asyncio.run(manager_mod.run_planner_agent(
        "goal", 23, str(tmp_path), {}, SimpleNamespace(model="m"),
    ))

    assert task_ids == expected


# ---------------------------------------------------------------------------
# 3107 review N1: --task retry path and AttemptCleanupError
# ---------------------------------------------------------------------------

def test_task_retry_cleanup_failure_blocks_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def failing_loop(task, project_dir, project_context, args, output=None):
        attempts.append(1)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    async def broken_cleanup(*args, **kwargs):
        raise dispatch_mod.AttemptCleanupError(
            "no single trusted default branch in this repository",
        )

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", failing_loop)
    monkeypatch.setattr(cli_mod, "cleanup_failed_attempt", broken_cleanup)
    args = SimpleNamespace(dispatch_config={
        "features": {"autoresearch": True}, "autoresearch_max_retries": 3,
    })

    result, cycles, outcome = asyncio.run(cli_mod._run_dev_test_mode(
        _task(3319), str(tmp_path), {}, args,
    ))

    assert outcome == "attempt_cleanup_failed"
    assert len(attempts) == 1, "retried on top of a failed reset"
