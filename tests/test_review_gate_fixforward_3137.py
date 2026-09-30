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
