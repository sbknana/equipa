"""The growth checks also judge the test's own pair of sizes (task 3180).

Task 3178 grew the INPUT while the quarter reading was under the 20 ms
floor, and decided the growth on the grown pair alone (IR75-01). Work that
is superlinear up to the test's size and cheaper past it (a window, an
input cap or a truncation the larger size reaches) then grows linearly at
the sizes that decided it, and passed where pre-3175 main (883c9f2, 2 ms
floor) and the default branch b81777b (repetitions) failed it (IR78-01):
3129's ``sanitize`` truncates at 64,000 characters, just over its
60,000-character test size, and a quadratic regression planted there read
105-157 ms and passed. A fast linear part of a multi-part check also
carried a quadratic part to 16 times the test's input (IR78-02).

``tests/host_timing.py`` now holds the test's own pair to main's rule and,
for one ``seconds_at``, to b81777b's repetitions; every grown pair to
main's rule as well. These tests plant each shape (modelled, real work on
the real clock, and a regression in the real ``sanitize`` behind its real
test) and pin the verdict.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import statistics
import time
from typing import Callable

import pytest

from tests import host_timing
from tests import test_lesson_sanitizer_3129 as sanitizer_tests
from tests.host_timing import (
    GROWTH,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
)
from tests.test_host_timing_3171 import (
    IR75_01_LARGE_READINGS,
    IR75_01_SIZE,
    _build_clock,
    _units_reading,
    planted_band,
    timing_failure,
)

MAX_SANITIZE_INPUT_LENGTH = sanitizer_tests.ls.MAX_SANITIZE_INPUT_LENGTH
SIZE = IR75_01_SIZE
# An input cap just over the test's size, as 3129's 64,000-character cap
# sits over its 60,000-character test size.
CAP = SIZE * MAX_SANITIZE_INPUT_LENGTH // 60_000


def _shape(name: str, reading: float) -> Callable[[int], float]:
    """Seconds at size ``n`` of a planted shape reading ``reading`` at SIZE."""
    shapes = {
        # Quadratic everywhere.
        "quadratic": lambda n: n * n,
        # Quadratic up to the test's size, linear past it (a window).
        "windowed": lambda n: min(n, SIZE) * n,
        # Quadratic up to twice the test's size, linear past it.
        "windowed2": lambda n: min(n, 2 * SIZE) * n,
        # Quadratic in the input left under a cap the larger size reaches.
        "capped": lambda n: min(n, CAP) ** 2,
        "linear": lambda n: n * SIZE,
        "capped-linear": lambda n: min(n, CAP) * SIZE,
    }
    work = shapes[name]
    return lambda n: reading * work(n) / work(SIZE)


SUPERLINEAR_SHAPES = ("quadratic", "windowed", "windowed2", "capped")
LINEAR_SHAPES = ("linear", "capped-linear")


def _built(clock, seconds_at: Callable[[int], float], build_seconds: float,
           calls: list[int] | None = None) -> Callable[[int], float]:
    """``seconds_at`` after a build of ``build_seconds`` at SIZE (linear in
    the size) on the wall clock ``tests/host_timing.py`` reads."""
    def built(size: int) -> float:
        if calls is not None:
            calls.append(size)
        clock.now += build_seconds * size / SIZE
        return seconds_at(size)

    return built


# --- IR78-01: superlinear work that gets cheaper past the test's size ------------


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
@pytest.mark.parametrize("shape", SUPERLINEAR_SHAPES)
def test_superlinear_work_of_16_to_159_ms_fails_at_the_tests_own_sizes(
        monkeypatch, shape, reading, build_seconds):
    """883c9f2 failed every one of these on the test's own pair (2 ms
    floor); 292395d passed the windowed and capped shapes from 20 ms on.
    The test's own pair decides now, whatever growing the input shows."""
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    seconds_at = _built(clock, _shape(shape, reading), build_seconds, calls)
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, SIZE, 10.0, shape)
    assert "at 1x the test's input" in str(failure.value)
    assert max(calls) == SIZE


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
@pytest.mark.parametrize("shape", SUPERLINEAR_SHAPES)
def test_a_superlinear_part_of_16_to_159_ms_fails_at_the_tests_own_sizes(
        monkeypatch, shape, reading, build_seconds):
    """IR78-01 in ``assert_linear_times``, beside a linear 10 ms part:
    only the superlinear part is named."""
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    superlinear = _built(clock, _shape(shape, reading), build_seconds, calls)

    def seconds_at(size: int) -> dict[str, float]:
        return {"linear": 0.01 * size / SIZE, shape: superlinear(size)}

    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_times(seconds_at, SIZE, 10.0, "parts")
    assert f"parts: {shape}:" in str(failure.value)
    assert "parts: linear" not in str(failure.value)
    assert max(calls) == SIZE


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("reading",
                         (0.001, 0.005) + IR75_01_LARGE_READINGS + (0.5,))
@pytest.mark.parametrize("shape", LINEAR_SHAPES)
def test_linear_work_that_stops_growing_past_the_tests_size_passes(
        monkeypatch, shape, reading, build_seconds):
    clock = _build_clock(monkeypatch)
    timing = assert_linear_time(
        _built(clock, _shape(shape, reading), build_seconds), SIZE, 10.0,
        shape)
    assert timing.ratio < host_timing.GROWTH_LIMIT
    timings = assert_linear_times(
        lambda size: {"a": _shape(shape, reading)(size),
                      "b": 0.01 * size / SIZE},
        SIZE, 10.0, "parts")
    assert all(part.ratio < host_timing.GROWTH_LIMIT
               for part in timings.values())


# --- b81777b: the test's own pair repeated under the floor -----------------------


@pytest.mark.parametrize("reading", (0.006, 0.008, 0.012, 0.0159))
@pytest.mark.parametrize("shape", SUPERLINEAR_SHAPES)
def test_superlinear_work_under_16_ms_fails_once_repeated(monkeypatch, shape,
                                                          reading):
    """b81777b measured both sizes of a cheap call again until the quarter
    size totalled 20 ms, so it failed quadratic work reading 6-16 ms that
    main's floor and 292395d passed. Its rule holds for the test's own
    pair."""
    clock = _build_clock(monkeypatch)
    with pytest.raises(TimingCheckFailed,
                       match=r"at 1x the test's input over \d+ runs of each"):
        assert_linear_time(_built(clock, _shape(shape, reading), 0.0), SIZE,
                           10.0, shape)
    # A part of cheap calls too: 16 ms is main's boundary (16 ms against
    # its 2 ms floor), so readings of 15.6-16.0 ms passed there.
    superlinear = _built(clock, _shape(shape, reading), 0.0)
    with pytest.raises(TimingCheckFailed,
                       match=rf"parts: {shape}: .* over \d+ runs of each"):
        assert_linear_times(
            lambda size: {"linear": 0.01 * size / SIZE,
                          shape: superlinear(size)},
            SIZE, 10.0, "parts")


@pytest.mark.parametrize("reading", (0.006, 0.0159))
def test_a_part_of_expensive_calls_is_not_repeated(monkeypatch, reading):
    """A part timed by one child-process run per call (3139's families) is
    repeated only as far as GROWTH_REPETITION_SECONDS pays for; past that,
    main's 2 ms floor decides, as before."""
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    quadratic = _built(clock, _shape("quadratic", reading), 2.0, calls)
    timings = assert_linear_times(lambda size: {"q": quadratic(size)}, SIZE,
                                  10.0, "parts")
    assert calls == [SIZE // GROWTH, SIZE]
    assert timings["q"].repetitions == 1


@pytest.mark.parametrize("reading", (0.006, 0.008, 0.012, 0.0159))
def test_linear_work_under_16_ms_is_not_repeated(monkeypatch, reading):
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    timing = assert_linear_time(
        _built(clock, _shape("linear", reading), 0.0, calls), SIZE, 10.0,
        "linear")
    assert calls == [SIZE // GROWTH, SIZE]
    assert timing.repetitions == 1


def test_repetitions_are_bounded_by_what_a_call_costs():
    """A call that builds an expensive shape around a 5 ms measurement is
    repeated only as far as GROWTH_REPETITION_SECONDS pays for; main's 2 ms
    floor then decides."""
    repetitions = host_timing.growth_repetitions
    cost = host_timing.GROWTH_REPETITION_SECONDS
    assert repetitions(0.005) == 4
    assert repetitions(0.005, cost / 2) == 3
    assert repetitions(0.005, cost * 4) == 1
    assert repetitions(0.0001) == host_timing.MAX_GROWTH_REPETITIONS
    assert repetitions(0.05, cost * 4) == 1
    # Linear work under the floor cannot reach the limit once repeated.
    assert repetitions(0.001, 0.0, 0.004) == 1
    assert host_timing.own_pair_floor(1) == host_timing.DETECTION_FLOOR_SECONDS
    assert host_timing.own_pair_floor(20) == pytest.approx(0.001)


# --- IR78-02: a part over the limit fails where it is over -----------------------


def _late(reading: float) -> Callable[[int], float]:
    """Linear up to SIZE, quadratic past it: reading ``reading`` at SIZE."""
    return lambda n: reading * n / SIZE * max(1.0, n / SIZE)


@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
def test_a_fast_part_never_carries_a_quadratic_part_past_its_sizes(reading):
    """292395d measured a quadratic part again at 4 and 16 times the test's
    input while a fast linear part grew (24-134 s per check at 30-159 ms).
    A pure quadratic part fails at the test's own pair; one quadratic only
    past the test's size fails at the first grown pair it is over at."""
    # The late shape's first grown pair: the scaled step a 16-20 ms reading
    # takes (``next_quarter_size``), 4 times over.
    first_grown_size = GROWTH * host_timing.next_quarter_size(
        SIZE, reading, SIZE * host_timing.MAX_INPUT_GROWTH)
    cases = [(_shape("quadratic", reading), SIZE)]
    if reading / GROWTH < host_timing.GROWTH_FLOOR_SECONDS:
        # From 80 ms on, the late shape's quarter reads the floor: it is
        # decided at the test's sizes, where it is linear (as in main).
        cases.append((_late(reading), first_grown_size))
    for superlinear, largest in cases:
        calls: list[int] = []

        def seconds_at(size: int, superlinear=superlinear, calls=calls
                       ) -> dict[str, float]:
            calls.append(size)
            return {"fast": 0.01 * size / SIZE, "slow": superlinear(size)}

        with pytest.raises(TimingCheckFailed, match="parts: slow") as failure:
            assert_linear_times(seconds_at, SIZE, 10.0, "parts")
        assert "parts: fast" not in str(failure.value)
        assert max(calls) <= largest


# --- Real work on the real clock -------------------------------------------------


def _windowed_pair_work(units: int, window: int) -> int:
    """Pure-Python work over min(units, window) x units pairs: quadratic up
    to ``window`` units, linear past it."""
    total = 0
    for row in range(min(units, window)):
        for column in range(units):
            total ^= row + column
    return total


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("target_seconds", (0.02, 0.12))
def test_real_windowed_work_of_16_to_159_ms_fails(target_seconds,
                                                  build_seconds):
    """IR78-01 on real work: quadratic up to the test's size and linear
    past it, reading about 20 ms or 120 ms at the test's size. The size is
    calibrated on this host and the reading asserted inside the band."""
    lower, upper = planted_band(target_seconds)
    for _ in range(3):
        units = _units_reading(target_seconds)
        readings: dict[int, list[float]] = {}

        def seconds_at(size: int, units=units, readings=readings) -> float:
            time.sleep(build_seconds * size / units)
            started = time.process_time()
            _windowed_pair_work(size, units)
            elapsed = time.process_time() - started
            readings.setdefault(size, []).append(elapsed)
            return elapsed

        failure = timing_failure(lambda: assert_linear_time(
            seconds_at, units, 30.0, "planted windowed"))
        planted = readings[units][0]
        if lower <= planted <= upper:
            break
    assert lower <= planted <= upper, (
        f"the planted reading {planted:.4f} s at {units} units missed the "
        f"{lower:.4f}-{upper:.4f} s band on this host")
    assert failure is not None and "superlinear growth" in str(failure), (
        f"windowed work reading {planted:.4f} s passed: {readings}")


# The reviewer's plant (IR78-01): quadratic work in ``sanitize`` right after
# its input cap, so the 240,000-character grown size reads what 64,000 do.
# Its cost is CPU time spun out to ``seconds_per_square`` x length ** 2, so
# it reads the same on any host and under any load.
PLANTED_SANITIZER = """\
import importlib.util
import sys
import time

sys.path.insert(0, {repo_root!r})
_spec = importlib.util.spec_from_file_location("lesson_sanitizer",
                                               {module_path!r})
_real = importlib.util.module_from_spec(_spec)
sys.modules["lesson_sanitizer"] = _real
_spec.loader.exec_module(_real)
_cap_input = _real._cap_input


def _planted_cap_input(text, *, label):
    text = _cap_input(text, label=label)
    spin_until = time.process_time() + {seconds_per_square!r} * len(text) ** 2
    while time.process_time() < spin_until:
        pass
    return text


_real._cap_input = _planted_cap_input
"""
# The reviewer read the unplanted sanitize at about 31 ms and the plant at
# 105-157 ms; a slower host scales the band by its own unplanted reading
# (over 26 ms, so the plant stays at least 4 times the linear work: 10x
# growth at the test's own pair).
PLANTED_SANITIZE_BAND = (0.105, 0.157)
PLANTED_SANITIZE_TARGET = 0.13
PLANTED_SANITIZE_LINEAR_SHARE = 0.2
SANITIZE_SIZE = 60_000


@pytest.mark.parametrize("case", ("bracket-then-spaces",
                                  "act-as-after-you-then-spaces",
                                  "base64-runs-just-short"))
def test_a_quadratic_regression_planted_in_sanitize_fails_its_real_test(
        monkeypatch, tmp_path, case):
    """IR78-01 on production code: ``test_sanitize_is_fast_on_adversarial_
    input[<case>-60000]`` runs the real ``sanitize`` in its real child
    probe, with a quadratic regression planted after the 64,000-character
    cap. 292395d passed it at 105-157 ms (12/12); 883c9f2 and b81777b
    failed it."""
    assert SANITIZE_SIZE < MAX_SANITIZE_INPUT_LENGTH < GROWTH * SANITIZE_SIZE
    text_length = len(sanitizer_tests.ADVERSARIAL_CASES[case][1](
        SANITIZE_SIZE))
    readings: dict[int, list[float]] = {}
    real_time_in_child = sanitizer_tests._time_in_child

    def recorded(target: str, at_case: str, size: int) -> float:
        seconds = real_time_in_child(target, at_case, size)
        readings.setdefault(size, []).append(seconds)
        return seconds

    for attempt in range(3):
        monkeypatch.undo()
        linear = statistics.median(
            real_time_in_child("sanitize", case, SANITIZE_SIZE)
            for _ in range(3))
        scale = max(1.0, linear / (PLANTED_SANITIZE_TARGET
                                   * PLANTED_SANITIZE_LINEAR_SHARE))
        lower, upper = (bound * scale for bound in PLANTED_SANITIZE_BAND)
        planted_seconds = PLANTED_SANITIZE_TARGET * scale - linear
        scratch = tmp_path / f"attempt-{attempt}"
        scratch.mkdir()
        (scratch / "lesson_sanitizer.py").write_text(
            PLANTED_SANITIZER.format(
                repo_root=str(sanitizer_tests.REPO_ROOT),
                module_path=str(sanitizer_tests.REPO_ROOT
                                / "lesson_sanitizer.py"),
                seconds_per_square=planted_seconds / text_length ** 2),
            encoding="utf-8")
        monkeypatch.setattr(sanitizer_tests, "REPO_ROOT", scratch)
        monkeypatch.setattr(sanitizer_tests, "_time_in_child", recorded)
        readings.clear()
        failure = timing_failure(
            lambda: sanitizer_tests.test_sanitize_is_fast_on_adversarial_input(
                case, SANITIZE_SIZE))
        planted = readings[SANITIZE_SIZE][0]
        if lower <= planted <= upper:
            break
    assert lower <= planted <= upper, (
        f"the planted sanitize read {planted:.4f} s at {SANITIZE_SIZE} "
        f"characters, outside {lower:.4f}-{upper:.4f} s (unplanted "
        f"{linear:.4f} s)")
    assert failure is not None and "superlinear growth" in str(failure), (
        f"a quadratic regression reading {planted:.4f} s in sanitize passed "
        f"its timing test: {readings}")
    assert "at 1x the test's input" in str(failure)
