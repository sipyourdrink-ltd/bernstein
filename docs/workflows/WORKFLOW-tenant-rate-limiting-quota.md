# WORKFLOW: Per-Tenant Rate Limiting and Quota Enforcement
**Version**: 0.1
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-008

---

## Overview

In multi-tenant mode, each tenant has configurable quotas: max concurrent agents, max tasks per hour, max API requests per minute, and max cost per day. These quotas are enforced at the API layer via middleware and route-level checks. Quota usage is visible in tenant dashboards. When a quota is exceeded, the system returns structured denial responses with retry-after headers.

---

## Actors
| Actor | Role in this workflow |
|---|---|
| Tenant User / Agent | Makes API requests scoped to a tenant |
| API Gateway (FastAPI middleware) | Intercepts requests, enforces per-tenant API rate limits |
| Route Handlers | Enforce domain-specific quotas (task creation, agent spawning) |
| TenantRateLimiter | Sliding-window rate limiter with per-tenant counters |
| TenantIsolationManager | Task-count quota checks, data path isolation |
| CostTracker | Tracks per-tenant spend, checks budget caps |
| TaskStore | Task creation, tenant-scoped queries |
| Orchestrator / Spawner | Agent lifecycle — records agent start/stop for concurrency tracking |

---

## Prerequisites
- Multi-tenant mode enabled in `bernstein.yaml` (tenants section populated)
- TenantRegistry built and loaded into `application.state.tenant_registry`
- TenantRateLimiter initialized with per-tenant configs
- TenantIsolationManager initialized with per-tenant quotas
- CostTracker initialized with per-tenant budget caps
- Tenant data directories provisioned (`.sdd/{tenant_id}/`)

---

## Trigger

Every inbound API request to the task server. Quota enforcement is passive (checked on each request), not active (no background enforcer).

---

## Workflow Tree

### STEP 1: Resolve Tenant Identity
**Actor**: SSOAuthMiddleware + request_tenant_id()
**Action**: Extract tenant identity from the request:
  1. Check `request.state.auth_claims` for tenant_id (set by JWT/SSO)
  2. Fall back to `x-tenant-id` header
  3. Fall back to `"default"` tenant
  4. Normalize via `normalize_tenant_id()` (lowercase, strip whitespace)
  5. Validate tenant exists in TenantRegistry
**Timeout**: N/A (in-process)
**Input**: HTTP request with auth headers
**Output on SUCCESS**: `tenant_id: string` attached to `request.state` -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(unknown_tenant)`: Tenant ID not in registry -> 403 Forbidden `{ "detail": "Unknown tenant" }`
  - `FAILURE(auth_failure)`: No valid auth token -> 401 Unauthorized (handled by auth middleware, before tenant resolution)

**Observable states during this step**:
  - Customer sees: nothing (transparent)
  - Operator sees: access log with tenant_id field
  - Logs: `[auth] request tenant_id=acme resolved from x-tenant-id header`

---

### STEP 2: Check Tenant Suspension
**Actor**: TenantRateLimiter (or middleware)
**Action**: Check if tenant is suspended
  1. Look up `TenantQuotaConfig` for tenant_id
  2. If `suspended == true`, deny immediately
**Timeout**: N/A (in-memory lookup)
**Input**: `{ tenant_id: string }`
**Output on SUCCESS**: Tenant not suspended -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(suspended)`: -> 403 Forbidden `{ "detail": "Tenant suspended", "code": "TENANT_SUSPENDED", "retry_after_s": 0 }`

**Observable states during this step**:
  - Customer sees: 403 error response
  - Logs: `[rate_limit] tenant=acme DENIED: suspended`

---

### STEP 3: Check API Request Rate Limit
**Actor**: Tenant Rate Limit Middleware (NEW — not yet wired)
**Action**: Enforce per-tenant requests-per-minute quota:
  1. Get `TenantQuotaConfig.requests_per_minute` for tenant (default: 60)
  2. Call `TenantRateLimiter.check_api_rate(tenant_id)`
  3. Sliding window: count request timestamps in last 60 seconds
  4. If count >= limit, calculate `retry_after_s = 60 - (now - oldest_timestamp_in_window)`
  5. If under limit, record timestamp and proceed
**Timeout**: N/A (in-memory)
**Input**: `{ tenant_id: string }`
**Output on SUCCESS**: Request allowed -> GO TO STEP 4 (route-specific checks)
**Output on FAILURE**:
  - `FAILURE(rate_limited)`: -> 429 Too Many Requests
    ```json
    {
      "detail": "Rate limit exceeded: 60 requests per minute",
      "code": "RATE_LIMITED",
      "quota_kind": "api_requests",
      "limit": 60,
      "current": 60,
      "retry_after_s": 12.5
    }
    ```
    Response header: `Retry-After: 13` (ceiling of retry_after_s)

**Observable states during this step**:
  - Customer sees: 429 with retry-after
  - Operator sees: rate_limit denial in structured logs
  - Logs: `[rate_limit] tenant=acme DENIED: api_requests 60/60, retry_after=12.5s`

**GAP — Not Wired**: `TenantRateLimiter.check_api_rate()` exists but is never called from middleware. The `RequestRateLimitMiddleware` in `auth_rate_limiter.py` is per-IP, not per-tenant. Fix: add tenant-aware middleware that calls `check_api_rate()` after tenant resolution.

---

### STEP 4: Route-Specific Quota Checks

Depending on the request type, additional quota checks apply:

#### STEP 4a: Task Creation Quota (POST /tasks, POST /tasks/batch)
**Actor**: Route handler + TenantRateLimiter + TenantIsolationManager
**Action**: Enforce tasks-per-hour quota:
  1. Call `TenantRateLimiter.check_task_quota(tenant_id)`
  2. Sliding window: count task creation timestamps in last 3600 seconds
  3. If count >= `tasks_per_hour` limit (default: 100), deny
  4. Also call `TenantIsolationManager.check_quota(tenant_id, current_count)` for absolute task count
  5. If both pass, record task creation timestamp
**Input**: `{ tenant_id: string, task_body: CreateTaskRequest }`
**Output on SUCCESS**: Task creation allowed -> proceed to TaskStore.create_task()
**Output on FAILURE**:
  - `FAILURE(task_quota)`: -> 429 Too Many Requests
    ```json
    {
      "detail": "Task quota exceeded: 100 tasks per hour",
      "code": "QUOTA_EXCEEDED",
      "quota_kind": "tasks_per_hour",
      "limit": 100,
      "current": 100,
      "retry_after_s": 1800
    }
    ```

**GAP — Partial Integration**: `TenantIsolationManager.check_quota()` is called in POST /tasks (returns 429). But `TenantRateLimiter.check_task_quota()` is never called. The isolation manager checks absolute task count, not tasks-per-hour sliding window.

#### STEP 4b: Agent Concurrency Quota (Agent Spawn)
**Actor**: Orchestrator / Spawner + TenantRateLimiter
**Action**: Enforce max-concurrent-agents quota:
  1. Before spawning an agent, call `TenantRateLimiter.check_agent_concurrency(tenant_id)`
  2. If `concurrent_agents >= max_concurrent_agents` (default: 6), deny spawn
  3. On spawn success, call `record_agent_start(tenant_id)` to increment counter
  4. On agent exit (success or failure), call `record_agent_stop(tenant_id)` to decrement counter
**Input**: `{ tenant_id: string }`
**Output on SUCCESS**: Agent spawn allowed
**Output on FAILURE**:
  - `FAILURE(concurrency)`: -> block spawn, queue task or return error
    ```json
    {
      "detail": "Concurrent agent limit exceeded: 6 agents",
      "code": "CONCURRENCY_EXCEEDED",
      "quota_kind": "concurrent_agents",
      "limit": 6,
      "current": 6,
      "retry_after_s": 0
    }
    ```

**GAP — Not Wired**: `record_agent_start()` and `record_agent_stop()` methods exist but are never called from the spawner or orchestrator. `check_agent_concurrency()` is never called. Fix: integrate into `AgentSpawner.spawn_for_task()` and agent reaping logic.

#### STEP 4c: Cost Budget Quota (Task Creation / Agent Spawn)
**Actor**: Route handler + CostTracker
**Action**: Enforce max-cost-per-day budget:
  1. Query `CostTracker` for today's spend for tenant_id
  2. Compare against `TenantConfig.budget_usd` (from `bernstein.yaml`)
  3. If spend >= budget, deny task creation or agent spawn
**Input**: `{ tenant_id: string }`
**Output on SUCCESS**: Budget available -> proceed
**Output on FAILURE**:
  - `FAILURE(budget_exceeded)`: -> 429 Too Many Requests
    ```json
    {
      "detail": "Daily budget exceeded: $50.00 of $50.00 used",
      "code": "BUDGET_EXCEEDED",
      "quota_kind": "cost_per_day",
      "limit": 50.0,
      "current": 50.0,
      "retry_after_s": 0
    }
    ```

**GAP — Not Enforced**: `TenantConfig.budget_usd` is stored and displayed in cost dashboards (attainment %), but never enforced to block requests. Fix: add budget check to task creation route and/or as middleware.

---

### STEP 5: Execute Request
**Actor**: Route handler
**Action**: Normal request processing (create task, list tasks, etc.) with tenant_id scoped:
  1. Task creation: set `task.tenant_id = tenant_id`
  2. Task queries: filter by `tenant_id` via `_verify_task_tenant_access()`
  3. Cost queries: filter by `tenant_id`
**Output on SUCCESS**: Normal 200/201 response
**Output on FAILURE**: Normal error handling (400, 404, 409, 500)

---

### STEP 6: Record Usage Metrics
**Actor**: TenantRateLimiter + CostTracker
**Action**: After successful request, update usage counters:
  1. API request timestamp already recorded in Step 3
  2. Task creation timestamp recorded in Step 4a (if applicable)
  3. Agent start recorded in Step 4b (if applicable)
  4. Cost tracked by CostTracker on API usage events
**Output**: Usage snapshot updated for dashboard queries

---

## State Transitions

```
Tenant quota states:

[under_quota] -> (request within limits) -> [under_quota] (counter incremented)
[under_quota] -> (request hits limit) -> [rate_limited] (429 returned)
[rate_limited] -> (sliding window expires oldest entry) -> [under_quota]
[any_state] -> (admin suspends tenant) -> [suspended] (403 on all requests)
[suspended] -> (admin unsuspends) -> [under_quota] (counters reset)
[under_quota] -> (budget spent >= budget_usd) -> [budget_exceeded] (block new tasks/agents)
[budget_exceeded] -> (new day / budget increased) -> [under_quota]
```

---

## Handoff Contracts

### Client -> API Gateway (Any Request)
**Headers required**:
- `Authorization: Bearer <token>` (agent JWT, SSO JWT, or legacy token)
- `x-tenant-id: <tenant_id>` (optional, falls back to auth claims or "default")

### Middleware -> TenantRateLimiter (Rate Check)
**Internal call**: `TenantRateLimiter.check_api_rate(tenant_id) -> QuotaDenial | None`
**On denial**: Middleware short-circuits with 429 + `Retry-After` header
**On allow**: Middleware calls `next(request)` to continue pipeline

### Route Handler -> TenantRateLimiter (Task Quota Check)
**Internal call**: `TenantRateLimiter.check_task_quota(tenant_id) -> QuotaDenial | None`
**On denial**: Handler raises `HTTPException(429, detail=denial.message)`

### Spawner -> TenantRateLimiter (Agent Concurrency Check)
**Internal call**: `TenantRateLimiter.check_agent_concurrency(tenant_id) -> QuotaDenial | None`
**On denial**: Spawner queues task or returns error to orchestrator

### Dashboard -> TenantRateLimiter (Usage Query)
**Internal call**: `TenantRateLimiter.get_usage_summary(tenant_id) -> dict`
**Response**:
```json
{
  "tenant_id": "acme",
  "api_requests": { "current": 42, "limit": 60, "window": "1m" },
  "tasks_per_hour": { "current": 15, "limit": 100, "window": "1h" },
  "concurrent_agents": { "current": 3, "limit": 6 },
  "storage_bytes": { "current": 0, "limit": 0 },
  "cost_today": { "current": 12.50, "limit": 50.00 }
}
```

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Request timestamps in sliding window | Step 3 | Auto-expiry | Timestamps older than window_seconds pruned on next check |
| Task timestamps in sliding window | Step 4a | Auto-expiry | Timestamps older than 3600s pruned on next check |
| Agent concurrency counter | Step 4b | record_agent_stop() | Decremented when agent exits |
| TenantUsageSnapshot | Initialization | reset_tenant() or server restart | In-memory, lost on restart |

No persistent resources created by rate limiting. All state is in-memory sliding windows.

**GAP — No Persistence**: All rate limiting state is in-memory. Server restart resets all counters, allowing a burst of requests that would normally be rate-limited. Risk: brief quota violation window after restart. Mitigation: persist snapshots to `.sdd/runtime/tenant_usage.json` periodically.

---

## Concurrency Analysis

### Sliding Window Thread Safety
- TenantRateLimiter uses synchronous methods (no async lock)
- FastAPI runs in asyncio event loop — synchronous methods are safe if no await between read and write
- **Risk**: If middleware is async and yields between checking and recording, a burst of concurrent requests could exceed the limit
- **Mitigation**: The check-and-record pattern in `check_api_rate()` is atomic within a single synchronous call (no yield points)

### Agent Counter Accuracy
- `record_agent_start()` increments, `record_agent_stop()` decrements
- If agent crashes without calling stop, counter permanently inflated
- **Risk**: Phantom agents consume quota permanently until server restart
- **Mitigation**: Periodic reconciliation — compare counter against actual running agent PIDs. Or use heartbeat-based tracking with timeout.

### Multi-Process Deployment
- Rate limiter is in-memory, per-process
- If server runs behind gunicorn with N workers, each worker has its own TenantRateLimiter
- Effective rate limit becomes N * configured_limit
- **Risk**: Quota enforcement is N times weaker than configured
- **Mitigation**: Use Redis or shared-memory for counters in multi-process mode

---

## Dashboard Requirements

### Tenant Usage Dashboard (NEW — route needed)
**Endpoint**: `GET /tenants/{tenant_id}/usage`
**Response**:
```json
{
  "tenant_id": "acme",
  "quotas": {
    "api_requests_per_minute": { "limit": 60, "current": 42, "utilization_pct": 70.0 },
    "tasks_per_hour": { "limit": 100, "current": 15, "utilization_pct": 15.0 },
    "concurrent_agents": { "limit": 6, "current": 3, "utilization_pct": 50.0 },
    "cost_today_usd": { "limit": 50.0, "current": 12.50, "utilization_pct": 25.0 }
  },
  "status": "active",
  "denials_last_hour": 0
}
```

### Tenant List Dashboard (NEW — route needed)
**Endpoint**: `GET /tenants`
**Response**: List of all tenants with summary usage and status

**GAP**: Neither endpoint exists. Cost data is available via `GET /costs?tenant=<id>` but no unified tenant usage view.

---

## Configuration

### Seed File (bernstein.yaml)
```yaml
tenants:
  - id: acme
    budget_usd: 50.0
    allowed_agents: [claude, codex]
    quotas:                          # NEW section needed
      requests_per_minute: 60
      tasks_per_hour: 100
      max_concurrent_agents: 6
      max_cost_per_day: 50.0

rate_limit:
  buckets:
    - name: api_general
      requests: 120
      window_seconds: 60
      path_prefixes: ["/"]
    - name: task_creation
      requests: 30
      window_seconds: 60
      path_prefixes: ["/tasks"]
      methods: ["POST"]
```

**GAP — Quota Config Not in Seed**: `TenantQuotaConfig` fields (requests_per_minute, tasks_per_hour, max_concurrent_agents) are not parsed from `bernstein.yaml`. They use hardcoded defaults. Fix: add `quotas` subsection to tenant config parsing in `_parse_tenants()`.

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Under quota | Tenant with 10/60 API requests | 200, request proceeds |
| TC-02: API rate limit hit | 61st request in 60s window | 429 with retry_after_s |
| TC-03: Task quota hit | 101st task creation in 1 hour | 429, task not created |
| TC-04: Agent concurrency hit | 7th agent spawn with limit=6 | Spawn blocked, task queued |
| TC-05: Budget exceeded | Spend >= budget_usd | 429, no new tasks/agents |
| TC-06: Tenant suspended | Request from suspended tenant | 403 on all requests |
| TC-07: Unknown tenant | x-tenant-id: "nonexistent" | 403 Forbidden |
| TC-08: Default tenant | No x-tenant-id header | Falls back to "default", default quotas apply |
| TC-09: Sliding window expiry | Wait 60s after rate limit | Requests succeed again |
| TC-10: Agent crash recovery | Agent crashes without stop | Counter inflated until reconciliation |
| TC-11: Concurrent burst | 100 concurrent requests from same tenant | At most `limit` succeed, rest get 429 |
| TC-12: Cross-tenant isolation | Tenant A at quota, Tenant B under | Tenant B requests succeed normally |
| TC-13: Retry-After accuracy | Rate limited with 45s remaining in window | Retry-After: 45 header |
| TC-14: Batch task creation | POST /tasks/batch with 20 tasks, quota=15 remaining | 429 before creating any (atomic check) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Single-process server deployment (asyncio, no gunicorn workers) | Not verified | Rate limits N times weaker with N workers |
| A2 | Tenant identity is always resolvable from auth token or header | Verified: request_tenant_id() falls back to "default" | Low risk — worst case, default tenant quotas apply |
| A3 | Cost data is available in near-real-time from CostTracker | Partially verified: CostTracker reads from JSONL | If JSONL flush is delayed, budget check lags |
| A4 | Agent exit always triggers cleanup (record_agent_stop) | Not verified: crash paths may skip | Counter drift over time |
| A5 | Sliding window timestamps use monotonic clock | Not verified: uses time.time() | Clock skew on NTP adjustment could cause brief window anomalies |
| A6 | TenantQuotaConfig defaults are reasonable (60 req/min, 100 tasks/hr, 6 agents) | Not verified: depends on workload | May need tuning per deployment |

---

## Open Questions

1. **Should quota config live in bernstein.yaml or a separate tenants.yaml?** Currently TenantConfig has budget_usd and allowed_agents but not rate limit parameters.
2. **Should rate limiting persist across server restarts?** Current in-memory design resets on restart. Is a brief burst acceptable?
3. **How should agent concurrency be reconciled?** If record_agent_stop is missed (crash), counter drifts. Options: periodic PID scan, heartbeat-based tracking, or accept drift until restart.
4. **Should the dashboard show historical usage or only current?** Current design is real-time sliding window only. Historical trends would require time-series storage.
5. **Should batch task creation be atomic?** If a batch of 20 tasks would exceed quota (15 remaining), should it reject all 20 or create 15?
6. **Should cost budget enforcement be hard (block) or soft (warn)?** Hard blocking could stop legitimate work mid-day. Soft warning lets operators intervene.

---

## Integration Roadmap (Implementation Order)

For the implementing engineer — recommended order to wire the existing components:

1. **Wire TenantRateLimiter into app state** — initialize in `create_app()`, register on `application.state`
2. **Parse quota config from seed** — extend `_parse_tenants()` to read `quotas` subsection
3. **Add tenant rate limit middleware** — new middleware after SSOAuthMiddleware that calls `check_api_rate()`
4. **Wire task quota check into POST /tasks** — call `check_task_quota()` alongside existing `check_quota()`
5. **Wire agent concurrency into spawner** — call `check_agent_concurrency()` before spawn, `record_agent_start/stop` on lifecycle
6. **Wire budget enforcement** — call CostTracker check before task creation
7. **Add tenant usage routes** — `GET /tenants/{id}/usage`, `GET /tenants`
8. **Add Retry-After headers** — ensure all 429 responses include the header

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code discovery | — |
| 2026-04-08 | TenantRateLimiter exists but is never called from routes or middleware | Documented as GAP; all check methods unused |
| 2026-04-08 | record_agent_start/stop never called from spawner | Documented as GAP; concurrency quota unenforced |
| 2026-04-08 | Budget tracked but not enforced | Documented as GAP; display-only in cost dashboard |
| 2026-04-08 | Quota config not parsed from bernstein.yaml | Documented as GAP; uses hardcoded defaults |
| 2026-04-08 | No tenant usage dashboard routes exist | Documented as GAP; recommend GET /tenants/{id}/usage |
| 2026-04-08 | RequestRateLimitMiddleware is per-IP, not per-tenant | Documented as GAP; separate concern from tenant rate limiting |
