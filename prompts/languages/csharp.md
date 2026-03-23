# C# Best Practices

## Critical
- Use `async/await` properly. Never use `.Result` or `.Wait()` on async methods (deadlock risk).
- Always dispose IDisposable resources with `using` statements.
- Never catch `Exception` without rethrowing or logging — silent swallowing hides bugs.
- Use nullable reference types (`#nullable enable`) and respect nullability annotations.

## High
- Use `record` types for immutable value objects.
- Prefer LINQ for collection operations but avoid over-chaining (readability > cleverness).
- Use `string.IsNullOrWhiteSpace()` not `string.IsNullOrEmpty()`.
- Use `CancellationToken` for all async operations that may need cancellation.
- Use `ILogger<T>` for structured logging via dependency injection.

## Style
- PascalCase for public members, _camelCase for private fields.
- Use file-scoped namespaces (`namespace X;` not `namespace X { }`).
- Use target-typed `new()` when type is obvious from context.
- Prefer pattern matching (`is`, `switch` expressions) over type casting.

## Entity Framework (when applicable)
- Use `AsNoTracking()` for read-only queries.
- Use `Include()` / `ThenInclude()` to avoid N+1 queries.
- Never call `SaveChanges()` in a loop — batch operations.
- Use migrations, not manual SQL, for schema changes.

## Testing
- Use xUnit or NUnit with `[Fact]`/`[Theory]` attributes.
- Use `Moq` or `NSubstitute` for mocking interfaces.
- Test through public API, not internal implementation.
