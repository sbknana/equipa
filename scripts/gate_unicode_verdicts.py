#!/usr/bin/env python3
"""Decide generated look-alike reviews through the merge gate, and compare
the verdicts two interpreters gave (task 3177, IR3174-01).

Usage:
    python3 scripts/gate_unicode_verdicts.py decide OUT.json
        [--bodies N] [--seed S] [--workers W]
    python3 scripts/gate_unicode_verdicts.py compare A.json B.json

``decide`` builds at least N bodies (default 100,000) with
tests/gate_unicode_lookalikes.py: the Unicode 14/15 marks indep-3174
reported, U+A7F2, the code points whose data changed in Unicode 15 and 16,
the table's lookalikes of the severity letters and a seeded sample of code
points outside the table, each in every spelling and placement. Every body
is written as a recorded reviewer artifact and decided by
``dispatch._security_review_blocks_merge`` (tests/review_gate_production.py),
and OUT.json maps each body's name to [blocks, verdict, detail]. The bodies
are built from the gate's checked-in table only, so every interpreter
decides the same bodies.

``compare`` prints how many decisions differ between two such files (and
the first few), and exits 1 when any does.

Run ``decide`` once per interpreter, then ``compare`` the two files. GATE-
AUDIT rows go to a scratch database, never to THEFORGE_DB.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import multiprocessing
import os
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent


def _code_points(bodies: int, seed: int) -> list[int]:
    """The code points whose bodies make at least ``bodies`` bodies."""
    from tests import gate_unicode_lookalikes as lookalikes

    fixed = sorted(set(
        lookalikes.unicode_14_15_marks()
        + [lookalikes.MODIFIER_CAPITAL_C, lookalikes.CHANGED_BY_UNICODE_16,
           lookalikes.UNASSIGNED]
        + list(lookalikes.CHANGED_BY_UNICODE_15)
        + lookalikes.table_lookalikes()
    ))
    per_code_point = len(lookalikes.SPELLINGS) * len(lookalikes.PLACEMENTS)
    wanted = -(-bodies // per_code_point)
    sample = lookalikes.outside_table_sample(
        max(0, wanted - len(fixed)) * 2, seed)
    extra = [code_point for code_point in sample if code_point not in fixed]
    return sorted(fixed + extra[:max(0, wanted - len(fixed))])


def _decide_slice(arguments: tuple[list[tuple[str, str]], str]) -> dict:
    """Decide each (name, body) in a scratch project of this worker."""
    items, scratch = arguments
    from tests import gate_unicode_lookalikes as lookalikes
    from tests.review_gate_production import (
        as_reviewer_artifact, production_decision)
    from equipa import loops

    project = Path(tempfile.mkdtemp(prefix="gate-verdicts-", dir=scratch))
    decided = {}
    for name, body in items:
        text = as_reviewer_artifact(lookalikes.review(body))
        # The gate prints a GATE-AUDIT line per decision; it is not wanted.
        with contextlib.redirect_stderr(io.StringIO()):
            decision = production_decision(text, project_dir=project)
            if decision.provenance.text is None:
                decided[name] = [decision.blocks, "untrusted",
                                 decision.provenance.reason]
                continue
            analysis = loops._analyze_review_file(
                Path("SECURITY-REVIEW.md"), text=decision.provenance.text)
        decided[name] = [decision.blocks, analysis.verdict, analysis.detail]
    return decided


def decide(out: Path, bodies: int, seed: int, workers: int) -> int:
    from tests import gate_unicode_lookalikes as lookalikes

    code_points = _code_points(bodies, seed)
    generated = sorted(lookalikes.lookalike_bodies(code_points).items())
    scratch = tempfile.mkdtemp(prefix="gate-verdicts-")
    slices = [(generated[index::workers], scratch) for index in range(workers)]
    started = time.monotonic()
    decided: dict[str, list] = {}
    context = multiprocessing.get_context("fork")
    with context.Pool(workers) as pool:
        for part in pool.imap_unordered(_decide_slice, slices):
            decided.update(part)
    out.write_text(json.dumps({
        "python": sys.version.split()[0],
        "unidata_version": unicodedata.unidata_version,
        "code_points": len(code_points),
        "decisions": decided,
    }, sort_keys=True))
    blocked = sum(1 for blocks, _verdict, _detail in decided.values() if blocks)
    print(f"python {sys.version.split()[0]} unicode "
          f"{unicodedata.unidata_version}: {len(decided)} bodies from "
          f"{len(code_points)} code points, {blocked} block, "
          f"{len(decided) - blocked} merge, "
          f"{time.monotonic() - started:.0f} s -> {out}")
    return 0


def compare(first: Path, second: Path) -> int:
    one = json.loads(first.read_text())
    other = json.loads(second.read_text())
    names = sorted(set(one["decisions"]) | set(other["decisions"]))
    missing = [name for name in names
               if name not in one["decisions"]
               or name not in other["decisions"]]
    differing = [name for name in names
                 if name not in missing
                 and one["decisions"][name] != other["decisions"][name]]
    print(f"{first.name}: python {one['python']} unicode "
          f"{one['unidata_version']}; {second.name}: python "
          f"{other['python']} unicode {other['unidata_version']}")
    print(f"{len(names)} bodies, {len(missing)} in one file only, "
          f"{len(differing)} decisions differ")
    first_only = sum(1 for name in differing
                     if one["decisions"][name][0]
                     and not other["decisions"][name][0])
    second_only = sum(1 for name in differing
                      if other["decisions"][name][0]
                      and not one["decisions"][name][0])
    print(f"block in {first.name} and merge in {second.name}: {first_only}; "
          f"merge in {first.name} and block in {second.name}: {second_only}")
    for name in (missing + differing)[:20]:
        print(f"  {name}: {one['decisions'].get(name)} vs "
              f"{other['decisions'].get(name)}")
    return 1 if missing or differing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    decide_parser = commands.add_parser("decide")
    decide_parser.add_argument("out", type=Path)
    decide_parser.add_argument("--bodies", type=int, default=100_000)
    decide_parser.add_argument("--seed", type=int, default=3177)
    decide_parser.add_argument("--workers", type=int,
                               default=max(1, (os.cpu_count() or 2) // 2))
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("first", type=Path)
    compare_parser.add_argument("second", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "compare":
        return compare(arguments.first, arguments.second)
    if arguments.bodies < 1 or arguments.workers < 1:
        parser.error("--bodies and --workers must be at least 1")
    # Before equipa is imported: the gate's audit rows go to a scratch file.
    os.environ["THEFORGE_DB"] = str(
        Path(tempfile.mkdtemp(prefix="gate-verdicts-db-")) / "scratch.db")
    sys.path.insert(0, str(REPOSITORY))
    logging.disable(logging.CRITICAL)
    return decide(arguments.out, arguments.bodies, arguments.seed,
                  arguments.workers)


if __name__ == "__main__":
    sys.exit(main())
