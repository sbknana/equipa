#!/usr/bin/env python3
"""Ollama-based tool-calling agent for EQUIPA.

A minimal tool-calling agent loop using Ollama's API with zero external
dependencies (uses urllib.request). Provides the same result dict format as
run_agent_streaming() for orchestrator compatibility.

Supports OpenAI-compatible tool calling (Ollama supports this natively).
File operations are sandboxed to the project directory.

Copyright 2026 Forgeborn
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Config ---

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:27b"
DEFAULT_MAX_TURNS = 25
REQUEST_TIMEOUT = 300  # 5 minutes per LLM call
MAX_OUTPUT_SIZE = 50_000  # truncate tool outputs at 50KB

# --- Tool Definitions (OpenAI-compatible format) ---

READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file content as a string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read (relative to project root)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at a given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (relative to project root). Use '.' for project root.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match (e.g., '**/*.py', 'src/**/*.ts')",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in (relative to project root). Defaults to '.'",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob filter (e.g., '*.py'). Optional.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a read-only shell command. Only safe commands are allowed (ls, cat, find, grep, git status, etc.). Write commands will be rejected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

WRITE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to write (relative to project root)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file with a new string. The old_string must be an exact match of existing file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit (relative to project root)",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact string to find and replace",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "String to replace it with",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_write",
            "description": "Execute a shell command that may modify the filesystem (mkdir, npm install, pip install, git commit, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

# Read-only roles get only read tools. Write roles get both.
READ_ONLY_ROLES = {"planner", "evaluator", "code-reviewer", "security-reviewer", "researcher"}
WRITE_ROLES = {"developer", "tester", "debugger", "frontend-designer", "integration-tester"}

# Dangerous commands blocked even for write roles
BLOCKED_COMMANDS = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", ":(){", "chmod -R 777 /",
    "curl | sh", "wget | sh", "curl | bash", "wget | bash",
]

# Read-only commands allowed for the read-only bash tool
SAFE_COMMAND_PREFIXES = [
    "ls", "cat", "head", "tail", "find", "grep", "rg", "wc", "file",
    "git status", "git log", "git diff", "git show", "git branch",
    "echo", "pwd", "which", "type", "env", "printenv",
    # python -c/node -e removed: arbitrary code execution risk
    "npm list", "pip list", "pip show", "go version",
    "test ", "[", "stat", "du ", "df ",
]


# --- Path Safety ---

def safe_path(project_dir, relative_path):
    """Resolve a relative path and ensure it's within the project directory.

    Returns the resolved absolute Path, or raises ValueError if the path
    escapes the sandbox.
    """
    project = Path(project_dir).resolve()
    target = (project / relative_path).resolve()
    if not target.is_relative_to(project):
        raise ValueError(f"Path escapes project directory: {relative_path}")
    return target


def is_safe_read_command(command):
    """Check if a shell command is safe (read-only)."""
    stripped = command.strip()
    if stripped.startswith("sudo "):
        stripped = stripped[5:].lstrip()
    return any(stripped.startswith(prefix) for prefix in SAFE_COMMAND_PREFIXES)


def is_blocked_command(command):
    """Check if a command matches any blocked pattern."""
    lower = command.lower()
    return any(blocked in lower for blocked in BLOCKED_COMMANDS)


# --- Tool Implementations ---

def exec_read_file(project_dir, args):
    """Read a file from the project directory."""
    path = safe_path(project_dir, args["path"])
    if not path.exists():
        return f"Error: File not found: {args['path']}"
    if not path.is_file():
        return f"Error: Not a file: {args['path']}"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_OUTPUT_SIZE:
            content = content[:MAX_OUTPUT_SIZE] + f"\n\n[...truncated at {MAX_OUTPUT_SIZE} chars]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def exec_list_directory(project_dir, args):
    """List files in a directory."""
    path = safe_path(project_dir, args.get("path", "."))
    if not path.exists():
        return f"Error: Directory not found: {args.get('path', '.')}"
    if not path.is_dir():
        return f"Error: Not a directory: {args.get('path', '.')}"
    try:
        entries = sorted(path.iterdir())
        lines = []
        for entry in entries[:500]:  # limit to 500 entries
            prefix = "d " if entry.is_dir() else "f "
            rel = entry.relative_to(Path(project_dir).resolve())
            lines.append(f"{prefix}{rel}")
        if len(entries) > 500:
            lines.append(f"[...and {len(entries) - 500} more entries]")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"Error listing directory: {e}"


def exec_search_files(project_dir, args):
    """Search for files matching a glob pattern."""
    project = Path(project_dir).resolve()
    pattern = args["pattern"]
    try:
        matches = sorted(project.glob(pattern))
        # Filter to only files within project
        safe_matches = [
            str(m.relative_to(project))
            for m in matches
            if m.is_relative_to(project) and m.is_file()
        ][:200]  # limit results
        if not safe_matches:
            return f"No files matching pattern: {pattern}"
        result = "\n".join(safe_matches)
        if len(matches) > 200:
            result += f"\n[...and {len(matches) - 200} more matches]"
        return result
    except Exception as e:
        return f"Error searching files: {e}"


def exec_grep(project_dir, args):
    """Search file contents for a regex pattern."""
    project = Path(project_dir).resolve()
    search_path = args.get("path", ".")
    target = safe_path(project_dir, search_path)
    pattern = args["pattern"]
    include = args.get("include")

    try:
        cmd = ["grep", "-rn", "--include", include, pattern, str(target)] if include else [
            "grep", "-rn", pattern, str(target)
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(project)
        )
        output = result.stdout
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + "\n[...truncated]"
        return output if output else f"No matches for pattern: {pattern}"
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30 seconds"
    except Exception as e:
        return f"Error running grep: {e}"


def exec_bash(project_dir, args, allow_write=False):
    """Execute a shell command, optionally restricted to read-only."""
    command = args["command"]

    if is_blocked_command(command):
        return "Error: Command blocked for safety reasons"

    if not allow_write and not is_safe_read_command(command):
        return (f"Error: Command not allowed in read-only mode. "
                f"Only safe read commands are permitted: {', '.join(SAFE_COMMAND_PREFIXES[:10])}...")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=project_dir,
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR: {result.stderr}"
        if result.returncode != 0:
            output += f"\n(exit code: {result.returncode})"
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + "\n[...truncated]"
        return output if output.strip() else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 120 seconds"
    except Exception as e:
        return f"Error executing command: {e}"


def exec_write_file(project_dir, args):
    """Write content to a file in the project directory."""
    path = safe_path(project_dir, args["path"])
    content = args["content"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {args['path']}"
    except Exception as e:
        return f"Error writing file: {e}"


def exec_edit_file(project_dir, args):
    """Replace a string in a file."""
    path = safe_path(project_dir, args["path"])
    if not path.exists():
        return f"Error: File not found: {args['path']}"
    try:
        content = path.read_text(encoding="utf-8")
        old = args["old_string"]
        new = args["new_string"]
        count = content.count(old)
        if count == 0:
            return f"Error: old_string not found in {args['path']}"
        if count > 1:
            return f"Error: old_string found {count} times in {args['path']}. Provide more context to make it unique."
        content = content.replace(old, new, 1)
        path.write_text(content, encoding="utf-8")
        return f"Successfully edited {args['path']}"
    except Exception as e:
        return f"Error editing file: {e}"


# Tool dispatch table
TOOL_HANDLERS = {
    "read_file": lambda pd, a: exec_read_file(pd, a),
    "list_directory": lambda pd, a: exec_list_directory(pd, a),
    "search_files": lambda pd, a: exec_search_files(pd, a),
    "grep": lambda pd, a: exec_grep(pd, a),
    "bash": lambda pd, a: exec_bash(pd, a, allow_write=False),
    "write_file": lambda pd, a: exec_write_file(pd, a),
    "edit_file": lambda pd, a: exec_edit_file(pd, a),
    "bash_write": lambda pd, a: exec_bash(pd, a, allow_write=True),
}


# --- Ollama API ---

def ollama_chat(base_url, model, messages, tools=None, timeout=REQUEST_TIMEOUT):
    """Call Ollama's chat completions API (OpenAI-compatible).

    Uses the /api/chat endpoint with tool definitions in OpenAI format.
    Returns the parsed response dict.
    """
    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 4096,
        },
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to Ollama at {base_url}: {e}") from e


# --- Agent Loop ---

def run_ollama_agent(system_prompt, project_dir, role="developer",
                     model=None, base_url=None, max_turns=None):
    """Run a tool-calling agent loop using Ollama.

    This is the main entry point for the orchestrator. Returns a result dict
    compatible with run_agent_streaming():
        {
            "success": bool,
            "result_text": str,
            "errors": list,
            "num_turns": int,
            "duration": float,
            "cost": float,  # always 0 for local LLM
        }
    """
    base_url = base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    max_turns = max_turns or DEFAULT_MAX_TURNS
    project_dir = str(Path(project_dir).resolve())

    # Select tools based on role
    if role in READ_ONLY_ROLES:
        tools = READ_TOOLS
    else:
        tools = READ_TOOLS + WRITE_TOOLS

    messages = [
        {"role": "system", "content": system_prompt},
    ]

    start_time = time.time()
    turn_count = 0
    all_text = []
    errors = []

    print(f"  [Ollama] Starting {role} agent with {model} (max {max_turns} turns)")

    while turn_count < max_turns:
        turn_count += 1

        try:
            response = ollama_chat(base_url, model, messages, tools=tools)
        except RuntimeError as e:
            errors.append(str(e))
            print(f"  [Ollama] API error at turn {turn_count}: {e}")
            break

        message = response.get("message", {})
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", [])

        # Accumulate assistant text
        if content:
            all_text.append(content)

        # Add assistant message to history
        messages.append(message)

        # If no tool calls, the agent is done
        if not tool_calls:
            if content:
                print(f"  [Ollama] Agent finished at turn {turn_count} (no more tool calls)")
            break

        # Process tool calls
        for tool_call in tool_calls:
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            tool_args = func.get("arguments", {})

            # Handle string arguments (some models return JSON string)
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError:
                    tool_args = {}

            print(f"  [Ollama] Turn {turn_count}: {tool_name}")

            # Execute tool
            handler = TOOL_HANDLERS.get(tool_name)
            if handler:
                try:
                    result = handler(project_dir, tool_args)
                except ValueError as e:
                    result = f"Error: {e}"
                except Exception as e:
                    result = f"Tool error: {e}"
                    errors.append(f"Turn {turn_count} {tool_name}: {e}")
            else:
                result = f"Error: Unknown tool '{tool_name}'"

            # Add tool result to messages
            messages.append({
                "role": "tool",
                "content": str(result),
            })

    duration = time.time() - start_time
    result_text = "\n".join(all_text)

    # Check for RESULT: block in output (EQUIPA convention)
    # Success = agent produced a structured result AND didn't exhaust turns
    has_result_block = "RESULT:" in result_text
    within_budget = turn_count < max_turns
    success = has_result_block and within_budget

    print(f"  [Ollama] Completed: {turn_count} turns, {duration:.1f}s, "
          f"{'success' if success else 'max turns hit'}")

    return {
        "success": success,
        "result_text": result_text,
        "errors": errors,
        "num_turns": turn_count,
        "duration": duration,
        "cost": 0.0,  # Local LLM — no API cost
        "provider": "ollama",
        "model": model,
    }


# --- Health Check ---

def check_ollama_health(base_url=None):
    """Check if Ollama is running and accessible.

    Returns (is_healthy, message) tuple.
    """
    base_url = base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    url = f"{base_url}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            return True, f"Ollama running. Models: {', '.join(models[:10])}"
    except Exception as e:
        return False, f"Cannot connect to Ollama at {base_url}: {e}"


def list_ollama_models(base_url=None):
    """List available Ollama models.

    Returns list of model name strings.
    """
    base_url = base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    url = f"{base_url}/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


if __name__ == "__main__":
    # Quick test: check Ollama health and run a simple agent
    import sys

    healthy, msg = check_ollama_health()
    print(f"Ollama health: {msg}")

    if not healthy:
        print("Ollama is not running. Start it with: ollama serve")
        sys.exit(1)

    if len(sys.argv) > 1:
        project = sys.argv[1]
    else:
        project = "."

    result = run_ollama_agent(
        system_prompt="You are a code reviewer. List the Python files in the project and summarize what you see.",
        project_dir=project,
        role="code-reviewer",
        max_turns=5,
    )
    print(f"\nResult: {'SUCCESS' if result['success'] else 'FAILED'}")
    print(f"Output:\n{result['result_text'][:500]}")
