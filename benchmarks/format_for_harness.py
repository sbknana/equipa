#!/usr/bin/env python3
"""
Format EQUIPA FeatureBench results for official harness evaluation.
(c) 2026 Forgeborn

Takes the results JSON from featurebench_runner.py and converts it
to the output.jsonl format that FeatureBench's harness expects.

The challenge: our runner doesn't save the actual patches (only patch_size).
This script re-runs the patch extraction on completed tasks by checking
their git state, OR reads patches from a separate collection step.

For the current run: we need to modify featurebench_runner.py to save
patches going forward. For already-completed tasks, we'll re-extract.

Usage:
    python format_for_harness.py --results featurebench_full_100.json --output output.jsonl
"""

import argparse
import json
from pathlib import Path


def format_results(results_path: str, dataset_path: str, output_path: str):
    """Convert EQUIPA results to FeatureBench harness format."""

    with open(results_path) as f:
        results = json.load(f)

    # Load dataset for any fields we need
    dataset = {}
    with open(dataset_path) as f:
        for line in f:
            item = json.loads(line)
            dataset[item["instance_id"]] = item

    predictions = []
    for r in results.get("results", []):
        iid = r["instance_id"]
        patch = r.get("patch", "")

        if not patch and r.get("resolved"):
            print(f"  WARNING: {iid} marked resolved but no patch saved!")
            continue

        predictions.append({
            "instance_id": iid,
            "model_patch": patch,
            "model_name_or_path": "EQUIPA (Opus 4.6 + Sonnet, dev-test loops, autoresearch)",
            "n_attempt": r.get("attempts", 1),
        })

    with open(output_path, "w") as f:
        for pred in predictions:
            f.write(json.dumps(pred) + "\n")

    print(f"Formatted {len(predictions)} predictions to {output_path}")
    resolved = sum(1 for r in results.get("results", []) if r.get("resolved"))
    print(f"Resolved: {resolved}/{len(results.get('results', []))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--dataset", default="featurebench_fast.jsonl")
    parser.add_argument("--output", default="output.jsonl")
    args = parser.parse_args()
    format_results(args.results, args.dataset, args.output)
