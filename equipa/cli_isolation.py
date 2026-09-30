"""Keep project-scope Claude CLI configuration away from EQUIPA's CLI runs.

Copyright (c) 2026 Forgeborn

Agents (and the RLM ``claude -p`` helpers) run with ``cwd`` set to the
project directory, which agents can write (sandbox-11). By default the Claude
CLI loads project-scope configuration from its cwd:

* ``.claude/settings.json`` and ``.claude/settings.local.json``: one line of
  ``{"disableAllHooks": true}`` turns off the PreToolUse Bash gate for every
  later run in that project, and an ``env`` block injects variables into the
  agent's tools;
* ``CLAUDE.md``: loaded as standing instructions, so a developer agent could
  plant instructions for later tester and reviewer runs;
* ``.mcp.json``: adds MCP servers that start in the project directory.

``--setting-sources user`` loads user settings only (the ``--settings`` file
EQUIPA passes is a flag source and is always loaded), and
``--strict-mcp-config`` limits MCP servers to the ones EQUIPA passes with
``--mcp-config`` (none when it passes none). Independent review IR-01 (task
3127 fix-forward, task 3134) reproduced the gate bypass with the real CLI and
confirmed these two flags close it.

This module has no EQUIPA imports so standalone scripts (forgesmith) can use
it without pulling in the orchestrator.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

# The only setting source a Claude CLI run started by EQUIPA may load.
ALLOWED_SETTING_SOURCES = "user"
SETTING_SOURCES_FLAG = "--setting-sources"
STRICT_MCP_FLAG = "--strict-mcp-config"
CLAUDE_CLI_ISOLATION_ARGS: tuple[str, ...] = (
    SETTING_SOURCES_FLAG, ALLOWED_SETTING_SOURCES, STRICT_MCP_FLAG,
)


def is_claude_cli(executable: str) -> bool:
    """True when ``executable`` names the Claude Code CLI (any directory)."""
    name = os.path.basename(executable).lower()
    return name in ("claude", "claude.exe", "claude.cmd")


def _setting_sources_values(cmd: Sequence[str]) -> list[str]:
    """Every value given to ``--setting-sources`` (both spellings)."""
    values: list[str] = []
    for index, arg in enumerate(cmd):
        if arg == SETTING_SOURCES_FLAG:
            values.append(cmd[index + 1] if index + 1 < len(cmd) else "")
        elif arg.startswith(SETTING_SOURCES_FLAG + "="):
            values.append(arg.split("=", 1)[1])
    return values


def isolate_claude_argv(cmd: Sequence[str]) -> list[str]:
    """``cmd`` with project-scope settings, CLAUDE.md and .mcp.json disabled.

    Appends ``--setting-sources user`` and ``--strict-mcp-config`` when they
    are missing; an argv that already has them is returned unchanged (as a
    new list). The caller's own ``--mcp-config`` is kept: with the strict
    flag it is the only MCP configuration the CLI reads.

    Raises:
        ValueError: ``cmd`` already asks for a setting source other than
            ``user`` (for example ``project``), which would re-open the
            bypass. Refused rather than silently rewritten.
    """
    isolated = list(cmd)
    sources = _setting_sources_values(isolated)
    widened = [value for value in sources if value != ALLOWED_SETTING_SOURCES]
    if widened:
        raise ValueError(
            f"Claude CLI argv asks for setting sources {widened!r}; EQUIPA "
            f"runs may load only {ALLOWED_SETTING_SOURCES!r} because project "
            f"settings in the agent-writable cwd can disable the Bash gate"
        )
    if not sources:
        isolated.extend([SETTING_SOURCES_FLAG, ALLOWED_SETTING_SOURCES])
    if STRICT_MCP_FLAG not in isolated:
        isolated.append(STRICT_MCP_FLAG)
    return isolated


def has_claude_cli_isolation(cmd: Sequence[str]) -> bool:
    """True when ``cmd`` loads user settings only and strict MCP config."""
    sources = _setting_sources_values(cmd)
    return (bool(sources)
            and all(value == ALLOWED_SETTING_SOURCES for value in sources)
            and STRICT_MCP_FLAG in cmd)
