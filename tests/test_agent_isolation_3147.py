"""Task 3147 (ISO-5): isolation runbook firewall, behavioural tests, private tmp.

Copyright 2026 Forgeborn

Follow-ups of the independent review of task 3142 (indep-3142) and of
SECURITY-REVIEW-3142. Each test fails on main before the change, except the
N2 tests, which pin guards main already has: they fail once the guard is
mutated away (M5: the CLI startup refusal, M18: the verify script's group
check), which the source-text tests they replace did not.

* N1 / R3142-01: the verify flow checks that the agent user's nftables
  table is loaded and probes, as the agent, its own listener on every
  address of this host (whatever its range) and every listening port there.
* R3142-02: LAN targets are probed as the agent, after the orchestrator
  showed it can reach them itself.
* F8 / R3142-04: the unit's TMPDIR is its own and off /, and a shared /tmp
  or /var/tmp on / fails the check.
* N1, N3, N4: the runbook's firewall, IO and MCP-install instructions.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import agent_launcher, isolation

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"
RUNBOOK = REPO_ROOT / "docs" / "AGENT_ISOLATION.md"


def _settings(tmp_path: Path, **overrides) -> isolation.IsolationSettings:
    exchange = tmp_path / "exchange"
    exchange.mkdir(exist_ok=True)
    section = {"exchange_dir": str(exchange), "python": sys.executable,
               **overrides}
    return isolation.load_isolation_settings({"agent_isolation": section})


def _call_script_function(function: str, *args: str,
                          env: dict[str, str] | None = None
                          ) -> tuple[list[str], int]:
    """Source the verify script (it defines its functions and runs
    nothing), call one check function and return its output lines and the
    failures it counted."""
    result = subprocess.run(
        ["bash", "-c",
         'source "$1"; shift; failures=0; "$@"; echo "failures=$failures"',
         "verify-test", str(VERIFY_SCRIPT), function, *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})})
    lines = result.stdout.splitlines()
    assert lines and lines[-1].startswith("failures="), (result.stdout,
                                                          result.stderr)
    return lines[:-1], int(lines[-1].partition("=")[2])


def _listener(address: str = "127.0.0.1") -> socket.socket:
    listener = socket.socket()
    listener.bind((address, 0))
    listener.listen(8)
    return listener


def _closed_port() -> int:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()                                   # nothing listens there
    return port


def _fake_python(tmp_path: Path, output: str = "", status: int = 0) -> dict:
    """PATH with a python3 that records its arguments and prints
    ``output``: the probe as a firewall that answers nothing would."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    (tmp_path / "probe-output").write_text(output)
    python = fake_bin / "python3"
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'probe-args'}\n"
                      f"cat {tmp_path / 'probe-output'}\nexit {status}\n")
    python.chmod(0o755)
    return {"PATH": f"{fake_bin}:{os.environ['PATH']}"}


def _probe_pairs(tmp_path: Path) -> list[tuple[str, str]]:
    """The address/port pairs the fake python3 was asked to probe."""
    args = (tmp_path / "probe-args").read_text().splitlines()
    pairs = args[args.index("3") + 1:]                # after -I -c CODE 3
    return list(zip(pairs[0::2], pairs[1::2]))


# --- N2: the CLI refuses at startup, before any mode runs (mutation M5) -------------------


@pytest.fixture
def host(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """The operator's marker (absent until a test creates it) and the host
    dispatch config (absent)."""
    from equipa import config

    state = SimpleNamespace(marker=tmp_path / "etc" / "require-agent-isolation",
                            config=tmp_path / "host" / "dispatch_config.json")
    state.marker.parent.mkdir()
    state.config.parent.mkdir()
    monkeypatch.setattr(isolation, "REQUIRED_MARKER", state.marker)
    monkeypatch.setattr(config, "host_dispatch_config_path",
                        lambda: state.config)
    monkeypatch.chdir(tmp_path)
    return state


@pytest.fixture
def cli_run(host, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """``cli.async_main`` with a per-run config that turns isolation off and
    a mode handler that only records that it was selected."""
    from equipa import cli

    per_run = tmp_path / "per-run.json"
    per_run.write_text('{"features": {"agent_isolation": false}}')
    state = SimpleNamespace(handlers=[])

    def select_handler(args):
        state.handlers.append(args)

        async def handler(args):
            return None
        return handler

    monkeypatch.setattr(sys, "argv",
                        ["forge_orchestrator.py", "--auto-run", "--dry-run",
                         "--dispatch-config", str(per_run)])
    monkeypatch.setattr(cli, "_select_mode_handler", select_handler)
    monkeypatch.setattr(cli, "set_active_dispatch_config", lambda config: None)
    monkeypatch.setattr(cli, "pin_global_git_config", lambda: None)
    state.run = lambda: asyncio.run(cli.async_main())
    return state


def test_cli_refuses_an_isolation_off_config_before_any_mode_runs(
        host, cli_run, capsys) -> None:
    from equipa.dispatch import DispatchRefused

    host.marker.write_text("")
    with pytest.raises(DispatchRefused):
        cli_run.run()
    assert cli_run.handlers == []
    assert "ERROR: " in capsys.readouterr().out


def test_cli_without_the_marker_reaches_the_mode_handler(cli_run) -> None:
    """The control: the same config on a host that does not require
    isolation is not refused, so the refusal above is the marker's."""
    cli_run.run()
    assert len(cli_run.handlers) == 1


# --- N2: the verify script's group check, fed fake group lists (mutation M18) -------------


@pytest.mark.parametrize("group", ["root", "sudo", "admin", "wheel", "adm",
                                   "docker", "lxd", "incus", "libvirt", "kvm",
                                   "disk", "shadow", "systemd-journal"])
def test_verify_group_check_fails_on_each_privileged_group(group: str) -> None:
    lines, failures = _call_script_function(
        "check_privileged_groups", "equipa-agent", "users", group)
    assert lines == [f"FAIL agent user is in the privileged group {group}"]
    assert failures == 1


def test_verify_group_check_counts_every_privileged_group() -> None:
    lines, failures = _call_script_function(
        "check_privileged_groups", "equipa-agent", "docker", "lxd")
    assert failures == 2
    assert "FAIL agent user is in the privileged group docker" in lines
    assert "FAIL agent user is in the privileged group lxd" in lines


@pytest.mark.parametrize("groups", [["equipa-agent"],
                                    ["equipa-agent", "users"],
                                    ["equipa-agent", "dockerx", "sudoers",
                                     "docker-users", "admins"]])
def test_verify_group_check_passes_unprivileged_groups(groups) -> None:
    lines, failures = _call_script_function("check_privileged_groups", *groups)
    assert failures == 0
    assert lines == [f"PASS agent user is in no privileged group "
                     f"(groups: {' '.join(groups)})"]


def test_verify_inside_checks_the_groups_id_reports(tmp_path: Path) -> None:
    """inside() feeds the agent's real group list (``id -nG``) to the check."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_id = subprocess.run(["sh", "-c", "command -v id"], capture_output=True,
                             text=True, check=True).stdout.strip()
    tools = {"id": f'[ "$1" = "-nG" ] && {{ echo "equipa-agent docker"; '
                   f'exit 0; }}\nexec {real_id} "$@"',
             "sudo": "exit 1",
             "crontab": "echo 'not allowed' >&2; exit 1",
             "at": "echo 'not allowed' >&2; exit 1",
             "loginctl": "echo no"}
    for tool, body in tools.items():
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT), "--inside"], capture_output=True,
        text=True, timeout=120,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})
    lines = result.stdout.splitlines()
    assert "FAIL agent user is in the privileged group docker" in lines
    assert lines[-1].startswith("RESULT: FAIL")


def test_sourcing_the_verify_script_runs_no_check() -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1" --inside; echo sourced', "verify-test",
         str(VERIFY_SCRIPT)],
        capture_output=True, text=True, timeout=60)
    assert result.stdout.splitlines() == ["sourced"]


# --- N1: the agent user's nftables table is loaded ----------------------------------------


def _fake_sudo(tmp_path: Path, status: int) -> str:
    sudo = tmp_path / "fake-sudo"
    sudo.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'sudo-args'}\n"
                    f"exit {status}\n")
    sudo.chmod(0o755)
    return str(sudo)


def test_a_loaded_firewall_table_passes(tmp_path: Path) -> None:
    settings = _settings(tmp_path, sudo=_fake_sudo(tmp_path, 0))
    assert isolation.firewall_table_failures(settings) == []
    assert (tmp_path / "sudo-args").read_text().split() == \
        ["-n", "/usr/sbin/nft", "list", "table", "inet", "equipa_agent"]


@pytest.mark.parametrize("status", [1, 127])
def test_a_missing_or_unlistable_firewall_table_fails(tmp_path: Path,
                                                      status: int) -> None:
    settings = _settings(tmp_path, sudo=_fake_sudo(tmp_path, status))
    failures = isolation.firewall_table_failures(settings)
    assert len(failures) == 1
    assert "the nftables table inet equipa_agent is not loaded" in failures[0]
    assert f"exited {status}" in failures[0]
    assert "step 4a" in failures[0]


def test_a_missing_sudo_fails_the_firewall_check(tmp_path: Path) -> None:
    settings = _settings(tmp_path, sudo=str(tmp_path / "no-sudo"))
    assert isolation.firewall_table_failures(settings)


def _narrow_rule_listing(settings) -> tuple[str, int]:
    command = f"{settings.python} -I {settings.launcher} --isolated"
    return ("    Defaults!EQUIPA_AGENT_LAUNCH !use_pty, !pam_session, "
            "env_reset, !log_output\n\n"
            "Sudoers entry: /etc/sudoers.d/equipa-agent\n"
            f"    RunAsUsers: {settings.agent_user}\n"
            "    Options: !authenticate\n"
            "    Commands:\n"
            f"\t{command}\n", 0)


def test_outer_checks_fail_without_the_firewall_table(
        tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, secret_scan_roots=[str(tmp_path)])
    monkeypatch.setattr(isolation, "THEFORGE_DB", tmp_path / "missing.db")
    monkeypatch.setattr(isolation, "MCP_CONFIG", tmp_path / "no-mcp.json")
    monkeypatch.setattr(isolation, "sudoers_listing", _narrow_rule_listing)
    monkeypatch.setattr(isolation, "_exit_status",
                        lambda argv: 1 if isolation.NFT_EXECUTABLE in argv
                        else 0)
    failures = isolation._outer_checks(settings)
    assert len(failures) == 1
    assert "the nftables table inet equipa_agent is not loaded" in failures[0]
    monkeypatch.setattr(isolation, "_exit_status", lambda argv: 0)
    assert isolation._outer_checks(settings) == []


# --- N1 / R3142-01: every host address, whatever its range, is probed ---------------------


def test_probe_command_lists_every_host_address(tmp_path: Path,
                                                monkeypatch) -> None:
    addresses = ["127.0.0.1", "192.168.1.21", "100.101.102.103",
                 "203.0.113.9", "::1", "2001:db8::5"]
    monkeypatch.setattr(agent_launcher, "_local_addresses", lambda: addresses)
    monkeypatch.setattr(isolation, "listening_loopback_ports", lambda: [])
    command = isolation.build_probe_command(
        str(VERIFY_SCRIPT), _settings(tmp_path), [], str(tmp_path))
    listed = [command[index + 1] for index, arg in enumerate(command)
              if arg == "--host-address"]
    assert listed == addresses


def test_verify_probes_own_listeners_ports_and_lan_targets(
        tmp_path: Path) -> None:
    """Every host address gets an own-listener probe (port 0), every port
    is tried at 127.0.0.1 and at each host address, and LAN targets are
    split into address and port (IPv6 in brackets)."""
    env = _fake_python(tmp_path)
    lines, failures = _call_script_function(
        "check_network", "127.0.0.1 100.101.102.103", "5432 8080",
        "192.0.2.10:445 [2001:db8::7]:22", env=env)
    assert failures == 0, lines
    assert set(_probe_pairs(tmp_path)) == {
        ("127.0.0.1", "0"), ("100.101.102.103", "0"),
        ("127.0.0.1", "5432"), ("127.0.0.1", "8080"),
        ("100.101.102.103", "5432"), ("100.101.102.103", "8080"),
        ("192.0.2.10", "445"), ("2001:db8::7", "22")}
    assert lines == [
        "PASS agent cannot connect to its own listener on any of 2 host "
        "address(es): 127.0.0.1 100.101.102.103",
        "PASS agent cannot connect to 127.0.0.1 on any of 2 local service "
        "port(s)",
        "PASS agent cannot connect to 100.101.102.103 on any of 2 local "
        "service port(s)",
        "PASS agent cannot connect to any of 2 LAN target(s)"]


def test_verify_fails_when_its_own_listener_on_a_host_address_answers(
        tmp_path: Path) -> None:
    """Without a firewall (as here) a listener on a local address is
    reachable: exactly what the nftables rule's fib daddr type local must
    stop for the agent user."""
    lines, failures = _call_script_function("check_network", "127.0.0.1",
                                            "", "")
    assert failures == 1
    assert lines[0].startswith("FAIL agent can connect to its own listener on "
                               "the host address(es) 127.0.0.1 ")


def test_verify_probes_ports_at_every_host_address(tmp_path: Path) -> None:
    """A service bound to one non-loopback local address is found there,
    not only at 127.0.0.1."""
    listener = _listener("127.0.0.2")
    port = listener.getsockname()[1]
    env = _fake_python(tmp_path, output=f"REACHED 127.0.0.2 {port}\n")
    try:
        lines, failures = _call_script_function(
            "check_network", "127.0.0.2", str(port), "", env=env)
        real_lines, real_failures = _call_script_function(
            "check_network", "127.0.0.2", str(port), "")
    finally:
        listener.close()
    assert ("FAIL agent can connect to 127.0.0.2 on port(s) "
            f"{port} (not blocked for the agent user)") in lines
    assert ("PASS agent cannot connect to 127.0.0.1 on any of 1 local "
            "service port(s)") in lines
    assert failures == 1
    # The real probe finds the same service (and its own listener).
    assert any(line.startswith(f"FAIL agent can connect to 127.0.0.2 on "
                               f"port(s) {port}") for line in real_lines)
    assert real_failures == 2


def test_verify_fails_when_a_lan_target_answers(tmp_path: Path) -> None:
    listener = _listener()
    port = listener.getsockname()[1]
    closed = _closed_port()
    try:
        lines, failures = _call_script_function(
            "check_network", "", "", f"127.0.0.1:{port} 127.0.0.1:{closed}",
            env=_fake_python(tmp_path, output=f"REACHED 127.0.0.1 {port}\n"))
        real_lines, _real = _call_script_function(
            "check_network", "", "", f"127.0.0.1:{port} 127.0.0.1:{closed}")
    finally:
        listener.close()
    expected = (f"FAIL agent can connect to the LAN target(s) 127.0.0.1:{port} "
                f"(not blocked for the agent user)")
    assert expected in lines
    assert expected in real_lines
    assert failures == 2           # the LAN target, and no host address given


def test_verify_without_host_addresses_fails(tmp_path: Path) -> None:
    lines, failures = _call_script_function(
        "check_network", "", "", "", env=_fake_python(tmp_path))
    assert failures == 1
    assert lines[0].startswith("FAIL no host address to probe")


def test_verify_notes_when_no_lan_target_is_listed(tmp_path: Path) -> None:
    lines, failures = _call_script_function(
        "check_network", "127.0.0.1", "", "", env=_fake_python(tmp_path))
    assert failures == 0
    assert any(line.startswith("NOTE no --lan-target listed") for line in lines)


@pytest.mark.parametrize("output, status, message", [
    ("ERROR 100.101.102.103 0 Cannot assign requested address\n", 0,
     "FAIL could not probe 100.101.102.103 port 0: Cannot assign requested "
     "address"),
    ("", 3, "FAIL the network probe could not run (python3 exited 3)"),
])
def test_verify_fails_when_the_probe_cannot_run(
        tmp_path: Path, output: str, status: int, message: str) -> None:
    lines, failures = _call_script_function(
        "check_network", "100.101.102.103", "", "",
        env=_fake_python(tmp_path, output=output, status=status))
    assert message in lines
    assert failures >= 1


def test_the_real_probe_batches_many_targets(tmp_path: Path) -> None:
    """Hundreds of targets run in bounded batches; a reachable one deep in
    the list is still found and the closed ones are not reported."""
    listener = _listener()
    port = listener.getsockname()[1]
    closed = _closed_port()
    targets = ["127.0.0.1", str(closed)] * 450
    targets[2 * 430 + 1] = str(port)
    script = 'source "$1"; shift; tcp_probe 3 "$@"'
    try:
        result = subprocess.run(
            ["bash", "-c", script, "verify-test", str(VERIFY_SCRIPT), *targets],
            capture_output=True, text=True, timeout=60)
    finally:
        listener.close()
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"REACHED 127.0.0.1 {port}"]


# --- R3142-02: LAN targets ----------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("192.0.2.10:445", ("192.0.2.10", 445)),
    ("[2001:db8::7]:22", ("2001:db8::7", 22)),
    ("[192.0.2.10]:80", ("192.0.2.10", 80)),
])
def test_lan_targets_parse(text: str, expected: tuple[str, int]) -> None:
    assert isolation._lan_target(text) == expected


@pytest.mark.parametrize("text", ["nas.local:445", "192.0.2.10", "192.0.2.10:",
                                  "192.0.2.10:0", "192.0.2.10:70000",
                                  "2001:db8::7:22", ":445", "[2001:db8::7]"])
def test_bad_lan_targets_are_rejected(text: str) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        isolation._lan_target(text)


def test_lan_target_control_needs_the_orchestrator_to_reach_it() -> None:
    listener = _listener()
    port = listener.getsockname()[1]
    closed = _closed_port()
    try:
        failures = isolation.lan_target_control_failures(
            [("127.0.0.1", port), ("127.0.0.1", closed)])
    finally:
        listener.close()
    assert failures == [
        f"the LAN target 127.0.0.1:{closed} does not answer the orchestrator "
        f"either, so the agent's probe of it proves nothing; list a LAN "
        f"service the orchestrator can reach"]


def test_verification_main_probes_lan_targets_after_the_control(
        tmp_path: Path, monkeypatch, capsys) -> None:
    seen: dict = {}
    monkeypatch.setattr(isolation, "get_active_dispatch_config", lambda: {
        "agent_isolation": {"exchange_dir": str(tmp_path)}})
    monkeypatch.setattr(isolation, "_outer_checks", lambda settings: [])
    monkeypatch.setattr(isolation, "build_probe_command",
                        lambda *args, **kwargs: ["claude", "--inside"])

    async def fake_probe(command, config):
        seen["command"] = command
        return "RESULT: PASS\n"

    monkeypatch.setattr(isolation, "_run_probe", fake_probe)
    argv = ["--verify-probe", str(VERIFY_SCRIPT), "--lan-target",
            "192.0.2.10:445", "--lan-target", "[2001:db8::7]:22"]
    monkeypatch.setattr(isolation, "tcp_reachable",
                        lambda targets, timeout=3.0: set(targets))
    assert isolation.verification_main(argv) == 0
    assert seen["command"] == ["claude", "--inside",
                               "--lan-target", "192.0.2.10:445",
                               "--lan-target", "[2001:db8::7]:22"]
    monkeypatch.setattr(isolation, "tcp_reachable",
                        lambda targets, timeout=3.0: set())
    assert isolation.verification_main(argv) == 1
    assert "FAIL the LAN target 192.0.2.10:445 does not answer" in \
        capsys.readouterr().out


# --- F8 / R3142-04: a private TMPDIR off /, no shared /tmp on / ---------------------------


def test_a_writable_shared_tmp_on_the_root_filesystem_fails(
        tmp_path: Path) -> None:
    shared = tmp_path / "tmp"
    shared.mkdir()
    lines, failures = _call_script_function("check_shared_tmp", str(tmp_path),
                                            str(shared))
    assert failures == 1
    assert lines[0].startswith(f"FAIL agent can write {shared} on the root "
                               f"filesystem {tmp_path} ")


def test_a_shared_tmp_on_another_filesystem_passes(tmp_path: Path) -> None:
    shared = tmp_path / "tmp"
    shared.mkdir()
    lines, failures = _call_script_function("check_shared_tmp", "/proc",
                                            str(shared))
    assert (lines, failures) == (
        [f"PASS {shared} is on a filesystem other than /proc"], 0)


def test_a_shared_tmp_closed_to_the_agent_passes(tmp_path: Path) -> None:
    shared = tmp_path / "tmp"
    shared.mkdir(mode=0o555)
    try:
        lines, failures = _call_script_function("check_shared_tmp",
                                                str(tmp_path), str(shared))
        writable = os.access(shared, os.W_OK)        # root ignores the mode
    finally:
        shared.chmod(0o755)
    assert failures == (1 if writable else 0)
    if not writable:
        assert lines == [f"PASS agent cannot write {shared}"]


def test_a_missing_shared_tmp_is_not_reported(tmp_path: Path) -> None:
    assert _call_script_function("check_shared_tmp", str(tmp_path),
                                 str(tmp_path / "absent")) == ([], 0)


def _unit_tmpdir(tmp_path: Path, mode: int = 0o700) -> Path:
    directory = tmp_path / ".equipa-agent" / "equipa-agent-1-2-ab" / "tmp"
    directory.mkdir(parents=True)
    directory.chmod(mode)
    return directory


def test_unit_tmpdir_off_the_root_filesystem_passes(tmp_path: Path) -> None:
    directory = _unit_tmpdir(tmp_path)
    lines, failures = _call_script_function(
        "check_unit_tmpdir", "/proc", env={"TMPDIR": str(directory)})
    assert (lines, failures) == (
        ["PASS agent TMPDIR is the unit's own, on a filesystem other than "
         "/proc"], 0)


def test_unit_tmpdir_on_the_root_filesystem_fails(tmp_path: Path) -> None:
    directory = _unit_tmpdir(tmp_path)
    lines, failures = _call_script_function(
        "check_unit_tmpdir", str(tmp_path), env={"TMPDIR": str(directory)})
    assert failures == 1
    assert lines[0].startswith(f"FAIL agent TMPDIR {directory} is on the root "
                               f"filesystem {tmp_path}")


def test_unit_tmpdir_open_to_others_fails(tmp_path: Path) -> None:
    directory = _unit_tmpdir(tmp_path, mode=0o755)
    lines, failures = _call_script_function(
        "check_unit_tmpdir", "/proc", env={"TMPDIR": str(directory)})
    assert (lines, failures) == (
        [f"FAIL agent TMPDIR {directory} is not a 0700 directory of the "
         f"agent user"], 1)


@pytest.mark.parametrize("value", ["", "/tmp", "/var/tmp/equipa-agent-1/tmp"])
def test_a_tmpdir_that_is_not_the_units_own_fails(value: str) -> None:
    lines, failures = _call_script_function("check_unit_tmpdir", "/proc",
                                            env={"TMPDIR": value})
    assert (lines, failures) == (
        [f"FAIL agent TMPDIR '{value}' is not the unit's own"], 1)


# --- N1, N3, N4: the runbook (documentation fences) ---------------------------------------


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _heredoc(target: str) -> str:
    """The body of the runbook's ``cat > target <<'EOF'`` block."""
    text = _runbook()
    opening = f"cat > {target} <<'EOF'\n"
    start = text.index(opening) + len(opening)
    return text[start:text.index("\nEOF\n", start)]


def _statements(body: str) -> list[str]:
    """Non-empty lines of an nft body, comments dropped, a set continued
    over several lines joined into one statement."""
    statements: list[str] = []
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if statements and statements[-1].count("{") > statements[-1].count("}") \
                and not statements[-1].endswith("{"):
            statements[-1] += " " + line
        else:
            statements.append(line)
    return statements


def _chain(statements: list[str], name: str) -> list[str]:
    start = statements.index(f"chain {name} {{") + 1
    return statements[start:statements.index("}", start)]


RULE_FILE = "/etc/nftables.d/equipa-agent.nft"
UNIT_FILE = "/etc/systemd/system/equipa-agent-firewall.service"


def test_runbook_rule_file_replaces_only_its_own_table() -> None:
    """Declare, delete, define: loading the file twice leaves one copy of
    the table, and nothing outside it is touched (no flush ruleset)."""
    statements = _statements(_heredoc(RULE_FILE))
    table = " ".join(isolation.NFT_TABLE)
    assert statements[:3] == [f"table {table}", f"delete table {table}",
                              f"table {table} {{"]
    assert not any("flush" in statement for statement in statements)
    assert sum(statement.startswith("delete ") for statement in statements) == 1
    assert "install -d -o root -g root -m 0755 /etc/nftables.d" in _runbook()


def test_runbook_loads_the_rule_with_its_own_unit() -> None:
    unit = _heredoc(UNIT_FILE).splitlines()
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert f"ExecStart={isolation.NFT_EXECUTABLE} -f {RULE_FILE}" in unit
    assert "WantedBy=multi-user.target" in unit
    assert not any(line.startswith("ExecStop") for line in unit)
    after = next(line for line in unit if line.startswith("After="))
    assert {"docker.service", "tailscaled.service"} <= set(after[6:].split())
    text = _runbook()
    assert "systemctl enable --now equipa-agent-firewall.service" in text
    # The stock service's config flushes the whole ruleset (N1).
    assert "systemctl enable nftables" not in text
    assert 'include "/etc/nftables.d' not in text


def test_runbook_rule_filters_only_the_agent_users_sockets() -> None:
    """A positive match: packets without an owning socket (kernel RSTs,
    ICMP errors, neighbour discovery) never reach the reject rules."""
    statements = _statements(_heredoc(RULE_FILE))
    assert _chain(statements, "output") == [
        "type filter hook output priority 0; policy accept;",
        'meta skuid "equipa-agent" jump agent']
    assert not any("skuid !=" in statement for statement in statements)


def test_runbook_rule_rejects_every_host_address_and_the_denied_ranges() -> None:
    import ipaddress

    agent = _chain(_statements(_heredoc(RULE_FILE)), "agent")
    dns = [index for index, statement in enumerate(agent)
           if statement.endswith("dport 53 accept")]
    local = agent.index("fib daddr type local reject")
    ranges = [index for index, statement in enumerate(agent)
              if re.match(r"ip6? daddr \{", statement)]
    assert dns and ranges and max(dns) < local < min(ranges)
    assert all(statement.endswith(" reject") for statement in agent[local:])
    networks = {ipaddress.ip_network(cidr.strip())
                for index in ranges
                for cidr in agent[index].split("{", 1)[1].split("}")[0].split(",")}
    expected = {ipaddress.ip_network(cidr) for cidr in
                (*isolation.DEFAULT_IP_ADDRESS_DENY, "100.64.0.0/10")}
    assert networks == expected


def _step7_config() -> dict:
    import json

    text = _runbook()
    start = text.index('"features": { "agent_isolation": true },')
    return json.loads("{" + text[start:text.index("\n```", start)] + "}")


def test_runbook_recommends_a_null_io_weight_where_weights_do_nothing(
        tmp_path: Path) -> None:
    text = _runbook()
    words = " ".join(text.split())
    for phrase in ("BFQ", "iocost", '"io_weight": null', "[mq-deadline]",
                   "leave `io` out of `Delegate=`"):
        assert phrase in words, phrase
    section = {**_step7_config()["agent_isolation"],
               "exchange_dir": str(tmp_path)}
    settings = isolation.load_isolation_settings({"agent_isolation": section})
    assert settings.io_weight is None
    assert "100.64.0.0/10" in settings.ip_address_deny
    # N3: systemd-run of systemd 255 takes the symbolic names; the ranges
    # are spelled out for the launcher, which parses each one.
    source = (REPO_ROOT / "equipa" / "isolation.py").read_text(encoding="utf-8")
    assert "does not parse the symbolic names" not in source
    assert "rejects the symbolic names" not in text


def test_runbook_pins_the_mcp_server_with_hashes() -> None:
    text = _runbook()
    assert "'mcp-server-sqlite==2025.4.25' 'mcp[cli]==1.30.0'" in text
    hashes = dict(re.findall(r"^([0-9a-f]{64})  (\S+\.whl)$", text, re.MULTILINE))
    assert set(hashes.values()) == {
        "mcp_server_sqlite-2025.4.25-py3-none-any.whl",
        "mcp-1.30.0-py3-none-any.whl"}
    assert "--require-hashes --no-deps -r requirements.lock" in text
    assert "--no-index --find-links ." in text
    assert "pip-audit --disable-pip --require-hashes -r requirements.lock" in text
    assert text.index("pip-audit") < text.index("--no-index --find-links .")
    assert "pip install mcp-server-sqlite" not in text
