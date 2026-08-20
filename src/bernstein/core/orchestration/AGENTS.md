# Orchestration engine

Deterministic tick loop: watch tasks, spawn agents, verify completion,
repeat. Coordination is plain Python, never an LLM (ADR-006).

## Key files

| File | Purpose |
|---|---|
| `orchestrator.py` | Public facade; single-threaded `while self._running` tick loop |
| `tick_pipeline.py` | Pure helpers: task fetching, batching, server interaction |
| `run_stall.py` | Pure decision function for zero-terminal run quiescence |
| `deterministic.py` | Seeded-run LLM record/replay via `.sdd/runs/` journals |
| `escalation.py` | Journal-anchored, Ed25519-signed stall escalation receipts |
| `trigger_manager.py` | Evaluates `triggers.yaml` rules into `TriggerEvent`s (event and cron sources) |
| `worker.py` | `bernstein-worker` process wrapper for spawned CLI agents |
| `run_closure_owner.py` | Universal authenticated run closure owner |

Heavy lifting lives in siblings: `../tasks/task_lifecycle.py` (claim, spawn, retry,
completion) and `../agents/` (spawner, heartbeat, crash detection, reaping).

## Invariants

- No LLM in any coordination path (`docs/decisions/006-no-embedded-llm.md`).
- The tick loop is single-threaded by design; do not add concurrent
  ticks without restoring a tick guard (`orchestrator.py` docstring).
- Replay is strict: a cache miss raises `ReplayMissError`, not a silent live call.
- Prefer pure decision functions (`run_stall.py` is the model) so a
  criterion is testable without a tick loop, server, or real clock.
- Optional third-party imports degrade to a no-op instead of raising;
  `trigger_manager.py` yields no events when `croniter` is absent.
- Operator config is validated per item: a bad entry is logged and skipped, never fatal
  (`croniter` can raise `TypeError` or `AttributeError`, not only `ValueError`).

## Testing

Single files only: `uv run pytest tests/unit/test_orchestrator.py -x -q`.
Never run the full suite (see `tests/AGENTS.md`).

<!-- Reviewed 2026-08-18 against this subtree; the notes above still hold. -->
