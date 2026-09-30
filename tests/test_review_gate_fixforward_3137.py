#!/usr/bin/env python3
"""Task 3137: fix-forward of the task 3130 review-gate parser.

The independent review of 3130 and EQUIPA's SECURITY-REVIEW-3130 found:

* M1 (SR3130-01): an HTML list item that opens with its own marker
  ("<ol><li>1. HIGH: SQLi</li></ol>") blocked before 3130 and merged after it.
* N1: EQUIPA's own marker comment splitting a word ("HI<!-- EQUIPA-X -->GH")
  made the parser skip the rendered view, so the split word merged.
* F4 leftovers: "Open redirect, severity HIGH in auth.py" and "HIGH SQL
  injection in login handler" merged.
* N2: seven Markdown shapes a CommonMark renderer shows as a finding, but the
  parser hid as code or could not reach.
* N3: a 200 KB review made only of line breaks took about 1 s to parse.

Each shape must now fail closed (count-mismatch) behind an all-zero footer.
Benign prose of the same look must still merge.

Copyright 2026 Forgeborn
"""

import time
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line

NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_LOW_COUNTS = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 1, "INFO": 0}


def review(summary: str, body: list[str], footer: str, low_heading: bool) -> str:
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if low_heading:
        lines += ["### [E1] LOW - verbose error message", "Details.", ""]
    lines += body + ["", "## Files Reviewed", "- app.py", "- tests/test_app.py",
                     "", "## Methodology", "Read the diff, ran semgrep.", "",
                     "## Counts", footer, review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def analyze(text: str) -> loops.ReviewCountAnalysis:
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text)


def assert_blocks_behind_zero_footer(body: list[str],
                                     severity: str = "HIGH") -> None:
    analysis = analyze(review("No findings.", ["## Findings", ""] + body, ZERO,
                              low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        body, analysis.detail)
    # Either a candidate rule saw the severity, or a strict finding heading
    # counted it and the zero footer disagrees.
    assert (f"{severity}=" in analysis.detail
            or analysis.header_counts[severity] > 0), (body, analysis.detail)


def assert_prose_merges(body: list[str]) -> None:
    analysis = analyze(review("1 finding.", ["## Notes", ""] + body, ONE_LOW,
                              low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (body, analysis.detail)
    assert analysis.counts == ONE_LOW_COUNTS


# --- M1 (SR3130-01): list items that open with their own marker ---------------

@pytest.mark.parametrize("line, severity", [
    # The eight shapes of SECURITY-REVIEW-3130, which blocked before task 3130.
    ("<ol><li>1. HIGH: SQLi in login</li></ol>", "HIGH"),
    ("<ol><li>1) HIGH: SQLi in login</li></ol>", "HIGH"),
    ("<ol><li>2. **HIGH** \N{EM DASH} SQLi</li></ol>", "HIGH"),
    ("<ol><li>10. CRITICAL - RCE in upload</li></ol>", "CRITICAL"),
    ("<ol><li>1. \N{LARGE RED CIRCLE} HIGH: SQLi</li></ol>", "HIGH"),
    ("<ol><li>1. <b>HIGH</b>: SQLi</li></ol>", "HIGH"),
    ("<ul><li>- - HIGH: SQLi</li></ul>", "HIGH"),
    ("<ul><li>> 1. HIGH: SQLi</li></ul>", "HIGH"),
    # Same family: an explicit list item as Markdown inside the HTML item.
    ("<ul><li>* SQLi in login (HIGH)</li></ul>", "HIGH"),
    ("<ol><li>3. SQL injection in search - HIGH</li></ol>", "HIGH"),
])
def test_html_list_item_opening_with_its_own_marker_fails_closed(line, severity):
    assert_blocks_behind_zero_footer([line], severity)


def test_html_list_item_with_its_own_marker_is_counted_once():
    analysis = analyze(review(
        "No findings.", ["<ol><li>1. HIGH: SQLi in login</li></ol>"], ZERO,
        low_heading=False))
    assert analysis.detail.endswith("HIGH=1"), analysis.detail


@pytest.mark.parametrize("line", [
    "<ol><li>1. Test coverage for the parser is high.</li></ol>",
    "<ul><li>- - -</li></ul>",
    "<ol><li>2. High-level design reviewed</li></ol>",
    "<ol><li>1. Overall risk: LOW</li></ol>",
])
def test_html_list_item_with_its_own_marker_prose_merges(line):
    assert_prose_merges([line])


# --- F4 leftovers of the 3122 review ------------------------------------------

@pytest.mark.parametrize("line, severity", [
    ("Open redirect, severity HIGH in auth.py", "HIGH"),
    ("HIGH SQL injection in login handler", "HIGH"),
    # The same shapes in the other positions a finding is written in.
    ("- Open redirect, severity HIGH in auth.py", "HIGH"),
    ("SQL injection (severity CRITICAL) in the search endpoint", "CRITICAL"),
    ("Token leak; severity MEDIUM for the session cookie", "MEDIUM"),
    ("- HIGH SQL injection in login handler", "HIGH"),
    ("1. CRITICAL RCE in the upload handler", "CRITICAL"),
    ("> HIGH SQL injection in login handler", "HIGH"),
    ("\N{LARGE RED CIRCLE} HIGH SQL injection in login handler", "HIGH"),
    ("**HIGH** SQL injection in login handler", "HIGH"),
])
def test_f4_leftover_shapes_fail_closed(line, severity):
    assert_blocks_behind_zero_footer([line], severity)


@pytest.mark.parametrize("line", [
    "HIGH availability is out of scope for this change.",
    "CRITICAL and HIGH findings block the merge.",
    "CRITICAL OR HIGH findings block the merge.",
    "HIGH MEDIUM LOW INFO are the levels the gate reads.",
    "No issues of severity HIGH in this diff.",
    "Findings are ordered, severity HIGH or above first.",
    "Nothing here, severity HIGH and above, was found.",
    "INFO Semgrep finished with no results.",
])
def test_f4_leftover_prose_merges(line):
    assert_prose_merges([line])


# --- N1: EQUIPA's own marker comment splitting a word ---------------------------

@pytest.mark.parametrize("line", [
    # The reviewer's three shapes.
    f"HI<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->GH: SQL injection",
    "- SQL injection in login - H<!--EQUIPA-X-->IGH",
    "### [S1] H<!-- EQUIPA-REVIEW-COMPLETE -->IGH \N{EM DASH} SQL injection",
    # A marker comment that shares its line with text is not standalone.
    f"<!-- EQUIPA-REVIEWER-RUN: {NONCE} -->HIGH: SQL injection",
    f"CRI<!-- EQUIPA-REVIEW-COMPLETE {NONCE} -->TICAL: RCE in upload",
])
def test_marker_comment_inside_a_line_fails_closed(line):
    severity = "CRITICAL" if "TICAL" in line else "HIGH"
    assert_blocks_behind_zero_footer([line], severity)


def test_standalone_marker_comments_still_parse_once(monkeypatch):
    """A clean review whose only comments are the gate's own markers, each on
    its own line, is parsed once (the rendered view adds nothing)."""
    calls = []
    real = loops._analyze_review_text

    def counting(text, nonblank_lines):
        calls.append(text)
        return real(text, nonblank_lines)

    monkeypatch.setattr(loops, "_analyze_review_text", counting)
    analysis = analyze(review("1 finding.", ["## Notes", "", "Plain prose."],
                              ONE_LOW, low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts == ONE_LOW_COUNTS
    assert len(calls) == 1


# --- N3: line-break floods --------------------------------------------------------

REVIEW_BYTES = 200 * 1024


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", " ",
                                        " \n", "\t\n"])
def test_200kb_of_line_breaks_parses_in_half_a_second(line_break):
    body = [line_break * (REVIEW_BYTES // len(line_break))]
    text = review("No findings.", body, ZERO, low_heading=False)
    assert len(text.encode()) >= REVIEW_BYTES
    started = time.perf_counter()
    analysis = analyze(text)
    elapsed = time.perf_counter() - started
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert elapsed < 0.5, f"{line_break!r}: {elapsed:.2f}s"


@pytest.mark.parametrize("gap", [3, 40, 5000])
def test_folded_blank_lines_keep_findings_and_footer(gap):
    """Folding blank-line runs changes no verdict: a finding far below the
    Summary still blocks, and a footer far below its heading still counts."""
    body = ["## Findings"] + [""] * gap + ["HIGH: SQL injection in login"]
    assert_blocks_behind_zero_footer(body)
    text = review("1 finding.", ["## Notes"] + [""] * gap + ["Plain prose."],
                  ONE_LOW, low_heading=True).replace(
                      "## Counts\n", "## Counts\n" + "\n" * gap)
    analysis = analyze(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, analysis.detail
    assert analysis.counts == ONE_LOW_COUNTS
