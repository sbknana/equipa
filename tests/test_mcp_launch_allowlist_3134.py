#!/usr/bin/env python3
"""Task 3134 IR-02 / IR-03: MCP server launch allowlist and --db-path error.

IR-03: the 3127 check was a denylist, and the independent review got these
accepted: wrappers (timeout, nice, stdbuf, setsid, busybox), shells,
``uv run``, ``node -r`` / ``--import`` / ``--loader`` / ``-e``, poetry,
pipx, corepack, deno, go, make, docker compose, java, ruby, ipython3,
python3-dbg, ``NODE_OPTIONS`` in the server env and non-object server
entries. Every one is a row in REFUSED below, plus launches that live inside
a project directory. ACCEPTED holds the shapes the allowlist keeps.

IR-02: a relative ``--db-path`` refuses every dispatch; the error must name
the config file and the fix (an absolute path).

No network; the only processes started are the fake CLI in the last tests.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner

PY = "/usr/bin/python3"
NODE = "/usr/bin/node"


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """A project directory and a trusted install directory next to it."""
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / ".venv" / "bin").mkdir(parents=True)
    trusted = tmp_path / "opt" / "srv"
    (trusted / "bin").mkdir(parents=True)
    for exe in (project / "bin" / "server", project / ".venv" / "bin" / "python3",
                trusted / "bin" / "mcp-server", trusted / "bin" / "uvx"):
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (trusted / "notexec").write_text("data", encoding="utf-8")
    (trusted / "bin" / "link-into-project").symlink_to(project / "bin" / "server")
    (trusted / "bin" / "srv-python").symlink_to(sys.executable)
    return {"project": project, "trusted": trusted, "tmp": tmp_path}


def _fill(value, layout: dict[str, Path]):
    """Replace {project} / {trusted} placeholders in a server definition."""
    if isinstance(value, str):
        return value.format(project=layout["project"], trusted=layout["trusted"])
    if isinstance(value, list):
        return [_fill(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, layout) for key, item in value.items()}
    return value


def _check(layout: dict[str, Path], server) -> None:
    config = layout["tmp"] / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"srv": server}}),
                      encoding="utf-8")
    agent_runner._check_mcp_servers(config, (layout["project"],))


REFUSED = {
    # --- wrappers in front of an interpreter (only the outer binary was
    # classified before) ---
    "timeout": {"command": "/usr/bin/timeout",
                "args": ["600", "python3", "-m", "srv"]},
    "nice": {"command": "/usr/bin/nice", "args": ["python3", "-m", "srv"]},
    "stdbuf": {"command": "/usr/bin/stdbuf",
               "args": ["-oL", "python3", "-m", "srv"]},
    "setsid bash -c": {"command": "/usr/bin/setsid",
                       "args": ["bash", "-c", "python3 -m srv"]},
    "busybox sh -c": {"command": "/bin/busybox", "args": ["sh", "-c", "srv"]},
    "env": {"command": "/usr/bin/env", "args": ["python3", "-m", "srv"]},
    "sudo": {"command": "/usr/bin/sudo", "args": ["/abs/srv"]},
    # --- shells ---
    "fish -c": {"command": "/usr/bin/fish", "args": ["-c", "srv"]},
    "bash absolute script": {"command": "/bin/bash", "args": ["/abs/run.sh"]},
    "sh -o errexit": {"command": "/bin/sh", "args": ["-o", "errexit", "/abs/s.sh"]},
    # --- package and task runners, other runtimes ---
    "uv run": {"command": "/opt/fake/bin/uv", "args": ["run", "/abs/srv.py"]},
    "poetry run": {"command": "/opt/fake/bin/poetry", "args": ["run", "srv"]},
    "pdm run": {"command": "/opt/fake/bin/pdm", "args": ["run", "srv"]},
    "hatch run": {"command": "/opt/fake/bin/hatch", "args": ["run", "srv"]},
    "pipx run": {"command": "/opt/fake/bin/pipx", "args": ["run", "srv"]},
    "npx": {"command": "/usr/bin/npx", "args": ["-y", "srv"]},
    "corepack pnpm dlx": {"command": "/usr/bin/corepack",
                          "args": ["pnpm", "dlx", "srv"]},
    "deno run": {"command": "/opt/fake/bin/deno", "args": ["run", "/abs/s.ts"]},
    "go run": {"command": "/usr/local/go/bin/go", "args": ["run", "/abs/main.go"]},
    "make": {"command": "/usr/bin/make", "args": ["-C", "/abs", "serve"]},
    "docker compose run": {"command": "/usr/bin/docker",
                           "args": ["compose", "run", "srv"]},
    "java -cp": {"command": "/usr/bin/java", "args": ["-cp", "lib/*", "Srv"]},
    "ruby -Ilib": {"command": "/usr/bin/ruby3.1", "args": ["-Ilib", "/abs/s.rb"]},
    "ipython3 -m": {"command": "/usr/bin/ipython3", "args": ["-m", "srv"]},
    "tsx": {"command": "/opt/fake/bin/tsx", "args": ["/abs/s.ts"]},
    # --- python ---
    "python3-dbg -m": {"command": "/usr/bin/python3-dbg",
                       "args": ["-I", "-m", "srv"]},
    "python3-dbg no -I": {"command": "/usr/bin/python3-dbg",
                          "args": ["/abs/srv.py"]},
    "python without -I": {"command": PY, "args": ["/abs/srv.py"]},
    "python -P -m": {"command": PY, "args": ["-P", "-m", "srv"],
                     "cwd": "/abs/checkout"},
    "python -I -X pycache_prefix": {
        "command": PY, "args": ["-I", "-X", "pycache_prefix=cache", "/abs/s.py"]},
    "python -I -i": {"command": PY, "args": ["-I", "-i", "/abs/s.py"]},
    "python long option": {"command": PY, "args": [
        "-I", "--check-hash-based-pycs", "never", "/abs/s.py"]},
    "python -I -c": {"command": PY, "args": ["-I", "-c", "import srv"]},
    "python -I stdin": {"command": PY, "args": ["-I"]},
    "symlink named srv to python, no -I": {
        "command": "{trusted}/bin/srv-python", "args": ["-m", "srv"]},
    # --- node: preload, loader, eval, env file, debugger ---
    "node -r planted": {"command": NODE, "args": ["-r", "planted", "/abs/s.js"]},
    "node --require": {"command": NODE,
                       "args": ["--require=ts-node/register", "/abs/s.ts"]},
    "node --import tsx": {"command": NODE, "args": ["--import", "tsx", "/abs/s.ts"]},
    "node --loader": {"command": NODE, "args": ["--loader", "ts-node/esm", "/abs/s.ts"]},
    "node --experimental-loader": {
        "command": NODE, "args": ["--experimental-loader=x", "/abs/s.mjs"]},
    "node -e": {"command": NODE, "args": ["-e", "require('srv')"]},
    "node -p": {"command": NODE, "args": ["-p", "1"]},
    "node --env-file": {"command": NODE, "args": ["--env-file=.env", "/abs/s.js"]},
    "node --inspect": {"command": NODE, "args": ["--inspect=0.0.0.0:9229", "/abs/s.js"]},
    "node relative script": {"command": NODE, "args": ["dist/index.js"]},
    "node no script": {"command": NODE, "args": []},
    "nodejs -r": {"command": "/usr/bin/nodejs", "args": ["-r", "x", "/abs/s.js"]},
    # --- code-loading variables in the server env ---
    "env NODE_OPTIONS": {"command": NODE, "args": ["/abs/s.js"],
                         "env": {"NODE_OPTIONS": "-r planted"}},
    "env PYTHONPATH": {"command": PY, "args": ["-I", "/abs/s.py"],
                       "env": {"PYTHONPATH": "/abs/lib"}},
    "env PYTHONSTARTUP": {"command": PY, "args": ["-I", "/abs/s.py"],
                          "env": {"PYTHONSTARTUP": "/abs/x.py"}},
    "env LD_PRELOAD": {"command": "{trusted}/bin/mcp-server", "args": [],
                       "env": {"LD_PRELOAD": "/abs/evil.so"}},
    "env LD_LIBRARY_PATH": {"command": "{trusted}/bin/mcp-server", "args": [],
                            "env": {"LD_LIBRARY_PATH": "lib"}},
    "env BASH_ENV": {"command": "{trusted}/bin/mcp-server", "args": [],
                     "env": {"BASH_ENV": "/abs/rc"}},
    "env not an object": {"command": "{trusted}/bin/mcp-server", "args": [],
                          "env": ["NODE_OPTIONS=-r x"]},
    # --- inside the project directory ---
    "project .venv python": {"command": "{project}/.venv/bin/python3",
                             "args": ["-I", "/abs/s.py"]},
    "project executable": {"command": "{project}/bin/server", "args": []},
    "script in project": {"command": PY, "args": ["-I", "{project}/srv.py"]},
    "node script in project": {"command": NODE, "args": ["{project}/s.js"]},
    "-I -m cwd in project": {"command": PY, "args": ["-I", "-m", "srv"],
                             "cwd": "{project}"},
    "symlink into project": {"command": "{trusted}/bin/link-into-project",
                             "args": []},
    # --- other malformed launches ---
    "not executable": {"command": "{trusted}/notexec", "args": []},
    "relative command": {"command": "bin/server", "args": []},
    "relative config arg": {"command": "{trusted}/bin/mcp-server",
                            "args": ["--config", "config.toml"]},
}


@pytest.mark.parametrize("server", REFUSED.values(), ids=REFUSED.keys())
def test_bypass_is_refused(layout, server):
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _check(layout, _fill(server, layout))


ACCEPTED = {
    "python -I absolute script": {"command": PY, "args": ["-I", "/abs/s.py"]},
    "python -IBu absolute script": {"command": PY, "args": ["-IBu", "/abs/s.py"]},
    "python -I -W ignore script": {"command": PY,
                                   "args": ["-I", "-W", "ignore", "/abs/s.py"]},
    "python -I -m absolute cwd": {"command": PY, "args": ["-I", "-m", "srv"],
                                  "cwd": "/abs/checkout"},
    "symlink to python, -I script": {"command": "{trusted}/bin/srv-python",
                                     "args": ["-I", "/abs/s.py"]},
    "node absolute script": {"command": NODE, "args": ["/abs/s.js", "--port", "1"]},
    "node heap limit": {"command": NODE,
                        "args": ["--max-old-space-size=4096", "/abs/s.js"]},
    # An installed uvx: since task 3138 (RR-02) a missing command is refused.
    "uvx package": {"command": "{trusted}/bin/uvx",
                    "args": ["mcp-server-sqlite", "--db-path", "/abs/t.db"]},
    # The operator lists it under mcp_trusted_executables (task 3138, RR-02:
    # any other program is refused).
    "own executable": {"command": "{trusted}/bin/mcp-server",
                       "args": ["--stdio"], "env": {"LOG_LEVEL": "info"}},
    "python unbuffered env": {"command": PY, "args": ["-I", "/abs/s.py"],
                              "env": {"PYTHONUNBUFFERED": "1"}},
}


@pytest.mark.parametrize("server", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_allowlisted_launch_is_accepted(layout, server, monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: [
            str(layout["trusted"] / "bin" / "mcp-server")]})
    _check(layout, _fill(server, layout))


@pytest.mark.parametrize("entry", ["python3 -m srv", 3, None, ["/abs/srv"]])
def test_non_object_server_entry_is_refused(layout, entry):
    with pytest.raises(agent_runner.AgentDispatchRefused, match="JSON object"):
        _check(layout, entry)


def test_mcp_servers_must_be_an_object(tmp_path):
    config = tmp_path / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": ["x"]}), encoding="utf-8")
    with pytest.raises(agent_runner.AgentDispatchRefused, match="mcpServers"):
        agent_runner._check_mcp_servers(config)


def test_configured_project_dirs_count_as_project_directories(
        layout, monkeypatch):
    """A server under ANY registered project is refused, not just this one."""
    other = layout["tmp"] / "other-project"
    (other / "tools").mkdir(parents=True)
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {"other": str(other)})
    server = {"command": PY, "args": ["-I", str(other / "tools" / "srv.py")]}
    with pytest.raises(agent_runner.AgentDispatchRefused, match="inside the project"):
        _check(layout, server)


def test_remote_server_is_still_not_checked_as_a_process(layout):
    _check(layout, {"type": "http", "url": "https://mcp.example.invalid/mcp"})


# --- IR-02: an actionable --db-path refusal ----------------------------------


@pytest.mark.parametrize("db_args, suggestion", [
    (["--db-path", "theforge.db"], None),
    (["--db-path=theforge.db"], None),
    (["--db-path", "~/theforge.db"], os.path.expanduser("~/theforge.db")),
    (["--db-path"], "/absolute/path/to/theforge.db"),
])
def test_relative_db_path_error_names_the_file_and_the_fix(
        layout, db_args, suggestion):
    server = {"command": "/opt/fake/bin/uvx",
              "args": ["mcp-server-sqlite", *db_args]}
    with pytest.raises(agent_runner.AgentDispatchRefused) as refused:
        _check(layout, server)

    message = str(refused.value)
    config = str(layout["tmp"] / "mcp_config.json")
    assert message.count(config) >= 2  # where it is, and what to edit
    assert "relative --db-path" in message
    assert "absolute path" in message and "Fix:" in message
    # Task 3138 RR-04 replaced the suggestion: a path resolved from the
    # orchestrator's cwd (or home) named a stale database copy, so the
    # message now shows a placeholder and says where the live DB is set.
    assert "absolute path of the live TheForge database" in message
    guessed = suggestion or os.path.abspath("theforge.db")
    if guessed != "/absolute/path/to/theforge.db":
        assert guessed not in message
    assert '"--db-path", "/absolute/path/to/theforge.db"' in message


FAKE_CLI = '''import os
here = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(here, "cli_started"), "w").close()
'''


def test_dispatch_refusal_reports_the_fix(layout, monkeypatch):
    """What an operator sees: the refused result carries the whole fix."""
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    bin_dir = layout["tmp"] / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_cli.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    config = layout["tmp"] / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"theforge": {
        "command": "/opt/fake/bin/uvx",
        "args": ["mcp-server-sqlite", "--db-path", "theforge.db"]}}}),
        encoding="utf-8")
    cmd = [sys.executable, str(fake), "--add-dir", str(layout["project"]),
           "--mcp-config", str(config)]

    result = asyncio.run(agent_runner.run_agent(cmd, timeout=30, max_retries=1))

    assert result["success"] is False
    [error] = [e for e in result["errors"] if "dispatch refused" in e]
    assert str(config) in error and "absolute path" in error
    assert not (bin_dir / "cli_started").exists()


def test_shipped_example_uses_the_allowlisted_python_form():
    example = json.loads((Path(__file__).resolve().parents[1]
                          / "mcp_config.example.json").read_text(encoding="utf-8"))
    server = example["mcpServers"]["equipa"]
    assert server["args"][0] == "-I"
    assert os.path.isabs(server["args"][1])
    assert "PYTHONPATH" not in server.get("env", {})
