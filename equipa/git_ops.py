"""EQUIPA git operations: repo setup, language detection, and git helpers.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
All functions are re-exported via equipa/__init__.py for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import atexit
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from equipa.constants import (
    GIT_DEFAULT_TIMEOUT,
    GITIGNORE_TEMPLATES,
    GITHUB_OWNER,
    PROJECT_DIRS,
)
from equipa.role_resolver import _GIT_SAFE_CONFIG

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


def _is_git_repo(path: str | Path) -> bool:
    """Check if a directory is a git repository."""
    return (Path(path) / ".git").exists()


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
# Programs named by the orchestrator's OWN environment (GIT_SSH_COMMAND,
# GIT_EDITOR, GIT_ASKPASS, ...) still outrank these pins. That environment is
# operator-controlled, not agent-writable.
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
)

GIT_HARDENING_ARGS: tuple[str, ...] = (
    "--no-pager",
    *_GIT_SAFE_CONFIG,
    *(
        flag
        for key, value in _GIT_PROGRAM_CONFIG_PINS
        for flag in ("-c", f"{key}={value}")
    ),
)

# GIT_CONFIG_NOSYSTEM / GIT_ATTR_NOSYSTEM (task #3116, MI-04): the system
# config and attributes files are skipped; the global config is replaced by
# the orchestrator's pre-dispatch snapshot (see pin_global_git_config).
GIT_HARDENING_ENV: Mapping[str, str] = MappingProxyType({
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
})

# diff.external cannot be cleared with -c: git runs an empty value as a
# command and every patch diff dies. Diff drivers are switched off per
# subcommand instead. --no-ext-diff covers diff.external and
# diff.<driver>.command; --no-textconv covers diff.<driver>.textconv, which
# could also show the security reviewer a converted view that hides code.
_DIFF_DRIVER_SUBCOMMANDS = frozenset({"diff", "log", "show"})
_DIFF_DRIVER_OFF_ARGS: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")

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


def _hardened_git_env(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Process env, then the caller's ``extra_env``, then the hardening.

    The hardening is applied last so no caller can switch it back off.
    """
    env = _get_repo_env()
    if extra_env:
        env.update(extra_env)
    env.update(GIT_HARDENING_ENV)
    pin = _global_config_pin
    if pin is not None:
        env["GIT_CONFIG_GLOBAL"] = str(pin.path)
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


def _hardened_git_argv(args: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Full argv for ``git <args>`` with the hardening flags in front.

    Diff-family subcommands also get ``_DIFF_DRIVER_OFF_ARGS`` directly after
    the subcommand name, ahead of any ``--`` the caller passes.
    """
    command = list(args)
    subcommand = _git_subcommand_index(command)
    if subcommand is not None and command[subcommand] in _DIFF_DRIVER_SUBCOMMANDS:
        command[subcommand + 1:subcommand + 1] = _DIFF_DRIVER_OFF_ARGS
    return ["git", *GIT_HARDENING_ARGS, *_operator_program_pins(env), *command]


def _get_repo_env() -> dict[str, str]:
    """Build an environment dict with git and gh on the PATH."""
    env = os.environ.copy()
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


def _run_with_env(
    args_list: list[str],
    cwd: str | Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Low-level subprocess runner with Windows-PATH-fixed env. Internal use only.

    ``env`` replaces the default :func:`_get_repo_env` environment when given.
    """
    return subprocess.run(
        args_list, capture_output=True, text=True,
        cwd=str(cwd), timeout=timeout,
        env=dict(env) if env is not None else _get_repo_env(),
    )


def git_run(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a hardened git command with standard env (Windows PATH fix) and timeout.

    The "git" prefix is added automatically — pass only the subcommand and
    its arguments, e.g. ``git_run(["status", "--porcelain"], cwd=repo)``.

    This is the single supported entry point for every git invocation in
    EQUIPA. It guarantees consistent timeout handling, PATH resolution and
    the gate-02/gate-03 hardening: replace refs are ignored and no hook or
    program named in repo config can run (see ``GIT_HARDENING_ARGS``).

    ``env`` holds extra variables layered over the process environment; the
    hardening variables are applied last and cannot be overridden.
    ``CompletedProcess.args`` is the full argv that actually ran.
    """
    run_env = _hardened_git_env(env)
    return _run_with_env(_hardened_git_argv(args, run_env), cwd, timeout, run_env)


async def git_run_async(
    args: list[str],
    cwd: str | Path,
    timeout: int = GIT_DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Async equivalent of ``git_run`` — does NOT block the event loop.

    Uses ``asyncio.create_subprocess_exec`` so callers running inside an
    async function (dispatch loops, dev-test loop) can issue git commands
    without serialising the loop. Returns a ``subprocess.CompletedProcess``
    with the same ``returncode``, ``stdout``, and ``stderr`` shape as
    ``git_run`` so call sites can be migrated incrementally. Applies the
    same hardening and ``env`` merging as ``git_run``. ``input``, when
    given, is written to git's stdin (e.g. for ``git patch-id``).

    A ``TimeoutError`` is raised if the command exceeds ``timeout`` seconds;
    the child process is killed before the error propagates.
    """
    run_env = _hardened_git_env(env)
    argv = _hardened_git_argv(args, run_env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=run_env,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
    return subprocess.CompletedProcess(
        args=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


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
    """Run a gh (GitHub CLI) command with the same env as git_run."""
    return _run_with_env(["gh", *args], cwd, timeout)


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


def _trusted_branch_exists(repo_path: str | Path, branch: str) -> bool:
    """True if ``refs/heads/<branch>`` names a commit; raises if git cannot run.

    A git failure must not read as "branch absent": that could turn an
    ambiguous main+master repo into a single-candidate one.
    """
    try:
        result = git_run(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
            repo_path, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise UntrustedDefaultBranchError(
            f"could not check refs/heads/{branch} in {repo_path!s}: {exc}"
        ) from exc
    return result.returncode == 0


def get_trusted_default_branch(repo_path: str | Path) -> str:
    """Default branch of ``repo_path`` from operator-controlled sources only.

    Never reads ``refs/remotes/origin/HEAD`` or the checked-out HEAD, and is
    never cached, so a symbolic ref repointed from an agent worktree has no
    effect. Resolution:

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
        if not _trusted_branch_exists(repo_path, configured):
            raise UntrustedDefaultBranchError(
                f"configured default branch {configured!r} does not exist in "
                f"{repo_path!s}"
            )
        return configured
    existing = [
        candidate for candidate in TRUSTED_DEFAULT_BRANCH_CANDIDATES
        if _trusted_branch_exists(repo_path, candidate)
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
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
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
