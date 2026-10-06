#!/usr/bin/env python3
"""Task 3153: no file-based settings source for any Claude CLI run.

R3150-01  The per-run CLAUDE_CONFIG_DIR was loaded as the USER scope
          (``--setting-sources user``). The directory is writable by the run,
          and the CLI re-read a settings.json written into it mid-run, so one
          Write tool call planted a hook the Bash gate never saw. Every CLI
          invocation now passes ``--setting-sources ""``: the empty value, as
          its own argv element. EQUIPA's ``--settings`` file is a flag source
          and still wires the PreToolUse gate.
R3150-07  In an isolated unit the config dir is ``<unit home>/.claude``,
          owned by the agent user. The launcher refuses a claude argv that
          would load any settings file, so the empty value is enforced inside
          the unit as well.
R3150-08  Per-run directories a crashed process left behind are swept (only
          EQUIPA-prefixed, real directories owned by this user, older than a
          day), and the suite keeps every directory it creates in a session
          temp directory that is removed at the end.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_launcher, agent_runner, cli_isolation
from equipa.cli_isolation import (
    CLAUDE_CLI_ISOLATION_ARGS,
    RUN_CONFIG_DIR_PREFIX,
    has_claude_cli_isolation,
    isolate_claude_argv,
    sweep_stale_run_config_dirs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_ON = {"features": {"bash_security_pretooluse": True}}
EXPECTED_ISOLATION = ("--setting-sources", "", "--strict-mcp-config")


@pytest.fixture(autouse=True)
def _empty_dispatch_config(monkeypatch):
    """Pin an empty active dispatch config (never the operator's file)."""
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


def _setting_sources_value(argv: list[str]) -> str:
    """The value after the single ``--setting-sources`` option."""
    assert argv.count("--setting-sources") == 1, argv
    assert not any(arg.startswith("--setting-sources=") for arg in argv), argv
    return argv[argv.index("--setting-sources") + 1]


# --- R3150-01: the argv ------------------------------------------------------------


def test_isolation_args_pass_the_empty_value_as_its_own_element():
    assert CLAUDE_CLI_ISOLATION_ARGS == EXPECTED_ISOLATION
    assert cli_isolation.ALLOWED_SETTING_SOURCES == ""


@pytest.mark.parametrize("streaming", [False, True])
def test_agent_argv_loads_no_settings_file_and_keeps_the_gate(streaming):
    with agent_runner.build_cli_command(
            "PROMPT", project_dir="/tmp", max_turns=5, model="sonnet",
            role="developer", streaming=streaming,
            dispatch_config=GATE_ON) as cmd:
        argv = list(cmd)
        assert _setting_sources_value(argv) == ""
        assert "user" not in argv
        assert has_claude_cli_isolation(argv)
        # The gate arrives through EQUIPA's own --settings file (flag
        # source), which the empty setting-sources value does not switch off.
        settings_path = argv[argv.index("--settings") + 1]
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    entry = payload["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    assert "pretooluse_bash_gate.py" in entry["hooks"][0]["command"]
    assert payload["disableAllHooks"] is False
    if "--" in argv:
        assert argv.index("--settings") < argv.index("--")
        assert argv.index("--setting-sources") < argv.index("--")


@pytest.mark.parametrize("cmd", [
    ["claude", "-p", "x"],
    ["claude", "-p", "--output-format", "json", "--", "hi"],
])
def test_isolate_claude_argv_adds_the_empty_value(cmd):
    isolated = isolate_claude_argv(cmd)
    assert _setting_sources_value(isolated) == ""
    assert has_claude_cli_isolation(isolated)


@pytest.mark.parametrize("value", ["user", "project", "local", "user,project",
                                   " ", ","])
def test_any_settings_source_is_refused(value):
    for cmd in (["claude", "--setting-sources", value, "-p", "x"],
                ["claude", f"--setting-sources={value}", "-p", "x"]):
        assert not has_claude_cli_isolation([*cmd, "--strict-mcp-config"])
        with pytest.raises(ValueError, match="setting sources"):
            isolate_claude_argv(cmd)


def test_a_flag_without_a_value_is_refused_not_read_as_empty():
    cmd = ["claude", "-p", "x", "--strict-mcp-config", "--setting-sources"]
    assert not has_claude_cli_isolation(cmd)
    with pytest.raises(ValueError):
        isolate_claude_argv(cmd)
    # "--" ends the options, so the flag before it has no value either.
    with pytest.raises(ValueError):
        isolate_claude_argv(["claude", "--setting-sources", "--", "x"])


def test_the_ssh_mutation_command_keeps_the_empty_value_in_the_shell_words():
    spec = importlib.util.spec_from_file_location(
        "autoresearch_3153", REPO_ROOT / "scripts" / "autoresearch_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    words = shlex.split(module.CLAUDE_MUTATE_COMMAND)
    assert words[words.index("--setting-sources") + 1] == ""


@pytest.mark.parametrize("script", ["autoresearch_loop.py",
                                    "forgesmith_simba.py"])
def test_standalone_fallbacks_pass_the_empty_value(script, monkeypatch):
    """With equipa not importable, the scripts' own copy of the flags is
    used: it must be the same tuple (a separate empty element)."""
    for name in ("equipa.cli_isolation", "equipa.isolation", "equipa.config"):
        monkeypatch.setitem(sys.modules, name, None)
    spec = importlib.util.spec_from_file_location(
        f"fallback_{script[:-3]}_3153", REPO_ROOT / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module.CLAUDE_CLI_ISOLATION_ARGS) == EXPECTED_ISOLATION


def test_docs_describe_the_empty_value():
    orchestrator = (REPO_ROOT / "docs" / "ORCHESTRATOR.md").read_text(
        encoding="utf-8")
    isolation_doc = (REPO_ROOT / "docs" / "AGENT_ISOLATION.md").read_text(
        encoding="utf-8")
    for text in (orchestrator, isolation_doc):
        assert '--setting-sources ""' in text
        assert "--setting-sources user" not in text
    # IR3150-A: the docs no longer claim isolation closes in-run config
    # directory writes.
    assert "isolation closes both" not in orchestrator


# --- R3150-01: the operator's live check gets a mid-run probe ----------------------


def _verify_script(cli_source: str, tmp_path: Path, *args: str):
    from tests.test_cli_config_isolation_3150 import _run_verify_script_with

    return _run_verify_script_with(cli_source, tmp_path, *args)


def _simulated_cli() -> str:
    from tests.test_cli_config_isolation_3150 import SIMULATED_CLI

    assert "HONOR_SOURCES = True" in SIMULATED_CLI
    return SIMULATED_CLI


def test_midrun_settings_write_probe_passes_when_the_cli_loads_no_settings(
        tmp_path):
    """The simulated CLI re-reads a mid-run settings.json only when the user
    source is loaded; EQUIPA's argv loads none, so W PASSes."""
    result = _verify_script(_simulated_cli(), tmp_path)
    out = result.stdout
    assert "[PASS] probe W_midrun_settings_write" in out, out + result.stderr
    assert "shell snapshot in the per-run config directory is not probed" in out
    assert result.returncode == 0, out + result.stderr


def test_midrun_settings_write_probe_fails_when_the_hook_is_hot_loaded(
        tmp_path):
    """A CLI that loads the file written mid-run runs the planted hook: W
    must FAIL and the whole check must fail."""
    hot_loading = _simulated_cli().replace("HONOR_SOURCES = True",
                                           "HONOR_SOURCES = False")
    result = _verify_script(hot_loading, tmp_path)
    assert "[FAIL] probe W_midrun_settings_write" in result.stdout, (
        result.stdout + result.stderr)
    assert "RESULT: FAIL" in result.stdout
    assert result.returncode == 1


def test_midrun_probe_needs_the_refusal_of_the_blocked_command_itself(
        tmp_path):
    """An error result of another call (here the Write) is not evidence that
    the gate refused the blocked command: W must not PASS on it."""
    other_error = _simulated_cli().replace(
        "WRITE_IS_ERROR, BLOCKED_IS_ERROR = False, True",
        "WRITE_IS_ERROR, BLOCKED_IS_ERROR = True, False")
    assert other_error != _simulated_cli()
    result = _verify_script(other_error, tmp_path)
    assert "[INCONCLUSIVE] probe W_midrun_settings_write" in result.stdout, (
        result.stdout + result.stderr)
    assert "RESULT: PASS" not in result.stdout
    assert result.returncode == 3


def test_midrun_probe_without_the_write_is_inconclusive_not_pass(tmp_path):
    """No settings.json written mid-run proves nothing about R3150-01."""
    never_writes = _simulated_cli().replace("Write tool to create the file",
                                            "never matches this prompt")
    result = _verify_script(never_writes, tmp_path)
    assert "[INCONCLUSIVE] probe W_midrun_settings_write" in result.stdout, (
        result.stdout + result.stderr)
    assert "RESULT: PASS" not in result.stdout
    assert result.returncode == 3


# --- R3150-07: the isolated unit ---------------------------------------------------


def _unit_header(argv: list[str], executable: str = sys.executable) -> dict:
    unit = "equipa-agent-1-1-0123456789abcdef"
    return {
        "unit": unit, "argv": argv, "executable": executable,
        "env": {"PATH": os.environ["PATH"]}, "files": [],
        "workdir_sources": [],
        "identity": {"user": "equipa-agent", "orchestrator_uid": os.getuid(),
                     "privileged_groups": []},
        "cgroup": {"path": f"/app.slice/{unit}.scope", "pids_max": 64,
                   "memory_max": 256 * 1024 ** 2, "cpu_weight": 100},
        "deny_read": [], "deny_write": [], "must_execute": [], "must_read": [],
        "git": {"executable": "/usr/bin/git", "hardening_args": [],
                "hardening_env": {}},
        "workspace": None, "grace": 1.0,
    }


@pytest.mark.parametrize("argv", [
    ["claude", "-p", "x", "--strict-mcp-config"],
    ["claude", "-p", "x", "--setting-sources", "user", "--strict-mcp-config"],
    ["claude", "--setting-sources=project", "-p", "x"],
    ["claude", "--setting-sources", "", "--setting-sources", "user"],
    ["claude", "-p", "x", "--setting-sources"],
    ["claude", "-p", "--", "--setting-sources", ""],
])
def test_unit_refuses_a_claude_argv_that_loads_settings_files(argv):
    """Inside the unit the config dir is the agent's own: a settings.json
    written there would be read unless the CLI loads no settings file."""
    session = agent_launcher._IsolatedSession(_unit_header(argv))
    with pytest.raises(agent_launcher.IsolationRefused,
                       match="--setting-sources"):
        session.materialize_argv()


def test_unit_starts_the_argv_the_orchestrator_builds():
    argv = isolate_claude_argv(["claude", "-p", "x"])
    session = agent_launcher._IsolatedSession(_unit_header(argv))
    assert session.materialize_argv() == argv


def test_unit_judges_a_claude_executable_whatever_argv0_says():
    assert agent_launcher.setting_sources_refusal(
        "/opt/bin/claude", ["agent", "-p", "x"]) is not None
    assert agent_launcher.setting_sources_refusal(
        "/opt/versions/2.1.280", ["claude", "-p", "x"]) is not None
    assert agent_launcher.setting_sources_refusal(
        "/bin/sh", ["sh", "-c", "true"]) is None


def test_unit_config_dir_settings_are_ignored_is_documented():
    text = (REPO_ROOT / "docs" / "AGENT_ISOLATION.md").read_text(
        encoding="utf-8")
    assert "R3150-07" in text


# --- R3150-08: stale per-run directories -------------------------------------------


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


DAY = 24 * 3600


def test_sweep_removes_only_old_owned_prefixed_directories(tmp_path):
    stale = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}stale"
    (stale / "shell-snapshots").mkdir(parents=True)
    (stale / ".claude.json").write_text("{}")
    _age(stale / "shell-snapshots", DAY + 60)
    _age(stale / ".claude.json", DAY + 60)
    _age(stale, DAY + 60)
    fresh = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}fresh"
    fresh.mkdir()
    live = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}live"
    (live / "projects").mkdir(parents=True)
    _age(live, DAY + 60)  # the CLI is still writing below it
    unrelated = tmp_path / "someone-else-old"
    unrelated.mkdir()
    _age(unrelated, DAY + 60)
    old_file = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}file"
    old_file.write_text("x")
    _age(old_file, DAY + 60)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}link"
    link.symlink_to(target)
    _age(link, DAY + 60)

    removed = sweep_stale_run_config_dirs(str(tmp_path))

    assert removed == [str(stale)]
    assert not stale.exists()
    for kept in (fresh, live, unrelated, old_file, link):
        assert os.path.lexists(kept), kept
    assert (target / "keep.txt").read_text() == "keep"


def test_sweep_leaves_another_users_directory(tmp_path, monkeypatch):
    stale = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}foreign"
    stale.mkdir()
    _age(stale, DAY + 60)
    monkeypatch.setattr(cli_isolation.os, "getuid", lambda: os.geteuid() + 1)

    assert sweep_stale_run_config_dirs(str(tmp_path)) == []
    assert stale.exists()


def test_sweep_age_is_configurable_and_must_be_positive(tmp_path):
    young = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}young"
    young.mkdir()
    _age(young, 120)
    assert sweep_stale_run_config_dirs(str(tmp_path)) == []
    assert sweep_stale_run_config_dirs(str(tmp_path), max_age_seconds=60) == [
        str(young)]
    with pytest.raises(ValueError):
        sweep_stale_run_config_dirs(str(tmp_path), max_age_seconds=0)


def test_first_run_dir_in_a_parent_sweeps_it_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_isolation, "_swept_parents", set())
    stale = tmp_path / f"{RUN_CONFIG_DIR_PREFIX}crashed"
    stale.mkdir()
    _age(stale, DAY + 60)

    first = cli_isolation.create_run_config_dir(str(tmp_path))
    assert not stale.exists()

    stale.mkdir()
    _age(stale, DAY + 60)
    second = cli_isolation.create_run_config_dir(str(tmp_path))
    assert stale.exists(), "the sweep runs once per parent and process"
    for path in (first, second):
        assert cli_isolation.remove_run_config_dir(path) == []


def test_the_suite_keeps_its_temp_files_in_a_session_directory():
    """conftest points TMPDIR (subprocesses) and tempfile (this process) at
    a private directory it removes at the end, so the config directories of
    SIGKILLed test orchestrators never reach the shared temp directory."""
    # The module pytest loaded (tests/ is not a package). "from tests import
    # conftest" would execute the file a second time: a new session temp
    # directory and a new THEFORGE_DB for the rest of the run.
    import conftest

    session_tmp = str(conftest.SESSION_TMP)
    assert Path(session_tmp).name.startswith("eqt-")
    assert tempfile.gettempdir() == session_tmp
    assert os.environ["TMPDIR"] == session_tmp
    child = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        capture_output=True, text=True, check=True, timeout=30)
    assert child.stdout.strip() == session_tmp
    config_dir = cli_isolation.create_run_config_dir()
    try:
        assert os.path.dirname(config_dir) == session_tmp
    finally:
        cli_isolation.remove_run_config_dir(config_dir)


# Runs the real conftest module body in a fresh interpreter (TMPDIR is the
# test's own directory), plants a per-run config directory in the session
# directory, has a forked child die of SIGTERM, then sends SIGTERM to itself:
# what `timeout` does to a slow suite, which skips pytest_unconfigure.
_SIGTERMED_SESSION = r'''
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location("conftest_sigterm_probe",
                                              sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
session = module.SESSION_TMP
(session / "equipa-claude-config-probe").mkdir()
child = os.fork()
if child == 0:
    os.kill(os.getpid(), signal.SIGTERM)
    os._exit(0)
_, status = os.waitpid(child, 0)
child_died_of_sigterm = (os.WIFSIGNALED(status)
                         and os.WTERMSIG(status) == signal.SIGTERM)
print(session, child_died_of_sigterm, session.is_dir(), flush=True)
os.kill(os.getpid(), signal.SIGTERM)
signal.pause()
'''


def test_a_sigtermed_session_removes_its_temp_dir(tmp_path):
    """SIGTERM removes the session directory with the per-run config
    directories in it; a forked child's SIGTERM removes nothing; and a
    day-old session directory a SIGKILLed run left is swept at import."""
    stale = tmp_path / "eqt-sigkilled"
    (stale / "equipa-claude-config-leaked").mkdir(parents=True)
    _age(stale / "equipa-claude-config-leaked", DAY + 60)
    _age(stale, DAY + 60)
    env = {**os.environ, "TMPDIR": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, "-c", _SIGTERMED_SESSION,
         str(REPO_ROOT / "tests" / "conftest.py")],
        capture_output=True, text=True, env=env, timeout=60)

    assert result.returncode == -15, result.stdout + result.stderr
    session, child_died_of_sigterm, kept_after_child = (
        result.stdout.split())
    assert Path(session).parent == tmp_path
    assert child_died_of_sigterm == "True"
    assert kept_after_child == "True", "a forked child removed the session dir"
    assert not os.path.lexists(session), "SIGTERM left the session directory"
    assert not stale.exists(), "the day-old session directory was not swept"
    assert sorted(path.name for path in tmp_path.iterdir()) == []


def test_only_day_old_session_dirs_of_this_user_are_swept(tmp_path,
                                                          monkeypatch):
    import conftest

    old = tmp_path / "eqt-old"
    old.mkdir()
    _age(old, DAY + 60)
    fresh = tmp_path / "eqt-fresh"
    fresh.mkdir()
    still_written = tmp_path / "eqt-still-written"
    (still_written / "theforge-test.db").parent.mkdir()
    (still_written / "theforge-test.db").write_text("x")
    _age(still_written, DAY + 60)
    other_prefix = tmp_path / "equipa-claude-config-old"
    other_prefix.mkdir()
    _age(other_prefix, DAY + 60)
    old_file = tmp_path / "eqt-file"
    old_file.write_text("x")
    _age(old_file, DAY + 60)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "eqt-link"
    link.symlink_to(target)
    _age(link, DAY + 60)

    assert conftest._sweep_stale_session_dirs(str(tmp_path)) == [str(old)]
    for kept in (fresh, still_written, other_prefix, old_file, link):
        assert os.path.lexists(kept), kept
    assert (target / "keep.txt").read_text() == "keep"

    foreign = tmp_path / "eqt-foreign"
    foreign.mkdir()
    _age(foreign, DAY + 60)
    monkeypatch.setattr(conftest.os, "getuid", lambda: os.geteuid() + 1)
    assert conftest._sweep_stale_session_dirs(str(tmp_path)) == []
    assert foreign.exists()


def test_no_test_imports_conftest_under_a_second_module_name():
    """A second copy of conftest re-runs its module body mid-session: a new
    session temp directory and a new THEFORGE_DB for every later test."""
    second_copy = ("from tests import conftest", "import tests.conftest",
                   "from tests.conftest import")
    offenders = [
        path.name for path in sorted((REPO_ROOT / "tests").glob("*.py"))
        if any(line.strip().startswith(second_copy)
               for line in path.read_text(encoding="utf-8").splitlines())
    ]
    assert offenders == []
    assert "tests.conftest" not in sys.modules
