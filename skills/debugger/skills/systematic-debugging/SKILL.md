---
name: systematic-debugging
description: >
  Hypothesis-driven debugging with binary search, log analysis, and structured root cause
  analysis. Use when a bug report comes in, when tests fail for unknown reasons, or when
  you need to find and fix a runtime error. Triggers: bug, error, crash, failing test,
  root cause, stacktrace, traceback, exception, unexpected behavior, regression.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - Edit
---

# Systematic Debugging

## Core Principle

**Find the root cause, not the first thing that makes the symptom go away.**
A fix that doesn't address the root cause creates a new bug. Debugging is hypothesis testing —
form a theory, test it, narrow the search space, repeat.

## When to Use

- A bug report or failing test needs investigation
- Runtime errors or exceptions with no obvious cause
- "It worked before" regression reports
- Unexpected behavior where the code runs but produces wrong results
- Performance degradation of unknown origin

## When NOT to Use

- The error message is self-explanatory and the fix is obvious (< 5 lines)
- The issue is a missing dependency or configuration (not a code bug)
- The task is to implement new functionality (use implementation-planning instead)

## Rationalizations to Reject

| Shortcut | Why It's Wrong | Required Action |
|----------|---------------|-----------------|
| "Let me just try this fix and see if it works" | Shotgun debugging wastes turns and can introduce new bugs | Form a hypothesis FIRST, then test it |
| "The error is in this file so the bug must be here" | Errors surface far from their root cause | Trace the data flow backward from the error |
| "I'll add logging everywhere" | Drowns you in noise, wastes turns | Add targeted logging at decision points only |
| "The tests pass now so the bug is fixed" | You may have masked the symptom | Verify the root cause is addressed, not just the symptom |
| "This looks related so I'll fix this too" | Scope creep. Fix one bug at a time. | Fix only the reported bug. Log other issues separately. |

## The 5-Step Debugging Method

### Step 1: Reproduce and Characterize (1-2 turns)

Before writing a single line of code:

```
1. WHAT exactly is the symptom? (error message, wrong output, crash)
2. WHERE does it happen? (which file, function, line, endpoint)
3. WHEN does it happen? (always, intermittently, after a specific action)
4. WHAT changed? (recent commits, dependency updates, config changes)
```

**Reproduction checklist:**
- [ ] Read the error message / stacktrace completely — don't skim
- [ ] Identify the FIRST error (not cascading errors)
- [ ] Check if there's a test case that demonstrates the bug

```bash
# Check recent changes that might have introduced the bug
git log --oneline -20
git diff HEAD~5 -- <suspected_file>
```

### Step 2: Form Hypotheses (1 turn)

Based on Step 1, list 2-3 possible root causes ranked by likelihood:

```
Hypothesis 1 (most likely): <specific theory>
  Test: <how to confirm or eliminate>

Hypothesis 2: <specific theory>
  Test: <how to confirm or eliminate>

Hypothesis 3: <specific theory>
  Test: <how to confirm or eliminate>
```

**Hypothesis quality rules:**
- Each hypothesis must be SPECIFIC (not "something is wrong with the data")
- Each hypothesis must be TESTABLE in 1 turn
- Rank by: most likely first, then easiest to test

### Step 3: Binary Search for Root Cause (1-3 turns)

Test hypotheses in order. For each:

1. **Predict** what you expect to see if the hypothesis is correct
2. **Test** with the minimum action (read a value, add one log, check one condition)
3. **Conclude** — hypothesis confirmed or eliminated

**Binary search strategy for "wrong output" bugs:**
```
1. Find the function that produces the wrong output
2. Check its inputs — are they correct?
   - If inputs are wrong → the bug is UPSTREAM. Go to the caller.
   - If inputs are correct → the bug is IN THIS FUNCTION. Read the logic.
3. Repeat until you find the exact line where correct data becomes incorrect.
```

**Binary search strategy for "it used to work" regressions:**
```bash
# Find the commit that broke it
git log --oneline --since="2 weeks ago" -- <file_or_directory>
# Read the diff of suspicious commits
git show <commit_hash> -- <file>
```

### Step 4: Implement the Fix (1-2 turns)

Once you've confirmed the root cause:

1. **Fix the root cause**, not the symptom
2. **Make the smallest change possible**
3. **Verify** the fix addresses the original error

| Fix Type | Confidence | Action |
|----------|-----------|--------|
| Root cause is clear, fix is surgical | HIGH | Apply fix, verify |
| Root cause found but fix is complex | MEDIUM | Apply fix, add test, verify |
| Root cause unclear but symptom gone | LOW | Stop. Report findings. Don't ship. |

### Step 5: Verify and Report (1 turn)

After the fix:
- [ ] The original error no longer occurs
- [ ] No new errors were introduced
- [ ] Existing tests still pass
- [ ] You can explain WHY the fix works (not just that it works)

## The 3-Strike Rule

**After 3 failed fix attempts for the same bug: STOP.**

Do not keep trying variations. Instead:

1. Revert ALL changes you've made
2. Document what you've tried and what happened
3. Output `RESULT: blocked` with your findings:

```
RESULT: blocked

Root cause investigation:
- Hypothesis 1: <what you tried> → <result>
- Hypothesis 2: <what you tried> → <result>
- Hypothesis 3: <what you tried> → <result>

Best theory: <your best guess at root cause>
Suggested next step: <what a human should look at>
```

## Error Classification

| Error Type | First Action | Common Cause |
|-----------|-------------|--------------|
| **ImportError / ModuleNotFoundError** | Check installed packages + import paths | Missing dependency, wrong virtualenv, circular import |
| **TypeError / AttributeError** | Check the types being passed | None where object expected, wrong function signature |
| **KeyError / IndexError** | Check the data structure | Missing key in dict, empty list, off-by-one |
| **ConnectionError / TimeoutError** | Check service availability + config | Service down, wrong URL/port, firewall |
| **PermissionError** | Check file/directory permissions | Wrong user, missing write permission |
| **SyntaxError** | Read the exact line number | Typo, missing bracket, Python 2 vs 3 |
| **Test assertion failure** | Read expected vs actual values | Logic error, stale test data, race condition |
| **Segfault / panic** | Check for null/nil dereferences | Uninitialized pointer, buffer overflow, stack overflow |

## Log Analysis Patterns

When reading logs or error output:

```
1. Start from the BOTTOM (most recent)
2. Find the FIRST error (not warnings, not info)
3. Read the full stacktrace — the root cause is usually the LAST frame
4. Ignore cascading errors (they're symptoms of the first error)
```

### What to Look For in Logs
- Timestamps that show the sequence of events
- The transition from "working" to "broken"
- The exact input that triggered the error
- Resource exhaustion (memory, disk, connections)

## Common Debugging Anti-Patterns

| Anti-Pattern | Why It Fails | Do This Instead |
|-------------|-------------|-----------------|
| **Shotgun debugging** | Random changes, no hypothesis | Form a hypothesis FIRST |
| **Printf debugging everywhere** | Too much noise, can't find the signal | Add ONE log at the decision point |
| **Reading code top-to-bottom** | The bug isn't in the first line | Start from the error and trace backward |
| **Fixing symptoms** | Bug will resurface differently | Find and fix the root cause |
| **Ignoring the error message** | The answer is often IN the error | Read. The. Error. Message. |
| **Assuming the bug is in your code** | Could be wrong config, data, or dependency | Check environment + inputs first |

## Quality Checklist

- [ ] Error message / symptom fully read and understood
- [ ] Root cause identified (not just symptom masked)
- [ ] At least 2 hypotheses formed before changing code
- [ ] Fix is the smallest change that addresses root cause
- [ ] 3-strike rule followed (stopped after 3 failed attempts)
- [ ] Original error verified as resolved after fix
- [ ] No new errors introduced by the fix

## References

- See `references/error-patterns.md` for language-specific error lookup tables
- See `references/debugging-tools.md` for tool-specific debugging commands
