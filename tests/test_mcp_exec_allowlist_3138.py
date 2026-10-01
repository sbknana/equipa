#!/usr/bin/env python3
"""Task 3138 RR-02: the MCP launch check is a true allowlist for executables.

SR3134-02 / RR-02: the 3134 check refused only the wrappers it listed, so
every other program counted as the server's own executable. These were
accepted: ld.so, setarch, prlimit, setpriv, systemd-run, xvfb-run, fakeroot,
eatmydata, numactl, ``sg -c``, pkexec, catchsegv, unbuffer, ``awk -f``, a
renamed copy of dash and a nonexistent path; for uvx (the launcher prod
uses) ``--from=.``, ``--with-editable=.`` and ``--from <project dir>``.

Now a plain executable must be uvx (with its install options checked) or be
listed under ``mcp_trusted_executables`` in dispatch_config.json, and a
command must exist. Wrappers and shells are refused even when listed, by
name, by resolved target and by the binary itself (a renamed shell copy).
Every refused row runs twice: unlisted, and listed as trusted.

No process is started: the check runs before the CLI is spawned.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner

PY = "/usr/bin/python3"


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """A project, and an install directory of fake programs outside it."""
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    installed = tmp_path / "installed" / "bin"
    for name in ("uvx", "mcp-server", "setarch", "prlimit", "setpriv",
                 "systemd-run", "xvfb-run", "fakeroot", "eatmydata", "numactl",
                 "sg", "su", "sudo", "doas", "nsenter", "unshare", "chroot",
                 "pkexec", "catchsegv", "unbuffer", "awk", "gawk",
                 "ld-linux-x86-64.so.2", "ld.so", "ld-musl-x86_64.so.1",
                 "bash", "dash", "zsh", "fish", "busybox"):
        _executable(installed / name)
    # A renamed copy of the real shell, and symlinks to it.
    real_shell = os.path.realpath("/bin/sh")
    shutil.copy2(real_shell, installed / "renamed-shell")
    (installed / "srv-link").symlink_to(real_shell)
    (installed / "bash-link").symlink_to("/bin/bash")
    (installed / "trusted-link").symlink_to(installed / "mcp-server")
    (installed / "notexec").write_text("data", encoding="utf-8")
    return {"project": project, "bin": installed, "tmp": tmp_path}


def _fill(value, layout: dict[str, Path]):
    if isinstance(value, str):
        return value.format(bin=layout["bin"], project=layout["project"])
    if isinstance(value, list):
        return [_fill(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, layout) for key, item in value.items()}
    return value


def _check(layout: dict[str, Path], server: dict) -> None:
    config = layout["tmp"] / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"srv": server}}),
                      encoding="utf-8")
    agent_runner._check_mcp_servers(config, (layout["project"],))


def _trust(monkeypatch, *paths: str, urls: list[str] | tuple = ()) -> None:
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: list(paths),
        # Since 3144 (RR3138-B) a uvx index URL must be listed.
        agent_runner.MCP_UVX_TRUSTED_URLS_KEY: list(urls)})


# Every reviewer case (SR3134-02, RR-02) and every program the task names.
REFUSED = {
    # --- programs that run the program in their arguments ---------------
    "ld-linux python -m": {"command": "{bin}/ld-linux-x86-64.so.2",
                           "args": [PY, "-m", "srv"]},
    "ld.so": {"command": "{bin}/ld.so", "args": [PY, "-m", "srv"]},
    "ld-musl": {"command": "{bin}/ld-musl-x86_64.so.1", "args": [PY, "/abs/s.py"]},
    "setarch": {"command": "{bin}/setarch",
                "args": ["x86_64", PY, "-m", "srv"]},
    "prlimit": {"command": "{bin}/prlimit",
                "args": ["--nofile=1024", PY, "-m", "srv"]},
    "setpriv": {"command": "{bin}/setpriv",
                "args": ["--reuid=1000", PY, "-m", "srv"]},
    "systemd-run": {"command": "{bin}/systemd-run",
                    "args": ["--user", "--scope", PY, "-m", "srv"]},
    "xvfb-run": {"command": "{bin}/xvfb-run", "args": [PY, "-m", "srv"]},
    "fakeroot": {"command": "{bin}/fakeroot", "args": [PY, "-m", "srv"]},
    "eatmydata": {"command": "{bin}/eatmydata", "args": [PY, "-m", "srv"]},
    "numactl -a": {"command": "{bin}/numactl", "args": ["-a", PY, "-m", "srv"]},
    "sg -c": {"command": "{bin}/sg", "args": ["user", "-c", "python3 -m srv"]},
    "su -c": {"command": "{bin}/su", "args": ["-c", "srv"]},
    "sudo": {"command": "{bin}/sudo", "args": ["/abs/srv"]},
    "doas": {"command": "{bin}/doas", "args": ["/abs/srv"]},
    "nsenter": {"command": "{bin}/nsenter", "args": ["-t", "1", "/abs/srv"]},
    "unshare": {"command": "{bin}/unshare", "args": ["-r", "/abs/srv"]},
    "chroot": {"command": "{bin}/chroot", "args": ["/", "/abs/srv"]},
    "pkexec": {"command": "{bin}/pkexec", "args": ["/abs/srv"]},
    "catchsegv": {"command": "{bin}/catchsegv", "args": ["/abs/srv"]},
    "unbuffer": {"command": "{bin}/unbuffer", "args": ["/abs/srv"]},
    "awk -f": {"command": "{bin}/awk", "args": ["-f", "srv.awk"]},
    "gawk -f": {"command": "{bin}/gawk", "args": ["-f", "/abs/srv.awk"]},
    # --- shells: by name, by resolved target, by the binary itself -------
    "bash": {"command": "{bin}/bash", "args": ["/abs/run.sh"]},
    "dash": {"command": "{bin}/dash", "args": ["/abs/run.sh"]},
    "zsh": {"command": "{bin}/zsh", "args": ["/abs/run.sh"]},
    "fish": {"command": "{bin}/fish", "args": ["/abs/run.sh"]},
    "busybox": {"command": "{bin}/busybox", "args": ["sh", "/abs/run.sh"]},
    "renamed copy of the shell": {"command": "{bin}/renamed-shell",
                                  "args": ["-c", "srv"]},
    "symlink to the shell": {"command": "{bin}/srv-link", "args": ["/abs/r.sh"]},
    "symlink to bash": {"command": "{bin}/bash-link", "args": ["/abs/r.sh"]},
    "/bin/sh itself": {"command": "/bin/sh", "args": ["/abs/r.sh"]},
    # --- nonexistent and unlisted ----------------------------------------
    "nonexistent path": {"command": "/opt/nonexistent3138/server", "args": []},
    "nonexistent uvx": {"command": "/opt/nonexistent3138/uvx",
                        "args": ["mcp-server-sqlite"]},
    "nonexistent python": {"command": "/opt/nonexistent3138/python3",
                           "args": ["-I", "/abs/s.py"]},
    "not executable": {"command": "{bin}/notexec", "args": []},
    # --- uvx installing local code ---------------------------------------
    "uvx --from=.": {"command": "{bin}/uvx", "args": ["--from=.", "srv"]},
    "uvx --from .": {"command": "{bin}/uvx", "args": ["--from", ".", "srv"]},
    "uvx --from ./pkg": {"command": "{bin}/uvx", "args": ["--from", "./pkg", "srv"]},
    "uvx --from pkg/sub": {"command": "{bin}/uvx",
                           "args": ["--from", "pkg/sub", "srv"]},
    "uvx --from project dir": {"command": "{bin}/uvx",
                               "args": ["--from", "{project}", "srv"]},
    "uvx --from=project dir": {"command": "{bin}/uvx",
                               "args": ["--from={project}/pkg", "srv"]},
    "uvx --from file URL": {"command": "{bin}/uvx",
                            "args": ["--from", "file://{project}/pkg", "srv"]},
    "uvx --from name @ path": {"command": "{bin}/uvx",
                               "args": ["--from", "srv @ ./pkg", "srv"]},
    "uvx --from name @ file URL": {"command": "{bin}/uvx", "args": [
        "--from", "srv @ file://{project}/pkg", "srv"]},
    "uvx --from wheel": {"command": "{bin}/uvx",
                         "args": ["--from", "srv-1.0-py3-none-any.whl", "srv"]},
    "uvx --with-editable=.": {"command": "{bin}/uvx",
                              "args": ["--with-editable=.", "srv"]},
    "uvx --with-editable absolute": {"command": "{bin}/uvx",
                                     "args": ["--with-editable", "/opt/x", "srv"]},
    "uvx --with ./pkg": {"command": "{bin}/uvx", "args": ["--with", "./pkg", "srv"]},
    "uvx --with-requirements": {"command": "{bin}/uvx",
                                "args": ["--with-requirements", "req.txt", "srv"]},
    "uvx --directory .": {"command": "{bin}/uvx",
                          "args": ["--directory", ".", "srv"]},
    "uvx --project=sub": {"command": "{bin}/uvx", "args": ["--project=sub", "srv"]},
    "uvx --python ./venv": {"command": "{bin}/uvx",
                            "args": ["--python", "./venv/bin/python", "srv"]},
    "uvx later --with-editable": {"command": "{bin}/uvx", "args": [
        "--from", "mcp-server-sqlite==0.6", "--with-editable", ".", "srv"]},
    # Short aliases, attached values and flag clusters (-w is --with).
    "uvx -w pkg/sub": {"command": "{bin}/uvx", "args": ["-w", "pkg/sub", "srv"]},
    "uvx -w./pkg": {"command": "{bin}/uvx", "args": ["-w./pkg", "srv"]},
    "uvx -w=pkg/sub": {"command": "{bin}/uvx", "args": ["-w=pkg/sub", "srv"]},
    "uvx -qw pkg/sub": {"command": "{bin}/uvx", "args": ["-qw", "pkg/sub", "srv"]},
    "uvx -w project dir": {"command": "{bin}/uvx",
                           "args": ["-w", "{project}/pkg", "srv"]},
    "uvx -c sub/c.txt": {"command": "{bin}/uvx",
                         "args": ["-c", "sub/c.txt", "srv"]},
    "uvx -fwheels/": {"command": "{bin}/uvx", "args": ["-fwheels/", "srv"]},
    "uvx -f project dir": {"command": "{bin}/uvx",
                           "args": ["-f", "{project}/wheels", "srv"]},
    # A package index can be a local directory.
    "uvx --index-url bare dir": {"command": "{bin}/uvx",
                                 "args": ["--index-url", "idx", "srv"]},
    "uvx --extra-index-url idx/simple": {"command": "{bin}/uvx", "args": [
        "--extra-index-url", "idx/simple", "srv"]},
    "uvx -i idx/simple": {"command": "{bin}/uvx",
                          "args": ["-i", "idx/simple", "srv"]},
    "uvx --index name=./idx": {"command": "{bin}/uvx",
                               "args": ["--index", "local=./idx", "srv"]},
    "uvx --default-index=file URL": {"command": "{bin}/uvx", "args": [
        "--default-index=file://{project}/idx", "srv"]},
    "uvx relative file URL": {"command": "{bin}/uvx",
                              "args": ["srv", "--config", "file:conf/srv"]},
    # --- path arguments hidden behind = or inside a project ---------------
    "--config=./relative": {"command": "{bin}/mcp-server",
                            "args": ["--config=./srv.toml"]},
    "--config=project file": {"command": "{bin}/mcp-server",
                              "args": ["--config={project}/srv.toml"]},
    "absolute arg in project": {"command": "{bin}/mcp-server",
                                "args": ["--data", "{project}/state"]},
    "node --opt=project": {"command": "/usr/bin/node",
                           "args": ["/abs/s.js", "--root={project}"]},
}


@pytest.mark.parametrize("server", REFUSED.values(), ids=REFUSED.keys())
def test_rr02_shape_is_refused(layout, server):
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _check(layout, _fill(server, layout))


@pytest.mark.parametrize("server", REFUSED.values(), ids=REFUSED.keys())
def test_rr02_shape_is_refused_even_when_listed_as_trusted(
        layout, server, monkeypatch):
    """The operator allowlist cannot re-admit a wrapper, shell or local
    install: the hard floor holds for listed executables too."""
    filled = _fill(server, layout)
    _trust(monkeypatch, filled["command"])
    with pytest.raises(agent_runner.AgentDispatchRefused, match="'srv'"):
        _check(layout, filled)


def test_unlisted_own_executable_is_refused(layout):
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="mcp_trusted_executables"):
        _check(layout, _fill({"command": "{bin}/mcp-server",
                              "args": ["--stdio"]}, layout))


def test_malformed_trust_list_trusts_nothing(layout, monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: str(
            layout["bin"] / "mcp-server")})
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="not an allowlisted"):
        _check(layout, _fill({"command": "{bin}/mcp-server"}, layout))


def test_relative_trust_entry_is_ignored(layout, monkeypatch):
    _trust(monkeypatch, "installed/bin/mcp-server")
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="not an allowlisted"):
        _check(layout, _fill({"command": "{bin}/mcp-server"}, layout))


ACCEPTED = {
    "uvx package, absolute db": {"command": "{bin}/uvx", "args": [
        "mcp-server-sqlite", "--db-path", "/abs/theforge.db"]},
    "uvx --from pinned version": {"command": "{bin}/uvx", "args": [
        "--from", "mcp-server-sqlite==0.6.2", "mcp-server-sqlite",
        "--db-path", "/abs/t.db"]},
    "uvx --from git URL": {"command": "{bin}/uvx", "args": [
        "--from", "git+https://git.example.invalid/srv.git", "srv"]},
    "uvx --from absolute outside projects": {"command": "{bin}/uvx", "args": [
        "--from", "/opt/servers/srv", "srv"]},
    "uvx --python version": {"command": "{bin}/uvx",
                             "args": ["--python", "3.12", "srv"]},
    "uvx tool option -p port": {"command": "{bin}/uvx",
                                "args": ["srv", "-p", "8080"]},
    "uvx tool option -b bind": {"command": "{bin}/uvx",
                                "args": ["srv", "-b", "0.0.0.0"]},
    "uvx tool option -f format": {"command": "{bin}/uvx",
                                  "args": ["srv", "-f", "json"]},
    "uvx -w package": {"command": "{bin}/uvx",
                       "args": ["-w", "httpx==0.27", "srv"]},
    "uvx -i remote index": {"command": "{bin}/uvx", "args": [
        "-i", "https://pypi.example.invalid/simple", "srv"],
        "trust_urls": ["https://pypi.example.invalid/simple"]},
    "uvx --index named remote": {"command": "{bin}/uvx", "args": [
        "--index", "internal=https://pypi.example.invalid/simple", "srv"],
        "trust_urls": ["https://pypi.example.invalid/simple"]},
    "uvx --index-url absolute outside projects": {"command": "{bin}/uvx",
                                                  "args": ["--index-url",
                                                           "/opt/wheelhouse",
                                                           "srv"]},
    # The --db-path value is the IR-02 check's: a data file, not code.
    "uvx --db-path in project": {"command": "{bin}/uvx", "args": [
        "mcp-server-sqlite", "--db-path", "{project}/theforge.db"]},
    "listed own executable": {"command": "{bin}/mcp-server",
                              "args": ["--stdio", "--log=/var/log/srv.log"],
                              "trust": ["{bin}/mcp-server"]},
    "listed through a symlink": {"command": "{bin}/trusted-link",
                                 "args": ["--stdio"],
                                 "trust": ["{bin}/mcp-server"]},
    "python -I absolute script": {"command": PY, "args": ["-I", "/abs/s.py"]},
    "node absolute script": {"command": "/usr/bin/node",
                             "args": ["/abs/s.js", "--port=8080"]},
}


@pytest.mark.parametrize("server", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_allowlisted_shape_is_accepted(layout, server, monkeypatch):
    filled = _fill(server, layout)
    if not os.path.exists(filled["command"]):
        pytest.fail(f"{filled['command']} must exist on the test host")
    _trust(monkeypatch, *filled.pop("trust", []),
           urls=filled.pop("trust_urls", []))
    _check(layout, filled)
