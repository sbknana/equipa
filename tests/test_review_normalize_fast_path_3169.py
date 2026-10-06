"""Task 3169: normalize_review_text and separated_review_text skip passes
that cannot change the text (an ASCII review; NFKC that changes nothing),
and give exactly what the three passes gave.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from equipa import security_gate
from equipa.security_gate import normalize_review_text, separated_review_text

# A private-use character, as the backstop's marks are.
SEPARATOR = chr(0xE000)


def three_passes(text: str, separator: str) -> str:
    """The normalisation as written before task 3169."""
    text = security_gate._INVISIBLE_CHARS_RE.sub(separator, text)
    text = unicodedata.normalize("NFKC", text)
    text = security_gate._INVISIBLE_CHARS_RE.sub(separator, text)
    return security_gate._LINE_BREAK_RE.sub("\n", text)


# Every line break, invisible characters (one NFKC makes: U+3164 becomes
# U+1160), compatibility forms, combining marks and plain words.
ALPHABET = [
    "H", "I", "G", "h", " ", "\t", "x", "1", "&", "#", ";",
    "\r", "\n", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
    "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}",
    "\N{SOFT HYPHEN}", "\N{ZERO WIDTH SPACE}", "\N{HANGUL FILLER}",
    "\N{HALFWIDTH HANGUL FILLER}", "\N{ZERO WIDTH NO-BREAK SPACE}",
    "\U000E0041", "\U0001D173",
    "\N{FULLWIDTH LATIN CAPITAL LETTER H}",
    "\N{MATHEMATICAL BOLD CAPITAL I}", "\N{LATIN SMALL LIGATURE FI}",
    "\N{COMBINING ACUTE ACCENT}", "\N{GREEK CAPITAL LETTER ETA}",
    "\N{ANGSTROM SIGN}", "\N{VULGAR FRACTION ONE HALF}",
]

CASES = [
    "",
    "HIGH",
    "\r" * 9,
    "a\r\r\nb\r\n\rc",
    "\x0b\x0c\x1c\x1d\x1e",
    "### [S1] HIGH\r\n## Counts\rCRITICAL: 0",
    "HI\N{ZERO WIDTH SPACE}GH",
    "Rated\N{HANGUL FILLER}HIGH",
    "\N{FULLWIDTH LATIN CAPITAL LETTER H}IGH\N{LINE SEPARATOR}x",
    "HIGH\N{COMBINING ACUTE ACCENT} " * 3,
    "e\N{COMBINING ACUTE ACCENT}\r\n\N{ZERO WIDTH SPACE}",
    "\N{HALFWIDTH HANGUL FILLER}",
]


@pytest.mark.parametrize("separator", ["", SEPARATOR])
@pytest.mark.parametrize("text", CASES)
def test_listed_texts_normalise_as_the_three_passes(text, separator):
    if separator:
        assert separated_review_text(text, separator) == three_passes(
            text, separator)
    else:
        assert normalize_review_text(text) == three_passes(text, "")


@pytest.mark.parametrize("separator", ["", SEPARATOR])
def test_random_texts_normalise_as_the_three_passes(separator):
    generator = random.Random(3169)
    for _ in range(20000):
        pieces = generator.choices(ALPHABET, k=generator.randrange(0, 12))
        if generator.random() < 0.3:
            # Pure ASCII texts take the fast path.
            pieces = [piece for piece in pieces if piece.isascii()]
        text = "".join(pieces)
        expected = three_passes(text, separator)
        if separator:
            actual = separated_review_text(text, separator)
        else:
            actual = normalize_review_text(text)
        assert actual == expected, ascii(text)


def test_ascii_line_breaks_are_every_ascii_break_of_the_regex():
    """The translate table and the regex name the same ASCII breaks."""
    ascii_breaks = {chr(code) for code in range(128)
                    if security_gate._LINE_BREAK_RE.fullmatch(chr(code))}
    assert ascii_breaks == {chr(code)
                            for code in security_gate._ASCII_LINE_BREAKS}


def test_no_invisible_character_is_ascii_and_nfkc_keeps_ascii():
    """The two facts the ASCII fast path rests on."""
    assert not any(security_gate._INVISIBLE_CHARS_RE.match(chr(code))
                   for code in range(128))
    every_ascii = "".join(chr(code) for code in range(128))
    assert unicodedata.normalize("NFKC", every_ascii) == every_ascii
