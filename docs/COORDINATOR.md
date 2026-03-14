# Coordinator Mode — The Recommended Way to Use EQUIPA

> Stop memorizing CLI commands. Just talk.

Coordinator Mode turns Claude Code into a natural language dispatcher for EQUIPA. You describe what you want in plain English. Claude handles the rest — planning, task creation, agent dispatch, monitoring, and error recovery.

This is the most powerful way to use EQUIPA. Period.

---

## What Is Coordinator Mode?

Traditional workflow: you read docs, write CLI commands, manage tasks manually, check logs yourself.

Coordinator Mode: you talk to Claude Code like a project manager. Claude does the busywork.

- You say what you want in plain English
- Claude creates tasks in the EQUIPA database
- Claude dispatches the orchestrator to assign agents
- Agents (Developer, Tester, Security Reviewer, etc.) do the actual implementation
- Claude monitors progress and reports back
- If something fails, Claude creates follow-up tasks and re-dispatches

Why is this better? Because Claude can:
- Plan multi-step workflows and identify dependencies
- Split work into parallel tasks automatically
- Pick the right agent roles for each task
- Handle error recovery without you babysitting
- Maintain context across the entire session

---

## How It Works

The flow is simple:

```
You describe what you want
        |
        v
Claude breaks it into tasks
        |
        v
Claude writes tasks to the EQUIPA database
        |
        v
Claude dispatches the orchestrator
        |
        v
Agents pick up tasks and do the work
        |
        v
Claude monitors, reports, and handles failures
```

### Step by step:

1. **You talk.** "Build me a login page with OAuth." That's it.
2. **Claude plans.** It analyzes your request, checks your project structure, and breaks the work into discrete tasks.
3. **Claude creates tasks.** Each task gets written to the EQUIPA SQLite database with a clear title, description, and assigned role.
4. **Claude dispatches.** It runs the orchestrator, pointing it at the new tasks. Independent tasks run in parallel.
5. **Agents work.** Developer writes code. Tester validates. Security Reviewer checks for vulnerabilities. Each agent operates in its own Claude Code session.
6. **Claude reports.** As tasks complete, Claude tells you what happened. Files changed, tests passed, issues found.
7. **Claude recovers.** If an agent fails or a test breaks, Claude creates a follow-up task and re-dispatches. You don't have to intervene.

---

## Setup

1. Install EQUIPA normally via `equipa_setup.py`
2. The installer generates a `.claude/CLAUDE.md` file in your project directory — this teaches Claude Code how to be the coordinator
3. Open Claude Code in your project directory
4. Start talking

That's it. No extra config. No flags. The generated CLAUDE.md gives Claude all the context it needs about your project structure, the database location, and how to dispatch agents.

---

## Example Conversations

### Example 1: Simple Feature

```
You: "Add a dark mode toggle to the settings page"

Claude: I'll create a task for that. Let me check your project structure first...

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

### Example 2: Multi-Task Feature

```
You: "Build me user authentication with Google OAuth"

Claude: That's a multi-step feature. Let me plan it out:

  1. Set up OAuth provider configuration
  2. Create login/callback routes
  3. Add session management middleware
  4. Build the login UI component
  5. Write integration tests

[Creates 5 tasks in DB with proper descriptions]
[Dispatches orchestrator with --tasks 100-104]
[Tasks 1-3 run in parallel — no dependencies between them]
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

Claude identified the dependencies automatically. Tasks 1-3 ran in parallel. Total wall-clock time is way less than running them sequentially.

### Example 3: Bug Fix

```
You: "Users are getting 500 errors on the checkout page"

Claude: Let me investigate. I'll start with a debugger task...

[Creates task: "Debug 500 errors on checkout page"]
[Dispatches debugger agent]
[Debugger analyzes error logs, traces the stack, finds the root cause]

Claude: Found it. Null pointer in calculateTotal() when the cart has a
discounted item with no original price. Creating a fix task...

[Creates task: "Fix null pointer in calculateTotal for discounted items"]
[Dispatches developer agent]
[Developer fixes the bug, adds edge case tests]
[Tester validates the fix]

Claude: Fixed. The issue was in src/utils/cart.ts line 47.
Added null check for originalPrice. 4 new test cases covering
discount edge cases. All passing.
```

Debug, fix, and test — all from one sentence.

### Example 4: Code Review

```
You: "Review the auth module for security issues"

Claude: I'll dispatch a security reviewer.

[Creates task: "Security review of auth module"]
[Dispatches security-reviewer agent]

Claude: Security review complete. 2 findings:

  MEDIUM: Session tokens not rotated after privilege escalation
  LOW: CSRF token missing on the logout endpoint

Want me to create fix tasks for these?
```

### Example 5: Parallel Work

```
You: "Add input validation to all API endpoints and write API docs"

Claude: Those are independent — I'll run them in parallel.

[Creates task 1: "Add input validation to API endpoints"]
[Creates task 2: "Generate API documentation"]
[Dispatches both simultaneously]

Claude: Both done.
- Validation added to 12 endpoints using zod schemas
- API docs generated at docs/api.md
```

---

## What Claude Runs Behind the Scenes

You don't need to know these commands — Claude handles them. But if you're curious:

### Creating tasks

```sql
INSERT INTO tasks (project_id, title, description, status, assigned_role)
VALUES (?, 'Add dark mode toggle', 'Implement...', 'todo', 'developer');
```

### Dispatching a single task

```bash
python3 forge_orchestrator.py --task 105 --dev-test -y
```

### Dispatching multiple tasks in parallel

```bash
python3 forge_orchestrator.py --tasks 100-104 --dev-test -y
```

### Checking task status

```sql
SELECT id, title, status, assigned_role
FROM tasks
WHERE project_id = ? AND status != 'done'
ORDER BY id;
```

### Reading agent output

Claude checks the orchestrator logs and task results in the database to report back to you.

---

## Available Agent Roles

Claude picks the right role automatically, but you can request specific ones:

| Role | What It Does |
|------|-------------|
| `developer` | Writes code. Your bread and butter. |
| `tester` | Writes and runs tests. |
| `debugger` | Investigates bugs, traces issues, finds root causes. |
| `code-reviewer` | Reviews code for quality, patterns, best practices. |
| `security-reviewer` | Checks for vulnerabilities, auth issues, injection risks. |
| `planner` | Breaks down complex features into task lists. |
| `evaluator` | Assesses implementations against requirements. |
| `frontend-designer` | UI/UX focused development. |
| `integration-tester` | Tests how components work together. |

You can ask for specific roles:

- "Run a security review on the payment module"
- "Have a code reviewer look at my PR"
- "Get a planner to break down the notification system"

---

## Tips for Getting the Best Results

**Be specific about what you want.**
- Good: "Add Google OAuth login with session cookies"
- Bad: "Add auth"

**Mention the tech stack when it matters.**
- Good: "Add rate limiting using Express middleware and Redis"
- Bad: "Add rate limiting"

**Let Claude plan big features.**
For anything with more than 2-3 moving parts, just describe the end goal. Claude will break it down better than manual task creation because it can read your codebase and understand what already exists.

**Ask for parallel work.**
If you have independent tasks, mention them together. Claude will dispatch them simultaneously.

**Use the right role for the job.**
- Bugs? Debugger first, then developer.
- Security concerns? Security reviewer.
- Quality check? Code reviewer.
- Not sure how to approach something? Planner.

**Give context when you have it.**
- "The 500 error started after yesterday's deploy"
- "It only happens when the user has a discount code"
- "The component should match the existing Card pattern in the design system"

---

## When NOT to Use Coordinator Mode

Coordinator Mode is powerful, but it's overkill for some things. Use Claude Code directly for:

- **Quick one-line fixes.** Just ask Claude to edit the file. No need to spin up an agent for a typo.
- **Research and exploration.** "How does the auth flow work?" — Claude can search the codebase and answer directly.
- **Configuration changes.** Editing a config file doesn't need an agent pipeline.
- **Database queries.** Claude can query your database directly.
- **Reading/understanding code.** Claude can read and explain code without dispatching anyone.

Rule of thumb: if the change is under 5 lines and doesn't need testing, just do it directly.

---

## Troubleshooting

**Agents keep failing on a task**
- The task description is probably too vague. Ask Claude to rewrite it with more detail.
- Check if the task requires context the agent doesn't have (API keys, external service access).

**Tests won't pass**
- Ask Claude to dispatch a debugger agent to analyze the test failures.
- "Why are the tests failing on task 105? Run a debugger."

**Agent produced wrong output**
- Ask Claude to review what the agent did and create a correction task.
- "Task 103 used the wrong database schema. Fix it."

**Want to retry a task**
- "Re-run task 105 with a fresh developer agent"
- Claude will reset the task status and re-dispatch.

**Orchestrator won't start**
- Check that EQUIPA is installed correctly: `python3 forge_orchestrator.py --help`
- Verify the database path is correct in your project's CLAUDE.md.

**Tasks stuck in 'in_progress'**
- Ask Claude to check the orchestrator logs.
- "What happened to task 107? It's been running for a while."

---

## How This Compares to Manual Mode

| | Manual Mode | Coordinator Mode |
|---|---|---|
| **Task creation** | You write SQL or use CLI | Claude does it |
| **Dispatching** | You run orchestrator commands | Claude does it |
| **Monitoring** | You check logs | Claude does it |
| **Error recovery** | You diagnose and retry | Claude does it |
| **Parallelism** | You figure out dependencies | Claude does it |
| **Role selection** | You pick the agent role | Claude does it |

Manual mode still works. It's documented in the User Guide. But for most workflows, Coordinator Mode is faster, easier, and produces better results because Claude can see the full picture.

---

## Summary

1. Install EQUIPA
2. Open Claude Code in your project
3. Tell it what you want
4. Let it handle the rest

That's Coordinator Mode. The agents do the work. Claude does the coordination. You just talk.

---

*Built by Forgeborn. Vibe coded with Claude.*
*Copyright 2026 Forgeborn. All rights reserved.*
