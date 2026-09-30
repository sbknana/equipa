"""Task 3136 (fix-forward of 3135): security review findings ISO-01..ISO-06.

Copyright 2026 Forgeborn

Each test fails on the 3135 branch as delivered:

* ISO-01: with agent_isolation on, preflight never runs project files as the
  orchestrator (behavioural fence over every spawn primitive, plus a
  source fence that fails when a new spawn in preflight.py lacks the check);
* ISO-02: every isolated unit gets its own empty HOME / CLAUDE_CONFIG_DIR and
  a pinned GIT_CONFIG_GLOBAL; nothing one unit plants reaches the next;
* ISO-03: the TheForge DB directory, the real -wal/-shm behind a symlink and
  the backup directories are in deny_read, and a traversable directory counts
  as readable;
* ISO-04: an export that turns a carried path into a symlink is refused;
* ISO-05: the verify script fails on readable secret files under the
  configured roots;
* ISO-06: the ForgeSmith GHOST/OPRO and SIMBA CLI spawns refuse with
  isolation on.
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from equipa import isolation, preflight

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- ISO-01: no orchestrator-run preflight on agent output ----------------------------


class _SpawnAttempted(Exception):
    """Raised by the spawn stand-ins; preflight swallows Exception, so the
    record list, not the exception, is what the tests assert on."""


def _project(tmp_path: Path, kind: str) -> Path:
    """A project directory whose build or install would run project code."""
    project = tmp_path / kind
    project.mkdir()
    files = {
        "node-tsc": {"package.json": "{}", "tsconfig.json": "{}"},
        "node-build": {"package.json": '{"scripts": {"build": "id"}}'},
        "go": {"go.mod": "module example.invalid/x\n"},
        "python": {"pyproject.toml": "[project]\nname='x'\n", "main.py": ""},
        "requirements": {"requirements.txt": "", "app.py": ""},
        "csharp": {"x.csproj": "<Project/>"},
    }[kind]
    for name, content in files.items():
        (project / name).write_text(content)
    return project


PROJECT_KINDS = ["node-tsc", "node-build", "go", "python", "requirements", "csharp"]


@pytest.fixture
def spawns(monkeypatch) -> list[tuple[str, object]]:
    """Record (and refuse) every way preflight could start a process."""
    calls: list[tuple[str, object]] = []

    def recorder(name: str):
        def record(*args, **kwargs):
            calls.append((name, kwargs.get("cwd", args[:1])))
            raise _SpawnAttempted(name)
        return record

    def async_recorder(name: str):
        async def record(*args, **kwargs):
            calls.append((name, kwargs.get("cwd")))
            raise _SpawnAttempted(name)
        return record

    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, async_recorder(name))
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, recorder(name))
    for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execve",
                 "execvp", "execvpe", "spawnv", "spawnve"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, recorder(name))
    return calls


def _flag(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: enabled)


@pytest.mark.parametrize("kind", PROJECT_KINDS)
def test_preflight_runs_no_project_code_with_isolation_on(
        tmp_path: Path, monkeypatch, spawns, kind: str) -> None:
    """ISO-01 fence: installs, build checks and auto-fix spawn nothing."""
    _flag(monkeypatch, True)
    project = str(_project(tmp_path, kind))

    async def no_autofix_agent(*args, **kwargs):
        raise AssertionError("auto-fix dispatched although its re-check "
                             "would be refused")

    monkeypatch.setattr(preflight, "_dispatch_autofix_agent", no_autofix_agent)

    asyncio.run(preflight.auto_install_dependencies(project))
    ok, _language, detail = asyncio.run(preflight.preflight_build_check(project))
    fixed, cost, summary = asyncio.run(preflight._handle_preflight_failure(
        {"id": 1}, project, {}, "node", "error", None))
    installed = asyncio.run(preflight._run_install_cmd(
        ["npm", "install"], project, "Node.js deps"))

    assert spawns == []
    assert ok is True and "agent_isolation" in detail
    assert (fixed, cost, summary) == (False, 0.0, "agent_isolation_refused")
    assert installed is False


@pytest.mark.parametrize("kind", ["node-tsc", "go", "csharp"])
def test_preflight_unchanged_with_isolation_off(
        tmp_path: Path, monkeypatch, spawns, kind: str) -> None:
    """Positive control for the fence: with the flag off the build check
    still runs the project's build command in the project directory."""
    _flag(monkeypatch, False)
    project = str(_project(tmp_path, kind))
    ok, _language, detail = asyncio.run(preflight.preflight_build_check(project))
    assert spawns == [("create_subprocess_exec", project)]
    assert ok is True and detail.startswith("Skipped: Preflight error")


def test_preflight_refuses_when_the_config_is_unreadable(
        tmp_path: Path, monkeypatch, spawns) -> None:
    """The flag is fail-closed: an unreadable dispatch config refuses too."""
    def broken():
        raise OSError("dispatch_config.json unreadable")

    monkeypatch.setattr(isolation, "get_active_dispatch_config", broken)
    project = str(_project(tmp_path, "node-build"))
    ok, _language, detail = asyncio.run(preflight.preflight_build_check(project))
    assert spawns == [] and ok and "agent_isolation" in detail


_SPAWN_CALLS = {
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("os", "system"), ("os", "posix_spawn"), ("os", "posix_spawnp"),
    ("os", "popen"), ("os", "execv"), ("os", "execve"), ("os", "execvp"),
}


def _spawn_sites(tree: ast.AST) -> list[tuple[ast.FunctionDef, ast.Call]]:
    sites = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in _SPAWN_CALLS):
                sites.append((function, node))
    return sites


def _refusal_lines(function: ast.AST) -> list[int]:
    return [node.lineno for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "worktree_execution_refusal"]


def test_every_preflight_spawn_is_preceded_by_the_isolation_refusal() -> None:
    """Source fence: a new spawn in preflight.py without the check fails."""
    source = (REPO_ROOT / "equipa" / "preflight.py").read_text()
    sites = _spawn_sites(ast.parse(source))
    assert len(sites) >= 2  # the install helper and the build check
    for function, call in sites:
        guards = [line for line in _refusal_lines(function) if line < call.lineno]
        assert guards, (f"{function.name} spawns a process at line "
                        f"{call.lineno} without isolation."
                        f"worktree_execution_refusal() first")


def test_source_fence_detects_an_unguarded_spawn() -> None:
    unguarded = ast.parse(
        "async def f(cwd):\n"
        "    await asyncio.create_subprocess_exec('npm', cwd=cwd)\n")
    (function, call), = _spawn_sites(unguarded)
    assert not [line for line in _refusal_lines(function) if line < call.lineno]
