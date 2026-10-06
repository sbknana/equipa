#!/usr/bin/env python3
"""Probe run by tests/test_conftest_xdist_3160.py under `pytest -n 2`.

Not collected by the suite itself (no test_ prefix): only that test runs it,
in a child pytest. Each test records what its xdist worker sees (its
THEFORGE_DB, session directories and tmp_path) into one JSON file in the
directory named by EQUIPA_XDIST_PROBE_OUT, for the parent test to compare
across workers.

Copyright 2026 Forgeborn
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest

import conftest
from equipa import constants


@pytest.mark.parametrize("index", range(2))
def test_record_worker_view(index, tmp_path):
    db_path = Path(constants.THEFORGE_DB)
    conftest._assert_db_isolated("xdist-probe")  # pytest.exit on failure
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    scratch_db = tmp_path / "scratch.db"
    scratch_db.write_bytes(b"")
    record = {
        "worker": os.environ.get("PYTEST_XDIST_WORKER"),
        "conftest_worker": conftest.XDIST_WORKER,
        "pid": os.getpid(),
        "db": str(db_path),
        "db_env": os.environ["THEFORGE_DB"],
        "db_has_schema": "tasks" in tables,
        "db_is_safe": conftest._is_safe_test_db(db_path),
        "session_tmp": str(conftest.SESSION_TMP),
        "session_root": str(conftest.SESSION_ROOT),
        "tmp_path": str(tmp_path),
        "tmp_path_db_is_safe": conftest._is_safe_test_db(scratch_db),
    }
    out_dir = Path(os.environ["EQUIPA_XDIST_PROBE_OUT"])
    out_file = out_dir / f"{record['worker']}-{index}.json"
    out_file.write_text(json.dumps(record))
