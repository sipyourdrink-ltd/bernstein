# Cloudflare Adapters

Two adapters let you run agents on Cloudflare infrastructure instead of locally: the Cloudflare Agents SDK adapter and the Codex-on-Cloudflare adapter.

---

## Cloudflare Agents SDK Adapter

**Module:** `bernstein.adapters.cloudflare_agents`
**Class:** `CloudflareAgentsAdapter`

Spawns agents via a Cloudflare Workers backend using `npx wrangler dev` locally. The adapter launches a local wrangler dev server that hosts a Cloudflare Agents SDK worker, passing the task prompt and model as Worker variables.

### Prerequisites

- Node.js 18+ and npm
- `wrangler` installed globally: `npm install -g wrangler`
- `wrangler login` completed
- A Cloudflare account with Workers enabled

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLOUDFLARE_ACCOUNT_ID` or `CF_ACCOUNT_ID` | Yes | Cloudflare account identifier |
| `CLOUDFLARE_API_TOKEN` or `CF_API_TOKEN` | Yes | API token with Workers permissions |
| `CLOUDFLARE_API_KEY` | No | Global API key (legacy, prefer token) |
| `CLOUDFLARE_EMAIL` | No | Account email (only with global key) |
| `WRANGLER_SEND_METRICS` | No | Control wrangler telemetry |

### How it works

1. The adapter builds a `npx wrangler dev` command with `--var` flags injecting the prompt, model, and session ID.
2. The command is wrapped with `build_worker_cmd()` for process visibility in `bernstein ps`.
3. Environment variables are filtered to only forward the Cloudflare-specific keys listed above (via `build_filtered_env()`).
4. The wrangler dev server runs as a subprocess with stdout/stderr captured to `.sdd/runtime/<session>.log`.
5. A timeout watchdog monitors the process.

### Configuration in bernstein.yaml

```yaml
cli: cloudflare_agents
```

### Spawn parameters

The adapter's `spawn()` method accepts the standard `CLIAdapter` parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | `str` | (required) | Task prompt for the agent |
| `workdir` | `Path` | (required) | Working directory |
| `model_config` | `ModelConfig` | (required) | Model and effort config |
| `session_id` | `str` | (required) | Unique session identifier |
| `mcp_config` | `dict` | `None` | MCP config (unused by this adapter) |
| `timeout_seconds` | `int` | `DEFAULT_TIMEOUT_SECONDS` | Process timeout |
| `task_scope` | `str` | `"medium"` | Task scope for budget caps |
| `budget_multiplier` | `float` | `1.0` | Retry budget multiplier |
| `system_addendum` | `str` | `""` | Additional system prompt text |

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
