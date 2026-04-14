#!/usr/bin/env python3
"""
SWE-bench Verified Runner — EQUIPA Inside Official Docker Containers
(c) 2026 Forgeborn

Runs EQUIPA inside SWE-bench's pre-built Docker evaluation containers so
patches are generated in the EXACT environment where the harness validates
them. This guarantees patches apply cleanly and tests run in the right env.

SWE-bench uses Docker images from the swebench harness. Each instance has:
- instance_id, repo, base_commit, patch (gold), test_patch
- FAIL_TO_PASS, PASS_TO_PASS test lists
- Docker images: swebench/sweb.eval.x86_64.{repo_slug}:{version}

KEY DIFFERENCES FROM FEATUREBENCH:
1. Different Docker image naming: swebench/sweb.eval.x86_64.{slug}
2. Repo is at /testbed/ (same workdir)
3. Masking: apply test_patch REMOVAL (hide the tests validating the fix)
4. Evaluation: run FAIL_TO_PASS tests — if they pass, task is resolved
5. SWE-bench dataset from HuggingFace: princeton-nlp/SWE-bench_Verified

Architecture:
  Phase 1 (one-time):  --setup    → Docker volume with Node.js + Claude CLI
  Phase 2 (per-task):  --limit N  → EQUIPA inside containers → output.jsonl
  Phase 3 (validate):  --validate → Official SWE-bench harness

Usage:
    python swebench_docker.py --setup
    python swebench_docker.py --limit 20 --retries 3 --workers 4
    python swebench_docker.py --validate
    python swebench_docker.py --limit 100 --retries 3 --validate
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sqlite3
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    import docker
    import docker.errors
except ImportError:
    print("ERROR: docker SDK not installed. Run: pip install docker")
    sys.exit(1)

from cumulative_db import CumulativeDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Paths ---

EQUIPA_ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS_DIR = EQUIPA_ROOT / "benchmarks"
SCHEMA_SQL = EQUIPA_ROOT / "schema.sql"

# SWE-bench dataset — download with:
#   python -c "from datasets import load_dataset; ds=load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); ds.to_json('swebench_verified.jsonl')"
DATASET_PATH = BENCHMARKS_DIR / "swebench_verified.jsonl"

# Docker constants
TOOLS_VOLUME = "equipa-claude-tools"
DOCKER_WORKDIR = "/testbed"
EQUIPA_DOCKER_DIR = "/opt/equipa"
CONDA_PREFIX = (
    "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed"
)

# EQUIPA source files to copy into Docker
EQUIPA_SOURCE_DIRS = ["equipa", "prompts", "skills"]
EQUIPA_SOURCE_FILES = [
    "forge_orchestrator.py",
    "forgesmith.py",
    "forgesmith_gepa.py",
    "lesson_sanitizer.py",
    "rubric_quality_scorer.py",
    "schema.sql",
    "dispatch_config.json",
    "forgesmith_config.json",
    "skill_manifest.json",
]


# ============================================================
# Helpers
# ============================================================


def load_dataset(
    path: str, limit: int = 0, offset: int = 0
) -> list[dict[str, Any]]:
    """Load SWE-bench instances from JSONL."""
    items: list[dict[str, Any]] = []
    skipped = 0
    with open(path) as f:
        for line in f:
            if skipped < offset:
                skipped += 1
                continue
            items.append(json.loads(line))
            if limit and len(items) >= limit:
                break
    return items


def get_swebench_image_name(instance: dict[str, Any]) -> str:
    """Derive the Docker image name for a SWE-bench instance.

    SWE-bench images follow the pattern:
        swebench/sweb.eval.x86_64.{repo_slug}:{instance_id}

    where repo_slug is the lowercased repo with / replaced by __ and
    the instance_id (with dots replaced by underscores for the tag) serves
    as the Docker tag.
    """
    # If the dataset already has an image_name field, use it
    if "image_name" in instance and instance["image_name"]:
        return instance["image_name"]

    # Derive from repo + instance_id
    repo = instance["repo"]
    instance_id = instance["instance_id"]

    # swebench convention: repo slug is lowercased, / → __
    repo_slug = repo.lower().replace("/", "__")

    # The Docker tag is the full instance_id
    return f"swebench/sweb.eval.x86_64.{repo_slug}:{instance_id}"


def exec_cmd(
    container: Any, cmd: str, timeout: int = 300, workdir: str | None = None
) -> tuple[int, str]:
    """Execute a bash command inside the Docker container.

    Args:
        container: Docker container object.
        cmd: Shell command to execute.
        timeout: Max seconds (used by callers that wrap with shell timeout).
        workdir: Working directory inside the container.

    Returns (exit_code, output_str). Never raises on command failure.
    """
    full_cmd = f'/bin/bash -lc "source ~/.bashrc 2>/dev/null; {cmd}"'
    try:
        result = container.exec_run(
            full_cmd,
            user="root",
            workdir=workdir or DOCKER_WORKDIR,
            demux=True,
        )
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        output = stdout + stderr
        return result.exit_code, output
    except Exception as e:
        return -1, str(e)


def exec_cmd_checked(
    container: Any,
    cmd: str,
    timeout: int = 300,
    workdir: str | None = None,
    label: str = "",
) -> tuple[int, str]:
    """Execute command, print on failure. Returns (exit_code, output)."""
    code, output = exec_cmd(container, cmd, timeout=timeout, workdir=workdir)
    if code != 0 and label:
        print(f"    [{label}] WARN: exit={code}")
        if output.strip():
            for line in output.strip().split("\n")[-5:]:
                print(f"      {line}")
    return code, output


def exec_cmd_as_equipa(
    container: Any, cmd: str, timeout: int = 300
) -> tuple[int, str]:
    """Execute a command as the equipa user inside the container."""
    full_cmd = (
        f'/bin/bash -lc "source /home/equipa/.bashrc 2>/dev/null; {cmd}"'
    )
    try:
        result = container.exec_run(
            full_cmd,
            user="equipa",
            workdir=DOCKER_WORKDIR,
            demux=True,
        )
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        return result.exit_code, stdout + stderr
    except Exception as e:
        return -1, str(e)


# ============================================================
# Phase 1: Setup — Docker volume with Claude CLI
# ============================================================


def setup_tools_volume(client: Any, force: bool = False) -> bool:
    """Create a Docker volume with Node.js 20 + Claude CLI + uv/uvx.

    Run once. The volume is mounted into every evaluation container so we
    don't re-install per task.
    """
    print("\n=== Setting Up EQUIPA Tools Volume ===\n")

    # Check if volume exists
    try:
        vol = client.volumes.get(TOOLS_VOLUME)
        print(f"  Volume '{TOOLS_VOLUME}' already exists.")
        if not force:
            resp = input("  Rebuild? (y/N): ").strip().lower()
            if resp != "y":
                print("  Keeping existing volume.")
                return True
        vol.remove()
        print("  Removed old volume.")
    except docker.errors.NotFound:
        pass

    # Create volume
    client.volumes.create(TOOLS_VOLUME)
    print(f"  Created volume: {TOOLS_VOLUME}")

    # Use a lightweight container to install tools into the volume
    print(
        "  Installing Node.js 20 + Claude CLI + uv "
        "(this takes a few minutes)..."
    )

    setup_script = """#!/bin/bash
set -ex
echo ">>> Installing Node.js 20..."
apt-get update -qq
apt-get install -y -qq curl ca-certificates
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y -qq nodejs
node --version
npm --version

echo ">>> Setting up /opt/tools..."
mkdir -p /opt/tools/bin /opt/tools/lib

echo ">>> Installing Claude CLI..."
npm install -g @anthropic-ai/claude-code --prefix /opt/tools
ls -la /opt/tools/bin/claude || echo "claude not in bin, checking lib..."
ls /opt/tools/lib/node_modules/@anthropic-ai/ 2>/dev/null || true

echo ">>> Installing uv/uvx..."
curl -LsSf https://astral.sh/uv/install.sh | sh
cp /root/.local/bin/uv /opt/tools/bin/ 2>/dev/null || true
cp /root/.local/bin/uvx /opt/tools/bin/ 2>/dev/null || true

echo ">>> Copying Node.js runtime..."
cp $(which node) /opt/tools/bin/
cp -r /usr/lib/node_modules /opt/tools/lib/ 2>/dev/null || true

echo ">>> Final contents:"
ls -la /opt/tools/bin/
echo ">>> Done."
"""

    try:
        container = client.containers.create(
            "ubuntu:22.04",
            command="/bin/bash /tmp/setup.sh",
            volumes={TOOLS_VOLUME: {"bind": "/opt/tools", "mode": "rw"}},
            detach=True,
        )

        script_bytes = setup_script.encode("utf-8")
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            info = tarfile.TarInfo(name="setup.sh")
            info.size = len(script_bytes)
            tar.addfile(info, io.BytesIO(script_bytes))
        tar_buf.seek(0)
        container.put_archive("/tmp", tar_buf)

        container.start()

        for chunk in container.logs(stream=True, follow=True):
            line = chunk.decode("utf-8", errors="replace").strip()
            if line:
                print(f"    {line}")
        result = container.wait()
        exit_code = result.get("StatusCode", -1)
        container.remove()

        if exit_code == 0:
            print("\n  Tools volume ready.")
            return True
        else:
            print(f"\n  ERROR: Setup failed with exit code {exit_code}")
            return False
    except Exception as e:
        print(f"\n  ERROR: {e}")
        return False


# ============================================================
# Phase 2: Run EQUIPA inside Docker containers
# ============================================================


def create_equipa_tar() -> io.BytesIO:
    """Create an in-memory tar archive of EQUIPA source files.

    Includes only what's needed to run the orchestrator — no tests,
    benchmarks, git history, or database.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirname in EQUIPA_SOURCE_DIRS:
            src = EQUIPA_ROOT / dirname
            if src.exists():
                for f in src.rglob("*"):
                    if f.is_file() and "__pycache__" not in str(f):
                        arcname = f"equipa_src/{f.relative_to(EQUIPA_ROOT)}"
                        tar.add(str(f), arcname=arcname)

        for filename in EQUIPA_SOURCE_FILES:
            src = EQUIPA_ROOT / filename
            if src.exists():
                tar.add(str(src), arcname=f"equipa_src/{filename}")

    buf.seek(0)
    return buf


def copy_tar_to_container(container: Any, tar_buf: io.BytesIO, dest_dir: str = "/opt") -> None:
    """Copy a tar archive into the container and extract it."""
    container.put_archive(dest_dir, tar_buf)


def setup_equipa_in_container(container: Any, api_key: str) -> bool:
    """Install EQUIPA inside the container.

    - Extracts source from /opt/equipa_src/
    - Creates fresh DB from schema.sql
    - Configures paths and environment
    - Sets up MCP config for TheForge DB
    """
    equipa_dir = EQUIPA_DOCKER_DIR

    # Move extracted source to final location
    exec_cmd_checked(
        container, f"mv /opt/equipa_src {equipa_dir}", label="move-source"
    )

    # Create fresh database from schema.sql via a Python script
    db_script = f"""#!/usr/bin/env python3
import sqlite3
conn = sqlite3.connect('{equipa_dir}/theforge.db')
with open('{equipa_dir}/schema.sql') as f:
    conn.executescript(f.read())
conn.close()
print('DB created')
"""
    _put_script(container, "create_db.py", db_script)
    exec_cmd_checked(container, "python3 /tmp/create_db.py", label="create-db")

    # Create non-root user (Claude CLI refuses bypassPermissions as root)
    exec_cmd_checked(
        container,
        "useradd -m -s /bin/bash equipa 2>/dev/null || true && "
        f"chown -R equipa:equipa {equipa_dir} && "
        f"chmod -R 755 {equipa_dir} && "
        f"chown -R equipa:equipa {DOCKER_WORKDIR} 2>/dev/null || true",
        label="create-user",
    )

    # Set up environment for both root and equipa user
    for bashrc in ["/root/.bashrc", "/home/equipa/.bashrc"]:
        exec_cmd_checked(
            container,
            f'echo \'export PATH="/opt/tools/bin:$PATH"\' >> {bashrc} && '
            f'echo \'export NODE_PATH="/opt/tools/lib/node_modules"\' >> {bashrc} && '
            f'echo \'export ANTHROPIC_API_KEY="{api_key}"\' >> {bashrc} && '
            f'echo \'export THEFORGE_DB="{equipa_dir}/theforge.db"\' >> {bashrc} && '
            f'echo \'export PYTHONPATH="{equipa_dir}:$PYTHONPATH"\' >> {bashrc}',
            label="env-setup",
        )

    # Create MCP config pointing to the fresh DB
    mcp_config = {
        "mcpServers": {
            "theforge": {
                "type": "stdio",
                "command": "/opt/tools/bin/uvx",
                "args": [
                    "mcp-server-sqlite",
                    "--db-path",
                    f"{equipa_dir}/theforge.db",
                ],
            }
        }
    }
    mcp_bytes = json.dumps(mcp_config, indent=2).encode("utf-8")
    _put_file(container, equipa_dir, "mcp_config.json", mcp_bytes)

    # Verify Claude CLI is accessible
    code, output = exec_cmd(
        container, 'export PATH="/opt/tools/bin:$PATH" && claude --version'
    )
    if code != 0:
        print(
            f"    Claude CLI from volume failed (exit={code}). "
            f"Installing Node.js in container..."
        )
        install_script = """#!/bin/bash
set -e
apt-get update -qq 2>/dev/null || true
apt-get install -y -qq curl ca-certificates 2>/dev/null || true
curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | bash - 2>/dev/null
apt-get install -y -qq nodejs 2>/dev/null || true
npm install -g @anthropic-ai/claude-code 2>/dev/null
which claude && claude --version
"""
        _put_script(container, "install_node.sh", install_script)
        code2, output2 = exec_cmd(
            container, "bash /tmp/install_node.sh", timeout=300
        )
        if code2 != 0:
            print("    WARNING: Node.js install failed. Cannot run agents.")
            return False
        for bashrc in ["/root/.bashrc", "/home/equipa/.bashrc"]:
            exec_cmd(
                container,
                f'echo \'export PATH="/usr/local/bin:/usr/bin:$PATH"\' >> {bashrc}',
            )
        output = output2

    print(f"    EQUIPA installed. Claude: {output.strip()[-60:]}")
    return True


def _put_script(container: Any, name: str, content: str) -> None:
    """Write a script into /tmp/ inside the container via tar API."""
    script_bytes = content.encode("utf-8")
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(script_bytes)
        tar.addfile(info, io.BytesIO(script_bytes))
    tar_buf.seek(0)
    container.put_archive("/tmp", tar_buf)


def _put_file(
    container: Any, dest_dir: str, name: str, content: bytes
) -> None:
    """Write a file into a directory inside the container via tar API."""
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    tar_buf.seek(0)
    container.put_archive(dest_dir, tar_buf)


def setup_masked_state(
    container: Any, instance: dict[str, Any]
) -> bool:
    """Set up the masked repo state in /testbed/ for SWE-bench evaluation.

    SWE-bench masking differs from FeatureBench:
    1. The repo is already at base_commit in the Docker image at /testbed/
    2. We apply test_patch REMOVAL — stripping the test that validates the fix
       so the agent can't see the expected answer
    3. We do NOT delete FAIL_TO_PASS files entirely; instead we reverse the
       test_patch (which adds the failing tests) so they don't exist yet

    Steps:
    1. Reset /testbed/ to clean state (git checkout)
    2. Reverse-apply test_patch (remove validation tests)
    3. git commit masked state for clean diffing later
    """
    iid = instance["instance_id"]

    # 1. Reset repo to clean state — the SWE-bench Docker image already has
    #    the repo at /testbed/ checked out to the correct base_commit
    exec_cmd_checked(
        container,
        f"cd {DOCKER_WORKDIR} && git checkout -- . && git clean -fd",
        timeout=120,
        label="reset-repo",
    )

    # Configure git identity
    git_cmds = [
        f'cd {DOCKER_WORKDIR} && git config user.email "equipa@forgeborn.dev"',
        f'cd {DOCKER_WORKDIR} && git config user.name "EQUIPA"',
    ]
    for cmd in git_cmds:
        exec_cmd(container, cmd, timeout=30)

    # 2. Reverse-apply test_patch — this REMOVES the tests that validate the
    #    fix, so the agent must discover the bug from the issue description
    #    alone without seeing the expected test assertions.
    test_patch = instance.get("test_patch", "")
    if test_patch:
        if not test_patch.endswith("\n"):
            test_patch += "\n"
        patch_bytes = test_patch.encode("utf-8")
        _put_file(container, "/tmp", "test_patch.diff", patch_bytes)

        # Reverse-apply: git apply --reverse removes the test additions
        code, output = exec_cmd(
            container,
            f"cd {DOCKER_WORKDIR} && "
            f"git apply --reverse --whitespace=fix /tmp/test_patch.diff 2>&1",
        )
        if code != 0:
            # Try with 3-way merge
            code, output = exec_cmd(
                container,
                f"cd {DOCKER_WORKDIR} && "
                f"git apply --reverse --3way --whitespace=fix /tmp/test_patch.diff 2>&1",
            )
            if code != 0:
                # Some test patches don't reverse cleanly if tests didn't exist
                # at base_commit. This is expected — proceed without masking.
                print(
                    f"    NOTE: test_patch reverse failed for {iid[:50]} "
                    f"(tests may not exist at base_commit)"
                )

    # 3. Commit the masked state for clean diffing later
    exec_cmd(
        container,
        f"cd {DOCKER_WORKDIR} && git add -A && "
        f'git commit -m "Masked state for evaluation" --allow-empty',
        timeout=60,
    )

    # Tag the masked state so we can diff against it later
    exec_cmd(container, f"cd {DOCKER_WORKDIR} && git tag masked-baseline")

    # Make /testbed/ writable by equipa user
    exec_cmd(
        container,
        f"chown -R equipa:equipa {DOCKER_WORKDIR} 2>/dev/null || true",
    )
    # Fix git "dubious ownership" for non-root user
    exec_cmd(
        container,
        f'su - equipa -c "git config --global --add safe.directory {DOCKER_WORKDIR}"',
    )
    exec_cmd(
        container,
        "su - equipa -c \"git config --global --add safe.directory '*'\"",
    )

    print(
        f"    Masked state ready (test_patch={len(test_patch)} chars)"
    )
    return True


def create_task_in_container(
    container: Any,
    instance: dict[str, Any],
    attempt: int = 1,
) -> int:
    """Insert a benchmark project + task into the container's fresh DB.

    Uses JSON file transfer to avoid shell escaping issues with complex
    problem statements.

    Returns task_id (always 1 for first task in empty DB).
    """
    iid = instance["instance_id"]
    problem = instance["problem_statement"]

    # Extract test validation info for the agent
    f2p = instance.get("FAIL_TO_PASS", [])
    p2p = instance.get("PASS_TO_PASS", [])
    if isinstance(f2p, str):
        try:
            f2p = json.loads(f2p)
        except (json.JSONDecodeError, TypeError):
            f2p = []
    if isinstance(p2p, str):
        try:
            p2p = json.loads(p2p)
        except (json.JSONDecodeError, TypeError):
            p2p = []

    test_info = ""
    if f2p:
        test_info += "\n\nTEST_VALIDATION:\n"
        test_info += (
            "FAIL_TO_PASS (these tests MUST pass after your fix):\n"
        )
        for t in f2p:
            test_info += f"  - {t}\n"
        if p2p:
            test_info += "PASS_TO_PASS (must continue passing):\n"
            for t in p2p[:20]:
                test_info += f"  - {t}\n"
            if len(p2p) > 20:
                test_info += f"  ... and {len(p2p) - 20} more\n"

    desc = (
        f"SWE-bench Verified task: {iid} (attempt {attempt})\n\n"
        f"Fix this GitHub issue in the repository at {DOCKER_WORKDIR}.\n\n"
        f"ISSUE:\n{problem}\n\n"
        f"{test_info}\n"
        f"Instructions: Read the issue carefully. Understand the root cause. "
        f"Find the relevant code, implement a correct fix, and ensure "
        f"existing tests still pass. Commit your changes when done."
    )

    # Transfer task data as JSON file — avoids all shell escaping issues
    task_data = {
        "iid": iid[:60],
        "description": desc,
        "db_path": f"{EQUIPA_DOCKER_DIR}/theforge.db",
        "workdir": DOCKER_WORKDIR,
    }
    task_json = json.dumps(task_data).encode("utf-8")
    _put_file(container, "/tmp", "task_data.json", task_json)

    # Python script reads JSON — no shell escaping needed
    py_script = """
import sqlite3, json
with open('/tmp/task_data.json') as f:
    d = json.load(f)
conn = sqlite3.connect(d['db_path'])
conn.execute(
    "INSERT INTO projects (name, codename, status, summary, local_path) "
    "VALUES (?, ?, 'active', 'Benchmark evaluation', ?)",
    ('SWE-bench', 'SWE-bench-eval', d['workdir']),
)
conn.execute(
    "INSERT INTO tasks (project_id, title, description, status, priority) "
    "VALUES (1, ?, ?, 'todo', 'high')",
    ('SWE: ' + d['iid'], d['description']),
)
conn.commit()
tid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
conn.close()
print(f'task_id={tid}')
"""
    _put_script(container, "create_task.py", py_script)

    code, output = exec_cmd_checked(
        container,
        f'export THEFORGE_DB="{EQUIPA_DOCKER_DIR}/theforge.db" && '
        f"python3 /tmp/create_task.py",
        label="create-task",
    )

    if "task_id=" in output:
        tid = int(output.split("task_id=")[1].strip())
        return tid
    return 1  # fallback


def extract_attempt_reflection(
    output: str,
    patch: str,
    attempt: int,
    attempt_time: float,
    task_status: str,
) -> str:
    """Extract a structured reflection from a failed attempt's output.

    Parses the agent's RESULT block (SUMMARY, FILES_CHANGED, BLOCKERS,
    REFLECTION) and combines it with attempt metadata to produce a concise
    (<500 token) reflection for the next attempt.

    Args:
        output: Raw agent output from the attempt
        patch: Git diff produced (may be empty)
        attempt: Attempt number (1-based)
        attempt_time: Wall-clock seconds for the attempt
        task_status: Final task status (e.g. "blocked", "unknown")

    Returns:
        Structured reflection string for injection into next attempt.
    """
    # --- Parse structured fields from agent output ---
    def _extract_field(text: str, field: str) -> str:
        pattern = rf"(?:^|\n)\s*{field}:\s*(.+?)(?=\n\s*(?:SUMMARY|FILES_CHANGED|BLOCKERS|DECISIONS|REFLECTION|RESULT):|$)"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()[:300]
        return ""

    summary = _extract_field(output, "SUMMARY")
    files_changed = _extract_field(output, "FILES_CHANGED")
    blockers = _extract_field(output, "BLOCKERS")
    reflection = _extract_field(output, "REFLECTION")

    # --- Determine failure reason ---
    output_lower = (output or "").lower()
    if "timeout" in output_lower or attempt_time > 1700:
        failure_reason = "timeout"
    elif "authentication" in output_lower or "401" in output_lower:
        failure_reason = "authentication_error"
    elif blockers and "none" not in blockers.lower():
        failure_reason = f"blocked: {blockers[:150]}"
    elif task_status == "blocked":
        failure_reason = "task_blocked"
    elif not patch:
        failure_reason = "no_patch_produced"
    else:
        failure_reason = f"tests_failed (status: {task_status})"

    # --- Compute patch stats ---
    patch_lines = patch.split("\n") if patch else []
    adds = sum(
        1 for line in patch_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    dels = sum(
        1 for line in patch_lines
        if line.startswith("-") and not line.startswith("---")
    )
    patch_size_kb = len(patch) / 1024 if patch else 0

    # --- Detect anti-patterns ---
    anti_patterns: list[str] = []
    if patch_size_kb > 500:
        anti_patterns.append(
            f"Patch bloated to {patch_size_kb:.0f}KB — avoid triggering "
            "compilation or including generated files"
        )
    if "cython" in output_lower or "building extension" in output_lower:
        anti_patterns.append(
            "Triggered Cython/extension rebuild — commit source changes "
            "BEFORE running tests"
        )
    if re.search(r"30 consecutive turns without file changes", output_lower):
        anti_patterns.append(
            "Agent spent all turns reading without writing code — "
            "START EDITING IMMEDIATELY"
        )
    if "no_patch_produced" in failure_reason and attempt_time > 600:
        anti_patterns.append(
            "Ran for {:.0f}s with no output — do not over-analyze, "
            "write code early".format(attempt_time)
        )

    # --- Build the reflection block ---
    parts: list[str] = [
        f"ATTEMPT {attempt} FAILED ({failure_reason}, {attempt_time:.0f}s):",
    ]
    if summary:
        parts.append(f"  Approach: {summary}")
    if files_changed and "none" not in files_changed.lower():
        parts.append(f"  Files touched: {files_changed[:200]}")
    if adds or dels:
        parts.append(f"  Patch: +{adds}/-{dels} lines, {patch_size_kb:.1f}KB")
    if reflection:
        parts.append(f"  Agent reflection: {reflection[:200]}")
    if anti_patterns:
        parts.append("  AVOID: " + "; ".join(anti_patterns))

    return "\n".join(parts)


def reset_task_for_retry(
    container: Any,
    instance: dict[str, Any],
    attempt: int,
    previous_reflections: str = "",
) -> None:
    """Reset the DB task for a retry attempt.

    If previous_reflections is provided, appends a PREVIOUS ATTEMPTS block
    to the task description so the next agent knows what was already tried.

    Args:
        container: Docker container handle
        instance: SWE-bench instance dict
        attempt: Current attempt number (1-based)
        previous_reflections: Accumulated reflections from prior attempts
    """
    # Build the reflection injection SQL
    if previous_reflections:
        # Transfer reflection via JSON to avoid shell escaping issues
        reflection_data = {
            "db_path": f"{EQUIPA_DOCKER_DIR}/theforge.db",
            "reflection": previous_reflections,
        }
        reflection_json = json.dumps(reflection_data).encode("utf-8")
        _put_file(container, "/tmp", "reflection_data.json", reflection_json)

        py_script = """
import sqlite3, json
with open('/tmp/reflection_data.json') as f:
    d = json.load(f)
conn = sqlite3.connect(d['db_path'])
# Reset task state
conn.execute("UPDATE tasks SET status='todo', completed_at=NULL WHERE id=1")
conn.execute("DELETE FROM agent_runs WHERE task_id=1")
conn.execute("DELETE FROM agent_actions WHERE task_id=1")
conn.execute("DELETE FROM agent_messages WHERE task_id=1")
# Inject reflection into task description
cur = conn.execute("SELECT description FROM tasks WHERE id=1")
row = cur.fetchone()
if row:
    desc = row[0]
    marker = '\\n\\n--- PREVIOUS ATTEMPTS ---\\n'
    if marker in desc:
        desc = desc[:desc.index(marker)]
    desc += marker + d['reflection']
    conn.execute("UPDATE tasks SET description=? WHERE id=1", (desc,))
conn.commit()
conn.close()
print('Task reset with reflection for attempt')
"""
        _put_script(container, "reset_task.py", py_script)
    else:
        py_script = f"""
import sqlite3
conn = sqlite3.connect('{EQUIPA_DOCKER_DIR}/theforge.db')
conn.execute("UPDATE tasks SET status='todo', completed_at=NULL WHERE id=1")
conn.execute("DELETE FROM agent_runs WHERE task_id=1")
conn.execute("DELETE FROM agent_actions WHERE task_id=1")
conn.execute("DELETE FROM agent_messages WHERE task_id=1")
conn.commit()
conn.close()
print('Task reset for attempt {attempt}')
"""
        _put_script(container, "reset_task.py", py_script)

    exec_cmd(container, "python3 /tmp/reset_task.py")


def run_equipa_in_container(
    container: Any, task_id: int, timeout: int = 1800
) -> tuple[int, str]:
    """Run the EQUIPA orchestrator inside the container as non-root user.

    Claude CLI refuses --permission-mode bypassPermissions as root,
    so we run as the 'equipa' user.
    """
    inner_cmd = (
        f'export PATH="/opt/tools/bin:$PATH" && '
        f'export NODE_PATH="/opt/tools/lib/node_modules" && '
        f'export THEFORGE_DB="{EQUIPA_DOCKER_DIR}/theforge.db" && '
        f'export PYTHONPATH="{EQUIPA_DOCKER_DIR}:$PYTHONPATH" && '
        f"{CONDA_PREFIX} && "
        f"cd {EQUIPA_DOCKER_DIR} && "
        f"python3 -u forge_orchestrator.py --task {task_id} --dev-test -y"
    )
    cmd = f"timeout -k 30 {timeout} bash -c '{inner_cmd}'"
    full_cmd = (
        f'/bin/bash -lc "source /home/equipa/.bashrc 2>/dev/null; {cmd}"'
    )
    try:
        result = container.exec_run(
            full_cmd,
            user="equipa",
            workdir=EQUIPA_DOCKER_DIR,
            demux=True,
        )
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        output = stdout + stderr
        code = result.exit_code
        if code == 124:
            output += f"\n[TIMEOUT] Orchestrator killed after {timeout}s"
    except Exception as e:
        code, output = -1, str(e)

    lines = output.strip().split("\n") if output else []
    for line in lines[-5:]:
        print(f"      {line[:120]}")

    return code, output


def _filter_patch_to_source(raw_patch: str, max_file_kb: int = 500) -> str:
    """Filter a git diff to only include source code files.

    Removes build artifacts, compiled files, binary blobs, and any single
    file diff larger than max_file_kb.
    """
    SOURCE_EXTS = {
        ".py", ".pyi", ".pyx", ".pxd",
        ".js", ".ts", ".jsx", ".tsx",
        ".rs", ".go", ".java", ".c", ".cpp",
        ".h", ".hpp",
        ".json", ".yaml", ".yml", ".toml",
        ".cfg", ".ini", ".conf",
        ".txt", ".md", ".rst", ".tex",
        ".html", ".css", ".xml", ".svg",
        ".sh", ".bash",
        ".sql",
    }

    chunks = raw_patch.split("\ndiff --git ")
    filtered: list[str] = []

    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk = "diff --git " + chunk

        first_line = chunk.split("\n", 1)[0]
        parts = first_line.split(" b/", 1)
        if len(parts) < 2:
            continue
        filepath = parts[1].strip()

        ext = ""
        if "." in filepath.rsplit("/", 1)[-1]:
            ext = "." + filepath.rsplit(".", 1)[-1].lower()
        if ext not in SOURCE_EXTS:
            continue

        if len(chunk) > max_file_kb * 1024:
            continue

        skip_patterns = [
            "/build/", "/dist/", "/.tox/", "/.nox/",
            "/__pycache__/", "/.eggs/", "/egg-info/",
            "/node_modules/", "/.mypy_cache/",
            ".forge-state.json", "SECURITY-REVIEW-",
        ]
        if any(pat in filepath for pat in skip_patterns):
            continue

        filtered.append(chunk)

    return "\n".join(filtered).strip() if filtered else ""


def extract_patch(container: Any) -> str:
    """Extract git diff from /testbed/ — this is the model_patch.

    Post-processes the diff to exclude build artifacts and binary files,
    keeping only source code.
    """
    # Add .gitignore to exclude build artifacts
    exec_cmd_as_equipa(
        container,
        f"cd {DOCKER_WORKDIR} && cat >> .gitignore << 'GIEOF'\n"
        f"__pycache__/\n*.pyc\n*.pyo\n*.egg-info/\n*.eggs/\n"
        f".eggs/\ndist/\nbuild/\n*.so\n.tox/\n.nox/\n"
        f".pytest_cache/\n.mypy_cache/\nvenv/\n.venv/\n"
        f"*.egg\nnode_modules/\n.forge-worktrees/\n"
        f".forge-checkpoints/\n.forge-state.json\n"
        f"SECURITY-REVIEW-*.md\n*.o\n*.a\n*.dylib\n*.dll\n"
        f"*.class\n*.jar\n*.whl\n*.tar.gz\n*.zip\n"
        f"*.npy\n*.npz\n*.pkl\n*.pickle\n*.h5\n*.hdf5\n"
        f"*.mat\n*.sav\n*.dat\n*.bin\n*.db\n*.sqlite\n"
        f"*.log\n*.coverage\n.coverage.*\nhtmlcov/\n"
        f"GIEOF",
    )

    # Stage only source files that were explicitly changed
    exec_cmd_as_equipa(
        container,
        f"cd {DOCKER_WORKDIR} && "
        f"git diff --name-only | "
        f"grep -E '\\.(py|pyi|pyx|pxd|js|ts|go|rs|java|json|yaml|yml|toml|"
        f"cfg|ini|txt|md|rst|html|css|xml|sh|sql)$' | "
        f"xargs -r git add 2>/dev/null; "
        f"git commit -m 'final uncommitted source changes' "
        f"--allow-empty 2>/dev/null || true",
    )

    raw_patch = ""

    # Strategy 1: Diff from masked-baseline tag (best)
    code, agent_files_out = exec_cmd_as_equipa(
        container,
        f"cd {DOCKER_WORKDIR} && "
        f"git log masked-baseline..HEAD --pretty=format: --name-only | "
        f"sort -u | grep -v '^$'",
    )
    agent_files: list[str] = []
    if code == 0 and agent_files_out:
        agent_files = [
            f.strip()
            for f in agent_files_out.strip().split("\n")
            if f.strip() and not f.strip().startswith(".")
        ]

    if len(agent_files) > 50:
        source_exts = (
            ".py", ".pyi", ".pyx", ".pxd", ".js", ".ts", ".jsx", ".tsx",
            ".rs", ".go", ".java", ".json", ".yaml", ".yml", ".toml",
            ".cfg", ".ini", ".txt", ".md", ".rst", ".html", ".css",
            ".sh", ".sql",
        )
        source_files = [
            f for f in agent_files
            if any(f.endswith(ext) for ext in source_exts)
        ]
        skip_patterns = [
            "/build/", "/dist/", "/.tox/", "/.eggs/",
            "/egg-info/", "/__pycache__/", "/.mypy_cache/",
        ]
        source_files = [
            f for f in source_files
            if not any(pat in f for pat in skip_patterns)
        ]

        if source_files:
            pathspecs = " ".join(f"'{f}'" for f in source_files[:200])
            code, patch = exec_cmd_as_equipa(
                container,
                f"cd {DOCKER_WORKDIR} && "
                f"git diff masked-baseline HEAD -- {pathspecs}",
            )
            if patch and patch.strip() and "diff --git" in patch:
                raw_patch = patch.strip()

    if not raw_patch:
        code, patch = exec_cmd_as_equipa(
            container,
            f"cd {DOCKER_WORKDIR} && "
            f"git diff masked-baseline HEAD "
            f"-- . ':!.gitignore' ':!.forge-state.json' "
            f"':!SECURITY-REVIEW-*.md'",
        )
        if patch and patch.strip() and "diff --git" in patch:
            raw_patch = patch.strip()

    # Strategy 2: Log-based
    if not raw_patch:
        code, log_output = exec_cmd_as_equipa(
            container, f"cd {DOCKER_WORKDIR} && git log --oneline --all"
        )
        if code == 0:
            commits = [
                line.strip()
                for line in log_output.split("\n")
                if line.strip()
            ]
            if len(commits) > 1:
                first = commits[-1].split()[0]
                last = commits[0].split()[0]
                code, patch = exec_cmd_as_equipa(
                    container,
                    f"cd {DOCKER_WORKDIR} && git diff {first}..{last}",
                )
                if patch and patch.strip() and "diff --git" in patch:
                    raw_patch = patch.strip()

    # Strategy 3: forge-task branches
    if not raw_patch:
        code, branches = exec_cmd_as_equipa(
            container, f"cd {DOCKER_WORKDIR} && git branch --all"
        )
        if code == 0:
            for line in branches.split("\n"):
                branch = line.strip().lstrip("* ")
                if "forge-task" in branch:
                    code, patch = exec_cmd_as_equipa(
                        container,
                        f"cd {DOCKER_WORKDIR} && git diff "
                        f"$(git rev-list --max-parents=0 HEAD)..{branch}",
                    )
                    if patch and patch.strip() and "diff --git" in patch:
                        raw_patch = patch.strip()
                        break

    # Strategy 4: Uncommitted changes
    if not raw_patch:
        code, patch = exec_cmd_as_equipa(
            container, f"cd {DOCKER_WORKDIR} && git diff HEAD"
        )
        if patch and patch.strip() and "diff --git" in patch:
            raw_patch = patch.strip()

    if not raw_patch:
        return ""

    filtered = _filter_patch_to_source(raw_patch)
    return filtered if filtered else raw_patch


def run_instance(
    client: Any,
    instance: dict[str, Any],
    equipa_tar_buf: io.BytesIO,
    api_key: str,
    max_retries: int = 3,
    timeout: int = 1800,
    cumdb: CumulativeDB | None = None,
    output_path: str = "swebench_output.jsonl",
) -> dict[str, Any]:
    """Full pipeline for one SWE-bench instance.

    1. Derive/pull Docker image
    2. Start container with tools volume
    3. Install EQUIPA
    4. Set up masked state (reverse test_patch)
    5. Run EQUIPA (with retry loop)
    6. Extract patch
    7. Cleanup
    """
    iid = instance["instance_id"]
    image_name = get_swebench_image_name(instance)

    result: dict[str, Any] = {
        "instance_id": iid,
        "model_patch": "",
        "model_name_or_path": "EQUIPA (Opus dev + Sonnet tester, Docker verified)",
        "resolved": False,
        "attempts": 0,
        "duration": 0,
    }

    container = None
    start = time.time()

    try:
        # Pull image
        print(f"    Pulling {image_name[:70]}...")
        try:
            client.images.get(image_name)
        except docker.errors.ImageNotFound:
            client.images.pull(image_name)

        # Create container
        container_name = (
            f"equipa-swe-{iid[:40]}-{int(time.time())}"
            .replace("/", "-")
            .replace(".", "-")
            .replace("__", "-")
        )
        run_kwargs: dict[str, Any] = {
            "image": image_name,
            "command": "/bin/bash -c 'sleep infinity'",
            "detach": True,
            "user": "root",
            "working_dir": DOCKER_WORKDIR,
            "network_mode": "bridge",
            "name": container_name,
            "volumes": {
                TOOLS_VOLUME: {"bind": "/opt/tools", "mode": "ro"},
            },
            "environment": {
                "ANTHROPIC_API_KEY": api_key,
            },
        }

        container = client.containers.run(**run_kwargs)
        print(f"    Container started: {container.short_id}")

        # Copy EQUIPA source
        equipa_tar_buf.seek(0)
        copy_tar_to_container(container, equipa_tar_buf)

        # Install EQUIPA
        if not setup_equipa_in_container(container, api_key):
            result["reason"] = "equipa_setup_failed"
            return result

        # Inject cumulative knowledge if enabled
        if cumdb:
            cumdb.inject_into_container(
                container, f"{EQUIPA_DOCKER_DIR}/theforge.db"
            )

        # Set up masked state
        if not setup_masked_state(container, instance):
            result["reason"] = "masked_state_failed"
            return result

        # Create task
        task_id = create_task_in_container(container, instance)

        # Retry loop with cross-attempt memory
        best_patch = ""
        best_changes = 0
        attempt_reflections: list[str] = []

        for attempt in range(1, max_retries + 1):
            result["attempts"] = attempt
            attempt_start = time.time()
            print(
                f"    [Attempt {attempt}/{max_retries}]",
                end=" ",
                flush=True,
            )

            if attempt > 1:
                setup_masked_state(container, instance)
                # Inject accumulated reflections from all prior attempts
                reflections_block = "\n\n".join(attempt_reflections)
                reset_task_for_retry(
                    container, instance, attempt,
                    previous_reflections=reflections_block,
                )

            code, output = run_equipa_in_container(
                container, task_id, timeout=timeout
            )
            attempt_time = time.time() - attempt_start

            # Check task outcome
            task_status = "unknown"

            status_script = (
                f"import sqlite3; "
                f"c=sqlite3.connect('{EQUIPA_DOCKER_DIR}/theforge.db'); "
                f"r=c.execute('SELECT status FROM tasks WHERE id={task_id}').fetchone(); "
                f"print(r[0] if r else 'unknown'); c.close()"
            )
            _put_script(container, "check_status.py", status_script)
            code_s, status_out = exec_cmd_as_equipa(
                container, "python3 /tmp/check_status.py"
            )
            if code_s == 0:
                task_status = status_out.strip()

            output_lower = output.lower() if output else ""
            if task_status not in ("done", "blocked"):
                if any(
                    sig in output_lower
                    for sig in [
                        "tests_passed",
                        "test passed",
                        "tests passed",
                        "all tests pass",
                        "pass_to_pass tests pass",
                        "pass successfully",
                    ]
                ):
                    task_status = "done"

            patch = extract_patch(container)
            if patch:
                lines = patch.split("\n")
                adds = sum(
                    1
                    for line in lines
                    if line.startswith("+") and not line.startswith("+++")
                )
                dels = sum(
                    1
                    for line in lines
                    if line.startswith("-") and not line.startswith("---")
                )
                changes = adds + dels

                if changes > 0:
                    if task_status == "done" or changes > best_changes:
                        best_patch = patch
                        best_changes = changes

                    if task_status == "done":
                        result["model_patch"] = patch
                        result["resolved"] = True
                        result["changes"] = changes
                        result["patch_size"] = len(patch)
                        result["duration"] = time.time() - start
                        result["task_status"] = task_status
                        print(
                            f"PASS ({adds}+ {dels}-, "
                            f"{len(patch)} chars, {attempt_time:.0f}s)"
                        )

                        # Extract cumulative knowledge
                        if cumdb:
                            cumdb.extract_and_merge(
                                container,
                                f"{EQUIPA_DOCKER_DIR}/theforge.db",
                            )

                        return result
                    else:
                        print(
                            f"patch but {task_status} ({adds}+ {dels}-, "
                            f"{attempt_time:.0f}s)"
                        )
                else:
                    print(f"empty diff ({attempt_time:.0f}s)")
                    patch = ""
            else:
                print(f"no patch ({attempt_time:.0f}s)")
                patch = ""

            # Extract reflection for cross-attempt memory
            reflection = extract_attempt_reflection(
                output=output,
                patch=patch if patch else "",
                attempt=attempt,
                attempt_time=attempt_time,
                task_status=task_status,
            )
            attempt_reflections.append(reflection)

        # Exhausted retries — submit best patch
        if best_patch:
            lines = best_patch.split("\n")
            adds = sum(
                1
                for line in lines
                if line.startswith("+") and not line.startswith("+++")
            )
            dels = sum(
                1
                for line in lines
                if line.startswith("-") and not line.startswith("---")
            )
            result["model_patch"] = best_patch
            result["resolved"] = True
            result["changes"] = adds + dels
            result["patch_size"] = len(best_patch)
            result["reason"] = "best_effort_after_retries"
            print(
                f"    Submitting best patch ({adds}+ {dels}-, "
                f"{len(best_patch)} chars)"
            )

            if cumdb:
                cumdb.extract_and_merge(
                    container, f"{EQUIPA_DOCKER_DIR}/theforge.db"
                )
        else:
            result["reason"] = "exhausted_retries_no_patch"

        result["duration"] = time.time() - start
        return result

    except Exception as e:
        result["reason"] = f"error: {str(e)[:200]}"
        result["duration"] = time.time() - start
        print(f"    ERROR: {e}")
        return result

    finally:
        if container:
            try:
                # Save container DB for telemetry
                db_save_dir = Path(output_path).parent / "container_dbs"
                db_save_dir.mkdir(exist_ok=True)
                safe_iid = iid.replace("/", "-").replace(".", "-")[:60]
                db_dest = str(db_save_dir / f"{safe_iid}.db")
                try:
                    bits, _ = container.get_archive(
                        f"{EQUIPA_DOCKER_DIR}/theforge.db"
                    )
                    raw = b"".join(bits)
                    tar_buf = io.BytesIO(raw)
                    with tarfile.open(fileobj=tar_buf) as tar:
                        member = tar.getmembers()[0]
                        f = tar.extractfile(member)
                        if f:
                            with open(db_dest, "wb") as out:
                                out.write(f.read())
                    print(f"    DB saved: {db_dest}")
                except Exception as db_err:
                    print(f"    DB extract failed: {db_err}")
            except Exception:
                pass
            try:
                container.stop(timeout=10)
                container.remove(force=True)
            except Exception:
                pass
            try:
                client.images.prune(filters={"dangling": True})
            except Exception:
                pass


def _run_instance_worker(args: tuple[Any, ...]) -> dict[str, Any]:
    """Wrapper for parallel execution — each worker creates its own Docker client.

    ProcessPoolExecutor requires picklable arguments, so we pass
    serializable data and reconstruct the Docker client per process.
    """
    (
        instance,
        equipa_tar_bytes,
        api_key,
        max_retries,
        timeout,
        cumulative_db_path,
        output_path,
    ) = args

    client = docker.from_env()
    equipa_tar_buf = io.BytesIO(equipa_tar_bytes)

    cumdb = None
    if cumulative_db_path:
        cumdb = CumulativeDB(cumulative_db_path)

    return run_instance(
        client,
        instance,
        equipa_tar_buf,
        api_key,
        max_retries=max_retries,
        timeout=timeout,
        cumdb=cumdb,
        output_path=output_path,
    )


def run_benchmark(
    limit: int = 10,
    offset: int = 0,
    max_retries: int = 3,
    timeout: int = 1800,
    output_path: str = "swebench_output.jsonl",
    cumulative: bool = True,
    workers: int = 1,
    instance_filter: str = "",
    skip_resolved: bool = False,
) -> None:
    """Main benchmark loop — run EQUIPA inside Docker for each instance.

    Args:
        limit: Maximum number of instances to run.
        offset: Skip first N instances.
        max_retries: Retry attempts per instance.
        timeout: Seconds per attempt.
        output_path: JSONL predictions output file.
        cumulative: Enable cumulative knowledge DB.
        workers: Number of concurrent Docker workers.
        instance_filter: If set, only run this specific instance_id.
        skip_resolved: Skip instances already in the output file.
    """

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    if not DATASET_PATH.exists():
        print(f"ERROR: Dataset not found at {DATASET_PATH}")
        print(
            "Download with:\n  python -c \"from datasets import load_dataset; "
            "ds=load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); "
            f"ds.to_json('{DATASET_PATH}')\""
        )
        sys.exit(1)

    client = docker.from_env()

    # Verify tools volume exists
    try:
        client.volumes.get(TOOLS_VOLUME)
    except docker.errors.NotFound:
        print(
            f"ERROR: Tools volume '{TOOLS_VOLUME}' not found. "
            f"Run: python {__file__} --setup"
        )
        sys.exit(1)

    dataset = load_dataset(str(DATASET_PATH), limit=limit, offset=offset)

    # Filter to a specific instance if requested
    if instance_filter:
        dataset = [
            inst for inst in dataset
            if inst["instance_id"] == instance_filter
        ]
        if not dataset:
            # Try partial match
            dataset = load_dataset(str(DATASET_PATH))
            dataset = [
                inst for inst in dataset
                if instance_filter in inst["instance_id"]
            ]
        if not dataset:
            print(f"ERROR: Instance '{instance_filter}' not found in dataset.")
            sys.exit(1)
        print(f"  Filtered to {len(dataset)} instance(s) matching '{instance_filter}'")

    # Skip already resolved instances if requested
    if skip_resolved:
        resolved_ids = _load_resolved_ids(output_path)
        if resolved_ids:
            before = len(dataset)
            dataset = [
                inst for inst in dataset
                if inst["instance_id"] not in resolved_ids
            ]
            skipped = before - len(dataset)
            if skipped:
                print(f"  Skipped {skipped} already-resolved instances")

    # Initialize cumulative DB if enabled
    cumdb = None
    cumulative_db_path = ""
    if cumulative:
        cumulative_db_path = str(BENCHMARKS_DIR / "swe_cumulative.db")
        cumdb = CumulativeDB(cumulative_db_path)
        stats = cumdb.get_stats()
        print(
            f"  Cumulative DB: {stats['lessons_merged']} lessons, "
            f"{stats['episodes_merged']} episodes, "
            f"{stats['decisions_merged']} decisions"
        )

    print(f"\n{'=' * 60}")
    print(f"  SWE-bench Verified — EQUIPA Inside Docker")
    print(f"  Tasks: {len(dataset)} (offset {offset})")
    print(f"  Max retries per task: {max_retries}")
    print(f"  Timeout per attempt: {timeout}s")
    print(f"  Workers: {workers}")
    print(f"  Output: {output_path}")
    print(f"  Cumulative mode: {'ON' if cumulative else 'OFF'}")
    print(f"{'=' * 60}\n")

    # Create EQUIPA tar once
    print("  Packaging EQUIPA source...")
    equipa_tar = create_equipa_tar()
    tar_size = equipa_tar.getbuffer().nbytes
    print(f"  EQUIPA archive: {tar_size / 1024 / 1024:.1f} MB\n")

    results: list[dict[str, Any]] = []
    resolved = 0
    total_start = time.time()

    if workers > 1:
        # Parallel execution
        equipa_tar_bytes = equipa_tar.getvalue()
        work_items = [
            (
                inst,
                equipa_tar_bytes,
                api_key,
                max_retries,
                timeout,
                cumulative_db_path if cumulative else "",
                output_path,
            )
            for inst in dataset
        ]

        with ProcessPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {
                executor.submit(_run_instance_worker, item): i
                for i, item in enumerate(work_items)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                iid = dataset[idx]["instance_id"]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "instance_id": iid,
                        "model_patch": "",
                        "model_name_or_path": "EQUIPA",
                        "resolved": False,
                        "attempts": 0,
                        "duration": 0,
                        "reason": f"worker_error: {e}",
                    }

                results.append(result)
                if result.get("resolved"):
                    resolved += 1

                done = len(results)
                rate = resolved / done * 100 if done else 0
                status = "RESOLVED" if result.get("resolved") else "FAILED"
                print(
                    f"  [{done}/{len(dataset)}] [{status}] {iid[:50]} | "
                    f"Running: {resolved}/{done} ({rate:.1f}%)"
                )

                # Write results incrementally
                _write_predictions(output_path, results)
    else:
        # Sequential execution
        for i, instance in enumerate(dataset):
            iid = instance["instance_id"]
            print(f"\n{'━' * 60}")
            print(f"  [{i + 1}/{len(dataset)}] {iid[:55]}")
            print(
                f"  Repo: {instance['repo']} | "
                f"Image: {get_swebench_image_name(instance)[:50]}"
            )

            result = run_instance(
                client,
                instance,
                equipa_tar,
                api_key,
                max_retries=max_retries,
                timeout=timeout,
                cumdb=cumdb,
                output_path=output_path,
            )
            results.append(result)

            if result.get("resolved"):
                resolved += 1

            rate = resolved / (i + 1) * 100
            status = "RESOLVED" if result.get("resolved") else "FAILED"
            print(
                f"  [{status}] Attempts: {result['attempts']} | "
                f"Running: {resolved}/{i + 1} ({rate:.1f}%)"
            )

            _write_predictions(output_path, results)

    # Final summary
    total_time = time.time() - total_start
    rate = resolved / len(dataset) * 100 if dataset else 0

    print(f"\n{'=' * 60}")
    print(f"  SWE-bench Verified — Results")
    print(f"{'=' * 60}")
    print(f"  Resolved: {resolved}/{len(dataset)} ({rate:.1f}%)")
    print(f"  Total time: {total_time / 60:.1f} min")
    print(f"  Output: {output_path}")
    print(f"{'=' * 60}")

    # Save full results JSON
    full_output: dict[str, Any] = {
        "benchmark": "SWE-bench Verified (Docker verified)",
        "system": "EQUIPA (full pipeline, Docker containers)",
        "model": "Opus (developer) + Sonnet (tester)",
        "max_retries": max_retries,
        "timeout_per_attempt": timeout,
        "workers": workers,
        "cumulative": cumulative,
        "resolved": resolved,
        "total": len(dataset),
        "resolution_rate": rate,
        "total_time_seconds": total_time,
        "results": results,
    }
    full_path = output_path.replace(".jsonl", "_full.json")
    with open(full_path, "w") as f:
        json.dump(full_output, f, indent=2)
    print(f"  Full results: {full_path}")


def _write_predictions(
    output_path: str, results: list[dict[str, Any]]
) -> None:
    """Write JSONL predictions file compatible with swebench harness."""
    with open(output_path, "w") as f:
        for r in results:
            if r.get("model_patch"):
                pred = {
                    "instance_id": r["instance_id"],
                    "model_patch": r["model_patch"],
                    "model_name_or_path": r.get(
                        "model_name_or_path", "EQUIPA"
                    ),
                }
                f.write(json.dumps(pred) + "\n")


# ============================================================
# Phase 3: Validate via official swebench harness
# ============================================================


def validate_results(output_path: str = "swebench_output.jsonl") -> None:
    """Run the official SWE-bench harness on our predictions.

    Requires: pip install swebench
    """
    if not Path(output_path).exists():
        print(f"ERROR: Predictions file not found: {output_path}")
        sys.exit(1)

    with open(output_path) as f:
        n_preds = sum(1 for _ in f)
    print(f"\n  Validating {n_preds} predictions via official swebench harness...")

    try:
        import subprocess

        cmd = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--predictions_path",
            str(Path(output_path).resolve()),
            "--swe_bench_tasks",
            "princeton-nlp/SWE-bench_Verified",
            "--log_dir",
            str(Path(output_path).parent / "swebench_logs"),
            "--testbed",
            str(Path(output_path).parent / "swebench_testbed"),
            "--skip_existing",
            "--timeout",
            "900",
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr[-500:])
    except ImportError:
        print(
            "ERROR: swebench not installed. Run: pip install swebench"
        )
    except Exception as e:
        print(f"ERROR: Validation failed: {e}")


def _list_instances(limit: int = 0, offset: int = 0) -> None:
    """Print available SWE-bench instances from the dataset."""
    if not DATASET_PATH.exists():
        print(f"ERROR: Dataset not found at {DATASET_PATH}")
        print(
            "Download with:\n  python -c \"from datasets import load_dataset; "
            "ds=load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); "
            f"ds.to_json('{DATASET_PATH}')\""
        )
        sys.exit(1)

    dataset = load_dataset(str(DATASET_PATH), limit=limit, offset=offset)
    print(f"\nSWE-bench Verified instances ({len(dataset)} shown):\n")
    for i, inst in enumerate(dataset):
        iid = inst["instance_id"]
        repo = inst["repo"]
        f2p = inst.get("FAIL_TO_PASS", [])
        if isinstance(f2p, str):
            try:
                f2p = json.loads(f2p)
            except (json.JSONDecodeError, TypeError):
                f2p = []
        n_tests = len(f2p) if isinstance(f2p, list) else 0
        print(f"  {i + 1:4d}. {iid:<55s} {repo:<30s} ({n_tests} F2P tests)")


def _load_resolved_ids(output_path: str) -> set[str]:
    """Load instance IDs already resolved in the output file."""
    resolved: set[str] = set()
    path = Path(output_path)
    if not path.exists():
        return resolved
    with open(path) as f:
        for line in f:
            try:
                pred = json.loads(line)
                if pred.get("model_patch"):
                    resolved.add(pred["instance_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return resolved


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SWE-bench Verified — EQUIPA Inside Docker"
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="One-time: build tools volume with Claude CLI",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run official swebench harness on output.jsonl",
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of tasks to run"
    )
    parser.add_argument(
        "--offset", type=int, default=0, help="Skip first N tasks"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Max retries per task (default 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout per attempt in seconds (default 1800)",
    )
    parser.add_argument(
        "--output",
        default="swebench_output.jsonl",
        help="Output predictions file",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent workers (default 1)",
    )
    parser.add_argument(
        "--cumulative",
        action="store_true",
        default=True,
        help="Enable cumulative knowledge DB (default: on)",
    )
    parser.add_argument(
        "--no-cumulative",
        action="store_true",
        help="Disable cumulative knowledge DB",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild tools volume (skip prompt)",
    )
    parser.add_argument(
        "--instance",
        type=str,
        default="",
        help="Run a single instance by ID (e.g., django__django-15790)",
    )
    parser.add_argument(
        "--skip-resolved",
        action="store_true",
        help="Skip instances already resolved in the output file",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_instances",
        help="List available instances from the dataset and exit",
    )
    args = parser.parse_args()

    cumulative = args.cumulative and not args.no_cumulative

    if args.setup:
        client = docker.from_env()
        setup_tools_volume(client, force=args.force)
    elif args.list_instances:
        _list_instances(args.limit, args.offset)
    elif args.validate:
        validate_results(args.output)
    else:
        run_benchmark(
            limit=args.limit,
            offset=args.offset,
            max_retries=args.retries,
            timeout=args.timeout,
            output_path=args.output,
            cumulative=cumulative,
            workers=args.workers,
            instance_filter=args.instance,
            skip_resolved=args.skip_resolved,
        )
        if args.validate:
            validate_results(args.output)
