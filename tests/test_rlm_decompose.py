"""Tests for RLM-style REPL decomposition module.

Covers: threshold gating, sub-query spawn, REPL sandbox safety,
token estimation, repo loading, and prompt building.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest

from equipa.rlm_decompose import (
    DECOMPOSE_ELIGIBLE_ROLES,
    MAX_REPL_TURNS,
    MAX_SUB_QUERIES,
    TOKEN_THRESHOLD,
    DecomposeResult,
    ReplSandbox,
    SandboxViolation,
    _call_outer_agent,
    _extract_code_blocks,
    build_decompose_system_prompt,
    build_repo_summary,
    estimate_context_tokens,
    estimate_repo_tokens,
    estimate_tokens,
    load_repo_files,
    run_decompose_session,
    should_decompose,
    validate_repl_code,
)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 1

    def test_known_length(self) -> None:
        assert estimate_tokens("a" * 400) == 100

    def test_non_multiple(self) -> None:
        assert estimate_tokens("abc") == 1  # ceil(3/4)

    def test_large_string(self) -> None:
        text = "x" * 400_000
        assert estimate_tokens(text) == 100_000


class TestEstimateRepoTokens:
    def test_single_file(self) -> None:
        files = {"main.py": "print('hello')"}
        result = estimate_repo_tokens(files)
        assert result > 0

    def test_empty_repo(self) -> None:
        result = estimate_repo_tokens({})
        assert result >= 1


class TestEstimateContextTokens:
    def test_combines_prompt_and_files(self) -> None:
        prompt = "Review this code."
        files = {"a.py": "x = 1", "b.py": "y = 2"}
        tokens = estimate_context_tokens(prompt, files)
        assert tokens > estimate_tokens(prompt)

    def test_empty_files(self) -> None:
        tokens = estimate_context_tokens("prompt", {})
        assert tokens == estimate_tokens("prompt")


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------

class TestShouldDecompose:
    def test_enabled_and_above_threshold(self) -> None:
        assert should_decompose("code-reviewer", TOKEN_THRESHOLD + 1, True)

    def test_enabled_and_at_threshold(self) -> None:
        assert not should_decompose("code-reviewer", TOKEN_THRESHOLD, True)

    def test_disabled_flag(self) -> None:
        assert not should_decompose("code-reviewer", TOKEN_THRESHOLD + 1, False)

    def test_wrong_role(self) -> None:
        assert not should_decompose("developer", TOKEN_THRESHOLD + 1, True)

    def test_below_threshold(self) -> None:
        assert not should_decompose("code-reviewer", 50_000, True)

    def test_integration_tester_eligible(self) -> None:
        assert should_decompose("integration-tester", TOKEN_THRESHOLD + 1, True)

    def test_all_eligible_roles(self) -> None:
        for role in DECOMPOSE_ELIGIBLE_ROLES:
            assert should_decompose(role, TOKEN_THRESHOLD + 1, True)

    def test_non_eligible_roles(self) -> None:
        for role in ("developer", "tester", "planner", "security-reviewer"):
            assert not should_decompose(role, TOKEN_THRESHOLD + 1, True)

    def test_zero_tokens(self) -> None:
        assert not should_decompose("code-reviewer", 0, True)

    def test_negative_tokens(self) -> None:
        assert not should_decompose("code-reviewer", -100, True)


# ---------------------------------------------------------------------------
# AST safety validation
# ---------------------------------------------------------------------------

class TestValidateReplCode:
    def test_safe_code(self) -> None:
        code = "x = [len(f) for f in repo_files]\nprint(sum(x))"
        assert validate_repl_code(code) == []

    def test_blocked_os_import(self) -> None:
        violations = validate_repl_code("import os")
        assert len(violations) == 1
        assert "os" in violations[0]

    def test_blocked_subprocess_import(self) -> None:
        violations = validate_repl_code("import subprocess")
        assert len(violations) == 1
        assert "subprocess" in violations[0]

    def test_blocked_from_import(self) -> None:
        violations = validate_repl_code("from os.path import join")
        assert len(violations) == 1
        assert "os" in violations[0]

    def test_blocked_socket(self) -> None:
        violations = validate_repl_code("import socket")
        assert len(violations) == 1

    def test_blocked_exec_call(self) -> None:
        violations = validate_repl_code("exec('print(1)')")
        assert len(violations) == 1
        assert "exec" in violations[0]

    def test_blocked_eval_call(self) -> None:
        violations = validate_repl_code("eval('1+1')")
        assert len(violations) == 1
        assert "eval" in violations[0]

    def test_blocked_open_call(self) -> None:
        violations = validate_repl_code("open('/etc/passwd')")
        assert len(violations) == 1
        assert "open" in violations[0]

    def test_blocked_compile(self) -> None:
        violations = validate_repl_code("compile('x', '<>', 'exec')")
        assert len(violations) == 1

    def test_blocked_dunder_import(self) -> None:
        violations = validate_repl_code("__import__('os')")
        assert len(violations) == 1

    def test_blocked_system_method(self) -> None:
        violations = validate_repl_code("x.system('ls')")
        assert len(violations) == 1
        assert "system" in violations[0]

    def test_blocked_popen_method(self) -> None:
        violations = validate_repl_code("x.popen('ls')")
        assert len(violations) == 1

    def test_blocked_subprocess_run(self) -> None:
        violations = validate_repl_code("x.run('ls')")
        assert len(violations) == 1

    def test_blocked_async(self) -> None:
        violations = validate_repl_code("async def foo(): pass")
        assert len(violations) == 1
        assert "Async" in violations[0]

    def test_syntax_error(self) -> None:
        violations = validate_repl_code("def (broken")
        assert len(violations) == 1
        assert "SyntaxError" in violations[0]

    def test_multiple_violations(self) -> None:
        code = "import os\nimport subprocess\nexec('x')"
        violations = validate_repl_code(code)
        assert len(violations) == 3

    def test_allowed_re_import(self) -> None:
        assert validate_repl_code("import re") == []

    def test_allowed_json_import(self) -> None:
        assert validate_repl_code("import json") == []

    def test_allowed_math_import(self) -> None:
        assert validate_repl_code("import math") == []

    def test_allowed_collections(self) -> None:
        assert validate_repl_code("import collections") == []

    def test_blocked_http(self) -> None:
        violations = validate_repl_code("import http")
        assert len(violations) == 1

    def test_blocked_urllib(self) -> None:
        violations = validate_repl_code("import urllib")
        assert len(violations) == 1

    def test_blocked_requests(self) -> None:
        violations = validate_repl_code("import requests")
        assert len(violations) == 1

    def test_blocked_ctypes(self) -> None:
        violations = validate_repl_code("import ctypes")
        assert len(violations) == 1

    def test_blocked_signal(self) -> None:
        violations = validate_repl_code("import signal")
        assert len(violations) == 1

    def test_blocked_pathlib(self) -> None:
        violations = validate_repl_code("import pathlib")
        assert len(violations) == 1

    def test_blocked_tempfile(self) -> None:
        violations = validate_repl_code("import tempfile")
        assert len(violations) == 1

    def test_blocked_breakpoint(self) -> None:
        violations = validate_repl_code("breakpoint()")
        assert len(violations) == 1

    def test_blocked_input(self) -> None:
        violations = validate_repl_code("input('prompt')")
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------

class TestReplSandbox:
    @pytest.fixture()
    def sandbox(self) -> ReplSandbox:
        repo = {"main.py": "print('hello')", "utils.py": "def add(a, b): return a+b"}
        return ReplSandbox(
            repo_files=repo,
            role="code-reviewer",
            project_dir="/tmp/test-repo",
            mcp_config="",
        )

    def test_basic_execution(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print(len(repo_files))")
        assert "2" in result

    def test_repo_files_accessible(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print(list(repo_files.keys()))")
        assert "main.py" in result
        assert "utils.py" in result

    def test_print_captures_output(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print('hello world')")
        assert "hello world" in result

    def test_no_output_returns_marker(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("x = 42")
        assert result == "[no output]"

    def test_re_module_available(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print(re.findall(r'\\w+', 'hello world'))")
        assert "hello" in result

    def test_math_module_available(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print(math.sqrt(16))")
        assert "4" in result

    def test_json_module_available(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print(json.dumps({'a': 1}))")
        assert '"a"' in result

    def test_collections_available(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute(
            "c = Counter(['a','b','a'])\nprint(c['a'])"
        )
        assert "2" in result

    def test_blocked_os_import_runtime(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("import os")
        assert "SANDBOX VIOLATION" in result

    def test_blocked_subprocess_runtime(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("import subprocess")
        assert "SANDBOX VIOLATION" in result

    def test_blocked_exec_runtime(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("exec('print(1)')")
        assert "SANDBOX VIOLATION" in result

    def test_blocked_eval_runtime(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("eval('1+1')")
        assert "SANDBOX VIOLATION" in result

    def test_blocked_open_runtime(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("open('/etc/passwd')")
        assert "SANDBOX VIOLATION" in result

    def test_runtime_exception_handled(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("1/0")
        assert "EXECUTION ERROR" in result
        assert "ZeroDivisionError" in result

    def test_syntax_error_handled(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("def (broken")
        assert "SANDBOX VIOLATION" in result
        assert "SyntaxError" in result

    def test_sub_query_exposed(self, sandbox: ReplSandbox) -> None:
        with patch("equipa.rlm_decompose._run_sub_query", return_value="mocked result"):
            result = sandbox.execute(
                "r = sub_query('what does main.py do?', {'main.py': repo_files['main.py']})\n"
                "print(r)"
            )
        assert "mocked result" in result

    def test_sub_query_limit(self, sandbox: ReplSandbox) -> None:
        sandbox.sub_queries_run = MAX_SUB_QUERIES
        result = sandbox.sub_query("test", {"a.py": "x"})
        assert "limit reached" in result

    def test_sub_query_empty_prompt(self, sandbox: ReplSandbox) -> None:
        result = sandbox.sub_query("", {"a.py": "x"})
        assert "non-empty string" in result

    def test_sub_query_invalid_files(self, sandbox: ReplSandbox) -> None:
        result = sandbox.sub_query("test", "not a dict")  # type: ignore[arg-type]
        assert "dict[str, str]" in result

    def test_sub_query_increments_counter(self, sandbox: ReplSandbox) -> None:
        with patch("equipa.rlm_decompose._run_sub_query", return_value="ok"):
            sandbox.sub_query("test", {"a.py": "x"})
        assert sandbox.sub_queries_run == 1

    def test_restricted_import_allowed_module(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("import itertools\nprint(list(itertools.chain([1],[2])))")
        assert "1" in result
        assert "2" in result

    def test_restricted_import_blocked_module(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("import socket")
        assert "SANDBOX VIOLATION" in result

    def test_restricted_import_unlisted_module(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("import numpy")
        assert "SANDBOX VIOLATION" in result or "EXECUTION ERROR" in result

    def test_multiple_prints(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("print('line1')\nprint('line2')")
        assert "line1" in result
        assert "line2" in result

    def test_repo_file_filtering(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute(
            "py_files = {k: v for k, v in repo_files.items() if k.endswith('.py')}\n"
            "print(len(py_files))"
        )
        assert "2" in result

    def test_persistent_state_across_calls(self, sandbox: ReplSandbox) -> None:
        sandbox.execute("my_var = 42")
        result = sandbox.execute("print(my_var * 2)")
        assert "84" in result

    def test_persistent_state_accumulates(self, sandbox: ReplSandbox) -> None:
        sandbox.execute("results = []")
        sandbox.execute("results.append('first')")
        sandbox.execute("results.append('second')")
        result = sandbox.execute("print(results)")
        assert "first" in result
        assert "second" in result

    def test_blocked_nested_import_via_builtins(self, sandbox: ReplSandbox) -> None:
        result = sandbox.execute("__builtins__['__import__']('os')")
        assert "SANDBOX VIOLATION" in result or "EXECUTION ERROR" in result


# ---------------------------------------------------------------------------
# Sub-query runner
# ---------------------------------------------------------------------------

class TestRunSubQuery:
    @patch("equipa.rlm_decompose.subprocess.run")
    def test_successful_json_response(self, mock_run: MagicMock) -> None:
        from equipa.rlm_decompose import _run_sub_query
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "found 3 bugs"}',
            stderr="",
        )
        result = _run_sub_query("test", {"a.py": "x"}, "haiku", "/tmp", "")
        assert "found 3 bugs" in result

    @patch("equipa.rlm_decompose.subprocess.run")
    def test_non_json_response(self, mock_run: MagicMock) -> None:
        from equipa.rlm_decompose import _run_sub_query
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="plain text result",
            stderr="",
        )
        result = _run_sub_query("test", {"a.py": "x"}, "haiku", "/tmp", "")
        assert "plain text result" in result

    @patch("equipa.rlm_decompose.subprocess.run")
    def test_nonzero_exit(self, mock_run: MagicMock) -> None:
        from equipa.rlm_decompose import _run_sub_query
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error occurred",
        )
        result = _run_sub_query("test", {"a.py": "x"}, "haiku", "/tmp", "")
        assert "sub_query error" in result
        assert "exit code 1" in result

    @patch("equipa.rlm_decompose.subprocess.run")
    def test_timeout(self, mock_run: MagicMock) -> None:
        from equipa.rlm_decompose import _run_sub_query
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        result = _run_sub_query("test", {"a.py": "x"}, "haiku", "/tmp", "")
        assert "timed out" in result

    @patch("equipa.rlm_decompose.subprocess.run")
    def test_unexpected_exception(self, mock_run: MagicMock) -> None:
        from equipa.rlm_decompose import _run_sub_query
        mock_run.side_effect = OSError("file not found")
        result = _run_sub_query("test", {"a.py": "x"}, "haiku", "/tmp", "")
        assert "sub_query error" in result


# ---------------------------------------------------------------------------
# Repo file loading
# ---------------------------------------------------------------------------

class TestLoadRepoFiles:
    def test_loads_from_directory(self, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        (p / "main.py").write_text("print('hello')")
        (p / "utils.py").write_text("x = 1")
        files = load_repo_files(str(p))
        assert "main.py" in files
        assert "utils.py" in files

    def test_skips_node_modules(self, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        nm = p / "node_modules"
        nm.mkdir()
        (nm / "dep.js").write_text("module.exports = {}")
        (p / "main.py").write_text("x = 1")
        files = load_repo_files(str(p))
        assert "main.py" in files
        assert not any("node_modules" in k for k in files)

    def test_skips_git_dir(self, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        git = p / ".git"
        git.mkdir()
        (git / "config").write_text("[core]")
        (p / "main.py").write_text("x = 1")
        files = load_repo_files(str(p))
        assert not any(".git" in k for k in files)

    def test_skips_large_files(self, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        (p / "big.py").write_text("x" * 200_000)
        (p / "small.py").write_text("y = 1")
        files = load_repo_files(str(p), max_file_size=100_000)
        assert "small.py" in files
        assert "big.py" not in files

    def test_skips_non_code_extensions(self, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        (p / "image.png").write_bytes(b"\x89PNG")
        (p / "main.py").write_text("x = 1")
        files = load_repo_files(str(p))
        assert "main.py" in files
        assert "image.png" not in files

    def test_nonexistent_dir(self) -> None:
        files = load_repo_files("/nonexistent/path")
        assert files == {}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

class TestBuildDecomposeSystemPrompt:
    def test_contains_original_prompt(self) -> None:
        result = build_decompose_system_prompt("Review code.", "code-reviewer", "10 files")
        assert "Review code." in result

    def test_contains_repl_instructions(self) -> None:
        result = build_decompose_system_prompt("Review.", "code-reviewer", "10 files")
        assert "repo_files" in result
        assert "sub_query" in result
        assert "REPL" in result

    def test_contains_repo_summary(self) -> None:
        result = build_decompose_system_prompt("Review.", "code-reviewer", "50 files, 10K lines")
        assert "50 files, 10K lines" in result

    def test_model_for_code_reviewer(self) -> None:
        result = build_decompose_system_prompt("Review.", "code-reviewer", "summary")
        assert "sonnet" in result

    def test_model_for_integration_tester(self) -> None:
        result = build_decompose_system_prompt("Review.", "integration-tester", "summary")
        assert "haiku" in result


class TestBuildRepoSummary:
    def test_file_counts(self) -> None:
        files = {"a.py": "x = 1", "b.py": "y = 2", "c.js": "var z = 3"}
        summary = build_repo_summary(files)
        assert "Total files: 3" in summary

    def test_extension_breakdown(self) -> None:
        files = {"a.py": "x", "b.py": "y", "c.js": "z"}
        summary = build_repo_summary(files)
        assert ".py: 2" in summary
        assert ".js: 1" in summary

    def test_directory_breakdown(self) -> None:
        files = {"src/a.py": "x", "src/b.py": "y", "lib/c.py": "z"}
        summary = build_repo_summary(files)
        assert "src/: 2" in summary
        assert "lib/: 1" in summary


# ---------------------------------------------------------------------------
# DecomposeResult
# ---------------------------------------------------------------------------

class TestDecomposeResult:
    def test_success_no_errors(self) -> None:
        r = DecomposeResult("output", 3, 10, [])
        assert r.success
        assert r.sub_queries_run == 3
        assert r.files_examined == 10

    def test_failure_with_errors(self) -> None:
        r = DecomposeResult("output", 0, 0, ["something broke"])
        assert not r.success

    def test_repr(self) -> None:
        r = DecomposeResult("out", 2, 5, [])
        assert "success=True" in repr(r)
        assert "sub_queries=2" in repr(r)


# ---------------------------------------------------------------------------
# Orchestration: run_decompose_session
# ---------------------------------------------------------------------------

class TestExtractCodeBlocks:
    def test_single_python_block(self) -> None:
        text = "Here is code:\n```python\nprint('hi')\n```\nDone."
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert "print('hi')" in blocks[0]

    def test_multiple_blocks(self) -> None:
        text = "```python\nx = 1\n```\nThen:\n```python\ny = 2\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 2

    def test_no_blocks(self) -> None:
        blocks = _extract_code_blocks("Just text, no code.")
        assert blocks == []

    def test_empty_block_skipped(self) -> None:
        text = "```python\n\n```"
        blocks = _extract_code_blocks(text)
        assert blocks == []

    def test_untagged_code_block(self) -> None:
        text = "```\nprint('hi')\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1


class TestRunDecomposeSession:
    def test_empty_repo(self) -> None:
        result = run_decompose_session(
            system_prompt="Review.",
            project_dir="/nonexistent",
            role="code-reviewer",
            repo_files={},
        )
        assert not result.success
        assert "empty_repo" in result.errors

    @patch(
        "equipa.rlm_decompose._call_outer_agent",
        return_value="No code blocks — final review: looks good.",
    )
    def test_with_files(self, _mock_agent: MagicMock) -> None:
        files = {"main.py": "print('hello')", "utils.py": "def add(a,b): return a+b"}
        result = run_decompose_session(
            system_prompt="Review this code.",
            project_dir="/tmp/test",
            role="code-reviewer",
            repo_files=files,
        )
        assert result.success
        assert result.files_examined == 2
        assert "looks good" in result.output

    def test_multi_turn_with_code_blocks(self) -> None:
        call_count = 0

        def mock_agent(prompt: str, model: str, project_dir: str, timeout: int = 180) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "```python\npy = [k for k in repo_files if k.endswith('.py')]\nprint(f'Found {len(py)} Python files')\n```"
            return "Final review: 2 Python files analyzed, all clean."

        with patch("equipa.rlm_decompose._call_outer_agent", side_effect=mock_agent):
            files = {"main.py": "x = 1", "utils.py": "y = 2", "readme.md": "# hi"}
            result = run_decompose_session(
                system_prompt="Review.",
                project_dir="/tmp/test",
                role="code-reviewer",
                repo_files=files,
            )
        assert result.success
        assert result.files_examined == 3
        assert call_count == 2

    @patch(
        "equipa.rlm_decompose._call_outer_agent",
        return_value="All clear.",
    )
    def test_auto_loads_repo(self, _mock_agent: MagicMock, tmp_path: object) -> None:
        from pathlib import Path
        p = Path(str(tmp_path))
        (p / "main.py").write_text("x = 1")
        result = run_decompose_session(
            system_prompt="Review.",
            project_dir=str(p),
            role="code-reviewer",
        )
        assert result.success
        assert result.files_examined >= 1


# ---------------------------------------------------------------------------
# Integration: dispatch config flag check
# ---------------------------------------------------------------------------

class TestFeatureFlagIntegration:
    def test_flag_defaults_to_false(self) -> None:
        from equipa.dispatch import DEFAULT_FEATURE_FLAGS
        assert DEFAULT_FEATURE_FLAGS["rlm_decompose"] is False

    def test_constants_threshold(self) -> None:
        from equipa.constants import RLM_TOKEN_THRESHOLD
        assert RLM_TOKEN_THRESHOLD == 100_000

    def test_constants_max_sub_queries(self) -> None:
        from equipa.constants import RLM_MAX_SUB_QUERIES
        assert RLM_MAX_SUB_QUERIES == 20

    def test_constants_timeout(self) -> None:
        from equipa.constants import RLM_SUB_QUERY_TIMEOUT
        assert RLM_SUB_QUERY_TIMEOUT == 120
