# MCP Remote Transport

**Module:** `bernstein.mcp.remote_transport`
**Class:** `StreamableHTTPTransport`

The MCP remote transport exposes Bernstein's MCP server over HTTP using the streamable HTTP transport spec. This lets remote MCP clients (Claude Desktop, other agents, CI systems) interact with a Bernstein instance over the network -- including deployment on Cloudflare Workers via a Python worker.

> This page covers configuring and deploying the streamable HTTP transport as a standalone ASGI app. For the MCP protocol surface itself -- transports, the stateless serving model, auth (bearer and OAuth-2 PKCE), JSON-RPC methods, the cost-meter envelope, cancellation, and worked `curl` examples -- see [Bernstein MCP server](../mcp/server.md). That surface is identical across every deployment and is documented once there.

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

The config is safe by default: it binds to loopback and expects a bearer token. Construction raises `RemoteMCPConfigError` for any combination that would expose the JSON-RPC surface without authentication -- `auth_type="none"` on a non-loopback host, or `auth_type="bearer"` with no token on a non-loopback host. There is no session store, so there are no session capacity or timeout fields; see [Stateless serving](../mcp/server.md#stateless-serving).

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

The app serves the single `/mcp` endpoint (JSON-RPC 2.0 over POST, plus CORS preflight). Authenticate with `Authorization: Bearer <token>`; the full request/response protocol is in [Bernstein MCP server](../mcp/server.md).

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

The legacy `mcp-session-id` header stays preflight-allowed during the compat window so older browser clients can still send it (the transport ignores it); no response header exposes it.

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

This lets MCP clients connect to your Bernstein instance from anywhere, with Cloudflare's global edge network handling TLS and routing.
