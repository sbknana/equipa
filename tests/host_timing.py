"""Host-calibrated budgets and growth checks of the timing tests (tasks 3171
and 3175).

The timing tests prove that a scan reads its input in LINEAR time, not that
the machine running them is fast. Their absolute budgets were calibrated on
the development host, and a slower runner (a GitHub runner, or Python 3.10
here) went a few percent over them on work that was still linear. So every
timing test checks two things through this module:

1. **A budget calibrated under the same load.** A measurement over its base
   budget is taken again with a fixed pure-Python reference workload timed
   in the same process right before and right after it
   (``LOAD_REFERENCE_RUNS`` runs of CPU time on each side), and its budget
   is the base budget times ``max(1.0, median / REFERENCE_SECONDS)``.
   ``REFERENCE_SECONDS`` is the same workload measured on the development
   host. A factor measured once at session start missed the contention
   between xdist workers on a small runner (task 3175: a linear 0.51 s
   against a 0.5 s budget on CI); one measured around the work sees the
   load the work saw. A measurement within its base budget passes at any
   factor, so the reference does not run for it: a test timing thousands
   of fast shapes ran the reference around each one for 25 minutes.
   ``budget(seconds)`` serves tests that time their own work: it scales by
   a factor measured in this process within the last
   ``FACTOR_REUSE_SECONDS``, or measures one right then. A faster host
   never tightens a budget, and no base budget is ever raised.

2. **A cap on the factor.** A measured factor over ``MAX_HOST_FACTOR``
   fails the measurement (``HostTooSlowError``) instead of loosening its
   budget further. Growth (below) only sees superlinear work, so a budget
   loosened N times hides a linear slowdown of up to N times; past 4x, a
   slow or overloaded host must say so loudly rather than pass. A forced
   factor over the cap is refused (``HostFactorError``).

3. **Growth.** ``assert_linear_time`` times the same shape at the test's
   size and at a quarter of it: linear work grows about 4x, quadratic work
   about 16x, and the ratio must stay below ``GROWTH_LIMIT``. That check
   does not depend on the host's speed, so a quadratic regression fails on
   a slow runner even when the host factor has loosened its budget. A
   ratio of two sub-millisecond times is noise (task 3175: 0.0006 s against
   0.0052 s), so while the quarter size takes under
   ``GROWTH_FLOOR_SECONDS`` the INPUT grows, as ``scan_growth`` in
   tests/test_review_gate_linear_3167.py does: the larger size becomes the
   quarter size and the next larger size is 4 times it, until the quarter
   size's reading reaches the floor (each step measures one size). A reading
   is never raised to hide work (IR75-01: raising 2 ms readings to a 20 ms
   floor at unchanged sizes let quadratic work of 16-159 ms pass). The
   input grows, whatever building the shape costs, while the larger
   reading is at least ``GROWTH_DETECTION_SECONDS`` (16 ms: the quadratic
   work main's 2 ms floor caught), up to ``MAX_INPUT_GROWTH`` times the
   test's sizes. A smaller larger reading is decided against the floor at
   the test's sizes (quadratic work that small passed main's floor too),
   so a test timing thousands of fast shapes does not grow each one.
   Grown sizes are held to the growth limit, not to the budget (it holds
   at the test's sizes). Each timed call (and each reference run) runs
   with the garbage collector paused, as ``timeit`` does: a full collection
   walks the whole heap a long xdist worker has built up, so it paused the
   larger size far more than its input could (task 3175: 0.0129 s against
   0.1341 s for a linear scan).

Set ``EQUIPA_TIMING_HOST_FACTOR`` (a finite number > 0, at most
``MAX_HOST_FACTOR``) to force the factor, for example
``EQUIPA_TIMING_HOST_FACTOR=2.0`` to simulate a runner twice as slow as the
development host. It replaces every measurement of the reference workload,
so a value below 1.0 tightens every budget.

Every check raises ``TimingCheckFailed`` explicitly: pytest does not
rewrite this module's ``assert`` statements, and ``python -O`` (or
``PYTHONOPTIMIZE``) strips them, which disabled every timing check.

Deadline tests are not timed here: a bound that tells "returned at once"
from "waited out a fixed sleep or timeout" (the agent launcher grace, a
generator timeout, a hanging ``find``) does not grow with the machine's
speed, and scaling it could carry it past the timeout it exists to tell
apart. ``tests/test_host_timing_3171.py`` lists them. A measurement that
never ends is cut by the per-test deadline (``tests/deadline_watchdog.py``).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import contextlib
import functools
import gc
import math
import os
import statistics
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Mapping, TypeVar

HOST_FACTOR_ENVIRONMENT_VARIABLE = "EQUIPA_TIMING_HOST_FACTOR"

# The median CPU time of ``reference_workload()`` on the development host:
# the lowest of 15 medians on Python 3.12 (they spanned 0.0283-0.0299 s; a
# value taken under load would tighten every other host). Python 3.10 took
# 0.0310-0.0313 s on the same host, so its factor is about 1.1.
REFERENCE_SECONDS = 0.0283
# Runs of the session-start calibration printed in the pytest header.
REFERENCE_RUNS = 5
REFERENCE_ITERATIONS = 40_000
# Runs of the reference workload on each side of one measurement; the
# median of both sides' runs gives that measurement's factor.
LOAD_REFERENCE_RUNS = 2
# ``budget()`` reuses a factor this process measured within this many
# seconds (wall clock), so a scan asking per pattern does not rerun the
# reference workload each time.
FACTOR_REUSE_SECONDS = 1.0
# Past this factor a budget would hide a linear slowdown as large: the
# measurement fails instead (see the module docstring).
MAX_HOST_FACTOR = 4.0

# Linear work grows 4x for 4x the input, quadratic work 16x.
GROWTH = 4
GROWTH_LIMIT = 8.0
# Below this a measurement is mostly clock, cache and scheduling noise: the
# input grows until the quarter size's reading reaches it. A reading still
# under it once the input stops growing is raised to it.
GROWTH_FLOOR_SECONDS = 0.02
# Main's floor before task 3175: quadratic work whose larger reading was
# GROWTH_LIMIT times it failed there. A larger reading of at least that
# makes the input grow whatever the calls cost, so it fails here too.
DETECTION_FLOOR_SECONDS = 0.002
GROWTH_DETECTION_SECONDS = GROWTH_LIMIT * DETECTION_FLOOR_SECONDS
# The input grows to at most this many times the test's sizes (16x a 1 MB
# shape is 16 MB). Quadratic work reading 16 ms at the test's larger size
# reads 256 ms at 4 times it, over the limit even against the floor;
# linear work reading 16 ms there takes the floor at 16 times.
MAX_INPUT_GROWTH = 16
# A ratio over the limit is measured again (keeping the fastest total of
# each size) before it counts: one descheduled run must not fail a test,
# a quadratic one fails every time.
GROWTH_RETRIES = 2

T = TypeVar("T")


class HostFactorError(ValueError):
    """``EQUIPA_TIMING_HOST_FACTOR`` does not hold a finite number > 0 and
    at most ``MAX_HOST_FACTOR``."""


class InputTooLarge(Exception):
    """Raised by a ``seconds_at`` asked for a size its shape does not exist
    at: the code under test refuses it (the merge gate reads no review
    artifact over ``MAX_REVIEW_ARTIFACT_BYTES``) or it cannot be built. The
    input then stops growing, and the growth is decided at the sizes
    measured against ``DETECTION_FLOOR_SECONDS`` (task 3178)."""


class TimingCheckFailed(AssertionError):
    """A timing check failed. Raised explicitly, never by ``assert``, so
    ``python -O`` cannot strip it."""


class HostTooSlowError(TimingCheckFailed):
    """The factor measured around a measurement is over ``MAX_HOST_FACTOR``."""


@contextlib.contextmanager
def collector_paused() -> Iterator[None]:
    """Keep the cyclic garbage collector from running inside a timed call,
    as ``timeit`` does. A full collection walks the whole heap of the
    process, so its pause depends on what earlier tests left alive, not on
    the timed input: in a worker holding 6 million objects one took 0.12 s
    inside a 0.05 s scan, and the larger size (allocating 4x as much)
    triggers more of them (task 3175: a linear scan read 0.0129 s against
    0.1341 s). The collector's previous state is restored afterwards."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _collector_paused_calls(seconds_at: Callable[[int], T]
                            ) -> Callable[[int], T]:
    """``seconds_at`` with the collector paused around each call."""
    @functools.wraps(seconds_at)
    def paused(size: int) -> T:
        with collector_paused():
            return seconds_at(size)
    return paused


def reference_workload(iterations: int = REFERENCE_ITERATIONS) -> int:
    """Fixed pure-Python work of the kinds the timed code does: calls,
    integer arithmetic, string building and slicing, dict and list use."""
    counts: dict[str, int] = {}
    pieces: list[str] = []
    checksum = 0
    for value in range(iterations):
        word = f"w{value % 97}"
        counts[word] = counts.get(word, 0) + 1
        pieces.append(word[::-1])
        checksum = (checksum * 31 + len(word) + value) % 1_000_003
    joined = "".join(pieces)
    return checksum + len(joined) + len(counts)


def reference_samples(runs: int) -> list[float]:
    """The CPU time of each of ``runs`` calls of ``reference_workload``,
    with the collector paused as it is around a timed call."""
    times = []
    for _ in range(runs):
        with collector_paused():
            started = time.process_time()
            reference_workload()
            times.append(time.process_time() - started)
    return times


def measure_reference_seconds(runs: int = REFERENCE_RUNS) -> float:
    """The median CPU time of ``runs`` calls of ``reference_workload``."""
    return statistics.median(reference_samples(runs))


def forced_host_factor() -> float | None:
    """The factor ``EQUIPA_TIMING_HOST_FACTOR`` forces, or None if unset."""
    raw = os.environ.get(HOST_FACTOR_ENVIRONMENT_VARIABLE)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise HostFactorError(
            f"{HOST_FACTOR_ENVIRONMENT_VARIABLE}={raw!r} is not a number"
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise HostFactorError(
            f"{HOST_FACTOR_ENVIRONMENT_VARIABLE}={raw!r} must be a finite "
            f"number > 0")
    if value > MAX_HOST_FACTOR:
        raise HostFactorError(
            f"{HOST_FACTOR_ENVIRONMENT_VARIABLE}={raw!r} is over the cap "
            f"{MAX_HOST_FACTOR}: a budget loosened that far would hide a "
            f"linear slowdown as large (tests/host_timing.py)")
    return value


def factor_from_reference(seconds: float) -> float:
    """The host factor of a reference workload that took ``seconds``:
    ``max(1.0, seconds / REFERENCE_SECONDS)``, refused over the cap."""
    factor = max(1.0, seconds / REFERENCE_SECONDS)
    if factor > MAX_HOST_FACTOR:
        raise HostTooSlowError(
            f"the timing reference workload took {seconds:.4f} s here, "
            f"{factor:.2f}x the {REFERENCE_SECONDS:.4f} s of the development "
            f"host and over the cap of {MAX_HOST_FACTOR}x: a budget loosened "
            f"that far would hide a linear slowdown as large, so this host "
            f"(or its load) is too slow to run the timing tests "
            f"(tests/host_timing.py)")
    return factor


@dataclass(frozen=True)
class HostCalibration:
    """The session-start calibration printed in the pytest header, and the
    forced factor when ``EQUIPA_TIMING_HOST_FACTOR`` sets one."""

    factor: float
    measured_seconds: float | None
    forced: bool

    def describe(self) -> str:
        if self.forced:
            return (f"timing host factor {self.factor:.2f} (forced by "
                    f"{HOST_FACTOR_ENVIRONMENT_VARIABLE})")
        text = (f"timing host factor {self.factor:.2f} at session start "
                f"(reference workload {self.measured_seconds:.4f} s here, "
                f"{REFERENCE_SECONDS:.4f} s on the development host); a "
                f"measurement over its base budget is scaled by the factor "
                f"measured around it, capped at {MAX_HOST_FACTOR}")
        if self.factor > MAX_HOST_FACTOR:
            text += "; OVER THE CAP: timing tests will fail on this host"
        return text


@functools.lru_cache(maxsize=None)
def host_calibration() -> HostCalibration:
    """Measured once per process (conftest calls it at session start). The
    measured factor is not capped here: a slow host must still be able to
    run the rest of the suite, and its timing tests fail on their own."""
    forced = forced_host_factor()
    if forced is not None:
        return HostCalibration(factor=forced, measured_seconds=None,
                               forced=True)
    measured = measure_reference_seconds()
    return HostCalibration(factor=max(1.0, measured / REFERENCE_SECONDS),
                           measured_seconds=measured, forced=False)


# (time.monotonic() when measured, factor) of this process's last
# measurement of the load, reused by ``host_factor`` for a short while.
_last_load_factor: tuple[float, float] | None = None


def _remember_load_factor(factor: float) -> None:
    global _last_load_factor
    _last_load_factor = (time.monotonic(), factor)


def measure_under_load(measure: Callable[[], T],
                       base_budget_seconds: float | None = None,
                       slowest: Callable[[T], float] = float,
                       ) -> tuple[T, float]:
    """``measure()`` and the host factor of the load it ran under: the
    reference workload timed right before and right after it in this
    process, median of both sides. The forced factor if one is set.

    With ``base_budget_seconds``, the reference only runs when it can
    change the verdict. A measurement whose ``slowest(result)`` is within
    the base budget passes at any factor (never below 1.0), so it is
    returned at factor 1.0 unmeasured: a test timing thousands of
    sub-millisecond shapes must not pay for the reference each time. One
    over it is measured again between two readings of the reference, and
    its budget is scaled by that factor. One at ``MAX_HOST_FACTOR`` times
    the base budget or more cannot pass at any factor up to the cap, so it
    is not measured again (a quadratic regression must not run twice); the
    reference runs right after it."""
    calibration = host_calibration()
    if calibration.forced:
        return measure(), calibration.factor
    if base_budget_seconds is not None:
        result = measure()
        seconds = slowest(result)
        if seconds < base_budget_seconds:
            return result, 1.0
        if seconds >= base_budget_seconds * MAX_HOST_FACTOR:
            factor = factor_from_reference(
                measure_reference_seconds(2 * LOAD_REFERENCE_RUNS))
            _remember_load_factor(factor)
            return result, factor
    before = reference_samples(LOAD_REFERENCE_RUNS)
    result = measure()
    after = reference_samples(LOAD_REFERENCE_RUNS)
    factor = factor_from_reference(statistics.median(before + after))
    _remember_load_factor(factor)
    return result, factor


def host_factor() -> float:
    """The factor of a budget for work timed now: the forced factor, or the
    load this process measured within the last ``FACTOR_REUSE_SECONDS``
    (measured now, ``2 * LOAD_REFERENCE_RUNS`` runs, when there is none)."""
    calibration = host_calibration()
    if calibration.forced:
        return calibration.factor
    if (_last_load_factor is not None
            and time.monotonic() - _last_load_factor[0] <= FACTOR_REUSE_SECONDS):
        return _last_load_factor[1]
    factor = factor_from_reference(
        measure_reference_seconds(2 * LOAD_REFERENCE_RUNS))
    _remember_load_factor(factor)
    return factor


def budget(seconds: float) -> float:
    """A base budget (calibrated on the development host) for this host
    under its current load."""
    return seconds * host_factor()


def growth_ratio(small_seconds: float, seconds: float,
                 floor_seconds: float = GROWTH_FLOOR_SECONDS) -> float:
    """How much longer the larger size took than the smaller one, the
    smaller time raised to ``floor_seconds`` first. A scan over many
    shapes compares this with ``GROWTH_LIMIT`` itself rather than asserting
    per shape through ``assert_linear_time``; it must grow its input until
    the smaller time reaches ``GROWTH_FLOOR_SECONDS`` (``grows_further``),
    or the raised floor hides quadratic work (IR75-01)."""
    return seconds / max(small_seconds, floor_seconds)


def _grown_further(grown: _GrownReadings, size: int) -> bool:
    """Measure ``size``, the next larger size of a growth check; False
    when the shape does not exist at it (``InputTooLarge``)."""
    try:
        grown.at(size)
    except InputTooLarge:
        return False
    return True


def grows_further(small_seconds: float, seconds: float,
                  input_growth: int) -> bool:
    """Whether the input grows another ``GROWTH`` times: the larger size
    becomes the quarter size, and the size 4 times it is measured.

    ``small_seconds`` and ``seconds`` are the readings at ``input_growth``
    times the test's sizes. A quarter reading at the floor is decided as
    read, and so is a ratio over the limit even against the floor (the
    retries decide it). Under the floor the input grows, to at most
    ``MAX_INPUT_GROWTH`` times the test's sizes and whatever the calls
    cost, while the larger reading is at least ``GROWTH_DETECTION_SECONDS``:
    it could be the quadratic work main's 2 ms floor caught (IR75-01). A
    smaller larger reading is decided against the floor at once: quadratic
    work that small passed main's floor too, and a test timing thousands
    of fast shapes must not pay for growing each one."""
    if small_seconds >= GROWTH_FLOOR_SECONDS:
        return False
    if growth_ratio(small_seconds, seconds) >= GROWTH_LIMIT:
        return False
    if input_growth * GROWTH > MAX_INPUT_GROWTH:
        return False
    return seconds >= GROWTH_DETECTION_SECONDS


class _GrownReadings:
    """The readings of one growth check by size: the ladder (N/4, N; N,
    4N; 4N, 16N) reuses each larger reading as the next quarter's."""

    def __init__(self, measure: Callable[[int], T]) -> None:
        self._measure = measure
        self.readings: dict[int, T] = {}

    def at(self, size: int) -> T:
        if size not in self.readings:
            self.readings[size] = self._measure(size)
        return self.readings[size]


def fail(message: str) -> None:
    """Fail a timing check (never an ``assert``: see the module docstring)."""
    raise TimingCheckFailed(message)


@dataclass(frozen=True)
class LinearTiming:
    """The two measurements the growth of one ``assert_linear_time`` call
    was decided on: the seconds of one call at each size (``input_growth``
    times the test's sizes), and the budget and host factor of the test's
    larger size (factor 1.0, unmeasured, when it was within its base
    budget)."""

    label: str
    small_size: int
    small_seconds: float
    size: int
    seconds: float
    budget_seconds: float
    factor: float = 1.0
    input_growth: int = 1
    # ``DETECTION_FLOOR_SECONDS`` when the shape did not exist at the next
    # larger size (``InputTooLarge``).
    floor_seconds: float = GROWTH_FLOOR_SECONDS

    @property
    def ratio(self) -> float:
        return growth_ratio(self.small_seconds, self.seconds, self.floor_seconds)

    def describe(self) -> str:
        return (f"{self.label}: {self.seconds:.4f} s at size {self.size}, "
                f"{self.small_seconds:.4f} s at size {self.small_size} "
                f"(growth {self.ratio:.1f}x at {self.input_growth}x the "
                f"test's input, floor {self.floor_seconds:g} s, limit "
                f"{GROWTH_LIMIT}x; budget {self.budget_seconds:.4f} s at host "
                f"factor {self.factor:.2f})")


def _check_budget(label: str, seconds: float, size: int,
                  base_budget_seconds: float, factor: float) -> None:
    budget_seconds = base_budget_seconds * factor
    if not seconds < budget_seconds:
        fail(f"{label}: {seconds:.4f} s at size {size}, over the budget "
             f"{budget_seconds:.4f} s at host factor {factor:.2f}")


def assert_linear_time(seconds_at: Callable[[int], float], size: int,
                       base_budget_seconds: float, label: str = "", *,
                       compare_with_larger: bool = False) -> LinearTiming:
    """Assert that the work ``seconds_at(size)`` times stays within its
    host-calibrated budget and grows linearly.

    ``seconds_at(n)`` builds the test's shape at size ``n`` and returns the
    seconds the work took, measured the way the test measures it (CPU time,
    a median, a best of several). It must not be answered from a cache.

    The growth is measured from a quarter of ``size`` to ``size``, both held
    to the budget at the factor measured around each. A shape that only
    exists from ``size`` on (more distinct characters than a table keeps,
    which a quarter of the text cannot hold) passes ``compare_with_larger``:
    the growth is then measured from ``size`` to four times it, and the
    budget holds at ``size``.

    The smaller size runs first, so a quadratic regression fails on its
    budget before the larger size takes seconds; any time series that grows
    with the input then fails at the larger size too. While the smaller
    reading is under ``GROWTH_FLOOR_SECONDS``, the input grows 4x
    (``grows_further``); the grown sizes are held to the growth limit only."""
    if size < GROWTH:
        raise ValueError(f"size {size} has no quarter to compare against")
    seconds_at = _collector_paused_calls(seconds_at)
    if compare_with_larger:
        small_size, large_size = size, size * GROWTH
    else:
        small_size, large_size = size // GROWTH, size
    grown = _GrownReadings(seconds_at)
    small_seconds, small_factor = measure_under_load(
        lambda: grown.at(small_size), base_budget_seconds)
    _check_budget(label, small_seconds, small_size, base_budget_seconds,
                  small_factor)
    if compare_with_larger:
        # Past ``size`` only the growth counts: no budget, no reference.
        seconds, factor = grown.at(large_size), small_factor
    else:
        seconds, factor = measure_under_load(lambda: grown.at(large_size),
                                             base_budget_seconds)
        _check_budget(label, seconds, large_size, base_budget_seconds, factor)
    input_growth, floor_seconds = 1, GROWTH_FLOOR_SECONDS
    while grows_further(grown.at(small_size), grown.at(large_size),
                        input_growth):
        if not _grown_further(grown, large_size * GROWTH):
            # The shape does not exist at the next size (a 200 KB review
            # grown 16x passes the gate's 2 MB artifact cap): decided here
            # with main's floor, under which a larger reading of 16 ms
            # that grew 16x fails (IR75-01 without growing).
            floor_seconds = DETECTION_FLOOR_SECONDS
            break
        input_growth *= GROWTH
        small_size, large_size = large_size, large_size * GROWTH
    timing = LinearTiming(label, small_size, grown.at(small_size),
                          large_size, grown.at(large_size),
                          base_budget_seconds * factor, factor, input_growth,
                          floor_seconds)
    for _ in range(GROWTH_RETRIES):
        if timing.ratio < GROWTH_LIMIT:
            break
        timing = replace(
            timing,
            small_seconds=min(timing.small_seconds, seconds_at(small_size)),
            seconds=min(timing.seconds, seconds_at(large_size)))
    if not timing.ratio < GROWTH_LIMIT:
        fail(f"superlinear growth: {timing.describe()}")
    return timing


def assert_linear_times(seconds_at: Callable[[int], Mapping[str, float]],
                        size: int, base_budget_seconds: float,
                        label: str = "") -> dict[str, LinearTiming]:
    """``assert_linear_time`` for work timed in several parts by one
    measurement (every pattern of a scan, timed by one probe run):
    ``seconds_at(n)`` returns the seconds of each part at size ``n``, and
    every part is held to the budget and to the growth limit. While any
    part's smaller reading is under the floor and ``grows_further`` says
    so, the input grows 4x for every part (one call times them all); the
    parts are decided at the sizes the growth stopped at. A part over the
    growth limit is measured again (all parts, keeping each part's fastest
    time) before it counts."""
    if size < GROWTH:
        raise ValueError(f"size {size} has no quarter to compare against")
    small_size = size // GROWTH

    def check_budget(times: Mapping[str, float], at_size: int,
                     factor: float) -> None:
        budget_seconds = base_budget_seconds * factor
        over = {part: round(seconds, 4) for part, seconds in times.items()
                if not seconds < budget_seconds}
        if over:
            fail(f"{label}: over the budget {budget_seconds:.4f} s at host "
                 f"factor {factor:.2f} at size {at_size}: {over}")

    def slowest_part(times: Mapping[str, float]) -> float:
        return max(times.values(), default=0.0)

    def check_parts(times: Mapping[str, float], expected: Mapping[str, float],
                    at_size: int) -> None:
        if set(times) != set(expected):
            fail(f"{label}: the parts differ between the sizes (at size "
                 f"{at_size}): {sorted(set(times) ^ set(expected))}")

    grown = _GrownReadings(lambda at_size: dict(seconds_at(at_size)))
    small, small_factor = measure_under_load(
        lambda: grown.at(small_size), base_budget_seconds, slowest_part)
    check_budget(small, small_size, small_factor)
    large, factor = measure_under_load(
        lambda: grown.at(size), base_budget_seconds, slowest_part)
    check_budget(large, size, factor)
    check_parts(small, large, small_size)
    input_growth, large_size = 1, size

    def any_part_grows() -> bool:
        small_times = grown.at(small_size)
        large_times = grown.at(large_size)
        check_parts(small_times, large, small_size)
        check_parts(large_times, large, large_size)
        return any(grows_further(small_times[part], large_times[part],
                                 input_growth) for part in large)

    floor_seconds = GROWTH_FLOOR_SECONDS
    while any_part_grows():
        if not _grown_further(grown, large_size * GROWTH):
            # As in assert_linear_time: no larger shape, main's floor.
            floor_seconds = DETECTION_FLOOR_SECONDS
            break
        input_growth *= GROWTH
        small_size, large_size = large_size, large_size * GROWTH
    small, large = dict(grown.at(small_size)), dict(grown.at(large_size))
    for _ in range(GROWTH_RETRIES):
        if all(growth_ratio(small[part], large[part], floor_seconds) < GROWTH_LIMIT
               for part in large):
            break
        for part, seconds in seconds_at(small_size).items():
            small[part] = min(small[part], seconds)
        for part, seconds in seconds_at(large_size).items():
            large[part] = min(large[part], seconds)
    timings = {part: LinearTiming(f"{label}: {part}", small_size, small[part],
                                  large_size, large[part],
                                  base_budget_seconds * factor, factor,
                                  input_growth, floor_seconds)
               for part in large}
    superlinear = [timing.describe() for timing in timings.values()
                   if not timing.ratio < GROWTH_LIMIT]
    if superlinear:
        fail(f"superlinear growth: {superlinear}")
    return timings
