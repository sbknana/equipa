#!/usr/bin/env python3
"""Task 3154: follow-ups of the task 3152 review-gate backstop.

Findings of SECURITY-REVIEW-3152 (R3152-*) and of the independent review of
task 3152 (I3152-*), each with rows that fail before this task:

* R3152-02: the seven R3149-02 review shapes with the MEDIUM word are
  untrusted again, as on main (ba6065a): "below" and a negation ending the
  line before no longer excuse a MEDIUM word;

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import pytest

from equipa import loops
from tests.test_review_gate_no_exemptions_3152 import (
    CONTEXTS,
    analyze,
    build_review,
    gate_blocks,
)

# --- R3152-02: MEDIUM keeps exactly main's exemptions ----------------------------

# The seven R3149-02 bodies with MEDIUM in place of CRITICAL or HIGH. Main
# blocks every one ("unaccounted severity token"); task 3152 trusted them
# with zero counts.
R3152_02_MEDIUM_BODIES = [
    ["The worst issue is described below MEDIUM severity stored XSS in "
     "app/view.py:7."],
    ["The worst issue is described below",
     "MEDIUM severity stored XSS in app/view.py:7."],
    ["Output encoding on the profile page: none",
     "MEDIUM, as any user stores script in app/view.py:7."],
    ["The profile form was reviewed. It was not",
     "MEDIUM. Stored XSS in /profile lets any user run script."],
    ["[a]: none",
     "MEDIUM issue was found in app/view.py:7 and stores script."],
    ["The profile handler was read.", "", "    no",
     "MEDIUM. Stored XSS in /profile lets any user run script."],
    ["The worst issue is described below  ",
     "MEDIUM issue was found in app/view.py:7."],
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", R3152_02_MEDIUM_BODIES)
def test_medium_r3149_02_shapes_are_untrusted_as_on_main(body, context):
    analysis = analyze(build_review(body, context))
    assert not analysis.trusted, analysis
    assert analysis.verdict == loops.REVIEW_VERDICT_COUNT_MISMATCH
    assert analysis.detail.startswith(loops.BACKSTOP_MEDIUM_REASON), (
        analysis.detail)
    assert gate_blocks(build_review(body, context))


# Negations main excused keep merging: an unaccounted MEDIUM is the defect,
# a reconciled one never blocks.
MEDIUM_NEGATIONS_THAT_MERGE = [
    ["The page needs a login, so this is not MEDIUM."],
    ["Kept at LOW rather than MEDIUM."],
    ["There are no MEDIUM findings."],
    ["The review found no MEDIUM issues in the diff."],
]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("body", MEDIUM_NEGATIONS_THAT_MERGE)
def test_reconciled_medium_negations_still_merge(body, context):
    analysis = analyze(build_review(body, context))
    assert analysis.trusted, analysis
    assert not gate_blocks(build_review(body, context))


def test_a_counted_medium_finding_does_not_block_the_merge():
    text = build_review(["MEDIUM issue: stored XSS in app/view.py:7."], "zero")
    text = text.replace("MEDIUM: 0 | LOW: 0", "MEDIUM: 1 | LOW: 0").replace(
        "## Findings\n", "## Findings\n### [M1] MEDIUM \N{EM DASH} stored XSS"
        "\nDetails.\n")
    analysis = analyze(text)
    assert analysis.trusted, analysis
    assert analysis.counts["MEDIUM"] == 1
    assert not gate_blocks(text)
