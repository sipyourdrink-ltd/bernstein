# 420 — Async orchestrator tick with concurrent task processing

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
`orchestrator.tick()` is fully synchronous: fetches tasks, claims them one-by-one via HTTP, spawns agents sequentially. This limits throughput to ~20-30 concurrent agents before latency dominates. The server is already async (FastAPI) but the orchestrator blocks on every I/O call.

## Implementation
1. Convert `Orchestrator.tick()` to `async def tick()`:
   - Use `httpx.AsyncClient` with connection pooling (max_connections=50)
   - Fetch open tasks and done tasks concurrently with `asyncio.gather()`
   - Claim tasks concurrently (batch of claims as gather)
   - Spawn agents concurrently within role groups
2. Persistent `httpx.AsyncClient` session (created once, reused):
   - Connection pool with keep-alive
   - Configurable timeout per request type (claim=5s, fetch=10s)
3. Convert `Orchestrator.run()` loop to async:
   - `await asyncio.sleep()` instead of `time.sleep()`
   - Clean shutdown via `asyncio.Event`
4. Update callers in bootstrap.py and main.py to use `asyncio.run()`
5. Keep spawner.spawn() sync (subprocess) but wrap in `asyncio.to_thread()`

## Files
- src/bernstein/core/orchestrator.py — async conversion
- src/bernstein/core/bootstrap.py — async entrypoint
- src/bernstein/cli/main.py — asyncio.run()
- tests/unit/test_orchestrator.py — update for async

## Completion signals
- test_passes: uv run pytest tests/unit/test_orchestrator.py -x -q
- file_contains: src/bernstein/core/orchestrator.py :: async def tick
