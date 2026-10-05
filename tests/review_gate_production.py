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
from typing import Any, Iterator

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
    reason = decision.provenance.reason
    assert (decision.provenance.trusted
            or reason.startswith(REVIEW_BIDI_CONTROL_REASON)), reason
    return decision.blocks


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


def production_seconds(text: str, *, nonce: str = NONCE) -> float:
    """Median CPU time of the merge gate on ``text`` (provenance, parse and
    audit line), the artifact written once beforehand. Provenance must trust
    the artifact, so the time is never that of an early rejection."""
    project = gate_project()
    path = write_recorded_review(project, GATE_TASK_ID, text, nonce=nonce)
    provenance = verify_reviewer_provenance(GATE_TASK_ID, path)
    assert provenance.trusted, provenance.reason
    with audit_rows_not_persisted():
        return median_cpu_seconds(
            _security_review_blocks_merge, str(project), GATE_TASK_ID,
            block_on_missing=True,
        )
