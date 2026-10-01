#!/usr/bin/env python3
"""Task 3144 RR3138-A: the Bash gate hook runs as ``<abs python> -I <hook>``.

The independent review of 3138 ran the real CLI with EQUIPA's generated
``--settings`` and showed the gate fail open in two ways. A user-scope
``{"env": {"PYTHONHASHSEED": "bogus"}}`` (or ``PYTHONMALLOC``) made the hook's
interpreter exit 1 at startup, and a ``.pth`` file in the agent HOME's user
site made it exit 0. The CLI ran the blocked command both times. Isolated
mode (``-I``) ignores PYTHON* variables and the user site directory.

These tests read the generated settings content, then run the generated hook
command through a shell, the way the CLI does, under the hostile environments
of the review. Each hostile case has a control that runs the hook the old way
(no ``-I``) and shows that the plant really does open the gate there.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from equipa import agent_runner
from equipa.agent_runner import (
    PRETOOLUSE_HOOK_SCRIPT,
    SETTINGS_ENV_NEUTRALISED,
    _gate_canary_ok,
    _hook_interpreter,
    _pretooluse_hook_command,
    _pretooluse_settings_payload,
    build_cli_command,
)

GATE_ON = {"features": {"bash_security_pretooluse": True}}
BLOCKED_COMMAND = "echo ran > hk.txt; echo `id`"  # check 8 (command substitution)
SAFE_COMMAND = "ls -la"

# Values that make a Python started without -I exit 1 before any code runs
# (measured on CPython 3.12; the review used the first two with the real CLI).
STARTUP_KILLERS = {
    "PYTHONHASHSEED": "bogus",
    "PYTHONMALLOC": "bogus",
    "PYTHONPLATLIBDIR": "bogus",
    "PYTHONIOENCODING": "bogus:x",
    "PYTHONUTF8": "bogus",
    "PYTHONINTMAXSTRDIGITS": "bogus",
    "PYTHONTRACEMALLOC": "bogus",
    "PYTHONHOME": "/nonexistent-python-home",
}


def _payload(command: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": command}})


def _generated_settings() -> dict:
    with build_cli_command("PROMPT", project_dir="/tmp", max_turns=5,
                           model="sonnet", role="developer",
                           dispatch_config=GATE_ON) as cmd:
        path = cmd[cmd.index("--settings") + 1]
        return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated_settings() -> dict:
    return _generated_settings()


@pytest.fixture(scope="module")
def hook_command(generated_settings) -> str:
    return generated_settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _old_hook_command() -> str:
    """The pre-3144 command: no -I."""
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(PRETOOLUSE_HOOK_SCRIPT))}"


def _run_hook(command: str, bash_command: str, env: dict[str, str],
              cwd: Path) -> subprocess.CompletedProcess:
    """Run the hook command through a shell, as the CLI does."""
    return subprocess.run(["/bin/sh", "-c", command], input=_payload(bash_command),
                          capture_output=True, text=True, env=env, cwd=cwd,
                          timeout=60)


def _base_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}


def _plant_user_site_pth(home: Path) -> Path:
    """A .pth in HOME's user site that exits 0 whenever the gate runs."""
    site_dir = (home / ".local" / "lib"
                / f"python{sys.version_info[0]}.{sys.version_info[1]}"
                / "site-packages")
    site_dir.mkdir(parents=True)
    plant = site_dir / "zz_rr3138.pth"
    plant.write_text(
        'import os, sys; os._exit(0) if any("pretooluse_bash_gate" in a '
        'for a in sys.argv) else None\n', encoding="utf-8")
    return plant


# --- the generated settings content ------------------------------------------

def test_generated_hook_command_is_absolute_python_isolated_absolute_hook(
        hook_command):
    argv = shlex.split(hook_command)
    assert argv == [_hook_interpreter(), "-I", str(PRETOOLUSE_HOOK_SCRIPT)]
    assert os.path.isabs(argv[0])
    assert os.path.isabs(argv[2])


def test_generated_settings_keep_the_pin_and_the_env_block(generated_settings):
    assert generated_settings["disableAllHooks"] is False
    env = generated_settings["env"]
    assert set(env) == set(SETTINGS_ENV_NEUTRALISED)
    assert set(env.values()) == {""}


@pytest.mark.parametrize("name", [
    "PYTHONHASHSEED", "PYTHONMALLOC", "PYTHONPATH", "PYTHONHOME",
    "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONPLATLIBDIR", "PYTHONIOENCODING",
    "PYTHONUTF8", "PYTHONINTMAXSTRDIGITS", "PYTHONTRACEMALLOC",
    "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE",
])
def test_hook_env_names_are_emptied_in_the_settings_env(name, generated_settings):
    assert generated_settings["env"][name] == ""


def test_payload_refuses_a_bare_interpreter_name():
    """A bare python3 would be looked up on the agent's PATH."""
    with pytest.raises(ValueError, match="absolute"):
        _pretooluse_settings_payload(PRETOOLUSE_HOOK_SCRIPT, "python3")


def test_payload_refuses_a_relative_hook_path():
    with pytest.raises(ValueError, match="absolute"):
        _pretooluse_settings_payload("hooks/pretooluse_bash_gate.py",
                                     sys.executable)


def test_hook_interpreter_falls_back_to_python3_on_path(monkeypatch, tmp_path):
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(agent_runner.sys, "executable", "")
    monkeypatch.setattr(agent_runner.shutil, "which",
                        lambda name, *a, **k: str(fake) if name == "python3" else None)
    assert _hook_interpreter() == str(fake)


def test_no_interpreter_means_no_gate_settings(monkeypatch):
    """No absolute interpreter: no hook is wired (the reactive check then
    kills on sight), rather than a bare ``python3`` hook."""
    monkeypatch.setattr(agent_runner.sys, "executable", "")
    monkeypatch.setattr(agent_runner.shutil, "which", lambda *a, **k: None)
    with build_cli_command("PROMPT", project_dir="/tmp", max_turns=5,
                           model="sonnet", role="developer",
                           dispatch_config=GATE_ON) as cmd:
        assert "--settings" not in cmd
        assert _pretooluse_hook_command(cmd) is None


def test_reactive_check_recognises_the_isolated_hook(tmp_path, generated_settings,
                                                     hook_command):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(generated_settings), encoding="utf-8")
    assert _pretooluse_hook_command(
        ["claude", "--settings", str(settings)]) == hook_command
    assert _gate_canary_ok(hook_command, cwd=str(tmp_path))


def test_reactive_check_rejects_the_old_command_shape(tmp_path):
    """A settings file whose hook lacks -I is not the gate EQUIPA wires."""
    payload = _generated_settings()
    payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = _old_hook_command()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    assert _pretooluse_hook_command(["claude", "--settings", str(settings)]) is None


# --- the generated command under hostile environments ------------------------

@pytest.mark.parametrize("name", sorted(STARTUP_KILLERS))
def test_hostile_python_env_cannot_open_the_gate(name, hook_command, tmp_path):
    env = {**_base_env(tmp_path), name: STARTUP_KILLERS[name]}

    control = _run_hook(_old_hook_command(), BLOCKED_COMMAND, env, tmp_path)
    assert control.returncode not in (0, 2), (
        f"control: {name} should kill a Python run without -I")

    blocked = _run_hook(hook_command, BLOCKED_COMMAND, env, tmp_path)
    assert blocked.returncode == 2, blocked.stderr
    assert "BLOCKED" in blocked.stderr
    assert _run_hook(hook_command, SAFE_COMMAND, env, tmp_path).returncode == 0


def test_planted_user_site_pth_cannot_open_the_gate(hook_command, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _plant_user_site_pth(home)
    env = _base_env(home)

    control = _run_hook(_old_hook_command(), BLOCKED_COMMAND, env, tmp_path)
    assert control.returncode == 0, "control: the .pth opens a gate run without -I"

    blocked = _run_hook(hook_command, BLOCKED_COMMAND, env, tmp_path)
    assert blocked.returncode == 2, blocked.stderr
    assert _run_hook(hook_command, SAFE_COMMAND, env, tmp_path).returncode == 0


def test_everything_hostile_at_once(hook_command, tmp_path):
    """The planted .pth plus every startup killer plus a hostile
    PYTHONPATH/PYTHONSTARTUP/PYTHONUSERBASE: still exit 2 and 0."""
    home = tmp_path / "home"
    home.mkdir()
    plant = _plant_user_site_pth(home)
    startup = tmp_path / "startup.py"
    startup.write_text("import os; os._exit(0)\n", encoding="utf-8")
    env = {**_base_env(home), **STARTUP_KILLERS,
           "PYTHONPATH": str(plant.parent), "PYTHONSTARTUP": str(startup),
           "PYTHONUSERBASE": str(home / ".local"), "PYTHONINSPECT": "1"}
    blocked = _run_hook(hook_command, BLOCKED_COMMAND, env, tmp_path)
    assert blocked.returncode == 2, blocked.stderr
    assert _run_hook(hook_command, SAFE_COMMAND, env, tmp_path).returncode == 0
