"""Task #3038 fix-forward: independent review findings IR38-01, IR38-02, IR38-05.

    IR38-02  With several unfenced ``## Counts`` footers, the merge counts use
             the per-severity MAXIMUM across all of them. "Last footer wins"
             let a later quoted all-zero footer hide an earlier correct
             non-zero one (a regression against main).
    IR38-01  A finding candidate (non-level-3 heading, bold lead-in, bracket
             bullet) is never dropped for a "resolved" tail. Only the strict,
             end-anchored level-3 status grammar marks it resolved, and a
             resolved candidate is ADDED to the merge counts, never subtracted.
    IR38-05  A heading whose only severity words are an explicit zero tally
             ("## Findings — 0 CRITICAL / 0 HIGH ...") or an "Overall risk:
             LOW/INFO" label is not a finding. Non-zero tally entries still are.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa.loops import (
    MERGE_BLOCKING_SEVERITIES,
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_OK,
    _analyze_review_file,
    _analyze_review_views,
    _count_findings_in_review_file,
    backstop_reason,
)
from equipa.security_gate import normalize_review_text
from tests.review_gate_production import (
    as_reviewer_artifact,
    gate_blocks,
    production_seconds,
)
from tests.review_gate_timing import timing_test

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{backstop_reason(severity)} at line "
    for severity in MERGE_BLOCKING_SEVERITIES
)

BODY = (
    "# Security Review\n\n## Summary\nReviewed the diff for the payments "
    "module thoroughly.\n\n## Findings\n\n"
)
DETAIL = "Detail line one.\nDetail line two.\n"


def _footer(
    critical: int = 0, high: int = 0, medium: int = 0, low: int = 0,
    info: int = 0,
) -> str:
    return (
        f"\n## Counts\nCRITICAL: {critical} | HIGH: {high} | "
        f"MEDIUM: {medium} | LOW: {low} | INFO: {info}\n"
    )


def _counts(
    critical: int = 0, high: int = 0, medium: int = 0, low: int = 0,
    info: int = 0,
) -> dict[str, int]:
    return {
        "CRITICAL": critical, "HIGH": high, "MEDIUM": medium, "LOW": low,
        "INFO": info,
    }


def _write(tmp_path: Path, markdown: str) -> Path:
    path = tmp_path / "SECURITY-REVIEW-3038.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def _blocks_merge(path: Path) -> bool:
    """dispatch._security_review_blocks_merge itself (task 3167) on the
    review at ``path``, written as this cycle's reviewer artifact: untrusted
    or C+H > 0."""
    return gate_blocks(as_reviewer_artifact(path.read_text(encoding="utf-8")))


def _rules_counts(path: Path) -> dict[str, int] | None:
    """The merge counts of the shape rules alone (no severity-token
    backstop), or None when the rules do not trust the review."""
    analysis = _analyze_review_views(
        normalize_review_text(path.read_text(encoding="utf-8")))
    return analysis.counts if analysis.trusted else None


def _assert_backstop_blocks(path: Path) -> None:
    """Task 3152: an UPPER-case CRITICAL or HIGH outside a counted heading's
    label and the final strict footer (a prose finding, a second footer, a
    recap bullet) blocks, whatever the counts. Each review below blocked the
    merge before as well, by its CRITICAL or HIGH count."""
    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), analysis
    assert _count_findings_in_review_file(path) is None
    assert _blocks_merge(path)


def _assert_medium_backstop_blocks(path: Path) -> None:
    """Task 3161: an UPPER-case MEDIUM outside a counted heading's label and
    the final strict footer blocks the same way (no MEDIUM exemption)."""
    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(
        backstop_reason("MEDIUM") + " at line "), analysis
    assert _count_findings_in_review_file(path) is None
    assert _blocks_merge(path)


# ---------- IR38-02: per-severity maximum across all footers ----------


def test_later_quoted_zero_footer_cannot_mask_earlier_real_footer(
    tmp_path: Path,
) -> None:
    # Independent review case N04 / M21: prose HIGH counted only by the
    # footer, then an unfenced quote of the stale all-zero footer.
    path = _write(
        tmp_path,
        BODY + "S1: HIGH SQL injection in orders.py:40.\n" + _footer(high=1)
        + "\nPrevious run footer was:\n" + _footer(),
    )

    assert _blocks_merge(path)
    assert _rules_counts(path) == _counts(high=1)
    _assert_backstop_blocks(path)


def test_footer_maximum_is_per_severity(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "Prose-only findings.\n" + _footer(critical=1, low=2)
        + "\nOlder tally:\n" + _footer(high=1, low=1),
    )

    assert _rules_counts(path) == _counts(critical=1, high=1, low=2)
    _assert_backstop_blocks(path)


def test_zero_then_real_footer_still_uses_the_real_one(tmp_path: Path) -> None:
    # S3033-04 must stay fixed under the maximum rule.
    path = _write(
        tmp_path,
        BODY + "Quoted skeleton:\n" + _footer()
        + "\nS1: HIGH SQL injection.\n" + _footer(high=1),
    )

    assert _rules_counts(path) == _counts(high=1)
    _assert_backstop_blocks(path)


def test_larger_quoted_footer_disagreeing_with_headers_is_untrusted(
    tmp_path: Path,
) -> None:
    # Footer/header disagreement keeps its existing fail-closed rule: the
    # maximum footer (HIGH: 2) disagrees with one HIGH heading.
    path = _write(
        tmp_path,
        BODY + "### [S1] HIGH — SQL injection\n" + DETAIL
        + "Draft:\n" + _footer(high=2) + "\nFinal:\n" + _footer(high=1),
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


# ---------- IR38-01: candidates are never dropped as "resolved" ----------


@pytest.mark.parametrize(
    "finding_line",
    [
        "## [S1] HIGH — JWT signing key: FIXED string in config.py",
        "#### [S1] CRITICAL — Auth bypass — FIXED upstream, reintroduced here",
        "- **[S1] HIGH — nonce: FIXED value reused**",
        "- **[S1] HIGH — AES nonce: FIXED value reused**",
        "## [S1] HIGH — auth bypass. FIXED in 3021 but reintroduced",
        "- **[S1] HIGH — auth bypass — FIXED upstream, regressed here**",
    ],
    ids=["M01-l2-colon", "M02-l4-dash", "M03-bold-nonce", "M03-bold-aes",
         "N06-l2-dot", "N05-bold-dash"],
)
@pytest.mark.parametrize(
    "footer", ["", _footer()], ids=["no-footer", "stale-zero-footer"],
)
def test_live_candidate_with_fixed_in_title_blocks(
    tmp_path: Path, finding_line: str, footer: str,
) -> None:
    path = _write(tmp_path, BODY + finding_line + "\n" + DETAIL + footer)

    assert _blocks_merge(path)


@pytest.mark.parametrize(
    "finding_line",
    [
        "## [S1] HIGH — SQL injection — RESOLVED",
        "#### [S1] CRITICAL — RCE in upload — FIXED, verified",
        "- **[S1] HIGH — nonce reuse: FIXED**",
        "- **F1 (HIGH, renderer SSRF): FIXED.** Now uses safeFetch.",
    ],
    ids=["M32-l2-resolved", "l4-fixed-verified", "bold-fixed",
         "bold-recap-2910"],
)
@pytest.mark.parametrize(
    "footer", ["", _footer()], ids=["no-footer", "stale-zero-footer"],
)
def test_resolved_candidate_adds_to_merge_counts(
    tmp_path: Path, finding_line: str, footer: str,
) -> None:
    # Same rule as a resolved level-3 heading: the resolved form may be
    # omitted from the footer, but it is counted, never subtracted.
    path = _write(tmp_path, BODY + finding_line + "\n" + DETAIL + footer)

    counts = _rules_counts(path)

    assert counts is not None
    assert counts["CRITICAL"] + counts["HIGH"] == 1
    _assert_backstop_blocks(path)


def test_resolved_medium_candidate_is_counted_not_dropped(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        BODY + "## [S1] MEDIUM — CSRF on settings — FIXED, verified\n"
        + DETAIL + _footer(),
    )

    # The shape rules count it; its UPPER-case MEDIUM is no label of a
    # counted "###" heading, so the backstop blocks (task 3161, as task 3152
    # did for the CRITICAL and HIGH forms above).
    assert _rules_counts(path) == _counts(medium=1)
    _assert_medium_backstop_blocks(path)


# ---------- IR38-05: tally / overall-risk headings are not findings ----------


@pytest.mark.parametrize(
    "heading",
    [
        "## Overall risk: LOW",
        "## Overall risk — INFO",
        "## Findings — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW / 0 INFO",
        "#### 3.2 — 0 critical ✅",
    ],
)
def test_zero_tally_or_low_risk_heading_is_not_a_finding(
    tmp_path: Path, heading: str,
) -> None:
    path = _write(
        tmp_path,
        "# Security Review\n\n## Summary\nNo findings.\n\n" + heading
        + "\nFine.\n\n## Files\n- a.py\n- b.py\n" + _footer(),
    )

    assert _rules_counts(path) == _counts()
    if "CRITICAL" in heading or "HIGH" in heading:
        # Task 3152: the UPPER-case zero tally merged under the 3143 tally
        # exemption; CRITICAL and HIGH have none now, nor MEDIUM since task
        # 3161. In lower case it merges.
        _assert_backstop_blocks(path)
        path = _write(tmp_path, path.read_text(encoding="utf-8").replace(
            "0 CRITICAL / 0 HIGH / 0 MEDIUM", "0 critical / 0 high / 0 medium"))
    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _count_findings_in_review_file(path) == _counts()


@pytest.mark.parametrize(
    "heading",
    [
        # Non-zero tally entries remain candidates, including ones after the
        # first severity word (which the plain candidate match never saw).
        "## Findings — 0 CRITICAL / 2 HIGH / 0 MEDIUM",
        "## Findings — 1 CRITICAL",
        # A severity not in tally form keeps the whole heading a finding.
        "## [S1] HIGH — 0 CRITICAL paths remain",
        # Overall-risk exemption covers LOW/INFO only.
        "## Overall risk: HIGH",
        "## Overall risk: CRITICAL",
        # Section numbers and finding tags are not counts.
        "## 3.2 HIGH — SQL injection",
        "## S0 HIGH — SQL injection",
        "## SR-0 CRITICAL — auth bypass",
        "## 10 HIGH — SQL injection",
    ],
)
def test_non_zero_tally_or_blocking_risk_heading_still_blocks(
    tmp_path: Path, heading: str,
) -> None:
    path = _write(
        tmp_path,
        "# Security Review\n\n## Summary\nReviewed the diff.\n\n" + heading
        + "\nDetail.\n\n## Files\n- a.py\n- b.py\n" + _footer(),
    )

    assert _blocks_merge(path)


# ---------- the strict resolved grammar stays linear on candidates ----------


@pytest.mark.parametrize(
    "adversarial_line",
    [
        "- **[S1] HIGH — " + "(" * 20_000 + "**",
        "## [S1] HIGH — " + "[" * 20_000,
        "#### [S1] CRITICAL — " + "(x" * 10_000,
    ],
    ids=["bold-parens", "l2-brackets", "l4-paren-x"],
)
@timing_test
def test_long_candidate_title_parses_quickly_and_still_blocks(
    tmp_path: Path, adversarial_line: str,
) -> None:
    path = _write(tmp_path, BODY + adversarial_line + "\n" + _footer())

    blocked = _blocks_merge(path)
    elapsed = production_seconds(
        as_reviewer_artifact(path.read_text(encoding="utf-8")))

    assert elapsed < 1.0, f"parse took {elapsed:.2f}s"
    assert blocked


def test_resolved_status_after_long_title_is_still_recognised(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        BODY + "- **[S1] MEDIUM — " + "x" * 5_000 + " — FIXED, verified**\n"
        + _footer(),
    )

    # Counted as resolved by the shape rules; the recap's UPPER-case MEDIUM
    # is no counted heading label, so it blocks (task 3161).
    assert _rules_counts(path) == _counts(medium=1)
    _assert_medium_backstop_blocks(path)
