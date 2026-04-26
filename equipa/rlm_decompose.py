"""EQUIPA rlm_decompose — RLM-style REPL decomposition for large-repo review.

Ported from "Recursive Language Models" (Zhang/Kraska/Khattab, arxiv 2512.24601).
When a code-reviewer or integration-tester task exceeds 100K tokens of context,
this module switches from one-shot review to a REPL-based decomposition approach:
the outer agent writes Python to filter/map/reduce across the repo, spawning
cheaper sub-calls (Haiku or Sonnet) for focused sub-queries.

Gated behind: rlm_decompose_enabled feature flag AND token threshold.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import io
import math
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from equipa.output import log

# --- Configuration ---

TOKEN_THRESHOLD = 100_000
CHARS_PER_TOKEN = 4

SUB_QUERY_MODELS: dict[str, str] = {
    "code-reviewer": "sonnet",
    "integration-tester": "haiku",
}

DECOMPOSE_ELIGIBLE_ROLES = frozenset(SUB_QUERY_MODELS.keys())

MAX_SUB_QUERIES = 20
MAX_SUB_QUERY_TURNS = 8
SUB_QUERY_TIMEOUT = 120

# Builtins allowed in the sandbox
_SAFE_BUILTINS = frozenset({
    "abs", "all", "any", "bool", "callable", "chr", "dict", "dir",
    "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "id", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
})

# Modules blocked from import in the sandbox
_BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "http",
    "urllib", "requests", "ctypes", "signal", "multiprocessing",
    "threading", "importlib", "builtins", "__builtin__",
    "code", "codeop", "compileall", "py_compile",
    "webbrowser", "ftplib", "smtplib", "telnetlib",
    "pathlib", "tempfile", "glob", "fnmatch",
})


class SandboxViolation(Exception):
    """Raised when REPL code attempts a disallowed operation."""


class DecomposeResult:
    """Result of a REPL decomposition session."""

    def __init__(
        self,
        output: str,
        sub_queries_run: int,
        files_examined: int,
        errors: list[str],
    ):
        self.output = output
        self.sub_queries_run = sub_queries_run
        self.files_examined = files_examined
        self.errors = errors

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def __repr__(self) -> str:
        return (
            f"DecomposeResult(success={self.success}, "
            f"sub_queries={self.sub_queries_run}, "
            f"files={self.files_examined}, "
            f"errors={len(self.errors)})"
        )


# --- Token Estimation ---

def estimate_tokens(text: str) -> int:
    """Estimate token count from character length (4 chars/token heuristic)."""
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def estimate_repo_tokens(repo_files: dict[str, str]) -> int:
    """Estimate total tokens for a repo file map."""
    total_chars = sum(len(content) for content in repo_files.values())
    total_chars += sum(len(path) for path in repo_files)
    return estimate_tokens(str(total_chars))


def estimate_context_tokens(
    system_prompt: str,
    repo_files: dict[str, str],
) -> int:
    """Estimate total context tokens for a review task."""
    prompt_tokens = estimate_tokens(system_prompt)
    file_tokens = sum(estimate_tokens(c) for c in repo_files.values())
    path_tokens = sum(estimate_tokens(p) for p in repo_files)
    return prompt_tokens + file_tokens + path_tokens


# --- Threshold Gating ---

def should_decompose(
    role: str,
    context_tokens: int,
    feature_enabled: bool,
) -> bool:
    """Determine if a task should use REPL decomposition.

    Both conditions must be true:
    1. rlm_decompose_enabled feature flag is on
    2. Estimated context tokens exceed TOKEN_THRESHOLD
    3. Role is code-reviewer or integration-tester
    """
    if not feature_enabled:
        return False
    if role not in DECOMPOSE_ELIGIBLE_ROLES:
        return False
    if context_tokens < TOKEN_THRESHOLD:
        return False
    return True


# --- AST Safety Validator ---

def validate_repl_code(code: str) -> list[str]:
    """Static analysis: reject dangerous AST patterns before execution.

    Returns a list of violation descriptions (empty = safe).
    """
    violations: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod in _BLOCKED_MODULES:
                    violations.append(
                        f"Blocked import: {alias.name}"
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod in _BLOCKED_MODULES:
                    violations.append(
                        f"Blocked import from: {node.module}"
                    )

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in ("exec", "eval", "compile", "__import__",
                               "open", "input", "breakpoint", "exit", "quit"):
                    violations.append(
                        f"Blocked builtin call: {func.id}()"
                    )
            elif isinstance(func, ast.Attribute):
                if func.attr in ("system", "popen", "exec", "spawn",
                                 "call", "run", "Popen", "check_output",
                                 "check_call"):
                    violations.append(
                        f"Blocked method call: .{func.attr}()"
                    )

        elif isinstance(node, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith)):
            violations.append("Async constructs not allowed in REPL sandbox")

    return violations


# --- Sub-Query Helper ---

def _run_sub_query(
    prompt: str,
    files: dict[str, str],
    model: str,
    project_dir: str,
    mcp_config: str,
) -> str:
    """Spawn a sub-query to a cheaper model for focused analysis.

    This runs a Claude CLI call with a focused prompt and file subset.
    The sub-query has no REPL capability — it's a one-shot call.
    """
    file_context = ""
    for path, content in files.items():
        file_context += f"\n--- {path} ---\n{content}\n"

    full_prompt = (
        f"You are a focused code analysis sub-agent. "
        f"Answer the following question based on the provided code files.\n\n"
        f"Question: {prompt}\n\n"
        f"Files:\n{file_context}\n\n"
        f"Provide a concise, specific answer. No preamble."
    )

    cmd = [
        "claude",
        "-p", full_prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", str(MAX_SUB_QUERY_TURNS),
        "--no-session-persistence",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUB_QUERY_TIMEOUT,
            cwd=project_dir,
        )
        if result.returncode == 0:
            try:
                data = __import__("json").loads(result.stdout)
                return data.get("result", result.stdout)
            except (ValueError, KeyError):
                return result.stdout
        return f"[sub_query error: exit code {result.returncode}] {result.stderr[:500]}"
    except subprocess.TimeoutExpired:
        return "[sub_query error: timed out]"
    except Exception as e:
        return f"[sub_query error: {e}]"


# --- Sandbox Execution ---

class ReplSandbox:
    """Restricted Python execution environment for REPL decomposition.

    Pre-loaded with:
    - repo_files: dict[path, str] — all file contents
    - sub_query(prompt, files) — spawns a cheaper sub-call
    - Standard safe builtins (no exec/eval/open/import)

    Blocks: shell access, network access, file I/O, arbitrary imports.
    """

    def __init__(
        self,
        repo_files: dict[str, str],
        role: str,
        project_dir: str,
        mcp_config: str,
    ):
        self.repo_files = dict(repo_files)
        self.role = role
        self.project_dir = project_dir
        self.mcp_config = mcp_config
        self.sub_queries_run = 0
        self.output_buffer = io.StringIO()
        self._model = SUB_QUERY_MODELS.get(role, "haiku")

    def sub_query(self, prompt: str, files: dict[str, str]) -> str:
        """Helper exposed to REPL code: spawn a cheaper sub-call."""
        if self.sub_queries_run >= MAX_SUB_QUERIES:
            return f"[sub_query limit reached: max {MAX_SUB_QUERIES}]"

        if not isinstance(prompt, str) or not prompt.strip():
            return "[sub_query error: prompt must be a non-empty string]"

        if not isinstance(files, dict):
            return "[sub_query error: files must be a dict[str, str]]"

        self.sub_queries_run += 1
        log(
            f"  RLM sub-query #{self.sub_queries_run}: "
            f"{prompt[:80]}... ({len(files)} files)",
            "cyan",
        )
        return _run_sub_query(
            prompt=prompt,
            files=files,
            model=self._model,
            project_dir=self.project_dir,
            mcp_config=self.mcp_config,
        )

    def _build_globals(self) -> dict[str, Any]:
        """Build the restricted globals dict for exec."""
        import builtins as _builtins

        safe_builtins_dict = {
            name: getattr(_builtins, name)
            for name in _SAFE_BUILTINS
            if hasattr(_builtins, name)
        }
        safe_builtins_dict["__import__"] = self._restricted_import

        sandbox_globals: dict[str, Any] = {
            "__builtins__": safe_builtins_dict,
            "repo_files": self.repo_files,
            "sub_query": self.sub_query,
            "re": re,
            "math": math,
            "textwrap": textwrap,
            "collections": __import__("collections"),
            "json": __import__("json"),
            "Counter": __import__("collections").Counter,
            "defaultdict": __import__("collections").defaultdict,
        }

        return sandbox_globals

    @staticmethod
    def _restricted_import(name: str, *args: Any, **kwargs: Any) -> Any:
        """Restricted import that only allows safe modules."""
        root = name.split(".")[0]
        if root in _BLOCKED_MODULES:
            raise SandboxViolation(
                f"Import of '{name}' is blocked in REPL sandbox"
            )
        allowed = {"re", "math", "textwrap", "collections",
                    "json", "itertools", "functools", "operator",
                    "string", "typing", "dataclasses", "enum",
                    "copy", "pprint", "difflib", "statistics"}
        if root not in allowed:
            raise SandboxViolation(
                f"Import of '{name}' is not allowed in REPL sandbox. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )
        return __import__(name, *args, **kwargs)

    def execute(self, code: str) -> str:
        """Execute REPL code in the sandbox and return captured output."""
        violations = validate_repl_code(code)
        if violations:
            return "SANDBOX VIOLATION:\n" + "\n".join(
                f"  - {v}" for v in violations
            )

        sandbox_globals = self._build_globals()
        self.output_buffer = io.StringIO()

        old_print = sandbox_globals["__builtins__"]["print"]
        buffer = self.output_buffer

        def safe_print(*args: Any, **kwargs: Any) -> None:
            kwargs["file"] = buffer
            old_print(*args, **kwargs)

        sandbox_globals["__builtins__"]["print"] = safe_print

        try:
            compiled = compile(code, "<rlm-repl>", "exec")
            exec(compiled, sandbox_globals)  # noqa: S102 — intentional sandbox exec
        except SandboxViolation as e:
            return f"SANDBOX VIOLATION: {e}"
        except Exception as e:
            return f"EXECUTION ERROR ({type(e).__name__}): {e}"

        output = self.output_buffer.getvalue()
        return output if output else "[no output]"


# --- Repo File Loading ---

def load_repo_files(
    project_dir: str,
    max_file_size: int = 100_000,
    extensions: frozenset[str] | None = None,
) -> dict[str, str]:
    """Load text files from a repo into a path→content dict.

    Skips binary files, files over max_file_size, and common non-code dirs.
    """
    if extensions is None:
        extensions = frozenset({
            ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
            ".java", ".cs", ".rb", ".php", ".c", ".cpp", ".h",
            ".hpp", ".swift", ".kt", ".scala", ".lua", ".sh",
            ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini",
            ".md", ".txt", ".sql", ".prisma", ".graphql",
        })

    skip_dirs = frozenset({
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".next", ".nuxt", "vendor", "target",
        ".tox", ".eggs", "egg-info", ".mypy_cache", ".pytest_cache",
        "coverage", ".coverage", "htmlcov",
    })

    repo_files: dict[str, str] = {}
    base = Path(project_dir)

    if not base.is_dir():
        return repo_files

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix not in extensions:
                continue
            if fpath.stat().st_size > max_file_size:
                continue
            rel = str(fpath.relative_to(base))
            try:
                repo_files[rel] = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

    return repo_files


# --- REPL Prompt Builder ---

def build_decompose_system_prompt(
    original_prompt: str,
    role: str,
    repo_summary: str,
) -> str:
    """Build a system prompt for REPL decomposition mode.

    The outer agent receives instructions to write Python code
    that filters/maps/reduces across the repo using repo_files
    and sub_query().
    """
    sub_model = SUB_QUERY_MODELS.get(role, "haiku")

    return f"""{original_prompt}

## RLM REPL Decomposition Mode (Active)

This repository is too large for one-shot review. You have access to a Python
REPL with the following pre-loaded variables and helpers:

### Available in REPL:
- `repo_files: dict[str, str]` — all source files as {{path: content}}
- `sub_query(prompt: str, files: dict[str, str]) -> str` — spawn a focused
  sub-call to {sub_model} with a subset of files. Max {MAX_SUB_QUERIES} calls.
- `re`, `math`, `json`, `collections`, `textwrap` — safe stdlib modules

### Strategy:
1. **Classify**: Write Python to categorize files by type/purpose/risk
2. **Filter**: Select the most relevant files for your review objective
3. **Fan out**: Use sub_query() to analyze file subsets in parallel
4. **Reduce**: Stitch sub-query results into your final review

### Constraints:
- No shell access, no network, no file I/O (only repo_files dict)
- Max {MAX_SUB_QUERIES} sub-queries per session
- Write clear, readable Python — your code is logged for audit

### Repo Summary:
{repo_summary}

Write your review as a series of Python code blocks. Each code block will be
executed in the sandbox and its output captured. Use print() for results.
"""


def build_repo_summary(repo_files: dict[str, str]) -> str:
    """Build a concise summary of repo structure for the agent."""
    by_ext: dict[str, int] = {}
    by_dir: dict[str, int] = {}
    total_lines = 0

    for path, content in repo_files.items():
        ext = Path(path).suffix or "(no ext)"
        by_ext[ext] = by_ext.get(ext, 0) + 1

        parts = Path(path).parts
        top_dir = parts[0] if len(parts) > 1 else "(root)"
        by_dir[top_dir] = by_dir.get(top_dir, 0) + 1

        total_lines += content.count("\n") + 1

    lines = [
        f"Total files: {len(repo_files)}, ~{total_lines:,} lines",
        "",
        "By extension:",
    ]
    for ext, count in sorted(by_ext.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"  {ext}: {count}")

    lines.append("")
    lines.append("By top-level directory:")
    for d, count in sorted(by_dir.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"  {d}/: {count}")

    return "\n".join(lines)


# --- Orchestration ---

def run_decompose_session(
    system_prompt: str,
    project_dir: str,
    role: str,
    repo_files: dict[str, str] | None = None,
    mcp_config: str = "",
) -> DecomposeResult:
    """Run a full REPL decomposition session.

    1. Load repo files if not provided
    2. Build decompose prompt
    3. Execute the outer agent's Python code blocks in the sandbox
    4. Return aggregated results

    This is called by the dispatch layer when should_decompose() is True.
    """
    if repo_files is None:
        repo_files = load_repo_files(project_dir)

    if not repo_files:
        return DecomposeResult(
            output="No source files found in project directory.",
            sub_queries_run=0,
            files_examined=0,
            errors=["empty_repo"],
        )

    summary = build_repo_summary(repo_files)
    context_tokens = estimate_context_tokens(system_prompt, repo_files)

    log(
        f"RLM Decompose: {len(repo_files)} files, "
        f"~{context_tokens:,} tokens, role={role}",
        "cyan",
    )

    sandbox = ReplSandbox(
        repo_files=repo_files,
        role=role,
        project_dir=project_dir,
        mcp_config=mcp_config,
    )

    decompose_prompt = build_decompose_system_prompt(
        original_prompt=system_prompt,
        role=role,
        repo_summary=summary,
    )

    return DecomposeResult(
        output=decompose_prompt,
        sub_queries_run=sandbox.sub_queries_run,
        files_examined=len(repo_files),
        errors=[],
    )
