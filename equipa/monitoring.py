"""EQUIPA monitoring module — loop detection, budget tracking, and streaming helpers.

Layer 6: Agent monitoring functions including LoopDetector, tool loop detection,
budget messages, cost limits, git change detection, early complete parsing,
output hash computation, and streaming result building.

Extracted from forge_orchestrator.py as part of Phase 2 monolith split.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import os
import subprocess

from equipa.constants import (
    BUDGET_CHECK_INTERVAL,
    BUDGET_CRITICAL_THRESHOLD,
    BUDGET_HALFWAY_THRESHOLD,
    COST_LIMITS,
    DYNAMIC_BUDGET_BLOCKED_RATIO,
    DYNAMIC_BUDGET_EXTEND_TURNS,
    DYNAMIC_BUDGET_MIN_TURNS,
    DYNAMIC_BUDGET_START_RATIO,
    EARLY_TERM_STUCK_PHRASES,
    MONOLOGUE_EXEMPT_TURNS,
    MONOLOGUE_THRESHOLD,
)
from equipa.hooks import fire as fire_hook_sync

# --- Compaction Detection Constants ---
COMPACTION_REINTRO_PHRASES: tuple[str, ...] = (
    "how can i help",
    "what would you like me to",
    "how can i assist",
    "what can i do for you",
    "i'd be happy to help",
    "let me know what you'd like",
    "what task would you like",
    "i'm ready to help",
)

# Minimum turns without new tool calls before quality drop is flagged
COMPACTION_STALE_TOOL_TURNS: int = 5

# Minimum repeated phrase ratio (0.0-1.0) to flag repetitive output
COMPACTION_REPETITION_THRESHOLD: float = 0.4


# --- Compaction Signal Detection ---

def detect_compaction_signals(
    text: str,
    turn_count: int,
    files_read: set[str],
    recent_tool_calls: list[str],
    turns_since_last_tool: int,
) -> list[dict[str, str]]:
    """Detect signals that a context compaction may have occurred.

    Returns a list of signal dicts, each with 'type' and 'detail' keys.
    An empty list means no compaction signals detected.

    Signals checked:
    1. Agent re-introduces itself (generic help phrases)
    2. Agent re-reads files it already read (tracked via files_read set)
    3. Output quality drops (no new tool calls for N turns, repetitive text)
    """
    signals: list[dict[str, str]] = []
    text_lower = text.lower()

    # Signal 1: Agent re-introduces itself
    for phrase in COMPACTION_REINTRO_PHRASES:
        if phrase in text_lower:
            signals.append({
                "type": "reintroduction",
                "detail": f"Agent re-introduced itself at turn {turn_count}: "
                          f"'{phrase}'",
            })
            break  # One match is enough

    # Signal 2: Agent re-reads already-read files
    for tool_call in recent_tool_calls:
        if tool_call.startswith("Read|"):
            file_path = tool_call.split("|", 1)[1].strip()
            if file_path in files_read:
                signals.append({
                    "type": "file_reread",
                    "detail": f"Agent re-read '{file_path}' at turn "
                              f"{turn_count} (already read earlier)",
                })

    # Signal 3: No new tool calls for several turns
    if turns_since_last_tool >= COMPACTION_STALE_TOOL_TURNS:
        signals.append({
            "type": "stale_tools",
            "detail": f"No new tool calls for {turns_since_last_tool} turns "
                      f"at turn {turn_count}",
        })

    # Signal 4: Repetitive text output (high ratio of repeated sentences)
    sentences = [s.strip() for s in text.split(".") if len(s.strip()) > 20]
    if len(sentences) >= 4:
        unique_sentences = set(s.lower() for s in sentences)
        repetition_ratio = 1.0 - (len(unique_sentences) / len(sentences))
        if repetition_ratio >= COMPACTION_REPETITION_THRESHOLD:
            signals.append({
                "type": "repetitive_output",
                "detail": f"Repetitive text detected at turn {turn_count}: "
                          f"{repetition_ratio:.0%} repeated sentences",
            })

    return signals


# --- Loop Detection Constants ---
LOOP_WARNING_THRESHOLD: int = 3   # inject "try different approach" warning
LOOP_TERMINATE_THRESHOLD: int = 5  # terminate agent early and mark blocked


# --- Stuck Phrase Detection ---

def _check_stuck_phrases(text: str) -> str | None:
    """Check if text contains any stuck signal phrases.

    Returns the matched phrase or None.
    """
    text_lower = text.lower()
    for phrase in EARLY_TERM_STUCK_PHRASES:
        if phrase in text_lower:
            return phrase
    return None


# --- Monologue Detection ---

def _check_monologue(
    consecutive_text_only_turns: int, turn_count: int
) -> str | None:
    """Check if the agent is monologuing (consecutive text-only messages without tool use).

    Returns:
        "terminate" if consecutive_text_only_turns >= MONOLOGUE_THRESHOLD and past exempt period.
        "warn" if consecutive_text_only_turns == MONOLOGUE_THRESHOLD - 1 and past exempt period.
        None otherwise.
    """
    # Do not trigger during the initial planning period
    if turn_count <= MONOLOGUE_EXEMPT_TURNS:
        return None

    if consecutive_text_only_turns >= MONOLOGUE_THRESHOLD:
        return "terminate"
    elif consecutive_text_only_turns == MONOLOGUE_THRESHOLD - 1:
        return "warn"

    return None


# --- Budget Messages ---

def _get_budget_message(turn_count: int, max_turns: int) -> str | None:
    """Generate a budget visibility message based on current turn count.

    Returns an escalating budget message at periodic intervals:
    - Every BUDGET_CHECK_INTERVAL turns: simple status update
    - At BUDGET_HALFWAY_THRESHOLD (50%): HALFWAY warning
    - At BUDGET_CRITICAL_THRESHOLD (75%): CRITICAL warning

    The most severe applicable message wins — if a turn triggers both
    a periodic check and a threshold crossing, the threshold message
    is returned (it carries the stronger signal).

    Args:
        turn_count: Current turn number (0-indexed tool calls).
        max_turns: Maximum turns allocated for this agent run.

    Returns:
        str: Budget message to inject, or None if no message needed.
    """
    if not max_turns or max_turns <= 0 or turn_count <= 0:
        return None

    # Only check on interval turns
    if turn_count % BUDGET_CHECK_INTERVAL != 0:
        return None

    remaining = max_turns - turn_count
    fraction_used = turn_count / max_turns

    if fraction_used >= BUDGET_CRITICAL_THRESHOLD:
        return (
            f"CRITICAL: Only {remaining} turns left out of {max_turns}. "
            f"Write files NOW. Commit partial progress if needed."
        )
    elif fraction_used >= BUDGET_HALFWAY_THRESHOLD:
        return (
            f"HALFWAY: {turn_count}/{max_turns} turns used. "
            f"{remaining} turns left. Prioritize writing code."
        )
    else:
        return (
            f"Budget: {turn_count}/{max_turns} turns used. "
            f"{remaining} turns left."
        )


# --- Cost Limit Checking ---

def _check_cost_limit(
    total_cost: float | None,
    complexity: str,
    config_limits: dict | None = None,
) -> str | None:
    """Check if total accumulated cost exceeds the limit for the given complexity tier.

    Cost limits scale per task complexity:
    - simple: $3.00 (default)
    - medium: $5.00 (default)
    - complex: $10.00 (default)
    - epic: $20.00 (default)

    Limits can be overridden via dispatch_config "cost_limits" dict.

    Args:
        total_cost: Total cost accumulated so far (in USD). None treated as $0.00.
        complexity: Task complexity tier ('simple', 'medium', 'complex', 'epic').
        config_limits: Optional dict overriding COST_LIMITS values from dispatch_config.

    Returns:
        str: Termination reason if cost exceeded, or None if within budget.
    """
    if total_cost is None or total_cost <= 0:
        return None

    # Resolve the limit: config override > default constants
    limits = config_limits if config_limits else COST_LIMITS
    # Default to $10.00 for unknown complexity tiers
    limit = limits.get(complexity, 10.0)

    if total_cost > limit:
        fire_hook_sync(
            "on_cost_warning",
            total_cost=total_cost, limit=limit, complexity=complexity,
        )
        return f"Cost limit exceeded: ${total_cost:.2f} > ${limit:.2f}"

    return None


# --- Git Change Detection ---

def _check_git_changes(project_dir: str | None) -> bool:
    """Check if the project's git working tree has any changes (modified, staged, or untracked).

    Runs `git diff --stat` and `git status --short` in the project directory.
    Returns True if either command produces output (indicating file changes).

    Args:
        project_dir: Path to the project directory (must be a git repo).

    Returns:
        bool: True if file changes detected, False otherwise (including errors).
    """
    if not project_dir:
        return False

    project_dir_str = str(project_dir)
    if not os.path.isdir(project_dir_str):
        return False

    try:
        # Check for unstaged changes
        diff_result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=project_dir_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff_result.returncode == 0 and diff_result.stdout.strip():
            return True

        # Check for staged/untracked files
        status_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_dir_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status_result.returncode == 0 and status_result.stdout.strip():
            return True

    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        # If git isn't available or times out, don't false-positive
        return False

    return False


# --- Early Complete Parsing ---

def _parse_early_complete(text: str) -> str | None:
    """Parse an EARLY_COMPLETE: <reason> signal from agent text.

    Agents can signal "I am done" mid-run to avoid burning remaining turns.
    The marker must appear at the start of a line (not inside code blocks,
    inline code, quoted blocks, or string literals).

    Returns the reason string if found, or None.
    """
    if "EARLY_COMPLETE:" not in text:
        return None

    # Check if we're inside a code block (triple backticks)
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()

        # Toggle code block state on triple-backtick lines
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        # Skip quoted blocks (> prefix)
        if stripped.startswith(">"):
            continue

        # Skip lines where EARLY_COMPLETE appears inside inline code (`...`)
        if "EARLY_COMPLETE:" in stripped:
            # Check if it's inside backticks on this line
            before_marker = stripped.split("EARLY_COMPLETE:")[0]
            # Count backticks before the marker — odd count means inside inline code
            if before_marker.count("`") % 2 == 1:
                continue

            # Check if it's inside double quotes
            if before_marker.count('"') % 2 == 1:
                continue

            # Valid EARLY_COMPLETE: marker — extract reason
            idx = stripped.index("EARLY_COMPLETE:")
            reason = stripped[idx + len("EARLY_COMPLETE:"):].strip()
            if reason:
                return reason

    return None


# --- Output Hash Computation ---

def _compute_output_hash(content) -> str:
    """Compute a SHA256 hash of tool result content for loop detection.

    Handles all content formats from Claude stream-json: string, list of
    content blocks, None, or empty values. Returns a 64-char hex digest.

    Args:
        content: Tool result content — str, list of content blocks, or None.

    Returns:
        str: SHA256 hex digest of the normalized content.
    """
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = " ".join(parts) if parts else ""
    else:
        text = str(content)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# --- Tool Signature / Loop Detection ---

# Lookup for tool signature key parameter (used by loop detection)
_TOOL_SIG_KEY: dict[str, str] = {
    "Bash": "command", "Read": "file_path", "Grep": "pattern",
    "Glob": "pattern", "Edit": "file_path", "Write": "file_path",
}


def _build_tool_signature(tool_name: str, tool_input: dict) -> str:
    """Build a fingerprint string for loop detection."""
    key = _TOOL_SIG_KEY.get(tool_name)
    param = str(tool_input.get(key, "") if key else
                next(iter(tool_input.values()), "") if tool_input else "")[:80]
    return f"{tool_name}|{param}"


def _detect_tool_loop(
    tool_history: list[str],
    tool_errors: list[str | None],
    warn_threshold: int = 3,
    terminate_threshold: int = 5,
    tool_output_hashes: list[str] | None = None,
) -> tuple[str, int, str | None]:
    """Detect when an agent repeats the same failing tool operation.

    Detects two patterns:
    1. Consecutive: Same tool signature repeated (A-A-A-A)
    2. Alternating: Two-tool cycle repeated (A-B-A-B-A-B)

    Enhanced to consider error patterns — only triggers on failed operations,
    not successful ones.

    When tool_output_hashes is provided, uses output hash matching to
    distinguish true stuck loops (same input AND same output) from retries
    after external state changes (same input but different output). When
    consecutive calls have identical output hashes, the effective thresholds
    are halved because identical output is strong confirmation the agent
    is stuck in a deterministic loop.

    Args:
        tool_history: List of tool signatures (tool_name|params)
        tool_errors: List of error summaries (None if success, string if error)
        warn_threshold: Number of repetitions before warning (default 3)
        terminate_threshold: Number of repetitions before termination (default 5)
        tool_output_hashes: Optional list of SHA256 output hashes per tool call.
            When provided and consecutive hashes match, thresholds are halved.

    Returns:
        tuple: (action, count, last_sig) where action is "ok", "warn", or "terminate"
    """
    if len(tool_history) < 2:
        return ("ok", 0, None)

    # Count consecutive occurrences of the last tool signature
    last_sig = tool_history[-1]
    last_error = tool_errors[-1] if tool_errors else None

    # If the last operation succeeded (no error), reset the loop counter
    # This prevents false positives when retrying after fixing a bug
    if not last_error:
        return ("ok", 0, last_sig)

    # --- Consecutive same-signature detection (A-A-A-A) ---
    consecutive = 1
    consecutive_failures = 1  # count only failing operations
    consecutive_same_output = 1  # count consecutive identical output hashes

    last_idx = len(tool_history) - 1
    last_output_hash = (
        tool_output_hashes[last_idx]
        if tool_output_hashes and last_idx < len(tool_output_hashes)
        else None
    )

    for i in range(len(tool_history) - 2, -1, -1):
        if tool_history[i] == last_sig:
            consecutive += 1
            # Only count if this was also a failure
            if i < len(tool_errors) and tool_errors[i]:
                consecutive_failures += 1
            # Track output hash matches for enhanced loop detection
            if (last_output_hash is not None
                    and tool_output_hashes
                    and i < len(tool_output_hashes)
                    and tool_output_hashes[i] == last_output_hash):
                consecutive_same_output += 1
            else:
                # Output changed — stop counting same-output streak
                # (but continue counting same-input streak for base thresholds)
                last_output_hash = None
        else:
            break

    # When outputs are identical, use halved thresholds (stronger stuck signal)
    # Minimum thresholds: warn at 2, terminate at 3
    if consecutive_same_output >= 2 and tool_output_hashes:
        effective_warn = max(2, warn_threshold // 2)
        effective_terminate = max(3, terminate_threshold // 2)
    else:
        effective_warn = warn_threshold
        effective_terminate = terminate_threshold

    # Check consecutive detection first
    if consecutive_failures >= effective_terminate:
        return ("terminate", consecutive_failures, last_sig)
    elif consecutive_failures >= effective_warn:
        return ("warn", consecutive_failures, last_sig)

    # --- Alternating two-tool cycle detection (A-B-A-B) ---
    # Only check if we have at least 4 entries and consecutive didn't trigger
    if len(tool_history) >= 4:
        sig_a = tool_history[-1]
        sig_b = tool_history[-2]
        if sig_a != sig_b:
            # Count how many entries match the A-B alternating pattern
            alternating_count = 0
            for i in range(len(tool_history) - 1, -1, -1):
                pos_in_pair = (len(tool_history) - 1 - i) % 2
                expected = sig_a if pos_in_pair == 0 else sig_b
                if tool_history[i] == expected:
                    # Only count if this entry was a failure
                    if i < len(tool_errors) and tool_errors[i]:
                        alternating_count += 1
                    else:
                        break  # success breaks the alternating failure chain
                else:
                    break

            # Alternating thresholds are +1 above consecutive thresholds
            # because the pattern involves two distinct signatures.
            # With defaults (warn=3, terminate=5): warn at 4, terminate at 6.
            if alternating_count >= terminate_threshold + 1:
                return ("terminate", alternating_count, f"{sig_a} <-> {sig_b}")
            elif alternating_count >= warn_threshold + 1:
                return ("warn", alternating_count, f"{sig_a} <-> {sig_b}")

    return ("ok", consecutive_failures, last_sig)


# --- Streaming Result Builder ---

def _build_streaming_result(
    turn_count: int,
    duration: float,
    has_any_file_change: bool,
    early_term_reason: str | None,
    agent_signaled_done: bool,
    early_complete_reason: str | None,
    result_data: dict | None,
    all_text_chunks: list[str],
) -> dict:
    """Build the result dict from run_agent_streaming state."""
    result = {
        "success": False,
        "result_text": "",
        "num_turns": turn_count,
        "duration": duration,
        "cost": None,
        "errors": [],
        "has_file_changes": has_any_file_change,
    }

    if early_term_reason:
        result["errors"].append(early_term_reason)
        result["early_terminated"] = True
        result["early_term_reason"] = early_term_reason

    if agent_signaled_done and not early_term_reason:
        result["early_completed"] = True
        result["early_complete_reason"] = early_complete_reason

    if result_data:
        final_text = result_data.get("result", "")
        if ("RESULT:" not in final_text and all_text_chunks
                and any("RESULT:" in chunk for chunk in all_text_chunks)):
            result["result_text"] = "\n".join(all_text_chunks)
        else:
            result["result_text"] = final_text
        result["num_turns"] = result_data.get("num_turns", turn_count)
        result["cost"] = result_data.get("total_cost_usd")

        subtype = result_data.get("subtype", "")
        if subtype == "error_max_turns":
            result["success"] = True
            result["errors"].append("Agent hit max turns limit")
        elif result_data.get("is_error"):
            result["errors"].append(
                f"Agent error: {result_data.get('result', 'unknown')}")
        else:
            result["success"] = not bool(early_term_reason)
    elif agent_signaled_done and not early_term_reason:
        result["result_text"] = "\n".join(all_text_chunks)
        result["success"] = True
    else:
        result["result_text"] = "\n".join(all_text_chunks)
        if has_any_file_change and not early_term_reason:
            result["success"] = True
            result["errors"].append("No result message from agent (process exited), "
                                    "but file changes detected — treating as partial success")

    return result


# --- LoopDetector Class ---

class LoopDetector:
    """Detect when an agent repeats the same failing pattern across cycles.

    Tracks fingerprints of agent output (error messages, result status,
    blockers) and detects repetition. Legitimate retries (where the agent
    makes changes between attempts) are excluded from repetition counts.

    Usage:
        detector = LoopDetector()
        for cycle in ...:
            result = await run_agent(cmd)
            action = detector.record(result, cycle)
            if action == "terminate":
                break
            elif action == "warn":
                compaction_history.append(detector.warning_message())
    """

    def __init__(
        self,
        warning_threshold: int = LOOP_WARNING_THRESHOLD,
        terminate_threshold: int = LOOP_TERMINATE_THRESHOLD,
    ):
        self.warning_threshold = warning_threshold
        self.terminate_threshold = terminate_threshold
        self.fingerprints: list[tuple] = []      # ordered list of fingerprints per cycle
        self.consecutive_same: int = 0   # consecutive identical fingerprints
        self.last_fingerprint: str | None = None
        self.warned: bool = False         # have we injected a warning already?

    def _fingerprint(self, result: dict) -> str:
        """Extract a normalized fingerprint from agent output.

        The fingerprint captures the essential pattern of what the agent did
        and what went wrong. It includes:
        - The RESULT: line (success/blocked/failed)
        - Error messages from the result dict
        - The BLOCKERS: section content
        - The SUMMARY: line

        Files changed are used to detect legitimate retries — if files differ
        between cycles, the repetition counter resets.
        """
        text = result.get("result_text", "") if isinstance(result, dict) else ""
        errors = result.get("errors", []) if isinstance(result, dict) else []

        parts: list[str] = []

        # Extract structured output markers
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("RESULT:"):
                parts.append(stripped.split(":", 1)[1].strip().lower())
            elif stripped.startswith("BLOCKERS:"):
                blocker_val = stripped.split(":", 1)[1].strip().lower()
                if blocker_val and blocker_val != "none":
                    parts.append(f"blocker:{blocker_val}")
            elif stripped.startswith("SUMMARY:"):
                parts.append(f"summary:{stripped.split(':', 1)[1].strip().lower()}")

        # Include error messages (normalized)
        for err in errors[:3]:
            normalized = err.lower().strip()[:200]
            parts.append(f"error:{normalized}")

        return "|".join(sorted(parts)) if parts else "empty"

    def _get_files_changed(self, result: dict) -> list[str]:
        """Extract FILES_CHANGED from result text for retry detection."""
        # Late import to avoid circular dependency with parsing module
        from equipa.parsing import parse_developer_output

        text = result.get("result_text", "") if isinstance(result, dict) else ""
        return parse_developer_output(text)

    def record(self, result: dict, cycle: int) -> str:
        """Record a cycle result and return the recommended action.

        Returns:
            "ok"        - no loop detected, continue normally
            "warn"      - repetition detected (>=warning_threshold), inject warning
            "terminate" - severe repetition (>=terminate_threshold), stop the agent
        """
        fp = self._fingerprint(result)
        files = self._get_files_changed(result)
        self.fingerprints.append((cycle, fp, files))

        if fp == self.last_fingerprint:
            # Same pattern — but check if files changed (legitimate retry)
            prev_files = self.fingerprints[-2][2] if len(self.fingerprints) >= 2 else []
            if sorted(files) != sorted(prev_files) and files:
                # Different files touched — this is a real retry, reset counter
                self.consecutive_same = 1
            else:
                self.consecutive_same += 1
        else:
            self.consecutive_same = 1
            self.last_fingerprint = fp

        if self.consecutive_same >= self.terminate_threshold:
            fire_hook_sync(
                "on_stuck_detected",
                action="terminate", consecutive=self.consecutive_same,
                cycle=cycle, fingerprint=fp,
            )
            return "terminate"
        elif self.consecutive_same >= self.warning_threshold and not self.warned:
            self.warned = True
            fire_hook_sync(
                "on_stuck_detected",
                action="warn", consecutive=self.consecutive_same,
                cycle=cycle, fingerprint=fp,
            )
            return "warn"

        return "ok"

    def warning_message(self) -> str:
        """Build a warning to inject into the agent's next prompt context."""
        return (
            "## LOOP DETECTED — Try a Different Approach\n\n"
            "The orchestrator has detected that you are repeating the same "
            "failing pattern for multiple consecutive cycles. Your last "
            f"{self.consecutive_same} attempts produced identical error "
            "signatures.\n\n"
            "**You MUST try a fundamentally different approach:**\n"
            "- If a file edit keeps failing, try a different file or strategy\n"
            "- If a build error persists, investigate the root cause instead of retrying\n"
            "- If you are blocked, report it as a blocker rather than retrying\n"
            "- If you tried approach A three times, try approach B or C\n\n"
            "**If you repeat the same approach again, the orchestrator will "
            "terminate your session and mark the task as blocked.**\n"
        )

    def termination_summary(self) -> str:
        """Build an error summary string for agent_runs.error_summary."""
        return (
            f"Loop detected: agent repeated the same failing pattern "
            f"{self.consecutive_same} times. Last fingerprint: "
            f"{self.last_fingerprint[:200] if self.last_fingerprint else 'unknown'}"
        )


# --- Dynamic Turn Budget ---

def calculate_dynamic_budget(max_turns: int) -> tuple[int, int]:
    """Calculate the starting turn budget for an agent.

    Starts at DYNAMIC_BUDGET_START_RATIO of max_turns, with a floor of
    DYNAMIC_BUDGET_MIN_TURNS to ensure agents can at least read files.

    Returns (starting_budget, max_turns) tuple.
    """
    starting = max(DYNAMIC_BUDGET_MIN_TURNS, int(max_turns * DYNAMIC_BUDGET_START_RATIO))
    # Don't exceed the max
    starting = min(starting, max_turns)
    return starting, max_turns


def adjust_dynamic_budget(
    current_budget: int, max_turns: int, result_text: str
) -> int:
    """Adjust dynamic turn budget based on agent output.

    - If FILES_CHANGED found in output: extend by DYNAMIC_BUDGET_EXTEND_TURNS (up to max)
    - If RESULT: blocked found in output: reduce remaining by DYNAMIC_BUDGET_BLOCKED_RATIO
    - Otherwise: no change

    Returns the new budget.
    """
    if not result_text:
        return current_budget

    result_text_lower = result_text.lower()

    # Check for RESULT: blocked — reduce budget
    if "result: blocked" in result_text_lower or "result:blocked" in result_text_lower:
        reduced = max(DYNAMIC_BUDGET_MIN_TURNS,
                      int(current_budget * DYNAMIC_BUDGET_BLOCKED_RATIO))
        return min(reduced, max_turns)

    # Check for FILES_CHANGED with actual content (not "none" or empty)
    files_changed_patterns = ["files_changed:", "files changed:"]
    has_files_changed = False
    for pattern in files_changed_patterns:
        idx = result_text_lower.find(pattern)
        if idx >= 0:
            # Extract the value after the marker
            after = result_text[idx + len(pattern):idx + len(pattern) + 200].strip()
            # Consider it real if it's not "none", empty, or just whitespace
            first_line = after.split("\n")[0].strip()
            if first_line and first_line.lower() not in ("none", "n/a", "no files", ""):
                has_files_changed = True
                break

    if has_files_changed:
        extended = min(current_budget + DYNAMIC_BUDGET_EXTEND_TURNS, max_turns)
        return extended

    return current_budget
