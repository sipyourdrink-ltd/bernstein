## ACP v1 JSON-RPC bridge, transport limits, and conformance gate

`bernstein acp serve` now speaks ACP v1 (`protocolVersion: 1`) over line-delimited
JSON-RPC 2.0 on `stdio` (with diagnostic output routed to `stderr` so `stdout`
carries JSON-RPC frames only) and over HTTP/SSE (`POST /acp` with optional
`Accept: text/event-stream`). Supported ACP v1 methods are `initialize`,
`authenticate`, `session/new`, `session/set_mode` (`manual` and `auto`),
blocking multi-turn `session/prompt` (returning `stopReason`), notification-only
`session/cancel`, and bidirectional stdio multiplexing for agent-to-client
`session/update` notifications and `session/request_permission` requests. Clients
that negotiate the legacy `"2025-04-01"` dialect or omit `protocolVersion`
continue to be served (`initialized`, `prompt`, `streamUpdate`, `cancel`,
`setMode`, `requestPermission`) on the same transport. On `bernstein acp serve
--http`, requests are stateless per POST, so connection-level legacy-on-v1
lockout is not applied (per-session protocol-version checks remain enforced) and
`require_auth=True` fails closed until HTTP authentication is bound to a
per-request token; stdio connections enforce per-connection initialization,
legacy-on-v1 rejection, and optional authentication. CI adds an `ACP v1
conformance` job against the `agent-client-protocol` Python SDK and tracks
prepared ACP Registry package assets under `packaging/acp-registry/bernstein/`
(#8211, #8212).

<!-- begin: #8213 (trim this paragraph if PR 2 lands in a later release) -->
When paired with the embedded ACP runner, `bernstein acp serve --stdio` executes
workspace turns in-process without `--server-url`, projects `EventJournal`
events as `session/update` notifications, bridges runtime approval gates over
`session/request_permission`, supports persisted `session/load` replay
(`loadSession: true`), provides `bernstein acp serve --setup` terminal-auth
verification and default `authenticate("bernstein-setup")`, and forwards
`auth_headers()` when `--server-url` is configured (#8213).
<!-- end: #8213 -->
