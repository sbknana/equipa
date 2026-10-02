"""Task #3038 (S3033-02): finding candidates at any heading level / bold form.

A live finding the footer does not count must never merge under a stale
all-zero footer, whatever markdown form the reviewer used for it:

    * a heading of ANY level, including a severity past the 80 characters
      the strict level-3 regex scans;
    * a bold span, bullet or numbered item led by an UPPERCASE severity
      ("**HIGH — nonce (fixed at zero)**"), or carrying a finding tag.

And the widening must not hold finished clean reviews whose bold prose
merely mentions a severity, nor a fix review whose document title names the
upstream finding being fixed.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from equipa.loops import (
    MERGE_BLOCKING_SEVERITIES,
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_OK,
    _analyze_review_file,
    backstop_reason,
)

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{backstop_reason(severity)} at line "
    for severity in MERGE_BLOCKING_SEVERITIES
)

TITLE = "# Security Review — Task 9999\n\n"
BODY = "## Summary\nReviewed the crypto module.\n\n## Findings\n\n"
ZERO_FOOTER = (
    "\n\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
)
PADDING = "x" * 90


def _write_review(tmp_path: Path, markdown: str) -> Path:
    path = tmp_path / "SECURITY-REVIEW-9999.md"
    path.write_text(markdown, encoding="utf-8")
    return path


# Task 3143: a standalone UPPER-case severity word, as the backstop reads it.
_SEVERITY_TOKEN_RE = re.compile(r"(?<![^\W_])(CRITICAL|HIGH|MEDIUM)(?![^\W_])")


def _lowercase_severity_words(text: str) -> str:
    return _SEVERITY_TOKEN_RE.sub(lambda match: match.group(1).lower(), text)


def _assert_only_the_backstop_blocks(path: Path) -> None:
    """Task 3143: the rules trusted the review (the backstop runs only then)
    and the severity-token backstop blocked it: the reviewer prompt allows
    UPPER-case CRITICAL, HIGH and MEDIUM only as a finding's label."""
    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), (
        analysis.detail)


@pytest.mark.parametrize(
    "finding_line",
    [
        "**HIGH — AES-GCM nonce (fixed at zero) allows forgery**",
        "**HIGH:** AES-GCM nonce (fixed at zero) allows forgery",
        "- **HIGH** — AES-GCM nonce fixed at zero",
        "**(HIGH) AES-GCM nonce fixed at zero**",
        "1. **[S1] HIGH** — AES-GCM nonce fixed at zero",
        "2) **HIGH: AES-GCM nonce fixed at zero**",
        f"**[S1] AES-GCM nonce {PADDING[:50]} (HIGH)**",
        "# HIGH — AES-GCM nonce (fixed at zero) allows forgery",
        f"## [S1] AES-GCM nonce {PADDING} — HIGH",
        f"### S1 AES-GCM nonce {PADDING} (HIGH)",
        f"#### S1 AES-GCM nonce {PADDING} (HIGH)",
    ],
    ids=[
        "bold-untagged", "bold-untagged-colon", "bullet-bold-untagged",
        "bold-severity-paren", "numbered-bold-tag", "numbered-bold-untagged",
        "bold-tag-long", "h1-after-title", "h2-long", "h3-long", "h4-long",
    ],
)
def test_uncounted_finding_form_blocks_under_zero_footer(
    tmp_path: Path, finding_line: str,
) -> None:
    review = _write_review(
        tmp_path,
        TITLE + BODY + finding_line + "\nImpact paragraph.\n" + ZERO_FOOTER,
    )

    analysis = _analyze_review_file(review)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "HIGH=1" in analysis.detail


# Task 3152: "**7 findings: 0 CRITICAL / 0 HIGH / ...**" and "**No CRITICAL
# or HIGH findings.** ..." merged as written under the 3143 tally and
# negation exemptions. CRITICAL and HIGH have none now: they block as written.


@pytest.mark.parametrize(
    "prose_line",
    [
        "**No CRITICAL or HIGH findings.** The change is security-positive.",
        "**7 findings: 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW / 0 INFO.**",
        "4. **Medium (30 min):** move the patch target.",
        "6. **High value (2-4 hr):** replace Test 1.",
        "**The reported HIGH is fixed, and fixed in depth.**",
        "- **Why not MEDIUM:** the primitive is unreachable.",
    ],
)
def test_bold_prose_mentioning_a_severity_does_not_hold_clean_review(
    tmp_path: Path, prose_line: str,
) -> None:
    # Task 3143: in UPPER case the severity word labels no finding, so the
    # backstop blocks; in lower case (reviewer prompt) it is prose.
    if ("MEDIUM" in prose_line and "HIGH" not in prose_line
          and "CRITICAL" not in prose_line):
        # Task 3152: a MEDIUM-only unaccounted token blocks, as on main
        # (task 3149, R3143-06, had counted it as a logged advisory).
        blocked = _analyze_review_file(_write_review(
            tmp_path, TITLE + BODY + prose_line + "\n" + ZERO_FOOTER,
        ))
        assert blocked.verdict == REVIEW_VERDICT_COUNT_MISMATCH, blocked
        assert blocked.detail.startswith(
            backstop_reason("MEDIUM") + " at line "), blocked.detail
        assert "MEDIUM=1 at line " in blocked.detail, blocked.detail
    elif _SEVERITY_TOKEN_RE.search(prose_line):
        _assert_only_the_backstop_blocks(_write_review(
            tmp_path, TITLE + BODY + prose_line + "\n" + ZERO_FOOTER,
        ))
    review = _write_review(
        tmp_path,
        TITLE + BODY + _lowercase_severity_words(prose_line) + "\n"
        + ZERO_FOOTER,
    )

    assert _analyze_review_file(review).verdict == REVIEW_VERDICT_OK


def test_severity_field_under_zero_footer_now_fails_closed(
    tmp_path: Path,
) -> None:
    """gate-06: a ``Severity:`` field is a finding form, whatever follows it.

    Task #3038 treated "**Severity:** HIGH" as prose because it assumed an
    enclosing heading carried the severity. A "### Finding 1" heading with
    no severity plus this field merged a HIGH behind a zero footer, so the
    field is counted now and the reviewer prompt forbids severity words in
    it. Exempting "HIGH is not ..." would let "HIGH exploitable ..." through.
    """
    review = _write_review(
        tmp_path,
        TITLE + BODY + "- **Severity:** HIGH is not reachable from this diff.\n"
        + ZERO_FOOTER,
    )

    analysis = _analyze_review_file(review)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "HIGH=1" in analysis.detail


def test_fix_review_title_naming_upstream_finding_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    # cryptotrader-v2 SECURITY-REVIEW-2872.md shape: the title cites the
    # finding the task fixed; this review itself found nothing blocking.
    # Task 3143: the cited severity in UPPER case labels no finding of this
    # review, so the backstop blocks it; in lower case it is no candidate.
    _assert_only_the_backstop_blocks(_write_review(
        tmp_path,
        "# Security Review: CT-FIX-F7 — phantom fill (D5-01 HIGH)\n\n"
        + BODY + "No blocking findings.\n" + ZERO_FOOTER,
    ))
    review = _write_review(
        tmp_path,
        "# Security Review: CT-FIX-F7 — phantom fill (D5-01 high)\n\n"
        + BODY + "No blocking findings.\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(review).verdict == REVIEW_VERDICT_OK


def test_title_exemption_covers_only_the_first_heading(tmp_path: Path) -> None:
    review = _write_review(
        tmp_path,
        "## Preamble\nContext.\n\n"
        "# Review: CT-FIX-F7 — phantom fill (D5-01 HIGH)\n\n"
        + BODY + ZERO_FOOTER,
    )

    analysis = _analyze_review_file(review)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "HIGH=1" in analysis.detail


def test_counted_untagged_bold_finding_is_trusted(tmp_path: Path) -> None:
    """Task 3152: an UPPER-case HIGH that is no counted heading's label
    blocks whatever the footer counts (it blocked the merge before as well,
    by HIGH=1). Written in title case the footer counts it as before."""
    footer = "\n## Counts\nCRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
    _assert_only_the_backstop_blocks(_write_review(
        tmp_path,
        TITLE + BODY
        + "**HIGH — AES-GCM nonce (fixed at zero) allows forgery**\n" + footer,
    ))
    review = _write_review(
        tmp_path,
        TITLE + BODY
        + "**High — AES-GCM nonce (fixed at zero) allows forgery**\n" + footer,
    )

    analysis = _analyze_review_file(review)

    assert analysis.verdict == REVIEW_VERDICT_OK, analysis
    assert analysis.counts is not None
    assert analysis.counts["HIGH"] == 1
