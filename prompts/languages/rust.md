# Rust Language Guidelines

## Ownership & Borrowing
- Prefer borrowing (`&T`, `&mut T`) over cloning. Only `.clone()` when ownership transfer is truly needed.
- Use `Cow<'_, str>` for functions that sometimes need owned data and sometimes borrowed.
- Avoid unnecessary `Box<dyn Trait>` — use generics with trait bounds for static dispatch when the type is known at compile time.
- Prefer `&str` over `&String` in function parameters.

## Error Handling
- Use `Result<T, E>` for recoverable errors, never `panic!` in library code.
- Define custom error types with `thiserror` for library errors, use `anyhow` for application-level error propagation.
- Use the `?` operator for error propagation. Avoid `.unwrap()` and `.expect()` in production code — only use them when a panic is the correct response (invariant violation).
- Add context to errors with `.context("description")` (anyhow) or `map_err` (custom errors).
- Implement `Display` and `Error` traits for custom error types.

## Memory Safety
- Avoid `unsafe` blocks unless absolutely necessary. Document every `unsafe` block with a `// SAFETY:` comment explaining the invariant.
- Prefer `Vec<T>` over raw pointers. Use `MaybeUninit` only when performance requires it and the safety is provable.
- Use `Arc<Mutex<T>>` for shared mutable state across threads. Prefer `RwLock` when reads vastly outnumber writes.

## Lifetime Annotations
- Let the compiler elide lifetimes when possible. Only annotate when the compiler requires it.
- Name lifetimes descriptively when there are multiple (`'input`, `'output`), use `'a` only for single-lifetime cases.
- Prefer owned types in struct fields over references with lifetimes, unless the struct is clearly a short-lived view.

## Pattern Matching
- Use `match` exhaustively — never use a wildcard `_` arm to silence a non-exhaustive match unless future variants are intentionally ignored.
- Use `if let` and `while let` for single-variant matches.
- Destructure structs and enums to access fields directly.

## Concurrency
- Use `tokio` or `async-std` for async I/O. Never block inside async contexts — use `spawn_blocking` for CPU-heavy work.
- Prefer channels (`tokio::sync::mpsc`, `crossbeam::channel`) over shared state for communication between tasks.
- Use `Send + Sync` bounds explicitly when designing concurrent APIs.
- Avoid `Mutex` across `.await` points — use `tokio::sync::Mutex` if an async-aware lock is required.

## Testing
- Use `#[test]` functions in the same file with `#[cfg(test)]` modules.
- Use `assert_eq!` and `assert_ne!` with descriptive messages as the third argument.
- Use `proptest` or `quickcheck` for property-based testing of algorithmic code.
- Test error paths: assert specific error variants with `matches!` or pattern matching.

## Cargo & Dependencies
- Pin dependency versions with `=` in production binaries. Use `cargo audit` to check for known vulnerabilities.
- Use `#[deny(clippy::all, clippy::pedantic)]` for strict linting. Address warnings, do not suppress them without justification.
- Prefer `workspace` dependencies in multi-crate projects for version consistency.
