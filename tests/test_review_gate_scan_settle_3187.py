"""The gate-regex growth scans decide on settled readings (task 3187, IR84-02).

``scan_growth`` (tests/test_review_gate_linear_3167.py) chooses its quarter
size on a median that a burst can lift to the floor. Under contention the
reviewer read ``_BLANK_LINE_RUN_RE`` at 0.0123 s against 0.162 s, 8.1x, on
linear work: the settled quarter reading was under the floor, so the pair
straddled the regex's memory step and was decided against the floor. Such a
pair is no longer decided while the input can still double.

``superlinear_scan``'s first look passed a pair under ``BORDERLINE_GROWTH``
on one reading of each size, so a burst on the quarter scan hid quadratic
growth there (R3184-01's shape). It now reads both sizes once more,
interleaved, before it passes.

Readings are scripted by text length; nothing here times a real regex.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

import pytest

from tests import test_review_gate_linear_3167 as scans
from tests.host_timing import (
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    SETTLE_RETRIES,
    SettledGrowth,
)

PATTERN = re.compile(r"\n+")
UNIT = "\n"
COUNT = 1_000


def _scripted(readings: Mapping[int, Sequence[float]],
              calls: list[int]) -> Callable[..., float]:
    """A scan timer reading each text length's values in turn, then
    repeating the last one."""
    pending = {length: list(values) for length, values in readings.items()}

    def seconds(pattern, anchored, text, *args) -> float:
        calls.append(len(text))
        values = pending[len(text)]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds


def test_a_quarter_lifted_to_the_floor_by_a_burst_grows_the_input(
        monkeypatch: pytest.MonkeyPatch):
    """The reviewer's readings: the quarter size's selection median read
    the floor under a burst, its settled reading 0.0123 s, the larger size
    0.162 s past the memory step (8.1x against the floor). Twice the
    quarter, both sizes are past the step and grow 4x."""
    calls: list[int] = []
    monkeypatch.setattr(scans, "_median_scan_seconds", _scripted({
        COUNT: (0.021, 0.0123),
        COUNT * GROWTH: (0.162,),
        2 * COUNT: (0.026,),
        2 * COUNT * GROWTH: (0.104,),
    }, calls))

    growth = scans.scan_growth(PATTERN, False, "", UNIT, COUNT)

    assert growth.quarter_count == 2 * COUNT
    assert growth.quarter_seconds >= GROWTH_FLOOR_SECONDS
    assert growth.ratio == pytest.approx(4.0)
    assert growth.ratio < GROWTH_LIMIT


def test_quadratic_work_grown_past_a_low_quarter_still_fails(
        monkeypatch: pytest.MonkeyPatch):
    """Growing on never hides quadratic work: twice the input it still
    reads 16x."""
    calls: list[int] = []
    monkeypatch.setattr(scans, "_median_scan_seconds", _scripted({
        COUNT: (0.021, 0.015),
        COUNT * GROWTH: (0.240,),
        2 * COUNT: (0.060,),
        2 * COUNT * GROWTH: (0.960,),
    }, calls))

    growth = scans.scan_growth(PATTERN, False, "", UNIT, COUNT)

    assert growth.quarter_count == 2 * COUNT
    assert growth.ratio >= GROWTH_LIMIT


def test_a_pair_at_the_size_cap_is_decided_as_read(
        monkeypatch: pytest.MonkeyPatch):
    """At the cap the input cannot double: the pair is decided against the
    floor, as before."""
    # Doubling the quarter would put the larger text over the cap.
    monkeypatch.setattr(scans, "MAX_GROWTH_SCAN_CHARACTERS",
                        COUNT * GROWTH * 2 - 1)
    calls: list[int] = []
    monkeypatch.setattr(scans, "_median_scan_seconds", _scripted({
        COUNT: (0.0123,),
        COUNT * GROWTH: (0.162,),
    }, calls))

    growth = scans.scan_growth(PATTERN, False, "", UNIT, COUNT)

    assert growth.quarter_count == COUNT
    assert growth.ratio == pytest.approx(0.162 / GROWTH_FLOOR_SECONDS)
    assert 2 * COUNT not in calls


def test_a_quarter_settled_over_the_floor_is_decided_where_chosen(
        monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    monkeypatch.setattr(scans, "_median_scan_seconds", _scripted({
        COUNT: (0.025,),
        COUNT * GROWTH: (0.100,),
    }, calls))

    growth = scans.scan_growth(PATTERN, False, "", UNIT, COUNT)

    assert growth.quarter_count == COUNT
    assert growth.ratio == pytest.approx(4.0)
    assert 2 * COUNT not in calls


# --- superlinear_scan's first look ----------------------------------------------------


def _flagging_scan_growth(calls: list[int]):
    def scan_growth(pattern, anchored, prefix, unit, count):
        calls.append(count)
        return scans.ScanGrowth(count, SettledGrowth(
            count, 0.025, count * GROWTH, 0.400))
    return scan_growth


def test_a_first_look_hidden_by_an_inflated_quarter_is_decided_by_the_scan(
        monkeypatch: pytest.MonkeyPatch):
    """The best of three quarter scans read 8 ms under a burst (2x the
    larger scan's 16 ms, 2x, under the band); read again, the quarter takes
    1 ms, 16x: the pair goes to ``scan_growth``, which flags it."""
    scan_calls: list[int] = []
    decided: list[int] = []
    monkeypatch.setattr(scans, "_scan_seconds", _scripted({
        COUNT: (0.001,), COUNT * GROWTH: (0.016,)}, scan_calls))
    monkeypatch.setattr(scans, "scan_growth", _flagging_scan_growth(decided))

    growth = scans.superlinear_scan(PATTERN, False, "", UNIT, COUNT, 0.008)

    assert growth is not None and growth.ratio >= GROWTH_LIMIT
    assert decided == [COUNT]
    assert scan_calls == [COUNT * GROWTH] + [COUNT, COUNT * GROWTH] * SETTLE_RETRIES


def test_linear_work_under_the_band_passes_after_one_more_reading(
        monkeypatch: pytest.MonkeyPatch):
    scan_calls: list[int] = []
    decided: list[int] = []
    monkeypatch.setattr(scans, "_scan_seconds", _scripted({
        COUNT: (0.004,), COUNT * GROWTH: (0.016,)}, scan_calls))
    monkeypatch.setattr(scans, "scan_growth", _flagging_scan_growth(decided))

    assert scans.superlinear_scan(PATTERN, False, "", UNIT, COUNT, 0.004) is None
    assert decided == []
    assert scan_calls == [COUNT * GROWTH] + [COUNT, COUNT * GROWTH] * SETTLE_RETRIES


def test_a_first_look_in_the_band_goes_to_the_scan_at_once(
        monkeypatch: pytest.MonkeyPatch):
    scan_calls: list[int] = []
    decided: list[int] = []
    monkeypatch.setattr(scans, "_scan_seconds", _scripted({
        COUNT * GROWTH: (0.024,)}, scan_calls))
    monkeypatch.setattr(scans, "scan_growth", _flagging_scan_growth(decided))

    assert scans.superlinear_scan(PATTERN, False, "", UNIT, COUNT, 0.004)
    assert scan_calls == [COUNT * GROWTH]
    assert decided == [COUNT]
