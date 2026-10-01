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

``--setting-sources user`` loads user settings only (the ``--settings`` file
EQUIPA passes is a flag source and is always loaded), and
``--strict-mcp-config`` limits MCP servers to the ones EQUIPA passes with
``--mcp-config`` (none when it passes none). Independent review IR-01 (task
3127 fix-forward, task 3134) reproduced the gate bypass with the real CLI and
confirmed these two flags close it.

``--setting-sources user`` still loads the USER scope, and without agent
isolation that is the operator's ``~/.claude``, which agents can write
(RR3144-A): an ``env`` block (``SHELL``, ``BASH_FUNC_<name>%%``) or a user
PreToolUse hook that answers ``updatedInput`` ran a command the gate had
judged harmless. Every CLI run therefore gets its own empty, EQUIPA-owned
``CLAUDE_CONFIG_DIR`` (:func:`fresh_claude_config_dir`), so no user-scope
settings, hooks, env blocks, CLAUDE.md or MCP servers exist for it. The CLI
authenticates from ``CLAUDE_CODE_OAUTH_TOKEN`` in its environment, which
needs nothing in the config directory.

This module has no EQUIPA imports so standalone scripts (forgesmith) can use
it without pulling in the orchestrator.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence

# The only setting source a Claude CLI run started by EQUIPA may load.
ALLOWED_SETTING_SOURCES = "user"
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


def _setting_sources_values(cmd: Sequence[str]) -> list[str]:
    """Every value given to ``--setting-sources`` (both spellings) as an
    option, i.e. before ``--``."""
    options = _option_args(cmd)
    values: list[str] = []
    for index, arg in enumerate(options):
        if arg == SETTING_SOURCES_FLAG:
            values.append(options[index + 1] if index + 1 < len(options) else "")
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
    """``cmd`` with project-scope settings, CLAUDE.md and .mcp.json disabled.

    Adds ``--setting-sources user`` and ``--strict-mcp-config`` when they
    are missing as options: at the end, or just before a ``--``, after which
    they would be prompt text (RR-05). An argv that already has them is
    returned unchanged (as a new list). The caller's own ``--mcp-config`` is
    kept: with the strict flag it is the only MCP configuration the CLI
    reads.

    Raises:
        ValueError: ``cmd`` already asks for a setting source other than
            ``user`` (for example ``project``), which would re-open the
            bypass. Refused rather than silently rewritten.
    """
    isolated = list(cmd)
    sources = _setting_sources_values(isolated)
    widened = [value for value in sources if value != ALLOWED_SETTING_SOURCES]
    if widened:
        raise ValueError(
            f"Claude CLI argv asks for setting sources {widened!r}; EQUIPA "
            f"runs may load only {ALLOWED_SETTING_SOURCES!r} because project "
            f"settings in the agent-writable cwd can disable the Bash gate"
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
    """True when ``cmd`` loads user settings only and strict MCP config, as
    options (before any ``--``)."""
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

    Raises:
        RunConfigDirError: the directory could not be created, or is not
            what mkdtemp promised.
    """
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
