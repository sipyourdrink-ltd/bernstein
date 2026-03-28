# 720 — Air-Gapped Mode with Ollama/Local Models

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

60% of AI failures will stem from governance gaps (IDC prediction). Enterprise security teams require agent orchestration that runs entirely within their network. Running Bernstein with Ollama or llama.cpp means zero data leaves the building. No competitor in the CLI orchestrator space supports this well.

## Design

### Local model support
- Detect Ollama installation (`ollama list`)
- Map Bernstein model configs to Ollama model names
- Support `--local` flag: `bernstein -g "task" --local`
- Automatic fallback cascade: local first, cloud if local fails

### Configuration
```yaml
# bernstein.yaml
providers:
  - type: ollama
    url: http://localhost:11434
    models: [qwen2.5-coder, deepseek-coder-v2]
  - type: anthropic  # fallback
```

## Files to modify

- `src/bernstein/adapters/ollama.py` (new)
- `src/bernstein/core/router.py` (add local routing)
- `tests/unit/test_adapter_ollama.py` (new)

## Completion signal

- `bernstein -g "task" --local` works with Ollama
- Zero external API calls in air-gapped mode
