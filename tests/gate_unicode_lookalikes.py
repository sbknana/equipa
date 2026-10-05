"""Generated look-alike review bodies for the review gate's Unicode checks
(task 3177, IR3174-01).

indep-3174 found the gate's verdict still depended on the interpreter's
Unicode data when a character reference only exists once the review is
normalised ("<FULLWIDTH AMPERSAND>#x1AC1;" is "&#x1AC1;" under NFKC; "&",
a zero-width space and "#x1AC1;" are "&#x1AC1;" once the space is deleted)
or once the renderer removes a comment or decodes "&amp;". This module
builds those bodies: each code point in each spelling, in each placement
inside a severity word.

Everything here is computed from the gate's checked-in table and from
constants, never from the interpreter's Unicode data, so the same bodies
are generated on every interpreter and their verdicts can be compared
across interpreters (tests/test_review_gate_formed_references_3177.py pins
a digest of them; scripts/gate_unicode_verdicts.py compares 100,000 and
more between two interpreters).

The source stays ASCII: special characters are named or built with chr().

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
from typing import Callable

from equipa import loops

FULLWIDTH_AMPERSAND = "\N{FULLWIDTH AMPERSAND}"
SMALL_AMPERSAND = "\N{SMALL AMPERSAND}"
SMALL_NUMBER_SIGN = "\N{SMALL NUMBER SIGN}"
ZERO_WIDTH_SPACE = "\N{ZERO WIDTH SPACE}"
SOFT_HYPHEN = "\N{SOFT HYPHEN}"
UNASSIGNED = 0x0378  # unassigned in every Unicode version so far


def fullwidth(text: str) -> str:
    """``text`` with each printable ASCII character as its fullwidth form."""
    return "".join(chr(ord(char) - 0x21 + 0xFF01) if "!" <= char <= "~"
                   else char for char in text)


def mathematical_digits(text: str) -> str:
    """``text`` with each ASCII digit as MATHEMATICAL BOLD DIGIT."""
    return "".join(chr(0x1D7CE + int(char)) if char.isdigit() else char
                   for char in text)


# Each spelling of one code point. The first four are references the text as
# written holds; the next nine only become one once the review is normalised
# (NFKC, invisible characters deleted); the last four once the gate decodes
# "&amp;" or removes a comment or a tag.
SPELLINGS: dict[str, Callable[[int], str]] = {
    "literal": chr,
    "hex": lambda code_point: f"&#x{code_point:X};",
    "decimal": lambda code_point: f"&#{code_point};",
    "hex-unterminated": lambda code_point: f"&#x{code_point:X} ",
    "fullwidth": lambda code_point: fullwidth(f"&#x{code_point:X};"),
    "fullwidth-ampersand": (
        lambda code_point: f"{FULLWIDTH_AMPERSAND}#x{code_point:X};"),
    "fullwidth-ampersand-decimal": (
        lambda code_point: f"{FULLWIDTH_AMPERSAND}#{code_point};"),
    "small-forms": (lambda code_point:
                    f"{SMALL_AMPERSAND}{SMALL_NUMBER_SIGN}x{code_point:X};"),
    "fullwidth-digits": (
        lambda code_point: f"&#x{fullwidth(f'{code_point:X}')};"),
    "mathematical-digits": (
        lambda code_point: f"&#{mathematical_digits(str(code_point))};"),
    "zero-width-space": (
        lambda code_point: f"&{ZERO_WIDTH_SPACE}#x{code_point:X};"),
    "soft-hyphen": lambda code_point: f"&#x{SOFT_HYPHEN}{code_point:X};",
    "fullwidth-decimal": lambda code_point: fullwidth(f"&#{code_point};"),
    "named-ampersand-number": lambda code_point: f"&amp;&num;x{code_point:X};",
    "comment": lambda code_point: f"&<!-- -->#x{code_point:X};",
    "tag": lambda code_point: f"&<b></b>#x{code_point:X};",
    "double-encoded": lambda code_point: f"&amp;#x{code_point:X};",
}
# The spellings that are a reference only once the review is normalised.
NORMALIZED_SPELLINGS = (
    "fullwidth", "fullwidth-ampersand", "fullwidth-ampersand-decimal",
    "small-forms", "fullwidth-digits", "mathematical-digits",
    "zero-width-space", "soft-hyphen", "fullwidth-decimal",
)

# Where a spelling goes: inside a severity word of a finding heading, a
# Severity field and a bold lead-in, or in place of its first letter.
PLACEMENTS = {
    "heading-high": "### [S2] HI{x}GH - upload handler",
    "heading-critical": "### [S2] CRITI{x}CAL - remote code execution",
    "severity-field": "- **Severity:** CRI{x}TICAL",
    "heading-first-letter": "### [S2] {x}RITI{x}AL - remote code execution",
    "bold-first-letter": "**{x}IGH** - finding",
}

# indep-3174: the code points whose reference blocked on Python 3.12 and
# merged on 3.10, as inclusive ranges: the combining marks new in Unicode
# 14 and 15, and U+A7F2 (a C under NFKC from Unicode 14 on).
UNICODE_14_15_MARK_RANGES = (
    (0x0898, 0x089F), (0x08CA, 0x08D2), (0x0C3C, 0x0C3C), (0x0ECE, 0x0ECE),
    (0x1AC1, 0x1ACE), (0x1DFA, 0x1DFA), (0x10EFD, 0x10EFF),
    (0x10F82, 0x10F85), (0x11070, 0x11074), (0x110C2, 0x110C2),
    (0x11241, 0x11241), (0x11F00, 0x11F42), (0x13440, 0x13455),
    (0x1CF00, 0x1CF46), (0x1E08F, 0x1E08F), (0x1E2AE, 0x1E2AE),
    (0x1E4EC, 0x1E4EF),
)
MODIFIER_CAPITAL_C = 0xA7F2
# The three Unicode 13.0 code points whose data changed by 15.0 (outside the
# table), and U+1171E, a table character Unicode 16.0 made a spacing mark.
CHANGED_BY_UNICODE_15 = (0x10FC, 0x1734, 0xAB69)
CHANGED_BY_UNICODE_16 = 0x1171E


def unicode_14_15_marks() -> list[int]:
    """Every code point of UNICODE_14_15_MARK_RANGES."""
    return [code_point for first, last in UNICODE_14_15_MARK_RANGES
            for code_point in range(first, last + 1)]


def one_mark_per_block() -> list[int]:
    """The first code point of each range of UNICODE_14_15_MARK_RANGES."""
    return [first for first, _last in UNICODE_14_15_MARK_RANGES]


def outside_table_ranges(limit: int = 0x40000) -> list[tuple[int, int]]:
    """The code points below ``limit`` outside the gate's table, as
    half-open ranges (every Unicode 14-16 character is below U+40000)."""
    edges = (0,) + loops._GATE_UNICODE_EDGES
    ranges = []
    # Between the end of one table range (an odd edge) and the next start.
    for start, end in zip(edges[0::2], edges[1::2]):
        start, end = min(start, limit), min(end, limit)
        if start < end:
            ranges.append((start, end))
    return ranges


def outside_table_sample(count: int, seed: int) -> list[int]:
    """``count`` code points outside the table, drawn with ``seed`` (the
    same ones on every interpreter), sorted."""
    ranges = outside_table_ranges()
    total = sum(end - start for start, end in ranges)
    picks = sorted(random.Random(seed).sample(range(total), min(count, total)))
    sample, offset, index = [], 0, 0
    for pick in picks:
        while pick >= offset + ranges[index][1] - ranges[index][0]:
            offset += ranges[index][1] - ranges[index][0]
            index += 1
        sample.append(ranges[index][0] + pick - offset)
    return sample


def table_lookalikes() -> list[int]:
    """The table's lookalikes of the severity words' letters that the
    backstop folds (they are read, so they are parsed alike)."""
    return sorted(code_point for code_point in loops._BACKSTOP_LETTER_FOLDS
                  if loops._in_gate_unicode_table(chr(code_point)))


def body_name(placement: str, spelling: str, code_point: int) -> str:
    return f"{placement}:{spelling}:U+{code_point:04X}"


def lookalike_bodies(code_points: list[int],
                     spellings: tuple[str, ...] = tuple(SPELLINGS),
                     placements: tuple[str, ...] = tuple(PLACEMENTS),
                     ) -> dict[str, str]:
    """name -> body line for each code point, spelling and placement."""
    return {
        body_name(placement, spelling, code_point):
            PLACEMENTS[placement].format(x=SPELLINGS[spelling](code_point))
        for code_point in code_points
        for spelling in spellings
        for placement in placements
    }


def review(body: str) -> str:
    """A review with one LOW finding, ``body`` on line 8 once the provenance
    line is added, and a footer that counts only the LOW (the review of
    tests/test_review_gate_unicode_data_3172.py)."""
    return (
        "# Security Review: task 1\n"
        "## Summary\n"
        "One finding.\n"
        "## Findings\n"
        "### [S1] LOW - log line\n"
        "- **File:** a.py:1\n"
        f"{body}\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n"
    )
