#!/usr/bin/env python3
"""
EQUIPA QLoRA Training Script
=====================================
Fine-tunes Qwen3.5 models using Unsloth for EQUIPA agent improvement.

Usage:
    # Activate env first: source REDACTED_VENV/bin/activate
    python3 train_qlora.py --model qwen3.5-35b-a3b  # MoE (17.5GB VRAM)
    python3 train_qlora.py --model qwen3.5-27b       # Dense (22GB VRAM)

Best Practices Applied:
    - 1 epoch only (more = worse per Lightning AI study)
    - Alpha = 2x rank
    - rsLoRA for better scaling
    - All linear layers targeted
    - Cosine schedule + AdamW 8-bit
    - ChatML formatting for Qwen
"""

import json
import os
import sys
import argparse
from datetime import datetime

# Model name mapping
MODEL_MAP = {
    "qwen3.5-35b-a3b": "unsloth/Qwen3.5-35B-A3B",
    "qwen3.5-27b": "unsloth/Qwen3.5-27B",
    "qwen2.5-coder-7b": "unsloth/Qwen2.5-Coder-7B",
}

# VRAM-aware configs
VRAM_CONFIGS = {
    "qwen3.5-35b-a3b": {
        "r": 16,
        "max_seq_length": 2048,
        "per_device_train_batch_size": 2,
        "gradient_accumulation_steps": 8,
    },
    "qwen3.5-27b": {
        "r": 16,
        "max_seq_length": 2048,
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

TRAINING_DATA_DIR = "training_data"
OUTPUT_DIR = "lora_output"


def main():
    parser = argparse.ArgumentParser(description="QLoRA fine-tuning with Unsloth")
    parser.add_argument(
        "--model",
        default="qwen3.5-35b-a3b",
        choices=list(MODEL_MAP.keys()),
        help="Model to fine-tune",
    )
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--epochs", type=int, default=1, help="Number of epochs (1 recommended)")
    parser.add_argument("--train-file", default=None, help="Training JSONL file")
    parser.add_argument("--eval-file", default=None, help="Eval JSONL file")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--save-gguf", action="store_true", default=True, help="Save as GGUF for Ollama")
    parser.add_argument("--gguf-quant", default="q4_k_m", help="GGUF quantization method")
    parser.add_argument("--dry-run", action="store_true", help="Print config and exit")
    args = parser.parse_args()

    model_name = MODEL_MAP[args.model]
    vram_cfg = VRAM_CONFIGS[args.model]
    train_file = args.train_file or os.path.join(TRAINING_DATA_DIR, "train.jsonl")
    eval_file = args.eval_file or os.path.join(TRAINING_DATA_DIR, "eval.jsonl")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_output_dir = os.path.join(args.output_dir, f"{args.model}_{timestamp}")

    config = {
        "model_name": model_name,
        "max_seq_length": vram_cfg["max_seq_length"],
        "load_in_4bit": True,
        "lora_r": vram_cfg["r"],
        "lora_alpha": vram_cfg["r"] * 2,  # Alpha = 2x rank
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
        "fp16": False,
        "bf16": True,
        "logging_steps": 5,
        "save_strategy": "epoch",
        "seed": 42,
        "output_dir": run_output_dir,
        "train_file": train_file,
        "eval_file": eval_file,
    }

    print("=" * 60)
    print("EQUIPA QLoRA Training")
    print("=" * 60)
    print(f"\nModel: {model_name}")
    print(f"LoRA rank: {config['lora_r']}, alpha: {config['lora_alpha']}")
    print(f"Seq length: {config['max_seq_length']}")
    print(f"Batch: {config['per_device_train_batch_size']} x {config['gradient_accumulation_steps']} grad accum")
    print(f"Effective batch size: {config['per_device_train_batch_size'] * config['gradient_accumulation_steps']}")
    print(f"LR: {config['learning_rate']}, Epochs: {config['num_train_epochs']}")
    print(f"Train file: {train_file}")
    print(f"Output: {run_output_dir}")

    if args.dry_run:
        print("\n[DRY RUN] Config:")
        print(json.dumps(config, indent=2))
        return

    # Verify training data exists
    if not os.path.exists(train_file):
        print(f"\nERROR: Training file not found: {train_file}")
        print("Run prepare_training_data.py first!")
        sys.exit(1)

    # Count training examples
    with open(train_file) as f:
        train_count = sum(1 for _ in f)
    print(f"\nTraining examples: {train_count}")

    if train_count < 10:
        print("WARNING: Very few training examples. Consider gathering more data.")

    # ---- Import heavy deps only after config validation ----
    print("\nLoading Unsloth...")
    from unsloth import FastModel
    from unsloth.chat_templates import get_chat_template, train_on_responses_only
    from trl import SFTTrainer, SFTConfig
    from datasets import load_dataset

    # Step 1: Load model
    print(f"\nLoading {model_name} (4-bit)...")
    model, tokenizer = FastModel.from_pretrained(
        model_name,
        max_seq_length=config["max_seq_length"],
        load_in_4bit=True,
    )

    # Step 2: Apply LoRA
    print("Applying LoRA adapters...")
    model = FastModel.get_peft_model(
        model,
        r=config["lora_r"],
        lora_alpha=config["lora_alpha"],
        lora_dropout=config["lora_dropout"],
        target_modules=config["target_modules"],
        use_rslora=config["use_rslora"],
    )

    # Print trainable params
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Trainable parameters: {trainable:,} / {total:,} ({100*trainable/total:.2f}%)")

    # Step 3: Set up chat template
    tokenizer = get_chat_template(tokenizer, chat_template="chatml")

    def formatting_func(examples):
        texts = []
        for convos in examples["conversations"]:
            if isinstance(convos, str):
                convos = json.loads(convos)
            text = tokenizer.apply_chat_template(
                convos, tokenize=False, add_generation_prompt=False
            )
            texts.append(text)
        return {"text": texts}

    # Step 4: Load dataset
    print("\nLoading training data...")
    dataset = load_dataset("json", data_files={"train": train_file}, split="train")
    dataset = dataset.map(formatting_func, batched=True)
    print(f"  Train samples: {len(dataset)}")

    eval_dataset = None
    if os.path.exists(eval_file):
        eval_dataset = load_dataset("json", data_files={"eval": eval_file}, split="eval")
        eval_dataset = eval_dataset.map(formatting_func, batched=True)
        print(f"  Eval samples: {len(eval_dataset)}")

    # Step 5: Configure trainer
    print("\nConfiguring SFT trainer...")
    os.makedirs(run_output_dir, exist_ok=True)

    training_args = SFTConfig(
        output_dir=run_output_dir,
        per_device_train_batch_size=config["per_device_train_batch_size"],
        gradient_accumulation_steps=config["gradient_accumulation_steps"],
        num_train_epochs=config["num_train_epochs"],
        learning_rate=config["learning_rate"],
        lr_scheduler_type=config["lr_scheduler_type"],
        warmup_ratio=config["warmup_ratio"],
        optim=config["optim"],
        fp16=config["fp16"],
        bf16=config["bf16"],
        logging_steps=config["logging_steps"],
        save_strategy=config["save_strategy"],
        seed=config["seed"],
        max_seq_length=config["max_seq_length"],
        dataset_text_field="text",
        packing=True,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        eval_dataset=eval_dataset,
        args=training_args,
    )

    # Train on responses only (mask user/system tokens)
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )

    # Step 6: Train
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

    # Step 7: Save adapter
    adapter_path = os.path.join(run_output_dir, "adapter")
    print(f"\nSaving LoRA adapter to {adapter_path}...")
    model.save_pretrained(adapter_path)
    tokenizer.save_pretrained(adapter_path)

    # Step 8: Export GGUF for Ollama
    if args.save_gguf:
        gguf_path = os.path.join(run_output_dir, "gguf")
        print(f"\nExporting GGUF ({args.gguf_quant}) to {gguf_path}...")
        model.save_pretrained_gguf(gguf_path, tokenizer, quantization_method=args.gguf_quant)

        # Create Modelfile for Ollama
        modelfile_path = os.path.join(run_output_dir, "Modelfile")
        gguf_files = [f for f in os.listdir(gguf_path) if f.endswith(".gguf")]
        if gguf_files:
            gguf_file = gguf_files[0]
            with open(modelfile_path, "w") as f:
                f.write(f"FROM {os.path.join(gguf_path, gguf_file)}\n")
                f.write('TEMPLATE """{{ if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}{{ if .Prompt }}<|im_start|>user\n{{ .Prompt }}<|im_end|>\n{{ end }}<|im_start|>assistant\n{{ .Response }}<|im_end|>\n"""\n')
                f.write('PARAMETER stop "<|im_end|>"\n')
                f.write("PARAMETER temperature 0.1\n")
                f.write("PARAMETER top_p 0.95\n")
            print(f"  Modelfile: {modelfile_path}")
            print(f"\n  To deploy to Ollama:")
            print(f"    ollama create forgetuned-{args.model} -f {modelfile_path}")

    # Save training config
    config["training_loss"] = trainer_stats.training_loss
    config["runtime_seconds"] = trainer_stats.metrics["train_runtime"]
    config_path = os.path.join(run_output_dir, "training_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nConfig saved: {config_path}")

    print("\nAll done! Next steps:")
    print("  1. Test adapter: model.generate() on sample prompts")
    print("  2. Deploy to Ollama: ollama create forgetuned-{model} -f Modelfile")
    print("  3. Update EQUIPA dispatch_config.json with new model name")


if __name__ == "__main__":
    main()
