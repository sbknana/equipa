"""Task #3038 (GATE-FOOTER-2): regressions from SECURITY-REVIEW-3033.

Every PoC case from /tmp/poc_3033_review.py is pinned here. Cases A1-G4 and
K must BLOCK the merge (None = treated as missing, or CRITICAL/HIGH > 0);
J1 is a finished clean review that must NOT be held.

    S3033-01  resolved-looking live headings (A1/A2/B1/B2)
    S3033-02  finding headings the level-3 regex cannot see (G1-G4)
    S3033-03  unfilled reviewer template (E)
    S3033-04  quoted all-zero footer before the real one (F)
    S3033-05  Summary merely mentions TODO / skeleton (J1)

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa import db as equipa_db
from equipa.dispatch import _security_review_blocks_merge
from equipa.loops import (
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_INCOMPLETE,
    REVIEW_VERDICT_OK,
    _analyze_review_file,
    _count_findings_in_review_file,
)

TASK_ID = 3038

BODY = "# Security Review\n\n## Summary\nReviewed the diff.\n\n## Findings\n\n"
ZERO_FOOTER = (
    "\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
)
HIGH_FOOTER = (
    "\n## Counts\nCRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
)

# The report skeleton prompts/security-reviewer.md prescribes, unfilled.
TEMPLATE_SKELETON = """# Security Review: CryptoTrader
Date: 2026-09-27
Reviewer: SecurityReviewer Agent
Tools: grep-based fallback

## Summary
[1-2 sentences: what was reviewed, finding count, overall risk]

## Findings

### [S1] [SEVERITY] — Title
- **File:** path/to/file.ext:line
- **Impact:** What an attacker could do

## Files Reviewed
- [list]

## Quick Win Checklist
- [ ] Hardcoded secrets: [PASS/FAIL]
- [ ] SQL injection: [PASS/FAIL]

## Counts
CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0
"""

MUST_BLOCK = {
    "A1-fixed-paren-no-footer":
        BODY + "### [S1] HIGH — AES-GCM nonce (fixed at zero) allows forgery\n"
        "Detail.\n",
    "A2-colon-FIXED-no-footer":
        BODY + "### [S1] HIGH — JWT signing key: FIXED string in config.py\n"
        "Detail.\n",
    "B1-fixed-paren-stale-zero-footer":
        BODY + "### [S1] HIGH — AES-GCM nonce (fixed at zero) allows forgery\n"
        "Detail.\n" + ZERO_FOOTER,
    "B2-resolved-bracket-stale-zero-footer":
        BODY + "### [S1] CRITICAL — Webhook host [resolved via attacker DNS] "
        "trusted\nDetail.\n" + ZERO_FOOTER,
    "B3-control-stale-zero-footer":
        BODY + "### [S1] HIGH — AES-GCM nonce is constant zero, allows "
        "forgery\nDetail.\n" + ZERO_FOOTER,
    "E-unfilled-template": TEMPLATE_SKELETON,
    "F-quoted-zero-footer-then-real-footer":
        BODY + "The skeleton footer was:\n\n```\n## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n```\n\n"
        "**S1 (HIGH):** SQL injection in orders.py:40.\n" + HIGH_FOOTER,
    "G1-level-2-heading":
        BODY + "## [S1] HIGH — SQL injection in orders.py:40\nDetail.\n"
        + ZERO_FOOTER,
    "G2-level-4-heading":
        BODY + "#### [S1] HIGH — SQL injection in orders.py:40\nDetail.\n"
        + ZERO_FOOTER,
    "G3-title-case-severity":
        BODY + "### [S1] High — SQL injection in orders.py:40\nDetail.\n"
        + ZERO_FOOTER,
    "G4-bold-bullet":
        BODY + "- **[S1] HIGH** — SQL injection in orders.py:40\n"
        + ZERO_FOOTER,
    "K-3031-shape":
        BODY + "### [S3031-01] MEDIUM — x\n### [S3031-02] LOW — y\n"
        + ZERO_FOOTER,
}


def _counts(
    critical: int = 0, high: int = 0, medium: int = 0, low: int = 0,
    info: int = 0,
) -> dict[str, int]:
    return {
        "CRITICAL": critical, "HIGH": high, "MEDIUM": medium, "LOW": low,
        "INFO": info,
    }


def _write(project_dir: Path, body: str) -> Path:
    path = project_dir / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def persisted_audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture gate-audit persistence instead of writing TheForge rows."""
    events: list[dict] = []

    def _record(message, task_id, *, event=None, counts=None):
        events.append({"event": event, "counts": counts})

    monkeypatch.setattr(equipa_db, "log_gate_audit", _record)
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")
    return events


# ---------- PoC cases: every one blocks the merge ----------


@pytest.mark.parametrize("body", MUST_BLOCK.values(), ids=MUST_BLOCK.keys())
def test_poc_case_blocks_merge(tmp_path: Path, body: str) -> None:
    _write(tmp_path, body)

    blocked, _ = _security_review_blocks_merge(str(tmp_path), TASK_ID)

    assert blocked is True


@pytest.mark.parametrize(
    "case", ["A1-fixed-paren-no-footer", "A2-colon-FIXED-no-footer"],
)
def test_no_footer_never_subtracts_a_resolved_looking_heading(
    tmp_path: Path, case: str,
) -> None:
    path = _write(tmp_path, MUST_BLOCK[case])

    assert _count_findings_in_review_file(path) == _counts(high=1)


@pytest.mark.parametrize(
    "case",
    ["B1-fixed-paren-stale-zero-footer", "B2-resolved-bracket-stale-zero-footer"],
)
def test_stale_footer_over_resolved_looking_live_heading_is_untrusted(
    tmp_path: Path, case: str,
) -> None:
    path = _write(tmp_path, MUST_BLOCK[case])

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


@pytest.mark.parametrize(
    "case", ["G1-level-2-heading", "G2-level-4-heading",
             "G3-title-case-severity", "G4-bold-bullet"],
)
def test_finding_the_strict_regex_cannot_see_is_a_count_mismatch(
    tmp_path: Path, case: str,
) -> None:
    path = _write(tmp_path, MUST_BLOCK[case])

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "HIGH=1" in analysis.detail


def test_unfilled_template_is_incomplete(tmp_path: Path) -> None:
    path = _write(tmp_path, TEMPLATE_SKELETON)

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "placeholder" in analysis.detail


def test_last_footer_wins_over_quoted_zero_footer(tmp_path: Path) -> None:
    path = _write(tmp_path, MUST_BLOCK["F-quoted-zero-footer-then-real-footer"])

    assert _count_findings_in_review_file(path) == _counts(high=1)


def test_last_unfenced_footer_wins(tmp_path: Path) -> None:
    # An early all-zero footer outside any code fence (e.g. the review's own
    # skeleton quoted as a section) must not mask the final footer.
    path = _write(
        tmp_path,
        "# Security Review\n\n## Summary\nOne HIGH.\n" + ZERO_FOOTER
        + "\n## Findings\n\n### [S1] HIGH — SQL injection\nDetail.\n"
        + HIGH_FOOTER,
    )

    assert _count_findings_in_review_file(path) == _counts(high=1)


def test_footer_followed_by_a_finding_is_incomplete(tmp_path: Path) -> None:
    # The footer was written first and more sections appended after it;
    # the tallies agree, so only the footer-position rule can catch it.
    path = _write(
        tmp_path,
        BODY
        + "\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 0 | INFO: 0\n"
        + "\n### [S1] MEDIUM — found later\nDetail.\n",
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "final section" in analysis.detail


def test_trailing_prose_after_footer_is_allowed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "No findings.\n" + ZERO_FOOTER + "\nReviewed by agent.\n",
    )

    assert _count_findings_in_review_file(path) == _counts()


# ---------- No false blocks on finished reviews ----------


def test_summary_mentioning_todo_endpoint_is_not_held(tmp_path: Path) -> None:
    # PoC J1 (S3033-05): the marker is not in status position.
    path = _write(
        tmp_path,
        "# Security Review\n\n## Summary\nReviewed the TODO-list API; no "
        "issues found.\n\n## Findings\nNone.\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (
        False, _counts(),
    )


def test_template_quoted_in_code_is_not_a_placeholder(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "The prompt stub `[SEVERITY]` is replaced; the old skeleton:\n\n"
        "```markdown\n### [S1] [SEVERITY] — Title\n- [ ] XSS: [PASS/FAIL]\n"
        "```\n\n### [S1] LOW — verbose errors\nDetail.\n"
        "\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n",
    )

    assert _count_findings_in_review_file(path) == _counts(low=1)


def test_uppercase_resolved_recap_bullet_is_not_a_live_candidate(
    tmp_path: Path,
) -> None:
    # GutenForge #2910 / StockForge #2638 fix-verification recap shape.
    path = _write(
        tmp_path,
        BODY + "Prior findings:\n"
        "- **F1 (HIGH, renderer SSRF): FIXED.** Now uses safeFetch.\n\n"
        "### [S2] LOW — verbose errors\nDetail.\n"
        "\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n",
    )

    # Task #3038 fix-forward (IR38-01): the recap is resolved, so the footer
    # may omit it, but like a resolved level-3 heading it is ADDED to the
    # merge counts, never dropped. HIGH=1 blocks the merge.
    assert _count_findings_in_review_file(path) == _counts(high=1, low=1)


def test_lowercase_fixed_wording_in_bullet_stays_live(tmp_path: Path) -> None:
    # Only an UPPERCASE status exempts a recap bullet (S3033-01 in bold form).
    path = _write(
        tmp_path,
        BODY + "- **[S1] HIGH — AES-GCM nonce (fixed at zero)** allows "
        "forgery\n" + ZERO_FOOTER,
    )

    # Task #3038 fix-forward (IR38-01): the bold span ends in "(fixed at
    # zero)", which the strict level-3 grammar reads as a resolved status,
    # and a resolved candidate is COUNTED, never dropped: HIGH=1 blocks.
    counts = _count_findings_in_review_file(path)
    assert counts is None or counts["HIGH"] >= 1
    assert counts == _counts(high=1)


def test_hyphenated_severity_word_is_not_a_candidate(tmp_path: Path) -> None:
    # StockForge #2678: "gate-critical" in a heading is not a CRITICAL.
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n## Verified controls (gate-critical items)\n"
        "- all intact\n" + ZERO_FOOTER,
    )

    assert _count_findings_in_review_file(path) == _counts()


def test_zero_finding_review_without_summary_or_statement_is_incomplete(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "# Security Review\nDate: 2026-09-27\nReviewer: agent\n\n"
        "## Files Reviewed\n- a.py\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE
