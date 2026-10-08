"""A pair cleared on a sub-floor re-read never passes a part it did not
judge (task 3207, R3204-I-01 of the independent 3204 review).

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

from typing import Callable, Mapping

import pytest

from tests import host_timing
from tests.host_timing import (
    InputTooLarge,
    TimingCheckFailed,
    assert_linear_times,
)
from tests.test_host_timing_3188 import _WallClock, _wall_clock

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
