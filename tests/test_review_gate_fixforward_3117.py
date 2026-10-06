#!/usr/bin/env python3
"""Fix-forward of task 3117 after its independent review.

1. Three finding SHAPES the listed candidate rules missed must fail closed
   behind a zero footer: a list item that starts with a severity, a
   Severity field in a nested list item, a table cell with a Severity field.
2. Prose mentions of a severity stay allowed (tasks 2315 / 3038 design).
3. Only status-position "Draft / WIP / Preliminary / Initial scan" blocks;
   ordinary prose that starts with those words does not.

Copyright 2026 Forgeborn
"""

import re
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line
from tests.review_gate_production import (
    AS_WRITTEN_ONLY_FINDINGS,
    blocked_by_the_gate,
)

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{loops.backstop_reason(severity)} at line "
    for severity in loops.MERGE_BLOCKING_SEVERITIES
)

NONCE = "0123456789abcdef0123456789abcdef"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"


def review(summary: str, body: list[str], footer: str, low_heading: bool) -> str:
    lines = [reviewer_nonce_line(NONCE), "# Security Review", "",
             "## Summary", summary, ""]
    if low_heading:
        lines += ["### [E1] LOW - verbose error message", "Details.", ""]
    lines += body + ["", "## Files Reviewed", "- app.py", "- tests/test_app.py",
                     "", "## Methodology", "Read the diff, ran semgrep.", "",
                     "## Counts", footer, review_complete_line(NONCE)]
    return "\n".join(lines) + "\n"


def verdict(text: str) -> str:
    return loops._analyze_review_file(Path("SECURITY-REVIEW-1.md"), text=text).verdict


def blocked_verdict(text: str) -> str:
    """The parser's verdict on a review the merge gate blocks (task 3170,
    IR67-02: every must-block case decides through the gate)."""
    return blocked_by_the_gate(text).verdict


# Task 3143: a standalone UPPER-case severity word, as the backstop reads it.
SEVERITY_TOKEN = re.compile(r"(?<![^\W_])(CRITICAL|HIGH|MEDIUM)(?![^\W_])")
# Task 3152: "E1 was rated LOW rather than HIGH because ...", "No HIGH or
# CRITICAL issues were found." and "semgrep: 0 CRITICAL/HIGH results ..."
# merged as written under the 3143 comparison, negation and tally
# exemptions. CRITICAL and HIGH have no exemption now: they block as written.


def lowercase_severity_words(text: str) -> str:
    return SEVERITY_TOKEN.sub(lambda match: match.group(1).lower(), text)


def assert_only_the_backstop_blocks(text: str) -> None:
    """The merge gate blocks ``text`` (task 3170, IR67-02): the rules
    trusted it (the backstop runs only then) and the severity-token backstop
    blocked it."""
    analysis = blocked_by_the_gate(text)
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), (
        analysis.detail)


@pytest.mark.parametrize("finding", sorted(AS_WRITTEN_ONLY_FINDINGS))
def test_the_must_block_helpers_read_the_review_as_written(finding):
    """Task 3170 (IR67-02): a severity only the review as written shows
    blocks through this suite's helper, so a gate that parsed the normalised
    text (R3161-01) fails this suite too."""
    assert_only_the_backstop_blocks(review(
        "No findings.", ["## Notes", AS_WRITTEN_ONLY_FINDINGS[finding]], ZERO,
        low_heading=False))


# --- 1. finding shapes that used to merge behind a zero footer ---------------

@pytest.mark.parametrize("body", [
    ["## Notes", "- HIGH: SQL injection in the search endpoint"],
    ["## Notes", "1. **Critical** - remote code execution in upload"],
    ["## Findings", "- S1 token leak", "    - **Severity:** HIGH"],
    ["## Findings", "- S1 token leak", "        * Severity: CRITICAL"],
    ["| ID | Detail | Where |", "|---|---|---|", "| S1 | Severity: HIGH | app.py:12 |"],
])
def test_finding_shapes_behind_zero_footer_fail_closed(body):
    text = review("No findings.", body, ZERO, low_heading=False)
    assert blocked_verdict(text) == loops.REVIEW_VERDICT_COUNT_MISMATCH


def test_leading_high_next_to_a_counted_low_fails_closed():
    text = review("1 finding.", ["## Notes", "- HIGH: auth bypass on /admin"],
                  ONE_LOW, low_heading=True)
    assert blocked_verdict(text) == loops.REVIEW_VERDICT_COUNT_MISMATCH


# --- 2. prose mentions and near-misses still merge ----------------------------

@pytest.mark.parametrize("line", [
    "Rated S1 MEDIUM, not HIGH: it needs a local foothold.",
    "E1 was rated LOW rather than HIGH because the path is admin-only.",
    "No HIGH or CRITICAL issues were found.",
    "semgrep: 0 CRITICAL/HIGH results across the diff.",
    "- High confidence in the fix; the caller validates input.",
    "- Low-level parsing is unchanged.",
    "    indented prose that mentions HIGH in passing",
])
def test_prose_mentions_still_merge(line):
    """Task 3143: the reviewer prompt allows UPPER-case CRITICAL, HIGH and
    MEDIUM only as a finding's label, so compliant prose is written in lower
    case and merges. The UPPER-case original is still read as prose by every
    rule: only the severity-token backstop blocks it."""
    text = review("1 finding.", ["## Notes", lowercase_severity_words(line)],
                  ONE_LOW, low_heading=True)
    assert verdict(text) == loops.REVIEW_VERDICT_OK, line
    written = review("1 finding.", ["## Notes", line], ONE_LOW, low_heading=True)
    if SEVERITY_TOKEN.search(line):
        assert_only_the_backstop_blocks(written)


# --- 3. unfinished-summary markers only in status position -------------------

@pytest.mark.parametrize("summary", [
    "Initial review of the 7 changed files found one LOW issue.",
    "WIP branch contains a refactor of the cache layer; 1 finding.",
    "Preliminary checks passed, then a full pass found 1 finding.",
    "The PR is awaiting review by the owner; 1 finding.",
    "Drafted fixes are out of scope; 1 finding.",
])
def test_honest_summaries_are_not_unfinished(summary):
    text = review(summary, [], ONE_LOW, low_heading=True)
    assert verdict(text) == loops.REVIEW_VERDICT_OK, summary


@pytest.mark.parametrize("summary", [
    "Draft",
    "WIP: still reading dispatch.py",
    "Preliminary - more to come",
    "Initial automated scan only; manual review pending",
    "IN PROGRESS - initial skeleton",
    "**Draft**",
    "Initial scan only",
])
def test_status_markers_still_block(summary):
    text = review(summary, [], ONE_LOW, low_heading=True)
    assert blocked_verdict(text) == loops.REVIEW_VERDICT_INCOMPLETE, summary



# --- re-review follow-up: shapes the first fix-forward still missed ---------

@pytest.mark.parametrize("body", [
    ["| ID | Detail | Where |", "|---|---|---|", "| S1 | SQLi | **Severity:** HIGH |"],
    ["| S1 | SQLi | **Severity**: HIGH |"],
    ["## Findings", "- S1 HIGH: SQL injection"],
    ["## Findings", "- S1: HIGH - SQL injection"],
    ["## Findings", "- (S1) HIGH: SQL injection"],
    ["## Findings", "- \U0001f534 HIGH: SQL injection"],
    ["## Findings", "- \u26a0\ufe0f HIGH: SQL injection"],
    ["## Findings", "- [x] HIGH: SQL injection"],
    ["## Findings", "- [ ] HIGH - SQL injection"],
    ["## Findings", "> - HIGH: SQL injection"],
])
def test_rereview_shapes_fail_closed(body):
    text = review("No findings.", body, ZERO, low_heading=False)
    assert blocked_verdict(text) == loops.REVIEW_VERDICT_COUNT_MISMATCH, body


@pytest.mark.parametrize("line", [
    "- Info: semgrep 1.176.0",
    "- Low: coverage of tests/ is thin",
    "1. Info: consider pinning deps",
])
def test_note_bullets_do_not_block(line):
    text = review("No findings.", ["## Notes", line], ZERO, low_heading=False)
    assert verdict(text) == loops.REVIEW_VERDICT_OK, line


@pytest.mark.parametrize("summary", [
    "Draft.",
    "WIP.",
    "Pending review.",
    "Awaiting review.",
    "Status: Draft",
    "**Status:** WIP",
    "Draft review; 1 finding.",
    "Preliminary findings: 1 LOW.",
])
def test_punctuated_status_markers_block(summary):
    text = review(summary, [], ONE_LOW, low_heading=True)
    assert blocked_verdict(text) == loops.REVIEW_VERDICT_INCOMPLETE, summary
