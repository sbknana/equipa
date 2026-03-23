# Python Best Practices

## Critical
- Never use mutable default arguments (`def f(items=[])`). Use `None` and initialize inside.
- Never use bare `except:`. Always catch specific exceptions or at minimum `except Exception`.
- Use `with` statements for all file/resource operations (context managers).
- Never use `eval()` or `exec()` with untrusted input.

## High
- Use type hints on all function signatures. Return types matter.
- Prefer `pathlib.Path` over `os.path` for file operations.
- Use f-strings for formatting, not `%` or `.format()`.
- Use `dataclasses` or `NamedTuple` instead of plain dicts for structured data.
- Use `logging` module, not `print()`, for production code.

## Style
- Follow PEP 8. Max line length 120 (not 79 — pragmatic).
- Use snake_case for functions/variables, PascalCase for classes.
- Prefer list/dict/set comprehensions over map/filter when readable.
- Use `isinstance()` not `type()` for type checking.

## Testing
- Use `pytest` conventions: `test_` prefix, fixtures, parametrize.
- Use `unittest.mock.patch` for mocking, not monkeypatching globals.
- Assert specific values, not just truthiness.

## Async
- Use `asyncio` for I/O-bound concurrency. Never mix sync blocking calls in async code.
- Use `asyncio.gather()` for parallel async operations.
- Always use `async with` for async context managers.
