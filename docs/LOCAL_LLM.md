# Local LLM Support

Run EQUIPA agents on local models via Ollama. No API key needed.

---

## Why Local LLMs?

- **No API costs.** Review and planning roles burn tokens fast. Local is free.
- **Privacy.** Your code never leaves your machine. Period.
- **Works offline.** No internet? No problem.
- **Good enough for read-only roles.** Planner, evaluator, code-reviewer, security-reviewer, researcher.. they read and analyze. They don't need frontier models.

Save your Claude tokens for the roles that actually write code.

---

## Prerequisites

- **Ollama** installed — [https://ollama.com](https://ollama.com)
- A model with **tool-calling support** (Qwen 3.5 recommended)
- **16GB+ RAM** for 27B models, **8GB** for 9B models

---

## Quick Start

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull qwen3.5:27b    # Full power (16GB RAM)
ollama pull qwen3.5:9b     # Lighter (8GB RAM)

# Optional: Pull the Forgeborn fine-tuned model (when available)
ollama pull forgeborn/equipa-qwen3.5-9b

# Verify it's there
ollama list
```

That's it. Ollama runs as a service automatically after install.

---

## Configuration

### Provider Settings in dispatch_config.json

The config file controls which provider each role uses. Mix and match freely.

```json
{
    "provider": "claude",
    "provider_planner": "ollama",
    "provider_evaluator": "ollama",
    "provider_code_reviewer": "ollama",
    "provider_researcher": "ollama",
    "ollama_base_url": "http://localhost:11434",
    "ollama_model": "qwen3.5:27b",
    "ollama_model_planner": "qwen3.5:27b"
}
```

### What Each Field Does

| Field | Purpose |
|-------|---------|
| `provider` | Global default. `claude` or `ollama`. Everything falls back to this. |
| `provider_{role}` | Per-role override. Set a specific role to use a different provider. |
| `ollama_base_url` | Where Ollama is running. Localhost by default, or point it at a remote machine. |
| `ollama_model` | Default model for all Ollama roles. |
| `ollama_model_{role}` | Per-role model override. Run your planner on 27B and reviewer on 9B if you want. |

The cascade: role-specific setting > global setting > hardcoded default.

### CLI Override

```bash
# Force ALL agents to use Ollama for this run
python3 forge_orchestrator.py --task 42 --dev-test --provider ollama -y

# Use whatever dispatch_config.json says (default behavior)
python3 forge_orchestrator.py --task 42 --dev-test -y
```

The `--provider` flag overrides everything in the config. Useful for testing.

---

## Recommended Role Mapping

| Role | Provider | Why |
|------|----------|-----|
| Developer | Claude | Writes code. Needs the best quality. |
| Tester | Claude | Writes tests. Quality matters here too. |
| Debugger | Claude | Complex multi-file reasoning. Don't cheap out. |
| Frontend Designer | Claude | Design quality is visible to users. |
| Planner | Ollama | Read-only analysis. Local models handle this fine. |
| Evaluator | Ollama | Read-only scoring against criteria. Straightforward. |
| Code Reviewer | Ollama | Read-only review. Pattern matching, not generation. |
| Security Reviewer | Ollama | Read-only audit. Checklist-driven work. |
| Researcher | Ollama | Read-only research. Summarization and analysis. |

**The rule is simple:** if it writes code, use Claude. If it reads and reports, use Ollama.

---

## How It Works

`ollama_agent.py` implements a tool-calling agent loop using Ollama's native OpenAI-compatible API.

- **Zero external dependencies.** Uses Python's built-in `urllib`. No pip install needed.
- **Read-only roles** get: `read_file`, `list_directory`, `search_files`, `grep`, `bash` (read-only)
- **Write roles** also get: `write_file`, `edit_file`, `bash_write`
- **All file operations are sandboxed** to the project directory. Can't escape it.

The agent loop is the same structure as the Claude agent — send a message, check for tool calls, execute them, feed results back. Just a different backend.

---

## Supported Models

Any Ollama model with tool-calling support works. Here's what we've tested:

| Model | RAM | Notes |
|-------|-----|-------|
| `qwen3.5:27b` | 16GB | Best quality. Recommended default. |
| `qwen3.5:9b` | 8GB | Good balance of speed and quality. |
| `qwen2.5-coder:32b` | 20GB | Code-specialized. Great for code review. |
| `deepseek-coder-v2:16b` | 10GB | Code-specialized, lighter weight. |

Bigger models = better results but slower. For read-only roles, even 9B models do solid work.

---

## Remote Ollama

Don't want to run models on your dev machine? Point at a different box.

### Via Config

```json
{
    "ollama_base_url": "http://YOUR_OLLAMA_HOST:11434"
}
```

### Via Environment Variable

```bash
export OLLAMA_BASE_URL=http://YOUR_OLLAMA_HOST:11434
```

The environment variable takes priority over the config file. Useful for per-session overrides.

Make sure the remote machine has Ollama running and the port is open. Default port is 11434.

---

## Troubleshooting

**"Cannot connect to Ollama"**
Ollama isn't running. Start it with `ollama serve`. On Linux it usually auto-starts as a systemd service. Check with `systemctl status ollama`.

**Model too slow**
Try a smaller model. Drop from 27B to 9B. The quality difference for read-only roles is smaller than you'd think.

**Tool calls not working**
Your model doesn't support tool calling. Stick to models that do: Qwen 3.5, Llama 3.x, Mistral. Older models won't work.

**Out of memory**
Your model is too big for your RAM. Use a smaller variant or a more aggressively quantized version (Q4 instead of Q8). Check available RAM with `free -h`.

**Agent hangs or loops**
Some models get stuck in reasoning loops. Set a timeout in your config or try a different model. Qwen 3.5 is the most reliable we've tested.

---

## What's Next

- **Forgeborn fine-tuned models** — QLoRA-trained on EQUIPA task data for better role performance
- **Automatic fallback** — If Ollama fails, fall back to Claude automatically
- **Performance benchmarks** — Track local vs. Claude quality per role

---

*Copyright 2026 Forgeborn. Vibe coded with Claude.*
