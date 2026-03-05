---
name: architecture-review
description: >
  Systematic architectural assessment of code changes. Evaluates coupling, cohesion, SOLID
  principles, and common anti-patterns. Use when reviewing changes that add new modules,
  modify data flow, change API contracts, or introduce new dependencies.
  Triggers: architecture, coupling, cohesion, design, SOLID, anti-pattern, code smell,
  separation of concerns, dependency direction.
allowed-tools:
  - Read
  - Glob
  - Grep
---

# Architecture Review

## Core Principle

**Good architecture minimizes the cost of future changes.** The question isn't "does this work?"
(that's the tester's job) — it's "will this be easy to change, test, and understand in 6 months?"

## When to Use

- Changes add new files, modules, or packages
- Changes modify interfaces between components
- Changes add new dependencies (imports, libraries)
- Changes modify data flow or introduce new data paths
- PR touches 5+ files across multiple directories

## When NOT to Use

- Changes are purely within a single function (too granular)
- Changes are to configuration files only
- Changes are to tests only
- Changes are cosmetic (formatting, comments, renaming)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "It works, so the architecture is fine" | Working code can still be unmaintainable | Evaluate maintainability, not correctness |
| "This is how the rest of the codebase does it" | Existing patterns may be bad patterns | Flag if the existing pattern is an anti-pattern |
| "It's just a small helper function" | Small functions in the wrong place create coupling | Check if the function belongs in the right module |
| "We can refactor later" | Technical debt compounds. Flag it now. | Document the concern even if not blocking |

## The Architecture Review Checklist

### 1. Dependency Direction (most important)

Dependencies should flow ONE way: Handler → Service → Repository → Model

```
GOOD: Controller imports Service
BAD:  Service imports Controller (circular dependency)
BAD:  Model imports Service (wrong direction)
BAD:  Utils imports business logic (utils should be generic)
```

**Check:**
- [ ] No circular imports between packages/modules
- [ ] Business logic does NOT import from HTTP/CLI/UI layers
- [ ] Models/types are leaf nodes (imported by many, import nothing from the project)
- [ ] Utils/helpers are generic (no business logic knowledge)

### 2. Separation of Concerns

Each module should have ONE reason to change.

| Layer | Should Contain | Should NOT Contain |
|-------|---------------|-------------------|
| **Handlers/Controllers** | Request parsing, response formatting, validation | Business logic, database queries |
| **Services** | Business rules, orchestration | HTTP concerns, SQL queries |
| **Repository/Data** | Database queries, data mapping | Business rules, HTTP concerns |
| **Models/Types** | Data structures, validation rules | Business logic, I/O |
| **Config** | Settings, constants, env vars | Logic, conditions |

**Red flags:**
- Functions over 100 lines (doing too much)
- Files over 500 lines (multiple responsibilities)
- God objects (one class that does everything)
- Database queries in HTTP handlers
- HTTP response formatting in business logic

### 3. SOLID Principles Assessment

| Principle | What to Check | Red Flag |
|-----------|--------------|----------|
| **S**ingle Responsibility | Does each class/module have ONE job? | Class with 10+ methods doing different things |
| **O**pen/Closed | Can behavior be extended without modifying existing code? | Switch statements that need updating for every new case |
| **L**iskov Substitution | Can subtypes replace parent types? | Type checks or casts in code that should be polymorphic |
| **I**nterface Segregation | Are interfaces focused? | Interface with 10+ methods where implementers only use 2 |
| **D**ependency Inversion | Do high-level modules depend on abstractions? | Direct database calls in business logic without an interface |

### 4. Common Anti-Patterns

| Anti-Pattern | Signal | Severity | Fix |
|-------------|--------|----------|-----|
| **God Object** | One class with 20+ methods | HIGH | Split by responsibility |
| **Feature Envy** | Method uses more data from another class than its own | MEDIUM | Move method to the other class |
| **Shotgun Surgery** | One feature change requires modifying 5+ files | HIGH | Consolidate related logic |
| **Primitive Obsession** | Using strings/ints for domain concepts (email, money, ID) | LOW | Create value objects |
| **Leaky Abstraction** | Implementation details exposed in interfaces | MEDIUM | Hide implementation behind interface |
| **Copy-Paste Code** | Identical logic in 3+ places | MEDIUM | Extract shared function |
| **Deep Nesting** | 4+ levels of if/for nesting | LOW | Extract to functions, use early returns |
| **Boolean Blindness** | Functions taking 3+ boolean parameters | LOW | Use options object or enum |

### 5. API Contract Review

For changes to APIs (HTTP, function interfaces, protocols):

- [ ] Is the change backwards compatible?
- [ ] Are new required fields justified (or should they be optional)?
- [ ] Is error handling consistent with existing endpoints?
- [ ] Are response shapes consistent with existing patterns?
- [ ] Is versioning needed?

## Output Format

```markdown
## Architecture Review

### Dependency Assessment
- [PASS/ISSUE] Dependency direction: <assessment>

### Separation of Concerns
- [PASS/ISSUE] Layer violations: <findings>

### SOLID Compliance
- [PASS/ISSUE] <specific principle>: <finding>

### Anti-Patterns Detected
- [SEVERITY] <anti-pattern>: <location and description>

### Recommendations
1. <actionable recommendation>
2. <actionable recommendation>
```

## Quality Checklist

- [ ] Dependency direction checked (no circular dependencies)
- [ ] Layer boundaries verified (no cross-layer violations)
- [ ] At least 3 SOLID principles assessed
- [ ] Anti-pattern scan completed
- [ ] API contract reviewed (if applicable)
- [ ] Findings are specific (file:line, not vague)
- [ ] Recommendations are actionable (not "consider refactoring")
