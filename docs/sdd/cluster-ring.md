# Cluster registry ring

`core/protocols/cluster/shard_ring.py` provides `HashRing`, `ShardMap` and
`NodeShardStorage`. With nonempty `ClusterConfig.shards`, the existing
`NodeRegistry` uses separate local dictionaries as authoritative partitions.
Registration, heartbeat, lookup and deletion select the owner by stable node ID;
identity lookup, scheduling, stale marking and persistence scan all partitions.
There is no duplicate aggregate node store. The ring and URL map use the same
configured shard IDs.

The default empty shard tuple retains the existing single dictionary. The flat
node persistence format is unchanged: loading inserts each record into its
current owner and starts it OFFLINE as before. Configured iteration follows shard
configuration order, then partition insertion order. This may change tie ordering
only for installations that opt in.

YAML accepts `cluster.shards: {<id>: <http(s)-base-url>}` and
`cluster.shard_vnodes: 128`; the runtime config stores an immutable tuple of
ID/URL pairs. See [cluster operations](../operations/cluster-mode.md) for
validation and an example. URLs are metadata: these are local storage partitions,
not distributed central servers. No HTTP discovery, 421 response, forwarding,
client routing or online topology migration is implemented.

Regression coverage lives in `tests/unit/test_registry_shards.py` and
`tests/unit/test_shard_ring.py`, including real application routes.

Aggregate reads snapshot partition membership under the same lock as structural
mutations. The snapshots retain the registry's shared node objects.

The default server factory reads only shard settings from its selected effective
seed mapping at startup, preserving environment-derived authentication and cluster
enablement. Invalid shard settings fail startup validation; unrelated metadata
errors retain the existing handled reload path. Metadata reload does not replace
the live ring; restart is required to change partitions.
