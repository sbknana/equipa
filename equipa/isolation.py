"""Agent isolation: a separate no-sudo user, a per-agent cgroup and clone.

Copyright 2026 Forgeborn

Feature flag ``agent_isolation`` (default off). Design, threat model and the
operator runbook are in docs/AGENT_ISOLATION.md. With the flag on, every agent
CLI that ``agent_runner._spawn_agent_process`` starts goes through
:func:`spawn_isolated_agent` instead of the same-UID launcher:

* ``systemd-run --user --scope`` puts the agent in a transient scope of the
  orchestrator's user manager with ``TasksMax`` (pids.max), ``MemoryMax`` and
  ``CPUWeight``. The scope's cgroup belongs to the orchestrator, so the
  orchestrator kills the whole agent with ``cgroup.kill``; the agent, running
  as another UID, can neither leave the cgroup nor change its limits, and
  cannot signal the orchestrator.
* Inside the scope ``sudo -n -u <agent_user>`` runs ``agent_launcher.py
  --isolated`` with exactly those arguments, which is all the sudoers rule
  allows (:func:`sudoers_snippet`). Everything else goes over the launcher's
  stdin as a handoff: the CLI argv, the environment (only allowlisted names
  plus CLAUDE_CODE_OAUTH_TOKEN), the prompt/settings/MCP file contents and a
  git bundle of the task worktree. The rest of stdin is the stop channel.
* Per-agent clone: the agent never touches the orchestrator's worktree or
  object store. The launcher clones the handoff bundle into the agent's HOME,
  and when the CLI exits it writes the result as a bundle to the exchange
  directory. The orchestrator copies that untrusted file, fetches its single
  state commit with fsck on, moves only the task branch (compare-and-swap
  against the dispatch base) and restores the working-tree state.
* The agent's TheForge MCP server reads a snapshot of the database without
  the excluded tables (api_keys), made read-only by file permissions; every
  other MCP server is dropped unless allowlisted, and an allowlisted one may
  not carry credential-like environment variables.

While the flag is on, anything that cannot be established or verified
refuses the dispatch with :class:`AgentIsolationError`; there is no fallback
to the unisolated launcher.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import contextlib
import contextvars
import copy
import json
import logging
import os
import posixpath
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from equipa import agent_launcher
from equipa.config import (
    CONFIG_LOAD_ERROR_KEY,
    get_active_dispatch_config,
    is_feature_enabled,
)
from equipa.constants import MCP_CONFIG, THEFORGE_DB
from equipa.env_loader import _API_BILLING_PREFIXES, _looks_like_credential
from equipa.git_ops import GIT_HARDENING_ARGS, GIT_HARDENING_ENV, git_run

logger = logging.getLogger(__name__)

FEATURE_FLAG = "agent_isolation"
CONFIG_KEY = "agent_isolation"
OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
UNIT_PREFIX = "equipa-agent-"
CGROUP_ROOT = Path("/sys/fs/cgroup")
RUNTIME_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

# Groups that are root-equivalent or expose other users' data. The agent
# user must be in none of them (checked here and again inside the launcher).
DEFAULT_PRIVILEGED_GROUPS: tuple[str, ...] = (
    "root", "sudo", "admin", "wheel", "adm", "docker", "lxd", "incus",
    "libvirt", "kvm", "disk", "shadow", "systemd-journal",
)
# Credential stores in the orchestrator's HOME the agent must not read.
ORCHESTRATOR_HOME_SECRETS: tuple[str, ...] = (
    ".claude", ".claude.json", ".config", ".gitconfig", ".git-credentials",
    ".ssh", ".netrc", ".pgpass", ".aws", ".docker", ".gnupg", ".bash_history",
)
# CLI options whose value is a file the orchestrator wrote for the agent.
# Those files are private to the orchestrator, so their CONTENT is handed
# over and the launcher writes an agent-owned copy.
HANDOFF_FILE_OPTIONS: Mapping[str, str] = {
    "--append-system-prompt-file": "system-prompt.md",
    "--system-prompt-file": "system-prompt-full.md",
    "--settings": "settings.json",
    "--mcp-config": "mcp-config.json",
}
# Environment names the launcher sets for the agent user itself; the
# orchestrator's values point into its own HOME and runtime directory.
_AGENT_SIDE_ENV = frozenset({
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "PWD",
    "OLDPWD", "MAIL", "CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL",
})
_PAGE_SIZE = 4096
_HANDOFF_FILE_MAX_BYTES = 32 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 120
_BUNDLE_TIMEOUT_SECONDS = 900
_SCOPE_TIMEOUT_SECONDS = 15.0
_CGROUP_KILL_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.05
_LEFTOVER_POLL_SECONDS = 0.25
_LAUNCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEMORY_RE = re.compile(r"^\s*(\d+)\s*([KMGT]?)\s*$", re.IGNORECASE)
_MEMORY_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                 "T": 1024 ** 4}
_UNIT_NAME_RE = re.compile(
    rf"^{re.escape(UNIT_PREFIX)}(\d+)-(\d+)-([0-9a-f]+)\.scope$")
# Characters that end a sudoers command token or need escaping there.
_SUDOERS_UNSAFE = frozenset(' \t\n,:=\\"#!*?[]()')
# The orchestrator writes review artifacts here (loops.ARTIFACTS_DIR_NAME);
# always protected on import, whatever carry_ignored_paths says.
ARTIFACTS_DIR = ".equipa-artifacts"
_SYMLINK_MODE = b"120000"
_MAX_CHANGED_SYMLINKS = 1000
# Symbolic links in one exported tree; more refuses the export unread.
_MAX_TREE_SYMLINKS = 10_000
# Longest link target an export may carry: the kernel refuses to create a
# longer one (PATH_MAX - 1), and it bounds the walk below (review R3140-01).
_MAX_LINK_TARGET_BYTES = 4095
# Path components one import check may walk over all links of a commit.
_MAX_LINK_WALK_STEPS = 1_000_000
# Symbolic links followed while resolving one imported link (MAXSYMLINKS).
_MAX_LINK_HOPS = 40
# How the commits of an export (state, parents) are named in refusals.
_EXPORT_COMMIT_NAMES = ("", " (committed task-branch tip)",
                        " (the clone's HEAD commit)")


class AgentIsolationError(RuntimeError):
    """Isolation could not be established or verified; no agent was run."""


# --- Settings -------------------------------------------------------------------


@dataclass(frozen=True)
class IsolationSettings:
    """Validated ``dispatch_config["agent_isolation"]`` (see _DEFAULTS)."""

    agent_user: str
    python: str
    launcher: str
    claude_executable: str
    git_executable: str
    sudo: str
    systemd_run: str
    exchange_dir: str
    view_db_path: str | None
    pids_max: int
    memory_max_bytes: int
    cpu_weight: int
    setup_timeout_sec: float
    stop_timeout_sec: float
    max_export_bytes: int
    forge_mcp_server: str
    allowed_mcp_servers: tuple[str, ...]
    exclude_tables: tuple[str, ...]
    deny_read: tuple[str, ...]
    deny_write: tuple[str, ...]
    privileged_groups: tuple[str, ...]
    oauth_token_file: str | None
    carry_ignored_paths: tuple[str, ...]
    db_backup_dirs: tuple[str, ...] = ()
    secret_scan_roots: tuple[str, ...] = ()
    unit_wait_timeout_sec: float = 21600.0


_DEFAULTS: dict[str, Any] = {
    "agent_user": "equipa-agent",
    "python": "/usr/bin/python3",
    "launcher": str(agent_launcher.LAUNCHER_PATH),
    "claude_executable": "/usr/local/bin/claude",
    "git_executable": "/usr/bin/git",
    "sudo": "/usr/bin/sudo",
    "systemd_run": "/usr/bin/systemd-run",
    "exchange_dir": None,
    "view_db_path": None,
    "pids_max": 512,
    "memory_max": "4G",
    "cpu_weight": 100,
    "setup_timeout_sec": 300,
    "stop_timeout_sec": 120,
    "max_export_bytes": 2 * 1024 ** 3,
    "forge_mcp_server": "theforge",
    "allowed_mcp_servers": ["theforge"],
    "exclude_tables": ["api_keys"],
    "deny_read": [],
    "deny_write": [],
    "privileged_groups": list(DEFAULT_PRIVILEGED_GROUPS),
    "oauth_token_file": None,
    "carry_ignored_paths": [".equipa-artifacts"],
    # Directories holding TheForge backups or other copies of the database.
    # Like the live database's own directory, the agent may neither list
    # nor enter them (review ISO-03).
    "db_backup_dirs": [],
    # Directories holding project checkouts; the verify script fails if the
    # agent user can read a secret-shaped file below them (review ISO-05).
    "secret_scan_roots": [],
    # How long a unit may wait for its turn: a reviewer until no other
    # isolated agent runs, any other unit while a reviewer runs or waits
    # (review R3136-03). Past it the dispatch is refused.
    "unit_wait_timeout_sec": 21600,
}


def _setting_error(key: str, problem: str) -> AgentIsolationError:
    return AgentIsolationError(
        f"{CONFIG_KEY}.{key} {problem} (see docs/AGENT_ISOLATION.md)")


def _abs_path(raw: Mapping[str, Any], key: str, *, optional: bool = False,
              sudoers: bool = False) -> str | None:
    value = raw.get(key, _DEFAULTS[key])
    if value is None and optional:
        return None
    if not isinstance(value, str) or not os.path.isabs(value):
        raise _setting_error(key, f"must be an absolute path, got {value!r}")
    if sudoers and any(char in _SUDOERS_UNSAFE for char in value):
        raise _setting_error(key, "must not contain spaces or sudoers "
                                  "special characters")
    return os.path.normpath(value)


def _int_in_range(raw: Mapping[str, Any], key: str, low: int, high: int) -> int:
    value = raw.get(key, _DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) \
            or not low <= value <= high:
        raise _setting_error(key, f"must be an integer from {low} to {high}, "
                                  f"got {value!r}")
    return value


def _str_tuple(raw: Mapping[str, Any], key: str,
               pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    value = raw.get(key, _DEFAULTS[key])
    if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value):
        raise _setting_error(key, f"must be a list of non-empty strings, got "
                                  f"{value!r}")
    if pattern is not None:
        bad = [item for item in value if not pattern.match(item)]
        if bad:
            raise _setting_error(key, f"has invalid entries {bad!r}")
    return tuple(value)


def parse_memory(value: object) -> int:
    """Bytes for an int or a string like ``"4G"``/``"512M"`` (binary units),
    rounded down to whole pages because that is what the kernel stores."""
    if isinstance(value, bool):
        raise ValueError(f"invalid memory size {value!r}")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and (match := _MEMORY_RE.match(value)):
        amount = int(match[1]) * _MEMORY_UNITS[match[2].upper()]
    else:
        raise ValueError(f"invalid memory size {value!r}")
    amount -= amount % _PAGE_SIZE
    if amount < 64 * 1024 ** 2:
        raise ValueError(f"memory size {value!r} is below 64M")
    return amount


def load_isolation_settings(dispatch_config: Mapping[str, Any] | None
                            ) -> IsolationSettings:
    """Validate the ``agent_isolation`` section; unknown keys are refused so a
    typo cannot silently fall back to a default."""
    raw: Any = {}
    if isinstance(dispatch_config, Mapping):
        raw = dispatch_config.get(CONFIG_KEY) or {}
    if not isinstance(raw, Mapping):
        raise AgentIsolationError(f"{CONFIG_KEY} must be a JSON object")
    unknown = sorted(key for key in raw
                     if key not in _DEFAULTS and not str(key).startswith("_"))
    if unknown:
        raise AgentIsolationError(f"unknown {CONFIG_KEY} settings {unknown}")
    agent_user = raw.get("agent_user", _DEFAULTS["agent_user"])
    if not isinstance(agent_user, str) or not _USER_NAME_RE.match(agent_user) \
            or agent_user == "root":
        raise _setting_error("agent_user", f"is not a valid unprivileged user "
                                           f"name: {agent_user!r}")
    exchange_dir = _abs_path(raw, "exchange_dir", optional=True)
    if exchange_dir is None:
        raise _setting_error("exchange_dir", "is required")
    try:
        memory = parse_memory(raw.get("memory_max", _DEFAULTS["memory_max"]))
    except ValueError as exc:
        raise _setting_error("memory_max", str(exc)) from exc
    carry = _str_tuple(raw, "carry_ignored_paths")
    if any(os.path.isabs(item) or ".." in Path(item).parts for item in carry):
        raise _setting_error("carry_ignored_paths", "must hold relative paths "
                                                    "inside the worktree")
    deny_read = _str_tuple(raw, "deny_read") if raw.get("deny_read") else ()
    deny_write = _str_tuple(raw, "deny_write") if raw.get("deny_write") else ()
    if not all(os.path.isabs(item) for item in (*deny_read, *deny_write)):
        raise _setting_error("deny_read/deny_write", "must hold absolute paths")
    path_lists = {key: (_str_tuple(raw, key) if raw.get(key) else ())
                  for key in ("db_backup_dirs", "secret_scan_roots")}
    for key, paths in path_lists.items():
        if not all(os.path.isabs(item) for item in paths):
            raise _setting_error(key, "must hold absolute paths")
    return IsolationSettings(
        agent_user=agent_user,
        python=_abs_path(raw, "python", sudoers=True),
        launcher=_abs_path(raw, "launcher", sudoers=True),
        claude_executable=_abs_path(raw, "claude_executable"),
        git_executable=_abs_path(raw, "git_executable"),
        sudo=_abs_path(raw, "sudo"),
        systemd_run=_abs_path(raw, "systemd_run"),
        exchange_dir=exchange_dir,
        view_db_path=_abs_path(raw, "view_db_path", optional=True),
        pids_max=_int_in_range(raw, "pids_max", 16, 1_000_000),
        memory_max_bytes=memory,
        cpu_weight=_int_in_range(raw, "cpu_weight", 1, 10_000),
        setup_timeout_sec=float(_int_in_range(raw, "setup_timeout_sec", 5, 3600)),
        stop_timeout_sec=float(_int_in_range(raw, "stop_timeout_sec", 5, 3600)),
        max_export_bytes=_int_in_range(raw, "max_export_bytes", 1024 ** 2,
                                       64 * 1024 ** 3),
        forge_mcp_server=str(raw.get("forge_mcp_server",
                                     _DEFAULTS["forge_mcp_server"])),
        allowed_mcp_servers=_str_tuple(raw, "allowed_mcp_servers"),
        exclude_tables=_str_tuple(raw, "exclude_tables", _TABLE_NAME_RE),
        deny_read=tuple(os.path.normpath(p) for p in deny_read),
        deny_write=tuple(os.path.normpath(p) for p in deny_write),
        privileged_groups=_str_tuple(raw, "privileged_groups"),
        oauth_token_file=_abs_path(raw, "oauth_token_file", optional=True),
        carry_ignored_paths=carry,
        db_backup_dirs=tuple(os.path.normpath(p)
                             for p in path_lists["db_backup_dirs"]),
        secret_scan_roots=tuple(os.path.normpath(p)
                                for p in path_lists["secret_scan_roots"]),
        unit_wait_timeout_sec=float(_int_in_range(
            raw, "unit_wait_timeout_sec", 60, 7 * 86400)),
    )


# While this file exists, agent isolation is REQUIRED on this host (review
# F1). The operator creates it as root, outside every repository and every
# dispatch config, so neither a per-run config, a mistyped config path nor
# an agent can switch isolation off. Its content is ignored.
REQUIRED_MARKER = Path("/etc/equipa/require-agent-isolation")


def isolation_required() -> bool:
    """True while the operator's :data:`REQUIRED_MARKER` exists.

    A marker that cannot be checked (a directory the orchestrator may not
    search) counts as present: the check fails closed.
    """
    try:
        os.lstat(REQUIRED_MARKER)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        logger.error("[Isolation] cannot check %s (%s); agent isolation is "
                     "treated as required", REQUIRED_MARKER, exc)
    return True


def isolation_enabled(dispatch_config: Mapping[str, Any] | None = None) -> bool:
    """Is ``features.agent_isolation`` on for the active dispatch config?

    The flag is fail-closed (config.FAIL_CLOSED_FEATURE_FLAGS): an
    unreadable config or an invalid value turns it ON, and the dispatch is
    then refused unless isolation is actually configured. While the host
    requires isolation (:func:`isolation_required`) it is on whatever the
    config says; :func:`isolation_requirement_refusal` then refuses a
    config that turns it off.
    """
    if isolation_required():
        return True
    if dispatch_config is None:
        try:
            dispatch_config = get_active_dispatch_config()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            dispatch_config = {CONFIG_LOAD_ERROR_KEY: f"{type(exc).__name__}: {exc}"}
    return is_feature_enabled(dispatch_config, FEATURE_FLAG)  # type: ignore[arg-type]


def isolation_requirement_refusal(
        dispatch_config: Mapping[str, Any] | None) -> str | None:
    """Why a dispatch with ``dispatch_config`` must not run, or None.

    While the host requires isolation, a config whose own
    ``features.agent_isolation`` is off (left out, false, or a config file
    that does not exist) is refused with this message rather than run with
    some other notion of the flag (review F1). A config that could not be
    read reads the flag as ON (fail-closed), so it is not refused here; its
    missing settings refuse the spawn instead.
    """
    if not isolation_required():
        return None
    if isinstance(dispatch_config, Mapping) and is_feature_enabled(
            dict(dispatch_config), FEATURE_FLAG):
        return None
    return (f"agent isolation is required on this host ({REQUIRED_MARKER} "
            f"exists) but the dispatch config turns features.{FEATURE_FLAG} "
            f"off; set it to true with its {CONFIG_KEY} section, or remove "
            f"the marker as root (docs/AGENT_ISOLATION.md)")


def describe_isolation_state(dispatch_config: Mapping[str, Any] | None) -> str:
    """One line for the orchestrator's startup log (review F1)."""
    if isolation_required():
        return f"agent_isolation: ON (required by {REQUIRED_MARKER})"
    if isolation_enabled(dispatch_config):
        return "agent_isolation: ON"
    return "agent_isolation: OFF"


def unisolated_spawn_refusal(
        purpose: str,
        remedy: str = "turn off rlm_decompose or agent_isolation",
        *, action: str = "start the Claude CLI") -> str | None:
    """Why ``purpose`` must not start the CLI itself, or None.

    Helpers that run ``claude`` directly (RLM decomposition, the ForgeSmith
    GHOST scout, OPRO and SIMBA) would run it as the orchestrator's user,
    outside the agent cgroup, on text agents wrote (reviews CT-04, ISO-06).
    The Ollama provider runs the model's shell commands in-process the same
    way (review R3136-01); ``action`` names what the path would do. With
    isolation on they refuse instead.
    """
    if not isolation_enabled():
        return None
    return (f"{purpose} refused: agent_isolation is on and this path would "
            f"{action} as the orchestrator's user outside the "
            f"agent sandbox; {remedy}")


# What the Ollama provider would do unsandboxed, and how to avoid the refusal.
OLLAMA_REFUSAL_ACTION = "run the model's shell commands"
OLLAMA_REFUSAL_REMEDY = ("use the Claude provider for every role, or turn off "
                         "agent_isolation")


def worktree_execution_refusal(action: str) -> str | None:
    """Why the orchestrator must not run ``action`` in a project directory,
    or None when agent_isolation is off.

    With isolation on, the task worktree holds work imported from an
    isolated agent. Install and build commands execute that content
    (package.json scripts, build backends, MSBuild targets, a tracked
    ``node_modules/.bin/tsc``), so running them as the orchestrator would run
    agent-authored code with the orchestrator's user and sudo, outside the
    sandbox (review ISO-01). They are refused rather than run in a scope: the
    agent's clone has no gitignored dependency trees, so an isolated build
    check would report every such project as broken.
    """
    if not isolation_enabled():
        return None
    return (f"{action} refused: agent_isolation is on and it would execute "
            f"agent-written project files as the orchestrator's user")


# --- Commands the operator installs and the orchestrator runs -------------------


def sudoers_snippet(settings: IsolationSettings, orchestrator_user: str) -> str:
    """The exact /etc/sudoers.d entry: the orchestrator user may run ONLY the
    launcher in isolated mode, ONLY as the agent user, with no other
    arguments. ``!pam_session`` keeps PAM session modules (pam_systemd) from
    moving the agent out of its scope; ``!use_pty`` keeps stdout a pipe."""
    if not _USER_NAME_RE.match(orchestrator_user):
        raise AgentIsolationError(f"invalid orchestrator user "
                                  f"{orchestrator_user!r}")
    command = f"{settings.python} -I {settings.launcher} --isolated"
    return (
        f"Cmnd_Alias EQUIPA_AGENT_LAUNCH = {command}\n"
        f"Defaults!EQUIPA_AGENT_LAUNCH !use_pty, !pam_session, env_reset, "
        f"!log_output\n"
        f"{orchestrator_user} ALL=({settings.agent_user}) NOPASSWD: "
        f"EQUIPA_AGENT_LAUNCH\n"
    )


def make_unit_name(pid: int | None = None, start_time: int | None = None,
                   token: str | None = None) -> str:
    """Scope name ``equipa-agent-<orchestrator pid>-<its start time>-<hex>``.

    The owner's identity in the name lets :func:`sweep_stale_scopes` find
    scopes whose orchestrator is gone.
    """
    pid = os.getpid() if pid is None else pid
    if start_time is None:
        start_time = agent_launcher.proc_start_time(pid) or 0
    token = secrets.token_hex(8) if token is None else token
    return f"{UNIT_PREFIX}{pid}-{start_time}-{token}"


def build_launch_command(settings: IsolationSettings, unit: str) -> list[str]:
    """argv that starts the launcher as the agent user in its own scope."""
    return [
        settings.systemd_run, "--user", "--scope", "--quiet", "--collect",
        f"--unit={unit}",
        f"--property=TasksMax={settings.pids_max}",
        f"--property=MemoryMax={settings.memory_max_bytes}",
        f"--property=CPUWeight={settings.cpu_weight}",
        "--",
        settings.sudo, "-n", "-u", settings.agent_user, "--",
        settings.python, "-I", settings.launcher,
        *agent_launcher.ISOLATED_ARGV,
    ]


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


# Parent of the per-user systemd runtime directories (logind creates
# /run/user/<uid> for the user alone).
_USER_RUNTIME_ROOT = "/run/user"


def _unit_lock_dir() -> str:
    """Directory of the unit locks: the orchestrator user's systemd runtime
    directory, whatever ``XDG_RUNTIME_DIR`` says.

    The variable is only inherited. An orchestrator process started with
    another value (a stale tmux or ``sudo`` shell) would lock files no
    other orchestrator process of the user sees, and its units would run
    beside a reviewer (review R3136-03).
    """
    return os.path.join(_USER_RUNTIME_ROOT, str(os.getuid()))


def launch_environment() -> dict[str, str]:
    """Environment for systemd-run and sudo: only what reaching the user
    manager needs. sudo resets it anyway; the agent's own environment
    travels in the handoff."""
    env = {"PATH": _LAUNCH_PATH, "LANG": os.environ.get("LANG", "C.UTF-8"),
           "XDG_RUNTIME_DIR": _runtime_dir()}
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    return env


# --- Identity and host checks (orchestrator side) -------------------------------


@dataclass(frozen=True)
class AgentIdentity:
    uid: int
    gid: int
    home: str


def resolve_agent_identity(settings: IsolationSettings) -> AgentIdentity:
    """The agent user's passwd entry, refusing root, the orchestrator's own
    user and membership of any privileged group."""
    import grp
    import pwd

    try:
        entry = pwd.getpwnam(settings.agent_user)
    except KeyError as exc:
        raise AgentIsolationError(
            f"agent user {settings.agent_user!r} does not exist; create it "
            f"as described in docs/AGENT_ISOLATION.md") from exc
    if entry.pw_uid == 0:
        raise AgentIsolationError(f"agent user {settings.agent_user!r} is root")
    if entry.pw_uid == os.getuid():
        raise AgentIsolationError(
            f"agent user {settings.agent_user!r} is the orchestrator's own "
            f"user; isolation needs a separate account")
    for name in settings.privileged_groups:
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        if settings.agent_user in group.gr_mem or group.gr_gid == entry.pw_gid:
            raise AgentIsolationError(
                f"agent user {settings.agent_user!r} is in the privileged "
                f"group {name!r}; remove it (gpasswd -d)")
    return AgentIdentity(uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)


def check_host(settings: IsolationSettings, identity: AgentIdentity) -> None:
    """Static prerequisites; the launcher re-checks the rest from inside."""
    if not sys.platform.startswith("linux"):
        raise AgentIsolationError("agent_isolation is Linux-only")
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        raise AgentIsolationError(f"cgroup v2 is not mounted at {CGROUP_ROOT}")
    runtime_dir = Path(_runtime_dir())
    if not ((runtime_dir / "systemd" / "private").exists()
            or (runtime_dir / "bus").exists()):
        raise AgentIsolationError(
            f"no systemd user manager for the orchestrator user at "
            f"{runtime_dir}; enable lingering (loginctl enable-linger)")
    for key in ("sudo", "systemd_run", "python", "claude_executable",
                "git_executable"):
        path = getattr(settings, key)
        if not os.path.isfile(path):
            raise AgentIsolationError(f"{CONFIG_KEY}.{key} {path} does not exist")
    if not os.path.isfile(settings.launcher):
        raise AgentIsolationError(f"launcher {settings.launcher} does not exist")
    try:
        info = os.lstat(settings.exchange_dir)
    except OSError as exc:
        raise AgentIsolationError(
            f"exchange directory {settings.exchange_dir} is unusable: {exc}"
        ) from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != identity.uid
            or info.st_mode & stat.S_IWOTH):
        raise AgentIsolationError(
            f"exchange directory {settings.exchange_dir} must be a directory "
            f"owned by {settings.agent_user} and not world-writable")
    if not os.access(settings.exchange_dir, os.X_OK):
        raise AgentIsolationError(
            f"the orchestrator cannot enter {settings.exchange_dir}; use mode "
            f"0711 or 0755")
    if settings.view_db_path is not None:
        view_dir = os.path.dirname(settings.view_db_path)
        if not os.path.isdir(view_dir) or not os.access(view_dir, os.W_OK):
            raise AgentIsolationError(
                f"the orchestrator cannot write the view directory {view_dir}")


def resolve_oauth_token(settings: IsolationSettings,
                        environ: Mapping[str, str] | None = None) -> str:
    """The one credential the agent gets. The agent user cannot read the
    orchestrator's ~/.claude, so the token has to be handed over."""
    source = os.environ if environ is None else environ
    token = source.get(OAUTH_TOKEN_VAR, "").strip()
    if not token and settings.oauth_token_file:
        try:
            fd = os.open(settings.oauth_token_file, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise AgentIsolationError(
                f"cannot read {CONFIG_KEY}.oauth_token_file: {exc}") from exc
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            if os.fstat(handle.fileno()).st_mode & 0o077:
                raise AgentIsolationError(
                    f"{settings.oauth_token_file} must be readable by its "
                    f"owner only (chmod 600)")
            token = handle.read(4096).strip()
    if not token:
        raise AgentIsolationError(
            f"{OAUTH_TOKEN_VAR} is not set and no {CONFIG_KEY}.oauth_token_file "
            f"is configured; create one with `claude setup-token`")
    if any(char in token for char in "\r\n\0 "):
        raise AgentIsolationError(f"{OAUTH_TOKEN_VAR} has invalid characters")
    return token


def _is_credential_name(name: str) -> bool:
    return (_looks_like_credential(name)
            or name.upper().startswith(_API_BILLING_PREFIXES))


def filter_agent_env(env: Mapping[str, str], oauth_token: str) -> dict[str, str]:
    """The agent's environment: ``env`` (already allowlisted by
    env_loader.build_agent_env) minus every credential-shaped name and every
    name that points into the orchestrator's HOME or runtime directory, plus
    the OAuth token. The launcher adds HOME/USER/LOGNAME/SHELL/TMPDIR."""
    agent_env: dict[str, str] = {}
    for name, value in env.items():
        if name in _AGENT_SIDE_ENV or name.upper().startswith("XDG_"):
            continue
        if _is_credential_name(name):
            continue
        agent_env[name] = value
    agent_env[OAUTH_TOKEN_VAR] = oauth_token
    return agent_env


# --- TheForge read-only view -----------------------------------------------------


def refresh_view_db(source: Path, destination: Path,
                    exclude_tables: Sequence[str]) -> None:
    """Publish a read-only copy of ``source`` without ``exclude_tables``.

    Views and triggers that mention an excluded table go too, secure_delete
    plus VACUUM leave none of the dropped rows in free pages, and the copy is
    in rollback-journal mode so readers need no -wal/-shm files. The file is
    0444 and replaced atomically, so running agents keep the inode they
    opened. The agent cannot write it: its directory is not agent-writable
    (checked by the launcher through deny_write).
    """
    if os.path.realpath(source) == os.path.realpath(destination):
        raise AgentIsolationError("view_db_path must not be the real database")
    fd, temp_name = tempfile.mkstemp(prefix=".theforge-view-", suffix=".db",
                                     dir=destination.parent)
    os.close(fd)
    excluded = {name.lower() for name in exclude_tables}
    try:
        with contextlib.closing(sqlite3.connect(
                f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src, \
                contextlib.closing(sqlite3.connect(temp_name)) as view:
            src.backup(view)
            view.execute("PRAGMA journal_mode=DELETE")
            view.execute("PRAGMA secure_delete=ON")
            _drop_excluded(view, excluded)
            view.commit()
            view.execute("VACUUM")
            remaining = {row[0].lower() for row in view.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if remaining & excluded:
                raise AgentIsolationError(
                    f"excluded tables survived: {sorted(remaining & excluded)}")
        os.chmod(temp_name, 0o444)
        os.replace(temp_name, destination)
    except (sqlite3.Error, OSError) as exc:
        raise AgentIsolationError(
            f"cannot build the TheForge view {destination}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def _drop_excluded(view: sqlite3.Connection, excluded: set[str]) -> None:
    if not excluded:
        return
    mentions = re.compile(
        r"\b(?:" + "|".join(re.escape(name) for name in sorted(excluded))
        + r")\b", re.IGNORECASE)
    schema = view.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('view', 'trigger', 'table')").fetchall()
    for kind in ("trigger", "view"):
        for row_kind, name, table, sql in schema:
            if row_kind == kind and (table.lower() in excluded
                                     or mentions.search(sql or "")):
                view.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
    for row_kind, name, _table, _sql in schema:
        if row_kind == "table" and name.lower() in excluded:
            view.execute(f'DROP TABLE IF EXISTS "{name}"')
    with contextlib.suppress(sqlite3.OperationalError):  # no AUTOINCREMENT
        view.executemany("DELETE FROM sqlite_sequence WHERE lower(name) = ?",
                         [(name,) for name in excluded])


# --- MCP and settings handed to the agent ----------------------------------------


@dataclass
class _AccessNeeds:
    """Paths the agent must be able to execute or read (launcher-checked)."""

    execute: set[str] = field(default_factory=set)
    read: set[str] = field(default_factory=set)


def build_agent_mcp_config(config: Mapping[str, Any], settings: IsolationSettings,
                           needs: _AccessNeeds) -> tuple[dict, str | None]:
    """The MCP config the agent gets, and the forge server's source DB path.

    Only allowlisted servers survive. The forge server's --db-path is pointed
    at the read-only view. A kept server may not pass credential-like
    environment variables: the agent's own processes can read them.
    """
    servers = config.get("mcpServers", {}) if isinstance(config, Mapping) else None
    if not isinstance(servers, Mapping):
        raise AgentIsolationError("the MCP config has no mcpServers object")
    agent_servers: dict[str, Any] = {}
    source_db: str | None = None
    for name, server in servers.items():
        if name not in settings.allowed_mcp_servers:
            logger.info("[Isolation] MCP server %r is not in "
                        "allowed_mcp_servers; the agent does not get it", name)
            continue
        if not isinstance(server, Mapping):
            raise AgentIsolationError(f"MCP server {name!r} is not an object")
        server_env = server.get("env") or {}
        if not isinstance(server_env, Mapping):
            raise AgentIsolationError(f"MCP server {name!r} env is not an object")
        leaked = sorted(key for key in server_env if _is_credential_name(str(key)))
        if leaked:
            raise AgentIsolationError(
                f"MCP server {name!r} passes credential variables {leaked} "
                f"to the agent; remove it from {CONFIG_KEY}.allowed_mcp_servers")
        agent_server = copy.deepcopy(dict(server))
        if name == settings.forge_mcp_server:
            source_db = _point_at_view(name, agent_server, settings)
        command = agent_server.get("command")
        if isinstance(command, str) and os.path.isabs(command):
            needs.execute.add(command)
        for arg in agent_server.get("args") or []:
            if isinstance(arg, str) and os.path.isabs(arg) and os.path.exists(arg):
                needs.read.add(arg)
        agent_servers[name] = agent_server
    return {"mcpServers": agent_servers}, source_db


def _point_at_view(name: str, server: dict, settings: IsolationSettings) -> str:
    args = server.get("args")
    if not isinstance(args, list) or settings.view_db_path is None:
        raise AgentIsolationError(
            f"MCP server {name!r} cannot be pointed at the read-only view; "
            f"set {CONFIG_KEY}.view_db_path or remove it from "
            f"allowed_mcp_servers")
    for index, arg in enumerate(args):
        if arg == "--db-path" and index + 1 < len(args):
            source = args[index + 1]
            args[index + 1] = settings.view_db_path
            return str(source)
        if isinstance(arg, str) and arg.startswith("--db-path="):
            args[index] = f"--db-path={settings.view_db_path}"
            return arg.split("=", 1)[1]
    raise AgentIsolationError(f"MCP server {name!r} has no --db-path to point "
                              f"at the read-only view")


def hook_command_paths(settings_text: str, needs: _AccessNeeds) -> None:
    """Record every absolute program and argument of the settings' hook
    commands: a hook the agent user cannot run would fail open."""
    try:
        data = json.loads(settings_text)
    except ValueError as exc:
        raise AgentIsolationError(f"agent settings are not JSON: {exc}") from exc

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            command = node.get("command")
            if isinstance(command, str):
                try:
                    tokens = shlex.split(command)
                except ValueError as exc:
                    raise AgentIsolationError(
                        f"unparseable hook command {command!r}") from exc
                if tokens and os.path.isabs(tokens[0]):
                    needs.execute.add(tokens[0])
                needs.read.update(token for token in tokens[1:]
                                  if os.path.isabs(token))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)


def _read_private_file(path: str, option: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read(_HANDOFF_FILE_MAX_BYTES + 1)
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentIsolationError(f"cannot hand over {option} {path}: {exc}") from exc
    if len(content) > _HANDOFF_FILE_MAX_BYTES:
        raise AgentIsolationError(f"{option} {path} is too large to hand over")
    return content


# --- Worktree, handoff bundle and import -----------------------------------------


@dataclass(frozen=True)
class WorktreeInfo:
    """The orchestrator's task worktree the agent's clone is made from."""

    path: str
    branch_ref: str
    base_sha: str
    git_dir: str
    common_dir: str
    main_root: str


def _git(args: list[str], cwd: str, env: Mapping[str, str] | None = None,
         timeout: int = _GIT_TIMEOUT_SECONDS) -> str:
    result = git_run(args, cwd=cwd, env=env, timeout=timeout)
    if result.returncode != 0:
        raise AgentIsolationError(
            f"git {args[0]} failed in {cwd}: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


def _main_worktree_branch(path: str) -> str | None:
    listing = _git(["worktree", "list", "--porcelain"], path)
    first_block = listing.split("\n\n", 1)[0]
    for line in first_block.splitlines():
        if line.startswith("branch "):
            return line.split(" ", 1)[1]
    return None


def describe_worktree(cwd: str) -> WorktreeInfo:
    """Refuse anything but the root of a linked worktree on a task branch.

    Imported agent work only ever moves that branch; the main checkout's
    branch is the default branch, which the gate alone may advance.
    """
    path = os.path.realpath(cwd)
    if not os.path.isdir(path):
        raise AgentIsolationError(f"project directory {cwd!r} does not exist")
    top = os.path.realpath(_git(["rev-parse", "--show-toplevel"], path))
    if top != path:
        raise AgentIsolationError(
            f"agent_isolation needs the agent to work at the root of a git "
            f"worktree; {path} is inside {top}")
    git_dir = os.path.realpath(os.path.join(path, _git(["rev-parse", "--git-dir"], path)))
    common_dir = os.path.realpath(os.path.join(
        path, _git(["rev-parse", "--git-common-dir"], path)))
    if git_dir == common_dir:
        raise AgentIsolationError(
            f"{path} is a repository's main checkout, not a task worktree; "
            f"agent_isolation runs agents only in worktree isolation")
    branch_ref = git_run(["symbolic-ref", "-q", "HEAD"], cwd=path).stdout.strip()
    if not branch_ref.startswith("refs/heads/"):
        raise AgentIsolationError(f"worktree {path} has a detached HEAD")
    if branch_ref == _main_worktree_branch(path):
        raise AgentIsolationError(
            f"worktree {path} is on the main checkout's branch {branch_ref}")
    base_sha = _git(["rev-parse", "--verify", "HEAD^{commit}"], path)
    main_root = (os.path.dirname(common_dir)
                 if os.path.basename(common_dir) == ".git" else common_dir)
    return WorktreeInfo(path=path, branch_ref=branch_ref, base_sha=base_sha,
                        git_dir=git_dir, common_dir=common_dir,
                        main_root=main_root)


def _git_runner(cwd: str):
    return lambda args, env: _git(args, cwd, env=env)


def create_handoff_bundle(worktree: WorktreeInfo, unit: str,
                          carry_paths: Sequence[str], destination: Path) -> str:
    """Bundle the task branch plus the worktree's current state (uncommitted
    work of an earlier attempt included) and return the bundled ref name."""
    handoff_ref = f"refs/equipa/isolation-handoff/{unit}"
    index_path = destination.with_name("handoff.index")
    state = agent_launcher.snapshot_worktree_state(
        _git_runner(worktree.path), worktree.path, carry_paths,
        str(index_path), worktree.branch_ref)
    with contextlib.suppress(FileNotFoundError):
        index_path.unlink()
    _git(["update-ref", handoff_ref, state], worktree.path)
    try:
        _git(["bundle", "create", "-q", str(destination), handoff_ref],
             worktree.path, timeout=_BUNDLE_TIMEOUT_SECONDS)
    finally:
        git_run(["update-ref", "-d", handoff_ref], cwd=worktree.path)
    return handoff_ref


def copy_untrusted_file(source: str, destination: Path, max_bytes: int) -> None:
    """Copy an agent-written file without following links or blocking on a
    FIFO, refusing anything but a regular file of at most ``max_bytes``."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_NOCTTY)
    except FileNotFoundError as exc:
        raise AgentIsolationError(f"the agent left no export at {source}") from exc
    except OSError as exc:
        raise AgentIsolationError(f"cannot open the export {source}: {exc}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise AgentIsolationError(f"the export {source} is not a regular file")
        if info.st_size > max_bytes:
            raise AgentIsolationError(
                f"the export {source} is larger than max_export_bytes")
        copied = 0
        with open(destination, "xb") as out:
            while chunk := handle.read(1 << 20):
                copied += len(chunk)
                if copied > max_bytes:
                    raise AgentIsolationError(
                        f"the export {source} grew past max_export_bytes")
                out.write(chunk)


@dataclass(frozen=True)
class _TreeLink:
    """A symbolic link of a tree: its target blob and the target's size."""

    blob: str
    size: int


def _tree_symlinks(worktree_path: str, treeish: str,
                   where: str = "") -> dict[str, _TreeLink]:
    """{path: link} of every symbolic link in ``treeish``.

    Refuses a tree with more than ``_MAX_TREE_SYMLINKS`` links; the target
    sizes come from the listing, so no blob is read here (review R3140-01).
    """
    result = git_run(["ls-tree", "-r", "-z", "-l", "--full-tree", treeish],
                     cwd=worktree_path, timeout=_BUNDLE_TIMEOUT_SECONDS,
                     text=False)
    if result.returncode != 0:
        raise AgentIsolationError(
            f"git ls-tree failed in {worktree_path}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()[-500:]}")
    links: dict[str, _TreeLink] = {}
    for record in result.stdout.split(b"\0"):
        meta, _tab, path = record.partition(b"\t")
        # <mode> SP <type> SP <object> SP+ <size>; the size is padded.
        fields = meta.split()
        if len(fields) != 4 or fields[0] != _SYMLINK_MODE:
            continue
        if len(links) >= _MAX_TREE_SYMLINKS:
            raise AgentIsolationError(
                f"the agent's export{where} holds more than "
                f"{_MAX_TREE_SYMLINKS} symbolic links")
        try:
            size = int(fields[3])
        except ValueError as exc:
            raise AgentIsolationError(
                f"unreadable git ls-tree record in {worktree_path}") from exc
        links[path.decode("utf-8", "surrogateescape")] = _TreeLink(
            blob=fields[2].decode("ascii", "replace"), size=size)
    return links


class _LinkTrie:
    """The symbolic links of a tree as a trie of path components.

    Walking a target looks each component up in its directory's node, so a
    walk costs one dict access per component. Joining the directory path
    for every lookup made it quadratic in the target length (R3140-01).
    """

    __slots__ = ("children", "link")

    def __init__(self) -> None:
        self.children: dict[str, _LinkTrie] = {}
        self.link: str | None = None  # the link's path, on a link's node

    @classmethod
    def build(cls, paths: Iterable[str]) -> _LinkTrie:
        root = cls()
        for path in paths:
            node = root
            for part in path.split("/"):
                child = node.children.get(part)
                if child is None:
                    child = node.children[part] = cls()
                node = child
            node.link = path
        return root


class _WalkBudget:
    """Path components the link walks of one import check may still take."""

    __slots__ = ("remaining",)

    def __init__(self, steps: int | None = None) -> None:
        self.remaining = _MAX_LINK_WALK_STEPS if steps is None else steps


def _target_too_long(target: str) -> bool:
    return (len(target) > _MAX_LINK_TARGET_BYTES
            or len(target.encode("utf-8", "surrogateescape"))
            > _MAX_LINK_TARGET_BYTES)


def _link_escapes(path: str, target: str,
                  link_target: Callable[[str], str | None] | None = None,
                  *, links: _LinkTrie | None = None,
                  budget: _WalkBudget | None = None) -> bool:
    """True for an absolute target or one that leaves the working tree.

    ``link_target(p)`` returns the target of the tree's own link at ``p``
    (None when ``p`` is no link). Each such component is followed, as the
    kernel would, so ``a/..`` through a link ``a`` resolves where the link
    points rather than lexically (review R3136-05). Too many hops, or a
    target longer than the kernel accepts, count as an escape. Without
    ``link_target`` the walk is purely lexical.

    With ``links`` (the tree's links as a :class:`_LinkTrie`) a component
    is looked up in its directory's node and ``link_target`` is asked only
    for real links, so the walk is linear in the components it visits
    (R3140-01); :func:`check_imported_links` always passes it. Without
    ``links`` the lookups join the directory path, which only the target
    cap bounds. ``budget`` caps the components over several calls; past
    it the check is refused.
    """
    if target.startswith("/") or _target_too_long(target):
        return True
    use_trie = links is not None and link_target is not None
    by_path = links is None and link_target is not None
    # One entry per level of the current directory: its trie node (None
    # when no link lies below it), its path (``by_path``), or None.
    levels: list[Any] = []

    def descend(part: str) -> Any:
        if use_trie:
            parent = levels[-1] if levels else links
            return None if parent is None else parent.children.get(part)
        if by_path:
            return part if not levels else f"{levels[-1]}/{part}"
        return None

    def target_of(level: Any) -> str | None:
        if level is None or link_target is None:
            return None
        if use_trie:
            return None if level.link is None else link_target(level.link)
        return link_target(level)

    for part in posixpath.dirname(path).split("/"):
        if part:
            levels.append(descend(part))
    pending = collections.deque(target.split("/"))
    hops = 0
    while pending:
        part = pending.popleft()
        if budget is not None:
            budget.remaining -= 1
            if budget.remaining < 0:
                raise AgentIsolationError(
                    f"the links of the agent's export take more than "
                    f"{_MAX_LINK_WALK_STEPS} path components to check")
        if part in ("", "."):
            continue
        if part == "..":
            if not levels:
                return True
            levels.pop()
            continue
        level = descend(part)
        inner = target_of(level)
        if inner is None:
            levels.append(level)
            continue
        hops += 1
        if hops > _MAX_LINK_HOPS or inner.startswith("/") \
                or _target_too_long(inner):
            return True
        # The inner target is relative to the link's directory, which is
        # the current directory itself.
        pending.extendleft(reversed(inner.split("/")))
    return False


def _read_link_target(worktree_path: str, path: str, link: _TreeLink,
                      where: str = "") -> str:
    """The target of an exported link, refused unread when it is longer
    than any target the kernel would create."""
    if link.size > _MAX_LINK_TARGET_BYTES:
        raise AgentIsolationError(
            f"the agent's export{where} has a symbolic link {path} whose "
            f"target is {link.size} bytes (limit {_MAX_LINK_TARGET_BYTES})")
    result = git_run(["cat-file", "blob", link.blob], cwd=worktree_path,
                     text=False)
    if result.returncode != 0:
        raise AgentIsolationError(f"cannot read the link {path} in the "
                                  f"agent's export")
    if len(result.stdout) > _MAX_LINK_TARGET_BYTES:
        raise AgentIsolationError(
            f"the agent's export{where} has a symbolic link {path} whose "
            f"target is longer than {_MAX_LINK_TARGET_BYTES} bytes")
    return result.stdout.decode("utf-8", "surrogateescape")


def check_imported_links(worktree: WorktreeInfo, state: str,
                         carry_paths: Sequence[str], *,
                         where: str = "") -> None:
    """Refuse agent state whose symbolic links would redirect the
    orchestrator's own writes (review ISO-04).

    ``read-tree -u --reset`` replaces the orchestrator's ignored
    ``.equipa-artifacts/`` with whatever the agent recorded there, and the
    orchestrator later writes review files into it as its own user. So no
    link may sit at, above or below a carried path. Anywhere else, a link
    the agent added or changed may not be absolute or resolve out of the
    tree, following the tree's other links (R3136-05); links already in
    the dispatch base are the project's own. ``state`` is any commit of the
    export; ``where`` names it in the refusal.
    """
    protected = [path.strip("/") for path in
                 dict.fromkeys((ARTIFACTS_DIR, *carry_paths))]
    state_links = _tree_symlinks(worktree.path, state, where)
    base_links = _tree_symlinks(worktree.path, worktree.base_sha)
    changed: list[str] = []
    for path, link in state_links.items():
        for carried in protected:
            if (path == carried or carried.startswith(path + "/")
                    or path.startswith(carried + "/")):
                raise AgentIsolationError(
                    f"the agent's export{where} makes {path} a symbolic link; "
                    f"the orchestrator writes into {carried}, so this is "
                    f"refused")
        base_link = base_links.get(path)
        if base_link is None or base_link.blob != link.blob:
            changed.append(path)
    if len(changed) > _MAX_CHANGED_SYMLINKS:
        raise AgentIsolationError(
            f"the agent's export{where} adds or changes {len(changed)} "
            f"symbolic links (limit {_MAX_CHANGED_SYMLINKS})")
    # Every changed target is size-checked before any is read or walked.
    for path in changed:
        if state_links[path].size > _MAX_LINK_TARGET_BYTES:
            raise AgentIsolationError(
                f"the agent's export{where} has a symbolic link {path} whose "
                f"target is {state_links[path].size} bytes (limit "
                f"{_MAX_LINK_TARGET_BYTES})")
    targets: dict[str, str] = {}  # by blob: links may share one target

    def link_target(path: str) -> str | None:
        link = state_links.get(path)
        if link is None:
            return None
        if link.blob not in targets:
            targets[link.blob] = _read_link_target(worktree.path, path, link,
                                                   where)
        return targets[link.blob]

    trie = _LinkTrie.build(state_links)
    budget = _WalkBudget()
    for path in changed:
        target = link_target(path) or ""
        if _link_escapes(path, target, link_target, links=trie, budget=budget):
            raise AgentIsolationError(
                f"the agent's export{where} adds a symbolic link {path} -> "
                f"{target[:200]} that points outside the worktree")


def import_agent_export(worktree: WorktreeInfo, unit: str, export_path: str,
                        max_bytes: int,
                        carry_paths: Sequence[str] = ()) -> str:
    """Bring the agent's exported work into the task worktree.

    Only the task branch moves, and only from the dispatch base
    (compare-and-swap). The working tree is then set to the exported state
    and the index to the new branch tip, so the agent's uncommitted and
    carried files look exactly as the agent left them. Symbolic links that
    would redirect the orchestrator's writes refuse the import first
    (:func:`check_imported_links`). Returns the new tip.
    """
    private_dir = Path(tempfile.mkdtemp(prefix="equipa-isolation-import-"))
    import_ref = f"refs/equipa/isolation-import/{unit}"
    try:
        private_copy = private_dir / "export.bundle"
        copy_untrusted_file(export_path, private_copy, max_bytes)
        heads = _git(["bundle", "list-heads", str(private_copy)], worktree.path)
        if agent_launcher.EXPORT_REF not in (line.split()[-1] for line in
                                             heads.splitlines() if line.split()):
            raise AgentIsolationError(
                f"the export has no {agent_launcher.EXPORT_REF}")
        _git(["bundle", "verify", "-q", str(private_copy)], worktree.path)
        _git(["-c", "transfer.fsckObjects=true", "fetch", "-q", "--no-tags",
              "--no-write-fetch-head", str(private_copy),
              f"+{agent_launcher.EXPORT_REF}:{import_ref}"], worktree.path,
             timeout=_BUNDLE_TIMEOUT_SECONDS)
        try:
            commits = _git(["rev-list", "--parents", "-n", "1",
                            f"{import_ref}^{{commit}}"], worktree.path).split()
            if not 2 <= len(commits) <= 3:
                raise AgentIsolationError("the exported state commit has an "
                                          "unexpected shape")
            state, tip = commits[0], commits[1]
            # The state tree lands in the worktree, but the tip becomes the
            # task branch and a second parent is the clone's HEAD. An agent
            # can commit a link and then drop it from its working tree, so
            # every one is checked (review R3136-02).
            for commit, where in zip(commits, _EXPORT_COMMIT_NAMES):
                check_imported_links(worktree, commit, carry_paths,
                                     where=where)
            # update-ref compares only the old value: without this a tip
            # that is not a descendant (an ancestor of the base, or an
            # unrelated commit) would rewind or replace the task branch
            # and drop history an earlier review saw (review F5).
            ancestry = git_run(["merge-base", "--is-ancestor",
                                worktree.base_sha, tip], cwd=worktree.path)
            if ancestry.returncode != 0:
                raise AgentIsolationError(
                    f"the agent's task-branch tip {tip} does not descend "
                    f"from the dispatch base {worktree.base_sha}; refused "
                    f"(a rewound or replaced branch)")
            _git(["update-ref", "-m", "equipa isolation: import agent work",
                  worktree.branch_ref, tip, worktree.base_sha], worktree.path)
            _git(["read-tree", "-u", "--reset", state], worktree.path)
            _git(["reset", "-q"], worktree.path)
        finally:
            git_run(["update-ref", "-d", import_ref], cwd=worktree.path)
        return tip
    finally:
        shutil.rmtree(private_dir, ignore_errors=True)


# --- cgroup helpers ---------------------------------------------------------------


def read_proc_cgroup(pid: int) -> str | None:
    """cgroup v2 path of ``pid`` (``0::<path>`` line), or None."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    return None


def _cgroup_dir(cgroup: str) -> Path:
    return CGROUP_ROOT / cgroup.lstrip("/")


def verify_scope_cgroup(cgroup: str, settings: IsolationSettings) -> None:
    """The scope carries the configured limits and can be killed by us."""
    directory = _cgroup_dir(cgroup)
    expected = {"pids.max": str(settings.pids_max),
                "memory.max": str(settings.memory_max_bytes),
                "cpu.weight": str(settings.cpu_weight)}
    for name, value in expected.items():
        try:
            actual = (directory / name).read_text(encoding="ascii").strip()
        except OSError:
            actual = None
        if actual != value:
            raise AgentIsolationError(
                f"agent scope {cgroup}: {name} is {actual!r}, expected {value}; "
                f"is the {name.split('.')[0]} controller delegated to the user "
                f"manager?")
    kill_file = directory / "cgroup.kill"
    if not kill_file.is_file() or not os.access(kill_file, os.W_OK):
        raise AgentIsolationError(
            f"agent scope {cgroup} has no writable cgroup.kill (Linux 5.14+)")


def kill_cgroup(cgroup: str) -> bool:
    """SIGKILL every process in the cgroup, race-free against forks. True
    when done or when the cgroup no longer exists."""
    try:
        (_cgroup_dir(cgroup) / "cgroup.kill").write_text("1", encoding="ascii")
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.error("[Isolation] cannot kill cgroup %s: %s", cgroup, exc)
        return False
    return True


def cgroup_populated(cgroup: str) -> bool:
    """True while any process is left in the cgroup."""
    try:
        events = (_cgroup_dir(cgroup) / "cgroup.events").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return "populated 1" in events.splitlines()


def user_app_slice() -> Path:
    uid = os.getuid()
    return (CGROUP_ROOT / "user.slice" / f"user-{uid}.slice"
            / f"user@{uid}.service" / "app.slice")


def sweep_stale_scopes(app_slice: Path | None = None) -> list[str]:
    """Kill agent scopes whose orchestrator is gone.

    Covers an orchestrator that was SIGKILLed while an agent had already
    killed its own launcher: nothing else would stop that agent's
    remaining processes. Returns the scope names killed.
    """
    slice_dir = user_app_slice() if app_slice is None else app_slice
    killed: list[str] = []
    try:
        entries = list(os.scandir(slice_dir))
    except OSError:
        return killed
    for entry in entries:
        match = _UNIT_NAME_RE.match(entry.name)
        if not match or not entry.is_dir(follow_symlinks=False):
            continue
        owner_pid, owner_start = int(match[1]), int(match[2])
        if agent_launcher.proc_start_time(owner_pid) == owner_start:
            continue
        cgroup = "/" + str(Path(entry.path).relative_to(CGROUP_ROOT))
        if cgroup_populated(cgroup) and kill_cgroup(cgroup):
            logger.warning("[Isolation] killed stale agent scope %s (its "
                           "orchestrator %d is gone)", entry.name, owner_pid)
            killed.append(entry.name)
    return killed


def parse_handshake(line: bytes) -> tuple[str | None, str]:
    """(status, reason) of the launcher's status line; (None, text) if the
    line is not one."""
    text = line.decode("utf-8", "replace").strip()
    try:
        message = json.loads(text)
    except ValueError:
        return None, text[:500]
    if (not isinstance(message, dict)
            or message.get("type") != agent_launcher.HANDSHAKE_TYPE):
        return None, text[:500]
    return str(message.get("status")), str(message.get("reason", ""))


# --- The handoff -----------------------------------------------------------------


@dataclass
class Handoff:
    header: dict[str, Any]
    bundle_path: Path | None
    export_path: str | None


def _existing(paths: Iterable[str | os.PathLike[str] | None]) -> list[str]:
    return sorted({os.path.abspath(os.fspath(path)) for path in paths
                   if path and os.path.lexists(os.fspath(path))})


def database_file_paths(database: str | os.PathLike[str] | None) -> list[str]:
    """The database and its SQLite side files, both as named and resolved.

    THEFORGE_DB is often a symlink, and SQLite keeps -wal/-shm next to the
    link's TARGET, so the side files beside the link may not exist while the
    real ones do (review ISO-03).
    """
    if not database:
        return []
    named = os.path.abspath(os.fspath(database))
    paths: list[str] = []
    for base in dict.fromkeys((named, os.path.realpath(named))):
        paths += [base, f"{base}-wal", f"{base}-shm", f"{base}-journal"]
    return paths


def database_directories(settings: IsolationSettings,
                         forge_source_db: str | None = None) -> list[str]:
    """Directories the agent may neither list nor enter: the resolved
    directory of every TheForge database and ``db_backup_dirs``.

    Protecting the directory, not the file, covers every backup and copy
    kept beside the database (``theforge_backup_*.db`` held api_keys while
    only the live file was checked; review ISO-03).
    """
    directories = [os.path.dirname(os.path.realpath(os.fspath(database)))
                   for database in (THEFORGE_DB, forge_source_db) if database]
    directories += [os.path.realpath(path) for path in settings.db_backup_dirs]
    return list(dict.fromkeys(directories))


def _deny_read_paths(settings: IsolationSettings,
                     forge_source_db: str | None) -> list[str]:
    """Paths the launcher refuses to run with if the agent can read them;
    for a directory, entering it counts as reading (see the launcher)."""
    import pwd

    home = pwd.getpwuid(os.getuid()).pw_dir
    candidates: list[str | None] = [MCP_CONFIG.as_posix(), home]
    for database in (str(THEFORGE_DB), forge_source_db):
        candidates += database_file_paths(database)
    candidates += database_directories(settings, forge_source_db)
    candidates += [os.path.join(home, name) for name in ORCHESTRATOR_HOME_SECRETS]
    candidates += settings.deny_read
    return _existing(candidates)


def _is_within(path: str, directory: str) -> bool:
    real_path, real_directory = os.path.realpath(path), os.path.realpath(directory)
    return (real_path == real_directory
            or real_path.startswith(real_directory.rstrip(os.sep) + os.sep))


def check_database_directory_conflicts(settings: IsolationSettings,
                                       directories: Sequence[str],
                                       required: Iterable[str]) -> None:
    """Refuse when something the agent must reach lies inside a directory it
    must not enter. The launcher would refuse too, but with a less direct
    reason; this names the fix."""
    needed = [settings.launcher, settings.python, settings.claude_executable,
              settings.git_executable, settings.exchange_dir,
              *([settings.view_db_path] if settings.view_db_path else []),
              *required]
    for directory in directories:
        for path in needed:
            if _is_within(path, directory):
                raise AgentIsolationError(
                    f"{path} is inside {directory}, a TheForge database or "
                    f"backup directory the agent must not enter; keep the "
                    f"database in a directory of its own "
                    f"(docs/AGENT_ISOLATION.md step 3)")


def _deny_write_paths(settings: IsolationSettings,
                      worktree: WorktreeInfo | None) -> list[str]:
    view = settings.view_db_path
    repository: list[str] = []
    if worktree is not None:
        repository = [worktree.path, os.path.dirname(worktree.path),
                      worktree.git_dir, worktree.common_dir, worktree.main_root]
    return _existing([
        RUNTIME_ROOT, PACKAGE_DIR, settings.launcher, settings.python,
        settings.claude_executable, settings.git_executable, *repository,
        view, os.path.dirname(view) if view else None,
        *settings.deny_write,
    ])


def _git_identity(path: str) -> dict[str, str]:
    identity = {}
    for key in ("user.name", "user.email"):
        result = git_run(["config", "--get", key], cwd=path)
        if result.returncode == 0 and result.stdout.strip():
            identity[key.replace(".", "_")] = result.stdout.strip()
    return identity


def build_handoff(cmd: Sequence[str], cwd: str | None, env: Mapping[str, str],
                  settings: IsolationSettings, unit: str,
                  worktree: WorktreeInfo | None, oauth_token: str,
                  bundle_path: Path) -> Handoff:
    """Everything the launcher needs, except the scope path (known only
    once the scope exists). Without a worktree (a helper agent with no
    project directory) nothing is bundled or exported."""
    if not cmd:
        raise AgentIsolationError("empty agent command")
    argv = [settings.claude_executable, *cmd[1:]]
    needs = _AccessNeeds()
    files: list[dict[str, Any]] = []
    forge_source_db: str | None = None
    index = 1
    while index < len(argv):
        option = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else None
        if option in HANDOFF_FILE_OPTIONS and value is not None:
            if option == "--settings" and not os.path.isfile(value) \
                    and value.lstrip().startswith("{"):
                hook_command_paths(value, needs)  # inline JSON settings
                index += 2
                continue
            content = _read_private_file(value, option)
            if option == "--mcp-config":
                try:
                    mcp = json.loads(content)
                except ValueError as exc:
                    raise AgentIsolationError(f"MCP config {value} is not "
                                              f"JSON: {exc}") from exc
                agent_mcp, forge_source_db = build_agent_mcp_config(
                    mcp, settings, needs)
                content = json.dumps(agent_mcp, indent=2)
            elif option == "--settings":
                hook_command_paths(content, needs)
            files.append({"index": index + 1,
                          "name": f"{index + 1}-{HANDOFF_FILE_OPTIONS[option]}",
                          "content": content})
            index += 2
            continue
        if option == "--add-dir" and value is not None:
            if worktree is None or os.path.realpath(value) != worktree.path:
                needs.read.add(value)
            index += 2
            continue
        index += 1
    check_database_directory_conflicts(
        settings, database_directories(settings, forge_source_db),
        [*needs.execute, *needs.read])
    if forge_source_db is not None:
        refresh_view_db(Path(forge_source_db), Path(settings.view_db_path),
                        settings.exclude_tables)
        needs.read.add(settings.view_db_path)
    workspace: dict[str, Any] | None = None
    export_path: str | None = None
    if worktree is not None:
        export_path = os.path.join(settings.exchange_dir, f"{unit}.bundle")
        workspace = {
            "handoff_ref": create_handoff_bundle(
                worktree, unit, settings.carry_ignored_paths, bundle_path),
            "branch_ref": worktree.branch_ref,
            "base_sha": worktree.base_sha,
            "export_path": export_path,
            "carry_paths": list(settings.carry_ignored_paths),
        }
    header = {
        "unit": unit,
        "argv": argv,
        "executable": settings.claude_executable,
        "env": filter_agent_env(env, oauth_token),
        "files": files,
        "workdir_sources": (sorted({worktree.path, os.path.abspath(cwd)})
                            if worktree is not None and cwd else []),
        "identity": {"user": settings.agent_user,
                     "orchestrator_uid": os.getuid(),
                     "privileged_groups": list(settings.privileged_groups)},
        "cgroup": {"path": None, "pids_max": settings.pids_max,
                   "memory_max": settings.memory_max_bytes,
                   "cpu_weight": settings.cpu_weight},
        "deny_read": _deny_read_paths(settings, forge_source_db),
        "deny_write": _deny_write_paths(settings, worktree),
        "must_execute": sorted(needs.execute),
        "must_read": sorted(needs.read),
        "git": {"executable": settings.git_executable,
                "hardening_args": list(GIT_HARDENING_ARGS),
                "hardening_env": dict(GIT_HARDENING_ENV),
                **(_git_identity(worktree.path) if worktree else {})},
        "workspace": workspace,
        "grace": agent_launcher.DEFAULT_GRACE_SECONDS,
    }
    return Handoff(header=header,
                   bundle_path=bundle_path if worktree is not None else None,
                   export_path=export_path)


def encode_handoff_preamble(header_bytes: bytes, bundle_size: int) -> bytes:
    return (f"{agent_launcher.HANDOFF_MAGIC} {agent_launcher.HANDOFF_VERSION} "
            f"{len(header_bytes)} {bundle_size}\n").encode("ascii")


# --- Reviewers run alone (review R3136-03) ------------------------------------------
#
# Every isolated unit runs as the same agent UID, so a unit running beside a
# security reviewer can read the reviewer's handed-over prompt (with its
# nonces) and write into its clone. Until each unit (or at least each
# reviewer) gets its own UID, reviewer units never overlap any other
# isolated unit: a reader-writer lock over two flock files in the
# orchestrator user's private runtime directory (/run/user/<uid>, never
# taken from XDG_RUNTIME_DIR), so it holds across every dispatch mode and
# every orchestrator process of this user.
#
# * Any other unit takes the turnstile shared for an instant, then the units
#   lock shared for its whole life.
# * A reviewer takes the turnstile exclusively (no new unit may start) and
#   then the units lock exclusively (every running unit has ended), and
#   keeps both until it ends. It also waits until no agent scope of this
#   user is populated, which covers a unit that survived cgroup.kill and
#   units of an orchestrator that predates the lock.

# Roles whose units run alone.
EXCLUSIVE_ROLES = frozenset({"security-reviewer", "code-reviewer"})
_UNIT_ROLE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "equipa_isolation_unit_role", default=None)
_TURNSTILE_LOCK_NAME = "equipa-isolation-turnstile.lock"
_UNITS_LOCK_NAME = "equipa-isolation-units.lock"
_SLOT_POLL_SECONDS = 0.25
_SLOT_LOG_EVERY_SECONDS = 300.0


@contextlib.contextmanager
def unit_role(role: str | None) -> Iterator[None]:
    """Mark the agents spawned inside this block as ``role``'s units.

    ``agent_runner.build_cli_command`` sets it around the ``with`` block
    that runs the command, so :func:`spawn_isolated_agent` knows a reviewer
    from any other unit without a change to every call site.
    """
    token = _UNIT_ROLE.set(role)
    try:
        yield
    finally:
        _UNIT_ROLE.reset(token)


def current_unit_role() -> str | None:
    return _UNIT_ROLE.get()


class UnitSlot:
    """A unit's place among the running isolated units; released (the lock
    files closed) when the unit has ended. Idempotent."""

    def __init__(self, label: str, exclusive: bool, handles: list[int]) -> None:
        self.label = label
        self.exclusive = exclusive
        self._handles = handles

    @property
    def held(self) -> bool:
        return bool(self._handles)

    def release(self) -> None:
        # Closing, not LOCK_UN: a forked child sharing the descriptors must
        # not drop the parent's lock, and closing the last one releases it.
        handles, self._handles = self._handles, []
        for handle in handles:
            with contextlib.suppress(OSError):
                os.close(handle)
        if handles and self.exclusive:
            logger.info("[Isolation] %s has ended; other isolated agents may "
                        "start again", self.label)


def _check_private_lock_dir(directory: str) -> None:
    """The unit locks hold only while nobody else can replace them.

    Whoever may write their directory may unlink a lock file a running
    unit holds; the next reviewer would then create and lock a fresh file
    at once and run beside that unit. So the directory itself (no link)
    must belong to this user and be writable by no one else.
    """
    try:
        info = os.lstat(directory)
    except OSError as exc:
        raise AgentIsolationError(
            f"cannot use the unit lock directory {directory}: {exc}") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise AgentIsolationError(
            f"the unit lock directory {directory} must be a directory of this "
            f"user that no one else can write to (mode "
            f"{stat.filemode(info.st_mode)}, owner uid {info.st_uid})")


def _open_lock_file(directory: str, name: str) -> int:
    path = os.path.join(directory, name)
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise AgentIsolationError(
            f"cannot open the unit lock {path}: {exc}") from exc
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(handle)
        raise AgentIsolationError(
            f"the unit lock {path} is not a regular file of this user")
    return handle


def live_agent_scopes(app_slice: Path | None = None) -> list[str]:
    """Names of this user's populated agent scopes, whoever started them."""
    slice_dir = user_app_slice() if app_slice is None else app_slice
    try:
        entries = list(os.scandir(slice_dir))
    except OSError:
        return []
    live = []
    for entry in entries:
        if (_UNIT_NAME_RE.match(entry.name)
                and entry.is_dir(follow_symlinks=False)
                and cgroup_populated(
                    "/" + str(Path(entry.path).relative_to(CGROUP_ROOT)))):
            live.append(entry.name)
    return live


class _SlotWait:
    """Deadline and progress logging while a unit waits for its turn."""

    def __init__(self, label: str, reason: str, timeout: float) -> None:
        self.label = label
        self.reason = reason
        self.timeout = timeout
        self.started = time.monotonic()
        self.logged_at: float | None = None

    async def pause(self, what: str) -> None:
        now = time.monotonic()
        if now - self.started >= self.timeout:
            raise AgentIsolationError(
                f"{self.label} waited {self.timeout:.0f}s for {what} "
                f"(unit_wait_timeout_sec); refused")
        if self.logged_at is None or now - self.logged_at >= _SLOT_LOG_EVERY_SECONDS:
            self.logged_at = now
            logger.warning("[Isolation] %s waits for %s: %s (%.0fs so far)",
                           self.label, what, self.reason, now - self.started)
        await asyncio.sleep(_SLOT_POLL_SECONDS)


async def _flock_when_free(handle: int, operation: int, wait: _SlotWait,
                           what: str) -> None:
    import fcntl  # Linux-only, like the rest of the isolated spawn

    while True:
        try:
            fcntl.flock(handle, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        except OSError as exc:
            raise AgentIsolationError(f"cannot lock the unit lock: {exc}") from exc
        await wait.pause(what)


async def acquire_unit_slot(label: str, *, exclusive: bool, timeout: float,
                            lock_dir: str | None = None,
                            app_slice: Path | None = None) -> UnitSlot:
    """Wait for this unit's turn (see the section comment) and hold it.

    Raises AgentIsolationError when the lock cannot be used or the wait
    exceeds ``timeout``; nothing is held then.
    """
    import fcntl

    directory = lock_dir or _unit_lock_dir()
    _check_private_lock_dir(directory)
    handles = [_open_lock_file(directory, _TURNSTILE_LOCK_NAME)]
    try:
        handles.append(_open_lock_file(directory, _UNITS_LOCK_NAME))
        turnstile, units = handles
        if exclusive:
            wait = _SlotWait(label, "a reviewer runs alone while every agent "
                             "shares one UID (review R3136-03)", timeout)
            await _flock_when_free(turnstile, fcntl.LOCK_EX, wait,
                                   "the reviewer before it")
            await _flock_when_free(units, fcntl.LOCK_EX, wait,
                                   "the running isolated agents to end")
            while True:
                # A scope whose orchestrator died is killed, not waited for.
                sweep_stale_scopes(app_slice)
                scopes = live_agent_scopes(app_slice)
                if not scopes:
                    break
                await wait.pause(f"agent scopes {', '.join(sorted(scopes))}")
            logger.warning("[Isolation] %s runs alone: new isolated agents "
                           "wait until it ends (shared agent UID, review "
                           "R3136-03)", label)
            return UnitSlot(label, True, handles)
        wait = _SlotWait(label, "a reviewer runs or waits to run alone "
                         "(review R3136-03)", timeout)
        await _flock_when_free(turnstile, fcntl.LOCK_SH, wait, "a reviewer")
        await _flock_when_free(units, fcntl.LOCK_SH, wait, "a reviewer")
        os.close(handles.pop(0))  # the turnstile is only passed through
        return UnitSlot(label, False, handles)
    except BaseException:
        for handle in handles:
            with contextlib.suppress(OSError):
                os.close(handle)
        raise


# --- The running agent -------------------------------------------------------------


_LIVE_ISOLATED_AGENTS: set[IsolatedAgent] = set()
_exit_handler_registered = False


class IsolatedAgent:
    """Handle on one isolated agent; the same interface as agent_runner's
    ``_ContainedAgent`` (request_termination / terminate / terminate_sync /
    release / leader_exited).

    Termination closes the stop channel (the launcher forwards SIGTERM to the
    CLI, sweeps and exports), waits ``stop_timeout_sec`` for that, then kills
    whatever is left with ``cgroup.kill`` and imports the export once.
    """

    def __init__(self, process: asyncio.subprocess.Process, unit: str,
                 settings: IsolationSettings, worktree: WorktreeInfo | None,
                 export_path: str | None, slot: UnitSlot | None = None) -> None:
        self.process = process
        # Held until release(): the unit's cgroup has been emptied by then.
        self.slot = slot
        self.pid = process.pid
        self.start_time = agent_launcher.proc_start_time(process.pid)
        self.unit = unit
        self.settings = settings
        self.worktree = worktree
        self.export_path = export_path
        self.cgroup: str | None = None
        self.started = False
        self.imported = False
        self.closed = False
        self.leftover_killer: asyncio.Task | None = None
        self.stop_channel: asyncio.StreamWriter | None = None
        # A forked child inherits the registry; only the spawner may stop it.
        self.owner_pid = os.getpid()

    # -- setup ------------------------------------------------------------------

    async def establish(self, handoff: Handoff) -> None:
        """Verify the scope, send the handoff, wait for the launcher's
        verdict. Raises AgentIsolationError; the caller aborts."""
        self.cgroup = await self._wait_for_scope()
        verify_scope_cgroup(self.cgroup, self.settings)
        handoff.header["cgroup"]["path"] = self.cgroup
        try:
            await asyncio.wait_for(self._send_handoff(handoff),
                                   timeout=self.settings.setup_timeout_sec)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the launcher refused early; its status line says why
        except asyncio.TimeoutError as exc:
            raise AgentIsolationError("sending the handoff timed out") from exc
        await self._wait_until_ready()
        # The stop channel is ours alone from here on. Process.communicate()
        # (agent_runner's non-streaming path) closes a visible stdin at once,
        # which the launcher would take as "stop the agent".
        self.stop_channel = self.process.stdin
        self.process.stdin = None
        self.started = True

    async def _wait_for_scope(self) -> str:
        suffix = f"/{self.unit}.scope"
        deadline = time.monotonic() + _SCOPE_TIMEOUT_SECONDS
        while True:
            cgroup = read_proc_cgroup(self.pid)
            if cgroup is not None and cgroup.endswith(suffix):
                return cgroup
            if self.process.returncode is not None:
                raise AgentIsolationError(
                    f"systemd-run/sudo exited with status "
                    f"{self.process.returncode} before the agent scope "
                    f"existed: {await self._stderr_tail()}")
            if time.monotonic() >= deadline:
                raise AgentIsolationError(
                    f"agent scope {self.unit}.scope did not appear within "
                    f"{_SCOPE_TIMEOUT_SECONDS:.0f}s")
            await asyncio.sleep(_POLL_SECONDS / 2)

    async def _send_handoff(self, handoff: Handoff) -> None:
        stdin = self.process.stdin
        header_bytes = json.dumps(handoff.header).encode("utf-8")
        bundle_size = (0 if handoff.bundle_path is None
                       else handoff.bundle_path.stat().st_size)
        stdin.write(encode_handoff_preamble(header_bytes, bundle_size))
        stdin.write(header_bytes)
        await stdin.drain()
        if handoff.bundle_path is None:
            return
        with open(handoff.bundle_path, "rb") as bundle:
            while chunk := bundle.read(1 << 20):
                stdin.write(chunk)
                await stdin.drain()

    async def _wait_until_ready(self) -> None:
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(),
                                          timeout=self.settings.setup_timeout_sec)
        except asyncio.TimeoutError as exc:
            raise AgentIsolationError(
                f"the agent launcher did not report within "
                f"{self.settings.setup_timeout_sec:.0f}s") from exc
        except ValueError as exc:  # a line longer than the stream limit
            raise AgentIsolationError(f"unreadable launcher status: {exc}") from exc
        status, reason = parse_handshake(line)
        if status == "ready":
            return
        if status == "refused":
            raise AgentIsolationError(f"the agent launcher refused to start "
                                      f"the agent: {reason}")
        self._kill_everything()
        raise AgentIsolationError(
            f"the agent launcher did not confirm isolation ({reason or 'EOF'}): "
            f"{await self._stderr_tail()}")

    async def _stderr_tail(self) -> str:
        if self.process.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self.process.stderr.read(), timeout=5)
        except (asyncio.TimeoutError, ValueError):
            return "(no stderr)"
        return data.decode("utf-8", "replace").strip()[-1000:]

    # -- termination --------------------------------------------------------------

    def leader_exited(self) -> bool:
        """True once the process the orchestrator spawned has exited."""
        if self.process.returncode is not None:
            return True
        stat_line = agent_launcher.read_proc_stat(self.pid)
        return (stat_line is None or stat_line[0] == "Z"
                or stat_line[3] != self.start_time)

    def request_termination(self) -> None:
        """Close the stop channel. Never blocks."""
        channel = self.stop_channel or self.process.stdin
        if channel is not None:
            with contextlib.suppress(OSError, RuntimeError):
                channel.close()

    def _kill_everything(self) -> None:
        if self.cgroup is not None:
            kill_cgroup(self.cgroup)
        elif self.process.returncode is None:
            # Still systemd-run (our own UID) before it entered the scope.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self.process.kill()

    def _cgroup_empty(self) -> bool:
        return self.cgroup is None or not cgroup_populated(self.cgroup)

    async def terminate(self) -> None:
        """Stop the agent, kill its cgroup and import its work. Idempotent;
        finishes synchronously if cancelled part-way."""
        if self.closed:
            return
        try:
            await self._terminate_async()
        except BaseException:
            self.terminate_sync()
            raise
        self.release()

    async def _terminate_async(self) -> None:
        self.request_termination()
        # Poll the leader rather than await process.wait(): since Python
        # 3.12 wait() only returns once every pipe is closed, and a process
        # the agent left behind can hold stdout open indefinitely.
        deadline = time.monotonic() + self.settings.stop_timeout_sec
        while not self.leader_exited():
            if time.monotonic() >= deadline:
                logger.warning("[Isolation] agent %s did not stop within "
                               "%.0fs; killing its cgroup", self.unit,
                               self.settings.stop_timeout_sec)
                break
            await asyncio.sleep(_POLL_SECONDS)
        await self._empty_cgroup()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(),
                                   timeout=_CGROUP_KILL_TIMEOUT_SECONDS)
        await asyncio.to_thread(self._import_once)

    async def _empty_cgroup(self) -> None:
        deadline = time.monotonic() + _CGROUP_KILL_TIMEOUT_SECONDS
        while not self._cgroup_empty():
            self._kill_everything()
            if time.monotonic() >= deadline:
                logger.error("[Isolation] agent scope %s survived cgroup.kill",
                             self.cgroup)
                return
            await asyncio.sleep(_POLL_SECONDS)

    async def kill_leftovers_when_launcher_exits(self) -> None:
        """Background task: once the launcher is gone (its export written,
        or the agent killed it), kill whatever the agent left in the scope,
        so no leftover can keep the CLI's stdout open and stall the
        orchestrator's read loop (review CT-01/CT-02)."""
        while not self.closed and not self.leader_exited():
            await asyncio.sleep(_LEFTOVER_POLL_SECONDS)
        if not self.closed:
            await self._empty_cgroup()

    def terminate_sync(self) -> None:
        """Blocking variant for cancellation, loop shutdown and atexit."""
        if self.closed:
            return
        try:
            self.request_termination()
            deadline = time.monotonic() + self.settings.stop_timeout_sec
            while not self.leader_exited() and time.monotonic() < deadline:
                time.sleep(_POLL_SECONDS)
            deadline = time.monotonic() + _CGROUP_KILL_TIMEOUT_SECONDS
            while not self._cgroup_empty() and time.monotonic() < deadline:
                self._kill_everything()
                time.sleep(_POLL_SECONDS)
            self._import_once()
        finally:
            self.release()

    async def abort(self) -> None:
        """Setup failed: kill everything; there is nothing to import."""
        self.request_termination()
        self._kill_everything()
        deadline = time.monotonic() + _CGROUP_KILL_TIMEOUT_SECONDS
        while not self._cgroup_empty() and time.monotonic() < deadline:
            self._kill_everything()
            await asyncio.sleep(_POLL_SECONDS)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(),
                                   timeout=_CGROUP_KILL_TIMEOUT_SECONDS)
        self.release()

    def _import_once(self) -> None:
        if (self.imported or not self.started or self.worktree is None
                or self.export_path is None):
            return
        self.imported = True
        try:
            tip = import_agent_export(self.worktree, self.unit, self.export_path,
                                      self.settings.max_export_bytes,
                                      self.settings.carry_ignored_paths)
        except AgentIsolationError as exc:
            logger.error(
                "[Isolation] the work of agent %s was NOT imported into %s: "
                "%s. An export stays at %s until it expires; if the launcher "
                "could not export, the agent's clone is kept in the agent "
                "user's ~/%s/%s/repo.", self.unit, self.worktree.path, exc,
                self.export_path, agent_launcher.AGENT_STATE_DIRNAME, self.unit)
            return
        logger.info("[Isolation] imported the work of agent %s: %s is at %s",
                    self.unit, self.worktree.branch_ref, tip)

    def release(self) -> None:
        if self.closed:
            return
        self.closed = True
        _LIVE_ISOLATED_AGENTS.discard(self)
        self.request_termination()
        if self.leftover_killer is not None and not self.leftover_killer.done():
            with contextlib.suppress(RuntimeError):  # loop already closed
                self.leftover_killer.cancel()
        if self.slot is not None:
            self.slot.release()


def _terminate_live_isolated_agents_at_exit() -> None:
    agents = [agent for agent in _LIVE_ISOLATED_AGENTS
              if agent.owner_pid == os.getpid()]
    for agent in agents:
        agent.request_termination()
    for agent in agents:
        try:
            agent.terminate_sync()
        except Exception:  # noqa: BLE001 - every remaining agent must be tried
            logger.exception("[Isolation] failed to stop agent %s at exit",
                             agent.unit)


def _track(agent: IsolatedAgent) -> None:
    global _exit_handler_registered
    if not _exit_handler_registered:
        atexit.register(_terminate_live_isolated_agents_at_exit)
        _exit_handler_registered = True
    _LIVE_ISOLATED_AGENTS.add(agent)


async def spawn_isolated_agent(
    cmd: Sequence[str], cwd: str | None, env: Mapping[str, str],
    dispatch_config: Mapping[str, Any] | None = None, *,
    limit: int | None = None,
) -> tuple[asyncio.subprocess.Process, IsolatedAgent]:
    """Start the agent CLI isolated (see the module docstring).

    Returns the process whose stdout/stderr carry the CLI's output (the
    launcher's status line is already consumed) and its handle. A reviewer
    unit (:data:`EXCLUSIVE_ROLES`, see :func:`unit_role`) first waits until
    no other isolated unit runs, and any unit waits while a reviewer runs or
    waits (:func:`acquire_unit_slot`); the handle holds that slot until it
    is released.

    Raises:
        AgentIsolationError: isolation could not be established or verified;
            nothing of the agent is left running.
    """
    if not sys.platform.startswith("linux"):
        raise AgentIsolationError("agent_isolation is Linux-only")
    if dispatch_config is None:
        dispatch_config = get_active_dispatch_config()
    refusal = isolation_requirement_refusal(dispatch_config)
    if refusal is not None:
        raise AgentIsolationError(refusal)
    settings = load_isolation_settings(dispatch_config)
    identity = resolve_agent_identity(settings)
    check_host(settings, identity)
    sweep_stale_scopes()
    unit = make_unit_name()
    role = current_unit_role()
    slot = await acquire_unit_slot(
        f"{role or 'agent'} unit {unit}", exclusive=role in EXCLUSIVE_ROLES,
        timeout=settings.unit_wait_timeout_sec)
    try:
        return await _spawn_in_slot(cmd, cwd, env, settings, unit, slot, limit)
    except BaseException:
        slot.release()
        raise


async def _spawn_in_slot(cmd: Sequence[str], cwd: str | None,
                         env: Mapping[str, str], settings: IsolationSettings,
                         unit: str, slot: UnitSlot, limit: int | None,
                         ) -> tuple[asyncio.subprocess.Process, IsolatedAgent]:
    """spawn_isolated_agent once the unit may run; the agent handle owns
    ``slot`` from its creation."""
    oauth_token = resolve_oauth_token(settings)
    worktree = (await asyncio.to_thread(describe_worktree, cwd)
                if cwd else None)
    temp_dir = Path(tempfile.mkdtemp(prefix="equipa-isolation-"))
    try:
        handoff = await asyncio.to_thread(
            build_handoff, cmd, cwd, env, settings, unit, worktree,
            oauth_token, temp_dir / "handoff.bundle")
        stream_options = {"limit": limit} if limit else {}
        try:
            process = await asyncio.create_subprocess_exec(
                *build_launch_command(settings, unit),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=launch_environment(),
                cwd="/", start_new_session=True, **stream_options,
            )
        except OSError as exc:
            raise AgentIsolationError(f"cannot run systemd-run: {exc}") from exc
        agent = IsolatedAgent(process, unit, settings, worktree,
                              handoff.export_path, slot)
        _track(agent)
        try:
            await agent.establish(handoff)
        except BaseException:
            await agent.abort()
            raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    agent.leftover_killer = asyncio.create_task(
        agent.kill_leftovers_when_launcher_exits())
    logger.info("[Isolation] agent %s runs as %s in %s", unit,
                settings.agent_user, agent.cgroup)
    return process, agent


# --- Operator verification (scripts/verify_agent_isolation.sh) --------------------


# Database files and copies: x.db, x.db-wal, x.db.pre-consolidation-backup,
# theforge_backup_<date>.db, x.sqlite3 ...
_DATABASE_COPY_RE = re.compile(r"\.(?:db|sqlite3?)(?:$|[-._])", re.IGNORECASE)
_MAX_DATABASE_COPIES = 5000


def find_database_copies(directories: Iterable[str],
                         limit: int = _MAX_DATABASE_COPIES
                         ) -> tuple[list[str], bool]:
    """Every database-looking file below ``directories``, as the
    orchestrator sees them; returns (paths, truncated at ``limit``)."""
    found: set[str] = set()
    for directory in directories:
        for root, _dirs, files in os.walk(directory, followlinks=False):
            for name in files:
                if _DATABASE_COPY_RE.search(name):
                    found.add(os.path.join(root, name))
                    if len(found) >= limit:
                        return sorted(found), True
    return sorted(found), False


# Below the project roots: the same pruning and depth as the verify script's
# secret-file search, and only real SQLite files are opened.
_SCAN_PRUNED_DIRS = frozenset({"node_modules", ".git", ".venv", "venv"})
_SCAN_MAX_DEPTH = 6
_SQLITE_HEADER = b"SQLite format 3\x00"


def _sqlite_tables(path: str) -> set[str]:
    """Lower-cased table names of the SQLite database at ``path`` (a
    regular file, opened read-only and immutable), or an empty set."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return set()
        with open(path, "rb") as handle:
            if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return set()
        uri = f"{Path(path).absolute().as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return set()
    return {str(name).lower() for (name,) in rows}


def find_credential_databases(roots: Iterable[str], tables: Iterable[str],
                              limit: int = _MAX_DATABASE_COPIES
                              ) -> tuple[list[str], bool]:
    """Database files below ``roots`` whose schema holds one of ``tables``
    (the excluded ones, api_keys): TheForge copies outside its protected
    directories, which the verify probe then tries to read as the agent
    (review R3136-08). Returns (paths, truncated at ``limit`` candidates)."""
    wanted = {table.lower() for table in tables}
    found: list[str] = []
    examined = 0
    for root in dict.fromkeys(roots):
        root_depth = os.path.normpath(root).count(os.sep)
        for directory, subdirs, files in os.walk(root, followlinks=False):
            if directory.count(os.sep) - root_depth >= _SCAN_MAX_DEPTH:
                subdirs[:] = []
            else:
                subdirs[:] = [name for name in subdirs
                              if name not in _SCAN_PRUNED_DIRS]
            for name in files:
                if not _DATABASE_COPY_RE.search(name):
                    continue
                examined += 1
                if examined > limit:
                    return found, True
                path = os.path.join(directory, name)
                if wanted & _sqlite_tables(path):
                    found.append(path)
    return found, False


def forge_source_database(settings: IsolationSettings) -> str | None:
    """The --db-path of the forge MCP server in mcp_config.json, or None."""
    try:
        config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        args = config["mcpServers"][settings.forge_mcp_server]["args"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(args, list):
        return None
    for index, arg in enumerate(args):
        if arg == "--db-path" and index + 1 < len(args):
            return str(args[index + 1])
        if isinstance(arg, str) and arg.startswith("--db-path="):
            return arg.split("=", 1)[1]
    return None


def _agent_in_group(settings: IsolationSettings, gid: int) -> bool:
    import grp
    import pwd

    try:
        entry = pwd.getpwnam(settings.agent_user)
        members = grp.getgrgid(gid).gr_mem
    except KeyError:
        return False
    return entry.pw_gid == gid or settings.agent_user in members


def _database_directory_failures(settings: IsolationSettings,
                                 directories: Sequence[str]) -> list[str]:
    """Mode checks, as the orchestrator, on the database and backup
    directories and on every copy below them (review ISO-03)."""
    failures: list[str] = []
    for directory in directories:
        try:
            info = os.stat(directory)
        except OSError as exc:
            failures.append(f"TheForge database/backup directory {directory} "
                            f"cannot be checked: {exc}")
            continue
        if info.st_mode & (stat.S_IROTH | stat.S_IXOTH):
            failures.append(f"{directory} can be listed or entered by other "
                            f"users, the agent included (chmod o-rwx)")
        if (info.st_mode & (stat.S_IRGRP | stat.S_IXGRP)
                and _agent_in_group(settings, info.st_gid)):
            failures.append(f"{directory} is open to a group the agent user "
                            f"is in (chmod g-rwx or change its group)")
    copies, truncated = find_database_copies(directories)
    for copy in copies:
        with contextlib.suppress(OSError):
            if os.stat(copy).st_mode & stat.S_IROTH:
                failures.append(f"{copy} is world-readable (chmod 0600)")
    if truncated:
        failures.append(f"more than {_MAX_DATABASE_COPIES} database copies "
                        f"under {list(directories)}; clean up before verifying")
    return failures


def _outer_checks(settings: IsolationSettings) -> list[str]:
    """Checks made as the orchestrator user; returns failure messages."""
    import pwd

    failures: list[str] = []
    orchestrator_user = pwd.getpwuid(os.getuid()).pw_name
    listed = _exit_status([settings.sudo, "-n", "-l", "-u", settings.agent_user,
                             settings.python, "-I", settings.launcher,
                             *agent_launcher.ISOLATED_ARGV])
    if listed != 0:
        failures.append("the sudoers rule is missing; install:\n"
                        + sudoers_snippet(settings, orchestrator_user))
    database = Path(THEFORGE_DB)
    if database.exists() and database.stat().st_mode & stat.S_IROTH:
        failures.append(f"{database} is world-readable (chmod o-r)")
    failures += _database_directory_failures(
        settings, database_directories(settings, forge_source_database(settings)))
    if not settings.secret_scan_roots:
        failures.append(f"{CONFIG_KEY}.secret_scan_roots is empty; list the "
                        f"directories that hold project checkouts so the "
                        f"agent's access to their secrets is checked")
    copies, truncated = find_credential_databases(settings.secret_scan_roots,
                                                  settings.exclude_tables)
    for copy in copies:
        with contextlib.suppress(OSError):
            if os.stat(copy).st_mode & stat.S_IROTH:
                failures.append(f"{copy} holds an excluded table "
                                f"({', '.join(settings.exclude_tables)}) and "
                                f"is world-readable (chmod 0600 or remove it)")
    if truncated:
        failures.append(f"more than {_MAX_DATABASE_COPIES} database files "
                        f"below secret_scan_roots; the search stopped there")
    return failures


def _exit_status(argv: Sequence[str]) -> int:
    """Exit status of a short, output-less command (outer checks only)."""
    import subprocess

    try:
        return subprocess.run(list(argv), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30,
                              check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 127


def build_probe_command(probe: str, settings: IsolationSettings,
                        repos: Sequence[str], orchestrator_home: str) -> list[str]:
    """argv for ``verify_agent_isolation.sh --inside`` (argv[0] is replaced
    by the probe path, as the CLI path is for a real agent)."""
    command = ["claude", "--inside",
               "--orchestrator-pid", str(os.getpid()),
               "--orchestrator-home", orchestrator_home,
               "--db", str(THEFORGE_DB),
               "--runtime", str(RUNTIME_ROOT),
               "--launcher", settings.launcher,
               "--pids-max", str(settings.pids_max)]
    for repo in [str(RUNTIME_ROOT), *repos]:
        git_dir = os.path.join(repo, ".git")
        if os.path.exists(git_dir):
            command += ["--git-dir", git_dir]
    if settings.view_db_path:
        command += ["--view-db", settings.view_db_path]
        for table in settings.exclude_tables:
            command += ["--exclude-table", table]
    if MCP_CONFIG.is_file():
        command += ["--mcp-config", str(MCP_CONFIG)]
    # ISO-03: the directories themselves, and every copy the orchestrator
    # can see in them, are probed as the agent.
    source_db = forge_source_database(settings)
    directories = database_directories(settings, source_db)
    for directory in directories:
        command += ["--deny-dir", directory]
    copies, _truncated = find_database_copies(directories)
    side_files = [path for path in (*database_file_paths(THEFORGE_DB),
                                    *database_file_paths(source_db))
                  if os.path.lexists(path)]
    for path in dict.fromkeys([*side_files, *copies]):
        command += ["--db-copy", path]
    # ISO-05: secret-shaped files readable below the project roots.
    roots = list(dict.fromkeys([*settings.secret_scan_roots, *repos]))
    for root in roots:
        command += ["--secret-root", root]
    # R3136-08: a database copy holding an excluded table anywhere below
    # them is probed by name too.
    credential_copies, _truncated = find_credential_databases(
        roots, settings.exclude_tables)
    for path in credential_copies:
        command += ["--db-copy", path]
    return command


async def _run_probe(command: list[str], dispatch_config: Mapping[str, Any]
                     ) -> str:
    from equipa.env_loader import build_agent_env

    process, agent = await spawn_isolated_agent(
        command, None, build_agent_env(dispatch_config), dispatch_config)
    try:
        output = await asyncio.wait_for(process.stdout.read(), timeout=120)
    finally:
        await agent.terminate()
    return output.decode("utf-8", "replace")


def verification_main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m equipa.isolation --verify-probe <script>``: run the
    operator's checks through the real isolated launch path. 0 = all pass."""
    import argparse
    import pwd

    parser = argparse.ArgumentParser(
        prog="python3 -m equipa.isolation",
        description="Verify agent isolation (docs/AGENT_ISOLATION.md).")
    parser.add_argument("--verify-probe", required=True,
                        help="absolute path of scripts/verify_agent_isolation.sh")
    parser.add_argument("--repo", action="append", default=[],
                        help="project repository whose .git the agent must "
                             "not write (repeatable)")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("FAIL run this as the orchestrator user, not root")
        return 2
    dispatch_config = copy.deepcopy(get_active_dispatch_config())
    section = dict(dispatch_config.get(CONFIG_KEY) or {})
    section["claude_executable"] = os.path.abspath(args.verify_probe)
    dispatch_config[CONFIG_KEY] = section
    try:
        settings = load_isolation_settings(dispatch_config)
    except AgentIsolationError as exc:
        print(f"FAIL {exc}")
        return 2
    failures = _outer_checks(settings)
    for failure in failures:
        print(f"FAIL {failure}")
    command = build_probe_command(args.verify_probe, settings, args.repo,
                                  pwd.getpwuid(os.getuid()).pw_dir)
    try:
        output = asyncio.run(_run_probe(command, dispatch_config))
    except AgentIsolationError as exc:
        print(f"FAIL isolation could not be established: {exc}")
        return 2
    print(output, end="")
    if failures or "RESULT: PASS" not in output.splitlines():
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(verification_main())
