---
name: framework-detection
description: >
  Quickly identify the test framework, find the correct test command, and run tests in
  unfamiliar projects. Use when you don't know what test framework the project uses, when
  you can't find the test command, or when tests won't run. Triggers: find tests, test command,
  how to run tests, test framework, no tests found.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Test Framework Detection

## Core Principle

**Find the test command in 1 turn, run it in turn 2, report results in turn 3.**
You have a maximum of 4 turns to complete your job. Spend zero time on anything else.

## When to Use

- You don't know what test framework the project uses
- You can't find the test command
- The obvious test command fails
- CI/CD config exists but you need the local equivalent

## When NOT to Use

- You already know the test command (just run it)
- The task is to write tests, not run them (use test-generation instead)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "I'll read the README to find the test command" | README may be outdated. Check config files. | Check package.json/Makefile/CI config first |
| "I'll search the entire codebase for test files" | Wastes turns. Use the detection matrix. | Follow the 3-step detection method |
| "There are no tests" | You probably looked in the wrong place | Exhaust ALL 5 detection strategies before declaring no-tests |
| "I'll install a test framework" | Not your job. Run what exists. | Report no-tests if nothing exists |

## The 3-Step Detection Method

### Step 1: Check Package Manager Config (1 turn max)

**Priority order — check the FIRST one that exists:**

| File | Framework Signal | Test Command |
|------|-----------------|-------------|
| `package.json` → `scripts.test` | Direct test command | `npm test` or `npm run test` |
| `package.json` → `devDependencies` | jest, vitest, mocha, etc. | See framework table below |
| `Makefile` or `Taskfile.yml` | `test:` target | `make test` or `task test` |
| `pyproject.toml` → `[tool.pytest]` | pytest | `pytest` or `python -m pytest` |
| `setup.cfg` → `[tool:pytest]` | pytest | `pytest` |
| `tox.ini` | tox + underlying framework | `tox` or `pytest` |
| `go.mod` | Go testing | `go test ./...` |
| `Cargo.toml` | Rust testing | `cargo test` |
| `*.csproj` | .NET testing | `dotnet test` |
| `pom.xml` | Maven | `mvn test` |
| `build.gradle` | Gradle | `./gradlew test` |
| `mix.exs` | Elixir | `mix test` |
| `Gemfile` | Ruby | `bundle exec rspec` or `bundle exec rake test` |

### Step 2: Check CI/CD Config (only if Step 1 fails)

```
Glob: .github/workflows/*.{yml,yaml}, .gitlab-ci.yml, Jenkinsfile,
      .circleci/config.yml, .travis.yml, bitbucket-pipelines.yml
```

Search for `test` in the CI config — the exact command is usually there.

### Step 3: Check Test File Patterns (only if Steps 1-2 fail)

```
Glob: **/*test*.{py,js,ts,go,rs,cs,java,rb}
Glob: **/*spec*.{py,js,ts,rb}
Glob: **/tests/**
Glob: **/test/**
Glob: **/__tests__/**
```

If test files exist but no config, use the framework detection table:

## Framework Detection Table

### Python
| Signal | Framework | Command |
|--------|-----------|---------|
| `import pytest` in test files | pytest | `pytest` |
| `import unittest` in test files | unittest | `python -m unittest discover` |
| `conftest.py` exists | pytest | `pytest` |
| `pytest.ini` or `pyproject.toml [tool.pytest]` | pytest | `pytest` |
| `nose2.cfg` | nose2 | `nose2` |

### JavaScript / TypeScript
| Signal | Framework | Command |
|--------|-----------|---------|
| `jest.config.*` exists | Jest | `npx jest` |
| `vitest.config.*` exists | Vitest | `npx vitest run` |
| `.mocharc.*` exists | Mocha | `npx mocha` |
| `cypress.config.*` exists | Cypress | `npx cypress run` |
| `playwright.config.*` exists | Playwright | `npx playwright test` |
| `karma.conf.*` exists | Karma | `npx karma start` |

### Go
| Signal | Framework | Command |
|--------|-----------|---------|
| `*_test.go` files exist | Go testing | `go test ./...` |
| `testify` in go.mod | Go + Testify | `go test ./...` |

### Rust
| Signal | Framework | Command |
|--------|-----------|---------|
| `#[cfg(test)]` in source files | Built-in | `cargo test` |
| `tests/` directory | Integration tests | `cargo test` |

### C# / .NET
| Signal | Framework | Command |
|--------|-----------|---------|
| `*.Tests.csproj` or `*.Test.csproj` | Various | `dotnet test` |
| `[TestClass]` or `[Fact]` in code | MSTest / xUnit | `dotnet test` |
| `NUnit` in .csproj | NUnit | `dotnet test` |

### Ruby
| Signal | Framework | Command |
|--------|-----------|---------|
| `spec/` directory | RSpec | `bundle exec rspec` |
| `test/` directory + `Rakefile` | Minitest | `bundle exec rake test` |

## Running Tests

### Timeout Handling

Always run tests with a timeout:
```bash
timeout 90 npm test                    # Node
timeout 90 pytest                      # Python
timeout 90 go test ./...               # Go
timeout 90 cargo test                  # Rust
timeout 90 dotnet test                 # C#
```

If tests hang after 60 seconds, kill and report `RESULT: blocked`.

### Test Scope

Run tests in this order:
1. **Full suite first:** `npm test` / `pytest` / `go test ./...`
2. **If that's too slow (>120s):** Run only tests related to changed files
3. **If specific tests fail:** Report the failures, don't try to fix them

### Background Execution for Long Suites

```bash
# Run in background, check result
npm test > /tmp/test-output.txt 2>&1 &
TEST_PID=$!
sleep 90
if kill -0 $TEST_PID 2>/dev/null; then
    kill $TEST_PID
    echo "TESTS TIMED OUT"
else
    wait $TEST_PID
    echo "Exit code: $?"
fi
cat /tmp/test-output.txt
```

## Quality Checklist

- [ ] Test command found within 1 turn
- [ ] Tests executed within 2 turns
- [ ] Results reported within 3 turns
- [ ] If no tests exist, all 5 detection strategies exhausted before reporting
- [ ] Timeout was set for test execution
- [ ] Output includes TESTS_RUN, TESTS_PASSED, TESTS_FAILED counts
