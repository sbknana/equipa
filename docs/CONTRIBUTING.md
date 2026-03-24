# Contributing to EQUIPA

## Table of Contents

- [Contributing to EQUIPA](#contributing-to-equipa)
  - [Welcome](#welcome)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Getting started](#getting-started)
- [Clone the repo](#clone-the-repo)
- [There's no requirements.txt. There's no venv dance.](#theres-no-requirementstxt-theres-no-venv-dance)
- [It's stdlib all the way down. Just run it.](#its-stdlib-all-the-way-down-just-run-it)
- [Set up the database and config](#set-up-the-database-and-config)
- [Verify everything works](#verify-everything-works)
    - [Running the tests](#running-the-tests)
- [All tests](#all-tests)
- [A specific test file](#a-specific-test-file)
- [Some test files also have their own main() — you can run them directly](#some-test-files-also-have-their-own-main-you-can-run-them-directly)
    - [Database migrations](#database-migrations)
  - [Code Style](#code-style)
    - [The basics](#the-basics)
    - [Naming conventions](#naming-conventions)
    - [File organization](#file-organization)
    - [Things we care about](#things-we-care-about)
  - [Making Changes](#making-changes)
    - [Branch naming](#branch-naming)
    - [Commit messages](#commit-messages)
- [Good](#good)
- [Less good](#less-good)
    - [Before you push](#before-you-push)
- [Run the tests. All of them.](#run-the-tests-all-of-them)
- [If you changed forgesmith or SIMBA:](#if-you-changed-forgesmith-or-simba)
- [If you changed early termination or loop detection:](#if-you-changed-early-termination-or-loop-detection)
- [If you touched the lesson system:](#if-you-touched-the-lesson-system)
  - [Testing](#testing)
    - [What to test](#what-to-test)
    - [How tests are organized](#how-tests-are-organized)
    - [Tests that need a database](#tests-that-need-a-database)
    - [What NOT to test](#what-not-to-test)
  - [Pull Request Process](#pull-request-process)
    - [Opening a PR](#opening-a-pr)
    - [Review expectations](#review-expectations)
    - [What will slow down your PR](#what-will-slow-down-your-pr)
    - [What will speed up your PR](#what-will-speed-up-your-pr)
  - [Issue Reporting](#issue-reporting)
    - [Bugs](#bugs)
  - [Bug: Loop detector false positive on 3-way tool rotation](#bug-loop-detector-false-positive-on-3-way-tool-rotation)
    - [What happened](#what-happened)
    - [Expected](#expected)
    - [Reproduction](#reproduction)
    - [Environment](#environment)
    - [Feature requests](#feature-requests)
  - [Known Limitations (a.k.a. "Here Be Dragons")](#known-limitations-aka-here-be-dragons)
  - [Code of Conduct](#code-of-conduct)
  - [Questions?](#questions)
  - [Related Documentation](#related-documentation)

## Welcome

Hey, thanks for being here. EQUIPA is a multi-agent AI orchestration platform built in pure Python with zero dependencies. It coordinates AI agents to write code, test it, review it, and iterate until things actually work.

We're glad you want to help. Whether it's fixing a bug, adding a feature, improving docs, or just pointing out something dumb we did — all of it matters.

This guide will help you get set up and explain how we work together.

---

## Development Setup

EQUIPA has zero pip dependencies. It's pure Python stdlib. That makes setup pretty straightforward.

### Prerequisites

- Python 3.10+ (3.11+ recommended)
- SQLite3 (comes with Python)
- Git
- An Anthropic API key (for agent dispatch) or a running Ollama instance (for local models)

### Getting started

```bash
# Clone the repo
git clone https://github.com/your-org/equipa.git
cd equipa

# There's no requirements.txt. There's no venv dance.
# It's stdlib all the way down. Just run it.

# Set up the database and config
python equipa_setup.py

# Verify everything works
python -m pytest tests/ -v
```

The setup wizard (`equipa_setup.py`) walks you through database creation, config generation, and optional components like the sentinel and forgebot. It's interactive — just follow the prompts.

### Running the tests

```bash
# All tests
python -m pytest tests/ -v

# A specific test file
python -m pytest tests/test_early_termination.py -v

# Some test files also have their own main() — you can run them directly
python tests/test_loop_detection.py
python tests/test_lesson_sanitizer.py
python tests/test_agent_messages.py
```

There are 334+ tests. They should all pass. If they don't on a fresh clone, that's a bug — please file an issue.

### Database migrations

If you're working with an existing database from a previous version:

```bash
python db_migrate.py
```

It auto-detects the version and applies migrations in order. It also creates a backup before touching anything.

---

## Code Style

### The basics

- **Pure Python stdlib.** No external dependencies. This is the single most important rule. If you need `requests`, use `urllib`. If you need `pyyaml`, use `json`. If you genuinely need something that stdlib can't do.. let's talk about it in an issue first.
- **Python 3.10+** — f-strings, type hints where they help readability, walrus operator if it makes things cleaner.
- **No linter is enforced in CI yet**, but we follow PEP 8 loosely. Don't stress about line length if breaking the line makes it harder to read.

### Naming conventions

- Functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Files: `snake_case.py`
- Test files: `test_<module_name>.py`
- Test functions: `test_<what_it_tests>()`

### File organization

```
equipa/           # Core package — orchestration, dispatch, parsing, CLI
tests/            # All tests live here
tools/            # Standalone utilities (dashboard, arena, benchmarks)
skills/           # Agent skill definitions and resources
forgesmith*.py    # Self-improvement subsystem (ForgeSmith, GEPA, SIMBA)
```

### Things we care about

- **Functions should do one thing.** If your function is 80+ lines, it probably wants to be two functions.
- **Error handling matters.** Agents fail. Network calls fail. Files go missing. Handle it.
- **Log what matters.** Use the `log()` function. Don't print to stdout in library code.
- **SQLite is the database.** Everything goes through `get_db()` or `get_db_connection()`. Don't open your own connections.

---

## Making Changes

### Branch naming

We're not strict about this, but a pattern helps:

```
fix/loop-detection-false-positive
feat/ollama-timeout-config
docs/contributing-guide
test/simba-rule-validation
```

Prefixes: `fix/`, `feat/`, `docs/`, `test/`, `refactor/`, `chore/`

### Commit messages

Write them like you'd explain the change to a teammate:

```
# Good
fix: loop detector now resets counter when files actually change
feat: add cost breaker config to dispatch_config.json
test: cover alternating tool pattern detection

# Less good
updated stuff
fixed bug
WIP
```

Keep the first line under 72 characters. Add a body if the "why" isn't obvious from the "what."

### Before you push

```bash
# Run the tests. All of them.
python -m pytest tests/ -v

# If you changed forgesmith or SIMBA:
python tests/test_forgesmith_simba.py

# If you changed early termination or loop detection:
python tests/test_early_termination.py
python tests/test_loop_detection.py

# If you touched the lesson system:
python tests/test_lesson_sanitizer.py
python tests/test_lessons_injection.py
```

Don't push with failing tests. If you're adding a new feature, add tests for it.

---

## Testing

### What to test

- **New functions** — if you wrote it, test it
- **Bug fixes** — write a test that would have caught the bug, then fix the bug
- **Edge cases** — empty inputs, None values, malformed data. Agents produce weird output. Plan for it.
- **Security-sensitive code** — the lesson sanitizer exists because agents can try to inject things into their own prompts. If you're touching anything that processes agent output, test for injection.

### How tests are organized

Most test files follow a simple pattern:

```python
def test_thing_does_expected_behavior():
    result = thing(input)
    assert result == expected

def main():
    test_thing_does_expected_behavior()
    print("All tests passed")

if __name__ == "__main__":
    main()
```

Some use pytest fixtures (see `tests/conftest.py`), some use unittest-style classes, some are standalone scripts with their own `main()`. We're not dogmatic about it. Pick whatever fits.

### Tests that need a database

Several tests create temporary SQLite databases. Look at how `test_agent_messages.py` or `test_forgesmith_simba.py` handle setup/teardown. The pattern is: create a temp db, do your thing, clean up.

### What NOT to test

- Don't mock everything. If you can test it with a real temp database, do that.
- Don't test Python builtins. `assert 1 + 1 == 2` doesn't help anyone.
- Don't write tests that depend on network calls or API keys. Those belong in integration tests, not the main suite.

---

## Pull Request Process

### Opening a PR

1. **Describe what changed and why.** Not just "fixed bug" — what was the bug? How does this fix it?
2. **Link to the issue** if there is one.
3. **List what you tested.** "Ran full test suite, all 334+ pass" is great. "I think it works" is less great.
4. **Note any areas you're unsure about.** We'd rather know upfront than discover it in review.

### Review expectations

- Someone will review your PR. We try to be quick about it, but we're not a huge team.
- Reviews focus on correctness, clarity, and whether it fits the project's patterns. We're not going to nitpick semicolons.
- If changes are requested, don't take it personally. We're all trying to make the thing better.
- One approval is enough to merge for most changes. Schema changes or ForgeSmith modifications might need more eyes.

### What will slow down your PR

- No tests for new functionality
- Breaking existing tests
- Adding external dependencies
- Large PRs that change 15 things at once — break it up if you can
- Changes to the database schema without a migration in `db_migrate.py`

### What will speed up your PR

- Small, focused changes
- Good commit messages
- Tests that actually cover the change
- A clear description of what and why

---

## Issue Reporting

### Bugs

When filing a bug, include:

- **What happened** — the actual behavior
- **What you expected** — the behavior you wanted
- **How to reproduce it** — steps, commands, config
- **Python version** and OS
- **Relevant log output** — the `log()` function writes to stderr, grab that

A good bug report:

```
## Bug: Loop detector false positive on 3-way tool rotation

### What happened
Agent was terminated for "tool loop" but was actually cycling through
read_file → grep → bash in a legitimate investigation pattern.

### Expected
Should not trigger loop detection for 3+ distinct tools.

### Reproduction
1. Set up a task that requires reading multiple files
2. Agent naturally rotates between read_file, grep, and bash
3. After 6 cycles, loop detector kills it

### Environment
Python 3.11.5, macOS 14.2
```

### Feature requests

Tell us:

- **What you want** — be specific
- **Why you want it** — what problem does it solve?
- **How you'd use it** — a concrete example helps

We're honest about what we'll prioritize. If it aligns with the project's direction, great. If it's niche, you might need to build it yourself — and we'll help you do that.

---

## Known Limitations (a.k.a. "Here Be Dragons")

If you're going to contribute, you should know where the rough edges are:

- **Agents still get stuck on complex tasks.** Analysis paralysis is real — sometimes an agent will read files for 10 turns without making a change. The early termination system catches some of this, but not all.
- **Git worktree merges occasionally need manual intervention.** The isolation system works, but merge conflicts happen. Don't assume worktree operations are always clean.
- **Self-improvement needs 20-30 tasks to show results.** ForgeSmith, GEPA, and SIMBA learn from episodic memory, but the feedback loop takes time. If you're testing changes to the self-improvement pipeline, you need real data — a handful of test runs won't tell you much.
- **The Tester role depends on the project having a working test suite.** If there are no tests to run, the tester agent doesn't have much to work with. It won't magically create a test framework from scratch (though it tries, bless it).
- **Early termination kills agents at 10 turns of reading.** This is intentional — it prevents runaway costs. But some legitimately complex tasks need more exploration time. If you're working on the early termination logic, be aware of this tradeoff.
- **It's not magic.** Agents still fail, get stuck, and waste turns sometimes. The system is designed to recover from failures and learn from them over time, but it's a process, not a switch.

---

## Code of Conduct

Be kind. Be patient. Assume good intent.

We're building something together. Disagreements about code are fine — disagreements about people are not. Everyone here is learning, including the maintainers.

- No harassment, discrimination, or personal attacks
- Give constructive feedback, not dismissive criticism
- Help newcomers — we were all new once
- If someone's contribution isn't ready, explain why and help them get there

If you experience or witness behavior that violates this, reach out to the maintainers directly.

---

## Questions?

Open an issue tagged `question`, or just ask in your PR. There are no stupid questions — only stupid assumptions that never get checked.

Thanks for contributing. Seriously. This project is better because people like you show up.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
