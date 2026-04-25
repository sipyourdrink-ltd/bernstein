# ACP native bridge

Bernstein speaks the [Agent Client Protocol](https://agentclientprotocol.org)
natively — editors that ship ACP support can plug Bernstein in as their
backend with zero per-IDE plumbing.

The bridge is a protocol adapter: ACP `prompt` opens a Bernstein task,
`streamUpdate` notifications tail the existing streaming-merge utility,
`cancel` walks the standard drain pipeline, and `setMode` toggles the
janitor approval gate. Cost-aware routing, HMAC audit, and sandbox-backend
selection apply identically to ACP-initiated and CLI-initiated sessions.

## Transports

| Transport | When to use | Command |
| --------- | ----------- | ------- |
| stdio (line-delimited JSON-RPC) | IDE embedding (default) | `bernstein acp serve --stdio` |
| HTTP / SSE | remote IDEs, CI, debugging | `bernstein acp serve --http :8062` |

Stdio is the canonical IDE transport: the editor spawns
`bernstein acp serve --stdio` as a subprocess and exchanges
line-delimited JSON-RPC frames over its stdio pipes. The HTTP transport
accepts a single JSON-RPC frame per `POST /acp` request and supports
`Accept: text/event-stream` for streaming responses.

## Zed `settings.json`

```json
{
  "agent_servers": {
    "bernstein": {
      "command": "bernstein",
      "args": ["acp", "serve", "--stdio"],
      "env": {}
    }
  }
}
```

## Generic ACP client (any editor)

```json
{
  "name": "bernstein",
  "transport": "stdio",
  "command": ["bernstein", "acp", "serve", "--stdio"],
  "protocolVersion": "2025-04-01"
}
```

For HTTP-mode integrations, point the client at `http://127.0.0.1:8062/acp`
and set `Accept: text/event-stream` to receive streaming
`streamUpdate` and `requestPermission` notifications.

## Methods supported

| Method | Direction | Notes |
| ------ | --------- | ----- |
| `initialize` | client → server | reports server capabilities, available adapters, and configured sandbox backends |
| `initialized` | client → server (notification) | acknowledged silently |
| `prompt` | client → server | opens a task in the existing task store; returns a real Bernstein session id |
| `streamUpdate` | server → client (notification) | tails the streaming-merge utility |
| `cancel` | client → server | walks the standard drain + shutdown pipeline |
| `setMode` | client → server | toggles `auto` (always-allow) ↔ `manual` (interactive approval gate) |
| `requestPermission` | server → client (prompt) and client → server (decision) | maps onto the janitor approval gate |

## Observability

Two Prometheus metrics ship with the bridge:

- `bernstein_acp_messages_total{method, outcome}` — JSON-RPC message
  count partitioned by method and outcome (`ok`, `error`, `rejected`,
  `cancelled`, `permission_denied`).
- `bernstein_acp_active_sessions` — gauge of live ACP sessions.

Both are exported via the existing observability stack — the running
task server's `/metrics` endpoint scrapes them automatically.

## Audit

Every ACP-driven mutation produces an HMAC-chained audit entry that is
byte-identical (modulo timestamp + chain HMAC) to the entry the CLI
surface emits for the same operation. See
`tests/integration/acp/test_audit_parity.py` for the parity guard.

## Out of scope (v1.9)

- Windows named-pipe transport — POSIX stdio + HTTP only.
- Bidirectional file-edit primitives that are not in the ratified ACP
  spec — those track in a follow-up.
- ACP authentication beyond loopback — remote HTTP usage rides the
  existing tunnel wrapper.
