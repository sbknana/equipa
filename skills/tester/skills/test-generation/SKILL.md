---
name: test-generation
description: >
  Write effective tests when the project has no existing tests or when new code lacks test
  coverage. Use when the developer didn't write tests, when you need to verify new functionality,
  or when existing tests don't cover the changed code. Triggers: no tests, write tests, add tests,
  test coverage, need tests for.
allowed-tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Test Generation

## Core Principle

**Write the fewest tests that catch the most bugs.** One test per behavior, not one test per
line of code. Test the contract (inputs → outputs), not the implementation.

## When to Use

- Developer's code has no tests
- Existing tests don't cover the changed code
- The task explicitly requires writing tests
- You need to verify a bug fix

## When NOT to Use

- Tests already exist and pass (just report results)
- The code is trivial configuration (constants, env vars)
- The code is auto-generated (migrations, protobuf stubs)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "100% coverage is needed" | Diminishing returns past 80%. Test behavior, not lines. | Cover critical paths and edge cases only |
| "I'll write tests for every function" | Internal functions tested via their callers | Test public API surface only |
| "I'll mock everything" | Over-mocking makes tests brittle and meaningless | Mock external dependencies only (DB, HTTP, filesystem) |
| "I'll test the happy path only" | Bugs live in edge cases | Happy path + 1 error case + 1 boundary case minimum |
| "Tests are optional for this change" | No. Tests are how we verify correctness. | Write at least 1 test per changed behavior |

## What to Test (Priority Order)

### Must Test (Always)
1. **Happy path** — Does it work with valid input?
2. **Error handling** — Does it fail gracefully with bad input?
3. **Edge cases** — Empty input, null, zero, max values, boundary conditions
4. **State changes** — Database writes, file writes, API calls (mock these)

### Should Test (If Time Permits)
5. **Authorization** — Does it check permissions?
6. **Concurrent access** — Race conditions on shared state
7. **Idempotency** — Does calling it twice cause problems?

### Skip Testing
- Getters/setters with no logic
- Framework boilerplate
- Third-party library behavior
- CSS/styling

## Test Structure Pattern

Every test follows **Arrange-Act-Assert** (AAA):

```
1. ARRANGE: Set up test data and dependencies
2. ACT: Call the function/endpoint being tested
3. ASSERT: Verify the result matches expectations
```

### Python (pytest)
```python
def test_create_user_with_valid_data():
    # Arrange
    user_data = {"name": "Test User", "email": "test@example.com"}

    # Act
    result = create_user(user_data)

    # Assert
    assert result.name == "Test User"
    assert result.id is not None

def test_create_user_with_missing_email():
    # Arrange
    user_data = {"name": "Test User"}

    # Act & Assert
    with pytest.raises(ValidationError):
        create_user(user_data)
```

### JavaScript (Jest)
```javascript
describe('createUser', () => {
  test('creates user with valid data', async () => {
    // Arrange
    const userData = { name: 'Test User', email: 'test@example.com' };

    // Act
    const result = await createUser(userData);

    // Assert
    expect(result.name).toBe('Test User');
    expect(result.id).toBeDefined();
  });

  test('throws on missing email', async () => {
    // Arrange
    const userData = { name: 'Test User' };

    // Act & Assert
    await expect(createUser(userData)).rejects.toThrow('email is required');
  });
});
```

### Go
```go
func TestCreateUser_ValidData(t *testing.T) {
    // Arrange
    data := UserData{Name: "Test User", Email: "test@example.com"}

    // Act
    result, err := CreateUser(data)

    // Assert
    if err != nil {
        t.Fatalf("unexpected error: %v", err)
    }
    if result.Name != "Test User" {
        t.Errorf("got name %q, want %q", result.Name, "Test User")
    }
}

func TestCreateUser_MissingEmail(t *testing.T) {
    data := UserData{Name: "Test User"}
    _, err := CreateUser(data)
    if err == nil {
        t.Fatal("expected error, got nil")
    }
}
```

## Mocking Strategy

### What to Mock
- Database queries and writes
- HTTP calls to external services
- File system operations
- Time-dependent operations (use fixed timestamps)
- Environment variables

### What NOT to Mock
- The function you're testing
- Pure utility functions (string parsing, math, etc.)
- Data structures and models

### Mock Placement
```
Python: pytest fixtures, unittest.mock.patch, monkeypatch
JS/TS:  jest.mock(), jest.spyOn(), manual __mocks__ directory
Go:     Interface-based injection, test doubles
Rust:   mockall crate, trait-based test doubles
C#:     Moq, NSubstitute, dependency injection
```

## Test File Conventions

Follow the project's existing convention. If none exists, use:

| Language | Convention | Location |
|----------|-----------|----------|
| Python | `test_<module>.py` | `tests/` directory or alongside source |
| JS/TS | `<module>.test.ts` | `__tests__/` or alongside source |
| Go | `<module>_test.go` | Same directory as source (required by Go) |
| Rust | `#[cfg(test)]` mod in source or `tests/` | Same file or `tests/` for integration |
| C# | `<Module>Tests.cs` | Separate `*.Tests` project |

## Quality Checklist

After writing tests:
- [ ] Tests pass when run
- [ ] Happy path is covered
- [ ] At least 1 error/edge case is covered
- [ ] External dependencies are mocked, not called
- [ ] Test names describe the behavior being tested
- [ ] No test depends on another test's state (each test is independent)
- [ ] Test data is self-contained (no reliance on external DB state)

## References

- See `references/mocking-patterns.md` for framework-specific mocking guides
