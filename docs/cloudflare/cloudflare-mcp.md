# MCP Remote Transport

**Module:** `bernstein.mcp.remote_transport`
**Class:** `StreamableHTTPTransport`

The MCP remote transport exposes Bernstein's MCP server over HTTP using the streamable HTTP transport spec. This allows remote MCP clients (Claude Desktop, other agents, CI systems) to interact with a Bernstein instance over the network -- including deployment on Cloudflare Workers via a Python worker.

---

## Configuration

`RemoteMCPConfig` dataclass fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `str` | `"127.0.0.1"` | Bind host (loopback by default) |
| `port` | `int` | `8053` | Bind port |
| `path` | `str` | `"/mcp"` | URL path for MCP endpoint |
| `auth_type` | `str` | `"bearer"` | Authentication: `"none"`, `"bearer"`, or `"oauth"` |
| `auth_token` | `str` | `""` | Bearer token; when empty it is read from `BERNSTEIN_MCP_TOKEN` (or `BERNSTEIN_MCP_AUTH_TOKEN`) |
| `cors_origins` | `list[str]` | `["http://localhost:*"]` | CORS allowed origins |

The config is safe by default: it binds to loopback and expects a bearer
token. Construction raises `RemoteMCPConfigError` for any combination that
would expose the JSON-RPC surface without authentication - `auth_type="none"`
on a non-loopback host, or `auth_type="bearer"` with no token on a
non-loopback host. There is no session store, so there are no session
capacity or timeout fields (see "Stateless operation" below).

---

## Available tools

The remote transport exposes exactly these MCP tools:

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

# Start with defaults (binds to 127.0.0.1:8053; token from BERNSTEIN_MCP_TOKEN)
run_remote()

# Custom configuration. A non-loopback bind requires a bearer token,
# either passed explicitly or via BERNSTEIN_MCP_TOKEN.
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
    host="0.0.0.0",
    port=8053,
    auth_type="bearer",
    auth_token="my-secret-token",
    cors_origins=["https://myapp.example.com"],
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
| GET | `/mcp` | Server-initiated SSE stream (stub, returns 501; use POST plus `notifications/cancelled`) |
| DELETE | `/mcp` | Legacy session close; acknowledged as a no-op during the compat window, 405 afterwards |
| OPTIONS | `/mcp` | CORS preflight |

### Headers

| Header | Direction | Description |
|--------|-----------|-------------|
| `Authorization` | Request | `Bearer <token>` when `auth_type="bearer"` |
| `Content-Type` | Both | `application/json` |
| `mcp-session-id` | Request (legacy) | Removed by the stateless spec revision; accepted and ignored until 2027-07-28, then refused with 400. Never returned in responses. |

### Stateless operation

The 2026-07-28 MCP spec revision removed protocol sessions, and the
transport implements the stateless model (#2506):

1. There is no server-side session store. Every request is served from its
   body plus the per-request `_meta` field alone, so any transport instance
   can serve any request with no shared memory.
2. Cross-call continuity is anchored in the run journal and the HMAC audit
   chain, not in a session: when a journal and audit chain are wired in,
   every served `tools/call` is recorded as an ordered
   `mcp.stateless_call` entry with content-derived ids.
3. A legacy client that still sends the removed `mcp-session-id` header is
   served normally, with the header ignored, until 2027-07-28. After that
   date a request carrying the header is refused with HTTP 400.
4. `DELETE /mcp` (the removed session-close lifecycle) is acknowledged as a
   no-op (`200 {"status":"ok"}`) during the same window; there is nothing
   to close. After the window it returns 405.

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

Every request is self-contained; no session header is exchanged.

```bash
# Initialize (returns server info and capabilities)
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $BERNSTEIN_MCP_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}'

# List tools
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $BERNSTEIN_MCP_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}'

# Run a task
curl -X POST http://localhost:8053/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $BERNSTEIN_MCP_TOKEN" \
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
| `bearer` | `auth_type="bearer"` (default), token from `auth_token=` or `BERNSTEIN_MCP_TOKEN` | Validates the `Authorization: Bearer <token>` header |
| `none` | `auth_type="none"` | No authentication; only accepted on a loopback host |

!!! warning "Non-loopback binds require a token"
    `RemoteMCPConfig` refuses to start (`RemoteMCPConfigError`) when the
    host is not loopback and either `auth_type="none"` or the bearer token
    is empty. Set `BERNSTEIN_MCP_TOKEN` (or pass `auth_token=`) before
    binding to a public interface.

---

## CORS configuration

By default only localhost origins are allowed (`["http://localhost:*"]`). For production, set your application domains:

```python
config = RemoteMCPConfig(
    auth_type="bearer",
    auth_token="secret",
    cors_origins=["https://myapp.example.com", "https://admin.example.com"],
)
```

The legacy `mcp-session-id` header stays preflight-allowed during the
compat window so older browser clients can still send it (the transport
ignores it); no response header exposes it.

---

## Deployment on Cloudflare Workers

The ASGI app can be deployed as a Cloudflare Python worker:

```python
# worker.py
from bernstein.mcp.remote_transport import RemoteMCPConfig, create_asgi_app

config = RemoteMCPConfig(
    auth_type="bearer",
    auth_token="YOUR_SECRET",
)

app = create_asgi_app(
    server_url="https://your-bernstein-server.example.com:8052",
    config=config,
)
```

This lets MCP clients connect to your Bernstein instance from anywhere with Cloudflare's global edge network handling TLS and routing.
