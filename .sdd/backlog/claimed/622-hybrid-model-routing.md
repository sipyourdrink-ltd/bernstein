# 622 — Hybrid Model Routing

**Role:** backend
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** none

## Problem

All tasks are routed to cloud models regardless of complexity. Simple tasks (formatting, renaming, boilerplate) that a local model could handle still consume expensive cloud API credits. Ollama has 52M monthly downloads, showing massive demand for local model usage. Hybrid routing could cut costs 70-80% for many workloads.

## Design

Implement intelligent model routing that sends simple tasks to local models (Ollama) and complex tasks to cloud providers. Build a task complexity classifier based on: task description keywords, file count, estimated lines of change, and historical success rates. Define routing tiers: tier 1 (local — formatting, simple refactoring, boilerplate), tier 2 (cloud-cheap — bug fixes, test writing, small features), tier 3 (cloud-premium — architecture, complex features, security). Configuration in `.sdd/config.toml` under `[routing]` with overridable defaults. Add an Ollama adapter to the adapter system. Support fallback: if local model fails, automatically escalate to cloud. Track success rates per model per task type to improve routing over time.

## Files to modify

- `src/bernstein/core/model_router.py` (new)
- `src/bernstein/core/task_classifier.py` (new)
- `src/bernstein/adapters/ollama.py` (new)
- `src/bernstein/core/orchestrator.py`
- `.sdd/config.toml`
- `tests/unit/test_model_router.py` (new)

## Completion signal

- Simple tasks route to Ollama, complex tasks route to cloud
- Fallback from local to cloud works on failure
- Cost savings measurable (target: 50%+ reduction on mixed workloads)
