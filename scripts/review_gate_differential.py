#!/usr/bin/env python3
"""Run review texts through this tree's review gate and through older trees,
and report every text an older tree blocked that this tree lets merge.

Usage:
    python3 scripts/review_gate_differential.py --tree NAME=PATH [...]
        (--bodies | --corpus ROOT [ROOT ...]) [--write-fixture PATH]
        [--sample N] [--seed S]

Each ``--tree`` is the root of another checkout of this repository (for
example a ``git archive`` copy of an older commit). It is run in a worker
subprocess, so each tree imports its own ``equipa`` package.

``--bodies`` builds review texts from every probe body the review-gate test
modules hold (parametrized values and upper-case module constants of the
3130, 3137, 3143, 3149 and 3152 modules, timing floods left out), each in a
zero-finding review and in a review with one counted LOW finding
(``build_review`` of tests/test_review_gate_no_exemptions_3152.py). With
``--write-fixture`` the bodies some older tree blocked are written as the
fixture of tests/test_review_gate_stricter_3152.py.

``--corpus`` reads every ``SECURITY-REVIEW-*.md`` below each ROOT
(``node_modules`` skipped) once per distinct sha256. Files are only read.

A text "blocks" as dispatch._security_review_blocks_merge decides: the
review is not trusted, or it counts a CRITICAL or HIGH finding. A parser
exception blocks too. The exit status is 1 when any text passes here that
an older tree blocked.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARVESTED_MODULES = (
    "test_review_gate_followups_3130.py",
    "test_review_gate_fixforward_3137.py",
    "test_review_gate_backstop_3143.py",
    "test_review_gate_followups_3149.py",
    "test_review_gate_tester_3149.py",
    "test_review_gate_no_exemptions_3152.py",
)
BUILDER_MODULE = "test_review_gate_no_exemptions_3152.py"
MAX_BODY_CHARACTERS = 4000
CURRENT = "this tree"


# --- worker: one tree's verdicts --------------------------------------------------

def run_worker(tree: str) -> None:
    """Read a JSON list of texts on stdin; print one verdict per text."""
    sys.path.insert(0, tree)
    from equipa import loops  # the tree's own package

    texts = json.load(sys.stdin)
    verdicts = []
    for text in texts:
        try:
            analysis = loops._analyze_review_file(
                Path("SECURITY-REVIEW-0.md"), text=text)
        except Exception as error:  # noqa: BLE001 - an exception blocks
            verdicts.append({"blocked": True, "verdict": "exception",
                             "detail": type(error).__name__})
            continue
        counts = analysis.counts or {}
        blocked = (not analysis.trusted or counts.get("CRITICAL", 0) > 0
                   or counts.get("HIGH", 0) > 0)
        verdicts.append({"blocked": blocked, "verdict": analysis.verdict,
                         "detail": analysis.detail})
    json.dump(verdicts, sys.stdout)


def verdicts_of(tree: Path, texts: list[str]) -> list[dict]:
    """The verdicts of ``tree`` for ``texts``, from a worker subprocess."""
    environment = dict(os.environ)
    environment.setdefault("THEFORGE_DB", "/nonexistent/review-gate-diff.db")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker",
         str(tree)],
        input=json.dumps(texts), capture_output=True, text=True,
        env=environment, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"worker for {tree} failed: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


# --- harvesting the probe bodies of the test modules -------------------------------

def _load_test_module(name: str):
    path = REPO / "tests" / name
    spec = importlib.util.spec_from_file_location(
        f"review_gate_diff_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bodies_in(value) -> list[list[str]]:
    """Every probe body inside one parametrized value or constant item: a
    string is a body (split into lines), a list of strings is a body."""
    import pytest

    if isinstance(value, type(pytest.param(None))):
        value = value.values
    if isinstance(value, str):
        return [value.split("\n")]
    if isinstance(value, list) and value and all(
            isinstance(item, str) for item in value):
        return [list(value)]
    if isinstance(value, (list, tuple)):
        return [body for item in value for body in _bodies_in(item)]
    if isinstance(value, dict):
        return [body for item in value.values() for body in _bodies_in(item)]
    return []


def _module_bodies(module) -> list[list[str]]:
    bodies: list[list[str]] = []
    for name, value in vars(module).items():
        if callable(value) and name.startswith("test_"):
            for mark in getattr(value, "pytestmark", []):
                if mark.name == "parametrize" and len(mark.args) > 1:
                    for item in mark.args[1]:
                        bodies.extend(_bodies_in(item))
        elif (name.isupper() and "FAMIL" not in name
              and isinstance(value, (list, tuple, dict))):
            items = value.values() if isinstance(value, dict) else value
            for item in items:
                bodies.extend(_bodies_in(item))
    return bodies


def harvested_bodies() -> list[tuple[str, list[str]]]:
    """(module, body) for every distinct short probe body, in order."""
    seen: set[tuple[str, ...]] = set()
    found: list[tuple[str, list[str]]] = []
    for name in HARVESTED_MODULES:
        for body in _module_bodies(_load_test_module(name)):
            key = tuple(body)
            if (key in seen or sum(map(len, body)) > MAX_BODY_CHARACTERS
                    or not any(char.isalpha() for line in body
                               for char in line)):
                continue
            seen.add(key)
            found.append((name, body))
    return found


# --- corpus ------------------------------------------------------------------------

def corpus_reviews(roots: list[str]) -> list[tuple[str, str]]:
    """(first path, text) per distinct SECURITY-REVIEW-*.md below ``roots``."""
    reviews: dict[str, tuple[str, str]] = {}
    for root in roots:
        for directory, subdirectories, files in os.walk(root):
            subdirectories[:] = sorted(name for name in subdirectories
                                       if name != "node_modules")
            for name in sorted(files):
                if not (name.startswith("SECURITY-REVIEW-")
                        and name.endswith(".md")):
                    continue
                path = Path(directory) / name
                try:
                    data = path.read_bytes()
                except OSError as error:
                    print(f"skip {path.name}: {error}", file=sys.stderr)
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if digest not in reviews:
                    reviews[digest] = (str(path), data.decode(
                        "utf-8", errors="replace"))
    return list(reviews.values())


# --- report ------------------------------------------------------------------------

def compare(labels: list[str], texts: list[str], trees: dict[str, Path],
            sample: int, seed: int) -> tuple[dict[str, list[dict]], int]:
    """Verdicts per tree (this tree first) and the number of regressions."""
    results = {CURRENT: verdicts_of(REPO, texts)}
    for name, path in trees.items():
        results[name] = verdicts_of(path, texts)
    current = results[CURRENT]
    print(f"texts: {len(texts)}")
    for name, verdicts in results.items():
        print(f"  {name}: {sum(v['blocked'] for v in verdicts)} block")
    regressions = 0
    rng = random.Random(seed)
    for name in trees:
        before = results[name]
        newly_pass = [index for index, (old, new) in enumerate(zip(before, current))
                      if old["blocked"] and not new["blocked"]]
        newly_block = [index for index, (old, new) in enumerate(zip(before, current))
                       if new["blocked"] and not old["blocked"]]
        regressions += len(newly_pass)
        print(f"vs {name}: newly PASS {len(newly_pass)}, "
              f"newly block {len(newly_block)}")
        for index in newly_pass:
            print(f"  NEWLY PASSES: {labels[index]}")
            print(f"    was: {before[index]['verdict']} "
                  f"{before[index]['detail'][:160]}")
            print(f"    now: {current[index]['verdict']} "
                  f"{current[index]['detail'][:160]}")
        for index in sorted(rng.sample(newly_block,
                                       min(sample, len(newly_block)))):
            print(f"  newly blocks: {labels[index]}: "
                  f"{current[index]['detail'][:160]}")
    return results, regressions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--tree", action="append", default=[],
                        metavar="NAME=PATH", help="an older tree to compare")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--bodies", action="store_true",
                        help="the probe bodies of the review-gate tests")
    source.add_argument("--corpus", nargs="+", metavar="ROOT",
                        help="real SECURITY-REVIEW files below each ROOT")
    parser.add_argument("--write-fixture", metavar="PATH")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--seed", type=int, default=3152)
    args = parser.parse_args()
    if args.worker:
        run_worker(args.worker)
        return 0
    if not (args.bodies or args.corpus):
        parser.error("give --bodies or --corpus")
    trees: dict[str, Path] = {}
    for item in args.tree:
        name, separator, path = item.partition("=")
        if not separator or not Path(path, "equipa", "loops.py").is_file():
            parser.error(f"--tree {item!r}: want NAME=PATH of a checkout")
        trees[name] = Path(path).resolve()

    if args.corpus:
        reviews = corpus_reviews(args.corpus)
        _, regressions = compare([path for path, _ in reviews],
                                 [text for _, text in reviews],
                                 trees, args.sample, args.seed)
        return 1 if regressions else 0

    sys.path.insert(0, str(REPO))
    builder = _load_test_module(BUILDER_MODULE).build_review
    contexts = _load_test_module(BUILDER_MODULE).CONTEXTS
    entries = [(module, body, context) for module, body in harvested_bodies()
               for context in contexts]
    texts = [builder(body, context) for _, body, context in entries]
    labels = [f"{module} {context} {body!r}"[:300]
              for module, body, context in entries]
    results, regressions = compare(labels, texts, trees, args.sample, args.seed)
    if args.write_fixture:
        write_fixture(Path(args.write_fixture), entries, texts, results, trees)
    return 1 if regressions else 0


def write_fixture(path: Path, entries: list, texts: list[str],
                  results: dict[str, list[dict]], trees: dict[str, Path]) -> None:
    """One line per body some older tree blocked in some context: the body,
    and per context the sha256 of its review text and the trees that
    blocked it."""
    bodies: dict[tuple[str, ...], dict] = {}
    for index, ((_, body, context), text) in enumerate(zip(entries, texts)):
        blocked_by = [name for name in sorted(trees)
                      if results[name][index]["blocked"]]
        if not blocked_by:
            continue
        entry = bodies.setdefault(tuple(body), {"body": body, "contexts": {}})
        entry["contexts"][context] = {
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "blocked_by": blocked_by,
        }
    lines = [json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
             for entry in bodies.values()]
    path.write_text(
        '{"trees":' + json.dumps(sorted(trees)) + ',"entries":[\n'
        + ",\n".join(lines) + "\n]}\n", encoding="utf-8")
    print(f"wrote {len(lines)} bodies to {path}")


if __name__ == "__main__":
    sys.exit(main())
