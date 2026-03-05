# Task Complexity Assessment Rubric

Score each dimension 1-3, sum for total. Use the total to determine approach.

## Dimensions

### 1. Scope (how many files?)
| Score | Criteria |
|-------|----------|
| 1 | 1-2 files change |
| 2 | 3-5 files change |
| 3 | 6+ files change, or new module/package |

### 2. Clarity (how specific is the task?)
| Score | Criteria |
|-------|----------|
| 1 | Specific file paths, function names, or error messages given |
| 2 | Feature described clearly but implementation details left open |
| 3 | Vague ("improve X", "add support for Y", "make it better") |

### 3. Pattern (is there a precedent?)
| Score | Criteria |
|-------|----------|
| 1 | Similar code exists in the codebase — follow the pattern |
| 2 | Standard industry pattern, but not yet in this codebase |
| 3 | Novel architecture decision required |

### 4. Risk (what breaks if you're wrong?)
| Score | Criteria |
|-------|----------|
| 1 | Isolated change, easy to revert |
| 2 | Touches shared code (models, utils, middleware) |
| 3 | Database migration, API contract change, or auth modification |

## Total Score → Approach

| Total | Complexity | Planning Approach |
|-------|-----------|-------------------|
| 4-5 | **Simple** | No explicit plan needed. Just implement. |
| 6-7 | **Medium** | 3-step plan. Verify after each step. |
| 8-9 | **Complex** | 5-step plan. Checkpoint every 10 turns. |
| 10-12 | **Epic** | Break into sub-tasks. Implement only the first sub-task. |

## Quick Decision: "Should I plan?"

If ANY of these are true, write a plan:
- You don't know which files to change
- The task involves a database migration
- The task involves changing an API contract
- The task description uses vague language
- A previous attempt at this task failed

If ALL of these are true, skip planning:
- You know exactly which file(s) to change
- The change is a straightforward code modification
- A similar pattern already exists in the codebase
- The change is isolated (no shared dependencies)
