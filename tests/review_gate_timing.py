"""Timing helpers of the review-gate tests (task 3161).

A single measurement of a 200 KB review flaked at its budget (one run in
three of the distinct_code_points family failed at 0.5 s, also under
``pytest -n auto``), so every review-gate timing test takes the median of
five runs of CPU time: a wall clock also counts the time an xdist worker
waits for a core (task 3160). The timing tests are also marked with one
xdist group, so ``-n auto --dist loadgroup`` runs them on one worker; the
documented ``--dist loadfile`` keeps each file on one worker anyway.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable

import pytest

TIMING_RUNS = 5
timing_test = pytest.mark.xdist_group("review_gate_timing")


def median_cpu_seconds(function: Callable[..., Any], *args: Any,
                       runs: int = TIMING_RUNS,
                       before: Callable[[], Any] | None = None,
                       **kwargs: Any) -> float:
    """The median CPU time of ``runs`` calls of ``function(*args)``;
    ``before`` (untimed) runs ahead of each call."""
    times = []
    for _ in range(runs):
        if before is not None:
            before()
        started = time.process_time()
        function(*args, **kwargs)
        times.append(time.process_time() - started)
    return statistics.median(times)
