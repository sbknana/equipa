"""Tests for the unified _extract_findings parser in equipa.loops.

Covers TheForge task #2135 — D4 SUGGESTION: Unify _extract_security_findings
and _extract_code_review_findings. The two prior 40-line near-duplicates
collapsed into one parametrized extractor backed by severity tables in
equipa.constants. These tests guard:

  - Security path: case-insensitive matching of CRITICAL/HIGH labels
  - Code review path: case-sensitive matching of Critical/Important labels
  - Bullet-prefix stripping
  - Short-line merge with the next line
  - 500-char description cap
  - Empty input
  - Negative case (prose mentioning the word should NOT match without a
    suffix pattern like ``:``, ``**``, ``[]``, ``-``, or ``—``)
"""

from __future__ import annotations

import pytest

from equipa.constants import (
    CODE_REVIEW_SEVERITY_PATTERNS,
    SECURITY_SEVERITY_PATTERNS,
)
from equipa.loops import (
    _extract_code_review_findings,
    _extract_findings,
    _extract_security_findings,
)


# ---------- Security extractor (CRITICAL/HIGH, case-insensitive) ----------

def test_security_extracts_critical_label():
    text = "## CRITICAL: SQL injection in login handler"
    findings = _extract_security_findings(text)
    assert findings == [("CRITICAL", "## CRITICAL: SQL injection in login handler")]


def test_security_extracts_high_label():
    text = "- HIGH: missing CSRF protection on /api/transfer"
    findings = _extract_security_findings(text)
    assert findings == [("HIGH", "HIGH: missing CSRF protection on /api/transfer")]


def test_security_is_case_insensitive():
    text = "* critical: hardcoded secret"
    findings = _extract_security_findings(text)
    assert findings == [("CRITICAL", "critical: hardcoded secret")]


def test_security_matches_bracketed_label():
    text = "[CRITICAL] arbitrary file write via path traversal"
    findings = _extract_security_findings(text)
    assert findings[0][0] == "CRITICAL"


def test_security_ignores_prose_mention():
    # "critical" appears but with no suffix pattern — must not match.
    text = "This finding is critical to address before launch."
    assert _extract_security_findings(text) == []


def test_security_short_header_merges_with_next_line():
    text = "CRITICAL:\nDetailed description on the next line of the report."
    findings = _extract_security_findings(text)
    assert len(findings) == 1
    assert "Detailed description on the next line" in findings[0][1]


def test_security_caps_long_descriptions_at_500():
    long_desc = "CRITICAL: " + ("x" * 1000)
    findings = _extract_security_findings(long_desc)
    assert len(findings) == 1
    assert len(findings[0][1]) == 500
    assert findings[0][1].endswith("...")


def test_security_empty_input_returns_empty_list():
    assert _extract_security_findings("") == []
    assert _extract_security_findings(None) == []  # type: ignore[arg-type]


# ---------- Code review extractor (Critical/Important, case-sensitive) ----------

def test_code_review_extracts_critical_label():
    text = "**Critical**: race condition in payment processor"
    findings = _extract_code_review_findings(text)
    assert findings[0][0] == "Critical"


def test_code_review_extracts_important_label():
    text = "- Important: function exceeds 200 lines"
    findings = _extract_code_review_findings(text)
    assert findings == [("Important", "Important: function exceeds 200 lines")]


def test_code_review_is_case_sensitive_skips_lowercase():
    # Lowercase "important" must NOT match — guards against "critically
    # important" prose. Original behavior preserved by case_sensitive=True.
    text = "This is critically important to fix."
    assert _extract_code_review_findings(text) == []


def test_code_review_first_label_wins_in_priority_order():
    # Dict insertion order (Critical before Important) determines priority
    # when both labels appear on the same line — matches the if/elif of
    # the original code.
    text = "Critical: also Important: combined finding"
    findings = _extract_code_review_findings(text)
    assert findings[0][0] == "Critical"


def test_code_review_strips_bullet_prefixes():
    for prefix in ("- ", "* ", "• "):
        text = f"{prefix}Critical: nested issue"
        findings = _extract_code_review_findings(text)
        assert findings[0][1] == "Critical: nested issue"


# ---------- Direct _extract_findings parametrization ----------

def test_extract_findings_with_custom_patterns():
    # Header is >=40 chars so the short-line merge does not pull in line 2.
    custom = {"BLOCKER": ("BLOCKER:", "[BLOCKER]")}
    text = (
        "BLOCKER: build is broken on the main branch after merge\n"
        "notice: unrelated"
    )
    findings = _extract_findings(text, custom, case_sensitive=False)
    assert findings == [
        ("BLOCKER", "BLOCKER: build is broken on the main branch after merge"),
    ]


def test_constants_severity_tables_are_dicts_of_tuples():
    for table in (SECURITY_SEVERITY_PATTERNS, CODE_REVIEW_SEVERITY_PATTERNS):
        assert isinstance(table, dict)
        for label, patterns in table.items():
            assert isinstance(label, str)
            assert isinstance(patterns, tuple)
            assert all(isinstance(p, str) for p in patterns)


@pytest.mark.parametrize(
    "text,expected_count",
    [
        ("", 0),
        ("no findings here", 0),
        ("CRITICAL: one\nHIGH: two\nLOW: three\n", 2),  # LOW not in patterns
    ],
)
def test_extract_findings_counts(text, expected_count):
    findings = _extract_findings(
        text, SECURITY_SEVERITY_PATTERNS, case_sensitive=False,
    )
    assert len(findings) == expected_count
