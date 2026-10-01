#!/usr/bin/env python3
"""Task 3150 / review finding RR3144-A: user scope cannot switch the gate off.

Without agent isolation the agent shares the operator's HOME, so it can
write ``~/.claude/settings.json``. The independent review reproduced four
bypasses of the PreToolUse Bash gate with the real CLI: a user-scope
``env.SHELL`` (the planted shell ran every command), a user-scope
``env.BASH_FUNC_ls%%``, a user PreToolUse hook answering ``updatedInput``,
and a function in ``~/.bashrc``.

Now every Claude CLI run EQUIPA starts gets its own empty, 0700,
EQUIPA-created CLAUDE_CONFIG_DIR, removed after the run, so user-scope
settings, hooks and env blocks do not exist for it. CLAUDE_CODE_SHELL is
pinned to a root-owned absolute bash (in the CLI environment and in the
--settings env block), and BASH_ENV, ENV, PROMPT_COMMAND and every
BASH_FUNC_* name are removed from the CLI environment.

These tests drive the real ``_spawn_agent_process`` with a fake ``claude``
that reports the environment and config directory it was given. The live
probes against the real CLI are in scripts/verify_gate_config_isolation.sh,
which the operator runs (they need a token and the network).

Every credential here is a fake sentinel.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from equipa import agent_runner, cli_isolation
from equipa.agent_runner import (
    PRETOOLUSE_HOOK_SCRIPT,
    _pretooluse_settings_payload,
    build_cli_command,
)
from equipa.cli_isolation import (
    claude_cli_env,
    create_run_config_dir,
    fresh_claude_config_dir,
    has_env_auth,
    remove_run_config_dir,
    trusted_bash,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_gate_config_isolation.sh"
GATE_ON = {"features": {"bash_security_pretooluse": True}}
FAKE_TOKEN = "fake-3150-oauth-token-sentinel"

# The fake CLI: reports its environment and its config directory, then exits.
# The report goes next to the script (agent env is allowlisted, see 3120).
FAKE_CLAUDE = '''#!{python}
import json, os, stat, sys
here = os.path.dirname(os.path.abspath(__file__))
config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
info = os.lstat(config_dir) if config_dir and os.path.lexists(config_dir) else None
report = {{
    "env": dict(os.environ),
    "argv": sys.argv[1:],
    "config_dir": config_dir,
    "config_dir_is_dir": bool(info) and stat.S_ISDIR(info.st_mode),
    "config_dir_mode": stat.S_IMODE(info.st_mode) if info else None,
    "config_dir_uid": info.st_uid if info else None,
    "config_dir_entries": sorted(os.listdir(config_dir)) if info else None,
}}
with open(os.path.join(here, "report.json"), "w", encoding="utf-8") as fh:
    json.dump(report, fh)
'''

# What an agent plants in the operator's user scope (the review's vectors).
PLANTED_USER_SETTINGS = {
    "env": {"SHELL": "/tmp/planted/bash",
            "BASH_FUNC_ls%%": "() { echo ran; }"},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/tmp/planted/rewrite_hook.sh"}]}]},
}


@pytest.fixture
def operator_config_dir(tmp_path: Path) -> Path:
    """The operator's (agent-writable) ~/.claude, with a hostile user scope."""
    config = tmp_path / "operator-home" / ".claude"
    config.mkdir(parents=True)
    (config / "settings.json").write_text(
        json.dumps(PLANTED_USER_SETTINGS), encoding="utf-8")
    (config / "CLAUDE.md").write_text("planted standing orders\n",
                                      encoding="utf-8")
    return config


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable),
                      encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def run_parent(tmp_path: Path, monkeypatch) -> Path:
    """Per-run config directories land here, so the test can list them."""
    parent = tmp_path / "orchestrator-tmp"
    parent.mkdir(mode=0o700)
    real_create = cli_isolation.create_run_config_dir
    monkeypatch.setattr(agent_runner, "create_run_config_dir",
                        lambda: real_create(str(parent)))
    return parent


@pytest.fixture
def hostile_agent_env(tmp_path: Path, operator_config_dir: Path, monkeypatch):
    """The allowlisted agent env, plus every shell-injection name an operator
    passthrough could carry."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(operator_config_dir.parent),
        "SHELL": "/bin/sh",
        "CLAUDE_CONFIG_DIR": str(operator_config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
        "BASH_FUNC_ls%%": "() { echo ran; }",
        "BASH_FUNC_git%%": "() { echo ran; }",
        "BASH_ENV": "/tmp/planted-bash-env.sh",
        "ENV": "/tmp/planted-sh-env.sh",
        "PROMPT_COMMAND": "echo ran",
        "CLAUDE_CODE_SHELL": "/tmp/planted/bash",
    }
    monkeypatch.setattr(agent_runner, "_agent_subprocess_env",
                        lambda: dict(env))
    return env


async def _spawn_and_finish(cmd: list[str], project: Path) -> str:
    process, agent = await agent_runner._spawn_agent_process(
        cmd, project_dir=str(project))
    await process.communicate()
    await agent_runner._terminate_agent(process, agent)
    if agent is not None:
        agent.release()
    return str(process.returncode)


def _run_fake(fake_claude: Path, tmp_path: Path) -> dict:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    cmd = [str(fake_claude), "-p", "x", "--add-dir", str(project)]
    returncode = asyncio.run(_spawn_and_finish(cmd, project))
    assert returncode == "0"
    return json.loads((fake_claude.parent / "report.json").read_text(
        encoding="utf-8"))


# --- the --settings env pin ---------------------------------------------------

def test_settings_env_pins_claude_code_shell_to_an_absolute_root_bash():
    """On base the payload emptied CLAUDE_CODE_SHELL, so the CLI used SHELL."""
    payload = _pretooluse_settings_payload(PRETOOLUSE_HOOK_SCRIPT,
                                           sys.executable)
    shell = payload["env"]["CLAUDE_CODE_SHELL"]
    assert shell == trusted_bash()
    assert os.path.isabs(shell)
    resolved = os.stat(os.path.realpath(shell))
    assert resolved.st_uid == 0
    assert not resolved.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


@pytest.mark.parametrize("name", ["BASH_ENV", "ENV", "PROMPT_COMMAND"])
def test_settings_env_still_empties_shell_startup_names(name):
    payload = _pretooluse_settings_payload(PRETOOLUSE_HOOK_SCRIPT,
                                           sys.executable)
    assert payload["env"][name] == ""


def test_generated_settings_file_carries_the_shell_pin():
    with build_cli_command("PROMPT", project_dir="/tmp", max_turns=3,
                           model="sonnet", dispatch_config=GATE_ON) as cmd:
        path = cmd[cmd.index("--settings") + 1]
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["env"]["CLAUDE_CODE_SHELL"] == trusted_bash()


def test_relative_shell_pin_is_refused():
    with pytest.raises(ValueError, match="CLAUDE_CODE_SHELL"):
        _pretooluse_settings_payload(PRETOOLUSE_HOOK_SCRIPT, sys.executable,
                                     shell_bin="bash")


# --- trusted_bash -------------------------------------------------------------

def test_trusted_bash_accepts_the_system_bash():
    assert trusted_bash() in ("/bin/bash", "/usr/bin/bash")


def test_trusted_bash_rejects_relative_missing_and_user_owned(tmp_path):
    user_bash = tmp_path / "bash"
    user_bash.write_text("#!/bin/sh\n", encoding="utf-8")
    user_bash.chmod(0o755)
    assert os.stat(user_bash).st_uid != 0, "test must not run as root"
    assert trusted_bash(("bash",)) is None
    assert trusted_bash((str(tmp_path / "missing-bash"),)) is None
    assert trusted_bash((str(user_bash),)) is None
    assert trusted_bash((str(user_bash), "/bin/bash")) == "/bin/bash"


def test_trusted_bash_rejects_a_user_symlink_to_a_user_file(tmp_path):
    target = tmp_path / "evil"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    link = tmp_path / "bash"
    link.symlink_to(target)
    assert trusted_bash((str(link),)) is None


# --- claude_cli_env -----------------------------------------------------------

def test_claude_cli_env_replaces_config_dir_and_strips_injection(tmp_path):
    base = {
        "PATH": "/usr/bin:/bin", "HOME": "/home/op",
        "CLAUDE_CONFIG_DIR": "/home/op/.claude",
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
        "BASH_FUNC_ls%%": "() { id; }", "BASH_FUNC_cd%%": "() { id; }",
        "BASH_ENV": "/x", "ENV": "/x", "PROMPT_COMMAND": "id",
        "SHELLOPTS": "xtrace", "BASHOPTS": "x", "PS4": "$(id)",
        "CLAUDE_CODE_SHELL": "/tmp/planted/bash",
    }
    env = claude_cli_env(base, str(tmp_path))
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert env["CLAUDE_CODE_SHELL"] == trusted_bash()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/home/op"
    for name in ("BASH_FUNC_ls%%", "BASH_FUNC_cd%%", "BASH_ENV", "ENV",
                 "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "PS4"):
        assert name not in env, name
    assert base["CLAUDE_CONFIG_DIR"] == "/home/op/.claude", "input not mutated"


def test_claude_cli_env_refuses_relative_paths():
    with pytest.raises(ValueError):
        claude_cli_env({}, "relative/dir")
    with pytest.raises(ValueError):
        claude_cli_env({}, "/abs/dir", shell="bash")


@pytest.mark.parametrize("env, expected", [
    ({"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}, True),
    ({"ANTHROPIC_API_KEY": "fake"}, True),
    ({"CLAUDE_CODE_OAUTH_TOKEN": "  "}, False),
    ({"PATH": "/usr/bin"}, False),
])
def test_has_env_auth(env, expected):
    assert has_env_auth(env) is expected


# --- the per-run directory ----------------------------------------------------

def test_run_config_dir_is_private_empty_and_removed(tmp_path):
    with fresh_claude_config_dir(str(tmp_path)) as config_dir:
        info = os.lstat(config_dir)
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()
        assert os.listdir(config_dir) == []
        assert Path(config_dir).parent == tmp_path
        Path(config_dir, "projects").mkdir()
        Path(config_dir, "projects", "session.jsonl").write_text("x")
    assert not os.path.lexists(config_dir)


def test_run_config_dir_is_removed_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with fresh_claude_config_dir(str(tmp_path)) as config_dir:
            raise RuntimeError("boom")
    assert not os.path.lexists(config_dir)


def test_removal_never_follows_a_symlink_swapped_in(tmp_path):
    """An agent that replaces its config dir with a symlink to something it
    wants deleted gets nothing deleted."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    config_dir = create_run_config_dir(str(tmp_path))
    os.rmdir(config_dir)
    os.symlink(victim, config_dir)
    errors = remove_run_config_dir(config_dir)
    assert errors, "a symlinked config dir must be refused, not followed"
    assert (victim / "keep.txt").read_text() == "keep"
    os.unlink(config_dir)


def test_removal_does_not_follow_symlinks_inside(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    config_dir = create_run_config_dir(str(tmp_path))
    os.symlink(victim, os.path.join(config_dir, "link"))
    assert remove_run_config_dir(config_dir) == []
    assert not os.path.lexists(config_dir)
    assert (victim / "keep.txt").read_text() == "keep"


# --- through the real spawn path ----------------------------------------------

@pytest.mark.parametrize("containment", [True, False],
                         ids=["launcher", "direct"])
def test_cli_gets_a_fresh_private_config_dir_not_the_user_scope(
        tmp_path, fake_claude, run_parent, hostile_agent_env,
        operator_config_dir, monkeypatch, containment):
    """On base the CLI got the operator's CLAUDE_CONFIG_DIR, which holds the
    planted settings.json (env.SHELL, BASH_FUNC_ls%%, a rewriting hook)."""
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: containment)
    report = _run_fake(fake_claude, tmp_path)

    config_dir = report["config_dir"]
    assert config_dir != str(operator_config_dir)
    assert Path(config_dir).parent == run_parent
    assert Path(config_dir).name.startswith(cli_isolation.RUN_CONFIG_DIR_PREFIX)
    assert report["config_dir_is_dir"] is True
    assert report["config_dir_mode"] == 0o700
    assert report["config_dir_uid"] == os.getuid()
    assert report["config_dir_entries"] == [], "nothing from user scope"
    assert not os.path.lexists(config_dir), "removed after the run"
    assert (operator_config_dir / "settings.json").exists(), "operator's kept"


@pytest.mark.parametrize("containment", [True, False],
                         ids=["launcher", "direct"])
def test_cli_env_pins_the_shell_and_drops_shell_injection(
        tmp_path, fake_claude, run_parent, hostile_agent_env, monkeypatch,
        containment):
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: containment)
    env = _run_fake(fake_claude, tmp_path)["env"]
    assert env["CLAUDE_CODE_SHELL"] == trusted_bash()
    for name in ("BASH_FUNC_ls%%", "BASH_FUNC_git%%", "BASH_ENV", "ENV",
                 "PROMPT_COMMAND"):
        assert name not in env, name


def test_cli_still_gets_what_it_needs_to_authenticate_and_run(
        tmp_path, fake_claude, run_parent, hostile_agent_env, monkeypatch):
    """The token, PATH and HOME reach the CLI; the argv keeps the isolation
    flags, so the run itself is unchanged apart from the config dir."""
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    report = _run_fake(fake_claude, tmp_path)
    assert report["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert report["env"]["PATH"] == hostile_agent_env["PATH"]
    assert report["env"]["HOME"] == hostile_agent_env["HOME"]
    argv = report["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv


def test_each_run_gets_its_own_directory(
        tmp_path, fake_claude, run_parent, hostile_agent_env, monkeypatch):
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    first = _run_fake(fake_claude, tmp_path)["config_dir"]
    second = _run_fake(fake_claude, tmp_path)["config_dir"]
    assert first != second
    assert os.listdir(run_parent) == []


def test_failed_spawn_removes_the_directory(
        tmp_path, run_parent, hostile_agent_env, monkeypatch):
    async def refuse(cmd, kwargs):
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"].startswith(str(run_parent))
        raise FileNotFoundError("command not found: claude")

    monkeypatch.setattr(agent_runner, "_spawn_unisolated", refuse)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(FileNotFoundError):
        asyncio.run(agent_runner._spawn_agent_process(
            ["claude", "-p", "x"], project_dir=str(project)))
    assert os.listdir(run_parent) == []


def test_uncreatable_directory_refuses_the_dispatch(
        tmp_path, hostile_agent_env, monkeypatch):
    def broken() -> str:
        raise cli_isolation.RunConfigDirError("no space left")

    monkeypatch.setattr(agent_runner, "create_run_config_dir", broken)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(agent_runner.AgentDispatchRefused, match="no space"):
        asyncio.run(agent_runner._spawn_agent_process(
            ["claude", "-p", "x"], project_dir=str(project)))


def test_non_claude_commands_keep_their_environment(
        tmp_path, run_parent, hostile_agent_env, monkeypatch):
    """Only the Claude CLI reads CLAUDE_CONFIG_DIR; other argv are unchanged."""
    seen: dict = {}

    async def record(cmd, kwargs):
        seen.update(kwargs["env"])
        raise FileNotFoundError("stop here")

    monkeypatch.setattr(agent_runner, "_spawn_unisolated", record)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(FileNotFoundError):
        asyncio.run(agent_runner._spawn_agent_process(
            [sys.executable, "-c", "pass"], project_dir=str(project)))
    assert seen["CLAUDE_CONFIG_DIR"] == hostile_agent_env["CLAUDE_CONFIG_DIR"]
    assert os.listdir(run_parent) == []


def test_missing_env_credential_is_named_in_the_log(
        tmp_path, fake_claude, run_parent, monkeypatch, caplog):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(tmp_path)}
    monkeypatch.setattr(agent_runner, "_agent_subprocess_env",
                        lambda: dict(env))
    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    monkeypatch.setattr(agent_runner, "_env_auth_warning_logged", False)
    with caplog.at_level(logging.WARNING, logger=agent_runner.logger.name):
        _run_fake(fake_claude, tmp_path)
        _run_fake(fake_claude, tmp_path)
    warnings = [r for r in caplog.records
                if "CLAUDE_CODE_OAUTH_TOKEN" in r.getMessage()]
    assert len(warnings) == 1, "warned once, naming the variable"


# --- the RLM claude -p calls ----------------------------------------------------

@pytest.mark.parametrize("call", ["sub_query", "outer_agent"])
def test_rlm_claude_calls_get_a_fresh_config_dir(
        tmp_path, operator_config_dir, monkeypatch, call):
    """On base the RLM calls passed the operator's CLAUDE_CONFIG_DIR."""
    from equipa import rlm_decompose

    monkeypatch.setattr(rlm_decompose, "active_agent_env", lambda: {
        "PATH": "/usr/bin:/bin", "HOME": str(operator_config_dir.parent),
        "CLAUDE_CONFIG_DIR": str(operator_config_dir),
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
        "BASH_FUNC_ls%%": "() { id; }"})
    monkeypatch.setattr(rlm_decompose, "unisolated_spawn_refusal",
                        lambda *args, **kwargs: None)
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        config_dir = kwargs["env"]["CLAUDE_CONFIG_DIR"]
        seen.update(env=kwargs["env"], config_dir=config_dir,
                    entries=os.listdir(config_dir),
                    mode=stat.S_IMODE(os.lstat(config_dir).st_mode))
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(rlm_decompose.subprocess, "run", fake_run)
    if call == "sub_query":
        rlm_decompose._run_sub_query("q", {"a.py": "x"}, "haiku",
                                     str(tmp_path), "")
    else:
        rlm_decompose._call_outer_agent("p", "haiku", str(tmp_path), 30)
    assert seen["config_dir"] != str(operator_config_dir)
    assert seen["entries"] == [] and seen["mode"] == 0o700
    assert seen["env"]["CLAUDE_CODE_SHELL"] == trusted_bash()
    assert "BASH_FUNC_ls%%" not in seen["env"]
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert not os.path.lexists(seen["config_dir"])


# --- the standalone claude -p calls (ForgeSmith, SIMBA, autoresearch) ---------

def _load_script(name: str, filename: str, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _standalone_call(name: str, monkeypatch):
    """Load the caller, make its spawn refusal pass, return a function that
    makes its claude call."""
    if name == "forgesmith-ghost" or name == "forgesmith-opro":
        monkeypatch.syspath_prepend(str(REPO_ROOT))
        import forgesmith as module
        if name == "forgesmith-ghost":
            def call():
                module.dispatch_ghost_scout("p")
        else:
            def call():
                module.call_claude_for_proposals("p", {"opro": {"model": None}})
    elif name == "simba":
        module = _load_script("forgesmith_simba_3150", "forgesmith_simba.py",
                              monkeypatch)
        monkeypatch.setattr(module, "resolve_claude_model",
                            lambda *_a, **_k: "fake-model")

        def call():
            module.call_claude_for_rules("p", {})
    else:
        module = _load_script("autoresearch_loop_3150", "autoresearch_loop.py",
                              monkeypatch)
        monkeypatch.setattr(module, "is_on_claudinator", lambda: True)

        def call():
            module.mutate_prompt("developer", "old", "none",
                                 {"success_rate": 50})
    monkeypatch.setattr(module, "unisolated_spawn_refusal",
                        lambda *args, **kwargs: None)
    return module, call


@pytest.mark.parametrize("caller", [
    "forgesmith-ghost", "forgesmith-opro", "simba", "autoresearch-local"])
def test_standalone_claude_calls_get_a_fresh_config_dir(
        caller, operator_config_dir, monkeypatch):
    """On base these calls inherited the operator's environment, so the CLI
    read the agent-writable ~/.claude user scope."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(operator_config_dir))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("BASH_FUNC_ls%%", "() { id; }")
    monkeypatch.setenv("BASH_ENV", str(operator_config_dir / "planted.sh"))
    module, call = _standalone_call(caller, monkeypatch)
    seen: list[dict] = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env")
        assert env is not None, "the CLI must not inherit the environment"
        config_dir = env["CLAUDE_CONFIG_DIR"]
        seen.append({"env": env, "config_dir": config_dir,
                     "entries": os.listdir(config_dir),
                     "mode": stat.S_IMODE(os.lstat(config_dir).st_mode)})
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "{}"}), stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    call()

    assert len(seen) == 1
    run = seen[0]
    assert run["config_dir"] != str(operator_config_dir)
    assert run["entries"] == [] and run["mode"] == 0o700
    assert run["env"]["CLAUDE_CODE_SHELL"] == trusted_bash()
    assert "BASH_FUNC_ls%%" not in run["env"]
    assert "BASH_ENV" not in run["env"]
    assert run["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert not os.path.lexists(run["config_dir"])


@pytest.mark.parametrize("caller", ["forgesmith-ghost", "simba"])
def test_standalone_call_without_a_config_dir_does_not_run(caller, monkeypatch):
    module, call = _standalone_call(caller, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise cli_isolation.RunConfigDirError("no per-run directory")

    def fake_run(cmd, **kwargs):
        raise AssertionError("the CLI ran without its own config directory")

    monkeypatch.setattr(module, "claude_cli_run_env", refuse)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    call()


def test_autoresearch_ssh_command_gets_a_fresh_config_dir(monkeypatch):
    """The remote shell command creates, uses and removes its own directory
    and pins the shell; on base it ran ``claude ... < file`` as is."""
    module, call = _standalone_call("autoresearch-ssh", monkeypatch)
    monkeypatch.setattr(module, "is_on_claudinator", lambda: False)
    commands: list[str] = []

    def fake_run(argv, **kwargs):
        if argv[0] == "ssh":
            commands.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="NEW PROMPT",
                                           stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    call()

    assert len(commands) == 1
    prefix = cli_isolation.REMOTE_RUN_CONFIG_DIR_PREFIX
    assert commands[0].startswith(prefix + module.CLAUDE_MUTATE_COMMAND)
    for part in ('mktemp -d', 'trap \'rm -rf -- "$cfg"\' EXIT',
                 'CLAUDE_CONFIG_DIR="$cfg"', "CLAUDE_CODE_SHELL=/bin/bash",
                 "-u BASH_ENV", "-u ENV", "-u PROMPT_COMMAND"):
        assert part in prefix


def test_remote_prefix_runs_claude_in_a_private_dir_and_removes_it(tmp_path):
    """Run the real prefix with bash and a fake ``claude`` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    report = tmp_path / "report.txt"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'stat -c "%a" "$CLAUDE_CONFIG_DIR" > "{report}"\n'
        f'echo "$CLAUDE_CONFIG_DIR $CLAUDE_CODE_SHELL ${{BASH_ENV-unset}}" '
        f'>> "{report}"\n'
        "cat\n"
        "exit 7\n", encoding="utf-8")
    fake.chmod(0o755)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello prompt\n", encoding="utf-8")
    command = (cli_isolation.REMOTE_RUN_CONFIG_DIR_PREFIX
               + f'claude --print < "{prompt}"')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "BASH_ENV": str(tmp_path / "planted.sh")}

    result = subprocess.run(["bash", "-c", command], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 7, "the claude exit status is kept"
    assert result.stdout == "hello prompt\n", "the prompt reaches claude"
    mode, details = report.read_text(encoding="utf-8").splitlines()
    config_dir, shell, bash_env = details.split()
    assert mode == "700"
    assert config_dir.startswith(str(tmp_path / cli_isolation.RUN_CONFIG_DIR_PREFIX))
    assert shell == "/bin/bash" and bash_env == "unset"
    assert not os.path.lexists(config_dir), "removed when the shell exits"


# --- the operator's live probe script -----------------------------------------

def test_verify_script_exists_and_is_executable():
    assert VERIFY_SCRIPT.is_file()
    assert os.access(VERIFY_SCRIPT, os.X_OK)
    result = subprocess.run(["bash", "-n", str(VERIFY_SCRIPT)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("probe, marker", [
    ("S", "user_env_SHELL"),
    ("B", "user_env_BASH_FUNC"),
    ("U", "user_hook_updatedInput"),
    ("R", "bashrc_function"),
])
def test_verify_script_runs_each_of_the_four_probes(probe, marker):
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert f"probe {probe}_{marker}" in text
    assert "PASS" in text and "FAIL" in text


# A stand-in for the real CLI that behaves like it where the review showed
# the bypasses: it loads <CLAUDE_CONFIG_DIR>/settings.json and lets a user
# env.SHELL, env.BASH_FUNC_* or PreToolUse hook run planted code, and it
# sources $HOME/.bashrc for its shell snapshot. Otherwise it "runs" ls and
# prints the stream-json events the script parses. No network.
SIMULATED_CLI = r'''#!{python}
import json, os, re, sys
config = os.environ.get("CLAUDE_CONFIG_DIR", "")
settings = {{}}
try:
    with open(os.path.join(config, "settings.json"), encoding="utf-8") as fh:
        settings = json.load(fh)
except (OSError, ValueError):
    pass
env = settings.get("env", {{}})
bypassed = bool(settings.get("hooks")) or any(
    name == "SHELL" or name.startswith("BASH_FUNC_") for name in env)
try:
    with open(os.path.join(os.environ["HOME"], ".bashrc"), encoding="utf-8") as fh:
        bypassed = bypassed or bool(re.search(r"^ls\(\)", fh.read(), re.M))
except OSError:
    pass
if bypassed:
    with open("hk.txt", "w", encoding="utf-8") as fh:
        fh.write("ran\n")
events = [
    {{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "name": "Bash", "input": {{"command": "ls"}}}}]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "is_error": False, "content": "hk.txt"}}]}}}},
    {{"type": "result", "is_error": False, "result": "DONE"}},
]
for event in events:
    print(json.dumps(event))
'''


def _run_verify_script_with(cli_source: str, tmp_path: Path,
                            *args: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "simbin"
    bin_dir.mkdir()
    cli = bin_dir / "claude"
    cli.write_text(cli_source.format(python=sys.executable), encoding="utf-8")
    cli.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
    }
    return subprocess.run(["bash", str(VERIFY_SCRIPT), *args],
                          capture_output=True, text=True, timeout=300,
                          env=env, cwd=tmp_path)


def test_verify_script_passes_the_user_scope_probes_and_reports_bashrc(
        tmp_path):
    """With the simulated CLI, S/B/U PASS because EQUIPA hands the CLI an
    empty config dir; R FAILs (known open until isolation) but does not
    fail the run. On base, S/B/U FAIL: the CLI got the planted user scope."""
    result = _run_verify_script_with(SIMULATED_CLI, tmp_path)
    out = result.stdout
    assert "[PASS] probe S_user_env_SHELL" in out, out + result.stderr
    assert "[PASS] probe B_user_env_BASH_FUNC" in out, out
    assert "[PASS] probe U_user_hook_updatedInput" in out, out
    assert "[FAIL] probe R_bashrc_function" in out, out
    assert "known open until agent isolation" in out
    assert result.returncode == 0, out + result.stderr
    assert FAKE_TOKEN not in out + result.stderr


def test_verify_script_strict_counts_the_bashrc_probe(tmp_path):
    result = _run_verify_script_with(SIMULATED_CLI, tmp_path, "--strict")
    assert "[FAIL] probe R_bashrc_function" in result.stdout
    assert result.returncode == 1


def test_verify_script_is_inconclusive_when_the_cli_does_not_run_ls(tmp_path):
    """A CLI that cannot authenticate must not read as a PASS."""
    not_logged_in = ('#!{python}\nimport sys\n'
                     'print("Invalid API key · Please run /login")\n'
                     'sys.exit(1)\n')
    result = _run_verify_script_with(not_logged_in, tmp_path)
    assert "[INCONCLUSIVE] probe S_user_env_SHELL" in result.stdout
    assert "[PASS]" not in result.stdout
    assert result.returncode == 3


def test_verify_script_uses_a_throwaway_home_and_never_prints_the_token():
    text = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert "mktemp -d" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert 'echo "$CLAUDE_CODE_OAUTH_TOKEN' not in text
    assert "set -x" not in text
    # EQUIPA's own spawn path builds the env and argv the probes run with.
    assert "_spawn_agent_process" in text
    assert "build_cli_command" in text
