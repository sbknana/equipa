#!/usr/bin/env python3
"""Task 3154: follow-ups of the task 3152 review-gate backstop.

Findings of SECURITY-REVIEW-3152 (R3152-*) and of the independent review of
task 3152 (I3152-*), each with rows that fail before this task:

* R3152-02: the seven R3149-02 review shapes with the MEDIUM word are
  untrusted again, as on main (ba6065a): "below" and a negation ending the
  line before no longer excuse a MEDIUM word;
* R3152-03: the task text run_security_review gives every reviewer states
  the prompt's lower-case rule instead of "a severity in prose is fine";

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import unicodedata
from unittest.mock import MagicMock, patch

import pytest

from equipa import loops
from equipa.security_gate import normalize_review_text
from equipa.severity_confusables import (
    CONFUSABLES_VERSION,
    SEVERITY_LETTER_CONFUSABLES,
)
from tests.test_review_gate_no_exemptions_3152 import (
    CONTEXTS,
    E,
    REPO,
    analyze,
    build_review,
    gate_blocks,
)

# --- R3152-02: MEDIUM kept exactly main's exemptions (none since task 3161) -------

# The seven R3149-02 bodies with MEDIUM in place of CRITICAL or HIGH. Main
# blocks every one ("unaccounted severity token"); task 3152 trusted them
# with zero counts.
R3152_02_MEDIUM_BODIES = [
    ["The worst issue is described below MEDIUM severity stored XSS in "
     "app/view.py:7."],
    ["The worst issue is described below",
     "MEDIUM severity stored XSS in app/view.py:7."],
    ["Output encoding on the profile page: none",
     "MEDIUM, as any user stores script in app/view.py:7."],
    ["The profile form was reviewed. It was not",
     "MEDIUM. Stored XSS in /profile lets any user run script."],
    ["[a]: none",
     "MEDIUM issue was found in app/view.py:7 and stores script."],
    ["The profile handler was read.", "", "    no",
     "MEDIUM. Stored XSS in /profile lets any user run script."],
    ["The worst issue is described below  ",
     "MEDIUM issue was found in app/view.py:7."],
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", R3152_02_MEDIUM_BODIES)
def test_medium_r3149_02_shapes_are_untrusted_as_on_main(body, context):
    analysis = analyze(build_review(body, context))
    assert not analysis.trusted, analysis
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert gate_blocks(build_review(body, context))


# Negations main excused, which merged under the MEDIUM exemptions. Task 3161
# deleted the exemptions (five rounds of holes), so every one blocks; the
# same prose in lower case (what the prompt asks for) merges.
MEDIUM_NEGATIONS = [
    ["The page needs a login, so this is not MEDIUM."],
    ["Kept at LOW rather than MEDIUM."],
    ["There are no MEDIUM findings."],
    ["The review found no MEDIUM issues in the diff."],
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", MEDIUM_NEGATIONS)
def test_medium_negations_block(body, context):
    analysis = analyze(build_review(body, context))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert gate_blocks(build_review(body, context))
    lower = [line.replace("MEDIUM", "medium") for line in body]
    assert analyze(build_review(lower, context)).trusted, lower
    assert not gate_blocks(build_review(lower, context))


# --- R3152-03: the per-task reviewer text states the lower-case rule --------------

async def _reviewer_task_description(tmp_path) -> str:
    """The task description run_security_review hands the reviewer."""
    captured: list[str] = []

    def capture_prompt(security_task, *args, **kwargs):
        captured.append(security_task["description"])
        return "prompt"

    class _Command:
        def __enter__(self):
            return ["fake-cmd"]

        def __exit__(self, *exc_info):
            return False

    async def fake_run_agent(cmd, timeout=None):
        return {"success": True, "duration": 0.1, "result_text": "",
                "errors": []}

    worktree = tmp_path / "wt-3154"
    worktree.mkdir()
    task = {"id": 3154, "title": "t", "description": "d", "project_id": 1}
    with patch("equipa.loops.run_agent", side_effect=fake_run_agent), \
         patch("equipa.loops.build_cli_command", return_value=_Command()), \
         patch("equipa.loops.build_system_prompt", side_effect=capture_prompt), \
         patch("equipa.loops.get_role_turns", return_value=10), \
         patch("equipa.loops.get_role_model", return_value="opus"), \
         patch("equipa.loops.load_dispatch_config",
               return_value={"security_review_timeout": 30}), \
         patch("equipa.loops._extract_security_findings", return_value=[]), \
         patch("equipa.loops._create_security_lessons", return_value=0):
        await loops.run_security_review(task, str(worktree), {}, MagicMock(),
                                        stable_project_dir=str(tmp_path))
    assert captured, "run_security_review built no reviewer prompt"
    return captured[0]


@pytest.mark.asyncio
async def test_reviewer_task_text_states_the_lower_case_rule(tmp_path):
    description = await _reviewer_task_description(tmp_path)
    assert "ordinary prose is fine" not in description
    assert "write critical, high and medium in lower case" in description
    # Task 3161: the rule covers MEDIUM too.
    assert ("Write CRITICAL, HIGH and MEDIUM in UPPER case only as the "
            "severity label of a finding heading and on the single "
            "`## Counts` footer line") in description
    assert "any other UPPER-case CRITICAL, HIGH or MEDIUM" in description
    assert "BLOCKS the merge" in description


def _counted_medium_review(prose: str) -> str:
    """A review with one counted MEDIUM finding [M1] and ``prose`` in its
    section."""
    text = build_review([prose], "zero")
    return text.replace("MEDIUM: 0 | LOW: 0", "MEDIUM: 1 | LOW: 0").replace(
        "## Findings\n", "## Findings\n### [M1] MEDIUM \N{EM DASH} stored XSS"
        "\nDetails.\n")


def test_a_counted_medium_finding_does_not_block_the_merge():
    text = _counted_medium_review("medium issue: stored XSS in app/view.py:7.")
    analysis = analyze(text)
    assert analysis.trusted, analysis
    assert analysis.counts["MEDIUM"] == 1
    assert not gate_blocks(text)


def test_a_counted_medium_repeated_in_its_section_blocks():
    """Task 3161: the section exemption is deleted, so the finding's own
    severity repeated in UPPER case in its section is unaccounted (this
    merged under task 3154)."""
    text = _counted_medium_review("MEDIUM issue: stored XSS in app/view.py:7.")
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert gate_blocks(text)


# --- I3152-01: a deleted character separates words --------------------------------

def _separator_bodies():
    """(id, body) for every separator I3152-01 names, as written and as a
    character reference, before and after the word."""
    separators = {
        "U+3164": "\N{HANGUL FILLER}", "U+115F": "\N{HANGUL CHOSEONG FILLER}",
        "U+1160": "\N{HANGUL JUNGSEONG FILLER}",
        "U+FFA0": "\N{HALFWIDTH HANGUL FILLER}",
        "U+0378": chr(0x0378), "U+3FFFF": chr(0x3FFFF),
        "U+200B": "\N{ZERO WIDTH SPACE}", "U+00AD": "\N{SOFT HYPHEN}",
        "U+2060": "\N{WORD JOINER}", "U+FE0F": chr(0xFE0F),
        "U+001F": "\x1f", "U+007F": "\x7f", "U+0001": "\x01",
    }
    references = ("&#x3164;", "&#x378;", "&#x115F;", "&#xFFA0;", "&#x3FFFF;",
                  "&#x200B;", "&#12644;")
    every = {**separators, **{reference: reference for reference in references}}
    for name, separator in every.items():
        yield (f"before-{name}",
               f"Rated{separator}HIGH: SQL injection in login.py lets anyone "
               f"in.")
        yield (f"after-{name}",
               f"The HIGH{separator}issue: SQL injection in login.py lets "
               f"anyone in.")
        yield (f"critical-{name}",
               f"Severity{separator}CRITICAL{separator}remote code execution "
               f"in app/upload.py:7.")


SEPARATOR_BODIES = dict(_separator_bodies())


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("name", sorted(SEPARATOR_BODIES))
def test_a_deleted_character_between_words_separates_them(name, context):
    assert gate_blocks(build_review([SEPARATOR_BODIES[name]], context)), name


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", [
    "HI\N{HANGUL FILLER}GH: SQL injection in login.py lets anyone in.",
    "HI&#x3164;GH: SQL injection in login.py lets anyone in.",
    "H\N{ZERO WIDTH SPACE}IGH: SQL injection in login.py lets anyone in.",
    "CRIT\N{SOFT HYPHEN}ICAL remote code execution in app/upload.py:7.",
])
def test_a_deleted_character_inside_a_word_still_joins_it(body, context):
    assert gate_blocks(build_review([body], context))


def _one_high_review(heading):
    text = build_review(["Details of the finding."], "zero")
    return text.replace("| HIGH: 0 |", "| HIGH: 1 |").replace(
        "## Findings\n", f"## Findings\n{heading}\nDetails.\n")


@pytest.mark.parametrize("heading", [
    f"### [S1] HIGH\N{ZERO WIDTH SPACE} {E} SQL injection in login.py",
    f"### [S1] HIGH {E} SQL injection in login.py \N{WARNING SIGN}️",
    f"### [S1] HIGH {E} SQL injection in login.py \N{WHITE HEAVY CHECK MARK}",
])
def test_a_counted_label_next_to_an_invisible_character_is_still_counted(
        heading):
    analysis = analyze(_one_high_review(heading))
    assert analysis.trusted, analysis
    assert analysis.counts["HIGH"] == 1


@pytest.mark.parametrize("body", [
    "Rated\N{HANGUL FILLER}high after review; no further action.",
    "The summary\N{ZERO WIDTH SPACE} lists every file \N{WARNING SIGN}️.",
    "Complexity is O(n\N{SUPERSCRIPT TWO}) in the parser, which is fine.",
    "The tree \N{BOX DRAWINGS LIGHT VERTICAL} src "
    "\N{BOX DRAWINGS LIGHT VERTICAL} tests was read.",
])
def test_separators_in_prose_without_a_severity_word_merge(body):
    for context in CONTEXTS:
        assert not gate_blocks(build_review([body], context)), body


# --- I3152-02: number forms next to the word do not glue it ------------------------

NUMBER_FORMS = {
    "superscript-1": "\N{SUPERSCRIPT ONE}", "subscript-2": "\N{SUBSCRIPT TWO}",
    "one-half": "\N{VULGAR FRACTION ONE HALF}",
    "roman-2": "\N{ROMAN NUMERAL TWO}", "circled-1": "\N{CIRCLED DIGIT ONE}",
    "ordinal-a": "\N{FEMININE ORDINAL INDICATOR}",
    "ordinal-o": "\N{MASCULINE ORDINAL INDICATOR}",
    "fullwidth-1": "\N{FULLWIDTH DIGIT ONE}", "nko-0": "\N{NKO DIGIT ZERO}",
    "modifier-h": "\N{MODIFIER LETTER SMALL H}",
    "reference-sup1": "&sup1;", "reference-185": "&#185;",
}
NUMBER_FORM_BODIES = {
    **{f"label-{name}": f"HIGH{form}: SQL injection in login.py lets anyone "
                        f"in." for name, form in NUMBER_FORMS.items()},
    **{f"prose-{name}": f"The HIGH{form} SQL injection in login.py lets anyone "
                        f"in." for name, form in NUMBER_FORMS.items()},
    **{f"before-{name}": f"Rated {form}HIGH: SQL injection in login.py."
       for name, form in NUMBER_FORMS.items()},
    "critical-superscript-2": "CRITICAL\N{SUPERSCRIPT TWO} remote code "
                              "execution in upload.py",
}


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("name", sorted(NUMBER_FORM_BODIES))
def test_a_number_form_next_to_the_word_does_not_glue_it(name, context):
    assert gate_blocks(build_review([NUMBER_FORM_BODIES[name]], context)), name


# --- R3152-01: a letter drawn as punctuation does not glue the word ----------------

GLUE_LETTERS = {
    "U+01C3": "\N{LATIN LETTER RETROFLEX CLICK}",
    "U+02BC": "\N{MODIFIER LETTER APOSTROPHE}",
    "U+0640": "\N{ARABIC TATWEEL}",
    "U+02D0": "\N{MODIFIER LETTER TRIANGULAR COLON}",
    "U+01C1": "\N{LATIN LETTER LATERAL CLICK}",
    "U+02B9": "\N{MODIFIER LETTER PRIME}",
    "U+0294": "\N{LATIN LETTER GLOTTAL STOP}",
    "U+00BA": "\N{MASCULINE ORDINAL INDICATOR}",
    "U+02BB": "\N{MODIFIER LETTER TURNED COMMA}",
}
GLUE_LETTER_BODIES = {
    **{f"before-{name}": f"{letter}HIGH: SQL injection in login.py lets "
                         f"anyone in." for name, letter in GLUE_LETTERS.items()},
    **{f"quoted-{name}": f"Rated {letter}HIGH{letter}: SQL injection in "
                         f"login.py." for name, letter in GLUE_LETTERS.items()},
    **{f"after-{name}": f"The HIGH{letter} SQL injection in login.py lets "
                        f"anyone in." for name, letter in GLUE_LETTERS.items()},
    "ascii-reference-html": "<div>The &#700;HIGH&#700; issue: SQL injection "
                            "in login.py.</div>",
    "ascii-reference-markdown": "The &#700;HIGH&#700; issue: SQL injection in "
                                "login.py.",
    "turned-comma-critical": "\N{MODIFIER LETTER TURNED COMMA}CRITICAL"
                             "\N{MODIFIER LETTER APOSTROPHE} code execution "
                             "in upload.py",
}


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("name", sorted(GLUE_LETTER_BODIES))
def test_a_letter_drawn_as_punctuation_does_not_glue_the_word(name, context):
    assert gate_blocks(build_review([GLUE_LETTER_BODIES[name]], context)), name


# Found while fixing R3152-01: task 3149 folded U+2223 DIVIDES and U+22A4
# DOWN TACK to I and T in every view, which glued "<U+2223>HIGH" into
# "IHIGH": main (ba6065a) blocks these bodies, task 3152 trusted them.
FOLD_GLUE_BODIES = [
    "The \N{DIVIDES}HIGH issue was found in login.py and lets anyone in.",
    "The HIGH\N{DOWN TACK} issue was found in login.py and lets anyone in.",
    "The \N{BOX DRAWINGS LIGHT VERTICAL}HIGH issue was found in login.py.",
    "The \N{REGIONAL INDICATOR SYMBOL LETTER A}HIGH issue was found in "
    "login.py.",
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", FOLD_GLUE_BODIES)
def test_a_folded_symbol_next_to_the_word_does_not_glue_it(body, context):
    assert gate_blocks(build_review([body], context))


def _standalone_severities(line: str) -> set[str]:
    """The severities the backstop reads as standalone words on ``line``,
    through the same readings _analyze_review_file builds."""
    # Task 3157: _analyze_review_file folds with _BACKSTOP_VIEW_FOLDS.
    folded = normalize_review_text(
        loops._translate_non_ascii(line, loops._BACKSTOP_VIEW_FOLDS))
    found = set()
    for view, origins in loops._backstop_views(folded):
        found |= {severity for _, severity in
                  loops._backstop_tokens(view, origins)}
    separated = loops._backstop_separated_text(line)
    if separated is not None:
        for view, origins in loops._backstop_views(separated, links=False):
            found |= {severity for _, severity in loops._backstop_tokens(
                view, origins, separated=True)}
    return found


def _neighbour_family():
    """Every BMP code point that is no cased letter (the large CJK and
    unassigned ranges sampled), plus samples of the other planes."""
    for code_point in range(0x80, 0x10000):
        if 0xD800 <= code_point <= 0xDFFF:
            continue
        category = unicodedata.category(chr(code_point))
        if category in ("Lu", "Ll", "Lt"):
            continue
        if category in ("Lo", "Cn") and code_point % 7:
            continue
        yield code_point
    yield from (0x1D173, 0x1BCA0, 0xE0001, 0xE0100, 0x3FFFF, 0x10FFFD)


def test_no_neighbour_but_an_ascii_letter_or_digit_glues_the_word():
    glued = []
    for code_point in _neighbour_family():
        char = chr(code_point)
        for line in (f"x{char}HIGH y", f"y HIGH{char}x"):
            if "HIGH" not in _standalone_severities(line):
                glued.append(f"U+{code_point:04X} {line!r}")
    assert not glued, glued[:40]


# --- I3152-03, R3152-04: the generated confusables table ---------------------------

CONFUSABLES_EXTRACT = (REPO / "tests" / "fixtures"
                       / "confusables_severity_letters.txt")
_WORD_OF_LETTER = {"C": "CRITICAL", "R": "CRITICAL", "I": "CRITICAL",
                   "T": "CRITICAL", "A": "CRITICAL", "L": "CRITICAL",
                   "H": "HIGH", "G": "HIGH", "M": "MEDIUM", "E": "MEDIUM",
                   "D": "MEDIUM", "U": "MEDIUM"}


def _generator():
    path = REPO / "scripts" / "generate_severity_confusables.py"
    spec = importlib.util.spec_from_file_location("severity_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_table_is_what_the_unicode_data_gives():
    generator = _generator()
    text = CONFUSABLES_EXTRACT.read_text(encoding="utf-8")
    table, _ = generator.severity_confusables(generator.parse_confusables(text))
    assert table == SEVERITY_LETTER_CONFUSABLES
    assert f"# Version: {CONFUSABLES_VERSION}" in text
    assert set("CRITALHG") <= set(table)
    # Cherokee TLE (I3152-03) and the syllabics of R3143-07 are among them.
    assert 0x13DE in table["L"] and 0x13DF in table["C"]
    assert 0x157C in table["H"] and 0x1587 in table["R"]


def test_every_confusable_of_the_letters_spells_the_word():
    missed = []
    for letter, code_points in sorted(SEVERITY_LETTER_CONFUSABLES.items()):
        word = _WORD_OF_LETTER[letter]
        for code_point in code_points:
            spelled = word.replace(letter, chr(code_point))
            text = build_review([f"{spelled}: code execution in app/upload.py:7."],
                                "zero")
            # A MEDIUM word untrusts the review; CRITICAL or HIGH blocks it.
            if (not analyze(text).trusted if word == "MEDIUM"
                    else gate_blocks(text)):
                continue
            missed.append(f"U+{code_point:04X} as {letter} in {word}")
    assert not missed, missed


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", [
    "CRITICA\N{CHEROKEE LETTER TLE}: RCE in upload.py",
    "\N{CHEROKEE LETTER TLI}RITICAL: RCE in upload.py",
    "\N{CANADIAN SYLLABICS TA}RITICAL: code execution in app/upload.py:7.",
    "CR\N{BOX DRAWINGS LIGHT VERTICAL}T\N{BOX DRAWINGS LIGHT VERTICAL}CAL: "
    "code execution in app/upload.py:7.",
    "H\N{HEBREW PUNCTUATION PASEQ}GH: SQL injection in login.py.",
])
def test_named_lookalike_labels_block(body, context):
    assert gate_blocks(build_review([body], context))
