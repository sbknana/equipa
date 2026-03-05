# Tester Agent Skills

Skills for the Project Pombal tester agent role. Address the top tester failure modes: inability to find test commands, test hangs/timeouts, and no existing tests to run.

## Skills

| Skill | Purpose |
|-------|---------|
| `framework-detection` | Identify test framework and find the correct test command fast |
| `test-generation` | Write effective tests when none exist |

## Usage

Loaded automatically by the orchestrator when dispatching tester agents:
```bash
claude -p --add-dir skills/tester/ ...
```
