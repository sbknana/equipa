"""EQUIPA agent_runner — agent dispatch, streaming, and retry logic.

Layer 6: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.security, equipa.roles.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import filecmp
import functools
import hashlib
import json
import logging
import math
import os
import random
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypedDict

logger = logging.getLogger(__name__)


def _run_started_at_utc() -> str:
    """Naive-UTC ISO timestamp marking when an agent run began.

    Stamped onto every run result so consumers (notably the single-agent
    guard's ``validate_tasks_created_claim``, which rejects a ``TASKS_CREATED``
    line referencing task ids that predate the run) can tell a freshly-created
    task from a pre-existing/hallucinated one.

    MUST be naive UTC: ``tasks.created_at`` is ``DEFAULT CURRENT_TIMESTAMP``,
    which SQLite stores as naive UTC. A tz-aware value would raise on the
    ``created < run_started`` comparison (naive vs aware), and a local-time
    value would false-flag tasks legitimately created *during* the run as
    pre-existing (off by the UTC offset).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

if TYPE_CHECKING:
    from equipa.prompts import PromptResult


# Module-level cache of recent tool calls per (task_id, role). Populated by
# the streaming loop via :func:`_record_tool_call`; read by
# :func:`get_recent_tool_calls` from ``equipa.sessions`` at cycle-boundary
# capture time. The buffer storage inside the loop (``tool_history``) is
# unchanged — this is a tiny parallel mirror so the orchestrator-cycle
# session capture can read recent activity without reaching into a frame
# local. PLAN-1067 §2.B2.
_RECENT_TOOL_CALLS: dict[tuple[int, str], list[dict[str, Any]]] = {}
_RECENT_TOOL_CALLS_MAX: int = 50


def _record_tool_call(
    task_id: int,
    role: str,
    tool: str,
    *,
    turn: int,
    ok: bool,
    args_hash: str = "",
) -> None:
    """Append a tool-call record to the per-(task, role) ring buffer.

    Caps the buffer at :data:`_RECENT_TOOL_CALLS_MAX` to keep memory bounded
    for long-running agents. Failure to record is non-fatal.
    """
    try:
        key = (int(task_id), str(role))
    except (TypeError, ValueError):
        return
    buf = _RECENT_TOOL_CALLS.setdefault(key, [])
    buf.append({
        "tool": tool,
        "args_hash": args_hash,
        "ok": bool(ok),
        "turn": int(turn),
    })
    if len(buf) > _RECENT_TOOL_CALLS_MAX:
        del buf[: len(buf) - _RECENT_TOOL_CALLS_MAX]


def get_recent_tool_calls(
    task_id: int,
    role: str,
    n: int = 20,
) -> list[dict[str, Any]]:
    """Return up to the last ``n`` tool calls recorded for ``(task_id, role)``.

    Returns the list in chronological order (oldest first). Returns an empty
    list if nothing has been recorded for this pair — callers must treat the
    buffer as best-effort. This is the ONE accessor exposed by
    ``agent_runner`` for orchestrator-cycle session capture (PLAN-1067 §2.B2);
    the underlying ``tool_history`` remains a frame-local list inside the
    streaming loop.
    """
    if n <= 0:
        return []
    key = (int(task_id), str(role))
    buf = _RECENT_TOOL_CALLS.get(key)
    if not buf:
        return []
    return list(buf[-n:])


class _AgentResultRequired(TypedDict):
    """Always-present keys in an AgentResult.

    Every return path of dispatch_agent and the underlying runners must set
    these six keys. They form the core contract relied on by callers
    (loops.py, dispatch.py, manager.py).
    """

    success: bool
    result_text: str
    num_turns: int
    duration: float
    cost: float | None
    errors: list[str]


class AgentResult(_AgentResultRequired, total=False):
    """Return shape for dispatch_agent and the underlying agent runners.

    The six required keys are declared on _AgentResultRequired. The keys
    below are optional — they are added by specific code paths (streaming
    runner, RLM decompose, callers that annotate results after dispatch).

    Adding a new key? Declare it here so mypy can catch typos at call sites.
    """

    # Streaming runner (run_agent_streaming) additions
    has_file_changes: bool
    early_terminated: bool
    early_term_reason: str
    early_completed: bool
    early_complete_reason: str
    compaction_count: int
    compaction_signals: list[dict[str, str]]
    files_read: list[str]
    files_changed_set: list[str]
    action_log: list[dict[str, Any]]

    # RLM decompose path additions
    rlm_decompose: bool
    files_examined: list[str]

    # Caller-injected annotations (set by loops.py after dispatch)
    turns_allocated: int
    turns_max: int

    # Run-start marker (naive UTC ISO), set by the top-level runners so the
    # single-agent guard can date-check a TASKS_CREATED claim. See
    # _run_started_at_utc.
    started_at: str

from equipa import agent_launcher, isolation
from equipa.abort_controller import AbortController, create_child_abort_controller
from equipa.cli_isolation import (
    CLAUDE_CLI_ISOLATION_ARGS,
    is_claude_cli,
    isolate_claude_argv,
    mcp_config_values,
)
from equipa.reactive_check import ReactiveBashChecker
from equipa.config import (
    get_active_dispatch_config,
    get_configured_model,
    get_persistent_retry_max_attempts,
    is_feature_enabled,
    load_dispatch_config,
)
from equipa.constants import (
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_WARN_TURNS,
    MCP_CONFIG,
    PROCESS_TIMEOUT,
    PROJECT_DIRS,
    ROLE_SKILLS,
)
from equipa.db import bulk_log_agent_actions, classify_error
from equipa.env_loader import active_agent_env, protect_orchestrator_process
from equipa.redact import redact_secrets, redact_tool_input, redacted_preview
from equipa.checkpoints import (
    SOFT_CHECKPOINT_INTERVAL,
    save_soft_checkpoint,
)
from equipa.monitoring import (
    LOOP_TERMINATE_THRESHOLD,
    LOOP_WARNING_THRESHOLD,
    _build_streaming_result,
    _build_tool_signature,
    _check_git_changes,
    _check_monologue,
    _check_stuck_phrases,
    _compute_output_hash,
    _detect_tool_loop,
    _get_budget_message,
    _parse_early_complete,
    detect_compaction_signals,
)
from equipa.output import log
from equipa.parsing import validate_output
from equipa.security import verify_skill_integrity
from equipa.tasks import verify_task_updated

# Retry configuration from Claude Code withRetry.ts
BASE_DELAY_MS = 500
MAX_BACKOFF_MS = 32000
# After this many consecutive 529/overloaded errors a loud warning is logged.
# The run keeps retrying on the SAME model — EQUIPA never swaps or downgrades
# the configured model (owner directive 2026-09-22, task #2992).
MAX_529_RETRIES = 3
JITTER_FACTOR = 0.25  # 25% jitter as per Claude Code

# Outcome recorded when sustained 529/overloaded errors exhaust every retry.
OVERLOADED_OUTCOME = "agent_overloaded"

# Persistent retry mode (for unattended sessions)
PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000  # 5 minutes
PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000  # 6 hours
HEARTBEAT_INTERVAL_MS = 30_000  # 30 seconds


def get_retry_delay(
    attempt: int,
    max_delay_ms: int = MAX_BACKOFF_MS,
    persistent: bool = False,
) -> float:
    """Exponential backoff with 25% jitter (Claude Code pattern).

    Args:
        attempt: Attempt number (1-indexed)
        max_delay_ms: Maximum delay cap in milliseconds
        persistent: If True, use persistent retry mode (higher backoff for unattended)

    Returns:
        Delay in seconds
    """
    if persistent:
        max_delay_ms = min(max_delay_ms, PERSISTENT_MAX_BACKOFF_MS)

    base_delay = min(BASE_DELAY_MS * math.pow(2, attempt - 1), max_delay_ms)
    jitter = random.random() * JITTER_FACTOR * base_delay
    return (base_delay + jitter) / 1000.0  # Convert ms to seconds


def is_overloaded_error(stderr: str, stdout: str) -> bool:
    """Detect 529/overloaded errors from Claude CLI output.

    Args:
        stderr: Standard error text
        stdout: Standard output text (may contain JSON error)

    Returns:
        True if this is an overloaded/529 error
    """
    return bool(_OVERLOADED_RE.search(f"{stderr} {stdout}"))


# Retry classifiers (F1, task 3134). A status code only counts as a whole
# token: "529" inside "15290 tokens" or "500" inside "1500" is not an API
# error. Callers pass structured error fields (stderr, the CLI's error
# result), never the agent's own RESULT text.
_OVERLOADED_RE = re.compile(r"\b529\b|overloaded", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"\b(?:429|529)\b|rate limit|overloaded", re.IGNORECASE)
_RETRYABLE_RE = re.compile(
    r"\b(?:429|500|502|503|504)\b|rate limit|connection|timeout|econnreset"
    r"|epipe", re.IGNORECASE)
# The error entry both result builders add for an error_max_turns run.
_MAX_TURNS_ERROR = "Agent hit max turns limit"


def _structured_error_text(result: Mapping[str, Any]) -> str:
    """The error fields the retry classifiers may read (F1, task 3134).

    ``errors`` holds what the orchestrator recorded: CLI stderr, the CLI's
    error result (an API error message), early-termination reasons. The
    agent's ``result_text`` is never included: a RESULT block that mentions
    "timeout" or "HTTP 500" is not an API error, and matching it relaunched
    a finished agent up to ten times.
    """
    errors = result.get("errors") or []
    return " ".join(error for error in errors
                    if isinstance(error, str) and error != _MAX_TURNS_ERROR)


def is_transient_capacity_error(stderr: str, stdout: str) -> bool:
    """Check if error is a transient capacity issue (429 or 529/overloaded).

    These errors are suitable for persistent retry mode with long backoff.

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if this is a 429 or 529 capacity error
    """
    return bool(_CAPACITY_RE.search(f"{stderr} {stdout}"))


def is_retryable_error(stderr: str, stdout: str) -> bool:
    """Check if error is retryable (network, timeout, 429, 500-level).

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if error should be retried
    """
    return bool(_RETRYABLE_RE.search(f"{stderr} {stdout}"))


def _cmd_model(cmd: list[str]) -> str | None:
    """Return the ``--model`` value of a claude command, or None if absent."""
    for index, arg in enumerate(cmd[:-1]):
        if arg == "--model":
            return cmd[index + 1]
    return None


def _note_overloaded(cmd: list[str], consecutive_529_errors: int) -> None:
    """Log loudly once sustained 529s cross MAX_529_RETRIES.

    The retry continues on the SAME model; there is no fallback path.
    """
    if consecutive_529_errors == MAX_529_RETRIES:
        print(f"  [Overloaded] {consecutive_529_errors} consecutive 529/overloaded "
              f"errors on model {_cmd_model(cmd) or '<unset>'} — retrying on "
              f"the SAME model "
              f"(model downgrades are forbidden)")


def _fail_overloaded(
    result: dict[str, Any],
    cmd: list[str],
    consecutive_529_errors: int,
    max_retries: int,
) -> dict[str, Any]:
    """Mark a run as a loud OVERLOADED failure after retries are exhausted.

    Sets ``outcome`` to OVERLOADED_OUTCOME so the dev loop records an
    explicit overloaded failure instead of a generic one.
    """
    message = (
        f"OVERLOADED: model {_cmd_model(cmd) or '<unset>'} returned "
        f"529/overloaded on "
        f"{consecutive_529_errors} consecutive attempt(s) and all "
        f"{max_retries} retries are exhausted. Failing the run — EQUIPA never "
        f"falls back to a different model."
    )
    result["success"] = False
    result["overloaded"] = True
    result["outcome"] = OVERLOADED_OUTCOME
    result.setdefault("errors", []).append(message)
    print(f"  [Overloaded] {message}")
    return result


def is_overloaded_result(result: object) -> bool:
    """Return True if an agent result is the loud sustained-529 failure.

    Every dispatch call site must check this BEFORE parsing the agent output:
    an overloaded run produced no work, so parsing it yields empty/unknown
    values that downstream logic can mistake for "no tests" or "no findings"
    (task #2994, SECURITY-REVIEW-2992 S1).
    """
    return isinstance(result, dict) and result.get("outcome") == OVERLOADED_OUTCOME


def _resolve_persistent_ceiling(persistent_max_attempts: int | None) -> int:
    """Return the persistent-retry ceiling: explicit arg, else config key."""
    if persistent_max_attempts is not None:
        if (isinstance(persistent_max_attempts, bool)
                or not isinstance(persistent_max_attempts, int)
                or persistent_max_attempts < 1):
            raise ValueError(
                f"persistent_max_attempts must be a positive int, got "
                f"{persistent_max_attempts!r}"
            )
        return persistent_max_attempts
    return get_persistent_retry_max_attempts()


def _fail_persistent_exhausted(
    result: dict[str, Any],
    cmd: list[str],
    overloaded: bool,
    consecutive_529_errors: int,
    persistent_attempts: int,
    last_error: str,
) -> dict[str, Any]:
    """Fail a persistent-retry run loudly once its capacity ceiling is hit.

    Sustained 529 fails with OVERLOADED_OUTCOME exactly like the bounded
    path; a 429 ceiling fails as a plain run failure. Either way the run
    stops instead of retrying forever (task #2994 S9).
    """
    print(f"  [PersistentRetry] Ceiling reached: {persistent_attempts} "
          f"consecutive capacity failure(s). Giving up.")
    if overloaded:
        return _fail_overloaded(
            result, cmd, consecutive_529_errors, persistent_attempts)
    result["success"] = False
    result.setdefault("errors", []).append(
        f"Persistent retry ceiling ({persistent_attempts}) reached on "
        f"capacity errors. Last error: {last_error}"
    )
    return result


# Path to the flag-gated PreToolUse Bash security gate hook. Resolved from
# this module's location (equipa/agent_runner.py -> <repo_root>/hooks/...),
# so it is correct regardless of the process cwd.
PRETOOLUSE_HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent / "hooks" / "pretooluse_bash_gate.py"
)


# Environment variables the --settings file sets to empty (RR-03). The CLI
# still loads the agent-writable ~/.claude/settings.json (user scope), whose
# "env" block reaches the CLI's own environment and every tool, hook and MCP
# server it starts. The CLI applies the env blocks in source order and the
# --settings file (flag scope) comes after user scope, so each name below is
# overwritten with "", which every consumer treats as unset:
# * shell and loader startup code: BASH_ENV/ENV run a file before every
#   non-interactive shell; SHELLOPTS=xtrace with PS4='$(...)' runs code per
#   command; LD_* load libraries into every program;
# * interpreter startup code: NODE_OPTIONS (--require), PYTHON*, PERL5*,
#   RUBY*, JAVA_TOOL_OPTIONS;
# * Claude CLI switches: CLAUDE_CODE_SAFE_MODE and CLAUDE_CODE_SIMPLE (bare
#   mode) turn every non-managed hook off, which is the Bash gate;
#   CLAUDE_CODE_SHELL_PREFIX wraps and CLAUDE_CODE_SHELL replaces the shell
#   that runs each command, and CLAUDE_ENV_FILE is sourced before it, all
#   after the gate has judged the command.
# An operator passthrough of one of these names is overridden too while the
# gate is on; they load code and have no place in an agent environment.
# Residual: user scope can still set other variables (PATH, HOME, SHELL,
# GIT_*), other settings (apiKeyHelper runs a command, extra hooks) and
# ~/.claude/CLAUDE.md. The complete fix is a per-unit CLAUDE_CONFIG_DIR that
# agents cannot write, under agent isolation (task 3136).
SETTINGS_ENV_NEUTRALISED: tuple[str, ...] = (
    "BASH_ENV", "ENV", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "PS4",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "PYTHONINSPECT", "PYTHONUSERBASE", "PERL5OPT", "PERL5LIB", "RUBYOPT",
    "RUBYLIB", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SHELL_PREFIX",
    "CLAUDE_CODE_SHELL", "CLAUDE_ENV_FILE",
)


def _pretooluse_settings_payload(hook_script: str | Path, python_bin: str) -> dict:
    """Build the Claude CLI ``--settings`` payload wiring the Bash gate hook.

    Returns a dict matching the Claude Code settings schema: a single
    ``PreToolUse`` hook whose matcher targets the ``Bash`` tool and whose
    command runs :mod:`hooks.pretooluse_bash_gate`. That script exits 2 (with
    the failing check + reason on stderr) to block an unsafe command BEFORE
    the CLI executes it — the pre-execution half of the bash security story.

    The payload also pins ``"disableAllHooks": false`` (RR-03). Agents can
    write the user-scope ``~/.claude/settings.json``, and one
    ``{"disableAllHooks": true}`` there would switch the gate off for every
    later run; flag-scope settings (this file) outrank user scope. That is
    the only hook switch the CLI reads outside managed policy
    (``allowManagedHooksOnly`` is read from policy settings only). The
    ``env`` block empties SETTINGS_ENV_NEUTRALISED, including the two
    variables that turn hooks off.

    Args:
        hook_script: Absolute path to ``pretooluse_bash_gate.py``.
        python_bin: Interpreter used to run the hook (normally the same
            interpreter running the orchestrator, ``sys.executable``).
    """
    command = f"{shlex.quote(str(python_bin))} {shlex.quote(str(hook_script))}"
    return {
        "disableAllHooks": False,
        "env": {name: "" for name in SETTINGS_ENV_NEUTRALISED},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": command},
                    ],
                }
            ]
        }
    }


# --- Reactive Bash check vs the PreToolUse gate (review finding sandbox-04) ---
#
# With features.bash_security_pretooluse on, the PreToolUse hook runs the same
# check_bash_command BEFORE a Bash command executes and refuses it (exit 2),
# telling the agent why so it can self-correct. The reactive stream check used
# to kill the agent anyway, for a command that never ran, so every false
# positive cost a whole attempt. Now a flagged command is judged by its own
# tool_result: the gate's refusal is a strike, anything else means the command
# executed (the gate failed open) and the agent is killed as before.
_HOOK_BLOCK_STRIKE_LIMIT = 8


def _pretooluse_hook_command(cmd: list[str]) -> str | None:
    """Return the Bash-gate hook command this CLI run was given, or None.

    Reads the --settings file that build_cli_command wrote (it exists for the
    whole run). Anything unexpected returns None, and the reactive check then
    keeps the old kill-on-flag behaviour: fail closed.
    """
    try:
        path = cmd[cmd.index("--settings") + 1]
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        for entry in payload.get("hooks", {}).get("PreToolUse", []):
            if entry.get("matcher") != "Bash":
                continue
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                expected = _pretooluse_settings_payload(
                    PRETOOLUSE_HOOK_SCRIPT, sys.executable or "python3",
                )["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                if command == expected:
                    return command
    except (ValueError, IndexError, OSError, AttributeError, TypeError):
        return None
    return None


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
    return ""


def _is_hook_block_result(
    content: Any, is_error: bool, hook_command: str | None, check_id: Any,
) -> bool:
    """True only when this tool_result is the PreToolUse gate refusing the call.

    The CLI reports a hook that exits 2 as an error result whose text starts
    ``PreToolUse:Bash hook error: [<hook command>]: `` followed by the hook's
    stderr, which names the check that fired. All of it must match; a command
    that actually ran produces its own output instead.
    """
    if not is_error or not hook_command:
        return False
    text = _tool_result_text(content)
    return (
        text.startswith(f"PreToolUse:Bash hook error: [{hook_command}]: ")
        and f"Bash security check {check_id} BLOCKED" in text
    )


def _read_budget_scale() -> float:
    """``dispatch_config['early_term_read_budget_scale']``, clamped to [1, 3].

    Lets one dispatch (via --dispatch-config) give read-heavy work, such as a
    task continuing from a saved patch and review, more turns before the
    no-edit watchdog fires. It can only loosen, never tighten, and anything
    invalid falls back to 1.0, which is today's thresholds.
    """
    try:
        from equipa.config import get_active_dispatch_config
        scale = float(get_active_dispatch_config().get(
            "early_term_read_budget_scale", 1.0))
    except (TypeError, ValueError, AttributeError, OSError):
        return 1.0
    if scale != scale:  # NaN
        return 1.0
    return min(3.0, max(1.0, scale))


# A reactive check slower than this means the hook, which runs the same check
# under the CLI's hook timeout, may have timed out and let the command run
# (sandbox-07). Such commands are killed on sight, as before.
_SLOW_CHECK_SECONDS = 5.0
_CANARY_COMMAND = "ls -la <(echo equipa-canary)"  # process substitution: always refused (check 8)


def _gate_fingerprint() -> str | None:
    """sha256 over the hook script and the checker it loads, or None."""
    h = hashlib.sha256()
    try:
        for path in (PRETOOLUSE_HOOK_SCRIPT,
                     Path(__file__).with_name("bash_security.py")):
            h.update(path.read_bytes())
    except OSError:
        return None
    return h.hexdigest()


def _gate_canary_ok(hook_command: str, cwd: str | None = None) -> bool:
    """Run the hook on a command it must refuse; True only on a correct refusal.

    If the gate cannot load, crashes or allows the canary, it would also fail
    open for real commands, so the reactive check must keep killing on sight.
    The canary runs with the agent's allowlisted environment and working
    directory, the conditions the CLI runs the real hook under, so a pass
    means the hook is active for THIS run (sandbox-04).
    """
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": _CANARY_COMMAND},
        "hook_event_name": "PreToolUse",
    })
    try:
        proc = subprocess.run(
            shlex.split(hook_command), input=payload, capture_output=True,
            text=True, timeout=30, env=_agent_subprocess_env(), cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 2 and "BLOCKED" in (proc.stderr or "")


# One persistent checker process per orchestrator (P2A-01). Tests replace it
# with a ReactiveBashChecker that loads a fake checker file.
_REACTIVE_CHECKER = ReactiveBashChecker()


async def _reactive_bash_check(bash_cmd: str) -> Any | None:
    """``check_bash_command`` in a separate process; None when it failed.

    The check is regex-bound and ``re`` holds the GIL, so a worker thread
    would still freeze the event loop that monitors every parallel agent
    (sandbox-07 iii, P2A-01). It runs in a persistent worker process with a
    ``_SLOW_CHECK_SECONDS`` deadline instead; a missed deadline kills and
    recycles the worker. Past the deadline the hook (same check, same input)
    may itself have timed out and let the command run, so the caller treats
    None (timeout, crash or malformed verdict) as a block.
    """
    return await _REACTIVE_CHECKER.check(bash_cmd, _SLOW_CHECK_SECONDS)


class AgentDispatchRefused(RuntimeError):
    """The agent must not be started; the message says why and how to fix it."""


def _cmd_option(cmd: list[str], flag: str) -> str | None:
    """Value following the FIRST ``flag`` in ``cmd``, or None."""
    try:
        return cmd[cmd.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _check_mcp_servers(
    mcp_config: str | os.PathLike[str],
    project_dirs: Iterable[str | os.PathLike[str] | None] = (),
) -> None:
    """Refuse an MCP config unless every stdio server is on the allowlist.

    The agent CLI runs in the project directory (sandbox-11), and the stdio
    MCP servers it starts inherit that cwd. Anything a server resolves
    relative to its cwd is therefore agent-writable: a relative
    ``--db-path`` silently opens a different database, and a launcher that
    loads code from its cwd runs agent-planted code holding the server's
    credentials, such as the EQUIPA_MCP_TOKEN (P2A-02). A denylist of such
    launchers kept missing some (``timeout python3 -m``, ``uv run``, ``node
    -r``; IR-03), so only these shapes are accepted (see _check_mcp_launch):

    * an absolute python with ``-I`` running an absolute script, or ``-I
      -m`` with an absolute cwd outside every project directory;
    * an absolute node running an absolute script, with no preload, loader
      or eval option;
    * any other absolute executable file that is not a wrapper, shell,
      language runtime or package runner.

    Nothing may live inside a project directory (``project_dirs`` plus every
    configured PROJECT_DIRS entry), and the server env may not set a
    code-loading variable (NODE_OPTIONS, PYTHONPATH, LD_PRELOAD, ...). Fail
    closed rather than guess. A missing config is left to the CLI, which
    reports it itself.

    Raises:
        AgentDispatchRefused: a server off the allowlist, or an unreadable
            or malformed config.
    """
    path = Path(mcp_config)
    if not path.is_file():
        return
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentDispatchRefused(
            f"cannot read MCP config {path} to verify its servers: {exc}"
        ) from exc
    _check_mcp_config_data(config, path, project_dirs)


def _check_mcp_config_value(value: str, cwd: str | None) -> None:
    """Check one ``--mcp-config`` value: a file, or inline JSON (RR-05).

    A relative file would be read from the CLI's cwd, the agent-writable
    project directory, so it is refused. Inline JSON (the manager passes
    ``{"mcpServers": {}}``) is checked like a file.
    """
    if value.lstrip().startswith("{"):
        try:
            config = json.loads(value)
        except ValueError as exc:
            raise AgentDispatchRefused(
                f"inline --mcp-config is not valid JSON: {exc}") from exc
        _check_mcp_config_data(config, "inline --mcp-config", (cwd,))
        return
    if not os.path.isabs(value):
        raise AgentDispatchRefused(
            f"--mcp-config {value!r} is a relative path, which the CLI reads "
            f"from the project directory {cwd!r} that agents can write. Use "
            f"an absolute path.")
    _check_mcp_servers(value, (cwd,))


def _check_mcp_config_data(
    config: Any, source: Path | str,
    project_dirs: Iterable[str | os.PathLike[str] | None],
) -> None:
    """Every server of one parsed MCP config (see _check_mcp_servers)."""
    path = source
    if not isinstance(config, dict):
        raise AgentDispatchRefused(
            f"cannot read MCP config {path} to verify its servers: it is not "
            f"a JSON object")
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise AgentDispatchRefused(
            f"MCP config {path}: \"mcpServers\" must be a JSON object")
    roots = _project_roots(project_dirs)
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise AgentDispatchRefused(
                f"MCP server {name!r} in {path} is not a JSON object "
                f"(R3127-07); fix or remove that entry")
        _check_mcp_db_path(name, server, path)
        _check_mcp_launch(name, server, path, roots)


def _project_roots(
    project_dirs: Iterable[str | os.PathLike[str] | None],
) -> list[str]:
    """Every agent-writable project root, lexically and with symlinks resolved."""
    roots: set[str] = set()
    for directory in [*project_dirs, *PROJECT_DIRS.values()]:
        if not directory:
            continue
        expanded = os.path.expanduser(os.fspath(directory))
        if os.path.isabs(expanded):
            roots.update({os.path.normpath(expanded), os.path.realpath(expanded)})
    return sorted(roots)


def _project_root_holding(path: str, roots: list[str]) -> str | None:
    """The project root ``path`` lies in (as written or resolved), or None."""
    for candidate in {os.path.normpath(path), os.path.realpath(path)}:
        for root in roots:
            try:
                if os.path.commonpath([candidate, root]) == root:
                    return root
            except ValueError:  # different drives (Windows)
                continue
    return None


# Programs that run another program, shells, language runtimes other than
# python and node, and package/task runners: each resolves code (or its
# configuration) from its arguments, environment or cwd in ways the check
# cannot follow, so none may start an MCP server (IR-03). Names are compared
# without version suffixes (ruby3.1 -> ruby, python3-dbg -> python).
_MCP_REFUSED_LAUNCHERS = frozenset({
    "env", "timeout", "nice", "ionice", "stdbuf", "setsid", "nohup", "chrt",
    "taskset", "unshare", "nsenter", "sudo", "doas", "su", "runuser", "xargs",
    "watch", "script", "flock", "time", "strace", "ltrace", "gdb", "valgrind",
    "firejail", "bwrap", "chroot", "busybox", "toybox", "exec",
    "sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "csh", "tcsh", "pwsh",
    "powershell", "cmd",
    "uv", "poetry", "pipx", "pdm", "hatch", "rye", "pipenv", "conda", "mamba",
    "micromamba", "tox", "nox", "ipython", "jupyter",
    "npx", "pnpx", "bunx", "npm", "pnpm", "yarn", "bun", "corepack", "deno",
    "tsx", "ts-node",
    "go", "cargo", "rustup", "make", "gmake", "cmake", "ninja", "just", "rake",
    "docker", "podman", "nerdctl", "kubectl",
    "ruby", "irb", "perl", "php", "java", "jshell", "lua", "luajit", "tclsh",
    "wish", "rscript", "julia", "osascript", "dotnet", "mono", "erl",
    "elixir", "iex", "mix", "gradle", "mvn", "sbt", "scala", "kotlin",
    "groovy", "swift",
    # RR-02: more programs that run the program named in their arguments
    # (setarch x86_64 python3 -m srv), and script interpreters (awk -f).
    "setarch", "linux", "prlimit", "setpriv", "systemd-run", "sg", "newgrp",
    "pkexec", "runcon", "capsh", "xvfb-run", "fakeroot", "fakechroot",
    "eatmydata", "numactl", "catchsegv", "unbuffer", "expect", "faketime",
    "torsocks", "proxychains", "chpst", "daemonize", "start-stop-daemon",
    "cpulimit", "dbus-launch", "dbus-run-session", "screen", "tmux", "ssh",
    "parallel", "entr", "find", "awk", "gawk", "mawk", "nawk", "sed",
    "rbash", "ash", "yash", "posh", "lksh", "oksh", "loksh", "pdksh",
    "elvish", "nu", "xonsh",
})
# Shells, named anywhere in the refused set above or in /etc/shells. A
# renamed copy or hard link of one is found by comparing the binary itself
# (_is_shell_binary).
_ETC_SHELLS = Path("/etc/shells")
_STANDARD_SHELL_PATHS = ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh",
                         "/bin/ksh", "/usr/bin/zsh", "/usr/bin/fish",
                         "/bin/busybox")
# dispatch_config.json key: absolute paths of MCP server executables the
# operator trusts, beyond python -I, node and uvx (RR-02).
MCP_TRUSTED_EXECUTABLES_KEY = "mcp_trusted_executables"
# uvx options that install local code: --with-editable always does.
_UVX_REFUSED_OPTIONS = frozenset({"--with-editable"})
# Server env variables that make an interpreter or the dynamic loader run
# extra code (IR-03).
_MCP_REFUSED_ENV = frozenset({
    "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "PYTHONINSPECT", "PYTHONUSERBASE", "BASH_ENV", "ENV", "PERL5OPT",
    "PERL5LIB", "RUBYOPT", "RUBYLIB", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
})
_MCP_REFUSED_ENV_PREFIXES = ("LD_", "DYLD_")
# Python options accepted before the script or -m. -I is also required.
# -W takes a value; -c, -m and -X are handled or refused separately.
_PYTHON_ALLOWED_FLAGS = frozenset("IBbdEOPqRsSuvW")
_PYTHON_VALUE_OPTIONS = frozenset("cmWX")
# Node options accepted before the script: resource limits and diagnostics.
# Anything else (-r, --require, --import, --loader, -e, -p, -i, --env-file,
# --inspect ...) can load code or open a debugger and is refused.
_NODE_ALLOWED_OPTIONS = frozenset({
    "--max-old-space-size", "--max-semi-space-size", "--stack-size",
    "--enable-source-maps", "--no-warnings", "--no-deprecation",
    "--trace-warnings", "--trace-deprecation", "--trace-uncaught",
    "--unhandled-rejections", "--dns-result-order",
})
# Suffixes that make a relative argument a script, module or config file.
_SCRIPT_SUFFIXES = (".py", ".pyc", ".pyz", ".js", ".mjs", ".cjs", ".ts", ".mts",
                    ".sh", ".bash", ".rb", ".pl", ".php", ".jar", ".toml",
                    ".cfg", ".ini", ".json", ".yaml", ".yml", ".env", ".pth")


def _launcher_stem(command: str) -> str:
    """``command``'s basename, lower-case, without .exe and version suffixes."""
    name = os.path.basename(command).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(suffix)
    return re.sub(r"[0-9.]*(?:-(?:dbg|debug))?$", "", name) or name


@functools.lru_cache(maxsize=1)
def _known_shell_binaries() -> tuple[str, ...]:
    """Resolved paths of the shells installed here (/etc/shells + standard)."""
    candidates = set(_STANDARD_SHELL_PATHS)
    try:
        for line in _ETC_SHELLS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("/"):
                candidates.add(line)
    except OSError:
        pass  # no /etc/shells: the standard paths still apply
    return tuple(sorted({os.path.realpath(path) for path in candidates
                         if os.path.isfile(path)}))


def _is_shell_binary(path: str) -> bool:
    """True when ``path`` is a shell: named like an installed shell, or the
    same file as one, or a byte-identical copy (a renamed dash)."""
    names = {os.path.basename(shell) for shell in _known_shell_binaries()}
    if {os.path.basename(path), os.path.basename(os.path.realpath(path))} & names:
        return True
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    for shell in _known_shell_binaries():
        try:
            if os.path.samefile(path, shell) or (
                    os.path.getsize(shell) == size
                    and filecmp.cmp(path, shell, shallow=False)):
                return True
        except OSError:
            continue
    return False


def _is_dynamic_loader(path: str) -> bool:
    """ld.so / ld-linux-x86-64.so.2 / ld-musl-*.so.1: runs its argument."""
    return any(re.fullmatch(r"ld[\w.-]*\.so(?:\.\d+)*", os.path.basename(name))
               for name in (path, os.path.realpath(path)))


def _launcher_kind(command: str) -> str:
    """"refused", "python", "node", "uvx" or "executable", judged on the name
    as written AND the resolved target (a symlink named srv -> python3), and
    for shells on the binary itself (a renamed copy of dash is a shell)."""
    stems = {_launcher_stem(command), _launcher_stem(os.path.realpath(command))}
    if (stems & _MCP_REFUSED_LAUNCHERS or _is_dynamic_loader(command)
            or _is_shell_binary(command)):
        return "refused"
    if stems & {"python", "pypy"}:
        return "python"
    if stems & {"node", "nodejs"}:
        return "node"
    if "uvx" in stems:
        return "uvx"
    return "executable"


def _trusted_mcp_executables() -> set[str]:
    """Resolved paths listed under mcp_trusted_executables (RR-02).

    An unreadable config or a malformed entry trusts nothing more.
    """
    try:
        config = get_active_dispatch_config()
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("cannot read %s from the dispatch config: %s",
                       MCP_TRUSTED_EXECUTABLES_KEY, exc)
        return set()
    entries = config.get(MCP_TRUSTED_EXECUTABLES_KEY, [])
    if not isinstance(entries, list):
        logger.warning("%s must be a list of absolute paths; ignored",
                       MCP_TRUSTED_EXECUTABLES_KEY)
        return set()
    return {os.path.realpath(entry) for entry in entries
            if isinstance(entry, str) and os.path.isabs(entry)}


def _arg_values(arg: str) -> list[str]:
    """``arg``, plus the value of an ``--opt=value`` argument, so a path
    hidden behind ``=`` is checked like a plain one (RR-02)."""
    if arg.startswith("-") and "=" in arg:
        return [arg, arg.split("=", 1)[1]]
    return [arg]


def _is_db_path_value(args: list[str], index: int) -> bool:
    """The data file of --db-path, which _check_mcp_db_path owns."""
    return (args[index].startswith("--db-path=")
            or (index > 0 and args[index - 1] == "--db-path"))


def _python_invocation(
        args: list[str]) -> tuple[str, str | None, set[str], list[str]]:
    """How ``python <args>`` picks its code.

    Returns (mode, target, single-letter flags, refused options). mode is
    "module" (-m), "code" (-c), "stdin" (no script, or ``-``) or "script".
    Clustered short options (``-IBm mod``) are honoured.
    """
    flags: set[str] = set()
    refused: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        following = args[index + 1] if index + 1 < len(args) else None
        if arg == "-":
            return "stdin", None, flags, refused
        if arg == "--":
            mode = "script" if following else "stdin"
            return mode, following, flags, refused
        if arg.startswith("--"):
            refused.append(arg)  # --check-hash-based-pycs, --help, ...
            index += 1
            continue
        if not arg.startswith("-"):
            return "script", arg, flags, refused
        cluster = arg[1:]
        for position, letter in enumerate(cluster):
            if letter not in _PYTHON_VALUE_OPTIONS:
                flags.add(letter)
                continue
            attached = cluster[position + 1:]
            value = attached or following
            if letter == "m":
                return "module", value, flags, refused
            if letter == "c":
                return "code", value, flags, refused
            flags.add(letter)
            if not attached:
                index += 1  # -W / -X consumed the next argument
            break
        index += 1
    return "stdin", None, flags, refused


def _looks_like_relative_path(arg: str) -> bool:
    if os.path.isabs(arg) or arg.startswith("-"):
        return False
    return (arg.startswith(("./", "../", "~")) or arg in (".", "..")
            or arg.lower().endswith(_SCRIPT_SUFFIXES))


def _check_mcp_launch(name: str, server: dict, config_path: Path,
                      roots: list[str] | None = None) -> None:
    """Refuse a stdio server that is not on the launch allowlist (IR-03)."""
    if server.get("type") in ("http", "sse") or (
            "url" in server and "command" not in server):
        return  # remote server: nothing is started in the project directory
    roots = roots if roots is not None else _project_roots(())

    def refuse(problem: str, fix: str) -> AgentDispatchRefused:
        return AgentDispatchRefused(
            f"MCP server {name!r} in {config_path} {problem}. Agents run in "
            f"the project directory and MCP servers inherit it, so this "
            f"would run agent-writable code. {fix}"
        )

    def refuse_inside_project(what: str, value: str) -> None:
        root = _project_root_holding(value, roots)
        if root is not None:
            raise refuse(f"has its {what} {value!r} inside the project "
                         f"directory {root!r}, which agents can write",
                         f"Install it outside every project directory.")

    command = server.get("command")
    args = server.get("args", [])
    cwd = server.get("cwd")
    env = server.get("env", {})
    if not isinstance(command, str) or not os.path.isabs(command):
        raise refuse(f"has a relative command {command!r}",
                     "Use an absolute path to the executable.")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise refuse("has non-string args", "Use a list of strings.")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        raise refuse(f"has a relative cwd {cwd!r}", "Use an absolute cwd.")
    if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env.items()):
        raise refuse("has an env that is not an object of strings",
                     'Use "env": {"NAME": "value"}.')
    for key in env:
        if key in _MCP_REFUSED_ENV or key.startswith(_MCP_REFUSED_ENV_PREFIXES):
            raise refuse(f"sets {key} in its env, which makes the server load "
                         f"extra code", f"Remove {key} from the env block.")
    refuse_inside_project("command", command)
    if cwd is not None:
        refuse_inside_project("cwd", cwd)

    kind = _launcher_kind(command)
    if kind == "refused":
        raise refuse(
            f"is started through {os.path.basename(command)!r}, a wrapper, "
            f"shell, loader, runtime or package runner that resolves code "
            f"the check cannot verify",
            "Run the server's own executable, an absolute python -I with an "
            "absolute script, or an absolute node with an absolute script.")
    # RR-02: a command that does not exist yet is whatever is put there
    # later; none of the checks below could look at it.
    if not os.path.exists(command):
        raise refuse(f"has the command {command!r}, which does not exist",
                     "Install the server first and point at its executable.")
    if not (os.path.isfile(command) and os.access(command, os.X_OK)):
        raise refuse(f"has the command {command!r}, which is not an "
                     f"executable file", "Point it at the server executable.")
    if kind == "python":
        _check_python_launch(args, cwd, refuse, refuse_inside_project)
    elif kind == "node":
        _check_node_launch(args, refuse, refuse_inside_project)
    elif kind == "uvx":
        _check_uvx_launch(args, refuse, refuse_inside_project)
    elif os.path.realpath(command) not in _trusted_mcp_executables():
        # RR-02: a true allowlist. Any other program may run the program in
        # its arguments (setarch, prlimit, ld.so, awk -f ...), and a list of
        # such programs is never complete.
        raise refuse(
            f"runs {command!r}, which is not an allowlisted MCP server "
            f"executable",
            f"Run it as an absolute python -I or node script, through uvx, "
            f"or add its absolute path to \"{MCP_TRUSTED_EXECUTABLES_KEY}\" "
            f"in dispatch_config.json if it is the server's own program.")
    for index, arg in enumerate(args):
        for value in _arg_values(arg):
            if _looks_like_relative_path(value):
                raise refuse(f"has the relative path argument {arg!r}",
                             "Use an absolute path.")
            if os.path.isabs(value) and not _is_db_path_value(args, index):
                refuse_inside_project("argument", value)


def _check_python_launch(args: list[str], cwd: str | None, refuse: Any,
                         refuse_inside_project: Any) -> None:
    mode, target, flags, long_options = _python_invocation(args)
    if long_options:
        raise refuse(f"passes python the option {long_options[0]!r}",
                     "Use only short isolation and warning flags (-I, -B, "
                     "-u, -W ...).")
    unknown = sorted(flags - _PYTHON_ALLOWED_FLAGS)
    if unknown:
        raise refuse(f"passes python the option -{unknown[0]}, which is not "
                     f"allowed", "Use only -I and -B, -b, -d, -E, -O, -P, -q, "
                     "-R, -s, -S, -u, -v, -W.")
    shown = {"module": f"-m {target}", "code": "-c ...", "stdin": "(stdin)",
             "script": str(target)}[mode]
    if mode in ("code", "stdin"):
        raise refuse(f"runs python {shown}, which cannot be verified",
                     "Run an absolute script with python -I.")
    if "I" not in flags:
        raise refuse(
            f"runs python {shown} without -I; Python would read PYTHON* "
            f"variables and put the project directory first on sys.path",
            'Add "-I" and run an absolute script (or -m with an absolute '
            '"cwd" outside every project directory).')
    if mode == "script":
        if not target or not os.path.isabs(target):
            raise refuse(f"runs the relative Python script {target!r}",
                         "Use an absolute script path.")
        refuse_inside_project("script", target)
    elif cwd is None:
        raise refuse(f"runs python {shown} without an absolute \"cwd\"; the "
                     f"project directory would be its working directory "
                     f"(sys.path)",
                     'Add an absolute "cwd" outside every project directory, '
                     "or run an absolute script.")


def _check_node_launch(args: list[str], refuse: Any,
                       refuse_inside_project: Any) -> None:
    script = None
    for index, arg in enumerate(args):
        if arg == "--":
            script = args[index + 1] if index + 1 < len(args) else None
            break
        if not arg.startswith("-") or arg == "-":
            script = arg
            break
        option = arg.split("=", 1)[0]
        if option not in _NODE_ALLOWED_OPTIONS:
            raise refuse(f"passes node the option {arg!r}, which can load "
                         f"code (preload, loader, eval, env file, debugger)",
                         "Remove it; bundle what it loads into the server "
                         "script.")
    if script is None or script == "-":
        raise refuse("runs node without a script", "Run an absolute script.")
    if not os.path.isabs(script):
        raise refuse(f"runs the relative node script {script!r}",
                     "Use an absolute script path.")
    refuse_inside_project("script", script)


# uvx options whose value is a local file or directory (RR-02); a bare name
# there (req.txt) is relative to the project directory too.
_UVX_PATH_OPTIONS = frozenset({
    "--directory", "--project", "--config-file", "--env-file",
    "--with-requirements", "--constraints", "--overrides",
    "--build-constraints", "--find-links", "--cache-dir",
})
# uvx options whose value is a package index: a URL, or a local directory
# (``--index name=./dir`` too), where a bare name is relative as well.
_UVX_INDEX_OPTIONS = frozenset({
    "--index", "--default-index", "--index-url", "--extra-index-url",
})
# uvx options whose value is a path only when it looks like one: --python
# 3.12 is a version, and the short aliases of path options (-c, -b, -f, -i)
# are scanned in the tool's own arguments too, where they can mean other
# things (-b 0.0.0.0).
_UVX_MAYBE_PATH_OPTIONS = frozenset({"--python", "-p", "-c", "-b", "-f", "-i"})
# uvx options whose value is a package: a name, or a local path or file: URL.
_UVX_PACKAGE_OPTIONS = frozenset({"--from", "--with", "-w"})
# Every short uvx option that takes a value, so an attached value (-w./pkg,
# -w=./pkg) or a cluster of flags ending in one (-qw ./pkg) is read too.
_UVX_SHORT_VALUE_LETTERS = frozenset("wcbifpPC")
# A file: URL names a local path wherever it appears.
_FILE_URL = re.compile(r"file:(?://(?:localhost)?)?([^\s#?]+)")


def _is_path_like(value: str) -> bool:
    return (value in (".", "..") or value.startswith(("/", "./", "../", "~"))
            or "/" in value)


def _uvx_option(args: list[str], index: int) -> tuple[str, str] | None:
    """``(option, value)`` when ``args[index]`` is an option, else None.

    Reads ``--opt value``, ``--opt=value``, ``-o value``, ``-ovalue``,
    ``-o=value`` and a cluster of short flags ending in a value option
    (``-qw value``). ``value`` is the next argument when none is attached,
    whether or not the option takes one; callers only look at the value of
    options they know take one.
    """
    arg = args[index]
    following = args[index + 1] if index + 1 < len(args) else ""
    if arg.startswith("--"):
        option, has_value, attached = arg.partition("=")
        return option, attached if has_value else following
    if not arg.startswith("-") or len(arg) < 2:
        return None
    for position, letter in enumerate(arg[1:], start=1):
        if letter in _UVX_SHORT_VALUE_LETTERS:
            attached = arg[position + 1:]
            attached = attached[1:] if attached.startswith("=") else attached
            return f"-{letter}", attached or following
        if not letter.isalpha():
            break
    return arg, following


def _index_paths(value: str) -> list[str]:
    """Local directories a uvx index option reads: ``./idx``, ``idx``,
    ``/abs/idx``, ``name=./idx``. A remote URL gives none; a ``file:`` URL
    is checked by the caller's file: URL scan."""
    name, has_name, location = value.partition("=")
    if has_name and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        value = location
    if not value or "://" in value or value.startswith("file:"):
        return []
    return [value]


def _local_package_paths(spec: str) -> list[str]:
    """Local paths a uvx package spec installs from: ``.``, ``./srv``,
    ``/abs/dir``, ``srv @ ./dir``, ``file:///abs/dir``, ``x.whl``.

    A package name (``mcp-server-sqlite==0.6``) or a remote URL gives none.
    """
    paths = [match.group(1) for match in _FILE_URL.finditer(spec)]
    for part in spec.split("@"):
        part = part.strip()
        if part and "://" not in part and not part.startswith("file:") and (
                _is_path_like(part)
                or part.endswith((".whl", ".tar.gz", ".zip"))):
            paths.append(part)
    return paths


def _check_uvx_launch(args: list[str], refuse: Any,
                      refuse_inside_project: Any) -> None:
    """uvx installs a package and runs it (RR-02).

    Refused: ``--with-editable`` (it always installs a local directory), and
    ``--from`` / ``--with`` / index / path options (long or short, value
    attached or not) naming a relative path, which resolves in the
    agent-writable project directory, or a path inside a project; so is a
    ``file:`` URL anywhere that names one. Every argument is scanned, the
    tool's own too: the tool name cannot be told from an option value
    without uvx's option table.
    """
    for index, arg in enumerate(args):
        checked: list[tuple[str, str, list[str]]] = [
            ("file: URL", arg, [match.group(1)
                                for match in _FILE_URL.finditer(arg)])]
        parsed = _uvx_option(args, index)
        if parsed is not None:
            option, value = parsed
            if option in _UVX_REFUSED_OPTIONS:
                raise refuse(f"passes uvx {option}, which installs a local "
                             f"directory an agent can write",
                             "Install the server from a package index, or "
                             "from an absolute path outside every project "
                             "directory.")
            if option in _UVX_PACKAGE_OPTIONS:
                checked.append((option, value, _local_package_paths(value)))
            elif option in _UVX_PATH_OPTIONS:
                checked.append((option, value,
                                [] if "://" in value else [value]))
            elif option in _UVX_INDEX_OPTIONS:
                checked.append((option, value, _index_paths(value)))
            elif (option in _UVX_MAYBE_PATH_OPTIONS and _is_path_like(value)
                  and "://" not in value):
                checked.append((option, value, [value]))
        for option, value, paths in checked:
            for path in paths:
                expanded = os.path.expanduser(path)
                if not os.path.isabs(expanded):
                    raise refuse(f"passes uvx {option} {value!r}, a path "
                                 f"relative to the project directory",
                                 "Use a package from an index, or an absolute "
                                 "path outside every project directory.")
                refuse_inside_project(f"uvx {option} path", expanded)


_DB_PATH_PLACEHOLDER = "/absolute/path/to/theforge.db"


def _check_mcp_db_path(name: str, server: dict, path: Path) -> None:
    """Refuse a relative ``--db-path`` (sandbox-11), saying how to fix it."""
    args = server.get("args")
    if isinstance(args, list):
        for index, arg in enumerate(args):
            if arg == "--db-path":
                db_path = args[index + 1] if index + 1 < len(args) else ""
            elif isinstance(arg, str) and arg.startswith("--db-path="):
                db_path = arg.split("=", 1)[1]
            else:
                continue
            if not isinstance(db_path, str) or not os.path.isabs(db_path):
                # IR-02: name the file and the exact edit. RR-04: suggest no
                # path. Resolving the relative value from the orchestrator's
                # cwd named a stale copy of the database on a real host, and
                # an operator who pasted it would silently point every agent
                # at it. Say where the live database is configured instead.
                raise AgentDispatchRefused(
                    f"MCP server {name!r} in {path} has a relative --db-path "
                    f"{db_path!r}. Agents run in the project directory, so it "
                    f"would open or create a different database there. Every "
                    f"dispatch is refused until this is fixed. Fix: edit "
                    f"{path} and set the --db-path argument of {name!r} to the "
                    f"absolute path of the live TheForge database: "
                    f"\"--db-path\", \"{_DB_PATH_PLACEHOLDER}\". EQUIPA does "
                    f"not guess it (a copy found from the current directory "
                    f"can be stale). Use the database the orchestrator itself "
                    f"opens, equipa.constants.THEFORGE_DB: the \"theforge_db\" "
                    f"entry of forge_config.json next to forge_orchestrator.py, "
                    f"else the THEFORGE_DB environment variable, else "
                    f"theforge.db beside the equipa package. Resolve symlinks "
                    f"(realpath) so both name the same file."
                )


def _agent_subprocess_env() -> dict[str, str]:
    """Allowlisted environment for an agent CLI (loop-03 / sandbox-03).

    The passthrough list comes from the active dispatch config. If that
    cannot be loaded, no names are added: fewer variables, never more.
    """
    return active_agent_env()


@contextlib.contextmanager
def build_cli_command(
    system_prompt: str | PromptResult,
    project_dir: str,
    max_turns: int,
    model: str,
    role: str = "developer",
    streaming: bool = False,
    prompt_message: str | None = None,
    dispatch_config: dict | None = None,
) -> Iterator[list[str]]:
    """Build the claude CLI command as a context manager that owns its tempfile.

    Yields the command list. On exit (normal or exceptional), the temp file
    holding the system prompt is removed. Callers MUST consume the cmd while
    inside the ``with`` block::

        with build_cli_command(prompt, dir, 50, "opus") as cmd:
            result = await run_agent(cmd)

    Args:
        system_prompt: Full system prompt string, or PromptResult from
            build_system_prompt(). PromptResult is coerced to str via
            __str__() which returns the full prompt with boundary marker.
        streaming: If True, use stream-json output format for real-time monitoring.
        prompt_message: Optional override for the user-facing -p message. Defaults
            to a generic "Execute the task..." instruction. Manager-mode dispatch
            (planner/evaluator) supplies role-specific text here.

    Yields:
        The argv list to pass to ``asyncio.create_subprocess_exec`` /
        ``subprocess.run``. The list contains a path to a tempfile that is
        deleted when the context exits — do NOT retain ``cmd`` past the
        ``with`` block.
    """
    # Explicit str() ensures PromptResult.__str__() is called, producing
    # the full prompt with SYSTEM_PROMPT_DYNAMIC_BOUNDARY marker.
    prompt_str = str(system_prompt)
    output_format = "stream-json" if streaming else "json"
    user_prompt = prompt_message or (
        f"Execute the task described in your system prompt. Work in: {project_dir}"
    )
    claude_bin = shutil.which("claude") or "claude"
    # sandbox-11: checked before any tempfile exists, so a refusal leaks none.
    _check_mcp_servers(MCP_CONFIG, (project_dir,))

    # Write system prompt to a temp file to avoid Windows command-line length
    # limits (WinError 206, ~8191 chars). delete=False so the async subprocess
    # can reopen it; the finally-block below removes it on context exit.
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="equipa_prompt_",
        delete=False, encoding="utf-8",
    )
    # Populated only when the pre-execution bash gate is enabled; cleaned up
    # alongside the prompt file in the finally block.
    settings_file_name: str | None = None
    try:
        prompt_file.write(prompt_str)
        prompt_file.close()

        cmd = [
            claude_bin,
            "-p",
            user_prompt,
            "--output-format", output_format,
            "--model", model,
            "--max-turns", str(max_turns),
            "--no-session-persistence",
            "--append-system-prompt-file", prompt_file.name,
            "--mcp-config", str(MCP_CONFIG),
            # IR-01: the CLI runs in the agent-writable project directory, so
            # load user settings only (a project .claude/settings.json could
            # set disableAllHooks and switch off the Bash gate; CLAUDE.md
            # would plant instructions) and only EQUIPA's MCP servers (no
            # project .mcp.json). See equipa/cli_isolation.py.
            *CLAUDE_CLI_ISOLATION_ARGS,
            "--add-dir", str(project_dir),
            "--permission-mode", "bypassPermissions",
        ]

        # Resolve dispatch config once: prefer the caller-supplied config
        # (threaded from the dispatch layer, or injected by tests), otherwise
        # load it from disk. Used both for the optional --effort flag and the
        # flag-gated pre-execution bash security hook below.
        _dc: dict = dispatch_config if isinstance(dispatch_config, dict) else {}
        if not _dc:
            try:
                _dc = load_dispatch_config(None) or {}
            except (ImportError, FileNotFoundError, OSError, KeyError, ValueError):
                _dc = {}  # config missing/unloadable → defaults everywhere below

        # Load effort flag from dispatch_config — production-only config-driven setting.
        # When dispatch_config.json has 'effort' set (e.g. "high"/"xhigh"/"max"), pass
        # it to the Claude CLI for extended thinking. No-op when unset (CLI uses default).
        _effort = _dc.get("effort")
        if _effort:
            cmd.extend(["--effort", _effort])

        # Flag-gated (features.bash_security_pretooluse, DEFAULT FALSE) TRUE
        # pre-execution bash security gate. When enabled, generate a --settings
        # file wiring a Claude Code PreToolUse hook on the Bash tool that runs
        # equipa.bash_security.check_bash_command BEFORE the tool executes and
        # blocks unsafe commands (hook exit 2). When disabled the cmd list is
        # byte-for-byte identical to the pre-2703 behavior (zero change).
        # The reactive stream check in the streaming loop stays on regardless
        # (defense-in-depth / belt-and-braces).
        if is_feature_enabled(_dc, "bash_security_pretooluse"):
            if PRETOOLUSE_HOOK_SCRIPT.is_file():
                settings_payload = _pretooluse_settings_payload(
                    PRETOOLUSE_HOOK_SCRIPT, sys.executable or "python3"
                )
                settings_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", prefix="equipa_settings_",
                    delete=False, encoding="utf-8",
                )
                try:
                    json.dump(settings_payload, settings_file)
                    settings_file.close()
                    settings_file_name = settings_file.name
                    cmd.extend(["--settings", settings_file_name])
                except OSError:
                    logger.warning(
                        "Failed to write PreToolUse settings file; skipping "
                        "pre-execution bash gate for this run", exc_info=True,
                    )
                    try:
                        os.unlink(settings_file.name)
                    except OSError:
                        pass
            else:
                logger.warning(
                    "bash_security_pretooluse enabled but hook script missing "
                    "at %s; skipping pre-execution gate", PRETOOLUSE_HOOK_SCRIPT,
                )

        # stream-json requires --verbose
        if streaming:
            cmd.append("--verbose")

        # Load role-specific skills directory if it exists
        skills_dir = ROLE_SKILLS.get(role)
        if skills_dir and skills_dir.exists():
            cmd.extend(["--add-dir", str(skills_dir)])

        # R3136-03: with agent_isolation on, reviewer units run alone.
        with isolation.unit_role(role):
            yield cmd
    finally:
        # Idempotent: missing_ok=True means a second cleanup (or one after a
        # partial setup failure) does not raise.
        try:
            os.unlink(prompt_file.name)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "Failed to remove agent prompt tempfile: %s", prompt_file.name,
                exc_info=True,
            )
        # Remove the generated PreToolUse settings file, if one was written.
        if settings_file_name is not None:
            try:
                os.unlink(settings_file_name)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "Failed to remove agent settings tempfile: %s",
                    settings_file_name, exc_info=True,
                )


def _evaluate_paralysis_retry_read_gate(
    paralysis_retry_count: int,
    turn_count: int,
    tool_name: str,
    has_any_file_change: bool,
    must_write_next_turn: bool,
) -> tuple[str | None, bool]:
    """Decide how to handle a read-only first tool call on a paralysis retry.

    Pure helper so the gate logic is unit-testable. Mirrors the in-loop check
    inside ``_run_agent_streaming_impl``.

    Behavior:
        * Not on a paralysis retry, agent already wrote, or tool is not read-only
          → no-op (None, must_write_next_turn unchanged).
        * On the FIRST paralysis retry (retry_count == 1) → no-op. The agent
          still has a reduced ``effective_kill_turns`` budget and normal
          warn/final-warn/kill escalation, but it can read 3-4 files before
          writing. Legitimately hard design tasks (novel wiring seams, DataContext
          injection, etc.) need multiple reads before they can produce a correct
          first edit — clamping to 1 read on the first retry killed them before
          they could start (task #2611 evidence: turns=2/400, PID 343414).
        * On the SECOND paralysis retry and beyond (retry_count >= 2) with a
          read-only tool on turn 1 (and we have not already armed
          must_write_next_turn) → allow ONE read and arm must_write_next_turn
          so the NEXT call must be Edit/Write or the agent dies on the regular
          paralysis path.

    The pre-2026-05-03 behavior killed instantly on retry >= 2 even on turn 1.
    That made refactor tasks unsatisfiable: after a paralysis kill the agent
    has zero forward-context (soft_checkpoint only stores the file list at
    kill time, not file contents) and must read at least one file to know what
    to edit. Forbidding all reads guaranteed a kill loop. The single-read
    allowance on retry >= 2 is preserved by the regular FAST_ESCALATION +
    must_write logic. The 30-min wall-clock cap and PARALYSIS_CYCLE_HARD_CAP
    remain as the true-wedge backstop for agents that never write (task #2604).

    Args:
        paralysis_retry_count: 0 on the first attempt, 1+ after each paralysis
            kill.
        turn_count: 1-indexed agent turn count.
        tool_name: Name of the tool being invoked (e.g. "Read", "Edit").
        has_any_file_change: True once the agent has produced any file change.
        must_write_next_turn: Current state of the must-write enforcement flag.

    Returns:
        Tuple of (early_term_reason, must_write_next_turn). early_term_reason
        is always None now — the prior kill branch was unsatisfiable.
    """
    # Gate is inactive on the first attempt (retry_count == 0) and the first
    # paralysis retry (retry_count == 1). First retry gets reduced kill
    # threshold via effective_kill_turns but no instant 1-read clamp — hard
    # tasks need multiple reads before they can write (task #2611).
    if paralysis_retry_count <= 1 or has_any_file_change:
        return None, must_write_next_turn
    if tool_name not in ("Read", "Grep", "Glob", "Agent"):
        return None, must_write_next_turn
    if turn_count == 1 and not must_write_next_turn:
        return None, True
    return None, must_write_next_turn


# --- Agent process containment (gate-05) --------------------------------------
#
# On Linux every agent CLI runs under equipa/agent_launcher.py, a per-agent
# supervisor that is a child subreaper. The real Claude CLI starts each
# Bash-tool shell in a NEW session, so a ``nohup`` watcher an agent launches is
# never in the CLI's process group; as a subreaper descendant it is still
# found and killed by the launcher when the CLI exits. The launcher also
# carries PR_SET_PDEATHSIG, so it cleans up by itself if the orchestrator dies.
#
# The launcher is started with start_new_session=True, so its process group
# (pgid == launcher pid) is a second layer: if the launcher does not finish its
# own cleanup in time, the orchestrator SIGKILLs that group.
#
# Everything here is Linux-only. Elsewhere the CLI is spawned directly and
# killed by pid, exactly as before.

# Seconds the launcher gives the CLI after a forwarded SIGTERM, and each
# descendant between SIGTERM and SIGKILL.
AGENT_TERMINATION_GRACE_SECONDS = 3.0
# How long to wait for a signalled launcher to finish its own cleanup (CLI
# grace + descendant grace + SIGKILL phase, plus slack) before the
# orchestrator SIGKILLs the launcher's process group itself.
_LAUNCHER_EXIT_TIMEOUT_SECONDS = (
    2 * AGENT_TERMINATION_GRACE_SECONDS
    + agent_launcher.KILL_PHASE_TIMEOUT_SECONDS
    + 3.0
)
# Upper bound on waiting for the group to empty after the SIGKILL layer.
_GROUP_KILL_TIMEOUT_SECONDS = 5.0
_CONTAINMENT_POLL_SECONDS = 0.05

# Agents spawned and not yet fully terminated. The atexit handler terminates
# whatever is left synchronously, so an interrupted orchestrator cannot leave
# an agent behind just because the event loop that would have escalated was
# shut down first (PT-02).
_LIVE_CONTAINED_AGENTS: set[_ContainedAgent] = set()
_exit_handler_registered = False


class AgentContainmentError(RuntimeError):
    """The agent could not be verified as contained, so it was not run."""


def _agent_containment_supported() -> bool:
    """True where agents are spawned through the subreaper launcher.

    Evaluated per spawn, not at import, so tests can force the fallback.
    """
    return (
        agent_launcher.is_supported_platform()
        and hasattr(os, "killpg")
        and bool(sys.executable)
        and agent_launcher.LAUNCHER_PATH.is_file()
    )


class _ContainedAgent:
    """Handle on one launcher process and the process group it leads.

    Signals are pinned to the launcher's identity (PT-04): the leader is only
    signalled through a pidfd opened right after spawn, or after a /proc
    start-time check where pidfds are unavailable, never by a raw pid that
    may already have been reaped and recycled. The group is only signalled
    while it is provably still this launcher's group.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        # getattr: a spawn stand-in without a pid must fail closed in verify()
        self.pid = getattr(process, "pid", None)
        self.pidfd: int | None = None
        self.start_time: int | None = None
        self.closed = False
        # A forked child inherits the registry and the exit handler; only the
        # process that spawned the launcher may terminate it at exit.
        self.owner_pid = os.getpid()

    def verify(self) -> None:
        """Pin the launcher's identity and check that it leads its own group.

        Fails closed (PT-05): raises AgentContainmentError when the pid is not
        a real process id, the process is already gone, or it is not the
        leader of a group of its own. Group termination would otherwise miss
        the agent, or hit the orchestrator's own group.
        """
        if not isinstance(self.pid, int) or self.pid <= 1:
            raise AgentContainmentError(f"invalid agent pid {self.pid!r}")
        if (hasattr(os, "pidfd_open")
                and hasattr(signal, "pidfd_send_signal")):
            try:
                self.pidfd = os.pidfd_open(self.pid)
            except ProcessLookupError as exc:
                raise AgentContainmentError(
                    f"agent launcher {self.pid} exited before it could be "
                    "verified") from exc
            except OSError:
                self.pidfd = None  # no kernel support: start time guards it
        self.start_time = agent_launcher.proc_start_time(self.pid)
        try:
            pgid = os.getpgid(self.pid)
        except ProcessLookupError as exc:
            raise AgentContainmentError(
                f"agent launcher {self.pid} exited before it could be "
                "verified") from exc
        if (self.start_time is None or pgid != self.pid
                or pgid == os.getpgrp()):
            raise AgentContainmentError(
                f"agent launcher {self.pid} is not the leader of its own "
                f"process group (pgid {pgid})")

    def leader_exited(self) -> bool:
        """True once the launcher has exited (reaped or still a zombie)."""
        if self.process.returncode is not None:
            return True
        if self.pidfd is not None:
            # A pidfd polls readable as soon as its process exits. poll(),
            # not select(): a long-running orchestrator can hold fds >= 1024.
            poller = select.poll()
            poller.register(self.pidfd, select.POLLIN)
            try:
                return bool(poller.poll(0))
            except OSError:
                pass  # fall through to the /proc check
        stat = agent_launcher.read_proc_stat(self.pid)
        return stat is None or stat[0] == "Z" or stat[3] != self.start_time

    def signal_leader(self, sig: int) -> bool:
        """Signal the launcher itself. False if it is gone or released."""
        if self.closed:
            return False
        if self.pidfd is not None:
            try:
                signal.pidfd_send_signal(self.pidfd, sig)
            except ProcessLookupError:
                return False
            except OSError as exc:
                logger.warning("[ProcessTree] cannot signal agent launcher "
                               "%d (signal %d): %s", self.pid, sig, exc)
                return False
            return True
        if self.start_time is None or self.leader_exited():
            return False
        return agent_launcher.signal_process_identity(
            self.pid, self.start_time, sig)

    def request_termination(self) -> None:
        """Ask the launcher to stop the agent. Never blocks.

        The launcher forwards SIGTERM to the CLI, SIGKILLs it after the
        grace period, then sweeps every descendant. That escalation runs in
        the launcher process, so no asyncio task has to survive for it.
        """
        self.signal_leader(signal.SIGTERM)

    def _group_is_ours(self) -> bool:
        pgid = self.pid
        if pgid <= 1 or pgid == os.getpgrp():
            logger.error("[ProcessTree] refusing to signal process group %d",
                         pgid)
            return False
        # Either the launcher still holds its pid (alive or a zombie), or
        # nobody does, in which case any live member still pins the pgid and
        # the kernel cannot hand that pid out again. A different start time
        # means the group emptied and the pid was recycled: not ours.
        leader_start = agent_launcher.proc_start_time(pgid)
        return leader_start is None or leader_start == self.start_time

    def _kill_leader_descendants(self) -> None:
        """Last resort when the launcher did not finish its sweep in time.

        While the launcher is alive it is the subreaper of every process the
        agent started, including setsid'd ones outside its group, so they
        are SIGKILLed here directly before the group SIGKILL takes the
        launcher down and orphans them to init. Skipped once the launcher's
        pid no longer belongs to it (exited and reaped, or recycled).
        """
        if agent_launcher.proc_start_time(self.pid) != self.start_time:
            return
        descendants = agent_launcher.list_descendants(self.pid)
        if descendants:
            logger.warning("[ProcessTree] SIGKILLing descendants %s of "
                           "agent launcher %d", sorted(descendants), self.pid)
        for pid, start_time in descendants.items():
            agent_launcher.signal_process_identity(pid, start_time,
                                                   signal.SIGKILL)

    def _kill_group(self) -> bool:
        """SIGKILL every live group member. True once the group is empty.

        Members are signalled one by one, each pinned to the identity seen
        in the /proc scan (PT-04), rather than with ``killpg``: by now the
        launcher has usually been reaped, and a raw group signal would go to
        whichever group holds the id at that instant. A member forked after
        the scan is caught by the caller's next round; a SIGKILLed process
        cannot fork again, so the rounds converge.
        """
        if not self._group_is_ours():
            return True
        members = agent_launcher.list_group_members(self.pid)
        if not members:
            return True
        # The /proc scan above takes a while on a busy host. Re-check before
        # signalling that the group id was not recycled while it ran.
        if not self._group_is_ours():
            return True
        logger.warning("[ProcessTree] agent process group %d still has "
                       "members %s; sending SIGKILL", self.pid, sorted(members))
        for pid, start_time in members.items():
            agent_launcher.signal_process_identity(pid, start_time,
                                                   signal.SIGKILL)
        return False

    def terminate_sync(self) -> None:
        """Blocking, bounded termination for contexts that cannot await:
        cancellation, loop shutdown and interpreter exit (PT-02)."""
        if self.closed:
            return
        try:
            if not self.leader_exited():
                self.request_termination()
                deadline = time.monotonic() + _LAUNCHER_EXIT_TIMEOUT_SECONDS
                while not self.leader_exited():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "[ProcessTree] agent launcher %d did not finish "
                            "its cleanup in %.1fs", self.pid,
                            _LAUNCHER_EXIT_TIMEOUT_SECONDS)
                        self._kill_leader_descendants()
                        break
                    time.sleep(min(remaining, _CONTAINMENT_POLL_SECONDS))
            deadline = time.monotonic() + _GROUP_KILL_TIMEOUT_SECONDS
            while not self._kill_group():
                if time.monotonic() >= deadline:
                    logger.error("[ProcessTree] agent process group %d "
                                 "survived SIGKILL", self.pid)
                    break
                time.sleep(_CONTAINMENT_POLL_SECONDS)
        finally:
            self.release()

    async def terminate(self) -> None:
        """Terminate the agent and reap the launcher. Idempotent.

        After a normal exit the launcher has already swept the tree, so this
        only confirms the group is empty. If the coroutine is cancelled
        part-way, termination finishes synchronously before re-raising.
        """
        if self.closed:
            return
        try:
            await self._terminate_async()
        except BaseException:
            self.terminate_sync()
            raise
        self.release()

    async def _terminate_async(self) -> None:
        if not self.leader_exited():
            self.request_termination()
        try:
            await asyncio.wait_for(self.process.wait(),
                                   timeout=_LAUNCHER_EXIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("[ProcessTree] agent launcher %d did not finish "
                           "its cleanup in %.1fs", self.pid,
                           _LAUNCHER_EXIT_TIMEOUT_SECONDS)
            self._kill_leader_descendants()
        deadline = time.monotonic() + _GROUP_KILL_TIMEOUT_SECONDS
        while not self._kill_group():
            if time.monotonic() >= deadline:
                logger.error("[ProcessTree] agent process group %d survived "
                             "SIGKILL", self.pid)
                break
            await asyncio.sleep(_CONTAINMENT_POLL_SECONDS)
        if self.process.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(),
                                       timeout=_GROUP_KILL_TIMEOUT_SECONDS)

    def release(self) -> None:
        """Stop tracking this agent. Later signal requests are no-ops."""
        if self.closed:
            return
        self.closed = True
        _LIVE_CONTAINED_AGENTS.discard(self)
        if self.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.pidfd)
            self.pidfd = None


def _terminate_live_agents_at_exit() -> None:
    """atexit: synchronously terminate every agent that is still tracked.

    All launchers are asked first, so their cleanups run in parallel. Agents
    spawned by another process (this one is a fork of the orchestrator) are
    not ours to stop: the fork only inherited the registry.
    """
    agents = [agent for agent in _LIVE_CONTAINED_AGENTS
              if agent.owner_pid == os.getpid()]
    for agent in agents:
        agent.request_termination()
    for agent in agents:
        try:
            agent.terminate_sync()
        except Exception:  # noqa: BLE001 - every remaining agent must be tried
            logger.exception("[ProcessTree] failed to terminate agent "
                             "launcher %d at exit", agent.pid)


def _track_live_agent(agent: _ContainedAgent) -> None:
    global _exit_handler_registered
    if not _exit_handler_registered:
        atexit.register(_terminate_live_agents_at_exit)
        _exit_handler_registered = True
    _LIVE_CONTAINED_AGENTS.add(agent)


async def _spawn_agent_process(
    cmd: list[str], project_dir: str | None = None, **kwargs: Any,
) -> tuple[asyncio.subprocess.Process, _ContainedAgent | None]:
    """Start the agent CLI with piped stdout/stderr, contained where possible.

    The CLI (and the launcher in front of it) gets the allowlisted
    environment and runs in the project directory: ``project_dir``, else the
    first ``--add-dir`` of ``cmd`` (sandbox-03, sandbox-11).

    Returns the process to read from and its containment handle (None on
    platforms without the launcher). Raises FileNotFoundError when the
    command is not on PATH, as a direct spawn would, AgentDispatchRefused
    when the project directory is missing or the MCP config has a relative
    --db-path, and AgentContainmentError when the launcher cannot be
    verified; the launcher is stopped first.
    """
    cwd = project_dir or _cmd_option(cmd, "--add-dir")
    if cwd is not None and not os.path.isdir(cwd):
        raise AgentDispatchRefused(f"project directory {cwd!r} does not exist")
    if cmd and is_claude_cli(cmd[0]):
        # IR-01 backstop for every caller (reflexion, manager, custom argv):
        # whatever built the argv, project-scope settings, CLAUDE.md and
        # .mcp.json in the cwd are never loaded.
        try:
            cmd = isolate_claude_argv(cmd)
        except ValueError as exc:
            raise AgentDispatchRefused(str(exc)) from exc
    # RR-05: every --mcp-config value, both spellings, files and inline JSON.
    for mcp_config in mcp_config_values(cmd):
        _check_mcp_config_value(mcp_config, cwd)
    # P2A-05: the scrubbed env below is pointless if the agent can read ours
    # from /proc/<pid>/environ. build_agent_env() already tries; on Linux a
    # failure refuses the dispatch instead of starting an agent anyway.
    if (not protect_orchestrator_process()
            and sys.platform.startswith("linux")):
        raise AgentDispatchRefused(
            "cannot make the orchestrator non-dumpable (PR_SET_DUMPABLE); "
            "an agent could read its environment from /proc"
        )
    kwargs["env"] = _agent_subprocess_env()
    kwargs["cwd"] = cwd

    if isolation.isolation_enabled():
        # Task 3135: separate agent UID, per-agent cgroup and clone; refused,
        # never downgraded, when isolation cannot be established.
        try:
            return await isolation.spawn_isolated_agent(
                cmd, cwd, kwargs["env"], limit=kwargs.get("limit"))
        except isolation.AgentIsolationError as exc:
            raise AgentDispatchRefused(f"agent isolation: {exc}") from exc

    if not _agent_containment_supported():
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **kwargs,
        )
        return process, None

    executable = shutil.which(cmd[0])
    if executable is None:
        raise FileNotFoundError(f"command not found: {cmd[0]}")
    launcher_cmd = [
        sys.executable, "-I", str(agent_launcher.LAUNCHER_PATH),
        "--parent-pid", str(os.getpid()),
        "--grace", str(AGENT_TERMINATION_GRACE_SECONDS),
        "--executable", os.path.abspath(executable),
        "--", *cmd,
    ]
    process = await asyncio.create_subprocess_exec(
        *launcher_cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, **kwargs,
    )
    agent = _ContainedAgent(process)
    try:
        agent.verify()
    except AgentContainmentError:
        logger.exception("[ProcessTree] agent containment check failed")
        # Only the launcher itself is signalled: its group is unverified.
        agent.request_termination()
        try:
            await asyncio.wait_for(process.wait(),
                                   timeout=_LAUNCHER_EXIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            agent.signal_leader(signal.SIGKILL)
        finally:
            # Also when cancelled while waiting: the SIGTERMed launcher
            # sweeps its own tree, and the pidfd must not leak.
            agent.release()
        raise
    _track_live_agent(agent)
    return process, agent


def _request_agent_termination(
    process: asyncio.subprocess.Process, agent: _ContainedAgent | None,
) -> None:
    """Non-blocking kill request, for abort handlers."""
    if agent is not None:
        agent.request_termination()
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _terminate_agent(
    process: asyncio.subprocess.Process, agent: _ContainedAgent | None,
) -> None:
    """Terminate the agent on a timeout, early termination or normal exit.

    Contained agents have their whole tree swept; without the launcher the
    CLI is killed by pid if it is still running, as before.
    """
    if agent is not None:
        await agent.terminate()
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def _terminate_agent_sync(
    process: asyncio.subprocess.Process, agent: _ContainedAgent | None,
) -> None:
    """Blocking variant for cancellation and loop shutdown (PT-02)."""
    if agent is not None:
        agent.terminate_sync()
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def _containment_failure_result(exc: AgentContainmentError) -> AgentResult:
    return {
        "success": False,
        "result_text": "",
        "num_turns": 0,
        "duration": 0,
        "cost": None,
        "errors": [f"Agent not started: containment check failed: {exc}"],
        "files_changed_set": [],
    }


def _dispatch_refused_result(exc: AgentDispatchRefused) -> AgentResult:
    logger.error("[Dispatch] agent refused: %s", exc)
    return {
        "success": False,
        "result_text": "",
        "num_turns": 0,
        "duration": 0,
        "cost": None,
        "errors": [f"Agent dispatch refused: {exc}"],
        "files_changed_set": [],
    }


async def run_agent(
    cmd: list[str],
    timeout: int | None = None,
    max_retries: int = 10,
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
    persistent_max_attempts: int | None = None,
    project_dir: str | None = None,
) -> AgentResult:
    """Spawn claude -p with retry logic and exponential backoff.

    Implements Claude Code withRetry.ts pattern:
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - Retries on 429, 529/overloaded, 5xx, connection errors, timeouts
    - 529/overloaded is retried on the SAME model; the --model argument is
      never rewritten. If retries exhaust while overloaded the run fails
      loudly with outcome OVERLOADED_OUTCOME.
    - Persistent retry mode: for unattended sessions, retries 429/529 with
      higher backoff (5 min max) and periodic heartbeats, up to a bounded
      ceiling; sustained 529 then fails loudly with OVERLOADED_OUTCOME.

    Args:
        cmd: Command list for subprocess
        timeout: Per-attempt timeout (default: PROCESS_TIMEOUT)
        max_retries: Maximum retry attempts (default: 10). In persistent mode
            429/529 capacity errors do not consume this budget.
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy
        persistent_max_attempts: Ceiling on capacity-error retries in
            persistent mode. None reads dispatch config
            ``persistent_retry_max_attempts`` (default 36).
        project_dir: Working directory for the CLI. None uses the first
            ``--add-dir`` of ``cmd`` (what build_cli_command puts there).

    Returns:
        Result dict with success, result_text, num_turns, duration, cost, errors
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    consecutive_529_errors = 0
    last_error = ""
    persistent_attempt = 0
    persistent_ceiling = (
        _resolve_persistent_ceiling(persistent_max_attempts)
        if persistent_retry else 0
    )

    # Create child abort controller if parent provided
    child_controller = (
        create_child_abort_controller(abort_controller)
        if abort_controller
        else AbortController()
    )

    # Counts only failures that consume the max_retries budget; persistent
    # capacity retries are bounded separately by persistent_ceiling.
    attempt = 0
    while True:
        attempt_start = time.time()

        # Check if already aborted before spawning subprocess
        if child_controller.signal.aborted:
            duration = time.time() - start_time
            return {
                "success": False,
                "result_text": "",
                "num_turns": 0,
                "duration": duration,
                "cost": None,
                "errors": [f"Aborted before execution: {child_controller.signal.reason}"],
                # Task #2314 Phase A: every exit path must set this so
                # _resolve_files_changed_count never falls back to the
                # agent-controlled FILES_CHANGED footer.
                "files_changed_set": [],
            }

        try:
            process, contained = await _spawn_agent_process(
                cmd, project_dir=project_dir)

            # Register abort handler to stop the agent's whole process tree
            def abort_handler(
                process: asyncio.subprocess.Process = process,
                contained: _ContainedAgent | None = contained,
            ) -> None:
                _request_agent_termination(process, contained)

            # Not once=True: its wrapper could not be removed again below.
            # abort() fires at most once, so the handler still runs once.
            child_controller.signal.add_event_listener("abort", abort_handler)

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                # Stop the whole tree, then capture any partial output
                await _terminate_agent(process, contained)
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=5,
                    )
                    partial_text = stdout_bytes.decode("utf-8", errors="replace").strip()
                except (asyncio.TimeoutError, OSError, ProcessLookupError):
                    partial_text = ""
                duration = time.time() - start_time
                return {
                    "success": False,
                    "result_text": partial_text,
                    "num_turns": 0,
                    "duration": duration,
                    "cost": None,
                    "errors": [f"Process timed out after {effective_timeout} seconds"],
                    "files_changed_set": [],
                }
            except BaseException:
                # Cancelled, or the loop is shutting down: stop the tree
                # synchronously. An awaited or scheduled kill could itself be
                # cancelled before it escalates (PT-02).
                _terminate_agent_sync(process, contained)
                raise
            finally:
                # A late parent abort must not signal a finished run (PT-04).
                child_controller.signal.remove_event_listener("abort", abort_handler)

            # The CLI exited on its own. The launcher has already swept what
            # it left running; this confirms the group is empty (gate-05).
            await _terminate_agent(process, contained)

        except FileNotFoundError:
            return {
                "success": False,
                "result_text": "",
                "num_turns": 0,
                "duration": 0,
                "cost": None,
                "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
                "files_changed_set": [],
            }
        except AgentContainmentError as exc:
            return _containment_failure_result(exc)
        except AgentDispatchRefused as exc:
            return _dispatch_refused_result(exc)

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        # Parse JSON output
        result: dict[str, Any] = {
            "success": False,
            "result_text": stdout_text,
            "num_turns": 0,
            "duration": time.time() - start_time,
            "cost": None,
            "errors": [],
            # Task #2314 Phase A: non-streaming run_agent path does not observe
            # tool calls, so the set is always empty. Setting it explicitly
            # blocks _resolve_files_changed_count from trusting the agent's
            # FILES_CHANGED footer for this code path.
            "files_changed_set": [],
        }

        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")

        # The CLI's own error message (an is_error result). With stderr it is
        # all the retry classifiers below may read: the JSON stdout also holds
        # the agent's text and token counts ("cache_read_input_tokens": 5029
        # contains "502"), neither of which is an API error (F1).
        api_error_text = ""
        if not stdout_text:
            result["errors"].append("No output from agent")
            last_error = "No output from agent"
        else:
            try:
                data = json.loads(stdout_text)
                result["result_text"] = data.get("result", stdout_text)
                result["num_turns"] = data.get("num_turns", 0)
                result["cost"] = data.get("cost_usd")

                # Check for error subtypes
                subtype = data.get("subtype", "")
                if subtype == "error_max_turns":
                    # The run was cut off, not finished (F8, as the streaming
                    # path reports it): not a success, and ``hit_max_turns``
                    # tells a caller that wants the partial work (the dev
                    # loop) or needs complete output (a reviewer, gate-07).
                    result["success"] = False
                    result["hit_max_turns"] = True
                    result["errors"].append(_MAX_TURNS_ERROR)
                elif data.get("is_error"):
                    result["success"] = False
                    error_msg = data.get('result', 'unknown')
                    result["errors"].append(f"Agent error: {error_msg}")
                    last_error = error_msg
                    api_error_text = str(error_msg)
                else:
                    result["success"] = True

            except json.JSONDecodeError:
                # Output wasn't JSON, treat raw text as result
                result["result_text"] = stdout_text
                result["success"] = process.returncode == 0
                if not result["success"]:
                    last_error = stdout_text[:200]

        # If successful, return immediately
        if result["success"]:
            return result
        # A run cut off by its turn budget is never relaunched (F1/F8): a
        # fresh agent with a full budget on a dirty worktree is not a retry.
        if result.get("hit_max_turns"):
            return result

        # 529/overloaded: keep retrying on the SAME model. Never swap --model.
        overloaded = is_overloaded_error(stderr_text, api_error_text)
        if overloaded:
            consecutive_529_errors += 1
            _note_overloaded(cmd, consecutive_529_errors)
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable (529/overloaded is transient capacity)
        if not overloaded and not is_retryable_error(stderr_text, api_error_text):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 with high backoff, bounded by
        # persistent_ceiling so a sustained outage still fails loudly.
        is_capacity_error = is_transient_capacity_error(stderr_text, api_error_text)
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
            if persistent_attempt >= persistent_ceiling:
                return _fail_persistent_exhausted(
                    result, cmd, overloaded, consecutive_529_errors,
                    persistent_attempt, last_error)
            # In persistent mode, use separate attempt counter and higher backoff
            delay_seconds = get_retry_delay(
                persistent_attempt,
                max_delay_ms=PERSISTENT_MAX_BACKOFF_MS,
                persistent=True,
            )
            # Cap total delay at 6 hours
            delay_ms = delay_seconds * 1000
            if delay_ms > PERSISTENT_RESET_CAP_MS:
                delay_ms = PERSISTENT_RESET_CAP_MS
                delay_seconds = delay_ms / 1000.0

            print(f"  [PersistentRetry] Attempt {persistent_attempt}/"
                  f"{persistent_ceiling} failed "
                  f"({time.time() - attempt_start:.1f}s). "
                  f"Retrying in {delay_seconds:.1f}s... "
                  f"(error: {last_error[:80]})")

            # Chunk long sleeps into heartbeat intervals to show we're alive
            remaining_ms = delay_ms
            while remaining_ms > 0:
                chunk_ms = min(remaining_ms, HEARTBEAT_INTERVAL_MS)
                await asyncio.sleep(chunk_ms / 1000.0)
                remaining_ms -= chunk_ms
                if remaining_ms > 0:
                    print(f"  [Heartbeat] Still retrying... "
                          f"{remaining_ms / 1000.0:.0f}s remaining")
            continue

        # Last attempt exhausted
        attempt += 1
        if attempt >= max_retries:
            if overloaded:
                return _fail_overloaded(
                    result, cmd, consecutive_529_errors, max_retries)
            result["errors"].append(
                f"Max retries ({max_retries}) exhausted. Last error: {last_error}"
            )
            return result

        # Calculate retry delay with exponential backoff + jitter
        delay_seconds = get_retry_delay(attempt)
        print(f"  [Retry] Attempt {attempt}/{max_retries} failed "
              f"({time.time() - attempt_start:.1f}s). "
              f"Retrying in {delay_seconds:.1f}s... "
              f"(error: {last_error[:80]})")

        await asyncio.sleep(delay_seconds)


async def _run_agent_streaming_impl(
    cmd: list[str],
    role: str = "developer",
    timeout: int | None = None,
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> dict[str, Any]:
    """Internal implementation of streaming agent execution.

    This is the actual implementation that gets wrapped by run_agent_streaming
    with retry logic.
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    from equipa.role_resolver import is_role_early_term_exempt
    is_exempt = is_role_early_term_exempt(role, project_dir)

    # Task #2314 Phase A3: capture HEAD before any agent work so that the
    # post-run git-diff cross-check measures ONLY commits this cycle made.
    # Using HEAD~1..HEAD (the prior implementation) had three failure modes:
    #   (a) multi-commit cycles under-counted (only last commit's files seen)
    #   (b) non-writer roles (code-reviewer / security-reviewer) running after
    #       the developer in the same worktree inherited the developer's diff
    #   (c) deferred commits caused stale-HEAD overrides of fresh edits
    # pre_head..post_head solves all three. None means "no commits yet" — the
    # later cross-check skips the override and trusts files_changed_set.
    pre_head: str | None = None
    if project_dir:
        pre_head = await _git_rev_parse_head(project_dir)

    # Create child abort controller if parent provided
    child_controller = (
        create_child_abort_controller(abort_controller)
        if abort_controller
        else AbortController()
    )

    # Tracking state
    turn_count = 0
    turns_without_file_change = 0
    # Scale early termination with budget — larger budgets get more reading time
    # but cap HARD to prevent analysis paralysis on large codebases.
    # Max kill threshold = 1.25x base. Previous 1.5x was too generous — agents
    # burned 12+ turns reading on 58KB+ patches (FeatureBench task 3).
    effective_kill_turns = min(
        int(EARLY_TERM_KILL_TURNS * 1.25),
        max(EARLY_TERM_KILL_TURNS, int((max_turns or EARLY_TERM_KILL_TURNS) * 0.15))
    )
    read_budget_scale = _read_budget_scale()
    if read_budget_scale != 1.0:
        effective_kill_turns = int(effective_kill_turns * read_budget_scale)
        log(f"  [EarlyTerm] read budget x{read_budget_scale:g} (dispatch config): "
            f"kill threshold {effective_kill_turns} turns", output)
    fast_escalation_reads = int(12 * read_budget_scale)
    # On paralysis retries, progressively tighten kill thresholds.
    # Each retry halves remaining patience: retry 1 → -2 turns, retry 2 → -3, etc.
    # Floor at 3 turns — even the most aggressive retry needs a couple turns.
    if paralysis_retry_count > 0:
        # Loosened 2026-05-02 for Opus 4.7 retest. Previous behavior:
        # halved patience per retry + ZERO free reads from turn 0. That
        # works for 4.6 but kills 4.7 instantly because 4.7 legitimately
        # needs 1-3 reads to plan complex edits. New behavior: gentler
        # reduction (cap at 25% of base, not halving), and DO NOT
        # pre-arm must_write_next_turn — let normal escalation rules
        # handle reading limits.
        reduction = min(paralysis_retry_count, max(1, effective_kill_turns // 4))
        effective_kill_turns = max(8, effective_kill_turns - reduction)
        log(f"  [EarlyTerm] Paralysis retry #{paralysis_retry_count}: "
            f"kill threshold reduced to {effective_kill_turns} turns "
            f"(loosened: free reads still allowed)", output)
    effective_final_warn_turns = max(EARLY_TERM_FINAL_WARN_TURNS, int(effective_kill_turns * 0.7))
    effective_warn_turns = max(EARLY_TERM_WARN_TURNS, int(effective_kill_turns * 0.45))
    has_any_file_change = False
    tool_history: list[str] = []
    tool_errors: list[str | None] = []
    tool_output_hashes: list[str] = []
    action_log: list[dict] = []
    stuck_phrase_count = 0
    consecutive_text_only_turns = 0
    monologue_warning_injected = False
    all_text_chunks: list[str] = []
    # Count read-only calls after the final warning (task #2604 wedge fix).
    # One read is permitted — the agent may need to see current file contents
    # before editing. Killing on the very first post-warning read created an
    # unrecoverable FINAL_WARNING → kill → retry → FINAL_WARNING loop.
    post_final_warning_reads = 0
    # sandbox-04: with the PreToolUse gate active, a flagged Bash command is
    # judged by its tool_result (refused vs executed), not killed on sight.
    hook_command = _pretooluse_hook_command(cmd)
    gate_fingerprint = _gate_fingerprint() if hook_command else None
    agent_cwd = project_dir or _cmd_option(cmd, "--add-dir")
    if agent_cwd is not None and not os.path.isdir(agent_cwd):
        agent_cwd = None  # the spawn below refuses it with a clear error
    if hook_command and (
        gate_fingerprint is None
        or not await asyncio.to_thread(_gate_canary_ok, hook_command, agent_cwd)
    ):
        log("  [BashSecurity] pre-execution gate failed its canary; flagged "
            "commands will be killed on sight", output)
        hook_command = None
    pending_flagged: dict[str, tuple[Any, str]] = {}
    hook_block_strikes = 0
    # Task #2242 Phase A3: accumulate framework stdout printed by bash/test
    # tool_result events so the downstream Phase-B grep can see what the
    # actual test runner reported (e.g. pytest's "= 3 skipped =" footer).
    # all_text_chunks only captures assistant TEXT blocks; tool stdout
    # never lands there, which previously made Phase B structurally blind
    # to framework-emitted skip counts.
    tool_output_text_chunks: list[str] = []
    result_data: dict | None = None
    warning_injected = False
    final_warning_injected = False
    must_write_next_turn = False  # After final warning, kill on next read-only turn
    consecutive_readonly_tools = 0  # Track read-only streaks for faster escalation
    loop_warning_injected = False
    early_term_reason: str | None = None
    loop_detected_details: str | None = None  # noqa: F841
    agent_signaled_done = False
    early_complete_reason: str | None = None

    # Compaction detection state
    files_read: set[str] = set()
    files_changed: set[str] = set()
    compaction_count: int = 0
    compaction_signals_all: list[dict[str, str]] = []
    turns_since_last_tool: int = 0
    last_soft_checkpoint_turn: int = 0

    # Check if already aborted before spawning subprocess
    if child_controller.signal.aborted:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": time.time() - start_time,
            "cost": None,
            "errors": [f"Aborted before execution: {child_controller.signal.reason}"],
            "files_changed_set": [],
        }

    try:
        process, contained = await _spawn_agent_process(
            cmd,
            project_dir=project_dir,
            limit=4 * 1024 * 1024,  # 4MB buffer for large file reads
        )

        # Register abort handler to stop the agent's whole process tree
        def abort_handler() -> None:
            _request_agent_termination(process, contained)

        # Not once=True: its wrapper could not be removed again below.
        # abort() fires at most once, so the handler still runs once.
        child_controller.signal.add_event_listener("abort", abort_handler)

    except FileNotFoundError:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": 0,
            "cost": None,
            "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
            "files_changed_set": [],
        }
    except AgentContainmentError as exc:
        return _containment_failure_result(exc)
    except AgentDispatchRefused as exc:
        return _dispatch_refused_result(exc)

    try:
        # Read stdout line-by-line with overall timeout
        while True:
            elapsed = time.time() - start_time
            remaining = effective_timeout - elapsed
            if remaining <= 0:
                early_term_reason = f"Process timed out after {effective_timeout} seconds"
                break

            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=min(remaining, 600),
                )
            except asyncio.TimeoutError:
                early_term_reason = f"No output for 600s (overall timeout: {effective_timeout}s)"
                break

            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            # Parse stream-json message
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # --- Handle "result" message (final) ---
            if msg_type == "result":
                result_data = msg
                break

            # --- Handle "assistant" messages (agent turns) ---
            if msg_type == "assistant":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                turn_has_file_change = False
                turn_has_tool_calls = False

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "text":
                        text = block.get("text", "")
                        all_text_chunks.append(text)

                        # Check for agent-initiated early completion signal
                        ec_reason = _parse_early_complete(text)
                        if ec_reason and not agent_signaled_done:
                            agent_signaled_done = True
                            early_complete_reason = ec_reason
                            log(f"  [EarlyComplete] Agent signaled done at turn "
                                f"~{turn_count}: {ec_reason}", output)

                        # Check for stuck phrases
                        matched = _check_stuck_phrases(text)
                        if matched:
                            stuck_phrase_count += 1
                            log(f"  [EarlyTerm] Stuck signal detected at turn ~{turn_count}: "
                                f"\"{matched}\" (count: {stuck_phrase_count})", output)
                            if stuck_phrase_count >= 3:
                                early_term_reason = (
                                    f"Agent stuck: repeated stuck phrases "
                                    f"({stuck_phrase_count}x, last: \"{matched}\")"
                                )

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        turn_count += 1
                        turn_has_tool_calls = True

                        # Record action entry for action logging. Preview and
                        # hash are persisted to agent_actions: string values
                        # are redacted before serialising, and the hash is no
                        # oracle for what was redacted (sandbox-12, P2A-04/08).
                        input_preview, input_hash = redact_tool_input(
                            tool_input, 200)
                        action_log.append({
                            "turn": turn_count,
                            "tool": tool_name,
                            "input_preview": input_preview,
                            "input_hash": input_hash,
                            "timestamp": time.time(),
                        })

                        # Track files read for compaction detection
                        if tool_name == "Read":
                            read_path = tool_input.get("file_path", "")
                            if read_path:
                                files_read.add(read_path)
                        elif tool_name in ("Glob", "Grep"):
                            pass  # Search tools — not file reads

                        # Track file-modifying tools
                        if tool_name in ("Edit", "Write", "NotebookEdit"):
                            turn_has_file_change = True
                            has_any_file_change = True
                            file_path = tool_input.get("file_path",
                                                       tool_input.get("notebook_path", ""))
                            if file_path:
                                files_changed.add(file_path)
                        elif tool_name == "Bash":
                            bash_cmd = tool_input.get("command", "")

                            # --- Bash security reactive check ---
                            # Off the event loop, with a deadline (sandbox-07).
                            # Commands and checker messages are redacted before
                            # they reach a log line or a result (sandbox-12).
                            sec_result = await _reactive_bash_check(bash_cmd)
                            cmd_preview = redacted_preview(bash_cmd, 120)
                            tool_use_id = block.get("id")
                            if sec_result is None:
                                early_term_reason = (
                                    f"Bash security: the reactive check did not "
                                    f"finish within {_SLOW_CHECK_SECONDS:g}s; "
                                    f"failing closed"
                                )
                                log(f"  [BashSecurity] BLOCKED: {early_term_reason} "
                                    f"— cmd={cmd_preview}", output)
                            elif not sec_result.safe and hook_command and tool_use_id:
                                # The gate ran this same check before execution;
                                # its tool_result says whether it was refused.
                                check_msg = redact_secrets(sec_result.message)
                                pending_flagged[tool_use_id] = (
                                    sec_result.check_id, check_msg)
                                log(f"  [BashSecurity] flagged check={sec_result.check_id}: "
                                    f"{check_msg} (awaiting pre-execution gate) "
                                    f"— cmd={cmd_preview}", output)
                            elif not sec_result.safe:
                                check_msg = redact_secrets(sec_result.message)
                                log(f"  [BashSecurity] BLOCKED check={sec_result.check_id}: "
                                    f"{check_msg} — cmd={cmd_preview}", output)
                                early_term_reason = (
                                    f"Bash security violation (check {sec_result.check_id}): "
                                    f"{check_msg}"
                                )

                            if any(kw in bash_cmd for kw in [
                                "git commit", "git add", "go build", "npm run build",
                                "mkdir", "cp ", "mv ", "touch ", "tee ", "> ",
                            ]):
                                turn_has_file_change = True
                                has_any_file_change = True

                # After processing all blocks in this assistant message,
                # update the file-change counter ONCE per API turn
                if turn_has_tool_calls and not is_exempt:
                    if turn_has_file_change:
                        turns_without_file_change = 0
                        consecutive_readonly_tools = 0
                        must_write_next_turn = False  # Agent wrote — crisis averted
                        post_final_warning_reads = 0  # Reset post-warning read counter
                    else:
                        turns_without_file_change += 1
                        # On paralysis retries (retry_count > 0), the prompt
                        # tells the agent to start with Edit/Write. If the
                        # very first tool is read-only, allow exactly ONE
                        # read and arm must_write_next_turn so the NEXT call
                        # must be a write or the agent dies on the regular
                        # paralysis path. Applies to all retry counts (>= 1)
                        # — the prior "kill on retry >= 2" branch was
                        # unsatisfiable for refactor tasks (no forward context
                        # carries across cycles, so the agent has to read at
                        # least one file to know what to edit).
                        prev_must_write = must_write_next_turn
                        gate_term, must_write_next_turn = (
                            _evaluate_paralysis_retry_read_gate(
                                paralysis_retry_count,
                                turn_count,
                                tool_name,
                                has_any_file_change,
                                must_write_next_turn,
                            )
                        )
                        if must_write_next_turn and not prev_must_write:
                            log(f"  [EarlyTerm] Paralysis retry "
                                f"#{paralysis_retry_count}: first tool is "
                                f"{tool_name}. Allowing ONE read — next call "
                                f"MUST be Edit/Write or you die.", output)
                            warning_injected = True
                            final_warning_injected = True
                        if gate_term is not None:
                            early_term_reason = gate_term
                            log(f"  [EarlyTerm] {early_term_reason}", output)

                        # Track consecutive read-only tool calls for faster
                        # escalation on large codebases. Threshold loosened
                        # 2026-05-02 from 2 to 12 for Opus 4.7 retest — 4.7
                        # legitimately needs more reading turns than 4.6 to
                        # plan complex edits. If 4.7 retest fails, restore
                        # to 2 (was tuned for 4.6 + FeatureBench task 3).
                        if tool_name in ("Read", "Grep", "Glob", "Agent"):
                            consecutive_readonly_tools += 1
                        if (consecutive_readonly_tools >= fast_escalation_reads
                                and not final_warning_injected):
                            log(f"  [EarlyTerm] FAST ESCALATION: "
                                f"{consecutive_readonly_tools} consecutive "
                                f"read-only tool calls without any file edit. "
                                f"Skipping to FINAL WARNING. "
                                f"(role={role}, turn ~{turn_count}). "
                                f"Your NEXT tool call MUST be Edit or Write "
                                f"or you will be TERMINATED.", output)
                            warning_injected = True
                            final_warning_injected = True
                            must_write_next_turn = True

                        tool_history.append(_build_tool_signature(tool_name, tool_input))

                        # Check for loop detection (repeated failing operations)
                        action, count, last_sig = _detect_tool_loop(
                            tool_history,
                            tool_errors,
                            warn_threshold=LOOP_WARNING_THRESHOLD,
                            terminate_threshold=LOOP_TERMINATE_THRESHOLD,
                            tool_output_hashes=tool_output_hashes,
                        )

                        if action == "terminate":
                            early_term_reason = (
                                f"Loop detected: agent repeated the same operation "
                                f"{count} times ({tool_name})"
                            )
                            log(f"  [LoopDetect] {early_term_reason}", output)
                        elif action == "warn" and not loop_warning_injected:
                            log(f"  [LoopDetect] WARNING: Repeated operation detected "
                                f"({count}x: {tool_name}). Try a different approach.", output)
                            loop_warning_injected = True

                        # File-change turn monitoring (non-exempt roles only)
                        if not is_exempt and turns_without_file_change > 0:
                            remaining = effective_kill_turns - turns_without_file_change

                            # Post-final-warning enforcement: if agent was told
                            # "write on your next turn or die" but used a read-only
                            # tool instead, kill immediately. This prevents agents
                            # from burning 2-3 extra turns after final warning.
                            # Kill on ANY tool that is not Edit/Write/Bash-edit
                            # after final warning. Previous list missed Bash,
                            # ToolSearch, and other non-writing tools. Inverting
                            # the check: only Edit and Write are writing tools.
                            write_tools = {"Edit", "Write", "NotebookEdit"}
                            if (must_write_next_turn
                                    and tool_name not in write_tools):
                                post_final_warning_reads += 1
                                if post_final_warning_reads == 1:
                                    # Allow ONE read-only call after the FINAL WARNING.
                                    # The agent may need to see the current file before
                                    # it can edit. Killing immediately here caused the
                                    # task #2604 wedge: FINAL_WARNING → kill → retry →
                                    # FINAL_WARNING loop (50 min, zero commits, PID 343414).
                                    log(
                                        f"  [EarlyTerm] POST-FINAL-WARNING read "
                                        f"#{post_final_warning_reads}: {tool_name} "
                                        f"after FINAL WARNING. ONE read permitted — "
                                        f"next call MUST be Edit/Write or you die.",
                                        output,
                                    )
                                else:
                                    early_term_reason = (
                                        f"Agent terminated: received FINAL WARNING "
                                        f"but made {post_final_warning_reads} "
                                        f"consecutive read-only calls (last: {tool_name}) "
                                        f"instead of Edit/Write. "
                                        f"{turns_without_file_change} turns without "
                                        f"file changes — analysis paralysis"
                                    )
                                    log(
                                        f"  [EarlyTerm] KILLED (post-warning, "
                                        f"{post_final_warning_reads}x read): "
                                        f"{early_term_reason}",
                                        output,
                                    )

                            if (turns_without_file_change >= effective_warn_turns
                                    and not warning_injected):
                                log(f"  [EarlyTerm] WARNING: {turns_without_file_change} "
                                    f"turns without file changes (role={role}, "
                                    f"turn ~{turn_count}). STOP READING AND WRITE "
                                    f"CODE NOW. Your next tool call MUST be Edit or "
                                    f"Write — not Read, not Grep, not Glob. Write a "
                                    f"stub or skeleton immediately. You have "
                                    f"{remaining} turns before termination. This is "
                                    f"not a suggestion — agents that ignore this "
                                    f"warning get killed.", output)
                                warning_injected = True

                            if (turns_without_file_change >= effective_final_warn_turns
                                    and not final_warning_injected):
                                log(f"  [EarlyTerm] FINAL WARNING — IMMINENT KILL: "
                                    f"{turns_without_file_change} turns without file "
                                    f"changes (role={role}, turn ~{turn_count}). "
                                    f"YOU WILL BE TERMINATED IN {remaining} TURNS. "
                                    f"A replacement agent is already queued. Your "
                                    f"ONLY option: call Edit or Write RIGHT NOW. "
                                    f"Write ANYTHING that creates a file change — "
                                    f"a stub, a skeleton, a partial implementation. "
                                    f"If your very next tool call is not Edit or "
                                    f"Write, you are dead.", output)
                                final_warning_injected = True
                                must_write_next_turn = True

                            # Reading-ratio kill: even if the agent made an
                            # early edit, catch agents that relapse into
                            # analysis after one trivial change. If >75% of
                            # tool calls are read-only after turn 8, kill.
                            if (turn_count >= 8
                                    and not early_term_reason
                                    and consecutive_readonly_tools >= 4
                                    and len(tool_history) > 0):
                                read_tools_total = sum(
                                    1 for sig in tool_history
                                    if any(sig.startswith(t)
                                           for t in ("Read:", "Grep:", "Glob:",
                                                     "Agent:"))
                                )
                                ratio = read_tools_total / len(tool_history)
                                if ratio >= 0.75:
                                    early_term_reason = (
                                        f"Agent terminated: {ratio:.0%} of "
                                        f"tool calls are read-only after "
                                        f"{turn_count} turns "
                                        f"({read_tools_total}/{len(tool_history)}). "
                                        f"Reading ratio exceeded 75% threshold "
                                        f"— analysis paralysis with token edits"
                                    )
                                    log(f"  [EarlyTerm] KILLED (reading ratio): "
                                        f"{early_term_reason}", output)

                            if turns_without_file_change >= effective_kill_turns:
                                early_term_reason = (
                                    f"Agent terminated: {turns_without_file_change} "
                                    f"consecutive turns without file changes "
                                    f"(threshold: {effective_kill_turns}). "
                                    f"Agent spent all turns reading/analyzing "
                                    f"instead of writing code — analysis paralysis"
                                )
                                log(f"  [EarlyTerm] KILLED: {early_term_reason}",
                                    output)

                # Budget visibility: log remaining budget at intervals
                if turn_has_tool_calls and max_turns:
                    budget_msg = _get_budget_message(turn_count, max_turns)
                    if budget_msg:
                        log(f"  [Budget] {budget_msg}", output)

                # Monologue detection: track consecutive text-only assistant turns
                if turn_has_tool_calls:
                    consecutive_text_only_turns = 0
                else:
                    consecutive_text_only_turns += 1

                    # Post-final-warning enforcement for text-only turns:
                    # If agent was told "write next turn or die" but responds
                    # with pure text (no tool calls at all), that's worse than
                    # a read-only tool call — kill immediately.
                    if must_write_next_turn and not is_exempt:
                        early_term_reason = (
                            f"Agent terminated: received FINAL WARNING but "
                            f"responded with text-only turn (no tool calls) "
                            f"instead of Edit/Write. "
                            f"{turns_without_file_change} turns without "
                            f"file changes — analysis paralysis"
                        )
                        log(f"  [EarlyTerm] KILLED (post-warning, text-only): "
                            f"{early_term_reason}", output)

                    monologue_action = _check_monologue(
                        consecutive_text_only_turns, turn_count,
                    )
                    if monologue_action == "terminate":
                        early_term_reason = (
                            f"Agent monologue: {consecutive_text_only_turns} "
                            f"consecutive text-only messages without tool use"
                        )
                        log(f"  [Monologue] {early_term_reason}", output)
                    elif (monologue_action == "warn"
                            and not monologue_warning_injected):
                        log(f"  [Monologue] WARNING: {consecutive_text_only_turns} "
                            f"consecutive text-only turns (role={role}, "
                            f"turn ~{turn_count}). Agent may be stuck reasoning "
                            f"without acting.", output)
                        monologue_warning_injected = True

                # Compaction detection: check for signals after processing
                if turn_has_tool_calls and all_text_chunks:
                    # Build recent tool calls for this turn
                    recent_tools = tool_history[-3:] if tool_history else []
                    if turn_has_tool_calls:
                        turns_since_last_tool = 0
                    else:
                        turns_since_last_tool += 1

                    latest_text = all_text_chunks[-1] if all_text_chunks else ""
                    signals = detect_compaction_signals(
                        text=latest_text,
                        turn_count=turn_count,
                        files_read=files_read,
                        recent_tool_calls=recent_tools,
                        turns_since_last_tool=turns_since_last_tool,
                    )
                    if signals:
                        compaction_count += 1
                        compaction_signals_all.extend(signals)
                        signal_types = [s["type"] for s in signals]
                        log(f"  [Compaction] Suspected compaction at turn "
                            f"~{turn_count} (#{compaction_count}): "
                            f"{', '.join(signal_types)}", output)

                        # Immediate soft checkpoint on compaction signal
                        last_text = "\n".join(all_text_chunks[-3:])
                        cp_path = save_soft_checkpoint(
                            task_id=task_id or 0,
                            turn_count=turn_count,
                            files_changed=files_changed,
                            files_read=files_read,
                            last_result_text=last_text,
                            compaction_count=compaction_count,
                            compaction_signals=compaction_signals_all,
                            role=role,
                        )
                        if cp_path:
                            log(f"  [SoftCheckpoint] Saved compaction "
                                f"checkpoint -> {cp_path.name}", output)
                            last_soft_checkpoint_turn = turn_count

                # Periodic soft checkpoint every N turns
                if (turn_has_tool_calls
                        and task_id
                        and turn_count - last_soft_checkpoint_turn
                        >= SOFT_CHECKPOINT_INTERVAL):
                    last_text = "\n".join(all_text_chunks[-3:])
                    cp_path = save_soft_checkpoint(
                        task_id=task_id,
                        turn_count=turn_count,
                        files_changed=files_changed,
                        files_read=files_read,
                        last_result_text=last_text,
                        compaction_count=compaction_count,
                        compaction_signals=compaction_signals_all,
                        role=role,
                    )
                    if cp_path:
                        log(f"  [SoftCheckpoint] Periodic checkpoint at "
                            f"turn {turn_count} -> {cp_path.name}", output)
                        last_soft_checkpoint_turn = turn_count

                # If we found a reason to terminate, break out
                if early_term_reason:
                    break

                # If agent signaled early completion, break after this
                # assistant message is fully processed
                if agent_signaled_done:
                    log(f"  [EarlyComplete] Current message processed, "
                        f"stopping stream.", output)
                    break

            # --- Handle "user" messages (tool results) ---
            elif msg_type == "user":
                message = msg.get("message", {})
                content_blocks = message.get("content", [])

                for block in content_blocks:
                    block_type = block.get("type", "")

                    if block_type == "tool_result":
                        is_error = block.get("is_error", False)
                        content = block.get("content", "")

                        flagged = pending_flagged.pop(block.get("tool_use_id"), None)
                        if flagged is not None:
                            check_id, check_msg = flagged
                            if _gate_fingerprint() != gate_fingerprint:
                                early_term_reason = (
                                    "Bash security: the pre-execution gate or its "
                                    "checker changed during the run; failing closed"
                                )
                                log(f"  [BashSecurity] {early_term_reason}", output)
                            elif _is_hook_block_result(
                                content, is_error, hook_command, check_id,
                            ):
                                hook_block_strikes += 1
                                log(f"  [BashSecurity] refused before execution by the "
                                    f"gate (check {check_id}); agent told why, not killed "
                                    f"(strike {hook_block_strikes}/"
                                    f"{_HOOK_BLOCK_STRIKE_LIMIT})", output)
                                if hook_block_strikes >= _HOOK_BLOCK_STRIKE_LIMIT:
                                    early_term_reason = (
                                        f"Bash security: {hook_block_strikes} commands "
                                        f"refused by the pre-execution gate; the agent "
                                        f"is not self-correcting"
                                    )
                            else:
                                early_term_reason = (
                                    f"Bash security violation (check {check_id}): "
                                    f"{check_msg} (the command EXECUTED: the "
                                    f"pre-execution gate did not refuse it)"
                                )
                                log(f"  [BashSecurity] {early_term_reason}", output)

                        # Task #2242 Phase A3: capture the raw stdout of tool
                        # results (bash, etc.) so Phase B can grep framework
                        # skip counts that never appear in assistant text.
                        if isinstance(content, str):
                            if content:
                                tool_output_text_chunks.append(content)
                        elif isinstance(content, list):
                            for _c in content:
                                if isinstance(_c, dict) and _c.get("type") == "text":
                                    _t = _c.get("text", "")
                                    if _t:
                                        tool_output_text_chunks.append(_t)

                        # Error output often echoes the command; it is
                        # persisted as agent_actions.error_summary (sandbox-12).
                        error_text = None
                        if is_error:
                            if isinstance(content, str):
                                error_text = redacted_preview(content, 200)
                            elif isinstance(content, list):
                                texts = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        texts.append(c.get("text", ""))
                                if texts:
                                    error_text = redacted_preview(" ".join(texts), 200)

                        tool_errors.append(error_text)

                        # Compute output hash for loop detection
                        output_hash = _compute_output_hash(content)
                        tool_output_hashes.append(output_hash)

                        # Update the most recent action_log entry with result
                        if action_log:
                            entry = action_log[-1]
                            if isinstance(content, str):
                                result_len = len(content)
                            elif isinstance(content, list):
                                result_len = sum(
                                    len(c.get("text", ""))
                                    for c in content
                                    if isinstance(c, dict)
                                )
                            else:
                                result_len = 0
                            entry["success"] = not is_error
                            entry["output_length"] = result_len
                            entry["output_hash"] = output_hash
                            entry["duration_ms"] = int(
                                (time.time() - entry.get("timestamp", time.time())) * 1000
                            )
                            if is_error and error_text:
                                entry["error_type"] = classify_error(error_text)
                                entry["error_summary"] = error_text[:200]

                            # After any tool completes, check git for file changes
                            if project_dir:
                                if _check_git_changes(project_dir):
                                    has_any_file_change = True
                                    turns_without_file_change = 0
                                    tool_label = entry.get("tool", "unknown")
                                    log(f"  [FileDetect] Git detected file changes "
                                        f"via {tool_label}", output)

                if early_term_reason:
                    break

    except Exception as e:
        # Justified broad catch: the streaming monitor parses arbitrary JSON tool events
        # from a long-lived subprocess; any unexpected event shape must NOT crash the
        # orchestrator. Reason captured into early_term_reason for surfacing upstream.
        early_term_reason = f"Streaming monitor error: {e}"
        logger.exception("[Telemetry] streaming monitor caught unexpected error")
    except BaseException:
        # Cancelled, or the loop is shutting down: stop the tree
        # synchronously. An awaited or scheduled kill could itself be
        # cancelled before it escalates (PT-02).
        child_controller.signal.remove_event_listener("abort", abort_handler)
        _terminate_agent_sync(process, contained)
        raise

    if pending_flagged and not early_term_reason:
        early_term_reason = (
            "Bash security: a flagged command got no tool_result, so whether it "
            "ran is unknown; failing closed"
        )

    # A late parent abort must not signal a finished run (PT-04).
    child_controller.signal.remove_event_listener("abort", abort_handler)

    # --- Terminate the agent's whole process tree (gate-05) ---
    # Runs on every exit path. After a normal exit the launcher has already
    # swept whatever the agent left running; this confirms and releases.
    was_running = process.returncode is None
    if was_running and early_term_reason:
        log(f"  [EarlyTerm] Killing agent process (reason: {early_term_reason})", output)
    elif was_running:
        # A normal finish: the stream ended (result event or EARLY_COMPLETE)
        # while the CLI or the launcher's sweep was still winding down.
        log("  [Cleanup] Agent stream finished; stopping the agent process "
            "tree", output)
    await _terminate_agent(process, contained)
    if was_running:
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
            await asyncio.wait_for(process.communicate(), timeout=5)

    duration = time.time() - start_time

    result = _build_streaming_result(
        turn_count, duration, has_any_file_change,
        early_term_reason, agent_signaled_done,
        early_complete_reason, result_data, all_text_chunks)

    # Read any remaining stderr
    try:
        stderr_bytes = await asyncio.wait_for(process.stderr.read(), timeout=2)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")
    except (asyncio.TimeoutError, OSError, AttributeError):
        pass

    # Bulk insert action log to agent_actions table
    if task_id and action_log:
        bulk_log_agent_actions(action_log, task_id, run_id, cycle_number, role)

    # Attach action_log to result for caller inspection
    result["action_log"] = action_log

    # Attach compaction metadata for loop/continuation logic
    result["compaction_count"] = compaction_count
    result["compaction_signals"] = compaction_signals_all
    result["files_read"] = sorted(files_read)
    result["files_changed_set"] = sorted(files_changed)

    # Task #2314 Phase A3: cross-check files_changed_set against the actual
    # git diff over the range pre_head..post_head — commits this cycle made,
    # NOT HEAD~1..HEAD (which mis-counts multi-commit cycles and leaks the
    # developer's diff into the reviewer's row). The override only fires when
    # at least one commit landed during this run; non-writer roles and
    # deferred-commit cycles see pre_head == post_head and keep the
    # files_changed_set count (Phase A made that field authoritative on every
    # exit path). The mismatch warning fires ONLY on real disagreement about
    # actually-committed work — silent on roles that wrote nothing.
    if project_dir and pre_head is not None:
        post_head = await _git_rev_parse_head(project_dir)
        if post_head and post_head != pre_head:
            git_diff_count = await _git_diff_files_changed_count(
                project_dir, pre_head, post_head,
            )
            if git_diff_count is not None:
                tool_count = len(files_changed)
                if git_diff_count != tool_count:
                    log(
                        f"  [Telemetry] files_changed cross-check mismatch: "
                        f"tool-calls={tool_count} git-diff={git_diff_count} "
                        f"(range {pre_head[:8]}..{post_head[:8]}). "
                        f"Bash-driven writes or fabrication possible.",
                        output,
                    )
                # Canonical signal: git's view of what actually committed
                # during this cycle. Only set when commits exist; otherwise
                # files_changed_set (already on result) is authoritative.
                result["files_changed_count"] = git_diff_count

    return result


async def _git_rev_parse_head(project_dir: str) -> str | None:
    """Resolve HEAD to a commit SHA, or ``None`` if the repo has no commits
    yet / project_dir is not a git repo / git errors. Never raises.

    Used as the bookends for the Phase-A3 pre_head..post_head diff range so
    the cross-check measures only commits made during this agent cycle.
    """
    try:
        from equipa.git_ops import git_run_async
        rev = await git_run_async(
            ["rev-parse", "--verify", "HEAD"], project_dir, timeout=10,
        )
        if rev.returncode != 0:
            return None
        sha = rev.stdout.strip()
        return sha or None
    except Exception:
        # Justified broad catch: best-effort telemetry probe. A git failure
        # here must not crash the orchestrator before / after the agent runs.
        return None


async def _git_diff_files_changed_count(
    project_dir: str,
    pre_head: str,
    post_head: str,
) -> int | None:
    """Return the number of unique files changed in the range
    ``pre_head..post_head``, or ``None`` if it cannot be determined.

    Canonical Phase-A3 signal for ``agent_runs.files_changed_count``. The
    range covers every commit made during the current agent cycle, so
    multi-commit cycles count correctly and non-writer roles (no commits)
    never reach this code path. Uses ``git_run_async`` so the event loop is
    not blocked. Never raises.
    """
    try:
        from equipa.git_ops import git_run_async
        diff = await git_run_async(
            ["diff", "--name-only", f"{pre_head}..{post_head}"],
            project_dir, timeout=10,
        )
        if diff.returncode != 0:
            return None
        files = {line for line in diff.stdout.splitlines() if line.strip()}
        return len(files)
    except Exception:
        # Justified broad catch: this is a best-effort telemetry probe; any
        # git error must NOT crash the orchestrator post-cycle.
        return None


async def run_agent_with_retries(
    cmd: list[str],
    task: dict[str, Any],
    max_retries: int,
) -> tuple[dict[str, Any], int]:
    """Run agent with retry logic on failure.

    Returns (result, attempt_number) tuple.
    """
    started_at = _run_started_at_utc()
    result: dict[str, Any] = {}
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"\n--- Retry {attempt}/{max_retries} ---")

        result = await run_agent(cmd)
        # Stamp the run start so downstream date-checks (e.g. the single-agent
        # guard's TASKS_CREATED validation) have a real value, not None.
        result.setdefault("started_at", started_at)

        # run_agent already exhausted its own 529 retries on the configured
        # model. Re-running would only repeat that; surface the loud
        # overloaded failure to the caller instead (task #2994).
        if is_overloaded_result(result):
            print("  Not retrying: model overloaded (529) through every retry")
            return result, attempt
        # Out of turns is not a transient failure: never relaunch (F1).
        if result.get("hit_max_turns"):
            print("  Not retrying: agent hit its max turns")
            return result, attempt

        # Check if output is valid
        is_valid, reason = validate_output(result)

        if is_valid:
            return result, attempt

        print(f"  Attempt {attempt} failed: {reason}")

        # Check if the agent updated the task to blocked — that's intentional
        verified, _ = verify_task_updated(task["id"])
        if verified:
            return result, attempt

        # Don't retry on timeout — the task is probably too complex
        if any("timed out" in e for e in result.get("errors", [])):
            print("  Not retrying: process timed out")
            return result, attempt

    print(f"\n  All {max_retries} attempts failed.")
    return result, max_retries


async def run_agent_streaming_with_retry(
    cmd: list[str],
    role: str = "developer",
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    max_retries: int = 10,
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
    persistent_max_attempts: int | None = None,
) -> AgentResult:
    """Wrap run_agent_streaming with retry logic + exponential backoff.

    Same retry architecture as run_agent():
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - Retryable errors: 429, 529/overloaded, 5xx, connection, timeout,
      ECONNRESET, EPIPE
    - 529/overloaded is retried on the SAME model; the --model argument is
      never rewritten. If retries exhaust while overloaded the run fails
      loudly with outcome OVERLOADED_OUTCOME.
    - Non-retryable errors fail immediately
    - Persistent retry mode: for unattended sessions, retries 429/529 with
      higher backoff (5 min max) and periodic heartbeats, up to a bounded
      ceiling; sustained 529 then fails loudly with OVERLOADED_OUTCOME.

    Args:
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy
        persistent_max_attempts: Ceiling on capacity-error retries in
            persistent mode. None reads dispatch config
            ``persistent_retry_max_attempts`` (default 36).
    """
    consecutive_529_errors = 0
    last_error = ""
    persistent_attempt = 0
    persistent_ceiling = (
        _resolve_persistent_ceiling(persistent_max_attempts)
        if persistent_retry else 0
    )

    # Counts only failures that consume the max_retries budget; persistent
    # capacity retries are bounded separately by persistent_ceiling.
    attempt = 0
    while True:
        attempt_start = time.time()

        # Execute streaming agent
        result = await _run_agent_streaming_impl(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, run_id=None, cycle_number=cycle_number,
            project_dir=project_dir, abort_controller=abort_controller,
            paralysis_retry_count=paralysis_retry_count,
        )

        # If successful, return immediately
        if result.get("success"):
            return result
        # A run cut off by its turn budget is never relaunched (F1, indep
        # review of 3122): the dev loop's continuation owns what happens
        # next, and a fresh agent with a full budget on a dirty worktree is
        # not a retry.
        if result.get("hit_max_turns"):
            return result

        # Extract error info. Only structured error fields are classified,
        # never the agent's RESULT text (F1).
        stderr_text = _structured_error_text(result)
        last_error = (stderr_text or result.get("result_text", ""))[:200]

        # Non-retryable: analysis paralysis kills must fail fast, not retry.
        # Retrying after a paralysis kill restarts the exact same pattern
        # (agent reads → FINAL WARNING → reads again → kill → retry → ...)
        # and burns API cost for 10 attempts before giving up (task #2604 wedge).
        if result.get("early_terminated"):
            term_reason = result.get("early_term_reason", "")
            if (
                "analysis paralysis" in term_reason
                or "without file changes" in term_reason
                or "read-only" in term_reason
                or "reading ratio" in term_reason
            ):
                return result

        # 529/overloaded: keep retrying on the SAME model. Never swap --model.
        overloaded = is_overloaded_error(stderr_text, "")
        if overloaded:
            consecutive_529_errors += 1
            _note_overloaded(cmd, consecutive_529_errors)
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable (529/overloaded is transient capacity)
        if not overloaded and not is_retryable_error(stderr_text, ""):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 with high backoff, bounded by
        # persistent_ceiling so a sustained outage still fails loudly.
        is_capacity_error = is_transient_capacity_error(stderr_text, "")
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
            if persistent_attempt >= persistent_ceiling:
                return _fail_persistent_exhausted(
                    result, cmd, overloaded, consecutive_529_errors,
                    persistent_attempt, last_error)
            # In persistent mode, use separate attempt counter and higher backoff
            delay_seconds = get_retry_delay(
                persistent_attempt,
                max_delay_ms=PERSISTENT_MAX_BACKOFF_MS,
                persistent=True,
            )
            # Cap total delay at 6 hours
            delay_ms = delay_seconds * 1000
            if delay_ms > PERSISTENT_RESET_CAP_MS:
                delay_ms = PERSISTENT_RESET_CAP_MS
                delay_seconds = delay_ms / 1000.0

            print(f"  [PersistentRetry] Streaming attempt {persistent_attempt}/"
                  f"{persistent_ceiling} failed "
                  f"({time.time() - attempt_start:.1f}s). "
                  f"Retrying in {delay_seconds:.1f}s... "
                  f"(error: {last_error[:80]})")

            # Chunk long sleeps into heartbeat intervals to show we're alive
            remaining_ms = delay_ms
            while remaining_ms > 0:
                chunk_ms = min(remaining_ms, HEARTBEAT_INTERVAL_MS)
                await asyncio.sleep(chunk_ms / 1000.0)
                remaining_ms -= chunk_ms
                if remaining_ms > 0:
                    print(f"  [Heartbeat] Still retrying... "
                          f"{remaining_ms / 1000.0:.0f}s remaining")
            continue

        # Last attempt exhausted
        attempt += 1
        if attempt >= max_retries:
            if overloaded:
                return _fail_overloaded(
                    result, cmd, consecutive_529_errors, max_retries)
            result["errors"].append(
                f"Max retries ({max_retries}) exhausted. Last error: {last_error}"
            )
            return result

        # Calculate retry delay with exponential backoff + jitter
        delay_seconds = get_retry_delay(attempt)
        print(f"  [Retry] Streaming attempt {attempt}/{max_retries} failed "
              f"({time.time() - attempt_start:.1f}s). "
              f"Retrying in {delay_seconds:.1f}s... "
              f"(error: {last_error[:80]})")

        await asyncio.sleep(delay_seconds)


async def run_agent_streaming(
    cmd: list[str],
    role: str = "developer",
    timeout: int | None = None,
    output: Any = None,
    max_turns: int | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    cycle_number: int = 1,
    project_dir: str | None = None,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> AgentResult:
    """Spawn claude -p with stream-json output for real-time stuck detection.

    Monitors agent output turn-by-turn and terminates early if stuck signals
    are detected. Only applies file-change monitoring to non-exempt roles
    (developer, tester, debugger, etc.).

    When task_id is provided, per-tool actions are logged to the agent_actions
    table for observability and ForgeSmith analysis.

    This function automatically includes retry logic with exponential backoff
    (529/overloaded retried on the same model — never downgraded). Use
    run_agent_streaming_with_retry
    directly if you need to customize retry parameters.

    Returns the same dict format as run_agent().
    """
    started_at = _run_started_at_utc()
    result = await run_agent_streaming_with_retry(
        cmd, role=role, output=output, max_turns=max_turns,
        task_id=task_id, cycle_number=cycle_number, project_dir=project_dir,
        paralysis_retry_count=paralysis_retry_count,
    )
    # Stamp the run start so downstream date-checks (e.g. the single-agent
    # guard's TASKS_CREATED validation) have a real value, not None.
    if isinstance(result, dict):
        result.setdefault("started_at", started_at)
    return result


async def dispatch_agent(
    cmd: list[str],
    role: str,
    output: Any,
    max_turns: int,
    task_id: int,
    cycle: int,
    system_prompt: str | PromptResult | None = None,
    project_dir: str | None = None,
    args: Any = None,
    paralysis_retry_count: int = 0,
) -> AgentResult:
    """Dispatch an agent using the configured provider (Claude or Ollama).

    For Claude: delegates to run_agent_streaming() or run_agent().
    For Ollama: delegates to run_ollama_agent().

    system_prompt accepts str or PromptResult (coerced to str via __str__).

    Returns the same result dict format regardless of provider.
    """
    # Coerce PromptResult to str for downstream consumers
    if system_prompt is not None:
        system_prompt = str(system_prompt)

    # Security: verify skill file integrity before building any agent prompt
    if not verify_skill_integrity():
        return {
            "success": False,
            "result": "blocked",
            "result_text": "CRITICAL: Skill integrity verification failed — agent dispatch refused. "
                          "Run --regenerate-manifest if changes are intentional.",
            "num_turns": 0,
            "cost": 0,
            "duration": 0,
            "errors": ["Skill integrity verification failed"],
        }

    # Late imports to avoid circular dependency
    from equipa.cli import get_ollama_base_url, get_ollama_model, get_provider

    dispatch_config = getattr(args, "dispatch_config", None) if args else None
    provider_override = getattr(args, "provider", None) if args else None

    # Determine provider: CLI override > config > default (claude)
    if provider_override:
        provider = provider_override
    else:
        provider = get_provider(role, dispatch_config)

    if provider == "ollama" and system_prompt and project_dir:
        # R3136-01: Ollama tool calls run in-process as the orchestrator user,
        # never through the isolated launcher, so the flag refuses them.
        refusal = isolation.unisolated_spawn_refusal(
            "Ollama agent", isolation.OLLAMA_REFUSAL_REMEDY,
            action=isolation.OLLAMA_REFUSAL_ACTION)
        if refusal:
            refused = _dispatch_refused_result(AgentDispatchRefused(refusal))
            refused["result"] = "blocked"
            refused["result_text"] = f"RESULT: blocked\nBLOCKERS: {refusal}"
            return refused
        from ollama_agent import run_ollama_agent
        model = get_ollama_model(role, dispatch_config)
        base_url = get_ollama_base_url(dispatch_config)
        return run_ollama_agent(
            system_prompt=system_prompt,
            project_dir=project_dir,
            role=role,
            model=model,
            base_url=base_url,
            max_turns=max_turns,
        )

    # RLM REPL decomposition for large-repo reviews
    if system_prompt and project_dir and role in ("code-reviewer", "integration-tester"):
        from equipa.rlm_decompose import (
            estimate_context_tokens,
            load_repo_files,
            run_decompose_session,
            should_decompose,
        )
        rlm_enabled = is_feature_enabled(dispatch_config, "rlm_decompose")
        if rlm_enabled:
            repo_files = load_repo_files(project_dir)
            ctx_tokens = estimate_context_tokens(system_prompt, repo_files)
            if should_decompose(role, ctx_tokens, rlm_enabled):
                from equipa.output import log
                log(
                    f"RLM Decompose active: {len(repo_files)} files, "
                    f"~{ctx_tokens:,} tokens, role={role}"
                )
                decompose_result = run_decompose_session(
                    system_prompt=system_prompt,
                    project_dir=project_dir,
                    role=role,
                    repo_files=repo_files,
                    mcp_config=str(MCP_CONFIG),
                    # Same model the role resolved to — never a cheaper one.
                    model=_cmd_model(cmd) or get_configured_model(dispatch_config),
                )
                rlm_result = {
                    "success": decompose_result.success,
                    "result_text": decompose_result.output,
                    "num_turns": decompose_result.sub_queries_run,
                    "duration": 0,
                    "cost": 0,
                    "errors": decompose_result.errors,
                    "rlm_decompose": True,
                    "files_examined": decompose_result.files_examined,
                }
                # Same loud outcome as the retry wrappers, so the tester and
                # review call sites refuse it via is_overloaded_result (#2994).
                if decompose_result.overloaded:
                    rlm_result["success"] = False
                    rlm_result["outcome"] = OVERLOADED_OUTCOME
                return rlm_result

    # Default: Claude via run_agent_streaming (with retry wrapper)
    from equipa.role_resolver import is_role_early_term_exempt
    use_streaming = not is_role_early_term_exempt(role, project_dir)
    if use_streaming:
        # Wrap streaming with retry logic
        return await run_agent_streaming_with_retry(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, cycle_number=cycle, project_dir=project_dir,
            paralysis_retry_count=paralysis_retry_count)
    else:
        return await run_agent(cmd, project_dir=project_dir)
