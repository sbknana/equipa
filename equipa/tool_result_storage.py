"""Tool result size limit + disk persistence — pure Python stdlib implementation.

When agent output exceeds 50KB, save to disk and inject file reference instead.
Prevents context bloat from large test outputs, file reads, or grep results.

Ported from nirholas-claude-code/src/utils/toolResultStorage.ts.
Pure Python stdlib only — NO pip dependencies.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# --- Constants ---

# Size threshold in bytes (50KB default — 200K tokens at 4 chars/token)
DEFAULT_MAX_RESULT_SIZE_BYTES: int = 50_000

# Preview size in bytes for the reference message
PREVIEW_SIZE_BYTES: int = 2000

# XML tag used to wrap persisted output messages
PERSISTED_OUTPUT_TAG: str = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG: str = "</persisted-output>"

# Subdirectory name for tool results within a session
TOOL_RESULTS_SUBDIR: str = "tool-results"


# --- Helper Functions ---

def format_file_size(size_bytes: int) -> str:
    """Format byte size as human-readable string (KB, MB)."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def generate_preview(content: str, max_bytes: int) -> tuple[str, bool]:
    """Generate a preview of content, truncating at a newline boundary when possible.

    Returns (preview_text, has_more).
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


def get_tool_results_dir(session_dir: str) -> Path:
    """Get the tool results directory for this session.

    Returns: session_dir/tool-results
    """
    return Path(session_dir) / TOOL_RESULTS_SUBDIR


def ensure_tool_results_dir(session_dir: str) -> None:
    """Ensure the session-specific tool results directory exists."""
    tool_results_dir = get_tool_results_dir(session_dir)
    tool_results_dir.mkdir(parents=True, exist_ok=True)


def get_tool_result_path(session_dir: str, tool_id: str, is_json: bool) -> Path:
    """Get the filepath where a tool result would be persisted."""
    ext = "json" if is_json else "txt"
    return get_tool_results_dir(session_dir) / f"{tool_id}.{ext}"


# --- Core Persistence Functions ---

def persist_tool_result(
    content: str | dict | list,
    tool_id: str,
    session_dir: str,
) -> tuple[Path, int, bool, str, bool] | tuple[None, None, None, str, None]:
    """Persist a tool result to disk and return information about the persisted file.

    Args:
        content: The tool result content to persist (string, dict, or list)
        tool_id: The ID of the tool that produced the result
        session_dir: Session directory path

    Returns:
        Success: (filepath, original_size, is_json, preview, has_more)
        Failure: (None, None, None, error_message, None)
    """
    is_json = isinstance(content, (dict, list))

    ensure_tool_results_dir(session_dir)
    filepath = get_tool_result_path(session_dir, tool_id, is_json)

    # Serialize content
    if is_json:
        try:
            content_str = json.dumps(content, indent=2, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return None, None, None, f"JSON serialization failed: {e}", None
    else:
        content_str = str(content)

    # Write to disk with exclusive create flag (wx) — skip if already exists
    # This prevents re-writing the same content on every compaction replay
    try:
        # Use os.open with O_CREAT | O_EXCL for atomic create-or-fail
        fd = os.open(filepath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, content_str.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Already persisted on a prior turn, fall through to preview
        pass
    except OSError as e:
        return None, None, None, f"File write failed: {e}", None

    # Generate a preview
    preview, has_more = generate_preview(content_str, PREVIEW_SIZE_BYTES)

    return filepath, len(content_str), is_json, preview, has_more


def build_large_tool_result_message(
    filepath: Path,
    original_size: int,
    preview: str,
    has_more: bool,
) -> str:
    """Build a message for large tool results with preview."""
    message = f"{PERSISTED_OUTPUT_TAG}\n"
    message += f"Output too large ({format_file_size(original_size)}). "
    message += f"Full output saved to: {filepath}\n\n"
    message += f"Preview (first {format_file_size(PREVIEW_SIZE_BYTES)}):\n"
    message += preview
    message += "\n...\n" if has_more else "\n"
    message += PERSISTED_OUTPUT_CLOSING_TAG
    return message


def maybe_persist_large_result(
    content: str | dict | list,
    tool_id: str,
    session_dir: str,
    threshold: int = DEFAULT_MAX_RESULT_SIZE_BYTES,
) -> str | dict | list:
    """Handle large tool results by persisting to disk instead of truncating.

    Returns the original content if no persistence needed, or a string message
    with a reference to the persisted file.

    Args:
        content: Tool result content (string, dict, or list)
        tool_id: Unique ID for this tool invocation
        session_dir: Session directory path
        threshold: Size threshold in bytes (default 50KB)

    Returns:
        Original content if under threshold, or replacement message string
    """
    # Check size
    if isinstance(content, str):
        size = len(content)
    else:
        # Approximate JSON size by serializing
        try:
            size = len(json.dumps(content, ensure_ascii=False))
        except (TypeError, ValueError):
            # Cannot serialize, pass through unchanged
            return content

    if size <= threshold:
        return content

    # Persist to disk
    result = persist_tool_result(content, tool_id, session_dir)
    filepath, original_size, is_json, preview, has_more = result

    if filepath is None:
        # Persistence failed, return original content unchanged
        return content

    # Return replacement message
    return build_large_tool_result_message(filepath, original_size, preview, has_more)


# --- Integration Utilities ---

def is_content_already_compacted(content: str) -> bool:
    """Check if content was already compacted by this system.

    All budget-produced content starts with the tag.
    """
    return isinstance(content, str) and content.startswith(PERSISTED_OUTPUT_TAG)


def process_agent_output(
    raw_output: str,
    agent_id: str,
    session_dir: str,
    threshold: int = DEFAULT_MAX_RESULT_SIZE_BYTES,
) -> str:
    """Process agent output and persist if too large.

    Main integration point for orchestrator.py — call this before compaction.

    Args:
        raw_output: Raw agent output text
        agent_id: Unique agent identifier (e.g., "developer-123-turn-5")
        session_dir: Session directory path
        threshold: Size threshold in bytes

    Returns:
        Original output or replacement message if persisted
    """
    if not raw_output or is_content_already_compacted(raw_output):
        return raw_output

    result = maybe_persist_large_result(raw_output, agent_id, session_dir, threshold)

    # Result is either the original string or a replacement message string
    return result if isinstance(result, str) else raw_output
