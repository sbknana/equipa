"""Growth checks settle a pair whose quarter reading may be inflated, and
keep each size's FASTEST reading (task 3187: R3184-01, R3184-03).

R3184-01: task 3184 measured a pair again only when its first ratio read
``BORDERLINE_GROWTH`` (5x) or more. A burst that inflated the quarter
reading of a true 9.3x regression 1.9x read 4.9x, and the pair passed on one
reading of each size. A pair whose quarter reading is over its floor (and
whose larger reading could reach the limit against a quieter quarter) is
now read again (``SETTLE_RETRIES``) before it passes.

R3184-03: every scripted series of task 3184 ended on its quietest value
and every real-clock burst fell on the first reading, so keeping each
size's LAST reading passed every test. Here the burst falls on a later
reading.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

import pytest

from tests import host_timing
from tests.host_timing import (
    BORDERLINE_GROWTH,
    DETECTION_FLOOR_SECONDS,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    SETTLE_RETRIES,
    TimingCheckFailed,
    assert_linear_per_unit,
    assert_linear_time,
    assert_linear_times,
    settled_growth,
)

SIZE = 60_000
QUARTER = SIZE // GROWTH
# The quiet readings of the regression planted in ``sanitize`` (task 3184's
# scripted tests): 9.3x.
QUIET_QUARTER, QUIET_LARGE = 0.0150, 0.1400


def _series(readings: Mapping[int, Sequence[float]],
            calls: list[int] | None = None) -> Callable[[int], float]:
    """A ``seconds_at`` reading each size's values in turn, then repeating
    the LAST one (which need not be the fastest)."""
    pending = {size: list(values) for size, values in readings.items()}

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        values = pending[size]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds_at


def test_settling_is_one_more_reading_and_stays_under_the_band():
    assert SETTLE_RETRIES >= 1
    assert SETTLE_RETRIES < host_timing.BORDERLINE_RETRIES
    assert QUIET_LARGE / QUIET_QUARTER >= GROWTH_LIMIT


# --- R3184-01: a regression pushed under the band ------------------------------------


@pytest.mark.parametrize("inflation", (1.9, 2.0, 2.33, 3.0))
def test_a_regression_whose_inflated_quarter_reads_under_the_band_fails(
        inflation: float):
    """The reviewer's scripted case: the first quarter reading inflated
    1.9x reads 4.91x (2.0x: 4.67x, 2.33x: 4.0x, 3.0x: 3.1x). Read again,
    the quiet quarter shows the 9.3x the regression grows."""
    loaded_quarter = QUIET_QUARTER * inflation
    assert QUIET_LARGE / loaded_quarter < BORDERLINE_GROWTH
    calls: list[int] = []
    seconds_at = _series({QUARTER: (loaded_quarter, QUIET_QUARTER),
                          SIZE: (QUIET_LARGE,)}, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, "planted sanitize")
    assert f"{QUIET_QUARTER:.4f} s at size {QUARTER}" in str(failure.value)
    assert calls[:4] == [QUARTER, SIZE, QUARTER, SIZE]


def test_without_the_settling_reading_the_inflated_regression_passes(
        monkeypatch: pytest.MonkeyPatch):
    """The test above tells the two rules apart: with task 3184's rule (no
    reading again under the band) the same readings pass."""
    monkeypatch.setattr(host_timing, "SETTLE_RETRIES", 0)
    calls: list[int] = []
    timing = assert_linear_time(
        _series({QUARTER: (QUIET_QUARTER * 1.9, QUIET_QUARTER),
                 SIZE: (QUIET_LARGE,)}, calls), SIZE, 10.0, "planted sanitize")
    assert timing.ratio < BORDERLINE_GROWTH
    assert calls == [QUARTER, SIZE]


def test_a_part_hidden_under_the_band_fails_beside_a_linear_part():
    hidden = _series({QUARTER: (QUIET_QUARTER * 1.9, QUIET_QUARTER),
                      SIZE: (QUIET_LARGE,)})

    def seconds_at(size: int) -> dict[str, float]:
        return {"linear": 0.08 * size / SIZE, "hidden": hidden(size)}

    with pytest.raises(TimingCheckFailed, match="parts: hidden") as failure:
        assert_linear_times(seconds_at, SIZE, 10.0, "parts")
    assert "parts: linear" not in str(failure.value)


def test_a_scan_pair_hidden_under_the_band_is_settled():
    """``settled_growth`` (scans that time their own sizes) settles the
    same way: a 9.6x pair whose quarter reading read 2x reads 4.8x first."""
    quiet_quarter, quiet_large = 0.025, 0.240
    pair = settled_growth(
        _series({1_000: (quiet_quarter,), 4_000: (quiet_large,)}),
        1_000, 4_000, quiet_quarter * 2, quiet_large)
    # Settled once, then over the limit: GROWTH_RETRIES readings in all.
    assert pair.samples == 1 + max(SETTLE_RETRIES, host_timing.GROWTH_RETRIES)
    assert pair.ratio >= GROWTH_LIMIT


def test_linear_work_with_an_inflated_quarter_passes_on_its_fastest_readings():
    calls: list[int] = []
    timing = assert_linear_time(
        _series({QUARTER: (0.045, 0.03), SIZE: (0.12,)}, calls), SIZE, 10.0,
        "linear")
    assert timing.small_seconds == pytest.approx(0.03)
    assert timing.ratio == pytest.approx(4.0)
    assert timing.samples == 1 + SETTLE_RETRIES
    assert calls == [QUARTER, SIZE] * (1 + SETTLE_RETRIES)


def test_a_pair_that_cannot_reach_the_limit_is_not_read_again():
    """A larger reading under ``GROWTH_LIMIT`` floors stays under the limit
    whatever the quarter reads (it is raised to the floor), so reading
    again could not change the verdict: cheap shapes cost what they did."""
    larger = GROWTH_LIMIT * DETECTION_FLOOR_SECONDS * 0.95
    calls: list[int] = []
    timing = assert_linear_time(
        _series({QUARTER: (larger / 2,), SIZE: (larger,)}, calls), SIZE, 10.0,
        "cheap")
    assert timing.samples == 1
    assert calls == [QUARTER, SIZE]


def test_a_quarter_reading_under_the_floor_is_not_read_again():
    calls: list[int] = []
    pair = settled_growth(_series({1_000: (0.015,), 4_000: (0.06,)}, calls),
                          1_000, 4_000, 0.015, 0.06)
    assert pair.samples == 1
    assert calls == []
    assert pair.ratio == pytest.approx(0.06 / GROWTH_FLOOR_SECONDS)


# --- R3184-03: each size keeps its fastest reading -----------------------------------


# A 9.6x regression whose quarter reading is over the 20 ms floor (so the
# test's own pair is not repeated, b81777b), and the reading a burst makes
# of that quarter: 6x, inside the band.
LATE_QUIET_QUARTER, LATE_LARGE, LATE_BURST_QUARTER = 0.025, 0.240, 0.040


def test_a_burst_on_a_later_quarter_reading_does_not_hide_a_regression():
    """The quiet quarter reading comes FIRST; every later one is inflated
    (6x). Kept as the quarter's reading, the last one would pass the pair
    after its borderline readings; the fastest fails it."""
    calls: list[int] = []
    seconds_at = _series({QUARTER: (LATE_QUIET_QUARTER, LATE_BURST_QUARTER),
                          SIZE: (LATE_LARGE,)}, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, "late burst")
    assert f"{LATE_QUIET_QUARTER:.4f} s at size {QUARTER}" in str(failure.value)
    assert calls.count(QUARTER) > 1


def test_a_burst_on_a_later_larger_reading_does_not_fail_linear_work():
    """Linear work whose LATER larger readings are inflated (13x) passes on
    its first, fastest one."""
    timing = assert_linear_time(
        _series({QUARTER: (0.03,), SIZE: (0.12, 0.40)}), SIZE, 10.0,
        "late burst")
    assert timing.seconds == pytest.approx(0.12)
    assert timing.ratio == pytest.approx(4.0)
    assert timing.samples > 1


def test_scan_pairs_keep_each_sizes_fastest_reading():
    quarter_late_burst = settled_growth(
        _series({1_000: (LATE_BURST_QUARTER,), 4_000: (LATE_LARGE,)}),
        1_000, 4_000, LATE_QUIET_QUARTER, LATE_LARGE)
    assert quarter_late_burst.small_seconds == pytest.approx(LATE_QUIET_QUARTER)
    assert quarter_late_burst.ratio >= GROWTH_LIMIT
    # Linear (4x), its larger reading able to reach the limit against a
    # quieter quarter, so it is read again: the later readings are a burst.
    larger_late_burst = settled_growth(
        _series({1_000: (0.05,), 4_000: (0.50,)}), 1_000, 4_000, 0.05, 0.20)
    assert larger_late_burst.samples > 1
    assert larger_late_burst.seconds == pytest.approx(0.20)
    assert larger_late_burst.ratio < GROWTH_LIMIT


def test_per_unit_pairs_keep_each_sizes_fastest_reading():
    """``assert_linear_per_unit``: linear work whose later larger readings
    are inflated passes on its first one, and quadratic work whose later
    smaller readings are inflated fails on its first one."""
    settled = assert_linear_per_unit(
        _series({50: (0.10,), 200: (0.10, 0.30)}), [50, 200], 10.0,
        "late burst on the larger size")
    assert settled[200] == pytest.approx(0.10)
    with pytest.raises(TimingCheckFailed, match="superlinear per unit"):
        assert_linear_per_unit(
            _series({50: (0.10, 0.25), 200: (0.40,)}), [50, 200], 10.0,
            "late burst on the smaller size")
