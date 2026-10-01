# Agent isolation: a separate no-sudo user, a per-agent cgroup and clone

Copyright 2026 Forgeborn

Feature flag: `features.agent_isolation` (default **off**). Code:
`equipa/isolation.py` (orchestrator side), `equipa/agent_launcher.py`
(`--isolated`, agent side). Operator check: `scripts/verify_agent_isolation.sh`.

## Why

Without this flag every agent runs as the orchestrator's own user. On a host
where that user has passwordless sudo, an agent, or a prompt injection that
reaches one, can:

* read every credential (the TheForge `api_keys` table through the agent's
  own `theforge` MCP server, `~/.claude`, `.env`, the MCP config's tokens);
* `sudo` to root;
* edit the EQUIPA runtime, `~/.gitconfig` and the main repository's `.git`
  (config, hooks, refs), which steers what the orchestrator's own git and the
  security reviewer see (SR3116-01/02);
* SIGKILL its own launcher and escape the process sweep with a fork-hopper
  (CT-01/CT-02).

Environment scrubbing, `PR_SET_DUMPABLE`, the bash checker and the git config
pins are defence in depth for a shared UID. They cannot close this class,
because every new git setting or process trick reopens it. A different UID
closes it: the kernel enforces the boundary, not EQUIPA.

## Design

```
orchestrator (UID orch)
 └─ systemd-run --user --scope  →  transient unit equipa-agent-<pid>-<start>-<hex>.scope
     │                             TasksMax / MemoryMax / MemorySwapMax=0 / CPUWeight /
     │                             IOWeight / IPAddressDeny, cgroup owned by orch
     └─ sudo -n -u equipa-agent -- /usr/bin/python3 -I <runtime>/equipa/agent_launcher.py --isolated
         └─ agent_launcher (UID equipa-agent)  ← stdin: handoff, then stop channel
             └─ claude (cwd = the agent's own clone)
                 └─ tool shells, MCP servers, anything the agent starts
```

### Launch: a user-manager scope plus a sudoers rule with exact arguments

There were two candidate launch paths:

1. **Chosen: `systemd-run --user --scope` + `sudo -n -u <agent>`.** The
   orchestrator's own systemd user manager creates a transient scope and
   applies `TasksMax` (pids.max), `MemoryMax` and `CPUWeight`. Inside the
   scope, `sudo` switches to the agent user and runs the launcher. The sudoers
   rule allows exactly one command line,
   `python3 -I <launcher> --isolated`, with no wildcard. The CLI argv,
   environment, files and repository do not travel as arguments. They go
   over stdin (the handoff below), so the rule needs no `*`.
2. **Rejected: `systemd-run --uid=<agent>` against the system manager.**
   Creating system units needs root. Granting it through sudo or polkit
   (`org.freedesktop.systemd1.manage-units`) is root-equivalent: whoever can
   start a transient unit can start it as root. A root-owned wrapper that
   validates arguments would work, but it adds root-side code to maintain,
   and killing or stopping the unit would need root again.

Why the chosen path holds:

* **The cgroup belongs to the orchestrator.** The scope's cgroup lives under
  the orchestrator's `user@<uid>.service`, owned by the orchestrator's UID.
  The orchestrator writes `cgroup.kill` directly, with no sudo. The agent's
  UID has no write access to any `cgroup.procs`, so it can neither leave the
  scope nor raise `pids.max`. A cgroup delegated to the *agent* user (the
  agent's own user manager) would let it do both, and would let it start
  units that outlive it. The agent user therefore must not have lingering or
  a login session.
* **`!pam_session` in the sudoers rule.** PAM session modules
  (`pam_systemd`) would otherwise move the agent into a new session scope
  outside ours. The launcher checks its own cgroup anyway and refuses if it
  was moved.
* **Signals stop at the UID boundary.** The agent can signal only its own
  UID. It cannot reach the orchestrator, sudo or other users' processes.
  It *can* kill its own launcher (CT-01). That no longer matters: the cgroup,
  not the launcher, is the containment boundary. The orchestrator kills
  whatever is left in the scope as soon as the launcher is gone
  (`kill_leftovers_when_launcher_exits`).
* **`cgroup.kill` is race-free against `fork()`** (Linux 5.14+). A pid-hopper
  that escapes a `/proc` walk dies with everything else in the scope (CT-02).

### The handoff (stdin)

The first line is `EQUIPA-HANDOFF 1 <header bytes> <bundle bytes>`. A JSON
header and a git bundle of the task worktree follow. The header carries:

* the CLI argv, with `argv[0]` replaced by `agent_isolation.claude_executable`;
* the agent's environment: the allowlisted variables
  (`env_loader.build_agent_env`) minus everything credential-shaped and
  everything that points into the orchestrator's HOME or runtime directory,
  plus `CLAUDE_CODE_OAUTH_TOKEN`. The launcher sets USER, LOGNAME, SHELL and
  the per-unit HOME, `CLAUDE_CONFIG_DIR`, XDG directories, TMPDIR and
  `GIT_CONFIG_GLOBAL` described below;
* the *contents* of the files the orchestrator wrote for the CLI (system
  prompt, `--settings`, `--mcp-config`). The originals are private to the
  orchestrator. The launcher writes 0600 copies in the agent's state
  directory;
* what the launcher must verify: agent user name, orchestrator UID, forbidden
  groups, cgroup path and limits, paths it must not read (`deny_read`) or
  write (`deny_write`), and paths it must be able to execute or read (hook
  programs and scripts, MCP server commands, skill directories).

After the handoff, stdin stays open as the **stop channel**. EOF (the
orchestrator closed it, or died) is handled like SIGTERM. The launcher
forwards it to the CLI, SIGKILLs the CLI after the grace period, sweeps and
exports. This replaces `PR_SET_PDEATHSIG`, which does not cross the sudo
boundary.

Before the CLI starts, the launcher writes exactly one status line to
stdout: `{"type": "equipa_isolation", "status": "ready"}` or `"refused"` with a
reason. The orchestrator consumes that line before `agent_runner` reads the
CLI's stream.

### Git: per-agent clones (implemented) versus shared objects with ACLs

| | Per-agent clone via bundles (**implemented**) | Linked worktree + ACLs on the shared `.git` |
|---|---|---|
| What the agent can write | only its own repository in its HOME | `objects/`, `refs/`, `logs/`, the worktree admin dir, and the `.git` directory itself (lock files, `packed-refs` rewrites) |
| Can it move `main` or edit `config`/`hooks`? | no: it has no access to the main `.git` at all | yes in practice. `packed-refs` is one file for every ref, and ref/config updates rename lock files inside `.git`, which needs write access to the directory. Anyone who can create `config.lock` can replace `config`. Per-file ACLs cannot express "only my branch". |
| What the orchestrator trusts from the agent | one bundle file: data, copied with `O_NOFOLLOW`, size-capped, fetched with `transfer.fsckObjects` into a private ref, then compare-and-swap on the task branch only | every ref and object the agent wrote into the shared repository |
| Git config/attributes the orchestrator's git reads | orchestrator-owned only (SR3116-01/02 closed for config) | agent-writable (the SR3116 class stays open) |
| Cost | bundling the branch history per dispatch (the size of the history; mostly pack reuse), plus a checkout per agent | none |

The ACL scheme cannot be made safe, because git needs directory-level write
access to `.git` to update any ref. Per-agent clones are implemented:

1. The orchestrator records the task worktree's state as a **state commit**:
   first parent is the task branch tip, tree is the full working tree
   (uncommitted work of an earlier attempt and `carry_ignored_paths`
   included). It bundles that commit.
2. The launcher `git init`s a private repository in
   `~<agent>/.equipa-agent/<unit>/repo`, fetches the bundle, points the task
   branch at the first parent and checks out the state tree. Uncommitted files
   stay uncommitted.
3. The CLI runs there. Every mention of the orchestrator worktree path in the
   argv and in the handed-over files is rewritten to the clone's path.
4. When the CLI exits and the tree has been swept, the launcher records the
   clone the same way and writes `refs/equipa/worktree-state` as a bundle to
   the exchange directory (`--not <base>`).
5. The orchestrator copies the bundle privately and verifies it. It then
   fetches the single ref into `refs/equipa/isolation-import/<unit>` and moves
   **only** the task branch with `update-ref <branch> <tip> <dispatch base>`,
   which refuses if the branch moved meanwhile. It restores the working tree
   (`read-tree -u --reset <state>`, then `reset`), so the review, gate and
   merge pipeline sees exactly what the agent left. Finally it deletes the
   import ref.

The branch tip travels as a parent of the state commit, not as its own
bundle ref. `git bundle create` silently drops a ref whose tip equals the
excluded base, which is the usual case when an agent made no commit.

Isolation refuses a worktree that is not the root of a linked worktree, has a
detached HEAD, or is on the main checkout's branch. Agent work is imported
into a task branch only, never into the default branch.

A helper agent without a project directory (reflexion) runs in an empty
private directory. Nothing is cloned or exported for it.

Before `read-tree` touches the worktree, the orchestrator lists the tree of
every commit of the export (the state commit, the committed task-branch tip
that becomes the task branch and, when present, the clone's HEAD) and
refuses the import (reviews ISO-04, R3136-02) when:

* a symbolic link sits at, above or below `.equipa-artifacts` or any
  `carry_ignored_paths` entry. `read-tree -u --reset` would otherwise replace
  the orchestrator's own ignored artifacts directory with the agent's link,
  and the orchestrator later writes review files into it as its own user.
  A link that is only committed would reach later checkouts of the branch
  (and of the default branch after a merge) instead;
* a link the agent added or changed is absolute or resolves out of the
  tree. The target is resolved the way the kernel would, following the
  tree's other links (`a/..` through a link `a` goes where `a` points), with
  at most 40 hops (review R3136-05). Links already in the dispatch base are
  the project's own and are kept;
* the check itself would cost too much (review R3140-01, F3): a changed
  link's target is longer than 4095 bytes (PATH_MAX - 1; the kernel could
  never create it), a tree holds more than 10,000 links or a commit adds or
  changes more than 1,000, or the walk of all links of one commit takes more
  than 1,000,000 path components. Target sizes come from `git ls-tree -l`,
  so an oversized target is refused before it is read, and the walk looks
  each component up in a trie of the tree's links, so it is linear in the
  components it visits (it joined the directory path per component before,
  which made one long target quadratic: hours for a few MB, while the unit
  held its slot). Targets are read once per blob.

The import also refuses a committed task-branch tip that does not descend
from the dispatch base (`git merge-base --is-ancestor`, review F5).
`update-ref <branch> <tip> <base>` compares only the old value, so without
it an agent could rewind the branch to an ancestor of the base or replace
it with unrelated history, dropping commits an earlier review saw.

The task branch has not moved at that point, so a refused import leaves the
worktree exactly at the dispatch base.

### Per-unit HOME: nothing one agent leaves reaches the next

All isolated agents share one UID, so a shared HOME would let a developer
agent plant `~/.claude/settings.json` hooks, `~/.claude/CLAUDE.md`,
`~/.gitconfig` filters or aliases, shell rc files or poisoned caches that a
later tester or security reviewer loads (review ISO-02, GP2-02, SR3116).
Therefore:

* the launcher gives every unit a fresh, empty HOME at
  `~<agent>/.equipa-agent/<unit>/home` and points `CLAUDE_CONFIG_DIR`,
  `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and
  TMPDIR into the unit's directory. Values for them in the handoff are
  ignored;
* the CLI's `GIT_CONFIG_GLOBAL` is `~<agent>/.equipa-agent/<unit>/gitconfig`,
  holding only the git identity the orchestrator handed over. The launcher's
  own git (clone, export) runs with `GIT_CONFIG_GLOBAL=/dev/null`, so not
  even the unit's own global config (which the agent could edit while it
  ran) applies to the recorded export;
* the unit's directory is removed when the unit ends. If the export fails,
  only the clone is kept for recovery; the HOME, the git config, the
  handed-over files (the system prompt with the review nonces) and TMPDIR
  are removed;
* the passwd HOME of the agent user must **not** be agent-writable (the
  launcher refuses otherwise). It is root-owned and holds only the
  operator-created state root `.equipa-agent` and the exchange directory.
  Some programs ignore `$HOME` (ssh reads `~/.ssh/config` from the passwd
  entry), so a writable passwd HOME would be a persistence channel even
  with a per-unit `$HOME`.

The CLI passes `--setting-sources "" --strict-mcp-config` (tasks 3134 and
3153): no user, project or local settings file is read, only EQUIPA's own
`--settings` file, which wires the PreToolUse gate.

**The unit's config directory is agent-writable (R3150-07).** The
launcher creates `<unit home>/.claude` as the agent user, and the CLI must
write its state there, so nothing on the filesystem stops the agent adding a
`settings.json` to it mid-run. The CLI ignores that file only because of the
empty `--setting-sources` value, so the launcher enforces it inside the unit:
`materialize_argv` refuses a claude argv whose `--setting-sources` is
missing, has no value or names any source (`setting_sources_refusal` in
`equipa/agent_launcher.py`), and the run never starts. Other files the CLI
writes there and reads back later, the shell snapshot in `shell-snapshots/`
among them, stay writable by the agent, so a per-unit HOME does not make the
Bash gate a boundary against the agent's own in-run writes; see "Bash gate
limitations without agent isolation" in [ORCHESTRATOR.md](ORCHESTRATOR.md).

### Every unit runs alone (until there is a UID pool)

A per-unit HOME does not help against a unit running *at the same time*:
every unit has the same UID, so a developer agent of task A could read the
handed-over prompt (with the provenance and completion nonces) of a security
reviewer running for task B, and write into that reviewer's clone, which is
exported into task B's worktree (review R3136-03, GP2-02). The same holds
for every other role (review F4): a developer unit of task B can watch the
shared exchange directory and replace task A's export between the launcher
writing it and the orchestrator importing it, or edit task A's tester clone
and so forge the "tests pass" verdict the gate trusts. Until the agents get
separate UIDs (`isolation.PER_UNIT_UIDS` is false: no UID pool exists), **no
isolated unit overlaps any other**, whatever its role
(`isolation.unit_runs_alone`):

* every unit waits until every running isolated unit has ended, and until
  no agent scope of the orchestrator user is populated (a unit that survived
  `cgroup.kill`, or one started by an orchestrator without the lock; a scope
  whose orchestrator is gone is killed by the sweep instead of waited for);
* while a unit waits or runs, every new unit waits (writer preference, so
  a steady stream of units cannot starve one that waits).

With the flag on, a `max_concurrent` above 1 is **refused** at dispatch
(`--tasks`, `--auto-run`, `--parallel-goals`; the CLI's
`--max-concurrent` and the dispatch config's `max_concurrent` alike):
tasks could not run side by side anyway, and would only queue for the unit
slot until `unit_wait_timeout_sec` refused them. Set `"max_concurrent": 1`.
A per-unit UID pool would lift both; it is not implemented, so there is no
setting for it (an unknown `agent_isolation` key is refused), and a dispatch
config that sets `isolation_uid_pool` is refused at startup and at every
isolated spawn rather than read as leave to raise `max_concurrent`
(`isolation.uid_pool_refusal`). Once a pool
exists, only `security-reviewer` and `code-reviewer` units
(`EXCLUSIVE_ROLES`) keep running alone.

It is a reader-writer lock over two `flock` files in the orchestrator
user's private runtime directory, so it covers every dispatch mode and
every orchestrator process of the same user, not only one event loop. The
directory is always `/run/user/<uid>`, never taken from `XDG_RUNTIME_DIR`:
the variable is only inherited, and a process started with another value
(a stale tmux or `sudo` shell) would lock files no other orchestrator
process sees. That directory must be the orchestrator user's own, not a link, and
writable by no one else, or the unit is refused: whoever can write it can
unlink a lock file a running unit holds, and the next reviewer would lock a
fresh file beside that unit. The unit's slot is taken before the handoff is built and given back
when the agent handle is released, after its cgroup was emptied and its
export imported; a refused setup gives it back at once. The role comes from
`build_cli_command(role=...)`. Waiting is logged (`[Isolation] ... waits
for ...`, repeated every 5 minutes, and `... runs alone`); a wait longer than
`unit_wait_timeout_sec` (default 6 h) refuses the dispatch. The cost is
throughput: one isolated agent at a time on the host.

**Long-term fix:** a pool of agent users, one UID per unit, or at least a
reviewer UID that no developer, tester or debugger unit ever shares. Each
unit's handed-over files, clone and export would then be closed to every
other unit by ordinary file permissions, ordinary units could run side by
side again, and only the reviewers would keep the lock.

### The orchestrator never executes agent output

With the flag on, the orchestrator does not run project files as its own
user (review ISO-01). `preflight.py` refuses, and logs why:

* `auto_install_dependencies` (venv/pip, `npm install`, `go mod download`):
  skipped. The agent's clone has no gitignored dependency trees anyway;
* `preflight_build_check` (`npm run build`, `npx tsc`, `go build`,
  `dotnet build`, `py_compile`): reported as skipped, like a missing build
  tool, so no auto-fix is started;
* `_handle_preflight_failure` (auto-fix): returns
  `agent_isolation_refused` without dispatching a debugger, because the
  re-check that would confirm the fix is refused.

A build check inside the agent scope was rejected: the clone has no
`node_modules`, venv or restored packages, so it would report most projects
as broken and start paid auto-fix runs that cannot succeed. Behaviour change:
with isolation on there is no pre-dispatch build check. The agents still
build and test inside their own sandbox.

`rlm_decompose`, the ForgeSmith GHOST scout and OPRO, SIMBA and the
autoresearch prompt mutation start `claude` themselves, outside any
launcher, on text derived from agent output. With the flag on they refuse
(CT-04, ISO-06, R3136-06); SIMBA and autoresearch run standalone without an
importable `equipa` refuse as well. Autoresearch no longer goes through
`bash -c`: the argv runs directly with the prompt on stdin and the
`--setting-sources "" --strict-mcp-config` pair.

The Ollama provider (`provider` or `provider_<role>` set to `ollama`, or
`--provider ollama`) runs the model's tool calls, `bash_write` included,
in the orchestrator's own process; it never reaches the isolated launcher.
With the flag on it is refused (review R3136-01): `dispatch_agent` returns a
blocked result (`RESULT: blocked`, `Ollama agent refused: agent_isolation is
on ...`) before `run_ollama_agent` is imported, and `run_ollama_agent`
refuses by itself as well, which covers its `__main__` demo. Use the Claude
provider for every role while isolation is on; routing Ollama's tools
through the launcher is future work.

Source fences keep this true (`tests/test_agent_isolation_3140.py`): every
`run_ollama_agent` call and every process spawn in a function that names
the Claude CLI (argv literal or a `claude ...` shell string) must sit behind
an `if` on the refusal that returns or raises first, and every `shell=True`,
`create_subprocess_shell`, `os.system`, `exec` or `eval` site must be in a
short, reasoned allowlist (operator hooks, the RLM REPL behind its refused
outer call, the Ollama tools behind their refused loop). Operator hooks run
in the task worktree, so they carry their own condition (see residual
risks).

### TheForge: read-only view without `api_keys`

When an agent is spawned, the orchestrator copies the database that the
`theforge` MCP server's `--db-path` names to `agent_isolation.view_db_path`,
using SQLite's backup API. In the copy it drops the `exclude_tables` (default
`api_keys`) and every view or trigger that mentions them, with
`secure_delete` plus `VACUUM` so no dropped row survives in free pages. It
switches the copy to rollback-journal mode, sets it `0444` and publishes it
with an atomic rename. The agent's MCP config points `--db-path` at the copy.
The copy's directory is not agent-writable (the launcher checks), so any
write fails at the SQLite level. MCP servers not listed in
`allowed_mcp_servers` (default: only `theforge`) are removed from the agent's
MCP config. An allowlisted server that passes credential-like environment
variables refuses the dispatch: the agent's own processes can read them.

**Behaviour change:** isolated agents cannot write TheForge (decisions,
open_questions, session_notes). Their results still reach the orchestrator
through the RESULT block.

### Network: no loopback, no LAN

A separate UID closes the file system, not the network. Services that
listen on `127.0.0.1` (databases, Redis, admin HTTP APIs) often trust any
local user, so an agent could reach them around every file permission
(review F2). Every unit is therefore started with

```
IPAddressDeny=127.0.0.0/8 ::1/128 0.0.0.0/8 ::/128 169.254.0.0/16 fe80::/10
              224.0.0.0/4 ff00::/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7
```

(loopback, the unspecified addresses, which a connect turns into loopback,
link-local, multicast and the private ranges), plus any
`ip_address_deny_extra`. The list can only grow. Everything else stays
reachable: the agent needs the public Anthropic API. **On a host running
Tailscale (or any other CGNAT overlay), add `"ip_address_deny_extra":
["100.64.0.0/10"]`** (step 7): a tailnet address of the host and every
tailnet peer lie in that range, outside the private ranges, and only a
listed range is probed by the launcher. The ranges are spelled out rather
than given as systemd's symbolic names (`localhost`, `link-local`,
`multicast`) because the same list goes to the launcher, which parses each
entry as an address range.

**On many hosts `IPAddressDeny=` does nothing, and the nftables rule is the
real control.** A user manager accepts the property but cannot apply it:
BPF firewalling needs privileges the orchestrator's user manager does not
have, so systemd logs "unit configures an IP firewall, but not running as
root" (or nothing) and starts the unit without it (observed on systemd 255:
inside such a scope `127.0.0.1:22`, a local database port and the host's
own LAN address were all reachable). The property alone therefore proves
nothing. The blocking is done by an nftables rule for the agent user's UID
(runbook step 4a), which also rejects **every address of the host,
whatever its range** (`fib daddr type local reject`): a service bound to
`0.0.0.0` answers on a tailnet, public or global IPv6 address of the host
too, and those lie outside every private range.

The launcher checks from inside the unit, as the agent user, before the CLI
starts: it binds a listener on `127.0.0.1` and on each of this host's own
addresses that falls in a denied range (from `/proc/net/fib_trie`, and
`/proc/net/if_inet6` when IPv6 is enabled), connects to each, and **refuses
the run if any connection is established** within 2 s. So until the
nftables rule exists, every dispatch is refused: the check fails closed,
and it accepts either mechanism. The verify script (step 8) goes further,
as the agent: it connects to a listener of its own on **every** address of
the host (loopback, LAN, tailnet, public), to every port that listens on
loopback or on every address and every `--loopback-port` at `127.0.0.1` and
at each host address, and to each `--lan-target` the operator lists (other
LAN machines, which no listener of the host's own can stand in for).

### Resources: memory without swap, IO weight, export size

Besides `TasksMax`, `MemoryMax` and `CPUWeight` (review F8):

* `MemorySwapMax=0`: `MemoryMax` alone lets a unit spill into swap. The
  orchestrator refuses a scope whose `memory.swap.max` is not `0`;
* `IOWeight=<io_weight>` (default 50 against the default 100 of everything
  else). The orchestrator refuses a scope whose `io.weight` does not start
  with `default <io_weight>`; that needs the io controller delegated to the
  user manager (step 4). **A weight only has an effect with the BFQ
  scheduler or iocost** (`io.cost.qos` configured); the `none` and
  `mq-deadline` schedulers ignore it. On a host without io delegation, or
  whose disks use `none`/`mq-deadline` with no iocost, set `"io_weight":
  null` (IO unweighted, nothing verified) rather than delegating io for a
  weight nothing applies (step 4);
* the export: the launcher writes the bundle with `RLIMIT_FSIZE` set to
  `max_export_bytes` on the git process (SIGXFSZ ignored, so the write
  fails with EFBIG) and checks the size before it publishes the file, so an
  oversized export never lands in the shared exchange directory; the
  orchestrator still refuses to copy a larger one. This caps the
  launcher's own export, not a hostile agent: the agent user owns the
  exchange directory and can write there directly;
* disk: EQUIPA does not cap a unit's clone, HOME, TMPDIR or the exchange
  directory. Each unit gets a **TMPDIR of its own**
  (`<state root>/.equipa-agent/<unit>/tmp`, mode 0700, removed with the
  unit), and all of them live on the agent state root, which must be a
  size-capped filesystem of its own (step 1) so a unit cannot fill `/`.
  A private TMPDIR does not stop an agent writing `/tmp/x` or `/var/tmp/x`
  directly, and a scope cannot take systemd's `PrivateTmp=`, so step 1
  also closes the shared `/tmp` and `/var/tmp` to the agent user or caps
  them. The verify script fails while the unit's TMPDIR, or a shared
  `/tmp` or `/var/tmp` the agent can write, lies on the root filesystem.

### Fail closed

With the flag on, `_spawn_agent_process` hands every agent to
`isolation.spawn_isolated_agent` (the Ollama provider, which never reaches
it, is refused before it runs anything). It never falls back to the same-UID
launcher. Each of the following refuses the dispatch
(`AgentDispatchRefused: agent isolation: ...`):

* the host requires isolation (the marker below) and the dispatch config
  turns it off;
* invalid, unknown or missing `agent_isolation` settings;
* the agent user is missing, is root, is the orchestrator's user, or is in a
  privileged group (`sudo`, `admin`, `wheel`, `adm`, `docker`, `lxd`,
  `incus`, `libvirt`, `kvm`, `disk`, `shadow`, `systemd-journal`, `root`;
  `privileged_groups` may add groups, never remove these), its primary or
  any supplementary group (`os.getgrouplist`) counting;
* `sudo -n -l -U <agent user>` does not report that the agent user "is not
  allowed to run sudo" (review F7): it lists rules for it, or the
  orchestrator may not list them (sudo needs the orchestrator to hold `ALL`
  or the `list` permission, step 6), or sudo is missing;
* not Linux, no cgroup v2, no systemd user manager for the orchestrator, a
  missing `sudo`/`systemd-run`/python/launcher/CLI/git, or an exchange
  directory that is missing, not owned by the agent user, or world-writable;
* no OAuth token, or a token file readable by group or others;
* the project directory is not the root of a linked task worktree;
* the launcher, python, CLI, git, the exchange or view directory, or a hook
  or MCP program lies inside the TheForge database directory or a
  `db_backup_dirs` entry (those must be closed to the agent);
* `max_concurrent` is above 1 (see "Every unit runs alone");
* `sudo` fails (no rule), or the scope does not appear within 15 s;
* the scope's `pids.max`/`memory.max`/`cpu.weight` differ from the settings,
  `memory.swap.max` is not `0`, `io.weight` is not `default <io_weight>`
  (unless `io_weight` is null), or `cgroup.kill` is missing or not writable;
* the launcher's own checks fail. It refuses when it runs as root, as the
  orchestrator's UID, as the wrong user or in a privileged group; when the
  agent user can write its passwd HOME; when the agent user lingers
  (`/var/lib/systemd/linger/<agent>`) or `crontab -l` / `at -l` do not
  report a permission denial (review R3136-04: a scheduled job or a user
  manager runs outside the scope and outlives the unit); when it runs outside the expected
  cgroup or with other limits; when it can read a `deny_read` path, where
  entering a directory (search permission) counts as reading it (the
  TheForge DB and its -wal/-shm both beside a symlink and beside its real
  target, the DB's real directory, every `db_backup_dirs` entry, the
  orchestrator's HOME and its `.claude`, `.config`, `.ssh`, `.gitconfig`,
  `.netrc`, ..., and `mcp_config.json`); when it can write a `deny_write`
  path (the EQUIPA
  runtime, the launcher, the CLI, git, python, the task worktree and its
  parent, the main `.git` and checkout, the view and its directory); when it
  can connect to its own listener on loopback or on one of the host's
  addresses in a denied range (see "Network"); when it cannot run a hook or
  MCP program; or when it cannot become non-dumpable or a child subreaper;
* no ready line within `setup_timeout_sec`;
* a unit waited longer than `unit_wait_timeout_sec` for the units before
  it, or the unit lock in the runtime directory cannot be opened.

The Ollama provider is refused with a blocked result rather than an
`AgentDispatchRefused` (see above).

The feature flag is in `FAIL_CLOSED_FEATURE_FLAGS`. An invalid value
(`"yes"`) or an unreadable dispatch config turns it **on**, which refuses
dispatch until isolation is configured. A typo therefore never runs agents
as the orchestrator's user.

### Once on, it stays on

An unreadable config is not the only way the flag could read as off
(review F1). `--dispatch-config FILE` is merged over the built-in defaults,
where the flag is false, so a per-run config without the key, a mistyped
path or a missing file would silently run agents with the orchestrator's
user and its sudo. Two things keep the flag on:

* **The host marker.** While `/etc/equipa/require-agent-isolation` exists
  (any content; created by root, outside every repository, so neither a
  config nor an agent can remove it), isolation is *required*:
  `isolation_enabled()` is true whatever the config says, every refusal that
  depends on it (preflight, RLM, ForgeSmith, Ollama, ...) applies, and a
  dispatch whose config turns the flag off is refused at startup with
  `agent isolation is required on this host ...`, before any mode runs (and
  again at every isolated spawn). A marker that cannot be checked (a
  directory the orchestrator may not search) counts as present.
* **Per-run configs may only turn it on.** When `--dispatch-config` names
  another file than the host's own config (the one a run without the
  option loads), the host config's `agent_isolation` carries over: if the
  host has the flag on, the per-run config gets it on (a per-run `false` is
  overridden with a WARNING) and gets the host's `agent_isolation` section
  unless it has its own. A missing per-run file logs an ERROR and still
  carries the host's isolation. Every other key is the per-run file's.

The orchestrator prints `agent_isolation: ON`, `ON (required by ...)` or
`OFF` at startup.

## Findings

| Finding | Status (flag on) | Test |
|---|---|---|
| IR-08: every agent gets the theforge MCP server on the DB with `api_keys`; DB mode 0644 | FIXED: read-only view without `api_keys`, other servers dropped; the real DB is refused if the agent can read it, and since task 3136 also its directory and backup copies (ISO-03) | `test_view_db_drops_api_keys_and_their_rows`, `test_agent_mcp_config_points_forge_at_view_and_drops_others`, `test_launcher_denied_and_required_access`, verify script |
| Same UID with passwordless sudo | FIXED: separate user; privileged groups refused inside and outside; `sudo -n true` checked on the host | `test_agent_user_*`, `test_launcher_refuses_*`, `test_spawn_refused_by_the_real_launcher`, verify script |
| Agent can edit the runtime, `~/.gitconfig`, the main `.git` | FIXED: per-agent clone; the agent has no write access (launcher `deny_write`) | `test_clone_round_trip_imports_only_the_task_branch`, `test_import_refuses_when_branch_moved_since_dispatch`, `test_worktree_on_default_branch_refuses`, verify script |
| Only the needed credential reaches the agent | FIXED: `CLAUDE_CODE_OAUTH_TOKEN` only | `test_agent_env_keeps_only_the_oauth_token`, `test_isolated_agent_end_to_end`, verify script |
| CT-01: agent SIGKILLs its launcher | FIXED: the scope is killed as soon as the launcher is gone | real-host hostile probe (launcher SIGKILLed, C fork-hopper left behind, scope empty afterwards); `test_cgroup_kill_and_stale_scope_sweep` |
| CT-02: pid-hopper escapes the `/proc` sweep | FIXED: `cgroup.kill` | same real-host probe; `test_scope_verification` |
| CT-03: containment failures fail open | FIXED for setup: every failure refuses the dispatch. **NOT FIXED** for teardown: a scope that survives `cgroup.kill` (D-state) or a failed import is logged at ERROR but does not mark the run failed, because that needs result plumbing in `agent_runner.py`, which another task is editing | `test_spawn_refused_*`, `test_agent_runner_refuses_instead_of_falling_back` |
| CT-04: rlm_decompose spawns the CLI outside the launcher | FIXED: refuses with the flag on. ForgeSmith and SIMBA: see ISO-06 | `test_rlm_decompose_refuses_direct_cli_spawns` |
| SR3116-01/02: the reviewer's git view can be steered through agent-writable config | FIXED for the orchestrator's repository (no agent-writable git config, attributes or submodule config reaches it) and, since task 3136, for the reviewer's own view against *earlier* units (per-unit HOME and git config, ISO-02). Residual: in-tree `.gitattributes` in the reviewed commit still apply; *concurrent* units share the agent UID (see residual risks) | `test_clone_round_trip_imports_only_the_task_branch`, `test_consecutive_units_get_disjoint_empty_homes` |

Security review of task 3135 (fix-forward task 3136):

| Finding | Status (flag on) | Test |
|---|---|---|
| ISO-01 HIGH: preflight build checks and auto-install run agent-written project files as the orchestrator | FIXED: refused (installs skipped, build check reported as skipped, auto-fix not dispatched); flag off unchanged | `test_preflight_runs_no_project_code_with_isolation_on` (every spawn primitive recorded, 6 project types), `test_every_preflight_spawn_is_preceded_by_the_isolation_refusal` (source fence), `test_preflight_unchanged_with_isolation_off`, `test_preflight_refuses_when_the_config_is_unreadable` |
| ISO-02 HIGH: one shared agent HOME lets a developer persist into and forge the later review | FIXED for sequential units: per-unit empty HOME, `CLAUDE_CONFIG_DIR`, XDG dirs and `GIT_CONFIG_GLOBAL`, removed with the unit (also on a failed export); launcher git uses `GIT_CONFIG_GLOBAL=/dev/null`; the passwd HOME must be read-only to the agent. NOT FIXED for concurrent units (same UID; needs a UID pool); since task 3140 reviewer units never run concurrently with another unit (R3136-03) | `test_consecutive_units_get_disjoint_empty_homes`, `test_launcher_git_ignores_global_config_the_agent_planted`, `test_failed_export_still_removes_the_unit_home`, `test_launcher_refuses_an_agent_writable_passwd_home`, `test_isolated_agent_end_to_end`, verify script |
| ISO-03 HIGH: `api_keys` readable through TheForge backup copies; symlinked DB hides the real -wal/-shm | FIXED: the DB's real directory, the real -wal/-shm and every `db_backup_dirs` entry are in `deny_read`, and an enterable directory counts as readable; the verify script checks the directories, probes every copy the orchestrator finds by name, searches for readable copies as the agent, and checks directory and copy modes from outside | `test_deny_read_covers_db_directory_real_side_files_and_backups`, `test_launcher_refuses_an_enterable_deny_read_directory`, `test_database_copies_are_found_beside_the_db_and_in_backup_dirs`, `test_probe_command_names_every_directory_and_copy`, `test_outer_checks_fail_on_open_directories_and_copies`, `test_verify_script_fails_on_readable_db_directory_and_copies`, `test_verify_script_passes_a_closed_db_directory` |
| ISO-04 MEDIUM: imported state turns `.equipa-artifacts` into a symlink the orchestrator writes through | FIXED: the import refuses links at, above or below the artifacts dir and carry paths, and new or changed links that are absolute or leave the tree | `test_import_refuses_links_at_or_below_the_artifacts_dir`, `test_import_refuses_a_link_above_a_configured_carry_path`, `test_import_refuses_new_links_out_of_the_tree`, `test_import_accepts_in_tree_links_and_links_from_the_base` |
| ISO-05 MEDIUM: deny_read is a blocklist; other world-readable credentials stay readable | FIXED as far as a blocklist can be: the runbook requires project checkouts closed to others, and the verify script FAILS when the agent can read a secret-shaped file below `secret_scan_roots` or any `--repo` (an empty list fails verification). NOT FIXED: an allowlist model (mount namespace, `ProtectHome=`/`TemporaryFileSystem=` for the scope) | `test_verify_script_fails_on_readable_project_secrets`, `test_outer_checks_fail_on_open_directories_and_copies` |
| ISO-06 MEDIUM: ForgeSmith GHOST/OPRO and SIMBA start `claude -p` as the orchestrator on agent-derived text | FIXED: refused with the flag on (SIMBA also when `equipa` is not importable) | `test_forgesmith_cli_spawns_refuse_with_isolation_on`, `test_forgesmith_cli_spawns_unchanged_with_isolation_off`, `test_every_direct_claude_spawn_checks_isolation_first` (source fence) |
| ISO-07 LOW: a 0711 orchestrator HOME passes the read check | FIXED with ISO-03: entering a `deny_read` directory is refused; the verify script tests `-x` on the orchestrator HOME; the runbook no longer offers 0711 | `test_launcher_refuses_an_enterable_deny_read_directory` |
| ISO-08 LOW: teardown fails open (failed import, survivor of `cgroup.kill`) | NOT FIXED: needs result plumbing in `agent_runner.py`, outside this task's scope | none |
| ISO-09 LOW: deny_write is not recursive | NOT FIXED (LOW); the runbook's `chmod -R go-w <runtime>` prevents it | none |
| ISO-10 LOW, ISO-14 INFO: import and swap limits | NOT FIXED (denial of service only) | none |
| ISO-11 INFO: exchange directory inside the agent's HOME | MITIGATED: the passwd HOME is now root-owned, so the agent can no longer replace `exchange` with a link | `test_launcher_refuses_an_agent_writable_passwd_home` |
| ISO-12 INFO: the view is a blocklist copy of the whole database | NOT FIXED (information exposure, no credentials) | none |
| ISO-13 LOW: bundle fetches skip fsck on git < 2.46 | NOT FIXED: `read-tree -u` still refuses `.git`/`..` paths; see residual risks | none |

Security review of task 3136 (fix-forward task 3140). All tests are in
`tests/test_agent_isolation_3140.py`:

| Finding | Status (flag on) | Test |
|---|---|---|
| R3136-01 HIGH: the Ollama provider runs model-chosen shell commands as the orchestrator user | FIXED: refused with a blocked result in `dispatch_agent` (provider, `provider_<role>`, `--provider`) and again inside `run_ollama_agent`; flag off unchanged. Routing Ollama's tools through the launcher is future work | `test_ollama_provider_is_refused_with_isolation_on` (3 ways to select it), `test_ollama_provider_unchanged_with_isolation_off`, `test_run_ollama_agent_itself_refuses_with_isolation_on`, `test_run_ollama_agent_unchanged_with_isolation_off`, fences `test_every_run_ollama_agent_call_is_gated_by_the_refusal`, `test_run_ollama_agent_refuses_before_its_tool_loop`, `test_every_command_executing_entry_point_is_known`, `test_ollama_tool_execution_is_only_reached_through_run_ollama_agent` |
| R3136-02 MEDIUM: the link check ignores the committed tip that becomes the task branch | FIXED: the state commit, the tip and the clone's HEAD are each checked with the same rules | `test_import_refuses_a_committed_link_the_working_tree_dropped`, `test_import_refuses_a_committed_escaping_link`, `test_import_refuses_a_link_in_the_clones_detached_head`, `test_import_accepts_a_committed_in_tree_link` |
| R3136-03 MEDIUM: concurrent units share one UID under parallel dispatch | MITIGATED until a UID pool exists: reviewer units run alone (host-wide reader-writer lock, logged); since task 3142 every unit does (F4, see "Every unit runs alone"). Long-term fix: per-unit or per-role UIDs | `test_reviewer_waits_for_running_units_and_blocks_new_ones`, `test_reviewers_do_not_overlap_each_other`, `test_reviewer_waits_for_live_agent_scopes`, `test_units_in_another_process_are_waited_for`, `test_unit_lock_ignores_another_xdg_runtime_dir`, `test_wait_past_the_timeout_refuses_and_holds_nothing`, `test_spawn_holds_the_slot_until_the_agent_is_released`, `test_failed_setup_gives_the_slot_back`, `test_a_replaced_lock_file_cannot_let_a_reviewer_overlap`, `test_unit_lock_directory_must_be_private`, `test_build_cli_command_marks_the_role_for_the_isolated_spawn`, `test_reviewer_spawn_sites_build_their_command_with_the_role` |
| R3136-04 LOW: cron/at denial is only a runbook line | FIXED: the launcher refuses when the agent lingers or `crontab -l`/`at -l` are not denied; the verify script checks the same | `test_launcher_refuses_an_agent_that_may_use_cron`, `test_launcher_refuses_an_agent_that_may_use_at`, `test_launcher_refuses_a_lingering_agent_user`, `test_launcher_accepts_denied_cron_and_at`, `test_verify_script_fails_when_the_agent_may_schedule_jobs`, `test_verify_script_passes_denied_cron_and_at` |
| R3136-05 LOW: the out-of-tree link test is lexical | FIXED: links are resolved through the tree's other links, 40 hops at most | `test_link_escape_follows_links_of_the_tree`, `test_import_refuses_a_link_pair_that_resolves_outside`, `test_import_accepts_a_link_pair_that_stays_inside` |
| R3136-06 LOW: autoresearch starts `claude --print` through `bash -c` | FIXED: refused with the flag on (and when `equipa` cannot be imported); direct argv with stdin and the isolation args | `test_autoresearch_mutation_refuses_with_isolation_on`, `test_autoresearch_mutation_runs_the_cli_without_a_shell`, `test_every_direct_claude_spawn_is_gated_by_the_refusal` |
| R3136-07 INFO: the fences prove the refusal exists, not that it gates the spawn | FIXED: the new fences require an `if` on the refusal that returns or raises before the spawn, in an enclosing block | `test_gate_detection_needs_an_exit_before_the_spawn`, `test_every_preflight_spawn_is_gated_by_the_refusal`, `test_every_direct_claude_spawn_is_gated_by_the_refusal`, `test_claude_fence_sees_a_shell_string_spawn` |
| R3136-08 INFO: the database-copy search covers only the configured directories | FIXED: SQLite files below `secret_scan_roots` and `--repo` whose schema holds an excluded table are probed by name as the agent, and a world-readable one fails the outer check | `test_credential_databases_below_project_roots_are_found`, `test_probe_command_probes_credential_copies_as_the_agent`, `test_outer_checks_fail_on_a_world_readable_credential_copy` |

Independent review of the isolation merge and SECURITY-REVIEW-3140 (task
3142, pre-enable fixes). All tests are in `tests/test_agent_isolation_3142.py`
unless named otherwise:

| Finding | Status (flag on) | Test |
|---|---|---|
| F1 MEDIUM: a per-run, missing or mistyped dispatch config turns isolation off silently | FIXED: the host marker `/etc/equipa/require-agent-isolation` makes it required (refused at startup and at spawn when the config turns it off); a per-run config carries the host config's isolation and may only turn it on; startup prints the state | `test_marker_turns_isolation_on_whatever_the_config_says`, `test_a_marker_that_cannot_be_checked_counts_as_present`, `test_required_isolation_refuses_a_config_that_turns_it_off`, `test_spawn_refuses_when_required_and_the_config_turns_it_off`, `test_agent_runner_never_spawns_unisolated_while_required`, `test_per_run_config_without_the_key_keeps_host_isolation`, `test_per_run_config_cannot_turn_host_isolation_off`, `test_a_missing_per_run_config_does_not_turn_isolation_off`, `test_an_unreadable_host_config_keeps_isolation_on`, `test_carrying_isolation_keeps_other_gates_fail_closed`, `test_cli_refuses_before_any_mode_runs`, `test_startup_line_names_the_isolation_state`, `test_cli_warns_about_a_missing_dispatch_config_before_loading_it`, `test_loading_a_missing_config_writes_nothing_to_stdout` |
| F2 MEDIUM: localhost and LAN services reachable from the agent UID | FIXED: `IPAddressDeny=` on every unit; since a user manager cannot apply it, the launcher refuses unless its own loopback and LAN listeners are unreachable (nftables rule, step 4a); the verify script probes every listening loopback port and every `--loopback-port` | `test_unit_denies_loopback_link_local_multicast_and_private_ranges`, `test_extra_denied_ranges_add_to_the_defaults`, `test_handoff_names_the_denied_ranges`, `test_launcher_refuses_while_loopback_is_reachable`, `test_launcher_verify_runs_the_network_check`, `test_launcher_refuses_a_deny_list_without_loopback`, `test_local_addresses_include_the_hosts_own_lan_address`, `test_listening_loopback_ports_are_read_from_proc`, `test_probe_command_lists_operator_and_listening_ports`, `test_verify_script_fails_when_a_loopback_service_is_reachable`, `test_verify_script_passes_an_unreachable_port` |
| F3 MEDIUM / R3140-01: the link walk is quadratic in the target length | FIXED: linear trie walk; targets over 4095 bytes refused unread, at most 10,000 links per tree and 1,000,000 walked components per commit; targets read once per blob | `test_a_one_megabyte_link_target_is_refused_in_under_0_2_seconds`, `test_a_link_target_longer_than_path_max_is_refused`, `test_a_target_of_exactly_the_limit_is_walked`, `test_a_tree_with_too_many_links_is_refused`, `test_many_long_links_are_checked_quickly`, `test_walk_budget_spans_every_link_of_the_check`, `test_trie_walk_is_linear_in_the_directory_depth`, `test_trie_walk_agrees_with_the_path_walk`, `test_import_check_walks_every_link_through_the_trie` |
| F4 MEDIUM: concurrent units of other tasks can replace an export or forge a tester verdict | FIXED until a UID pool exists: every isolated unit runs alone (host-wide lock), `max_concurrent` above 1 is refused at dispatch, and a config that sets `isolation_uid_pool` (not implemented) is refused at startup and at spawn | `test_two_ordinary_units_never_overlap`, `test_units_of_another_process_are_waited_for`, `test_every_role_runs_alone_until_units_have_their_own_uid`, `test_concurrency_above_one_is_refused_with_isolation_on`, `test_parallel_tasks_refuse_a_cap_above_one_with_isolation_on`, `test_auto_run_and_parallel_goals_refuse_a_cap_above_one`, `test_every_dispatch_semaphore_is_gated_by_the_concurrency_refusal`, `test_a_uid_pool_is_refused_while_isolation_is_on`, `test_isolation_settings_and_spawn_refuse_a_uid_pool`, `test_concurrency_refusal_names_the_unimplemented_uid_pool`, `test_cli_refuses_a_uid_pool_before_any_mode_runs` |
| F5 LOW: the imported tip need not descend from the base | FIXED: `merge-base --is-ancestor` before `update-ref` | `test_import_refuses_a_rewound_task_branch`, `test_import_refuses_an_unrelated_task_branch_tip`, `test_import_accepts_a_tip_that_descends_from_the_base`, `test_import_accepts_an_unchanged_tip` |
| F6 LOW: external lifecycle hooks not refused with the flag on | NOT FIXED: outside this task's scope (`equipa/hooks`); still latent (no production caller of `load_hooks_config`), see residual risks | none |
| F7 LOW: the agent user's sudo is not checked at dispatch | FIXED: `sudo -n -l -U <agent>` must report "not allowed" (fail closed otherwise); root-equivalent groups are a floor `privileged_groups` cannot remove, supplementary groups included; the verify script checks the groups | `test_agent_without_sudo_rights_passes`, `test_agent_sudo_rights_or_an_unclear_answer_refuse`, `test_a_missing_sudo_refuses`, `test_dispatch_refuses_an_agent_user_with_sudo_rights`, `test_root_equivalent_groups_cannot_be_configured_away`, `test_dispatch_refuses_a_supplementary_privileged_group`, `test_verify_script_checks_the_root_equivalent_groups` |
| F8 LOW: no swap, disk or IO limits | FIXED for swap and IO (`MemorySwapMax=0`, `IOWeight`, both verified in the scope) and for the export (the launcher never publishes one over `max_export_bytes`); disk for the clone and HOME: runbook (own filesystem for the state root) | `test_unit_has_no_swap_and_a_lower_io_weight`, `test_scope_with_swap_is_refused`, `test_scope_io_weight_is_verified_when_set`, `test_handoff_tells_the_launcher_the_export_cap`, `test_launcher_never_publishes_an_export_over_the_cap`, `test_launcher_publishes_an_export_within_the_cap`, `test_launcher_rejects_an_invalid_export_cap`, `test_scope_verification` (3135) |
| F9 LOW: the launcher's cgroup-path check and the scope check at spawn are not pinned | FIXED: both mutations (M19 `if False:`, M22 the call removed) now fail a test | `test_launcher_refuses_another_cgroup_even_with_the_right_limits`, `test_spawn_refuses_a_scope_whose_limits_are_wrong` |
| F10 LOW: runbook gaps (MCP server under HOME, `-newer` search, new project dirs) | FIXED in the runbook (steps 0, 3, 7) | none (documentation) |
| I1 INFO: the sudoers check passes with `(ALL) NOPASSWD: ALL` | FIXED: the rule is judged by the content of `sudo -n -ll` (run-as user, exact command, NOPASSWD, `!pam_session`, `!use_pty`) | `test_narrow_rule_is_recognised_by_content`, `test_an_incomplete_rule_is_reported`, `test_an_all_rule_alone_does_not_pass_the_outer_check` |
| I2-I4 INFO | NOT FIXED (outside this task's scope; I4 is ISO-13 below) | none |
| I5 INFO: `--task` (single-task mode) is refused with the flag on | DOCUMENTED (step 9): use `--tasks <id>` | none |

Independent review of task 3142 and SECURITY-REVIEW-3142 (task 3147). All
tests are in `tests/test_agent_isolation_3147.py`:

| Finding | Status (flag on) | Test |
|---|---|---|
| N1 LOW: the runbook's nftables loading wipes Docker's rules, `/etc/nftables.d` is missing, a reload duplicates rules | FIXED: a self-contained rule file that deletes and re-creates only its own table, loaded by its own one-shot unit after docker and tailscaled (step 4a); the rule matches the agent's sockets positively, so packets without an owning socket never reach its rejects; the verify script checks that the table is loaded | `test_runbook_rule_file_replaces_only_its_own_table`, `test_runbook_loads_the_rule_with_its_own_unit`, `test_runbook_rule_filters_only_the_agent_users_sockets`, `test_a_missing_or_unlistable_firewall_table_fails`, `test_outer_checks_fail_without_the_firewall_table` |
| R3142-01 MEDIUM: host addresses outside the private ranges (tailnet, public, global IPv6) are neither denied nor probed | FIXED in the rule (`fib daddr type local reject`, `100.64.0.0/10`) and the runbook (`ip_address_deny_extra`, step 7); the verify script connects, as the agent, to its own listener on every host address and to every listening port at each of them | `test_runbook_rule_rejects_every_host_address_and_the_denied_ranges`, `test_probe_command_lists_every_host_address`, `test_verify_probes_own_listeners_ports_and_lan_targets`, `test_verify_fails_when_its_own_listener_on_a_host_address_answers`, `test_verify_probes_ports_at_every_host_address` |
| R3142-02 LOW: the LAN half of the deny list is never probed | FIXED in the verify script: `--lan-target` services are probed as the agent after the orchestrator reached them itself | `test_verify_fails_when_a_lan_target_answers`, `test_lan_target_control_needs_the_orchestrator_to_reach_it`, `test_verification_main_probes_lan_targets_after_the_control` |
| N2 LOW: the CLI startup refusal and the verify script's group check are pinned only by source text | FIXED: `async_main` is run with the marker and an isolation-off config; the group check is called with fake group lists | `test_cli_refuses_an_isolation_off_config_before_any_mode_runs`, `test_verify_group_check_fails_on_each_privileged_group`, `test_verify_inside_checks_the_groups_id_reports` |
| N3 INFO: IO weight is inert without BFQ or iocost; a misleading systemd 255 comment | FIXED in the runbook (`"io_weight": null` on such hosts, steps 4 and 7) and the comments | `test_runbook_recommends_a_null_io_weight_where_weights_do_nothing` |
| N4 INFO: unpinned MCP server install | FIXED in the runbook (step 0): pinned versions, published hashes, a hash-locked offline install after a scan; `mcp` is pinned below 2 because the server crashes on `mcp` 2.x | `test_runbook_pins_the_mcp_server_with_hashes` |
| F8 / R3142-04 LOW: the agent can fill `/` through `/tmp` and `/var/tmp` | FIXED in the verify script (the unit's TMPDIR must be its own and off `/`; a writable `/tmp` or `/var/tmp` on `/` fails) and the runbook (step 1: a tmpfiles.d ACL closes both to the agent user); the ACL was not recursive, see SR3147-01 below | `test_a_writable_shared_tmp_on_the_root_filesystem_fails`, `test_unit_tmpdir_on_the_root_filesystem_fails`, `test_unit_tmpdir_off_the_root_filesystem_passes` |

Reviews of task 3147 (SECURITY-REVIEW-3147 and the independent review),
task 3153. Tests are in `tests/test_agent_isolation_3153.py`; the CLI
settings items of the task 3150 reviews are in
`tests/test_cli_setting_sources_3153.py`:

| Finding | Status (flag on) | Test |
|---|---|---|
| SR3147-01 LOW: 1777 directories below `/tmp`, `/var/crash` and `/dev/shm` stay writable; the verify script reports PASS | FIXED in the runbook (step 1: `u:equipa-agent:---` on `/tmp`, `/var/tmp`, `/var/crash`, `/dev/shm` and every other world-writable directory on `/`; a per-user `/dev/shm` quota of at most 256 MiB as the alternative) and the verify script (a real file create in every world-writable directory on `/` that `find -xdev -perm -0002` reports as the agent, plus the well-known names below `/tmp`; `/dev/shm` writes must stop by the cap). NOT FIXED as a per-unit private `/tmp`: the unit is a user scope, which cannot take `PrivateTmp=`, `PrivateDevices=` or `InaccessiblePaths=` | `test_a_writable_world_writable_dir_on_the_root_filesystem_fails`, `test_a_dir_hidden_below_an_unlistable_parent_is_probed_by_name`, `test_an_uncapped_shared_memory_dir_fails`, `test_writes_stopped_by_the_cap_pass`, `test_writes_stopped_only_beyond_the_cap_fail`, `test_the_runbook_closes_every_shared_directory_completely`, `test_the_inside_run_calls_the_new_checks` |
| SR3147-02 LOW: the limited broadcast `255.255.255.255` is not rejected | FIXED in the rule (`fib daddr type broadcast reject`, `ip daddr 255.255.255.255 reject`) and the verify script (one UDP datagram with `SO_BROADCAST` must be refused). `DEFAULT_IP_ADDRESS_DENY` (the unit's `IPAddressDeny=`, which a user manager does not apply) is unchanged | `test_the_rule_rejects_broadcast`, `test_a_sent_broadcast_fails`, `test_a_rejected_broadcast_passes` |
| SR3147-03 LOW: global IPv6 addresses of LAN peers are reachable | FIXED in the rule: operator-filled `lan6_prefixes` / `lan4_prefixes` sets are rejected (step 4a); the verify script probes an IPv6 `--lan-target` | `test_operator_lan_prefixes_are_rejected`, `test_the_prefix_sets_are_declared_with_documentation_examples` |
| IR3147-A INFO: a DNATed connection to a published container port is judged by the container's address | FIXED in the rule: `ct status dnat reject` first in the agent chain. The verify script does not probe a published port (the agent user has no Docker access to find one) | `test_a_dnated_connection_is_rejected_whatever_its_new_address`, `test_the_rule_keeps_its_earlier_verdicts` |
| SR3147-04 LOW: the MCP install recipe continues past a failed hash check | FIXED: the recipe runs in its own `bash` with `set -euo pipefail` (step 0). NOT FIXED: the transitive wheels are still locked by the hash of the day's download | `test_the_mcp_recipe_stops_at_a_failed_hash_check`, `test_the_mcp_recipe_runs_through_when_the_hashes_match` |
| R3150-07 LOW: the unit's config directory is agent-writable | FIXED for settings: every CLI run passes `--setting-sources ""`, and the launcher refuses a claude argv without it inside the unit. NOT FIXED: the CLI's shell snapshot in that directory (documented in ORCHESTRATOR.md) | `test_unit_refuses_a_claude_argv_that_loads_settings_files`, `test_unit_starts_the_argv_the_orchestrator_builds` |

## Residual risks and limitations

* **All agents share one agent UID.** Sequential units no longer share
  anything (per-unit HOME, ISO-02), and since task 3142 no isolated unit
  overlaps another (F4), so they cannot read, write or signal each other's
  processes and unit directories while they run. The price is one isolated
  agent at a time on the host. A pool of agent users with one UID per unit
  (future work) would let ordinary units run in parallel again.
* The Ollama provider cannot be used with the flag on (R3136-01).
* External lifecycle hooks (`equipa/hooks`) run their operator-configured
  command through the shell, as the orchestrator user, with the task
  worktree as the working directory; nothing refuses them with the flag on.
  The command text is the operator's (task data travels in the
  environment), but the working directory holds the imported agent work,
  so even an absolute command runs agent-written code when it reads
  project files: `/usr/bin/make` reads the Makefile, `npm test` the
  package.json scripts, `pytest` conftest.py, `python3 -m pkg` imports from
  the working directory, and the shipped example hooks run linters and
  builds on the project. An absolute path does not make a hook safe:
  configure no lifecycle hooks while the flag is on (review F6, R3140-02;
  the stock orchestrator registers none). Refusing or isolating them is
  future work.
* The file-access boundary is a blocklist (`deny_read` plus the verify
  script's scans), not an allowlist: world-readable files outside the
  checked locations stay readable to the agent. The runbook closes project
  checkouts and the TheForge tree; a mount-namespace allowlist is future
  work (ISO-05).
* With the flag on there is no pre-dispatch build check, dependency
  auto-install or build auto-fix (ISO-01).
* The host's git (< 2.46) does not fsck objects fetched from a bundle
  (ISO-13); malformed objects from an agent can reach the project's object
  store. `read-tree -u` still refuses `.git` and `..` paths.
* Teardown still fails open (ISO-08): a failed import is logged at ERROR but
  does not mark the run failed.
* Loopback, the LAN and every address of the host are closed (F2), the
  public internet is not. An agent can still exfiltrate what it
  legitimately holds: its OAuth token and the code it works on. On a user
  manager the blocking depends on the operator's nftables rule; the
  launcher refuses while it is missing, and the verify script fails.
* Unix-domain sockets are not IP traffic: neither `IPAddressDeny=` nor the
  nftables rule covers them. A world-writable socket of a local service
  (a database, a container or VPN daemon's API) is reachable by the agent;
  make sure each one authenticates the agent user (PostgreSQL `local` lines
  `peer` or `scram-sha-256`, never `trust`).
* A unit's clone, HOME, TMPDIR and exports have no quota of their own; the
  runbook puts the agent state root on a size-capped filesystem of its own
  and closes the shared world-writable directories (`/tmp`, `/var/tmp`,
  `/var/crash`, `/dev/shm`) to the agent user or caps them (F8,
  SR3147-01). These are host settings, not per-unit ones: the unit is a
  user scope, which cannot take `PrivateTmp=` or `InaccessiblePaths=`.
* In-tree `.gitattributes` of the agent's commits still apply to the
  orchestrator's git (for example `-diff` or `merge=union`). Drivers must be
  defined in config, which agents can no longer write.
* Gitignored files other than `carry_ignored_paths` do not travel back.
* If the orchestrator is SIGKILLed *and* the agent had killed its own
  launcher, the scope's remaining processes live until the next isolated
  spawn. `sweep_stale_scopes` kills scopes whose owning orchestrator is gone.
* Bundling costs time proportional to the task branch's history on each
  dispatch.
* Roles that run in the main checkout (goal planner/evaluator) are refused;
  they need worktree isolation first.
* `/tmp` and `/var/tmp` stay visible (a scope has no private mount
  namespace); the agent gets a private `TMPDIR`, and step 1 closes or caps
  the shared ones.
* The orchestrator's own sudo is untouched. With isolation it is no longer
  reachable from agents, but narrowing it is still good hygiene.

## Operator runbook

Placeholders: `<orch>` is the orchestrator user, `<runtime>` the EQUIPA
checkout the orchestrator runs from, `<db>` the TheForge database. Run every
step as an administrator unless noted. Nothing here is done by EQUIPA itself.

### 0. Requirements

Linux 5.14+ with cgroup v2 (`/sys/fs/cgroup/cgroup.controllers` exists),
systemd with user managers, sudo 1.8.7+, git 2.29+, nftables (step 4a). The
Claude CLI must be installed at a system path readable and executable by
other users, not under `<orch>`'s HOME. Use the path `command -v claude`
prints for a system-wide install: `/usr/bin/claude` for an npm global
install on Ubuntu, `/usr/local/bin/claude` for others. Set it as
`agent_isolation.claude_executable` (step 7).

**MCP servers run inside the agent unit, as the agent user.** They are the
agent's tools: the CLI starts them, in the unit, from the commands in the
agent's MCP config. They are not run as the orchestrator. So every MCP
server the agent keeps (`allowed_mcp_servers`, by default only `theforge`)
must be executable by the agent user from a system location, and must not
depend on `<orch>`'s HOME, which step 3 closes (0700). A `uvx` under
`~<orch>/.local/bin` fails that, and under a fresh per-unit HOME `uvx` would
also download the server again for every unit. Install the server into a
root-owned virtualenv and point `mcp_config.json` at its absolute path.

Every agent executes this code, so **install a pinned version, verify it
against known hashes, and scan it before it is installed** (review N4).
The recipe downloads the pinned server and every dependency as wheels,
installs nothing yet, checks the two pinned wheels against the hashes PyPI
publishes, writes a lock file that holds every file's hash, scans that,
and only then installs exactly the scanned files, offline:

The recipe runs in its own `bash` with `set -euo pipefail`, so a `FAILED`
from `sha256sum -c`, a finding from the scanner or any other failing step
stops it before anything is installed (review SR3147-04); pasting the
lines into an interactive shell one by one would carry on past them.
`bash` reads the recipe from its standard input, so every `pip` call runs
with `--no-input`: a prompt (index credentials, for example) would
otherwise read, and swallow, the rest of the recipe (F-6 of the 3153
review).

```bash
bash <<'RECIPE'
set -euo pipefail
python3 -m venv /opt/equipa-mcp
install -d -m 0700 /root/equipa-mcp-wheels && cd /root/equipa-mcp-wheels
# 1. Download, install nothing. mcp is pinned too: mcp-server-sqlite
#    2025.4.25 asks for mcp>=1.6.0 and crashes at startup on mcp 2.x
#    ("'Server' object has no attribute 'list_resources'").
/opt/equipa-mcp/bin/pip download --no-input --only-binary=:all: --dest . \
    'mcp-server-sqlite==2025.4.25' 'mcp[cli]==1.30.0'
# 2. The pinned wheels are the published ones (sha256 from PyPI):
sha256sum -c - <<'EOF'
5ba5706aa29d249a3cde8226577e021c07792d3198e9db40fd005578d2a0801d  mcp_server_sqlite-2025.4.25-py3-none-any.whl
666edb5009503e1047c9d60346a756f94b261f05cc2625f23d41c728ffc484d0  mcp-1.30.0-py3-none-any.whl
EOF
# 3. Lock every downloaded file by its hash:
for wheel in *.whl; do
    name="${wheel%%-*}"; rest="${wheel#*-}"
    printf '%s==%s --hash=sha256:%s\n' "$name" "${rest%%-*}" \
        "$(sha256sum "$wheel" | cut -d' ' -f1)"
done > requirements.lock
# 4. SCAN before installing, with your scanner of record, for example:
pip-audit --disable-pip --require-hashes -r requirements.lock
#    (or osv-scanner on requirements.lock). Stop on any finding.
# 5. Install exactly the scanned files, offline, every hash enforced:
/opt/equipa-mcp/bin/pip install --no-input --no-index --find-links . \
    --require-hashes --no-deps -r requirements.lock
/opt/equipa-mcp/bin/pip check --no-input
install -m 0644 requirements.lock /opt/equipa-mcp/requirements.lock
chmod -R go-w /opt/equipa-mcp && chmod -R o+rX /opt/equipa-mcp
RECIPE
```

To upgrade, change the pins, repeat every step, and keep the new
`requirements.lock` beside the old one. Re-installing from a kept
`requirements.lock` and its wheel directory gives the same files.

```json
"theforge": {
    "command": "/opt/equipa-mcp/bin/mcp-server-sqlite",
    "args": ["--db-path", "/absolute/path/to/theforge.db"]
}
```

The `--db-path` must be absolute; the orchestrator rewrites it to the
read-only view (`view_db_path`) for the agent. The launcher refuses a unit
whose MCP command the agent user cannot execute. Paths handed to the agent
with `--add-dir` (for example a skills checkout) must stay readable by
others (`o+rX`) for the same reason.

### 1. Create the agent user

```bash
useradd --system --no-create-home --home-dir /var/lib/equipa-agent \
        --shell /bin/bash --user-group equipa-agent
passwd -l equipa-agent                    # no password login
# The passwd HOME is root-owned and NOT agent-writable: the launcher refuses
# otherwise. Each agent gets its own HOME below the state root instead.
install -d -o root -g root -m 0711 /var/lib/equipa-agent
install -d -o equipa-agent -g equipa-agent -m 0700 /var/lib/equipa-agent/.equipa-agent
id equipa-agent                           # must show NO sudo/admin/wheel/adm/docker/lxd/... group
sudo -l -U equipa-agent                   # must say: not allowed to run sudo
loginctl disable-linger equipa-agent      # no user manager: it could start units that outlive agents
echo equipa-agent >> /etc/cron.deny       # no cron/at persistence
echo equipa-agent >> /etc/at.deny
```

**The agent user must not be in `docker`, `lxd`, `sudo`, `adm` or any other
group that grants root-equivalent access** (also `root`, `admin`, `wheel`,
`incus`, `libvirt`, `kvm`, `disk`, `shadow`, `systemd-journal`), and no
sudoers rule may name it or one of its groups. Membership of `docker` or
`lxd` is root on the host as surely as sudo is. The orchestrator checks both
at every dispatch (`os.getgrouplist` and `sudo -n -l -U equipa-agent`) and
refuses otherwise; the launcher checks the groups again from inside, and the
verify script fails on them. `privileged_groups` can add groups to the list,
never remove these. (The orchestrator user itself may well be in `docker`
and `sudo`; that is why agents must not run as it.)

The launcher refuses to start an agent while the agent user lingers or may
use `crontab` or `at` (if they are installed), and the verify script checks
the same. If `/etc/cron.allow` or `/etc/at.allow` exists, it takes
precedence: leave the agent user out of it instead.

**Disk.** Each unit's clone, HOME and TMPDIR live under
`/var/lib/equipa-agent/.equipa-agent`, and its exports in the exchange
directory below `/var/lib/equipa-agent`; EQUIPA caps none of them against a
hostile agent (`max_export_bytes` caps only the launcher's own export). Put
`/var/lib/equipa-agent` on a size-capped filesystem of its own (a separate
partition, or a fixed-size image mounted there, for example a 20 GiB ext4
image) so a unit cannot fill `/` and break the orchestrator or TheForge.

Each unit's TMPDIR is its own (`.../<unit>/tmp`, mode 0700) and lies on
that filesystem. **The unit is a `systemd-run --user --scope`, and a scope
cannot take systemd's `PrivateTmp=`, `PrivateDevices=`, a `TemporaryFileSystem=`
for `/dev/shm` or `InaccessiblePaths=`** (they are execution settings of a
service; the launch path would have to become a system service started as
the agent user, which the narrow sudoers rule of step 5 does not allow). So
the shared world-writable directories stay writable to every user, and an
agent that writes `/var/tmp/x` instead of `$TMPDIR/x` fills `/`. Close them
to the agent user on the host instead (review SR3147-01).

The ACL of `/tmp` is not recursive: with search permission left on `/tmp`,
the agent could still write the 1777 directories below it that systemd
re-creates at every boot (`/tmp/.X11-unix`, `/tmp/.ICE-unix`,
`/tmp/.XIM-unix`, `/tmp/.font-unix`), all of them on `/`. So give the agent
user **no** permission at all on `/tmp`, `/var/tmp` and `/var/crash`
(another 1777 directory on `/`): nothing below them is reachable then,
whatever its name. A tmpfiles.d ACL also survives the tmpfs `/tmp` being
re-created at boot:

```bash
printf '%s\n' 'a+ /tmp       - - - - u:equipa-agent:---' \
              'a+ /var/tmp   - - - - u:equipa-agent:---' \
              'a+ /var/crash - - - - u:equipa-agent:---' \
              'a+ /dev/shm   - - - - u:equipa-agent:---' \
    > /etc/tmpfiles.d/equipa-agent-tmp.conf
systemd-tmpfiles --create /etc/tmpfiles.d/equipa-agent-tmp.conf
getfacl /tmp /var/tmp /var/crash /dev/shm | grep equipa-agent
                                   # user:equipa-agent:--- on each
# Every other world-writable directory on / (the list differs per host):
find / -xdev -type d -perm -0002 -not -path '/tmp/*' -not -path '/var/tmp/*'
# add an 'a+ <dir> - - - - u:equipa-agent:---' line for each one listed.
```

The named-user ACL entry takes precedence over the world bits, so the agent
can neither enter nor list these directories, let alone create files there.
Tools inside the unit that honour `TMPDIR` (the CLI, git, python's
`tempfile`, `mktemp`, bash) are unaffected; a command that hard-codes
`/tmp/...` fails instead of filling `/`.

`/dev/shm` is a host-wide tmpfs (half the RAM by default), and its files
outlive the unit: units run one after another could pile them up until the
host runs out of memory, even though each stays under its own `MemoryMax`.
The ACL line above closes it, at a cost: POSIX shared memory and named
semaphores (`shm_open`, `sem_open`) fail for the agent, so a project whose
tests use python's `multiprocessing` locks or queues fails inside the unit.
If agents need it, drop that line and cap the agent user instead with a
per-user tmpfs quota on `/dev/shm` (`usrquota`, kernel 6.6 or later; quota
options can only be set when the tmpfs is mounted, not on a remount) of at
most **256 MiB**, the cap the verify script enforces (`AGENT_SHM_CAP_MB` in
`scripts/verify_agent_isolation.sh`).

If agents need a shared
`/tmp`, make it a size-capped tmpfs instead (`systemctl enable tmp.mount`
with `size=` in its `Options=`), knowing that an agent can then fill it for
every other user, the orchestrator's own `/tmp` files included, and keep
`/var/tmp` closed by the ACL or on a filesystem of its own. The verify
script fails while the agent can write a
`/tmp` or `/var/tmp` that lies on `/`, any other world-writable directory
on `/` (it runs `find / -xdev -type d -perm -0002` as the agent and creates
a file in each candidate, plus the well-known names below `/tmp`), more
than 256 MiB into `/dev/shm`, or while the unit's TMPDIR lies on `/`. It
prints a NOTE while the agent may still enter `/tmp` or `/var/tmp` without
listing them (the `--x` entry of earlier versions of this step), because
only well-known names below them can be probed then.

No global git configuration is needed, and none is read: the launcher
copies `user.name`/`user.email` from the task worktree into each clone and
into the unit's own `GIT_CONFIG_GLOBAL` file. Do not put dot-files
(`.gitconfig`, `.ssh`, `.claude`, shell rc files) in `/var/lib/equipa-agent`.

### 2. Exchange and view directories

```bash
install -d -o equipa-agent -g equipa-agent -m 0711 /var/lib/equipa-agent/exchange
install -d -o <orch> -g <orch> -m 0755 /var/lib/equipa-view
```

The exchange directory is where agents leave their export bundles. It is
owned by the agent user and entered, not listed, by the orchestrator. Its
parent (the root-owned agent HOME) is not agent-writable, so the agent
cannot swap it for a link. The view directory is written only by the
orchestrator.

### 3. Lock down what the agent must not read or write

Placeholders for this step: `<db-dir>` is the directory the TheForge
database really lives in (`dirname "$(readlink -f <db>)"`; `<db>` is often a
symlink), `<backup-dir>` each directory that holds database backups, and
`<projects>` each directory that holds project checkouts.

```bash
chmod 0700 ~<orch>                           # the runtime must NOT live under it
chmod 0700 ~<orch>/.claude ~<orch>/.config ~<orch>/.ssh 2>/dev/null

# TheForge: protect the DIRECTORY and every copy, not only the live file.
# Keep the database in a directory of its own that holds nothing an agent
# needs (no runtime, launcher, hook or MCP program), owned by <orch>.
chmod 0700 <db-dir>                          # agent may neither list nor enter it
chmod 0700 <backup-dir>                      # each backup directory, likewise
find <db-dir> <backup-dir> -xdev -type f \( -name '*.db' -o -name '*.db[-._]*' \
     -o -name '*.sqlite*' \) -exec chmod 0600 {} +   # every copy owner-only
# Stray copies anywhere, old or new (backups are usually OLDER than <db>, so
# no -newer), on EVERY mounted filesystem (no -xdev: a projects share or a
# backup disk is often a mount of its own), compressed ones included
# (*.db.gz matches *.db*): move each into <backup-dir>, or chmod 0600 it.
find / \( -path /proc -o -path /sys -o -path /dev -o -path /run \) -prune \
     -o -type f \( -name '*.db*' -o -name '*.sqlite*' \) -perm -o=r -print 2>/dev/null

chmod 0600 <runtime>/mcp_config.json <runtime>/.env 2>/dev/null
chmod -R go-w <runtime>                      # runtime read-only to others
chmod o+x <runtime>                          # traversable, not listable
chmod -R o+rX <runtime>/equipa <runtime>/hooks <runtime>/skills <runtime>/scripts

# Project checkouts: agents get bundles, so they need no access at all.
chmod -R o-rwx <projects>/<each project>     # or a group the agent user is not in
```

Project directories created *later* (a new EQUIPA project, a client copying
files over Samba) are world-readable under the usual umask 022 until this
step is repeated. Close that gap at the source:

* run the orchestrator with `umask 027` (`UMask=0027` in its systemd unit,
  or `umask 027` in the shell profile it starts from), so everything it
  creates is closed to others;
* give Samba shares below `<projects>` `create mask = 0660` and
  `directory mask = 2770` (no bits for others);
* run `scripts/verify_agent_isolation.sh` (step 8) on a schedule or before
  each orchestrator start; it fails on any secret-shaped file the agent can
  read below `secret_scan_roots`.

Neither `<db-dir>` nor a `<backup-dir>` may be open to a group the agent user
is in. List every `<backup-dir>` in `agent_isolation.db_backup_dirs` and
every `<projects>` directory in `agent_isolation.secret_scan_roots` (step 7):
the launcher refuses to run when the agent can list or enter a database or
backup directory, and the verify script fails when it can read any database
copy or a secret-shaped file (`.env`, keys, `credentials*.json`, ...) below a
project root.

The agent must be able to **read and execute** the launcher, the hook script
and the python named in the generated hook settings. Keep the runtime's
virtualenv, if any, inside the runtime rather than in `<orch>`'s HOME, and
the runtime itself outside `<orch>`'s HOME (a HOME the agent can enter
exposes every world-readable file at a known name in it). If the runtime
lives inside a projects directory, close the individual project checkouts
rather than their parent.

### 4. The orchestrator's user manager and controller delegation

```bash
loginctl enable-linger <orch>
cat /sys/fs/cgroup/user.slice/user-$(id -u <orch>).slice/user@$(id -u <orch>).service/cgroup.subtree_control
# must list: cpu io memory pids. If not:
mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=pids memory cpu io\n' > /etc/systemd/system/user@.service.d/delegate.conf
systemctl daemon-reload   # then restart the orchestrator's user manager (log out / reboot)
```

The io controller carries the units' `IOWeight` (F8). Without it the scope
has no `io.weight` and every dispatch is refused. But a weight only does
something with the BFQ scheduler or iocost; check before delegating io:

```bash
cat /sys/block/*/queue/scheduler        # the active one is in [brackets]
cat /sys/fs/cgroup/io.cost.qos 2>/dev/null   # iocost: lines with enable=1
```

If every disk the agents and TheForge use runs `[bfq]`, or iocost is
enabled, keep the default weight and delegate `io`. On a host whose disks
run `[none]` or `[mq-deadline]` without iocost (common on NVMe and VMs),
the weight is ignored: set `"io_weight": null` in step 7 and leave `io` out
of `Delegate=` (`Delegate=pids memory cpu`). Do the same on a host where
`io` cannot be delegated. `null` is then the honest setting: agents compete
for IO on equal terms either way. `memory.swap.max` comes with the memory
controller.

### 4a. Network: no loopback, LAN, tailnet or host address for the agent user

Every unit carries `IPAddressDeny=` for loopback, link-local, multicast and
the private ranges (see "Network"), but on many hosts the orchestrator's
user manager cannot apply it, so **this nftables rule for the agent user's
UID is the real control**. Until it is loaded the launcher refuses every
agent (its own reachability check fails closed) and the verify script
fails.

**Never use the stock `nftables.service` for this, and never run `nft -f
/etc/nftables.conf`, `systemctl start/restart/reload nftables` or `nft
flush ruleset` on a host with Docker or Tailscale.** The stock
`/etc/nftables.conf` begins with `flush ruleset`, which deletes the whole
ruleset of the host, including the rules Docker and tailscaled keep there
(iptables-nft writes into the same ruleset): container networking and
published ports break until Docker restarts. The rule below lives in a
table of its own, the file deletes and re-creates **only that table**
(loading it twice leaves one copy, not duplicate rules), and its own
one-shot unit loads it after Docker and tailscaled.

The rule rejects, for the agent user only:

* `fib daddr type local`: **every address of this host**, whatever its
  range: loopback, the LAN addresses, a tailnet address, a public IPv4 or
  global IPv6 address (a service bound to `0.0.0.0` answers on all of
  them);
* the denied ranges (see "Network"), **plus `100.64.0.0/10`**, the CGNAT
  range Tailscale uses for every tailnet peer;
* **the IPv4 limited broadcast `255.255.255.255` and every broadcast
  address** (`fib daddr type broadcast`): neither is a local address nor in
  a denied range, and any unprivileged socket may set `SO_BROADCAST`, so
  without these lines the agent could talk UDP request/response with every
  broadcast-answering service on the LAN segment (review SR3147-02);
* **the LAN's own prefixes that are not private ranges**, from two sets the
  operator fills in (`lan6_prefixes`, `lan4_prefixes`): on a dual-stack LAN
  every NAS, router or database server also has a global IPv6 address from
  the delegated prefix, and some LANs use a publicly routed IPv4 subnet
  (review SR3147-03). List the prefix (`ip -6 route show proto kernel`,
  `ip -6 route show proto ra`) in `elements = { ... }`, and give the verify
  script an IPv6 `--lan-target` on such a LAN;
* **every connection whose destination was rewritten by DNAT** (`ct status
  dnat`, first in the chain). Docker publishes container ports through a
  DNAT in the `nat` output hook, which runs before this filter: a connect to
  a published port at a host address reaches this chain with the
  container's address, which `fib daddr type local` no longer matches, and
  which is rejected only while the container subnet happens to lie in a
  denied range (review IR3147-A);

except DNS to the local resolver (`127.0.0.53` on Ubuntu, where
`/etc/resolv.conf` names it; the agent must resolve the API's name). If
`/etc/resolv.conf` names another resolver inside a rejected range (a
tailnet's `100.100.100.100`, a LAN router), accept that one for port 53
the same way, before the `reject` lines.

```bash
install -d -o root -g root -m 0755 /etc/nftables.d
cat > /etc/nftables.d/equipa-agent.nft <<'EOF'
#!/usr/sbin/nft -f
# EQUIPA agent user: no loopback, LAN, tailnet or own-host address.
# Re-creates ONLY this table (never "flush ruleset": Docker and tailscaled
# keep their rules in the same ruleset). Declaring the table first makes
# the delete succeed when it does not exist yet; the file is applied as
# one transaction, so a reload never leaves a gap or a duplicate.
table inet equipa_agent
delete table inet equipa_agent
table inet equipa_agent {
    # The LAN's own prefixes where they are not private ranges: the global
    # IPv6 prefix of a dual-stack LAN, a publicly routed IPv4 LAN subnet.
    # Edit the elements line for this host; the sets may stay empty.
    set lan6_prefixes {
        type ipv6_addr; flags interval;
        # elements = { 2001:db8:1234::/48 }
    }
    set lan4_prefixes {
        type ipv4_addr; flags interval;
        # elements = { 198.51.100.0/24 }
    }
    chain agent {
        # A destination rewritten by DNAT (Docker's published ports at a
        # host address, nat output hook) is never what the rules below see
        # it was: reject every DNATed connection first.
        ct status dnat reject
        ip daddr 127.0.0.53 udp dport 53 accept
        ip daddr 127.0.0.53 tcp dport 53 accept
        fib daddr type local reject
        fib daddr type broadcast reject
        ip daddr 255.255.255.255 reject
        ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
                   169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16,
                   224.0.0.0/4 } reject
        ip6 daddr { ::/128, ::1/128, fc00::/7, fe80::/10, ff00::/8 } reject
        ip daddr @lan4_prefixes reject
        ip6 daddr @lan6_prefixes reject
    }
    chain output {
        type filter hook output priority 0; policy accept;
        # Only packets of the agent user's own sockets enter "agent". Never
        # the negation ('meta skuid != "equipa-agent" accept'): packets
        # with no owning socket (the kernel's RSTs, ICMP errors, IPv6
        # neighbour discovery, IGMP) match neither form, and would then
        # reach the reject rules for every user of the host.
        meta skuid "equipa-agent" jump agent
    }
}
EOF
chmod 0644 /etc/nftables.d/equipa-agent.nft

cat > /etc/systemd/system/equipa-agent-firewall.service <<'EOF'
[Unit]
Description=EQUIPA agent user firewall (nftables table inet equipa_agent)
# Its own unit, never nftables.service (whose config flushes the ruleset).
Wants=network-pre.target
After=network-pre.target docker.service tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d/equipa-agent.nft
ExecReload=/usr/sbin/nft -f /etc/nftables.d/equipa-agent.nft

[Install]
WantedBy=multi-user.target
EOF
nft -c -f /etc/nftables.d/equipa-agent.nft   # syntax check only, loads nothing
systemctl daemon-reload
systemctl enable --now equipa-agent-firewall.service
nft list table inet equipa_agent           # the rule, once
systemctl reload equipa-agent-firewall.service && nft list table inet equipa_agent
                                           # still once: reloading is idempotent
```

The unit has no `ExecStop`: stopping it leaves the table in place. To
remove the rule on purpose, `nft delete table inet equipa_agent` (the
launcher then refuses every agent again).

Also add `100.64.0.0/10` to `ip_address_deny_extra` (step 7) on a
Tailscale host, and every other range you add to the rule: each listed
range also goes into the unit's `IPAddressDeny=` and into the launcher's
check of the host's own addresses, which then covers a tailnet address of
the host fail-closed even without the `fib` line. `reject` rather than
`drop` makes each probe fail at once instead of after a timeout.

The verify script (step 8) proves the rule from both sides: as `<orch>` it
checks that the table is loaded (`sudo -n /usr/sbin/nft list table inet
equipa_agent`; listing needs CAP_NET_ADMIN, see step 6) and reads that
listing: the `agent` chain must hold `ct status dnat reject` as its first
statement plus both broadcast rejects (an older rule file fails), and on a
host with a global IPv6 address an empty `lan6_prefixes` set fails with a
WARNING (review R3153-02). It does not check that the listed prefixes are
the right ones; the IPv6 `--lan-target` probe does that. As the agent
it fails if it can connect to a listener of its own on **any** address of
the host, to any listening port at `127.0.0.1` or at any host address, or to
any `--lan-target`. The launcher's own check (its listeners on `127.0.0.1`
and on this host's addresses in the denied ranges) runs before every agent.

The orchestrator process needs `XDG_RUNTIME_DIR=/run/user/<uid of orch>`
(set automatically for login sessions and user services).

### 5. The OAuth token

As `<orch>`, run `claude setup-token` and store the long-lived token in a file
only `<orch>` can read:

```bash
install -m 0600 /dev/null ~<orch>/.equipa-agent-token   # then paste the token into it
```

Point `agent_isolation.oauth_token_file` at it, or export
`CLAUDE_CODE_OAUTH_TOKEN` in the orchestrator's service environment.

### 6. The sudoers rule

Print the exact lines for your paths (as `<orch>`, from `<runtime>`, once the
`agent_isolation` section of step 7 is in `dispatch_config.json`; the flag
itself can still be off):

```bash
python3 -c 'import getpass; from equipa import config, isolation; print(isolation.sudoers_snippet(isolation.load_isolation_settings(config.get_active_dispatch_config()), getpass.getuser()))'
```

For `/usr/bin/python3` and a runtime at `/opt/equipa` it prints:

```
Cmnd_Alias EQUIPA_AGENT_LAUNCH = /usr/bin/python3 -I /opt/equipa/equipa/agent_launcher.py --isolated
Defaults!EQUIPA_AGENT_LAUNCH !use_pty, !pam_session, env_reset, !log_output
<orch> ALL=(equipa-agent) NOPASSWD: EQUIPA_AGENT_LAUNCH
```

Install it with `visudo -f /etc/sudoers.d/equipa-agent`. The command has no
wildcard, so `<orch>` may run exactly that one command line, and only as
`equipa-agent`. `!pam_session` keeps `pam_systemd` from moving the agent out
of its scope. `!use_pty` keeps stdout a pipe for the CLI's stream. The
launcher path must not be writable by the agent user (step 3); it is also in
the launcher's own `deny_write` check.

The verify script (step 8) checks this rule **by its content** in
`sudo -n -ll` (review I1): an entry that runs exactly that command line as
`equipa-agent`, NOPASSWD, and `Defaults!EQUIPA_AGENT_LAUNCH` with
`!pam_session` and `!use_pty`. That `<orch>` can run the command proves
nothing while `<orch>` also holds `(ALL) NOPASSWD: ALL`.

At every dispatch the orchestrator runs `sudo -n -l -U equipa-agent` to
confirm the agent user has no sudo rights (F7). sudo answers that only to a
user holding `ALL`, or the `list` permission for the agent user. If you
narrow `<orch>`'s own sudo (recommended once isolation runs), keep that
listing possible, for example:

```
<orch> ALL=(equipa-agent) NOPASSWD: list
```

(sudoers(5), "list"). Without it every dispatch is refused with `cannot
verify that the agent user ... has no sudo rights`.

The verify script (step 8) also lists the agent user's nftables table
(step 4a), which needs CAP_NET_ADMIN. A narrowed `<orch>` keeps exactly
that read-only command:

```
<orch> ALL=(root) NOPASSWD: /usr/sbin/nft list table inet equipa_agent
```

### 7. Configure and switch on

In `dispatch_config.json`:

```json
"features": { "agent_isolation": true },
"max_concurrent": 1,
"agent_isolation": {
    "agent_user": "equipa-agent",
    "python": "/usr/bin/python3",
    "claude_executable": "/usr/bin/claude",
    "exchange_dir": "/var/lib/equipa-agent/exchange",
    "view_db_path": "/var/lib/equipa-view/theforge-view.db",
    "oauth_token_file": "/home/<orch>/.equipa-agent-token",
    "db_backup_dirs": ["/path/to/theforge-backups"],
    "secret_scan_roots": ["/path/to/projects"],
    "pids_max": 512,
    "memory_max": "4G",
    "cpu_weight": 100,
    "ip_address_deny_extra": ["100.64.0.0/10"],
    "io_weight": null
}
```

`ip_address_deny_extra` as shown is for a host running Tailscale (or any
CGNAT overlay); keep it in step with the nftables rule (step 4a).
`"io_weight": null` is for a host whose disks ignore IO weights or that
cannot delegate `io` (step 4); leave the key out to keep the weight of 50.

Other keys, with defaults: `launcher` (this checkout's
`equipa/agent_launcher.py`), `git_executable` (`/usr/bin/git`), `sudo`,
`systemd_run`, `setup_timeout_sec` (300), `stop_timeout_sec` (120),
`max_export_bytes` (2 GiB), `forge_mcp_server` (`theforge`),
`allowed_mcp_servers` (`["theforge"]`), `exclude_tables` (`["api_keys"]`),
`deny_read`/`deny_write` (extra absolute paths), `privileged_groups`,
`carry_ignored_paths` (`[".equipa-artifacts"]`), `db_backup_dirs` (`[]`:
directories with TheForge backups, closed to the agent like the database's
own directory), `secret_scan_roots` (`[]`: project roots the verify script
scans; it fails while this is empty), `unit_wait_timeout_sec` (21600: how
long a unit may wait for the one running before it, before the dispatch is
refused), `ip_address_deny_extra` (`[]`: more ranges the agent may not
reach, step 4a), `io_weight` (50; `null` for none, step 4). Unknown keys are
refused. `privileged_groups` adds to the built-in root-equivalent groups.

`max_concurrent` must be 1 while the flag is on (dispatch refuses a higher
value, from the config or `--max-concurrent`): every isolated unit runs
alone until per-unit agent UIDs exist ("Every unit runs alone"). Do not set
`isolation_uid_pool`: the pool is not implemented, and a config that names
one is refused.

Once `scripts/verify_agent_isolation.sh` prints `RESULT: PASS` (step 8),
make the setting stick, as root:

```bash
install -d -o root -g root -m 0755 /etc/equipa
install -o root -g root -m 0644 /dev/null /etc/equipa/require-agent-isolation
```

While that file exists, isolation is required on this host: a dispatch
whose config turns the flag off (a per-run `--dispatch-config` without it,
a mistyped path, `false`) is refused instead of running agents as `<orch>`
("Once on, it stays on"). The directory must stay searchable by `<orch>`;
a marker that cannot be checked counts as present. The orchestrator prints
`agent_isolation: ON (required by /etc/equipa/require-agent-isolation)` at
startup.

Restart every orchestrator process on the new code before relying on the
unit lock: a process still running older code takes no lock.

### 8. Verify on the real host

As `<orch>`, from `<runtime>`, with the orchestrator's environment:

```bash
# --loopback-port: each local service you know of; --lan-target: LAN services
scripts/verify_agent_isolation.sh --repo /path/to/a/project \
    --loopback-port 5432 --loopback-port 6379 \
    --lan-target 192.0.2.10:445 --lan-target 192.0.2.1:443
```

Each `--lan-target` is `ADDRESS:PORT` (`[ADDRESS]:PORT` for IPv6) of a
service on another LAN machine (a NAS, the router's admin page, a database
server, a tailnet peer) that the orchestrator itself can reach: the agent
must not. A target that does not answer the orchestrator fails the check,
since its probe would prove nothing. Without any `--lan-target` the output
has a `NOTE` line: the LAN ranges were then checked only through the rule's
content, not probed.

It runs its own checks as an isolated agent through the real path (scope,
sudoers rule, launcher, handoff). Every line must be `PASS`, and the last
line must be `RESULT: PASS`. The checks: runs as the agent user and cannot
sudo; cannot read the DB, the orchestrator HOME and its secrets, or
`mcp_config.json`, and cannot enter the orchestrator HOME; can neither list
nor enter the database's real directory or any `db_backup_dirs` entry, cannot
read any database file or copy the orchestrator finds there (each probed by
name), and finds no readable copy when it searches them itself; cannot read
a secret-shaped file below any `secret_scan_roots` entry or `--repo`;
cannot write any listed `.git`, the runtime or the
launcher; is in its own `equipa-agent-*.scope` with `pids.max` and
`memory.max` set; cannot raise its limits or write any `cgroup.procs`; cannot
signal the orchestrator; the view is read-only and has no `api_keys`; no
credential except the OAuth token is in its environment; its HOME,
`CLAUDE_CONFIG_DIR` and `GIT_CONFIG_GLOBAL` are the unit's own and it cannot
write its passwd HOME; it cannot use `crontab` or `at` and does not linger;
it cannot read any SQLite file below the project roots whose schema holds an
excluded table (found by the orchestrator, probed by name); it cannot
connect to a listener of its own on any address of the host (every address
the kernel's local routing table lists: loopback, LAN, tailnet, public,
global IPv6), nor to any `--loopback-port` or any port that listens on
loopback or on every address (read from `/proc/net/tcp` and `tcp6` by the
orchestrator, so the list covers services you did not name) at `127.0.0.1`
and at each host address, nor to any `--lan-target`; its TMPDIR is the
unit's own (mode 0700) and not on `/`, and it cannot write a `/tmp` or
`/var/tmp` that lies on `/` (step 1). The launcher has already
refused the probe if its own loopback or LAN listeners were reachable. From
outside, as the orchestrator, it also fails when the narrow sudoers rule is
not installed as step 6 prints it (judged by `sudo -n -ll`), when the
agent user's nftables table is not loaded (`sudo -n /usr/sbin/nft list
table inet equipa_agent`, step 4a), when a `--lan-target` does not answer
the orchestrator, when
the database or backup directories are open to others or to a group of the
agent user, when any copy in them, or any such file below
`secret_scan_roots`, is world-readable, and when
`secret_scan_roots` is empty. Exit status 0 means
everything passed. 1 means a check failed. 2 means isolation could not be
established (the message says which refusal).

### 9. Operating it

* Logs: `[Isolation]` lines show the unit, cgroup and import result of each
  agent, and when a unit waits for the one before it or runs alone.
* Running agents: `systemctl --user list-units 'equipa-agent-*'` (as `<orch>`).
  Kill one: `echo 1 > /sys/fs/cgroup/<ControlGroup of the unit>/cgroup.kill`.
* If an import fails, the ERROR line names the export bundle in the
  exchange directory (kept 24 h). Recover it with
  `git fetch <bundle> refs/equipa/worktree-state:refs/recovered/<unit>` in
  the project repository. If the launcher could not export, the clone stays
  in `~equipa-agent/.equipa-agent/<unit>/repo` (the unit's HOME and files
  are removed). An import refused because of a symbolic link (ISO-04) names
  the link; the bundle can be inspected the same way.
* Dispatch modes: run tasks with `--tasks <id> [<id> ...]`, which gives
  each task its own worktree. Single-task mode (`--task <id>`) runs the
  agent in the project's main checkout, and the isolated spawn refuses a
  main checkout, so it is refused with the flag on, as are roles that run in
  the main checkout (goal planner/evaluator).
* Per-run configs: `--dispatch-config FILE` keeps the host config's
  isolation (flag and section) unless FILE has its own section; it cannot
  turn isolation off ("Once on, it stays on"). A FILE that does not exist
  prints a `WARNING: --dispatch-config ... does not exist` line at startup
  and the run uses the defaults, isolation included as above.
* Roll back: as root, `rm /etc/equipa/require-agent-isolation`, then set
  `features.agent_isolation` to `false`. With the marker still present a
  config with the flag off is refused, by design.
