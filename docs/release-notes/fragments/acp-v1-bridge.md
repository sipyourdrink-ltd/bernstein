## ACP v1 JSON-RPC bridge, transport limits, and conformance gate

`bernstein acp serve` now speaks ACP v1 (`protocolVersion: 1`) over line-delimited
JSON-RPC 2.0 on `stdio` (with diagnostic output routed to `stderr` so `stdout`
carries JSON-RPC frames only) and over loopback HTTP/SSE (`POST /acp` with
optional `Accept: text/event-stream`). Supported ACP v1 methods are `initialize`,
`authenticate`, `session/new`, `session/load`, `session/set_mode` (`manual` and
`auto` on `stdio`), blocking multi-turn `session/prompt` (returning `stopReason`
and optional `usage`), notification-only `session/cancel`, and bidirectional
stdio multiplexing for agent-to-client `session/update` notifications and
`session/request_permission` requests. Clients that negotiate the legacy
`"2025-04-01"` dialect or omit `protocolVersion` continue to be served
(`initialized`, `prompt`, `streamUpdate`, `cancel`, `setMode`,
`requestPermission`) on the same transport. On `bernstein acp serve --http`,
non-loopback binds are refused, loopback `Host` and optional loopback `Origin`
headers are allow-listed (known limit: `Origin` accepts any localhost port),
`auto` mode is refused (`-32600`), local `auth_headers()` are never forwarded
upstream, the embedded runner requires `--allow-embedded-http` (defaulting
otherwise to `--server-url` / `http://localhost:8052`), `require_auth=True`
fails closed, and session ownership is bound by client IP (known limits: HTTP
ownership is per IP rather than per client token, and legacy `cancel` and
`requestPermission` do not enforce an owner check). CI adds an `ACP v1
conformance` job against the `agent-client-protocol` Python SDK and tracks
prepared ACP Registry package assets under `packaging/acp-registry/bernstein/`
(#8211, #8212).

When paired with the embedded ACP runner (`bernstein acp serve --stdio` by
default, or loopback `--http` with `--allow-embedded-http`), turns execute
in-process without `--server-url`, project `EventJournal` events as
`session/update` notifications, bridge runtime approval gates over
`session/request_permission` (prompting `"run agent turn"` with `allow_once` /
`reject_once` only for embedded turns), support persisted `session/load` replay
(`loadSession: true`, always resetting loaded sessions to `manual` mode with an
empty allowlist; capped at 2,000 updates / 16 MiB after reading the full
journal), provide `bernstein acp serve --setup` terminal-auth readiness checks
and default `authenticate("bernstein-setup")`, and attach workspace
`auth_headers()` to `--server-url` requests on `stdio` only (#8213).
