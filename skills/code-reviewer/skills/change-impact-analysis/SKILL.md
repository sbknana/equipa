---
name: change-impact-analysis
description: >
  Assess the blast radius of code changes — what could break, what downstream systems are
  affected, and whether the change is safe to merge. Use when reviewing PRs that modify shared
  code, change interfaces, update dependencies, or touch database schemas.
  Triggers: blast radius, breaking change, impact analysis, what could break, downstream,
  compatibility, migration, schema change.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Change Impact Analysis

## Core Principle

**Every change has a blast radius. Your job is to measure it before it detonates.**
A change to a shared utility is more dangerous than a change to a leaf component.

## When to Use

- Changes to shared code (utils, middleware, base classes)
- Changes to interfaces, types, or API contracts
- Database schema changes
- Dependency version updates
- Changes to authentication or authorization
- Changes to configuration or environment handling

## When NOT to Use

- Changes to leaf components with no dependents
- New files that nothing imports yet
- Test-only changes
- Documentation-only changes

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "It's just a small change" | Small changes to shared code have large blast radii | Measure the blast radius regardless of change size |
| "The tests pass so it's fine" | Tests don't cover every consumer | Check all callers, not just tests |
| "It's backwards compatible" | Verify, don't assume | Check for implicit contracts (behavior, timing, ordering) |

## Blast Radius Assessment

### Step 1: Identify Changed Interfaces

For each modified file, determine:
- **Exports changed?** — Functions, classes, types, constants that other files import
- **Behavior changed?** — Same signature but different behavior (most dangerous)
- **Side effects changed?** — Database writes, HTTP calls, file operations

### Step 2: Find All Consumers

```bash
# Find everything that imports the changed module
grep -rn "import.*<module_name>" --include="*.{ts,js,py,go,rs}"
grep -rn "from <module_name> import" --include="*.py"
grep -rn "require.*<module_name>" --include="*.{ts,js}"
```

Count consumers and classify them:

| Consumer Count | Risk Level | Action |
|---------------|-----------|--------|
| 0 | None | Leaf code, safe to change |
| 1-3 | Low | Verify each consumer manually |
| 4-10 | Medium | Check consumers, flag for careful review |
| 10+ | High | This is shared infrastructure. Treat with extreme care. |

### Step 3: Breaking Change Detection

| Change Type | Breaking? | Mitigation |
|------------|-----------|------------|
| **Add** optional parameter | No | Safe |
| **Add** required parameter | YES | Add with default value instead |
| **Remove** parameter | YES | Deprecate first, remove later |
| **Change** return type | YES | Version the function or add new function |
| **Change** behavior (same signature) | YES (subtle) | Document the change clearly |
| **Rename** function/class | YES | Re-export old name as alias |
| **Remove** export | YES | Check for consumers first |
| **Change** database column | MAYBE | Depends on read/write patterns |
| **Add** database column | No (usually) | Unless NOT NULL without default |
| **Remove** database column | YES | Check all queries referencing it |

### Step 4: Risk Classification

| Risk Level | Criteria | Review Action |
|-----------|---------|---------------|
| **CRITICAL** | Auth/authz changes, payment logic, data deletion, schema migrations | Block until verified by security review |
| **HIGH** | Shared infrastructure, API contracts, dependency upgrades | Flag for human review, require tests |
| **MEDIUM** | Service-to-service interfaces, business logic changes | Verify consumers, check tests |
| **LOW** | Leaf components, new features, test changes | Standard review sufficient |

## Database Schema Impact

For any database changes:

```
1. Is the migration reversible?
2. Can it run without downtime?
   - Adding a column: Usually safe
   - Adding NOT NULL without default: DANGEROUS (locks table, fails for existing rows)
   - Dropping a column: Safe if no code reads it
   - Renaming a column: DANGEROUS (breaks all queries)
   - Adding an index: Usually safe but may lock table briefly
3. Do any queries, ORMs, or raw SQL reference the changed columns?
```

```bash
# Find all references to a database column
grep -rn "column_name" --include="*.{py,ts,js,go,sql,rs}"
```

## Dependency Update Impact

For dependency version changes:

```
1. Is this a major version bump? (Breaking changes expected)
2. Read the CHANGELOG between old and new version
3. Check if the project uses deprecated APIs from the old version
4. Are transitive dependencies affected?
```

```bash
# Check what changed (Node)
npm diff --diff=<package>@<old> --diff=<package>@<new>

# Check what changed (Python)
pip install <package>==<new> --dry-run
```

## Output Format

```markdown
## Change Impact Analysis

### Blast Radius
- Files changed: N
- Direct consumers affected: N
- Risk level: LOW/MEDIUM/HIGH/CRITICAL

### Breaking Changes
- [ ] None detected / [BREAKING] <description>

### Consumer Impact
| Consumer | Impact | Status |
|----------|--------|--------|
| <file> | <what changes for them> | OK / NEEDS UPDATE |

### Database Impact
- [ ] No schema changes / <migration assessment>

### Dependency Impact
- [ ] No dependency changes / <version change assessment>

### Recommendation
- [ ] SAFE TO MERGE / NEEDS FIXES / NEEDS HUMAN REVIEW
```

## Quality Checklist

- [ ] All changed exports identified
- [ ] All consumers of changed exports found via grep
- [ ] Breaking changes documented with severity
- [ ] Database impact assessed (if applicable)
- [ ] Dependency changes assessed (if applicable)
- [ ] Risk level classification assigned
- [ ] Clear merge recommendation given
