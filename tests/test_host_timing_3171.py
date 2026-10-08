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
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from tests import host_timing
from tests.host_timing import (
    DETECTION_FLOOR_SECONDS,
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    HOST_FACTOR_ENVIRONMENT_VARIABLE,
    MAX_GROWTH_REPETITIONS,
    MAX_HOST_FACTOR,
    MAX_INPUT_GROWTH,
    REFERENCE_SECONDS,
    HostCalibration,
    HostFactorError,
    HostTooSlowError,
    InputTooLarge,
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


# Readings of each size a pair whose quarter reading is over its floor (and
# whose larger reading could reach the limit against a quieter quarter) is
# decided on: task 3187 (R3184-01) measures it again before it passes.
SETTLED_READINGS = 1 + host_timing.SETTLE_RETRIES


def test_linear_work_passes_and_reports_both_sizes():
    # 0.04 s at the quarter size: over the floor, so each size runs once,
    # then once more, interleaved, before the pair passes (R3184-01).
    calls: list[int] = []
    timing = assert_linear_time(_model(4e-6, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000] * SETTLED_READINGS
    assert timing.samples == SETTLED_READINGS
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
        return 0.01 * size / 10_000     # linear work

    timing = assert_linear_time(seconds_at, 40_000, 0.5, "spike")
    # The spike is measured again and settles at 0.04 s; the input then
    # grows from that reading as it would have without the spike (task
    # 3191, R3188-01), and linear work grows 4x there.
    assert (timing.small_size, timing.small_seconds) == (
        40_000, pytest.approx(0.04))
    assert timing.ratio == pytest.approx(GROWTH)


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
    and 20 ms against 80 ms is decided as read. (The test's own pair, a
    5 ms quarter over main's 2 ms floor, is settled first: R3184-01.)"""
    calls: list[int] = []
    timing = assert_linear_time(_model(5e-7, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000] * SETTLED_READINGS + [160_000]
    assert timing.repetitions == 1
    assert (timing.small_size, timing.size) == (40_000, 160_000)
    assert timing.input_growth == 4
    assert timing.ratio == pytest.approx(4.0)


def test_a_larger_reading_under_the_floor_grows_the_input_in_one_step():
    """Task 3178: 518 units of 3138's pattern 24 read 16.5 ms at 64 KB on
    Python 3.10. Reused as the next quarter, 16.5 ms is under the floor
    again, so each unit grew twice (to 1 MB). The next quarter size is
    scaled instead to read 1.2 times the floor: one step, the quarter over
    the floor, and under 40% of the two 4x steps' input."""
    size = 64 * 1024
    per_byte = 0.0165 / size
    calls: list[int] = []
    timing = assert_linear_time(_model(per_byte, 1, calls), size, 0.5, "pat")
    quarter = math.ceil(size * host_timing.GROWTH_STEP_MARGIN
                        * GROWTH_FLOOR_SECONDS / 0.0165)
    own_pair_calls = [size // GROWTH, size] * SETTLED_READINGS
    assert calls == own_pair_calls + [quarter, quarter * GROWTH]
    assert timing.small_seconds >= GROWTH_FLOOR_SECONDS
    assert timing.input_growth == pytest.approx(quarter / (size // GROWTH))
    assert timing.ratio == pytest.approx(4.0)
    assert sum(calls[len(own_pair_calls):]) < 0.4 * (4 + 16) * size


@pytest.mark.parametrize("larger_reading", (0.0161, 0.017, 0.0199, 0.04))
def test_growth_of_four_to_the_1_6_main_caught_still_fails(larger_reading):
    """4 ** 1.6 is 9.2x. Main's 2 ms floor caught it from a 16 ms larger
    reading on; a scaled step lands the quarter over the floor, where it
    still reads 9.2x."""
    size = 40_000
    per_unit = larger_reading / size ** 1.6

    def seconds_at(at_size: int) -> float:
        return per_unit * at_size ** 1.6

    assert growth_ratio(seconds_at(size // GROWTH), seconds_at(size),
                        DETECTION_FLOOR_SECONDS) >= GROWTH_LIMIT
    with pytest.raises(TimingCheckFailed, match="superlinear growth"):
        assert_linear_time(seconds_at, size, 10.0, "n^1.6")
    with pytest.raises(TimingCheckFailed, match="parts: steep"):
        assert_linear_times(
            lambda at_size: {"flat": 1e-9 * at_size,
                             "steep": seconds_at(at_size)},
            size, 10.0, "parts")


def test_the_growing_part_with_the_smallest_reading_sets_the_step():
    """Every growing part's quarter must reach the floor: the step is
    scaled from the smallest larger reading among the growing parts."""
    size = 40_000
    calls: list[int] = []

    def seconds_at(at_size: int) -> dict[str, float]:
        calls.append(at_size)
        return {"a": 0.0165 * at_size / size, "b": 0.019 * at_size / size,
                "fast": 0.001 * at_size / size}

    timings = assert_linear_times(seconds_at, size, 0.5, "parts")
    # The own pair (settled, R3184-01), then one scaled growth step.
    assert len(calls) == 2 * SETTLED_READINGS + 2
    assert timings["a"].small_seconds >= GROWTH_FLOOR_SECONDS
    assert all(timing.ratio == pytest.approx(4.0)
               for name, timing in timings.items() if name != "fast")


def test_the_scaled_step_never_passes_the_input_growth_cap():
    next_quarter_size = host_timing.next_quarter_size
    assert next_quarter_size(40_000, 0.02, 10**9) == 40_000
    assert next_quarter_size(40_000, 0.016, 10**9) == 60_000
    assert next_quarter_size(40_000, 0.016, 50_000) == 50_000
    # Never scaled past what a 16 ms reading needs.
    assert next_quarter_size(40_000, 0.001, 10**9) == 60_000


def test_the_ci_reading_is_measured_again_and_passes():
    """Task 3175: CI read a linear regex at 0.0006 s against 0.0052 s, a
    sub-millisecond ratio of 8.7x. That larger reading could reach the
    limit once repeated (the test's own pair, b81777b's rule), so both
    sizes run 32 times and their means (0.6 ms against 2.5 ms) are linear;
    5.2 ms is under the 16 ms that quadratic work main caught reads at, so
    the input does not grow. The same spike over a 5 ms quarter reading
    (24 ms) cannot reach the limit when repeated, so it is not repeated;
    its quarter reading is over main's 2 ms floor, so the pair is read once
    more before it passes (R3184-01), and it grows the input: at 4 times it
    the fastest reading (20 ms, now the quarter) against 80 ms is linear."""
    for quarter, spike, runs, grown in (
            (0.0006, 0.0052, MAX_GROWTH_REPETITIONS, 1),
            (0.005, 0.024, SETTLED_READINGS, 4)):
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
        assert calls.count(10_000) == calls.count(40_000) == runs
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
    read 4x. The test's own pair is held to main's 2 ms floor (16x), after
    four runs of each size (b81777b: 20 ms against 320 ms); it fails there,
    before the input grows to 1.28 s."""
    calls: list[int] = []
    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth.*16\.0x at 1x the test's "
                             r"input over 4 runs of each, floor 0\.002 s"):
        assert_linear_time(_model(5e-11, 2, calls), 40_000, 0.5, "quad")
    assert max(calls) == 40_000


def test_quadratic_work_past_the_tests_size_fails_on_the_grown_input():
    """Linear up to the test's size and quadratic past it: the test's own
    pair reads 4x, so only the grown input (task 3178) sees the growth: it
    fails at the first grown pair (40,000 then 160,000 units: 20 ms then
    320 ms), against the 20 ms floor 3178 held grown sizes to."""
    def seconds_at(size: int) -> float:
        return 0.02 * size / 40_000 * max(1.0, size / 40_000)

    with pytest.raises(TimingCheckFailed,
                       match=r"superlinear growth.*at 4x the test's input"
                             r", floor 0\.02 s"):
        assert_linear_time(seconds_at, 40_000, 10.0, "late")


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


def _pair_work_seconds(units: int) -> float:
    """The CPU time of one ``_pair_work(units)`` call, collector paused."""
    with host_timing.collector_paused():
        started = time.process_time()
        _pair_work(units)
        return time.process_time() - started


# Readings within this share of the target end the calibration.
PLANT_CALIBRATION_TOLERANCE = 0.1
PLANT_CALIBRATION_ROUNDS = 5


def _units_reading(target_seconds: float) -> int:
    """How many units ``_pair_work`` takes ``target_seconds`` of CPU time on
    here, measured on the work itself: the median of three readings at a
    size, rescaled (quadratic work: by the square root of the ratio) until
    one lands within PLANT_CALIBRATION_TOLERANCE of the target. Task 3180:
    one 400-unit reading, scaled 30 times over, planted a reading outside
    16-159 ms on a CI runner, and the proof passed (DID NOT RAISE)."""
    units = 400
    for _ in range(PLANT_CALIBRATION_ROUNDS):
        reading = statistics.median(_pair_work_seconds(units)
                                    for _ in range(3))
        if abs(reading / target_seconds - 1) <= PLANT_CALIBRATION_TOLERANCE:
            break
        units = max(GROWTH, round(
            units * math.sqrt(target_seconds / max(reading, 1e-6))))
    return units


def planted_band(target_seconds: float) -> tuple[float, float]:
    """The readings a planted proof aiming at ``target_seconds`` must land
    in at the test's size: inside 16.5-159 ms (quadratic work main's 2 ms
    floor caught, up to the 159 ms of IR75-01), within a quarter (-) or a
    third (+) of the target. The real-clock proofs aim at ``plant_target``
    (30 ms and up, task 3208), so they plant 22.5-159 ms; 16.5-22.5 ms is
    proved on the scripted clock."""
    return (max(1.03 * host_timing.GROWTH_DETECTION_SECONDS,
                0.75 * target_seconds),
            min(0.159, target_seconds * 4 / 3))


# The fastest reading at the test's size a planted proof must reach: clearly
# above the GROWTH_FLOOR_SECONDS noise floor (task 3208). CI run 1 of PR #44
# planted 0.0190 s and 0.0181 s at the 20 ms target, their fastest readings
# 14 ms (under the 16 ms main's floor catches), and the check rightly left
# them unjudged: the proofs failed on a plant, not on the check.
PLANT_FLOOR_SECONDS = 1.1 * GROWTH_FLOOR_SECONDS
# The lowest target a plant calibrates to, so that its readings land above
# PLANT_FLOOR_SECONDS on a runner whose fastest reading is a quarter under
# its median; and the highest a plant re-calibrates up to, the top proof's
# target (the band then stays inside 16-159 ms).
PLANT_LOWEST_TARGET = 1.5 * GROWTH_FLOOR_SECONDS
PLANT_HIGHEST_TARGET = 0.12


def plant_target(target_seconds: float,
                 fastest_seconds: float | None = None) -> float:
    """The target a proof aiming at ``target_seconds`` calibrates its plant
    to (task 3208): at least PLANT_LOWEST_TARGET. After a plant whose
    fastest reading at the test's size, ``fastest_seconds``, missed
    PLANT_FLOOR_SECONDS, re-calibrated up by the share that reading fell
    short of PLANT_LOWEST_TARGET, never past PLANT_HIGHEST_TARGET."""
    target = max(target_seconds, PLANT_LOWEST_TARGET)
    if fastest_seconds is not None and fastest_seconds < PLANT_FLOOR_SECONDS:
        target = max(target, min(PLANT_HIGHEST_TARGET, target
                                 * PLANT_LOWEST_TARGET
                                 / max(fastest_seconds, 1e-6)))
    return target


def planted_over_the_floor(fastest_seconds: float, units: int) -> str:
    """The message of a plant whose fastest reading at ``units`` units
    stayed under PLANT_FLOOR_SECONDS (``plant_target``)."""
    return (f"the fastest planted reading {fastest_seconds:.4f} s at {units} "
            f"units stayed under the {PLANT_FLOOR_SECONDS:.4f} s a plant must "
            f"clear on this host")


# How many times a real-clock proof plants its work before its band and
# floor assertions decide (task 3209): the first plant at the units
# ``_units_reading`` calibrates, every later one calibrated afresh and at
# the units ``replanted_units`` re-scales from the plants before it (a
# damped estimate of the runner's bias over all of them, task 3210). A
# proof always has the PLANTS_KEPT plants it had before re-scaling, and
# plants up to PLANTS times while the next plant fits in the test's 30 s
# budget (PLANT_SECONDS of wall time). A plant takes 4.5-6 s here, 9 s with
# the 0.3 s build, and CI ran three in 18 s.
#
# Task 3210 (R3209-I-04): the budget counts a plant's wall time less the
# sleep that models building the shape. That sleep was 3.4-8.2 s of each
# 0.3 s-build plant here, which left 0.8-2.3 s to count. Counted, the sleep
# left those proofs 3 plants where the others get 5, and with 3 plants
# the re-scaling false-failed more often than the old loop on an unbiased
# noisy runner. Every plant must also clear its band, so a plant that
# keeps missing ends only at PLANTS (about 45 s with the build).
PLANTS_KEPT = 3
PLANTS = 5
PLANT_SECONDS = 30.0


def another_plant(plant_seconds: list[float]) -> bool:
    """Whether a proof whose plants so far each took ``plant_seconds`` of
    wall time, the last one off its band, plants again: always up to
    PLANTS_KEPT plants; past them, up to PLANTS while one more plant as
    long as the longest so far still ends within PLANT_SECONDS."""
    if len(plant_seconds) < PLANTS_KEPT:
        return True
    return (len(plant_seconds) < PLANTS
            and sum(plant_seconds) + max(plant_seconds) <= PLANT_SECONDS)


# A proof's calibration: the units ``_units_reading`` returned and the
# target it calibrated them to.
Calibration = tuple[int, float]


class Plant(NamedTuple):
    """A plant: the calibration it ran from, the units it ran at and its
    first reading at the test's size (the reading its band check reads).
    Every plant is calibrated afresh on its target, as before task 3209,
    and its bias is read against its own calibration (task 3210)."""

    calibration: Calibration
    units: int
    reading: float

    def log_bias(self) -> float:
        """The log of the plant's reading over what its calibration
        predicts at its units. The calibrated units read the calibrated
        target there, and the planted work is quadratic in its units, so
        the prediction is the target x (units / calibrated units) ** 2."""
        calibrated_units, calibrated_seconds = self.calibration
        if calibrated_units <= 0 or calibrated_seconds <= 0:
            raise ValueError(f"a calibration of {calibrated_units} units for "
                             f"{calibrated_seconds} s predicts no reading")
        predicted = calibrated_seconds * (self.units / calibrated_units) ** 2
        return math.log(max(self.reading, 1e-6) / predicted)


# How the runner's bias is estimated (task 3210, R3209-I-04). A plant's bias
# is its reading over what its calibration predicts at its units. Each plant
# reads the runner's bias off by its own noise (its calibration's included),
# a log normal of PLANT_NOISE. The estimate is the posterior mean of the log
# bias over every plant so far, under a prior that the runner is unbiased
# with probability PLANT_UNBIASED_SHARE and is otherwise biased by a log
# normal of PLANT_BIAS_SPREAD. So a single miss of the size noise explains
# barely moves the next plant. A miss as large as CI's 0.65 moves it most of
# the way, and plants that agree pull the estimate to their geometric mean.
PLANT_NOISE = 0.15
PLANT_BIAS_SPREAD = 0.4
PLANT_UNBIASED_SHARE = 0.85


def runner_bias(plants: Sequence[Plant]) -> float:
    """The damped estimate of how much this runner's readings at the test's
    size differ from what their calibrations predict (task 3210), 1.0
    before any plant.

    Task 3209 re-scaled each plant from the one reading before it. On an
    unbiased runner, a plant that missed on noise was then re-scaled by
    that noise, and the next one tended to miss the other way: the proofs
    whose 0.3 s build fits only 3 plants false-failed more often than the
    old re-calibrate loop at noise 0.10-0.15 (R3209-I-04)."""
    if not plants:
        return 1.0
    log_biases = [plant.log_bias() for plant in plants]
    mean = statistics.fmean(log_biases)
    noise_variance = PLANT_NOISE ** 2 / len(log_biases)
    biased_variance = PLANT_BIAS_SPREAD ** 2 + noise_variance
    # The log odds that the runner is biased: the prior odds times the
    # likelihood ratio of the mean under each hypothesis (normal densities
    # of zero mean), taken in logs so a far-off mean cannot overflow.
    log_odds = (math.log((1 - PLANT_UNBIASED_SHARE) / PLANT_UNBIASED_SHARE)
                - 0.5 * math.log(biased_variance / noise_variance)
                + mean ** 2 / 2 * (1 / noise_variance - 1 / biased_variance))
    biased_share = 1 / (1 + math.exp(min(-log_odds, 700.0)))
    return math.exp(biased_share * mean
                    * PLANT_BIAS_SPREAD ** 2 / biased_variance)


def replanted_units(calibration: Calibration, plants: Sequence[Plant],
                    target_seconds: float) -> int:
    """The units the next plant runs at, after ``plants`` (task 3209): the
    units its own ``calibration`` predicts read ``target_seconds`` on a
    runner of ``runner_bias`` (task 3210). The planted work is quadratic in
    its units at the test's size, so the units scale by the square root, as
    ``_units_reading`` scales them, and by at most GROWTH either way from
    the calibrated units.

    CI run 4 of PR #44: a plant calibrated to 50.8 ms at 860 units read
    0.0330 s, under its 0.0381-0.0677 s band. Calibrating again on the same
    target can return the same units, and that plant can miss the same
    way. Re-scaled by the damped estimate of that reading, the next plant
    lands in the band, and a later one moves closer to the 50.8 ms target
    as the plants agree.

    Each plant is calibrated afresh, as the old loop calibrated it. Task
    3210 first kept the first plant's calibration for every plant. Its
    error was then the same on each plant, and the estimate damps a small
    shared error like noise. The burst proof (``test_host_timing_3184``)
    aims at 0.14 s, a ninth under its band's upper edge, and lost the old
    loop's fresh calibrations: in the reviewer's model it never landed 9
    times as often as the old loop on an unbiased runner at noise 0.05."""
    if not plants:
        raise ValueError("a plant is re-planted only after a first plant")
    calibrated_units, calibrated_seconds = calibration
    if calibrated_units <= 0 or calibrated_seconds <= 0:
        raise ValueError(f"a calibration of {calibrated_units} units for "
                         f"{calibrated_seconds} s predicts no reading")
    units = calibrated_units * math.sqrt(
        target_seconds / (calibrated_seconds * runner_bias(plants)))
    return max(GROWTH, round(min(max(units, calibrated_units / GROWTH),
                                 calibrated_units * GROWTH)))


def timing_failure(check: Callable[[], object]) -> TimingCheckFailed | None:
    """The ``TimingCheckFailed`` ``check()`` raised, or None if it passed."""
    try:
        check()
    except TimingCheckFailed as failure:
        return failure
    return None


@pytest.mark.parametrize("build_seconds", (0.0, 0.3))
@pytest.mark.parametrize("target_seconds", (PLANT_LOWEST_TARGET, 0.12))
def test_real_quadratic_work_of_22_5_to_159_ms_fails(target_seconds,
                                                     build_seconds):
    """IR75-01 on real work and the real clock: quadratic work planted at
    30 ms (a 22.5-40 ms band) or 120 ms (90-159 ms) at the test's size
    fails, also when building the shape takes 0.3 s of wall time per call
    (3175 then measured each size once and raised the quarter reading to
    the 20 ms floor). Readings of 16.5-22.5 ms are proved on the scripted
    clock (``test_quadratic_work_of_16_to_159_ms_fails_whatever_a_call_
    costs``): the real clock cannot plant them over the noise floor.

    Task 3180: the proof must plant what it claims on any host. The size
    is calibrated on the work itself, the reading at the test's size is
    recorded, and the test asserts it landed in the band (re-planted as
    ``another_plant`` allows) before asserting the check failed on it.

    Task 3208: the plant clears the noise floor. The lowest proof plants
    at PLANT_LOWEST_TARGET (30 ms, ``plant_target`` of the 20 ms aim), and
    every reading at the test's size, the fastest the check settles on
    included, must reach PLANT_FLOOR_SECONDS; a plant under it is
    re-calibrated up to a higher target.

    Task 3209: a plant that misses its band, under or over it, runs again
    at units re-scaled from the readings at the test's size
    (``replanted_units``), so the plants converge on the band; one that
    still misses at its last plant (``another_plant``) fails on the band
    assertion. Task 3210: every plant is calibrated afresh, then re-scaled
    by a damped estimate of the runner's bias over every plant so far
    (``runner_bias``), not from the last reading alone."""
    target = plant_target(target_seconds)
    plants: list[Plant] = []
    plant_seconds: list[float] = []
    while not plant_seconds or another_plant(plant_seconds):
        if plant_seconds:
            target = plant_target(target, fastest)
        calibration = (_units_reading(target), target)
        units = (replanted_units(calibration, plants, target) if plants
                 else calibration[0])
        lower, upper = planted_band(target)
        readings: dict[int, list[float]] = {}
        built: list[float] = []

        def seconds_at(size: int, units=units, readings=readings,
                       built=built) -> float:
            built.append(build_seconds * size / units)
            time.sleep(built[-1])
            started = time.process_time()
            _pair_work(size)
            elapsed = time.process_time() - started
            readings.setdefault(size, []).append(elapsed)
            return elapsed

        plant_started = time.monotonic()
        failure = timing_failure(lambda: assert_linear_time(
            seconds_at, units, 30.0, "planted quadratic"))
        plant_seconds.append(time.monotonic() - plant_started - sum(built))
        planted, fastest = readings[units][0], min(readings[units])
        plants.append(Plant(calibration, units, planted))
        if lower <= planted <= upper and fastest >= PLANT_FLOOR_SECONDS:
            break
    assert lower <= planted <= upper, (
        f"the planted reading {planted:.4f} s at {units} units missed the "
        f"{lower:.4f}-{upper:.4f} s band on this host")
    assert fastest >= PLANT_FLOOR_SECONDS, planted_over_the_floor(fastest,
                                                                 units)
    assert failure is not None and "superlinear growth" in str(failure), (
        f"quadratic work reading {planted:.4f} s passed: {readings}")


@pytest.mark.parametrize("target_seconds, fastest_seconds, planted", [
    # The 20 ms proof plants at 30 ms; the 120 ms one as aimed.
    (0.02, None, PLANT_LOWEST_TARGET),
    (0.12, None, 0.12),
    # A plant whose fastest reading cleared the floor is planted again as
    # aimed; one under it is re-calibrated up by the share it fell short.
    (0.03, 0.025, 0.03),
    (0.03, 0.019, 0.03 * PLANT_LOWEST_TARGET / 0.019),
    # Never past the top proof's target.
    (0.12, 0.015, PLANT_HIGHEST_TARGET),
])
def test_a_plant_under_the_floor_is_recalibrated_up(target_seconds,
                                                    fastest_seconds, planted):
    assert plant_target(target_seconds, fastest_seconds) == pytest.approx(
        planted)


def test_every_planted_band_clears_the_floor():
    """Task 3208: CI planted 0.0190 s at the 20 ms target, under the noise
    floor; a plant in the band of any proof's target now reads over
    PLANT_FLOOR_SECONDS, which is over it."""
    assert PLANT_FLOOR_SECONDS > GROWTH_FLOOR_SECONDS > 0.0190
    for target_seconds in (0.02, 0.12):
        lower, upper = planted_band(plant_target(target_seconds))
        assert PLANT_FLOOR_SECONDS <= lower < upper <= 0.159


# --- Task 3209: a plant that misses its band is re-scaled from its reading ---


# CI run 4 of PR #44 (3.12 runner): the 30 ms proof's target had been raised
# to 50.8 ms (band 0.0381-0.0677 s), and its plant, calibrated at 860
# units, read 0.0330 s there in the check.
CI_PLANT_TARGET = 0.0380904614035782 / 0.75
CI_PLANT_UNITS = 860
CI_PLANTED_SECONDS = 0.03297001700000024


class _ScriptedRunner:
    """A runner on a scripted CPU clock, replaying a real-clock proof (task
    3209). ``_pair_work_seconds``, which ``_units_reading`` calibrates on,
    reads ``calibrated_seconds`` at ``units`` units. The first plant (the
    first ``assert_linear_time`` call) reads ``planted_seconds`` there, and
    every later plant reads ``drift`` times the CPU time per pair of the
    plant before it. ``plants`` keeps each plant's size and its readings by
    size; the build sleeps advance ``wall``, the clock host_timing reads."""

    def __init__(self, wall: _BuildClock, units: int,
                 calibrated_seconds: float, planted_seconds: float,
                 drift: float) -> None:
        self.wall = wall
        self.calibrated_per_pair = calibrated_seconds / units ** 2
        self.per_pair = planted_seconds / units ** 2 / drift
        self.drift = drift
        self.now = 0.0
        self.plants: list[tuple[int, dict[int, list[float]]]] = []

    def calibration_reading(self, units: int) -> float:
        return self.calibrated_per_pair * units ** 2

    def process_time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.wall.now += seconds

    def spin_until(self, started: float, seconds: float) -> None:
        """A burst of contention spent up to ``seconds`` after ``started``
        on the scripted CPU clock (``test_host_timing_3184``'s
        ``_spin_cpu_until``, whose busy-wait never ends on a clock only the
        work advances)."""
        self.now = max(self.now, started + seconds)

    def work(self, size: int, window: int | None = None) -> int:
        """``_pair_work(size)`` or ``_windowed_pair_work(size, window)``:
        min(size, window) x size pairs of scripted CPU time."""
        seconds = self.per_pair * min(size, window or size) * size
        self.now += seconds
        self.plants[-1][1].setdefault(size, []).append(seconds)
        return 0

    def start_plant(self, size: int) -> None:
        self.per_pair *= self.drift
        self.plants.append((size, {}))

    def planted(self) -> list[tuple[int, float]]:
        """Each plant's units and its first reading there."""
        return [(size, readings[size][0]) for size, readings in self.plants]


def scripted_runner(monkeypatch, proof_module, work_name: str,
                    drift: float = 1.0) -> _ScriptedRunner:
    """A ``_ScriptedRunner`` replaying CI run 4 of PR #44 under the real-
    clock proof in ``proof_module`` whose work is ``work_name``: calibrated
    at CI_PLANT_UNITS for CI_PLANT_TARGET, its first plant reading
    CI_PLANTED_SECONDS there."""
    runner = _ScriptedRunner(_build_clock(monkeypatch), CI_PLANT_UNITS,
                             CI_PLANT_TARGET, CI_PLANTED_SECONDS, drift)
    monkeypatch.setattr(sys.modules[__name__], "_pair_work_seconds",
                        runner.calibration_reading)
    monkeypatch.setattr(proof_module, "time", SimpleNamespace(
        process_time=runner.process_time, sleep=runner.sleep,
        monotonic=runner.wall.monotonic))
    monkeypatch.setattr(proof_module, work_name, runner.work)
    check = proof_module.assert_linear_time

    def planted_check(seconds_at, size, *args, **kwargs):
        runner.start_plant(size)
        return check(seconds_at, size, *args, **kwargs)

    monkeypatch.setattr(proof_module, "assert_linear_time", planted_check)
    return runner


CI_BAND_MISSED = r"missed the 0\.0381-0\.0677 s band on this host"


def test_the_ci_plant_under_its_band_lands_in_it_once_replanted(monkeypatch):
    """CI run 4 of PR #44: the plant read 0.0330 s at 860 units against
    its 0.0381-0.0677 s band. Re-scaled from the damped estimate of that
    reading (task 3210: a bias of 0.77, where the reading alone says 0.65),
    the next plant runs at 982 units and reads 43.0 ms, inside the band.
    The proof's check fails the quadratic work there."""
    runner = scripted_runner(monkeypatch, sys.modules[__name__],
                             "_pair_work")
    test_real_quadratic_work_of_22_5_to_159_ms_fails(CI_PLANT_TARGET, 0.0)
    lower, upper = planted_band(CI_PLANT_TARGET)
    (first_units, first), (units, planted) = runner.planted()
    assert (first_units, f"{first:.4f}") == (CI_PLANT_UNITS, "0.0330")
    assert first < lower
    assert units == 982
    assert f"{planted:.4f}" == "0.0430"
    assert lower <= planted <= upper


def test_the_ci_plant_calibrated_again_misses_again(monkeypatch):
    """The control: a plant calibrated again on the same target and run at
    its calibrated units, as every plant was before task 3209 (task 3210
    keeps the calibration and drops only the re-scaling), runs at 860
    units again on CI's runner, and the proof fails with CI's message
    after its last plant."""
    runner = scripted_runner(monkeypatch, sys.modules[__name__],
                             "_pair_work")
    monkeypatch.setattr(sys.modules[__name__], "replanted_units",
                        lambda calibration, plants, target: calibration[0])
    with pytest.raises(AssertionError,
                       match=r"the planted reading 0\.0330 s at 860 units "
                             + CI_BAND_MISSED):
        test_real_quadratic_work_of_22_5_to_159_ms_fails(CI_PLANT_TARGET, 0.0)
    assert [units for units, _ in runner.planted()] == [860] * PLANTS


# Every plant of a runner that never lands reads this share of the CPU time
# per pair of the plant before it. CI's 0.65 keeps each plant under its
# band, and the floor raises each later plant's target and band
# (``plant_target``). 2.0 puts every re-scaled plant over its band. At 1.5,
# the damped estimate (task 3210) lands the second plant: its bias reads
# 1.5 x 0.65 = 0.97.
NEVER_LANDING_DRIFTS = (CI_PLANTED_SECONDS / CI_PLANT_TARGET, 2.0)
BAND_MISSED = r"missed the [\d.]+-[\d.]+ s band on this host"


def never_landed(plants: Sequence[tuple[int, float]]) -> bool:
    """Whether every plant of a scripted CI replay missed its own band.
    Each plant's band is the band of CI_PLANT_TARGET unless a plant before
    it read under the floor, which only raises the target and the band
    (``plant_target``). So a plant under the first band missed its own,
    and while no plant reads under the floor, each band is the first."""
    lower, upper = planted_band(CI_PLANT_TARGET)
    readings = [reading for _, reading in plants]
    if all(reading < lower for reading in readings):
        return True
    return (min(readings) >= PLANT_FLOOR_SECONDS
            and not any(lower <= reading <= upper for reading in readings))


@pytest.mark.parametrize("drift", NEVER_LANDING_DRIFTS)
def test_a_runner_whose_plants_never_land_still_fails_loudly(monkeypatch,
                                                             drift):
    """Re-scaling converges only on a runner that holds still. A runner
    whose every plant reads off by ``drift`` again misses each band. It
    plants as often as ``another_plant`` allows, then fails on the band,
    naming the last plant's reading."""
    runner = scripted_runner(monkeypatch, sys.modules[__name__],
                             "_pair_work", drift)
    with pytest.raises(AssertionError, match=BAND_MISSED):
        test_real_quadratic_work_of_22_5_to_159_ms_fails(CI_PLANT_TARGET, 0.0)
    planted = runner.planted()
    assert len(planted) == PLANTS
    assert never_landed(planted)


def test_a_runner_drifting_half_again_lands_on_its_second_plant(monkeypatch):
    """Task 3210 replaced 1.5 in NEVER_LANDING_DRIFTS: under the damped
    estimate such a runner is no longer one that never lands. Its second
    plant reads 1.5 x 0.65 = 0.97 of its calibration, at the 982 units the
    first plant's 0.65 re-scales to, inside the band, and the proof's
    check fails the quadratic work there."""
    runner = scripted_runner(monkeypatch, sys.modules[__name__],
                             "_pair_work", 1.5)
    test_real_quadratic_work_of_22_5_to_159_ms_fails(CI_PLANT_TARGET, 0.0)
    lower, upper = planted_band(CI_PLANT_TARGET)
    (_, first), (units, planted) = runner.planted()
    assert first < lower
    assert units == 982
    assert lower <= planted <= upper


# The calibration of the scripted plants below: 1000 units read 30 ms.
CALIBRATED_30_MS = (1000, 0.03)


def calibrated_plant(reading: float, units: int = 1000,
                     calibration: Calibration = CALIBRATED_30_MS) -> Plant:
    """A scripted plant of ``units`` units reading ``reading`` at the
    test's size, calibrated to ``calibration`` (1000 units read 30 ms)."""
    return Plant(calibration, units, reading)


CI_PLANT = Plant((CI_PLANT_UNITS, CI_PLANT_TARGET), CI_PLANT_UNITS,
                 CI_PLANTED_SECONDS)


@pytest.mark.parametrize("calibration, plants, target_seconds, replanted", [
    # CI run 4: under the band, re-scaled up by the damped estimate (a bias
    # of 0.77 where the reading says 0.65).
    ((CI_PLANT_UNITS, CI_PLANT_TARGET), [CI_PLANT], CI_PLANT_TARGET, 982),
    # Twice the calibration: over the band, re-scaled down (a bias of 1.83).
    (CALIBRATED_30_MS, [calibrated_plant(0.06)], 0.03, 738),
    # 1.4 times it, a miss noise of PLANT_NOISE can explain: barely moved.
    (CALIBRATED_30_MS, [calibrated_plant(0.042)], 0.03, 948),
    # Readings on both sides of the calibration: no bias, back to it.
    (CALIBRATED_30_MS, [calibrated_plant(0.042),
                        calibrated_plant(0.03 / 1.4)], 0.03, 1000),
    # In the band but under the floor: calibrated afresh on the raised
    # target, and re-scaled to it from a stale calibration as well.
    ((1033, 0.05), [calibrated_plant(0.03, 800, (800, 0.03))], 0.05, 1033),
    ((800, 0.03), [calibrated_plant(0.03, 800, (800, 0.03))], 0.05, 1033),
    # The next plant follows its own calibration, not the last plant's
    # units; each plant's bias is read against the calibration it ran from.
    ((1100, 0.03), [calibrated_plant(0.03)], 0.03, 1100),
    ((900, 0.03), [calibrated_plant(0.03 * CI_PLANTED_SECONDS
                                    / CI_PLANT_TARGET)], 0.03, 1028),
    # At most GROWTH times either way from the calibrated units, never
    # under GROWTH units.
    (CALIBRATED_30_MS, [calibrated_plant(0.0)], 0.03, 4000),
    (CALIBRATED_30_MS, [calibrated_plant(10.0)], 0.03, 250),
    ((GROWTH, 0.03), [calibrated_plant(1.0, GROWTH, (GROWTH, 0.03))], 0.03,
     GROWTH),
])
def test_a_plant_is_replanted_from_the_plants_before_it(
        calibration, plants, target_seconds, replanted):
    assert replanted_units(calibration, plants,
                           target_seconds) == replanted


def test_quadratic_work_is_replanted_by_the_square_root():
    """Scaling the units by the ratio itself (units x target / reading)
    squares it on quadratic work. CI's plant would then read 78 ms, over
    its band. Scaled by the square root of the damped estimate, it lands
    inside the band."""
    lower, upper = planted_band(CI_PLANT_TARGET)
    ratio = CI_PLANT_TARGET / CI_PLANTED_SECONDS
    assert CI_PLANTED_SECONDS * ratio ** 2 > upper
    replanted = replanted_units((CI_PLANT_UNITS, CI_PLANT_TARGET),
                                [CI_PLANT], CI_PLANT_TARGET)
    reading = CI_PLANTED_SECONDS * (replanted / CI_PLANT_UNITS) ** 2
    assert lower <= reading <= upper


# The runner's bias each scripted plant reads, as a share of what the
# calibration predicts: CI's systematic 0.65, and readings that stray on
# noise of PLANT_NOISE.
CI_BIAS = CI_PLANTED_SECONDS / CI_PLANT_TARGET


def test_one_miss_noise_explains_barely_moves_the_next_plant():
    """R3209-I-04: re-scaled from one reading alone, a plant 1.35 times its
    calibration on an unbiased runner was scaled down by 1.35, and the
    next plant, reading true, missed under the band. The damped estimate
    moves it by under a tenth of that; CI's 0.65 moves it most of the way."""
    noisy = runner_bias([calibrated_plant(0.03 * 1.35)])
    biased = runner_bias([calibrated_plant(0.03 * CI_BIAS)])
    assert 1.0 < noisy < 1.1
    assert CI_BIAS < biased < 0.8
    lower, _ = planted_band(0.03)
    assert 0.03 / noisy > lower
    assert 0.03 * CI_BIAS / biased > lower


def test_plants_that_agree_pull_the_estimate_to_their_bias():
    """The more plants read CI's bias, the closer the estimate comes to it;
    readings on both sides of the calibration cancel."""
    estimates = [runner_bias([calibrated_plant(0.03 * CI_BIAS)] * count)
                 for count in range(1, PLANTS + 1)]
    assert estimates == sorted(estimates, reverse=True)
    assert estimates[-1] == pytest.approx(CI_BIAS, rel=0.02)
    assert runner_bias([calibrated_plant(0.03 * 1.35),
                        calibrated_plant(0.03 / 1.35)]) == pytest.approx(1.0)


@pytest.mark.parametrize("bias", (CI_BIAS, 1.35, 4.0, 1e-3))
def test_the_estimate_is_the_same_either_way_of_the_calibration(bias):
    """The bias is estimated on its logarithm: a runner reading ``bias``
    times its calibration and one reading its inverse are damped alike,
    and a reading far off (or none) cannot overflow the estimate."""
    over = runner_bias([calibrated_plant(0.03 * bias)])
    under = runner_bias([calibrated_plant(0.03 / bias)])
    assert over * under == pytest.approx(1.0)


def test_the_estimate_reads_every_plant_at_its_own_units():
    """A plant at twice the calibrated units is predicted to read four
    times the calibrated target (quadratic work): reading that is no bias."""
    assert runner_bias([calibrated_plant(0.12, 2000)]) == pytest.approx(1.0)
    assert runner_bias([]) == 1.0


def test_the_estimate_reads_every_plant_against_its_own_calibration():
    """Each plant is calibrated afresh (task 3210), so two plants of the
    same units and reading can carry different calibrations. Read against
    its own, a plant whose calibration halved its units for the same
    target predicts 120 ms at 1000 units: reading it is no bias. Read
    against the first plant's calibration, it would read 4 times its
    prediction, and the estimate over both plants nearly 2."""
    first = calibrated_plant(0.03)
    recalibrated = calibrated_plant(0.12, 1000, (500, 0.03))
    assert recalibrated.log_bias() == pytest.approx(0.0)
    assert runner_bias([first, recalibrated]) == pytest.approx(1.0)
    assert runner_bias([first, calibrated_plant(0.12)]) > 1.8


@pytest.mark.parametrize("calibration", ((0, 0.03), (1000, 0.0), (-5, -1.0)))
def test_a_calibration_that_predicts_nothing_is_refused(calibration):
    with pytest.raises(ValueError, match="predicts no reading"):
        runner_bias([calibrated_plant(0.03, 1000, calibration)])
    with pytest.raises(ValueError, match="predicts no reading"):
        replanted_units(calibration, [calibrated_plant(0.03)], 0.03)


def test_a_proof_replants_only_after_a_first_plant():
    with pytest.raises(ValueError, match="only after a first plant"):
        replanted_units(CALIBRATED_30_MS, [], 0.03)


@pytest.mark.parametrize("plant_seconds, plants_again", [
    # The first PLANTS_KEPT plants, however long they take.
    ([], True),
    ([20.0, 20.0], True),
    # Past them, another plant only while it fits in PLANT_SECONDS: five
    # 6 s plants (CI's; a 0.3 s-build plant less its sleep counts 1-2.5 s,
    # task 3210), three 9 s ones.
    ([6.0] * 4, True),
    ([6.0, 6.0, 6.0, 6.1], False),
    ([9.0] * 3, False),
    # Never past PLANTS.
    ([1.0] * 5, False),
])
def test_a_proof_plants_again_within_its_budget(plant_seconds, plants_again):
    assert another_plant(plant_seconds) is plants_again
    assert PLANTS_KEPT == 3 and PLANT_SECONDS == 30.0


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


def _capped(seconds_at, largest_size: int):
    """``seconds_at`` of a shape that does not exist past ``largest_size``
    (a review the gate's artifact cap refuses)."""
    def capped(size: int):
        if size > largest_size:
            raise InputTooLarge(f"size {size} is over {largest_size}")
        return seconds_at(size)

    return capped


# How far the input can grow: not at all, or one step (a 200 KB review
# grows to 800 KB, not to 3.2 MB, under the gate's 2 MB cap).
CAPPED_GROWTH = {"cannot-grow": 1, "grows-once": GROWTH}


@pytest.mark.parametrize("growth", sorted(CAPPED_GROWTH))
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
def test_quadratic_work_of_16_to_159_ms_fails_where_the_input_cannot_grow(
        monkeypatch, reading, growth):
    """Task 3178: under load the review-gate checks grew a 200 KB review
    to 3.2 MB, which the gate does not read. A shape that stops growing is
    decided at the sizes measured against main's 2 ms floor, so quadratic
    work of 16-159 ms still fails (IR75-01)."""
    clock = _build_clock(monkeypatch)
    seconds_at = _capped(_planted(clock, reading, 0.3, 2),
                         IR75_01_SIZE * CAPPED_GROWTH[growth])
    with pytest.raises(TimingCheckFailed, match="superlinear growth") as failure:
        assert_linear_time(seconds_at, IR75_01_SIZE, 10.0, "quadratic")
    if growth == "cannot-grow":
        # Its quarter reads under 10 ms: growth was tried, and refused.
        assert f"floor {DETECTION_FLOOR_SECONDS:g} s" in str(failure.value)


@pytest.mark.parametrize("growth", sorted(CAPPED_GROWTH))
@pytest.mark.parametrize("reading", (0.001, 0.005) + IR75_01_LARGE_READINGS)
def test_linear_work_passes_where_the_input_cannot_grow(
        monkeypatch, reading, growth):
    clock = _build_clock(monkeypatch)
    calls: list[int] = []
    largest_size = IR75_01_SIZE * CAPPED_GROWTH[growth]
    seconds_at = _capped(_planted(clock, reading, 0.3, 1, calls), largest_size)
    timing = assert_linear_time(seconds_at, IR75_01_SIZE, 10.0, "linear")
    assert timing.ratio < GROWTH_LIMIT
    assert max(calls) <= largest_size
    # Main's floor only where a growth step was tried and refused: the
    # check then stopped at the largest size the shape exists at.
    if timing.floor_seconds == DETECTION_FLOOR_SECONDS:
        assert timing.size == largest_size
    if reading == 0.016 and growth == "cannot-grow":
        assert timing.floor_seconds == DETECTION_FLOOR_SECONDS


@pytest.mark.parametrize("growth", sorted(CAPPED_GROWTH))
@pytest.mark.parametrize("reading", IR75_01_LARGE_READINGS)
def test_a_quadratic_part_fails_where_the_input_cannot_grow(
        monkeypatch, reading, growth):
    clock = _build_clock(monkeypatch)
    quadratic = _planted(clock, reading, 0.3, 2)
    largest_size = IR75_01_SIZE * CAPPED_GROWTH[growth]

    def seconds_at(size: int) -> dict[str, float]:
        if size > largest_size:
            raise InputTooLarge(f"size {size} is over {largest_size}")
        return {"linear": reading * size / IR75_01_SIZE,
                "quadratic": quadratic(size)}

    with pytest.raises(TimingCheckFailed, match="superlinear") as failure:
        assert_linear_times(seconds_at, IR75_01_SIZE, 10.0, "parts")
    assert "parts: quadratic" in str(failure.value)
    assert "parts: linear" not in str(failure.value)


def test_a_shape_too_large_at_the_tests_own_size_is_an_error(monkeypatch):
    """Only growth stops at ``InputTooLarge``: a test whose own sizes do
    not exist is broken, not passed."""
    _build_clock(monkeypatch)
    with pytest.raises(InputTooLarge):
        assert_linear_time(_capped(lambda size: 0.001, IR75_01_SIZE // 2),
                           IR75_01_SIZE, 10.0, "too large")


def test_the_gate_helper_refuses_a_review_over_the_artifact_cap():
    from equipa.security_gate import MAX_REVIEW_ARTIFACT_BYTES
    from tests.review_gate_production import production_seconds

    with pytest.raises(InputTooLarge, match="artifact cap"):
        production_seconds("No findings.\n" + "a" * MAX_REVIEW_ARTIFACT_BYTES)


def test_a_quarter_reading_at_the_floor_is_decided_without_growing():
    calls: list[int] = []
    timing = assert_linear_time(_model(2e-6, 1, calls), 40_000, 0.5, "lin")
    assert calls == [10_000, 40_000] * SETTLED_READINGS
    assert timing.input_growth == 1


def test_compare_with_larger_times_the_size_and_four_times_it():
    calls: list[int] = []
    assert_linear_time(_model(1e-6, 1, calls), 40_000, 0.5, "cap",
                       compare_with_larger=True)
    assert calls == [40_000, 160_000] * SETTLED_READINGS


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
# Wall-clock datetimes, read with any arguments (a time zone):
# ``datetime.now() - started`` (IR75-03, task 3178).
DATETIME_CLOCKS = {"now", "utcnow"}
DATETIME_READINGS = {f"datetime.{clock}" for clock in DATETIME_CLOCKS}
CLOCK_FUNCTIONS = CLOCKS | CLOCKS_BY_ID | USAGE_CLOCKS | DATETIME_READINGS
# Modules whose names an ``import ... as`` can give a clock another name.
CLOCK_MODULES = {"time", "datetime", "os", "resource"}
# Calls that time work themselves: ``timeit.timeit``/``timeit.repeat`` and
# a Timer's ``timeit``/``autorange``.
TIMEIT_METHODS = {"timeit", "autorange"}
# A test is calibrated when it reaches a growth check (IR71-02: only a
# call counts, not a mention of a name such as GROWTH_LIMIT) that settles
# its readings under contention (task 3185).
GROWTH_CHECKS = {"assert_linear_time", "assert_linear_times",
                 "assert_linear_per_unit", "settled_growth"}
# Growth checks whose result the test compares itself: a discarded one
# calibrates nothing (IR75-03).
COMPARED_GROWTH_CHECKS = {"settled_growth"}
# A ratio compared by hand on the test's own readings: one reading of each
# size, decided by the load it ran under (task 3185: CI read a linear scan
# at 0.067 s per MB at 50 KB and 0.137 s at 200 KB, and failed).
HAND_GROWTH_CHECKS = {"growth_ratio"}
BUDGETS = {"budget", "host_factor", "measure_under_load"}
# Modules whose functions are followed when a test calls them.
HELPER_MODULES = {"review_gate_timing", "review_gate_production"}


def _clock_name(function: ast.AST, aliases: Mapping[str, str]) -> str | None:
    """The clock *function* names, through ``aliases`` (``tick`` for
    ``from time import perf_counter as tick``, ``clock`` for ``clock =
    time.perf_counter``, a ``now()`` helper): ``perf_counter`` for
    ``time.perf_counter`` or ``tick``, ``datetime.now`` for
    ``datetime.now`` or ``dt.now``."""
    if isinstance(function, ast.Attribute):
        if function.attr not in DATETIME_CLOCKS:
            return function.attr
        owner = function.value
        owner_name = (owner.id if isinstance(owner, ast.Name)
                      else owner.attr if isinstance(owner, ast.Attribute)
                      else None)
        if owner_name is not None and aliases.get(owner_name, owner_name) == "datetime":
            return f"datetime.{function.attr}"
        return None
    if isinstance(function, ast.Name):
        return aliases.get(function.id, function.id)
    return None


def _is_clock_call(node: ast.AST, aliases: Mapping[str, str] | None = None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _clock_name(node.func, aliases or {})
    if name is None:
        return False
    if name == "getrusage" or name in CLOCKS_BY_ID or name in DATETIME_READINGS:
        return True
    return not node.args and not node.keywords and (
        name in CLOCKS or name in USAGE_CLOCKS)


def _clock_reference(value: ast.AST, aliases: Mapping[str, str]) -> str | None:
    """The clock ``name = <value>`` makes ``name`` another name for:
    ``time.perf_counter`` (not called) or ``lambda: time.perf_counter()``."""
    if isinstance(value, ast.Lambda):
        if (not value.args.args and not value.args.posonlyargs
                and _is_clock_call(value.body, aliases)):
            return _clock_name(value.body.func, aliases)
        return None
    if isinstance(value, (ast.Name, ast.Attribute)):
        name = _clock_name(value, aliases)
        return name if name in CLOCK_FUNCTIONS else None
    return None


def _clock_returned(function: ast.AST, aliases: Mapping[str, str]) -> str | None:
    """The clock a helper ``def now(): return time.perf_counter()`` reads."""
    arguments = function.args
    if len(arguments.posonlyargs) + len(arguments.args) > len(arguments.defaults):
        return None
    body = function.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body = body[1:]
    if (len(body) == 1 and isinstance(body[0], ast.Return)
            and _is_clock_call(body[0].value, aliases)):
        return _clock_name(body[0].value.func, aliases)
    return None


def _clock_aliases(statements: Iterable[ast.AST],
                   inherited: Mapping[str, str]) -> dict[str, str]:
    """``inherited`` plus the clock aliases *statements* define (IR75-03):
    ``from time import perf_counter as tick``, ``clock =
    time.perf_counter``, ``now = lambda: time.monotonic()`` and ``def now():
    return time.perf_counter()``."""
    aliases = dict(inherited)
    for node in statements:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.rpartition(".")[2] not in CLOCK_MODULES:
                continue
            for alias in node.names:
                if alias.asname and alias.asname != alias.name:
                    aliases[alias.asname] = aliases.get(alias.name, alias.name)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            clock = _clock_reference(node.value, aliases)
            if clock is not None:
                aliases[node.targets[0].id] = clock
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            clock = _clock_returned(node, aliases)
            if clock is not None:
                aliases[node.name] = clock
    return aliases


def _reading_key(node: ast.AST) -> str | None:
    """``started`` or ``self.started``: what a clock reading is kept in;
    ``stamps[]`` for any item of a list ``stamps`` of readings."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.unparse(node)
    if isinstance(node, ast.Subscript):
        container = _reading_key(node.value)
        return None if container is None else f"{container}[]"
    return None


def _holds_a_reading(value: ast.AST, aliases: Mapping[str, str]) -> bool:
    """``[clock(), ...]`` or ``[clock() for ...]``: a list of readings."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return any(_is_clock_call(item, aliases) for item in value.elts)
    if isinstance(value, ast.ListComp):
        return _is_clock_call(value.elt, aliases)
    return False


def _clock_names(function: ast.AST,
                 aliases: Mapping[str, str] | None = None) -> set[str]:
    """What *function* (nested functions and methods included) sets to a
    clock reading: ``started = time.process_time()``, ``self.start =
    perf_counter()``, ``start, n = monotonic(), 0``, ``start: float =
    monotonic()``; ``stamps[]`` for a list readings are kept in
    (``stamps.append(clock())``), ``-elapsed`` for ``elapsed =
    -clock()``."""
    aliases = aliases or {}
    names: set[str] = set()
    for node in ast.walk(function):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "insert")
                and any(_is_clock_call(argument, aliases) for argument in node.args)):
            container = _reading_key(node.func.value)
            if container is not None:
                names.add(f"{container}[]")
            continue
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
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
                if key is None:
                    continue
                if isinstance(node, ast.AugAssign):
                    if _holds_a_reading(item_value, aliases):
                        names.add(f"{key}[]")
                elif _is_clock_call(item_value, aliases):
                    names.add(key)
                elif _holds_a_reading(item_value, aliases):
                    names.add(f"{key}[]")
                elif (isinstance(item_value, ast.UnaryOp)
                      and isinstance(item_value.op, ast.USub)
                      and _is_clock_call(item_value.operand, aliases)):
                    names.add(f"-{key}")
    return names


def _measures_elapsed(node: ast.AST, clock_names: set[str],
                      aliases: Mapping[str, str] | None = None) -> bool:
    """``clock() - started`` or ``now - started`` with both sides clock
    readings: a deadline ``clock() + timeout`` or a timestamp ``clock() -
    AGE`` is not a measurement. Also the negated start, ``elapsed =
    -clock()`` then ``elapsed += clock()``, and ``started -= clock()``."""
    def is_reading(side: ast.AST) -> bool:
        return (_is_clock_call(side, aliases)
                or _reading_key(side) in clock_names)

    if isinstance(node, ast.AugAssign):
        key = _reading_key(node.target)
        if key is None or not is_reading(node.value):
            return False
        if isinstance(node.op, ast.Add):
            return f"-{key}" in clock_names
        return isinstance(node.op, ast.Sub) and key in clock_names
    if not isinstance(node, ast.BinOp):
        return False
    left, right = node.left, node.right
    if isinstance(node.op, ast.Add):
        # ``elapsed + clock()`` after ``elapsed = -clock()``.
        return any(f"-{_reading_key(negated)}" in clock_names and is_reading(other)
                   for negated, other in ((left, right), (right, left)))
    if not isinstance(node.op, ast.Sub):
        return False
    if (isinstance(left, ast.Attribute) and isinstance(right, ast.Attribute)
            and left.attr in USAGE_SECONDS and right.attr in USAGE_SECONDS):
        # ``after.ru_utime - before.ru_utime``: fields of two readings.
        left, right = left.value, right.value
    return is_reading(left) and _reading_key(right) in clock_names


def _parents(function: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(function)
            for child in ast.iter_child_nodes(parent)}


def _result_is_compared(call: ast.Call, parents: Mapping[ast.AST, ast.AST],
                        function: ast.AST) -> bool:
    """Whether a ``growth_ratio(...)`` result is compared, returned (to a
    caller that compares it), or kept in a name that is compared: a
    discarded ratio calibrates nothing (IR75-03, task 3178)."""
    node = call
    while not isinstance(parents.get(node), (ast.stmt, type(None))):
        node = parents[node]
        if isinstance(node, ast.Compare):
            return True
    statement = parents.get(node)
    if isinstance(statement, ast.Return):
        return True
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, ast.AnnAssign):
        targets = [statement.target]
    else:
        return False
    kept_in = {target.id for target in targets if isinstance(target, ast.Name)}
    return any(isinstance(compared, ast.Name) and compared.id in kept_in
               for comparison in ast.walk(function)
               if isinstance(comparison, ast.Compare)
               for compared in ast.walk(comparison))


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
        self.classes: dict[str, ast.ClassDef] = {}
        # Fixtures by the name a test requests them under (IR75-03).
        self.fixtures: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                source_module = node.module.rpartition(".")[2]
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (
                        source_module, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
                fixture_name = _fixture_name(node)
                if fixture_name is not None:
                    self.fixtures[fixture_name] = node
                if node.name.startswith("test_"):
                    self.tests[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.functions[node.name] = node
                self.classes[node.name] = node
                for member in node.body:
                    if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and member.name.startswith("test_")):
                        self.tests[f"{node.name}.{member.name}"] = member
        self.aliases = _clock_aliases(tree.body, {})

    def method(self, class_name: str, name: str) -> ast.AST | None:
        """``class_name``'s method ``name``, from it or a base class of
        this module."""
        pending, seen = [class_name], set()
        while pending:
            current = pending.pop(0)
            if current in seen or current not in self.classes:
                continue
            seen.add(current)
            for member in self.classes[current].body:
                if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and member.name == name):
                    return member
            pending += [base.id for base in self.classes[current].bases
                        if isinstance(base, ast.Name)]
        return None


def _fixture_name(function: ast.AST) -> str | None:
    """The name a test requests *function* under, when it is a fixture
    (``@pytest.fixture``, ``@pytest.fixture(name=...)``)."""
    for decorator in function.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if not ast.unparse(target).endswith("fixture"):
            continue
        for keyword in call.keywords if call is not None else []:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
        return function.name
    return None


def _gives_a_reading(fixture: ast.AST, aliases: Mapping[str, str]) -> bool:
    """Whether *fixture* returns or yields a clock reading."""
    aliases = _clock_aliases(ast.walk(fixture), aliases)
    return any(isinstance(node, (ast.Return, ast.Yield))
               and _is_clock_call(node.value, aliases)
               for node in ast.walk(fixture))


def _requested_fixtures(function: ast.AST) -> list[str]:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    arguments = function.args
    return [argument.arg for argument in
            arguments.posonlyargs + arguments.args + arguments.kwonlyargs
            if argument.arg not in ("self", "cls")]


def _load_modules() -> dict[str, _Module]:
    paths = sorted(TESTS_DIR.glob("test_*.py"))
    paths += [TESTS_DIR / f"{name}.py" for name in sorted(HELPER_MODULES)]
    paths.append(TESTS_DIR / "conftest.py")
    return {path.stem: _Module(path.stem, path.read_text(encoding="utf-8"))
            for path in paths}


@dataclass(frozen=True)
class _Reach:
    """What a test does, its called functions included."""

    measures: bool
    growth_checked: bool
    budgeted: bool
    hand_growth: bool = False

    @property
    def verdict(self) -> str:
        if not self.measures:
            return "not timing"
        if self.growth_checked:
            return "calibrated"
        if self.hand_growth:
            return "unsettled growth"
        return "budget only" if self.budgeted else "unscaled"


def _reach(modules: dict[str, _Module], module: _Module,
           node: ast.AST, class_name: str | None = None) -> _Reach:
    """What *node* and every function it calls (or passes on by name, as
    ``assert_linear_time(seconds_at, ...)``) in this module or a followed
    one do: measure elapsed time, reach a growth check, reach a budget.
    Also followed (IR75-03, task 3178): the fixtures a function requests
    (from its module, its class or conftest.py) and, in a method of
    ``class_name``, the ``self.<method>`` it calls or passes on."""
    measures = growth_checked = budgeted = hand_growth = False
    seen: set[int] = {id(node)}
    pending = [(module, node, class_name)]
    conftest = modules.get("conftest")

    def follow(callee_module: _Module, callee: ast.AST | None,
               owner: str | None) -> None:
        if callee is not None and id(callee) not in seen:
            seen.add(id(callee))
            pending.append((callee_module, callee, owner))

    while pending:
        current, function, owner = pending.pop()
        aliases = _clock_aliases(ast.walk(function), current.aliases)
        clock_names = _clock_names(function, aliases)
        parents = _parents(function)
        for request in _requested_fixtures(function):
            in_class = None if owner is None else current.method(owner, request)
            if in_class is not None and _fixture_name(in_class) == request:
                fixture_module, fixture, fixture_owner = current, in_class, owner
            elif request in current.fixtures:
                fixture_module, fixture, fixture_owner = (
                    current, current.fixtures[request], None)
            elif conftest is not None and request in conftest.fixtures:
                fixture_module, fixture, fixture_owner = (
                    conftest, conftest.fixtures[request], None)
            else:
                continue
            if _gives_a_reading(fixture, fixture_module.aliases):
                # ``def started(): return time.perf_counter()``: the
                # requested value is itself a clock reading.
                clock_names.add(request)
            follow(fixture_module, fixture, fixture_owner)
        for child in ast.walk(function):
            measures = measures or _measures_elapsed(child, clock_names, aliases)
            referenced = None
            if isinstance(child, ast.Call):
                measures = measures or _times_with_timeit(child, current.imports)
                name = _called_name(child)
                growth_checked = growth_checked or (
                    name in GROWTH_CHECKS and (
                        name not in COMPARED_GROWTH_CHECKS
                        or _result_is_compared(child, parents, function)))
                hand_growth = hand_growth or (
                    name in HAND_GROWTH_CHECKS
                    and _result_is_compared(child, parents, function))
                budgeted = budgeted or name in BUDGETS
                if name in current.imports:
                    referenced = name
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                referenced = child.id
            elif (owner is not None and isinstance(child, ast.Attribute)
                  and isinstance(child.value, ast.Name)
                  and child.value.id in ("self", "cls")):
                follow(current, current.method(owner, child.attr), owner)
            target = None
            if referenced in current.imports:
                source, original = current.imports[referenced]
                if source in modules and source != "host_timing":
                    target = (source, original)
            elif referenced in current.functions:
                target = (current.name, referenced)
            if target:
                callee = modules[target[0]]
                follow(callee, callee.functions.get(target[1]), None)
    return _Reach(measures, growth_checked, budgeted, hand_growth)


def _reach_test(modules: dict[str, _Module], module: _Module, test: str) -> _Reach:
    """``_reach`` of the test ``name`` or ``Class.name`` of *module*."""
    class_name = test.rpartition(".")[0] or None
    return _reach(modules, module, module.tests[test], class_name)


@pytest.fixture(scope="module")
def timing_reach() -> dict[str, _Reach]:
    modules = _load_modules()
    return {
        f"{module.name}.py::{test}": _reach_test(modules, module, test)
        for module in modules.values() if module.name.startswith("test_")
        for test in module.tests
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


def test_every_growth_check_is_settled_under_contention(timing_reach):
    """Task 3185: a test comparing ``growth_ratio`` by hand decides on one
    reading of each size, at the mercy of the load it ran under."""
    unsettled = sorted(key for key, reach in timing_reach.items()
                       if reach.verdict == "unsettled growth")
    assert not unsettled, (
        "these tests compare growth_ratio by hand on readings "
        "tests/host_timing.py does not settle under contention (use "
        "assert_linear_time, assert_linear_per_unit or settled_growth): "
        f"{unsettled}")


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
            "test_sanitizer_3163.py::test_the_tail_judge_stays_linear",
            # Task 3185: the per-unit check and the settled scan pairs.
            "test_review_gate_polish_3170.py::"
            "test_the_ci_shape_is_linear_from_50kb_to_2mb",
            "test_review_gate_polish_3170.py::"
            "test_no_loops_regex_grows_faster_than_linear",
            "test_review_gate_linear_3167.py::"
            "test_no_loops_regex_is_slow_on_a_bracket_run"]:
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
    # IR75-03 (task 3178): aliases, lists, negated starts, datetimes.
    ("from time import perf_counter as tick\nstart = tick()\nx = tick() - start",
     True),
    ("clock = time.perf_counter\nstart = clock()\nx = clock() - start", True),
    ("now = lambda: time.monotonic()\nstart = now()\nx = now() - start", True),
    ("def now():\n    return time.perf_counter()\n"
     "start = now()\nx = now() - start", True),
    ("stamps = []\nstamps.append(time.perf_counter())\n"
     "stamps.append(time.perf_counter())\nx = stamps[1] - stamps[0]", True),
    ("stamps = [time.perf_counter() for _ in range(2)]\nx = stamps[-1] - stamps[0]",
     True),
    ("elapsed = -time.perf_counter()\nelapsed += time.perf_counter()", True),
    ("elapsed = -time.perf_counter()\nx = elapsed + time.perf_counter()", True),
    ("start = time.perf_counter()\nstart -= time.perf_counter()", True),
    ("from datetime import datetime\nstart = datetime.now()\n"
     "x = (datetime.now() - start).total_seconds()", True),
    ("import datetime as dt\nstart = dt.datetime.utcnow()\n"
     "x = dt.datetime.utcnow() - start", True),
    ("from datetime import datetime as moment\nstart = moment.now(timezone.utc)\n"
     "x = moment.now(timezone.utc) - start", True),
    ("cutoff = datetime.now() - timedelta(days=1)", False),
    ("total = 0\ntotal += time.perf_counter()", False),
    ("start = clock.now()\nx = clock.now() - start", False),
    ("names = [name() for name in hooks]\nx = names[1] - names[0]", False),
])
def test_a_clock_subtraction_is_found_and_a_deadline_is_not(source, measures):
    tree = ast.parse(source)
    aliases = _clock_aliases(ast.walk(tree), {})
    names = _clock_names(tree, aliases)
    assert any(_measures_elapsed(node, names, aliases)
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
    # The blind shapes of the independent review of task 3175 (IR75-03).
    "import-alias-clock": ("unscaled", """
from time import perf_counter as tick
def test_shape():
    start = tick()
    work()
    assert tick() - start < 0.5
"""),
    "local-import-alias-clock": ("unscaled", """
def test_shape():
    from time import process_time as cpu
    start = cpu()
    work()
    assert cpu() - start < 0.5
"""),
    "module-clock-alias": ("unscaled", """
import time
clock = time.perf_counter
def test_shape():
    start = clock()
    work()
    assert clock() - start < 0.5
"""),
    "local-clock-alias": ("unscaled", """
import time
def test_shape():
    clock = time.monotonic
    start = clock()
    work()
    assert clock() - start < 0.5
"""),
    "readings-in-a-list": ("unscaled", """
import time
def test_shape():
    stamps = []
    stamps.append(time.perf_counter())
    work()
    stamps.append(time.perf_counter())
    assert stamps[1] - stamps[0] < 0.5
"""),
    "negated-start": ("unscaled", """
import time
def test_shape():
    elapsed = -time.perf_counter()
    work()
    elapsed += time.perf_counter()
    assert elapsed < 0.5
"""),
    "datetime-now": ("unscaled", """
from datetime import datetime
def test_shape():
    start = datetime.now()
    work()
    assert (datetime.now() - start).total_seconds() < 0.5
"""),
    "datetime-utcnow-module": ("unscaled", """
import datetime
def test_shape():
    start = datetime.datetime.utcnow()
    work()
    assert (datetime.datetime.utcnow() - start).total_seconds() < 0.5
"""),
    "now-helper": ("unscaled", """
import time
def now():
    \"\"\"The current reading.\"\"\"
    return time.perf_counter()
def test_shape():
    start = now()
    work()
    assert now() - start < 0.5
"""),
    "timing-fixture": ("unscaled", """
import time
import pytest
@pytest.fixture
def within_half_a_second():
    start = time.perf_counter()
    yield
    assert time.perf_counter() - start < 0.5
def test_shape(within_half_a_second):
    work()
"""),
    "named-fixture-requesting-a-fixture": ("unscaled", """
import time
import pytest
@pytest.fixture
def started():
    return time.perf_counter()
@pytest.fixture(name="bounded")
def _bounded(started):
    yield
    assert time.perf_counter() - started < 0.5
def test_shape(bounded):
    work()
"""),
    "self-method": ("unscaled", """
import time
class TestShape:
    def _seconds(self):
        start = time.process_time()
        work()
        return time.process_time() - start
    def test_shape(self):
        assert self._seconds() < 0.5
"""),
    "base-class-method-passed-on": ("calibrated", """
import time
from tests.host_timing import assert_linear_time
class Timed:
    def seconds_at(self, size):
        start = time.process_time()
        work(size)
        return time.process_time() - start
class TestShape(Timed):
    def test_shape(self):
        assert_linear_time(self.seconds_at, 4096, 0.5)
"""),
    "discarded-growth-ratio": ("unscaled", """
import time
from tests.host_timing import growth_ratio
def test_shape():
    growth_ratio(1.0, 1.0)
    start = time.process_time()
    work()
    assert time.process_time() - start < 0.5
"""),
    "kept-but-never-compared-growth-ratio": ("unscaled", """
import time
from tests.host_timing import growth_ratio
def test_shape():
    ratio = growth_ratio(1.0, 1.0)
    start = time.process_time()
    work()
    assert time.process_time() - start < 0.5
"""),
    # Task 3185: a ratio compared by hand is not settled under contention.
    "compared-growth-ratio": ("unsettled growth", """
import time
from tests.host_timing import GROWTH_LIMIT, growth_ratio
def seconds_at(size):
    start = time.process_time()
    work(size)
    return time.process_time() - start
def test_shape():
    ratio = growth_ratio(seconds_at(1024), seconds_at(4096))
    assert ratio < GROWTH_LIMIT
"""),
    # The CI-shape test as it was before task 3185 (CI run 37447348074
    # failed it at {50: 0.067, 200: 0.137}): a budget and retries of the
    # larger size only, then growth_ratio by hand.
    "pre-3185-ci-shape": ("unsettled growth", """
import statistics
import time
from tests.host_timing import GROWTH_RETRIES, budget, growth_ratio
LIMIT = 2.0
def per_megabyte(kilobytes):
    runs = []
    for _ in range(3):
        started = time.process_time()
        scan(kilobytes)
        runs.append(time.process_time() - started)
    return statistics.median(runs) / (kilobytes / 1024)
def test_shape():
    per = {}
    for kilobytes in (50, 200, 2048):
        seconds = per_megabyte(kilobytes)
        assert seconds < budget(0.5), (kilobytes, seconds)
        smallest = per.setdefault(50, seconds)
        for _ in range(GROWTH_RETRIES):
            if growth_ratio(smallest, seconds) < LIMIT:
                break
            seconds = min(seconds, per_megabyte(kilobytes))
        per[kilobytes] = seconds
        assert growth_ratio(smallest, seconds) < LIMIT, per
"""),
    "per-unit-check": ("calibrated", """
import time
from tests.host_timing import assert_linear_per_unit
def per_megabyte(kilobytes):
    started = time.process_time()
    scan(kilobytes)
    return (time.process_time() - started) / (kilobytes / 1024)
def test_shape():
    assert_linear_per_unit(per_megabyte, (50, 200, 2048), 0.5)
"""),
    "compared-settled-growth": ("calibrated", """
import time
from tests.host_timing import GROWTH_LIMIT, settled_growth
def seconds_at(size):
    start = time.process_time()
    work(size)
    return time.process_time() - start
def test_shape():
    growth = settled_growth(seconds_at, 1024, 4096, seconds_at(1024),
                            seconds_at(4096))
    assert growth.ratio < GROWTH_LIMIT
"""),
    "discarded-settled-growth": ("unscaled", """
import time
from tests.host_timing import settled_growth
def seconds_at(size):
    start = time.process_time()
    work(size)
    return time.process_time() - start
def test_shape():
    settled_growth(seconds_at, 1024, 4096, 0.1, 0.4)
    assert seconds_at(4096) < 0.5
"""),
    "a-datetime-cutoff-is-not-timing": ("not timing", """
from datetime import datetime, timedelta
def test_shape():
    cutoff = datetime.now() - timedelta(days=1)
    assert prune(cutoff) == 0
"""),
}


@pytest.mark.parametrize("shape", sorted(PLANTED_SHAPES))
def test_the_fence_gives_each_planted_shape_its_verdict(shape):
    verdict, source = PLANTED_SHAPES[shape]
    scratch = _Module("test_scratch_shape", source)
    (test,) = [name for name in scratch.tests
               if name.rpartition(".")[2] == "test_shape"]
    reach = _reach_test({"test_scratch_shape": scratch}, scratch, test)
    assert reach.verdict == verdict


def test_conftest_fixtures_a_test_requests_are_followed():
    """IR75-03: a timing fixture in conftest.py is followed too."""
    conftest = _Module("conftest", """
import time
import pytest
@pytest.fixture
def stopwatch():
    start = time.perf_counter()
    yield
    assert time.perf_counter() - start < 0.5
""")
    scratch = _Module("test_scratch_shape", "def test_shape(stopwatch):\n    work()\n")
    modules = {"conftest": conftest, "test_scratch_shape": scratch}
    assert _reach_test(modules, scratch, "test_shape").verdict == "unscaled"


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
