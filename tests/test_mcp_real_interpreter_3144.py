#!/usr/bin/env python3
"""Task 3144 RR3138-C: a python, node or uvx command must be the real one.

The launch check picks its python/node/uvx rules by the command's name, and
those arms never consulted the operator allowlist. The independent review of
3138 got these accepted: ``/usr/bin/perl`` copied to ``<dir>/python3`` with
``-I <abs script>``, ``/usr/bin/env`` copied to ``<dir>/node`` with an
absolute script, and ``/usr/bin/env`` copied to ``<dir>/uvx`` running
``/bin/true``. Each one runs a program the python/node/uvx rules know
nothing about.

Now the file itself must be the program its name says: the same file as that
name found in the system command directories, the orchestrator's own
interpreter for python, or a path listed under ``mcp_trusted_executables``.
The orchestrator's PATH is not trusted for this.

No server is started: the check runs before the CLI is spawned.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from tests.system_node import install_system_node

PY = "/usr/bin/python3"
# Task 3169: the system node is one of the test's own (the system_node
# fixture); a clean CI runner has no /usr/bin/node.
NODE = "{node}"
PERL = shutil.which("perl") or "/usr/bin/env"  # perl-base is essential on Debian


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})


@pytest.fixture(autouse=True)
def system_node(tmp_path, monkeypatch) -> dict[str, str]:
    """Every test sees a real (system) node, so a renamed program named node
    is refused because it is not that node, on any host."""
    return install_system_node(monkeypatch, tmp_path / "system-bin")


def _copy(source: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.path.realpath(source), target)
    return target


def _check(tmp_path: Path, server: dict) -> None:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = tmp_path / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"srv": server}}),
                      encoding="utf-8")
    agent_runner._check_mcp_servers(config, (project,))


def _trust(monkeypatch, *paths: Path | str) -> None:
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: [str(p) for p in paths]})


def _renamed(tmp_path: Path) -> dict[str, dict]:
    """The review's rows, plus a shell script and a copy of python itself."""
    bin_dir = tmp_path / "opt" / "bin"
    script_uvx = bin_dir / "script" / "uvx"
    script_uvx.parent.mkdir(parents=True)
    script_uvx.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    script_uvx.chmod(0o755)
    return {
        "perl as python3": {"command": str(_copy(PERL, bin_dir / "perl" / "python3")),
                            "args": ["-I", "/abs/srv.py"]},
        "env as python3": {"command": str(_copy("/usr/bin/env", bin_dir / "env" / "python3")),
                           "args": ["-I", "/abs/srv.py"]},
        "env as node": {"command": str(_copy("/usr/bin/env", bin_dir / "env" / "node")),
                        "args": ["/abs/srv.js"]},
        "env as uvx": {"command": str(_copy("/usr/bin/env", bin_dir / "env" / "uvx")),
                       "args": ["/bin/true"]},
        "true as uvx": {"command": str(_copy("/bin/true", bin_dir / "true" / "uvx")),
                        "args": ["srv"]},
        "shell script as uvx": {"command": str(script_uvx), "args": ["srv"]},
        "copy of python as python3": {
            "command": str(_copy(sys.executable, bin_dir / "pycopy" / "python3")),
            "args": ["-I", "/abs/srv.py"]},
    }


ROWS = ["perl as python3", "env as python3", "env as node", "env as uvx",
        "true as uvx", "shell script as uvx", "copy of python as python3"]


@pytest.mark.parametrize("row", ROWS)
def test_renamed_program_is_refused(tmp_path, row):
    server = _renamed(tmp_path)[row]
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="'srv'.*not the .* installed in.*mcp_trusted_executables"):
        _check(tmp_path, server)


@pytest.mark.parametrize("row", ROWS)
def test_renamed_program_on_the_orchestrator_path_is_still_refused(
        tmp_path, row, monkeypatch):
    """The orchestrator's PATH (often holding the agent-writable
    ~/.local/bin) does not vouch for a binary."""
    server = _renamed(tmp_path)[row]
    monkeypatch.setenv("PATH", f"{Path(server['command']).parent}{os.pathsep}"
                               f"{os.environ.get('PATH', '')}")
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _check(tmp_path, server)


def test_listed_uvx_is_accepted(tmp_path, monkeypatch):
    """The operator lists a uvx outside the system directories, as prod's
    ~/.local/bin/uvx must be."""
    server = _renamed(tmp_path)["shell script as uvx"]
    _trust(monkeypatch, server["command"])
    _check(tmp_path, {**server, "args": ["mcp-server-sqlite", "--db-path",
                                         "/abs/theforge.db"]})


def test_listed_uvx_still_has_its_options_checked(tmp_path, monkeypatch):
    server = _renamed(tmp_path)["shell script as uvx"]
    _trust(monkeypatch, server["command"])
    with pytest.raises(agent_runner.AgentDispatchRefused, match="-f"):
        _check(tmp_path, {**server, "args": ["-f", "wheels", "srv"]})


ACCEPTED = {
    "system python3": {"command": PY, "args": ["-I", "/abs/srv.py"]},
    "orchestrator python": {"command": sys.executable,
                            "args": ["-I", "/abs/srv.py"]},
    "system node": {"command": NODE, "args": ["/abs/srv.js"]},
}


@pytest.mark.parametrize("server", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_real_interpreter_is_accepted(tmp_path, server, system_node):
    server = {**server,
              "command": server["command"].format(node=system_node["node"])}
    if not os.path.exists(server["command"]):
        pytest.fail(f"{server['command']} must exist on the test host")
    _check(tmp_path, server)


def test_symlink_to_the_system_python_is_accepted(tmp_path):
    link = tmp_path / "opt" / "bin" / "python3"
    link.parent.mkdir(parents=True)
    link.symlink_to(PY)
    _check(tmp_path, {"command": str(link), "args": ["-I", "/abs/srv.py"]})


def test_versioned_system_python_is_accepted(tmp_path):
    versioned = os.path.realpath(PY)  # /usr/bin/python3.12
    assert os.path.basename(versioned) != "python3"
    _check(tmp_path, {"command": versioned, "args": ["-I", "/abs/srv.py"]})


def test_symlink_to_a_renamed_copy_is_refused(tmp_path):
    copy = _copy("/usr/bin/env", tmp_path / "opt" / "real" / "python3")
    link = tmp_path / "opt" / "bin" / "python3"
    link.parent.mkdir(parents=True)
    link.symlink_to(copy)
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _check(tmp_path, {"command": str(link), "args": ["-I", "/abs/s.py"]})


def test_shipped_example_comment_names_the_rule_and_the_residual_risk():
    example = json.loads((Path(__file__).resolve().parents[1]
                          / "mcp_config.example.json").read_text(encoding="utf-8"))
    comment = example["_comment_paths"]
    for needle in ("RR3138-C", "~/.local/bin", "mcp_trusted_executables",
                   "Residual risk"):
        assert needle in comment, needle


def test_check_mcp_servers_docstring_describes_the_allowlist():
    """RR3138-G: the docstring described the generic arm as a denylist."""
    doc = " ".join((agent_runner._check_mcp_servers.__doc__ or "").split())
    assert "any other absolute executable file that is not a wrapper" not in doc
    for needle in ("allowlist", "mcp_trusted_executables", "uvx",
                   "real program", "UV_*"):
        assert needle in doc, needle


def test_is_real_interpreter_direct(system_node):
    assert agent_runner._is_real_interpreter(PY, "python")
    assert agent_runner._is_real_interpreter(sys.executable, "python")
    assert not agent_runner._is_real_interpreter("/usr/bin/env", "python")
    assert not agent_runner._is_real_interpreter(PY, "node")
    assert agent_runner._is_real_interpreter(system_node["node"], "node")
    assert agent_runner._is_real_interpreter(system_node["nodejs"], "node")
    assert not agent_runner._is_real_interpreter(system_node["node"], "python")
