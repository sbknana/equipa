# Custom Agent Creation Guide

ForgeTeam agents are defined by markdown prompt files. Creating a new agent is as simple as dropping a `.md` file in the `prompts/` directory.

---

## How Agent Roles Work

Each agent role is a markdown file in the `prompts/` directory. When the orchestrator spawns an agent, it:

1. Reads `_common.md` (shared rules for all agents)
2. Reads the role-specific `.md` file (e.g., `developer.md`)
3. Prepends the common rules to the role prompt
4. Adds the task-specific instructions
5. Passes everything as the system prompt to `claude -p`

The `_discover_roles()` function scans `prompts/` at startup and automatically registers any `.md` file (except those starting with `_`) as an available role.

### Built-in Roles

ForgeTeam ships with 9 built-in agent roles:

| Role File | Purpose |
|-----------|---------|
| `developer.md` | Write code, fix bugs, implement features |
| `tester.md` | Run unit tests and report results |
| `planner.md` | Break goals into ordered tasks |
| `evaluator.md` | Verify goal completion, create follow-ups |
| `security-reviewer.md` | 4-phase code security audit |
| `frontend-designer.md` | Create polished, production-grade UI/UX |
| `integration-tester.md` | Deploy and test full applications end-to-end |
| `debugger.md` | Trace errors to root cause and fix them |
| `code-reviewer.md` | Review code quality, consistency, correctness |

---

## Creating a New Agent

### 1. Create the Prompt File

Create a new `.md` file in your `prompts/` directory:

```
prompts/documentation-writer.md
```

The filename stem becomes the role name: `documentation-writer`.

### 2. Write the Prompt

Your prompt file should include these sections:

```markdown
# Documentation Writer Agent

## Role
You are a documentation writer for TheForge projects. Your job is to create
and update project documentation based on the current codebase.

## Responsibilities
- Read the codebase to understand architecture and features
- Create/update README.md, API docs, and user guides
- Follow the project's existing documentation style
- Keep docs accurate and concise

## Output Format
Your response MUST be valid JSON with this structure:

{
    "status": "done" | "blocked",
    "summary": "What you did",
    "files_changed": ["list", "of", "files"],
    "notes": "Any additional context"
}

Always include `RESULT:` followed by your JSON output.

## Tool Permissions
- Read: YES (read any file)
- Write: YES (create/edit documentation files only)
- Edit: YES (documentation files only)
- Bash: YES (read-only commands like ls, find, grep)
- TheForge MCP: YES (read project context, update task status)

## Constraints
- NEVER modify source code files
- NEVER delete existing documentation without replacement
- ALWAYS check existing docs before creating new ones
- Keep documentation concise — avoid boilerplate
```

### 3. Use Your Agent

The new role is automatically discovered at startup:

```bash
python forge_orchestrator.py --task 42 --role documentation-writer -y
```

---

## The `_common.md` File

The `_common.md` file contains rules that apply to ALL agents. This includes:

- Branding requirements (TheForge, LLC attribution)
- Windows-specific instructions
- TheForge database usage patterns
- Output format requirements
- Safety constraints

When you create a custom agent, these rules are automatically prepended to your prompt. You don't need to repeat them.

---

## Required Sections

While not strictly enforced, every agent prompt should include:

| Section | Purpose |
|---------|---------|
| **Role** | What this agent is and what it does |
| **Responsibilities** | Specific tasks the agent should perform |
| **Output Format** | JSON structure for structured parsing |
| **Tool Guidance** | What tools the agent should and shouldn't use |
| **Constraints** | What the agent must NOT do |

> **Note on tool permissions:** All agents run with `--permission-mode bypassPermissions`, which gives them full tool access. The "Tool Guidance" section in your prompt is *advisory* — it tells the agent what it should and shouldn't do, but it's not enforced by the CLI. Well-written prompts are effective at keeping agents in their lane, but they're not a security boundary.

---

## Example: API Reviewer Agent

```markdown
# API Reviewer Agent

## Role
You review API endpoints for consistency, completeness, and best practices.

## Responsibilities
- Check all API endpoints follow REST conventions
- Verify request/response schemas are documented
- Flag missing error handling or validation
- Check authentication/authorization on all endpoints
- Report findings as tasks in TheForge

## Output Format
{
    "status": "done" | "blocked",
    "summary": "Review summary",
    "findings": [
        {
            "severity": "high" | "medium" | "low",
            "endpoint": "/api/resource",
            "issue": "Description of the issue",
            "recommendation": "What to fix"
        }
    ]
}

Always include `RESULT:` followed by your JSON output.

## Tool Permissions
- Read: YES
- Write: NO
- Edit: NO
- Bash: YES (read-only)
- TheForge MCP: YES

## Constraints
- NEVER modify any code
- NEVER skip endpoints — review all of them
- Report findings even if minor
```

---

## Custom Turn Limits and Models

You can configure per-role turn limits and models for custom agents in `dispatch_config.json`:

```json
{
    "max_turns_documentation_writer": 15,
    "model_documentation_writer": "haiku"
}
```

The key pattern is `max_turns_{role}` and `model_{role}`, where `{role}` is the filename stem with hyphens replaced by underscores.

If no per-role config exists, the agent uses the global `max_turns` and `model` defaults.

---

## Tips

- **Keep prompts focused.** One agent, one job. Don't try to make a do-everything agent.
- **Be explicit about output format.** The orchestrator parses JSON from agent output.
- **Set clear boundaries.** Specify what the agent can and cannot do.
- **Test with `--dry-run` first.** See the prompt size and structure before running.
- **Use `--role your-agent` to select it.** The filename stem is the role name.
- **Check `_common.md` for overlap.** Don't repeat rules that are already shared.
- **Tool guidance is advisory.** Agents run with full permissions — your prompt controls behavior, not the CLI.
