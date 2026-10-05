"""Production-path helpers of the review-gate tests (task 3167, I3164-02).

Task 3164 (R3161-01) found the merge gate parsing a different text than the
tests did: every must-block suite called ``loops._analyze_review_file``
directly, so they all passed while production merged reviews they blocked.
These helpers decide through ``dispatch._security_review_blocks_merge``, the
function the orchestrator calls before a merge: the text is written as this
cycle's ``SECURITY-REVIEW`` artifact, a succeeded reviewer run is recorded
for it with the nonce the text carries, and provenance hands the parser the
bytes it verified. A regression anywhere on that chain fails the suites that
use them.

Helpers that take only a text write to one scratch project per process
(``gate_project``); ``project_dir`` points them at a test's own ``tmp_path``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import equipa.db as equipa_db
from equipa import loops
from equipa.dispatch import _security_review_blocks_merge
from equipa.security_gate import (
    REVIEW_BIDI_CONTROL_REASON,
    REVIEWER_STATUS_SUCCEEDED,
    ProvenanceVerdict,
    ReviewerRunRecord,
    fingerprint_artifact,
    record_reviewer_run,
    review_complete_line,
    review_completion_nonce,
    reviewer_nonce_line,
    verify_reviewer_provenance,
)
from tests.review_gate_timing import median_cpu_seconds

# The nonce every review-gate test review carries in its provenance and
# completion lines.
NONCE = "0123456789abcdef0123456789abcdef"
GATE_TASK_ID = 3167

_scratch_projects: dict[str, tempfile.TemporaryDirectory] = {}


@dataclass(frozen=True)
class ProductionDecision:
    """What the merge gate decided on one review, and the provenance verdict
    that handed the parser its text."""

    blocks: bool
    counts: dict | None
    provenance: ProvenanceVerdict


def gate_project() -> Path:
    """This process's scratch project directory (created on first use and
    removed when the interpreter exits)."""
    if "project" not in _scratch_projects:
        _scratch_projects["project"] = tempfile.TemporaryDirectory(
            prefix="review-gate-production-")
    return Path(_scratch_projects["project"].name)


def as_reviewer_artifact(text: str, *, nonce: str = NONCE) -> str:
    """``text`` as a reviewer run writes it: its provenance line first and
    its completion line last, each added only when missing (older suites
    build bare reviews)."""
    if reviewer_nonce_line(nonce) not in text:
        text = f"{reviewer_nonce_line(nonce)}\n{text}"
    if review_completion_nonce(text) != nonce:
        text = f"{text.rstrip()}\n{review_complete_line(nonce)}\n"
    return text


def write_recorded_review(project_dir: Path, task_id: int, text: str, *,
                          nonce: str = NONCE) -> Path:
    """Write ``text`` as the task's review artifact and record the succeeded
    reviewer run that wrote it."""
    path = Path(project_dir) / ".equipa-artifacts" / f"SECURITY-REVIEW-{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    record_reviewer_run(ReviewerRunRecord(
        task_id=task_id, nonce=nonce, status=REVIEWER_STATUS_SUCCEEDED,
        started_at=1.0, post_artifact=fingerprint_artifact(path), attempts=1,
    ))
    return path


def _skip_audit_row(*_args: Any, **_kwargs: Any) -> None:
    """Stands in for ``equipa.db.log_gate_audit`` (see below)."""


@contextlib.contextmanager
def audit_rows_not_persisted() -> Iterator[None]:
    """Run the gate without writing its GATE-AUDIT rows to the test database.

    Two rows per decision cost about 20 ms of sqlite commits, against under
    1 ms for the parse, and the suites decide tens of thousands of reviews.
    The persistence is fail-open by design (``_gate_audit_log``): it never
    changes a decision, and the stderr audit line is still emitted.
    """
    persist = equipa_db.log_gate_audit
    equipa_db.log_gate_audit = _skip_audit_row
    try:
        yield
    finally:
        equipa_db.log_gate_audit = persist


def production_decision(text: str, *, project_dir: Path | None = None,
                        task_id: int = GATE_TASK_ID,
                        nonce: str = NONCE) -> ProductionDecision:
    """``_security_review_blocks_merge`` on ``text`` as this cycle's review."""
    project = Path(project_dir) if project_dir is not None else gate_project()
    path = write_recorded_review(project, task_id, text, nonce=nonce)
    with audit_rows_not_persisted():
        blocks, counts = _security_review_blocks_merge(
            str(project), task_id, block_on_missing=True,
        )
    return ProductionDecision(blocks, counts,
                              verify_reviewer_provenance(task_id, path))


def gate_blocks(text: str, *, nonce: str = NONCE) -> bool:
    """The production merge decision on a review the reviewer could have
    written. Provenance must trust it, or reject it for a bidi control the
    text holds on purpose (production's own first check), so a block never
    comes from a test artifact missing its nonce or completion line."""
    decision = production_decision(text, nonce=nonce)
    if decision.provenance.trusted:
        return decision.blocks
    reason = decision.provenance.reason
    assert reason.startswith(REVIEW_BIDI_CONTROL_REASON), reason
    # Production refuses the bidi control before parsing; the suites also
    # held the parser to blocking these texts, and still do.
    return decision.blocks and parser_blocks(decision.provenance.text)


def parser_blocks(text: str) -> bool:
    """The merge rule applied to the parser alone (an untrusted review
    blocks; a trusted one blocks on CRITICAL or HIGH), for a text the
    production gate refuses before parsing it."""
    analysis = loops._analyze_review_file(
        Path(f"SECURITY-REVIEW-{GATE_TASK_ID}.md"), text=text)
    if not analysis.trusted:
        return True
    counts = analysis.counts or {}
    return counts.get("CRITICAL", 0) > 0 or counts.get("HIGH", 0) > 0


def decision_and_analysis(
    text: str, *, nonce: str = NONCE,
) -> tuple[ProductionDecision, loops.ReviewCountAnalysis]:
    """The production merge decision on ``text``, and the parser's analysis
    of the text provenance handed the gate (for assertions on the reason
    behind a verdict)."""
    decision = production_decision(text, nonce=nonce)
    assert decision.provenance.text is not None, decision.provenance.reason
    analysis = loops._analyze_review_file(
        Path(f"SECURITY-REVIEW-{GATE_TASK_ID}.md"),
        text=decision.provenance.text,
    )
    return decision, analysis


def blocked_by_the_gate(text: str, *,
                        nonce: str = NONCE) -> loops.ReviewCountAnalysis:
    """Assert that the merge gate blocks ``text`` on the parser's reading of
    it, and return that analysis for the asserts on the reason (task 3170,
    IR67-02: the suites' own must-block helpers decide here).

    Provenance must trust the review, or refuse it for a bidi control the
    text holds on purpose (production's first check, see ``gate_blocks``),
    so a block never comes from a test artifact missing its nonce or
    completion line. Callers assert the analysis blocks too."""
    decision, analysis = decision_and_analysis(text, nonce=nonce)
    if not decision.provenance.trusted:
        reason = decision.provenance.reason
        assert reason.startswith(REVIEW_BIDI_CONTROL_REASON), reason
    assert decision.blocks, (analysis.verdict, analysis.detail)
    return analysis


def review_as_written(path: Path) -> str:
    """The review at ``path`` as production reads it: its bytes decoded,
    with no newline translation (``Path.read_text`` turns a CR into a line
    feed, so a fixture holding one was decided on another text; R3167-02).
    """
    return Path(path).read_bytes().decode("utf-8")


def blocked_by_the_gate_at(path: Path) -> loops.ReviewCountAnalysis:
    """``blocked_by_the_gate`` on the review a test wrote at ``path``, read
    as written and made this cycle's reviewer artifact."""
    return blocked_by_the_gate(as_reviewer_artifact(review_as_written(path)))


# Task 3170 (IR67-02): finding lines only the review as written shows a
# severity in. Normalisation deletes the filler and glues "Rated" to the
# word, so with provenance handing the parser the normalised text (the
# R3161-01 regression) each merges behind a zero footer. Every must-block
# suite decides them through its own helpers, so that regression fails each
# suite, not only the ones whose shapes happen to hold such a character.
AS_WRITTEN_ONLY_FINDINGS = {
    "hangul-filler-high": ("Rated\N{HANGUL FILLER}HIGH remote code "
                           "execution in upload."),
    "halfwidth-filler-critical": ("Rated\N{HALFWIDTH HANGUL FILLER}CRITICAL: "
                                  "RCE in upload."),
}


def production_seconds(text: str, *, nonce: str = NONCE,
                       before: Callable[[], Any] | None = None) -> float:
    """Median CPU time of the merge gate on ``text`` (provenance, parse and
    audit line), the artifact written once beforehand; ``before`` (untimed)
    runs ahead of each call. Provenance must trust the artifact (or reject a
    bidi control the text holds on purpose, production's first check), so
    the time is never that of a malformed test artifact's early rejection.

    The gate refuses a bidi control before the parser runs, so for such a
    text the parser's time on the text provenance read is added: a family
    holding a bidi control (on purpose or from a code-point range) is still
    held to its budget for the parse."""
    project = gate_project()
    path = write_recorded_review(project, GATE_TASK_ID, text, nonce=nonce)
    provenance = verify_reviewer_provenance(GATE_TASK_ID, path)
    refused_before_parsing = (
        not provenance.trusted
        and provenance.reason.startswith(REVIEW_BIDI_CONTROL_REASON))
    assert provenance.trusted or refused_before_parsing, provenance.reason
    with audit_rows_not_persisted():
        gate_seconds = median_cpu_seconds(
            _security_review_blocks_merge, str(project), GATE_TASK_ID,
            block_on_missing=True, before=before,
        )
    if not refused_before_parsing:
        return gate_seconds
    return gate_seconds + median_cpu_seconds(
        loops._analyze_review_file, path, text=provenance.text, before=before,
    )
