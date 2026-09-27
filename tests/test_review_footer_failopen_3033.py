"""Regression tests for task #3033 — GATE-FOOTER-FAILOPEN.

Observed 2026-09-27 on CryptoTrader task #3031: SECURITY-REVIEW-3031.md was
an unfinished review (Summary "IN PROGRESS - initial skeleton") holding
``### [S3031-01] MEDIUM`` and ``### [S3031-02] LOW`` headers but still ending
with the skeleton footer ``CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 |
INFO: 0``. ``_count_findings_in_review_file`` preferred the footer, the gate
logged ``C=0 H=0 M=0 L=0 I=0 reason=clean`` and merged. A HIGH header written
the same way would have merged too.

The fix tallies BOTH the footer and the finding headers. These tests pin:
  * the per-severity MAX tally is what gets reported (stale-zero footer +
    MEDIUM header reports MEDIUM=1);
  * any footer/header disagreement (except zero headers + non-zero footer)
    makes the artifact "missing", so ``block_on_missing`` blocks, with a
    ``GATE-AUDIT event=count-mismatch`` line;
  * an IN PROGRESS / skeleton / TODO Summary, or a near-empty zero-finding
    file, is "incomplete" and blocks the same way;
  * a legitimate footer-only review, a matching footer + headers review, a
    header-only (no footer) review and a terse clean review still work.

Copyright 2026 Forgeborn
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

TASK_ID = 3031

ZERO_FOOTER = (
    "## Counts\n"
    "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
)

# Verbatim shape of the artifact that merged clean on task #3031.
TASK_3031_SKELETON_REVIEW = (
    "# Security Review — task 3031\n\n"
    "Summary: IN PROGRESS - initial skeleton\n\n"
    "## Findings\n\n"
    "### [S3031-01] MEDIUM — Unvalidated order size accepted from config\n"
    "Details to follow.\n\n"
    "### [S3031-02] LOW — Verbose exception text in API error body\n"
    "Details to follow.\n\n"
    + ZERO_FOOTER
)


def _counts(
    critical: int = 0, high: int = 0, medium: int = 0, low: int = 0,
    info: int = 0,
) -> dict[str, int]:
    return {
        "CRITICAL": critical, "HIGH": high, "MEDIUM": medium, "LOW": low,
        "INFO": info,
    }


def _write_review(project_dir: Path, body: str) -> Path:
    path = project_dir / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def persisted_audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture gate-audit persistence instead of writing TheForge rows."""
    events: list[dict] = []

    def _record(message, task_id, *, event=None, counts=None):
        events.append(
            {"message": message, "task_id": task_id, "event": event,
             "counts": counts},
        )

    monkeypatch.setattr(equipa_db, "log_gate_audit", _record)
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")
    return events


# ---------- The observed #3031 artifact ----------


def test_task_3031_skeleton_review_blocks_merge(
    tmp_path: Path, persisted_audit_events: list[dict], capsys,
) -> None:
    _write_review(tmp_path, TASK_3031_SKELETON_REVIEW)

    blocks, counts = _security_review_blocks_merge(str(tmp_path), TASK_ID)

    assert blocks is True
    assert counts is None
    stderr = capsys.readouterr().err
    assert f"task={TASK_ID} event=count-mismatch" in stderr
    assert "footer=[C=0 H=0 M=0 L=0 I=0]" in stderr
    assert "headers=[C=0 H=0 M=1 L=1 I=0]" in stderr
    mismatch_rows = [
        row for row in persisted_audit_events
        if row["event"] == "count-mismatch"
    ]
    assert mismatch_rows and mismatch_rows[0]["task_id"] == TASK_ID
    assert mismatch_rows[0]["counts"] == _counts(medium=1, low=1)


# ---------- Stale footer vs real headers ----------


def test_stale_zero_footer_with_medium_header_counts_medium(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\n"
        "Summary: one medium issue in the retry loop.\n\n"
        "### [S1] MEDIUM — Unbounded retry on exchange 5xx\n"
        "Body.\n\n" + ZERO_FOOTER,
    )

    analysis = _analyze_review_file(path)

    assert analysis.counts == _counts(medium=1)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None


def test_stale_zero_footer_with_high_header_blocks(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\n"
        "Summary: review complete.\n\n"
        "### [S1] HIGH — API key written to debug log\n"
        "Body.\n\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).counts == _counts(high=1)
    blocks, counts = _security_review_blocks_merge(str(tmp_path), TASK_ID)
    assert (blocks, counts) == (True, None)


@pytest.mark.parametrize(
    ("headers", "footer_line"),
    [
        # Footer over-counts a severity the headers also carry.
        (
            "### [S1] HIGH — one\n",
            "CRITICAL: 0 | HIGH: 2 | MEDIUM: 0 | LOW: 0 | INFO: 0",
        ),
        # Footer moved a finding to a different severity.
        (
            "### [S1] MEDIUM — one\n",
            "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0",
        ),
        # Prose heading counted as a finding; zero footer (old F-02 case).
        (
            "### Summary of HIGH-impact findings\n",
            "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0",
        ),
    ],
)
def test_any_footer_header_disagreement_blocks(
    tmp_path: Path, persisted_audit_events: list[dict], headers: str,
    footer_line: str,
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\nSummary: done.\n\n" + headers
        + "Body.\n\n## Counts\n" + footer_line + "\n",
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


# ---------- Unfinished reviews ----------


@pytest.mark.parametrize(
    "summary_block",
    [
        "Summary: IN PROGRESS - initial skeleton\n",
        "**Summary:** in-progress, findings pending\n",
        "## Summary\n\nSkeleton only; sections below are placeholders.\n",
        "## Summary\nTODO: fill in after running semgrep\n",
    ],
)
def test_unfinished_summary_marks_review_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict], capsys,
    summary_block: str,
) -> None:
    # Zero headers and a zero footer agree, so only the Summary gives the
    # unfinished review away.
    path = _write_review(
        tmp_path,
        "# Security Review\n\n" + summary_block + "\n"
        "## Scope\nReviewed the order router.\n\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)
    assert "event=review-incomplete" in capsys.readouterr().err


def test_todo_outside_summary_does_not_mark_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\n"
        "Summary: no blocking findings.\n\n"
        "## Findings\n\n"
        "### [S1] LOW — A TODO comment references a skeleton handler\n"
        "The in-progress refactor left a TODO; harmless.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n",
    )

    assert _count_findings_in_review_file(path) == _counts(low=1)


@pytest.mark.parametrize(
    "body",
    ["", "\n\n", ZERO_FOOTER, "# Security Review\n\n" + ZERO_FOOTER],
)
def test_near_empty_zero_finding_review_is_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict], body: str,
) -> None:
    path = _write_review(tmp_path, body)

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


# ---------- Legitimate reviews still work ----------


def test_footer_only_review_with_prose_findings_is_honoured(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\nPlain prose findings, no formal headers.\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 1 | MEDIUM: 0 | LOW: 0 | INFO: 0\n",
    )

    assert _count_findings_in_review_file(path) == _counts(high=1)
    blocks, counts = _security_review_blocks_merge(str(tmp_path), TASK_ID)
    assert blocks is True
    assert counts == _counts(high=1)


def test_terse_clean_review_with_zero_footer_passes(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    _write_review(
        tmp_path,
        "# Security Review\n\nNo blocking findings.\n\n" + ZERO_FOOTER,
    )

    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (
        False, _counts(),
    )
    assert not [
        row for row in persisted_audit_events
        if row["event"] in ("count-mismatch", "review-incomplete")
    ]


def test_matching_footer_and_headers_pass(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\nSummary: complete.\n\n"
        "### [S1] MEDIUM — one\n### [S2] LOW — two\n### [S3] INFO — three\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 1 | LOW: 1 | INFO: 1\n",
    )

    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_OK
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (
        False, _counts(medium=1, low=1, info=1),
    )


@pytest.mark.parametrize(
    "resolved_heading",
    [
        # Shapes seen on StockForge #3029 and 3DGenerator #3000, which the
        # audit found merged correctly with the footer excluding them.
        "### SR29-00 HIGH (fixed, verified, not counted) — duplicate key\n",
        "### SR-2996 S1 (MEDIUM) — FIXED, verified\n",
        "### [S1] HIGH [RESOLVED] — token leak\n",
    ],
)
def test_resolved_fix_verification_headings_are_not_live_findings(
    tmp_path: Path, persisted_audit_events: list[dict], resolved_heading: str,
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Re-review\n\nSummary: prior findings verified fixed.\n\n"
        + resolved_heading + "Verified.\n\n"
        "### [V1] LOW — new minor issue\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 1 | INFO: 0\n",
    )

    assert _count_findings_in_review_file(path) == _counts(low=1)


def test_fixed_word_in_title_is_still_a_live_finding(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # "Fixed-size" is part of the title, not a resolution status: with a
    # stale zero footer this must still be a mismatch, never a clean merge.
    _write_review(
        tmp_path,
        "# Security Review\n\nSummary: done.\n\n"
        "### [S1] HIGH — Fixed-size buffer overflow in frame parser\n"
        "Body.\n\n" + ZERO_FOOTER,
    )

    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


def test_fixed_inside_a_live_finding_note_is_still_counted(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # Shape from StockForge #3032: a live LOW whose note mentions "fixed".
    _write_review(
        tmp_path,
        "# Security Review\n\nSummary: done.\n\n"
        "### [S1] LOW (latent; re-rate MEDIUM when S2 is fixed) — carry\n"
        "Body.\n\n" + ZERO_FOOTER,
    )

    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


def test_footer_may_count_a_resolved_heading(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Re-review\n\nSummary: done.\n\n"
        "### [S0] LOW (fixed, verified) — old issue\n"
        "### [S1] LOW — new issue\n\n"
        "## Counts\n"
        "CRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 2 | INFO: 0\n",
    )

    assert _count_findings_in_review_file(path) == _counts(low=2)


def test_headers_without_footer_use_header_counts(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_review(
        tmp_path,
        "# Security Review\n\n### [S1] CRITICAL — one\n### [S2] HIGH — two\n",
    )

    assert _count_findings_in_review_file(path) == _counts(critical=1, high=1)
