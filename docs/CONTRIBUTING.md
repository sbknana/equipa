# Contributing to EQUIPA

## Table of Contents

- [Contributing to EQUIPA](#contributing-to-equipa)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Clone and Configure](#clone-and-configure)
    - [Verify Setup](#verify-setup)
  - [Code Style](#code-style)
    - [Linting](#linting)
    - [Type Hints](#type-hints)
    - [Async/Await](#asyncawait)
  - [Making Changes](#making-changes)
    - [Branch Naming](#branch-naming)
    - [Commit Messages](#commit-messages)
    - [Pull Request Process](#pull-request-process)
  - [Summary](#summary)
  - [Changes](#changes)
  - [Testing](#testing)
  - [Related Issues](#related-issues)
    - [Review Expectations](#review-expectations)
  - [Testing](#testing)
    - [Running Tests](#running-tests)
    - [What to Test](#what-to-test)
    - [Test Database](#test-database)
    - [Test Coverage](#test-coverage)
  - [Pull Request Template](#pull-request-template)
  - [What does this PR do?](#what-does-this-pr-do)
  - [Why is this needed?](#why-is-this-needed)
  - [How was it tested?](#how-was-it-tested)
  - [Breaking changes?](#breaking-changes)
  - [Related issues](#related-issues)
  - [Issue Reporting](#issue-reporting)
    - [Filing Bugs](#filing-bugs)
    - [Requesting Features](#requesting-features)
    - [Security Issues](#security-issues)
  - [Code of Conduct](#code-of-conduct)
  - [Current Limitations](#current-limitations)
    - [Analysis Paralysis](#analysis-paralysis)
    - [Git Worktree Merges](#git-worktree-merges)
    - [Self-Improvement Latency](#self-improvement-latency)
    - [Test Suite Dependency](#test-suite-dependency)
    - [Early Termination False Positives](#early-termination-false-positives)
    - [Cost Escalation](#cost-escalation)
  - [Getting Help](#getting-help)
  - [License](#license)
  - [Related Documentation](#related-documentation)

Welcome. EQUIPA is a multi-agent AI orchestrator that writes code, runs tests, and fixes bugs. It is pure Python stdlib — no pip dependencies, no framework lock-in. If you can edit text and run Python, you can contribute.

This guide covers how to get started, what to test, and how to submit changes. It assumes you already understand the basics of git and Python.

---

## Development Setup

### Prerequisites

You need Python 3.10+. That is it. No virtualenv, no pip installs. EQUIPA is pure stdlib.

Check your version:

```bash
python3 --version
```

If you are below 3.10, upgrade. The code uses structural pattern matching and async improvements from 3.10.

### Clone and Configure

```bash
git clone <your-fork-url>
cd equipa
```

EQUIPA needs a SQLite database and a config file. The fastest way to set up:

```bash
python3 equipa_setup.py
```

This wizard will:
- Create the database at `~/.forge/main.db`
- Copy core files (prompts, skills, schemas)
- Generate `~/.forge/config.json` with your Anthropic API key
- Optionally configure ForgeSmith nightly self-improvement runs

**Manual setup:** If you skip the wizard, you need:
1. A SQLite database with the schema from `resources/schema.sql`
2. A `config.json` with at least `{"anthropic_key": "sk-ant-..."}`
3. The `prompts/` directory accessible at runtime

### Verify Setup

Run the test suite:

```bash
python3 -m pytest tests/ -v
```

All tests should pass. If you see import errors or missing tables, rerun `equipa_setup.py`.

Run a smoke test dispatch:

```bash
python3 -m equipa.cli dispatch --task-ids 1
```

If task 1 does not exist, create one in the database or use `--auto` to dispatch pending work. You should see agent turns streaming to stdout.

---

## Code Style

EQUIPA follows PEP 8 with these exceptions:
- Line length: 120 characters (not 79)
- Trailing commas in multi-line structures
- Double quotes for strings (single quotes for dict keys if needed)

### Linting

No external linters required. Python's built-in tools are enough:

```bash
python3 -m py_compile equipa/*.py
python3 -m py_compile tests/*.py
```

If it compiles, it is probably fine. The test suite will catch most logic errors.

### Type Hints

Use them where they help. Do not use them where they do not. The codebase is partially typed — function signatures are annotated, but internal variables often are not. Match the existing style.

**Good:**
```python
def dispatch_task(task_id: int, role: str) -> dict[str, Any]:
    result = run_agent(task_id, role)
    return result
```

**Overkill:**
```python
def dispatch_task(task_id: int, role: str) -> dict[str, Any]:
    result: dict[str, Any] = run_agent(task_id, role)
    output: str = result.get("output", "")
    return result
```

### Async/Await

The dispatch system is async. Agent runners are sync. Do not mix them unless you know what you are doing. If you add async code, test it with real I/O (file writes, API calls). Mock-heavy async tests hide bugs.

---

## Making Changes

### Branch Naming

Use descriptive branch names:
- `feat/episode-graph-reranking` — new features
- `fix/loop-detector-false-positive` — bug fixes
- `docs/contributing-guide` — documentation
- `test/bash-security-coverage` — test additions

### Commit Messages

Write commit messages that explain WHY, not WHAT. The diff shows what changed. The message explains why it was necessary.

**Bad:**
```
Add graph reranking
```

**Good:**
```
Add graph-based episode reranking for cross-project learning

Episodes that help complete many tasks now score higher via
PageRank. Fixes issue where rare but critical episodes ranked
below common low-value ones.
```

Format:
```
Short summary (50 chars)

Longer explanation if needed. Wrap at 72 characters.
Reference issue numbers if relevant: #123
```

### Pull Request Process

1. **Fork and branch:** Create a feature branch from `main`.
2. **Make changes:** Edit code, add tests, update docs if needed.
3. **Run tests:** `pytest tests/ -v` — all tests must pass.
4. **Commit:** Follow commit message guidelines above.
5. **Push and PR:** Open a pull request against `main`.

**PR template:**

```markdown
## Summary
Brief description of what this PR does.

## Changes
- Added X to Y
- Fixed Z bug
- Updated documentation for feature A

## Testing
- Ran `pytest tests/test_X.py` — all pass
- Manual test: dispatched task with new feature, verified output

## Related Issues
Closes #123
```

### Review Expectations

PRs get reviewed within 48 hours. Expect feedback on:
- **Logic:** Does it work? Edge cases handled?
- **Tests:** New code needs tests. Fixes need regression tests.
- **Style:** Matches existing code style? No unnecessary complexity?
- **Documentation:** User-facing changes documented?

If a PR sits for more than 3 days with no review, ping the maintainers.

---

## Testing

### Running Tests

Full suite:
```bash
pytest tests/ -v
```

Single test file:
```bash
pytest tests/test_bash_security.py -v
```

Single test function:
```bash
pytest tests/test_bash_security.py::test_jq_system_function -v
```

### What to Test

**New features:** Add integration tests. Example: if you add a new agent role, create `tests/test_my_role.py` with:
- Prompt injection test (does it follow instructions?)
- Tool usage test (does it call tools correctly?)
- Error recovery test (does it retry transient failures?)

**Bug fixes:** Add regression tests. Example: if you fix a loop detector false positive, add a test case that triggers the old bug and verifies the fix.

**Refactors:** Existing tests should still pass. If you change internal structure but not behavior, tests should not need updates.

### Test Database

Tests use `tmp_path` fixtures for isolated databases. Do not write to `~/.forge/main.db` in tests. Do not mock SQLite — use real databases. Mocking SQLite hides schema bugs.

**Good:**
```python
def test_episode_retrieval(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("EQUIPA_DB", str(db_path))
    
    conn = sqlite3.connect(db_path)
    # setup schema
    # run test
    conn.close()
```

**Bad:**
```python
@patch("equipa.db.sqlite3.connect")
def test_episode_retrieval(mock_connect):
    # mocked database behavior — fragile, hides schema issues
```

### Test Coverage

Aim for 80%+ coverage on new code. Use `pytest --cov`:

```bash
pytest tests/ --cov=equipa --cov-report=term-missing
```

If coverage drops below 75%, the CI will flag it. Do not chase 100% coverage — some error paths are not worth testing (e.g., "SQLite file corrupted" edge cases).

---

## Pull Request Template

When you open a PR, include:

```markdown
## What does this PR do?
Brief summary (1-2 sentences).

## Why is this needed?
Context: what problem does it solve? What bug does it fix?

## How was it tested?
- Unit tests: `pytest tests/test_X.py`
- Integration test: dispatched task Y, verified output
- Manual verification: checked database state after run

## Breaking changes?
Yes/No. If yes, describe migration path.

## Related issues
Closes #123
Fixes #456
```

---

## Issue Reporting

### Filing Bugs

Use the bug report template:

```markdown
**Describe the bug**
A clear description of what went wrong.

**To Reproduce**
Steps to reproduce:
1. Run `python3 -m equipa.cli dispatch --task-ids 42`
2. Agent hits turn 5
3. Crash with error X

**Expected behavior**
Agent should retry API call, not crash.

**Environment**
- OS: macOS 14.2
- Python: 3.11.5
- EQUIPA commit: abc123f

**Logs**
Paste relevant logs from `~/.forge/logs/`.

**Additional context**
Anything else that might help.
```

### Requesting Features

Use the feature request template:

```markdown
**Feature description**
What do you want EQUIPA to do?

**Use case**
Why is this useful? Example scenario.

**Proposed implementation**
(Optional) How might this work?

**Alternatives considered**
(Optional) Other approaches you thought about.
```

### Security Issues

Do not file public issues for security bugs. Email the maintainers directly. If you find a bash injection bypass or prompt injection exploit, disclose privately first.

---

## Code of Conduct

**Be respectful.** This is a small project. Everyone here is learning. Code reviews should critique the code, not the author.

**Be honest.** If your PR is experimental, say so. If you are not sure about an approach, ask. Half-baked ideas are fine — just label them as such.

**Be patient.** Maintainers review PRs in their free time. If you need urgent feedback, say so, but do not expect instant responses.

**No tolerance for:**
- Personal attacks
- Harassment
- Spam or self-promotion unrelated to the project

Violations get you banned. No warnings.

---

## Current Limitations

EQUIPA is not magic. Here is what does not work well yet:

### Analysis Paralysis
Agents sometimes get stuck reading files repeatedly without making progress. The loop detector catches this after 6-8 turns, but it wastes API calls.

**Workaround:** Set `max_turns` lower for simple tasks. Use `complexity: low` in task descriptions.

### Git Worktree Merges
The autoresearch loop isolates failed tasks in git worktrees. Merging them back occasionally requires manual conflict resolution.

**Workaround:** Check `git status` in the project directory after autoresearch runs. Resolve conflicts manually if needed.

### Self-Improvement Latency
ForgeSmith (the self-improvement agent) needs 20-30 task completions before patterns emerge. Early runs produce generic advice like "add error handling".

**Workaround:** Let it run nightly for 2-3 weeks before expecting useful prompt changes.

### Test Suite Dependency
The Tester role assumes your project has a working test suite. If tests are broken or missing, the agent will fail every task.

**Workaround:** Add a basic test suite before dispatching Tester tasks. Even a single smoke test is enough.

### Early Termination False Positives
The loop detector kills agents after 10 consecutive read-only turns. Some legitimate tasks (e.g., "analyze 50 files and summarize") get killed prematurely.

**Workaround:** Split large analysis tasks into smaller chunks.

### Cost Escalation
Complex tasks can burn $5-10 in API calls if the agent gets stuck. The cost breaker kills runaway agents, but the limit is per-task, not per-session.

**Workaround:** Monitor `~/.forge/logs/` for high-cost tasks. Lower `max_cost_per_task` in `dispatch_config.json` if needed.

---

## Getting Help

- **GitHub Issues:** Questions about usage, architecture, or design.
- **Pull Requests:** Propose changes, even if incomplete. Tag as [WIP] if still working on it.
- **Discussions:** Long-form questions or brainstorming.

Do not DM maintainers unless it is a security issue. Public questions help everyone.

---

## License

EQUIPA is MIT-licensed. By contributing, you agree your code will be released under the same license.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
