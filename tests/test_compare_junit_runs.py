#!/usr/bin/env python3
"""scripts/compare_junit_runs.py: a parallel run must match the serial run.

Task 3160. The script is the proof that `pytest -n auto` runs the same tests
with the same outcomes as a serial run, so it must catch every kind of
difference: a test missing on one side, a changed outcome, a test reported
twice, and a skipped test.

Copyright 2026 Forgeborn
"""

import subprocess
import sys
from pathlib import Path

import pytest

import compare_junit_runs

SCRIPT = Path(compare_junit_runs.__file__)


def _case(test_id: str, outcome: str = "passed") -> str:
    classname, name = test_id.split("::")
    child = {"passed": "",
             "failed": '<failure message="assert 0">trace</failure>',
             "error": '<error message="boom">trace</error>',
             "skipped": '<skipped type="pytest.skip" message="no">x</skipped>',
             "failed+error": ('<failure message="a">t</failure>'
                              '<error message="b">t</error>')}[outcome]
    return (f'<testcase classname="{classname}" name="{name}" time="0.1">'
            f"{child}</testcase>")


def _report(path: Path, *cases: str) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        '<testsuite name="pytest" tests="0">' + "".join(cases)
        + "</testsuite></testsuites>")
    return path


def _run(*args: str) -> tuple[int, str]:
    result = subprocess.run([sys.executable, str(SCRIPT), *args],
                            capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout + result.stderr


BASE = ("tests.test_a::test_one", "tests.test_a::test_two[x-1]",
        "tests.test_b.TestK::test_three")


def test_identical_runs_split_across_files_match(tmp_path):
    serial_1 = _report(tmp_path / "s1.xml", _case(BASE[0]), _case(BASE[1]))
    serial_2 = _report(tmp_path / "s2.xml", _case(BASE[2], "failed"))
    parallel = _report(tmp_path / "p.xml", _case(BASE[2], "failed"),
                       _case(BASE[1]), _case(BASE[0]))

    code, output = _run("--serial", str(serial_1), str(serial_2),
                        "--parallel", str(parallel))

    assert code == 0, output
    assert "serial: 2 passed, 1 failed, 0 error, 0 skipped, total 3" in output
    assert "parallel: 2 passed, 1 failed, 0 error, 0 skipped, total 3" in output
    assert "IDENTICAL: 3 test ids" in output


@pytest.mark.parametrize("parallel_cases, expected", [
    ((BASE[0], BASE[1]), "tests.test_b.TestK::test_three: serial passed, "
                         "parallel absent"),
    ((*BASE, "tests.test_c::test_new"), "tests.test_c::test_new: serial "
                                        "absent, parallel passed"),
    ((*BASE, BASE[0]), "tests.test_a::test_one: serial passed, "
                       "parallel passed/passed"),
])
def test_a_missing_extra_or_repeated_test_is_a_difference(tmp_path,
                                                          parallel_cases,
                                                          expected):
    serial = _report(tmp_path / "s.xml", *(_case(i) for i in BASE))
    parallel = _report(tmp_path / "p.xml", *(_case(i) for i in parallel_cases))

    code, output = _run("--serial", str(serial), "--parallel", str(parallel))

    assert code == 1, output
    assert "DIFFERENT: 1 test id(s) differ" in output
    assert expected in output


@pytest.mark.parametrize("outcome", ["failed", "error", "failed+error"])
def test_a_changed_outcome_is_a_difference(tmp_path, outcome):
    serial = _report(tmp_path / "s.xml", *(_case(i) for i in BASE))
    parallel = _report(tmp_path / "p.xml", _case(BASE[0]), _case(BASE[1]),
                       _case(BASE[2], outcome))

    code, output = _run("--serial", str(serial), "--parallel", str(parallel))

    assert code == 1, output
    assert (f"tests.test_b.TestK::test_three: serial passed, "
            f"parallel {outcome}") in output


def test_a_skipped_test_fails_even_when_both_runs_skip_it(tmp_path):
    cases = (_case(BASE[0]), _case(BASE[1], "skipped"))
    serial = _report(tmp_path / "s.xml", *cases)
    parallel = _report(tmp_path / "p.xml", *cases)

    code, output = _run("--serial", str(serial), "--parallel", str(parallel))
    assert code == 1, output
    assert "SKIPPED: 1 test(s) skipped in the serial run" in output
    assert "SKIPPED: 1 test(s) skipped in the parallel run" in output

    code, output = _run("--serial", str(serial), "--parallel", str(parallel),
                        "--allow-skipped")
    assert code == 0, output
    assert "1 skipped, total 2" in output


@pytest.mark.parametrize("content", [
    None,
    "<testsuites><testsuite>",
    "<html><body>not junit</body></html>",
    '<testsuites><testsuite name="pytest"></testsuite></testsuites>',
])
def test_an_unreadable_report_is_an_error_not_a_match(tmp_path, content):
    good = _report(tmp_path / "good.xml", _case(BASE[0]))
    bad = tmp_path / "bad.xml"
    if content is not None:
        bad.write_text(content)

    code, output = _run("--serial", str(good), "--parallel", str(bad))

    assert code == 2, output
    assert f"error: {bad}" in output
