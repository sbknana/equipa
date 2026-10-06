"""Task #3183: follow-ups to the non-git project guard (IR78-04).

* IR80-02: a process runner started with no ``env`` used to guard on an
  empty mapping, while its child got ``_get_repo_env()``, whose
  ``GIT_CONFIG_GLOBAL`` and ``GIT_SSH_COMMAND`` reach git. The guard now
  reads the very environment the child is given.
* IR80-03: config keys and variables whose value is a command line
  (``core.sshCommand``, ``diff.external``, ``GIT_SSH_COMMAND`` ...) can carry
  ``cd P && git ...`` like an alias, and are refused like one, except for a
  value that runs nothing or EQUIPA's own hardening pin.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from equipa import git_ops
from equipa.git_ops import dispatched_without_git, git_run

COMMAND_LINE = "carries a command line"


@dataclass
class _Start:
    argv: list[str]
    env: dict[str, str]


class _StartRecorder:
    """Stands in for subprocess.run in git_ops: records each start."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.starts: list[_Start] = []
        monkeypatch.setattr(git_ops.subprocess, "run", self._run)

    def _run(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.starts.append(_Start(list(argv), dict(kwargs.get("env") or {})))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _project_and_elsewhere(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return project, elsewhere


# --- IR80-02 ----------------------------------------------------------------------

# Variables _get_repo_env() hands a child from the orchestrator's environment
# that name a location in, or a command line into, the recorded project.
INHERITED_INTO_THE_PROJECT = {
    "GIT_CONFIG_GLOBAL": "{project}/.git/config",
    "GIT_SSH_COMMAND": "sh -c 'cd {project} && git status' #",
}


@pytest.mark.parametrize("variable", sorted(INHERITED_INTO_THE_PROJECT))
def test_a_runner_given_no_env_is_guarded_on_what_its_child_inherits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    monkeypatch.setenv(variable, INHERITED_INTO_THE_PROJECT[variable].format(project=project))
    assert variable in git_ops._get_repo_env()  # the child would get it
    recorder = _StartRecorder(monkeypatch)

    with dispatched_without_git(project):
        result = git_ops._run_with_env(["git", "status"], elsewhere, 30)

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert "not a git repository at dispatch" in result.stderr


@pytest.mark.parametrize("env", [None, {"LANG": "C"}])
def test_the_guard_reads_the_environment_the_child_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str] | None,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(elsewhere / "gitconfig"))
    read: list[dict[str, str]] = []
    real_read = git_ops._read_git_call

    def reading(argv: Any, cwd: Any, guard_env: Any) -> Any:
        read.append(dict(guard_env))
        return real_read(argv, cwd, guard_env)

    monkeypatch.setattr(git_ops, "_read_git_call", reading)
    recorder = _StartRecorder(monkeypatch)

    with dispatched_without_git(project):
        git_ops._run_with_env(["git", "status"], elsewhere, 30, env)

    assert [start.env for start in recorder.starts] == read
    assert len(read) == 1


# Set in the orchestrator's environment, never handed to git by the runners:
# git cannot be pointed at the project by them.
NOT_INHERITED = {
    "GIT_DIR": "{project}/.git",
    "GIT_WORK_TREE": "{project}",
    "GIT_COMMON_DIR": "{project}/.git",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.worktree",
    "GIT_CONFIG_VALUE_0": "{project}",
    "GIT_CONFIG_PARAMETERS": "'core.worktree'='{project}'",
}


@pytest.mark.parametrize("runner", ["git_run", "_run_with_env"])
def test_variables_the_child_is_not_given_never_reach_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str,
) -> None:
    """IR80-02 control: GIT_DIR / GIT_CONFIG_* in os.environ (the review's
    probe) are not in the child's environment, so they cannot point the git
    started elsewhere at the project; the guard and the child agree."""
    project, elsewhere = _project_and_elsewhere(tmp_path)
    for variable, value in NOT_INHERITED.items():
        monkeypatch.setenv(variable, value.format(project=project))
    recorder = _StartRecorder(monkeypatch)

    with dispatched_without_git(project):
        if runner == "git_run":
            git_run(["status"], elsewhere)
        else:
            git_ops._run_with_env(["git", "status"], elsewhere, 30)

    assert recorder.starts, "git was refused, so the probe proves nothing"
    for start in recorder.starts:
        assert set(start.env) & set(NOT_INHERITED) == set(), start.env


# --- IR80-03 ----------------------------------------------------------------------

WRAPPER = "sh -c 'cd {project} && git diff --stat' #"

# Calls carrying a command line, as (argv after "git", env).
COMMAND_CARRYING_CALLS: dict[str, tuple[list[str], dict[str, str]]] = {
    "core.sshCommand": (["-c", f"core.sshCommand={WRAPPER}", "fetch", "origin"], {}),
    "core.pager": (["-c", f"core.pager={WRAPPER}", "log"], {}),
    "core.editor": (["-c", f"core.editor={WRAPPER}", "commit"], {}),
    "core.editor-with-the-pager-pin-value": (["-c", "core.editor=cat", "commit"], {}),
    "sequence.editor": (["-c", f"sequence.editor={WRAPPER}", "rebase", "-i", "x"], {}),
    "core.askPass": (["-c", f"core.askPass={WRAPPER}", "fetch", "origin"], {}),
    "core.gitProxy": (["-c", f"core.gitProxy={WRAPPER}", "fetch", "origin"], {}),
    "core.fsmonitor-command": (["-c", f"core.fsmonitor={WRAPPER}", "status"], {}),
    "core.alternateRefsCommand":
        (["-c", f"core.alternateRefsCommand={WRAPPER}", "fetch", "origin"], {}),
    "diff.external": (["-c", f"diff.external={WRAPPER}", "diff"], {}),
    "credential.helper-shell": (["-c", f"credential.helper=!{WRAPPER}", "push"], {}),
    "credential.helper-chained":
        (["-c", "credential.helper=store; cd {project} && git status", "push"], {}),
    "credential.url.helper":
        (["-c", f"credential.https://example.com.helper=!{WRAPPER}", "push"], {}),
    "pager.log": (["-c", f"pager.log={WRAPPER}", "log"], {}),
    "diff.driver.command": (["-c", f"diff.planted.command={WRAPPER}", "diff"], {}),
    "diff.driver.textconv": (["-c", f"diff.planted.textconv={WRAPPER}", "diff"], {}),
    "filter.driver.smudge": (["-c", f"filter.planted.smudge={WRAPPER}", "checkout", "."], {}),
    "filter.driver.process": (["-c", f"filter.planted.process={WRAPPER}", "checkout", "."], {}),
    "merge.driver.driver": (["-c", f"merge.planted.driver={WRAPPER}", "merge", "x"], {}),
    "difftool.cmd": (["-c", f"difftool.planted.cmd={WRAPPER}", "difftool"], {}),
    "remote.uploadpack": (["-c", f"remote.origin.uploadpack={WRAPPER}", "fetch", "origin"], {}),
    "upper-case-key": (["-c", f"CORE.SSHCOMMAND={WRAPPER}", "fetch", "origin"], {}),
    "config-env": (["--config-env=core.pager=PLANTED_PAGER", "log"],
                   {"PLANTED_PAGER": WRAPPER}),
    "GIT_CONFIG_COUNT": (["status"], {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.external",
        "GIT_CONFIG_VALUE_0": WRAPPER}),
    "GIT_CONFIG_PARAMETERS": (["log"], {"GIT_CONFIG_PARAMETERS": f"'core.pager'='{WRAPPER}'"}),
    **{variable: (["status"], {variable: WRAPPER}) for variable in (
        "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_PAGER", "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR", "GIT_PROXY_COMMAND")},
}


def _formatted(call: tuple[list[str], dict[str, str]], project: Path
               ) -> tuple[list[str], dict[str, str]]:
    options, env = call
    return ([part.format(project=project) for part in options],
            {key: value.format(project=project) for key, value in env.items()})


@pytest.mark.parametrize("shape", sorted(COMMAND_CARRYING_CALLS))
def test_a_command_line_in_config_or_the_environment_is_refused(
    tmp_path: Path, shape: str,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    options, env = _formatted(COMMAND_CARRYING_CALLS[shape], project)

    with dispatched_without_git(project):
        refusal = git_ops._non_git_project_refusal(["git", *options], elsewhere, env)

    assert refusal is not None and COMMAND_LINE in refusal, refusal


@pytest.mark.parametrize("shape", ["core.sshCommand", "diff.external", "GIT_SSH_COMMAND"])
def test_git_run_starts_nothing_for_a_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    options, env = _formatted(COMMAND_CARRYING_CALLS[shape], project)
    recorder = _StartRecorder(monkeypatch)

    with dispatched_without_git(project):
        result = git_run(options, elsewhere, env=env or None)

    assert recorder.starts == []
    assert result.returncode == git_ops._REFUSED_RETURNCODE
    assert COMMAND_LINE in result.stderr


def test_a_command_line_is_allowed_while_no_project_is_recorded(tmp_path: Path) -> None:
    _, elsewhere = _project_and_elsewhere(tmp_path)
    options, env = _formatted(COMMAND_CARRYING_CALLS["core.sshCommand"], tmp_path)

    assert git_ops._non_git_project_refusal(["git", *options], elsewhere, env) is None


# Values with which a command key runs nothing.
ALLOWED_VALUES = {
    "fsmonitor-off": ["-c", "core.fsmonitor=false", "status"],
    "fsmonitor-empty": ["-c", "core.fsmonitor=", "status"],
    "fsmonitor-builtin": ["-c", "core.fsmonitor=true", "status"],
    "pager-of-a-command-off": ["-c", "pager.log=false", "log"],
    "credential-helper-reset": ["-c", "credential.helper=", "fetch", "origin"],
    "a-key-given-no-value": ["-c", "core.pager", "log"],
    "a-key-that-only-shares-a-prefix": ["-c", "core.pagerx=sh -c x", "status"],
    "a-non-command-key-of-a-command-section": ["-c", "diff.planted.binary=true", "diff"],
}


@pytest.mark.parametrize("shape", sorted(ALLOWED_VALUES))
def test_a_value_that_runs_nothing_is_allowed(tmp_path: Path, shape: str) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    with dispatched_without_git(project):
        assert git_ops._non_git_project_refusal(
            ["git", *ALLOWED_VALUES[shape]], elsewhere, {}) is None


@pytest.mark.parametrize("operator_env", [
    {},
    {"GIT_SSH": "/usr/bin/ssh"},
    {"GIT_SSH": "/opt/ssh tools/ssh", "SSH_ASKPASS": "/usr/bin/ksshaskpass"},
])
@pytest.mark.parametrize("args", [["status"], ["diff", "--stat"], ["fetch", "origin"]])
def test_equipas_own_hardened_call_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    operator_env: dict[str, str], args: list[str],
) -> None:
    """Control: the hardening pins core.pager, core.editor, sequence.editor,
    core.sshCommand and core.askPass itself; those values are not refused."""
    project, elsewhere = _project_and_elsewhere(tmp_path)
    for variable in ("GIT_SSH", "SSH_ASKPASS"):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in operator_env.items():
        monkeypatch.setenv(variable, value)
    env = git_ops._hardened_git_env(None, args)
    argv = git_ops._hardened_git_argv(args, env)
    assert any(part.startswith("core.sshCommand=") for part in argv)  # not vacuous

    with dispatched_without_git(project):
        assert git_ops._non_git_project_refusal(argv, elsewhere, env) is None


def test_another_ssh_command_than_the_operators_pin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, elsewhere = _project_and_elsewhere(tmp_path)
    monkeypatch.setenv("GIT_SSH", "/usr/bin/ssh")
    env = git_ops._hardened_git_env(None, ["fetch", "origin"])
    argv = git_ops._hardened_git_argv(["fetch", "origin"], env)
    planted = ["git", "-c", f"core.sshCommand={WRAPPER.format(project=project)}", *argv[1:]]

    with dispatched_without_git(project):
        refusal = git_ops._non_git_project_refusal(planted, elsewhere, env)

    assert refusal is not None and COMMAND_LINE in refusal


def test_real_git_still_runs_elsewhere_while_a_project_is_recorded(tmp_path: Path) -> None:
    """End to end: the hardened argv with its pins starts real git."""
    project, _ = _project_and_elsewhere(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True,
                   capture_output=True)

    with dispatched_without_git(project):
        result = git_run(["rev-parse", "--is-inside-work-tree"], repository)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"
