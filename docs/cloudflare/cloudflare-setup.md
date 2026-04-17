# Cloudflare Setup Guide

This guide walks through provisioning the Cloudflare resources needed for Bernstein's cloud integration.

---

## Prerequisites

- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free tier is sufficient for Workers AI and basic Workers)
- [Node.js](https://nodejs.org/) 18+ (for wrangler CLI)
- `wrangler` CLI installed:

```bash
npm install -g wrangler
wrangler login
```

---

## 1. Get your account ID

Your account ID appears in the Cloudflare dashboard URL and is required by every module.

```bash
wrangler whoami
# Account ID: abc123def456...
```

Set it as an environment variable:

```bash
export CLOUDFLARE_ACCOUNT_ID="abc123def456"
# or
export CF_ACCOUNT_ID="abc123def456"
```

---

## 2. Create an API token

Go to **Cloudflare Dashboard > My Profile > API Tokens > Create Token**.

For full Bernstein integration, the token needs these permissions:

| Permission | Scope | Used by |
|-----------|-------|---------|
| Workers Scripts: Edit | Account | RuntimeBridge, WorkflowBridge, Agents adapter |
| Workers AI: Run | Account | Workers AI provider |
| D1: Edit | Account | D1 Analytics |
| R2: Edit | Account | R2 Workspace Sync |
| Vectorize: Edit | Account | Vectorize Cache |
| Browser Rendering: Run | Account | Browser Rendering bridge |

!!! tip "Least-privilege tokens"
    If you only need a subset of features (e.g., just Workers AI for free LLM planning), create a token with only those permissions.

```bash
export CLOUDFLARE_API_TOKEN="cf_token_..."
# or
export CF_API_TOKEN="cf_token_..."
```

---

## 3. Create an R2 bucket (workspace sync)

R2 stores workspace file snapshots for cloud-based agent execution. Agents running in Cloudflare sandboxes read their workspace from R2 and write results back.

```bash
wrangler r2 bucket create bernstein-workspaces
```

The default bucket name used by Bernstein is `bernstein-workspaces`. To use a different name, set it in bridge config:

```python
from bernstein.bridges.r2_sync import R2Config

config = R2Config(
    account_id="abc123",
    api_token="cf_token_...",
    bucket_name="my-custom-bucket",  # default: "bernstein-workspaces"
    max_file_size_mb=50,             # skip files larger than this
    exclude_patterns=(               # default exclusions
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "*.pyc",
        ".sdd/runtime",
        ".sdd/logs",
    ),
)
```

---

## 4. Create a D1 database (analytics & billing)

D1 is Cloudflare's serverless SQLite. Bernstein uses it for usage metering, billing tier enforcement, and cost reporting.

```bash
wrangler d1 create bernstein-analytics
```

Note the `database_id` from the output. The schema is created automatically on first use via `D1AnalyticsClient.initialize_schema()`.

```python
from bernstein.core.cost.d1_analytics import D1Config

config = D1Config(
    account_id="abc123",
    api_token="cf_token_...",
    database_id="d1-uuid-from-create",
    database_name="bernstein-analytics",  # human-readable name
)
```

---

## 5. Create a Vectorize index (semantic cache)

Vectorize stores embeddings for semantic LLM response caching. When a new prompt is similar enough to a cached one, Bernstein returns the cached response instead of making another LLM call.

```bash
wrangler vectorize create bernstein-cache \
  --dimensions 768 \
  --metric cosine
```

!!! note "Dimensions must match the embedding model"
    The default embedding model `@cf/baai/bge-base-en-v1.5` produces 768-dimensional vectors. If you change the embedding model, update the dimensions accordingly.

```python
from bernstein.core.memory.vectorize_cache import VectorizeConfig

config = VectorizeConfig(
    account_id="abc123",
    api_token="cf_token_...",
    index_name="bernstein-cache",           # default
    embedding_model="@cf/baai/bge-base-en-v1.5",  # default
    similarity_threshold=0.92,              # default; lower = more cache hits
    max_cache_entries=10_000,               # default
    ttl_seconds=86_400,                     # default: 24 hours
    dimensions=768,                         # must match embedding model
)
```

---

## 6. Deploy the agent Worker (optional)

If you want to run agents on Cloudflare Workers (not just use Workers AI locally), deploy the agent worker:

```bash
# From the Bernstein repo root
cd templates/bernstein-cloud
npx wrangler deploy --name bernstein-agent
```

Or use the CLI shortcut:

```bash
bernstein cloud deploy --worker-name bernstein-agent
```

---

## 7. Authenticate the Cloud CLI

For the hosted Bernstein Cloud service (api.bernstein.run):

```bash
bernstein cloud login --api-key YOUR_KEY
# or set via environment
export BERNSTEIN_CLOUD_API_KEY="your-key"
bernstein cloud login
```

Credentials are stored in `~/.config/bernstein/cloud-token.json` (mode 0600).

---

## Environment variable reference

| Variable | Required by | Description |
|----------|-------------|-------------|
| `CLOUDFLARE_ACCOUNT_ID` / `CF_ACCOUNT_ID` | All modules | Cloudflare account identifier |
| `CLOUDFLARE_API_TOKEN` / `CF_API_TOKEN` | All modules | API token with appropriate permissions |
| `CLOUDFLARE_API_KEY` | Agents adapter (legacy) | Global API key (prefer token) |
| `CLOUDFLARE_EMAIL` | Agents adapter (legacy) | Account email (only with global key) |
| `WRANGLER_SEND_METRICS` | Agents adapter | Control wrangler telemetry |
| `BERNSTEIN_CLOUD_API_KEY` | Cloud CLI | API key for bernstein.run hosted service |

---

## Verify setup

```bash
# Check Workers AI access
curl -X POST \
  "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Say hello"}]}'

# Check R2 bucket
wrangler r2 bucket list | grep bernstein

# Check D1 database
wrangler d1 list | grep bernstein

# Check Vectorize index
wrangler vectorize list | grep bernstein
```

---

## What to read next

- **[Bridges](cloudflare-bridges.md)** -- configure runtime, workflow, and sandbox bridges
- **[Workers AI](cloudflare-ai.md)** -- use free LLM models for planning
- **[Cloud CLI](cloudflare-cli.md)** -- manage cloud runs from the terminal
