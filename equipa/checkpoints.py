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
    parts: list[str] = []

    parts.append(
        "## Context Recovery After Compaction\n\n"
        "**You were working on this task and hit a context limit.** "
        "Here is your saved state. Do NOT re-read files you already read. "
        "Do NOT re-introduce yourself. Resume from where you left off.\n"
    )

    # Soft checkpoint data
    turn = soft_checkpoint.get("turn_count", 0)
    files_changed = soft_checkpoint.get("files_changed", [])
    files_read = soft_checkpoint.get("files_read", [])
    compaction_count = soft_checkpoint.get("compaction_count", 0)
    last_text = soft_checkpoint.get("last_result_text", "")

    parts.append(f"**Turn count at checkpoint:** {turn}")
    parts.append(f"**Compactions detected so far:** {compaction_count}")

    if files_changed:
        parts.append(
            f"**Files you already changed:** {', '.join(files_changed)}"
        )

    if files_read:
        parts.append(
            f"**Files you already read (do NOT re-read):** "
            f"{', '.join(files_read)}"
        )

    if last_text:
        parts.append(
            f"\n**Your last output (truncated):**\n```\n{last_text}\n```"
        )

    # .forge-state.json data (agent's own state file)
    if forge_state:
        parts.append("\n**Agent state file (.forge-state.json):**")
        current_step = forge_state.get("current_step", "")
        if current_step:
            parts.append(f"- Current step: {current_step}")
        next_action = forge_state.get("next_action", "")
        if next_action:
            parts.append(f"- Next action: {next_action}")
        state_decisions = forge_state.get("decisions", [])
        if state_decisions:
            parts.append(
                f"- Decisions made: {', '.join(str(d) for d in state_decisions[:5])}"
            )
        state_files = forge_state.get("files_changed", [])
        if state_files:
            parts.append(
                f"- Files changed (from state): {', '.join(state_files)}"
            )

    parts.append(
        "\n**RESUME NOW.** Pick up from your next action. "
        "Do not waste turns re-reading files."
    )

    return "\n".join(parts)
