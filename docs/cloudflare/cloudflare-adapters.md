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

Runs OpenAI Codex agents inside Cloudflare sandboxes rather than locally. Combines Codex CLI capabilities with Cloudflare's isolated sandbox infrastructure for secure, scalable code execution.

### Configuration

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

### Usage

```python
from bernstein.adapters.codex_cloudflare import (
    CodexCloudflareAdapter,
    CodexSandboxConfig,
)

adapter = CodexCloudflareAdapter(CodexSandboxConfig(
    cloudflare_account_id="abc123",
    cloudflare_api_token="cf_token_...",
    openai_api_key="sk-...",
    memory_mb=1024,
    max_execution_minutes=60,
))

# Execute a task
result = await adapter.execute(
    prompt="Add input validation to all API endpoints",
    workspace_id="task-123",
    model="codex-mini",
    timeout_minutes=45,
)

print(result.status)                # "completed", "failed", "timeout"
print(result.files_changed)         # ["src/api/validation.py", ...]
print(result.execution_time_seconds)
print(result.stdout)
```

### Execution lifecycle

1. **Create sandbox** -- provisions a Cloudflare sandbox container with the specified image, memory, CPU, and network settings. Injects `OPENAI_API_KEY`, `WORKSPACE_R2_BUCKET`, and `WORKSPACE_ID` as environment variables.
2. **Sync workspace** -- the sandbox pulls workspace files from the configured R2 bucket.
3. **Inject Codex command** -- sends `codex exec --full-auto -m <model> <prompt>` to the sandbox.
4. **Poll for completion** -- checks sandbox status every 5 seconds until completed, failed, or timeout.
5. **Collect results** -- fetches stdout/stderr logs from the sandbox.
6. **Cleanup** -- terminates the sandbox on timeout or error.

### Result type

`CodexSandboxResult` fields:

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

### Management methods

```python
# Check status of a running sandbox
status = await adapter.get_status("sandbox-id")

# Cancel execution
await adapter.cancel("sandbox-id")

# Get logs
logs = await adapter.get_logs("sandbox-id")
```

---

## Choosing between adapters

| Criterion | Cloudflare Agents | Codex-on-Cloudflare |
|-----------|-------------------|---------------------|
| LLM Provider | Any (via Worker) | OpenAI Codex |
| Execution location | Local wrangler dev | Remote Cloudflare sandbox |
| Isolation | Worker process | Full container sandbox |
| Workspace sync | Manual | Automatic via R2 |
| Best for | Development, testing | Production, untrusted code |
