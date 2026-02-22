# ForgeTeam Improvement Proposals

**Author:** Claude Opus 4.6 (after extensive hands-on usage across 2 sessions)
**Date:** 2026-02-08
**Updated:** 2026-02-08 — 11 of 16 improvements IMPLEMENTED + 3 new research-driven features
**Context:** Used ForgeTeam to build CryptoTrader v2 (7 dev tasks, 4 review tasks), ArrMada (6+ tasks), and managed orchestration from a remote Windows PC via SSH to Claudinator.

### Implementation Status
| # | Issue | Status |
|---|-------|--------|
| 1 | Orchestrator-side DB updates | DONE |
| 2 | Fix false BLOCKED verdicts | DONE |
| 3 | Tester early exit (via per-role turns) | DONE |
| 4 | Unbuffered output | DONE |
| 5 | Cost tracking | TODO |
| 6 | Parallel task execution | DONE |
| 7 | No hardcoded IPs | TODO (CLAUDE.md guidance) |
| 8 | Per-role turn limits | DONE |
| 9 | Auto dependency install | DONE |
| 10 | Cross-task context | TODO |
| 11 | Security blocks on CRITICAL | TODO |
| 12 | Integration test mode | TODO |
| 13 | Built-in batch mode (--tasks) | DONE |
| 14 | Completion notification | TODO |
| 15 | Per-project config | TODO |
| 16 | Auto-yes non-TTY | DONE |
| 17 | Checkpoint/Resume on timeout | DONE |
| 18 | Adaptive turn limits by complexity | DONE |
| 19 | Model tiering per-role/complexity | DONE |

---

## Executive Summary

ForgeTeam works. It took a project from zero to a full-stack application with 84 files in under 30 minutes of agent time. That said, I ran into enough friction and failure modes that I spent nearly as much time babysitting the orchestrator as it spent coding. Below are concrete issues ranked by impact, with proposed fixes.

---

## CRITICAL Issues (These cause tasks to fail or produce wrong results)

### 1. Agents Don't Update TheForge DB Status Reliably
**Problem:** Out of 7 Phase 1 CryptoTrader tasks, only 2 had their DB status updated to "done" by the agent. The other 5 completed all their code work but ran out of turns before the housekeeping SQL. I had to manually `UPDATE tasks SET status = 'done'` for tasks 110, 111, 112, 113, 114.

**Root Cause:** DB update is at the end of the agent's work. If it uses all its turns on coding, the TheForge update never fires.

**Proposed Fix:**
- **Option A (recommended):** Move DB status update OUT of the agent. The orchestrator should update the DB based on the agent's success/failure, not rely on the agent doing it. The orchestrator already has DB access — it should `UPDATE tasks SET status = 'done'` after a successful dev-test loop.
- **Option B:** Reserve the last 2 turns for housekeeping by adding explicit instructions: "With 2 turns remaining, stop coding and update TheForge."

**Impact:** High — every run requires manual cleanup without this fix.

### 2. "No Progress" Detection Causes False BLOCKED Verdicts
**Problem:** When a dev agent completes all work in cycle 1, cycle 2 sees no new file changes and marks the task as "BLOCKED — No file changes for 2 consecutive cycles." But the task IS done. Task 108 and 110 both hit this.

**Root Cause:** `parse_developer_output()` looks for `FILES_CHANGED` in the agent's output text, but agents don't consistently output this marker. Also, if all work is done in cycle 1, cycle 2 legitimately has nothing to change.

**Proposed Fix:**
- If the Tester passes in cycle 1, exit immediately (this already works).
- If the Tester returns "no-tests" or "unknown" AND the developer completed successfully, accept the result instead of running cycle 2.
- Add a "TASK_COMPLETE" output marker that agents can emit to signal they're done, separate from FILES_CHANGED.

**Impact:** High — creates confusing reports and sometimes prevents the DB from being updated correctly.

### 3. Tester Agent Frequently Returns "unknown (0/0 passed)"
**Problem:** The tester often can't find or run tests, returning `unknown` with 0 tests. This triggers the "no_tests" path, which accepts the dev result — but it means no actual testing happened.

**Root Cause:** The tester prompt assumes tests exist or can be written and run. For scaffolding tasks, documentation tasks, or tasks where the dev didn't create tests, the tester has nothing to do but burns through all its turns looking.

**Proposed Fix:**
- For tasks tagged as `type: scaffolding`, `type: docs`, or similar — skip the tester entirely.
- Add a `skip_tester` flag in the task or dispatch config.
- If the Tester finds no tests after 5 turns, exit early instead of burning 25+ turns.

**Impact:** Medium-high — wastes time and money on every non-code task.

---

## HIGH Issues (Significant friction, workarounds needed)

### 4. Python Output Buffering Makes Logs Empty During Execution
**Problem:** When running via `nohup`, the log file stays empty until the process completes because Python buffers stdout. Can't monitor progress.

**Root Cause:** Python's default stdout buffering in non-TTY mode.

**Proposed Fix:** Always use `python3 -u` (unbuffered) when spawning the orchestrator. Update the docs and any wrapper scripts. The batch script I created should use `python3 -u forge_orchestrator.py`.

**Impact:** Medium — makes monitoring impossible without checking files on disk.

### 5. Cost Tracking Shows $0.00
**Problem:** Every task summary shows `Cost: $0.0000 total`. Cost data isn't being captured.

**Root Cause:** The cost is parsed from Claude Code's JSON output, but the field may have changed format or isn't being emitted in the version on Claudinator.

**Proposed Fix:** Check `claude --version` output format. Parse the `costUSD` field from the usage JSON that Claude Code emits on exit. The raw data IS there (I saw it in task 115's output), it's just not being parsed correctly by `run_agent()`.

**Impact:** Medium — can't track spend per task or project.

### 6. No Parallel Task Execution Within a Project
**Problem:** Tasks 109-114 were independent (DB schemas, auth, pipeline, strategies, trading engine, frontend) but ran sequentially because the batch script processes them one at a time.

**Root Cause:** The orchestrator's `--task` mode runs one task. The `--auto-run` mode picks tasks across projects but doesn't parallelize within a project.

**Proposed Fix:**
- Add a `--parallel-tasks 109,110,111` mode that runs multiple tasks concurrently (respecting `max_concurrent`).
- Or: detect task dependencies (via a `depends_on` field in the tasks table) and auto-parallelize independent tasks.

**Impact:** Medium — CryptoTrader Phase 1 took ~25 minutes sequential. Could have been ~8 minutes parallel.

### 7. Hardcoded Host IPs in CLI Defaults
**Problem:** `data/cli.py` had `INTERNAL_HOST` hardcoded as defaults for `--questdb-host` and `--redis-url`. The agents copy IPs from the CLAUDE.md or task description into code defaults.

**Root Cause:** Agents see connection details in context and helpfully(?) embed them as defaults.

**Proposed Fix:**
- CLAUDE.md should say "NEVER hardcode IP addresses as defaults. Always load from settings/config."
- Add a post-generation check (linter or grep) for hardcoded IPs.

**Impact:** Medium — we fixed this manually but it'll recur.

---

## MEDIUM Issues (Quality of life improvements)

### 8. Max Turns Still Too Low for Complex Tasks
**Current:** 38 turns (bumped from 25).

**Observation:** Task 109 needed a retry. Task 115 (security review) hit 26 turns. Complex tasks with venv setup, dependency installation, AND coding regularly use 25-30 turns.

**Proposed Fix:** Bump to 50 for developer agents. Consider separate limits: `dev_max_turns: 50`, `tester_max_turns: 25`, `security_max_turns: 40`. The orchestrator already compacts context, so more turns don't mean infinite context growth.

### 9. No Dependency Installation Stage
**Problem:** Agents create `pyproject.toml` but then either spend turns creating a venv themselves, or the next agent can't import anything.

**Proposed Fix:** Add an automated pre-task step: if `pyproject.toml` exists and no `venv/` exists, run `python3 -m venv venv && venv/bin/pip install -e ".[dev]"` before spawning the agent. Same for `package.json` + `npm install`.

### 10. Agent Context Doesn't Include Other Agents' Recent Work
**Problem:** Task 111 (Binance pipeline) didn't know what Task 110 (auth) had created. Each agent starts fresh with only the task description and CLAUDE.md.

**Proposed Fix:** Before spawning an agent, generate a brief summary of recently-completed tasks and their file changes. Pass this as extra context. Example: "Task 110 created: api/app.py, auth/service.py, auth/models.py..."

### 11. Security Review Doesn't Block on Critical Findings
**Problem:** The security review runs and produces a report, but the task is marked "done" regardless of findings. Critical vulnerabilities should block deployment.

**Proposed Fix:** If the security reviewer finds CRITICAL findings, the orchestrator should:
- Mark the task as "blocked" instead of "done"
- Create new fix tasks automatically from the findings
- Log to TheForge open_questions

### 12. No Integration Test After All Phase Tasks Complete
**Problem:** Each task is tested independently, but nobody verifies the whole system works together.

**Proposed Fix:** Add a `--phase-complete` mode that runs after all tasks in a phase finish. It would:
- Attempt to start the application (`python main.py`)
- Run a basic smoke test (health check, can import all modules)
- Report any import errors or startup failures

---

## LOW Issues (Nice to have)

### 13. Batch Script Should Be Built-In
**Problem:** I had to write a custom bash script (`/tmp/cryptotrader-batch.sh`) to run multiple tasks. This should be a native orchestrator feature.

**Proposed Fix:** `--tasks 109,110,111,112,113,114 --sequential` or `--tasks 109-114`.

### 14. No Notification on Completion
**Problem:** Long-running batches complete silently. Had to poll the log file.

**Proposed Fix:** Add `--notify` flag that sends a Discord webhook, email, or writes to a known file when the batch completes.

### 15. Dispatch Config Could Have Per-Project Overrides
**Current:** Global `max_turns: 38` for all projects.

**Proposed Fix:**
```json
{
    "max_turns": 38,
    "project_overrides": {
        "CryptoTrader": { "max_turns": 50, "security_review": true },
        "ArrMada": { "max_turns": 25, "security_review": false }
    }
}
```

### 16. The `--yes` Flag Should Be Default for Non-Interactive
**Problem:** Forgot `--yes` and the process hung on a prompt via nohup.

**Proposed Fix:** Auto-detect non-TTY (stdin is not a terminal) and default to `--yes`.

---

## Priority Implementation Order

1. **#1 — Orchestrator-side DB updates** (eliminates manual cleanup)
2. **#2 — Fix false BLOCKED verdicts** (eliminates confusing results)
3. **#4 — Python unbuffered output** (one-line fix, huge debugging improvement)
4. **#16 — Auto-yes for non-TTY** (one-line fix, prevents hangs)
5. **#3 — Tester early exit when no tests** (saves time/money)
6. **#8 — Separate per-role turn limits** (prevents complex tasks from failing)
7. **#9 — Auto dependency install** (prevents import failures)
8. **#6 — Parallel task execution** (speed improvement)
9. **#11 — Security review blocks on CRITICAL** (security improvement)
10. Everything else

---

## Estimated Effort

| # | Issue | Effort | Impact |
|---|-------|--------|--------|
| 1 | DB status from orchestrator | 1-2 hours | Critical |
| 2 | Fix false BLOCKED | 1 hour | Critical |
| 3 | Tester early exit | 30 min | High |
| 4 | Unbuffered output | 5 min | High |
| 5 | Cost tracking | 30 min | Medium |
| 6 | Parallel tasks | 2-3 hours | High |
| 7 | No hardcoded IPs | 15 min (CLAUDE.md) | Medium |
| 8 | Per-role turn limits | 1 hour | Medium |
| 9 | Auto dep install | 1-2 hours | Medium |
| 10 | Cross-task context | 1 hour | Medium |
| 11 | Security blocks CRITICAL | 1 hour | Medium |
| 12 | Integration test mode | 2-3 hours | Medium |
| 13 | Built-in batch mode | 1 hour | Low |
| 14 | Completion notification | 30 min | Low |
| 15 | Per-project config | 1 hour | Low |
| 16 | Auto-yes non-TTY | 5 min | High |

**Total estimated effort:** ~15-20 hours for all improvements.
**Quick wins (< 30 min each):** #4, #7, #16 = massive reliability improvement.

---

*Generated from real-world usage building CryptoTrader v2 and ArrMada projects via ForgeTeam orchestration.*
