# Contributing to Itzamna (ForgeTeam)

## Table of Contents

- [Contributing to Itzamna (ForgeTeam)](#contributing-to-itzamna-forgeteam)
  - [Welcome](#welcome)
  - [Development Setup](#development-setup)
    - [Prerequisites](#prerequisites)
    - [Getting Started](#getting-started)
    - [Project Structure](#project-structure)
  - [Code Style](#code-style)
    - [General Conventions](#general-conventions)
    - [Documentation](#documentation)
    - [Database Conventions](#database-conventions)
    - [Naming Patterns](#naming-patterns)
  - [Making Changes](#making-changes)
    - [Branch Naming](#branch-naming)
    - [Commit Messages](#commit-messages)
    - [Working with the Database](#working-with-the-database)
  - [Testing](#testing)
    - [Running Tests](#running-tests)
- [Run individual test suites](#run-individual-test-suites)
    - [Running All Tests](#running-all-tests)
- [Run all test files](#run-all-test-files)
    - [What to Test](#what-to-test)
    - [Test Patterns in This Project](#test-patterns-in-this-project)
  - [Pull Request Process](#pull-request-process)
    - [Before Submitting](#before-submitting)
    - [PR Description](#pr-description)
    - [Review Expectations](#review-expectations)
  - [Issue Reporting](#issue-reporting)
    - [Bug Reports](#bug-reports)
    - [Feature Requests](#feature-requests)
    - [Labels](#labels)
  - [Code of Conduct](#code-of-conduct)
  - [Related Documentation](#related-documentation)

## Welcome

Thank you for your interest in contributing to Itzamna! Whether you're fixing a bug, improving documentation, adding a new feature, or enhancing the multi-agent orchestration system, your contribution is valued. ForgeTeam is a community-driven project, and we're glad you're here.

This guide will help you get set up and contributing effectively.

---

## Development Setup

### Prerequisites

Itzamna requires the following tools. The setup wizard (`itzamna_setup.py`) will verify most of these for you:

- **Python 3.10+**
- **SQLite3** (ships with Python, but the CLI tool is useful for debugging)
- **Claude Code** (for agent orchestration)
- **Git**

### Getting Started

1. **Fork and clone the repository:**

   ```bash
   git clone https://github.com/<your-username>/itzamna.git
   cd itzamna
   ```

2. **Run the setup wizard** to initialize the database and configuration:

   ```bash
   python itzamna_setup.py
   ```

   This will walk you through prerequisites checks, install path selection, database creation, config generation, and optional components (Sentinel, ForgeBot).

3. **Run database migrations** (if working on an existing install):

   ```bash
   python db_migrate.py
   ```

4. **Verify the setup:**

   The setup wizard includes a verification step. You can also check the database manually:

   ```bash
   sqlite3 <your-db-path> ".tables"
   ```

### Project Structure

| Path | Purpose |
|------|---------|
| `forge_orchestrator.py` | Core multi-agent orchestrator — the heart of ForgeTeam |
| `forgesmith.py` | Self-improvement engine: analysis, lessons, rubric scoring |
| `forgesmith_simba.py` | SIMBA rule generation from high-variance episodes |
| `forgesmith_gepa.py` | Genetic-evolutionary prompt optimization (GEPA) |
| `forgesmith_backfill.py` | Backfill historical episode data |
| `forge_arena.py` | Adversarial testing arena |
| `forge_dashboard.py` | Performance dashboard |
| `analyze_performance.py` | Performance analytics and reporting |
| `ollama_agent.py` | Local Ollama model integration |
| `itzamna_setup.py` | Guided installer/setup wizard |
| `db_migrate.py` | Database schema migrations |
| `prepare_training_data.py` | Training data preparation for fine-tuning |
| `train_qlora.py` / `train_qlora_peft.py` | QLoRA fine-tuning scripts |
| `skills/` | Modular skill definitions (e.g., SARIF parsing) |
| `test_*.py` | Test files |

---

## Code Style

### General Conventions

- **Python** is the primary language. Follow [PEP 8](https://peps.python.org/pep-0008/) conventions.
- Use **snake_case** for functions and variables, **PascalCase** for classes.
- Keep functions focused — this codebase favors many small, well-named functions over large monolithic ones.
- Use **type hints** where practical, especially for function signatures.
- Prefer **f-strings** for string formatting.

### Documentation

- All public functions should have clear, descriptive names. Docstrings are encouraged for non-trivial logic.
- Update `CLAUDE.md` if your change affects project behavior, configuration, or the orchestration pipeline.
- SQL schema changes must include a corresponding migration in `db_migrate.py`.

### Database Conventions

- All database access should use the `get_db()` or `get_db_connection()` patterns established in the codebase.
- Use the `write=True` parameter when performing writes.
- Table creation functions should be idempotent (use `CREATE TABLE IF NOT EXISTS`).
- Schema changes require a new migration version in `db_migrate.py`.

### Naming Patterns

- Test files: `test_<module_name>.py`
- Test functions: `test_<what_is_being_tested>()`
- Helper/setup functions in tests: `setup_test_data()`, `cleanup_*()`, `make_*()` prefixes
- Configuration loading: `load_config()` pattern
- Database connections: `get_db()` or `get_db_connection()` pattern

---

## Making Changes

### Branch Naming

Use descriptive branch names with a prefix indicating the type of work:

| Prefix | Use For |
|--------|---------|
| `feat/` | New features (`feat/ollama-streaming`) |
| `fix/` | Bug fixes (`fix/loop-detection-threshold`) |
| `docs/` | Documentation changes (`docs/contributing-guide`) |
| `refactor/` | Code restructuring (`refactor/episode-injection`) |
| `test/` | Test additions or fixes (`test/simba-validation`) |
| `schema/` | Database schema changes (`schema/add-q-value-index`) |

### Commit Messages

Write clear, descriptive commit messages:

```
<type>: <short summary>

<optional longer description>
```

**Examples:**

```
feat: add cross-project episode fallback for injection

When no project-specific episodes are found, the system now
falls back to cross-project episodes with reduced Q-value weight.
```

```
fix: loop detector resets counter when files change

The LoopDetector._get_files_changed check was not properly
resetting the repetition counter on new file modifications.
```

```
schema: add prompt_version column to agent_episodes

Migration v3: adds prompt_version tracking for GEPA A/B testing.
Includes backfill for existing rows.
```

### Working with the Database

If your change involves schema modifications:

1. Add a new migration function in `db_migrate.py` following the pattern `migrate_vN_to_vN+1(conn)`.
2. Update the migration chain in `run_migrations()`.
3. Make sure the migration is idempotent and safe to re-run.
4. Test the migration against both fresh databases and existing ones.

---

## Testing

### Running Tests

The project uses standalone test scripts (no external test framework required). Each test file has a `main()` function that runs its tests:

```bash
# Run individual test suites
python test_loop_detection.py
python test_early_termination.py
python test_agent_messages.py
python test_agent_actions.py
python test_episode_injection.py
python test_lessons_injection.py
python test_forgesmith_simba.py
python test_rubric_scoring.py
python test_task_type_routing.py
python test_task_type_routing_verification.py
python test_task_665_verification.py
```

### Running All Tests

```bash
# Run all test files
for f in test_*.py; do echo "=== $f ===" && python "$f"; done
```

### What to Test

- **New features:** Add corresponding test functions in the relevant `test_*.py` file, or create a new test file if the feature is a new module.
- **Bug fixes:** Add a regression test that would have caught the bug.
- **Database changes:** Test both the migration path and the new functionality.
- **Edge cases:** The existing tests demonstrate good patterns — test empty inputs, boundary values, error conditions, and idempotency.

### Test Patterns in This Project

Tests in Itzamna follow consistent patterns:

```python
def test_your_feature():
    """Test that your feature does the expected thing."""
    # Setup
    # ... arrange test data ...

    # Execute
    result = function_under_test(input_data)

    # Verify
    assert result == expected, f"Expected {expected}, got {result}"
    print("  ✓ test_your_feature passed")
```

- Tests use `assert` statements with descriptive failure messages.
- Each test prints a checkmark on success for clear visual feedback.
- Setup/teardown helpers create and clean up temporary databases when needed.
- Tests are self-contained — no shared mutable state between test functions.

---

## Pull Request Process

### Before Submitting

1. **Run all tests** and confirm they pass.
2. **Test your change manually** if it affects the orchestrator, dashboard, or setup wizard.
3. **Check for regressions** — if you modified `forge_orchestrator.py`, run the full test suite since many modules depend on it.
4. **Update documentation** — if your change affects behavior described in `CLAUDE.md` or the README, update those files.

### PR Description

Include in your pull request:

- **What** the change does (concise summary)
- **Why** the change is needed (bug report link, feature request, or rationale)
- **How** it works (brief technical explanation for non-trivial changes)
- **Testing** — what tests you added or ran
- **Migration notes** — if the change requires database migration or configuration updates

### Review Expectations

- PRs will be reviewed for correctness, test coverage, and consistency with existing patterns.
- Database schema changes and orchestrator modifications receive extra scrutiny since they affect the entire system.
- Small, focused PRs are easier to review and more likely to be merged quickly.
- Expect constructive feedback — we're all here to make ForgeTeam better.

---

## Issue Reporting

### Bug Reports

When filing a bug, please include:

1. **Description:** What happened vs. what you expected.
2. **Steps to reproduce:** Minimal steps to trigger the bug.
3. **Environment:** Python version, OS, database schema version (check with `python db_migrate.py`).
4. **Logs/output:** Relevant error messages or orchestrator output.
5. **Database state:** If relevant, the task status or episode data involved.

### Feature Requests

For feature requests, please describe:

1. **The problem** you're trying to solve.
2. **Your proposed solution** (if you have one).
3. **Alternatives considered** and why they don't work.
4. **Which components** would be affected (orchestrator, forgesmith, dashboard, etc.).

### Labels

- `bug` — Something isn't working correctly
- `enhancement` — New feature or improvement
- `documentation` — Documentation improvements
- `schema` — Database schema changes
- `forgesmith` — Related to the self-improvement engine
- `orchestrator` — Related to the core agent orchestration

---

## Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free experience for everyone. All contributors are expected to:

- **Be respectful** and constructive in all interactions.
- **Be collaborative** — assume good intent, offer help, and accept feedback gracefully.
- **Be inclusive** — welcome newcomers and support contributors of all experience levels.
- **Focus on the work** — technical disagreements are fine; personal attacks are not.

If you experience or witness unacceptable behavior, please report it to the project maintainers. We take all reports seriously and will respond promptly.

---

Thank you for contributing to Itzamna! Every improvement — whether it's a typo fix, a new test, or a major feature — helps make ForgeTeam better for everyone. 🏗️
---

## Related Documentation

- [Readme](README.md)
- [Architecture](ARCHITECTURE.md)
- [Api](API.md)
- [Deployment](DEPLOYMENT.md)
