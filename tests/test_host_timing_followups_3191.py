"""Follow-ups of the independent review of task 3188, closed in task 3191.

R3188-01: a first larger reading over the limit against the floor stopped
the growth ("the retries decide it"), and the stop pair at the test's sizes
then cleared on its re-readings or confirming rounds, so work superlinear
only past the test's size passed with no grown size ever read. The growth
now goes on when the stop pair's settled readings would have grown it.

R3188-02: a part the confirming rounds cleared kept its MEDIAN round, and
the next grown pair reused that reading as its quarter reading, so a pair
9.5x over the fastest reading of that size passed at 7.1x. It now keeps its
fastest round.

IR88-01: the majority vote of the confirming rounds can only turn a fail
into a pass. A seeded simulation pins what that costs: regressions of 10x
and more per step, read under one-sided bursts, fail at least as often as
before task 3188, within a stated tolerance, while linear work fails less
often.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import math
import random
from typing import Callable, Mapping

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    GROWTH,
    GROWTH_LIMIT,
    GROWTH_RETRIES,
    TimingCheckFailed,
    assert_linear_time,
)
from tests.test_host_timing_3171 import _build_clock

QUARTER, SIZE = 15_000, 60_000
GROWN, THIRD = SIZE * GROWTH, SIZE * GROWTH ** 2
BUDGET_SECONDS = 60.0


def _series(readings: Mapping[int, list[float]],
            calls: list[int] | None = None) -> Callable[[int], float]:
    """Each size reads its list in order, then its last reading forever."""
    pending = {size: list(values) for size, values in readings.items()}

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        values = pending[size]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds_at


# --- R3188-01: a cleared stop pair grows the input -----------------------------

# The test's quarter reads 15 ms (under the floor, so the input may grow)
# and its larger size 60 ms; a burst covers the first six readings of the
# larger size (the own pair's two-run means and both of its retries), so
# the first ratio against the floor is 15x and stops the growth. The
# quarter and later larger readings are quiet.
BURST_READINGS = [0.30] * 6
QUIET_LARGE = 0.06


def _chain(past_the_test: float, calls: list[int]) -> Callable[[int], float]:
    """The chain above; past the test's size the work grows
    ``past_the_test`` times per GROWTH step."""
    return _series({QUARTER: [0.015],
                    SIZE: BURST_READINGS + [QUIET_LARGE],
                    GROWN: [QUIET_LARGE * past_the_test],
                    THIRD: [QUIET_LARGE * past_the_test ** 2]}, calls)


def test_work_quadratic_past_a_cleared_stop_pair_fails():
    """Before the fix this passed at the test's sizes (3.0x against the
    floor once the burst settled) with no grown size read. It fails at the
    grown pair, 16x: over what a cache step reads, so the size past it is
    not read for an exponent (R3191-03)."""
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: chain: 0\.9600 s at size "
                             rf"{GROWN}") as failure:
        assert_linear_time(_chain(GROWTH ** 2, calls), SIZE, BUDGET_SECONDS,
                           "chain")
    assert "growth exponent" not in str(failure.value)
    assert GROWN in calls and THIRD not in calls


def test_linear_work_past_a_cleared_stop_pair_passes():
    """The control: the same burst on linear work grows the input from the
    settled readings and passes there."""
    calls: list[int] = []
    timing = assert_linear_time(_chain(GROWTH, calls), SIZE, BUDGET_SECONDS,
                                "chain")
    assert (timing.small_size, timing.size) == (SIZE, GROWN)
    assert timing.small_seconds == pytest.approx(QUIET_LARGE)
    assert timing.ratio == pytest.approx(GROWTH)
    assert THIRD not in calls


def test_growth_that_ran_out_of_input_is_not_asked_again():
    """A shape that does not exist past the stop pair is decided there, on
    main's floor, once: the re-decided growth never loops."""
    calls: list[int] = []
    readings = _chain(GROWTH, calls)

    def capped(size: int) -> float:
        if size > SIZE:
            calls.append(size)
            raise host_timing.InputTooLarge(size)
        return readings(size)

    timing = assert_linear_time(capped, SIZE, BUDGET_SECONDS, "capped")
    assert (timing.small_size, timing.size) == (QUARTER, SIZE)
    assert timing.floor_seconds == host_timing.DETECTION_FLOOR_SECONDS
    assert calls.count(GROWN) == 1


# --- R3188-02: a cleared part keeps its fastest round ---------------------------


def _median_rounds(clock, calls: list[int]) -> Callable[[int], float]:
    """The review's readings, each call taking 0.6 s of wall time so the own
    pair is read once and a confirming round reads once: the larger size
    bursts (20x) on its first reading and both retries, then rounds of
    60 ms (under), 300 ms (over), 80 ms and 70 ms (under) clear it 3 of 4,
    its median round 80 ms and its fastest 60 ms; every later reading of it
    is 90 ms. The grown size reads 0.57 s: 9.5x the fastest reading and
    7.1x the median round; quadratic past it."""
    seconds_at = _series({
        QUARTER: [0.015],
        SIZE: [0.30] * (1 + GROWTH_RETRIES) + [0.06, 0.30, 0.08, 0.07, 0.09],
        GROWN: [0.57], THIRD: [0.57 * GROWTH ** 2]}, calls)

    def timed(size: int) -> float:
        clock.now += 0.6
        return seconds_at(size)

    return timed


def test_the_next_grown_pair_starts_from_the_fastest_round(monkeypatch):
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=rf"0\.5700 s at size {GROWN}, 0\.0600 s at size "
                             rf"{SIZE} \(growth 9\.5x"):
        assert_linear_time(_median_rounds(clock, calls), SIZE,
                           BUDGET_SECONDS, "median")
    assert GROWN in calls


def test_the_median_round_let_the_grown_pair_pass(monkeypatch):
    """The replica of the check before the fix (median round kept)."""
    clock = _build_clock(monkeypatch)
    original = host_timing._confirmed

    def median_kept(measure, pair, small, large, over, *args):
        # One reading per round here (each call takes 0.6 s), so every
        # larger reading the rounds take is a round's reading.
        larger_by_round: dict[str, list[float]] = {part: [] for part in over}

        def recording(at_size: int) -> Mapping[str, float]:
            readings = measure(at_size)
            if at_size == pair.size:
                for part in over:
                    larger_by_round[part].append(readings[part])
            return readings

        small, large, still_over, confirmations = original(
            recording, pair, small, large, over, *args)
        for part in over:
            if part not in still_over:
                ordered = sorted(larger_by_round[part])
                large[part] = ordered[len(ordered) // 2]
        return small, large, still_over, confirmations

    monkeypatch.setattr(host_timing, "_confirmed", median_kept)
    timing = assert_linear_time(_median_rounds(clock, []), SIZE,
                                BUDGET_SECONDS, "median")
    assert timing.ratio == pytest.approx(0.57 / 0.08)
    assert timing.ratio < GROWTH_LIMIT


# --- IR88-01: what the majority vote costs, on seeded bursts --------------------

TRIALS_PER_CELL = 40
# Readings inflated by a burst: this share of them, by 1.5-10x; the rest
# read up to a few percent over their cost (one-sided: load never speeds
# work up).
BURST_SHARE = 0.25
BURST_RANGE = (1.5, 10.0)
QUIET_SIGMA = 0.03
# Regressions confirmation may clear that the check before task 3188
# failed, as a share of the regression trials.
ESCAPE_TOLERANCE = 0.01


def _shape(kind: str, ratio: float) -> Callable[[int], float]:
    """The cost at ``n`` of work growing ``ratio`` times per GROWTH step:
    from the test's quarter (``at-size``: 30 ms there, over the floor;
    ``means``: 5 ms there, read in repetitions), or linear up to the
    test's size and growing past it (``past``: 20 ms at the test's size,
    so the input grows)."""
    power = math.log(ratio) / math.log(GROWTH)
    if kind == "at-size":
        return lambda n: 0.03 * (n / QUARTER) ** power
    if kind == "means":
        return lambda n: 0.005 * (n / QUARTER) ** power
    return lambda n: (0.02 * n / SIZE if n <= SIZE
                      else 0.02 * (n / SIZE) ** power)


def _noisy(cost: Callable[[int], float], seed: int) -> Callable[[int], float]:
    stream = random.Random(seed)

    def seconds_at(n: int) -> float:
        if stream.random() < BURST_SHARE:
            return cost(n) * stream.uniform(*BURST_RANGE)
        return cost(n) * (1.0 + abs(stream.gauss(0.0, QUIET_SIGMA)))

    return seconds_at


def _fails(cost: Callable[[int], float], seed: int) -> bool:
    try:
        assert_linear_time(_noisy(cost, seed), SIZE, BUDGET_SECONDS, "seeded")
    except TimingCheckFailed:
        return True
    return False


def _failures(kinds: tuple[str, ...], ratios: tuple[float, ...]
              ) -> dict[tuple[str, float, int], bool]:
    return {(kind, ratio, seed): _fails(_shape(kind, ratio), seed)
            for kind in kinds for ratio in ratios
            for seed in range(TRIALS_PER_CELL)}


def _unconfirmed(measure, pair, small, large, over, *args):
    """The check before task 3188: a pair still over once settled fails."""
    return dict(small), dict(large), list(over), {}


KINDS = ("at-size", "means", "past")


def test_regressions_fail_on_bursts_as_often_as_before_confirmation(
        monkeypatch):
    regressions = (10.0, 12.0, 16.0)
    confirmed = _failures(KINDS, regressions)
    monkeypatch.setattr(host_timing, "_confirmed", _unconfirmed)
    before = _failures(KINDS, regressions)
    escapes = [trial for trial, failed in before.items()
               if failed and not confirmed[trial]]
    assert len(escapes) <= ESCAPE_TOLERANCE * len(before), escapes
    assert sum(confirmed.values()) >= (sum(before.values())
                                       - ESCAPE_TOLERANCE * len(before))
    # Each kind of regression is caught almost every time. Both checks miss
    # about 5% of them under these bursts, the same trials: a burst on the
    # quarter size's first reading and on its one settling re-reading
    # (SETTLE_RETRIES) hides the growth before any confirmation runs.
    for kind in KINDS:
        caught = sum(failed for (at_kind, _, _), failed in confirmed.items()
                     if at_kind == kind)
        assert caught >= 0.9 * TRIALS_PER_CELL * len(regressions), kind


def test_linear_work_fails_on_bursts_less_often_than_before(monkeypatch):
    confirmed = _failures(KINDS, (float(GROWTH),))
    monkeypatch.setattr(host_timing, "_confirmed", _unconfirmed)
    before = _failures(KINDS, (float(GROWTH),))
    assert sum(confirmed.values()) <= sum(before.values())
    # The rounds need a majority to fail: a linear pair fails only when
    # bursts cover the larger size in CONFIRM_MAJORITY whole rounds.
    assert sum(confirmed.values()) <= 0.05 * len(confirmed), CONFIRM_MAJORITY
