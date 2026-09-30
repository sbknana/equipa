"""Task 3135 (P2-ISO): agents run as a separate no-sudo user in their own cgroup.

Copyright 2026 Forgeborn

Unit tests for equipa.isolation (command construction and every refusal
path), the launcher's ``--isolated`` mode (inside checks, per-agent clone,
export) and the import back into the task worktree. Creating a second user,
a sudoers rule or a real cgroup is impossible from a test, so:

* the pieces that need them are exercised with stand-ins: fake
  ``systemd-run``/``sudo`` scripts that exec the rest of their argv, and a
  launcher wrapper that relaxes ONLY the three inside checks that need the
  real host (identity, cgroup, denied access). Everything else - handoff,
  handshake, clone, CLI, export, cgroup kill, import - is the real code;
* the real-host verification is scripts/verify_agent_isolation.sh, which the
  operator runs after setup. The tests below assert it exists, is executable
  and checks each required property; nothing is skipped.
"""

from __future__ import annotations

import asyncio
import grp
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from equipa import agent_launcher, isolation
from equipa.config import (
    CONFIG_LOAD_ERROR_KEY,
    DEFAULT_FEATURE_FLAGS,
    FAIL_CLOSED_FEATURE_FLAGS,
    is_feature_enabled,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"
GIT = shutil.which("git") or "/usr/bin/git"
UNIT = "equipa-agent-1-1-0123456789abcdef"


def _settings(tmp_path: Path, **overrides) -> isolation.IsolationSettings:
    exchange = tmp_path / "exchange"
    exchange.mkdir(exist_ok=True)
    section = {"exchange_dir": str(exchange), "git_executable": GIT,
               "python": sys.executable, **overrides}
    return isolation.load_isolation_settings({"agent_isolation": section})


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [GIT, "-c", "user.name=Orchestrator", "-c", "user.email=o@example.invalid",
         *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A main checkout on ``main`` and a linked task worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.name", "Forgeborn", cwd=main)
    _git("config", "user.email", "forgeborn@example.invalid", cwd=main)
    (main / ".gitignore").write_text(".equipa-artifacts/\n")
    (main / "README").write_text("base\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-q", "-m", "base", cwd=main)
    worktree = tmp_path / "worktrees" / "task-1"
    worktree.parent.mkdir()
    _git("worktree", "add", "-q", "-b", "forge-task-1", str(worktree), cwd=main)
    return {"main": main, "worktree": worktree}


# --- Flag ---------------------------------------------------------------------


def test_flag_defaults_off_and_is_fail_closed() -> None:
    assert DEFAULT_FEATURE_FLAGS["agent_isolation"] is False
    assert "agent_isolation" in FAIL_CLOSED_FEATURE_FLAGS
    assert is_feature_enabled({}, "agent_isolation") is False
    assert is_feature_enabled({"features": {"agent_isolation": "yes"}},
                              "agent_isolation") is True
    assert is_feature_enabled({CONFIG_LOAD_ERROR_KEY: "bad json"},
                              "agent_isolation") is True
    assert isolation.isolation_enabled(
        {"features": {"agent_isolation": True}}) is True
    assert isolation.isolation_enabled(
        {"features": {"agent_isolation": False}}) is False


# --- Settings -------------------------------------------------------------------


def test_settings_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.agent_user == "equipa-agent"
    assert settings.pids_max == 512
    assert settings.memory_max_bytes == 4 * 1024 ** 3
    assert settings.allowed_mcp_servers == ("theforge",)
    assert settings.exclude_tables == ("api_keys",)
    assert settings.launcher == str(agent_launcher.LAUNCHER_PATH)


@pytest.mark.parametrize("section, message", [
    ({}, "exchange_dir"),
    ({"exchange_dir": "relative/dir"}, "absolute"),
    ({"exchange_dir": "/x", "pid_max": 5}, "unknown"),
    ({"exchange_dir": "/x", "pids_max": True}, "pids_max"),
    ({"exchange_dir": "/x", "pids_max": 4}, "pids_max"),
    ({"exchange_dir": "/x", "memory_max": "10M"}, "memory_max"),
    ({"exchange_dir": "/x", "memory_max": "lots"}, "memory_max"),
    ({"exchange_dir": "/x", "agent_user": "root"}, "agent_user"),
    ({"exchange_dir": "/x", "agent_user": "Bad User"}, "agent_user"),
    ({"exchange_dir": "/x", "python": "/usr/bin/python3 -c"}, "python"),
    ({"exchange_dir": "/x", "launcher": "/opt/a,b.py"}, "launcher"),
    ({"exchange_dir": "/x", "exclude_tables": ["api_keys; DROP"]}, "exclude"),
    ({"exchange_dir": "/x", "carry_ignored_paths": ["../out"]}, "carry"),
    ({"exchange_dir": "/x", "deny_read": ["relative"]}, "absolute"),
])
def test_invalid_settings_refuse(section: dict, message: str) -> None:
    with pytest.raises(isolation.AgentIsolationError, match=message):
        isolation.load_isolation_settings({"agent_isolation": section})


def test_non_object_section_refuses() -> None:
    with pytest.raises(isolation.AgentIsolationError, match="JSON object"):
        isolation.load_isolation_settings({"agent_isolation": ["x"]})


def test_parse_memory_rounds_to_pages() -> None:
    assert isolation.parse_memory("4G") == 4 * 1024 ** 3
    assert isolation.parse_memory("512m") == 512 * 1024 ** 2
    assert isolation.parse_memory(100 * 1024 ** 2 + 5) == 100 * 1024 ** 2


# --- Command construction ----------------------------------------------------------


def test_launch_command_is_scope_plus_exact_sudo_rule(tmp_path: Path) -> None:
    settings = _settings(tmp_path, pids_max=300, memory_max="1G", cpu_weight=50)
    command = isolation.build_launch_command(settings, UNIT)
    assert command[:5] == [settings.systemd_run, "--user", "--scope",
                           "--quiet", "--collect"]
    assert f"--unit={UNIT}" in command
    assert "--property=TasksMax=300" in command
    assert f"--property=MemoryMax={1024 ** 3}" in command
    assert "--property=CPUWeight=50" in command
    sudo_at = command.index(settings.sudo)
    assert command[sudo_at:sudo_at + 5] == [settings.sudo, "-n", "-u",
                                            "equipa-agent", "--"]
    # What runs as the agent user is exactly the sudoers command.
    run_as_agent = " ".join(command[sudo_at + 5:])
    snippet = isolation.sudoers_snippet(settings, "orchestrator")
    assert f"Cmnd_Alias EQUIPA_AGENT_LAUNCH = {run_as_agent}\n" in snippet
    assert run_as_agent.endswith("agent_launcher.py --isolated")


def test_sudoers_snippet_allows_only_the_launcher(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    snippet = isolation.sudoers_snippet(settings, "orchestrator")
    rule = snippet.splitlines()[-1]
    assert rule == ("orchestrator ALL=(equipa-agent) NOPASSWD: "
                    "EQUIPA_AGENT_LAUNCH")
    alias = snippet.splitlines()[0]
    assert "*" not in alias and " ALL" not in alias
    assert "!pam_session" in snippet and "!use_pty" in snippet
    with pytest.raises(isolation.AgentIsolationError):
        isolation.sudoers_snippet(settings, "bad user")


def test_unit_name_matches_launcher_and_sweep_patterns() -> None:
    unit = isolation.make_unit_name()
    assert agent_launcher._UNIT_RE.match(unit)
    assert isolation._UNIT_NAME_RE.match(f"{unit}.scope")
    pid, start = unit.split("-")[2:4]
    assert int(pid) == os.getpid()
    assert int(start) == agent_launcher.proc_start_time(os.getpid())


def test_agent_env_keeps_only_the_oauth_token() -> None:
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/home/orch",
           "XDG_RUNTIME_DIR": "/run/user/1000", "CLAUDE_CONFIG_DIR": "/home/o",
           "GITHUB_TOKEN": "g", "ANTHROPIC_API_KEY": "a", "DATABASE_URL": "d",
           "PGPASSWORD": "p", "MY_SECRET_THING": "s",
           "CLAUDE_CODE_OAUTH_TOKEN": "stale"}
    agent_env = isolation.filter_agent_env(env, "fresh-token")
    assert agent_env == {"PATH": "/usr/bin", "LANG": "C.UTF-8",
                         "CLAUDE_CODE_OAUTH_TOKEN": "fresh-token"}


def test_launch_environment_is_minimal(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    env = isolation.launch_environment()
    assert set(env) <= {"PATH", "LANG", "XDG_RUNTIME_DIR",
                        "DBUS_SESSION_BUS_ADDRESS"}
    assert env["XDG_RUNTIME_DIR"] == "/run/user/4242"


# --- MCP config and hooks ---------------------------------------------------------


def _mcp_config(db_arg: list[str]) -> dict:
    return {"mcpServers": {
        "theforge": {"command": "/usr/bin/uvx",
                     "args": ["mcp-server-sqlite", *db_arg]},
        "equipa": {"command": "/usr/bin/python3", "args": ["-P", "-m", "x"],
                   "env": {"EQUIPA_MCP_TOKEN": "secret"}},
    }}


@pytest.mark.parametrize("db_arg", [["--db-path", "/srv/real.db"],
                                    ["--db-path=/srv/real.db"]])
def test_agent_mcp_config_points_forge_at_view_and_drops_others(
        tmp_path: Path, db_arg: list[str]) -> None:
    settings = _settings(tmp_path, view_db_path=str(tmp_path / "view.db"))
    needs = isolation._AccessNeeds()
    config, source = isolation.build_agent_mcp_config(
        _mcp_config(db_arg), settings, needs)
    assert source == "/srv/real.db"
    assert set(config["mcpServers"]) == {"theforge"}
    args = config["mcpServers"]["theforge"]["args"]
    assert str(tmp_path / "view.db") in " ".join(args)
    assert "/srv/real.db" not in json.dumps(config)
    assert "secret" not in json.dumps(config)
    assert "/usr/bin/uvx" in needs.execute


def test_allowlisted_mcp_server_with_credentials_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, view_db_path=str(tmp_path / "v.db"),
                         allowed_mcp_servers=["theforge", "equipa"])
    with pytest.raises(isolation.AgentIsolationError, match="EQUIPA_MCP_TOKEN"):
        isolation.build_agent_mcp_config(
            _mcp_config(["--db-path", "/x.db"]), settings,
            isolation._AccessNeeds())


def test_forge_server_without_view_path_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(isolation.AgentIsolationError, match="view_db_path"):
        isolation.build_agent_mcp_config(
            _mcp_config(["--db-path", "/x.db"]), settings,
            isolation._AccessNeeds())


def test_forge_server_without_db_path_refuses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, view_db_path=str(tmp_path / "v.db"))
    with pytest.raises(isolation.AgentIsolationError, match="--db-path"):
        isolation.build_agent_mcp_config(
            {"mcpServers": {"theforge": {"command": "/x", "args": []}}},
            settings, isolation._AccessNeeds())


def test_hook_commands_must_be_accessible_to_the_agent() -> None:
    needs = isolation._AccessNeeds()
    settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/opt/venv/bin/python /opt/eq/hook.py"}]}]}}
    isolation.hook_command_paths(json.dumps(settings), needs)
    assert needs.execute == {"/opt/venv/bin/python"}
    assert needs.read == {"/opt/eq/hook.py"}


# --- Refusal paths (orchestrator side) --------------------------------------------


class _Passwd:
    def __init__(self, uid: int, gid: int = 4242, home: str = "/home/agent"):
        self.pw_uid, self.pw_gid, self.pw_dir = uid, gid, home


def test_missing_agent_user_refuses(tmp_path: Path, monkeypatch) -> None:
    import pwd

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", missing)
    with pytest.raises(isolation.AgentIsolationError, match="does not exist"):
        isolation.resolve_agent_identity(_settings(tmp_path))


@pytest.mark.parametrize("uid, message", [(0, "is root"),
                                          (None, "orchestrator's own user")])
def test_agent_user_must_be_unprivileged_and_separate(
        tmp_path: Path, monkeypatch, uid, message) -> None:
    import pwd

    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: _Passwd(os.getuid() if uid is None else uid))
    with pytest.raises(isolation.AgentIsolationError, match=message):
        isolation.resolve_agent_identity(_settings(tmp_path))


def test_agent_user_in_privileged_group_refuses(tmp_path: Path,
                                                monkeypatch) -> None:
    import pwd

    group_name = grp.getgrgid(os.getgid()).gr_name
    monkeypatch.setattr(pwd, "getpwnam",
                        lambda name: _Passwd(os.getuid() + 1, gid=os.getgid()))
    settings = _settings(tmp_path, privileged_groups=[group_name])
    with pytest.raises(isolation.AgentIsolationError, match="privileged group"):
        isolation.resolve_agent_identity(settings)


def _host_ready(tmp_path: Path, monkeypatch) -> tuple:
    """A host state where check_host passes, for mutating one piece."""
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    (cgroup_root / "cgroup.controllers").write_text("cpu memory pids\n")
    runtime = tmp_path / "runtime"
    (runtime / "systemd").mkdir(parents=True)
    (runtime / "systemd" / "private").write_text("")
    monkeypatch.setattr(isolation, "CGROUP_ROOT", cgroup_root)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    settings = _settings(tmp_path, sudo=str(tool), systemd_run=str(tool),
                         claude_executable=str(tool), git_executable=str(tool))
    identity = isolation.AgentIdentity(uid=os.getuid(), gid=os.getgid(),
                                       home=str(tmp_path))
    return settings, identity


def test_check_host_passes_when_ready(tmp_path: Path, monkeypatch) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    isolation.check_host(settings, identity)


def test_check_host_refuses_without_cgroup_v2(tmp_path: Path, monkeypatch) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    (isolation.CGROUP_ROOT / "cgroup.controllers").unlink()
    with pytest.raises(isolation.AgentIsolationError, match="cgroup v2"):
        isolation.check_host(settings, identity)


def test_check_host_refuses_without_user_manager(tmp_path: Path,
                                                 monkeypatch) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(isolation.AgentIsolationError, match="enable-linger"):
        isolation.check_host(settings, identity)


def test_check_host_refuses_missing_sudo(tmp_path: Path, monkeypatch) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    settings = isolation.IsolationSettings(**{
        **settings.__dict__, "sudo": str(tmp_path / "no-sudo")})
    with pytest.raises(isolation.AgentIsolationError, match="sudo"):
        isolation.check_host(settings, identity)


def test_check_host_refuses_non_linux(tmp_path: Path, monkeypatch) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    with pytest.raises(isolation.AgentIsolationError, match="Linux-only"):
        isolation.check_host(settings, identity)


@pytest.mark.parametrize("problem", ["missing", "foreign_owner",
                                     "world_writable"])
def test_check_host_refuses_bad_exchange_dir(tmp_path: Path, monkeypatch,
                                             problem: str) -> None:
    settings, identity = _host_ready(tmp_path, monkeypatch)
    exchange = Path(settings.exchange_dir)
    if problem == "missing":
        exchange.rmdir()
    elif problem == "foreign_owner":
        identity = isolation.AgentIdentity(uid=os.getuid() + 1, gid=0,
                                           home="/x")
    else:
        exchange.chmod(0o777)
    with pytest.raises(isolation.AgentIsolationError, match="exchange directory"):
        isolation.check_host(settings, identity)


def test_oauth_token_required(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(isolation.AgentIsolationError, match="setup-token"):
        isolation.resolve_oauth_token(settings, environ={})
    assert isolation.resolve_oauth_token(
        settings, environ={"CLAUDE_CODE_OAUTH_TOKEN": " tok "}) == "tok"
    with pytest.raises(isolation.AgentIsolationError, match="invalid"):
        isolation.resolve_oauth_token(
            settings, environ={"CLAUDE_CODE_OAUTH_TOKEN": "a\nb"})


def test_oauth_token_file_must_be_private(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    token_file.chmod(0o644)
    settings = _settings(tmp_path, oauth_token_file=str(token_file))
    with pytest.raises(isolation.AgentIsolationError, match="chmod 600"):
        isolation.resolve_oauth_token(settings, environ={})
    token_file.chmod(0o600)
    assert isolation.resolve_oauth_token(settings, environ={}) == "file-token"


def test_worktree_description(repo: dict[str, Path]) -> None:
    info = isolation.describe_worktree(str(repo["worktree"]))
    assert info.branch_ref == "refs/heads/forge-task-1"
    assert info.common_dir == str((repo["main"] / ".git").resolve())
    assert info.main_root == str(repo["main"].resolve())
    assert info.base_sha == _git("rev-parse", "main", cwd=repo["main"])


def test_main_checkout_refuses(repo: dict[str, Path]) -> None:
    with pytest.raises(isolation.AgentIsolationError, match="main checkout"):
        isolation.describe_worktree(str(repo["main"]))


def test_worktree_subdirectory_refuses(repo: dict[str, Path]) -> None:
    sub = repo["worktree"] / "sub"
    sub.mkdir()
    with pytest.raises(isolation.AgentIsolationError, match="root of a git"):
        isolation.describe_worktree(str(sub))


def test_detached_worktree_refuses(repo: dict[str, Path]) -> None:
    _git("checkout", "-q", "--detach", cwd=repo["worktree"])
    with pytest.raises(isolation.AgentIsolationError, match="detached"):
        isolation.describe_worktree(str(repo["worktree"]))


def test_worktree_on_default_branch_refuses(repo: dict[str, Path]) -> None:
    """``worktree add --force`` can check out the main checkout's branch a
    second time; an import there would move the default branch."""
    other = repo["worktree"].parent / "on-main"
    _git("worktree", "add", "-q", "--force", str(other), "main", cwd=repo["main"])
    with pytest.raises(isolation.AgentIsolationError,
                       match="main checkout's branch refs/heads/main"):
        isolation.describe_worktree(str(other))


def test_view_db_drops_api_keys_and_their_rows(tmp_path: Path) -> None:
    source = tmp_path / "theforge.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY "
                     "AUTOINCREMENT, api_key TEXT)")
        conn.execute("INSERT INTO api_keys (api_key) VALUES "
                     "('sk-SUPERSECRET-0123456789')")
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO tasks (title) VALUES ('keep me')")
        conn.execute("CREATE VIEW key_view AS SELECT api_key FROM api_keys")
        conn.execute("CREATE TRIGGER t AFTER INSERT ON tasks BEGIN "
                     "INSERT INTO api_keys (api_key) VALUES ('x'); END")
    view_dir = tmp_path / "share"
    view_dir.mkdir()
    view = view_dir / "theforge-view.db"
    isolation.refresh_view_db(source, view, ["api_keys"])
    assert stat.S_IMODE(view.stat().st_mode) == 0o444
    assert b"SUPERSECRET" not in view.read_bytes()
    with sqlite3.connect(f"{view.as_uri()}?mode=ro", uri=True) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master")}
        assert conn.execute("SELECT title FROM tasks").fetchall() == [("keep me",)]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert "api_keys" not in names and "key_view" not in names and "t" not in names
    assert list(view_dir.iterdir()) == [view]
    with pytest.raises(isolation.AgentIsolationError, match="real database"):
        isolation.refresh_view_db(source, source, ["api_keys"])


def test_scope_verification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(isolation, "CGROUP_ROOT", tmp_path)
    settings = _settings(tmp_path, pids_max=64, memory_max="256M", cpu_weight=100)
    scope = tmp_path / "app.slice" / f"{UNIT}.scope"
    scope.mkdir(parents=True)
    (scope / "pids.max").write_text("64\n")
    (scope / "memory.max").write_text(f"{256 * 1024 ** 2}\n")
    (scope / "cpu.weight").write_text("100\n")
    cgroup = f"/app.slice/{UNIT}.scope"
    with pytest.raises(isolation.AgentIsolationError, match="cgroup.kill"):
        isolation.verify_scope_cgroup(cgroup, settings)
    (scope / "cgroup.kill").write_text("")
    isolation.verify_scope_cgroup(cgroup, settings)
    (scope / "pids.max").write_text("max\n")
    with pytest.raises(isolation.AgentIsolationError, match="pids.max"):
        isolation.verify_scope_cgroup(cgroup, settings)


def test_cgroup_kill_and_stale_scope_sweep(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(isolation, "CGROUP_ROOT", tmp_path)
    app_slice = tmp_path / "app.slice"
    dead_owner = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                                capture_output=True, text=True, check=True)
    dead_pid = int(dead_owner.stdout)
    stale = app_slice / f"equipa-agent-{dead_pid}-1-00ff00ff00ff00ff.scope"
    ours = app_slice / f"{isolation.make_unit_name(token='abcdefabcdefabcd')}.scope"
    for scope in (stale, ours):
        scope.mkdir(parents=True)
        (scope / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        (scope / "cgroup.kill").write_text("")
    killed = isolation.sweep_stale_scopes(app_slice)
    assert killed == [stale.name]
    assert (stale / "cgroup.kill").read_text() == "1"
    assert (ours / "cgroup.kill").read_text() == ""
    assert isolation.cgroup_populated(f"/app.slice/{ours.name}") is True
    assert isolation.kill_cgroup("/app.slice/gone.scope") is True
    assert isolation.cgroup_populated("/app.slice/gone.scope") is False


def test_handshake_parsing() -> None:
    ready = json.dumps({"type": "equipa_isolation", "status": "ready"}).encode()
    assert isolation.parse_handshake(ready + b"\n") == ("ready", "")
    assert isolation.parse_handshake(b'{"type": "result"}\n')[0] is None
    assert isolation.parse_handshake(b"garbage\n") == (None, "garbage")


# --- Launcher inside checks --------------------------------------------------------


def _header(tmp_path: Path, **overrides) -> dict:
    header = {
        "unit": UNIT, "argv": ["claude", "-p", "x"],
        "executable": sys.executable, "env": {"PATH": os.environ["PATH"]},
        "files": [], "workdir_sources": [],
        "identity": {"user": "equipa-agent", "orchestrator_uid": os.getuid(),
                     "privileged_groups": []},
        "cgroup": {"path": f"/app.slice/{UNIT}.scope", "pids_max": 64,
                   "memory_max": 256 * 1024 ** 2, "cpu_weight": 100},
        "deny_read": [], "deny_write": [], "must_execute": [], "must_read": [],
        "git": {"executable": GIT, "hardening_args": [], "hardening_env": {}},
        "workspace": None, "grace": 1.0,
    }
    header.update(overrides)
    return header


@pytest.mark.parametrize("override", [
    {"unit": "evil; rm -rf /"}, {"executable": "claude"}, {"argv": []},
    {"env": {"A": 1}}, {"files": [{"index": 0, "name": "x", "content": ""}]},
    {"files": [{"index": 1, "name": "../x", "content": ""}]},
    {"deny_read": ["relative"]},
    {"workspace": {"handoff_ref": "HEAD", "branch_ref": "refs/heads/x",
                   "base_sha": "0" * 40, "export_path": "/x", "carry_paths": []}},
    {"workspace": {"handoff_ref": "refs/x", "branch_ref": "refs/tags/x",
                   "base_sha": "0" * 40, "export_path": "/x", "carry_paths": []}},
])
def test_launcher_rejects_malformed_handoff(tmp_path: Path, override: dict) -> None:
    with pytest.raises(agent_launcher.IsolationRefused):
        agent_launcher._IsolatedSession(_header(tmp_path, **override))


def test_launcher_refuses_orchestrator_uid(tmp_path: Path) -> None:
    session = agent_launcher._IsolatedSession(_header(tmp_path))
    with pytest.raises(agent_launcher.IsolationRefused):
        session._verify_identity()


def test_launcher_refuses_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, identity={"user": "root", "orchestrator_uid": 1,
                                    "privileged_groups": []}))
    with pytest.raises(agent_launcher.IsolationRefused, match="uid 0"):
        session._verify_identity()


def test_launcher_refuses_wrong_user_and_privileged_group(
        tmp_path: Path) -> None:
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    other = {"user": "someone-else", "orchestrator_uid": os.getuid() + 1,
             "privileged_groups": []}
    session = agent_launcher._IsolatedSession(_header(tmp_path, identity=other))
    with pytest.raises(agent_launcher.IsolationRefused, match="expected"):
        session._verify_identity()
    group = grp.getgrgid(os.getgid()).gr_name
    privileged = {"user": me, "orchestrator_uid": os.getuid() + 1,
                  "privileged_groups": [group]}
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, identity=privileged))
    with pytest.raises(agent_launcher.IsolationRefused, match="privileged"):
        session._verify_identity()


def test_launcher_cgroup_checks(tmp_path: Path, monkeypatch) -> None:
    session = agent_launcher._IsolatedSession(_header(tmp_path))
    monkeypatch.setattr(agent_launcher, "_own_cgroup",
                        lambda: "/user.slice/session-1.scope")
    with pytest.raises(agent_launcher.IsolationRefused, match="expected"):
        session._verify_cgroup()
    monkeypatch.setattr(agent_launcher, "_own_cgroup",
                        lambda: f"/app.slice/{UNIT}.scope")
    values = {"pids.max": "max", "memory.max": str(256 * 1024 ** 2),
              "cpu.weight": "100"}
    monkeypatch.setattr(agent_launcher, "_read_cgroup_value",
                        lambda cgroup, name: values[name])
    with pytest.raises(agent_launcher.IsolationRefused, match="pids.max"):
        session._verify_cgroup()
    values["pids.max"] = "64"
    session._verify_cgroup()


def test_launcher_denied_and_required_access(tmp_path: Path) -> None:
    secret = tmp_path / "secret.db"
    secret.write_text("x")
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, deny_read=[str(secret)]))
    with pytest.raises(agent_launcher.IsolationRefused, match="can read"):
        session._verify_denied_access()
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, deny_write=[str(tmp_path)]))
    with pytest.raises(agent_launcher.IsolationRefused, match="can write"):
        session._verify_denied_access()
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, must_execute=[str(secret)]))
    with pytest.raises(agent_launcher.IsolationRefused, match="cannot execute"):
        session._verify_required_access()
    unreadable = tmp_path / "nope"
    session = agent_launcher._IsolatedSession(
        _header(tmp_path, must_read=[str(unreadable)]))
    with pytest.raises(agent_launcher.IsolationRefused, match="cannot read"):
        session._verify_required_access()


def _run_launcher(stdin: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(agent_launcher.LAUNCHER_PATH), "--isolated"],
        input=stdin, capture_output=True, timeout=60, check=False)


def _handoff_bytes(header: dict) -> bytes:
    body = json.dumps(header).encode()
    return isolation.encode_handoff_preamble(body, 0) + body


@pytest.mark.parametrize("stdin, reason", [
    (b"", "no handoff"),
    (b"HELLO 1 2 3\n", "malformed"),
    (b"EQUIPA-HANDOFF 1 99 0\n{}", "ended early"),
])
def test_launcher_process_refuses_bad_handoff(stdin: bytes, reason: str) -> None:
    result = _run_launcher(stdin)
    assert result.returncode == agent_launcher.EXIT_ISOLATION_REFUSED
    status, message = isolation.parse_handshake(result.stdout.splitlines()[0])
    assert status == "refused" and reason in message


def test_launcher_process_refuses_orchestrator_uid(tmp_path: Path) -> None:
    result = _run_launcher(_handoff_bytes(_header(tmp_path)))
    assert result.returncode == agent_launcher.EXIT_ISOLATION_REFUSED
    status, message = isolation.parse_handshake(result.stdout.splitlines()[0])
    assert status == "refused"
    assert "orchestrator's own user" in message or "uid 0" in message
    assert len(result.stdout.splitlines()) == 1  # nothing was started


def test_isolated_mode_needs_the_exact_argv() -> None:
    with pytest.raises(SystemExit):
        agent_launcher.main(["--isolated", "--", "/bin/sh"])


# --- Per-agent clone round trip (real git, no cgroup) ------------------------------


def _session_for(repo: dict[str, Path], tmp_path: Path,
                 settings: isolation.IsolationSettings):
    info = isolation.describe_worktree(str(repo["worktree"]))
    bundle = tmp_path / "handoff.bundle"
    handoff = isolation.build_handoff(
        ["claude", "-p", f"Work in: {repo['worktree']}", "--add-dir",
         str(repo["worktree"])], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok", bundle)
    handoff.header["cgroup"]["path"] = f"/app.slice/{UNIT}.scope"  # set by establish()
    session = agent_launcher._IsolatedSession(handoff.header)
    home = tmp_path / "agent-home"
    home.mkdir()
    session.home = str(home)
    with open(bundle, "rb") as source:
        session.receive_workspace(source.fileno(), bundle.stat().st_size)
    return info, handoff, session


def test_clone_round_trip_imports_only_the_task_branch(
        repo: dict[str, Path], tmp_path: Path) -> None:
    worktree, main = repo["worktree"], repo["main"]
    (worktree / "earlier.txt").write_text("uncommitted by an earlier attempt\n")
    main_before = _git("rev-parse", "main", cwd=main)
    settings = _settings(tmp_path)
    info, handoff, session = _session_for(repo, tmp_path, settings)
    clone = session.repo_dir
    # The clone is a separate repository with its own object store.
    assert (clone / ".git").is_dir()
    assert _git("rev-parse", "--git-common-dir", cwd=clone) == ".git"
    assert (clone / "earlier.txt").read_text().startswith("uncommitted")
    assert "?? earlier.txt" in _git("status", "--porcelain", cwd=clone)
    assert _git("config", "--local", "user.name", cwd=clone) == "Forgeborn"
    argv = session.materialize_argv()
    assert argv[2] == f"Work in: {clone}" and argv[4] == str(clone)
    # The agent works in its clone.
    (clone / "feature.py").write_text("print('agent')\n")
    _git("add", "feature.py", cwd=clone)
    _git("commit", "-q", "-m", "agent work", cwd=clone)
    (clone / "wip.txt").write_text("not committed\n")
    (clone / ".equipa-artifacts").mkdir()
    (clone / ".equipa-artifacts" / "SECURITY-REVIEW-1.md").write_text("ok\n")
    session.export()
    session.discard()
    assert not session.state_dir.exists()
    tip = isolation.import_agent_export(info, UNIT, handoff.export_path,
                                        settings.max_export_bytes)
    assert _git("log", "-1", "--format=%s", "forge-task-1", cwd=main) == "agent work"
    assert _git("rev-parse", "forge-task-1", cwd=main) == tip
    assert _git("rev-parse", "main", cwd=main) == main_before
    status = _git("status", "--porcelain", cwd=worktree)
    assert "?? wip.txt" in status and "?? earlier.txt" in status
    assert (worktree / "feature.py").is_file()
    assert (worktree / ".equipa-artifacts" / "SECURITY-REVIEW-1.md").is_file()
    assert _git("for-each-ref", "refs/equipa", cwd=main) == ""


def test_import_refuses_when_branch_moved_since_dispatch(
        repo: dict[str, Path], tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    info, handoff, session = _session_for(repo, tmp_path, settings)
    (session.repo_dir / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=session.repo_dir)
    _git("commit", "-q", "-m", "agent", cwd=session.repo_dir)
    session.export()
    (repo["worktree"] / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=repo["worktree"])
    _git("commit", "-q", "-m", "moved meanwhile", cwd=repo["worktree"])
    moved = _git("rev-parse", "HEAD", cwd=repo["worktree"])
    with pytest.raises(isolation.AgentIsolationError, match="update-ref"):
        isolation.import_agent_export(info, UNIT, handoff.export_path,
                                      settings.max_export_bytes)
    assert _git("rev-parse", "forge-task-1", cwd=repo["main"]) == moved


def test_import_refuses_export_without_state_ref(repo: dict[str, Path],
                                                 tmp_path: Path) -> None:
    info = isolation.describe_worktree(str(repo["worktree"]))
    other = tmp_path / "other.bundle"
    _git("bundle", "create", "-q", str(other), "main", cwd=repo["main"])
    with pytest.raises(isolation.AgentIsolationError, match="worktree-state"):
        isolation.import_agent_export(info, UNIT, str(other), 1024 ** 3)


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "oversize"])
def test_untrusted_export_copy_refusals(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "export.bundle"
    if kind == "symlink":
        (tmp_path / "secret").write_text("secret")
        source.symlink_to(tmp_path / "secret")
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "oversize":
        source.write_bytes(b"x" * 2048)
    with pytest.raises(isolation.AgentIsolationError):
        isolation.copy_untrusted_file(str(source), tmp_path / "copy", 1024)
    assert not (tmp_path / "copy").exists() or kind == "oversize"


# --- End to end through stand-ins for systemd-run, sudo and the second user ---------


def _write_script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


_EXEC_AFTER_DASHES = textwrap.dedent("""\
    #!/bin/sh
    # Stand-in for systemd-run / sudo: drop options, exec what follows --.
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
    shift
    exec "$@"
    """)


def _stand_ins(tmp_path: Path, monkeypatch, launcher: str, **overrides):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = _write_script(bin_dir / "exec-after-dashes", _EXEC_AFTER_DASHES)
    settings_section = {
        "exchange_dir": str(tmp_path / "exchange"), "git_executable": GIT,
        "python": sys.executable, "systemd_run": fake, "sudo": fake,
        "launcher": launcher, "stop_timeout_sec": 30, **overrides}
    (tmp_path / "exchange").mkdir(exist_ok=True)
    config = {"features": {"agent_isolation": True},
              "agent_isolation": settings_section}
    monkeypatch.setattr(isolation, "resolve_agent_identity",
                        lambda settings: isolation.AgentIdentity(
                            uid=os.getuid() + 1, gid=0, home=str(tmp_path)))
    monkeypatch.setattr(isolation, "check_host", lambda settings, ident: None)
    monkeypatch.setattr(isolation, "sweep_stale_scopes", lambda: [])
    monkeypatch.setattr(isolation, "make_unit_name", lambda: UNIT)
    monkeypatch.setattr(isolation, "read_proc_cgroup",
                        lambda pid: f"/fake/{UNIT}.scope")
    monkeypatch.setattr(isolation, "verify_scope_cgroup",
                        lambda cgroup, settings: None)
    monkeypatch.setattr(isolation, "CGROUP_ROOT", tmp_path / "no-cgroupfs")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    return config


def test_spawn_refused_by_the_real_launcher(tmp_path: Path, monkeypatch) -> None:
    """The real launcher, started through the stand-ins as the SAME user,
    refuses from the inside and the orchestrator reports it."""
    config = _stand_ins(tmp_path, monkeypatch, str(agent_launcher.LAUNCHER_PATH))
    cli = _write_script(tmp_path / "cli", "#!/bin/sh\necho started > started\n")
    config["agent_isolation"]["claude_executable"] = cli

    async def spawn():
        return await isolation.spawn_isolated_agent(
            ["claude", "-p", "x"], None, {"PATH": os.environ["PATH"]}, config)

    with pytest.raises(isolation.AgentIsolationError,
                       match="launcher refused to start the agent"):
        asyncio.run(spawn())
    assert not list(tmp_path.rglob("started"))
    assert not isolation._LIVE_ISOLATED_AGENTS


def test_spawn_refused_when_sudo_fails(tmp_path: Path, monkeypatch) -> None:
    config = _stand_ins(tmp_path, monkeypatch, str(agent_launcher.LAUNCHER_PATH))
    config["agent_isolation"]["systemd_run"] = _write_script(
        tmp_path / "bin" / "no-rule",
        "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
    monkeypatch.setattr(isolation, "read_proc_cgroup", lambda pid: None)
    config["agent_isolation"]["claude_executable"] = sys.executable

    async def spawn():
        return await isolation.spawn_isolated_agent(
            ["claude"], None, {}, config)

    with pytest.raises(isolation.AgentIsolationError,
                       match="password is required"):
        asyncio.run(spawn())


def test_spawn_refused_when_scope_never_appears(tmp_path: Path,
                                                monkeypatch) -> None:
    config = _stand_ins(tmp_path, monkeypatch, str(agent_launcher.LAUNCHER_PATH))
    config["agent_isolation"]["systemd_run"] = _write_script(
        tmp_path / "bin" / "hang", "#!/bin/sh\nexec sleep 30\n")
    config["agent_isolation"]["claude_executable"] = sys.executable
    monkeypatch.setattr(isolation, "read_proc_cgroup", lambda pid: "/elsewhere")
    monkeypatch.setattr(isolation, "_SCOPE_TIMEOUT_SECONDS", 0.3)

    async def spawn():
        return await isolation.spawn_isolated_agent(["claude"], None, {}, config)

    with pytest.raises(isolation.AgentIsolationError, match="did not appear"):
        asyncio.run(spawn())


_RELAXED_LAUNCHER = textwrap.dedent("""\
    # Test-only wrapper: the real launcher with the four inside checks that
    # need a second user, host scheduler config and a real cgroup relaxed;
    # everything else is real.
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("agent_launcher", {launcher!r})
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    session = launcher._IsolatedSession
    def identity(self):
        self.home = {home!r}
        self.shell = "/bin/sh"
    session._verify_identity = identity
    session._verify_cgroup = lambda self: None
    session._verify_denied_access = lambda self: None
    session._verify_no_scheduler = lambda self: None
    sys.exit(launcher._run_isolated())
    """)

_FAKE_CLI = textwrap.dedent("""\
    #!{python}
    import json, os, subprocess, sys, time
    # Work for a moment first: a stop request that arrives too early (for
    # example communicate() closing the stop channel) would kill us here.
    time.sleep(1.0)
    open("feature.txt", "w").write("from the isolated agent\\n")
    subprocess.run([{git!r}, "add", "feature.txt"], check=True)
    subprocess.run([{git!r}, "commit", "-q", "-m", "isolated agent"], check=True)
    print(json.dumps({{"type": "result", "cwd": os.getcwd(),
                      "home": os.environ["HOME"],
                      "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                      "leak": os.environ.get("GITHUB_TOKEN"),
                      "argv": sys.argv[1:]}}), flush=True)
    """)


@pytest.mark.parametrize("reader", ["streaming", "communicate"])
def test_isolated_agent_end_to_end(repo: dict[str, Path], tmp_path: Path,
                                   monkeypatch, reader: str) -> None:
    """Both agent_runner read styles: line streaming, and communicate(),
    which closes a visible stdin (the stop channel) at once."""
    home = tmp_path / "agent-home"
    home.mkdir()
    wrapper = tmp_path / "relaxed_launcher.py"
    wrapper.write_text(_RELAXED_LAUNCHER.format(
        launcher=str(agent_launcher.LAUNCHER_PATH), home=str(home)))
    config = _stand_ins(tmp_path, monkeypatch, str(wrapper))
    config["agent_isolation"]["claude_executable"] = _write_script(
        tmp_path / "fake-claude", _FAKE_CLI.format(python=sys.executable, git=GIT))
    worktree = repo["worktree"]
    main_before = _git("rev-parse", "main", cwd=repo["main"])

    async def run():
        process, agent = await isolation.spawn_isolated_agent(
            ["claude", "-p", f"Work in: {worktree}", "--add-dir", str(worktree)],
            str(worktree), {"PATH": os.environ["PATH"], "GITHUB_TOKEN": "leak"},
            config)
        assert process.stdin is None  # the stop channel is the handle's
        if reader == "communicate":
            output, _ = await asyncio.wait_for(process.communicate(), 60)
        else:
            output = await asyncio.wait_for(process.stdout.read(), 60)
        await agent.terminate()
        return output, agent

    output, agent = asyncio.run(run())
    result = json.loads(output.decode().splitlines()[0])
    clone = home / agent_launcher.AGENT_STATE_DIRNAME / UNIT / "repo"
    assert result["cwd"] == str(clone)
    # ISO-02 (task 3136): the unit's own HOME, not the shared passwd HOME.
    assert result["home"] == str(clone.parent / "home")
    assert result["home"] != str(home)
    assert result["token"] == "test-token" and result["leak"] is None
    assert result["argv"] == ["-p", f"Work in: {clone}", "--add-dir", str(clone)]
    assert agent.closed and agent.imported
    assert _git("log", "-1", "--format=%s", cwd=worktree) == "isolated agent"
    assert (worktree / "feature.txt").read_text() == "from the isolated agent\n"
    assert _git("rev-parse", "main", cwd=repo["main"]) == main_before
    assert not clone.exists()


# --- agent_runner and rlm_decompose wiring -----------------------------------------


def test_agent_runner_refuses_instead_of_falling_back(tmp_path: Path,
                                                      monkeypatch) -> None:
    from equipa import agent_runner

    monkeypatch.setattr(isolation, "isolation_enabled", lambda config=None: True)
    monkeypatch.setattr(isolation, "get_active_dispatch_config",
                        lambda: {"features": {"agent_isolation": True}})

    async def no_spawn(*args, **kwargs):
        raise AssertionError("an unisolated agent was spawned")

    monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec", no_spawn)
    with pytest.raises(agent_runner.AgentDispatchRefused,
                       match="agent isolation: .*exchange_dir"):
        asyncio.run(agent_runner._spawn_agent_process(
            ["claude", "-p", "x"], project_dir=str(tmp_path)))


def test_rlm_decompose_refuses_direct_cli_spawns(tmp_path: Path,
                                                 monkeypatch) -> None:
    """CT-04: RLM starts ``claude`` itself; with isolation on it refuses."""
    from equipa import rlm_decompose

    monkeypatch.setattr(isolation, "isolation_enabled", lambda config=None: True)

    def no_run(*args, **kwargs):
        raise AssertionError("the CLI was started outside the sandbox")

    monkeypatch.setattr(rlm_decompose.subprocess, "run", no_run)
    outer = rlm_decompose._call_outer_agent("prompt", "model", str(tmp_path))
    sub = rlm_decompose._run_sub_query("q", {}, "model", str(tmp_path), "/x.json")
    assert outer.startswith("[agent error: RLM decomposition refused")
    assert sub.startswith("[sub_query error: RLM sub-query refused")
    assert "agent_isolation" in outer and "agent_isolation" in sub


def test_rlm_decompose_unchanged_when_isolation_is_off(tmp_path: Path,
                                                       monkeypatch) -> None:
    from equipa import rlm_decompose

    monkeypatch.setattr(isolation, "isolation_enabled", lambda config=None: False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "answer", "")

    monkeypatch.setattr(rlm_decompose.subprocess, "run", fake_run)
    assert rlm_decompose._call_outer_agent("p", "model", str(tmp_path)) == "answer"
    assert calls and calls[0][0] == "claude"


# --- The operator verification script -----------------------------------------------


def test_verify_script_exists_and_is_executable() -> None:
    assert VERIFY_SCRIPT.is_file()
    assert os.access(VERIFY_SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(VERIFY_SCRIPT)],
                          check=False).returncode == 0


@pytest.mark.parametrize("item, markers", [
    ("cannot read the DB", ["--db)", 'head -c 1 -- "$path"',
                            "agent cannot read"]),
    ("cannot sudo", ["sudo -n true", "agent user cannot sudo"]),
    ("cannot write .git", ['"$git_dir/equipa-isolation-probe', "cannot write $git_dir"]),
    ("own cgroup", ["/proc/self/cgroup", "*/equipa-agent-*.scope"]),
    ("pids.max", ["pids.max", '"$pids_max" != "max"']),
    ("cannot leave the cgroup", ["-name cgroup.procs -writable"]),
    ("cannot signal the orchestrator", ['kill -0 "$orchestrator_pid"']),
    ("no api_keys in the view", ["--exclude-table)", "sqlite_master"]),
    ("only the OAuth token", ["CLAUDE_CODE_OAUTH_TOKEN)", "*TOKEN*"]),
    ("real launch path", ["-m equipa.isolation --verify-probe"]),
])
def test_verify_script_checks_each_item(item: str, markers: list[str]) -> None:
    text = VERIFY_SCRIPT.read_text()
    for marker in markers:
        assert marker in text, f"{item}: {marker!r} missing"


def test_probe_command_options_are_all_handled_by_the_script(
        tmp_path: Path) -> None:
    settings = _settings(tmp_path, view_db_path=str(tmp_path / "v.db"))
    command = isolation.build_probe_command(str(VERIFY_SCRIPT), settings, [],
                                            str(tmp_path))
    text = VERIFY_SCRIPT.read_text()
    options = {arg for arg in command[1:] if arg.startswith("--")}
    assert {"--inside", "--db", "--git-dir", "--pids-max",
            "--orchestrator-pid", "--view-db", "--exclude-table"} <= options
    for option in options - {"--inside"}:
        assert f"{option})" in text, option


def test_verify_script_reports_failures_for_an_unisolated_user(
        tmp_path: Path) -> None:
    """Run the inside checks as the current (unisolated) user: the readable
    DB, the writable .git and a sudo that works must all be reported."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_script(fake_bin / "sudo", "#!/bin/sh\nexit 0\n")
    database = tmp_path / "theforge.db"
    database.write_text("x")
    git_dir = tmp_path / "repo.git"
    git_dir.mkdir()
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(VERIFY_SCRIPT), "--inside", "--db", str(database),
         "--git-dir", str(git_dir), "--orchestrator-pid", str(os.getpid()),
         "--pids-max", "64"],
        capture_output=True, text=True, env=env, timeout=120, check=False)
    lines = result.stdout.splitlines()
    assert f"FAIL agent can read {database}" in lines
    assert f"FAIL agent can write {git_dir} (.git is writable)" in lines
    assert "FAIL agent user can sudo (sudo -n true succeeded)" in lines
    assert any(line.startswith("FAIL agent is not in an equipa-agent scope")
               for line in lines)
    assert f"FAIL agent can signal the orchestrator (pid {os.getpid()})" in lines
    assert lines[-1].startswith("RESULT: FAIL")
    assert not list(git_dir.iterdir())


def _verification_config(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "exchange").mkdir(exist_ok=True)
    monkeypatch.setattr(isolation, "get_active_dispatch_config", lambda: {
        "features": {"agent_isolation": True},
        "agent_isolation": {"exchange_dir": str(tmp_path / "exchange"),
                            # Required since task 3136 (ISO-05).
                            "secret_scan_roots": [str(tmp_path)]}})
    monkeypatch.setattr(isolation, "THEFORGE_DB", tmp_path / "missing.db")


@pytest.mark.parametrize("rule_status, probe_output, expected", [
    (0, "PASS a\nRESULT: PASS\n", 0),
    (0, "FAIL agent can read x\nRESULT: FAIL (1 failed)\n", 1),
    (1, "PASS a\nRESULT: PASS\n", 1),   # sudoers rule missing
])
def test_verification_entry_point_exit_codes(tmp_path: Path, monkeypatch, capsys,
                                             rule_status, probe_output,
                                             expected) -> None:
    _verification_config(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(isolation, "_exit_status", lambda argv: rule_status)

    async def fake_probe(command, config):
        seen["command"], seen["config"] = command, config
        return probe_output

    monkeypatch.setattr(isolation, "_run_probe", fake_probe)
    status = isolation.verification_main(["--verify-probe", str(VERIFY_SCRIPT)])
    assert status == expected
    # The probe replaces the CLI, so it runs through the real launch path.
    assert seen["config"]["agent_isolation"]["claude_executable"] == \
        str(VERIFY_SCRIPT)
    assert seen["command"][1] == "--inside"
    if rule_status:
        assert "EQUIPA_AGENT_LAUNCH" in capsys.readouterr().out


def test_verification_entry_point_reports_refusal(tmp_path: Path, monkeypatch,
                                                  capsys) -> None:
    _verification_config(tmp_path, monkeypatch)
    monkeypatch.setattr(isolation, "_exit_status", lambda argv: 0)

    async def refused(*args, **kwargs):
        raise isolation.AgentIsolationError("agent user 'equipa-agent' does "
                                            "not exist")

    monkeypatch.setattr(isolation, "spawn_isolated_agent", refused)
    status = isolation.verification_main(["--verify-probe", str(VERIFY_SCRIPT)])
    assert status == 2
    assert "isolation could not be established" in capsys.readouterr().out


def test_verification_entry_point_refuses_root(monkeypatch, capsys) -> None:
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 0)
    assert isolation.verification_main(
        ["--verify-probe", str(VERIFY_SCRIPT)]) == 2
    assert "not root" in capsys.readouterr().out


def test_verify_script_refuses_root_outer_mode() -> None:
    text = VERIFY_SCRIPT.read_text()
    assert 'if [ "$(id -u)" -eq 0 ]; then' in text
