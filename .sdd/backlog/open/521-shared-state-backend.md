# 521 — Shared state backend (PostgreSQL/Redis) for multi-instance

**Role:** backend
**Priority:** 2 (high)
**Scope:** large
**Depends on:** #519

## Problem

Task server stores everything in-memory with JSONL file persistence. This works
for single-instance but breaks for clusters: no shared state, no distributed
locking, state lost on crash with no replay.

## Design

### Storage tiers
- **Hot (Redis)**: task claims, agent heartbeats, bulletin board, distributed locks
- **Warm (PostgreSQL)**: tasks, metrics, evolution history, agent sessions
- **Cold (JSONL files)**: archive, unchanged — local backup / offline mode

### Migration path
1. Abstract `TaskStore` interface (already partially exists)
2. Add `PostgresTaskStore` implementation using asyncpg
3. Add `RedisCoordinator` for distributed locking (Redlock algorithm)
4. Config: `storage.backend: memory | postgres | sqlite` in bernstein.yaml
5. Default stays `memory` for single-instance; `postgres` for cluster mode

### Task claiming with distributed locks
```
CLAIM(task_id, agent_id):
  LOCK task_id via Redis (TTL 30s)
  SELECT status FROM tasks WHERE id = task_id
  IF status != 'open': UNLOCK, return None
  UPDATE tasks SET status='claimed', agent_id=agent_id
  UNLOCK
  return task
```

### Backward compatibility
- `bernstein run` with no config = in-memory (zero dependencies, works today)
- `bernstein run --cluster` = requires postgres + redis connection strings
- Auto-detect: if BERNSTEIN_DATABASE_URL is set, use postgres

## Files to modify
- `src/bernstein/core/server.py` — pluggable TaskStore
- New: `src/bernstein/core/store_postgres.py`
- New: `src/bernstein/core/store_redis.py`
- `bernstein.yaml` — storage config section

## Completion signal
- Tests pass with both memory and postgres backends
- Two servers sharing same postgres can coordinate tasks
- Redis locks prevent double-claiming
