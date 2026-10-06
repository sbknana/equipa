#!/usr/bin/env python3
"""Task 3172: follow-ups of the verify script's --inside options (task 3169).

* R3169-02: the world-writable search bound (600 s) could never fire on the
  only real-host caller, the operator check, which stops reading the probe
  after 120 s; the operator got a traceback instead of the script's
  "did not finish" line. The default is now 90 s (see
  tests/test_agent_isolation_3169.py, which pins it below the caller's
  read timeout), and a search cut short at the default bound is reported
  within the caller's time.
* R3169-03: ``--root-fs`` was spliced into a bash pattern substitution, so
  with bash 5.2 an "&" in the root expanded to the matched text and the
  well-known directories (``/tmp/.X11-unix`` below a search-only ``/tmp``)
  were read at a path that does not exist and skipped. They are now joined
  to the root one by one, and a root that is not an absolute path of an
  existing directory fails (the default / applies).

``find`` and the host-reading tools are fakes on PATH, so nothing here
searches the host.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from tests.test_agent_isolation_3169 import (
    _HANGING_FIND,
    _QUIET_TOOLS,
    _RECORDING_FIND,
    VERIFY_SCRIPT,
    _call,
    _fake_bin,
    _run_inside,
    probe_read_timeout_seconds,
)


def _default_bound() -> int:
    return int(re.search(r"^WORLD_WRITABLE_SCAN_SECONDS=([0-9]+)$",
                         VERIFY_SCRIPT.read_text(encoding="utf-8"),
                         re.MULTILINE).group(1))


@pytest.mark.parametrize("name", ["root&fs", "a&&b", "x&"])
def test_a_root_holding_an_ampersand_still_probes_the_well_known_directories(
        tmp_path, name):
    root = tmp_path / name
    (root / "tmp" / ".X11-unix").mkdir(parents=True)
    (root / "var" / "tmp").mkdir(parents=True)
    # The search finds nothing, so only the well-known list names them.
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", str(root))
    assert any(line.startswith(f"FAIL agent can write the world-writable "
                               f"directory {root}/tmp/.X11-unix on the root "
                               f"filesystem {root} (") for line in lines), lines
    assert any(line.startswith(f"FAIL agent can write {root}/var/tmp on the "
                               f"root filesystem {root}") for line in lines)
    assert not any("--root-fs" in line for line in lines), lines


def test_the_well_known_directories_are_joined_to_the_root_one_by_one(
        tmp_path):
    """Called with a root holding "&", each well-known name is probed below
    that root exactly (a writable one is reported at its own path)."""
    root = tmp_path / "r&oot"
    for name in ("tmp/.X11-unix", "var/crash"):
        (root / name).mkdir(parents=True)
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", f"{root}/")
    for name in ("tmp/.X11-unix", "var/crash"):
        assert any(line.startswith(f"FAIL agent can write the world-writable "
                                   f"directory {root}/{name} on the root "
                                   f"filesystem {root}/ (")
                   for line in lines), (name, lines)


@pytest.mark.parametrize("value", ["relative/root", "", "/nonexistent-3172",
                                   "FILE"])
def test_a_root_that_is_not_an_existing_absolute_directory_fails(tmp_path,
                                                                 value):
    if value == "FILE":
        value = str(tmp_path / "a-file")
        Path(value).write_text("not a directory\n")
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", value,
                                  "--scan-seconds", "5")
    assert (f"FAIL --root-fs '{value}' is not an absolute path of an "
            f"existing directory (the default / applies)") in lines
    calls = (fake_bin / "find.calls").read_text().splitlines()
    assert "/ -xdev -type d -perm -0002 -print0" in calls
    assert lines[-1].startswith("RESULT: FAIL")


def test_a_valid_root_is_not_reported(tmp_path):
    root = tmp_path / "rootfs"
    root.mkdir()
    fake_bin = _fake_bin(tmp_path, {**_QUIET_TOOLS, "find": _RECORDING_FIND})
    lines, _seconds = _run_inside(fake_bin, "--root-fs", str(root))
    assert not any("--root-fs" in line for line in lines), lines


def test_a_search_cut_short_at_the_default_bound_reports_before_the_caller_stops(
        tmp_path):
    """The default bound, as the operator check runs it (no
    --scan-seconds), reports the cut-short search within the seconds that
    caller reads. The bound is shortened by a fake ``timeout`` that runs
    the search for 1 s and then exits as timeout(1) does when it kills it,
    after checking the bound it was given is the script's default."""
    root = tmp_path / "rootfs"
    root.mkdir()
    default = _default_bound()
    fake_timeout = (
        f'[ "$1" = "--kill-after=5" ] && [ "$2" = "{default}" ] || exit 99\n'
        'shift 2\n'
        '"$@" &\n'
        'child=$!\n'
        'sleep 1\n'
        'kill "$child" 2>/dev/null\n'
        'exit 124'
    )
    fake_bin = _fake_bin(tmp_path, {"find": _HANGING_FIND,
                                    "timeout": fake_timeout})
    started = time.monotonic()
    lines, failures = _call("check_world_writable_dirs", str(root),
                            path=f"{fake_bin}:/usr/bin:/bin")
    assert failures == 1, lines
    assert lines[0] == (f"FAIL the search of the root filesystem {root} for "
                        f"world-writable directories did not finish within "
                        f"{default} s: the directories it did not reach were "
                        f"not probed")
    assert time.monotonic() - started < 25
    assert default + 5 < probe_read_timeout_seconds()
