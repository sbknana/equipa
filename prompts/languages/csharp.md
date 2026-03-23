# C# Language Guidelines

## Async/Await
- Use `async Task` (not `async void`) for all async methods except event handlers.
- Always `await` tasks. Never use `.Result` or `.Wait()` — they cause deadlocks in UI and ASP.NET contexts.
- Use `ConfigureAwait(false)` in library code to avoid capturing the synchronization context.
- Use `Task.WhenAll()` for concurrent operations instead of sequential `await` in loops.
- Pass `CancellationToken` through async call chains and check for cancellation in long-running operations.

## IDisposable & Resource Management
- Implement `IDisposable` for types that hold unmanaged resources or other disposables.
- Always use `using` statements (or `using` declarations in C# 8+) for disposable objects.
- Follow the dispose pattern: protected `Dispose(bool disposing)` with a finalizer only when directly holding unmanaged resources.
- Never call methods on disposed objects — check `ObjectDisposedException` in public methods after disposal.

## LINQ
- Use LINQ for readable data transformations. Avoid LINQ in performance-critical hot paths — benchmark first.
- Prefer method syntax (`.Where()`, `.Select()`) over query syntax for consistency.
- Avoid multiple enumeration of `IEnumerable<T>` — materialise with `.ToList()` or `.ToArray()` when the source will be iterated more than once.
- Use `Any()` instead of `Count() > 0` for existence checks.
- Prefer `FirstOrDefault` with a predicate over `Where().FirstOrDefault()`.

## Nullable Reference Types
- Enable nullable reference types (`<Nullable>enable</Nullable>`) in all projects.
- Annotate all reference type parameters and return types with `?` when null is valid.
- Use the null-forgiving operator `!` sparingly and only when you can prove the value is non-null.
- Prefer pattern matching (`is not null`, `is { Property: var p }`) over null checks.

## Error Handling
- Catch specific exceptions, never bare `catch (Exception)` without re-throwing or logging.
- Use `throw;` (not `throw ex;`) to preserve the original stack trace.
- Define custom exception types for domain-specific errors. Include inner exceptions.
- Use `ExceptionDispatchInfo.Capture(ex).Throw()` when rethrowing across async boundaries.

## Naming & Style
- Follow Microsoft naming guidelines: PascalCase for public members, camelCase with `_` prefix for private fields.
- Use `var` when the type is obvious from the right-hand side. Use explicit types when clarity matters.
- Prefer `string` over `String`, `int` over `Int32` (language keywords over framework types).
- Use `nameof()` for parameter names in exceptions and property change notifications.

## Dependency Injection
- Register services with appropriate lifetimes: `Singleton`, `Scoped`, `Transient`.
- Never resolve scoped services from a singleton — this causes captive dependency issues.
- Use constructor injection. Avoid service locator patterns (`IServiceProvider.GetService` in business logic).
- Use `IOptions<T>` / `IOptionsSnapshot<T>` for configuration injection.

## Testing
- Use xUnit, NUnit, or MSTest consistently within a project.
- Use `[Theory]` / `[InlineData]` (xUnit) for parameterised tests.
- Mock dependencies with interfaces and a mocking framework (Moq, NSubstitute).
- Use `FluentAssertions` for readable assertion messages.
- Test async code with `async Task` test methods, not `.Result` blocking.
