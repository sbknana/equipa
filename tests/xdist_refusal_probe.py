#!/usr/bin/env python3
"""Probe run by tests/test_conftest_worker_refusal_3163.py under `pytest -n 2`.

Not collected by the suite itself (no test_ prefix): only that test runs it,
in a child pytest. The xdist worker named by EQUIPA_REFUSAL_PROBE_WORKER
(every worker when it is ``all``) misbehaves the way EQUIPA_REFUSAL_PROBE_MODE says:

- ``escape``: at import time (collection) it rebinds
  equipa.constants.THEFORGE_DB to EQUIPA_REFUSAL_PROBE_DB, a sentinel path
  outside the run's session directory, so that worker's conftest isolation
  check refuses to run (the parent test checks the sentinel never appears);
- ``crash``: its first test kills the worker process.

Every other worker (and every worker when the mode is ``none``) just runs
the tests.

Copyright 2026 Forgeborn
"""

import os

import pytest

from equipa import constants

PROBE_WORKER = os.environ.get("EQUIPA_REFUSAL_PROBE_WORKER", "")
PROBE_MODE = os.environ.get("EQUIPA_REFUSAL_PROBE_MODE", "none")
IS_PROBE_WORKER = (PROBE_WORKER == "all"
                   or os.environ.get("PYTEST_XDIST_WORKER") == PROBE_WORKER)

if IS_PROBE_WORKER and PROBE_MODE == "escape":
    # Never opened: the collection-stage check stops this worker first.
    constants.THEFORGE_DB = os.environ["EQUIPA_REFUSAL_PROBE_DB"]


@pytest.mark.parametrize("index", range(4))
def test_probe(index):
    if IS_PROBE_WORKER and PROBE_MODE == "crash":
        os._exit(1)
    assert index in range(4)
