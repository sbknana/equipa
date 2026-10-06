"""Task #3189 (IR87-02): a caller's include cannot slip past the hardening
override refusal.

Task #3187 refused a caller ``-c`` pair that gives a pinned key another
value, but git reads an included file where its ``include.path`` stands,
after EQUIPA's pins: ``git_run(["-c", "include.path=<file setting
core.hooksPath>", "commit", ...])`` ran the caller's pre-commit hook. A
``--config-env`` pair was checked against the variable as the caller asked
for it, while git read it after the hardening rewrote it
(``core.hooksPath=GIT_NO_REPLACE_OBJECTS`` became ``1`` and ran
``<repo>/1/pre-commit``). Outside a non-git dispatch nothing refused a
``trailer.<token>.cmd`` either.

The runners now refuse, before git starts, any caller pair (``-c``,
``--config-env``, ``GIT_CONFIG_*``) that includes a file or names a trailer
command, and any ``--config-env`` pair for a pinned key.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from equipa import git_ops
from equipa.git_ops import git_run, git_run_async

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
REFUSED = "includes a config file or names a command"
OVERRIDE = "would override EQUIPA's hardening pin"


class _StartRecorder:
    """Stands in for subprocess.run in git_ops: records each start."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.starts: list[list[str]] = []
        monkeypatch.setattr(git_ops.subprocess, "run", self._run)

    def _run(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.starts.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True,
                   capture_output=True)
    return repository


def _hook(directory: Path, marker: Path) -> None:
    """A pre-commit hook in ``directory`` that writes ``marker``."""
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)


def _file_setting_hooks(tmp_path: Path, marker: Path) -> Path:
    """A config file outside the repository whose ``core.hooksPath`` holds a
    hook that writes ``marker``."""
    hooks = tmp_path / "hooks"
    _hook(hooks, marker)
    included = tmp_path / "hooks.cfg"
    included.write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    return included


def _commit(repository: Path, *global_options: str,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return git_run([*global_options, *IDENTITY, "commit", "--allow-empty",
                    "-q", "-m", "x"], repository, env=env)


# --- real git: the reviewer's shapes ------------------------------------------


def test_an_included_hooks_path_never_runs_a_hook(
    repository: Path, tmp_path: Path,
) -> None:
    """The reviewer's probe: rc 0 and the hook ran on 3187."""
    marker = tmp_path / "hook-ran"
    included = _file_setting_hooks(tmp_path, marker)

    result = _commit(repository, "-c", f"include.path={included}")

    assert not marker.exists()
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert REFUSED in result.stderr


def test_a_conditional_include_never_runs_a_hook(
    repository: Path, tmp_path: Path,
) -> None:
    marker = tmp_path / "hook-ran"
    included = _file_setting_hooks(tmp_path, marker)

    result = _commit(repository, "-c",
                     f"includeIf.gitdir:{repository}/.path={included}")

    assert not marker.exists()
    assert REFUSED in result.stderr


@pytest.mark.parametrize("shape", ["count", "parameters"])
def test_an_include_in_the_config_variables_never_runs_a_hook(
    repository: Path, tmp_path: Path, shape: str,
) -> None:
    marker = tmp_path / "hook-ran"
    included = _file_setting_hooks(tmp_path, marker)
    env = ({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path",
            "GIT_CONFIG_VALUE_0": str(included)} if shape == "count"
           else {"GIT_CONFIG_PARAMETERS": f"'include.path={included}'"})

    result = _commit(repository, env=env)

    assert not marker.exists()
    assert REFUSED in result.stderr


def test_a_config_env_pair_read_after_the_hardening_never_runs_a_hook(
    repository: Path, tmp_path: Path,
) -> None:
    """The reviewer's ``--config-env`` probe: the check saw the pinned
    ``/dev/null``, git saw the hardening's ``1`` and ran
    ``<repo>/1/pre-commit``."""
    marker = tmp_path / "hook-ran"
    _hook(repository / "1", marker)

    result = _commit(repository, "--config-env",
                     "core.hooksPath=GIT_NO_REPLACE_OBJECTS",
                     env={"GIT_NO_REPLACE_OBJECTS": "/dev/null"})

    assert not marker.exists()
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert OVERRIDE in result.stderr


def test_a_trailer_command_never_runs_outside_a_non_git_dispatch(
    repository: Path, tmp_path: Path,
) -> None:
    """3187 refused it only while a project dispatched without git was
    recorded; ``interpret-trailers --trailer s=x`` ran it otherwise."""
    marker = tmp_path / "trailer-ran"
    message = tmp_path / "message.txt"
    message.write_text("subject\n", encoding="utf-8")

    result = git_run(["-c", f"trailer.s.cmd=touch '{marker}' #",
                      "interpret-trailers", "--trailer", "s=x", str(message)],
                     repository)

    assert not marker.exists()
    assert REFUSED in result.stderr


# --- every refused shape, before git starts -----------------------------------

REFUSED_CALLS: dict[str, tuple[list[str], dict[str, str]]] = {
    "include.path": (["-c", "include.path=/tmp/x.cfg", "status"], {}),
    "include-case": (["-c", "INCLUDE.Path=/tmp/x.cfg", "status"], {}),
    "include-no-value": (["-c", "include.path", "status"], {}),
    "includeIf": (["-c", "includeIf.gitdir:/.path=/tmp/x.cfg", "status"], {}),
    "includeIf-dotted-condition": (
        ["-c", "includeIf.gitdir:/a.b/.path=/tmp/x.cfg", "status"], {}),
    "includeif-case": (["-c", "includeif.onbranch:main.path=/tmp/x.cfg",
                        "status"], {}),
    "include-after-other-options": (
        ["-C", "/tmp", "--no-replace-objects", "-c", "include.path=/tmp/x.cfg",
         "status"], {}),
    "trailer.cmd": (["-c", "trailer.s.cmd=touch /tmp/x", "interpret-trailers"], {}),
    "trailer.command": (["-c", "trailer.s.command=touch /tmp/x",
                         "interpret-trailers"], {}),
    "trailer-case": (["-c", "Trailer.Sign.CMD=touch /tmp/x", "commit"], {}),
    "config-env-include": (["--config-env=include.path=INCLUDE_3189", "status"],
                           {"INCLUDE_3189": "/tmp/x.cfg"}),
    "config-env-include-two-tokens": (["--config-env", "include.path=INCLUDE_3189",
                                       "status"], {"INCLUDE_3189": "/tmp/x.cfg"}),
    "config-env-pinned-value": (["--config-env=core.hooksPath=HOOKS_3189",
                                 "commit"], {"HOOKS_3189": "/dev/null"}),
    "config-env-ext-pinned-value": (["--config-env",
                                     "protocol.ext.allow=EXT_3189", "fetch"],
                                    {"EXT_3189": "never"}),
    "config-env-hardened-variable": (
        ["--config-env=core.hooksPath=GIT_NO_REPLACE_OBJECTS", "commit"],
        {"GIT_NO_REPLACE_OBJECTS": "/dev/null"}),
    "GIT_CONFIG_COUNT-include": (["status"], {
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "t", "GIT_CONFIG_KEY_1": "include.path",
        "GIT_CONFIG_VALUE_1": "/tmp/x.cfg"}),
    "GIT_CONFIG_COUNT-includeIf": (["status"], {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "includeIf.gitdir:/.path",
        "GIT_CONFIG_VALUE_0": "/tmp/x.cfg"}),
    "GIT_CONFIG_COUNT-trailer": (["commit"], {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "trailer.s.cmd",
        "GIT_CONFIG_VALUE_0": "touch /tmp/x"}),
    "GIT_CONFIG_PARAMETERS-include": (["status"], {
        "GIT_CONFIG_PARAMETERS": "'user.name=t' 'include.path=/tmp/x.cfg'"}),
}


@pytest.mark.parametrize("shape", sorted(REFUSED_CALLS))
def test_a_refused_pair_never_starts_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    args, env = REFUSED_CALLS[shape]
    recorder = _StartRecorder(monkeypatch)

    result = git_run(args, tmp_path, env=env or None)

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert "equipa: refused to run git" in result.stderr


@pytest.mark.parametrize("shape", ["include.path", "config-env-pinned-value",
                                   "trailer.cmd", "GIT_CONFIG_COUNT-include"])
def test_the_async_runner_refuses_them_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    args, env = REFUSED_CALLS[shape]
    recorder = _StartRecorder(monkeypatch)

    result = asyncio.run(git_run_async(args, tmp_path, env=env or None))

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert "equipa: refused to run git" in result.stderr


# --- controls: what still runs ------------------------------------------------


@pytest.mark.parametrize("args", [
    ["-c", "core.hooksPath=/dev/null", "rev-parse", "--is-inside-work-tree"],
    ["-c", "trailer.sign.key=Signed-off-by", "rev-parse", "--is-inside-work-tree"],
    ["-c", "trailer.cmd=x", "rev-parse", "--is-inside-work-tree"],
    ["-c", "user.includes=x", "rev-parse", "--is-inside-work-tree"],
    ["--config-env=user.name=NAME_3189", "rev-parse", "--is-inside-work-tree"],
])
def test_pairs_that_include_nothing_still_run(
    repository: Path, monkeypatch: pytest.MonkeyPatch, args: list[str],
) -> None:
    """A pinned value repeated by ``-c``, a trailer setting that is not a
    command, a key merely named like one, and ``--config-env`` for a key
    EQUIPA does not pin run real git as before."""
    result = git_run(args, repository, env={"NAME_3189": "t"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"


def test_a_commit_without_caller_pairs_runs_no_hook(
    repository: Path, tmp_path: Path,
) -> None:
    """Control for the real-git cases: the repository's own hook directory
    is pinned off, and a plain commit succeeds."""
    marker = tmp_path / "hook-ran"
    _hook(repository / ".git" / "hooks", marker)

    result = _commit(repository)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("key, refused", [
    ("include.path", True), ("INCLUDE.PATH", True), ("include.anything", True),
    ("includeIf.gitdir:/x/.path", True), ("includeif.x.y.path", True),
    ("trailer.s.cmd", True), ("trailer.s.command", True),
    ("trailer.a.b.cmd", True), ("trailer.s.key", False), ("trailer.cmd", False),
    ("core.hooksPath", False), ("user.include", False), ("includes.path", False),
])
def test_which_keys_are_refused_whatever_their_value(key: str, refused: bool) -> None:
    assert git_ops._refused_caller_config_key(key) is refused
