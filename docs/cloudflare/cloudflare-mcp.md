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
| `cors_origins` | `list[str]` | `["http://localhost:*"]` | CORS allowed origins; clear-text `http://` origins must be loopback-pinned |

The config is safe by default: it binds to loopback and expects a bearer token. Construction raises `RemoteMCPConfigError` for any combination that would expose the JSON-RPC surface without authentication -- `auth_type="none"` on a non-loopback host, or `auth_type="bearer"` with no token on a non-loopback host. There is no session store, so there are no session capacity or timeout fields; see [Stateless serving](../mcp/server.md#stateless-serving).

The same rule covers browser origins. Bearer tokens ride on whatever origin CORS admits, so a clear-text origin is only accepted when it is pinned to a loopback host (`127.0.0.1`, `localhost` or `[::1]`); that is why the default is safe despite being clear-text. Any other clear-text origin is refused with a `RemoteMCPConfigError` naming the offending entries, so a non-loopback origin has to use TLS. The clear-text schemes held to this rule are `http`, `ws` and `ftp`.

Origins are parsed with `urllib.parse.urlsplit`, so a malformed authority (`http://[::1]evil.test`, `http://[::1]@evil.test`) is refused rather than being read as loopback. Origins carrying no scheme at all, such as the `*` and `null` CORS tokens, are left untouched.

---

## Available tools

The remote transport exposes exactly these MCP tools:

| Tool | Description | Required args |
|------|-------------|---------------|
| `bernstein_run` | Start an orchestration run (optionally a subtask via `parent_task_id`) | `goal` |
| `bernstein_status` | Liveness, task counts, cost; optional `status` filter and `detail` flag | None |
| `bernstein_cancel` | Cancel one task and its subtask tree | `task_id` |
| `bernstein_shutdown_orchestrator` | Whole-orchestrator shutdown signal | None |
| `bernstein_approve` | Approve a pending task | `task_id` |
| `bernstein_complete` | Complete a task the caller is executing | `task_id`, `result_summary` |

That is 6 advertised tools, a subset of what the stdio server registers. The
removed names (`bernstein_health`, `bernstein_tasks`, `bernstein_cost`,
`bernstein_stop`, `bernstein_create_subtask`) stay callable for one minor
release as deprecated aliases that answer with their historical payload plus
a notice naming the replacement; they are never advertised. Tools such as
`bernstein_run_status`, `bernstein_claim`, `bernstein_post_message`,
`bernstein_post_artifact`, `bernstein_task_capsule`, `load_skill`, the
scenario tool and `bernstein_verify_lineage` are not reachable over this
transport. The transport does serve `resources/list` and `resources/read`
for the capability card and the skill index (and, when
`BERNSTEIN_LINEAGE_MCP_ENABLED=1` opts in, the lineage records).

### Argument validation on this transport is weaker than on stdio

The deny-by-default input firewall described in
[MCP tool-call input validation](../mcp/input-validation.md) is applied by the
stdio and SSE servers in `bernstein.mcp.server`. The streamable HTTP transport
in `bernstein.mcp.remote_transport` does not call `validate_tool_call`. On this
path:

- the size cap, recursion cap and control-character filter are not applied;
- arguments are not checked against the JSON Schemas in
  `src/bernstein/mcp/tool_schemas/`; the schemas in the table above are
  restated inside the transport module and are looser than the real ones;
- a malformed argument object is rejected by the handler it reaches, if at
  all, rather than by a uniform structured validation error.

Treat every client of this transport as trusted, or terminate it behind a
gateway that validates request bodies. Starting the transport logs a
one-line warning saying the same thing.

Sharing one registry and one validation path between the transports is
tracked in issue #3083. This section is an interim notice and goes away with
that change.

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
