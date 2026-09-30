#!/usr/bin/env python3
"""Task 3127 / review finding P2A-05: agents cannot read the orchestrator env.

Agents run as the orchestrator's UID. While the orchestrator is dumpable, any
agent shell can read ``/proc/<orchestrator pid>/environ`` and recover every
credential the env allowlist dropped. The orchestrator now sets
``PR_SET_DUMPABLE`` 0 before its first spawn; exec'd children regain
dumpability, so agents keep working.

The main test starts a fake orchestrator process with a fake credential in
its launch environment. It spawns a probe "agent" through the real
``_spawn_agent_process`` path (with and without the per-agent launcher), and
the probe tries to read its orchestrator's /proc files. All values are fake.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from equipa import agent_runner, env_loader

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_DSN = "postgresql://fake:FAKE-3127-sentinel-pw@db.invalid/fake"

# The agent: tries to read the orchestrator's /proc files and reports what it
# could see, its own dumpability, and whether its own /proc is still usable.
PROBE_AGENT = '''import ctypes, json, os, sys
target, out_path = int(sys.argv[1]), sys.argv[2]
report = {}
for name in ("environ", "mem"):
    try:
        with open("/proc/%d/%s" % (target, name), "rb") as fh:
            data = fh.read(65536) if name == "environ" else b""
        report[name] = "readable:" + data.decode("latin-1")
    except PermissionError:
        report[name] = "PermissionError"
    except OSError as exc:
        report[name] = "OSError:%s" % exc.errno
libc = ctypes.CDLL(None)
report["dumpable"] = libc.prctl(3, 0, 0, 0, 0)
with open("/proc/self/environ", "rb") as fh:
    report["own_environ_readable"] = len(fh.read()) >= 0
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh)
'''

# The orchestrator: spawns the probe through the real spawn path.
FAKE_ORCHESTRATOR = '''import asyncio, ctypes, json, os, sys
repo, mode, probe, out_path, project = sys.argv[1:6]
sys.path.insert(0, repo)
import equipa.config as equipa_config
from equipa import agent_runner
equipa_config._active_dispatch_config = {}
if mode == "direct":
    agent_runner._agent_containment_supported = lambda: False
async def main():
    cmd = [sys.executable, probe, str(os.getpid()), out_path]
    process, agent = await agent_runner._spawn_agent_process(
        cmd, project_dir=project)
    stdout, stderr = await process.communicate()
    if agent is not None:
        agent.release()
    libc = ctypes.CDLL(None)
    print(json.dumps({"agent_exit": process.returncode,
                      "agent_stderr": stderr.decode(errors="replace"),
                      "orchestrator_dumpable": libc.prctl(3, 0, 0, 0, 0)}))
asyncio.run(main())
'''


def _run_fake_orchestrator(tmp_path: Path, mode: str) -> tuple[dict, dict]:
    probe = tmp_path / "probe_agent.py"
    probe.write_text(PROBE_AGENT, encoding="utf-8")
    orchestrator = tmp_path / "fake_orchestrator.py"
    orchestrator.write_text(FAKE_ORCHESTRATOR, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    report_path = tmp_path / "probe_report.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "DATABASE_URL": FAKE_DSN,
        "THEFORGE_DB": str(tmp_path / "scratch_theforge.db"),
    }
    proc = subprocess.run(
        [sys.executable, str(orchestrator), str(REPO_ROOT), mode, str(probe),
         str(report_path), str(project)],
        capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return summary, report


@pytest.mark.parametrize("mode", ["launcher", "direct"])
def test_agent_cannot_read_orchestrator_proc_environ(tmp_path, mode):
    summary, report = _run_fake_orchestrator(tmp_path, mode)

    assert report["environ"] == "PermissionError", report["environ"][:200]
    assert report["mem"] == "PermissionError"
    assert "FAKE-3127-sentinel-pw" not in json.dumps(report)
    assert summary["orchestrator_dumpable"] == 0


@pytest.mark.parametrize("mode", ["launcher", "direct"])
def test_agent_regains_dumpability_and_still_runs(tmp_path, mode):
    summary, report = _run_fake_orchestrator(tmp_path, mode)

    assert summary["agent_exit"] == 0, summary["agent_stderr"]
    assert report["dumpable"] == 1
    assert report["own_environ_readable"] is True


def test_protection_is_idempotent_and_reports_success():
    assert env_loader.protect_orchestrator_process() is True
    assert env_loader.protect_orchestrator_process() is True
    assert env_loader._prctl(env_loader._PR_GET_DUMPABLE) == 0


def test_non_linux_is_a_debug_logged_no_op(monkeypatch, caplog):
    monkeypatch.setattr(env_loader, "_orchestrator_non_dumpable", False)
    monkeypatch.setattr(env_loader.sys, "platform", "darwin")
    called = []
    monkeypatch.setattr(env_loader, "_prctl",
                        lambda *args: called.append(args) or 0)

    with caplog.at_level(logging.DEBUG, logger="equipa.env_loader"):
        assert env_loader.protect_orchestrator_process() is False

    assert called == []
    assert any(record.levelno == logging.DEBUG and "Linux-only" in record.message
               for record in caplog.records)


def test_prctl_failure_is_reported_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(env_loader, "_orchestrator_non_dumpable", False)

    def failing_prctl(*args):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(env_loader, "_prctl", failing_prctl)
    with caplog.at_level(logging.ERROR, logger="equipa.env_loader"):
        assert env_loader.protect_orchestrator_process() is False
    assert any("PR_SET_DUMPABLE" in record.message for record in caplog.records)


def test_spawn_refuses_when_protection_fails_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "protect_orchestrator_process",
                        lambda: False)
    marker = tmp_path / "started"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]

    with pytest.raises(agent_runner.AgentDispatchRefused, match="non-dumpable"):
        asyncio.run(agent_runner._spawn_agent_process(
            cmd, project_dir=str(tmp_path)))
    assert not marker.exists()
