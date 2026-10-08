"""A pair is never failed on a smaller reading under the noise floor (task
3204), and the 3191 review follow-ups R3191-01, R3191-02 and R3191-03.

CI (PR #44, run 37729154851, Python 3.12) failed
``test_redaction_linear_3138::test_every_pattern_is_fast_on_64kb_adversarial_repeats``
for redaction pattern 24 on ``'//'`` repeats: 0.0182 s at 64 KB and
0.0020 s at 16 KB, 9.1x against the test's own pair's 2 ms floor, each the
fastest of three interleaved readings and confirmed in 3 of 3 rounds of 11.
The pattern is linear: 3.7-4.6 ms at 16 KB and 14.7-18.3 ms at 64 KB here,
on Python 3.12 and 3.10. The quarter size keeps its fastest reading, and
the fastest of 36 calls of a few milliseconds read under half their cost: a
call that short can run whole inside a quiet spell of the runner (a turbo
clock, an idle SMT sibling) that a call four times longer never fits. The
same runner read real linear work at 1.28x the cost per unit over 0.3 s
that it read over 25 ms (the 3191 test of the same CI run). A coarse clock
does not explain CI's readings (0.0182 s is no multiple of a tick), but its
zero readings are the same hazard, and both are modelled here.

``tests/host_timing.py`` now never fails a pair on such a reading: a pair
over the limit only on a smaller reading totalling under
``GROWTH_FLOOR_SECONDS`` (it would pass were that reading raised to the
floor, ``over_on_a_sub_floor_reading``) is read again at the same sizes
over enough interleaved runs of each that the smaller one totals twice the
floor (``sub_floor_repetitions``), and is decided there. The sizes do not
grow: the test's own pair is what catches work superlinear only up to the
test's size (IR78-01). Superlinear work still fails, on every model and on
the real clock.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import itertools
import math
import time
from typing import Callable

import pytest

from tests import host_timing
from tests.host_timing import (
    BORDERLINE_RETRIES,
    CONFIRM_MAJORITY,
    EXPONENT_MAX_FIRST_STEP,
    EXPONENT_MIN_SECOND_STEP,
    EXPONENT_SECONDS,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    MAX_GROWTH_REPETITIONS,
    SUB_FLOOR_MARGIN,
    SUB_FLOOR_READING_SECONDS,
    Confirmation,
    TimingCheckFailed,
    assert_linear_time,
    over_on_a_sub_floor_reading,
    sub_floor_repetitions,
)
from tests.test_host_timing_3171 import (
    _build_clock,
    _pair_work,
    _units_reading,
    timing_failure,
)
from tests.test_host_timing_3188 import _wall_clock
from tests.test_host_timing_3191 import _linear_units, _linear_work

# The CI run's sizes and readings.
CI_SIZE = 64 * 1024
CI_QUARTER = CI_SIZE // GROWTH
CI_LARGE_SECONDS = 0.0182
CI_QUIET_QUARTER_SECONDS = 0.00196
CI_BUDGET_SECONDS = 0.1
CI_LABEL = "'//'"
# The quarter's cost on linear work (4.55 ms, as here), and the share of it
# a call run inside a quiet spell reads: CI's 0.0020 s.
QUIET_RATE = CI_QUIET_QUARTER_SECONDS / (CI_LARGE_SECONDS / GROWTH)
# A call this short can run whole inside a quiet spell; every other one does.
QUIET_SPELL_SECONDS = 0.005
# Wall time per call (building 64 KB, and the reference workload of
# ``budget()`` once a second): enough that the own pair is not repeated
# (b81777b), as CI's message shows.
CI_CALL_WALL_SECONDS = 0.03
# A coarse CPU clock (tick-based accounting) reads whole ticks.
TICK_SECONDS = 0.01


def _quiet_spells(clock, power: float = 1, reading: float = CI_LARGE_SECONDS,
                  wall_seconds: float = CI_CALL_WALL_SECONDS,
                  linear_from: int = 0,
                  calls: list[int] | None = None) -> Callable[[int], float]:
    """``seconds_at`` of work costing ``reading`` at CI_SIZE and growing as
    size ** ``power`` (linearly past ``linear_from``, a window, when set),
    timed on a runner whose quiet spells every other call under
    QUIET_SPELL_SECONDS runs inside: it reads QUIET_RATE of its cost. Each
    call advances the wall clock by ``wall_seconds``."""
    short_calls = itertools.count()

    def cost(size: int) -> float:
        if linear_from and size > linear_from:
            return cost(linear_from) * size / linear_from
        return reading * (size / CI_SIZE) ** power

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        clock.now += wall_seconds
        seconds = cost(size)
        if seconds < QUIET_SPELL_SECONDS and next(short_calls) % 2 == 1:
            return seconds * QUIET_RATE
        return seconds

    return seconds_at


def _coarse_clock(clock, calls: list[int]) -> Callable[[int], float]:
    """CI's linear work (4.55 ms at 16 KB, 18.2 ms at 64 KB) on a CPU clock
    of TICK_SECONDS ticks: the quarter reads one tick, then none, in turn
    (it spans a tick about half the time); the larger size two ticks."""
    quarter_ticks = itertools.cycle((1, 0))

    def seconds_at(size: int) -> float:
        calls.append(size)
        clock.now += CI_CALL_WALL_SECONDS
        if size == CI_QUARTER:
            return next(quarter_ticks) * TICK_SECONDS
        return round(CI_LARGE_SECONDS * size / CI_SIZE / TICK_SECONDS
                     ) * TICK_SECONDS

    return seconds_at


def _before_3204(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before task 3204: a pair over the limit fails on its
    smaller reading whatever it totals."""
    monkeypatch.setattr(host_timing, "sub_floor_repetitions",
                        lambda small_seconds, repetitions, cost: repetitions)


def _spied_repetitions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The run counts ``sub_floor_repetitions`` hands out, in order."""
    handed_out: list[int] = []
    real = host_timing.sub_floor_repetitions

    def spy(small_seconds, repetitions, cost):
        handed_out.append(real(small_seconds, repetitions, cost))
        return handed_out[-1]

    monkeypatch.setattr(host_timing, "sub_floor_repetitions", spy)
    return handed_out


# --- The rule ----------------------------------------------------------------


def test_a_pair_over_only_on_a_sub_floor_reading_is_read_again():
    # CI's own pair: 1.96 ms totals under the noise floor, and 18.2 ms
    # against the floor is 0.9x: over only on the reading.
    assert over_on_a_sub_floor_reading(CI_QUIET_QUARTER_SECONDS,
                                       CI_LARGE_SECONDS)
    # Over the limit even against the noise floor: no reading can clear it.
    assert not over_on_a_sub_floor_reading(0.001, GROWTH_LIMIT
                                           * GROWTH_FLOOR_SECONDS)
    # A smaller reading at the noise floor is the work's cost.
    assert not over_on_a_sub_floor_reading(GROWTH_FLOOR_SECONDS, 0.17)
    # The mean of 16 runs of 1.96 ms totals 31 ms: over the floor.
    assert not over_on_a_sub_floor_reading(CI_QUIET_QUARTER_SECONDS,
                                           CI_LARGE_SECONDS, 16)


def test_the_runs_reach_twice_the_floor_within_their_wall_time():
    target = SUB_FLOOR_MARGIN * GROWTH_FLOOR_SECONDS
    assert sub_floor_repetitions([0.005], 1, 0.0) == math.ceil(target / 0.005)
    # The most suspicious part sets the runs.
    assert sub_floor_repetitions([0.005, 0.004], 1, 0.0) == 10
    # At most MAX_GROWTH_REPETITIONS runs, even of a zero reading.
    assert sub_floor_repetitions([0.0], 1, 0.0) == MAX_GROWTH_REPETITIONS
    # No more than one reading of SUB_FLOOR_READING_SECONDS pays for.
    assert sub_floor_repetitions([CI_QUIET_QUARTER_SECONDS], 1, 0.06) == int(
        SUB_FLOOR_READING_SECONDS / 0.06)
    # None fits: the runs the pair had, so it is not read again.
    assert sub_floor_repetitions([0.001], 3, 2.0) == 3


# --- CI's readings on a runner with quiet spells -----------------------------


def test_the_ci_failure_replays_on_the_check_before_3204(monkeypatch):
    """The replay is faithful: before the fix it fails with CI's message
    (own pair, one run of each size, 2 ms floor, 9.1x, three readings and
    3 of 3 confirming rounds of 11)."""
    _before_3204(monkeypatch)
    clock = _wall_clock(monkeypatch)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(_quiet_spells(clock), CI_SIZE, CI_BUDGET_SECONDS,
                           CI_LABEL)
    message = str(failure.value)
    assert (f"{CI_LABEL}: 0.0182 s at size {CI_SIZE}, 0.0020 s at size "
            f"{CI_QUARTER} (growth 9.1x at 1x the test's input, floor "
            f"0.002 s, limit 8.0x;" in message)
    assert ("each the fastest of 3 interleaved readings; confirmed: over "
            "the limit in 3 of 3 confirming rounds of 11 interleaved "
            "readings of each size)" in message)


def test_the_ci_readings_pass_read_again_over_more_runs(monkeypatch):
    """Fixed, the same readings pass: the own pair is read again over 16
    runs of each size (what a second of wall time pays for at 60 ms a
    pair), and the quarter's mean, half its calls in a quiet spell, reads
    5.6x: borderline, settled, under the limit."""
    clock = _wall_clock(monkeypatch)
    handed_out = _spied_repetitions(monkeypatch)
    calls: list[int] = []
    timing = assert_linear_time(_quiet_spells(clock, calls=calls), CI_SIZE,
                                CI_BUDGET_SECONDS, CI_LABEL)
    assert handed_out == [int(SUB_FLOOR_READING_SECONDS
                              / (2 * CI_CALL_WALL_SECONDS))] == [16]
    # The first reading over 16 runs and its borderline retries.
    assert calls.count(CI_QUARTER) >= 16 * (1 + BORDERLINE_RETRIES)
    # The growth then went on, as ever, to sizes reading over the floor.
    assert timing.small_size > CI_SIZE and timing.ratio < GROWTH_LIMIT


@pytest.mark.parametrize("build_seconds", (0.0, 0.03, 0.12))
@pytest.mark.parametrize("reading", (0.016, 0.0182, 0.02, 0.04))
def test_linear_work_in_quiet_spells_passes(monkeypatch, reading,
                                            build_seconds):
    """Linear work whose quarter calls (4-10 ms) can fit a quiet spell
    passes, also when building the shape takes 0.12 s of wall time per
    call (four runs of each size fit a second then)."""
    clock = _wall_clock(monkeypatch)
    timing = assert_linear_time(
        _quiet_spells(clock, reading=reading, wall_seconds=build_seconds),
        CI_SIZE, 10.0, "linear")
    assert timing.ratio < GROWTH_LIMIT


def test_no_run_affordable_fails_closed_as_before(monkeypatch):
    """When one more run of each size takes over SUB_FLOOR_READING_SECONDS
    of wall time, the pair is not read again: it fails as it did before
    the fix (fail closed), never passes unread."""
    clock = _wall_clock(monkeypatch)
    handed_out = _spied_repetitions(monkeypatch)
    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(_quiet_spells(clock, wall_seconds=0.6), CI_SIZE,
                           CI_BUDGET_SECONDS, CI_LABEL)
    assert handed_out == [1]


def test_a_coarse_clocks_zero_reading_is_never_compared(monkeypatch):
    """A CPU clock of 10 ms ticks reads the 4.55 ms quarter as one tick or
    none. Before the fix the zero reading, raised to the 2 ms floor, failed
    the pair at 10x; read again over 16 runs, the quarter's mean is one
    tick in two (5 ms), and the pair grows 4x."""
    clock = _wall_clock(monkeypatch)
    calls: list[int] = []
    _before_3204(monkeypatch)
    with pytest.raises(TimingCheckFailed,
                       match=r"0\.0200 s at size 65536, 0\.0000 s at size "
                             r"16384 \(growth 10\.0x at 1x"):
        assert_linear_time(_coarse_clock(clock, calls), CI_SIZE,
                           CI_BUDGET_SECONDS, CI_LABEL)
    monkeypatch.undo()
    clock = _wall_clock(monkeypatch)
    timing = assert_linear_time(_coarse_clock(clock, calls), CI_SIZE,
                                CI_BUDGET_SECONDS, CI_LABEL)
    assert timing.ratio < GROWTH_LIMIT


# --- Superlinear work still fails --------------------------------------------


@pytest.mark.parametrize("build_seconds", (0.0, 0.03, 0.12, 0.3))
@pytest.mark.parametrize("reading", (0.016, 0.02, 0.04, 0.08, 0.12, 0.159))
@pytest.mark.parametrize("windowed", (False, True),
                         ids=["quadratic", "windowed"])
def test_quadratic_work_in_quiet_spells_still_fails(monkeypatch, windowed,
                                                    reading, build_seconds):
    """IR75-01's quadratic work of 16-159 ms at the test's size, timed on a
    runner with quiet spells, fails at the test's own pair as before,
    whatever building the shape costs, and so does the same work linear
    past the test's size (a window, IR78-01): reading the pair again over
    more runs never hides it."""
    clock = _wall_clock(monkeypatch)
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: quadratic: .* at 1x the "
                             r"test's input"):
        assert_linear_time(
            _quiet_spells(clock, power=2, reading=reading,
                          wall_seconds=build_seconds,
                          linear_from=CI_SIZE if windowed else 0,
                          calls=calls),
            CI_SIZE, 10.0, "quadratic")
    assert max(calls) == CI_SIZE


def test_a_regression_read_again_says_so(monkeypatch):
    """Quadratic work 18.2 ms at the test's size, whose own pair one run of
    each size decided (b81777b: 0.12 s of wall time a call), is read again
    over the four runs a second pays for (0.24 s a pair) and fails there,
    naming the reading it was read again from."""
    clock = _wall_clock(monkeypatch)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(_quiet_spells(clock, power=2, wall_seconds=0.12),
                           CI_SIZE, CI_BUDGET_SECONDS, "quadratic")
    message = str(failure.value)
    assert "at 1x the test's input over 4 runs of each" in message
    assert ("read again over 4 runs of each size: the smaller reading over "
            "the limit was" in message)


# --- Real work on the real clock, read short in quiet spells -----------------


def _read_short_in_quiet_spells(work: Callable[[int], object]
                                ) -> Callable[[int], float]:
    """CPU seconds of ``work(units)`` on the real clock, every other reading
    under QUIET_SPELL_SECONDS scaled by QUIET_RATE: a runner's quiet spell
    laid over this host's clock."""
    short_calls = itertools.count()

    def seconds_at(units: int) -> float:
        started = time.process_time()
        work(units)
        seconds = time.process_time() - started
        if seconds < QUIET_SPELL_SECONDS and next(short_calls) % 2 == 1:
            return seconds * QUIET_RATE
        return seconds

    return seconds_at


def test_real_linear_work_read_short_in_quiet_spells_passes():
    """Real linear work reading about 18 ms at the test's size (CI's
    pattern 24), its quarter read short in quiet spells, passes."""
    units = _linear_units(CI_LARGE_SECONDS)
    timing = assert_linear_time(_read_short_in_quiet_spells(_linear_work),
                                units, 30.0, "real linear")
    assert timing.ratio < GROWTH_LIMIT


def test_real_quadratic_work_read_short_in_quiet_spells_fails():
    """Real quadratic work reading about 25 ms at the test's size, its
    quarter (under 2 ms) read short in quiet spells, fails at the test's
    own pair."""
    units = _units_reading(0.025)
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth: real quadratic: .* at 1x "
                             r"the test's input"):
        assert_linear_time(_read_short_in_quiet_spells(_pair_work), units,
                           30.0, "real quadratic")


# --- R3191-01: only a cache step is cleared on its exponent ------------------

SIZE = 40_000
READING = 0.04


def _shaped(cost: Callable[[float], float],
            calls: list[int] | None = None) -> Callable[[int], float]:
    """Seconds of work reading READING times ``cost(size / SIZE)``."""
    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        return READING * cost(size / SIZE)

    return seconds_at


def _steps(first: float, second: float) -> Callable[[float], float]:
    """Linear up to the test's size, then ``first`` times over the first
    grown pair (N to 4N) and ``second`` times over the step past it."""
    def cost(scale: float) -> float:
        if scale <= 1:
            return scale
        if scale <= GROWTH:
            return scale ** math.log(first, GROWTH)
        return first * (scale / GROWTH) ** math.log(second, GROWTH)

    return cost


def _capped(start: float, cap: float) -> Callable[[float], float]:
    """Quadratic from ``start`` times the test's size, its input capped at
    ``cap`` times it (truncated: no dearer past the cap)."""
    def cost(scale: float) -> float:
        scale = min(scale, cap)
        return scale if scale <= start else start * (scale / start) ** 2

    return cost


def _windowed(start: float, end: float) -> Callable[[float], float]:
    """Quadratic from ``start`` to ``end`` times the test's size, linear
    past it (a window)."""
    def cost(scale: float) -> float:
        if scale <= start:
            return scale
        if scale <= end:
            return start * (scale / start) ** 2
        return start * (end / start) ** 2 * scale / end

    return cost


def _without_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exponent rule before R3191-01: any outer pair under the limit
    clears the part."""
    monkeypatch.setattr(host_timing, "EXPONENT_MAX_FIRST_STEP", math.inf)
    monkeypatch.setattr(host_timing, "EXPONENT_MIN_SECOND_STEP", 0.0)


def test_the_window_holds_cis_steps():
    assert EXPONENT_MAX_FIRST_STEP == pytest.approx(15.0)
    assert EXPONENT_MIN_SECOND_STEP == pytest.approx(GROWTH / 1.5)
    # CI's 3191 readings: 8.1x, then linear.
    assert 8.1 <= EXPONENT_MAX_FIRST_STEP and GROWTH >= EXPONENT_MIN_SECOND_STEP


@pytest.mark.parametrize("name, cost", [
    ("2x cache step", _steps(8.2, GROWTH)),
    ("3x cache step", _steps(12.0, GROWTH)),
    ("3.5x cache step", _steps(14.0, GROWTH)),
    ("3.7x cache step, under the bound", _steps(14.8, GROWTH)),
    ("12x then 2.7x", _steps(12.0, 2.7)),
    # The trade-off three sizes cannot avoid: a window ending inside the
    # pair reads as a cache step does (12x then 4x is a 3x step at 3N),
    # and one ending past it as a step then a steeper linear stretch.
    ("window 1N-3N, read as a 3x cache step", _windowed(1.0, 3.0)),
    ("window 1.5N-3.5N, read as a 2.3x cache step", _windowed(1.5, 3.5)),
    ("window 1.5N-5.5N, 10.7x then 5.5x", _windowed(1.5, 5.5)),
], ids=lambda value: value if isinstance(value, str) else "")
def test_cache_step_shapes_pass_on_their_exponent(monkeypatch, name, cost):
    _build_clock(monkeypatch)
    timing = assert_linear_time(_shaped(cost), SIZE, 60.0, name)
    assert timing.exponent is not None and not timing.exponent.superlinear
    assert timing.exponent.cache_step


@pytest.mark.parametrize("window", (True, False),
                         ids=["3204", "before-R3191-01"])
@pytest.mark.parametrize("name, cost, third_read", [
    ("3.9x step", _steps(15.6, GROWTH), False),
    ("12x then 2.6x", _steps(12.0, 2.6), True),
    ("quadratic 1N-4N, capped at 4N", _capped(1.0, 4.0), False),
    ("quadratic 1.2N-3.5N, capped at 3.5N", _capped(1.2, 3.5), True),
    ("quadratic 1.2N-5N, capped at 5N", _capped(1.2, 5.0), True),
    ("quadratic 1.5N-6N, capped at 6N", _capped(1.5, 6.0), True),
    ("window 1N-3.9N", _windowed(1.0, 3.9), False),
    ("window 1.05N-4.15N", _windowed(1.05, 4.15), False),
], ids=lambda value: value if isinstance(value, str) else "")
def test_capped_and_windowed_shapes_fail_where_main_failed(
        monkeypatch, window, name, cost, third_read):
    """Superlinear work capped or windowed just past the grown pair reads
    10-16x then flat or linear, under the outer limit (62x at most here):
    without the window it passes on its exponent (R3191-01's escape); with
    it it fails as main failed it, and a first step over the window's 15x
    never reads the third size (R3191-03)."""
    _build_clock(monkeypatch)
    if not window:
        _without_the_window(monkeypatch)
    calls: list[int] = []
    failure = timing_failure(lambda: assert_linear_time(
        _shaped(cost, calls), SIZE, 60.0, name))
    if not window:
        assert failure is None, str(failure)
        return
    assert failure is not None and f"superlinear growth: {name}" in str(
        failure)
    assert (GROWTH ** 2 * SIZE in calls) == third_read
    if third_read:
        assert "not a cache step" in str(failure)


# --- R3191-02: rounds cut short fail closed ----------------------------------


@pytest.mark.parametrize("rounds, rounds_over, confirmed", [
    (0, 0, True),   # no evidence
    (1, 1, True),
    (1, 0, False),  # every round read under
    (2, 1, True),   # a tie is no majority: before the fix it cleared
    (2, 2, True),
    (2, 0, False),
    (3, 1, False),  # a majority under, as before
    (3, 2, True),
])
def test_rounds_cut_short_clear_only_when_every_round_read_under(
        rounds, rounds_over, confirmed):
    confirmation = Confirmation(rounds, rounds_over, 3)
    assert confirmation.cut_short == (rounds < CONFIRM_MAJORITY)
    assert confirmation.confirmed is confirmed


def test_a_deadline_after_two_rounds_of_a_tie_keeps_the_part_over(monkeypatch):
    """The confirming rounds of an outer pair are stopped by its deadline
    after two rounds, one over the limit and one under: no majority either
    way, so the part stays over (it cleared before the fix)."""
    clock = _wall_clock(monkeypatch)
    larger = iter([0.48, 0.12])

    def measure(size: int) -> dict[str, float]:
        clock.now += 1.0
        return {"part": 0.03 if size == 1000 else next(larger)}

    pair = host_timing._Pair(1000, 4000, 1.0, GROWTH_FLOOR_SECONDS)
    _, _, still_over, confirmations = host_timing._confirmed(
        measure, pair, {"part": 0.03}, {"part": 0.48}, ["part"],
        deadline=4.5, reread_seconds=2.0)
    confirmation = confirmations["part"]
    assert (confirmation.rounds, confirmation.rounds_over) == (2, 1)
    assert not 2 * confirmation.rounds_over > confirmation.rounds
    assert still_over == ["part"]
    assert "cut short" in confirmation.describe()


# --- R3191-03: the third size's cost is bounded ------------------------------


@pytest.mark.parametrize("larger_wall_seconds, third_read", [
    (2.0, True),    # could clear: at most 64 / 12 x 2 s = 10.7 s
    (3.0, False),   # 16 s, over EXPONENT_SECONDS: fail closed on the pair
])
def test_a_third_size_it_could_clear_is_read_within_the_budget(
        monkeypatch, larger_wall_seconds, third_read):
    """A 3x cache step whose larger size costs ``larger_wall_seconds`` of
    wall time: a third size that clears it reads under the outer limit, at
    most 64 / 12 times the larger size, so it is read only when that fits
    ``EXPONENT_SECONDS``; otherwise the part fails on the pair."""
    clock = _wall_clock(monkeypatch)
    cost = _steps(12.0, GROWTH)
    calls: list[int] = []

    def seconds_at(size: int) -> float:
        calls.append(size)
        scale = size / SIZE
        clock.now += larger_wall_seconds * cost(scale) / cost(GROWTH)
        return READING * cost(scale)

    third = GROWTH ** 2 * SIZE
    assert ((larger_wall_seconds * GROWTH_LIMIT ** 2 / 12.0
             <= EXPONENT_SECONDS) == third_read)
    failure = timing_failure(lambda: assert_linear_time(
        seconds_at, SIZE, 600.0, "3x cache step"))
    assert (third in calls) == third_read
    assert (failure is None) == third_read
    if not third_read:
        assert "growth exponent" not in str(failure)
