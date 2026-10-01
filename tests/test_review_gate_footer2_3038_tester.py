"""Task #3038 (GATE-FOOTER-2): independent tester coverage of edge cases.

Complements the developer's #3038 suites with contracts they do not pin:

    S3033-01  resolved headings ADD to the merge counts even when the footer
              is present and in range (counts = max(live + resolved, footer))
    S3033-02  a level-2 FIRST heading is not a title-exempt document title;
              a severity past the old 80-char heading cap is still seen;
              numbered-list bold lead-ins; a FIXED outside the bold span
              does not resolve a live bold finding
    S3033-04  fenced material after the footer does not count as trailing
              structure; ``_blank_code`` only closes a fence with the same
              fence character

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from equipa.loops import (
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_INCOMPLETE,
    REVIEW_VERDICT_OK,
    _analyze_review_file,
    _blank_code,
    _count_findings_in_review_file,
)

BODY = "# Security Review\n\n## Summary\nReviewed the diff.\n\n## Findings\n\n"


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
    assert analysis.detail.startswith("unaccounted severity token:"), (
        analysis.detail)


# ---------- S3033-01: resolved headings never lower the merge counts ----------


def test_resolved_heading_adds_to_counts_when_footer_counts_only_live(
    tmp_path: Path,
) -> None:
    """Footer HIGH: 1 is in range [live=1, live+resolved=2]; counts are 2."""
    path = _write(
        tmp_path,
        BODY
        + "### [S1] HIGH — Token replay in webhook handler\nDetail.\n\n"
        + "### [S0] HIGH — Upstream open redirect — FIXED\nVerified.\n"
        + _footer(high=1),
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_OK
    assert analysis.header_counts["HIGH"] == 1
    assert analysis.counts == _counts(high=2)
    assert _count_findings_in_review_file(path) == _counts(high=2)


@pytest.mark.parametrize(
    "footer", [_footer(), ""], ids=["zero-footer", "no-footer"],
)
def test_only_resolved_heading_over_blocks_rather_than_merging(
    tmp_path: Path, footer: str,
) -> None:
    """A heading that merely LOOKS resolved keeps its severity in the counts."""
    path = _write(
        tmp_path,
        BODY
        + "### [S1] HIGH — Session fixation (fixed, verified, not counted)\n"
        + "Verified in the diff.\n"
        + footer,
    )

    counts = _count_findings_in_review_file(path)

    assert counts is not None
    assert counts["HIGH"] == 1


# ---------- S3033-02: finding candidates the strict regex cannot see ----------


def test_level_two_first_heading_is_not_title_exempt(tmp_path: Path) -> None:
    """Only a level-1 first heading is a document title."""
    path = _write(
        tmp_path,
        "## [S1] HIGH — SQL injection in orders.py\n\n"
        "Summary: reviewed the order module.\n\n"
        + "Detail line one.\nDetail line two.\n"
        + _footer(),
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "HIGH=1" in analysis.detail
    assert _count_findings_in_review_file(path) is None


def test_severity_beyond_eighty_characters_in_heading_is_seen(
    tmp_path: Path,
) -> None:
    long_prefix = "Order routing and settlement ledger interaction " * 3
    assert len(long_prefix) > 80
    path = _write(
        tmp_path,
        BODY + f"### {long_prefix}— HIGH\nDetail.\n" + _footer(),
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


@pytest.mark.parametrize(
    "line",
    [
        "1. **[S1] Token replay — HIGH** — webhook handler",
        "2) **HIGH — nonce reuse** in the signer",
        "10. **(CRITICAL)** remote code execution via template",
    ],
)
def test_numbered_bold_lead_in_is_a_candidate(
    tmp_path: Path, line: str,
) -> None:
    path = _write(tmp_path, BODY + line + "\n" + _footer())

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


def test_title_case_untagged_bold_lead_in_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    """Untagged bold spans need an UPPERCASE severity to be candidates."""
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n1. **High availability** was not assessed.\n"
        + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK


def test_fixed_outside_bold_span_does_not_resolve_live_finding(
    tmp_path: Path,
) -> None:
    """Only the bold lead-in is the title span of a bullet finding."""
    path = _write(
        tmp_path,
        BODY + "- **[S1] HIGH — nonce reuse** — FIXED\n" + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


def test_fixed_inside_bold_span_resolves_bullet_recap(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n- **[S1] HIGH — nonce reuse: FIXED**\n"
        + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    # Task #3038 fix-forward (IR38-01): resolved, so the zero footer may
    # omit it, but it is ADDED to the merge counts and blocks (HIGH=1).
    assert _count_findings_in_review_file(path) == _counts(high=1)


def test_candidate_in_inline_code_is_ignored(tmp_path: Path) -> None:
    # Task 3143: code is not exempt from the backstop, so the examples in
    # UPPER case block; in lower case (reviewer prompt) no rule reads them.
    examples = (
        "No findings. Headings look like `### [S1] HIGH — title`.\n"
        + "`- **[S2] CRITICAL** — example`\n"
    )
    _assert_only_the_backstop_blocks(_write(tmp_path, BODY + examples + _footer()))
    path = _write(
        tmp_path, BODY + _lowercase_severity_words(examples) + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK


# ---------- S3033-04: the footer closes the review ----------


def test_fenced_heading_after_footer_is_not_trailing_structure(
    tmp_path: Path,
) -> None:
    # Task 3143: the example in UPPER case is a severity word in code, which
    # the backstop blocks; in lower case (reviewer prompt) it is no trailing
    # structure.
    example = "\n```markdown\n## Notes\n- **[S9] HIGH** — example only\n```\n"
    _assert_only_the_backstop_blocks(
        _write(tmp_path, BODY + "No findings.\n" + _footer() + example))
    path = _write(
        tmp_path,
        BODY + "No findings.\n" + _footer() + _lowercase_severity_words(example),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _count_findings_in_review_file(path) == _counts()


def test_strict_finding_header_inside_code_fence_still_counts(
    tmp_path: Path,
) -> None:
    """The strict ``### [TAG] SEV`` tally reads the raw text (fail closed).

    Only the #3038 candidate scan skips code. A strict-form heading quoted in
    a fence is therefore still a HIGH the zero footer does not count.
    """
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n```markdown\n### [S9] HIGH — example\n```\n"
        + _footer(),
    )

    analysis = _analyze_review_file(path)

    assert analysis.header_counts["HIGH"] == 1
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


def test_later_footer_after_real_one_is_the_one_used(tmp_path: Path) -> None:
    """Two footers: the per-severity MAXIMUM is used (task #3038 IR38-02).

    "Last footer wins" let a later all-zero footer mask the real one; the
    maximum keeps HIGH=1, which agrees with the header and blocks.
    """
    path = _write(
        tmp_path,
        BODY + "### [S1] HIGH — Token replay\nDetail.\n"
        + _footer(high=1) + _footer(),
    )

    analysis = _analyze_review_file(path)

    assert analysis.footer_counts == _counts(high=1)
    assert analysis.verdict == REVIEW_VERDICT_OK
    assert _count_findings_in_review_file(path) == _counts(high=1)


def test_trailing_level_one_heading_after_footer_is_incomplete(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        BODY + "No findings.\n" + _footer() + "\n# Appendix\nNotes.\n",
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "not the final section" in analysis.detail


def test_blank_code_needs_matching_fence_character() -> None:
    text = "a\n```\n## Counts\n~~~\nstill code\n```\nc"

    blanked = _blank_code(text).split("\n")

    assert blanked == ["a", "", "", "", "", "", "c"]


def test_blank_code_tilde_fence_and_inline_spans() -> None:
    text = "x `### [S1] HIGH` y\n~~~\n## Counts\n~~~\nz"

    blanked = _blank_code(text).split("\n")

    assert blanked == ["x  y", "", "", "", "z"]
