#!/usr/bin/env python3
"""Task 3122: review-gate severity shapes left open by the 3117 review.

Each shape below merged a finding behind an all-zero ``## Counts`` footer.
It must now fail closed (count-mismatch). Next to each group sits benign
prose of the same look, which must still merge. Lower-case "low" / "info"
never count (PR #40), the footer is still cross-checked, an incomplete
review still blocks, and a 200 KB adversarial review parses in under 2 s.

Copyright 2026 Forgeborn
"""

import time
from pathlib import Path

import pytest

from equipa import loops
from equipa.security_gate import review_complete_line, reviewer_nonce_line

NONCE = "fedcba9876543210fedcba9876543210"
ZERO = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0"
ONE_LOW = "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0"
ONE_HIGH = "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0"


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


def assert_blocks_behind_zero_footer(body: list[str]) -> None:
    analysis = analyze(review("No findings.", body, ZERO, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH, (
        body, analysis.detail)


def assert_prose_merges(body: list[str]) -> None:
    analysis = analyze(review("1 finding.", body, ONE_LOW, low_heading=True))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (body, analysis.detail)
    assert analysis.counts == {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0,
                               "LOW": 1, "INFO": 0}


# --- 1. trailing-severity list items -----------------------------------------

@pytest.mark.parametrize("line", [
    "- SQL injection in login handler (HIGH)",
    "- Missing CSRF token - Severity: Medium",
    "- SQL injection in the search endpoint - HIGH",
    "- SQL injection in the search endpoint: HIGH",
    "- **Token leak in request logs** — CRITICAL",
    "2. Open redirect after login, MEDIUM.",
    "> - Open redirect in /login (HIGH)",
])
def test_trailing_severity_list_item_fails_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", line])


@pytest.mark.parametrize("line", [
    "- Confidence in the fix: high",
    "- Performance impact of the change - Low",
    "- Overall risk: LOW",
    "- Test coverage for the parser is high.",
])
def test_trailing_prose_list_item_merges(line):
    assert_prose_merges(["## Notes", line])


# --- 2. Severity field in the middle of a line --------------------------------

@pytest.mark.parametrize("line", [
    "Finding 3: open redirect, severity HIGH, in auth.py",
    "- SQL injection in search. Severity: HIGH.",
    "The token check is missing (**Severity:** CRITICAL) in api.py.",
    "Open redirect in /next; CVSS severity: High.",
    "Stored XSS in comments, Severity (CVSS 3.1): HIGH",
])
def test_mid_line_severity_field_fails_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", line])


@pytest.mark.parametrize("line", [
    "Findings are ordered by severity, highest first.",
    "No finding reached severity HIGH or above.",
    "The severity of E1 is low because only admins reach it.",
    "Severity ratings follow the CVSS 3.1 scale.",
    "- Log severity: info for auth events.",
])
def test_mid_line_severity_prose_merges(line):
    assert_prose_merges(["## Notes", line])


# --- 3. bare "HIGH:" / "CRITICAL:" lines ----------------------------------------

@pytest.mark.parametrize("line", [
    "HIGH: SQL injection in the search endpoint",
    "CRITICAL: remote code execution in upload",
    "> HIGH: session fixation after login",
    "S2 (HIGH): stored XSS in comments",
    "S1: HIGH",
])
def test_bare_leading_severity_line_fails_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line, ""])


@pytest.mark.parametrize("line", [
    "High-level design is unchanged.",
    "Info: semgrep 1.176.0 found nothing new.",
    "Low-risk refactor of the cache layer.",
    "HIGH: 0 and CRITICAL: 0 from semgrep.",
])
def test_bare_leading_prose_merges(line):
    assert_prose_merges(["## Notes", "", line, ""])


# --- 4. HTML ------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    ["<details><summary><b>HIGH</b> SQL injection in search</summary>", "",
     "Details.", "</details>"],
    ["<details>", "<summary><strong>CRITICAL</strong> RCE in upload</summary>",
     "</details>"],
    ["<p><b>Severity:</b> High</p>"],
    ["Stored XSS in comments.", "", "<b>Severity:</b> High"],
    ["<details>", "<summary>HIGH: stored XSS</summary>", "</details>"],
    ["<ul><li>CRITICAL: RCE in upload</li></ul>"],
    ["<table><tr><td>S1</td><td>HIGH</td><td>SQLi</td></tr></table>"],
])
def test_html_finding_fails_closed(body):
    assert_blocks_behind_zero_footer(["## Findings", ""] + body)


@pytest.mark.parametrize("body", [
    ["<p>No <b>HIGH</b> or <b>CRITICAL</b> issues were found.</p>"],
    ["<details><summary>Scan output</summary>", "semgrep: clean", "</details>"],
    ["<p>Risk is low; more info below.</p>"],
    ["<b>Note:</b> high test coverage on the parser."],
])
def test_html_prose_merges(body):
    assert_prose_merges(["## Notes", ""] + body)


# --- 5. Sev: / Risk: / Impact: aliases -----------------------------------------

@pytest.mark.parametrize("line", [
    "Risk: High",
    "- **Risk:** HIGH",
    "Impact: CRITICAL",
    "- Sev: HIGH",
    "**Impact**: High - account takeover",
    "- Risk level: MEDIUM",
])
def test_severity_alias_field_fails_closed(line):
    assert_blocks_behind_zero_footer(["## Findings", "", line])


@pytest.mark.parametrize("line", [
    "Overall risk: LOW",
    "Risk: low, the endpoint is internal.",
    "- **Impact:** an attacker could read other tenants' rows.",
    "- **Impact:** High-value sessions could be hijacked.",
    "Risk-free change to the logging format.",
])
def test_severity_alias_prose_merges(line):
    assert_prose_merges(["## Notes", "", line])


# --- 6. table cells -------------------------------------------------------------

@pytest.mark.parametrize("cell", [
    "High risk",
    "Critical impact",
    "**High**",
    "HIGH: SQL injection in search",
    "Risk: HIGH",
    "Sev: High",
    "**Severity:** HIGH",
    "Severity - HIGH",
])
def test_table_cell_finding_fails_closed(cell):
    assert_blocks_behind_zero_footer([
        "## Findings", "", "| ID | Issue | Rating |", "|---|---|---|",
        f"| S1 | SQL injection | {cell} |",
    ])


@pytest.mark.parametrize("row", [
    "| cache refactor | Low risk |",
    "| cache refactor | High memory use under load |",
    "| Overall risk | LOW |",
    "| semgrep | HIGH: 0 |",
    "| docs | more info in the README |",
])
def test_table_prose_cell_merges(row):
    assert_prose_merges(["## Notes", "", "| Item | Note |", "|---|---|", row])


# --- 7. lower-case prose, footer cross-check, incomplete, timing -------------------

@pytest.mark.parametrize("line", [
    "- more info: see the scan log",
    "- risk: low",
    "Impact: low, only admins can reach it.",
    "info: the scan used the default rules",
    "- Old logging is low - priority cleanup, info only",
])
def test_lower_case_low_and_info_never_count(line):
    analysis = analyze(review("No findings.", ["## Notes", "", line], ZERO,
                             low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (line, analysis.detail)


COUNTED_SHAPES = [
    "- SQL injection in the search endpoint - HIGH",
    "Finding 3: open redirect, severity HIGH, in auth.py",
    "HIGH: SQL injection in the search endpoint",
    "<details><summary><b>HIGH</b> SQL injection</summary></details>",
    "Risk: High",
    "| S1 | SQL injection | High risk |",
]


@pytest.mark.parametrize("line", COUNTED_SHAPES)
def test_shape_counted_by_the_footer_merges_with_that_count(line):
    """A candidate only fails closed when the footer does not count it."""
    analysis = analyze(review("1 finding.", ["## Findings", "", line],
                             ONE_HIGH, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_OK, (line, analysis.detail)
    assert analysis.counts["HIGH"] == 1


@pytest.mark.parametrize("line", COUNTED_SHAPES)
def test_count_findings_returns_none_behind_zero_footer(line, monkeypatch):
    """The gate entry point treats the review as missing (fail closed)."""
    audit_lines = []
    monkeypatch.setattr(loops, "_gate_audit_log",
                        lambda message, **_kw: audit_lines.append(message))
    text = review("No findings.", ["## Findings", "", line], ZERO,
                  low_heading=False)
    counts = loops._count_findings_in_review_file(
        Path("SECURITY-REVIEW-1.md"), task_id=1, text=text)
    assert counts is None
    assert any("event=count-mismatch" in entry for entry in audit_lines)


@pytest.mark.parametrize("line", COUNTED_SHAPES)
def test_shape_after_the_footer_is_an_incomplete_review(line):
    analysis = analyze(review("1 finding.", ["## Findings", "", line],
                             ONE_HIGH, low_heading=False,
                             after_footer=["", line]))
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE, (
        line, analysis.detail)


def test_unfinished_summary_still_blocks_with_counted_shape():
    analysis = analyze(review(
        "IN PROGRESS - initial skeleton",
        ["## Findings", "", "- SQL injection in the search endpoint - HIGH"],
        ONE_HIGH, low_heading=False))
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE


REVIEW_BYTES = 200 * 1024


def _padded_lines(line: str) -> list[str]:
    return [line] * (REVIEW_BYTES // (len(line) + 1) + 1)


ADVERSARIAL_BODIES = {
    "trailing-separators": ["- x" + " - a" * (REVIEW_BYTES // 4)],
    "trailing-no-severity": _padded_lines("- a, b: c - d — e – f, HIGHx"),
    "space-runs": ["- a" + " " * REVIEW_BYTES + "- HIGH x"],
    "severity-words": ["severity " * (REVIEW_BYTES // 9)],
    "severity-colon-runs": ["Severity" + ":" * REVIEW_BYTES],
    "severity-paren-runs": ["severity (" * (REVIEW_BYTES // 10)],
    "alias-lines": _padded_lines("Risk:" + " " * 7 + "*" * 3 + " maybe"),
    "id-runs": ["S1 " * (REVIEW_BYTES // 3)],
    "bare-no-separator": _padded_lines("HIGH HIGH HIGH HIGH HIGH"),
    "html-tags": ["<b>" * (REVIEW_BYTES // 3)],
    "html-open-brackets": ["<" * REVIEW_BYTES],
    "html-long-tags": _padded_lines("<summary " + "a" * 190 + ">HIGH"),
    "table-cells": ["| " + "High riskx | " * (REVIEW_BYTES // 13)],
    "table-lines": _padded_lines("| S1 | Risk:" + " " * 8 + "x | HIGHx |"),
    "paren-list": ["- " + "(" * REVIEW_BYTES + "HIGH"],
    "blockquote-runs": _padded_lines("> > > > > - x (HIGHx"),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_BODIES))
def test_200kb_adversarial_review_parses_under_two_seconds(name):
    text = review("No findings.", ADVERSARIAL_BODIES[name], ZERO,
                  low_heading=False)
    assert len(text.encode()) >= REVIEW_BYTES
    started = time.perf_counter()
    analyze(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{name}: {elapsed:.2f}s"
