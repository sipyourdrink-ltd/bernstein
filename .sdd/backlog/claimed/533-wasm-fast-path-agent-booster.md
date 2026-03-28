# 533 — WASM fast-path for simple tasks (Agent Booster pattern)

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large

## Problem

Every task currently goes through an LLM agent, even trivial ones (formatting,
imports, renames, config tweaks). Ruflo's "Agent Booster" uses WebAssembly to
handle simple code transforms 352x faster at zero LLM cost, claiming 85% API
cost reduction and 2.5x usage extension. This is a massive competitive gap.

## Design

### Fast-path task classification
Before spawning an LLM agent, classify task complexity:
- **L0 (Trivial)**: formatting, import sorting, rename, config value change
  -> Execute via deterministic script/WASM, no LLM needed
- **L1 (Simple)**: add a test, update docstring, fix lint error
  -> Use cheapest model (Haiku/free tier)
- **L2+ (Complex)**: feature work, refactoring, architecture
  -> Full LLM agent (current behavior)

### Implementation options
1. **Python-native fast-path** (pragmatic, ship first):
   - AST transforms via `libcst` for Python refactors
   - `ruff --fix` for lint/format tasks
   - Template-based for config changes
   - Regex/AST for renames across codebase

2. **WASM sandbox** (ambitious, ship later):
   - Compile transform scripts to WASM via `wasmtime-py`
   - Sandboxed execution (can't break things)
   - Community can contribute transform plugins

### Task classifier
- Rule-based first: regex on task title/description
  - "format" / "lint" / "sort imports" -> L0
  - "rename X to Y" -> L0
  - "add test for" -> L1
- Later: lightweight LLM classifier (Haiku, <100 tokens)

### Metrics
- Track: tasks_bypassed_llm, cost_saved_usd, time_saved_seconds
- Dashboard: "Saved $X.XX by fast-pathing Y tasks this session"

## Files to modify
- `src/bernstein/core/orchestrator.py` — fast-path check before spawn
- New: `src/bernstein/core/fast_path.py` — task classifier + deterministic executors
- `src/bernstein/core/router.py` — L0 routing
- `.sdd/config/routing.yaml` — fast-path rules

## Completion signal
- Formatting/lint tasks execute in <1s with no LLM call
- Cost savings visible in `bernstein cost`
- At least 30% of trivial tasks fast-pathed in a typical evolve session
