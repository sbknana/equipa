# Go Best Practices

## Critical
- Always check errors. Never use `_` to discard errors unless explicitly documented why.
- Use `errors.Is()` and `errors.As()` for error comparison, not `==`.
- Wrap errors with context: `fmt.Errorf("doing X: %w", err)`.
- Always pass `context.Context` as first parameter to functions that do I/O.
- Close resources with `defer` immediately after creation.

## High
- Use `struct{}` for signal channels, not `bool`.
- Use `sync.WaitGroup` or `errgroup.Group` for goroutine coordination.
- Never start goroutines without a way to stop them (context cancellation or done channel).
- Use `context.WithTimeout` or `context.WithCancel` for all async operations.
- Prefer table-driven tests.

## Style
- Use short, descriptive names. Receivers are 1-2 letters (`s` for server, `c` for client).
- Keep functions short. If a function needs a comment explaining what it does, split it.
- Use `go vet`, `staticcheck`, and `golangci-lint`.
- Group imports: stdlib, blank line, external, blank line, internal.

## Concurrency
- Never share memory between goroutines without synchronization.
- Prefer channels for communication, mutexes for protecting shared state.
- Use `sync.Once` for one-time initialization.
- Always handle the default case in select with channels.

## Testing
- Use `testing.T`, not assertion libraries (keep it stdlib).
- Use `t.Helper()` in test helpers.
- Use `t.Parallel()` for independent tests.
- Use `testdata/` directory for test fixtures.
