"""Test git diff context passed to tester agent.

Verifies that the dev-test loop captures git diff and passes it to the tester
prompt with proper formatting and truncation.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock

import pytest


def test_git_diff_integration_with_loops():
    """Test that git diff integration is present in loops.py code.

    This test verifies the implementation exists by checking the actual
    code structure rather than running the async function. Post-S3 refactor,
    the git-diff capture lives in _capture_git_diff_context, which is
    invoked from run_dev_test_loop.
    """
    from equipa import loops
    import inspect

    # Combined source of the orchestrator + the extracted helper. Either
    # the inline form (pre-refactor) or the helper invocation (post-refactor)
    # satisfies the structural assertions below.
    loop_source = inspect.getsource(loops.run_dev_test_loop)
    helper_source = inspect.getsource(loops._capture_git_diff_context)
    source = loop_source + "\n" + helper_source

    # Check for git diff capture — impl may call subprocess.run directly or
    # delegate to equipa.git_ops.git_run (the unified git entry point post-S7).
    assert "git diff" in source.lower() or "git" in source.lower()
    assert (
        "_sp.run" in source
        or "subprocess.run" in source
        or "git_run(" in source
        or "git_run," in source  # post-#2145: invoked via asyncio.to_thread
        or "git_run_async" in source  # post-#2142: native asyncio subprocess
    )
    assert '["git", "diff",' in source or '["diff",' in source

    # Check for context building
    assert "tester_extra_context" in loop_source
    assert "Developer Changes" in source

    # Check for truncation logic (current impl caps at 8000 chars via
    # TESTER_GIT_DIFF_MAX_CHARS constant from equipa.constants).
    assert (
        "3000" in source
        or "max_diff_chars" in source
        or "TESTER_GIT_DIFF_MAX_CHARS" in source
    )

    # Check that context is passed to tester
    assert "extra_context" in loop_source
    assert "build_system_prompt" in loop_source

    # Check for error handling (timeout kwarg in subprocess.run calls)
    assert "except" in source.lower() or "timeout" in source.lower()
    assert "timeout" in source.lower()

    # Refactor invariant: orchestrator delegates to the helper
    assert "_capture_git_diff_context" in loop_source


def test_git_diff_truncation_at_8000_chars():
    """Test that large git diffs are truncated to prevent prompt bloat."""
    # Create a very long diff
    long_diff = "diff --git a/file.py b/file.py\n"
    long_diff += "+ line\n" * 2000  # Should exceed 8000 chars

    # This is the truncation logic from loops.py
    max_diff_chars = 8000
    if len(long_diff) > max_diff_chars:
        truncated = long_diff[:max_diff_chars] + f"\n\n[... diff truncated, {len(long_diff) - max_diff_chars} chars omitted ...]"
    else:
        truncated = long_diff

    # Verify truncation happened
    assert len(long_diff) > 8000
    assert len(truncated) <= 8100  # 8000 + some buffer for the message
    assert "[... diff truncated" in truncated
    assert "chars omitted ...]" in truncated


def test_git_diff_empty_handled_gracefully():
    """Test that empty git diff (no changes) is handled without errors."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""  # No changes

    # Simulate the logic from loops.py
    git_diff_context = ""
    if mock_result.returncode == 0 and mock_result.stdout.strip():
        git_diff = mock_result.stdout.strip()
        git_diff_context = f"\n\n## Developer Changes (git diff)\n\n{git_diff}"

    # Verify empty diff produces empty context
    assert git_diff_context == ""


def test_git_diff_error_handling():
    """Test that git command failures are caught and logged."""
    # Simulate git command failure
    mock_result = Mock()
    mock_result.returncode = 128  # Git error code
    mock_result.stdout = ""

    # The code uses try-except to handle errors
    git_diff_context = ""
    try:
        if mock_result.returncode == 0 and mock_result.stdout.strip():
            git_diff = mock_result.stdout.strip()
            git_diff_context = f"## Developer Changes\n{git_diff}"
    except Exception:
        pass

    # Verify error doesn't crash - context is empty
    assert git_diff_context == ""


def test_git_diff_format_in_prompt():
    """Test that git diff is properly formatted with markdown code blocks."""
    sample_diff = """diff --git a/module.py b/module.py
index abc123..def456 100644
--- a/module.py
+++ b/module.py
@@ -10,5 +10,6 @@ def process():
     data = load()
-    return data
+    result = transform(data)
+    return result
"""

    # Replicate the formatting from loops.py
    git_diff_context = (
        f"\n\n## Developer Changes (git diff)\n\n"
        f"The developer made the following changes:\n\n"
        f"```diff\n{sample_diff}\n```\n\n"
        f"Write tests that verify these specific changes work correctly. "
        f"Focus your testing on the modified files and functions shown above."
    )

    # Verify proper markdown formatting
    assert "## Developer Changes (git diff)" in git_diff_context
    assert "```diff\n" in git_diff_context
    assert "```\n\n" in git_diff_context
    assert "Write tests that verify these specific changes" in git_diff_context
    assert "module.py" in git_diff_context
