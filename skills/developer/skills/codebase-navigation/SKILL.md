---
name: codebase-navigation
description: >
  Systematic approach to understanding unfamiliar codebases quickly and finding change targets
  without analysis paralysis. Use when starting work on a new codebase, when you can't find the
  right file to modify, when you've spent more than 5 turns reading without writing, or when
  a project's structure is unclear. Triggers: unfamiliar codebase, can't find file, where does
  this live, analysis paralysis, reading too much.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Codebase Navigation

## Core Principle

**Find the smallest set of files needed to complete your task, then stop reading.**
Every turn spent reading instead of writing is a turn wasted. The goal is surgical precision,
not comprehensive understanding.

## When to Use

- Starting work on a codebase you haven't seen before
- You can't find where a feature lives or where to make changes
- You've read more than 5 files without making any changes
- The project structure doesn't follow obvious conventions
- You need to understand data flow or call chains

## When NOT to Use

- You already know exactly which files to modify
- The task description includes specific file paths
- You've already started writing code (stay focused, don't re-navigate)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "I need to read the whole codebase first" | You never need the whole codebase. You need 3-8 files. | Use the 4-step navigation method below |
| "Let me read every file in this directory" | Directory listing gives structure; reading every file wastes turns | Glob for structure, Grep for specifics, Read only targets |
| "I should understand the full architecture" | You need to understand the part you're changing | Map only the relevant call chain |
| "This import chain goes deep, let me follow it" | Stop at 3 levels deep. Beyond that, trust the interface. | Read the function signature and docstring, not the implementation |
| "I'll read the tests to understand behavior" | Tests are supplementary, not primary. Read the source. | Read source first, tests only if behavior is genuinely ambiguous |

## The 4-Step Navigation Method

### Step 1: Entry Points (1 turn max)

Identify the project type and find entry points. Use ONE of these strategies:

**Web app/API:**
```
Glob: **/routes.*, **/urls.*, **/router.*, **/app.{ts,js,py}, **/main.{ts,js,py,go}
```

**CLI tool:**
```
Glob: **/main.*, **/cli.*, **/cmd/**/main.*, **/bin/**
```

**Library:**
```
Glob: **/index.{ts,js}, **/__init__.py, **/lib.rs, **/mod.go
```

**Unknown — use the universal approach:**
```
Read: package.json OR pyproject.toml OR go.mod OR Cargo.toml OR *.csproj
Then: Glob for the main source directory
```

### Step 2: Architecture Map (1-2 turns max)

From entry points, identify the layer structure. Don't read files yet — just list them:

```
Glob: src/**/*.{ts,py,go,rs} (or whatever the source dir is)
```

Classify directories into layers:
- **Handlers/Controllers** — HTTP endpoints, CLI commands, event handlers
- **Services/Business Logic** — Core domain logic
- **Data/Repository** — Database queries, external API calls
- **Models/Types** — Data structures, interfaces
- **Config** — Settings, environment, constants
- **Utils** — Shared helpers (usually NOT what you need)

### Step 3: Find Change Targets (1-2 turns max)

Now use Grep to find exactly where your change belongs:

```
Grep for: function names, error messages, UI text, API endpoint paths,
          database table names, or any unique string from the task description
```

**Priority order for finding the right file:**
1. Grep for keywords from the task description
2. Grep for related function/class names
3. Grep for error messages if fixing a bug
4. Grep for test files that exercise the feature
5. Look at recent git commits touching related areas

### Step 4: Read Only What You Need (1-2 turns max)

Read ONLY the files you identified as change targets. For each file:
- Read the full file if it's under 200 lines
- Read the relevant function/class if the file is longer
- Note imports only if you need to modify the interface

**STOP READING. START WRITING.**

## Anti-Patterns (Red Flags)

| Red Flag | What's Happening | Fix |
|----------|-----------------|-----|
| 10+ files read, 0 files changed | Analysis paralysis | Pick the most likely target, start writing, iterate |
| Following import chains 4+ levels deep | Rabbit hole | Trust the interface at level 3, stop diving |
| Reading test files before source files | Backwards discovery | Read source first, tests only for ambiguous behavior |
| Reading README/docs instead of code | Procrastination | Code is the source of truth. Read it. |
| Re-reading files you already read | Lost context | Take notes in your working memory, don't re-read |

## Quality Checklist

Before moving to implementation:
- [ ] I know which 1-3 files need changes
- [ ] I understand the function signatures I'll call or modify
- [ ] I've spent fewer than 6 turns on navigation
- [ ] I have NOT read more than 8 files total
- [ ] I can describe in one sentence what each target file does

## References

- See `references/framework-structures.md` for common project layouts
- See `references/grep-patterns.md` for effective search patterns
