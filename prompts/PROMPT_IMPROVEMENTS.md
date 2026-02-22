# Prompt Improvements — Before/After Comparison

Generated for Task #199: ForgeTeam Self-Improvement System
Date: 2026-02-09

## Problems Identified

Analysis of checkpoint files, orchestrator code, and task history revealed these recurring issues:

| # | Problem | Evidence | Impact |
|---|---------|----------|--------|
| 1 | Agents hit max_turns without producing structured output | Checkpoint files for tasks 131, 164, 188 all show `error_max_turns` with no RESULT block | Orchestrator cannot parse outcome; task flagged as failed/blocked |
| 2 | Agents spend excessive turns on environment/build issues | Agents spiral on `npm install` failures, missing venvs, wrong Python versions | Wasted turns; tasks time out before real work starts |
| 3 | Developers don't commit before running out of turns | Code is written but not committed; continuation agent starts from scratch | All previous work lost; duplicate effort |
| 4 | Tester exhausts turns on broken builds | Tester retries same broken build repeatedly instead of reporting blocked | Wastes Tester turn budget; delays feedback loop |
| 5 | No guidance on turn budget management | Agents have no awareness they are approaching turn limits | Agents run out mid-task with no output |
| 6 | Test discovery too rigid ("check ALL 5 strategies") | Forced exhaustive search wastes turns when CLAUDE.md has the answer | Unnecessary exploration; reduced time for actual testing |

---

## Changes by File

### `_common.md` — Shared Rules

| Section | Before | After | Rationale |
|---------|--------|-------|-----------|
| Turn Budget Awareness | *Not present* | New section: warns about limited turns, advises wrapping up at 70% budget, explains partial output > no output | **Addresses #1, #5**: agents now have explicit guidance to produce output before running out of turns |
| Build and Environment Errors | *Not present* | New section: try ONE fix, don't spiral, environment issues are blockers, max 3 turns on setup | **Addresses #2**: prevents agents from wasting entire session on env problems |
| Output Format | Brief mention of FILES_CHANGED | Expanded: marked as CRITICAL, explicit instruction to output block even when running low on turns, "none" guidance for FILES_CHANGED | **Addresses #1**: stronger emphasis ensures agents always produce parseable output |

### `developer.md` — Developer Agent

| Section | Before | After | Rationale |
|---------|--------|-------|-----------|
| Workflow | *Not present* | New section: 6-step ordered workflow (Read → Explore → Implement → Verify → Commit → Output) with emphasis on steps 5-6 | **Addresses #3**: gives agents a clear checklist where commit and output are explicit mandatory steps |
| Git Commit Requirements | One-liner "Always commit your work" | Expanded section: specific `git add`/`git commit` pattern, commit message format, avoid `git add .`, skip if no `.git` dir | **Addresses #3**: concrete instructions reduce ambiguity; agents know exactly what to do |
| Handling Build Errors | *Not present* | New section: read error carefully, fix own code, install deps, report env issues as blockers, max 2 attempts per build error | **Addresses #2**: prevents developer from spiraling on build problems |
| Task Status section | Contained `UPDATE tasks SET status` example SQL | Removed — replaced with "Recording Blockers and Decisions" section using only INSERT queries | **Reinforces _common.md rule**: agents should never update task status |
| Output Requirements | Basic "must end with" | Added: "If you are running low on turns: Stop, commit, output immediately" | **Addresses #1, #3**: explicit recovery strategy when turns are running low |

### `tester.md` — Tester Agent

| Section | Before | After | Rationale |
|---------|--------|-------|-----------|
| Test Discovery | "MUST try ALL strategies, do not stop after first" | "Stop as soon as you find a clear test command. If Strategy 1 gives a definitive answer, use it." | **Addresses #6**: CLAUDE.md usually has the test command; no need to exhaustively check all 5 strategies |
| Handling Build Failures | *Not present* | New section: report `RESULT: blocked` immediately for build failures, do not retry, includes common scenarios (npm, Python imports, TS errors, env vars) | **Addresses #4**: tester stops wasting turns on broken builds and gives fast feedback |
| Output block | No emphasis on always producing it | Added: "CRITICAL: Always produce this output block. Even if everything goes wrong, output with RESULT: blocked" | **Addresses #1**: ensures orchestrator can always parse tester output |
| FAILURE_DETAILS for blocked | Only for test failures | Now explicitly covers build/environment errors too | **Addresses #4**: developer gets actionable info about why the build is broken |
| No Tests Found | "you MUST have checked all 5 strategies" | "you MUST have checked at least strategies 1-3" | **Addresses #6**: strategies 4-5 are rarely necessary; saves turns |

---

## Expected Impact

| Metric | Current Behavior | Expected After |
|--------|-----------------|----------------|
| Structured output compliance | Agents frequently hit max_turns without RESULT block | Turn budget awareness + "output immediately" guidance should ensure 90%+ compliance |
| Build error recovery | Agents spiral 5-10 turns on env issues | Max 2-3 turns on env, then report blocked |
| Commit rate | Developers often skip commit | Explicit 6-step workflow with commit as mandatory step 5 |
| Tester turnaround | Tester exhausts turns on broken builds | Fast `RESULT: blocked` with build error details |
| Test discovery efficiency | All 5 strategies checked every time | Stop-early approach saves 2-4 turns per tester run |
| Turn utilization | Turns wasted on unrelated exploration | "Only read relevant files" guidance in workflow |

---

## Files Changed

- `prompts/_common.md` — Added Turn Budget Awareness, Build/Environment Errors, expanded Output Format
- `prompts/developer.md` — Added Workflow, Git Commit Requirements, Handling Build Errors, improved output guidance
- `prompts/tester.md` — Added Handling Build Failures, improved test discovery (stop-early), output compliance emphasis
- `prompts/PROMPT_IMPROVEMENTS.md` — This comparison document
