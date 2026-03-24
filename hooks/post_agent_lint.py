#!/usr/bin/env python3
"""Built-in hook: Post-agent linting check.

Runs a project linter after an agent finishes editing files.
Reports lint issues but does not block by default.

Usage as external hook:
    {"command": "python hooks/post_agent_lint.py", "timeout": 60, "block_on_fail": false}

Environment variables (set by EQUIPA hook runner):
    EQUIPA_HOOK_PROJECT_DIR — project directory to lint
    EQUIPA_HOOK_TASK_ID — the task that was completed
    EQUIPA_HOOK_ROLE — the agent role that just finished

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess
import sys


# Lint commands by language
LINT_COMMANDS: dict[str, list[list[str]]] = {
    "python": [
        ["python", "-m", "flake8", "--max-line-length=88", "--count", "--statistics"],
        ["python", "-m", "ruff", "check", "--no-fix"],
    ],
    "javascript": [
        ["npx", "eslint", ".", "--max-warnings=0"],
    ],
    "typescript": [
        ["npx", "eslint", ".", "--max-warnings=0"],
    ],
    "rust": [
        ["cargo", "clippy", "--", "-D", "warnings"],
    ],
    "go": [
        ["golangci-lint", "run"],
    ],
}

# File patterns for language detection
LANGUAGE_MARKERS: dict[str, list[str]] = {
    "python": ["setup.py", "pyproject.toml", "requirements.txt"],
    "javascript": ["package.json"],
    "typescript": ["tsconfig.json"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
}


def detect_language(project_dir: str) -> str | None:
    """Detect the primary language of a project."""
    for lang, markers in LANGUAGE_MARKERS.items():
        for marker in markers:
            if os.path.exists(os.path.join(project_dir, marker)):
                return lang
    return None


def run_lint(project_dir: str) -> int:
    """Run lint checks for the detected project language.

    Tries each linter in order and uses the first one that's available.

    Returns:
        0 if lint passes or no linter is available.
        Non-zero if lint finds issues.
    """
    lang = detect_language(project_dir)
    if not lang:
        print(f"[post_agent_lint] No language detected — skipping lint")
        return 0

    linter_cmds = LINT_COMMANDS.get(lang, [])
    if not linter_cmds:
        print(f"[post_agent_lint] No linter configured for {lang}")
        return 0

    for cmd in linter_cmds:
        try:
            print(f"[post_agent_lint] Trying: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                print(f"[post_agent_lint] Lint passed ({lang}, {cmd[0]})")
            else:
                issue_count = result.stdout.count("\n") if result.stdout else 0
                print(f"[post_agent_lint] Lint issues found ({issue_count} lines):")
                # Show first 10 lines of output
                output = result.stdout or result.stderr
                for line in output.splitlines()[:10]:
                    print(f"  {line}")
                if issue_count > 10:
                    print(f"  ... and {issue_count - 10} more")
            return result.returncode
        except FileNotFoundError:
            # This linter isn't installed, try the next one
            continue
        except subprocess.TimeoutExpired:
            print(f"[post_agent_lint] Lint timed out after 120s")
            return 0  # Don't block on timeout

    print(f"[post_agent_lint] No linter available for {lang} — install one of: "
          f"{', '.join(cmd[0] if isinstance(cmd, list) else cmd for cmd in linter_cmds)}")
    return 0


def main() -> int:
    """Entry point for external hook execution."""
    project_dir = os.environ.get("EQUIPA_HOOK_PROJECT_DIR", os.getcwd())
    task_id = os.environ.get("EQUIPA_HOOK_TASK_ID", "unknown")
    role = os.environ.get("EQUIPA_HOOK_ROLE", "unknown")
    print(f"[post_agent_lint] Linting after {role} agent on task {task_id}")
    return run_lint(project_dir)


if __name__ == "__main__":
    sys.exit(main())
