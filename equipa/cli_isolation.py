"""Keep project-scope Claude CLI configuration away from EQUIPA's CLI runs.

Copyright (c) 2026 Forgeborn

Agents (and the RLM ``claude -p`` helpers) run with ``cwd`` set to the
project directory, which agents can write (sandbox-11). By default the Claude
CLI loads project-scope configuration from its cwd:

* ``.claude/settings.json`` and ``.claude/settings.local.json``: one line of
  ``{"disableAllHooks": true}`` turns off the PreToolUse Bash gate for every
  later run in that project, and an ``env`` block injects variables into the
  agent's tools;
* ``CLAUDE.md``: loaded as standing instructions, so a developer agent could
  plant instructions for later tester and reviewer runs;
* ``.mcp.json``: adds MCP servers that start in the project directory.

``--setting-sources ""`` loads no settings file at all (the ``--settings``
file EQUIPA passes is a flag source and is always loaded, so the PreToolUse
gate still applies), and ``--strict-mcp-config`` limits MCP servers to the
ones EQUIPA passes with ``--mcp-config`` (none when it passes none).
Independent review IR-01 (task 3127 fix-forward, task 3134) reproduced the
project-settings gate bypass with the real CLI and confirmed these flags
close it.

The USER scope was loaded until task 3153. Without agent isolation it was
the operator's ``~/.claude``, which agents can write (RR3144-A): an ``env``
block (``SHELL``, ``BASH_FUNC_<name>%%``) or a user PreToolUse hook that
answers ``updatedInput`` ran a command the gate had judged harmless. Every
CLI run therefore gets its own empty, EQUIPA-owned ``CLAUDE_CONFIG_DIR``
(:func:`fresh_claude_config_dir`), so no user-scope CLAUDE.md or MCP servers
exist for it. That directory is still writable by the run (the CLI writes
its state there and the agent shares its UID), and the CLI re-read a
``settings.json`` written into it mid-run (R3150-01); with the empty
setting-sources value it no longer reads one. The CLI authenticates from
``CLAUDE_CODE_OAUTH_TOKEN`` in its environment, which needs nothing in the
config directory.

This module has no EQUIPA imports so standalone scripts (forgesmith) can use
it without pulling in the orchestrator.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence

_LOG = logging.getLogger(__name__)

# The setting sources a Claude CLI run started by EQUIPA may load: none.
# ``--setting-sources ""`` (the empty value as its own argv element) loads no
# file-based scope at all: no user, project or local settings.json. The
# ``--settings`` file EQUIPA passes is a flag source the CLI always loads, so
# the PreToolUse gate still applies. Loading ``user`` bought nothing once the
# per-run config directory is empty, and it kept the CLI's settings watcher
# alive: a settings.json the agent writes into its own config directory
# mid-run was hot-loaded and its hooks ran outside the gate (R3150-01).
ALLOWED_SETTING_SOURCES = ""
SETTING_SOURCES_FLAG = "--setting-sources"
STRICT_MCP_FLAG = "--strict-mcp-config"
CLAUDE_CLI_ISOLATION_ARGS: tuple[str, ...] = (
    SETTING_SOURCES_FLAG, ALLOWED_SETTING_SOURCES, STRICT_MCP_FLAG,
)


def is_claude_cli(executable: str) -> bool:
    """True when ``executable`` names the Claude Code CLI (any directory)."""
    name = os.path.basename(executable).lower()
    return name in ("claude", "claude.exe", "claude.cmd")


MCP_CONFIG_FLAG = "--mcp-config"
END_OF_OPTIONS = "--"


def _option_args(cmd: Sequence[str]) -> list[str]:
    """``cmd`` up to its first ``--``: after it every argument is prompt
    text to the CLI, never an option (RR-05)."""
    args = list(cmd)
    if END_OF_OPTIONS in args:
        return args[:args.index(END_OF_OPTIONS)]
    return args


def _setting_sources_values(cmd: Sequence[str]) -> list[str | None]:
    """Every value given to ``--setting-sources`` (both spellings) as an
    option, i.e. before ``--``. A flag with no value after it is None, never
    the (allowed) empty string."""
    options = _option_args(cmd)
    values: list[str | None] = []
    for index, arg in enumerate(options):
        if arg == SETTING_SOURCES_FLAG:
            values.append(options[index + 1] if index + 1 < len(options)
                          else None)
        elif arg.startswith(SETTING_SOURCES_FLAG + "="):
            values.append(arg.split("=", 1)[1])
    return values


def mcp_config_values(cmd: Sequence[str]) -> list[str]:
    """Every value the CLI reads as an MCP config (RR-05).

    ``--mcp-config`` is variadic: it takes every following argument up to
    the next option, each a file or inline JSON. ``--mcp-config=<value>``
    takes that one value. Arguments after ``--`` are prompt text.
    """
    options = _option_args(cmd)
    values: list[str] = []
    index = 0
    while index < len(options):
        arg = options[index]
        index += 1
        if arg.startswith(MCP_CONFIG_FLAG + "="):
            values.append(arg.split("=", 1)[1])
        elif arg == MCP_CONFIG_FLAG:
            while index < len(options) and not options[index].startswith("-"):
                values.append(options[index])
                index += 1
    return values


def isolate_claude_argv(cmd: Sequence[str]) -> list[str]:
    """``cmd`` with every file-based settings source, CLAUDE.md and
    .mcp.json disabled.

    Adds ``--setting-sources ""`` (the empty value as its own argument) and
    ``--strict-mcp-config`` when they are missing as options: at the end, or
    just before a ``--``, after which they would be prompt text (RR-05). An
    argv that already has them is returned unchanged (as a new list). The
    caller's own ``--mcp-config`` and ``--settings`` are kept: with the
    strict flag the former is the only MCP configuration the CLI reads, and
    the latter is a flag source the empty value does not switch off.

    Raises:
        ValueError: ``cmd`` already asks for a setting source (``user``,
            ``project``, ``local``) or gives the flag no value. Any of them
            re-opens a bypass: project settings live in the agent-writable
            cwd, and user settings in the agent-writable per-run config
            directory, which the CLI re-reads mid-run (R3150-01). Refused
            rather than silently rewritten.
    """
    isolated = list(cmd)
    sources = _setting_sources_values(isolated)
    widened = [value for value in sources if value != ALLOWED_SETTING_SOURCES]
    if widened:
        raise ValueError(
            f"Claude CLI argv asks for setting sources {widened!r}; EQUIPA "
            f"runs pass {SETTING_SOURCES_FLAG} {ALLOWED_SETTING_SOURCES!r} "
            f"(no settings files) because project settings in the "
            f"agent-writable cwd and user settings in the agent-writable "
            f"config directory can disable the Bash gate"
        )
    missing: list[str] = []
    if not sources:
        missing.extend([SETTING_SOURCES_FLAG, ALLOWED_SETTING_SOURCES])
    if STRICT_MCP_FLAG not in _option_args(isolated):
        missing.append(STRICT_MCP_FLAG)
    end_of_options = len(_option_args(isolated))
    isolated[end_of_options:end_of_options] = missing
    return isolated


def has_claude_cli_isolation(cmd: Sequence[str]) -> bool:
    """True when ``cmd`` loads no settings file (every ``--setting-sources``
    has the empty value) and strict MCP config, as options (before any
    ``--``)."""
    sources = _setting_sources_values(cmd)
    return (bool(sources)
            and all(value == ALLOWED_SETTING_SOURCES for value in sources)
            and STRICT_MCP_FLAG in _option_args(cmd))


# --- Per-run configuration directory and shell pin (RR3144-A) ---------------

CLAUDE_CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
CLAUDE_CODE_SHELL_VAR = "CLAUDE_CODE_SHELL"
RUN_CONFIG_DIR_PREFIX = "equipa-claude-config-"

# The shell the CLI's Bash tool must use. The CLI takes CLAUDE_CODE_SHELL
# before SHELL and accepts any executable whose name contains "bash" or "zsh",
# so an unpinned run used whatever SHELL user scope planted (RR3144-A (1)).
TRUSTED_BASH_CANDIDATES: tuple[str, ...] = ("/bin/bash", "/usr/bin/bash")

# Shell startup code and exported functions: never part of a CLI environment.
# BASH_ENV/ENV name a file every non-interactive shell sources, PROMPT_COMMAND
# runs before each prompt, SHELLOPTS/BASHOPTS can switch on xtrace (PS4 then
# runs code), and BASH_FUNC_<name>%% defines a function that replaces <name>
# in every bash the tool starts (RR3144-A (2)).
SHELL_INJECTION_ENV_NAMES: frozenset[str] = frozenset({
    "BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "PS4",
})
SHELL_FUNCTION_ENV_PREFIX = "BASH_FUNC_"

# Environment variables the CLI authenticates with on its own. Each needs
# nothing in CLAUDE_CONFIG_DIR, which is why a fresh, empty directory works.
CLI_ENV_AUTH_NAMES: tuple[str, ...] = (
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)


def _is_root_owned_and_not_shared_writable(path: str) -> bool:
    try:
        info = os.stat(path)
    except OSError:
        return False
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def trusted_bash(candidates: Sequence[str] = TRUSTED_BASH_CANDIDATES) -> str | None:
    """The first candidate that is a root-owned, executable regular file in a
    root-owned directory nobody else can write, or None.

    The candidate path itself is returned (``/bin/bash``), not its resolved
    target, so the pin reads as the conventional absolute path.
    """
    for candidate in candidates:
        if not os.path.isabs(candidate):
            continue
        resolved = os.path.realpath(candidate)
        try:
            info = os.stat(resolved)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if (_is_root_owned_and_not_shared_writable(resolved)
                and _is_root_owned_and_not_shared_writable(
                    os.path.dirname(resolved))):
            return candidate
    return None


def is_shell_injection_env_name(name: str) -> bool:
    """True for a variable that runs shell code before or inside the agent's
    commands (see SHELL_INJECTION_ENV_NAMES)."""
    return (name in SHELL_INJECTION_ENV_NAMES
            or name.startswith(SHELL_FUNCTION_ENV_PREFIX))


def has_env_auth(env: Mapping[str, str]) -> bool:
    """True when ``env`` holds a credential the CLI can authenticate with
    without reading anything from its config directory."""
    return any(env.get(name, "").strip() for name in CLI_ENV_AUTH_NAMES)


class RunConfigDirError(RuntimeError):
    """A per-run CLAUDE_CONFIG_DIR could not be created safely."""


def create_run_config_dir(parent: str | None = None) -> str:
    """Create an empty per-run CLAUDE_CONFIG_DIR (mode 0700) and return it.

    ``tempfile.mkdtemp`` creates it with O_EXCL semantics under ``parent``
    (default: the orchestrator's temp directory), so it never reuses a path
    someone planted. It is checked to be a real directory, owned by this
    process's user, with no group or other permission bits.

    It is seeded with nothing: the CLI authenticates from
    ``CLAUDE_CODE_OAUTH_TOKEN`` in its environment, and anything else in a
    config directory (settings.json, hooks, CLAUDE.md, .claude.json with MCP
    servers) is exactly what must not be loaded.

    The first call for a ``parent`` in this process also removes the stale
    directories crashed runs left there (:func:`sweep_stale_run_config_dirs`).

    Raises:
        RunConfigDirError: the directory could not be created, or is not
            what mkdtemp promised.
    """
    _sweep_parent_once(parent)
    try:
        path = tempfile.mkdtemp(prefix=RUN_CONFIG_DIR_PREFIX, dir=parent)
    except OSError as exc:
        raise RunConfigDirError(
            f"cannot create a per-run Claude config directory: {exc}") from exc
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        remove_run_config_dir(path)
        raise RunConfigDirError(
            f"per-run Claude config directory {path} is not a private "
            f"directory owned by this user")
    return path


def remove_run_config_dir(path: str) -> list[str]:
    """Remove a per-run config directory and everything the CLI wrote in it.

    Symlinks inside are removed, never followed, and a ``path`` that was
    replaced by a symlink is refused (``shutil.rmtree`` semantics). Returns a
    description of every entry that could not be removed (empty on success);
    the caller logs them. A directory left behind is never reused, because
    every run creates a new one.
    """
    errors: list[str] = []

    def _record(failed_path: str, exc: BaseException) -> None:
        if not isinstance(exc, FileNotFoundError):
            errors.append(f"{failed_path}: {exc}")

    if not os.path.lexists(path):
        return errors
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda _func, failed, exc: _record(failed, exc))
    else:
        shutil.rmtree(path, onerror=lambda _func, failed, exc_info: _record(
            failed, exc_info[1]))
    return errors


# A per-run directory outlives a process that is SIGKILLed, OOM-killed or
# exits with a run pending: neither _terminate_agent nor the finalizer or
# atexit hook runs (R3150-08). The first per-run directory a process creates
# in a parent directory therefore sweeps that parent once: EQUIPA-prefixed,
# real directories (never symlinks) owned by this user whose newest entry,
# anywhere in the tree, is older than this. No CLI run lasts anywhere near
# a day, so a live run's directory is never taken.
STALE_RUN_CONFIG_DIR_SECONDS = 24 * 3600
# _newest_mtime() looks at no more than this many entries, and this many
# levels, below a directory. A bigger tree is never judged stale: it stays
# rather than risk taking a live run's directory.
STALE_SCAN_MAX_ENTRIES = 10_000
STALE_SCAN_MAX_DEPTH = 16
_swept_parents: set[str] = set()
_sweep_lock = threading.Lock()


def _newest_mtime(path: str, top: os.stat_result) -> float:
    """The newest mtime of ``path`` and of everything below it (symlinks
    are not followed).

    The CLI writes into nested subdirectories of a live run's directory
    (``projects/<cwd>/<session>.jsonl``), which changes neither the
    directory's mtime nor those of its direct entries, so another process
    judging only the top level could sweep a live run (F-8 of the 3153
    review). The walk is bounded by STALE_SCAN_MAX_ENTRIES and
    STALE_SCAN_MAX_DEPTH; past either bound this returns ``math.inf`` and
    the directory is kept. A subdirectory that cannot be listed counts with
    its own mtime only.
    """
    newest = top.st_mtime
    pending = [(path, 0)]
    seen = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > STALE_SCAN_MAX_ENTRIES:
                        return math.inf
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, info.st_mtime)
                    if stat.S_ISDIR(info.st_mode):
                        if depth + 1 >= STALE_SCAN_MAX_DEPTH:
                            return math.inf
                        pending.append((entry.path, depth + 1))
        except OSError:
            continue
    return newest


def sweep_stale_run_config_dirs(
        parent: str | None = None,
        max_age_seconds: float = STALE_RUN_CONFIG_DIR_SECONDS,
        now: float | None = None) -> list[str]:
    """Remove per-run config directories a crashed process left behind.

    Looks only at entries of ``parent`` (default: the temp directory) whose
    name starts with :data:`RUN_CONFIG_DIR_PREFIX`, that are real
    directories (``lstat``: a symlink is never followed or removed), owned by
    this process's user, and whose newest entry anywhere below is older than
    ``max_age_seconds``. Everything else, including another user's
    directory and a fresh one, is left alone. Entries that cannot be removed
    are logged and skipped.

    Returns:
        The paths that were removed completely.

    Raises:
        ValueError: ``max_age_seconds`` is not positive.
    """
    if max_age_seconds <= 0:
        raise ValueError(
            f"max_age_seconds must be positive, got {max_age_seconds!r}")
    root = parent if parent is not None else tempfile.gettempdir()
    cutoff = (time.time() if now is None else now) - max_age_seconds
    uid = os.getuid()
    removed: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        _LOG.warning("cannot list %s for stale Claude config directories: %s",
                     root, exc)
        return removed
    for entry in entries:
        if not entry.name.startswith(RUN_CONFIG_DIR_PREFIX):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
            continue
        if _newest_mtime(entry.path, info) > cutoff:
            continue
        errors = remove_run_config_dir(entry.path)
        if errors:
            _LOG.warning("stale Claude config directory %s not fully "
                         "removed: %s", entry.path, "; ".join(errors[:5]))
        else:
            removed.append(entry.path)
    return removed


def _sweep_parent_once(parent: str | None) -> None:
    """Run :func:`sweep_stale_run_config_dirs` on ``parent`` the first time
    this process creates a per-run directory there."""
    root = os.path.realpath(parent if parent is not None
                            else tempfile.gettempdir())
    with _sweep_lock:
        if root in _swept_parents:
            return
        _swept_parents.add(root)
    sweep_stale_run_config_dirs(root)


@contextlib.contextmanager
def fresh_claude_config_dir(parent: str | None = None) -> Iterator[str]:
    """A per-run CLAUDE_CONFIG_DIR that exists only inside the ``with`` block.

    Removed on exit, also when the body raises. Removal errors are not
    raised (the run's own outcome matters more); use
    :func:`remove_run_config_dir` directly to see them.
    """
    path = create_run_config_dir(parent)
    try:
        yield path
    finally:
        remove_run_config_dir(path)


def claude_cli_env(env: Mapping[str, str], config_dir: str,
                   shell: str | None = None) -> dict[str, str]:
    """The environment for one Claude CLI run.

    ``env`` (already allowlisted) with ``CLAUDE_CONFIG_DIR`` pointing at the
    run's own directory, ``CLAUDE_CODE_SHELL`` pinned to ``shell`` (default:
    :func:`trusted_bash`; left out when no trusted bash exists) and every
    shell-injection name removed (BASH_ENV, ENV, PROMPT_COMMAND, SHELLOPTS,
    BASHOPTS, PS4, BASH_FUNC_*).

    Raises:
        ValueError: ``config_dir`` or ``shell`` is not an absolute path.
    """
    if not os.path.isabs(config_dir):
        raise ValueError(
            f"CLAUDE_CONFIG_DIR must be absolute, got {config_dir!r}")
    cli_env = claude_cli_shell_env(env, shell)
    cli_env[CLAUDE_CONFIG_DIR_VAR] = config_dir
    return cli_env


def claude_cli_shell_env(env: Mapping[str, str],
                         shell: str | None = None) -> dict[str, str]:
    """``env`` with the shell pinned and the shell-injection names removed.

    The part of :func:`claude_cli_env` that does not depend on the config
    directory, for a run whose CLAUDE_CONFIG_DIR is set elsewhere (an
    isolated unit gets its own from the launcher). ``CLAUDE_CODE_SHELL`` is
    ``shell`` (default: :func:`trusted_bash`; left out when no trusted bash
    exists) and BASH_ENV, ENV, PROMPT_COMMAND, SHELLOPTS, BASHOPTS, PS4 and
    every BASH_FUNC_* name are dropped.

    Raises:
        ValueError: ``shell`` is not an absolute path.
    """
    pinned_shell = trusted_bash() if shell is None else shell
    if pinned_shell is not None and not os.path.isabs(pinned_shell):
        raise ValueError(
            f"CLAUDE_CODE_SHELL must be absolute, got {pinned_shell!r}")
    cli_env = {name: value for name, value in env.items()
               if not is_shell_injection_env_name(name)}
    if pinned_shell is None:
        cli_env.pop(CLAUDE_CODE_SHELL_VAR, None)
    else:
        cli_env[CLAUDE_CODE_SHELL_VAR] = pinned_shell
    return cli_env


@contextlib.contextmanager
def claude_cli_run_env(env: Mapping[str, str] | None = None,
                       parent: str | None = None) -> Iterator[dict[str, str]]:
    """The environment for one ``claude`` run started with subprocess.run.

    For the callers that do not go through the agent runner (ForgeSmith
    GHOST/OPRO, SIMBA, the autoresearch mutation): ``env`` (default: this
    process's environment) through :func:`claude_cli_env` with a fresh
    per-run config directory that exists only inside the ``with`` block.

    Raises:
        RunConfigDirError: the per-run directory could not be created.
    """
    with fresh_claude_config_dir(parent) as config_dir:
        yield claude_cli_env(os.environ if env is None else env, config_dir)


# The same per-run directory and shell pin for a ``claude`` command run as a
# shell command on another host (the autoresearch SSH path). mktemp -d makes
# a new 0700 directory, the EXIT trap removes it, and env -u drops the shell
# startup names before the CLI starts. Prepend it to the claude command; a
# trailing ``< file`` still binds to the claude command.
REMOTE_RUN_CONFIG_DIR_PREFIX = (
    'cfg=$(mktemp -d -t ' + RUN_CONFIG_DIR_PREFIX + 'XXXXXXXXXX) || exit 1; '
    'trap \'rm -rf -- "$cfg"\' EXIT; '
    'test -x /bin/bash || exit 1; '
    'env -u BASH_ENV -u ENV -u PROMPT_COMMAND -u SHELLOPTS -u BASHOPTS '
    '-u PS4 CLAUDE_CONFIG_DIR="$cfg" CLAUDE_CODE_SHELL=/bin/bash '
)
