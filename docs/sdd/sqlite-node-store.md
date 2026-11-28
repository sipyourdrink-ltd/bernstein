# SQLite WAL cluster node store

`core/protocols/cluster/sqlite_node_store.py` provides `SQLiteNodeStore`, a
WAL-mode SQLite persistence backend for `NodeRegistry` replacing full-file
`nodes.json` rewrites. `server_app.create_app` defaults `.sdd/runtime`
persistence to `nodes.db`, and `NodeRegistry` maps any `.json` `persist_path`
to its sibling `.db` path.

Mutations (`register`, `unregister`, `cordon`, `uncordon`, `start_drain`, and
telemetry-changing heartbeats) write only the affected row under the registry's
mutation lock; unchanged heartbeats coalesce disk writes to a 30-second window,
and `mark_stale` updates timed-out nodes in one transaction. On startup,
`CORDONED` and `DRAINING` operator statuses survive while all other persisted
nodes start `OFFLINE` until their next heartbeat.

When a legacy `nodes.json` exists alongside an empty `nodes.db`, startup imports
all records (including worker health telemetry) in one transaction and renames
the legacy file to `nodes.json.migrated`. See
[cluster operations](../operations/cluster-mode.md) for operator details.
