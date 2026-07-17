# Bernstein MCP server surface audit (2026)

This audit records the protocol surface the Bernstein MCP server exposes and
where it stands against common MCP server implementations in the ecosystem.
The goal is plain coverage: a host that auto-discovers MCP servers should be
able to connect and negotiate without surprises. Where a dimension is not yet
covered, the gap is recorded as a follow-up rather than left undefined.

## Status legend

| Mark | Meaning |
|------|---------|
| Yes | Implemented and tested. |
| Partial | Implemented for the common path; documented limits remain. |
| Planned | Acknowledged gap, tracked for a follow-up. |

## Comparison table

| Dimension | Common ecosystem baseline | Bernstein | Status |
|-----------|---------------------------|-----------|--------|
| Spec-rev compliance | Latest MCP spec revision | Targets spec rev `2025-03-26`, reported in the capability card and the HTTP `initialize` result | Yes |
| Transport: stdio | Standard for local IDE integration | `bernstein mcp` (default) | Yes |
| Transport: SSE | Common for remote/web | `bernstein mcp --transport http` (FastMCP SSE app) | Yes |
| Transport: streamable HTTP | Increasingly common for remote | POST request/response plus session management on `/mcp` | Yes |
| Transport: server-initiated SSE push (GET) | Optional | GET returns 501; cancellation handled over POST | Planned |
| Transport: WebSocket | Uncommon | Not implemented | Planned |
| Auth: anonymous | Loopback-only convenience | Allowed on loopback only; refused on non-loopback binds | Yes |
| Auth: static bearer | Common | Constant-time bearer check; token from env or config | Yes |
| Auth: OAuth-2 PKCE | Emerging | Protected-resource metadata (RFC 9728) served at `/.well-known/oauth-protected-resource` when `BERNSTEIN_MCP_OAUTH_ISSUER` is set; the client follows `authorization_servers[0]` to the IdP's own RFC 8414 metadata. Token issuance is delegated to the configured IdP. See `src/bernstein/mcp/oauth.py` | Partial |
| Auth: OIDC federation | Emerging | Not implemented; advertised under `auth.planned` | Planned |
| Capability negotiation: static manifest | Standard `initialize` capabilities | Static `capabilities` object on `initialize` | Yes |
| Capability negotiation: runtime capability cards | Less common | `bernstein://capability` resource and `capabilityCard` on `initialize`, built from live process state | Yes |
| Streaming tool-call output | Common | Tool calls run as cancellable tasks on the HTTP transport | Partial |
| Cancel in-flight tool call | Common | `notifications/cancelled` by request id | Yes |
| Partial-result preservation on cancel | Less common | Cancelled calls return `cancelled: true` with preserved `partial` chunks | Yes |
| Resource listing: prompts routes | Common | Built-in prompt catalogue served via `prompts/list` and `prompts/get`; see `src/bernstein/mcp/prompts.py` | Yes |
| Resource listing: sampling routes | Less common | Not implemented; advertised under `auth.planned`-style follow-up | Planned |
| Observability: per-call latency | Less common | `_meter.latency_ms` on every response | Yes |
| Observability: per-call cost meter | Uncommon | `_meter.cost_usd` envelope on every response | Yes |
| Observability: call trace id | Less common | `_meter.call_id` on every response | Yes |
| Tool-catalogue richness | Varies | Tiered catalogue (`core` / `standard` / `all`); see `docs/mcp/tool_tiers.md` | Yes |

## Surfaces landed in earlier passes

1. Per-call cost-meter envelope on every tool response (`result` plus `_meter`
   with latency, cost, trace id, status), uniform across stdio, SSE, and the
   streamable HTTP transport. Opt-out via `BERNSTEIN_MCP_COST_METER`.
2. Runtime capability cards: a `bernstein://capability` resource and the
   `capabilityCard` field on the HTTP `initialize` result, built from live
   process state (transports, auth modes, active tier, meter state, spec rev).
3. Streaming tool-call cancel with partial-result preservation on the
   streamable HTTP transport, via `notifications/cancelled`.

## Surfaces landed in this pass

1. Built-in prompt catalogue exposed via `prompts/list` and `prompts/get` on
   the FastMCP server and the streamable HTTP transport. Three orchestration
   prompts (`orchestrate_goal`, `triage_failed_tasks`, `cost_recap`) are
   rendered server-side from arguments; see `src/bernstein/mcp/prompts.py`.
2. OAuth-2 PKCE protected-resource metadata (RFC 9728) published at the
   `.well-known/oauth-protected-resource` discovery path on the streamable
   HTTP transport. The metadata is opt-in (configured via
   `BERNSTEIN_MCP_OAUTH_ISSUER`) so a client can locate the IdP by
   following `authorization_servers[0]` and then fetching the IdP's own
   RFC 8414 metadata from the IdP. Bernstein does not fabricate
   authorization-server metadata; only the IdP can publish that
   document. See `src/bernstein/mcp/oauth.py`.

## Follow-ups (tracked, not in this pass)

- Full OAuth-2 PKCE token issuance and OIDC federation behind the metadata.
  The discovery metadata advertises the IdP a host should redirect to;
  bearer-token validation against that issuer is still the static-bearer
  path.
- WebSocket transport.
- Server-initiated SSE push channel over GET.
- Sampling resource routes (`sampling/createMessage`).

New tools are out of scope for this pass by design; this work closes
protocol-surface gaps only.
