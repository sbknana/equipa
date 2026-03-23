"""EQUIPA git operations: repo setup, language detection, and git helpers.

Extracted from forge_orchestrator.py as part of Phase 1 monolith split.
All functions are re-exported via equipa/__init__.py for backward compatibility.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from equipa.constants import (
    GITIGNORE_TEMPLATES,
    GITHUB_OWNER,
    PROJECT_DIRS,
)


def _is_git_repo(path: str | Path) -> bool:
    """Check if a directory is a git repository."""
    return (Path(path) / ".git").exists()


def detect_project_language(project_dir: str | Path) -> dict:
    """Detect languages and frameworks in a project by scanning for marker files.

    Returns a dict with:
        - languages: list of detected language strings
        - frameworks: list of detected framework strings
        - primary: the most likely primary language (string)

    The primary language is chosen by a priority order that favours explicit
    project manifests over file-extension scanning.
    """
    p = Path(project_dir)
    languages: list[str] = []
    frameworks: list[str] = []

    # --- Language detection via marker files ---

    # Python: pyproject.toml, setup.py, requirements.txt, Pipfile
    python_markers = ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"]
    if any((p / m).exists() for m in python_markers) or list(p.glob("*.py")):
        languages.append("python")
        if (p / "pyproject.toml").exists():
            try:
                content = (p / "pyproject.toml").read_text(
                    encoding="utf-8", errors="replace",
                )
                if "django" in content.lower():
                    frameworks.append("django")
                if "fastapi" in content.lower():
                    frameworks.append("fastapi")
                if "flask" in content.lower():
                    frameworks.append("flask")
            except OSError:
                pass

    # TypeScript: tsconfig.json
    has_tsconfig = (p / "tsconfig.json").exists()
    if has_tsconfig:
        languages.append("typescript")

    # JavaScript: package.json without tsconfig (pure JS)
    has_package_json = (p / "package.json").exists()
    if has_package_json and not has_tsconfig:
        if (p / "jsconfig.json").exists() or not has_tsconfig:
            languages.append("javascript")

    # Detect Node/JS frameworks from package.json
    if has_package_json:
        try:
            content = (p / "package.json").read_text(
                encoding="utf-8", errors="replace",
            )
            if '"next"' in content:
                frameworks.append("nextjs")
            if '"react"' in content:
                frameworks.append("react")
            if '"express"' in content:
                frameworks.append("express")
            if '"vue"' in content:
                frameworks.append("vue")
            if '"angular"' in content or '"@angular/core"' in content:
                frameworks.append("angular")
        except OSError:
            pass

    # Go: go.mod
    if (p / "go.mod").exists():
        languages.append("go")

    # Rust: Cargo.toml
    if (p / "Cargo.toml").exists():
        languages.append("rust")

    # C#/.NET: *.csproj, *.sln
    if (
        list(p.glob("*.csproj"))
        or list(p.glob("*.sln"))
        or list(p.glob("**/*.csproj"))
    ):
        languages.append("csharp")
        frameworks.append("dotnet")

    # Java: pom.xml, build.gradle
    if (
        (p / "pom.xml").exists()
        or (p / "build.gradle").exists()
        or (p / "build.gradle.kts").exists()
    ):
        languages.append("java")
        if (p / "pom.xml").exists():
            frameworks.append("maven")
        if (p / "build.gradle").exists() or (p / "build.gradle.kts").exists():
            frameworks.append("gradle")

    # Determine primary language (first detected wins based on priority above)
    primary = languages[0] if languages else "default"

    return {
        "languages": languages,
        "frameworks": frameworks,
        "primary": primary,
    }


def _get_repo_env() -> dict[str, str]:
    """Build an environment dict with git and gh on the PATH."""
    env = os.environ.copy()
    extra_paths = []
    for candidate in [
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\GitHub CLI",
    ]:
        if os.path.isdir(candidate) and candidate not in env.get("PATH", ""):
            extra_paths.append(candidate)
    if extra_paths:
        env["PATH"] = ";".join(extra_paths) + ";" + env.get("PATH", "")
    return env


def _git_run(
    args_list: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a git/gh command with standard options. Returns subprocess result."""
    return subprocess.run(
        args_list, capture_output=True, text=True,
        cwd=str(cwd), timeout=timeout, env=_get_repo_env(),
    )


def check_gh_installed() -> bool:
    """Verify that gh CLI is installed and authenticated.

    Returns True if ready, prints error and returns False otherwise.
    """
    if not shutil.which("gh"):
        print("ERROR: GitHub CLI (gh) is not installed.")
        print("Install it from: https://cli.github.com/")
        return False

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print("ERROR: GitHub CLI is not authenticated.")
            print("Run: gh auth login")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("ERROR: Could not check gh auth status.")
        return False

    return True


def setup_single_repo(
    codename: str,
    project_dir: str | Path,
    owner: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Initialize git and create a GitHub private repo for a single project.

    Returns (success: bool, message: str).
    """
    p = Path(project_dir)
    repo_name = codename.lower().replace(" ", "-")

    # Skip if already fully set up (has .git AND a remote)
    has_git = (p / ".git").exists()
    if has_git:
        try:
            r = _git_run(["git", "remote", "get-url", "origin"], p, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True, f"Already set up (remote: {r.stdout.strip()})"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if dry_run:
        lang_info = detect_project_language(project_dir)
        return True, (
            f"DRY RUN: Would init git, detect={lang_info['primary']}, "
            f"create {owner}/{repo_name}"
        )

    # .gitignore
    lang_info = detect_project_language(project_dir)
    lang = lang_info["primary"]
    # Map new language keys to existing gitignore template keys
    gitignore_key_map = {
        "typescript": "node",
        "javascript": "node",
        "csharp": "dotnet",
    }
    gitignore_key = gitignore_key_map.get(lang, lang)
    gitignore_path = p / ".gitignore"
    if not gitignore_path.exists():
        template = GITIGNORE_TEMPLATES.get(
            gitignore_key, GITIGNORE_TEMPLATES["default"],
        )
        gitignore_path.write_text(template + "\n", encoding="utf-8")
        print(f"    Created .gitignore ({lang})")

    # git init
    if not has_git:
        r = _git_run(["git", "init"], p)
        if r.returncode != 0:
            return False, f"git init failed: {r.stderr.strip()}"
    else:
        print("    .git already exists, resuming setup")

    # git add (filter CRLF warnings)
    r = _git_run(["git", "add", "."], p, timeout=300)
    if r.returncode != 0:
        real_errors = [
            line for line in r.stderr.strip().splitlines()
            if not line.startswith("warning:")
        ]
        if real_errors:
            return False, f"git add failed: {chr(10).join(real_errors)}"

    # git commit
    r = _git_run(["git", "commit", "-m", "Initial commit"], p, timeout=120)
    if r.returncode != 0:
        if "nothing to commit" not in (r.stdout + r.stderr):
            return False, f"git commit failed: {r.stderr.strip()}"
        print("    Nothing to commit (empty or already committed)")

    # gh repo create
    r = _git_run(
        ["gh", "repo", "create", f"{owner}/{repo_name}",
         "--private", "--source=.", "--push"],
        p, timeout=300,
    )
    if r.returncode != 0:
        if "already exists" in r.stderr:
            print("    Repo already exists on GitHub, adding remote...")
            _git_run(
                ["git", "remote", "add", "origin",
                 f"https://github.com/{owner}/{repo_name}.git"],
                p, timeout=10,
            )
            pr = _git_run(
                ["git", "push", "-u", "origin", "main"], p, timeout=120,
            )
            if pr.returncode != 0:
                _git_run(
                    ["git", "push", "-u", "origin", "master"], p, timeout=120,
                )
        else:
            return False, f"gh repo create failed: {r.stderr.strip()}"

    return True, f"Created https://github.com/{owner}/{repo_name}"


def setup_all_repos(args) -> None:
    """Initialize git + GitHub repos for all (or one) project.

    Uses --setup-repos for all, --setup-repos-project for a single project.

    NOTE: This function imports fetch_project_info from the orchestrator at
    call time to avoid circular imports during the Phase 1 split.
    """
    # Lazy import to avoid circular dependency during package split
    from equipa.tasks import fetch_project_info

    # Check prerequisites (skip for dry run)
    if not args.dry_run and not check_gh_installed():
        sys.exit(1)

    # synced storage warning
    print("\n" + "!" * 60)
    print("WARNING: Git repos in synced storage can experience corruption")
    print("from sync conflicts on the .git/index binary file.")
    print("")
    print("Recommendation: Avoid editing the same project on multiple")
    print("PCs simultaneously. The GitHub remote serves as your backup —")
    print("you can always re-clone if needed.")
    print("!" * 60)

    if not args.yes and not args.dry_run:
        response = input("\nContinue? (y/n): ").strip().lower()
        if response != "y":
            print("Aborted.")
            return

    # Determine which projects to set up
    if args.setup_repos_project:
        # Single project by ID
        project_info = fetch_project_info(args.setup_repos_project)
        if not project_info:
            print(
                f"ERROR: Project {args.setup_repos_project} not found in TheForge",
            )
            sys.exit(1)

        codename = project_info.get("codename", "").lower().strip()
        pname = project_info.get("name", "").lower().strip()
        project_dir = PROJECT_DIRS.get(codename) or PROJECT_DIRS.get(pname)

        if not project_dir:
            print(
                f"ERROR: No directory mapped for project "
                f"'{project_info.get('name')}'",
            )
            sys.exit(1)

        targets = [(codename or pname, project_dir)]
    else:
        # All projects
        targets = list(PROJECT_DIRS.items())

    print(f"\nSetting up {len(targets)} project(s)...\n")

    results = []
    for codename, project_dir in targets:
        if not Path(project_dir).exists():
            print(
                f"  [{codename}] SKIP — directory does not exist: {project_dir}",
            )
            results.append((codename, False, "Directory does not exist"))
            continue

        print(f"  [{codename}] {project_dir}")
        success, msg = setup_single_repo(
            codename, project_dir, GITHUB_OWNER, args.dry_run,
        )
        status = "OK" if success else "FAIL"
        print(f"    -> {status}: {msg}")
        results.append((codename, success, msg))

    # Summary
    ok = sum(1 for _, s, _ in results if s)
    fail = len(results) - ok
    print(
        f"\nDone: {ok} succeeded, {fail} failed out of {len(results)} projects.",
    )
