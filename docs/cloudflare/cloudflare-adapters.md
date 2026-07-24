# Cloudflare Adapters

Two adapters were intended to run agents on Cloudflare infrastructure instead of locally: the Cloudflare Agents SDK adapter and the Codex-on-Cloudflare adapter. **Both are experimental and currently non-functional** — see the status notes below before relying on them.

---

## Cloudflare Agents SDK Adapter

**Module:** `bernstein.adapters.cloudflare_agents`
**Class:** `CloudflareAgentsAdapter`

!!! warning "Experimental — non-functional (issue #2782)"
    This adapter has no worker-trigger path. It could only start a local
    `npx wrangler dev` server, which is a long-running dev server that is never
    sent a request and never signals completion, so every task would run until
    the timeout watchdog killed it — producing no artifact. Rather than pretend,
    `spawn()` now refuses immediately with an actionable error.

    To run agents on Cloudflare today, deploy a worker that implements the
    `/agents/*` HTTP contract and drive it with
    `bernstein.bridges.cloudflare.CloudflareBridge`, or run agents locally with
    an adapter such as `claude`, `codex`, `aider`, or `mock`.

### Behaviour

`CloudflareAgentsAdapter.spawn()` raises `RuntimeError` with a message that
names issue #2782 and points to the working alternatives above. The
`cloudflare` registry key still resolves (so `cli: cloudflare` parses), but a
run routed to it fails fast instead of timing out.

### Configuration in bernstein.yaml

```yaml
cli: cloudflare
```

The registry key is `cloudflare` (see `bernstein.adapters.registry`); the module name `cloudflare_agents` is not a registered key.

---

## Codex-on-Cloudflare Adapter

**Module:** `bernstein.adapters.codex_cloudflare`
**Class:** `CodexCloudflareAdapter`

!!! warning "Experimental — non-functional (issue #2783)"
    This adapter drove every operation against
    `https://api.cloudflare.com/client/v4/accounts/{id}/sandbox/...`, a REST
    route family that **does not exist** — an authenticated request returns
    HTTP 400 with Cloudflare errors 7000/7003 ("No route for that URI").
    Cloudflare's real sandbox/container product runs inside a Worker/Durable
    Object (the `@cloudflare/sandbox` SDK), not a `client/v4` REST surface.

    Because the target API cannot be routed, no operation can run or populate a
    result. Every public method (`execute`, `get_status`, `cancel`, `get_logs`)
    now raises `RuntimeError` with an actionable message instead of issuing a
    doomed request. To run Codex on Cloudflare, drive a deployed worker via
    `bernstein.bridges.cloudflare.CloudflareBridge`; to run Codex locally, use
    the `codex` adapter.

### Configuration

`CodexSandboxConfig` and `CodexSandboxResult` remain importable so callers and
a future real implementation keep a stable surface, but no method issues a
request today.

`CodexSandboxConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cloudflare_account_id` | `str` | `""` | Cloudflare account ID |
| `cloudflare_api_token` | `str` | `""` | Cloudflare API token |
| `openai_api_key` | `str` | `""` | OpenAI API key for Codex |
| `sandbox_image` | `str` | `"codex-sandbox:latest"` | Container image for the sandbox |
| `max_execution_minutes` | `int` | `30` | Maximum execution time |
| `memory_mb` | `int` | `512` | Memory allocation in MiB |
| `cpu_cores` | `float` | `1.0` | CPU cores allocation |
| `network_access` | `str` | `"restricted"` | Network access level |
| `r2_bucket` | `str` | `"bernstein-workspaces"` | R2 bucket for workspace sync |

### Result type

`CodexSandboxResult` fields (populated only once a real sandbox surface is
implemented):

| Field | Type | Description |
|-------|------|-------------|
| `sandbox_id` | `str` | Sandbox instance identifier |
| `status` | `str` | `"completed"`, `"failed"`, `"timeout"`, or `"cancelled"` |
| `files_changed` | `list[str]` | Relative paths of modified files |
| `stdout` | `str` | Captured stdout |
| `stderr` | `str` | Captured stderr |
| `exit_code` | `int` | Process exit code |
| `execution_time_seconds` | `float` | Wall-clock execution time |
| `tokens_used` | `int` | Tokens consumed by Codex |

---

## Choosing between adapters

Both Cloudflare adapters are experimental and non-functional at present (see the
status notes above). Run agents locally (`claude`, `codex`, `aider`, `mock`) or
drive a deployed worker via `bernstein.bridges.cloudflare.CloudflareBridge`.
