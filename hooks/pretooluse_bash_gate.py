#!/usr/bin/env python3
"""Claude Code PreToolUse hook — pre-execution Bash security gate.

This is the *prevention* half of EQUIPA's bash security story. The reactive
stream observer in ``equipa.agent_runner`` sees a Bash tool call only AFTER
the Claude CLI subprocess has already executed it — that is
detect-and-terminate, not prevention. This hook, when wired into the spawned
CLI via a generated ``--settings`` file (feature flag
``features.bash_security_pretooluse``, DEFAULT OFF), runs
``equipa.bash_security.check_bash_command`` on the Bash command BEFORE the
tool is allowed to run, giving true pre-execution blocking.

Contract (Claude Code PreToolUse hook, exit-code form)
------------------------------------------------------
* Reads the tool-call JSON from stdin::

      {"hook_event_name": "PreToolUse", "tool_name": "Bash",
       "tool_input": {"command": "...", "description": "..."}}

* Exit 0  -> allow the tool call.
* Exit 2  -> block the tool call; stderr is fed back to the agent so it can
             see WHY and self-correct.
* Another tool (``tool_name`` is a string other than ``"Bash"``) or an empty
  command -> exit 0: there is nothing for this gate to judge.
* The gate FAILS CLOSED (exit 2, reason on stderr) on everything else:
  unparseable stdin, a Bash payload without a string ``command``, a checker
  that cannot be loaded (ImportError, SyntaxError, ...), an exception raised
  inside ``check_bash_command``, or a result without a boolean ``safe``.
  Claude Code treats any exit code other than 2 (including the exit 1 an
  uncaught exception produces) as a non-blocking error and runs the command,
  so every failure is mapped to exit 2 explicitly. Do NOT assume another
  layer catches what this gate lets through: the reactive stream check does
  not run for every role (non-streaming roles are never checked by it).

Import resolution
-----------------
The script may be invoked with an arbitrary cwd (Claude Code runs hooks from
the session directory). It resolves its own repository root from ``__file__``
and loads ``equipa/bash_security.py`` DIRECTLY by file path via importlib,
deliberately bypassing ``equipa/__init__.py`` — that package initializer
imports the whole orchestrator (cli, dispatch, loops, manager, mcp_server),
which would be far too heavy and fragile for a gate that runs before every
single bash command. ``bash_security`` is documented as a standalone,
zero-EQUIPA-import module, so loading it in isolation is safe and fast. The
repo root is also inserted into ``sys.path`` so an ordinary
``import equipa.bash_security`` would resolve as a fallback.

Standard-library only — NO third-party dependencies (Claude Code may run this
hook in a minimal environment).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

# Exit codes per the Claude Code PreToolUse hook contract.
EXIT_ALLOW = 0
EXIT_BLOCK = 2


def _repo_root() -> Path:
    """Return the repository root (the directory that contains ``equipa/``).

    ``__file__`` lives at ``<repo_root>/hooks/pretooluse_bash_gate.py``; the
    root is therefore two levels up. ``resolve()`` makes this robust to being
    invoked via a relative path or a symlink.
    """
    return Path(__file__).resolve().parent.parent


def _load_check_bash_command() -> Callable[[str], Any]:
    """Load ``check_bash_command`` without triggering ``equipa/__init__.py``.

    Loads ``equipa/bash_security.py`` as an isolated module by file path.
    Falls back to a normal package import (after putting the repo root on
    ``sys.path``) if the direct load fails for any reason.

    Raises:
        ImportError: if the checker cannot be loaded by either strategy.
    """
    root = _repo_root()
    module_path = root / "equipa" / "bash_security.py"

    # Primary: direct file-path load — fast, and does NOT execute the heavy
    # equipa package initializer.
    if module_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "equipa_bash_security_gate", module_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            # Register before exec: bash_security defines a @dataclass, and
            # dataclasses resolves ``cls.__module__`` via ``sys.modules`` — an
            # unregistered module makes that lookup return None and raise
            # ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
            check = getattr(module, "check_bash_command", None)
            if callable(check):
                return check

    # Fallback: ordinary package import (accepts the __init__ cost). Only
    # reached if the direct load above did not yield the symbol.
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from equipa.bash_security import check_bash_command  # type: ignore

    return check_bash_command


class PayloadError(ValueError):
    """The hook payload cannot be interpreted; the gate must block."""


def _block(reason: str) -> int:
    """Print *reason* for the agent (stderr) and return the block exit code.

    A broken stderr must not turn the block into an exception (exit 1).
    """
    try:
        print(
            f"pretooluse_bash_gate: {reason} - command blocked (the gate "
            "fails closed).",
            file=sys.stderr,
        )
    except (OSError, ValueError):
        pass
    return EXIT_BLOCK


def _read_tool_input() -> dict[str, Any]:
    """Parse the PreToolUse payload from stdin.

    Raises:
        PayloadError: stdin is unreadable, empty, not JSON, or not an object.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        raise PayloadError(f"could not read hook stdin ({exc})") from exc
    if not raw or not raw.strip():
        raise PayloadError("empty hook payload on stdin")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise PayloadError(f"hook payload is not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise PayloadError(
            f"hook payload is a JSON {type(payload).__name__}, not an object"
        )
    return payload


def _extract_bash_command(payload: dict[str, Any]) -> str | None:
    """Return the Bash command to check, or None when there is nothing to judge.

    None means "a different tool" or "an empty command" - both are allowed.

    Raises:
        PayloadError: the payload claims to be (or might be) a Bash call but
            its shape is wrong, so the command cannot be judged.
    """
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        raise PayloadError("hook payload has no string tool_name")
    if tool_name != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise PayloadError("Bash payload has no tool_input object")
    command = tool_input.get("command")
    if not isinstance(command, str):
        raise PayloadError("Bash payload has no string command")
    if not command.strip():
        return None
    return command


def main() -> int:
    """Run the gate. Returns the process exit code (0 allow / 2 block)."""
    try:
        command = _extract_bash_command(_read_tool_input())
    except PayloadError as exc:
        return _block(str(exc))
    if command is None:
        return EXIT_ALLOW

    try:
        check_bash_command = _load_check_bash_command()
    except BaseException as exc:  # noqa: BLE001 - fail closed on ANY load error
        # ImportError, SyntaxError from a bad deploy, OSError, even SystemExit
        # raised at import: without a checker nothing can be judged.
        return _block(
            f"could not load the bash security checker "
            f"({type(exc).__name__}: {exc})"
        )

    try:
        result = check_bash_command(command)
    except BaseException as exc:  # noqa: BLE001 - fail closed on ANY error
        return _block(
            f"the bash security checker raised {type(exc).__name__}: {exc}"
        )

    safe = getattr(result, "safe", None)
    if safe is True:
        return EXIT_ALLOW
    if safe is not False:
        return _block(
            f"the bash security checker returned an unusable result "
            f"({type(result).__name__} without a boolean 'safe')"
        )

    check_id = getattr(result, "check_id", 0)
    message = getattr(result, "message", "unsafe command")
    # stderr is surfaced to the agent on exit 2 so it can self-correct.
    print(
        f"BashSecurity check {check_id}: {message} — command blocked before "
        "execution by the EQUIPA pre-execution gate.",
        file=sys.stderr,
    )
    return EXIT_BLOCK


def _run() -> int:
    """Entry point: map ANY escape from main() to a block, never exit 1."""
    try:
        return main()
    except BaseException as exc:  # noqa: BLE001 - exit 1 would allow the call
        return _block(f"internal gate error ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    sys.exit(_run())
