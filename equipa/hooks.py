"""EQUIPA hooks — lightweight lifecycle callback registry with external command support.

Provides a simple event system for extending orchestrator behavior without
modifying core code. Supports both Python callables and external command hooks
configured via hooks.json.

9 lifecycle events:
    pre_agent_start, post_agent_finish, pre_cycle, post_cycle,
    on_checkpoint, on_cost_warning, on_stuck_detected,
    pre_dispatch, post_task_complete

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

LIFECYCLE_EVENTS: tuple[str, ...] = (
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

# --- Internal callback registry ---

_registry: dict[str, list[Callable[..., Any]]] = {event: [] for event in LIFECYCLE_EVENTS}

# --- External hooks config cache ---

_external_hooks: dict[str, list[dict[str, Any]]] = {}


def register(event: str, callback: Callable[..., Any]) -> None:
    """Register a Python callable for a lifecycle event.

    Args:
        event: One of the LIFECYCLE_EVENTS names.
        callback: A callable that accepts **kwargs. Will be called with
                  event-specific context (task_id, cycle, project_dir, etc.).

    Raises:
        ValueError: If event name is not a recognized lifecycle event.
    """
    if event not in LIFECYCLE_EVENTS:
        raise ValueError(
            f"Unknown lifecycle event '{event}'. "
            f"Valid events: {', '.join(LIFECYCLE_EVENTS)}"
        )
    _registry[event].append(callback)


def unregister(event: str, callback: Callable[..., Any]) -> bool:
    """Remove a previously registered callback.

    Returns True if the callback was found and removed, False otherwise.
    """
    if event not in _registry:
        return False
    try:
        _registry[event].remove(callback)
        return True
    except ValueError:
        return False


def fire(event: str, **kwargs: Any) -> list[Any]:
    """Fire all registered callbacks for an event.

    Calls each Python callback and runs each external command hook.
    Failures are logged but never crash the orchestrator.

    Args:
        event: The lifecycle event name.
        **kwargs: Context passed to callbacks (task_id, cycle, project_dir, etc.).

    Returns:
        List of callback return values (None for failed callbacks).
    """
    results: list[Any] = []

    # Fire Python callbacks
    for callback in _registry.get(event, []):
        try:
            result = callback(event=event, **kwargs)
            results.append(result)
        except Exception as exc:
            logger.warning("Hook callback %s for '%s' failed: %s", getattr(callback, "__name__", repr(callback)), event, exc)
            results.append(None)

    # Fire external command hooks
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
                results.append({"blocked": True, "command": command, "exit_code": exit_code})
            else:
                results.append({"exit_code": exit_code, "command": command})
        except Exception as exc:
            logger.warning("External hook for '%s' failed: %s", event, exc)
            results.append(None)

    return results


async def fire_async(event: str, **kwargs: Any) -> list[Any]:
    """Async version of fire() — runs external hooks via asyncio subprocess.

    Use this from async orchestrator code for non-blocking hook execution.
    """
    results: list[Any] = []

    # Fire Python callbacks (synchronous — kept fast)
    for callback in _registry.get(event, []):
        try:
            result = callback(event=event, **kwargs)
            results.append(result)
        except Exception as exc:
            logger.warning("Hook callback %s for '%s' failed: %s", getattr(callback, "__name__", repr(callback)), event, exc)
            results.append(None)

    # Fire external command hooks asynchronously
    for hook_cfg in _external_hooks.get(event, []):
        try:
            command = hook_cfg.get("command", "")
            timeout = hook_cfg.get("timeout", 30)
            block_on_fail = hook_cfg.get("block_on_fail", False)
            project_dir = kwargs.get("project_dir", ".")

            exit_code = await run_external_hook_async(command, kwargs, project_dir, timeout)

            if exit_code != 0 and block_on_fail:
                logger.error(
                    "Blocking external hook '%s' for '%s' failed (exit %d)",
                    command, event, exit_code,
                )
                results.append({"blocked": True, "command": command, "exit_code": exit_code})
            else:
                results.append({"exit_code": exit_code, "command": command})
        except Exception as exc:
            logger.warning("External hook for '%s' failed: %s", event, exc)
            results.append(None)

    return results


def load_hooks_config(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load external hook definitions from a hooks.json file.

    Format:
        {
            "pre_agent_start": [
                {"command": "python hooks/lint_check.py", "timeout": 30, "block_on_fail": true}
            ],
            "post_agent_finish": [
                {"command": "python hooks/notify.py", "timeout": 10}
            ]
        }

    Args:
        path: Path to the hooks.json configuration file.

    Returns:
        Dict mapping event names to lists of hook configurations.
        Returns empty dict if file doesn't exist or is invalid.
    """
    global _external_hooks

    config_path = Path(path)
    if not config_path.exists():
        logger.debug("Hooks config not found at %s — no external hooks loaded", config_path)
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load hooks config from %s: %s", config_path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning("hooks.json must be a JSON object, got %s", type(raw).__name__)
        return {}

    # Validate and load only recognized events
    loaded: dict[str, list[dict[str, Any]]] = {}
    for event_name, hook_list in raw.items():
        if event_name not in LIFECYCLE_EVENTS:
            logger.warning("Unknown event '%s' in hooks config — skipping", event_name)
            continue
        if not isinstance(hook_list, list):
            logger.warning("Event '%s' hooks must be a list — skipping", event_name)
            continue

        valid_hooks: list[dict[str, Any]] = []
        for hook in hook_list:
            if not isinstance(hook, dict) or "command" not in hook:
                logger.warning("Invalid hook entry for '%s' — must have 'command' key", event_name)
                continue
            valid_hooks.append({
                "command": str(hook["command"]),
                "timeout": int(hook.get("timeout", 30)),
                "block_on_fail": bool(hook.get("block_on_fail", False)),
            })
        if valid_hooks:
            loaded[event_name] = valid_hooks

    _external_hooks = loaded
    logger.info("Loaded %d external hook event(s) from %s", len(loaded), config_path)
    return loaded


def run_external_hook(
    command: str,
    context: dict[str, Any],
    project_dir: str,
    timeout: int = 30,
) -> int:
    """Run an external command hook synchronously.

    The command is executed as a shell subprocess in the project directory.
    Context is passed via environment variables prefixed with EQUIPA_HOOK_.

    Args:
        command: Shell command to execute.
        context: Dict of context values (converted to env vars).
        project_dir: Working directory for the subprocess.
        timeout: Maximum execution time in seconds.

    Returns:
        Process exit code (0 = success). Returns -1 on timeout, -2 on error.
    """
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
    """Run an external command hook asynchronously via asyncio subprocess.

    Args:
        command: Shell command to execute.
        context: Dict of context values (converted to env vars).
        project_dir: Working directory for the subprocess.
        timeout: Maximum execution time in seconds.

    Returns:
        Process exit code (0 = success). Returns -1 on timeout, -2 on error.
    """
    import os

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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("Async external hook '%s' timed out after %ds", command, timeout)
            return -1

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
            logger.warning(
                "Async external hook '%s' exited %d: %s",
                command, proc.returncode, stderr_text,
            )
        return proc.returncode or 0
    except OSError as exc:
        logger.warning("Async external hook '%s' failed to execute: %s", command, exc)
        return -2


def _build_hook_env(context: dict[str, Any]) -> dict[str, str]:
    """Build environment variables for hook subprocess.

    Converts context dict to EQUIPA_HOOK_* env vars. Only string-safe values
    are included. Inherits the current process environment.
    """
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
