#!/usr/bin/env python3
"""Prove two pytest runs collected the same tests with the same outcomes.

Task 3160 moved the suite to pytest-xdist (``-n auto --dist loadfile``).
This compares the ``--junitxml`` reports of a serial run and a parallel run:
the set of test ids and each test's outcome must be identical, so the
parallel run is a drop-in replacement for the serial one.

Either side may be several report files (a serial run too long for one
command can be split into parts); their test cases are pooled. A test id
reported twice on one side counts twice, so a test that ran in two parts
is a difference, not a match.

Outcomes are read from each ``<testcase>``: ``failure`` -> failed,
``error`` -> error, ``skipped`` -> skipped (xfail included), otherwise
passed. A test with a call failure and a teardown error keeps both.

Usage::

    python3 scripts/compare_junit_runs.py \\
        --serial serial-1.xml serial-2.xml --parallel parallel.xml

Exit codes:
    0  identical test ids and outcomes, and no skipped test (unless
       ``--allow-skipped``)
    1  the runs differ, or a test was skipped
    2  a report is missing or not a pytest junitxml file

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# <testcase> child tag -> outcome, in the order a combined outcome lists them.
_OUTCOME_TAGS = (("failure", "failed"), ("error", "error"),
                 ("skipped", "skipped"))
_SUMMARY_ORDER = ("passed", "failed", "error", "skipped")

# How many differing test ids to print before summarising the rest.
_MAX_LISTED = 50


class ReportError(Exception):
    """A report file cannot be read as a pytest junitxml report."""


@dataclass
class RunResults:
    """Pooled outcomes of one side of the comparison."""

    label: str
    # (test id, outcome) -> how many times it was reported.
    cases: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.cases.values())

    def outcome_counts(self) -> Counter:
        counts: Counter = Counter()
        for (_test_id, outcome), times in self.cases.items():
            for part in outcome.split("+"):
                counts[part] += times
        return counts

    def summary(self) -> str:
        counts = self.outcome_counts()
        parts = [f"{counts.get(name, 0)} {name}" for name in _SUMMARY_ORDER]
        return f"{self.label}: {', '.join(parts)}, total {self.total}"


def _case_outcome(testcase: ElementTree.Element) -> str:
    outcomes = [outcome for tag, outcome in _OUTCOME_TAGS
                if testcase.find(tag) is not None]
    return "+".join(outcomes) if outcomes else "passed"


def _case_id(testcase: ElementTree.Element) -> str:
    classname = testcase.get("classname", "")
    name = testcase.get("name", "")
    return f"{classname}::{name}" if classname else name


def read_report(path: Path, into: RunResults) -> int:
    """Add every test case in the junitxml file *path* to *into*.

    Returns the number of test cases read. Raises ReportError for a
    missing, unparsable or non-junit file, or one with no test cases.
    """
    try:
        root = ElementTree.parse(path).getroot()
    except FileNotFoundError as err:
        raise ReportError(f"{path}: no such file") from err
    except (ElementTree.ParseError, OSError) as err:
        raise ReportError(f"{path}: cannot read junitxml: {err}") from err
    if root.tag not in ("testsuites", "testsuite"):
        raise ReportError(f"{path}: root element is <{root.tag}>, "
                          "not <testsuites> or <testsuite>")
    count = 0
    for testcase in root.iter("testcase"):
        into.cases[(_case_id(testcase), _case_outcome(testcase))] += 1
        count += 1
    if count == 0:
        raise ReportError(f"{path}: contains no test cases")
    return count


def compare(serial: RunResults, parallel: RunResults) -> list[str]:
    """Describe every difference between the two runs, one line each."""
    serial_by_id = _outcomes_by_id(serial)
    parallel_by_id = _outcomes_by_id(parallel)
    differences = []
    for test_id in sorted(serial_by_id.keys() | parallel_by_id.keys()):
        in_serial = serial_by_id.get(test_id, [])
        in_parallel = parallel_by_id.get(test_id, [])
        if in_serial != in_parallel:
            differences.append(
                f"{test_id}: {serial.label} {_describe(in_serial)}, "
                f"{parallel.label} {_describe(in_parallel)}")
    return differences


def _outcomes_by_id(results: RunResults) -> dict[str, list[str]]:
    by_id: dict[str, list[str]] = {}
    for (test_id, outcome), times in results.cases.items():
        by_id.setdefault(test_id, []).extend([outcome] * times)
    return {test_id: sorted(outcomes) for test_id, outcomes in by_id.items()}


def _describe(outcomes: list[str]) -> str:
    return "/".join(outcomes) if outcomes else "absent"


def _load(label: str, paths: list[Path]) -> RunResults:
    results = RunResults(label)
    for path in paths:
        read_report(path, results)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the junitxml reports of a serial and a parallel "
                    "pytest run: test ids and outcomes must be identical.")
    parser.add_argument("--serial", nargs="+", type=Path, required=True,
                        help="junitxml report(s) of the serial run")
    parser.add_argument("--parallel", nargs="+", type=Path, required=True,
                        help="junitxml report(s) of the parallel run")
    parser.add_argument("--allow-skipped", action="store_true",
                        help="do not fail when a test was skipped")
    args = parser.parse_args(argv)

    try:
        serial = _load("serial", args.serial)
        parallel = _load("parallel", args.parallel)
    except ReportError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    print(serial.summary())
    print(parallel.summary())

    failed = False
    differences = compare(serial, parallel)
    if differences:
        failed = True
        print(f"DIFFERENT: {len(differences)} test id(s) differ")
        for line in differences[:_MAX_LISTED]:
            print(f"  {line}")
        if len(differences) > _MAX_LISTED:
            print(f"  ... and {len(differences) - _MAX_LISTED} more")
    for results in (serial, parallel):
        skipped = results.outcome_counts().get("skipped", 0)
        if skipped and not args.allow_skipped:
            failed = True
            print(f"SKIPPED: {skipped} test(s) skipped in the "
                  f"{results.label} run")
    if failed:
        return 1
    print(f"IDENTICAL: {serial.total} test ids, same outcome in both runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
