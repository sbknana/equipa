"""EQUIPA hooks — pluggable lifecycle event system.

This package is BOTH the event registry/dispatcher AND the home for
pipeline hook handler modules.

Two parallel event namespaces are supported:

1. Legacy snake_case events (``pre_agent_start``, ``post_agent_finish``,
   ``pre_cycle``, ``post_cycle``, ``on_checkpoint``, ``on_cost_warning``,
   ``on_stuck_detected``, ``pre_dispatch``, ``post_task_complete``) —
   the original 9 lifecycle events fired from the orchestrator.

2. Colon-namespaced pipeline events (``attempt:before``, ``attempt:after``,
   ``dev_test:fail``, ``diff:empty``, ``security_review:after``,
   ``compaction:before``, ``compaction:after``, ``task:done``,
   ``task:blocked``) — used by the handler modules in this package that
   migrate the inline guards previously scattered in loops.py:

       * ``vacuous_pass``        — vacuous-pass check
       * ``classifier_retry``    — classifier false-positive retry
       * ``security_review_gate`` — security-reviewer output gate

Authoring a new handler: drop a module in this package that exposes a
top-level ``register(dispatcher)`` function. ``bootstrap_pipeline_hooks()``
will discover and wire it at orchestrator start. See
``docs/HOOKS.md`` for the full handler authoring guide.

Sync vs async: ``fire`` runs callbacks synchronously and is suited to
short Python handlers. ``fire_async`` runs external command hooks via
``asyncio.create_subprocess_shell`` for non-blocking execution; use it
from async orchestrator code or for long-running hooks. Python callbacks
are still invoked synchronously inside ``fire_async`` to keep ordering
deterministic — long-running Python handlers should spawn their own
asyncio task and return quickly.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# --- Lifecycle Event Names ---

# Legacy snake_case events (original 9, pre-task-2075).
LEGACY_EVENTS: tuple[str, ...] = (
    "pre_agent_start",
    "post_agent_finish",
    "pre_cycle",
    "post_cycle",
    "on_checkpoint",
    "on_cost_warning",
    "on_stuck_detected",
    "pre_dispatch",
    "post_task_complete",
)

# Colon-namespaced pipeline events (task #2075).
# These fire at well-defined points in run_dev_test_loop and dispatch.
PIPELINE_EVENTS: tuple[str, ...] = (
    "attempt:before",
    "attempt:after",
    "dev_test:fail",
    "diff:empty",
    "security_review:after",
    "compaction:before",
    "compaction:after",
    "task:done",
    "task:blocked",
)

LIFECYCLE_EVENTS: tuple[str, ...] = LEGACY_EVENTS + PIPELINE_EVENTS

# --- Internal callback registry ---

_registry: dict[str, list[Callable[..., Any]]] = {
    event: [] for event in LIFECYCLE_EVENTS
}

# --- External hooks config cache ---

_external_hooks: dict[str, list[dict[str, Any]]] = {}


def register(event: str, callback: Callable[..., Any]) -> None:
    """Register a Python callable for a lifecycle event."""
    if event not in LIFECYCLE_EVENTS:
        raise ValueError(
            f"Unknown lifecycle event '{event}'. "
            f"Valid events: {', '.join(LIFECYCLE_EVENTS)}"
        )
    _registry[event].append(callback)


def unregister(event: str, callback: Callable[..., Any]) -> bool:
    """Remove a previously registered callback. Returns True on success."""
    if event not in _registry:
        return False
    try:
        _registry[event].remove(callback)
        return True
    except ValueError:
        return False


def fire(event: str, **kwargs: Any) -> list[Any]:
    """Fire all registered callbacks for an event (synchronous)."""
    results: list[Any] = []

    for callback in _registry.get(event, []):
        try:
            result = callback(event=event, **kwargs)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 — never crash orchestrator
            cb_name = getattr(callback, "__name__", repr(callback))
            cb_module = getattr(callback, "__module__", "unknown")
            logger.warning(
                "Hook callback %s (from %s) for '%s' failed: %s: %s",
                cb_name, cb_module, event,
                type(exc).__name__, exc,
            )
            results.append(None)

    for hook_cfg in _external_hooks.get(event, []):
        try:
            command = hook_cfg.get("command", "")
            timeout = hook_cfg.get("timeout", 30)
            block_on_fail = hook_cfg.get("block_on_fail", False)
            project_dir = kwargs.get("project_dir", ".")

            exit_code = run_external_hook(command, kwargs, project_dir, timeout)

            if exit_code != 0 and block_on_fail:
                logger.error(
                    "Blocking external hook '%s' for '%s' failed (exit %d)",
                    command, event, exit_code,
                )
                results.append({
                    "blocked": True,
                    "command": command,
                    "exit_code": exit_code,
                })
            else:
                results.append({"exit_code": exit_code, "command": command})
        except Exception as exc:  # noqa: BLE001
            logger.warning("External hook for '%s' failed: %s", event, exc)
            results.append(None)

    return results


async def fire_async(event: str, **kwargs: Any) -> list[Any]:
    """Async version of fire() — runs external hooks via asyncio subprocess."""
    results: list[Any] = []

    for callback in _registry.get(event, []):
        try:
            result = callback(event=event, **kwargs)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            cb_name = getattr(callback, "__name__", repr(callback))
            cb_module = getattr(callback, "__module__", "unknown")
            logger.warning(
                "Hook callback %s (from %s) for '%s' failed: %s: %s",
                cb_name, cb_module, event,
                type(exc).__name__, exc,
            )
            results.append(None)

    for hook_cfg in _external_hooks.get(event, []):
        try:
            command = hook_cfg.get("command", "")
            timeout = hook_cfg.get("timeout", 30)
            block_on_fail = hook_cfg.get("block_on_fail", False)
            project_dir = kwargs.get("project_dir", ".")

            exit_code = await run_external_hook_async(
                command, kwargs, project_dir, timeout
            )

            if exit_code != 0 and block_on_fail:
                logger.error(
                    "Blocking external hook '%s' for '%s' failed (exit %d)",
                    command, event, exit_code,
                )
                results.append({
                    "blocked": True,
                    "command": command,
                    "exit_code": exit_code,
                })
            else:
                results.append({"exit_code": exit_code, "command": command})
        except Exception as exc:  # noqa: BLE001
            logger.warning("External hook for '%s' failed: %s", event, exc)
            results.append(None)

    return results


def load_hooks_config(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load external hook definitions from a hooks.json file.

    Format::

        {
            "pre_agent_start": [
                {"command": "python hooks/lint_check.py",
                 "timeout": 30, "block_on_fail": true}
            ],
            "post_agent_finish": [
                {"command": "python hooks/notify.py", "timeout": 10}
            ]
        }
    """
    global _external_hooks

    config_path = Path(path)
    if not config_path.exists():
        logger.debug(
            "Hooks config not found at %s — no external hooks loaded", config_path
        )
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load hooks config from %s: %s", config_path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            "hooks.json must be a JSON object, got %s", type(raw).__name__
        )
        return {}

    loaded: dict[str, list[dict[str, Any]]] = {}
    for event_name, hook_list in raw.items():
        if event_name not in LIFECYCLE_EVENTS:
            logger.warning(
                "Unknown event '%s' in hooks config — skipping", event_name
            )
            continue
        if not isinstance(hook_list, list):
            logger.warning(
                "Event '%s' hooks must be a list — skipping", event_name
            )
            continue

        valid_hooks: list[dict[str, Any]] = []
        for hook in hook_list:
            if not isinstance(hook, dict) or "command" not in hook:
                logger.warning(
                    "Invalid hook entry for '%s' — must have 'command' key",
                    event_name,
                )
                continue
            valid_hooks.append({
                "command": str(hook["command"]),
                "timeout": int(hook.get("timeout", 30)),
                "block_on_fail": bool(hook.get("block_on_fail", False)),
            })
        if valid_hooks:
            loaded[event_name] = valid_hooks

    _external_hooks = loaded
    logger.info(
        "Loaded %d external hook event(s) from %s", len(loaded), config_path
    )
    return loaded


def run_external_hook(
    command: str,
    context: dict[str, Any],
    project_dir: str,
    timeout: int = 30,
) -> int:
    """Run an external command hook synchronously."""
    env = _build_hook_env(context)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "External hook '%s' exited %d: %s",
                command, result.returncode, result.stderr[:200],
            )
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.warning("External hook '%s' timed out after %ds", command, timeout)
        return -1
    except OSError as exc:
        logger.warning("External hook '%s' failed to execute: %s", command, exc)
        return -2


async def run_external_hook_async(
    command: str,
    context: dict[str, Any],
    project_dir: str,
    timeout: int = 30,
) -> int:
    """Run an external command hook asynchronously via asyncio subprocess."""
    env = _build_hook_env(context)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=project_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "Async external hook '%s' timed out after %ds", command, timeout
            )
            return -1

        if proc.returncode != 0:
            stderr_text = (
                stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
            )
            logger.warning(
                "Async external hook '%s' exited %d: %s",
                command, proc.returncode, stderr_text,
            )
        return proc.returncode or 0
    except OSError as exc:
        logger.warning(
            "Async external hook '%s' failed to execute: %s", command, exc
        )
        return -2


def _build_hook_env(context: dict[str, Any]) -> dict[str, str]:
    """Build environment variables for hook subprocess."""
    import os

    env = os.environ.copy()
    for key, value in context.items():
        if value is not None:
            env_key = f"EQUIPA_HOOK_{key.upper()}"
            env[env_key] = str(value)[:500]  # Cap value length for safety
    return env


def clear_registry() -> None:
    """Clear all registered callbacks (useful for testing)."""
    for event in _registry:
        _registry[event] = []


def clear_external_hooks() -> None:
    """Clear loaded external hook configurations."""
    global _external_hooks
    _external_hooks = {}


def get_registered_count(event: str | None = None) -> int:
    """Get the number of registered callbacks for an event (or all events)."""
    if event:
        return len(_registry.get(event, []))
    return sum(len(cbs) for cbs in _registry.values())


def get_external_hook_count(event: str | None = None) -> int:
    """Get the number of loaded external hooks for an event (or all events)."""
    if event:
        return len(_external_hooks.get(event, []))
    return sum(len(hooks) for hooks in _external_hooks.values())


# --- Pipeline hook auto-discovery ---

# Imported at the bottom to avoid circular-import issues — dispatcher
# imports the registry symbols defined above.
from equipa.hooks.dispatcher import (  # noqa: E402
    bootstrap_pipeline_hooks,
    discover_handlers,
    register_handler_module,
)

__all__ = [
    # Event name constants
    "LEGACY_EVENTS",
    "PIPELINE_EVENTS",
    "LIFECYCLE_EVENTS",
    # Registry API
    "register",
    "unregister",
    "fire",
    "fire_async",
    "load_hooks_config",
    "clear_registry",
    "clear_external_hooks",
    "get_registered_count",
    "get_external_hook_count",
    "run_external_hook",
    "run_external_hook_async",
    # Pipeline auto-discovery
    "bootstrap_pipeline_hooks",
    "discover_handlers",
    "register_handler_module",
]
