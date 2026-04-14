# MCP Remote Transport

**Module:** `bernstein.mcp.remote_transport`
**Class:** `StreamableHTTPTransport`

The MCP remote transport exposes Bernstein's MCP server over HTTP using the streamable HTTP transport spec. This allows remote MCP clients (Claude Desktop, other agents, CI systems) to interact with a Bernstein instance over the network -- including deployment on Cloudflare Workers via a Python worker.

---

## Configuration

`RemoteMCPConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `str` | `"0.0.0.0"` | Bind host |
| `port` | `int` | `8053` | Bind port |
| `path` | `str` | `"/mcp"` | URL path for MCP endpoint |
| `auth_type` | `str` | `"none"` | Authentication: `"none"`, `"bearer"`, or `"oauth"` |
| `auth_token` | `str` | `""` | Bearer token (when `auth_type="bearer"`) |
| `cors_origins` | `list[str]` | `["*"]` | CORS allowed origins |
| `max_sessions` | `int` | `100` | Maximum concurrent MCP sessions |
| `session_timeout_seconds` | `int` | `3600` | Session expiry (1 hour) |

---

## Available tools

The remote transport exposes these MCP tools (same as the local MCP server):

| Tool | Description | Required args |
|------|-------------|---------------|
| `bernstein_health` | Liveness check | None |
| `bernstein_run` | Start an orchestration run | `goal` |
| `bernstein_status` | Task count summary | None |
| `bernstein_tasks` | List tasks (optional `status` filter) | None |
| `bernstein_cost` | Cost summary (total + per-role) | None |
| `bernstein_stop` | Graceful shutdown | None |
| `bernstein_approve` | Approve a pending task | `task_id` |
| `bernstein_create_subtask` | Create a subtask | `parent_task_id`, `goal` |

---

## Starting the server

### Python API

```python
from bernstein.mcp.remote_transport import RemoteMCPConfig, run_remote

# Start with defaults (binds to 0.0.0.0:8053)
run_remote()

# Custom configuration
run_remote(
    server_url="http://127.0.0.1:8052",  # Bernstein task server
    host="0.0.0.0",
    port=8053,
)
```

### ASGI application

For deployment with any ASGI server (uvicorn, hypercorn, Cloudflare Python workers):

```python
from bernstein.mcp.remote_transport import RemoteMCPConfig, create_asgi_app

config = RemoteMCPConfig(
    port=8053,
    auth_type="bearer",
    auth_token="my-secret-token",
    cors_origins=["https://myapp.example.com"],
    max_sessions=50,
)

app = create_asgi_app(
    server_url="http://127.0.0.1:8052",
    config=config,
)

# Run with uvicorn
import uvicorn
uvicorn.run(app, host="0.0.0.0", port=8053)
```

---

## HTTP protocol

The transport implements the MCP streamable HTTP transport spec:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/mcp` | JSON-RPC 2.0 request/notification (single or batch) |
| GET | `/mcp` | SSE stream endpoint (stub, returns 501) |
| DELETE | `/mcp` | Close an MCP session |
| OPTIONS | `/mcp` | CORS preflight |

### Headers

| Header | Direction | Description |
|--------|-----------|-------------|
| `mcp-session-id` | Both | Session identifier (returned on first POST, send on subsequent requests) |
| `Authorization` | Request | `Bearer <token>` when `auth_type="bearer"` |
| `Content-Type` | Both | `application/json` |

### Session lifecycle

1. First POST creates a new session and returns `mcp-session-id` in the response headers.
2. Subsequent requests include the session ID header to maintain state.
3. DELETE with the session ID closes the session.
4. Sessions expire after `session_timeout_seconds` (default 1 hour) of inactivity.
5. Expired sessions are pruned automatically on each request.

---

## JSON-RPC methods

| Method | Description |
|--------|-------------|
| `initialize` | Return server info and capabilities |
| `tools/list` | List available Bernstein tools |
| `tools/call` | Execute a tool by name |
| `ping` | Liveness check |
| `notifications/initialized` | Client notification (no-op) |

### Example request

```bash
# Initialize session
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}'

# List tools (include session ID from previous response)
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: SESSION_ID" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}'

# Run a task
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: SESSION_ID" \
  -d '{
    "jsonrpc":"2.0",
    "method":"tools/call",
    "params":{
      "name":"bernstein_run",
      "arguments":{"goal":"Add input validation","role":"backend"}
    },
    "id":3
  }'
```

---

## Authentication

| Mode | Config | Behavior |
|------|--------|----------|
| `none` | `auth_type="none"` | No authentication (default, for local development) |
| `bearer` | `auth_type="bearer"`, `auth_token="secret"` | Validates `Authorization: Bearer secret` header |

!!! warning "Production deployment"
    Always use `auth_type="bearer"` with a strong token when exposing the MCP server over the network. The `"none"` mode is only safe for local development.

---

## CORS configuration

By default, all origins are allowed (`["*"]`). For production, restrict to your application domains:

```python
config = RemoteMCPConfig(
    auth_type="bearer",
    auth_token="secret",
    cors_origins=["https://myapp.example.com", "https://admin.example.com"],
)
```

CORS headers exposed: `mcp-session-id`.

---

## Deployment on Cloudflare Workers

The ASGI app can be deployed as a Cloudflare Python worker:

```python
# worker.py
from bernstein.mcp.remote_transport import RemoteMCPConfig, create_asgi_app

config = RemoteMCPConfig(
    auth_type="bearer",
    auth_token="YOUR_SECRET",
    max_sessions=100,
)

app = create_asgi_app(
    server_url="https://your-bernstein-server.example.com:8052",
    config=config,
)
```

This lets MCP clients connect to your Bernstein instance from anywhere with Cloudflare's global edge network handling TLS and routing.
