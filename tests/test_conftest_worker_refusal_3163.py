#!/usr/bin/env python3
"""Task 3163 (review X1): a refusing or crashed xdist worker fails the run.

xdist treats only a worker's exit status 2 as fatal. When one worker's
conftest isolation check refused to run (pytest.exit, status 3), the other
workers ran its tests and the run could end with exit status 0, the
REFUSING reason printed nowhere. tests/conftest.py now sends a worker's
refusal to the controller, which prints it and forces exit status 3 when any
worker refused or went down.

The end-to-end tests run tests/xdist_refusal_probe.py in a child
`pytest -n 2` whose TMPDIR is the test's own directory.

Copyright 2026 Forgeborn
"""

import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

import conftest

PROBE = conftest.REPO_ROOT / "tests" / "xdist_refusal_probe.py"


def _run_probe(tmp_path, mode: str, worker: str = "gw1",
               dist: str = "loadfile", extra_args: tuple = (),
               extra_env: dict | None = None) -> subprocess.CompletedProcess:
    run_tmp = tmp_path / "tmp"
    run_tmp.mkdir()
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTEST_XDIST_WORKER", conftest.SESSION_ROOT_ENV)}
    env.update({
        "TMPDIR": str(run_tmp),
        "EQUIPA_REFUSAL_PROBE_WORKER": worker,
        "EQUIPA_REFUSAL_PROBE_MODE": mode,
        "EQUIPA_REFUSAL_PROBE_DB": str(tmp_path / "sentinel" / "theforge.db"),
        **(extra_env or {}),
    })
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-n", "2", "--dist", dist, *extra_args, str(PROBE)],
        cwd=conftest.REPO_ROOT, env=env, capture_output=True, text=True,
        timeout=180)


# --- the decision, one worker at a time ---

@pytest.mark.parametrize("workeroutput", [
    {"exitstatus": 0},
    {"exitstatus": 1},   # failed tests: the controller counts them itself
    {"exitstatus": 5},   # nothing to run on this worker (-k deselected all)
    {"exitstatus": 0, conftest.WORKER_REFUSAL_KEY: []},
])
def test_a_worker_that_finished_normally_does_not_fail_the_run(workeroutput):
    assert conftest._worker_failure("gw0", workeroutput, None) is None


def test_a_refusing_worker_fails_the_run_with_its_reasons():
    reasons = ["[conftest] REFUSING TO RUN (collection): x.THEFORGE_DB = /a",
               "[conftest] REFUSING TO RUN (configure): y"]
    failure = conftest._worker_failure(
        "gw3", {"exitstatus": 3, conftest.WORKER_REFUSAL_KEY: reasons}, None)
    assert failure.startswith("xdist worker gw3 refused:")
    for reason in reasons:
        assert reason in failure


def test_exit_status_3_without_a_reason_still_fails_the_run():
    failure = conftest._worker_failure("gw1", {"exitstatus": 3}, None)
    assert "gw1" in failure and "exit status 3" in failure


@pytest.mark.parametrize("workeroutput", [None, {}, {"exitstatus": 0}])
def test_a_worker_that_went_down_fails_the_run(workeroutput):
    failure = conftest._worker_failure("gw2", workeroutput,
                                       "Not properly terminated")
    assert failure == "xdist worker gw2 went down: Not properly terminated"


# --- the hooks, with the module's state swapped out ---

def _session(workeroutput=None):
    config = SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda name: None))
    if workeroutput is not None:
        config.workeroutput = workeroutput
    return SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)


def test_a_worker_sends_its_refusal_reasons(monkeypatch):
    monkeypatch.setattr(conftest, "_REFUSAL_REASONS", ["reason one"])
    monkeypatch.setattr(conftest, "_WORKER_FAILURES", [])
    workeroutput = {"exitstatus": 3}
    session = _session(workeroutput)

    conftest.pytest_sessionfinish(session, 3)

    assert workeroutput[conftest.WORKER_REFUSAL_KEY] == ["reason one"]
    assert session.exitstatus == pytest.ExitCode.OK


def test_the_controller_forces_exit_status_3(monkeypatch):
    monkeypatch.setattr(conftest, "_REFUSAL_REASONS", [])
    monkeypatch.setattr(conftest, "_WORKER_FAILURES",
                        ["xdist worker gw1 refused:\nwhy"])
    session = _session()

    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.INTERNAL_ERROR


def test_a_controller_without_failures_keeps_its_exit_status(monkeypatch):
    monkeypatch.setattr(conftest, "_REFUSAL_REASONS", [])
    monkeypatch.setattr(conftest, "_WORKER_FAILURES", [])
    session = _session()

    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)

    assert session.exitstatus == pytest.ExitCode.OK


def test_the_controller_records_a_refusing_node(monkeypatch):
    failures: list[str] = []
    monkeypatch.setattr(conftest, "_WORKER_FAILURES", failures)
    node = SimpleNamespace(
        gateway=SimpleNamespace(id="gw1"),
        workeroutput={"exitstatus": 3,
                      conftest.WORKER_REFUSAL_KEY: ["REFUSING TO RUN x"]},
        config=_session().config)

    conftest.pytest_testnodedown(node, None)

    assert failures == ["xdist worker gw1 refused:\nREFUSING TO RUN x"]


# --- end to end: a child `pytest -n 2` ---

def test_control_run_with_no_misbehaving_worker_passes(tmp_path):
    result = _run_probe(tmp_path, "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout, result.stdout
    assert "REFUSING" not in result.stdout + result.stderr


@pytest.mark.parametrize("worker", ["gw0", "gw1"])
def test_one_refusing_worker_fails_the_whole_run(tmp_path, worker):
    """The review's probe: an escape in one worker only. Before the fix the
    other worker ran every test and the run could exit 0 with the reason
    printed nowhere."""
    result = _run_probe(tmp_path, "escape", worker=worker)
    output = result.stdout + result.stderr

    assert result.returncode == 3, output
    assert f"xdist worker {worker} refused:" in result.stdout, output
    assert "REFUSING TO RUN (collection)" in result.stdout, output
    assert ("equipa.constants.THEFORGE_DB = "
            f"{tmp_path / 'sentinel' / 'theforge.db'}") in result.stdout, output
    assert "RUN FAILED: an xdist worker refused or went down" in output
    # The escaped path was never opened (sqlite would have created it).
    assert not (tmp_path / "sentinel").exists()


def test_every_refusing_worker_fails_the_run(tmp_path):
    """Both workers escape: exit status 3 with a refusal printed. xdist
    itself often stops on an INTERNALERROR after the first refusing worker
    (its scheduler asserts the worker had no pending tests), so only the
    first worker's reason is guaranteed to arrive."""
    result = _run_probe(tmp_path, "escape", worker="all")
    output = result.stdout + result.stderr

    assert result.returncode == 3, output
    assert re.search(r"xdist worker gw[01] refused:", result.stdout), output
    assert "REFUSING TO RUN (collection)" in result.stdout, output
    assert not (tmp_path / "sentinel").exists()


def test_a_crashed_worker_fails_the_run_with_exit_status_3(tmp_path):
    """xdist replaces a crashed worker and counts the crashing test as one
    failure (exit status 1). The run must say the worker went down and
    exit 3."""
    result = _run_probe(tmp_path, "crash", dist="each")
    output = result.stdout + result.stderr

    assert result.returncode == 3, output
    assert "xdist worker gw1 went down:" in result.stdout, output
    assert "RUN FAILED: an xdist worker refused or went down" in output


def test_a_worker_refusing_at_configure_fails_the_run(tmp_path):
    """A worker that imported equipa.constants before conftest set the test
    DB refuses in pytest_configure, before xdist can collect its output: the
    controller sees it go down, and the worker's own REFUSING line shows."""
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "refusal_probe_early_import.py").write_text(
        "import os\n"
        "if os.environ.get('PYTEST_XDIST_WORKER') == 'gw1':\n"
        "    import equipa.constants  # noqa: F401\n")
    python_path = os.pathsep.join([str(plugin_dir),
                                   str(conftest.REPO_ROOT)])
    result = _run_probe(tmp_path, "none",
                        extra_args=("-p", "refusal_probe_early_import"),
                        extra_env={"PYTHONPATH": python_path})
    output = result.stdout + result.stderr

    assert result.returncode == 3, output
    assert "xdist worker gw1 went down:" in result.stdout, output
    assert ("REFUSING TO RUN: equipa.constants was imported before the test "
            "DB was set") in output
