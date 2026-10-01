# Orchestrator CLI Reference

> Most users should use [Coordinator Mode](README.md) — just talk to Claude. This page documents the CLI for automation, scripting, and advanced usage.

---

## Running Tasks

```bash
# Single task by ID
python forge_orchestrator.py --task 42 --dev-test -y

# Multiple tasks in parallel
python forge_orchestrator.py --tasks 42,43,44 --dev-test -y

# Task range
python forge_orchestrator.py --tasks 100-104 --dev-test -y

# Auto-dispatch: scan all projects, prioritize, run
python forge_orchestrator.py --dispatch -y

# Dry run: see what would be dispatched without running
python forge_orchestrator.py --dispatch --dry-run
```

## Goal-Driven Mode

Give a high-level goal and the planner agent breaks it into tasks:

```bash
python forge_orchestrator.py --goal "Add JWT authentication to the API" --goal-project 1 -y
```

The planner creates prioritized, dependency-aware tasks. Then the orchestrator dispatches agents for each one.

## Security Review

```bash
# After a dev-test run
python forge_orchestrator.py --task 42 --dev-test --security-review -y

# Standalone security review
python forge_orchestrator.py --task 42 --role security-reviewer -y
```

Security reviews auto-dispatch after dev-test when `security_review: true` is set in `dispatch_config.json` (enabled by default).

## Specific Agent Roles

Run a specific role on a task:

```bash
python forge_orchestrator.py --task 42 --role developer -y
python forge_orchestrator.py --task 42 --role tester -y
python forge_orchestrator.py --task 42 --role security-reviewer -y
python forge_orchestrator.py --task 42 --role code-reviewer -y
python forge_orchestrator.py --task 42 --role debugger -y
```

## Project Management

```bash
# Add a new project
python forge_orchestrator.py --add-project "MyProject" --project-dir "/path/to/code"

# List registered projects
python forge_orchestrator.py --list-projects
```

## ForgeSmith Self-Improvement

```bash
# See what ForgeSmith would change (dry run)
python forgesmith.py --dry-run

# Apply improvements
python forgesmith.py --auto

# Check ForgeSmith run history
python forgesmith.py --status
```

ForgeSmith runs nightly via cron (configured during setup). It analyzes agent performance and tunes prompts, turn limits, and model assignments automatically.

## Dashboard

```bash
python forge_dashboard.py
```

Terminal-based view of task completion rates, blocked items, agent performance, and session activity.

## Database Migration

```bash
# Detect version and apply pending migrations (v0 through v4)
python db_migrate.py /path/to/theforge.db

# Run the migration benchmark (reproducible demo)
python benchmark_migrations.py
```

## Manual Setup

If you prefer not to use the guided installer:

```bash
# 1. Clone the repository
git clone <repo-url> equipa
cd equipa

# 2. Initialize the database
sqlite3 theforge.db < schema.sql

# 3. Run migrations (sets PRAGMA user_version)
python db_migrate.py theforge.db

# 4. Copy and edit the config
cp config.example.json forge_config.json
# Edit forge_config.json with your paths

# 5. Generate MCP config for Claude Code
# (or let equipa_setup.py do it)

# 6. Verify
python forge_orchestrator.py --help
```

## Configuration

### forge_config.json

| Setting | What it does |
|---|---|
| `theforge_db` | Path to the SQLite database |
| `project_dirs` | Map of project names to local paths |
| `github_owner` | GitHub username for repo operations |
| `prompts_dir` | Path to agent prompt files |
| `mcp_config` | Path to MCP server config |

### dispatch_config.json

| Setting | What it does |
|---|---|
| `model` | Default model for all agents |
| `model_<role>` | Model override per role (e.g., `model_tester`) |
| `max_turns_<role>` | Turn budget per role |
| `provider` | Default provider (`claude` or `ollama`) |
| `provider_<role>` | Provider override per role |
| `security_review` | Auto-dispatch security review after dev-test (default: true) |
| `security_review_timeout` | Timeout for security reviews in seconds |
| `max_concurrent_agents` | Parallel dispatch limit (default: 4) |
| `task_type_prompts` | Per-task-type prompt supplements |
| `agent_env_passthrough` | Exact extra environment variable names agents (and preflight builds) receive. No wildcards |
| `agent_allow_api_key` | Must be `true` to pass through any `ANTHROPIC_*` or `CLAUDE_CODE_USE_*` name (API billing or another endpoint) |
| `agent_allow_credentials` | Must be `true` to pass through any other credential-shaped name (`DATABASE_URL`, `PG*`, `*_TOKEN`, `*_KEY`, `*SECRET*`, `*PASSWORD*`) |

### MCP server config

Agents run in the project directory, and the MCP servers the agent CLI starts
inherit that directory. Anything a launcher resolves from its working
directory is therefore agent-writable, so every dispatch is refused unless
each stdio server in the MCP config matches one of these shapes (an
allowlist; task 3134, IR-03):

- an absolute `python` with `-I` running an absolute script, or `-I -m` with
  an absolute `cwd` outside every project directory;
- an absolute `node` running an absolute script, with no preload, loader or
  eval option (`-r`, `--require`, `--import`, `--loader`, `-e`, `-p`, ...);
- the server's own absolute executable file (such as `uvx`), outside every
  project directory.

Wrappers (`timeout`, `nice`, `env`, `stdbuf`, `setsid`, `busybox`, ...),
shells, other runtimes and package or task runners (`uv`, `poetry`, `pipx`,
`npx`, `deno`, `go`, `make`, ...) are refused, because each loads code the
check cannot follow: `timeout python3 -m x` or `uv run` still runs a module or
`.venv` planted in the project, and `node -r <name>` loads the project's
`node_modules`. A server `env` may not set `NODE_OPTIONS`, `PYTHONPATH`,
`PYTHONSTARTUP`, `LD_*` or similar code-loading variables, and a server entry
that is not a JSON object is refused. See `mcp_config.example.json` for the
accepted forms.

`--db-path` must be absolute too. **Deploy check:** an MCP config written
before this rule (for example `"--db-path", "theforge.db"`) makes every
dispatch refuse with an error that names the config file and the absolute path
to use; edit the file before deploying.

The Claude CLI currently ignores a per-server `cwd` and starts every stdio
server in its own working directory, the project. That is why python needs
`-I` (the project directory never reaches `sys.path`) and why the example runs
`equipa/mcp_server.py` as an absolute script instead of `-m`. The `equipa` MCP
server starts its dispatch child (`python -m equipa.cli`) from its own
checkout for the same reason.

### Project-scope Claude configuration is ignored

Every Claude CLI run EQUIPA starts (agents, reviewers, reflexion, the RLM
`claude -p` calls, forgesmith) passes `--setting-sources ""` (the empty
value as its own argument: no settings file at all) and
`--strict-mcp-config` (`equipa/cli_isolation.py`, IR-01). The CLI runs in the
agent-writable project directory, and by default it loads project-scope
configuration from there: one `{"disableAllHooks": true}` in
`.claude/settings.json` switched off the PreToolUse Bash gate for every later
run in that project, an `env` block reached the agent's tools, `CLAUDE.md` was
read as standing instructions for later tester and reviewer runs, and
`.mcp.json` could add servers. With the two flags the CLI reads no user,
project or local settings file, only the `--settings` file EQUIPA passes (a
flag source, which `--setting-sources ""` does not switch off, so the
PreToolUse gate still applies) and only the servers in EQUIPA's own
`--mcp-config`. A project `CLAUDE.md` is therefore no longer seen by agents;
put anything agents must know into the task or role prompt. A claude argv that
asks for any settings source (`user`, `project`, `local`) or gives the flag no
value is refused, not rewritten.

### User-scope Claude configuration is not loaded either

Until task 3153 the CLI was started with the setting source `user`, which
loads the USER scope. Without agent isolation that was the operator's
`~/.claude`, which agents running as the orchestrator's user can write. The
independent review (RR3144-A) showed four
ways that switched the Bash gate off with the real CLI: a user-scope
`env.SHELL` naming a planted shell, a user-scope `env.BASH_FUNC_ls%%`, a user
PreToolUse hook answering `updatedInput` (the CLI runs the rewritten command,
the gate judged the original) and a function in `~/.bashrc`.

Every Claude CLI run EQUIPA starts (agents, testers, reviewers and
reflexion through `_spawn_agent_process`; the RLM `claude -p` calls; the
ForgeSmith GHOST scout and OPRO proposals, SIMBA rule generation and the
autoresearch prompt mutation through `claude_cli_run_env`, and the
autoresearch SSH path through `REMOTE_RUN_CONFIG_DIR_PREFIX`) therefore gets
its **own CLAUDE_CONFIG_DIR** (`equipa/cli_isolation.py`). A drift fence in
`tests/test_cli_config_isolation_3150.py` fails when a new `claude` argv
appears in a function that uses none of these:

- created per run with `mkdtemp` (mode 0700, owned by the orchestrator's
  user, checked after creation) in the orchestrator's temp directory;
- seeded with nothing, so no user `settings.json`, hooks, `env` block,
  `CLAUDE.md` or `.claude.json` MCP servers exist for the run. The CLI
  authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in its environment, which
  needs nothing in the directory;
- removed when the run ends (`_terminate_agent`, every exit path; a
  finalizer covers a process object that is never terminated). A process
  that is SIGKILLed or OOM-killed runs none of these, so the first per-run
  directory any EQUIPA process creates also removes the `equipa-claude-config-*`
  directories in the same temp directory that are owned by its user, are real
  directories (never symlinks) and have not changed for 24 hours
  (`sweep_stale_run_config_dirs`, R3150-08).

The directory is still writable by the run itself: the CLI writes its state
there, and an agent running as the same user can write there too. The CLI
re-reads a `settings.json` in its config directory while it runs, so a hook
written there mid-run used to run outside the gate (R3150-01). With
`--setting-sources ""` the CLI reads no settings file from the directory at
any time.

The CLI environment also pins `CLAUDE_CODE_SHELL` to an absolute,
root-owned bash (`/bin/bash`, verified at run time), which the CLI prefers
over `SHELL`, and drops `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `SHELLOPTS`,
`BASHOPTS`, `PS4` and every `BASH_FUNC_*` name, even when an operator
passthrough lists them. Under agent isolation the launcher sets the unit's
own HOME and CLAUDE_CONFIG_DIR, and the same pin and removals apply
(`claude_cli_shell_env`).

**Deploy note:** the login in `~/.claude/.credentials.json` is no longer
used by any of these runs. Export `CLAUDE_CODE_OAUTH_TOKEN` (create one
with `claude setup-token`) for the orchestrator and for the ForgeSmith,
SIMBA and autoresearch jobs (cron does not read `~/.bashrc`), or every run
fails to authenticate; the orchestrator logs a WARNING naming the variable
on the first run without it. The startup check in `equipa/cli.py` still
accepts the credentials file as a login and does not yet say this.

With `bash_security_pretooluse` on, the `--settings` file EQUIPA generates
is the second layer. It pins `"disableAllHooks": false` (flag-scope
settings outrank every file scope, should one ever be loaded again; RR-03),
pins `CLAUDE_CODE_SHELL` to the same
absolute bash, and sets to empty the variables an `env` block could use to
turn hooks off (`CLAUDE_CODE_SAFE_MODE`, `CLAUDE_CODE_SIMPLE`) or to run code
around every command (`BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `LD_PRELOAD`,
`NODE_OPTIONS`, `CLAUDE_CODE_SHELL_PREFIX`, `CLAUDE_ENV_FILE`, ...; the list
is `SETTINGS_ENV_NEUTRALISED` in `equipa/agent_runner.py`). An operator
passthrough of one of those names is overridden as well. With the flag off
no `--settings` file is passed; the per-run config directory and the CLI
environment above still apply.

**Live check:** `scripts/verify_gate_config_isolation.sh` (run by the
operator with `CLAUDE_CODE_OAUTH_TOKEN` exported; five short CLI runs) plants
each of the review's four vectors in a throwaway HOME and runs the real CLI
through EQUIPA's own `build_cli_command` and `_spawn_agent_process` with the
gate on. A fifth probe (W) has the agent's first tool call write a
`settings.json` with a PreToolUse hook into the run's own config directory
after the CLI has started, and then checks that a later command the gate
blocks is still blocked and the planted hook never ran. It reports PASS, FAIL
or INCONCLUSIVE per probe. The three user-scope probes and W must PASS; the
`~/.bashrc` probe is expected to FAIL until agent isolation (see the
limitations below). No probe covers the shell snapshot (next section).

### Bash gate limitations without agent isolation

Until agent isolation is on (separate agent user and a per-unit HOME,
[AGENT_ISOLATION.md](AGENT_ISOLATION.md)), the gate is a guard rail, not a
boundary, for these reasons:

- **`~/.bashrc` functions are still a bypass (RR3144-A (4)).** The CLI
  takes a snapshot of the user's shell with `bash -c -l`, which sources the
  rc files in `HOME`, and replays it before every command. A function `ls()`
  planted in `~/.bashrc` runs in place of the `ls` the gate allowed. The
  per-run CLAUDE_CONFIG_DIR does not change `HOME`, so this stays open until
  each unit has its own HOME. Before relying on the gate, check that the
  host's `~/.bashrc`, `~/.bash_profile` and `~/.profile` define no functions
  or aliases for common commands.
- **The hook and its checker live in an agent-writable tree (RR3144-B).**
  `hooks/pretooluse_bash_gate.py` loads `equipa/bash_security.py` by path
  from the deployed checkout, which is owned by the user agents run as. An
  agent that edits either file turns the gate into allow-all for every later
  run. `-I` on the hook's interpreter does not address this. The deploy must
  make the runtime tree read-only to the agent user, which agent isolation
  provides; until then, treat both files as part of the deploy checklist.
- **The run's own config directory is writable by the run, with or without
  isolation.** The CLI needs to write to it, and the agent shares its user;
  under agent isolation the unit's directory (`<unit home>/.claude`) is
  created by and owned by the agent user. A `settings.json` written there is
  ignored (`--setting-sources ""`; the isolated launcher refuses a claude
  argv without it). Other files the CLI both writes there and later reads are
  not protected: a function appended to the shell snapshot in
  `shell-snapshots/` mid-run replaces a command the gate allowed (R3150-07),
  in an isolated unit as well. For an unattended agent the rest of its own
  run is the whole window the gate exists for, so the gate stays a guard rail
  even under isolation while the CLI's config directory is writable by the
  agent's user. Without isolation, concurrent runs also share the user, so
  one agent can write into another run's live directory.

## Agent isolation: what is and is not covered

Agents run as the orchestrator's Unix user. The current controls are defence
in depth for that shared UID:

- Agent CLIs, operator hooks, preflight install/build commands and the RLM
  `claude -p` calls get an allowlisted environment (`equipa/env_loader.py`),
  never the orchestrator's.
- Before the first agent starts, the orchestrator makes itself non-dumpable
  (`PR_SET_DUMPABLE` 0, Linux only), so an agent cannot read the
  orchestrator's `/proc/<pid>/environ` or `/proc/<pid>/mem`. Children regain
  dumpability when they exec, so agents are unaffected. Keep credentials in
  the `.env` file rather than the environment of the shell that starts the
  orchestrator: other same-UID processes, such as that shell, stay readable.
- The reactive Bash check runs in a separate worker process
  (`equipa/reactive_check.py`); a check that misses its deadline is a block
  and the worker is recycled.
- Tool inputs are redacted before they are persisted to `agent_actions`.

None of this stops a same-UID process from reading files the orchestrator's
user can read (`~/.pgpass`, `~/.config/gh`, `~/.claude`, `.env`), or from
writing into another run's live per-run config directory (its shell snapshot
replaces commands the gate allowed). Nor does it
cover short-lived orchestrator helpers that exec with the full environment
(git, docker): after exec they are dumpable again, so their
`/proc/<pid>/environ` is readable while they run. **The
complete fix is running agents under a separate Unix user** without access to
those files. That is planned for a later wave.

### Ollama (Local Models)

```json
{
  "provider": "claude",
  "provider_planner": "ollama",
  "provider_evaluator": "ollama",
  "ollama_base_url": "http://localhost:11434",
  "ollama_model": "qwen3.5:27b"
}
```

Read-only roles (planner, evaluator, code-reviewer, researcher) work well on local models at zero API cost.

---

<p align="center">
  <em>For day-to-day use, just talk to Claude. See the <a href="README.md">README</a>.</em>
</p>

