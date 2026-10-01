#!/usr/bin/env python3
"""Task 3153: follow-ups of the reviews of task 3147 (agent isolation runbook).

SR3147-01  The /tmp ACL was not recursive: 1777 directories below /tmp
           (/tmp/.X11-unix, ...), /var/crash and /dev/shm stayed writable to
           the agent, and the verify script probed only /tmp and /var/tmp.
           The agent unit is a user scope, which cannot take PrivateTmp=,
           so the runbook closes them on the host (u:equipa-agent:---), and
           the verify script creates a file in every world-writable
           directory on / and caps /dev/shm.
SR3147-02  The nftables rule left the IPv4 limited broadcast address open.
SR3147-03  Global IPv6 addresses (and publicly routed IPv4 subnets) of the
           LAN were not covered: operator-filled prefix sets now are.
IR3147-A   A DNATed connection (Docker's published ports) reached the rule
           with the container's address: "ct status dnat reject" first.
SR3147-04  The MCP install recipe went on after a failed hash check.

The shell checks are called by sourcing the verify script with fake
inputs; the rule is evaluated statement by statement by a small model of
the nft expressions it uses, so a reordering or a dropped line fails.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_agent_isolation_3147 import (
    RULE_FILE,
    _chain,
    _heredoc,
    _runbook,
    _statements,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"


def _call(function: str, *args: str, prefix: str = "",
          env: dict[str, str] | None = None) -> tuple[list[str], int]:
    """Source the verify script, run ``prefix`` (a ulimit, say), call one
    check function; return its output lines and the failures it counted."""
    result = subprocess.run(
        ["bash", "-c",
         f'source "$1"; shift; failures=0; {prefix} "$@"; '
         f'echo "failures=$failures"',
         "verify-test", str(VERIFY_SCRIPT), function, *args],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {})})
    lines = result.stdout.splitlines()
    assert lines and lines[-1].startswith("failures="), (result.stdout,
                                                          result.stderr)
    return lines[:-1], int(lines[-1].partition("=")[2])


def _running_as_root() -> bool:
    return os.geteuid() == 0


# --- SR3147-01: world-writable directories on / ----------------------------------


def test_a_writable_world_writable_dir_on_the_root_filesystem_fails(tmp_path):
    shared = tmp_path / "x11-like"
    shared.mkdir()
    shared.chmod(0o1777)
    lines, failures = _call("check_world_writable_dirs", str(tmp_path))
    assert failures == 1
    assert lines == [
        f"FAIL agent can write the world-writable directory {shared} on the "
        f"root filesystem {tmp_path} (it can fill it; close it with a "
        f"tmpfiles.d ACL u:equipa-agent:---, docs/AGENT_ISOLATION.md step 1)"]
    assert list(shared.iterdir()) == [], "the probe file was not removed"


def test_a_world_writable_dir_the_agent_cannot_write_passes(tmp_path):
    closed = tmp_path / "closed"
    closed.mkdir()
    closed.chmod(0o557)          # other may write, the owner (us) may not
    try:
        lines, failures = _call("check_world_writable_dirs", str(tmp_path))
    finally:
        closed.chmod(0o755)
    if _running_as_root():       # root ignores the mode bits
        assert failures == 1
    else:
        assert (lines, failures) == ([
            f"PASS agent cannot write any of 1 world-writable "
            f"director(y/ies) on the root filesystem {tmp_path}"], 0)


def test_a_dir_hidden_below_an_unlistable_parent_is_probed_by_name(tmp_path):
    """find(1) cannot list a parent the agent may only search (the --x /tmp
    of the 3147 runbook), so the well-known names are probed as well."""
    parent = tmp_path / "tmp"
    parent.mkdir()
    hidden = parent / ".X11-unix"
    hidden.mkdir()
    hidden.chmod(0o1777)
    parent.chmod(0o311)
    try:
        unnamed = _call("check_world_writable_dirs", str(tmp_path))
        named = _call("check_world_writable_dirs", str(tmp_path), str(hidden))
        note = _call("note_search_only_dirs", str(parent))
    finally:
        parent.chmod(0o755)
    if _running_as_root():
        return
    assert unnamed[1] == 0, "find cannot see below the unlistable parent"
    assert named[1] == 1
    assert named[0][0].startswith(
        f"FAIL agent can write the world-writable directory {hidden} ")
    assert note[0] == [
        f"NOTE agent may enter but not list {parent}: only well-known names "
        f"below it were probed (close it completely with u:equipa-agent:---, "
        f"docs/AGENT_ISOLATION.md step 1)"]


def test_a_closed_or_listable_dir_gets_no_search_only_note(tmp_path):
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o000)
    try:
        assert _call("note_search_only_dirs", str(open_dir), str(closed),
                     str(tmp_path / "absent")) == ([], 0)
    finally:
        closed.chmod(0o755)


def test_symlinks_and_other_filesystems_are_not_candidates(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    lines, failures = _call("check_world_writable_dirs", str(tmp_path),
                            str(link), "/proc")
    assert failures == 0
    assert lines == [f"PASS agent cannot write any of 0 world-writable "
                     f"director(y/ies) on the root filesystem {tmp_path}"]


def test_the_inside_run_calls_the_new_checks():
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("\ninside() {"):source.index("\n}\n",
                                                           source.index("\ninside() {"))]
    calls = [line.strip() for line in body.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert 'check_world_writable_dirs / "${WELL_KNOWN_WORLD_WRITABLE[@]}"' in calls
    assert "note_search_only_dirs /tmp /var/tmp" in calls
    assert 'check_shm_cap /dev/shm "$AGENT_SHM_CAP_MB"' in calls
    assert "check_broadcast 255.255.255.255" in calls
    known = re.search(r"WELL_KNOWN_WORLD_WRITABLE=\(([^)]*)\)", source).group(1)
    for directory in ("/tmp/.X11-unix", "/tmp/.ICE-unix", "/tmp/.XIM-unix",
                      "/tmp/.font-unix", "/var/crash"):
        assert directory in known.split()


# --- SR3147-01: the /dev/shm cap ---------------------------------------------------


def test_an_uncapped_shared_memory_dir_fails(tmp_path):
    lines, failures = _call("check_shm_cap", str(tmp_path), "1")
    assert failures == 1
    assert lines[0].startswith(f"FAIL agent wrote 17 MiB to {tmp_path}, "
                               f"beyond the 1 MiB cap")
    assert list(tmp_path.iterdir()) == [], "the probe file was not removed"


def test_writes_stopped_by_the_cap_pass(tmp_path):
    # RLIMIT_FSIZE stands in for the tmpfs quota: the write fails at 4 MiB.
    lines, failures = _call("check_shm_cap", str(tmp_path), "8",
                            prefix="ulimit -f 4096;")
    assert (lines, failures) == ([
        f"PASS agent's writes to {tmp_path} stop at 4 MiB (cap 8 MiB)"], 0)
    assert list(tmp_path.iterdir()) == []


def test_writes_stopped_only_beyond_the_cap_fail(tmp_path):
    lines, failures = _call("check_shm_cap", str(tmp_path), "2",
                            prefix="ulimit -f 4096;")
    assert failures == 1
    assert lines[0].startswith(f"FAIL agent's writes to {tmp_path} stop only "
                               f"at 4 MiB, beyond the 2 MiB cap")


def test_a_shared_memory_dir_closed_to_the_agent_passes(tmp_path):
    closed = tmp_path / "shm"
    closed.mkdir(mode=0o555)
    try:
        lines, failures = _call("check_shm_cap", str(closed), "256")
    finally:
        closed.chmod(0o755)
    if not _running_as_root():
        assert (lines, failures) == (
            [f"PASS agent cannot create files in {closed}"], 0)


def test_a_missing_shared_memory_dir_passes(tmp_path):
    assert _call("check_shm_cap", str(tmp_path / "absent"), "256") == (
        [f"PASS no {tmp_path / 'absent'} on this host"], 0)


def test_the_script_cap_matches_the_runbook():
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    cap = int(re.search(r"^AGENT_SHM_CAP_MB=(\d+)$", source, re.M).group(1))
    assert cap == 256
    assert f"most **{cap} MiB**" in " ".join(_runbook().split())


# --- SR3147-02: the limited broadcast probe ----------------------------------------


def _fake_python(tmp_path: Path, output: str) -> dict[str, str]:
    """A python3 on PATH that prints ``output``: the probe never sends a
    real broadcast from the test host."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_a_rejected_broadcast_passes(tmp_path):
    env = _fake_python(tmp_path, "REJECTED Operation not permitted")
    assert _call("check_broadcast", "255.255.255.255", env=env) == ([
        "PASS agent cannot send to the broadcast address 255.255.255.255 "
        "(Operation not permitted)"], 0)


def test_a_sent_broadcast_fails(tmp_path):
    env = _fake_python(tmp_path, "SENT")
    lines, failures = _call("check_broadcast", "255.255.255.255", env=env)
    assert failures == 1
    assert lines[0].startswith("FAIL agent can send to the broadcast address "
                               "255.255.255.255")


def test_a_broadcast_probe_that_cannot_run_fails(tmp_path):
    env = _fake_python(tmp_path, "Traceback (most recent call last):")
    lines, failures = _call("check_broadcast", "255.255.255.255", env=env)
    assert failures == 1
    assert lines[0].startswith("FAIL could not probe the broadcast address")


# --- SR3147-02/-03, IR3147-A: the rule, evaluated ---------------------------------

_DNS = re.compile(r"^(ip6?) daddr (\S+) (udp|tcp) dport (\d+)$")
_SET_LITERAL = re.compile(r"^(ip6?) daddr \{(.*)\}$")
_NAMED_SET = re.compile(r"^(ip6?) daddr @(\w+)$")
_SINGLE = re.compile(r"^(ip6?) daddr (\S+)$")


def _family(address: str) -> str:
    return "ip6" if ipaddress.ip_address(address).version == 6 else "ip"


def _matches(condition: str, packet: dict, sets: dict) -> bool:
    """A model of the expressions the agent chain uses. An expression it
    does not know fails the test, so a new rule shape gets modelled."""
    address = ipaddress.ip_address(packet["daddr"])
    if condition == "ct status dnat":
        return packet.get("dnat", False)
    if condition.startswith("fib daddr type "):
        return packet.get("addrtype", "unicast") == condition.split()[-1]
    if match := _DNS.match(condition):
        return (match[1] == _family(packet["daddr"])
                and address == ipaddress.ip_address(match[2])
                and packet["proto"] == match[3]
                and packet["dport"] == int(match[4]))
    if match := _SET_LITERAL.match(condition):
        return match[1] == _family(packet["daddr"]) and any(
            address in ipaddress.ip_network(cidr.strip())
            for cidr in match[2].split(","))
    if match := _NAMED_SET.match(condition):
        return match[1] == _family(packet["daddr"]) and any(
            address in network for network in sets[match[2]])
    if match := _SINGLE.match(condition):
        return (match[1] == _family(packet["daddr"])
                and address in ipaddress.ip_network(match[2]))
    raise AssertionError(f"no model for the nft expression {condition!r}")


def _verdict(packet: dict, sets: dict | None = None) -> str:
    """accept or reject for one agent packet; the output chain's policy
    accepts what the agent chain does not decide."""
    sets = {"lan6_prefixes": [], "lan4_prefixes": [], **(sets or {})}
    for statement in _chain(_statements(_heredoc(RULE_FILE)), "agent"):
        condition, _, action = statement.rpartition(" ")
        if _matches(condition, packet, sets):
            return action
    return "accept"


def _packet(daddr: str, dport: int = 443, proto: str = "tcp", **extra) -> dict:
    return {"daddr": daddr, "dport": dport, "proto": proto, **extra}


@pytest.mark.parametrize("packet", [
    _packet("255.255.255.255", 9, "udp", addrtype="broadcast"),
    # Even where fib reported it as plain unicast, the explicit line holds.
    _packet("255.255.255.255", 137, "udp"),
    # A directed broadcast of a public subnet: only fib's type catches it.
    _packet("192.0.2.255", 9, "udp", addrtype="broadcast"),
])
def test_the_rule_rejects_broadcast(packet):
    assert _verdict(packet) == "reject"


def test_a_dnated_connection_is_rejected_whatever_its_new_address():
    """Docker's published port at a host address, DNATed to a container
    in a pool outside every denied range."""
    assert _verdict(_packet("198.18.0.2", 8080, dnat=True)) == "reject"
    assert _verdict(_packet("198.18.0.2", 8080)) == "accept"
    # DNAT to the resolver is not the resolver the accept names.
    assert _verdict(_packet("127.0.0.53", 53, "udp", addrtype="local",
                            dnat=True)) == "reject"


def test_operator_lan_prefixes_are_rejected():
    lan6 = [ipaddress.ip_network("2001:db8:1234::/48")]
    lan4 = [ipaddress.ip_network("198.51.100.0/24")]
    nas6 = _packet("2001:db8:1234::10", 445)
    nas4 = _packet("198.51.100.7", 22)
    assert _verdict(nas6) == "accept"            # empty sets: not listed yet
    assert _verdict(nas6, {"lan6_prefixes": lan6}) == "reject"
    assert _verdict(nas4, {"lan4_prefixes": lan4}) == "reject"


@pytest.mark.parametrize("packet, expected", [
    (_packet("127.0.0.53", 53, "udp", addrtype="local"), "accept"),
    (_packet("127.0.0.53", 53, "tcp", addrtype="local"), "accept"),
    (_packet("127.0.0.53", 80, "tcp", addrtype="local"), "reject"),
    (_packet("203.0.113.9", 443), "accept"),      # the public internet
    (_packet("10.1.2.3", 445), "reject"),
    (_packet("fd00::1", 22), "reject"),
    (_packet("100.100.100.100", 53, "udp"), "reject"),
])
def test_the_rule_keeps_its_earlier_verdicts(packet, expected):
    assert _verdict(packet) == expected


def test_the_prefix_sets_are_declared_with_documentation_examples():
    body = _heredoc(RULE_FILE)
    statements = _statements(body)
    for name, kind in (("lan6_prefixes", "ipv6_addr"),
                       ("lan4_prefixes", "ipv4_addr")):
        start = statements.index(f"set {name} {{")
        assert statements[start + 1] == f"type {kind}; flags interval;"
        assert statements[start + 2] == "}"
    examples = re.findall(r"#\s*elements = \{ ([^}]*) \}", body)
    documentation = [ipaddress.ip_network(n) for n in
                     ("2001:db8::/32", "192.0.2.0/24", "198.51.100.0/24",
                      "203.0.113.0/24")]
    assert len(examples) == 2
    for example in examples:
        network = ipaddress.ip_network(example.strip())
        assert any(network.subnet_of(doc) for doc in documentation
                   if doc.version == network.version), example


# --- SR3147-01: the runbook closes the directories ---------------------------------


def test_the_runbook_closes_every_shared_directory_completely():
    text = _runbook()
    for directory in ("/tmp", "/var/tmp", "/var/crash", "/dev/shm"):
        assert re.search(rf"'a\+ {re.escape(directory)} +- - - - "
                         rf"u:equipa-agent:---'", text), directory
    assert "u:equipa-agent:--x'" not in text
    words = " ".join(text.split())
    assert "find / -xdev -type d -perm -0002" in words
    assert "cannot take systemd's `PrivateTmp=`, `PrivateDevices=`" in words
    assert "`InaccessiblePaths=`" in words


# --- SR3147-04: the MCP recipe stops at a failed hash check ------------------------


def _recipe(tmp_path: Path) -> str:
    text = _runbook()
    start = text.index("bash <<'RECIPE'\n")
    end = text.index("\nRECIPE\n", start) + len("\nRECIPE\n")
    recipe = text[start:end]
    assert recipe.splitlines()[1] == "set -euo pipefail"
    return (recipe.replace("/opt/equipa-mcp", str(tmp_path / "opt"))
            .replace("/root/equipa-mcp-wheels", str(tmp_path / "wheels")))


def _recipe_stubs(tmp_path: Path) -> dict[str, str]:
    """python3 -m venv, pip (download writes tampered wheels, install leaves
    a marker) and pip-audit, all local: no network, nothing installed."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    marker = tmp_path / "installed"
    pip = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  download)\n"
        "    for name in mcp_server_sqlite-2025.4.25-py3-none-any.whl "
        "mcp-1.30.0-py3-none-any.whl anyio-4.0.0-py3-none-any.whl; do\n"
        '      printf tampered > "$name"\n'
        "    done ;;\n"
        f'  install) touch "{marker}" ;;\n'
        "esac\n"
        "exit 0\n")
    python = (
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$3/bin"\n'
        f"  cat > \"$3/bin/pip\" <<'PIP'\n{pip}PIP\n"
        '  chmod 755 "$3/bin/pip"\n'
        "fi\n")
    (bin_dir / "python3").write_text(python, encoding="utf-8")
    (bin_dir / "pip-audit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path), "LC_ALL": "C"}


def _run_recipe(recipe: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", recipe], cwd=tmp_path,
                          env=_recipe_stubs(tmp_path), capture_output=True,
                          text=True, timeout=60)


def test_the_mcp_recipe_stops_at_a_failed_hash_check(tmp_path):
    result = _run_recipe(_recipe(tmp_path), tmp_path)
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not (tmp_path / "installed").exists(), "pip install still ran"
    assert not (tmp_path / "wheels" / "requirements.lock").exists()


def test_the_mcp_recipe_runs_through_when_the_hashes_match(tmp_path):
    """Control: the same stubs with matching hashes reach the install, so
    the stop above is the hash check's doing."""
    tampered = hashlib.sha256(b"tampered").hexdigest()
    recipe = re.sub(r"^[0-9a-f]{64}(?=  \S+\.whl$)", tampered,
                    _recipe(tmp_path), flags=re.MULTILINE)
    result = _run_recipe(recipe, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "installed").exists()
    assert (tmp_path / "opt" / "requirements.lock").is_file()
