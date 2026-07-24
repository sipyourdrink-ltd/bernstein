# MCP gateway proxy

`bernstein gateway` is a transparent MCP JSON-RPC proxy: it sits between an
MCP client and an upstream MCP server, forwards every request unchanged,
and records each tool call to a write-ahead log (WAL). A later `gateway
replay` run can serve responses straight from that WAL, with no upstream
server needed — useful for offline debugging or deterministic re-runs of a
recorded session.

## Usage

```bash
# Point an MCP client at the gateway instead of the real server command.
bernstein gateway start --upstream "uvx mcp-server-git"

# SSE (HTTP) transport instead of stdio.
bernstein gateway start --upstream "npx @modelcontextprotocol/server-filesystem ." \
    --transport sse --port 8054

# Replay a previously recorded session offline.
bernstein gateway replay gw-abc12345

# List recorded x402 pay-and-retry settlements.
bernstein gateway settlements
```

### `bernstein gateway start`

| Flag | Default | Meaning |
|---|---|---|
| `--upstream CMD` | required | Shell command that starts the real upstream MCP server (e.g. `uvx mcp-server-git`). |
| `--transport` | `stdio` | `stdio` or `sse`. |
| `--port` | `8054` | Port for SSE transport (ignored in stdio mode). |
| `--run-id ID` | auto-generated | WAL run id; defaults to `gw-<8 hex chars>`. |
| `--server-name NAME` | `unknown` | Logical MCP server name recorded into the WAL for historical analytics. |

In `stdio` mode the gateway itself behaves as an MCP stdio server: an MCP
client should be pointed at `bernstein gateway start --upstream <cmd>`
rather than at the real server command. It spawns the upstream command as a
subprocess and proxies stdin/stdout between the client and that subprocess.
In `sse` mode it listens on `--port` and exposes `GET /sse` /
`POST /message`, still proxying to the upstream via stdio underneath.

### `bernstein gateway replay`

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | required (argument) | The `--run-id` (or auto-generated id) from a previous `gateway start` session. |
| `--transport` | `stdio` | `stdio` or `sse`. |
| `--port` | `8054` | Port for SSE transport. |

Replay reads `.sdd/runtime/wal/<run-id>.wal.jsonl` and serves responses from
it without connecting to any upstream process. If the file doesn't exist
the command exits `1` and points at `bernstein audit show` to list recorded
run ids. Replay indexes every `mcp_tool_call` WAL entry by
`method:tool_name` at startup, so lookups are O(1); a call with no matching
recorded entry gets a JSON-RPC error response (`"No recorded response for
this call"`) rather than hanging.

### `bernstein gateway settlements`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir`, `-w` | `.` | Project root containing `.sdd/`. |

Lists x402 pay-and-retry settlements recorded under
`.sdd/x402/settlements` — each row is a chain-anchored spend receipt
binding a settled upstream tool call to the exact WAL invocation it paid
for. See [x402 settlement for metered MCP gateway calls](x402-settlement.md)
for the full settlement flow and how to verify a receipt with
`bernstein mandate verify-settlement`.

## What gets recorded

Every proxied `tools/call` (and other JSON-RPC methods) is written to the
WAL as a `decision_type="mcp_tool_call"` entry with inputs
`{method, server_name, tool_name, arguments, request_id}` and output
`{result, error, latency_ms}`. Per-tool call count, error count, and
latency percentiles (p50/p90/p99) are accumulated in memory for the
duration of the process and exposed at `GET /gateway/metrics` in SSE mode.

When a run journal is wired in, each proxied call is also mirrored into the
journal / audit chain as a content-addressed `mcp.stateless_call` entry, so
a verifier can reconstruct the call ordering from the chain alone even
without the WAL file. A failure to anchor a call is logged but never takes
the proxy down — it shows up to a verifier as a call-index gap rather than
a crash.

## SSE transport correlation

The SSE transport is stateless by design (no gateway instance holds
per-client state): a response is correlated to its request by a
content-derived span id, taken from the request's `_meta` field if present
or otherwise deterministically derived from the request content. That span
id rides as the `X-Bernstein-Span-Id` response header and as the SSE event
`id:` line, so any gateway instance can serve any request and a client
juggling several in-flight requests still matches each response correctly.

## Source

`src/bernstein/cli/commands/gateway_cmd.py` (registered as
`bernstein gateway`, reachable via the back-compat alias
`bernstein.cli.gateway_cmd`), `src/bernstein/core/protocols/mcp/mcp_gateway.py`
(`MCPGateway`, `GatewayReplay`, `ToolMetrics`, `create_gateway_sse_app`).
