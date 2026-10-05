"""Host-calibrated budgets and growth checks of the timing tests (task 3171).

The timing tests prove that a scan reads its input in LINEAR time, not that
the machine running them is fast. Their absolute budgets were calibrated on
the development host, and a slower runner (a GitHub runner, or Python 3.10
here) went a few percent over them on work that was still linear. So every
timing test checks two things through this module:

1. **A host-calibrated budget.** At session start (``tests/conftest.py``)
   each process times a fixed pure-Python reference workload (median of
   ``REFERENCE_RUNS`` runs of CPU time) and derives
   ``host_factor() = max(1.0, measured / REFERENCE_SECONDS)``.
   ``REFERENCE_SECONDS`` is the same workload measured on the development
   host. ``budget(seconds)`` multiplies a base budget by that factor; a
   faster host never tightens a budget. No base budget is ever raised.

2. **Growth.** ``assert_linear_time`` times the same shape at the test's
   size and at a quarter of it: linear work grows about 4x, quadratic work
   about 16x, and the ratio must stay below ``GROWTH_LIMIT``. That check
   does not depend on the host's speed, so a quadratic regression fails on
   a slow runner even when the host factor has loosened its budget.

Set ``EQUIPA_TIMING_HOST_FACTOR`` (a finite number > 0) to force the factor,
for example ``EQUIPA_TIMING_HOST_FACTOR=2.0`` to simulate a runner twice as
slow as the development host. It replaces the measurement, so a value below
1.0 tightens every budget.

Deadline tests are not timed here: a bound that tells "returned at once"
from "waited out a fixed sleep or timeout" (the agent launcher grace, a
generator timeout, a hanging ``find``) does not grow with the machine's
speed, and scaling it could carry it past the timeout it exists to tell
apart. ``tests/test_host_timing_3171.py`` lists them.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import functools
import math
import os
import statistics
import time
from dataclasses import dataclass
from typing import Callable

HOST_FACTOR_ENVIRONMENT_VARIABLE = "EQUIPA_TIMING_HOST_FACTOR"

# The median CPU time of ``reference_workload()`` on the development host:
# the lowest of 15 medians on Python 3.12 (they spanned 0.0283-0.0299 s; a
# value taken under load would tighten every other host). Python 3.10 took
# 0.0310-0.0313 s on the same host, so its factor is about 1.1.
REFERENCE_SECONDS = 0.0283
REFERENCE_RUNS = 5
REFERENCE_ITERATIONS = 40_000

# Linear work grows 4x for 4x the input, quadratic work 16x.
GROWTH = 4
GROWTH_LIMIT = 8.0
# Below this a measurement is mostly clock and cache noise: the quarter-size
# time is raised to it before the ratio is taken, so work that finishes in
# under GROWTH_LIMIT * GROWTH_FLOOR_SECONDS at the test's size always passes
# the growth check (it is still held to its budget).
GROWTH_FLOOR_SECONDS = 0.002
# A ratio over the limit is measured again (keeping the fastest time of
# each size) before it counts: one descheduled run must not fail a test,
# a quadratic one fails every time.
GROWTH_RETRIES = 2


class HostFactorError(ValueError):
    """``EQUIPA_TIMING_HOST_FACTOR`` does not hold a finite number > 0."""


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


def measure_reference_seconds(runs: int = REFERENCE_RUNS) -> float:
    """The median CPU time of ``runs`` calls of ``reference_workload``."""
    times = []
    for _ in range(runs):
        started = time.process_time()
        reference_workload()
        times.append(time.process_time() - started)
    return statistics.median(times)


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
    return value


@dataclass(frozen=True)
class HostCalibration:
    """What the session's budgets are multiplied by, and why."""

    factor: float
    measured_seconds: float | None
    forced: bool

    def describe(self) -> str:
        if self.forced:
            return (f"timing host factor {self.factor:.2f} (forced by "
                    f"{HOST_FACTOR_ENVIRONMENT_VARIABLE})")
        return (f"timing host factor {self.factor:.2f} (reference workload "
                f"{self.measured_seconds:.4f} s here, {REFERENCE_SECONDS:.4f}"
                f" s on the development host)")


@functools.lru_cache(maxsize=None)
def host_calibration() -> HostCalibration:
    """Measured once per process (conftest calls it at session start)."""
    forced = forced_host_factor()
    if forced is not None:
        return HostCalibration(factor=forced, measured_seconds=None,
                               forced=True)
    measured = measure_reference_seconds()
    return HostCalibration(factor=max(1.0, measured / REFERENCE_SECONDS),
                           measured_seconds=measured, forced=False)


def host_factor() -> float:
    return host_calibration().factor


def budget(seconds: float) -> float:
    """A base budget (calibrated on the development host) for this host."""
    return seconds * host_factor()


@dataclass(frozen=True)
class LinearTiming:
    """The two measurements of one ``assert_linear_time`` call."""

    label: str
    small_size: int
    small_seconds: float
    size: int
    seconds: float
    budget_seconds: float

    @property
    def ratio(self) -> float:
        return self.seconds / max(self.small_seconds, GROWTH_FLOOR_SECONDS)

    def describe(self) -> str:
        return (f"{self.label}: {self.seconds:.4f} s at size {self.size}, "
                f"{self.small_seconds:.4f} s at size {self.small_size} "
                f"(growth {self.ratio:.1f}x, limit {GROWTH_LIMIT}x; budget "
                f"{self.budget_seconds:.4f} s at host factor "
                f"{host_factor():.2f})")


def assert_linear_time(seconds_at: Callable[[int], float], size: int,
                       base_budget_seconds: float, label: str = "", *,
                       compare_with_larger: bool = False) -> LinearTiming:
    """Assert that the work ``seconds_at(size)`` times stays within its
    host-calibrated budget and grows linearly.

    ``seconds_at(n)`` builds the test's shape at size ``n`` and returns the
    seconds the work took, measured the way the test measures it (CPU time,
    a median, a best of several). It must not be answered from a cache.

    The growth is measured from a quarter of ``size`` to ``size``, both held
    to the budget. A shape that only exists from ``size`` on (more distinct
    characters than a table keeps, which a quarter of the text cannot hold)
    passes ``compare_with_larger``: the growth is then measured from ``size``
    to four times it, and the budget holds at ``size``.

    The smaller size runs first, so a quadratic regression fails on its
    budget before the larger size takes seconds; any time series that grows
    with the input then fails at the larger size too."""
    if size < GROWTH:
        raise ValueError(f"size {size} has no quarter to compare against")
    if compare_with_larger:
        small_size, large_size = size, size * GROWTH
    else:
        small_size, large_size = size // GROWTH, size
    budget_seconds = budget(base_budget_seconds)
    small_seconds = seconds_at(small_size)
    assert small_seconds < budget_seconds, (
        f"{label}: {small_seconds:.4f} s at size {small_size}, over the "
        f"budget {budget_seconds:.4f} s at host factor {host_factor():.2f}")
    seconds = seconds_at(large_size)
    timing = LinearTiming(label, small_size, small_seconds, large_size,
                          seconds, budget_seconds)
    if not compare_with_larger:
        assert timing.seconds < budget_seconds, timing.describe()
    for _ in range(GROWTH_RETRIES):
        if timing.ratio < GROWTH_LIMIT:
            break
        timing = LinearTiming(
            label, small_size, min(timing.small_seconds,
                                   seconds_at(small_size)),
            large_size, min(timing.seconds, seconds_at(large_size)),
            budget_seconds)
    assert timing.ratio < GROWTH_LIMIT, (
        f"superlinear growth: {timing.describe()}")
    return timing
