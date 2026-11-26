## Loopback Host header guard and persisted GUI bearer token

Servers bound to a loopback address (`127.0.0.1`, `localhost`, or `::1`) now
enable the `Host` header guard by default and reject requests whose `Host`
header is not loopback (`localhost`, `localhost.localdomain`, `127.0.0.0/8`, or
`::1`) with HTTP 403 (or WebSocket close code 1008). Deployments running a
reverse proxy, tunnel, or container hostname alias in front of a loopback-bound
server are affected when the forwarded `Host` header carries a non-loopback
hostname; set `BERNSTEIN_ALLOWED_HOSTS` to a comma-separated list of permitted
hostnames to allow them. For `bernstein gui serve` and `bernstein run` on
loopback binds, the GUI API bearer token now persists to
`.sdd/runtime/auth.token` (`0600`) when `.sdd/` exists and is reused across
launches instead of rotating on every start (#8181).
