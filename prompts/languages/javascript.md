# JavaScript Language Guidelines

## Strict Mode & Modern Syntax
- Use ES module syntax (`import`/`export`), not CommonJS (`require`/`module.exports`) in new code.
- Prefer `const` by default, `let` when reassignment is needed. Never use `var`.
- Use template literals (backticks) for string interpolation, not concatenation.
- Use destructuring for object and array access where it improves readability.
- Use optional chaining (`?.`) and nullish coalescing (`??`) instead of verbose null checks.

## Type Safety Without TypeScript
- Use JSDoc type annotations (`@param`, `@returns`, `@typedef`) for function signatures and complex objects.
- Configure `// @ts-check` at the top of files and `jsconfig.json` with `checkJs: true` for IDE type checking without a build step.
- Validate external input (API responses, user data) at system boundaries — do not assume shapes.

## Async Correctness
- Always `await` promises or attach `.catch()`. Unhandled promise rejections crash Node.js processes.
- Use `Promise.all()` for concurrent independent operations, not sequential `await` in loops.
- Use `Promise.allSettled()` when you need results from all promises regardless of individual failures.
- Use `AbortController` for cancellable fetch requests and long-running operations.
- Never mix callbacks and promises in the same API — convert callbacks to promises with `util.promisify` or manual wrapping.

## Error Handling
- Use try/catch around async operations. Never swallow errors with empty `catch {}` blocks.
- Throw `Error` objects (or subclasses), never throw strings or plain objects.
- Add contextual information when re-throwing: `throw new Error("context: " + err.message, { cause: err })`.
- Use `finally` or cleanup patterns for resource management (closing connections, clearing timeouts).

## Common Bugs to Avoid
- **Floating point:** Use `Math.round()`, `toFixed()`, or integer math (cents not dollars) for financial calculations. Never compare floats with `===`.
- **Equality:** Use `===` and `!==` exclusively. Never use `==` or `!=`.
- **Array methods:** Remember that `.sort()` mutates in place and converts elements to strings by default — always pass a comparator.
- **Object mutation:** Use `structuredClone()`, `Object.assign({}, obj)`, or spread `{...obj}` to avoid unintended mutation of shared references.
- **`this` binding:** Arrow functions inherit `this` from the enclosing scope. Use arrow functions for callbacks to avoid unexpected `this` binding.

## Node.js Patterns
- Use `process.env` for configuration. Validate required environment variables at startup, not at first use.
- Use streams for large file and data processing — never load entire files into memory with `fs.readFileSync` for large inputs.
- Handle `SIGINT` and `SIGTERM` for graceful shutdown in server processes.
- Use `path.join()` and `path.resolve()` for file paths — never string-concatenate paths.

## Testing
- Use a modern test runner: Jest, Vitest, or Node.js built-in test runner (`node:test`).
- Structure tests with `describe`/`it` blocks. Use descriptive test names that explain the expected behaviour.
- Mock external dependencies (HTTP calls, file system, databases) — do not make real external calls in unit tests.
- Test error paths and edge cases, not just happy paths.

## Security
- Never use `eval()`, `new Function()`, or `setTimeout(string)` — they execute arbitrary code.
- Sanitise user input before inserting into HTML (prevent XSS). Use established libraries like DOMPurify.
- Use parameterised queries for database access. Never interpolate user input into SQL strings.
- Validate and sanitise file paths to prevent directory traversal.
