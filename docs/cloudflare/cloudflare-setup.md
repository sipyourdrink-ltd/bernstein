# Cloudflare Setup Guide

This guide walks through provisioning the Cloudflare resources needed for Bernstein's cloud integration.

---

## Prerequisites

- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free tier is sufficient for basic Workers)
- [Node.js](https://nodejs.org/) 18+ (for wrangler CLI)
- **wrangler v3.0+** installed. Earlier wrangler 2.x releases use a different command surface (notably for D1 and Workers AI) and will not match the commands below.

```bash
npm install -g wrangler@latest
wrangler --version   # should print 3.x or later
wrangler login
```

!!! note "wrangler version check"
    If you have a 2.x installation pinned by your project, run `npx wrangler@latest <command>` instead of installing globally.

---

## 1. Get your account ID

Your account ID appears in the Cloudflare dashboard URL and is required by every bridge config.

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
| Workers Scripts: Edit | Account | RuntimeBridge, WorkflowBridge |
| Workers AI: Run | Account | Workers AI (no Bernstein provider ships yet) |
| D1: Edit | Account | D1 analytics (not yet used by Bernstein) |
| R2: Edit | Account | R2 Workspace Sync |
| Browser Rendering: Run | Account | Browser Rendering bridge |

!!! tip "Least-privilege tokens"
    If you only need a subset of features (e.g., just the Browser Rendering bridge), create a token with only those permissions.

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
    max_file_size_mb=50,  # skip files larger than this
    exclude_patterns=(  # default exclusions
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

D1 is Cloudflare's serverless SQLite. No shipped Bernstein module uses it yet (see [Analytics & Billing](cloudflare-analytics.md)); this step only provisions a database for the planned integration.

```bash
wrangler d1 create bernstein-analytics
```

Note the `database_id` from the output.

---

## 5. Deploy the agent Worker (optional)

> Note: Prompt caching is delivered via Anthropic's native `cache_control` headers, independent of Cloudflare Vectorize. There is no `wrangler vectorize create` step in this setup. See [analytics](cloudflare-analytics.md) for context.



If you want to run agents on Cloudflare Workers, scaffold and deploy the agent worker. `bernstein cloud init` writes a free-tier `wrangler.toml` and a minimal `src/index.js` that answers every request with a static status (this works from the published wheel, which does not ship the repo `templates/` directory). To drive it with `CloudflareBridge`, replace the handler with one that implements `POST /agents/spawn`, `GET /agents/{id}/status`, `POST /agents/{id}/cancel` and `GET /agents/{id}/logs`:

```bash
bernstein cloud init                 # writes wrangler.toml + src/index.js
# set account_id in wrangler.toml, then:
npx wrangler deploy --name bernstein-agent
```

`bernstein cloud init` only scaffolds; the deploy itself is a wrangler step
run against your own Cloudflare account.

---

## 6. Authenticate the Cloud CLI

!!! warning "Hosted service is experimental"
    The hosted Bernstein Cloud service at `api.bernstein.run` is experimental
    and **not currently available** (the host does not resolve in DNS). The
    `bernstein cloud run/status/runs/cost` commands target it and will
    report that the service is not reachable; `bernstein cloud login` only
    stores the key in `~/.config/bernstein/cloud-token.json`. Everything else
    in this guide (R2, worker deployment, and provisioning the Workers AI and
    D1 resources) works against your own Cloudflare account and does not
    depend on the hosted service.

For the hosted Bernstein Cloud service (when available):

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
| `CLOUDFLARE_ACCOUNT_ID` / `CF_ACCOUNT_ID` | wrangler; your own code | Cloudflare account identifier (no Bernstein module reads it; pass it into the bridge configs) |
| `CLOUDFLARE_API_TOKEN` / `CF_API_TOKEN` | wrangler; your own code | API token with appropriate permissions (no Bernstein module reads it; pass it into the bridge configs) |
| `CLOUDFLARE_API_KEY` | wrangler (legacy) | Global API key (prefer token) |
| `CLOUDFLARE_EMAIL` | wrangler (legacy) | Account email (only with global key) |
| `WRANGLER_SEND_METRICS` | wrangler | Control wrangler telemetry |
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
```

---

## What to read next

- **[Bridges](cloudflare-bridges.md)** -- configure runtime, workflow, and sandbox bridges
- **[Workers AI](cloudflare-ai.md)** -- use free LLM models for planning
- **[Cloud CLI](cloudflare-cli.md)** -- manage cloud runs from the terminal
