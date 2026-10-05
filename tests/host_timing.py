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
   0.0052 s), so the smaller time is raised to ``GROWTH_FLOOR_SECONDS``
   before the ratio is taken. When the quarter size takes under the floor
   and the larger size's reading could still reach the limit, both sizes
   are measured again, the same number of times, until the quarter size's
   total reaches the floor (at most ``MAX_GROWTH_REPETITIONS`` times, and
   within ``GROWTH_REPETITION_SECONDS`` of wall time for the whole calls,
   building the shape included); the totals are compared, the smaller
   raised to the floor. Linear work under the floor is not repeated: it
   cannot reach the limit.

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

import functools
import math
import os
import statistics
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, TypeVar

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
# Below this a measurement is mostly clock, cache and scheduling noise. The
# quarter size is measured again until its total reaches it, and the
# smaller total is raised to it before the ratio is taken.
GROWTH_FLOOR_SECONDS = 0.02
MAX_GROWTH_REPETITIONS = 32
# The wall time the extra measurements of both sizes may take in all. A
# ``seconds_at`` that builds a 200 KB review and writes an artifact around
# a 10 ms measurement repeats less (or not at all: the floor then holds).
GROWTH_REPETITION_SECONDS = 0.5
# A ratio over the limit is measured again (keeping the fastest total of
# each size) before it counts: one descheduled run must not fail a test,
# a quadratic one fails every time.
GROWTH_RETRIES = 2

T = TypeVar("T")


class HostFactorError(ValueError):
    """``EQUIPA_TIMING_HOST_FACTOR`` does not hold a finite number > 0 and
    at most ``MAX_HOST_FACTOR``."""


class TimingCheckFailed(AssertionError):
    """A timing check failed. Raised explicitly, never by ``assert``, so
    ``python -O`` cannot strip it."""


class HostTooSlowError(TimingCheckFailed):
    """The factor measured around a measurement is over ``MAX_HOST_FACTOR``."""


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
    """The CPU time of each of ``runs`` calls of ``reference_workload``."""
    times = []
    for _ in range(runs):
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


def growth_ratio(small_seconds: float, seconds: float) -> float:
    """How much longer the larger size took than the smaller one, the
    smaller time raised to ``GROWTH_FLOOR_SECONDS`` first. A scan over many
    shapes compares this with ``GROWTH_LIMIT`` itself rather than asserting
    per shape through ``assert_linear_time``; it must pass times of at
    least ``GROWTH_FLOOR_SECONDS``."""
    return seconds / max(small_seconds, GROWTH_FLOOR_SECONDS)


def growth_repetitions(small_seconds: float,
                       pair_cost_seconds: float = 0.0,
                       seconds: float | None = None) -> int:
    """How many times to measure each size so that the quarter size's total
    reaches ``GROWTH_FLOOR_SECONDS``: at most ``MAX_GROWTH_REPETITIONS``,
    and no more extra pairs than ``GROWTH_REPETITION_SECONDS`` of wall time
    pays for when one call of each size took ``pair_cost_seconds``.

    ``seconds`` is the larger size's first reading. When that many
    repetitions of it would total under ``GROWTH_LIMIT`` floors, the
    smaller total is raised to the floor and the ratio cannot reach the
    limit, so the sizes are measured once: linear work under the floor
    (about 4 floors in all) is never repeated, a suspicious reading is."""
    if small_seconds >= GROWTH_FLOOR_SECONDS:
        return 1
    needed = math.ceil(GROWTH_FLOOR_SECONDS / max(small_seconds, 1e-6))
    affordable = MAX_GROWTH_REPETITIONS
    if pair_cost_seconds > 0:
        affordable = 1 + int(GROWTH_REPETITION_SECONDS / pair_cost_seconds)
    repetitions = max(1, min(MAX_GROWTH_REPETITIONS, needed, affordable))
    if (seconds is not None
            and repetitions * seconds < GROWTH_LIMIT * GROWTH_FLOOR_SECONDS):
        return 1
    return repetitions


def fail(message: str) -> None:
    """Fail a timing check (never an ``assert``: see the module docstring)."""
    raise TimingCheckFailed(message)


@dataclass(frozen=True)
class LinearTiming:
    """The two measurements of one ``assert_linear_time`` call: the seconds
    of one call at each size (a mean over ``repetitions`` calls), and the
    budget and host factor of the larger size's first measurement (factor
    1.0, unmeasured, when it was within its base budget)."""

    label: str
    small_size: int
    small_seconds: float
    size: int
    seconds: float
    budget_seconds: float
    factor: float = 1.0
    repetitions: int = 1

    @property
    def ratio(self) -> float:
        return growth_ratio(self.small_seconds * self.repetitions,
                            self.seconds * self.repetitions)

    def describe(self) -> str:
        return (f"{self.label}: {self.seconds:.4f} s at size {self.size}, "
                f"{self.small_seconds:.4f} s at size {self.small_size} "
                f"(growth {self.ratio:.1f}x over {self.repetitions} run(s) "
                f"of each, limit {GROWTH_LIMIT}x; budget "
                f"{self.budget_seconds:.4f} s at host factor "
                f"{self.factor:.2f})")


def _check_budget(label: str, seconds: float, size: int,
                  base_budget_seconds: float, factor: float) -> None:
    budget_seconds = base_budget_seconds * factor
    if not seconds < budget_seconds:
        fail(f"{label}: {seconds:.4f} s at size {size}, over the budget "
             f"{budget_seconds:.4f} s at host factor {factor:.2f}")


def _mean_seconds(seconds_at: Callable[[int], float], size: int,
                  repetitions: int, first: float | None = None) -> float:
    """The mean of ``repetitions`` measurements at ``size``, ``first``
    (when given) counting as one of them."""
    total = 0.0 if first is None else first
    for _ in range(repetitions - (0 if first is None else 1)):
        total += seconds_at(size)
    return total / repetitions


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
    with the input then fails at the larger size too."""
    if size < GROWTH:
        raise ValueError(f"size {size} has no quarter to compare against")
    if compare_with_larger:
        small_size, large_size = size, size * GROWTH
    else:
        small_size, large_size = size // GROWTH, size
    call_costs: dict[int, float] = {}

    def costed(at_size: int) -> float:
        """``seconds_at(at_size)``, keeping the wall time of the whole call
        (building the shape included): what one more repetition costs."""
        started = time.monotonic()
        seconds_taken = seconds_at(at_size)
        call_costs[at_size] = time.monotonic() - started
        return seconds_taken

    small_seconds, small_factor = measure_under_load(
        lambda: costed(small_size), base_budget_seconds)
    _check_budget(label, small_seconds, small_size, base_budget_seconds,
                  small_factor)
    if compare_with_larger:
        # Past ``size`` only the growth counts: no budget, no reference.
        seconds, factor = costed(large_size), small_factor
    else:
        seconds, factor = measure_under_load(lambda: costed(large_size),
                                             base_budget_seconds)
        _check_budget(label, seconds, large_size, base_budget_seconds, factor)
    repetitions = growth_repetitions(small_seconds, sum(call_costs.values()),
                                     seconds)
    timing = LinearTiming(
        label, small_size,
        _mean_seconds(seconds_at, small_size, repetitions, small_seconds),
        large_size, _mean_seconds(seconds_at, large_size, repetitions, seconds),
        base_budget_seconds * factor, factor, repetitions)
    for _ in range(GROWTH_RETRIES):
        if timing.ratio < GROWTH_LIMIT:
            break
        timing = replace(
            timing,
            small_seconds=min(timing.small_seconds, _mean_seconds(
                seconds_at, small_size, repetitions)),
            seconds=min(timing.seconds, _mean_seconds(
                seconds_at, large_size, repetitions)))
    if not timing.ratio < GROWTH_LIMIT:
        fail(f"superlinear growth: {timing.describe()}")
    return timing


def assert_linear_times(seconds_at: Callable[[int], Mapping[str, float]],
                        size: int, base_budget_seconds: float,
                        label: str = "") -> dict[str, LinearTiming]:
    """``assert_linear_time`` for work timed in several parts by one
    measurement (every pattern of a scan, timed by one probe run):
    ``seconds_at(n)`` returns the seconds of each part at size ``n``, and
    every part is held to the budget and to the growth limit. A part over
    the growth limit is measured again (all parts, keeping each part's
    fastest time) before it counts. One call is a child process run, so a
    part is not repeated below the floor: its time is raised to it."""
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

    small, small_factor = measure_under_load(
        lambda: dict(seconds_at(small_size)), base_budget_seconds,
        slowest_part)
    check_budget(small, small_size, small_factor)
    large, factor = measure_under_load(
        lambda: dict(seconds_at(size)), base_budget_seconds, slowest_part)
    check_budget(large, size, factor)
    if set(small) != set(large):
        fail(f"{label}: the parts differ between the sizes: "
             f"{sorted(set(small) ^ set(large))}")
    for _ in range(GROWTH_RETRIES):
        if all(growth_ratio(small[part], large[part]) < GROWTH_LIMIT
               for part in large):
            break
        for part, seconds in seconds_at(small_size).items():
            small[part] = min(small[part], seconds)
        for part, seconds in seconds_at(size).items():
            large[part] = min(large[part], seconds)
    timings = {part: LinearTiming(f"{label}: {part}", small_size, small[part],
                                  size, large[part],
                                  base_budget_seconds * factor, factor)
               for part in large}
    superlinear = [timing.describe() for timing in timings.values()
                   if not timing.ratio < GROWTH_LIMIT]
    if superlinear:
        fail(f"superlinear growth: {superlinear}")
    return timings
