# Wire TierAwareRouter into orchestrator and spawner

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** medium

## Problem

TierAwareRouter (601 LOC in `router.py`) is fully implemented with provider health tracking,
cost optimization, free tier awareness, and fallback chains — but the spawner still uses the
simple `route_task()` function. The router's intelligence is unused at runtime.

## Implementation

1. **Orchestrator.__init__()** — accept optional `TierAwareRouter` instance.
   If not provided but `.sdd/config/providers.yaml` exists, create one automatically.

2. **AgentSpawner.spawn_for_tasks()** — if router is available, call `router.route(task)`
   instead of `route_task(task)`. Return `RoutingDecision` with provider, model, fallback chain.

3. **Post-completion** — in `_handle_orphaned_task()`, after janitor verification:
   - Call `router.update_provider_health(provider, success=bool, latency_ms=int)`
   - Call `router.record_provider_cost(provider, tokens_in, tokens_out, cost_usd)`

4. **bootstrap.py** — optionally create TierAwareRouter from `.sdd/config/providers.yaml`
   and pass to Orchestrator.

5. **Fallback** — if router not configured, `route_task()` continues to work as before.

## Files
- src/bernstein/core/orchestrator.py
- src/bernstein/core/spawner.py
- src/bernstein/core/bootstrap.py
- tests/unit/test_orchestrator.py
- tests/unit/test_spawner.py

## Completion signals
- test_passes: uv run pytest tests/unit/test_orchestrator.py tests/unit/test_spawner.py tests/unit/test_router.py -x -q
- file_contains: src/bernstein/core/orchestrator.py :: TierAwareRouter
