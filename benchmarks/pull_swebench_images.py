#!/usr/bin/env python3
"""
Pull Docker images for SWE-bench Verified instances.

Usage:
    python pull_swebench_images.py --limit 50  # Pull first 50
    python pull_swebench_images.py --all       # Pull all
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

DATASET_PATH = Path(__file__).parent / "swebench_verified_full.jsonl"


def load_instances(limit=None):
    """Load SWE-bench instances from dataset."""
    instances = []
    with open(DATASET_PATH) as f:
        for line in f:
            instances.append(json.loads(line))
            if limit and len(instances) >= limit:
                break
    return instances


def extract_instance_ids(instances):
    """Extract instance_id list from instances."""
    return [inst["instance_id"] for inst in instances]


def main():
    parser = argparse.ArgumentParser(description="Pull SWE-bench Docker images")
    parser.add_argument("--limit", type=int, help="Pull images for first N instances")
    parser.add_argument("--all", action="store_true", help="Pull all images")
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel workers")
    args = parser.parse_args()

    if not args.all and not args.limit:
        print("ERROR: Specify --limit N or --all")
        sys.exit(1)

    # Load instances
    instances = load_instances(limit=args.limit)
    instance_ids = extract_instance_ids(instances)

    print(f"Pulling Docker images for {len(instance_ids)} instances...")
    print(f"Dataset: {DATASET_PATH}")
    print(f"Workers: {args.max_workers}")
    print()

    # Build command
    cmd = [
        "python", "-m", "swebench.harness.prepare_images",
        "--dataset_name", "princeton-nlp/SWE-bench_Verified",
        "--split", "test",
        "--max_workers", str(args.max_workers),
        "--instance_ids"
    ] + instance_ids

    print("Running:", " ".join(cmd[:7]), f"--instance_ids ... ({len(instance_ids)} IDs)")
    print()

    # Execute
    try:
        subprocess.run(cmd, check=True)
        print(f"\n✓ Successfully pulled images for {len(instance_ids)} instances")
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Failed to pull images: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
