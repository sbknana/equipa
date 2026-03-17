#!/usr/bin/env python3
"""
ForgeSmith Training Data Preparation
=====================================
Downloads, filters, and formats coding datasets from HuggingFace
into ChatML JSONL format for QLoRA fine-tuning.

Sources:
    1. m-a-p/Code-Feedback — multi-turn code conversations
    2. m-a-p/CodeFeedback-Filtered-Instruction — instruction/response pairs
    3. google-research-datasets/mbpp + openai/openai_humaneval — verified Python
    4. sahil2801/CodeAlpaca-20k + nickrosh/Evol-Instruct-Code-80k-v1 — diverse tasks

Usage:
    python prepare_training_data.py --max-total 100000
"""

import json
import os
import argparse
import hashlib
import random

from datasets import load_dataset


def make_conv(system, user, assistant):
    """Create a ChatML conversation."""
    msgs = []
    if system and system.strip():
        msgs.append({"role": "system", "content": system.strip()})
    msgs.append({"role": "user", "content": user.strip()})
    msgs.append({"role": "assistant", "content": assistant.strip()})
    return msgs


def chash(text):
    """Content hash for deduplication."""
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def is_quality(text, min_len=50, max_len=15000):
    """Basic quality filter."""
    if not text or not text.strip():
        return False
    t = text.strip()
    if len(t) < min_len or len(t) > max_len:
        return False
    if sum(1 for c in t if ord(c) < 128) / max(len(t), 1) < 0.7:
        return False
    return True


SYS_CODE = (
    "You are an expert software engineer. Write clean, efficient, "
    "well-documented code. Explain your reasoning when asked."
)
SYS_GO = (
    "You are an expert Go developer. Write idiomatic, well-tested "
    "Go code following Go best practices."
)
SYS_PY = (
    "You are an expert Python developer. Write clean, Pythonic code "
    "with proper type hints and documentation."
)
SYS_DBG = (
    "You are an expert debugger. Analyze code, identify issues, "
    "and provide clear fixes with explanations."
)


def detect_sys(text):
    """Detect appropriate system prompt from content."""
    lo = text.lower()
    if "golang" in lo or "package main" in lo or "func(" in lo:
        return SYS_GO
    elif "python" in lo or "def " in lo or "import " in lo:
        return SYS_PY
    elif "debug" in lo or "traceback" in lo:
        return SYS_DBG
    return SYS_CODE


def proc_code_feedback(mx=30000):
    """m-a-p/Code-Feedback: multi-turn code conversations."""
    print(f"\n[1/4] Code-Feedback (target: {mx})...")
    try:
        ds = load_dataset(
            "m-a-p/Code-Feedback", split="train", trust_remote_code=True
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

    res, seen = [], set()
    sk = {"q": 0, "d": 0, "f": 0}

    for i, row in enumerate(ds):
        if len(res) >= mx:
            break
        msgs = row.get("messages", [])
        if not msgs or len(msgs) < 2:
            sk["f"] += 1
            continue

        turns, ok = [], True
        for m in msgs:
            r = m.get("role", "")
            c = m.get("content", "")
            if r not in ("user", "assistant", "system"):
                ok = False
                break
            if not is_quality(c, 20, 15000):
                ok = False
                break
            turns.append({"role": r, "content": c.strip()})

        if not ok:
            sk["q"] += 1
            continue

        fu = next((t["content"] for t in turns if t["role"] == "user"), "")
        h = chash(fu)
        if h in seen:
            sk["d"] += 1
            continue
        seen.add(h)

        if not turns or turns[0]["role"] != "system":
            turns.insert(0, {"role": "system", "content": SYS_CODE})

        res.append({"conversations": turns})
        if len(res) % 5000 == 0:
            print(f"  ...kept {len(res)}")

    print(f"  Result: {len(res)} (skipped: {sk})")
    return res


def proc_code_instr(mx=30000):
    """m-a-p/CodeFeedback-Filtered-Instruction: instruction/response pairs."""
    print(f"\n[2/4] CodeFeedback-Filtered-Instruction (target: {mx})...")
    try:
        ds = load_dataset(
            "m-a-p/CodeFeedback-Filtered-Instruction",
            split="train",
            trust_remote_code=True,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        return []

    res, seen = [], set()
    sk = {"q": 0, "d": 0}

    for i, row in enumerate(ds):
        if len(res) >= mx:
            break
        q = row.get("query", "") or row.get("instruction", "") or ""
        a = row.get("answer", "") or row.get("response", "") or ""
        if not is_quality(q, 20, 5000) or not is_quality(a, 50, 15000):
            sk["q"] += 1
            continue
        h = chash(q)
        if h in seen:
            sk["d"] += 1
            continue
        seen.add(h)
        res.append({"conversations": make_conv(detect_sys(q + a), q, a)})
        if len(res) % 5000 == 0:
            print(f"  ...kept {len(res)}")

    print(f"  Result: {len(res)} (skipped: {sk})")
    return res


def proc_python(mx=20000):
    """MBPP + HumanEval: verified Python solutions."""
    print(f"\n[3/4] Python datasets (target: {mx})...")
    res, seen = [], set()
    sk = {"q": 0, "d": 0}

    # MBPP
    try:
        ds = load_dataset(
            "google-research-datasets/mbpp",
            "full",
            split="train",
            trust_remote_code=True,
        )
        for row in ds:
            if len(res) >= mx // 2:
                break
            p = row.get("text", "") or ""
            c = row.get("code", "") or ""
            tl = row.get("test_list", [])
            if not is_quality(p, 10, 2000) or not is_quality(c, 20, 5000):
                sk["q"] += 1
                continue
            h = chash(p)
            if h in seen:
                sk["d"] += 1
                continue
            seen.add(h)
            resp = c.strip()
            if tl:
                resp += "\n\n# Tests:\n" + "\n".join(tl[:3])
            res.append({"conversations": make_conv(SYS_PY, p, resp)})
    except Exception as e:
        print(f"  ERROR mbpp: {e}")

    # HumanEval
    try:
        ds = load_dataset(
            "openai/openai_humaneval", split="test", trust_remote_code=True
        )
        for row in ds:
            if len(res) >= mx:
                break
            p = row.get("prompt", "")
            s = row.get("canonical_solution", "")
            if not p.strip() or not s.strip():
                continue
            h = chash(p)
            if h in seen:
                continue
            seen.add(h)
            um = f"Complete this Python function:\n\n```python\n{p.strip()}\n```"
            resp = f"```python\n{p.strip()}{s.strip()}\n```"
            res.append({"conversations": make_conv(SYS_PY, um, resp)})
    except Exception as e:
        print(f"  ERROR humaneval: {e}")

    print(f"  Result: {len(res)} (skipped: {sk})")
    return res


def proc_swe(mx=20000):
    """CodeAlpaca + Evol-Instruct-Code: diverse coding tasks."""
    print(f"\n[4/4] SWE/instruction datasets (target: {mx})...")
    res, seen = [], set()
    sk = {"q": 0, "d": 0}

    # CodeAlpaca
    try:
        ds = load_dataset(
            "sahil2801/CodeAlpaca-20k", split="train", trust_remote_code=True
        )
        for row in ds:
            if len(res) >= mx // 2:
                break
            instr = row.get("instruction", "") or ""
            inp = row.get("input", "") or ""
            out = row.get("output", "") or ""
            um = instr.strip()
            if inp.strip():
                um += "\n\n" + inp.strip()
            if not is_quality(um, 20, 5000) or not is_quality(out, 30, 10000):
                sk["q"] += 1
                continue
            h = chash(um)
            if h in seen:
                sk["d"] += 1
                continue
            seen.add(h)
            res.append({"conversations": make_conv(detect_sys(um + out), um, out)})
    except Exception as e:
        print(f"  ERROR CodeAlpaca: {e}")

    # Evol-Instruct-Code
    try:
        ds = load_dataset(
            "nickrosh/Evol-Instruct-Code-80k-v1",
            split="train",
            trust_remote_code=True,
        )
        for row in ds:
            if len(res) >= mx:
                break
            instr = row.get("instruction", "") or ""
            out = row.get("output", "") or ""
            if not is_quality(instr, 20, 5000) or not is_quality(out, 50, 15000):
                sk["q"] += 1
                continue
            h = chash(instr)
            if h in seen:
                sk["d"] += 1
                continue
            seen.add(h)
            res.append({"conversations": make_conv(SYS_CODE, instr, out)})
            if len(res) % 5000 == 0:
                print(f"  ...kept {len(res)}")
    except Exception as e:
        print(f"  ERROR Evol-Instruct: {e}")

    print(f"  Result: {len(res)} (skipped: {sk})")
    return res


def main():
    pa = argparse.ArgumentParser(description="Prepare ForgeSmith training data")
    pa.add_argument("--output-dir", default=os.environ.get("FORGESMITH_DATA_DIR", "forgesmith_output"))
    pa.add_argument("--max-total", type=int, default=100000)
    pa.add_argument("--eval-ratio", type=float, default=0.05)
    pa.add_argument("--seed", type=int, default=42)
    pa.add_argument("--include-existing", default=None)
    args = pa.parse_args()

    random.seed(args.seed)
    os.makedirs(args.output_dir, exist_ok=True)

    b = args.max_total
    alloc = {
        "cf": int(b * 0.30),
        "ci": int(b * 0.30),
        "py": int(b * 0.20),
        "swe": int(b * 0.20),
    }

    print("=" * 60)
    print("ForgeSmith Training Data Preparation")
    print("=" * 60)
    print(f"\nTarget: {b} | Output: {args.output_dir}")
    print(f"Budget: {alloc}")

    all_ex = []
    all_ex.extend(proc_code_feedback(alloc["cf"]))
    all_ex.extend(proc_code_instr(alloc["ci"]))
    all_ex.extend(proc_python(alloc["py"]))
    all_ex.extend(proc_swe(alloc["swe"]))

    if args.include_existing and os.path.exists(args.include_existing):
        print(f"\nMerging {args.include_existing}...")
        with open(args.include_existing) as f:
            ex = [json.loads(line) for line in f if line.strip()]
        all_ex.extend(ex)
        print(f"  Added {len(ex)} existing")

    # Global dedup
    print("\nDeduplicating...")
    sh, dd = set(), []
    for e in all_ex:
        fu = next(
            (m["content"] for m in e.get("conversations", []) if m["role"] == "user"),
            "",
        )
        h = chash(fu)
        if h not in sh:
            sh.add(h)
            dd.append(e)
    print(f"  {len(all_ex)} -> {len(dd)} (removed {len(all_ex) - len(dd)})")
    all_ex = dd

    random.shuffle(all_ex)

    # Split
    ec = max(50, int(len(all_ex) * args.eval_ratio))
    ev, tr = all_ex[:ec], all_ex[ec:]
    print(f"\nTrain: {len(tr)} | Eval: {len(ev)}")

    tp = os.path.join(args.output_dir, "train.jsonl")
    ep = os.path.join(args.output_dir, "eval.jsonl")

    # Backup existing
    for p in [tp, ep]:
        if os.path.exists(p):
            os.rename(p, p + ".bak")
            print(f"  Backed up {p}")

    with open(tp, "w") as f:
        for e in tr:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(ep, "w") as f:
        for e in ev:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    with open(os.path.join(args.output_dir, "dataset_stats.json"), "w") as f:
        json.dump(
            {"train": len(tr), "eval": len(ev), "total": len(all_ex)}, f, indent=2
        )

    print(f"\n{'=' * 60}")
    print(f"DONE: {len(tr)} train + {len(ev)} eval")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

