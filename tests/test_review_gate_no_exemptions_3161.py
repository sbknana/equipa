#!/usr/bin/env python3
"""Task 3161: the review-gate backstop has NO exemptions for MEDIUM either.

Fix-forward of task 3157, whose independent review (indep-3157-3158) found
R3157-01: a lookalike letter inside an ordinary word was a word break to the
MEDIUM exemption rules, so "Two M<U+0126>NOR MEDIUM issues remain." read as
a "nor" negation and an uncounted MEDIUM finding was trusted (10,248 texts
newly passed against cb3373c, 8,712 against ba6065a). R3157-02: a raw
private-use character split a word the same way. Exemptions had leaked in
five review rounds, so the operator extended the no-exemption rule of task
3152 to MEDIUM: every standalone UPPER-case CRITICAL, HIGH or MEDIUM must be
the label of a finding heading the parser counted, at the offset it
attributed, or a label of the final strict "## Counts" line. Anything else
blocks with "unaccounted <SEV> token at line N". LOW and INFO stay ignored.

Covered here, each failing on main before this task (502975b):

* every text of the probe-corpus families this task added blocks with the
  reason naming its severity and line, for every severity word, in both
  review contexts: each lookalike the gate knows before a negation tail and
  at the end of a noun, comma and list label (R3157-01), and a private-use
  character inside a word (R3157-02); so do the bodies the reviews wrote;
* each deleted exemption shape (negation, tally, comparison, soft wrap,
  section, finding ID, "Overall risk") blocks for every severity, with the
  reason naming the severity and the line;
* lower-case prose, LOW and INFO words, a counted label and a strict footer
  still give a trusted review;
* lookalikes are folded for the letters of the severity words only.

Copyright 2026 Forgeborn
"""

import ast
import functools
import importlib.util
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import REVIEW_BIDI_CONTROL_REASON
from tests.review_gate_production import (
    AS_WRITTEN_ONLY_FINDINGS,
    blocked_by_the_gate,
    decision_and_analysis,
)
from tests.test_review_gate_no_exemptions_3152 import (
    CONTEXTS, E, analyze, build_review, gate_blocks, review,
)

REPO = Path(__file__).resolve().parent.parent
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")


def _probe_corpus():
    path = REPO / "scripts" / "review_gate_probe_corpus.py"
    spec = importlib.util.spec_from_file_location("review_gate_corpus_3161",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORPUS = _probe_corpus()


def _line_of(text: str, body_line: str) -> int:
    """The 1-based line of ``text`` that is ``body_line``."""
    return text.split("\n").index(body_line) + 1


def _unaccounted(text: str, severity: str, body_line: str) -> str | None:
    """None when the merge gate blocks the review, untrusted for an
    unaccounted ``severity`` word on ``body_line``'s line, else what the gate
    said instead. Task 3170 (IR67-02): the reason is the parser's reading of
    the text provenance handed the gate, which provenance must trust (or
    refuse for a bidi control, its first check)."""
    decision, analysis = decision_and_analysis(text)
    provenance = decision.provenance
    # Task 3172 (R3169-01): a lookalike outside the gate's Unicode table
    # (assigned after Unicode 13.0, such as U+1CCD6) has the review refused
    # before it is parsed, naming that character on this line.
    unread = [char for char in body_line
              if not loops._in_gate_unicode_table(char)]
    if unread:
        refused = (f"{loops.REVIEW_UNICODE_DATA_REASON}: "
                   f"U+{ord(unread[0]):04X} at line "
                   f"{_line_of(text, body_line)} (name the character, never "
                   f"paste it)")
        if (analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE
                and analysis.detail == refused and decision.blocks
                and provenance.trusted):
            return None
        return f"{analysis.verdict}: {analysis.detail[:120]}"
    expected = (f"{loops.backstop_reason(severity)} at line "
                f"{_line_of(text, body_line)}")
    if (analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
            and expected in analysis.detail.split(": ")[0]
            and decision.blocks
            and (provenance.trusted
                 or provenance.reason.startswith(REVIEW_BIDI_CONTROL_REASON))):
        return None
    return f"{analysis.verdict}: {analysis.detail[:120]}"


@pytest.mark.parametrize("finding, severity", [
    ("hangul-filler-high", "HIGH"),
    ("halfwidth-filler-critical", "CRITICAL"),
])
def test_the_must_block_helper_reads_the_review_as_written(finding, severity):
    """Task 3170 (IR67-02): a severity only the review as written shows
    blocks through this suite's helper, so a gate that parsed the normalised
    text (R3161-01) fails this suite too."""
    line = AS_WRITTEN_ONLY_FINDINGS[finding]
    for context in CONTEXTS:
        text = build_review([line], context)
        assert _unaccounted(text, severity, line) is None, (context,
                                                            analyze(text))


# --- R3157-01, R3157-02: the families of the independent 3157 review -------------

# The probe-corpus families task 3161 added (scripts/review_gate_probe_corpus
# .py): a lookalike after the M of "MINOR"-like words whose tail is a
# negation ("split"), at the end of a generic noun ("noun"), of a lower-case
# word after a comma ("comma") and of a listed LOW ("list"), and a
# private-use character inside "MINOR" ("private-use"), for every severity
# word. They are replayed here, every text of them, rather than in
# tests/test_review_gate_followups_3157.py, which replays the texts some
# older tree blocked: R3157-02 passed on every older tree.
NEW_FAMILIES = ("split", "noun", "comma", "list", "private-use")
CORPUS_FIXTURE = REPO / "tests" / "fixtures" / "review_gate_probe_corpus_3157.json"
BASELINE_TREES = ("ba6065a", "3afec74", "cb3373c", "502975b")


@functools.lru_cache(maxsize=1)
def _new_family_rows() -> tuple[list[tuple[int, str, str, list[str]]], dict]:
    """(index, key, severity, lines) of every body of the 3161 families, by
    its index in the corpus, and per (tree, context) the older tree's
    verdicts on the whole corpus."""
    path = REPO / "scripts" / "review_gate_differential.py"
    spec = importlib.util.spec_from_file_location("review_gate_diff_3161",
                                                  path)
    differential = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(differential)
    bodies = differential.probe_corpus_bodies()
    fixture = json.loads(CORPUS_FIXTURE.read_text(encoding="ascii"))
    assert fixture["bodies"] == len(bodies)
    blocked = {
        (tree, context): differential.decode_bits(
            fixture["blocked"][tree][context], len(bodies))
        for tree in BASELINE_TREES for context in CONTEXTS
    }
    rows = [(index, key, severity, lines)
            for index, (key, severity, lines) in enumerate(bodies)
            if key.split("|", 1)[0] in NEW_FAMILIES]
    return rows, blocked


# Texts of each severity's 3161 families that ba6065a blocked, per context
# (the fixture): the split rows of MEDIUM, whose exemption R3157-01 split,
# and the uncounted CRITICAL and HIGH words ba6065a never exempted.
BA6065A_BLOCKED_AT_LEAST = {"CRITICAL": 4000, "HIGH": 4000, "MEDIUM": 4000}


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("severity", SEVERITIES)
def test_every_text_of_the_3161_families_blocks(severity, context):
    rows, blocked = _new_family_rows()
    offset = CONTEXTS.index(context)
    failures = []
    ba6065a_blocked = 0
    judged = 0
    for index, key, body_severity, lines in rows:
        if body_severity != severity:
            continue
        judged += 1
        trees = [tree for tree in BASELINE_TREES
                 if blocked[(tree, context)][index]]
        ba6065a_blocked += "ba6065a" in trees
        problem = _unaccounted(build_review(lines, context), severity,
                               lines[0])
        if problem is not None:
            failures.append(f"{key} {CONTEXTS[offset]} (blocked by "
                            f"{','.join(trees) or 'no older tree'}): {problem}")
    assert not failures, (len(failures), failures[:10])
    # Each family is there for each lookalike (514) or private-use point.
    lookalikes = len(CORPUS.lookalikes())
    tails = len(CORPUS.SPLIT_TAILS) * (2 if severity == "MEDIUM" else 1)
    assert judged == (tails + 3) * lookalikes + len(CORPUS.PRIVATE_USE) + 1
    assert ba6065a_blocked >= BA6065A_BLOCKED_AT_LEAST[severity]


def test_the_family_holds_the_reported_code_points():
    # The 244 code points of the report include U+0126 and U+0399; the
    # family holds every lookalike the gate knows (514), a superset.
    folds = CORPUS.lookalikes()
    assert len(folds) >= 500
    assert {0x0126, 0x0399, 0x041D, 0x0422, 0x0395, 0x1D07, 0x2113} <= set(
        folds)


# The bodies the 3157 reviews wrote out (R3157-01: SECURITY-REVIEW-3157 and
# indep-3157-3158; R3157-02).
REPORTED_BODIES = {
    "minor-iota": "Two M\N{GREEK CAPITAL LETTER IOTA}NOR MEDIUM issues "
                  "remain.",
    "minor-h-stroke": "Two M\N{LATIN CAPITAL LETTER H WITH STROKE}NOR "
                      "MEDIUM issues remain.",
    "casino-iota": "CAS\N{GREEK CAPITAL LETTER IOTA}NO MEDIUM issues "
                   "remain.",
    "whenever-epsilon": "WH\N{GREEK CAPITAL LETTER EPSILON}NEVER MEDIUM "
                        "issues remain.",
    "whenever-small-e": "wh\N{LATIN LETTER SMALL CAPITAL E}never MEDIUM "
                        "issues remain.",
    "nano-alpha": "N\N{GREEK CAPITAL LETTER ALPHA}NO MEDIUM issues remain.",
    "minor-script-l": "m\N{SCRIPT SMALL L}nor MEDIUM issues remain.",
    "noun-en": "No MEDIUM issues\N{CYRILLIC CAPITAL LETTER EN}: SQL "
               "injection in login.py allows auth bypass.",
    "noun-te": "No MEDIUM issues\N{CYRILLIC CAPITAL LETTER TE}: SQL "
               "injection in login.py allows auth bypass.",
    "comma-en": "Not MEDIUM, see\N{CYRILLIC CAPITAL LETTER EN}: SQL "
                "injection in login.py allows auth bypass.",
    "list-en": "not MEDIUM / LOW\N{CYRILLIC CAPITAL LETTER EN}: SQL "
               "injection in login.py allows auth bypass.",
    "private-use-e000": "Two MINOR MEDIUM issues remain.",
    "private-use-f0000": "Two MI\U000f0000NOR MEDIUM issues remain.",
}


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("name", sorted(REPORTED_BODIES))
def test_a_reported_body_blocks(name, context):
    line = REPORTED_BODIES[name]
    text = build_review([line], context)
    assert _unaccounted(text, "MEDIUM", line) is None, analyze(text)


# --- The deleted exemption shapes block for every severity -------------------------

EXEMPTION_SHAPES = {
    "negation": ["No {SEV} findings."],
    "negation-nor": ["Neither LOW nor {SEV} issues were found."],
    "negation-without": ["The diff ships without {SEV} findings."],
    "tally": ["{SEV}: 0"],
    "tally-table": ["| {SEV} | 0 |"],
    "tally-count-before": ["0 {SEV} findings."],
    "comparison": ["Every finding is rated below {SEV}."],
    "comparison-lower": ["Nothing rated higher than LOW, lower than {SEV}."],
    "list": ["We looked for LOW or {SEV} issues and found none."],
    "soft-wrap": ["The worst issue was not", "{SEV}. It was LOW."],
    "finding-id": ["See R3157-{SEV}-01 in the previous review."],
    "overall-risk": ["Overall risk: {SEV}"],
}


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("severity", SEVERITIES)
@pytest.mark.parametrize("shape", sorted(EXEMPTION_SHAPES))
def test_a_deleted_exemption_shape_blocks(shape, severity, context):
    lines = [line.replace("{SEV}", severity)
             for line in EXEMPTION_SHAPES[shape]]
    word_line = next(line for line in lines if severity in line)
    text = build_review(lines, context)
    assert _unaccounted(text, severity, word_line) is None, analyze(text)


def _one_finding_footer(severity: str) -> str:
    return " | ".join(f"{word}: {int(word == severity)}"
                      for word in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"))


@pytest.mark.parametrize("severity", SEVERITIES)
@pytest.mark.parametrize("shape", ["own-section", "finding-id"])
def test_a_counted_finding_credits_no_other_word(shape, severity):
    # The deleted section rule read the finding's severity repeated in its
    # own section as that finding, and the ID rule a line naming a counted
    # finding's ID.
    if shape == "own-section":
        line = f"{severity} because the cache is shared by every user."
        body = [line]
    else:
        line = f"S1 is {severity} and stays open."
        body = ["## Notes", "", line]
    text = review("1 finding.", body, _one_finding_footer(severity),
                  heading=f"### [S1] {severity} {E} stale cache in cache.py")
    assert _unaccounted(text, severity, line) is None, analyze(text)
    # Without the second word the same review is trusted.
    lower = review("1 finding.", [part.replace(severity, severity.lower())
                                  for part in body],
                   _one_finding_footer(severity),
                   heading=f"### [S1] {severity} {E} stale cache in cache.py")
    assert analyze(lower).trusted, analyze(lower)


def test_the_reason_names_each_severity_and_its_lines():
    lines = ["No MEDIUM findings.", "", "Not HIGH either.", "",
             "No MEDIUM issues remain."]
    text = build_review(lines, "zero")
    analysis = blocked_by_the_gate(text)
    medium_lines = (_line_of(text, lines[0]), _line_of(text, lines[4]))
    high_line = _line_of(text, lines[2])
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    reasons = analysis.detail.split(": ")[0]
    # The most severe first.
    assert reasons == (
        f"unaccounted HIGH token at line {high_line}; unaccounted MEDIUM "
        f"token at line {medium_lines[0]}, {medium_lines[1]}"), analysis.detail


# --- What still gives a trusted review --------------------------------------------

def test_lower_case_prose_is_trusted():
    text = build_review([
        "No critical, high or medium findings.",
        "Two M\N{GREEK CAPITAL LETTER IOTA}NOR medium issues were considered "
        "and rejected; nothing rated below high is open.",
        "critical: 0, high: 0, medium: 0.",
    ], "zero")
    analysis = analyze(text)
    assert analysis.trusted, analysis
    assert not gate_blocks(text)


@pytest.mark.parametrize("line", [
    "Not LOW either.", "LOW: 0", "INFO: 1 (informational only).",
    "INFO-level notes follow.", "Overall risk: LOW",
    "No LOW or INFO findings.",
])
def test_low_and_info_words_are_ignored(line):
    text = build_review([line], "zero")
    assert analyze(text).trusted, analyze(text)


def test_a_counted_medium_label_and_the_strict_footer_are_credited():
    text = review(
        "1 finding.", ["## Notes", "", "Nothing else."],
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0",
        heading=f"### [M1] MEDIUM {E} reflected XSS in view.py")
    analysis = analyze(text)
    assert analysis.trusted, analysis
    assert (analysis.counts or {}).get("MEDIUM") == 1
    assert not gate_blocks(text)


def test_a_second_medium_on_the_heading_line_blocks():
    heading = f"### [M1] MEDIUM {E} reflected XSS, MEDIUM in view.py"
    text = review("1 finding.", ["## Notes", "", "Nothing else."],
                  "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0",
                  heading=heading)
    assert _unaccounted(text, "MEDIUM", heading) is None, analyze(text)


@pytest.mark.parametrize("footer", [
    ["CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 (none) | LOW: 0 | INFO: 0"],
    ["CRITICAL: 0 | HIGH: 0 |", "MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["**CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0**"],
])
def test_a_footer_outside_the_strict_grammar_credits_no_medium(footer):
    text = review("No findings.", ["## Findings", "", "None."], footer)
    assert gate_blocks(text), analyze(text)


# --- Folding is for the letters of the severity words only ----------------------

def test_only_severity_word_letters_are_folded():
    # Every FOLD mark in the table is the mark of a letter of CRITICAL, HIGH
    # or MEDIUM (or l, read as I); the private-use characters as written
    # become the foreign mark, which is no letter.
    marks = {mark for mark in loops._BACKSTOP_VIEW_FOLDS.values()
             if mark != loops._BACKSTOP_FOREIGN_MARK}
    letters = {loops._backstop_unmarked(mark) for mark in marks}
    assert letters <= set("CRITALHGMEDUl"), sorted(letters - set(
        "CRITALHGMEDUl"))
    # Greek capital NU, Cyrillic capital O, Greek small omicron and the
    # parenthesised N: lookalikes of letters no severity word has.
    for code_point in (0x039D, 0x041E, 0x03BF, 0x1F11D):
        assert code_point not in loops._BACKSTOP_VIEW_FOLDS, hex(code_point)


def test_a_lookalike_still_spells_a_severity_word():
    # U+0399 (Greek capital IOTA) as the I of HIGH, U+041C (Cyrillic EM) as
    # the M of MEDIUM.
    for severity, written in (("HIGH", "H\N{GREEK CAPITAL LETTER IOTA}GH"),
                              ("MEDIUM", "\N{CYRILLIC CAPITAL LETTER EM}EDIUM")):
        line = f"Rated {written}: SQL injection in login.py."
        text = build_review([line], "zero")
        assert _unaccounted(text, severity, line) is None, analyze(text)


def test_a_lookalike_of_another_letter_does_not_hide_the_word():
    # Greek capital NU is no longer folded to N, so it no longer glues to the
    # word (an unfolded letter blocks rather than hides).
    line = "\N{GREEK CAPITAL LETTER NU}MEDIUM: SQL injection in login.py."
    text = build_review([line], "zero")
    assert _unaccounted(text, "MEDIUM", line) is None, analyze(text)


# --- The reviewer is told so -----------------------------------------------------

def test_the_reviewer_prompt_requires_lower_case_medium():
    prompt = " ".join(
        (REPO / "prompts" / "security-reviewer.md").read_text("utf-8").split())
    assert ("There are no exceptions for critical, high or medium: write "
            "critical, high and medium in lower case everywhere except the "
            "severity label of a finding heading and the `## Counts` footer "
            "line. Any other UPPER-case CRITICAL, HIGH or MEDIUM blocks the "
            "merge") in prompt
    assert "For critical and high there are no exceptions" not in prompt


def _string_literals(function) -> str:
    """The string literals of ``function`` (f-string pieces included) in
    source order, joined: the text it builds, without its comments."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    pieces = [node.value for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return " ".join("".join(pieces).split())


def test_the_per_task_reviewer_text_requires_lower_case_medium():
    # run_security_review builds the reviewer's task text inline.
    text = _string_literals(loops.run_security_review)
    assert ("write critical, high and medium in lower case (\"no medium "
            "findings\"") in text
    assert ("any other UPPER-case CRITICAL, HIGH or MEDIUM, also in a "
            "negation, tally, comparison, table or finding ID, BLOCKS the "
            "merge") in text
