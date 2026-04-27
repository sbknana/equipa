"""Integration tests for tool result storage with parsing.py.

Copyright 2026 Forgeborn
"""

import tempfile
from pathlib import Path

from equipa.parsing import compact_agent_output
from equipa.tool_result_storage import (
    DEFAULT_PERSIST_THRESHOLD,
    PERSISTED_OUTPUT_TAG,
    TOOL_RESULTS_SUBDIR,
)


def test_compact_agent_output_integration_small():
    """Test that small outputs pass through unchanged."""
    output = """RESULT: success
SUMMARY: Fixed the login bug
FILES_CHANGED:
- src/auth.py
- tests/test_auth.py
BLOCKERS: none
REFLECTION: Used TDD approach, all tests now pass
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = compact_agent_output(
            output,
            max_words=200,
            agent_id="dev-1",
            session_dir=tmpdir,
        )
        
        # Should compact normally without persistence
        assert "Fixed the login bug" in result
        assert "src/auth.py" in result
        # Should NOT create tool-results directory
        tool_results_dir = Path(tmpdir) / TOOL_RESULTS_SUBDIR
        assert not tool_results_dir.exists()


def test_compact_agent_output_integration_large():
    """Test that large outputs are persisted to disk."""
    # Create output that exceeds 50KB threshold
    large_output = "x" * (DEFAULT_PERSIST_THRESHOLD + 1000)
    large_output += "\n\nRESULT: success\nSUMMARY: Generated huge output\n"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        result = compact_agent_output(
            large_output,
            max_words=200,
            agent_id="dev-2",
            session_dir=tmpdir,
        )
        
        # Should return persistence reference, NOT compacted output
        assert result.startswith(PERSISTED_OUTPUT_TAG)
        assert "Output too large" in result
        assert "dev-2.txt" in result
        
        # Should create tool-results directory and file
        tool_results_dir = Path(tmpdir) / TOOL_RESULTS_SUBDIR
        assert tool_results_dir.exists()
        
        persisted_file = tool_results_dir / "dev-2.txt"
        assert persisted_file.exists()
        
        # Verify full content was persisted
        content = persisted_file.read_text(encoding="utf-8")
        assert len(content) == len(large_output)
        assert "Generated huge output" in content


def test_compact_agent_output_without_persistence():
    """Test that compact_agent_output works without session_dir (backward compat)."""
    output = """RESULT: success
SUMMARY: Fixed the login bug
FILES_CHANGED:
- src/auth.py
BLOCKERS: none
"""
    
    # No agent_id/session_dir — persistence disabled
    result = compact_agent_output(output, max_words=200)
    
    # Should compact normally
    assert "Fixed the login bug" in result
    assert "src/auth.py" in result


def test_compact_agent_output_custom_threshold():
    """Test custom persistence threshold."""
    # Create output between 5KB and 50KB
    medium_output = "y" * 10000
    medium_output += "\n\nRESULT: success\nSUMMARY: Medium sized output\n"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # With default threshold (50KB), should NOT persist
        result_default = compact_agent_output(
            medium_output,
            max_words=200,
            agent_id="dev-3a",
            session_dir=tmpdir,
            persist_threshold=DEFAULT_PERSIST_THRESHOLD,
        )
        assert not result_default.startswith(PERSISTED_OUTPUT_TAG)
        
        # With custom threshold (5KB), SHOULD persist
        result_custom = compact_agent_output(
            medium_output,
            max_words=200,
            agent_id="dev-3b",
            session_dir=tmpdir,
            persist_threshold=5000,
        )
        assert result_custom.startswith(PERSISTED_OUTPUT_TAG)


def test_compact_agent_output_idempotent():
    """Test that compacting already-persisted output is idempotent."""
    # Create output with persistence tag (simulating already-persisted content)
    already_persisted = f"""{PERSISTED_OUTPUT_TAG}
Output too large (100.0KB). Full output saved to: /tmp/previous.txt

Preview (first 2.0KB):
Some preview content...
...
</persisted-output>"""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        result = compact_agent_output(
            already_persisted,
            max_words=200,
            agent_id="dev-4",
            session_dir=tmpdir,
        )
        
        # Should return unchanged (no double-persistence)
        assert result == already_persisted


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
