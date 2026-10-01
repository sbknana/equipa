#!/usr/bin/env python3
"""Task 3130: review-gate shapes still missed after task 3122.

The independent review of 3122 (F2, F3, F4, F6) and the 3122 security review
(R3122-01 to R3122-04) listed finding shapes that still merged behind an
all-zero ``## Counts`` footer. Each must now fail closed (count-mismatch).
Next to each group sits benign prose of the same look, which must still
merge. The prompt must tell reviewers to write severities in UPPER case
without hinting that another case escapes the gate, and a 200 KB adversarial
review must still parse in under 1 s.

Copyright 2026 Forgeborn
"""

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "prompts" / "security-reviewer.md"

NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_HIGH = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_MEDIUM = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0"
# Spelled by name so a reader can tell it from a Latin H.
ETA = "\N{GREEK CAPITAL LETTER ETA}"


def review(summary: str, body: list[str], footer: str, low_heading: bool,
           after_footer: list[str] | None = None) -> str:
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if low_heading:
        lines += ["### [E1] LOW - verbose error message", "Details.", ""]
    lines += body + ["", "## Files Reviewed", "- app.py", "- tests/test_app.py",
                     "", "## Methodology", "Read the diff, ran semgrep.", "",
                     "## Counts", footer]
    lines += (after_footer or []) + [review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text: str) -> loops.ReviewCountAnalysis:
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def assert_blocks_behind_zero_footer(body: list[str],
                                     severity: str = "HIGH") -> None:
    analysis = analyze(review("No findings.", body, ZERO, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        body, analysis.detail)
    # Either a candidate rule saw the severity, or a strict finding heading
    # counted it and the zero footer disagrees.
    assert (f"{severity}=" in analysis.detail
            or analysis.header_counts[severity] > 0), (body, analysis.detail)


def assert_prose_merges(body: list[str]) -> None:
    analysis = analyze(review("1 finding.", body, ONE_LOW, low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (body, analysis.detail)
    assert analysis.counts == {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0,
                               "LOW": 1, "INFO": 0}


# Task 3143: a standalone UPPER-case severity word, as the backstop reads it.
SEVERITY_TOKEN = re.compile(r"(?<![^\W_])(CRITICAL|HIGH|MEDIUM)(?![^\W_])")


def lowercase_severity_words(line: str) -> str:
    return SEVERITY_TOKEN.sub(lambda match: match.group(1).lower(), line)


def assert_only_the_backstop_blocks(text: str) -> loops.ReviewCountAnalysis:
    """The rules trusted ``text`` (the backstop runs only then) and the
    severity-token backstop blocked it."""
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith("unaccounted severity token:"), (
        analysis.detail)
    return analysis


def assert_compliant_prose_merges(body: list[str]) -> None:
    """Task 3143: the reviewer prompt allows UPPER-case CRITICAL, HIGH and
    MEDIUM only as a finding's label (never in prose, code or examples), so
    compliant prose is written in lower case and merges. The UPPER-case
    original is still read as prose or code by every rule: only the
    severity-token backstop blocks it."""
    assert_prose_merges([lowercase_severity_words(line) for line in body])
    if any(SEVERITY_TOKEN.search(line) for line in body):
        assert_only_the_backstop_blocks(
            review("1 finding.", body, ONE_LOW, low_heading=True))


# --- F2 / R3122-02: HTML list items and line breaks ----------------------------

@pytest.mark.parametrize("body", [
    ["<ul><li>SQL injection (HIGH)</li></ul>"],
    ["<li>SQL injection - HIGH</li>"],
    ["<ul>", "<li><b>SQLi</b> (HIGH)</li>", "</ul>"],
    ["<ol><li>SQLi — HIGH</li></ol>"],
    ["Finding 1<br>HIGH: SQL injection"],
    ["Finding 1<br/>HIGH: SQL injection"],
    ["Finding 1<BR />HIGH: SQL injection"],
    ["Finding 1<hr>HIGH: SQL injection"],
])
def test_html_list_item_and_line_break_fail_closed(body):
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


@pytest.mark.parametrize("body", [
    ["<ul><li>Test coverage for the parser is high.</li></ul>"],
    ["<li>Performance impact of the change - Low</li>"],
    ["Scan finished<br>semgrep reported nothing new."],
    ["<ul><li>Overall risk: LOW</li></ul>"],
])
def test_html_list_item_prose_merges(body):
    assert_prose_merges(["## Notes", ""] + body)


def test_html_list_item_is_counted_once():
    analysis = analyze(review(
        "No findings.", ["## Findings", "", "<li>SQL injection - HIGH</li>"],
        ZERO, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.endswith("HIGH=1"), analysis.detail


# --- F3 / R3122-01: padded Severity fields --------------------------------------

@pytest.mark.parametrize("body", [
    ["| ID | Rating | Issue |", "|---|---|---|",
     "| S1 | Severity:            HIGH | SQLi |"],
    ["| ID | Rating | Issue |", "|---|---|---|",
     "| S1 | Severity:" + " " * 40 + "HIGH | SQLi |"],
    ["- Severity:              HIGH"],
    ["**Severity:**            HIGH"],
    ["SQL injection in login, severity:" + "\t" * 12 + "HIGH"],
])
def test_padded_severity_field_fails_closed(body):
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


def test_padded_indented_code_stays_code():
    """Leading indentation is kept, so an indented code line is not a field.
    Task 3143: severity words in code are lower case (reviewer prompt)."""
    assert_compliant_prose_merges(["## Notes", "", "Example output:", "",
                                   "        log.info('HIGH:   ' + message)"])


# --- F4 / R3122-03: shapes the prompt promised -----------------------------------

@pytest.mark.parametrize("line", [
    "- SQLi -- HIGH",
    "- SQL injection in the search endpoint -- CRITICAL",
    "[HIGH] SQL injection in login handler",
    "(HIGH) SQL injection in login handler",
    "**[HIGH]** SQL injection in login handler",
    "[S2] HIGH SQL injection",
    "1. S1 HIGH SQLi in search",
    "- S1 HIGH SQLi in search",
])
def test_promised_leading_and_trailing_shapes_fail_closed(line):
    severity = "CRITICAL" if "CRITICAL" in line else "HIGH"
    assert_blocks_behind_zero_footer(["## Findings", "", line, ""], severity)


def test_bracketed_severity_in_summary_tag_fails_closed():
    assert_blocks_behind_zero_footer([
        "## Findings", "", "<details>",
        "<summary>[HIGH] SQL injection in login handler</summary>",
        "</details>",
    ])


@pytest.mark.parametrize("line", [
    "The severity is HIGH.",
    "Open redirect in /next, severity is HIGH",
    "SQL injection in search; the severity is rated HIGH.",
    "Stored XSS with a severity of HIGH.",
])
def test_severity_with_a_verb_fails_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


@pytest.mark.parametrize("line", [
    "Priority: HIGH",
    "- **Priority:** HIGH",
    "Priority: High",
    "- Priority level: CRITICAL",
])
def test_priority_field_fails_closed(line):
    severity = "CRITICAL" if "CRITICAL" in line else "HIGH"
    assert_blocks_behind_zero_footer(["## Findings", "", line], severity)


@pytest.mark.parametrize("line", [
    "Findings whose severity is HIGH or above block the merge.",
    "No finding reached severity HIGH or above.",
    "The severity of E1 is low because only admins reach it.",
    "Priority: low, cleanup only.",
    "- Priority: fix the logging first.",
    "- Priority of the cleanup is high.",
])
def test_verb_and_priority_prose_merges(line):
    assert_compliant_prose_merges(["## Notes", "", line])


@pytest.mark.parametrize("line", [
    "Rating: HIGH",
    "- **Rating:** HIGH",
    "Finding: SQLi, rated HIGH",
    "Finding S1 is rated HIGH.",
    "Token reuse, rated HIGH severity, in session.py",
    "SQL injection in login. Risk: HIGH.",
    "Token leak in request logs; Impact: HIGH",
    "- SQLi \N{EM DASH} HIGH \N{EM DASH} auth.py",
    "- SQLi - HIGH - login handler",
    "- **H**IGH: SQL injection",
    "HI*G*H: SQL injection",
])
def test_rated_rating_mid_line_alias_dashes_and_split_emphasis_fail_closed(
        line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


@pytest.mark.parametrize("line", [
    "Rating: 4 of 5 for clarity.",
    "- Rating: high confidence in the fix.",
    "Findings rated HIGH or above block the merge.",
    "The fix is rated high by the team.",
    "The endpoint is internal. Risk: low.",
    "Tokens are signed. Impact: High-value sessions stay safe.",
    "The formula is a*b*c for the cache size.",
    "Use - dashes - freely in prose.",
])
def test_rated_rating_mid_line_alias_dash_and_emphasis_prose_merges(line):
    assert_compliant_prose_merges(["## Notes", "", line])


@pytest.mark.parametrize("cell, higher", [
    ("HIGH/MEDIUM", "HIGH"),
    ("MEDIUM/HIGH", "HIGH"),
    ("Medium / High", "HIGH"),
    ("LOW/CRITICAL", "CRITICAL"),
    ("**HIGH/MEDIUM**", "HIGH"),
    ("MEDIUM–HIGH", "HIGH"),
])
def test_severity_range_cell_counts_the_higher(cell, higher):
    body = ["## Findings", "", "| ID | Issue | Rating |", "|---|---|---|",
            f"| S1 | SQL injection | {cell} |"]
    assert_blocks_behind_zero_footer(body, higher)
    # A footer that counts only the lower severity still blocks.
    lower_footer = {
        "HIGH": ONE_MEDIUM,
        "CRITICAL": ONE_LOW,
    }[higher]
    analysis = analyze(review("1 finding.", body, lower_footer,
                             low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        cell, analysis.detail)
    assert f"{higher}=1" in analysis.detail


def test_severity_range_cell_counted_by_footer_merges():
    """Task 3143: only the higher bound of a range is a counted finding, so
    the UPPER-case lower bound is a severity word that labels no counted
    finding: the rules trust the review and the backstop blocks it. With
    the lower bound in lower case (reviewer prompt) the review merges with
    the count the footer states."""
    body = ["## Findings", "", "| ID | Issue | Rating |", "|---|---|---|",
            "| S1 | SQL injection | HIGH/MEDIUM |"]
    # Task 3149 (R3143-06): the unaccounted MEDIUM is counted and logged as
    # an advisory (MEDIUM never blocks a merge); the HIGH still holds it.
    advisory = analyze(review("1 finding.", body, ONE_HIGH, low_heading=False))
    assert advisory.counts["HIGH"] == 1
    assert advisory.counts["MEDIUM"] == 1, advisory.detail
    assert "MEDIUM=1" in advisory.detail
    assert "(advisory)" in advisory.detail
    body[-1] = "| S1 | SQL injection | HIGH/medium |"
    analysis = analyze(review("1 finding.", body, ONE_HIGH, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts["HIGH"] == 1


@pytest.mark.parametrize("row", [
    "| cache refactor | Low/no risk |",
    "| parser | high/low watermark logic |",
    "| docs | see HIGH/MEDIUM guidance in the README |",
])
def test_range_prose_cell_merges(row):
    assert_compliant_prose_merges(["## Notes", "", "| Item | Note |", "|---|---|", row])


# --- F4 / R3122-04: lookalike letters, entities and comments ---------------------

@pytest.mark.parametrize("line", [
    f"{ETA}IGH: SQL injection in login",
    "\N{CYRILLIC CAPITAL LETTER EN}IGH: SQL injection in login",
    "H\N{CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I}GH: SQL injection",
    "HI\N{CYRILLIC CAPITAL LETTER KOMI SJE}H: SQL injection in login",
    f"- SQL injection in login - {ETA}\N{GREEK CAPITAL LETTER IOTA}GH",
    "| S1 | SQLi | \N{CHEROKEE LETTER MI}IGH |",
    f"{ETA}igh: SQL injection in login",  # Title case, lookalike capital
    "\N{LISU LETTER XA}IGH: SQL injection in login",
])
def test_confusable_letters_fail_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


def test_confusable_heading_is_a_strict_finding_heading():
    analysis = analyze(review(
        "1 finding.", ["## Findings", "", f"### [S1] {ETA}IGH — SQL injection"],
        ONE_HIGH, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.header_counts["HIGH"] == 1


@pytest.mark.parametrize("line", [
    "&#72;IGH: SQL injection",
    "&#x48;IGH: SQL injection",
    "&#X48;&#73;GH: SQL injection",
    "HIGH&#58; SQL injection",
    "- SQL injection in login - &#72;IGH",
    "&#919;IGH: SQL injection",                  # entity for Greek Eta
    "HI<!-- x -->GH: SQL injection",
    "HI<!---->GH: SQL injection",
    "- SQL injection in login - H<!-- split -->IGH",
])
def test_entities_and_comments_inside_a_word_fail_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


@pytest.mark.parametrize("heading", [
    "### [S1] &#72;IGH — SQL injection",
    "### [S1] H<!-- -->IGH — SQL injection",
])
def test_entity_or_comment_in_heading_fails_closed(heading):
    analysis = analyze(review("No findings.", ["## Findings", "", heading],
                             ZERO, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        heading, analysis.detail)
    assert analysis.header_counts["HIGH"] == 1


@pytest.mark.parametrize("body", [
    # Decoding must never CREATE code that hides the rest of the line.
    ["&#96;HIGH: SQL injection&#96;"],
    ["&#96;&#96;&#96;", "HIGH: SQL injection", "&#96;&#96;&#96;"],
    # A decoded line break must not turn literal backticks into a fence.
    ["x&#10;```", "HIGH: SQL injection", "```"],
])
def test_decoded_entities_never_hide_a_finding(body):
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


@pytest.mark.parametrize("body", [
    ["<!--", "HIGH: SQL injection kept for the operator", "-->"],
    ["<!--", "### [S1] HIGH — SQL injection", "-->"],
    ["<!-->", "HIGH: SQL injection", "-->"],
    ["<!--->", "HIGH: SQL injection", "-->"],
])
def test_severity_inside_a_comment_still_blocks(body):
    """A multi-line comment's lines still block, as they did before (3122).

    Stripping comments for the reader view must never loosen the gate: the
    review is parsed again with its comments kept and the stricter view wins.
    """
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


@pytest.mark.parametrize("opener", ["<!-->", "<!--->", "<!--"])
def test_comment_around_a_draft_summary_is_still_incomplete(opener):
    """A comment, even a malformed one, cannot hide an unfinished Summary."""
    text = "\n".join([
        reviewer_nonce_line(NONCE), "# Security Review", "", opener,
        "## Summary", "IN PROGRESS - initial skeleton", "-->", "",
        "## Files Reviewed", "- app.py", "", "## Counts", ZERO,
        review_complete_line(NONCE),
    ]) + "\n"
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE, analysis.detail


@pytest.mark.parametrize("body", [
    ["AT&amp;T and R&amp;D endpoints were reviewed; &lt;none&gt; flagged."],
    ["<!-- reviewer note: nothing else to add -->"],
    ["Coverage is high<!-- measured with pytest-cov -->."],
    # Greek and Cyrillic prose: folding lookalikes must not invent a severity.
    ["Η προστασία "
     "είναι εντάξει."],
    ["Ничего не "
     "найдено."],
    ["&#72;igh-level design is unchanged."],
])
def test_entity_comment_and_non_latin_prose_merges(body):
    assert_prose_merges(["## Notes", ""] + body)


def test_comment_markers_quoted_in_code_open_no_comment():
    """Real review: a "<!--" in one code span and a "-->" in another, 30 lines
    later, hid the two finding headings between them."""
    body = [
        "## Findings", "",
        "Parser probes: `<!-x`, `<!--`, `<![CDATA[` and unclosed quotes.", "",
        "### [S1] MEDIUM — preview has no post-parse backstop",
        "Details.", "",
        "### [S2] LOW — deep import of an internal file",
        "- **Detail:** `<mj-raw><!-- 3000 x --></mj-raw>` compiles.",
    ]
    footer = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 1 | INFO: 0"
    analysis = analyze(review("2 findings.", body, footer, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.header_counts["MEDIUM"] == 1
    assert analysis.header_counts["LOW"] == 1


@pytest.mark.parametrize("body", [
    # Real review: a wrapped prose line that happens to open with an ID and
    # an upstream finding's severity.
    ["The guard now covers the tables named by the upstream findings (the",
     "  SR-2937 CRITICAL and the SR-2949 grounding findings)."],
    ["Re-checked earlier work:", "SR-12 HIGH was fixed in the previous round."],
])
def test_wrapped_prose_line_opening_with_an_id_merges(body):
    assert_compliant_prose_merges(["## Notes", ""] + body)


@pytest.mark.parametrize("line", [
    "HI&#8203;GH: SQL injection",       # zero-width space renders as nothing
    "HI&shy;GH: SQL injection",         # soft hyphen renders as nothing
    "&#xFF28;IGH: SQL injection",       # fullwidth H
    "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;HIGH: SQL injection",
])
def test_invisible_fullwidth_and_blank_references_fail_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


@pytest.mark.parametrize("body", [
    # Blocked before task 3130 by the text as written; the rendered view
    # alone would read a zero tally. The stricter view must win.
    ["HIGH: &#48; SQL injection in login"],
    ["## [S1] 0      HIGH — SQL injection"],
    # Blocked before as a bare line; now a list item, still blocked.
    ["<ul><li>HIGH</li></ul>"],
    ["<ul><li>\N{LARGE RED CIRCLE} HIGH</li></ul>"],
    ["<ol><li>S1: HIGH</li></ol>"],
])
def test_rendered_view_never_loosens_an_earlier_block(body):
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


def test_terse_review_with_comment_lines_is_not_near_empty():
    """Stripping the provenance comments does not make a review near-empty."""
    text = "\n".join([
        reviewer_nonce_line(NONCE), "# Security Review", "No findings.",
        "## Counts", ZERO, review_complete_line(NONCE),
    ]) + "\n"
    assert analyze(text).verdict == loops.REVIEW_VERDICT_OK


# --- F6 + title case in the positions the prompt lists ------------------------------

@pytest.mark.parametrize("line", [
    "High: SQL injection in the search endpoint",
    "Critical: remote code execution in upload",
    "> High: session fixation after login",
    "**High:** SQL injection in search",
    "**High — nonce reuse in the session cipher**",
    "S2 (High): stored XSS in comments",
    "- SQL injection in login handler - High",
    "- **SQLi** — High",
    "- SQLi (CVSS 8.1, High)",
    "Finding 3: open redirect, severity High, in auth.py",
    "| S1 | SQLi | High: token leak in logs |",
])
def test_title_case_in_listed_positions_fails_closed(line):
    severity = "CRITICAL" if "Critical" in line else "HIGH"
    body = ["## Findings", "", line]
    if line.startswith("|"):
        body = ["## Findings", "", "| ID | Issue | Note |", "|---|---|---|",
                line]
    assert_blocks_behind_zero_footer(body, severity)


@pytest.mark.parametrize("line", [
    "High-level design is unchanged.",
    "High availability is out of scope for this change.",
    "Info: semgrep 1.176.0 found nothing new.",
    "Low: nothing to report for the cache layer.",
    "- Performance impact of the change - Low",
    "- Confidence in the fix: high",
    "**Medium (30 min):** rotate the staging key.",
    "Critical path latency did not change.",
])
def test_title_case_prose_merges(line):
    assert_prose_merges(["## Notes", "", line])


def _prompt_format_rule() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    start = text.index("**Finding-shaped lines are counted as findings too.**")
    return text[start:text.index("\n", start)]


def test_prompt_tells_reviewers_to_write_upper_case_without_an_escape_hint():
    rule = _prompt_format_rule()
    assert "other cases in most" not in rule
    assert "other cases" not in rule
    # Task 3143 replaced "Always write every severity word in UPPER case" and
    # the list of places after it: UPPER case is now for a finding's label
    # only, and any other UPPER-case severity word blocks (the backstop).
    assert ("Write CRITICAL, HIGH and MEDIUM in UPPER case ONLY when "
            "labelling an actual finding") in rule
    # The instruction comes before its consequence, not as a caveat on it.
    assert rule.index("UPPER case ONLY") < rule.index("BLOCKS the merge")


def test_prompt_keeps_every_other_format_obligation():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    for obligation in (
        "**One heading per finding:** `### [TAG-NN] SEVERITY — title`",
        "**A finding heading still counts when it is marked fixed or resolved.**",
        "**The `## Counts` footer must agree with the finding headings.**",
        "BLOCKS the merge",
        "Write it ONCE, as the LAST line of the file",
    ):
        assert obligation in text, obligation
    # Task 3143: "mentioning one in ordinary prose is fine" and the list of
    # places (`<li>`, `Risk:`, "(HIGH)", ...) were replaced by one rule that
    # covers every place: the whole review, code and HTML included.
    assert "mentioning one in ordinary prose is fine" not in text
    rule = _prompt_format_rule()
    for obligation in ("In any other prose", "lower case",
                       "Do not put severity words inside code blocks",
                       "anywhere in the review that is not a counted finding "
                       "BLOCKS the merge"):
        assert obligation in rule, obligation


def test_prompt_hash_in_skill_manifest_matches_the_file():
    manifest = json.loads((REPO_ROOT / "skill_manifest.json").read_text())
    digest = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    entries = manifest.get("files", manifest)
    assert entries["prompts/security-reviewer.md"] == digest


# --- timing: every new rewrite and rule stays linear ----------------------------------

REVIEW_BYTES = 200 * 1024


def _padded_lines(line: str) -> list[str]:
    return [line] * (REVIEW_BYTES // (len(line) + 1) + 1)


ADVERSARIAL_BODIES = {
    "comment-openers": ["<!--" * (REVIEW_BYTES // 4)],
    "comment-pairs": ["HI<!-- -->" * (REVIEW_BYTES // 10)],
    "multiline-comments": _padded_lines("<!--") + ["-->"],
    "comment-with-severity": ["<!--"] + _padded_lines("HIGH: x") + ["-->"],
    "unterminated-comment": ["<!-- HIGH"] + _padded_lines("x " * 20),
    "entity-flood": ["&#72;" * (REVIEW_BYTES // 5)],
    "entity-almost": ["&#" * (REVIEW_BYTES // 2)],
    "named-entity-flood": ["&amp;" * (REVIEW_BYTES // 5)],
    "li-flood": ["<li>" * (REVIEW_BYTES // 4)],
    "br-flood": ["x<br>" * (REVIEW_BYTES // 5)],
    "confusable-flood": [ETA * (REVIEW_BYTES // 2)],
    "blank-runs": ["Severity:" + " \t" * (REVIEW_BYTES // 2) + "x"],
    "severity-is-runs": ["severity " + "is " * (REVIEW_BYTES // 3)],
    "priority-lines": _padded_lines("Priority:" + " " * 7 + "*" * 3 + " maybe"),
    "range-cells": ["| " + "HIGH/MEDIUMx | " * (REVIEW_BYTES // 15)],
    "bracket-runs": ["[HIGH]" * (REVIEW_BYTES // 6)],
    "id-tag-runs": _padded_lines("[S2] HIGH1 [S2] HIGH1 [S2] HIGH1"),
    "double-dash-runs": ["- x" + " --" * (REVIEW_BYTES // 3)],
    "title-case-lines": _padded_lines("High High High High High"),
    "rated-runs": ["rated " * (REVIEW_BYTES // 6)],
    "sentence-alias-runs": [". Risk:" * (REVIEW_BYTES // 7)],
    "dash-severity-runs": [" \N{EM DASH} HIGH" * (REVIEW_BYTES // 7)],
    "intraword-star-runs": ["a*" * (REVIEW_BYTES // 2)],
    # The 3122 bodies, which must now also stay under 1 s.
    "trailing-separators": ["- x" + " - a" * (REVIEW_BYTES // 4)],
    "space-runs": ["- a" + " " * REVIEW_BYTES + "- HIGH x"],
    "severity-paren-runs": ["severity (" * (REVIEW_BYTES // 10)],
    "html-open-brackets": ["<" * REVIEW_BYTES],
    "html-long-tags": _padded_lines("<summary " + "a" * 190 + ">HIGH"),
    "table-lines": _padded_lines("| S1 | Risk:" + " " * 8 + "x | HIGHx |"),
    "blockquote-runs": _padded_lines("> > > > > - x (HIGHx"),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_BODIES))
def test_200kb_adversarial_review_parses_under_one_second(name):
    text = review("No findings.", ADVERSARIAL_BODIES[name], ZERO,
                  low_heading=False)
    assert len(text.encode()) >= REVIEW_BYTES
    started = time.perf_counter()
    analyze(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s"
