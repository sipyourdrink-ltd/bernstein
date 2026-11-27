# ACP native bridge

Bernstein speaks the [Agent Client Protocol](https://agentclientprotocol.org)
(**ACP v1**, `protocolVersion: 1`) over JSON-RPC 2.0 so editors with ACP
support can drive Bernstein directly over stdio or HTTP/SSE.

Existing clients using the legacy `"2025-04-01"` dialect remain supported on
the same transport. By default, `bernstein acp serve` runs the embedded
in-process workspace runner against `<cwd>/.sdd` (see
[Embedded runner, session replay, and terminal setup](#embedded-runner-session-replay-and-terminal-setup)).
Pass `--server-url` to forward tasks to a running Bernstein task server with
`auth_headers()` instead.

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

### HTTP transport limits

Because `POST /acp` requests are stateless per request, the HTTP transport has
the following limits relative to `stdio`:

- **No connection-level dialect lockout:** Unlike `stdio`, HTTP does not reject
  legacy `"2025-04-01"` calls after a v1 `initialize` (or vice versa) across
  requests. Isolation is enforced per session instead: calling legacy session
  methods (`cancel`, `setMode`, `requestPermission`) on a v1 session or v1
  session methods (`session/prompt`, `session/set_mode`) on a legacy session
  returns `-32600`.
- **`require_auth` is refused on HTTP:** `HttpAcpTransport` raises `ValueError`
  if constructed with `registry.require_auth=True` and fails closed with
  `-32000` (`AUTH_REQUIRED`) on requests (or HTTP `202` / empty SSE stream on
  notifications) if `require_auth` is enabled after construction, because HTTP
  does not bind authentication to a per-request token.
- **Sessions are not bound to the peer that created them:** Session IDs are not
  scoped to the creating HTTP peer (a known limit on the HTTP transport). ACP
  v1 session IDs are 64-bit random identifiers (`sess-` plus 16 hex digits from
  `uuid4`).
- **HTTP `authenticate` is a no-op with per-IP method state:**
  With `require_auth=False`, `authenticate` is still dispatched on HTTP if
  called (for example against the default `bernstein-setup` terminal-auth
  method when advertised), but its result is not checked by subsequent HTTP
  requests. Its advertised `authMethods` list is shared per client IP and reset
  on each `initialize`, so one client's `initialize` from the same IP (for
  instance without terminal-auth capabilities) can cause another client's
  `authenticate` call to fail with `-32602` (`unknown authentication method`).
- **No interactive `manual` permission prompts on HTTP:** On HTTP, `manual`
  permission mode cannot prompt the client over `session/request_permission`,
  so gated tool calls are rejected immediately; use `stdio` or `auto` mode.

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
| `initialize` | client -> server | negotiates `protocolVersion: 1`; returns `agentCapabilities` (`loadSession: true`), `agentInfo`, `authMethods` (advertises the `bernstein-setup` terminal-auth method with `args: ["--setup"]` when `clientCapabilities.auth.terminal = true` or `clientCapabilities._meta["terminal-auth"] = true`; empty otherwise unless extra methods are configured), and `_meta` (`adapters`, `sandboxBackends`). Legacy methods are rejected with `-32600` on an ACP v1 `stdio` connection (HTTP relies on per-session version checks; see [HTTP transport limits](#http-transport-limits)). |
| `authenticate` | client -> server | validates `methodId` against advertised `authMethods` (`-32602` on unknown method) and delegates to the configured `auth_handler` (by default validating `bernstein-setup` against local adapter availability, or accepting in `--server-url` mode), failing closed with `-32000` when no `auth_handler` is configured or authentication fails. Auth enforcement applies on `stdio` only; see [HTTP transport limits](#http-transport-limits). |
| `session/new` | client -> server | requires an absolute `cwd` and `mcpServers` list; returns `{sessionId, modes}` with `manual` (default) and `auto` modes and persists session metadata under `<cwd>/.sdd/runtime/acp_sessions/`. |
| `session/load` | client -> server | reloads a persisted ACP v1 session from `<cwd>/.sdd/runtime/acp_sessions/` (verifying `sessionId`, canonical absolute `cwd`, `run_id`, and strict journal containment under `<cwd>/.sdd/runs/<run_id>/`), replays persisted `EventJournal` entries as `session/update` notifications, and returns `{modes}`. |
| `session/set_mode` | client -> server | switches the session approval mode between `manual` and `auto` and persists the updated mode; returns `-32002` for unknown session or `-32602` for invalid mode. |
| `session/prompt` | client -> server | blocking multi-turn request accepting ACP v1 `ContentBlock` arrays (`text`, `resource_link`, `resource`) and resolving with `{stopReason, usage?}` (`end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`). In standalone mode (default when `--server-url` is unset), `EmbeddedOrchestratorRunner` runs governed workspace turns in-process against `<cwd>/.sdd`, streams `EventJournal` updates as `session/update`, and bridges approval gates over `session/request_permission`. With `--server-url`, prompts are forwarded to `POST /tasks` with `auth_headers()`. |
| `session/cancel` | client -> server (notification) | notification-only (no `id`, no reply frame); signals cancellation for the active turn, rejects pending approvals, and reaps the active worker process group (or cancels the remote task when `--server-url` is set) while keeping the session alive for subsequent turns. |
| `session/update` | server -> client (notification) | outbound turn updates projected from `EventJournal` (`agent_message_chunk`, `agent_thought_chunk`, `user_message_chunk`, `tool_call`, `tool_call_update`, `plan`, `usage_update`, `current_mode_update`). |
| `session/request_permission` | server -> client (request) | bidirectional multiplexed agent-to-client request bridging runtime `ApprovalQueue` gates in `manual` mode (resolving with `{outcome: {outcome: "selected", optionId}}` or `{outcome: {outcome: "cancelled"}}`; `allow_always` persists per-session tool allowlists). |

## Legacy `"2025-04-01"` dialect compatibility

When a client sends `initialize` with a string `protocolVersion` (such as
`"2025-04-01"`) or omits `protocolVersion`, the server negotiates the legacy
dialect and continues accepting `initialized`, `prompt`, `streamUpdate`,
`cancel`, `setMode`, and `requestPermission` so existing fixtures and
integrations keep working without breakage.

## Embedded runner, session replay, and terminal setup

- **Embedded standalone runner:** When `--server-url` is omitted,
  `build_default_server()` wires `EmbeddedOrchestratorRunner` to execute turns
  in-process against `<cwd>/.sdd`, persisting tasks in
  `<cwd>/.sdd/runtime/tasks.jsonl` and events in
  `<cwd>/.sdd/runs/<run_id>/journal.jsonl`. Pass `--cli <adapter>` (or set
  `BERNSTEIN_DEFAULT_CLI`) to pin a specific adapter CLI.
- **`EventJournal` to `session/update` projection:** Live journal entries are
  projected by `JournalACPProjector` into ACP v1 `session/update` notifications
  (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
  `tool_call_update`, `plan`, and `usage_update`).
- **Live approval-gate bridging:** In `manual` mode, `ACPApprovalGateBridge`
  forwards pending `ApprovalQueue` items over `session/request_permission`,
  records the resolved `ApprovalPrincipal`, and interrupts pending waits on
  `session/cancel` or transport EOF.
- **`session/load` replay:** `agentCapabilities.loadSession` reports `true`.
  Persisted session metadata under `<cwd>/.sdd/runtime/acp_sessions/` is
  validated for exact `session_id`, absolute canonical `cwd`, matching
  `run_id`, and symlink-free journal containment under
  `<cwd>/.sdd/runs/<run_id>/` before replaying history.
- **Terminal-auth setup and `--server-url` auth headers:**
  `bernstein acp serve --setup` (and `bernstein acp --setup`) checks local
  adapter availability on `PATH` (returning exit code `0` when ready or `1`
  with guidance on `stderr`) and backs the advertised `bernstein-setup`
  terminal-auth method. When `--server-url` is configured, task creation and
  cancellation attach `auth_headers()` resolved for the session workspace.

## Conformance and registry packaging

- **CI conformance gate:** `.github/workflows/ci.yml` runs the `ACP v1
  conformance` job against `tests/integration/acp/test_acp_v1_conformance.py`,
  `tests/integration/acp/test_acp_v1_sdk.py`, and
  `tests/integration/acp/test_acp_v1_pr2_runner.py` using the official
  `agent-client-protocol>=1.1` Python SDK. It verifies the registry
  `run_auth_check` initialize handshake (including non-empty default
  `authMethods`), strict JSON-RPC `stdout` purity, embedded runner updates,
  permission bridging, `session/load` replay, official SDK interoperability,
  and registry package asset rules.
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

When `bernstein acp serve` runs with `audit_emitter=None` (the current CLI
default in `src/bernstein/cli/commands/acp_cmd.py`), bridge audit events fall
back to standard Python `logging` on `stderr`.

## Out of scope

- Windows named-pipe transport - POSIX stdio + HTTP only.
- File-edit primitives that are not in the ratified ACP spec.
- ACP authentication beyond loopback - remote HTTP usage rides the
  existing tunnel wrapper.
