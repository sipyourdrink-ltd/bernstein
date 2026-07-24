# Cloudflare Adapters

The Codex-on-Cloudflare adapter runs agents on Cloudflare infrastructure instead of locally. **It is experimental and currently non-functional** — see the status note below before relying on it.

The Cloudflare Agents SDK adapter (registry key `cloudflare`) was removed in issue #2970. It refused every spawn and had no path to a working one: the Agents SDK dispatches to a Worker the operator writes rather than exposing an invocation contract to implement against, and it does not execute shell. Configuring `cli: cloudflare` now fails with an error naming this page's supported path instead of resolving to an adapter that always refuses.

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

## Running agents today

The Codex-on-Cloudflare adapter is non-functional at present (see the status note
above). Run agents locally (`claude`, `codex`, `aider`, `mock`) or drive a worker
you deployed yourself via `bernstein.bridges.cloudflare.CloudflareBridge`. That
bridge calls a `/agents/*` route contract defined by Bernstein, not by Cloudflare:
the Worker you deploy has to implement those routes.
