"""A pair over the growth limit is confirmed before it fails (task 3188).

CI (Python 3.10, a 12-minute contended suite) failed
``test_every_pattern_is_fast_on_64kb_adversarial_repeats`` for redaction
pattern 24 on ``' \\n'`` repeats: 0.0030 s at 16 KB and 0.0250 s at 64 KB,
8.3x against the test's own pair's 2 ms floor, each the fastest of three
interleaved readings. The pattern grows 4.0x on both interpreters here: all
three larger readings were inflated. The own-pair check at the 2 ms floor
stays (IR78-01); ``tests/host_timing.py`` now confirms a pair still over the
limit once settled (``_confirmed``): both sizes are read again in up to
``CONFIRM_ROUNDS`` rounds of several interleaved readings, at the pair's own
sizes, and the pair fails only when a majority of the rounds read over the
limit too. Each round reads the larger size afresh and holds it against the
quarter size's fastest reading so far.

These tests pin that on scripted readings (the CI readings among them) and
on real work on the real clock: superlinear work fails in every round of
every run, and a capped regression is confirmed at the sizes it is
quadratic at.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
import time
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    CONFIRM_READINGS,
    CONFIRM_ROUNDS,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    GROWTH_RETRIES,
    MAX_GROWTH_REPETITIONS,
    Confirmation,
    TimingCheckFailed,
    assert_linear_per_unit,
    assert_linear_time,
    assert_linear_times,
    settled_growth,
)
from tests.test_host_timing_3171 import _pair_work, _units_reading, timing_failure

# The CI run's sizes and readings (run 37493579809, pattern 24 of task 3138).
CI_SIZE = 64 * 1024
CI_QUARTER = CI_SIZE // GROWTH
CI_QUARTER_SECONDS, CI_LARGE_SECONDS = 0.0030, 0.0250
# What the pattern's linear growth makes of the quarter reading: 4x.
CI_QUIET_LARGE_SECONDS = 0.0120
CI_BUDGET_SECONDS = 0.1
# Wall time per call the CI test paid (building 64 KB and, about once a
# second, the reference workload of ``budget()``): enough that the own pair
# was not repeated there (b81777b), as its failure message shows.
CI_CALL_WALL_SECONDS = 0.06

SIZE = 60_000
QUARTER = SIZE // GROWTH
QUIET_QUARTER, QUADRATIC_LARGE, LINEAR_LARGE = 0.03, 0.48, 0.12


def _series(readings: Mapping[int, Sequence[float]],
            calls: list[int] | None = None) -> Callable[[int], float]:
    """A ``seconds_at`` reading each size's values in turn, then repeating
    the last one."""
    pending = {size: list(values) for size, values in readings.items()}

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        values = pending[size]
        return values.pop(0) if len(values) > 1 else values[0]

    return seconds_at


class _WallClock:
    """``time.monotonic`` for ``tests/host_timing.py``, advanced by the
    calls a fake ``seconds_at`` makes, so a model of an expensive call
    costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _wall_clock(monkeypatch: pytest.MonkeyPatch) -> _WallClock:
    clock = _WallClock()
    monkeypatch.setattr(host_timing, "time", SimpleNamespace(
        monotonic=clock.monotonic, process_time=time.process_time))
    return clock


def _without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before task 3188: a pair still over once settled fails."""
    def unconfirmed(measure, pair, small, large, over, deadline=None,
                    reread_seconds=0.0):
        return dict(small), dict(large), list(over), {}

    monkeypatch.setattr(host_timing, "_confirmed", unconfirmed)


def _rounds(*over_rounds: bool, readings: int = CONFIRM_READINGS
            ) -> list[float]:
    """Larger readings of confirming rounds of ``readings`` each: a round
    reads over the limit (16x the quiet quarter) or linear (4x)."""
    return [QUADRATIC_LARGE if over else LINEAR_LARGE
            for over in over_rounds for _ in range(readings)]


def test_the_rounds_have_a_strict_majority():
    assert CONFIRM_ROUNDS % 2 == 1
    assert 2 * CONFIRM_MAJORITY > CONFIRM_ROUNDS >= CONFIRM_MAJORITY >= 2
    assert 1 <= CONFIRM_READINGS <= MAX_GROWTH_REPETITIONS
    # No round read decides nothing: fail closed.
    assert Confirmation(0, 0, 0).confirmed


# --- The CI readings -----------------------------------------------------------------


def _ci_replay(monkeypatch: pytest.MonkeyPatch, calls: list[int]
               ) -> Callable[[int], float]:
    """The CI readings: a quiet quarter, and a larger size inflated on the
    first reading and on both retries of the limit (GROWTH_RETRIES), then
    read at the 4x the pattern grows."""
    clock = _wall_clock(monkeypatch)
    inflated = 1 + GROWTH_RETRIES
    series = _series({CI_QUARTER: (CI_QUARTER_SECONDS,),
                      CI_SIZE: (CI_LARGE_SECONDS,) * inflated
                      + (CI_QUIET_LARGE_SECONDS,)}, calls)

    def seconds_at(size: int) -> float:
        clock.now += CI_CALL_WALL_SECONDS
        return series(size)

    return seconds_at


def test_the_ci_failure_replays_on_the_check_before_confirmation(monkeypatch):
    """The replay is faithful: without confirmation it fails with the CI
    message (own pair, one run of each size, 2 ms floor, 8.3x)."""
    _without_confirmation(monkeypatch)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(_ci_replay(monkeypatch, calls), CI_SIZE,
                           CI_BUDGET_SECONDS, "' \\n'")
    message = str(failure.value)
    assert (f"{CI_LARGE_SECONDS:.4f} s at size {CI_SIZE}, "
            f"{CI_QUARTER_SECONDS:.4f} s at size {CI_QUARTER} (growth 8.3x "
            f"at 1x the test's input, floor 0.002 s" in message)
    assert "runs of each" not in message
    assert "each the fastest of 3 interleaved readings)" in message


def test_the_ci_readings_pass_once_confirmed(monkeypatch):
    """Confirmed, the same readings pass: every confirming round reads the
    larger size at 4x the quarter. Each round takes seven readings of each
    size (a 3 ms quarter totals the 20 ms floor in seven), alternating."""
    calls: list[int] = []
    timing = assert_linear_time(_ci_replay(monkeypatch, calls), CI_SIZE,
                                CI_BUDGET_SECONDS, "' \\n'")
    assert timing.ratio < GROWTH_LIMIT
    settled_calls = [CI_QUARTER, CI_SIZE] * (1 + GROWTH_RETRIES)
    readings = -(-GROWTH_FLOOR_SECONDS // CI_QUARTER_SECONDS)
    assert readings == 7
    assert calls == settled_calls + [CI_QUARTER, CI_SIZE] * int(
        readings * CONFIRM_MAJORITY)


# --- Majority -----------------------------------------------------------------------


@pytest.mark.parametrize("over_rounds, rounds_over, rounds, confirmed", [
    ((True, True, True), 3, 3, True),
    ((False, False, False), 0, 3, False),
    ((True, False, True, False, False), 2, 5, False),
    ((True, False, True, False, True), 3, 5, True),
    ((False, True, True, True), 3, 4, True),
    ((True, False, False, False), 1, 4, False),
])
def test_a_pair_fails_only_when_most_rounds_read_over_the_limit(
        over_rounds, rounds_over, rounds, confirmed):
    """``settled_growth`` (the same settling as every pair): the scripted
    pair is over the limit on its settled readings (16x), then its rounds
    read over or linear. The rounds stop at a majority either way."""
    calls: list[int] = []
    settled_larger = [QUADRATIC_LARGE] * GROWTH_RETRIES
    pair = settled_growth(
        _series({QUARTER: (QUIET_QUARTER,),
                 SIZE: settled_larger + _rounds(*over_rounds)}, calls),
        QUARTER, SIZE, QUIET_QUARTER, QUADRATIC_LARGE)
    assert pair.confirmation == Confirmation(rounds, rounds_over,
                                             CONFIRM_READINGS)
    assert pair.confirmation.confirmed is confirmed
    assert (pair.ratio >= GROWTH_LIMIT) is confirmed
    assert calls == [QUARTER, SIZE] * (GROWTH_RETRIES
                                       + rounds * CONFIRM_READINGS)
    if confirmed:
        assert pair.seconds == pytest.approx(QUADRATIC_LARGE)
    else:
        # The fastest round's reading: linear, as most rounds read.
        assert pair.seconds == pytest.approx(LINEAR_LARGE)


def test_assert_linear_time_fails_on_a_majority_and_says_so():
    calls: list[int] = []
    seconds_at = _series({QUARTER: (QUIET_QUARTER,),
                          SIZE: [QUADRATIC_LARGE] * (1 + GROWTH_RETRIES)
                          + _rounds(True, False, True, False, True)}, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, "three of five")
    assert ("confirmed: over the limit in 3 of 5 confirming rounds of "
            f"{CONFIRM_READINGS} interleaved readings of each size"
            in str(failure.value))


def test_assert_linear_time_passes_on_a_minority():
    timing = assert_linear_time(
        _series({QUARTER: (QUIET_QUARTER,),
                 SIZE: [QUADRATIC_LARGE] * (1 + GROWTH_RETRIES)
                 + _rounds(True, False, True, False, False)}),
        SIZE, 10.0, "two of five")
    assert timing.ratio == pytest.approx(LINEAR_LARGE / QUIET_QUARTER)


def test_only_the_confirmed_part_fails_beside_a_cleared_one():
    """``assert_linear_times``: a part whose larger readings were inflated
    while settling is cleared, a quadratic part beside it is not."""
    burst = _series({QUARTER: (QUIET_QUARTER,),
                     SIZE: [QUADRATIC_LARGE] * (1 + GROWTH_RETRIES)
                     + [LINEAR_LARGE]})

    def seconds_at(size: int) -> dict[str, float]:
        return {"burst": burst(size),
                "quadratic": QUADRATIC_LARGE * (size / SIZE) ** 2}

    with pytest.raises(TimingCheckFailed, match="parts: quadratic") as failure:
        assert_linear_times(seconds_at, SIZE, 10.0, "parts")
    assert "parts: burst" not in str(failure.value)


# --- What confirmation must not clear ------------------------------------------------


@pytest.mark.parametrize("inflation", (1.9, 2.33, 3.0))
def test_a_round_never_decides_on_a_loaded_quarter(inflation):
    """The quarter reads quiet while the pair settles and loaded in every
    confirming round: on their own quarter readings the rounds would read
    a 9.3x regression at 4.9x or less and clear it (R3184-03). Against the
    quarter's fastest reading they read 9.3x."""
    quiet_quarter, larger = 0.0150, 0.1400
    loaded = [quiet_quarter * inflation]
    # Against the test's own pair's floor (main's 2 ms), as CI's pair was.
    pair = settled_growth(
        _series({QUARTER: [quiet_quarter] * GROWTH_RETRIES + loaded,
                 SIZE: (larger,)}),
        QUARTER, SIZE, quiet_quarter, larger,
        host_timing.DETECTION_FLOOR_SECONDS)
    assert larger / loaded[0] < GROWTH_LIMIT
    assert pair.confirmation is not None and pair.confirmation.confirmed
    assert pair.ratio >= GROWTH_LIMIT


def test_a_capped_regression_is_confirmed_at_the_sizes_it_is_quadratic_at():
    """Quadratic up to the test's size and flat past it (an input cap the
    larger size reaches, IR78-01): a pair past the test's size reads 1x, so
    the rounds read the pair's own sizes and nothing larger."""
    calls: list[int] = []

    def seconds_at(size: int) -> float:
        calls.append(size)
        return QUADRATIC_LARGE * (min(size, SIZE) / SIZE) ** 2

    with pytest.raises(TimingCheckFailed, match="confirmed: over the limit "
                       f"in {CONFIRM_MAJORITY} of {CONFIRM_MAJORITY}"):
        assert_linear_time(seconds_at, SIZE, 10.0, "capped")
    assert set(calls) == {QUARTER, SIZE}


def test_a_per_unit_pair_is_confirmed_too():
    """``assert_linear_per_unit``: quadratic per unit (4x at 4 times the
    input) fails confirmed; linear work whose larger size read 2.04x per
    unit while settling (task 3185's CI readings) passes once its rounds
    read it linear."""
    with pytest.raises(TimingCheckFailed, match="confirmed: over the limit"):
        assert_linear_per_unit(_series({50: (0.05,), 200: (0.2,)}),
                               (50, 200), 10.0, "quadratic")
    settled = assert_linear_per_unit(
        _series({50: (0.067,), 200: [0.137] * (1 + host_timing
                                                .PER_UNIT_OVER_RETRIES)
                 + [0.068]}), (50, 200), 10.0, "settling burst")
    assert settled[200] == pytest.approx(0.068)


@pytest.mark.parametrize("seed", range(200))
def test_every_verdict_agrees_with_the_readings_it_keeps(seed):
    """Two parts over the limit, confirmed on noisy readings (seeded): each
    larger reading anywhere from linear to quadratic, each quarter reading
    sometimes loaded, so the quarter's fastest reading falls between rounds
    and recounts a round decided earlier. Whatever the readings, every part
    has a strict majority one way, fails exactly when confirmed, and keeps
    readings that agree with its verdict: a confirmed part its settled
    ones (over the limit), a cleared part a round under the limit."""
    rng = random.Random(seed)
    quiet = {"a": 0.030, "b": 0.025}
    settled_small = {part: seconds * 1.3 for part, seconds in quiet.items()}
    settled_large = {"a": 0.48, "b": 0.30}
    pair = host_timing._Pair(QUARTER, SIZE, 1.0, GROWTH_FLOOR_SECONDS)

    def measure(size: int) -> dict[str, float]:
        if size == QUARTER:
            return {part: seconds * rng.choice((1.0, 1.1, 1.3, 2.0))
                    for part, seconds in quiet.items()}
        return {part: seconds * rng.uniform(3.5, 18.0)
                for part, seconds in quiet.items()}

    assert all(host_timing.growth_ratio(settled_small[part],
                                        settled_large[part]) >= GROWTH_LIMIT
               for part in quiet)
    small, large, still_over, confirmations = host_timing._confirmed(
        measure, pair, settled_small, settled_large, list(quiet))

    assert set(confirmations) == set(quiet)
    assert len({verdict.rounds for verdict in confirmations.values()}) == 1
    for part, verdict in confirmations.items():
        assert CONFIRM_MAJORITY <= verdict.rounds <= CONFIRM_ROUNDS
        assert 2 * verdict.rounds_over != verdict.rounds, verdict
        assert (part in still_over) is verdict.confirmed
        assert small[part] <= settled_small[part]
        ratio = host_timing.growth_ratio(small[part], large[part])
        if verdict.confirmed:
            assert large[part] == settled_large[part]
            assert ratio >= GROWTH_LIMIT
        else:
            assert ratio < GROWTH_LIMIT, (part, verdict, small, large)


# --- Cost ---------------------------------------------------------------------------


def test_an_expensive_pair_reads_once_per_round(monkeypatch):
    """A round's readings are bounded by CONFIRM_ROUND_SECONDS of wall time
    measured on its first: a pair costing 1.2 s reads once per round."""
    clock = _wall_clock(monkeypatch)
    calls: list[int] = []
    quadratic = _series({QUARTER: (QUIET_QUARTER,), SIZE: (QUADRATIC_LARGE,)},
                        calls)

    def seconds_at(size: int) -> float:
        clock.now += 0.6
        return quadratic(size)

    pair = settled_growth(seconds_at, QUARTER, SIZE, QUIET_QUARTER,
                          QUADRATIC_LARGE)
    assert pair.confirmation == Confirmation(CONFIRM_MAJORITY,
                                             CONFIRM_MAJORITY, 1)
    assert calls == [QUARTER, SIZE] * (GROWTH_RETRIES + CONFIRM_MAJORITY)


def test_a_cheap_quarter_reads_until_its_round_totals_the_floor():
    """A 2.5 ms quarter reading totals the 20 ms floor in eight readings."""
    small = GROWTH_FLOOR_SECONDS / 8
    pair = settled_growth(_series({QUARTER: (small,), SIZE: (small * 16,)}),
                          QUARTER, SIZE, small, small * 16,
                          host_timing.DETECTION_FLOOR_SECONDS)
    assert pair.confirmation == Confirmation(CONFIRM_MAJORITY,
                                             CONFIRM_MAJORITY, 8)


# --- Real work on the real clock -----------------------------------------------------


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


def _linear_work(units: int) -> int:
    total = 0
    for value in range(units):
        total ^= value
    return total


def _spin_cpu_until(started: float, seconds: float) -> None:
    """Spend CPU time until ``seconds`` have passed since ``started``."""
    while time.process_time() - started < seconds:
        pass


@pytest.mark.parametrize("confirming", (True, False),
                         ids=["confirmed", "before-3188"])
def test_real_linear_work_with_every_settling_reading_inflated(
        monkeypatch, confirming):
    """Real linear work reading about 3 ms at the quarter size, every
    larger reading inflated to 16 times the quarter's fastest until
    confirmation starts (the CI shape: a burst over the first reading and
    both retries of the limit, here wide enough that a loaded quarter
    cannot hide it). The check before task 3188 fails it; confirmed, it
    passes."""
    units = _linear_units(0.012)
    quarter = units // GROWTH
    state = {"confirming": False, "quarter": float("inf")}
    if confirming:
        original = host_timing._confirmed

        def confirm(*args, **kwargs):
            state["confirming"] = True
            return original(*args, **kwargs)

        monkeypatch.setattr(host_timing, "_confirmed", confirm)
    else:
        _without_confirmation(monkeypatch)

    def seconds_at(size: int) -> float:
        started = time.process_time()
        _linear_work(size)
        if size == quarter:
            elapsed = time.process_time() - started
            state["quarter"] = min(state["quarter"], elapsed)
            return elapsed
        if not state["confirming"]:
            _spin_cpu_until(started, GROWTH ** 2 * state["quarter"])
        return time.process_time() - started

    failure = timing_failure(lambda: assert_linear_time(
        seconds_at, units, 30.0, "linear under a settling burst"))
    if confirming:
        assert failure is None, str(failure)
        assert state["confirming"]
    else:
        assert failure is not None and "superlinear growth" in str(failure)


def test_real_quadratic_work_fails_confirmed_every_time():
    """Real quadratic work reading about 25 ms at the test's size and under
    2 ms at its quarter (the regime CI's noise lives in): three runs, each
    failing on growth in every confirming round."""
    units = _units_reading(0.025)
    for run in range(3):
        failure = timing_failure(lambda: assert_linear_time(
            _quadratic_seconds, units, 30.0, "quadratic"))
        assert failure is not None, f"run {run} passed quadratic work"
        assert (f"confirmed: over the limit in {CONFIRM_MAJORITY} of "
                f"{CONFIRM_MAJORITY} confirming rounds" in str(failure))


def _quadratic_seconds(units: int) -> float:
    started = time.process_time()
    _pair_work(units)
    return time.process_time() - started
