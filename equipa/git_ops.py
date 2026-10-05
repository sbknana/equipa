"""EQUIPA git operations: repo setup, language detection, and git helpers.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
All functions are re-exported via equipa/__init__.py for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import errno
import hashlib
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from types import MappingProxyType

from equipa.constants import (
    GIT_DEFAULT_TIMEOUT,
    GITIGNORE_TEMPLATES,
    GITHUB_OWNER,
    PROJECT_DIRS,
)
from equipa.role_resolver import _GIT_SAFE_CONFIG, WORKTREE_BASE_DIRNAME

logger = logging.getLogger(__name__)


class WorktreeBranchConflictError(RuntimeError):
    """Raised when ``create_task_worktree`` would reuse an existing branch.

    Task #2488 root cause: the orchestrator was not isolating tasks in
    separate worktrees, so when task #N+1's description happened to
    reference task #N's branch, commits landed on #N instead of being
    rejected. This defensive invariant fails fast at worktree-creation
    time when ``forge-task-<id>`` already exists, rather than silently
    proceeding and leaving the operator to diagnose a missing branch in
    the merge step.
    """


class MissingTaskBranchError(RuntimeError):
    """Raised when ``merge_task_branch`` is asked to merge a branch that
    does not exist.

    Task #2488: previously the merge step swallowed this case as
    "branch has no commits ahead of HEAD" and silently skipped the
    merge, masking the underlying worktree-isolation bug. Raise loudly
    so the orchestrator can surface the failure to the operator.
    """


@dataclass(frozen=True)
class TaskWorktree:
    """Handle returned by :func:`create_task_worktree`.

    Attributes:
        path: Filesystem path of the isolated worktree the agent should
            work inside.
        branch: Name of the fresh branch backing the worktree
            (``forge-task-<id>``).
        repo: Path of the main checkout the worktree was attached to.
            Needed by :func:`remove_task_worktree` so the caller does not
            have to remember it separately.
    """

    path: Path
    branch: str
    repo: Path


def _task_branch_name(task_id: int) -> str:
    return f"forge-task-{task_id}"


def _branch_exists(repo_path: Path, branch: str) -> bool:
    result = git_run(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        repo_path, timeout=10,
    )
    return result.returncode == 0


def create_task_worktree(
    repo: str | Path,
    task_id: int,
    base_ref: str | None = None,
) -> TaskWorktree:
    """Create a fresh ``git worktree`` for ``task_id`` branched from
    ``base_ref``.

    Implements the per-task isolation contract documented in task #2488:

    1. Every task gets a brand-new branch named ``forge-task-<id>``.
    2. If that branch already exists, raise
       :class:`WorktreeBranchConflictError` rather than silently reusing
       it. The previous behaviour was to ``git checkout`` the stale
       branch in the main checkout, so commits for task N+1 landed on
       task N's branch when a description happened to reference it.
    3. The worktree lives in ``<repo_parent>/.equipa-worktrees/task-<id>``
       — a sibling directory of the main checkout — so concurrent
       dispatches do not share working-tree state.
    4. ``base_ref`` defaults to the repo's detected default branch when
       not supplied, so callers do not have to special-case
       ``master`` vs ``main``.

    Returns a :class:`TaskWorktree` handle. Always pair with
    :func:`remove_task_worktree` (in a ``try``/``finally``) so the main
    checkout returns to a clean state on the default branch.

    Raises:
        WorktreeBranchConflictError: ``forge-task-<task_id>`` already
            exists.
        RuntimeError: ``git worktree add`` itself failed.
    """
    repo_path = Path(repo).resolve()
    branch = _task_branch_name(task_id)

    if _branch_exists(repo_path, branch):
        raise WorktreeBranchConflictError(
            f"refusing to create worktree for task {task_id}: branch "
            f"{branch!r} already exists. This usually means a prior "
            f"dispatch did not clean up. Run "
            f"`git worktree remove --force <path>` and "
            f"`git branch -D {branch}` before retrying."
        )

    if base_ref is None:
        base_ref = get_default_branch(repo_path)

    worktree_root = repo_path.parent / ".equipa-worktrees"
    worktree_root.mkdir(exist_ok=True)
    wt_path = worktree_root / f"task-{task_id}"

    if wt_path.exists():
        # Stale directory from a crashed prior dispatch. Try the clean
        # `git worktree remove` first, fall back to rmtree if git has
        # already lost track of it.
        git_run(
            ["worktree", "remove", "--force", str(wt_path)],
            repo_path, timeout=30,
        )
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        git_run(["worktree", "prune"], repo_path, timeout=10)

    result = git_run(
        ["worktree", "add", "-b", branch, str(wt_path), base_ref],
        repo_path,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed for task {task_id} "
            f"(branch={branch}, base={base_ref}): {result.stderr.strip()}"
        )

    logger.info(
        "created worktree for task %s at %s on branch %s (base=%s)",
        task_id, wt_path, branch, base_ref,
    )
    return TaskWorktree(path=wt_path, branch=branch, repo=repo_path)


def remove_task_worktree(repo: str | Path, wt: TaskWorktree) -> None:
    """Tear down a worktree created by :func:`create_task_worktree`.

    After this call:

    * The worktree directory is gone (forcibly removed; uncommitted
      changes inside the worktree are dropped — agents are expected to
      commit before tear-down).
    * Stale worktree metadata is pruned from ``.git/worktrees``.
    * The main checkout is switched back to the default branch
      (``main``/``master``) so the next dispatch starts from a known
      state. Task #2488 observed the main checkout being left on the
      prior task's branch, which is precisely how branch reuse
      happened.

    The branch itself is NOT deleted — callers (the orchestrator merge
    step) may still need to inspect or merge it. Use
    ``git branch -D forge-task-<id>`` after a successful merge if you
    want to reclaim the name.
    """
    repo_path = Path(repo).resolve()
    git_run(
        ["worktree", "remove", "--force", str(wt.path)],
        repo_path, timeout=60,
    )
    if wt.path.exists():
        shutil.rmtree(wt.path, ignore_errors=True)
    git_run(["worktree", "prune"], repo_path, timeout=10)

    default_branch = get_default_branch(repo_path)
    checkout = git_run(
        ["checkout", "-q", default_branch], repo_path, timeout=30,
    )
    if checkout.returncode != 0:
        logger.warning(
            "failed to checkout %s after worktree teardown: %s",
            default_branch, checkout.stderr.strip(),
        )


def merge_task_branch(
    repo: str | Path,
    task_id: int,
    target_ref: str | None = None,
) -> subprocess.CompletedProcess:
    """Merge ``forge-task-<task_id>`` into ``target_ref`` (default:
    repo's default branch).

    Defensive check (task #2488 acceptance criterion 5): if the branch
    does not exist, raise :class:`MissingTaskBranchError` with an
    explicit message instead of returning a "no commits ahead" no-op.
    Silent skip is exactly how the original bug masked itself.

    Returns the :class:`subprocess.CompletedProcess` of the underlying
    ``git merge`` invocation so callers can inspect ``returncode`` /
    ``stdout`` / ``stderr`` for merge conflicts and other recoverable
    failures.
    """
    repo_path = Path(repo).resolve()
    branch = _task_branch_name(task_id)

    if not _branch_exists(repo_path, branch):
        raise MissingTaskBranchError(
            f"cannot merge task {task_id}: branch {branch!r} does not "
            f"exist in {repo_path}. This usually means the worktree "
            f"was never created (per-task branch reuse bug, task "
            f"#2488). Inspect dispatch logs — do NOT silently skip the "
            f"merge: the agent's commits may have landed on an "
            f"unexpected branch."
        )

    if target_ref is None:
        # SR-2997 S1: never merge into whatever origin/HEAD names (agent-
        # writable); raises UntrustedDefaultBranchError (a RuntimeError).
        target_ref = get_trusted_default_branch(repo_path)

    checkout = git_run(["checkout", "-q", target_ref], repo_path, timeout=30)
    if checkout.returncode != 0:
        raise RuntimeError(
            f"failed to checkout {target_ref!r} before merging "
            f"{branch!r}: {checkout.stderr.strip()}"
        )

    merge_args = [
        *_git_identity_args(),
        "merge", "--no-ff",
        "-m", f"merge {branch} (task {task_id})",
        branch,
    ]
    return git_run(merge_args, repo_path, timeout=180)


class DefaultBranchDetectionError(RuntimeError):
    """Raised by ``get_default_branch(strict=True)`` when every detection
    strategy fails (no ``origin/HEAD``, no local ``main``/``master``, no
    valid ``HEAD``).

    Callers that need fail-closed behaviour (security_gate, dispatch) can
    opt in via ``strict=True``. Default behaviour remains backward-
    compatible: ``strict=False`` falls back to the legacy ``"master"``
    string while emitting a WARNING log so the silent fallback is at
    least observable.
    """


def _get_git_identity() -> tuple[str | None, str | None]:
    """Read git_author_name and git_author_email from dispatch_config.json.

    Returns (name, email) tuple. Either or both may be None if not configured.
    """
    import json as _json
    try:
        from equipa.constants import THEFORGE_DB
        config_path = Path(THEFORGE_DB).parent / "dispatch_config.json"
        if config_path.exists():
            data = _json.loads(config_path.read_text(encoding="utf-8"))
            return data.get("git_author_name"), data.get("git_author_email")
    except Exception:
        pass
    return None, None


def _git_identity_args() -> list[str]:
    """Return git -c args for author identity, or empty list if not configured."""
    name, email = _get_git_identity()
    args = []
    if name:
        args.extend(["-c", f"user.name={name}"])
    if email:
        args.extend(["-c", f"user.email={email}"])
    return args


class GitRepositoryUnreadableError(RuntimeError):
    """A directory belongs to a git repository that git cannot read.

    R3119-02 (task #3126): such a directory is neither "git" nor "not git".
    A dispatch there is refused; it never runs ungated in the checkout.
    """


# git's answer for a directory outside every repository; read with LC_ALL=C.
_NOT_A_REPOSITORY = "not a git repository"


class GitNotRunnableError(OSError):
    """The git executable could not be started (missing from PATH, ...)."""


def _contained_root(path: str | Path, toplevel: str) -> tuple[Path | None, str]:
    """``toplevel`` as the root of ``path``, or ``(None, why not)``.

    IND-02 (task #3132): ``rev-parse --show-toplevel`` answers with an
    agent-writable ``core.worktree``, which can name ANOTHER repository's
    checkout. A root that does not contain ``path`` is never ``path``'s work
    tree, so every gate, merge and cleanup refuses it.
    """
    root = Path(toplevel)
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return None, (
            f"git names {root} as the work-tree root of {path}, which does "
            f"not contain it (core.worktree or a similar redirect)"
        )
    return root, ""


# ``rev-parse`` prints one line per option, in the order given.
_WORK_TREE_PROBE = ("rev-parse", "--absolute-git-dir", "--show-toplevel")
_GIT_DIR_PROBE = ("rev-parse", "--absolute-git-dir")


def _parse_work_tree_probe(stdout: str | None) -> tuple[str, str] | None:
    """``(git dir, work-tree root)`` from :data:`_WORK_TREE_PROBE` output."""
    lines = (stdout or "").splitlines()
    if len(lines) != 2 or not all(line.strip() for line in lines):
        return None
    return lines[0].strip(), lines[1].strip()


def _root_needs_repository_check(path: str | Path, root: Path) -> bool:
    """True when ``root`` is an ancestor of ``path``, not ``path`` itself.

    git run at ``path`` already answered for ``path``'s repository; only a
    different directory can discover a different one.
    """
    return root.resolve() != Path(path).resolve()


def _same_repository_problem(
    path: str | Path, root: Path, git_dir: str, root_git_dir: str | None,
) -> str | None:
    """Why git run AT ``root`` would not work on ``path``'s repository.

    IND-02 (task #3132): an agent-written ``core.worktree`` naming an
    ANCESTOR directory passes the containment check. When that ancestor is
    another repository's checkout, every orchestrator git call re-rooted
    there (gate diff, merge, reset) discovers that repository instead.
    """
    if root_git_dir is None:
        return (
            f"git names {root} as the work-tree root of {path}, but git run "
            f"there finds no repository (core.worktree or a similar redirect)"
        )
    if Path(root_git_dir).resolve() != Path(git_dir).resolve():
        return (
            f"git names {root} as the work-tree root of {path}, but git run "
            f"there uses the repository at {root_git_dir}, not {git_dir} "
            f"(core.worktree or a similar redirect)"
        )
    return None


def _git_dir_at(directory: Path) -> str | None:
    """Absolute git dir that git discovers from ``directory``, None if none."""
    try:
        result = git_run(list(_GIT_DIR_PROBE), directory, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("[git] no git dir at %s: %s", directory, exc)
        return None
    printed = (result.stdout or "").strip()
    return printed if result.returncode == 0 and printed else None


async def _git_dir_at_async(directory: Path) -> str | None:
    """:func:`_git_dir_at` without blocking the event loop."""
    try:
        result = await git_run_async(list(_GIT_DIR_PROBE), directory, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("[git] no git dir at %s: %s", directory, exc)
        return None
    printed = (result.stdout or "").strip()
    return printed if result.returncode == 0 and printed else None


def _probe_work_tree(path: str | Path) -> tuple[Path | None, str]:
    """``(work-tree root, "")``, or ``(None, why git named no root)``.

    The root must contain ``path`` and, when it is an ancestor, git run
    there must find the same repository (IND-02, task #3132).

    Raises :class:`GitNotRunnableError` when the git executable itself
    could not be started; every other failure is reported, not raised.
    """
    try:
        result = git_run(
            list(_WORK_TREE_PROBE), path, timeout=10, env={"LC_ALL": "C"},
        )
    except subprocess.SubprocessError as exc:
        return None, f"git could not be run: {exc}"
    except OSError as exc:
        raise GitNotRunnableError(f"git could not be run: {exc}") from exc
    probed = _parse_work_tree_probe(result.stdout) if result.returncode == 0 else None
    if probed is None:
        detail = (result.stderr or "").strip()[:300]
        return None, detail or f"git rev-parse exited {result.returncode}"
    git_dir, toplevel = probed
    root, problem = _contained_root(path, toplevel)
    if root is None or not _root_needs_repository_check(path, root):
        return root, problem
    problem = _same_repository_problem(path, root, git_dir, _git_dir_at(root))
    return (None, problem) if problem else (root, "")


def _nearest_git_entry(path: Path) -> Path | None:
    """The first ``.git`` entry at ``path`` or above it, None if there is none.

    Both the path as given and its symlink-resolved form are walked: git
    itself discovers the repository from the resolved working directory.

    S3168-04 (task 3172): a symlink loop at or above ``path`` made
    ``Path.resolve`` raise (RuntimeError on Python 3.10 to 3.12), and the
    error escaped every repository-appearance check. Such a path cannot be
    walked, so nothing shows that no repository is reachable through it: the
    looping path itself is returned (fail closed), and every caller treats
    the project as holding a repository (N1 blocks the task). The loop is
    found with ``os.stat`` (ELOOP), which every Python version reports the
    same way; ``Path.resolve`` stopped raising for it in Python 3.13.
    """
    absolute = path.absolute()
    try:
        os.stat(absolute)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return absolute
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return absolute
    for start in dict.fromkeys((absolute, resolved)):
        for directory in (start, *start.parents):
            entry = directory / ".git"
            if os.path.lexists(entry):
                return entry
    return None


def git_toplevel(path: str | Path) -> Path | None:
    """Root of the git work tree that contains ``path``, or None.

    None when ``path`` is not a directory inside a git work tree (a plain
    directory, a bare repository, a ``.git`` directory itself), when git
    names a root that does not contain ``path`` (IND-02, task #3132) or git
    cannot be run there. Use :func:`_is_git_repo` to tell those apart.
    """
    if not Path(path).is_dir():
        return None
    try:
        toplevel, problem = _probe_work_tree(path)
    except GitNotRunnableError as exc:
        toplevel, problem = None, str(exc)
    if toplevel is None:
        logger.debug("[git] no work tree for %s: %s", path, problem)
    return toplevel


async def git_toplevel_async(path: str | Path) -> Path | None:
    """:func:`git_toplevel` without blocking the event loop."""
    if not Path(path).is_dir():
        return None
    try:
        result = await git_run_async(list(_WORK_TREE_PROBE), path, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("[git] could not locate the work tree of %s: %s", path, exc)
        return None
    probed = _parse_work_tree_probe(result.stdout) if result.returncode == 0 else None
    if probed is None:
        return None
    git_dir, toplevel = probed
    root, problem = _contained_root(path, toplevel)
    if root is not None and _root_needs_repository_check(path, root):
        problem = _same_repository_problem(
            path, root, git_dir, await _git_dir_at_async(root),
        ) or ""
        if problem:
            root = None
    if root is None:
        logger.warning("[git] refusing the work-tree root of %s: %s", path, problem)
    return root


def _is_git_repo(path: str | Path) -> bool:
    """True when ``path`` lies inside a git work tree.

    Task #3119: a project nested in another repository has no ``.git`` of
    its own, yet every commit made there lands on the enclosing repository.
    Checking for a ``.git`` entry treated it as "not git", so it got no
    worktree, no guard and no gate. ``git rev-parse --show-toplevel``
    answers for the enclosing repository as well.

    R3119-02 (task #3126): fails closed. False only for a missing
    directory, or when git positively answers "not a git repository" and
    there is no ``.git`` at ``path`` or above it. Any other failure (a
    corrupted config, a broken ``.git`` file, a timeout, a work-tree root
    that does not contain ``path``) raises
    :class:`GitRepositoryUnreadableError`: an agent-broken repository must
    not turn the next dispatch into an ungated one.

    IND-03 (task #3132): with no ``git`` binary at all, a directory with no
    ``.git`` at or above it is simply not git (it runs as before); one that
    has a ``.git`` is refused.
    """
    directory = Path(path)
    if not directory.is_dir():
        return False
    try:
        toplevel, problem = _probe_work_tree(directory)
    except GitNotRunnableError as exc:
        git_entry = _nearest_git_entry(directory)
        if git_entry is None:
            return False
        raise GitRepositoryUnreadableError(
            f"{exc}, but {git_entry} exists. Refusing to treat {directory} "
            "as a non-git project; install git before dispatching."
        ) from exc
    if toplevel is not None:
        return True
    git_entry = _nearest_git_entry(directory)
    if git_entry is None and _NOT_A_REPOSITORY in problem.lower():
        return False
    raise GitRepositoryUnreadableError(
        f"{directory} cannot be read by git ({problem})"
        + (f"; {git_entry} exists" if git_entry is not None else "")
        + ". Refusing to treat it as a non-git project; repair the "
        "repository before dispatching."
    )


def detect_project_language(project_dir: str | Path) -> dict:
    """Detect languages and frameworks in a project by scanning for marker files.

    Returns a dict with:
        - languages: list of detected language strings
        - frameworks: list of detected framework strings
        - primary: the most likely primary language (string)

    The primary language is chosen by a priority order that favours explicit
    project manifests over file-extension scanning.
    """
    p = Path(project_dir)
    languages: list[str] = []
    frameworks: list[str] = []

    # --- Language detection via marker files ---

    # Python: pyproject.toml, setup.py, requirements.txt, Pipfile
    python_markers = ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"]
    if any((p / m).exists() for m in python_markers) or list(p.glob("*.py")):
        languages.append("python")
        if (p / "pyproject.toml").exists():
            try:
                content = (p / "pyproject.toml").read_text(
                    encoding="utf-8", errors="replace",
                )
                if "django" in content.lower():
                    frameworks.append("django")
                if "fastapi" in content.lower():
                    frameworks.append("fastapi")
                if "flask" in content.lower():
                    frameworks.append("flask")
            except OSError:
                pass

    # TypeScript: tsconfig.json
    has_tsconfig = (p / "tsconfig.json").exists()
    if has_tsconfig:
        languages.append("typescript")

    # JavaScript: package.json without tsconfig (pure JS)
    has_package_json = (p / "package.json").exists()
    if has_package_json and not has_tsconfig:
        if (p / "jsconfig.json").exists() or not has_tsconfig:
            languages.append("javascript")

    # Detect Node/JS frameworks from package.json
    if has_package_json:
        try:
            content = (p / "package.json").read_text(
                encoding="utf-8", errors="replace",
            )
            if '"next"' in content:
                frameworks.append("nextjs")
            if '"react"' in content:
                frameworks.append("react")
            if '"express"' in content:
                frameworks.append("express")
            if '"vue"' in content:
                frameworks.append("vue")
            if '"angular"' in content or '"@angular/core"' in content:
                frameworks.append("angular")
        except OSError:
            pass

    # Go: go.mod
    if (p / "go.mod").exists():
        languages.append("go")

    # Rust: Cargo.toml
    if (p / "Cargo.toml").exists():
        languages.append("rust")

    # C#/.NET: *.csproj, *.sln
    if (
        list(p.glob("*.csproj"))
        or list(p.glob("*.sln"))
        or list(p.glob("**/*.csproj"))
    ):
        languages.append("csharp")
        frameworks.append("dotnet")

    # Java: pom.xml, build.gradle
    if (
        (p / "pom.xml").exists()
        or (p / "build.gradle").exists()
        or (p / "build.gradle.kts").exists()
    ):
        languages.append("java")
        if (p / "pom.xml").exists():
            frameworks.append("maven")
        if (p / "build.gradle").exists() or (p / "build.gradle.kts").exists():
            frameworks.append("gradle")

    # Determine primary language (first detected wins based on priority above)
    primary = languages[0] if languages else "default"

    return {
        "languages": languages,
        "frameworks": frameworks,
        "primary": primary,
    }


# --- Hardened git invocation (gate-02, gate-03) -------------------------------
#
# Agent worktrees share the repository's common git dir, so ``.git/config``,
# hook directories and ``refs/replace/*`` are all agent-writable. Every git
# subprocess started by this module therefore:
#
# * runs with GIT_NO_REPLACE_OBJECTS=1, so a replace ref cannot make a code
#   commit read as a README-only change to the gate diff, or make a merge use
#   a tree other than the commit's real one (gate-02);
# * pins, with ``-c``, every config key whose value is a program git may run.
#   Command-line config outranks system, global, repo and worktree config, so
#   agent-written repo config cannot run code inside the orchestrator's
#   checkout/merge/rebase/stash/diff (gate-03);
# * passes ``--no-pager``, and ``--no-ext-diff --no-textconv`` to the
#   diff-family subcommands.
#
# Programs named by the orchestrator's OWN environment and kept by the IR-04
# allowlist (GIT_SSH, GIT_SSH_COMMAND, GIT_ASKPASS, SSH_ASKPASS) still outrank
# these pins. That environment is operator-controlled, not agent-writable.
#
# Residual, not neutralisable by a fixed argument list, because the driver
# name is chosen by agent-writable .gitattributes / info/attributes:
# filter.<driver>.clean/smudge/process and merge.<driver>.driver (both run
# during checkout/merge/rebase/stash), plus diff.<driver>.textconv for
# subcommands outside _DIFF_DRIVER_SUBCOMMANDS (blame, grep, cat-file,
# none of which EQUIPA calls). Reached by network operations only:
# credential.helper (an empty -c value would also discard the operator's
# global helper), core.gitProxy (the first matching value wins, so a -c value
# cannot override repo config) and remote.<name>.uploadpack/receivepack/vcs.
#
# FF-3155 (task #3158): "network operations only" does not hold in a partial
# clone. With extensions.partialClone and a promisor remote in repo config
# (an agent's plain `git config` in its worktree writes the shared config),
# any local command that looks up a missing object (`rev-parse --verify`)
# lazy-fetches it and runs that remote's uploadpack program, or its ext::
# command. GIT_NO_LAZY_FETCH (below) stops the lazy fetch on every hardened
# call; git on an agent's worktree also runs with no transport allowed at all
# (_NO_TRANSPORT_ENV), which does not depend on the git version.

_GIT_PROGRAM_CONFIG_PINS: tuple[tuple[str, str], ...] = (
    # A non-directory, so no hook can be found under it. role_resolver's
    # _GIT_SAFE_CONFIG sets an empty value; this later -c supersedes it.
    ("core.hooksPath", "/dev/null"),
    ("core.pager", "cat"),
    # ":" is git's own "no editor" value: nothing is launched.
    ("core.editor", ":"),
    ("sequence.editor", ":"),
    ("protocol.ext.allow", "never"),
    # Signing stays off, so gpg.ssh.defaultKeyCommand is never reached, and
    # signature verification (log.showSignature, merge.verifySignatures) can
    # only run the stock programs.
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("gpg.program", "gpg"),
    ("gpg.openpgp.program", "gpg"),
    ("gpg.x509.program", "gpgsm"),
    ("gpg.ssh.program", "ssh-keygen"),
    # Task #3116 (MI-05): checkout / merge / stash never recurse into a
    # submodule, where that submodule's own config could name a driver.
    ("submodule.recurse", "false"),
)

# R3119-01 (task #3126): run from a sub-directory (a project nested in
# another repository), repo config ``diff.relative=true`` makes git diff /
# log / show drop every path outside that directory, so code elsewhere in
# the repository read as a doc-only change. Paths are always reported from
# the work-tree root, whatever the repo config says.
#
# IND-01 (task #3132): ``diff.ignoreSubmodules`` likewise drops a submodule
# pointer bump from porcelain ``git diff``. The pin overrides repo config;
# ``submodule.<name>.ignore`` (config or ``.gitmodules``) is only overridden
# by the ``--ignore-submodules=none`` flag the gate diff passes.
_GIT_PATH_SCOPE_PINS: tuple[tuple[str, str], ...] = (
    ("diff.relative", "false"),
    ("status.relativePaths", "false"),
    ("diff.ignoreSubmodules", "none"),
)

GIT_HARDENING_ARGS: tuple[str, ...] = (
    "--no-pager",
    *_GIT_SAFE_CONFIG,
    *(
        flag
        for key, value in (*_GIT_PROGRAM_CONFIG_PINS, *_GIT_PATH_SCOPE_PINS)
        for flag in ("-c", f"{key}={value}")
    ),
)

# GIT_CONFIG_NOSYSTEM / GIT_ATTR_NOSYSTEM (task #3116, MI-04): the system
# config and attributes files are skipped; the global config is replaced by
# the orchestrator's pre-dispatch snapshot (see pin_global_git_config).
#
# GIT_NO_LAZY_FETCH (FF-3155, task #3158): a missing object is an error,
# never fetched from a promisor remote (see the residual note above).
# EQUIPA's repositories are full clones, so nothing it reads is ever missing.
GIT_HARDENING_ENV: Mapping[str, str] = MappingProxyType({
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
})

# FF-3155 (task #3158): an allow-list naming no protocol, so git refuses
# every transport (file, ssh, ext::, remote helpers) whatever the repo config
# allows. For git that only reads and writes local objects and refs: older
# git ignores GIT_NO_LAZY_FETCH, but has honoured this since 2.6.
_NO_TRANSPORT_ENV: Mapping[str, str] = MappingProxyType({"GIT_ALLOW_PROTOCOL": ""})

# diff.external cannot be cleared with -c: git runs an empty value as a
# command and every patch diff dies. Diff drivers are switched off per
# subcommand instead. --no-ext-diff covers diff.external and
# diff.<driver>.command; --no-textconv covers diff.<driver>.textconv, which
# could also show the security reviewer a converted view that hides code.
# --no-relative backs up the diff.relative pin (R3119-01, task #3126).
_DIFF_DRIVER_SUBCOMMANDS = frozenset({"diff", "log", "show"})
_DIFF_DRIVER_OFF_ARGS: tuple[str, ...] = (
    "--no-ext-diff", "--no-textconv", "--no-relative",
)

# git global options whose value is the NEXT token (``-c k=v``, ``-C dir``).
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--attr-source", "--super-prefix",
})


def _operator_program_pins(env: Mapping[str, str]) -> tuple[str, ...]:
    """``-c`` pins for programs whose operator fallback lives in ``env``.

    git ranks ``core.sshCommand`` above ``GIT_SSH`` and ``core.askPass`` above
    ``SSH_ASKPASS``. Pinning either to a constant would silently discard the
    operator's choice, so each pin carries the operator's value instead.
    """
    operator_ssh = env.get("GIT_SSH")
    ssh_command = shlex.quote(operator_ssh) if operator_ssh else "ssh"
    ask_pass = env.get("SSH_ASKPASS", "")
    return (
        "-c", f"core.sshCommand={ssh_command}",
        "-c", f"core.askPass={ask_pass}",
    )


def _hardened_git_env(
    extra_env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    repository: PinnedGitRepository | None = None,
) -> dict[str, str]:
    """Allowlisted env, push credentials for a push ``args``, the caller's
    ``extra_env``, then the hardening and the pinned ``repository``.

    The hardening is applied last so no caller can switch it back off.
    """
    env = _get_repo_env()
    env.update(_credential_env_for(args))
    if extra_env:
        env.update(extra_env)
    env.update(GIT_HARDENING_ENV)
    pin = _global_config_pin
    if pin is not None:
        env["GIT_CONFIG_GLOBAL"] = str(pin.path)
    if repository is not None:
        env.update(repository.env())
    return env


def _git_subcommand_index(args: Sequence[str]) -> int | None:
    """Index of the subcommand in ``args``, past any leading global options."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            return index
    return None


def _hardened_git_argv(
    args: Sequence[str],
    env: Mapping[str, str],
    pin: PinnedGitRepository | None = None,
) -> list[str]:
    """Full argv for ``git <args>`` with the hardening flags in front.

    Diff-family subcommands also get ``_DIFF_DRIVER_OFF_ARGS`` directly after
    the subcommand name, ahead of any ``--`` the caller passes. A ``pin``
    adds its ``--git-dir`` / ``--work-tree`` options.
    """
    command = list(args)
    subcommand = _git_subcommand_index(command)
    if subcommand is not None and command[subcommand] in _DIFF_DRIVER_SUBCOMMANDS:
        command[subcommand + 1:subcommand + 1] = _DIFF_DRIVER_OFF_ARGS
    pin_args = pin.argv() if pin is not None else []
    return ["git", *GIT_HARDENING_ARGS, *_operator_program_pins(env), *pin_args, *command]


# --- Pinned repositories (R3146-01, task #3151) --------------------------------
#
# git finds a repository through the ``.git`` entry it meets in its working
# directory, and agents share the orchestrator's UID: between the guard's
# identity check and ``git merge``, an agent can rename ``.git`` away and put a
# symlink or ``gitdir:`` file to a clone of its own in its place. The merge then
# runs in the clone, including the clone's filter drivers, inside the
# orchestrator. A realpath does not help, since the same path now leads to the
# clone. The merge path therefore opens the pinned git directories once, checks
# their inodes against the snapshot and hands git the open descriptors
# (``--git-dir=/proc/self/fd/N`` plus ``GIT_COMMON_DIR``). git keeps that path
# as its git dir, so every later lookup goes through the descriptor, wherever
# the directory has been renamed to and whatever now sits at ``.git``.

_FD_DIRECTORY = "/proc/self/fd"


class PinnedRepositoryError(Exception):
    """A pinned git directory is not the one recorded at the snapshot."""


@dataclass(frozen=True)
class PinnedGitRepository:
    """The repository every git call run in ``work_tree`` must use.

    ``git_dir`` and ``common_dir`` are what git is given: descriptor paths
    under ``/proc/self/fd`` for the open ``fds``, or plain realpaths on a
    system without that directory (not rename-proof there).

    ``path`` is the directory as the merge path names it, the key a git
    call's ``cwd`` must equal (absolute, symlinks NOT resolved; defaults to
    ``work_tree``). ``work_tree_id`` is that directory's (device, inode)
    when it was pinned; :func:`git_repositories_pinned` records it when the
    pin does not.

    ``work_tree_fd`` (R3155-02, task #3158) is the work tree opened when it
    was pinned. git then starts inside that directory
    (``/proc/self/fd/N``) and is told the directory it stands in is the
    work tree, so the inode checked is the inode git uses, not whatever a
    rename put at ``path`` between the check and git's start. ``fds``
    includes it.
    """

    work_tree: str
    git_dir: str
    common_dir: str
    fds: tuple[int, ...] = ()
    path: str = ""
    work_tree_id: tuple[int, int] | None = None
    work_tree_fd: int | None = None

    @property
    def key(self) -> str:
        return _pin_key(self.path or self.work_tree)

    def argv(self) -> list[str]:
        work_tree = "." if self.work_tree_fd is not None else self.work_tree
        return [f"--git-dir={self.git_dir}", f"--work-tree={work_tree}"]

    def run_cwd(self, cwd: str | Path) -> str:
        """Where git starts for a call whose ``cwd`` named this work tree."""
        if self.work_tree_fd is None:
            return str(cwd)
        return f"{_FD_DIRECTORY}/{self.work_tree_fd}"

    def env(self) -> dict[str, str]:
        # Overrides the git dir's ``commondir`` file, which an agent can
        # rewrite to point at another repository.
        return {"GIT_COMMON_DIR": self.common_dir}


_pinned_repositories: contextvars.ContextVar[Mapping[str, PinnedGitRepository]] = (
    contextvars.ContextVar("equipa_pinned_git_repositories", default=MappingProxyType({}))
)


def fd_pinning_available() -> bool:
    """True where git can be handed an open directory as ``/proc/self/fd/N``."""
    return os.path.isdir(_FD_DIRECTORY)


def _directory_id(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def check_pinned_directory(path: str, expected_id: tuple[int, int]) -> None:
    """Refuse ``path`` unless it is the directory with ``expected_id``.

    The check for systems without :func:`fd_pinning_available`, where git is
    given the realpath itself. Raises :class:`PinnedRepositoryError`.
    """
    try:
        info = os.stat(path)
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot read the pinned git directory {path}: {exc.strerror}"
        ) from exc
    if (info.st_dev, info.st_ino) != tuple(expected_id):
        raise PinnedRepositoryError(
            f"{path} is no longer the git directory pinned at the snapshot "
            f"(device/inode {tuple(expected_id)})"
        )


def open_pinned_directory(
    path: str,
    expected_id: tuple[int, int] | None,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open the directory ``path`` (relative to ``dir_fd`` when given).

    With ``expected_id`` the opened directory's (device, inode) must match,
    so a directory renamed or swapped in after the snapshot is refused
    before it is used. With ``dir_fd`` the last component must not be a
    symlink. Raises :class:`PinnedRepositoryError`; the caller owns the fd.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if dir_fd is not None:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot open the pinned git directory {path}: {exc.strerror}"
        ) from exc
    if expected_id is not None and _directory_id(fd) != tuple(expected_id):
        os.close(fd)
        raise PinnedRepositoryError(
            f"{path} is no longer the git directory pinned at the snapshot "
            f"(device/inode {tuple(expected_id)})"
        )
    return fd


def open_work_tree(path: str) -> int:
    """Open the work-tree directory ``path`` the way git's ``chdir`` into it
    would (symlinks followed). Raises :class:`PinnedRepositoryError`; the
    caller owns the fd.
    """
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot open the work tree {path}: {exc.strerror}"
        ) from exc


def descriptor_path(fd: int) -> str:
    """The path the open directory ``fd`` has now (its ``/proc/self/fd``
    link). Raises :class:`PinnedRepositoryError`."""
    try:
        return os.readlink(f"{_FD_DIRECTORY}/{fd}")
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot read the path of descriptor {fd}: {exc.strerror}"
        ) from exc


def pinned_repository(
    work_tree: str, git_dir_fd: int, common_dir_fd: int,
    *, git_dir: str, common_dir: str, work_tree_fd: int | None = None,
) -> PinnedGitRepository:
    """A :class:`PinnedGitRepository` for open directory descriptors.

    ``work_tree`` is the directory as the merge path names it: the pin is
    keyed by that path. With ``work_tree_fd`` (from :func:`open_work_tree`)
    the pin records that directory's (device, inode) and git starts in it;
    without, the directory at ``work_tree`` now. ``git_dir`` /
    ``common_dir`` are the realpaths, used only where ``/proc/self/fd``
    does not exist.
    """
    if not fd_pinning_available():
        return pinned_repository_by_path(work_tree, git_dir=git_dir, common_dir=common_dir)
    path = _pin_key(work_tree)
    fds = {git_dir_fd, common_dir_fd}
    if work_tree_fd is None:
        real_work_tree, work_tree_id = os.path.realpath(work_tree), _work_tree_id(path)
    else:
        real_work_tree = descriptor_path(work_tree_fd)
        work_tree_id = _directory_id(work_tree_fd)
        fds.add(work_tree_fd)
    return PinnedGitRepository(
        real_work_tree,
        f"{_FD_DIRECTORY}/{git_dir_fd}",
        f"{_FD_DIRECTORY}/{common_dir_fd}",
        tuple(sorted(fds)),
        path=path,
        work_tree_id=work_tree_id,
        work_tree_fd=work_tree_fd,
    )


def pinned_repository_by_path(
    work_tree: str, *, git_dir: str, common_dir: str,
) -> PinnedGitRepository:
    """A :class:`PinnedGitRepository` that names the realpaths ``git_dir`` /
    ``common_dir`` (systems without ``/proc/self/fd``), keyed by
    ``work_tree`` as the merge path names it, with its (device, inode) now.
    """
    path = _pin_key(work_tree)
    return PinnedGitRepository(
        os.path.realpath(work_tree), git_dir, common_dir,
        path=path, work_tree_id=_work_tree_id(path),
    )


# R3151-01 / F2 (task #3155): the pin used to be looked up by the realpath of
# a git call's ``cwd``, and a ``cwd`` that matched no pin ran unpinned. An
# agent that renamed the main checkout (or the task worktree) away and put a
# symlink to its own clone at that path therefore sent every later merge-path
# call through a fresh discovery into the clone, whose config and filter
# drivers then ran inside the orchestrator. Now the pin is found by the path
# exactly as the caller passes it, the directory there must still be the one
# pinned (device and inode, so a real directory moved into place is refused
# too), and inside a pinned block a ``cwd`` that names no pin is refused
# instead of discovered. Each refusal is a PinnedRepositoryError, which the
# merge path turns into a tripped guard.


def _pin_key(path: str | os.PathLike) -> str:
    """The lookup key of a work tree: absolute, symlinks NOT resolved."""
    return os.path.abspath(os.fspath(path))


def _work_tree_id(path: str) -> tuple[int, int]:
    """(device, inode) of the directory at ``path``, following symlinks the
    way git's ``chdir`` into it would. Raises :class:`PinnedRepositoryError`."""
    try:
        info = os.stat(path)
    except OSError as exc:
        raise PinnedRepositoryError(
            f"cannot read the pinned work tree {path}: {exc.strerror}"
        ) from exc
    return info.st_dev, info.st_ino


@contextlib.contextmanager
def git_repositories_pinned(*repositories: PinnedGitRepository) -> Iterator[None]:
    """Inside the block, every :func:`git_run` / :func:`git_run_async` runs
    on a pinned repository: its ``cwd`` must be exactly the path of a pinned
    work tree (as the caller names it, not its realpath) and still be that
    directory, or :class:`PinnedRepositoryError` is raised; git never
    discovers a repository here. Context-local, so a concurrent merge in
    another task is unaffected. The caller keeps the descriptors open for
    the whole block and closes them afterwards.
    """
    pins = dict(_pinned_repositories.get())
    for repository in repositories:
        if repository.work_tree_id is None:
            repository = dataclass_replace(
                repository, work_tree_id=_work_tree_id(repository.key),
            )
        pins[repository.key] = repository
    token = _pinned_repositories.set(MappingProxyType(pins))
    try:
        yield
    finally:
        _pinned_repositories.reset(token)


def _pinned_repository_for(cwd: str | Path) -> PinnedGitRepository | None:
    """The pin a git call in ``cwd`` must use; None outside a pinned block.

    Raises :class:`PinnedRepositoryError` inside one when ``cwd`` names no
    pinned work tree, or names one whose directory has been swapped.
    """
    pins = _pinned_repositories.get()
    if not pins:
        return None
    key = _pin_key(cwd)
    pin = pins.get(key)
    if pin is None:
        raise PinnedRepositoryError(
            f"git would run in {key}, which is not a work tree pinned for "
            f"this merge; a pinned merge never discovers a repository"
        )
    if _work_tree_id(key) != pin.work_tree_id:
        raise PinnedRepositoryError(
            f"{key} is no longer the work tree pinned for this merge "
            f"(device/inode {pin.work_tree_id}); it was swapped after the pin"
        )
    return pin


def _check_work_tree_after_call(pin: PinnedGitRepository | None) -> None:
    """R3155-02 (task #3158): the pinned work tree must still be at its path
    once git has run. A swap made while git ran (and still in place) is
    raised, so the merge path trips the guard instead of carrying on.
    """
    if pin is None:
        return
    if _work_tree_id(pin.key) != pin.work_tree_id:
        raise PinnedRepositoryError(
            f"{pin.key} is no longer the work tree pinned for this merge "
            f"(device/inode {pin.work_tree_id}); it was swapped while git ran"
        )


# IR-04 (task #3132): agents share the orchestrator's UID, so any agent shell
# can read /proc/<pid>/environ of every git / gh child the orchestrator
# starts. Those children therefore get an allowlisted environment, never a
# copy of the orchestrator's (which holds DATABASE_URL, API keys, tokens).
# Kept: what git needs to run, find the operator's own config and identity,
# and reach a remote over ssh. Dropped with everything else: GIT_DIR,
# GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE, GIT_CONFIG_PARAMETERS and
# friends, which would redirect or reconfigure the repository git works on.
_GIT_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR",
    "XDG_CONFIG_HOME", "SSH_AUTH_SOCK", "SSH_ASKPASS",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    # Windows: git for Windows and gh need these to start at all.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA",
    # IND3132-04 (task #3146): only the locale variables git and gh read
    # for text handling, not every LC_* name (an arbitrary LC_SECRET used to
    # reach every child).
    "LC_ALL", "LC_CTYPE",
})

# Given only to the calls that talk to GitHub (gh, git push): credentials,
# gh's own config location and the proxy settings (a proxy URL can carry a
# password too).
GITHUB_CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST", "GH_CONFIG_DIR",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
)


# git subcommands that talk to a remote and may need the credentials above.
# EQUIPA only pushes; every other git call runs without them.
_CREDENTIAL_SUBCOMMANDS = frozenset({"push"})


def github_credential_env() -> dict[str, str]:
    """The GitHub credential / proxy variables set in the orchestrator's env.

    :func:`git_run` / :func:`git_run_async` add them to ``git push`` and
    :func:`_gh_run` to every gh call; no other child receives them.
    """
    return {
        key: os.environ[key] for key in GITHUB_CREDENTIAL_ENV_KEYS
        if key in os.environ
    }


def _credential_env_for(args: Sequence[str]) -> dict[str, str]:
    """GitHub credentials when ``git <args>`` is a push, else nothing."""
    subcommand = _git_subcommand_index(args)
    if subcommand is not None and args[subcommand] in _CREDENTIAL_SUBCOMMANDS:
        return github_credential_env()
    return {}


def _get_repo_env() -> dict[str, str]:
    """Allowlisted environment for a git / gh child, git and gh on the PATH."""
    env = {
        key: value for key, value in os.environ.items()
        if key in _GIT_ENV_ALLOWLIST
    }
    extra_paths = []
    for candidate in [
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\GitHub CLI",
    ]:
        if os.path.isdir(candidate) and candidate not in env.get("PATH", ""):
            extra_paths.append(candidate)
    if extra_paths:
        env["PATH"] = ";".join(extra_paths) + ";" + env.get("PATH", "")
    return env


# --- Projects that were not git at dispatch (R3166-01, task #3168) ---

# The given and the resolved form of every project directory a dispatch in
# this context recorded as not git. A ContextVar, so each task of the
# parallel loop (its own asyncio task) sees only the records of its own run.
_NON_GIT_PROJECT_ROOTS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "equipa_non_git_project_roots", default=frozenset(),
)

# The environment variables that point git at a repository or work tree
# other than the one it would discover from its working directory. git
# reads config, hooks and info/attributes, so filters, from GIT_COMMON_DIR
# even when its own repository lies elsewhere.
_GIT_LOCATION_ENV_KEYS: tuple[str, ...] = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _path_forms(directory: str | os.PathLike[str]) -> set[str]:
    """The absolute and the symlink-resolved form of ``directory``."""
    path = os.fspath(directory)
    return {os.path.abspath(path), os.path.realpath(path)}


@contextlib.contextmanager
def dispatched_without_git(project_dir: str | os.PathLike[str]) -> Iterator[None]:
    """Record, while the block runs, that ``project_dir`` was not a git
    repository when its task was dispatched.

    R3166-01 (task #3168): a ``.git`` found there later is one an agent
    made. Git run there by discovery reads the agent's config and
    attributes, so a work-tree ``git diff`` runs the agent's clean filter
    as a child of the orchestrator, outside agent containment. While the
    record is active, no git or gh process this module starts runs at or
    under ``project_dir``, in its given or its resolved form (IR73-02, task
    #3176: enforced in the process runners, so a helper that does not ask
    :func:`git_checks_allowed` first still runs no git there). Nesting is
    safe: an inner record only adds paths.
    """
    roots = _NON_GIT_PROJECT_ROOTS.get() | _path_forms(project_dir)
    token = _NON_GIT_PROJECT_ROOTS.set(frozenset(roots))
    try:
        yield
    finally:
        _NON_GIT_PROJECT_ROOTS.reset(token)


def git_checks_allowed(directory: str | os.PathLike[str] | None) -> bool:
    """False when ``directory`` lies in a project :func:`dispatched_without_git`
    recorded; True otherwise, including when nothing is recorded.

    Both forms of ``directory`` are compared with both recorded forms, so an
    agent that swaps the project path for a symlink is still matched by the
    path as given.
    """
    roots = _NON_GIT_PROJECT_ROOTS.get()
    if not roots or not directory:
        return True
    for candidate in _path_forms(directory):
        for root in roots:
            if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
                return False
    return True


def _git_call_directories(
    argv: Sequence[str], cwd: str | Path, env: Mapping[str, str],
) -> list[str]:
    """Every directory the process ``argv`` runs in or takes a repository
    from: ``cwd``; for git each ``-C`` applied in turn and every
    ``--git-dir`` / ``--work-tree`` option; and every ``GIT_DIR`` /
    ``GIT_WORK_TREE`` / ``GIT_COMMON_DIR`` variable (gh passes them to the
    git it runs), resolved as git resolves them."""
    directory = os.path.abspath(os.fspath(cwd))
    directories = [directory]
    is_git = bool(argv) and os.path.basename(os.fspath(argv[0])) == "git"
    options = [os.fspath(token) for token in argv[1:]] if is_git else []
    index = 0
    while index < len(options):
        token = options[index]
        name, separator, value = token.partition("=")
        if separator and name in ("--git-dir", "--work-tree"):
            directories.append(os.path.join(directory, value))
            index += 1
        elif token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 < len(options):
                value = options[index + 1]
                if token == "-C":
                    directory = os.path.join(directory, value)
                    directories.append(directory)
                elif token in ("--git-dir", "--work-tree"):
                    directories.append(os.path.join(directory, value))
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    directories.extend(
        os.path.join(directory, env[key]) for key in _GIT_LOCATION_ENV_KEYS if env.get(key)
    )
    return directories


def _directory_in_non_git_project(
    argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None,
) -> str | None:
    """The first directory of the process ``argv`` that lies in a project
    :func:`dispatched_without_git` recorded, None when there is none (or
    nothing is recorded, the common case, decided without touching
    ``argv``)."""
    if not _NON_GIT_PROJECT_ROOTS.get():
        return None
    for directory in _git_call_directories(argv, cwd, env or {}):
        if not git_checks_allowed(directory):
            return directory
    return None


def _non_git_refused_result(
    argv: Sequence[str], directory: str, text: bool,
) -> subprocess.CompletedProcess:
    """The result of a process refused by IR73-02: git's own "not a git
    repository" status, which is what EQUIPA knows the project to be, so a
    caller reads it the way it reads a project that has no repository."""
    message = (
        f"equipa: refused to run {os.path.basename(os.fspath(argv[0]))} at {directory}: "
        f"it is in a project that was not a git repository at dispatch, so a "
        f"repository there is an agent's"
    )
    logger.warning("[git] %r", message)
    return subprocess.CompletedProcess(
        args=list(argv), returncode=_REFUSED_RETURNCODE,
        stdout="" if text else b"",
        stderr=f"{message}\n" if text else f"{message}\n".encode("utf-8", "replace"),
    )


def _run_with_env(
    args_list: list[str],
    cwd: str | Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
    *,
    text: bool = True,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess:
    """Low-level subprocess runner with Windows-PATH-fixed env. Internal use only.

    ``env`` replaces the default :func:`_get_repo_env` environment when given.
    ``pass_fds`` are kept open, at the same numbers, in the child.
    Starts nothing, and returns a failed result, when the process would run
    in a project :func:`dispatched_without_git` recorded (IR73-02).
    """
    refused_in = _directory_in_non_git_project(args_list, cwd, env)
    if refused_in is not None:
        return _non_git_refused_result(args_list, refused_in, text)
    return subprocess.run(
        args_list, capture_output=True, text=text,
        cwd=str(cwd), timeout=timeout,
        env=dict(env) if env is not None else _get_repo_env(),
        pass_fds=tuple(pass_fds),
    )


def git_run(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    *,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a hardened git command with standard env (Windows PATH fix) and timeout.

    The "git" prefix is added automatically — pass only the subcommand and
    its arguments, e.g. ``git_run(["status", "--porcelain"], cwd=repo)``.

    This is the single supported entry point for every git invocation in
    EQUIPA. It guarantees consistent timeout handling, PATH resolution and
    the gate-02/gate-03 hardening: replace refs are ignored and no hook or
    program named in repo config can run (see ``GIT_HARDENING_ARGS``).

    ``env`` holds extra variables layered over the allowlisted environment; the
    hardening variables are applied last and cannot be overridden.
    ``text=False`` returns stdout/stderr as bytes (e.g. ``cat-file blob``).
    ``CompletedProcess.args`` is the full argv that actually ran.
    Inside :func:`git_repositories_pinned`, a ``cwd`` that is a pinned work
    tree runs on the pinned repository (R3146-01, task #3151). Outside one,
    a subcommand that reads work-tree files (``diff``, ``status``,
    ``ls-files``, ...) whose ``cwd`` is in a task worktree runs where no
    agent-planted driver is defined (FF-3155, task #3158; see
    :func:`_git_run_in_worktree_view`).
    """
    pin = _pinned_repository_for(cwd)
    if pin is None and _reads_work_tree(args):
        location = _task_worktree_path(cwd)
        if location is not None:
            return _git_run_in_worktree_view(location, args, cwd, timeout, env, text)
    run_env = _hardened_git_env(env, args, pin)
    # Only a descriptor pin hands anything down; other calls keep the plain
    # runner signature.
    inherited = {"pass_fds": pin.fds} if pin is not None and pin.fds else {}
    result = _run_with_env(
        _hardened_git_argv(args, run_env, pin),
        pin.run_cwd(cwd) if pin is not None else cwd,
        timeout, run_env, text=text, **inherited,
    )
    _check_work_tree_after_call(pin)
    return result


async def git_run_async(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
    *,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Async equivalent of ``git_run`` — does NOT block the event loop.

    Uses ``asyncio.create_subprocess_exec`` so callers running inside an
    async function (dispatch loops, dev-test loop) can issue git commands
    without serialising the loop. Returns a ``subprocess.CompletedProcess``
    with the same ``returncode``, ``stdout``, and ``stderr`` shape as
    ``git_run`` so call sites can be migrated incrementally. Applies the
    same hardening and ``env`` merging as ``git_run``. ``input``, when
    given, is written to git's stdin (e.g. for ``git patch-id``).
    ``text=False`` returns ``stdout`` and ``stderr`` as the exact bytes git
    wrote (e.g. blob content from ``git cat-file --batch``).

    A ``TimeoutError`` is raised if the command exceeds ``timeout`` seconds;
    the child process is killed before the error propagates.
    """
    pin = _pinned_repository_for(cwd)
    if pin is None and _reads_work_tree(args):
        location = _task_worktree_path(cwd)
        if location is not None:
            return await _git_run_async_in_worktree_view(
                location, args, cwd, timeout, env, input, text,
            )
    run_env = _hardened_git_env(env, args, pin)
    result = await _run_git_process_async(
        _hardened_git_argv(args, run_env, pin),
        pin.run_cwd(cwd) if pin is not None else str(cwd),
        run_env, timeout, input=input, text=text,
        pass_fds=pin.fds if pin is not None else (),
    )
    _check_work_tree_after_call(pin)
    return result


async def _run_git_process_async(
    argv: list[str],
    cwd: str,
    env: Mapping[str, str],
    timeout: int,
    *,
    input: bytes | None = None,
    text: bool = True,
    pass_fds: Sequence[int] = (),
) -> subprocess.CompletedProcess:
    """Run the full git ``argv`` without blocking the event loop.

    The process half of :func:`git_run_async`, for callers that build their
    own hardened argv and environment. Raises ``subprocess.TimeoutExpired``
    (the child killed first) after ``timeout`` seconds. Starts nothing, and
    returns a failed result, when git would run in a project
    :func:`dispatched_without_git` recorded (IR73-02).
    """
    refused_in = _directory_in_non_git_project(argv, cwd, env)
    if refused_in is not None:
        return _non_git_refused_result(argv, refused_in, text)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=tuple(pass_fds),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input), timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise subprocess.TimeoutExpired(argv, timeout) from e
    returncode = proc.returncode if proc.returncode is not None else -1
    if not text:
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout_b, stderr=stderr_b,
        )
    return subprocess.CompletedProcess(
        args=argv,
        returncode=returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


# --- git in an agent's task worktree (R3155-01, task #3158) --------------------
#
# Cleanup runs git in a task worktree after the agent stopped: is there
# uncommitted work, and if so, stash it on the branch before the worktree is
# removed. Run there by discovery, that git used whatever repository the
# worktree's ``.git`` named, with that repository's config, and every command
# that hashes or writes file content (``status`` on a stat-dirty file,
# ``stash``) ran the filter driver the agent's attributes selected, inside the
# orchestrator. Those commands now run on a private git dir the orchestrator
# writes for the one sequence of calls:
#
# * its config is the only config git reads: no system config, no global
#   config, neither the repository's config nor its ``config.worktree``. No
#   filter, merge or diff driver, fsmonitor or hook is defined anywhere git
#   looks, so none can run;
# * attributes come from the empty tree (``GIT_ATTR_SOURCE``), the private
#   dir's absent ``info/attributes`` and no global or system file, so the
#   work tree's ``.gitattributes`` select nothing either;
# * objects are the repository's own (``GIT_OBJECT_DIRECTORY``), HEAD is the
#   commit the worktree's git dir names, and the index is a copy of the
#   worktree's index;
# * that git dir is the ``worktrees/<name>`` entry of the main repository
#   whose ``gitdir`` file names this work tree, never what its ``.git`` says,
#   and git starts inside the opened work-tree directory.
#
# A stash made there is copied to the repository's ``refs/stash``.


class AgentWorktreeGitError(RuntimeError):
    """The task worktree cannot be inspected without trusting agent files."""


# The empty tree of each object format git supports.
_EMPTY_TREES: Mapping[str, str] = MappingProxyType({
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
})
# A HEAD or gitdir file is one short line.
_GIT_LINE_FILE_LIMIT = 4096
# A worktree index can be large, but not unbounded.
_INDEX_COPY_LIMIT = 1024 * 1024 * 1024
# The only settings taken from the pinned global config: who commits the
# stash and which files the operator ignores. None of them names a program.
_PRIVATE_SETTINGS_FROM_GLOBAL = r"^(user\.name|user\.email|core\.excludesfile)$"
# The only settings taken from the repository's own config file: how git
# compares a file with its index entry (a share without exec bits sets
# core.filemode=false). Booleans and stat options; none names a program.
_PRIVATE_SETTINGS_FROM_REPOSITORY = (
    r"^core\.(filemode|symlinks|ignorecase|precomposeunicode|trustctime|checkstat)$"
)
# Used when neither the environment nor the global config names a committer.
_FALLBACK_IDENTITY = (("user.name", "EQUIPA orchestrator"), ("user.email", "equipa@localhost"))
# A branch the private HEAD may name as a plain file under refs/heads.
_PLAIN_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}")
_HEX_OBJECT_NAME_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def find_worktree_git_dir(common_dir: str, work_tree: str) -> str | None:
    """The ``worktrees/<name>`` git dir of ``common_dir`` registered for the
    work tree whose realpath is ``work_tree``; None unless exactly one is.

    Read from the main repository, never through the work tree's ``.git``:
    a git dir counts when its ``gitdir`` file names ``<work_tree>/.git``.
    """
    expected = os.path.join(work_tree, ".git")
    matches: list[str] = []
    try:
        with os.scandir(os.path.join(common_dir, "worktrees")) as entries:
            candidates = [
                entry.path for entry in entries if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return None
    for candidate in candidates:
        try:
            named = read_regular_file_bounded(
                os.path.join(candidate, "gitdir"), _GIT_LINE_FILE_LIMIT,
            ).decode("utf-8", "replace").strip()
        except OSError:
            continue
        # git writes an absolute path; a relative one is relative to the entry.
        if named and os.path.normpath(os.path.join(candidate, named)) == expected:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _open_agent_work_tree(work_tree: str | os.PathLike) -> tuple[int | None, str]:
    """``(fd, realpath)`` of the task work tree, whose last component must
    not be a symlink. The fd is None where git cannot start in it."""
    path = os.path.abspath(os.fspath(work_tree))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AgentWorktreeGitError(
            f"cannot open the task worktree {path}: {exc.strerror}"
        ) from exc
    if not fd_pinning_available():
        os.close(fd)
        return None, os.path.realpath(path)
    try:
        return fd, os.readlink(f"{_FD_DIRECTORY}/{fd}")
    except OSError as exc:
        os.close(fd)
        raise AgentWorktreeGitError(
            f"cannot read the path of the task worktree {path}: {exc.strerror}"
        ) from exc


def _copy_regular_file(source: str, destination: str, limit: int) -> bool:
    """Copy the regular file ``source`` (never a symlink or FIFO) with its
    timestamps; False when it does not exist. Raises
    :class:`AgentWorktreeGitError`.

    The timestamps matter for an index: git re-hashes an entry whose file
    is not older than the index itself ("racily clean"). A copy stamped now
    would make a same-size edit made in the second of the checkout read as
    unchanged.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AgentWorktreeGitError(f"cannot open {source}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise AgentWorktreeGitError(
                f"{source} is not a regular file of at most {limit} bytes"
            )
        with open(destination, "wb") as writer:
            copied = 0
            while chunk := reader.read(1024 * 1024):
                copied += len(chunk)
                if copied > limit:
                    raise AgentWorktreeGitError(f"{source} grew past {limit} bytes")
                writer.write(chunk)
        os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
    return True


async def _run_in_main_repository(
    common_dir: str, args: list[str], timeout: int,
) -> subprocess.CompletedProcess:
    """Hardened git on the main repository's git dir, named explicitly,
    with no transport allowed (only local objects and refs are touched)."""
    return await git_run_async(
        [f"--git-dir={common_dir}", *args], common_dir, timeout=timeout,
        env=_NO_TRANSPORT_ENV,
    )


async def remove_registered_worktree(
    common_dir: str | os.PathLike, work_tree: str | os.PathLike, *, timeout: int = 30,
) -> subprocess.CompletedProcess:
    """``git worktree remove --force <work_tree>`` on the repository whose git
    common dir is ``common_dir``, named explicitly.

    FF-3158 (task #3162): git is not started by discovery, neither in the
    work tree nor in a checkout. It finds the ``worktrees/<name>`` entry
    whose ``gitdir`` file names ``work_tree`` and reads the work tree's
    ``.git`` only as data (a rewritten one makes it refuse). ``--force``
    skips the clean check, which would run ``git status`` in the work tree.
    """
    return await _run_in_main_repository(
        os.path.realpath(os.fspath(common_dir)),
        ["worktree", "remove", "--force", os.path.abspath(os.fspath(work_tree))],
        timeout,
    )


async def _worktree_head(common_dir: str, git_dir: str) -> tuple[str, str | None]:
    """``(commit, branch)`` the worktree git dir's HEAD names (branch None
    when detached), resolved in the main repository."""
    try:
        head = read_regular_file_bounded(
            os.path.join(git_dir, "HEAD"), _GIT_LINE_FILE_LIMIT,
        ).decode("utf-8", "replace").strip()
    except OSError as exc:
        raise AgentWorktreeGitError(f"cannot read {git_dir}/HEAD: {exc}") from exc
    branch: str | None = None
    if head.startswith("ref: refs/heads/"):
        branch = head[len("ref: refs/heads/"):]
        revision = f"refs/heads/{branch}"
    elif _HEX_OBJECT_NAME_RE.fullmatch(head):
        revision = head
    else:
        raise AgentWorktreeGitError(f"{git_dir}/HEAD names {head[:200]!r}")
    resolved = await _run_in_main_repository(
        common_dir, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], 10,
    )
    commit = (resolved.stdout or "").strip()
    if resolved.returncode != 0 or not _HEX_OBJECT_NAME_RE.fullmatch(commit):
        raise AgentWorktreeGitError(f"HEAD of {git_dir} ({revision}) is not a commit")
    return commit, branch


async def read_worktree_head(
    common_dir: str | os.PathLike, work_tree: str | os.PathLike,
) -> tuple[str, str | None]:
    """``(commit, branch)`` checked out in the task worktree ``work_tree`` of
    the repository whose git common dir is ``common_dir``; branch is None
    when HEAD is detached.

    FF-3155 (task #3158): read from the repository's ``worktrees/<name>``
    entry whose ``gitdir`` file names ``work_tree``, never by running git
    through the work tree's ``.git``, so a repository the agent planted
    there (and a promisor remote whose upload-pack is the agent's program)
    is neither read nor run. Raises :class:`AgentWorktreeGitError`.
    """
    common = os.path.realpath(os.fspath(common_dir))
    real_work_tree = os.path.realpath(os.fspath(work_tree))
    git_dir = find_worktree_git_dir(common, real_work_tree)
    if git_dir is None:
        raise AgentWorktreeGitError(
            f"{real_work_tree} is not a registered worktree of {common}"
        )
    return await _worktree_head(common, git_dir)


async def _settings_from_file(config_file: Path, pattern: str) -> list[tuple[str, str | None]]:
    """The entries of the one config file ``config_file`` (includes not
    followed) whose key matches ``pattern``; none when it cannot be read."""
    listing = await git_run_async(
        ["config", "--file", str(config_file), "-z", "--get-regexp", pattern],
        config_file.parent, timeout=10,
    )
    return parse_config_list_z(listing.stdout) if listing.returncode == 0 else []


async def _operator_settings() -> list[tuple[str, str | None]]:
    """The :data:`_PRIVATE_SETTINGS_FROM_GLOBAL` entries of the pinned
    global config; none while it is not pinned or no longer verifies."""
    pin = _global_config_pin
    if pin is None or verify_global_git_config_pin() is not None:
        return []
    return await _settings_from_file(pin.path, _PRIVATE_SETTINGS_FROM_GLOBAL)


@dataclass(frozen=True)
class AgentWorktreeGit:
    """git on one task worktree that reads nothing the agent can write but
    the work tree's own files (see the section comment); made by
    :func:`agent_worktree_git`."""

    work_tree: str
    common_dir: str
    git_dir: str
    head: str
    branch: str | None
    private_dir: str
    empty_tree: str
    work_tree_fd: int | None

    def _env(self) -> dict[str, str]:
        env = _hardened_git_env(_NO_TRANSPORT_ENV)
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OBJECT_DIRECTORY": os.path.join(self.common_dir, "objects"),
            "GIT_INDEX_FILE": os.path.join(self.private_dir, "index"),
            "GIT_ATTR_SOURCE": self.empty_tree,
        })
        return env

    async def run(
        self, args: list[str], *, timeout: int,
        input: bytes | None = None, text: bool = True,
    ) -> subprocess.CompletedProcess:
        """``git <args>`` on the private git dir, inside the work tree."""
        env = self._env()
        command = [
            "-c", f"core.attributesFile={os.devnull}",
            f"--git-dir={self.private_dir}", "--work-tree=.", *args,
        ]
        if self.work_tree_fd is None:
            cwd, pass_fds = self.work_tree, ()
        else:
            cwd = f"{_FD_DIRECTORY}/{self.work_tree_fd}"
            pass_fds = (self.work_tree_fd,)
        return await _run_git_process_async(
            _hardened_git_argv(command, env), cwd, env, timeout,
            input=input, text=text, pass_fds=pass_fds,
        )

    async def skip_submodule_work_trees(self) -> None:
        """Mark every submodule entry (gitlink) of the private index
        skip-worktree, so git never looks inside a submodule's directory.

        git checks a checked-out submodule for local changes by starting git
        inside it, by discovery. That git reads the submodule's own config
        and ``info/attributes``, both the agent's, so the agent's filter
        driver would run. ``diff.ignoreSubmodules`` cannot stop it, since the
        agent's ``.gitmodules`` can set ``submodule.<name>.ignore`` back for
        each submodule; git does not examine the work tree of a skip-worktree
        entry at all. A stash still records each submodule entry as staged;
        a submodule HEAD moved but not staged is not recorded.

        Raises :class:`AgentWorktreeGitError` when the index cannot be read,
        holds an unmerged submodule entry, or cannot be marked.
        """
        listing = await self.run(["ls-files", "--stage", "-z"], timeout=30, text=False)
        if listing.returncode != 0:
            raise AgentWorktreeGitError(
                f"cannot list the index of {self.work_tree} (rc={listing.returncode})"
            )
        submodules: list[bytes] = []
        # Each record is "<mode> <object> <stage>\t<path>".
        for record in listing.stdout.split(b"\0"):
            entry, _, path = record.partition(b"\t")
            fields = entry.split(b" ")
            if len(fields) != 3 or fields[0] != b"160000":
                continue
            if fields[2] != b"0":
                raise AgentWorktreeGitError(
                    f"the submodule entry {path[:200]!r} of {self.work_tree} is unmerged"
                )
            submodules.append(path)
        if not submodules:
            return
        marked = await self.run(
            ["update-index", "-z", "--skip-worktree", "--stdin"], timeout=30,
            input=b"".join(path + b"\0" for path in submodules),
        )
        if marked.returncode != 0:
            raise AgentWorktreeGitError(
                f"cannot mark the submodule entries of {self.work_tree} "
                f"(rc={marked.returncode}: {(marked.stderr or '').strip()[:200]})"
            )

    async def run_in_repository(
        self, args: list[str], *, timeout: int,
    ) -> subprocess.CompletedProcess:
        """``git <args>`` on the main repository's git dir (refs, objects;
        no work tree, so nothing that reads or writes file content)."""
        return await _run_in_main_repository(self.common_dir, args, timeout)

    async def current_head(self) -> tuple[str, str | None]:
        """``(commit, branch)`` the worktree's own git dir names now."""
        return await _worktree_head(self.common_dir, self.git_dir)

    async def publish_reset(self, branch: str, new: str, old: str) -> None:
        """After ``reset --hard`` here: move ``refs/heads/<branch>`` from
        ``old`` to ``new`` in the repository (with a reflog entry; refused
        if the branch moved meanwhile) and install the private index as
        the worktree's index, timestamps kept."""
        moved = await self.run_in_repository(
            ["update-ref", "--create-reflog", "-m", f"reset: moving to {new}",
             f"refs/heads/{branch}", new, old],
            timeout=15,
        )
        if moved.returncode != 0:
            raise AgentWorktreeGitError(
                f"could not move {branch} to {new[:12]} "
                f"(rc={moved.returncode}: {(moved.stderr or '').strip()[:200]})"
            )
        # A fresh name created here (never through a planted symlink), then
        # renamed over the index in one step.
        staged = os.path.join(self.git_dir, f"index.equipa-{os.getpid()}-{time.monotonic_ns()}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(staged, flags, 0o644)
            with os.fdopen(fd, "wb") as writer, open(
                os.path.join(self.private_dir, "index"), "rb",
            ) as reader:
                shutil.copyfileobj(reader, writer)
                info = os.fstat(reader.fileno())
            os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.replace(staged, os.path.join(self.git_dir, "index"))
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(staged)
            raise AgentWorktreeGitError(
                f"could not install the reset index in {self.git_dir}: {exc}"
            ) from exc

    async def store_stash(self) -> str:
        """Copy the private ``refs/stash`` to the repository's, with the
        stash's own message; returns the stash commit."""
        top = await self.run(["log", "-1", "--format=%H%n%s", "refs/stash"], timeout=15)
        commit, _, subject = (top.stdout or "").strip().partition("\n")
        if top.returncode != 0 or not _HEX_OBJECT_NAME_RE.fullmatch(commit):
            raise AgentWorktreeGitError(
                f"no stash was made in the private git dir (rc={top.returncode})"
            )
        stored = await _run_in_main_repository(
            self.common_dir,
            ["update-ref", "--create-reflog", "-m", subject, "refs/stash", commit], 15,
        )
        if stored.returncode != 0:
            raise AgentWorktreeGitError(
                f"could not record stash {commit[:12]} in {self.common_dir} "
                f"(rc={stored.returncode}: {(stored.stderr or '').strip()[:200]})"
            )
        return commit


async def _write_private_git_dir(
    private_dir: str, common_dir: str, git_dir: str,
    head: str, branch: str | None, object_format: str,
) -> bool:
    """Lay out the private git dir; True when the worktree index was copied."""
    for directory in ("refs/heads", "objects"):
        os.makedirs(os.path.join(private_dir, directory))
    if branch is not None and _PLAIN_BRANCH_RE.fullmatch(branch):
        Path(private_dir, "refs", "heads", branch).write_text(f"{head}\n", encoding="ascii")
        Path(private_dir, "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="ascii")
    else:
        Path(private_dir, "HEAD").write_text(f"{head}\n", encoding="ascii")
    settings: list[tuple[str, str | None]] = [
        ("core.repositoryformatversion", "0" if object_format == "sha1" else "1"),
        ("core.bare", "false"),
    ]
    if object_format != "sha1":
        settings.append(("extensions.objectformat", object_format))
    settings.extend(await _settings_from_file(
        Path(common_dir, "config"), _PRIVATE_SETTINGS_FROM_REPOSITORY,
    ))
    operator = await _operator_settings()
    settings.extend(operator)
    named = {key for key, _ in operator}
    settings.extend(entry for entry in _FALLBACK_IDENTITY if entry[0] not in named)
    Path(private_dir, "config").write_text(serialize_git_config(settings), encoding="utf-8")
    return _copy_regular_file(
        os.path.join(git_dir, "index"), os.path.join(private_dir, "index"),
        _INDEX_COPY_LIMIT,
    )


@contextlib.asynccontextmanager
async def agent_worktree_git(
    common_dir: str | os.PathLike, work_tree: str | os.PathLike,
) -> AsyncIterator[AgentWorktreeGit]:
    """An :class:`AgentWorktreeGit` for the task worktree ``work_tree`` of
    the repository whose git common dir is ``common_dir``.

    Raises :class:`AgentWorktreeGitError` when the work tree is a symlink,
    is not a registered worktree of that repository, or its HEAD or index
    cannot be read; the private git dir is removed on the way out.
    """
    common = os.path.realpath(os.fspath(common_dir))
    fd, real_work_tree = _open_agent_work_tree(work_tree)
    private_dir: str | None = None
    try:
        git_dir = find_worktree_git_dir(common, real_work_tree)
        if git_dir is None:
            raise AgentWorktreeGitError(
                f"{real_work_tree} is not a registered worktree of {common}"
            )
        formats = await _run_in_main_repository(
            common, ["rev-parse", "--show-object-format"], 10,
        )
        object_format = (formats.stdout or "").strip()
        if formats.returncode != 0 or object_format not in _EMPTY_TREES:
            raise AgentWorktreeGitError(
                f"cannot read the object format of {common} (rc={formats.returncode})"
            )
        head, branch = await _worktree_head(common, git_dir)
        private_dir = tempfile.mkdtemp(prefix="equipa-worktree-git-")
        index_copied = await _write_private_git_dir(
            private_dir, common, git_dir, head, branch, object_format,
        )
        worktree_git = AgentWorktreeGit(
            real_work_tree, common, git_dir, head, branch, private_dir,
            _EMPTY_TREES[object_format], fd,
        )
        if not index_copied:
            # No index: start from HEAD's tree, which leaves the files alone.
            read_tree = await worktree_git.run(["read-tree", head], timeout=30)
            if read_tree.returncode != 0:
                raise AgentWorktreeGitError(
                    f"cannot build an index for {real_work_tree} "
                    f"(rc={read_tree.returncode})"
                )
        yield worktree_git
    finally:
        if fd is not None:
            os.close(fd)
        if private_dir is not None:
            shutil.rmtree(private_dir, ignore_errors=True)


# --- Read-only git in a task worktree (FF-3155, task #3158) --------------------
#
# Cleanup is not the only git the orchestrator runs in an agent's worktree.
# While the agent runs and right after, the dev-test loop (the tester's diff
# context, the no-change check, the markdown scan), the claimed-file check,
# the progress monitor and the reviewer's tree snapshot run ``git diff`` /
# ``git status`` / ``git ls-files`` there through git_run / git_run_async.
# Found by discovery, that git read the repository's config and the work
# tree's attributes, so hashing a stat-dirty file (``git diff <rev>`` every
# time, ``git status`` on a same-size edit) ran the clean filter the agent
# had planted: a plain ``git config`` in its worktree writes the shared
# config, and ``.gitattributes`` or ``info/attributes`` selects the driver.
#
# Those subcommands now run, when their cwd lies in
# ``<project>/.forge-worktrees/<name>``, on the git dir the main repository
# registered for that worktree (HEAD and index are the worktree's own), with
# ``GIT_COMMON_DIR`` naming a private directory the orchestrator writes for
# the one call. git reads config and ``info/attributes`` from the common dir,
# so neither the repository's config, its ``config.worktree`` (the extension
# is not copied) nor its ``info/attributes`` is read; the global config is
# ``/dev/null`` and the system files are off. No driver is defined anywhere
# git looks, so none runs; as defence in depth (FF-3158, task #3162) the
# attributes come from the empty tree (``GIT_ATTR_SOURCE``) and no global
# attributes file is read, so nothing selects one either. Objects are
# the repository's (``GIT_OBJECT_DIRECTORY``); refs, reflogs and packed refs
# are reached through links to the repository's own (data git reads, never
# runs). Submodule work trees are not examined (``--ignore-submodules=dirty``,
# or the caller's ``=all``): git would start inside each one, by discovery,
# with that submodule's own config. A path under ``.forge-worktrees`` that is
# not a registered worktree, or that leaves its work tree through a symlink,
# is refused with a failed result rather than run by discovery.

# Read-only subcommands that hash work-tree files (and so can run a clean
# filter). Other subcommands keep running by discovery: location queries
# must report the real repository, and cleanup writes go through
# agent_worktree_git.
_WORK_TREE_READING_SUBCOMMANDS = frozenset({
    "diff", "diff-files", "diff-index", "grep", "ls-files", "status",
})
# The ones that check each checked-out submodule for local changes.
_SUBMODULE_EXAMINING_SUBCOMMANDS = frozenset({"diff", "diff-files", "diff-index", "status"})
# The repository settings the private config keeps: its format, and how git
# compares a file with its index entry. None names a program.
_VIEW_SETTINGS_FROM_REPOSITORY = (
    r"^(core\.(repositoryformatversion|filemode|symlinks|ignorecase"
    r"|precomposeunicode|trustctime|checkstat)|extensions\.(objectformat|refstorage))$"
)
# Linked from the repository's common dir into the private one.
_VIEW_LINKED_ENTRIES = ("refs", "packed-refs", "logs", "shallow", "reftable")
# git's exit status for "could not run"; what a refused call returns.
_REFUSED_RETURNCODE = 128


@dataclass(frozen=True)
class _TaskWorktreePath:
    """A cwd inside ``<project_root>/.forge-worktrees/<name>`` (``work_tree``)."""

    project_root: str
    work_tree: str


def _reads_work_tree(args: Sequence[str]) -> bool:
    subcommand = _git_subcommand_index(args)
    return subcommand is not None and args[subcommand] in _WORK_TREE_READING_SUBCOMMANDS


def _task_worktree_path(cwd: str | os.PathLike) -> _TaskWorktreePath | None:
    """The task worktree ``cwd`` lies in, judged on the path as given and
    on its realpath (a symlink into or out of a worktree counts); None for
    any other directory."""
    for candidate in (os.path.abspath(os.fspath(cwd)), os.path.realpath(cwd)):
        parts = Path(candidate).parts
        if WORKTREE_BASE_DIRNAME in parts[:-1]:
            index = parts.index(WORKTREE_BASE_DIRNAME)
            return _TaskWorktreePath(
                project_root=str(Path(*parts[:index])),
                work_tree=str(Path(*parts[:index + 2])),
            )
    return None


def _checkout_common_dir(project_root: str) -> str:
    """Realpath of the git common dir of the checkout at ``project_root``,
    read as data (a ``.git`` directory, or the ``gitdir:`` file and the
    ``commondir`` file of the git dir it names). Raises
    :class:`AgentWorktreeGitError`."""
    dot_git = os.path.join(project_root, ".git")
    try:
        if os.path.isdir(dot_git):
            git_dir = dot_git
        else:
            named = read_regular_file_bounded(dot_git, _GIT_LINE_FILE_LIMIT)
            text = named.decode("utf-8", "replace").strip()
            if not text.startswith("gitdir:"):
                raise AgentWorktreeGitError(f"{dot_git} names no git dir")
            git_dir = os.path.join(project_root, text[len("gitdir:"):].strip())
        try:
            common = read_regular_file_bounded(
                os.path.join(git_dir, "commondir"), _GIT_LINE_FILE_LIMIT,
            ).decode("utf-8", "replace").strip()
        except FileNotFoundError:
            common = "."
    except OSError as exc:
        raise AgentWorktreeGitError(
            f"cannot locate the repository of {project_root}: {exc}"
        ) from exc
    return os.path.realpath(os.path.join(git_dir, common))


def _submodule_work_trees_unexamined(args: Sequence[str]) -> list[str]:
    """``args`` with ``--ignore-submodules=dirty`` before any ``--``, for a
    subcommand that examines submodules, unless the caller's own last
    ``--ignore-submodules`` already ignores them all. The flag overrides
    ``.gitmodules`` and config; ``dirty`` still reports a moved gitlink."""
    command = list(args)
    subcommand = _git_subcommand_index(command)
    if subcommand is None or command[subcommand] not in _SUBMODULE_EXAMINING_SUBCOMMANDS:
        return command
    end = command.index("--") if "--" in command[subcommand:] else len(command)
    chosen = [
        token.partition("=")[2] or "all"
        for token in command[subcommand + 1:end]
        if token == "--ignore-submodules" or token.startswith("--ignore-submodules=")
    ]
    if chosen and chosen[-1] == "all":
        return command
    command.insert(end, "--ignore-submodules=dirty")
    return command


def _view_settings_commands(common_dir: str) -> list[list[str]]:
    """``git config`` listings of the settings the private config keeps:
    the repository's (:data:`_VIEW_SETTINGS_FROM_REPOSITORY`) and, while the
    global config is pinned and verifies, the operator's identity and
    ``core.excludesFile``."""
    commands = [[
        "config", "--file", os.path.join(common_dir, "config"), "-z",
        "--get-regexp", _VIEW_SETTINGS_FROM_REPOSITORY,
    ]]
    pin = _global_config_pin
    if pin is not None and verify_global_git_config_pin() is None:
        commands.append([
            "config", "--file", str(pin.path), "-z",
            "--get-regexp", _PRIVATE_SETTINGS_FROM_GLOBAL,
        ])
    return commands


@dataclass(frozen=True)
class _WorktreeView:
    """One read-only call's view of a task worktree; see the section comment."""

    work_tree: str
    relative: str
    common_dir: str
    git_dir: str
    work_tree_fd: int | None
    private_dir: str | None = None
    empty_tree: str | None = None

    @property
    def _work_tree_path(self) -> str:
        if self.work_tree_fd is None:
            return self.work_tree
        return f"{_FD_DIRECTORY}/{self.work_tree_fd}"

    @property
    def cwd(self) -> str:
        """Where git starts: the caller's directory, below the opened work tree."""
        return os.path.join(self._work_tree_path, self.relative)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return () if self.work_tree_fd is None else (self.work_tree_fd,)

    def env(self, extra_env: Mapping[str, str] | None, args: Sequence[str]) -> dict[str, str]:
        if self.private_dir is None or self.empty_tree is None:  # pragma: no cover
            raise AgentWorktreeGitError("the private common dir was not written")
        env = _hardened_git_env(extra_env, args)
        env.update(_NO_TRANSPORT_ENV)
        env.update({
            "GIT_COMMON_DIR": self.private_dir,
            "GIT_OBJECT_DIRECTORY": os.path.join(self.common_dir, "objects"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            # FF-3158 (task #3162), defence in depth: the work tree's
            # .gitattributes select nothing either (git >= 2.40).
            "GIT_ATTR_SOURCE": self.empty_tree,
        })
        return env

    def argv(self, args: Sequence[str], env: Mapping[str, str]) -> list[str]:
        return _hardened_git_argv(
            ["-c", f"core.attributesFile={os.devnull}",
             f"--git-dir={self.git_dir}", f"--work-tree={self._work_tree_path}",
             *_submodule_work_trees_unexamined(args)],
            env,
        )

    def close(self) -> None:
        if self.work_tree_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.work_tree_fd)
        if self.private_dir is not None:
            shutil.rmtree(self.private_dir, ignore_errors=True)


def _locate_worktree_view(location: _TaskWorktreePath, cwd: str | os.PathLike) -> _WorktreeView:
    """Open the work tree of ``location`` and find its registered git dir.

    Raises :class:`AgentWorktreeGitError` when the work tree is a symlink,
    ``cwd`` leaves it, or the repository holding ``.forge-worktrees`` does
    not register it. The caller owns the open descriptor.
    """
    common_dir = _checkout_common_dir(location.project_root)
    fd, real_work_tree = _open_agent_work_tree(location.work_tree)
    try:
        real_cwd = os.path.realpath(cwd)
        if real_cwd != real_work_tree and not real_cwd.startswith(real_work_tree + os.sep):
            raise AgentWorktreeGitError(
                f"{os.fspath(cwd)} leaves the task worktree {real_work_tree}"
            )
        git_dir = find_worktree_git_dir(common_dir, real_work_tree)
        if git_dir is None:
            raise AgentWorktreeGitError(
                f"{real_work_tree} is not a registered worktree of {common_dir}"
            )
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    return _WorktreeView(
        work_tree=real_work_tree,
        relative=os.path.relpath(real_cwd, real_work_tree),
        common_dir=common_dir,
        git_dir=git_dir,
        work_tree_fd=fd,
    )


def _with_private_common_dir(
    view: _WorktreeView, listings: Sequence[subprocess.CompletedProcess],
) -> _WorktreeView:
    """``view`` with its private common dir written: the settings parsed
    from ``listings`` (the :func:`_view_settings_commands` results), the
    links of :data:`_VIEW_LINKED_ENTRIES` and a copy of ``info/exclude``."""
    settings: list[tuple[str, str | None]] = [("core.bare", "false")]
    for listing in listings:
        if listing.returncode == 0:
            settings.extend(parse_config_list_z(listing.stdout))
    object_format = next(
        ((value or "").strip().lower() for key, value in reversed(settings)
         if key.lower() == "extensions.objectformat"),
        "sha1",
    )
    if object_format not in _EMPTY_TREES:
        raise AgentWorktreeGitError(
            f"{view.common_dir} uses the unknown object format {object_format[:40]!r}"
        )
    private_dir = tempfile.mkdtemp(prefix="equipa-worktree-view-")
    try:
        Path(private_dir, "config").write_text(serialize_git_config(settings), encoding="utf-8")
        for name in _VIEW_LINKED_ENTRIES:
            target = os.path.join(view.common_dir, name)
            if os.path.lexists(target):
                os.symlink(target, os.path.join(private_dir, name))
        # Patterns only (nothing git runs); a planted FIFO or a huge file
        # is skipped rather than read.
        with contextlib.suppress(OSError):
            exclude = read_regular_file_bounded(
                os.path.join(view.common_dir, "info", "exclude"), MAX_TRUSTED_FILE_BYTES,
            )
            os.mkdir(os.path.join(private_dir, "info"))
            Path(private_dir, "info", "exclude").write_bytes(exclude)
    except OSError as exc:
        shutil.rmtree(private_dir, ignore_errors=True)
        raise AgentWorktreeGitError(f"cannot write a private common dir: {exc}") from exc
    return dataclass_replace(
        view, private_dir=private_dir, empty_tree=_EMPTY_TREES[object_format],
    )


def _refused_result(
    args: Sequence[str], reason: AgentWorktreeGitError, text: bool,
) -> subprocess.CompletedProcess:
    """A failed result for a call that would have run git by discovery in
    an agent-controlled directory. Callers already treat a failed git as
    "could not tell", so none of them is left with an exception it does
    not expect."""
    message = f"equipa: refused to run git {list(args)!r} by discovery: {reason}"
    logger.warning("[git] %r", message)
    return subprocess.CompletedProcess(
        args=["git", *args], returncode=_REFUSED_RETURNCODE,
        stdout="" if text else b"",
        stderr=f"{message}\n" if text else f"{message}\n".encode("utf-8", "replace"),
    )


def _git_run_in_worktree_view(
    location: _TaskWorktreePath,
    args: list[str],
    cwd: str | Path,
    timeout: int,
    env: Mapping[str, str] | None,
    text: bool,
) -> subprocess.CompletedProcess:
    """:func:`git_run` for a work-tree reading call inside a task worktree."""
    try:
        view = _locate_worktree_view(location, cwd)
    except AgentWorktreeGitError as exc:
        return _refused_result(args, exc, text)
    try:
        listings = [
            git_run(command, view.common_dir, timeout=10)
            for command in _view_settings_commands(view.common_dir)
        ]
        view = _with_private_common_dir(view, listings)
        run_env = view.env(env, args)
        return _run_with_env(
            view.argv(args, run_env), view.cwd, timeout, run_env,
            text=text, pass_fds=view.pass_fds,
        )
    except AgentWorktreeGitError as exc:
        return _refused_result(args, exc, text)
    finally:
        view.close()


async def _git_run_async_in_worktree_view(
    location: _TaskWorktreePath,
    args: list[str],
    cwd: str | Path,
    timeout: int,
    env: Mapping[str, str] | None,
    input: bytes | None,
    text: bool,
) -> subprocess.CompletedProcess:
    """:func:`git_run_async` for a work-tree reading call inside a task worktree."""
    try:
        view = _locate_worktree_view(location, cwd)
    except AgentWorktreeGitError as exc:
        return _refused_result(args, exc, text)
    try:
        listings = [
            await git_run_async(command, view.common_dir, timeout=10)
            for command in _view_settings_commands(view.common_dir)
        ]
        view = _with_private_common_dir(view, listings)
        run_env = view.env(env, args)
        return await _run_git_process_async(
            view.argv(args, run_env), view.cwd, run_env, timeout,
            input=input, text=text, pass_fds=view.pass_fds,
        )
    except AgentWorktreeGitError as exc:
        return _refused_result(args, exc, text)
    finally:
        view.close()


# --- Pinned global git config (task #3116, MI-04) -----------------------------
#
# Agents run as the operator's UID with the operator's HOME, so the global git
# config (~/.gitconfig, $XDG_CONFIG_HOME/git/config and every file they
# include) is agent-writable. A filter or merge driver defined there runs
# inside the orchestrator's own checkout, merge or status. Before dispatch the
# orchestrator therefore copies the effective global config, includes
# resolved, into a read-only file it created, and every hardened git call runs
# with GIT_CONFIG_GLOBAL pointing at that copy: an agent's later edits to the
# real files never reach the orchestrator. merge_integrity scans the copy for
# driver programs outside an allowlist and checks its hash before a merge.
#
# The copy is taken once per process and kept for its lifetime. Conditional
# includes (includeIf) are evaluated outside any repository when the copy is
# taken, so their repository-dependent parts do not survive.

# A pinned file, an attributes file or a config file is a few KiB. Anything
# larger is not trusted rather than read unbounded.
MAX_TRUSTED_FILE_BYTES = 1024 * 1024

# Keys that pull in another file. ``--includes`` already inlines what they
# include; keeping the key in the copy would re-read an agent-writable file.
_INCLUDE_KEY_RE = re.compile(r"^(include|includeif\..+)\.path$")


class GlobalConfigPinError(RuntimeError):
    """The operator's global git config could not be pinned or verified."""


@dataclass(frozen=True)
class GlobalConfigPin:
    """The orchestrator-owned copy of the operator's global git config."""

    path: Path
    sha256: str
    entries: int


_global_config_pin: GlobalConfigPin | None = None
_global_config_pin_lock = threading.Lock()


def read_regular_file_bounded(
    path: str | os.PathLike, limit: int = MAX_TRUSTED_FILE_BYTES,
) -> bytes:
    """Bytes of the regular file at ``path``, read without following a FIFO.

    Raises ``FileNotFoundError`` when absent and ``OSError`` when ``path`` is
    not a regular file or is larger than ``limit`` (a FIFO or device planted
    at the path must not hang or exhaust the orchestrator).
    """
    fd = os.open(
        os.fspath(path),
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0),
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{os.fspath(path)} is not a regular file")
        if info.st_size > limit:
            raise OSError(
                f"{os.fspath(path)} is {info.st_size} bytes (limit {limit})"
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise OSError(f"{os.fspath(path)} grew past {limit} bytes while read")
        return data
    finally:
        os.close(fd)


def parse_config_list_z(raw: str) -> list[tuple[str, str | None]]:
    """(key, value) pairs from ``git config --list -z`` output, in order.

    A record without a newline is a bare boolean key (``[core] bare``) and
    gets the value None.
    """
    entries: list[tuple[str, str | None]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        key, newline, value = record.partition("\n")
        entries.append((key, value if newline else None))
    return entries


def _quote_config_value(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def serialize_git_config(entries: Sequence[tuple[str, str | None]]) -> str:
    """Render (key, value) pairs as a git config file, order preserved.

    Every entry gets its own section header, so multi-valued keys keep their
    order (a ``credential.helper`` reset depends on it) and subsections that
    contain dots (``credential.https://example.com.helper``) round-trip.
    """
    lines: list[str] = []
    for key, value in entries:
        section, _, rest = key.partition(".")
        subsection, _, name = rest.rpartition(".")
        if not section or not name or "\n" in subsection:
            raise GlobalConfigPinError(f"cannot serialise config key {key!r}")
        if subsection:
            quoted = subsection.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'[{section} "{quoted}"]')
        else:
            lines.append(f"[{section}]")
        if value is None:
            lines.append(f"\t{name}")
        else:
            lines.append(f"\t{name} = {_quote_config_value(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _operator_global_config_files(env: Mapping[str, str]) -> list[Path]:
    """The files ``git config --global`` reads for this environment."""
    if env.get("GIT_CONFIG_GLOBAL"):
        return [Path(env["GIT_CONFIG_GLOBAL"])]
    home = Path(env.get("HOME") or Path.home())
    xdg = env.get("XDG_CONFIG_HOME")
    xdg_dir = Path(xdg) if xdg else home / ".config"
    return [xdg_dir / "git" / "config", home / ".gitconfig"]


def _read_operator_global_config() -> list[tuple[str, str | None]]:
    """The operator's effective global config, includes resolved."""
    env = _get_repo_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    with tempfile.TemporaryDirectory(prefix="equipa-gitcfg-read-") as neutral:
        # Outside any repository, so no includeIf can match this repo's state.
        env["GIT_CEILING_DIRECTORIES"] = str(Path(neutral).parent)
        result = _run_with_env(
            ["git", "config", "--global", "--list", "--includes", "-z"],
            neutral, GIT_DEFAULT_TIMEOUT, env,
        )
    if result.returncode == 0:
        return parse_config_list_z(result.stdout)
    if not any(p.exists() for p in _operator_global_config_files(env)):
        return []  # no global config at all: pin an empty file
    raise GlobalConfigPinError(
        f"could not read the global git config (rc={result.returncode}: "
        f"{result.stderr.strip()[:200]})"
    )


def global_git_config_pin() -> GlobalConfigPin | None:
    """The active pin, or None before :func:`pin_global_git_config` ran."""
    return _global_config_pin


def pin_global_git_config() -> GlobalConfigPin:
    """Pin the operator's global git config for every later hardened git call.

    Idempotent: the first call copies the config and every later call returns
    the same pin. Call it before any agent runs (``DefaultBranchGuard``
    does). Raises :class:`GlobalConfigPinError` when the config cannot be
    read, written or read back identically.
    """
    global _global_config_pin
    with _global_config_pin_lock:
        if _global_config_pin is not None:
            return _global_config_pin
        entries = [
            (key, value) for key, value in _read_operator_global_config()
            if not _INCLUDE_KEY_RE.match(key)
        ]
        text = serialize_git_config(entries)
        directory = Path(tempfile.mkdtemp(prefix="equipa-gitconfig-"))
        path = directory / "global.gitconfig"
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.chmod(path, 0o400)
            data = read_regular_file_bounded(path)
            env = _get_repo_env()
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            readback = _run_with_env(
                ["git", "config", "--file", str(path), "--list", "-z"],
                directory, GIT_DEFAULT_TIMEOUT, env,
            )
            if (
                readback.returncode != 0
                or parse_config_list_z(readback.stdout) != entries
            ):
                raise GlobalConfigPinError(
                    f"pinned global git config does not read back as written "
                    f"(rc={readback.returncode}: {readback.stderr.strip()[:200]})"
                )
        except (OSError, subprocess.SubprocessError, GlobalConfigPinError) as exc:
            shutil.rmtree(directory, ignore_errors=True)
            if isinstance(exc, GlobalConfigPinError):
                raise
            raise GlobalConfigPinError(
                f"could not write the pinned global git config: {exc}"
            ) from exc
        pin = GlobalConfigPin(path, hashlib.sha256(data).hexdigest(), len(entries))
        atexit.register(shutil.rmtree, directory, True)
        _global_config_pin = pin
        logger.info(
            "Pinned global git config: %d entries at %s (sha256 %s)",
            pin.entries, pin.path, pin.sha256[:16],
        )
        return pin


def verify_global_git_config_pin() -> str | None:
    """Why the pinned copy can no longer be trusted, or None while intact."""
    pin = _global_config_pin
    if pin is None:
        return "the global git config was not pinned before dispatch"
    try:
        data = read_regular_file_bounded(pin.path)
    except OSError as exc:
        return f"pinned global git config {pin.path} is unreadable ({exc})"
    if hashlib.sha256(data).hexdigest() != pin.sha256:
        return f"pinned global git config {pin.path} changed after it was pinned"
    return None


def pinned_git_env() -> dict[str, str]:
    """Environment for a git call that cannot go through :func:`git_run`.

    The allowlisted environment plus the hardening variables and, once pinned,
    ``GIT_CONFIG_GLOBAL`` pointing at the pre-dispatch copy — so a direct
    ``subprocess.run(["git", ...])`` in the orchestrator reads the same
    config as the hardened helper does (task #3116, MI-04).
    """
    return _hardened_git_env()


def reset_global_git_config_pin() -> None:
    """Forget the pin so the next :func:`pin_global_git_config` re-reads.

    For tests that swap HOME; production pins once per process.
    """
    global _global_config_pin
    with _global_config_pin_lock:
        _global_config_pin = None


def _gh_run(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a gh (GitHub CLI) command: git's allowlisted env plus the GitHub
    credentials (IR-04, task #3132)."""
    env = _get_repo_env()
    env.update(github_credential_env())
    return _run_with_env(["gh", *args], cwd, timeout, env)


# Per-process cache of detected default branch, keyed by resolved repo path.
# Populated by get_default_branch() so the orchestrator does not re-shell out
# on every security-gate diff. Entries store (branch_name, expiry_epoch).
# A 5-minute TTL bounds the window where a rename (master->main) goes
# undetected within a long-lived process (S2 of SECURITY-REVIEW-2479).
# Tests that recreate repos at the same path within the TTL must call
# ``_clear_default_branch_cache`` explicitly.
_DEFAULT_BRANCH_CACHE: dict[str, tuple[str, float]] = {}
_DEFAULT_BRANCH_CACHE_TTL_SECONDS: float = 300.0


def _cache_set(key: str, value: str) -> None:
    _DEFAULT_BRANCH_CACHE[key] = (value, time.monotonic() + _DEFAULT_BRANCH_CACHE_TTL_SECONDS)


def _cache_get(key: str) -> str | None:
    entry = _DEFAULT_BRANCH_CACHE.get(key)
    if entry is None:
        return None
    value, expiry = entry
    if time.monotonic() >= expiry:
        _DEFAULT_BRANCH_CACHE.pop(key, None)
        return None
    return value


def get_default_branch(repo_path: str | Path, *, strict: bool = False) -> str:
    """Detect the default branch of ``repo_path`` (``main`` vs ``master``).

    Resolution order:
      1. ``git symbolic-ref --short refs/remotes/origin/HEAD`` — fast and
         definitive when ``origin/HEAD`` is set on the remote.
      2. First existing local branch among ``main`` then ``master``.
      3. ``git rev-parse --abbrev-ref HEAD`` — whatever branch is currently
         checked out.
      4. If ``strict=True``, raise :class:`DefaultBranchDetectionError`.
         Otherwise (default) emit a WARNING and fall back to ``"master"``.

    NOT a trust decision: steps 1 and 3 read refs any agent worktree can
    rewrite. Anything that picks a trusted commit (overlay pin, merge target,
    gate diff base) must use :func:`get_trusted_default_branch` (SR-2997 S1).

    Results are cached per-process for 5 minutes keyed by the resolved
    absolute path of ``repo_path``. The TTL bounds the window where a
    branch rename remains undetected in a long-lived orchestrator
    process. Call :func:`_clear_default_branch_cache` to invalidate
    immediately (only needed in tests that recreate repos at the same
    path within the TTL).

    Args:
        repo_path: Filesystem path to a git working tree.
        strict: If True, raise :class:`DefaultBranchDetectionError` on
            total detection failure instead of returning ``"master"``.

    Raises:
        DefaultBranchDetectionError: Only when ``strict=True`` and every
            detection strategy fails.
    """
    key = str(Path(repo_path).resolve())
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # 1) origin/HEAD
    try:
        result = git_run(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            repo_path, timeout=5,
        )
        if result.returncode == 0:
            ref = (result.stdout or "").strip()
            if ref.startswith("origin/"):
                ref = ref[len("origin/"):]
            if ref:
                _cache_set(key, ref)
                return ref
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    # 2) Local branch existence: prefer main, fall back to master
    for candidate in ("main", "master"):
        try:
            result = git_run(
                ["rev-parse", "--verify", "--quiet",
                 f"refs/heads/{candidate}"],
                repo_path, timeout=5,
            )
            if result.returncode == 0:
                _cache_set(key, candidate)
                return candidate
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            continue

    # 3) Current branch
    try:
        result = git_run(
            ["rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=5,
        )
        if result.returncode == 0:
            ref = (result.stdout or "").strip()
            if ref and ref != "HEAD":
                _cache_set(key, ref)
                return ref
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    # 4) Total detection failure.
    if strict:
        raise DefaultBranchDetectionError(
            f"could not detect default branch for {repo_path!s}"
        )
    logger.warning(
        "could not detect default branch for %s, using legacy fallback 'master'",
        repo_path,
    )
    _cache_set(key, "master")
    return "master"


def _clear_default_branch_cache() -> None:
    """Drop every cached default-branch entry. Intended for tests.

    The underscore prefix marks this as a test-only helper (S5 of
    SECURITY-REVIEW-2479).
    """
    _DEFAULT_BRANCH_CACHE.clear()


# --- Trusted default branch (SR-2997 S1) -------------------------------------
#
# ``get_default_branch`` above is a best-effort DETECTOR: it trusts
# ``refs/remotes/origin/HEAD`` first and the checked-out HEAD last. Both live in
# the git dir that every agent worktree shares and can write, so an agent can
# point them at its own ``forge-task-*`` branch. Anything that decides WHICH
# commit is trusted — the role-overlay pin, the merge target, the security
# gate's diff base — must use ``get_trusted_default_branch`` instead.

TRUSTED_DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master")
AGENT_BRANCH_PREFIXES: tuple[str, ...] = ("forge-task-",)
PROJECT_DEFAULT_BRANCHES_CONFIG_KEY = "project_default_branches"
_SAFE_BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class UntrustedDefaultBranchError(DefaultBranchDetectionError):
    """No operator-trusted default branch could be resolved.

    Callers on a trust path must fail closed: no role overlays, no merge.
    """


def is_agent_branch_name(name: str) -> bool:
    """True if ``name`` is a per-task agent branch (never a default branch)."""
    return name.lower().startswith(AGENT_BRANCH_PREFIXES)


def _is_safe_branch_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(_SAFE_BRANCH_NAME_RE.fullmatch(name))
        and ".." not in name
        and "//" not in name
        and not name.endswith((".lock", "/", "."))
    )


def configured_default_branch(repo_path: str | Path) -> str | None:
    """Operator-named default branch of the project containing ``repo_path``.

    Read from the dispatch config key ``project_default_branches``: a mapping
    of project root path to branch name, e.g.
    ``{"/srv/forge-share/AI_Stuff/Equipa-repo": "main"}``. A path inside an
    agent worktree maps to its project root first. Returns None when the
    project has no entry.

    Raises:
        UntrustedDefaultBranchError: the entry exists but is not a string.
    """
    from equipa.config import get_active_dispatch_config
    from equipa.role_resolver import stable_project_root

    try:
        config = get_active_dispatch_config()
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("could not load dispatch config for %s: %s",
                       PROJECT_DEFAULT_BRANCHES_CONFIG_KEY, exc)
        return None
    mapping = config.get(PROJECT_DEFAULT_BRANCHES_CONFIG_KEY) if isinstance(config, dict) else None
    if not isinstance(mapping, dict):
        return None
    root = stable_project_root(repo_path)
    for raw_path, branch in mapping.items():
        try:
            matches = Path(str(raw_path)).expanduser().resolve() == root
        except (OSError, RuntimeError):
            continue
        if not matches:
            continue
        if not isinstance(branch, str):
            raise UntrustedDefaultBranchError(
                f"{PROJECT_DEFAULT_BRANCHES_CONFIG_KEY}[{raw_path!r}] must be a "
                f"branch name, got {branch!r}"
            )
        return branch.strip()
    return None


def _trusted_branch_exists(
    repo_path: str | Path, branch: str, common_dir: str | os.PathLike | None = None,
) -> bool:
    """True if ``refs/heads/<branch>`` names a commit; raises if git cannot run.

    A git failure must not read as "branch absent": that could turn an
    ambiguous main+master repo into a single-candidate one. With
    ``common_dir`` the lookup runs on that git dir, named explicitly, with
    no transport allowed (no discovery from ``repo_path``).
    """
    lookup = ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"]
    try:
        if common_dir is None:
            result = git_run(lookup, repo_path, timeout=10)
        else:
            common = os.fspath(common_dir)
            result = git_run(
                [f"--git-dir={common}", *lookup], common, timeout=10,
                env=_NO_TRANSPORT_ENV,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        raise UntrustedDefaultBranchError(
            f"could not check refs/heads/{branch} in {repo_path!s}: {exc}"
        ) from exc
    return result.returncode == 0


def get_trusted_default_branch(
    repo_path: str | Path, *, common_dir: str | os.PathLike | None = None,
) -> str:
    """Default branch of ``repo_path`` from operator-controlled sources only.

    Never reads ``refs/remotes/origin/HEAD`` or the checked-out HEAD, and is
    never cached, so a symbolic ref repointed from an agent worktree has no
    effect.

    ``common_dir`` (FF-3158, task #3162) is the git common dir of the
    repository to look the branches up in, for a ``repo_path`` in an agent's
    task worktree: git is then never started there by discovery, where the
    worktree's ``.git`` (the agent's) could name another repository.
    ``repo_path`` still selects the operator's configured entry. Resolution:

      1. The operator-named branch in ``project_default_branches``. It must be
         a plain branch name, must not be a ``forge-task-*`` agent branch, and
         must exist as ``refs/heads/<name>``.
      2. Otherwise exactly one of ``main`` / ``master`` must exist locally.
         Both existing is ambiguous (an agent can create either), so it fails
         closed rather than guessing.

    Raises:
        UntrustedDefaultBranchError: nothing trusted resolved; fail closed.
    """
    configured = configured_default_branch(repo_path)
    if configured is not None:
        if not _is_safe_branch_name(configured) or is_agent_branch_name(configured):
            raise UntrustedDefaultBranchError(
                f"configured default branch {configured!r} for {repo_path!s} is "
                f"not an operator branch name"
            )
        if not _trusted_branch_exists(repo_path, configured, common_dir):
            raise UntrustedDefaultBranchError(
                f"configured default branch {configured!r} does not exist in "
                f"{repo_path!s}"
            )
        return configured
    existing = [
        candidate for candidate in TRUSTED_DEFAULT_BRANCH_CANDIDATES
        if _trusted_branch_exists(repo_path, candidate, common_dir)
    ]
    if len(existing) == 1:
        return existing[0]
    found = " and ".join(existing) if existing else "neither main nor master"
    raise UntrustedDefaultBranchError(
        f"no trusted default branch for {repo_path!s} ({found} exist); set "
        f"{PROJECT_DEFAULT_BRANCHES_CONFIG_KEY} in the dispatch config"
    )


def _git_commit(
    message: str,
    cwd: str | Path,
    timeout: int = 120,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run git commit with EQUIPA identity from dispatch_config.

    Always injects -c user.name and -c user.email if configured,
    so commits are attributed correctly regardless of global git config.
    """
    args = [*_git_identity_args(), "commit", "-m", message]
    if extra_args:
        args.extend(extra_args)
    return git_run(args, cwd, timeout=timeout)


def check_gh_installed() -> bool:
    """Verify that gh CLI is installed and authenticated.

    Returns True if ready, prints error and returns False otherwise.
    """
    if not shutil.which("gh"):
        print("ERROR: GitHub CLI (gh) is not installed.")
        print("Install it from: https://cli.github.com/")
        return False

    try:
        result = _gh_run(["auth", "status"], Path.cwd(), timeout=10)
        if result.returncode != 0:
            print("ERROR: GitHub CLI is not authenticated.")
            print("Run: gh auth login")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("ERROR: Could not check gh auth status.")
        return False

    return True


def setup_single_repo(
    codename: str,
    project_dir: str | Path,
    owner: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Initialize git and create a GitHub private repo for a single project.

    Returns (success: bool, message: str).
    """
    p = Path(project_dir)
    repo_name = codename.lower().replace(" ", "-")

    # Skip if already fully set up (has .git AND a remote)
    has_git = (p / ".git").exists()
    if has_git:
        try:
            r = git_run(["remote", "get-url", "origin"], p, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True, f"Already set up (remote: {r.stdout.strip()})"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if dry_run:
        lang_info = detect_project_language(project_dir)
        return True, (
            f"DRY RUN: Would init git, detect={lang_info['primary']}, "
            f"create {owner}/{repo_name}"
        )

    # .gitignore
    lang_info = detect_project_language(project_dir)
    lang = lang_info["primary"]
    # Map new language keys to existing gitignore template keys
    gitignore_key_map = {
        "typescript": "node",
        "javascript": "node",
        "csharp": "dotnet",
    }
    gitignore_key = gitignore_key_map.get(lang, lang)
    gitignore_path = p / ".gitignore"
    if not gitignore_path.exists():
        template = GITIGNORE_TEMPLATES.get(
            gitignore_key, GITIGNORE_TEMPLATES["default"],
        )
        gitignore_path.write_text(template + "\n", encoding="utf-8")
        print(f"    Created .gitignore ({lang})")

    # git init
    if not has_git:
        r = git_run(["init"], p)
        if r.returncode != 0:
            return False, f"git init failed: {r.stderr.strip()}"
    else:
        print("    .git already exists, resuming setup")

    # git add (filter CRLF warnings)
    r = git_run(["add", "."], p, timeout=300)
    if r.returncode != 0:
        real_errors = [
            line for line in r.stderr.strip().splitlines()
            if not line.startswith("warning:")
        ]
        if real_errors:
            return False, f"git add failed: {chr(10).join(real_errors)}"

    # git commit
    commit_args = [*_git_identity_args(), "commit", "-m", "Initial commit"]
    r = git_run(commit_args, p, timeout=120)
    if r.returncode != 0:
        if "nothing to commit" not in (r.stdout + r.stderr):
            return False, f"git commit failed: {r.stderr.strip()}"
        print("    Nothing to commit (empty or already committed)")

    # gh repo create
    r = _gh_run(
        ["repo", "create", f"{owner}/{repo_name}",
         "--private", "--source=.", "--push"],
        p, timeout=300,
    )
    if r.returncode != 0:
        if "already exists" in r.stderr:
            print("    Repo already exists on GitHub, adding remote...")
            git_run(
                ["remote", "add", "origin",
                 f"https://github.com/{owner}/{repo_name}.git"],
                p, timeout=10,
            )
            # Task #2479: push the repo's actual default branch instead of
            # blindly trying main then master — once Equipa-repo renames
            # master -> main, a stray "master" push would recreate the old
            # name on the remote.
            # Task #2482 S1: if the first push fails and we fall back to
            # "main", we MUST check the second push's returncode too and
            # propagate failure with both stderr blobs. Returning success
            # when both pushes failed was the original HIGH finding.
            default_branch = get_default_branch(p)
            pr = git_run(
                ["push", "-u", "origin", default_branch], p, timeout=120,
            )
            if pr.returncode != 0:
                if default_branch == "main":
                    return False, (
                        f"git push failed for branch '{default_branch}': "
                        f"{pr.stderr.strip()}"
                    )
                pr2 = git_run(
                    ["push", "-u", "origin", "main"], p, timeout=120,
                )
                if pr2.returncode != 0:
                    return False, (
                        f"git push failed for both branches; "
                        f"'{default_branch}' stderr: {pr.stderr.strip()}; "
                        f"'main' stderr: {pr2.stderr.strip()}"
                    )
        else:
            return False, f"gh repo create failed: {r.stderr.strip()}"

    return True, f"Created https://github.com/{owner}/{repo_name}"


def setup_all_repos(args) -> None:
    """Initialize git + GitHub repos for all (or one) project.

    Uses --setup-repos for all, --setup-repos-project for a single project.

    NOTE: This function imports fetch_project_info from the orchestrator at
    call time to avoid circular imports during the Phase 1 split.
    """
    # Lazy import to avoid circular dependency during package split
    from equipa.tasks import fetch_project_info

    # Check prerequisites (skip for dry run)
    if not args.dry_run and not check_gh_installed():
        sys.exit(1)

    # synced storage warning
    print("\n" + "!" * 60)
    print("WARNING: Git repos in synced storage can experience corruption")
    print("from sync conflicts on the .git/index binary file.")
    print("")
    print("Recommendation: Avoid editing the same project on multiple")
    print("PCs simultaneously. The GitHub remote serves as your backup —")
    print("you can always re-clone if needed.")
    print("!" * 60)

    if not args.yes and not args.dry_run:
        response = input("\nContinue? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Determine which projects to set up
    if args.setup_repos_project:
        # Single project by ID
        project_info = fetch_project_info(args.setup_repos_project)
        if not project_info:
            print(
                f"ERROR: Project {args.setup_repos_project} not found in TheForge",
            )
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(
                f"ERROR: No directory mapped for project "
                f"'{project_info.get('name')}'",
            )
            sys.exit(1)

        targets = [(codename or pname, project_dir)]
    else:
        # All projects
        targets = list(PROJECT_DIRS.items())

    print(f"\nSetting up {len(targets)} project(s)...\n")

    results = []
    for codename, project_dir in targets:
        if not Path(project_dir).exists():
            print(
                f"  [{codename}] SKIP — directory does not exist: {project_dir}",
            )
            results.append((codename, False, "Directory does not exist"))
            continue

        print(f"  [{codename}] {project_dir}")
        success, msg = setup_single_repo(
            codename, project_dir, GITHUB_OWNER, args.dry_run,
        )
        status = "OK" if success else "FAIL"
        print(f"    -> {status}: {msg}")
        results.append((codename, success, msg))

    # Summary
    ok = sum(1 for _, s, _ in results if s)
    fail = len(results) - ok
    print(
        f"\nDone: {ok} succeeded, {fail} failed out of {len(results)} projects.",
    )
