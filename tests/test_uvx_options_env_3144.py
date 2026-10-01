#!/usr/bin/env python3
"""Task 3144 RR3138-B: uvx's own options are read with uvx's option table.

The 3138 check judged a short uvx option by whether its value looked like a
path, because it scanned the tool's arguments too (``srv -f json``). The
independent review got these accepted: ``uvx -f wheels``, ``-fwheels``,
``-i idx``, ``-c cons.txt``, and ``UV_FIND_LINKS``, ``UV_PYTHON``,
``UV_INDEX_URL`` and ``UV_CONFIG_FILE`` in the server env. A bare name after
-f/-i/-c is a directory or file in the agent-writable project directory.

Now uvx's options are read up to the tool name. Each index, find-links,
constraint, requirement or directory value must be an absolute path outside
every project directory, or an https URL the operator listed under
``mcp_uvx_trusted_urls``. An option uvx's table does not list is refused,
and ``UV_*`` is refused in a server env.

No process is started: the check runs before the CLI is spawned.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner

LISTED_URL = "https://pypi.example.invalid/simple"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def layout(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A project, and an installed fake uvx the operator lists as trusted
    (so these rows test the options, not the binary check of RR3138-C)."""
    project = tmp_path / "project"
    project.mkdir()
    uvx = _executable(tmp_path / "installed" / "bin" / "uvx")
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: [str(uvx)],
        agent_runner.MCP_UVX_TRUSTED_URLS_KEY: [LISTED_URL, "http://plain.example.invalid/simple"],
    })
    return {"project": project, "uvx": uvx, "tmp": tmp_path}


def _check(layout: dict[str, Path], args: list[str],
           env: dict[str, str] | None = None) -> None:
    server = {"command": str(layout["uvx"]),
              "args": [arg.format(project=layout["project"]) for arg in args]}
    if env is not None:
        server["env"] = env
    config = layout["tmp"] / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"srv": server}}),
                      encoding="utf-8")
    agent_runner._check_mcp_servers(config, (layout["project"],))


REFUSED = {
    # The review's accepted rows: a bare name behind the short alias.
    "-f wheels": ["-f", "wheels", "srv"],
    "-fwheels": ["-fwheels", "srv"],
    "-f=wheels": ["-f=wheels", "srv"],
    "-qf wheels": ["-qf", "wheels", "srv"],
    "-i idx": ["-i", "idx", "srv"],
    "-iidx": ["-iidx", "srv"],
    "-i=idx": ["-i=idx", "srv"],
    "-c cons.txt": ["-c", "cons.txt", "srv"],
    "-c=cons.txt": ["-c=cons.txt", "srv"],
    "-b build.txt": ["-b", "build.txt", "srv"],
    # The long forms and their = forms.
    "--find-links wheels": ["--find-links", "wheels", "srv"],
    "--find-links=wheels": ["--find-links=wheels", "srv"],
    "--index idx": ["--index", "idx", "srv"],
    "--index=idx": ["--index=idx", "srv"],
    "--index name=idx": ["--index", "name=idx", "srv"],
    "--constraints cons.txt": ["--constraints", "cons.txt", "srv"],
    "--constraints=cons.txt": ["--constraints=cons.txt", "srv"],
    "--overrides over.txt": ["--overrides", "over.txt", "srv"],
    "--with-requirements req.txt": ["--with-requirements", "req.txt", "srv"],
    # https URLs the operator did not list, and other schemes.
    "-i unlisted https": ["-i", "https://evil.example.invalid/simple", "srv"],
    "--index-url unlisted https": ["--index-url",
                                   "https://evil.example.invalid/simple", "srv"],
    "--extra-index-url unlisted https": [
        "--extra-index-url", "https://evil.example.invalid/simple", "srv"],
    "-f unlisted https": ["-f", "https://evil.example.invalid/wheels", "srv"],
    "--default-index named unlisted": [
        "--default-index", "x=https://evil.example.invalid/simple", "srv"],
    "listed URL as a prefix": ["-i", LISTED_URL + ".evil.example.invalid", "srv"],
    "listed URL plus a path": ["-i", LISTED_URL + "/../other", "srv"],
    "http even when listed": ["-i", "http://plain.example.invalid/simple", "srv"],
    "-f file URL": ["-f", "file:///opt/wheels", "srv"],
    "-c https constraints unlisted": ["-c", "https://evil.example.invalid/c.txt",
                                      "srv"],
    # uv splits these values on whitespace: every word is judged.
    "-c absolute then bare": ["-c", "/opt/c.txt cons.txt", "srv"],
    "-f listed then bare": ["-f", f"{LISTED_URL} wheels", "srv"],
    # Absolute, but inside the project.
    "-f project dir": ["-f", "{project}/wheels", "srv"],
    "-c project file": ["-c={project}/c.txt", "srv"],
    # No value at all.
    "-f with no value": ["-f"],
    "-f= empty": ["-f=", "srv"],
    "--find-links= empty": ["--find-links=", "srv"],
    # Options uvx's table does not list: the tool name cannot be found.
    "alias --constraint": ["--constraint", "cons.txt", "srv"],
    "unknown long option": ["--bogus-option", "wheels", "srv"],
    "unknown short letter": ["-Z", "srv"],
    "flag given a value": ["--offline=wheels", "srv"],
}


@pytest.mark.parametrize("args", REFUSED.values(), ids=REFUSED.keys())
def test_uvx_option_shape_is_refused(layout, args):
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'.*uvx"):
        _check(layout, args)


ACCEPTED = {
    "prod shape": ["mcp-server-sqlite", "--db-path", "/abs/theforge.db"],
    "-f absolute": ["-f", "/opt/wheels", "srv"],
    "-f=absolute": ["-f=/opt/wheels", "srv"],
    "-c absolute": ["-c", "/opt/c.txt", "srv"],
    "-i listed https": ["-i", LISTED_URL, "srv"],
    "-i listed https with a trailing slash": ["-i", LISTED_URL + "/", "srv"],
    "--index named listed": ["--index", f"internal={LISTED_URL}", "srv"],
    "--index-url=listed": [f"--index-url={LISTED_URL}", "srv"],
    "-qf absolute": ["-qf", "/opt/wheels", "srv"],
    "flags before the tool": ["--offline", "--no-cache", "-q", "--isolated",
                              "srv"],
    "--python version": ["--python", "3.12", "srv"],
    "-p version": ["-p", "3.12", "srv"],
    "--from pinned package": ["--from", "mcp-server-sqlite==0.6.2",
                              "mcp-server-sqlite"],
    # After the tool name the arguments are the tool's own.
    "tool option -f format": ["srv", "-f", "json"],
    "tool option -c count": ["srv", "-c", "3"],
    "tool option -i interval": ["srv", "-i", "5"],
    "tool unknown option": ["srv", "--bogus-option", "x"],
    "-- then the tool": ["--", "srv", "-f", "json"],
}


@pytest.mark.parametrize("args", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_uvx_option_shape_is_accepted(layout, args):
    _check(layout, args)


@pytest.mark.parametrize("name", [
    "UV_FIND_LINKS", "UV_PYTHON", "UV_INDEX_URL", "UV_CONFIG_FILE",
    "UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_CONSTRAINT",
    "UV_PROJECT", "UV_WORKING_DIR",
])
def test_uv_env_is_refused_in_a_server_env(layout, name):
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match=f"'srv'.*sets {name}"):
        _check(layout, ["srv"], env={name: "wheels"})


def test_uv_env_is_refused_for_a_python_server_too(layout):
    """UV_* is refused in every server env: a python server can run uv."""
    config = layout["tmp"] / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"srv": {
        "command": "/usr/bin/python3", "args": ["-I", "/abs/srv.py"],
        "env": {"UV_PYTHON": "/tmp/py"}}}}), encoding="utf-8")
    with pytest.raises(agent_runner.AgentDispatchRefused, match="UV_PYTHON"):
        agent_runner._check_mcp_servers(config, (layout["project"],))


def test_other_server_env_is_still_accepted(layout):
    _check(layout, ["srv"], env={"EQUIPA_MCP_TOKEN": "FAKE-3144",
                                  "MY_UV_SETTING": "x"})


@pytest.mark.parametrize("entries", [
    "https://pypi.example.invalid/simple",  # not a list
    [123, None],
    ["https://pypi.example.invalid/simple https://x.example.invalid"],
])
def test_malformed_url_list_trusts_nothing(layout, monkeypatch, entries):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: [str(layout["uvx"])],
        agent_runner.MCP_UVX_TRUSTED_URLS_KEY: entries})
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="mcp_uvx_trusted_urls"):
        _check(layout, ["-i", LISTED_URL, "srv"])


def test_options_before_tool_parser_reads_uvx_shapes():
    parse = agent_runner._uvx_options_before_tool
    assert parse(["-qf", "/w", "srv", "-f", "x"]) == [
        ("-q", None), ("--find-links", "/w")]
    assert parse(["-fwheels", "srv"]) == [("--find-links", "wheels")]
    assert parse(["-f=", "srv"]) == [("--find-links", "")]
    assert parse(["--index=a=b", "srv"]) == [("--index", "a=b")]
    assert parse(["--", "-f", "x"]) == []
    assert parse(["-f"]) == [("--find-links", None)]
    with pytest.raises(ValueError):
        parse(["--constraint", "c.txt", "srv"])


def test_shipped_example_comment_describes_the_rules():
    """The example no longer claims short index/path options are refused
    only when they name a relative path."""
    example = json.loads((Path(__file__).resolve().parents[1]
                          / "mcp_config.example.json").read_text(encoding="utf-8"))
    comment = example["_comment_paths"]
    for needle in ("-f", "-i", "-c", "mcp_uvx_trusted_urls", "UV_*",
                   "absolute path"):
        assert needle in comment, needle
