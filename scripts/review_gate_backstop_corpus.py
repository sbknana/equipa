#!/usr/bin/env python3
"""Run the review gate over real SECURITY-REVIEW files, with and without the
task 3143 severity-token backstop, and report what the backstop changes.

Usage:
    python3 scripts/review_gate_backstop_corpus.py ROOT [ROOT ...]
        [--sample N] [--lines] [--seed S]

Every ``SECURITY-REVIEW-*.md`` below each ROOT (``node_modules`` skipped) is
read once per distinct sha256. Files are only read. For each review the
shape rules alone (``_analyze_review_views``) and the full gate
(``_analyze_review_file``, rules plus backstop) are run, and the script
prints:

* how many distinct reviews the gate trusted before and after;
* how many would have MERGED before (trusted, CRITICAL and HIGH both 0) and
  are now blocked by the backstop ("newly block"), as a share of all;
* a seeded sample of the backstop reasons, and with ``--lines`` the text of
  each blocking line (to see which positions cause the blocks).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from equipa import loops  # noqa: E402
from equipa.security_gate import normalize_review_text  # noqa: E402


def find_reviews(roots: list[str]) -> list[Path]:
    """Every SECURITY-REVIEW-*.md below ``roots``, node_modules skipped."""
    found: list[Path] = []
    for root in roots:
        for directory, subdirectories, files in os.walk(root):
            subdirectories[:] = [name for name in subdirectories
                                 if name != "node_modules"]
            for name in files:
                if name.startswith("SECURITY-REVIEW-") and name.endswith(".md"):
                    found.append(Path(directory) / name)
    return sorted(found)


def distinct_reviews(paths: list[Path]) -> dict[str, tuple[Path, str]]:
    """sha256 -> (first path, text) for every readable review."""
    reviews: dict[str, tuple[Path, str]] = {}
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as error:
            print(f"skip {path.name}: {error}", file=sys.stderr)
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest not in reviews:
            reviews[digest] = (path, data.decode("utf-8", errors="replace"))
    return reviews


def merges(analysis: loops.ReviewCountAnalysis) -> bool:
    """True when the merge gate would let this review's branch merge."""
    counts = analysis.counts or {}
    return (analysis.trusted and counts.get("CRITICAL", 0) == 0
            and counts.get("HIGH", 0) == 0)


def blocking_line_numbers(detail: str) -> list[int]:
    """The line numbers a backstop reason names."""
    numbers: list[int] = []
    for part in detail.split("at line ")[1:]:
        listed = part.split(" (")[0].split(" and ")[0]
        numbers += [int(item) for item in listed.split(", ") if item.isdigit()]
    return numbers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--seed", type=int, default=3143)
    parser.add_argument("--lines", action="store_true",
                        help="print every blocking line of each newly blocked review")
    arguments = parser.parse_args()

    paths = find_reviews(arguments.roots)
    reviews = distinct_reviews(paths)
    trusted_before = trusted_after = merged_before = 0
    newly_blocked: list[tuple[Path, str, str]] = []
    slowest = (0.0, "")
    for path, text in reviews.values():
        normalized = normalize_review_text(text)
        if loops.SECURITY_REVIEW_FALLBACK_MARKER in normalized[:512]:
            continue
        before = loops._analyze_review_views(normalized)
        started = time.perf_counter()
        after = loops._analyze_review_file(path, text=text)
        elapsed = time.perf_counter() - started
        if elapsed > slowest[0]:
            slowest = (elapsed, path.name)
        trusted_before += before.trusted
        trusted_after += after.trusted
        if merges(before):
            merged_before += 1
            if not merges(after):
                newly_blocked.append((path, normalized, after.detail))

    total = len(reviews)
    share = 100.0 * len(newly_blocked) / total if total else 0.0
    print(f"paths: {len(paths)}  distinct reviews: {total}")
    print(f"trusted: {trusted_before} before, {trusted_after} after")
    print(f"would merge before: {merged_before}")
    print(f"newly blocked by the backstop: {len(newly_blocked)} "
          f"({share:.1f}% of distinct reviews)")
    print(f"slowest full gate: {slowest[0]:.3f}s ({slowest[1]})")
    rng = random.Random(arguments.seed)
    sample = rng.sample(newly_blocked, min(arguments.sample, len(newly_blocked)))
    print(f"\nsample of {len(sample)} reasons:")
    for path, _, detail in sample:
        print(f"- {path.name}: {detail}")
    if arguments.lines:
        print("\nblocking lines:")
        for path, normalized, detail in newly_blocked:
            lines = normalized.split("\n")
            for number in blocking_line_numbers(detail):
                if 0 < number <= len(lines):
                    print(f"{path.name}:{number}: {lines[number - 1][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
