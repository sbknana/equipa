#!/usr/bin/env bash
# verify_agent_isolation.sh - operator check for EQUIPA agent isolation.
#
# Copyright 2026 Forgeborn
#
# Run it AS THE ORCHESTRATOR USER (not root) after following
# docs/AGENT_ISOLATION.md, from the EQUIPA runtime checkout, with the same
# environment the orchestrator runs with (THEFORGE_DB, the OAuth token):
#
#     scripts/verify_agent_isolation.sh [--repo /path/to/project ...]
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
#   * has no credential in its environment except CLAUDE_CODE_OAUTH_TOKEN.
# Every check prints PASS or FAIL; the last line is RESULT: PASS|FAIL.
# Exit status: 0 all checks passed, 1 a check failed, 2 isolation could not
# be established at all.
#
# This is a real-host check on purpose: the unit tests cannot create users,
# sudoers rules or cgroups, so nothing here is simulated or skipped.

set -u

inside() {
    local orchestrator_pid="" orchestrator_home="" database="" runtime=""
    local launcher="" pids_expected="" view_db="" mcp_config=""
    local -a git_dirs=() excluded_tables=() deny_dirs=() db_copies=()
    local -a secret_roots=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --deny-dir) deny_dirs+=("$2"); shift 2 ;;
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
            *) shift ;;
        esac
    done
    local failures=0
    pass() { echo "PASS $*"; }
    fail() { echo "FAIL $*"; failures=$((failures + 1)); }

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
    local group
    for group in root sudo admin wheel adm docker lxd incus libvirt kvm disk shadow systemd-journal; do
        if id -nG | tr ' ' '\n' | grep -qx -- "$group"; then
            fail "agent user is in the privileged group $group"
        fi
    done

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
exec "${EQUIPA_PYTHON:-python3}" -m equipa.isolation --verify-probe "$self" "$@"
