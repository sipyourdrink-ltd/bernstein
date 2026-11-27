# ACP native bridge

Bernstein speaks the [Agent Client Protocol](https://agentclientprotocol.org)
(**ACP v1**, `protocolVersion: 1`) over JSON-RPC 2.0 so editors with ACP
support can drive Bernstein directly over stdio or loopback HTTP/SSE.

Existing clients using the legacy `"2025-04-01"` dialect remain supported on
the same transport. On `--stdio` (the default transport), `bernstein acp serve`
runs the embedded in-process workspace runner against `<cwd>/.sdd` (see
[Embedded runner, session replay, and terminal setup](#embedded-runner-session-replay-and-terminal-setup));
pass `--server-url` (or set `BERNSTEIN_SERVER_URL`) to forward tasks to a
running Bernstein task server with `auth_headers()` instead. On `--http`, the
server binds loopback only and defaults to forwarding to `--server-url` (or
`http://localhost:8052`) without attaching local `auth_headers()`; pass
`--allow-embedded-http` on a loopback bind to opt into the embedded runner over
HTTP.

> **Legacy REST surface:** The FastAPI endpoints in
> `src/bernstein/core/routes/acp.py` (`GET /.well-known/acp.json`,
> `/acp/v0/agents`, `/acp/v0/runs`) belong to the legacy REST v0 surface and
> are separate from the ACP v1 JSON-RPC bridge documented on this page.

## Transports

| Transport | When to use | Command |
| --------- | ----------- | ------- |
| stdio (line-delimited JSON-RPC 2.0) | IDE embedding, embedded runner (default) | `bernstein acp serve --stdio` |
| HTTP / SSE (loopback only) | local debugging, CI, or behind a tunnel wrapper | `bernstein acp serve --http :8062` |

Stdio is the canonical IDE transport: the editor spawns
`bernstein acp serve --stdio` as a subprocess and exchanges line-delimited
JSON-RPC 2.0 frames over `stdin` and `stdout`. All diagnostic logs go to
`stderr` so `stdout` carries strictly valid JSON-RPC frames.

The HTTP transport accepts a single JSON-RPC frame per `POST /acp` request,
supports `Accept: text/event-stream` for streaming responses, and binds
loopback addresses only (`127.0.0.1`, `::1`, or `localhost`).

### HTTP transport limits

Because `POST /acp` requests are stateless and unauthenticated per request, the
HTTP transport enforces the following restrictions and carries the following
known limits relative to `stdio`:

- **Loopback bind only:** `bernstein acp serve --http HOST:PORT` (and
  `_run_http`) refuses any non-loopback host (`127.0.0.1`, `::1`, and
  `localhost` only). Remote HTTP access must go through a tunnel wrapper.
- **Embedded runner requires `--allow-embedded-http`:** Without
  `--allow-embedded-http`, `--http` defaults to forwarding tasks to
  `--server-url` (falling back to `BERNSTEIN_SERVER_URL` or
  `http://localhost:8052`). Passing `--allow-embedded-http` enables the
  embedded workspace runner on a loopback HTTP bind; it requires `--http` and
  cannot be combined with CLI `--server-url`.
- **Local `auth_headers()` are never forwarded for HTTP peers:** `bernstein acp
  serve --http` disables upstream auth-header forwarding
  (`forward_auth_headers=False`), and `_should_attach_auth_headers()` prevents
  local `auth_headers()` from being attached to `--server-url` task creation or
  cancellation on behalf of any HTTP peer.
- **`Host` and `Origin` header allow-listing:** Every HTTP request must supply
  exactly one loopback `Host` header (otherwise HTTP `403 forbidden host`).
  `Origin` is optional for non-browser clients, but when present it must appear
  once, must not be `null`, and must be an `http`/`https` loopback origin with
  no userinfo, path, query, or fragment (otherwise HTTP `403 forbidden
  origin`). **Known limit:** the `Origin` allow-list accepts any valid
  localhost/loopback port rather than restricting to the listening port.
- **`auto` mode is refused over HTTP:** HTTP peers cannot enable or run `auto`
  approval mode. `session/set_mode`, `session/prompt`, legacy `prompt`, and
  legacy `setMode` return `-32600` (`Invalid Request`) for `auto` mode over
  HTTP, and the embedded runner never auto-approves HTTP peers.
- **No interactive `manual` permission prompts on HTTP:** Stateless HTTP has no
  bidirectional `client_requester`, so `manual` mode cannot send
  `session/request_permission` prompts to the client; gated embedded-runner
  turns fail closed (`transport_unsupported` / rejected). Because HTTP also
  refuses `auto` mode, embedded turns that require approval prompts or `auto`
  mode must use `--stdio`.
- **Session ownership binding and known limits:** Sessions record `owner_peer`
  (`"stdio"` on stdio; `"http://<peer_ip>"` on HTTP). ACP v1 `session/load`,
  `session/set_mode`, `session/prompt`, `session/cancel`, and legacy `setMode`
  enforce `owner_peer == ctx.peer` (rejecting non-owners with `-32002` /
  `-32001`, or ignoring `session/cancel` notifications). **Known limits:**
  - On HTTP, ownership is bound by client IP (`http://<peer_ip>`), not a
    per-client credential, so processes sharing the same loopback IP share the
    same owner identity (ACP v1 session IDs are 64-bit random `sess-` + 16 hex
    digits from `uuid4`).
  - Legacy `cancel` and legacy `requestPermission` do not enforce an
    `owner_peer` check (they still reject ACP v1 sessions with `-32600`).
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
- **HTTP `authenticate` is a no-op with per-IP method state:**
  With `require_auth=False`, `authenticate` is still dispatched on HTTP if
  called (for example against the default `bernstein-setup` terminal-auth
  method when advertised), but its result is not checked by subsequent HTTP
  requests. Its advertised `authMethods` list is shared per client IP and reset
  on each `initialize`, so one client's `initialize` from the same IP (for
  instance without terminal-auth capabilities) can cause another client's
  `authenticate` call to fail with `-32602` (`unknown authentication method`).

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

For loopback HTTP integrations, point the client at
`http://127.0.0.1:8062/acp` with a loopback `Host` header (and optional
loopback `Origin`) and set `Accept: text/event-stream` to receive streaming
notifications.

## Methods supported (ACP v1)

Every ACP v1 method other than `initialize` requires a completed `initialize`
handshake first; pre-initialization requests return JSON-RPC error `-32600`
(`Invalid Request`).

| Method | Direction | Notes |
| ------ | --------- | ----- |
| `initialize` | client -> server | negotiates `protocolVersion: 1`; returns `agentCapabilities` (`loadSession: true`), `agentInfo`, `authMethods` (advertises the `bernstein-setup` terminal-auth method with `args: ["--setup"]` when `clientCapabilities.auth.terminal = true` or `clientCapabilities._meta["terminal-auth"] = true`; empty otherwise unless extra methods are configured), and `_meta` (`adapters`, `sandboxBackends`). Legacy methods are rejected with `-32600` on an ACP v1 `stdio` connection (HTTP relies on per-session version checks; see [HTTP transport limits](#http-transport-limits)). |
| `authenticate` | client -> server | validates `methodId` against advertised `authMethods` (`-32602` on unknown method) and delegates to the configured `auth_handler` (by default validating `bernstein-setup` against local adapter availability, or accepting in `--server-url` mode; this default method is an adapter-readiness check rather than caller-identity access control), failing closed with `-32000` when no `auth_handler` is configured or authentication fails. Auth enforcement applies on `stdio` only; see [HTTP transport limits](#http-transport-limits). |
| `session/new` | client -> server | requires an absolute `cwd` and `mcpServers` list; binds `owner_peer` to the creating peer, returns `{sessionId, modes}` starting in `manual` mode (with `manual` and `auto` advertised), and persists session metadata under `<cwd>/.sdd/runtime/acp_sessions/`. |
| `session/load` | client -> server | reloads a persisted ACP v1 session from `<cwd>/.sdd/runtime/acp_sessions/` (verifying `sessionId`, `owner_peer`, canonical absolute `cwd`, `run_id`, and strict symlink-free journal containment under `<cwd>/.sdd/runs/<run_id>/`). Always resets the loaded session to `manual` mode with an empty `always_allowed_tools` set (never restoring approval-relaxing fields from the agent-writable workspace), replays persisted `EventJournal` entries as `session/update` notifications (capped to the newest 2,000 updates / 16 MiB; reads the full journal before slicing), and returns `{modes}`. |
| `session/set_mode` | client -> server | switches the session approval mode between `manual` and `auto` on `stdio`; returns `-32600` if an HTTP peer requests `auto`, `-32002` for unknown or non-owned session, or `-32602` for invalid mode. Loaded sessions always reset to `manual` on `session/load`. |
| `session/prompt` | client -> server | blocking multi-turn request accepting ACP v1 `ContentBlock` arrays (`text`, `resource_link`, `resource`) and resolving with `{stopReason, usage?}` (`end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`). Enforces `owner_peer` (`-32002`) and rejects `auto` mode over HTTP (`-32600`). In standalone embedded mode (default on `--stdio`; opt-in via `--allow-embedded-http` on loopback `--http`), `EmbeddedOrchestratorRunner` runs governed workspace turns in-process against `<cwd>/.sdd`, streams `EventJournal` updates as `session/update`, and bridges approval gates over `session/request_permission`. With `--server-url`, prompts are forwarded to `POST /tasks` with `auth_headers()` on `stdio` only (never for HTTP peers). |
| `session/cancel` | client -> server (notification) | notification-only (no `id`, no reply frame); ignores non-owner peers, signals cancellation for the active turn, rejects pending approvals, and reaps the active worker process group (or cancels the remote task when `--server-url` is set) while keeping the session alive for subsequent turns. |
| `session/update` | server -> client (notification) | outbound turn updates projected from `EventJournal` (`agent_message_chunk`, `agent_thought_chunk`, `user_message_chunk`, `tool_call`, `tool_call_update`, `plan`, `usage_update`), plus `current_mode_update` when emitted explicitly via `emit_current_mode_update()`. |
| `session/request_permission` | server -> client (request) | bidirectional multiplexed agent-to-client request bridging runtime `ApprovalQueue` gates in `manual` mode (resolving with `{outcome: {outcome: "selected", optionId}}` or `{outcome: {outcome: "cancelled"}}`). For embedded turn execution (`run_agent_turn`), the prompt title reads `"run agent turn: <goal summary>"` (`kind: "execute"`) and offers `allow_once` (`Allow once`) and `reject_once` (`Reject`) only. Non-turn tool approvals offer `allow_once`, `allow_always`, and `reject_once`; `allow_always` applies only within the live in-memory session and is cleared on `session/load`. Fails closed (`transport_unsupported`) on HTTP. |

## Legacy `"2025-04-01"` dialect compatibility

When a client sends `initialize` with a string `protocolVersion` (such as
`"2025-04-01"`) or omits `protocolVersion`, the server negotiates the legacy
dialect and continues accepting `initialized`, `prompt`, `streamUpdate`,
`cancel`, `setMode`, and `requestPermission` so existing fixtures and
integrations keep working without breakage. On HTTP, legacy `prompt` and
`setMode` also reject `auto` mode with `-32600`; legacy `setMode` checks
`owner_peer`, whereas legacy `cancel` and `requestPermission` do not enforce an
`owner_peer` check.

## Embedded runner, session replay, and terminal setup

- **Embedded standalone runner:** On `--stdio` with `--server-url` omitted,
  `build_default_server()` wires `EmbeddedOrchestratorRunner` to execute turns
  in-process against `<cwd>/.sdd`, persisting tasks in
  `<cwd>/.sdd/runtime/tasks.jsonl` and events in
  `<cwd>/.sdd/runs/<run_id>/journal.jsonl`. On `--http`, the CLI defaults to
  forwarding to `--server-url` (`SERVER_URL` / `http://localhost:8052`) and
  requires `--allow-embedded-http` on a loopback bind to enable the embedded
  runner. Pass `--cli <adapter>` (or set `BERNSTEIN_DEFAULT_CLI`) to pin a
  specific adapter CLI.
- **`EventJournal` to `session/update` projection:** Live journal entries are
  projected by `JournalACPProjector` into ACP v1 `session/update` notifications
  (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
  `tool_call_update`, `plan`, and `usage_update`).
- **Live approval-gate bridging:** In `manual` mode on `stdio`,
  `ACPApprovalGateBridge` forwards pending `ApprovalQueue` items over
  `session/request_permission`, ignores forged unsigned `*.resolved.json` disk
  files for in-process approvals, records the nonce-checked resolution with a
  client-asserted `ApprovalPrincipal` (`acp-client-asserted:<clientInfo.name>`),
  and interrupts pending waits on `session/cancel` or transport EOF. Each
  embedded turn is gated under `run_agent_turn` (`"run agent turn: <goal
  summary>"`), offering `allow_once` and `reject_once` only.
- **`session/load` replay and known limit:** `agentCapabilities.loadSession`
  reports `true`. Persisted session metadata under
  `<cwd>/.sdd/runtime/acp_sessions/` is validated for exact `session_id`,
  matching `owner_peer`, absolute canonical `cwd`, matching `run_id`, and
  symlink-free journal containment under `<cwd>/.sdd/runs/<run_id>/` before
  replaying history. Approval-relaxing fields (`mode` and
  `always_allowed_tools`) are never restored from the workspace file: loaded
  sessions always start in `manual` mode with an empty allowlist. Replay emits
  at most the newest 2,000 updates and 16 MiB (`MAX_SESSION_LOAD_UPDATES` /
  `MAX_SESSION_LOAD_BYTES`); **known limit:** `project_journal_to_acp_updates`
  reads and projects the whole `journal.jsonl` file before slicing to those
  caps.
- **Terminal-auth setup and `--server-url` auth headers:**
  `bernstein acp serve --setup` (and `bernstein acp --setup`) checks local
  adapter availability on `PATH` (returning exit code `0` when ready or `1`
  with guidance on `stderr`) and backs the advertised `bernstein-setup`
  terminal-auth method (an adapter-readiness gate rather than caller-identity
  access control). When `--server-url` is configured, task creation and
  cancellation attach workspace `auth_headers()` on local `stdio` connections
  only and never on behalf of HTTP peers.

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

- Windows named-pipe transport - POSIX stdio + loopback HTTP only.
- File-edit primitives that are not in the ratified ACP spec.
- ACP authentication beyond loopback - `--http` refuses non-loopback binds, so
  remote HTTP usage rides the existing tunnel wrapper.
