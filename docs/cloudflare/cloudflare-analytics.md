# Analytics & Billing (D1)

Bernstein's Cloudflare integration uses **D1** -- Cloudflare's serverless SQLite -- as the persistence layer for usage analytics, metering, and billing-tier enforcement. This is the data backbone of the hosted Bernstein SaaS but is usable by any deployment that needs durable per-user usage tracking.

> **Prompt caching note.** Bernstein's prompt caching is delivered via Anthropic's native `cache_control` headers (`core/agents/prompt_cache.py`), independent of Cloudflare Vectorize.

---

## D1 Analytics

**Module:** `bernstein.core.cost.d1_analytics`
**Class:** `D1AnalyticsClient`
**Source:** `src/bernstein/core/cost/d1_analytics.py`

Tracks per-user usage, metering events, and cost data for billing, dashboards, and usage reports. All writes are append-only -- you can replay the event log to reconstruct state.

### What gets captured

The schema is auto-created on first use via `D1AnalyticsClient.initialize_schema()`.

#### `usage_events` (append-only metering log)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT (PK) | UUID generated per row |
| `user_id` | TEXT | Owning user identifier |
| `event_type` | TEXT | One of `run_start`, `run_complete`, `agent_spawn`, `token_usage` |
| `timestamp` | REAL | Unix epoch seconds |
| `metadata` | TEXT | JSON-encoded context blob |
| `tokens_input` | INTEGER | Input tokens consumed |
| `tokens_output` | INTEGER | Output tokens consumed |
| `cost_usd` | REAL | Estimated cost in USD |
| `model` | TEXT | Model identifier (e.g. `claude-sonnet-4-6`) |
| `run_id` | TEXT | Orchestration run identifier |

A composite index `idx_user_events (user_id, timestamp)` keeps per-user time-range queries cheap.

#### `user_quotas`

| Column | Type | Description |
|--------|------|-------------|
| `user_id` | TEXT (PK) | User identifier |
| `tier` | TEXT | `free`, `pro`, `team`, `enterprise` |
| `updated_at` | REAL | Unix epoch seconds of last tier change |

### Configuration

`D1Config` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with D1: Edit permission |
| `database_id` | `str` | (required) | D1 database UUID (from `wrangler d1 create`) |
| `database_name` | `str` | `"bernstein-analytics"` | Human-readable name |

### Usage

```python
import time
from bernstein.core.cost.d1_analytics import (
    D1AnalyticsClient,
    D1Config,
    UsageEvent,
)

client = D1AnalyticsClient(D1Config(
    account_id="abc123",
    api_token="cf_token_...",
    database_id="d1-uuid",
))

# Idempotent; safe to call on every boot.
await client.initialize_schema()

# Record a single event.
await client.record_event(UsageEvent(
    user_id="user-42",
    event_type="run_start",
    timestamp=time.time(),
    model="claude-sonnet-4-6",
    run_id="run-001",
    tokens_input=5000,
    tokens_output=2000,
    cost_usd=0.045,
))

# Batch insert (single transaction).
await client.record_events_batch([event1, event2, event3])

# Per-user monthly summary.
summary = await client.get_usage_summary("user-42", "2026-04")
print(f"Runs:   {summary.total_runs}")
print(f"Agents: {summary.total_agents_spawned}")
print(f"Cost:   ${summary.total_cost_usd:.2f}")

# Quota gate before starting a run.
result = await client.check_quota("user-42", "pro")
if not result.within_limits:
    print(f"Over quota: {result.reason}")

# Top spenders for the month.
top = await client.get_top_users("2026-04", limit=10)
```

### Billing tiers

Pre-defined tiers in `BILLING_TIERS` (see `d1_analytics.py:140-189`):

| Tier | Daily runs | Parallel agents | Monthly cap | Features |
|------|-----------|-----------------|-------------|----------|
| `free` | 5 | 1 | $0 (free models only) | `basic_models` |
| `pro` | Unlimited | 5 | $49 | `all_models`, `priority_queue` |
| `team` | Unlimited | 10 | $199 | + `sso`, `audit_logs`, `shared_workspaces` |
| `enterprise` | Unlimited | 50 | Unlimited | + `dedicated_infra`, `sla` |

### Event types

| Event type | When recorded |
|-----------|---------------|
| `run_start` | Orchestration run begins |
| `run_complete` | Orchestration run finishes (success or fail) |
| `agent_spawn` | An agent process is spawned |
| `token_usage` | Token consumption checkpoint (mid-run) |

---

## Example queries

D1 is plain SQLite over HTTP. You can run ad-hoc analytics with the `wrangler d1 execute` CLI or via the Cloudflare API.

### Cost by model, last 30 days

```bash
wrangler d1 execute bernstein-analytics --command "
  SELECT model,
         SUM(tokens_input)  AS in_tok,
         SUM(tokens_output) AS out_tok,
         SUM(cost_usd)      AS cost
  FROM usage_events
  WHERE timestamp > strftime('%s','now','-30 days')
    AND event_type = 'token_usage'
  GROUP BY model
  ORDER BY cost DESC;
"
```

### Daily run volume per tier

```sql
SELECT date(usage_events.timestamp, 'unixepoch') AS day,
       user_quotas.tier,
       COUNT(*) AS runs
FROM usage_events
JOIN user_quotas USING (user_id)
WHERE event_type = 'run_start'
GROUP BY day, tier
ORDER BY day DESC;
```

### Approaching-quota users

```sql
SELECT user_id,
       SUM(cost_usd) AS month_to_date
FROM usage_events
WHERE strftime('%Y-%m', timestamp, 'unixepoch') = strftime('%Y-%m','now')
GROUP BY user_id
HAVING month_to_date > 40   -- 80% of $49 pro cap
ORDER BY month_to_date DESC;
```

---

## Where the data is consumed

- **Bernstein Cloud dashboard** -- `https://dashboard.bernstein.run` reads `usage_events` for the per-user spend chart and the team-level admin view.
- **Workspace sync metrics** -- `bridges/r2_sync.py` records `agent_spawn` events with the workspace size and file count in `metadata`. Useful for catching pathological repos.
- **Cost-export pipeline** -- `core/cost/cloud_cost_export.py` reads the local `.sdd/archive/tasks.jsonl` and can be paired with D1 to drive CloudHealth / Kubecost / Spot.io allocations. D1 is the durable substrate; the exporter is the one-shot transformer.
- **Self-hosted Grafana** -- the Grafana endpoint at `/grafana/dashboard` (see `core/observability/grafana_dashboard.py`) does **not** read D1 directly. It scrapes Prometheus on `/metrics`. Use D1 for billing-grade history; use Prometheus for live ops.

---

## Setup

See [Cloudflare setup](cloudflare-setup.md#4-create-a-d1-database-analytics-billing) for the `wrangler d1 create` flow. Schema creation is idempotent and runs on first call to `initialize_schema()` -- no separate migration step.
