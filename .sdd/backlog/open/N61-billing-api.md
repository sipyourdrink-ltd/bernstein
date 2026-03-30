# N61 — Billing API

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
External billing systems and internal finance tools have no programmatic way to query Bernstein usage data, requiring manual export and transformation for invoicing.

## Solution
- Add REST API endpoint `GET /api/v1/usage` to the task server
- Return metered usage data: tokens consumed, tasks executed, compute-minutes, grouped by time range
- Support query parameters: `from`, `to`, `granularity` (hourly, daily, monthly)
- JSON response with pagination (`offset`, `limit`, `total`)
- Authenticate via API key (from N53)

## Acceptance
- [ ] `GET /api/v1/usage` endpoint returns metered usage data
- [ ] Response includes tokens, tasks, and compute-minutes metrics
- [ ] `from` and `to` query parameters filter by time range
- [ ] `granularity` parameter supports hourly, daily, and monthly aggregation
- [ ] Response is paginated with `offset`, `limit`, and `total` fields
- [ ] Endpoint requires valid API key authentication
