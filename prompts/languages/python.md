# Python Language Guidelines

## Style & Formatting
- Follow PEP 8 conventions. Use 4-space indentation, snake_case for functions and variables, PascalCase for classes.
- Maximum line length: 88 characters (Black formatter default) or 79 (PEP 8 strict).
- Use f-strings for string formatting over `.format()` or `%`.

## Type Hints
- Add type hints to all function signatures: parameters and return types.
- Use `from __future__ import annotations` for deferred evaluation in Python 3.9 and earlier.
- Prefer `list[str]` over `List[str]` (Python 3.9+). Use `X | None` over `Optional[X]` (Python 3.10+).
- Use `TypeAlias` or `type` statement for complex types.

## Common Bugs to Avoid
- **Mutable default arguments:** Never use `def f(items=[])`. Use `def f(items=None)` and assign inside the function body.
- **Late binding closures:** Variables in closures bind at call time, not definition time. Use default argument binding: `lambda x=x: x`.
- **Bare except:** Never use `except:` or `except Exception:` without logging or re-raising. Catch specific exceptions.
- **String-based path manipulation:** Use `pathlib.Path` instead of `os.path.join` and string concatenation.

## Async Patterns
- Use `async def` with `await` for I/O-bound operations.
- Never call blocking I/O (`time.sleep`, synchronous HTTP) inside async functions — use `asyncio.sleep`, `aiohttp`, or `asyncio.to_thread`.
- Use `asyncio.gather()` for concurrent coroutines, not sequential awaits in a loop.
- Always handle `asyncio.CancelledError` in long-running tasks.

## Testing (pytest)
- Use `pytest` with fixtures, not `unittest.TestCase`.
- Use `@pytest.mark.parametrize` for data-driven tests instead of repeated test functions.
- Use `tmp_path` fixture for temporary files, not `tempfile` directly.
- Prefer `pytest.raises(ExactException)` over try/except in tests.
- Use `monkeypatch` for patching — avoid `unittest.mock.patch` decorators when possible.

## Error Handling
- Use specific exception types. Define custom exceptions for domain errors.
- Always use `finally:` or context managers (`with`) for resource cleanup.
- Log exceptions with `logger.exception()` to capture tracebacks.
- Use `raise ... from err` to preserve exception chains.

## Imports
- Group imports: stdlib, third-party, local — separated by blank lines.
- Use absolute imports over relative imports in application code.
- Never use wildcard imports (`from module import *`).
