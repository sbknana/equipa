"""Task 3164 (R3161-02): the review gate's mark stripper and the backstop's
per-word checks are cheap and give the answers they gave before.

* ``_strip_combining_marks`` decomposes the whole text and deletes its marks
  in one translate; it equals task 3137's definition (each non-ASCII run
  decomposed, its Mn and Me characters dropped).
* ``_translate_non_ascii`` completes its table for the text first; a table
  that fills itself per key (``__missing__``) gives what str.translate gives.
* The FOLD-mark test is a set lookup; it equals the offset arithmetic.
* 200 KB of mark-dense text strips in a fraction of what it took, and the
  families added to the 200 KB timing set
  (tests/test_review_gate_backstop_3143.py) still block.

The source stays ASCII: special characters are code points.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
import re
import unicodedata

import pytest

from equipa import loops
from tests.host_timing import assert_linear_time
from tests.review_gate_production import (
    AS_WRITTEN_ONLY_FINDINGS,
    blocked_by_the_gate,
)
from tests.review_gate_timing import median_cpu_seconds, timing_test
from tests.test_review_gate_backstop_3143 import (
    BACKSTOP_FAMILIES,
    RB,
    ZERO,
    backstop_families,
    review,
)

MARK_CATEGORIES = ("Mn", "Me")
NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")
NEW_TIMING_FAMILIES = ("eta_led", "mark_after_each_letter",
                       "mark_after_each_word")

# ASCII, combining marks of several classes (Mn and Me), precomposed letters,
# a Hangul syllable (NFD splits it into jamo), U+FFFD, an ideograph, a
# fullwidth letter, invisible characters and the lookalike eta.
CHARACTER_POOL = list("HIGHhigh MEDIUM CRITICAL\n\t|-_:1") + [
    chr(code_point) for code_point in (
        0x0301, 0x0300, 0x0308, 0x030A, 0x0323, 0x0327, 0x0332, 0x05B9,
        0x093C, 0x20DD, 0x0489, 0x00CD, 0x00E5, 0x1EBF, 0x212B, 0x0397,
        0xD55C, 0xFFFD, 0x4E00, 0xFF28, 0x200B, 0x3164, 0x00A0,
    )
]


def reference_strip(text: str) -> str:
    """Task 3137's definition: every non-ASCII run decomposed (NFD) and its
    combining marks dropped, ASCII left as it is."""
    def without_marks(run: re.Match[str]) -> str:
        return "".join(
            char for char in unicodedata.normalize("NFD", run.group(0))
            if unicodedata.category(char) not in MARK_CATEGORIES)
    return NON_ASCII_RUN.sub(without_marks, text)


def random_texts(count: int, seed: int) -> list[str]:
    generator = random.Random(seed)
    return ["".join(generator.choice(CHARACTER_POOL)
                    for _ in range(generator.randrange(0, 60)))
            for _ in range(count)]


# --- The mark stripper ------------------------------------------------------

def test_the_stripper_equals_the_per_run_definition():
    for text in random_texts(4000, seed=3164):
        assert loops._strip_combining_marks(text) == reference_strip(text), (
            text.encode("unicode_escape"))


def test_the_stripper_drops_every_combining_mark():
    marks = [chr(code_point) for code_point in range(0x110000)
             if unicodedata.category(chr(code_point)) in MARK_CATEGORIES]
    assert len(marks) > 1500
    text = "".join(f"H{mark}IGH " for mark in marks)

    stripped = loops._strip_combining_marks(text)

    assert stripped == reference_strip(text)
    assert stripped == "HIGH " * len(marks)


def test_the_stripper_reads_letters_under_precomposed_accents():
    accented = "".join(chr(code_point) for code_point in (0x0048, 0x00CD,
                                                          0x0047, 0x0048))
    assert loops._strip_combining_marks(accented) == "HIGH"


@timing_test
def test_200kb_of_marks_strip_in_one_pass():
    """One callback per short run took 0.13 s here (task 3161). Budget
    host-calibrated, growth from 50 KB to 200 KB linear (task 3171)."""
    def seconds_at(rb):
        text = review("No findings.",
                      backstop_families(rb)["mark_after_each_letter"], ZERO)
        return median_cpu_seconds(loops._strip_combining_marks, text)

    assert_linear_time(seconds_at, RB, 0.06, "mark_after_each_letter")


# --- The completed translate table -----------------------------------------

class FillingTable(dict):
    """A table that fills itself per key, as the backstop's tables do. Like
    every table _translate_non_ascii takes, it maps no ASCII character."""

    def __missing__(self, code_point: int) -> str:
        char = chr(code_point)
        if char.isascii():
            value = char
        elif char == "\N{ZERO WIDTH SPACE}":
            value = ""
        else:
            value = char.upper()
        self[code_point] = value
        return value


TABLES = {
    "filling": FillingTable,
    "plain": lambda: {0x0397: "H", 0x200B: None, 0x00CD: 0x49},
    "backstop-characters": loops._BackstopCharacterTable,
    "backstop-separators": loops._BackstopSeparatorTable,
}


@pytest.mark.parametrize("density", ["sparse", "dense"])
@pytest.mark.parametrize("table", sorted(TABLES))
def test_the_completed_table_translates_as_str_translate(table, density):
    texts = random_texts(300, seed=31642)
    if density == "sparse":
        # Under one character in sixteen non-ASCII: the run-split path.
        texts = [f"{'a' * 40 * len(text)}{text}" for text in texts]
    for text in texts:
        expected = text.translate(TABLES[table]())
        assert loops._translate_non_ascii(text, TABLES[table]()) == expected, (
            text.encode("unicode_escape"))


def test_a_filling_table_is_read_for_each_character():
    """A table's ``.get`` would skip ``__missing__`` and leave the
    lookalike as written."""
    eta_text = "\N{GREEK CAPITAL LETTER ETA}IGH " * 50
    folded = loops._translate_non_ascii(eta_text,
                                        loops._BackstopCharacterTable())
    assert folded == eta_text.translate(loops._BackstopCharacterTable())
    assert folded != eta_text


# --- The FOLD-mark test -----------------------------------------------------

def test_the_fold_mark_set_equals_the_offset_arithmetic():
    def by_arithmetic(char: str) -> bool:
        offset = ord(char) - loops._BACKSTOP_FOLD_MARK
        return 0x41 <= offset % 0x100 <= 0x7A and offset // 0x100 in (0, 1)

    for code_point in range(0xDF00, 0xE300):
        char = chr(code_point)
        assert loops._backstop_is_fold_mark(char) == by_arithmetic(char), (
            hex(code_point))


def test_a_new_fold_mark_is_found_in_a_word():
    mark = chr(loops._BACKSTOP_NEW_FOLD_MARK + ord("H"))
    word = f"{mark}IGH"
    view = f" {word} "
    assert loops._backstop_separated_counts(word, view, 1, 5)
    assert not loops._backstop_separated_counts(word, view, 1, 5,
                                                new_folds=False)


# --- The new timing families still block -----------------------------------

def test_the_new_families_are_in_the_timing_set():
    assert set(NEW_TIMING_FAMILIES) <= set(BACKSTOP_FAMILIES)


@pytest.mark.parametrize("name", NEW_TIMING_FAMILIES)
def test_a_new_timing_family_blocks(name):
    """Decided through the merge gate (task 3170, IR67-02)."""
    text = review("No findings.", BACKSTOP_FAMILIES[name], ZERO)
    analysis = blocked_by_the_gate(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(loops.backstop_reason("HIGH")), (
        analysis.detail[:120])


@pytest.mark.parametrize("finding", sorted(AS_WRITTEN_ONLY_FINDINGS))
def test_the_must_block_helper_reads_the_review_as_written(finding):
    """Task 3170 (IR67-02): a severity only the review as written shows
    blocks through the merge gate here as well, so a gate that parsed the
    normalised text (R3161-01) fails this suite too."""
    text = review("No findings.", [AS_WRITTEN_ONLY_FINDINGS[finding]], ZERO)
    analysis = blocked_by_the_gate(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(tuple(
        loops.backstop_reason(severity)
        for severity in loops.MERGE_BLOCKING_SEVERITIES)), analysis.detail
