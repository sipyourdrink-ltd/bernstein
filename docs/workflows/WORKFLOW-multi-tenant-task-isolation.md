# WORKFLOW: Multi-Tenant Task Isolation (ENT-001)
**Version**: 1.1
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Review
**Implements**: ENT-001 (plans/strategic-300.yaml)

---

## Overview

Multi-tenant task isolation ensures that tasks, backlog files, metrics, WAL entries, and archive records belonging to one tenant are invisible to other tenants in a shared Bernstein server deployment. Every API request carries a tenant context (via `X-Tenant-Id` header or auth state), and every data path is scoped to `.sdd/{tenant_id}/`.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| API Client (agent/CLI) | Sends requests with tenant identity |
| FastAPI Middleware | Extracts and normalizes tenant ID from request |
| Task Routes | Enforces tenant scoping on every CRUD operation |
| TaskStore | Persists tasks with tenant_id; mirrors records to tenant-scoped backlog |
| TenantIsolationManager | Manages tenant contexts, quotas, directory layout |
| TenantRegistry | Validates tenant IDs against seed config |
| MetricsCollector | Mirrors metrics into tenant-scoped metrics directories |
| CostTracker | Scopes cost records by tenant |
| Filesystem (.sdd/) | Stores tenant-isolated backlog, metrics, WAL, and audit data |

---

## Prerequisites

- Bernstein server is running with a seed config (`bernstein.yaml`) that defines tenants
- `.sdd/` directory is writable
- Tenant IDs are configured in `bernstein.yaml` under `tenants:` (or the `default` tenant is used implicitly)

---

## Trigger

Any API request to a tenant-scoped endpoint. Tenant identity is resolved from:
1. `request.state.tenant_id` (set by auth middleware), OR
2. `X-Tenant-Id` HTTP header, OR
3. Falls back to `"default"` tenant

---

## Workflow Tree

### STEP 1: Tenant ID Extraction
**Actor**: `request_tenant_id()` (tenanting.py:170-176)
**Action**: Extract tenant ID from request state or X-Tenant-Id header; normalize via `normalize_tenant_id()`
**Timeout**: Synchronous, <1ms
**Input**: `Request` object
**Output on SUCCESS**: Normalized tenant ID string -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(empty_or_missing)`: Returns `"default"` -> GO TO STEP 2 (not a hard failure)

**Observable states**:
  - Customer sees: Nothing (transparent extraction)
  - Operator sees: Access log entry with extracted tenant_id
  - Database: No state change
  - Logs: `[access_log] tenant_id=<value> extracted`

---

### STEP 2: Tenant Scope Resolution
**Actor**: `resolve_tenant_scope()` (tenanting.py:110-137)
**Action**: Resolve effective tenant for the request. Checks:
  1. If bound tenant (from auth) is non-default and differs from requested tenant -> PermissionError
  2. If registry is configured and target tenant is unknown -> LookupError
**Timeout**: Synchronous, <1ms
**Input**: `{ bound_tenant: str, requested_tenant: str | None, registry: TenantRegistry | None }`
**Output on SUCCESS**: Effective tenant ID -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(permission_denied)`: Bound tenant tried to access another tenant's scope -> Return HTTP 403 `"tenant scope '{target}' is not accessible from '{bound}'"`. No cleanup needed.
  - `FAILURE(unknown_tenant)`: Requested tenant not in registry -> Return HTTP 404 `"unknown tenant '{target}'"`. No cleanup needed.

**Observable states**:
  - Customer sees: 403 error if scope violation
  - Operator sees: 403 in access log
  - Database: No state change
  - Logs: `[tenanting] PermissionError: tenant scope 'X' is not accessible from 'Y'`

---

### STEP 3: Tenant Directory Layout Provisioning
**Actor**: `ensure_tenant_data_layout()` (tenant_isolation.py:65-80) via TenantIsolationManager
**Action**: Create tenant-scoped directories on first access:
  - `.sdd/{tenant_id}/backlog/`
  - `.sdd/{tenant_id}/metrics/`
  - `.sdd/{tenant_id}/runtime/`
  - `.sdd/{tenant_id}/runtime/wal/`
  - `.sdd/{tenant_id}/audit/`
**Timeout**: <100ms (filesystem mkdir)
**Input**: `{ sdd_dir: Path, tenant_id: str }`
**Output on SUCCESS**: `TenantDataPaths` -> GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(os_error)`: Disk full or permission denied -> Return HTTP 500, log error. No cleanup needed (mkdir is idempotent).

**Observable states**:
  - Customer sees: Nothing (transparent provisioning)
  - Operator sees: New directories appear under `.sdd/{tenant_id}/`
  - Database: No DB change; filesystem directories created
  - Logs: `[tenant_isolation] Created tenant data layout for '{tenant_id}'`

---

### STEP 4: Quota Check (on task creation only)
**Actor**: `TenantIsolationManager.check_quota()` (tenant_isolation.py:183-196)
**Action**: Compare current task count against tenant's `max_tasks` limit
**Timeout**: Synchronous, <1ms
**Input**: `{ tenant_id: str, current_task_count: int }`
**Output on SUCCESS**: `(True, "")` -> GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(quota_exceeded)`: Tenant at max_tasks -> Return HTTP 429 `"Tenant {id} has reached max_tasks limit ({N})"`. No cleanup needed.

**Observable states**:
  - Customer sees: 429 Too Many Requests with reason
  - Operator sees: 429 in access log, tenant approaching limits
  - Database: No state change
  - Logs: `[tenant_isolation] Quota check failed for tenant '{id}': max_tasks={N}`

---

### STEP 5: Tenant-Scoped Data Operation
**Actor**: TaskStore + route handler
**Action**: Execute the actual CRUD operation with tenant scoping:

  - **CREATE** (POST /tasks): Sets `tenant_id` on TaskCreate body via `request_tenant_id()` (tasks.py:186). TaskStore persists to global JSONL AND mirrors to `.sdd/{tenant_id}/backlog/tasks.jsonl` (task_store.py:552-560).
  - **LIST** (GET /tasks): Filters tasks by `tenant_id` parameter in `store.list_tasks()` (tasks.py:733-737).
  - **CLAIM** (POST /tasks/claim): Scoped to tenant via `_resolve_request_tenant_scope()` (tasks.py:409).
  - **BATCH CLAIM** (POST /tasks/batch-claim): Validates each task's `tenant_id` matches request tenant (tasks.py:430-437).
  - **COMPLETE/FAIL**: Task looked up by ID, then `_assert_task_tenant()` verifies tenant ownership (tasks.py:164-168). Archive record mirrored to `.sdd/{tenant_id}/backlog/archive.jsonl`.
  - **COUNT** (GET /tasks/counts): Scoped by `tenant_id` (tasks.py:770).
  - **ARCHIVE** (GET /tasks/archive): Filtered by `tenant_id` (tasks.py:786).
  - **GRAPH** (GET /tasks/graph): Lists only tenant-scoped tasks (tasks.py:804).

**Timeout**: Varies by operation (10ms-500ms for file I/O)
**Input**: Operation-specific payload + effective tenant_id
**Output on SUCCESS**: Operation result (task, list, counts, etc.)
**Output on FAILURE**:
  - `FAILURE(task_not_in_tenant)`: Task exists but belongs to different tenant -> Return HTTP 404 `"Task '{id}' not found"` (intentionally 404, not 403, to avoid information leakage).
  - `FAILURE(io_error)`: JSONL write failure -> Retried 3x with exponential backoff (task_store.py:196-219) -> TaskStoreUnavailable after exhaustion.

**Observable states**:
  - Customer sees: Task data scoped to their tenant only
  - Operator sees: Global JSONL has all tasks; tenant-scoped JSONL under `.sdd/{tenant_id}/`
  - Database: Task records with `tenant_id` field; mirrored JSONL in tenant dirs
  - Logs: `[task_store] Task {id} created for tenant '{tenant_id}'`

---

### STEP 6: Tenant-Scoped Metrics Mirroring
**Actor**: MetricsCollector
**Action**: When task metrics are flushed, records are mirrored to `.sdd/{tenant_id}/metrics/*.jsonl` using `tenant_metrics_dir()` (tenanting.py:161-167)
**Timeout**: <100ms (file append)
**Input**: Metric record with `tenant_id` label
**Output on SUCCESS**: Metric written to both global and tenant-scoped paths
**Output on FAILURE**:
  - `FAILURE(io_error)`: Write failure to tenant metrics dir -> Logged, global metrics still written. Non-fatal.

**Observable states**:
  - Customer sees: Tenant-scoped cost/metrics via /costs?tenant=X
  - Operator sees: Per-tenant metrics files under `.sdd/{tenant_id}/metrics/`
  - Database: Metric JSONL files
  - Logs: `[metric_collector] Flushed metrics for tenant '{tenant_id}'`

---

## State Transitions

```
[request_received] -> (tenant extracted) -> [tenant_resolved]
[tenant_resolved] -> (scope valid) -> [operation_executing]
[tenant_resolved] -> (scope invalid) -> [rejected_403]
[operation_executing] -> (quota ok, operation succeeds) -> [completed]
[operation_executing] -> (quota exceeded) -> [rejected_429]
[operation_executing] -> (task not in tenant) -> [rejected_404]
[operation_executing] -> (I/O failure after retries) -> [error_500]
```

---

## Handoff Contracts

### API Client -> Task Routes
**Endpoint**: Any task endpoint (POST /tasks, GET /tasks, etc.)
**Payload**: Standard request body + `X-Tenant-Id` header
**Success response**: Operation-specific JSON
**Failure response**:
```json
{
  "detail": "string — error description"
}
```
HTTP status codes: 403 (scope violation), 404 (task not in tenant), 429 (quota exceeded)
**Timeout**: 30s (default FastAPI)

### TaskStore -> Filesystem (tenant backlog mirror)
**Endpoint**: File write to `.sdd/{tenant_id}/backlog/tasks.jsonl`
**Payload**: JSONL line with full task record including `tenant_id`
**Success response**: Line appended
**Failure response**: OSError -> retried 3x with backoff
**Timeout**: 5s per retry
**On failure**: Global JSONL is authoritative; tenant mirror is best-effort

### TenantIsolationManager -> Filesystem (state persistence)
**Endpoint**: Write to `.sdd/config/tenant_isolation.json`
**Payload**: JSON with tenant quotas and context state
**Success response**: File written
**Failure response**: Logged warning, in-memory state preserved
**Timeout**: 5s

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Tenant directories | Step 3 | Manual cleanup | `rm -rf .sdd/{tenant_id}/` |
| Tenant backlog JSONL | Step 5 | Manual cleanup | File deletion |
| Tenant metrics JSONL | Step 6 | Manual cleanup | File deletion |
| Tenant isolation state | Step 5 (persist) | Manual cleanup | Delete `.sdd/config/tenant_isolation.json` |
| In-memory tenant context | Step 3 | Server restart | Garbage collected |

Note: There is no automated tenant deletion workflow. Tenant cleanup is a manual operation. **This is a gap — see Open Questions.**

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | TaskStore writes to global JSONL and tenant-scoped JSONL, but WAL is NOT tenant-scoped — `task_store.py` does not write WAL entries to `.sdd/{tenant_id}/runtime/wal/` despite directories being created | High | Step 5 | WAL tenant scoping directories exist but are unused. The WAL path in `tenant_data_paths()` creates the directory but no code writes to it. Flag for implementation. |
| RC-2 | `_assert_task_tenant()` returns 404 (not 403) for cross-tenant access, which is correct for information-leakage prevention but means operators cannot distinguish "task doesn't exist" from "tenant violation" without checking logs | Low | Step 5 | Intentional design — document for operator awareness |
| RC-3 | `resolve_tenant_scope` raises `PermissionError` (-> HTTP 403) and `LookupError` (-> HTTP 404) — this is correctly mapped in `_resolve_request_tenant_scope` (tasks.py:157-160) | Info | Step 2 | Verified correct: PermissionError -> 403, LookupError -> 404 |
| RC-4 | Tenant quota check (`check_quota`) only checks `max_tasks` — it does not check `max_agents` or `budget_usd` at the route level, only task count | Medium | Step 4 | `max_agents` and `budget_usd` quotas are defined in TenantQuota but not enforced at the API route level. Partially enforced by `TenantRateLimiter` (tenant_rate_limiter.py) separately. |
| RC-5 | No tenant-scoped audit log entries — `audit_dir` is created but nothing writes audit events there | Medium | Step 3 | Audit directory provisioned but unused. Flag for implementation. |
| RC-6 | In-memory task store (`_tasks` dict in task_store.py:250) is a single flat dict — not partitioned by tenant. Tenant filtering happens at query time via list comprehension (task_store.py:958-964, 1638-1640). A tenant with many tasks impacts memory and query latency for all tenants. | Medium | Step 5 | No per-tenant memory partitioning. Acceptable for small deployments; may need sharding for large multi-tenant use. |
| RC-7 | Priority queues (`_priority_queues`, `_by_status`) are global, not tenant-scoped. `claim_next()` scans the global priority queue and filters by tenant after dequeue, causing wasted iterations when tenants have imbalanced workloads. | Low | Step 5 | Performance concern only at scale. Current design is functionally correct. |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — create task with tenant | POST /tasks with X-Tenant-Id: team-a | Task created with tenant_id=team-a; mirrored to .sdd/team-a/backlog/tasks.jsonl |
| TC-02: Tenant isolation — list tasks | GET /tasks with X-Tenant-Id: team-a | Only team-a tasks returned; team-b tasks invisible |
| TC-03: Cross-tenant access denied | GET /tasks?tenant=team-a with X-Tenant-Id: team-b | HTTP 403 "tenant scope not accessible" |
| TC-04: Unknown tenant rejected | POST /tasks with X-Tenant-Id: unknown-tenant | HTTP 403 "unknown tenant" (when registry configured) |
| TC-05: Quota exceeded | POST /tasks when tenant at max_tasks | HTTP 429 with quota message |
| TC-06: Default tenant fallback | POST /tasks without X-Tenant-Id | Task created under "default" tenant |
| TC-07: Batch claim cross-tenant | POST /tasks/batch-claim with task IDs from another tenant | Unauthorized IDs excluded from claim |
| TC-08: Archive tenant scoping | GET /tasks/archive?tenant=team-a | Only team-a archive records returned |
| TC-09: Metrics tenant mirroring | Complete a task for team-a | Metrics appear in .sdd/team-a/metrics/ |
| TC-10: Cost scoping | GET /costs?tenant=team-a | Only team-a costs returned with team-a budget |
| TC-11: Task detail cross-tenant | GET /tasks/{id} where task belongs to team-b, request from team-a | HTTP 404 (not 403) |

**Existing test coverage**: `tests/unit/test_tenant_isolation.py` (unit), `tests/unit/test_multi_tenant.py` (integration). TC-01 through TC-05, TC-06, TC-08, TC-09, TC-10 are covered.

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Tenant IDs in seed config are stable and not renamed at runtime | Verified: `build_tenant_registry` deduplicates but does not handle renames | Orphaned tenant directories if ID changes |
| A2 | Global JSONL is the authoritative data source; tenant-scoped JSONL is a mirror | Verified: TaskStore reads from global JSONL on replay | If global JSONL is lost, tenant mirrors alone are insufficient for recovery |
| A3 | `X-Tenant-Id` header is trusted (no spoofing protection at this layer) | Verified: No signature/MAC on tenant header | In shared deployments without auth middleware, any client can claim any tenant |
| A4 | Filesystem permissions are sufficient to isolate tenant directories | Not verified at OS level | A process with `.sdd/` access can read any tenant's data directly |

---

## Open Questions

- **Q1**: Should there be a tenant deletion/offboarding workflow? Currently no way to clean up a tenant's data programmatically.
- **Q2**: Should the WAL be tenant-scoped? Directories are created but nothing writes there. Is this intentional (WAL is global) or a gap?
- **Q3**: Should audit events be written to `.sdd/{tenant_id}/audit/`? Directory exists but is unused.
- **Q4**: Should `X-Tenant-Id` be validated against a signed token or auth session to prevent header spoofing?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code audit of tenanting.py, tenant_isolation.py, task_store.py, routes/tasks.py | — |
| 2026-04-08 | WAL and audit directories created but unused (RC-1, RC-5) | Documented as gaps |
| 2026-04-08 | Quota enforcement partial — only max_tasks checked at route level (RC-4) | Documented; TenantRateLimiter handles other quotas separately |
| 2026-04-08 | Verification pass: fixed RC-3 (LookupError correctly maps to 404, not 403 as previously claimed). Added RC-6 (flat task dict, no per-tenant partitioning) and RC-7 (global priority queues). Bumped to v1.1. | Spec updated |
