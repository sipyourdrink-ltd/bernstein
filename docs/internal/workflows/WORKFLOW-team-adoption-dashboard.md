# WORKFLOW: Team Adoption Dashboard
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-007-team-adoption-dashboard

---

## Overview

Engineering managers request org-level usage metrics at `GET /dashboard/team`. The endpoint aggregates data from five independent file-based and in-memory sources — cost tracker files, quality gate JSONL, merge queue state, task store, and team state — and returns a single JSON envelope with KPIs: total runs, tasks completed, cost saved, code merged, and quality gate pass rate.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Engineering Manager | Requests the dashboard via browser or API client |
| API Gateway (Starlette) | Routes request to the handler |
| Dashboard Handler | Orchestrates five aggregation sub-queries |
| TaskStore (in-memory) | Provides task status counts |
| Cost Tracker files | `.sdd/runtime/costs/*.json` — per-run cost snapshots |
| Quality Gate JSONL | `.sdd/metrics/quality_gates.jsonl` — append-only event log |
| Merge Queue / Archive | `.sdd/archive/tasks.jsonl` or merge queue state |
| TeamStateStore | `.sdd/runtime/team.json` — agent roster |
| TenantRegistry | Scopes data to the requesting org (currently missing) |

---

## Prerequisites

- Task server is running on port 8052
- `.sdd/` directory exists and is readable
- At least one orchestrator run has occurred (otherwise all KPIs return zero)
- For tenant-scoped view: tenant ID available via request header or session state

---

## Trigger

`GET /dashboard/team` — HTTP request from engineering manager's browser or monitoring tool.

Optional future query parameters: `?tenant_id=<org>`, `?since=<timestamp>`, `?window=24h`

---

## Workflow Tree

### STEP 1: Resolve `.sdd/` directory

**Actor**: Dashboard Handler (`_get_sdd_dir`)
**Action**: Determine the `.sdd/` root directory from app state, workdir, or cwd.
**Timeout**: <1ms (filesystem path resolution)
**Input**: `request.app.state.sdd_dir` | `request.app.state.workdir` | `Path.cwd()`
**Output on SUCCESS**: `sdd_dir: Path` → GO TO STEP 2 (parallel fan-out)
**Output on FAILURE**:
  - `FAILURE(no_sdd_dir)`: cwd fallback used, directory may not contain valid data → CONTINUE with zeros (degraded mode, not fatal)

**Observable states**:
  - Customer sees: nothing yet (request in flight)
  - Operator sees: nothing
  - Database: N/A
  - Logs: `[dashboard] sdd_dir resolved to {path}`

**REALITY CHECK FINDING RC-1 (Medium)**: `_get_sdd_dir` silently falls back to `Path.cwd() / ".sdd"`. In containerized deployments where cwd differs from the project root, all file-based aggregators silently return zeros. No warning is logged. Resolution: log a warning when falling back to cwd.

---

### STEP 2: Fan-out — five parallel aggregations

Steps 2a–2e execute concurrently. All are independent reads. Any single aggregator failing must not block the others — return zeros for the failed section and continue.

---

### STEP 2a: Aggregate costs

**Actor**: Dashboard Handler (`_aggregate_costs`)
**Action**: Scan `.sdd/runtime/costs/*.json`, sum `spent_usd`, `budget_usd`, and per-agent/per-model breakdowns from `usages[]` array.
**Timeout**: 5s (filesystem scan + JSON parse)
**Input**: `sdd_dir / "runtime" / "costs"` directory
**Output on SUCCESS**:
```json
{
  "total_spent_usd": 42.50,
  "total_budget_usd": 100.00,
  "cost_saved_usd": 57.50,
  "per_agent": {"agent-abc": 20.0, "agent-def": 22.50},
  "per_model": {"opus": 30.0, "sonnet": 12.50},
  "run_count": 5
}
```
→ MERGE into response envelope

**Output on FAILURE**:
  - `FAILURE(dir_not_found)`: directory doesn't exist → return `{}` (zeros), no cleanup needed
  - `FAILURE(json_parse_error)`: individual file corrupted → skip file, log warning, continue with remaining files
  - `FAILURE(permission_error)`: `.sdd/` not readable → return `{}`, log error

**REALITY CHECK FINDING RC-2 (Critical)**: Key mismatch between writer and reader. `CostTracker.save()` writes `"spent_usd"` (`cost_tracker.py:497`). Dashboard reads `data.get("total_cost_usd", 0.0)` (`team_dashboard.py:68`). Result: `total_spent_usd` is **always zero** even when valid cost files exist. The `budget_usd` key matches correctly.

**Required fix**: Dashboard must read `spent_usd` instead of `total_cost_usd`.

**REALITY CHECK FINDING RC-3 (Low)**: `cost_saved_usd = max(0, budget - spent)`. Negative ROI (spent > budget) is clamped to zero. This masks over-budget runs. Consider exposing the raw delta and letting the UI decide how to render negative savings.

---

### STEP 2b: Aggregate quality gates

**Actor**: Dashboard Handler (`_aggregate_quality`)
**Action**: Read `.sdd/metrics/quality_gates.jsonl`, count events where `blocked: false` (passed) vs `blocked: true` (failed). Compute `pass_rate_pct`.
**Timeout**: 5s
**Input**: `sdd_dir / "metrics" / "quality_gates.jsonl"`
**Output on SUCCESS**:
```json
{
  "passed": 45,
  "failed": 5,
  "total": 50,
  "pass_rate_pct": 90.0
}
```
→ MERGE into response envelope

**Output on FAILURE**:
  - `FAILURE(file_not_found)`: no quality gate data yet → return zeros
  - `FAILURE(parse_error)`: corrupted line → skip line, continue

**REALITY CHECK FINDING RC-4 (Critical)**: Data source mismatch. The canonical quality gate writer (`quality_gates.py:965`) writes to `.sdd/metrics/quality_gates.jsonl` in JSONL format with fields `{task_id, gate, blocked, reason, ts, verdict}`. The current dashboard implementation reads two non-existent paths instead:
  1. `.sdd/metrics/*.json` looking for `quality_gates[]` arrays — no writer produces this format
  2. `.sdd/runtime/quality/*.json` looking for `{passed, status}` — no writer produces this format

Result: quality gate KPIs are **always zero** in production.

**Required fix**: Dashboard must read `.sdd/metrics/quality_gates.jsonl` and parse `blocked` field (false = passed, true = failed).

---

### STEP 2c: Aggregate task stats

**Actor**: Dashboard Handler (`_task_stats`)
**Action**: Query in-memory `TaskStore.list_tasks()`, count by status.
**Timeout**: 1s (in-memory operation)
**Input**: `TaskStore` instance from `request.app.state.store`
**Output on SUCCESS**:
```json
{
  "total": 120,
  "completed": 95,
  "failed": 10,
  "in_progress": 5,
  "completion_rate_pct": 79.2,
  "by_status": {"DONE": 95, "FAILED": 10, "CLAIMED": 5, ...},
  "by_role": {"backend": 40, "qa": 30, ...},
  "by_agent": {"agent-abc": 20, ...}
}
```
→ MERGE into response envelope

**Output on FAILURE**:
  - `FAILURE(store_unavailable)`: TaskStore not initialized → return zeros
  - `FAILURE(attribute_error)`: store not in app state → return zeros

**REALITY CHECK FINDING RC-5 (Medium)**: `in_progress` count relies on `TaskStatus.IN_PROGRESS`, but the normal lifecycle is `OPEN → CLAIMED → DONE/FAILED`. `IN_PROGRESS` is only set by explicit `progress()` API calls. In most runs, `in_progress` will be zero while `CLAIMED` tasks are the actually-running ones. The dashboard should count `CLAIMED` tasks as "in progress" for the manager's view.

**REALITY CHECK FINDING RC-6 (Medium)**: `list_tasks()` returns all tasks across all tenants when called without filters. In a multi-tenant deployment, one org's manager sees another org's task counts.

---

### STEP 2d: Aggregate merge stats

**Actor**: Dashboard Handler (`_merge_stats`)
**Action**: Count merged PRs and files changed from archive or merge queue records.
**Timeout**: 5s
**Input**: Data source TBD — see RC-7

**Output on SUCCESS**:
```json
{
  "merged_count": 12,
  "files_changed_total": 87
}
```
→ MERGE into response envelope

**Output on FAILURE**:
  - `FAILURE(no_data)`: no merge records → return zeros

**REALITY CHECK FINDING RC-7 (Critical)**: No writer exists for either data source the dashboard reads:
  1. `.sdd/runtime/merge_queue/*.json` — `MergeQueue` writes a single `runtime/merge_queue.json` (singular), not a directory of per-merge files
  2. `.sdd/runtime/progress/*.json` — no writer exists anywhere

`drain_merge.py` produces `MergeResult` objects in memory but never persists them to disk. Result: merge KPIs are **always zero**.

**Required fix**: Either (a) `drain_merge.py` must persist `MergeResult` to a known location after each successful merge, or (b) the dashboard must derive merge stats from the task archive JSONL by counting `DONE` tasks whose `result_summary` mentions merge activity — fragile but no new writer needed.

**Recommended approach**: Add a `MergeResult` persistence step in `drain_merge.py` that writes to `.sdd/archive/merges.jsonl`. Dashboard reads that file.

---

### STEP 2e: Aggregate team roster

**Actor**: Dashboard Handler (reads `TeamStateStore`)
**Action**: Read `.sdd/runtime/team.json`, compute active/finished counts, role distribution.
**Timeout**: 2s
**Input**: `sdd_dir / "runtime" / "team.json"`
**Output on SUCCESS**:
```json
{
  "total_members": 8,
  "active_count": 3,
  "finished_count": 5,
  "roles": {"backend": 2, "qa": 1},
  "members": [...]
}
```
→ MERGE into response envelope

**Output on FAILURE**:
  - `FAILURE(file_not_found)`: no team file → return empty roster
  - `FAILURE(json_parse_error)`: corrupted file → return empty roster, log error

**REALITY CHECK FINDING RC-8 (High)**: `on_complete()` and `on_fail()` both set status to `"dead"` (`team_state.py:210-241`). They are indistinguishable in persisted state. An engineering manager cannot tell from the dashboard whether agents completed successfully or failed. Resolution: use `"completed"` vs `"failed"` as distinct terminal statuses instead of `"dead"` for both.

---

### STEP 3: Assemble response envelope

**Actor**: Dashboard Handler
**Action**: Merge outputs from Steps 2a–2e into a single JSON response. Compute `summary` section with top-level KPIs.
**Timeout**: <1ms (in-memory dict merge)
**Input**: Five aggregation results (any may be empty/zeros)
**Output on SUCCESS**:
```json
{
  "timestamp": 1712836800.0,
  "summary": {
    "total_runs": 5,
    "total_tasks": 120,
    "tasks_completed": 95,
    "tasks_failed": 10,
    "total_cost_usd": 42.50,
    "budget_usd": 100.00,
    "cost_saved_usd": 57.50,
    "quality_gate_pass_rate_pct": 90.0,
    "merged_count": 12,
    "files_changed_total": 87,
    "active_agents": 3,
    "total_agents": 8
  },
  "costs": { ... },
  "quality_gates": { ... },
  "tasks": { ... },
  "merges": { ... },
  "team": { ... }
}
```
→ Return HTTP 200 with JSON body

**Output on FAILURE**:
  - `FAILURE(serialization_error)`: unexpected data type → HTTP 500, log traceback

**Observable states**:
  - Customer sees: dashboard rendered with KPI cards, charts, agent roster
  - Operator sees: HTTP 200 in access logs with response time
  - Logs: `[dashboard] team dashboard served in {ms}ms`

---

## State Transitions

This workflow is stateless — it reads current state and returns it. No mutations occur.

```
[request_received] → (all aggregators succeed) → [response_200]
[request_received] → (some aggregators fail) → [response_200_degraded] (zeros for failed sections)
[request_received] → (handler crashes) → [response_500]
```

---

## Handoff Contracts

### Browser → Dashboard Handler

**Endpoint**: `GET /dashboard/team`
**Query params** (future): `tenant_id: str | None`, `since: float | None`, `window: str | None`
**Headers** (future): `Authorization: Bearer <token>`, `X-Tenant-Id: <org>`
**Success response**: HTTP 200, `Content-Type: application/json`, body as shown in Step 3
**Failure response**:
```json
{
  "ok": false,
  "error": "Internal server error",
  "code": "DASHBOARD_ERROR",
  "retryable": true
}
```
**Timeout**: 10s total (sum of all aggregator timeouts with parallelism)

### Dashboard Handler → TaskStore

**Method**: `store.list_tasks()` (in-process Python call)
**Input**: No filters (returns all tasks)
**Output**: `list[Task]` — full task objects with status, role, assigned_agent
**Timeout**: 1s
**On failure**: Return empty task stats section

### Dashboard Handler → Filesystem (costs, quality, merges, team)

**Method**: `Path.glob()` + `json.loads()` / line-by-line JSONL parse
**Input**: Directory path pattern
**Output**: Parsed JSON dicts
**Timeout**: 5s per aggregator
**On failure**: Return zeros for that section, log warning, continue

---

## Cleanup Inventory

No resources are created by this workflow. It is read-only.

---

## Reality Checker Findings Summary

| # | Finding | Severity | Spec section | Resolution |
|---|---|---|---|---|
| RC-1 | `_get_sdd_dir` silently falls back to cwd — wrong dir in containers | Medium | Step 1 | Log warning on cwd fallback |
| RC-2 | Key mismatch: dashboard reads `total_cost_usd`, writer writes `spent_usd` — costs always 0 | Critical | Step 2a | Dashboard must read `spent_usd` key |
| RC-3 | Negative ROI clamped to 0, masking over-budget runs | Low | Step 2a | Expose raw delta |
| RC-4 | Quality gate data source mismatch — reads non-existent paths, ignores `quality_gates.jsonl` | Critical | Step 2b | Read `.sdd/metrics/quality_gates.jsonl`, parse `blocked` field |
| RC-5 | `in_progress` count uses wrong status — `CLAIMED` tasks are the actually-running ones | Medium | Step 2c | Count `CLAIMED` as in-progress |
| RC-6 | No tenant scoping — all orgs see all tasks | Medium | Step 2c | Filter by `tenant_id` from request |
| RC-7 | No writer for merge stats — merge KPIs always 0 | Critical | Step 2d | Persist `MergeResult` to `.sdd/archive/merges.jsonl` |
| RC-8 | `on_complete` and `on_fail` both set `"dead"` — indistinguishable | High | Step 2e | Use distinct terminal statuses |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — all data present | Valid cost files, quality JSONL, tasks, merges, team | All KPIs populated with correct aggregated values |
| TC-02: Empty state — first run | No `.sdd/runtime/` files, empty TaskStore | HTTP 200 with all KPIs at zero |
| TC-03: Corrupted cost file | One cost JSON file has invalid JSON | Skip corrupted file, aggregate remaining files correctly |
| TC-04: Corrupted quality line | One JSONL line is malformed | Skip corrupted line, count remaining correctly |
| TC-05: Missing costs directory | `.sdd/runtime/costs/` doesn't exist | Cost section returns zeros, other sections unaffected |
| TC-06: High volume | 1000+ cost files, 10000+ quality events | Response within 10s SLA, no memory spike |
| TC-07: Concurrent requests | 10 simultaneous dashboard requests | All return consistent data, no file locking errors |
| TC-08: Tenant-scoped request (future) | `?tenant_id=org-123` | Only data for that tenant returned |
| TC-09: Cost key reads `spent_usd` | Cost file with `spent_usd: 42.50` | `total_spent_usd` shows 42.50, not 0 |
| TC-10: Quality reads JSONL | `quality_gates.jsonl` with `blocked: false/true` entries | Correct pass/fail counts |
| TC-11: CLAIMED counted as in-progress | 5 tasks in CLAIMED status | `in_progress` shows 5 |
| TC-12: Complete vs failed agents | 3 completed, 2 failed agents | Distinct counts, not merged into `finished_count` |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `.sdd/runtime/costs/*.json` is the canonical cost data location | Verified: `cost_tracker.py:480` | Cost KPIs show zeros |
| A2 | `.sdd/metrics/quality_gates.jsonl` is the canonical quality gate log | Verified: `quality_gates.py:965` | Quality KPIs show zeros |
| A3 | `TaskStore` is available in `request.app.state.store` | Verified: `server.py` app startup | Task KPIs show zeros |
| A4 | `.sdd/runtime/team.json` is atomically written | Verified: `team_state.py:133-136` | Partial reads possible |
| A5 | Dashboard is read-only — no mutations, no side effects | Verified: code inspection | N/A |
| A6 | Single-server deployment — no distributed aggregation needed | Not verified | Missing data from other nodes |

## Open Questions

- Should the dashboard support time-windowed queries (last 24h, last 7 days) or always show all-time totals?
- Should the dashboard include a "cost per task" breakdown derived from archive records?
- Should there be authentication/authorization on the dashboard endpoint?
- Should the dashboard expose Prometheus-compatible metrics for Grafana to consume directly?
- What is the refresh frequency expectation — real-time, 30s polling, manual refresh?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created with 8 Reality Checker findings | — |
| 2026-04-11 | 3 Critical gaps identified: cost key mismatch, quality source mismatch, missing merge writer | Documented required fixes |
