# Orchestration engine

Deterministic tick loop: watch tasks, spawn agents, verify completion,
repeat. The orchestrator is plain Python code, not an LLM (ADR-006);
model calls belong to workers, never to coordination decisions.

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

Heavy lifting lives in sibling packages: `../tasks/task_lifecycle.py`
(claim, spawn, retry, completion) and `../agents/` (spawner, heartbeat,
crash detection, reaping).

## Invariants

- No LLM call in any coordination decision path
  (`docs/decisions/006-no-embedded-llm.md`).
- The tick loop is single-threaded by design; do not introduce
  concurrent ticks without restoring a tick guard (see the
  `orchestrator.py` module docstring).
- Replay is strict: a cache miss in a seeded run raises
  `ReplayMissError` instead of silently calling a live model
  (`deterministic.py`).
- Prefer pure decision functions (`run_stall.py` is the model) so a
  criterion is testable without a tick loop, server, or real clock.
- Optional third-party imports degrade to a no-op instead of raising;
  `trigger_manager.py` yields no events when `croniter` is absent.
- Operator-supplied config is validated per item inside the loop that
  consumes it, so one malformed entry is logged and skipped rather than
  aborting the whole pass. Catch the errors the library actually raises:
  a bad cron schedule reaches `croniter` as `TypeError` or
  `AttributeError` as readily as `ValueError`.

## Testing

Single files only, e.g.
`uv run pytest tests/unit/test_orchestrator.py -x -q`.
Never run the full suite (see `tests/AGENTS.md`).
