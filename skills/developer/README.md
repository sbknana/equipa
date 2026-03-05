# Developer Agent Skills

Skills for the Project Pombal developer agent role. These address the top failure modes observed in production: analysis paralysis (50% early termination rate), complex task stalling, and unrecoverable errors.

## Skills

| Skill | Purpose |
|-------|---------|
| `codebase-navigation` | Systematic approach to understanding unfamiliar codebases without getting lost |
| `implementation-planning` | Decompose complex tasks into checkpointed steps |
| `error-recovery` | Structured recovery when builds break, tests fail, or you're stuck |

## Usage

Loaded automatically by the orchestrator when dispatching developer agents:
```bash
claude -p --add-dir skills/developer/ ...
```
