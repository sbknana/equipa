"""A grown pair over the limit is judged on its growth exponent (task 3191).

CI (PR #44, run 37543697794, Python 3.10) failed
``test_sanitizer_3139::test_every_pattern_is_fast_on_one_megabyte_of_whitespace
[ws-sentence-end-newlines]``: the trust-boundary marker read 0.0320 s at
1 MB and 0.2605 s at 4 MB, 8.1x against the 8.0x limit on the GROWN pair,
confirmed in a majority of rounds (task 3188). The pattern is a linear
scan: its cost per byte steps up about 2x where the input outgrows a CPU
cache of the small runner. That is a constant factor, not growth, and two
sizes cannot tell it from superlinear work.

``tests/host_timing.py`` now reads a grown pair still over the limit once
confirmed at one more size, GROWTH times its larger one, and fails it only
when the growth exponent over the three sizes is superlinear too
(``_exponent_judged``): quadratic work grows GROWTH ** 2 on every step, a
cache step grows about GROWTH on the step past it. The test's own pair is
held exactly as before (IR78-01).

These tests pin that on modelled readings (CI's among them), cache steps of
2-3.5x per unit beside the same work made quadratic, cliffs of 4x and more,
and real work on the real clock.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import math
import time
from typing import Callable

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    EXPONENT_STEPS,
    GROWTH,
    GROWTH_EXPONENT_LIMIT,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    GrowthExponent,
    InputTooLarge,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
)
from tests.test_host_timing_3171 import _build_clock, timing_failure

# The CI run's test size, larger reading and the step its 4 MB reading took.
CI_SIZE = 1_000_000
CI_LARGE_SECONDS = 0.0320
CI_GROWN_SECONDS = 0.2605
CI_STEP = CI_GROWN_SECONDS / (GROWTH * CI_LARGE_SECONDS)
BUDGET_SECONDS = 10.0


def _stepped(reading: float, size: int, step_from: int, step: float,
             power: int = 1, power_from: int = 0,
             calls: list[int] | None = None) -> Callable[[int], float]:
    """Seconds at ``n`` of work reading ``reading`` at ``size``: growing as
    ``n ** power`` past ``power_from`` and linearly below it, its cost per
    unit ``step`` times higher past ``step_from`` (a cache it outgrows)."""
    def work(n: int) -> float:
        if n <= power_from:
            return n / size
        return power_from / size * (n / power_from) ** power if power_from \
            else (n / size) ** power

    def seconds_at(n: int) -> float:
        if calls is not None:
            calls.append(n)
        return reading * work(n) * (step if n > step_from else 1.0)

    return seconds_at


def _before_3191(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before task 3191: a grown pair still over the limit once
    confirmed fails on its own two sizes."""
    def pair_alone(grown, measure, pair, small, large, over):
        return list(over), {}

    monkeypatch.setattr(host_timing, "_exponent_judged", pair_alone)


def _first_grown_quarter(size: int, reading: float) -> int:
    """The quarter size of the first grown pair of work reading ``reading``
    at ``size`` (``next_quarter_size``)."""
    return host_timing.next_quarter_size(
        size, reading, size // GROWTH * host_timing.MAX_INPUT_GROWTH)


# --- The rule ----------------------------------------------------------------


def test_the_exponent_limit_is_the_growth_limits_own():
    assert GROWTH_EXPONENT_LIMIT == pytest.approx(1.5)
    assert GROWTH ** GROWTH_EXPONENT_LIMIT == pytest.approx(GROWTH_LIMIT)
    outer = host_timing._Pair(1, GROWTH ** EXPONENT_STEPS, 1.0,
                              GROWTH_FLOOR_SECONDS, steps=EXPONENT_STEPS)
    assert outer.limit == GROWTH_LIMIT ** 2
    assert outer.borderline == host_timing.BORDERLINE_GROWTH ** 2
    # One step, as every pair before task 3191.
    pair = host_timing._Pair(1, GROWTH, 1.0, GROWTH_FLOOR_SECONDS)
    assert (pair.limit, pair.borderline) == (GROWTH_LIMIT,
                                             host_timing.BORDERLINE_GROWTH)


@pytest.mark.parametrize("middle, larger, exponent", (
    (0.32, 5.12, 2.0),    # quadratic: 16x on each step
    (0.16, 1.28, 1.5),    # 8x on each step: the limit on both
    (0.24, 0.96, 1.396),  # a 3x cache step, then linear
    (0.08, 0.32, 1.0),    # linear
))
def test_the_exponent_is_the_slope_over_the_outer_pair(middle, larger,
                                                       exponent):
    """For sizes GROWTH apart the least-squares slope of log(seconds)
    against log(size) is the outer pair's: the middle reading only says
    where the step fell."""
    judged = GrowthExponent(1000, 0.02, 4000, middle, 16000, larger,
                            GROWTH_FLOOR_SECONDS, False)
    assert judged.exponent == pytest.approx(exponent, abs=0.001)
    xs = [math.log(size) for size in (1000, 4000, 16000)]
    ys = [math.log(seconds) for seconds in (0.02, middle, larger)]
    mean_x, mean_y = sum(xs) / 3, sum(ys) / 3
    least_squares = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
                     / sum((x - mean_x) ** 2 for x in xs))
    assert judged.exponent == pytest.approx(least_squares)


def test_the_exponent_raises_a_quarter_under_the_floor_to_it():
    """As every pair's ratio: a raised quarter can only lower the exponent,
    which is why grown pairs read their third size above the floor."""
    judged = GrowthExponent(1000, 0.005, 4000, 0.16, 16000, 1.28,
                            GROWTH_FLOOR_SECONDS, False)
    assert judged.exponent == pytest.approx(1.5)
    assert "1000, 4000 and 16000" in judged.describe()
    assert "a constant-factor step, not growth" in judged.describe()


# --- CI's readings -----------------------------------------------------------


@pytest.mark.parametrize("judged", (True, False), ids=["3191", "before-3191"])
def test_the_ci_readings_pass_on_their_growth_exponent(monkeypatch, judged):
    """CI's pair (0.0320 s at 1 MB, 0.2605 s at 4 MB, 8.1x) over the limit
    in every confirming round, linear past it (1.042 s at 16 MB): the check
    before task 3191 fails it with CI's message; its exponent over the
    three sizes is 1.26, and it passes."""
    _build_clock(monkeypatch)
    if not judged:
        _before_3191(monkeypatch)
    calls: list[int] = []
    seconds_at = _stepped(CI_LARGE_SECONDS, CI_SIZE, CI_SIZE, CI_STEP,
                          calls=calls)
    failure = timing_failure(lambda: assert_linear_time(
        seconds_at, CI_SIZE, BUDGET_SECONDS, "trust-boundary marker"))
    if not judged:
        assert failure is not None
        assert ("0.2605 s at size 4000000, 0.0320 s at size 1000000 "
                "(growth 8.1x at 4x the test's input" in str(failure))
        assert max(calls) == GROWTH * CI_SIZE
        return
    assert failure is None, str(failure)
    timing = assert_linear_time(seconds_at, CI_SIZE, BUDGET_SECONDS,
                                "trust-boundary marker")
    assert (timing.small_size, timing.size) == (CI_SIZE, GROWTH * CI_SIZE)
    assert timing.ratio >= GROWTH_LIMIT
    assert timing.confirmation is not None and timing.confirmation.confirmed
    assert timing.exponent is not None and not timing.exponent.superlinear
    assert timing.exponent.larger_size == GROWTH ** 2 * CI_SIZE
    assert timing.exponent.exponent == pytest.approx(1.256, abs=0.001)
    assert "a constant-factor step, not growth" in timing.describe()
    # One size past the pair, never further.
    assert max(calls) == GROWTH ** 2 * CI_SIZE


def test_the_ci_readings_pass_beside_every_other_pattern(monkeypatch):
    """The real test times every pattern in one probe run: the stepped
    pattern passes on its exponent, the others are not judged on one."""
    _build_clock(monkeypatch)
    stepped = _stepped(CI_LARGE_SECONDS, CI_SIZE, CI_SIZE, CI_STEP)
    timings = assert_linear_times(
        lambda size: {"trust-boundary marker": stepped(size),
                      "encoded payload": 0.0977 * size / CI_SIZE,
                      "dangerous command": 0.0402 * size / CI_SIZE},
        CI_SIZE, BUDGET_SECONDS, "1 MB of ws-sentence-end-newlines")
    assert timings["trust-boundary marker"].exponent is not None
    assert not timings["trust-boundary marker"].exponent.superlinear
    assert timings["dangerous command"].exponent is None
    assert timings["dangerous command"].ratio < GROWTH_LIMIT


def test_a_cleared_pair_is_confirmed_once(monkeypatch):
    """The pair the growth stopped at is the cleared pair again: it is not
    confirmed, nor its exponent read, a second time."""
    _build_clock(monkeypatch)
    counts = {"confirmed": 0, "judged": 0}
    confirmed, judged = host_timing._confirmed, host_timing._exponent_judged

    def counting_confirmed(*args, **kwargs):
        counts["confirmed"] += 1
        return confirmed(*args, **kwargs)

    def counting_judged(*args, **kwargs):
        counts["judged"] += 1
        return judged(*args, **kwargs)

    monkeypatch.setattr(host_timing, "_confirmed", counting_confirmed)
    monkeypatch.setattr(host_timing, "_exponent_judged", counting_judged)
    assert_linear_time(_stepped(CI_LARGE_SECONDS, CI_SIZE, CI_SIZE, CI_STEP),
                       CI_SIZE, BUDGET_SECONDS, "trust-boundary marker")
    # The grown pair's rounds; the outer pair, borderline, settles unconfirmed.
    assert counts == {"confirmed": 1, "judged": 1}


# --- Cache steps against the same work made superlinear ----------------------

# Readings at the test's size that grow the input (a quarter under the
# 20 ms floor, a larger reading of at least 16 ms) to a first grown pair
# whose quarter reads the floor or more.
GROWING_READINGS = (0.016, 0.03, 0.06)
CACHE_STEPS = (2.0, 2.5, 3.0, 3.5)
SIZE = 200_000


def _step_inside_the_first_grown_pair(reading: float) -> int:
    quarter = _first_grown_quarter(SIZE, reading)
    return GROWTH // 2 * quarter


@pytest.mark.parametrize("step", CACHE_STEPS)
@pytest.mark.parametrize("reading", GROWING_READINGS)
def test_linear_work_with_a_cache_step_passes(monkeypatch, reading, step):
    """Linear work whose cost per unit steps up ``step`` times inside the
    first grown pair: over the limit there (GROWTH times the step), and
    failed by the check before task 3191; GROWTH on the step past it, so
    its exponent stays under 1.5 and it passes."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(reading)
    calls: list[int] = []
    timing = assert_linear_time(
        _stepped(reading, SIZE, step_from, step, calls=calls), SIZE,
        BUDGET_SECONDS, "cache step")
    assert timing.exponent is not None and not timing.exponent.superlinear
    assert timing.exponent.exponent < GROWTH_EXPONENT_LIMIT
    assert max(calls) == GROWTH * timing.size
    _before_3191(monkeypatch)
    failure = timing_failure(lambda: assert_linear_time(
        _stepped(reading, SIZE, step_from, step), SIZE, BUDGET_SECONDS,
        "cache step"))
    assert failure is not None and "superlinear growth" in str(failure)


@pytest.mark.parametrize("step", CACHE_STEPS)
@pytest.mark.parametrize("reading", GROWING_READINGS)
def test_the_same_work_made_quadratic_fails(monkeypatch, reading, step):
    """The same cache step on quadratic work fails at the test's own pair,
    as before (IR78-01), never reading past the test's size; quadratic only
    past the test's size, it fails at the first grown pair on its exponent
    (16x on every step, times the cache step)."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(reading)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: quadratic: .* at 1x the "
                             r"test's input"):
        assert_linear_time(
            _stepped(reading, SIZE, step_from, step, power=2, calls=calls),
            SIZE, BUDGET_SECONDS, "quadratic")
    assert max(calls) == SIZE
    calls.clear()
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: late quadratic: .*growth "
                             r"exponent .*: superlinear") as failure:
        assert_linear_time(
            _stepped(reading, SIZE, step_from, step, power=2,
                     power_from=SIZE, calls=calls),
            SIZE, BUDGET_SECONDS, "late quadratic")
    assert "at 1x the test's input" not in str(failure.value)
    first_grown_size = GROWTH * _first_grown_quarter(SIZE, reading)
    assert max(calls) == GROWTH * first_grown_size


@pytest.mark.parametrize("step", (4.0, 8.0, 40.0))
def test_a_step_of_growth_times_or_more_per_unit_is_a_cliff(monkeypatch,
                                                           step):
    """A cost per unit GROWTH times higher past a size reads GROWTH ** 2
    over the pair it falls in, as quadratic work does: no cache does that,
    and the exponent (at least 1.5) fails it."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(0.03)
    with pytest.raises(TimingCheckFailed,
                       match=r"growth exponent .*: superlinear"):
        assert_linear_time(_stepped(0.03, SIZE, step_from, step), SIZE,
                           BUDGET_SECONDS, "cliff")


def test_a_cache_step_beside_a_quadratic_part_names_only_the_quadratic(
        monkeypatch):
    """One call times both parts: both are over the limit at the first
    grown pair; only the part superlinear over three sizes fails."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(0.03)
    cached = _stepped(0.03, SIZE, step_from, 3.0)
    late = _stepped(0.03, SIZE, step_from, 1.0, power=2, power_from=SIZE)
    with pytest.raises(TimingCheckFailed, match="parts: late") as failure:
        assert_linear_times(lambda size: {"cached": cached(size),
                                          "late": late(size)},
                            SIZE, BUDGET_SECONDS, "parts")
    assert "parts: cached" not in str(failure.value)


def test_a_burst_on_the_third_size_is_settled(monkeypatch):
    """The third size's first reading inflated 2x (a span of 96x, over the
    outer limit) is read again, interleaved, and the fastest reading
    decides: the cache step passes."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(0.03)
    quiet = _stepped(0.03, SIZE, step_from, 3.0)
    quarter = _first_grown_quarter(SIZE, 0.03)
    third = GROWTH ** 2 * quarter
    burst = {"left": 1}

    def seconds_at(size: int) -> float:
        if size == third and burst["left"]:
            burst["left"] -= 1
            return 2 * quiet(size)
        return quiet(size)

    timing = assert_linear_time(seconds_at, SIZE, BUDGET_SECONDS, "burst")
    assert timing.exponent is not None
    assert timing.exponent.larger_seconds == pytest.approx(quiet(third))
    assert not burst["left"]


# --- The test's own pair, and no third size ----------------------------------


def test_a_cache_step_inside_the_tests_own_pair_fails_as_before(monkeypatch):
    """The own pair is held exactly as before (IR78-01): a 3x step inside it
    reads 12x against main's 2 ms floor and fails there, never read past
    the test's size or judged on an exponent."""
    _build_clock(monkeypatch)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"at 1x the test's input") as failure:
        assert_linear_time(
            _stepped(0.12, SIZE, SIZE // 2, 3.0, calls=calls), SIZE,
            BUDGET_SECONDS, "own pair step")
    assert "growth exponent" not in str(failure.value)
    assert max(calls) == SIZE


def test_without_a_third_size_the_pair_decides_alone(monkeypatch):
    """Where the shape does not exist at the third size (``InputTooLarge``)
    no exponent is measured: CI's pair fails on its own sizes, as before."""
    _build_clock(monkeypatch)
    stepped = _stepped(CI_LARGE_SECONDS, CI_SIZE, CI_SIZE, CI_STEP)

    def seconds_at(size: int) -> float:
        if size > GROWTH * CI_SIZE:
            raise InputTooLarge(f"size {size} is over {GROWTH * CI_SIZE}")
        return stepped(size)

    with pytest.raises(TimingCheckFailed,
                       match=r"growth 8\.1x at 4x the test's input") as failure:
        assert_linear_time(seconds_at, CI_SIZE, BUDGET_SECONDS,
                           "trust-boundary marker")
    assert "growth exponent" not in str(failure.value)
    assert (f"confirmed: over the limit in {CONFIRM_MAJORITY} of "
            f"{CONFIRM_MAJORITY} confirming rounds" in str(failure.value))


# --- Real work on the real clock -----------------------------------------------


def _linear_work(units: int) -> int:
    total = 0
    for value in range(units):
        total ^= value
    return total


def _linear_units(target_seconds: float) -> int:
    """Units of ``_linear_work`` reading about ``target_seconds`` here."""
    units = 100_000
    for _ in range(4):
        with host_timing.collector_paused():
            started = time.process_time()
            _linear_work(units)
            reading = time.process_time() - started
        units = max(GROWTH, round(units * target_seconds / max(reading, 1e-6)))
    return units


def _real_stepped(step_from: int, step: int) -> Callable[[int], float]:
    """CPU seconds of real linear work, each unit ``step`` times as costly
    past ``step_from`` units (the loop run ``step`` times): a cache step."""
    def seconds_at(units: int) -> float:
        started = time.process_time()
        for _ in range(step if units > step_from else 1):
            _linear_work(units)
        return time.process_time() - started

    return seconds_at


@pytest.mark.parametrize("judged", (True, False), ids=["3191", "before-3191"])
def test_real_linear_work_with_a_3x_step_passes(monkeypatch, judged):
    """Real linear work reading about 25 ms at the test's size whose cost
    per unit triples past twice the test's size: 12x over the first grown
    pair (the check before task 3191 fails it), 4x past it."""
    units = _linear_units(0.025)
    if not judged:
        _before_3191(monkeypatch)
    failure = timing_failure(lambda: assert_linear_time(
        _real_stepped(2 * units, 3), units, 30.0, "real cache step"))
    if judged:
        assert failure is None, str(failure)
    else:
        assert failure is not None and "superlinear growth" in str(failure)
