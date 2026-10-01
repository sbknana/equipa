"""EQUIPA checkpoint save/load/clear for agent resume on retry.

Includes soft checkpointing for periodic state snapshots during streaming,
and full checkpoints for agent resume on timeout/max-turns.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
Enhanced with soft checkpointing as part of Phase 2B compaction detection.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import time
import unicodedata
from pathlib import Path

from equipa.constants import CHECKPOINT_DIR


def save_checkpoint(
    task_id: int,
    attempt: int,
    output_text: str,
    role: str = "developer",
) -> Path | None:
    """Save agent output to a checkpoint file for resume on retry.

    Returns the checkpoint file path.
    """
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"task_{task_id}_{role}_attempt_{attempt}.txt"
    filepath = CHECKPOINT_DIR / filename
    try:
        filepath.write_text(output_text, encoding="utf-8")
    except OSError as e:
        print(f"  [Checkpoint] WARNING: Failed to save checkpoint: {e}")
        return None
    return filepath


def load_checkpoint(
    task_id: int,
    role: str = "developer",
) -> tuple[str | None, int]:
    """Load the most recent checkpoint for a task+role.

    Returns (checkpoint_text, attempt_number) or (None, 0) if no checkpoint exists.
    """
    if not CHECKPOINT_DIR.exists():
        return None, 0

    # Find all checkpoints for this task+role, sorted by attempt number
    pattern = f"task_{task_id}_{role}_attempt_*.txt"
    checkpoints = sorted(CHECKPOINT_DIR.glob(pattern))
    if not checkpoints:
        return None, 0

    latest = checkpoints[-1]
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError:
        return None, 0

    # Extract attempt number from filename
    stem = latest.stem  # e.g. task_124_developer_attempt_2
    try:
        attempt = int(stem.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        attempt = 0

    return text, attempt


def clear_checkpoints(task_id: int, role: str | None = None) -> None:
    """Remove checkpoint files for a completed task."""
    if not CHECKPOINT_DIR.exists():
        return
    if role:
        pattern = f"task_{task_id}_{role}_attempt_*.txt"
    else:
        pattern = f"task_{task_id}_*_attempt_*.txt"
    for f in CHECKPOINT_DIR.glob(pattern):
        try:
            f.unlink()
        except OSError:
            pass

    # Also clear soft checkpoints
    soft_pattern = f"task_{task_id}_*_soft_*.json"
    for f in CHECKPOINT_DIR.glob(soft_pattern):
        try:
            f.unlink()
        except OSError:
            pass


# --- Soft Checkpointing ---

# Interval (in turns) between automatic soft checkpoints
SOFT_CHECKPOINT_INTERVAL: int = 10

# Maximum length for truncated result text in soft checkpoints
SOFT_CHECKPOINT_TEXT_LIMIT: int = 2000


def save_soft_checkpoint(
    task_id: int,
    turn_count: int,
    files_changed: set[str],
    files_read: set[str],
    last_result_text: str,
    compaction_count: int = 0,
    compaction_signals: list[dict[str, str]] | None = None,
    role: str = "developer",
) -> Path | None:
    """Save a lightweight soft checkpoint during streaming.

    Called every SOFT_CHECKPOINT_INTERVAL turns. Captures just enough
    state to resume intelligently if context compaction occurs.

    Args:
        task_id: Current task ID.
        turn_count: Current turn number.
        files_changed: Set of files the agent has modified.
        files_read: Set of files the agent has read.
        last_result_text: Most recent agent text output (truncated to limit).
        compaction_count: Number of suspected compaction events so far.
        compaction_signals: List of detected compaction signal dicts.
        role: Agent role (default: developer).

    Returns:
        Path to the saved soft checkpoint file, or None on error.
    """
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"task_{task_id}_{role}_soft_{turn_count}.json"
    filepath = CHECKPOINT_DIR / filename

    # Truncate result text to keep soft checkpoints lightweight
    truncated_text = last_result_text[:SOFT_CHECKPOINT_TEXT_LIMIT]
    if len(last_result_text) > SOFT_CHECKPOINT_TEXT_LIMIT:
        truncated_text += "\n[...truncated...]"

    checkpoint_data = {
        "task_id": task_id,
        "role": role,
        "turn_count": turn_count,
        "timestamp": time.time(),
        "files_changed": sorted(files_changed),
        "files_read": sorted(files_read),
        "last_result_text": truncated_text,
        "compaction_count": compaction_count,
        "compaction_signals": compaction_signals or [],
    }

    try:
        filepath.write_text(
            json.dumps(checkpoint_data, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"  [SoftCheckpoint] WARNING: Failed to save: {e}")
        return None

    return filepath


def load_soft_checkpoint(
    task_id: int,
    role: str = "developer",
) -> dict | None:
    """Load the most recent soft checkpoint for a task+role.

    Returns the checkpoint dict, or None if no soft checkpoint exists.
    """
    if not CHECKPOINT_DIR.exists():
        return None

    pattern = f"task_{task_id}_{role}_soft_*.json"
    checkpoints = sorted(CHECKPOINT_DIR.glob(pattern))
    if not checkpoints:
        return None

    latest = checkpoints[-1]
    try:
        text = latest.read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None


# How much agent-authored free text one recovery prompt scans (review F6 of
# task 3139). Each sanitize() call costs up to about 0.2 s at its 64k input
# cap, and this prompt makes four of them, so the last output and each
# .forge-state.json field are cut first, with a visible marker. The soft
# checkpoint already stores at most SOFT_CHECKPOINT_TEXT_LIMIT characters.
RECOVERY_TEXT_SCAN_LIMIT: int = 16_000
STATE_FIELD_SCAN_LIMIT: int = 4_000

# Unicode categories of characters that break a line or are invisible
# controls: Cc (C0/C1, including newline and tab), Zl and Zp (U+2028,
# U+2029). An agent-recorded path or tool name holding one could start a
# line of its own in the prompt.
_LINE_BREAKING_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def _sanitize_agent_text(
    text: object, label: str, scan_limit: int = STATE_FIELD_SCAN_LIMIT
) -> str:
    """Reject-mode sanitize agent-authored free text for a recovery prompt.

    Text longer than *scan_limit* is cut, with a marker, before scanning;
    the cut can only drop text, so the kept part is scanned whole.
    """
    from lesson_sanitizer import sanitize  # HARD dependency
    # Late import keeps this module free of parsing's git_ops dependency.
    from equipa.parsing import AGENT_OUTPUT_WITHHELD

    text = str(text) if text else ""
    if len(text) > scan_limit:
        dropped = len(text) - scan_limit
        text = f"{text[:scan_limit]}\n[... {dropped} chars not shown]"
    return sanitize(text, label=label) or AGENT_OUTPUT_WITHHELD


def _safe_entry(value: object) -> str:
    """One agent-recorded path or tool name, or the withheld-line marker.

    An entry with a line break or control character is withheld: it could
    start a heading or an order on a line of its own (review F2 of task
    3139). The others are boundary-escaped by the wrapper they render in.
    """
    from equipa.parsing import AGENT_OUTPUT_LINE_WITHHELD

    text = str(value)
    if any(
        unicodedata.category(char) in _LINE_BREAKING_CATEGORIES for char in text
    ):
        return AGENT_OUTPUT_LINE_WITHHELD
    return text


def _join_paths(paths: list) -> str:
    """Comma-join agent-supplied paths, withholding multi-line entries.

    The caller renders the result inside wrap_agent_output(), which escapes
    every wrapper token.
    """
    return ", ".join(_safe_entry(path) for path in paths)


def _format_recovery_prompt(
    state: dict,
    forge_state: dict | None = None,
) -> str:
    """Format a recovery / resume prompt from a state dict.

    Shared by :func:`build_compaction_recovery_context` (within-task soft
    checkpoint path) and :func:`equipa.sessions.build_resume_prompt`
    (orchestrator-cycle path). The session state is a strict superset of the
    soft-checkpoint state, so this single formatter handles both — keys that
    only exist on the session side (``open_files``, ``recent_tool_calls``,
    ``partial_reasoning``) are rendered when present and silently skipped
    otherwise.

    The last output and the ``.forge-state.json`` fields are agent-authored
    and this prompt joins compaction history, so they get the same treatment
    as the compaction summary: reject-mode sanitize (a rejected field becomes
    AGENT_OUTPUT_WITHHELD) inside an escaped ``<task-input>`` block (review
    N3 of task 3129). File paths and tool names are agent-chosen too: they
    render inside their own escaped block, and an entry with a line break or
    control character is withheld (review F2 of task 3139).
    """
    # Late import keeps this module free of parsing's git_ops dependency.
    from equipa.parsing import wrap_agent_output

    parts: list[str] = []

    parts.append(
        "## Context Recovery After Compaction\n\n"
        "**You were working on this task and hit a context limit.** "
        "Here is your saved state. Do NOT re-read files you already read. "
        "Do NOT re-introduce yourself. Resume from where you left off.\n"
    )

    turn = state.get("turn_count", 0)
    files_changed = state.get("files_changed", [])
    files_read = state.get("files_read", [])
    open_files = state.get("open_files", [])
    compaction_count = state.get("compaction_count", 0)
    # Sessions populate ``partial_reasoning``; soft checkpoints use
    # ``last_result_text``. Either is acceptable input here.
    last_text = (
        state.get("partial_reasoning")
        or state.get("last_result_text")
        or ""
    )
    recent_tool_calls = state.get("recent_tool_calls") or []

    parts.append(f"**Turn count at checkpoint:** {turn}")
    parts.append(f"**Compactions detected so far:** {compaction_count}")

    # Paths and tool names: agent-recorded, so one escaped block.
    recorded_lines: list[str] = []
    if files_changed:
        recorded_lines.append(
            f"Files you already changed: {_join_paths(files_changed)}"
        )
    if files_read:
        recorded_lines.append(
            "Files you already read (do NOT re-read): "
            f"{_join_paths(files_read)}"
        )
    if open_files:
        recorded_lines.append(f"Open files: {_join_paths(open_files)}")

    rendered_calls = []
    for call in recent_tool_calls:
        if not isinstance(call, dict):
            continue
        tool = _safe_entry(call.get("tool", "?"))
        turn_num = _safe_entry(call.get("turn", "?"))
        ok = _safe_entry(call.get("ok", "?"))
        rendered_calls.append(f"- turn {turn_num}: {tool} (ok={ok})")
    if rendered_calls:
        recorded_lines.append("Recent tool calls:")
        recorded_lines.extend(rendered_calls)

    if recorded_lines:
        parts.append(
            "\n**Files and tool calls from your saved state:**\n"
            + wrap_agent_output("recorded-paths", "\n".join(recorded_lines))
        )

    if last_text:
        safe_last_text = _sanitize_agent_text(
            last_text, "checkpoint last output",
            scan_limit=RECOVERY_TEXT_SCAN_LIMIT,
        )
        parts.append(
            "\n**Your last output (truncated):**\n"
            + wrap_agent_output("last-output", safe_last_text)
        )

    if forge_state:
        state_lines: list[str] = []
        current_step = forge_state.get("current_step", "")
        if current_step:
            state_lines.append(
                f"- Current step: "
                f"{_sanitize_agent_text(current_step, 'forge-state current_step')}"
            )
        next_action = forge_state.get("next_action", "")
        if next_action:
            state_lines.append(
                f"- Next action: "
                f"{_sanitize_agent_text(next_action, 'forge-state next_action')}"
            )
        state_decisions = forge_state.get("decisions", [])
        if state_decisions:
            decisions_text = ", ".join(str(d) for d in state_decisions[:5])
            state_lines.append(
                f"- Decisions made: "
                f"{_sanitize_agent_text(decisions_text, 'forge-state decisions')}"
            )
        state_files = forge_state.get("files_changed", [])
        if state_files:
            state_lines.append(
                f"- Files changed (from state): {_join_paths(state_files)}"
            )
        if state_lines:
            parts.append("\n**Agent state file (.forge-state.json):**")
            parts.append(wrap_agent_output("forge-state", "\n".join(state_lines)))

    parts.append(
        "\n**RESUME NOW.** Pick up from your next action. "
        "Do not waste turns re-reading files."
    )

    return "\n".join(parts)


def build_compaction_recovery_context(
    soft_checkpoint: dict,
    forge_state: dict | None = None,
) -> str:
    """Build a strong recovery prompt from soft checkpoint + .forge-state.json.

    Used when a compaction is detected to give the continuation agent
    maximum context about what was already accomplished.

    Args:
        soft_checkpoint: Data from load_soft_checkpoint().
        forge_state: Optional data from .forge-state.json on disk.

    Returns:
        Formatted context string for injection into the agent prompt.
    """
    return _format_recovery_prompt(soft_checkpoint, forge_state=forge_state)
