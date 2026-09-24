# Project-specific roles

Equipa resolves an agent role by checking a project's private overlay **before** the shared
base role set. This lets a project define its own roles without editing base Equipa, and keeps
two projects' same-named roles fully isolated.

## Where roles live (precedence)

For a role named `<role>` dispatched against a project, resolution is:

1. `<equipa>/prompts/<role>.md` — the shared base role. It always wins; base names are reserved.
2. `<project_dir>/.equipa/roles/<role>.md` — the project's private overlay, for NEW role names only.
   It is read from the default branch as committed (see [Trust boundary](#trust-boundary-sr-2994-sr-2997)).

A project overlay can add a role for that project. It can never shadow a base role.

## Isolation guarantee

Two projects can each define a `cybersecurity-engineer` (or any same-named role) with totally
different prompts and config. They never interfere:

- Resolution is keyed on the dispatching project's directory.
- It holds **no process-global mutable role state** — the resolver reads files fresh per call.

This is what makes per-project roles safe under `--auto-run`, which dispatches multiple
projects concurrently in a single process. Project A's role can never leak into project B.

## Self-describing roles (frontmatter)

A role `.md` may begin with optional frontmatter declaring its own config. Absent frontmatter,
behavior is identical to the legacy global role set — fully backward-compatible, no base
`constants.py` edit required.

```markdown
---
model: opus              # model tier (else dispatch-config / CLI / defaults)
turns: 35                # turn budget (else DEFAULT_ROLE_TURNS / DEFAULT_MAX_TURNS)
effort: high             # reasoning effort hint
early_term_exempt: true  # exempt from the no-file-change early-termination kill
skills: [security]       # role skill dirs
---
## CRITICAL: Bias for Action
...the prompt body begins here (frontmatter is stripped before use)...
```

### Config precedence

For `model` and `turns`, explicit operator overrides still win over a role's own frontmatter:

```
dispatch-config per-role/complexity  >  CLI --model/--max-turns  >  frontmatter  >  base defaults
```

For `early_term_exempt`, a role file's frontmatter value (when set) wins over the base
`EARLY_TERM_EXEMPT_ROLES` set.

## Adding a project role

1. `mkdir -p <project_dir>/.equipa/roles`
2. Write `<project_dir>/.equipa/roles/<role>.md` (optionally with frontmatter).
3. Dispatch it: `python forge_orchestrator.py --task <ID> --role <role> -y`.

No base-code changes, no merge conflicts on Equipa updates. See
[`examples/roles/`](../examples/roles/) for ready-to-copy starting points.

## Trust boundary (SR-2994, SR-2997)

Overlays are agent instructions in a repo that agents can write, so they are constrained:

- **Add-only.** An overlay may never shadow a base role name (anything in `prompts/`,
  `ROLE_PROMPTS`, or the reserved gate roles). It can only add new role names; a same-named
  overlay is ignored.
- **Committed on the operator's default branch only.** Overlays are read with git from the
  project root at the SHA the default branch had before the dispatch. Worktrees, uncommitted
  files, and agent task branches are never read. "Default branch" is decided from operator-controlled
  sources only, never from `refs/remotes/origin/HEAD` or the checked-out `HEAD`, because any agent
  worktree can repoint those:
  1. `project_default_branches` in `dispatch_config.json`, e.g.
     `{"project_default_branches": {"/srv/forge-share/AI_Stuff/HomeNetwork": "main"}}`. The value
     must be a plain branch name that exists as `refs/heads/<name>`. `forge-task-*` is rejected.
  2. Otherwise, exactly one of `main` / `master` must exist. If both exist, or neither does, the
     choice is ambiguous and it **fails closed**. Configure the project explicitly.
  The same trusted branch is the merge target and the security gate's diff base.
- **Fail closed.** If there is no trusted branch, a git error in the project (a corrupt
  `.git/HEAD`, a broken worktree `.git` file), or a new pin that switches branch or does not
  descend from the previous pin, every project overlay is disabled. A refused pin is logged as
  `[GATE-AUDIT] event=overlay-pin-refused`. Only a project with no `.git` anywhere above it
  reads `.equipa/roles/` from disk.
- **Capped.** Overlay `turns`/`effort` are capped by `project_role_max_turns` /
  `project_role_max_effort`. `early_term_exempt` is honoured only for roles listed in
  `early_term_exempt_project_roles`. Overlay `skills` is ignored.
- **Operator-merged.** A diff touching `.equipa/roles/` or any `.claude/` directory always blocks
  the automatic merge. `CLAUDE.md`, `AGENTS.md`, `.claude/**` and `.equipa/**` are never
  treated as doc-only, so they always get a security review.

## Dispatching by stored role (`tasks.role`)

A task can carry its own dispatch role in the `tasks.role` column. When set, it
drives dispatch wherever `--role` is not given explicitly:

- **Single task:** `--task <ID>` (no `--role`) uses the task's role; an explicit
  `--role X` still overrides it.
- **Autonomous / scan modes:** `--auto-run` and `--project <ID>` dispatch each
  task with its stored role, so a project can fan work out to specialist or
  project-overlay roles instead of always running the dev/test loop.

Role-selection precedence: explicit `--role` → `tasks.role` → `developer`.

Report-writer roles (frontmatter `early_term_exempt: true`) skip the tester
phase when they produce no code diff — the same treatment as the built-in
reviewer roles.

Set a task's role via the DB / MCP, e.g.:

```sql
UPDATE tasks SET role = 'scilab-engineer' WHERE id = 100028;
```

The `tasks.role` column is added automatically on startup (idempotent, additive)
for existing databases, and is part of `schema.sql` for fresh installs.
