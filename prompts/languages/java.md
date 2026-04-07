# Java Language Guidelines

## Null Safety
- Use `Optional<T>` for return values that may be absent. Never return `null` from methods that return collections — return empty collections instead.
- Use `@Nullable` and `@NonNull` annotations from `jakarta.annotation` or `org.jetbrains.annotations` to document null contracts.
- Prefer `Objects.requireNonNull(param, "message")` at method entry points for fail-fast validation.
- Never use `Optional` as a method parameter or field type — it is designed for return values only.

## Error Handling
- Catch specific exceptions, never bare `catch (Exception e)` without re-throwing or logging.
- Use `throw new CustomException("context", cause)` to preserve exception chains.
- Close resources with try-with-resources (`try (var r = open())`) — never rely on `finally` for `AutoCloseable` resources.
- Define custom exceptions extending `RuntimeException` for programming errors, `Exception` for recoverable conditions.
- Log exceptions with `logger.error("context", e)` — include the exception as the last argument to capture the stack trace.

## Concurrency
- Use `ExecutorService` and `CompletableFuture` over raw `Thread` creation.
- Prefer `ConcurrentHashMap` over `Collections.synchronizedMap`.
- Use `volatile` for simple flags, `AtomicReference`/`AtomicInteger` for lock-free updates, `synchronized` blocks only when atomic operations are insufficient.
- Always set thread names for debugging: use `ThreadFactory` with meaningful names.
- Use virtual threads (Java 21+) for I/O-bound work instead of platform threads.

## Collections & Streams
- Use the diamond operator (`<>`) and `var` for local variables with obvious types.
- Prefer `List.of()`, `Map.of()`, `Set.of()` for immutable collections.
- Use streams for data transformation pipelines. Avoid streams for simple iterations where a for-loop is clearer.
- Never modify a collection while iterating — use `Iterator.remove()`, `removeIf()`, or collect into a new collection.

## Dependency Injection (Spring)
- Use constructor injection, not field injection (`@Autowired` on fields).
- Define beans with appropriate scope: singleton (default), request, prototype.
- Use `@Value` with `${property:default}` for configuration — never hardcode config values.
- Use `@Transactional` on service methods that need atomicity. Understand propagation levels.

## Testing
- Use JUnit 5 with `@Test`, `@ParameterizedTest`, and `@DisplayName` for readable test names.
- Use Mockito for mocking dependencies. Prefer `@ExtendWith(MockitoExtension.class)` over `MockitoAnnotations.openMocks`.
- Use AssertJ for fluent assertions: `assertThat(result).isEqualTo(expected)`.
- Test exceptions with `assertThrows(ExceptionType.class, () -> ...)`.
- Use `@Nested` classes to group related test scenarios.

## Build & Project Structure
- Follow Maven standard directory layout: `src/main/java`, `src/test/java`, `src/main/resources`.
- Use dependency management (BOM imports) to align transitive dependency versions.
- Run `mvn dependency:analyze` or `gradle dependencies` to detect unused/undeclared dependencies.
- Keep `pom.xml` / `build.gradle` organised: group dependencies by scope (compile, test, runtime).

## Style
- Follow Google Java Style or project-specific conventions consistently.
- Use `final` for parameters and local variables that should not be reassigned.
- Prefer enums over integer/string constants for fixed sets of values.
- Use `record` types (Java 16+) for immutable data carriers instead of manual POJO boilerplate.
