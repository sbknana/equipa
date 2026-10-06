"""The timing tests' host calibration and growth checks (task 3171).

``tests/host_timing.py`` scales every absolute timing budget by a host
factor and adds a growth check, so a slower runner (a GitHub runner, Python
3.10) no longer fails tests that exist to prove LINEAR time. This file tests
the helper itself, and fences the test suite: every test that measures
elapsed time must reach the helper, or be listed in ``DEADLINE_TESTS`` with
the reason its bound must not scale.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import gc
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import host_timing
from tests.host_timing import (
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    HOST_FACTOR_ENVIRONMENT_VARIABLE,
    MAX_HOST_FACTOR,
    MAX_INPUT_GROWTH,
    REFERENCE_SECONDS,
    HostCalibration,
    HostFactorError,
    HostTooSlowError,
    TimingCheckFailed,
    assert_linear_time,
    assert_linear_times,
    budget,
    growth_ratio,
)

TESTS_DIR = Path(__file__).resolve().parent
# The session's calibration, before the fixture below replaces it.
SESSION_CALIBRATION = host_timing.host_calibration


def _force_factor(monkeypatch, factor: float) -> None:
    """Every measurement scaled by ``factor``, as EQUIPA_TIMING_HOST_FACTOR
    would force it, without running the reference workload."""
    calibration = HostCalibration(factor=factor, measured_seconds=None,
                                  forced=True)
    monkeypatch.setattr(host_timing, "host_calibration", lambda: calibration)


def _measure_load(monkeypatch, *factors: float) -> list[str]:
    """Each reference run of the next measurements takes ``factors[i]``
    times the development host's time, in turn; returns the log of calls."""
    measured = HostCalibration(factor=1.0, measured_seconds=REFERENCE_SECONDS,
                               forced=False)
    monkeypatch.setattr(host_timing, "host_calibration", lambda: measured)
    pending = list(factors)
    log: list[str] = []

    def samples(runs: int) -> list[float]:
        log.append(f"reference x{runs}")
        return [pending.pop(0) * REFERENCE_SECONDS for _ in range(runs)]

    monkeypatch.setattr(host_timing, "reference_samples", samples)
    return log


@pytest.fixture(autouse=True)
def _development_host_speed(monkeypatch):
    """The model tests below time fake work against budgets set for the
    development host: the session's factor (measured on a slow runner, or
    forced by EQUIPA_TIMING_HOST_FACTOR) must not decide whether a model
    meant to go over budget does. A test that needs another factor or a
    measured load patches the calibration itself."""
    _force_factor(monkeypatch, 1.0)


# --- The host factor ------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("2.0", 2.0), (" 1.5 ", 1.5), ("0.5", 0.5), ("3", 3.0),
])
def test_a_forced_factor_is_read_from_the_environment(monkeypatch, raw,
                                                       expected):
    monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, raw)
    assert host_timing.forced_host_factor() == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_unset_or_blank_factor_is_measured(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, raw)
    assert host_timing.forced_host_factor() is None


@pytest.mark.parametrize("raw", ["fast", "0", "-1", "inf", "nan", "1e999",
                                 "4.01", "100"])
def test_a_malformed_factor_is_refused(monkeypatch, raw):
    monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, raw)
    with pytest.raises(HostFactorError, match=HOST_FACTOR_ENVIRONMENT_VARIABLE):
        host_timing.forced_host_factor()


def test_the_cap_itself_may_be_forced(monkeypatch):
    assert MAX_HOST_FACTOR == 4.0
    monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, "4.0")
    assert host_timing.forced_host_factor() == MAX_HOST_FACTOR


def _calibrate(monkeypatch, measured: float) -> HostCalibration:
    """host_calibration's logic without its per-process cache, which the
    rest of this worker's session keeps using."""
    monkeypatch.delenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setattr(host_timing, "measure_reference_seconds",
                        lambda: measured)
    return SESSION_CALIBRATION.__wrapped__()


def test_a_slower_host_loosens_every_budget(monkeypatch):
    calibration = _calibrate(monkeypatch, REFERENCE_SECONDS * 1.3)
    assert calibration.factor == pytest.approx(1.3)
    assert not calibration.forced
    assert "reference workload" in calibration.describe()


def test_a_faster_host_never_tightens_a_budget(monkeypatch):
    assert _calibrate(monkeypatch, REFERENCE_SECONDS / 2).factor == 1.0


def test_a_forced_factor_replaces_the_measurement(monkeypatch):
    def unexpected() -> float:
        raise AssertionError("measured although the factor was forced")

    monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, "0.5")
    monkeypatch.setattr(host_timing, "measure_reference_seconds", unexpected)
    monkeypatch.setattr(host_timing, "reference_samples", unexpected)
    calibration = SESSION_CALIBRATION.__wrapped__()
    assert (calibration.factor, calibration.forced) == (0.5, True)
    assert HOST_FACTOR_ENVIRONMENT_VARIABLE in calibration.describe()
    monkeypatch.setattr(host_timing, "host_calibration", lambda: calibration)
    assert host_timing.measure_under_load(lambda: "work") == ("work", 0.5)
    assert budget(1.0) == 0.5


def test_the_session_factor_is_at_least_one_unless_forced():
    calibration = SESSION_CALIBRATION()
    assert math.isfinite(calibration.factor) and calibration.factor > 0
    if not calibration.forced:
        assert calibration.factor >= 1.0
        assert calibration.measured_seconds > 0
        assert f"capped at {MAX_HOST_FACTOR}" in calibration.describe()


def test_a_session_start_over_the_cap_is_reported_not_refused(monkeypatch):
    """The rest of the suite still runs on a very slow host; its timing
    tests fail on their own measurements."""
    calibration = _calibrate(monkeypatch, REFERENCE_SECONDS * 5)
    assert calibration.factor == pytest.approx(5.0)
    assert "OVER THE CAP" in calibration.describe()


def test_budget_scales_by_the_host_factor(monkeypatch):
    _force_factor(monkeypatch, 2.0)
    assert budget(0.5) == 1.0
    assert budget(0.2) == pytest.approx(0.4)


# --- The factor is measured around each measurement (task 3175) ------------------


def test_the_reference_runs_right_before_and_right_after_the_work(monkeypatch):
    log = _measure_load(monkeypatch, 2.0, 2.0, 3.0, 3.0)

    def work() -> str:
        log.append("work")
        return "result"

    result, factor = host_timing.measure_under_load(work)
    assert log == ["reference x2", "work", "reference x2"]
    # The median of both sides: 2, 2, 3, 3.
    assert (result, factor) == ("result", pytest.approx(2.5))


def test_a_budget_is_scaled_by_the_load_the_measurement_ran_under(
        monkeypatch):
    """The session started on an idle host (factor 1.0); the workers then
    loaded it so that the reference ran 1.5x slower around the work, and
    the linear work 1.4x slower: it passes at the factor measured around
    it, where the session-start factor would have failed it."""
    _measure_load(monkeypatch, *[1.5] * 8)
    timing = assert_linear_time(
        lambda size: 0.7 if size == 40_000 else 0.175, 40_000, 0.5, "loaded")
    assert timing.factor == pytest.approx(1.5)
    assert timing.budget_seconds == pytest.approx(0.75)


def test_linear_work_over_its_loaded_budget_still_fails(monkeypatch):
    _measure_load(monkeypatch, *[1.5] * 8)
    with pytest.raises(TimingCheckFailed, match="over the budget 0.7500 s"):
        assert_linear_time(lambda size: 0.8 if size == 40_000 else 0.2,
                           40_000, 0.5, "slow")


def test_a_factor_over_the_cap_fails_loudly(monkeypatch):
    """A budget loosened past the cap would hide a linear slowdown as
    large; growth only sees superlinear work."""
    _measure_load(monkeypatch, 4.5, 4.5, 4.5, 4.5)
    with pytest.raises(HostTooSlowError, match="over the cap of 4.0x"):
        assert_linear_time(lambda size: 0.6 * size / 10_000, 40_000, 0.5)
    assert issubclass(HostTooSlowError, AssertionError)


def test_work_within_its_base_budget_never_runs_the_reference(monkeypatch):
    """It passes at any factor (never below 1.0): a test timing thousands
    of fast shapes must not pay for the reference around each one. No load
    is queued, so a reference run would fail on an empty queue."""
    log = _measure_load(monkeypatch)
    timing = assert_linear_time(_model(1e-6, 1), 40_000, 0.5, "fast")
    assert log == []
    assert (timing.factor, timing.budget_seconds) == (1.0, 0.5)
    timings = assert_linear_times(lambda size: {"part": 1e-6 * size},
                                  40_000, 0.5)
    assert log == []
    assert timings["part"].factor == 1.0


def test_a_measurement_over_its_base_budget_is_taken_again_under_the_load(
        monkeypatch):
    log = _measure_load(monkeypatch, 1.5, 1.5, 1.5, 1.5)

    def work() -> float:
        log.append("work")
        return 0.6

    seconds, factor = host_timing.measure_under_load(work, 0.5)
    assert log == ["work", "reference x2", "work", "reference x2"]
    assert (seconds, factor) == (0.6, pytest.approx(1.5))


def test_a_measurement_no_factor_can_pass_is_not_taken_again(monkeypatch):
    """At MAX_HOST_FACTOR times its base budget or more, a quadratic
    regression must not run twice: the reference runs once, after it."""
    log = _measure_load(monkeypatch, *[1.5] * 8)

    def work() -> float:
        log.append("work")
        return 0.5 * MAX_HOST_FACTOR

    seconds, factor = host_timing.measure_under_load(work, 0.5)
    assert log == ["work", "reference x4"]
    assert factor == pytest.approx(1.5)
    with pytest.raises(TimingCheckFailed, match="over the budget 0.7500 s"):
        assert_linear_time(lambda size: 2.0 * size / 10_000, 40_000, 0.5)


def test_the_cap_is_inclusive(monkeypatch):
    _measure_load(monkeypatch, *[4.0] * 4)
    assert host_timing.measure_under_load(lambda: None)[1] == MAX_HOST_FACTOR


def test_a_faster_load_never_tightens_a_budget(monkeypatch):
    _measure_load(monkeypatch, 0.5, 0.5, 0.5, 0.5)
    assert host_timing.measure_under_load(lambda: None)[1] == 1.0


def test_budget_reuses_a_recent_factor_and_measures_a_stale_one(monkeypatch):
    log = _measure_load(monkeypatch, *[2.0] * 4)
    monkeypatch.setattr(host_timing, "_last_load_factor",
                        (time.monotonic(), 3.0))
    assert budget(1.0) == 3.0
    assert log == []
    monkeypatch.setattr(host_timing, "_last_load_factor",
                        (time.monotonic() - 10, 3.0))
    assert budget(1.0) == pytest.approx(2.0)
    assert log == ["reference x4"]
    assert budget(0.5) == pytest.approx(1.0)
    assert log == ["reference x4"]


def test_the_checks_hold_under_python_optimize():
    """IR71-01: ``python -O`` strips ``assert`` statements in modules pytest
    does not rewrite; the helper's checks must still fail."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(TESTS_DIR.parent)!r})\n"
        "from tests import host_timing\n"
        "assert False, 'asserts run: not optimized'\n"
        "failures = []\n"
        "for name, seconds_at in [\n"
        "        ('budget', lambda size: 1e-4 * size),\n"
        "        ('growth', lambda size: 6.25e-10 * size ** 2)]:\n"
        "    try:\n"
        "        host_timing.assert_linear_time(seconds_at, 40_000, 2.0)\n"
        "    except host_timing.TimingCheckFailed:\n"
        "        failures.append(name)\n"
        "try:\n"
        "    host_timing.assert_linear_times(\n"
        "        lambda size: {'part': 6.25e-10 * size ** 2}, 40_000, 2.0)\n"
        "except host_timing.TimingCheckFailed:\n"
        "    failures.append('parts')\n"
        "print(','.join(failures))\n"
    )
    environment = {"PATH": "/usr/bin:/bin",
                   HOST_FACTOR_ENVIRONMENT_VARIABLE: "1.0"}
    completed = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True,
        env=environment, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "budget,growth,parts"


def _asserts_in(node: ast.AST) -> list[int]:
    return [child.lineno for child in ast.walk(node)
            if isinstance(child, ast.Assert)]


def test_the_timing_helpers_raise_instead_of_asserting():
    """IR71-01: pytest rewrites only test modules and conftest, so an
    ``assert`` in a helper the timing tests call is stripped by ``python
    -O``. ``production_seconds`` refuses to time an artifact provenance
    rejected (its early rejection is not the parse)."""
    for name in ("host_timing", "review_gate_timing", "deadline_watchdog"):
        source = (TESTS_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert _asserts_in(ast.parse(source)) == [], name
    source = (TESTS_DIR / "review_gate_production.py").read_text(
        encoding="utf-8")
    timed = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef)
             and node.name == "production_seconds"]
    assert len(timed) == 1
    assert _asserts_in(timed[0]) == []


# Fixture data, never imported: the source files of a fake repository the
# docs-drift tests read as text.
NOT_IMPORTED_FIXTURES = TESTS_DIR / "fixtures" / "drift_test_docs"


def _helper_modules() -> list[Path]:
    """Every Python module under tests/ that pytest does not rewrite: not a
    test module or conftest, and not a probe a child pytest collects as a
    test module (it defines tests, and a path given to pytest is
    rewritten)."""
    helpers = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if (path.name.startswith("test_") or path.name == "conftest.py"
                or NOT_IMPORTED_FIXTURES in path.parents
                or "__pycache__" in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.FunctionDef)
               and node.name.startswith("test_") for node in tree.body):
            continue
        helpers.append(path)
    return helpers


def test_no_helper_module_the_suites_import_asserts():
    """IR75-04: an ``assert`` in a helper module is stripped by ``python
    -O``, so tests/review_gate_production.py's must-block helper stopped
    checking that the gate blocked, and ``gate_blocks`` returned a block
    provenance refused for any reason. Every helper raises instead."""
    helpers = _helper_modules()
    names = {path.name for path in helpers}
    assert {"review_gate_production.py", "host_timing.py",
            "deadline_watchdog.py", "sanitizer_timing_probe.py"} <= names
    asserting = {str(path.relative_to(TESTS_DIR)): len(_asserts_in(
        ast.parse(path.read_text(encoding="utf-8")))) for path in helpers}
    assert {name: count for name, count in asserting.items() if count} == {}


def test_the_must_block_helper_raises_when_the_gate_does_not_block(
        monkeypatch):
    """IR75-04, behaviour: the helper fails on a clean review by raising,
    so it fails under ``python -O`` too."""
    from tests import review_gate_production as production

    refused = SimpleNamespace(trusted=False, text=None,
                              reason="no completion line")
    monkeypatch.setattr(production, "production_decision",
                        lambda text, nonce: SimpleNamespace(
                            blocks=True, provenance=refused))
    with pytest.raises(AssertionError, match="another reason"):
        production.gate_blocks("text")
    with pytest.raises(AssertionError, match="provenance read no text"):
        production.blocked_by_the_gate("text")


def test_the_reference_workload_is_fixed_work():
    assert host_timing.reference_workload() == host_timing.reference_workload()
    assert host_timing.reference_workload(10) != host_timing.reference_workload()
    assert host_timing.measure_reference_seconds(runs=3) > 0


def test_the_growth_limit_tells_linear_from_quadratic():
    assert GROWTH == 4
    assert GROWTH < GROWTH_LIMIT < GROWTH ** 2


# --- The growth check ------------------------------------------------------------


def _model(per_unit: float, power: int, calls: list[int] | None = None):
    """A fake ``seconds_at``: per_unit * size ** power seconds."""
    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        return per_unit * size ** power

    return seconds_at


def test_linear_work_passes_and_reports_both_sizes():
    # 0.04 s at the quarter size: over the floor, so each size runs once.
    calls: list[int] = []
    timing = assert_linear_time(_model(4e-6, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000]
    assert (timing.small_size, timing.size) == (10_000, 40_000)
    assert timing.ratio == pytest.approx(4.0)
    assert "growth 4.0x" in timing.describe()


def test_quadratic_work_under_its_budget_still_fails_on_growth():
    # 0.0625 s and 1.0 s: both under a 2 s budget, 16x apart.
    with pytest.raises(AssertionError, match="superlinear growth"):
        assert_linear_time(_model(1e-9 / 1.6, 2), 40_000, 2.0, "quad")


def test_quadratic_work_fails_on_a_runner_forced_twice_as_slow(monkeypatch):
    """The factor loosens the budget, never the growth limit: the slow
    runner simulation must not hide a quadratic regression."""
    _force_factor(monkeypatch, 2.0)
    with pytest.raises(AssertionError, match="superlinear growth"):
        assert_linear_time(_model(1e-9 / 1.6, 2), 40_000, 1.0, "quad")


def test_a_small_size_over_budget_fails_before_the_large_one_runs():
    calls: list[int] = []
    with pytest.raises(AssertionError, match="over the budget"):
        assert_linear_time(_model(1e-4, 1, calls), 40_000, 0.5, "slow")
    assert calls == [10_000]


def test_the_large_size_is_held_to_the_budget():
    with pytest.raises(AssertionError, match="budget"):
        assert_linear_time(_model(1.5e-5, 1), 40_000, 0.5, "over")


def test_one_descheduled_run_is_measured_again():
    spikes = iter([0.4])                # the first large run took 40x

    def seconds_at(size: int) -> float:
        if size == 40_000:
            return next(spikes, 0.04)
        return 0.01

    timing = assert_linear_time(seconds_at, 40_000, 0.5, "spike")
    assert timing.seconds == pytest.approx(0.04)


def test_work_below_the_floor_passes_the_growth_check():
    # 0.0001 s and 0.0006 s: 6x, but both are mostly clock noise. A larger
    # reading under GROWTH_DETECTION_SECONDS cannot be quadratic work main's
    # 2 ms floor caught, so it is decided against the floor as read.
    calls: list[int] = []

    def seconds_at(size: int) -> float:
        calls.append(size)
        return 0.0001 if size < 40_000 else 0.0006

    timing = assert_linear_time(seconds_at, 40_000, 0.5, "tiny")
    assert calls == [10_000, 40_000]
    assert timing.input_growth == 1
    assert timing.ratio == pytest.approx(0.0006 / GROWTH_FLOOR_SECONDS)


def test_linear_work_under_the_floor_is_not_repeated():
    """Linear work whose quarter size takes under the floor is not measured
    again at the same sizes: its input grows until the quarter size takes
    the floor (the 20 ms reading of 40,000 becomes the quarter of 160,000),
    and 20 ms against 80 ms is decided as read."""
    calls: list[int] = []
    timing = assert_linear_time(_model(5e-7, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000, 160_000]
    assert (timing.small_size, timing.size) == (40_000, 160_000)
    assert timing.input_growth == 4
    assert timing.ratio == pytest.approx(4.0)


def test_the_ci_reading_is_measured_again_and_passes():
    """Task 3175: CI read a linear regex at 0.0006 s against 0.0052 s, a
    sub-millisecond ratio of 8.7x. 5.2 ms is under the 16 ms that quadratic
    work main caught reads at, so it is decided against the floor; the
    same spike over a 5 ms quarter reading (24 ms) grows the input, and at
    4 times it the spike (now the quarter) against 80 ms is linear."""
    for quarter, spike, grown in ((0.0006, 0.0052, 1), (0.005, 0.024, 4)):
        calls: list[int] = []
        spikes = iter([spike])

        def seconds_at(size: int, quarter=quarter, spikes=spikes,
                       calls=calls) -> float:
            calls.append(size)
            linear = quarter * size / 10_000
            if size == 40_000:
                return next(spikes, linear)
            return linear

        timing = assert_linear_time(seconds_at, 40_000, 0.5, "ci")
        assert calls.count(40_000) == 1
        assert timing.input_growth == grown
        assert timing.ratio < 4.1


def test_input_growth_is_decided_by_the_readings_not_by_what_a_call_costs():
    """IR75-01: the 3175 repetitions stopped where a call was expensive, so
    a 200 KB gate test's quadratic reading was raised to the floor. The
    input grows on the readings alone: while the larger one could be
    quadratic work main's 2 ms floor caught (GROWTH_DETECTION_SECONDS, 16
    ms), up to MAX_INPUT_GROWTH."""
    grows = host_timing.grows_further
    assert host_timing.GROWTH_DETECTION_SECONDS == pytest.approx(
        GROWTH_LIMIT * 0.002)
    assert grows(0.001, 0.016, 1)
    assert grows(0.005, 0.08, MAX_INPUT_GROWTH // GROWTH)
    assert not grows(0.005, 0.08, MAX_INPUT_GROWTH)
    # Under the detection reading: decided against the floor at once.
    assert not grows(0.001, 0.0159, 1)
    # Decided as read: the quarter at the floor, or over the limit already.
    assert not grows(GROWTH_FLOOR_SECONDS, 0.5, 1)
    assert not grows(0.001, GROWTH_LIMIT * GROWTH_FLOOR_SECONDS, 1)


def test_quadratic_work_under_the_floor_fails_once_repeated():
    """5 ms then 80 ms: the 5 ms reading raised to the 20 ms floor would
    read 4x. The input grows instead: 80 ms against 1.28 s reads 16x."""
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth.*at 4x the test's input"):
        assert_linear_time(_model(5e-11, 2), 40_000, 0.5, "quad")


class _BuildClock:
    """The wall clock ``tests/host_timing.py`` reads, advanced only by the
    shape building a fake ``seconds_at`` declares, so a model of an
    expensive build costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _build_clock(monkeypatch) -> _BuildClock:
    clock = _BuildClock()
    monkeypatch.setattr(host_timing, "time", SimpleNamespace(
        monotonic=clock.monotonic, process_time=time.process_time))
    return clock


# IR75-01: readings at the test's larger size, and the wall time building
# the shape takes per call at that size (the 200 KB review-gate tests build
# for 0.1-0.3 s around a 10 ms measurement).
IR75_01_LARGE_READINGS = (0.016, 0.02, 0.04, 0.08, 0.12, 0.159)
IR75_01_BUILD_SECONDS = (0.0, 0.1, 0.3, 2.0)
IR75_01_SIZE = 200_000


def _planted(clock: _BuildClock, reading: float, build_seconds: float,
             power: int, calls: list[int] | None = None):
    """``reading`` seconds at IR75_01_SIZE growing as size ** power, after
    a build of ``build_seconds`` (linear in the size) on the wall clock."""
    def seconds_at(size: int) -> float:
        if calls is not None:
            calls.append(size)
        clock.now += build_seconds * size / IR75_01_SIZE
        return reading * (size / IR75_01_SIZE) ** power

    return seconds_at


@pytest.mark.parametrize("build_seconds", IR75_01_BUILD_SECONDS)
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
def test_quadratic_work_of_16_to_159_ms_fails_whatever_a_call_costs(
        monkeypatch, reading, build_seconds):
    """IR75-01: a 20 ms floor at unchanged sizes passed quadratic work
    reading 16-159 ms at the larger size (main failed it from 16 ms on).
    The budget (10 s) cannot decide: only the growth does."""
    clock = _build_clock(monkeypatch)
    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(_planted(clock, reading, build_seconds, 2),
                           IR75_01_SIZE, 10.0, "quadratic")


@pytest.mark.parametrize("build_seconds", IR75_01_BUILD_SECONDS)
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
def test_a_quadratic_part_of_16_to_159_ms_fails_whatever_a_run_costs(
        monkeypatch, reading, build_seconds):
    """IR75-01 for ``assert_linear_times`` (the 22 tests of 3139's 1 MB
    whitespace families): the quadratic part fails, the linear one is
    named nowhere in the failure."""
    clock = _build_clock(monkeypatch)
    quadratic = _planted(clock, reading, build_seconds, 2)

    def seconds_at(size: int) -> dict[str, float]:
        return {"linear": 0.001 * size / IR75_01_SIZE,
                "quadratic": quadratic(size)}

    with pytest.raises(TimingCheckFailed, match="superlinear") as failure:
        assert_linear_times(seconds_at, IR75_01_SIZE, 10.0, "parts")
    assert "parts: quadratic" in str(failure.value)
    assert "parts: linear" not in str(failure.value)


@pytest.mark.parametrize("build_seconds", IR75_01_BUILD_SECONDS)
@pytest.mark.parametrize("reading",
                         (0.00005, 0.001, 0.005) + IR75_01_LARGE_READINGS)
def test_linear_work_passes_whatever_a_call_costs(monkeypatch, reading,
                                                  build_seconds):
    """The other side of IR75-01: linear work of any reading passes both
    helpers, and the input only grows while the reading requires it (a
    sub-detection reading stays at the test's sizes)."""
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    timing = assert_linear_time(
        _planted(clock, reading, build_seconds, 1, calls), IR75_01_SIZE,
        10.0, "linear")
    assert timing.ratio < GROWTH_LIMIT
    assert timing.size <= IR75_01_SIZE * MAX_INPUT_GROWTH
    if reading < host_timing.GROWTH_DETECTION_SECONDS:
        assert calls == [IR75_01_SIZE // GROWTH, IR75_01_SIZE]
    else:
        assert timing.small_seconds >= GROWTH_FLOOR_SECONDS
    linear = _planted(clock, reading, build_seconds, 1)
    timings = assert_linear_times(
        lambda size: {"a": linear(size), "b": reading * size / IR75_01_SIZE},
        IR75_01_SIZE, 10.0, "linear parts")
    assert all(part.ratio < GROWTH_LIMIT for part in timings.values())


def _pair_work(units: int) -> int:
    """Pure-Python work over every pair of ``units`` items: quadratic."""
    total = 0
    for row in range(units):
        for column in range(units):
            total ^= row + column
    return total


def _units_reading(target_seconds: float) -> int:
    """How many units ``_pair_work`` takes ``target_seconds`` of CPU time on
    here (measured, so the planted reading is what this host reads)."""
    started = time.process_time()
    _pair_work(400)
    per_pair = (time.process_time() - started) / 400 ** 2
    return max(GROWTH, round(math.sqrt(target_seconds / per_pair)))


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("target_seconds", (0.02, 0.12))
def test_real_quadratic_work_of_16_to_159_ms_fails(target_seconds,
                                                   build_seconds):
    """IR75-01 on real work and the real clock: quadratic work reading
    about 20 ms or 120 ms at the test's size fails, also when building the
    shape takes 0.3 s of wall time per call (3175 then measured each size
    once and raised the quarter reading to the 20 ms floor)."""
    units = _units_reading(target_seconds)

    def seconds_at(size: int) -> float:
        time.sleep(build_seconds * size / units)
        started = time.process_time()
        _pair_work(size)
        return time.process_time() - started

    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(seconds_at, units, 30.0, "planted quadratic")


@pytest.mark.parametrize("target_seconds", (0.005, 0.05))
def test_real_linear_work_passes(target_seconds):
    units = _units_reading(target_seconds) ** 2

    def seconds_at(size: int) -> float:
        started = time.process_time()
        total = 0
        for value in range(size):
            total ^= value
        return time.process_time() - started

    timing = assert_linear_time(seconds_at, units, 30.0, "planted linear")
    assert timing.ratio < GROWTH_LIMIT


def test_a_quarter_reading_at_the_floor_is_decided_without_growing():
    calls: list[int] = []
    timing = assert_linear_time(_model(2e-6, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000]
    assert timing.input_growth == 1


def test_compare_with_larger_times_the_size_and_four_times_it():
    calls: list[int] = []
    assert_linear_time(_model(1e-6, 1, calls), 40_000, 0.5, "cap",
                       compare_with_larger=True)
    assert calls == [40_000, 160_000]


def test_a_size_without_a_quarter_is_refused():
    with pytest.raises(ValueError):
        assert_linear_time(_model(1e-6, 1), 3, 0.5)


def test_several_parts_are_each_checked():
    def seconds_at(size: int) -> dict[str, float]:
        return {"linear": 1e-6 * size, "quadratic": 1e-10 * size ** 2}

    with pytest.raises(AssertionError, match="quadratic"):
        assert_linear_times(seconds_at, 40_000, 1.0, "parts")
    timings = assert_linear_times(
        lambda size: {"a": 1e-6 * size, "b": 2e-6 * size}, 40_000, 1.0)
    assert set(timings) == {"a", "b"}


def test_growth_ratio_raises_the_small_time_to_the_floor():
    assert GROWTH_FLOOR_SECONDS == 0.02
    assert growth_ratio(0.0, 0.004) == 0.004 / GROWTH_FLOOR_SECONDS
    assert growth_ratio(0.0006, 0.0052) == 0.0052 / GROWTH_FLOOR_SECONDS
    assert growth_ratio(0.05, 0.2) == pytest.approx(4.0)


def test_a_heap_sized_collection_pause_does_not_fail_linear_work():
    """Task 3175: a linear tokenizer scan read 0.0129 s against 0.1341 s in a
    long full-suite worker, where a full collection of the worker's heap
    landed in each larger scan. Modelled as a 0.1 s pause that only a
    larger call takes, and only while the collector may run: unpaused, the
    ratio would read (0.0516 + 0.1) / 0.0129, about 11.8x."""
    calls_with_the_collector_enabled: list[int] = []

    def seconds_at(size: int) -> float:
        if gc.isenabled():
            calls_with_the_collector_enabled.append(size)
        collection_pause = 0.1 if gc.isenabled() and size == 16_384 else 0.0
        return 0.0129 * size / 4096 + collection_pause

    timing = assert_linear_time(seconds_at, 16_384, 1.0, "scan")
    assert calls_with_the_collector_enabled == []
    # 0.0129 s is under the floor: the input grows 4x (0.0516 s quarter).
    assert timing.input_growth == 4
    assert timing.ratio == pytest.approx(4.0)


def test_the_collector_is_paused_only_inside_each_timed_call():
    states: list[bool] = []

    def seconds_at(size: int) -> float:
        states.append(gc.isenabled())
        return 1e-6 * size

    assert gc.isenabled()
    assert_linear_time(seconds_at, 40_000, 0.5, "paused")
    assert states and not any(states)
    assert gc.isenabled()


def test_a_disabled_collector_stays_disabled_and_a_failure_restores_it():
    gc.disable()
    try:
        with host_timing.collector_paused():
            assert not gc.isenabled()
        assert not gc.isenabled()
    finally:
        gc.enable()
    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(_model(1e-10, 2), 40_000, 1.0, "quad")
    assert gc.isenabled()


def test_the_reference_runs_with_the_collector_paused(monkeypatch):
    states: list[bool] = []
    monkeypatch.setattr(host_timing, "reference_workload",
                        lambda: states.append(gc.isenabled()))
    host_timing.reference_samples(3)
    assert states == [False, False, False]
    assert gc.isenabled()


# --- The fence: every timing test is calibrated or a listed deadline -------------

# A bound that tells "returned at once" from "waited out a fixed sleep or
# timeout" does not scale with the machine's speed, and a host factor could
# carry it past the timeout it exists to tell apart; growth has no meaning
# for it either. Each entry is "file::test" with the reason.
DEADLINE_TESTS = {
    "test_agent_containment_3114.py::"
    "test_slow_launcher_backstop_kills_setsid_descendants":
        "the backstop kills at once vs. waiting out the launcher grace",
    "test_agent_isolation_3142.py::test_probe_finds_a_reachable_listener_quickly":
        "a listener answers at once vs. the probe timeout",
    "test_agent_isolation_3142.py::"
    "test_probe_reports_an_address_that_never_answers_as_unreachable":
        "the probe gives up at its 0.3 s timeout, not later",
    "test_agent_isolation_3169.py::"
    "test_a_search_that_does_not_finish_fails_within_the_bound":
        "a hanging find is cut at its 1 s scan bound, not the 120 s timeout",
    "test_agent_isolation_3169.py::test_the_bound_fails_even_when_nothing_else_does":
        "a hanging find is cut at its 1 s scan bound",
    "test_agent_env_cwd_redaction_3120.py::"
    "test_slow_reactive_check_times_out_as_a_block_without_freezing":
        "the event loop keeps beating (stall < 1 s) while the check sleeps 2 s",
    "test_reactive_check_process_3127.py::"
    "test_streaming_agent_is_blocked_when_the_check_misses_its_deadline":
        "the event loop keeps beating while the check misses its deadline",
    "test_deadline_watchdog_3175.py::"
    "test_a_catastrophic_regex_is_cut_at_its_deadline":
        "the regex is cut at its 0.5 s deadline, not after its minutes",
    "test_deadline_watchdog_3175.py::"
    "test_a_watchdog_thread_could_not_have_cut_a_regex":
        "the match runs to its 0.5 s deadline with no thread tick inside it",
    "test_deadline_watchdog_3175.py::"
    "test_a_swallowed_deadline_stops_the_process_and_names_the_test":
        "the child is stopped 2 s in, not after its endless loop",
    "test_deadline_watchdog_3175.py::"
    "test_with_the_signal_taken_the_thread_stops_a_swallowed_deadline":
        "the child is stopped 2 s in by the fallback thread, not after its "
        "endless loop",
    "test_deadline_watchdog_3175.py::"
    "test_after_a_hang_the_next_hang_is_cut_short_and_named":
        "the second hang is cut to 1 s (6-7 s of the child's CPU time in "
        "all), not left to its 60 s spin",
    "test_agent_isolation_3172.py::"
    "test_a_search_cut_short_at_the_default_bound_reports_before_the_caller_stops":
        "the cut-short search is reported within the 25 s the default bound "
        "and kill grace leave before the caller's 120 s read timeout",
    "test_generated_file_merge_3131.py::"
    "test_generator_timeout_kills_its_group_and_fails_cleanly":
        "the generator is killed at its timeout, not after its 120 s sleep",
    "test_generated_file_merge_3141.py::"
    "test_oversized_output_is_cut_off_while_streaming":
        "the output cap stops the generator before its 30 s timeout",
    "test_merge_path_pinned_3155.py::"
    "test_commondir_that_is_not_a_regular_file_blocks_within_a_second":
        "a FIFO commondir blocks the pin vs. returning at once",
    "test_reactive_check_process_3127.py::"
    "test_catastrophic_regex_checker_does_not_freeze_the_event_loop":
        "the check gives up at its deadline instead of freezing the loop",
    "test_reactive_check_process_3127.py::"
    "test_the_heartbeat_reads_a_frozen_loop_in_full":
        "a 1 s freeze of the loop reads at least 0.9 s once run-queue wait "
        "is taken out, not the scheduling noise around it",
    "test_stop_signal_3159.py::test_the_watchdog_cuts_a_hanging_cleanup_short":
        "the watchdog cuts cleanup at its budget, not after a 10 s hang",
}
# Tests that reach a timed helper but bound no time: they read only what
# the helper did.
NOT_TIMING_TESTS = {
    "test_agent_isolation_3169.py::test_a_bad_scan_bound_fails_and_keeps_the_default":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3169.py::"
    "test_root_fs_moves_every_disk_check_below_the_given_root":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3172.py::"
    "test_a_root_holding_an_ampersand_still_probes_the_well_known_directories":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3172.py::"
    "test_the_well_known_directories_are_joined_to_the_root_one_by_one":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3172.py::"
    "test_a_root_that_is_not_an_existing_absolute_directory_fails":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3172.py::test_a_valid_root_is_not_reported":
        "reads the script's output; the run's seconds are discarded",
    "test_agent_isolation_3169.py::"
    "test_without_root_fs_the_real_root_filesystem_is_searched":
        "reads the script's output; the run's seconds are discarded",
    "test_review_gate_linear_3167.py::test_every_timed_review_is_parsed":
        "counts the parses production_seconds makes",
}

# Tests that hold their own measurement to ``budget()`` with no growth check:
# whole-corpus caps that bound a run, not a proof of linear time (IR71-02:
# a budget alone is not a calibrated timing test unless it is listed here).
CORPUS_CAPS = {
    "test_bash_tokenizer_3128.py::"
    "test_tokenizer_agrees_with_bash_on_generated_commands":
        "a seed's whole generated corpus against bash, capped at 20 s",
    "test_bash_tokenizer_3128.py::"
    "test_substitutions_bash_runs_are_never_proven_inert":
        "the whole substitution corpus against bash, capped at 20 s",
    "test_bash_tokenizer_3128.py::"
    "test_every_variable_assignment_bash_runs_in_a_fresh_bash_is_refused":
        "the whole assignment corpus, each run in a fresh bash, capped at 60 s",
}

# Clock readings, as ``time.<clock>()``, ``<clock>()`` once imported, or
# ``<event loop>.time()``; the ``_ns`` variants too.
CLOCKS = {"process_time", "perf_counter", "monotonic", "time", "thread_time"}
CLOCKS |= {f"{clock}_ns" for clock in CLOCKS}
# Clock readings that name their clock: ``time.clock_gettime(clock_id)``,
# e.g. another thread's CPU clock from ``time.pthread_getcpuclockid``.
CLOCKS_BY_ID = {"clock_gettime", "clock_gettime_ns"}
# Readings that carry CPU (or elapsed) seconds in fields: ``os.times()`` and
# ``resource.getrusage(who)``, compared as ``after.ru_utime -
# before.ru_utime`` (a child process's CPU time, task 3175).
USAGE_CLOCKS = {"times", "getrusage"}
USAGE_SECONDS = {"ru_utime", "ru_stime", "user", "system", "children_user",
                 "children_system", "elapsed"}
# Calls that time work themselves: ``timeit.timeit``/``timeit.repeat`` and
# a Timer's ``timeit``/``autorange``.
TIMEIT_METHODS = {"timeit", "autorange"}
# A test is calibrated when it reaches a growth check (IR71-02: only a
# call counts, not a mention of a name such as GROWTH_LIMIT).
GROWTH_CHECKS = {"assert_linear_time", "assert_linear_times", "growth_ratio"}
BUDGETS = {"budget", "host_factor", "measure_under_load"}
# Modules whose functions are followed when a test calls them.
HELPER_MODULES = {"review_gate_timing", "review_gate_production"}


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if isinstance(function, ast.Attribute):
        name = function.attr
    elif isinstance(function, ast.Name):
        name = function.id
    else:
        return False
    if name == "getrusage" or name in CLOCKS_BY_ID:
        return True
    return not node.args and not node.keywords and (
        name in CLOCKS or name in USAGE_CLOCKS)


def _reading_key(node: ast.AST) -> str | None:
    """``started`` or ``self.started``: what a clock reading is kept in."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.unparse(node)
    return None


def _clock_names(function: ast.AST) -> set[str]:
    """What *function* (nested functions and methods included) sets to a
    clock reading: ``started = time.process_time()``, ``self.start =
    perf_counter()``, ``start, n = monotonic(), 0``, ``start: float =
    monotonic()``."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            if (isinstance(target, (ast.Tuple, ast.List))
                    and isinstance(value, (ast.Tuple, ast.List))
                    and len(target.elts) == len(value.elts)):
                pairs_of_items = zip(target.elts, value.elts)
            else:
                pairs_of_items = [(target, value)]
            for item_target, item_value in pairs_of_items:
                key = _reading_key(item_target)
                if key is not None and _is_clock_call(item_value):
                    names.add(key)
    return names


def _measures_elapsed(node: ast.AST, clock_names: set[str]) -> bool:
    """``clock() - started`` or ``now - started`` with both sides clock
    readings: a deadline ``clock() + timeout`` or a timestamp ``clock() -
    AGE`` is not a measurement."""
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
        return False
    left, right = node.left, node.right
    if (isinstance(left, ast.Attribute) and isinstance(right, ast.Attribute)
            and left.attr in USAGE_SECONDS and right.attr in USAGE_SECONDS):
        # ``after.ru_utime - before.ru_utime``: fields of two readings.
        left, right = left.value, right.value
    left_is_reading = (_is_clock_call(left)
                       or _reading_key(left) in clock_names)
    return left_is_reading and _reading_key(right) in clock_names


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _times_with_timeit(call: ast.Call, imports: dict[str, tuple[str, str]]) -> bool:
    function = call.func
    if isinstance(function, ast.Attribute):
        return ((isinstance(function.value, ast.Name)
                 and function.value.id == "timeit")
                or function.attr in TIMEIT_METHODS)
    return (isinstance(function, ast.Name) and function.id in imports
            and imports[function.id][0] == "timeit")


class _Module:
    """The functions of one module: which measure, which reach the helper,
    and what each calls (by the name it is called under). A class counts as
    a function: calling it follows every method (a timer context manager)."""

    def __init__(self, name: str, source: str) -> None:
        self.name = name
        tree = ast.parse(source)
        self.imports: dict[str, tuple[str, str]] = {}
        self.functions: dict[str, ast.AST] = {}
        self.tests: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                source_module = node.module.rpartition(".")[2]
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (
                        source_module, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
                if node.name.startswith("test_"):
                    self.tests[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.functions[node.name] = node
                for member in node.body:
                    if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and member.name.startswith("test_")):
                        self.tests[f"{node.name}.{member.name}"] = member


def _load_modules() -> dict[str, _Module]:
    paths = sorted(TESTS_DIR.glob("test_*.py"))
    paths += [TESTS_DIR / f"{name}.py" for name in sorted(HELPER_MODULES)]
    return {path.stem: _Module(path.stem, path.read_text(encoding="utf-8"))
            for path in paths}


@dataclass(frozen=True)
class _Reach:
    """What a test does, its called functions included."""

    measures: bool
    growth_checked: bool
    budgeted: bool

    @property
    def verdict(self) -> str:
        if not self.measures:
            return "not timing"
        if self.growth_checked:
            return "calibrated"
        return "budget only" if self.budgeted else "unscaled"


def _reach(modules: dict[str, _Module], module: _Module,
           node: ast.AST) -> _Reach:
    """What *node* and every function it calls (or passes on by name, as
    ``assert_linear_time(seconds_at, ...)``) in this module or a followed
    one do: measure elapsed time, reach a growth check, reach a budget."""
    measures = growth_checked = budgeted = False
    seen: set[tuple[str, str]] = set()
    pending = [(module, node)]
    while pending:
        current, function = pending.pop()
        clock_names = _clock_names(function)
        for child in ast.walk(function):
            measures = measures or _measures_elapsed(child, clock_names)
            referenced = None
            if isinstance(child, ast.Call):
                measures = measures or _times_with_timeit(child, current.imports)
                name = _called_name(child)
                growth_checked = growth_checked or name in GROWTH_CHECKS
                budgeted = budgeted or name in BUDGETS
                if name in current.imports:
                    referenced = name
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                referenced = child.id
            target = None
            if referenced in current.imports:
                source, original = current.imports[referenced]
                if source in modules and source != "host_timing":
                    target = (source, original)
            elif referenced in current.functions:
                target = (current.name, referenced)
            if target and target not in seen:
                seen.add(target)
                callee = modules[target[0]]
                if target[1] in callee.functions:
                    pending.append((callee, callee.functions[target[1]]))
    return _Reach(measures, growth_checked, budgeted)


@pytest.fixture(scope="module")
def timing_reach() -> dict[str, _Reach]:
    modules = _load_modules()
    return {
        f"{module.name}.py::{test}": _reach(modules, module, node)
        for module in modules.values() if module.name.startswith("test_")
        for test, node in module.tests.items()
    }


def test_every_timing_test_is_calibrated_or_a_listed_deadline(timing_reach):
    uncalibrated = sorted(
        key for key, reach in timing_reach.items()
        if reach.verdict == "unscaled"
        and key not in DEADLINE_TESTS and key not in NOT_TIMING_TESTS)
    assert not uncalibrated, (
        "these tests time work without tests/host_timing.py (use "
        "assert_linear_time, or list a deadline in DEADLINE_TESTS): "
        f"{uncalibrated}")


def test_a_budget_without_a_growth_check_is_a_listed_corpus_cap(timing_reach):
    unlisted = sorted(key for key, reach in timing_reach.items()
                      if reach.verdict == "budget only"
                      and key not in CORPUS_CAPS)
    assert not unlisted, (
        "these tests hold work to budget() without a growth check (use "
        "assert_linear_time, or list a corpus cap in CORPUS_CAPS): "
        f"{unlisted}")


@pytest.mark.parametrize("listed, verdict", [
    (DEADLINE_TESTS, "unscaled"), (NOT_TIMING_TESTS, "unscaled"),
    (CORPUS_CAPS, "budget only"),
], ids=["deadlines", "not-timing", "corpus-caps"])
def test_every_listed_test_has_the_verdict_it_is_listed_for(
        timing_reach, listed, verdict):
    assert not set(DEADLINE_TESTS) & set(NOT_TIMING_TESTS)
    assert not (set(DEADLINE_TESTS) | set(NOT_TIMING_TESTS)) & set(CORPUS_CAPS)
    stale = sorted(key for key in listed if key not in timing_reach
                   or timing_reach[key].verdict != verdict)
    assert not stale, f"not a {verdict} timing test (renamed?): {stale}"


def test_the_fence_sees_the_shapes_it_must(timing_reach):
    """The fence is not vacuous: in-module helpers, imported helpers and
    nested ``seconds_at`` functions are followed."""
    for key in [
            "test_review_gate_polish_3170.py::"
            "test_a_200kb_heading_status_is_read_in_one_pass",
            "test_redaction_linear_3138.py::"
            "test_redact_secrets_on_64kb_adversarial_input_is_fast",
            "test_sanitizer_3163.py::test_the_tail_judge_stays_linear"]:
        assert timing_reach[key].verdict == "calibrated", key
    # The event-loop heartbeats (``now - last``) are seen, and listed.
    for key in [
            "test_agent_env_cwd_redaction_3120.py::"
            "test_slow_reactive_check_times_out_as_a_block_without_freezing",
            "test_reactive_check_process_3127.py::"
            "test_catastrophic_regex_checker_does_not_freeze_the_event_loop"]:
        assert timing_reach[key].verdict == "unscaled", key
    # 69 test functions time work in process when this fence was written.
    assert sum(reach.measures for reach in timing_reach.values()) >= 60


@pytest.mark.parametrize("source, measures", [
    ("started = time.process_time()\nx = time.process_time() - started", True),
    ("start = perf_counter()\nx = perf_counter() - start", True),
    ("t0 = time.perf_counter()\nt1 = time.perf_counter()\nx = t1 - t0", True),
    ("deadline = time.monotonic() + 5", False),
    ("old = time.time() - 3600", False),
    ("old = time.time() - AGE_SECONDS", False),
    ("began = time.time() - 61\nx = time.time() - began", False),
    ("began = time.time() - 61\nnow = time.time()\nx = now - began", False),
    ("x = time.time(5) - time.time(3)", False),
    ("before = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
     "after = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
     "x = after.ru_utime - before.ru_utime", True),
    ("before = os.times()\nx = os.times().children_user - before.children_user",
     True),
    ("before = resource.getrusage(resource.RUSAGE_SELF)\n"
     "after = resource.getrusage(resource.RUSAGE_SELF)\n"
     "x = after.ru_maxrss - before.ru_maxrss", False),
    ("x = usage.ru_utime - baseline.ru_utime", False),
    ("clock = time.pthread_getcpuclockid(ident)\n"
     "start = time.clock_gettime(clock)\n"
     "x = time.clock_gettime(clock) - start", True),
    ("start = clock_gettime_ns(CLOCK_MONOTONIC)\n"
     "x = clock_gettime_ns(CLOCK_MONOTONIC) - start", True),
    ("deadline = time.clock_gettime(CLOCK_MONOTONIC) + 5", False),
])
def test_a_clock_subtraction_is_found_and_a_deadline_is_not(source, measures):
    tree = ast.parse(source)
    names = _clock_names(tree)
    assert any(_measures_elapsed(node, names)
               for node in ast.walk(tree)) is measures


# Each shape is a scratch test module; the fence must give it the verdict.
# The first eleven are the uncalibrated shapes of the independent review of
# task 3171 (IR71-02), only three of which the first fence saw.
PLANTED_SHAPES = {
    "process-time-minus-start": ("unscaled", """
import time
def test_shape():
    start = time.process_time()
    work()
    assert time.process_time() - start < 0.5
"""),
    "bare-perf-counter-import": ("unscaled", """
from time import perf_counter
def test_shape():
    start = perf_counter()
    work()
    assert perf_counter() - start < 0.5
"""),
    "elapsed-monotonic": ("unscaled", """
from time import monotonic
def test_shape():
    start = monotonic()
    work()
    elapsed = monotonic() - start
    assert elapsed < 0.5
"""),
    "two-readings": ("unscaled", """
import time
def test_shape():
    t0 = time.perf_counter()
    work()
    t1 = time.perf_counter()
    assert t1 - t0 < 0.5
"""),
    "timeit-module": ("unscaled", """
import timeit
def test_shape():
    assert timeit.timeit(work, number=10) < 0.5
"""),
    "timeit-imported-repeat": ("unscaled", """
from timeit import repeat
def test_shape():
    assert min(repeat(work, number=1, repeat=3)) < 0.5
"""),
    "timer-autorange": ("unscaled", """
from timeit import Timer
def test_shape():
    loops, seconds = Timer(work).autorange()
    assert seconds / loops < 0.5
"""),
    "tuple-assigned-start": ("unscaled", """
import time
def test_shape():
    start, runs = time.perf_counter(), 3
    work()
    assert time.perf_counter() - start < 0.5 * runs
"""),
    "nanosecond-clock": ("unscaled", """
import time
def test_shape():
    start = time.perf_counter_ns()
    work()
    assert time.perf_counter_ns() - start < 500_000_000
"""),
    "timer-context-manager": ("unscaled", """
import time
class Stopwatch:
    def __enter__(self):
        self.start = time.perf_counter()
        return self
    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.start
def test_shape():
    with Stopwatch() as watch:
        work()
    assert watch.elapsed < 0.5
"""),
    "budget-only": ("budget only", """
import time
from tests.host_timing import budget
def test_shape():
    start = time.process_time()
    work()
    assert time.process_time() - start < budget(0.5)
"""),
    "unrelated-growth-limit": ("unscaled", """
import time
from tests.host_timing import GROWTH_LIMIT
def test_shape():
    start = time.process_time()
    work()
    assert time.process_time() - start < 0.5
    assert GROWTH_LIMIT == 8.0
"""),
    "child-cpu-rusage": ("unscaled", """
import resource
import subprocess
def test_shape():
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    subprocess.run(["work"], check=False)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert ((after.ru_utime - before.ru_utime)
            + (after.ru_stime - before.ru_stime)) < 60
"""),
    "child-cpu-os-times": ("unscaled", """
import os
def test_shape():
    before = os.times()
    work()
    assert os.times().children_user - before.children_user < 60
"""),
    "event-loop-heartbeat": ("unscaled", """
import asyncio
async def beat(gaps, done):
    loop = asyncio.get_running_loop()
    last = loop.time()
    while not done.is_set():
        await asyncio.sleep(0.02)
        now = loop.time()
        gaps.append(now - last)
        last = now
def test_shape():
    gaps = []
    asyncio.run(beat(gaps, asyncio.Event()))
    assert max(gaps) < 1.0
"""),
    "thread-cpu-clock": ("unscaled", """
import threading
import time
def test_shape():
    clock = time.pthread_getcpuclockid(threading.main_thread().ident)
    start = time.clock_gettime(clock)
    work()
    assert time.clock_gettime(clock) - start < 0.5
"""),
    "annotated-start": ("unscaled", """
import time
def test_shape():
    start: float = time.thread_time()
    work()
    assert time.thread_time() - start < 0.5
"""),
    "growth-checked": ("calibrated", """
import time
from tests.host_timing import assert_linear_time
def seconds_at(size):
    start = time.process_time()
    work(size)
    return time.process_time() - start
def test_shape():
    assert_linear_time(seconds_at, 4096, 0.5)
"""),
    "a-deadline-is-not-timing": ("not timing", """
import time
def test_shape():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if poll():
            break
"""),
}


@pytest.mark.parametrize("shape", sorted(PLANTED_SHAPES))
def test_the_fence_gives_each_planted_shape_its_verdict(shape):
    verdict, source = PLANTED_SHAPES[shape]
    scratch = _Module("test_scratch_shape", source)
    reach = _reach({"test_scratch_shape": scratch}, scratch,
                   scratch.tests["test_shape"])
    assert reach.verdict == verdict


@pytest.mark.parametrize("path", sorted(TESTS_DIR.glob("test_*.py")),
                         ids=lambda path: path.name)
def test_a_timing_probe_module_uses_the_helper(path):
    """Timing measured in a child process (a probe prints its seconds) is
    invisible to the call fence: a module that runs one must still read the
    seconds against the helper."""
    source = path.read_text(encoding="utf-8")
    runs_probe = ("timing_probe" in source
                  or "print(time.process_time() - " in source)
    if runs_probe and path.name != Path(__file__).name:
        assert "tests.host_timing import" in source, path.name
