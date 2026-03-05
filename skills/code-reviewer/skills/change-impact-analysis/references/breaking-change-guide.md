# Breaking Change Detection Guide

Reference for identifying breaking changes across different languages and contexts.
Use during change impact analysis to verify backwards compatibility.

## API Breaking Changes (REST / HTTP)

### Definitely Breaking
| Change | Why | Example |
|--------|-----|---------|
| Remove endpoint | Clients get 404 | DELETE `/api/v1/users/:id/avatar` |
| Change HTTP method | Clients get 405 | GET → POST on `/api/search` |
| Remove response field | Clients parsing it crash | Remove `user.email` from response |
| Change response field type | Clients get type errors | `"count": "5"` → `"count": 5` |
| Add required request field | Existing requests fail validation | New required `region` param |
| Change authentication | Existing tokens rejected | Bearer → API key |
| Change error format | Client error handling breaks | `{error: "msg"}` → `{errors: [{msg}]}` |

### NOT Breaking
| Change | Why |
|--------|-----|
| Add optional request field | Existing requests still valid |
| Add response field | Clients ignore unknown fields |
| Add new endpoint | Nothing calls it yet |
| Add optional header | Existing requests work without it |
| Change internal implementation | Same inputs → same outputs |

### Subtle / Conditional
| Change | Breaking When... |
|--------|-----------------|
| Change default value | Clients rely on the default behavior |
| Change sort order | Clients assume a specific order |
| Change pagination | Clients cache page tokens |
| Add rate limiting | Existing usage exceeds the limit |
| Change response time | Clients have tight timeouts |

## Function / Method Breaking Changes

### Definitely Breaking
```
Remove function                    → Callers get compile/import error
Remove parameter                   → Callers passing it get error
Add required parameter             → Callers missing it get error
Change return type                 → Callers expecting old type break
Change exception/error types       → Callers catching specific errors miss them
Rename function                    → Callers referencing old name break
Change parameter order             → Positional callers pass wrong values
Make synchronous → async           → Callers not awaiting get Promise/Future
```

### NOT Breaking
```
Add optional parameter with default → Existing callers unaffected
Add overloaded method              → Existing callers use original
Widen parameter type (accept more) → Existing callers still valid
Narrow return type (return less)   → Existing callers still valid
Add a new function                 → Nothing calls it yet
```

### Language-Specific Gotchas

**Python:**
```python
# BREAKING: Change **kwargs behavior
# Before: def process(data, **kwargs)  # silently ignored unknown kwargs
# After:  def process(data, *, strict=False)  # now validates parameters

# BREAKING: Change mutable default
# Before: def append_to(element, target=[])  # reuses list between calls
# After:  def append_to(element, target=None)  # creates new list each call
# This fixes a bug but changes behavior for code relying on the shared list

# NOT BREAKING: Add type hints (runtime doesn't enforce them)
def greet(name: str) -> str:  # Safe to add
```

**TypeScript:**
```typescript
// BREAKING: Narrow a union type
// Before: type Status = 'active' | 'inactive' | 'pending'
// After:  type Status = 'active' | 'inactive'
// Callers using 'pending' get type errors

// BREAKING: Make property required
// Before: interface User { email?: string }
// After:  interface User { email: string }

// NOT BREAKING: Widen a union type
// Before: type Status = 'active' | 'inactive'
// After:  type Status = 'active' | 'inactive' | 'pending'
```

**Go:**
```go
// BREAKING: Change receiver type (pointer ↔ value)
// Before: func (u User) Name() string
// After:  func (u *User) Name() string
// Interface implementors may break

// BREAKING: Add return value
// Before: func Save(u User)
// After:  func Save(u User) error
// All callers must now handle the error

// NOT BREAKING: Add method to struct (unless it clashes with embedded type)
```

## Database Breaking Changes

### Column Changes
| Change | Breaking? | Safe Migration |
|--------|-----------|---------------|
| Add nullable column | NO | `ALTER TABLE ADD COLUMN x DEFAULT NULL` |
| Add NOT NULL column without default | YES (locks table, fails for existing rows) | Add as nullable, backfill, then add constraint |
| Drop column | MAYBE (breaks queries that SELECT it) | Verify no code references it first |
| Rename column | YES (breaks all queries) | Add new column, migrate data, drop old |
| Change column type | MAYBE (depends on cast compatibility) | Add new column, migrate, drop old |
| Add index | NO (but may lock table briefly) | Use `CREATE INDEX CONCURRENTLY` (PostgreSQL) |
| Drop index | NO (but queries may get slower) | Check query plans first |

### Table Changes
| Change | Breaking? | Safe Migration |
|--------|-----------|---------------|
| Add table | NO | Nothing references it yet |
| Drop table | YES | Verify no code references it |
| Rename table | YES | Create view with old name as alias |

### Detection Commands
```bash
# Find all references to a column/table
grep -rn "column_name" --include="*.{py,ts,js,go,rs,sql,cs}"

# Find ORM references (may use different names)
grep -rn "ColumnName\|column_name" --include="*.{py,ts,js,go,rs,cs}"

# Check for raw SQL
grep -rn "SELECT.*column_name\|INSERT.*column_name\|WHERE.*column_name" \
  --include="*.{py,ts,js,go,rs,cs}"
```

## Dependency Version Breaking Changes

### Semantic Versioning Guide
```
1.2.3 → 1.2.4  (PATCH) — Bug fixes only. Should be safe.
1.2.3 → 1.3.0  (MINOR) — New features, backwards compatible. Usually safe.
1.2.3 → 2.0.0  (MAJOR) — Breaking changes expected. Read the CHANGELOG.
```

### High-Risk Dependency Updates
| Signal | Risk | Action |
|--------|------|--------|
| Major version bump | HIGH | Read CHANGELOG, check migration guide |
| Framework update (React, Django, etc.) | HIGH | Check deprecated API usage |
| Security patch (same version) | LOW | Apply quickly, minimal testing |
| Transitive dependency change | MEDIUM | Check lockfile diff |
| Compiler/runtime update (Node, Go, Python) | HIGH | Check compatibility matrix |

### Checking for Breaking Changes
```bash
# Node.js — compare package versions
npm outdated
npm diff --diff=package@old --diff=package@new

# Python — check what would change
pip install --dry-run package==new_version

# Go — check module compatibility
go mod tidy
go vet ./...

# Read changelogs
# Most packages publish: CHANGELOG.md, HISTORY.md, or GitHub Releases
```

## Consumer Impact Assessment Template

When you find a breaking change, document its impact on each consumer:

```markdown
### Breaking Change: [description]

| Consumer | File:Line | Impact | Required Fix |
|----------|-----------|--------|-------------|
| UserController | src/controllers/user.ts:42 | Uses removed `email` field | Update to use `contact.email` |
| UserService | src/services/user.ts:18 | Calls renamed function | Update import to new name |
| Tests | tests/user.test.ts:55 | Asserts on old response shape | Update expected values |

**Migration effort:** [trivial / moderate / significant]
**Can be done incrementally:** [yes / no]
```
