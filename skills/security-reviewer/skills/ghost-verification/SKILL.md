---
name: ghost-verification
description: >
  Verify whether a previously-resolved security finding is truly fixed or still present
  in the codebase. Used by ForgeSmith nightly review to detect ghost findings — security
  issues marked as resolved but still exploitable. Takes a finding description and code
  path as input; outputs a structured verdict with evidence.
  Triggers: nightly review, security finding verification, ghost finding check, resolved
  finding audit, security regression detection.
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Ghost Finding Verification

## Purpose

You are verifying whether a **resolved security finding** is actually fixed in the codebase.
A "ghost finding" is a security issue that was marked as resolved but the vulnerability
still exists in the code.

## Input

You will receive:
- **Finding description:** What the security issue was (e.g., SQL injection in user endpoint)
- **Code path:** The file(s) and location(s) where the vulnerability was reported
- **Resolution context:** How the finding was supposedly resolved (task ID, decision rationale)

## Verification Process

### Step 1: Locate the Code (1 turn)

Read the file(s) at the specified code path. If the file has moved or been renamed,
use Glob/Grep to find it. If the file was deleted, that counts as VERIFIED (the
vulnerable code no longer exists).

### Step 2: Check for the Vulnerability Pattern (1-2 turns)

Look for the specific vulnerability pattern described in the finding:

| Finding Type | What to Look For |
|-------------|-----------------|
| SQL injection | String concatenation/f-strings in SQL queries, missing parameterization |
| XSS | Unsanitized user input rendered in HTML, missing escaping |
| Command injection | User input in subprocess/os.system calls, missing shell escaping |
| Path traversal | User input in file paths without normalization/validation |
| Hardcoded secrets | API keys, passwords, tokens in source code |
| Missing auth | Endpoints without authentication/authorization checks |
| Insecure deserialization | pickle.loads, yaml.load without SafeLoader on untrusted input |
| SSRF | User-controlled URLs in HTTP requests without allowlist |

### Step 3: Check the Fix (1-2 turns)

If a fix was applied:
1. Verify the fix actually addresses the root cause (not just a symptom)
2. Check for bypass opportunities (e.g., fix on one endpoint but not another)
3. Look for the same pattern elsewhere in the file or related files

### Step 4: Render Verdict (final turn)

## Output Format

You MUST output exactly one of these two verdict blocks at the END of your response.
The ForgeSmith parser looks for these exact patterns.

### If the finding is genuinely fixed:

```
VERDICT: VERIFIED
EVIDENCE: [1-3 sentences describing what fix was applied and why it's sufficient]
```

### If the vulnerability still exists:

```
VERDICT: STILL_PRESENT
EVIDENCE: [1-3 sentences describing where the vulnerability remains and what's wrong]
SEVERITY: [critical|high|medium|low]
```

## Rules

- **Be conservative.** If you are unsure whether the fix is complete, report STILL_PRESENT.
- **Check related code.** A fix in one location doesn't help if the same pattern exists nearby.
- **Evidence is required.** Never output a verdict without citing specific code or file paths.
- **Stay focused.** You are verifying ONE specific finding, not doing a full security audit.
- **Max 10 turns.** You have a strict turn budget. Do not waste turns on unrelated exploration.

## Quality Checklist

Before rendering your verdict:
- [ ] I read the actual code at the reported location
- [ ] I checked whether the vulnerability pattern is present or absent
- [ ] I checked for the same pattern in related files (if applicable)
- [ ] My evidence cites specific file paths and code patterns
- [ ] My verdict is one of exactly: VERIFIED or STILL_PRESENT
