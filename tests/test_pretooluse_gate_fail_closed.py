"""Fail-closed contract of the PreToolUse Bash gate (task 3121, sandbox-06/18).

Claude Code treats a PreToolUse hook exit code other than 2 as a NON-blocking
error and runs the command anyway. Before task 3121 the gate exited 0 when the
checker could not be loaded and exited 1 (uncaught exception) when the checker
raised or had a SyntaxError, so every infrastructure failure let the command
through. Each test here drives one of those paths through the real hook script
in a throwaway repo layout (``<root>/hooks/`` + ``<root>/equipa/``) and asserts
exit 2 with a reason on stderr.

The config half covers the dispatch-config side: a corrupt or unreadable
dispatch_config.json must not silently switch the gate off, and feature flags
are coerced strictly.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from equipa.agent_runner import build_cli_command
from equipa.config import (
    CONFIG_LOAD_ERROR_KEY,
    DEFAULT_FEATURE_FLAGS,
    is_feature_enabled,
    load_dispatch_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "hooks" / "pretooluse_bash_gate.py"
REAL_CHECKER = REPO_ROOT / "equipa" / "bash_security.py"

SAFE_COMMAND = "ls -la"


def _bash_payload(command: object) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "test"},
        }
    )


def _make_gate_root(tmp_path: Path, checker_source: str | None) -> Path:
    """Build ``<root>/hooks/<gate copy>`` and ``<root>/equipa/bash_security.py``.

    The hook resolves the checker relative to its own ``__file__``, so a copy
    in a temp root loads the temp checker. ``equipa/__init__.py`` is created
    so the hook's package-import fallback also resolves inside the temp root
    and can never pick up the real, working checker by accident.
    """
    root = tmp_path / "gate_root"
    (root / "hooks").mkdir(parents=True)
    (root / "equipa").mkdir()
    (root / "equipa" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(HOOK_SCRIPT, root / "hooks" / "pretooluse_bash_gate.py")
    if checker_source is not None:
        (root / "equipa" / "bash_security.py").write_text(
            checker_source, encoding="utf-8"
        )
    return root


def _run_gate(root: Path, stdin_text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "hooks" / "pretooluse_bash_gate.py")],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# Hook: every failure path exits 2 (never 0, never 1)
# ---------------------------------------------------------------------------

BROKEN_CHECKERS = {
    # Runtime exception inside check_bash_command: was exit 1 (allowed).
    "raises_at_runtime": (
        "def check_bash_command(command):\n"
        "    raise RuntimeError('checker exploded')\n",
        "RuntimeError",
    ),
    # A bad deploy with a syntax error: exec_module raised SyntaxError, which
    # the old gate did not catch -> exit 1 (allowed).
    "syntax_error": (
        "def check_bash_command(command) return None\n",
        "SyntaxError",
    ),
    # The module loads but has no checker, and the package fallback fails
    # too: was exit 0 ("allowing command").
    "missing_symbol": (
        "SOMETHING_ELSE = 1\n",
        "ImportError",
    ),
    # A checker returning a non-result: getattr(None, 'safe', True) allowed.
    "returns_none": (
        "def check_bash_command(command):\n"
        "    return None\n",
        "unusable result",
    ),
    # A checker whose 'safe' is truthy but not True must not allow.
    "returns_truthy_string": (
        "class R:\n"
        "    safe = 'false'\n"
        "def check_bash_command(command):\n"
        "    return R()\n",
        "unusable result",
    ),
    # SystemExit(0) raised by the checker must not become exit 0.
    "calls_sys_exit_zero": (
        "import sys\n"
        "def check_bash_command(command):\n"
        "    sys.exit(0)\n",
        "SystemExit",
    ),
    # SystemExit raised while importing the checker module.
    "exits_at_import": (
        "import sys\n"
        "sys.exit(0)\n",
        "SystemExit",
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN_CHECKERS))
def test_broken_checker_blocks_with_reason(tmp_path: Path, case: str):
    source, reason_marker = BROKEN_CHECKERS[case]
    root = _make_gate_root(tmp_path, source)
    result = _run_gate(root, _bash_payload(SAFE_COMMAND))
    assert result.returncode == 2, (
        f"{case}: gate must fail closed with exit 2, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert reason_marker in result.stderr, result.stderr
    assert "fails closed" in result.stderr
    # The misleading fail-open message is gone for good.
    assert "reactive stream check remains active" not in result.stderr


def test_missing_checker_file_blocks(tmp_path: Path):
    """No bash_security.py at all: the checker cannot be loaded -> exit 2."""
    root = _make_gate_root(tmp_path, None)
    result = _run_gate(root, _bash_payload(SAFE_COMMAND))
    assert result.returncode == 2, result.stderr
    assert "could not load the bash security checker" in result.stderr


def test_working_checker_copy_still_allows_and_blocks(tmp_path: Path):
    """Positive control: the temp-root harness with the REAL checker behaves
    like production, so the failures above are caused by the broken checker,
    not by the harness."""
    root = _make_gate_root(tmp_path, REAL_CHECKER.read_text(encoding="utf-8"))
    assert _run_gate(root, _bash_payload(SAFE_COMMAND)).returncode == 0
    blocked = _run_gate(root, _bash_payload("echo `id`"))
    assert blocked.returncode == 2
    assert "BashSecurity check 8" in blocked.stderr


@pytest.mark.parametrize(
    "payload",
    [
        _bash_payload(123),                                   # command not a string
        _bash_payload(None),                                  # command null
        json.dumps({"tool_name": "Bash"}),                    # no tool_input
        json.dumps({"tool_name": "Bash", "tool_input": "ls"}),  # tool_input not object
        json.dumps({"tool_input": {"command": "ls"}}),        # no tool_name
        json.dumps({"tool_name": 7, "tool_input": {"command": "ls"}}),
    ],
)
def test_unparseable_bash_payload_blocks(payload: str):
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (payload, result.stderr)
    assert "fails closed" in result.stderr


def test_other_tools_and_empty_command_still_allowed():
    """Fail-closed must not brick non-Bash tools or an empty Bash command."""
    for payload in (
        json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}),
        _bash_payload(""),
    ):
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            cwd="/tmp",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (payload, result.stderr)


# ---------------------------------------------------------------------------
# dispatch_config: corrupt / unreadable config keeps the gate ON
# ---------------------------------------------------------------------------

CORRUPT_CONFIGS = {
    "truncated_json": '{"features": {"bash_security_pretooluse": tr',
    "not_an_object": '["features"]',
    "invalid_utf8": b'{"features": {"bash_security_pretooluse": true}} \xff\xfe',
}


def _write_config(path: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("case", sorted(CORRUPT_CONFIGS))
def test_corrupt_config_marks_load_error_and_forces_gate_on(
    tmp_path: Path, case: str, caplog: pytest.LogCaptureFixture
):
    config_path = tmp_path / "dispatch_config.json"
    _write_config(config_path, CORRUPT_CONFIGS[case])

    with caplog.at_level(logging.ERROR, logger="equipa.config"):
        config = load_dispatch_config(config_path)
        enabled = is_feature_enabled(config, "bash_security_pretooluse")

    assert config.get(CONFIG_LOAD_ERROR_KEY), config
    assert enabled is True
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Could not load dispatch config" in r.getMessage() for r in errors)
    assert any("fail-closed" in r.getMessage() for r in errors)


def test_unreadable_config_forces_gate_on(tmp_path: Path):
    """A config path that exists but cannot be read (a directory here, which
    raises IsADirectoryError without depending on file permissions or uid)."""
    config_path = tmp_path / "dispatch_config.json"
    config_path.mkdir()
    config = load_dispatch_config(config_path)
    assert config.get(CONFIG_LOAD_ERROR_KEY)
    assert is_feature_enabled(config, "bash_security_pretooluse") is True


def test_corrupt_config_wires_the_hook_into_the_cli(tmp_path: Path):
    """End to end: the argv built from a corrupt config carries --settings."""
    config_path = tmp_path / "dispatch_config.json"
    _write_config(config_path, CORRUPT_CONFIGS["truncated_json"])
    config = load_dispatch_config(config_path)
    with build_cli_command(
        "PROMPT",
        project_dir="/tmp",
        max_turns=10,
        model="sonnet",
        role="developer",
        dispatch_config=config,
    ) as cmd:
        assert "--settings" in cmd


def test_fail_closed_applies_only_to_security_gates(tmp_path: Path):
    """Non-security flags keep their defaults on a load error."""
    config_path = tmp_path / "dispatch_config.json"
    _write_config(config_path, CORRUPT_CONFIGS["truncated_json"])
    config = load_dispatch_config(config_path)
    assert is_feature_enabled(config, "hooks") is DEFAULT_FEATURE_FLAGS["hooks"]
    assert is_feature_enabled(config, "vector_memory") is False


def test_valid_config_can_still_turn_the_gate_off(tmp_path: Path):
    config_path = tmp_path / "dispatch_config.json"
    _write_config(
        config_path, json.dumps({"features": {"bash_security_pretooluse": False}})
    )
    config = load_dispatch_config(config_path)
    assert CONFIG_LOAD_ERROR_KEY not in config
    assert is_feature_enabled(config, "bash_security_pretooluse") is False


# ---------------------------------------------------------------------------
# sandbox-18: strict flag coercion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        (" TRUE ", True),
        ("False", False),
        # JSON integers 0/1 (BS3121-02): main treated 1 as ON.
        (1, True),
        (0, False),
    ],
)
def test_flag_coercion_accepts_documented_values(value: object, expected: bool):
    config = {"features": {"bash_security_pretooluse": value}}
    assert is_feature_enabled(config, "bash_security_pretooluse") is expected
    # And in the other direction for a default-ON flag.
    config = {"features": {"security_review": value}}
    assert is_feature_enabled(config, "security_review") is expected
    # And for an ordinary default-OFF flag.
    config = {"features": {"hooks": value}}
    assert is_feature_enabled(config, "hooks") is expected


INVALID_FLAG_VALUES = [
    "yes", "no", "off", "on", "", "enabled", 2, -1, None, [], {}, 1.0, 0.0,
]


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES)
def test_invalid_value_uses_default_for_ordinary_flags(
    value: object, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        # Default OFF flag: the string "false" used to be truthy -> ON.
        off_default = is_feature_enabled({"features": {"hooks": value}}, "hooks")
        on_default = is_feature_enabled(
            {"features": {"security_review": value}}, "security_review"
        )
    assert off_default is DEFAULT_FEATURE_FLAGS["hooks"]
    assert on_default is DEFAULT_FEATURE_FLAGS["security_review"]
    assert sum("invalid value" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES)
def test_invalid_value_forces_security_gate_on(
    value: object, caplog: pytest.LogCaptureFixture
):
    """BS3121-02: an unrecognised value for a FAIL_CLOSED flag is treated like
    a load error - ERROR logged, gate ON - never like "use the default OFF"."""
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        enabled = is_feature_enabled(
            {"features": {"bash_security_pretooluse": value}},
            "bash_security_pretooluse",
        )
    assert enabled is True
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "fail-closed" in r.getMessage() and "bash_security_pretooluse" in r.getMessage()
        for r in errors
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("value", [1, "yes", "on", "ON", " Yes "])
def test_operator_spellings_of_on_keep_the_gate_on(value: object):
    """The review's probe: 1, "yes" and "on" used to turn the gate OFF."""
    config = {"features": {"bash_security_pretooluse": value}}
    assert is_feature_enabled(config, "bash_security_pretooluse") is True


def test_bool_is_not_mistaken_for_an_integer():
    """True/False are ints in Python; they keep their JSON-boolean meaning."""
    assert is_feature_enabled({"features": {"hooks": True}}, "hooks") is True
    assert is_feature_enabled(
        {"features": {"bash_security_pretooluse": False}}, "bash_security_pretooluse"
    ) is False


def test_features_not_a_dict_uses_defaults(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        assert is_feature_enabled({"features": ["hooks"]}, "hooks") is False
        assert is_feature_enabled({"features": "x"}, "security_review") is True
    assert any("not a dict" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("features", [None, "x", ["bash_security_pretooluse"], 1])
def test_features_not_a_dict_forces_security_gate_on(
    features: object, caplog: pytest.LogCaptureFixture
):
    """BS3121-02: ``"features": null`` must not switch the gate off."""
    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        enabled = is_feature_enabled(
            {"features": features}, "bash_security_pretooluse"
        )
    assert enabled is True
    assert any(
        r.levelno >= logging.ERROR and "fail-closed" in r.getMessage()
        for r in caplog.records
    )
