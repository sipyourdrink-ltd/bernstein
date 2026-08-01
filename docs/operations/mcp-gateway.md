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

## Attestation interlock modes

The gateway has a provider-neutral pre-dispatch interlock for integrations
that need a completeness claim over connector calls. It is separate from the
observe-only instrumenter:

- **Enforced:** before upstream I/O, the configured evidence provider must
  return non-empty handles for both a verified, durably chained attestation
  and the dispatch marker that references it. A provider, signer, or durable
  append failure raises and the connector is not invoked.
- **Observed:** the gateway attempts the same preparation, but a failure is
  logged and the connector remains reachable. A receipt can still be built,
  but its verifier reports `observed`, never complete.

Completeness is derived from ordered chain projections. Every
`toolcall.enforced_dispatch` marker must reference a preceding
`toolcall.attestation`; a receipt field claiming `complete` cannot upgrade a
run with absent, reordered, or unmatched markers. The interlock protocol owns
neither identity keys nor policy evaluation, so native and external providers
can implement the same contract without becoming dependencies of the gateway.
The gateway hashes an opaque provider-defined scope, request span, server,
tool, request id, and argument digest into one call-intent digest. Returned
evidence must bind that exact digest; stale evidence for a different call is a
hard failure in enforced mode.

This initial integration seam is programmatic. The CLI does not select a
provider yet, and an unwired gateway retains its existing observe-only
behavior. The boundary covers calls that cross the gateway; it cannot contain
effects that bypass the host dispatch hook.

Measure the partial seam at its real boundary with:

```bash
uv run python scripts/bench_toolcall_interlock.py \
  --calls 256 --parallel 32 --repetitions 5
```

The script times complete `MCPGateway.handle_jsonrpc` dispatches, including WAL
recording, with and without the interlock under parallel load. Its in-process
provider returns content-bound evidence handles, so the reported delta is the
host-seam budget—not the future cost of signature verification or durable
chain storage. Repeat the same gateway-level measurement with the native
provider when that implementation lands; a signer-only microbenchmark does not
satisfy the dispatch-path requirement.

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
(`MCPGateway`, `GatewayReplay`, `ToolMetrics`, `create_gateway_sse_app`), and
`src/bernstein/core/security/toolcall_interlock.py` (provider-neutral enforced
and observed dispatch contract). `scripts/bench_toolcall_interlock.py` measures
the seam at that gateway boundary under parallel load.
