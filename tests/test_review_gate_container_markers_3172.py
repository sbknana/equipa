"""Nested container markers are read in C (task 3172, timing on Python 3.10).

The 200 KB ``list_markers`` review ("- " * 100,000, then "HIGH: x") took
0.45 s of its 0.5 s gate budget on Python 3.10 and failed the 3.10 full
suite at 0.5047 s on a loaded host: ``_rendered_blocks`` and
``_innermost_container_line`` each matched one container marker per Python
step, 200,000 steps per gate call. Both now find the run of markers with one
match (``_CONTAINER_MARKER_RUN_RE``) and read its markers with ``finditer``
(``loops._container_markers``), and ``_rendered_blocks`` drops the list
items a line ends with one ``bisect``.

These tests hold the new code to the old results (a frozen copy of the
pre-3172 loops and a digest of ``_rendered_blocks`` taken on main c163986,
the same on Python 3.10 and 3.12), to no Python step per marker through the
merge gate, and the run regex to bounded memory on Python 3.10.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
import re

from equipa import loops
from tests.review_gate_production import production_decision
from tests.test_regex_py310_compat_3169 import (
    MEMORY_BYTES_PER_CHARACTER,
    MEMORY_RUN,
    _peak_traced_bytes,
)

LINE_ALPHABET = [">", "-", "*", "1", ".", ")", " ", "\t", "a"]
LINE_LENGTH = 5
PIECES = ["- ", "-", "* ", "+ ", "1. ", "12) ", "123456789. ", "1234567890. ",
          "> ", ">", ">\t", "-\t", "-    ", "-     ", " ", "  ", "    ", "\t",
          "a", "HIGH: x", "[^1]: ", "[^a]:", "```", "~~~", "<!-- ", "-->",
          "<div>", "|", "| a |", "|---|", "# ", "---", "===", "`", "x\\", ">>"]
TEXT_SEED = 3172
TEXT_COUNT = 20_000
# _rendered_blocks over the seeded texts and every alphabet line, computed
# with the pre-3172 loops on main c163986 (Python 3.10 and 3.12 alike).
PRE_3172_BLOCKS_DIGEST = (
    "8c6f8883f546e93d82aee71bd4dc19272b972f4a76cbce684a3c1133b21d0606")
DEEP_RUN = 100_000


def alphabet_lines() -> list[str]:
    """Every line of up to LINE_LENGTH characters of LINE_ALPHABET."""
    return ["".join(chars)
            for length in range(LINE_LENGTH + 1)
            for chars in itertools.product(LINE_ALPHABET, repeat=length)]


def seeded_texts() -> list[str]:
    """Multi-line texts of container markers, indentation, fences, comments,
    footnotes, tables and blank lines (seeded, so the digest holds)."""
    rng = random.Random(TEXT_SEED)
    texts = []
    for _ in range(TEXT_COUNT):
        lines = []
        for _ in range(rng.randint(1, 8)):
            if rng.random() < 0.15:
                lines.append("")
                continue
            lines.append("".join(rng.choice(PIECES)
                                 for _ in range(rng.randint(0, 9))))
        texts.append("\n".join(lines))
    return texts


def repeated_marker_spans(text: str, start: int) -> list[tuple[int, int]]:
    """The pre-3172 marker loop: match again where the last match ended."""
    spans = []
    while (marker := loops._CONTAINER_MARKER_RE.match(text, start)) is not None:
        spans.append(marker.span())
        start = marker.end()
    return spans


def pre_3172_innermost_container_line(line: str) -> str:
    """``loops._innermost_container_line`` as it was before task 3172."""
    stripped = line.lstrip(" \t")
    indent = len(line) - len(stripped)
    first = loops._CONTAINER_MARKER_RE.match(line, indent)
    if first is None:
        return line
    innermost = loops._CONTAINER_MARKER_RE.match(line, first.end())
    if innermost is None or loops._THEMATIC_BREAK_RE.fullmatch(line):
        return line
    while (inner := loops._CONTAINER_MARKER_RE.match(
            line, innermost.end())) is not None:
        innermost = inner
    content = line[innermost.end():]
    if not content.strip():
        return line
    return line[:indent] + innermost.group(0) + content


def blocks_record(text: str) -> list:
    blocks = loops._rendered_blocks(text)
    return [blocks.lines, sorted(blocks.fences.items()), sorted(blocks.breaks),
            sorted(blocks.table_rows), sorted(blocks.comment_openers)]


def test_the_markers_of_a_run_are_those_the_repeated_match_finds():
    lines = alphabet_lines() + [
        "- " * 50 + "x", "1. " * 40 + "> " * 40, "123456789. 1234567890. x",
        "-    -     -", ">>>>- > -", "-\t-\t>\t1)\tx",
    ]
    for line in lines:
        indent = len(line) - len(line.lstrip(" \t"))
        for start in {0, indent}:
            found = [marker.span()
                     for marker in loops._container_markers(line, start)]
            assert found == repeated_marker_spans(line, start), (line, start)


def test_innermost_container_line_is_unchanged():
    lines = alphabet_lines() + [
        line for text in seeded_texts() for line in text.split("\n")]
    lines += ["- " * DEEP_RUN + "HIGH: x", "> " * DEEP_RUN + "- HIGH: x",
              "  " + "1. " * 1_000 + "HIGH", "- " * 1_000]
    for line in lines:
        assert (loops._innermost_container_line(line)
                == pre_3172_innermost_container_line(line)), line[:80]


def test_rendered_blocks_are_unchanged():
    digest = hashlib.sha256()
    for text in alphabet_lines() + seeded_texts():
        digest.update(json.dumps([text, blocks_record(text)]).encode())
    assert digest.hexdigest() == PRE_3172_BLOCKS_DIGEST


def test_a_deep_run_opens_a_list_item_per_marker_before_a_quote():
    """The item columns the rows below a deep run are read against: each
    list marker before the first quote opens one, so a line indented to the
    innermost item's content column is a paragraph of it, not code."""
    head = "- " * 3 + "> " + "- " * 2 + "x"
    blocks = loops._rendered_blocks(head + "\n\n" + " " * 6 + "    `HIGH`")
    # Columns 2, 4 and 6 are open; 10 columns is code relative to column 6.
    assert blocks.lines[2] == " " * 10 + loops._LITERAL_CODE_MARK + "HIGH" \
        + loops._LITERAL_CODE_MARK


class CountingPattern:
    """A stand-in for _CONTAINER_MARKER_RE that counts its ``match`` calls."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.wrapped = pattern
        self.match_calls = 0

    def match(self, *args: object) -> re.Match[str] | None:
        self.match_calls += 1
        return self.wrapped.match(*args)

    def __getattr__(self, name: str) -> object:
        return getattr(self.wrapped, name)


def test_a_deep_marker_run_takes_no_python_step_per_marker(monkeypatch):
    """Through the merge gate: 100,000 nested "- " markers cost a handful of
    marker matches (before task 3172, over 200,000), and the review still
    blocks (its HIGH finding is in no footer)."""
    counting = CountingPattern(loops._CONTAINER_MARKER_RE)
    monkeypatch.setattr(loops, "_CONTAINER_MARKER_RE", counting)
    text = "\n".join([
        "# Security Review", "", "## Summary", "No findings.", "",
        "- " * DEEP_RUN + "HIGH: x", "", "## Counts",
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0",
    ]) + "\n"
    from tests.review_gate_production import as_reviewer_artifact

    decision = production_decision(as_reviewer_artifact(text))
    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks
    assert 0 < counting.match_calls < 1_000, counting.match_calls


def test_the_run_regex_runs_in_bounded_memory():
    """Python 3.10's sre keeps a backtracking entry per pass of a plain loop
    over a group (task 3169); the run regex commits each batch of passes."""
    for text in ("- " * (MEMORY_RUN // 2), "> " * (MEMORY_RUN // 2),
                 "1. " * (MEMORY_RUN // 3), "-\t>" * (MEMORY_RUN // 3)):
        re.purge()
        assert loops._CONTAINER_MARKER_RUN_RE.match(text).end() == len(text)
        peak = _peak_traced_bytes(loops._CONTAINER_MARKER_RUN_RE, "match",
                                  text)
        assert peak <= MEMORY_BYTES_PER_CHARACTER * len(text), (text[:6], peak)
