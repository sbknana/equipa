#!/usr/bin/env python3
"""Task 3138 RR-04 / RR-05.

RR-04: the IR-02 refusal of a relative ``--db-path`` suggested the path it
resolves to from the orchestrator's cwd. From the operator's home on a real
host that named a stale copy of the database, so pasting it would silently
point every agent at it. The message now suggests no path and says where the
live database is configured.

RR-05: the argv backstop appended the isolation flags even after ``--``,
where the CLI reads them as prompt text, and counted flags there as present;
only the first ``--mcp-config`` value was checked, so a second value, the
``=`` spelling or inline JSON never reached the server check.

No network; no process is started.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from equipa.cli_isolation import (
    has_claude_cli_isolation,
    isolate_claude_argv,
    mcp_config_values,
)


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})


# --- RR-04 ------------------------------------------------------------------------

def _db_refusal(tmp_path: Path, db_args: list[str]) -> str:
    config = tmp_path / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"theforge": {
        "command": "/opt/fake/bin/uvx",
        "args": ["mcp-server-sqlite", *db_args]}}}), encoding="utf-8")
    with pytest.raises(agent_runner.AgentDispatchRefused) as refused:
        agent_runner._check_mcp_servers(config)
    return str(refused.value)


@pytest.mark.parametrize("db_args", [
    ["--db-path", "theforge.db"],
    ["--db-path=theforge.db"],
    ["--db-path", "./theforge.db"],
    ["--db-path", "~/theforge.db"],
])
def test_refusal_never_suggests_a_path_found_from_the_cwd(
        tmp_path, monkeypatch, db_args):
    """The review's case: the orchestrator runs from a directory holding a
    stale theforge.db; that path must not be offered as the fix."""
    stale_home = tmp_path / "home"
    stale_home.mkdir()
    (stale_home / "theforge.db").write_bytes(b"stale copy")
    monkeypatch.chdir(stale_home)
    monkeypatch.setenv("HOME", str(stale_home))

    message = _db_refusal(tmp_path, db_args)

    assert str(stale_home / "theforge.db") not in message
    assert str(stale_home) not in message


def test_refusal_says_where_the_live_database_is_configured(tmp_path):
    message = _db_refusal(tmp_path, ["--db-path", "theforge.db"])
    assert "absolute path of the live TheForge database" in message
    assert '"--db-path", "/absolute/path/to/theforge.db"' in message
    assert "equipa.constants.THEFORGE_DB" in message
    assert '"theforge_db"' in message and "forge_config.json" in message
    assert "THEFORGE_DB environment variable" in message
    assert "does not guess" in message
    assert str(tmp_path / "mcp_config.json") in message


# --- RR-05: position-aware flags ----------------------------------------------------

def test_flags_go_before_end_of_options():
    cmd = isolate_claude_argv(["claude", "-p", "--output-format", "json",
                               "--", "hi"])
    assert cmd[-2:] == ["--", "hi"]
    assert cmd.index("--setting-sources") < cmd.index("--")
    assert cmd.index("--strict-mcp-config") < cmd.index("--")
    assert has_claude_cli_isolation(cmd)


def test_flags_after_end_of_options_do_not_count():
    prompt_text = ["claude", "-p", "--", "--setting-sources", "user",
                   "--strict-mcp-config"]
    assert not has_claude_cli_isolation(prompt_text)
    isolated = isolate_claude_argv(prompt_text)
    assert isolated.index("--setting-sources") < isolated.index("--")
    assert has_claude_cli_isolation(isolated)


def test_prompt_text_naming_project_sources_is_not_a_widening():
    """After -- it is text; only an option may widen the sources."""
    cmd = isolate_claude_argv(["claude", "-p", "--", "--setting-sources",
                               "project"])
    assert has_claude_cli_isolation(cmd)
    with pytest.raises(ValueError):
        isolate_claude_argv(["claude", "--setting-sources", "project", "--",
                             "x"])


def test_argv_without_end_of_options_is_unchanged_in_shape():
    assert isolate_claude_argv(["claude", "-p", "x"]) == [
        "claude", "-p", "x", "--setting-sources", "",
        "--strict-mcp-config"]


@pytest.mark.parametrize("cmd, expected", [
    (["claude", "--mcp-config", "/a.json"], ["/a.json"]),
    (["claude", "--mcp-config", "/a.json", "/b.json", "-p", "x"],
     ["/a.json", "/b.json"]),
    (["claude", "--mcp-config=/a.json", "x"], ["/a.json"]),
    (["claude", "--mcp-config", "/a.json", "--mcp-config=/b.json"],
     ["/a.json", "/b.json"]),
    (["claude", "--mcp-config", '{"mcpServers": {}}'], ['{"mcpServers": {}}']),
    (["claude", "-p", "--", "--mcp-config", "/a.json"], []),
])
def test_every_mcp_config_value_is_found(cmd, expected):
    assert mcp_config_values(cmd) == expected


# --- RR-05: the spawn backstop checks every value --------------------------------

@pytest.fixture
def bad_config(tmp_path: Path) -> Path:
    """A config whose only server is a shell: refused wherever it appears."""
    config = tmp_path / "bad_mcp.json"
    config.write_text(json.dumps({"mcpServers": {"srv": {
        "command": "/bin/sh", "args": ["-c", "true"]}}}), encoding="utf-8")
    return config


@pytest.fixture
def good_config(tmp_path: Path) -> Path:
    config = tmp_path / "good_mcp.json"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return config


def _spawn(cmd: list[str], project: Path, monkeypatch) -> None:
    started: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        started.append(list(argv))
        raise AssertionError("the CLI was started")

    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec",
                        fake_exec)
    try:
        asyncio.run(agent_runner._spawn_agent_process(cmd, str(project)))
    finally:
        assert not started


@pytest.mark.parametrize("shape", ["second value", "equals spelling",
                                   "repeated flag", "inline JSON"])
def test_spawn_refuses_a_bad_server_in_any_mcp_config_value(
        tmp_path, monkeypatch, good_config, bad_config, shape):
    project = tmp_path / "project"
    project.mkdir()
    inline = json.dumps({"mcpServers": {"srv": {
        "command": "/bin/sh", "args": ["-c", "true"]}}})
    tail = {
        "second value": ["--mcp-config", str(good_config), str(bad_config)],
        "equals spelling": [f"--mcp-config={bad_config}"],
        "repeated flag": ["--mcp-config", str(good_config),
                          "--mcp-config", str(bad_config)],
        "inline JSON": ["--mcp-config", inline],
    }[shape]
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _spawn(["claude", "-p", "x", *tail], project, monkeypatch)


def test_spawn_refuses_a_relative_mcp_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(agent_runner.AgentDispatchRefused, match="relative"):
        _spawn(["claude", "-p", "x", "--mcp-config", "mcp.json"], project,
               monkeypatch)


def test_spawn_refuses_malformed_inline_json(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(agent_runner.AgentDispatchRefused, match="not valid JSON"):
        _spawn(["claude", "-p", "x", "--mcp-config", "{not json"], project,
               monkeypatch)


def test_managers_empty_inline_config_is_accepted():
    """The planner/evaluator pass {"mcpServers": {}} inline (manager.py)."""
    agent_runner._check_mcp_config_value('{"mcpServers": {}}', "/abs/project")


def test_absolute_good_config_is_accepted(good_config):
    agent_runner._check_mcp_config_value(str(good_config), "/abs/project")
    assert os.path.isabs(str(good_config))
