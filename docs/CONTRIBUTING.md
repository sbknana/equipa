# Contributing to EQUIPA

## Table of Contents

- [Contributing to EQUIPA](#contributing-to-equipa)
  - [Welcome](#welcome)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Clone and go](#clone-and-go)
    - [Database setup](#database-setup)
    - [Running migrations](#running-migrations)
  - [Code Style](#code-style)
    - [The basics](#the-basics)
    - [Formatting](#formatting)
    - [File organization](#file-organization)
    - [Security](#security)
  - [Making Changes](#making-changes)
    - [Branch naming](#branch-naming)
    - [Commit messages](#commit-messages)
    - [Before you push](#before-you-push)
  - [Testing](#testing)
    - [Run all tests](#run-all-tests)
    - [Run a specific test file](#run-a-specific-test-file)
    - [Run a specific test](#run-a-specific-test)
    - [Some tests can also run standalone](#some-tests-can-also-run-standalone)
    - [What to test](#what-to-test)
    - [Test database handling](#test-database-handling)
  - [Pull Request Process](#pull-request-process)
    - [Before opening a PR](#before-opening-a-pr)
    - [PR description](#pr-description)
    - [Review expectations](#review-expectations)
    - [Areas that need extra review scrutiny](#areas-that-need-extra-review-scrutiny)
  - [Issue Reporting](#issue-reporting)
    - [Bugs](#bugs)
    - [Feature requests](#feature-requests)
    - [Things to know before filing](#things-to-know-before-filing)
  - [Code of Conduct](#code-of-conduct)
  - [Quick Reference](#quick-reference)
  - [Related Documentation](#related-documentation)

## Welcome

Hey, thanks for being here. EQUIPA is a multi-agent AI orchestrator — pure Python, zero dependencies, SQLite-backed. It coordinates AI agents to write, test, review, and secure code.

The primary way people use EQUIPA is conversational: you talk to Claude, Claude runs EQUIPA behind the scenes. But the internals are what you'd be contributing to, and there's plenty to improve.

We're happy to have you. Whether you're fixing a typo, adding a test, or tackling something gnarly in the self-improvement loop — it all counts.

---

## Development Setup

EQUIPA has zero pip dependencies. It's pure Python stdlib. So setup is.. refreshingly boring.

### Prerequisites

- Python 3.10+
- Git
- SQLite3 (comes with Python)
- Claude CLI (for running agents, not required for contributing code)

### Clone and go

```bash
git clone https://github.com/sbknana/equipa.git
cd equipa
```

That's it. No `pip install`, no virtual environment drama. If you want one anyway (fair), go ahead:

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
```

### Database setup

EQUIPA uses SQLite with a 30+ table schema. The setup wizard handles this:

```bash
python equipa_setup.py
```

This walks you through database creation, config generation, and verification. If you just want to run tests, most of them create temporary databases on their own — you don't need a full setup.

### Running migrations

If you're working with an existing database:

```bash
python db_migrate.py
```

This handles versioned migrations (v0 through v5 currently), creates backups before migrating, and logs what it did.

---

## Code Style

### The basics

- **Pure Python stdlib.** This is a hard rule. No pip dependencies. If you need something, write it or find it in the standard library.
- **Type hints** are welcome but not enforced everywhere yet. New code should include them where it makes sense.
- **Docstrings** for public functions. Don't write a novel — just say what it does and why.

### Formatting

We don't currently enforce a specific formatter across the project. That said:

- 4-space indentation (it's Python, so..)
- Keep lines reasonable — under 100 characters when you can
- Use snake_case for functions and variables
- Use PascalCase for classes
- Constants in UPPER_SNAKE_CASE

### File organization

- Core orchestration code lives in `equipa/`
- Self-improvement systems (ForgeSmith, GEPA, SIMBA) are top-level scripts and `scripts/`
- Agent role prompts are in `prompts/`
- Language-specific prompts are in `prompts/languages/`
- Skills (like SARIF parsing) are in `skills/`
- Tests go in `tests/`

### Security

EQUIPA runs bash commands from AI agents, so security matters a lot. If you're touching `bash_security.py`, `security.py`, or anything in the command execution path — be extra careful. Look at the existing test coverage in `test_bash_security.py` (it's extensive) and make sure you're not opening holes.

---

## Making Changes

### Branch naming

Use descriptive branch names. The pattern doesn't need to be rigid, but something like:

```
fix/loop-detection-false-positive
feat/new-agent-role
test/cost-routing-edge-cases
docs/contributing-guide
```

### Commit messages

Write commit messages that explain *what* changed and *why*. The format:

```
Short summary (50 chars or less)

Longer explanation if needed. What was the problem?
What does this change? Why this approach?
```

Don't stress about conventional commits or any specific format. Just be clear.

### Before you push

1. Run the tests (see below)
2. Make sure you haven't introduced any pip dependencies
3. If you changed prompts, run `python -c "from equipa.security import verify_skill_integrity; print(verify_skill_integrity())"` to check skill integrity
4. If you changed database schema, add a migration in `db_migrate.py`

---

## Testing

EQUIPA has 334+ passing tests. They matter. Run them before submitting anything.

### Run all tests

```bash
python -m pytest tests/
```

### Run a specific test file

```bash
python -m pytest tests/test_early_termination.py
python -m pytest tests/test_bash_security.py
python -m pytest tests/test_loop_detection.py
```

### Run a specific test

```bash
python -m pytest tests/test_cost_routing.py::test_circuit_breaker_degrades_after_5_failures
```

### Some tests can also run standalone

Several test files have their own `main()` or `run_all_tests()`:

```bash
python tests/test_early_termination.py
python tests/test_lesson_sanitizer.py
python tests/test_agent_messages.py
python tests/test_episode_injection.py
python tests/test_lessons_injection.py
```

### What to test

- **New features:** Write tests. No exceptions.
- **Bug fixes:** Write a test that reproduces the bug first, then fix it. The test should fail without your fix and pass with it.
- **Prompt changes:** These are harder to test automatically, but the task type routing tests (`test_task_type_routing.py`) and prompt cache split tests (`test_prompt_cache_split.py`) verify structural integrity.
- **Security changes:** The bash security tests are *thorough* — 100+ test cases covering obfuscation, command substitution, Unicode tricks, zsh exploits, and more. If you're touching security code, add tests for every edge case you can think of, then think of three more.

### Test database handling

Most tests create temporary databases using `tmp_path` or `monkeypatch`. Don't rely on a real database being present. Don't leave test databases lying around.

---

## Pull Request Process

### Before opening a PR

- [ ] All tests pass (`python -m pytest tests/`)
- [ ] No new pip dependencies introduced
- [ ] New code has tests
- [ ] Commit messages are clear
- [ ] You've tested your change manually if it affects agent behavior

### PR description

Tell us:

1. **What** does this change?
2. **Why** does it matter?
3. **How** did you test it?
4. **Anything weird** the reviewer should know about?

If your PR relates to an issue, reference it. If it doesn't, that's fine too.

### Review expectations

- Someone will review your PR. We'll try to be quick but this is an open-source project, so patience appreciated.
- Reviews focus on correctness, security implications, and whether the change makes EQUIPA better without making it more complicated than it needs to be.
- Don't take feedback personally. We're all trying to make the thing better.
- Small, focused PRs get reviewed faster than massive ones. If your change is big, consider splitting it.

### Areas that need extra review scrutiny

Some parts of the codebase are more sensitive than others:

- **`equipa/bash_security.py`** — This is the barrier between AI agents and your filesystem. Changes here get extra scrutiny.
- **`equipa/agent_runner.py`** — The core agent execution loop. Subtle bugs here affect everything.
- **`forgesmith.py`, `forgesmith_gepa.py`, `forgesmith_simba.py`** — The self-improvement loop. Changes can have compounding effects over time.
- **Database migrations** — These run on real user databases. They need to be backward-compatible and tested against every prior version.

---

## Issue Reporting

### Bugs

Open an issue with:

- **What happened** — the actual behavior
- **What you expected** — the behavior you wanted
- **How to reproduce** — steps, commands, config if relevant
- **Environment** — Python version, OS, any relevant config
- **Logs** — if an agent misbehaved, include the relevant log output

### Feature requests

We're open to ideas. Tell us:

- What problem does this solve?
- How do you imagine it working?
- Are you willing to implement it? (Not required, just helpful to know)

### Things to know before filing

EQUIPA has some known limitations that aren't bugs — they're just.. where things are right now:

- **Agents get stuck on complex tasks.** Analysis paralysis is real. If an agent burns turns reading files without acting, that's a known issue with the early termination heuristics. Currently agents get killed at 10 turns of reading-only behavior, which is sometimes too aggressive for legitimately complex tasks.
- **Git worktree merges need manual intervention sometimes.** The worktree isolation is still being refined. Don't assume merge conflicts will resolve themselves.
- **Self-improvement takes time.** ForgeSmith + GEPA + SIMBA need 20-30 completed tasks before patterns emerge and improvements kick in. If you just set it up, it won't seem like it's doing much yet.
- **The Tester role needs your project to have a working test suite.** If your project doesn't have tests, the Tester agent can't do much.
- **Cost controls kill agents.** That's by design, but sometimes legitimate expensive tasks get terminated. The cost breaker scales with complexity, but it's not perfect.

If your issue is one of these — it's probably not a bug, but we're still interested in hearing about specific cases that might help us improve the heuristics.

---

## Code of Conduct

Be kind. Be respectful. Assume good intent.

We're building something together. Disagreements about code are fine — make them about the code, not the person. Help newcomers. Answer questions patiently. Remember that everyone was new once.

Harassment, discrimination, and general jerkiness won't be tolerated. If someone makes you uncomfortable, reach out to the maintainers.

---

## Quick Reference

| What | Command |
|------|---------|
| Run all tests | `python -m pytest tests/` |
| Run setup wizard | `python equipa_setup.py` |
| Run migrations | `python db_migrate.py` |
| Benchmark migrations | `python tools/benchmark_migrations.py` |
| Performance dashboard | `python tools/forge_dashboard.py` |
| Nightly review | `python scripts/nightly_review.py` |
| Run ForgeSmith | `python forgesmith.py` |
| Run SIMBA rules | `python forgesmith_simba.py` |
| Run GEPA prompts | `python forgesmith_gepa.py` |

Thanks for contributing. Seriously. Every improvement matters.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
