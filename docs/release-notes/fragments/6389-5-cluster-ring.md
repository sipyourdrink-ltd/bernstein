## Cluster registry ring

Connect the deterministic hash ring to opt-in local registry partitions through
`cluster.shards` and `cluster.shard_vnodes`. Node registration, heartbeat and
administrative mutations use the owning partition; defaults and the flat node
persistence format remain unchanged. HTTP sharding is not enabled.
