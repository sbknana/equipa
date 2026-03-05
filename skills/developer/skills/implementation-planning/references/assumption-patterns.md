# Handling Ambiguous Requirements

When task descriptions are vague, use these patterns to make safe assumptions.

## The Assumption Hierarchy

Always choose the option that is:
1. **Most conventional** for the framework/language in use
2. **Simplest** to implement correctly
3. **Easiest** to change later if the assumption was wrong

## Common Ambiguities and Safe Defaults

### "Add authentication"
- **Default:** Session-based auth if the app already has a user model
- **Default:** JWT if it's a stateless API
- **Never assume:** OAuth/SSO unless explicitly asked (too complex for a single task)

### "Add a new endpoint/route"
- **Default:** Follow the exact same pattern as existing routes in the project
- **Default:** RESTful naming conventions
- **Default:** Return JSON for APIs, render template for web apps
- **Document:** Request/response schema you chose

### "Fix the bug in X"
- **Default:** The bug is the most obvious incorrect behavior in that area
- **Default:** The fix should not change the public API
- **Default:** Add a test that demonstrates the fix
- **Document:** What you identified as the root cause

### "Add tests"
- **Default:** Use the test framework already in the project
- **Default:** Unit tests, not integration tests
- **Default:** Test the happy path + one error case
- **Never assume:** 100% coverage is required (diminishing returns)

### "Improve performance"
- **Default:** Fix the most obvious N+1 query or unnecessary loop
- **Default:** Add caching only if an existing caching layer exists
- **Never assume:** You should change the architecture

### "Add logging"
- **Default:** Use the logging library already in the project
- **Default:** Log at INFO level for success, ERROR for failures
- **Default:** Include request ID / correlation ID if the pattern exists
- **Never assume:** You need to add a logging framework

### "Add validation"
- **Default:** Validate at the API boundary (request handlers)
- **Default:** Return 400 with descriptive error messages
- **Default:** Use the validation library already in the project
- **Never assume:** You need client-side validation too

## Documenting Assumptions

Always include assumptions in your output:

```
DECISIONS:
- Assumed session-based auth (project uses Express + Passport)
- Used existing User model, added 'role' column
- Login endpoint returns 200 + session cookie (not JWT)
ALTERNATIVES_CONSIDERED:
- JWT: Would require token refresh infrastructure
- OAuth: Too complex for a single task, could be added later
```
