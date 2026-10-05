#!/usr/bin/env python3
"""Task 3169: the verify script's --inside run is bounded and testable.

On a clean CI runner every test that ran ``verify_agent_isolation.sh
--inside`` hit its 120 s timeout: the world-writable check searched the
whole root filesystem (``find / -xdev``), and a runner's / holds millions
of toolchain directories. The run now takes ``--root-fs DIR`` (the tests
name a directory of their own; the orchestrator passes nothing, so a real
host is still searched at /) and bounds the search with
``--scan-seconds`` (default WORLD_WRITABLE_SCAN_SECONDS). A search cut
short, or one that cannot run, FAILS: what it did not reach was not
probed, so the bound never turns into a pass.

``find`` and ``timeout`` are replaced by fakes on PATH, so nothing here
searches the host.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"

# The tools inside() calls that would otherwise read the host's state.
_QUIET_TOOLS = {
    "sudo": "exit 1",
    "crontab": "echo 'not allowed' >&2; exit 1",
    "at": "echo 'not allowed' >&2; exit 1",
    "loginctl": "echo no",
}

# A find(1) that records its arguments next to itself and finds nothing.
_RECORDING_FIND = (
    'printf "%s\\n" "$*" >> "$0.calls"\n'
    "exit 0"
)

# A find(1) whose root-filesystem search reports one world-writable
# directory and then never finishes; every other search finds nothing.
_HANGING_FIND = (
    'for argument in "$@"; do\n'
    '    if [ "$argument" = "-perm" ]; then\n'
    '        printf "%s/shared\\000" "$1"\n'
    "        exec sleep 30\n"
    "    fi\n"
    "done\n"
    "exit 0"
)


def _fake_bin(tmp_path: Path, tools: dict[str, str]) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name, body in tools.items():
        script = directory / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    return directory


def _run_inside(fake_bin: Path, *args: str) -> tuple[list[str], float]:
    """Run the inside checks as this (unisolated) user; return the output
    lines and the seconds the run took."""
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT), "--inside", *args],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"})
    return result.stdout.splitlines(), time.monotonic() - started


def _call(function: str, *args: str, prefix: str = "",
          path: str | None = None) -> tuple[list[str], int]:
    """Source the verify script, run ``prefix``, call one check function;
    return its output lines and the failures it counted."""
    result = subprocess.run(
        ["bash", "-c",
         f'source "$1"; shift; failures=0; {prefix} "$@"; '
         f'echo "failures=$failures"',
         "verify-test", str(VERIFY_SCRIPT), function, *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": path or os.environ["PATH"]})
    lines = result.stdout.splitlines()
    assert lines and lines[-1].startswith("failures="), (result.stdout,
                                                          result.stderr)
    return lines[:-1], int(lines[-1].partition("=")[2])


def test_without_root_fs_the_real_root_filesystem_is_searched(tmp_path):
    """The orchestrator passes no --root-fs: the search is still of /."""
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin)
    calls = (fake_bin / "find.calls").read_text().splitlines()
    assert "/ -xdev -type d -perm -0002 -print0" in calls
    assert any(re.search(r"on the root filesystem / ?(\(|$)", line)
               for line in lines), lines
    assert lines[-1].startswith("RESULT: ")


def test_root_fs_moves_every_disk_check_below_the_given_root(tmp_path):
    root = tmp_path / "rootfs"
    (root / "tmp" / ".X11-unix").mkdir(parents=True)
    (root / "var" / "tmp").mkdir(parents=True)
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", str(root))
    calls = (fake_bin / "find.calls").read_text().splitlines()
    assert f"{root} -xdev -type d -perm -0002 -print0" in calls
    assert not any(call.startswith("/ ") for call in calls), calls
    # The well-known names are read below the root, never on the host.
    assert any(line.startswith(f"FAIL agent can write the world-writable "
                               f"directory {root}/tmp/.X11-unix on the root "
                               f"filesystem {root} (") for line in lines)
    assert any(line.startswith(f"FAIL agent can write {root}/var/tmp on the "
                               f"root filesystem {root}") for line in lines)
    disk_lines = [line for line in lines if "root filesystem" in line]
    assert disk_lines and all(str(root) in line for line in disk_lines)


def test_a_search_that_does_not_finish_fails_within_the_bound(tmp_path):
    root = tmp_path / "rootfs"
    root.mkdir()
    shared = root / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _HANGING_FIND})
    lines, seconds = _run_inside(fake_bin, "--root-fs", str(root),
                                 "--scan-seconds", "1")
    assert (f"FAIL the search of the root filesystem {root} for "
            f"world-writable directories did not finish within 1 s: the "
            f"directories it did not reach were not probed") in lines
    # What it reported before the bound is still probed.
    assert any(line.startswith(f"FAIL agent can write the world-writable "
                               f"directory {shared} on the root filesystem")
               for line in lines), lines
    assert lines[-1].startswith("RESULT: FAIL")
    assert seconds < 25, f"the bounded run took {seconds:.1f} s"


def test_the_bound_fails_even_when_nothing_else_does(tmp_path):
    """Called alone, a cut-short search is the only failure counted."""
    root = tmp_path / "rootfs"
    root.mkdir()
    fake_bin = _fake_bin(tmp_path, {"find": _HANGING_FIND})
    started = time.monotonic()
    lines, failures = _call("check_world_writable_dirs", str(root),
                            prefix="WORLD_WRITABLE_SCAN_SECONDS=1;",
                            path=f"{fake_bin}:/usr/bin:/bin")
    assert failures == 1, lines
    assert lines == [f"FAIL the search of the root filesystem {root} for "
                     f"world-writable directories did not finish within 1 s: "
                     f"the directories it did not reach were not probed",
                     f"PASS agent cannot write any of 0 world-writable "
                     f"director(y/ies) on the root filesystem {root}"]
    assert time.monotonic() - started < 25


@pytest.mark.parametrize("status", [125, 126, 127])
def test_a_search_that_cannot_run_fails(tmp_path, status):
    root = tmp_path / "rootfs"
    root.mkdir()
    fake_bin = _fake_bin(tmp_path, {"timeout": f"exit {status}"})
    lines, failures = _call("check_world_writable_dirs", str(root),
                            path=f"{fake_bin}:/usr/bin:/bin")
    assert failures == 1, lines
    assert lines[0] == (f"FAIL the search of the root filesystem {root} for "
                        f"world-writable directories could not run "
                        f"(exit {status})")


def test_a_search_that_finishes_counts_no_failure(tmp_path):
    """find exits 1 for directories the agent cannot read: not a failure."""
    root = tmp_path / "rootfs"
    root.mkdir()
    fake_bin = _fake_bin(tmp_path, {"find": "exit 1"})
    lines, failures = _call("check_world_writable_dirs", str(root),
                            path=f"{fake_bin}:/usr/bin:/bin")
    assert (lines, failures) == ([
        f"PASS agent cannot write any of 0 world-writable director(y/ies) "
        f"on the root filesystem {root}"], 0)


@pytest.mark.parametrize("value", ["0", "abc", "-5", "1.5", "007"])
def test_a_bad_scan_bound_fails_and_keeps_the_default(tmp_path, value):
    root = tmp_path / "rootfs"
    root.mkdir()
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", str(root),
                                  "--scan-seconds", value)
    default = re.search(r"^WORLD_WRITABLE_SCAN_SECONDS=([0-9]+)$",
                        VERIFY_SCRIPT.read_text(encoding="utf-8"),
                        re.MULTILINE).group(1)
    assert (f"FAIL --scan-seconds '{value}' is not a positive whole number "
            f"(the default {default} s applies)") in lines
    assert lines[-1].startswith("RESULT: FAIL")


def test_the_default_bound_leaves_a_real_host_time_to_search():
    """The default is long enough for a large real root filesystem."""
    match = re.search(r"^WORLD_WRITABLE_SCAN_SECONDS=([0-9]+)$",
                      VERIFY_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) >= 300
