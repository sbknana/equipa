#!/usr/bin/env python3
"""Check SWE-bench setup status and report disk usage."""

import json
import subprocess
import os
from pathlib import Path

def check_dataset():
    """Check if dataset is downloaded."""
    dataset_path = Path("/srv/forge-share/AI_Stuff/Equipa/benchmarks/swebench_verified_full.jsonl")
    if dataset_path.exists():
        size_mb = dataset_path.stat().st_size / (1024 * 1024)
        with open(dataset_path) as f:
            lines = sum(1 for _ in f)
        print(f"✓ Dataset downloaded: {dataset_path}")
        print(f"  - Size: {size_mb:.1f} MB")
        print(f"  - Instances: {lines}")
        return True
    else:
        print(f"✗ Dataset not found: {dataset_path}")
        return False

def check_swebench_installed():
    """Check if swebench is installed."""
    try:
        result = subprocess.run(['pip', 'show', 'swebench'],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            version = None
            for line in result.stdout.splitlines():
                if line.startswith('Version:'):
                    version = line.split(':', 1)[1].strip()
            print(f"✓ swebench installed (version: {version or 'unknown'})")
            return True
        else:
            print("✗ swebench not installed")
            return False
    except Exception as e:
        print(f"✗ Error checking swebench: {e}")
        return False

def check_docker_images():
    """Check pulled Docker images."""
    try:
        result = subprocess.run(['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            all_images = result.stdout.strip().split('\n')
            swebench_images = [img for img in all_images if 'sweb.' in img]

            print(f"✓ Docker accessible")
            print(f"  - Total SWE-bench images: {len(swebench_images)}")

            if swebench_images:
                print(f"  - Images:")
                for img in sorted(swebench_images)[:10]:
                    print(f"    • {img}")
                if len(swebench_images) > 10:
                    print(f"    ... and {len(swebench_images) - 10} more")

            return len(swebench_images) > 0
        else:
            print("✗ Docker not accessible or not running")
            return False
    except Exception as e:
        print(f"✗ Error checking Docker: {e}")
        return False

def check_disk_usage():
    """Check disk usage in relevant locations."""
    print("\nDisk usage:")

    # Check benchmarks directory
    try:
        result = subprocess.run(['du', '-sh', '/srv/forge-share/AI_Stuff/Equipa/benchmarks'],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            print(f"  - Benchmarks directory: {result.stdout.strip().split()[0]}")
    except Exception:
        pass

    # Check docker (if accessible)
    try:
        result = subprocess.run(['docker', 'system', 'df'],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            print("  - Docker storage:")
            for line in result.stdout.splitlines()[1:5]:
                print(f"    {line}")
    except Exception:
        pass

def main():
    """Run all checks."""
    print("=" * 60)
    print("SWE-bench Verified Setup Status")
    print("=" * 60)
    print()

    checks = [
        ("SWE-bench installed", check_swebench_installed()),
        ("Dataset downloaded", check_dataset()),
        ("Docker images pulled", check_docker_images()),
    ]

    check_disk_usage()

    print()
    print("=" * 60)
    print("Summary:")
    all_passed = all(result for _, result in checks)
    for name, result in checks:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {name}")

    print("=" * 60)

    if all_passed:
        print("\n✓ All checks passed! SWE-bench Verified is ready.")
        return 0
    else:
        print("\n⚠ Some checks failed. Review output above.")
        return 1

if __name__ == "__main__":
    exit(main())
