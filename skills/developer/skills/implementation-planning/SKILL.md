---
name: implementation-planning
description: >
  Decompose complex tasks into small, verifiable steps with checkpoints. Use when a task
  involves multiple files, unclear requirements, architectural decisions, or when the task
  description is vague. Prevents the agent from attempting too much at once and losing track.
  Triggers: complex task, multi-file change, unclear requirements, where do I start, big feature.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Implementation Planning

## Core Principle

**Break every task into steps small enough that each one can be verified independently.**
A step that changes more than 3 files or takes more than 10 turns is too big. Split it.

## When to Use

- Task involves changes to 3+ files
- Task description is vague or open-ended ("improve the API", "add authentication")
- You're not sure where to start
- The task requires an architectural decision
- Previous attempt at this task failed or timed out

## When NOT to Use

- Task is a simple bug fix with a clear target file
- Task is a single-file change with clear requirements
- You've already planned and are mid-implementation (don't re-plan)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "I'll figure it out as I go" | Complex tasks without plans have 73% failure rate | Write a 3-5 step plan before touching code |
| "The plan is in my head" | Working memory degrades over turns. Write it down. | Document the plan in your output |
| "I'll do everything in one big change" | Big changes are hard to verify and debug | Break into steps, verify each |
| "I need to handle every edge case" | Ship the core first. Edge cases come second. | Implement the happy path, then harden |
| "Let me refactor this first" | Refactoring before the feature is scope creep | Make it work, then make it right |

## The Planning Method

### Step 1: Classify Task Complexity (1 turn)

| Complexity | Signals | Approach |
|-----------|---------|----------|
| **Simple** | 1-2 files, clear target, known pattern | Skip planning, just implement |
| **Medium** | 3-5 files, clear requirements, standard pattern | 3-step plan, verify after each step |
| **Complex** | 5+ files, unclear requirements, new pattern | 5+ step plan, checkpoint after each step |
| **Epic** | Cross-cutting concern, architectural change | Break into sub-tasks, do one at a time |

### Step 2: Write the Plan (1 turn)

For each step, write:
```
Step N: [One-sentence description]
  Files: [which files to create/modify]
  Verify: [how to confirm this step worked]
  Depends on: [which previous step, if any]
```

**Example:**
```
Step 1: Add the database migration for user_preferences table
  Files: migrations/20260305_user_preferences.sql
  Verify: Run migration, check table exists
  Depends on: nothing

Step 2: Create the UserPreferences model
  Files: models/user_preferences.py
  Verify: Import succeeds, types are correct
  Depends on: Step 1

Step 3: Add API endpoints for preferences CRUD
  Files: routes/preferences.py, routes/__init__.py
  Verify: curl each endpoint, check responses
  Depends on: Step 2
```

### Step 3: Execute One Step at a Time

For each step:
1. Implement the changes
2. Run the verification check
3. If it passes, move to the next step
4. If it fails, fix before moving on — don't accumulate broken state

### Step 4: Checkpoint Strategy

At these turn counts, stop and assess:

| Turn | Action |
|------|--------|
| **6** | You should have a plan and be implementing Step 1 |
| **15** | At least 1 step should be complete and verified |
| **25** | Core functionality should work (happy path) |
| **35** | All steps should be complete, working on edge cases |
| **40** | STOP. Output what you have. Do not start new steps. |

## Handling Vague Tasks

When the task description is unclear:

1. **Don't ask for clarification** — you're running autonomously
2. **Make the simplest reasonable assumption** and document it
3. **Implement the minimum viable version** that satisfies the task title
4. **Document your assumptions** in the DECISIONS section of your output

**Example:** "Add authentication" → Assume session-based auth with existing user model.
Document: "DECISIONS: Implemented session-based authentication. Assumed existing users table.
Alternative: JWT tokens would require additional infrastructure."

## Handling Dependencies

When your task depends on something that doesn't exist yet:

1. **Create a minimal stub** that satisfies the interface
2. **Document the stub** so the next task can replace it
3. **Don't build the dependency yourself** unless it's part of your task

## Anti-Patterns

| Red Flag | What's Happening | Fix |
|----------|-----------------|-----|
| No plan by turn 5 | Diving in without direction | Stop, write 3-5 steps, then implement |
| Step involves 5+ files | Step is too big | Split into 2-3 smaller steps |
| Changing the plan mid-step | Scope creep | Finish current step first, then revise plan |
| Implementing tests before code | Premature testing | Write code first, tests come with or after |
| Spending turns on code style | Premature optimization | Make it work first. Style is last. |

## Quality Checklist

Before moving to implementation:
- [ ] Plan has 3-7 concrete steps
- [ ] Each step has clear verification criteria
- [ ] No step touches more than 3 files
- [ ] Happy path is covered by step 3 at the latest
- [ ] Dependencies between steps are clear
- [ ] I've documented any assumptions about vague requirements

## References

- See `references/complexity-rubric.md` for task complexity assessment
- See `references/assumption-patterns.md` for handling ambiguous requirements
