#!/usr/bin/env python3
"""Task 3204: the suite's bash scripts run as on a real host.

The Claude CLI's Bash tool exports SHELLOPTS with "onecmd", which made every
bash a test started exit after its first command (32 tester failures in
scripts/verify_agent_isolation.sh and the 3183 script-default tests).
tests/conftest.py drops SHELLOPTS and BASHOPTS before any test runs.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess

import pytest


@pytest.mark.parametrize("name", ["SHELLOPTS", "BASHOPTS"])
def test_no_inherited_bash_options_reach_the_tests(name):
    assert name not in os.environ


def test_a_child_bash_runs_every_line_of_a_script(tmp_path):
    script = tmp_path / "three_lines.sh"
    script.write_text("echo one\necho two\necho three\n")
    result = subprocess.run(["bash", str(script)], capture_output=True,
                            text=True, timeout=30, env=dict(os.environ))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["one", "two", "three"]


def test_an_exported_onecmd_is_what_cut_the_scripts_short(tmp_path):
    """Control: with SHELLOPTS=onecmd exported, bash stops after one line,
    so the test above would catch the scrub going missing."""
    script = tmp_path / "three_lines.sh"
    script.write_text("echo one\necho two\necho three\n")
    environment = {**os.environ, "SHELLOPTS": "braceexpand:hashall:onecmd"}
    result = subprocess.run(["bash", str(script)], capture_output=True,
                            text=True, timeout=30, env=environment)
    assert result.stdout.splitlines() != ["one", "two", "three"], (
        result.stdout, script.read_text())
