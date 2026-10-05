#!/usr/bin/env python3
"""Task 3149: follow-ups of the task 3143 severity-token backstop.

The independent review of 3143 (indep-3143) and EQUIPA's own review
(SECURITY-REVIEW-3143) found that the backstop still merged a real CRITICAL
or HIGH behind an all-zero footer through its zero-count paths. Every row
below fails on the commit before this task.

* R3143-01 / SR3143-01: a heading the parser never counted (inside <p>, after
  "&#35;" decoding) excused itself, its section and its ID.
* R3143-02 / SR3143-02: tally, negation, comparison and list exemptions
  accepted an explicit "<sev>:" or "<sev> -" finding label.
* R3143-03 / SR3143-03: Markdown inline markup split a severity word.
* R3143-04 / SR3143-04, R3143-05: long numeric references and U+0130 raised
  instead of returning a verdict; the gate now turns any parse exception into
  a logged block.
* R3143-06: the reviewer prompt's own prose used UPPER-case severity words;
  MEDIUM-only tokens are counted and logged but no longer block a merge.
* R3143-07: confusable letters and bidi controls.
* R3143-08: the invisible-character list is written with escapes.

Copyright 2026 Forgeborn
"""

import unicodedata
from pathlib import Path

import pytest

from equipa import loops, security_gate
from equipa.security_gate import review_complete_line, reviewer_nonce_line
from tests.review_gate_production import gate_blocks as production_gate_blocks

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{loops.backstop_reason(severity)} at line "
    for severity in loops.MERGE_BLOCKING_SEVERITIES
)

REPO = Path(__file__).resolve().parent.parent
NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_MEDIUM = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0"
E = "\N{EM DASH}"
LOW_HEADING = f"### [E1] LOW {E} verbose error message"


def review(summary, body, footer, *, heading=None):
    """A finished review: provenance line, Summary, body, Counts, sentinel."""
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if heading:
        lines += [heading, "Details.", ""]
    lines += list(body) + ["", "## Files Reviewed", "- app.py", "",
                           "## Methodology", "Read the diff, ran semgrep.", "",
                           "## Counts", footer, review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text):
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def gate_blocks(text):
    """dispatch._security_review_blocks_merge itself (task 3167).

    An untrusted review blocks; a trusted one blocks on CRITICAL or HIGH.
    """
    return production_gate_blocks(text, nonce=NONCE)


def zero_review(body):
    """A zero-finding review: the probes of indep-3143."""
    return review("No findings.", ["## Findings", ""] + list(body), ZERO)


def one_low_review(body):
    """One counted LOW finding: the probes of SECURITY-REVIEW-3143."""
    return review("1 finding.", ["## Notes", ""] + list(body), ONE_LOW,
                  heading=LOW_HEADING)


def assert_gate_blocks(body):
    for text in (zero_review(body), one_low_review(body)):
        assert gate_blocks(text), (body, analyze(text))


def assert_gate_merges(body):
    text = one_low_review(body)
    analysis = analyze(text)
    assert analysis.trusted and not gate_blocks(text), (body, analysis)


# --- R3143-01 / SR3143-01: only headings the parser counted are credited ---------

R3143_01_BODIES = [
    [f"<span></span>### [S2] HIGH {E} see below", "",
     "The HIGH SQL injection in login.py lets anyone log in as admin."],
    [f"<b></b>### [S2] CRITICAL {E} RCE in upload.py"],
    [f"<span>### [S2] HIGH {E} SQL injection in login.py</span>"],
    [f"<p>### [S2] HIGH {E} SQL injection in login.py</p>"],
    [f"<span></span>### [S2] HIGH {E} see below", "", "## Notes", "",
     "See S2: The HIGH SQL injection in login.py lets anyone in."],
    [f"&#35;## [S2] HIGH {E} SQL injection", "",
     "The HIGH SQL injection in login.py lets anyone log in as admin."],
    [f"&#x23;&#x23;&#x23; [S2] CRITICAL {E} RCE in upload.py"],
    [f"<!-- x -->### [S2] HIGH {E} SQL injection in login.py"],
]


@pytest.mark.parametrize("body", R3143_01_BODIES)
def test_r3143_01_uncounted_heading_credits_nothing(body):
    assert_gate_blocks(body)


def test_r3143_01_heading_lines_come_from_the_parser():
    """The credited labels are the parser's own counted headings. Task 3161:
    the MEDIUM label too, and only at the offset the parser attributed (the
    heading and section credit that read lines is deleted)."""
    text = loops.normalize_review_text(review("1 finding.", [
        "## Findings", "", f"### [S1] MEDIUM {E} Missing rate limit",
        f"<p>### [S2] MEDIUM {E} not a heading</p>",
    ], ONE_MEDIUM))
    analysis = analyze(text)
    masked = loops._backstop_masked(text, analysis.heading_offsets).split("\n")
    assert f"### [S1] medium {E} Missing rate limit" in masked
    assert f"<p>### [S2] MEDIUM {E} not a heading</p>" in masked
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    line = masked.index(f"<p>### [S2] MEDIUM {E} not a heading</p>") + 1
    assert analysis.detail.startswith(
        f"{loops.backstop_reason('MEDIUM')} at line {line}:"), analysis.detail


# --- R3143-02 / SR3143-02: no exemption may excuse a finding label ---------------

# Every body of indep-3143's attack table and SECURITY-REVIEW-3143's SR3143-02,
# then the same shapes with the other separators a label uses.
R3143_02_BODIES = [
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
    ["- not exploitable remotely? No. HIGH \N{EM DASH} SQL injection in login.py"],
    ["<!-- no --> HIGH: SQL injection in login.py"],
    ["<span>no</span> HIGH: SQL injection in login.py"],
    ["[x](no) HIGH: SQL injection in login.py"],
    ['[x](y "no") HIGH: SQL injection in login.py'],
    ["None of these are HIGH except: SQL injection in login.py (HIGH)"],
    # Variants: dashes, closing emphasis, zero tallies, table cells.
    ["No HIGH - SQL injection in login.py"],
    ["No HIGH \N{EN DASH} SQL injection in login.py"],
    ["No HIGH\N{EM DASH}SQL injection in login.py"],
    ["**No HIGH**: SQL injection in login.py"],
    ["- 0 HIGH: SQL injection in login.py"],
    [f"- 0 HIGH {E} SQL injection in login.py"],
    ["Zero CRITICAL: RCE via upload handler"],
    ["Without HIGH (SQL injection in login.py)"],
    ["Kept at LOW rather than HIGH: SQL injection in login.py"],
    ["HIGH: 0 in login.py lets anyone in"],
    ["HIGH = 0 auth checks on /admin"],
    ["| HIGH | 0 auth checks on /admin |"],
    ["| finding | HIGH: 0 rate limiting on login |"],
    ["CRITICAL: 0-click RCE in upload"],
    ["Severity: not MEDIUM, HIGH."],
    ["not LOW, MEDIUM, HIGH"],
    # A negation at the end of the line before (soft wrap) is no excuse
    # either when the token labels a finding.
    ["Was it fixed? No", "HIGH SQL injection remains in login.py"],
    ["Was it fixed? No", "HIGH: SQL injection in login.py"],
    ["# Was it fixed? No", "HIGH findings remain."],
    # A generic noun, verb or conjunction run between the token and the
    # label mark leaves the label in place (tester, cycle 2).
    ["No HIGH issues: SQL injection in login.py"],
    ["No HIGH severity issues: SQL injection in login.py"],
    ["No HIGH issues remain: SQL injection in login.py"],
    ["No HIGH issues were found - SQL injection in login.py"],
    [f"0 CRITICAL findings {E} RCE in upload.py"],
    ["No HIGH findings \N{EN DASH} RCE in upload.py"],
    ["No HIGH findings **:** RCE in upload.py"],
    ["Not CRITICAL because: RCE via upload handler in upload.py."],
    ["None is HIGH or CRITICAL, as: RCE in upload.py"],
    ["No HIGH issues = RCE in upload.py"],
    # "below" (new in 3149) is guarded like every other negation.
    ["Rated below CRITICAL: RCE via upload handler"],
    [f"- below HIGH {E} SQL injection in login.py"],
    ["Just below HIGH SQL injection in login.py lets anyone in."],
    # A negated next word excuses the word before it, never itself.
    ["No CRITICAL and no HIGH: SQL injection in login.py"],
    [f"No CRITICAL and no HIGH {E} RCE in upload.py"],
    ["No MEDIUM and no CRITICAL: RCE via upload handler"],
    ["No CRITICAL, and not HIGH SQL injection in login.py"],
]


@pytest.mark.parametrize("body", R3143_02_BODIES)
def test_r3143_02_exemptions_never_excuse_a_label(body):
    assert_gate_blocks(body)


# Tallies and negations of MEDIUM a reviewer writes. They merged under the
# MEDIUM exemptions; task 3161 deleted those, so they block, and the same
# prose in lower case (what the prompt asks for) merges.
R3143_02_MEDIUM_PROSE = [
    ["Rated LOW, not MEDIUM, as it needs a local account."],
    ["Kept at LOW rather than MEDIUM."],
]


@pytest.mark.parametrize("body", R3143_02_MEDIUM_PROSE)
def test_r3143_02_medium_tallies_and_negations_block(body):
    analysis = analyze(one_low_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        loops.backstop_reason("MEDIUM") + " at line "), analysis.detail
    assert_gate_blocks(body)
    lower = analyze(one_low_review([line.replace("MEDIUM", "medium")
                                    for line in body]))
    assert lower.trusted and lower.detail == "", (body, lower)


# Task 3152: these merged under the 3143/3149 exemptions. CRITICAL and HIGH
# now have no exemption at all, so every one blocks with the new reason; the
# same prose in lower case (what the prompt asks for) merges.
R3143_02_CRITICAL_HIGH_PROSE = [
    ["Totals: 0 CRITICAL / 0 HIGH / 0 MEDIUM / 1 LOW / 0 INFO."],
    ["semgrep: CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 across the diff."],
    ["| Severity | Count |", "|---|---|", "| HIGH | 0 |", "| CRITICAL | 0 |"],
    ["**No CRITICAL or HIGH findings.**"],
    ["There are no CRITICAL, HIGH or MEDIUM issues."],
    ["There are no CRITICAL, HIGH, or MEDIUM findings."],
    ["None is CRITICAL or HIGH."],
    ["0 CRITICAL/HIGH results from semgrep."],
    ["HIGH: 0 and CRITICAL: 0 from semgrep."],
    ["| semgrep | HIGH: 0 |"],
    ["No HIGH-severity issues."],
    ["No HIGH or CRITICAL issues were found."],
    ["Rated LOW rather than MEDIUM or HIGH."],
    ["No HIGH issues remain in this diff."],
    ["No HIGH findings were reported by semgrep, and none by bandit."],
    ["Rated LOW rather than HIGH because the endpoint needs an admin token."],
    # Corpus shape: a comparison ending the sentence (blocked on main).
    ["Promotion remains operator-gated, which caps this below HIGH."],
    # Corpus shape: each word negated on its own (merged on main).
    ["**No CRITICAL and no HIGH findings.** There is no new network surface."],
    ["No CRITICAL and no HIGH findings."],
    ["There is no CRITICAL, and no HIGH issue."],
]


def _lower_case_critical_high(body):
    # MEDIUM is lowered too: a MEDIUM left outside a lowered negation's list
    # ("no critical, high or MEDIUM") blocks, as it does on main (task 3152).
    return [line.replace("CRITICAL", "critical").replace("HIGH", "high")
            .replace("MEDIUM", "medium") for line in body]


@pytest.mark.parametrize("body", R3143_02_CRITICAL_HIGH_PROSE)
def test_r3143_02_critical_high_tallies_and_negations_block(body):
    analysis = analyze(one_low_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, body
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), (
        analysis.detail)
    assert_gate_blocks(body)
    lower = analyze(one_low_review(_lower_case_critical_high(body)))
    assert lower.trusted and not gate_blocks(one_low_review(
        _lower_case_critical_high(body))), (body, lower)


def test_r3143_06_soft_wrapped_negation_blocks():
    """The corpus shape: "No" ends the line, the list opens the next one.

    Task 3152: merged under the 3149 soft-wrap exemption; blocks now. The
    same sentence with critical/high/medium in lower case merges.
    """
    body = ["The diff touches only the parser, and the scan found no",
            "CRITICAL/HIGH/MEDIUM issues. Remaining items are LOW/INFO."]
    analysis = analyze(one_low_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS)
    lower = analyze(one_low_review(_lower_case_critical_high(body)))
    assert lower.trusted, lower


@pytest.mark.parametrize("before", [
    "## Findings: no",          # a heading is not a paragraph line
    "| scan | no",              # neither is a table row
    "",                         # a blank line ends the paragraph
])
def test_r3143_06_wrapped_negation_needs_a_paragraph_line(before):
    body = [before, "CRITICAL issues in login.py."]
    assert gate_blocks(one_low_review(body)), body


# --- R3143-04 / SR3143-04, R3143-05: no input raises -----------------------------

@pytest.mark.parametrize("reference", [
    "&#" + "9" * 5000,
    "&#" + "9" * 5000 + ";",
    "&#" + "1" * 4301,
    "&#x" + "F" * 5000 + ";",
    "&#" + "0" * 5000 + "72;",
])
def test_r3143_04_long_numeric_reference_returns_a_verdict(reference):
    text = one_low_review([f"See {reference} in the log."])
    analysis = analyze(text)   # raised ValueError before task 3149
    assert analysis.verdict in (loops.REVIEW_VERDICT_OK,
                                loops.REVIEW_VERDICT_COUNT_MISMATCH)


def test_r3143_04_leading_zeros_still_decode_to_a_letter():
    """A browser reads every leading zero: "&#000000072;" is H."""
    assert_gate_blocks(["&#" + "0" * 40 + "72;IGH: SQL injection in login.py"])


@pytest.mark.parametrize("line", [
    "- H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH: SQL injection in login.py",
    "### [S2] H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH \N{EM DASH} SQLi",
    "| SQL injection | H\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}GH |",
])
def test_r3143_05_dotted_capital_i_blocks_instead_of_raising(line):
    assert_gate_blocks(["## Findings", "", line])


def test_parse_error_becomes_a_logged_block(monkeypatch, tmp_path):
    """ANY parser exception is a count-mismatch with a logged reason."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    logged = []
    monkeypatch.setattr(loops, "_analyze_review_file", explode)
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append((message, fields)))
    path = tmp_path / "SECURITY-REVIEW-7.md"
    counts = loops._count_findings_in_review_file(
        path, task_id=7, text=one_low_review([]))
    assert counts is None
    assert len(logged) == 1, logged
    message, fields = logged[0]
    assert fields["event"] == "count-mismatch"
    assert "review parse error: RuntimeError" in message
    assert "action=treat-as-missing" in message


def test_r3143_04_old_crash_input_blocks_with_a_logged_verdict(monkeypatch, tmp_path):
    """The R3143-04 input end to end through the gate's counting call."""
    logged = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append(message))
    text = one_low_review(["HIGH &#" + "9" * 5000])
    assert loops._count_findings_in_review_file(
        tmp_path / "SECURITY-REVIEW-8.md", task_id=8, text=text) is None
    assert any("unaccounted HIGH token at line" in message for message in logged)


# --- R3143-06: MEDIUM-only tokens block again (task 3152) ------------------------

def test_r3143_06_medium_only_tokens_block_and_are_logged(monkeypatch,
                                                         tmp_path):
    """Task 3152: task 3149 kept this review trusted and logged an advisory;
    main blocks it, so it blocks again (strictly stricter than main)."""
    logged = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **fields: logged.append((message, fields)))
    text = one_low_review(["The cache has a MEDIUM issue: stale entries."])
    counts = loops._count_findings_in_review_file(
        tmp_path / "SECURITY-REVIEW-9.md", task_id=9, text=text)
    assert counts is None
    events = [fields["event"] for _, fields in logged]
    assert events == ["count-mismatch"], logged
    assert (loops.backstop_reason("MEDIUM") + " at line ") in logged[0][0]
    assert "MEDIUM=1 at line" in logged[0][0]
    assert not hasattr(loops, "BACKSTOP_ADVISORY_REASON")


def test_r3143_06_medium_with_high_still_blocks_and_names_both():
    text = one_low_review(["A MEDIUM issue and a HIGH one: SQL injection."])
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS)
    assert "HIGH=1" in analysis.detail and "MEDIUM=1" in analysis.detail


def test_merge_blocking_severities_match_the_gate_policy():
    """MERGE_BLOCKING_SEVERITIES names the severities the gate blocks on."""
    source = (REPO / "equipa" / "dispatch.py").read_text(encoding="utf-8")
    assert ('blocks = counts.get("CRITICAL", 0) > 0 or '
            'counts.get("HIGH", 0) > 0') in source
    assert loops.MERGE_BLOCKING_SEVERITIES == ("CRITICAL", "HIGH")


def test_decide_merge_gate_turns_a_raising_review_check_into_a_block(
        monkeypatch):
    logged = []
    monkeypatch.setattr(security_gate, "_gate_audit_log",
                        lambda message, **fields: logged.append((message, fields)))

    def raising_check(project_dir, task_id, *, block_on_missing):
        raise ValueError("Exceeds the limit (4300 digits)")

    decision = security_gate.decide_merge_gate(
        ["app.py"], security_review_blocks_merge=raising_check,
        project_dir="/nonexistent", task_id=11)
    assert decision.blocks_merge is True
    assert decision.reason == "review parse error: ValueError"
    assert decision.expect_artifact is True and decision.counts is None
    assert [fields["event"] for _, fields in logged] == ["review-parse-error"]
    assert logged[0][1]["task_id"] == 11


# --- R3143-03 / SR3143-03: Markdown inline markup inside a word -------------------

R3143_03_BODIES = [
    ["The **H**IGH SQL injection in login.py lets anyone log in."],
    ["H`IGH`: SQL injection in login.py"],
    ["The H`IGH` SQL injection in login.py lets anyone in."],
    ["H*IG*H: SQL injection in login.py"],
    ["HI__G__H: SQL injection in login.py"],
    ["HI~~~~GH: SQL injection in login.py"],
    ["HI~~GH: SQL injection in login.py"],
    ["HI![](x)GH: SQL injection in login.py"],
    ["[HI](#x)GH: SQL injection in login.py"],
    ["[HI][r]GH: SQL injection in login.py"],
    ["H\\IGH: SQL injection in login.py"],
    ["CRI`TI`CAL: RCE via upload"],
    ["The [CRIT](#a)ICAL RCE in upload.py."],
]


@pytest.mark.parametrize("body", R3143_03_BODIES)
def test_r3143_03_inline_markup_inside_a_word_blocks(body):
    assert_gate_blocks(body)


def test_r3143_03_markup_between_words_adds_nothing():
    """Joining only touches marks between two letters or link text."""
    analysis = analyze(one_low_review([
        "See [the docs](https://example.org/a) and `code` with *emphasis*.",
        "A [high](#x) bar, ~~struck~~ text and a\\_b escape.",
    ]))
    assert analysis.trusted and analysis.detail == "", analysis


# Link shapes the bounded pattern missed (tester, cycle 2), and more of the
# same grammar: any length, parentheses nested up to 32 deep, titles, a line
# break, labels, images. Each renders one UPPER-case word in CommonMark
# (checked with markdown-it-py while writing this test) except the last two
# rows: "*HI*GH" (a renderer may leave the marks as text) and "H1GH" (the
# "1" drawn as I, R3143-07), which the backstop reads as a word either way.
R3143_03_LINK_BODIES = [
    ["[" + "a " * 300 + "HI](#x)GH: SQL injection in login.py"],
    ["[HI](#" + "a" * 5000 + ")GH: SQL injection in login.py"],
    ["[HI](a(b(c(d(e)d)c)b)a)GH: SQL injection in login.py"],
    ["[HI](" + "(" * 32 + "x" + ")" * 32 + ")GH: SQL injection in login.py"],
    ["[HI](a\\)b\\(c)GH: SQL injection in login.py"],
    ['[HI](#x "a)b")GH: SQL injection in login.py'],
    ["[HI](#x 'a(b')GH: SQL injection in login.py"],
    ["[HI](#x (title))GH: SQL injection in login.py"],
    ["[HI](<a b(c>)GH: SQL injection in login.py"],
    ["[HI](", "#x", '"title")GH: SQL injection in login.py'],
    ["[HI]()GH: SQL injection in login.py"],
    ["[HI]GH: SQL injection in login.py", "", "[HI]: #x"],
    ["[HI][" + "r" * 900 + "]GH: SQL injection in login.py", "",
     "[" + "r" * 900 + "]: #x"],
    ["HI![" + "a" * 3000 + "](x)GH: SQL injection in login.py"],
    ["HI![alt][ref]GH: SQL injection in login.py", "", "[ref]: x.png"],
    ["[H](" + "a" * 600 + ")[I](" + "b" * 600 + ")GH: SQL injection"],
    ["H[IGH](#" + "x" * 600 + "): SQL injection in login.py"],
    ["The [CRIT](#" + "a" * 600 + ")ICAL RCE in upload.py."],
    ["*[HI](#" + "x" * 600 + ")*GH: SQL injection in login.py"],
    ["[H1](#" + "a" * 600 + ")GH: SQL injection in login.py"],
]


@pytest.mark.parametrize("body", R3143_03_LINK_BODIES)
def test_r3143_03_links_of_any_shape_read_as_their_text(body):
    assert_gate_blocks(body)


@pytest.mark.parametrize("body", [
    # Not links: 33 levels of parentheses, unbalanced, a blank line.
    ["[HI](" + "(" * 33 + "x" + ")" * 33 + ")"],
    ["[HI](a(b)GH: x"],
    ["[HI](", "", "#x)GH: x"],
])
def test_r3143_03_link_reader_line_numbers_hold(body):
    """Whatever the reader makes of a malformed tail, every token keeps a
    real line number and the review gets a verdict."""
    analysis = analyze(zero_review(body))
    assert analysis.verdict in (loops.REVIEW_VERDICT_OK,
                                loops.REVIEW_VERDICT_COUNT_MISMATCH), analysis


def test_r3143_03_link_join_reports_the_line_of_the_link():
    text = zero_review(["Intro.", "", "See [HI](", "#x)GH: SQL injection"])
    analysis = analyze(text)
    line = text.split("\n").index("See [HI](") + 1
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert f"HIGH=1 at line {line} " in analysis.detail, analysis.detail


def test_r3143_03_ordinary_links_and_images_add_nothing():
    analysis = analyze(one_low_review([
        "See [the docs](https://example.org/a_(b)) and ![logo](x.png).",
        "Refs: [RFC 9110][rfc], [CWE-89] and [a](<b c> 'title').",
        "Code: `[x](y)`, a[0] = b[1] and f(x)(y).",
        "",
        "[rfc]: https://www.rfc-editor.org/rfc/rfc9110",
    ]))
    assert analysis.trusted and analysis.detail == "", analysis


# --- R3143-07: lookalike letters and bidi controls --------------------------------

def _spell(word, replacements):
    return "".join(replacements.get(index, letter)
                   for index, letter in enumerate(word))


R3143_07_WORDS = [
    "HlGH", "H1GH", "H|GH", "CRlTlCAL", "CR1T1CAL", "MEDlUM",
    _spell("HIGH", {0: "\N{CYRILLIC CAPITAL LETTER EN WITH DESCENDER}"}),
    _spell("MEDIUM", {0: "\N{GREEK CAPITAL LETTER SAN}"}),
    _spell("HIGH", {0: "\N{LATIN CAPITAL LETTER H WITH STROKE}"}),
    _spell("CRITICAL", {1: "\N{LATIN LETTER YR}"}),
    _spell("HIGH", {0: "\N{CANADIAN SYLLABICS NUNAVUT H}"}),
    _spell("CRITICAL", {1: "\N{CANADIAN SYLLABICS TLHI}",
                        6: "\N{CANADIAN SYLLABICS CARRIER GHO}",
                        7: "\N{CANADIAN SYLLABICS MA}"}),
    _spell("MEDIUM", {0: "\N{CANADIAN SYLLABICS CARRIER GO}",
                      2: "\N{CANADIAN SYLLABICS CARRIER PE}",
                      4: "\N{CANADIAN SYLLABICS TE}"}),
    _spell("HIGH", {1: "\N{RUNIC LETTER ISAZ IS ISS I}"}),
    _spell("HIGH", {1: "\N{TIFINAGH LETTER YAN}"}),
    _spell("MEDIUM", {1: "\N{TIFINAGH LETTER YADD}"}),
    _spell("HIGH", {1: "\N{NKO LETTER A}"}),
    _spell("HIGH", {1: "\N{OLD ITALIC LETTER I}"}),
    _spell("HIGH", {1: "\N{DIVIDES}"}),
    _spell("CRITICAL", {3: "\N{DOWN TACK}"}),
    _spell("CRITICAL", {3: "\N{CARIAN LETTER D}"}),
    _spell("HIGH", {2: "\N{CHEROKEE LETTER YU}"}),
    _spell("MEDIUM", {4: "\N{ARMENIAN CAPITAL LETTER SEH}"}),
]


# TeX math draws no gap between letters (indep-3143's LaTeX row), and an
# empty link between two halves joins them (indep-3143's "HI[](x)GH").
R3143_07_MATH_BODIES = [
    ["$\\mathrm{H}\\mathrm{I}\\mathrm{G}\\mathrm{H}$: SQL injection in login.py"],
    ["The $\\mathrm{H}\\mathrm{I}\\mathrm{G}\\mathrm{H}$ SQL injection is open."],
    ["$H I G H$: SQL injection in login.py"],
    ["$\\text{C}\\,\\text{R}\\text{ITICAL}$: RCE via upload"],
    ["$${\\bf H}{\\bf I}{\\bf G}{\\bf H}$$: SQL injection in login.py"],
    ["HI[](x)GH: SQL injection in login.py"],
]


@pytest.mark.parametrize("body", R3143_07_MATH_BODIES)
def test_r3143_07_tex_math_and_empty_links_join_a_word(body):
    assert_gate_blocks(body)


def test_r3143_07_ordinary_dollar_text_adds_nothing():
    analysis = analyze(one_low_review([
        "It costs $5 and $10 to run; set $HOME and $PATH first.",
        "Complexity is $O(n \\log n)$ and $\\mathrm{high}$ is lower case.",
    ]))
    assert analysis.trusted and analysis.detail == "", analysis


@pytest.mark.parametrize("word", R3143_07_WORDS)
def test_r3143_07_lookalike_severity_word_is_read(word):
    """Every lookalike blocks; MEDIUM with its own reason (task 3152)."""
    body = [f"Notes: the {word} issue is SQL injection in login."]
    if len(word) == 6:   # MEDIUM
        analysis = analyze(one_low_review(body))
        assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
        assert analysis.detail.startswith(
            loops.backstop_reason("MEDIUM") + " at line "), analysis
        assert "MEDIUM=1 at line" in analysis.detail, analysis
    assert_gate_blocks(body)


BIDI_CONTROLS = [
    "\N{LEFT-TO-RIGHT EMBEDDING}", "\N{RIGHT-TO-LEFT EMBEDDING}",
    "\N{POP DIRECTIONAL FORMATTING}", "\N{LEFT-TO-RIGHT OVERRIDE}",
    "\N{RIGHT-TO-LEFT OVERRIDE}", "\N{LEFT-TO-RIGHT ISOLATE}",
    "\N{RIGHT-TO-LEFT ISOLATE}", "\N{FIRST STRONG ISOLATE}",
    "\N{POP DIRECTIONAL ISOLATE}",
]


@pytest.mark.parametrize("control", BIDI_CONTROLS)
def test_r3143_07_bidi_control_rejects_the_review(control):
    text = one_low_review([f"Notes: the {control}HGIH issue is in the cache."])
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    line = text.split("\n").index(
        f"Notes: the {control}HGIH issue is in the cache.") + 1
    assert analysis.detail.startswith(
        f"bidi control character: U+{ord(control):04X} at line {line}"), (
        analysis.detail)


def test_r3143_07_reversed_word_renders_as_a_severity():
    """The probe of indep-3143: RLO + HGIH + PDF shows HIGH to a reader."""
    body = ["\N{RIGHT-TO-LEFT OVERRIDE}HGIH\N{POP DIRECTIONAL FORMATTING}"
            ": SQL injection in login.py"]
    assert_gate_blocks(body)


def test_r3143_07_provenance_rejects_bidi_in_the_bytes(tmp_path):
    """The gate parses normalised text (controls stripped), so provenance
    looks at the bytes as written, before any other check."""
    path = tmp_path / "SECURITY-REVIEW-12.md"
    path.write_text(one_low_review(
        ["Line \N{RIGHT-TO-LEFT ISOLATE}x\N{POP DIRECTIONAL ISOLATE}"]),
        encoding="utf-8")
    verdict = security_gate.verify_reviewer_provenance(12, path)
    assert verdict.trusted is False
    assert verdict.reason.startswith("review-bidi-control-U+2067-at-line-"), (
        verdict.reason)
    assert verdict.audit_event == "artifact-provenance-rejected"


@pytest.mark.parametrize("reference, code_point", [
    ("&#x202E;", "U+202E"),
    ("&#X0202e", "U+202E"),
    ("&#8238;", "U+202E"),
    ("&#0008294", "U+2066"),
    ("&#x2069;", "U+2069"),
])
def test_r3143_07_character_reference_to_a_bidi_control_is_rejected(
    tmp_path, reference, code_point,
):
    """A reference renders as the control itself (tester, cycle 2)."""
    text = one_low_review([f"{reference}HGIH: SQL injection in login.py"])
    found = security_gate.find_bidi_control(text)
    assert found is not None and found.startswith(f"{code_point} reference"), (
        found)
    assert analyze(text).verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    path = tmp_path / "SECURITY-REVIEW-12.md"
    path.write_text(text, encoding="utf-8")
    verdict = security_gate.verify_reviewer_provenance(12, path)
    assert verdict.trusted is False
    assert verdict.reason.startswith(
        f"review-bidi-control-{code_point}-reference-at-line-"), verdict.reason


@pytest.mark.parametrize("text", [
    "&#x202EA; and &#82380; and &#x2066F are other characters",
    "&#x202; &#823; &#x2065; &#8293; &#8239;",
    "&amp;#x202E; is written out, not decoded",
])
def test_r3143_07_other_references_are_not_bidi_controls(text):
    assert security_gate.find_bidi_control(text) is None


def test_r3143_07_left_to_right_and_arabic_marks_are_not_rejected():
    """LRM/RLM/ALM cannot reorder Latin letters; they are stripped as before."""
    text = one_low_review(["Notes \N{LEFT-TO-RIGHT MARK}x\N{RIGHT-TO-LEFT MARK}"
                           "y\N{ARABIC LETTER MARK}z."])
    assert security_gate.find_bidi_control(text) is None
    assert analyze(text).trusted


# --- R3143-08: the invisible-character list uses escapes ------------------------

def test_r3143_08_security_gate_source_holds_no_invisible_character():
    """The PUBLIC repo must not ship raw bidi or zero-width characters."""
    source = (REPO / "equipa" / "security_gate.py").read_text(encoding="utf-8")
    hidden = sorted({
        f"U+{ord(char):04X}" for char in source
        if security_gate._INVISIBLE_CHARS_RE.match(char)
        or unicodedata.category(char) in ("Cf", "Zl", "Zp")
    })
    assert hidden == [], hidden


def test_r3143_08_escaped_list_still_strips_every_format_character():
    missing = [
        f"U+{code_point:04X}" for code_point in range(0x110000)
        if unicodedata.category(chr(code_point)) == "Cf"
        and not security_gate._INVISIBLE_CHARS_RE.match(chr(code_point))
    ]
    assert missing == []


# --- Timing helpers (tester, cycle 2): same output, less time --------------------

TRANSLATE_SAMPLES = [
    "",
    "plain ASCII HIGH: 0\n",
    "ascii \x01\x7f\r controls\n",
    f"### [S2] HIGH {E} x\n" * 50,
    "H\N{GREEK CAPITAL LETTER IOTA}GH and \N{CYRILLIC CAPITAL LETTER EN}IGH\n",
    "HI\N{ZERO WIDTH SPACE}GH \N{COMBINING ACUTE ACCENT}\N{SOFT HYPHEN}\n",
    "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}" * 40 + "\n",
    "\N{CJK UNIFIED IDEOGRAPH-4E00}" * 300 + "x\n",
    "a\N{EM DASH}" * 500,
    "\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\x85 x",
]


@pytest.mark.parametrize("table_name", [
    "_BACKSTOP_LETTER_FOLDS", "_CONFUSABLE_LETTERS",
])
@pytest.mark.parametrize("text", TRANSLATE_SAMPLES)
def test_translate_non_ascii_equals_translate(table_name, text):
    table = getattr(loops, table_name)
    assert loops._translate_non_ascii(text, table) == text.translate(table)


@pytest.mark.parametrize("text", TRANSLATE_SAMPLES)
def test_backstop_translated_equals_translate(text):
    expected = text.translate(loops._BACKSTOP_CHARACTERS)
    assert loops._backstop_translated(text) == expected


def test_fold_tables_leave_ascii_and_line_feeds_alone():
    """_translate_non_ascii's precondition: no table maps an ASCII character
    or produces a line feed; _backstop_translated's: the backstop table
    deletes exactly the ASCII controls other than tab and line feed."""
    for table in (loops._BACKSTOP_LETTER_FOLDS, loops._CONFUSABLE_LETTERS):
        assert not [key for key in table if key < 0x80]
        assert not [value for value in table.values() if "\n" in value]
    for code_point in range(0x80):
        char = chr(code_point)
        expected = ("" if loops._BACKSTOP_ASCII_CONTROLS_RE.fullmatch(char)
                    else char)
        assert loops._BACKSTOP_CHARACTERS[code_point] == expected, code_point
    for code_point in range(0x80, 0x30000):
        assert "\n" not in loops._BACKSTOP_CHARACTERS[code_point], code_point


@pytest.mark.parametrize("tail", [
    ": 0",
    ": 0 | CRITICAL: 0",
    ": 0 and 0 HIGH from semgrep.",
    ": 0 | 0 HIGH: 0",
    ": 0, **2** MEDIUM",
    ": 0 0 HIGH**: 0",
    ": 0 rate limiting on login",
    ": 0 0 HIGH:",
    ": 0" + " 1" * 95 + " x",
])
def test_every_medium_tally_tail_blocks(tail):
    """These tails were read by the MEDIUM tally grammar, and the first six
    excused the word. Task 3161 deleted the grammar: a MEDIUM tally blocks
    whatever follows it, and its lower-case form merges."""
    body = ["semgrep MEDIUM" + tail]
    analysis = analyze(one_low_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert loops.backstop_reason("MEDIUM") + " at line " in analysis.detail
    assert_gate_blocks(body)
    lower = [line.replace("CRITICAL", "critical").replace("HIGH", "high")
             .replace("MEDIUM", "medium") for line in body]
    assert analyze(one_low_review(lower)).trusted, lower


# --- Timing: lines holding a severity word are capped ------------------------
#
# Each such line costs about 40 microseconds across the shape rules (as
# written, from HTML, as rendered) and the backstop views. A 200 KB review of
# 7,500 of them took 0.6 s on main. More than _REVIEW_MAX_SEVERITY_LINES (the
# real reviews hold at most 138) is not parsed and blocks as incomplete.

CAP = loops._REVIEW_MAX_SEVERITY_LINES


def _flood(template, count):
    return [template.format(number=number) for number in range(count)]


@pytest.mark.parametrize("template", [
    "<p>### [S{number}] HIGH " + E + " x</p>",
    "low risk item {number}",
    "Information note {number}.",
])
def test_review_over_the_severity_line_cap_blocks_as_incomplete(template):
    analysis = analyze(zero_review(_flood(template, CAP + 1)))
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE, analysis
    assert "too dense to parse" in analysis.detail
    assert "as written" in analysis.detail


def test_severity_lines_spelled_only_when_rendered_count_toward_the_cap():
    """A reference ("H&#73;GH") spells the word only once it is decoded."""
    body = _flood("Item {number}: H&#73;GH.", CAP + 1)
    assert not loops._severity_word_lines("\n".join(body))[1]
    analysis = analyze(zero_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE, analysis
    assert "as rendered" in analysis.detail


def test_review_under_the_severity_line_cap_is_still_parsed():
    """Just under the cap the gate reads every line as before."""
    body = _flood("<p>### [S{number}] HIGH " + E + " x</p>", CAP - 50)
    analysis = analyze(zero_review(body))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), analysis.detail
    lower = analyze(zero_review(_flood("a low risk item {number}", CAP - 50)))
    assert lower.trusted, lower


def test_severity_line_cap_is_far_above_any_real_review():
    # 138 lines in the longest of the 870 distinct real reviews.
    assert CAP >= 7 * 138
