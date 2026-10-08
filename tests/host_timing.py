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
   size's reading reaches the floor (each step measures one size). A larger
   reading still under the floor is not reused: the next quarter size is
   scaled to read ``GROWTH_STEP_MARGIN`` times the floor were the work
   linear (``next_quarter_size``, one step instead of two). A reading
   is never raised to hide work (IR75-01: raising 2 ms readings to a 20 ms
   floor at unchanged sizes let quadratic work of 16-159 ms pass). The
   input grows, whatever building the shape costs, while the larger
   reading is at least ``GROWTH_DETECTION_SECONDS`` (16 ms: the quadratic
   work main's 2 ms floor caught), up to ``MAX_INPUT_GROWTH`` times the
   test's sizes. A smaller larger reading is decided against the floor at
   the test's sizes (quadratic work that small passed main's floor too),
   so a test timing thousands of fast shapes does not grow each one.
   Grown sizes are held to the growth limit, not to the budget (it holds
   at the test's sizes). The grown sizes never replace the test's own
   (task 3180, IR78-01): work superlinear up to the test's size and
   cheaper past it (a window, or an input cap the larger size reaches)
   grows linearly there. So the test's own pair is also held to main's
   rule (``GROWTH_LIMIT`` against ``DETECTION_FLOOR_SECONDS``), measured
   again first, as b81777b did, when it could reach the limit once
   repeated (``growth_repetitions``), and every grown pair is held to
   the growth limit against the 20 ms floor (as task 3178 held the pair
   the growth stopped at): a part fails at the first pair it is over at, so
   another part still growing cannot carry it to 16 times the test's input
   (IR78-02). Each timed call (and each reference run) runs
   with the garbage collector paused, as ``timeit`` does: a full collection
   walks the whole heap a long xdist worker has built up, so it paused the
   larger size far more than its input could (task 3175: 0.0129 s against
   0.1341 s for a linear scan).

4. **Contention.** One reading of each size is at the mercy of the load it
   ran under. A burst that inflates the larger reading fakes growth, so a
   pair over the limit is measured again (``GROWTH_RETRIES``) before it
   fails; one that inflates the quarter reading hides it (task 3184: a
   regression planted in ``sanitize`` read 0.0228 s then 0.1486 s, 6.5x,
   during a full parallel suite at host load 20, and passed). So a pair
   reading from ``BORDERLINE_GROWTH`` up to the limit, on a quarter reading
   over its floor, is measured again ``BORDERLINE_RETRIES`` times before it
   is decided, and any other pair whose quarter reading is over its floor
   (and whose larger reading is at least ``GROWTH_LIMIT`` floors, so a
   quieter quarter could fail it) ``SETTLE_RETRIES`` time before it
   passes: a burst on that one reading
   could push a 9.3x regression to 4.9x, under the band (task 3187,
   R3184-01). Every reading taken again alternates between the two sizes
   (quarter, larger, quarter, larger, ...; the own pair's repetitions too),
   so a burst falls on both sizes rather than on one size's whole run, and
   each size keeps its fastest reading: the minimum is the estimate of the
   work's cost the load inflated least. A later pair at the same sizes
   starts from those readings (``_GrownReadings.settle``). Tests that
   choose their own sizes settle their pairs the same way: work timed per
   unit of input at several sizes (``assert_linear_per_unit``) and scans
   collecting every slow shape (``settled_growth``). A ratio compared by
   hand on one reading of each size is decided by the load it ran under
   (task 3185: CI read a linear scan at 0.067 s per MB at 50 KB and
   0.137 s at 200 KB), and the timing meta-test flags it.

5. **Confirmation.** A pair still over the limit once settled is confirmed
   before it fails (``_confirmed``, task 3188): CI read a linear pattern of
   task 3138 at 0.0030 s then 0.0250 s, 8.3x against the test's own pair's
   2 ms floor, each the fastest of three readings, on work that grows 4.0x
   (its larger readings were all inflated). Both sizes are read again in
   up to ``CONFIRM_ROUNDS`` rounds of several interleaved readings each, at
   the pair's own sizes (a capped regression grows linearly past them,
   IR78-01), and the pair fails only when a majority of the rounds read
   over the limit too. Each round reads the larger size afresh (its bursts
   fake growth) and holds it against the quarter size's fastest reading so
   far (a quieter quarter only raises the ratio). Superlinear work reads
   over the limit in every round and fails after ``CONFIRM_MAJORITY`` of
   them; no floor, limit or budget moves. A part the rounds clear keeps
   its fastest round, the reading the next grown pair starts from
   (R3188-02), and growth a first ratio over the limit stopped goes on
   when the stop pair's settled readings would have grown it (R3188-01):
   work superlinear only past the test's size is read there.

6. **Growth exponent.** A GROWN pair still over the limit once confirmed
   is judged on its growth exponent over three sizes before it fails
   (``_exponent_judged``, task 3191): CI read a linear pattern of task
   3139 at 0.0320 s at 1 MB and 0.2605 s at 4 MB, 8.1x, confirmed: its
   cost per byte steps up about 2x where the input outgrows a CPU cache of
   the small runner, a constant factor, not growth. Two sizes cannot tell
   that step from superlinear work; three can. The size ``GROWTH`` times
   the pair's larger one is read, and the exponent (the least-squares
   slope of log(seconds) against log(size), for sizes ``GROWTH`` apart the
   outer pair's) must stay under ``GROWTH_EXPONENT_LIMIT`` (1.5, the
   limit's own exponent): the outer pair is held to ``GROWTH_LIMIT ** 2``,
   settled and confirmed as every pair is. Quadratic work grows
   ``GROWTH ** 2`` on every step and fails; a cache step of under
   ``GROWTH`` times per byte reads about ``GROWTH`` on the step past it
   and passes. The test's own pair (IR78-01) and the pair the growth
   stopped at, at the test's sizes, never go by an exponent; nor does a
   pair whose third size does not exist (``InputTooLarge``): it decides
   alone, as before. The outer pair is read again only within
   ``EXPONENT_SECONDS`` of the third size's first reading (superlinear
   work makes it expensive); a part still over then stays over, and so
   does one whose confirming rounds the deadline cut short with any round
   over (R3191-02). Only readings shaped like a cache step are cleared
   (R3191-01): a first step of at most ``EXPONENT_MAX_FIRST_STEP`` (15x)
   and a second of at least ``EXPONENT_MIN_SECOND_STEP``; work superlinear
   up to an input cap or a window just past the pair reads a steeper first
   step or a flat second one and fails, as main failed it. A first step
   over the window, or a third size too dear to read for a part it could
   clear, is not read at all (R3191-03).

7. **Sub-floor readings.** A pair is never failed on a smaller reading
   under the noise floor (task 3204): CI read redaction pattern 24 at
   0.0020 s at 16 KB (the fastest of 36 calls of a linear pattern taking
   3.7-4.6 ms here) against 0.0182 s at 64 KB, 9.1x against the own pair's
   2 ms floor. A call of a few milliseconds can run whole inside a quiet
   spell of its runner that a call four times longer never fits, so the
   fastest of many short calls reads under their cost, and main's floor
   is a tenth of the noise floor. A pair over the limit only on a smaller
   reading totalling under ``GROWTH_FLOOR_SECONDS`` (it would pass were
   the reading raised to that floor, ``over_on_a_sub_floor_reading``) is
   read again at the same sizes (the own pair is what catches work
   superlinear only up to the test's size, IR78-01), each reading the mean
   of enough interleaved runs that the smaller size totals
   ``SUB_FLOOR_MARGIN`` times the floor (``sub_floor_repetitions``), and
   decided there; when no more runs fit ``SUB_FLOOR_READING_SECONDS`` it
   is decided as before (fail closed). A grown pair's floor is the noise
   floor, so a grown pair is never read again for it.

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
from typing import Callable, Iterator, Mapping, Sequence, TypeVar

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
# A larger reading of at least the floor becomes the next quarter reading.
# One under it (16-20 ms) would be a quarter under the floor again, so the
# next quarter size is that size scaled, as linear work would scale, to a
# reading of this many times the floor: one growth step past the floor
# instead of two (518 units of 3138's pattern 24 read 16.5 ms each on
# Python 3.10, and two 4x steps each took the test past 190 s).
GROWTH_STEP_MARGIN = 1.2
# A ratio over the limit is measured again (keeping the fastest total of
# each size) before it counts: one descheduled run must not fail a test,
# a quadratic one fails every time.
GROWTH_RETRIES = 2
# A ratio from here up to the limit is borderline: linear work reads about
# 4x, and contention that inflated the quarter reading compresses quadratic
# work's 16x toward it (task 3184: a regression planted in ``sanitize`` read
# 0.0228 s then 0.1486 s, 6.5x, at host load 20, and passed on one reading
# of each size). Once a pair reads borderline, both sizes are measured again
# this many times, interleaved, before it is decided on each size's fastest
# reading (``_settled_readings``).
BORDERLINE_GROWTH = 5.0
BORDERLINE_RETRIES = 4
# A quarter reading over its pair's floor may itself be inflated, and a
# burst on it alone can push a regression under the band: a true 9.3x pair
# whose quarter read 1.9x its cost read 4.9x and passed on one reading of
# each size (task 3187, R3184-01). So such a pair is measured again at least
# this many times, both sizes interleaved, before it passes: each size's
# reading is then the fastest of two or more, whatever the first ratio.
# (A quarter reading under the floor is raised to it, so a burst there
# cannot lower the ratio.)
SETTLE_RETRIES = 1
# Work timed per unit of input reads the same at every size when linear.
# ``assert_linear_per_unit`` holds it to the limit of one GROWTH step, per
# unit: GROWTH_LIMIT for GROWTH times the input is this much per unit.
PER_UNIT_GROWTH_LIMIT = GROWTH_LIMIT / GROWTH
# A per-unit pair over that limit is measured again this many times (both
# sizes, interleaved), not GROWTH_RETRIES: each reading is a run of fixed
# length (about a megabyte scanned), so settling costs seconds at most, and
# work over its budget per unit has failed before its pair is read. More
# readings cannot hide quadratic work: the smaller size's fastest reading
# only falls, which only raises the ratio.
PER_UNIT_OVER_RETRIES = BORDERLINE_RETRIES
# A pair still over the limit once settled is CONFIRMED before it fails: it
# is read again in up to this many rounds, each of several interleaved
# readings of both sizes, and fails only when a majority of them read over
# the limit too (task 3188: CI read pattern 24 of task 3138 at 0.0030 s
# then 0.0250 s, 8.3x, each the fastest of three readings, on work that
# grows 4.0x; its larger readings were all inflated). Quadratic work reads
# over the limit in every round, so it fails after CONFIRM_MAJORITY rounds.
CONFIRM_ROUNDS = 5
CONFIRM_MAJORITY = CONFIRM_ROUNDS // 2 + 1
# Interleaved readings of each size in one round: at least this many, and
# more while the quarter size's total over the round is under
# GROWTH_FLOOR_SECONDS (a round of 3 ms readings is 7 of them)...
CONFIRM_READINGS = 3
# ...within what this much wall time per round pays for, measured on the
# round's first reading (an expensive pair reads once per round).
CONFIRM_ROUND_SECONDS = 1.0
# A GROWN pair still over the limit once confirmed is judged on its growth
# EXPONENT over three sizes before it fails (task 3191): the pair's sizes
# and the size GROWTH times its larger one. A linear scan whose cost per
# byte steps up when its input outgrows a CPU cache reads over the limit
# across the one step the cache boundary falls in (CI read a linear pattern
# of task 3139 at 0.0320 s at 1 MB and 0.2605 s at 4 MB, 8.1x) and about
# GROWTH across the next; quadratic work reads GROWTH ** 2 across every
# step. The exponent is the least-squares slope of log(seconds) against
# log(size) over the three sizes; for sizes GROWTH apart that is the slope
# between the outer two, so the pair fails when the outer pair grows at
# least GROWTH_LIMIT ** 2 (the limit of both steps, an exponent of 1.5).
GROWTH_EXPONENT_LIMIT = math.log(GROWTH_LIMIT) / math.log(GROWTH)
EXPONENT_STEPS = 2
# The outer pair is read again (settled and confirmed) only while one more
# re-reading ends within this many seconds of the third size's first
# reading; a part still over the limit when it does not stays over (fail
# closed, as before task 3191). Work quadratic past the test's size read
# about 10 s at its third size and took 69 s to fail on six readings of it
# (6 s without the exponent); a cache step's third size (CI's 16 MB, about
# 1 s) is read again in full.
EXPONENT_SECONDS = 15.0
# The exponent clears only what looks like a cache step (R3191-01): a first
# step of at most this much (a cost per byte stepping up under 3.75 times;
# CI's 8.1x and 2x-3.5x steps read 8-14x)...
EXPONENT_MAX_FIRST_STEP = GROWTH * 3.75
# ...and a second step of at least this much (linear past the step). Work
# superlinear up to an input cap or a window just past the pair reads up to
# 58x and then under GROWTH, under the outer limit; it stays over.
EXPONENT_MIN_SECOND_STEP = GROWTH / 1.5
# A part is never failed on a smaller reading under the noise floor (task
# 3204): CI read redaction pattern 24 at 0.0020 s at 16 KB, the fastest of
# 36 calls of a pattern taking 3.7-4.6 ms there on two interpreters here,
# against 0.0182 s at 64 KB: 9.1x against the own pair's 2 ms floor on work
# that grows 4.0x. A call of a few milliseconds can run whole inside a
# quiet spell of its runner (a turbo clock, an idle SMT sibling) that a
# call several times longer never fits, so the fastest of many short calls
# reads under their cost (the same runner read real linear work at 1.28x
# the cost per unit over 0.3 s that it read over 25 ms). So a part over the
# limit only on a smaller reading totalling under GROWTH_FLOOR_SECONDS over
# its runs (it would pass were that reading raised to the noise floor,
# ``over_on_a_sub_floor_reading``) is read again at the same sizes, each
# reading the mean of enough interleaved runs that the smaller size's total
# reaches this many times GROWTH_FLOOR_SECONDS, and is decided there
# (``sub_floor_repetitions``)...
SUB_FLOOR_MARGIN = 2.0
# ...within MAX_GROWTH_REPETITIONS runs and what this much wall time per
# reading (the runs of both sizes) pays for. A part still under the floor
# when no more runs fit is decided as before (fail closed).
SUB_FLOOR_READING_SECONDS = 1.0
# The test's own pair of sizes is held to main's floor and, for one
# ``seconds_at``, to the rule of b81777b (the default branch before task
# 3178): a quarter reading under GROWTH_FLOOR_SECONDS whose larger reading
# could reach the limit once repeated is measured again, both sizes alike,
# until the quarter size's total reaches the floor. At most this many runs
# of each size...
MAX_GROWTH_REPETITIONS = 32
# ...and no more extra runs than this much wall time pays for: a
# ``seconds_at`` building a 200 KB review around a 10 ms measurement
# repeats less, or not at all (main's 2 ms floor then decides).
GROWTH_REPETITION_SECONDS = 0.5

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


def growth_repetitions(small_seconds: float, pair_cost_seconds: float = 0.0,
                       seconds: float | None = None) -> int:
    """How many times a growth check measures each size of the test's own
    pair (the rule of b81777b) so that the quarter size's total reaches
    ``GROWTH_FLOOR_SECONDS``: at most ``MAX_GROWTH_REPETITIONS``,
    and no more extra pairs than ``GROWTH_REPETITION_SECONDS`` of wall time
    pays for when one call of each size took ``pair_cost_seconds``.

    ``seconds`` is the larger size's first reading. When that many
    repetitions of it would total under ``GROWTH_LIMIT`` floors, the ratio
    cannot reach the limit against the floor the repetitions earn
    (``own_pair_floor``), so the sizes are measured once and main's floor
    decides: linear work under the floor (about 4 floors in all) is never
    repeated, a suspicious reading is."""
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


def own_pair_floor(repetitions: int) -> float:
    """The floor of the test's own pair when each size's reading is the
    mean of ``repetitions`` runs: main's ``DETECTION_FLOOR_SECONDS``, or
    ``GROWTH_FLOOR_SECONDS`` spread over the runs when that is lower
    (b81777b raised the quarter size's TOTAL to the floor)."""
    return min(DETECTION_FLOOR_SECONDS, GROWTH_FLOOR_SECONDS / repetitions)


def over_on_a_sub_floor_reading(small_seconds: float, seconds: float,
                                repetitions: int = 1,
                                limit: float = GROWTH_LIMIT) -> bool:
    """Whether a pair over ``limit`` is over only on a smaller reading under
    the noise floor (task 3204): the smaller reading (the mean of
    ``repetitions`` runs) totals under ``GROWTH_FLOOR_SECONDS`` over its
    runs, and the larger one's total is under ``limit`` times that floor,
    so the pair would pass were the smaller reading raised to the floor.
    Such a reading is never the work's cost: the pair is read again
    (``sub_floor_repetitions``). A grown pair's floor is the noise floor,
    so it never is; the test's own pair's (main's 2 ms) is under it."""
    small_total = repetitions * small_seconds
    return (small_total < GROWTH_FLOOR_SECONDS
            and growth_ratio(small_total, repetitions * seconds) < limit)


def sub_floor_repetitions(small_seconds: Sequence[float], repetitions: int,
                          pair_cost_seconds: float) -> int:
    """How many runs of each size a pair takes when it is read again
    because a part over the limit had a smaller reading (the mean of
    ``repetitions`` runs; one per such part in ``small_seconds``) under the
    noise floor (task 3204): enough that every such reading totals
    ``SUB_FLOOR_MARGIN`` times ``GROWTH_FLOOR_SECONDS``, at most
    ``MAX_GROWTH_REPETITIONS`` and no more than one reading of
    ``SUB_FLOOR_READING_SECONDS`` of wall time pays for when one run of
    each size took ``pair_cost_seconds``. ``repetitions`` (read again: no)
    when no more runs fit."""
    target = SUB_FLOOR_MARGIN * GROWTH_FLOOR_SECONDS
    needed = max((math.ceil(target / max(seconds, 1e-6))
                  for seconds in small_seconds), default=repetitions)
    affordable = MAX_GROWTH_REPETITIONS
    if pair_cost_seconds > 0:
        affordable = int(SUB_FLOOR_READING_SECONDS / pair_cost_seconds)
    return max(repetitions, min(MAX_GROWTH_REPETITIONS, needed, affordable))


def _grown_further(grown: _GrownReadings, *sizes: int) -> bool:
    """Measure ``sizes``, the next sizes of a growth check (the quarter
    first); False when the shape does not exist at one (``InputTooLarge``)."""
    try:
        for size in sizes:
            grown.at(size)
    except InputTooLarge:
        return False
    return True


def _grow_step(grown: _GrownReadings, large_size: int,
               quarter_size: int) -> int | None:
    """The quarter size of the next growth step whose sizes the shape
    exists at: ``quarter_size``, else the 4x step reusing ``large_size``
    (a scaled step can pass a cap the 4x step stays under); None when
    neither exists."""
    for candidate in dict.fromkeys((quarter_size, large_size)):
        if _grown_further(grown, candidate, candidate * GROWTH):
            return candidate
    return None


def next_quarter_size(large_size: int, larger_seconds: float,
                      max_quarter_size: int) -> int:
    """The quarter size of the next growth step: ``large_size`` (its
    reading is reused) when ``larger_seconds`` takes the floor, else
    ``large_size`` scaled to a linear reading of ``GROWTH_STEP_MARGIN``
    times the floor; never over ``max_quarter_size``.

    The input only grows while the larger reading is at least
    ``GROWTH_DETECTION_SECONDS``, so the scale stays under 1.5x."""
    if larger_seconds >= GROWTH_FLOOR_SECONDS:
        quarter_size = large_size
    else:
        scale = (GROWTH_STEP_MARGIN * GROWTH_FLOOR_SECONDS
                 / max(larger_seconds, GROWTH_DETECTION_SECONDS))
        quarter_size = math.ceil(large_size * scale)
    return min(quarter_size, max_quarter_size)


def grows_further(small_seconds: float, seconds: float,
                  input_growth: float) -> bool:
    """Whether the input grows again: the next quarter size is the larger
    size, or larger (``next_quarter_size``), and the size 4 times it is
    measured.

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
    """The readings of one growth check by size (the seconds of each part):
    the ladder (N/4, N; N, 4N; 4N, 16N) reuses each larger reading of at
    least the floor as the next quarter's (``next_quarter_size``)."""

    def __init__(self, measure: Callable[[int], dict[str, float]]) -> None:
        self._measure = measure
        self.readings: dict[int, dict[str, float]] = {}
        # The wall time of the last call at each size, building the shape
        # included: what one more repetition of it costs.
        self.costs: dict[int, float] = {}
        # How many readings of each size its reading is the fastest of
        # (``_settled_readings``).
        self.samples: dict[int, int] = {}

    def measure(self, size: int) -> dict[str, float]:
        """A new reading at ``size``, kept as its reading: the test's sizes
        are measured again under the load (``measure_under_load``)."""
        started = time.monotonic()
        self.readings[size] = self._measure(size)
        self.costs[size] = time.monotonic() - started
        self.samples[size] = 1
        return self.readings[size]

    def settle(self, size: int, readings: Mapping[str, float],
               samples: int) -> None:
        """Keep ``readings``, each part's fastest of ``samples`` readings at
        ``size``, as its reading: a later pair at the size starts from it
        instead of measuring the size again from its first reading."""
        self.readings[size] = dict(readings)
        self.samples[size] = samples

    def repetitions(self, small_size: int, size: int) -> int:
        """How many runs of each size the pair (``small_size``, ``size``)
        takes (``growth_repetitions``): as many as its most suspicious part
        needs, within what its calls cost."""
        small, large = self.at(small_size), self.at(size)
        pair_cost = self.costs[small_size] + self.costs[size]
        return max((growth_repetitions(small[part], pair_cost, large[part])
                    for part in large), default=1)

    def at(self, size: int) -> dict[str, float]:
        if size not in self.readings:
            self.measure(size)
        return self.readings[size]


def _interleaved_means(measure: Callable[[int], Mapping[str, float]],
                       small_size: int, size: int, repetitions: int,
                       first: tuple[Mapping[str, float],
                                    Mapping[str, float]] | None = None
                       ) -> tuple[dict[str, float], dict[str, float]]:
    """Each part's mean over ``repetitions`` measurements at ``small_size``
    and at ``size``, taken alternately (quarter, larger, quarter, larger,
    ...) so both sizes run under the same load: a burst of contention then
    inflates both sizes' readings, not one size's whole run. ``first`` (a
    reading already taken at each size) counts as one measurement of
    each."""
    small_totals = dict(first[0]) if first is not None else {}
    large_totals = dict(first[1]) if first is not None else {}
    for _ in range(repetitions - (0 if first is None else 1)):
        for totals, at_size in ((small_totals, small_size),
                                (large_totals, size)):
            for part, seconds in measure(at_size).items():
                totals[part] = totals.get(part, 0.0) + seconds
    return ({part: total / repetitions for part, total in small_totals.items()},
            {part: total / repetitions for part, total in large_totals.items()})


@dataclass(frozen=True)
class Confirmation:
    """The confirming rounds of a part still over the limit once its pair
    settled (``_confirmed``): how many rounds were read, how many of them
    read over the limit, and how many interleaved readings of each size
    each round took."""

    rounds: int
    rounds_over: int
    readings: int

    @property
    def cut_short(self) -> bool:
        """Fewer rounds than a majority were read: a deadline stopped them
        (``_confirmed``) before either verdict could have a majority."""
        return self.rounds < CONFIRM_MAJORITY

    @property
    def confirmed(self) -> bool:
        """A majority of the rounds read over the limit, or no round was
        read (fail closed: no evidence clears a pair). Rounds cut short
        clear a pair only when every one of them read under the limit
        (R3191-02: one round under, or a tie of two, is no majority)."""
        if self.cut_short:
            return self.rounds == 0 or self.rounds_over > 0
        return 2 * self.rounds_over > self.rounds

    def describe(self) -> str:
        if self.rounds == 0:
            return ("confirmed: no confirming round fitted the time left, "
                    "so the pair stays over the limit (fail closed)")
        verdict = "confirmed" if self.confirmed else "not confirmed"
        short = (f" (cut short under {CONFIRM_MAJORITY} rounds: any round "
                 f"over keeps the pair over)" if self.cut_short else "")
        return (f"{verdict}: over the limit in {self.rounds_over} of "
                f"{self.rounds} confirming rounds of {self.readings} "
                f"interleaved readings of each size{short}")


@dataclass(frozen=True)
class _Pair:
    """A pair of sizes a growth check holds to the limit, and how: the
    floor of the smaller reading, the runs each reading is a mean of and
    how many times a pair over the limit is measured again."""

    small_size: int
    size: int
    input_growth: float
    floor_seconds: float
    repetitions: int = 1
    over_retries: int = GROWTH_RETRIES
    # How many GROWTH steps the sizes are apart: 1, or EXPONENT_STEPS for
    # the outer pair of a growth exponent (``_exponent_judged``). The limit
    # and the borderline band hold the same exponent over every step.
    steps: int = 1

    @property
    def limit(self) -> float:
        return GROWTH_LIMIT ** self.steps

    @property
    def borderline(self) -> float:
        return BORDERLINE_GROWTH ** self.steps


def _within(deadline: float | None, reread_seconds: float) -> bool:
    """Whether one more re-reading of a pair, taking ``reread_seconds`` as
    the last one did, ends by ``deadline`` (``time.monotonic``; None: the
    pair has no deadline)."""
    return deadline is None or time.monotonic() + reread_seconds <= deadline


def _settled_readings(measure: Callable[[int], Mapping[str, float]],
                      pair: _Pair, small: Mapping[str, float],
                      large: Mapping[str, float], parts: list[str],
                      samples: int = 1, deadline: float | None = None,
                      reread_seconds: float = 0.0
                      ) -> tuple[dict[str, float], dict[str, float],
                                 list[str], int, dict[str, Confirmation]]:
    """The parts over the growth limit at ``pair`` (``pair.limit``:
    ``GROWTH_LIMIT``, or its power for a pair several steps apart) once its
    readings have settled.

    ``small`` and ``large`` are each part's reading at the pair's sizes,
    each already the fastest of ``samples`` readings of its size.
    One reading of each size is at the mercy of the load it ran under, so
    both sizes are measured again (all parts, ``pair.repetitions`` runs
    each, the quarter size and the larger one interleaved) while a part is
    over the limit, up to ``pair.over_retries`` times (``GROWTH_RETRIES``
    as main did, unless the check settles more); once
    any part has read a borderline ratio (``BORDERLINE_GROWTH`` up to the
    limit) on a quarter reading over the pair's floor,
    ``BORDERLINE_RETRIES`` times in all, however it reads after; and
    otherwise, when any part's quarter reading is over the pair's floor
    and its larger reading could reach the limit against a quieter one
    (at least ``GROWTH_LIMIT`` floors), ``SETTLE_RETRIES`` times before it
    passes (a burst on that one quarter reading can push a regression's
    ratio under the band, R3184-01). (A
    ratio taken against the floor cannot rise when measured again: only a
    quarter reading over the floor can hide growth.) Each part keeps its
    fastest reading of each size: the minimum is the
    estimate of the work's cost the load inflated least, so a burst that
    inflated the quarter reading (and hid quadratic growth, task 3184) or
    the larger one (and faked it) is outrun by a quieter run of that size.
    A part still over the limit then is confirmed (``_confirmed``) and
    counts as over only when a majority of the confirming rounds read over
    the limit too.
    With a ``deadline`` (the outer pair of a growth exponent,
    ``EXPONENT_SECONDS``) no re-reading starts that would end after it,
    ``reread_seconds`` being what reading both sizes once last took; a part
    still over then stays over (``_confirmed`` reads no round: fail closed).
    Returns the readings kept, the parts still over, how many readings of
    each size the minimum was taken over and the confirmation of each part
    that was confirmed."""
    small, large = dict(small), dict(large)
    borderline_seen = False
    over_floor_seen = False
    retries_done = samples - 1
    while True:
        ratios = {part: growth_ratio(small[part], large[part],
                                     pair.floor_seconds) for part in parts}
        over = [part for part in parts if not ratios[part] < pair.limit]
        borderline_seen = borderline_seen or any(
            pair.borderline <= ratios[part] < pair.limit
            and small[part] > pair.floor_seconds for part in parts)
        # A quarter reading over the floor may be inflated; it matters only
        # when a quieter one could carry the ratio to the limit, which a
        # larger reading under ``pair.limit`` floors never reaches.
        over_floor_seen = over_floor_seen or any(
            small[part] > pair.floor_seconds
            and large[part] >= pair.limit * pair.floor_seconds
            for part in parts)
        if borderline_seen:
            retries = BORDERLINE_RETRIES
        elif over_floor_seen:
            retries = SETTLE_RETRIES
        else:
            retries = 0
        if over:
            retries = max(retries, pair.over_retries)
        if retries_done >= retries or not _within(deadline, reread_seconds):
            confirmations: dict[str, Confirmation] = {}
            if over:
                small, large, over, confirmations = _confirmed(
                    measure, pair, small, large, over, deadline,
                    reread_seconds)
            return small, large, over, 1 + retries_done, confirmations
        started = time.monotonic()
        small_again, large_again = _interleaved_means(
            measure, pair.small_size, pair.size, pair.repetitions)
        reread_seconds = time.monotonic() - started
        for part in small:
            small[part] = min(small[part], small_again[part])
            large[part] = min(large[part], large_again[part])
        retries_done += 1


def _confirmed(measure: Callable[[int], Mapping[str, float]], pair: _Pair,
               small: Mapping[str, float], large: Mapping[str, float],
               over: list[str], deadline: float | None = None,
               reread_seconds: float = 0.0
               ) -> tuple[dict[str, float], dict[str, float], list[str],
                          dict[str, Confirmation]]:
    """Confirm the parts ``over`` the limit at ``pair`` on their settled
    readings (``small``, ``large``) before they fail (task 3188).

    Both sizes are read again in up to ``CONFIRM_ROUNDS`` rounds, each of
    ``CONFIRM_READINGS`` or more interleaved readings (enough for the
    quarter size's total over the round to reach ``GROWTH_FLOOR_SECONDS``,
    within ``CONFIRM_ROUND_SECONDS``), measured as the pair measures them
    (``pair.repetitions`` runs each) and at the pair's own sizes: a capped
    or windowed regression grows linearly past the test's size, so a larger
    pair could clear it (IR78-01). A round reads over the limit when its
    fastest larger reading is ``GROWTH_LIMIT`` times the part's fastest
    quarter reading so far (against the pair's floor). The larger reading
    is the one a burst inflates to fake growth, so each round reads it
    afresh; the quarter keeps its fastest reading through the rounds, since
    a quieter quarter can only raise the ratio, and a round deciding on a
    quarter it read loaded would clear a regression (R3184-03). The rounds
    stop once every part has a majority either way, and with a
    ``deadline`` (``_settled_readings``) before a round that would end
    after it: with no round read a part stays over (fail closed).

    A part over the limit in a majority of the rounds stays over, on its
    settled readings; any other is kept on the reading of its fastest
    round, which is under the limit as most rounds read (R3188-02: the
    median round handed the next grown pair an inflated quarter reading,
    and a later pair 9.5x over the fastest reading passed at 7.1x). Returns
    the readings, the parts still over and each confirmed part's
    ``Confirmation``."""
    small, large = dict(small), dict(large)
    quarter_total = pair.repetitions * max(min(small[part] for part in over),
                                           1e-6)
    wanted = min(MAX_GROWTH_REPETITIONS, max(
        CONFIRM_READINGS, math.ceil(GROWTH_FLOOR_SECONDS / quarter_total)))
    readings = wanted
    larger_by_round: dict[str, list[float]] = {part: [] for part in over}

    def rounds_over(part: str) -> int:
        return sum(1 for seconds in larger_by_round[part]
                   if not growth_ratio(small[part], seconds,
                                       pair.floor_seconds) < pair.limit)

    def decided(part: str) -> bool:
        read_over = rounds_over(part)
        read_under = len(larger_by_round[part]) - read_over
        return max(read_over, read_under) >= CONFIRM_MAJORITY

    rounds = 0
    while (rounds < CONFIRM_ROUNDS and not all(map(decided, over))
           and _within(deadline, reread_seconds)):
        round_large = {part: math.inf for part in over}
        reading = 0
        while reading < readings:
            started = time.monotonic()
            small_again, large_again = _interleaved_means(
                measure, pair.small_size, pair.size, pair.repetitions)
            reread_seconds = max(time.monotonic() - started, 1e-9)
            if rounds == 0 and reading == 0:
                readings = max(1, min(
                    wanted, 1 + int(CONFIRM_ROUND_SECONDS / reread_seconds)))
            for part in over:
                small[part] = min(small[part], small_again[part])
                round_large[part] = min(round_large[part], large_again[part])
            reading += 1
        for part in over:
            larger_by_round[part].append(round_large[part])
        rounds += 1

    confirmations = {part: Confirmation(len(larger_by_round[part]),
                                        rounds_over(part), readings)
                     for part in over}
    still_over = [part for part in over if confirmations[part].confirmed]
    for part in over:
        if part not in still_over:
            # The fastest round: under the limit, as most rounds read, and
            # the estimate the load inflated least (R3188-02).
            large[part] = min(larger_by_round[part])
    return small, large, still_over, confirmations


@dataclass(frozen=True)
class GrowthExponent:
    """The growth exponent a grown pair over the limit was judged on
    (``_exponent_judged``, task 3191): the readings at the pair's sizes and
    at the size GROWTH times its larger one, whether the outer pair grew by
    the limit of both steps, and its confirming rounds when it did once
    settled."""

    small_size: int
    small_seconds: float
    size: int
    seconds: float
    larger_size: int
    larger_seconds: float
    floor_seconds: float
    superlinear: bool
    confirmation: Confirmation | None = None

    @staticmethod
    def _slope(small_seconds: float, seconds: float, floor_seconds: float,
               size_ratio: float) -> float:
        ratio = growth_ratio(small_seconds, seconds, floor_seconds)
        return math.log(max(ratio, 1e-12)) / math.log(size_ratio)

    @property
    def exponent(self) -> float:
        """The least-squares slope of log(seconds) against log(size) over the
        three sizes, which for sizes GROWTH apart is the outer pair's (the
        smaller reading raised to the floor first, as every pair's)."""
        return self._slope(self.small_seconds, self.larger_seconds,
                           self.floor_seconds,
                           self.larger_size / self.small_size)

    @property
    def first_step(self) -> float:
        """The growth over the pair (its smaller reading raised to the
        floor first)."""
        return growth_ratio(self.small_seconds, self.seconds,
                            self.floor_seconds)

    @property
    def second_step(self) -> float:
        """The growth from the pair's larger size to the third size."""
        return growth_ratio(self.seconds, self.larger_seconds,
                            self.floor_seconds)

    @property
    def cache_step(self) -> bool:
        """The three readings look like a cache step (R3191-01): a first
        step of at most ``EXPONENT_MAX_FIRST_STEP`` and a second of at least
        ``EXPONENT_MIN_SECOND_STEP``. Work superlinear up to an input cap
        or a window just past the pair reads a larger first step, or a flat
        second one, and is not cleared on its exponent."""
        return (self.first_step <= EXPONENT_MAX_FIRST_STEP
                and self.second_step >= EXPONENT_MIN_SECOND_STEP)

    def describe(self) -> str:
        first_step = self._slope(self.small_seconds, self.seconds,
                                 self.floor_seconds,
                                 self.size / self.small_size)
        second_step = self._slope(self.seconds, self.larger_seconds,
                                  self.floor_seconds,
                                  self.larger_size / self.size)
        if not self.cache_step:
            verdict = (f"not a cache step (steps {self.first_step:.1f}x "
                       f"then {self.second_step:.1f}x; a cache step reads "
                       f"at most {EXPONENT_MAX_FIRST_STEP:g}x then at least "
                       f"{EXPONENT_MIN_SECOND_STEP:.2f}x)")
        elif self.superlinear:
            verdict = "superlinear"
        else:
            verdict = "a constant-factor step, not growth"
        confirmed = (f"; {self.confirmation.describe()}"
                     if self.confirmation is not None else "")
        return (f"growth exponent {self.exponent:.2f} over sizes "
                f"{self.small_size}, {self.size} and {self.larger_size} "
                f"({self.small_seconds:.4f} s, {self.seconds:.4f} s, "
                f"{self.larger_seconds:.4f} s; {first_step:.2f} then "
                f"{second_step:.2f} per step), limit "
                f"{GROWTH_EXPONENT_LIMIT:.2f}: {verdict}{confirmed}")


def _exponent_judged(grown: _GrownReadings,
                     measure: Callable[[int], Mapping[str, float]],
                     pair: _Pair, small: Mapping[str, float],
                     large: Mapping[str, float], over: list[str]
                     ) -> tuple[list[str], dict[str, GrowthExponent]]:
    """The parts ``over`` the limit at the GROWN ``pair`` (settled and
    confirmed) whose growth exponent over three sizes is superlinear too
    (task 3191).

    The third size is GROWTH times the pair's larger one. For sizes GROWTH
    apart the least-squares exponent is the outer pair's, so the outer pair
    (``EXPONENT_STEPS`` steps) is held to the limit of both steps,
    ``GROWTH_LIMIT ** 2``, through the machinery every pair goes through
    (``_settled_readings``): both sizes read again interleaved, each keeping
    its fastest reading, and an outer pair still over confirmed in rounds.
    Quadratic work grows ``GROWTH ** 4`` over the outer pair and fails; a
    linear scan whose cost per byte steps up past a cache size inside the
    pair reads over the limit there and about GROWTH past it, under the
    outer limit for any step under GROWTH times (task 3191: CI read 8.1x
    across 1 to 4 MB on a linear pattern). A step of GROWTH times or more
    per byte (a cliff, not a cache) still fails.

    The outer pair is read again only within ``EXPONENT_SECONDS`` of the
    third size's first reading, which work superlinear past the test's
    size makes expensive: a part still over when no more fits stays over
    (fail closed), as it would without the third size.

    Three sizes cannot tell a cache step from superlinear work that stops
    growing just past the pair: quadratic work capped (a window, an input
    cap) under twice the pair's larger size reads 16x and then under
    GROWTH, as a cache step does. A cap near the test's size is what the
    test's own pair is held for, never on an exponent (IR78-01); only a
    part that pair and every smaller grown pair passed reaches this.

    So only a part whose readings look like a cache step is cleared
    (R3191-01, ``GrowthExponent.cache_step``): a part whose first step is
    over ``EXPONENT_MAX_FIRST_STEP`` stays over without the third size
    being read (R3191-03: it cannot be cleared, and superlinear work makes
    the third size expensive), and one whose second step is under
    ``EXPONENT_MIN_SECOND_STEP`` stays over on its exponent (a cap or a
    window flattened it). The third size is not read either when reading
    it would take more than ``EXPONENT_SECONDS`` were the part one it could
    clear: one whose outer pair reads under the outer limit, so at most
    that limit over its first step times the pair's larger reading.

    Without the third size (the shape does not exist there,
    ``InputTooLarge``) no exponent is measured and every part stays over:
    the pair decides alone, as before. Returns the parts still over and
    each judged part's ``GrowthExponent``; the readings settled at the
    outer pair's sizes are kept in ``grown``."""
    first_steps = {part: growth_ratio(small[part], large[part],
                                      pair.floor_seconds) for part in over}
    judged = [part for part in over
              if first_steps[part] <= EXPONENT_MAX_FIRST_STEP]
    if not judged:
        return over, {}
    outer_limit = GROWTH_LIMIT ** EXPONENT_STEPS
    clearing_cost = grown.costs[pair.size] * outer_limit / min(
        first_steps[part] for part in judged)
    if clearing_cost > EXPONENT_SECONDS:
        return over, {}
    larger_size = pair.size * GROWTH
    started = time.monotonic()
    if not _grown_further(grown, larger_size):
        return over, {}
    outer = _Pair(pair.small_size, larger_size, pair.input_growth,
                  pair.floor_seconds, pair.repetitions, pair.over_retries,
                  EXPONENT_STEPS)
    first_samples = min(grown.samples[pair.small_size],
                        grown.samples[larger_size])
    outer_small, outer_large, outer_over, samples, confirmations = (
        _settled_readings(measure, outer, small, grown.at(larger_size),
                          judged, first_samples, started + EXPONENT_SECONDS,
                          outer.repetitions * (grown.costs[pair.small_size]
                                               + grown.costs[larger_size])))
    for at_size, readings in ((pair.small_size, outer_small),
                              (larger_size, outer_large)):
        grown.settle(at_size, readings,
                     grown.samples[at_size] + samples - first_samples)
    exponents: dict[str, GrowthExponent] = {}
    for part in judged:
        shape = GrowthExponent(
            pair.small_size, outer_small[part], pair.size, large[part],
            larger_size, outer_large[part], pair.floor_seconds, False,
            confirmations.get(part))
        exponents[part] = replace(
            shape, superlinear=part in outer_over or not shape.cache_step)
    still_over = [part for part in over
                  if part not in exponents or exponents[part].superlinear]
    return still_over, exponents


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
    input_growth: float = 1.0
    # ``DETECTION_FLOOR_SECONDS`` when the shape did not exist at the next
    # larger size (``InputTooLarge``) and at the test's own pair, or
    # ``own_pair_floor`` there when repeated.
    floor_seconds: float = GROWTH_FLOOR_SECONDS
    # Each reading is the mean of this many runs (the test's own pair,
    # ``growth_repetitions``).
    repetitions: int = 1
    # Each reading is the fastest of this many readings of its size, taken
    # interleaved with the other size's (``_settled_readings``).
    samples: int = 1
    # The confirming rounds when the settled readings were over the limit
    # (``_confirmed``); a part they did not confirm keeps its fastest round.
    confirmation: Confirmation | None = None
    # The growth exponent over three sizes of a grown pair the confirming
    # rounds kept over the limit (``_exponent_judged``): a part it does not
    # find superlinear passes on a ratio over the limit, a constant-factor
    # step.
    exponent: GrowthExponent | None = None
    # The smaller reading (the total of its runs) under the noise floor that
    # the pair, over the limit on it, was read again from over
    # ``repetitions`` runs of each size (task 3204): never compared itself.
    sub_floor_seconds: float | None = None

    @property
    def ratio(self) -> float:
        return growth_ratio(self.small_seconds, self.seconds, self.floor_seconds)

    def describe(self) -> str:
        runs = (f" over {self.repetitions} runs of each"
                if self.repetitions > 1 else "")
        fastest = (f"; each the fastest of {self.samples} interleaved "
                   f"readings" if self.samples > 1 else "")
        confirmed = (f"; {self.confirmation.describe()}"
                     if self.confirmation is not None else "")
        exponent = (f"; {self.exponent.describe()}"
                    if self.exponent is not None else "")
        read_again = (f"; read again over {self.repetitions} runs of each "
                      f"size: the smaller reading over the limit was "
                      f"{self.sub_floor_seconds:.4f} s, under the "
                      f"{GROWTH_FLOOR_SECONDS:g} s noise floor"
                      if self.sub_floor_seconds is not None else "")
        return (f"{self.label}: {self.seconds:.4f} s at size {self.size}, "
                f"{self.small_seconds:.4f} s at size {self.small_size} "
                f"(growth {self.ratio:.1f}x at {self.input_growth:.3g}x the "
                f"test's input{runs}, floor {self.floor_seconds:g} s, limit "
                f"{GROWTH_LIMIT}x; budget {self.budget_seconds:.4f} s at host "
                f"factor {self.factor:.2f}{fastest}{confirmed}{exponent}"
                f"{read_again})")


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
    reading is under ``GROWTH_FLOOR_SECONDS``, the input grows
    (``grows_further``, ``next_quarter_size``); the grown sizes are held to
    the growth limit only. The test's own sizes are held to it as well, as
    main and b81777b held them (``_check_growth``, IR78-01)."""
    if size < GROWTH:
        raise ValueError(f"size {size} has no quarter to compare against")
    seconds_at = _collector_paused_calls(seconds_at)
    if compare_with_larger:
        small_size, large_size = size, size * GROWTH
    else:
        small_size, large_size = size // GROWTH, size
    grown = _GrownReadings(lambda at_size: {label: seconds_at(at_size)})
    small_seconds, small_factor = measure_under_load(
        lambda: grown.measure(small_size)[label], base_budget_seconds)
    _check_budget(label, small_seconds, small_size, base_budget_seconds,
                  small_factor)
    if compare_with_larger:
        # Past ``size`` only the growth counts: no budget, no reference.
        seconds, factor = grown.at(large_size)[label], small_factor
    else:
        seconds, factor = measure_under_load(
            lambda: grown.measure(large_size)[label], base_budget_seconds)
        _check_budget(label, seconds, large_size, base_budget_seconds, factor)
    timings = _check_growth(
        grown, lambda at_size: {label: seconds_at(at_size)}, small_size,
        large_size, {label: label}, base_budget_seconds * factor, factor,
        grown.repetitions(small_size, large_size))
    return timings[label]


def assert_linear_times(seconds_at: Callable[[int], Mapping[str, float]],
                        size: int, base_budget_seconds: float,
                        label: str = "") -> dict[str, LinearTiming]:
    """``assert_linear_time`` for work timed in several parts by one
    measurement (every pattern of a scan, timed by one probe run):
    ``seconds_at(n)`` returns the seconds of each part at size ``n``, and
    every part is held to the budget and to the growth limit. While any
    part's smaller reading is under the floor and ``grows_further`` says
    so, the input grows for every part (one call times them all; the
    growing part with the smallest larger reading sets the step); the
    parts are decided at the test's own sizes, at every grown pair (a part
    fails at the first one it is over at, IR78-02) and at the sizes the
    growth stopped at (``_check_growth``). A part over the growth limit is
    measured again (all parts, keeping each part's fastest time) before it
    counts."""
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

    first_parts: list[set[str]] = []

    def measured(at_size: int) -> dict[str, float]:
        """``seconds_at(at_size)``, refused when its parts are not those
        of the first measurement."""
        times = dict(seconds_at(at_size))
        if not first_parts:
            first_parts.append(set(times))
        elif set(times) != first_parts[0]:
            fail(f"{label}: the parts differ between the sizes (at size "
                 f"{at_size}): {sorted(set(times) ^ first_parts[0])}")
        return times

    grown = _GrownReadings(measured)
    small, small_factor = measure_under_load(
        lambda: grown.measure(small_size), base_budget_seconds, slowest_part)
    check_budget(small, small_size, small_factor)
    large, factor = measure_under_load(
        lambda: grown.measure(size), base_budget_seconds, slowest_part)
    check_budget(large, size, factor)
    return _check_growth(grown, measured, small_size, size,
                         {part: f"{label}: {part}" for part in large},
                         base_budget_seconds * factor, factor,
                         grown.repetitions(small_size, size))


@dataclass(frozen=True)
class SettledGrowth:
    """One pair of sizes a test measured itself, its readings settled under
    contention (``settled_growth``): each the fastest of ``samples``
    readings of its size, taken interleaved with the other size's."""

    small_size: int
    small_seconds: float
    size: int
    seconds: float
    samples: int = 1
    floor_seconds: float = GROWTH_FLOOR_SECONDS
    # The confirming rounds when the settled readings were over the limit.
    confirmation: Confirmation | None = None

    @property
    def ratio(self) -> float:
        return growth_ratio(self.small_seconds, self.seconds, self.floor_seconds)


def settled_growth(seconds_at: Callable[[int], float], small_size: int,
                   size: int, small_seconds: float, seconds: float,
                   floor_seconds: float = GROWTH_FLOOR_SECONDS
                   ) -> SettledGrowth:
    """The growth from ``small_size`` to ``size`` of work a test times at
    sizes it chose itself (a scan of many shapes that collects every slow
    one rather than failing at the first), settled the way every pair of
    ``assert_linear_time`` is (``_settled_readings``, task 3184): over
    ``GROWTH_LIMIT`` it is measured again ``GROWTH_RETRIES`` times, and
    borderline ``BORDERLINE_RETRIES`` times, both sizes interleaved, each
    keeping its fastest reading; still over, it is confirmed
    (``_confirmed``), and the readings of a pair a majority of the
    confirming rounds did not read over the limit are its fastest round's.

    ``seconds_at(n)`` times the work at size ``n`` as the test does;
    ``small_seconds`` and ``seconds`` are the readings the test already
    took. The caller compares the result's ``ratio`` with ``GROWTH_LIMIT``
    (task 3185: a ratio compared by hand on one reading of each size is the
    load's verdict, not the work's)."""
    part = "growth"
    small, large, _, samples, confirmations = _settled_readings(
        _collector_paused_calls(lambda at_size: {part: seconds_at(at_size)}),
        _Pair(small_size, size, 1.0, floor_seconds),
        {part: small_seconds}, {part: seconds}, [part])
    return SettledGrowth(small_size, small[part], size, large[part], samples,
                         floor_seconds, confirmations.get(part))


def assert_linear_per_unit(per_unit_at: Callable[[int], float],
                           sizes: Sequence[int], base_budget_seconds: float,
                           label: str = "") -> dict[int, float]:
    """Assert that the work ``per_unit_at`` times costs the same per unit of
    input at every one of ``sizes`` (smallest first), within its
    host-calibrated budget per unit; returns each size's settled reading.

    ``per_unit_at(n)`` builds the shape at size ``n`` and returns the
    seconds per unit of input its work took, measured so that every reading
    is a run of the same length whatever the size (task 3175's CI shape: a
    median of runs that each scan about a megabyte, over the megabytes
    scanned), never one sub-millisecond call. Linear work reads the same at
    every size; quadratic work reads ``n / sizes[0]`` times as much.

    Each size is held to the budget at the factor measured around it
    (``measure_under_load``), then, before the next size is read, to
    ``PER_UNIT_GROWTH_LIMIT`` against the smallest size: the per-unit form
    of one ``GROWTH`` step's ``GROWTH_LIMIT``. That pair is settled as every
    growth pair is (``_settled_readings``): over the limit or borderline,
    both sizes are measured again, interleaved, each keeping its fastest
    reading, so a burst on either size does not decide alone (task 3185:
    CI read 0.067 s per MB at 50 KB and 0.137 s at 200 KB on linear work,
    the larger size measured again twice, the smaller never). A pair over
    the limit is measured again ``PER_UNIT_OVER_RETRIES`` times, not
    ``GROWTH_RETRIES``: CI's three readings of 200 KB all fell in one burst.
    The smallest size keeps its settled reading for the next pair."""
    if len(sizes) < 2 or list(sizes) != sorted(set(sizes)):
        raise ValueError(
            f"sizes {list(sizes)!r}: at least two, smallest first, each once")
    per_unit_at = _collector_paused_calls(per_unit_at)

    def read(at_size: int) -> float:
        seconds, factor = measure_under_load(lambda: per_unit_at(at_size),
                                             base_budget_seconds)
        _check_budget(f"{label} (seconds per unit)", seconds, at_size,
                      base_budget_seconds, factor)
        return seconds

    def as_one_step(larger_size: int) -> Callable[[int], dict[str, float]]:
        """Readings with the larger size's scaled by GROWTH: linear work
        then reads as one GROWTH step, held to GROWTH_LIMIT."""
        def measure(at_size: int) -> dict[str, float]:
            reading = per_unit_at(at_size)
            return {label: reading * GROWTH if at_size == larger_size
                    else reading}
        return measure

    smallest = sizes[0]
    settled = {smallest: read(smallest)}
    for size in sizes[1:]:
        seconds = read(size)
        small, large, over, samples, confirmations = _settled_readings(
            as_one_step(size),
            _Pair(smallest, size, size / smallest, GROWTH_FLOOR_SECONDS,
                  over_retries=PER_UNIT_OVER_RETRIES),
            {label: settled[smallest]}, {label: seconds * GROWTH}, [label])
        settled[smallest], settled[size] = small[label], large[label] / GROWTH
        if over:
            fail(f"{label}: superlinear per unit: {settled[size]:.4f} s per "
                 f"unit at size {size}, {settled[smallest]:.4f} s at size "
                 f"{smallest} ({settled[size] / settled[smallest]:.2f}x, "
                 f"limit {PER_UNIT_GROWTH_LIMIT}x; each the fastest of "
                 f"{samples} interleaved readings; "
                 f"{confirmations[label].describe()}): {settled}")
    return settled


def _check_growth(grown: _GrownReadings,
                  measure: Callable[[int], Mapping[str, float]],
                  test_small_size: int, test_size: int,
                  labels: Mapping[str, str], budget_seconds: float,
                  factor: float, repetitions: int = 1
                  ) -> dict[str, LinearTiming]:
    """The growth verdict of every part of ``labels`` (part -> label), once
    the test's sizes are in ``grown``; returns each part's timing at the
    sizes the growth stopped at. A check fails when ANY of these holds:

    1. The test's own pair (``test_small_size``, ``test_size``) breaks
       main's rule: growth of ``GROWTH_LIMIT`` against
       ``DETECTION_FLOOR_SECONDS`` (pre-3175 main, 883c9f2). With
       ``repetitions`` (``growth_repetitions`` of the most suspicious
       part, within what a call costs), both sizes are means of that many
       runs against ``own_pair_floor``: also the rule of b81777b. Grown sizes are no substitute for these:
       superlinear work that gets cheaper past the test's size (a window,
       an input cap or truncation the larger size reaches) grows linearly
       there (IR78-01: 3129's ``sanitize`` truncates at 64,000 characters,
       just over its 60,000-character test size).
    2. A pair of grown sizes reaches ``GROWTH_LIMIT`` against
       ``GROWTH_FLOOR_SECONDS`` (the floor task 3178 held grown sizes to),
       for any part, and its growth exponent over that pair and the size
       ``GROWTH`` times past it is superlinear too (``_exponent_judged``,
       task 3191). A part over the limit fails at the first pair it is
       over at, the one size past it read for its exponent, so another
       part still growing cannot carry it to 16 times the test's input
       (IR78-02: 2 minutes for a 159 ms quadratic part).
    3. The pair the growth stopped at breaks the growth check of task 3178
       (``grows_further``): against ``GROWTH_FLOOR_SECONDS``, or main's
       floor where the shape does not exist at the next size; at grown
       sizes its exponent decides as in 2. When it passes on settled
       readings that ``grows_further`` would grow from (its first ratio
       over the limit stopped the growth), the growth goes on from them
       (R3188-01) and 2 and 3 apply to the pairs past it.

    Each pair over the limit is measured again before it fails, and a
    borderline one before it passes (``_settled_readings``); one still over
    then fails only when a majority of its confirming rounds read over the
    limit (``_confirmed``).
    """
    parts = list(labels)
    # Grown pairs whose exponent cleared a part over the limit: the pair the
    # growth stopped at is that pair again, and is not confirmed twice.
    cleared: dict[_Pair, dict[str, LinearTiming]] = {}

    def timings_at(pair: _Pair, small: Mapping[str, float],
                   large: Mapping[str, float], at_parts: list[str],
                   samples: int, confirmations: Mapping[str, Confirmation],
                   exponents: Mapping[str, GrowthExponent],
                   sub_floor: Mapping[str, float]
                   ) -> dict[str, LinearTiming]:
        return {part: LinearTiming(labels[part], pair.small_size, small[part],
                                   pair.size, large[part], budget_seconds,
                                   factor, pair.input_growth,
                                   pair.floor_seconds, pair.repetitions,
                                   samples, confirmations.get(part),
                                   exponents.get(part), sub_floor.get(part))
                for part in at_parts}

    def held(pair: _Pair, means: tuple[Mapping[str, float],
                                       Mapping[str, float]] | None = None,
             *, grown_sizes: bool = False, judged: list[str] | None = None,
             sub_floor: Mapping[str, float] | None = None
             ) -> dict[str, LinearTiming]:
        """``pair`` held to the limit on ``means`` (the means of its
        repetitions), or on the readings ``grown`` keeps at its sizes; those
        are then replaced by the settled ones, so a later pair at the same
        sizes (the pair the growth stopped at) starts from them. A pair of
        ``grown_sizes`` still over the limit once confirmed is judged on
        its growth exponent over three sizes (``_exponent_judged``).

        Only the ``judged`` parts are held (every part by default). When
        every part over the limit is over only on a smaller reading under
        the noise floor (``over_on_a_sub_floor_reading``), none of them
        fails on it: the pair is held again on means of more runs of each
        size (``sub_floor_repetitions``), interleaved, its floor spread over
        the runs as the own pair's is (``own_pair_floor``), and those parts
        are decided there, ``sub_floor`` keeping the readings read again
        from (task 3204). Any other part over the limit fails as before, at
        once, and the parts beside it over only on such a reading are named
        as not judged, never as growth."""
        if pair in cleared:
            return cleared[pair]
        judged = parts if judged is None else judged
        sub_floor = {} if sub_floor is None else sub_floor
        if means is not None:
            small, large = means
            first_samples = 1
        else:
            small, large = grown.at(pair.small_size), grown.at(pair.size)
            first_samples = min(grown.samples[pair.small_size],
                                grown.samples[pair.size])
        small, large, over, samples, confirmations = _settled_readings(
            measure, pair, small, large, judged, first_samples)
        if means is None:
            for at_size, readings in ((pair.small_size, small),
                                      (pair.size, large)):
                grown.settle(at_size, readings, grown.samples[at_size]
                             + samples - first_samples)
        read_again: dict[str, LinearTiming] = {}
        sub_floor_over = [part for part in over if over_on_a_sub_floor_reading(
            small[part], large[part], pair.repetitions, pair.limit)]
        if over and len(sub_floor_over) == len(over):
            repetitions = sub_floor_repetitions(
                [small[part] for part in over], pair.repetitions,
                grown.costs[pair.small_size] + grown.costs[pair.size])
            if repetitions > pair.repetitions:
                again = replace(pair, repetitions=repetitions,
                                floor_seconds=min(
                                    pair.floor_seconds,
                                    GROWTH_FLOOR_SECONDS / repetitions))
                read_again = held(
                    again, _interleaved_means(measure, pair.small_size,
                                              pair.size, repetitions),
                    grown_sizes=grown_sizes, judged=over,
                    sub_floor={part: pair.repetitions * small[part]
                               for part in over})
                over = []
        exponents: dict[str, GrowthExponent] = {}
        if over and grown_sizes:
            over, exponents = _exponent_judged(grown, measure, pair, small,
                                               large, over)
        if over:
            # Beside a part over on readings that are its cost, a part over
            # only on a sub-floor reading is not read again (the check fails
            # anyway) and is never reported as growth itself: it is named
            # as not judged. Alone, unread for want of runs, it fails as
            # before (fail closed).
            failing = [part for part in over
                       if part not in sub_floor_over] or over
            not_judged = [labels[part] for part in over
                          if part not in failing]
            unjudged = (f"; not judged, over the limit only on a smaller "
                        f"reading under the {GROWTH_FLOOR_SECONDS:g} s noise "
                        f"floor (task 3204): {', '.join(not_judged)}"
                        if not_judged else "")
            fail("superlinear growth: " + "; ".join(
                timing.describe() for timing in timings_at(
                    pair, small, large, failing, samples, confirmations,
                    exponents, sub_floor).values()) + unjudged)
        timings = timings_at(pair, small, large, judged, samples,
                             confirmations, exponents, sub_floor)
        timings.update(read_again)
        if exponents or read_again:
            cleared[pair] = timings
        return timings

    # 1. The test's own pair, its repetitions interleaved.
    own_pair = _Pair(test_small_size, test_size, 1.0,
                     own_pair_floor(repetitions), repetitions)
    if repetitions == 1:
        held(own_pair)
    else:
        held(own_pair, _interleaved_means(
            measure, test_small_size, test_size, repetitions,
            (grown.at(test_small_size), grown.at(test_size))))

    # 2. Growth (task 3178), every grown pair held to the growth limit.
    small_size, size, input_growth = test_small_size, test_size, 1.0
    floor_seconds = GROWTH_FLOOR_SECONDS
    # The shape does not exist at the next size: the input never grows again.
    exhausted = False

    def growing_parts() -> list[str]:
        small_times, large_times = grown.at(small_size), grown.at(size)
        return [part for part in parts
                if grows_further(small_times[part], large_times[part],
                                 input_growth)]

    while True:
        while not exhausted and (growing := growing_parts()):
            # The step every growing part needs: the one whose larger
            # reading is smallest sets it.
            quarter_size = _grow_step(grown, size, next_quarter_size(
                size, min(grown.at(size)[part] for part in growing),
                test_small_size * MAX_INPUT_GROWTH))
            if quarter_size is None:
                # The shape does not exist at the next size (a 200 KB
                # review grown 16x passes the gate's 2 MB artifact cap):
                # decided here with main's floor, under which a larger
                # reading of 16 ms that grew 16x fails (IR75-01 without
                # growing).
                floor_seconds = DETECTION_FLOOR_SECONDS
                exhausted = True
                break
            small_size, size = quarter_size, quarter_size * GROWTH
            input_growth = small_size / test_small_size
            # Against the floor task 3178 held grown sizes to: main never
            # measured them, and a quarter reading under 20 ms there is no
            # steadier than at the test's sizes (3138's pattern 22 read
            # 5.9 ms then 75.5 ms at 5.5 times its input under a full xdist
            # run, where 3178 grew on and passed it). Over the limit there,
            # the growth exponent over three sizes decides (task 3191).
            held(_Pair(small_size, size, input_growth, GROWTH_FLOOR_SECONDS),
                 grown_sizes=True)

        # 3. The pair the growth stopped at, as task 3178 decided it; at the
        # test's own sizes (no growth) exactly as before, never on an
        # exponent.
        stopped = held(_Pair(small_size, size, input_growth, floor_seconds),
                       grown_sizes=(small_size, size) != (test_small_size,
                                                          test_size))
        # Growth that stopped on a first ratio over the limit ("the retries
        # decide it") goes on when the stop pair's settled readings, or the
        # fastest round of a pair its confirming rounds cleared, would have
        # grown (R3188-01): work superlinear only past that pair is read
        # there, not passed on the readings that cleared it.
        if exhausted or not growing_parts():
            return stopped
