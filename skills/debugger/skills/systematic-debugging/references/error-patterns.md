# Error Patterns Reference

Quick-reference lookup tables for common errors by language. Use this when you encounter an
error and need to quickly identify likely causes and fixes.

## Python

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `ModuleNotFoundError: No module named 'X'` | Package not installed or wrong venv | `pip install X` or activate correct venv |
| `ImportError: cannot import name 'X' from 'Y'` | Circular import or renamed export | Check import chain, restructure if circular |
| `TypeError: 'NoneType' object is not subscriptable` | Function returned None, then accessed like dict/list | Check the function's return path — it's missing a return |
| `TypeError: X() takes N positional arguments but M were given` | Wrong number of args | Check function signature. Missing `self`? Extra arg? |
| `AttributeError: 'X' object has no attribute 'Y'` | Typo in attribute name, or wrong type | Check spelling. Print `type(obj)` to verify. |
| `KeyError: 'X'` | Dict doesn't have the expected key | Use `.get('X', default)` or check data source |
| `ValueError: invalid literal for int() with base 10` | Trying to convert non-numeric string | Validate input before converting |
| `RecursionError: maximum recursion depth exceeded` | Infinite recursion | Check base case, or use iteration |
| `UnicodeDecodeError` | Reading binary file as text, or wrong encoding | Use `encoding='utf-8'` or `'rb'` mode |
| `PermissionError: [Errno 13]` | No write permission | Check file/dir permissions, or running as wrong user |
| `ConnectionRefusedError` | Target service not running | Check if service is up, correct host/port |
| `sqlite3.OperationalError: database is locked` | Another process holds the lock | Close other connections, check for WAL mode |
| `json.JSONDecodeError` | Invalid JSON string | Check for trailing commas, single quotes, or truncated data |

### Python-Specific Gotchas

| Pattern | Bug | Fix |
|---------|-----|-----|
| `def f(x=[]):` | Mutable default arg shared across calls | Use `def f(x=None): x = x or []` |
| `except:` (bare) | Catches KeyboardInterrupt and SystemExit | Use `except Exception:` |
| `==` vs `is` | `is` checks identity, `==` checks equality | Use `==` for values, `is` only for `None` |
| `datetime.utcnow()` | Returns naive datetime (deprecated) | Use `datetime.now(timezone.utc)` |
| Late binding closures | Lambda in loop captures variable reference | Use `lambda x=x: ...` default arg |

## JavaScript / TypeScript

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `TypeError: Cannot read properties of undefined (reading 'X')` | Accessing property on undefined | Add null check or use optional chaining `?.` |
| `TypeError: X is not a function` | Variable is not what you think | Check imports, check `typeof X` |
| `ReferenceError: X is not defined` | Variable not in scope | Check import, check spelling, check scope |
| `SyntaxError: Unexpected token` | JSON parse failure or bad syntax | Check for trailing commas in JSON |
| `ERR_MODULE_NOT_FOUND` | Import path wrong or missing extension | Check path. ESM needs `.js` extension. |
| `ECONNREFUSED` | Service not running | Check host/port, check if service started |
| `ENOENT: no such file or directory` | File path wrong | Check relative vs absolute, check cwd |
| `Maximum call stack size exceeded` | Infinite recursion | Check base case, check circular references |
| `UnhandledPromiseRejection` | Missing `.catch()` or `try/catch` | Add error handling to async code |
| `CORS error` | Server missing Access-Control-Allow-Origin | Add CORS headers on server, not client |

### JS/TS-Specific Gotchas

| Pattern | Bug | Fix |
|---------|-----|-----|
| `==` instead of `===` | Type coercion surprises | Always use `===` |
| `async` without `await` | Returns Promise instead of value | Add `await` |
| `for...in` on array | Iterates keys (strings), not values | Use `for...of` or `.forEach()` |
| `this` in callback | Loses context | Use arrow function or `.bind(this)` |
| `parseInt("08")` | Works now but historic octal issue | Always pass radix: `parseInt("08", 10)` |

## Go

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `undefined: X` | Not imported, not exported (lowercase), or wrong package | Check import and capitalization |
| `cannot use X (type Y) as type Z` | Type mismatch | Check interface implementation, use type assertion |
| `nil pointer dereference` | Accessing method/field on nil pointer | Check for nil before accessing |
| `deadlock - all goroutines are asleep` | Channel or mutex deadlock | Check channel sends/receives match, check lock ordering |
| `multiple-value X in single-value context` | Go function returns (value, error) | Handle both: `val, err := X()` |
| `imported and not used` | Unused import | Remove it or use `_` |
| `declared and not used` | Unused variable | Remove it or use `_` |
| `cannot take the address of` | Trying to get pointer to literal | Assign to variable first |
| `index out of range` | Slice/array bounds exceeded | Check `len()` before accessing |
| `fatal error: concurrent map writes` | Map accessed from multiple goroutines | Use `sync.Mutex` or `sync.Map` |

### Go-Specific Gotchas

| Pattern | Bug | Fix |
|---------|-----|-----|
| `defer` in loop | Defers don't run until function returns | Wrap in anonymous function |
| Goroutine loop var | Captures loop variable by reference | Pass as argument: `go func(v T) {...}(v)` |
| `range` over map | Order is randomized | Sort keys first if order matters |
| Error shadowing | `:=` in inner scope hides outer `err` | Use `=` for existing variables |
| `json.Marshal` on `[]byte` | Base64-encodes it | Use `json.RawMessage` for raw JSON |

## Rust

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `E0382: borrow of moved value` | Ownership moved, then used | Clone, borrow (&), or restructure ownership |
| `E0502: cannot borrow as mutable` | Mutable + immutable borrow conflict | Restructure to avoid overlapping borrows |
| `E0308: mismatched types` | Wrong type | Check expected vs actual, add conversion |
| `E0433: failed to resolve` | Missing `use` or wrong path | Add `use` import |
| `unwrap() on None/Err` | Panics at runtime | Use `match`, `if let`, or `?` operator |
| `thread 'main' panicked at 'index out of bounds'` | Vec/slice access beyond length | Check `.len()` or use `.get()` |
| `the trait X is not implemented for Y` | Missing trait impl | Implement trait or use different type |

## C# / .NET

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `NullReferenceException` | Accessing member on null object | Add null check or use `?.` operator |
| `InvalidOperationException` | Collection modified during iteration | Use `.ToList()` before iterating, or use index loop |
| `FileNotFoundException` | Wrong path or missing file | Check `Path.Combine()`, check working directory |
| `ArgumentNullException` | Null passed to parameter that doesn't accept null | Validate inputs, check caller |
| `TaskCanceledException` | HTTP timeout | Increase timeout or add retry logic |
| `DbUpdateConcurrencyException` | EF Core optimistic concurrency conflict | Reload entity and retry |
| `CS0120: non-static member requires object reference` | Accessing instance member from static context | Create instance or make member static |

## Universal Patterns

### "Works on my machine" Debugging

```
1. Check: Environment variables (different between machines?)
2. Check: Database state (different data?)
3. Check: OS / architecture differences (paths, line endings)
4. Check: Dependency versions (lockfile in sync?)
5. Check: File permissions (different user?)
6. Check: Network / DNS (different routes?)
```

### "It was working yesterday" Debugging

```
1. git log --oneline -10          # What changed?
2. git stash && test              # Is it your local changes?
3. Check dependency lockfile diff # Did a dependency update?
4. Check environment changes      # New env var? Changed config?
5. Check external services        # Is the API/DB still up?
```

### Performance Debugging

```
1. Measure FIRST (don't guess the bottleneck)
2. Check: N+1 queries (most common in web apps)
3. Check: Missing database indexes
4. Check: Unbounded loops or recursion
5. Check: Large data loaded into memory
6. Check: Synchronous I/O blocking the event loop (Node.js)
```
