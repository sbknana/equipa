"""Tests for tool result persistence module.

Copyright 2026 Forgeborn
"""

import json
import os
from pathlib import Path
import tempfile

import pytest

from equipa.tool_result_storage import (
    DEFAULT_MAX_RESULT_SIZE_BYTES,
    PERSISTED_OUTPUT_TAG,
    PERSISTED_OUTPUT_CLOSING_TAG,
    PREVIEW_SIZE_BYTES,
    format_file_size,
    generate_preview,
    get_tool_results_dir,
    get_tool_result_path,
    persist_tool_result,
    build_large_tool_result_message,
    maybe_persist_large_result,
    is_content_already_compacted,
    process_agent_output,
)


# --- Utility Tests ---


def test_format_file_size():
    """Test human-readable file size formatting."""
    assert format_file_size(512) == "512B"
    assert format_file_size(1024) == "1.0KB"
    assert format_file_size(1536) == "1.5KB"
    assert format_file_size(1024 * 1024) == "1.0MB"
    assert format_file_size(1536 * 1024) == "1.5MB"


def test_generate_preview_small():
    """Preview for content under limit returns full content."""
    content = "Hello world"
    preview, has_more = generate_preview(content, 100)
    assert preview == content
    assert has_more is False


def test_generate_preview_large():
    """Preview for large content truncates at newline boundary."""
    content = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n" * 100
    preview, has_more = generate_preview(content, 200)
    assert has_more is True
    assert len(preview) <= 200
    # Should end at a newline boundary (or close to limit)
    assert preview.count("\n") >= 1


def test_generate_preview_no_newline():
    """Preview without newlines falls back to exact limit."""
    content = "x" * 1000
    preview, has_more = generate_preview(content, 100)
    assert has_more is True
    assert len(preview) == 100


# --- Path Tests ---


def test_get_tool_results_dir():
    """Test tool results directory path construction."""
    session_dir = "/tmp/session-123"
    tool_dir = get_tool_results_dir(session_dir)
    assert tool_dir == Path("/tmp/session-123/tool-results")


def test_get_tool_result_path():
    """Test tool result file path construction."""
    session_dir = "/tmp/session-123"
    path_txt = get_tool_result_path(session_dir, "tool-456", is_json=False)
    path_json = get_tool_result_path(session_dir, "tool-789", is_json=True)

    assert path_txt == Path("/tmp/session-123/tool-results/tool-456.txt")
    assert path_json == Path("/tmp/session-123/tool-results/tool-789.json")


# --- Persistence Tests ---


def test_persist_tool_result_string(tmp_path):
    """Persist a string result to disk."""
    content = "Test output\n" * 100
    tool_id = "test-tool-1"

    filepath, size, is_json, preview, has_more = persist_tool_result(
        content, tool_id, str(tmp_path)
    )

    assert filepath is not None
    assert filepath.exists()
    assert size == len(content)
    assert is_json is False
    assert len(preview) > 0
    assert filepath.read_text() == content


def test_persist_tool_result_dict(tmp_path):
    """Persist a dict result as JSON."""
    content = {"status": "success", "data": [1, 2, 3], "nested": {"key": "value"}}
    tool_id = "test-tool-2"

    filepath, size, is_json, preview, has_more = persist_tool_result(
        content, tool_id, str(tmp_path)
    )

    assert filepath is not None
    assert filepath.exists()
    assert size > 0
    assert is_json is True
    stored = json.loads(filepath.read_text())
    assert stored == content


def test_persist_tool_result_list(tmp_path):
    """Persist a list result as JSON."""
    content = ["item1", "item2", "item3"]
    tool_id = "test-tool-3"

    filepath, size, is_json, preview, has_more = persist_tool_result(
        content, tool_id, str(tmp_path)
    )

    assert filepath is not None
    assert is_json is True
    stored = json.loads(filepath.read_text())
    assert stored == content


def test_persist_tool_result_idempotent(tmp_path):
    """Persisting the same tool_id twice should not overwrite."""
    content1 = "First write"
    content2 = "Second write"
    tool_id = "test-tool-4"

    # First write
    filepath1, _, _, _, _ = persist_tool_result(content1, tool_id, str(tmp_path))
    assert filepath1.read_text() == content1

    # Second write (should be no-op due to FileExistsError catch)
    filepath2, _, _, _, _ = persist_tool_result(content2, tool_id, str(tmp_path))
    assert filepath2 == filepath1
    # Original content should remain
    assert filepath2.read_text() == content1


def test_persist_tool_result_json_error(tmp_path):
    """Unserializable content returns error."""
    # Create an unserializable object (set is not JSON-serializable)
    content = {"data": {1, 2, 3}}
    tool_id = "test-tool-5"

    result = persist_tool_result(content, tool_id, str(tmp_path))
    filepath, size, is_json, error_msg, has_more = result

    assert filepath is None
    assert "JSON serialization failed" in error_msg


# --- Message Building Tests ---


def test_build_large_tool_result_message():
    """Test replacement message construction."""
    filepath = Path("/tmp/session/tool-results/tool-123.txt")
    original_size = 100_000
    preview = "First line\nSecond line"
    has_more = True

    message = build_large_tool_result_message(filepath, original_size, preview, has_more)

    assert PERSISTED_OUTPUT_TAG in message
    assert PERSISTED_OUTPUT_CLOSING_TAG in message
    assert str(filepath) in message
    assert "97.7KB" in message  # format_file_size(100_000)
    assert "First line" in message
    assert "..." in message


# --- Integration Tests ---


def test_maybe_persist_large_result_small(tmp_path):
    """Small results pass through unchanged."""
    content = "Small output"
    result = maybe_persist_large_result(content, "tool-1", str(tmp_path))
    assert result == content


def test_maybe_persist_large_result_large(tmp_path):
    """Large results are persisted and replaced."""
    content = "x" * 60_000
    tool_id = "tool-large"

    result = maybe_persist_large_result(content, tool_id, str(tmp_path))

    # Result should be a string replacement message
    assert isinstance(result, str)
    assert result != content
    assert PERSISTED_OUTPUT_TAG in result
    assert "tool-large.txt" in result

    # File should exist
    filepath = get_tool_result_path(str(tmp_path), tool_id, is_json=False)
    assert filepath.exists()
    assert filepath.read_text() == content


def test_maybe_persist_large_result_custom_threshold(tmp_path):
    """Custom threshold can be specified."""
    content = "x" * 1500
    tool_id = "tool-custom"

    # With default threshold (50KB), this should pass through
    result1 = maybe_persist_large_result(content, tool_id, str(tmp_path))
    assert result1 == content

    # With custom low threshold (1KB), this should be persisted
    result2 = maybe_persist_large_result(content, "tool-custom-2", str(tmp_path), threshold=1000)
    assert isinstance(result2, str)
    assert PERSISTED_OUTPUT_TAG in result2


def test_is_content_already_compacted():
    """Test detection of already-compacted content."""
    normal_content = "Regular output text"
    compacted_content = f"{PERSISTED_OUTPUT_TAG}\nOutput too large...\n{PERSISTED_OUTPUT_CLOSING_TAG}"

    assert is_content_already_compacted(normal_content) is False
    assert is_content_already_compacted(compacted_content) is True
    assert is_content_already_compacted("") is False


def test_process_agent_output_small(tmp_path):
    """Small agent output passes through unchanged."""
    raw_output = "Agent result: success\nFiles changed: 3"
    result = process_agent_output(raw_output, "agent-1", str(tmp_path))
    assert result == raw_output


def test_process_agent_output_large(tmp_path):
    """Large agent output is persisted."""
    raw_output = "Result line\n" * 10_000  # ~120KB
    agent_id = "developer-123-turn-5"

    result = process_agent_output(raw_output, agent_id, str(tmp_path))

    assert result != raw_output
    assert PERSISTED_OUTPUT_TAG in result
    assert "developer-123-turn-5.txt" in result

    # File should exist
    filepath = get_tool_result_path(str(tmp_path), agent_id, is_json=False)
    assert filepath.exists()


def test_process_agent_output_idempotent(tmp_path):
    """Processing already-compacted content is a no-op."""
    compacted = f"{PERSISTED_OUTPUT_TAG}\nAlready compacted\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    result = process_agent_output(compacted, "agent-2", str(tmp_path))
    assert result == compacted


def test_process_agent_output_empty(tmp_path):
    """Empty output passes through."""
    assert process_agent_output("", "agent-3", str(tmp_path)) == ""
    assert process_agent_output(None, "agent-4", str(tmp_path)) is None


# --- Permission Tests ---


def test_persist_tool_result_creates_secure_file(tmp_path):
    """Persisted files should have secure permissions (0o600)."""
    content = "Secure content"
    tool_id = "secure-tool"

    filepath, _, _, _, _ = persist_tool_result(content, tool_id, str(tmp_path))

    # Check file permissions on Unix systems
    if os.name != "nt":  # Skip on Windows
        stat_info = filepath.stat()
        # mode & 0o777 extracts permission bits
        assert (stat_info.st_mode & 0o777) == 0o600
