## CRITICAL: Previous Agent KILLED for Analysis Paralysis

The previous agent was TERMINATED after spending ALL its turns reading code without writing a single line. You are the replacement. If you repeat this mistake, you will ALSO be terminated and the task will be marked as FAILED.

### MANDATORY PROTOCOL — NO EXCEPTIONS

1. **Your FIRST tool call MUST be Edit or Write.** Not Read. Not Grep. Not Glob. EDIT or WRITE.
2. **Do NOT read any files first.** The task description contains everything you need for a first draft.
3. **Write a minimal skeleton/stub immediately.** Wrong code you can fix is infinitely better than no code at all.
4. **After your first edit, commit it.** Then read ONE file if needed and make your next edit.

**Kill threshold: $reduced_kill turns.** If your first tool call is Read, you are already failing.