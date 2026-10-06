#!/usr/bin/env python3
"""The probe corpus of the independent review of task 3154, as a generator.

Usage:
    python3 scripts/review_gate_probe_corpus.py      (prints family sizes)

The review that found R3154-01, R3154-02 and R3154-03 judged five probe sets
on main (3afec74), ba6065a and the 3154 tree. This module rebuilds them, for
every severity word, so that a regression test can replay them
(tests/test_review_gate_followups_3157.py) and
scripts/review_gate_differential.py --families can judge them on other
trees:

* ``replay``: the reviewers' hand-written bodies
  (tests/fixtures/review_gate_probe_bodies_3154.json);
* ``separator``: a severity word in one of eleven letter styles (ASCII,
  circled, squared, negative circled and squared, parenthesised, fullwidth,
  mathematical bold, regional indicators, modifier and small capitals), as a
  whole or with one letter styled, next to each of eighteen separators
  (fillers, unassigned code points and their references, number forms, a
  zero-width and a no-break space), in three positions (R3154-01, I3152-01,
  I3152-02);
* ``tally``: a count, negation or label before the word and a tally,
  noun or finding text after it, plain and soft-wrapped (R3154-02);
* ``negation``: every negation word with one of its letters replaced by
  each lookalike of that letter the review gate knows (the Unicode
  confusables of the letters of the three words and the styled capitals
  above), before the word (R3154-03);
* ``split``, ``noun``, ``comma``, ``list`` and ``private-use`` (task 3161,
  from the independent review of task 3157): a lookalike after the M of a
  word whose tail is a negation ("MINOR"), at the end of a generic noun, of a
  lower-case word after a comma and of a listed LOW, and a private-use
  character inside "MINOR" (R3157-01, R3157-02).

Every body is a list of lines; ``probe_bodies`` returns them in a fixed
order with a key and the severity word they hold. Nothing here reads the
review gate itself, so the corpus does not change when the gate does.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import itertools
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPLAY_FIXTURE = REPO / "tests" / "fixtures" / "review_gate_probe_bodies_3154.json"
SEVERITY_WORDS = ("CRITICAL", "HIGH", "MEDIUM")
TAIL = ": SQL injection in login.py allows auth bypass."


def _styled(first: int):
    """A style mapping each capital A-Z to ``first`` + its offset."""
    return lambda letter: chr(first + ord(letter) - ord("A"))


# Modifier capitals (U+1D2C-U+1D41) and small capitals of the letters of
# the three words; every other letter stays ASCII in those styles.
MODIFIER_CAPITALS = {
    "A": 0x1D2C, "D": 0x1D30, "E": 0x1D31, "G": 0x1D33, "H": 0x1D34,
    "I": 0x1D35, "L": 0x1D38, "M": 0x1D39, "R": 0x1D3F, "T": 0x1D40,
    "U": 0x1D41,
}
SMALL_CAPITALS = {
    "A": 0x1D00, "C": 0x1D04, "D": 0x1D05, "E": 0x1D07, "G": 0x0262,
    "H": 0x029C, "I": 0x026A, "L": 0x029F, "M": 0x1D0D, "R": 0x0280,
    "T": 0x1D1B, "U": 0x1D1C,
}
LETTER_STYLES = {
    "ascii": lambda letter: letter,
    "circled": _styled(0x24B6),
    "squared": _styled(0x1F130),
    "neg-circled": _styled(0x1F150),
    "neg-squared": _styled(0x1F170),
    "paren": _styled(0x1F110),
    "fullwidth": _styled(0xFF21),
    "math-bold": _styled(0x1D400),
    "regional": _styled(0x1F1E6),
    "modifier": lambda letter: chr(MODIFIER_CAPITALS.get(letter, ord(letter))),
    "smallcap": lambda letter: chr(SMALL_CAPITALS.get(letter, ord(letter))),
}
SEPARATORS = {
    "none": "", "space": " ",
    "u3164": chr(0x3164), "u115F": chr(0x115F), "uFFA0": chr(0xFFA0),
    "u0378": chr(0x0378), "ref3164": "&#x3164;", "ref378": "&#x378;",
    "sup1": chr(0x00B9), "sub2": chr(0x2082), "half": chr(0x00BD),
    "romanII": chr(0x2161), "circ1": chr(0x2460), "ordA": chr(0x00AA),
    "fw1": chr(0xFF11), "refsup1": "&sup1;", "zwsp": chr(0x200B),
    "nbsp": chr(0x00A0),
}
ZERO_WIDTH_SPACE = chr(0x200B)
TALLY_PREFIXES = (
    "", "Found ", "We found ", "There are ", "1 ", "2 ", "3 ", "one ",
    "two ", "0 ", "zero ", "no ", "No ", "not ", "without ", "below ",
    "rather than ", "lower than ", "less than ", "LOW rather than ",
    "LOW, not ", "none of ", "none at ", "never ", "neither ", "nor ",
    "nothing ", "Overall risk: ", "Risk: ", "Severity: ", "- ", "* ",
    "| ", "| 2 ", "**", "`", "Rated ", "rated ", "below the ",
    "2 x ", "2x ", "N=2 ", "count 2 ", "(2) ", "#2 ", "2. ", "2) ",
    "- 2 ", "Found 2 ", "Found two ", "| 1 | ",
)
TALLY_SUFFIXES = (
    "", ": 0", ": 0 open.", " = 0", " | 0 |", ": 2", ": 1", " findings",
    " findings.", ": none", " (0)", " 0", ": zero", " issues: 0",
    "/LOW", " or LOW", " and LOW: 0", ": SQL injection in login.py",
    f" {chr(0x2014)} SQL injection", " finding: XSS in view.py", "**", "`",
    " |", " | 2 |", ": 0 | LOW: 0", "=0", ":0", " -> 0", " count 0",
    " findings (0)", ": 0.", ": 00", ": 0 (was 2)",
)
NEGATIONS = (
    "no", "not", "nor", "zero", "without", "nothing", "neither", "never",
    "rather than", "other than", "lower than", "less than", "none",
    "none is", "none of", "none reached", "none rated",
)


def _styled_word(word: str, style: str) -> str:
    """``word`` in ``style``; regional indicators are joined by a
    zero-width space, or they would pair into flags."""
    styled = [LETTER_STYLES[style](letter) for letter in word]
    return (ZERO_WIDTH_SPACE if style == "regional" else "").join(styled)


def separator_family() -> list[tuple[str, str, list[str]]]:
    bodies = []
    for word in SEVERITY_WORDS:
        for style in LETTER_STYLES:
            spellings = {"all": _styled_word(word, style)}
            if style not in ("ascii", "regional"):
                for index, letter in enumerate(word):
                    styled = LETTER_STYLES[style](letter)
                    if styled != letter:
                        spellings[f"one{index}"] = (word[:index] + styled
                                                    + word[index + 1:])
            for spelling, written in spellings.items():
                for name, separator in SEPARATORS.items():
                    key = f"separator|{word}|{style}|{spelling}|{name}"
                    bodies += [
                        (f"{key}|A", word, [f"Rated{separator}{written}{TAIL}"]),
                        (f"{key}|B", word, [f"{written}{separator}{TAIL}"]),
                        (f"{key}|C", word,
                         [f"The {written}{separator}issue{TAIL}"]),
                    ]
    return bodies


def tally_family() -> list[tuple[str, str, list[str]]]:
    bodies = []
    for word in SEVERITY_WORDS:
        for prefix, suffix in itertools.product(TALLY_PREFIXES, TALLY_SUFFIXES):
            key = f"tally|{word}|{prefix!r}|{suffix!r}"
            bodies.append((key, word, [f"{prefix}{word}{suffix}"]))
            bodies.append((f"{key}|wrap", word, [
                f"Summary line ending {prefix}".rstrip(), f"{word}{suffix}"]))
    return bodies


# The shapes the review gate folds beyond the Unicode confusables data, as
# of task 3154 (equipa/loops.py _BACKSTOP_EXTRA_LOOKALIKES), copied so the
# corpus stays fixed when the gate's tables change.
EXTRA_LOOKALIKES = {
    "A": (0x1D00, 0x2C80, 0x15C5, 0x15E9),
    "C": (0x1D04, 0x2CA4, 0x1455),
    "D": (0x1D05, 0x15EA),
    "E": (0x1D07, 0x2C88, 0x2D39),
    "G": (0x13F3,),
    "H": (0x2C8E, 0x04A2, 0x04C7, 0x04C9, 0x0126, 0x2C67, 0x157C),
    "I": (0xA7AE, 0x2C92, 0x16C1, 0x2D4F, 0x07CA, 0x10309, 0x2223),
    "L": (0x029F, 0x14AA),
    "M": (0x1D0D, 0x2C98, 0x03FA, 0x15F0),
    "R": (0x0280, 0x01A6, 0x1587),
    "T": (0x1D1B, 0x2CA6, 0x22A4, 0x102A2),
    "U": (0x1D1C, 0x144C, 0x054D),
}


def lookalikes() -> dict[int, str]:
    """Code point -> the capital it is drawn as: every Unicode confusable of
    the letters of the three words (the I table holds what is drawn as a
    small l), the extra shapes above and the styled capitals."""
    sys.path.insert(0, str(REPO))
    try:
        from equipa.severity_confusables import SEVERITY_LETTER_CONFUSABLES
    finally:
        sys.path.pop(0)
    found = {
        code_point: letter
        for table in (SEVERITY_LETTER_CONFUSABLES, EXTRA_LOOKALIKES)
        for letter, code_points in table.items()
        for code_point in code_points
    }
    for style in LETTER_STYLES:
        if style == "ascii":
            continue
        for letter in "CRITALHGMEDU":
            styled = LETTER_STYLES[style](letter)
            if styled != letter:
                found.setdefault(ord(styled), letter)
    return dict(sorted(found.items()))


def negation_family() -> list[tuple[str, str, list[str]]]:
    bodies = []
    folds = lookalikes()
    for negation in NEGATIONS:
        for index, char in enumerate(negation):
            for code_point, letter in folds.items():
                if not (letter.lower() == char
                        or (letter == "I" and char in "il")):
                    continue
                written = negation[:index] + chr(code_point) + negation[index + 1:]
                key = f"negation|{negation}|{index}|{code_point:04X}"
                bodies += [
                    (f"{key}|a", "MEDIUM", [f"{written} MEDIUM findings."]),
                    (f"{key}|b", "MEDIUM",
                     [f"Shipped {written} MEDIUM or LOW findings."]),
                    (f"{key}|high", "HIGH", [f"{written} HIGH findings."]),
                ]
    return bodies


# Task 3161: the shapes of the independent review of task 3157 (R3157-01,
# R3157-02). A lookalike, or a private-use character, inside an ordinary
# word split it for the deleted exemption rules: the tail of "MINOR" became
# a negation, and a label word ending in one was no longer read as a label.
SPLIT_TAILS = ("nor", "no", "not", "never", "none", "zero", "without",
               "nothing", "neither")
PRIVATE_USE = (0xE000, 0xE123, 0xF8FF, 0xF0000, 0x100000)


def split_family() -> list[tuple[str, str, list[str]]]:
    """``Two M<x><tail> WORD issues remain.``: a lookalike after the M of a
    word whose tail is a negation. MEDIUM, whose exemptions R3157-01 split,
    gets each tail in upper and lower case; CRITICAL and HIGH (exemption-free
    since task 3152) each tail in upper case."""
    bodies = []
    folds = lookalikes()
    for word in SEVERITY_WORDS:
        cases = ("up", "low") if word == "MEDIUM" else ("up",)
        for tail, case in itertools.product(SPLIT_TAILS, cases):
            written = tail.upper() if case == "up" else tail
            for code_point in folds:
                bodies.append((
                    f"split|{word}|{tail}|{case}|{code_point:04X}", word,
                    [f"Two M{chr(code_point)}{written} {word} issues remain."]))
    return bodies


def label_family() -> list[tuple[str, str, list[str]]]:
    """A lookalike at the end of a label word: after a generic noun
    (``noun``), a lower-case word after a comma (``comma``) and a listed
    severity word (``list``)."""
    bodies = []
    folds = lookalikes()
    for word in SEVERITY_WORDS:
        for code_point in folds:
            mark = chr(code_point)
            for shape, line in (
                    ("noun", f"No {word} issues{mark}{TAIL}"),
                    ("comma", f"Not {word}, see{mark}{TAIL}"),
                    ("list", f"not {word} / LOW{mark}{TAIL}")):
                bodies.append((f"{shape}|{word}|{code_point:04X}", word,
                               [line]))
    return bodies


def private_use_family() -> list[tuple[str, str, list[str]]]:
    """``Two MI<private use>NOR WORD issues remain.`` (R3157-02), and the
    same with a zero-width space, which was always deleted."""
    bodies = []
    for word in SEVERITY_WORDS:
        for code_point in (*PRIVATE_USE, 0x200B):
            bodies.append((
                f"private-use|{word}|{code_point:04X}", word,
                [f"Two MI{chr(code_point)}NOR {word} issues remain."]))
    return bodies


def replay_family() -> list[tuple[str, str, list[str]]]:
    replay = json.loads(REPLAY_FIXTURE.read_text(encoding="utf-8"))
    bodies = []
    for name, lines in replay.items():
        text = "\n".join(lines)
        severity = next((word for word in SEVERITY_WORDS if word in text),
                        "other")
        bodies.append((f"replay|{name}", severity, lines))
    return bodies


def probe_bodies() -> list[tuple[str, str, list[str]]]:
    """(key, severity word, lines) of every probe body, in a fixed order."""
    bodies = (replay_family() + separator_family() + tally_family()
              + negation_family() + split_family() + label_family()
              + private_use_family())
    keys = [key for key, _, _ in bodies]
    if len(set(keys)) != len(keys):
        raise RuntimeError("duplicate probe body keys")
    return bodies


def main() -> int:
    bodies = probe_bodies()
    families = Counter(key.split("|", 1)[0] for key, _, _ in bodies)
    severities = Counter(severity for _, severity, _ in bodies)
    print(f"{len(bodies)} bodies: {dict(families)}; by severity "
          f"{dict(severities)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
