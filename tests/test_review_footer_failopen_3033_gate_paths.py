"""Task #3033 — GATE-FOOTER-FAILOPEN: gate-path and edge-case coverage.

Complements ``test_review_footer_failopen_3033.py`` (parser behaviour and
``_security_review_blocks_merge``) by pinning the OTHER consumers of
``_count_findings_in_review_file`` and the parser edges it leaves open:

  * the ``_merge_task_branch`` defensive invariant — the last line of defence
    before ``git merge`` — must refuse a stale-footer review instead of
    trusting its all-zero footer;
  * ``run_security_review`` must log the "not a trustworthy finished review"
    warning for an untrusted-but-present artifact, must NOT overwrite it with
    a fallback dump, and the persisted stable copy must still block;
  * the security-reviewer prompt tells the reviewer about the new rules;
  * missing / unreadable / fallback artifacts stay ``missing``/``fallback``
    and do not emit a spurious ``count-mismatch`` audit event;
  * a stale zero footer is a mismatch at EVERY severity, and the per-severity
    MAX tally is what the analysis carries;
  * Summary-section scoping, resolved-heading bounds and the near-empty line
    threshold boundary.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from equipa import db as equipa_db
from equipa.dispatch import _merge_task_branch, _security_review_blocks_merge
from equipa.loops import (
    ARTIFACTS_DIR_NAME,
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_FALLBACK,
    REVIEW_VERDICT_INCOMPLETE,
    REVIEW_VERDICT_MISSING,
    REVIEW_VERDICT_OK,
    SECURITY_REVIEW_FALLBACK_MARKER,
    _analyze_review_file,
    _count_findings_in_review_file,
    run_security_review,
)
from equipa.security_gate import SecurityGateBypassError

TASK_ID = 3031

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")

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

# The same stale-footer mistake, but with a HIGH header and a Summary that
# claims the review is finished — only the header/footer check can catch it.
STALE_FOOTER_HIGH_REVIEW = (
    "# Security Review\n\n"
    "Summary: review complete.\n\n"
    "### [S1] HIGH — API key logged at startup\n"
    "Body.\n\n"
    + ZERO_FOOTER
)

UNTRUSTED_EVENTS = ("count-mismatch", "review-incomplete")


def _counts(
    critical: int = 0, high: int = 0, medium: int = 0, low: int = 0,
    info: int = 0,
) -> dict[str, int]:
    return {
        "CRITICAL": critical, "HIGH": high, "MEDIUM": medium, "LOW": low,
        "INFO": info,
    }


def _footer(**counts: int) -> str:
    values = _counts(**counts)
    return "## Counts\n" + " | ".join(
        f"{severity}: {values[severity]}" for severity in SEVERITIES
    ) + "\n"


def _write_artifact(project_dir: Path, body: str) -> Path:
    """Write the review at the task-2476 ``.equipa-artifacts/`` location."""
    artifact_dir = project_dir / ARTIFACTS_DIR_NAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _untrusted_events(events: list[dict]) -> list[dict]:
    return [row for row in events if row["event"] in UNTRUSTED_EVENTS]


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


# ---------- Defensive invariant in _merge_task_branch ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_body",
    [TASK_3031_SKELETON_REVIEW, STALE_FOOTER_HIGH_REVIEW],
    ids=["task-3031-skeleton", "stale-footer-high"],
)
async def test_defensive_invariant_refuses_stale_footer_review(
    tmp_path: Path, persisted_audit_events: list[dict], capsys,
    review_body: str,
) -> None:
    # Before #3033 both artifacts parsed as all-zero and the invariant let
    # the merge proceed to git. It must now raise BEFORE any git call —
    # tmp_path is not a repository, so reaching git would fail differently.
    _write_artifact(tmp_path, review_body)

    with patch("equipa.dispatch.git_run_async") as git_mock:
        with pytest.raises(SecurityGateBypassError, match="missing or unparseable"):
            await _merge_task_branch(
                str(tmp_path), TASK_ID, f"forge-task-{TASK_ID}",
            )

    git_mock.assert_not_called()
    stderr = capsys.readouterr().err
    assert f"task={TASK_ID} event=count-mismatch" in stderr
    assert f"task={TASK_ID} event=defensive-invariant-fired" in stderr
    assert "reason=artifact-unparseable-or-missing" in stderr
    fired = [
        row for row in persisted_audit_events
        if row["event"] == "defensive-invariant-fired"
    ]
    assert fired and fired[0]["task_id"] == TASK_ID


@pytest.mark.asyncio
async def test_defensive_invariant_refuses_unfinished_summary_review(
    tmp_path: Path, persisted_audit_events: list[dict], capsys,
) -> None:
    # Headers and footer agree (both zero) — only the Summary betrays it.
    _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: TODO\n\n"
        "## Scope\nOrder router.\n\n" + ZERO_FOOTER,
    )

    with patch("equipa.dispatch.git_run_async") as git_mock:
        with pytest.raises(SecurityGateBypassError):
            await _merge_task_branch(
                str(tmp_path), TASK_ID, f"forge-task-{TASK_ID}",
            )

    git_mock.assert_not_called()
    assert "event=review-incomplete" in capsys.readouterr().err


# ---------- run_security_review wiring ----------


@pytest.fixture
def patched_security_runner():
    """Stub the agent + prompt plumbing so only the post-review path runs."""
    agent_result = {
        "success": True,
        "duration": 0.1,
        "result_text": "Review finished.",
        "errors": [],
    }

    async def fake_run_agent(cmd, timeout=None):
        return agent_result

    class _FakeCommand:
        def __enter__(self):
            return ["fake-cmd"]

        def __exit__(self, *exc_info):
            return False

    with patch("equipa.loops.run_agent", side_effect=fake_run_agent), \
         patch("equipa.loops.build_cli_command", return_value=_FakeCommand()), \
         patch(
             "equipa.loops.build_system_prompt", return_value="prompt",
         ) as prompt_mock, \
         patch("equipa.loops.get_role_turns", return_value=10), \
         patch("equipa.loops.get_role_model", return_value="opus"), \
         patch(
             "equipa.loops.load_dispatch_config",
             return_value={"security_review_timeout": 30},
         ), \
         patch("equipa.loops._extract_security_findings", return_value=[]), \
         patch("equipa.loops._create_security_lessons", return_value=0):
        yield prompt_mock


@pytest.mark.asyncio
async def test_run_security_review_keeps_untrusted_artifact_and_blocks(
    tmp_path: Path, persisted_audit_events: list[dict],
    patched_security_runner: MagicMock,
) -> None:
    worktree = tmp_path / "worktree"
    stable = tmp_path / "stable"
    worktree.mkdir()
    stable.mkdir()
    artifact = _write_artifact(worktree, TASK_3031_SKELETON_REVIEW)
    task = {"id": TASK_ID, "title": "t", "description": "d", "project_id": 1}
    output: list[str] = []

    await run_security_review(
        task, str(worktree), {}, MagicMock(), output=output,
        stable_project_dir=str(stable),
    )

    joined = "\n".join(output)
    assert "not a trustworthy finished review" in joined
    assert "artifact missing" not in joined
    assert "No critical or high severity findings" not in joined
    # The reviewer's own file is preserved verbatim — never replaced by a
    # fallback dump that would hide what the reviewer actually wrote.
    assert artifact.read_text(encoding="utf-8") == TASK_3031_SKELETON_REVIEW
    stable_copy = stable / ARTIFACTS_DIR_NAME / f"SECURITY-REVIEW-{TASK_ID}.md"
    assert stable_copy.read_text(encoding="utf-8") == TASK_3031_SKELETON_REVIEW
    assert SECURITY_REVIEW_FALLBACK_MARKER not in stable_copy.read_text(
        encoding="utf-8",
    )
    # The merge gate evaluates the stable copy and must block.
    assert _security_review_blocks_merge(str(stable), TASK_ID) == (True, None)


@pytest.mark.asyncio
async def test_run_security_review_trusted_artifact_reports_clean(
    tmp_path: Path, persisted_audit_events: list[dict],
    patched_security_runner: MagicMock,
) -> None:
    _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: complete.\n\n"
        "### [S1] LOW — minor\nBody.\n\n" + _footer(low=1),
    )
    task = {"id": TASK_ID, "title": "t", "description": "d", "project_id": 1}
    output: list[str] = []

    await run_security_review(task, str(tmp_path), {}, MagicMock(), output=output)

    joined = "\n".join(output)
    assert "No critical or high severity findings" in joined
    assert "not a trustworthy finished review" not in joined
    assert not _untrusted_events(persisted_audit_events)


@pytest.mark.asyncio
async def test_security_reviewer_prompt_states_fail_closed_rules(
    tmp_path: Path, persisted_audit_events: list[dict],
    patched_security_runner: MagicMock,
) -> None:
    task = {"id": TASK_ID, "title": "t", "description": "d", "project_id": 1}

    await run_security_review(task, str(tmp_path), {}, MagicMock(), output=[])

    security_task = patched_security_runner.call_args.args[0]
    description = security_task["description"]
    assert "### [TAG-NN] SEVERITY — title" in description
    assert "if they disagree the merge is BLOCKED" in description
    assert "Update the footer last" in description
    assert "IN PROGRESS, skeleton or TODO" in description
    # The pre-#3033 claim that the footer wins must be gone.
    assert "falls back to header counting" not in description


# ---------- Missing / unreadable / fallback artifacts ----------


def test_missing_artifact_is_missing_without_mismatch_event(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = tmp_path / f"SECURITY-REVIEW-{TASK_ID}.md"

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_MISSING
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None
    assert not _untrusted_events(persisted_audit_events)


def test_unreadable_artifact_is_treated_as_missing(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # A directory at the artifact path raises IsADirectoryError (an OSError).
    path = tmp_path / f"SECURITY-REVIEW-{TASK_ID}.md"
    path.mkdir()

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_MISSING
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None


def test_fallback_dump_stays_fallback_even_with_disagreeing_counts(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # A fallback dump quoting a skeleton review is a fallback first: it is
    # never re-classified as a count mismatch or re-trusted.
    path = _write_artifact(
        tmp_path,
        SECURITY_REVIEW_FALLBACK_MARKER + "\n" + TASK_3031_SKELETON_REVIEW,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_FALLBACK
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None
    assert not _untrusted_events(persisted_audit_events)
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


def test_mismatch_without_task_id_still_fails_closed(
    tmp_path: Path, persisted_audit_events: list[dict], capsys,
) -> None:
    path = _write_artifact(tmp_path, STALE_FOOTER_HIGH_REVIEW)

    assert _count_findings_in_review_file(path) is None
    assert "task=None event=count-mismatch" in capsys.readouterr().err


# ---------- Stale zero footer at every severity ----------


@pytest.mark.parametrize("severity", SEVERITIES)
def test_stale_zero_footer_is_mismatch_at_every_severity(
    tmp_path: Path, persisted_audit_events: list[dict], capsys, severity: str,
) -> None:
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: done.\n\n"
        f"### [S1] {severity} — finding\nBody.\n\n" + ZERO_FOOTER,
    )

    analysis = _analyze_review_file(path)
    expected_max = _counts(**{severity.lower(): 1})
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.trusted is False
    assert analysis.footer_counts == _counts()
    assert analysis.header_counts == expected_max
    assert analysis.counts == expected_max
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)
    rows = [
        row for row in persisted_audit_events
        if row["event"] == "count-mismatch"
    ]
    assert rows and rows[-1]["counts"] == expected_max
    assert "action=treat-as-missing" in capsys.readouterr().err


def test_max_tally_takes_larger_source_per_severity(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # Headers see HIGH, footer sees MEDIUM: the analysis MAX carries both,
    # so no source's findings are under-reported in the audit record.
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: done.\n\n"
        "### [S1] HIGH — one\nBody.\n\n" + _footer(medium=2),
    )

    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.counts == _counts(high=1, medium=2)


# ---------- Resolved fix-verification heading bounds ----------


@pytest.mark.parametrize(
    "footer",
    [_footer(low=0), _footer(low=3)],
    ids=["footer-below-live", "footer-above-live-plus-resolved"],
)
def test_footer_outside_resolved_window_is_mismatch(
    tmp_path: Path, persisted_audit_events: list[dict], footer: str,
) -> None:
    # One live LOW + one resolved LOW: the footer must be 1 or 2.
    path = _write_artifact(
        tmp_path,
        "# Security Re-review\n\nSummary: done.\n\n"
        "### [S0] LOW (fixed, verified) — old issue\n"
        "### [S1] LOW — new issue\n\n" + footer,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None


def test_all_findings_resolved_with_zero_footer_is_clean(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_artifact(
        tmp_path,
        "# Security Re-review\n\nSummary: all prior findings verified.\n\n"
        "### [S1] HIGH (fixed, verified) — token leak\nVerified.\n\n"
        "### [S2] MEDIUM [RESOLVED] — weak hash\nVerified.\n\n"
        + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (
        False, _counts(),
    )


def test_resolved_heading_without_footer_is_not_counted(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_artifact(
        tmp_path,
        "# Security Re-review\n\nSummary: done.\n\n"
        "### [S1] CRITICAL (resolved) — RCE in upload\nVerified.\n\n"
        "### [S2] LOW — new issue\nBody.\n",
    )

    assert _count_findings_in_review_file(path) == _counts(low=1)


# ---------- Summary scoping ----------


def test_marker_after_summary_section_ends_is_ignored(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # The Summary section ends at the next heading; a TODO in a later
    # "Follow-ups" section is not an unfinished-review marker.
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\n"
        "## Summary\nOne low-severity finding.\n\n"
        "## Follow-ups\nTODO: rotate the staging keys next sprint.\n\n"
        "### [S1] LOW — verbose errors\nBody.\n\n" + _footer(low=1),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK
    assert _count_findings_in_review_file(path) == _counts(low=1)


@pytest.mark.parametrize(
    "summary_block",
    [
        "## Summary — IN PROGRESS\nFindings below.\n",
        "- **Summary**: work_in progress skeleton\n",
        "### Summary\nIN_PROGRESS\n",
    ],
    ids=["marker-in-heading", "bulleted-bold-field", "underscore-variant"],
)
def test_summary_marker_variants_mark_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict], summary_block: str,
) -> None:
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\n" + summary_block + "\n"
        "### [S1] LOW — minor\nBody.\n\n" + _footer(low=1),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE
    assert _security_review_blocks_merge(str(tmp_path), TASK_ID) == (True, None)


def test_unfinished_summary_beats_non_zero_footer_only_review(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    # The footer-only "prose findings" tolerance must not rescue a review
    # that says it is unfinished.
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: IN PROGRESS\n\n"
        "Prose findings so far.\n\n" + _footer(high=1),
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE
    assert _count_findings_in_review_file(path, task_id=TASK_ID) is None


def test_unfinished_summary_without_footer_is_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_artifact(
        tmp_path,
        "# Security Review\n\nSummary: skeleton\n\n"
        "### [S1] MEDIUM — one\nBody.\n",
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_INCOMPLETE


# ---------- Near-empty threshold boundary ----------


def test_four_line_zero_finding_review_passes_threshold(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_artifact(
        tmp_path, "# Security Review\nNo findings.\n" + ZERO_FOOTER,
    )

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_OK


def test_three_line_zero_finding_review_is_incomplete(
    tmp_path: Path, persisted_audit_events: list[dict],
) -> None:
    path = _write_artifact(tmp_path, "# Security Review\n\n" + ZERO_FOOTER)

    analysis = _analyze_review_file(path)
    assert analysis.verdict == REVIEW_VERDICT_INCOMPLETE
    assert "near-empty" in analysis.detail
