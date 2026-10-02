#!/usr/bin/env python3
"""Task 3152: the review-gate backstop has NO exemptions for CRITICAL/HIGH.

Fix-forward of task 3149, whose security review (SECURITY-REVIEW-3149) found
the gate looser than main: new "below" and soft-wrap negations trusted
reviews main blocks (R3149-02, high), tally tail words and "no less than"
still excused a finding label (R3149-01, R3149-03), capped reference digits
left digits glued to the word (R3149-04) and the link reader was quadratic
(R3149-05). This was the fourth round in which an exemption opened a hole,
so the operator decided the design: every standalone UPPER-case CRITICAL or
HIGH must be the label of a finding heading the parser counted, at the
offset it attributed, or a label of the final strict "## Counts" line.
Anything else blocks with "unaccounted CRITICAL/HIGH token at line N".

Covered here, each with rows that fail before this task:

* every body of SECURITY-REVIEW-3149 and of indep-3143 blocks, in a review
  with zero findings and in one with one counted LOW finding;
* R3149-04: a numeric reference takes every digit and is U+FFFD past
  U+10FFFF without int();
* R3149-05: the link reader is linear and reads exactly what the old one
  read (a differential against the old algorithm on random markup);
* only the exact counted label and a strict final footer are credited;
* the exact completion and provenance marker lines alone are blanked;
* an unaccounted MEDIUM blocks again as on main (task 3149 had made it a
  logged advisory). MEDIUM kept its exemptions here; task 3161 removed
  them, so MEDIUM negations and tallies block too.

Copyright 2026 Forgeborn
"""

import random
import re
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line
from tests.review_gate_timing import median_cpu_seconds, timing_test

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{loops.backstop_reason(severity)} at line "
    for severity in loops.MERGE_BLOCKING_SEVERITIES
)

REPO = Path(__file__).resolve().parent.parent
NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_HIGH = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"
E = "\N{EM DASH}"
LOW_HEADING = f"### [E1] LOW {E} verbose error message"
CONTEXTS = ("zero", "one_low")
RB = 200 * 1024


def review(summary, body, footer_lines, *, heading=None, last_line=None,
           first_line=None):
    """A finished review: provenance line, Summary, body, Counts, sentinel."""
    if isinstance(footer_lines, str):
        footer_lines = [footer_lines]
    lines = [reviewer_nonce_line(NONCE) if first_line is None else first_line,
             "# Security Review", "", "## Summary", summary, ""]
    if heading:
        lines += [heading, "Details.", ""]
    lines += list(body) + ["", "## Files Reviewed", "- app.py", "",
                           "## Methodology", "Read the diff, ran semgrep.", "",
                           "## Counts", *footer_lines,
                           review_complete_line(NONCE) if last_line is None
                           else last_line]
    return "\n".join(lines) + "\n"


def build_review(body, context):
    """``body`` between a zero-finding review's Findings heading and its
    footer, or after one counted LOW finding (the reviewers' probe shapes:
    a footer counting zero critical, high and medium findings)."""
    if context == "zero":
        return review("No findings.", ["## Findings", ""] + list(body), ZERO)
    return review("1 finding.", ["## Notes", ""] + list(body), ONE_LOW,
                  heading=LOW_HEADING)


def analyze(text):
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def gate_blocks(text):
    """The merge decision of dispatch._security_review_blocks_merge: an
    untrusted review blocks; a trusted one blocks on CRITICAL or HIGH."""
    analysis = analyze(text)
    if not analysis.trusted:
        return True
    counts = analysis.counts or {}
    return counts.get("CRITICAL", 0) > 0 or counts.get("HIGH", 0) > 0


def assert_backstop_blocks(text):
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), (
        analysis.detail)
    return analysis


# --- Every body of SECURITY-REVIEW-3149 ------------------------------------------

# R3149-02 (high): "below" and soft-wrap negations.
R3149_02_BODIES = [
    ["The worst issue is described below CRITICAL severity remote code "
     "execution in app/upload.py:7."],
    ["The worst issue is described below",
     "CRITICAL severity remote code execution in app/upload.py:7."],
    ["Authentication on the upload route: none",
     "CRITICAL, as any anonymous user reaches code execution in "
     "app/upload.py:7."],
    ["The login form was reviewed. It was not",
     "HIGH. SQL injection in /login allows authentication bypass."],
    ["[a]: none",
     "CRITICAL issue was found in app/upload.py:7 and reaches code execution."],
    ["The login handler was read.", "", "    no",
     "HIGH. SQL injection in /login allows authentication bypass."],
    ["The worst issue is described below  ",
     "CRITICAL issue was found in app/upload.py:7."],
]
# R3149-04 (medium): over-long numeric references.
R3149_04_BODIES = [
    ["<div>The worst issue is &#999999999HIGH severity remote code execution "
     "in app/upload.py:7.</div>"],
    ["<div>The worst issue is &#x11111111HIGH severity remote code execution "
     "in app/upload.py:7.</div>"],
    ["<div>The worst issue is &#123456789CRITICAL severity remote code "
     "execution in app/upload.py:7.</div>"],
]
# R3149-01 (medium): a digit-led title made of tally tail words.
R3149_01_BODIES = [
    ["CRITICAL: 0 checks on code from npm packages",
     "A postinstall script runs arbitrary code in CI with the deploy token."],
    ["HIGH: 0 audit of new dependency packages"],
]
# R3149-03 (medium): a comparison negated before it asserts the severity.
R3149_03_BODIES = [
    ["Severity is no less than CRITICAL, because uploads run as code "
     "(app/upload.py:7)."],
    ["The upload issue is not lower than HIGH, because any user reaches code "
     "execution (app/upload.py:7)."],
]

# --- Every body of indep-3143's attack table and findings ------------------------

INDEP_3143_BODIES = [
    ["- Not MEDIUM, HIGH: SQL injection in login.py allows auth bypass"],
    ["Downgraded: not CRITICAL, HIGH: SQL injection in login.py."],
    ["Severity: not MEDIUM, HIGH"],
    ["- Rated higher than MEDIUM, HIGH: SQL injection in login.py"],
    [f"- Severity worse than HIGH {E} RCE in upload handler"],
    [f"- Neither LOW nor MEDIUM, CRITICAL {E} RCE via upload"],
    ["- 0 MEDIUM, CRITICAL: RCE via upload handler"],
    ["0 CRITICAL / HIGH: SQL injection in login.py lets anyone in."],
    ["CRITICAL: 0 rate limiting on login enables credential stuffing"],
    ["HIGH: 0 auth checks on /admin let anyone delete users."],
    ["Was it fixed? No HIGH SQL injection remains in login.py"],
    [f"<b></b>### [S2] CRITICAL {E} RCE in upload.py"],
    [f"<p>### [S2] HIGH {E} SQL injection in login.py</p>"],
    [f"&#35;## [S2] HIGH {E} SQL injection", "",
     "The HIGH SQL injection in login.py lets anyone log in as admin."],
    [f"<span></span>### [S2] HIGH {E} see below", "", "## Notes", "",
     "See S2: The HIGH SQL injection in login.py lets anyone in."],
    [f"- not exploitable remotely? No. HIGH {E} SQL injection in login.py"],
    ["<!-- no --> HIGH: SQL injection in login.py"],
    ["<span>no</span> HIGH: SQL injection in login.py"],
    ["[x](no) HIGH: SQL injection in login.py"],
    ['[x](y "no") HIGH: SQL injection in login.py'],
    ["None of these are HIGH except: SQL injection in login.py (HIGH)"],
    ["```", f"### [S1] HIGH {E} example", "```", "HIGH: 2FA bypass in login"],
    [f"### [M1] MEDIUM {E} CSRF", "", f"### [M1] MEDIUM {E} CSRF"],
    # R3143-03: inline markup splitting a word.
    ["The **H**IGH SQL injection in login.py lets anyone in."],
    ["H`IGH`: SQL injection in login.py"],
    ["H*IG*H SQL injection in login.py"],
    ["HI__G__H SQL injection in login.py"],
    ["HI~~~~GH SQL injection in login.py"],
    ["HI![](x)GH: SQL injection in login.py"],
    ["[HI](#x)GH: SQL injection in login.py"],
    ["[r]: #x", "", "[HI][r]GH SQL injection in login.py"],
    ["HI[](x)GH SQL injection in login.py"],
    # R3143-04: thousands of digits.
    ["HIGH &#" + "9" * 5000],
    # R3143-05: U+0130.
    ["- H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH: SQL injection"],
    # R3143-07: ASCII and other lookalikes, bidi, TeX.
    ["HlGH SQL injection in login.py"],
    ["H|GH SQL injection in login.py"],
    ["H1GH SQL injection in login.py"],
    ["\N{CYRILLIC CAPITAL LETTER EN WITH DESCENDER}IGH SQL injection"],
    ["\N{GREEK CAPITAL LETTER ETA}IGH SQL injection in login.py"],
    ["\N{LATIN CAPITAL LETTER H WITH STROKE}IGH SQL injection in login.py"],
    ["\N{RIGHT-TO-LEFT OVERRIDE}HGIH\N{POP DIRECTIONAL FORMATTING} SQLi"],
    ["\N{RIGHT-TO-LEFT ISOLATE}HGIH\N{POP DIRECTIONAL ISOLATE} SQLi"],
    ["$\\mathrm{H}\\mathrm{I}\\mathrm{G}\\mathrm{H}$ SQL injection"],
    # R3143-06: corpus shapes, blocked now (no exemption).
    ["No HIGH or CRITICAL findings, per the HIGH+ logging rule."],
    ["The scan found no", "CRITICAL/HIGH/MEDIUM issues. Remaining are LOW."],
    ["Overall risk: LOW, merge-safe with the HIGH noted above."],
]

REPORT_BODIES = (R3149_02_BODIES + R3149_04_BODIES + R3149_01_BODIES
                 + R3149_03_BODIES + INDEP_3143_BODIES)


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", REPORT_BODIES)
def test_every_reviewer_body_blocks(body, context):
    text = build_review(body, context)
    assert gate_blocks(text), (body, context, analyze(text))


@pytest.mark.parametrize("body", R3149_02_BODIES + R3149_01_BODIES
                         + R3149_03_BODIES + R3149_04_BODIES)
def test_r3149_bodies_block_with_the_new_reason_and_line(body):
    """Each blocks for its unaccounted word, on the line a reader sees it."""
    text = build_review(body, "one_low")
    analysis = assert_backstop_blocks(text)
    lines = text.split("\n")
    expected = next(number for number, line in enumerate(lines, 1)
                    if any(word in line for word in ("HIGH", "CRITICAL"))
                    and "|" not in line and not line.startswith("### [E1]"))
    severity = next(word for word in ("CRITICAL", "HIGH")
                    if word in lines[expected - 1])
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), analysis.detail
    assert re.search(rf"{loops.backstop_reason(severity)} at line "
                     rf"(?:\d+, )*{expected}(?!\d)", analysis.detail), (
        analysis.detail)


@pytest.mark.parametrize("body", R3149_02_BODIES + R3149_01_BODIES
                         + R3149_03_BODIES)
def test_the_same_prose_in_lower_case_merges(body):
    """The compliant form of each body (what the prompt asks for) merges."""
    lower = [line.replace("CRITICAL", "critical").replace("HIGH", "high")
             for line in body]
    text = build_review(lower, "one_low")
    analysis = analyze(text)
    assert analysis.trusted and not gate_blocks(text), analysis


# --- R3149-04: numeric references read every digit --------------------------------

@pytest.mark.parametrize("written, shown", [
    ("&#999999999HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),
    ("&#x11111111HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),
    ("&#123456789CRITICAL", "\N{REPLACEMENT CHARACTER}CRITICAL"),
    ("&#1114112HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),       # 7 digits
    ("&#x110000HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),       # 6 hex digits
    ("&#" + "9" * 100000 + "HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),
    ("&#x" + "F" * 100000 + ";HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),
    ("&#" + "0" * 5000 + "72;IGH", "HIGH"),
    ("&#x" + "0" * 5000 + "48IGH", "HIGH"),
    # Seven digits at the limit still decode: U+10FFFF is a noncharacter,
    # which the backstop deletes like every unassigned code point.
    ("&#1114111;HIGH", "HIGH"),
    ("&#x10FFFF;HIGH", "HIGH"),
    ("&#72IGH", "HIGH"),
    ("&#0;HIGH", "\N{REPLACEMENT CHARACTER}HIGH"),
])
def test_numeric_reference_takes_every_digit(written, shown):
    """Like a browser and html.unescape: no digit is left in the text."""
    assert loops._backstop_normalized(written) == shown


def test_hex_digits_after_a_reference_are_part_of_it():
    """A browser reads "C" as a hex digit, so "&#x110000CRITICAL" shows
    "RITICAL" after U+FFFD, and no reader sees a CRITICAL word."""
    assert (loops._backstop_normalized("&#x110000CRITICAL")
            == "\N{REPLACEMENT CHARACTER}RITICAL")


@timing_test
def test_a_200kb_reference_is_decided_without_int():
    text = build_review(["&#" + "9" * RB + "HIGH severity RCE"], "zero")
    assert median_cpu_seconds(assert_backstop_blocks, text) < 0.5


# --- R3149-05: the link reader is linear and reads what it read before ------------

def _old_link_reading(view, tails, labels, drop_alt):
    """Branch 3149's reader: the nearest "[" is found by scanning back to the
    last link for every "]" (quadratic), otherwise the same reading."""
    closes = loops._LINK_ANY_CLOSE_RE if labels else loops._LINK_CLOSE_RE
    kept = []
    joined_spans = []
    copied = searched = 0
    saw_image = False
    while (close := closes.search(view, searched)) is not None:
        bracket = close.end() - 1
        image = view.startswith("!", close.start())
        opener = (close.start() + 1 if image
                  else view.rfind("[", copied, bracket))
        span = (opener + 1, bracket) if opener >= 0 else (bracket, bracket)
        end = tails.end(close.end(), span, labels)
        if end is None:
            searched = close.end()
            continue
        text = view[span[0]:span[1]]
        if image:
            saw_image = True
            kept.append(view[copied:close.start()])
            if not drop_alt:
                kept.append(text)
            deleted_from = close.start() if drop_alt else bracket
        else:
            if opener >= 0:
                kept.append(view[copied:opener])
                kept.append(text)
            else:
                kept.append(view[copied:bracket])
            deleted_from = bracket
        if view.count("\n", deleted_from, end):
            joined_spans.append((deleted_from, end))
        copied = searched = end
    kept.append(view[copied:])
    origins = (loops._backstop_line_origins(view, joined_spans)
               if joined_spans else None)
    return (loops._backstop_split_words_joined("".join(kept)), origins,
            saw_image)


def test_link_reader_reads_what_the_old_reader_read():
    rng = random.Random(3152)
    alphabet = "[[]]()!ab H\n:<>\\\"x "
    labels_choices = [frozenset(), frozenset({"a", "b", "ab", "x"})]
    for _ in range(4000):
        view = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
        for labels in labels_choices:
            for drop_alt in (True, False):
                new = loops._backstop_link_reading(
                    view, loops._LinkTails(view), labels, drop_alt)
                old = _old_link_reading(
                    view, loops._LinkTails(view), labels, drop_alt)
                assert new == old, (view, labels, drop_alt)


@pytest.mark.parametrize("body", [
    ["[a]: x", "", "a[" + "b]" * (RB // 2)],
    ["a[" + "b](" * (RB // 3)],
    ["[a]: x", "", "a]" * (RB // 2)],
    ["[a]: x", "", "[" + " " * (RB // 2) + "a]" * (RB // 4)],
])
@timing_test
def test_r3149_05_link_bodies_parse_in_half_a_second(body):
    text = build_review(body, "zero")
    elapsed = median_cpu_seconds(analyze, text)
    assert elapsed < 0.5, elapsed


def test_link_text_longer_than_any_label_is_no_shortcut_link():
    """CommonMark labels hold at most 999 characters: a longer link text is
    never a defined label, so its brackets stay (and nothing is joined)."""
    view = "[a]: x\n\nHI[" + "GH" + " " * 2000 + "]"
    tails = loops._LinkTails(view)
    text, _, _ = loops._backstop_link_reading(view, tails, frozenset({"gh"}),
                                              True)
    assert text == view
    short = "[gh]: x\n\nHI[GH]"
    text, _, _ = loops._backstop_link_reading(
        short, loops._LinkTails(short), frozenset({"gh"}), True)
    assert text.endswith("HIGH")


# --- Only the counted label and a strict final footer are credited ----------------

def one_high_review(body, footer_lines=(ONE_HIGH,)):
    return review("1 finding.", ["## Findings", "",
                                 f"### [S1] HIGH {E} SQL injection in login.py",
                                 "Details."] + list(body), list(footer_lines))


def test_a_counted_heading_and_strict_footer_merge_with_their_count():
    analysis = analyze(one_high_review(["The fix is a parameterised query."]))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts["HIGH"] == 1


def test_heading_offsets_point_at_the_label():
    text = one_high_review([])
    analysis = analyze(text)
    assert analysis.heading_offsets
    for offset, severity in analysis.heading_offsets:
        assert text[offset:offset + len(severity)] == severity


@pytest.mark.parametrize("body", [
    ["- **Severity:** HIGH"],                       # its own section
    ["HIGH because the endpoint is public."],
    ["| S1 | HIGH | SQL injection |"],              # names the counted ID
    ["Overall risk: HIGH."],
    ["The one HIGH has a one-line fix."],
    ["No CRITICAL issues."],
])
def test_no_section_id_or_risk_exemption_with_a_counted_heading(body):
    assert_backstop_blocks(one_high_review(body))


def test_a_second_word_on_the_heading_line_blocks():
    text = review("1 finding.", ["## Findings", "",
                                 f"### [S1] HIGH {E} HIGH impact SQL injection"],
                  ONE_HIGH)
    assert_backstop_blocks(text)


@pytest.mark.parametrize("footer_lines", [
    ["CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["", "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["| CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0 |"],
    ["CRITICAL: 0, HIGH: 1, MEDIUM: 0, LOW: 0, INFO: 0"],
    ["CRITICAL:0|HIGH:1|MEDIUM:0|LOW:0|INFO:0"],
    ["Critical: 0 | HIGH: 1 | Medium: 0 | Low: 0 | Info: 0"],
])
def test_strict_footer_shapes_are_credited(footer_lines):
    analysis = analyze(one_high_review([], footer_lines))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail


@pytest.mark.parametrize("footer_lines", [
    ["**CRITICAL: 0** | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["Totals: CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0 (one fixed)"],
    ["CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0 | HIGH"],
    ["CRITICAL: 0 | HIGH:", "1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
    ["Notes first.", "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"],
])
def test_other_footer_shapes_credit_nothing(footer_lines):
    """A final footer outside the strict grammar credits no CRITICAL or HIGH
    label, so its UPPER-case labels block (it may still count)."""
    text = one_high_review([], footer_lines)
    assert gate_blocks(text)
    analysis = analyze(text)
    if loops._analyze_review_views(
            loops.normalize_review_text(text)).trusted:
        assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), analysis


def test_only_the_final_footer_is_credited():
    text = one_high_review(["", "## Counts", ZERO, "", "## Notes", "Done."])
    assert_backstop_blocks(text)


@timing_test
def test_strict_footer_grammar_is_linear_on_a_padded_line():
    footer = ("CRITICAL: 0" + " " * RB + "| HIGH: 1 | MEDIUM: 0 | LOW: 0 | "
              "INFO: 0 x")
    text = one_high_review([], [footer])
    assert gate_blocks(text)
    assert median_cpu_seconds(gate_blocks, text) < 0.5


@pytest.mark.parametrize("last_line", [
    f"<!-- EQUIPA-REVIEW-COMPLETE-HIGH {NONCE} -->",
    "<!-- EQUIPA-REVIEW-COMPLETE-CRITICAL -->",
    "<!-- EQUIPA-HIGH-REVIEW-COMPLETE -->",
])
def test_a_marker_line_naming_a_severity_is_read(last_line):
    """Only the exact completion line is blanked; the 3149 marker pattern
    blanked any EQUIPA-* name holding EQUIPA-REVIEW-COMPLETE."""
    text = review("No findings.", ["## Findings", "", "None."], ZERO,
                  last_line=last_line)
    assert_backstop_blocks(text)


def test_a_provenance_line_naming_a_severity_is_read():
    text = review("No findings.", ["## Findings", "", "None."], ZERO,
                  first_line="<!-- EQUIPA-REVIEWER-RUN-HIGH: 0123 -->")
    assert gate_blocks(text)


def test_the_exact_marker_lines_are_still_blanked():
    analysis = analyze(review("No findings.", ["## Findings", "", "None."], ZERO))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail


# --- MEDIUM: no exemptions either (task 3161); anything else blocks ---------------

@pytest.mark.parametrize("body", [
    ["No MEDIUM findings."],
    ["Rated LOW rather than MEDIUM."],
    ["0 MEDIUM results from semgrep."],
])
@pytest.mark.parametrize("context", CONTEXTS)
def test_medium_negations_and_tallies_block(body, context):
    """Task 3152 kept these trusted (MEDIUM exemptions). Task 3161 deleted
    the exemptions, so they block like their CRITICAL and HIGH forms; the
    lower-case prose merges."""
    analysis = analyze(build_review(body, context))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert gate_blocks(build_review(body, context))
    lower = [line.replace("MEDIUM", "medium") for line in body]
    assert not gate_blocks(build_review(lower, context)), lower


@pytest.mark.parametrize("body", [
    ["A MEDIUM issue: stale cache."],
    ["Two MEDIUM issues remain in the cache."],
    [": 0, **2** MEDIUM"],
    ["- **Why not MEDIUM:** the primitive is unreachable."],
])
@pytest.mark.parametrize("context", CONTEXTS)
def test_an_unaccounted_medium_blocks_as_on_main(body, context):
    """Task 3149 (R3143-06) kept these trusted with the MEDIUM counted as a
    logged advisory; main blocks them, so that let main-blocked reviews
    merge. They block again, with their own reason."""
    analysis = analyze(build_review(body, context))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert gate_blocks(build_review(body, context))
    lower = [line.replace("MEDIUM", "medium") for line in body]
    assert not gate_blocks(build_review(lower, context)), lower


# --- The reviewer prompt states the rule -------------------------------------------

def test_prompt_states_there_are_no_exceptions_for_critical_and_high():
    prompt = (REPO / "prompts" / "security-reviewer.md").read_text(
        encoding="utf-8")
    # Task 3161: the rule covers MEDIUM too.
    for phrase in (
        "There are no exceptions for critical, high or medium",
        "write critical, high and medium in lower case everywhere except the "
        "severity label of a finding heading and the `## Counts` footer line",
        "Any other UPPER-case CRITICAL, HIGH or MEDIUM blocks the merge",
        "a negation",
        "a tally",
        "a comparison",
        "`## Counts` on its own line, then one line",
    ):
        assert phrase in prompt, phrase
