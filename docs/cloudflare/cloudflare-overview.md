# Cloudflare Integration Overview

> **Preview:** The Cloud / Cloudflare surfaces are under active hardening. The hosted API (api.bernstein.run) does not resolve in DNS, so bernstein cloud commands report the service unreachable and exit non-zero. Rows graduate out of Preview individually as smoke coverage lands.

!!! warning "Experimental; hosted API not yet available"
    Cloud execution is experimental. Workers, Workflows, R2, and Browser
    Rendering run against **your own** Cloudflare account. The hosted Bernstein Cloud API at
    `api.bernstein.run` does not resolve in DNS, so `bernstein cloud run`,
    `status`, `runs`, and `cost` report that the service is unreachable
    and exit non-zero. `bernstein cloud login` only stores the key locally
    and prints a warning.

Bernstein can run agents locally or in the cloud. The Cloudflare integration lets you execute agents on Cloudflare's edge infrastructure using Workers, Durable Objects, and Workflows -- while the orchestrator stays deterministic and local.

---

## When to use cloud vs local

| Scenario | Recommended | Why |
|----------|-------------|-----|
| Solo developer, small codebase | Local | Zero setup, fastest iteration |
| CI/CD pipeline | Local (Docker/K8s) | Full filesystem access, no network hops |
| Team with shared orchestration | Cloudflare | Centralized billing, no shared server to maintain |
| Global team, low-latency agent dispatch | Cloudflare Workers | Edge execution near developers |
| SaaS / hosted Bernstein | Cloudflare (full stack) | D1 analytics, R2 storage |

---

## Architecture

```mermaid
graph TD
    User["Developer / CI"]
    CLI["bernstein cloud CLI"]
    Orch["Local Orchestrator<br/>(deterministic tick loop)"]
    Bridge["RuntimeBridge<br/>(cloudflare / cloudflare-workflow)"]

    subgraph Cloudflare["Cloudflare Edge"]
        Worker["Workers + Durable Objects<br/>(agent lifecycle)"]
        Workflow["Workflows<br/>(durable multi-step execution)"]
        R2["R2 Object Storage<br/>(workspace sync)"]
        D1["D1 (SQLite)<br/>(analytics & billing, planned)"]
        AI["Workers AI<br/>(free LLM provider, planned)"]
        Browser["Browser Rendering<br/>(web browsing)"]
    end

    MCP["MCP Remote Transport<br/>(streamable HTTP)"]

    User --> CLI --> Orch
    Bridge --> Worker & Workflow
    Worker --> R2
    Workflow --> Worker
    MCP --> Orch
```

The Cloudflare bridges and Browser Rendering are library classes you instantiate from your own code; the orchestrator's only runtime bridge is OpenClaw. Workers AI and D1 are planned.

---

## Module map

| Module | Import path | Purpose |
|--------|-------------|---------|
| Workers RuntimeBridge | `bernstein.bridges.cloudflare` | Spawn agents on Cloudflare Workers with Durable Objects |
| Workflow Bridge | `bernstein.bridges.cloudflare_workflow` | Durable multi-step workflows with auto-retry and approval gates |
| Browser Rendering | `bernstein.bridges.browser_rendering` | Headless browsing, screenshots, scraping, PDF generation |
| R2 Workspace Sync | `bernstein.bridges.r2_sync` | Content-addressed file sync between local and R2 |
| Workers AI Provider | (not implemented) | No `bernstein.core.routing.cloudflare_ai` module exists in the source tree; see [Workers AI](cloudflare-ai.md) |
| Codex-on-Cloudflare Adapter | `bernstein.adapters.codex_cloudflare` | Runs Codex in a sandbox container via an operator-deployed `@cloudflare/sandbox` bridge Worker; needs a Workers Paid plan |
| MCP Remote Transport | `bernstein.mcp.remote_transport` | Streamable HTTP transport for remote MCP server access |
| Cloud CLI | `bernstein.cli.commands.cloud_cmd` | `bernstein cloud` subcommands (init is local; login/run/status/runs/cost target the experimental, currently-unavailable `api.bernstein.run`) |

---

## What you need

At minimum:

- A Cloudflare account (free tier works for Workers AI and basic Workers)
- A Cloudflare API token with appropriate permissions
- Your Cloudflare account ID

For the full stack, you also need:

- An R2 bucket for workspace sync
- A D1 database for analytics (not yet used by any shipped Bernstein module)

See [Setup](cloudflare-setup.md) for step-by-step provisioning instructions.

---

## What to read next

- **[Setup Guide](cloudflare-setup.md)** -- provision Cloudflare resources
- **[Bridges](cloudflare-bridges.md)** -- runtime and workflow bridges
- **[Adapters](cloudflare-adapters.md)** -- Codex-on-Cloudflare
- **[Workers AI](cloudflare-ai.md)** -- planned free LLM provider for planning (not implemented)
- **[Analytics & Billing](cloudflare-analytics.md)** -- planned D1 usage metering and billing tiers (not implemented)
- **[Cloud CLI](cloudflare-cli.md)** -- `bernstein cloud` commands
- **[MCP Remote](cloudflare-mcp.md)** -- remote MCP transport
