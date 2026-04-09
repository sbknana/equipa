#!/usr/bin/env python3
"""Quick single-task test of the full EQUIPA pipeline on SWE-bench."""
import subprocess, os, tempfile, json, sqlite3, sys, time

EQUIPA_ROOT = "/srv/forge-share/AI_Stuff/Equipa"
DB_PATH = os.path.join(EQUIPA_ROOT, "theforge.db")

# Load one task
with open(os.path.join(EQUIPA_ROOT, "benchmarks/swebench_verified.jsonl")) as f:
    lines = f.readlines()
ds = json.loads(lines[20])
iid = ds["instance_id"]
print(f"Testing: {iid} ({ds['repo']})")

# Setup repo
work_dir = tempfile.mkdtemp(prefix="swebench_test_")
repo_dir = os.path.join(work_dir, "repo")
subprocess.run(["git", "init", repo_dir], capture_output=True, check=True)
subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{ds['repo']}.git"],
               cwd=repo_dir, capture_output=True, check=True)
print("Fetching commit...")
subprocess.run(["git", "fetch", "--depth", "1", "origin", ds["base_commit"]],
               cwd=repo_dir, capture_output=True, check=True, timeout=180)
subprocess.run(["git", "checkout", "FETCH_HEAD"],
               cwd=repo_dir, capture_output=True, check=True)
subprocess.run(["git", "checkout", "-b", "main"],
               cwd=repo_dir, capture_output=True)
print(f"Repo ready: {repo_dir}")

# Update project path and create task
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE projects SET local_path = ? WHERE id = 65", (repo_dir,))
conn.commit()
desc = f"Fix this GitHub issue:\n\n{ds['problem_statement'][:2000]}"
conn.execute(
    "INSERT INTO tasks (project_id, title, description, status, priority) VALUES (65, ?, ?, 'todo', 'high')",
    (f"SWE-bench: {iid}", desc),
)
conn.commit()
task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.close()
print(f"Task #{task_id} created")

# Run orchestrator
print("Running EQUIPA orchestrator...")
start = time.time()
result = subprocess.run(
    ["python3", "-u", os.path.join(EQUIPA_ROOT, "forge_orchestrator.py"),
     "--task", str(task_id), "--dev-test", "-y"],
    capture_output=True, text=True, timeout=600,
    env={**os.environ, "THEFORGE_DB": DB_PATH},
)
duration = time.time() - start

print(f"\nReturn code: {result.returncode}")
print(f"Duration: {duration:.0f}s")
print(f"\n--- STDOUT (last 1000 chars) ---")
print(result.stdout[-1000:])
if result.stderr:
    print(f"\n--- STDERR (last 500 chars) ---")
    print(result.stderr[-500:])

# Check patch
diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
print(f"\nPatch size: {len(diff.stdout)} chars")
if diff.stdout:
    print("RESOLVED — patch generated")
else:
    # Check branches
    branches = subprocess.run(["git", "branch", "-a"], cwd=repo_dir, capture_output=True, text=True)
    print(f"Branches: {branches.stdout.strip()}")
    for line in branches.stdout.strip().split("\n"):
        branch = line.strip().lstrip("* ")
        if "forge-task" in branch:
            bd = subprocess.run(["git", "diff", f"main..{branch}"], cwd=repo_dir, capture_output=True, text=True)
            if bd.stdout.strip():
                print(f"Found patch on branch {branch}: {len(bd.stdout)} chars")
                break
    else:
        print("UNRESOLVED — no patch found")
