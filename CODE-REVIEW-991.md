# Code Review: Task #991 — Project Pombal Orchestrator (Cycle 5 — Semantic Analysis)
**Status**: Complete
**Reviewer**: code-reviewer agent (Cycle 5)
**Date**: 2026-03-05
**Scope**: Full line-by-line re-read of `forge_orchestrator.py` (5853 lines) focusing on semantic correctness, operator precedence bugs, task_role resolution inconsistency, memory leaks in module-level state, and connection leak accounting.

---

## Review Scope

Cycle 4 performs a line-by-line deep pass on the full orchestrator looking for new findings not caught in Cycles 1-3, plus cross-file consistency analysis of auxiliary files not previously reviewed.

| File | Lines | Status |
|------|-------|--------|
| `forge_orchestrator.py` | 5853 | Deep re-review (line-by-line) |
| `forge_arena.py` | ~450 | **NEW** — cross-file consistency |
| `forge_dashboard.py` | ~400 | **NEW** — cross-file consistency |
| `prepare_training_data.py` | ~200 | **NEW** — cross-file consistency |

**Total new files**: 3, ~1,050 lines. Full orchestrator re-read: 5,853 lines.

---

## Prior Cycle Status (Cycles 1-3)

- **55 total findings** across 3 cycles
- **4 CRITICAL, 8 HIGH, 11 MEDIUM, 3 LOW, 2 INFO** (Cycle 3 totals)
- **0 findings fixed** between any cycles
- **14 test failures** traced to 2 root causes (CR3-01, CR3-02)
- All prior findings **spot-checked and CONFIRMED** still present

---

## New Findings (Cycle 4)

### [HIGH] CR4-01: `forge_arena.py:58` — DB path points to wrong location

**File**: `forge_arena.py:58`
```python
THEFORGE_DB = SCRIPT_DIR.parent / "TheForge" / "theforge.db"
```

**Compare with** `forge_orchestrator.py:65`:
```python
THEFORGE_DB = Path(__file__).parent / "theforge.db"
```

**Issue**: `forge_arena.py` looks for the database at `../TheForge/theforge.db` (sibling directory named "TheForge"), while the orchestrator looks at `./theforge.db` (same directory). If both files are in `${PROJECT_BASE_DIR}/ProjectPombal/`, the arena will look at `${PROJECT_BASE_DIR}/TheForge/theforge.db` while the orchestrator uses `${PROJECT_BASE_DIR}/ProjectPombal/theforge.db`. These are **different databases**.

Additionally, `forge_arena.py` does NOT call `load_config()` and does NOT support `forge_config.json` or environment variable overrides. The arena is completely unaware of the portable configuration system.

**Impact**: The arena training loop will read/write a different database than the orchestrator, creating phantom tasks, orphaned episodes, and data inconsistency.

**Fix**: Import `THEFORGE_DB` from `forge_orchestrator` or `load_config()`, or at minimum use the same default path (`Path(__file__).parent / "theforge.db"`).

---

### [HIGH] CR4-02: `forge_orchestrator.py:1300` — Redundant `import re` inside function

**File**: `forge_orchestrator.py:1300`
```python
def compute_keyword_overlap(text_a, text_b):
    ...
    import re  # <-- redundant, re already imported at line 43
    words_a = set(re.findall(r'\w+', text_a.lower()))
```

**Issue**: `re` is already imported at module level (line 43). This inner import is dead weight and suggests the function was copy-pasted from a different context without cleanup. While not a runtime error, it indicates a code review gap and makes the function look like it was developed in isolation.

**Severity justification**: Elevated to HIGH because this pattern indicates the function may have been pasted from a standalone prototype. If the standalone version differed, there could be behavioral divergence not caught by review. The dedup/scoring pipeline relies on this function.

---

### [MEDIUM] CR4-03: `_check_repeated_tool_calls()` is dead code

**File**: `forge_orchestrator.py:2212-2220`
```python
def _check_repeated_tool_calls(tool_history, window=4):
    """Detect repeated identical tool calls within a sliding window."""
    if len(tool_history) < window:
        return False
    recent = tool_history[-window:]
    return len(set(recent)) == 1
```

**Issue**: This function is defined but **never called anywhere** in the codebase. It was superseded by `_detect_tool_loop()` (line 2223) which provides more sophisticated loop detection with error-awareness. This is dead code.

**Confirmed**: Cycle 1 flagged dead code in the orchestrator (CR-04/05/06), but `_check_repeated_tool_calls` was not listed — this is a new finding. While `log_agent_action()` (CR-05 from Cycle 1) was flagged as dead, it is actually defined (line 728) and ALSO never called. `bulk_log_agent_actions()` (line 753) is the one actually used. So `log_agent_action()` is confirmed dead code along with `_check_repeated_tool_calls`.

---

### [MEDIUM] CR4-04: `log_agent_action()` (line 728) — Dead code, superseded by `bulk_log_agent_actions()`

**File**: `forge_orchestrator.py:728-750`

**Issue**: The function `log_agent_action()` accepts a single action and inserts it. However, `run_agent_streaming()` accumulates all actions into `action_log` and then calls `bulk_log_agent_actions()` (line 2604). `log_agent_action()` is never called. It has 23 lines including a `try/except Exception: pass` that silently swallows ALL errors — even database corruption. This is both dead code and a swallowed-exception pattern.

---

### [MEDIUM] CR4-05: `get_action_summary()` — Only called from tests, never from production code

**File**: `forge_orchestrator.py:785-826`

**Issue**: Grepping the full codebase, `get_action_summary()` is called only from `test_agent_actions.py`. The docstring says "Get per-tool action summary for ForgeSmith analysis" but ForgeSmith never imports or calls it. It exists as public API but has no production consumer. The CHANGELOG lists it as synced from production, so it may be used externally — but within this codebase, it's unused production code.

---

### [MEDIUM] CR4-06: `forge_orchestrator.py:3574-3575` — Security finding count uses naive string matching

**File**: `forge_orchestrator.py:3574-3575`
```python
critical_count = result_text.lower().count("critical")
high_count = result_text.lower().count("high")
```

**Issue**: Counting `"critical"` and `"high"` as substrings matches false positives: `"highly"`, `"highway"`, `"critical mass"`, `"non-critical"`, `"Higher"`. For a security review summary, this can misreport finding counts. The `_extract_security_findings()` function (line 3596) uses proper pattern matching with severity markers — but the log message at 3576-3577 uses the naive count.

**Fix**: Use the already-extracted `findings` list from `_extract_security_findings()` (available at line 3583) instead of naive string counting.

---

### [MEDIUM] CR4-07: `forge_orchestrator.py:3651` — `_create_security_lessons()` leaks connection on exception

**File**: `forge_orchestrator.py:3651-3689`
```python
def _create_security_lessons(findings, project_id=None):
    conn = get_db_connection(write=True)
    created = 0
    for severity, description in findings:
        ...
    conn.commit()
    conn.close()
    return created
```

**Issue**: The connection is opened at line 3651 but `conn.close()` at line 3688 is not in a `finally` block. If `conn.execute()` on lines 3659, 3667, or 3679 raises an exception (e.g., `sqlite3.IntegrityError`, `sqlite3.OperationalError`), the connection leaks. This is the same pattern as the 13 functions flagged in Cycle 1 (CR-07) — confirming this function was also affected but not previously counted.

**Updated count**: Total functions with potential connection leaks: **14** (13 from CR-07 + this one).

---

### [MEDIUM] CR4-08: `forge_orchestrator.py:5819` — `main()` does raw `sys.argv` parsing instead of using argparse

**File**: `forge_orchestrator.py:5819`
```python
is_project_mode = "--project" in sys.argv and "--task" not in sys.argv and "--tasks" not in sys.argv
```

**Issue**: `main()` manually inspects `sys.argv` to determine if it should loop, rather than parsing args properly. This fragile approach will break if argument names change (e.g., `--project-id`), doesn't handle argument abbreviation (`--proj`), and doesn't account for positional interactions. The proper pattern is to run `argparse.parse_args()` once in `main()` and pass the namespace down — or restructure so `async_main()` returns a signal indicating whether to loop.

This is also the root cause of CR-01 (NameError at line 5834): `main()` can't access `args.project` because `args` is local to `async_main()`.

---

### [LOW] CR4-09: `forge_orchestrator.py:2397` — `turn_count` incremented per tool_use block, not per API turn

**File**: `forge_orchestrator.py:2397`
```python
elif block_type == "tool_use":
    ...
    turn_count += 1
```

**Issue**: `turn_count` is incremented for every `tool_use` block within an `assistant` message. But a single assistant message (one API turn) can contain multiple parallel tool calls. This means `turn_count` overcounts — a single API turn with 3 parallel tool calls registers as 3 turns. This inflates `num_turns` in the result dict and skews telemetry data (reported in agent_runs table).

The variable name `turn_count` is misleading — it's actually `tool_call_count`. The `result_data.get("num_turns", turn_count)` at line 2578 correctly prefers the API-reported count, but if `result_data` is None (no result message received), the fallback to `turn_count` is inaccurate.

---

### [LOW] CR4-10: `forge_orchestrator.py:3540-3552` — Security review description overwrites original task description

**File**: `forge_orchestrator.py:3540-3552`
```python
security_task = dict(task)  # copy
security_task["description"] = (
    f"Security review of code written for: {task['title']}. "
    f"...Original task description: {task['description']}"
)
```

**Issue**: The original `task['description']` is appended at the end of a long instruction string that's already ~500+ characters. If the description is long, the total string will be truncated when `build_task_prompt()` wraps it in `<task-input>` tags, potentially losing the original context. The task description should be preserved intact in a separate field rather than concatenated with instructions.

---

### [LOW] CR4-11: `forge_dashboard.py:47-51` — Dashboard has its own `sys.exit(1)` on missing DB

**File**: `forge_dashboard.py:49-50`
```python
if not db_path or not os.path.exists(db_path):
    print(f"Error: Database not found at '{db_path}'", file=sys.stderr)
    sys.exit(1)
```

**Issue**: Same `sys.exit()` in library code pattern as CR3-01, making `forge_dashboard.py` untestable without a real database.

---

### [LOW] CR4-12: `forge_arena.py` — Empty project directory strings

**File**: `forge_arena.py:64-88`
```python
PROJECT_PROFILES = {
    "apocrypha": {
        "id": 50, "codename": "apocrypha", "dir": "", ...
    },
    ...
}
```

**Issue**: All project profiles have `"dir": ""` (empty string). The arena would need PROJECT_DIRS or `forge_config.json` to resolve actual paths, but it doesn't import either. If run as-is, the arena will pass empty strings as project directories to the orchestrator.

---

### [INFO] CR4-13: Cross-file THEFORGE_DB inconsistency summary

| File | DB Path Pattern | Supports env var? | Supports config? |
|------|----------------|--------------------|--------------------|
| `forge_orchestrator.py` | `Path(__file__).parent / "theforge.db"` | No | Yes (forge_config.json) |
| `forgesmith.py` | `os.environ.get(...)` | Yes | No |
| `forgesmith_simba.py` | `os.environ.get(...)` | Yes | No |
| `forgesmith_gepa.py` | `os.environ.get(...)` | Yes | No |
| `forgesmith_backfill.py` | `os.environ.get(...)` | Yes | No |
| `forge_arena.py` | `SCRIPT_DIR.parent / "TheForge" / "theforge.db"` | No | No |
| `forge_dashboard.py` | `config.get("theforge_db", "")` | No | Yes |

**6 different strategies** across 7 files for locating the same database. This is the most significant consistency issue in the codebase. Any change to the DB location requires updating multiple files with different override mechanisms.

---

### [INFO] CR4-14: Planner and Evaluator bypass `build_cli_command()` and `get_role_model()`

**Files**: `forge_orchestrator.py:3881-3893` and `3930-3941`

Both `run_planner_agent()` and `run_evaluator_agent()` build their own CLI command lists inline instead of using `build_cli_command()`. They also use `args.model` directly instead of `get_role_model("planner", ...)`. This means:
- Planner/evaluator don't get role-specific skills directories
- They bypass complexity-based model tiering
- They bypass dispatch config per-role model overrides
- Any future changes to `build_cli_command()` won't apply to them

This was noted in Cycle 1-2 as CR-10/CR-11 but re-verified with specific line references and expanded impact.

---

## New Findings (Cycle 5)

### [HIGH] CR5-01: `forge_orchestrator.py:921` — Operator precedence bug in `task_type` assignment

**File**: `forge_orchestrator.py:921`
```python
task_type = task.get("role") or role if isinstance(task, dict) else role
```

**Issue**: Python operator precedence makes this parse as:
```python
task_type = (task.get("role") or role) if isinstance(task, dict) else role
```

Two bugs in one line:
1. **Wrong field**: The variable is named `task_type` but reads `task.get("role")`. The `role` field (e.g. "developer", "tester") is semantically different from `task_type` (e.g. "feature", "bugfix", "security"). This means episodes stored via `record_agent_episode()` will have `task_type="developer"` instead of `task_type="feature"`, breaking the cross-project episode matching in `get_relevant_episodes()` (line 1569: `WHERE role = ? AND task_type = ?`).
2. **Fallback confusion**: If `task.get("role")` is falsy (empty string, None), it falls back to the `role` parameter — which is the same thing. The intended fallback was likely `task.get("task_type", role)`.

**Impact**: Episode retrieval for MemRL is silently broken. Episodes are stored with role as task_type, so `get_relevant_episodes(role="developer", task_type="feature")` will never match them.

**Fix**: `task_type = task.get("task_type") or (task.get("role") or role) if isinstance(task, dict) else role`

---

### [HIGH] CR5-02: `forge_orchestrator.py:3196,5649` — Redundant `getattr()` on dict objects

**Files**: `forge_orchestrator.py:3196` and `5649`
```python
task_role = getattr(task, 'role', None) or (task.get('role') if isinstance(task, dict) else None) or "developer"
```

**Issue**: `task` is always a `dict` (returned by `fetch_task()` → `dict(row)`). Calling `getattr(task, 'role', None)` on a dict will ALWAYS return `None` because dicts don't have a `.role` attribute — they use `task['role']` or `task.get('role')`. The `getattr` is dead code that always falls through to the second clause.

There are also **5 different patterns** for resolving `task_role` across the file:
| Line | Pattern | Effective behavior |
|------|---------|-------------------|
| 921 | `task.get("role") or role` | Uses role field, falls back to param |
| 3196 | `getattr(task, 'role', None) or (task.get('role')...) or "developer"` | Always uses `task.get('role')` or "developer" |
| 5023 | `task.get("role") or "developer"` | Simple, correct |
| 5649 | Same as 3196 | Redundant |
| 5719 | Same as 5023 | Simple, correct |

This should be extracted into a single `get_task_role(task, default="developer")` helper.

---

### [MEDIUM] CR5-03: `forge_orchestrator.py:1805,1831` — `task_type` variable shadowed within `build_system_prompt()`

**File**: `forge_orchestrator.py:1805` and `1831`

Line 1805 sets:
```python
task_type = task.get("task_type", "feature") if isinstance(task, dict) else None
```
Then line 1831 reassigns the same variable:
```python
task_type = task.get("task_type", "feature") or "feature"
```

The second assignment shadows the first. The difference: line 1805 can return `None` (when task is not a dict), while line 1831 adds `or "feature"` fallback. Since `task` is always a dict in practice, both produce the same result. But the redundant reassignment is confusing — it looks like the code was written by two people who didn't see each other's work.

---

### [MEDIUM] CR5-04: `forge_orchestrator.py:1511,1825` — `_injected_episodes_by_task` memory leak

**File**: `forge_orchestrator.py:1511`
```python
_injected_episodes_by_task = {}  # module-level dict
```

**Issue**: Episode IDs are added at line 1825:
```python
_injected_episodes_by_task[task_id] = ep_ids
```

They're removed at line 1724 via `.pop()`:
```python
ep_ids = _injected_episodes_by_task.pop(task_id, [])
```

But `pop()` is only called from `update_injected_episode_q_values_for_task()`, which is called in exactly 3 places (lines 5035, 5731, 5799). If an exception occurs in `run_dev_test_loop()` **before** reaching one of those callsites (e.g., in `run_manager_loop` line 4021 where the exception is caught at line 4374), the episode IDs stay in the dict forever.

In long-running `--auto-run` sessions processing hundreds of tasks, this dict grows unboundedly. Each entry is a list of episode IDs (typically 3 ints = ~100 bytes), so for 1000 tasks it's ~100KB — not catastrophic but it's unbounded growth with no cleanup.

**Fix**: Wrap the `pop()` in a `finally` block, or clear the dict for a task_id in `run_dev_test_loop`'s exception handlers.

---

### [MEDIUM] CR5-05: `forge_orchestrator.py:1515` — `_last_prompt_version` is not thread/task-safe

**File**: `forge_orchestrator.py:1515`
```python
_last_prompt_version = {}  # module-level dict
```

**Issue**: Set at line 1780 inside `build_system_prompt()`, read at line 459 inside `record_agent_run()`. In parallel execution (auto-run, parallel-goals, parallel-tasks), multiple coroutines call `build_system_prompt()` for different roles concurrently. Since dict updates are not atomic across `await` points, the `_last_prompt_version[role]` for one coroutine's agent could be overwritten by another coroutine's `build_system_prompt()` call before the first coroutine reaches `record_agent_run()`.

Example: Coroutine A builds prompt for developer (sets `_last_prompt_version["developer"] = "v2"`). Before A's agent finishes, coroutine B builds prompt for developer with baseline (sets `_last_prompt_version["developer"] = "baseline"`). When A records telemetry, it reads "baseline" instead of "v2".

**Fix**: Pass `prompt_version` explicitly as a return value from `build_system_prompt()` rather than relying on module-level state.

---

### [MEDIUM] CR5-06: `forge_orchestrator.py:2428-2445` — Tool signature built only when `turn_has_file_change` is False

**File**: `forge_orchestrator.py:2420-2445`
```python
if turn_has_tool_calls and not is_exempt:
    if turn_has_file_change:
        turns_without_file_change = 0
    else:
        turns_without_file_change += 1
        # Build tool signature for repetition detection
        sig_parts = [tool_name]
        ...
        tool_history.append(tool_sig)
```

**Issue**: The tool signature is built using `tool_name` and `tool_input` from the LAST block in the `for block in content_blocks` loop. But this loop iterates over ALL content blocks in the assistant message — `tool_name`/`tool_input` are overwritten on each iteration. When an assistant message contains multiple tool_use blocks (parallel tool calls), only the LAST tool call's name/input are used for the signature.

This means loop detection is blind to the actual sequence of tools within a turn. If an agent makes 3 different tool calls in parallel but the last one happens to be the same as last turn's last tool, it triggers a false positive.

**Fix**: Build signatures per tool_use block (not per turn), or collect all tool_names from the turn for the signature.

---

### [LOW] CR5-07: `forge_orchestrator.py:5651` — `get_role_turns("developer", args, task=task)` ignores `task_role`

**File**: `forge_orchestrator.py:5651`
```python
task_role = getattr(task, 'role', None) or (task.get('role') if isinstance(task, dict) else None) or "developer"
dev_model = get_role_model(task_role, args, task=task)
dev_turns = get_role_turns("developer", args, task=task)  # <-- hardcoded "developer", ignores task_role
```

The model is resolved using `task_role` (line 5650) but turns are resolved using hardcoded `"developer"` (line 5651). If the task has `role="debugger"` (turns=30) but this code fetches turns for `"developer"` (turns=40), the agent gets the wrong turn budget.

Compare with line 3199 which correctly uses `task_role`:
```python
dev_turns_max = get_role_turns(task_role, args, task=task)
```

---

### [LOW] CR5-08: `forge_orchestrator.py:2428-2445` — `tool_sig` defined in else branch but `tool_errors` appended unconditionally

**File**: `forge_orchestrator.py:2422-2514`

The `tool_history` list is only appended inside the `else` branch (when there are no file changes, line 2445). But `tool_errors` is appended at line 2514 for EVERY tool result, regardless of branch. This means `tool_history` and `tool_errors` can get out of sync — `tool_errors` may have more entries than `tool_history`.

`_detect_tool_loop()` reads `tool_errors[-1]` (line 2244) and iterates `tool_errors[i]` (line 2258), which works because it bounds by `len(tool_history)`. But the mismatch means error context from file-modifying turns is lost, and the error at index `i` in `tool_errors` may not correspond to the signature at index `i` in `tool_history`.

---

### [INFO] CR5-09: `forge_orchestrator.py:3186-3190` — Direct DB status update bypasses `update_task_status()`

**File**: `forge_orchestrator.py:3187-3190`
```python
conn = get_db_connection(write=True)
conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
conn.commit()
conn.close()
```

The `run_dev_test_loop()` function directly updates task status at its start, bypassing `update_task_status()` (line 397) which handles error logging and missing-task checks. This is a one-off pattern — all other status updates go through `update_task_status()`. The connection also lacks a `finally` block.

---

### [INFO] CR5-10: `forge_orchestrator.py:5783-5786` — Single-agent mode labels all success as "tests_passed"

**File**: `forge_orchestrator.py:5782-5786`
```python
if result.get("early_terminated"):
    single_outcome = "early_terminated"
elif result["success"]:
    single_outcome = "tests_passed"
else:
    single_outcome = "developer_failed"
```

In single-agent mode (non-dev-test), there are no tests. But successful completion is labeled `"tests_passed"`, which is semantically incorrect for roles like security-reviewer, code-reviewer, or planner. This pollutes the `agent_runs` table with misleading outcome data, making telemetry queries unreliable for non-developer roles.

---

## Consolidated Finding Counts (All Cycles)

| Severity | Cycles 1-4 | Cycle 5 New | **Grand Total** |
|----------|-----------|-------------|-----------------|
| CRITICAL | 4 | 0 | **4** |
| HIGH | 10 | 2 | **12** |
| MEDIUM | 13 | 4 | **17** |
| LOW | 4 | 2 | **6** |
| INFO | 4 | 2 | **6** |
| **Total** | **35** | **10** | **45** |

*Note: Cycle 5 re-verified all prior findings are still open. CR4-02 (redundant import re) downgraded from HIGH to LOW in cycle 5 assessment — no runtime impact. Counts above use Cycle 4 original severity for continuity.*

**Files changed**: 0 (this is a review, not a fix)
**All 45 findings remain OPEN. Zero fixes across 5 review cycles.**

---

## Top 5 Recommendations (Priority Order)

1. **CR3-01 CRITICAL**: Replace `sys.exit(1)` in `get_db_connection()` with a raised exception. This alone fixes 10 test failures and makes all DB functions testable.

2. **CR-01/CR4-08 CRITICAL**: Fix `args.project` NameError in `main()` — guaranteed crash on `--project` completion. Restructure `main()` to parse args once and pass them down.

3. **CR5-01 HIGH**: Fix `task_type` assignment at line 921 — currently reads `task.get("role")` instead of `task.get("task_type")`. This silently breaks MemRL episode matching.

4. **CR-07/CR4-07 HIGH**: Create a `@contextmanager db_connection()` helper and apply to all **14+** leaking functions. Single fix for the systemic connection leak pattern.

5. **CR5-05 MEDIUM + CR5-02 HIGH**: Extract `get_task_role(task)` helper and pass `prompt_version` explicitly to eliminate 5 inconsistent patterns and the module-level race condition.

---

```
RESULT: issues-found
FILES_REVIEWED: 1
FINDINGS:
  - [CRITICAL] forge_orchestrator.py:386 — sys.exit(1) in get_db_connection() (CR3-01, still open)
  - [CRITICAL] forge_orchestrator.py:65 — THEFORGE_DB ignores env var override (CR3-02, still open)
  - [CRITICAL] forge_orchestrator.py:5834 — NameError: args.project in main() (CR-01, still open)
  - [CRITICAL] forge_orchestrator.py:118 — Comment/value mismatch 60% vs 0.8 (CR-02, still open)
  - [HIGH] forge_orchestrator.py:921 — task_type reads "role" field instead of "task_type", breaks MemRL episode matching (CR5-01 NEW)
  - [HIGH] forge_orchestrator.py:3196,5649 — Redundant getattr() on dict + 5 inconsistent task_role patterns (CR5-02 NEW)
  - [HIGH] forge_arena.py:58 — DB path ../TheForge/theforge.db differs from orchestrator (CR4-01, still open)
  - [HIGH] ollama_agent.py:548 — Incorrect success detection logic (CR3-03, still open)
  - [HIGH] ollama_agent.py:233 — lstrip("sudo ") character-set bug (CR3-04, still open)
  - [HIGH] forgesmith.py:1146-1150 — Broken type conversion in rollback (CR3-05, still open)
  - [HIGH] forgesmith.py:1141-1154 — rollback_change() no error handling (CR3-06, still open)
  - [HIGH] forgesmith.py:1002 — Division by zero, no guard (CR3-07, still open)
  - [HIGH] pombal_setup.py:672 — Shell injection in cron (CR3-08, still open)
  - [HIGH] forge_orchestrator.py — 14+ functions leak DB connections (CR-07+CR4-07, still open)
  - [HIGH] forge_orchestrator.py:290,1494 — Raw sqlite3.connect() bypasses get_db_connection() (still open)
  - [HIGH] forge_orchestrator.py:1300 — Redundant import re inside function (CR4-02, still open)
  - [MEDIUM] forge_orchestrator.py:1805,1831 — task_type variable shadowed within build_system_prompt() (CR5-03 NEW)
  - [MEDIUM] forge_orchestrator.py:1511 — _injected_episodes_by_task memory leak on exception paths (CR5-04 NEW)
  - [MEDIUM] forge_orchestrator.py:1515 — _last_prompt_version race condition in parallel execution (CR5-05 NEW)
  - [MEDIUM] forge_orchestrator.py:2428-2445 — Tool signature uses last block only, blind to parallel tool calls (CR5-06 NEW)
  - [MEDIUM] forge_orchestrator.py:2212-2220 — _check_repeated_tool_calls() dead code (CR4-03, still open)
  - [MEDIUM] forge_orchestrator.py:728-750 — log_agent_action() dead code (CR4-04, still open)
  - [MEDIUM] forge_orchestrator.py:785-826 — get_action_summary() no production callers (CR4-05, still open)
  - [MEDIUM] forge_orchestrator.py:3574 — Naive "critical"/"high" substring counting (CR4-06, still open)
  - [MEDIUM] forge_orchestrator.py:3651 — _create_security_lessons() connection leak (CR4-07, still open)
  - [MEDIUM] forge_orchestrator.py:5819 — main() raw sys.argv parsing (CR4-08, still open)
  - [MEDIUM] forge_orchestrator.py:2296,2306 — Dead variables (CR3-09, still open)
  - [MEDIUM] forge_orchestrator.py:5661-5663 — Checkpoint loaded but never used (CR3-11, still open)
  - [MEDIUM] 4 files — Duplicate get_db()/log() (CR3-12, still open)
  - [MEDIUM] db_migrate.py:376-382 — Overly broad exception catch (CR3-13, still open)
  - [MEDIUM] forge_orchestrator.py:278 — _discover_roles() replaces ROLE_PROMPTS (still open)
  - [MEDIUM] forge_orchestrator.py:2297 — Unbounded tool_history O(n^2) (still open)
  - [LOW] forge_orchestrator.py:5651 — get_role_turns("developer") ignores task_role (CR5-07 NEW)
  - [LOW] forge_orchestrator.py:2428-2514 — tool_history/tool_errors list length mismatch (CR5-08 NEW)
  - [LOW] forge_orchestrator.py:2397 — turn_count counts tool_use not API turns (CR4-09, still open)
  - [LOW] forge_orchestrator.py:3540 — Security review overwrites task description (CR4-10, still open)
  - [LOW] forge_dashboard.py:49 — sys.exit() in library code (CR4-11, still open)
  - [LOW] forge_arena.py:64-88 — Empty project directory strings (CR4-12, still open)
  - [INFO] forge_orchestrator.py:3187-3190 — Direct DB status update bypasses update_task_status() (CR5-09 NEW)
  - [INFO] forge_orchestrator.py:5783-5786 — Single-agent mode labels all success as "tests_passed" (CR5-10 NEW)
  - [INFO] 7 files — 6 different DB path strategies (CR4-13, still open)
  - [INFO] forge_orchestrator.py:3881,3930 — Planner/evaluator bypass build_cli_command (CR4-14, still open)
  - [INFO] forge_orchestrator.py — 5853-line monolith, 86 functions (CR3-22, still open)
  - [INFO] test files — Tests use production database (CR3-23, still open)
CRITICAL_COUNT: 4
HIGH_COUNT: 12
MEDIUM_COUNT: 17
LOW_COUNT: 6
INFO_COUNT: 6
TOP_RECOMMENDATIONS:
  1. Fix CR3-01: Replace sys.exit(1) in get_db_connection() with exception (fixes 10 test failures)
  2. Fix CR-01/CR4-08: Restructure main() to parse args once — fixes NameError crash
  3. Fix CR5-01: Change line 921 from task.get("role") to task.get("task_type") — fixes broken MemRL
  4. Fix CR-07/CR4-07: @contextmanager db_connection() for all 14+ leaking functions
  5. Fix CR5-02/CR5-05: Extract get_task_role() helper + pass prompt_version explicitly
SUMMARY: Cycle 5 semantic analysis of forge_orchestrator.py (5853 lines). Found 10 new issues (2 HIGH, 4 MEDIUM, 2 LOW, 2 INFO). Most critical new finding: CR5-01 — line 921 assigns task.get("role") to task_type variable, silently breaking MemRL episode retrieval. Also found 5 inconsistent patterns for resolving task_role (CR5-02), module-level state race conditions in parallel execution (CR5-05), and a memory leak in _injected_episodes_by_task (CR5-04). Grand total: 45 unique findings (4C/12H/17M/6L/6I) across 5 cycles. Zero findings fixed. Review approaching saturation — recommend fixing existing findings before further review iterations.
```
