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
     │                             TasksMax / MemoryMax / CPUWeight, cgroup owned by orch
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
  plus `CLAUDE_CODE_OAUTH_TOKEN`. The launcher sets HOME, USER, LOGNAME,
  SHELL and a private TMPDIR for the agent user;
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

### Fail closed

With the flag on, `_spawn_agent_process` hands every agent to
`isolation.spawn_isolated_agent`. It never falls back to the same-UID
launcher. Each of the following refuses the dispatch
(`AgentDispatchRefused: agent isolation: ...`):

* invalid, unknown or missing `agent_isolation` settings;
* the agent user is missing, is root, is the orchestrator's user, or is in a
  privileged group (`sudo`, `admin`, `wheel`, `adm`, `docker`, `lxd`,
  `incus`, `libvirt`, `kvm`, `disk`, `shadow`, `systemd-journal`, `root`);
* not Linux, no cgroup v2, no systemd user manager for the orchestrator, a
  missing `sudo`/`systemd-run`/python/launcher/CLI/git, or an exchange
  directory that is missing, not owned by the agent user, or world-writable;
* no OAuth token, or a token file readable by group or others;
* the project directory is not the root of a linked task worktree;
* `sudo` fails (no rule), or the scope does not appear within 15 s;
* the scope's `pids.max`/`memory.max`/`cpu.weight` differ from the settings,
  or `cgroup.kill` is missing or not writable;
* the launcher's own checks fail. It refuses when it runs as root, as the
  orchestrator's UID, as the wrong user or in a privileged group; when it
  runs outside the expected cgroup or with other limits; when it can read a
  `deny_read` path (the TheForge DB and its -wal/-shm, the orchestrator's
  HOME and its `.claude`, `.config`, `.ssh`, `.gitconfig`, `.netrc`, ...,
  and `mcp_config.json`); when it can write a `deny_write` path (the EQUIPA
  runtime, the launcher, the CLI, git, python, the task worktree and its
  parent, the main `.git` and checkout, the view and its directory); when it
  cannot run a hook or MCP program; or when it cannot become non-dumpable or
  a child subreaper;
* no ready line within `setup_timeout_sec`.

The feature flag is in `FAIL_CLOSED_FEATURE_FLAGS`. An invalid value
(`"yes"`) or an unreadable dispatch config turns it **on**, which refuses
dispatch until isolation is configured. A typo therefore never runs agents
as the orchestrator's user.

`rlm_decompose` starts `claude` itself, outside any launcher. With the flag
on, its sub-queries and outer agent refuse (CT-04).

## Findings

| Finding | Status (flag on) | Test |
|---|---|---|
| IR-08: every agent gets the theforge MCP server on the DB with `api_keys`; DB mode 0644 | FIXED: read-only view without `api_keys`, other servers dropped; the real DB is refused if the agent can read it | `test_view_db_drops_api_keys_and_their_rows`, `test_agent_mcp_config_points_forge_at_view_and_drops_others`, `test_launcher_denied_and_required_access`, verify script |
| Same UID with passwordless sudo | FIXED: separate user; privileged groups refused inside and outside; `sudo -n true` checked on the host | `test_agent_user_*`, `test_launcher_refuses_*`, `test_spawn_refused_by_the_real_launcher`, verify script |
| Agent can edit the runtime, `~/.gitconfig`, the main `.git` | FIXED: per-agent clone; the agent has no write access (launcher `deny_write`) | `test_clone_round_trip_imports_only_the_task_branch`, `test_import_refuses_when_branch_moved_since_dispatch`, `test_worktree_on_default_branch_refuses`, verify script |
| Only the needed credential reaches the agent | FIXED: `CLAUDE_CODE_OAUTH_TOKEN` only | `test_agent_env_keeps_only_the_oauth_token`, `test_isolated_agent_end_to_end`, verify script |
| CT-01: agent SIGKILLs its launcher | FIXED: the scope is killed as soon as the launcher is gone | real-host hostile probe (launcher SIGKILLed, C fork-hopper left behind, scope empty afterwards); `test_cgroup_kill_and_stale_scope_sweep` |
| CT-02: pid-hopper escapes the `/proc` sweep | FIXED: `cgroup.kill` | same real-host probe; `test_scope_verification` |
| CT-03: containment failures fail open | FIXED for setup: every failure refuses the dispatch. **NOT FIXED** for teardown: a scope that survives `cgroup.kill` (D-state) or a failed import is logged at ERROR but does not mark the run failed, because that needs result plumbing in `agent_runner.py`, which another task is editing | `test_spawn_refused_*`, `test_agent_runner_refuses_instead_of_falling_back` |
| CT-04: rlm_decompose spawns the CLI outside the launcher | FIXED: refuses with the flag on | `test_rlm_decompose_refuses_direct_cli_spawns` |
| SR3116-01/02: the reviewer's git view can be steered through agent-writable config | FIXED for config: no agent-writable git config, attributes or submodule config reaches the orchestrator's repository. Residual: in-tree `.gitattributes` in the reviewed commit still apply | `test_clone_round_trip_imports_only_the_task_branch` |

## Residual risks and limitations

* **All agents share one agent UID.** Concurrent agents can read, write and
  signal each other's processes and clones (not the orchestrator's). If this
  matters, use a pool of agent users (future work).
* The network is not restricted. An agent can still exfiltrate what it
  legitimately holds: its OAuth token and the code it works on.
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
* `/tmp` is still shared. The agent gets a private `TMPDIR`.
* The orchestrator's own sudo is untouched. With isolation it is no longer
  reachable from agents, but narrowing it is still good hygiene.

## Operator runbook

Placeholders: `<orch>` is the orchestrator user, `<runtime>` the EQUIPA
checkout the orchestrator runs from, `<db>` the TheForge database. Run every
step as an administrator unless noted. Nothing here is done by EQUIPA itself.

### 0. Requirements

Linux 5.14+ with cgroup v2 (`/sys/fs/cgroup/cgroup.controllers` exists),
systemd with user managers, sudo 1.8.7+, git 2.29+. The Claude CLI must be
installed at a system path readable and executable by other users (for
example `/usr/local/bin/claude`), not under `<orch>`'s HOME.

### 1. Create the agent user

```bash
useradd --system --create-home --home-dir /var/lib/equipa-agent \
        --shell /bin/bash --user-group equipa-agent
passwd -l equipa-agent                    # no password login
chmod 0700 /var/lib/equipa-agent
id equipa-agent                           # must show NO sudo/admin/wheel/adm/docker/lxd/... group
loginctl disable-linger equipa-agent      # no user manager: it could start units that outlive agents
echo equipa-agent >> /etc/cron.deny       # no cron/at persistence
echo equipa-agent >> /etc/at.deny
```

Set the git identity the agent commits with (it cannot read `<orch>`'s):

```bash
sudo -u equipa-agent git config --global user.name  "Forgeborn"
sudo -u equipa-agent git config --global user.email "<address>"
```

(The launcher also copies `user.name`/`user.email` from the task worktree into
each clone.)

### 2. Exchange and view directories

```bash
install -d -o equipa-agent -g equipa-agent -m 0711 /var/lib/equipa-agent/exchange
install -d -o <orch> -g <orch> -m 0755 /var/lib/equipa-view
```

The exchange directory is where agents leave their export bundles. It is
owned by the agent user and entered, not listed, by the orchestrator. The
view directory is written only by the orchestrator.

### 3. Lock down what the agent must not read or write

```bash
chmod 0700 ~<orch>                           # or 0711 if the runtime lives under it
chmod 0700 ~<orch>/.claude ~<orch>/.config ~<orch>/.ssh 2>/dev/null
chmod 0600 <db> <db>-wal <db>-shm 2>/dev/null # TheForge: owner only (was 0644)
chmod 0600 <runtime>/mcp_config.json <runtime>/.env 2>/dev/null
chmod -R go-w <runtime>                      # runtime read-only to others
chmod o+x <runtime>                          # traversable, not listable
chmod -R o+rX <runtime>/equipa <runtime>/hooks <runtime>/skills <runtime>/scripts
```

The agent must be able to **read and execute** the launcher, the hook script
and the python named in the generated hook settings. Keep the runtime's
virtualenv, if any, inside the runtime rather than in `<orch>`'s HOME.
Project repositories need no agent access at all, because agents get
bundles. Just make sure they are not writable by others (`chmod -R o-w`).

### 4. The orchestrator's user manager and controller delegation

```bash
loginctl enable-linger <orch>
cat /sys/fs/cgroup/user.slice/user-$(id -u <orch>).slice/user@$(id -u <orch>).service/cgroup.subtree_control
# must list: cpu memory pids. If not:
mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=pids memory cpu\n' > /etc/systemd/system/user@.service.d/delegate.conf
systemctl daemon-reload   # then restart the orchestrator's user manager (log out / reboot)
```

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

### 7. Configure and switch on

In `dispatch_config.json`:

```json
"features": { "agent_isolation": true },
"agent_isolation": {
    "agent_user": "equipa-agent",
    "python": "/usr/bin/python3",
    "claude_executable": "/usr/local/bin/claude",
    "exchange_dir": "/var/lib/equipa-agent/exchange",
    "view_db_path": "/var/lib/equipa-view/theforge-view.db",
    "oauth_token_file": "/home/<orch>/.equipa-agent-token",
    "pids_max": 512,
    "memory_max": "4G",
    "cpu_weight": 100
}
```

Other keys, with defaults: `launcher` (this checkout's
`equipa/agent_launcher.py`), `git_executable` (`/usr/bin/git`), `sudo`,
`systemd_run`, `setup_timeout_sec` (300), `stop_timeout_sec` (120),
`max_export_bytes` (2 GiB), `forge_mcp_server` (`theforge`),
`allowed_mcp_servers` (`["theforge"]`), `exclude_tables` (`["api_keys"]`),
`deny_read`/`deny_write` (extra absolute paths), `privileged_groups`,
`carry_ignored_paths` (`[".equipa-artifacts"]`). Unknown keys are refused.

### 8. Verify on the real host

As `<orch>`, from `<runtime>`, with the orchestrator's environment:

```bash
scripts/verify_agent_isolation.sh --repo /path/to/a/project
```

It runs its own checks as an isolated agent through the real path (scope,
sudoers rule, launcher, handoff). Every line must be `PASS`, and the last
line must be `RESULT: PASS`. The checks: runs as the agent user and cannot
sudo; cannot read the DB, the orchestrator HOME and its secrets, or
`mcp_config.json`; cannot write any listed `.git`, the runtime or the
launcher; is in its own `equipa-agent-*.scope` with `pids.max` and
`memory.max` set; cannot raise its limits or write any `cgroup.procs`; cannot
signal the orchestrator; the view is read-only and has no `api_keys`; no
credential except the OAuth token is in its environment. Exit status 0 means
everything passed. 1 means a check failed. 2 means isolation could not be
established (the message says which refusal).

### 9. Operating it

* Logs: `[Isolation]` lines show the unit, cgroup and import result of each
  agent.
* Running agents: `systemctl --user list-units 'equipa-agent-*'` (as `<orch>`).
  Kill one: `echo 1 > /sys/fs/cgroup/<ControlGroup of the unit>/cgroup.kill`.
* If an import fails, the ERROR line names the export bundle in the
  exchange directory (kept 24 h). Recover it with
  `git fetch <bundle> refs/equipa/worktree-state:refs/recovered/<unit>` in
  the project repository. If the launcher could not export, the clone stays
  in `~equipa-agent/.equipa-agent/<unit>/repo`.
* Roll back: set `features.agent_isolation` to `false`. Nothing else changes.
