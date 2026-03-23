# Go Language Guidelines

## Error Handling
- Always check returned errors. Never use `_` to discard errors unless the function is documented as infallible.
- Wrap errors with context using `fmt.Errorf("operation failed: %w", err)` — use `%w` to enable `errors.Is` and `errors.As` unwrapping.
- Return errors to the caller instead of logging and continuing. Let the caller decide how to handle them.
- Use sentinel errors (`var ErrNotFound = errors.New("not found")`) for expected error conditions. Use custom error types for errors that carry additional data.
- Never panic in library code. Reserve `panic` for truly unrecoverable programmer errors.

## Goroutine Safety
- **Goroutine leaks:** Every `go func()` must have a clear exit path. Use `context.Context` cancellation or `done` channels to signal shutdown.
- Always pass `context.Context` as the first parameter to functions that do I/O or may block.
- Use `sync.WaitGroup` or `errgroup.Group` to wait for goroutine completion.
- Never share variables between goroutines without synchronization — use channels or `sync.Mutex`.
- Prefer channels for communication, mutexes for state protection.

## Context.Context
- Thread `context.Context` through all call chains. Never store it in structs.
- Use `context.WithTimeout` or `context.WithDeadline` for operations that should not run indefinitely.
- Check `ctx.Err()` or `ctx.Done()` in loops and before expensive operations.
- Create child contexts with `context.WithValue` only for request-scoped data (trace IDs, auth), never for function parameters.

## Defer Patterns
- Use `defer` for cleanup (closing files, releasing locks, closing connections).
- Remember that `defer` evaluates arguments immediately but executes the call on function return.
- Be cautious with `defer` inside loops — deferred calls accumulate until the function returns. Use an inner function or explicit close.
- Use `defer rows.Close()` immediately after obtaining database query results.

## Naming & Style
- Use short, descriptive names. Single-letter variables are acceptable in small scopes (loop counters, receivers).
- Exported names are PascalCase, unexported are camelCase.
- Interface names use the `-er` suffix for single-method interfaces (`Reader`, `Writer`, `Closer`).
- Package names are lowercase, single-word, and do not repeat the import path.

## Testing
- Use table-driven tests with `t.Run()` subtests for comprehensive coverage.
- Use `t.Helper()` in test utility functions to improve error reporting.
- Use `t.Parallel()` for tests that can run concurrently.
- Use `testify/assert` or stdlib comparisons consistently — do not mix approaches.
- Use `t.TempDir()` for temporary test directories (auto-cleaned).

## Performance
- Use `strings.Builder` for string concatenation in loops, not `+=`.
- Prefer `make([]T, 0, expectedLen)` when the slice capacity is known.
- Use `sync.Pool` for frequently allocated and discarded objects.
- Profile before optimizing — use `go test -bench` and `pprof`.

## Database
- Always use parameterized queries. Never interpolate user input into SQL strings.
- Use `defer rows.Close()` immediately after `db.Query()`.
- Check `rows.Err()` after the scan loop completes.
- Use transactions (`db.BeginTx`) for multi-statement operations.
