"""Task #3158 (FF-3155) — read-only git the orchestrator runs in a task worktree.

R3155-01 named the cleanup stash; the task asked for every git command the
orchestrator runs inside an agent's worktree. While the agent runs and right
after, the dev-test loop (tester diff context, no-change check), the
claimed-file check, the progress monitor and the reviewer's tree snapshot
ran ``git diff`` / ``git status`` / ``git ls-files`` there through
``git_run`` / ``git_run_async``, by discovery. Hashing a stat-dirty file ran
the clean filter the agent had planted (repository config written by a plain
``git config`` in the worktree, selected by ``.gitattributes`` or
``info/attributes``), inside the orchestrator.

Every check plants such a driver, leaves a same-size edit (git must hash it)
and asserts the driver never ran while git still reported the change.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import equipa.git_ops as git_ops_mod
from equipa.git_ops import git_run, git_run_async
from equipa.loops import _capture_git_diff_context, _git_diff_is_empty
from equipa.merge_integrity import snapshot_reviewed_tree
from equipa.monitoring import _check_git_changes
from equipa.parsing import verify_files_changed

from test_dispatch_modes_gated_3112 import _git, _init_repo
from test_worktree_cleanup_filters_3158 import (
    NESTED,
    PLANTS,
    Drivers,
    _define_filter,
    _leave_unsaved_work,
    _plant,
    _plant_nested_repository,
    _task_worktree,
)

# (args, text every correct answer contains)
READING_CALLS = (
    (["diff", "HEAD"], "+SEED"),
    (["diff", "--name-only", "HEAD"], "README.md"),
    (["diff", "--stat"], "README.md"),
    (["diff-files", "--name-only"], "README.md"),
    (["status", "--porcelain"], " M README.md"),
    (["status", "--short", "--ignore-submodules=all"], " M README.md"),
    (["ls-files", "-m"], "README.md"),
)


def _call(args: list[str], cwd: Path, runner: str) -> subprocess.CompletedProcess:
    if runner == "sync":
        return git_run(args, cwd, timeout=30)
    return asyncio.run(git_run_async(args, cwd, timeout=30))


def _planted_worktree(tmp_path: Path, kind: str) -> tuple[Path, Path, Drivers]:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant(kind, repo, worktree, drivers, tmp_path)
    _leave_unsaved_work(worktree)
    return repo, worktree, drivers


@pytest.mark.parametrize("runner", ("sync", "async"))
@pytest.mark.parametrize(("args", "expected"), READING_CALLS, ids=lambda v: str(v))
@pytest.mark.parametrize("kind", PLANTS)
def test_work_tree_reading_git_never_runs_an_agent_driver(
    tmp_path: Path, kind: str, args: list[str], expected: str, runner: str,
) -> None:
    _repo, worktree, drivers = _planted_worktree(tmp_path, kind)

    result = _call(args, worktree, runner)

    assert not drivers.ran(), f"the agent's {kind} driver ran for git {args}"
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout, result.stdout


@pytest.mark.parametrize("runner", ("sync", "async"))
def test_a_sub_directory_of_the_worktree_is_covered(tmp_path: Path, runner: str) -> None:
    """A nested project runs git from ``<worktree>/<sub>``."""
    _repo, worktree, drivers = _planted_worktree(tmp_path, "repository-config")
    sub = worktree / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("x = 1\n")

    status = _call(["status", "--porcelain"], sub, runner)
    diff = _call(["diff", "--name-only", "HEAD"], sub, runner)

    assert not drivers.ran()
    assert " M README.md" in status.stdout and "?? pkg/" in status.stdout, status.stdout
    assert diff.stdout.split() == ["README.md"]


def test_a_symlink_into_the_worktree_is_covered(tmp_path: Path) -> None:
    _repo, worktree, drivers = _planted_worktree(tmp_path, "info-attributes")
    link = tmp_path / "link-to-worktree"
    link.symlink_to(worktree)

    result = git_run(["diff", "HEAD"], link, timeout=30)

    assert not drivers.ran()
    assert "+SEED" in result.stdout


# --- The callers that ran git there --------------------------------------------


@pytest.mark.parametrize("kind", PLANTS)
def test_the_orchestrator_callers_never_run_an_agent_driver(tmp_path: Path, kind: str) -> None:
    _repo, worktree, drivers = _planted_worktree(tmp_path, kind)
    wt = str(worktree)

    claimed = verify_files_changed(["README.md", "elsewhere.py"], wt)
    changed = _check_git_changes(wt)
    empty = asyncio.run(_git_diff_is_empty(wt))
    context = asyncio.run(_capture_git_diff_context(wt, 1))
    snapshot = asyncio.run(snapshot_reviewed_tree(wt))

    assert not drivers.ran(), f"the agent's {kind} driver ran in the orchestrator"
    assert claimed == ["README.md"]
    assert changed is True
    assert empty is False
    assert "+SEED" in context
    assert snapshot.clean is False


# --- Submodules and repositories inside the worktree ---------------------------


@pytest.mark.parametrize("runner", ("sync", "async"))
@pytest.mark.parametrize("args", (
    ["status", "--porcelain"],
    ["status", "--porcelain", "--ignore-submodules=none"],
    ["diff", "HEAD"],
    ["diff", "--stat", "--ignore-submodules=untracked"],
    ["diff-index", "HEAD"],
), ids=lambda v: " ".join(v))
@pytest.mark.parametrize("kind", NESTED)
def test_reading_git_never_starts_git_in_a_nested_repository(
    tmp_path: Path, kind: str, args: list[str], runner: str,
) -> None:
    _repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _leave_unsaved_work(worktree)
    _plant_nested_repository(worktree, drivers, kind)

    result = _call(args, worktree, runner)

    assert not drivers.ran(), "the nested repository's driver ran in the orchestrator"
    assert result.returncode == 0, result.stderr
    assert "README.md" in result.stdout


def test_a_staged_submodule_pointer_is_still_reported(tmp_path: Path) -> None:
    """``--ignore-submodules=dirty`` skips the submodule's work tree, not its
    pointer: a gitlink bump stays visible."""
    _repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _plant_nested_repository(worktree, drivers, "submodule-entry")

    result = git_run(["diff", "--name-only", "--cached"], worktree, timeout=30)

    assert not drivers.ran()
    assert "vendored" in result.stdout.split()


# --- Refusals: a path that is not a registered worktree ------------------------


def test_a_sub_directory_swapped_for_a_symlink_out_of_the_worktree_is_refused(
    tmp_path: Path,
) -> None:
    _repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    agent = _init_repo(tmp_path / "agent-repo")
    _define_filter(agent, drivers)
    (agent / ".gitattributes").write_text("* filter=probe\n")
    (agent / "README.md").write_text("SEED\n")
    (worktree / "pkg").symlink_to(agent)

    result = git_run(["status", "--porcelain"], worktree / "pkg", timeout=30)

    assert not drivers.ran()
    assert result.returncode == git_ops_mod._REFUSED_RETURNCODE
    assert "leaves the task worktree" in result.stderr


def test_an_unregistered_repository_under_forge_worktrees_is_refused(tmp_path: Path) -> None:
    repo, _worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    rogue = _init_repo(repo / ".forge-worktrees" / "rogue")
    _define_filter(rogue, drivers)
    (rogue / ".gitattributes").write_text("* filter=probe\n")
    (rogue / "README.md").write_text("SEED\n")

    sync = git_run(["diff", "HEAD"], rogue, timeout=30)
    run_async = asyncio.run(git_run_async(["diff", "HEAD"], rogue, timeout=30))

    assert not drivers.ran()
    for result in (sync, run_async):
        assert result.returncode == git_ops_mod._REFUSED_RETURNCODE
        assert "not a registered worktree" in result.stderr


def test_a_worktree_swapped_for_a_symlink_is_refused(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    _define_filter(repo, drivers)
    (repo / "README.md").write_text("SEED\n")
    (repo / ".gitattributes").write_text("* filter=probe\n")
    worktree.rename(tmp_path / "moved-away")
    worktree.symlink_to(repo)

    result = git_run(["status", "--porcelain"], worktree, timeout=30)

    assert not drivers.ran()
    assert result.returncode == git_ops_mod._REFUSED_RETURNCODE


# --- What the view keeps ---------------------------------------------------------


def _plain_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotepath=true", *args], cwd=cwd,
        capture_output=True, text=True, check=True,
    ).stdout


def test_answers_match_plain_git_on_an_unplanted_worktree(tmp_path: Path) -> None:
    """Modified, staged, deleted, untracked and excluded files, file-mode
    settings and the repository's ``info/exclude``: the same answers."""
    repo, worktree = _task_worktree(tmp_path)
    (worktree / "kept.py").write_text("a = 1\n")
    (worktree / "gone.py").write_text("b = 2\n")
    _git(worktree, "add", "kept.py", "gone.py")
    _git(worktree, "commit", "-q", "-m", "files")
    (worktree / "README.md").write_text("SEED\n")
    (worktree / "kept.py").write_text("a = 10\n")
    _git(worktree, "add", "kept.py")
    (worktree / "gone.py").unlink()
    (worktree / "new.py").write_text("c = 3\n")
    (worktree / "debug.log").write_text("noise\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("*.log\n")
    _git(repo, "config", "core.filemode", "false")
    (worktree / "README.md").chmod(0o755)

    for args in (
        ["status", "--porcelain"],
        ["diff", "HEAD"],
        ["diff", "--name-status", "--cached"],
        ["ls-files", "-m", "-o", "--exclude-standard"],
    ):
        assert git_run(args, worktree, timeout=30).stdout == _plain_git(worktree, *args), args
    assert "debug.log" not in git_run(["status", "--porcelain"], worktree).stdout


@pytest.fixture
def pinned_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway HOME whose global config the test writes and then pins;
    the pin is forgotten before and after."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    git_ops_mod.reset_global_git_config_pin()
    yield home
    git_ops_mod.reset_global_git_config_pin()


def test_a_driver_in_the_pinned_global_config_never_runs(
    tmp_path: Path, pinned_home: Path,
) -> None:
    """The pinned global config can hold drivers (an operator's LFS, or one
    an agent wrote to ``~/.gitconfig`` before the pin). Only the operator's
    identity and ``core.excludesFile`` reach the view; ``.gitattributes``
    cannot select a global driver."""
    _repo, worktree = _task_worktree(tmp_path)
    drivers = Drivers(tmp_path)
    ignore = tmp_path / "global-ignore"
    ignore.write_text("*.tmp\n")
    (pinned_home / ".gitconfig").write_text(
        f'[filter "probe"]\n\tclean = {drivers.filter}\n'
        f"[core]\n\texcludesFile = {ignore}\n"
    )
    git_ops_mod.pin_global_git_config()
    (worktree / ".gitattributes").write_text("* filter=probe\n")
    _leave_unsaved_work(worktree)
    (worktree / "scratch.tmp").write_text("ignored\n")

    status = git_run(["status", "--porcelain"], worktree)
    diff = git_run(["diff", "HEAD"], worktree)

    assert not drivers.ran(), "the global config's driver ran in the orchestrator"
    assert " M README.md" in status.stdout and "scratch.tmp" not in status.stdout
    assert "+SEED" in diff.stdout


def test_location_queries_still_name_the_real_repository(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)

    common = git_run(["rev-parse", "--path-format=absolute", "--git-common-dir"], worktree)

    assert common.stdout.strip() == str((repo / ".git").resolve())


def test_git_outside_a_task_worktree_runs_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    seen: list[dict[str, str]] = []
    real = git_ops_mod._run_with_env

    def recording(args_list, cwd, timeout, env=None, **kwargs):
        seen.append(dict(env or {}))
        return real(args_list, cwd, timeout, env, **kwargs)

    monkeypatch.setattr(git_ops_mod, "_run_with_env", recording)
    git_run(["status", "--porcelain"], repo)

    assert len(seen) == 1 and "GIT_COMMON_DIR" not in seen[0]


def test_the_private_common_dir_and_descriptor_are_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, worktree, _drivers = _planted_worktree(tmp_path, "repository-config")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    before = set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else set()

    git_run(["status", "--porcelain"], worktree)
    asyncio.run(git_run_async(["diff", "HEAD"], worktree))

    assert list(scratch.iterdir()) == []
    if before:
        assert set(os.listdir("/proc/self/fd")) <= before


@pytest.mark.parametrize(("args", "expected"), (
    (["status", "--porcelain"], ["status", "--porcelain", "--ignore-submodules=dirty"]),
    (["status", "--ignore-submodules=all"], ["status", "--ignore-submodules=all"]),
    (["status", "--ignore-submodules"], ["status", "--ignore-submodules"]),
    (["status", "--ignore-submodules=all", "--ignore-submodules=none"],
     ["status", "--ignore-submodules=all", "--ignore-submodules=none",
      "--ignore-submodules=dirty"]),
    (["diff", "HEAD", "--", "a.py"], ["diff", "HEAD", "--ignore-submodules=dirty", "--", "a.py"]),
    (["ls-files", "-m"], ["ls-files", "-m"]),
    (["grep", "x"], ["grep", "x"]),
))
def test_submodule_work_trees_are_never_examined(args: list[str], expected: list[str]) -> None:
    assert git_ops_mod._submodule_work_trees_unexamined(args) == expected
