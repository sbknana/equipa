#!/usr/bin/env python3
"""Download FeatureBench dataset from HuggingFace."""
from datasets import load_dataset
import json

print("Loading FeatureBench dataset...")
ds = load_dataset("LiberCoders/FeatureBench", split="fast")
print(f"Loaded {len(ds)} instances")

with open("featurebench_fast.jsonl", "w") as f:
    for item in ds:
        f.write(json.dumps(dict(item)) + "\n")

sample = ds[0]
print(f"\nSample:")
print(f"  instance_id: {sample.get('instance_id', 'N/A')}")
print(f"  repo: {sample.get('repo', 'N/A')}")
print(f"  Keys: {list(sample.keys())[:15]}")
desc = sample.get("problem_statement", sample.get("description", sample.get("feature_description", "N/A")))
print(f"  Description (first 300 chars): {str(desc)[:300]}")
print(f"\nSaved to featurebench_fast.jsonl")
