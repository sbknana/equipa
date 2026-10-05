"""Task #3162 (FF-3158) — the worktree poison-vector matrix.

The independent review of task #3158 approved it on EQUIPA's own marker
tests and asked for a wider probe before deploy: plant every way an agent
can make git run a program of its choosing from its own task worktree, run
the orchestrator's real cleanup on that worktree, and check that no planted
program ran while the cleanup still did its job.

* VECTORS plant programs (``touch`` scripts in a temp dir, each creating its
  own marker) where an agent can write them: the shared repository config
  (a plain ``git config`` in the worktree writes it), ``config.worktree``,
  ``.git/hooks``, ``info/attributes``, the work tree's files, the worktree's
  ``.git`` file and an untracked repository nested in the worktree.
* OPERATIONS are the orchestrator's cleanup calls: the stash of an unmerged
  task, the dirty check, the failed-attempt reset (with and without a
  recorded base), the branch check, the read-only diff/status view, the
  worktree removal (merged and unmerged) and the retirement of a leftover
  worktree. Each one also has to do its job (the work is stashed, the reset
  lands on the base, the worktree is gone).
* Whole isolated runs (success, failure, early termination) plant the
  vector from inside the agent and run the real post-agent path.
* R3162-01 (task #3165): two vectors replace the worktree root with a
  symlink, to the agent's own repository or to a worktree that repository
  registers. Every operation must then refuse (``Operation.refused``) and
  leave both repositories as they were; the single-task CLI loop must stop
  at its post-attempt branch check, before any cleanup.
* R3165-03 (task #3166): two more vectors replace ``.forge-worktrees``
  itself, leaving the worktree's own path alone, so ``task-<id>`` is the
  agent's own repository or a worktree that repository registers. They are
  refused like the root vectors. A ``_canonical_task_worktree_root`` that
  follows the symlinked base fails 10 of their cases, branch check
  included.
* R3165-01 (task #3166): a project that was not git at dispatch, where
  the agent runs ``git init`` during a failed attempt. Both retry loops are
  driven as production calls them (no task branch). The cleanup must start
  no git process at all and must report the repository instead.

Every check snapshots the markers right after the orchestrator's call; a
failure reads ``EXECUTED <vector>/<operation>: [<markers>]``.
``scripts/poison_matrix_summary.py`` tabulates a JUnit report of this module
by vector and operation. On d5dcc05 (before task #3158) the matrix shows the
filter, include, promisor and redirected-``.git`` vectors executing; on this
tree none does.

The review also listed three calls that still found the repository by
discovery: ``_is_git_repo(project_dir)`` in ``cleanup_failed_attempt``,
``get_trusted_default_branch(worktree_dir)`` when no base is recorded, and
``git worktree remove --force``. Each cleanup operation is therefore checked
structurally too: every git process it starts either names its repository
(``--git-dir``) or starts outside the worktree, and ``worktree remove``
names it. A second dimension reruns the transport vectors as on a git that
ignores ``GIT_NO_LAZY_FETCH`` (older than 2.44 without the May 2024
backports): the cleanup must start no transport there either.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

import equipa.dispatch as dispatch_mod
import equipa.git_ops as git_ops_mod
from equipa.git_ops import git_run, git_run_async

from test_dispatch_modes_gated_3112 import (
    _git,
    _init_repo,
    _master,
    _reset_shutdown_flag,  # noqa: F401  (autouse fixture)
    _task,
)
from test_repository_identity_3146 import TASK_BRANCH, TASK_ID

STASH_TAG = f"equipa-early-term task-{TASK_ID}"
UNSAVED = "print('unsaved agent work')"
# A commit no repository here holds: what a promisor remote is asked for.
MISSING_COMMIT = "0123456789abcdef0123456789abcdef01234567"
HOOKS = ("post-checkout", "post-merge", "reference-transaction", "pre-auto-gc")
PAGED_COMMANDS = ("status", "diff", "log", "stash", "branch")


def _run(coroutine):
    return asyncio.run(coroutine)


def _with_project_dir(func: Callable[..., Any], *args: Any, project_dir: Path) -> Any:
    """Call ``func`` the way the code under test accepts: task #3158 added
    ``project_dir``; without it the call is the pre-3158 one, so the matrix
    runs on d5dcc05 and fails there for what the code does, not a TypeError."""
    if "project_dir" in inspect.signature(func).parameters:
        return func(*args, project_dir=str(project_dir))
    return func(*args)


# --- The agent and its programs ----------------------------------------------


@dataclass
class Agent:
    """Where the agent writes, and the programs it names. Each program
    creates its marker when it runs; nothing else does."""

    repo: Path
    worktree: Path | None
    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    # git itself refuses `worktree remove` once the worktree's .git is rewritten.
    git_refuses_removal: bool = False
    ran_while_planting: list[str] = field(default_factory=list)
    # R3162-01: the repository a symlink at the worktree's path now names,
    # and the commit it had checked out when the symlink was planted.
    replaced_root: Path | None = None
    replaced_root_head: str = ""
    # The symlink planted: the worktree's path, or (R3165-03) its base.
    replaced_link: Path | None = None

    @property
    def root_replaced(self) -> bool:
        return self.replaced_root is not None

    def __post_init__(self) -> None:
        self.bin = self.tmp / "agent-bin"
        self.markers = self.tmp / "markers"
        self.bin.mkdir(exist_ok=True)
        self.markers.mkdir(exist_ok=True)
        # Any ssh git starts is this one: a transport started at all shows
        # up as a marker, and no test ever reaches the network.
        path_bin = self.tmp / "path-bin"
        self.program("transport-ssh", "exit 1", path=path_bin / "ssh")
        monkeypatch = self.monkeypatch
        monkeypatch.setenv("PATH", f"{path_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        for variable in ("GIT_SSH", "GIT_SSH_COMMAND"):
            monkeypatch.delenv(variable, raising=False)

    @property
    def tree(self) -> Path:
        if self.worktree is None:  # pragma: no cover - set before any plant
            raise AssertionError("the agent has no worktree yet")
        return self.worktree

    def program(self, marker: str, then: str = "exit 0", *, path: Path | None = None) -> str:
        """A shell script that creates ``markers/<marker>``, then runs ``then``."""
        script = path or self.bin / marker
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!/bin/sh\ntouch '{self.markers / marker}'\n{then}\n")
        script.chmod(0o755)
        return str(script)

    def config(self, *args: str, where: Path | None = None) -> None:
        """``git config`` as the agent runs it, in its worktree by default."""
        _git(where or self.tree, "config", *args)

    def select(self, attribute: str) -> None:
        """Select ``attribute`` for every path through the work tree's own
        ``.gitattributes``."""
        (self.tree / ".gitattributes").write_text(f"* {attribute}\n")

    def ran(self) -> list[str]:
        return sorted(marker.name for marker in self.markers.iterdir())


# --- Vectors -----------------------------------------------------------------


def _define_filter(agent: Agent, marker: str, *scope: str, where: Path | None = None) -> None:
    program = agent.program(marker, "exec cat")
    agent.config(*scope, "filter.probe.clean", program, where=where)
    agent.config(*scope, "filter.probe.smudge", program, where=where)


def _agent_config_file(agent: Agent, marker: str) -> Path:
    """A config file of the agent's defining a filter and an external diff;
    the work tree's ``.gitattributes`` selects the filter for every path."""
    program = agent.program(marker, "exec cat")
    external = agent.program(f"{marker}-diff-external")
    config_file = agent.tmp / f"{marker}.cfg"
    config_file.write_text(
        f'[filter "probe"]\n\tclean = {program}\n\tsmudge = {program}\n'
        f"[diff]\n\texternal = {external}\n"
    )
    agent.select("filter=probe")
    return config_file


def _promisor(agent: Agent, url: str, repo: Path | None = None) -> None:
    """``repo`` (the shared repository by default) made a partial clone whose
    promisor remote is ``url``, with a branch naming a commit only that
    remote could supply: looking the branch up lazy-fetches it."""
    git_dir = (repo or agent.repo) / ".git"
    where = repo or agent.tree
    agent.config("extensions.partialClone", "probe", where=where)
    agent.config("remote.probe.url", url, where=where)
    agent.config("remote.probe.promisor", "true", where=where)
    (git_dir / "refs" / "heads" / "main").write_text(f"{MISSING_COMMIT}\n")


def _hooks(agent: Agent, directory: Path, prefix: str) -> None:
    for hook in HOOKS:
        agent.program(f"{prefix}-{hook}", path=directory / hook)


def plant_fsmonitor(agent: Agent) -> None:
    agent.config("core.fsmonitor", agent.program("fsmonitor", "exit 1"))


def plant_fsmonitor_config_worktree(agent: Agent) -> None:
    agent.config("extensions.worktreeConfig", "true")
    agent.config("--worktree", "core.fsmonitor", agent.program("fsmonitor-worktree", "exit 1"))


def plant_hooks_path(agent: Agent) -> None:
    hooks = agent.tmp / "agent-hooks"
    _hooks(agent, hooks, "hooksPath")
    agent.config("core.hooksPath", str(hooks))


def plant_git_hooks_dir(agent: Agent) -> None:
    _hooks(agent, agent.repo / ".git" / "hooks", "git-hooks")


def plant_filter_gitattributes(agent: Agent) -> None:
    _define_filter(agent, "filter-gitattributes")
    agent.select("filter=probe")


def plant_filter_info_attributes(agent: Agent) -> None:
    _define_filter(agent, "filter-info-attributes")
    info = agent.repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=probe\n")


def plant_filter_attributes_file(agent: Agent) -> None:
    _define_filter(agent, "filter-attributesFile")
    attributes = agent.tmp / "agent-attributes"
    attributes.write_text("* filter=probe\n")
    agent.config("core.attributesFile", str(attributes))


def plant_filter_process(agent: Agent) -> None:
    agent.config("filter.probe.process", agent.program("filter-process", "exit 1"))
    agent.select("filter=probe")


def plant_diff_textconv(agent: Agent) -> None:
    agent.config("diff.probe.textconv", agent.program("diff-textconv", 'cat "$1"'))
    agent.select("diff=probe")


def plant_diff_external(agent: Agent) -> None:
    agent.config("diff.external", agent.program("diff-external"))


def plant_merge_driver(agent: Agent) -> None:
    agent.config("merge.probe.driver", agent.program("merge-driver") + " %O %A %B")
    agent.select("merge=probe")


def plant_include_path(agent: Agent) -> None:
    agent.config("include.path", str(_agent_config_file(agent, "include-path")))


def plant_include_if(agent: Agent) -> None:
    config_file = str(_agent_config_file(agent, "includeIf"))
    git_dir = os.path.realpath(agent.repo / ".git")
    agent.config(f"includeIf.gitdir:{git_dir}/.path", config_file)
    agent.config(f"includeIf.onbranch:{TASK_BRANCH}.path", config_file)


def plant_config_worktree_include(agent: Agent) -> None:
    config_file = str(_agent_config_file(agent, "config-worktree-include"))
    agent.config("extensions.worktreeConfig", "true")
    agent.config("--worktree", "include.path", config_file)
    agent.config("--worktree", f"includeIf.onbranch:{TASK_BRANCH}.path", config_file)


def plant_promisor_uploadpack(agent: Agent) -> None:
    remote = agent.tmp / "agent-remote"
    remote.mkdir()
    agent.config("remote.probe.uploadpack", agent.program("promisor-uploadpack", "exit 1"))
    _promisor(agent, str(remote))


def plant_promisor_ssh_command(agent: Agent) -> None:
    agent.config("core.sshCommand", agent.program("promisor-sshCommand", "exit 1"))
    _promisor(agent, "ssh://agent.invalid/remote.git")


def plant_promisor_protocol_ext(agent: Agent) -> None:
    agent.config("protocol.ext.allow", "always")
    _promisor(agent, "ext::" + agent.program("promisor-ext", "exit 1"))


def plant_pager(agent: Agent) -> None:
    pager = agent.program("pager", "exec cat")
    agent.config("core.pager", pager)
    for command in PAGED_COMMANDS:
        agent.config(f"pager.{command}", pager)


def _poisoned_repository(agent: Agent, repo: Path, prefix: str) -> None:
    """Every in-repository vector at once in ``repo``, the agent's own."""
    _define_filter(agent, f"{prefix}-filter", where=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=probe\n")
    agent.config("core.fsmonitor", agent.program(f"{prefix}-fsmonitor", "exit 1"), where=repo)
    hooks = agent.tmp / f"{prefix}-hooks"
    _hooks(agent, hooks, prefix)
    agent.config("core.hooksPath", str(hooks), where=repo)
    agent.config("diff.external", agent.program(f"{prefix}-diff-external"), where=repo)
    remote = agent.tmp / f"{prefix}-remote"
    remote.mkdir()
    agent.config(
        "remote.probe.uploadpack", agent.program(f"{prefix}-uploadpack", "exit 1"), where=repo,
    )
    _promisor(agent, str(remote), repo)


def plant_redirected_gitfile(agent: Agent) -> None:
    """The worktree's ``.git`` rewritten to name the agent's own repository:
    a partial clone whose task branch only its promisor holds, which has a
    ``main`` but no ``master``, and every in-repository vector."""
    other = _init_repo(agent.tmp / "agent-repo")
    _git(other, "branch", "-m", "master", "agent-main")
    _poisoned_repository(agent, other, "gitfile")
    git_dir = other / ".git"
    (git_dir / "refs" / "heads" / "main").write_text(f"{_git(other, 'rev-parse', 'agent-main')}\n")
    (git_dir / "refs" / "heads" / TASK_BRANCH).write_text(f"{MISSING_COMMIT}\n")
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{TASK_BRANCH}\n")
    (agent.tree / ".git").write_text(f"gitdir: {git_dir}\n")
    agent.git_refuses_removal = True


def plant_nested_repository(agent: Agent) -> None:
    """An untracked repository inside the worktree, with its own poisoned
    config and a same-size edit git would have to hash."""
    nested = _init_repo(agent.tree / "vendored")
    _poisoned_repository(agent, nested, "nested")
    (nested / "README.md").write_text("SEED\n")


def _replace_root(agent: Agent, target: Path) -> None:
    """Move the worktree aside (staying inside ``.forge-worktrees``, where
    the agent may write) and put a symlink to ``target`` at its path."""
    agent.replaced_root_head = _git(target, "rev-parse", "HEAD")
    agent.tree.rename(agent.tree.with_name(f"{agent.tree.name}-moved"))
    agent.tree.symlink_to(target, target_is_directory=True)
    agent.replaced_root = target
    agent.replaced_link = agent.tree
    agent.git_refuses_removal = True


def plant_symlinked_root(agent: Agent) -> None:
    """R3162-01: the worktree root replaced with a symlink to the agent's
    own repository outside ``.forge-worktrees``. That repository is on the
    task branch and has a ``main`` adding a file its ``.gitattributes``
    sends through the agent's filter, so checking ``main`` out there runs
    the filter; it also carries every in-repository vector."""
    other = _init_repo(agent.tmp / "agent-repo")
    _git(other, "branch", "-m", "master", "main")
    (other / ".gitattributes").write_text("* filter=probe\n")
    (other / "a.txt").write_text("agent main\n")
    _git(other, "add", ".gitattributes", "a.txt")
    _git(other, "commit", "-q", "-m", "agent main")
    agent_main = _git(other, "rev-parse", "main")
    _git(other, "checkout", "-q", "-b", TASK_BRANCH, "main~1")
    _replace_root(agent, other)
    _poisoned_repository(agent, other, "symlinked-root")
    # _poisoned_repository points main at a commit only the promisor has.
    (other / ".git" / "refs" / "heads" / "main").write_text(f"{agent_main}\n")


def plant_symlinked_root_to_own_worktree(agent: Agent) -> None:
    """The worktree root replaced with a symlink to a worktree the agent's
    own repository registers, on a branch named like the task's: read
    through the symlink, it looks like a registered task worktree."""
    other = _init_repo(agent.tmp / "agent-repo")
    own_worktree = agent.tmp / "agent-worktree"
    _git(other, "worktree", "add", "-q", "-b", TASK_BRANCH, str(own_worktree), "master")
    _replace_root(agent, own_worktree)
    _poisoned_repository(agent, other, "own-worktree")


def _agent_repository(agent: Agent, path: Path, prefix: str) -> Path:
    """A repository of the agent's at ``path``, on the task branch, whose
    ``main`` adds a file its ``.gitattributes`` sends through the agent's
    filter (checking ``main`` out runs the filter), with every
    in-repository vector besides."""
    _init_repo(path)
    _git(path, "branch", "-m", "master", "main")
    (path / ".gitattributes").write_text("* filter=probe\n")
    (path / "a.txt").write_text("agent main\n")
    _git(path, "add", ".gitattributes", "a.txt")
    _git(path, "commit", "-q", "-m", "agent main")
    agent_main = _git(path, "rev-parse", "main")
    _git(path, "checkout", "-q", "-b", TASK_BRANCH, "main~1")
    _poisoned_repository(agent, path, prefix)
    # _poisoned_repository points main at a commit only the promisor has.
    (path / ".git" / "refs" / "heads" / "main").write_text(f"{agent_main}\n")
    return path


def _replace_base(agent: Agent, target: Path) -> None:
    """R3165-03: move ``.forge-worktrees`` itself aside and put a symlink at
    its path to the directory holding ``target``, the agent's directory
    named like the task worktree. The worktree's own path is untouched."""
    base = agent.tree.parent
    agent.replaced_root_head = _git(target, "rev-parse", "HEAD")
    base.rename(base.with_name(f"{base.name}-moved"))
    base.symlink_to(target.parent, target_is_directory=True)
    agent.replaced_root = target
    agent.replaced_link = base
    agent.git_refuses_removal = True


def plant_symlinked_worktree_base(agent: Agent) -> None:
    """``.forge-worktrees`` replaced with a symlink to a directory whose
    ``task-<id>`` is the agent's own repository (see
    :func:`_agent_repository`)."""
    target = agent.tmp / "agent-base" / agent.tree.name
    _replace_base(agent, _agent_repository(agent, target, "symlinked-base"))


def plant_symlinked_worktree_base_to_own_worktree(agent: Agent) -> None:
    """``.forge-worktrees`` replaced with a symlink to a directory whose
    ``task-<id>`` is a worktree the agent's own repository registers, on a
    branch named like the task's. A check that follows the symlinked base
    (and compares the result with itself) reads it as a registered task
    worktree and returns the agent's HEAD."""
    other = _init_repo(agent.tmp / "agent-repo")
    own_worktree = agent.tmp / "agent-base" / agent.tree.name
    _git(other, "worktree", "add", "-q", "-b", TASK_BRANCH, str(own_worktree), "master")
    _replace_base(agent, own_worktree)
    _poisoned_repository(agent, other, "own-base")


VECTORS: dict[str, Callable[[Agent], None]] = {
    "fsmonitor": plant_fsmonitor,
    "fsmonitor-config-worktree": plant_fsmonitor_config_worktree,
    "hooks-path": plant_hooks_path,
    "git-hooks-dir": plant_git_hooks_dir,
    "filter-gitattributes": plant_filter_gitattributes,
    "filter-info-attributes": plant_filter_info_attributes,
    "filter-attributes-file": plant_filter_attributes_file,
    "filter-process": plant_filter_process,
    "diff-textconv": plant_diff_textconv,
    "diff-external": plant_diff_external,
    "merge-driver": plant_merge_driver,
    "include-path": plant_include_path,
    "include-if": plant_include_if,
    "config-worktree-include": plant_config_worktree_include,
    "promisor-uploadpack": plant_promisor_uploadpack,
    "promisor-ssh-command": plant_promisor_ssh_command,
    "promisor-protocol-ext": plant_promisor_protocol_ext,
    "pager": plant_pager,
    "redirected-gitfile": plant_redirected_gitfile,
    "nested-repository": plant_nested_repository,
    "symlinked-root": plant_symlinked_root,
    "symlinked-root-to-own-worktree": plant_symlinked_root_to_own_worktree,
    "symlinked-worktree-base": plant_symlinked_worktree_base,
    "symlinked-worktree-base-to-own-worktree": plant_symlinked_worktree_base_to_own_worktree,
}
# The vectors whose program is started through a transport (a lazy fetch).
TRANSPORT_VECTORS = (
    "promisor-uploadpack", "promisor-ssh-command", "promisor-protocol-ext",
    "redirected-gitfile", "nested-repository", "symlinked-root",
    "symlinked-root-to-own-worktree", "symlinked-worktree-base",
    "symlinked-worktree-base-to-own-worktree",
)
# The vectors that put a symlink at the worktree's path or at its base.
SYMLINKED_VECTORS = (
    "symlinked-root", "symlinked-root-to-own-worktree",
    "symlinked-worktree-base", "symlinked-worktree-base-to-own-worktree",
)


def _plant(agent: Agent, vector: str) -> None:
    VECTORS[vector](agent)
    agent.ran_while_planting = agent.ran()


def _leave_unsaved_work(worktree: Path) -> None:
    # Same size as the committed "seed\n": git has to hash it to see the
    # change, which is when a clean filter runs.
    (worktree / "README.md").write_text("SEED\n")
    (worktree / "work.py").write_text(f"{UNSAVED}\n")


def _assert_nothing_ran(agent: Agent, vector: str, operation: str, fired: list[str]) -> None:
    assert not agent.ran_while_planting, f"the plant itself ran {agent.ran_while_planting}"
    assert not fired, f"EXECUTED {vector}/{operation}: {fired}"


# --- Recording the git processes the orchestrator starts ----------------------


@dataclass(frozen=True)
class GitCall:
    argv: tuple[str, ...]
    # Where the process starts, resolved when it was started.
    where: str
    explicit_repository: bool


class GitRecorder:
    """Every git process started while :meth:`recording` is active."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[GitCall] = []
        self._active = False
        real_run = subprocess.run
        real_exec = asyncio.create_subprocess_exec

        def recording_run(argv, *args, **kwargs):
            self._record(argv, kwargs)
            return real_run(argv, *args, **kwargs)

        async def recording_exec(*argv, **kwargs):
            self._record(argv, kwargs)
            return await real_exec(*argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", recording_run)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)

    def _record(self, argv: Any, kwargs: dict[str, Any]) -> None:
        if not self._active or isinstance(argv, (str, bytes)):
            return
        command = tuple(os.fspath(token) for token in argv)
        if not command or os.path.basename(command[0]) != "git":
            return
        cwd = kwargs.get("cwd")
        env = kwargs.get("env") or {}
        explicit = "GIT_DIR" in env or any(
            token == "--git-dir" or token.startswith("--git-dir=") for token in command
        )
        self.calls.append(GitCall(
            command, os.path.realpath(os.fspath(cwd) if cwd else os.getcwd()), explicit,
        ))

    @contextmanager
    def recording(self) -> Iterator[None]:
        self._active = True
        try:
            yield
        finally:
            self._active = False

    @contextmanager
    def paused(self) -> Iterator[None]:
        """The agent's own git calls, made by the test, are not recorded."""
        was_active, self._active = self._active, False
        try:
            yield
        finally:
            self._active = was_active

    def discovery_in(self, directory: Path) -> list[tuple[str, ...]]:
        """git calls that found their repository from inside ``directory``."""
        root = os.path.realpath(directory)
        return [
            call.argv for call in self.calls
            if not call.explicit_repository
            and (call.where == root or call.where.startswith(root + os.sep))
        ]

    def worktree_removals(self) -> list[GitCall]:
        return [
            call for call in self.calls
            if "worktree" in call.argv and "remove" in call.argv
        ]


# --- Operations ----------------------------------------------------------------


class _NoDb:
    """The task-status write of ``cleanup_failed_attempt``, discarded."""

    def __init__(self, write: bool = False) -> None:
        pass

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self) -> None:
        # No task row: the reflection injection then writes nothing.
        return None

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _assert_work_stashed(repo: Path) -> None:
    """The stash is in the repository, on the task branch, with the work."""
    stashes = _git(repo, "stash", "list")
    assert STASH_TAG in stashes and f"On {TASK_BRANCH}:" in stashes, stashes
    assert _git(repo, "cat-file", "-p", "stash@{0}:README.md") == "SEED"
    assert _git(repo, "cat-file", "-p", "stash@{0}^3:work.py") == UNSAVED
    assert _git(repo, "rev-parse", "stash@{0}^1") == _git(repo, "rev-parse", TASK_BRANCH)


def _branch_exists(repo: Path) -> bool:
    return bool(_git(repo, "rev-parse", "--verify", "--quiet", TASK_BRANCH, check=False))


def _commit_failed_attempt(agent: Agent) -> None:
    (agent.tree / "failed.py").write_text("failed attempt\n")
    _git(agent.tree, "add", "failed.py")
    _git(agent.tree, "commit", "-q", "-m", "failed attempt")


def _assert_reset(agent: Agent, base_sha: str) -> None:
    assert _git(agent.repo, "rev-parse", TASK_BRANCH) == base_sha
    assert (agent.tree / "README.md").read_text() == "seed\n"
    assert not (agent.tree / "failed.py").exists()
    assert not (agent.tree / "work.py").exists()


def _reset(agent: Agent, base_sha: str | None) -> None:
    agent.monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)
    _run(dispatch_mod.cleanup_failed_attempt(
        TASK_ID, str(agent.tree), [], output=[], base_sha=base_sha, expect_repository=True,
    ))


# (git arguments, text every correct answer contains)
READ_VIEW_CALLS = (
    (["diff", "HEAD"], "+SEED"),
    (["diff", "--stat"], "README.md"),
    (["status", "--porcelain"], " M README.md"),
    (["ls-files", "-m"], "README.md"),
)


def _read_view(agent: Agent) -> list[subprocess.CompletedProcess]:
    results = [git_run(args, agent.tree, timeout=30) for args, _ in READ_VIEW_CALLS]
    results.extend(
        _run(git_run_async(args, agent.tree, timeout=30)) for args, _ in READ_VIEW_CALLS
    )
    return results


def _check_read_view(agent: Agent, results: list[subprocess.CompletedProcess]) -> None:
    for result, (args, expected) in zip(results, READ_VIEW_CALLS * 2):
        assert result.returncode == 0, (args, result.stderr)
        assert expected in result.stdout, (args, result.stdout)


def _remove(agent: Agent, merged: bool) -> None:
    _run(dispatch_mod._cleanup_worktrees(
        str(agent.repo), {TASK_ID: str(agent.tree)},
        {TASK_ID} if merged else set(), agent.repo / ".forge-worktrees",
    ))


def _check_removed(agent: Agent, *, merged: bool) -> None:
    if not merged:
        _assert_work_stashed(agent.repo)
    assert agent.tree.exists() is agent.git_refuses_removal
    # A merged task's branch goes with its worktree; one git refused to
    # remove still holds the branch, which git then refuses to delete.
    assert _branch_exists(agent.repo) is (not merged or agent.git_refuses_removal)


def _check_retired(agent: Agent, problem: str | None) -> None:
    _assert_work_stashed(agent.repo)
    if agent.git_refuses_removal:
        assert problem and "could not remove" in problem, problem
        assert agent.tree.exists()
    else:
        assert problem is None, problem
        assert not agent.tree.exists()


def _assert_replaced_root_untouched(agent: Agent, task_branch_before: str) -> None:
    """R3162-01: nothing acted on the repository the symlink names, nor on
    the task branch of the project's repository."""
    target = agent.replaced_root
    assert target is not None and agent.replaced_link is not None
    assert agent.replaced_link.is_symlink(), f"the symlink at {agent.replaced_link} is gone"
    assert os.path.realpath(agent.tree) == os.path.realpath(target)
    assert _git(target, "symbolic-ref", "HEAD") == f"refs/heads/{TASK_BRANCH}"
    assert _git(target, "rev-parse", "HEAD") == agent.replaced_root_head
    assert (target / "work.py").read_text() == f"{UNSAVED}\n", "the agent's files were cleaned"
    assert _git(agent.repo, "rev-parse", TASK_BRANCH) == task_branch_before


def _refused_with_cleanup_error(agent: Agent, result: Any, error: Exception | None) -> None:
    assert isinstance(error, dispatch_mod.AttemptCleanupError), (
        f"not refused: returned {result!r}, raised {error!r}"
    )


def _refused_stash(agent: Agent, problem: Any, error: Exception | None) -> None:
    assert error is None, error
    assert problem, "the stash reported the work saved"


def _refused_dirty_check(agent: Agent, dirty: Any, error: Exception | None) -> None:
    assert error is None, error
    assert dirty and "could not be inspected safely" in dirty, dirty


def _refused_read_view(
    agent: Agent, results: list[subprocess.CompletedProcess], error: Exception | None,
) -> None:
    assert error is None, error
    assert all(result.returncode != 0 for result in results), [
        (result.returncode, result.stdout) for result in results
    ]


def _refused_removal(agent: Agent, _: Any, error: Exception | None) -> None:
    assert error is None, error
    # R3162-02: the branch the kept worktree holds is not deleted either.
    assert _branch_exists(agent.repo)


def _refused_retirement(agent: Agent, problem: Any, error: Exception | None) -> None:
    assert error is None, error
    assert problem and "not a registered worktree" in problem, problem


@dataclass(frozen=True)
class Operation:
    """One orchestrator cleanup call: ``prepare`` runs before the plant,
    ``run`` is the call under test and ``check`` asserts it did its job.
    ``refused`` asserts how it declines a worktree whose root the agent
    replaced with a symlink (R3162-01), given its result or exception."""

    run: Callable[[Agent], Any]
    check: Callable[[Agent, Any], None]
    refused: Callable[[Agent, Any, Exception | None], None]
    prepare: Callable[[Agent], None] = lambda agent: None
    removes_worktree: bool = False


OPERATIONS: dict[str, Operation] = {
    "stash": Operation(
        run=lambda agent: _run(_with_project_dir(
            dispatch_mod._stash_uncommitted_in_worktree,
            str(agent.tree), TASK_ID, TASK_BRANCH, project_dir=agent.repo,
        )),
        check=lambda agent, problem: (
            _assert_work_stashed(agent.repo) if problem is None
            else pytest.fail(f"the stash failed: {problem}")
        ),
        refused=_refused_stash,
    ),
    "dirty-check": Operation(
        run=lambda agent: _run(_with_project_dir(
            dispatch_mod._worktree_dirty_reason, str(agent.tree), project_dir=agent.repo,
        )),
        check=lambda agent, dirty: None if dirty and "uncommitted change" in dirty else (
            pytest.fail(f"the dirty check answered {dirty!r}")
        ),
        refused=_refused_dirty_check,
    ),
    "reset": Operation(
        prepare=_commit_failed_attempt,
        run=lambda agent: _reset(agent, _master(agent.repo)),
        check=lambda agent, _: _assert_reset(agent, _master(agent.repo)),
        refused=_refused_with_cleanup_error,
    ),
    "reset-no-base": Operation(
        prepare=_commit_failed_attempt,
        run=lambda agent: _reset(agent, None),
        check=lambda agent, _: _assert_reset(agent, _master(agent.repo)),
        refused=_refused_with_cleanup_error,
    ),
    "branch-check": Operation(
        run=lambda agent: _run(dispatch_mod._require_task_branch(str(agent.tree), TASK_BRANCH)),
        check=lambda agent, head: None if head == _git(agent.repo, "rev-parse", TASK_BRANCH) else (
            pytest.fail(f"the branch check read {head!r}")
        ),
        refused=_refused_with_cleanup_error,
    ),
    "read-view": Operation(run=_read_view, check=_check_read_view, refused=_refused_read_view),
    "remove-unmerged": Operation(
        run=lambda agent: _remove(agent, merged=False),
        check=lambda agent, _: _check_removed(agent, merged=False),
        refused=_refused_removal,
        removes_worktree=True,
    ),
    "remove-merged": Operation(
        run=lambda agent: _remove(agent, merged=True),
        check=lambda agent, _: _check_removed(agent, merged=True),
        refused=_refused_removal,
        removes_worktree=True,
    ),
    "retire": Operation(
        run=lambda agent: _run(dispatch_mod._retire_leftover_worktree(
            str(agent.repo), agent.tree, TASK_ID, TASK_BRANCH,
        )),
        check=_check_retired,
        refused=_refused_retirement,
        removes_worktree=True,
    ),
}


def _task_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_repo(tmp_path / "repo")
    worktree = repo / ".forge-worktrees" / f"task-{TASK_ID}"
    _git(repo, "worktree", "add", "-q", "-b", TASK_BRANCH, str(worktree), "master")
    return repo, worktree


def _check_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, name: str,
) -> None:
    operation = OPERATIONS[name]
    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    operation.prepare(agent)
    task_branch_before = _git(repo, "rev-parse", TASK_BRANCH)
    _plant(agent, vector)
    _leave_unsaved_work(worktree)
    recorder = GitRecorder(monkeypatch)

    # The markers are read before anything else can fail: an exception from
    # the call is raised only once they have been checked.
    error: Exception | None = None
    result: Any = None
    with recorder.recording():
        try:
            result = operation.run(agent)
        except Exception as exc:  # re-raised below, after the markers
            error = exc
    fired = agent.ran()
    _assert_nothing_ran(agent, vector, name, fired)
    if error is not None and not agent.root_replaced:
        raise error

    # With a replaced root, the worktree's path resolves to the agent's
    # repository: no git may find its repository from there either.
    assert recorder.discovery_in(worktree) == [], (
        f"git found its repository from inside the worktree ({vector}/{name})"
    )
    removals = recorder.worktree_removals()
    assert all(call.explicit_repository for call in removals), [
        call.argv for call in removals
    ]
    if agent.root_replaced:
        # R3162-01: every operation declines the worktree and leaves both
        # repositories as they were.
        operation.refused(agent, result, error)
        _assert_replaced_root_untouched(agent, task_branch_before)
        return
    if operation.removes_worktree:
        assert removals, "the worktree was never removed"
    operation.check(agent, result)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("vector", VECTORS)
def test_cleanup_runs_no_agent_program_and_does_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, operation: str,
) -> None:
    _check_operation(tmp_path, monkeypatch, vector, operation)


def _git_without_lazy_fetch_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """What every hardened call gets from a git older than 2.44 without the
    May 2024 backports: GIT_NO_LAZY_FETCH is not honoured, so it is gone."""
    monkeypatch.setattr(git_ops_mod, "GIT_HARDENING_ENV", MappingProxyType({
        key: value for key, value in git_ops_mod.GIT_HARDENING_ENV.items()
        if key != "GIT_NO_LAZY_FETCH"
    }))
    monkeypatch.delenv("GIT_NO_LAZY_FETCH", raising=False)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("vector", TRANSPORT_VECTORS)
def test_cleanup_starts_no_transport_on_a_git_without_lazy_fetch_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, operation: str,
) -> None:
    """Review item I3158-02: the cleanup's own git starts no transport at
    all (``GIT_ALLOW_PROTOCOL`` empty), whatever the git version."""
    _git_without_lazy_fetch_control(monkeypatch)
    _check_operation(tmp_path, monkeypatch, vector, operation)


def test_without_lazy_fetch_control_a_discovered_lookup_does_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the dimension above: the same plant, looked up
    by discovery from the worktree with the hardened env minus the lazy
    fetch pin, does run the agent's upload-pack."""
    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    _plant(agent, "promisor-uploadpack")
    _git_without_lazy_fetch_control(monkeypatch)

    git_run(["rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"], worktree)

    assert agent.ran() == ["promisor-uploadpack"]


# --- Whole isolated runs ---------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS)
@pytest.mark.parametrize(
    ("outcome", "commit"),
    [
        ("tests_passed", True),       # success with commits
        ("tests_failed", True),       # failure
        ("early_terminated", False),  # early termination
    ],
    ids=["success", "failure", "early-termination"],
)
def test_isolated_run_runs_no_agent_program_after_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, outcome: str, commit: bool,
) -> None:
    repo = _init_repo(tmp_path / "repo")
    master_before = _master(repo)
    agent = Agent(repo, None, tmp_path, monkeypatch)
    task_branch_after_agent: list[str] = []

    async def execute(agent_dir: str, task_branch: str):
        agent.worktree = Path(agent_dir)
        if commit:
            (agent.worktree / "NOTES.md").write_text("agent notes\n")
            _git(agent.worktree, "add", "NOTES.md")
            _git(agent.worktree, "commit", "-q", "-m", "agent notes")
        task_branch_after_agent.append(_git(repo, "rev-parse", TASK_BRANCH))
        _plant(agent, vector)
        _leave_unsaved_work(agent.worktree)
        return {"cost": 0.0, "duration": 0.0}, 1, outcome

    args = SimpleNamespace(security_review=False, dispatch_config={})
    run = _run(dispatch_mod.run_task_in_isolation(
        _task(TASK_ID), str(repo), {}, args, execute=execute,
    ))

    _assert_nothing_ran(agent, vector, f"isolated-{outcome}", agent.ran())
    assert agent.tree.exists() is agent.git_refuses_removal
    if agent.root_replaced:
        # R3162-01: the post-agent branch check refuses the swapped root, so
        # nothing is merged, and the branch and the agent's repository stay.
        assert run.outcome == "worktree_branch_mismatch", run
        assert run.merged_sha is None
        assert _master(repo) == master_before
        _assert_replaced_root_untouched(agent, task_branch_after_agent[0])
        return
    if run.merged_sha is not None:
        # Only a success merges, and only when the plant is not a merge
        # hazard (a program key the hardening pins, e.g. core.fsmonitor):
        # the agent's commit is on the default branch and its branch is gone.
        assert outcome == "tests_passed", run
        assert _master(repo) == run.merged_sha
        assert _git(repo, "cat-file", "-p", f"{run.merged_sha}:NOTES.md") == "agent notes"
        assert not _branch_exists(repo)
    else:
        # Nothing merged: the agent's uncommitted work is a stash on its branch.
        assert _master(repo) == master_before
        _assert_work_stashed(repo)


@pytest.mark.parametrize("vector", SYMLINKED_VECTORS)
@pytest.mark.parametrize("outcome", ["tests_failed", "tests_passed"])
def test_cli_dev_test_loop_refuses_a_root_swapped_during_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector: str, outcome: str,
) -> None:
    """R3162-01, the single-task ``--task --dev-test`` loop: the agent swaps
    its worktree root during the attempt. The branch check after the
    attempt stops the loop before any cleanup, so the failed attempt is not
    reset by discovery in the agent's repository (where ``git checkout``
    ran its smudge filter), and a passing attempt is not accepted."""
    import equipa.cli as cli_mod

    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)
    attempts: list[str] = []
    cleanups: list[str] = []
    task_branch_after_agent: list[str] = []
    real_cleanup = cli_mod.cleanup_failed_attempt

    async def attempt(task, project_dir, project_context, args):
        attempts.append(project_dir)
        if len(attempts) == 1:
            # Only the first attempt swaps the root; a second one starting
            # at all is the failure, and must not run the test's own git.
            _commit_failed_attempt(agent)
            task_branch_after_agent.append(_git(repo, "rev-parse", TASK_BRANCH))
            _plant(agent, vector)
            _leave_unsaved_work(agent.tree)
        return {"cost": 0.0, "duration": 0.0}, 1, outcome

    async def recorded_cleanup(task_id, project_dir, *args, **kwargs):
        cleanups.append(project_dir)
        return await real_cleanup(task_id, project_dir, *args, **kwargs)

    monkeypatch.setattr(cli_mod, "run_dev_test_loop", attempt)
    monkeypatch.setattr(cli_mod, "cleanup_failed_attempt", recorded_cleanup)
    args = SimpleNamespace(dispatch_config={
        "features": {"autoresearch": True}, "autoresearch_max_retries": 1,
    })

    # The markers are read before an exception from the loop is raised.
    error: Exception | None = None
    final_outcome = None
    try:
        _, _, final_outcome = _run(cli_mod._run_dev_test_mode(
            _task(TASK_ID), str(worktree), {}, args, task_branch=TASK_BRANCH,
        ))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, vector, f"cli-{outcome}", agent.ran())
    if error is not None:
        raise error
    assert final_outcome == "worktree_branch_mismatch"
    assert attempts == [str(worktree)]
    assert cleanups == [], "the swapped worktree reached cleanup_failed_attempt"
    _assert_replaced_root_untouched(agent, task_branch_after_agent[0])


# --- A project that was not git at dispatch (R3165-01) --------------------------

NON_GIT_VECTOR = "non-git-project"
# N1 (task #3168). Read when the module has it, so the matrix also runs on
# trees before it and fails there for what the code does, not an
# AttributeError (as _with_project_dir does for task #3158).
REPOSITORY_APPEARED = getattr(dispatch_mod, "REPOSITORY_APPEARED_OUTCOME", "repository_appeared")


def _non_git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('project')\n")
    return project


def _make_agent_repository(agent: Agent, project: Path) -> None:
    """What an agent can do during a failed attempt in a project that was
    not git at dispatch: make it a repository of its own."""
    _agent_repository(agent, project, NON_GIT_VECTOR)
    agent.ran_while_planting = agent.ran()


async def _run_non_git_loop(loop: str, project: Path, output: list[str]) -> str:
    """The failed-attempt path of ``loop`` exactly as production calls it
    for a project that is not git: no worktree, so no task branch."""
    import equipa.cli as cli_mod

    autoresearch = {"features": {"autoresearch": True}, "autoresearch_max_retries": 1}
    if loop == "cli":
        args = SimpleNamespace(dispatch_config=autoresearch)
        _, _, outcome = await cli_mod._run_dev_test_mode(_task(TASK_ID), str(project), {}, args)
        return outcome
    _, _, outcome, _, _, _ = await dispatch_mod.run_dev_test_loop_with_autoresearch(
        _task(TASK_ID), str(project), {}, SimpleNamespace(), autoresearch, output=output,
    )
    return outcome


def _record_gate_audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """The durable GATE-AUDIT records (message, event) both loops write."""
    records: list[tuple[str, str | None]] = []

    def record(message: str, task_id: int | None = None, **kwargs: Any) -> None:
        records.append((message, kwargs.get("event")))

    monkeypatch.setattr(dispatch_mod, "log_gate_audit", record)
    return records


def _patch_non_git_loops(monkeypatch: pytest.MonkeyPatch, attempt: Any) -> None:
    import equipa.cli as cli_mod

    monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)
    monkeypatch.setattr(dispatch_mod, "fetch_task", lambda task_id: _task(task_id))
    for module in (cli_mod, dispatch_mod):
        monkeypatch.setattr(module, "run_dev_test_loop", attempt)


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_failed_attempt_in_a_non_git_project_runs_no_git_in_the_agents_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, loop: str,
) -> None:
    """R3165-01: in a project that was not git at dispatch, the agent runs
    ``git init`` during a failed attempt. ``cleanup_failed_attempt`` asked
    ``_is_git_repo`` by discovery, found the agent's repository and checked
    out its ``main``, which ran the agent's smudge filter in the
    orchestrator. Now no git step runs there. ``loop`` is the single-task
    ``--task --dev-test`` loop (``cli``) or the autoresearch wrapper the
    parallel and per-project loops share (``dispatch``), each called without
    a task branch as production does.

    N1 (task #3168): the repository is no longer only reported and retried
    in (a retry let the dev-test loop run git there). The task stops right
    after the attempt, blocked, with a durable GATE-AUDIT record, before
    any cleanup. The cleanup's own no-git path is checked on the agent's
    repository by ``test_cleanup_of_a_non_git_project_runs_no_git_in_the_agents_repository``."""
    import equipa.cli as cli_mod

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []
    cleanups: list[Any] = []
    real_cleanup = dispatch_mod.cleanup_failed_attempt

    async def attempt(task, project_dir, project_context, args, output=None):
        attempts.append(project_dir)
        if len(attempts) == 1:
            with recorder.paused():
                _make_agent_repository(agent, project)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    async def recorded_cleanup(*args, **kwargs):
        cleanups.append(kwargs)
        return await real_cleanup(*args, **kwargs)

    _patch_non_git_loops(monkeypatch, attempt)
    for module in (cli_mod, dispatch_mod):
        monkeypatch.setattr(module, "cleanup_failed_attempt", recorded_cleanup)
    output: list[str] = []

    # The markers are read before an exception from the loop is raised.
    error: Exception | None = None
    outcome = None
    try:
        with recorder.recording():
            outcome = _run(_run_non_git_loop(loop, project, output))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, NON_GIT_VECTOR, f"{loop}-cleanup", agent.ran())
    if error is not None:
        raise error
    assert recorder.calls == [], [call.argv for call in recorder.calls]
    # N1: blocked after the attempt, with no cleanup and no retry.
    assert outcome == REPOSITORY_APPEARED
    assert attempts == [str(project)]
    assert cleanups == []
    # The agent's repository is as the agent left it.
    assert _git(project, "symbolic-ref", "HEAD") == f"refs/heads/{TASK_BRANCH}"
    assert _branch_exists(project)
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (
        f"after attempt 1 (tests_failed): a git repository appeared at {project / '.git'}"
        in audit[0][0]
    ), audit
    logged = "\n".join(output) + capsys.readouterr().out
    assert f"[GATE-AUDIT] task={TASK_ID} event=repository-appeared" in logged, logged


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_a_repository_already_in_a_non_git_project_stops_the_task_before_any_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: str,
) -> None:
    """N1 (task #3168): the per-project loop decides "not git" once per run,
    so an earlier task's agent can leave a repository the next task would
    run in, and a later dispatch would adopt as the project's checkout.
    Found before the first attempt, the task stops there: no agent runs and
    no git runs."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _make_agent_repository(agent, project)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        attempts.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed"

    _patch_non_git_loops(monkeypatch, attempt)

    with recorder.recording():
        outcome = _run(_run_non_git_loop(loop, project, []))

    _assert_nothing_ran(agent, NON_GIT_VECTOR, f"{loop}-before-attempt", agent.ran())
    assert recorder.calls == [], [call.argv for call in recorder.calls]
    assert attempts == []
    assert outcome == REPOSITORY_APPEARED
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert f"before attempt 1: a git repository appeared at {project / '.git'}" in audit[0][0]


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_failed_attempt_in_a_non_git_project_without_a_repository_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, loop: str,
) -> None:
    """N1 stops only a task whose project gained a repository: a failed
    attempt that left none is cleaned up (no git) and retried as before."""
    project = _non_git_project(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        attempts.append(project_dir)
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_failed"

    _patch_non_git_loops(monkeypatch, attempt)
    output: list[str] = []

    with recorder.recording():
        outcome = _run(_run_non_git_loop(loop, project, output))

    assert recorder.calls == [], [call.argv for call in recorder.calls]
    assert outcome == "tests_failed"
    assert attempts == [str(project), str(project)]
    assert audit == []
    logged = "\n".join(output) + capsys.readouterr().out
    assert f"Reset task #{TASK_ID} to todo" in logged, logged


def test_cleanup_of_a_non_git_project_runs_no_git_in_the_agents_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3165-01 at the cleanup itself, which N1 now keeps the loops from
    reaching with a repository present: with the agent's repository in the
    project, ``expect_repository=False`` starts no git process, runs none of
    the agent's programs and reports the repository."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _make_agent_repository(agent, project)
    monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)
    recorder = GitRecorder(monkeypatch)
    output: list[str] = []

    with recorder.recording():
        _run(dispatch_mod.cleanup_failed_attempt(
            TASK_ID, str(project), [], output=output, expect_repository=False,
        ))

    _assert_nothing_ran(agent, NON_GIT_VECTOR, "cleanup", agent.ran())
    assert recorder.calls == [], [call.argv for call in recorder.calls]
    assert any(
        f"A git repository appeared at {project / '.git'}" in line for line in output
    ), output
    assert _git(project, "symbolic-ref", "HEAD") == f"refs/heads/{TASK_BRANCH}"


def test_cleanup_of_a_non_git_project_without_a_repository_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _non_git_project(tmp_path)
    monkeypatch.setattr(dispatch_mod, "get_db_connection", _NoDb)
    recorder = GitRecorder(monkeypatch)
    output: list[str] = []

    with recorder.recording():
        _run(dispatch_mod.cleanup_failed_attempt(
            TASK_ID, str(project), [], output=output, expect_repository=False,
        ))

    assert recorder.calls == []
    assert not any("appeared" in line for line in output), output
    assert any(f"Reset task #{TASK_ID} to todo" in line for line in output), output


def test_checking_out_the_agents_main_does_run_its_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the plant above: the checkout the cleanup used
    to run in the agent's repository runs the agent's filter."""
    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _make_agent_repository(agent, project)

    _git(project, "checkout", "-q", "main")

    assert f"{NON_GIT_VECTOR}-filter" in agent.ran()


# --- R3166-01 (task #3168): the change checks during and after the agent ------

CHANGE_CHECK_VECTOR = "non-git-change-check"

# A fake Claude CLI that replays stream.jsonl from its own directory (the
# agent env is allowlisted, so nothing comes through the environment). A
# {"run": "<script>"} line is not printed: the agent runs that shell script,
# as its Bash tool would, before the tool result that follows it.
FAKE_AGENT_CLI = '''import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(here, "stream.jsonl"), encoding="utf-8"):
    event = json.loads(line)
    if "run" in event:
        subprocess.run(["/bin/sh", os.path.join(here, event["run"])], check=True)
        continue
    sys.stdout.write(line)
    sys.stdout.flush()
'''


def _plant_script(agent: Agent, project: Path) -> str:
    """What the agent's first Bash call does: make ``project`` a repository
    of its own whose clean filter, selected for every path, is a program of
    the agent's, and leave a committed file changed (same size, so git has
    to read it through the filter)."""
    git = shlex.quote(shutil.which("git") or "git")
    program = shlex.quote(agent.program(f"{CHANGE_CHECK_VECTOR}-filter", "exec cat"))
    return "\n".join([
        "set -e",
        f"cd {shlex.quote(str(project))}",
        f"{git} init -q -b main",
        "printf '* filter=probe\\n' > .gitattributes",
        f"{git} add .gitattributes app.py",
        f"{git} -c user.name=agent -c user.email=agent@example.invalid commit -q -m agent",
        # Defined after the commit, so planting never runs it.
        f"{git} config filter.probe.clean {program}",
        f"{git} config filter.probe.smudge {program}",
        "mkdir -p .git/info",
        "printf '* filter=probe\\n' > .git/info/attributes",
        "printf '%s\\n' \"PRINT('project')\" > app.py",
        "",
    ])


def _tool_call(tool_id: str, command: str) -> list[dict[str, Any]]:
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": "Bash",
             "input": {"command": command}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "",
             "is_error": False}]}},
    ]


def _fake_agent(tmp_path: Path, agent: Agent, project: Path) -> list[str]:
    """argv of a fake agent whose first Bash call runs :func:`_plant_script`
    and whose second one changes nothing."""
    directory = tmp_path / "fake-agent"
    directory.mkdir()
    (directory / "plant.sh").write_text(_plant_script(agent, project))
    first, first_result = _tool_call("toolu_plant", "git init")
    events = [
        first, {"run": "plant.sh"}, first_result,
        *_tool_call("toolu_status", "ls"),
        {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 2, "total_cost_usd": 0.0},
    ]
    (directory / "stream.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
    )
    cli = directory / "fake_claude.py"
    cli.write_text(FAKE_AGENT_CLI)
    return [sys.executable, str(cli)]


def _assert_planted(agent: Agent, project: Path) -> None:
    """The fake agent did plant (so no marker is not a vacuous pass)."""
    assert (project / ".git").is_dir(), "the fake agent made no repository"
    assert _git(project, "config", "filter.probe.clean") == str(
        agent.bin / f"{CHANGE_CHECK_VECTOR}-filter"
    )


@pytest.mark.parametrize("loop", ["cli", "dispatch"])
def test_change_checks_run_no_git_in_a_repository_the_agent_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: str,
) -> None:
    """R3166-01: in a project that was not git at dispatch, the agent runs
    ``git init`` and plants a clean filter. After its next tool result the
    streaming runner's ``_check_git_changes`` ran ``git diff --stat`` there
    by discovery, which ran the filter in the orchestrator (no failed
    attempt needed); so would the progress checks the dev-test loop makes
    after an agent run. Now the attempt runs under the dispatch's non-git
    record: the runner starts no git there, ends the run when the
    repository appears, the checks after it run no git, and N1 blocks the
    task. The agent is a real subprocess under the real streaming runner,
    inside each loop as production calls it for a project that is not git."""
    import equipa.agent_runner as agent_runner
    from equipa.monitoring import _check_git_changes, has_branch_commits
    from equipa.parsing import verify_files_changed

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    agent_cmd = _fake_agent(tmp_path, agent, project)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    attempts: list[str] = []
    runs: list[dict[str, Any]] = []

    async def attempt(task, project_dir, project_context, args, output=None):
        """One dev-test attempt: the streamed agent run, then the change
        checks made on its result."""
        attempts.append(project_dir)
        run = await agent_runner.run_agent_streaming(
            agent_cmd, role="developer", output=output, max_turns=40,
            project_dir=project_dir,
        )
        runs.append(run)
        _check_git_changes(project_dir)
        has_branch_commits(project_dir)
        verify_files_changed(["app.py"], project_dir)
        return run, 1, "early_terminated" if run.get("early_terminated") else "tests_failed"

    _patch_non_git_loops(monkeypatch, attempt)

    # The markers are read before an exception from the loop is raised.
    error: Exception | None = None
    outcome = None
    try:
        with recorder.recording():
            outcome = _run(_run_non_git_loop(loop, project, []))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, CHANGE_CHECK_VECTOR, f"{loop}-change-check", agent.ran())
    if error is not None:
        raise error
    _assert_planted(agent, project)
    assert recorder.discovery_in(project) == [], recorder.discovery_in(project)
    assert len(runs) == 1, runs
    assert runs[0].get("early_terminated"), runs[0]
    assert (
        f"a git repository appeared at {project / '.git'}" in runs[0]["early_term_reason"]
    ), runs[0]
    assert outcome == REPOSITORY_APPEARED
    assert attempts == [str(project)]
    assert [event for _, event in audit] == ["repository-appeared"], audit


def test_the_change_check_runs_the_agents_filter_when_nothing_records_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the plant above: without the dispatch's non-git
    record, the orchestrator's change check does run the agent's filter."""
    from equipa.monitoring import _check_git_changes

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    _fake_agent(tmp_path, agent, project)
    subprocess.run(["/bin/sh", str(tmp_path / "fake-agent" / "plant.sh")], check=True)
    assert agent.ran() == [], "planting ran the filter"

    assert _check_git_changes(str(project)) is True

    assert f"{CHANGE_CHECK_VECTOR}-filter" in agent.ran()


# --- R3166-01 / N1 (task #3168): the parallel loop's review of a non-git task --

PLANTING_TASK_ID = TASK_ID + 1


def _reviewer_agent(tmp_path: Path) -> list[str]:
    """argv of a fake reviewer whose one Bash call changes nothing: any git
    its run starts in the project comes from the orchestrator."""
    directory = tmp_path / "fake-reviewer"
    directory.mkdir()
    events = [
        *_tool_call("toolu_read", "ls"),
        {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 1, "total_cost_usd": 0.0},
    ]
    (directory / "stream.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
    )
    cli = directory / "fake_claude.py"
    cli.write_text(FAKE_AGENT_CLI)
    return [sys.executable, str(cli)]


@dataclass
class ParallelNonGitRun:
    """What the parallel loop did for its non-git tasks."""

    statuses: dict[int, str] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    # Index into GitRecorder.calls when the agent's repository was planted.
    planted_at: int | None = None


def _patch_parallel_non_git_review(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    task_ids: list[int],
    attempt: Callable[..., Any],
    reviewer: Callable[..., Any],
) -> ParallelNonGitRun:
    """``run_parallel_tasks`` for a project that is not git, with the
    attempts and the reviewer's agent replaced and everything between them
    (the run's non-git record, the N1 check, ``review_task_branch``) as in
    production."""
    run = ParallelNonGitRun()

    def update_status(task_id, outcome, **kwargs):
        run.statuses[task_id] = outcome

    monkeypatch.setattr(
        dispatch_mod, "fetch_tasks_by_ids",
        lambda ids: [_task(task_id) for task_id in task_ids if task_id in ids],
    )
    monkeypatch.setattr(dispatch_mod, "resolve_project_dir", lambda _task: str(project))
    monkeypatch.setattr(dispatch_mod, "fetch_project_context", lambda _pid: {})
    monkeypatch.setattr(dispatch_mod, "run_dev_test_loop_with_autoresearch", attempt)
    monkeypatch.setattr(dispatch_mod, "run_security_review", reviewer)
    monkeypatch.setattr(dispatch_mod, "update_task_status", update_status)
    monkeypatch.setattr(dispatch_mod, "record_agent_run", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_mod, "get_role_model", lambda *a, **k: "claude-test")
    monkeypatch.setattr(dispatch_mod, "get_role_turns", lambda *a, **k: 20)
    monkeypatch.setattr("equipa.scaffold.ensure_scaffold", lambda *a, **k: False)
    return run


def _parallel_args() -> SimpleNamespace:
    return SimpleNamespace(
        yes=True, max_concurrent=2, use_flow=False, security_review=True,
        dispatch_config={},
    )


def _plant_change_check_repository(
    agent: Agent, project: Path, tmp_path: Path, recorder: GitRecorder,
    run: ParallelNonGitRun,
) -> None:
    """What another task's agent does in the shared project: make it a
    repository whose clean filter is its program (see :func:`_plant_script`)."""
    _fake_agent(tmp_path, agent, project)
    with recorder.paused():
        subprocess.run(["/bin/sh", str(tmp_path / "fake-agent" / "plant.sh")], check=True)
    agent.ran_while_planting = agent.ran()
    run.planted_at = len(recorder.calls)


def _discovery_after_planting(
    recorder: GitRecorder, run: ParallelNonGitRun, project: Path,
) -> list[tuple[str, ...]]:
    """git calls that found the agent's repository by discovery. The loop
    asks whether the project is git once at dispatch, before any plant."""
    assert run.planted_at is not None, "the agent's repository was never planted"
    root = os.path.realpath(project)
    return [
        call.argv for call in recorder.calls[run.planted_at:]
        if not call.explicit_repository
        and (call.where == root or call.where.startswith(root + os.sep))
    ]


def test_parallel_review_of_a_non_git_task_runs_no_git_in_a_repository_another_task_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """R3166-01 / N1 in the parallel loop: its non-git tasks share the
    project. Task ``TASK_ID`` finished its attempts cleanly; then the agent
    of task ``PLANTING_TASK_ID`` made the project a repository with a clean
    filter of its own. The security review of ``TASK_ID`` ran after its
    attempts, outside the attempts' non-git record: the reviewer's
    streaming runner found ``.git``, so its change check ran ``git diff``
    there, and the agent's filter in the orchestrator. Now the task is
    blocked before its review starts, with a durable GATE-AUDIT record."""
    import equipa.agent_runner as agent_runner

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    reviewer_cmd = _reviewer_agent(tmp_path)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    planted: dict[str, asyncio.Event] = {}
    run: ParallelNonGitRun

    def planted_event() -> asyncio.Event:
        return planted.setdefault("event", asyncio.Event())

    async def attempt(task, task_dir, project_context, args, config, output=None,
                      task_branch=None):
        assert task_branch is None
        if task["id"] == PLANTING_TASK_ID:
            _plant_change_check_repository(agent, project, tmp_path, recorder, run)
            planted_event().set()
            # What the autoresearch wrapper returns after its own N1 check.
            return {"cost": 0.0, "duration": 0.0}, 1, REPOSITORY_APPEARED, 0.0, 0.0, task
        # This task's attempts ended before the repository appeared.
        await planted_event().wait()
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed", 0.0, 0.0, task

    async def reviewer(review_task, task_dir, project_context, args, output=None,
                       stable_project_dir=None):
        result = await agent_runner.run_agent_streaming(
            reviewer_cmd, role="security-reviewer", output=output, max_turns=40,
            project_dir=task_dir,
        )
        run.reviews.append({"task": review_task["id"], **result})
        return result

    run = _patch_parallel_non_git_review(
        monkeypatch, project, [TASK_ID, PLANTING_TASK_ID], attempt, reviewer,
    )

    # The markers are read before an exception from the loop is raised.
    error: Exception | None = None
    try:
        with recorder.recording():
            _run(dispatch_mod.run_parallel_tasks([TASK_ID, PLANTING_TASK_ID], _parallel_args()))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, CHANGE_CHECK_VECTOR, "parallel-review", agent.ran())
    if error is not None:
        raise error
    _assert_planted(agent, project)
    assert _discovery_after_planting(recorder, run, project) == []
    assert run.reviews == [], run.reviews
    assert run.statuses == {TASK_ID: REPOSITORY_APPEARED, PLANTING_TASK_ID: REPOSITORY_APPEARED}
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (
        f"before the security review (tests_passed): a git repository appeared at "
        f"{project / '.git'}" in audit[0][0]
    ), audit
    logged = capsys.readouterr().out
    assert f"[GATE-AUDIT] task={TASK_ID} event=repository-appeared" in logged, logged


def test_parallel_reviewer_of_a_non_git_task_starts_no_git_in_a_repository_made_during_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3166-01: the N1 check before the review reads the filesystem once.
    A repository another task's agent makes after it, as the reviewer
    starts, is caught by the non-git record the whole parallel task now
    runs under: the reviewer's streaming runner does not start the agent
    and runs no git. Without the record it found ``.git`` and its change
    check ran the agent's filter."""
    import equipa.agent_runner as agent_runner

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    reviewer_cmd = _reviewer_agent(tmp_path)
    _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    run: ParallelNonGitRun

    async def attempt(task, task_dir, project_context, args, config, output=None,
                      task_branch=None):
        assert task_branch is None
        return {"cost": 0.0, "duration": 0.0}, 1, "tests_passed", 0.0, 0.0, task

    async def reviewer(review_task, task_dir, project_context, args, output=None,
                       stable_project_dir=None):
        _plant_change_check_repository(agent, project, tmp_path, recorder, run)
        result = await agent_runner.run_agent_streaming(
            reviewer_cmd, role="security-reviewer", output=output, max_turns=40,
            project_dir=task_dir,
        )
        run.reviews.append({"task": review_task["id"], **result})
        return result

    run = _patch_parallel_non_git_review(monkeypatch, project, [TASK_ID], attempt, reviewer)

    error: Exception | None = None
    try:
        with recorder.recording():
            _run(dispatch_mod.run_parallel_tasks([TASK_ID], _parallel_args()))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, CHANGE_CHECK_VECTOR, "parallel-reviewer", agent.ran())
    if error is not None:
        raise error
    _assert_planted(agent, project)
    assert _discovery_after_planting(recorder, run, project) == []
    [review] = run.reviews
    assert review["early_terminated"], review
    assert f"a git repository appeared at {project / '.git'}" in review["early_term_reason"]
    assert run.statuses[TASK_ID] != "tests_passed", run.statuses


# --- R3166-01 / N1 (task #3168): the CLI's single-agent mode -------------------


def test_single_agent_run_in_a_non_git_project_runs_no_git_in_the_agents_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """R3166-01 / N1 in ``--task`` without ``--dev-test``: the agent of a
    project that was not git at dispatch made it a repository with a clean
    filter of its own and reported success. The no-output guard then asked
    git for the changed files (``single_agent_guard._git_diff_files``: no
    ``main...HEAD`` change and no ``HEAD~1``, so ``git status --porcelain``),
    which ran the agent's filter in the orchestrator. Now the task stops
    after the agent, blocked with a durable GATE-AUDIT record, and no git
    runs in the project. Driven through ``run_mode_task`` as production
    calls it."""
    import equipa.cli as cli_mod

    project = _non_git_project(tmp_path)
    agent = Agent(project, None, tmp_path, monkeypatch)
    audit = _record_gate_audit(monkeypatch)
    recorder = GitRecorder(monkeypatch)
    run = ParallelNonGitRun()
    outcomes: list[str] = []
    task = _task(TASK_ID)

    for name, value in {
        "fetch_task": lambda _id: task,
        "resolve_project_dir": lambda _task: str(project),
        "fetch_project_context": lambda _pid: {},
        "_auto_snapshot_dispatch": lambda *a, **k: None,
        "get_task_complexity": lambda _task: "medium",
        "get_role_model": lambda *a, **k: "claude-test",
        "get_role_turns": lambda *a, **k: 20,
        "calculate_dynamic_budget": lambda turns, **k: (turns, turns),
        "load_checkpoint": lambda *a, **k: (None, None),
        "verify_task_updated": lambda _id: (True, "ok"),
        "print_summary": lambda *a, **k: None,
        "build_system_prompt": lambda *a, **k: "prompt",
    }.items():
        monkeypatch.setattr(cli_mod, name, value)

    async def record_outcome(task, result, outcome, *args, **kwargs):
        outcomes.append(outcome)

    @contextmanager
    def fake_build_cli_command(system_prompt, project_dir, *args, **kwargs):
        yield ["fake-claude", project_dir]

    async def fake_agent(cmd, *args, **kwargs):
        """The agent's Bash calls made the repository; its run succeeded."""
        _plant_change_check_repository(agent, project, tmp_path, recorder, run)
        result = {
            "success": True, "result_text": "RESULT: success", "stdout": "",
            "cost": 0.0, "duration": 0.0, "files_changed": ["app.py"],
        }
        return result if "role" in kwargs else (result, 1)

    monkeypatch.setattr(cli_mod, "_post_task_telemetry", record_outcome)
    monkeypatch.setattr(cli_mod, "build_cli_command", fake_build_cli_command)
    monkeypatch.setattr(cli_mod, "run_agent_streaming", fake_agent)
    monkeypatch.setattr(cli_mod, "run_agent_with_retries", fake_agent)
    args = argparse.Namespace(
        task=TASK_ID, project=None, role="developer", dev_test=False, dry_run=False,
        yes=True, retries=0, dispatch_config={}, security_review=True,
    )

    error: Exception | None = None
    try:
        with recorder.recording():
            _run(cli_mod.run_mode_task(args))
    except Exception as exc:  # re-raised below, after the markers
        error = exc

    _assert_nothing_ran(agent, CHANGE_CHECK_VECTOR, "cli-single-agent", agent.ran())
    if error is not None:
        raise error
    _assert_planted(agent, project)
    assert _discovery_after_planting(recorder, run, project) == []
    assert outcomes == [REPOSITORY_APPEARED]
    assert [event for _, event in audit] == ["repository-appeared"], audit
    assert (
        f"after the developer agent: a git repository appeared at {project / '.git'}"
        in audit[0][0]
    ), audit
    assert f"[GATE-AUDIT] task={TASK_ID} event=repository-appeared" in capsys.readouterr().out


# --- The three remaining discovery calls, one by one ----------------------------


def test_reset_without_a_base_reads_the_default_branch_of_the_registered_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worktree's ``.git`` names the agent's repository, which has a
    ``main`` and no ``master``. Read by discovery, ``main`` was taken as the
    default branch and the reset could not find its fork point."""
    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    other = _init_repo(tmp_path / "agent-repo")
    _git(other, "branch", "-m", "master", "main")
    (worktree / ".git").write_text(f"gitdir: {other / '.git'}\n")

    assert git_ops_mod.get_trusted_default_branch(
        worktree, common_dir=repo / ".git",
    ) == "master"
    _reset(agent, None)

    assert _git(repo, "rev-parse", TASK_BRANCH) == _master(repo)


def test_cleanup_failed_attempt_never_asks_git_whether_a_task_worktree_is_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(dispatch_mod, "_is_git_repo", lambda path: asked.append(path) or True)

    _reset(agent, _master(repo))

    assert asked == []
    assert _git(repo, "rev-parse", TASK_BRANCH) == _master(repo)


def test_cleanup_keeps_the_worktree_when_the_repository_cannot_be_located(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    repo, worktree = _task_worktree(tmp_path)

    async def no_common_dir(directory):
        return None

    monkeypatch.setattr(dispatch_mod, "_git_common_dir", no_common_dir)
    _run(dispatch_mod._cleanup_worktrees(
        str(repo), {TASK_ID: str(worktree)}, {TASK_ID}, repo / ".forge-worktrees",
    ))

    assert worktree.exists()
    assert _branch_exists(repo)
    out = capsys.readouterr().out
    assert f"Cleanup error for task #{TASK_ID} (branch '{TASK_BRANCH}')" in out, out
    assert "could not locate the repository" in out, out


@pytest.mark.parametrize("merged", [True, False], ids=["merged", "unmerged"])
@pytest.mark.parametrize("refusal", ["locked", "redirected-gitfile"])
def test_cleanup_logs_a_worktree_git_refuses_to_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, refusal: str, merged: bool,
) -> None:
    """R3162-02: git refuses ``worktree remove --force`` for a worktree the
    agent locked or whose ``.git`` it rewrote. The kept directory is logged
    as a cleanup error, and the branch it still holds is not deleted."""
    repo, worktree = _task_worktree(tmp_path)
    agent = Agent(repo, worktree, tmp_path, monkeypatch)
    if refusal == "locked":
        _git(repo, "worktree", "lock", "--reason", "agent", str(worktree))
    else:
        _plant(agent, "redirected-gitfile")
    recorder = GitRecorder(monkeypatch)
    capsys.readouterr()

    with recorder.recording():
        _remove(agent, merged=merged)

    out = capsys.readouterr().out
    assert f"Cleanup error for task #{TASK_ID} (branch '{TASK_BRANCH}')" in out, out
    assert f"git refused to remove worktree {worktree}" in out, out
    assert worktree.exists()
    assert _branch_exists(repo)
    assert not [call.argv for call in recorder.calls if "branch" in call.argv], (
        "branch -D ran for a branch the kept worktree holds"
    )
    assert agent.ran() == []


# --- The read-only view takes attributes from the empty tree ----------------------


def _diff_readme(worktree: Path) -> list[subprocess.CompletedProcess]:
    args = ["diff", "HEAD", "--", "README.md"]
    return [git_run(args, worktree, timeout=30), _run(git_run_async(args, worktree, timeout=30))]


def test_read_view_ignores_the_work_tree_attributes(tmp_path: Path) -> None:
    """``-diff`` in the work tree's ``.gitattributes`` would make the diff
    read "Binary files differ"; with attributes from the empty tree the
    change is shown as text."""
    _repo, worktree = _task_worktree(tmp_path)
    (worktree / ".gitattributes").write_text("README.md -diff\n")
    (worktree / "README.md").write_text("SEED\n")

    for diff in _diff_readme(worktree):
        assert diff.returncode == 0, diff.stderr
        assert "+SEED" in diff.stdout, diff.stdout


def test_read_view_ignores_the_global_attributes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``core.attributesFile`` set, git reads
    ``$HOME/.config/git/attributes``, which the agent can write."""
    _repo, worktree = _task_worktree(tmp_path)
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "attributes").write_text("README.md -diff\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (worktree / "README.md").write_text("SEED\n")

    for diff in _diff_readme(worktree):
        assert diff.returncode == 0, diff.stderr
        assert "+SEED" in diff.stdout, diff.stdout


def test_read_view_of_a_sha256_repository_uses_its_empty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--object-format=sha256", "-b", "master")
    _git(repo, "config", "user.email", "test@forgeborn.dev")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    worktree = repo / ".forge-worktrees" / f"task-{TASK_ID}"
    _git(repo, "worktree", "add", "-q", "-b", TASK_BRANCH, str(worktree), "master")
    (worktree / ".gitattributes").write_text("README.md -diff\n")
    (worktree / "README.md").write_text("SEED\n")

    for diff in _diff_readme(worktree):
        assert diff.returncode == 0, diff.stderr
        assert "+SEED" in diff.stdout, diff.stdout


def test_read_view_refuses_an_unknown_object_format(tmp_path: Path) -> None:
    view = git_ops_mod._WorktreeView(
        work_tree=str(tmp_path), relative=".", common_dir=str(tmp_path),
        git_dir=str(tmp_path), work_tree_fd=None,
    )
    listing = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="extensions.objectformat\nsha512\0",
    )

    with pytest.raises(git_ops_mod.AgentWorktreeGitError, match="unknown object format"):
        git_ops_mod._with_private_common_dir(view, [listing])
