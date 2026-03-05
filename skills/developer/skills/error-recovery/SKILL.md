---
name: error-recovery
description: >
  Structured recovery strategies when builds break, tests fail, imports are missing, or you're
  stuck in a loop. Use when you encounter errors, test failures, build failures, dependency
  issues, or when you've attempted the same fix more than twice without success.
  Triggers: build failed, test failed, import error, stuck, error loop, dependency issue,
  compilation error, runtime error, same error twice.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - Edit
  - Write
---

# Error Recovery

## Core Principle

**Never attempt the same fix twice. If it didn't work, the problem is different than you think.**
Every failed fix gives you information. Use it to update your mental model before trying again.

## When to Use

- A build or compilation fails after your changes
- Tests fail after your changes
- You get import/dependency errors
- You've tried a fix and it didn't work
- You're in a loop doing the same thing repeatedly
- Your changes cause runtime errors

## When NOT to Use

- You haven't tried anything yet (plan first, recover later)
- The error is in code you didn't write and isn't related to your task
- The error existed before your changes (not your problem — document and move on)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "Let me try the same thing again" | Identical inputs produce identical outputs | Change your approach, not your attempt |
| "I'll just suppress the error" | Hiding errors creates worse errors | Fix the root cause |
| "Let me rewrite everything from scratch" | Nuclear option wastes all previous work | Isolate the broken part, fix only that |
| "The tests must be wrong" | Tests were passing before your changes | Your changes broke something |
| "I'll add a try/except to catch it" | Exception swallowing masks real bugs | Fix the cause, don't catch the symptom |

## The Error Recovery Decision Tree

### 1. Classify the Error (1 turn max)

| Error Type | Examples | Go to |
|-----------|---------|-------|
| **Build/Compile** | Syntax error, type error, missing import | Section A |
| **Test Failure** | Assertion failed, expected X got Y | Section B |
| **Runtime** | NullPointerException, undefined is not a function | Section C |
| **Dependency** | Module not found, version conflict, missing package | Section D |
| **Environment** | Permission denied, file not found, connection refused | Section E |

### Section A: Build/Compile Errors

**Step 1:** Read the FULL error message. The line number is usually in the message.
**Step 2:** Go to that exact line. Is it your code or generated code?
**Step 3:** Common fixes:

| Error Pattern | Likely Cause | Fix |
|--------------|-------------|-----|
| Missing import | You used a symbol without importing | Add the import |
| Type mismatch | Wrong type passed to function | Check the function signature |
| Syntax error | Mismatched brackets, missing comma | Check the exact line cited |
| Undefined variable | Typo in variable name | Grep for the correct spelling |
| Cannot find module | Wrong import path | Check the actual file path |

**Step 4:** After fixing, run the build again IMMEDIATELY. Don't make other changes first.

### Section B: Test Failures

**Step 1:** Read the assertion message. What was expected vs. actual?
**Step 2:** Determine if this is:
- **Your test failing** → Your test expectations may be wrong
- **Existing test failing** → Your code change broke existing behavior

**For your test failing:**
```
1. Check: Does the function actually return what you expect?
2. Check: Are you testing the right function?
3. Check: Is your test setup correct (mocks, fixtures, test data)?
4. Fix the TEST if the code is correct. Fix the CODE if the test is correct.
```

**For existing test failing:**
```
1. Read the failing test to understand what behavior it asserts
2. Your changes violated that behavior contract
3. Options:
   a. Adjust your implementation to preserve the existing behavior
   b. Update the test IF the behavior change is intentional and correct
4. NEVER delete a failing test to make it pass
```

### Section C: Runtime Errors

**Step 1:** Read the stack trace. Start from the BOTTOM (the root cause), not the top.
**Step 2:** Find YOUR code in the stack trace. That's where the fix belongs.
**Step 3:** Common runtime error patterns:

| Error | Root Cause | Fix |
|-------|-----------|-----|
| null/undefined access | Variable not initialized or function returns null | Add null check or fix initialization |
| index out of bounds | Array/list shorter than expected | Check length before access |
| key not found | Dictionary/map missing expected key | Use `.get()` with default or check first |
| type error at runtime | Dynamic type doesn't match expected | Add type checking or fix the source |
| connection refused | Service not running or wrong port | Check the service/config, not your code |

### Section D: Dependency Errors

**Step 1:** Is the dependency in the project's dependency file?
- `package.json`, `requirements.txt`, `go.mod`, `Cargo.toml`, `*.csproj`

**Step 2:** If missing, add it:
```bash
# Don't guess versions — check what the project uses
npm install <package>          # Node
pip install <package>          # Python (or uv add)
go get <package>               # Go
cargo add <package>            # Rust
dotnet add package <package>   # C#
```

**Step 3:** If version conflict:
```
1. Check which version the project currently uses
2. Use THAT version, not the latest
3. Don't upgrade dependencies unless that's your task
```

### Section E: Environment Errors

**Stop.** Environment errors are usually NOT your problem to solve.

```
- Permission denied → Document in BLOCKERS, don't try to chmod
- File not found → Check the path. Is it relative vs absolute?
- Connection refused → Is the service running? Document in BLOCKERS.
- Out of memory → Your code may have an infinite loop. Check for that.
```

## The 3-Strike Rule

If you've tried 3 different fixes for the same error:

1. **STOP trying to fix it**
2. **Document what you tried** and what happened each time
3. **Output RESULT: blocked** with clear BLOCKERS description
4. **Do NOT continue making changes** — you're making it worse

## Recovery From a Loop

Signs you're in a loop:
- You've undone and redone the same change
- You're alternating between two approaches
- Each fix creates a new error in a different place

**Break the loop:**
1. Revert ALL your changes: `git checkout -- .`
2. Re-read the task description
3. Start with a DIFFERENT approach entirely
4. If you've reverted twice, output RESULT: blocked

## Quality Checklist

After recovering from an error:
- [ ] The original error is fixed (not just suppressed)
- [ ] No new errors were introduced
- [ ] The build succeeds
- [ ] Existing tests still pass
- [ ] I haven't suppressed any warnings or errors with catch-all handlers
- [ ] I've run the verification step from my implementation plan

## References

- See `references/common-errors.md` for language-specific error patterns
