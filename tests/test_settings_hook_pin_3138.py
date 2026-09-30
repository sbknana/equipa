#!/usr/bin/env python3
"""Task 3138 RR-03: the --settings file EQUIPA passes keeps the gate on.

Agents run with ``--setting-sources user``, so the CLI still loads the
agent-writable ``~/.claude/settings.json``. The independent review showed
with the real CLI (2.1.280) that ``{"disableAllHooks": true}`` there turned
the PreToolUse Bash gate off, and that ``"disableAllHooks": false`` in the
flag-scope --settings file beats it. The CLI applies each source's ``env``
block in source order (user, then flag), and an ``env`` block there can
also turn hooks off (``CLAUDE_CODE_SAFE_MODE``, ``CLAUDE_CODE_SIMPLE``) or
run code around every command (``BASH_ENV``, ``LD_PRELOAD``,
``CLAUDE_CODE_SHELL_PREFIX``, ...). The flag file sets those to empty.

These tests read the generated settings content; no network and no CLI.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from equipa.agent_runner import (
    PRETOOLUSE_HOOK_SCRIPT,
    SETTINGS_ENV_NEUTRALISED,
    _pretooluse_hook_command,
    _pretooluse_settings_payload,
    build_cli_command,
)

GATE_ON = {"features": {"bash_security_pretooluse": True}}

# What a planted user-scope ~/.claude/settings.json can hold.
PLANTED_USER_SETTINGS = {
    "disableAllHooks": True,
    "env": {
        "BASH_ENV": "/tmp/planted-bash-env.sh",
        "ENV": "/tmp/planted-sh-env.sh",
        "PROMPT_COMMAND": "planted",
        "LD_PRELOAD": "/tmp/planted.so",
        "NODE_OPTIONS": "--require /tmp/planted.js",
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_SHELL_PREFIX": "/tmp/planted-prefix",
        "CLAUDE_CODE_SHELL": "/tmp/planted/bash",
        "CLAUDE_ENV_FILE": "/tmp/planted-env-file",
    },
}


def _truthy(value: str | None) -> bool:
    """The CLI's env switch parse (``Me`` in the 2.1.280 bundle)."""
    return bool(value) and value.strip().lower() in ("1", "true", "yes", "on")


def _effective(user: dict, flag: dict) -> tuple[bool, dict[str, str]]:
    """The CLI's merge for these keys: env blocks applied user then flag
    (later wins per name); disableAllHooks from the merged settings."""
    env: dict[str, str] = {}
    for source in (user, flag):
        env.update(source.get("env", {}))
    merged = {**user, **{k: v for k, v in flag.items() if k != "env"}}
    return merged.get("disableAllHooks") is True, env


@pytest.fixture
def generated_settings() -> dict:
    """The settings file build_cli_command writes with the gate on."""
    with build_cli_command("PROMPT", project_dir="/tmp", max_turns=5,
                           model="sonnet", role="developer",
                           dispatch_config=GATE_ON) as cmd:
        path = cmd[cmd.index("--settings") + 1]
        return json.loads(Path(path).read_text(encoding="utf-8"))


def test_generated_settings_pin_disable_all_hooks_false(generated_settings):
    assert generated_settings["disableAllHooks"] is False


def test_generated_settings_empty_every_neutralised_name(generated_settings):
    env = generated_settings["env"]
    assert set(env) == set(SETTINGS_ENV_NEUTRALISED)
    assert all(value == "" for value in env.values())


@pytest.mark.parametrize("name", [
    "BASH_ENV", "ENV", "PROMPT_COMMAND", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "SHELLOPTS", "PS4", "NODE_OPTIONS", "PYTHONPATH",
    "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SHELL_PREFIX",
    "CLAUDE_CODE_SHELL", "CLAUDE_ENV_FILE",
])
def test_the_review_and_cli_switch_names_are_neutralised(name, generated_settings):
    assert generated_settings["env"][name] == ""


def test_generated_settings_still_wire_the_gate(generated_settings):
    entry = generated_settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert "pretooluse_bash_gate.py" in entry["hooks"][0]["command"]


def test_payload_never_names_a_value_to_keep():
    """The pins are empty strings only: no EQUIPA path or credential is
    written to the temp settings file."""
    payload = _pretooluse_settings_payload("/hooks/gate.py", "/py/bin")
    assert set(payload) == {"disableAllHooks", "env", "hooks"}
    assert set(payload["env"].values()) == {""}


def test_planted_user_scope_is_overridden_by_the_flag_file(generated_settings):
    hooks_disabled, env = _effective(PLANTED_USER_SETTINGS, generated_settings)
    assert hooks_disabled is False
    for name in PLANTED_USER_SETTINGS["env"]:
        assert env[name] == "", name
    assert not _truthy(env["CLAUDE_CODE_SAFE_MODE"])
    assert not _truthy(env["CLAUDE_CODE_SIMPLE"])


def test_without_the_pin_the_planted_user_scope_wins():
    """Control: the hooks-only payload of before RR-03 left both open."""
    hooks_only = {"hooks": _pretooluse_settings_payload(
        PRETOOLUSE_HOOK_SCRIPT, sys.executable)["hooks"]}
    hooks_disabled, env = _effective(PLANTED_USER_SETTINGS, hooks_only)
    assert hooks_disabled is True
    assert _truthy(env["CLAUDE_CODE_SAFE_MODE"])


def test_empty_bash_env_runs_nothing(tmp_path):
    """"" is inert where a planted value runs code: bash sources BASH_ENV."""
    marker = tmp_path / "ran"
    planted = tmp_path / "planted.sh"
    planted.write_text(f"touch {marker}\n", encoding="utf-8")
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    subprocess.run(["bash", "-c", "true"], check=True,
                   env={**base_env, "BASH_ENV": str(planted)})
    assert marker.exists(), "control: a planted BASH_ENV runs"
    marker.unlink()

    subprocess.run(["bash", "-c", "true"], check=True,
                   env={**base_env, "BASH_ENV": ""})
    assert not marker.exists()


def test_the_reactive_check_still_recognises_the_pinned_hook(tmp_path):
    """_pretooluse_hook_command reads the same file and must still find the
    gate, or every refusal would kill the agent (sandbox-04)."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(_pretooluse_settings_payload(
        PRETOOLUSE_HOOK_SCRIPT, sys.executable or "python3")), encoding="utf-8")
    assert _pretooluse_hook_command(
        ["claude", "--settings", str(settings)]) is not None
