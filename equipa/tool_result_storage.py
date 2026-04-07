"""Tool result persistence for large agent outputs.

Prevents context bloat by saving large tool results (>50KB) to disk and
injecting file references instead of full content. Pure Python stdlib only.

Ported from nirholas-claude-code/src/utils/toolResultStorage.ts.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# --- Constants ---

# Size threshold in bytes for tool result persistence (50KB default)
DEFAULT_PERSIST_THRESHOLD = 50_000

# Subdirectory name for tool results within a session
TOOL_RESULTS_SUBDIR = "tool-results"

# XML tag used to wrap persisted output messages
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# Preview size in bytes for the reference message
PREVIEW_SIZE_BYTES = 2000


# --- Helper Functions ---

def format_file_size(size_bytes: int) -> str:
    """Format byte count as human-readable string (KB, MB)."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def generate_preview(content: str, max_bytes: int = PREVIEW_SIZE_BYTES) -> tuple[str, bool]:
    """Generate a preview of content, truncating at a newline boundary when possible.

    Returns:
        (preview, has_more): preview string and flag indicating if content was truncated
    """
    if len(content) <= max_bytes:
        return content, False

    # Find the last newline within the limit to avoid cutting mid-line
    truncated = content[:max_bytes]
    last_newline = truncated.rfind("\n")

    # If we found a newline reasonably close to the limit, use it
    # Otherwise fall back to the exact limit
    cut_point = last_newline if last_newline > max_bytes * 0.5 else max_bytes

    return content[:cut_point], True


def is_content_already_compacted(content: str) -> bool:
    """Check if content has already been compacted/persisted.

    All persistence-produced content starts with the tag. Using .startswith()
    avoids false-positives when the tag appears elsewhere in content.
    """
    return content.startswith(PERSISTED_OUTPUT_TAG)


# --- Core Persistence Functions ---

def get_tool_results_dir(session_dir: str) -> Path:
    """Get the tool results directory for this session (session_dir/tool-results).

    Args:
        session_dir: Absolute path to session directory

    Returns:
        Path object for tool results directory
    """
    return Path(session_dir) / TOOL_RESULTS_SUBDIR


def ensure_tool_results_dir(session_dir: str) -> None:
    """Ensure the session-specific tool results directory exists.

    Args:
        session_dir: Absolute path to session directory

    Raises:
        OSError: If directory cannot be created (permission, etc.)
    """
    tool_results_dir = get_tool_results_dir(session_dir)
    tool_results_dir.mkdir(parents=True, exist_ok=True)


def get_tool_result_path(session_dir: str, agent_id: str, is_json: bool = False) -> Path:
    """Get the filepath where a tool result would be persisted.

    Args:
        session_dir: Absolute path to session directory
        agent_id: Unique identifier for this agent invocation (e.g., "developer-123-turn-5")
        is_json: Whether to use .json extension (default: .txt)

    Returns:
        Path object for the persisted file
    """
    ext = "json" if is_json else "txt"
    return get_tool_results_dir(session_dir) / f"{agent_id}.{ext}"


def persist_tool_result(
    content: str,
    agent_id: str,
    session_dir: str,
) -> dict | None:
    """Persist a tool result to disk and return information about the persisted file.

    Uses 'x' mode for atomic write-once behavior — prevents re-writing
    the same content on every turn when context compaction replays messages.

    Args:
        content: The tool result content to persist (string)
        agent_id: Unique identifier for this agent invocation
        session_dir: Absolute path to session directory

    Returns:
        Dict with filepath, original_size, preview, has_more on success,
        or None on failure
    """
    try:
        ensure_tool_results_dir(session_dir)
    except (OSError, IOError) as e:
        # Cannot create directory — return None to signal failure
        return None

    filepath = get_tool_result_path(session_dir, agent_id, is_json=False)

    # Use 'x' mode for atomic write-once — fails if file exists (already persisted)
    try:
        with open(filepath, "x", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError:
        # Already persisted on a prior turn, fall through to preview
        pass
    except (OSError, IOError) as e:
        # Filesystem error — return None to signal failure
        return None

    # Generate a preview
    preview, has_more = generate_preview(content, PREVIEW_SIZE_BYTES)

    return {
        "filepath": str(filepath),
        "original_size": len(content),
        "preview": preview,
        "has_more": has_more,
    }


def build_large_tool_result_message(result: dict) -> str:
    """Build a message for large tool results with preview.

    Args:
        result: Dict from persist_tool_result() with filepath, original_size, preview, has_more

    Returns:
        Formatted message string to replace the original content
    """
    message = f"{PERSISTED_OUTPUT_TAG}\n"
    message += f"Output too large ({format_file_size(result['original_size'])}). "
    message += f"Full output saved to: {result['filepath']}\n\n"
    message += f"Preview (first {format_file_size(PREVIEW_SIZE_BYTES)}):\n"
    message += result["preview"]
    message += "\n...\n" if result["has_more"] else "\n"
    message += PERSISTED_OUTPUT_CLOSING_TAG
    return message


# --- Main Integration Point ---

def process_agent_output(
    raw_output: str,
    agent_id: str,
    session_dir: str,
    persist_threshold: int = DEFAULT_PERSIST_THRESHOLD,
) -> str:
    """Process agent output, persisting to disk if over threshold.

    This is the main entry point for equipa/parsing.py compact_agent_output().

    Args:
        raw_output: Raw agent output text
        agent_id: Unique identifier for this agent invocation (e.g., "developer-123-turn-5")
        session_dir: Absolute path to session directory
        persist_threshold: Size threshold in bytes (default 50KB)

    Returns:
        Original output if under threshold, or persistence reference message
    """
    if not raw_output:
        return ""

    # Skip if already compacted
    if is_content_already_compacted(raw_output):
        return raw_output

    # Check size threshold
    size = len(raw_output.encode("utf-8"))
    if size <= persist_threshold:
        return raw_output

    # Persist the entire content
    result = persist_tool_result(raw_output, agent_id, session_dir)
    if result is None:
        # Persistence failed, return original (fallback)
        return raw_output

    # Return the reference message
    return build_large_tool_result_message(result)
