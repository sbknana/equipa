#!/usr/bin/env python3
"""Built-in hook: Preflight build check before agent dispatch.

Verifies the project builds cleanly before an agent starts work.
Replaces the hardcoded preflight logic with a hookable, configurable version.

Usage as external hook:
    {"command": "python hooks/preflight_build.py", "timeout": 60, "block_on_fail": true}

Environment variables (set by EQUIPA hook runner):
    EQUIPA_HOOK_PROJECT_DIR — project directory to check
    EQUIPA_HOOK_TASK_ID — the task being dispatched

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import subprocess
import sys

# Build commands by language detection
BUILD_COMMANDS: dict[str, list[str]] = {
    "python": ["python", "-m", "py_compile"],
    "javascript": ["npm", "run", "build"],
    "typescript": ["npx", "tsc", "--noEmit"],
    "rust": ["cargo", "check"],
    "go": ["go", "build", "./..."],
    "java": ["mvn", "compile", "-q"],
    "csharp": ["dotnet", "build", "--no-restore", "-q"],
}

# File patterns that indicate a language
LANGUAGE_MARKERS: dict[str, list[str]] = {
    "python": ["setup.py", "pyproject.toml", "requirements.txt"],
    "javascript": ["package.json"],
    "typescript": ["tsconfig.json"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
    "java": ["pom.xml", "build.gradle"],
    "csharp": ["*.csproj", "*.sln"],
}


def detect_language(project_dir: str) -> str | None:
    """Detect the primary language of a project by marker files."""
    for lang, markers in LANGUAGE_MARKERS.items():
        for marker in markers:
            if marker.startswith("*"):
                # Glob pattern — check if any file matches
                import glob
                if glob.glob(os.path.join(project_dir, marker)):
                    return lang
            elif os.path.exists(os.path.join(project_dir, marker)):
                return lang
    return None


def run_build_check(project_dir: str) -> int:
    """Run a build check for the detected project language.

    Returns:
        0 if build succeeds or no build command applies.
        Non-zero exit code if build fails.
    """
    lang = detect_language(project_dir)
    if not lang:
        print(f"[preflight_build] No language detected in {project_dir} — skipping")
        return 0

    cmd = BUILD_COMMANDS.get(lang)
    if not cmd:
        print(f"[preflight_build] No build command for {lang} — skipping")
        return 0

    # For Python, we do a syntax check on all .py files instead of compiling one
    if lang == "python":
        return _check_python_syntax(project_dir)

    print(f"[preflight_build] Running: {' '.join(cmd)} (language: {lang})")
    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"[preflight_build] FAILED (exit {result.returncode}):")
            print(result.stderr[:500] if result.stderr else result.stdout[:500])
        else:
            print(f"[preflight_build] Build OK ({lang})")
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[preflight_build] Build timed out after 120s")
        return 1
    except FileNotFoundError:
        print(f"[preflight_build] Build tool not found: {cmd[0]}")
        return 0  # Don't block if tool is missing


def _check_python_syntax(project_dir: str) -> int:
    """Check Python syntax by compiling all .py files."""
    errors = 0
    for root, _dirs, files in os.walk(project_dir):
        # Skip common non-source directories
        if any(skip in root for skip in [".git", "__pycache__", "node_modules", ".venv", "venv"]):
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    source = f.read()
                compile(source, filepath, "exec")
            except SyntaxError as e:
                print(f"[preflight_build] Syntax error in {filepath}: {e}")
                errors += 1
    if errors:
        print(f"[preflight_build] {errors} syntax error(s) found")
        return 1
    print(f"[preflight_build] Python syntax OK")
    return 0


def main() -> int:
    """Entry point for external hook execution."""
    project_dir = os.environ.get("EQUIPA_HOOK_PROJECT_DIR", os.getcwd())
    task_id = os.environ.get("EQUIPA_HOOK_TASK_ID", "unknown")
    print(f"[preflight_build] Checking build for task {task_id} in {project_dir}")
    return run_build_check(project_dir)


if __name__ == "__main__":
    sys.exit(main())
