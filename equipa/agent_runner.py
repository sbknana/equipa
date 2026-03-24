"""EQUIPA agent_runner — agent dispatch, streaming, and retry logic.

Layer 6: Imports from equipa.constants, equipa.db, equipa.monitoring, equipa.output,
         equipa.parsing, equipa.security, equipa.roles.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

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
)
from equipa.output import log
from equipa.parsing import validate_output
from equipa.security import verify_skill_integrity
from equipa.tasks import verify_task_updated


def build_cli_command(
    system_prompt: str,
    project_dir: str,
    max_turns: int,
    model: str,
    role: str = "developer",
    streaming: bool = False,
) -> list[str]:
    """Build the claude CLI command as a list of arguments.

    Args:
        streaming: If True, use stream-json output format for real-time monitoring.
    """
    output_format = "stream-json" if streaming else "json"
    cmd = [
        "claude",
        "-p",
        f"Execute the task described in your system prompt. Work in: {project_dir}",
        "--output-format", output_format,
        "--model", model,
        "--max-turns", str(max_turns),
        "--no-session-persistence",
        "--append-system-prompt", system_prompt,
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


async def run_agent(cmd: list[str], timeout: int | None = None) -> dict[str, Any]:
    """Spawn claude -p, capture output, handle timeout."""
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

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

    duration = time.time() - start_time
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    # Parse JSON output
    result: dict[str, Any] = {
        "success": False,
        "result_text": stdout_text,
        "num_turns": 0,
        "duration": duration,
        "cost": None,
        "errors": [],
    }

    if stderr_text:
        result["errors"].append(f"stderr: {stderr_text}")

    if not stdout_text:
        result["errors"].append("No output from agent")
        return result

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
            result["errors"].append(f"Agent error: {data.get('result', 'unknown')}")
        else:
            result["success"] = True

    except json.JSONDecodeError:
        # Output wasn't JSON, treat raw text as result
        result["result_text"] = stdout_text
        result["success"] = process.returncode == 0

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
) -> dict[str, Any]:
    """Spawn claude -p with stream-json output for real-time stuck detection.

    Monitors agent output turn-by-turn and terminates early if stuck signals
    are detected. Only applies file-change monitoring to non-exempt roles
    (developer, tester, debugger, etc.).

    When task_id is provided, per-tool actions are logged to the agent_actions
    table for observability and ForgeSmith analysis.

    Returns the same dict format as run_agent().
    """
    effective_timeout = timeout or PROCESS_TIMEOUT
    start_time = time.time()
    is_exempt = role in EARLY_TERM_EXEMPT_ROLES

    # Tracking state
    turn_count = 0
    turns_without_file_change = 0
    # Scale early termination with budget — larger budgets get more reading time
    # but never exceed 2x the base threshold (prevents overly generous scaling)
    effective_kill_turns = min(
        EARLY_TERM_KILL_TURNS * 2,
        max(EARLY_TERM_KILL_TURNS, int((max_turns or EARLY_TERM_KILL_TURNS) * 0.2))
    )
    effective_final_warn_turns = max(EARLY_TERM_FINAL_WARN_TURNS, int(effective_kill_turns * 0.8))
    effective_warn_turns = max(EARLY_TERM_WARN_TURNS, int(effective_kill_turns * 0.5))
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
    loop_warning_injected = False
    early_term_reason: str | None = None
    loop_detected_details: str | None = None  # noqa: F841
    agent_signaled_done = False
    early_complete_reason: str | None = None

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,  # 4MB buffer for large file reads
        )
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

                        # Track file-modifying tools
                        if tool_name in ("Edit", "Write", "NotebookEdit"):
                            turn_has_file_change = True
                            has_any_file_change = True
                        elif tool_name == "Bash":
                            bash_cmd = tool_input.get("command", "")
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
                    else:
                        turns_without_file_change += 1

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
                            if (turns_without_file_change >= effective_warn_turns
                                    and not warning_injected):
                                log(f"  [EarlyTerm] WARNING: {turns_without_file_change} "
                                    f"turns without file changes (role={role}, "
                                    f"turn ~{turn_count}). WARNING: You have not "
                                    f"written any code yet. Your job is to WRITE "
                                    f"CODE, not read the entire codebase. Start "
                                    f"writing NOW or you will be replaced.", output)
                                warning_injected = True

                            if (turns_without_file_change >= effective_final_warn_turns
                                    and not final_warning_injected):
                                log(f"  [EarlyTerm] FINAL WARNING: "
                                    f"{turns_without_file_change} turns without file "
                                    f"changes (role={role}, turn ~{turn_count}). "
                                    f"FINAL WARNING: You are about to be TERMINATED "
                                    f"for wasting budget. Write code in the NEXT "
                                    f"TURN or a new agent takes over. Do NOT read "
                                    f"another file. Kill threshold: "
                                    f"{effective_kill_turns}.", output)
                                final_warning_injected = True

                            if turns_without_file_change >= effective_kill_turns:
                                early_term_reason = (
                                    f"Agent terminated: {turns_without_file_change} "
                                    f"consecutive turns without file changes. "
                                    f"Agent spent all turns reading instead of "
                                    f"writing code — replaced with stricter agent"
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


async def dispatch_agent(
    cmd: list[str],
    role: str,
    output: Any,
    max_turns: int,
    task_id: int,
    cycle: int,
    system_prompt: str | None = None,
    project_dir: str | None = None,
    args: Any = None,
) -> dict[str, Any]:
    """Dispatch an agent using the configured provider (Claude or Ollama).

    For Claude: delegates to run_agent_streaming() or run_agent().
    For Ollama: delegates to run_ollama_agent().

    Returns the same result dict format regardless of provider.
    """
    # Security: verify skill file integrity before building any agent prompt
    if not verify_skill_integrity():
        return {
            "result": "blocked",
            "output": "CRITICAL: Skill integrity verification failed — agent dispatch refused. "
                      "Run --regenerate-manifest if changes are intentional.",
            "cost": 0,
            "duration": 0,
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

    # Default: Claude via run_agent_streaming
    use_streaming = role not in EARLY_TERM_EXEMPT_ROLES
    if use_streaming:
        return await run_agent_streaming(
            cmd, role=role, output=output, max_turns=max_turns,
            task_id=task_id, run_id=None, cycle_number=cycle,
            project_dir=project_dir)
    else:
        return await run_agent(cmd)
