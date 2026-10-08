"""A pair read again on a sub-floor reading is no looser than the check
before task 3204 (task 3207, R3204-I-01 and R3204-I-02 of the independent
3204 review).

R3204-I-02: a pair is read again only for parts its confirming rounds kept
over the limit on the fastest readings. cbba569 then cleared a part on one
settle pass over means of several runs, and a mean is far likelier to carry
a burst than the fastest single reading: in the review's seeded noise
simulation (38,400 trials) 365 regressions that 6ef24c6 failed passed (479
escapes against ea2bf69's verdicts, 115 on 6ef24c6). A pair read again now
confirms every part it judges and clears one only when every one of
``CONFIRM_ROUNDS`` rounds reads it under the limit (``Confirmation
.unanimous``): 114 escapes, none that 6ef24c6 failed, linear false failures
unchanged at 10 of 4,800; CI's quiet-spell readings still pass.

R3204-I-01:
``_check_growth`` caches the verdict of a pair it read again (``cleared``)
so the pair the growth stopped at, when it is that pair again, is not
confirmed twice. On cbba569 the cache was keyed on the pair alone. A pair
read again for SOME parts (the own pair's nested sub-floor re-read, judging
only the parts over on a sub-floor reading) can equal a later pair over
OTHER parts (the stop pair's re-read at the same runs), and the later pair
then took the first one's verdict: a part confirmed over the limit passed.

The reproduction (the review's, on fake readings and the harness's fake
wall clock): two parts at N = 64 KB, sizes past N raise ``InputTooLarge``
(growth exhausted at the test's sizes, so the stop pair is on main's 2 ms
floor). Part A is a real 16x regression whose quarter reads loaded while the
own pair is held; part B is linear with CI's quiet-spell quarter readings.
Each time the harness asks ``sub_floor_repetitions`` the readings move to
the next phase (the load changing):

====================  ==========================  ==========================
phase                 A quarter / large           B quarter / large
====================  ==========================  ==========================
first call            9.5 ms / 72 ms              6 ms / 37 ms
P0: own pair (r = 3)  9.5 / 72 (7.6x, passes)     4.45 / 37: 8.3x, sub-floor
P1: re-read           -                           2.0 / 17: sub-floor again
P2+: quiet            4.5 / 72 (16x)              4.5 / 17
====================  ==========================  ==========================

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import itertools
from typing import Callable, Mapping

import pytest

from tests import host_timing
from tests.host_timing import (
    CONFIRM_MAJORITY,
    CONFIRM_ROUNDS,
    Confirmation,
    InputTooLarge,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
)
from tests.test_host_timing_3188 import _WallClock, _wall_clock
from tests.test_host_timing_3204 import (
    CI_BUDGET_SECONDS,
    CI_LABEL,
    CI_SIZE,
    _quiet_spells,
)

N = 64 * 1024
Q = N // 4
LABEL = "repro"

# One reading per phase: (A small, A large, B small, B large). FIRST is the
# first call at each size (the readings the own pair starts from).
Phase = tuple[float, float, float, float]
FIRST: Phase = (0.0095, 0.072, 0.006, 0.037)
OWN_PAIR: Phase = (0.0095, 0.072, 0.00445, 0.037)
QUIET_SPELL: Phase = (0.0045, 0.072, 0.002, 0.017)
QUIET: Phase = (0.0045, 0.072, 0.0045, 0.017)
# B reads under the floor again when the own pair is read again: its
# re-read is read again (a nested pair judging B alone, then cached).
NESTED_PHASES = (OWN_PAIR, QUIET_SPELL, QUIET)
# Control: B passes at its first re-read, so nothing partial is cached.
CONTROL_PHASES = (OWN_PAIR, QUIET, QUIET)


def _scripted(clock: _WallClock, asked: list[tuple[int, int]],
              phases: tuple[Phase, Phase, Phase]
              ) -> Callable[[int], Mapping[str, float]]:
    """Readings of parts A and B at Q and N, one phase further each time
    ``sub_floor_repetitions`` was asked; each call takes 1 ms of the fake
    wall clock."""
    seen = {Q: 0, N: 0}

    def seconds_at(size: int) -> Mapping[str, float]:
        if size not in seen:
            raise InputTooLarge(size)
        clock.now += 0.001
        phase = FIRST if seen[size] == 0 else phases[min(len(asked), 2)]
        seen[size] += 1
        a_small, a_large, b_small, b_large = phase
        if size == Q:
            return {"A": a_small, "B": b_small}
        return {"A": a_large, "B": b_large}

    return seconds_at


def _asked_sub_floor_repetitions(monkeypatch: pytest.MonkeyPatch
                                 ) -> list[tuple[int, int]]:
    """Each ``sub_floor_repetitions`` answer as (runs before, runs after)."""
    asked: list[tuple[int, int]] = []
    real = host_timing.sub_floor_repetitions

    def spy(small_seconds, repetitions, pair_cost_seconds):
        runs = real(small_seconds, repetitions, pair_cost_seconds)
        asked.append((repetitions, runs))
        return runs

    monkeypatch.setattr(host_timing, "sub_floor_repetitions", spy)
    return asked


@pytest.mark.parametrize("phases", [NESTED_PHASES, CONTROL_PHASES],
                         ids=["nested-re-read", "control"])
def test_a_part_over_the_limit_fails_after_another_parts_re_read(
        monkeypatch: pytest.MonkeyPatch,
        phases: tuple[Phase, Phase, Phase]) -> None:
    monkeypatch.setenv(host_timing.HOST_FACTOR_ENVIRONMENT_VARIABLE, "1.0")
    clock = _wall_clock(monkeypatch)
    asked = _asked_sub_floor_repetitions(monkeypatch)

    with pytest.raises(TimingCheckFailed) as failure:
        assert_linear_times(_scripted(clock, asked, phases), N, 1.0, LABEL)

    message = str(failure.value)
    assert f"{LABEL}: A" in message
    assert "growth 16.0x" in message
    # B is linear: it is never reported as growth.
    assert f"{LABEL}: B" not in message
    # The own pair read B again (it read under the floor), so the path the
    # cache sits on is the one this test exercises.
    assert asked and asked[0][1] > asked[0][0]


def test_the_nested_re_read_judges_b_alone_before_the_stop_pair(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction's premise: B's re-read is read again (a nested pair
    over B alone), and the stop pair asks for a re-read of A afterwards.
    Without both, the cache holds nothing partial and the test above would
    pass on cbba569 too."""
    monkeypatch.setenv(host_timing.HOST_FACTOR_ENVIRONMENT_VARIABLE, "1.0")
    clock = _wall_clock(monkeypatch)
    asked = _asked_sub_floor_repetitions(monkeypatch)
    judged: list[list[str]] = []
    real_settled = host_timing._settled_readings

    def settled(measure, pair, small, large, parts, *args, **kwargs):
        judged.append(sorted(parts))
        return real_settled(measure, pair, small, large, parts, *args,
                            **kwargs)

    monkeypatch.setattr(host_timing, "_settled_readings", settled)

    with pytest.raises(TimingCheckFailed):
        assert_linear_times(_scripted(clock, asked, NESTED_PHASES), N, 1.0,
                            LABEL)

    assert len(asked) >= 3
    assert asked[1][0] == asked[0][1] and asked[1][1] > asked[1][0]
    assert ["B"] in judged and ["A"] in judged


# --- R3204-I-02: every round of a pair read again must clear a part ----------


@pytest.mark.parametrize("rounds, rounds_over, confirmed", [
    (CONFIRM_ROUNDS, 0, False),
    (CONFIRM_ROUNDS, 1, True),
    (1, 1, True),
    # A majority under is not enough, and neither is a cut-short run of
    # rounds that all read under.
    (CONFIRM_MAJORITY, 0, True),
    (CONFIRM_ROUNDS - 1, 0, True),
    (0, 0, True),
])
def test_unanimous_rounds_clear_only_when_every_round_reads_under(
        rounds: int, rounds_over: int, confirmed: bool) -> None:
    confirmation = Confirmation(rounds, rounds_over, 3, unanimous=True)
    assert confirmation.confirmed is confirmed
    if rounds:
        assert (f"cleared only when every one of {CONFIRM_ROUNDS} rounds "
                f"reads under the limit" in confirmation.describe())


def test_the_majority_rule_is_unchanged_for_every_other_pair() -> None:
    # Two of five rounds over: the majority clears it, unanimity does not.
    assert not Confirmation(CONFIRM_ROUNDS, 2, 3).confirmed
    assert Confirmation(CONFIRM_ROUNDS, 2, 3, unanimous=True).confirmed


# A 12x regression timed on a runner whose calls take 0.12 s of wall time:
# one run of each size decides the own pair (b81777b), whose quarter is
# under the noise floor, so the pair is read again over the four runs a
# second pays for.
REGRESSION_QUARTER = 0.0015
REGRESSION_LARGE = 0.018
CALL_WALL_SECONDS = 0.12
READ_AGAIN_RUNS = 4
# The review's burst model runs a quarter of the calls up to 10x their cost.
BURST = 10.0


def _burst_hidden_regression(clock: _WallClock, asked: list[tuple[int, int]]
                             ) -> Callable[[int], float]:
    """``seconds_at`` of the regression. Once the pair is read again (once
    ``sub_floor_repetitions`` was asked), the first two means of the quarter
    (its first reading and its settle retry) each carry one burst run, the
    review's example (both quarter means held a burst, the ratio read 3.7x,
    under the borderline band); every later call is quiet. Sizes past the
    test's do not exist (growth is exhausted)."""
    quarter_calls_read_again = itertools.count()

    def seconds_at(size: int) -> float:
        clock.now += CALL_WALL_SECONDS
        if size == N:
            return REGRESSION_LARGE
        if size != Q:
            raise InputTooLarge(size)
        if not asked:
            return REGRESSION_QUARTER
        index = next(quarter_calls_read_again)
        if (index < 2 * READ_AGAIN_RUNS
                and index % READ_AGAIN_RUNS == READ_AGAIN_RUNS - 1):
            return REGRESSION_QUARTER * BURST
        return REGRESSION_QUARTER

    return seconds_at


def test_a_regression_whose_re_read_means_carry_a_burst_still_fails(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """cbba569 passed it on the settle pass (two means of 4.9 ms against
    18 ms: 3.7x). Confirmed in unanimous rounds, the first round reads the
    quarter's cost and fails it at 9x."""
    monkeypatch.setenv(host_timing.HOST_FACTOR_ENVIRONMENT_VARIABLE, "1.0")
    clock = _wall_clock(monkeypatch)
    asked = _asked_sub_floor_repetitions(monkeypatch)

    with pytest.raises(TimingCheckFailed,
                       match="superlinear growth") as failure:
        assert_linear_time(_burst_hidden_regression(clock, asked), N, 10.0,
                           "regression")

    # Read again over four runs; still over there, no more runs fit.
    assert asked[0] == (1, READ_AGAIN_RUNS)
    assert all(before == after for before, after in asked[1:])
    message = str(failure.value)
    assert (f"regression: {REGRESSION_LARGE:.4f} s at size {N}, "
            f"{REGRESSION_QUARTER:.4f} s at size {Q} (growth 9.0x at 1x the "
            f"test's input over {READ_AGAIN_RUNS} runs of each" in message)
    assert ("confirmed: over the limit in 1 of 1 confirming rounds" in message
            and f"every one of {CONFIRM_ROUNDS} rounds" in message)


def test_ci_readings_read_again_clear_only_after_every_round(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """CI's linear pattern on the quiet-spell runner (task 3204's replay)
    still passes, and the pair read again cleared it only after all
    ``CONFIRM_ROUNDS`` rounds read it under the limit."""
    clock = _wall_clock(monkeypatch)
    verdicts: list[Confirmation] = []
    real_confirmed = host_timing._confirmed

    def confirmed(measure, pair, small, large, over, *args, **kwargs):
        result = real_confirmed(measure, pair, small, large, over, *args,
                                **kwargs)
        if pair.read_again:
            verdicts.extend(result[3].values())
        return result

    monkeypatch.setattr(host_timing, "_confirmed", confirmed)

    timing = assert_linear_time(_quiet_spells(clock), CI_SIZE,
                                CI_BUDGET_SECONDS, CI_LABEL)

    assert timing.ratio < host_timing.GROWTH_LIMIT
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.unanimous and not verdict.confirmed
    assert (verdict.rounds, verdict.rounds_over) == (CONFIRM_ROUNDS, 0)
