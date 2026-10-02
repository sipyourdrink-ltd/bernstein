## Per-adapter spawn-failure cooldown now actually engages

`SpawnerCore`'s per-adapter crash cooldown compares the current time
against `_agent_failure_timestamps[adapter_name]`, but the only writer of
that dict read `getattr(session, "adapter", "unknown")` -- a field
`AgentSession` has never had (the real field is `endpoint_adapter_name`,
added in #4908). Every crash therefore recorded its timestamp under the
literal key `"unknown"`, which the cooldown lookup (keyed by the real
adapter name) never matched, so the cooldown never fired for any adapter.
Both sites now read `session.endpoint_adapter_name`.

The cooldown starts only after a genuine failure: a known non-zero exit
status that is not a deliberate stop. An agent that exits cleanly after
finishing its task (exit code 0), an agent whose exit status is unknown, and
an agent stopped by a user interrupt, a shutdown signal or a cascaded abort
do not cool the adapter down, so the next task can still spawn on it in the
same tick. After a crash, spawns on that adapter are refused for
`spawn_failure_cooldown_s` (300 s by default).
