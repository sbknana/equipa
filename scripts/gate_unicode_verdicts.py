#!/usr/bin/env python3
"""Decide generated look-alike reviews through the merge gate, and compare
the verdicts two interpreters gave (task 3177, IR3174-01).

Usage:
    python3 scripts/gate_unicode_verdicts.py decide OUT.json
        [--bodies N] [--seed S] [--workers W] [--from-file BODIES.json]
    python3 scripts/gate_unicode_verdicts.py compare A.json B.json

``decide`` builds at least N bodies (default 100,000) with
tests/gate_unicode_lookalikes.py: the Unicode 14/15 marks indep-3174
reported, U+A7F2, the code points whose data changed in Unicode 15 and 16,
the table's lookalikes of the severity letters and a seeded sample of code
points outside the table, each in every spelling and placement. The bodies
are built from the gate's checked-in table only, so every interpreter
decides the same bodies. With ``--from-file`` the bodies are read from a
JSON object instead (the format of the indep-3174 probe): a string is a
line of a review with one LOW finding, ``{"raw": text}`` a whole review, a
list of lines the body of a review with no finding and of one with a LOW
finding (two decisions, ``#zero`` and ``#one-low``).

Every review is written as a recorded reviewer artifact (lone surrogates
kept) and decided by ``dispatch._security_review_blocks_merge``, the
function the orchestrator calls before a merge; OUT.json maps each name to
[blocks, verdict, detail], with "untrusted" and the provenance reason when
provenance refuses the review, or "exception" and its type.

``compare`` prints how many decisions differ between two such files, in
which direction, and the first few, and exits 1 when any does.

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
TASK_ID = 3177

# The two reviews a list of lines is decided in (the indep-3174 harness).
_ZERO_FOOTER = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
_ONE_LOW_FOOTER = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
_LOW_HEADING = "### [E1] LOW \N{EM DASH} verbose error message"


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


def _list_review(summary: str, lines: list[str], footer: str,
                 heading: str | None = None) -> str:
    text = ["# Security Review", "", "## Summary", summary, ""]
    if heading:
        text += [heading, "Details.", ""]
    text += [*lines, "", "## Files Reviewed", "- app.py", "",
             "## Methodology", "Read the diff, ran semgrep.", "",
             "## Counts", footer]
    return "\n".join(text) + "\n"


def _reviews(name: str, body: object) -> list[tuple[str, str]]:
    """(name, review text) for one body of either source."""
    from tests import gate_unicode_lookalikes as lookalikes

    if isinstance(body, str):
        return [(name, lookalikes.review(body))]
    if isinstance(body, dict) and isinstance(body.get("raw"), str):
        return [(name, body["raw"])]
    if isinstance(body, list) and all(isinstance(line, str) for line in body):
        return [
            (f"{name}#zero", _list_review(
                "No findings.", ["## Findings", "", *body], _ZERO_FOOTER)),
            (f"{name}#one-low", _list_review(
                "1 finding.", ["## Notes", "", *body], _ONE_LOW_FOOTER,
                heading=_LOW_HEADING)),
        ]
    raise ValueError(f"{name}: not a string, a {{'raw': text}} or a list of "
                     f"lines")


def _decide_text(project: Path, text: str) -> list:
    """[blocks, verdict, detail] of the merge gate on ``text`` as this
    task's recorded review in ``project``."""
    from equipa import loops
    from equipa.dispatch import _security_review_blocks_merge
    from equipa.security_gate import (
        REVIEWER_STATUS_SUCCEEDED, ReviewerRunRecord, fingerprint_artifact,
        record_reviewer_run, verify_reviewer_provenance)
    from tests.review_gate_production import (
        NONCE, as_reviewer_artifact, audit_rows_not_persisted)

    path = project / ".equipa-artifacts" / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(as_reviewer_artifact(text).encode("utf-8",
                                                       "surrogatepass"))
    record_reviewer_run(ReviewerRunRecord(
        task_id=TASK_ID, nonce=NONCE, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=fingerprint_artifact(path), attempts=1,
    ))
    with audit_rows_not_persisted():
        blocks, _counts = _security_review_blocks_merge(
            str(project), TASK_ID, block_on_missing=True)
    provenance = verify_reviewer_provenance(TASK_ID, path)
    if provenance.text is None:
        # The reason may name the artifact, whose directory differs per run.
        return [blocks, "untrusted",
                provenance.reason.replace(str(project), "<project>")]
    analysis = loops._analyze_review_file(path, text=provenance.text)
    return [blocks, analysis.verdict, analysis.detail]


def _decide_slice(arguments: tuple[list[tuple[str, object]], str]) -> dict:
    """Decide each (name, body) in a scratch project of this worker."""
    items, scratch = arguments
    project = Path(tempfile.mkdtemp(prefix="gate-verdicts-", dir=scratch))
    decided = {}
    for name, body in items:
        for review_name, text in _reviews(name, body):
            # The gate prints a GATE-AUDIT line per decision; not wanted.
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    decided[review_name] = _decide_text(project, text)
                except Exception as error:  # noqa: BLE001 - recorded
                    decided[review_name] = ["exception", type(error).__name__,
                                            str(error)[:200]]
    return decided


def decide(out: Path, bodies: int, seed: int, workers: int,
           from_file: Path | None) -> int:
    from tests import gate_unicode_lookalikes as lookalikes

    if from_file is None:
        code_points = _code_points(bodies, seed)
        source = f"{len(code_points)} code points"
        generated = sorted(lookalikes.lookalike_bodies(code_points).items())
    else:
        loaded = json.loads(from_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SystemExit(f"{from_file}: not a JSON object of bodies")
        source = from_file.name
        generated = sorted(loaded.items())
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
        "source": source,
        "decisions": decided,
    }, sort_keys=True))
    blocked = sum(1 for decision in decided.values() if decision[0] is True)
    failed = sum(1 for decision in decided.values()
                 if decision[0] == "exception")
    print(f"python {sys.version.split()[0]} unicode "
          f"{unicodedata.unidata_version}: {len(decided)} decisions from "
          f"{source}, {blocked} block, {len(decided) - blocked - failed} "
          f"merge, {failed} exceptions, {time.monotonic() - started:.0f} s "
          f"-> {out}")
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
    print(f"{len(names)} decisions, {len(missing)} in one file only, "
          f"{len(differing)} differ")
    first_only = sum(1 for name in differing
                     if one["decisions"][name][0] is True
                     and other["decisions"][name][0] is False)
    second_only = sum(1 for name in differing
                      if other["decisions"][name][0] is True
                      and one["decisions"][name][0] is False)
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
    decide_parser.add_argument("--from-file", type=Path, default=None)
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
                  arguments.workers, arguments.from_file)


if __name__ == "__main__":
    sys.exit(main())
