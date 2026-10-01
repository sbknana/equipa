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
  the project's own and are kept.

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

The CLI still passes `--setting-sources user --strict-mcp-config` (task
3134); with the per-unit `CLAUDE_CONFIG_DIR` the user scope is empty.

### Reviewers run alone (until there is a UID pool)

A per-unit HOME does not help against a unit running *at the same time*:
every unit has the same UID, so a developer agent of task A could read the
handed-over prompt (with the provenance and completion nonces) of a security
reviewer running for task B, and write into that reviewer's clone, which is
exported into task B's worktree (review R3136-03, GP2-02). Until the agents
get separate UIDs, reviewer units never overlap any other isolated unit:

* `security-reviewer` and `code-reviewer` units (`EXCLUSIVE_ROLES`) wait
  until every running isolated unit has ended, and until no agent scope of
  the orchestrator user is populated (a unit that survived `cgroup.kill`, or
  one started by an orchestrator without the lock; a scope whose
  orchestrator is gone is killed by the sweep instead of waited for);
* while a reviewer waits or runs, every new unit waits (writer preference,
  so a steady stream of developers cannot starve the review), and so does a
  second reviewer;
* ordinary units run side by side as before.

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
`build_cli_command(role=...)`, which both reviewer call sites use. Waiting
is logged (`[Isolation] ... waits for ...`, repeated every 5 minutes, and
`... runs alone`); a wait longer than `unit_wait_timeout_sec` (default 6 h)
refuses the dispatch. The cost is throughput: in a parallel wave, a review
holds back the other tasks' next units while it runs.

**Long-term fix:** a pool of agent users, one UID per unit, or at least a
reviewer UID that no developer, tester or debugger unit ever shares. The
reviewer's handed-over files and clone would then be closed to every other
unit by ordinary file permissions, and the lock above could go.

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
`--setting-sources user --strict-mcp-config` pair.

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

### Fail closed

With the flag on, `_spawn_agent_process` hands every agent to
`isolation.spawn_isolated_agent` (the Ollama provider, which never reaches
it, is refused before it runs anything). It never falls back to the same-UID
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
* the launcher, python, CLI, git, the exchange or view directory, or a hook
  or MCP program lies inside the TheForge database directory or a
  `db_backup_dirs` entry (those must be closed to the agent);
* `sudo` fails (no rule), or the scope does not appear within 15 s;
* the scope's `pids.max`/`memory.max`/`cpu.weight` differ from the settings,
  or `cgroup.kill` is missing or not writable;
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
  cannot run a hook or MCP program; or when it cannot become non-dumpable or
  a child subreaper;
* no ready line within `setup_timeout_sec`;
* a reviewer unit (or a unit held back by one) waited longer than
  `unit_wait_timeout_sec`, or the unit lock in the runtime directory cannot
  be opened.

The Ollama provider is refused with a blocked result rather than an
`AgentDispatchRefused` (see above).

The feature flag is in `FAIL_CLOSED_FEATURE_FLAGS`. An invalid value
(`"yes"`) or an unreadable dispatch config turns it **on**, which refuses
dispatch until isolation is configured. A typo therefore never runs agents
as the orchestrator's user.

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
| R3136-03 MEDIUM: concurrent units share one UID under parallel dispatch | MITIGATED until a UID pool exists: reviewer units run alone (host-wide reader-writer lock, logged; see "Reviewers run alone"). Long-term fix: per-unit or per-role UIDs | `test_reviewer_waits_for_running_units_and_blocks_new_ones`, `test_reviewers_do_not_overlap_each_other`, `test_reviewer_waits_for_live_agent_scopes`, `test_units_in_another_process_are_waited_for`, `test_unit_lock_ignores_another_xdg_runtime_dir`, `test_wait_past_the_timeout_refuses_and_holds_nothing`, `test_spawn_holds_the_slot_until_the_agent_is_released`, `test_failed_setup_gives_the_slot_back`, `test_a_replaced_lock_file_cannot_let_a_reviewer_overlap`, `test_unit_lock_directory_must_be_private`, `test_build_cli_command_marks_the_role_for_the_isolated_spawn`, `test_reviewer_spawn_sites_build_their_command_with_the_role` |
| R3136-04 LOW: cron/at denial is only a runbook line | FIXED: the launcher refuses when the agent lingers or `crontab -l`/`at -l` are not denied; the verify script checks the same | `test_launcher_refuses_an_agent_that_may_use_cron`, `test_launcher_refuses_an_agent_that_may_use_at`, `test_launcher_refuses_a_lingering_agent_user`, `test_launcher_accepts_denied_cron_and_at`, `test_verify_script_fails_when_the_agent_may_schedule_jobs`, `test_verify_script_passes_denied_cron_and_at` |
| R3136-05 LOW: the out-of-tree link test is lexical | FIXED: links are resolved through the tree's other links, 40 hops at most | `test_link_escape_follows_links_of_the_tree`, `test_import_refuses_a_link_pair_that_resolves_outside`, `test_import_accepts_a_link_pair_that_stays_inside` |
| R3136-06 LOW: autoresearch starts `claude --print` through `bash -c` | FIXED: refused with the flag on (and when `equipa` cannot be imported); direct argv with stdin and the isolation args | `test_autoresearch_mutation_refuses_with_isolation_on`, `test_autoresearch_mutation_runs_the_cli_without_a_shell`, `test_every_direct_claude_spawn_is_gated_by_the_refusal` |
| R3136-07 INFO: the fences prove the refusal exists, not that it gates the spawn | FIXED: the new fences require an `if` on the refusal that returns or raises before the spawn, in an enclosing block | `test_gate_detection_needs_an_exit_before_the_spawn`, `test_every_preflight_spawn_is_gated_by_the_refusal`, `test_every_direct_claude_spawn_is_gated_by_the_refusal`, `test_claude_fence_sees_a_shell_string_spawn` |
| R3136-08 INFO: the database-copy search covers only the configured directories | FIXED: SQLite files below `secret_scan_roots` and `--repo` whose schema holds an excluded table are probed by name as the agent, and a world-readable one fails the outer check | `test_credential_databases_below_project_roots_are_found`, `test_probe_command_probes_credential_copies_as_the_agent`, `test_outer_checks_fail_on_a_world_readable_credential_copy` |

## Residual risks and limitations

* **All agents share one agent UID.** Sequential units no longer share
  anything (per-unit HOME, ISO-02), and reviewer units never overlap another
  unit (R3136-03). Other *concurrent* agents (developers, testers and
  debuggers of a parallel wave) can still read, write and signal each
  other's processes and unit directories (not the orchestrator's), so one
  task's developer can tamper with another task's developer or tester
  clone while both run; that work still passes that task's own review.
  Closing it needs a pool of agent users with one UID per unit (future
  work), which would also let reviewers run in parallel again.
* The Ollama provider cannot be used with the flag on (R3136-01).
* External lifecycle hooks (`equipa/hooks`) run their operator-configured
  command through the shell, as the orchestrator user, with the task
  worktree as the working directory; nothing refuses them with the flag on.
  The command text is the operator's (task data travels in the
  environment), but a command that names a project file (`./check.sh`,
  `npm run ...`, `make`) runs whatever the imported agent work put there.
  With the flag on, configure hooks only with absolute paths outside every
  project. Refusing or isolating them is future work.
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
useradd --system --no-create-home --home-dir /var/lib/equipa-agent \
        --shell /bin/bash --user-group equipa-agent
passwd -l equipa-agent                    # no password login
# The passwd HOME is root-owned and NOT agent-writable: the launcher refuses
# otherwise. Each agent gets its own HOME below the state root instead.
install -d -o root -g root -m 0711 /var/lib/equipa-agent
install -d -o equipa-agent -g equipa-agent -m 0700 /var/lib/equipa-agent/.equipa-agent
id equipa-agent                           # must show NO sudo/admin/wheel/adm/docker/lxd/... group
loginctl disable-linger equipa-agent      # no user manager: it could start units that outlive agents
echo equipa-agent >> /etc/cron.deny       # no cron/at persistence
echo equipa-agent >> /etc/at.deny
```

The launcher refuses to start an agent while the agent user lingers or may
use `crontab` or `at` (if they are installed), and the verify script checks
the same. If `/etc/cron.allow` or `/etc/at.allow` exists, it takes
precedence: leave the agent user out of it instead.

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
find / -xdev -type f -name '*.db*' -newer <db> -perm -o=r 2>/dev/null  # stray copies: move them into <backup-dir>

chmod 0600 <runtime>/mcp_config.json <runtime>/.env 2>/dev/null
chmod -R go-w <runtime>                      # runtime read-only to others
chmod o+x <runtime>                          # traversable, not listable
chmod -R o+rX <runtime>/equipa <runtime>/hooks <runtime>/skills <runtime>/scripts

# Project checkouts: agents get bundles, so they need no access at all.
chmod -R o-rwx <projects>/<each project>     # or a group the agent user is not in
```

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
    "db_backup_dirs": ["/path/to/theforge-backups"],
    "secret_scan_roots": ["/path/to/projects"],
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
`carry_ignored_paths` (`[".equipa-artifacts"]`), `db_backup_dirs` (`[]`:
directories with TheForge backups, closed to the agent like the database's
own directory), `secret_scan_roots` (`[]`: project roots the verify script
scans; it fails while this is empty), `unit_wait_timeout_sec` (21600: how
long a reviewer may wait for the running units, or any unit for a reviewer,
before the dispatch is refused). Unknown keys are refused.

### 8. Verify on the real host

As `<orch>`, from `<runtime>`, with the orchestrator's environment:

```bash
scripts/verify_agent_isolation.sh --repo /path/to/a/project
```

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
excluded table (found by the orchestrator, probed by name). From outside, as
the orchestrator, it also fails when
the database or backup directories are open to others or to a group of the
agent user, when any copy in them, or any such file below
`secret_scan_roots`, is world-readable, and when
`secret_scan_roots` is empty. Exit status 0 means
everything passed. 1 means a check failed. 2 means isolation could not be
established (the message says which refusal).

### 9. Operating it

* Logs: `[Isolation]` lines show the unit, cgroup and import result of each
  agent, and when a unit waits for a reviewer or a reviewer runs alone.
* Running agents: `systemctl --user list-units 'equipa-agent-*'` (as `<orch>`).
  Kill one: `echo 1 > /sys/fs/cgroup/<ControlGroup of the unit>/cgroup.kill`.
* If an import fails, the ERROR line names the export bundle in the
  exchange directory (kept 24 h). Recover it with
  `git fetch <bundle> refs/equipa/worktree-state:refs/recovered/<unit>` in
  the project repository. If the launcher could not export, the clone stays
  in `~equipa-agent/.equipa-agent/<unit>/repo` (the unit's HOME and files
  are removed). An import refused because of a symbolic link (ISO-04) names
  the link; the bundle can be inspected the same way.
* Roll back: set `features.agent_isolation` to `false`. Nothing else changes.
