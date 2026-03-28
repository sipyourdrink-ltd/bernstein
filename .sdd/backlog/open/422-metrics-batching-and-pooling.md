# 422 — Metrics write batching and connection pooling

**Role:** backend
**Priority:** 3
**Scope:** small
**Complexity:** low
**Depends on:** [420]

## Problem
Metrics are written to JSONL files line-by-line on every event. Under high throughput (50+ agents), this causes file I/O contention. Also, no connection pooling for HTTP clients means new TCP connections per request.

## Implementation
1. Metrics write batching in `src/bernstein/core/metrics.py`:
   - Buffer metrics records in memory (max 50 records or 5 seconds, whichever first)
   - Flush buffer to JSONL in a single write
   - Flush on shutdown
   - Use `asyncio.Lock` to prevent concurrent writes to same file
2. Connection pooling:
   - Single `httpx.AsyncClient` instance shared across orchestrator (from #420)
   - Configure: `max_connections=50`, `max_keepalive_connections=20`
   - Pass shared client to all components that make HTTP calls
3. Benchmark before/after: measure tick() latency at 10, 30, 50 concurrent tasks

## Files
- src/bernstein/core/metrics.py — add batching
- src/bernstein/core/orchestrator.py — shared client
- tests/unit/test_metrics_batching.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_metrics_batching.py -x -q
- file_contains: src/bernstein/core/metrics.py :: flush
