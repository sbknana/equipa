#!/usr/bin/env python3
"""Tabulate a JUnit report of tests/test_worktree_poison_matrix_3158.py.

Task #3162 (FF-3158): the poison-vector matrix runs every planted vector
against every cleanup operation. This prints, per vector, what happened in
each operation:

* ``ok``   the test passed;
* ``RAN``  a planted program ran (the failure reads ``EXECUTED ...``);
* ``fail`` the test failed for another reason (a discovery call, the
  cleanup did not do its job, an API the tree lacks).

Usage::

    python3 -m pytest -q -p no:cacheprovider -n auto \\
        tests/test_worktree_poison_matrix_3158.py --junitxml=report.xml
    python3 scripts/poison_matrix_summary.py report.xml

Vector and operation names are read from the test module's source (``ast``),
so the script runs without importing EQUIPA.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from pathlib import Path

DEFAULT_MODULE = Path(__file__).resolve().parent.parent / "tests" / "test_worktree_poison_matrix_3158.py"
CLEANUP_TEST = "test_cleanup_runs_no_agent_program_and_does_its_job"
NO_LAZY_FETCH_TEST = "test_cleanup_starts_no_transport_on_a_git_without_lazy_fetch_control"
ISOLATED_TEST = "test_isolated_run_runs_no_agent_program_after_the_agent"
ISOLATED_OUTCOMES = ("success", "failure", "early-termination")
EXECUTED_RE = re.compile(r"EXECUTED (\S+)/(\S+): (\[[^\]]*\])")


def _dict_keys(tree: ast.Module, name: str) -> list[str]:
    """String keys of the module-level dict literal assigned to ``name``."""
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        )
        if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Dict):
            return [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
    raise SystemExit(f"no dict literal named {name} in the test module")


def _matrix_names(module: Path) -> tuple[list[str], list[str]]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return _dict_keys(tree, "VECTORS"), _dict_keys(tree, "OPERATIONS")


def _split_id(param_id: str, firsts: list[str], seconds: list[str]) -> tuple[str, str] | None:
    """``(first, second)`` for a pytest id ``<first>-<second>`` (names may
    themselves contain hyphens); None when it matches no pair."""
    for first in sorted(firsts, key=len, reverse=True):
        if param_id.startswith(f"{first}-") and param_id[len(first) + 1:] in seconds:
            return first, param_id[len(first) + 1:]
    return None


def _cases(report: Path) -> list[tuple[str, str, str]]:
    """``(test name with id, verdict, failure text)`` per test case."""
    cases: list[tuple[str, str, str]] = []
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if case.find("skipped") is not None:
            cases.append((case.get("name", ""), "skipped", ""))
        elif problem is None:
            cases.append((case.get("name", ""), "ok", ""))
        else:
            text = f"{problem.get('message', '')}\n{problem.text or ''}"
            verdict = "RAN" if EXECUTED_RE.search(text) else "fail"
            cases.append((case.get("name", ""), verdict, text))
    return cases


def _print_table(title: str, rows: list[str], columns: list[str],
                 cells: dict[tuple[str, str], str]) -> None:
    print(f"\n## {title}\n")
    print("| vector | " + " | ".join(columns) + " |")
    print("|---" * (len(columns) + 1) + "|")
    for row in rows:
        values = [cells.get((row, column), "-") for column in columns]
        if any(value != "-" for value in values):
            print(f"| {row} | " + " | ".join(values) + " |")


def summarize(report: Path, module: Path) -> int:
    vectors, operations = _matrix_names(module)
    cleanup: dict[tuple[str, str], str] = {}
    no_lazy_fetch: dict[tuple[str, str], str] = {}
    isolated: dict[tuple[str, str], str] = {}
    executed: list[str] = []
    verdicts: Counter[str] = Counter()
    for name, verdict, text in _cases(report):
        test, _, param_id = name.partition("[")
        param_id = param_id.rstrip("]")
        verdicts[verdict] += 1
        if test in (CLEANUP_TEST, NO_LAZY_FETCH_TEST):
            pair = _split_id(param_id, vectors, operations)
            if pair is not None:
                (cleanup if test == CLEANUP_TEST else no_lazy_fetch)[pair] = verdict
        elif test == ISOLATED_TEST:
            pair = _split_id(param_id, list(ISOLATED_OUTCOMES), vectors)
            if pair is not None:
                isolated[(pair[1], pair[0])] = verdict
        match = EXECUTED_RE.search(text)
        if match:
            executed.append(f"{match.group(1)}/{match.group(2)}: {match.group(3)}")
    _print_table("Cleanup operations", vectors, operations, cleanup)
    _print_table("Cleanup on a git without lazy-fetch control", vectors, operations, no_lazy_fetch)
    _print_table("Isolated runs", vectors, list(ISOLATED_OUTCOMES), isolated)
    print("\n## Planted programs that ran\n")
    # The same pair also runs in the no-lazy-fetch dimension: listed once.
    for line in sorted(set(executed)) or ["none"]:
        print(f"- {line}")
    ran_vectors = sorted({line.split("/", 1)[0] for line in executed})
    print(
        f"\nTotals: {sum(verdicts.values())} cases, {verdicts['ok']} ok, "
        f"{verdicts['RAN']} RAN, {verdicts['fail']} fail, {verdicts['skipped']} skipped; "
        f"vectors that ran: {len(ran_vectors)} ({', '.join(ran_vectors) or 'none'})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", type=Path, help="JUnit XML report of the matrix module")
    parser.add_argument("--module", type=Path, default=DEFAULT_MODULE,
                        help="the matrix test module (default: %(default)s)")
    options = parser.parse_args(argv)
    if not options.report.is_file():
        print(f"no such report: {options.report}", file=sys.stderr)
        return 2
    return summarize(options.report, options.module)


if __name__ == "__main__":
    sys.exit(main())
