#!/usr/bin/env python3
"""Integration test for tool result persistence through compact_agent_output."""

import sys
import tempfile
from pathlib import Path

# Import module directly
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

parsing = load_module("parsing", Path(__file__).parent / "equipa" / "parsing.py")
trs = load_module("tool_result_storage", Path(__file__).parent / "equipa" / "tool_result_storage.py")

print("Testing integration: parsing.compact_agent_output with persistence...")

# Test 1: Small output - no persistence
with tempfile.TemporaryDirectory() as tmpdir:
    small_output = "RESULT: success\nSUMMARY: Fixed bug\nFILES_CHANGED:\n- file.py"
    result = parsing.compact_agent_output(
        small_output,
        max_words=200,
        agent_id="test-1",
        session_dir=tmpdir,
    )
    assert trs.PERSISTED_OUTPUT_TAG not in result
    assert "Fixed bug" in result
    print("✓ Small output not persisted")

# Test 2: Large output - should persist
with tempfile.TemporaryDirectory() as tmpdir:
    # Create 100KB of structured output
    large_output = "RESULT: success\n"
    large_output += "SUMMARY: Fixed major performance issue\n"
    large_output += "FILES_CHANGED:\n- performance.py\n"
    large_output += "REFLECTION: " + ("Very detailed analysis. " * 5000) + "\n"

    result = parsing.compact_agent_output(
        large_output,
        max_words=200,
        agent_id="test-2",
        session_dir=tmpdir,
    )

    # Output should be replaced with persistence reference
    assert trs.PERSISTED_OUTPUT_TAG in result
    assert "Output too large" in result
    assert "Full output saved to:" in result

    # Verify file was created
    tool_results_dir = Path(tmpdir) / "tool-results"
    assert tool_results_dir.exists()
    persisted_files = list(tool_results_dir.glob("test-2.txt"))
    assert len(persisted_files) == 1

    # Verify file contains original content
    with open(persisted_files[0], 'r') as f:
        saved_content = f.read()
    assert "Fixed major performance issue" in saved_content
    assert "Very detailed analysis." in saved_content

    print("✓ Large output persisted correctly")

# Test 3: Already compacted output - pass through
with tempfile.TemporaryDirectory() as tmpdir:
    already_compacted = f"{trs.PERSISTED_OUTPUT_TAG}\nAlready saved\n{trs.PERSISTED_OUTPUT_CLOSING_TAG}"
    result = parsing.compact_agent_output(
        already_compacted,
        max_words=200,
        agent_id="test-3",
        session_dir=tmpdir,
    )
    assert result == already_compacted
    print("✓ Already compacted content passed through")

# Test 4: No agent_id/session_dir - persistence disabled
large_output_2 = "RESULT: success\n" + ("x" * 60000)
result = parsing.compact_agent_output(large_output_2, max_words=200)
# Should be compacted normally, not persisted
assert trs.PERSISTED_OUTPUT_TAG not in result
assert len(result.split()) <= 210  # ~200 words max
print("✓ Persistence disabled when agent_id/session_dir omitted")

# Test 5: Custom threshold
with tempfile.TemporaryDirectory() as tmpdir:
    medium_output = "RESULT: success\n" + ("x" * 10000)  # 10KB

    # With 20KB threshold, should NOT persist
    result = parsing.compact_agent_output(
        medium_output,
        max_words=200,
        agent_id="test-5a",
        session_dir=tmpdir,
        persist_threshold=20_000,
    )
    assert trs.PERSISTED_OUTPUT_TAG not in result

    # With 5KB threshold, SHOULD persist
    result = parsing.compact_agent_output(
        medium_output,
        max_words=200,
        agent_id="test-5b",
        session_dir=tmpdir,
        persist_threshold=5_000,
    )
    assert trs.PERSISTED_OUTPUT_TAG in result
    print("✓ Custom persistence threshold works")

print("\n✅ All 5 integration tests passed!")
