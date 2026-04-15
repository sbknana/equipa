"""EQUIPA agent_runner — agent dispatch, streaming, and retry logic.

Layer 6: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.security, equipa.roles.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from equipa.prompts import PromptResult

from equipa.abort_controller import AbortController, create_child_abort_controller
from equipa.bash_security import check_bash_command
from equipa.constants import (
    EARLY_TERM_EXEMPT_ROLES,
    EARLY_TERM_FINAL_WARN_TURNS,
    EARLY_TERM_KILL_TURNS,
    EARLY_TERM_WARN_TURNS,
    MCP_CONFIG,
    PROCESS_TIMEOUT,
    ROLE_SKILLS,
)
from equipa.db import bulk_log_agent_actions, classify_error
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
MAX_529_RETRIES = 3  # After 3x 529/overloaded, fall back to cheaper model
JITTER_FACTOR = 0.25  # 25% jitter as per Claude Code

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
    combined = f"{stderr} {stdout}".lower()
    return any(marker in combined for marker in [
        "529",
        "overloaded",
        "overloaded_error",
        "temporarily overloaded",
    ])


def is_transient_capacity_error(stderr: str, stdout: str) -> bool:
    """Check if error is a transient capacity issue (429 or 529/overloaded).

    These errors are suitable for persistent retry mode with long backoff.

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if this is a 429 or 529 capacity error
    """
    combined = f"{stderr} {stdout}".lower()
    return any(marker in combined for marker in ["429", "rate limit", "529", "overloaded"])


def is_retryable_error(stderr: str, stdout: str) -> bool:
    """Check if error is retryable (network, timeout, 429, 500-level).

    Args:
        stderr: Standard error text
        stdout: Standard output text

    Returns:
        True if error should be retried
    """
    combined = f"{stderr} {stdout}".lower()
    retryable_markers = [
        "429",
        "rate limit",
        "connection",
        "timeout",
        "econnreset",
        "epipe",
        "500",
        "502",
        "503",
        "504",
    ]
    return any(marker in combined for marker in retryable_markers)


def build_cli_command(
    system_prompt: str | PromptResult,
    project_dir: str,
    max_turns: int,
    model: str,
    role: str = "developer",
    streaming: bool = False,
) -> list[str]:
    """Build the claude CLI command as a list of arguments.

    Args:
        system_prompt: Full system prompt string, or PromptResult from
            build_system_prompt(). PromptResult is coerced to str via
            __str__() which returns the full prompt with boundary marker.
        streaming: If True, use stream-json output format for real-time monitoring.
    """
    # Explicit str() ensures PromptResult.__str__() is called, producing
    # the full prompt with SYSTEM_PROMPT_DYNAMIC_BOUNDARY marker.
    prompt_str = str(system_prompt)
    output_format = "stream-json" if streaming else "json"
    cmd = [
        "claude",
        "-p",
        f"Execute the task described in your system prompt. Work in: {project_dir}",
        "--output-format", output_format,
        "--model", model,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
        "--append-system-prompt", prompt_str,
        "--mcp-config", str(MCP_CONFIG),
        "--add-dir", str(project_dir),
        "--permission-mode", "bypassPermissions",
    ]

    # stream-json requires --verbose
    if streaming:
        cmd.append("--verbose")

    # Load role-specific skills directory if it exists
    skills_dir = ROLE_SKILLS.get(role)
    if skills_dir and skills_dir.exists():
        cmd.extend(["--add-dir", str(skills_dir)])

    return cmd


async def run_agent(
    cmd: list[str],
    timeout: int | None = None,
    max_retries: int = 10,
    fallback_model: str | None = None,
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
) -> dict[str, Any]:
    """Spawn claude -p with retry logic, exponential backoff, and model fallback.

    Implements Claude Code withRetry.ts pattern:
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - After 3 consecutive 529/overloaded errors, fall back to cheaper model
    - Retries on 429, 5xx, connection errors, timeouts
    - Persistent retry mode: for unattended sessions, retries 429/529 indefinitely
      with higher backoff (5 min max) and periodic heartbeats

    Args:
        cmd: Command list for subprocess
        timeout: Per-attempt timeout (default: PROCESS_TIMEOUT)
        max_retries: Maximum retry attempts (default: 10, ignored if persistent=True)
        fallback_model: Model to fall back to after 3x 529 (e.g., "sonnet")
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy

    Returns:
        Result dict with success, result_text, num_turns, duration, cost, errors
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    consecutive_529_errors = 0
    last_error = ""
    model_fallback_triggered = False
    persistent_attempt = 0

    # Create child abort controller if parent provided
    child_controller = (
        create_child_abort_controller(abort_controller)
        if abort_controller
        else AbortController()
    )

    for attempt in range(1, max_retries + 1):
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
            }

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Register abort handler to kill subprocess
            def abort_handler() -> None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass

            child_controller.signal.add_event_listener("abort", abort_handler, once=True)

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                # Try to capture any partial output before killing
                process.kill()
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(), timeout=5,
                    )
                    partial_text = stdout_bytes.decode("utf-8", errors="replace").strip()
                except Exception:
                    partial_text = ""
                duration = time.time() - start_time
                return {
                    "success": False,
                    "result_text": partial_text,
                    "num_turns": 0,
                    "duration": duration,
                    "cost": None,
                    "errors": [f"Process timed out after {effective_timeout} seconds"],
                }

        except FileNotFoundError:
            return {
                "success": False,
                "result_text": "",
                "num_turns": 0,
                "duration": 0,
                "cost": None,
                "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
            }

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
        }

        if stderr_text:
            result["errors"].append(f"stderr: {stderr_text}")

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
                    # Agent ran out of turns but may have done useful work
                    result["success"] = True
                    result["errors"].append("Agent hit max turns limit")
                elif data.get("is_error"):
                    result["success"] = False
                    error_msg = data.get('result', 'unknown')
                    result["errors"].append(f"Agent error: {error_msg}")
                    last_error = error_msg
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

        # Check for 529/overloaded errors
        if is_overloaded_error(stderr_text, stdout_text):
            consecutive_529_errors += 1
            if consecutive_529_errors >= MAX_529_RETRIES and fallback_model:
                # Trigger model fallback
                if not model_fallback_triggered:
                    # Find --model flag in cmd and replace
                    for i, arg in enumerate(cmd):
                        if arg == "--model" and i + 1 < len(cmd):
                            original_model = cmd[i + 1]
                            cmd[i + 1] = fallback_model
                            model_fallback_triggered = True
                            print(f"  [Retry] Model fallback triggered: "
                                  f"{original_model} -> {fallback_model} "
                                  f"(after {consecutive_529_errors}x 529 errors)")
                            consecutive_529_errors = 0  # Reset counter for new model
                            break
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable
        if not is_retryable_error(stderr_text, stdout_text):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 indefinitely with high backoff
        is_capacity_error = is_transient_capacity_error(stderr_text, stdout_text)
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
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

            print(f"  [PersistentRetry] Attempt {persistent_attempt} failed "
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

            # Clamp attempt counter so we never exit the loop in persistent mode
            if attempt >= max_retries:
                attempt = max_retries
            continue

        # Last attempt exhausted (non-persistent mode)
        if attempt >= max_retries:
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

    # Should never reach here, but fallback return
    return result


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
    is_exempt = role in EARLY_TERM_EXEMPT_ROLES

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
    # On paralysis retries, progressively tighten kill thresholds.
    # Each retry halves remaining patience: retry 1 → -2 turns, retry 2 → -3, etc.
    # Floor at 3 turns — even the most aggressive retry needs a couple turns.
    if paralysis_retry_count > 0:
        reduction = min(paralysis_retry_count + 1, effective_kill_turns - 3)
        effective_kill_turns = max(3, effective_kill_turns - reduction)
        log(f"  [EarlyTerm] Paralysis retry #{paralysis_retry_count}: "
            f"kill threshold reduced to {effective_kill_turns} turns", output)
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
        }

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,  # 4MB buffer for large file reads
        )

        # Register abort handler to kill subprocess
        def abort_handler() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        child_controller.signal.add_event_listener("abort", abort_handler, once=True)

    except FileNotFoundError:
        return {
            "success": False,
            "result_text": "",
            "num_turns": 0,
            "duration": 0,
            "cost": None,
            "errors": ["'claude' command not found. Is Claude Code installed and on PATH?"],
        }

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

                        # Record action entry for action logging
                        try:
                            input_str = json.dumps(tool_input, default=str)
                        except (TypeError, ValueError):
                            input_str = str(tool_input)
                        action_log.append({
                            "turn": turn_count,
                            "tool": tool_name,
                            "input_preview": input_str[:200],
                            "input_hash": hashlib.sha256(
                                input_str.encode("utf-8", errors="replace")
                            ).hexdigest(),
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

                            # --- Bash security pre-execution filter ---
                            sec_result = check_bash_command(bash_cmd)
                            if not sec_result.safe:
                                log(f"  [BashSecurity] BLOCKED check={sec_result.check_id}: "
                                    f"{sec_result.message} — cmd={bash_cmd[:120]}", output)
                                early_term_reason = (
                                    f"Bash security violation (check {sec_result.check_id}): "
                                    f"{sec_result.message}"
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
                    else:
                        turns_without_file_change += 1
                        # Track consecutive read-only tool calls for faster
                        # escalation on large codebases. When agent calls
                        # Read/Grep/Glob 3+ times without Edit/Write, skip
                        # straight to final warning — don't wait for the
                        # normal turn-based escalation. Reduced from 4 to 3
                        # because agents on 58KB+ patches hit all 4 reads
                        # before the warning could change their behavior.
                        if tool_name in ("Read", "Grep", "Glob", "Agent"):
                            consecutive_readonly_tools += 1
                        if (consecutive_readonly_tools >= 3
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
                            if (must_write_next_turn
                                    and tool_name in (
                                        "Read", "Grep", "Glob", "Agent",
                                        "WebSearch", "WebFetch",
                                    )):
                                early_term_reason = (
                                    f"Agent terminated: received FINAL WARNING "
                                    f"but next tool call was {tool_name} (read-only) "
                                    f"instead of Edit/Write. "
                                    f"{turns_without_file_change} turns without "
                                    f"file changes — analysis paralysis"
                                )
                                log(f"  [EarlyTerm] KILLED (post-warning): "
                                    f"{early_term_reason}", output)

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

                        error_text = None
                        if is_error:
                            if isinstance(content, str):
                                error_text = content[:200]
                            elif isinstance(content, list):
                                texts = []
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        texts.append(c.get("text", ""))
                                if texts:
                                    error_text = " ".join(texts)[:200]

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

    except Exception as e:
        early_term_reason = f"Streaming monitor error: {e}"

    # --- Kill process if still running ---
    if process.returncode is None:
        log(f"  [EarlyTerm] Killing agent process (reason: {early_term_reason})", output)
        process.kill()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except Exception:
            pass

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
    except Exception:
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

    return result


async def run_agent_with_retries(
    cmd: list[str],
    task: dict[str, Any],
    max_retries: int,
) -> tuple[dict[str, Any], int]:
    """Run agent with retry logic on failure.

    Returns (result, attempt_number) tuple.
    """
    result: dict[str, Any] = {}
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"\n--- Retry {attempt}/{max_retries} ---")

        result = await run_agent(cmd)

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
    fallback_model: str | None = "sonnet",
    persistent_retry: bool = False,
    abort_controller: AbortController | None = None,
    paralysis_retry_count: int = 0,
) -> dict[str, Any]:
    """Wrap run_agent_streaming with retry logic + exponential backoff + model fallback.

    Same retry architecture as run_agent():
    - Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
    - After 3 consecutive 529/overloaded errors, fall back to cheaper model
    - Retryable errors: 429, 5xx, connection, timeout, ECONNRESET, EPIPE
    - Non-retryable errors fail immediately
    - Persistent retry mode: for unattended sessions, retries 429/529 indefinitely
      with higher backoff (5 min max) and periodic heartbeats

    Args:
        persistent_retry: Enable persistent retry mode for unattended sessions
        abort_controller: Optional parent abort controller for cancellation hierarchy
    """
    consecutive_529_errors = 0
    model_fallback_triggered = False
    last_error = ""
    persistent_attempt = 0

    for attempt in range(1, max_retries + 1):
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

        # Extract error info
        stderr_text = " ".join(result.get("errors", []))
        stdout_text = result.get("result_text", "")
        last_error = stderr_text[:200] if stderr_text else stdout_text[:200]

        # Check for 529/overloaded errors
        if is_overloaded_error(stderr_text, stdout_text):
            consecutive_529_errors += 1
            if consecutive_529_errors >= MAX_529_RETRIES and fallback_model:
                # Trigger model fallback
                if not model_fallback_triggered:
                    # Find --model flag in cmd and replace
                    for i, arg in enumerate(cmd):
                        if arg == "--model" and i + 1 < len(cmd):
                            original_model = cmd[i + 1]
                            cmd[i + 1] = fallback_model
                            model_fallback_triggered = True
                            print(f"  [Retry] Model fallback triggered: "
                                  f"{original_model} -> {fallback_model} "
                                  f"(after {consecutive_529_errors}x 529 errors)")
                            consecutive_529_errors = 0  # Reset counter for new model
                            break
        else:
            consecutive_529_errors = 0  # Reset on non-529 error

        # Check if error is retryable
        if not is_retryable_error(stderr_text, stdout_text):
            # Non-retryable error, fail immediately
            return result

        # Persistent retry mode: retry 429/529 indefinitely with high backoff
        is_capacity_error = is_transient_capacity_error(stderr_text, stdout_text)
        if persistent_retry and is_capacity_error:
            persistent_attempt += 1
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

            print(f"  [PersistentRetry] Streaming attempt {persistent_attempt} failed "
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

            # Clamp attempt counter so we never exit the loop in persistent mode
            if attempt >= max_retries:
                attempt = max_retries
            continue

        # Last attempt exhausted (non-persistent mode)
        if attempt >= max_retries:
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

    # Should never reach here, but fallback return
    return result


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
) -> dict[str, Any]:
    """Spawn claude -p with stream-json output for real-time stuck detection.

    Monitors agent output turn-by-turn and terminates early if stuck signals
    are detected. Only applies file-change monitoring to non-exempt roles
    (developer, tester, debugger, etc.).

    When task_id is provided, per-tool actions are logged to the agent_actions
    table for observability and ForgeSmith analysis.

    This function automatically includes retry logic with exponential backoff
    and model fallback (after 3x 529 errors). Use run_agent_streaming_with_retry
    directly if you need to customize retry parameters.

    Returns the same dict format as run_agent().
    """
    return await run_agent_streaming_with_retry(
        cmd, role=role, output=output, max_turns=max_turns,
        task_id=task_id, cycle_number=cycle_number, project_dir=project_dir,
        paralysis_retry_count=paralysis_retry_count,
    )


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
) -> dict[str, Any]:
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

    # Default: Claude via run_agent_streaming (with retry wrapper)
    use_streaming = role not in EARLY_TERM_EXEMPT_ROLES
    if use_streaming:
        # Wrap streaming with retry logic
        return await run_agent_streaming_with_retry(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, cycle_number=cycle, project_dir=project_dir,
            paralysis_retry_count=paralysis_retry_count)
    else:
        return await run_agent(cmd)
