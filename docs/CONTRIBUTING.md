# Contributing to Project Pombal

## Table of Contents

- [Contributing to Project Pombal](#contributing-to-project-pombal)
  - [Welcome](#welcome)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Getting Started](#getting-started)
    - [Zero Dependencies Policy](#zero-dependencies-policy)
  - [Code Style](#code-style)
    - [General Conventions](#general-conventions)
    - [Naming Conventions](#naming-conventions)
    - [Code Patterns to Follow](#code-patterns-to-follow)
    - [Documentation](#documentation)
  - [Making Changes](#making-changes)
    - [Branch Naming](#branch-naming)
    - [Commit Messages](#commit-messages)
    - [Key Architectural Considerations](#key-architectural-considerations)
  - [Testing](#testing)
    - [Running Tests](#running-tests)
- [Core orchestration tests](#core-orchestration-tests)
- [Agent communication tests](#agent-communication-tests)
- [Learning system tests](#learning-system-tests)
- [Quality and scoring tests](#quality-and-scoring-tests)
- [Routing and dispatch tests](#routing-and-dispatch-tests)
- [Monologue detection tests](#monologue-detection-tests)
- [Forgesmith self-improvement tests](#forgesmith-self-improvement-tests)
- [Database migration benchmarks](#database-migration-benchmarks)
    - [What to Test](#what-to-test)
    - [Testing Conventions](#testing-conventions)
  - [Pull Request Process](#pull-request-process)
    - [Before Submitting](#before-submitting)
    - [PR Description](#pr-description)
    - [Review Expectations](#review-expectations)
  - [Issue Reporting](#issue-reporting)
    - [Bug Reports](#bug-reports)
    - [Feature Requests](#feature-requests)
    - [Labels](#labels)
  - [Code of Conduct](#code-of-conduct)
  - [Questions?](#questions)
  - [Related Documentation](#related-documentation)

## Welcome

Thank you for your interest in contributing to Project Pombal! Whether you're fixing a bug, improving documentation, adding tests, or proposing a new feature, your contributions are valued and appreciated.

Project Pombal is a multi-agent AI orchestration platform — pure Python stdlib, zero pip dependencies, SQLite-based. We aim to keep it that way: lean, understandable, and reliable.

---

## Development Setup

### Prerequisites

- **Python 3.8+** (no external packages required — pure stdlib)
- **SQLite 3** (bundled with Python)
- **Git**
- **Claude CLI** (for agent dispatch features — optional for most contributions)

### Getting Started

1. **Fork and clone the repository:**
   ```bash
   git clone https://github.com/<your-username>/ProjectPombal.git
   cd ProjectPombal
   ```

2. **Run the setup wizard** (configures database and local environment):
   ```bash
   python pombal_setup.py
   ```

3. **Initialize/migrate the database:**
   ```bash
   python db_migrate.py
   ```

4. **Verify your setup** by running the test suite:
   ```bash
   python test_early_termination.py
   python test_loop_detection.py
   python test_agent_messages.py
   python test_agent_actions.py
   python test_lesson_sanitizer.py
   python test_lessons_injection.py
   python test_episode_injection.py
   python test_rubric_scoring.py
   python test_rubric_quality_scorer.py
   python test_task_type_routing.py
   python test_early_termination_monologue.py
   python test_forgesmith_simba.py
   ```

### Zero Dependencies Policy

Project Pombal uses **only the Python standard library**. Do not introduce pip dependencies. If you need functionality from an external package, implement it using stdlib or discuss the need in an issue first.

---

## Code Style

### General Conventions

- **Pure Python stdlib** — no external dependencies
- **Functions over classes** — the codebase favors standalone functions; use classes only when state management genuinely requires it
- **SQLite for persistence** — all data flows through the 30+ table SQLite schema
- **Descriptive function names** — `classify_agent_failure`, `build_checkpoint_context`, `parse_tester_output`
- **Consistent `main()` entry points** — each script has a `main()` function as its entry point

### Naming Conventions

- **Files:** `snake_case.py` (e.g., `forge_orchestrator.py`, `lesson_sanitizer.py`)
- **Functions:** `snake_case` (e.g., `get_db_connection`, `run_dev_test_loop`)
- **Classes:** `PascalCase` (e.g., `LoopDetector`, `RolePromptModule`)
- **Constants:** `UPPER_SNAKE_CASE`
- **Test functions:** `test_<description>` (e.g., `test_fingerprint_extracts_blockers`)

### Code Patterns to Follow

- Use `get_db(write=False)` / `get_db_connection(write=False)` pattern for database access
- Use `log(msg)` helper functions for output
- Guard destructive operations behind `dry_run` parameters
- Include backup mechanisms before modifying config or prompts (see `backup_file()`)

### Documentation

- Include docstrings for public functions
- Update `CLAUDE.md` if you change the file map or add new modules
- Keep comments practical — explain *why*, not *what*

---

## Making Changes

### Branch Naming

Use descriptive branch names with a prefix:

- `feature/` — new functionality (e.g., `feature/parallel-goal-dispatch`)
- `fix/` — bug fixes (e.g., `fix/loop-detector-reset-counter`)
- `test/` — test additions or improvements (e.g., `test/episode-injection-edge-cases`)
- `docs/` — documentation updates (e.g., `docs/update-setup-guide`)
- `refactor/` — code improvements without behavior change (e.g., `refactor/consolidate-db-helpers`)

### Commit Messages

Write clear, descriptive commit messages:

```
<type>: <short summary>

<optional body explaining why, not what>
```

**Types:** `feat`, `fix`, `test`, `docs`, `refactor`, `perf`

**Examples:**
```
feat: add cost breaker configurable via dispatch config
fix: loop detector not resetting counter on file changes
test: add edge cases for lesson sanitizer base64 stripping
docs: update CLAUDE.md with new forgesmith_simba module
```

### Key Architectural Considerations

- **`forge_orchestrator.py`** is the core — changes here have wide impact. Be extra careful and thorough with testing.
- **`forgesmith.py`** and its siblings (`forgesmith_simba.py`, `forgesmith_gepa.py`, `forgesmith_impact.py`) form the self-improvement engine. Changes should preserve the analysis → propose → validate → apply pipeline.
- **Database schema changes** must go through `db_migrate.py` with a proper migration function (e.g., `migrate_v4_to_v5`).
- **The `dry_run` pattern** is sacred — any function that writes to disk or database should support `dry_run=True`.

---

## Testing

### Running Tests

Each test module is a standalone script. Run individual test files:

```bash
# Core orchestration tests
python test_early_termination.py
python test_loop_detection.py

# Agent communication tests
python test_agent_messages.py
python test_agent_actions.py

# Learning system tests
python test_lessons_injection.py
python test_lesson_sanitizer.py
python test_episode_injection.py

# Quality and scoring tests
python test_rubric_scoring.py
python test_rubric_quality_scorer.py

# Routing and dispatch tests
python test_task_type_routing.py

# Monologue detection tests
python test_early_termination_monologue.py

# Forgesmith self-improvement tests
python test_forgesmith_simba.py

# Database migration benchmarks
python benchmark_migrations.py
```

To run all tests:

```bash
for f in test_*.py; do echo "=== $f ===" && python "$f" && echo "PASS" || echo "FAIL"; done
```

### What to Test

- **New functions:** Write tests following the existing pattern — standalone test functions named `test_<description>()`
- **Bug fixes:** Add a regression test that would have caught the bug
- **Database changes:** Test migration paths and verify data integrity
- **Sanitization/security:** Test adversarial inputs (see `test_lesson_sanitizer.py` for examples — injection tags, base64 payloads, ANSI escapes)
- **Edge cases:** Empty inputs, `None` values, boundary conditions, and error paths
- **Dry run behavior:** Verify that `dry_run=True` never modifies state

### Testing Conventions

- Tests use in-memory SQLite databases (`:memory:`) or temporary files — never touch production data
- Each test module includes a `main()` or `run_tests()` / `run_all_tests()` entry point
- Use `setup_test_data()` / `cleanup_*()` patterns for test fixtures
- Tests should be self-contained and order-independent

---

## Pull Request Process

### Before Submitting

1. **Run the full test suite** — all tests must pass
2. **Test your changes manually** if they affect agent dispatch or database operations
3. **Check the dry_run path** if your change involves writes
4. **Update `CLAUDE.md`** if you've added or renamed files/modules
5. **Keep the zero-dependency constraint** — no new pip packages

### PR Description

Include in your pull request:

- **What** — clear summary of the change
- **Why** — the problem it solves or feature it adds
- **How** — brief description of the approach
- **Testing** — which tests you ran and any manual testing performed
- **Breaking changes** — flag any changes to database schema, config format, or CLI interface

### Review Expectations

- PRs will be reviewed for correctness, test coverage, and adherence to the project's architectural patterns
- Expect feedback on edge cases, error handling, and the dry_run path
- Database schema changes and changes to `forge_orchestrator.py` receive extra scrutiny
- Small, focused PRs are reviewed faster than large omnibus changes

---

## Issue Reporting

### Bug Reports

When filing a bug, please include:

- **What happened** vs. **what you expected**
- **Steps to reproduce**
- **Which script/module** is affected (e.g., `forge_orchestrator.py`, `forgesmith.py`)
- **Database state** if relevant (schema version from `db_migrate.py`)
- **Error output** — full traceback if available
- **Environment** — Python version, OS

### Feature Requests

For feature requests, please describe:

- **The problem** you're trying to solve
- **Your proposed solution** (if you have one)
- **Alternatives considered**
- **Impact on existing components** — which modules would be affected

### Labels

Use descriptive titles. Prefix with the affected area when possible:

- `[orchestrator] Agent retries not respecting max_retries`
- `[forgesmith] SIMBA rules not pruning stale entries`
- `[schema] Need migration for new quality_scores column`

---

## Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free experience for everyone. We expect all contributors to:

- **Be respectful** — treat others as you'd want to be treated
- **Be constructive** — offer helpful feedback, not dismissive criticism
- **Be collaborative** — we're all building something together
- **Be patient** — especially with newcomers learning the codebase
- **Assume good intent** — misunderstandings happen; clarify before escalating

Unacceptable behavior includes harassment, personal attacks, trolling, and publishing others' private information. Violations can be reported to the project maintainers and may result in removal from the project.

---

## Questions?

If you're unsure about anything — where to start, how something works, whether a change makes sense — open an issue or start a discussion. There are no bad questions, especially in a codebase that coordinates AI agents to rebuild things from scratch. The Marquis would approve.
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
