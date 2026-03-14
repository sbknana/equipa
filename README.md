<h1 align="center">EQUIPA</h1>

<p align="center">
  <strong>Your AI development team.</strong>
</p>

<p align="center">
  <em>European Portuguese for "team" — a self-improving AI agent orchestrator that builds, reviews, tests, and secures your code.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/license-Apache%202.0-orange" alt="Apache 2.0">
  <img src="https://img.shields.io/badge/tests-280%2B-success" alt="280+ Tests">
</p>

---

You describe what you want in plain English. Equipa breaks it into tasks, dispatches specialized AI agents, and iterates until the work is done. Developers write code, testers validate it, security reviewers audit it — all autonomously, all in parallel.

No boilerplate. No manual task management. One sentence in, shipped code out.

---

## See It in Action

```
You: "Build user authentication with Google OAuth"

Equipa:
  Planning: broke feature into 5 tasks with dependency graph
  Tasks 1-3 dispatched in parallel (no dependencies)
  Task 4 waiting on task 2 (needs routes)
  Task 5 waiting on all (integration tests)

  OAuth config            -> src/config/auth.ts
  Login/callback routes   -> /auth/google, /auth/callback
  Session middleware       -> signed cookies
  Login UI component      -> Google branding guidelines
  8 integration tests     -> all passing

  "Want me to run a security review on the auth flow?"
```

```
You: "Users are getting 500 errors on checkout"

Equipa:
  Dispatched debugger agent
  Root cause: null pointer in calculateTotal() — discounted items with no original price
  Dispatched developer agent -> fix applied
  Dispatched tester agent -> 4 new edge case tests, all passing
  Done.
```

```
You: "Review the payment module for security issues"

Equipa:
  Dispatched security reviewer (Trail of Bits tooling)
  MEDIUM: Session tokens not rotated after privilege escalation
  LOW: CSRF token missing on logout endpoint
  "Want me to create fix tasks for these?"
```

One sentence from you. Full implementation with tests.

---

## What Makes Equipa Different

Most agent frameworks give you a chatbot that writes code. Equipa gives you a **development team** — with memory, specialization, and the ability to learn from its own mistakes.

**It actually improves over time.** Every agent run is recorded as a structured episode. ForgeSmith analyzes failures, extracts lessons, and injects them into future prompts. GEPA evolves prompts using DSPy-style optimization. SIMBA generates behavioral rules from high-variance outcomes. Your agents tomorrow are better than your agents today.

**It doesn't let agents spiral.** Loop detection catches stuck agents repeating the same failed action. Monologue detection kills agents that talk instead of using tools. Turn budgets scale with task complexity. Cost breakers prevent runaway spending. If an agent gets stuck, the system warns it — then terminates it.

**Security isn't an afterthought.** Seven Trail of Bits security skills ship out of the box: static analysis (Semgrep + CodeQL), variant analysis, audit context building, differential review, fix validation, custom Semgrep rule creation, and dangerous API detection. Security reviews auto-dispatch after dev-test cycles.

---

## Agent Roles

| Role | What It Does |
|------|-------------|
| **Developer** | Writes code. Loads codebase navigation, implementation planning, and error recovery skills. |
| **Tester** | Writes and runs tests. Validates developer output. Iterates in dev-test loops. |
| **Security Reviewer** | Deep audit with 7 Trail of Bits skills. Static analysis, variant analysis, sharp-edge detection. |
| **Code Reviewer** | Quality, patterns, best practices. Architecture-level feedback. |
| **Debugger** | Investigates bugs with hypothesis-driven 5-step methodology. Traces root causes. |
| **Planner** | Breaks complex features into task lists with dependency graphs. |
| **Frontend Designer** | UI/UX focused development. |
| **Evaluator** | Assesses implementations against requirements. |
| **Integration Tester** | Tests how components work together across boundaries. |

You can also [create custom agent roles](docs/CUSTOM_AGENTS.md) by dropping a `.md` file in `prompts/`.

---

## Key Features

### Dev-Test Loops
Developer writes code, tester validates it. They iterate with compacted context from prior cycles until tests pass or budget runs out. This mirrors how human teams actually work.

### Parallel Task Execution
Independent tasks run simultaneously in isolated git worktrees. No filesystem conflicts. Merged branches are cleaned up; unmerged branches are preserved for manual recovery.

### Persistent Memory
Every agent run, test result, and lesson learned is stored in a 30+ table SQLite database. Agents automatically query past episodes, relevant lessons, and project context before starting work. The system builds institutional knowledge about *your* codebase.

### Self-Improving Agents
Three feedback mechanisms run continuously:
- **Lessons** — Text patterns extracted from failures, injected into future prompts
- **GEPA** — DSPy-style prompt evolution with A/B testing and automatic rollback
- **SIMBA** — Behavioral rules generated from high-variance outcomes with effectiveness scoring

### QLoRA Fine-Tuning
Export your agent performance data and fine-tune local models on it. The Arena module runs automated stress tests and generates training data. Train with QLoRA/PEFT on consumer GPUs.

### Anti-Paralysis Guardrails
- **Loop detection** — Catches agents repeating the same failed action
- **Monologue detection** — Kills agents that talk instead of using tools
- **Alternating pattern detection** — Catches A-B-A-B oscillation
- **Dynamic turn budgets** — Scale with task complexity (simple: 0.5x, epic: 2.0x)
- **Cost breakers** — Hard spend limits per task

### Security Pipeline
Seven Trail of Bits security skills baked in:

| Skill | Purpose |
|-------|---------|
| Static Analysis | Semgrep + CodeQL scanning |
| Variant Analysis | Find similar vulnerabilities across codebase |
| Audit Context Building | Understand security-relevant architecture |
| Differential Review | Security impact of code changes |
| Fix Review | Validate that security fixes actually fix the issue |
| Semgrep Rule Creator | Generate custom rules for project-specific patterns |
| Sharp-Edge Detection | Flag dangerous APIs and patterns |

### Multi-Model Support
Route tasks to different models based on role and complexity. Use Claude for complex work, local Ollama models for cost-sensitive tasks. Configurable per-role in `dispatch_config.json`.

### Multi-Tool Compatibility
Works with Claude Code, Roo Code, Cline, Cursor, Windsurf, and Continue.dev. The setup wizard auto-generates MCP configuration for your tool of choice.

---

## Architecture

```
You describe what you want
        |
        v
Equipa breaks it into tasks (with dependency graph)
        |
        v
Dispatches specialized agents in parallel
        |
        v
Dev-test loops iterate until code works
        |
        v
Results scored, episodes recorded, lessons extracted
        |
        v
ForgeSmith evolves prompts for next time
```

Everything flows through a single SQLite database (30+ tables). No Redis. No message queues. No infrastructure overhead. One file you can back up with `cp`.

For the full architecture with Mermaid diagrams, see [ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick Start

### Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | [python.org](https://python.org) |
| An MCP-compatible AI coding tool | Claude Code, Cursor, Roo Code, Cline, Windsurf, or Continue.dev |
| git | [git-scm.com](https://git-scm.com) |
| uvx / uv | [docs.astral.sh/uv](https://docs.astral.sh/uv) |

You also need an LLM provider — Anthropic API key, Claude Pro/Max subscription, or local models via Ollama.

### Install

```bash
git clone https://github.com/sbknana/equipa.git
cd equipa
python equipa_setup.py
```

The setup wizard handles everything: prerequisite checks, database creation, config generation, and MCP integration. Just answer the prompts.

### Your First Task

```bash
# Open your AI coding tool in the Equipa directory
claude

# Then just talk to it:
> "Add a new project called MyApp at ~/myapp"
> "Create a task: set up the database schema"
> "Work on that task"
```

Or use the CLI directly:

```bash
# Single agent
python forge_orchestrator.py --task 1 -y

# Dev + test loop (recommended)
python forge_orchestrator.py --task 1 --dev-test -y

# Goal-driven mode — describe the end state, Equipa plans the rest
python forge_orchestrator.py --goal "Add dark mode with system preference detection" --goal-project 1

# Auto-run — scan all projects, dispatch by priority
python forge_orchestrator.py --auto-run -y

# Dry run — see what would happen without executing
python forge_orchestrator.py --task 1 --dry-run
```

For the full setup guide, see [QUICKSTART.md](docs/QUICKSTART.md).

---

## How Self-Improvement Works

```
Agent completes a task
        |
        v
Episode recorded (outcome, errors, reflection, files changed)
        |
        v
ForgeSmith analyzes recent episodes
        |
        |-->  Extracts lessons from failures
        |     (sanitized against prompt injection)
        |
        |-->  GEPA evolves role prompts
        |     (DSPy-style optimization with A/B testing)
        |
        |-->  SIMBA generates behavioral rules
        |     (from high-variance outcomes)
        |
        +-->  Impact assessment gates risky changes
              (blast-radius analysis before applying mutations)
        |
        v
Improved prompts + lessons injected into next run
```

Every lesson is sanitized before injection — stripping XML tags, role overrides, base64 payloads, and code blocks that could poison the learning pipeline. Changes above a risk threshold require manual approval.

The result: agents that failed at a pattern last week won't fail at it the same way this week.

---

## Tech Stack

- **Pure Python** — Zero pip dependencies. stdlib only. Copy files and run.
- **SQLite** — 30+ tables, single file, zero infrastructure.
- **Claude API** — Primary LLM provider (Anthropic).
- **Ollama** — Optional local model support for cost-sensitive tasks.
- **Trail of Bits** — 7 security skills using Semgrep and CodeQL.
- **QLoRA/PEFT** — Fine-tune local models on your own agent performance data.

---

## Project Stats

- **~7,000 lines** of orchestrator code
- **280+ tests** covering loop detection, lesson injection, sanitization, scoring, and more
- **30+ database tables** tracking every aspect of agent behavior
- **12 test suites** validating core subsystems
- **9 agent roles** with per-role skills, prompts, and turn budgets
- **7 security skills** from Trail of Bits methodology
- **Zero pip dependencies** — pure Python stdlib

---

## Documentation

| Doc | What It Covers |
|-----|-------------|
| [Architecture](docs/ARCHITECTURE.md) | System design, data flow, Mermaid diagrams, key decisions |
| [Quick Start](docs/QUICKSTART.md) | Step-by-step getting started guide |
| [User Guide](docs/USER_GUIDE.md) | Comprehensive usage documentation |
| [Orchestrator](docs/ORCHESTRATOR.md) | CLI commands, flags, advanced usage |
| [Custom Agents](docs/CUSTOM_AGENTS.md) | Create your own agent roles |
| [Capabilities](docs/CAPABILITIES.md) | Deep dive: ForgeSmith, security pipeline, benchmarks |
| [API Reference](docs/API.md) | Module-level API documentation |

---

## Contributing

Contributions welcome. If you're using AI coding tools to build software, this is for you.

1. Fork the repo
2. Create a branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Test with `--dry-run` to verify prompt generation
5. Submit a PR

---

## License

[Apache License 2.0](LICENSE) — use it commercially, modify it, distribute it. Just include the license and attribution.

---

<p align="center">
  <strong>Built by <a href="https://github.com/Forgeborn">Forgeborn</a></strong><br>
  Vibe coded with Claude<br>
  <em>&copy; 2026 Forgeborn</em>
</p>
