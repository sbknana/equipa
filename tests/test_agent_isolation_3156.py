#!/usr/bin/env python3
"""Task 3156: verify-script firewall content checks and the recipe's pip.

R3153-02 / F-4 of the 3153 review: the verify script only checked that the
nftables table was loaded. A host still on an older rule file (no
``ct status dnat reject``, no broadcast rejects) passed, and so did a host
with a global IPv6 address whose ``lan6_prefixes`` set shipped empty. The
orchestrator side now reads ``nft list table inet equipa_agent`` and fails
on each, the empty set with a loud WARNING.

F-6: the MCP recipe is fed to ``bash`` on stdin, so a ``pip`` prompt would
swallow the rest of it; every pip call now runs with ``--no-input``.

The check functions are called on listings built from the runbook's own
rule file, so a doc change that drops a rule fails here too.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from tests.test_agent_isolation_3147 import RULE_FILE, _heredoc
from tests.test_agent_isolation_3153 import _call, _recipe, _recipe_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"
GLOBAL_V6 = "2001:db8:1234::5"
LAN6_EXAMPLE = "2001:db8:1234::/48"


def _runbook_table() -> str:
    """The runbook rule file's table block, as the table is listed."""
    rule = _heredoc(RULE_FILE)
    return rule[rule.index("table inet equipa_agent {"):]


def _with_lan6_elements(listing: str) -> str:
    return listing.replace(f"# elements = {{ {LAN6_EXAMPLE} }}",
                           f"elements = {{ {LAN6_EXAMPLE} }}")


# How nft itself lists a pre-3153 table: no DNAT or broadcast rejects, no
# prefix sets.
OLD_RULE_LISTING = """table inet equipa_agent {
\tchain agent {
\t\tip daddr 127.0.0.53 udp dport 53 accept
\t\tip daddr 127.0.0.53 tcp dport 53 accept
\t\tfib daddr type local reject
\t\tip daddr { 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8 } reject
\t\tip6 daddr { ::/128, ::1/128, fc00::/7, fe80::/10, ff00::/8 } reject
\t}

\tchain output {
\t\ttype filter hook output priority filter; policy accept;
\t\tmeta skuid "equipa-agent" jump agent
\t}
}
"""

# How nft lists the current rule: one attribute per line, the elements
# continued over two lines, rejects with their type spelled out.
NFT_STYLE_LISTING = """table inet equipa_agent {
\tset lan6_prefixes {
\t\ttype ipv6_addr
\t\tflags interval
\t\telements = { 2001:db8:1234::/48,
\t\t\t     2001:db8:abcd::/48 }
\t}

\tset lan4_prefixes {
\t\ttype ipv4_addr
\t\tflags interval
\t}

\tchain agent {
\t\tct status dnat reject with icmpx port-unreachable
\t\tip daddr 127.0.0.53 udp dport 53 accept
\t\tfib daddr type local reject with icmpx port-unreachable
\t\tfib daddr type broadcast reject with icmpx port-unreachable
\t\tip daddr 255.255.255.255 reject with icmpx port-unreachable
\t\tip6 daddr @lan6_prefixes reject with icmpx port-unreachable
\t}
}
"""


def test_the_runbook_rule_with_its_prefix_passes():
    lines, failures = _call("check_firewall_rule",
                            _with_lan6_elements(_runbook_table()), GLOBAL_V6)
    assert failures == 0, lines
    assert lines == [
        "PASS the loaded agent chain has 'ct status dnat reject'",
        "PASS the loaded agent chain has 'fib daddr type broadcast reject'",
        "PASS the loaded agent chain has 'ip daddr 255.255.255.255 reject'",
        f"PASS the LAN IPv6 prefix set lists: {{ {LAN6_EXAMPLE} }}",
    ]


def test_an_nft_style_listing_is_read():
    lines, failures = _call("check_firewall_rule", NFT_STYLE_LISTING,
                            GLOBAL_V6)
    assert failures == 0, lines
    assert lines[-1] == ("PASS the LAN IPv6 prefix set lists: "
                         "{ 2001:db8:1234::/48,")


def test_an_older_rule_file_fails_every_missing_rule():
    lines, failures = _call("check_firewall_rule", OLD_RULE_LISTING, "")
    assert failures == 4, lines
    for statement in ("ct status dnat reject",
                      "fib daddr type broadcast reject",
                      "ip daddr 255.255.255.255 reject"):
        assert any(line.startswith("FAIL the loaded agent chain lacks "
                                   f"'{statement}'") for line in lines), lines
    assert lines[-1].startswith("FAIL the loaded table has no lan6_prefixes set")


def test_an_empty_lan6_set_on_a_global_ipv6_host_warns_and_fails():
    lines, failures = _call("check_firewall_rule", _runbook_table(),
                            f"{GLOBAL_V6} ")
    assert failures == 1, lines
    warning = lines[-1]
    assert warning.startswith("FAIL WARNING: the LAN IPv6 prefix set "
                              "lan6_prefixes is EMPTY")
    assert f"({GLOBAL_V6})" in warning
    assert "docs/AGENT_ISOLATION.md step 4a" in warning


def test_an_empty_lan6_set_without_global_ipv6_passes():
    lines, failures = _call("check_firewall_rule", _runbook_table(), "")
    assert failures == 0, lines
    assert lines[-1].startswith("PASS no global IPv6 address on this host")


def test_a_dnat_reject_after_an_accept_fails():
    listing = _with_lan6_elements(_runbook_table())
    dnat = "        ct status dnat reject\n"
    assert dnat in listing
    moved = listing.replace(dnat, "").replace(
        "        fib daddr type local reject\n",
        "        fib daddr type local reject\n" + dnat)
    lines, failures = _call("check_firewall_rule", moved, GLOBAL_V6)
    assert failures == 1, lines
    assert [line for line in lines if line.startswith("FAIL")] == [
        "FAIL 'ct status dnat reject' is not the first statement of the "
        "loaded agent chain (it is 'ip daddr 127.0.0.53 udp dport 53 "
        "accept'; a DNATed connection must be rejected before any accept)"]


def test_a_commented_out_rule_does_not_count():
    listing = _with_lan6_elements(_runbook_table()).replace(
        "        fib daddr type broadcast reject\n",
        "        # fib daddr type broadcast reject\n")
    lines, failures = _call("check_firewall_rule", listing, GLOBAL_V6)
    assert failures == 1, lines
    assert ("FAIL the loaded agent chain lacks 'fib daddr type broadcast "
            "reject'") in lines[1]


def test_a_listing_without_the_agent_chain_fails():
    lines, failures = _call("check_firewall_rule",
                            "table inet equipa_agent {\n}\n", GLOBAL_V6)
    assert failures == 1
    assert lines[0].startswith("FAIL the loaded nftables table inet "
                               "equipa_agent has no 'agent' chain")


def _fake_bin(tmp_path: Path, scripts: dict[str, str]) -> dict[str, str]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        path.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_global_ipv6_addresses_skip_unique_local_addresses(tmp_path):
    ip_output = (
        "2: eth0    inet6 2001:db8:1234::5/64 scope global dynamic "
        "mngtmpaddr \\       valid_lft 86000sec preferred_lft 14000sec\n"
        "2: eth0    inet6 fd00:1::5/64 scope global \\       valid_lft "
        "forever preferred_lft forever\n"
        "3: tailscale0    inet6 FD7A:115C:A1E0::1/128 scope global \\\n"
        "4: wlan0    inet6 2001:DB8:ABCD::7/64 scope global \\\n")
    env = _fake_bin(tmp_path, {"ip": f"cat <<'EOF'\n{ip_output}EOF\n"})
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; global_ipv6_addresses', "verify-test",
         str(VERIFY_SCRIPT)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **env})
    assert result.stdout.split() == ["2001:db8:1234::5", "2001:db8:abcd::7"]


def _list_global_ipv6(tmp_path: Path, ip_body: str,
                      if_inet6: Path) -> subprocess.CompletedProcess:
    env = _fake_bin(tmp_path, {"ip": ip_body})
    return subprocess.run(
        ["bash", "-c",
         'source "$1"; IF_INET6_FILE="$2"; global_ipv6_addresses',
         "verify-test", str(VERIFY_SCRIPT), str(if_inet6)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **env})


def test_global_ipv6_addresses_fail_when_ip_fails_on_an_ipv6_host(tmp_path):
    """A failing ``ip`` must not read as "no global IPv6 address"."""
    if_inet6 = tmp_path / "if_inet6"
    if_inet6.write_text("", encoding="utf-8")
    result = _list_global_ipv6(tmp_path, "exit 1\n", if_inet6)
    assert result.returncode != 0, result.stdout
    assert result.stdout == ""


def test_global_ipv6_addresses_are_none_without_an_ipv6_stack(tmp_path):
    result = _list_global_ipv6(tmp_path, "exit 1\n",
                               tmp_path / "no-if_inet6")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_an_empty_lan6_set_with_unlisted_ipv6_addresses_fails():
    lines, failures = _call("check_firewall_rule", _runbook_table(), "",
                            "unknown")
    assert failures == 1, lines
    assert lines[-1].startswith("FAIL WARNING: the LAN IPv6 prefix set "
                                "lan6_prefixes is EMPTY and this host's "
                                "IPv6 addresses could not be listed")


def test_a_listed_lan6_set_does_not_need_the_host_addresses():
    lines, failures = _call("check_firewall_rule",
                            _with_lan6_elements(_runbook_table()), "",
                            "unknown")
    assert failures == 0, lines
    assert lines[-1] == f"PASS the LAN IPv6 prefix set lists: {{ {LAN6_EXAMPLE} }}"


def test_the_orchestrator_check_fails_when_ip_fails_on_an_ipv6_host(
        tmp_path):
    """End to end through check_loaded_firewall: the loaded table has the
    shipped empty lan6_prefixes set and ``ip`` cannot list addresses."""
    if_inet6 = tmp_path / "if_inet6"
    if_inet6.write_text("", encoding="utf-8")
    env = _fake_bin(tmp_path, {
        "sudo": f"cat <<'EOF'\n{_runbook_table()}\nEOF\n",
        "ip": "exit 1\n",
    })
    lines, failures = _call("check_loaded_firewall",
                            prefix=f"IF_INET6_FILE={if_inet6}", env=env)
    assert failures == 1, lines
    assert "could not be listed" in lines[-1], lines


def test_the_orchestrator_check_notes_an_unlistable_table(tmp_path):
    env = _fake_bin(tmp_path, {"sudo": "exit 1\n", "ip": "exit 0\n"})
    lines, failures = _call("check_loaded_firewall", env=env)
    assert failures == 0, lines
    assert lines == ["NOTE the nftables table could not be listed, so its "
                     "rules were not checked (the table check below "
                     "reports why)"]


def test_the_script_fails_on_an_older_rule_even_when_the_probe_passes(
        tmp_path):
    """End to end on the orchestrator side: a fake sudo lists the older
    rule, a fake python runs the (passing) probe."""
    env = _fake_bin(tmp_path, {
        "sudo": f"cat <<'EOF'\n{OLD_RULE_LISTING}EOF\n",
        "ip": "exit 0\n",
        "id": "echo 1000\n",
        "probe-python": 'echo "PASS probe"\necho "RESULT: PASS"\nexit 0\n',
    })
    env["EQUIPA_PYTHON"] = str(tmp_path / "fakebin" / "probe-python")
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT)], capture_output=True, text=True,
        timeout=60, env={**os.environ, **env})
    lines = result.stdout.splitlines()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "RESULT: PASS" in lines, "the probe did not run"
    assert lines[-1] == ("RESULT: FAIL (4 orchestrator-side firewall rule "
                         "check(s) failed)")


def test_the_script_keeps_the_probe_status_when_the_rule_is_current(tmp_path):
    env = _fake_bin(tmp_path, {
        "sudo": "cat <<'EOF'\n"
                f"{_with_lan6_elements(_runbook_table())}\nEOF\n",
        "ip": "exit 0\n",
        "id": "echo 1000\n",
        "probe-python": 'echo "RESULT: PASS"\nexit 0\n',
    })
    env["EQUIPA_PYTHON"] = str(tmp_path / "fakebin" / "probe-python")
    result = subprocess.run(
        ["bash", str(VERIFY_SCRIPT)], capture_output=True, text=True,
        timeout=60, env={**os.environ, **env})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "RESULT: PASS"


# --- F-6: the recipe's pip never reads the recipe as input ---------------------

def test_every_pip_call_in_the_recipe_has_no_input(tmp_path):
    recipe = _recipe(tmp_path)
    calls = [line for line in recipe.splitlines()
             if re.search(r"/bin/pip\s", line)]
    assert len(calls) == 3, calls
    for call in calls:
        assert " --no-input" in call, call


def test_a_prompting_pip_does_not_swallow_the_recipe(tmp_path):
    """A pip that would read stdin without --no-input (a credentials
    prompt) eats the rest of the recipe, so nothing is installed."""
    # The stub wheels hold "tampered": pin that hash so the recipe reaches
    # the install (as the 3153 control test does).
    recipe = re.sub(r"^[0-9a-f]{64}(?=  \S+\.whl$)",
                    hashlib.sha256(b"tampered").hexdigest(),
                    _recipe(tmp_path), flags=re.MULTILINE)
    env = _recipe_stubs(tmp_path)
    venv_python = Path(env["PATH"].split(os.pathsep)[0]) / "python3"
    # The stub pip reads all of stdin unless --no-input is given.
    venv_python.write_text(venv_python.read_text(encoding="utf-8").replace(
        "#!/bin/sh\ncase", '#!/bin/sh\ncase " $* " in *" --no-input "*) ;; '
        '*) cat >/dev/null ;; esac\ncase'), encoding="utf-8")
    result = subprocess.run(["bash", "-c", recipe], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "installed").exists(), "pip install never ran"
    assert (tmp_path / "opt" / "requirements.lock").is_file()
