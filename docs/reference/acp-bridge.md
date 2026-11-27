# ACP native bridge

Bernstein speaks the [Agent Client Protocol](https://agentclientprotocol.org)
(**ACP v1**, `protocolVersion: 1`) over JSON-RPC 2.0 so editors with ACP
support can drive Bernstein directly over stdio or HTTP/SSE.

Existing clients using the legacy `"2025-04-01"` dialect remain supported on
the same transport. Tasks dispatched by the bridge to the Bernstein task server
(`--server-url`) reuse the server's routing and sandbox-backend configuration;
see [Audit](#audit) and
[Planned in follow-up runner integration](#planned-in-follow-up-runner-integration-pr-2-placeholders)
for what is active on `bernstein acp serve` today versus tracked for PR 2.

> **Legacy REST surface:** The FastAPI endpoints in
> `src/bernstein/core/routes/acp.py` (`GET /.well-known/acp.json`,
> `/acp/v0/agents`, `/acp/v0/runs`) belong to the legacy REST v0 surface and
> are separate from the ACP v1 JSON-RPC bridge documented on this page.

## Transports

| Transport | When to use | Command |
| --------- | ----------- | ------- |
| stdio (line-delimited JSON-RPC 2.0) | IDE embedding (default) | `bernstein acp serve --stdio` |
| HTTP / SSE | remote IDEs, CI, debugging | `bernstein acp serve --http :8062` |

Stdio is the canonical IDE transport: the editor spawns
`bernstein acp serve --stdio` as a subprocess and exchanges line-delimited
JSON-RPC 2.0 frames over `stdin` and `stdout`. All diagnostic logs go to
`stderr` so `stdout` carries strictly valid JSON-RPC frames.

The HTTP transport accepts a single JSON-RPC frame per `POST /acp` request and
supports `Accept: text/event-stream` for streaming responses.

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

## Generic ACP v1 client

```json
{
  "name": "bernstein",
  "transport": "stdio",
  "command": ["bernstein", "acp", "serve", "--stdio"],
  "protocolVersion": 1
}
```

For HTTP-mode integrations, point the client at `http://127.0.0.1:8062/acp`
and set `Accept: text/event-stream` to receive streaming notifications.

## Methods supported (ACP v1)

Every ACP v1 method other than `initialize` requires a completed `initialize`
handshake first; pre-initialization requests return JSON-RPC error `-32600`
(`Invalid Request`).

| Method | Direction | Notes |
| ------ | --------- | ----- |
| `initialize` | client -> server | negotiates `protocolVersion: 1`; returns `agentCapabilities`, `agentInfo`, `authMethods` (empty by default until PR 2 wires the terminal-auth setup target; opt-in `terminal_auth_method` is gated on `clientCapabilities.auth.terminal = true` or `clientCapabilities._meta["terminal-auth"] = true`), and `_meta` (`adapters`, `sandboxBackends`). Legacy methods are rejected with `-32600` on an ACP v1 connection. |
| `authenticate` | client -> server | validates `methodId` against advertised `authMethods` (`-32602` on unknown method) and delegates to the configured `auth_handler`, failing closed with `-32000` when no `auth_handler` is configured or authentication fails. |
| `session/new` | client -> server | requires an absolute `cwd` and `mcpServers` list; returns `{sessionId, modes}` with `manual` (default) and `auto` modes. |
| `session/set_mode` | client -> server | switches the session approval mode between `manual` and `auto`; returns `-32002` for unknown session or `-32602` for invalid mode. |
| `session/prompt` | client -> server | blocking multi-turn request accepting ACP v1 `ContentBlock` arrays (`text`, `resource_link`, `resource`) and resolving with `{stopReason}` (`end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`). On the default `bernstein acp serve` CLI (PR 1), `_default_prompt_runner` forwards the prompt to `--server-url` (`POST /tasks`) and returns `end_turn` or `cancelled` without streaming `session/update`; embedded execution and live streaming are pending PR 2. |
| `session/cancel` | client -> server (notification) | notification-only (no `id`, no reply frame); signals cancellation for the active turn while keeping the session alive for subsequent turns. |
| `session/update` | server -> client (notification) | transport-level outbound notification primitive for turn updates (message chunks, tool-call updates, plan updates, mode updates); live `EventJournal` emission from the default CLI is pending PR 2. |
| `session/request_permission` | server -> client (request) | transport-level bidirectional multiplexed request from agent to client (resolving with `{outcome: {outcome: "selected", optionId}}` or `{outcome: {outcome: "cancelled"}}`); live janitor approval-gate wiring on the default CLI is pending PR 2. |

## Legacy `"2025-04-01"` dialect compatibility

When a client sends `initialize` with a string `protocolVersion` (such as
`"2025-04-01"`) or omits `protocolVersion`, the server negotiates the legacy
dialect and continues accepting `initialized`, `prompt`, `streamUpdate`,
`cancel`, `setMode`, and `requestPermission` so existing fixtures and
integrations keep working without breakage.

## Planned in follow-up runner integration (PR 2 placeholders)

The wire schema, bidirectional stdio multiplexing, handshake, and session
lifecycle above are implemented now. The following runtime features are
tracked for the follow-up runner PR and are **not yet wired** in
`bernstein acp serve --stdio`:

- **Embedded standalone runner:** `build_default_server()` currently forwards
  `session/prompt` to `--server-url` (a running Bernstein task server).
  Standalone in-process execution without a separate server is pending PR 2.
- **`EventJournal` to `session/update` projection:** mapping live orchestrator
  journal events to `agent_message_chunk`, `tool_call`, `tool_call_update`,
  `plan`, and `usage_update` notifications is pending PR 2.
- **Live approval-gate wiring:** routing runtime janitor approval gates
  through `session/request_permission` is pending PR 2 (transport-level
  bidirectional RPC multiplexing is already active).
- **`session/load` replay:** `agentCapabilities.loadSession` currently reports
  `false`; persisted session reload and replay are pending PR 2.
- **Terminal-auth setup flow and server auth headers:** executing the terminal
  setup flow advertised in `authMethods` and forwarding `auth_headers()` to
  `--server-url` are pending PR 2.

## Conformance and registry packaging

- **CI conformance gate:** `.github/workflows/ci.yml` runs the `ACP v1
  conformance` job against `tests/integration/acp/test_acp_v1_conformance.py`
  and `tests/integration/acp/test_acp_v1_sdk.py` using the official
  `agent-client-protocol>=1.1` Python SDK. It verifies the registry
  `run_auth_check` initialize handshake, strict JSON-RPC `stdout` purity,
  official SDK interoperability, and registry package asset rules.
- **Registry assets (prepared, not submitted):**
  `packaging/acp-registry/bernstein/agent.json` and
  `packaging/acp-registry/bernstein/icon.svg` hold the prepared ACP Registry
  manifest and 16x16 monochrome icon for submission after an ACP v1 Bernstein
  release is published to PyPI.

## Observability

Two Prometheus metrics ship with the bridge:

- `bernstein_acp_messages_total{method, outcome}` - JSON-RPC message
  count partitioned by method and outcome (`ok`, `error`, `rejected`,
  `cancelled`, `permission_denied`).
- `bernstein_acp_active_sessions` - gauge of live ACP sessions.

Both are exported via the existing observability stack - the running
task server's `/metrics` endpoint scrapes them automatically.

## Audit

When an `AuditLog` emitter is injected into
`build_default_server(audit_emitter=...)`, ACP-driven mutations produce
HMAC-chained audit entries that are byte-identical (modulo timestamp + chain
HMAC) to the entries the CLI surface emits for the same operation. See
`tests/integration/acp/test_audit_parity.py` for the parity guard.

When `bernstein acp serve` runs with `audit_emitter=None` (the current default
in `src/bernstein/cli/commands/acp_cmd.py`), bridge audit events fall back to
standard Python `logging` on `stderr` until PR 2 wires CLI `AuditLog`
persistence.

## Out of scope

- Windows named-pipe transport - POSIX stdio + HTTP only.
- File-edit primitives that are not in the ratified ACP spec.
- ACP authentication beyond loopback - remote HTTP usage rides the
  existing tunnel wrapper.
