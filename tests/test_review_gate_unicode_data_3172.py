"""Task 3172 (R3169-01): the review gate's verdict does not depend on the
interpreter's Unicode data.

Python 3.10 ships Unicode 13.0 and 3.12 Unicode 15.0. U+A7F2 (MODIFIER
LETTER CAPITAL C, Unicode 14) is a C under NFKC on 3.12 and unassigned on
3.10, so a finding heading that spelled the top severity with it blocked on
3.12 and merged on 3.10 (the same for a Severity field, a bold lead-in and,
found by this task, a numeric reference "&#xA7F2;" in an ASCII review). The
gate now reads only the characters of a checked-in table
(``loops._GATE_UNICODE_RANGES``): every code point assigned in Unicode 13.0
except three whose properties changed by 15.0. A review holding any other
character, or a reference to one, is not parsed (fail closed).

* The reported bodies are refused through ``_security_review_blocks_merge``
  with the same verdict and reason on every interpreter (the expected values
  are constants, and CI runs this file on 3.10 and 3.12).
* The decision on every body is the same when the gate reads Unicode data
  that predates every character outside the table, which is what an older
  interpreter sees.
* Every table character has the same Unicode data on every CI interpreter:
  a digest of every property the gate, ``re`` and ``str`` read is pinned.
* An interpreter older than the table parses no review that is not ASCII.

The source stays ASCII: special characters are named or built with chr().

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import _sre
import hashlib
import html
import html.entities
import itertools
import random
import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import equipa.security_gate as security_gate
from equipa import loops
from tests.host_timing import assert_linear_time
from tests.review_gate_production import (
    as_reviewer_artifact,
    decision_and_analysis,
    production_decision,
    production_seconds,
)
from tests.review_gate_timing import timing_test

MODIFIER_CAPITAL_C = chr(0xA7F2)  # Unicode 14, NFKC "C" from 14 on
MODIFIER_CAPITAL_F = chr(0xA7F3)  # Unicode 14, NFKC "F"
MODIFIER_CAPITAL_Q = chr(0xA7F4)  # Unicode 14, NFKC "Q"
MODIFIER_SMALL_Q = chr(0x107A5)  # Unicode 14, NFKC "q"
HANUNOO_PAMUDPOD = chr(0x1734)  # Mn in Unicode 13, Mc in 15
UNASSIGNED = chr(0x0378)  # unassigned in every version so far
CJK_EXTENSION_H = chr(0x31350)  # Unicode 15

# The three Unicode 13.0 code points whose properties changed by 15.0.
CHANGED_BY_UNICODE_15 = (0x10FC, 0x1734, 0xAB69)

C = MODIFIER_CAPITAL_C
REFUSED_BODIES = {
    # The review's bodies (SECURITY-REVIEW-3169 R3169-01, indep-3168-3169).
    "heading": (f"### [S2] {C}RITI{C}AL \N{EM DASH} remote code execution",
                "U+A7F2 at line 8"),
    "severity-field": (f"- **Severity:** {C}RITI{C}AL", "U+A7F2 at line 8"),
    "bold-lead-in": (f"**{C}RITI{C}AL** \N{EM DASH} remote code execution",
                     "U+A7F2 at line 8"),
    # The same spelling as numeric references in an otherwise ASCII review.
    "hex-reference": ("### [S2] &#xA7F2;RITI&#xA7F2;AL - remote code "
                      "execution", "U+A7F2 reference at line 8"),
    "decimal-reference": ("### [S2] &#0042994RITI&#42994;AL - remote code "
                          "execution", "U+A7F2 reference at line 8"),
    # The other post-13.0 NFKC folds to ASCII letters, and characters whose
    # data differs or is missing.
    "modifier-f": (f"### [S2] HIGH - {MODIFIER_CAPITAL_F}ile write",
                   "U+A7F3 at line 8"),
    "modifier-q": (f"Note: {MODIFIER_CAPITAL_Q}uery {MODIFIER_SMALL_Q}ueue",
                   "U+A7F4 at line 8"),
    "superscript-q": (f"Note: {MODIFIER_SMALL_Q}ueue", "U+107A5 at line 8"),
    "changed-category": (f"### [S2] HI{HANUNOO_PAMUDPOD}GH - upload",
                         "U+1734 at line 8"),
    "unassigned": (f"### [S2] HI{UNASSIGNED}GH - upload", "U+0378 at line 8"),
    "cjk-extension-h": (f"Note: {CJK_EXTENSION_H}", "U+31350 at line 8"),
}
# Bodies made of table characters only: the gate parses them as before.
PARSED_BODIES = {
    "latin": ("### [S2] CRITICAL - remote code execution", True),
    "cyrillic-es": ("### [S2] \N{CYRILLIC CAPITAL LETTER ES}RITI"
                    "\N{CYRILLIC CAPITAL LETTER ES}AL - remote code "
                    "execution", True),
    "fullwidth": ("### [S2] \N{FULLWIDTH LATIN CAPITAL LETTER H}"
                  "\N{FULLWIDTH LATIN CAPITAL LETTER I}"
                  "\N{FULLWIDTH LATIN CAPITAL LETTER G}"
                  "\N{FULLWIDTH LATIN CAPITAL LETTER H} - upload", True),
    "hangul-filler": ("Rated\N{HANGUL FILLER}HIGH remote code execution.",
                      True),
    "clean": ("- **Status:** \N{WHITE HEAVY CHECK MARK} fixed \N{EM DASH} "
              "nothing else", False),
}


def review(body: str) -> str:
    """A reviewer artifact with one LOW finding, ``body`` on line 8 and a
    footer that counts only the LOW."""
    return as_reviewer_artifact(
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


def refused_detail(named: str) -> str:
    return (f"{loops.REVIEW_UNICODE_DATA_REASON}: {named} (name the "
            f"character, never paste it)")


class _UnicodeDataBeforeTheTable:
    """``unicodedata`` as an interpreter that predates every character
    outside the gate's table reads it: each of them is unassigned (category
    Cn, no decomposition, left alone by every normalisation form). Table
    characters read as the running interpreter reads them."""

    unidata_version = unicodedata.unidata_version

    def __getattr__(self, name: str) -> Any:
        return getattr(unicodedata, name)

    @staticmethod
    def category(char: str) -> str:
        if loops._in_gate_unicode_table(char):
            return unicodedata.category(char)
        return "Cn"

    @staticmethod
    def decomposition(char: str) -> str:
        if loops._in_gate_unicode_table(char):
            return unicodedata.decomposition(char)
        return ""

    @staticmethod
    def normalize(form: str, text: str) -> str:
        pieces: list[str] = []
        run: list[str] = []
        for char in text:
            if loops._in_gate_unicode_table(char):
                run.append(char)
                continue
            pieces.append(unicodedata.normalize(form, "".join(run)))
            pieces.append(char)
            run = []
        pieces.append(unicodedata.normalize(form, "".join(run)))
        return "".join(pieces)

    def is_normalized(self, form: str, text: str) -> bool:
        return self.normalize(form, text) == text


# Every table the gate fills per character or reference seen; a decision
# under other Unicode data must not reuse what the last one cached.
_GATE_TABLES = ("_BACKSTOP_CHARACTERS", "_BACKSTOP_SPELLINGS",
                "_BACKSTOP_SEPARATORS", "_BACKSTOP_SEPARATED_SPELLINGS")
_GATE_REFERENCE_TABLES = ("_RENDERED_REFERENCES", "_HTML_BLOCK_REFERENCES",
                          "_BACKSTOP_REFERENCES")


def _fresh_gate_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GATE_TABLES:
        monkeypatch.setattr(loops, name, type(getattr(loops, name))())
    for name in _GATE_REFERENCE_TABLES:
        table = getattr(loops, name)
        monkeypatch.setattr(loops, name, type(table)(table._decode))


def _decision(text: str, monkeypatch: pytest.MonkeyPatch) -> tuple:
    _fresh_gate_tables(monkeypatch)
    decision, analysis = decision_and_analysis(text)
    return (decision.provenance.trusted, decision.blocks, decision.counts,
            analysis.verdict, analysis.detail)


@pytest.mark.parametrize("name", sorted(REFUSED_BODIES))
def test_a_reported_body_is_refused_alike_on_every_interpreter(name):
    body, named = REFUSED_BODIES[name]
    decision, analysis = decision_and_analysis(review(body))
    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE
    assert analysis.detail == refused_detail(named)


@pytest.mark.parametrize("name", sorted(PARSED_BODIES))
def test_a_body_of_table_characters_is_parsed(name):
    body, blocks = PARSED_BODIES[name]
    decision, analysis = decision_and_analysis(review(body))
    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks is blocks, (analysis.verdict, analysis.detail)
    assert analysis.verdict != loops.REVIEW_VERDICT_INCOMPLETE


@pytest.mark.parametrize("name", sorted(REFUSED_BODIES) + sorted(PARSED_BODIES))
def test_the_decision_is_the_same_with_older_unicode_data(name, monkeypatch):
    body = {**REFUSED_BODIES, **PARSED_BODIES}[name][0]
    text = review(body)
    current = _decision(text, monkeypatch)
    older = _UnicodeDataBeforeTheTable()
    monkeypatch.setattr(loops, "unicodedata", older)
    monkeypatch.setattr(security_gate, "unicodedata", older)
    assert _decision(text, monkeypatch) == current


def test_the_check_reads_no_unicode_data(monkeypatch):
    """The refusal comes from the checked-in table alone: with every
    ``unicodedata`` function failing, it names the same characters."""

    class Unreadable:
        unidata_version = unicodedata.unidata_version

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"unicodedata.{name} read")

    monkeypatch.setattr(loops, "unicodedata", Unreadable())
    text = f"ok \N{EM DASH} fine\n&#x2014;\n{UNASSIGNED} &#xA7F2;"
    assert (loops._character_outside_gate_unicode_data(text, set(text))
            == "U+0378 at line 3")
    assert loops._character_outside_gate_unicode_data(
        "ok \N{EM DASH} &#x2014;", set("ok \N{EM DASH} &#x2014;")) is None


@pytest.mark.parametrize("version", ["12.1.0", "3.2.0", "13.0.0b1", ""])
def test_an_interpreter_older_than_the_table_parses_no_non_ascii_review(
        version, monkeypatch):
    class Older(_UnicodeDataBeforeTheTable):
        unidata_version = version

    monkeypatch.setattr(loops, "unicodedata", Older())
    suffix = (f": the interpreter's Unicode data {version} predates 13.0.0 "
              f"(name the character, never paste it)")
    decision, analysis = decision_and_analysis(
        review("- **Status:** fixed \N{EM DASH} nothing else"))
    assert decision.blocks
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE
    assert analysis.detail == (f"{loops.REVIEW_UNICODE_DATA_REASON}: "
                               f"U+2014 at line 8" + suffix)
    decision, analysis = decision_and_analysis(
        review("- **Status:** fixed &#x2014; nothing else"))
    assert decision.blocks
    assert analysis.detail == (f"{loops.REVIEW_UNICODE_DATA_REASON}: "
                               f"U+2014 reference at line 8" + suffix)
    # ASCII reads the same in every Unicode version.
    decision = production_decision(
        review("- **Status:** fixed &#45; nothing else"))
    assert decision.provenance.trusted and not decision.blocks


def test_the_current_interpreter_is_not_older_than_the_table():
    assert loops._gate_unicode_data_is_current()


@pytest.mark.parametrize("text, named", [
    # Every line-break form counts as one break, as in find_bidi_control.
    (f"a\rb\N{LINE SEPARATOR}c\r\n{C}", "U+A7F2 at line 4"),
    (f"a\x0bb\x0cc\x85d\N{PARAGRAPH SEPARATOR}{C}", "U+A7F2 at line 5"),
    # Whichever comes first, a character or a reference to one.
    (f"&#xA7F3;\n{C}", "U+A7F3 reference at line 1"),
    (f"{C}\n&#xA7F3;", "U+A7F2 at line 1"),
    # A flood of table references, then one outside.
    ("&#10;" * 500 + "&#x0378;", "U+0378 reference at line 1"),
    # Leading zeros, upper-case X, no semicolon.
    ("x&#X000A7F4y", "U+A7F4 reference at line 1"),
    (f"{CJK_EXTENSION_H}{UNASSIGNED}", "U+31350 at line 1"),
])
def test_the_first_outside_character_is_named(text, named):
    characters = None if text.isascii() else set(text)
    assert loops._character_outside_gate_unicode_data(text, characters) == named


@pytest.mark.parametrize("text", [
    "&#65;&#x41;&#0;&#xD800;&#x110000;&#99999999999999;&#x3FFFF;&#128;",
    "&#x" + "0" * 50_000 + "41;",
    "&#" + "9" * 50_000,
    "plain text, &amp; &copy; &#x2014; \N{EM DASH}",
])
def test_references_to_table_characters_are_read(text):
    """Overlong, surrogate, noncharacter and Windows-1252 references decode
    (as html.unescape does) to table characters or to nothing."""
    characters = None if text.isascii() else set(text)
    assert loops._character_outside_gate_unicode_data(text, characters) is None


def _table_code_points() -> list[int]:
    edges = loops._GATE_UNICODE_EDGES
    return [code_point
            for start, end in zip(edges[::2], edges[1::2])
            for code_point in range(start, end)]


_WORD = re.compile(r"\w")
_SPACE = re.compile(r"\s")
_DIGIT = re.compile(r"\d")


def _unicode_properties(code_point: int) -> tuple:
    """Every property of one character the gate, ``re`` and ``str`` read."""
    char = chr(code_point)
    return (
        unicodedata.category(char), unicodedata.bidirectional(char),
        unicodedata.combining(char), unicodedata.east_asian_width(char),
        unicodedata.mirrored(char), unicodedata.decomposition(char),
        unicodedata.decimal(char, -1), unicodedata.digit(char, -1),
        unicodedata.numeric(char, -1), unicodedata.name(char, ""),
        unicodedata.normalize("NFC", char), unicodedata.normalize("NFD", char),
        unicodedata.normalize("NFKC", char),
        unicodedata.normalize("NFKD", char),
        char.isalpha(), char.isalnum(), char.isdecimal(), char.isdigit(),
        char.isnumeric(), char.isspace(), char.isprintable(),
        char.isidentifier(), char.isupper(), char.islower(), char.istitle(),
        char.upper(), char.lower(), char.casefold(), char.title(),
        char.swapcase(), bool(_WORD.match(char)), bool(_SPACE.match(char)),
        bool(_DIGIT.match(char)), _sre.unicode_tolower(code_point),
        _sre.unicode_iscased(code_point),
        len(("a" + char + "b").splitlines()),
    )


# Computed on Python 3.10.20 (Unicode 13.0.0) and 3.12.3 (Unicode 15.0.0):
# the same on both. A new interpreter whose data differs for a table
# character fails here; drop that code point from the table (the gate then
# refuses it) and pin the new digest.
TABLE_CODE_POINTS = 283_437
TABLE_PROPERTY_DIGEST = (
    "960e588265cb11d1723da41f00bb9704a961112d4290431ee81aebbcbf41c319"
)


def test_every_table_character_has_the_pinned_unicode_data():
    digest = hashlib.sha256()
    code_points = _table_code_points()
    for code_point in code_points:
        digest.update(repr((code_point, _unicode_properties(code_point)))
                      .encode("utf-8", "surrogatepass"))
    assert len(code_points) == TABLE_CODE_POINTS
    assert digest.hexdigest() == TABLE_PROPERTY_DIGEST


# Assigned code points outside the table, per Unicode version: the three
# changed ones, plus every code point Unicode 14 and 15 assigned.
ASSIGNED_OUTSIDE_THE_TABLE = {"13.0.0": 3, "15.0.0": 5_330}


def test_the_table_is_unicode_13_without_the_changed_code_points():
    table = set(_table_code_points())
    assigned = {code_point for code_point in range(0x110000)
                if unicodedata.category(chr(code_point)) != "Cn"}
    assert table <= assigned
    assert table.isdisjoint(CHANGED_BY_UNICODE_15)
    assert set(CHANGED_BY_UNICODE_15) <= assigned
    expected = ASSIGNED_OUTSIDE_THE_TABLE.get(unicodedata.unidata_version)
    if unicodedata.unidata_version == "13.0.0":
        assert table == assigned - set(CHANGED_BY_UNICODE_15)
    if expected is not None:
        assert len(assigned - table) == expected
    assert list(loops._GATE_UNICODE_EDGES) == sorted(
        set(loops._GATE_UNICODE_EDGES))


def test_named_and_replaced_references_decode_to_table_characters():
    """The check decodes numeric references only: every named reference,
    and every character html.unescape puts in place of a numeric one, is a
    table character."""
    decoded = "".join(html.entities.html5.values())
    decoded += "".join(html._invalid_charrefs.values())  # Windows-1252 fixes
    decoded += "\N{REPLACEMENT CHARACTER}"
    assert all(loops._in_gate_unicode_table(char) for char in decoded)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        repr(value).encode("utf-8", "surrogatepass")).hexdigest()


def _extra_case_table() -> dict[int, tuple[int, ...]]:
    """sre's IGNORECASE equivalences beyond lower-casing each character (for
    example LATIN SMALL LETTER LONG S matching "s"): ``re._casefix`` from
    Python 3.11 on, ``sre_compile._ignorecase_fixes`` before."""
    try:
        from re import _casefix
    except ImportError:  # Python 3.10
        import sre_compile
        return dict(sre_compile._ignorecase_fixes)
    return dict(_casefix._EXTRA_CASES)


_SIGMA = "\N{GREEK CAPITAL LETTER SIGMA}"


def _case_context(char: str) -> tuple[str, ...]:
    """How ``str.lower`` reads ``char`` as a neighbour of a capital sigma
    (final or not): it reads the Cased and Case_Ignorable properties, which
    no single-character result shows."""
    return ((char + _SIGMA).lower(), ("A" + char + _SIGMA).lower(),
            ("A" + _SIGMA + char).lower(), ("A" + _SIGMA + char + "B").lower())


# Interpreter data the gate reads besides each character's own properties,
# computed on Python 3.10.20 and 3.12.3: the same on both. The named
# character references (``html.unescape`` in the rendered views), the
# IGNORECASE equivalences (46 IGNORECASE patterns in loops.py) and the case
# context of every table character. A new interpreter that changes one of
# them changes what a review reads as; it fails here instead.
NAMED_REFERENCES_DIGEST = (
    "cce279ce7b21206dbf91830cf183e501355914670261c1b05458c07310915de7"
)
EXTRA_CASES_DIGEST = (
    "e8c106a563d19c0a98a3766224cc5ce9b4ccd95ab55cea3ace5d2be2ac9c0f97"
)
CASE_CONTEXT_DIGEST = (
    "f7ce99e0abe99117a68bfbb6529103732ee0fb5cf71f439b2ace72b52b36d394"
)


def test_the_named_references_and_case_tables_are_pinned():
    assert len(html.entities.html5) == 2_231
    assert _sha256(sorted(html.entities.html5.items())) == (
        NAMED_REFERENCES_DIGEST)
    extra_cases = _extra_case_table()
    assert len(extra_cases) == 50
    assert all(map(loops._in_gate_unicode_table,
                   map(chr, itertools.chain(extra_cases, *extra_cases.values()))))
    assert _sha256(sorted(extra_cases.items())) == EXTRA_CASES_DIGEST
    contexts = [_case_context(chr(code_point))
                for code_point in _table_code_points()]
    assert _sha256(contexts) == CASE_CONTEXT_DIGEST


# The size of the timed reference bodies: each body below is built at a
# size, and at this one it is the 200 KB body the timing test was written
# with.
REFERENCES_SIZE = 200_000

# Distinct references to table characters (CJK ideographs and the Hangul
# syllables, all assigned before Unicode 13) fill a 200 KB review: the
# check decodes each spelling once.
_DISTINCT_REFERENCE_CODE_POINTS = tuple(itertools.chain(
    range(0x4E00, 0x4E00 + 14_000), range(0xAC00, 0xD7A4)))


def distinct_references(size: int = REFERENCES_SIZE) -> str:
    """The first ``size / REFERENCES_SIZE`` of the distinct references:
    at ``REFERENCES_SIZE``, all 25,172 of them."""
    count = len(_DISTINCT_REFERENCE_CODE_POINTS) * size // REFERENCES_SIZE
    return "".join(f"&#x{code_point:X};"
                   for code_point in _DISTINCT_REFERENCE_CODE_POINTS[:count])


DISTINCT_REFERENCES = distinct_references()

REFERENCE_BODIES = {
    "distinct-references": distinct_references,
    "one-reference-flood": lambda size: "&#x2014;" * (size // 8),
    "zero-run": lambda size: "&#" + "0" * size + "65;",
}


@timing_test
@pytest.mark.parametrize("name", list(REFERENCE_BODIES))
def test_200kb_of_references_is_checked_in_time(name):
    """Timed through the merge gate. The 0.5 s budget is scaled by the host
    factor and the time must grow linearly from 50 KB to 200 KB (task 3174,
    as task 3171 requires of every timing test)."""
    build = REFERENCE_BODIES[name]
    text = review(build(REFERENCES_SIZE))
    assert len(text) > 200_000
    assert loops._character_outside_gate_unicode_data(text, None) is None

    def seconds_at(size: int) -> float:
        sized = review(build(size))
        assert loops._character_outside_gate_unicode_data(sized, None) is None
        return production_seconds(sized)

    assert_linear_time(seconds_at, REFERENCES_SIZE, 0.5, name)


def test_references_to_table_code_points_are_not_decoded(monkeypatch):
    """Cycle 3 (3.10 timing): the check decoded every distinct spelling with
    ``html.unescape``, 0.09 s for 22,000 of them, and the 3143 family
    eta_then_distinct_reference took 0.51 s in the 3.10 full suite. A
    reference to a table code point is read from its digits; only one to
    another code point is decoded."""
    decoded: list[str] = []
    unescape = html.unescape

    def counting_unescape(text: str) -> str:
        decoded.append(text)
        return unescape(text)

    monkeypatch.setattr(html, "unescape", counting_unescape)
    assert loops._character_outside_gate_unicode_data(
        DISTINCT_REFERENCES, None) is None
    assert decoded == []
    text = DISTINCT_REFERENCES + "\n&#xA7F2;"
    assert (loops._character_outside_gate_unicode_data(text, None)
            == "U+A7F2 reference at line 2")
    assert "&#xA7F2;" in decoded


def _outside_after_decoding(text: str) -> str | None:
    """The check's expected answer for ``text`` holding one reference and
    otherwise ASCII: the first character of ``html.unescape(text)`` outside
    the table, named as the check names a reference."""
    for char in html.unescape(text):
        if not loops._in_gate_unicode_table(char):
            return f"U+{ord(char):04X} reference at line 1"
    return None


# Both sides of every table range edge, every code point html.unescape shows
# as something other than its own character (NUL, the surrogates, the C1
# controls, the controls and noncharacters it drops), and the first code
# points past U+10FFFF and the largest a short digit run names.
REFERENCE_EDGE_CODE_POINTS = sorted(
    {code_point + offset
     for code_point in loops._GATE_UNICODE_EDGES for offset in (-1, 0)
     if code_point + offset >= 0}
    | {0, 0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0x10FFFF, 0x110000, 0xFFFFFF,
       9_999_999}
    | set(html._invalid_charrefs) | set(html._invalid_codepoints)
)


@pytest.mark.parametrize("form", ["&#{};", "&#x{:X};", "&#00{}", "&#X0{:x}"])
def test_a_reference_is_refused_exactly_when_its_decoding_is(form):
    """Skipping the decoding of a reference to a table code point gives the
    verdict of decoding it, at every edge where the two could differ."""
    differ = []
    for code_point in REFERENCE_EDGE_CODE_POINTS:
        text = f"a {form.format(code_point)} b"
        expected = _outside_after_decoding(text)
        if loops._references_name_only_table_code_points(text):
            assert expected is None, text
        found = loops._character_outside_gate_unicode_data(text, None)
        if found != expected:
            differ.append((text, found, expected))
    assert differ == []
    assert len(REFERENCE_EDGE_CODE_POINTS) > 1_500


def test_the_decimal_and_hex_halves_find_the_checked_references():
    """_GATE_DECIMAL_REFERENCE_RE and _GATE_HEX_REFERENCE_RE find the
    references and digits _GATE_NUMERIC_REFERENCE_RE finds, so the read
    without decoding covers every reference the decoding would."""
    generator = random.Random(3172)
    alphabet = "&&##xX0019aAfFg; "
    for _ in range(20_000):
        text = "".join(generator.choice(alphabet)
                       for _ in range(generator.randrange(1, 20)))
        both = loops._GATE_NUMERIC_REFERENCE_RE.findall(text)
        assert loops._GATE_DECIMAL_REFERENCE_RE.findall(text) == [
            decimal for decimal, _ in both if decimal], text
        assert loops._GATE_HEX_REFERENCE_RE.findall(text) == [
            hexadecimal for _, hexadecimal in both if hexadecimal], text
