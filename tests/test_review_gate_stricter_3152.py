"""Task 3152 item 6: the review gate is strictly stricter.

tests/fixtures/review_gate_stricter_3152.json is written by
scripts/review_gate_differential.py --bodies --write-fixture. It holds every
must-block probe body of the 3130, 3137, 3143, 3149 and 3152 test modules and
of the reviewers' reports that an older tree (6f55944, main, forge-task-3149)
blocked, in a zero-finding review and after one counted LOW finding, with the
sha256 of each review text and the trees that blocked it. Nothing an older
tree blocked may merge here.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_review_gate_no_exemptions_3152 import build_review, gate_blocks

FIXTURE = Path(__file__).parent / "fixtures" / "review_gate_stricter_3152.json"
_DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = [
    (index, context, entry["body"], expected)
    for index, entry in enumerate(_DATA["entries"])
    for context, expected in sorted(entry["contexts"].items())
]


def test_fixture_covers_every_older_tree():
    assert {"6f55944", "main", "3149"} <= {
        tree["name"] if isinstance(tree, dict) else tree
        for tree in _DATA["trees"]}
    assert len(_DATA["entries"]) >= 400
    assert all(expected["blocked_by"] for *_, expected in CASES)


@pytest.mark.parametrize(
    "index,context,body,expected", CASES,
    ids=[f"{index}-{context}" for index, context, *_ in CASES])
def test_blocked_before_still_blocks(index, context, body, expected):
    text = build_review(body, context)
    # The text is byte-identical to the one the older trees judged.
    assert hashlib.sha256(text.encode()).hexdigest() == expected["sha256"]
    assert gate_blocks(text), (index, context, expected["blocked_by"], body)
