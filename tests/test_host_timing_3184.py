"""Growth checks hold under CPU contention (task 3184).

During a full parallel suite at host load about 20, a quadratic regression
planted in ``sanitize`` (``test_host_timing_3180.py``) read 0.0228 s at
15,000 characters and 0.1486 s at 60,000: 6.5x, under the 8x limit, and it
passed its real test (alone it failed 5/5). Contention had inflated the
quarter reading, and a ratio under the limit was decided on one reading of
each size: only a ratio OVER the limit was measured again.

``tests/host_timing.py`` now measures a borderline pair again
(``BORDERLINE_GROWTH`` up to the limit, on a quarter reading over the
pair's floor) ``BORDERLINE_RETRIES`` times, the quarter size and the larger
one interleaved, and decides it on each size's fastest reading; the
repetitions of the test's own pair are interleaved too. These tests pin
that on scripted readings and on real quadratic work on the real clock.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import time
from typing import Callable, Mapping, Sequence

import pytest

from tests import host_timing
from tests.host_timing import (
    BORDERLINE_GROWTH,
    BORDERLINE_RETRIES,
    GROWTH,
    GROWTH_LIMIT,
    GROWTH_RETRIES,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
)
from tests.test_host_timing_3171 import (
    _pair_work,
    _units_reading,
    planted_band,
    timing_failure,
)

SIZE = 60_000
QUARTER = SIZE // GROWTH
# What CI read under load (the quarter inflated), then what the same
# planted regression reads on a quieter run of each size: 9.3x.
LOADED_QUARTER, LOADED_LARGE = 0.0228, 0.1486
QUIET_QUARTER, QUIET_LARGE = 0.0150, 0.1400


def _scripted(readings: Mapping[int, Sequence[float]],
              calls: list[int] | None = None) -> Callable[[int], float]:
    """A ``seconds_at`` reading each size's values in turn, the last one
    from then on."""
    pending = {size: list(values) for size, values in readings.items()}

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        values = pending[size]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds_at


def test_the_band_lies_between_linear_and_quadratic_growth():
    assert GROWTH < BORDERLINE_GROWTH < GROWTH_LIMIT < GROWTH ** 2
    assert BORDERLINE_RETRIES > GROWTH_RETRIES
    assert LOADED_LARGE / LOADED_QUARTER < GROWTH_LIMIT
    assert QUIET_LARGE / QUIET_QUARTER >= GROWTH_LIMIT


def test_the_regression_ci_read_under_load_fails_on_its_fastest_readings():
    """The readings of the failed CI run: 6.5x on one reading of each size
    (main passed it). Measured again, the quieter quarter reading shows the
    9.3x the regression grows."""
    calls: list[int] = []
    seconds_at = _scripted({QUARTER: (LOADED_QUARTER, QUIET_QUARTER),
                            SIZE: (LOADED_LARGE, QUIET_LARGE)}, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, "planted sanitize")
    message = str(failure.value)
    assert "at 1x the test's input" in message
    assert (f"each the fastest of {1 + BORDERLINE_RETRIES} interleaved "
            f"readings" in message)
    assert f"{QUIET_QUARTER:.4f} s at size {QUARTER}" in message
    # Each size measured again, the quarter first, the two alternating.
    assert calls == [QUARTER, SIZE] * (1 + BORDERLINE_RETRIES)


def test_a_part_hidden_by_a_loaded_quarter_fails_beside_a_linear_part():
    """``assert_linear_times``: one call times every part, so every part is
    measured again; only the superlinear part is named."""
    calls: list[int] = []
    hidden = _scripted({QUARTER: (LOADED_QUARTER, QUIET_QUARTER),
                        SIZE: (LOADED_LARGE, QUIET_LARGE)}, calls)

    def seconds_at(size: int) -> dict[str, float]:
        # The linear part's quarter reads the floor: no part grows the input.
        return {"linear": 0.08 * size / SIZE, "hidden": hidden(size)}

    with pytest.raises(TimingCheckFailed, match="parts: hidden") as failure:
        assert_linear_times(seconds_at, SIZE, 10.0, "parts")
    assert "parts: linear" not in str(failure.value)
    assert calls == [QUARTER, SIZE] * (1 + BORDERLINE_RETRIES)


@pytest.mark.parametrize("larger_readings", (
    (0.17, 0.12),           # borderline (5.7x), then 4x
    (0.4, 0.17, 0.12),      # over the limit, then borderline, then 4x
))
def test_linear_work_read_borderline_passes_on_its_fastest_readings(
        larger_readings):
    """A burst on the larger size pushes linear work into the band (or over
    the limit and then into it); its fastest readings grow 4x."""
    calls: list[int] = []
    timing = assert_linear_time(
        _scripted({QUARTER: (0.03,), SIZE: larger_readings}, calls), SIZE,
        10.0, "linear")
    assert timing.seconds == pytest.approx(0.12)
    assert timing.ratio == pytest.approx(4.0)
    assert timing.samples == 1 + BORDERLINE_RETRIES
    assert "fastest of 5 interleaved readings" in timing.describe()
    assert calls == [QUARTER, SIZE] * (1 + BORDERLINE_RETRIES)


def test_linear_work_out_of_the_band_is_measured_once():
    calls: list[int] = []
    timing = assert_linear_time(
        _scripted({QUARTER: (0.03,), SIZE: (0.149,)}, calls), SIZE, 10.0,
        "linear")
    assert timing.ratio < BORDERLINE_GROWTH
    assert timing.samples == 1
    assert calls == [QUARTER, SIZE]


def test_quadratic_work_over_the_limit_is_measured_again_as_often_as_before():
    """A regression over the limit from its first readings costs what it
    did on main: GROWTH_RETRIES more readings of each size."""
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(_scripted({QUARTER: (0.03,), SIZE: (0.48,)}, calls),
                           SIZE, 10.0, "quadratic")
    assert calls == [QUARTER, SIZE] * (1 + GROWTH_RETRIES)


def test_a_ratio_against_the_floor_is_not_measured_again():
    """A quarter reading under the pair's floor counts as the floor: another
    reading can lower the ratio but never raise it, so a borderline ratio
    on it is decided as read (main's floor, as before)."""
    calls: list[int] = []
    small = host_timing.DETECTION_FLOOR_SECONDS * 0.75
    larger = host_timing.DETECTION_FLOOR_SECONDS * 5.5
    assert BORDERLINE_GROWTH <= larger / host_timing.DETECTION_FLOOR_SECONDS
    # Not repeated either (b81777b): repeated, it could not reach the limit.
    assert host_timing.growth_repetitions(small, 0.0, larger) == 1
    timing = assert_linear_time(
        _scripted({QUARTER: (small,), SIZE: (larger,)}, calls), SIZE, 10.0,
        "cheap")
    assert timing.samples == 1
    assert calls == [QUARTER, SIZE]


def test_the_own_pairs_repetitions_alternate_between_the_sizes():
    """b81777b's repetitions (a 5 ms quarter reading, measured until the
    quarter totals the floor) ran every quarter call, then every larger
    one: a burst then fell on one size's whole run."""
    calls: list[int] = []
    repetitions = host_timing.growth_repetitions(0.005, 0.0, 0.08)
    assert repetitions == 4
    with pytest.raises(TimingCheckFailed,
                       match=r"over 4 runs of each") as failure:
        assert_linear_time(
            _scripted({QUARTER: (0.005,), SIZE: (0.08,)}, calls), SIZE,
            10.0, "cheap quadratic")
    assert calls == [QUARTER, SIZE] * (repetitions * (1 + GROWTH_RETRIES))
    assert "each the fastest of 3 interleaved readings" in str(failure.value)


# A quarter reading under the 20 ms floor and a larger one over it grow the
# input 4x (task 3178): the grown pair is (SIZE, GROWN), its quarter reading
# the one the test's own pair took at SIZE.
GROWN = SIZE * GROWTH
GROWN_PAIR_CALLS = [QUARTER, SIZE, GROWN] + [SIZE, GROWN] * BORDERLINE_RETRIES


def test_a_grown_pair_hidden_by_a_loaded_quarter_fails():
    """Linear at the test's sizes, superlinear past them: the reading at
    SIZE that the grown pair reuses was taken under load (0.04 s, quietly
    0.025 s), so the grown pair reads 6.5x once (main passed it) and
    10.4x on each size's fastest reading."""
    calls: list[int] = []
    seconds_at = _scripted({QUARTER: (0.010,), SIZE: (0.040, 0.025),
                            GROWN: (0.260,)}, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, "grown")
    message = str(failure.value)
    assert f"0.0250 s at size {SIZE}" in message
    assert "at 4x the test's input" in message
    assert (f"each the fastest of {1 + BORDERLINE_RETRIES} interleaved "
            f"readings" in message)
    assert calls == GROWN_PAIR_CALLS


def test_a_settled_grown_pair_is_not_measured_again_where_growth_stops():
    """Linear work whose first grown reading a burst pushed into the band
    passes on its fastest readings, and the pair the growth stopped at (the
    same sizes) starts from them (``_GrownReadings.settle``) instead of
    reading the burst again and measuring both sizes another
    ``BORDERLINE_RETRIES`` times."""
    calls: list[int] = []
    timing = assert_linear_time(
        _scripted({QUARTER: (0.010,), SIZE: (0.040,), GROWN: (0.250, 0.160)},
                  calls), SIZE, 10.0, "grown linear")
    assert (timing.small_size, timing.size) == (SIZE, GROWN)
    assert timing.input_growth == GROWTH
    assert timing.seconds == pytest.approx(0.160)
    assert timing.ratio == pytest.approx(4.0)
    assert timing.samples == 1 + BORDERLINE_RETRIES
    assert calls == GROWN_PAIR_CALLS


# --- Real work on the real clock -------------------------------------------------


def _spin_cpu_until(started: float, seconds: float) -> None:
    """Spend CPU time until ``seconds`` have passed since ``started``
    (``time.process_time``): a burst of contention on one reading."""
    while time.process_time() - started < seconds:
        pass


# The planted reading at the test's size, and the first ratio the burst
# aims at (CI's 6.5x). The burst quarter reading (21.5 ms) is over the
# 20 ms floor, as CI's 22.8 ms was, so it is not repeated (b81777b) and
# one reading of each size decides it on main.
BURST_TARGET_SECONDS = 0.14
BURST_RATIO = 6.5


def test_real_quadratic_work_read_under_a_burst_fails():
    """Real quadratic work, its first quarter reading inflated by CPU time
    so that the first pair reads inside the band (as CI's did): one
    reading of each size passes it; the fastest of each fails it."""
    target_seconds = BURST_TARGET_SECONDS
    burst_seconds = target_seconds / BURST_RATIO
    assert burst_seconds > host_timing.GROWTH_FLOOR_SECONDS
    lower, upper = planted_band(target_seconds)
    for _ in range(3):
        units = _units_reading(target_seconds)
        quarter = units // GROWTH
        readings: dict[int, list[float]] = {}

        def seconds_at(size: int, quarter=quarter, readings=readings
                       ) -> float:
            started = time.process_time()
            _pair_work(size)
            if size == quarter and size not in readings:
                _spin_cpu_until(started, burst_seconds)
            elapsed = time.process_time() - started
            readings.setdefault(size, []).append(elapsed)
            return elapsed

        failure = timing_failure(lambda: assert_linear_time(
            seconds_at, units, 30.0, "quadratic under a burst"))
        planted = readings[units][0]
        first_ratio = planted / readings[quarter][0]
        if (lower <= planted <= upper
                and BORDERLINE_GROWTH <= first_ratio < GROWTH_LIMIT):
            break
    assert lower <= planted <= upper, (
        f"the planted reading {planted:.4f} s at {units} units missed the "
        f"{lower:.4f}-{upper:.4f} s band on this host")
    assert BORDERLINE_GROWTH <= first_ratio < GROWTH_LIMIT, (
        f"the burst did not put the first pair in the band: {readings}")
    assert failure is not None and "superlinear growth" in str(failure), (
        f"quadratic work read {first_ratio:.1f}x under a burst and passed: "
        f"{readings}")


def test_real_linear_work_read_under_a_burst_passes():
    """Real linear work, its first larger reading inflated into the band,
    passes on its fastest readings. About 160 ms at the test's size, so
    the quarter reading (about 40 ms) is well over the floor."""
    units = _units_reading(0.16) ** 2
    readings: dict[int, list[float]] = {}

    def seconds_at(size: int) -> float:
        started = time.process_time()
        total = 0
        for value in range(size):
            total ^= value
        if size == units and size not in readings:
            quarter_reading = readings[units // GROWTH][0]
            _spin_cpu_until(started, 6.5 * quarter_reading)
        elapsed = time.process_time() - started
        readings.setdefault(size, []).append(elapsed)
        return elapsed

    timing = assert_linear_time(seconds_at, units, 30.0, "linear under a burst")
    assert readings[units][0] / readings[units // GROWTH][0] >= 6.0
    # Both sizes were measured again before the pair passed. How often
    # depends on the load (the band's retries, or the limit's when the load
    # pushed the first ratio over it, and repetitions when a quarter reading
    # fell under the floor); the scripted tests above pin the counts.
    assert len(readings[units]) > 1
    assert len(readings[units // GROWTH]) > 1
    assert timing.ratio < GROWTH_LIMIT
