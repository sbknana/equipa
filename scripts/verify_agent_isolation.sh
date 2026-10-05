#!/usr/bin/env bash
# verify_agent_isolation.sh - operator check for EQUIPA agent isolation.
#
# Copyright 2026 Forgeborn
#
# Run it AS THE ORCHESTRATOR USER (not root) after following
# docs/AGENT_ISOLATION.md, from the EQUIPA runtime checkout, with the same
# environment the orchestrator runs with (THEFORGE_DB, the OAuth token):
#
#     scripts/verify_agent_isolation.sh [--repo /path/to/project ...] \
#         [--loopback-port PORT ...] [--lan-target ADDRESS:PORT ...]
#
# It starts THIS script as an isolated agent through the real launch path
# (systemd-run --user --scope + the sudoers rule + agent_launcher --isolated)
# and, inside, checks as the agent user that it:
#   * is not root and cannot sudo;
#   * cannot read the TheForge database or the orchestrator's HOME secrets;
#   * can neither list nor enter the TheForge database directory or any
#     backup directory, and cannot read any database copy in them (every
#     copy the orchestrator finds is probed by name, and the agent searches
#     for readable ones itself);
#   * cannot read secret-shaped files (.env, keys, credentials) below the
#     configured project roots (agent_isolation.secret_scan_roots, --repo);
#   * cannot write any repository .git, the EQUIPA runtime or the launcher;
#   * runs in its own equipa-agent-*.scope cgroup with pids.max and
#     memory.max set, and cannot leave that cgroup or raise its limits;
#   * cannot signal the orchestrator;
#   * sees a TheForge view without the excluded tables (api_keys);
#   * has no credential in its environment except CLAUDE_CODE_OAUTH_TOKEN;
#   * cannot use crontab or at, and does not linger (nothing outlives it);
#   * cannot connect to its own listener on any address of this host
#     (loopback, LAN, tailnet, public: the nftables rule's
#     "fib daddr type local reject"), to any --loopback-port or port that
#     listens on loopback at 127.0.0.1 or at any host address, or to any
#     --lan-target (the launcher also refuses while its own loopback and
#     LAN listeners are reachable);
#   * cannot send to the IPv4 limited broadcast address 255.255.255.255;
#   * has a TMPDIR of its own on a filesystem other than /, and cannot
#     write a shared /tmp or /var/tmp that lies on / (it could fill /), nor
#     any other world-writable directory on / (find / -xdev -perm -0002,
#     plus /tmp/.X11-unix-style names below a /tmp it cannot list);
#   * cannot hold more than AGENT_SHM_CAP_MB MiB in /dev/shm (files there
#     outlive the unit).
# As the orchestrator it also checks that the narrow sudoers rule is
# installed, by its content (an ALL rule does not count), that the agent
# user's nftables table (inet equipa_agent) is loaded with the step 4a
# rules (ct status dnat reject first, the broadcast rejects, and a
# non-empty lan6_prefixes set on a host with a global IPv6 address), and
# that every --lan-target answers the orchestrator (else its probe proves
# nothing).
# Every check prints PASS or FAIL (a NOTE names what was not probed); the
# last line, and the only RESULT line, is RESULT: PASS|FAIL.
# Exit status: 0 all checks passed, 1 a check failed, 2 isolation could not
# be established at all.
#
# This is a real-host check on purpose: the unit tests cannot create users,
# sudoers rules or cgroups, so nothing here is simulated or skipped.

set -u

# pass/fail count into the caller's "failures" (inside() keeps its own).
failures=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }

# The agent's groups ("$@", as id -nG prints them) must include none that
# is root-equivalent or exposes other users' data (review F7; dispatch and
# the launcher check the same list).
check_privileged_groups() {
    local group member privileged=0
    for group in root sudo admin wheel adm docker lxd incus libvirt kvm disk shadow systemd-journal; do
        for member in "$@"; do
            if [ "$member" = "$group" ]; then
                fail "agent user is in the privileged group $group"
                privileged=$((privileged + 1))
            fi
        done
    done
    if [ "$privileged" -eq 0 ]; then
        pass "agent user is in no privileged group (groups: $*)"
    fi
}

# Each shared temporary directory in "$@" the agent can write must lie on a
# filesystem other than the root filesystem $1: /tmp and /var/tmp are
# world-writable, so a TMPDIR of its own does not stop an agent writing
# /var/tmp/x until / is full (review R3142-04, F8).
check_shared_tmp() {
    local root="$1" directory root_device
    shift
    root_device="$(stat -c %d -- "$root" 2>/dev/null)"
    for directory in "$@"; do
        [ -d "$directory" ] || continue
        if [ ! -w "$directory" ]; then
            pass "agent cannot write $directory"
        elif [ -z "$root_device" ] \
                || [ "$(stat -c %d -- "$directory" 2>/dev/null)" = "$root_device" ]; then
            fail "agent can write $directory on the root filesystem $root (it can fill it; make $directory a size-capped filesystem or close it to the agent user, docs/AGENT_ISOLATION.md step 1)"
        else
            pass "$directory is on a filesystem other than $root"
        fi
    done
}

# No world-writable directory on the root filesystem $1 may be writable by
# the agent (review SR3147-01): the tmpfiles ACL of step 1 on /tmp is not
# recursive, and 1777 directories such as /tmp/.X11-unix and /var/crash lie
# on / too. Candidates are every directory find(1) reports with the other-
# write bit, run as the agent (it only sees what it can traverse), plus the
# directories named in "$@" (well-known ones below a /tmp the agent may
# enter but not list). Each candidate on $1's filesystem is probed with a
# real file create, removed again at once: access(2) and the mode bits do
# not tell the whole story (ACLs, read-only mounts).
# The search runs at most WORLD_WRITABLE_SCAN_SECONDS (task 3169: a root
# filesystem holding millions of directories, such as a CI runner's
# toolchains, took longer than any caller waits). A search cut short, or one
# that could not run, fails: the directories it did not reach were not
# probed. The candidates it did report are probed all the same.
WORLD_WRITABLE_SCAN_SECONDS=600
check_world_writable_dirs() {
    local root="$1" root_device directory probe count=0 status
    local -a candidates=() writable=()
    local -A seen=()
    shift
    root_device="$(stat -c %d -- "$root" 2>/dev/null)"
    if [ -z "$root_device" ]; then
        fail "cannot stat the root filesystem $root"
        return 0
    fi
    while IFS= read -r -d '' directory; do
        candidates+=("$directory")
    done < <(timeout --kill-after=5 "$WORLD_WRITABLE_SCAN_SECONDS" \
                 find "$root" -xdev -type d -perm -0002 -print0 2>/dev/null)
    wait "$!"
    status=$?
    # find exits 1 for the directories the agent cannot read: expected.
    case "$status" in
        124|137) fail "the search of the root filesystem $root for world-writable directories did not finish within $WORLD_WRITABLE_SCAN_SECONDS s: the directories it did not reach were not probed" ;;
        125|126|127) fail "the search of the root filesystem $root for world-writable directories could not run (exit $status)" ;;
    esac
    candidates+=("$@")
    for directory in "${candidates[@]}"; do
        [ -n "${seen["$directory"]:-}" ] && continue
        seen["$directory"]=1
        [ -d "$directory" ] && [ ! -L "$directory" ] || continue
        [ "$(stat -c %d -- "$directory" 2>/dev/null)" = "$root_device" ] || continue
        count=$((count + 1))
        if probe="$(mktemp -p "$directory" .equipa-verify-ww.XXXXXXXX 2>/dev/null)"; then
            rm -f -- "$probe"
            writable+=("$directory")
        fi
    done
    for directory in "${writable[@]}"; do
        fail "agent can write the world-writable directory $directory on the root filesystem $root (it can fill it; close it with a tmpfiles.d ACL u:equipa-agent:---, docs/AGENT_ISOLATION.md step 1)"
    done
    if [ "${#writable[@]}" -eq 0 ]; then
        pass "agent cannot write any of $count world-writable director(y/ies) on the root filesystem $root"
    fi
}

# A directory in "$@" the agent may enter but not list hides the names below
# it from find(1): only the well-known ones were probed.
note_search_only_dirs() {
    local directory
    for directory in "$@"; do
        if [ -d "$directory" ] && [ -x "$directory" ] && [ ! -r "$directory" ]; then
            echo "NOTE agent may enter but not list $directory: only well-known names below it were probed (close it completely with u:equipa-agent:---, docs/AGENT_ISOLATION.md step 1)"
        fi
    done
}

# The agent may hold at most $2 MiB in the shared-memory directory $1
# (review SR3147-01): /dev/shm is a host-wide tmpfs (half the RAM by
# default) whose files outlive the unit, so units run one after another
# could pile them up. Either the agent cannot create files there, or its
# writes stop (quota, ENOSPC) by the cap. The probe writes at most $2 + 16
# MiB in 1 MiB chunks and always removes its file.
SHM_PROBE_PY='
import errno, os, sys
directory, cap_mb = sys.argv[1], int(sys.argv[2])
limit_mb = cap_mb + 16
try:
    with open("/proc/self/cgroup", encoding="ascii") as handle:
        cgroup = handle.read().strip().rpartition(":")[2]
    with open("/sys/fs/cgroup" + cgroup + "/memory.max", encoding="ascii") as handle:
        memory_max = handle.read().strip()
except OSError:
    memory_max = "max"
if memory_max != "max" and int(memory_max) < (limit_mb + 64) * 1024 * 1024:
    print("NOMEM", memory_max)
    sys.exit(0)
path = os.path.join(directory, ".equipa-verify-shm.%d" % os.getpid())
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except PermissionError:
    print("CLOSED")
    sys.exit(0)
except OSError as exc:
    print("ERROR", exc.strerror)
    sys.exit(0)
written = 0
chunk = b"\0" * (1024 * 1024)
try:
    while written < limit_mb:
        os.write(fd, chunk)
        written += 1
    print("WROTE", written)
except OSError as exc:
    if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
        print("STOPPED", written)
    else:
        print("ERROR", exc.strerror)
finally:
    os.close(fd)
    os.unlink(path)
'

check_shm_cap() {
    local directory="$1" cap_mb="$2" kind value rest
    if [ ! -d "$directory" ]; then
        pass "no $directory on this host"
        return 0
    fi
    read -r kind value rest < <(python3 -I -c "$SHM_PROBE_PY" "$directory" "$cap_mb" 2>&1)
    case "$kind" in
        CLOSED) pass "agent cannot create files in $directory" ;;
        STOPPED)
            if [ "$value" -le "$cap_mb" ]; then
                pass "agent's writes to $directory stop at $value MiB (cap $cap_mb MiB)"
            else
                fail "agent's writes to $directory stop only at $value MiB, beyond the $cap_mb MiB cap (docs/AGENT_ISOLATION.md step 1)"
            fi ;;
        WROTE) fail "agent wrote $value MiB to $directory, beyond the $cap_mb MiB cap: its files outlive the unit (close it to the agent user or give it a per-user quota, docs/AGENT_ISOLATION.md step 1)" ;;
        NOMEM) fail "cannot probe the $cap_mb MiB cap of $directory: the unit's memory.max ($value bytes) is too small for the probe" ;;
        *) fail "could not probe $directory: $kind $value $rest" ;;
    esac
}

# The IPv4 limited broadcast address 255.255.255.255 is neither a local
# address nor in a denied range, so only an explicit rule rejects it
# (review SR3147-02). One UDP datagram to the discard port with
# SO_BROADCAST must be refused.
BROADCAST_PROBE_PY='
import socket, sys
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    probe.sendto(b"equipa-verify", (sys.argv[1], 9))
except OSError as exc:
    print("REJECTED", exc.strerror)
else:
    print("SENT")
finally:
    probe.close()
'

check_broadcast() {
    local address="${1:-255.255.255.255}" kind rest
    read -r kind rest < <(python3 -I -c "$BROADCAST_PROBE_PY" "$address" 2>&1)
    case "$kind" in
        REJECTED) pass "agent cannot send to the broadcast address $address ($rest)" ;;
        SENT) fail "agent can send to the broadcast address $address (the nftables rule must reject it for the agent user, docs/AGENT_ISOLATION.md step 4a)" ;;
        *) fail "could not probe the broadcast address $address: $kind $rest" ;;
    esac
}

# The cap check_shm_cap enforces (runbook step 1 uses the same number).
AGENT_SHM_CAP_MB=256
# Well-known world-writable directories below /tmp and on / that a /tmp the
# agent may enter but not list would hide from find(1).
WELL_KNOWN_WORLD_WRITABLE=(/tmp /var/tmp /var/crash /tmp/.X11-unix
    /tmp/.ICE-unix /tmp/.XIM-unix /tmp/.font-unix /tmp/.Test-unix)

# The unit's TMPDIR is its own (in the unit's state directory, mode 0700)
# and lies on a filesystem other than the root filesystem $1, the
# size-capped agent state root of runbook step 1 (F8).
check_unit_tmpdir() {
    local root="$1" directory="${TMPDIR:-}"
    case "$directory" in
        */.equipa-agent/equipa-agent-*/tmp) ;;
        *) fail "agent TMPDIR '$directory' is not the unit's own"; return 0 ;;
    esac
    if [ ! -d "$directory" ] || [ ! -O "$directory" ] \
            || [ "$(stat -c %a -- "$directory" 2>/dev/null)" != "700" ]; then
        fail "agent TMPDIR $directory is not a 0700 directory of the agent user"
    elif [ "$(stat -c %d -- "$directory" 2>/dev/null)" = "$(stat -c %d -- "$root" 2>/dev/null)" ]; then
        fail "agent TMPDIR $directory is on the root filesystem $root; put the agent state root on a size-capped filesystem of its own (docs/AGENT_ISOLATION.md step 1)"
    else
        pass "agent TMPDIR is the unit's own, on a filesystem other than $root"
    fi
}

# Tries every target side by side and prints "REACHED <address> <port>"
# for each connection that was established, "ERROR <address> <port>
# <reason>" for each that could not be tried. Arguments: the timeout in
# seconds, then address/port pairs; port 0 means a listener of the
# probe's own, bound on that address. A batch shares one deadline (a
# dropped SYN never answers); batches keep the open sockets bounded.
TCP_PROBE_PY='
import errno, select, socket, sys, time
timeout = float(sys.argv[1])
pairs = sys.argv[2:]
targets = [(pairs[index], int(pairs[index + 1]))
           for index in range(0, len(pairs) - 1, 2)]
waiting = (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN)

def probe(batch):
    opened, clients, pending = [], {}, []
    try:
        for address, port in batch:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            try:
                client = socket.socket(family, socket.SOCK_STREAM)
                opened.append(client)
                destination = (address, port)
                if port == 0:
                    listener = socket.socket(family, socket.SOCK_STREAM)
                    opened.append(listener)
                    listener.bind((address, 0))
                    listener.listen(1)
                    destination = listener.getsockname()[:2]
                client.setblocking(False)
                code = client.connect_ex(destination)
            except OSError as exc:
                print("ERROR", address, port, exc.strerror or exc)
                continue
            clients[client] = (address, port)
            if code == 0:
                print("REACHED", address, port)
            elif code in waiting:
                pending.append(client)
        deadline = time.monotonic() + timeout
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for client in select.select([], pending, [], remaining)[1]:
                pending.remove(client)
                if client.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    print("REACHED", *clients[client])
    finally:
        for sock in opened:
            sock.close()

for start in range(0, len(targets), 200):
    probe(targets[start:start + 200])
'
tcp_probe() {
    python3 -I -c "$TCP_PROBE_PY" "$@"
}

# The network checks (review F2, N1, R3142-01/02): its own listener on each
# host address in $1 (space-separated), every port in $2 at 127.0.0.1 and
# at each host address (a service bound to every address answers on all of
# them), and each LAN target (ADDRESS:PORT or [IPV6]:PORT) in $3. The
# nftables rule of runbook step 4a rejects every local address and the
# denied ranges for the agent user, so nothing may be reached.
check_network() {
    local host_addresses="$1" ports="$2" lan_targets="$3"
    local address port target host output status kind rest count
    local -a addresses=() targets=() errors=()
    local -A reached=()
    for address in 127.0.0.1 $host_addresses; do
        case " ${addresses[*]} " in *" $address "*) ;; *) addresses+=("$address") ;; esac
    done
    for address in $host_addresses; do targets+=("$address" 0); done
    for port in $ports; do
        for address in "${addresses[@]}"; do targets+=("$address" "$port"); done
    done
    for target in $lan_targets; do
        port="${target##*:}"
        host="${target%:*}"
        host="${host#[}"
        targets+=("${host%]}" "$port")
    done
    if [ "${#targets[@]}" -gt 0 ]; then
        output="$(tcp_probe 3 "${targets[@]}")"
        status=$?
        if [ "$status" -ne 0 ]; then
            fail "the network probe could not run (python3 exited $status)"
        fi
        while read -r kind address port rest; do
            case "$kind" in
                REACHED) reached["$address $port"]=1 ;;
                ERROR) errors+=("$address port $port: $rest") ;;
            esac
        done <<< "$output"
    fi
    for target in "${errors[@]}"; do
        fail "could not probe $target"
    done

    if [ -z "$host_addresses" ]; then
        fail "no host address to probe (the orchestrator lists every address of this host)"
    else
        rest=""
        for address in $host_addresses; do
            if [ -n "${reached["$address 0"]:-}" ]; then rest="$rest $address"; fi
        done
        if [ -n "$rest" ]; then
            fail "agent can connect to its own listener on the host address(es)$rest (the nftables rule must reject every local address for the agent user: fib daddr type local reject)"
        else
            pass "agent cannot connect to its own listener on any of $(count_words $host_addresses) host address(es):" $host_addresses
        fi
    fi

    if [ -z "$ports" ]; then
        pass "no local service port to probe (none listed with --loopback-port, none listening on loopback)"
    else
        for address in "${addresses[@]}"; do
            rest=""
            for port in $ports; do
                if [ -n "${reached["$address $port"]:-}" ]; then rest="$rest $port"; fi
            done
            if [ -n "$rest" ]; then
                fail "agent can connect to $address on port(s)$rest (not blocked for the agent user)"
            else
                pass "agent cannot connect to $address on any of $(count_words $ports) local service port(s)"
            fi
        done
    fi

    if [ -z "$lan_targets" ]; then
        echo "NOTE no --lan-target listed: the LAN ranges were not probed (list a LAN service, for example a NAS or router port)"
    else
        rest=""
        for target in $lan_targets; do
            port="${target##*:}"
            host="${target%:*}"
            host="${host#[}"
            if [ -n "${reached["${host%]} $port"]:-}" ]; then rest="$rest $target"; fi
        done
        if [ -n "$rest" ]; then
            fail "agent can connect to the LAN target(s)$rest (not blocked for the agent user)"
        else
            pass "agent cannot connect to any of $(count_words $lan_targets) LAN target(s)"
        fi
    fi
}

count_words() { echo "$#"; }

# --- orchestrator side: the content of the loaded nftables rule ------------
# The table check (equipa.isolation) only proves the table is loaded. A host
# still running an older rule file passes it, and the agent-side probes need
# a published Docker port or a global IPv6 LAN target to notice what is
# missing (R3153-02, F-4 of the 3153 review). These read the listing
# instead: `sudo -n /usr/sbin/nft list table inet equipa_agent`, the one
# nft command the runbook lets the orchestrator run (step 6).
NFT_EXECUTABLE=/usr/sbin/nft
# Statements of the step 4a "agent" chain this check requires; the first
# must also come first in the chain (IR3147-A: before any accept).
REQUIRED_AGENT_CHAIN_STATEMENTS=(
    "ct status dnat reject"
    "fib daddr type broadcast reject"
    "ip daddr 255.255.255.255 reject"
)

# The body of the block "$2 {" in the nft listing "$1", one trimmed
# statement per line, comments and blank lines dropped; nothing when the
# block is missing.
nft_block() {
    printf '%s\n' "$1" | awk -v header="$2 {" '
        { line = $0; gsub(/^[ \t]+|[ \t]+$/, "", line) }
        !inside { if (line == header) { inside = 1; depth = 1 }; next }
        {
            depth += gsub(/\{/, "{") - gsub(/\}/, "}")
            if (depth <= 0) exit
            if (line != "" && substr(line, 1, 1) != "#") print line
        }'
}

# 0 when the statement lines "$1" hold "$2" as a whole statement (nft may
# list a reject with its default type: "... reject with icmpx ...").
has_statement() {
    local line
    while IFS= read -r line; do
        case "$line" in "$2"|"$2 with "*) return 0 ;; esac
    done <<< "$1"
    return 1
}

# Present whenever the kernel has an IPv6 stack (absent with ipv6.disable=1).
IF_INET6_FILE=/proc/net/if_inet6

# The global IPv6 addresses of this host, space-separated. Unique local
# addresses (fc00::/7) are left out: the rule rejects that range already.
# Non-zero when `ip` cannot list them on a host with an IPv6 stack: an
# empty answer then would read as "no global address" and pass the check.
global_ipv6_addresses() {
    local listing
    if ! listing="$(ip -6 -o addr show scope global 2>/dev/null)"; then
        [ -e "$IF_INET6_FILE" ] && return 1
        return 0
    fi
    printf '%s\n' "$listing" | awk '
        { split($4, parts, "/"); address = tolower(parts[1])
          if (address !~ /^f[cd]/) printf "%s ", address }'
}

# $1: the listing of table inet equipa_agent; $2: the host's global IPv6
# addresses (space-separated, empty when it has none); $3: "unknown" when
# they could not be listed.
check_firewall_rule() {
    local listing="$1" global_ipv6="$2" ipv6_known="${3:-known}"
    local chain first statement lan6
    chain="$(nft_block "$listing" "chain agent")"
    if [ -z "$chain" ]; then
        fail "the loaded nftables table inet equipa_agent has no 'agent' chain (reload the rule file of docs/AGENT_ISOLATION.md step 4a)"
        return
    fi
    for statement in "${REQUIRED_AGENT_CHAIN_STATEMENTS[@]}"; do
        if has_statement "$chain" "$statement"; then
            pass "the loaded agent chain has '$statement'"
        else
            fail "the loaded agent chain lacks '$statement' (an older rule file is loaded; reload the rule file of docs/AGENT_ISOLATION.md step 4a)"
        fi
    done
    first="$(head -n 1 <<< "$chain")"
    statement="${REQUIRED_AGENT_CHAIN_STATEMENTS[0]}"
    if has_statement "$chain" "$statement" \
            && ! has_statement "$first" "$statement"; then
        fail "'$statement' is not the first statement of the loaded agent chain (it is '$first'; a DNATed connection must be rejected before any accept)"
    fi
    if ! printf '%s\n' "$listing" | grep -Eq '^[[:space:]]*set lan6_prefixes \{[[:space:]]*$'; then
        fail "the loaded table has no lan6_prefixes set (an older rule file is loaded; reload the rule file of docs/AGENT_ISOLATION.md step 4a)"
        return
    fi
    lan6="$(nft_block "$listing" "set lan6_prefixes" | grep -E '^elements = ')"
    if [ -n "$lan6" ]; then
        pass "the LAN IPv6 prefix set lists: ${lan6#elements = }"
    elif [ -n "$global_ipv6" ]; then
        fail "WARNING: the LAN IPv6 prefix set lan6_prefixes is EMPTY on a host with global IPv6 address(es) (${global_ipv6% }): the agent can reach every NAS, router or database on the LAN through its global IPv6 address; list the LAN's prefix ('ip -6 route show proto kernel', 'ip -6 route show proto ra') in the set (docs/AGENT_ISOLATION.md step 4a)"
    elif [ "$ipv6_known" = unknown ]; then
        fail "WARNING: the LAN IPv6 prefix set lan6_prefixes is EMPTY and this host's IPv6 addresses could not be listed ('ip -6 addr show' failed), so a LAN IPv6 prefix left open cannot be ruled out; fix 'ip' or list the LAN's prefix in the set (docs/AGENT_ISOLATION.md step 4a)"
    else
        pass "no global IPv6 address on this host, so the empty lan6_prefixes set leaves no LAN IPv6 prefix open"
    fi
}

# Orchestrator side: list the loaded table and check its rules. Listing it
# needs the sudoers rule of step 6; without it the table check of
# equipa.isolation fails and says why.
check_loaded_firewall() {
    local listing global_ipv6 ipv6_known=known
    if ! listing="$(sudo -n "$NFT_EXECUTABLE" list table inet equipa_agent 2>/dev/null)" \
            || [ -z "$listing" ]; then
        echo "NOTE the nftables table could not be listed, so its rules were not checked (the table check below reports why)"
        return
    fi
    global_ipv6="$(global_ipv6_addresses)" || ipv6_known=unknown
    check_firewall_rule "$listing" "$global_ipv6" "$ipv6_known"
}

inside() {
    local orchestrator_pid="" orchestrator_home="" database="" runtime=""
    local launcher="" pids_expected="" view_db="" mcp_config=""
    local root_fs=/ scan_seconds=""
    local -a git_dirs=() excluded_tables=() deny_dirs=() db_copies=()
    local -a secret_roots=() deny_ports=() host_addresses=() lan_targets=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --deny-dir) deny_dirs+=("$2"); shift 2 ;;
            --deny-port) deny_ports+=("$2"); shift 2 ;;
            --host-address) host_addresses+=("$2"); shift 2 ;;
            --lan-target) lan_targets+=("$2"); shift 2 ;;
            --db-copy) db_copies+=("$2"); shift 2 ;;
            --secret-root) secret_roots+=("$2"); shift 2 ;;
            --orchestrator-pid) orchestrator_pid="$2"; shift 2 ;;
            --orchestrator-home) orchestrator_home="$2"; shift 2 ;;
            --db) database="$2"; shift 2 ;;
            --runtime) runtime="$2"; shift 2 ;;
            --launcher) launcher="$2"; shift 2 ;;
            --pids-max) pids_expected="$2"; shift 2 ;;
            --git-dir) git_dirs+=("$2"); shift 2 ;;
            --view-db) view_db="$2"; shift 2 ;;
            --exclude-table) excluded_tables+=("$2"); shift 2 ;;
            --mcp-config) mcp_config="$2"; shift 2 ;;
            # The root filesystem the disk checks search, and the bound on
            # that search (task 3169). The orchestrator passes neither, so a
            # real host is checked at / with the default bound; the tests
            # name a directory of their own and finish in seconds.
            --root-fs) root_fs="$2"; shift 2 ;;
            --scan-seconds) scan_seconds="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    local failures=0
    if [ -n "$scan_seconds" ]; then
        case "$scan_seconds" in
            *[!0-9]*|0*) fail "--scan-seconds '$scan_seconds' is not a positive whole number (the default $WORLD_WRITABLE_SCAN_SECONDS s applies)" ;;
            *) WORLD_WRITABLE_SCAN_SECONDS="$scan_seconds" ;;
        esac
    fi

    # --- identity: not root, no sudo, no privileged group -------------------
    local uid user
    uid="$(id -u)"
    user="$(id -un)"
    if [ "$uid" -eq 0 ]; then fail "agent runs as root"; else
        pass "agent runs as $user (uid $uid)"; fi
    if sudo -n true >/dev/null 2>&1; then
        fail "agent user can sudo (sudo -n true succeeded)"
    else
        pass "agent user cannot sudo (sudo -n true refused)"
    fi
    # id -nG prints the names separated by spaces; none contains one.
    # shellcheck disable=SC2046
    check_privileged_groups $(id -nG)

    # --- secrets: TheForge DB and the orchestrator's HOME ------------------
    local path
    for path in "$database" "$database-wal" "$database-shm"; do
        [ -n "$database" ] || break
        if head -c 1 -- "$path" >/dev/null 2>&1; then
            fail "agent can read $path"
        else
            pass "agent cannot read $path"
        fi
    done
    # The directories, not only the files: a backup copy beside the live
    # database (theforge_backup_<date>.db) holds the same api_keys table.
    local directory readable
    for directory in "${deny_dirs[@]}"; do
        if [ -r "$directory" ] || [ -x "$directory" ]; then
            fail "agent can list or enter the TheForge database/backup directory $directory"
        else
            pass "agent can neither list nor enter $directory"
        fi
        readable="$(find "$directory" -xdev -type f \( -name '*.db' -o -name '*.db[-._]*' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.sqlite[-._]*' -o -name '*.sqlite3[-._]*' \) -readable -print 2>/dev/null | head -n 5 | tr '\n' ' ')"
        if [ -n "$readable" ]; then
            fail "agent can read database copies under $directory: $readable"
        fi
    done
    local copies_readable=0
    for path in "${db_copies[@]}"; do
        if head -c 1 -- "$path" >/dev/null 2>&1; then
            fail "agent can read the database copy $path"
            copies_readable=$((copies_readable + 1))
        fi
    done
    if [ "${#db_copies[@]}" -gt 0 ] && [ "$copies_readable" -eq 0 ]; then
        pass "agent cannot read any of the ${#db_copies[@]} database files and copies"
    fi
    local root secrets
    for root in "${secret_roots[@]}"; do
        secrets="$(find "$root" -xdev -maxdepth 6 \( -name node_modules -o -name .git -o -name .venv -o -name venv \) -prune -o -type f \( -name '.env' -o -name '.env.*' -o -name '*.pem' -o -name '*.key' -o -name 'id_rsa*' -o -name 'id_ed25519*' -o -name 'id_ecdsa*' -o -name '*.p12' -o -name '*.pfx' -o -name 'credentials*.json' -o -name '.netrc' -o -name '.pgpass' -o -name '.git-credentials' \) ! -name '*.example' ! -name '*.sample' ! -name '*.template' ! -name '*.pub' -readable -print 2>/dev/null | head -n 10 | tr '\n' ' ')"
        if [ -n "$secrets" ]; then
            fail "agent can read secret files under $root: $secrets"
        else
            pass "agent can read no secret files under $root"
        fi
    done
    if [ -n "$orchestrator_home" ]; then
        if ls -- "$orchestrator_home" >/dev/null 2>&1; then
            fail "agent can list the orchestrator HOME $orchestrator_home"
        else
            pass "agent cannot list the orchestrator HOME"
        fi
        if [ -x "$orchestrator_home" ]; then
            fail "agent can enter the orchestrator HOME $orchestrator_home (0711 exposes every file at a known name)"
        fi
        for path in .claude .claude.json .config .ssh .gitconfig .git-credentials .netrc .pgpass; do
            if head -c 1 -- "$orchestrator_home/$path" >/dev/null 2>&1 \
                    || ls -- "$orchestrator_home/$path" >/dev/null 2>&1; then
                fail "agent can read $orchestrator_home/$path"
            fi
        done
    fi
    if [ -n "$mcp_config" ] && head -c 1 -- "$mcp_config" >/dev/null 2>&1; then
        fail "agent can read the orchestrator MCP config $mcp_config"
    fi

    # --- repositories and runtime are not writable -------------------------
    local git_dir probe
    for git_dir in "${git_dirs[@]}"; do
        probe="$git_dir/equipa-isolation-probe.$$"
        if (: > "$probe") 2>/dev/null; then
            rm -f -- "$probe"
            fail "agent can write $git_dir (.git is writable)"
        elif [ -w "$git_dir/config" ] || [ -w "$git_dir/refs/heads" ] \
                || [ -w "$git_dir/objects" ] || [ -w "$git_dir/hooks" ]; then
            fail "agent can write inside $git_dir"
        else
            pass "agent cannot write $git_dir"
        fi
    done
    for path in "$runtime" "$runtime/equipa" "$launcher"; do
        [ -n "$path" ] || continue
        if [ -w "$path" ]; then fail "agent can write $path"; else
            pass "agent cannot write $path"; fi
    done

    # --- own cgroup with limits --------------------------------------------
    local cgroup pids_max memory_max
    cgroup="$(sed -n 's/^0:://p' /proc/self/cgroup)"
    case "$cgroup" in
        */equipa-agent-*.scope) pass "agent is in its own cgroup $cgroup" ;;
        *) fail "agent is not in an equipa-agent scope (cgroup '$cgroup')" ;;
    esac
    pids_max="$(cat "/sys/fs/cgroup$cgroup/pids.max" 2>/dev/null)"
    if [ -n "$pids_max" ] && [ "$pids_max" != "max" ] \
            && { [ -z "$pids_expected" ] || [ "$pids_max" = "$pids_expected" ]; }; then
        pass "cgroup pids.max is $pids_max"
    else
        fail "cgroup pids.max is '${pids_max:-unreadable}', expected ${pids_expected:-a limit}"
    fi
    memory_max="$(cat "/sys/fs/cgroup$cgroup/memory.max" 2>/dev/null)"
    if [ -n "$memory_max" ] && [ "$memory_max" != "max" ]; then
        pass "cgroup memory.max is $memory_max"
    else
        fail "cgroup memory.max is '${memory_max:-unreadable}'"
    fi
    if (echo max > "/sys/fs/cgroup$cgroup/pids.max") 2>/dev/null; then
        fail "agent can raise its own pids.max"
    else
        pass "agent cannot raise its pids.max"
    fi
    # Leaving the scope needs write access to some other cgroup.procs; one
    # also exists if the agent user has its own systemd user manager
    # (lingering or a login session), which would let it start units that
    # outlive the agent.
    local writable_procs
    writable_procs="$(find /sys/fs/cgroup -name cgroup.procs -writable 2>/dev/null | head -n 3 | tr '\n' ' ')"
    if [ -n "$writable_procs" ]; then
        fail "agent can move processes into other cgroups: $writable_procs"
    else
        pass "agent cannot leave its scope (no writable cgroup.procs)"
    fi

    # --- nothing outlives the unit: no cron, no at, no lingering -----------
    # A scheduled job or a lingering user manager runs outside the scope, so
    # cgroup.kill never ends it (review R3136-04).
    local scheduler scheduler_output linger
    for scheduler in crontab at; do
        command -v "$scheduler" >/dev/null 2>&1 || continue
        if scheduler_output="$(LC_ALL=C "$scheduler" -l 2>&1)"; then
            fail "agent user can use $scheduler ($scheduler -l succeeded)"
            continue
        fi
        case "$scheduler_output" in
            *"not allowed"*|*[Pp]ermission*|*"not permitted"*)
                pass "agent user cannot use $scheduler" ;;
            *) fail "agent user can use $scheduler ($scheduler -l: $(printf '%s' "$scheduler_output" | head -n 1))" ;;
        esac
    done
    linger="$(loginctl show-user "$user" --property=Linger --value 2>/dev/null)"
    if [ -e "/var/lib/systemd/linger/$user" ] || [ "$linger" = "yes" ]; then
        fail "lingering is enabled for $user (a user manager outside the scope)"
    else
        pass "lingering is off for $user"
    fi

    # --- network: no host address, local service or LAN target (F2, N1) ----
    # The launcher already refused unless its own listeners on loopback and
    # on the host's addresses in the denied ranges were unreachable; this
    # covers every host address, whatever its range, the real services and
    # the operator's LAN targets.
    check_network "${host_addresses[*]}" "${deny_ports[*]}" "${lan_targets[*]}"
    check_broadcast 255.255.255.255

    # --- disk: a TMPDIR of its own, no shared /tmp on / (F8, SR3147-01) ------
    # $root_fs is / unless --root-fs names another root; every directory
    # below is then read below that root ($root_prefix is empty for /).
    local root_prefix="${root_fs%/}"
    check_unit_tmpdir "$root_fs"
    check_shared_tmp "$root_fs" "$root_prefix/tmp" "$root_prefix/var/tmp"
    check_world_writable_dirs "$root_fs" "${WELL_KNOWN_WORLD_WRITABLE[@]/#/$root_prefix}"
    note_search_only_dirs "$root_prefix/tmp" "$root_prefix/var/tmp"
    check_shm_cap /dev/shm "$AGENT_SHM_CAP_MB"

    # --- cannot signal the orchestrator --------------------------------------
    if [ -n "$orchestrator_pid" ]; then
        if kill -0 "$orchestrator_pid" 2>/dev/null; then
            fail "agent can signal the orchestrator (pid $orchestrator_pid)"
        else
            pass "agent cannot signal the orchestrator"
        fi
    fi

    # --- TheForge view: read-only, no excluded tables -------------------------
    if [ -n "$view_db" ] && [ -e "$view_db" ]; then
        if [ -w "$view_db" ] || [ -w "$(dirname -- "$view_db")" ]; then
            fail "agent can write the TheForge view or its directory"
        else
            pass "TheForge view is read-only to the agent"
        fi
        local table count
        for table in "${excluded_tables[@]}"; do
            count="$(python3 -I -c 'import sqlite3, sys; con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True); print(con.execute("SELECT count(*) FROM sqlite_master WHERE lower(name) = lower(?)", (sys.argv[2],)).fetchone()[0])' "$view_db" "$table" 2>/dev/null)"
            if [ "$count" = "0" ]; then
                pass "TheForge view has no $table table"
            else
                fail "TheForge view has the $table table (or is unreadable: '$count')"
            fi
        done
    elif [ -n "$view_db" ]; then
        fail "TheForge view $view_db does not exist"
    fi

    # --- environment: only the OAuth token -----------------------------------
    local name leaked=""
    for name in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
        case "$name" in
            CLAUDE_CODE_OAUTH_TOKEN) ;;
            *TOKEN*|*_KEY|*SECRET*|*PASSWORD*|*PASSWD*|DATABASE_URL|PG*|ANTHROPIC_*)
                leaked="$leaked $name" ;;
        esac
    done
    if [ -n "$leaked" ]; then fail "credential-like variables reach the agent:$leaked"
    else pass "no credential but CLAUDE_CODE_OAUTH_TOKEN in the agent environment"; fi
    if [ "${HOME:-}" = "$orchestrator_home" ]; then
        fail "agent HOME is the orchestrator's HOME"
    fi

    # --- per-unit HOME: nothing one agent leaves reaches the next ------------
    local passwd_home
    passwd_home="$(getent passwd "$user" | cut -d: -f6)"
    if [ -n "$passwd_home" ] && [ -w "$passwd_home" ]; then
        fail "agent can write its passwd HOME $passwd_home (files planted there reach later agents)"
    else
        pass "agent cannot write its passwd HOME ${passwd_home:-(none)}"
    fi
    case "${HOME:-}" in
        */.equipa-agent/equipa-agent-*/home) pass "agent HOME is its own unit's $HOME" ;;
        *) fail "agent HOME '${HOME:-}' is not a per-unit HOME" ;;
    esac
    case "${CLAUDE_CONFIG_DIR:-}" in
        "$HOME"/*) pass "CLAUDE_CONFIG_DIR is inside the unit HOME" ;;
        *) fail "CLAUDE_CONFIG_DIR '${CLAUDE_CONFIG_DIR:-}' is not inside the unit HOME" ;;
    esac
    case "${GIT_CONFIG_GLOBAL:-}" in
        */.equipa-agent/equipa-agent-*/gitconfig) pass "GIT_CONFIG_GLOBAL is the unit's own file" ;;
        *) fail "GIT_CONFIG_GLOBAL '${GIT_CONFIG_GLOBAL:-}' is not the unit's own file" ;;
    esac

    if [ "$failures" -eq 0 ]; then echo "RESULT: PASS"; else
        echo "RESULT: FAIL ($failures failed)"; fi
    return 0
}

# Sourced (the tests call the check functions with fake inputs): define
# the functions and run nothing.
if (return 0 2>/dev/null); then
    return 0
fi

if [ "${1:-}" = "--inside" ]; then
    shift
    inside "$@"
    exit 0
fi

if [ "$(id -u)" -eq 0 ]; then
    echo "FAIL run this as the orchestrator user, not root" >&2
    exit 2
fi
self="$(readlink -f -- "$0")"
runtime="$(dirname -- "$(dirname -- "$self")")"
cd -- "$runtime" || exit 2
check_loaded_firewall
# The probe's own RESULT line is held back and folded into the one final
# RESULT line below, so a passing probe next to a failing firewall check
# never prints "RESULT: PASS" (R5 of the 3156 review).
probe_verdict=""
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        "RESULT: "*) probe_verdict="${line#RESULT: }" ;;
        *) printf '%s\n' "$line" ;;
    esac
done < <("${EQUIPA_PYTHON:-python3}" -m equipa.isolation --verify-probe "$self" "$@")
wait "$!"
status=$?
reasons=""
case "$probe_verdict" in
    PASS) ;;
    "FAIL ("*")") reasons="${probe_verdict#FAIL (}"; reasons="${reasons%)}" ;;
    "") [ "$status" -ne 0 ] && reasons="the isolation check exited $status without a verdict" ;;
    *) reasons="probe: $probe_verdict" ;;
esac
if [ "$probe_verdict" = "PASS" ] && [ "$status" -ne 0 ]; then
    reasons="the isolation check exited $status"
fi
if [ "$failures" -gt 0 ]; then
    reasons="${reasons:+$reasons; }$failures orchestrator-side firewall rule check(s) failed"
fi
if [ "$status" -eq 0 ] && [ "$failures" -eq 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL ($reasons)"
    [ "$status" -eq 0 ] && status=1
fi
exit "$status"
