# Orchestration engine

Deterministic tick loop: watch tasks, spawn agents, verify completion, repeat. Coordination is plain Python, never an LLM (ADR-006).

## Key files

| File | Purpose |
|---|---|
| `orchestrator.py` | Public facade; single-threaded `while self._running` tick loop |
| `tick_pipeline.py` | Pure helpers: task fetching, batching, server interaction |
| `run_stall.py` | Pure decision function for zero-terminal run quiescence |
| `quiescence.py` | Records whether every process the run started had exited before the seal (#5272); `verified=False, method="unsupported"` where it cannot look, never a silent true |
| `controller_state.py` | `.sdd/runtime/controllers.json` sidecar: adaptive parallelism and claim-conflict cooldowns survive a restart; expired cooldowns are pruned on load |
| `deterministic.py` | Seeded-run LLM record/replay via `.sdd/runs/` journals |
| `escalation.py` | Journal-anchored, Ed25519-signed stall escalation receipts |
| `decomposition_proposal.py`, `decomposition_emitter.py` | Content-addressed proposal to split an issue whose tasks keep exhausting their retries |
| `trigger_manager.py` | Evaluates `triggers.yaml` rules into `TriggerEvent`s (event and cron sources) |
| `worker.py`, `terminal_report_outbox.py` | `bernstein-worker` process wrapper for spawned CLI agents; the outbox is the durable queue of terminal outcomes a worker still owes the central. The JSONL file is rewritten whole under a cross-process lock through the atomic-write helper (temp file, fsync, rename, best-effort directory fsync) rather than appended in place, so a crash mid-write leaves the previous file instead of a truncated one; logically the queue only ever gains an outcome, and a record is dropped only on an ack from the central (#2808) |
| `run_closure_owner.py` | Universal authenticated run closure owner |
| `issue_claim.py` | Claim etiquette for coordinator-free issue intake: recognises a run's own claim, detects a stale one |
| `source_fetcher.py` | The research worker's `fetch(ref) -> bytes` protocol plus a recorded, offline implementation (#3112) |

Heavy lifting lives in siblings: `../tasks/task_lifecycle.py` (claim, spawn, retry, completion) and `../agents/` (spawner, heartbeat, crash detection, reaping).

## Invariants

- No LLM in any coordination path (`docs/decisions/006-no-embedded-llm.md`).
- The tick loop is single-threaded by design; do not add concurrent ticks without restoring a tick guard (`orchestrator.py`).
- Replay is strict: a cache miss raises `ReplayMissError`, not a silent live call.
- Prefer pure decision functions (`run_stall.py` is the model) so a criterion is testable without a tick loop, server, or real clock.
- A quiescence self-stop writes `.sdd/runtime/spawner-deliberate-stop`, and the watchdog does not restart past it (#6097, #6474). So the settle re-check refuses to stop while any task is `claimed` or `in_progress`, whoever holds it (#6537), and counts archived tasks as terminal (#5968).
- A worker's terminal outcome is queued durably *before* it is posted and removed only on a response that acknowledges the task (#2808): never clear a queued outcome on a failed post, never read an unparseable or foreign 2xx body as that acknowledgement, reconcile `409`/`422` against `GET /tasks/{id}` rather than blind-retrying, and leave a conflicting server outcome in place instead of compensating over it. An exit status the worker did not observe is `unknown`, never success - resolved from server state, and it posts neither `/complete` nor `/release` (a release re-opens work that may be done).
- A failed task handed over by the quiescence pass is tracked for drain only; the completion chain runs for `done` tasks and never reopens a failed one (#6542).
- Optional third-party imports degrade to a no-op instead of raising; `trigger_manager.py` yields no events when `croniter` is absent. Operator config is validated per item: a bad entry is logged and skipped, never fatal (`croniter` can raise `TypeError` or `AttributeError`, not only `ValueError`).

## Testing

Single files only: `uv run pytest tests/unit/test_orchestrator.py -x -q`; quiescence lives in `tests/unit/test_run_quiescence.py` and `test_quiescence_*.py`. Never run the full suite (see `tests/AGENTS.md`).

<!-- Reviewed 2026-10-16 against this subtree; the notes above still hold. -->
