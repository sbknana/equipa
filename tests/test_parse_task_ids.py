"""Tests for equipa.dispatch.parse_task_ids range bounding.

Covers security finding EP-01: unbounded range expansion in
parse_task_ids could materialise ~1B ints (~8 GB RAM) given input
like "1-999999999". The fix bounds each range by MAX_TASK_RANGE.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import pytest

from equipa.constants import MAX_TASK_RANGE
from equipa.dispatch import parse_task_ids


def test_single_id() -> None:
    assert parse_task_ids("109") == [109]


def test_comma_separated_ids() -> None:
    assert parse_task_ids("109,110,111") == [109, 110, 111]


def test_small_range() -> None:
    assert parse_task_ids("109-114") == [109, 110, 111, 112, 113, 114]


def test_mixed_ids_and_range() -> None:
    assert parse_task_ids("109,112-114") == [109, 112, 113, 114]


def test_range_at_limit_is_allowed() -> None:
    """A range exactly equal to MAX_TASK_RANGE must be accepted."""
    result = parse_task_ids(f"1-{1 + MAX_TASK_RANGE}")
    assert len(result) == MAX_TASK_RANGE + 1
    assert result[0] == 1
    assert result[-1] == 1 + MAX_TASK_RANGE


def test_range_too_large_rejected() -> None:
    """The DoS payload from EP-01 must be rejected before expansion."""
    payload = "1-999999999"
    with pytest.raises(ValueError, match="task range too large"):
        parse_task_ids(payload)


def test_range_just_over_limit_rejected() -> None:
    payload = f"1-{2 + MAX_TASK_RANGE}"
    with pytest.raises(ValueError, match=f"max {MAX_TASK_RANGE}"):
        parse_task_ids(payload)


def test_oversize_range_in_compound_input_rejected() -> None:
    """An oversize range mixed with valid IDs is still rejected."""
    with pytest.raises(ValueError, match="task range too large"):
        parse_task_ids("5,1-9999999,7")
