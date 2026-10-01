#!/usr/bin/env python3
"""Task 3138 RR-08: autoresearch's Claude CLI call is isolated like the rest.

``scripts/autoresearch_loop.py`` ran ``claude --print --model opus`` without
``--setting-sources user --strict-mcp-config`` (IR-01), so a project-scope
``.claude/settings.json``, ``CLAUDE.md`` or ``.mcp.json`` in its cwd would
load. Both the local and the SSH call now carry the flags. The CLI is never
started: subprocess.run is captured.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from equipa.cli_isolation import CLAUDE_CLI_ISOLATION_ARGS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autoresearch_loop.py"


@pytest.fixture
def autoresearch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("autoresearch_loop_3138",
                                                  SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _claude_words(shell_command: str) -> list[str]:
    """The claude argv inside ``[<config-dir prefix>]<claude argv> < "<file>"``.

    The SSH path starts with the per-run config directory prefix (task 3150,
    RR3144-A); its presence is asserted in test_cli_config_isolation_3150.
    """
    prefix = autoresearch_prefix()
    if prefix and shell_command.startswith(prefix):
        shell_command = shell_command[len(prefix):]
    return shlex.split(shell_command.split(" < ", 1)[0])


def autoresearch_prefix() -> str:
    from equipa.cli_isolation import REMOTE_RUN_CONFIG_DIR_PREFIX
    return REMOTE_RUN_CONFIG_DIR_PREFIX


def test_mutate_command_carries_the_isolation_flags(autoresearch):
    words = _claude_words(autoresearch.CLAUDE_MUTATE_COMMAND + ' < "/tmp/p"')
    assert words[:4] == ["claude", "--print", "--model", "opus"]
    assert words[4:] == list(CLAUDE_CLI_ISOLATION_ARGS)
    assert tuple(autoresearch.CLAUDE_CLI_ISOLATION_ARGS) == (
        "--setting-sources", "user", "--strict-mcp-config")


@pytest.mark.parametrize("local", [True, False], ids=["local", "ssh"])
def test_mutate_prompt_runs_claude_isolated(autoresearch, monkeypatch, local):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="NEW PROMPT",
                                           stderr="")

    monkeypatch.setattr(autoresearch, "is_on_claudinator", lambda: local)
    monkeypatch.setattr(autoresearch.subprocess, "run", fake_run)

    result = autoresearch.mutate_prompt(
        "developer", "old prompt", "no failures", {"success_rate": 50})

    assert result == "NEW PROMPT"
    # The local call runs the claude argv directly (no shell, task 3140); the
    # SSH call passes it as one shell command. Collect the claude words of each.
    claude_calls = []
    for argv in calls:
        if argv and argv[0] == "claude":
            claude_calls.append(list(argv))
        elif "claude" in argv[-1]:
            claude_calls.append(_claude_words(argv[-1]))
    assert len(claude_calls) == 1
    words = claude_calls[0]
    assert words[0] == "claude"
    assert "--strict-mcp-config" in words
    assert words[words.index("--setting-sources") + 1] == "user"
