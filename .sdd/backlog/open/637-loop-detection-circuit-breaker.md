# 637 — Loop Detection Circuit Breaker

**Role:** backend
**Priority:** 4 (low)
**Scope:** medium
**Depends on:** #601

## Problem

Agents can enter infinite loops — retrying failed operations, generating the same code repeatedly, or oscillating between two approaches. Reflexion-style loops can consume 50x the normal token count. There is no detection or prevention mechanism.

## Design

Implement loop detection and runaway agent prevention with automatic circuit-breaking. Monitor agent behavior for loop indicators: repeated identical tool calls, oscillating file changes (edit-revert-edit cycles), repeated identical error messages, and token consumption rate spikes. Use a sliding window approach: if the last N actions match a loop pattern, trigger the circuit breaker. Circuit breaker actions: warn (log and continue), throttle (add delay between actions), pause (stop agent and notify), or kill (terminate agent and mark task failed). Configure thresholds in `.sdd/config.toml` under `[circuit_breaker]`: max retries, max token rate, loop detection window size. Integrate with cost tracking — a cost spike is itself a loop indicator.

## Files to modify

- `src/bernstein/core/circuit_breaker.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `.sdd/config.toml`
- `tests/unit/test_circuit_breaker.py` (new)

## Completion signal

- Repeated identical actions detected and agent stopped
- Token consumption rate spike triggers throttling
- Circuit breaker thresholds configurable
