# Analytics & Caching

Bernstein's Cloudflare integration includes two data services: D1 for usage analytics and billing, and Vectorize for semantic LLM response caching.

---

## D1 Analytics

**Module:** `bernstein.core.cost.d1_analytics`
**Class:** `D1AnalyticsClient`

Tracks per-user usage, metering events, and cost data in Cloudflare D1 (serverless SQLite). Designed for the hosted Bernstein SaaS but usable by any deployment that needs persistent usage tracking.

### Configuration

`D1Config` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with D1: Edit permission |
| `database_id` | `str` | (required) | D1 database UUID (from `wrangler d1 create`) |
| `database_name` | `str` | `"bernstein-analytics"` | Human-readable name |

### Schema

The client auto-creates these tables via `initialize_schema()`:

```sql
-- Usage events (append-only metering log)
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- "run_start", "run_complete", "agent_spawn", "token_usage"
    timestamp REAL NOT NULL,
    metadata TEXT,                  -- JSON
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    model TEXT DEFAULT '',
    run_id TEXT DEFAULT ''
);

-- Index for efficient per-user queries
CREATE INDEX IF NOT EXISTS idx_user_events ON usage_events (user_id, timestamp);

-- User quota tracking
CREATE TABLE IF NOT EXISTS user_quotas (
    user_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL DEFAULT 'free',
    updated_at REAL NOT NULL
);
```

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

# Initialize tables (idempotent)
await client.initialize_schema()

# Record a usage event
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

# Batch insert
await client.record_events_batch([event1, event2, event3])

# Get monthly summary
summary = await client.get_usage_summary("user-42", "2026-04")
print(f"Runs: {summary.total_runs}")
print(f"Agents: {summary.total_agents_spawned}")
print(f"Cost: ${summary.total_cost_usd:.2f}")
print(f"Models: {summary.models_used}")

# Check quota
result = await client.check_quota("user-42", "pro")
if not result.within_limits:
    print(f"Over quota: {result.reason}")

# Top users by cost
top = await client.get_top_users("2026-04", limit=10)
```

### Billing tiers

Pre-defined tiers in `BILLING_TIERS`:

| Tier | Daily runs | Parallel agents | Monthly cap | Features |
|------|-----------|-----------------|-------------|----------|
| `free` | 5 | 1 | $0 (free only) | `basic_models` |
| `pro` | Unlimited | 5 | $49 | `all_models`, `priority_queue` |
| `team` | Unlimited | 10 | $199 | + `sso`, `audit_logs`, `shared_workspaces` |
| `enterprise` | Unlimited | 50 | Unlimited | + `dedicated_infra`, `sla` |

### Event types

| Event type | When recorded |
|-----------|---------------|
| `run_start` | Orchestration run begins |
| `run_complete` | Orchestration run finishes |
| `agent_spawn` | An agent is spawned |
| `token_usage` | Token consumption checkpoint |

---

## Vectorize Semantic Cache

**Module:** `bernstein.core.memory.vectorize_cache`
**Class:** `VectorizeSemanticCache`

Caches LLM completions by embedding similarity. When a new prompt is semantically close to a previously cached prompt (above a configurable threshold), the cached response is returned instead of making another LLM API call. This saves tokens and reduces latency.

### Configuration

`VectorizeConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with Vectorize and Workers AI permissions |
| `index_name` | `str` | `"bernstein-cache"` | Vectorize index name |
| `embedding_model` | `str` | `"@cf/baai/bge-base-en-v1.5"` | Workers AI embedding model |
| `similarity_threshold` | `float` | `0.92` | Minimum cosine similarity for cache hit |
| `max_cache_entries` | `int` | `10_000` | Maximum cache size |
| `ttl_seconds` | `int` | `86_400` | Cache entry TTL (24 hours) |
| `dimensions` | `int` | `768` | Vector dimensions (must match embedding model) |

### Usage

```python
from bernstein.core.memory.vectorize_cache import (
    VectorizeConfig,
    VectorizeSemanticCache,
)

cache = VectorizeSemanticCache(VectorizeConfig(
    account_id="abc123",
    api_token="cf_token_...",
))

# Check cache before LLM call
result = await cache.lookup("Decompose: add OAuth2 to the API")
if result.hit:
    print(f"Cache hit! Similarity: {result.similarity:.3f}")
    print(f"Cached response: {result.entry.response}")
    print(f"Tokens saved: {result.entry.tokens_saved}")
else:
    # Make the LLM call, then store result
    llm_response = await call_llm("Decompose: add OAuth2 to the API")
    cache_id = await cache.store(
        prompt="Decompose: add OAuth2 to the API",
        response=llm_response,
        model="claude-sonnet-4-6",
        tokens_used=3500,
    )

# Invalidate a specific entry
await cache.invalidate("cache-id")

# Clear all cache entries
deleted = await cache.clear()

# Check statistics
stats = cache.stats
print(f"Hit rate: {stats.hit_rate:.1%}")
print(f"Avg lookup: {stats.avg_lookup_ms:.1f}ms")
print(f"Tokens saved: {stats.total_tokens_saved}")
```

### Cache statistics

The `CacheStats` object tracks runtime performance:

| Property | Type | Description |
|----------|------|-------------|
| `lookups` | `int` | Total lookup attempts |
| `hits` | `int` | Successful cache hits |
| `misses` | `int` | Cache misses |
| `stores` | `int` | Entries stored |
| `total_tokens_saved` | `int` | Cumulative tokens saved |
| `hit_rate` | `float` | `hits / lookups` |
| `avg_lookup_ms` | `float` | Average lookup latency |

### How it works

1. The prompt is embedded using Workers AI (`@cf/baai/bge-base-en-v1.5` by default).
2. The embedding is queried against the Vectorize index for the top 3 nearest neighbors.
3. If the best match exceeds `similarity_threshold` (default 0.92), the cached response is returned.
4. On store, the prompt is embedded and upserted into the Vectorize index with the response and metadata.
5. Cache entries are identified by a truncated SHA-256 hash of the prompt.

!!! warning "Threshold tuning"
    The default threshold of 0.92 is conservative -- it only returns cached responses for very similar prompts. Lower it (e.g., 0.85) for more aggressive caching at the risk of returning less-relevant cached responses. Monitor `cache.stats.hit_rate` to find the right balance.
