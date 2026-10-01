#!/usr/bin/env python3
"""Task 3120: agent subprocess env allowlist, cwd, redaction, reactive check.

Review findings loop-03 / sandbox-03 (env allowlist), sandbox-17 (operator
hooks), sandbox-11 (cwd and relative MCP --db-path), sandbox-12 (write-time
redaction), sandbox-04 (defer to an active gate), sandbox-07 iii (reactive
check off the event loop) and the misleading "[EarlyTerm] Killing agent
process (reason: None)" line on a normal finish.

Every test spawns real child processes: a fake ``claude`` script that records
the environment and cwd it was started with and replays scripted stream-json.
All credential values are obviously fake sentinels.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from equipa.hooks import run_external_hook, run_external_hook_async

# --- fixtures and helpers ----------------------------------------------------

# Variables that must never reach an agent or hook.
SENTINELS = {
    "DATABASE_URL": "postgresql://fake:fake-sentinel-pw@db.invalid/fake",
    "PGPASSWORD": "fake-sentinel-pgpassword",
    "PGHOST": "db.invalid",
    "PGPASSFILE": "/nonexistent/fake-sentinel-pgpass",
    "ANTHROPIC_API_KEY": "sk-fake-sentinel-api-key-0000",
    "GITHUB_TOKEN": "fake-sentinel-github-token",
    "GH_TOKEN": "fake-sentinel-gh-token",
    "EQUIPA_FAKE_SERVICE_TOKEN": "fake-sentinel-service-token",
    "EQUIPA_FAKE_API_KEY": "fake-sentinel-api-key",
    "EQUIPA_FAKE_SECRET_VALUE": "fake-sentinel-secret",
    "EQUIPA_FAKE_DB_PASSWORD": "fake-sentinel-db-password",
    "XDG_FAKE_SESSION_TOKEN": "fake-sentinel-xdg-token",
    "EQUIPA_FAKE_UNLISTED": "fake-sentinel-plain-value",
}

# Variables that must arrive unchanged.
ALLOWED = {
    "CLAUDE_CODE_OAUTH_TOKEN": "fake-sentinel-oauth-token",
    "CLAUDE_CONFIG_DIR": "/nonexistent/fake-claude-config",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "TERM": "dumb",
    "USER": "equipa-test",
    "LOGNAME": "equipa-test",
    "SHELL": "/bin/sh",
    "XDG_CONFIG_HOME": "/nonexistent/fake-xdg-config",
}

# Records env and cwd next to itself, replays stream.jsonl when present
# (else prints one result line), then lingers if linger.txt says so.
FAKE_CLI = '''import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "seen.json"), "w", encoding="utf-8") as fh:
    json.dump({"env": dict(os.environ), "cwd": os.getcwd()}, fh)
stream = os.path.join(here, "stream.jsonl")
if os.path.exists(stream):
    for line in open(stream, encoding="utf-8"):
        sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(0.01)
else:
    print(json.dumps({"type": "result", "subtype": "success", "result": "done",
                      "num_turns": 1, "is_error": False}), flush=True)
linger = os.path.join(here, "linger.txt")
if os.path.exists(linger):
    time.sleep(float(open(linger).read()))
'''

# Operator hook: records env and cwd next to itself.
HOOK_DUMP = '''import json, os
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "hook_seen.json"), "w", encoding="utf-8") as fh:
    json.dump({"env": dict(os.environ), "cwd": os.getcwd()}, fh)
'''

FLAGGED_CMD = "ls -la <(echo hi)"  # check 8: process substitution
FLAGGED_CHECK = 8
FINAL = {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 2, "total_cost_usd": 0.0}


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    """Pin an empty active dispatch config (never the operator's file)."""
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


@pytest.fixture
def parent_env(monkeypatch):
    for name, value in {**SENTINELS, **ALLOWED}.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def agent(tmp_path):
    """A fake CLI in tmp_path/bin and a project dir that is not the cwd."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_claude.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    return bin_dir, fake, project


def _seen(bin_dir: Path) -> dict:
    return json.loads((bin_dir / "seen.json").read_text(encoding="utf-8"))


def _write_stream(bin_dir: Path, events: list[dict]) -> None:
    (bin_dir / "stream.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _assistant_bash(tool_id: str, command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"command": command}}]}}


def _result(tool_id: str, content: str, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id,
         "content": content, "is_error": is_error}]}}


def _run_plain(cmd, project):
    return agent_runner.run_agent(cmd, timeout=60, max_retries=1)


def _run_streaming(cmd, project, **kwargs):
    kwargs.setdefault("output", [])
    return agent_runner._run_agent_streaming_impl(
        cmd, role="developer", timeout=60, max_turns=40, **kwargs)


RUNNERS = [
    pytest.param(_run_plain, id="run_agent"),
    pytest.param(_run_streaming, id="streaming"),
]


@pytest.fixture(params=["launcher", "direct"])
def containment(request, monkeypatch):
    """Both spawn paths: through the per-agent launcher and without it."""
    if request.param == "direct":
        monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                            lambda: False)
    elif not agent_runner._agent_containment_supported():
        pytest.fail("the launcher path needs Linux")
    return request.param


# --- 1. loop-03 / sandbox-03: allowlisted env --------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_agent_child_gets_only_allowlisted_env(
        agent, parent_env, containment, runner):
    bin_dir, fake, project = agent
    cmd = [sys.executable, str(fake), "--add-dir", str(project)]

    result = asyncio.run(runner(cmd, project))

    assert result["success"], result.get("errors")
    env = _seen(bin_dir)["env"]
    leaked = sorted(name for name in SENTINELS if name in env)
    assert not leaked, f"credential variables reached the agent: {leaked}"
    for name, value in ALLOWED.items():
        assert env.get(name) == value, name
    assert env.get("PATH") and env.get("HOME")


def test_passthrough_adds_exact_names_but_not_the_api_key(agent, parent_env):
    bin_dir, fake, project = agent
    equipa_config._active_dispatch_config = {
        "agent_env_passthrough": ["EQUIPA_FAKE_UNLISTED", "ANTHROPIC_API_KEY",
                                  "PG*", "GITHUB_*"],
    }
    cmd = [sys.executable, str(fake), "--add-dir", str(project)]

    result = asyncio.run(_run_plain(cmd, project))

    assert result["success"], result.get("errors")
    env = _seen(bin_dir)["env"]
    assert env.get("EQUIPA_FAKE_UNLISTED") == SENTINELS["EQUIPA_FAKE_UNLISTED"]
    assert "ANTHROPIC_API_KEY" not in env  # subscription billing by default
    assert not any(name.startswith("PG") for name in env)  # no wildcards
    assert "GITHUB_TOKEN" not in env


def test_api_key_needs_the_explicit_opt_in(agent, parent_env):
    bin_dir, fake, project = agent
    equipa_config._active_dispatch_config = {
        "agent_env_passthrough": ["ANTHROPIC_API_KEY"],
        "agent_allow_api_key": True,
    }
    cmd = [sys.executable, str(fake), "--add-dir", str(project)]

    result = asyncio.run(_run_plain(cmd, project))

    assert result["success"], result.get("errors")
    env = _seen(bin_dir)["env"]
    assert env.get("ANTHROPIC_API_KEY") == SENTINELS["ANTHROPIC_API_KEY"]
    assert "DATABASE_URL" not in env


# --- 2. sandbox-17: operator hooks -------------------------------------------


def _hook_command(tmp_path: Path) -> tuple[str, Path]:
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    script = hook_dir / "dump_env.py"
    script.write_text(HOOK_DUMP, encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    return command, hook_dir / "hook_seen.json"


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_operator_hook_gets_scrubbed_env_plus_context(
        tmp_path, parent_env, use_async):
    command, seen_file = _hook_command(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    context = {"task_id": 3120, "role": "developer"}

    if use_async:
        code = asyncio.run(run_external_hook_async(command, context, str(project)))
    else:
        code = run_external_hook(command, context, str(project))

    assert code == 0
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    env = seen["env"]
    leaked = sorted(name for name in SENTINELS if name in env)
    assert not leaked, f"credential variables reached the hook: {leaked}"
    assert env["EQUIPA_HOOK_TASK_ID"] == "3120"
    assert env["EQUIPA_HOOK_ROLE"] == "developer"
    assert env.get("PATH") and env.get("LANG") == ALLOWED["LANG"]
    assert Path(seen["cwd"]).resolve() == project.resolve()


# --- 3. sandbox-11: cwd and relative MCP --db-path ---------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_agent_runs_in_the_project_dir(agent, containment, runner):
    bin_dir, fake, project = agent
    cmd = [sys.executable, str(fake), "--add-dir", str(project)]

    result = asyncio.run(runner(cmd, project))

    assert result["success"], result.get("errors")
    assert Path(_seen(bin_dir)["cwd"]).resolve() == project.resolve()


def test_streaming_project_dir_argument_sets_cwd(agent):
    bin_dir, fake, project = agent
    result = asyncio.run(_run_streaming([sys.executable, str(fake)], project,
                                        project_dir=str(project)))
    assert result["success"], result.get("errors")
    assert Path(_seen(bin_dir)["cwd"]).resolve() == project.resolve()


def _mcp_config(tmp_path: Path, db_args: list[str]) -> Path:
    # Absolute command: relative commands are refused since 3127 (P2A-02),
    # and missing ones since 3138 (RR-02), so an installed fake uvx. It sits
    # beside tmp_path, which some tests use as the project directory.
    uvx = tmp_path.parent / f"{tmp_path.name}-installed" / "uvx"
    uvx.parent.mkdir(exist_ok=True)
    uvx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uvx.chmod(0o755)
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps({"mcpServers": {
        "theforge": {"type": "stdio", "command": str(uvx),
                     "args": ["mcp-server-sqlite", *db_args]},
    }}), encoding="utf-8")
    return path


@pytest.mark.parametrize("db_args", [
    ["--db-path", "theforge.db"],
    ["--db-path=theforge.db"],
    ["--db-path", "~/theforge.db"],
    ["--db-path"],
])
def test_relative_db_path_refuses_the_cli_build(tmp_path, monkeypatch, db_args):
    monkeypatch.setattr(agent_runner, "MCP_CONFIG", _mcp_config(tmp_path, db_args))

    def no_tempfile(*args, **kwargs):
        raise AssertionError("prompt tempfile created before the refusal")

    # The refusal comes first, so no prompt tempfile can leak.
    monkeypatch.setattr(agent_runner.tempfile, "NamedTemporaryFile", no_tempfile)
    with pytest.raises(RuntimeError, match="relative --db-path"):
        with agent_runner.build_cli_command(
                "prompt", str(tmp_path), 5, "opus",
                dispatch_config={"effort": None}):
            pass


def test_absolute_db_path_builds(tmp_path, monkeypatch):
    db = tmp_path / "theforge.db"
    monkeypatch.setattr(agent_runner, "MCP_CONFIG",
                        _mcp_config(tmp_path, ["--db-path", str(db)]))
    # The installed fake uvx is listed as an operator lists ~/.local/bin/uvx:
    # since task 3144 (RR3138-C) a uvx must be the real one or listed.
    fake_uvx = tmp_path.parent / f"{tmp_path.name}-installed" / "uvx"
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {
        agent_runner.MCP_TRUSTED_EXECUTABLES_KEY: [str(fake_uvx)]})
    with agent_runner.build_cli_command(
            "prompt", str(tmp_path), 5, "opus",
            dispatch_config={"effort": None}) as cmd:
        assert cmd[cmd.index("--mcp-config") + 1] == str(agent_runner.MCP_CONFIG)


@pytest.mark.parametrize("runner", RUNNERS)
def test_spawn_refuses_relative_db_path_without_starting_the_cli(
        agent, tmp_path, runner):
    bin_dir, fake, project = agent
    config = _mcp_config(tmp_path, ["--db-path", "theforge.db"])
    cmd = [sys.executable, str(fake), "--add-dir", str(project),
           "--mcp-config", str(config)]

    result = asyncio.run(runner(cmd, project))

    assert not result["success"]
    assert any("relative --db-path" in err for err in result["errors"])
    assert not (bin_dir / "seen.json").exists(), "the CLI was started"


def test_missing_project_dir_is_refused(agent, tmp_path):
    bin_dir, fake, _ = agent
    cmd = [sys.executable, str(fake), "--add-dir", str(tmp_path / "gone")]

    result = asyncio.run(_run_plain(cmd, None))

    assert not result["success"]
    assert any("does not exist" in err for err in result["errors"])
    assert not (bin_dir / "seen.json").exists()


# --- 4. sandbox-12: redaction before persisting and logging ------------------

FAKE_PW = "FAKE-sentinel-pw-0000"
FAKE_BEARER = "FAKEbearerSentinel0000"


def test_flagged_command_secret_never_reaches_log_or_action_log(agent):
    bin_dir, fake, project = agent
    command = (f"PGPASSWORD={FAKE_PW} psql -h db.invalid -c 'select 1' "
               f"<(echo x)")
    _write_stream(bin_dir, [_assistant_bash("t1", command), FINAL])
    output: list[str] = []

    result = asyncio.run(_run_streaming(
        [sys.executable, str(fake)], project, output=output))

    assert result.get("early_terminated")  # flagged, no gate: killed
    assert any("[BashSecurity] BLOCKED" in line for line in output)
    assert FAKE_PW not in "\n".join(output)
    assert FAKE_PW not in json.dumps(result, default=str)
    preview = result["action_log"][0]["input_preview"]
    assert "PGPASSWORD=[REDACTED]" in preview


def test_error_summary_is_redacted(agent):
    bin_dir, fake, project = agent
    command = f"curl -s -H 'Authorization: Bearer {FAKE_BEARER}' https://x.invalid"
    _write_stream(bin_dir, [
        _assistant_bash("t1", command),
        _result("t1", f"curl: (6) Could not resolve host; sent "
                      f"Authorization: Bearer {FAKE_BEARER}", is_error=True),
        FINAL,
    ])

    result = asyncio.run(_run_streaming([sys.executable, str(fake)], project))

    entry = result["action_log"][0]
    assert FAKE_BEARER not in json.dumps(result["action_log"])
    assert "[REDACTED]" in entry["input_preview"]
    assert "[REDACTED]" in entry["error_summary"]


# --- 5. sandbox-04: the gate canary runs in the agent's env and cwd ----------

# A stand-in gate whose behaviour depends on its environment. REFUSES_IF
# decides whether it refuses (like the real gate) or lets everything through.
FAKE_GATE = '''import json, os, sys
payload = json.load(sys.stdin)
if {condition}:
    sys.stderr.write("Bash security check 8 BLOCKED command: "
                     + payload["tool_input"]["command"] + "\\n")
    sys.exit(2)
sys.exit(0)
'''


def _fake_gate(tmp_path: Path, monkeypatch, condition: str):
    gate = tmp_path / "gate" / "pretooluse_bash_gate.py"
    gate.parent.mkdir()
    gate.write_text(FAKE_GATE.format(condition=condition), encoding="utf-8")
    monkeypatch.setattr(agent_runner, "PRETOOLUSE_HOOK_SCRIPT", gate)
    payload = agent_runner._pretooluse_settings_payload(gate, sys.executable)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    hook_command = payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return settings, hook_command


def _refusal(hook_command: str) -> str:
    return (f"PreToolUse:Bash hook error: [{hook_command}]: Bash security check "
            f"{FLAGGED_CHECK} BLOCKED command: {FLAGGED_CMD}\n")


def test_canary_runs_with_the_agent_env(tmp_path, monkeypatch, parent_env):
    # Refuses only where the orchestrator's credentials are absent, i.e. in
    # the environment the CLI will actually run the hook in.
    _, hook_command = _fake_gate(tmp_path, monkeypatch,
                                 '"DATABASE_URL" not in os.environ')
    assert agent_runner._gate_canary_ok(hook_command)


def test_active_gate_refusal_does_not_kill_the_agent(
        agent, tmp_path, monkeypatch, parent_env):
    bin_dir, fake, project = agent
    settings, hook_command = _fake_gate(tmp_path, monkeypatch,
                                        '"DATABASE_URL" not in os.environ')
    _write_stream(bin_dir, [
        _assistant_bash("t1", FLAGGED_CMD),
        _result("t1", _refusal(hook_command), is_error=True),
        FINAL,
    ])
    output: list[str] = []

    result = asyncio.run(_run_streaming(
        [sys.executable, str(fake), "--settings", str(settings)], project,
        output=output))

    assert not result.get("early_terminated"), result.get("early_term_reason")
    assert any("refused before execution by the gate" in line for line in output)


def test_gate_broken_in_the_agent_env_means_kill_on_sight(
        agent, tmp_path, monkeypatch, parent_env):
    # Works only with an orchestrator-only variable: in the agent's env it
    # would fail open, so it must not be trusted and the flagged command is
    # killed on sight, before it could run (not after it EXECUTED).
    bin_dir, fake, project = agent
    monkeypatch.setenv("EQUIPA_FAKE_GATE_DEPENDENCY", "1")
    settings, hook_command = _fake_gate(
        tmp_path, monkeypatch, '"EQUIPA_FAKE_GATE_DEPENDENCY" in os.environ')
    _write_stream(bin_dir, [
        _assistant_bash("t1", FLAGGED_CMD),
        _result("t1", "lr-x------ 1 u u 64 /dev/fd/63 -> pipe:[1]"),
        FINAL,
    ])
    output: list[str] = []

    result = asyncio.run(_run_streaming(
        [sys.executable, str(fake), "--settings", str(settings)], project,
        output=output))

    assert result.get("early_terminated")
    assert "EXECUTED" not in result["early_term_reason"]
    assert any("failed its canary" in line for line in output)
    assert result["num_turns"] == 1


# --- 6. sandbox-07 iii: reactive check off the event loop, with a timeout ----


async def _with_heartbeat(coro) -> tuple[dict, float]:
    """Run ``coro`` while measuring the longest event-loop stall."""
    done = asyncio.Event()
    gaps: list[float] = []

    async def beat() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def run() -> dict:
        try:
            return await coro
        finally:
            done.set()

    result, _ = await asyncio.gather(run(), beat())
    return result, max(gaps)


# The reactive check runs in a worker process since task 3127 (P2A-01), so a
# slow checker is injected as a checker FILE the worker loads.
SLOW_CHECKER = '''import time
from dataclasses import dataclass
@dataclass(frozen=True)
class Result:
    safe: bool
    check_id: int = 0
    message: str = ""
def check(command):
    time.sleep(2.0)
    return Result(safe=True)
'''


def test_slow_reactive_check_times_out_as_a_block_without_freezing(
        agent, monkeypatch, tmp_path):
    bin_dir, fake, project = agent
    checker_file = tmp_path / "slow_checker.py"
    checker_file.write_text(SLOW_CHECKER, encoding="utf-8")
    checker = agent_runner.ReactiveBashChecker(checker_file, "check")
    monkeypatch.setattr(agent_runner, "_REACTIVE_CHECKER", checker)
    monkeypatch.setattr(agent_runner, "_SLOW_CHECK_SECONDS", 0.3)
    _write_stream(bin_dir, [_assistant_bash("t1", "ls -la"), FINAL])
    output: list[str] = []

    result, longest_stall = asyncio.run(_with_heartbeat(_run_streaming(
        [sys.executable, str(fake)], project, output=output)))

    assert longest_stall < 1.0, f"event loop frozen for {longest_stall:.2f}s"
    assert result.get("early_terminated")  # a timeout is a block, not a pass
    assert "did not finish" in result["early_term_reason"]


def test_fast_safe_check_is_unaffected(agent):
    bin_dir, fake, project = agent
    _write_stream(bin_dir, [_assistant_bash("t1", "ls -la"),
                            _result("t1", "total 0"), FINAL])
    result = asyncio.run(_run_streaming([sys.executable, str(fake)], project))
    assert not result.get("early_terminated"), result.get("early_term_reason")


# --- 7. no "[EarlyTerm] ... (reason: None)" on a normal finish ---------------


def test_normal_finish_logs_cleanup_not_early_term(agent):
    bin_dir, fake, project = agent
    _write_stream(bin_dir, [FINAL])
    (bin_dir / "linger.txt").write_text("3", encoding="utf-8")
    output: list[str] = []

    result = asyncio.run(_run_streaming(
        [sys.executable, str(fake)], project, output=output))

    assert not result.get("early_terminated")
    assert not any("[EarlyTerm] Killing agent process" in line for line in output)
    assert any("[Cleanup]" in line for line in output)


def test_real_early_term_still_logs_the_kill_with_its_reason(agent):
    bin_dir, fake, project = agent
    _write_stream(bin_dir, [_assistant_bash("t1", FLAGGED_CMD), FINAL])
    (bin_dir / "linger.txt").write_text("3", encoding="utf-8")
    output: list[str] = []

    asyncio.run(_run_streaming([sys.executable, str(fake)], project,
                               output=output))

    kills = [line for line in output if "[EarlyTerm] Killing agent process" in line]
    assert kills and "reason: None" not in kills[0]
    assert "Bash security violation" in kills[0]
