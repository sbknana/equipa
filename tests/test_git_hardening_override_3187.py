"""Task #3187 (IR83-03): a caller's own config pairs cannot override
EQUIPA's git hardening pins.

``_hardened_git_argv`` puts the pins (``core.hooksPath=/dev/null``,
``protocol.ext.allow=never``, ...) before the caller's arguments, and git
lets the last pair of a key win: ``git_run(["-c",
"protocol.ext.allow=always", "ls-remote", "ext::sh -c ..."])`` ran a shell
command, and ``-c core.hooksPath=<dir>`` ran that directory's hooks. The
runners now refuse a call whose ``-c`` / ``--config-env`` pairs, or whose
``GIT_CONFIG_*`` variables, give a pinned key another value.

While a project dispatched without git is recorded, the guard also reads an
``include.path`` / ``includeIf.<condition>.path`` pair as a wrapper it
cannot read, like an alias: the included file (outside the project) defined
``alias.st=!...`` and git ran it.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from equipa import git_ops
from equipa.git_ops import dispatched_without_git, git_run, git_run_async

OVERRIDE = "would override EQUIPA's hardening pin"

# Calls a caller could make that switch a pin off: (git args, extra env).
OVERRIDING_CALLS: dict[str, tuple[list[str], dict[str, str]]] = {
    "protocol.ext.allow": (["-c", "protocol.ext.allow=always", "status"], {}),
    "core.hooksPath": (["-c", "core.hooksPath=/tmp/hooks", "status"], {}),
    "key-case": (["-c", "CORE.HOOKSPATH=/tmp/hooks", "status"], {}),
    "core.pager": (["-c", "core.pager=less", "log"], {}),
    "core.editor-config-env": (["--config-env=core.editor=EDITOR_3187", "commit"],
                               {"EDITOR_3187": "vi"}),
    "config-env-two-tokens": (["--config-env", "core.editor=EDITOR_3187", "commit"],
                              {"EDITOR_3187": "vi"}),
    "commit.gpgSign": (["-c", "commit.gpgSign=true", "commit"], {}),
    "gpg.program": (["-c", "gpg.program=/tmp/evil", "log"], {}),
    "submodule.recurse": (["-C", "/tmp", "-c", "submodule.recurse=true", "checkout"], {}),
    "diff.relative": (["-c", "diff.relative=true", "diff"], {}),
    "core.fsmonitor": (["-c", "core.fsmonitor=/tmp/monitor", "status"], {}),
    "core.sshCommand": (["-c", "core.sshCommand=sh -c x", "fetch", "origin"], {}),
    "GIT_CONFIG_COUNT": (["status"], {"GIT_CONFIG_COUNT": "1",
                                      "GIT_CONFIG_KEY_0": "core.hooksPath",
                                      "GIT_CONFIG_VALUE_0": "/tmp/hooks"}),
    "GIT_CONFIG_PARAMETERS": (["status"],
                              {"GIT_CONFIG_PARAMETERS": "'protocol.ext.allow=always'"}),
}
# Calls including a config file outside the recorded project: the file can
# define an alias or a command key the non-git guard never reads.
INCLUDING_CALLS: dict[str, tuple[list[str], dict[str, str]]] = {
    "include.path": (["-c", "include.path={outside}", "status"], {}),
    "includeIf": (["-c", "includeIf.gitdir:/.path={outside}", "status"], {}),
    "environment": (["status"], {"GIT_CONFIG_COUNT": "1",
                                 "GIT_CONFIG_KEY_0": "include.path",
                                 "GIT_CONFIG_VALUE_0": "{outside}"}),
}
INCLUDE_REFUSED = "includes a config file"


class _StartRecorder:
    """Stands in for subprocess.run in git_ops: records each start."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.starts: list[list[str]] = []
        monkeypatch.setattr(git_ops.subprocess, "run", self._run)

    def _run(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.starts.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.mark.parametrize("shape", sorted(OVERRIDING_CALLS))
def test_a_pair_overriding_a_pin_is_refused_before_git_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    args, env = OVERRIDING_CALLS[shape]
    recorder = _StartRecorder(monkeypatch)

    result = git_run(args, tmp_path, env=env or None)

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert OVERRIDE in result.stderr


def _project_and_elsewhere(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return project, elsewhere


def _with_outside(call: tuple[list[str], dict[str, str]], outside: Path
                  ) -> tuple[list[str], dict[str, str]]:
    args, env = call
    return ([arg.format(outside=outside) for arg in args],
            {key: value.format(outside=outside) for key, value in env.items()})


@pytest.mark.parametrize("shape", sorted(INCLUDING_CALLS))
def test_an_included_config_file_is_unreadable_to_the_non_git_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    args, env = _with_outside(INCLUDING_CALLS[shape], tmp_path / "outside.cfg")
    recorder = _StartRecorder(monkeypatch)

    with dispatched_without_git(project):
        result = git_run(args, elsewhere, env=env or None)

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert INCLUDE_REFUSED in result.stderr


def test_an_alias_in_an_included_file_never_runs_during_a_non_git_dispatch(
    tmp_path: Path,
) -> None:
    """The reviewer's real-git shape: a file outside the project defines
    ``alias.st=!...``; ``-c include.path=<file> st`` ran it (base)."""
    project, elsewhere = _project_and_elsewhere(tmp_path)
    marker = tmp_path / "alias-ran"
    included = tmp_path / "outside.cfg"
    included.write_text(f"[alias]\n\tst = !touch '{marker}'\n", encoding="utf-8")

    with dispatched_without_git(project):
        result = git_run(["-c", f"include.path={included}", "st"], elsewhere)

    assert not marker.exists()
    assert INCLUDE_REFUSED in result.stderr


def test_an_included_file_still_reads_while_no_project_is_recorded(
    repository: Path, tmp_path: Path,
) -> None:
    """Control: outside a non-git dispatch an include is read as before."""
    included = tmp_path / "plain.cfg"
    included.write_text("[equipa3187]\n\tvalue = included\n", encoding="utf-8")

    result = git_run(["-c", f"include.path={included}", "config",
                      "equipa3187.value"], repository)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "included"


def test_the_async_runner_refuses_the_same_calls(tmp_path: Path) -> None:
    args, env = OVERRIDING_CALLS["protocol.ext.allow"]

    result = asyncio.run(git_run_async(args, tmp_path, env=env))

    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert OVERRIDE in result.stderr


def test_unreadable_config_variables_are_refused(tmp_path: Path) -> None:
    result = git_run(["status"], tmp_path, env={"GIT_CONFIG_COUNT": "many"})

    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert "cannot be checked against EQUIPA's hardening pins" in result.stderr


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True,
                   capture_output=True)
    return repository


@pytest.mark.parametrize("args", [
    ["-c", "user.name=EQUIPA", "rev-parse", "--is-inside-work-tree"],
    ["-c", "core.pager=cat", "rev-parse", "--is-inside-work-tree"],
    ["-c", f"core.attributesFile={os.devnull}", "rev-parse",
     "--is-inside-work-tree"],
    ["-c", "protocol.ext.allow=never", "rev-parse", "--is-inside-work-tree"],
])
def test_pairs_that_keep_every_pin_still_run(repository: Path, args: list[str]) -> None:
    """Control: EQUIPA's own pairs (``user.name``, ``core.attributesFile``)
    and a pair repeating a pinned value run real git."""
    result = git_run(args, repository)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


def test_a_hooks_directory_given_by_a_caller_never_runs(
    repository: Path, tmp_path: Path,
) -> None:
    """End to end with real git: the caller's hooks directory holds a
    pre-commit hook that writes a marker. Base ran it."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)

    result = git_run(["-c", f"core.hooksPath={hooks}", "-c", "user.name=t",
                      "-c", "user.email=t@example.invalid", "commit",
                      "--allow-empty", "-q", "-m", "x"], repository)

    assert not marker.exists()
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert OVERRIDE in result.stderr


def test_an_ext_transport_enabled_by_a_caller_never_runs(
    repository: Path, tmp_path: Path,
) -> None:
    """End to end with real git: ``ext::`` runs its command line once a
    caller's pair allows it after the pin. Base ran it."""
    marker = tmp_path / "ext-ran"

    result = git_run(["-c", "protocol.ext.allow=always", "ls-remote",
                      f"ext::sh -c touch% {marker}"], repository, timeout=30)

    assert not marker.exists()
    assert result.returncode == git_ops._REFUSED_RETURNCODE


def test_every_pinned_key_is_known_to_the_refusal() -> None:
    pins = git_ops._hardening_config_pins({})
    for key in ("core.hookspath", "protocol.ext.allow", "core.pager",
                "core.editor", "sequence.editor", "commit.gpgsign",
                "gpg.program", "submodule.recurse", "diff.relative",
                "core.fsmonitor", "core.sshcommand", "core.askpass"):
        assert key in pins, key
    # The later pin wins, as in git: _GIT_SAFE_CONFIG's empty hooksPath is
    # superseded by /dev/null.
    assert pins["core.hookspath"] == "/dev/null"
