# TypeScript Language Guidelines

## Strict Mode
- Always enable `strict: true` in `tsconfig.json`. Never disable individual strict checks.
- Never use `any` — use `unknown` for truly unknown types and narrow with type guards.
- Avoid type assertions (`as Type`) unless necessary. Prefer type narrowing with `if`/`in`/`instanceof`.
- Use `satisfies` operator to validate types without widening.

## Type Safety
- Define explicit return types on exported functions and public API methods.
- Use discriminated unions over optional fields for variant types.
- Prefer `interface` for object shapes that may be extended, `type` for unions and intersections.
- Use `readonly` for properties that should not be mutated after construction.
- Use `Record<K, V>` over `{ [key: string]: V }` for mapped types.

## Async Correctness
- Always `await` promises. Unhandled promise rejections crash Node.js processes.
- Use `Promise.all()` for concurrent operations, not sequential `await` in loops.
- Add `.catch()` or try/catch around all async operations that can fail.
- Never use `void` return on async functions called for their side effects — always handle the returned promise.
- Use `AbortController` for cancellable async operations.

## React Patterns (when framework detected)
- Use functional components exclusively. No class components.
- Memoize expensive computations with `useMemo` and callback references with `useCallback`.
- Avoid inline object/array literals in JSX props — they cause unnecessary re-renders.
- Use `key` prop correctly in lists — never use array index as key for dynamic lists.
- Prefer controlled components over refs for form state.
- Clean up side effects in `useEffect` return functions (timers, subscriptions, abort controllers).

## Error Handling
- Use discriminated result types (`{ ok: true; data: T } | { ok: false; error: E }`) for expected failures.
- Use `Error` subclasses for unexpected failures. Include contextual information.
- Never swallow errors with empty `catch {}` blocks.

## Null Safety
- Prefer `undefined` over `null` for absent values (consistent with optional chaining `?.`).
- Use nullish coalescing `??` over logical OR `||` to avoid false-positive on `0`, `""`, `false`.
- Use optional chaining `?.` instead of nested null checks.

## Imports & Modules
- Use ES module syntax (`import`/`export`), not CommonJS (`require`).
- Use barrel exports (`index.ts`) sparingly — they can cause circular dependency issues and tree-shaking failures.
- Prefer named exports over default exports for better refactoring support.
