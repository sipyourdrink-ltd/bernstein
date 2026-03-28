# 393 — Execution Recording and Replay

**Role:** backend
**Priority:** 2 (high)
**Scope:** large
**Depends on:** none

## Problem

There is no way to record, inspect, or replay a Bernstein orchestration run. Without execution recording, debugging failed runs requires reading scattered log files. Deterministic execution is Bernstein's core brand differentiator, but there is no mechanism to prove or leverage it.

## Design

Implement execution recording that captures the full provenance of every run: task assignments, agent spawns, model calls (prompts and responses), tool invocations, file changes, and timing data. Store recordings in `.sdd/runs/{run_id}/` as structured JSONL files. Generate a cryptographic run fingerprint (SHA-256 of the recording) for integrity verification. Build a replay mode (`bernstein replay {run_id}`) that re-executes the recorded steps, optionally with a different model or configuration. Replay mode enables A/B testing and debugging. Recording should be zero-cost when disabled and low-overhead (<5% latency) when enabled. Default to enabled.

## Files to modify

- `src/bernstein/core/recorder.py` (new)
- `src/bernstein/core/replayer.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `src/bernstein/cli/replay.py` (new)
- `tests/unit/test_recorder.py` (new)

## Completion signal

- `bernstein run` produces a recording in `.sdd/runs/{run_id}/`
- `bernstein replay {run_id}` replays the recording
- Run fingerprint generated and verifiable
