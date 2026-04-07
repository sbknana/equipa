"""Tests for tool result storage persistence.

Copyright 2026 Forgeborn
"""

import os
import tempfile
from pathlib import Path

import pytest

from equipa.tool_result_storage import (
    DEFAULT_PERSIST_THRESHOLD,
    PERSISTED_OUTPUT_TAG,
    PREVIEW_SIZE_BYTES,
    TOOL_RESULTS_SUBDIR,
    build_large_tool_result_message,
    ensure_tool_results_dir,
    format_file_size,
    generate_preview,
    get_tool_result_path,
    get_tool_results_dir,
    is_content_already_compacted,
    persist_tool_result,
    process_agent_output,
)


class TestFormatFileSize:
    """Tests for format_file_size()."""

    def test_bytes(self):
        assert format_file_size(512) == "512B"
        assert format_file_size(1023) == "1023B"

    def test_kilobytes(self):
        assert format_file_size(1024) == "1.0KB"
        assert format_file_size(2048) == "2.0KB"
        assert format_file_size(51200) == "50.0KB"

    def test_megabytes(self):
        assert format_file_size(1024 * 1024) == "1.0MB"
        assert format_file_size(5 * 1024 * 1024) == "5.0MB"


class TestGeneratePreview:
    """Tests for generate_preview()."""

    def test_short_content_no_truncation(self):
        content = "Short content"
        preview, has_more = generate_preview(content, 100)
        assert preview == content
        assert has_more is False

    def test_long_content_with_newline(self):
        # Content with newline near the cut point
        content = "Line 1\n" * 200  # Each line is 7 chars
        preview, has_more = generate_preview(content, 1000)
        assert has_more is True
        assert len(preview) <= 1000
        # Should cut at newline boundary (or exact limit if no newline past 50%)
        # The implementation finds last newline within limit that's past 50% of limit
        # Since we have many newlines, it should find one
        assert "\n" in preview

    def test_long_content_no_newline(self):
        # Content with no newlines
        content = "x" * 5000
        preview, has_more = generate_preview(content, 1000)
        assert has_more is True
        assert len(preview) == 1000

    def test_custom_max_bytes(self):
        content = "x" * 5000
        preview, has_more = generate_preview(content, 500)
        assert has_more is True
        assert len(preview) == 500


class TestIsContentAlreadyCompacted:
    """Tests for is_content_already_compacted()."""

    def test_compacted_content(self):
        content = f"{PERSISTED_OUTPUT_TAG}\nSome preview...\n"
        assert is_content_already_compacted(content) is True

    def test_uncompacted_content(self):
        content = "Regular output from agent"
        assert is_content_already_compacted(content) is False

    def test_tag_in_middle(self):
        # Tag appears in middle but not at start
        content = f"Some output\n{PERSISTED_OUTPUT_TAG}\nMore"
        assert is_content_already_compacted(content) is False


class TestDirectoryFunctions:
    """Tests for directory helper functions."""

    def test_get_tool_results_dir(self, tmp_path):
        session_dir = tmp_path / "session-123"
        result = get_tool_results_dir(str(session_dir))
        assert result == session_dir / TOOL_RESULTS_SUBDIR

    def test_ensure_tool_results_dir(self, tmp_path):
        session_dir = tmp_path / "session-456"
        ensure_tool_results_dir(str(session_dir))
        tool_results_dir = session_dir / TOOL_RESULTS_SUBDIR
        assert tool_results_dir.exists()
        assert tool_results_dir.is_dir()

    def test_get_tool_result_path(self, tmp_path):
        session_dir = tmp_path / "session-789"
        agent_id = "developer-123-turn-5"
        
        # Text file
        path = get_tool_result_path(str(session_dir), agent_id, is_json=False)
        assert path == session_dir / TOOL_RESULTS_SUBDIR / f"{agent_id}.txt"
        
        # JSON file
        path_json = get_tool_result_path(str(session_dir), agent_id, is_json=True)
        assert path_json == session_dir / TOOL_RESULTS_SUBDIR / f"{agent_id}.json"


class TestPersistToolResult:
    """Tests for persist_tool_result()."""

    def test_persist_small_content(self, tmp_path):
        session_dir = tmp_path / "session-001"
        agent_id = "agent-001"
        content = "Small output"

        result = persist_tool_result(content, agent_id, str(session_dir))
        
        assert result is not None
        assert result["original_size"] == len(content)
        assert result["preview"] == content
        assert result["has_more"] is False
        
        # Verify file was written
        filepath = Path(result["filepath"])
        assert filepath.exists()
        assert filepath.read_text(encoding="utf-8") == content

    def test_persist_large_content(self, tmp_path):
        session_dir = tmp_path / "session-002"
        agent_id = "agent-002"
        content = "x" * 10000

        result = persist_tool_result(content, agent_id, str(session_dir))
        
        assert result is not None
        assert result["original_size"] == 10000
        assert len(result["preview"]) == PREVIEW_SIZE_BYTES
        assert result["has_more"] is True

    def test_persist_idempotent(self, tmp_path):
        session_dir = tmp_path / "session-003"
        agent_id = "agent-003"
        content = "Content to persist"

        # First persist
        result1 = persist_tool_result(content, agent_id, str(session_dir))
        assert result1 is not None
        
        # Second persist (should not overwrite)
        result2 = persist_tool_result(content, agent_id, str(session_dir))
        assert result2 is not None
        assert result2["filepath"] == result1["filepath"]


class TestBuildLargeToolResultMessage:
    """Tests for build_large_tool_result_message()."""

    def test_message_format(self):
        result = {
            "filepath": "/tmp/session/tool-results/agent-001.txt",
            "original_size": 100000,
            "preview": "Preview content...",
            "has_more": True,
        }

        message = build_large_tool_result_message(result)
        
        assert message.startswith(PERSISTED_OUTPUT_TAG)
        assert message.endswith("</persisted-output>")
        assert "97.7KB" in message  # formatted size
        assert result["filepath"] in message
        assert result["preview"] in message
        assert "..." in message  # truncation indicator

    def test_message_no_truncation(self):
        result = {
            "filepath": "/tmp/session/tool-results/agent-002.txt",
            "original_size": 1000,
            "preview": "Short content",
            "has_more": False,
        }

        message = build_large_tool_result_message(result)
        
        # Should not have truncation indicator
        lines = message.split("\n")
        # The "..." line should not appear before closing tag
        assert not any(line == "..." for line in lines[:-2])


class TestProcessAgentOutput:
    """Tests for process_agent_output() integration."""

    def test_small_output_unchanged(self, tmp_path):
        session_dir = tmp_path / "session-101"
        agent_id = "agent-101"
        content = "Small output under threshold"

        result = process_agent_output(content, agent_id, str(session_dir))
        
        # Should return original content
        assert result == content
        
        # Should not create file
        tool_results_dir = session_dir / TOOL_RESULTS_SUBDIR
        assert not tool_results_dir.exists()

    def test_large_output_persisted(self, tmp_path):
        session_dir = tmp_path / "session-102"
        agent_id = "agent-102"
        content = "x" * (DEFAULT_PERSIST_THRESHOLD + 1000)

        result = process_agent_output(content, agent_id, str(session_dir))
        
        # Should return reference message
        assert result.startswith(PERSISTED_OUTPUT_TAG)
        assert "Output too large" in result
        
        # Should create file
        tool_results_dir = session_dir / TOOL_RESULTS_SUBDIR
        assert tool_results_dir.exists()

    def test_custom_threshold(self, tmp_path):
        session_dir = tmp_path / "session-103"
        agent_id = "agent-103"
        content = "x" * 10000
        
        # Use custom threshold of 5000 bytes
        result = process_agent_output(content, agent_id, str(session_dir), persist_threshold=5000)
        
        # Should be persisted (10000 > 5000)
        assert result.startswith(PERSISTED_OUTPUT_TAG)

    def test_empty_output(self, tmp_path):
        session_dir = tmp_path / "session-104"
        agent_id = "agent-104"

        result = process_agent_output("", agent_id, str(session_dir))
        assert result == ""

    def test_already_compacted_output(self, tmp_path):
        session_dir = tmp_path / "session-105"
        agent_id = "agent-105"
        content = f"{PERSISTED_OUTPUT_TAG}\nAlready compacted\n</persisted-output>"

        result = process_agent_output(content, agent_id, str(session_dir))
        
        # Should return unchanged (skip re-compaction)
        assert result == content

    def test_utf8_encoding(self, tmp_path):
        session_dir = tmp_path / "session-106"
        agent_id = "agent-106"
        # Content with Unicode characters
        content = "Unicode: 🚀 测试 " * 10000

        result = process_agent_output(content, agent_id, str(session_dir))
        
        # Should persist (encoded size will exceed threshold)
        assert result.startswith(PERSISTED_OUTPUT_TAG)
        
        # Verify file is readable
        filepath = session_dir / TOOL_RESULTS_SUBDIR / f"{agent_id}.txt"
        assert filepath.exists()
        persisted_content = filepath.read_text(encoding="utf-8")
        assert "🚀" in persisted_content
        assert "测试" in persisted_content


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_read_only_directory(self, tmp_path):
        """Test behavior when directory is read-only."""
        session_dir = tmp_path / "session-ro"
        session_dir.mkdir()
        # Make directory read-only
        session_dir.chmod(0o444)
        
        agent_id = "agent-ro"
        content = "x" * (DEFAULT_PERSIST_THRESHOLD + 1000)
        
        try:
            result = process_agent_output(content, agent_id, str(session_dir))
            # Should fall back to returning original content
            assert result == content
        finally:
            # Restore permissions for cleanup
            session_dir.chmod(0o755)

    def test_special_characters_in_agent_id(self, tmp_path):
        """Test agent IDs with special characters (sanitized by Path)."""
        session_dir = tmp_path / "session-special"
        # Path will handle sanitization
        agent_id = "agent-with-slashes/and\\backslashes"
        content = "x" * (DEFAULT_PERSIST_THRESHOLD + 1000)
        
        # Should not crash, though the path might be different
        result = process_agent_output(content, agent_id, str(session_dir))
        assert PERSISTED_OUTPUT_TAG in result or result == content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
