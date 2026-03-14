# EQUIPA Concurrency Guide

Benchmark results and tuning recommendations for running multiple agents in parallel.

---

## Benchmark Results (January 2026)

Two rounds of benchmarking were performed to establish safe concurrency limits for Claude Code CLI agents running under a Claude Max subscription.

### Phase 0: Single-Turn Benchmark

| Concurrent Processes | Turns Each | Total Runs | Success Rate | Throttled |
|---------------------|------------|------------|-------------|-----------|
| 1 | 1 | 5 | 100% | No |
| 2 | 1 | 6 | 100% | No |
| 4 | 1 | 8 | 100% | No |
| 8 | 1 | 10 | 100% | No |
| **Total** | | **29** | **100%** | **No** |

### Phase 0.5: Multi-Turn Benchmark

| Concurrent Processes | Turns Each | Total Runs | Success Rate | Throttled |
|---------------------|------------|------------|-------------|-----------|
| 4 | 4 | 10 | 100% | No |
| 8 | 4 | 10 | 100% | No |
| 12 | 4 | 15 | 100% | No |
| 16 | 4 | 15 | 100% | No |
| **Total** | | **50** | **100%** | **No** |

### Key Findings

- **API response times stay flat regardless of concurrency level.** Going from 1 to 16 concurrent agents produces no measurable increase in per-request latency.
- **Zero throttling observed.** 79 total test runs across 1-16 concurrent agents, every single one succeeded without rate limiting.
- **The true ceiling was not found.** 16 concurrent multi-turn agents was the maximum tested. The actual limit may be higher.
- **Local machine resources are the real bottleneck.** Each `claude` process consumes 200-400 MB of RAM. At 16 agents, that's 3-6 GB.

---

## Recommended Settings

### By Machine Spec

| RAM | CPU Cores | Recommended `max_concurrent` | Notes |
|-----|-----------|------------------------------|-------|
| 8 GB | 4 | 2-3 | Leave headroom for OS + browser |
| 16 GB | 8 | 4-6 | Comfortable for typical workloads |
| 32 GB | 12+ | 8-12 | Power user setup |
| 64 GB+ | 16+ | 12-16 | Maximum tested, likely safe to go higher |

### Default Settings

The `dispatch_config.json` defaults are conservative:

```json
{
    "max_concurrent": 4,
    "model": "sonnet",
    "max_turns": 25,
    "max_turns_developer": 50,
    "max_turns_tester": 20,
    "max_turns_security_reviewer": 40,
    "max_tasks_per_project": 5,
    "security_review": true,
    "model_tester": "haiku",
    "model_epic": "opus"
}
```

These work well on a 16 GB machine with 8 cores.

---

## Tuning `dispatch_config.json`

| Field | Default | What It Controls |
|-------|---------|-----------------|
| `max_concurrent` | 4 | Max agents running in parallel during auto-run |
| `model` | sonnet | Default model for all agents (sonnet, opus, haiku) |
| `max_turns` | 25 | Base conversation turns per agent invocation |
| `max_turns_developer` | 50 | Turn limit for Developer agents |
| `max_turns_tester` | 20 | Turn limit for Tester agents |
| `max_turns_security_reviewer` | 40 | Turn limit for Security Reviewer agents |
| `max_tasks_per_project` | 5 | Prevents one project from consuming all agent time |
| `security_review` | true | Run security audit after successful dev-test loops |
| `skip_projects` | [] | Project IDs or codenames to never auto-run |
| `priority_boost` | {} | Manual priority overrides: `{"myproject": 100}` |
| `only_projects` | [] | Whitelist mode — if non-empty, only these projects run |
| `model_tester` | *(none)* | Override model for Tester agents (e.g., `"haiku"`) |
| `model_developer` | *(none)* | Override model for Developer agents |
| `model_epic` | *(none)* | Override model for epic-complexity tasks (e.g., `"opus"`) |
| `model_simple` | *(none)* | Override model for simple-complexity tasks |

Per-role turn limits are adjusted by task complexity (simple: 0.5x, medium: 1.0x, complex: 1.5x, epic: 2.0x). Model priority: complexity model > role model > CLI `--model` > config `model`.

### Tuning Tips

**Increase `max_concurrent` if:**
- Your machine has spare RAM (check Task Manager during a run)
- Agents are completing quickly (under 2 minutes each)
- You have many projects with pending work

**Decrease `max_concurrent` if:**
- You see system slowdowns during runs
- Agents are timing out (1200s default)
- RAM usage exceeds 80%

**Adjust `max_tasks_per_project` if:**
- One project has dozens of tasks drowning out others (decrease)
- You want to focus on clearing a specific project's backlog (increase + `only_projects`)

**Use `priority_boost` to:**
- Force a specific project to the top of the queue
- Deprioritize projects you're not actively working on (negative values)

---

## Parallel Tasks Mode (Within a Project)

The `--tasks` flag runs multiple tasks concurrently within a single project:

```bash
# Run 3 tasks in parallel with dev-test loops
python forge_orchestrator.py --tasks 109,110,111 --dev-test -y

# Range syntax
python forge_orchestrator.py --tasks 109-114 --dev-test -y
```

This respects `max_concurrent` from dispatch_config.json. Each task spawns its own agent process. Use `--dry-run` to preview what would run.

---

## Parallel Goals Mode

For `--parallel-goals`, the concurrency limit comes from the goals JSON file:

```json
{
    "max_concurrent": 4,
    "goals": [...]
}
```

Override with `--max-concurrent N` on the command line.

The same machine-resource guidelines apply. Each goal runs a full Manager loop (Planner + Dev+Test + Evaluator), so each concurrent goal uses more resources than a single Dev+Test dispatch.

---

## Monitoring During Runs

Watch system resources during parallel execution:

- **Windows:** Task Manager > Performance tab (RAM, CPU)
- **Linux/Mac:** `htop` or `top`

If RAM exceeds 80% or CPU is pegged at 100%, reduce `max_concurrent` for next run.

Agent processes appear as `node` (Claude Code CLI runs on Node.js). Each `claude -p` invocation spawns a separate process.
