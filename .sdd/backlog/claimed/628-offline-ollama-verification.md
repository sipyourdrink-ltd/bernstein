# 628 — Offline Ollama Verification

**Role:** devops
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** none

## Problem

It is unverified whether Bernstein works fully offline with Ollama-backed CLI agents. Air-gapped environments represent a $100B+ enterprise market (defense, finance, healthcare). Without verified offline support, Bernstein cannot credibly target these customers.

## Design

Verify and document full offline operation with Ollama as the model backend. Test the complete flow: `bernstein init`, `bernstein run`, task decomposition, agent spawning, and task completion — all without internet access. Document which CLI agents support Ollama (Claude Code does not, but Codex CLI and others may). Create an Ollama-specific configuration guide covering: model selection for coding tasks, recommended hardware specs, performance expectations vs cloud models. Test with at least two Ollama models (e.g., Qwen 2.5 Coder, DeepSeek Coder). Document any features that degrade gracefully offline (e.g., MCP servers that require network). Add an integration test that runs with a mock Ollama server.

## Files to modify

- `docs/offline-setup.md` (new)
- `src/bernstein/adapters/ollama.py` (verify/create)
- `.sdd/config.toml` (offline configuration example)
- `tests/integration/test_offline.py` (new)

## Completion signal

- Full orchestration run completes with Ollama backend, no internet
- Documentation covers setup, model selection, and hardware recommendations
- Integration test passes with mock Ollama server
