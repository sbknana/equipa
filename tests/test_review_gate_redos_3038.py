"""Task #3038: the finding-candidate scan must stay linear on long lines.

``_FINDING_CANDIDATE_RE`` lost the strict header regex's 80-character cap
(S3033-02). Its unbracketed-tag branch was spelled ``\\d+[\\w-]*[^*\\n]*?``,
three overlapping quantifiers that backtracked cubically: one "**S1" line
with 1000 trailing digits took 17 s, so a review quoting a long number or
hash after a bold tag hung the security gate. The tag now stops at its first
digit and the lazy span covers the rest, which matches the same lines.

The sizes below make the old pattern exceed the bound by several times
(roughly 4 s and 8 s) while the linear one needs about a millisecond.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa.loops import (
    REVIEW_VERDICT_COUNT_MISMATCH,
    REVIEW_VERDICT_OK,
    _analyze_review_file,
    _count_findings_in_review_file,
)
from tests.review_gate_production import (
    as_reviewer_artifact,
    production_seconds,
)
from tests.review_gate_timing import timing_test

BODY = "# Security Review\n\n## Summary\nNo findings.\n\n## Findings\n\n"
ZERO_FOOTER = (
    "\n## Counts\nCRITICAL: 0 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0\n"
)
PARSE_BOUND_SECONDS = 1.0


def _write(tmp_path: Path, markdown: str) -> Path:
    path = tmp_path / "SECURITY-REVIEW-3038.md"
    path.write_text(markdown, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "adversarial_line",
    [
        "**S1" + "1" * 600,
        "**S1" + "a" * 20_000,
        "- **AB1" + "a-1_" * 5_000,
        "1. **RT-01" + "9" * 600 + "*",
    ],
    ids=["digits", "word-chars", "mixed-tag-chars", "numbered-digits"],
)
@timing_test
def test_long_bold_tag_line_parses_in_linear_time(
    tmp_path: Path, adversarial_line: str,
) -> None:
    path = _write(tmp_path, BODY + adversarial_line + "\n" + ZERO_FOOTER)

    analysis = _analyze_review_file(path)
    elapsed = production_seconds(
        as_reviewer_artifact(path.read_text(encoding="utf-8")))

    assert elapsed < PARSE_BOUND_SECONDS, f"parse took {elapsed:.2f}s"
    assert analysis.verdict == REVIEW_VERDICT_OK


@pytest.mark.parametrize(
    "candidate_line",
    [
        # Severity after a long tag tail is still seen.
        "**S1" + "1" * 600 + " HIGH** — nonce reuse",
        "- **SR29-00 HIGH** — duplicate key",
        "- **RT-01a_b-c (CRITICAL):** template injection",
        "2. **F12x HIGH — renderer SSRF**",
    ],
    ids=["long-tail", "hyphen-tag", "tag-suffix", "numbered"],
)
def test_unbracketed_tag_candidates_are_still_detected(
    tmp_path: Path, candidate_line: str,
) -> None:
    path = _write(tmp_path, BODY + candidate_line + "\n" + ZERO_FOOTER)

    assert _analyze_review_file(path).verdict == REVIEW_VERDICT_COUNT_MISMATCH
    assert _count_findings_in_review_file(path) is None
