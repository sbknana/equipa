#!/usr/bin/env python3
"""
EQUIPA QLoRA Training Script (PEFT + TRL)
=============================================
Fine-tunes Qwen models using QLoRA via HuggingFace PEFT + TRL.
No Unsloth dependency — works on any CUDA GPU including Pascal (sm_61).

Usage:
    source REDACTED_VENV/bin/activate
    TORCHDYNAMO_DISABLE=1 python3 train_qlora_peft.py --model qwen3.5-9b
    TORCHDYNAMO_DISABLE=1 python3 train_qlora_peft.py --model qwen2.5-coder-7b

Best Practices:
    - 1 epoch only (more = worse per Lightning AI study)
    - Alpha = 2x rank
    - rsLoRA for better scaling
    - All linear layers targeted
    - Cosine schedule + AdamW 8-bit
    - ChatML formatting for Qwen

Copyright 2026 Forgeborn. All rights reserved.
"""

import json
import os
import sys
import argparse
from datetime import datetime

import torch

# Better memory management for constrained GPUs
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

MODEL_MAP = {
    "qwen3.5-9b": "Qwen/Qwen3.5-9B",
    "qwen3.5-4b": "Qwen/Qwen3.5-4B",
    "qwen2.5-coder-7b": "Qwen/Qwen2.5-Coder-7B-Instruct",
}

# VRAM-aware configs (QLoRA 4-bit)
# qwen3.5-9b: ~6.5GB, qwen3.5-4b: ~3.5GB, qwen2.5-coder-7b: ~5GB
VRAM_CONFIGS = {
    "qwen3.5-9b": {
        "r": 16,
        "max_seq_length": 2048,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 16,
    },
    "qwen3.5-4b": {
        "r": 32,
        "max_seq_length": 1024,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 16,
    },
    "qwen2.5-coder-7b": {
        "r": 32,
        "max_seq_length": 2048,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 16,
    },
}

TRAINING_DATA_DIR = "REDACTED_TRAINING_DIR"
OUTPUT_DIR = "REDACTED_TRAINING_DIR/output"


def main():
    parser = argparse.ArgumentParser(description="QLoRA fine-tuning with PEFT + TRL")
    parser.add_argument(
        "--model",
        default="qwen3.5-9b",
        choices=list(MODEL_MAP.keys()),
        help="Model to fine-tune",
    )
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--epochs", type=int, default=1, help="Number of epochs (1 recommended)")
    parser.add_argument("--train-file", default=None, help="Training JSONL file")
    parser.add_argument("--eval-file", default=None, help="Eval JSONL file")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--no-gguf", action="store_true", help="Skip GGUF export")
    parser.add_argument("--gguf-quant", default="q4_k_m", help="GGUF quantization method")
    parser.add_argument("--dry-run", action="store_true", help="Print config and exit")
    args = parser.parse_args()

    model_id = MODEL_MAP[args.model]
    vram_cfg = VRAM_CONFIGS[args.model]
    train_file = args.train_file or os.path.join(TRAINING_DATA_DIR, "train.jsonl")
    eval_file = args.eval_file or os.path.join(TRAINING_DATA_DIR, "eval.jsonl")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_output_dir = os.path.join(args.output_dir, f"{args.model}_{timestamp}")

    config = {
        "model_id": model_id,
        "model_key": args.model,
        "max_seq_length": vram_cfg["max_seq_length"],
        "load_in_4bit": True,
        "lora_r": vram_cfg["r"],
        "lora_alpha": vram_cfg["r"] * 2,
        "lora_dropout": 0,
        "use_rslora": True,
        "target_modules": [
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        "learning_rate": args.lr,
        "num_train_epochs": args.epochs,
        "per_device_train_batch_size": vram_cfg["per_device_train_batch_size"],
        "gradient_accumulation_steps": vram_cfg["gradient_accumulation_steps"],
        "warmup_ratio": 0.05,
        "lr_scheduler_type": "cosine",
        "optim": "adamw_8bit",
        "logging_steps": 5,
        "save_strategy": "epoch",
        "seed": 42,
        "output_dir": run_output_dir,
        "train_file": train_file,
        "eval_file": eval_file,
    }

    print("=" * 60)
    print("EQUIPA QLoRA Training (PEFT + TRL)")
    print("=" * 60)
    print(f"\nModel: {model_id}")
    print(f"LoRA rank: {config['lora_r']}, alpha: {config['lora_alpha']}")
    print(f"Seq length: {config['max_seq_length']}")
    print(f"Batch: {config['per_device_train_batch_size']} x {config['gradient_accumulation_steps']} grad accum")
    print(f"Effective batch size: {config['per_device_train_batch_size'] * config['gradient_accumulation_steps']}")
    print(f"LR: {config['learning_rate']}, Epochs: {config['num_train_epochs']}")
    print(f"Train file: {train_file}")
    print(f"Output: {run_output_dir}")
    print(f"GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB" if torch.cuda.is_available() else "")

    if args.dry_run:
        print("\n[DRY RUN] Config:")
        print(json.dumps(config, indent=2))
        return

    # Verify training data exists
    if not os.path.exists(train_file):
        print(f"\nERROR: Training file not found: {train_file}")
        sys.exit(1)

    # Count training examples
    with open(train_file) as f:
        train_count = sum(1 for _ in f)
    print(f"\nTraining examples: {train_count}")

    if train_count < 10:
        print("WARNING: Very few training examples (<10). Results may be poor.")

    # ---- Import heavy deps after config validation ----
    print("\nLoading libraries...")
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
    )
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from trl import SFTTrainer, SFTConfig
    from datasets import load_dataset

    # Step 1: Load model in 4-bit
    print(f"\nLoading {model_id} (QLoRA 4-bit)...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )

    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
        device_map="auto",
        dtype=torch.float16,
        trust_remote_code=True,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        trust_remote_code=True,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Step 2: Apply LoRA (skip prepare_model_for_kbit_training to save VRAM)
    print("Applying LoRA adapters...")
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()

    lora_config = LoraConfig(
        r=config["lora_r"],
        lora_alpha=config["lora_alpha"],
        lora_dropout=config["lora_dropout"],
        target_modules=config["target_modules"],
        bias="none",
        task_type="CAUSAL_LM",
        use_rslora=config["use_rslora"],
    )

    model = get_peft_model(model, lora_config)

    # Fix: Cast any bf16 params to fp16 (Qwen3.5 stores weights in bf16,
    # but 1080 Ti has no bf16 hardware and fp16 AMP GradScaler chokes on bf16)
    bf16_count = 0
    for param in model.parameters():
        if param.dtype == torch.bfloat16:
            param.data = param.data.to(torch.float16)
            bf16_count += 1
    if bf16_count:
        print(f"  Cast {bf16_count} bf16 parameters to fp16 (Pascal GPU compat)")

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Trainable parameters: {trainable:,} / {total:,} ({100*trainable/total:.2f}%)")

    # Step 3: Load and format dataset
    print("\nLoading training data...")

    def formatting_func(example):
        """Format conversations to ChatML."""
        convos = example["conversations"]
        if isinstance(convos, str):
            convos = json.loads(convos)
        text = tokenizer.apply_chat_template(
            convos, tokenize=False, add_generation_prompt=False
        )
        return {"text": text}

    dataset = load_dataset("json", data_files={"train": train_file}, split="train")
    dataset = dataset.map(formatting_func)
    print(f"  Train samples: {len(dataset)}")

    eval_dataset = None
    if os.path.exists(eval_file):
        eval_dataset = load_dataset("json", data_files={"eval": eval_file}, split="eval")
        eval_dataset = eval_dataset.map(formatting_func)
        print(f"  Eval samples: {len(eval_dataset)}")

    # Step 4: Configure trainer
    print("\nConfiguring SFT trainer...")
    os.makedirs(run_output_dir, exist_ok=True)

    sft_kwargs = {
        "output_dir": run_output_dir,
        "per_device_train_batch_size": config["per_device_train_batch_size"],
        "gradient_accumulation_steps": config["gradient_accumulation_steps"],
        "num_train_epochs": config["num_train_epochs"],
        "learning_rate": config["learning_rate"],
        "lr_scheduler_type": config["lr_scheduler_type"],
        "warmup_steps": max(1, int(train_count * config["warmup_ratio"] / config["per_device_train_batch_size"] / config["gradient_accumulation_steps"])),
        "optim": config["optim"],
        "fp16": False,  # Disabled: Qwen3.5 GatedDeltaNet produces bf16 gradients internally,
        "bf16": False,  # which crashes the fp16 GradScaler. 4-bit quant handles precision.
        "logging_steps": config["logging_steps"],
        "save_strategy": config["save_strategy"],
        "seed": config["seed"],
        "dataset_text_field": "text",
        "packing": False,  # Requires flash attention to avoid cross-contamination
        "report_to": "none",
    }

    # Handle API differences between TRL versions
    try:
        training_args = SFTConfig(max_seq_length=config["max_seq_length"], **sft_kwargs)
    except TypeError:
        training_args = SFTConfig(**sft_kwargs)

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        eval_dataset=eval_dataset,
        args=training_args,
    )

    # Step 5: Train
    print("\n" + "=" * 60)
    print("STARTING TRAINING")
    print("=" * 60)

    trainer_stats = trainer.train()

    print("\n" + "=" * 60)
    print("TRAINING COMPLETE")
    print("=" * 60)
    print(f"  Train loss: {trainer_stats.training_loss:.4f}")
    print(f"  Train runtime: {trainer_stats.metrics['train_runtime']:.1f}s")
    print(f"  Samples/second: {trainer_stats.metrics['train_samples_per_second']:.2f}")

    # Step 6: Save adapter
    adapter_path = os.path.join(run_output_dir, "adapter")
    print(f"\nSaving LoRA adapter to {adapter_path}...")
    model.save_pretrained(adapter_path)
    tokenizer.save_pretrained(adapter_path)

    # Step 7: Save training config + results
    config["training_loss"] = trainer_stats.training_loss
    config["runtime_seconds"] = trainer_stats.metrics["train_runtime"]
    config["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    config_path = os.path.join(run_output_dir, "training_config.json")
    with open(config_path, "w") as f:
        json.dump(config, indent=2, fp=f)
    print(f"Config saved: {config_path}")

    # Step 8: VRAM report
    if torch.cuda.is_available():
        peak_mem = torch.cuda.max_memory_allocated() / 1024**3
        print(f"\nPeak GPU memory: {peak_mem:.2f} GB")

    print("\nAll done! Next steps:")
    print(f"  1. Test: load adapter from {adapter_path}")
    print(f"  2. Merge: merge adapter with base model for deployment")
    print(f"  3. Export GGUF: use llama.cpp or AutoGGUF for Ollama deployment")
    print(f"  4. Update EQUIPA dispatch_config.json with new model")


if __name__ == "__main__":
    main()
