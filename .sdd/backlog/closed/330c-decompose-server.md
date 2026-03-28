# 330c — Decompose server.py (2258 lines → modules)

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

`src/bernstein/core/server.py` is 2258 lines — the entire FastAPI app with all routes, store, middleware, SSE, dashboard, webhooks in one file.

## Design

Split into:

- `server.py` — FastAPI app creation, middleware, lifespan (~200 lines)
- `store.py` — TaskStore class (already exists but not used for all state)
- `routes/tasks.py` — task CRUD routes
- `routes/status.py` — status, dashboard, SSE routes
- `routes/webhooks.py` — GitHub webhook handler
- `routes/costs.py` — cost/budget endpoints

Use FastAPI routers (APIRouter) for clean separation.

## Completion signal

- `server.py` < 300 lines
- Routes organized by domain
- All server tests pass
