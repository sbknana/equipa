"""No orchestrator git call bypasses the hardened helper (task #3112, 3108 review).

Every git invocation in the ``equipa`` package must go through
``equipa.git_ops.git_run`` / ``git_run_async``: they add the gate-02/03
hardening (no replace refs, no hook / fsmonitor / pager / editor / driver
program, no diff drivers) and the pinned global config (MI-04). A raw
``subprocess.run(["git", ...])`` in the orchestrator — the worst was
``dispatch._commit_initiative_plan`` running ``git commit`` with the
repository's hooks — runs whatever an agent configured.

The scan parses every module and flags a process-spawning call whose
program is a literal ``git``. Only ``equipa/git_ops.py`` is exempt: it IS
the hardened helper, and its two literal calls read the operator's real
global config to build the pin.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "equipa"

# The hardened helper itself (see module docstring).
EXEMPT_MODULES = frozenset({"git_ops.py"})

# Functions that start a process with an argv (first positional argument).
_ARGV_SPAWNERS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput",
})
# Functions whose first positional argument is the program itself.
_PROGRAM_SPAWNERS = frozenset({
    "create_subprocess_exec", "execv", "execvp", "execvpe", "execl",
    "execlp", "spawnv", "spawnvp", "spawnl", "spawnlp", "posix_spawn",
    "posix_spawnp",
})
_SHELL_SPAWNERS = frozenset({"system", "popen", "create_subprocess_shell"})


def _is_git_program(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and Path(node.value.strip()).name in ("git", "git.exe")
    )


def _is_git_shell_string(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        first = node.value.strip().split(" ", 1)[0]
        return Path(first).name in ("git", "git.exe")
    if isinstance(node, ast.JoinedStr) and node.values:
        return _is_git_shell_string(node.values[0])
    return False


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def raw_git_calls(source: str, filename: str = "<source>") -> list[int]:
    """Line numbers of calls that start ``git`` without the hardened helper."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _call_name(node)
        first = node.args[0]
        if name in _ARGV_SPAWNERS:
            if isinstance(first, (ast.List, ast.Tuple)) and first.elts and _is_git_program(first.elts[0]):
                hits.append(node.lineno)
            elif _is_git_shell_string(first):
                hits.append(node.lineno)
        elif name in _PROGRAM_SPAWNERS:
            if _is_git_program(first):
                hits.append(node.lineno)
        elif name in _SHELL_SPAWNERS and _is_git_shell_string(first):
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize(
    "snippet",
    [
        'import subprocess\nsubprocess.run(["git", "commit", "-m", "x"], cwd=d)\n',
        'import subprocess\nsubprocess.check_output(("git", "status"))\n',
        'import subprocess\nsubprocess.Popen(["/usr/bin/git", "log"])\n',
        'import subprocess\nsubprocess.run("git add -A", shell=True)\n',
        'import subprocess\nsubprocess.run(f"git -C {d} status", shell=True)\n',
        'import asyncio\nasyncio.create_subprocess_exec("git", "diff")\n',
        'import os\nos.system("git push")\n',
        'import os\nos.execvp("git", ["git", "merge"])\n',
    ],
)
def test_scanner_flags_raw_git_calls(snippet: str) -> None:
    """Positive control: every spawning shape is caught."""
    assert raw_git_calls(snippet), f"scanner missed: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        'from equipa.git_ops import git_run\ngit_run(["commit", "-m", "x"], d)\n',
        'import subprocess\nsubprocess.run(["npm", "test"])\n',
        'TOOLS = frozenset({"git", "gh"})\n',
        'import subprocess\nsubprocess.run(["gh", "pr", "view"])\n',
    ],
)
def test_scanner_ignores_hardened_and_non_git_calls(snippet: str) -> None:
    assert raw_git_calls(snippet) == []


def test_package_has_no_raw_git_subprocess_calls() -> None:
    modules = sorted(PACKAGE_DIR.rglob("*.py"))
    assert len(modules) > 20, f"package scan found only {len(modules)} modules"
    offenders: list[str] = []
    for module in modules:
        if module.name in EXEMPT_MODULES and module.parent == PACKAGE_DIR:
            continue
        for line in raw_git_calls(module.read_text(encoding="utf-8"), str(module)):
            offenders.append(f"{module.relative_to(PACKAGE_DIR.parent)}:{line}")
    assert offenders == [], (
        "raw git subprocess calls bypass equipa.git_ops.git_run / git_run_async "
        "(hooks, fsmonitor and drivers an agent configured would run in the "
        f"orchestrator): {offenders}"
    )
