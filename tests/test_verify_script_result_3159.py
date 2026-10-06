#!/usr/bin/env python3
"""Task 3159: scripts/verify_agent_isolation.sh prints exactly one RESULT.

R5 of the 3156 review: when the probe passed but an orchestrator-side
firewall check failed, the output held the probe's "RESULT: PASS" and then
the script's "RESULT: FAIL (...)". The last line and the exit status were
right, the earlier line was misleading. The probe's RESULT line is now held
back and folded into one final line.

The script runs end to end with a fake sudo (the loaded nftables table), a
fake ip and a fake python standing in for ``python3 -m equipa.isolation``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_agent_isolation_3156 import (
    OLD_RULE_LISTING,
    VERIFY_SCRIPT,
    _fake_bin,
    _runbook_table,
    _with_lan6_elements,
)

CURRENT_TABLE = f"cat <<'EOF'\n{_with_lan6_elements(_runbook_table())}\nEOF\n"
OLD_TABLE = f"cat <<'EOF'\n{OLD_RULE_LISTING}EOF\n"


def _run(tmp_path: Path, table: str, probe: str) -> tuple[int, list[str]]:
    env = _fake_bin(tmp_path, {
        "sudo": table,
        "ip": "exit 0\n",
        "id": "echo 1000\n",
        "probe-python": probe,
    })
    env["EQUIPA_PYTHON"] = str(tmp_path / "fakebin" / "probe-python")
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT)], capture_output=True, text=True,
        timeout=60, env={**os.environ, **env})
    return result.returncode, result.stdout.splitlines()


def _result_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("RESULT:")]


@pytest.mark.parametrize("table, probe, status, result", [
    # the review's case: the probe passes, the loaded rule is an old one
    (OLD_TABLE, 'echo "PASS probe"\necho "RESULT: PASS"\nexit 0\n', 1,
     "RESULT: FAIL (4 orchestrator-side firewall rule check(s) failed)"),
    # both fail: one line names both
    (OLD_TABLE, 'echo "FAIL probe"\necho "RESULT: FAIL (2 failed)"\nexit 1\n',
     1, "RESULT: FAIL (2 failed; 4 orchestrator-side firewall rule check(s) "
        "failed)"),
    # only the probe fails
    (CURRENT_TABLE,
     'echo "FAIL probe"\necho "RESULT: FAIL (2 failed)"\nexit 1\n', 1,
     "RESULT: FAIL (2 failed)"),
    # everything passes
    (CURRENT_TABLE, 'echo "PASS probe"\necho "RESULT: PASS"\nexit 0\n', 0,
     "RESULT: PASS"),
    # isolation could not be established: no verdict from the probe at all
    (CURRENT_TABLE, 'echo "FAIL isolation could not be established"\nexit 2\n',
     2, "RESULT: FAIL (the isolation check exited 2 without a verdict)"),
    # a passing probe line but a failing exit status is not a pass
    (CURRENT_TABLE, 'echo "FAIL outer check"\necho "RESULT: PASS"\nexit 1\n',
     1, "RESULT: FAIL (the isolation check exited 1)"),
], ids=["probe-pass-firewall-fail", "both-fail", "probe-fail",
        "all-pass", "no-verdict", "pass-line-fail-status"])
def test_the_script_prints_exactly_one_result_line(tmp_path, table, probe,
                                                   status, result):
    returncode, lines = _run(tmp_path, table, probe)

    assert returncode == status, lines
    assert _result_lines(lines) == [result], lines
    assert lines[-1] == result


def test_the_probe_output_is_kept_in_order(tmp_path):
    probe = ('echo "PASS one"\necho "FAIL two"\necho "NOTE three"\n'
             'echo "RESULT: FAIL (1 failed)"\nexit 1\n')

    _returncode, lines = _run(tmp_path, CURRENT_TABLE, probe)

    assert lines[-4:] == ["PASS one", "FAIL two", "NOTE three",
                          "RESULT: FAIL (1 failed)"]


def test_a_last_probe_line_without_a_newline_is_kept(tmp_path):
    probe = 'printf "PASS last"\nexit 1\n'

    _returncode, lines = _run(tmp_path, CURRENT_TABLE, probe)

    assert "PASS last" in lines
    assert _result_lines(lines) == [
        "RESULT: FAIL (the isolation check exited 1 without a verdict)"]
