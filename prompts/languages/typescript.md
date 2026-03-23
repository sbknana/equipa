# TypeScript Best Practices

## Critical
- Enable `strict: true` in tsconfig. Never use `any` unless absolutely unavoidable.
- Always handle Promise rejections. Use try/catch with async/await.
- Never use `!` (non-null assertion) to silence the compiler — fix the actual type.
- Validate all external input at system boundaries (API params, user input, env vars).

## High
- Use discriminated unions over boolean flags for state.
- Prefer `unknown` over `any` when type is genuinely unknown — force narrowing.
- Use `readonly` for arrays/objects that should not be mutated.
- Use `satisfies` operator for type checking without widening.
- Define return types explicitly on exported functions.

## Style
- Use `interface` for object shapes, `type` for unions/intersections/utilities.
- Use `const` by default. `let` only when reassignment is needed. Never `var`.
- Use optional chaining (`?.`) and nullish coalescing (`??`) over manual checks.
- Prefer named exports over default exports.

## React (when applicable)
- Use functional components with hooks, never class components.
- Memoize expensive computations with `useMemo`, callbacks with `useCallback`.
- Never put derived state in useState — compute it inline.
- Use `key` props properly — never use array index as key for dynamic lists.

## Testing
- Use `describe`/`it` blocks with clear test names.
- Mock external dependencies, not internal implementation.
- Test behavior, not implementation details.
