"""Task #3119: edge cases around the dispatch-07 / nested-project / LOW fixes.

Complements tests/test_goal_isolation_3119.py (tester pass). Covers shapes
the main file does not pin:

- the read-only ``--tools`` allowlist is the LAST option of the goal agents'
  command (``--tools <tools...>`` is variadic in the Claude CLI, so anything
  positional after it would be swallowed as a tool name), and a command that
  already selects its tools is refused rather than given a second list;
- nested (LIFO) merge signal shields, and a signal that arrives after the
  first of two overlapping merges finished while the second is still
  running: it must be deferred for the second, not delivered to the
  operator's handler mid-merge;
- the planner claim check with an offset-aware ``run_started_at`` (a naive
  ``created_at`` used to raise ``TypeError`` against it);
- ``collect_refusals`` / ``run_single_goal`` refusal bookkeeping;
- ``project_dir_in_worktree`` for root, nested, symlinked and untracked
  projects;
- ``load_config`` updating the ``PROJECT_DIRS`` dict that equipa.tasks,
  equipa.git_ops and equipa.output imported by name.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timedelta, timezone
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
import equipa.output as output_mod
import equipa.single_agent_guard as guard_mod
import equipa.tasks as tasks_mod

from test_dispatch_modes_gated_3112 import _commit_files, _git, _init_repo

RUN_STARTED_NAIVE = "2026-09-30 10:00:00"
READ_ONLY_TOOLS = "Read,Glob,Grep"


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


@pytest.fixture
def operator_sigterm_handler():
    """Install a recording SIGTERM handler for the test, restore the old one."""
    received: list[int] = []

    def operator_handler(signum, _frame):
        received.append(signum)

    previous = signal.signal(signal.SIGTERM, operator_handler)
    try:
        yield operator_handler, received
    finally:
        signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------------------
# 1. Read-only goal agents: shape of the command line
# ---------------------------------------------------------------------------

def test_restrict_to_read_only_tools_appends_last_and_leaves_input_alone() -> None:
    original = ["/usr/bin/claude", "-p", "plan", "--add-dir", "/proj"]

    restricted = manager_mod.restrict_to_read_only_tools(original)

    # R3119-03 (task #3126): --strict-mcp-config comes before the allowlist.
    assert restricted[:-3] == original
    assert restricted[-3:] == ["--strict-mcp-config", "--tools", READ_ONLY_TOOLS]
    assert original == ["/usr/bin/claude", "-p", "plan", "--add-dir", "/proj"]


def test_restrict_to_read_only_tools_refuses_a_command_with_its_own_tools() -> None:
    with pytest.raises(ValueError, match="--tools"):
        manager_mod.restrict_to_read_only_tools(
            ["/usr/bin/claude", "-p", "plan", "--tools", "default"],
        )


@pytest.mark.parametrize("role", ["planner", "evaluator"])
def test_goal_agent_command_ends_with_the_read_only_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    """Nothing follows the variadic ``--tools`` value in the real command."""
    captured: list[list[str]] = []

    async def fake_run_agent(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return {"success": False, "errors": ["stubbed"], "duration": 0.0}

    monkeypatch.setattr(manager_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(manager_mod, "build_planner_prompt", lambda *a, **k: "plan it")
    monkeypatch.setattr(manager_mod, "build_evaluator_prompt", lambda *a, **k: "judge it")
    args = SimpleNamespace(model="m", max_turns=10, dispatch_config={})

    async def run() -> None:
        if role == "planner":
            await manager_mod.run_planner_agent("goal", 9001, str(tmp_path), {}, args)
        else:
            await manager_mod.run_evaluator_agent(
                "goal", 9001, str(tmp_path), {}, [], [], args,
            )

    asyncio.run(run())

    [cmd] = captured
    assert cmd[-2:] == ["--tools", READ_ONLY_TOOLS]
    assert cmd.count("--tools") == 1
    prompt = cmd[cmd.index("-p") + 1]
    assert str(tmp_path) in prompt, "the project dir left the agent's prompt"


# ---------------------------------------------------------------------------
# 3. LOW: merge signal shields, beyond the out-of-order overlap
# ---------------------------------------------------------------------------

def test_nested_shields_keep_deferring_until_the_outer_exits_control(
    tmp_path: Path, operator_sigterm_handler,
) -> None:
    operator_handler, received_by_operator = operator_sigterm_handler
    repo = _init_repo(tmp_path / "repo")
    handler_between: list[Any] = []

    async def nested_merges():
        async with merge_safety_mod.MergeSignalShield(repo, context="outer") as outer:
            async with merge_safety_mod.MergeSignalShield(repo, context="inner") as inner:
                pass
            handler_between.append(signal.getsignal(signal.SIGTERM))
        return outer, inner

    outer, inner = asyncio.run(nested_merges())

    assert handler_between[0] is not operator_handler, (
        "the inner shield restored the operator handler while the outer merge ran"
    )
    assert signal.getsignal(signal.SIGTERM) is operator_handler
    assert outer.received is None and inner.received is None
    assert received_by_operator == []
    assert merge_safety_mod.shutdown_requested() is None


def test_signal_after_the_first_overlapping_merge_ends_is_deferred_for_the_second(
    tmp_path: Path, operator_sigterm_handler,
) -> None:
    """Merge A ends, merge B is still in git: a SIGTERM now belongs to B."""
    operator_handler, received_by_operator = operator_sigterm_handler
    repo = _init_repo(tmp_path / "repo")
    received_mid_merge: list[int] = []

    async def overlapping_merges():
        first = merge_safety_mod.MergeSignalShield(repo, context="merge A")
        second = merge_safety_mod.MergeSignalShield(repo, context="merge B")
        await first.__aenter__()
        await second.__aenter__()
        await first.__aexit__(None, None, None)
        os.kill(os.getpid(), signal.SIGTERM)
        received_mid_merge.extend(received_by_operator)
        await second.__aexit__(None, None, None)
        return first, second

    first, second = asyncio.run(overlapping_merges())

    assert received_mid_merge == [], (
        "SIGTERM reached the operator handler while merge B was still running"
    )
    assert first.received is None
    assert second.received == signal.SIGTERM
    assert merge_safety_mod.shutdown_requested() == signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is operator_handler


def test_a_later_shield_restores_a_handler_installed_between_merges_control(
    tmp_path: Path, operator_sigterm_handler,
) -> None:
    repo = _init_repo(tmp_path / "repo")

    def replacement_handler(_signum, _frame):
        return None

    async def one_merge(context: str) -> None:
        async with merge_safety_mod.MergeSignalShield(repo, context=context):
            pass

    asyncio.run(one_merge("merge A"))
    signal.signal(signal.SIGTERM, replacement_handler)
    asyncio.run(one_merge("merge B"))

    assert signal.getsignal(signal.SIGTERM) is replacement_handler


# ---------------------------------------------------------------------------
# 3. LOW: planner claim timestamps with an offset-aware run start
# ---------------------------------------------------------------------------

class _Rows:
    def __init__(self, created_at: Any) -> None:
        self._created_at = created_at

    def fetch_tasks_by_ids(self, ids):
        return [{"id": 7, "project_id": 9001, "created_at": self._created_at}]


def _verdict(created_at: Any, run_started_at: Any):
    return guard_mod.validate_tasks_created_claim(
        stdout="TASKS_CREATED: 7", run_started_at=run_started_at,
        expected_project_id=9001, db=_Rows(created_at),
    )


@pytest.mark.parametrize(("run_started_at", "created_at", "accepted"), [
    ("2026-09-30T10:00:00Z", "2026-09-30 10:00:05", True),
    ("2026-09-30T10:00:00Z", "2026-09-30 09:59:59", False),
    ("2026-09-30T12:00:00+02:00", "2026-09-30 10:00:05", True),
    ("2026-09-30T12:00:00+02:00", "2026-09-30T09:59:59z", False),
    (datetime(2026, 9, 30, 12, 0, tzinfo=timezone(timedelta(hours=2))),
     datetime(2026, 9, 30, 10, 0, 5), True),
], ids=["z-start-naive-after", "z-start-naive-before", "offset-start-naive-after",
        "offset-start-lower-z-before", "aware-datetime-start-naive-datetime"])
def test_claim_check_compares_aware_and_naive_timestamps_in_utc(
    run_started_at: Any, created_at: Any, accepted: bool,
) -> None:
    verdict = _verdict(created_at, run_started_at)

    assert verdict.is_valid is accepted, verdict


def test_lowercase_z_suffix_parses_as_utc() -> None:
    parsed = guard_mod._parse_iso_timestamp("2026-09-30T10:00:05z")

    assert parsed == datetime(2026, 9, 30, 10, 0, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("created_at", ["", "   ", 1727690405],
                         ids=["empty", "blank", "epoch-int"])
def test_unreadable_created_at_is_rejected(created_at: Any) -> None:
    verdict = _verdict(created_at, RUN_STARTED_NAIVE)

    assert not verdict.is_valid
    assert verdict.invalid_ids == (7,)


# ---------------------------------------------------------------------------
# 3. LOW: refusal bookkeeping for --auto-run and --parallel-goals
# ---------------------------------------------------------------------------

def test_collect_refusals_labels_each_refusal_and_counts_crashes() -> None:
    # R3119-04 (task #3126): a crash returned by gather() used to be ignored,
    # so the run exited 0; it is now a refusal of its own.
    results = [
        {"codename": "alpha", "refusals": ["task #1 worktree_refused: stale branch"]},
        RuntimeError("goal crashed"),
        {"project_name": "Beta", "refusals": ["goal stopped: merge_integrity_failed"]},
        {"codename": "gamma", "refusals": []},
        {"codename": "delta"},
        {"refusals": ["no directory mapped for the project"]},
    ]

    assert dispatch_mod.collect_refusals(results) == [
        "alpha: task #1 worktree_refused: stale branch",
        "exception: RuntimeError: goal crashed",
        "Beta: goal stopped: merge_integrity_failed",
        "?: no directory mapped for the project",
    ]


@pytest.mark.parametrize(("outcome", "refused"), [
    ("merge_integrity_failed", True),
    ("worktree_refused", True),
    ("goal_complete", False),
    ("interrupted", False),
])
def test_run_single_goal_records_a_refusal_only_for_refused_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcome: str, refused: bool,
) -> None:
    async def fake_manager_loop(*args, **kwargs):
        return outcome, 1, [], [], 0.0, 0.0

    monkeypatch.setattr(dispatch_mod, "run_manager_loop", fake_manager_loop)
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    goal = {"goal": "ship it", "project_id": 9001, "project_dir": "/nowhere",
            "project_info": {"name": "Gated"}}
    defaults = {"model": "m", "max_turns": 10, "max_rounds": 1}

    async def run() -> dict:
        return await dispatch_mod.run_single_goal(
            goal, asyncio.Semaphore(1), 0, defaults, SimpleNamespace(),
        )

    result = asyncio.run(run())

    expected = [f"goal stopped: {outcome}"] if refused else []
    assert result.get("refusals") == expected
    assert bool(dispatch_mod.collect_refusals([result])) is refused


# ---------------------------------------------------------------------------
# 2. Nested projects: where the agent runs inside the task worktree
# ---------------------------------------------------------------------------

def _outer_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    outer = _init_repo(tmp_path / "outer")
    _commit_files(outer, {"apps/web/app.py": "print('web')\n",
                          ".gitignore": "scratch/\n"}, "add nested project")
    worktree = tmp_path / "wt"
    _git(outer, "worktree", "add", "-q", "-b", "forge-task-1", str(worktree))
    return outer, worktree


def test_project_at_the_repository_root_runs_at_the_worktree_root(
    tmp_path: Path,
) -> None:
    outer, worktree = _outer_with_worktree(tmp_path)

    assert dispatch_mod.project_dir_in_worktree(str(outer), str(worktree)) == (
        str(worktree), "",
    )


def test_nested_project_runs_in_its_subdirectory_of_the_worktree(
    tmp_path: Path,
) -> None:
    outer, worktree = _outer_with_worktree(tmp_path)

    agent_dir, problem = dispatch_mod.project_dir_in_worktree(
        str(outer / "apps" / "web"), str(worktree),
    )

    assert (agent_dir, problem) == (str(worktree / "apps" / "web"), "")


def test_symlinked_nested_project_resolves_to_its_worktree_subdirectory(
    tmp_path: Path,
) -> None:
    outer, worktree = _outer_with_worktree(tmp_path)
    link = tmp_path / "links" / "web"
    link.parent.mkdir()
    link.symlink_to(outer / "apps" / "web", target_is_directory=True)

    agent_dir, problem = dispatch_mod.project_dir_in_worktree(str(link), str(worktree))

    assert (agent_dir, problem) == (str(worktree / "apps" / "web"), "")


def test_untracked_or_non_git_project_has_no_directory_in_the_worktree(
    tmp_path: Path,
) -> None:
    outer, worktree = _outer_with_worktree(tmp_path)
    ignored = outer / "scratch" / "proj"
    ignored.mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()

    ignored_dir, ignored_problem = dispatch_mod.project_dir_in_worktree(
        str(ignored), str(worktree),
    )
    plain_dir, plain_problem = dispatch_mod.project_dir_in_worktree(
        str(plain), str(worktree),
    )

    assert ignored_dir is None and "not tracked" in ignored_problem
    assert plain_dir is None and "not inside a git work tree" in plain_problem


def test_git_toplevel_names_the_enclosing_repository(tmp_path: Path) -> None:
    outer, _worktree = _outer_with_worktree(tmp_path)
    readme = outer / "README.md"

    assert git_ops_mod.git_toplevel(outer / "apps" / "web") == outer.resolve()
    assert git_ops_mod.git_toplevel(readme) is None, "a file is not a work tree"


# ---------------------------------------------------------------------------
# 4. PROJECT_DIRS loaded by the CLI reach every module that imported it
# ---------------------------------------------------------------------------

def test_load_config_updates_the_dict_other_modules_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    project_dirs["stale"] = "/old/path"
    (tmp_path / "forge_config.json").write_text(
        json.dumps({"project_dirs": {"GatedProj": str(tmp_path / "gated")}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "__file__", str(tmp_path / "equipa" / "cli.py"))

    cli_mod.load_config()

    expected = {"gatedproj": str(tmp_path / "gated")}
    assert constants_mod.PROJECT_DIRS == expected, "stale entries survived a reload"
    for module in (tasks_mod, git_ops_mod, output_mod, cli_mod):
        assert module.PROJECT_DIRS == expected, (
            f"{module.__name__}.PROJECT_DIRS does not see the loaded config"
        )
