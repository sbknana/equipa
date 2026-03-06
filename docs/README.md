<p align="center">
  <img src="ProjectPombal.png" alt="Project Pombal" width="200">
</p>

<h1 align="center">Project Pombal</h1>

<p align="center">
  <strong>Tell Claude what you want built. Agents do the rest.</strong>
</p>

<p align="center">
  <em>Coordinate. Build. Ship.</em>
</p>

<p align="center">
  <em>Named after the <a href="https://en.wikipedia.org/wiki/Marquis_of_Pombal">Marquis de Pombal</a>, who coordinated the rebuilding of Lisbon after the 1755 earthquake</em>
</p>

---

## What is this?

Project Pombal is an AI-powered development team you control through conversation. You talk to Claude in plain English. Claude manages everything else — planning, task creation, agent dispatch, progress tracking, error recovery, and reporting.

No CLI commands to memorize. No task IDs to look up. No SQL to write. You make decisions. Claude handles execution.

---

## See It in Action

### Simple Feature

```
You: "Add a dark mode toggle to the settings page"

Claude: I'll create a task for that. Let me check your project structure...

[Reads your codebase, finds Settings.tsx]
[Creates task in DB: "Add dark mode toggle to settings page"]
[Dispatches developer agent]
[Developer writes the code, creates tests]
[Tester validates]

Claude: Done. Dark mode toggle added to src/components/Settings.tsx.
Uses your existing theme context. Toggle persists to localStorage.
All 3 tests passing.
```

One sentence from you. Full implementation with tests.

### Multi-Step Feature

```
You: "Build me user authentication with Google OAuth"

Claude: That's a multi-step feature. Let me plan it out:

  1. Set up OAuth provider configuration
  2. Create login/callback routes
  3. Add session management middleware
  4. Build the login UI component
  5. Write integration tests

[Creates 5 tasks in DB with proper descriptions]
[Tasks 1-3 run in parallel — no dependencies]
[Task 4 waits for task 2 — needs the routes]
[Task 5 waits for all — needs everything wired up]

Claude: All 5 tasks complete.
- OAuth config in src/config/auth.ts
- Routes at /auth/google and /auth/callback
- Session middleware using signed cookies
- Login button component with Google branding
- 8 integration tests, all passing

Want me to run a security review on the auth flow?
```

Claude identified the dependencies automatically. Independent tasks ran in parallel.

### Bug Fix

```
You: "Users are getting 500 errors on the checkout page"

Claude: Let me investigate. I'll start with a debugger task...

[Dispatches debugger agent]
[Debugger analyzes error logs, traces the stack, finds root cause]

Claude: Found it. Null pointer in calculateTotal() when the cart has a
discounted item with no original price. Creating a fix task...

[Dispatches developer agent]
[Developer fixes the bug, adds edge case tests]
[Tester validates the fix]

Claude: Fixed. The issue was in src/utils/cart.ts line 47.
Added null check for originalPrice. 4 new test cases covering
discount edge cases. All passing.
```

Debug, fix, and test — all from one sentence.

### Security Review

```
You: "Review the auth module for security issues"

Claude: I'll dispatch a security reviewer.

[Dispatches security-reviewer with Trail of Bits tooling]

Claude: Security review complete. 2 findings:

  MEDIUM: Session tokens not rotated after privilege escalation
  LOW: CSRF token missing on the logout endpoint

Want me to create fix tasks for these?
```

### Project Management

```
You: "What's the status of our Loom project?"

Claude: Here's where Loom stands:
  - 12 tasks done, 3 in progress, 2 blocked
  - Last session: implemented world generation system
  - Open question: how to handle offline save conflicts
  - Next steps: integrate quest engine with dialogue system
```

```
You: "What tasks are outstanding across all projects?"

Claude: [queries database, groups by project and priority]

  TorqueDesk (3 high priority):
    - Implement invoice re-open workflow
    - Add tech time tracking with punch in/out
    ...
```

You never look up a task ID. You never write SQL. You just ask.

### Persistent Memory

The system doesn't just do work — it remembers what happened and gets smarter over time.

```
You: "What lessons have we learned from failed security reviews on Loom?"

Claude: 3 lessons extracted from Loom security failures:

  1. Always validate JWT expiry server-side — client-side checks
     were bypassed in task #87 (injected into 12 future runs)
  2. Rate-limit auth endpoints before deploying — missed in tasks
     #91 and #103 (injected into 8 future runs)
  3. Sanitize user input in GraphQL resolvers — XSS found twice
     in the quest engine (injected into 5 future runs)

These lessons are automatically injected into future developer
and security reviewer prompts for Loom.
```

Every agent run, every test result, every lesson learned — stored in a 30-table SQLite database that agents query automatically. The system builds institutional knowledge about *your* projects.

---

## How It Works

```
You describe what you want
        |
        v
Claude breaks it into tasks
        |
        v
Claude dispatches specialized agents
        |
        v
Agents do the work (dev → test → security review)
        |
        v
Claude reports results and handles failures
```

### The Agent Roster

Claude picks the right agent automatically, but you can request specific ones:

| Role | What It Does |
|------|-------------|
| **Developer** | Writes code. Your bread and butter. |
| **Tester** | Writes and runs tests. Validates everything. |
| **Debugger** | Investigates bugs, traces issues, finds root causes. |
| **Security Reviewer** | Deep audit with Trail of Bits tooling (Semgrep, CodeQL). |
| **Code Reviewer** | Quality, patterns, best practices. |
| **Planner** | Breaks complex features into task lists. |
| **Frontend Designer** | UI/UX focused development. |
| **Evaluator** | Assesses implementations against requirements. |
| **Integration Tester** | Tests how components work together. |

"Run a security review on the payment module" — Claude dispatches the right agent with the right tools. You don't need to know which CLI flags to pass.

---

## Quick Start

**1. Run the guided installer:**
```bash
python pombal_setup.py
```

**2. The installer handles everything:** prerequisites, database, config, MCP integration. Just answer the prompts.

**3. Open Claude Code in your Project Pombal directory and start talking:**
```bash
cd ~/Project Pombal
claude
```

> "Show me all active projects"
>
> "Add a new project called MyApp with the code at ~/myapp"
>
> "Create a task for MyApp: set up the database schema"
>
> "Work on that task"

That's it. Claude knows the database, the orchestrator, and the config. Just tell it what you want.

---

## Tips for Best Results

**Be specific about what you want.**
- Good: "Add Google OAuth login with session cookies"
- Bad: "Add auth"

**Mention the tech stack when it matters.**
- Good: "Add rate limiting using Express middleware and Redis"
- Bad: "Add rate limiting"

**Let Claude plan big features.** For anything with 2+ moving parts, describe the end goal. Claude will break it down better than manual task creation because it can read your codebase and understand what already exists.

**Ask for parallel work.** Mention independent tasks together. Claude will dispatch them simultaneously.

**Give context when you have it.**
- "The 500 error started after yesterday's deploy"
- "It only happens when the user has a discount code"
- "Match the existing Card pattern in the design system"

---

## When to Skip the Agents

Not everything needs a full agent pipeline. Use Claude Code directly for:

- **Quick fixes** — Typos, one-liner changes. Just ask Claude to edit the file.
- **Research** — "How does the auth flow work?" Claude reads the code and answers.
- **Config changes** — Editing a config file doesn't need a dev-test loop.
- **Database queries** — Claude queries the DB directly via MCP.

Rule of thumb: if the change is under 5 lines and doesn't need testing, just do it directly.

---

## Features

- **Conversational interface** — Talk to Claude in plain English. No commands, no task IDs.
- **Autonomous dev-test loops** — Developer and tester iterate until the code works
- **Per-role agent skills** — Specialized skills per role: codebase navigation, error recovery, systematic debugging, architecture review
- **Git worktree isolation** — Parallel tasks run in isolated branches, merged on success, preserved on failure
- **Post-task quality scoring** — 5-dimension quality scorer with role-specific weights
- **Persistent project memory** — Agents learn from past successes and failures on *your* projects
- **Self-improving prompts** — ForgeSmith evolves agent behavior based on real outcomes
- **Failure classification** — Structured taxonomy for targeted improvements
- **Change-impact analysis** — Blast-radius assessment before applying prompt mutations
- **Security pipeline** — Trail of Bits tooling with auto-dispatch after dev-test
- **Inter-agent messaging** — Agents share findings, blockers, and context across cycles
- **Loop detection** — Catches stuck agents and terminates gracefully
- **Multi-model support** — Claude Code, Ollama (local models), configurable per role
- **Database migrations** — Schema evolves safely with automatic backups, zero data loss
- **Zero pip dependencies** — Pure Python stdlib. Requires Python 3.10+, Claude Code CLI, git, and uvx

---

## Installation

### Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | [python.org](https://python.org) |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` |
| git | [git-scm.com](https://git-scm.com) |
| uvx / uv | [docs.astral.sh/uv](https://docs.astral.sh/uv) |

### Guided Setup (Recommended)
```bash
git clone <repo-url> project-pombal
cd project-pombal
python pombal_setup.py
```

The installer walks you through: prerequisites check, database creation, config generation, MCP integration, and optional components (Sentinel monitoring, ForgeBot Discord bot).

### Manual Setup

See [ORCHESTRATOR.md](ORCHESTRATOR.md) for manual installation and CLI usage.

---

## Coordinator vs Manual Mode

| | Coordinator Mode | Manual Mode |
|---|---|---|
| **Task creation** | Claude does it | You write SQL or CLI commands |
| **Dispatching** | Claude does it | You run orchestrator commands |
| **Monitoring** | Claude does it | You check logs |
| **Error recovery** | Claude does it | You diagnose and retry |
| **Parallelism** | Claude figures out dependencies | You manage dependencies |
| **Role selection** | Claude picks the right agent | You specify the role |

Manual mode works and is fully documented in [ORCHESTRATOR.md](ORCHESTRATOR.md). But for most workflows, Coordinator Mode is faster, easier, and produces better results.

---

## Related Documentation

| Doc | What's in it |
|-----|-------------|
| [Capabilities](CAPABILITIES.md) | Architecture deep dive, ForgeSmith, security pipeline, benchmarks |
| [Orchestrator](ORCHESTRATOR.md) | CLI commands, flags, manual setup, advanced usage |
| [Architecture](ARCHITECTURE.md) | System design, data flow, key decisions |
| [Quick Start](QUICKSTART.md) | Step-by-step getting started guide |
| [User Guide](USER_GUIDE.md) | Comprehensive usage documentation |
| [Custom Agents](CUSTOM_AGENTS.md) | How to create your own agent roles |
| [API Reference](API.md) | Module-level API documentation |

---

<p align="center">
  <strong>Built by Forgeborn</strong><br>
  Vibe coded with Claude<br>
  <em>&copy; 2026 Forgeborn</em>
</p>
