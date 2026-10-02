#!/usr/bin/env python3
"""Task 3160: the suite runs under pytest-xdist with one test DB per worker.

tests/conftest.py runs in every xdist worker process. Each worker must get
its own THEFORGE_DB and session directory, and the test-DB guard
(_is_safe_test_db / _assert_db_isolated) must hold in every worker, judged
against the controller's session directory (where xdist puts each worker's
tmp_path). A child process that merely inherits PYTEST_XDIST_WORKER must
stay a top-level session.

Copyright 2026 Forgeborn
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

PROBE = conftest.REPO_ROOT / "tests" / "xdist_worker_probe.py"


def _session_dir(parent: Path, name: str = "eqt-controller") -> Path:
    path = parent / name
    path.mkdir()
    return path


def test_a_worker_of_this_controller_joins_its_session_root(tmp_path):
    root = _session_dir(tmp_path)
    assert conftest._xdist_session_root("gw3", str(root), str(root)) == root


@pytest.mark.parametrize("worker_id", [None, "", "master", "gw", "gw1/../x",
                                       "../gw1", "gw1 "])
def test_a_process_that_is_not_an_xdist_worker_stays_top_level(tmp_path,
                                                               worker_id):
    root = _session_dir(tmp_path)
    assert conftest._xdist_session_root(worker_id, str(root), str(root)) is None


def test_a_child_whose_tmpdir_moved_stays_top_level(tmp_path):
    """A test's subprocess inherits PYTEST_XDIST_WORKER and the exported
    root, but TMPDIR is the worker's own directory (or the test's)."""
    root = _session_dir(tmp_path)
    elsewhere = _session_dir(tmp_path, "eqt-worker")
    assert conftest._xdist_session_root("gw0", str(root),
                                        str(elsewhere)) is None
    assert conftest._xdist_session_root("gw0", None, str(root)) is None


def test_an_exported_root_that_is_not_a_session_dir_is_refused(tmp_path):
    plain = tmp_path / "not-a-session"
    plain.mkdir()
    assert conftest._xdist_session_root("gw0", str(plain), str(plain)) is None

    missing = tmp_path / "eqt-missing"
    assert conftest._xdist_session_root("gw0", str(missing),
                                        str(missing)) is None

    real = _session_dir(tmp_path, "real-target")
    link = tmp_path / "eqt-link"
    link.symlink_to(real, target_is_directory=True)
    assert conftest._xdist_session_root("gw0", str(link), str(link)) is None

    a_file = tmp_path / "eqt-file"
    a_file.write_text("")
    assert conftest._xdist_session_root("gw0", str(a_file),
                                        str(a_file)) is None


def test_this_process_db_is_inside_its_own_session_dir():
    assert conftest.TEST_DB_PATH.is_relative_to(conftest.SESSION_TMP)
    assert conftest.SESSION_TMP.is_relative_to(conftest.SESSION_ROOT)
    assert os.environ[conftest.SESSION_ROOT_ENV] == str(conftest.SESSION_ROOT)


def test_every_xdist_worker_gets_its_own_isolated_db(tmp_path):
    """Run the probe under `pytest -n 2 --dist each` (every worker runs
    every probe test) in a child pytest whose TMPDIR is this test's own
    directory, with the PYTEST_XDIST_WORKER and session root a test inside
    a worker would pass on, which must not make the child a worker."""
    out_dir = tmp_path / "records"
    out_dir.mkdir()
    run_tmp = tmp_path / "tmp"
    run_tmp.mkdir()
    env = {**os.environ,
           "TMPDIR": str(run_tmp),
           "EQUIPA_XDIST_PROBE_OUT": str(out_dir),
           "PYTEST_XDIST_WORKER": "gw7",
           conftest.SESSION_ROOT_ENV: str(conftest.SESSION_ROOT)}

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-n", "2", "--dist", "each", str(PROBE)],
        cwd=conftest.REPO_ROOT, env=env, capture_output=True, text=True,
        timeout=180)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout, result.stdout
    records = [json.loads(path.read_text())
               for path in sorted(out_dir.glob("*.json"))]
    assert len(records) == 4, records
    assert {record["worker"] for record in records} == {"gw0", "gw1"}

    by_worker = {}
    for record in records:
        by_worker.setdefault(record["worker"], []).append(record)
    views = {}
    for worker, worker_records in by_worker.items():
        # One process, one DB, one session directory per worker.
        assert len({(r["pid"], r["db"], r["session_tmp"])
                    for r in worker_records}) == 1, worker_records
        views[worker] = worker_records[0]

    roots = {view["session_root"] for view in views.values()}
    assert len(roots) == 1, views
    session_root = Path(roots.pop())
    # The child controller was top-level: its session directory sits in
    # the TMPDIR it was given, not in this run's session directory.
    assert session_root.parent == run_tmp
    assert session_root.name.startswith("eqt-")

    dbs = {view["db"] for view in views.values()}
    sessions = {view["session_tmp"] for view in views.values()}
    pids = {view["pid"] for view in views.values()}
    assert len(dbs) == len(sessions) == len(pids) == 2, views
    for worker, view in views.items():
        db = Path(view["db"])
        assert view["conftest_worker"] == worker
        assert view["db_env"] == view["db"]
        assert view["db_has_schema"], view
        assert view["db_is_safe"], view
        assert view["tmp_path_db_is_safe"], view
        assert db.parent.name.startswith(f"equipa-test-db-{worker}-")
        assert db.is_relative_to(view["session_tmp"])
        assert Path(view["session_tmp"]).parent == session_root
        assert Path(view["tmp_path"]).is_relative_to(session_root)
        assert not Path(view["tmp_path"]).is_relative_to(view["session_tmp"])

    # The child run removed everything it put in its temp directory: the
    # workers' DBs and session directories with the controller's.
    assert sorted(path.name for path in run_tmp.iterdir()) == []
