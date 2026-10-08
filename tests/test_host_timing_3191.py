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
import re
import time
from dataclasses import dataclass
from typing import Callable

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    EXPONENT_MAX_FIRST_STEP,
    EXPONENT_MIN_SECOND_STEP,
    EXPONENT_SECONDS,
    EXPONENT_STEPS,
    GROWTH,
    GROWTH_EXPONENT_LIMIT,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    GROWTH_RETRIES,
    GrowthExponent,
    InputTooLarge,
    LinearTiming,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
    growth_ratio,
)
from tests.test_host_timing_3171 import _build_clock, timing_failure

# The CI run's test size, larger reading and the step its 4 MB reading took.
CI_SIZE = 1_000_000
CI_LARGE_SECONDS = 0.0320
CI_GROWN_SECONDS = 0.2605
CI_STEP = CI_GROWN_SECONDS / (GROWTH * CI_LARGE_SECONDS)
BUDGET_SECONDS = 10.0


def _stepped(reading: float, size: int, step_from: int, step: float,
             power: float = 1, power_from: int = 0,
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
    past the test's size, it fails at the first grown pair (16x times the
    cache step), never reading the size past it: no cache step reads a
    first step over ``EXPONENT_MAX_FIRST_STEP`` (R3191-01, R3191-03)."""
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
                       match=r"superlinear growth: late quadratic: ") as failure:
        assert_linear_time(
            _stepped(reading, SIZE, step_from, step, power=2,
                     power_from=SIZE, calls=calls),
            SIZE, BUDGET_SECONDS, "late quadratic")
    assert "at 1x the test's input" not in str(failure.value)
    assert "growth exponent" not in str(failure.value)
    first_grown_size = GROWTH * _first_grown_quarter(SIZE, reading)
    assert max(calls) == first_grown_size


@pytest.mark.parametrize("step", (4.0, 8.0, 40.0))
def test_a_step_of_growth_times_or_more_per_unit_is_a_cliff(monkeypatch,
                                                           step):
    """A cost per unit GROWTH times higher past a size reads GROWTH ** 2
    over the pair it falls in, as quadratic work does: no cache does that.
    Its first step is over ``EXPONENT_MAX_FIRST_STEP``, so it fails on the
    pair without the size past it being read (R3191-03)."""
    _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(0.03)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: cliff: ") as failure:
        assert_linear_time(_stepped(0.03, SIZE, step_from, step, calls=calls),
                           SIZE, BUDGET_SECONDS, "cliff")
    assert "growth exponent" not in str(failure.value)
    assert max(calls) == GROWTH * _first_grown_quarter(SIZE, 0.03)


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


# --- What reading the third size may cost --------------------------------------


def _taking_wall_time(clock, seconds_at: Callable[[int], float]
                      ) -> Callable[[int], float]:
    """``seconds_at`` whose every call also takes its reading of wall time
    on the scripted clock (``_build_clock``)."""
    def timed(size: int) -> float:
        seconds = seconds_at(size)
        clock.now += seconds
        return seconds

    return timed


def test_a_burst_on_a_third_size_of_seconds_is_settled_within_the_budget(
        monkeypatch):
    """CI's case on the wall clock: a third size reading 1.44 s (a 3x step
    past the pair), its first reading inflated 2x by a burst. Reading it
    again fits ``EXPONENT_SECONDS``, so the fastest reading decides and
    the cache step passes."""
    clock = _build_clock(monkeypatch)
    step_from = _step_inside_the_first_grown_pair(0.03)
    quiet = _stepped(0.03, SIZE, step_from, 3.0)
    third = GROWTH ** 2 * _first_grown_quarter(SIZE, 0.03)
    assert quiet(third) == pytest.approx(1.44)
    burst = {"left": 1}

    def seconds_at(size: int) -> float:
        if size == third and burst["left"]:
            burst["left"] -= 1
            return 2 * quiet(size)
        return quiet(size)

    timing = assert_linear_time(_taking_wall_time(clock, seconds_at), SIZE,
                                BUDGET_SECONDS, "burst")
    assert timing.exponent is not None and not timing.exponent.superlinear
    assert timing.exponent.larger_seconds == pytest.approx(quiet(third))


@pytest.mark.parametrize("budgeted", (True, False),
                         ids=["3191", "without-budget"])
def test_an_expensive_third_size_is_not_read_past_the_budget(monkeypatch,
                                                            budgeted):
    """Work growing as size ** 1.9 past the test's size reads 13.9x over
    the first grown pair (under ``EXPONENT_MAX_FIRST_STEP``, so its
    exponent is read: quadratic work's 16x is not, R3191-03) and 11.6 s at
    the third size: one more reading would end past ``EXPONENT_SECONDS``,
    so it is read once and the part fails closed with no confirming round;
    without the budget it is read again to settle and confirm."""
    clock = _build_clock(monkeypatch)
    if not budgeted:
        monkeypatch.setattr(host_timing, "EXPONENT_SECONDS", math.inf)
    calls: list[int] = []
    late = _stepped(0.06, SIZE, SIZE, 1.0, power=1.9, power_from=SIZE,
                    calls=calls)
    third = GROWTH ** 2 * _first_grown_quarter(SIZE, 0.06)
    assert late(GROWTH * SIZE) / late(SIZE) == pytest.approx(13.93, abs=0.01)
    assert late(third) == pytest.approx(11.64, abs=0.01)
    calls.clear()
    with pytest.raises(TimingCheckFailed,
                       match=r"growth exponent .*: superlinear") as failure:
        assert_linear_time(_taking_wall_time(clock, late), SIZE,
                           BUDGET_SECONDS, "late quadratic")
    if budgeted:
        assert calls.count(third) == 1
        assert ("no confirming round fitted the time left, so the pair "
                "stays over the limit (fail closed)" in str(failure.value))
    else:
        assert calls.count(third) > 1
        assert "fail closed" not in str(failure.value)


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


def _real_stepped(step_from: int, step: float,
                  quadratic_over: int = 0) -> Callable[[int], float]:
    """CPU seconds of real linear work, each unit ``step`` times as costly
    past ``step_from`` units (``step`` times the units run): a cache step.
    With ``quadratic_over`` the work at ``units`` is ``units ** 2 /
    quadratic_over`` units instead: the same step on quadratic work, which
    reads the same as the linear work at ``quadratic_over`` units."""
    def seconds_at(units: int) -> float:
        work = units * units // quadratic_over if quadratic_over else units
        if units > step_from:
            work = round(work * step)
        started = time.process_time()
        _linear_work(work)
        return time.process_time() - started

    return seconds_at


# Interleaved readings of each call length ``_host_step`` keeps the fastest of.
HOST_STEP_READINGS = 3


def _linear_seconds(units: int) -> float:
    """The CPU time of one ``_linear_work(units)`` call, collector paused."""
    with host_timing.collector_paused():
        started = time.process_time()
        _linear_work(units)
        return time.process_time() - started


def _host_step(units: int, step: float,
               seconds_of: Callable[[int], float] = _linear_seconds) -> float:
    """The step to plant past twice the test's size so that THIS host reads
    real linear work's cost per unit ``step`` times higher there (task
    3204): ``step`` over the ratio of the cost per unit this host reads
    over a stepped call at the first grown pair's larger size (``GROWTH *
    step`` times ``units``) to the one it reads over ``units``, each the
    fastest of HOST_STEP_READINGS interleaved readings, as the check keeps
    its readings. The CI runner of run 37729154851 read that ratio at
    1.28: three times the units read 15.4x over the first grown pair, a
    steeper step than the proof claims and over the 15x a cache step may
    read (R3191-01). ``seconds_of(count)`` reads the work over ``count``
    units (this host's CPU time by default)."""
    stepped_units = round(units * GROWTH * step)
    fastest = {units: math.inf, stepped_units: math.inf}
    for _ in range(HOST_STEP_READINGS):
        for count in fastest:
            fastest[count] = min(fastest[count], seconds_of(count))
    skew = (fastest[stepped_units] / stepped_units) / (fastest[units] / units)
    return step / skew


# --- The real cache step, measured (task 3208) ---------------------------------
#
# CI's runners read the planted step far steeper than ``_host_step`` sized it
# (12.7x-21x over the first grown pair, 3 of 3 runs of PR #44): a call of a
# few tens of ms runs whole inside a quiet spell that a longer one never
# fits, so the fastest of the many quarter readings the check takes reads
# under the fastest of the three ``_host_step`` takes. The proof now
# measures the step it planted, as the check reads it, and holds the
# check's verdict to what that measurement says.

# The test's size reads about this much: well over the GROWTH_DETECTION_
# SECONDS the input grows from, so the check reads past the step.
PROOF_TARGET_SECONDS = 0.03
# The fastest of how many readings of the quarter size the measurement
# keeps (the check reads it in its settling, confirming rounds and outer
# pair, 13-17 times), and of each larger size (the first reading and
# GROWTH_RETRIES, as the check settles them).
SHAPE_QUARTER_READINGS = 16
SHAPE_READINGS = 1 + GROWTH_RETRIES
# How far either way the measured steps are read for the verdicts they
# allow: the check reads the same work at other moments.
SHAPE_MARGIN = 1.15
# The first step the planted step is calibrated to read: the middle of the
# window a cache step is cleared in (over GROWTH_LIMIT, at most
# EXPONENT_MAX_FIRST_STEP), a step of about 2.7x per unit.
TARGET_FIRST_STEP = math.sqrt(GROWTH_LIMIT * EXPONENT_MAX_FIRST_STEP)
STEP_CALIBRATIONS = 4
# Plants of longer work when the check never read past the step.
PROOF_PLANTS = 3

PASSED = "passed"
PASSED_ON_ITS_EXPONENT = "passed on its exponent"
FAILED = "failed"


@dataclass(frozen=True)
class _MeasuredShape:
    """The planted work measured independently of the check: the fastest
    readings at a quarter size, GROWTH times it and (for the exponent)
    GROWTH ** 2 times it, each step against the GROWTH_FLOOR_SECONDS floor
    every grown pair is held to."""

    quarter_size: int
    quarter_seconds: float
    seconds: float
    larger_seconds: float | None = None

    @property
    def first_step(self) -> float:
        return growth_ratio(self.quarter_seconds, self.seconds)

    @property
    def second_step(self) -> float:
        if self.larger_seconds is None:
            return math.nan
        return growth_ratio(self.seconds, self.larger_seconds)

    @property
    def outer_step(self) -> float:
        if self.larger_seconds is None:
            return math.nan
        return growth_ratio(self.quarter_seconds, self.larger_seconds)

    def describe(self) -> str:
        larger = (f", {self.larger_seconds:.4f} s at "
                  f"{GROWTH ** 2 * self.quarter_size}; steps "
                  f"{self.first_step:.1f}x then {self.second_step:.1f}x, "
                  f"outer {self.outer_step:.1f}x"
                  if self.larger_seconds is not None
                  else f"; first step {self.first_step:.1f}x")
        return (f"measured {self.quarter_seconds:.4f} s at "
                f"{self.quarter_size}, {self.seconds:.4f} s at "
                f"{GROWTH * self.quarter_size}{larger}")


def _measured_shape(seconds_at: Callable[[int], float], quarter_size: int,
                    third: bool) -> _MeasuredShape:
    """The work ``seconds_at`` measured at ``quarter_size`` and GROWTH
    times it (and GROWTH ** 2 times it when ``third``), interleaved, each
    the fastest of as many readings as the check settles it on."""
    sizes = [GROWTH * quarter_size] + ([GROWTH ** 2 * quarter_size]
                                       if third else [])
    fastest = dict.fromkeys([quarter_size, *sizes], math.inf)
    with host_timing.collector_paused():
        for reading in range(SHAPE_QUARTER_READINGS):
            fastest[quarter_size] = min(fastest[quarter_size],
                                        seconds_at(quarter_size))
            if reading < SHAPE_READINGS:
                for size in sizes:
                    fastest[size] = min(fastest[size], seconds_at(size))
    return _MeasuredShape(quarter_size, fastest[quarter_size],
                          fastest[sizes[0]],
                          fastest[sizes[1]] if third else None)


def _calibrated_step(units: int) -> float:
    """The step to plant past twice ``units`` so that this host reads the
    first grown pair at about TARGET_FIRST_STEP: measured
    (``_measured_shape``) and rescaled at most STEP_CALIBRATIONS times
    until it reads inside the window by SHAPE_MARGIN. The step a runner
    keeps reading outside it is planted as last rescaled; the verdict is
    then held to what the measurement after the run says."""
    step = TARGET_FIRST_STEP / GROWTH
    for _ in range(STEP_CALIBRATIONS):
        first_step = _measured_shape(_real_stepped(2 * units, step), units,
                                     third=False).first_step
        if (GROWTH_LIMIT * SHAPE_MARGIN <= first_step
                <= EXPONENT_MAX_FIRST_STEP / SHAPE_MARGIN):
            break
        step *= TARGET_FIRST_STEP / first_step
    return step


def _expected_verdict(first_step: float, second_step: float,
                      outer_step: float, judged: bool,
                      clearing_seconds: float) -> str:
    """The verdict the check owes readings of these steps (R3191-01,
    R3191-03): a pair under the limit passes; over it, the check before
    task 3191 fails it, and the check after clears it on its exponent only
    when its steps are a cache step inside the window, the outer pair
    under its limit and the third size within EXPONENT_SECONDS
    (``clearing_seconds``, at this first step)."""
    if first_step < GROWTH_LIMIT:
        return PASSED
    if (not judged or first_step > EXPONENT_MAX_FIRST_STEP
            or second_step < EXPONENT_MIN_SECOND_STEP
            or outer_step >= GROWTH_LIMIT ** EXPONENT_STEPS
            or clearing_seconds > EXPONENT_SECONDS):
        return FAILED
    return PASSED_ON_ITS_EXPONENT


def _allowed_verdicts(shape: _MeasuredShape, judged: bool,
                      grown_call_seconds: float) -> set[str]:
    """Every verdict the measured shape owes with each of its steps read
    up to SHAPE_MARGIN either way: one verdict for a shape clear of every
    edge of the window, the verdicts on both sides of an edge it is
    near. ``grown_call_seconds`` is the wall time the check's first call
    at GROWTH times the quarter size took: the third size costs it about
    the outer limit over the first step (``_exponent_judged``)."""
    shares = (1 / SHAPE_MARGIN, 1.0, SHAPE_MARGIN)
    outer_limit = GROWTH_LIMIT ** EXPONENT_STEPS
    return {_expected_verdict(
                shape.first_step * first, shape.second_step * second,
                shape.outer_step * outer, judged,
                grown_call_seconds * outer_limit / (shape.first_step * first))
            for first in shares for second in shares for outer in shares}


def _check_verdict(check: Callable[[], LinearTiming]
                   ) -> tuple[str, LinearTiming | None, TimingCheckFailed | None]:
    """The verdict of ``check()``, its timing when it passed and its
    failure when it failed."""
    try:
        timing = check()
    except TimingCheckFailed as failure:
        return FAILED, None, failure
    verdict = PASSED if timing.exponent is None else PASSED_ON_ITS_EXPONENT
    return verdict, timing, None


def _assert_the_verdict_reads_the_window(timing: LinearTiming | None,
                                         failure: TimingCheckFailed | None,
                                         judged: bool,
                                         grown_call_seconds: float) -> None:
    """What holds of the check's verdict on this work whatever the runner
    reads: linear at the test's size, it never fails the test's own pair
    or its budget; a pass on an exponent read a cache step over the limit,
    inside the window, and any other pass a pair under the limit; a
    failure reads a grown pair over the limit and, ``judged``, gives the
    window's reason (an exponent read, a first step over
    EXPONENT_MAX_FIRST_STEP, or a third size costing over
    EXPONENT_SECONDS)."""
    if failure is None:
        assert timing is not None
        if timing.exponent is not None:
            assert timing.exponent.cache_step, timing.exponent.describe()
            assert not timing.exponent.superlinear, timing.exponent.describe()
            assert timing.exponent.first_step >= GROWTH_LIMIT, timing
        else:
            assert timing.ratio < GROWTH_LIMIT, timing.describe()
        return
    message = str(failure)
    assert message.startswith("superlinear growth: real cache step: "), message
    assert "at 1x the test's input" not in message, message
    if judged and "growth exponent" in message:
        return
    read = re.search(r"\(growth ([\d.]+)x at", message)
    assert read is not None, message
    growth = float(read.group(1))
    assert growth >= GROWTH_LIMIT, message
    if judged:
        assert (growth > EXPONENT_MAX_FIRST_STEP or grown_call_seconds
                * GROWTH_LIMIT ** EXPONENT_STEPS / growth
                > EXPONENT_SECONDS), message


@pytest.mark.parametrize("judged", (True, False), ids=["3191", "before-3191"])
def test_real_linear_work_with_a_3x_step_passes(monkeypatch, judged):
    """Real linear work reading about 30 ms at the test's size whose cost
    per unit, as this host reads it, steps up about 3x (2.7x: the middle of
    the window) past twice the test's size: about 11x over the first grown
    pair (the check before task 3191 fails it), 4x past it.

    Task 3208: the step is calibrated on its measured growth
    (``_calibrated_step``), the check runs, and the step is measured again
    at the sizes the check read, as the check reads them
    (``_measured_shape``). The check's verdict must be the one that
    measurement owes (``_allowed_verdicts``): passed on its exponent for a
    cache step inside the window, failed outside it. Within SHAPE_MARGIN
    of an edge of the window either verdict on that edge is allowed, and
    what holds of every verdict is asserted
    (``_assert_the_verdict_reads_the_window``)."""
    if not judged:
        _before_3191(monkeypatch)
    target_seconds = PROOF_TARGET_SECONDS
    for _ in range(PROOF_PLANTS):
        units = _linear_units(target_seconds)
        stepped = _real_stepped(2 * units, _calibrated_step(units))
        calls: list[tuple[int, float]] = []

        def seconds_at(size: int, stepped=stepped, calls=calls) -> float:
            started = time.monotonic()
            seconds = stepped(size)
            calls.append((size, time.monotonic() - started))
            return seconds

        verdict, timing, failure = _check_verdict(lambda: assert_linear_time(
            seconds_at, units, 30.0, "real cache step"))
        if max(size for size, _ in calls) > 2 * units:
            break
        target_seconds *= 1.5
    # The premise: the check read the pair the step is in.
    assert max(size for size, _ in calls) > 2 * units, (
        f"the check never read past the step at {2 * units} units: {calls}")
    quarter_size = max(size for size, _ in calls if size <= 2 * units)
    grown_call_seconds = next(seconds for size, seconds in calls
                              if size == GROWTH * quarter_size)
    shape = _measured_shape(stepped, quarter_size, third=judged)
    allowed = _allowed_verdicts(shape, judged, grown_call_seconds)
    assert verdict in allowed, (
        f"the check {verdict} the planted step, {shape.describe()}, which "
        f"owes {sorted(allowed)}: {failure if failure else timing}")
    _assert_the_verdict_reads_the_window(timing, failure, judged,
                                         grown_call_seconds)


def _shape_reading(first_step: float, second_step: float) -> _MeasuredShape:
    """A measured shape whose steps read ``first_step`` then
    ``second_step``, its quarter over the floor."""
    quarter = 1.5 * GROWTH_FLOOR_SECONDS
    return _MeasuredShape(1000, quarter, quarter * first_step,
                          quarter * first_step * second_step)


@pytest.mark.parametrize("first_step, second_step, judged, allowed", [
    # The middle of the window: the proof's calibrated step.
    (11.0, 4.0, True, {PASSED_ON_ITS_EXPONENT}),
    (11.0, 4.0, False, {FAILED}),
    # A cliff, and work flat past the pair (a cap or a window).
    (20.0, 4.0, True, {FAILED}),
    (11.0, 2.0, True, {FAILED}),
    # Near an edge: the verdicts on both sides of it.
    (14.5, 4.0, True, {PASSED_ON_ITS_EXPONENT, FAILED}),
    (8.5, 4.0, True, {PASSED, PASSED_ON_ITS_EXPONENT}),
    (6.0, 4.0, True, {PASSED}),
])
def test_a_measured_shape_owes_the_verdicts_of_the_window(
        first_step, second_step, judged, allowed):
    shape = _shape_reading(first_step, second_step)
    assert shape.first_step == pytest.approx(first_step)
    assert _allowed_verdicts(shape, judged, 0.3) == allowed


def test_a_measured_shape_whose_third_size_costs_too_much_owes_a_failure():
    """R3191-03: a 3 s call at the pair's larger size makes the third size
    cost about 17 s at an 11x first step, over EXPONENT_SECONDS: the check
    fails the pair closed without reading it."""
    assert _allowed_verdicts(_shape_reading(11.0, 4.0), True, 3.0) == {FAILED}


def test_real_quadratic_work_with_the_same_step_fails():
    """The same real work made quadratic (about 25 ms at the test's size,
    under 2 ms at its quarter, the same 3x step past twice the size) fails
    at the test's own pair as before (IR78-01), never on an exponent."""
    units = _linear_units(0.025)
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: real quadratic step: .* "
                             r"at 1x the test's input") as failure:
        assert_linear_time(_real_stepped(2 * units, 3, quadratic_over=units),
                           units, 30.0, "real quadratic step")
    assert "growth exponent" not in str(failure.value)
