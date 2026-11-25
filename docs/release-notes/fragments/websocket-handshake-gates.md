## WebSocket handshakes use the HTTP access gates

WebSocket handshakes to `/ws` and `/api/v1/ws` pass the same authentication,
IP allowlist and request-limit middleware as an HTTP `GET` to the same path.
A rejected handshake receives the gate's HTTP status (401, 403 or 429) and
never reaches the event stream. Clients must send `Authorization: Bearer`
on the handshake.
