"""Edge cases of the confirming rounds of ``tests/host_timing.py`` (task 3188).

``tests/test_host_timing_3188.py`` pins the majority rule, the CI replay and
real work. These tests pin the parts of ``_confirmed`` that size a round and
judge it, on scripted readings and a scripted wall clock:

* a round's readings count every repetition of a reading toward the
  ``GROWTH_FLOOR_SECONDS`` total, never take fewer than ``CONFIRM_READINGS``
  or more than ``MAX_GROWTH_REPETITIONS``, and stay within what
  ``CONFIRM_ROUND_SECONDS`` of wall time pays for;
* a round is judged against the pair's floor, as the settled readings were;
* a zero quarter reading neither divides by zero nor clears the pair;
* the caller's readings are never changed;
* every verdict says what it was decided on.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import time
from types import MappingProxyType, SimpleNamespace
from typing import Mapping

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    CONFIRM_READINGS,
    CONFIRM_ROUND_SECONDS,
    CONFIRM_ROUNDS,
    DETECTION_FLOOR_SECONDS,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    GROWTH_RETRIES,
    MAX_GROWTH_REPETITIONS,
    Confirmation,
    assert_linear_time,
    growth_ratio,
    settled_growth,
)

SIZE = 60_000
QUARTER = SIZE // GROWTH
PART = "pattern"


class _ScriptedClock:
    """``time.monotonic`` for ``tests/host_timing.py`` that moves only by
    what each scripted measurement says it cost."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _ScriptedClock:
    scripted = _ScriptedClock()
    monkeypatch.setattr(host_timing, "time", SimpleNamespace(
        monotonic=scripted.monotonic, process_time=time.process_time))
    return scripted


class _Measure:
    """A ``measure`` reading fixed seconds per size for one part, logging
    every size it was called at and advancing ``clock`` by
    ``wall_seconds`` per call."""

    def __init__(self, seconds_by_size: Mapping[int, float],
                 clock: _ScriptedClock, wall_seconds: float = 0.0) -> None:
        self.seconds_by_size = dict(seconds_by_size)
        self.clock = clock
        self.wall_seconds = wall_seconds
        self.calls: list[int] = []

    def __call__(self, size: int) -> dict[str, float]:
        self.calls.append(size)
        self.clock.now += self.wall_seconds
        return {PART: self.seconds_by_size[size]}


def _confirm(measure: _Measure, small_seconds: float, large_seconds: float,
             floor_seconds: float, repetitions: int = 1):
    pair = host_timing._Pair(QUARTER, SIZE, 1.0, floor_seconds, repetitions)
    assert not growth_ratio(small_seconds, large_seconds,
                            floor_seconds) < GROWTH_LIMIT, (
        "the scripted settled pair must be over the limit")
    return host_timing._confirmed(measure, pair, {PART: small_seconds},
                                  {PART: large_seconds}, [PART])


# --- Confirmation ----------------------------------------------------------------


@pytest.mark.parametrize("rounds, rounds_over, verdict", [
    (5, 3, "confirmed"),
    (3, 3, "confirmed"),
    (5, 2, "not confirmed"),
    (3, 0, "not confirmed"),
])
def test_a_confirmation_describes_its_verdict_and_its_evidence(
        rounds, rounds_over, verdict):
    confirmation = Confirmation(rounds, rounds_over, 7)
    assert confirmation.describe() == (
        f"{verdict}: over the limit in {rounds_over} of {rounds} confirming "
        f"rounds of 7 interleaved readings of each size")
    assert confirmation.confirmed is (verdict == "confirmed")


def test_a_tie_is_not_a_majority():
    """Never produced by the rounds (they stop on a strict majority), but
    a tie must not fail a pair on half the evidence."""
    assert not Confirmation(4, 2, 3).confirmed
    assert Confirmation(4, 3, 3).confirmed


# --- How many readings a round takes ---------------------------------------------


@pytest.mark.parametrize("repetitions, readings", [
    # 2.5 ms per run: 8 runs total the 20 ms floor.
    (1, 8),
    (2, 4),
    # 4 runs per reading total 10 ms; two readings would do, but a round
    # never takes fewer than CONFIRM_READINGS.
    (4, CONFIRM_READINGS),
])
def test_every_repetition_counts_toward_the_round_floor(
        clock, repetitions, readings):
    quarter_seconds = GROWTH_FLOOR_SECONDS / 8
    measure = _Measure({QUARTER: quarter_seconds,
                        SIZE: quarter_seconds * GROWTH ** 2}, clock)
    _, _, still_over, confirmations = _confirm(
        measure, quarter_seconds, quarter_seconds * GROWTH ** 2,
        DETECTION_FLOOR_SECONDS, repetitions)
    assert still_over == [PART]
    assert confirmations[PART] == Confirmation(CONFIRM_MAJORITY,
                                               CONFIRM_MAJORITY, readings)
    # Quadratic work reads over the limit in every round: it stops at the
    # majority, each reading `repetitions` runs of both sizes, alternating.
    assert measure.calls == [QUARTER, SIZE] * (
        CONFIRM_MAJORITY * readings * repetitions)


@pytest.mark.parametrize("quarter_seconds", (1e-4, 0.0),
                         ids=["a-tenth-of-a-millisecond", "zero"])
def test_a_round_takes_at_most_max_growth_repetitions_readings(
        clock, quarter_seconds):
    """A quarter reading far under the floor (or none at all, a clock
    that did not tick) would need hundreds of readings to total the floor;
    a round stops at MAX_GROWTH_REPETITIONS, and a zero reading is neither
    a division by zero nor a reason to clear the pair."""
    larger_seconds = GROWTH_LIMIT * DETECTION_FLOOR_SECONDS * 2
    measure = _Measure({QUARTER: quarter_seconds, SIZE: larger_seconds}, clock)
    _, _, still_over, confirmations = _confirm(
        measure, quarter_seconds, larger_seconds, DETECTION_FLOOR_SECONDS)
    assert still_over == [PART]
    assert confirmations[PART] == Confirmation(
        CONFIRM_MAJORITY, CONFIRM_MAJORITY, MAX_GROWTH_REPETITIONS)
    assert len(measure.calls) == 2 * CONFIRM_MAJORITY * MAX_GROWTH_REPETITIONS


def test_a_rounds_readings_stay_within_its_wall_time(clock):
    """A first reading costing 0.3 s of wall time (both sizes) leaves room
    for three more within CONFIRM_ROUND_SECONDS: four readings per round,
    not the eight the floor asks for, in every round."""
    assert CONFIRM_ROUND_SECONDS == 1.0
    quarter_seconds = GROWTH_FLOOR_SECONDS / 8
    measure = _Measure({QUARTER: quarter_seconds,
                        SIZE: quarter_seconds * GROWTH ** 2}, clock,
                       wall_seconds=0.15)
    _, _, _, confirmations = _confirm(
        measure, quarter_seconds, quarter_seconds * GROWTH ** 2,
        DETECTION_FLOOR_SECONDS)
    assert confirmations[PART] == Confirmation(CONFIRM_MAJORITY,
                                               CONFIRM_MAJORITY, 4)
    assert len(measure.calls) == 2 * CONFIRM_MAJORITY * 4


# --- How a round is judged --------------------------------------------------------


def test_a_round_is_judged_against_the_pairs_floor(clock):
    """Settled at 0.5 ms and 20 ms (10x the 2 ms floor: over), every round
    reads 12 ms: 24x the quarter reading, but 6x the floor the pair is held
    to. The rounds clear it, as the settled check would have; judged on the
    raw quarter reading they would confirm it."""
    quarter_seconds, settled_larger, round_larger = 0.0005, 0.020, 0.012
    assert round_larger / quarter_seconds >= GROWTH_LIMIT
    assert growth_ratio(quarter_seconds, round_larger,
                        DETECTION_FLOOR_SECONDS) < GROWTH_LIMIT
    measure = _Measure({QUARTER: quarter_seconds, SIZE: round_larger}, clock)
    small, large, still_over, confirmations = _confirm(
        measure, quarter_seconds, settled_larger, DETECTION_FLOOR_SECONDS)
    assert still_over == []
    assert confirmations[PART].rounds_over == 0
    assert confirmations[PART].rounds == CONFIRM_MAJORITY
    assert small[PART] == quarter_seconds
    assert large[PART] == round_larger


def test_a_round_judged_at_the_growth_floor_clears_a_sub_floor_burst(clock):
    """The same shape at GROWTH_FLOOR_SECONDS (the grown pairs' floor):
    settled 0.2 s against 20 ms is 10x, the rounds read 0.1 s (5x)."""
    measure = _Measure({QUARTER: 0.001, SIZE: 0.1}, clock)
    _, large, still_over, confirmations = _confirm(
        measure, 0.001, 0.2, GROWTH_FLOOR_SECONDS)
    assert still_over == []
    assert not confirmations[PART].confirmed
    assert large[PART] == 0.1


def test_the_callers_readings_are_never_changed(clock):
    """A cleared part's readings are replaced in the mappings returned,
    not in the ones passed in (read-only here, so a write would raise)."""
    measure = _Measure({QUARTER: 0.0005, SIZE: 0.012}, clock)
    pair = host_timing._Pair(QUARTER, SIZE, 1.0, DETECTION_FLOOR_SECONDS)
    small_in = MappingProxyType({PART: 0.0005})
    large_in = MappingProxyType({PART: 0.020})
    over_in = [PART]
    small, large, still_over, _ = host_timing._confirmed(
        measure, pair, small_in, large_in, over_in)
    assert dict(small_in) == {PART: 0.0005}
    assert dict(large_in) == {PART: 0.020}
    assert over_in == [PART]
    assert still_over == [] and still_over is not over_in
    assert large == {PART: 0.012}


# --- What the checks report ------------------------------------------------------


def _series(readings: Mapping[int, list[float]],
            calls: list[int] | None = None):
    pending = {size: list(values) for size, values in readings.items()}

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        values = pending[size]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds_at


def test_the_stop_pair_decides_on_the_cleared_pairs_fastest_round():
    """Two of five rounds over the limit clear the own pair, which writes
    its fastest round back (task 3191, R3188-02). The pair the growth stops
    at (the same sizes here) decides on those readings: linear, with no
    measurement and no confirmation of its own."""
    quiet_quarter, quadratic, linear = 0.03, 0.48, 0.12
    rounds = [True, False, True, False, False]
    assert len(rounds) == CONFIRM_ROUNDS
    larger = [quadratic] * (1 + GROWTH_RETRIES) + [
        quadratic if over else linear
        for over in rounds for _ in range(CONFIRM_READINGS)]
    calls: list[int] = []
    timing = assert_linear_time(
        _series({QUARTER: [quiet_quarter], SIZE: larger}, calls), SIZE, 10.0,
        "two of five")
    assert (timing.small_size, timing.size) == (QUARTER, SIZE)
    assert timing.seconds == pytest.approx(linear)
    assert timing.ratio == pytest.approx(linear / quiet_quarter)
    assert timing.confirmation is None
    assert calls == [QUARTER, SIZE] * (1 + GROWTH_RETRIES
                                       + CONFIRM_ROUNDS * CONFIRM_READINGS)


def test_a_timing_describes_the_rounds_that_cleared_it():
    timing = host_timing.LinearTiming(
        "cleared", QUARTER, 0.03, SIZE, 0.12, 10.0, 1.0, 1.0,
        GROWTH_FLOOR_SECONDS, 1, 3, Confirmation(CONFIRM_ROUNDS, 2,
                                                 CONFIRM_READINGS))
    assert timing.describe().endswith(
        f"each the fastest of 3 interleaved readings; not confirmed: over "
        f"the limit in 2 of {CONFIRM_ROUNDS} confirming rounds of "
        f"{CONFIRM_READINGS} interleaved readings of each size)")


def test_a_pair_never_over_the_limit_is_not_confirmed_at_all():
    """Linear readings never reach the rounds: no confirmation is recorded
    and none is described."""
    pair = settled_growth(_series({QUARTER: [0.03], SIZE: [0.12]}),
                          QUARTER, SIZE, 0.03, 0.12)
    assert pair.confirmation is None
    timing = assert_linear_time(_series({QUARTER: [0.03], SIZE: [0.12]}),
                                SIZE, 10.0, "linear")
    assert timing.confirmation is None
    assert "confirm" not in timing.describe()
