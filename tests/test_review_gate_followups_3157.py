"""Task 3157: follow-ups of the independent review of task 3154.

* R3154-02: a count before the MEDIUM word and a zero tally after it ("Found
  2 MEDIUM: 0 open.") untrust the review again, as on ba6065a, which read
  the count before the word first. The larger of the two counts is used.
  (Task 3161 deleted the MEDIUM exemptions: every such tally now blocks,
  and the tests that kept MEDIUM tallies and negations merging assert the
  block instead.)
* R3154-03: a lookalike letter is read as its letter inside a severity
  word only. Task 3154 folded every Unicode confusable of the letters of
  the three words to an ASCII capital in every view, so "ne<U+2113>ther",
  "w<U+2113>thout" and "no" + CJK U+4E05 (drawn as T) became the
  case-insensitive negations "neither", "without" and "not".
* Acceptance: the probe corpus of the independent 3154 review, rebuilt by
  scripts/review_gate_probe_corpus.py for every severity word, is replayed
  here. Nothing ba6065a, main (3afec74) or the 3154 tree (cb3373c) blocked
  may merge (tests/fixtures/review_gate_probe_corpus_3157.json, written by
  scripts/review_gate_differential.py --families --write-fixture). Task
  3161 added the split, label and private-use shapes of the independent
  3157 review (R3157-01, R3157-02) and main before it (502975b).
* R3154-01, I3154-01: circled, squared, parenthesised and modifier capitals
  are letters of a severity word next to a filler or a number form.
* R3154-04: the two title-case HTML bodies of R3152-04 block.
* R3154-05: the shared lookup tables start over when full, so one hostile
  review does not slow every later one.
* R3154-06: the confusables table is checked against properties the
  generator's table code does not produce.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import re
import unicodedata
from pathlib import Path

import pytest

from equipa import loops
from equipa.severity_confusables import (
    CONFUSABLES_EXTRACT_SHA256,
    CONFUSABLES_LETTER_MAPPINGS,
    SEVERITY_LETTER_CONFUSABLES,
)
from tests.host_timing import assert_linear_time
from tests.review_gate_production import production_seconds
from tests.review_gate_timing import timing_test
from tests.test_review_gate_no_exemptions_3152 import (
    CONTEXTS,
    RB,
    ZERO,
    analyze,
    build_review,
    gate_blocks,
    review,
)

REPO = Path(__file__).resolve().parent.parent
TAIL = ": SQL injection in login.py allows auth bypass."
FILLER = chr(0x3164)  # HANGUL FILLER, drawn as a blank
SUPERSCRIPT_ONE = chr(0x00B9)


def _medium_untrusts(text: str) -> bool:
    """True when the review is not trusted (a MEDIUM word only untrusts it;
    the backstop or a shape rule may be the one that does)."""
    return not analyze(text).trusted


# --- R3154-02: a count before the word is its count -----------------------------

COUNT_BEFORE_BODIES = [
    "Found 2 MEDIUM: 0 open.",
    "| 2 MEDIUM | 0 |",
    "- 3 MEDIUM: 0",
    "2 MEDIUM: 0",
    "1 MEDIUM = 0",
    "two MEDIUM = 0",
    "one MEDIUM | 0 |",
    "N=2 MEDIUM: 0 | LOW: 0",
    "#2 MEDIUM:0",
    "count 2 MEDIUM =0",
    "Found two MEDIUM: 0.",
    "3 MEDIUM: 00",
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", COUNT_BEFORE_BODIES)
def test_a_count_before_the_word_is_not_undone_by_a_zero_tally(body, context):
    assert _medium_untrusts(build_review([body], context)), body


def _blocks_on_medium(text: str) -> bool:
    """True when the review is untrusted for an unaccounted MEDIUM word."""
    analysis = analyze(text)
    return (analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
            and analysis.detail.startswith(
                loops.backstop_reason("MEDIUM") + " at line "))


# Task 3161: the tally counting that read these is deleted with the other
# MEDIUM exemptions; each tally blocks whatever its counts, and merges in
# lower case.
@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("written", [
    "Found 2 MEDIUM: 0 open.", "0 MEDIUM: 2", "MEDIUM: 0", "0 MEDIUM: 0",
    "two MEDIUM = 1", "No MEDIUM: 0.",
])
def test_a_medium_tally_blocks_whatever_its_counts(written, context):
    assert _blocks_on_medium(build_review([written], context)), written
    lower = build_review([written.replace("MEDIUM", "medium")], context)
    assert analyze(lower).trusted, written


def test_a_counted_medium_tally_blocks_and_merges_in_lower_case():
    """The tally merged with the review's two counted MEDIUM findings
    (task 3157); with no exemption it blocks, and merges in lower case."""
    footer = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 2 | LOW: 0 | INFO: 0"
    body = [
        "### [M1] MEDIUM \N{EM DASH} weak hash in auth.py", "Details.", "",
        "### [M2] MEDIUM \N{EM DASH} verbose errors in app.py", "Details.", "",
        "## Notes", "", "Found 2 MEDIUM: 0 open.",
    ]
    assert _blocks_on_medium(review("2 findings.", body, footer))
    body[-1] = "Found 2 medium: 0 open."
    analysis = analyze(review("2 findings.", body, footer))
    assert analysis.trusted, analysis.detail
    assert analysis.counts["MEDIUM"] == 2


# --- R3154-03: lookalikes are letters inside a severity word only ---------------

# Drawn as a small l (the prototype of I in the Unicode confusables data).
L_LOOKALIKES = (0x2113, 0x217C, 0x05D5, 0x05DF, 0x0627, 0x0661, 0x06F1,
                0x16D0, 0x2D4A, 0x1D425, 0x1D7CF)
I_NEGATIONS = ("ne{}ther MEDIUM nor LOW findings.",
               "Shipped w{}thout MEDIUM findings.",
               "noth{}ng MEDIUM was found.",
               "none {}s MEDIUM.")
# Drawn as a capital T.
T_LOOKALIKES = (0x4E05, 0x3112, 0x07E0, 0xA50B, 0x10297, 0x16F0A)
T_NEGATIONS = ("no{} MEDIUM findings.",
               "no{} MEDIUM or LOW findings.",
               "ra{}her than MEDIUM findings.",
               "o{}her than MEDIUM.")
OTHER_EXEMPTION_WORDS = [
    # Cherokee GV (E), Cherokee E (R), Canadian TA (C), Lisu LA (L).
    f"z{chr(0x13AC)}ro MEDIUM findings.",
    f"neve{chr(0x13A1)} MEDIUM findings.",
    f"No MEDIUM be{chr(0x1455)}ause it needs a login.",
    f"{chr(0xA4E1)}ess than MEDIUM findings.",
    f"{chr(0x2113)}ower than MEDIUM findings.",
]
NEGATION_FOLD_BODIES = (
    [shape.format(chr(code_point)) for code_point in L_LOOKALIKES
     for shape in I_NEGATIONS]
    + [shape.format(chr(code_point)) for code_point in T_LOOKALIKES
       for shape in T_NEGATIONS]
    + OTHER_EXEMPTION_WORDS
)


def test_the_probe_lookalikes_are_folded_letters():
    folds = loops._BACKSTOP_LETTER_FOLDS
    assert all(folds.get(code_point) == "I" for code_point in L_LOOKALIKES)
    assert all(folds.get(code_point) == "T" for code_point in T_LOOKALIKES)


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", NEGATION_FOLD_BODIES)
def test_a_lookalike_never_spells_an_exemption_word(body, context):
    assert _medium_untrusts(build_review([body], context)), ascii(body)


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", [
    "neither MEDIUM nor LOW findings.",
    f"no MED{chr(0x2113)}UM findings.",
    f"No {chr(0x13B7)}EDIUM issues were found.",
])
def test_a_negated_word_blocks_with_or_without_a_lookalike(body, context):
    """These merged under the MEDIUM negation exemption (task 3157). Task
    3161 deleted it: a negated MEDIUM, spelled with lookalikes or not,
    blocks."""
    assert _blocks_on_medium(build_review([body], context)), ascii(body)


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", [
    f"MED{chr(0x2113)}UM: SQL injection in login.py.",
    f"H{chr(0x2113)}GH: SQL injection in login.py.",
    f"CR{chr(0x05D5)}T{chr(0x0627)}CAL: code execution in app/upload.py:7.",
    f"HI{chr(0x13C0)}H{TAIL}",
])
def test_a_lookalike_still_spells_the_word(body, context):
    assert gate_blocks(build_review([body], context)), ascii(body)


def test_a_lookalike_is_a_mark_only_the_severity_words_read():
    """Task 3161: every folded lookalike is the FOLD mark of its letter,
    fullwidth capitals included, and only lookalikes of the letters the
    severity words hold are folded (no other word is read any more)."""
    for code_point, letter in ((0x2113, "I"), (0xFF34, "T"), (0x0399, "I")):
        mark = loops._BACKSTOP_VIEW_FOLDS[code_point]
        assert loops._backstop_is_fold_mark(mark), hex(code_point)
        assert loops._backstop_unmarked(mark) == letter, hex(code_point)
    # Greek NU and OMICRON, Cyrillic small O and the parenthesised N.
    for code_point in (0x039D, 0x039F, 0x043E, 0x1F11D):
        assert code_point not in loops._BACKSTOP_LETTER_FOLDS, hex(code_point)
    assert set(loops._BACKSTOP_LETTER_FOLDS.values()) <= set("CRITALHGMEDUl")


def test_a_private_use_character_as_written_is_never_a_letter():
    # U+E048 is the FOLD mark of H: as written it must not spell HIGH.
    body = [f"{chr(0xE048)}IGH and &#xE048;IGH are private-use text."]
    assert not gate_blocks(build_review(body, "zero"))


# --- Acceptance: the probe corpus of the independent 3154 review -----------------

CORPUS_FIXTURE = REPO / "tests" / "fixtures" / "review_gate_probe_corpus_3157.json"
BASELINE_TREES = ("ba6065a", "3afec74", "cb3373c", "502975b")
FAMILIES_OF_3161 = ("split", "noun", "comma", "list", "private-use")


@functools.lru_cache(maxsize=1)
def _differential():
    path = REPO / "scripts" / "review_gate_differential.py"
    spec = importlib.util.spec_from_file_location("review_gate_diff_3157", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _corpus():
    """(bodies, texts, fixture): every probe body, its review texts (body by
    body, context by context) and the older trees' verdicts."""
    differential = _differential()
    bodies = differential.probe_corpus_bodies()
    texts = differential.family_texts(bodies, build_review, CONTEXTS)
    fixture = json.loads(CORPUS_FIXTURE.read_text(encoding="ascii"))
    return bodies, texts, fixture


def test_the_corpus_is_the_one_the_older_trees_judged():
    bodies, texts, fixture = _corpus()
    assert fixture["bodies"] == len(bodies)
    assert tuple(fixture["contexts"]) == CONTEXTS
    # Byte for byte the texts the older trees judged.
    assert _differential().corpus_digest(texts) == fixture["corpus_sha256"]
    assert set(BASELINE_TREES) <= set(fixture["blocked"])
    families = {key.split("|", 1)[0] for key, _, _ in bodies}
    assert families == {"replay", "separator", "tally", "negation", "split",
                        "noun", "comma", "list", "private-use"}


def _must_block(severity: str) -> list[tuple[int, int, list[str]]]:
    """(body, context, trees) of every text of ``severity`` some older tree
    blocked, in the families of task 3157."""
    bodies, _, fixture = _corpus()
    decode = _differential().decode_bits
    blocked = {
        (tree, context): decode(fixture["blocked"][tree][context], len(bodies))
        for tree in BASELINE_TREES for context in CONTEXTS
    }
    rows = []
    for index, (key, body_severity, _) in enumerate(bodies):
        # Every text of the families task 3161 added, blocked by an older
        # tree or not, is judged by tests/test_review_gate_no_exemptions_3161
        # .py (test_every_text_of_the_3161_families_blocks).
        if (body_severity != severity
                or key.split("|", 1)[0] in FAMILIES_OF_3161):
            continue
        for offset, context in enumerate(CONTEXTS):
            trees = [tree for tree in BASELINE_TREES
                     if blocked[(tree, context)][index]]
            if trees:
                rows.append((index, offset, trees))
    return rows


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH", "MEDIUM", "other"])
def test_nothing_an_older_tree_blocked_merges(severity):
    bodies, texts, _ = _corpus()
    rows = _must_block(severity)
    # Every severity word has texts ba6065a blocked.
    assert sum("ba6065a" in trees for *_, trees in rows) >= (
        30 if severity == "other" else 1000)
    newly_pass = [
        f"{bodies[index][0]} {CONTEXTS[offset]} (blocked by {','.join(trees)})"
        for index, offset, trees in rows
        if not gate_blocks(texts[index * len(CONTEXTS) + offset])
    ]
    assert not newly_pass, (len(newly_pass), newly_pass[:20])


# --- R3154-01, I3154-01: enclosed and modifier capitals --------------------------

def _styled(word: str, first: int) -> str:
    return "".join(chr(first + ord(letter) - ord("A")) for letter in word)


MODIFIER_CAPITALS = {"G": 0x1D33, "H": 0x1D34, "I": 0x1D35}
ENCLOSED_BODIES = [
    f"Rated{FILLER}{_styled('HIGH', 0x24B6)}{TAIL}",
    f"Rated&#x3164;{_styled('HIGH', 0x24B6)}{TAIL}",
    f"{_styled('HIGH', 0x24B6)}{SUPERSCRIPT_ONE}{TAIL}",
    f"{_styled('CRITICAL', 0x24B6)}{chr(0x00B2)} remote code execution in "
    f"upload.py",
    f"The {_styled('HIGH', 0x24B6)}{FILLER}issue{TAIL}",
    f"Rated{FILLER}{_styled('HIGH', 0x1F130)}{SUPERSCRIPT_ONE}{TAIL}",
    f"Rated{FILLER}"
    + "".join(chr(MODIFIER_CAPITALS[letter]) for letter in "HIGH")
    + f"{SUPERSCRIPT_ONE}{TAIL}",
    f"HIG{chr(0x24BD)}{SUPERSCRIPT_ONE}{TAIL}",
    f"Rated{FILLER}{chr(0x24BD)}IGH{TAIL}",
    # All ASCII as written.
    f"Rated&#x3164;&#x24BD;&#x24BE;&#x24BC;&#x24BD;{TAIL}",
    f"&#x24BD;&#x24BE;&#x24BC;&#x24BD;&sup1;{TAIL}",
    # I3154-01: parenthesised capitals, next to a plain space too.
    f"{_styled('HIGH', 0x1F110)}{TAIL}",
    f"The {_styled('CRITICAL', 0x1F110)} issue: code execution in upload.py.",
    f"Rated{FILLER}{_styled('HIGH', 0x1F110)}{TAIL}",
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", ENCLOSED_BODIES)
def test_an_enclosed_or_modifier_capital_is_a_letter(body, context):
    assert gate_blocks(build_review([body], context)), ascii(body)


def _single_letter_code_points() -> dict[int, str]:
    """Every code point below U+20000 that is no cased letter and that NFKD
    reads as one capital of the three words alone, and the parenthesised
    capitals of those letters."""
    found = {}
    for code_point in range(0x80, 0x20000):
        if 0xD800 <= code_point <= 0xDFFF:
            continue
        char = chr(code_point)
        if unicodedata.category(char) in ("Lu", "Ll", "Lt"):
            continue
        letters = [part for part in unicodedata.normalize("NFKD", char)
                   if unicodedata.category(part) not in ("Mn", "Me", "Cf")]
        if len(letters) == 1 and letters[0] in "CRITALHGMEDU":
            found[code_point] = letters[0]
    for letter in "CRITALHGMEDU":
        found[0x1F110 + ord(letter) - ord("A")] = letter
    return found


SEPARATOR_CLASSES = [FILLER, "&#x3164;", chr(0x0378), chr(0x200B),
                     SUPERSCRIPT_ONE, chr(0x00BD), chr(0x2161), chr(0x2460),
                     chr(0x00AA), chr(0xFF11), "&sup1;"]


def test_every_single_letter_form_spells_the_word_next_to_a_separator():
    code_points = _single_letter_code_points()
    # Circled, squared and modifier capitals and the Roman numerals I, C,
    # D, L and M are among them.
    assert {0x24BD, 0x1F137, 0x1D34, 0x2160, 0x216D} <= set(code_points)
    missed = []
    for code_point, letter in code_points.items():
        for word in ("CRITICAL", "HIGH", "MEDIUM"):
            index = word.find(letter)
            if index < 0:
                continue
            written = word[:index] + chr(code_point) + word[index + 1:]
            for separator in SEPARATOR_CLASSES:
                for line in (f"Rated{separator}{written}{TAIL}",
                             f"{written}{separator}{TAIL}"):
                    text = build_review([line], "zero")
                    if (_medium_untrusts(text) if word == "MEDIUM"
                            else gate_blocks(text)):
                        continue
                    missed.append(f"U+{code_point:04X} {ascii(line)}")
    assert not missed, (len(missed), missed[:20])


# --- R3154-04: title-case labels in HTML blocks -------------------------------------

TITLE_CASE_HTML_BODIES = [
    ["<p>&#72igh: SQL injection in login.py lets anyone in.</p>"],
    ["<p>&#x48igh: SQL injection in login.py lets anyone in.</p>"],
    ["<span>", "```", "Risk: High", "```", "</span>"],
    ['<span class="note" id=x>', "```", "Risk: High", "```", "</span>"],
    ["</section>", "~~~", "Severity: Critical", "~~~"],
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", TITLE_CASE_HTML_BODIES)
def test_a_title_case_label_in_an_html_block_blocks(body, context):
    assert gate_blocks(build_review(body, context)), body


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", [
    # A type 7 HTML block cannot interrupt a paragraph: the fence is code.
    ["Some text", "<span>", "```", "Risk: High", "```", "</span>"],
    # Markdown text needs the ";": the reference is shown as written.
    ["Text &#72igh: SQL injection in login.py."],
])
def test_markdown_outside_an_html_block_keeps_its_rendering(body, context):
    assert not gate_blocks(build_review(body, context)), body


@pytest.mark.parametrize("flood", [
    lambda rb: "<span " + " a=b" * (rb // 4) + ">",
    lambda rb: "<div>" + "&#1" * (rb // 3),
    lambda rb: "<span>\n" + "&#x4" * (rb // 4),
], ids=["span-attributes", "div-decimal-references", "span-hex-references"])
@timing_test
def test_html_block_floods_parse_in_half_a_second(flood):
    """Budget host-calibrated, growth from 50 KB to 200 KB linear (task
    3171)."""
    assert_linear_time(
        lambda rb: production_seconds(build_review([flood(rb)], "zero")),
        RB, 0.5, flood(8))


# --- R3154-05: the shared lookup tables start over when full ---------------------

@pytest.mark.parametrize("make_table,key_of", [
    (loops._BackstopSeparatorTable, lambda index: 0x20000 + index),
    (loops._BackstopCharacterTable, lambda index: 0x20000 + index),
])
def test_a_full_character_table_caches_the_next_review(monkeypatch, make_table,
                                                       key_of):
    monkeypatch.setattr(loops, "_BACKSTOP_TABLE_LIMIT", 64)
    table = make_table()
    for index in range(200):
        table[key_of(index)]
    assert len(table) <= 64
    newcomer = 0xAC00  # a Hangul syllable a later review holds
    value = table[newcomer]
    assert table.get(newcomer) == value


def test_a_full_reference_table_caches_the_next_review(monkeypatch):
    monkeypatch.setattr(loops, "_REFERENCE_TABLE_LIMIT", 64)
    table = loops._ReferenceTable(loops._backstop_reference_text)
    for index in range(200):
        table[f"&#{0x20000 + index};"]
    assert len(table) <= 64
    assert table["&#72;"] == "H" and table.get("&#72;") == "H"


def _distinct_review(first: int, count: int) -> str:
    """A review of ``count`` distinct code points from ``first`` on."""
    characters = "".join(chr(first + offset) for offset in range(count))
    lines = [characters[start:start + 100]
             for start in range(0, len(characters), 100)]
    return build_review(lines, "zero")


# Task 3161: a review of more than 4,096 distinct characters is not parsed,
# so the shared tables are filled by fifteen reviews just under that cap
# (60,000 distinct code points of CJK Extension B onwards, as one review of
# 61,000 did before).
HOSTILE_REVIEW_COUNT = 15
HOSTILE_REVIEW_DISTINCT = 4_000


def _hostile_reviews() -> list[str]:
    # Task 3172 (R3169-01): only code points of the gate's Unicode table; the
    # gaps Unicode 14 and 15 filled (U+2A6DE, U+2B735, U+2CEA2 ...) would
    # have each review refused before it fills any table.
    code_points = [code_point for code_point in range(0x20000, 0x30000)
                   if loops._in_gate_unicode_table(chr(code_point))]
    reviews = []
    for index in range(HOSTILE_REVIEW_COUNT):
        chunk = code_points[index * HOSTILE_REVIEW_DISTINCT:
                            (index + 1) * HOSTILE_REVIEW_DISTINCT]
        assert len(chunk) == HOSTILE_REVIEW_DISTINCT
        characters = "".join(map(chr, chunk))
        reviews.append(build_review(
            [characters[start:start + 100]
             for start in range(0, len(characters), 100)], "zero"))
    return reviews


def _hangul_review(rb: int = RB) -> str:
    """``rb`` bytes of Hangul syllables (decomposed by rule), 4,000 of them
    distinct (under the distinct-character cap)."""
    syllables = "".join(chr(0xAC00 + offset % 4000)
                        for offset in range(rb // 3))
    return build_review([syllables[start:start + 80]
                         for start in range(0, len(syllables), 80)], "zero")


def _fill_tables(hostile: list[str]) -> None:
    for text in hostile:
        analyze(text)


def test_the_hostile_reviews_fill_the_shared_tables(monkeypatch):
    # Task 3169: the table is shared by every test of an xdist worker, so
    # what it held before depended on which files the worker ran first; one
    # that already held more than 5,496 entries started over part way and
    # ended with about 16,000 (seen on a clean CI runner). The property is
    # that these reviews alone fill a table to near its limit, so they start
    # from an empty one, as in a fresh process (the shared one is restored).
    table = loops._BackstopCharacterTable()
    monkeypatch.setattr(loops, "_BACKSTOP_CHARACTERS", table)
    hostile = _hostile_reviews()
    assert all(analyze(text).verdict != loops.REVIEW_VERDICT_INCOMPLETE
               for text in hostile)
    hostile_code_points = {ord(char) for text in hostile for char in text
                           if ord(char) >= 0x20000}
    assert len(hostile_code_points) == (HOSTILE_REVIEW_COUNT
                                        * HOSTILE_REVIEW_DISTINCT)
    assert hostile_code_points <= table.keys()
    assert 50_000 <= len(table) <= loops._BACKSTOP_TABLE_LIMIT


@timing_test
@pytest.mark.parametrize("later", [
    _hangul_review,
    lambda rb: build_review([f"HIGH{SUPERSCRIPT_ONE} " * (rb // 6)], "zero"),
], ids=["hangul", "superscript-one"])
def test_a_hostile_review_does_not_slow_the_next_one(later):
    """Budget host-calibrated, growth of the later review from 50 KB to
    200 KB linear (task 3171)."""
    hostile = _hostile_reviews()
    assert_linear_time(
        lambda rb: production_seconds(
            later(rb), before=lambda: _fill_tables(hostile)),
        RB, 0.5, "after the hostile reviews")


@timing_test
def test_a_review_of_too_many_distinct_characters_fails_closed_fast():
    # 61,000 distinct code points: not parsed, so not trusted, in well under
    # the 0.5 s budget (task 3161 target: 0.25 s). A quarter of them is
    # still over the distinct-character cap (task 3171: growth linear).
    text = _distinct_review(0x20000, 61_000)
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE, analysis
    assert not analysis.trusted
    assert 61_000 // 4 > loops._REVIEW_MAX_DISTINCT_CHARACTERS
    assert_linear_time(
        lambda count: production_seconds(_distinct_review(0x20000, count)),
        61_000, 0.25, "distinct code points")


# --- R3154-06: the confusables table against independent properties --------------

CONFUSABLES_EXTRACT = (REPO / "tests" / "fixtures"
                       / "confusables_severity_letters.txt")
# "SOURCE ; TARGET ;" with one code point on each side; scanned here with
# no code of scripts/generate_severity_confusables.py.
MAPPING_LINE_RE = re.compile(
    r"^([0-9A-F]{4,6}) ;\t([0-9A-F]{4,6}) ;", re.MULTILINE)


def _prototype(letter: str) -> str:
    return "l" if letter == "I" else letter


def _extract_mappings() -> dict[int, str]:
    text = CONFUSABLES_EXTRACT.read_text(encoding="utf-8")
    return {int(source, 16): chr(int(target, 16))
            for source, target in MAPPING_LINE_RE.findall(text)}


def test_the_table_holds_every_mapping_the_source_file_counts():
    assert sum(map(len, SEVERITY_LETTER_CONFUSABLES.values())) == (
        CONFUSABLES_LETTER_MAPPINGS)
    listed = [code_point for code_points in SEVERITY_LETTER_CONFUSABLES.values()
              for code_point in code_points]
    assert len(set(listed)) == len(listed)


def test_the_extract_is_the_recorded_extract():
    data = CONFUSABLES_EXTRACT.read_bytes()
    assert hashlib.sha256(data).hexdigest() == CONFUSABLES_EXTRACT_SHA256
    prototypes = {_prototype(letter) for letter in "CRITALHGMEDU"}
    counted = sum(1 for source, target in _extract_mappings().items()
                  if source >= 0x80 and target in prototypes)
    assert counted == CONFUSABLES_LETTER_MAPPINGS


def test_every_listed_code_point_reads_as_its_letter():
    mappings = _extract_mappings()
    wrong = []
    for letter, code_points in SEVERITY_LETTER_CONFUSABLES.items():
        for code_point in code_points:
            nfkc = unicodedata.normalize("NFKC", chr(code_point))
            if nfkc in (letter, _prototype(letter)):
                continue
            if mappings.get(code_point) == _prototype(letter):
                continue
            wrong.append(f"U+{code_point:04X} as {letter}")
    assert not wrong, wrong


# Known lookalikes, from the Unicode charts, independent of the generator.
KNOWN_CONFUSABLES = {
    "A": (0x0391, 0x0410, 0x13AA, 0xA4EE, 0xFF21, 0x1D400),
    "C": (0x03F9, 0x0421, 0x13DF, 0x216D, 0x2CA4, 0xA4DA, 0xFF23),
    "D": (0x13A0, 0x216E, 0xA4D3, 0x1D403),
    "E": (0x0395, 0x0415, 0x13AC, 0x2D39, 0xA4F0),
    "G": (0x050C, 0x13C0, 0x13F3, 0xA4D6),
    "H": (0x0397, 0x041D, 0x13BB, 0x157C, 0x2C8E, 0xA4E7, 0x1D407),
    "I": (0x0399, 0x0406, 0x04C0, 0x2160, 0x2113, 0x05D5, 0x0627, 0x2223,
          0x2D4F, 0x16C1, 0xA4F2, 0x217C, 0x1D425, 0x0661, 0x06F1),
    "L": (0x13DE, 0x14AA, 0x216C, 0x2CD0, 0xA4E1),
    "M": (0x039C, 0x041C, 0x13B7, 0x15F0, 0x216F, 0xA4DF),
    "R": (0x13A1, 0x1587, 0xA4E3, 0x01A6),
    "T": (0x03A4, 0x0422, 0x13A2, 0x22A4, 0x4E05, 0x3112, 0xA4D4, 0x1D413,
          0x07E0, 0xA50B, 0x10297, 0x16F0A),
    "U": (0x054D, 0x144C, 0xA4F4, 0x1D414),
}


def test_known_confusables_are_listed():
    missing = [f"U+{code_point:04X} as {letter}"
               for letter, code_points in KNOWN_CONFUSABLES.items()
               for code_point in code_points
               if code_point not in SEVERITY_LETTER_CONFUSABLES[letter]]
    assert not missing, missing
