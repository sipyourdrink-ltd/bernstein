# Cloudflare Adapters

The Codex-on-Cloudflare adapter runs agents on Cloudflare infrastructure instead of locally, by driving the sandbox bridge Worker you deploy into your own account. It needs a Cloudflare Workers Paid plan, and end-to-end verification against a live deployment is still pending — see the status note below before relying on it.

The Cloudflare Agents SDK adapter (registry key `cloudflare`) was removed in issue #2970. It refused every spawn and had no path to a working one: the Agents SDK dispatches to a Worker the operator writes rather than exposing an invocation contract to implement against, and it does not execute shell. Configuring `cli: cloudflare` now fails with an error naming this page's supported path instead of resolving to an adapter that always refuses.

---

## Codex-on-Cloudflare Adapter

**Module:** `bernstein.adapters.codex_cloudflare`
**Class:** `CodexCloudflareAdapter`

Runs Codex inside a Cloudflare sandbox container by driving the sandbox bridge
Worker you deploy into your own account (`@cloudflare/sandbox` 0.12.4, API
contract `1.0.0`). It creates a sandbox, seeds a workspace, streams the run over
SSE, collects a workspace diff, records content-addressed sandbox evidence, and
tears the container down.

Requires a Cloudflare Workers Paid plan and an operator-deployed bridge; without
`bridge_url` and `bridge_api_key` every method refuses, and it never falls back
to local execution.

!!! info "End-to-end verification against a live deployment is pending"
    Built and tested against the published bridge contract with recorded HTTP
    and SSE fixtures; not yet run against a real Cloudflare deployment.

**See [Codex on Cloudflare Sandboxes](cloudflare-codex-sandbox.md)** for deploy
steps, the instance-type requirement, the authentication warning, the
cancellation semantics, the live-verification command, and the stated
limitations.

---

## Running agents today

To run a coding agent on Cloudflare, use the Codex-on-Cloudflare adapter with a
bridge Worker deployed into your own account — see
[Codex on Cloudflare Sandboxes](cloudflare-codex-sandbox.md). It needs a Workers
Paid plan, and end-to-end verification against a live deployment is still
pending.

The alternative is to drive a worker you deployed yourself via
`bernstein.bridges.cloudflare.CloudflareBridge`. That bridge calls a `/agents/*`
route contract defined by Bernstein, not by Cloudflare: the Worker you deploy has
to implement those routes.

To run agents on this host instead, use a local adapter such as `claude`,
`codex`, `aider`, or `mock`.
