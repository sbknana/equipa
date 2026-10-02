"""Task #3158 (FF-3155) — the branch check around each agent run reads the
worktree's HEAD without running git through the worktree, and hardened git
never lazy-fetches from a promisor remote an agent configured.

``_require_task_branch`` runs before and after every agent attempt and after
the whole run. It ran ``symbolic-ref HEAD`` and ``rev-parse --verify
HEAD^{commit}`` in the agent's directory by discovery. An agent that left a
partial-clone repository there (at the worktree's ``.git``, behind a
redirected ``gitdir:`` file, or in the sub-directory a nested project runs
in), whose HEAD names a missing commit, had git lazy-fetch that commit: git
started the repository's ``remote.<name>.uploadpack`` program, the agent's,
inside the orchestrator. The same lazy fetch is reachable from the main
repository, because an agent's plain ``git config`` in its worktree writes
the shared config.

Every check plants such an upload-pack program and asserts it never ran,
and that the branch check reports the worktree's real branch tip.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops
from equipa.merge_integrity import resolve_commit

from test_dispatch_modes_gated_3112 import (
    _git,
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)
from test_repository_identity_3146 import TASK_BRANCH, TASK_ID

# A commit no repository here holds: looking it up makes git lazy-fetch it.
MISSING_COMMIT = "1" * 40

PLANTS = ("planted-repository", "redirected-gitfile", "nested-repository")


def _run(coro):
    return asyncio.run(coro)


class UploadPack:
    """The program an agent names as its promisor remote's upload-pack;
    it records that it ran."""

    def __init__(self, tmp_path: Path) -> None:
        self.marker = tmp_path / "agent-upload-pack-ran"
        self.program = tmp_path / "agent-upload-pack"
        self.program.write_text(f"#!/bin/sh\ntouch '{self.marker}'\nexit 1\n")
        self.program.chmod(0o755)
        self.server = tmp_path / "promisor-server"
        _git(tmp_path, "init", "-q", "--bare", str(self.server))

    def configure(self, directory: Path, *scope: str) -> None:
        """Make ``directory``'s repository a partial clone of a remote
        whose upload-pack is this program (``git config`` run there)."""
        for key, value in (
            ("core.repositoryformatversion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.url", str(self.server)),
            ("remote.origin.promisor", "true"),
            ("remote.origin.uploadpack", str(self.program)),
        ):
            _git(directory, "config", *scope, key, value)

    def ran(self) -> bool:
        return self.marker.exists()


def _task_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path / "repo")
    worktree = repo / ".forge-worktrees" / f"task-{TASK_ID}"
    _git(repo, "worktree", "add", "-q", "-b", TASK_BRANCH, str(worktree), "master")
    return repo, worktree


def _planted_repository(path: Path, head: str | None) -> Path:
    """A repository at ``path`` on ``TASK_BRANCH``: its tip is ``head``, or
    a commit of its own when ``head`` is None."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", TASK_BRANCH)
    _git(path, "config", "user.email", "agent@forgeborn.dev")
    _git(path, "config", "user.name", "Agent")
    _git(path, "config", "commit.gpgsign", "false")
    if head is None:
        (path / "AGENT.md").write_text("planted\n")
        _git(path, "add", "AGENT.md")
        _git(path, "commit", "-q", "-m", "planted")
    else:
        (path / ".git" / "refs" / "heads" / TASK_BRANCH).write_text(f"{head}\n")
    return path


def _plant(
    kind: str, worktree: Path, tmp_path: Path, upload_pack: UploadPack | None,
    *, head: str | None,
) -> Path:
    """The agent's plant; returns the directory the agent ran in."""
    if kind == "planted-repository":
        (worktree / ".git").unlink()
        planted, agent_dir = _planted_repository(worktree, head), worktree
    elif kind == "redirected-gitfile":
        planted = _planted_repository(tmp_path / "agent-repo", head)
        (worktree / ".git").write_text(f"gitdir: {planted / '.git'}\n")
        agent_dir = worktree
    elif kind == "nested-repository":
        planted = _planted_repository(worktree / "pkg", head)
        agent_dir = planted
    else:  # pragma: no cover - a typo in PLANTS
        raise AssertionError(kind)
    if upload_pack is not None:
        upload_pack.configure(planted)
    return agent_dir


def _branch_check(agent_dir: Path) -> str | Exception:
    """``_require_task_branch``'s SHA, or the error it raised."""
    try:
        return _run(dispatch_mod._require_task_branch(str(agent_dir), TASK_BRANCH))
    except dispatch_mod.AttemptCleanupError as exc:
        return exc


# --- the branch check never runs git through the worktree -------------------


@pytest.mark.parametrize("kind", PLANTS)
def test_branch_check_never_runs_a_planted_promisor_upload_pack(
    tmp_path: Path, kind: str,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    upload_pack = UploadPack(tmp_path)
    agent_dir = _plant(kind, worktree, tmp_path, upload_pack, head=MISSING_COMMIT)

    head = _branch_check(agent_dir)

    assert not upload_pack.ran(), (
        f"the agent's upload-pack ran in the orchestrator ({kind})"
    )
    assert head == _git(repo, "rev-parse", f"refs/heads/{TASK_BRANCH}")


@pytest.mark.parametrize("kind", PLANTS)
def test_branch_check_reads_the_real_branch_not_a_planted_repository(
    tmp_path: Path, kind: str,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    agent_dir = _plant(kind, worktree, tmp_path, None, head=None)
    planted_tip = _git(agent_dir, "rev-parse", "HEAD")

    head = _branch_check(agent_dir)

    real_tip = _git(repo, "rev-parse", f"refs/heads/{TASK_BRANCH}")
    assert planted_tip != real_tip
    assert head == real_tip


def test_branch_check_follows_a_commit_on_the_task_branch(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)
    (worktree / "NOTES.md").write_text("agent notes\n")
    _git(worktree, "add", "NOTES.md")
    _git(worktree, "commit", "-q", "-m", "agent notes")

    assert _branch_check(worktree) == _git(repo, "rev-parse", TASK_BRANCH)
    assert _branch_check(worktree) != _master(repo)


@pytest.mark.parametrize(
    "move", ["detached", "other-branch"],
)
def test_branch_check_refuses_a_worktree_off_its_branch(tmp_path: Path, move: str) -> None:
    _repo, worktree = _task_worktree(tmp_path)
    if move == "detached":
        _git(worktree, "checkout", "-q", "--detach")
    else:
        _git(worktree, "checkout", "-q", "-b", "elsewhere")

    error = _branch_check(worktree)

    assert isinstance(error, dispatch_mod.AttemptCleanupError)
    expected = "a detached HEAD" if move == "detached" else "'elsewhere'"
    assert expected in str(error)


def test_branch_check_refuses_a_directory_no_repository_registers(tmp_path: Path) -> None:
    _repo, worktree = _task_worktree(tmp_path)
    stray = tmp_path / "stray"
    shutil.copytree(worktree, stray)

    error = _branch_check(stray)

    assert isinstance(error, dispatch_mod.AttemptCleanupError)
    assert "registers" in str(error)


# --- hardened git never lazy-fetches -----------------------------------------


def _agent_configures_a_promisor_remote(
    repo: Path, worktree: Path, upload_pack: UploadPack,
) -> None:
    """What an agent can do from its worktree: ``git config`` there writes
    the shared repository config, and the task branch's ref file is in the
    shared git dir."""
    upload_pack.configure(worktree)
    common_dir = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert common_dir == repo / ".git"
    (common_dir / "refs" / "heads" / TASK_BRANCH).write_text(f"{MISSING_COMMIT}\n")


def test_hardened_git_never_lazy_fetches_from_a_promisor_remote(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)
    upload_pack = UploadPack(tmp_path)
    _agent_configures_a_promisor_remote(repo, worktree, upload_pack)

    resolved = _run(resolve_commit(str(repo), f"refs/heads/{TASK_BRANCH}"))
    assert not upload_pack.ran(), "resolve_commit ran the agent's upload-pack"
    assert resolved is None

    synchronous = git_ops.git_run(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{TASK_BRANCH}^{{commit}}"],
        str(repo),
    )
    assert not upload_pack.ran(), "git_run ran the agent's upload-pack"
    assert synchronous.returncode != 0


@pytest.fixture
def git_without_lazy_fetch_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hardening as a git that ignores GIT_NO_LAZY_FETCH sees it."""
    monkeypatch.setattr(git_ops, "GIT_HARDENING_ENV", MappingProxyType({
        key: value for key, value in git_ops.GIT_HARDENING_ENV.items()
        if key != "GIT_NO_LAZY_FETCH"
    }))


@pytest.mark.usefixtures("git_without_lazy_fetch_switch")
def test_branch_check_allows_no_transport_whatever_the_git_version(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)
    upload_pack = UploadPack(tmp_path)
    _agent_configures_a_promisor_remote(repo, worktree, upload_pack)

    error = _branch_check(worktree)

    assert not upload_pack.ran(), "the branch check ran the agent's upload-pack"
    assert isinstance(error, dispatch_mod.AttemptCleanupError)


@pytest.mark.usefixtures("git_without_lazy_fetch_switch")
def test_worktree_git_allows_no_transport_whatever_the_git_version(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)
    upload_pack = UploadPack(tmp_path)
    _agent_configures_a_promisor_remote(repo, worktree, upload_pack)

    async def enter() -> None:
        async with git_ops.agent_worktree_git(repo / ".git", worktree):
            pass  # pragma: no cover - the HEAD cannot resolve

    with pytest.raises(git_ops.AgentWorktreeGitError):
        _run(enter())
    assert not upload_pack.ran(), "agent_worktree_git ran the agent's upload-pack"


def test_worktree_git_runs_with_no_transport_allowed(tmp_path: Path) -> None:
    repo, worktree = _task_worktree(tmp_path)

    async def environments() -> tuple[str | None, str | None]:
        async with git_ops.agent_worktree_git(repo / ".git", worktree) as worktree_git:
            return (
                worktree_git._env().get("GIT_ALLOW_PROTOCOL"),
                worktree_git._env().get("GIT_NO_LAZY_FETCH"),
            )

    assert _run(environments()) == ("", "1")


# --- whole isolated runs ------------------------------------------------------


@pytest.mark.parametrize("kind", PLANTS)
@pytest.mark.parametrize(
    ("outcome", "commit"),
    [
        ("tests_passed", True),
        ("tests_failed", True),
        ("early_terminated", False),
    ],
    ids=["success", "failure", "early-termination"],
)
def test_isolated_run_never_runs_a_planted_upload_pack(
    tmp_path: Path, kind: str, outcome: str, commit: bool,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    master_before = _master(repo)
    upload_pack = UploadPack(tmp_path)

    async def agent(agent_dir: str, task_branch: str):
        worktree = Path(agent_dir)
        if commit:
            (worktree / "NOTES.md").write_text("agent notes\n")
            _git(worktree, "add", "NOTES.md")
            _git(worktree, "commit", "-q", "-m", "agent notes")
        _plant(kind, worktree, tmp_path, upload_pack, head=MISSING_COMMIT)
        return {"cost": 0.0, "duration": 0.0}, 1, outcome

    args = SimpleNamespace(security_review=False, dispatch_config={})
    run = _run(dispatch_mod.run_task_in_isolation(
        _task(TASK_ID), str(repo), {}, args, execute=agent,
    ))

    assert not upload_pack.ran(), (
        f"the agent's upload-pack ran in the orchestrator ({kind}, {run.outcome})"
    )
    # A plant at the worktree's .git leaves the work tree unreadable, so
    # nothing merges. A nested one is an untracked directory beside a clean
    # commit, which merges: either way only the agent's real commit lands.
    if run.merged_sha is None:
        assert _master(repo) == master_before
    else:
        assert outcome == "tests_passed" and kind == "nested-repository"
        assert _git(repo, "rev-parse", "master^") == master_before
        assert _git(repo, "ls-tree", "-r", "--name-only", "master").split() == [
            "NOTES.md", "README.md",
        ]
