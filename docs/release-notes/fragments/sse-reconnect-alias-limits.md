## Versioned SSE aliases share the reconnect limit

The SSE reconnect limiter counts `/api/v1/events`, `/api/v1/events/cost` and
`/api/v1/events/thread` against the same per-client budget as their canonical
routes.
