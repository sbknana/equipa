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
for one ``seconds_at``, to b81777b's repetitions; every grown pair to the
growth limit against the 20 ms floor. These tests plant each shape (modelled, real work on
the real clock, and a regression in the real ``sanitize`` behind its real
test) and pin the verdict.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import statistics
import sys
import time
from typing import Callable

import pytest

from tests import host_timing
from tests import test_host_timing_3171 as host_timing_3171
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
    PLANT_FLOOR_SECONDS,
    PLANT_LOWEST_TARGET,
    PLANTS,
    _build_clock,
    _units_reading,
    plant_target,
    planted_band,
    planted_over_the_floor,
    replanted_units,
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
    past the test's size fails at the first grown pair it is over at, once
    the one size past that pair shows its growth exponent (task 3191)."""
    # The late shape's first grown pair: the scaled step a 16-20 ms reading
    # takes (``next_quarter_size``), 4 times over; its exponent is read at 4
    # times that pair's larger size, and the input grows no further.
    first_grown_size = GROWTH * host_timing.next_quarter_size(
        SIZE, reading, SIZE * host_timing.MAX_INPUT_GROWTH)
    cases = [(_shape("quadratic", reading), SIZE)]
    if reading / GROWTH < host_timing.GROWTH_FLOOR_SECONDS:
        # From 80 ms on, the late shape's quarter reads the floor: it is
        # decided at the test's sizes, where it is linear (as in main).
        cases.append((_late(reading), GROWTH * first_grown_size))
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


CLIFF_TEST_SIZE = 65_536
CLIFF_SIZE = 100_000


def _cliff_seconds_at(spikes: list[float], steepness: float = 3.2,
                      calls: list[int] | None = None
                      ) -> Callable[[int], float]:
    """3138's pattern 22 on a full xdist run: linear (5.9 ms at 90,503
    units), its cost per unit ``steepness`` times higher past CLIFF_SIZE (a
    cache cliff, not an algorithm), and the test's size reading ``spikes``
    first, as one descheduled call read 17.4 ms against about 4.3 ms."""
    pending = list(spikes)

    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        if size == CLIFF_TEST_SIZE and pending:
            return pending.pop(0)
        return (0.0059 * size / 90_503
                * (steepness if size > CLIFF_SIZE else 1.0))

    return seconds_at


def test_a_constant_factor_step_past_the_tests_size_is_not_growth():
    """The spike grows the input; the first grown pair reads 5.9 ms then
    75.5 ms (12.7x against main's 2 ms floor). 883c9f2, b81777b and
    292395d all pass this work; held to main's floor at a size main never
    measured, the full suite failed it once in four runs. Grown pairs are
    held against the 20 ms floor, as task 3178 held them."""
    def decided_at_the_cliff(timing: host_timing.LinearTiming) -> bool:
        # The pair of CI's readings: a quarter under the floor, 12.7x raw.
        return (timing.small_size < CLIFF_SIZE < timing.size
                and timing.small_seconds < host_timing.GROWTH_FLOOR_SECONDS
                and timing.seconds / timing.small_seconds
                >= host_timing.GROWTH_LIMIT)

    timing = assert_linear_time(_cliff_seconds_at([0.0174]), CLIFF_TEST_SIZE,
                                0.1, "cliff")
    assert decided_at_the_cliff(timing), timing
    timings = assert_linear_times(
        lambda size, part=_cliff_seconds_at([0.0174]): {
            "cliff": part(size), "fast": 0.001 * size / CLIFF_TEST_SIZE},
        CLIFF_TEST_SIZE, 0.1, "parts")
    assert decided_at_the_cliff(timings["cliff"]), timings


def test_a_grown_pair_over_the_limit_against_the_growth_floor_still_fails():
    """The control: the same cliff 40 times steep is over the limit at the
    first grown pair even against the 20 ms floor, and fails there. A step
    of 4 times or more per unit is a cliff, not a cache: its first step is
    over what a cache step reads (``EXPONENT_MAX_FIRST_STEP``), so the size
    4 times past the pair is never read for an exponent (R3191-03)."""
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth.*floor 0\.02 s") as failure:
        assert_linear_time(_cliff_seconds_at([0.0174], 40.0, calls),
                           CLIFF_TEST_SIZE, 10.0, "steep cliff")
    assert "growth exponent" not in str(failure.value)
    assert max(calls) <= GROWTH * host_timing.next_quarter_size(
        CLIFF_TEST_SIZE, 0.0174, CLIFF_TEST_SIZE // GROWTH
        * host_timing.MAX_INPUT_GROWTH)


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
@pytest.mark.parametrize("target_seconds", (PLANT_LOWEST_TARGET, 0.12))
def test_real_windowed_work_of_22_5_to_159_ms_fails(target_seconds,
                                                    build_seconds):
    """IR78-01 on real work: quadratic up to the test's size and linear
    past it, planted at 30 ms (a 22.5-40 ms band) or 120 ms (90-159 ms) at
    the test's size; 16.5-22.5 ms is proved on the scripted clock
    (``test_superlinear_work_of_16_to_159_ms_fails_at_the_tests_own_
    sizes``). The size is calibrated on this host and the reading asserted
    inside the band, and every reading at the test's size over
    PLANT_FLOOR_SECONDS: the lowest proof plants at PLANT_LOWEST_TARGET
    (30 ms, ``plant_target`` of the 20 ms aim), re-calibrated up to a
    higher target when its fastest reading there falls under it (task
    3208). A plant that misses its band, under or over it, runs again at
    units re-scaled from its own reading at the test's size
    (``replanted_units``, task 3209), up to PLANTS plants."""
    target = plant_target(target_seconds)
    units = _units_reading(target)
    for plant in range(PLANTS):
        if plant:
            target = plant_target(target, fastest)
            units = replanted_units(units, planted, target)
        lower, upper = planted_band(target)
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
        planted, fastest = readings[units][0], min(readings[units])
        if lower <= planted <= upper and fastest >= PLANT_FLOOR_SECONDS:
            break
    assert lower <= planted <= upper, (
        f"the planted reading {planted:.4f} s at {units} units missed the "
        f"{lower:.4f}-{upper:.4f} s band on this host")
    assert fastest >= PLANT_FLOOR_SECONDS, planted_over_the_floor(fastest,
                                                                 units)
    assert failure is not None and "superlinear growth" in str(failure), (
        f"windowed work reading {planted:.4f} s passed: {readings}")


# --- The proofs plant what they claim on any host (CI run 37404143426) -----------


# A runner whose calibration is off, simulated: (target, share of it every
# plant reads). An eighth of the 120 ms target (the CI case) reads 15 ms,
# under the 16 ms main caught, so the check rightly passes it (and no
# re-calibration up past the 120 ms target reaches the band); twice the
# lowest proof's 30 ms plant target (PLANT_LOWEST_TARGET, task 3208) reads
# 60 ms, over the band, so it proves nothing about the 22.5-40 ms it
# claims.
MISCALIBRATIONS = ((0.12, 1 / 8), (PLANT_LOWEST_TARGET, 2.0))


@pytest.mark.parametrize("target_seconds, miscalibration", MISCALIBRATIONS)
@pytest.mark.parametrize("proof", ("quadratic", "windowed"))
def test_a_proof_planted_outside_its_band_fails_loudly(
        monkeypatch, proof, target_seconds, miscalibration):
    """CI run 37404143426 failed 3178's proof with DID NOT RAISE: its plant
    did not read what the proof claimed, and nothing said so. A runner that
    plants outside the band now fails the proof on the band, naming the
    reading, instead of passing (a plant under 16 ms) or proving nothing
    about the band it claims (a plant over it).

    Task 3209: a plant is re-scaled from its own reading, which corrects a
    calibration that is off once. The runner here stays off: the first
    plant's calibration and every re-scaling aim at ``miscalibration``
    times the target, so all PLANTS plants miss and the band fails it."""
    real_units_reading = host_timing_3171._units_reading
    real_replanted_units = host_timing_3171.replanted_units

    def miscalibrated(target_seconds: float) -> int:
        return real_units_reading(target_seconds * miscalibration)

    def misreplanted(units: int, planted_seconds: float,
                     target_seconds: float) -> int:
        return real_replanted_units(units, planted_seconds,
                                    target_seconds * miscalibration)

    module = host_timing_3171 if proof == "quadratic" else sys.modules[
        __name__]
    monkeypatch.setattr(module, "_units_reading", miscalibrated)
    monkeypatch.setattr(module, "replanted_units", misreplanted)
    if proof == "quadratic":
        run = host_timing_3171.test_real_quadratic_work_of_22_5_to_159_ms_fails
    else:
        run = test_real_windowed_work_of_22_5_to_159_ms_fails
    with pytest.raises(AssertionError, match=r"missed the [\d.]+-[\d.]+ s "
                                             r"band on this host"):
        run(target_seconds, 0.0)


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
# 105-157 ms. The planted reading is at least PLANTED_SANITIZE_OVER_LINEAR
# times the unplanted one, so the test's own pair grows at least 9.6x
# (``own_pair_growth``) whatever the host. Up to PLANTED_SANITIZE_BAND_TOP
# that stays inside the reviewer's band (unplanted readings up to about
# 33 ms); a slower host scales the band by the target it needs.
PLANTED_SANITIZE_BAND = (0.105, 0.157)
PLANTED_SANITIZE_TARGET = 0.13
PLANTED_SANITIZE_OVER_LINEAR = 4.5
# The highest target kept inside the reviewer's band: the readings of the
# unplanted work vary by a few milliseconds over the median it is planted on.
PLANTED_SANITIZE_BAND_TOP = 0.15
SANITIZE_SIZE = 60_000


def own_pair_growth(reading: float, linear: float) -> float:
    """The growth at the test's own pair (a quarter, then the test's size)
    of a plant reading ``reading`` at the test's size over linear work
    reading ``linear`` there, its quadratic part growing 16x."""
    return reading / (linear / GROWTH + (reading - linear) / GROWTH ** 2)


def planted_sanitize_target(linear: float) -> tuple[float, float, float]:
    """The reading to plant over an unplanted ``sanitize`` reading
    ``linear`` at the test's size, and the band it must land in."""
    target = max(PLANTED_SANITIZE_TARGET, PLANTED_SANITIZE_OVER_LINEAR * linear)
    if target <= PLANTED_SANITIZE_BAND_TOP:
        return (target, *PLANTED_SANITIZE_BAND)
    scale = target / PLANTED_SANITIZE_TARGET
    lower, upper = PLANTED_SANITIZE_BAND
    return target, lower * scale, upper * scale


@pytest.mark.parametrize("linear", (0.005, 0.026, 0.031, 0.0333, 0.034,
                                    0.05, 0.2))
def test_the_sanitize_plant_keeps_to_the_reviewers_band_where_it_can(linear):
    """On a host reading the unplanted ``sanitize`` like the reviewer's
    (about 31 ms), the plant lands in their 105-157 ms band; everywhere it
    grows at least 9.6x at the test's own pair, over main's 8x."""
    target, lower, upper = planted_sanitize_target(linear)
    assert lower <= target <= upper
    # 9.6x exactly at the lowest plant (4.5 times the linear work).
    assert own_pair_growth(target, linear) >= 9.6 - 1e-9
    if linear <= 0.0333:
        assert (lower, upper) == PLANTED_SANITIZE_BAND
        assert target <= PLANTED_SANITIZE_BAND_TOP


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
        target, lower, upper = planted_sanitize_target(linear)
        planted_seconds = target - linear
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
