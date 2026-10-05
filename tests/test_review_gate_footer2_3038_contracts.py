"""Task #3038 (GATE-FOOTER-2): tester-authored contract tests.

Complements tests/test_review_gate_footer2_3038.py (the PoC regressions) by
pinning each new rule in equipa/loops.py at its boundary, so a mutation that
loosens or tightens one regex fails a named case:

    S3033-01  _RESOLVED_FINDING_HEADER_RE: status token anchored at the END
              of the heading; resolved headings never lower merge counts
    S3033-02  _FINDING_CANDIDATE_RE: which lines are finding candidates and
              which (checklists, hyphenated words, code) are not
    S3033-03  _TEMPLATE_PLACEHOLDER_RE and the zero-finding completion signal
    S3033-04  footer must be last; a stray unterminated fence hides nothing
    S3033-05  _INCOMPLETE_REVIEW_MARKER_RE: status position only

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from equipa import db as equipa_db
from equipa.dispatch import _security_review_blocks_merge
from equipa.loops import (
    MERGE_BLOCKING_SEVERITIES,
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_INCOMPLETE,
    REVIEW_VERDICT_OK,
    _RESOLVED_FINDING_HEADER_RE,
    _analyze_review_file,
    _analyze_review_views,
    _blank_code,
    _count_findings_in_review_file,
    backstop_reason,
)
from equipa.security_gate import normalize_review_text
from tests.review_gate_production import (
    AS_WRITTEN_ONLY_FINDINGS,
    blocked_by_the_gate_at,
)

# Task 3161: the backstop reason names the severity ("unaccounted HIGH token").
BLOCKING_TOKEN_REASONS = tuple(
    f"{backstop_reason(severity)} at line "
    for severity in MERGE_BLOCKING_SEVERITIES
)


def _rules_analysis(path: Path):
    """The shape rules alone, without the severity-token backstop."""
    return _analyze_review_views(
        normalize_review_text(path.read_text(encoding="utf-8")))


def _assert_backstop_blocks(path: Path) -> None:
    """Task 3152: an UPPER-case HIGH outside a counted heading's label and
    the final strict footer blocks whatever the counts (each review here
    blocked the merge before as well, by HIGH=1). Task 3170 (IR67-02):
    decided through the merge gate."""
    analysis = blocked_by_the_gate_at(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), analysis
    assert _count_findings_in_review_file(path) is None

TASK_ID = 3038

BODY = "# Security Review\n\n## Summary\nReviewed the diff.\n\n## Findings\n\n"
NO_SUMMARY_BODY = (
    "# Security Review\nDate: 2026-09-27\nReviewer: agent\n\n"
    "## Files Reviewed\n- equipa/loops.py\n\n"
)


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


def _write(project_dir: Path, body: str) -> Path:
    path = project_dir / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.write_text(body, encoding="utf-8")
    return path


# Task 3143: a standalone UPPER-case severity word, as the backstop reads it.
_SEVERITY_TOKEN_RE = re.compile(r"(?<![^\W_])(CRITICAL|HIGH|MEDIUM)(?![^\W_])")


def _lowercase_severity_words(text: str) -> str:
    return _SEVERITY_TOKEN_RE.sub(lambda match: match.group(1).lower(), text)


def _assert_only_the_backstop_blocks(path: Path) -> None:
    """Task 3143: the rules trusted the review (the backstop runs only then)
    and the severity-token backstop blocked it: the reviewer prompt allows
    UPPER-case CRITICAL, HIGH and MEDIUM only as a finding's label. Task
    3170 (IR67-02): decided through the merge gate."""
    analysis = blocked_by_the_gate_at(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH, analysis
    assert analysis.detail.startswith(BLOCKING_TOKEN_REASONS), (
        analysis.detail)


@pytest.mark.parametrize("finding", sorted(AS_WRITTEN_ONLY_FINDINGS))
def test_the_must_block_helpers_read_the_review_as_written(
    tmp_path: Path, finding: str,
) -> None:
    """Task 3170 (IR67-02): a severity only the review as written shows
    blocks through this suite's helpers, so a gate that parsed the
    normalised text (R3161-01) fails this suite too."""
    path = _write(tmp_path,
                  BODY + AS_WRITTEN_ONLY_FINDINGS[finding] + "\n" + _footer())
    _assert_only_the_backstop_blocks(path)
    _assert_backstop_blocks(path)


@pytest.fixture(autouse=True)
def persisted_audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture gate-audit persistence instead of writing TheForge rows."""
    events: list[dict] = []

    def _record(message, task_id, *, event=None, counts=None):
        events.append({"message": message, "event": event, "counts": counts})

    monkeypatch.setattr(equipa_db, "log_gate_audit", _record)
    monkeypatch.setenv("EQUIPA_GATE_AUDIT_LOG", "1")
    return events


# ---------- S3033-01: resolved-heading form, anchored at the end ----------


@pytest.mark.parametrize(
    "heading",
    [
        "### SR29-00 HIGH (fixed, verified, not counted)",
        "### SR-2996 S1 (MEDIUM) — FIXED, verified",
        "### [2775-S01] HIGH — requestPayout double spend → **FIXED**",
        "### [S1] HIGH — open redirect — RESOLVED (verified in a1b2c3d)",
        "### [S1] HIGH — open redirect [resolved]",
        "### [S1] HIGH — open redirect (carried over, not counted)",
        "### [S1] HIGH — open redirect: FIXED.",
        "### [S1] HIGH — open redirect — *FIXED*",
    ],
)
def test_resolved_status_at_end_of_heading_is_recognised(heading: str) -> None:
    assert _RESOLVED_FINDING_HEADER_RE.search(heading) is not None


@pytest.mark.parametrize(
    "heading",
    [
        # S3033-01 PoC titles: the status-like word is not the last token.
        "### [S1] HIGH — AES-GCM nonce (fixed at zero) allows forgery",
        "### [S1] HIGH — JWT signing key: FIXED string in config.py",
        "### [S1] CRITICAL — Webhook host [resolved via attacker DNS] trusted",
        "### [S1] HIGH — Fixed-size buffer overflow in parser",
        # StockForge #3032: "fixed" inside a note that does not start with it.
        "### [S1] LOW (latent; re-rate MEDIUM when S2 is fixed)",
        # Lowercase status after a separator is title wording, not a status.
        "### [S1] HIGH — nonce reuse — fixed",
        "### [S1] HIGH — session: resolved lazily from cookie",
    ],
)
def test_live_heading_with_status_like_wording_is_not_resolved(
    heading: str,
) -> None:
    assert _RESOLVED_FINDING_HEADER_RE.search(heading) is None


def test_trailing_fixed_paren_on_live_title_still_counts_without_footer(
    tmp_path: Path,
) -> None:
    # The task-description shape: "(fixed at zero)" IS the last token, so the
    # regex calls it resolved -- only "never subtract" keeps it counted.
    path = _write(
        tmp_path, BODY + "### [S1] HIGH — AES-GCM nonce (fixed at zero)\nx\n",
    )

    assert _count_findings_in_review_file(path) == _counts(high=1)
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID)[0] is True


def test_trailing_fixed_paren_on_live_title_blocks_under_zero_footer(
    tmp_path: Path,
) -> None:
    # The zero footer is tolerated (it may omit a resolved heading) but the
    # merge counts are max(live + resolved, footer), so HIGH still blocks.
    path = _write(
        tmp_path,
        BODY + "### [S1] HIGH — AES-GCM nonce (fixed at zero)\nx\n" + _footer(),
    )

    analysis = _analyze_review_file(path)

    assert analysis.verdict == REVIEW_VERDICT_OK
    assert analysis.counts == _counts(high=1)
    assert analysis.header_counts == _counts()
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID)[0] is True


def test_footer_above_live_plus_resolved_is_a_mismatch(tmp_path: Path) -> None:
    # Footer tolerance is the closed interval [live, live + resolved].
    path = _write(
        tmp_path,
        BODY + "### [S1] MEDIUM — CSRF on settings — FIXED, verified\nx\n"
        + _footer(medium=2),
    )

    assert blocked_by_the_gate_at(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


# ---------- S3033-02: finding candidates the strict regex cannot see ----------


@pytest.mark.parametrize(
    "line",
    [
        "###### [S1] Critical — deserialisation of job payload",
        "   #### [S1] HIGH — indented heading (up to three spaces)",
        "- [S1] HIGH — plain bullet with a finding tag",
        "* **[S1] critical** — lowercase severity in a bold tag",
        "**S1 (HIGH):** SQL injection in orders.py:40",
        "+ **SR12-3 High:** token logged at debug level",
    ],
)
def test_uncounted_candidate_form_is_a_count_mismatch(
    tmp_path: Path, line: str,
) -> None:
    path = _write(tmp_path, BODY + line + "\n" + _footer())

    analysis = blocked_by_the_gate_at(path)

    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert "finding-shaped lines not counted" in analysis.detail
    assert _count_findings_in_review_file(path) is None


@pytest.mark.parametrize(
    "line",
    [
        "- [x] High entropy secret scan: PASS",
        "- [ ] XSS in a high traffic template: PASS",
        "## Information disclosure review",
        "## Low-level parser notes",
        "- **Impact:** High if exploited, but not reachable",
        "    ## [S9] HIGH — four-space indented code, not a heading",
        "Prose that says a HIGH finding was considered and rejected.",
    ],
)
def test_non_candidate_line_does_not_hold_a_clean_review(
    tmp_path: Path, line: str,
) -> None:
    # Task 3143: an UPPER-case severity word in code or prose is blocked by
    # the backstop only; written in lower case (reviewer prompt) it merges.
    if _SEVERITY_TOKEN_RE.search(line):
        _assert_only_the_backstop_blocks(_write(
            tmp_path, BODY + "No findings.\n\n" + line + "\n" + _footer(),
        ))
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n" + _lowercase_severity_words(line) + "\n"
        + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _count_findings_in_review_file(path) == _counts()


def test_level_two_finding_counted_by_footer_is_trusted(tmp_path: Path) -> None:
    # A candidate is only a problem when nothing counted its severity.
    path = _write(
        tmp_path,
        BODY + "## [S1] HIGH — SQL injection in orders.py:40\nx\n"
        + _footer(high=1),
    )

    rules = _rules_analysis(path)
    assert rules.verdict == REVIEW_VERDICT_OK
    assert rules.counts == _counts(high=1)
    # Task 3152: a level-2 heading is no finding heading the parser counts,
    # so its UPPER-case HIGH is unaccounted.
    _assert_backstop_blocks(path)


def test_candidate_inside_closed_code_fence_is_ignored(tmp_path: Path) -> None:
    # Task 3143: code is not exempt from the backstop (the reviewer prompt
    # keeps UPPER-case severity words out of code), so the payload quoted in
    # UPPER case blocks; quoted in lower case, no rule reads it.
    body = (
        BODY + "No findings. The PoC payload was:\n\n```markdown\n"
        "## [S1] HIGH — example heading inside a quoted payload\n```\n"
    )
    _assert_only_the_backstop_blocks(_write(tmp_path, body + _footer()))
    path = _write(tmp_path, _lowercase_severity_words(body) + _footer())

    assert _count_findings_in_review_file(path) == _counts()


def test_unterminated_fence_hides_no_candidate(tmp_path: Path) -> None:
    # Fail closed: a stray fence must not blank the rest of the file.
    path = _write(
        tmp_path,
        BODY + "No findings.\n\n```text\n"
        "## [S1] HIGH — written after a stray fence\n" + _footer(),
    )

    assert blocked_by_the_gate_at(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None


def test_blank_code_keeps_line_numbers_and_unterminated_tail() -> None:
    text = "a `code` b\n```\nhidden\n```\nshown\n~~~\nstray\n"

    blanked = _blank_code(text).split("\n")

    assert len(blanked) == len(text.split("\n"))
    assert blanked[0] == "a  b"
    assert blanked[1:4] == ["", "", ""]
    assert blanked[4] == "shown"
    assert blanked[5:7] == ["~~~", "stray"]


def test_count_mismatch_emits_gate_audit_event(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write(
        tmp_path, BODY + "#### [S1] HIGH — level-4 finding\nx\n" + _footer(),
    )

    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None
    assert [event["event"] for event in persisted_audit_events] == [
        "count-mismatch",
    ]
    assert "HIGH=1" in persisted_audit_events[0]["message"]


# ---------- S3033-04: the footer closes the review ----------


def test_heading_after_footer_is_incomplete(tmp_path: Path) -> None:
    path = _write(
        tmp_path, BODY + "No findings.\n" + _footer() + "\n## Appendix\nx\n",
    )

    analysis = blocked_by_the_gate_at(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "final section" in analysis.detail


def test_counted_bullet_finding_after_footer_is_incomplete(
    tmp_path: Path,
) -> None:
    # The severity IS counted, so only the footer-position rule catches it.
    path = _write(
        tmp_path,
        BODY + "### [S1] LOW — verbose errors\nx\n" + _footer(low=1)
        + "\n- **[S2] LOW** — appended after the footer\n",
    )

    assert blocked_by_the_gate_at(path).verdict == REVIEW_VERDICT_INCOMPLETE


def test_non_zero_earlier_footer_is_overridden_by_last(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "### [S1] HIGH — SQL injection\n### [S2] LOW — verbose errors\n"
        + "Draft tally:\n" + _footer(high=1) + "\nFinal tally:\n"
        + _footer(high=1, low=1),
    )

    analysis = _rules_analysis(path)

    assert analysis.verdict == REVIEW_VERDICT_OK
    assert analysis.footer_counts == _counts(high=1, low=1)
    # Task 3152: the draft tally's CRITICAL and HIGH labels are not the
    # final footer's.
    _assert_backstop_blocks(path)


# ---------- S3033-03: unfilled template / zero-finding completion ----------


@pytest.mark.parametrize(
    "placeholder_line",
    [
        "[1-2 sentences: what was reviewed, finding count, overall risk]",
        "### [S1] [SEVERITY] — Title",
        "- Hardcoded secrets: [PASS/FAIL]",
        "- [list]",
        "Date: [date]",
        "# Security Review: [Project Name]",
        "- **File:** path/to/file.ext:42",
        "Semgrep: [X findings]",
        "Grep patterns: [X pattern matches]",
        "Scope: [X files inspected]",
    ],
)
def test_each_template_placeholder_marks_review_incomplete(
    tmp_path: Path, placeholder_line: str,
) -> None:
    path = _write(
        tmp_path, BODY + "No findings.\n\n" + placeholder_line + "\n" + _footer(),
    )

    analysis = blocked_by_the_gate_at(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "placeholder" in analysis.detail
    assert _count_findings_in_review_file(path) is None


@pytest.mark.parametrize(
    "statement",
    [
        "No security issues found.",
        "No blocking findings.",
        "Findings: none",
        "Issues — **none**",
        "There are no exploitable vulnerabilities in this diff.",
    ],
)
def test_no_findings_statement_completes_review_without_summary(
    tmp_path: Path, statement: str,
) -> None:
    path = _write(tmp_path, NO_SUMMARY_BODY + statement + "\n" + _footer())

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK


@pytest.mark.parametrize(
    "prose",
    [
        "Notes about findings are pending.",
        "Known issues will be listed here.",
    ],
)
def test_prose_that_is_not_a_no_findings_statement_stays_incomplete(
    tmp_path: Path, prose: str,
) -> None:
    path = _write(tmp_path, NO_SUMMARY_BODY + prose + "\n" + _footer())

    assert blocked_by_the_gate_at(path).verdict == REVIEW_VERDICT_INCOMPLETE


def test_empty_summary_section_is_not_a_summary(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "# Security Review\n\n## Summary\n\n## Files Reviewed\n- a.py\n"
        "- b.py\n" + _footer(),
    )

    analysis = blocked_by_the_gate_at(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "no Summary" in analysis.detail


# ---------- S3033-05: unfinished marker only in status position ----------


@pytest.mark.parametrize(
    "summary",
    [
        "Summary: IN PROGRESS - initial skeleton",
        "## Summary\n**WORK IN PROGRESS**",
        "## Summary\nTODO: fill in after the scan",
        "## Summary\n[In progress]",
        "- **Summary:** in_progress",
        "## Summary — skeleton",
    ],
)
def test_status_position_marker_marks_review_incomplete(
    tmp_path: Path, summary: str,
) -> None:
    path = _write(
        tmp_path,
        "# Security Review\n\n" + summary + "\n\n## Findings\nNone.\n\n"
        "## Files Reviewed\n- a.py\n" + _footer(),
    )

    analysis = blocked_by_the_gate_at(path)

    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "summary marker" in analysis.detail


@pytest.mark.parametrize(
    "summary",
    [
        "## Summary\nReviewed the skeleton loader; no issues found.",
        "## Summary\nNo findings; the TODO endpoint was checked.",
        "Summary: Reviewed the diff. Nothing in progress remains.",
        "## Summary\nComplete. One in-progress migration was out of scope.",
    ],
)
def test_marker_word_in_finished_summary_prose_is_not_held(
    tmp_path: Path, summary: str,
) -> None:
    path = _write(
        tmp_path,
        "# Security Review\n\n" + summary + "\n\n## Findings\nNone.\n\n"
        "## Files Reviewed\n- a.py\n" + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (
        False, _counts(),
    )


def test_marker_outside_summary_does_not_hold(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BODY + "None.\n\n## Follow-ups\nTODO: rotate the staging key.\n"
        + _footer(),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
