---
name: bootstrap-tests
description: >
  Create test infrastructure from scratch when none exists. Teaches the tester how to create
  pytest conftest.py, add pytest to dependencies, create test fixtures, and run tests.
  Use when: project has no tests, no conftest.py, no test framework installed, or pytest not
  in dependencies. The tester should NEVER block because "no tests exist" — CREATE the infrastructure.
allowed-tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Bootstrap Test Infrastructure

## Core Principle

**NEVER block because tests don't exist. CREATE the test infrastructure in 3-5 turns.**

When a project has no tests, your job is to create the minimum viable test infrastructure so future tests can be written. This is not about writing comprehensive tests — it's about removing the blocker that prevents ANY tests from running.

## When to Use This Skill

- Project has NO test files at all
- `conftest.py` does not exist (Python projects)
- pytest is not installed / not in dependencies
- Test command fails with "No module named 'pytest'"
- You've been asked to test something but infrastructure is missing

## When NOT to Use

- Tests exist but are failing (fix the tests, don't rebuild infrastructure)
- pytest is installed and working (just write tests)
- You're in a non-Python project (use language-specific test setup)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "There are no tests, I'll report blocked" | Your job is to CREATE infrastructure | Bootstrap pytest + conftest.py |
| "I don't know what to test" | You're not writing tests yet, just infrastructure | Create fixtures, add pytest to deps, write 1 smoke test |
| "The project is too complex for me to test" | Start simple — test one function | Write a single passing test to prove infrastructure works |
| "I'll wait for the developer to set this up" | NO. You're the tester. This is your job. | Create the infrastructure NOW |

## The 5-Turn Bootstrap Process

### Turn 1: Detect Language & Current State

**Goal: Identify what exists and what's missing**

```bash
# Check for Python project markers
ls -la | grep -E "(pyproject.toml|setup.py|requirements.txt|Pipfile)"

# Check for existing tests
find . -name "*test*.py" -o -name "conftest.py" | head -20

# Check if pytest is installed
python -m pytest --version 2>&1 || echo "pytest not installed"
```

**Decision tree:**
- Python project + no pytest → Add pytest to dependencies (Turn 2)
- Python project + pytest installed + no conftest.py → Create conftest.py (Turn 2)
- Python project + pytest + conftest.py exists → Write smoke test (Turn 2)
- Non-Python project → Use language-specific bootstrap (see Language Matrix below)

### Turn 2: Add pytest to Dependencies

**For Python projects, pytest MUST be in the dependency manifest.**

#### If `requirements.txt` exists:
```bash
# Check if pytest is already there
grep -i pytest requirements.txt

# If not, add it (use Edit tool)
# Add to requirements.txt:
pytest>=8.0.0
pytest-asyncio>=0.23.0  # If project uses asyncio
```

#### If `pyproject.toml` exists:
```python
# Add to [project.optional-dependencies] or [tool.poetry.group.dev.dependencies]

# For setuptools projects:
[project.optional-dependencies]
test = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

# For Poetry projects:
[tool.poetry.group.dev.dependencies]
pytest = "^8.0.0"
pytest-asyncio = "^0.23.0"
```

#### If `Pipfile` exists:
```toml
[dev-packages]
pytest = ">=8.0.0"
pytest-asyncio = ">=0.23.0"
```

**Then install:**
```bash
# Try the appropriate command:
pip install pytest pytest-asyncio      # requirements.txt
poetry install                         # pyproject.toml + poetry
pipenv install --dev                   # Pipfile
```

### Turn 3: Create conftest.py

**Create a minimal `conftest.py` with common fixtures.**

Location: `tests/conftest.py` or `conftest.py` in project root.

```python
"""
Pytest configuration and shared fixtures.
"""
import pytest
import tempfile
import shutil
from pathlib import Path


@pytest.fixture
def tmp_dir():
    """
    Provide a temporary directory that's cleaned up after the test.

    Usage:
        def test_something(tmp_dir):
            test_file = tmp_dir / "test.txt"
            test_file.write_text("data")
    """
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def sample_data():
    """
    Provide sample data for testing.

    Usage:
        def test_something(sample_data):
            assert sample_data["key"] == "value"
    """
    return {
        "key": "value",
        "items": [1, 2, 3],
        "nested": {"inner": "data"}
    }


# Add asyncio fixture if project uses async
@pytest.fixture
def event_loop():
    """
    Create an event loop for async tests.

    Usage:
        @pytest.mark.asyncio
        async def test_async_function():
            result = await async_call()
            assert result == expected
    """
    import asyncio
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
```

**Customize based on project:**
- If project uses databases → add DB fixture
- If project uses APIs → add mock API fixture
- If project has config files → add config fixture

### Turn 4: Create pytest.ini (Optional but Recommended)

**Create `pytest.ini` to configure test discovery and output.**

```ini
[pytest]
# Test discovery patterns
python_files = test_*.py *_test.py
python_classes = Test*
python_functions = test_*

# Test directories
testpaths = tests

# Output options
addopts =
    -v                      # Verbose output
    --tb=short             # Short traceback format
    --strict-markers       # Error on unknown markers
    --disable-warnings     # Hide warnings for cleaner output

# Markers for categorizing tests
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    integration: marks tests as integration tests
    unit: marks tests as unit tests
```

### Turn 5: Write a Smoke Test

**Create a single test to verify infrastructure works.**

Location: `tests/test_smoke.py`

```python
"""
Smoke test to verify pytest infrastructure is working.
"""
import pytest


def test_pytest_works():
    """Basic test to confirm pytest can run."""
    assert True


def test_fixtures_available(tmp_dir, sample_data):
    """Verify conftest.py fixtures are loaded."""
    assert tmp_dir.exists()
    assert "key" in sample_data


@pytest.mark.asyncio
async def test_async_works():
    """Verify async tests can run (if project uses asyncio)."""
    result = await async_dummy()
    assert result == "async works"


async def async_dummy():
    """Dummy async function for testing."""
    return "async works"
```

**Then run:**
```bash
python -m pytest tests/test_smoke.py -v
```

If this passes → infrastructure is complete. If it fails → fix the error and re-run.

## Language-Specific Bootstrap

### JavaScript / TypeScript

**Turn 1: Check package.json**
```bash
cat package.json | grep -E "(jest|vitest|mocha)"
```

**Turn 2: Add test framework**
```bash
# For Jest:
npm install --save-dev jest @types/jest

# For Vitest:
npm install --save-dev vitest
```

**Turn 3: Create jest.config.js or vitest.config.ts**
```javascript
// jest.config.js
module.exports = {
  testEnvironment: 'node',
  testMatch: ['**/__tests__/**/*.test.js'],
  collectCoverageFrom: ['src/**/*.js'],
};

// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
  },
});
```

**Turn 4: Add to package.json**
```json
{
  "scripts": {
    "test": "jest",
    "test:watch": "jest --watch"
  }
}
```

**Turn 5: Write smoke test**
```javascript
// __tests__/smoke.test.js
describe('Smoke Test', () => {
  test('infrastructure works', () => {
    expect(true).toBe(true);
  });
});
```

### Go

**Turn 1: Check for go.mod**
```bash
cat go.mod
```

**Turn 2: Create test file**
```go
// smoke_test.go
package main

import "testing"

func TestSmoke(t *testing.T) {
    if true != true {
        t.Fatal("infrastructure broken")
    }
}
```

**Turn 3: Run tests**
```bash
go test ./...
```

**No additional setup needed — Go has built-in testing.**

### Rust

**Turn 1: Check for Cargo.toml**
```bash
cat Cargo.toml
```

**Turn 2: Create test file**
```rust
// tests/smoke_test.rs
#[test]
fn test_infrastructure() {
    assert_eq!(2 + 2, 4);
}
```

**Turn 3: Run tests**
```bash
cargo test
```

**No additional setup needed — Cargo has built-in testing.**

### C# / .NET

**Turn 1: Check for .csproj**
```bash
find . -name "*.csproj"
```

**Turn 2: Create test project**
```bash
dotnet new xunit -n ProjectName.Tests
cd ProjectName.Tests
dotnet add reference ../ProjectName/ProjectName.csproj
```

**Turn 3: Write smoke test**
```csharp
// SmokeTests.cs
using Xunit;

public class SmokeTests
{
    [Fact]
    public void TestInfrastructure()
    {
        Assert.True(true);
    }
}
```

**Turn 4: Run tests**
```bash
dotnet test
```

## Common Fixtures Library

### Python Fixtures

```python
# Database fixture (for projects using SQLite/PostgreSQL)
@pytest.fixture
def test_db():
    """Provide a test database connection."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


# Mock API fixture
@pytest.fixture
def mock_api(monkeypatch):
    """Mock external API calls."""
    def mock_get(*args, **kwargs):
        return {"status": "ok", "data": []}

    monkeypatch.setattr("requests.get", mock_get)
    return mock_get


# Environment variable fixture
@pytest.fixture
def test_env(monkeypatch):
    """Set test environment variables."""
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("DEBUG", "true")
    yield
    # Cleanup happens automatically
```

### JavaScript Fixtures

```javascript
// Mock setup
beforeEach(() => {
  jest.clearAllMocks();
});

// Common test data
const mockUser = {
  id: 1,
  name: 'Test User',
  email: 'test@example.com'
};

// API mocking
jest.mock('./api', () => ({
  fetchData: jest.fn(() => Promise.resolve({ data: [] }))
}));
```

## Running Tests After Bootstrap

Once infrastructure is created, verify it works:

```bash
# Python
python -m pytest -v

# JavaScript (Jest)
npm test

# JavaScript (Vitest)
npm run test

# Go
go test ./...

# Rust
cargo test

# C#
dotnet test
```

**Expected output:**
```
======================== test session starts =========================
collected 3 items

tests/test_smoke.py::test_pytest_works PASSED                  [ 33%]
tests/test_smoke.py::test_fixtures_available PASSED            [ 66%]
tests/test_smoke.py::test_async_works PASSED                   [100%]

========================= 3 passed in 0.12s ==========================
```

## Interpreting Output

### Success Indicators
- ✅ "X passed" in output
- ✅ Exit code 0
- ✅ No "ModuleNotFoundError" or "ImportError"
- ✅ Fixtures load without errors

### Failure Indicators
- ❌ "No tests collected" → Test files not in correct location
- ❌ "ModuleNotFoundError: No module named 'pytest'" → pytest not installed
- ❌ "fixture 'X' not found" → conftest.py not loaded or has errors
- ❌ Exit code non-zero → Tests failed or infrastructure broken

**If you see failures:**
1. Read the error message carefully
2. Fix the specific error (missing import, wrong path, etc.)
3. Re-run tests
4. If 3 attempts fail → report `RESULT: blocked` with the error message

## Quality Checklist

- [ ] pytest (or appropriate framework) added to dependencies
- [ ] conftest.py created with at least 2 fixtures
- [ ] pytest.ini or equivalent config file created
- [ ] At least 1 smoke test written
- [ ] Tests run successfully (`pytest` command exits 0)
- [ ] Output shows "X passed" not "no tests collected"

## Final Note

**Your job is to remove blockers, not achieve perfection.** The infrastructure you create doesn't need to be comprehensive — it needs to be functional. Write the minimum viable test setup, verify it works with a smoke test, then move on. Future developers and testers will expand it as needed.
