## Per-adapter spawn-failure cooldown now actually engages

`SpawnerCore`'s per-adapter crash cooldown compares the current time
against `_agent_failure_timestamps[adapter_name]`, but the only writer of
that dict read `getattr(session, "adapter", "unknown")` -- a field
`AgentSession` has never had (the real field is `endpoint_adapter_name`,
added in #4908). Every crash therefore recorded its timestamp under the
literal key `"unknown"`, which the cooldown lookup (keyed by the real
adapter name) never matched, so the cooldown never fired for any adapter.
Both sites now read `session.endpoint_adapter_name`.
