"""Task #3126: fix-forward of task #3119's security review (R3119-01..06).

Each test fails on the applied 3119 branch on its assertion and passes after
the fix; tests named ``..._control`` pin behaviour that must not change.

* R3119-01 (HIGH): orchestrator git for a project nested in another
  repository ran from the project's sub-directory, where an agent-writable
  ``diff.relative=true`` in the shared repo config narrowed ``git diff`` to
  that sub-directory. A branch changing code OUTSIDE it plus a README inside
  it read as a doc-only change: no review, merged. Now every gate / merge /
  hazard-scan git call runs at the work-tree root, the hardened helper pins
  ``diff.relative=false`` and passes ``--no-relative``, and the hazard scan
  refuses ``diff.relative`` / ``status.relativePaths``.
* R3119-02 (MEDIUM): ``_is_git_repo`` failed open: any git error made a real
  repository "not git", so its agent ran ungated in the main checkout. Now a
  directory with a ``.git`` (itself or an ancestor) that git cannot read is
  refused; only a positive "not a git repository" with no ``.git`` anywhere
  up the tree is "not git".
* R3119-03 (LOW): goal planner / evaluator load only the MCP servers
  EQUIPA passes (``--strict-mcp-config``).
* R3119-04 (LOW): a project or goal that raised counts as a refusal.
* R3119-05 (LOW): a directory at a nested project's ``.forge-state.json``
  no longer crashes the task loop.
* R3119-06 (INFO): a nested agent directory that escapes the task worktree
  through a symlink is refused.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.cli as cli_mod
import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
import equipa.manager as manager_mod
import equipa.merge_safety as merge_safety_mod
from equipa.merge_integrity import find_repo_execution_hazards, index_flag_problem
from equipa.security_gate import get_changed_files_for_branch, is_doc_only_diff

from test_dispatch_modes_gated_3112 import (
    GateProbe,
    _branch_sha,
    _cli_args,
    _commit_files,
    _git,
    _init_repo,
    _master,
    _patch_cli_task_basics,
    _task,
)
from test_goal_isolation_3119 import (
    _captured_agent_command,
    _committing_loop,
    _nested_project,
    _patch_auto_run,
    _run_project,
    project_dirs,  # noqa: F401 - pytest fixture
)

# equipa.dispatch.EXIT_DISPATCH_REFUSED
EXIT_REFUSED = 2
# The payload an agent smuggles outside the nested project's directory.
OUTSIDE_CODE = "import os\nos.system('echo FAKE-SENTINEL-3126')\n"
# git subcommands whose result depends on the directory git runs in.
PATH_SCOPED_SUBCOMMANDS = frozenset({
    "add", "checkout", "clean", "commit", "diff", "log", "ls-files", "merge",
    "rebase", "reset", "show", "stash", "status",
})


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """A deferred SIGTERM must not leak into other tests."""
    merge_safety_mod.reset_shutdown_request()
    yield
    merge_safety_mod.reset_shutdown_request()


def _branch_outside_subdir(tmp_path: Path) -> Path:
    """Repo whose ``feature`` branch changes ``other/code.py`` and ``sub/README.md``."""
    repo = _init_repo(tmp_path / "outer")
    _commit_files(repo, {"sub/app.py": "print('sub')\n"}, "add sub project")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit_files(
        repo,
        {"other/code.py": OUTSIDE_CODE, "sub/README.md": "docs\n"},
        "code outside sub plus a doc inside it",
    )
    _git(repo, "checkout", "-q", "master")
    return repo


def _corrupt_repo_config(repo: Path) -> None:
    """An unterminated section header: every git command in the repo fails."""
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("[core\n")


# ---------------------------------------------------------------------------
# R3119-01: diff.relative from a nested project directory
# ---------------------------------------------------------------------------

def test_gate_diff_from_a_nested_dir_ignores_repo_diff_relative(tmp_path: Path) -> None:
    repo = _branch_outside_subdir(tmp_path)
    _git(repo, "config", "diff.relative", "true")

    changed = asyncio.run(get_changed_files_for_branch(
        str(repo / "sub"), base_ref="master", head_ref="feature",
    ))

    assert sorted(changed) == ["other/code.py", "sub/README.md"], (
        f"gate diff from the sub-directory saw only {changed}"
    )
    assert not is_doc_only_diff(changed)


@pytest.mark.parametrize(
    "args",
    [
        ["diff", "--name-only", "master...feature"],
        ["log", "--name-only", "--format=", "master..feature"],
        ["show", "--name-only", "--format=", "feature"],
    ],
    ids=["diff", "log", "show"],
)
def test_hardened_git_ignores_diff_relative_in_a_subdirectory(
    tmp_path: Path, args: list[str],
) -> None:
    repo = _branch_outside_subdir(tmp_path)
    _git(repo, "config", "diff.relative", "true")

    result = git_ops_mod.git_run(args, repo / "sub")

    assert result.returncode == 0, result.stderr
    assert "other/code.py" in result.stdout.split(), (
        f"git {args[0]} from the sub-directory hid other/code.py: {result.stdout!r}"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [("diff.relative", "true"), ("status.relativePaths", "true")],
)
def test_hazard_scan_refuses_relative_path_config(
    tmp_path: Path, key: str, value: str,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    assert asyncio.run(find_repo_execution_hazards(repo)) == []

    _git(repo, "config", key, value)

    hazards = asyncio.run(find_repo_execution_hazards(repo))
    # git config --list reports keys lowercased.
    assert any(key.lower() in hazard for hazard in hazards), hazards


def test_hazard_scan_accepts_relative_paths_switched_off_control(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "config", "diff.relative", "false")
    _git(repo, "config", "status.relativePaths", "false")

    assert asyncio.run(find_repo_execution_hazards(repo)) == []


def test_index_flag_check_sees_entries_outside_a_nested_dir(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit_files(repo, {"sub/app.py": "x = 1\n", "lib/core.py": "y = 2\n"}, "files")
    _git(repo, "update-index", "--skip-worktree", "lib/core.py")

    problem = asyncio.run(index_flag_problem(repo / "sub"))

    assert problem is not None and "lib/core.py" in problem, (
        f"skip-worktree entry outside the sub-directory was not seen: {problem!r}"
    )


def test_gated_merge_refuses_nested_project_with_diff_relative(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    outer, project = _nested_project(tmp_path)
    _git(outer, "checkout", "-q", "-b", "forge-task-3401")
    _commit_files(
        outer,
        {"lib/evil.py": OUTSIDE_CODE, "apps/web/README.md": "docs\n"},
        "code outside the nested project",
    )
    _git(outer, "checkout", "-q", "master")
    _git(outer, "config", "diff.relative", "true")
    baseline = _master(outer)

    status = asyncio.run(dispatch_mod._gated_merge_task(
        repo=str(project), branch="forge-task-3401", outcome="tests_passed",
        task_id=3401,
    ))

    assert status == "blocked"
    assert _master(outer) == baseline, "code outside the nested project was merged"
    assert "diff.relative" in capsys.readouterr().out


def _escaping_agent(probe: GateProbe):
    """Commits a README in the nested project and code outside it."""

    async def dev_test_loop(task, project_dir, project_context, args, output=None, **_):
        probe.agent_dirs.append(project_dir)
        agent_dir = Path(project_dir)
        (agent_dir / "README.md").write_text("only docs here\n")
        outside = agent_dir.parents[1] / "lib" / "evil.py"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(OUTSIDE_CODE)
        _git(agent_dir, "add", "-A", ":/")
        _git(agent_dir, "commit", "-q", "-m", "docs")
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    return dev_test_loop


def _silent_reviewer(reviews: list[str]):
    """A reviewer that runs but writes no artifact."""

    async def run_security_review(task, project_dir, project_context, args,
                                  output=None, stable_project_dir=None):
        reviews.append(project_dir)

    return run_security_review


class GitCwdRecorder:
    """Every hardened git call EQUIPA makes: ``(argv, cwd)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self
        real_run = git_ops_mod._run_with_env
        real_spawn = asyncio.create_subprocess_exec

        def run_with_env(args_list, cwd, timeout, env=None, *, text=True):
            recorder.calls.append((list(args_list), Path(cwd)))
            return real_run(args_list, cwd, timeout, env, text=text)

        async def spawn(*argv, **kwargs):
            if argv and argv[0] == "git":
                recorder.calls.append((list(argv), Path(kwargs["cwd"])))
            return await real_spawn(*argv, **kwargs)

        monkeypatch.setattr(git_ops_mod, "_run_with_env", run_with_env)
        monkeypatch.setattr(git_ops_mod.asyncio, "create_subprocess_exec", spawn)

    def path_scoped_calls_in(self, directories: set[Path]) -> list[str]:
        found = []
        for argv, cwd in self.calls:
            index = git_ops_mod._git_subcommand_index(argv[1:])
            if index is None:
                continue
            subcommand, rest = argv[1 + index], argv[2 + index:]
            if subcommand == "stash" and rest[:1] == ["list"]:
                continue  # ``stash list`` prints reflog entries, no paths
            if subcommand in PATH_SCOPED_SUBCOMMANDS and cwd.resolve() in directories:
                found.append(f"git {subcommand} in {cwd}")
        return found


def test_nested_project_code_outside_subdir_is_reviewed_and_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    """The reviewer's probe as a dispatch: nested project + diff.relative."""
    outer, project = _nested_project(tmp_path)
    task = _task(3402)
    probe = GateProbe(outer, 0)
    probe.install(monkeypatch)
    reviews: list[str] = []
    monkeypatch.setattr(dispatch_mod, "run_security_review", _silent_reviewer(reviews))
    _patch_auto_run(monkeypatch, task, probe, _escaping_agent(probe))
    recorder = GitCwdRecorder()
    recorder.install(monkeypatch)
    project_dirs["nestedproj"] = str(project)
    _git(outer, "config", "diff.relative", "true")

    _run_project("nestedproj", 3402)

    worktree = project / ".forge-worktrees" / "task-3402"
    assert probe.agent_dirs == [str(worktree / "apps" / "web")]
    assert len(reviews) == 1, "the change was treated as doc-only and never reviewed"
    assert probe.statuses == [(3402, "security_review_blocked", None)]
    assert _master(outer) == probe.baseline, "unreviewed code reached master"
    assert _branch_sha(outer, "forge-task-3402") not in (None, probe.baseline)
    nested_dirs = {project.resolve(), (worktree / "apps" / "web").resolve()}
    assert recorder.path_scoped_calls_in(nested_dirs) == [], (
        "orchestrator git ran from the nested sub-directory"
    )


def test_nested_project_merge_runs_at_the_work_tree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    """A clean review: checkout / merge run at the root, not the sub-directory."""
    outer, project = _nested_project(tmp_path)
    task = _task(3407)
    probe = GateProbe(outer, 0)
    probe.install(monkeypatch)
    _patch_auto_run(monkeypatch, task, probe, _committing_loop(probe))
    recorder = GitCwdRecorder()
    recorder.install(monkeypatch)
    project_dirs["nestedproj"] = str(project)

    _run_project("nestedproj", 3407)

    [(task_id, outcome, merged_sha)] = probe.statuses
    assert (task_id, outcome) == (3407, "tests_passed") and merged_sha
    assert merged_sha == _master(outer)
    worktree = project / ".forge-worktrees" / "task-3407"
    nested_dirs = {project.resolve(), (worktree / "apps" / "web").resolve()}
    assert recorder.path_scoped_calls_in(nested_dirs) == [], (
        "orchestrator git ran from the nested sub-directory"
    )


def test_nested_project_retry_cleanup_runs_at_the_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3119-07 variant: ``git clean`` from the sub-directory left untracked
    files elsewhere in the worktree for the next attempt."""
    outer, project = _nested_project(tmp_path)
    worktree = tmp_path / "wt"
    _git(outer, "worktree", "add", "-q", "-b", "forge-task-3403", str(worktree))
    base_sha = _git(worktree, "rev-parse", "HEAD")
    leftover = worktree / "lib" / "leftover.py"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(OUTSIDE_CODE)
    monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)

    asyncio.run(dispatch_mod.cleanup_failed_attempt(
        3403, str(worktree / "apps" / "web"), [], output=[], base_sha=base_sha,
    ))

    assert not leftover.exists(), "untracked file outside the sub-directory survived"


class _NoDb:
    """Stands in for the TheForge connection in ``cleanup_failed_attempt``."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return None

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# R3119-02: _is_git_repo fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["root", "nested"])
def test_unreadable_repository_is_refused_not_treated_as_non_git(
    tmp_path: Path, target: str,
) -> None:
    outer, project = _nested_project(tmp_path)
    _corrupt_repo_config(outer)
    path = outer if target == "root" else project

    with pytest.raises(RuntimeError, match="cannot be read by git"):
        git_ops_mod._is_git_repo(path)


def test_broken_git_file_is_refused_not_treated_as_non_git(tmp_path: Path) -> None:
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {tmp_path / 'gone' / '.git'}\n")

    with pytest.raises(RuntimeError, match="cannot be read by git"):
        git_ops_mod._is_git_repo(worktree)


def test_directories_without_any_git_are_not_git_control(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_ops_mod._is_git_repo(plain) is False
    assert git_ops_mod._is_git_repo(tmp_path / "missing") is False


def test_auto_run_refuses_a_project_whose_repository_git_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3404)
    probe = GateProbe(repo, 0)
    probe.install(monkeypatch)

    async def records_where_it_ran(task, project_dir, project_context, args,
                                   output=None, **_):
        probe.agent_dirs.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    _patch_auto_run(monkeypatch, task, probe, records_where_it_ran)
    project_dirs["brokenrepo"] = str(repo)
    _corrupt_repo_config(repo)

    result = _run_project("brokenrepo", 3404)

    assert probe.agent_dirs == [], "the agent ran ungated in the main checkout"
    assert probe.statuses == []
    assert result["tasks_attempted"] == 0
    assert len(result["refusals"]) == 1
    assert "cannot be read by git" in result["refusals"][0]


def test_single_task_cli_refuses_a_repository_git_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    task = _task(3405)
    probe = GateProbe(repo, 0)
    reviews: list[str] = []
    monkeypatch.setattr(cli_mod, "run_security_review", _silent_reviewer(reviews))
    _patch_cli_task_basics(monkeypatch, repo, task, probe)

    async def fake_dev_test_loop(task, project_dir, project_context, args, output=None):
        probe.agent_dirs.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", fake_dev_test_loop)
    _corrupt_repo_config(repo)

    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(cli_mod.run_mode_task(_cli_args(task=3405)))

    assert exit_info.value.code == EXIT_REFUSED
    assert probe.agent_dirs == [], "the agent ran ungated in the main checkout"


# ---------------------------------------------------------------------------
# LOWs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["planner", "evaluator"])
def test_goal_agents_load_only_the_mcp_servers_equipa_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    """R3119-03: no user / project MCP server reaches the read-only agents."""
    args = SimpleNamespace(model="m", max_turns=10, dispatch_config={})

    async def run() -> None:
        if role == "planner":
            await manager_mod.run_planner_agent("goal", 9001, str(tmp_path), {}, args)
        else:
            await manager_mod.run_evaluator_agent(
                "goal", 9001, str(tmp_path), {}, [], [], args,
            )

    cmd = _captured_agent_command(monkeypatch, run)

    assert "--strict-mcp-config" in cmd


def test_project_that_raises_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3119-04: ``--auto-run`` exits non-zero when a project crashed."""

    async def crashes(*args, **kwargs):
        raise OSError("worktree setup failed")

    monkeypatch.setattr(dispatch_mod, "run_project_tasks", crashes)
    summary = {"project_id": 9001, "codename": "crashy", "total_todo": 1}

    result = asyncio.run(dispatch_mod.run_project_dispatch(
        summary, asyncio.Semaphore(1), {}, SimpleNamespace(),
    ))

    refusals = dispatch_mod.collect_refusals([result])
    assert len(refusals) == 1 and "worktree setup failed" in refusals[0]


def test_goal_that_raises_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3119-04: ``--parallel-goals`` exits non-zero when a goal crashed."""

    async def crashes(*args, **kwargs):
        raise OSError("merge crashed")

    monkeypatch.setattr(dispatch_mod, "run_manager_loop", crashes)
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    goal = {"goal": "g", "project_id": 9001, "project_dir": "/nonexistent",
            "project_info": {"name": "Crashy"}}
    defaults = {"model": "m", "max_turns": 5, "max_rounds": 1}

    result = asyncio.run(dispatch_mod.run_single_goal(
        goal, asyncio.Semaphore(1), 0, defaults, SimpleNamespace(),
    ))

    refusals = dispatch_mod.collect_refusals([result])
    assert len(refusals) == 1 and "merge crashed" in refusals[0]


def test_collect_refusals_counts_a_raised_exception() -> None:
    """R3119-04: ``gather(..., return_exceptions=True)`` entries are refusals."""
    refusals = dispatch_mod.collect_refusals([RuntimeError("boom"), {"refusals": []}])

    assert len(refusals) == 1 and "boom" in refusals[0]


def test_forge_state_directory_in_nested_project_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dirs: dict,
) -> None:
    """R3119-05: the task is still recorded, reviewed and gated."""
    outer, project = _nested_project(tmp_path)
    task = _task(3406)
    probe = GateProbe(outer, 1)
    probe.install(monkeypatch)

    async def leaves_a_directory(task, project_dir, project_context, args,
                                 output=None, **_):
        probe.agent_commits(project_dir, task["id"])
        (Path(project_dir) / ".forge-state.json").mkdir()
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    _patch_auto_run(monkeypatch, task, probe, leaves_a_directory)
    project_dirs["nestedproj"] = str(project)

    result = _run_project("nestedproj", 3406)

    assert probe.statuses == [(3406, "security_review_blocked", None)]
    assert result["tasks_attempted"] == 1
    assert _master(outer) == probe.baseline


def test_nested_agent_dir_escaping_the_worktree_is_refused(tmp_path: Path) -> None:
    """R3119-06: a symlinked path component must not lead out of the worktree."""
    outer = _init_repo(tmp_path / "outer")
    _commit_files(outer, {"apps/web/app.py": "print('web')\n"}, "nested project")
    project = outer / "apps" / "web"
    worktree = tmp_path / "wt"
    _git(outer, "worktree", "add", "-q", "--detach", str(worktree))
    elsewhere = tmp_path / "elsewhere" / "web"
    elsewhere.mkdir(parents=True)
    moved = worktree / "apps"
    for child in sorted(moved.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    moved.rmdir()
    moved.symlink_to(elsewhere.parent, target_is_directory=True)

    agent_dir, problem = dispatch_mod.project_dir_in_worktree(str(project), str(worktree))

    assert agent_dir is None, f"agent would run in {agent_dir}, outside the worktree"
    assert "outside" in problem
