# Common Error Patterns by Language

## Python

| Error | Likely Cause | Quick Fix |
|-------|-------------|-----------|
| `ModuleNotFoundError` | Missing install or wrong import path | `pip install <package>` or fix import |
| `ImportError: cannot import name` | Circular import or renamed export | Check the source module for the correct name |
| `AttributeError: 'NoneType'` | Function returned None unexpectedly | Add None check or fix the function |
| `TypeError: missing required argument` | Wrong number of args to function | Check function signature |
| `IndentationError` | Mixed tabs/spaces or wrong indent level | Fix indentation (use spaces) |
| `KeyError` | Dictionary key doesn't exist | Use `.get(key, default)` |
| `ValueError: too many values to unpack` | Tuple/list size mismatch in destructuring | Check the source data structure |
| `sqlite3.OperationalError: no such table` | Table not created or wrong DB path | Check migrations, check DB path |
| `RecursionError` | Infinite recursion | Add base case or check for cycles |

## JavaScript / TypeScript

| Error | Likely Cause | Quick Fix |
|-------|-------------|-----------|
| `Cannot find module` | Wrong import path or missing install | Check path, run `npm install` |
| `TypeError: X is not a function` | Wrong import (default vs named) or null | Check the export type |
| `TypeError: Cannot read properties of undefined` | Accessing property on undefined | Add optional chaining `?.` or null check |
| `SyntaxError: Unexpected token` | Wrong file extension or missing babel/ts config | Check file extension matches content |
| `ReferenceError: X is not defined` | Variable not declared or wrong scope | Check declaration and scope |
| `TS2322: Type X is not assignable to type Y` | Type mismatch | Fix the type or add proper conversion |
| `TS2339: Property does not exist on type` | Missing interface property | Add to interface or check spelling |
| `ERR_REQUIRE_ESM` | Mixing CommonJS require with ESM module | Use `import` instead of `require` |

## Go

| Error | Likely Cause | Quick Fix |
|-------|-------------|-----------|
| `undefined: X` | Not imported or not exported (lowercase) | Import the package or capitalize the name |
| `cannot use X as type Y` | Type mismatch | Check types, add conversion |
| `imported and not used` | Unused import | Remove it or use it |
| `declared and not used` | Unused variable | Remove it or use `_` |
| `cannot refer to unexported name` | Trying to use lowercase (private) from another package | The function/type must be Capitalized |
| `nil pointer dereference` | Calling method on nil | Add nil check before use |
| `multiple-value in single-value context` | Ignoring error return | Handle the error: `val, err := ...` |
| `go.sum mismatch` | Module cache inconsistent | Run `go mod tidy` |

## Rust

| Error | Likely Cause | Quick Fix |
|-------|-------------|-----------|
| `E0382: borrow of moved value` | Value used after move | Clone, borrow with `&`, or restructure |
| `E0502: cannot borrow as mutable` | Simultaneous mutable + immutable borrow | Restructure to avoid overlapping borrows |
| `E0433: failed to resolve` | Missing `use` statement or wrong path | Add the correct `use` import |
| `E0308: mismatched types` | Wrong type | Check expected vs actual types |
| `E0599: no method named X` | Missing trait import or wrong type | `use TraitName;` or check the type |

## C# / .NET

| Error | Likely Cause | Quick Fix |
|-------|-------------|-----------|
| `CS0246: type or namespace not found` | Missing using or NuGet package | Add `using` or install package |
| `CS1061: does not contain a definition` | Wrong type or missing method | Check the type definition |
| `NullReferenceException` | Null object access | Add null check or fix initialization |
| `CS0029: Cannot implicitly convert type` | Type mismatch | Add explicit cast or fix the type |
| `InvalidOperationException: Sequence contains no elements` | `.First()` on empty collection | Use `.FirstOrDefault()` |

## Universal Patterns

### "It worked before my changes"
1. `git diff` — see exactly what you changed
2. The bug is in your diff, not elsewhere
3. If the diff is large, bisect: revert half, test, narrow down

### "It works locally but fails in CI"
1. Environment difference (Node version, Python version, etc.)
2. Missing environment variable
3. File path case sensitivity (Linux is case-sensitive, macOS/Windows aren't)
4. Missing dependency not committed to lock file

### "Error only happens sometimes"
1. Race condition — look for async/concurrent code
2. Uninitialized state — look for code that depends on order of execution
3. External dependency — network, database, file system timing
