---
name: bootstrap-tests
description: >
  Create test infrastructure from scratch when none exists. Sets up pytest config, conftest.py,
  adds test dependencies, creates common fixtures, and ensures tests can run. Use when the project
  has no test infrastructure, when conftest.py is missing, or when test dependencies are not installed.
  Triggers: no tests found, pytest not configured, no conftest, missing test dependencies, cannot run tests.
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

**Your job is to CREATE test infrastructure, not just report that it's missing.**
If tests don't exist, BUILD the scaffolding so they can exist. Never output "no tests to run" as a blocker — that's what you're here to fix.

## When to Use

- Project has no test files at all
- Test files exist but no test runner configuration
- `pytest` command fails with "No module named pytest"
- `conftest.py` doesn't exist but tests need shared fixtures
- Tests exist but dependencies aren't declared in requirements/pyproject

## When NOT to Use

- Tests already run successfully (use framework-detection instead)
- Tests exist and are configured (just run them)
- You're only asked to run tests, not create infrastructure

## Decision Tree: What to Bootstrap

```
START: "Run tests"
├─ tests/ directory exists?
│  ├─ YES → test_*.py files inside?
│  │  ├─ YES → Run `pytest tests/`
│  │  │  ├─ ModuleNotFoundError: No module named 'pytest'
│  │  │  │  └─ Bootstrap: Step 2 (Install pytest)
│  │  │  ├─ ImportError: cannot import project code
│  │  │  │  └─ Run `pip install -e .` then retry
│  │  │  │     └─ Still fails? Add to PYTHONPATH, then retry
│  │  │  ├─ Tests run but fail → NOT a bootstrap issue
│  │  │  │  └─ EXIT: Report test failures to developer
│  │  │  └─ Tests pass → SUCCESS
│  │  │     └─ EXIT: Report results
│  │  └─ NO (empty tests/ dir) → Bootstrap: Step 4-6 (fixtures + sample test)
│  └─ NO → Does *.py code exist in src/?
│     ├─ YES → Bootstrap: Step 1-6 (full setup)
│     └─ NO → EXIT: Report "No code to test"
```

Use this tree on EVERY "run tests" task. Never skip straight to "no tests found" — check each branch.

## Bootstrap Process

### Step 1: Detect Project Structure (1 turn)

Check what already exists:

```bash
# Check for existing test infrastructure
ls -la conftest.py pytest.ini pyproject.toml setup.py setup.cfg 2>/dev/null
find . -name "*test*.py" -o -name "*spec*.py" | head -10
grep -r "import pytest\|import unittest\|from unittest" --include="*.py" | head -5
```

Identify the project type:
- **Modern Python (pyproject.toml exists):** Add pytest to `[tool.pytest.ini_options]` and `[project.optional-dependencies]`
- **Classic Python (requirements.txt exists):** Create/update `requirements-dev.txt` or `requirements-test.txt`
- **Poetry (poetry.lock exists):** Run `poetry add --group dev pytest pytest-cov pytest-asyncio`
- **No dependency management:** Create `requirements-test.txt` with core test dependencies

### Step 2: Install Test Framework (1 turn)

**Priority order — do the FIRST one that matches:**

#### For pyproject.toml projects:
```bash
# Add pytest to pyproject.toml [project.optional-dependencies]
# Then install:
pip install -e ".[test]"
```

#### For requirements-based projects:
```bash
# Create requirements-test.txt if missing
cat > requirements-test.txt << 'EOF'
pytest>=8.0.0
pytest-cov>=4.1.0
pytest-asyncio>=0.23.0
pytest-mock>=3.12.0
EOF

pip install -r requirements-test.txt
```

#### For Poetry projects:
```bash
poetry add --group dev pytest pytest-cov pytest-asyncio pytest-mock
```

#### For pipenv projects:
```bash
pipenv install --dev pytest pytest-cov pytest-asyncio pytest-mock
```

#### For conda environments:
```bash
conda install pytest pytest-cov pytest-asyncio pytest-mock
```

**Core test dependencies to always include:**
- `pytest` — the test runner
- `pytest-cov` — coverage reporting
- `pytest-asyncio` — async test support (if project uses asyncio)
- `pytest-mock` — mocking utilities

### Step 3: Create pytest Configuration (1 turn)

Create the most appropriate config for the project:

#### Option A: pyproject.toml (preferred for modern projects)

If `pyproject.toml` exists, add:

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
python_files = ["test_*.py", "*_test.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = [
    "-ra",
    "--strict-markers",
    "--strict-config",
    "--cov=.",
    "--cov-report=term-missing:skip-covered",
    "--cov-report=html",
    "--cov-report=xml",
]
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: marks tests as integration tests",
    "unit: marks tests as unit tests",
]
```

#### Option B: pytest.ini (if no pyproject.toml)

```ini
[pytest]
minversion = 8.0
testpaths = tests
python_files = test_*.py *_test.py
python_classes = Test*
python_functions = test_*
addopts =
    -ra
    --strict-markers
    --strict-config
    --cov=.
    --cov-report=term-missing:skip-covered
    --cov-report=html
    --cov-report=xml
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    integration: marks tests as integration tests
    unit: marks tests as unit tests
```

### Step 4: Create conftest.py (1 turn)

Create `tests/conftest.py` with common fixtures:

```python
"""Shared test fixtures and configuration."""
import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest


# ============================================================================
# Directory Fixtures
# ============================================================================

@pytest.fixture
def tmp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def sample_file(tmp_dir: Path) -> Path:
    """Create a sample text file for testing."""
    file_path = tmp_dir / "sample.txt"
    file_path.write_text("Sample content for testing\n")
    return file_path


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a clean environment with no sensitive env vars."""
    sensitive_vars = [
        "API_KEY", "SECRET_KEY", "PASSWORD", "TOKEN",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ]
    for var in sensitive_vars:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Provide a mock environment with test values."""
    test_env = {
        "TEST_MODE": "true",
        "LOG_LEVEL": "DEBUG",
    }
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)
    return test_env


# ============================================================================
# Database Fixtures (add only if project uses DB)
# ============================================================================

@pytest.fixture
def db_session():
    """Create a test database session (customize for your ORM)."""
    # Example for SQLAlchemy:
    # engine = create_engine("sqlite:///:memory:")
    # Base.metadata.create_all(engine)
    # Session = sessionmaker(bind=engine)
    # session = Session()
    # yield session
    # session.close()
    pytest.skip("Database fixtures not yet implemented")


# ============================================================================
# Mock API Fixtures
# ============================================================================

@pytest.fixture
def mock_http_response():
    """Create a mock HTTP response object."""
    class MockResponse:
        def __init__(self, json_data: dict, status_code: int = 200):
            self.json_data = json_data
            self.status_code = status_code
            self.text = str(json_data)

        def json(self):
            return self.json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    return MockResponse


# ============================================================================
# Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_data() -> dict:
    """Provide sample test data."""
    return {
        "id": 1,
        "name": "Test Item",
        "active": True,
        "tags": ["test", "sample"],
    }
```

### Step 5: Create Sample Test (1 turn)

Create `tests/test_example.py` to verify the setup works:

```python
"""Example test to verify test infrastructure is working."""
import pytest
from pathlib import Path


def test_basic_assertion():
    """Verify basic assertions work."""
    assert 1 + 1 == 2


def test_fixture_usage(tmp_dir: Path):
    """Verify fixtures are working."""
    assert tmp_dir.exists()
    assert tmp_dir.is_dir()

    # Create a test file
    test_file = tmp_dir / "test.txt"
    test_file.write_text("Hello, tests!")

    assert test_file.exists()
    assert test_file.read_text() == "Hello, tests!"


def test_sample_data_fixture(sample_data: dict):
    """Verify data fixtures work."""
    assert sample_data["id"] == 1
    assert sample_data["name"] == "Test Item"
    assert sample_data["active"] is True


@pytest.mark.parametrize("input,expected", [
    (1, 2),
    (2, 4),
    (3, 6),
])
def test_parametrized(input: int, expected: int):
    """Verify parametrized tests work."""
    assert input * 2 == expected


@pytest.mark.slow
def test_slow_operation():
    """Example of marking slow tests."""
    import time
    time.sleep(0.1)  # Simulate slow operation
    assert True
```

### Step 6: Verify and Run (1 turn)

```bash
# Verify pytest is installed
python -m pytest --version

# Run tests with verbose output
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov --cov-report=term-missing
```

## Language-Specific Bootstrapping

### JavaScript/TypeScript Projects

For Node.js projects without test infrastructure:

```bash
# Install Jest
npm install --save-dev jest @types/jest

# Create jest.config.js
cat > jest.config.js << 'EOF'
module.exports = {
  testEnvironment: 'node',
  testMatch: ['**/__tests__/**/*.test.js', '**/?(*.)+(spec|test).js'],
  collectCoverageFrom: ['src/**/*.js', '!src/**/*.test.js'],
  coverageThreshold: {
    global: {
      branches: 70,
      functions: 70,
      lines: 70,
      statements: 70
    }
  }
};
EOF

# Add test script to package.json
npm pkg set scripts.test="jest"

# Create sample test
mkdir -p __tests__
cat > __tests__/example.test.js << 'EOF'
describe('Example Test Suite', () => {
  test('basic assertion', () => {
    expect(1 + 1).toBe(2);
  });
});
EOF
```

### Go Projects

```bash
# Go has built-in testing - just create test files
cat > example_test.go << 'EOF'
package main

import "testing"

func TestBasicAssertion(t *testing.T) {
    result := 1 + 1
    if result != 2 {
        t.Errorf("Expected 2, got %d", result)
    }
}
EOF

# Run tests
go test ./...
```

### Rust Projects

```bash
# Create tests directory
mkdir -p tests

cat > tests/integration_test.rs << 'EOF'
#[test]
fn test_basic_assertion() {
    assert_eq!(1 + 1, 2);
}
EOF

# Run tests
cargo test
```

### C# Projects

```bash
# For .NET projects, tests are typically in separate test projects
# Create a test project
dotnet new xunit -n ProjectName.Tests

# Add reference to main project
cd ProjectName.Tests
dotnet add reference ../ProjectName/ProjectName.csproj

# Create sample test
cat > UnitTest1.cs << 'EOF'
using Xunit;

namespace ProjectName.Tests;

public class ExampleTests
{
    [Fact]
    public void TestBasicAssertion()
    {
        Assert.Equal(2, 1 + 1);
    }

    [Theory]
    [InlineData(1, 2)]
    [InlineData(2, 4)]
    [InlineData(3, 6)]
    public void TestParameterized(int input, int expected)
    {
        Assert.Equal(expected, input * 2);
    }
}
EOF

# Run tests
dotnet test
```

## Common Fixtures Library

When creating `conftest.py`, include these fixtures based on what the project needs:

### File System Operations
- `tmp_dir` — temporary directory
- `sample_file` — pre-populated test file
- `mock_file_structure` — nested directory tree

### Environment & Configuration
- `clean_env` — isolated environment
- `mock_env` — test environment variables
- `mock_config` — test configuration object

### Database Operations (if applicable)
- `db_session` — database session
- `db_transaction` — rollback after test
- `sample_db_data` — pre-populated test data

### HTTP/API Mocking (if applicable)
- `mock_http_response` — mock HTTP response
- `mock_api_client` — mock API client
- `requests_mock` — intercept requests library calls

### Async Operations (if project uses asyncio)
- `event_loop` — pytest-asyncio provides this
- `async_client` — mock async HTTP client

## Troubleshooting

### "ModuleNotFoundError: No module named 'pytest'"

```bash
# Verify pip is using the correct Python
which python
which pip

# Install pytest
pip install pytest

# If that fails, use python -m pip
python -m pip install pytest
```

### Virtual Environment Issues

```bash
# Check if you're in a virtual environment
echo $VIRTUAL_ENV

# If not activated, find and activate it
# For venv:
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate  # Windows

# For conda:
conda activate <env-name>

# Install dependencies in the activated environment
pip install -r requirements-test.txt
```

### "No tests collected"

```bash
# Check test discovery patterns
pytest --collect-only

# Verify test file naming
ls tests/test_*.py tests/*_test.py

# Check pytest configuration
pytest --version && pytest --help | grep testpaths
```

### "Import errors in tests"

```bash
# Install package in editable mode
pip install -e .

# Or add src to PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)/src"
pytest tests/
```

### Tests found but fixtures fail

```bash
# Verify conftest.py is in the right location
ls tests/conftest.py

# Check for syntax errors
python -m py_compile tests/conftest.py

# Run pytest with verbose fixture info
pytest tests/ --fixtures
```

### Multiple or non-standard test directories

```bash
# If tests are scattered across the project
# Update pytest config to include all test paths
# In pyproject.toml:
[tool.pytest.ini_options]
testpaths = ["tests", "integration_tests", "unit_tests", "src"]

# Or in pytest.ini:
[pytest]
testpaths = tests integration_tests unit_tests src

# Discover all test files in project
find . -name "test_*.py" -o -name "*_test.py" | grep -v venv | grep -v ".tox"
```

## Quality Checklist

- [ ] Test framework installed and version verified
- [ ] Configuration file created (pytest.ini or pyproject.toml)
- [ ] `conftest.py` created with at least 3 common fixtures
- [ ] Sample test file created and passes
- [ ] `pytest --collect-only` shows tests are discovered
- [ ] `pytest` command runs without errors
- [ ] Coverage reporting configured
- [ ] Test dependencies added to requirements/pyproject

## Output Format

When bootstrap is complete, provide this summary:

```
TEST INFRASTRUCTURE BOOTSTRAPPED

Framework: pytest 8.x.x
Config: [pytest.ini | pyproject.toml | setup.cfg]
Fixtures: [list fixture names from conftest.py]
Sample Tests: [number] tests in tests/test_example.py

RUN TESTS:
  pytest tests/ -v
  pytest tests/ --cov

NEXT STEPS:
  1. [Any remaining manual steps]
  2. [Project-specific fixture recommendations]
```

## Anti-Patterns to Avoid

| Wrong Approach | Why It's Wrong | Correct Approach |
|---------------|---------------|------------------|
| "No tests exist, reporting blocked" | Your job is to CREATE infrastructure | Bootstrap conftest.py and pytest.ini, create sample test |
| Install every pytest plugin | Bloats dependencies unnecessarily | Install only pytest-cov, pytest-asyncio (if needed), pytest-mock |
| Create fixtures for everything upfront | Unknown what tests will need | Create 3-5 common fixtures, expand as needed |
| Skip dependency declaration | Tests won't run in CI/other machines | Always add pytest to requirements/pyproject |
| Use unittest instead of pytest | Harder to maintain, less powerful | Always use pytest for new test infrastructure |

## Handling Import Errors After Bootstrap

After creating test infrastructure, tests may fail with import errors. Handle systematically:

### Pattern 1: "ModuleNotFoundError" for project code

```bash
# Check project structure
ls -la src/ lib/ *.py

# Install project in editable mode
pip install -e .

# If no setup.py/pyproject.toml with [project], add src to PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)/src:$(pwd)"
pytest tests/ -v
```

### Pattern 2: Missing test dependencies in existing tests

```bash
# Scan test files for imports
grep -h "^import\|^from" tests/*.py | sort -u

# Install missing packages
pip install <missing-package>

# Add to requirements-test.txt for future runs
echo "<missing-package>" >> requirements-test.txt
```

### Pattern 3: Tests import from wrong locations

```python
# BAD: Assumes tests run from project root
from src.module import function  # Breaks if run from tests/

# GOOD: Use proper package imports
from myproject.module import function  # Works from any directory
```

Fix by ensuring package is installed (`pip install -e .`) or adjusting imports.

### Pattern 4: Circular imports or initialization issues

```bash
# Run single test file to isolate the issue
pytest tests/test_specific.py -v

# Check if __init__.py files exist where needed
find . -type d -name tests -o -name src | xargs -I {} ls {}/__init__.py 2>&1 | grep "No such file"

# Create missing __init__.py files
touch tests/__init__.py src/__init__.py
```

## Real-World EQUIPA Context

When working in EQUIPA multi-agent system, you'll encounter these scenarios:

### Scenario: Developer wrote code but no tests

**Action:** Don't just report "no tests exist". Create test infrastructure AND write a basic smoke test for the new code:

```python
# tests/test_new_feature.py
"""Smoke tests for newly implemented feature."""
import pytest

def test_new_feature_imports():
    """Verify new feature code can be imported."""
    from myproject.new_feature import main_function
    assert callable(main_function)

def test_new_feature_basic():
    """Verify new feature doesn't crash on basic input."""
    from myproject.new_feature import main_function
    # Use safe test data
    result = main_function(test_input={"key": "value"})
    assert result is not None
```

### Scenario: Tests exist but pytest not configured

**Action:** Add configuration without breaking existing tests:

1. Run tests without config first: `python -m pytest tests/ --collect-only`
2. Note discovered tests
3. Add minimal config that preserves discovery
4. Re-run and verify same tests are found

### Scenario: Multiple test frameworks detected

```bash
# Check what's actually being used
grep -r "import unittest\|import pytest\|import nose" tests/

# Prefer pytest even if unittest-style tests exist
# pytest runs unittest.TestCase classes natively
pip install pytest
pytest tests/ -v  # Will run both styles
```

Don't rewrite unittest tests to pytest format — pytest runs them as-is.

## Escape Hatch

If you've completed all 6 steps and tests still won't run due to complex project-specific issues (e.g., missing system dependencies, complex build process), then output:

```
RESULT: blocked
SUMMARY: Test infrastructure created but tests require [specific blocker]
BLOCKERS: [Exact description of what's preventing test execution]
```

But this should be RARE. 95% of projects can be bootstrapped successfully.
