# Cloudflare Bridges

Bernstein uses **bridges** to abstract where agents execute. The Cloudflare integration provides five bridges: the Workers runtime bridge, the Workflow bridge, the sandbox bridge, the browser rendering bridge, and the R2 workspace sync.

All bridges except browser rendering and R2 sync implement the `RuntimeBridge` interface from `bernstein.bridges.base`, making them drop-in replacements for local execution.

---

## Workers RuntimeBridge

**Module:** `bernstein.bridges.cloudflare`
**Class:** `CloudflareBridge`

Executes agents on Cloudflare Workers with Durable Objects. Each agent becomes a Durable Object instance with its own lifecycle.

### Configuration

| Parameter | Source | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `endpoint` | `config.endpoint` | Yes | -- | Base URL of deployed Worker (e.g. `https://my-worker.account.workers.dev`) |
| `api_key` | `config.api_key` | Yes | -- | Cloudflare API token |
| `account_id` | `config.extra["account_id"]` | Yes | -- | Cloudflare account ID |
| `worker_name` | `config.extra["worker_name"]` | No | `"bernstein-agent"` | Name of the deployed Worker script |
| `timeout_seconds` | `config.timeout_seconds` | No | (from BridgeConfig) | HTTP request timeout |
| `max_log_bytes` | `config.max_log_bytes` | No | (from BridgeConfig) | Max log bytes to fetch |

### Usage

```python
from bernstein.bridges.base import BridgeConfig, SpawnRequest
from bernstein.bridges.cloudflare import CloudflareBridge

config = BridgeConfig(
    bridge_type="cloudflare",
    endpoint="https://bernstein-agent.myaccount.workers.dev",
    api_key="cf_token_...",
    extra={
        "account_id": "abc123",
        "worker_name": "bernstein-agent",
    },
)

bridge = CloudflareBridge(config)
status = await bridge.spawn(SpawnRequest(
    agent_id="agent-001",
    prompt="Add input validation to all API endpoints",
    model="sonnet",
    role="backend",
    effort="high",
))

# Poll status
current = await bridge.status("agent-001")

# Fetch logs
logs = await bridge.logs("agent-001", tail=100)

# Cancel if needed
await bridge.cancel("agent-001")
```

### Worker API endpoints

The bridge communicates with the Worker via these HTTP endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/agents/spawn` | Create a new agent Durable Object |
| GET | `/agents/{id}/status` | Get agent state |
| POST | `/agents/{id}/cancel` | Request cancellation |
| GET | `/agents/{id}/logs` | Fetch stdout/stderr (supports `?tail=N`) |

---

## Workflow Bridge

**Module:** `bernstein.bridges.cloudflare_workflow`
**Class:** `CloudflareWorkflowBridge`

Maps Bernstein tasks to Cloudflare Workflows for durable, crash-proof execution with auto-retry and human approval gates.

### Workflow steps

Each task follows this pipeline:

```text
claim -> spawn -> execute -> verify -> [approval] -> merge -> complete
```

Steps are defined in the `WorkflowStep` enum:

| Step | Description |
|------|-------------|
| `CLAIM` | Task claimed by the workflow |
| `SPAWN` | Agent process spawned |
| `EXECUTE` | Agent executing the task |
| `VERIFY` | Quality gates and janitor verification |
| `APPROVAL` | Human approval gate (optional) |
| `MERGE` | Git merge of completed work |
| `COMPLETE` | Task marked done |

### Configuration

| Parameter | Source | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `bridge_type` | `config.bridge_type` | Yes | Must be `"cloudflare-workflow"` | Bridge type discriminator |
| `api_key` | `config.api_key` | Yes | -- | Cloudflare API token |
| `account_id` | `config.extra["account_id"]` | Yes | -- | Cloudflare account ID |
| `worker_name` | `config.extra["worker_name"]` | No | `"bernstein-agent"` | Worker script name |

The `WorkflowConfig` dataclass has additional tuning parameters:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_retries` | `int` | `3` | Maximum retry attempts per workflow step |
| `spawn_timeout_minutes` | `int` | `30` | Timeout for the spawn step |
| `execute_timeout_minutes` | `int` | `120` | Timeout for execution |
| `verify_timeout_minutes` | `int` | `15` | Timeout for verification |
| `require_approval` | `bool` | `False` | Gate on human approval before merge |

### Usage

```python
from bernstein.bridges.base import BridgeConfig, SpawnRequest
from bernstein.bridges.cloudflare_workflow import CloudflareWorkflowBridge

config = BridgeConfig(
    bridge_type="cloudflare-workflow",
    api_key="cf_token_...",
    extra={
        "account_id": "abc123",
        "worker_name": "bernstein-agent",
    },
)

bridge = CloudflareWorkflowBridge(config)

# Dispatch a workflow
status = await bridge.spawn(SpawnRequest(
    agent_id="task-42",
    prompt="Refactor the auth module",
    model="opus",
    role="architect",
))

# Check detailed workflow status
wf_status = await bridge.get_workflow_status("task-42")
print(wf_status.current_step)   # WorkflowStep.EXECUTE
print(wf_status.retries_used)   # 0

# Approve a workflow waiting at the approval gate
await bridge.approve("task-42")
```

!!! warning "Approval gates"
    When `require_approval` is `True`, workflows pause at the `APPROVAL` step until `approve()` is called. The agent state maps to `PENDING` during this wait.

---

## Sandbox Bridge

**Module:** `bernstein.bridges.cloudflare_sandbox`
**Class:** `CloudflareSandboxBridge`

Executes agent code in isolated V8 isolates or full Linux containers on Cloudflare's edge. No host filesystem access -- workspace files are synced via R2.

### Sandbox types

| Type | Enum | Characteristics |
|------|------|-----------------|
| V8 Isolate | `SandboxType.ISOLATE` | Fast startup, lightweight, limited to JavaScript/WASM runtime |
| Container | `SandboxType.CONTAINER` | Full Linux environment, heavier, supports any language |

### Configuration

| Parameter | Source | Required | Default | Description |
|-----------|--------|----------|---------|-------------|
| `bridge_type` | `config.bridge_type` | Yes | Must be `"cloudflare-sandbox"` | Bridge type discriminator |
| `api_key` | `config.api_key` | Yes | -- | Cloudflare API token |
| `account_id` | `config.extra["account_id"]` | Yes | -- | Cloudflare account ID |
| `sandbox_type` | `config.extra["sandbox_type"]` | No | `"isolate"` | `"isolate"` or `"container"` |
| `max_memory_mb` | `config.extra["max_memory_mb"]` | No | `128` | Memory limit in MiB |
| `max_execution_seconds` | `config.extra["max_execution_seconds"]` | No | `300` | Wall-clock timeout |
| `r2_bucket` | `config.extra["r2_bucket"]` | No | `"bernstein-workspaces"` | R2 bucket for workspace sync |

### Network access control

The `NetworkAccess` enum controls outbound network:

| Level | Enum | Behavior |
|-------|------|----------|
| None | `NetworkAccess.NONE` | No outbound network |
| Restricted | `NetworkAccess.RESTRICTED` | Only `allowed_domains` reachable |
| Full | `NetworkAccess.FULL` | Unrestricted outbound |

Default allowed domains (when restricted): `api.github.com`, `registry.npmjs.org`, `pypi.org`.

### Usage

```python
from bernstein.bridges.base import BridgeConfig, SpawnRequest
from bernstein.bridges.cloudflare_sandbox import CloudflareSandboxBridge

config = BridgeConfig(
    bridge_type="cloudflare-sandbox",
    endpoint="https://api.cloudflare.com",
    api_key="cf_token_...",
    extra={
        "account_id": "abc123",
        "sandbox_type": "container",
        "max_memory_mb": 256,
        "max_execution_seconds": 600,
        "r2_bucket": "bernstein-workspaces",
    },
)

bridge = CloudflareSandboxBridge(config)

status = await bridge.spawn(SpawnRequest(
    agent_id="agent-sandbox-01",
    prompt="Run the test suite and fix failures",
    model="sonnet",
    role="qa",
))

# List files modified in the sandbox (for selective R2 download)
artifacts = await bridge.download_artifacts("sandbox-id-from-metadata")
```

---

## Browser Rendering Bridge

**Module:** `bernstein.bridges.browser_rendering`
**Class:** `BrowserRenderingBridge`

Gives agents the ability to browse web pages, take screenshots, extract content, scrape data, generate PDFs, and execute JavaScript on rendered pages.

### Configuration

`BrowserConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with Browser Rendering permissions |
| `timeout_seconds` | `int` | `30` | HTTP request timeout |
| `viewport_width` | `int` | `1280` | Browser viewport width in pixels |
| `viewport_height` | `int` | `720` | Browser viewport height in pixels |
| `user_agent` | `str` | `"BernsteinBot/1.0"` | User-Agent string |
| `block_ads` | `bool` | `True` | Block ad-related requests |
| `javascript_enabled` | `bool` | `True` | Execute JavaScript on pages |

### Usage

```python
from bernstein.bridges.browser_rendering import BrowserConfig, BrowserRenderingBridge

browser = BrowserRenderingBridge(BrowserConfig(
    account_id="abc123",
    api_token="cf_token_...",
))

# Render a page and extract content
page = await browser.render("https://example.com", screenshot=True)
print(page.title)
print(page.content[:500])
print(page.links)

# Scrape structured data
data = await browser.scrape(
    "https://example.com",
    selector=".article",
    attributes=["text", "href"],
)

# Take a full-page screenshot (returns PNG bytes)
png = await browser.screenshot("https://example.com", full_page=True)

# Generate a PDF
pdf_bytes = await browser.pdf("https://docs.example.com")

# Execute JavaScript
result = await browser.execute_script(
    "https://example.com",
    "return document.querySelectorAll('a').length",
)
```

### Return types

- `render()` returns `PageResult` with: `url`, `title`, `content`, `html`, `screenshot_base64`, `status_code`, `load_time_ms`, `links`, `metadata`
- `scrape()` returns `ScrapedData` with: `url`, `selector`, `elements` (list of dicts)
- `screenshot()` returns raw PNG `bytes`
- `pdf()` returns raw PDF `bytes`
- `execute_script()` returns the JSON-serializable value from the script

---

## R2 Workspace Sync

**Module:** `bernstein.bridges.r2_sync`
**Class:** `R2WorkspaceSync`

Synchronizes local workspace files to/from Cloudflare R2 before and after cloud agent execution. Uses content-addressed storage (SHA-256 hashes) to minimize transfer size -- unchanged files are never re-uploaded.

### Configuration

`R2Config` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `account_id` | `str` | (required) | Cloudflare account ID |
| `api_token` | `str` | (required) | API token with R2 read/write permissions |
| `bucket_name` | `str` | `"bernstein-workspaces"` | R2 bucket name |
| `max_file_size_mb` | `int` | `50` | Skip files larger than this |
| `exclude_patterns` | `tuple[str, ...]` | See below | Glob patterns to exclude |

Default exclude patterns:

```python
(".git", "__pycache__", "node_modules", ".venv", "*.pyc", ".sdd/runtime", ".sdd/logs")
```

### Usage

```python
from pathlib import Path
from bernstein.bridges.r2_sync import R2Config, R2WorkspaceSync

sync = R2WorkspaceSync(R2Config(
    account_id="abc123",
    api_token="cf_token_...",
))

# Upload workspace before agent runs
manifest = await sync.upload(
    workdir=Path("/path/to/project"),
    workspace_id="task-123",
)
print(f"Uploaded {manifest.file_count} files, {manifest.total_bytes} bytes")

# Download modified files after agent completes
result = await sync.download(
    workdir=Path("/path/to/project"),
    workspace_id="task-123",
)
print(f"Downloaded {result.files_downloaded} changed files")
print(f"Changed: {result.files_changed}")

# Clean up after task completion
await sync.cleanup(workspace_id="task-123")
```

### How delta sync works

1. On upload, Bernstein scans the workspace and computes SHA-256 hashes for all files (respecting exclude patterns and size limits).
2. It fetches the existing manifest from R2 (if any) and compares hashes.
3. Only new or changed files are packaged into a zip and uploaded.
4. A manifest JSON is stored alongside the zip for future delta comparisons.
5. On download, the same comparison runs in reverse -- only files with different hashes are extracted from the zip.

!!! tip "Manifest structure"
    The `SyncManifest` contains `workspace_id`, a `files` dict mapping relative paths to SHA-256 hashes, `total_bytes`, and `file_count`. It is serializable via `to_dict()` / `from_dict()`.
